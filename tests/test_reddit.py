"""Tests for the Reddit fast path."""

import pytest
from curl_cffi.requests import exceptions as cc_exc

from parkour_mcp.reddit import (
    _detect_reddit_url,
    _classify_reddit_url,
    _extract_comment_permalink,
    _fetch_reddit_content,
    _format_comment_thread,
    _format_listing,
    _render_comments,
    _format_timestamp,
    _format_relative_time,
    _build_comment_section_tree,
    _split_by_comments,
    _MAX_COMMENT_DEPTH,
    RedditPageType,
)
from parkour_mcp._pipeline import _reddit_fast_path, _page_cache


# ---------------------------------------------------------------------------
# Sample JSON fixtures
# ---------------------------------------------------------------------------

def _make_post(
    *,
    title: str = "Test Post Title",
    author: str = "test_user",
    selftext: str = "This is the post body.",
    score: int = 42,
    num_comments: int = 5,
    subreddit: str = "Python",
    created_utc: float = 1700000000.0,
    is_self: bool = True,
    url: str = "https://old.reddit.com/r/Python/comments/abc123/test_post_title/",
    link_flair_text: str | None = "Discussion",
    upvote_ratio: float = 0.85,
) -> dict:
    return {
        "kind": "t3",
        "data": {
            "title": title,
            "author": author,
            "selftext": selftext,
            "score": score,
            "num_comments": num_comments,
            "subreddit": subreddit,
            "created_utc": created_utc,
            "is_self": is_self,
            "url": url,
            "link_flair_text": link_flair_text,
            "upvote_ratio": upvote_ratio,
        },
    }


def _make_comment(
    *,
    id: str = "abc1234",
    author: str = "commenter",
    body: str = "Great post!",
    score: int = 10,
    created_utc: float = 1700000100.0,
    replies: object = "",
) -> dict:
    return {
        "kind": "t1",
        "data": {
            "id": id,
            "author": author,
            "body": body,
            "score": score,
            "created_utc": created_utc,
            "replies": replies,
        },
    }


def _make_thread_json(
    post: dict | None = None,
    comments: list[dict] | None = None,
) -> list:
    if post is None:
        post = _make_post()
    if comments is None:
        comments = [_make_comment()]
    return [
        {"data": {"children": [post]}},
        {"data": {"children": comments}},
    ]


def _make_listing_json(posts: list[dict] | None = None, after: str | None = None) -> list:
    if posts is None:
        posts = [_make_post(title=f"Post {i}") for i in range(3)]
    return [{"data": {"children": posts, "after": after}}]


THREAD_JSON = _make_thread_json()

LISTING_JSON = _make_listing_json()


# ---------------------------------------------------------------------------
# URL detection
# ---------------------------------------------------------------------------

class TestDetectRedditUrl:
    def test_www_reddit(self):
        result = _detect_reddit_url("https://www.reddit.com/r/Python/")
        assert result == "https://old.reddit.com/r/Python/"

    def test_old_reddit(self):
        result = _detect_reddit_url("https://old.reddit.com/r/Python/")
        assert result == "https://old.reddit.com/r/Python/"

    def test_new_reddit(self):
        result = _detect_reddit_url("https://new.reddit.com/r/Python/")
        assert result == "https://old.reddit.com/r/Python/"

    def test_np_reddit(self):
        result = _detect_reddit_url("https://np.reddit.com/r/Python/")
        assert result == "https://old.reddit.com/r/Python/"

    def test_bare_reddit(self):
        result = _detect_reddit_url("https://reddit.com/r/Python/")
        assert result == "https://old.reddit.com/r/Python/"

    def test_redd_it(self):
        result = _detect_reddit_url("https://redd.it/abc123")
        assert result == "https://redd.it/abc123"

    def test_non_reddit_returns_none(self):
        assert _detect_reddit_url("https://example.com/page") is None
        assert _detect_reddit_url("https://arxiv.org/abs/1234.5678") is None

    def test_preserves_sort_param(self):
        result = _detect_reddit_url("https://www.reddit.com/r/Python/?sort=top")
        assert result is not None
        assert "sort=top" in result

    def test_strips_other_params(self):
        result = _detect_reddit_url("https://www.reddit.com/r/Python/?ref=share&sort=new")
        assert result is not None
        assert "sort=new" in result
        assert "ref" not in result

    def test_adds_trailing_slash(self):
        result = _detect_reddit_url("https://www.reddit.com/r/Python")
        assert result is not None
        assert result.endswith("/") or "?" in result

    def test_comment_thread_url(self):
        url = "https://www.reddit.com/r/Python/comments/abc123/some_title/"
        result = _detect_reddit_url(url)
        assert result is not None
        assert "old.reddit.com" in result
        assert "/r/Python/comments/abc123/some_title/" in result

    def test_user_url(self):
        result = _detect_reddit_url("https://www.reddit.com/u/spez/")
        assert result is not None
        assert "old.reddit.com" in result


