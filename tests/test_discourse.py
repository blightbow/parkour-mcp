"""Tests for parkour_mcp.discourse module."""

import httpx
import pytest
import respx

from parkour_mcp._pipeline import _discourse_fast_path
from parkour_mcp.discourse import (
    _base_url_from,
    _build_post_section_tree,
    _clean_raw,
    _detect_discourse_headers,
    _extract_topic_id,
    _format_latest,
    _format_search_results,
    _format_topic,
    _split_by_posts,
    discourse,
)

# ---------------------------------------------------------------------------
# Sample response fixtures
# ---------------------------------------------------------------------------

def _make_post(
    post_id: int, post_number: int, username: str, raw: str,
    *,
    created_at: str = "2026-04-01T12:00:00.000Z",
    reply_to: int | None = None,
) -> dict:
    """Construct a minimal Discourse post dict."""
    p = {
        "id": post_id,
        "post_number": post_number,
        "username": username,
        "raw": raw,
        "cooked": f"<p>{raw}</p>",
        "created_at": created_at,
        "reads": 10,
        "score": 5.0,
    }
    if reply_to is not None:
        p["reply_to_post_number"] = reply_to
    return p


SAMPLE_POSTS = [
    _make_post(1001, 1, "alice", "This is the opening post.\n\n## Details\n\nSome details here.",
               created_at="2026-04-01T10:00:00.000Z"),
    _make_post(1002, 2, "bob", "I agree with this proposal.",
               created_at="2026-04-01T10:15:00.000Z", reply_to=1),
    _make_post(1003, 3, "carol", "Here is an alternative view.",
               created_at="2026-04-01T11:30:00.000Z"),
]

SAMPLE_TOPIC_RESPONSE = {
    "id": 12345,
    "title": "Test Topic Title",
    "posts_count": 3,
    "views": 42,
    "reply_count": 1,
    "like_count": 5,
    "category_id": 7,
    "tags": [
        {"id": 1, "name": "test", "slug": "test"},
        {"id": 2, "name": "meta", "slug": "meta"},
    ],
    "created_at": "2026-04-01T10:00:00.000Z",
    "slug": "test-topic-title",
    "chunk_size": 20,
    "post_stream": {
        "stream": [1001, 1002, 1003],
        "posts": SAMPLE_POSTS,
    },
}

SAMPLE_SEARCH_RESPONSE = {
    # Mirrors meta.discourse.org's actual /search.json shape: SearchPostSerializer
    # does NOT emit topic_title (the post's `name` is the user's display name),
    # and SearchTopicListItemSerializer does NOT emit `views`. Title and reply
    # counts must come from the parallel topics[] array.
    "posts": [
        {
            "id": 2001,
            "topic_id": 100,
            "username": "admin",
            "name": "Admin User",
            "post_number": 1,
            "blurb": "Follow these steps to install Discourse on your server...",
        },
        {
            "id": 2002,
            "topic_id": 200,
            "username": "dev",
            "name": "Dev User",
            "post_number": 3,
            "blurb": "Creating plugins requires understanding the Ember frontend...",
        },
    ],
    "topics": [
        {"id": 100, "title": "How to install Discourse", "reply_count": 5},
        {"id": 200, "title": "Discourse plugin development", "reply_count": 12},
    ],
}

SAMPLE_LATEST_RESPONSE = {
    "topic_list": {
        "topics": [
            {
                "id": 300,
                "title": "Welcome to our forum",
                "posts_count": 1,
                "views": 500,
                "reply_count": 0,
                "created_at": "2026-04-01T08:00:00.000Z",
            },
            {
                "id": 301,
                "title": "Getting started guide",
                "posts_count": 15,
                "views": 1200,
                "reply_count": 14,
                "created_at": "2026-04-01T09:00:00.000Z",
            },
        ],
    },
}

BASE_URL = "https://forum.example.com"


# ---------------------------------------------------------------------------
# _detect_discourse_headers
# ---------------------------------------------------------------------------

