"""Discourse forum integration — fetches topics and search results via JSON API.

Detects Discourse instances via the ``x-discourse-route`` response header
(present on all Discourse responses, hosted and self-hosted).  Fetches
structured JSON with raw author markdown via ``include_raw=true``, avoiding
HTML→markdown conversion entirely.

Supports topic threads (with batch post fetching for >20 posts), search,
and latest-topics listings.
"""

import contextlib
import logging
import re
from collections import defaultdict
from collections.abc import Mapping
from datetime import datetime
from typing import Annotated
from urllib.parse import urlparse, urlunparse

import httpx
from pydantic import Field

from .common import (
    _API_HEADERS,
    RateLimiter,
    check_url_scheme,
    guarded_client,
    proxy_warning,
    tool_name,
)
from .markdown import _TRUST_ADVISORY, _build_frontmatter, _fence_content

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Per-host rate limiting
# ---------------------------------------------------------------------------

_discourse_limiters: dict[str, RateLimiter] = {}
_DEFAULT_DISCOURSE_INTERVAL = 1.0


def _get_limiter(hostname: str) -> RateLimiter:
    """Get or create a rate limiter for a Discourse host."""
    if hostname not in _discourse_limiters:
        _discourse_limiters[hostname] = RateLimiter(_DEFAULT_DISCOURSE_INTERVAL)
    return _discourse_limiters[hostname]


# ---------------------------------------------------------------------------
# Detection
# ---------------------------------------------------------------------------

_DISCOURSE_ROUTE_HEADER = "x-discourse-route"


def _detect_discourse_headers(headers: Mapping[str, str]) -> str | None:
    """Check for Discourse ``x-discourse-route`` header.

    Returns the route value (e.g. ``topics/show``, ``list/latest``) or None.
    """
    return headers.get(_DISCOURSE_ROUTE_HEADER)


# /t/slug/NNN, /t/NNN, /t/slug/NNN/post_number, /t/NNN/post_number
_TOPIC_ID_RE = re.compile(r"/t/(?:[^/]*/)?(\d+)(?:/\d+)?/?$")


def _extract_topic_id(url: str) -> int | None:
    """Extract topic ID from a Discourse topic URL.

    Handles ``/t/slug/12345``, ``/t/12345``, ``/t/slug/12345/3``.
    """
    parsed = urlparse(url)
    m = _TOPIC_ID_RE.search(parsed.path)
    if m:
        return int(m.group(1))
    return None


def _base_url_from(url: str) -> str:
    """Reduce any Discourse URL to the instance root that serves it.

    Accepts a topic URL or an instance root and returns the root either
    way.  A trailing ``/t/...`` segment is removed, since that is what
    distinguishes the two; on a root URL the removal is a no-op.  Any
    remaining path prefix is kept, so subfolder installs survive:
    ``https://example.com/forum/t/slug/1`` becomes ``.../forum``.

    Params, query, and fragment are dropped, and that matters beyond
    tidiness.  Endpoint paths are appended to this value as text, so a base
    ending in ``#`` or ``?`` swallows the appended path into a fragment or
    query that is never sent as a path, leaving a caller in control of the
    whole request path rather than the few Discourse endpoints below.
    """
    parsed = urlparse(url)
    return urlunparse((
        parsed.scheme, parsed.netloc,
        _TOPIC_ID_RE.sub("", parsed.path).rstrip("/"), "", "", "",
    ))


def _resolve_base(raw: str) -> tuple[str, str] | str:
    """Normalize a caller-supplied forum URL to ``(base_url, hostname)``.

    Returns an error string instead when the URL cannot be used.
    """
    base = _base_url_from(raw)
    scheme_error = check_url_scheme(base)
    if scheme_error:
        return scheme_error
    hostname = urlparse(base).hostname
    if not hostname:
        return f"Error: Could not determine a forum host from {raw!r}."
    return base, hostname


# ---------------------------------------------------------------------------
# JSON fetching
# ---------------------------------------------------------------------------