# ---------------------------------------------------------------------------
# Comment-permalink extraction
# ---------------------------------------------------------------------------

class TestExtractCommentPermalink:
    def test_permalink_with_slug(self):
        url = "https://old.reddit.com/r/LocalLLaMA/comments/1sp10oa/kimi_k26_soon/ogz8eaz/"
        result = _extract_comment_permalink(url)
        assert result is not None
        stripped, comment_id = result
        assert stripped == "https://old.reddit.com/r/LocalLLaMA/comments/1sp10oa/kimi_k26_soon/"
        assert comment_id == "ogz8eaz"

    def test_permalink_without_trailing_slash(self):
        url = "https://old.reddit.com/r/Python/comments/abc123/title/def456"
        result = _extract_comment_permalink(url)
        assert result is not None
        stripped, comment_id = result
        assert stripped == "https://old.reddit.com/r/Python/comments/abc123/title/"
        assert comment_id == "def456"

    def test_permalink_preserves_query_string(self):
        url = "https://old.reddit.com/r/Python/comments/abc123/title/def456/?context=3"
        result = _extract_comment_permalink(url)
        assert result is not None
        stripped, _ = result
        assert stripped == "https://old.reddit.com/r/Python/comments/abc123/title/?context=3"

    def test_whole_post_returns_none(self):
        url = "https://old.reddit.com/r/Python/comments/abc123/title/"
        assert _extract_comment_permalink(url) is None

    def test_whole_post_no_slug_returns_none(self):
        # Ambiguous slug-less form /r/SUB/comments/POSTID/ — we require
        # the slug to disambiguate from permalinks, so this must NOT
        # match (falling through to normal whole-post handling).
        url = "https://old.reddit.com/r/Python/comments/abc123/"
        assert _extract_comment_permalink(url) is None

    def test_subreddit_returns_none(self):
        assert _extract_comment_permalink("https://old.reddit.com/r/Python/") is None

    def test_user_page_returns_none(self):
        assert _extract_comment_permalink("https://old.reddit.com/u/spez/") is None

    def test_non_reddit_returns_none(self):
        assert _extract_comment_permalink("https://example.com/some/path/") is None


# ---------------------------------------------------------------------------
# Page type classification
# ---------------------------------------------------------------------------

class TestClassifyRedditUrl:
    def test_comment_thread(self):
        url = "https://old.reddit.com/r/Python/comments/abc123/title/"
        assert _classify_reddit_url(url) == RedditPageType.COMMENT_THREAD

    def test_subreddit(self):
        url = "https://old.reddit.com/r/Python/"
        assert _classify_reddit_url(url) == RedditPageType.SUBREDDIT

    def test_user_u(self):
        url = "https://old.reddit.com/u/spez/"
        assert _classify_reddit_url(url) == RedditPageType.USER

    def test_user_full(self):
        url = "https://old.reddit.com/user/spez/"
        assert _classify_reddit_url(url) == RedditPageType.USER

    def test_redd_it(self):
        url = "https://redd.it/abc123"
        assert _classify_reddit_url(url) == RedditPageType.SHORT_LINK


