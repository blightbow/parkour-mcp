"""Shared content-processing pipelines for fetch tools.

Consolidates MediaWiki fast-path logic and post-fetch section/truncation/frontmatter
assembly that is common to both web_fetch_js and web_fetch_direct.
"""

import logging
from collections import OrderedDict
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
from .doi import _detect_doi_url, _fetch_doi_paper
from .reddit import _detect_reddit_url, _fetch_reddit_content, _split_by_comments

logger = logging.getLogger(__name__)


def _fmt_bytes(n: int) -> str:
    """Format a byte count as a human-readable string for log messages."""
    if n < 1024:
        return f"{n} B"
    if n < 1024 * 1024:
        return f"{n / 1024:.1f} KB"
    return f"{n / (1024 * 1024):.1f} MB"


# Default maximum entries for caches.  Each page-cache entry holds a tantivy
# in-memory index + slices + full markdown, so memory is proportional to page
# size.  8 entries across probation+protected is enough for comparing several
# pages while keeping recently drilled-into ones warm.
PAGE_CACHE_MAX_ENTRIES = 8
WIKI_CACHE_MAX_ENTRIES = 5


# ---------------------------------------------------------------------------
# LRU MediaWiki page cache
# ---------------------------------------------------------------------------
# Avoids redundant API calls for the common workflow:
#   web_fetch_sections(url) → web_fetch_direct(url, section=...) → citation
# Keyed on canonical URL (no fragment). Stores detect + full-page results.

class _WikiCacheEntry:
    """A single cached MediaWiki page."""

    __slots__ = ("url", "wiki_info", "wiki_page")

    def __init__(self, url: str, wiki_info: dict, wiki_page: Optional[dict]):
        self.url = url
        self.wiki_info = wiki_info
        self.wiki_page = wiki_page


class _WikiCache:
    """LRU cache for MediaWiki API results."""

    def __init__(self, max_entries: int = WIKI_CACHE_MAX_ENTRIES):
        self._entries: OrderedDict[str, _WikiCacheEntry] = OrderedDict()
        self._max_entries = max_entries

    def get(self, url: str) -> tuple[Optional[dict], Optional[dict]]:
        """Return (wiki_info, wiki_page) if url is cached, else (None, None)."""
        entry = self._entries.get(url)
        if entry is not None:
            self._entries.move_to_end(url)
            return entry.wiki_info, entry.wiki_page
        return None, None

    def store(self, url: str, wiki_info: dict, wiki_page: Optional[dict]):
        """Cache a MediaWiki page, evicting the LRU entry if at capacity."""
        if url in self._entries:
            self._entries[url] = _WikiCacheEntry(url, wiki_info, wiki_page)
            self._entries.move_to_end(url)
        else:
            if len(self._entries) >= self._max_entries:
                self._entries.popitem(last=False)
            self._entries[url] = _WikiCacheEntry(url, wiki_info, wiki_page)

    @property
    def stats(self) -> dict:
        """Return cache diagnostics for developer inspection."""
        return {
            "max_entries": self._max_entries,
            "total_entries": len(self._entries),
            "urls": list(self._entries.keys()),
        }

    def clear(self):
        """Evict all entries."""
        self._entries.clear()


_wiki_cache = _WikiCache()


# ---------------------------------------------------------------------------
# LRU page cache (post-markdown-conversion)
# ---------------------------------------------------------------------------
# Caches recently fetched pages as pre-sliced content for keyword search and
# index-based retrieval.  Populated by _process_markdown_sections (all HTML
# paths feed through it) and by fast-path handlers (Reddit, etc.).

