"""arXiv API integration for academic paper lookup and search."""

import asyncio
import logging
import re
import xml.etree.ElementTree as ET
from typing import Annotated, Optional

from pydantic import Field

import httpx

from .common import _API_USER_AGENT, RateLimiter, tool_name
from .markdown import _build_frontmatter

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# URL detection
# ---------------------------------------------------------------------------
# Matches arxiv.org/{abs,pdf}/<id> and export.arxiv.org variants.
# Excludes /html/ — arXiv's HTML endpoint serves full rendered papers;
# intercepting it would discard full text in favor of metadata-only.
ARXIV_URL_RE = re.compile(
    r'https?://(?:export\.)?arxiv\.org/(?:abs|pdf)/(\d{4}\.\d{4,5}(?:v\d+)?)',
    re.IGNORECASE,
)


def _detect_arxiv_url(url: str) -> Optional[str]:
    """Extract a bare arXiv ID from an arXiv URL, or None.

    Matches /abs/ and /pdf/ paths. Does NOT match /html/ — those should
    fall through to HTTP fetch for full paper text with BM25 slicing.
    """
    m = ARXIV_URL_RE.search(url)
    return m.group(1) if m else None


# Matches /html/ paths (full paper text — not intercepted by the fast path)
_ARXIV_HTML_RE = re.compile(
    r'https?://(?:export\.)?arxiv\.org/html/(\d{4}\.\d{4,5}(?:v\d+)?)',
    re.IGNORECASE,
)


def _detect_arxiv_html_url(url: str) -> Optional[str]:
    """Extract arXiv ID from an /html/ URL, or None."""
    m = _ARXIV_HTML_RE.search(url)
    return m.group(1) if m else None


_VERSION_SUFFIX_RE = re.compile(r'v\d+$')


def _strip_version(arxiv_id: str) -> str:
    """Strip the version suffix from an arXiv ID for DOI synthesis.

    DataCite registers one DOI per paper, always versionless:
    ``10.48550/arXiv.2501.16496`` (not ``v1``).  The Atom API always
    returns versioned IDs (e.g. ``2501.16496v1``), so this helper is
    needed whenever constructing DOIs from API-returned IDs.

    The versioned ID should still be used for arXiv URLs (abs, pdf, html)
    and display — arXiv recommends citing with the specific version.
    """
    return _VERSION_SUFFIX_RE.sub('', arxiv_id)


# ---------------------------------------------------------------------------
# Rate limiter — 3 seconds between requests per arXiv API terms of use.
# ---------------------------------------------------------------------------
_arxiv_limiter = RateLimiter(3.0)


# ---------------------------------------------------------------------------
# HTTP + XML parsing
# ---------------------------------------------------------------------------
ARXIV_API_URL = "https://export.arxiv.org/api/query"

_ATOM_NS = "http://www.w3.org/2005/Atom"
_ARXIV_NS = "http://arxiv.org/schemas/atom"
_ARXIV_HEADERS = {
    "User-Agent": _API_USER_AGENT,
    "Accept": "application/atom+xml",
}

_ARXIV_MAX_RETRIES = 3
_ARXIV_RETRY_BACKOFF = 3.0  # seconds; doubles each retry