# ---------------------------------------------------------------------------
# Comment thread formatting
# ---------------------------------------------------------------------------

class TestFormatCommentThread:
    def test_renders_post_header(self):
        title, md = _format_comment_thread(THREAD_JSON)
        assert title == "Test Post Title"
        assert "# Test Post Title" in md
        assert "u/test_user" in md
        assert "42 points" in md
        assert "r/Python" in md

    def test_renders_selftext(self):
        _, md = _format_comment_thread(THREAD_JSON)
        assert "This is the post body." in md

    def test_renders_link_post(self):
        link_post = _make_post(
            is_self=False,
            selftext="",
            url="https://example.com/article",
        )
        _, md = _format_comment_thread(_make_thread_json(post=link_post))
        assert "https://example.com/article" in md

    def test_renders_flair(self):
        _, md = _format_comment_thread(THREAD_JSON)
        assert "[Discussion]" in md

    def test_renders_comments(self):
        _, md = _format_comment_thread(THREAD_JSON)
        assert "## Comments" in md
        assert "### abc1234" in md
        assert "u/commenter" in md
        assert "Great post!" in md

    def test_renders_nested_comments(self):
        reply = _make_comment(
            id="reply123",
            author="replier",
            body="I agree!",
            score=5,
        )
        parent = _make_comment(
            id="parent456",
            author="parent_commenter",
            body="Top-level comment",
            replies={
                "data": {
                    "children": [reply],
                },
            },
        )
        _, md = _format_comment_thread(_make_thread_json(comments=[parent]))
        assert "### parent456" in md
        assert "#### reply123" in md
        assert "u/parent_commenter" in md
        assert "u/replier" in md

    def test_deleted_author(self):
        comment = _make_comment(author="[deleted]", body="[deleted]")
        _, md = _format_comment_thread(_make_thread_json(comments=[comment]))
        assert "[deleted]" in md

    def test_empty_replies_string(self):
        """Reddit uses empty string for no replies — should not crash."""
        comment = _make_comment(replies="")
        _, md = _format_comment_thread(_make_thread_json(comments=[comment]))
        assert "Great post!" in md


class TestRenderComments:
    def test_skips_non_t1_kinds(self):
        children = [{"kind": "more", "data": {"count": 42}}]
        result = _render_comments(children, depth=0)
        assert result == ""

    def test_max_depth_stops_recursion(self):
        result = _render_comments(
            [_make_comment()], depth=_MAX_COMMENT_DEPTH,
        )
        assert result == ""

    def test_heading_levels_increase_with_depth(self):
        reply = _make_comment(id="deep_reply", author="deep_user", body="Deeply nested")
        parent = _make_comment(
            id="top_comment",
            author="top_user",
            body="Top level",
            replies={"data": {"children": [reply]}},
        )
        result = _render_comments([parent], depth=0)
        assert "### top_comment" in result
        assert "#### deep_reply" in result
        assert "**u/top_user**" in result
        assert "**u/deep_user**" in result


# ---------------------------------------------------------------------------
# Listing formatting
# ---------------------------------------------------------------------------

class TestFormatListing:
    def test_subreddit_listing(self):
        title, md = _format_listing(LISTING_JSON, kind="subreddit")
        assert title == "r/Python"
        assert "# r/Python" in md
        assert "**Post 0**" in md
        assert "**Post 1**" in md
        assert "**Post 2**" in md

    def test_includes_scores(self):
        _, md = _format_listing(LISTING_JSON, kind="subreddit")
        assert "42 pts" in md
        assert "5 comments" in md

    def test_includes_flair(self):
        _, md = _format_listing(LISTING_JSON, kind="subreddit")
        assert "[Discussion]" in md

    def test_empty_listing(self):
        empty = [{"data": {"children": [], "after": None}}]
        _, md = _format_listing(empty, kind="subreddit")
        assert "*No posts found.*" in md

    def test_pagination_hint(self):
        data = _make_listing_json(after="t3_nextpage")
        _, md = _format_listing(data, kind="subreddit")
        assert "t3_nextpage" in md

    def test_user_listing_with_comments(self):
        user_comment = {
            "kind": "t1",
            "data": {
                "author": "test_user",
                "body": "This is my comment on a thread",
                "score": 7,
                "subreddit": "Python",
                "created_utc": 1700000000.0,
            },
        }
        data = [{"data": {"children": [user_comment], "after": None}}]
        title, md = _format_listing(data, kind="user")
        assert title == "u/test_user"
        assert "r/Python" in md