async def _discourse_get(
    url: str, hostname: str, params: dict | None = None,
) -> httpx.Response:
    """Rate-limited GET request to a Discourse endpoint."""
    await _get_limiter(hostname).wait()
    # guarded_client, not a bare AsyncClient: the host here comes from the
    # caller, so the destination needs the same address check the generic
    # fetch path applies.
    async with guarded_client(follow_redirects=True, timeout=30.0) as client:
        resp = await client.get(url, headers=_API_HEADERS, params=params)
        resp.raise_for_status()
        return resp


async def _fetch_topic(
    base_url: str, topic_id: int, hostname: str,
) -> dict | str:
    """Fetch ``/t/{id}.json?include_raw=true``.

    Returns parsed topic JSON or an error string.
    """
    url = f"{base_url}/t/{topic_id}.json"
    try:
        resp = await _discourse_get(url, hostname, params={"include_raw": "true"})
        return resp.json()
    except httpx.HTTPStatusError as e:
        return f"Error: HTTP {e.response.status_code} fetching topic {topic_id}"
    except httpx.RequestError as e:
        return f"Error: {type(e).__name__} fetching topic {topic_id}"
    except ValueError:
        return f"Error: Invalid JSON from {url}"


async def _fetch_remaining_posts(
    base_url: str, topic_id: int, post_ids: list[int], hostname: str,
) -> list[dict] | str:
    """Batch-fetch posts via ``/t/{id}/posts.json?post_ids[]=...``.

    Returns list of post dicts or an error string.
    """
    # Build URL manually — httpx doesn't natively handle repeated params
    url = f"{base_url}/t/{topic_id}/posts.json"
    id_params = "&".join(f"post_ids[]={pid}" for pid in post_ids)
    full_url = f"{url}?include_raw=true&{id_params}"
    try:
        resp = await _discourse_get(full_url, hostname)
        data = resp.json()
        return data.get("post_stream", {}).get("posts", [])
    except httpx.HTTPStatusError as e:
        return f"Error: HTTP {e.response.status_code} fetching posts for topic {topic_id}"
    except httpx.RequestError as e:
        return f"Error: {type(e).__name__} fetching posts for topic {topic_id}"
    except ValueError:
        return "Error: Invalid JSON from posts endpoint"


async def _fetch_search(
    base_url: str, query: str, hostname: str,
) -> dict | str:
    """Fetch ``/search.json?q=...``."""
    url = f"{base_url}/search.json"
    try:
        resp = await _discourse_get(url, hostname, params={"q": query})
        return resp.json()
    except httpx.HTTPStatusError as e:
        return f"Error: HTTP {e.response.status_code} searching"
    except httpx.RequestError as e:
        return f"Error: {type(e).__name__} during search"
    except ValueError:
        return "Error: Invalid JSON from search endpoint"


async def _fetch_latest(
    base_url: str, hostname: str,
) -> dict | str:
    """Fetch ``/latest.json``."""
    url = f"{base_url}/latest.json"
    try:
        resp = await _discourse_get(url, hostname)
        return resp.json()
    except httpx.HTTPStatusError as e:
        return f"Error: HTTP {e.response.status_code} fetching latest topics"
    except httpx.RequestError as e:
        return f"Error: {type(e).__name__} fetching latest topics"
    except ValueError:
        return "Error: Invalid JSON from latest endpoint"


# ---------------------------------------------------------------------------
# Raw markdown cleaning
# ---------------------------------------------------------------------------

# [quote="username, post:N, topic:T, full:true"]...[/quote]
_QUOTE_OPEN_RE = re.compile(
    r'\[quote="([^"]*)"]\s*',
    re.IGNORECASE,
)
_QUOTE_CLOSE_RE = re.compile(r"\[/quote\]\s*", re.IGNORECASE)

# upload://identifier or upload://identifier.ext
_UPLOAD_RE = re.compile(r"!\[([^\]]*)\]\(upload://[^)]+\)")