def _parse_arxiv_entry(entry_el: ET.Element) -> dict:
    """Extract structured data from a single Atom <entry> element."""
    def _text(tag: str, ns: str = _ATOM_NS) -> Optional[str]:
        el = entry_el.find(f"{{{ns}}}{tag}")
        return el.text.strip() if el is not None and el.text else None

    # ID — extract bare arXiv ID from the full URL
    raw_id = _text("id") or ""
    arxiv_id = raw_id.rsplit("/abs/", 1)[-1] if "/abs/" in raw_id else raw_id

    # Title — normalize whitespace (arXiv API returns multi-line titles)
    title = _text("title") or "Untitled"
    title = " ".join(title.split())

    # Abstract — same whitespace normalization
    abstract = _text("summary") or ""
    abstract = " ".join(abstract.split())

    # Authors with optional affiliations
    authors = []
    for author_el in entry_el.findall(f"{{{_ATOM_NS}}}author"):
        name_el = author_el.find(f"{{{_ATOM_NS}}}name")
        name = name_el.text.strip() if name_el is not None and name_el.text else "Unknown"
        affiliations = []
        for aff_el in author_el.findall(f"{{{_ARXIV_NS}}}affiliation"):
            if aff_el.text:
                affiliations.append(aff_el.text.strip())
        authors.append({"name": name, "affiliations": affiliations})

    # Categories
    categories = []
    primary_category = None
    for cat_el in entry_el.findall(f"{{{_ATOM_NS}}}category"):
        term = cat_el.get("term")
        if term:
            categories.append(term)
    pc_el = entry_el.find(f"{{{_ARXIV_NS}}}primary_category")
    if pc_el is not None:
        primary_category = pc_el.get("term")

    # Dates
    published = _text("published")
    updated = _text("updated")

    # Optional fields
    doi = _text("doi", _ARXIV_NS)
    journal_ref = _text("journal_ref", _ARXIV_NS)
    comment = _text("comment", _ARXIV_NS)

    # Links
    links = []
    for link_el in entry_el.findall(f"{{{_ATOM_NS}}}link"):
        href = link_el.get("href")
        rel = link_el.get("rel", "alternate")
        link_type = link_el.get("type", "")
        link_title = link_el.get("title", "")
        if href:
            links.append({
                "href": href,
                "rel": rel,
                "type": link_type,
                "title": link_title,
            })

    return {
        "id": arxiv_id,
        "title": title,
        "authors": authors,
        "abstract": abstract,
        "categories": categories,
        "primary_category": primary_category,
        "published": published,
        "updated": updated,
        "doi": doi,
        "journal_ref": journal_ref,
        "comment": comment,
        "links": links,
    }


async def _arxiv_request(params: dict) -> list[dict] | str:
    """HTTP GET to arXiv API, rate-limited, with retry on 503.

    Returns a list of parsed entry dicts on success, or an error string.
    """
    for attempt in range(_ARXIV_MAX_RETRIES + 1):
        await _arxiv_limiter.wait()

        try:
            async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
                response = await client.get(
                    ARXIV_API_URL, headers=_ARXIV_HEADERS, params=params,
                )
        except httpx.TimeoutException:
            return "Error: arXiv API request timed out."
        except httpx.RequestError as e:
            return f"Error: arXiv API request failed - {type(e).__name__}"

        if response.status_code == 200:
            break
        if response.status_code == 503:
            if attempt < _ARXIV_MAX_RETRIES:
                backoff = _ARXIV_RETRY_BACKOFF * (2 ** attempt)
                logger.info("arXiv 503, retry %d after %.1fs", attempt + 1, backoff)
                await asyncio.sleep(backoff)
                continue
            return "Error: arXiv API returned HTTP 503 (overloaded). Try again shortly."
        return f"Error: arXiv API returned HTTP {response.status_code}."
    else:
        # Loop completed without break — shouldn't happen, but satisfies type checker
        return "Error: arXiv API request failed."

    # Parse Atom XML
    try:
        root = ET.fromstring(response.text)
    except ET.ParseError as e:
        return f"Error: Failed to parse arXiv API response - {e}"

    entries = root.findall(f"{{{_ATOM_NS}}}entry")
    return [_parse_arxiv_entry(entry) for entry in entries]


# ---------------------------------------------------------------------------
# Formatters
# ---------------------------------------------------------------------------

