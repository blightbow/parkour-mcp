"""Reddit fast path — fetches Reddit pages via old.reddit.com .json endpoint.

Rewrites any reddit.com URL to old.reddit.com and appends .json to get
structured JSON data without OAuth, API keys, or approval.  This bypasses
Reddit's login/age walls on www.reddit.com and avoids their monetised API.

Supports comment threads, subreddit listings, and user pages.
"""

import logging
import re
from datetime import datetime, timezone
from enum import Enum
from typing import Optional, Union
from urllib.parse import urlparse, urlunparse, urlencode, parse_qs

import httpx

from .common import RateLimiter, _FETCH_HEADERS

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Rate limiter — 2s between unauthenticated requests
# ---------------------------------------------------------------------------

_reddit_limiter = RateLimiter(2.0)

# ---------------------------------------------------------------------------
# URL detection
# ---------------------------------------------------------------------------

_REDDIT_URL_RE = re.compile(
    r"https?://(?:(?:www|old|new|np)\.)?reddit\.com/",
    re.IGNORECASE,
)

_REDD_IT_RE = re.compile(
    r"https?://redd\.it/(\w+)",
    re.IGNORECASE,
)

_MAX_COMMENT_DEPTH = 6


def _detect_reddit_url(url: str) -> Optional[str]:
    """Return normalised old.reddit.com URL if *url* is a Reddit link, else None.

    Rewrites the host to old.reddit.com, preserves ``sort`` query param,
    strips everything else.  Does NOT append ``.json`` — the fetch function
    does that.  For ``redd.it`` short links the original URL is returned
    (redirect resolved during fetch).
    """
    # Short links
    if _REDD_IT_RE.match(url):
        return url

    if not _REDDIT_URL_RE.match(url):
        return None

    parsed = urlparse(url)

    # Rewrite host
    netloc = "old.reddit.com"

    # Ensure trailing slash on path
    path = parsed.path
    if not path.endswith("/"):
        path += "/"

    # Preserve only ?sort=
    qs = parse_qs(parsed.query)
    keep: dict[str, list[str]] = {}
    if "sort" in qs:
        keep["sort"] = qs["sort"]
    query = urlencode(keep, doseq=True)

    return urlunparse(("https", netloc, path, "", query, ""))


# ---------------------------------------------------------------------------
# Page type classification
# ---------------------------------------------------------------------------

class RedditPageType(Enum):
    COMMENT_THREAD = "comment_thread"
    SUBREDDIT = "subreddit"
    USER = "user"
    SHORT_LINK = "short_link"


_COMMENT_RE = re.compile(r"/r/[^/]+/comments/\w+", re.IGNORECASE)
_USER_RE = re.compile(r"/(?:u|user)/[^/]+", re.IGNORECASE)

def _classify_reddit_url(url: str) -> RedditPageType:
    """Classify a Reddit URL by page type."""
    if _REDD_IT_RE.match(url):
        return RedditPageType.SHORT_LINK

    parsed = urlparse(url)
    path = parsed.path

    if _COMMENT_RE.search(path):
        return RedditPageType.COMMENT_THREAD
    if _USER_RE.search(path):
        return RedditPageType.USER
    return RedditPageType.SUBREDDIT


# ---------------------------------------------------------------------------
# JSON fetch
# ---------------------------------------------------------------------------

async def _resolve_redd_it(url: str) -> Optional[str]:
    """Follow a redd.it short link redirect to get the canonical URL."""
    try:
        await _reddit_limiter.wait()
        async with httpx.AsyncClient(
            follow_redirects=True, timeout=10.0,
        ) as client:
            resp = await client.head(url, headers=_FETCH_HEADERS)
            final = str(resp.url)
            # Normalise the resolved URL
            return _detect_reddit_url(final)
    except Exception as exc:
        logger.debug("redd.it redirect failed for %s: %s", url, exc)
        return None


async def _fetch_reddit_json(url: str) -> Union[list, dict, str]:
    """Fetch the .json endpoint for a Reddit URL.

    Returns parsed JSON (list or dict) on success, or an error string.
    """
    # Append .json
    parsed = urlparse(url)
    path = parsed.path.rstrip("/") + "/.json"
    json_url = urlunparse((
        parsed.scheme, parsed.netloc, path,
        parsed.params, parsed.query, "",
    ))

    await _reddit_limiter.wait()
    try:
        async with httpx.AsyncClient(
            follow_redirects=True, timeout=30.0,
        ) as client:
            resp = await client.get(json_url, headers=_FETCH_HEADERS)
            if resp.status_code == 429:
                return "Error: Reddit rate limit exceeded. Try again later."
            resp.raise_for_status()
            return resp.json()
    except httpx.TimeoutException:
        return f"Error: Request timed out for {url}"
    except httpx.HTTPStatusError as exc:
        return f"Error: HTTP {exc.response.status_code} for {url}"
    except httpx.RequestError as exc:
        return f"Error: Failed to fetch {url} — {type(exc).__name__}"
    except ValueError:
        return f"Error: Invalid JSON response from {url}"