class _CacheEntry:
    """A single cached page with sliced content and BM25 search index."""

    __slots__ = ("url", "title", "markdown", "slices", "slice_ancestry",
                 "_tantivy_index", "renderer", "group")

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

    def __init__(
        self,
        url: str,
        title: str,
        markdown: str,
        renderer: Optional[str] = None,
        group: Optional[str] = None,
        presplit: Optional[list[tuple[int, str]]] = None,
    ):
        self.url = url
        self.title = title
        self.markdown = markdown
        self.renderer = renderer
        self.group = group

        if presplit is not None:
            self.slices: list[str] = [text for _, text in presplit]
            offsets = [offset for offset, _ in presplit]
        else:
            chunks = self._SPLITTER.chunk_indices(markdown)
            self.slices = [text for _, text in chunks]
            offsets = [offset for offset, _ in chunks]

        sections = _extract_sections_from_markdown(markdown)
        self.slice_ancestry: list[str] = _compute_slice_ancestry(sections, offsets)

        # Build tantivy in-memory search index over slices
        schema = self._get_schema()
        self._tantivy_index = tantivy.Index(schema)
        writer = self._tantivy_index.writer()
        for i, text in enumerate(self.slices):
            writer.add_document(tantivy.Document(body=text, idx=i))
        writer.commit()
        self._tantivy_index.reload()

    @property
    def estimated_bytes(self) -> int:
        """Estimate memory usage of this entry's Python-side data.

        Counts the markdown source, slices (which duplicate most of the
        markdown text), and ancestry strings.  The tantivy index lives in
        Rust heap memory and cannot be measured from Python; a 0.7x
        multiplier on the indexed text approximates the compressed store +
        inverted index overhead (empirically 0.65-0.72x across 10-200
        slices via disk-write measurement of equivalent RAM indexes).
        """
        md_bytes = len(self.markdown.encode("utf-8")) if self.markdown else 0
        slices_bytes = sum(len(s.encode("utf-8")) for s in self.slices)
        ancestry_bytes = sum(len(a.encode("utf-8")) for a in self.slice_ancestry)
        # Tantivy index heuristic: stored fields + inverted index ≈ 0.7× text
        tantivy_est = int(slices_bytes * 0.7)
        return md_bytes + slices_bytes + ancestry_bytes + tantivy_est

    def search(self, query_str: str, limit: int = 50) -> list[int]:
        """BM25 search over cached slices. Returns matching slice indices ranked by relevance."""
        if not self._tantivy_index or not self.slices:
            return []
        query = self._tantivy_index.parse_query(query_str, ["body"])
        searcher = self._tantivy_index.searcher()
        results = searcher.search(query, limit=limit)
        return [searcher.doc(addr)["idx"][0] for _score, addr in results.hits]


class _PageCache:
    """2Q (two-queue) cache for sliced page content with BM25 search indexes.

    New entries land in the **probation** queue (FIFO).  When a probation
    entry is accessed again via ``get()``, it is **promoted** to the
    **protected** queue (LRU).  Eviction prefers probation (cheap one-hit
    pages) before falling back to the protected LRU tail.

    This is scan-resistant: pages fetched once during browsing stay in
    probation and get evicted first, while pages the user drills into
    (search, section, slices) get promoted and persist.

    The optional ``group`` field on entries enables entity linking: entries
    sharing a group tag are evicted together (e.g. a PR's comments and code
    as separate but linked cache entries).
    """

    def __init__(self, max_entries: int = PAGE_CACHE_MAX_ENTRIES):
        self._probation: OrderedDict[str, _CacheEntry] = OrderedDict()
        self._protected: OrderedDict[str, _CacheEntry] = OrderedDict()
        self._max_entries = max_entries

    def _total(self) -> int:
        return len(self._probation) + len(self._protected)

    @property
    def stats(self) -> dict:
        """Return cache diagnostics for developer inspection.

        Includes queue distribution, per-entry size estimates, and totals.
        This is a developer tool — not exposed to the LLM via tool output.
        """
        def _entry_info(entry: _CacheEntry, queue: str) -> dict:
            return {
                "url": entry.url,
                "title": entry.title,
                "renderer": entry.renderer,
                "group": entry.group,
                "slices": len(entry.slices),
                "estimated_bytes": entry.estimated_bytes,
                "queue": queue,
            }

        entries = []
        for e in self._probation.values():
            entries.append(_entry_info(e, "probation"))
        for e in self._protected.values():
            entries.append(_entry_info(e, "protected"))

        total_bytes = sum(e["estimated_bytes"] for e in entries)
        return {
            "max_entries": self._max_entries,
            "total_entries": self._total(),
            "probation_entries": len(self._probation),
            "protected_entries": len(self._protected),
            "total_estimated_bytes": total_bytes,
            "entries": entries,
        }

    def get(self, url: str, renderer: Optional[str] = None) -> Optional[_CacheEntry]:
        """Return the cached entry for *url*, or None on miss.

        Accessing a **probation** entry promotes it to **protected** (proving
        it is part of the working set, not a scan).  Accessing a
        **protected** entry refreshes its LRU position.

        When *renderer* is specified, only returns a hit if the cached entry
        was produced by the same renderer.  This prevents WebFetchJS from
        reusing sparse content that WebFetchDirect cached from a JS-heavy page.
        """
        # Check protected first (most likely for active pages)
        entry = self._protected.get(url)
        if entry is not None:
            if renderer is not None and entry.renderer != renderer:
                return None
            self._protected.move_to_end(url)
            return entry

        # Check probation — hit here triggers promotion
        entry = self._probation.get(url)
        if entry is not None:
            if renderer is not None and entry.renderer != renderer:
                return None
            # Promote: move from probation to protected
            del self._probation[url]
            self._protected[url] = entry
            self._protected.move_to_end(url)
            logger.debug(
                "cache promote %s → protected (%d probation, %d protected, ~%s)",
                url, len(self._probation), len(self._protected),
                _fmt_bytes(entry.estimated_bytes),
            )
            return entry

        return None

    def store(
        self,
        url: str,
        title: str,
        markdown: str,
        renderer: Optional[str] = None,
        presplit: Optional[list[tuple[int, str]]] = None,
        group: Optional[str] = None,
    ):
        """Slice markdown, build BM25 index, and cache the entry.

        New URLs enter **probation**.  If the URL already exists (in either
        queue), the entry is replaced in-place without evicting others.

        When *presplit* is provided it is used directly instead of running
        ``MarkdownSplitter``.  Each element is ``(char_offset, text)`` —
        the offset is used for section-ancestry computation.  This lets
        callers supply domain-aware chunks (e.g. one chunk per Reddit
        comment) while still getting BM25 indexing and ancestry breadcrumbs.

        The *group* tag enables entity linking: entries sharing a group are
        evicted together when any member is the eviction victim.
        """
        entry = _CacheEntry(
            url, title, markdown,
            renderer=renderer, group=group, presplit=presplit,
        )

        # Update in-place if URL already cached (in either queue)
        if url in self._protected:
            self._protected[url] = entry
            self._protected.move_to_end(url)
            return
        if url in self._probation:
            self._probation[url] = entry
            self._probation.move_to_end(url)
            return

        # New entry → probation (FIFO)
        while self._total() >= self._max_entries:
            self._evict()
        self._probation[url] = entry
        logger.debug(
            "cache store %s → probation (%d probation, %d protected, ~%s)",
            url, len(self._probation), len(self._protected),
            _fmt_bytes(entry.estimated_bytes),
        )

    def _evict(self):
        """Evict one entry, preferring probation over protected.

        Group-aware: if the victim has a group tag, all entries sharing
        that group are evicted together from both queues.
        """
        # Prefer evicting from probation (cheap one-hit pages)
        victim_queue = self._probation if self._probation else self._protected
        if not victim_queue:
            return

        oldest_url = next(iter(victim_queue))
        oldest = victim_queue[oldest_url]

        if oldest.group is not None:
            group = oldest.group
            evicted: list[str] = []
            for q in (self._probation, self._protected):
                to_remove = [u for u, e in q.items() if e.group == group]
                for u in to_remove:
                    evicted.append(u)
                    del q[u]
            logger.debug("cache evict group %s: %s", group, evicted)
        else:
            logger.debug("cache evict %s (~%s)", oldest_url, _fmt_bytes(oldest.estimated_bytes))
            del victim_queue[oldest_url]

    def clear(self):
        """Evict all entries from both queues."""
        self._probation.clear()
        self._protected.clear()