def _format_arxiv_paper(data: dict, *, html_available: bool = True) -> str:
    """Format a full paper response as markdown."""
    parts = []

    title = data.get("title", "Untitled")
    arxiv_id = data.get("id", "")
    parts.append(f"# {title}\n")

    # Authors with affiliations
    authors = data.get("authors") or []
    if authors:
        author_strs = []
        for a in authors:
            name = a.get("name", "Unknown")
            affs = a.get("affiliations") or []
            if affs:
                name += f" ({', '.join(affs)})"
            author_strs.append(name)
        parts.append(f"**Authors:** {', '.join(author_strs)}\n")

    # Dates and version
    meta_bits = []
    if published := data.get("published"):
        meta_bits.append(f"**Published:** {published}")
    if updated := data.get("updated"):
        if updated != data.get("published"):
            meta_bits.append(f"**Updated:** {updated}")
    if meta_bits:
        parts.append("  \n".join(meta_bits) + "\n")

    # Categories
    primary = data.get("primary_category")
    categories = data.get("categories") or []
    if primary:
        parts.append(f"**Primary category:** {primary}")
    if categories:
        other = [c for c in categories if c != primary]
        if other:
            parts.append(f"**Categories:** {', '.join(other)}")
    if primary or categories:
        parts.append("")

    # DOIs — synthesized arXiv DOI (versionless) + publisher DOI from Atom API
    arxiv_doi = f"10.48550/arXiv.{_strip_version(arxiv_id)}" if arxiv_id else None
    publisher_doi = data.get("doi")  # from <arxiv:doi> — this is the PUBLISHER DOI

    if arxiv_doi:
        parts.append(f"**arXiv DOI:** [{arxiv_doi}](https://doi.org/{arxiv_doi})")
    if publisher_doi and publisher_doi != arxiv_doi:
        parts.append(f"**Publisher DOI:** [{publisher_doi}](https://doi.org/{publisher_doi})")

    # Journal ref, comment
    if journal_ref := data.get("journal_ref"):
        parts.append(f"**Journal ref:** {journal_ref}")
    if comment := data.get("comment"):
        parts.append(f"**Comment:** {comment}")
    if arxiv_doi or publisher_doi or journal_ref or comment:
        parts.append("")

    # Links
    if arxiv_id:
        parts.append(f"**Abstract:** https://arxiv.org/abs/{arxiv_id}")
        parts.append(f"**PDF:** https://arxiv.org/pdf/{arxiv_id}")
        if html_available:
            parts.append(f"**HTML:** https://arxiv.org/html/{arxiv_id}")
        parts.append("")

    # Cross-reference to Semantic Scholar (only when S2 is opted in)
    if arxiv_id:
        from .common import s2_enabled
        if s2_enabled():
            if html_available:
                parts.append(
                    f"*For citation data, use {tool_name('semantic_scholar')} with `ARXIV:{arxiv_id}`*\n"
                )
            else:
                parts.append(
                    f"*For citation data and body text snippets, use {tool_name('semantic_scholar')} with `ARXIV:{arxiv_id}`*\n"
                )

    # Abstract
    if abstract := data.get("abstract"):
        parts.append(f"## Abstract\n\n{abstract}\n")

    return "\n".join(parts)


def _format_arxiv_list(
    papers: list[dict], total: int | None, offset: int,
    include_hint: bool = True,
) -> str:
    """Format a compact numbered list for search results."""
    if not papers:
        return "No papers found."

    lines = []
    for i, paper in enumerate(papers, start=offset + 1):
        title = paper.get("title", "Untitled")
        arxiv_id = paper.get("id", "")
        authors = paper.get("authors") or []
        first_author = authors[0].get("name", "Unknown") if authors else "Unknown"
        et_al = " et al." if len(authors) > 1 else ""
        primary = paper.get("primary_category") or ""
        cat_str = f" [{primary}]" if primary else ""

        lines.append(f"{i}. **{title}**{cat_str}")
        lines.append(f"   {first_author}{et_al}")
        if arxiv_id:
            lines.append(f"   arXiv:{arxiv_id}")

    if include_hint:
        from .common import s2_enabled
        if s2_enabled():
            hint = (
                f"\n*Use `paper` action or {tool_name('semantic_scholar')} with `ARXIV:<id>` "
                "for full details and citation data.*"
            )
        else:
            hint = "\n*Use `paper` action for full details.*"
        lines.append(hint)

    if total is not None and total > offset + len(papers):
        lines.append(
            f"\nShowing {offset + 1}-{offset + len(papers)} of {total:,} results. "
            "Use offset/limit to paginate."
        )

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Fast-path entry point (called from _pipeline.py)
# ---------------------------------------------------------------------------

def _arxiv_see_also(
    arxiv_id: str, html_available: bool, citation_text: Optional[str],
) -> list[str] | str | None:
    """Build see_also hints for an arXiv paper response."""
    from .common import s2_enabled

    hints = []
    if s2_enabled():
        if html_available:
            hints.append(f"ARXIV:{arxiv_id} with {tool_name('semantic_scholar')} for citation counts")
        else:
            hints.append(f"ARXIV:{arxiv_id} with {tool_name('semantic_scholar')} for citation counts and body text snippets")
    if not citation_text:
        hints.append(f"https://doi.org/10.48550/arXiv.{_strip_version(arxiv_id)} for formatted citation")
    if not hints:
        return None
    return hints if len(hints) > 1 else hints[0]


