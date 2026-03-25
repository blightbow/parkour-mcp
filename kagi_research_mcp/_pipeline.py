"""Shared content-processing pipelines for fetch tools.

Consolidates MediaWiki fast-path logic and post-fetch section/truncation/frontmatter
assembly that is common to both web_fetch_js and web_fetch_direct.
"""

import logging
from typing import Optional
from urllib.parse import urldefrag

import tantivy
from semantic_text_splitter import MarkdownSplitter

from .markdown import (
    _extract_sections_from_markdown,
    _build_section_list,
    _filter_markdown_by_sections,
    _build_frontmatter,
    _apply_semantic_truncation,
    _compute_slice_ancestry,
    _fence_content,
    _TRUST_ADVISORY,
)
from .mediawiki import _detect_mediawiki, _fetch_mediawiki_page, _mediawiki_html_to_markdown
from .semantic_scholar import _detect_s2_url, _fetch_s2_paper
from .arxiv import _detect_arxiv_url, _fetch_arxiv_paper

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Single-entry MediaWiki page cache
# ---------------------------------------------------------------------------
# Avoids redundant API calls for the common workflow:
#   web_fetch_sections(url) → web_fetch_direct(url, section=...) → citation
# Keyed on canonical URL (no fragment). Stores detect + full-page results.
# Invalidates automatically when a different URL is requested.

class _WikiCache:
    """Single-entry cache for the most recently fetched MediaWiki page."""

    __slots__ = ("url", "wiki_info", "wiki_page")

    def __init__(self):
        self.url: Optional[str] = None
        self.wiki_info: Optional[dict] = None
        self.wiki_page: Optional[dict] = None

    def get(self, url: str) -> tuple[Optional[dict], Optional[dict]]:
        """Return (wiki_info, wiki_page) if url matches, else (None, None)."""
        if self.url == url:
            return self.wiki_info, self.wiki_page
        return None, None

    def store(self, url: str, wiki_info: dict, wiki_page: Optional[dict]):
        self.url = url
        self.wiki_info = wiki_info
        self.wiki_page = wiki_page


_wiki_cache = _WikiCache()


# ---------------------------------------------------------------------------
# Single-entry page cache (post-markdown-conversion)
# ---------------------------------------------------------------------------
# Caches the most recently fetched page as pre-sliced content for keyword
# search and index-based retrieval.  Populated by _process_markdown_sections
# (all HTML paths feed through it).  Evicts automatically on a new URL.

class _PageCache:
    """Single-entry cache for sliced page content with BM25 search index."""

    __slots__ = ("url", "title", "markdown", "slices", "slice_ancestry",
                 "_tantivy_index", "renderer")

    _SPLITTER = MarkdownSplitter((1600, 2000))

    # Shared tantivy schema — one text field for content, one for slice index
    _SCHEMA = None

    @classmethod
    def _get_schema(cls):
        if cls._SCHEMA is None:
            builder = tantivy.SchemaBuilder()
            builder.add_text_field("body", stored=True)
            builder.add_unsigned_field("idx", stored=True)
            cls._SCHEMA = builder.build()
        return cls._SCHEMA

    def __init__(self):
        self.url: Optional[str] = None
        self.title: Optional[str] = None
        self.markdown: Optional[str] = None
        self.slices: Optional[list[str]] = None
        self.slice_ancestry: Optional[list[str]] = None
        self._tantivy_index = None
        self.renderer: Optional[str] = None  # "direct" or "js"

    def get(self, url: str, renderer: Optional[str] = None):
        """Return self if url matches, else None.

        When renderer is specified, only returns a hit if the cached entry
        was produced by the same renderer.  This prevents WebFetchJS from
        reusing sparse content that WebFetchDirect cached from a JS-heavy page.
        """
        if self.url == url:
            if renderer is not None and self.renderer != renderer:
                return None
            return self
        return None

    def store(self, url: str, title: str, markdown: str, renderer: Optional[str] = None):
        """Slice markdown, build BM25 index, and cache results."""
        self.url = url
        self.title = title
        self.markdown = markdown
        self.renderer = renderer
        chunks = self._SPLITTER.chunk_indices(markdown)
        self.slices = [text for _, text in chunks]
        offsets = [offset for offset, _ in chunks]
        sections = _extract_sections_from_markdown(markdown)
        self.slice_ancestry = _compute_slice_ancestry(sections, offsets)

        # Build tantivy in-memory search index over slices
        schema = self._get_schema()
        self._tantivy_index = tantivy.Index(schema)
        writer = self._tantivy_index.writer()
        for i, text in enumerate(self.slices):
            writer.add_document(tantivy.Document(body=text, idx=i))
        writer.commit()
        self._tantivy_index.reload()

    def search(self, query_str: str, limit: int = 50) -> list[int]:
        """BM25 search over cached slices. Returns matching slice indices ranked by relevance."""
        if not self._tantivy_index or not self.slices:
            return []
        query = self._tantivy_index.parse_query(query_str, ["body"])
        searcher = self._tantivy_index.searcher()
        results = searcher.search(query, limit=limit)
        return [searcher.doc(addr)["idx"][0] for _score, addr in results.hits]