class TestBaseUrlNormalization:
    """Endpoint paths are appended to the base as text, so the base has to
    be reduced to an instance root before anything is concatenated onto it."""

    @pytest.mark.parametrize(("raw", "expected"), [
        ("https://meta.discourse.org", "https://meta.discourse.org"),
        ("https://meta.discourse.org/", "https://meta.discourse.org"),
        # A topic URL reduces to the root that serves it.
        ("https://m.example.com/t/slug/123", "https://m.example.com"),
        ("https://m.example.com/t/123", "https://m.example.com"),
        ("https://m.example.com/t/slug/123/4", "https://m.example.com"),
        # Subfolder installs keep their prefix.
        ("https://example.com/forum", "https://example.com/forum"),
        ("https://example.com/forum/t/slug/1", "https://example.com/forum"),
    ])
    def test_reduces_to_instance_root(self, raw, expected):
        assert _base_url_from(raw) == expected

    @pytest.mark.parametrize("raw", [
        "https://example.com/admin/secrets#",
        "https://example.com/admin/secrets?x=",
        "https://example.com/admin/secrets#/t/1.json",
    ])
    def test_strips_fragment_and_query(self, raw):
        """A base ending in '#' or '?' would swallow the appended endpoint
        into a fragment or query, handing the caller the whole request path
        instead of a Discourse endpoint."""
        base = _base_url_from(raw)
        assert "#" not in base
        assert "?" not in base
        # The endpoint suffix survives as a real path segment.
        assert f"{base}/latest.json".endswith("/latest.json")