_page_cache = _PageCache()


async def _cached_mediawiki_fetch(url: str) -> tuple[Optional[dict], Optional[dict]]:
    """Detect and fetch a MediaWiki page, using the LRU cache.

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
    Uses the wiki cache so repeated calls for the same page are free.
    """
    wiki_info, wiki_page = await _cached_mediawiki_fetch(url)
    if not wiki_info or not wiki_page:
        return None

    markdown_content = _mediawiki_html_to_markdown(wiki_page["html"])

    title = wiki_page["title"]
    frontmatter_entries = {
        "source": url,
        "site": wiki_info["sitename"] or None,
        "generator": wiki_info["generator"] or None,
    }
    if extra_entries:
        frontmatter_entries.update(extra_entries)

    return _process_markdown_sections(
        markdown_content, section_names, max_tokens, frontmatter_entries,
        title=title, cache_url=cache_url, renderer="wiki",
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


async def _doi_fast_path(url: str) -> Optional[str]:
    """Attempt to resolve a doi.org URL via content negotiation.

    Returns formatted paper details on success, or None to signal fallback.
    Delegates arXiv DOIs (10.48550/arXiv.*) to the arXiv handler.
    """
    doi = _detect_doi_url(url)
    if not doi:
        return None

    return await _fetch_doi_paper(doi)


# ---------------------------------------------------------------------------
# Reddit fast path
# ---------------------------------------------------------------------------

async def _reddit_fast_path(url: str, max_tokens: int = 5000) -> Optional[str]:
    """Attempt to fetch a Reddit page via the old.reddit.com .json endpoint.

    Returns formatted content on success, or None if not a Reddit URL.
    Once matched, always returns a string (even errors) to prevent
    fallback to generic HTTP fetch (which hits Reddit's login wall).

    Populates ``_page_cache`` so the caller can dispatch slicing.
    """
    reddit_url = _detect_reddit_url(url)
    if not reddit_url:
        return None

    title, full_markdown = await _fetch_reddit_content(reddit_url)

    # Populate cache with comment-aware splitting (one slice per comment)
    comment_chunks = _split_by_comments(full_markdown)
    _page_cache.store(
        url, title, full_markdown,
        renderer="reddit", presplit=comment_chunks,
    )

    truncated, trunc_hint = _apply_semantic_truncation(full_markdown, max_tokens)

    fm_entries: dict[str, object] = {
        "source": url,
        "api": "Reddit (.json)",
        "trust": _TRUST_ADVISORY,
    }
    if trunc_hint:
        fm_entries["truncated"] = trunc_hint

    fm = _build_frontmatter(fm_entries)
    # Reddit markdown already starts with "# {title}" — don't re-add it
    return fm + "\n\n" + _fence_content(truncated)


# ---------------------------------------------------------------------------
# GitHub fast path
# ---------------------------------------------------------------------------

async def _github_fast_path(
    url: str, max_tokens: int = 5000,
    line_range: Optional[tuple[int, int]] = None,
) -> Optional[str]:
    """Attempt to handle a GitHub URL via the API or raw.githubusercontent.com.

    Returns formatted content on success, or None if not a GitHub URL.
    Once matched, always returns a string (even errors) to prevent
    fallback to generic HTTP fetch (which hits GitHub's JS-heavy SPA).

    Populates ``_page_cache`` for slicing/search support.

    Args:
        line_range: Optional (start, end) 1-based inclusive line range for
            blob URLs.  Extracted from ``#L45`` or ``#L45-L100`` fragments.
    """
    from .github import (
        _detect_github_url, _action_repo, _action_tree,
        _build_issue_markdown, _build_pr_markdown,
        _sectionize_code, _split_github_comments,
        _rate_limit_warning, _get_github_token,
    )
    from .common import _FETCH_HEADERS
    from pathlib import Path
    import httpx

    match = _detect_github_url(url)
    if match is None:
        return None

    # --- Blob: raw file fetch (single request, shared between cache and response) ---
    if match.kind == "blob" and match.ref and match.path:

        raw_url = f"https://raw.githubusercontent.com/{match.owner}/{match.repo}/{match.ref}/{match.path}"
        headers = dict(_FETCH_HEADERS)
        token = _get_github_token()
        if token:
            headers["Authorization"] = f"token {token}"

        try:
            async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
                resp = await client.get(raw_url, headers=headers)
        except httpx.TimeoutException:
            return f"Error: Request timed out for {raw_url}"
        except httpx.RequestError as e:
            return f"Error: Request failed - {type(e).__name__}"

        if resp.status_code == 404:
            if not token:
                return "Error: File not found. If this is a private repo, set GITHUB_TOKEN."
            return "Error: File not found."
        if resp.status_code != 200:
            return f"Error: HTTP {resp.status_code} for {raw_url}"

        raw_content = resp.text

        # Binary detection
        if "\x00" in raw_content[:8192]:
            return f"Error: Binary file ({match.path}). Use the GitHub web UI to view this file."

        # Empty file
        if not raw_content.strip():
            return f"Empty file ({match.path})."

        # Cache with code-aware presplit
        ext = Path(match.path).suffix.lower()
        presplit = _sectionize_code(raw_content, ext)
        _page_cache.store(
            url, match.path, raw_content,
            renderer="github", presplit=presplit,
        )

        # Format response (same as _action_file but without a second fetch)
        from .common import _LANGUAGE_MAP
        lang = _LANGUAGE_MAP.get(ext, "")

        all_lines = raw_content.split("\n")
        total_lines = len(all_lines)

        source = f"https://github.com/{match.owner}/{match.repo}/blob/{match.ref}/{match.path}"
        fm_entries: dict[str, object] = {"source": source, "api": "GitHub (raw)"}
        if lang:
            fm_entries["language"] = lang

        # Apply line range if specified (1-based inclusive)
        if line_range:
            req_start, req_end = line_range
            if req_start > req_end:
                return (
                    f"Error: Invalid line range L{req_start}-L{req_end} "
                    f"(start must not exceed end)."
                )
            if req_start > total_lines:
                return (
                    f"Error: Requested lines {req_start}-{req_end} "
                    f"but file has only {total_lines} lines."
                )
            start = max(1, req_start)
            end = min(req_end, total_lines)
            display_lines = all_lines[start - 1:end]
            line_offset = start

            # Check if the line range exceeds max_tokens budget
            range_chars = sum(len(ln) for ln in display_lines) + len(display_lines)
            char_budget = max_tokens * 4
            if range_chars > char_budget:
                return (
                    f"Error: Line range L{req_start}-L{end} is ~{range_chars // 4} tokens "
                    f"but max_tokens is {max_tokens}. "
                    f"Narrow the line range or increase max_tokens."
                )

            fm_entries["lines"] = f"{start}-{end} of {total_lines}"
            if req_end > total_lines:
                fm_entries["warning"] = (
                    f"Requested lines {req_start}-{req_end} "
                    f"but file ends at line {total_lines}"
                )
        else:
            # Apply max_tokens truncation only when no line range is specified
            char_budget = max_tokens * 4
            if len(raw_content) > char_budget:
                truncated_content = raw_content[:char_budget]
                display_lines = truncated_content.split("\n")
                fm_entries["truncated"] = f"Content truncated to ~{max_tokens} tokens"
            else:
                display_lines = all_lines
            line_offset = 1

        rl_warn = _rate_limit_warning()
        if rl_warn:
            fm_entries["warning"] = rl_warn
        fm = _build_frontmatter(fm_entries)

        width = len(str(line_offset + len(display_lines) - 1))
        numbered = "\n".join(
            f"{i + line_offset:>{width}} | {line}"
            for i, line in enumerate(display_lines)
        )

        fenced_code = f"```{lang}\n{numbered}\n```"
        return fm + "\n\n" + _fence_content(fenced_code, title=match.path)

    # --- Tree: directory listing ---
    if match.kind == "tree" and match.path:
        query = f"{match.owner}/{match.repo}/{match.path}"
        return await _action_tree(query, match.ref)

    # --- Issue ---
    if match.kind == "issue" and match.number:
        built = await _build_issue_markdown(
            match.owner, match.repo, match.number, limit=100, page=1,
        )
        if isinstance(built, str):
            return built  # error string
        title, raw_md, state, extra_fm = built

        # Cache raw markdown with comment-boundary presplit for BM25 search
        comment_chunks = _split_github_comments(raw_md)
        _page_cache.store(
            url, title, raw_md,
            renderer="github", presplit=comment_chunks,
        )

        # Format truncated output
        fm_entries: dict[str, object] = {
            "source": f"https://github.com/{match.owner}/{match.repo}/issues/{match.number}",
            "api": "GitHub", "trust": _TRUST_ADVISORY,
        }
        fm_entries.update(extra_fm)
        rl_warn = _rate_limit_warning()
        if rl_warn:
            fm_entries["warning"] = rl_warn
        content, trunc_hint = _apply_semantic_truncation(raw_md, 5000)
        if trunc_hint:
            fm_entries["truncated"] = trunc_hint
            fm_entries["hint"] = (
                "Use search= for BM25 keyword search across comments, "
                "or section= with a comment ID (e.g. section='ic_12345') "
                "to extract a specific comment."
            )
        fm = _build_frontmatter(fm_entries)
        return fm + "\n\n" + _fence_content(content, title=title)

    # --- Pull request ---
    if match.kind == "pull" and match.number:
        built = await _build_pr_markdown(
            match.owner, match.repo, match.number, limit=100, page=1,
        )
        if isinstance(built, str):
            return built
        title, raw_md, display_state, extra_fm = built

        # Cache raw markdown with comment-boundary presplit for BM25 search
        comment_chunks = _split_github_comments(raw_md)
        _page_cache.store(
            url, title, raw_md,
            renderer="github", presplit=comment_chunks,
        )

        fm_entries = {
            "source": f"https://github.com/{match.owner}/{match.repo}/pull/{match.number}",
            "api": "GitHub", "trust": _TRUST_ADVISORY,
        }
        fm_entries.update(extra_fm)
        rl_warn = _rate_limit_warning()
        if rl_warn:
            fm_entries["warning"] = rl_warn
        content, trunc_hint = _apply_semantic_truncation(raw_md, 5000)
        if trunc_hint:
            fm_entries["truncated"] = trunc_hint
            fm_entries["hint"] = (
                "Use search= for BM25 keyword search across comments and review threads, "
                "or section= with a file path or comment ID to extract specific content."
            )
        fm = _build_frontmatter(fm_entries)
        return fm + "\n\n" + _fence_content(content, title=title)

    # --- Repo root ---
    if match.kind == "repo":
        query = f"{match.owner}/{match.repo}"
        return await _action_repo(query)

    # --- Gist ---
    if match.kind == "gist" and match.gist_id:
        from .github import _github_request
        gist_result = await _github_request("GET", f"/gists/{match.gist_id}")
        if isinstance(gist_result, str):
            return gist_result
        assert isinstance(gist_result, dict)

        gist_desc = gist_result.get("description") or "Untitled gist"
        files = gist_result.get("files", {})

        fm = _build_frontmatter({
            "source": url,
            "api": "GitHub",
            "trust": _TRUST_ADVISORY,
        })

        parts = []
        for filename, fdata in files.items():
            lang = fdata.get("language", "").lower()
            content = fdata.get("content", "")
            parts.append(f"## {filename}\n")
            parts.append(f"```{lang}\n{content}\n```\n")

        body = "\n".join(parts)
        _page_cache.store(url, gist_desc, body, renderer="github")
        return fm + "\n\n" + _fence_content(body, title=gist_desc)

    # --- Org/user profile ---
    if match.kind == "org":
        from .github import _github_request

        org_result = await _github_request("GET", f"/orgs/{match.owner}")
        # Fall back to user endpoint if orgs/ returns error (personal accounts)
        is_user = False
        if isinstance(org_result, str):
            org_result = await _github_request("GET", f"/users/{match.owner}")
            if isinstance(org_result, str):
                return org_result
            is_user = True
        assert isinstance(org_result, dict)

        repos_result = await _github_request(
            "GET",
            f"/{'users' if is_user else 'orgs'}/{match.owner}/repos",
            params={"sort": "pushed", "per_page": "30"},
        )
        repos = repos_result if isinstance(repos_result, list) else []

        name = org_result.get("name") or match.owner
        desc = org_result.get("description") or org_result.get("bio") or ""
        public_repos = org_result.get("public_repos", 0)
        profile_type = "User" if is_user else "Organization"

        parts = [f"**{name}** — {profile_type}"]
        if desc:
            parts.append(f"\n{desc}")
        parts.append(f"\nPublic repositories: {public_repos}")
        parts.append("")

        if repos:
            parts.append("### Recently active repositories")
            parts.append("")
            for r in repos:
                stars = r.get("stargazers_count", 0)
                lang = r.get("language") or ""
                rdesc = r.get("description") or ""
                star_str = f" | {stars:,} stars" if stars else ""
                lang_str = f" | {lang}" if lang else ""
                line = f"- **{r['name']}**{lang_str}{star_str}"
                if rdesc:
                    line += f" — {rdesc}"
                parts.append(line)

        fm = _build_frontmatter({
            "source": f"https://github.com/{match.owner}",
            "api": "GitHub",
            "type": profile_type.lower(),
            "trust": _TRUST_ADVISORY,
        })
        return fm + "\n\n" + _fence_content("\n".join(parts), title=name)

    # --- Wiki page ---
    if match.kind == "wiki":
        page_name = match.path or "Home"
        raw_url = (
            f"https://raw.githubusercontent.com/wiki/"
            f"{match.owner}/{match.repo}/{page_name}.md"
        )
        headers = dict(_FETCH_HEADERS)
        token = _get_github_token()
        if token:
            headers["Authorization"] = f"token {token}"

        try:
            async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as client:
                resp = await client.get(raw_url, headers=headers)
        except httpx.RequestError:
            return f"Error: Failed to fetch wiki page '{page_name}'."

        if resp.status_code == 404:
            if page_name == "Home":
                return (
                    f"Error: This repository does not have a wiki. "
                    f"({match.owner}/{match.repo})"
                )
            return f"Error: Wiki page '{page_name}' not found in {match.owner}/{match.repo} wiki."
        if resp.status_code != 200:
            return f"Error: HTTP {resp.status_code} fetching wiki page '{page_name}'."

        wiki_md = resp.text
        wiki_url = f"https://github.com/{match.owner}/{match.repo}/wiki/{page_name}"

        # Cache for search/slices
        _page_cache.store(url, page_name, wiki_md, renderer="github")

        content, trunc_hint = _apply_semantic_truncation(wiki_md, max_tokens)
        fm_entries: dict[str, object] = {
            "source": wiki_url,
            "api": "GitHub (wiki)",
            "trust": _TRUST_ADVISORY,
        }
        if trunc_hint:
            fm_entries["truncated"] = trunc_hint
        rl_warn = _rate_limit_warning()
        if rl_warn:
            fm_entries["warning"] = rl_warn
        fm = _build_frontmatter(fm_entries)
        return fm + "\n\n" + _fence_content(content, title=page_name)

    # --- Commit ---
    if match.kind == "commit" and match.ref:
        from .github import _github_request

        result = await _github_request(
            "GET", f"/repos/{match.owner}/{match.repo}/commits/{match.ref}",
        )
        if isinstance(result, str):
            return result
        assert isinstance(result, dict)

        sha = result.get("sha", match.ref)
        commit = result.get("commit", {})
        message = commit.get("message", "")
        author_info = commit.get("author", {})
        author_name = author_info.get("name", "Unknown")
        author_date = author_info.get("date", "")
        stats = result.get("stats", {})
        files = result.get("files", [])

        parts = [f"**{sha[:10]}** by {author_name} — {author_date}", ""]
        parts.append(message)
        parts.append("")

        if stats:
            parts.append(
                f"**{stats.get('total', 0)} changes**: "
                f"+{stats.get('additions', 0)} / -{stats.get('deletions', 0)} "
                f"across {len(files)} file{'s' if len(files) != 1 else ''}"
            )
            parts.append("")

        for f in files[:50]:
            status_icon = {"added": "+", "removed": "-", "modified": "~"}.get(
                f.get("status", ""), "?"
            )
            parts.append(f"  {status_icon} {f.get('filename', '?')} (+{f.get('additions', 0)}/-{f.get('deletions', 0)})")

        if len(files) > 50:
            parts.append(f"  ... and {len(files) - 50} more files")

        title = message.split("\n")[0][:80] if message else sha[:10]
        fm = _build_frontmatter({
            "source": f"https://github.com/{match.owner}/{match.repo}/commit/{sha}",
            "api": "GitHub",
            "type": "commit",
            "trust": _TRUST_ADVISORY,
        })
        return fm + "\n\n" + _fence_content("\n".join(parts), title=title)

    # --- Compare ---
    if match.kind == "compare" and match.path:
        from .github import _github_request

        result = await _github_request(
            "GET", f"/repos/{match.owner}/{match.repo}/compare/{match.path}",
        )
        if isinstance(result, str):
            return result
        assert isinstance(result, dict)

        base_label = result.get("base_commit", {}).get("sha", "?")[:10]
        head_label = result.get("commits", [{}])[-1].get("sha", "?")[:10] if result.get("commits") else "?"
        commits = result.get("commits", [])
        files = result.get("files", [])
        status = result.get("status", "")  # ahead, behind, diverged, identical

        parts = [f"**{status}** — {len(commits)} commit{'s' if len(commits) != 1 else ''}, {len(files)} file{'s' if len(files) != 1 else ''} changed"]
        parts.append(f"{base_label} → {head_label}")
        parts.append("")

        if commits:
            parts.append("### Commits")
            parts.append("")
            for c in commits[:30]:
                sha_short = c.get("sha", "?")[:10]
                msg = c.get("commit", {}).get("message", "").split("\n")[0]
                parts.append(f"- `{sha_short}` {msg}")
            if len(commits) > 30:
                parts.append(f"- ... and {len(commits) - 30} more")
            parts.append("")

        if files:
            parts.append("### Files changed")
            parts.append("")
            for f in files[:50]:
                status_icon = {"added": "+", "removed": "-", "modified": "~"}.get(
                    f.get("status", ""), "?"
                )
                parts.append(f"  {status_icon} {f.get('filename', '?')} (+{f.get('additions', 0)}/-{f.get('deletions', 0)})")
            if len(files) > 50:
                parts.append(f"  ... and {len(files) - 50} more files")

        fm = _build_frontmatter({
            "source": f"https://github.com/{match.owner}/{match.repo}/compare/{match.path}",
            "api": "GitHub",
            "type": "compare",
            "status": status,
            "trust": _TRUST_ADVISORY,
        })
        return fm + "\n\n" + _fence_content("\n".join(parts), title=match.path)

    # --- Releases ---
    if match.kind == "releases":
        from .github import _github_request

        # Check if this is a specific tag release
        rest = match.path
        if rest and rest.startswith("tag/"):
            tag = rest[4:]
            result = await _github_request(
                "GET",
                f"/repos/{match.owner}/{match.repo}/releases/tags/{tag}",
            )
            if isinstance(result, str):
                return result
            assert isinstance(result, dict)

            name = result.get("name") or tag
            body = result.get("body") or ""
            published = result.get("published_at", "")
            author = result.get("author", {}).get("login", "")
            prerelease = result.get("prerelease", False)
            assets = result.get("assets", [])

            parts = [f"**{name}** ({tag})"]
            if author:
                parts[0] += f" by @{author}"
            if published:
                parts[0] += f" — {published}"
            if prerelease:
                parts.append("*Pre-release*")
            parts.append("")
            if body:
                parts.append(body)
                parts.append("")
            if assets:
                parts.append("### Assets")
                parts.append("")
                for a in assets:
                    size_mb = a.get("size", 0) / (1024 * 1024)
                    dl = a.get("download_count", 0)
                    parts.append(f"- {a['name']} ({size_mb:.1f} MB, {dl:,} downloads)")

            fm = _build_frontmatter({
                "source": f"https://github.com/{match.owner}/{match.repo}/releases/tag/{tag}",
                "api": "GitHub",
                "type": "release",
                "trust": _TRUST_ADVISORY,
            })
            return fm + "\n\n" + _fence_content("\n".join(parts), title=name)

        # List recent releases
        result = await _github_request(
            "GET",
            f"/repos/{match.owner}/{match.repo}/releases",
            params={"per_page": "20"},
        )
        if isinstance(result, str):
            return result
        assert isinstance(result, list)

        if not result:
            return f"Error: No releases found for {match.owner}/{match.repo}."

        parts = []
        for r in result:
            tag = r.get("tag_name", "?")
            name = r.get("name") or tag
            published = r.get("published_at", "")[:10]
            prerelease = " (pre-release)" if r.get("prerelease") else ""
            parts.append(f"- **{name}** `{tag}` — {published}{prerelease}")

        fm = _build_frontmatter({
            "source": f"https://github.com/{match.owner}/{match.repo}/releases",
            "api": "GitHub",
            "type": "releases",
            "hint": "Use WebFetchDirect with a specific release tag URL for full release notes and assets",
            "trust": _TRUST_ADVISORY,
        })
        return fm + "\n\n" + _fence_content("\n".join(parts), title=f"{match.owner}/{match.repo} releases")

    # --- Unsupported paths — clean errors instead of broken HTML scrapes ---
    _UNSUPPORTED_MESSAGES = {
        "blame": "Blame view is not available via API. Use the blob URL for file content.",
        "actions": "Actions workflows are not content-oriented. Use the GitHub web UI or `gh run list`.",
        "projects": "Projects boards are not yet supported. Use the GitHub web UI.",
    }
    if match.kind in _UNSUPPORTED_MESSAGES:
        return f"Error: {_UNSUPPORTED_MESSAGES[match.kind]}"

    # Matched but unhandled kind
    return f"Error: GitHub URL type '{match.kind}' is not yet supported."


# ---------------------------------------------------------------------------
# Shared post-processing
# ---------------------------------------------------------------------------

def _process_markdown_sections(
    markdown_content: str,
    section_names: Optional[list[str]],
    max_tokens: int,
    frontmatter_entries: dict,
    title: Optional[str] = None,
    cache_url: Optional[str] = None,
    renderer: Optional[str] = None,
) -> str:
    """Apply section filtering, truncation, and frontmatter to markdown content.

    Common post-processing for both browser-rendered and httpx-fetched HTML.
    Returns the complete formatted output string.

    Args:
        title: Page title for display inside the content fence (untrusted).
            Kept separate from frontmatter_entries because it is untrusted
            content that must never appear in server-generated metadata.
        cache_url: When provided, populates the page cache with the full
            (pre-filtered) markdown so subsequent search/slices calls can
            use it.
        renderer: Tag stored with the cache entry ("direct" or "js") so
            that WebFetchJS won't reuse sparse content cached by
            WebFetchDirect.
    """
    # Populate the page cache before any filtering/truncation
    if cache_url and markdown_content:
        _page_cache.store(cache_url, title or "Untitled", markdown_content, renderer=renderer)

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

    frontmatter_entries["trust"] = _TRUST_ADVISORY
    frontmatter_entries["truncated"] = truncation_hint
    fm = _build_frontmatter(
        frontmatter_entries,
        sections_not_found=sections_not_found,
    )

    # Append section metadata to fenced content (untrusted heading names)
    body_parts = [markdown_content]
    if sections_available:
        body_parts.append("\n\nSections:\n" + "\n".join(sections_available))
    fenced = _fence_content("\n".join(body_parts) if len(body_parts) > 1 else markdown_content, title=title)
    return fm + "\n\n" + fenced


# ---------------------------------------------------------------------------
# Slice output, search, and retrieval
# ---------------------------------------------------------------------------

def _slice_output(
    cache: _CacheEntry,
    indices: list[int],
    max_tokens: int,
    fm_entries: dict,
    title: Optional[str] = None,
    search_term: Optional[str] = None,
) -> str:
    """Assemble sliced output with YAML frontmatter and --- dividers.

    Each slice is preceded by a ``--- slice N (Ancestry) ---`` header.
    Respects max_tokens budget — stops emitting slices when exhausted.

    Args:
        title: Page title for display inside the content fence (untrusted).
    """
    assert cache.slices is not None
    assert cache.slice_ancestry is not None

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
    title: Optional[str] = None,
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

    return _slice_output(cached, matched, max_tokens, fm_entries, title=title, search_term=search)


def _get_slices(
    url: str,
    indices: list[int],
    max_tokens: int,
    fm_entries: dict,
    title: Optional[str] = None,
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

    return _slice_output(cached, valid, max_tokens, fm_entries, title=title)


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
    # Reddit markdown already embeds "# {title}" as slice 0's first line
    # (kept so slice ancestry remains informative) — skip re-adding it via
    # _fence_content to avoid a duplicated title heading.
    title = None if cached.renderer == "reddit" else cached.title
    fm_base = {"source": source_url, "warning": warning}
    if search is not None:
        return _search_slices(url, search, max_tokens, fm_base, title=title) or \
            "Error: Page cache unavailable."
    else:
        return _get_slices(url, slices_list, max_tokens, fm_base, title=title) or \
            "Error: Page cache unavailable."
