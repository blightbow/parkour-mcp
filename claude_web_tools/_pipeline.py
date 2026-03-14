"""Shared content-processing pipelines for fetch tools.

Consolidates MediaWiki fast-path logic and post-fetch section/truncation/frontmatter
assembly that is common to both web_fetch_js and web_fetch_direct.
"""

from typing import Optional

from .markdown import (
    _extract_sections_from_markdown,
    _build_section_list,
    _filter_markdown_by_sections,
    _build_frontmatter,
    _apply_truncation,
)
from .mediawiki import _detect_mediawiki, _fetch_mediawiki_page, _mediawiki_html_to_markdown


def _normalize_sections(section) -> Optional[list[str]]:
    """Normalize section parameter to a list or None."""
    if section is None:
        return None
    return [section] if isinstance(section, str) else list(section)


async def _mediawiki_fast_path(
    url: str,
    section_names: Optional[list[str]],
    max_tokens: int,
    extra_entries: Optional[dict] = None,
) -> Optional[str]:
    """Attempt to fetch a MediaWiki page via the API, bypassing browser/httpx.

    Returns formatted output string on success, or None to signal fallback.
    """
    wiki_info = await _detect_mediawiki(url)
    if not wiki_info:
        return None

    wiki_page = await _fetch_mediawiki_page(
        wiki_info["api_base"],
        wiki_info["page_title"],
        sections=section_names,
    )
    if not wiki_page:
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

    if section_names:
        # Section-specific: the MW API already returned only requested sections,
        # but we still need to extract metadata for frontmatter ancestry paths.
        all_sections = _extract_sections_from_markdown(markdown_content)
        filtered, matched_meta, unmatched = _filter_markdown_by_sections(
            markdown_content, section_names, all_sections
        )
        filtered, truncation_hint = _apply_truncation(
            filtered, max_tokens,
            hint_prefix="Section content",
            hint_suffix="",
        )
        frontmatter_entries["truncated"] = truncation_hint
        fm = _build_frontmatter(
            frontmatter_entries,
            sections_requested=matched_meta,
            sections_not_found=unmatched or None,
        )
        return fm + "\n\n" + filtered
    else:
        all_sections = _extract_sections_from_markdown(markdown_content)
        markdown_content, truncation_hint = _apply_truncation(markdown_content, max_tokens)
        sections_available = None
        if truncation_hint and all_sections:
            sections_available = _build_section_list(all_sections)
        frontmatter_entries["truncated"] = truncation_hint
        fm = _build_frontmatter(
            frontmatter_entries, sections_available=sections_available
        )
        return fm + "\n\n" + markdown_content


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
