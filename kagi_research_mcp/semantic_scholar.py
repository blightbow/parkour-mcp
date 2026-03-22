"""Semantic Scholar API integration for academic paper lookup."""

import asyncio
import logging
import os
import re
import time
from pathlib import Path
from typing import Annotated, Optional

from pydantic import Field

import httpx

from .common import _API_HEADERS
from .markdown import _build_frontmatter

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Rate limiter — 1 request per second to respect S2 upstream policy.
# Uses a lock so concurrent MCP calls (parallel tool use) are serialized
# and the second caller sleeps only for the remaining window.
# ---------------------------------------------------------------------------
_s2_rate_lock = asyncio.Lock()
_s2_last_request: float = 0.0
_S2_MIN_INTERVAL = 1.0  # seconds

S2_BASE_URL = "https://api.semanticscholar.org/graph/v1"
S2_CONFIG_PATH = Path.home() / ".config" / "kagi" / "s2_api_key"

# Matches semanticscholar.org/paper/ URLs, captures 40-char hex paper ID
S2_URL_RE = re.compile(
    r'https?://(?:www\.)?semanticscholar\.org/paper/(?:[^/]+/)?([0-9a-f]{40})',
    re.IGNORECASE,
)

_NO_KEY_MSG = (
    "Rate limited (HTTP 429). Unauthenticated requests share a global pool.\n"
    "To get your own rate limit, set S2_API_KEY env var or create "
    "~/.config/kagi/s2_api_key with a free key from:\n"
    "https://www.semanticscholar.org/product/api#api-key-form"
)

# Field sets for different query types
_SEARCH_FIELDS = (
    "paperId,title,year,authors,citationCount,referenceCount,"
    "publicationTypes,journal,openAccessPdf,tldr"
)
_DETAIL_FIELDS = (
    "paperId,title,year,authors,abstract,venue,citationCount,"
    "influentialCitationCount,referenceCount,publicationTypes,"
    "journal,externalIds,openAccessPdf,tldr,publicationDate"
)
_REFERENCE_FIELDS = (
    "paperId,title,year,authors,citationCount,venue,contexts"
)
_AUTHOR_FIELDS = (
    "authorId,name,affiliations,paperCount,citationCount,hIndex"
)
_AUTHOR_PAPER_FIELDS = (
    "paperId,title,year,citationCount,venue"
)


def _get_s2_api_key() -> str:
    """Load Semantic Scholar API key from env or config file. Returns '' if missing."""
    if key := os.environ.get("S2_API_KEY"):
        return key
    if S2_CONFIG_PATH.exists():
        return S2_CONFIG_PATH.read_text().strip()
    return ""


def _s2_headers() -> dict:
    """Build request headers, adding API key if available."""
    headers = dict(_API_HEADERS)
    key = _get_s2_api_key()
    if key:
        headers["x-api-key"] = key
    return headers


_S2_MAX_RETRIES = 3
_S2_RETRY_BACKOFF = 1.25  # seconds; doubles each retry


async def _s2_request(path: str, params: Optional[dict] = None) -> dict | str:
    """Core HTTP call to Semantic Scholar API.

    Returns parsed JSON dict on success, or an error string on failure.
    Enforces a 1-second minimum interval between requests and retries
    with exponential backoff on HTTP 429.
    """
    global _s2_last_request
    url = f"{S2_BASE_URL}{path}"

    for attempt in range(_S2_MAX_RETRIES + 1):
        async with _s2_rate_lock:
            elapsed = time.monotonic() - _s2_last_request
            if elapsed < _S2_MIN_INTERVAL:
                await asyncio.sleep(_S2_MIN_INTERVAL - elapsed)
            _s2_last_request = time.monotonic()

        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.get(url, headers=_s2_headers(), params=params)
        except httpx.TimeoutException:
            return "Error: Semantic Scholar API request timed out."
        except httpx.RequestError as e:
            return f"Error: Semantic Scholar API request failed - {type(e).__name__}"

        if response.status_code == 200:
            return response.json()
        if response.status_code == 404:
            return "Error: Not found on Semantic Scholar."
        if response.status_code == 429:
            if attempt < _S2_MAX_RETRIES:
                backoff = _S2_RETRY_BACKOFF * (2 ** attempt)
                logger.info("S2 rate limited (429), retry %d after %.1fs", attempt + 1, backoff)
                await asyncio.sleep(backoff)
                continue
            # Exhausted retries
            if _get_s2_api_key():
                return "Error: Rate limited (HTTP 429). Try again shortly."
            return f"Error: {_NO_KEY_MSG}"
        return f"Error: Semantic Scholar API returned HTTP {response.status_code}."

    # Unreachable, but satisfies type checker
    return "Error: Semantic Scholar API request failed."