# ---------------------------------------------------------------------------
# Comment section tree (for web_fetch_sections)
# ---------------------------------------------------------------------------

class TestBuildCommentSectionTree:
    def test_builds_tree(self):
        reply = _make_comment(id="reply_id", author="replier", body="Short", score=5)
        parent = _make_comment(
            id="parent_id", author="parent_user", body="A longer comment body here",
            score=42, replies={"data": {"children": [reply]}},
        )
        data = _make_thread_json(comments=[parent])
        title, body = _build_comment_section_tree(data)
        assert title == "Test Post Title"
        assert "#parent_id" in body
        assert "u/parent_user" in body
        assert "42 pts" in body
        assert "#reply_id" in body
        assert "u/replier" in body

    def test_includes_char_count(self):
        comment = _make_comment(id="cmt1", body="Hello world", score=1)
        data = _make_thread_json(comments=[comment])
        _, body = _build_comment_section_tree(data)
        assert "11 chars" in body

    def test_includes_post_timestamp(self):
        data = _make_thread_json()
        _, body = _build_comment_section_tree(data)
        # Post absolute timestamp in the title line
        assert "2023-11-14" in body
        assert "UTC" in body

    def test_includes_relative_time(self):
        # Post at T=1700000000, comment at T=1700000100 → T+00:01:40
        comment = _make_comment(id="cmt1", body="test", created_utc=1700000100.0)
        data = _make_thread_json(comments=[comment])
        _, body = _build_comment_section_tree(data)
        assert "T+00:01:40" in body

    def test_indents_replies(self):
        reply = _make_comment(id="child", body="reply", score=1)
        parent = _make_comment(
            id="parent", body="top", score=1,
            replies={"data": {"children": [reply]}},
        )
        data = _make_thread_json(comments=[parent])
        _, body = _build_comment_section_tree(data)
        lines = body.split("\n")
        parent_line = [line for line in lines if "#parent" in line][0]
        child_line = [line for line in lines if "#child" in line][0]
        # Child should be indented more than parent
        assert len(child_line) - len(child_line.lstrip()) > len(parent_line) - len(parent_line.lstrip())


# ---------------------------------------------------------------------------
# Relative time formatting
# ---------------------------------------------------------------------------

class TestFormatRelativeTime:
    def test_zero_delta(self):
        assert _format_relative_time(100.0, 100.0) == "T+00:00:00"

    def test_seconds(self):
        assert _format_relative_time(145.0, 100.0) == "T+00:00:45"

    def test_minutes_and_seconds(self):
        assert _format_relative_time(200.0, 100.0) == "T+00:01:40"

    def test_hours(self):
        assert _format_relative_time(7300.0, 0.0) == "T+02:01:40"

    def test_large_delta(self):
        # 25 hours
        assert _format_relative_time(90000.0, 0.0) == "T+25:00:00"

    def test_negative_clamped_to_zero(self):
        assert _format_relative_time(50.0, 100.0) == "T+00:00:00"


# ---------------------------------------------------------------------------
# Timestamp formatting
# ---------------------------------------------------------------------------

class TestFormatTimestamp:
    def test_formats_utc(self):
        result = _format_timestamp(1700000000.0)
        assert "2023-11-14" in result
        assert "UTC" in result


# ---------------------------------------------------------------------------
# Fetch integration (mocked HTTP)
# ---------------------------------------------------------------------------