async def _check_html_available(arxiv_id: str) -> bool:
    """Check whether an arXiv HTML render exists for the given paper ID."""
    html_url = f"https://arxiv.org/html/{arxiv_id}"
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            head = await client.head(html_url)
            return head.status_code == 200
    except httpx.RequestError:
        return False


async def _fetch_arxiv_paper(arxiv_id: str, *, _pdf_url: bool = False) -> str:
    """Fetch a single paper by ID and return formatted markdown with frontmatter.

    Args:
        arxiv_id: Bare arXiv ID (e.g. "1706.03762" or "1706.03762v5")
        _pdf_url: If True, the original URL was a /pdf/ link — add a hint.
    """
    from .doi import (
        _alt_dois_from_relations,
        _build_alert_message,
        _build_correction_note,
        _relations_fm_entry,
        fetch_crossref_metadata,
        fetch_formatted_citation,
    )
    from .markdown import _format_retraction_banner

    result = await _arxiv_request({"id_list": arxiv_id})
    if isinstance(result, str):
        return result
    if not result:
        return f"Error: No paper found for arXiv ID: {arxiv_id}"

    paper = result[0]
    clean_id = paper.get("id", arxiv_id)
    # DOI is always versionless; clean_id keeps the version for URLs
    arxiv_doi = f"10.48550/arXiv.{_strip_version(clean_id)}"
    publisher_doi_preflight = paper.get("doi")

    # Concurrent: HTML availability check + DOI citation fetch
    #           + CrossRef REST enrichment (publisher DOI if available,
    #             otherwise arXiv DOI as a fallback — CrossRef does serve
    #             arXiv 10.48550/ entries, though retraction data lives
    #             primarily on journal DOIs).
    enrichment_doi = publisher_doi_preflight or arxiv_doi
    html_result, cite_result, crossref_result = await asyncio.gather(
        _check_html_available(clean_id),
        fetch_formatted_citation(arxiv_doi),
        fetch_crossref_metadata(enrichment_doi),
        return_exceptions=True,
    )
    html_available = html_result if isinstance(html_result, bool) else False
    citation_text = cite_result if isinstance(cite_result, str) else None
    crossref_meta = crossref_result if isinstance(crossref_result, dict) else None
    retraction = (crossref_meta or {}).get("retraction")
    other_update = (crossref_meta or {}).get("other_update")
    relations = (crossref_meta or {}).get("relations") or {}

    html_url = f"https://arxiv.org/html/{clean_id}"
    fm_entries = {
        "title": paper.get("title", "Untitled"),
        "source": f"https://arxiv.org/abs/{clean_id}",
        "api": "arXiv",
        "full_text": (
            f"Use {tool_name('web_fetch_direct')} with {html_url} for full paper text with search/slices"
            if html_available
            else None
        ),
        "warning": (
            None if html_available
            else "HTML full text is not available for this paper; only abstract and metadata are included"
        ),
        "see_also": _arxiv_see_also(clean_id, html_available, citation_text),
    }
    if _pdf_url:
        fm_entries["note"] = (
            "Original URL was a PDF link. Structured metadata returned instead."
            + (f" For readable full text, use {html_url}" if html_available else "")
        )

    # Passive shelf tracking (fire-and-forget)
    # Prefer publisher DOI as primary when available; arXiv DOI becomes alt
    from .shelf import _track_on_shelf, CitationRecord
    published = paper.get("published") or ""
    year = int(published[:4]) if len(published) >= 4 and published[:4].isdigit() else None
    publisher_doi = paper.get("doi")
    if publisher_doi and publisher_doi != arxiv_doi:
        shelf_doi = publisher_doi
        shelf_alt = [arxiv_doi]
    else:
        shelf_doi = arxiv_doi
        shelf_alt = []
    # Merge CrossRef version-linkage DOIs into alt_dois for shelf dedup
    for rd in _alt_dois_from_relations(relations):
        if rd != shelf_doi and rd not in shelf_alt:
            shelf_alt.append(rd)
    shelf_result = await _track_on_shelf(CitationRecord(
        doi=shelf_doi,
        title=paper.get("title", "Untitled"),
        authors=[a.get("name", "Unknown") for a in paper.get("authors") or []],
        year=year,
        alt_dois=shelf_alt,
        source_tool="arxiv",
        retraction=retraction,
    ))
    fm_entries["shelf"] = shelf_result.status_line
    # Retraction / EoC / correction surfacing (fail-open: missing metadata
    # just leaves these fm fields absent via _build_frontmatter's None skip)
    fm_entries["alert"] = _build_alert_message(retraction, other_update)
    fm_note = shelf_result.shelf_note or _build_correction_note(other_update)
    # Don't clobber an existing note (PDF-URL hint): prefer the more urgent
    # retraction/correction note; fall back to whatever was already set.
    if fm_note:
        fm_entries["note"] = fm_note
    fm_entries["relation"] = _relations_fm_entry(relations)

    fm = _build_frontmatter(fm_entries)
    body = _format_arxiv_paper(paper, html_available=html_available)
    if banner := _format_retraction_banner(retraction, other_update):
        body = banner + "\n\n" + body
    if citation_text:
        body += f"\n## Citation\n\n{citation_text}\n"
    return fm + "\n\n" + body