def _detect_s2_url(url: str) -> Optional[str]:
    """Extract a 40-char hex paper ID from a Semantic Scholar URL, or None."""
    m = S2_URL_RE.search(url)
    return m.group(1) if m else None


def _format_paper_detail(data: dict) -> str:
    """Format a full paper response as markdown."""
    parts = []

    title = data.get("title", "Untitled")
    parts.append(f"# {title}\n")

    # Authors
    authors = data.get("authors") or []
    if authors:
        names = [a.get("name", "Unknown") for a in authors]
        parts.append(f"**Authors:** {', '.join(names)}\n")

    # Year, venue, publication date
    year = data.get("year")
    venue = data.get("venue")
    pub_date = data.get("publicationDate")
    meta_bits = []
    if year:
        meta_bits.append(f"**Year:** {year}")
    if venue:
        meta_bits.append(f"**Venue:** {venue}")
    if pub_date:
        meta_bits.append(f"**Published:** {pub_date}")
    if meta_bits:
        parts.append("  \n".join(meta_bits) + "\n")

    # Citation / reference counts
    cite_count = data.get("citationCount")
    influential_count = data.get("influentialCitationCount")
    ref_count = data.get("referenceCount")
    counts = []
    if cite_count is not None:
        cite_str = f"**Citations:** {cite_count:,}"
        if influential_count is not None:
            cite_str += f" ({influential_count:,} influential)"
        counts.append(cite_str)
    if ref_count is not None:
        counts.append(f"**References:** {ref_count:,}")
    if counts:
        parts.append(" | ".join(counts) + "\n")

    # External IDs (DOI, ArXiv)
    ext_ids = data.get("externalIds") or {}
    id_lines = []
    if doi := ext_ids.get("DOI"):
        id_lines.append(f"**DOI:** [{doi}](https://doi.org/{doi})")
    if arxiv := ext_ids.get("ArXiv"):
        id_lines.append(f"**ArXiv:** [{arxiv}](https://arxiv.org/abs/{arxiv})")
    if pmid := ext_ids.get("PubMed"):
        id_lines.append(f"**PubMed:** {pmid}")
    if id_lines:
        parts.append("  \n".join(id_lines) + "\n")

    # Open access PDF
    oa = data.get("openAccessPdf") or {}
    if pdf_url := oa.get("url"):
        parts.append(f"**Open Access PDF:** [{pdf_url}]({pdf_url})\n")

    # TL;DR
    tldr = data.get("tldr") or {}
    if tldr_text := tldr.get("text"):
        parts.append(f"## TL;DR\n\n{tldr_text}\n")

    # Abstract
    if abstract := data.get("abstract"):
        parts.append(f"## Abstract\n\n{abstract}\n")

    # Publication types
    pub_types = data.get("publicationTypes") or []
    if pub_types:
        parts.append(f"**Publication types:** {', '.join(pub_types)}\n")

    return "\n".join(parts)


def _format_paper_list(papers: list[dict], total: Optional[int] = None, offset: int = 0) -> str:
    """Format a list of papers as a compact numbered list."""
    if not papers:
        return "No papers found."

    lines = []
    for i, paper in enumerate(papers, start=offset + 1):
        title = paper.get("title", "Untitled")
        year = paper.get("year") or "n.d."
        authors = paper.get("authors") or []
        first_author = authors[0].get("name", "Unknown") if authors else "Unknown"
        et_al = " et al." if len(authors) > 1 else ""
        cite_count = paper.get("citationCount")
        cite_str = f" [{cite_count:,} citations]" if cite_count is not None else ""

        paper_id = paper.get("paperId", "")
        venue = paper.get("venue") or ""
        venue_str = f" — {venue}" if venue else ""

        lines.append(f"{i}. **{title}** ({year})")
        lines.append(f"   {first_author}{et_al}{venue_str}{cite_str}")
        if paper_id:
            lines.append(f"   ID: {paper_id}")

        # Citation contexts (for citation/reference results)
        contexts = paper.get("contexts") or []
        if contexts:
            for ctx in contexts[:2]:
                lines.append(f"   > {ctx}")

    if total is not None and total > offset + len(papers):
        lines.append(
            f"\nShowing {offset + 1}-{offset + len(papers)} of {total:,} results. "
            "Use offset/limit to paginate."
        )

    return "\n".join(lines)