class TestFetchRedditContent:
    @pytest.mark.asyncio
    async def test_fetches_comment_thread(self, fake_async_session):
        url = "https://old.reddit.com/r/Python/comments/abc123/test/"
        fake_async_session.mock_get(
            "https://old.reddit.com/r/Python/comments/abc123/test/.json",
            json_data=THREAD_JSON,
        )

        title, md = await _fetch_reddit_content(url)
        assert title == "Test Post Title"
        assert "This is the post body." in md

    @pytest.mark.asyncio
    async def test_fetches_subreddit_listing(self, fake_async_session):
        url = "https://old.reddit.com/r/Python/"
        fake_async_session.mock_get(
            "https://old.reddit.com/r/Python/.json",
            json_data=LISTING_JSON,
        )

        title, md = await _fetch_reddit_content(url)
        assert title == "r/Python"
        assert "Post 0" in md

    @pytest.mark.asyncio
    async def test_http_error_returns_error_string(self, fake_async_session):
        url = "https://old.reddit.com/r/Python/comments/abc123/test/"
        fake_async_session.mock_get(
            "https://old.reddit.com/r/Python/comments/abc123/test/.json",
            status=404,
        )

        title, md = await _fetch_reddit_content(url)
        assert title == "Reddit"
        assert "Error" in md

    @pytest.mark.asyncio
    async def test_rate_limit_429(self, fake_async_session):
        url = "https://old.reddit.com/r/Python/"
        fake_async_session.mock_get(
            "https://old.reddit.com/r/Python/.json",
            status=429,
        )

        _, md = await _fetch_reddit_content(url)
        assert "rate limit" in md.lower()

    @pytest.mark.asyncio
    async def test_timeout_returns_error(self, fake_async_session):
        url = "https://old.reddit.com/r/Python/"
        fake_async_session.raise_on_get(
            "https://old.reddit.com/r/Python/.json",
            cc_exc.Timeout("timeout"),
        )

        _, md = await _fetch_reddit_content(url)
        assert "timed out" in md.lower()

    @pytest.mark.asyncio
    async def test_redd_it_resolves_redirect(self, fake_async_session):
        # redd.it HEAD follows redirects server-side; the mock returns the
        # post-redirect URL as the response's .url attribute, which
        # _resolve_redd_it reads via str(resp.url).
        fake_async_session.mock_head(
            "https://redd.it/abc123",
            final_url="https://www.reddit.com/r/Python/comments/abc123/test/",
        )
        fake_async_session.mock_get(
            "https://old.reddit.com/r/Python/comments/abc123/test/.json",
            json_data=THREAD_JSON,
        )

        title, _ = await _fetch_reddit_content("https://redd.it/abc123")
        assert title == "Test Post Title"


# ---------------------------------------------------------------------------
# Fast-path integration
# ---------------------------------------------------------------------------