async def _fetch_reddit_content(url: str) -> tuple[str, str]:
    """Fetch and format a Reddit page.

    Returns ``(title, full_markdown)``.  The caller (pipeline) handles
    frontmatter, fencing, truncation, and cache population.

    On any error the title is ``"Reddit"`` and the markdown is the error
    message — this function never raises.
    """
    page_type = _classify_reddit_url(url)

    # Resolve short links first
    if page_type == RedditPageType.SHORT_LINK:
        resolved = await _resolve_redd_it(url)
        if resolved is None:
            return "Reddit", f"Error: Could not resolve short link {url}"
        url = resolved
        page_type = _classify_reddit_url(url)

    data = await _fetch_reddit_json(url)
    if isinstance(data, str):
        # Error string from _fetch_reddit_json
        return "Reddit", data

    try:
        if page_type == RedditPageType.COMMENT_THREAD and isinstance(data, list):
            return _format_comment_thread(data)
        elif page_type == RedditPageType.USER:
            return _format_listing(data, kind="user")
        else:
            return _format_listing(data, kind="subreddit")
    except Exception as exc:
        logger.debug("Reddit formatting error: %s", exc)
        return "Reddit", f"Error: Failed to parse Reddit response — {type(exc).__name__}"


# ---------------------------------------------------------------------------
# Formatting — comment threads
# ---------------------------------------------------------------------------

def _format_timestamp(utc: float) -> str:
    """Convert Unix timestamp to human-readable UTC date string."""
    dt = datetime.fromtimestamp(utc, tz=timezone.utc)
    return dt.strftime("%Y-%m-%d %H:%M UTC")


def _format_comment_thread(data: list) -> tuple[str, str]:
    """Format a comment-thread JSON response as markdown.

    Returns ``(title, markdown)``.
    """
    post_listing = data[0]
    comment_listing = data[1]

    post_data = post_listing["data"]["children"][0]["data"]

    title = post_data.get("title", "Untitled")
    author = post_data.get("author", "[deleted]")
    score = post_data.get("score", 0)
    num_comments = post_data.get("num_comments", 0)
    subreddit = post_data.get("subreddit", "")
    created = post_data.get("created_utc", 0)
    flair = post_data.get("link_flair_text")
    is_self = post_data.get("is_self", True)
    selftext = post_data.get("selftext", "")
    link_url = post_data.get("url", "")
    upvote_ratio = post_data.get("upvote_ratio", 0)

    parts: list[str] = []

    # Header
    parts.append(f"# {title}\n")
    meta_parts = [
        f"**u/{author}**",
        f"{score} points ({upvote_ratio:.0%} upvoted)",
        f"{num_comments} comments",
        f"r/{subreddit}",
    ]
    if flair:
        meta_parts.append(f"[{flair}]")
    meta_parts.append(_format_timestamp(created))
    parts.append(" | ".join(meta_parts) + "\n")

    # Body
    if is_self and selftext:
        parts.append(selftext + "\n")
    elif not is_self:
        parts.append(f"Link: {link_url}\n")

    # Comments
    comment_children = comment_listing["data"]["children"]
    comments_md = _render_comments(comment_children, depth=0)
    if comments_md:
        parts.append("## Comments\n")
        parts.append(comments_md)

    return title, "\n".join(parts)


def _render_comments(
    children: list[dict], depth: int,
) -> str:
    """Recursively render a comment tree as markdown.

    Each comment becomes a heading (### at depth 0, #### at depth 1, etc.)
    with the comment ID as the heading text.  This enables section-based
    navigation: ``section="ochpsln"`` extracts a specific comment, and
    ``web_fetch_sections`` shows the comment tree as a section hierarchy
    with ancestry breadcrumbs (``Comments > ochpsln > oci19t7``).
    """
    if depth >= _MAX_COMMENT_DEPTH:
        return ""

    # Heading level: ### (h3) for top-level comments under ## Comments (h2)
    hlevel = "#" * min(depth + 3, 6)
    parts: list[str] = []

    for child in children:
        if child.get("kind") != "t1":
            continue

        cdata = child["data"]
        comment_id = cdata.get("id", cdata.get("name", ""))
        author = cdata.get("author", "[deleted]")
        body = cdata.get("body", "")
        score = cdata.get("score", 0)
        created = cdata.get("created_utc", 0)

        # Heading is just the comment ID — enables section= matching
        parts.append(f"{hlevel} {comment_id}\n")

        # Metadata line
        parts.append(f"**u/{author}** ({score} points) — {_format_timestamp(created)}\n")

        # Comment body
        if body:
            parts.append(body + "\n")

        # Recurse into replies
        replies = cdata.get("replies")
        if replies and isinstance(replies, dict):
            reply_children = replies.get("data", {}).get("children", [])
            if reply_children:
                nested = _render_comments(reply_children, depth + 1)
                if nested:
                    parts.append(nested)

    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Comment-aware splitting for BM25 indexing
# ---------------------------------------------------------------------------

# Matches comment headings (### id, #### id, etc.) at the start of a line
_COMMENT_HEADING_RE = re.compile(r"^(#{3,6}) \S+$", re.MULTILINE)


