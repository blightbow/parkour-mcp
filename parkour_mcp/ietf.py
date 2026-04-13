"""IETF RFC and Internet-Draft integration via RFC Editor and Datatracker APIs."""

import asyncio
import logging
import re
import xml.etree.ElementTree as ET
from typing import Annotated, Optional

from pydantic import Field

import httpx

from .common import _API_HEADERS, _API_USER_AGENT, RateLimiter, s2_enabled, tool_name
from .markdown import _build_frontmatter, _fence_content

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# URL detection
# ---------------------------------------------------------------------------
# Matches rfc-editor.org/rfc/rfc{N} with any suffix (.json, .html, .txt, .xml)
# or bare path.  Does NOT match /info/ pages (used for subseries resolution).
_RFC_EDITOR_RE = re.compile(
    r'https?://www\.rfc-editor\.org/rfc/rfc(\d+)(?:\.\w+)?',
    re.IGNORECASE,
)

# Matches datatracker.ietf.org/doc/{rfc{N}|draft-*}
_DATATRACKER_RE = re.compile(
    r'https?://datatracker\.ietf\.org/doc/(rfc(\d+)|draft-[\w.-]+)/?',
    re.IGNORECASE,
)

# Matches RFC DOIs: 10.17487/RFC{N}
_RFC_DOI_RE = re.compile(r'^10\.17487/RFC(\d+)$', re.IGNORECASE)

# Parse subseries identifiers: STD97, BCP14, FYI36, etc.
_SUBSERIES_ID_RE = re.compile(
    r'^(STD|BCP|FYI)\s*0*(\d+)$', re.IGNORECASE,
)


def _detect_ietf_url(url: str) -> Optional[dict]:
    """Detect an IETF RFC or Internet-Draft URL.

    Returns ``{"type": "rfc", "number": int}`` for RFC URLs,
    ``{"type": "draft", "name": str}`` for I-D URLs, or None.
    """
    m = _RFC_EDITOR_RE.search(url)
    if m:
        return {"type": "rfc", "number": int(m.group(1))}

    m = _DATATRACKER_RE.search(url)
    if m:
        if m.group(2):
            # rfc{N}
            return {"type": "rfc", "number": int(m.group(2))}
        # draft-*
        return {"type": "draft", "name": m.group(1)}

    return None


# ---------------------------------------------------------------------------
# Rate limiter — 1s between Datatracker API requests (undocumented limit).
# No limiter for RFC Editor CDN (static files, Cloudflare-cached).
# ---------------------------------------------------------------------------
_datatracker_limiter = RateLimiter(1.0)


# ---------------------------------------------------------------------------
# RFC Editor JSON fetch (metadata)
# ---------------------------------------------------------------------------

async def _fetch_rfc_metadata(number: int) -> Optional[dict]:
    """Fetch per-document JSON from the RFC Editor.

    Returns parsed JSON dict or None on failure.
    """
    url = f"https://www.rfc-editor.org/rfc/rfc{number}.json"
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(url, headers=_API_HEADERS, timeout=15.0)
            if resp.status_code == 404:
                return None
            resp.raise_for_status()
            return resp.json()
    except Exception:
        logger.debug("RFC Editor fetch failed for RFC %d", number, exc_info=True)
        return None


# ---------------------------------------------------------------------------
# Datatracker convenience fetch (denormalized metadata)
# ---------------------------------------------------------------------------

async def _fetch_datatracker_doc(name: str) -> Optional[dict]:
    """Fetch denormalized metadata from Datatracker /doc/{name}/doc.json.

    Args:
        name: Document name, e.g. "rfc9110" or "draft-ietf-httpbis-semantics".
    """
    await _datatracker_limiter.wait()
    url = f"https://datatracker.ietf.org/doc/{name}/doc.json"
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                url,
                headers={"User-Agent": _API_USER_AGENT, "Accept": "application/json"},
                timeout=15.0,
            )
            if resp.status_code == 404:
                return None
            resp.raise_for_status()
            return resp.json()
    except Exception:
        logger.debug("Datatracker fetch failed for %s", name, exc_info=True)
        return None


# ---------------------------------------------------------------------------
# Datatracker REST search
# ---------------------------------------------------------------------------

