"""Direct HTTP content fetching without JavaScript rendering."""

import logging
import re
from typing import Optional, Union

import httpx

from .common import _FETCH_HEADERS, check_url_ssrf
from .markdown import (
    html_to_markdown, _detect_js_dependent,
    _extract_sections_from_markdown, _build_section_list,
    _filter_markdown_by_sections, _build_frontmatter, _apply_hard_truncation,
    _fence_content, _TRUST_ADVISORY,
)
from ._pipeline import (
    _extract_fragment, _normalize_sections, _resolve_fragment_source,
    _mediawiki_fast_path, _arxiv_fast_path, _s2_fast_path, _doi_fast_path, _reddit_fast_path, _github_fast_path,
    _process_markdown_sections,
    _cached_mediawiki_fetch,
    _page_cache, _search_slices, _get_slices,
    _dispatch_slicing,
)
from .mediawiki import _mediawiki_html_to_markdown, _extract_citations, _format_citations

logger = logging.getLogger(__name__)

# GitHub line anchor fragments: #L45 or #L45-L100
_LINE_ANCHOR_RE = re.compile(r"^L(\d+)(?:-L(\d+))?$")


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

    # Detect GitHub line anchors (#L45, #L45-L100) before they become
    # section requests — these are line ranges, not heading names.
    line_range: Optional[tuple[int, int]] = None
    if fragment and not section_names:
        lm = _LINE_ANCHOR_RE.match(fragment)
        if lm:
            start = int(lm.group(1))
            end = int(lm.group(2)) if lm.group(2) else start
            line_range = (start, end)
            # Don't convert to section_names — line ranges are handled
            # by the GitHub fast path directly
        else:
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
        if cached and cached.renderer in ("direct", "wiki", "reddit", "github"):
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
                fm_entries: dict[str, str | bool | list[int]] = {
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
        return "Error: Footnote retrieval requires a MediaWiki page (Wikipedia, etc.)"

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

    # --- Reddit fast path (after DOI, before MediaWiki) ---
    try:
        from .reddit import _detect_reddit_url
        if _detect_reddit_url(url):
            # Always run the fast path first — it populates _page_cache
            result = await _reddit_fast_path(url, max_tokens)
            if result is not None:
                if want_slicing:
                    return _dispatch_slicing(
                        url, search, slices,
                        slices_list if slices is not None else [],
                        max_tokens, source_url, warning=fragment_warning,
                    )
                if section_names:
                    # Section filtering on cached Reddit markdown
                    cached = _page_cache.get(url)
                    if cached and cached.markdown:
                        return _process_markdown_sections(
                            cached.markdown, section_names, max_tokens,
                            frontmatter_entries={
                                "source": source_url,
                                "api": "Reddit (.json)",
                                "warning": fragment_warning,
                            },
                            cache_url=url,
                        )
                return result
    except Exception:
        pass

    # --- GitHub fast path (after Reddit, before MediaWiki) ---
    try:
        from .github import _detect_github_url
        if _detect_github_url(url):
            result = await _github_fast_path(url, max_tokens, line_range=line_range)
            if result is not None:
                if want_slicing:
                    return _dispatch_slicing(
                        url, search, slices,
                        slices_list if slices is not None else [],
                        max_tokens, source_url, warning=fragment_warning,
                    )
                if section_names:
                    cached = _page_cache.get(url)
                    if cached and cached.markdown:
                        return _process_markdown_sections(
                            cached.markdown, section_names, max_tokens,
                            frontmatter_entries={
                                "source": source_url,
                                "api": "GitHub",
                                "warning": fragment_warning,
                            },
                            cache_url=url,
                        )
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

    # --- SSRF check ---
    ssrf_error = check_url_ssrf(url)
    if ssrf_error:
        return ssrf_error

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
        if _detect_js_dependent(response.text):
            fm = _build_frontmatter({
                "source": source_url,
                "warning": fragment_warning,
                "see_also": "WebFetchJS — this page requires JavaScript to render content",
            })
            return fm
        return f"Error: No content extracted from {url}"

    fm_entries = {"source": source_url, "warning": fragment_warning}

    # arXiv /html/ auto-tracking: if this is a full paper fetch, track it
    # on the shelf so it shows up alongside papers found via ArXiv/S2 tools.
    try:
        from .arxiv import _detect_arxiv_html_url, _strip_version
        arxiv_id = _detect_arxiv_html_url(url)
        if arxiv_id:
            from .shelf import _track_on_shelf, CitationRecord
            arxiv_doi = f"10.48550/arXiv.{_strip_version(arxiv_id)}"
            fm_entries["shelf"] = await _track_on_shelf(CitationRecord(
                doi=arxiv_doi,
                title=title,
                source_tool="arxiv",
            ))
    except Exception:
        pass

    output = _process_markdown_sections(
        markdown_content, section_names, max_tokens, fm_entries,
        title=title, cache_url=url, renderer="direct",
    )

    # If search/slices was requested, the cache is now populated — dispatch
    if want_slicing:
        return _dispatch_slicing(url, search, slices, slices_list if slices is not None else [],
                                 max_tokens, source_url, warning=fragment_warning)

    return output


async def _github_sections(
    match, original_url: str, section_names: Optional[list[str]],
) -> Optional[str]:
    """Build section listing for GitHub URLs.

    Returns a formatted section list, or None if the URL kind
    isn't supported for section listing.
    """
    from .github import (
        extract_code_definitions, format_code_sections,
        _build_issue_markdown, _build_pr_markdown,
        _get_github_token,
    )
    from .common import _FETCH_HEADERS
    from pathlib import Path

    # --- Blob: code definition tree via tree-sitter ---
    if match.kind == "blob" and match.ref and match.path:
        ext = Path(match.path).suffix.lower()

        # Check cache first
        cached = _page_cache.get(original_url, renderer="github")
        if cached:
            source_text = cached.markdown
        else:
            # Fetch raw content
            raw_url = (
                f"https://raw.githubusercontent.com/"
                f"{match.owner}/{match.repo}/{match.ref}/{match.path}"
            )
            headers = dict(_FETCH_HEADERS)
            token = _get_github_token()
            if token:
                headers["Authorization"] = f"token {token}"

            try:
                async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
                    resp = await client.get(raw_url, headers=headers)
                if resp.status_code != 200:
                    return None
                source_text = resp.text
            except Exception:
                return None

        defs = extract_code_definitions(source_text, ext)
        if not defs:
            fm = _build_frontmatter({
                "source": original_url,
                "api": "GitHub (raw)",
                "note": f"No code definitions extracted for {ext} file. "
                        "Grammar may not be installed, or file has no function/class definitions.",
                "hint": "Use WebFetchDirect to view the file content directly",
            })
            return fm

        section_body = format_code_sections(defs)
        fm = _build_frontmatter({
            "source": original_url,
            "api": "GitHub (raw)",
            "language": ext.lstrip("."),
            "definitions": len(defs),
            "trust": _TRUST_ADVISORY,
            "hint": "Use WebFetchDirect with section= to extract a specific "
                    "definition, or search= for BM25 keyword search within the file",
        })
        return fm + "\n\n" + _fence_content(section_body, title=match.path)

    # --- Issue: comment tree ---
    if match.kind == "issue" and match.number:
        built = await _build_issue_markdown(
            match.owner, match.repo, match.number, limit=100, page=1,
        )
        if isinstance(built, str):
            return built
        title, raw_md, state, _ = built

        # Extract comment headings as section tree
        lines = []
        for line in raw_md.split("\n"):
            if line.startswith("### ic_"):
                lines.append(f"- {line.lstrip('#').strip()}")
            elif line.startswith("**@") and lines:
                # Append author info to last entry
                lines[-1] += f" {line}"

        if not lines:
            lines.append("(no comments)")

        section_body = "\n".join(lines)
        fm = _build_frontmatter({
            "source": f"https://github.com/{match.owner}/{match.repo}/issues/{match.number}",
            "api": "GitHub",
            "type": "issue",
            "state": state,
            "trust": _TRUST_ADVISORY,
            "hint": "Use WebFetchDirect with section='ic_<id>' to extract a "
                    "specific comment, or search= for BM25 keyword search",
        })
        return fm + "\n\n" + _fence_content(section_body, title=title)

    # --- Pull request: comment tree ---
    if match.kind == "pull" and match.number:
        built = await _build_pr_markdown(
            match.owner, match.repo, match.number, limit=100, page=1,
        )
        if isinstance(built, str):
            return built
        title, raw_md, display_state, _ = built

        lines = []
        for line in raw_md.split("\n"):
            if line.startswith("## "):
                # Top-level sections: "Diff stat", "Review comments", "Comments"
                lines.append(f"\n**{line.lstrip('#').strip()}**")
            elif line.startswith("### ") and not line.startswith("### ic_"):
                # File path heading (review comments)
                lines.append(f"- {line.lstrip('#').strip()}")
            elif line.startswith("#### rc_"):
                lines.append(f"  - {line.lstrip('#').strip()}")
            elif line.startswith("### ic_"):
                lines.append(f"- {line.lstrip('#').strip()}")
            elif line.startswith("**@") and lines:
                lines[-1] += f" {line}"

        if not lines:
            lines.append("(no comments)")

        section_body = "\n".join(lines).strip()
        fm = _build_frontmatter({
            "source": f"https://github.com/{match.owner}/{match.repo}/pull/{match.number}",
            "api": "GitHub",
            "type": "pull_request",
            "state": display_state,
            "trust": _TRUST_ADVISORY,
            "hint": "Use WebFetchDirect with section= to extract a file's "
                    "review thread or specific comment, or search= for BM25 keyword search",
        })
        return fm + "\n\n" + _fence_content(section_body, title=title)

    # --- Repo: redirect to GitHub tool ---
    if match.kind == "repo":
        fm = _build_frontmatter({
            "source": original_url,
            "api": "GitHub",
            "note": "Section listing is not applicable for repository pages.",
            "see_also": f"Use GitHub tool with action='repo' query='{match.owner}/{match.repo}' "
                        "for repo metadata and README, or action='tree' for directory listing",
        })
        return fm

    # --- Tree: redirect to GitHub tool ---
    if match.kind == "tree":
        query = f"{match.owner}/{match.repo}/{match.path or ''}"
        fm = _build_frontmatter({
            "source": original_url,
            "api": "GitHub",
            "note": "Section listing is not applicable for directory pages.",
            "see_also": f"Use GitHub tool with action='tree' query='{query}' "
                        "for directory listing",
        })
        return fm

    # --- Gist, discussion, or unknown: redirect ---
    fm = _build_frontmatter({
        "source": original_url,
        "api": "GitHub",
        "see_also": "Use the GitHub tool for structured access to this content",
    })
    return fm


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

    # --- Reddit fast path (comment tree as sections) ---
    from .reddit import (
        _detect_reddit_url, _classify_reddit_url, _fetch_reddit_json,
        _resolve_redd_it, _build_comment_section_tree, RedditPageType,
    )
    reddit_url = _detect_reddit_url(url)
    if reddit_url:
        try:
            # Resolve short links
            if _classify_reddit_url(reddit_url) == RedditPageType.SHORT_LINK:
                reddit_url = await _resolve_redd_it(reddit_url) or reddit_url

            page_type = _classify_reddit_url(reddit_url)
            if page_type == RedditPageType.COMMENT_THREAD:
                data = await _fetch_reddit_json(reddit_url)
                if isinstance(data, list) and len(data) >= 2:
                    title, section_body = _build_comment_section_tree(data)
                    fm = _build_frontmatter({
                        "source": original_url,
                        "api": "Reddit (.json)",
                        "trust": _TRUST_ADVISORY,
                        "hint": "Use WebFetchDirect with section=#comment_id to "
                                "extract a specific comment and its replies, "
                                "or search= for keyword search across comments",
                    })
                    return fm + "\n\n" + _fence_content(section_body)

            # Non-thread Reddit pages: no meaningful section tree
            fm = _build_frontmatter({
                "source": original_url,
                "api": "Reddit (.json)",
                "note": "Section listing is only available for comment threads. "
                        "Use WebFetchDirect with search= for keyword search.",
            })
            return fm
        except Exception:
            pass

    # --- GitHub fast path ---
    from .github import _detect_github_url
    gh_match = _detect_github_url(url)
    if gh_match is not None:
        result = await _github_sections(gh_match, original_url, section_names)
        if result is not None:
            return result

    # --- MediaWiki fast path ---
    try:
        wiki_info, wiki_page = await _cached_mediawiki_fetch(url)
        if wiki_info and wiki_page:
            markdown_content = _mediawiki_html_to_markdown(wiki_page["html"])
            return _sections_response(
                wiki_page["title"], original_url, markdown_content, section_names,
            )
    except Exception:
        pass

    # --- SSRF check ---
    ssrf_error = check_url_ssrf(url)
    if ssrf_error:
        return ssrf_error

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
        if _detect_js_dependent(response.text):
            fm = _build_frontmatter({
                "source": original_url,
                "see_also": "WebFetchJS — this page requires JavaScript to render content",
            })
            return fm
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
