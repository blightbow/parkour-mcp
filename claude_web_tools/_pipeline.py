"""Shared content-processing pipelines for fetch tools.

Consolidates MediaWiki fast-path logic and post-fetch section/truncation/frontmatter
assembly that is common to both web_fetch_js and web_fetch_direct.
"""

import logging
from typing import Optional
from urllib.parse import urldefrag

from .markdown import (
    _extract_sections_from_markdown,
    _build_section_list,
    _filter_markdown_by_sections,
    _build_frontmatter,
    _apply_truncation,
)
from .mediawiki import _detect_mediawiki, _fetch_mediawiki_page, _mediawiki_html_to_markdown
from .semantic_scholar import _detect_s2_url, _fetch_s2_paper

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
    )


# ---------------------------------------------------------------------------
# Semantic Scholar fast path
# ---------------------------------------------------------------------------

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
) -> str:
    """Apply section filtering, truncation, and frontmatter to markdown content.

    Common post-processing for both browser-rendered and httpx-fetched HTML.
    Returns the complete formatted output string.
    """
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

    markdown_content, truncation_hint = _apply_truncation(markdown_content, max_tokens)
    if truncation_hint and all_sections and not section_names:
        sections_available = _build_section_list(all_sections)

    frontmatter_entries["truncated"] = truncation_hint
    fm = _build_frontmatter(
        frontmatter_entries,
        sections_requested=sections_requested_meta,
        sections_not_found=sections_not_found,
        sections_available=sections_available,
    )
    return fm + "\n\n" + markdown_content