async def _search_rfcs(
    query: str,
    limit: int = 10,
    offset: int = 0,
    status: Optional[str] = None,
    wg: Optional[str] = None,
) -> tuple[list[dict], int]:
    """Search RFCs via Datatracker REST API.

    Returns (results_list, total_count).
    """
    await _datatracker_limiter.wait()
    params: dict[str, str | int] = {
        "title__icontains": query,
        "name__startswith": "rfc",
        "limit": min(limit, 50),
        "offset": offset,
    }
    if status:
        # Map short slugs to Datatracker std_level URIs
        slug_map = {
            "ps": "ps", "std": "std", "bcp": "bcp",
            "inf": "inf", "exp": "exp", "hist": "hist",
        }
        slug = slug_map.get(status.lower())
        if slug:
            params["std_level"] = f"/api/v1/name/stdlevelname/{slug}/"
    if wg:
        params["group__acronym"] = wg

    url = "https://datatracker.ietf.org/api/v1/doc/document/"
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                url,
                params=params,
                headers={"User-Agent": _API_USER_AGENT, "Accept": "application/json"},
                timeout=15.0,
            )
            resp.raise_for_status()
            data = resp.json()
            objects = data.get("objects") or []
            total = data.get("meta", {}).get("total_count", 0)
            return objects, total
    except Exception:
        logger.debug("Datatracker search failed for %r", query, exc_info=True)
        return [], 0


# ---------------------------------------------------------------------------
# Subseries resolution (STD, BCP, FYI)
# ---------------------------------------------------------------------------

async def _resolve_subseries(ref: str) -> Optional[str]:
    """Resolve a subseries identifier to its constituent RFCs.

    Uses the IETF BibXML service which returns a ``<referencegroup>``
    containing one ``<reference>`` per member RFC with full metadata.

    Args:
        ref: Subseries ID, e.g. "STD97", "BCP14", "FYI36", or
             zero-padded from see_also like "STD0097".

    Returns formatted markdown listing member RFCs, or None on failure.
    """
    m = _SUBSERIES_ID_RE.match(ref.strip())
    if not m:
        return None

    series_type = m.group(1).upper()
    series_num = int(m.group(2))
    bibxml_url = (
        f"https://bib.ietf.org/public/rfc/bibxml9/"
        f"reference.{series_type}.{series_num:04d}.xml"
    )

    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(bibxml_url, headers=_API_HEADERS, timeout=15.0)
            if resp.status_code == 404:
                return None
            resp.raise_for_status()
            xml_text = resp.text
    except Exception:
        logger.debug("BibXML fetch failed for %s", ref, exc_info=True)
        return None

    # Parse <referencegroup> → <reference anchor="RFC{N}"> children
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        logger.debug("BibXML parse failed for %s", ref, exc_info=True)
        return None

    # Handle both <referencegroup> wrapper and bare <reference>
    if root.tag == "referencegroup":
        refs = root.findall("reference")
    elif root.tag == "reference":
        refs = [root]
    else:
        # Try namespace-aware search
        refs = root.findall(".//{*}reference") or root.findall(".//reference")

    if not refs:
        return None

    lines = []
    rfc_numbers = []
    for ref_el in refs:
        anchor = ref_el.get("anchor", "")
        # Extract RFC number from anchor like "RFC2119"
        rfc_match = re.match(r'RFC(\d+)', anchor, re.IGNORECASE)
        rfc_num = int(rfc_match.group(1)) if rfc_match else 0
        if rfc_num:
            rfc_numbers.append(rfc_num)

        # Extract title from <front><title>
        title_el = ref_el.find("front/title")
        title = title_el.text if title_el is not None and title_el.text else "front/title:Missing"

        # Extract date
        date_el = ref_el.find("front/date")
        date_str = ""
        if date_el is not None:
            month = date_el.get("month", "")
            year = date_el.get("year", "")
            date_str = f"{month} {year}".strip()

        # Extract authors
        author_els = ref_el.findall("front/author")
        authors = []
        for a in author_els:
            fullname = a.get("fullname", "")
            initials = a.get("initials", "")
            surname = a.get("surname", "")
            if fullname:
                authors.append(fullname)
            elif surname:
                authors.append(f"{initials} {surname}".strip())

        # Extract series info for status
        series_els = ref_el.findall("seriesInfo")
        rfc_label = anchor
        for si in series_els:
            if si.get("name") == "RFC":
                rfc_label = f"RFC {si.get('value', '')}"

        author_str = ", ".join(authors) if authors else ""
        lines.append(
            f"- **{rfc_label}**: {title}"
            + (f" ({date_str})" if date_str else "")
            + (f"\n  Authors: {author_str}" if author_str else "")
        )

    info_url = f"https://www.rfc-editor.org/info/{series_type.lower()}{series_num}"
    fm = _build_frontmatter({
        "source": info_url,
        "api": "IETF (BibXML)",
        "subseries": f"{series_type} {series_num}",
        "member_count": len(rfc_numbers),
        "see_also": "Use IETF tool with rfc action for details on any member RFC",
    })
    body = f"# {series_type} {series_num}\n\n" + "\n".join(lines)
    return fm + "\n\n" + _fence_content(body)