_page_cache = _PageCache()


async def _cached_mediawiki_fetch(url: str) -> tuple[Optional[dict], Optional[dict]]:
    """Detect and fetch a MediaWiki page, using the single-entry cache.

    Returns (wiki_info, wiki_page) or (None, None) if not a MediaWiki site.
    Always fetches the full page (no section filtering) for cacheability.
    """
    cached_info, cached_page = _wiki_cache.get(url)
    if cached_info is not None:
        logger.debug("wiki cache hit for %s", url)
        return cached_info, cached_page

    wiki_info = await _detect_mediawiki(url)
    if not wiki_info:
        return None, None

    wiki_page = await _fetch_mediawiki_page(
        wiki_info["api_base"],
        wiki_info["page_title"],
    )

    _wiki_cache.store(url, wiki_info, wiki_page)
    return wiki_info, wiki_page


# ---------------------------------------------------------------------------
# URL helpers
# ---------------------------------------------------------------------------

def _extract_fragment(url: str) -> tuple[str, Optional[str]]:
    """Split a URL fragment and return (clean_url, fragment_or_none)."""
    clean, fragment = urldefrag(url)
    return clean, fragment or None


def _normalize_sections(section) -> Optional[list[str]]:
    """Normalize section parameter to a list or None."""
    if section is None:
        return None
    return [section] if isinstance(section, str) else list(section)


def _resolve_fragment_source(
    url: str, fragment: Optional[str], section
) -> tuple[str, Optional[str]]:
    """Compute the citation source URL and any fragment-override warning.

    Returns (source_url, fragment_warning_or_none).
    """
    if fragment and section:
        return url, (
            f"URL fragment #{fragment} was ignored; "
            "explicit section parameter takes precedence"
        )
    if fragment:
        return f"{url}#{fragment}", None
    return url, None


# ---------------------------------------------------------------------------
# MediaWiki fast path
# ---------------------------------------------------------------------------

async def _mediawiki_fast_path(
    url: str,
    section_names: Optional[list[str]],
    max_tokens: int,
    extra_entries: Optional[dict] = None,
    cache_url: Optional[str] = None,
) -> Optional[str]:
    """Attempt to fetch a MediaWiki page via the API, bypassing browser/httpx.

    Returns formatted output string on success, or None to signal fallback.
    Uses the single-entry cache so repeated calls for the same page are free.
    """
    wiki_info, wiki_page = await _cached_mediawiki_fetch(url)
    if not wiki_info or not wiki_page:
        return None

    markdown_content = _mediawiki_html_to_markdown(wiki_page["html"])

    frontmatter_entries = {
        "title": wiki_page["title"],
        "source": url,
        "site": wiki_info["sitename"] or None,
        "generator": wiki_info["generator"] or None,
    }
    if extra_entries:
        frontmatter_entries.update(extra_entries)

    return _process_markdown_sections(
        markdown_content, section_names, max_tokens, frontmatter_entries,
        cache_url=cache_url, renderer="wiki",
    )