class TestRedditFastPath:
    @pytest.mark.asyncio
    async def test_reddit_url_intercepted(self, fake_async_session):
        url = "https://www.reddit.com/r/Python/comments/abc123/test/"
        fake_async_session.mock_get(
            "https://old.reddit.com/r/Python/comments/abc123/test/.json",
            json_data=THREAD_JSON,
        )

        result = await _reddit_fast_path(url)
        assert result is not None
        assert "Test Post Title" in result
        assert "api: Reddit (.json)" in result

    @pytest.mark.asyncio
    async def test_non_reddit_url_returns_none(self):
        result = await _reddit_fast_path("https://example.com/page")
        assert result is None

    @pytest.mark.asyncio
    async def test_error_returns_string_not_none(self, fake_async_session):
        url = "https://www.reddit.com/r/Python/comments/abc123/test/"
        fake_async_session.mock_get(
            "https://old.reddit.com/r/Python/comments/abc123/test/.json",
            status=500,
        )

        result = await _reddit_fast_path(url)
        assert result is not None
        assert "Error" in result

    @pytest.mark.asyncio
    async def test_cache_populated(self, fake_async_session):
        url = "https://www.reddit.com/r/Python/comments/abc123/test/"
        fake_async_session.mock_get(
            "https://old.reddit.com/r/Python/comments/abc123/test/.json",
            json_data=THREAD_JSON,
        )

        await _reddit_fast_path(url)
        cached = _page_cache.get(url)
        assert cached is not None
        assert cached.renderer == "reddit"
        assert cached.title == "Test Post Title"

    @pytest.mark.asyncio
    async def test_slicing_search(self, fake_async_session):
        """BM25 search over cached Reddit content works."""
        url = "https://www.reddit.com/r/Python/comments/abc123/test/"
        fake_async_session.mock_get(
            "https://old.reddit.com/r/Python/comments/abc123/test/.json",
            json_data=THREAD_JSON,
        )

        await _reddit_fast_path(url)
        cached = _page_cache.get(url)
        assert cached is not None
        indices, _ = cached.search("post body")
        assert len(indices) > 0

    @pytest.mark.asyncio
    async def test_fenced_content(self, fake_async_session):
        """Output should have content fencing for untrusted content."""
        url = "https://www.reddit.com/r/Python/comments/abc123/test/"
        fake_async_session.mock_get(
            "https://old.reddit.com/r/Python/comments/abc123/test/.json",
            json_data=THREAD_JSON,
        )

        result = await _reddit_fast_path(url)
        assert result is not None
        assert "┌─ untrusted content" in result
        assert "└─ untrusted content" in result
        assert "│" in result

    @pytest.mark.asyncio
    async def test_comment_aware_slicing(self, fake_async_session):
        """Cache should split by comment, not arbitrary text boundaries."""
        reply = _make_comment(id="reply_1", author="replier", body="A reply", score=3)
        c1 = _make_comment(
            id="comment_1", author="user_a", body="First comment",
            replies={"data": {"children": [reply]}},
        )
        c2 = _make_comment(id="comment_2", author="user_b", body="Second comment")
        data = _make_thread_json(comments=[c1, c2])
        url = "https://www.reddit.com/r/Python/comments/abc123/test/"
        fake_async_session.mock_get(
            "https://old.reddit.com/r/Python/comments/abc123/test/.json",
            json_data=data,
        )

        await _reddit_fast_path(url)
        cached = _page_cache.get(url)
        assert cached is not None
        # Slice 0 = post body, then one slice per comment
        assert cached.slices is not None
        assert len(cached.slices) == 4  # post + c1 + reply_1 + c2
        # Each comment slice should start with the comment heading
        assert "### comment_1" in cached.slices[1]
        assert "#### reply_1" in cached.slices[2]
        assert "### comment_2" in cached.slices[3]
        # Post body slice should NOT contain comment headings
        assert "###" not in cached.slices[0]


# ---------------------------------------------------------------------------
# Comment-permalink integration (URL normalization in fetch_direct)
# ---------------------------------------------------------------------------

