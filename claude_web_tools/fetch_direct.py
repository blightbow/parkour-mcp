"""Direct HTTP content fetching without JavaScript rendering."""

import logging
from typing import Optional, Union

import httpx

from .common import _FETCH_HEADERS
from .markdown import (
    html_to_markdown, _extract_sections_from_markdown, _build_section_list,
    _filter_markdown_by_sections, _build_frontmatter, _apply_truncation,
)
from ._pipeline import (
    _extract_fragment, _normalize_sections, _resolve_fragment_source,
    _mediawiki_fast_path, _process_markdown_sections,
    _cached_mediawiki_fetch,
)
from .mediawiki import _mediawiki_html_to_markdown, _extract_citations, _format_citations

logger = logging.getLogger(__name__)


async def web_fetch_direct(
    url: str,
    max_tokens: int = 5000,
    section: Optional[Union[str, list[str]]] = None,
    citation: Optional[Union[int, list[int]]] = None,
) -> str:
    """Fetch raw content from a URL without JavaScript rendering.

    Returns markdown with YAML frontmatter. Supports HTML, plain text, JSON,
    and XML content types. For HTML pages, use the section parameter to extract
    specific sections by heading name.

    For MediaWiki pages (Wikipedia, etc.), use the citation parameter to
    retrieve specific numbered references (e.g. citation=4 or citation=[1,3,8]).

    Args:
        url: The URL to fetch
        max_tokens: Limit on content length in approximate token count (default 5000)
        section: Section name or list of section names to extract from the page
        citation: Citation number or list of numbers to retrieve from the page
    """
    # Extract fragment from URL (e.g. #section-name) as implicit section request
    url, fragment = _extract_fragment(url)
    section_names = _normalize_sections(section)
    if fragment and not section_names:
        section_names = [fragment]
    source_url, fragment_warning = _resolve_fragment_source(url, fragment, section)

    # --- Citation-only path (MediaWiki pages) ---
    if citation is not None:
        requested = [citation] if isinstance(citation, int) else list(citation)
        try:
            wiki_info, wiki_page = await _cached_mediawiki_fetch(url)
            if wiki_info and wiki_page:
                all_citations = _extract_citations(wiki_page["html"])
                if not all_citations:
                    return f"Error: No citations found for {url}"
                # Filter to requested citation numbers
                selected = [c for c in all_citations if c["n"] in requested]
                not_found = sorted(set(requested) - {c["n"] for c in selected})
                fm_entries = {
                    "title": wiki_page["title"],
                    "source": source_url,
                    "cite_only": True,
                }
                if not_found:
                    available = sorted(c["n"] for c in all_citations)
                    # Show a compact range hint
                    fm_entries["citations_not_found"] = not_found
                    fm_entries["citations_available"] = f"1-{available[-1]}"
                fm = _build_frontmatter(fm_entries)
                if selected:
                    return fm + "\n\n" + _format_citations(selected)
                return fm
        except Exception:
            pass
        return f"Error: Citation retrieval requires a MediaWiki page (Wikipedia, etc.)"

    # --- MediaWiki fast path (before HTTP fetch) ---
    try:
        result = await _mediawiki_fast_path(
            url, section_names, max_tokens,
            extra_entries={"source": source_url, "warning": fragment_warning},
        )
        if result is not None:
            return result
    except Exception:
        pass  # Fall through to HTTP fetch

    # --- HTTP fetch ---
    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=30.0) as client:
            response = await client.get(url, headers=_FETCH_HEADERS)
            response.raise_for_status()
    except httpx.TimeoutException:
        return f"Error: Request timed out for {url}"
    except httpx.HTTPStatusError as e:
        return f"Error: HTTP {e.response.status_code} for {url}"
    except httpx.RequestError as e:
        return f"Error: Failed to fetch {url} - {type(e).__name__}"

    # Check content type
    content_type = response.headers.get("content-type", "")
    is_html = "text/html" in content_type or "application/xhtml" in content_type
    is_plain = "text/plain" in content_type
    is_json = "application/json" in content_type or "text/json" in content_type
    is_xml = (
        "application/xml" in content_type or "text/xml" in content_type
    ) and not is_html

    if not any([is_html, is_plain, is_json, is_xml]):
        return (
            f"Error: Unsupported content type '{content_type}'. "
            f"Supported: text/html, text/plain, application/json, application/xml."
        )

    # --- Markdown output ---
    if is_plain or is_json or is_xml:
        # Non-HTML content: YAML frontmatter + raw body
        text = response.text.strip()
        if not text:
            return f"Error: No content extracted from {url}"

        title = url.rsplit("/", 1)[-1] or "Untitled"
        ct_label = "json" if is_json else ("xml" if is_xml else "plain text")

        text, truncation_hint = _apply_truncation(
            text, max_tokens,
            hint_prefix="Full content",
            hint_suffix="Use max_tokens to adjust.",
        )

        fm = _build_frontmatter({
            "title": title,
            "source": source_url,
            "warning": fragment_warning,
            "content_type": ct_label,
            "truncated": truncation_hint,
        })
        return fm + "\n\n" + text

    # HTML content: parse → markdown
    title, markdown_content = html_to_markdown(response.text)

    if not markdown_content:
        return f"Error: No content extracted from {url}"

    output = _process_markdown_sections(
        markdown_content, section_names, max_tokens,
        {"title": title, "source": source_url, "warning": fragment_warning},
    )
    return output