# ---------------------------------------------------------------------------
# Standalone MCP tool
# ---------------------------------------------------------------------------

async def arxiv(
    action: Annotated[str, Field(
        description=(
            "The operation to perform. "
            "search: find papers using arXiv query syntax. "
            "paper: get details by arXiv ID or URL. "
            "category: browse recent papers in an arXiv category."
        ),
    )],
    query: Annotated[str, Field(
        description=(
            "For search: arXiv query syntax with field prefixes and boolean operators. "
            "Field prefixes: ti: (title), au: (author), abs: (abstract), cat: (category), "
            "all: (all fields), co: (comment), jr: (journal ref), rn: (report number). "
            "Boolean operators: AND, OR, ANDNOT. "
            'Example: "ti:attention AND cat:cs.CL". '
            "For paper: arXiv ID (e.g. 1706.03762) or arXiv URL. "
            "For category: arXiv category (e.g. cs.AI, math.CO, hep-th)."
        ),
    )],
    limit: Annotated[int, Field(
        description="Maximum results to return (default 10, max 100).",
    )] = 10,
    offset: Annotated[int, Field(
        description="Starting position for pagination.",
    )] = 0,
    sort_by: Annotated[Optional[str], Field(
        description="Sort field: relevance, lastUpdatedDate, or submittedDate (default: relevance for search, submittedDate for category).",
    )] = None,
    sort_order: Annotated[Optional[str], Field(
        description="Sort direction: ascending or descending (default: descending).",
    )] = None,
) -> str:
    """Search and retrieve academic paper data from arXiv."""
    if action == "search":
        params = {
            "search_query": query,
            "start": offset,
            "max_results": min(limit, 100),
        }
        if sort_by:
            params["sortBy"] = sort_by
        if sort_order:
            params["sortOrder"] = sort_order

        result = await _arxiv_request(params)
        if isinstance(result, str):
            return result
        if not result:
            return f"No papers found for: {query}"

        from .common import s2_enabled as _s2_on
        _search_hint = (
            f"Use paper action for full details, or {tool_name('semantic_scholar')} with ARXIV:<id> for citation data"
            if _s2_on() else "Use paper action for full details"
        )
        fm = _build_frontmatter({
            "api": "arXiv",
            "action": "search",
            "query": query,
            "hint": _search_hint,
        })
        return fm + "\n\n" + _format_arxiv_list(result, total=None, offset=offset, include_hint=False)

    elif action == "paper":
        # Accept arXiv URLs — auto-detect and extract ID
        detected = _detect_arxiv_url(query)
        arxiv_id = detected if detected else query
        return await _fetch_arxiv_paper(arxiv_id)

    elif action == "category":
        params = {
            "search_query": f"cat:{query}",
            "start": offset,
            "max_results": min(limit, 100),
            "sortBy": sort_by or "submittedDate",
            "sortOrder": sort_order or "descending",
        }

        result = await _arxiv_request(params)
        if isinstance(result, str):
            return result
        if not result:
            return f"No papers found in category: {query}"

        from .common import s2_enabled as _s2_cat
        _cat_hint = (
            f"Use paper action for full details, or {tool_name('semantic_scholar')} with ARXIV:<id> for citation data"
            if _s2_cat() else "Use paper action for full details"
        )
        fm = _build_frontmatter({
            "api": "arXiv",
            "action": "category",
            "category": query,
            "hint": _cat_hint,
        })
        return fm + "\n\n" + _format_arxiv_list(result, total=None, offset=offset, include_hint=False)

    else:
        return (
            f"Error: Unknown action '{action}'. "
            "Valid actions: search, paper, category"
        )