class TestRedditCommentPermalinkIntegration:
    """End-to-end tests for comment-permalink URL normalization.

    The permalink normalizer lives in ``fetch_direct.py``'s Reddit
    dispatch block, upstream of ``_reddit_fast_path``, so these tests
    exercise the ``web_fetch_direct`` entry point directly.
    """

    @pytest.mark.asyncio
    async def test_permalink_strips_and_injects_section(self, fake_async_session):
        from parkour_mcp.fetch_direct import web_fetch_direct

        target = _make_comment(id="target_c", author="u", body="TARGET BODY", score=5)
        other = _make_comment(id="other_c", author="u2", body="other body", score=3)
        data = _make_thread_json(comments=[target, other])

        # Mock the WHOLE-POST .json — the normalizer strips COMMENTID before fetching
        fake_async_session.mock_get(
            "https://old.reddit.com/r/Python/comments/abc123/title/.json",
            json_data=data,
        )

        permalink = "https://www.reddit.com/r/Python/comments/abc123/title/target_c/"
        result = await web_fetch_direct(permalink, max_tokens=3000)

        assert "TARGET BODY" in result
        assert "other body" not in result
        assert "identified comment 'target_c'" in result
        # Cache key is the stripped URL (trailing comment ID removed),
        # keeping the input's netloc — a subsequent fetch of the post
        # URL hits the same cache entry.
        assert _page_cache.get("https://www.reddit.com/r/Python/comments/abc123/title/") is not None
        assert _page_cache.get("https://www.reddit.com/r/Python/comments/abc123/title/target_c/") is None

    @pytest.mark.asyncio
    async def test_permalink_with_explicit_section_user_wins(self, fake_async_session):
        from parkour_mcp.fetch_direct import web_fetch_direct

        target = _make_comment(id="target_c", author="u", body="TARGET BODY", score=5)
        other = _make_comment(id="other_c", author="u2", body="OTHER BODY", score=3)
        data = _make_thread_json(comments=[target, other])

        fake_async_session.mock_get(
            "https://old.reddit.com/r/Python/comments/abc123/title/.json",
            json_data=data,
        )

        permalink = "https://www.reddit.com/r/Python/comments/abc123/title/target_c/"
        result = await web_fetch_direct(permalink, max_tokens=3000, section="other_c")

        # User's explicit section= wins over the URL's implicit target
        assert "OTHER BODY" in result
        assert "TARGET BODY" not in result
        # No permalink note — the URL strip is silent when the caller overrides
        assert "identified comment" not in result

    @pytest.mark.asyncio
    async def test_permalink_with_search_fetches_full_thread(self, fake_async_session):
        from parkour_mcp.fetch_direct import web_fetch_direct

        target = _make_comment(id="target_c", author="u", body="TARGET BODY", score=5)
        other = _make_comment(id="other_c", author="u2", body="other discussion", score=3)
        data = _make_thread_json(comments=[target, other])

        fake_async_session.mock_get(
            "https://old.reddit.com/r/Python/comments/abc123/title/.json",
            json_data=data,
        )

        permalink = "https://www.reddit.com/r/Python/comments/abc123/title/target_c/"
        result = await web_fetch_direct(permalink, max_tokens=3000, search="discussion")

        # Search runs over the full thread (both comments cached), returns
        # matches — no implicit section filter narrows scope.
        assert "other discussion" in result

    @pytest.mark.asyncio
    async def test_whole_post_url_unchanged(self, fake_async_session):
        from parkour_mcp.fetch_direct import web_fetch_direct

        fake_async_session.mock_get(
            "https://old.reddit.com/r/Python/comments/abc123/title/.json",
            json_data=THREAD_JSON,
        )

        url = "https://www.reddit.com/r/Python/comments/abc123/title/"
        result = await web_fetch_direct(url, max_tokens=3000)
        # No permalink note — this was never a permalink
        assert "identified comment" not in result


# ---------------------------------------------------------------------------
# Comment-aware splitting
# ---------------------------------------------------------------------------

class TestSplitByComments:
    def test_splits_at_comment_headings(self):
        md = "# Title\n\nPost body.\n\n## Comments\n\n### abc\n\nComment 1.\n\n### def\n\nComment 2.\n"
        chunks = _split_by_comments(md)
        assert len(chunks) == 3  # post body + 2 comments
        assert "# Title" in chunks[0][1]
        assert "### abc" in chunks[1][1]
        assert "### def" in chunks[2][1]

    def test_nested_comments_split_separately(self):
        md = "# Title\n\n## Comments\n\n### parent\n\nParent body.\n\n#### child\n\nChild body.\n"
        chunks = _split_by_comments(md)
        assert len(chunks) == 3  # post + parent + child
        assert "### parent" in chunks[1][1]
        assert "#### child" in chunks[2][1]
        # Parent chunk should not contain child
        assert "child" not in chunks[1][1].lower()

    def test_no_comments_returns_single_chunk(self):
        md = "# Title\n\nJust a listing, no comments.\n"
        chunks = _split_by_comments(md)
        assert len(chunks) == 1
        assert chunks[0] == (0, md)

    def test_offsets_are_correct(self):
        md = "# Title\n\nBody.\n\n### abc\n\nComment.\n"
        chunks = _split_by_comments(md)
        for offset, text in chunks:
            assert md[offset:].startswith(text[:20])