# <div data-theme-toc="true"> </div> and similar
_TOC_DIV_RE = re.compile(r'<div[^>]*data-theme-toc[^>]*>.*?</div>', re.DOTALL)

# Image sizing hints in alt text: |NNNxNNN or |NNN
_IMAGE_SIZE_RE = re.compile(r"\|(\d+x\d+|\d+)(, \d+%)?(?=\])")


def _parse_quote_attr(attr_str: str) -> tuple[str, int | None]:
    """Parse ``username, post:N, topic:T`` into (username, post_number)."""
    parts = [p.strip() for p in attr_str.split(",")]
    username = parts[0] if parts else "unknown"
    post_num = None
    for part in parts[1:]:
        if part.startswith("post:"):
            with contextlib.suppress(ValueError):
                post_num = int(part[5:])
    return username, post_num


def _clean_raw(raw: str) -> str:
    """Preprocess Discourse raw markdown for display.

    - Converts ``[quote]`` BBCode to markdown blockquotes
    - Replaces ``upload://`` image refs with ``[image]``
    - Strips TOC div markers
    - Removes image sizing hints from alt text
    """
    # Strip TOC divs
    text = _TOC_DIV_RE.sub("", raw)

    # Convert quote blocks to blockquotes
    def _replace_quote_open(m: re.Match) -> str:
        username, post_num = _parse_quote_attr(m.group(1))
        if post_num is not None:
            return f"> **@{username}** (post #{post_num}):\n> "
        return f"> **@{username}**:\n> "

    text = _QUOTE_OPEN_RE.sub(_replace_quote_open, text)
    text = _QUOTE_CLOSE_RE.sub("\n", text)

    # Indent quoted content — lines between quote open and close should be >-prefixed.
    # The regex replacement above handles the opener; content lines within a quote
    # block are already inline.  For multi-line quotes, ensure continuation lines
    # get the > prefix.  This is a best-effort heuristic.

    # Replace upload:// image refs
    text = _UPLOAD_RE.sub("[image]", text)

    # Strip image sizing hints from alt text
    text = _IMAGE_SIZE_RE.sub("", text)

    return text.strip()


# ---------------------------------------------------------------------------
# Timestamp formatting
# ---------------------------------------------------------------------------

def _format_timestamp(iso_str: str) -> str:
    """Convert ISO 8601 timestamp to human-readable UTC string."""
    try:
        dt = datetime.fromisoformat(iso_str)
        return dt.strftime("%Y-%m-%d %H:%M UTC")
    except (ValueError, AttributeError):
        return iso_str


def _format_relative_time(post_iso: str, topic_iso: str) -> str:
    """Format post time as T+HH:MM:SS relative to topic creation."""
    try:
        post_dt = datetime.fromisoformat(post_iso)
        topic_dt = datetime.fromisoformat(topic_iso)
        delta = max(0, int((post_dt - topic_dt).total_seconds()))
        hours, remainder = divmod(delta, 3600)
        minutes, seconds = divmod(remainder, 60)
    except (ValueError, AttributeError):
        return ""
    else:
        return f"T+{hours:02d}:{minutes:02d}:{seconds:02d}"


# ---------------------------------------------------------------------------
# Content assembly
# ---------------------------------------------------------------------------

async def _fetch_discourse_content(url: str) -> tuple[str, str]:
    """Fetch and format a Discourse topic as markdown.

    Two-request strategy:
    1. ``GET /t/{id}.json?include_raw=true`` — metadata + first 20 posts
    2. If >20 posts, batch-fetch remaining via ``post_ids[]``

    Returns ``(title, full_markdown)`` — same contract as ``_fetch_reddit_content``.
    """
    topic_id = _extract_topic_id(url)
    if topic_id is None:
        return "Discourse", f"Error: Could not extract topic ID from {url}"

    base_url = _base_url_from(url)
    hostname = urlparse(url).hostname or ""

    data = await _fetch_topic(base_url, topic_id, hostname)
    if isinstance(data, str):
        return "Discourse", data

    # Collect all posts — first page is inline
    ps = data.get("post_stream", {})
    stream = ps.get("stream", [])
    posts = list(ps.get("posts", []))
    inline_ids = {p["id"] for p in posts}

    # Fetch remaining posts if needed
    remaining_ids = [pid for pid in stream if pid not in inline_ids]
    if remaining_ids:
        extra = await _fetch_remaining_posts(base_url, topic_id, remaining_ids, hostname)
        if isinstance(extra, str):
            logger.warning("Failed to fetch remaining posts: %s", extra)
        else:
            posts.extend(extra)

    # Sort by post_number to ensure correct order
    posts.sort(key=lambda p: p.get("post_number", 0))

    return _format_topic(data, posts)