def _format_author(data: dict, papers: Optional[list[dict]] = None) -> str:
    """Format author details as markdown."""
    parts = []

    name = data.get("name", "Unknown")
    parts.append(f"# {name}\n")

    affiliations = data.get("affiliations") or []
    if affiliations:
        parts.append(f"**Affiliations:** {', '.join(affiliations)}\n")

    meta = []
    if (pc := data.get("paperCount")) is not None:
        meta.append(f"**Papers:** {pc:,}")
    if (cc := data.get("citationCount")) is not None:
        meta.append(f"**Citations:** {cc:,}")
    if (h := data.get("hIndex")) is not None:
        meta.append(f"**h-index:** {h}")
    if meta:
        parts.append(" | ".join(meta) + "\n")

    author_id = data.get("authorId")
    if author_id:
        parts.append(f"**Author ID:** {author_id}\n")

    if papers:
        parts.append("## Top Papers\n")
        for i, p in enumerate(papers, 1):
            title = p.get("title", "Untitled")
            year = p.get("year") or "n.d."
            cites = p.get("citationCount")
            cite_str = f" [{cites:,} citations]" if cites is not None else ""
            parts.append(f"{i}. **{title}** ({year}){cite_str}")

    return "\n".join(parts)


async def _fetch_s2_paper(paper_id: str) -> str:
    """Fetch a single paper and return formatted markdown with frontmatter."""
    result = await _s2_request(f"/paper/{paper_id}", {"fields": _DETAIL_FIELDS})
    if isinstance(result, str):
        return result

    title = result.get("title", "Untitled")
    s2_id = result.get("paperId", paper_id)
    source_url = f"https://www.semanticscholar.org/paper/{s2_id}"

    fm = _build_frontmatter({
        "title": title,
        "source": source_url,
        "api": "Semantic Scholar",
    })
    return fm + "\n\n" + _format_paper_detail(result)


def _format_snippets(data: dict, paper_id: Optional[str] = None) -> str:
    """Format snippet search results as markdown.

    For single-paper results (paper_id given): group by section.
    For corpus-wide results: group by paper, then section.
    """
    results = data.get("data") or []
    if not results:
        if paper_id:
            return f"No snippet matches found in paper {paper_id}."
        return "No snippet matches found."

    if paper_id:
        # Single-paper: group by section
        sections: dict[str, list[str]] = {}
        for item in results:
            snippet = item.get("snippet", {})
            section = snippet.get("section") or "Untitled Section"
            text = snippet.get("text", "")
            kind = snippet.get("snippetKind", "body")
            if kind != "body":
                text = f"[{kind}] {text}"
            sections.setdefault(section, []).append(text)

        parts = []
        for section, texts in sections.items():
            parts.append(f"### {section}\n")
            for t in texts:
                parts.append(t + "\n")
        return "\n".join(parts)
    else:
        # Corpus-wide: group by paper
        papers: dict[str, dict] = {}  # corpusId -> {title, sections}
        for item in results:
            paper = item.get("paper", {})
            corpus_id = str(paper.get("corpusId", "unknown"))
            title = paper.get("title", "Untitled")
            snippet = item.get("snippet", {})
            section = snippet.get("section") or "Untitled Section"
            text = snippet.get("text", "")
            kind = snippet.get("snippetKind", "body")
            if kind != "body":
                text = f"[{kind}] {text}"

            if corpus_id not in papers:
                papers[corpus_id] = {"title": title, "sections": {}}
            papers[corpus_id]["sections"].setdefault(section, []).append(text)

        parts = []
        for corpus_id, info in papers.items():
            parts.append(f"## {info['title']}\n")
            for section, texts in info["sections"].items():
                parts.append(f"### {section}\n")
                for t in texts:
                    parts.append(t + "\n")
        return "\n".join(parts)