# ---------------------------------------------------------------------------
# Semantic Scholar fast path
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# arXiv fast path
# ---------------------------------------------------------------------------

async def _arxiv_fast_path(url: str) -> Optional[str]:
    """Attempt to fetch an arXiv paper via the API.

    Returns formatted paper details on success, or None if the URL is not
    an arXiv /abs/ or /pdf/ link. /html/ URLs are deliberately not matched
    so they fall through to HTTP fetch for full paper text.
    """
    arxiv_id = _detect_arxiv_url(url)
    if not arxiv_id:
        return None

    # Detect if original URL was a PDF link
    is_pdf = "/pdf/" in url

    result = await _fetch_arxiv_paper(arxiv_id, _pdf_url=is_pdf)
    # Always return the result to avoid falling through to HTTP fetch
    return result


async def _s2_fast_path(url: str) -> Optional[str]:
    """Attempt to fetch a Semantic Scholar paper via the API.

    Returns formatted paper details on success, or None to signal fallback.
    """
    paper_id = _detect_s2_url(url)
    if not paper_id:
        return None

    result = await _fetch_s2_paper(paper_id)
    # _fetch_s2_paper always returns a string; if it starts with "Error:"
    # the API call failed — still return it to avoid falling through to
    # an HTTP fetch that would hit CAPTCHA.
    return result


# ---------------------------------------------------------------------------
# Shared post-processing
# ---------------------------------------------------------------------------

def _process_markdown_sections(
    markdown_content: str,
    section_names: Optional[list[str]],
    max_tokens: int,
    frontmatter_entries: dict,
    cache_url: Optional[str] = None,
    renderer: Optional[str] = None,
) -> str:
    """Apply section filtering, truncation, and frontmatter to markdown content.

    Common post-processing for both browser-rendered and httpx-fetched HTML.
    Returns the complete formatted output string.

    When cache_url is provided, populates the page cache with the full
    (pre-filtered) markdown so subsequent search/slices calls can use it.
    The renderer tag ("direct" or "js") is stored with the cache entry so
    that WebFetchJS won't reuse sparse content cached by WebFetchDirect.
    """
    # Populate the page cache before any filtering/truncation
    if cache_url and markdown_content:
        title = frontmatter_entries.get("title", "Untitled")
        _page_cache.store(cache_url, title, markdown_content, renderer=renderer)

    all_sections = _extract_sections_from_markdown(markdown_content)
    sections_requested_meta = None
    sections_available = None

    sections_not_found = None

    if section_names and all_sections:
        markdown_content, sections_requested_meta, unmatched = _filter_markdown_by_sections(
            markdown_content, section_names, all_sections
        )
        sections_not_found = unmatched or None
        # When sections aren't found, show available sections with slugs
        if sections_not_found:
            sections_available = _build_section_list(all_sections, include_slugs=True)
        # Warn when extracted sections have subsections not included in the output
        if sections_requested_meta and any(m.get("has_subsections") for m in sections_requested_meta):
            frontmatter_entries["note"] = (
                "Section extraction returns only the selected heading's direct content. "
                "Subsections are separate entries — request them by name to include them."
            )

    markdown_content, truncation_hint = _apply_semantic_truncation(markdown_content, max_tokens)
    if truncation_hint and all_sections and not section_names:
        sections_available = _build_section_list(all_sections)

    # Move title out of frontmatter — it goes inside the fence
    title = frontmatter_entries.pop("title", None)

    frontmatter_entries["trust"] = _TRUST_ADVISORY
    frontmatter_entries["truncated"] = truncation_hint
    fm = _build_frontmatter(
        frontmatter_entries,
        sections_requested=sections_requested_meta,
        sections_not_found=sections_not_found,
        sections_available=sections_available,
    )
    fenced = _fence_content(markdown_content, title=title)
    return fm + "\n\n" + fenced


# ---------------------------------------------------------------------------
# Slice output, search, and retrieval
# ---------------------------------------------------------------------------