def _format_topic(data: dict, all_posts: list[dict]) -> tuple[str, str]:
    """Format a Discourse topic with all posts as markdown.

    Returns ``(title, markdown)``.
    """
    title = data.get("title", "Untitled")
    posts_count = data.get("posts_count", len(all_posts))
    views = data.get("views", 0)
    created = data.get("created_at", "")

    parts: list[str] = []

    # Header
    parts.append(f"# {title}\n")
    meta = [
        f"{posts_count} posts",
        f"{views} views",
        _format_timestamp(created),
    ]
    # Discourse's `tags` field is a list of {id, name, slug} dicts on modern
    # instances; older instances returned bare strings. Accept both.
    tags_raw = data.get("tags") or []
    tag_names = [
        t if isinstance(t, str) else t.get("name", "")
        for t in tags_raw
    ]
    tag_names = [n for n in tag_names if n]
    if tag_names:
        meta.append("tags: " + ", ".join(tag_names))
    parts.append(" | ".join(meta) + "\n")

    # Mega-topic warning: topics with >=10000 posts omit `post_stream.stream`
    # entirely and emit `isMegaTopic: true, lastId: <int>` instead. We can't
    # batch-fetch the remaining posts, so only the inline ~20 are included.
    if data.get("post_stream", {}).get("isMegaTopic"):
        parts.append(
            f"> **Note:** This is a mega-topic ({posts_count} posts total). "
            f"Only the first {len(all_posts)} posts are shown — Discourse "
            f"does not expose the full post stream for very large topics.\n"
        )

    # Posts
    for post in all_posts:
        post_num = post.get("post_number", 0)
        username = post.get("username", "unknown")
        post_created = post.get("created_at", "")
        reply_to = post.get("reply_to_post_number")
        raw = post.get("raw", "")

        parts.append(f"### {post_num}\n")

        # Metadata line
        meta_parts = [f"**@{username}**"]
        if reply_to:
            meta_parts.append(f"reply to #{reply_to}")
        meta_parts.append(_format_timestamp(post_created))
        parts.append(" — ".join(meta_parts) + "\n")

        # Body
        if raw:
            parts.append(_clean_raw(raw) + "\n")

    return title, "\n".join(parts)


# ---------------------------------------------------------------------------
# Post-aware splitting for BM25 indexing
# ---------------------------------------------------------------------------

# Matches post headings: ### N at start of line
_POST_HEADING_RE = re.compile(r"^### \d+$", re.MULTILINE)


def _split_by_posts(markdown: str) -> list[tuple[int, str]]:
    """Split formatted topic markdown into per-post chunks.

    The topic header (everything before the first ``### N`` heading)
    becomes slice 0.  Each subsequent post heading and its content
    becomes its own slice.

    Returns ``[(char_offset, chunk_text), ...]`` suitable for
    ``_PageCache.store(presplit=...)``.
    """
    splits = list(_POST_HEADING_RE.finditer(markdown))

    if not splits:
        return [(0, markdown)]

    chunks: list[tuple[int, str]] = []

    # Chunk 0: topic header before first post
    first_offset = splits[0].start()
    if first_offset > 0:
        chunks.append((0, markdown[:first_offset].rstrip()))

    # Each post heading → next heading boundary
    for i, match in enumerate(splits):
        start = match.start()
        end = splits[i + 1].start() if i + 1 < len(splits) else len(markdown)
        chunks.append((start, markdown[start:end].rstrip()))

    return chunks