def _subseries_label(see_also: list[str]) -> Optional[str]:
    """Build a compact subseries label from a ``see_also`` list.

    Returns e.g. ``"STD 97"`` or None if no subseries refs present.
    """
    for ref in see_also:
        m = _SUBSERIES_ID_RE.match(ref.strip())
        if m:
            return f"{m.group(1).upper()} {int(m.group(2))}"
    return None


# ---------------------------------------------------------------------------
# Format RFC metadata as markdown
# ---------------------------------------------------------------------------

def _format_rfc_paper(meta: dict) -> str:
    """Format RFC Editor JSON into a markdown body."""
    doc_id = meta.get("doc_id", "")
    number = str(int(re.sub(r'\D', '', doc_id))) if re.search(r'\d', doc_id) else doc_id
    title = (meta.get("title") or "").strip() or f"RFC {number} title:Missing"
    authors = meta.get("authors") or []
    pub_date = meta.get("pub_date", "")
    status = meta.get("status", "Unknown")
    abstract = (meta.get("abstract") or "").strip()
    keywords = meta.get("keywords") or []
    source_wg = meta.get("source", "")
    page_count = meta.get("page_count", "")
    draft = meta.get("draft", "")
    errata_url = meta.get("errata_url")
    formats = meta.get("format") or []

    lines = [f"# RFC {number}: {title}\n"]

    # Authors and date
    if authors:
        lines.append(f"**Authors:** {', '.join(authors)}")
    lines.append(f"**Date:** {pub_date}")
    lines.append(f"**Status:** {status}")
    if source_wg:
        lines.append(f"**Working Group:** {source_wg}")
    if page_count:
        lines.append(f"**Pages:** {page_count}")
    if draft:
        lines.append(f"**Origin:** {draft}")
    lines.append("")

    # Abstract
    if abstract:
        lines.append("## Abstract\n")
        lines.append(abstract)
        lines.append("")

    # Keywords
    clean_keywords = [k.strip() for k in keywords if k.strip()]
    if clean_keywords:
        lines.append(f"**Keywords:** {', '.join(clean_keywords)}")
        lines.append("")

    # Relationship chains
    for label, key in [
        ("Obsoletes", "obsoletes"),
        ("Obsoleted by", "obsoleted_by"),
        ("Updates", "updates"),
        ("Updated by", "updated_by"),
    ]:
        refs = meta.get(key) or []
        if refs:
            linked = ", ".join(refs)
            lines.append(f"**{label}:** {linked}")
    if any(meta.get(k) for k in ("obsoletes", "obsoleted_by", "updates", "updated_by")):
        lines.append("")

    # Available formats
    if formats:
        n = number or doc_id.replace("RFC", "")
        fmt_links = []
        for fmt in formats:
            fl = fmt.lower()
            if fl in ("html", "text", "pdf", "xml", "ascii"):
                ext = "txt" if fl in ("text", "ascii") else fl
                fmt_links.append(
                    f"[{fmt}](https://www.rfc-editor.org/rfc/rfc{n}.{ext})"
                )
        if fmt_links:
            lines.append(f"**Formats:** {' | '.join(fmt_links)}")

    # Errata
    if errata_url:
        lines.append(f"**Errata:** [{errata_url}]({errata_url})")

    return "\n".join(lines)


def _format_rfc_list(
    results: list[dict], total: int, offset: int,
) -> str:
    """Format Datatracker search results as a compact numbered list."""
    lines = []
    for i, doc in enumerate(results, start=offset + 1):
        name = doc.get("name", "")
        title = (doc.get("title") or "").strip() or f"{name} title:Missing"
        # Extract RFC number from name like "rfc9110"
        num_match = re.match(r'rfc(\d+)', name, re.IGNORECASE)
        rfc_num = num_match.group(1) if num_match else name
        pages = doc.get("pages") or ""
        page_info = f", {pages}p" if pages else ""
        lines.append(f"{i}. **RFC {rfc_num}**: {title}{page_info}")

    if total > offset + len(results):
        remaining = total - offset - len(results)
        lines.append(f"\n*{remaining} more results available (use offset={offset + len(results)})*")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Main RFC paper fetch (with shelf tracking)
# ---------------------------------------------------------------------------