def _slice_output(
    cache: _PageCache,
    indices: list[int],
    max_tokens: int,
    fm_entries: dict,
    search_term: Optional[str] = None,
) -> str:
    """Assemble sliced output with YAML frontmatter and --- dividers.

    Each slice is preceded by a ``--- slice N (Ancestry) ---`` header.
    Respects max_tokens budget — stops emitting slices when exhausted.
    """
    assert cache.slices is not None
    assert cache.slice_ancestry is not None

    # Move title out of frontmatter — it goes inside the fence
    title = fm_entries.pop("title", None)

    fm_entries["trust"] = _TRUST_ADVISORY
    fm_entries["total_slices"] = len(cache.slices)
    if search_term is not None:
        fm_entries["search"] = f'"{search_term}"'
        fm_entries["matched_slices"] = indices
        fm_entries["hint"] = "Use slices= to retrieve adjacent context by index"
    else:
        fm_entries["slices"] = indices
        fm_entries["hint"] = "Use search= for BM25 keyword search, or slices= with adjacent indices for more context"

    fm = _build_frontmatter(fm_entries)

    char_budget = max_tokens * 4
    parts: list[str] = []
    used = 0
    for idx in indices:
        ancestry = cache.slice_ancestry[idx]
        header = f"--- slice {idx} ({ancestry}) ---" if ancestry else f"--- slice {idx} ---"
        content = cache.slices[idx]
        needed = len(header) + 1 + len(content) + 2  # header + \n + content + \n\n
        if used + needed > char_budget and parts:
            break
        parts.append(f"{header}\n{content}")
        used += needed

    fenced = _fence_content("\n\n".join(parts), title=title)
    return fm + "\n\n" + fenced


def _search_slices(
    url: str,
    search: str,
    max_tokens: int,
    fm_entries: dict,
) -> Optional[str]:
    """BM25 search over cached page slices.

    Uses tantivy for language-aware tokenization and BM25 ranking.
    Returns formatted output on cache hit, or None on cache miss.
    """
    cached = _page_cache.get(url)
    if not cached or not cached.slices:
        return None

    matched = cached.search(search)

    if not matched:
        fm_entries["total_slices"] = len(cached.slices)
        fm_entries["search"] = f'"{search}"'
        fm_entries["matched_slices"] = "none"
        fm = _build_frontmatter(fm_entries)
        return fm + "\n\nNo matching slices found."

    return _slice_output(cached, matched, max_tokens, fm_entries, search_term=search)


def _get_slices(
    url: str,
    indices: list[int],
    max_tokens: int,
    fm_entries: dict,
) -> Optional[str]:
    """Retrieve specific slices by index from the page cache.

    Returns formatted output on cache hit, or None on cache miss.
    """
    cached = _page_cache.get(url)
    if not cached or not cached.slices:
        return None

    total = len(cached.slices)
    valid = [i for i in indices if 0 <= i < total]
    invalid = [i for i in indices if i < 0 or i >= total]

    if not valid:
        fm_entries["total_slices"] = total
        fm_entries["slices_not_found"] = invalid
        fm = _build_frontmatter(fm_entries)
        return fm + f"\n\nNo valid slice indices. Total slices: {total} (0-{total - 1})."

    if invalid:
        fm_entries["slices_not_found"] = invalid

    return _slice_output(cached, valid, max_tokens, fm_entries)


def _dispatch_slicing(
    url: str,
    search: Optional[str],
    slices,
    slices_list: list[int],
    max_tokens: int,
    source_url: str,
    warning=None,
) -> str:
    """Dispatch to search or slice retrieval after cache has been populated."""
    cached = _page_cache.get(url)
    if not cached:
        return "Error: Page cache could not be populated for this URL."
    fm_base = {"title": cached.title, "source": source_url, "warning": warning}
    if search is not None:
        return _search_slices(url, search, max_tokens, fm_base) or \
            "Error: Page cache unavailable."
    else:
        return _get_slices(url, slices_list, max_tokens, fm_base) or \
            "Error: Page cache unavailable."
