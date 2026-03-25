"""DOI content negotiation and resolution helpers.

Provides async functions for fetching citation metadata via DOI content
negotiation (doi.org) and the DataCite REST API.  These are used for:
  - Passive enrichment of arXiv and Semantic Scholar paper responses
  - The DOI URL fast-path handler (P5)
  - DataCite metadata enrichment (P6)
"""

import asyncio
import logging
import re
import time
from typing import Optional

import httpx

from .common import _API_USER_AGENT
from .markdown import _build_frontmatter, _fence_content, _TRUST_ADVISORY

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Rate limiter — shared across all doi.org content negotiation calls.
# CrossRef polite pool: 10 req/s with mailto, 5 req/s without.
# DataCite via doi.org: 1,000/5min (~3.3/s).
# Conservative default: 5/sec (0.2s interval).
# ---------------------------------------------------------------------------
_doi_rate_lock = asyncio.Lock()
_doi_last_request: float = 0.0
_DOI_MIN_INTERVAL = 0.2  # seconds between doi.org requests


# ---------------------------------------------------------------------------
# URL detection
# ---------------------------------------------------------------------------
DOI_URL_RE = re.compile(
    r'https?://(?:dx\.)?doi\.org/(10\.\S+)',
    re.IGNORECASE,
)

ARXIV_DOI_RE = re.compile(
    r'^10\.48550/arXiv\.(.+)$',
    re.IGNORECASE,
)


def _detect_doi_url(url: str) -> Optional[str]:
    """Extract a bare DOI from a doi.org URL, or None."""
    m = DOI_URL_RE.search(url)
    return m.group(1) if m else None


# ---------------------------------------------------------------------------
# Registration Agency detection (cached per-prefix)
# ---------------------------------------------------------------------------
_ra_cache: dict[str, str] = {}


async def _detect_ra(doi: str, *, timeout: float = 5.0) -> Optional[str]:
    """Detect the Registration Agency for a DOI prefix.

    Uses doi.org/doiRA/{prefix} API with in-memory caching.
    Returns "DataCite", "Crossref", etc., or None on failure.
    """
    prefix = doi.split("/")[0]  # "10.48550"
    if prefix in _ra_cache:
        return _ra_cache[prefix]

    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.get(
                f"https://doi.org/doiRA/{prefix}",
                headers={"User-Agent": _API_USER_AGENT},
            )
            if resp.status_code == 200:
                data = resp.json()
                if isinstance(data, list) and data:
                    ra = data[0].get("RA", "")
                    _ra_cache[prefix] = ra
                    return ra
    except Exception as e:
        logger.debug("RA detection failed for %s: %s", prefix, e)
    return None


# ---------------------------------------------------------------------------
# DataCite REST API
# ---------------------------------------------------------------------------
_datacite_rate_lock = asyncio.Lock()
_datacite_last_request: float = 0.0
_DATACITE_MIN_INTERVAL = 0.1  # 10 req/s


async def _datacite_rate_wait() -> None:
    """Enforce minimum interval between DataCite API requests."""
    global _datacite_last_request
    async with _datacite_rate_lock:
        elapsed = time.monotonic() - _datacite_last_request
        if elapsed < _DATACITE_MIN_INTERVAL:
            await asyncio.sleep(_DATACITE_MIN_INTERVAL - elapsed)
        _datacite_last_request = time.monotonic()


async def fetch_datacite_metadata(
    doi: str, *, timeout: float = 5.0,
) -> Optional[dict]:
    """Fetch enriched metadata from DataCite REST API.

    Returns a simplified dict with ORCIDs, affiliations, SPDX license,
    and related identifiers — or None on failure.
    """
    await _datacite_rate_wait()
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.get(
                f"https://api.datacite.org/dois/{doi}",
                headers={"User-Agent": _API_USER_AGENT, "Accept": "application/json"},
            )
            if resp.status_code != 200:
                return None
            raw = resp.json()
            attrs = raw.get("data", {}).get("attributes", {})

            # Extract ORCIDs from creators
            orcids: dict[str, str] = {}
            creators = attrs.get("creators") or []
            for c in creators:
                name = c.get("name", "")
                for ni in c.get("nameIdentifiers") or []:
                    if ni.get("nameIdentifierScheme") == "ORCID":
                        orcid_url = ni.get("nameIdentifier", "")
                        # Normalize: may be full URL or bare ID
                        orcid_id = orcid_url.replace("https://orcid.org/", "")
                        if orcid_id:
                            orcids[name] = orcid_id

            # SPDX license
            rights_list = attrs.get("rightsList") or []
            license_id = None
            license_url = None
            for r in rights_list:
                if r.get("rightsIdentifierScheme") == "SPDX":
                    license_id = r.get("rightsIdentifier")
                    license_url = r.get("rightsUri")
                    break
                # Fallback: any rights entry with a URI
                if not license_id and r.get("rightsUri"):
                    license_id = r.get("rights")
                    license_url = r.get("rightsUri")

            # Related identifiers
            related = []
            for ri in (attrs.get("relatedIdentifiers") or [])[:10]:
                related.append({
                    "type": ri.get("relatedIdentifierType"),
                    "relation": ri.get("relationType"),
                    "id": ri.get("relatedIdentifier"),
                })

            return {
                "orcids": orcids,
                "license_id": license_id,
                "license_url": license_url,
                "related": related,
                "resource_type": attrs.get("types", {}).get("resourceTypeGeneral"),
            }
    except Exception as e:
        logger.debug("DataCite fetch failed for %s: %s", doi, e)
        return None


