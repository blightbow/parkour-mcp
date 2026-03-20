"""Direct HTTP content fetching without JavaScript rendering."""

import logging
import re
from typing import Optional, Union
from xml.sax.saxutils import escape as xml_escape

import httpx
from bs4 import BeautifulSoup

from .common import _FETCH_HEADERS
from .markdown import (
    html_to_markdown, _extract_sections_from_markdown, _build_section_list,
    _filter_markdown_by_sections, _build_frontmatter, _apply_truncation,
)
from ._pipeline import (
    _extract_fragment, _normalize_sections, _mediawiki_fast_path, _process_markdown_sections,
)

logger = logging.getLogger(__name__)


def _extract_text_spans(soup: BeautifulSoup) -> list[str]:
    """Extract text content from HTML, split into spans by block elements."""
    # Remove script and style elements
    for tag in soup(["script", "style", "nav", "header", "footer", "aside"]):
        tag.decompose()

    # Find main content area if possible
    main = soup.find("main") or soup.find("article") or soup.find("body") or soup

    spans = []
    # Block elements that should create span boundaries
    block_tags = {"p", "div", "section", "article", "h1", "h2", "h3", "h4", "h5", "h6",
                  "li", "td", "th", "blockquote", "pre", "figcaption"}

    for element in main.find_all(block_tags):
        text = element.get_text(separator=" ", strip=True)
        # Clean up whitespace
        text = re.sub(r"\s+", " ", text).strip()
        if text and len(text) > 20:  # Skip very short fragments
            spans.append(text)

    # Deduplicate while preserving order (nested elements can cause duplicates)
    seen = set()
    unique_spans = []
    for span in spans:
        if span not in seen:
            seen.add(span)
            unique_spans.append(span)

    return unique_spans


def _build_document_xml(
    title: str,
    spans: list[str],
    url: str,
    mime_type: str,
    doc_index: int = 1
) -> str:
    """Build XML document structure matching Claude Desktop's format."""
    lines = [f'<document index="{doc_index}">']
    lines.append(f"  <source>{xml_escape(title)}</source>")
    lines.append("  <document_content>")

    for i, span_text in enumerate(spans, 1):
        span_index = f"{doc_index}-{i}"
        lines.append(f'    <span index="{span_index}">{xml_escape(span_text)}</span>')

    lines.append("  </document_content>")
    lines.append('  <metadata key="content_type">html</metadata>')
    lines.append(f'  <metadata key="destination_url">{xml_escape(url)}</metadata>')
    lines.append(f'  <metadata key="mime_type">{xml_escape(mime_type)}</metadata>')
    lines.append("</document>")

    return "\n".join(lines)


async def web_fetch_direct(
    url: str,
    max_tokens: int = 5000,
    cite: bool = False,
    section: Optional[Union[str, list[str]]] = None,
) -> str:
    """Fetch raw content from a URL without JavaScript rendering.

    Returns markdown by default. Use cite=True for XML document format with
    span indices for citation (legacy behavior, no section support).

    Supports HTML, plain text, JSON, and XML content types. For HTML pages,
    use the section parameter to extract specific sections by heading name.

    Args:
        url: The URL to fetch
        max_tokens: Limit on content length in approximate token count (default 5000)
        cite: If True, return XML format with span indices for citation (default False)
        section: Section name or list of section names to extract from the page
    """
    # Extract fragment from URL (e.g. #section-name) as implicit section request
    url, fragment = _extract_fragment(url)
    section_names = _normalize_sections(section)
    if fragment and not section_names:
        section_names = [fragment]
    # Preserve fragment in source URL only when it drove the section resolution
    source_url = f"{url}#{fragment}" if fragment and not section else url
    # Warn when explicit section= overrides a URL fragment
    fragment_warning = (
        f"URL fragment #{fragment} was ignored; explicit section parameter takes precedence"
        if fragment and section else None
    )

    # --- MediaWiki fast path (before HTTP fetch) ---
    if not cite:
        try:
            result = await _mediawiki_fast_path(url, section_names, max_tokens,
                                                source_url=source_url,
                                                extra_entries={"warning": fragment_warning})
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

    # --- cite=True path: XML format with span indices (legacy) ---
    if cite:
        if is_plain or is_json or is_xml:
            text = response.text.strip()
            if not text:
                return f"Error: No content extracted from {url}"
            title = url.rsplit("/", 1)[-1] or "Untitled"
            if is_plain:
                spans = [p.strip() for p in re.split(r'\n\s*\n', text) if p.strip()]
            else:
                spans = [text]
        else:
            try:
                soup = BeautifulSoup(response.text, "html.parser")
            except Exception as e:
                return f"Error: Failed to parse HTML - {e}"

            title_tag = soup.find("h1") or soup.find("title")
            title = title_tag.get_text(strip=True) if title_tag else "Untitled"
            spans = _extract_text_spans(soup)

        if not spans:
            return f"Error: No content extracted from {url}"

        # Apply token limit
        total_chars = sum(len(s) for s in spans)
        char_limit = max_tokens * 4
        truncation_hint = None
        if total_chars > char_limit:
            total_kb = total_chars / 1024
            total_tokens_est = total_chars // 4
            truncation_hint = (
                f"Full page is {total_kb:.1f} KB (~{total_tokens_est:,} tokens), "
                f"showing first ~{max_tokens:,} tokens. "
                f"Use max_tokens to adjust, or kagi_summarize for a summary of large pages."
            )
            char_count = 0
            truncated_spans = []
            for span in spans:
                if char_count + len(span) > char_limit:
                    truncated_spans.append("[content truncated]")
                    break
                truncated_spans.append(span)
                char_count += len(span)
            spans = truncated_spans

        mime_type = content_type.split(";")[0].strip() if content_type else "text/html"
        xml_output = _build_document_xml(title, spans, str(response.url), mime_type)

        if truncation_hint:
            return f"<!-- truncated: {truncation_hint} -->\n{xml_output}"
        return xml_output

    # --- cite=False (default): markdown output ---
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

    # --- MediaWiki fast path ---
    try:
        from .mediawiki import _detect_mediawiki, _fetch_mediawiki_page, _mediawiki_html_to_markdown

        wiki_info = await _detect_mediawiki(url)
        if wiki_info:
            wiki_page = await _fetch_mediawiki_page(
                wiki_info["api_base"], wiki_info["page_title"],
            )
            if wiki_page:
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

    source_url = str(response.url)
    if fragment:
        source_url += f"#{fragment}"
    return _sections_response(title, source_url, markdown_content, section_names)


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