# ---------------------------------------------------------------------------
# Section tree for web_fetch_sections
# ---------------------------------------------------------------------------

def _build_post_section_tree(
    data: dict, all_posts: list[dict],
) -> tuple[str, str]:
    """Build an indented section listing of posts.

    Returns ``(title, section_body)`` with lines like::

        - #1 — @username (234 chars, T+00:00:00)
          - #2 — @other (reply to #1, 456 chars, T+00:15:00)

    Uses ``reply_to_post_number`` to reconstruct threading.
    """
    title = data.get("title", "Untitled")
    topic_created = data.get("created_at", "")

    lines: list[str] = [f"# {title} ({_format_timestamp(topic_created)})\n"]

    # Build reply tree: parent_post_number → [child posts]
    children_map: dict[int, list[dict]] = defaultdict(list)
    root_posts: list[dict] = []

    for post in all_posts:
        reply_to = post.get("reply_to_post_number")
        if reply_to:
            children_map[reply_to].append(post)
        else:
            root_posts.append(post)

    def _walk(post: dict, depth: int) -> None:
        post_num = post.get("post_number", 0)
        username = post.get("username", "unknown")
        raw = post.get("raw", "")
        char_len = len(raw)
        post_created = post.get("created_at", "")
        reply_to = post.get("reply_to_post_number")
        reltime = _format_relative_time(post_created, topic_created)

        indent = "  " * depth
        reply_str = f"reply to #{reply_to}, " if reply_to else ""
        time_str = f", {reltime}" if reltime else ""
        lines.append(
            f"{indent}- #{post_num} — @{username} ({reply_str}{char_len} chars{time_str})"
        )

        for child in children_map.get(post_num, []):
            _walk(child, depth + 1)

    for post in root_posts:
        _walk(post, 0)

    return title, "\n".join(lines)


# ---------------------------------------------------------------------------
# Search and latest formatting
# ---------------------------------------------------------------------------

def _format_search_results(data: dict, base_url: str, limit: int = 10) -> str:
    """Format Discourse search results as markdown."""
    posts = data.get("posts", [])[:limit]
    topics = data.get("topics", [])

    if not posts and not topics:
        return "No results found."

    parts: list[str] = []

    if posts:
        # Build topic_id → topic info map for enrichment.  SearchPostSerializer
        # does NOT emit topic_title — the title must come from the parallel
        # topics[] array.
        topic_map = {t["id"]: t for t in topics}

        for i, post in enumerate(posts, 1):
            topic_id = post.get("topic_id", 0)
            topic_info = topic_map.get(topic_id, {})
            topic_title = topic_info.get("title", "Untitled")
            username = post.get("username", "unknown")
            post_num = post.get("post_number", 1)
            blurb = post.get("blurb", "")
            reply_count = topic_info.get("reply_count", 0)

            parts.append(f"{i}. **{topic_title}**")
            parts.append(f"   @{username} (post #{post_num}) | {reply_count} replies")
            parts.append(f"   {base_url}/t/{topic_id}/{post_num}")
            if blurb:
                parts.append(f"   {blurb[:200]}")

    return "\n".join(parts)


def _format_latest(data: dict, base_url: str, limit: int = 10) -> str:
    """Format Discourse latest topics listing as markdown."""
    topics = data.get("topic_list", {}).get("topics", [])[:limit]

    if not topics:
        return "No topics found."

    parts: list[str] = []
    for i, topic in enumerate(topics, 1):
        tid = topic.get("id", 0)
        title = topic.get("title", "Untitled")
        posts_count = topic.get("posts_count", 0)
        views = topic.get("views", 0)
        reply_count = topic.get("reply_count", 0)
        created = topic.get("created_at", "")

        parts.append(f"{i}. **{title}**")
        parts.append(
            f"   {posts_count} posts, {reply_count} replies, {views} views — {_format_timestamp(created)}"
        )
        parts.append(f"   {base_url}/t/{tid}")

    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Standalone MCP tool