async def semantic_scholar(
    action: Annotated[str, Field(
        description=(
            "The operation to perform. "
            "search: find papers by keywords. "
            "paper: get details by paper ID, DOI:10.xxx, ARXIV:xxx, or S2 URL. "
            "references: list papers cited by a paper. "
            "author_search: find authors by name. "
            "author: get author details and top papers by author ID. "
            "snippets: search within paper body text (~500-word excerpts by section)."
        ),
    )],
    query: Annotated[str, Field(
        description="Search terms (search/snippets), paper ID or DOI/ARXIV/PMID prefix (paper/references), or author ID/name (author/author_search).",
    )],
    limit: Annotated[int, Field(
        description="Maximum results to return (default 10, max 100 for most actions, max 1000 for snippets).",
    )] = 10,
    offset: Annotated[int, Field(
        description="Starting position for pagination.",
    )] = 0,
    fields: Annotated[Optional[str], Field(
        description="Comma-separated S2 API field names to override defaults (advanced, rarely needed).",
    )] = None,
    paper_id: Annotated[Optional[str], Field(
        description="Paper ID to scope snippet search to a single paper. Accepts S2 hash, DOI:10.xxx, ARXIV:xxx, or S2 URL. Only used by snippets action.",
    )] = None,
) -> str:
    """Search and retrieve academic paper data from Semantic Scholar."""
    # Resolve S2 URLs to paper IDs for paper/references/snippets actions
    if action in ("paper", "references", "snippets"):
        detected_id = _detect_s2_url(query)
        if detected_id:
            query = detected_id
        if action == "snippets" and paper_id:
            pid_detected = _detect_s2_url(paper_id)
            if pid_detected:
                paper_id = pid_detected

    if action == "search":
        params = {
            "query": query,
            "fields": fields or _SEARCH_FIELDS,
            "limit": min(limit, 100),
            "offset": offset,
        }
        result = await _s2_request("/paper/search", params)
        if isinstance(result, str):
            return result
        papers = result.get("data") or []
        total = result.get("total")
        if not papers:
            return f"No papers found for: {query}"
        return _format_paper_list(papers, total=total, offset=offset)

    elif action == "paper":
        params = {"fields": fields or _DETAIL_FIELDS}
        result = await _s2_request(f"/paper/{query}", params)
        if isinstance(result, str):
            return result
        return _format_paper_detail(result)

    elif action == "references":
        params = {
            "fields": fields or _REFERENCE_FIELDS,
            "limit": min(limit, 100),
            "offset": offset,
        }
        result = await _s2_request(f"/paper/{query}/references", params)
        if isinstance(result, str):
            return result
        # References endpoint wraps each paper in {"citedPaper": {...}}
        raw = result.get("data") or []
        papers = [item.get("citedPaper", item) for item in raw]
        total = result.get("total")
        if not papers:
            return f"No references found for paper: {query}"
        return _format_paper_list(papers, total=total, offset=offset)

    elif action == "author_search":
        params = {
            "query": query,
            "fields": fields or _AUTHOR_FIELDS,
            "limit": min(limit, 100),
            "offset": offset,
        }
        result = await _s2_request("/author/search", params)
        if isinstance(result, str):
            return result
        authors = result.get("data") or []
        total = result.get("total")
        if not authors:
            return f"No authors found for: {query}"

        lines = []
        for i, a in enumerate(authors, start=offset + 1):
            name = a.get("name", "Unknown")
            author_id = a.get("authorId", "")
            affiliations = a.get("affiliations") or []
            aff_str = f" — {', '.join(affiliations)}" if affiliations else ""
            h = a.get("hIndex")
            h_str = f" [h-index: {h}]" if h is not None else ""
            pc = a.get("paperCount")
            pc_str = f" [{pc:,} papers]" if pc is not None else ""
            lines.append(f"{i}. **{name}**{aff_str}{h_str}{pc_str}")
            if author_id:
                lines.append(f"   ID: {author_id}")

        if total is not None and total > offset + len(authors):
            lines.append(
                f"\nShowing {offset + 1}-{offset + len(authors)} of {total:,} results. "
                "Use offset/limit to paginate."
            )
        return "\n".join(lines)

    elif action == "author":
        params = {"fields": fields or _AUTHOR_FIELDS}
        result = await _s2_request(f"/author/{query}", params)
        if isinstance(result, str):
            return result

        # Also fetch author's papers
        paper_params = {
            "fields": _AUTHOR_PAPER_FIELDS,
            "limit": min(limit, 100),
            "offset": offset,
        }
        papers_result = await _s2_request(f"/author/{query}/papers", paper_params)
        papers = []
        if isinstance(papers_result, dict):
            papers = papers_result.get("data") or []

        return _format_author(result, papers=papers)

    elif action == "snippets":
        # Pre-flight: check text availability when scoped to a single paper
        if paper_id:
            avail_result = await _s2_request(
                f"/paper/{paper_id}", {"fields": "textAvailability"}
            )
            if isinstance(avail_result, str):
                return avail_result
            text_avail = avail_result.get("textAvailability")
            if text_avail != "fulltext":
                title = avail_result.get("title", paper_id)
                return (
                    f"Full text is not available for \"{title}\" "
                    f"(textAvailability: {text_avail}). "
                    "Try the paper action for abstract and TL;DR instead."
                )

        params = {"query": query, "limit": min(limit, 1000)}
        if paper_id:
            params["paperIds"] = paper_id
        result = await _s2_request("/snippet/search", params)
        if isinstance(result, str):
            return result
        return _format_snippets(result, paper_id=paper_id)

    else:
        return (
            f"Error: Unknown action '{action}'. "
            "Valid actions: search, paper, references, author_search, author, snippets"
        )