# ---------------------------------------------------------------------------
# Content negotiation
# ---------------------------------------------------------------------------

async def _doi_rate_wait() -> None:
    """Enforce minimum interval between doi.org requests."""
    global _doi_last_request
    async with _doi_rate_lock:
        elapsed = time.monotonic() - _doi_last_request
        if elapsed < _DOI_MIN_INTERVAL:
            await asyncio.sleep(_DOI_MIN_INTERVAL - elapsed)
        _doi_last_request = time.monotonic()


async def fetch_formatted_citation(
    doi: str, *, style: str = "apa", timeout: float = 5.0,
) -> Optional[str]:
    """Fetch a pre-formatted citation string via DOI content negotiation.

    Uses the ``text/x-bibliography`` content type with the specified CSL
    style (default APA).  The doi.org server runs citeproc and returns a
    ready-to-paste citation string.

    Returns the citation string, or None on any failure.
    Designed for concurrent use with asyncio.gather — never raises.
    """
    await _doi_rate_wait()
    try:
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
            resp = await client.get(
                f"https://doi.org/{doi}",
                headers={
                    "User-Agent": _API_USER_AGENT,
                    "Accept": f"text/x-bibliography; style={style}",
                },
            )
            if resp.status_code == 200:
                text = resp.text.strip()
                return text if text else None
            logger.debug("DOI citation fetch HTTP %d for %s", resp.status_code, doi)
            return None
    except Exception as e:
        logger.debug("DOI citation fetch failed for %s: %s", doi, e)
        return None


async def fetch_csl_json(
    doi: str, *, timeout: float = 5.0,
) -> Optional[dict]:
    """Fetch structured CSL-JSON metadata via DOI content negotiation.

    Returns parsed JSON dict with fields like ``author``, ``title``,
    ``DOI``, ``issued``, ``publisher``, ``type``, ``abstract``, etc.

    Returns None on any failure.  Designed for concurrent use — never raises.
    """
    await _doi_rate_wait()
    try:
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
            resp = await client.get(
                f"https://doi.org/{doi}",
                headers={
                    "User-Agent": _API_USER_AGENT,
                    "Accept": "application/vnd.citationstyles.csl+json",
                },
            )
            if resp.status_code == 200:
                return resp.json()
            logger.debug("DOI CSL-JSON fetch HTTP %d for %s", resp.status_code, doi)
            return None
    except Exception as e:
        logger.debug("DOI CSL-JSON fetch failed for %s: %s", doi, e)
        return None


# ---------------------------------------------------------------------------
# CSL-JSON → markdown formatting
# ---------------------------------------------------------------------------

def _format_csl_author(author: dict) -> str:
    """Format a single CSL-JSON author entry."""
    if literal := author.get("literal"):
        return literal
    family = author.get("family", "")
    given = author.get("given", "")
    if family and given:
        return f"{family}, {given}"
    return family or given or "Unknown"


def _format_csl_date(issued: dict) -> Optional[str]:
    """Format a CSL-JSON date-parts or literal date."""
    if literal := issued.get("literal"):
        return literal
    parts = issued.get("date-parts")
    if parts and parts[0]:
        dp = parts[0]
        if len(dp) >= 3:
            return f"{dp[0]}-{dp[1]:02d}-{dp[2]:02d}"
        elif len(dp) >= 2:
            return f"{dp[0]}-{dp[1]:02d}"
        else:
            return str(dp[0])
    return None