async def web_fetch_sections(url: str) -> str:
    """List the section headings of a web page.

    Returns a section tree with heading names and anchor slugs.
    If the URL contains a fragment, resolves it against the tree.

    Args:
        url: The URL to inspect (fragments are resolved, not stripped)
    """
    original_url = url
    url, fragment = _extract_fragment(url)
    section_names = [fragment] if fragment else None

    # --- MediaWiki fast path (uses single-entry cache) ---
    try:
        wiki_info, wiki_page = await _cached_mediawiki_fetch(url)
        if wiki_info and wiki_page:
            markdown_content = _mediawiki_html_to_markdown(wiki_page["html"])
            return _sections_response(
                wiki_page["title"], original_url, markdown_content, section_names,
            )
    except Exception:
        pass

    # --- HTTP fetch ---
    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=30.0) as client:
            response = await client.get(url, headers=_FETCH_HEADERS)
            response.raise_for_status()
    except httpx.TimeoutException:
        return f"Error: Request timed out for {url}"
    except httpx.HTTPStatusError as e:
        return f"Error: HTTP {e.response.status_code} for {url}"
    except httpx.RequestError as e:
        return f"Error: Failed to fetch {url} - {type(e).__name__}"

    content_type = response.headers.get("content-type", "")
    is_html = "text/html" in content_type or "application/xhtml" in content_type

    if not is_html:
        return f"Error: Section listing requires HTML content (got '{content_type}')."

    title, markdown_content = html_to_markdown(response.text)

    if not markdown_content:
        return f"Error: No content extracted from {url}"

    return _sections_response(title, original_url, markdown_content, section_names)


def _sections_response(
    title: str,
    url: str,
    markdown_content: str,
    section_names: Optional[list[str]],
) -> str:
    """Build a sections-only response from markdown content."""
    all_sections = _extract_sections_from_markdown(markdown_content)

    if not all_sections:
        fm = _build_frontmatter({"title": title, "source": url})
        return fm + "\n\nNo sections found."

    entries = {"title": title, "source": url}
    sections_available = _build_section_list(all_sections, include_slugs=True)
    sections_not_found = None

    if section_names:
        _, matched_meta, unmatched = _filter_markdown_by_sections(
            markdown_content, section_names, all_sections,
        )
        sections_not_found = unmatched or None
        # Surface match info in entries so it doesn't suppress the tree
        if matched_meta:
            m = matched_meta[0]
            entries["section"] = m["name"]
            if m.get("matched_fragment"):
                entries["matched_fragment"] = f'"#{m["matched_fragment"]}"'

    fm = _build_frontmatter(
        entries,
        sections_not_found=sections_not_found,
        sections_available=sections_available,
    )
    return fm