# ---------------------------------------------------------------------------

async def discourse(
    action: Annotated[str, Field(
        description=(
            "The operation to perform. "
            "topic: fetch a Discourse topic with all posts. "
            "search: search a Discourse forum. "
            "latest: browse the latest topics on a forum."
        ),
    )],
    query: Annotated[str, Field(
        description=(
            "For topic: full topic URL (e.g. 'https://meta.discourse.org/t/topic-slug/12345'). "
            "For search: search query string. "
            "For latest: ignored (use base_url to identify the forum)."
        ),
    )],
    base_url: Annotated[str | None, Field(
        description=(
            "Base URL of the Discourse instance (e.g. 'https://meta.discourse.org'). "
            "Required for search and latest actions. For topic, inferred from the query URL."
        ),
    )] = None,
    limit: Annotated[int, Field(
        description="Maximum results for search/latest (default 10).",
    )] = 10,
) -> str:
    """Search and browse Discourse forum topics."""

    if action == "topic":
        topic_id = _extract_topic_id(query)
        if topic_id is None:
            return "Error: Could not extract topic ID from the provided URL."

        resolved = _resolve_base(base_url or query)
        if isinstance(resolved, str):
            return resolved
        effective_base, hostname = resolved

        data = await _fetch_topic(effective_base, topic_id, hostname)
        if isinstance(data, str):
            return data

        # Collect all posts
        ps = data.get("post_stream", {})
        stream = ps.get("stream", [])
        posts = list(ps.get("posts", []))
        inline_ids = {p["id"] for p in posts}

        remaining_ids = [pid for pid in stream if pid not in inline_ids]
        if remaining_ids:
            extra = await _fetch_remaining_posts(effective_base, topic_id, remaining_ids, hostname)
            if isinstance(extra, list):
                posts.extend(extra)

        posts.sort(key=lambda p: p.get("post_number", 0))
        title, full_md = _format_topic(data, posts)

        fm = _build_frontmatter({
            "title": title,
            # Built from the base actually fetched, not from `query`.  When
            # base_url and query disagree it is base_url that chooses the
            # destination, so echoing `query` would attribute the content to
            # a host it never came from.
            "source": f"{effective_base}/t/{topic_id}",
            "api": "Discourse",
            "trust": _TRUST_ADVISORY,
            "warning": proxy_warning(),
            "posts": data.get("posts_count", len(posts)),
            "hint": f"Use {tool_name('web_fetch_direct')} with section=N to extract a specific post, "
                    "or search= for keyword search across posts",
        })
        return fm + "\n\n" + _fence_content(full_md, title=None)

    if action == "search":
        if not base_url:
            return "Error: base_url is required for search action."
        resolved = _resolve_base(base_url)
        if isinstance(resolved, str):
            return resolved
        effective_base, hostname = resolved

        data = await _fetch_search(effective_base, query, hostname)
        if isinstance(data, str):
            return data

        result = _format_search_results(data, effective_base, limit=limit)
        fm = _build_frontmatter({
            "api": "Discourse",
            "action": "search",
            "query": query,
            "source": effective_base,
            "trust": _TRUST_ADVISORY,
            "warning": proxy_warning(),
        })
        return fm + "\n\n" + _fence_content(result)

    if action == "latest":
        if not base_url:
            return "Error: base_url is required for latest action."
        resolved = _resolve_base(base_url)
        if isinstance(resolved, str):
            return resolved
        effective_base, hostname = resolved

        data = await _fetch_latest(effective_base, hostname)
        if isinstance(data, str):
            return data

        result = _format_latest(data, effective_base, limit=limit)
        fm = _build_frontmatter({
            "api": "Discourse",
            "action": "latest",
            "source": effective_base,
            "trust": _TRUST_ADVISORY,
            "warning": proxy_warning(),
        })
        return fm + "\n\n" + _fence_content(result)

    return (
        f"Error: Unknown action '{action}'. "
        "Valid actions: topic, search, latest"
    )