class TestDiscourseAddressGuard:
    """base_url is caller-supplied, so it gets the same address check the
    generic fetch path applies.  No respx mock: a refused destination must
    never reach a transport."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize(("action", "kwargs"), [
        ("topic", {"query": "http://127.0.0.1/t/x/1"}),
        ("search", {"query": "q", "base_url": "http://127.0.0.1"}),
        ("latest", {"query": "", "base_url": "http://127.0.0.1"}),
    ])
    async def test_refuses_loopback(self, action, kwargs):
        result = await discourse(action=action, **kwargs)
        assert result.startswith("Error:")
        # BlockedAddress is what the transport raises; seeing it here proves
        # the refusal came from the address check rather than from the
        # connection merely failing.
        assert "BlockedAddress" in result
        # Nothing was fetched, so nothing was fenced.
        assert "untrusted content" not in result

    @pytest.mark.asyncio
    async def test_refuses_non_http_scheme(self):
        result = await discourse(
            action="latest", query="", base_url="file:///etc/hosts",
        )
        assert result.startswith("Error:")
        assert "Unsupported URL scheme" in result


class TestDetectDiscourseHeaders:
    def test_present(self):
        headers = httpx.Headers({"x-discourse-route": "topics/show"})
        assert _detect_discourse_headers(headers) == "topics/show"

    def test_absent(self):
        headers = httpx.Headers({"content-type": "text/html"})
        assert _detect_discourse_headers(headers) is None

    def test_list_route(self):
        headers = httpx.Headers({"x-discourse-route": "list/latest"})
        assert _detect_discourse_headers(headers) == "list/latest"


# ---------------------------------------------------------------------------
# _extract_topic_id
# ---------------------------------------------------------------------------

class TestExtractTopicId:
    def test_slug_and_id(self):
        assert _extract_topic_id("https://forum.example.com/t/my-topic/12345") == 12345

    def test_id_only(self):
        assert _extract_topic_id("https://forum.example.com/t/12345") == 12345

    def test_with_post_number(self):
        assert _extract_topic_id("https://forum.example.com/t/my-topic/12345/3") == 12345

    def test_trailing_slash(self):
        assert _extract_topic_id("https://forum.example.com/t/my-topic/12345/") == 12345

    def test_not_a_topic(self):
        assert _extract_topic_id("https://forum.example.com/c/category/5") is None

    def test_non_discourse_url(self):
        assert _extract_topic_id("https://example.com/page") is None


# ---------------------------------------------------------------------------
# _clean_raw
# ---------------------------------------------------------------------------

class TestCleanRaw:
    def test_quote_bbcode(self):
        raw = '[quote="alice, post:1, topic:100"]\nSome quoted text.\n[/quote]\n\nMy reply.'
        result = _clean_raw(raw)
        assert "> **@alice** (post #1):" in result
        assert "My reply." in result

    def test_quote_without_post_number(self):
        raw = '[quote="bob"]\nQuoted text.\n[/quote]'
        result = _clean_raw(raw)
        assert "> **@bob**:" in result

    def test_upload_ref(self):
        raw = "Look at this: ![image|690x292](upload://wYlfn4GAyl9tHG7UKdqtV5n5biP.png)"
        result = _clean_raw(raw)
        assert "upload://" not in result
        assert "[image]" in result

    def test_toc_div(self):
        raw = '<div data-theme-toc="true"> </div>\n\nActual content.'
        result = _clean_raw(raw)
        assert "data-theme-toc" not in result
        assert "Actual content." in result

    def test_image_sizing(self):
        raw = "![Screenshot|690x400](https://example.com/img.png)"
        result = _clean_raw(raw)
        assert "|690x400" not in result

    def test_plain_text_unchanged(self):
        raw = "Just a normal paragraph with **bold** and *italic*."
        assert _clean_raw(raw) == raw


# ---------------------------------------------------------------------------
# _format_topic
# ---------------------------------------------------------------------------

class TestFormatTopic:
    def test_basic_topic(self):
        title, md = _format_topic(SAMPLE_TOPIC_RESPONSE, SAMPLE_POSTS)
        assert title == "Test Topic Title"
        assert "# Test Topic Title" in md
        assert "3 posts" in md
        assert "42 views" in md
        assert "tags: test, meta" in md

    def test_posts_in_output(self):
        _, md = _format_topic(SAMPLE_TOPIC_RESPONSE, SAMPLE_POSTS)
        assert "### 1" in md
        assert "**@alice**" in md
        assert "### 2" in md
        assert "**@bob**" in md
        assert "reply to #1" in md
        assert "### 3" in md
        assert "**@carol**" in md

    def test_post_content_cleaned(self):
        posts = [
            _make_post(1, 1, "user", "![img|500x300](upload://abc123.png)"),
        ]
        _, md = _format_topic(SAMPLE_TOPIC_RESPONSE, posts)
        assert "upload://" not in md
        assert "[image]" in md

    def test_tags_legacy_string_shape(self):
        """Older Discourse instances return tags as a list of bare strings."""
        topic = {**SAMPLE_TOPIC_RESPONSE, "tags": ["alpha", "beta"]}
        _, md = _format_topic(topic, SAMPLE_POSTS)
        assert "tags: alpha, beta" in md

    def test_tags_modern_dict_shape(self):
        """Modern Discourse returns tags as {id, name, slug} dicts."""
        topic = {
            **SAMPLE_TOPIC_RESPONSE,
            "tags": [
                {"id": 10, "name": "alpha", "slug": "alpha"},
                {"id": 20, "name": "beta", "slug": "beta"},
            ],
        }
        _, md = _format_topic(topic, SAMPLE_POSTS)
        assert "tags: alpha, beta" in md

    def test_tags_missing(self):
        topic = {k: v for k, v in SAMPLE_TOPIC_RESPONSE.items() if k != "tags"}
        _, md = _format_topic(topic, SAMPLE_POSTS)
        assert "tags:" not in md

    def test_mega_topic_emits_truncation_note(self):
        """Topics with >=10k posts omit post_stream.stream and set
        isMegaTopic: true. We can only surface the inline posts and must
        clearly tell the caller the rest is unavailable."""
        topic = {
            **SAMPLE_TOPIC_RESPONSE,
            "posts_count": 12345,
            "post_stream": {
                "isMegaTopic": True,
                "lastId": 99999,
                "posts": SAMPLE_POSTS,
            },
        }
        _, md = _format_topic(topic, SAMPLE_POSTS)
        assert "mega-topic" in md
        assert "12345 posts total" in md
        assert f"first {len(SAMPLE_POSTS)} posts" in md

    def test_normal_topic_no_mega_note(self):
        _, md = _format_topic(SAMPLE_TOPIC_RESPONSE, SAMPLE_POSTS)
        assert "mega-topic" not in md


# ---------------------------------------------------------------------------
# _split_by_posts
# ---------------------------------------------------------------------------

class TestSplitByPosts:
    def test_splits_on_post_headings(self):
        _, md = _format_topic(SAMPLE_TOPIC_RESPONSE, SAMPLE_POSTS)
        chunks = _split_by_posts(md)
        # Should have header chunk + 3 post chunks
        assert len(chunks) == 4

    def test_first_chunk_is_header(self):
        _, md = _format_topic(SAMPLE_TOPIC_RESPONSE, SAMPLE_POSTS)
        chunks = _split_by_posts(md)
        # First chunk should contain the title
        assert "# Test Topic Title" in chunks[0][1]

    def test_post_chunks_contain_content(self):
        _, md = _format_topic(SAMPLE_TOPIC_RESPONSE, SAMPLE_POSTS)
        chunks = _split_by_posts(md)
        # Post chunks should start with ### N
        assert chunks[1][1].startswith("### 1")
        assert chunks[2][1].startswith("### 2")
        assert chunks[3][1].startswith("### 3")

    def test_no_headings(self):
        chunks = _split_by_posts("Just plain text with no headings.")
        assert len(chunks) == 1
        assert chunks[0] == (0, "Just plain text with no headings.")


# ---------------------------------------------------------------------------
# _build_post_section_tree
# ---------------------------------------------------------------------------

class TestBuildPostSectionTree:
    def test_basic_tree(self):
        title, body = _build_post_section_tree(SAMPLE_TOPIC_RESPONSE, SAMPLE_POSTS)
        assert title == "Test Topic Title"
        assert "# Test Topic Title" in body
        assert "#1 — @alice" in body
        assert "#2 — @bob" in body
        assert "reply to #1" in body
        assert "#3 — @carol" in body

    def test_relative_timestamps(self):
        _, body = _build_post_section_tree(SAMPLE_TOPIC_RESPONSE, SAMPLE_POSTS)
        assert "T+00:00:00" in body  # alice — same as topic creation
        assert "T+00:15:00" in body  # bob — 15 min later
        assert "T+01:30:00" in body  # carol — 1.5 hours later

    def test_threading_indentation(self):
        _, body = _build_post_section_tree(SAMPLE_TOPIC_RESPONSE, SAMPLE_POSTS)
        lines = body.split("\n")
        # bob (reply to #1) should be indented under alice
        bob_line = next(ln for ln in lines if "#2 — @bob" in ln)
        alice_line = next(ln for ln in lines if "#1 — @alice" in ln)
        assert bob_line.startswith("  -")  # indented
        assert alice_line.startswith("-")   # root level


# ---------------------------------------------------------------------------
# _format_search_results
# ---------------------------------------------------------------------------

class TestFormatSearchResults:
    def test_basic_search(self):
        result = _format_search_results(SAMPLE_SEARCH_RESPONSE, BASE_URL)
        assert "How to install Discourse" in result
        assert "@admin" in result
        assert "Discourse plugin development" in result
        assert BASE_URL in result

    def test_empty_results(self):
        result = _format_search_results({"posts": [], "topics": []}, BASE_URL)
        assert "No results found" in result

    def test_limit(self):
        result = _format_search_results(SAMPLE_SEARCH_RESPONSE, BASE_URL, limit=1)
        assert "How to install Discourse" in result
        assert "Discourse plugin development" not in result

    def test_title_comes_from_topics_array(self):
        """Regression: SearchPostSerializer never emits topic_title.

        Earlier code fell back to post.get('name'), which is the user's
        display name (e.g. 'Sam Saffron'), not the topic title. The title
        must be looked up from the parallel topics[] array via topic_id.
        """
        result = _format_search_results(SAMPLE_SEARCH_RESPONSE, BASE_URL)
        # Real titles from topics[] are present
        assert "How to install Discourse" in result
        assert "Discourse plugin development" in result
        # User display names from posts[].name must NOT leak into headlines
        assert "**Admin User**" not in result
        assert "**Dev User**" not in result

    def test_unknown_topic_id_falls_back_to_untitled(self):
        """If a search post references a topic_id missing from topics[],
        the title falls back to 'Untitled' rather than crashing."""
        data = {
            "posts": [{"id": 1, "topic_id": 999, "username": "u", "post_number": 1, "blurb": ""}],
            "topics": [],
        }
        result = _format_search_results(data, BASE_URL)
        assert "Untitled" in result


# ---------------------------------------------------------------------------
# _format_latest
# ---------------------------------------------------------------------------

class TestFormatLatest:
    def test_basic_latest(self):
        result = _format_latest(SAMPLE_LATEST_RESPONSE, BASE_URL)
        assert "Welcome to our forum" in result
        assert "Getting started guide" in result
        assert BASE_URL in result

    def test_empty_latest(self):
        result = _format_latest({"topic_list": {"topics": []}}, BASE_URL)
        assert "No topics found" in result


# ---------------------------------------------------------------------------
# _discourse_fast_path
# ---------------------------------------------------------------------------

class TestDiscourseFastPath:
    @pytest.mark.asyncio
    @respx.mock
    async def test_topic_detected(self):
        """Header detection triggers JSON re-fetch."""
        headers = httpx.Headers({"x-discourse-route": "topics/show"})
        url = "https://forum.example.com/t/test-topic/12345"

        respx.get(f"{BASE_URL}/t/12345.json").mock(
            return_value=httpx.Response(200, json=SAMPLE_TOPIC_RESPONSE),
        )

        result = await _discourse_fast_path(url, headers)
        assert result is not None
        assert "Test Topic Title" in result
        assert "Discourse" in result

    @pytest.mark.asyncio
    async def test_non_topic_route_returns_none(self):
        """Non-topic routes should return None."""
        headers = httpx.Headers({"x-discourse-route": "list/latest"})
        result = await _discourse_fast_path("https://example.com/latest", headers)
        assert result is None

    @pytest.mark.asyncio
    async def test_no_header_returns_none(self):
        """Missing header should return None."""
        headers = httpx.Headers({"content-type": "text/html"})
        result = await _discourse_fast_path("https://example.com/page", headers)
        assert result is None


# ---------------------------------------------------------------------------
# discourse tool — topic action
# ---------------------------------------------------------------------------

class TestDiscourseToolTopic:
    @pytest.mark.asyncio
    @respx.mock
    async def test_fetch_topic(self):
        url = f"{BASE_URL}/t/test-topic/12345"
        respx.get(f"{BASE_URL}/t/12345.json").mock(
            return_value=httpx.Response(200, json=SAMPLE_TOPIC_RESPONSE),
        )

        result = await discourse("topic", url)
        assert "Test Topic Title" in result
        assert "@alice" in result
        assert "@bob" in result

    @pytest.mark.asyncio
    async def test_invalid_url(self):
        result = await discourse("topic", "https://example.com/not-a-topic")
        assert "Error" in result

    @pytest.mark.asyncio
    async def test_invalid_action(self):
        result = await discourse("invalid", "test")
        assert "Error" in result
        assert "Unknown action" in result


# ---------------------------------------------------------------------------
# discourse tool — search action
# ---------------------------------------------------------------------------

class TestDiscourseToolSearch:
    @pytest.mark.asyncio
    @respx.mock
    async def test_search(self):
        respx.get(f"{BASE_URL}/search.json").mock(
            return_value=httpx.Response(200, json=SAMPLE_SEARCH_RESPONSE),
        )

        result = await discourse("search", "install guide", base_url=BASE_URL)
        assert "How to install Discourse" in result

    @pytest.mark.asyncio
    async def test_search_requires_base_url(self):
        result = await discourse("search", "test query")
        assert "Error" in result
        assert "base_url" in result


# ---------------------------------------------------------------------------
# discourse tool — latest action
# ---------------------------------------------------------------------------

class TestDiscourseToolLatest:
    @pytest.mark.asyncio
    @respx.mock
    async def test_latest(self):
        respx.get(f"{BASE_URL}/latest.json").mock(
            return_value=httpx.Response(200, json=SAMPLE_LATEST_RESPONSE),
        )

        result = await discourse("latest", "", base_url=BASE_URL)
        assert "Welcome to our forum" in result

    @pytest.mark.asyncio
    async def test_latest_requires_base_url(self):
        result = await discourse("latest", "")
        assert "Error" in result
        assert "base_url" in result


# ---------------------------------------------------------------------------
# Batch post fetching
# ---------------------------------------------------------------------------

class TestBatchPostFetching:
    @pytest.mark.asyncio
    @respx.mock
    async def test_topic_with_remaining_posts(self):
        """Topics with >20 posts should batch-fetch remaining."""
        # First page has 2 posts but stream has 3
        first_page = {
            **SAMPLE_TOPIC_RESPONSE,
            "post_stream": {
                "stream": [1001, 1002, 1003],
                "posts": SAMPLE_POSTS[:2],  # Only first 2
            },
        }
        batch_response = {
            "post_stream": {
                "posts": [SAMPLE_POSTS[2]],  # The remaining post
            },
        }

        url = f"{BASE_URL}/t/test-topic/12345"
        respx.get(f"{BASE_URL}/t/12345.json").mock(
            return_value=httpx.Response(200, json=first_page),
        )
        respx.get(url=__name__).pass_through()
        # Mock the batch endpoint — match any URL containing posts.json
        respx.get(url__startswith=f"{BASE_URL}/t/12345/posts.json").mock(
            return_value=httpx.Response(200, json=batch_response),
        )

        result = await discourse("topic", url)
        # All 3 posts should be present
        assert "@alice" in result
        assert "@bob" in result
        assert "@carol" in result