def _format_csl_json_as_markdown(data: dict, *, datacite: Optional[dict] = None) -> str:
    """Format CSL-JSON metadata into readable markdown.

    When datacite enrichment dict is provided, merges ORCIDs into author
    display and adds SPDX license info.
    """
    parts = []
    dc_orcids = (datacite or {}).get("orcids") or {}

    title = data.get("title", "Untitled")
    parts.append(f"# {title}\n")

    # Authors (with ORCIDs from DataCite when available)
    authors = data.get("author") or []
    if authors:
        author_strs = []
        for a in authors[:10]:
            name = _format_csl_author(a)
            # Match ORCID by "Last, First" name
            orcid = dc_orcids.get(name)
            if not orcid:
                # DataCite uses "Last, First" format — also try CSL family name
                family = a.get("family", "")
                given = a.get("given", "")
                if family and given:
                    orcid = dc_orcids.get(f"{family}, {given}")
            if orcid:
                name += f" [ORCID](https://orcid.org/{orcid})"
            author_strs.append(name)
        if len(authors) > 10:
            author_strs.append(f"... and {len(authors) - 10} more")
        parts.append(f"**Authors:** {', '.join(author_strs)}\n")

    # Date, publisher, type
    meta_bits = []
    if issued := data.get("issued"):
        if date_str := _format_csl_date(issued):
            meta_bits.append(f"**Published:** {date_str}")
    if publisher := data.get("publisher"):
        meta_bits.append(f"**Publisher:** {publisher}")
    if container := data.get("container-title"):
        meta_bits.append(f"**Journal:** {container}")
    csl_type = data.get("type")
    # Prefer DataCite's resource type (more specific than CSL-JSON mapping)
    dc_type = (datacite or {}).get("resource_type")
    display_type = dc_type or csl_type
    if display_type:
        meta_bits.append(f"**Type:** {display_type}")
    if meta_bits:
        parts.append("  \n".join(meta_bits) + "\n")

    # DOI link
    if doi := data.get("DOI"):
        parts.append(f"**DOI:** [{doi}](https://doi.org/{doi})\n")

    # License — prefer SPDX from DataCite, fall back to CSL-JSON copyright
    dc_license_id = (datacite or {}).get("license_id")
    dc_license_url = (datacite or {}).get("license_url")
    if dc_license_id and dc_license_url:
        parts.append(f"**License:** [{dc_license_id}]({dc_license_url})\n")
    elif dc_license_id:
        parts.append(f"**License:** {dc_license_id}\n")
    elif copyright_text := data.get("copyright"):
        parts.append(f"**License:** {copyright_text}\n")

    # Abstract
    if abstract := data.get("abstract"):
        clean = re.sub(r'<[^>]+>', '', abstract).strip()
        if clean:
            parts.append(f"## Abstract\n\n{clean}\n")

    return "\n".join(parts)


# ---------------------------------------------------------------------------
# DOI fast-path handler
# ---------------------------------------------------------------------------

async def _fetch_doi_paper(doi: str) -> str:
    """Fetch DOI metadata via content negotiation and return formatted markdown.

    For arXiv DOIs (10.48550/arXiv.*), delegates to the arXiv handler.
    For all other DOIs, fetches CSL-JSON and APA citation concurrently.
    """
    # Delegate arXiv DOIs to the arXiv handler
    arxiv_match = ARXIV_DOI_RE.match(doi)
    if arxiv_match:
        from .arxiv import _fetch_arxiv_paper
        arxiv_id = arxiv_match.group(1)
        return await _fetch_arxiv_paper(arxiv_id)

    # Concurrent: CSL-JSON metadata + formatted APA citation
    csl_result, cite_result = await asyncio.gather(
        fetch_csl_json(doi),
        fetch_formatted_citation(doi),
        return_exceptions=True,
    )
    csl_data = csl_result if isinstance(csl_result, dict) else None
    citation_text = cite_result if isinstance(cite_result, str) else None

    if not csl_data and not citation_text:
        return f"Error: Could not resolve DOI: {doi}. No metadata returned from doi.org."

    # DataCite enrichment: ORCIDs, SPDX license, related identifiers
    datacite = None
    ra = await _detect_ra(doi)
    if ra == "DataCite":
        datacite = await fetch_datacite_metadata(doi)

    # Format body from CSL-JSON, or minimal fallback
    if csl_data:
        body = _format_csl_json_as_markdown(csl_data, datacite=datacite)
        title = csl_data.get("title", "Untitled")
    else:
        title = "Untitled"
        body = f"# {doi}\n"

    if citation_text:
        body += f"\n## Citation\n\n{citation_text}\n"

    # Passive shelf tracking
    fm_shelf = None
    try:
        from .shelf import _get_shelf, CitationRecord
        shelf = _get_shelf()
        authors = [_format_csl_author(a) for a in (csl_data or {}).get("author", [])]
        year = None
        if issued := (csl_data or {}).get("issued"):
            parts = issued.get("date-parts")
            if parts and parts[0]:
                year = parts[0][0]
        orcids = datacite.get("orcids") if datacite else None
        shelf.track(CitationRecord(
            doi=doi,
            title=title,
            authors=authors,
            year=year,
            venue=(csl_data or {}).get("container-title"),
            source_tool="doi",
            citation_apa=citation_text,
            orcids=orcids,
        ))
        fm_shelf = shelf.status_line()
    except Exception:
        logger.debug("Shelf tracking failed for DOI %s", doi, exc_info=True)

    fm = _build_frontmatter({
        "title": title,
        "source": f"https://doi.org/{doi}",
        "api": "DOI",
        "trust": _TRUST_ADVISORY,
        "see_also": f"DOI:{doi} with SemanticScholar for citation counts and references",
        "shelf": fm_shelf,
    })

    fenced = _fence_content(body, title=None)  # title already in body as H1
    return fm + "\n\n" + fenced
