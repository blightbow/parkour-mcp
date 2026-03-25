"""Direct HTTP content fetching without JavaScript rendering."""

import logging
from typing import Optional, Union

import httpx

from .common import _FETCH_HEADERS
from .markdown import (
    html_to_markdown, _extract_sections_from_markdown, _build_section_list,
    _filter_markdown_by_sections, _build_frontmatter, _apply_hard_truncation,
    _fence_content, _TRUST_ADVISORY,
)
from ._pipeline import (
    _extract_fragment, _normalize_sections, _resolve_fragment_source,
    _mediawiki_fast_path, _arxiv_fast_path, _s2_fast_path, _doi_fast_path,
    _process_markdown_sections,
    _cached_mediawiki_fetch,
    _page_cache, _search_slices, _get_slices,
    _dispatch_slicing,
)
from .mediawiki import _mediawiki_html_to_markdown, _extract_citations, _format_citations

logger = logging.getLogger(__name__)


async def web_fetch_direct(
    url: str,
    max_tokens: int = 5000,
    section: Optional[Union[str, list[str]]] = None,
    footnotes: Optional[Union[int, list[int]]] = None,
    search: Optional[str] = None,
    slices: Optional[Union[int, list[int]]] = None,
) -> str:
    """Fetch raw content from a URL without JavaScript rendering.

    Returns markdown with YAML frontmatter. Supports HTML, plain text, JSON,
    and XML content types. For HTML pages, use the section parameter to extract
    specific sections by heading name.

    For MediaWiki pages (Wikipedia, etc.), inline footnotes appear as [^N]
    markers; use the footnotes parameter to retrieve specific entries
    (e.g. footnotes=4 or footnotes=[1,3,8]).

    For long or poorly-sectioned pages, use search for BM25 keyword search
    (returns matching ~500-token slices ranked by relevance, terms matched
    independently), or slices to retrieve specific slices by index.

    Args:
        url: The URL to fetch
        max_tokens: Limit on content length in approximate token count (default 5000)
        section: Section name or list of section names to extract from the page
        footnotes: Footnote number or list of numbers to retrieve from the page
        search: Search terms for BM25 keyword matching within cached page content
        slices: Slice index or list of indices to retrieve from cached page content
    """
    # Extract fragment from URL (e.g. #section-name) as implicit section request
    url, fragment = _extract_fragment(url)
    section_names = _normalize_sections(section)
    if fragment and not section_names:
        section_names = [fragment]
    source_url, fragment_warning = _resolve_fragment_source(url, fragment, section)

    # Normalize empty search/slices to None
    if search is not None and search == "":
        search = None
    slices_list: list[int] = []
    if slices is not None:
        slices_list = [slices] if isinstance(slices, int) else list(slices)
        if not slices_list:
            slices = None
    want_slicing = search is not None or slices is not None

    # --- Parameter validation ---
    if search is not None and slices is not None:
        return "Error: 'search' and 'slices' are mutually exclusive."
    if want_slicing and section_names:
        return "Error: 'search'/'slices' and 'section' are mutually exclusive."

    # Footnotes are a companion ask — when combined with another mode,
    # honor the primary intent and warn that footnotes were ignored.
    # Footnotes-only mode is for when you specifically want bibliography entries.
    if footnotes is not None and (want_slicing or section_names):
        _fn_warn = (
            "footnotes parameter ignored — use footnotes as the sole parameter "
            "to retrieve bibliography entries"
        )
        if fragment_warning:
            fragment_warning = [fragment_warning, _fn_warn]
        else:
            fragment_warning = _fn_warn
        footnotes = None

    # --- Search/slices cache-first path ---
    # Only reuse "direct" or "wiki" entries.  A "js" entry was produced by
    # Playwright and should not be served from a tool that does static HTTP.
    if want_slicing:
        fm_base = {"source": source_url, "warning": fragment_warning}
        cached = _page_cache.get(url)
        if cached and cached.renderer in ("direct", "wiki"):
            fm_base["title"] = cached.title or "Untitled"
            if search is not None:
                return _search_slices(url, search, max_tokens, fm_base) or \
                    "Error: Page cache unavailable."
            else:
                return _get_slices(url, slices_list, max_tokens, fm_base) or \
                    "Error: Page cache unavailable."
        # Cache miss — fall through to fetch, which populates the cache

    # --- Footnote-only path (MediaWiki pages) ---
    if footnotes is not None:
        requested = [footnotes] if isinstance(footnotes, int) else list(footnotes)
        try:
            wiki_info, wiki_page = await _cached_mediawiki_fetch(url)
            if wiki_info and wiki_page:
                all_footnotes = _extract_citations(wiki_page["html"])
                if not all_footnotes:
                    return f"Error: No footnotes found for {url}"
                # Filter to requested footnote numbers
                selected = [c for c in all_footnotes if c["n"] in requested]
                not_found = sorted(set(requested) - {c["n"] for c in selected})
                title = wiki_page["title"]
                fm_entries = {
                    "source": source_url,
                    "trust": _TRUST_ADVISORY,
                    "footnotes_only": True,
                }
                if not_found:
                    available = sorted(c["n"] for c in all_footnotes)
                    fm_entries["footnotes_not_found"] = not_found
                    fm_entries["footnotes_available"] = f"1-{available[-1]}"
                fm = _build_frontmatter(fm_entries)
                if selected:
                    return fm + "\n\n" + _fence_content(
                        _format_citations(selected), title=title,
                    )
                return fm + "\n\n" + _fence_content("", title=title)
        except Exception:
            pass
        return f"Error: Footnote retrieval requires a MediaWiki page (Wikipedia, etc.)"

    # --- arXiv fast path (before S2 — arXiv URLs get arXiv-native metadata) ---
    try:
        from .arxiv import _detect_arxiv_url
        if _detect_arxiv_url(url):
            if want_slicing:
                return (
                    "Error: search/slices not supported for arXiv abstract/PDF URLs. "
                    "Use the /html/ URL for full text with search/slices support."
                )
            result = await _arxiv_fast_path(url)
            if result is not None:
                return result
    except Exception:
        pass

    # --- Semantic Scholar fast path ---
    try:
        from .semantic_scholar import _detect_s2_url
        if _detect_s2_url(url):
            if want_slicing:
                return (
                    "Error: search/slices not supported for Semantic Scholar URLs. "
                    "Use the SemanticScholar tool's snippets action instead."
                )
            result = await _s2_fast_path(url)
            if result is not None:
                return result
    except Exception:
        pass

    # --- DOI fast path (after arXiv/S2, before MediaWiki) ---
    try:
        from .doi import _detect_doi_url
        if _detect_doi_url(url):
            if want_slicing:
                return "Error: search/slices not supported for DOI resolver URLs."
            result = await _doi_fast_path(url)
            if result is not None:
                return result
    except Exception:
        pass

    # --- MediaWiki fast path (before HTTP fetch) ---
    try:
        result = await _mediawiki_fast_path(
            url, section_names, max_tokens,
            extra_entries={"source": source_url, "warning": fragment_warning},
            cache_url=url,
        )
        if result is not None:
            if want_slicing:
                return _dispatch_slicing(url, search, slices, slices_list if slices is not None else [],
                                         max_tokens, source_url, warning=fragment_warning)
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

    # --- Non-HTML content ---
    if is_plain or is_json or is_xml:
        if want_slicing:
            return "Error: search/slices requires HTML content."
        text = response.text.strip()
        if not text:
            return f"Error: No content extracted from {url}"

        title = url.rsplit("/", 1)[-1] or "Untitled"
        ct_label = "json" if is_json else ("xml" if is_xml else "plain text")

        text, truncation_hint = _apply_hard_truncation(
            text, max_tokens,
            hint_prefix="Full content",
            hint_suffix="Use max_tokens to adjust.",
        )

        fm = _build_frontmatter({
            "source": source_url,
            "trust": _TRUST_ADVISORY,
            "warning": fragment_warning,
            "content_type": ct_label,
            "truncated": truncation_hint,
        })
        return fm + "\n\n" + _fence_content(text, title=title)

    # HTML content: parse → markdown
    title, markdown_content = html_to_markdown(response.text)

    if not markdown_content:
        return f"Error: No content extracted from {url}"

    fm_entries = {"title": title, "source": source_url, "warning": fragment_warning}

    # arXiv /html/ auto-tracking: if this is a full paper fetch, track it
    # on the shelf so it shows up alongside papers found via ArXiv/S2 tools.
    try:
        from .arxiv import _detect_arxiv_html_url, _strip_version
        arxiv_id = _detect_arxiv_html_url(url)
        if arxiv_id:
            from .shelf import _track_on_shelf, CitationRecord
            arxiv_doi = f"10.48550/arXiv.{_strip_version(arxiv_id)}"
            fm_entries["shelf"] = _track_on_shelf(CitationRecord(
                doi=arxiv_doi,
                title=title,
                source_tool="arxiv",
            ))
    except Exception:
        pass

    output = _process_markdown_sections(
        markdown_content, section_names, max_tokens, fm_entries,
        cache_url=url, renderer="direct",
    )

    # If search/slices was requested, the cache is now populated — dispatch
    if want_slicing:
        return _dispatch_slicing(url, search, slices, slices_list if slices is not None else [],
                                 max_tokens, source_url, warning=fragment_warning)

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

    # --- arXiv fast path (sections not applicable for API data) ---
    from .arxiv import _detect_arxiv_url
    if _detect_arxiv_url(url):
        arxiv_id = _detect_arxiv_url(url)
        fm = _build_frontmatter({
            "title": "arXiv paper",
            "source": original_url,
            "api": "arXiv",
            "note": "Section listing is not applicable for API-sourced paper data. "
                    f"Use WebFetchDirect with https://arxiv.org/html/{arxiv_id} "
                    "for full paper text with section-aware browsing.",
        })
        return fm

    # --- Semantic Scholar fast path (sections not applicable for API data) ---
    from .semantic_scholar import _detect_s2_url
    if _detect_s2_url(url):
        fm = _build_frontmatter({
            "title": "Semantic Scholar paper",
            "source": original_url,
            "api": "Semantic Scholar",
            "note": "Section listing is not applicable for API-sourced paper data. "
                    "Use WebFetchDirect or SemanticScholar tool for full content.",
        })
        return fm

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
        fm = _build_frontmatter({
            "source": url,
            "trust": _TRUST_ADVISORY,
        })
        return fm + "\n\n" + _fence_content("No sections found.", title=title)

    entries = {
        "source": url,
        "trust": _TRUST_ADVISORY,
        "hint": "Use WebFetchDirect with section parameter to extract specific sections by name",
    }
    sections_available = _build_section_list(all_sections, include_slugs=True)
    sections_not_found = None

    if section_names:
        _, _matched_meta, unmatched = _filter_markdown_by_sections(
            markdown_content, section_names, all_sections,
        )
        sections_not_found = unmatched or None

    fm = _build_frontmatter(
        entries,
        sections_not_found=sections_not_found,
    )
    # Render section list as fenced body content (headings are untrusted)
    section_body = "\n".join(sections_available)
    return fm + "\n\n" + _fence_content(section_body, title=title)