async def _fetch_rfc_paper(number: int) -> str:
    """Fetch a single RFC and return formatted markdown with frontmatter.

    Concurrent: RFC Editor JSON + formatted APA citation via DOI.
    Passive shelf tracking via CitationRecord.
    """
    from .doi import fetch_formatted_citation
    from .shelf import _track_on_shelf, CitationRecord

    rfc_doi = f"10.17487/RFC{number:04d}"

    # Concurrent: metadata + APA citation
    meta_result, cite_result = await asyncio.gather(
        _fetch_rfc_metadata(number),
        fetch_formatted_citation(rfc_doi),
        return_exceptions=True,
    )
    meta = meta_result if isinstance(meta_result, dict) else None
    citation_text = cite_result if isinstance(cite_result, str) else None

    if not meta:
        return f"Error: RFC {number} not found."

    title = (meta.get("title") or "").strip() or f"RFC {number} title:Missing"
    authors = meta.get("authors") or []
    status = meta.get("status", "Unknown")
    pub_status = meta.get("pub_status", "")
    pub_date = meta.get("pub_date", "")
    see_also = meta.get("see_also") or []

    # Extract year from pub_date (e.g. "June 2022" → 2022)
    year = None
    year_match = re.search(r'\b(\d{4})\b', pub_date)
    if year_match:
        year = int(year_match.group(1))

    # Passive shelf tracking
    shelf_result = await _track_on_shelf(CitationRecord(
        doi=rfc_doi,
        title=title,
        authors=authors,
        year=year,
        venue="IETF",
        source_tool="ietf",
    ))

    # Build frontmatter
    fm_entries: dict[str, object] = {
        "title": title,
        "source": f"https://www.rfc-editor.org/rfc/rfc{number}",
        "api": "IETF (RFC Editor)",
        "status": status,
        "doi": rfc_doi,
        "shelf": shelf_result.status_line,
        "full_text": (
            f"Use {tool_name('web_fetch_direct')} with https://www.rfc-editor.org/rfc/rfc{number}.html "
            "for full RFC text with search/slices"
        ),
        "see_also": (
            f"Use {tool_name('semantic_scholar')} with DOI:{rfc_doi} for citation data"
            if s2_enabled() else None
        ),
    }

    # Subseries membership
    sub_label = _subseries_label(see_also)
    if sub_label:
        fm_entries["subseries"] = sub_label

    # Relationship chains in frontmatter
    for key in ("obsoletes", "obsoleted_by", "updates", "updated_by"):
        refs = meta.get(key) or []
        if refs:
            fm_entries[key] = refs

    # UNKNOWN status note — pub_status reflects original publication status
    if pub_status == "UNKNOWN":
        fm_entries["note"] = (
            "RFC predates the current status system; treat as informational at best"
        )

    fm = _build_frontmatter(fm_entries)
    body = _format_rfc_paper(meta)
    if citation_text:
        body += f"\n\n## Citation\n\n{citation_text}\n"
    return fm + "\n\n" + _fence_content(body, title=title)


# ---------------------------------------------------------------------------
# Internet-Draft lookup
# ---------------------------------------------------------------------------

async def _fetch_draft(name: str) -> str:
    """Fetch an Internet-Draft via Datatracker and return formatted markdown."""
    doc = await _fetch_datatracker_doc(name)
    if not doc:
        return f"Error: Internet-Draft '{name}' not found."

    title = (doc.get("title") or "").strip() or f"{name} title:Missing"
    authors = doc.get("authors") or []
    rev = doc.get("rev", "")
    state = doc.get("iesg_state") or doc.get("state") or ""
    group_info = doc.get("group") or {}
    group_name = group_info.get("name", "")
    group_acronym = group_info.get("acronym", "")
    abstract = doc.get("abstract", "").strip()
    std_level = doc.get("std_level") or ""
    stream = doc.get("stream") or ""
    rev_history = doc.get("rev_history") or []

    lines = [f"# {name}\n"]
    lines.append(f"**Title:** {title}")
    if authors:
        author_names = [
            a.get("name", "") if isinstance(a, dict) else str(a)
            for a in authors
        ]
        lines.append(f"**Authors:** {', '.join(author_names)}")
    if rev:
        lines.append(f"**Revision:** {rev}")
    if state:
        lines.append(f"**State:** {state}")
    if std_level:
        lines.append(f"**Intended Status:** {std_level}")
    if stream:
        lines.append(f"**Stream:** {stream}")
    if group_name:
        wg_label = f"{group_name} ({group_acronym})" if group_acronym else group_name
        lines.append(f"**Working Group:** {wg_label}")
    lines.append("")

    if abstract:
        lines.append("## Abstract\n")
        lines.append(abstract)
        lines.append("")

    # Revision history (last few entries)
    if rev_history:
        lines.append("## Revision History\n")
        for entry in rev_history[-5:]:
            if isinstance(entry, dict):
                rev_name = entry.get("name", "")
                rev_num = entry.get("rev", "")
                published = entry.get("published", "")
                lines.append(f"- {rev_name}-{rev_num} ({published})")
        if len(rev_history) > 5:
            lines.append(f"- ... ({len(rev_history) - 5} earlier revisions)")
        lines.append("")

    fm = _build_frontmatter({
        "title": title,
        "source": f"https://datatracker.ietf.org/doc/{name}/",
        "api": "IETF (Datatracker)",
        "state": state,
        "see_also": (
            f"Use {tool_name('web_fetch_direct')} with https://www.ietf.org/archive/id/{name}-{rev}.html "
            "for full draft text"
            if rev else None
        ),
    })
    body = "\n".join(lines)
    return fm + "\n\n" + _fence_content(body, title=title)