def _split_by_comments(markdown: str) -> list[tuple[int, str]]:
    """Split formatted Reddit markdown into per-comment chunks.

    The post body (everything before the first ``###`` comment heading)
    becomes slice 0.  Each subsequent comment heading and its content
    (up to the next heading at the same or higher level) becomes its own
    slice.  This produces one BM25-indexed slice per comment rather than
    arbitrary ~1600-char text chunks.

    Returns ``[(char_offset, chunk_text), ...]`` suitable for
    ``_PageCache.store(presplit=...)``.
    """
    splits = list(_COMMENT_HEADING_RE.finditer(markdown))

    if not splits:
        # No comment headings — single chunk (listing or empty thread)
        return [(0, markdown)]

    chunks: list[tuple[int, str]] = []

    # Chunk 0: post body (before first comment heading)
    first_offset = splits[0].start()
    if first_offset > 0:
        chunks.append((0, markdown[:first_offset].rstrip()))

    # Each comment heading → next heading boundary
    for i, match in enumerate(splits):
        start = match.start()
        end = splits[i + 1].start() if i + 1 < len(splits) else len(markdown)
        chunks.append((start, markdown[start:end].rstrip()))

    return chunks


# ---------------------------------------------------------------------------
# Section tree for web_fetch_sections
# ---------------------------------------------------------------------------

def _build_comment_section_tree(data: list) -> tuple[str, str]:
    """Build a custom section listing from comment thread JSON.

    Returns ``(title, section_body)`` where section_body has lines like::

        - #ochpsln — u/ManyInterests (53 pts, 287 chars)
          - #oci19t7 — u/dan_ohn (11 pts, 142 chars)

    This is used by ``web_fetch_sections`` to show the comment tree as
    navigable sections instead of the generic heading-based listing.
    """
    post_listing = data[0]
    comment_listing = data[1]

    post_data = post_listing["data"]["children"][0]["data"]
    title = post_data.get("title", "Untitled")

    lines: list[str] = [f"# {title}\n"]
    comment_children = comment_listing["data"]["children"]
    _walk_comment_tree(comment_children, depth=0, lines=lines)

    return title, "\n".join(lines)


def _walk_comment_tree(
    children: list[dict], depth: int, lines: list[str],
) -> None:
    """Recursively build indented section lines for the comment tree."""
    if depth >= _MAX_COMMENT_DEPTH:
        return

    indent = "  " * depth

    for child in children:
        if child.get("kind") != "t1":
            continue

        cdata = child["data"]
        comment_id = cdata.get("id", cdata.get("name", ""))
        author = cdata.get("author", "[deleted]")
        score = cdata.get("score", 0)
        body = cdata.get("body", "")
        char_len = len(body)

        lines.append(
            f"{indent}- #{comment_id} — u/{author} ({score} pts, {char_len} chars)"
        )

        replies = cdata.get("replies")
        if replies and isinstance(replies, dict):
            reply_children = replies.get("data", {}).get("children", [])
            if reply_children:
                _walk_comment_tree(reply_children, depth + 1, lines=lines)


# ---------------------------------------------------------------------------
# Formatting — subreddit and user listings
# ---------------------------------------------------------------------------

def _format_listing(
    data: Union[list, dict], *, kind: str = "subreddit",
) -> tuple[str, str]:
    """Format a subreddit or user listing as markdown.

    Returns ``(title, markdown)``.
    """
    # Listings come as either a single dict or a one-element list
    if isinstance(data, list):
        listing = data[0]
    else:
        listing = data

    children = listing.get("data", {}).get("children", [])

    # Determine title from first entry
    if kind == "user" and children:
        first = children[0].get("data", {})
        user = first.get("author", "unknown")
        title = f"u/{user}"
    elif children:
        first = children[0].get("data", {})
        sub = first.get("subreddit", "unknown")
        title = f"r/{sub}"
    else:
        title = "Reddit"

    parts: list[str] = [f"# {title}\n"]

    for i, child in enumerate(children, 1):
        cdata = child.get("data", {})
        ckind = child.get("kind", "")

        if ckind == "t3":
            # Post
            ptitle = cdata.get("title", "Untitled")
            score = cdata.get("score", 0)
            num_comments = cdata.get("num_comments", 0)
            author = cdata.get("author", "[deleted]")
            flair = cdata.get("link_flair_text")
            flair_str = f" [{flair}]" if flair else ""
            parts.append(
                f"{i}. **{ptitle}**{flair_str} "
                f"({score} pts, {num_comments} comments) — u/{author}"
            )
        elif ckind == "t1":
            # Comment (user pages mix posts and comments)
            body_preview = (cdata.get("body", "") or "")[:120]
            if len(cdata.get("body", "")) > 120:
                body_preview += "…"
            score = cdata.get("score", 0)
            subreddit = cdata.get("subreddit", "")
            parts.append(
                f"{i}. r/{subreddit} ({score} pts): {body_preview}"
            )

    if not children:
        parts.append("*No posts found.*")

    # Pagination hint
    after = listing.get("data", {}).get("after")
    if after:
        parts.append(f"\n*More posts available (pagination cursor: {after})*")

    return title, "\n".join(parts)