# ---------------------------------------------------------------------------
# Standalone MCP tool
# ---------------------------------------------------------------------------

async def ietf(
    action: Annotated[str, Field(
        description=(
            "The operation to perform. "
            "rfc: look up a single RFC by number or URL. "
            "search: search RFCs by keyword via IETF Datatracker. "
            "draft: look up an Internet-Draft by name or URL. "
            "subseries: resolve a subseries (STD, BCP, FYI) to its constituent RFCs."
        ),
    )],
    query: Annotated[str, Field(
        description=(
            "For rfc: RFC number (e.g. '9110') or RFC URL. "
            "For search: keywords for title search. "
            "For draft: Internet-Draft name (e.g. 'draft-ietf-httpbis-semantics') or URL. "
            "For subseries: subseries identifier (e.g. 'STD97', 'BCP14', 'FYI36')."
        ),
    )],
    limit: Annotated[int, Field(
        description="Maximum results to return for search (default 10, max 50).",
    )] = 10,
    offset: Annotated[int, Field(
        description="Starting position for search pagination.",
    )] = 0,
    status: Annotated[Optional[str], Field(
        description=(
            "Filter search by RFC status: ps (Proposed Standard), std (Internet Standard), "
            "bcp (Best Current Practice), inf (Informational), exp (Experimental), "
            "hist (Historic)."
        ),
    )] = None,
    wg: Annotated[Optional[str], Field(
        description="Filter search by working group acronym (e.g. 'httpbis', 'tls').",
    )] = None,
) -> str:
    """Search and retrieve IETF RFCs and Internet-Drafts."""
    if action == "rfc":
        # Accept RFC number, URL, or DOI
        detected = _detect_ietf_url(query)
        if detected and detected["type"] == "rfc":
            return await _fetch_rfc_paper(detected["number"])
        # Try bare number
        num_match = re.match(r'^\s*(?:RFC\s*)?(\d+)\s*$', query, re.IGNORECASE)
        if num_match:
            return await _fetch_rfc_paper(int(num_match.group(1)))
        # Try DOI
        doi_match = _RFC_DOI_RE.match(query.strip())
        if doi_match:
            return await _fetch_rfc_paper(int(doi_match.group(1)))
        return f"Error: Could not parse RFC identifier from: {query}"

    elif action == "search":
        results, total = await _search_rfcs(
            query, limit=limit, offset=offset, status=status, wg=wg,
        )
        if not results:
            return f"No RFCs found for: {query}"

        fm = _build_frontmatter({
            "api": "IETF (Datatracker)",
            "action": "search",
            "query": query,
            "total_results": total,
            "hint": "Use rfc action for full details on any result",
        })
        return fm + "\n\n" + _format_rfc_list(results, total, offset)

    elif action == "draft":
        # Accept draft name or URL
        detected = _detect_ietf_url(query)
        if detected and detected["type"] == "draft":
            return await _fetch_draft(detected["name"])
        # Bare draft name
        name = query.strip()
        if name.startswith("draft-"):
            return await _fetch_draft(name)
        return f"Error: Could not parse Internet-Draft name from: {query}"

    elif action == "subseries":
        result = await _resolve_subseries(query)
        if result:
            return result
        return f"Error: Could not resolve subseries: {query}"

    else:
        return (
            f"Error: Unknown action '{action}'. "
            "Valid actions: rfc, search, draft, subseries"
        )
