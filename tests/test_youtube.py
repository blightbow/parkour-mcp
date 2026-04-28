"""Tests for parkour_mcp.youtube module."""

import sys

import pytest
import tantivy

import parkour_mcp.youtube  # noqa: F401
_yt_module = sys.modules["parkour_mcp.youtube"]

# Capture the real chapter-fetch function before any autouse fixture
# substitutes a stub. Tests that exercise the parsing logic call this
# saved reference directly so they don't go through the module attribute
# the autouse fixture has replaced.
_REAL_FETCH_CHAPTERS = _yt_module._fetch_video_chapters_sync


@pytest.fixture(autouse=True)
def _clear_transcript_cache():
    """Reset the module-level transcript and yt-dlp-info caches between tests.

    Without this, fake-fetch tests using the same URL would silently
    cache-hit the entry created by an earlier test, never invoking the
    monkeypatched fetcher. The yt-dlp-info cache memoizes the full
    extract_info dict and is shared across the video / chapter / fallback
    paths; clearing it keeps every test's mock assumptions clean.
    """
    _yt_module._transcript_cache.clear()
    _yt_module._yt_info_cache.clear()
    yield
    _yt_module._transcript_cache.clear()
    _yt_module._yt_info_cache.clear()


@pytest.fixture(autouse=True)
def _mock_chapters_offline(monkeypatch):
    """Stub the chapter fetcher to return [] by default.

    The transcript action launches a concurrent yt-dlp call to fetch
    chapter metadata. Without a stub, every test that exercises the
    transcript action would hit the live network for chapter data.
    Tests specifically exercising chapter integration override this
    by monkeypatching ``_fetch_video_chapters_sync`` again.
    """
    monkeypatch.setattr(
        _yt_module, "_fetch_video_chapters_sync", lambda _: [],
    )

from parkour_mcp.youtube import (  # noqa: E402
    Chapter,
    Segment,
    Window,
    _build_chapter_marks,
    _captions_summary,
    _detect_outlier_gaps,
    _detect_youtube_url,
    _format_duration,
    _format_upload_date,
    _map_transcript_error,
    _map_yt_dlp_error,
    _mmss,
    _punctuation_density,
    _segment_ends_sentence,
    _window_chapter_title,
    coalesce_windows,
    render_transcript,
    youtube,
)


# ---------------------------------------------------------------------------
# URL detection
# ---------------------------------------------------------------------------

class TestDetectYouTubeUrl:
    def test_watch_url(self):
        assert _detect_youtube_url(
            "https://www.youtube.com/watch?v=jNQXAC9IVRw"
        ) == ("video", "jNQXAC9IVRw")

    def test_watch_url_with_extra_query(self):
        assert _detect_youtube_url(
            "https://www.youtube.com/watch?feature=share&v=jNQXAC9IVRw&t=42s"
        ) == ("video", "jNQXAC9IVRw")

    def test_short_url(self):
        assert _detect_youtube_url("https://youtu.be/jNQXAC9IVRw") == (
            "video", "jNQXAC9IVRw",
        )

    def test_short_url_with_timestamp(self):
        assert _detect_youtube_url("https://youtu.be/jNQXAC9IVRw?t=10") == (
            "video", "jNQXAC9IVRw",
        )

    def test_shorts_url(self):
        assert _detect_youtube_url(
            "https://www.youtube.com/shorts/abc123def45"
        ) == ("video", "abc123def45")

    def test_clip_url(self):
        assert _detect_youtube_url(
            "https://www.youtube.com/clip/UgkxAbCdEf12"
        ) == ("video", "UgkxAbCdEf12")

    def test_embed_url(self):
        assert _detect_youtube_url(
            "https://www.youtube.com/embed/jNQXAC9IVRw"
        ) == ("video", "jNQXAC9IVRw")

    def test_mobile_url(self):
        assert _detect_youtube_url(
            "https://m.youtube.com/watch?v=jNQXAC9IVRw"
        ) == ("video", "jNQXAC9IVRw")

    def test_handle_channel(self):
        assert _detect_youtube_url(
            "https://www.youtube.com/@MarquesBrownlee"
        ) == ("channel", "@MarquesBrownlee")

    def test_channel_id(self):
        result = _detect_youtube_url(
            "https://www.youtube.com/channel/UCBJycsmduvYEL83R_U4JriQ"
        )
        assert result == ("channel", "UCBJycsmduvYEL83R_U4JriQ")

    def test_legacy_user(self):
        assert _detect_youtube_url(
            "https://www.youtube.com/user/Computerphile"
        ) == ("channel", "Computerphile")

    def test_vanity_channel(self):
        assert _detect_youtube_url(
            "https://www.youtube.com/c/Veritasium"
        ) == ("channel", "Veritasium")

    def test_playlist(self):
        result = _detect_youtube_url(
            "https://www.youtube.com/playlist?list=PLrAXtmRdnEQy6nuLMt9H1Pj7RgqZTjB"
        )
        assert result is not None
        assert result[0] == "playlist"

    def test_music_excluded(self):
        # music.youtube.com is deferred to a sibling tool; detection must
        # surface it as 'music' kind so the dispatcher can emit a clear error.
        result = _detect_youtube_url("https://music.youtube.com/watch?v=jNQXAC9IVRw")
        assert result is not None
        assert result[0] == "music"

    def test_non_youtube_url(self):
        assert _detect_youtube_url("https://example.com/watch?v=jNQXAC9IVRw") is None
        assert _detect_youtube_url("https://vimeo.com/123456") is None
        assert _detect_youtube_url("https://twitch.tv/somestreamer") is None


# ---------------------------------------------------------------------------
# Format helpers
# ---------------------------------------------------------------------------

class TestFormatHelpers:
    def test_duration_under_minute(self):
        assert _format_duration(19) == "0:19"

    def test_duration_under_hour(self):
        assert _format_duration(125) == "2:05"

    def test_duration_over_hour(self):
        assert _format_duration(3725) == "1:02:05"

    def test_duration_zero(self):
        assert _format_duration(0) == "0:00"

    def test_duration_none(self):
        assert _format_duration(None) is None

    def test_duration_float(self):
        # yt-dlp returns floats; we truncate to whole seconds
        assert _format_duration(125.7) == "2:05"

    def test_upload_date(self):
        assert _format_upload_date("20050423") == "2005-04-23"

    def test_upload_date_invalid_passthrough(self):
        # Non-8-digit strings pass through unchanged so callers can decide
        assert _format_upload_date("notadate") == "notadate"

    def test_upload_date_none(self):
        assert _format_upload_date(None) is None

    def test_captions_summary_manual_only(self):
        info = {"subtitles": {"en": [], "fr": []}, "automatic_captions": {}}
        langs, auto_only = _captions_summary(info)
        assert langs == ["en", "fr"]
        assert auto_only is False

    def test_captions_summary_auto_only(self):
        info = {"subtitles": {}, "automatic_captions": {"en": []}}
        langs, auto_only = _captions_summary(info)
        assert langs == ["en"]
        assert auto_only is True

    def test_captions_summary_mixed(self):
        info = {
            "subtitles": {"en": []},
            "automatic_captions": {"en": [], "fr": []},
        }
        langs, auto_only = _captions_summary(info)
        assert langs == ["en", "fr"]
        # Mixed = manual exists; not auto-only
        assert auto_only is False

    def test_captions_summary_none(self):
        langs, auto_only = _captions_summary({})
        assert langs == []
        assert auto_only is False


# ---------------------------------------------------------------------------
# Error mapping
# ---------------------------------------------------------------------------

class TestErrorMapping:
    def test_bot_detection(self):
        from yt_dlp.utils import ExtractorError  # type: ignore[import-not-found]
        err = ExtractorError("Sign in to confirm you're not a bot.")
        result = _map_yt_dlp_error(err)
        assert "bot" in result.lower()
        assert "residential" in result.lower()

    def test_private_video(self):
        from yt_dlp.utils import ExtractorError  # type: ignore[import-not-found]
        err = ExtractorError("Private video")
        result = _map_yt_dlp_error(err)
        assert "private" in result.lower()

    def test_video_unavailable(self):
        from yt_dlp.utils import ExtractorError  # type: ignore[import-not-found]
        err = ExtractorError("Video unavailable")
        result = _map_yt_dlp_error(err)
        assert "unavailable" in result.lower()

    def test_geo_restricted(self):
        from yt_dlp.utils import GeoRestrictedError  # type: ignore[import-not-found]
        err = GeoRestrictedError("Not available in your country")
        result = _map_yt_dlp_error(err)
        assert "geo" in result.lower()


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------

class TestDispatcher:
    @pytest.mark.asyncio
    async def test_unknown_action(self):
        result = await youtube(action="unknown")
        assert result.startswith("Error:")
        assert "video" in result  # lists valid actions

    @pytest.mark.asyncio
    async def test_video_missing_url(self):
        result = await youtube(action="video")
        assert "Error:" in result
        assert "url" in result.lower()

    @pytest.mark.asyncio
    async def test_video_non_youtube_url(self):
        result = await youtube(
            action="video", url="https://example.com/foo",
        )
        assert "Error:" in result
        assert "recognized" in result.lower() or "youtube" in result.lower()

    @pytest.mark.asyncio
    async def test_video_with_channel_url(self):
        result = await youtube(
            action="video", url="https://www.youtube.com/@MarquesBrownlee",
        )
        assert "Error:" in result
        assert "channel" in result.lower()

    @pytest.mark.asyncio
    async def test_video_with_music_url(self):
        result = await youtube(
            action="video",
            url="https://music.youtube.com/watch?v=jNQXAC9IVRw",
        )
        assert "Error:" in result
        assert "music" in result.lower()


# ---------------------------------------------------------------------------
# _video action with mocked yt-dlp
# ---------------------------------------------------------------------------

# Modeled after yt-dlp's actual output for "Me at the zoo" (jNQXAC9IVRw),
# trimmed to the fields _video reads.
_SAMPLE_INFO = {
    "id": "jNQXAC9IVRw",
    "title": "Me at the zoo",
    "description": "The first video on YouTube. Maybe ever.",
    "channel": "jawed",
    "uploader": "jawed",
    "channel_id": "UC4QobU6STFB0P71PMvOGN5A",
    "channel_url": "https://www.youtube.com/channel/UC4QobU6STFB0P71PMvOGN5A",
    "duration": 19.0,
    "upload_date": "20050423",
    "view_count": 365129877,
    "like_count": 10728475,
    "language": "en",
    "live_status": "not_live",
    "availability": "public",
    "subtitles": {"en": [{"ext": "vtt"}]},
    "automatic_captions": {"en": [{"ext": "vtt"}], "fr": [{"ext": "vtt"}]},
}


class _FakeYoutubeDL:
    """Minimal stand-in for yt_dlp.YoutubeDL covering what _video calls."""

    def __init__(self, payload):
        # payload may be a dict (for extract_info to return) or an exception
        # to raise on extract_info.
        self._payload = payload

    def extract_info(self, *args, **kwargs):
        del args, kwargs
        if isinstance(self._payload, BaseException):
            raise self._payload
        return self._payload

    @staticmethod
    def sanitize_info(info):
        # The real implementation strips non-JSON-safe values; our fixture
        # is already JSON-safe so passthrough is correct.
        return info


class TestVideoAction:
    @pytest.mark.asyncio
    async def test_metadata_and_description(self, monkeypatch):
        monkeypatch.setattr(
            _yt_module, "_get_ydl_video",
            lambda: _FakeYoutubeDL(_SAMPLE_INFO),
        )
        result = await youtube(
            action="video",
            url="https://www.youtube.com/watch?v=jNQXAC9IVRw",
        )

        frontmatter, _, body = result.partition("\n\n")

        # Structurally-validated fields stay in frontmatter
        assert "video_id: jNQXAC9IVRw" in frontmatter
        assert "duration: 0:19" in frontmatter
        assert "upload_date: 2005-04-23" in frontmatter
        assert "view_count: 365129877" in frontmatter
        assert "language: en" in frontmatter
        assert "channel_id: UC4QobU6STFB0P71PMvOGN5A" in frontmatter
        # User-generated strings must NOT appear in frontmatter — they
        # would inherit the trust of tool-generated metadata. See
        # docs/frontmatter-standard.md.
        assert "title: Me at the zoo" not in frontmatter
        assert "channel: jawed" not in frontmatter
        # They render inside the fenced body instead
        assert "# Me at the zoo" in body  # fence heading
        assert "**Channel**: [jawed]" in body
        assert "The first video on YouTube" in body
        assert "untrusted content" in result

    @pytest.mark.asyncio
    async def test_no_description_fallback(self, monkeypatch):
        info = dict(_SAMPLE_INFO)
        info["description"] = ""
        monkeypatch.setattr(
            _yt_module, "_get_ydl_video",
            lambda: _FakeYoutubeDL(info),
        )
        result = await youtube(
            action="video",
            url="https://www.youtube.com/watch?v=jNQXAC9IVRw",
        )
        assert "no description" in result.lower()

    @pytest.mark.asyncio
    async def test_yt_dlp_returns_none(self, monkeypatch):
        monkeypatch.setattr(
            _yt_module, "_get_ydl_video",
            lambda: _FakeYoutubeDL(None),
        )
        result = await youtube(
            action="video",
            url="https://www.youtube.com/watch?v=jNQXAC9IVRw",
        )
        assert "Error:" in result
        assert "no metadata" in result.lower()

    @pytest.mark.asyncio
    async def test_bot_detection_propagates(self, monkeypatch):
        from yt_dlp.utils import ExtractorError  # type: ignore[import-not-found]
        exc = ExtractorError("Sign in to confirm you're not a bot.")
        monkeypatch.setattr(
            _yt_module, "_get_ydl_video",
            lambda: _FakeYoutubeDL(exc),
        )
        result = await youtube(
            action="video",
            url="https://www.youtube.com/watch?v=jNQXAC9IVRw",
        )
        assert "Error:" in result
        assert "bot" in result.lower()

    @pytest.mark.asyncio
    async def test_captions_auto_only_flag(self, monkeypatch):
        info = dict(_SAMPLE_INFO)
        info["subtitles"] = {}
        info["automatic_captions"] = {"en": [{"ext": "vtt"}]}
        monkeypatch.setattr(
            _yt_module, "_get_ydl_video",
            lambda: _FakeYoutubeDL(info),
        )
        result = await youtube(
            action="video",
            url="https://www.youtube.com/watch?v=jNQXAC9IVRw",
        )
        assert "captions_auto_only: True" in result

    @pytest.mark.asyncio
    async def test_video_with_comment_count_emits_see_also(self, monkeypatch):
        info = dict(_SAMPLE_INFO)
        info["comment_count"] = 1234
        monkeypatch.setattr(
            _yt_module, "_get_ydl_video",
            lambda: _FakeYoutubeDL(info),
        )
        result = await youtube(
            action="video",
            url="https://www.youtube.com/watch?v=jNQXAC9IVRw",
        )
        assert "comment_count: 1234" in result
        # see_also points at the dedicated comments tool
        assert "see_also" in result.lower()
        assert "YoutubeComments" in result
        assert "The first video on YouTube" in result

    @pytest.mark.asyncio
    async def test_video_no_comment_count_no_see_also(self, monkeypatch):
        info = dict(_SAMPLE_INFO)
        info.pop("comment_count", None)
        monkeypatch.setattr(
            _yt_module, "_get_ydl_video",
            lambda: _FakeYoutubeDL(info),
        )
        result = await youtube(
            action="video",
            url="https://www.youtube.com/watch?v=jNQXAC9IVRw",
        )
        assert "YoutubeComments" not in result


# ---------------------------------------------------------------------------
# Comment formatting and the fetch_comments pivot
# ---------------------------------------------------------------------------

_COMMENTS_INFO = {
    **_SAMPLE_INFO,
    "comments": [
        {
            "id": "Ugxa1",
            "parent": "root",
            "author": "First Commenter",
            "text": "Great video!",
            "like_count": 42,
            "_time_text": "1 year ago",
            "is_pinned": True,
            "author_is_uploader": False,
        },
        {
            "id": "Ugxa1.reply",
            "parent": "Ugxa1",
            "author": "Replier",
            "text": "I agree.\nMore thoughts here.",
            "like_count": 5,
            "_time_text": "11 months ago",
        },
        {
            "id": "Ugxa2",
            "parent": "root",
            "author": "Second Commenter",
            "text": "Useful.",
            "like_count": 12,
            "_time_text": "6 months ago",
            "author_is_uploader": True,
        },
    ],
    "comment_count": 3,
}


class TestFormatComment:
    def test_with_full_meta(self):
        c = {
            "author": "Alice",
            "text": "Hello there.",
            "like_count": 100,
            "_time_text": "2 days ago",
            "is_pinned": True,
            "author_is_uploader": False,
        }
        out = _yt_module._format_comment(c)
        assert "**Alice**" in out
        assert "[pinned]" in out
        assert "100 likes" in out
        assert "2 days ago" in out
        assert "Hello there." in out

    def test_uploader_badge(self):
        c = {"author": "Bob", "text": "Thanks.", "author_is_uploader": True}
        out = _yt_module._format_comment(c)
        assert "[uploader]" in out

    def test_anonymous_fallback(self):
        c = {"text": "Some text"}
        out = _yt_module._format_comment(c)
        assert "(anonymous)" in out

    def test_text_whitespace_normalized(self):
        c = {"author": "X", "text": "Line one\nline two\n\nline three"}
        out = _yt_module._format_comment(c)
        assert "Line one line two line three" in out


class TestFormatTopLevelOverview:
    def test_empty(self):
        out = _yt_module._format_top_level_overview([])
        assert "(no top-level comments)" in out

    def test_overview_lists_top_level_with_ids(self):
        # Pass only top-level comments (overview mode). Inline cast to
        # narrow the dict-literal value's mixed-type inference for ty.
        from typing import cast
        all_comments = cast(list[dict], _COMMENTS_INFO["comments"])
        top_level = [
            c for c in all_comments
            if not c.get("parent") or c.get("parent") == "root"
        ]
        out = _yt_module._format_top_level_overview(top_level)
        assert "Comments (2 top-level" in out
        # Each top-level numbered, includes id for drill-down
        assert "1. **First Commenter**" in out
        assert "id=Ugxa1" in out
        assert "2. **Second Commenter**" in out
        assert "id=Ugxa2" in out
        # Replies must NOT be in the overview body
        assert "**Replier**" not in out


class TestFormatCommentThread:
    def test_thread_renders_target_and_replies(self):
        comments = _COMMENTS_INFO["comments"]
        out = _yt_module._format_comment_thread(comments, "Ugxa1")
        assert "Thread for comment id=Ugxa1" in out
        assert "**First Commenter**" in out
        # Replies section
        assert "Replies (1)" in out
        assert "**Replier**" in out
        # Reply text whitespace was normalized through _format_comment
        assert "I agree. More thoughts here." in out

    def test_thread_for_comment_with_no_replies(self):
        comments = _COMMENTS_INFO["comments"]
        out = _yt_module._format_comment_thread(comments, "Ugxa2")
        assert "Thread for comment id=Ugxa2" in out
        assert "**Second Commenter**" in out
        assert "Replies (0)" in out
        assert "(no replies in view)" in out

    def test_thread_unknown_id(self):
        comments = _COMMENTS_INFO["comments"]
        out = _yt_module._format_comment_thread(comments, "UgxNotThere")
        assert "Error" in out
        assert "not found" in out

    def test_thread_id_is_a_reply_rejected(self):
        comments = _COMMENTS_INFO["comments"]
        out = _yt_module._format_comment_thread(comments, "Ugxa1.reply")
        assert "Error" in out
        assert "is a reply, not a top-level" in out


class TestYoutubeCommentsOverview:
    @pytest.mark.asyncio
    async def test_overview_lists_top_level_only(self, monkeypatch):
        def fake_extract(url, max_comments):
            del url, max_comments
            return _COMMENTS_INFO
        monkeypatch.setattr(
            _yt_module, "_extract_video_with_comments_sync", fake_extract,
        )
        result = await _yt_module.youtube_comments(
            url="https://www.youtube.com/watch?v=jNQXAC9IVRw",
        )
        assert "view: overview" in result
        assert "top_level_comments: 2" in result
        assert "**First Commenter**" in result
        assert "**Second Commenter**" in result
        # Replies absent in overview
        assert "**Replier**" not in result
        # IDs visible for drill-down
        assert "id=Ugxa1" in result
        assert "id=Ugxa2" in result
        # Hint nudges toward drill-down
        assert "comment_id" in result.lower()

    @pytest.mark.asyncio
    async def test_overview_empty(self, monkeypatch):
        info = dict(_COMMENTS_INFO)
        info["comments"] = []

        def fake_extract(url, max_comments):
            del url, max_comments
            return info
        monkeypatch.setattr(
            _yt_module, "_extract_video_with_comments_sync", fake_extract,
        )
        result = await _yt_module.youtube_comments(
            url="https://www.youtube.com/watch?v=jNQXAC9IVRw",
        )
        assert "top_level_comments: 0" in result
        assert "(no top-level comments)" in result

    @pytest.mark.asyncio
    async def test_overview_extractor_args_use_caller_limit(self, monkeypatch):
        captured = {}

        class _Spy:
            def __init__(self, opts):
                captured["opts"] = opts
            def extract_info(self, url, download):
                del url, download
                return _COMMENTS_INFO
            def sanitize_info(self, info):
                return info

        import yt_dlp as _ydl_pkg
        monkeypatch.setattr(_ydl_pkg, "YoutubeDL", _Spy)

        await _yt_module.youtube_comments(
            url="https://www.youtube.com/watch?v=jNQXAC9IVRw",
            limit=15,
        )
        ea = captured["opts"]["extractor_args"]["youtube"]
        assert ea["comment_sort"] == ["top"]
        # Overview cap encodes the caller's limit; replies disabled
        assert ea["max_comments"] == ["15", "15", "0"]

    @pytest.mark.asyncio
    async def test_overview_limit_clamped(self, monkeypatch):
        captured = {}

        class _Spy:
            def __init__(self, opts):
                captured["opts"] = opts
            def extract_info(self, url, download):
                del url, download
                return _COMMENTS_INFO
            def sanitize_info(self, info):
                return info

        import yt_dlp as _ydl_pkg
        monkeypatch.setattr(_ydl_pkg, "YoutubeDL", _Spy)

        await _yt_module.youtube_comments(
            url="https://www.youtube.com/watch?v=jNQXAC9IVRw",
            limit=999,
        )
        ea = captured["opts"]["extractor_args"]["youtube"]
        assert ea["max_comments"][0] == str(_yt_module._YOUTUBE_COMMENTS_LIMIT_MAX)


class TestYoutubeCommentsThread:
    @pytest.mark.asyncio
    async def test_comment_id_drills_into_thread(self, monkeypatch):
        def fake_extract(url, max_comments):
            del url, max_comments
            return _COMMENTS_INFO
        monkeypatch.setattr(
            _yt_module, "_extract_video_with_comments_sync", fake_extract,
        )
        result = await _yt_module.youtube_comments(
            url="https://www.youtube.com/watch?v=jNQXAC9IVRw",
            comment_id="Ugxa1",
        )
        assert "view: thread" in result
        assert "comment_id: Ugxa1" in result
        assert "replies_in_view: 1" in result
        # Target + reply present
        assert "**First Commenter**" in result
        assert "**Replier**" in result
        # Other top-level NOT present
        assert "**Second Commenter**" not in result

    @pytest.mark.asyncio
    async def test_comment_id_unknown_renders_error_in_body(self, monkeypatch):
        def fake_extract(url, max_comments):
            del url, max_comments
            return _COMMENTS_INFO
        monkeypatch.setattr(
            _yt_module, "_extract_video_with_comments_sync", fake_extract,
        )
        result = await _yt_module.youtube_comments(
            url="https://www.youtube.com/watch?v=jNQXAC9IVRw",
            comment_id="UgxNotThere",
        )
        assert "view: thread" in result
        assert "Error" in result
        assert "not found" in result.lower()

    @pytest.mark.asyncio
    async def test_comment_id_pointing_at_reply_rejected(self, monkeypatch):
        def fake_extract(url, max_comments):
            del url, max_comments
            return _COMMENTS_INFO
        monkeypatch.setattr(
            _yt_module, "_extract_video_with_comments_sync", fake_extract,
        )
        result = await _yt_module.youtube_comments(
            url="https://www.youtube.com/watch?v=jNQXAC9IVRw",
            comment_id="Ugxa1.reply",
        )
        assert "is a reply" in result.lower()

    @pytest.mark.asyncio
    async def test_thread_extractor_args_shape(self, monkeypatch):
        captured = {}

        class _Spy:
            def __init__(self, opts):
                captured["opts"] = opts
            def extract_info(self, url, download):
                del url, download
                return _COMMENTS_INFO
            def sanitize_info(self, info):
                return info

        import yt_dlp as _ydl_pkg
        monkeypatch.setattr(_ydl_pkg, "YoutubeDL", _Spy)

        await _yt_module.youtube_comments(
            url="https://www.youtube.com/watch?v=jNQXAC9IVRw",
            comment_id="Ugxa1",
        )
        ea = captured["opts"]["extractor_args"]["youtube"]
        assert ea["max_comments"] == list(_yt_module._THREAD_MAX_COMMENTS)


class TestYoutubeCommentsValidation:
    @pytest.mark.asyncio
    async def test_no_url(self):
        result = await _yt_module.youtube_comments(url="")
        assert "Error" in result and "url" in result.lower()

    @pytest.mark.asyncio
    async def test_non_youtube_url(self):
        result = await _yt_module.youtube_comments(url="https://example.com/v")
        assert "Error" in result

    @pytest.mark.asyncio
    async def test_channel_url_rejected(self):
        result = await _yt_module.youtube_comments(
            url="https://www.youtube.com/@MKBHD",
        )
        assert "Error" in result
        assert "channel" in result.lower()

    @pytest.mark.asyncio
    async def test_music_url_rejected(self):
        result = await _yt_module.youtube_comments(
            url="https://music.youtube.com/watch?v=jNQXAC9IVRw",
        )
        assert "Error" in result
        assert "music" in result.lower()


# ---------------------------------------------------------------------------
# Transcript: helpers
# ---------------------------------------------------------------------------

class TestTranscriptHelpers:
    def test_mmss_under_minute(self):
        assert _mmss(19) == "00:19"

    def test_mmss_under_hour(self):
        assert _mmss(125) == "02:05"

    def test_mmss_over_hour(self):
        assert _mmss(3725) == "01:02:05"

    def test_punctuation_density_punctuated(self):
        segs = [
            Segment(0, 2, "Hello there. How are you?"),
            Segment(2, 2, "I am well. Thanks!"),
        ]
        d = _punctuation_density(segs)
        # 4 enders, 9 words → ~0.44
        assert d > 0.3

    def test_punctuation_density_unpunctuated(self):
        segs = [
            Segment(0, 2, "all right so here we are in front of"),
            Segment(2, 2, "the elephants the cool thing about these"),
        ]
        d = _punctuation_density(segs)
        assert d == 0.0

    def test_punctuation_density_empty(self):
        assert _punctuation_density([]) == 0.0

    def test_segment_ends_sentence_period(self):
        assert _segment_ends_sentence(Segment(0, 1, "Hello world.")) is True

    def test_segment_ends_sentence_no(self):
        assert _segment_ends_sentence(Segment(0, 1, "Hello world")) is False

    def test_segment_ends_sentence_trailing_whitespace(self):
        # Trailing whitespace shouldn't fool the detector
        assert _segment_ends_sentence(Segment(0, 1, "Hello world. ")) is True

    def test_segment_ends_sentence_empty(self):
        assert _segment_ends_sentence(Segment(0, 1, "")) is False


# ---------------------------------------------------------------------------
# Outlier gap detection
# ---------------------------------------------------------------------------

class TestOutlierGaps:
    def test_empty(self):
        assert _detect_outlier_gaps([]) == []

    def test_single(self):
        assert _detect_outlier_gaps([Segment(0, 1, "a")]) == [False]

    def test_short_with_outlier_uses_fallback(self):
        # 3 segments → 2 gaps. Below the rolling-window threshold, so the
        # 3.0s fixed fallback applies.
        segs = [
            Segment(0, 1, "a"),     # ends at 1
            Segment(2, 1, "b"),     # gap=1.0 — under fallback
            Segment(8, 1, "c"),     # gap=5.0 — over fallback
        ]
        out = _detect_outlier_gaps(segs)
        assert out == [False, True, False]

    def test_long_with_outlier_uses_rolling(self):
        # 12 segments at steady 1s gaps + one ~5s outlier gap. The rolling
        # median is ~1.0, threshold = max(2*1, 1.5) = 2.0; the 5s gap
        # crosses, the 1s gaps don't.
        segs: list[Segment] = []
        t = 0.0
        for i in range(11):
            segs.append(Segment(t, 1.0, f"seg{i}"))
            t = t + 1.0 + 1.0  # 1s segment + 1s gap
        # Inject outlier: bump next segment's start so gap is ~5s
        segs.append(Segment(t + 4.0, 1.0, "outlier"))
        out = _detect_outlier_gaps(segs)
        # Last in-gap position before outlier should flag
        assert out[-2] is True
        # Steady-cadence positions should not flag
        assert all(o is False for o in out[:-2])
        assert out[-1] is False

    def test_floor_blocks_tiny_outliers(self):
        # All gaps <0.5s; nothing should flag even though some are 4× the median
        segs = [
            Segment(0, 0.1, "a"),   # ends 0.1
            Segment(0.2, 0.1, "b"), # gap 0.1
            Segment(0.3, 0.1, "c"), # gap 0.0
            Segment(0.5, 0.1, "d"), # gap 0.1 — but tiny, under 1.5 floor
        ]
        out = _detect_outlier_gaps(segs)
        assert all(o is False for o in out)


# ---------------------------------------------------------------------------
# Window coalescer
# ---------------------------------------------------------------------------

def _make_segments(spec: list[tuple[float, float, str]]) -> list[Segment]:
    """Helper: build segments from (start, duration, text) tuples."""
    return [Segment(s, d, t) for s, d, t in spec]


class TestCoalesceWindows:
    def test_empty(self):
        assert coalesce_windows([], sentence_aware=False) == []

    def test_short_input_one_window(self):
        # Total duration < min — everything goes in one window
        segs = _make_segments([
            (0, 2, "a"),
            (2, 2, "b"),
            (4, 2, "c"),
        ])
        windows = coalesce_windows(segs, sentence_aware=False)
        assert len(windows) == 1
        assert windows[0].start == 0
        assert windows[0].end == 6
        assert len(windows[0].segments) == 3

    def test_max_duration_forces_cut(self):
        # 8 segments × 5s each = 40s total, max is 35s. Should cut.
        segs = _make_segments([(i * 5.0, 5.0, f"s{i}") for i in range(8)])
        windows = coalesce_windows(segs, sentence_aware=False)
        assert len(windows) >= 2
        # All windows must respect max duration
        for w in windows:
            assert (w.end - w.start) <= 35.0 + 0.01  # tiny float slack

    def test_pause_boundary_triggers_cut_in_band(self):
        # 6 segments × 5s reaching 30s, then a 3s pause, then more segments.
        # The pause should cut once we're in the [25, 35] band.
        segs = _make_segments([
            (0, 5, "a"),
            (5, 5, "b"),
            (10, 5, "c"),
            (15, 5, "d"),
            (20, 5, "e"),
            (25, 5, "f"),  # ends at 30
            # 3s gap
            (33, 5, "g"),
            (38, 5, "h"),
        ])
        windows = coalesce_windows(segs, sentence_aware=False)
        # Expect the cut at the pause
        assert len(windows) == 2
        assert windows[0].segments[-1].text == "f"
        assert windows[1].segments[0].text == "g"

    def test_no_pause_runs_to_max(self):
        # No pauses at all → cut at max
        segs = _make_segments([(i * 4.0, 4.0, f"s{i}") for i in range(15)])
        windows = coalesce_windows(segs, sentence_aware=False)
        for w in windows:
            assert (w.end - w.start) <= 35.0 + 0.01

    def test_sentence_aware_cuts_at_period(self):
        # 5 segments × 6s. After the third, the text ends with a period.
        # In sentence-aware mode, that should cut even without a pause.
        segs = _make_segments([
            (0, 6, "first"),     # ends 6
            (6, 6, "second"),    # ends 12
            (12, 6, "third."),   # ends 18, but in band? 18 < 25 — no cut yet
            (18, 6, "fourth"),   # ends 24, still < 25 — no cut yet
            (24, 6, "fifth."),   # ends 30 — IN band, sentence end → cut
            (30, 6, "sixth"),    # next window
        ])
        windows = coalesce_windows(segs, sentence_aware=True)
        assert len(windows) == 2
        assert windows[0].segments[-1].text == "fifth."
        assert windows[1].segments[0].text == "sixth"

    def test_sentence_aware_off_uses_pause_only(self):
        # Same input but sentence_aware=False — the sentence break shouldn't
        # cut; we'd need a pause boundary to cut. With contiguous timing,
        # that means everything stays in one window (or hits max).
        segs = _make_segments([
            (0, 6, "first"),
            (6, 6, "second."),
            (12, 6, "third"),
            (18, 6, "fourth."),
        ])
        windows = coalesce_windows(segs, sentence_aware=False)
        # No pauses, no max breach (24s) → one window
        assert len(windows) == 1


# ---------------------------------------------------------------------------
# Renderers
# ---------------------------------------------------------------------------

class TestRenderTranscript:
    @staticmethod
    def _windows() -> list[Window]:
        # Modeled after "Me at the zoo"
        segs = [
            Segment(1.20, 2.16, "All right, so here we are, in front of the elephants"),
            Segment(5.32, 2.66, "the cool thing about these guys is that they have really..."),
            Segment(7.97, 4.64, "really really long trunks"),
            Segment(12.62, 1.75, "and that's cool"),
            Segment(14.42, 1.31, "(baaaaaaaaaaahhh!!)"),
            Segment(16.88, 2.00, "and that's pretty much all there is to say"),
        ]
        return [Window(start=segs[0].start, end=segs[-1].end, segments=tuple(segs))]

    def test_none_mode_flat_text(self):
        out = render_transcript(self._windows(), mode="none")
        assert "[" not in out
        assert "All right, so here we are" in out
        assert "all there is to say" in out

    def test_absolute_mode_per_line_timestamps(self):
        out = render_transcript(self._windows(), mode="absolute")
        assert "[00:01]" in out
        assert "[00:05]" in out
        assert "[00:16]" in out
        assert "All right, so here we are" in out

    def test_compact_mode_single_window_anchor(self):
        out = render_transcript(self._windows(), mode="compact")
        # Window anchor present
        assert "[00:01]" in out
        # Each segment on its own line
        assert "All right, so here we are, in front of the elephants" in out
        assert "(baaaaaaaaaaahhh!!)" in out
        # Compact mode emits one anchor (window start), not per-line
        assert out.count("[00:") == 1 or out.count("[00:") <= 2

    def test_compact_mode_multi_window_anchors(self):
        # Two windows with a clear gap between
        segs1 = [Segment(i * 5.0, 5.0, f"win1 seg{i}") for i in range(6)]
        segs2 = [Segment(35.0 + i * 5.0, 5.0, f"win2 seg{i}") for i in range(4)]
        windows = [
            Window(0, 30, tuple(segs1)),
            Window(35, 55, tuple(segs2)),
        ]
        out = render_transcript(windows, mode="compact")
        assert "[00:00]" in out
        assert "[00:35]" in out

    def test_structured_mode_yaml(self):
        out = render_transcript(self._windows(), mode="structured")
        # Should be parseable YAML
        import yaml
        data = yaml.safe_load(out)
        assert isinstance(data, list)
        assert len(data) == 6
        assert data[0]["t"] == 1.2
        assert "elephants" in data[0]["text"]

    def test_empty_windows(self):
        assert render_transcript([], mode="compact") == ""
        assert render_transcript([], mode="none") == ""
        assert render_transcript([], mode="absolute") == ""


# ---------------------------------------------------------------------------
# Transcript error mapping
# ---------------------------------------------------------------------------

class TestTranscriptErrorMapping:
    def test_ip_blocked(self):
        from youtube_transcript_api import IpBlocked
        err = IpBlocked("vid")
        out = _map_transcript_error(err)
        # Acknowledges both paths failed — fallback already attempted
        assert "IP reputation" in out or "429" in out
        assert "fallback" in out.lower()
        assert "residential proxy" in out.lower()

    def test_request_blocked(self):
        from youtube_transcript_api import RequestBlocked
        err = RequestBlocked("vid")
        out = _map_transcript_error(err)
        assert "bot" in out.lower()
        assert "fallback" in out.lower()

    def test_po_token_required(self):
        from youtube_transcript_api import PoTokenRequired
        err = PoTokenRequired("vid")
        out = _map_transcript_error(err)
        assert "PoToken" in out
        # Now points users at the plugin path rather than calling the
        # fallback "on the roadmap" (which is no longer accurate)
        assert "plugin" in out.lower()
        assert "bgutil-ytdlp-pot-provider" in out

    def test_transcripts_disabled(self):
        from youtube_transcript_api import TranscriptsDisabled
        err = TranscriptsDisabled("vid")
        out = _map_transcript_error(err)
        assert "disabled" in out.lower()

    def test_no_transcript_found(self):
        from youtube_transcript_api import NoTranscriptFound
        # NoTranscriptFound has a specific signature; pass minimal args
        err = NoTranscriptFound("vid", ["en"], None)
        out = _map_transcript_error(err)
        assert "no transcript" in out.lower()

    def test_video_unavailable(self):
        from youtube_transcript_api import VideoUnavailable
        err = VideoUnavailable("vid")
        out = _map_transcript_error(err)
        assert "unavailable" in out.lower()


# ---------------------------------------------------------------------------
# _transcript action
# ---------------------------------------------------------------------------

class _FakeFetchedTranscript:
    """Stand-in for youtube-transcript-api's FetchedTranscript."""

    def __init__(self, snippets, language_code="en", is_generated=False):
        self.snippets = snippets
        self.language = "English"
        self.language_code = language_code
        self.is_generated = is_generated
        self.video_id = "fake"


class _FakeSnippet:
    """Stand-in for FetchedTranscriptSnippet (simple value object)."""
    def __init__(self, start, duration, text):
        self.start = start
        self.duration = duration
        self.text = text


# Modeled on "Me at the zoo" — manual captions, punctuated.
_ZOO_SNIPPETS = [
    _FakeSnippet(1.20, 2.16, "All right, so here we are, in front of the elephants"),
    _FakeSnippet(5.32, 2.66, "the cool thing about these guys is that they have really..."),
    _FakeSnippet(7.97, 4.64, "really really long trunks"),
    _FakeSnippet(12.62, 1.75, "and that's cool"),
    _FakeSnippet(14.42, 1.31, "(baaaaaaaaaaahhh!!)"),
    _FakeSnippet(16.88, 2.00, "and that's pretty much all there is to say"),
]

# Auto-caption shape: lowercase, no punctuation.
_AUTO_SNIPPETS = [
    _FakeSnippet(0.0, 3.0, "all right so here we are in front of the"),
    _FakeSnippet(3.0, 3.0, "elephants the cool thing about these guys is"),
    _FakeSnippet(6.0, 3.0, "that they have really really really long trunks"),
    _FakeSnippet(9.0, 3.0, "and that's cool"),
]


class TestTranscriptAction:
    @pytest.mark.asyncio
    async def test_punctuated_returns_compact_default(self, monkeypatch):
        def fake_fetch(video_id, languages):
            del video_id, languages
            return _FakeFetchedTranscript(_ZOO_SNIPPETS, "en", is_generated=False)
        monkeypatch.setattr(_yt_module, "_fetch_transcript_sync", fake_fetch)

        result = await youtube(
            action="transcript",
            url="https://www.youtube.com/watch?v=jNQXAC9IVRw",
        )
        assert "transcript_kind: manual" in result
        assert "transcript_language: en" in result
        assert "All right, so here we are, in front of the elephants" in result
        assert "untrusted content" in result

    @pytest.mark.asyncio
    async def test_auto_caption_uses_time_window(self, monkeypatch):
        def fake_fetch(video_id, languages):
            del video_id, languages
            return _FakeFetchedTranscript(_AUTO_SNIPPETS, "en", is_generated=True)
        monkeypatch.setattr(_yt_module, "_fetch_transcript_sync", fake_fetch)

        result = await youtube(
            action="transcript",
            url="https://www.youtube.com/watch?v=jNQXAC9IVRw",
        )
        assert "transcript_kind: auto" in result
        assert "chunking_strategy: time_window" in result

    @pytest.mark.asyncio
    async def test_punctuated_uses_sentence_strategy(self, monkeypatch):
        def fake_fetch(video_id, languages):
            del video_id, languages
            return _FakeFetchedTranscript(_ZOO_SNIPPETS, "en", is_generated=False)
        monkeypatch.setattr(_yt_module, "_fetch_transcript_sync", fake_fetch)

        result = await youtube(
            action="transcript",
            url="https://www.youtube.com/watch?v=jNQXAC9IVRw",
        )
        # Density of _ZOO_SNIPPETS is high (commas/periods/exclam) so sentence-aware
        assert "chunking_strategy: sentence" in result

    @pytest.mark.asyncio
    async def test_timestamps_absolute_mode(self, monkeypatch):
        def fake_fetch(video_id, languages):
            del video_id, languages
            return _FakeFetchedTranscript(_ZOO_SNIPPETS, "en", is_generated=False)
        monkeypatch.setattr(_yt_module, "_fetch_transcript_sync", fake_fetch)

        result = await youtube(
            action="transcript",
            url="https://www.youtube.com/watch?v=jNQXAC9IVRw",
            timestamps="absolute",
        )
        # Absolute mode emits a per-line [MM:SS]
        assert "[00:01]" in result
        assert "[00:05]" in result

    @pytest.mark.asyncio
    async def test_timestamps_none_mode(self, monkeypatch):
        def fake_fetch(video_id, languages):
            del video_id, languages
            return _FakeFetchedTranscript(_ZOO_SNIPPETS, "en", is_generated=False)
        monkeypatch.setattr(_yt_module, "_fetch_transcript_sync", fake_fetch)

        result = await youtube(
            action="transcript",
            url="https://www.youtube.com/watch?v=jNQXAC9IVRw",
            timestamps="none",
        )
        # No bracketed timestamps in the body
        body_start = result.index("\n\n") + 2
        body = result[body_start:]
        # `[` only appears in fence markers and (baaaaa...!!) lines
        # Specifically, no [00:NN] timestamps
        import re as _re
        assert _re.search(r"\[\d+:\d+\]", body) is None

    @pytest.mark.asyncio
    async def test_no_url(self):
        result = await youtube(action="transcript")
        assert "Error" in result and "url" in result.lower()

    @pytest.mark.asyncio
    async def test_non_youtube_url(self):
        result = await youtube(
            action="transcript",
            url="https://example.com/video",
        )
        assert "Error" in result

    @pytest.mark.asyncio
    async def test_channel_url_rejected(self):
        result = await youtube(
            action="transcript",
            url="https://www.youtube.com/@somechan",
        )
        assert "Error" in result and "channel" in result.lower()

    @pytest.mark.asyncio
    async def test_transcripts_disabled_propagates(self, monkeypatch):
        from youtube_transcript_api import TranscriptsDisabled

        def fake_fetch(video_id, languages):
            del languages
            raise TranscriptsDisabled(video_id)
        monkeypatch.setattr(_yt_module, "_fetch_transcript_sync", fake_fetch)

        result = await youtube(
            action="transcript",
            url="https://www.youtube.com/watch?v=jNQXAC9IVRw",
        )
        assert "Error" in result
        assert "disabled" in result.lower()

    @pytest.mark.asyncio
    async def test_request_blocked_propagates(self, monkeypatch):
        from youtube_transcript_api import RequestBlocked

        def fake_fetch(video_id, languages):
            del languages
            raise RequestBlocked(video_id)
        monkeypatch.setattr(_yt_module, "_fetch_transcript_sync", fake_fetch)

        # Disable the yt-dlp fallback so this test isolates the
        # RequestBlocked → user-facing error mapping. Fallback success
        # is exercised separately in TestYtDlpTranscriptFallback.
        async def _no_fallback(video_id, languages):
            del video_id, languages
            return None
        monkeypatch.setattr(
            _yt_module, "_yt_dlp_transcript_fallback", _no_fallback,
        )

        result = await youtube(
            action="transcript",
            url="https://www.youtube.com/watch?v=jNQXAC9IVRw",
        )
        assert "Error" in result
        assert "bot" in result.lower()

    @pytest.mark.asyncio
    async def test_normalizes_embedded_newlines(self, monkeypatch):
        # YouTube caption cues frequently contain embedded \n for display
        # wrapping. Each segment must render as one coherent line.
        snippets = [
            _FakeSnippet(0.0, 2.0, "First line of\ncaption"),
            _FakeSnippet(2.0, 2.0, "Second  \n  line\nhere"),
        ]

        def fake_fetch(video_id, languages):
            del video_id, languages
            return _FakeFetchedTranscript(snippets, "en", is_generated=False)
        monkeypatch.setattr(_yt_module, "_fetch_transcript_sync", fake_fetch)

        result = await youtube(
            action="transcript",
            url="https://www.youtube.com/watch?v=jNQXAC9IVRw",
        )
        assert "First line of caption" in result
        assert "Second line here" in result

    @pytest.mark.asyncio
    async def test_empty_snippets_handled(self, monkeypatch):
        def fake_fetch(video_id, languages):
            del video_id, languages
            return _FakeFetchedTranscript([], "en", is_generated=False)
        monkeypatch.setattr(_yt_module, "_fetch_transcript_sync", fake_fetch)

        result = await youtube(
            action="transcript",
            url="https://www.youtube.com/watch?v=jNQXAC9IVRw",
        )
        assert "Error" in result
        assert "no segments" in result.lower()


# ---------------------------------------------------------------------------
# _TranscriptCache and _TranscriptEntry
# ---------------------------------------------------------------------------

def _multi_window_snippets(n_windows: int = 3, per_window: int = 6) -> list:
    """Build snippets that should coalesce into approximately n_windows.

    Each window covers ~30s with ``per_window`` segments of ~5s each, so
    the coalescer's [25s, 35s] band cuts cleanly between windows.
    """
    snippets = []
    t = 0.0
    for w in range(n_windows):
        for i in range(per_window):
            snippets.append(_FakeSnippet(
                t, 5.0,
                f"window {w} segment {i}: keyword{w}_{i} the rest is filler.",
            ))
            t += 5.0
        # Big gap between windows to force a clean cut
        t += 2.0
    return snippets


def _build_entry(language_code="en", is_generated=False, snippets=None):
    """Construct a _TranscriptEntry directly from snippets, bypassing the cache."""
    if snippets is None:
        snippets = _multi_window_snippets()
    fetched = _FakeFetchedTranscript(snippets, language_code, is_generated)
    return _yt_module._build_transcript_entry(
        canonical_url="https://www.youtube.com/watch?v=test",
        video_id="test",
        fetched=fetched,
    )


class TestTranscriptCache:
    def test_store_and_get_promotes(self):
        cache = _yt_module._TranscriptCache(max_entries=4)
        entry = _build_entry()
        cache.store(entry.url, entry)
        # First get promotes from probation to protected
        assert cache.get(entry.url) is entry
        # Probation should now be empty for this URL
        assert entry.url not in cache._probation
        assert entry.url in cache._protected

    def test_store_replaces_in_place(self):
        cache = _yt_module._TranscriptCache(max_entries=4)
        e1 = _build_entry()
        cache.store(e1.url, e1)
        e2 = _build_entry()  # Different instance, same URL
        cache.store(e1.url, e2)
        # No eviction triggered, still single entry
        assert cache._total() == 1
        assert cache.get(e1.url) is e2

    def test_eviction_prefers_probation(self):
        cache = _yt_module._TranscriptCache(max_entries=2)
        # Disable the group key so this test isolates the queue-priority
        # logic from group-eviction behavior (which is exercised separately
        # in TestCrossCacheGroupEviction).
        e1 = _build_entry()
        e1.url = "url_a"
        e1.group = None
        cache.store("url_a", e1)
        cache.get("url_a")  # promote
        e2 = _build_entry()
        e2.group = None
        cache.store("url_b", e2)
        e3 = _build_entry()
        e3.group = None
        cache.store("url_c", e3)
        assert "url_a" in cache._protected
        assert "url_b" not in cache._probation
        assert "url_b" not in cache._protected
        assert "url_c" in cache._probation

    def test_group_eviction_takes_protected_too(self):
        # When a probation victim has a group, ALL entries sharing that
        # group evict — including protected ones. This is the documented
        # atomicity guarantee for grouped sources.
        cache = _yt_module._TranscriptCache(max_entries=2)
        e1 = _build_entry()
        e1.url = "url_a"  # group = "yt:test"
        cache.store("url_a", e1)
        cache.get("url_a")  # promote to protected
        e2 = _build_entry()
        cache.store("url_b", e2)  # probation, same group
        e3 = _build_entry()
        cache.store("url_c", e3)  # triggers eviction; url_b is victim
        # Both url_a and url_b should evict because they share the group
        assert "url_a" not in cache._protected
        assert "url_b" not in cache._probation
        assert "url_c" in cache._probation

    def test_clear(self):
        cache = _yt_module._TranscriptCache()
        cache.store("url", _build_entry())
        cache.clear()
        assert cache._total() == 0
        assert cache.get("url") is None


class TestCrossCacheGroupEviction:
    def test_group_eviction_walks_registered_caches(self):
        # Register two transcript caches and verify group eviction visits both
        from parkour_mcp._pipeline import _evict_group, register_group_cache

        sentinel_cache = _yt_module._TranscriptCache(max_entries=4)
        register_group_cache(sentinel_cache)

        try:
            # Same group key in two different caches
            entry_a = _build_entry()
            entry_a.group = "yt:shared"
            _yt_module._transcript_cache.store("url_main", entry_a)

            entry_b = _build_entry()
            entry_b.group = "yt:shared"
            sentinel_cache.store("url_sentinel", entry_b)

            # Trigger group eviction
            _evict_group("yt:shared")

            assert _yt_module._transcript_cache.get("url_main") is None
            assert sentinel_cache.get("url_sentinel") is None
        finally:
            # Avoid leaking the sentinel cache into the global registry
            from parkour_mcp import _pipeline
            _pipeline._group_caches.remove(sentinel_cache)

    def test_evict_propagates_to_page_cache_via_group(self):
        from parkour_mcp._pipeline import _page_cache

        # Set up: a transcript entry and a page cache entry with the same group
        entry = _build_entry()
        entry.group = "yt:GROUP123"
        _yt_module._transcript_cache.store(entry.url, entry)
        _page_cache.store(
            "https://www.youtube.com/watch?v=GROUP123",
            "title", "fake markdown", group="yt:GROUP123",
        )

        # Evict the transcript entry's group → page cache should also drop
        _yt_module._transcript_cache._evict()

        assert _yt_module._transcript_cache.get(entry.url) is None
        assert _page_cache.get(
            "https://www.youtube.com/watch?v=GROUP123"
        ) is None


class TestTranscriptEntryLifecycle:
    def test_windows_built_eagerly(self):
        entry = _build_entry()
        assert len(entry.windows) >= 2
        # Index has NOT been built yet
        assert entry.is_built is False

    def test_index_built_lazily_on_search(self):
        entry = _build_entry()
        assert entry.is_built is False
        entry.search("keyword0_0")
        assert entry.is_built is True

    def test_search_result_indices_match_windows(self):
        entry = _build_entry()
        matched, _warnings = entry.search("keyword1_3")
        # keyword1_3 is in window 1
        assert 1 in matched

    def test_empty_query_returns_all_windows(self):
        entry = _build_entry()
        matched, _ = entry.search(None)
        assert sorted(matched) == list(range(len(entry.windows)))


class TestTranscriptSchema:
    def test_schema_field_types(self):
        schema = _yt_module._TranscriptEntry._get_schema()
        # Tantivy schemas don't expose field metadata in a stable way for
        # introspection, so just verify build succeeded and re-fetching
        # returns the same instance.
        assert schema is not None
        again = _yt_module._TranscriptEntry._get_schema()
        assert schema is again

    def test_schema_supports_range_query(self):
        # Smoke: building a range query on start_seconds doesn't raise
        schema = _yt_module._TranscriptEntry._get_schema()
        q = tantivy.Query.range_query(
            schema, "start_seconds", tantivy.FieldType.Float,
            0.0, 30.0, include_lower=True, include_upper=False,
        )
        assert q is not None


class TestTranscriptSearch:
    def test_bm25_query_returns_matched_window(self):
        entry = _build_entry()
        matched, warnings = entry.search("keyword2_4")
        assert warnings == []
        # Should match exactly one window — the one whose segments contain
        # that keyword. Don't pin to a specific index because the
        # sentence-aware coalescer's window-count depends on pause vs
        # punctuation interaction in the synthetic fixture.
        assert len(matched) == 1
        body = " ".join(s.text for s in entry.windows[matched[0]].segments)
        assert "keyword2_4" in body

    def test_bm25_no_match(self):
        entry = _build_entry()
        matched, warnings = entry.search("absolutelynothingmatchesthis")
        assert matched == []
        assert warnings == []

    def test_range_filter_only(self):
        entry = _build_entry()
        # First window covers 0..30s; range 0..30 should match window 0
        matched, _ = entry.search(None, start_seconds=0.0, end_seconds=30.0)
        assert 0 in matched
        # Window 1 starts after window 0; 0..30 may or may not include it
        # depending on exact boundary

    def test_range_filter_excludes_outside(self):
        entry = _build_entry()
        last = entry.segments[-1].end
        # Range entirely beyond transcript should match nothing
        matched, _ = entry.search(
            None, start_seconds=last + 100, end_seconds=last + 200,
        )
        assert matched == []

    def test_combined_query_and_range(self):
        # Locate the window containing keyword1_3 by query, then verify a
        # range spanning that window plus its query both hit it.
        entry = _build_entry()
        bare_match, _ = entry.search("keyword1_3")
        assert len(bare_match) == 1
        target_idx = bare_match[0]
        target = entry.windows[target_idx]
        matched, _ = entry.search(
            "keyword1_3",
            start_seconds=target.start, end_seconds=target.end + 0.1,
        )
        assert matched == [target_idx]

    def test_combined_query_excluded_by_range(self):
        # Restrict to window 0's time range; query for keyword in a later
        # window. Must return empty.
        entry = _build_entry()
        win0 = entry.windows[0]
        matched, _ = entry.search(
            "keyword2_4",
            start_seconds=win0.start, end_seconds=win0.end,
        )
        assert matched == []

    def test_order_by_time(self):
        entry = _build_entry()
        # Match all windows in time order
        matched, _ = entry.search(None, order="time")
        # Should be sorted by start_seconds ascending = window indices in order
        assert matched == sorted(matched)


# ---------------------------------------------------------------------------
# Window retrieval action
# ---------------------------------------------------------------------------

class TestWindowRetrievalAction:
    @pytest.mark.asyncio
    async def test_retrieve_specific_windows(self, monkeypatch):
        snippets = _multi_window_snippets(n_windows=3)

        def fake_fetch(video_id, languages):
            del video_id, languages
            return _FakeFetchedTranscript(snippets, "en", is_generated=False)
        monkeypatch.setattr(_yt_module, "_fetch_transcript_sync", fake_fetch)

        result = await youtube(
            action="transcript",
            url="https://www.youtube.com/watch?v=jNQXAC9IVRw",
            windows=[0, 2],
        )
        assert "matched_windows:" in result
        assert "requested_windows:" in result
        # Window 1 content should NOT appear in body
        # (look for keyword unique to window 1)
        # Body lives after the second `---`; check window 0 and 2 are present
        assert "window 0 segment" in result
        assert "window 2 segment" in result

    @pytest.mark.asyncio
    async def test_out_of_range_windows_reported(self, monkeypatch):
        snippets = _multi_window_snippets(n_windows=2)

        def fake_fetch(video_id, languages):
            del video_id, languages
            return _FakeFetchedTranscript(snippets, "en", is_generated=False)
        monkeypatch.setattr(_yt_module, "_fetch_transcript_sync", fake_fetch)

        result = await youtube(
            action="transcript",
            url="https://www.youtube.com/watch?v=jNQXAC9IVRw",
            windows=[0, 99],
        )
        assert "unknown_windows:" in result
        assert "99" in result

    @pytest.mark.asyncio
    async def test_all_invalid_windows_emit_note(self, monkeypatch):
        snippets = _multi_window_snippets(n_windows=2)

        def fake_fetch(video_id, languages):
            del video_id, languages
            return _FakeFetchedTranscript(snippets, "en", is_generated=False)
        monkeypatch.setattr(_yt_module, "_fetch_transcript_sync", fake_fetch)

        result = await youtube(
            action="transcript",
            url="https://www.youtube.com/watch?v=jNQXAC9IVRw",
            windows=[100, 200],
        )
        assert "note:" in result.lower()


# ---------------------------------------------------------------------------
# Search action via dispatcher
# ---------------------------------------------------------------------------

class TestSearchAction:
    @pytest.mark.asyncio
    async def test_search_returns_matched_windows(self, monkeypatch):
        snippets = _multi_window_snippets(n_windows=3)

        def fake_fetch(video_id, languages):
            del video_id, languages
            return _FakeFetchedTranscript(snippets, "en", is_generated=False)
        monkeypatch.setattr(_yt_module, "_fetch_transcript_sync", fake_fetch)

        result = await youtube(
            action="transcript",
            url="https://www.youtube.com/watch?v=jNQXAC9IVRw",
            search="keyword1_2",
        )
        assert "search: keyword1_2" in result
        assert "matched_windows:" in result
        # Hint for context retrieval
        assert "hint:" in result.lower()
        assert "windows=" in result

    @pytest.mark.asyncio
    async def test_search_with_time_range(self, monkeypatch):
        snippets = _multi_window_snippets(n_windows=3)

        def fake_fetch(video_id, languages):
            del video_id, languages
            return _FakeFetchedTranscript(snippets, "en", is_generated=False)
        monkeypatch.setattr(_yt_module, "_fetch_transcript_sync", fake_fetch)

        result = await youtube(
            action="transcript",
            url="https://www.youtube.com/watch?v=jNQXAC9IVRw",
            search="keyword1_2",
            start_seconds=30.0,
            end_seconds=70.0,
        )
        # Range echoed in frontmatter
        assert "start_seconds:" in result
        assert "end_seconds:" in result

    @pytest.mark.asyncio
    async def test_range_only_no_query(self, monkeypatch):
        snippets = _multi_window_snippets(n_windows=3)

        def fake_fetch(video_id, languages):
            del video_id, languages
            return _FakeFetchedTranscript(snippets, "en", is_generated=False)
        monkeypatch.setattr(_yt_module, "_fetch_transcript_sync", fake_fetch)

        result = await youtube(
            action="transcript",
            url="https://www.youtube.com/watch?v=jNQXAC9IVRw",
            start_seconds=0.0,
            end_seconds=35.0,
        )
        # Should match window 0 (0-30s) but not necessarily window 2
        assert "matched_windows:" in result

    @pytest.mark.asyncio
    async def test_range_outside_transcript(self, monkeypatch):
        snippets = _multi_window_snippets(n_windows=2)

        def fake_fetch(video_id, languages):
            del video_id, languages
            return _FakeFetchedTranscript(snippets, "en", is_generated=False)
        monkeypatch.setattr(_yt_module, "_fetch_transcript_sync", fake_fetch)

        result = await youtube(
            action="transcript",
            url="https://www.youtube.com/watch?v=jNQXAC9IVRw",
            start_seconds=10000.0,
            end_seconds=20000.0,
        )
        # _build_frontmatter renders an empty list as a bare key followed
        # by a newline (no list items). Look for the key + the note, not
        # a literal "[]" rendering.
        assert "matched_windows:\n" in result
        assert "note:" in result.lower()
        assert "transcript spans" in result.lower()

    @pytest.mark.asyncio
    async def test_order_time_echoed(self, monkeypatch):
        snippets = _multi_window_snippets(n_windows=3)

        def fake_fetch(video_id, languages):
            del video_id, languages
            return _FakeFetchedTranscript(snippets, "en", is_generated=False)
        monkeypatch.setattr(_yt_module, "_fetch_transcript_sync", fake_fetch)

        result = await youtube(
            action="transcript",
            url="https://www.youtube.com/watch?v=jNQXAC9IVRw",
            search="keyword",  # match-all-ish
            order="time",
        )
        assert "order: time" in result

    @pytest.mark.asyncio
    async def test_score_order_not_echoed(self, monkeypatch):
        snippets = _multi_window_snippets(n_windows=3)

        def fake_fetch(video_id, languages):
            del video_id, languages
            return _FakeFetchedTranscript(snippets, "en", is_generated=False)
        monkeypatch.setattr(_yt_module, "_fetch_transcript_sync", fake_fetch)

        result = await youtube(
            action="transcript",
            url="https://www.youtube.com/watch?v=jNQXAC9IVRw",
            search="keyword1_2",
        )
        # Default order, should NOT appear in frontmatter
        assert "order:" not in result


# ---------------------------------------------------------------------------
# Dispatcher validation
# ---------------------------------------------------------------------------

class TestDispatcherValidation:
    @pytest.mark.asyncio
    async def test_search_and_windows_mutually_exclusive(self):
        result = await youtube(
            action="transcript",
            url="https://www.youtube.com/watch?v=jNQXAC9IVRw",
            search="foo",
            windows=[0],
        )
        assert "Error" in result
        assert "mutually exclusive" in result.lower()

    @pytest.mark.asyncio
    async def test_windows_and_range_incompatible(self):
        result = await youtube(
            action="transcript",
            url="https://www.youtube.com/watch?v=jNQXAC9IVRw",
            windows=[0],
            start_seconds=10.0,
        )
        assert "Error" in result
        assert "windows" in result.lower()

    @pytest.mark.asyncio
    async def test_inverted_range_rejected(self):
        result = await youtube(
            action="transcript",
            url="https://www.youtube.com/watch?v=jNQXAC9IVRw",
            start_seconds=100.0,
            end_seconds=10.0,
        )
        assert "Error" in result
        assert "start_seconds" in result.lower()


# ---------------------------------------------------------------------------
# Caching behavior via dispatcher
# ---------------------------------------------------------------------------

class TestTranscriptCacheBehavior:
    @pytest.mark.asyncio
    async def test_second_call_hits_cache(self, monkeypatch):
        snippets = _multi_window_snippets(n_windows=2)
        fetch_count = {"n": 0}

        def fake_fetch(video_id, languages):
            del video_id, languages
            fetch_count["n"] += 1
            return _FakeFetchedTranscript(snippets, "en", is_generated=False)
        monkeypatch.setattr(_yt_module, "_fetch_transcript_sync", fake_fetch)

        await youtube(
            action="transcript",
            url="https://www.youtube.com/watch?v=jNQXAC9IVRw",
        )
        await youtube(
            action="transcript",
            url="https://www.youtube.com/watch?v=jNQXAC9IVRw",
            search="keyword0_1",
        )
        # Only one fetch — second call hit cache
        assert fetch_count["n"] == 1


# ---------------------------------------------------------------------------
# Chapter integration: helpers, rendering, fetch, dispatch
# ---------------------------------------------------------------------------

class TestChapterHelpers:
    def test_window_chapter_title_inside(self):
        ch = (
            Chapter(start_time=0.0, end_time=60.0, title="Intro"),
            Chapter(start_time=60.0, end_time=180.0, title="Demo"),
        )
        # Window starting at 30s is inside Intro
        w = Window(start=30.0, end=55.0, segments=())
        assert _window_chapter_title(w, ch) == "Intro"
        # Window starting at 60s is inside Demo (half-open)
        w = Window(start=60.0, end=90.0, segments=())
        assert _window_chapter_title(w, ch) == "Demo"

    def test_window_chapter_title_outside(self):
        # No chapters → empty string
        assert _window_chapter_title(
            Window(start=10.0, end=20.0, segments=()), (),
        ) == ""

    def test_window_chapter_title_after_last(self):
        ch = (Chapter(start_time=0.0, end_time=60.0, title="Only"),)
        # Window starting after the last chapter ends → empty
        w = Window(start=100.0, end=130.0, segments=())
        assert _window_chapter_title(w, ch) == ""

    def test_build_chapter_marks(self):
        windows = [
            Window(start=0.0, end=30.0, segments=()),
            Window(start=30.0, end=60.0, segments=()),
            Window(start=60.0, end=90.0, segments=()),
        ]
        chapters = (
            Chapter(start_time=0.0, end_time=60.0, title="First"),
            Chapter(start_time=60.0, end_time=120.0, title="Second"),
        )
        marks = _build_chapter_marks(windows, chapters)
        # Each window maps to a list of chapters; closely-spaced chapters
        # stack rather than overwrite.
        assert list(marks.keys()) == [0, 2]
        assert [c.title for c in marks[0]] == ["First"]
        assert [c.title for c in marks[2]] == ["Second"]

    def test_build_chapter_marks_no_match(self):
        windows = [Window(start=0.0, end=10.0, segments=())]
        chapters = (Chapter(start_time=100.0, end_time=200.0, title="Late"),)
        marks = _build_chapter_marks(windows, chapters)
        assert marks == {}

    def test_closely_spaced_chapters_stack_in_same_window(self):
        # Regression for UAT bug: two chapters 19s apart both fall into
        # the same ~30s window. The earlier (and later) implementation
        # used setdefault on a single-title-per-window dict, silently
        # dropping the later chapter. Both must now render.
        windows = [
            Window(start=0.0, end=30.0, segments=()),
            Window(start=30.0, end=60.0, segments=()),
            Window(start=60.0, end=90.0, segments=()),
            Window(start=90.0, end=126.0, segments=()),
            Window(start=126.0, end=156.0, segments=()),
        ]
        chapters = (
            Chapter(start_time=0.0, end_time=19.0, title="Untitled Chapter 1"),
            Chapter(start_time=19.0, end_time=103.0, title="What Is Spiciness"),
            Chapter(start_time=103.0, end_time=122.0, title="The Scoville Scale"),
            Chapter(start_time=122.0, end_time=200.0, title="Trinidad Moruga Scorpion"),
        )
        marks = _build_chapter_marks(windows, chapters)
        # Chapters 1 and 2 each fall into their own window
        assert [c.title for c in marks[0]] == ["Untitled Chapter 1"]
        assert [c.title for c in marks[1]] == ["What Is Spiciness"]
        # Chapters 3 and 4 both first-cross at window 4 (start=126).
        # Both must appear, in order.
        assert [c.title for c in marks[4]] == [
            "The Scoville Scale",
            "Trinidad Moruga Scorpion",
        ]


class TestRenderCompactWithChapters:
    def test_compact_emits_chapter_headings(self):
        windows = [
            Window(start=0.0, end=30.0, segments=(
                Segment(0.0, 5.0, "Welcome"),
            )),
            Window(start=60.0, end=90.0, segments=(
                Segment(60.0, 5.0, "Demo content"),
            )),
        ]
        chapters = (
            Chapter(start_time=0.0, end_time=60.0, title="Intro"),
            Chapter(start_time=60.0, end_time=120.0, title="Demo"),
        )
        out = render_transcript(windows, mode="compact", chapters=chapters)
        # Heading timestamps use chapter.start_time, not window.start
        assert "## [00:00] Intro" in out
        assert "## [01:00] Demo" in out
        # Window anchors still present below the headings
        assert "[00:00]" in out
        assert "[01:00]" in out

    def test_compact_stacks_closely_spaced_chapters(self):
        # Two chapters falling into the same window must both render,
        # each with its own start_time as the heading timestamp.
        windows = [
            Window(start=0.0, end=30.0, segments=(
                Segment(0.0, 5.0, "Welcome"),
            )),
            Window(start=126.0, end=156.0, segments=(
                Segment(126.0, 5.0, "Later content"),
            )),
        ]
        chapters = (
            Chapter(start_time=103.0, end_time=122.0, title="The Scoville Scale"),
            Chapter(start_time=122.0, end_time=200.0, title="Trinidad Moruga Scorpion"),
        )
        out = render_transcript(windows, mode="compact", chapters=chapters)
        # Both headings present, with chapter-time stamps
        assert "## [01:43] The Scoville Scale" in out
        assert "## [02:02] Trinidad Moruga Scorpion" in out
        # And they appear in chronological order (Scoville before Trinidad)
        assert out.index("Scoville Scale") < out.index("Trinidad Moruga")
        # Window anchor still renders, separately from the headings
        assert "[02:06]" in out

    def test_compact_without_chapters_no_headings(self):
        windows = [
            Window(start=0.0, end=30.0, segments=(Segment(0.0, 5.0, "Hi"),)),
        ]
        out = render_transcript(windows, mode="compact")
        assert "##" not in out

    def test_chapters_only_for_compact_mode(self):
        windows = [
            Window(start=0.0, end=10.0, segments=(Segment(0.0, 5.0, "Hi"),)),
        ]
        chapters = (Chapter(start_time=0.0, end_time=60.0, title="Intro"),)
        # Other modes ignore chapters — they target programmatic / per-line
        # consumption where chapter headings would interrupt cadence.
        for mode in ("absolute", "none", "structured"):
            out = render_transcript(windows, mode=mode, chapters=chapters)
            assert "## [" not in out


class TestFetchVideoChaptersSync:
    def test_parses_well_formed_chapters(self, monkeypatch):
        info = {
            "chapters": [
                {"start_time": 0.0, "end_time": 60.0, "title": "Intro"},
                {"start_time": 60.0, "end_time": 180.0, "title": "Demo"},
            ],
        }
        monkeypatch.setattr(
            _yt_module, "_extract_video_info_sync", lambda _: info,
        )
        # Use the pre-fixture reference to bypass the autouse stub
        chapters = _REAL_FETCH_CHAPTERS("vid")
        assert len(chapters) == 2
        assert chapters[0].title == "Intro"
        assert chapters[0].start_time == 0.0
        assert chapters[0].end_time == 60.0

    def test_skips_malformed_entries(self, monkeypatch):
        info = {
            "chapters": [
                {"start_time": 0.0, "end_time": 60.0, "title": "Good"},
                {"start_time": 60.0},  # missing title and end_time
                "not a dict",
                {"title": "no times"},  # missing both times
            ],
        }
        monkeypatch.setattr(
            _yt_module, "_extract_video_info_sync", lambda _: info,
        )
        chapters = _REAL_FETCH_CHAPTERS("vid")
        assert len(chapters) == 1
        assert chapters[0].title == "Good"

    def test_empty_chapters_returns_empty(self, monkeypatch):
        monkeypatch.setattr(
            _yt_module, "_extract_video_info_sync",
            lambda _: {"chapters": []},
        )
        assert _REAL_FETCH_CHAPTERS("vid") == []

    def test_extract_failure_returns_empty(self, monkeypatch):
        def _boom(_):
            raise RuntimeError("yt-dlp blew up")
        monkeypatch.setattr(_yt_module, "_extract_video_info_sync", _boom)
        assert _REAL_FETCH_CHAPTERS("vid") == []


class TestYtDlpInfoCache:
    @pytest.mark.asyncio
    async def test_video_then_transcript_shares_extract(self, monkeypatch):
        """A video action followed by transcript on the same URL should
        trigger only ONE yt-dlp extract_info call. The chapter fetch on
        the transcript path reuses the cached info dict.
        """
        # Patch the sanitized-info entry in the singleton's extract path.
        # We can't easily mock _extract_video_info_sync because that's the
        # function under test; instead, mock the underlying ydl methods.
        info = {
            "id": "vid",
            "title": "Test",
            "description": "Desc",
            "duration": 60.0,
            "upload_date": "20260101",
            "channel": "Chan",
            "channel_id": "UCx",
            "channel_url": "https://example",
            "subtitles": {},
            "automatic_captions": {},
            "chapters": [
                {"start_time": 0.0, "end_time": 30.0, "title": "A"},
            ],
        }
        call_count = {"n": 0}

        class _CountingYdl:
            def extract_info(self, url, download=False):
                del url, download
                call_count["n"] += 1
                return info

            def sanitize_info(self, raw):
                return raw

        # Patch the singleton getter to return a counting fake
        monkeypatch.setattr(_yt_module, "_get_ydl_video", lambda: _CountingYdl())

        # Also stub the transcript-api fetch so we don't hit the network
        def fake_transcript(video_id, languages):
            del video_id, languages
            return _FakeFetchedTranscript(_ZOO_SNIPPETS, "en", is_generated=False)
        monkeypatch.setattr(_yt_module, "_fetch_transcript_sync", fake_transcript)

        # First call: video — should miss cache and fetch once
        await youtube(
            action="video",
            url="https://www.youtube.com/watch?v=jNQXAC9IVRw",
        )
        assert call_count["n"] == 1

        # Second call: transcript on the SAME URL. The chapter fetch
        # should hit the cached info dict, not re-extract.
        await youtube(
            action="transcript",
            url="https://www.youtube.com/watch?v=jNQXAC9IVRw",
        )
        assert call_count["n"] == 1, (
            f"transcript triggered an extra extract_info "
            f"(total calls: {call_count['n']})"
        )

    @pytest.mark.asyncio
    async def test_cache_lru_eviction(self, monkeypatch):
        """Cache evicts oldest entry when capacity is exceeded."""
        # Stub the underlying extract so each URL produces a distinct dict
        class _UrlYdl:
            def extract_info(self, url, download=False):
                del download
                return {"id": url, "title": f"Title for {url}"}

            def sanitize_info(self, raw):
                return raw

        monkeypatch.setattr(_yt_module, "_get_ydl_video", lambda: _UrlYdl())

        # Reach into the module-level cap for the assertion
        cap = _yt_module._YT_INFO_CACHE_MAX

        # Populate cap+1 distinct URLs; the oldest should evict
        for i in range(cap + 1):
            _yt_module._extract_video_info_sync(f"https://example.com/{i}")

        cache = _yt_module._yt_info_cache
        assert len(cache) == cap
        # Oldest entry (i=0) evicted
        assert "https://example.com/0" not in cache
        # Most recent entry retained
        assert f"https://example.com/{cap}" in cache


class TestChaptersInTranscriptResponse:
    @pytest.mark.asyncio
    async def test_chapters_surface_in_frontmatter(self, monkeypatch):
        chapters_data = [
            Chapter(start_time=0.0, end_time=10.0, title="Intro"),
            Chapter(start_time=10.0, end_time=20.0, title="Outro"),
        ]
        # Override the offline chapter stub for this test
        monkeypatch.setattr(
            _yt_module, "_fetch_video_chapters_sync", lambda _: chapters_data,
        )

        def fake_fetch(video_id, languages):
            del video_id, languages
            return _FakeFetchedTranscript(_ZOO_SNIPPETS, "en", is_generated=False)
        monkeypatch.setattr(_yt_module, "_fetch_transcript_sync", fake_fetch)

        result = await youtube(
            action="transcript",
            url="https://www.youtube.com/watch?v=jNQXAC9IVRw",
        )
        frontmatter, _, body = result.partition("\n\n")
        # Frontmatter carries only the chapter count (numeric, structurally
        # safe); the chapter list with user-generated titles renders inside
        # the fenced body as a "## Chapters" TOC.
        assert "chapter_count: 2" in frontmatter
        assert "chapters:" not in frontmatter  # the list itself stays out
        assert "## Chapters" in body
        assert "[00:00] Intro" in body
        assert "[00:10] Outro" in body
        # Compact-mode body emits chapter heading at the chapter's
        # declared start_time (not the window's anchor)
        assert "## [00:00] Intro" in body

    @pytest.mark.asyncio
    async def test_chapter_filter_scopes_search(self, monkeypatch):
        snippets = _multi_window_snippets(n_windows=3)
        chapters_data = [
            Chapter(start_time=0.0, end_time=32.0, title="Intro"),
            Chapter(start_time=32.0, end_time=64.0, title="Middle"),
            Chapter(start_time=64.0, end_time=200.0, title="End"),
        ]
        monkeypatch.setattr(
            _yt_module, "_fetch_video_chapters_sync", lambda _: chapters_data,
        )

        def fake_fetch(video_id, languages):
            del video_id, languages
            return _FakeFetchedTranscript(snippets, "en", is_generated=False)
        monkeypatch.setattr(_yt_module, "_fetch_transcript_sync", fake_fetch)

        result = await youtube(
            action="transcript",
            url="https://www.youtube.com/watch?v=jNQXAC9IVRw",
            chapter="Middle",
        )
        assert "chapter: Middle" in result
        assert "matched_windows:" in result

    @pytest.mark.asyncio
    async def test_chapter_filter_no_matches_emits_note(self, monkeypatch):
        snippets = _multi_window_snippets(n_windows=2)
        chapters_data = [
            Chapter(start_time=0.0, end_time=64.0, title="Intro"),
        ]
        monkeypatch.setattr(
            _yt_module, "_fetch_video_chapters_sync", lambda _: chapters_data,
        )

        def fake_fetch(video_id, languages):
            del video_id, languages
            return _FakeFetchedTranscript(snippets, "en", is_generated=False)
        monkeypatch.setattr(_yt_module, "_fetch_transcript_sync", fake_fetch)

        result = await youtube(
            action="transcript",
            url="https://www.youtube.com/watch?v=jNQXAC9IVRw",
            chapter="NonexistentChapter",
        )
        # Note lists the available chapters so the LLM can retry with a
        # valid filter rather than guessing
        assert "Available chapters" in result
        assert "Intro" in result

    @pytest.mark.asyncio
    async def test_windows_with_chapter_rejected(self):
        result = await youtube(
            action="transcript",
            url="https://www.youtube.com/watch?v=jNQXAC9IVRw",
            windows=[0],
            chapter="Intro",
        )
        assert "Error" in result
        assert "windows" in result.lower()
        assert "chapter" in result.lower()

    @pytest.mark.asyncio
    async def test_chapter_fetch_failure_degrades_silently(self, monkeypatch):
        def boom(_):
            raise RuntimeError("network died")
        monkeypatch.setattr(_yt_module, "_fetch_video_chapters_sync", boom)

        def fake_fetch(video_id, languages):
            del video_id, languages
            return _FakeFetchedTranscript(_ZOO_SNIPPETS, "en", is_generated=False)
        monkeypatch.setattr(_yt_module, "_fetch_transcript_sync", fake_fetch)

        result = await youtube(
            action="transcript",
            url="https://www.youtube.com/watch?v=jNQXAC9IVRw",
        )
        # Transcript content present
        assert "All right, so here we are" in result
        # No chapters in frontmatter
        assert "\nchapters:" not in result


# ---------------------------------------------------------------------------
# Channel and playlist actions
# ---------------------------------------------------------------------------

_CHANNEL_INFO = {
    "_type": "playlist",
    "id": "UCBJycsmduvYEL83R_U4JriQ",
    "title": "MKBHD",
    "channel": "MKBHD",
    "channel_id": "UCBJycsmduvYEL83R_U4JriQ",
    "channel_url": "https://www.youtube.com/@MKBHD",
    "uploader": "MKBHD",
    "uploader_id": "@MKBHD",
    "uploader_url": "https://www.youtube.com/@MKBHD",
    "webpage_url": "https://www.youtube.com/@MKBHD",
    "channel_follower_count": 18_500_000,
    "playlist_count": 1500,
    "description": "Quality Tech Videos.",
    "entries": [
        {
            "id": f"vid{i:02d}",
            "title": f"Video {i}",
            "duration": 600 + i * 30,
            "view_count": 100_000 + i * 1000,
        }
        for i in range(5)
    ],
}

_PLAYLIST_INFO = {
    "_type": "playlist",
    "id": "PLrAXtmRdnEQy6nuLMt9H1Pj7RgqZTjB",
    "title": "Curated Reading",
    "uploader": "Some User",
    "uploader_id": "@SomeUser",
    "uploader_url": "https://www.youtube.com/@SomeUser",
    "webpage_url": (
        "https://www.youtube.com/playlist?list=PLrAXtmRdnEQy6nuLMt9H1Pj7RgqZTjB"
    ),
    "modified_date": "20260301",
    "playlist_count": 12,
    "description": "Things to watch.",
    "entries": [
        {"id": f"item{i}", "title": f"Item {i}", "duration": 300}
        for i in range(3)
    ],
}


class TestChannelAction:
    @pytest.mark.asyncio
    async def test_channel_returns_frontmatter_and_entries(self, monkeypatch):
        def fake_extract(url, limit):
            del url, limit
            return _CHANNEL_INFO
        monkeypatch.setattr(_yt_module, "_extract_flat_sync", fake_extract)

        result = await youtube(
            action="channel",
            url="https://www.youtube.com/@MKBHD",
        )
        frontmatter, _, body = result.partition("\n\n")
        # Structurally-validated fields stay in frontmatter
        assert "channel_id: UCBJycsmduvYEL83R_U4JriQ" in frontmatter
        assert "follower_count: 18500000" in frontmatter
        assert "total_videos: 1500" in frontmatter
        assert "returned_videos: 5" in frontmatter
        # User-generated channel name and title NOT in frontmatter
        assert "title: MKBHD" not in frontmatter
        assert "channel: MKBHD" not in frontmatter
        # They appear in the fenced body
        assert "# MKBHD" in body  # fence heading
        assert "Quality Tech Videos." in body
        assert "Recent uploads (5)" in body
        assert "Video 0" in body
        assert "https://www.youtube.com/watch?v=vid00" in body
        assert "untrusted content" in result

    @pytest.mark.asyncio
    async def test_channel_no_url(self):
        result = await youtube(action="channel")
        assert "Error" in result and "url" in result.lower()

    @pytest.mark.asyncio
    async def test_channel_video_url_rejected(self):
        result = await youtube(
            action="channel",
            url="https://www.youtube.com/watch?v=jNQXAC9IVRw",
        )
        assert "Error" in result
        assert "channel" in result.lower()

    @pytest.mark.asyncio
    async def test_channel_music_url_rejected(self):
        result = await youtube(
            action="channel",
            url="https://music.youtube.com/channel/UCxxx",
        )
        assert "Error" in result
        assert "music" in result.lower()

    @pytest.mark.asyncio
    async def test_channel_limit_clamped(self, monkeypatch):
        observed = {"limit": None}

        def fake_extract(url, limit):
            del url
            observed["limit"] = limit
            return _CHANNEL_INFO
        monkeypatch.setattr(_yt_module, "_extract_flat_sync", fake_extract)

        await youtube(
            action="channel",
            url="https://www.youtube.com/@MKBHD",
            limit=999,  # over max
        )
        # _LIST_LIMIT_MAX = 200; should clamp
        assert observed["limit"] == 200

        await youtube(
            action="channel",
            url="https://www.youtube.com/@MKBHD",
            limit=0,  # under min
        )
        assert observed["limit"] == 1

    @pytest.mark.asyncio
    async def test_channel_empty_entries(self, monkeypatch):
        info = dict(_CHANNEL_INFO)
        info["entries"] = []
        info["playlist_count"] = 0

        def fake_extract(url, limit):
            del url, limit
            return info
        monkeypatch.setattr(_yt_module, "_extract_flat_sync", fake_extract)

        result = await youtube(
            action="channel",
            url="https://www.youtube.com/@MKBHD",
        )
        assert "Recent uploads (0)" in result
        assert "(no entries)" in result

    @pytest.mark.asyncio
    async def test_channel_bot_detection_error(self, monkeypatch):
        from yt_dlp.utils import ExtractorError  # type: ignore[import-not-found]
        exc = ExtractorError("Sign in to confirm you're not a bot.")

        def fake_extract(url, limit):
            del url, limit
            raise exc
        monkeypatch.setattr(_yt_module, "_extract_flat_sync", fake_extract)

        result = await youtube(
            action="channel",
            url="https://www.youtube.com/@MKBHD",
        )
        assert "Error" in result
        assert "bot" in result.lower()


class TestPlaylistAction:
    @pytest.mark.asyncio
    async def test_playlist_returns_frontmatter_and_items(self, monkeypatch):
        def fake_extract(url, limit):
            del url, limit
            return _PLAYLIST_INFO
        monkeypatch.setattr(_yt_module, "_extract_flat_sync", fake_extract)

        result = await youtube(
            action="playlist",
            url=(
                "https://www.youtube.com/playlist?"
                "list=PLrAXtmRdnEQy6nuLMt9H1Pj7RgqZTjB"
            ),
        )
        frontmatter, _, body = result.partition("\n\n")
        # Structurally-validated fields stay in frontmatter
        assert "uploader_id: @SomeUser" in frontmatter
        assert "last_updated: 2026-03-01" in frontmatter
        assert "total_items: 12" in frontmatter
        assert "returned_items: 3" in frontmatter
        # User-generated title and uploader display name are NOT in frontmatter
        assert "title: Curated Reading" not in frontmatter
        # `uploader_id:` is structurally constrained (the @handle form)
        # and stays in frontmatter; the bare `uploader:` display name
        # must not.
        assert "uploader: Some User" not in frontmatter
        # They render in the fenced body
        assert "# Curated Reading" in body  # fence heading
        assert "**Uploader**: [Some User]" in body
        assert "Items (3)" in body
        assert "Item 0" in body

    @pytest.mark.asyncio
    async def test_playlist_no_url(self):
        result = await youtube(action="playlist")
        assert "Error" in result and "url" in result.lower()

    @pytest.mark.asyncio
    async def test_playlist_video_url_rejected(self):
        result = await youtube(
            action="playlist",
            url="https://www.youtube.com/watch?v=jNQXAC9IVRw",
        )
        assert "Error" in result
        assert "playlist" in result.lower()

    @pytest.mark.asyncio
    async def test_playlist_channel_url_rejected(self):
        result = await youtube(
            action="playlist",
            url="https://www.youtube.com/@MKBHD",
        )
        assert "Error" in result


class TestVideoEntryFormatting:
    def test_format_with_all_meta(self):
        entry = {
            "id": "abc",
            "title": "Test Title",
            "duration": 125,
            "view_count": 12345,
            "uploader": "Channel Name",
        }
        out = _yt_module._format_video_entry(entry, index=1)
        assert "1. **Test Title**" in out
        assert "2:05" in out
        assert "12,345 views" in out
        assert "Channel Name" in out
        assert "https://www.youtube.com/watch?v=abc" in out

    def test_format_minimal(self):
        entry = {"id": "xyz", "title": "Bare"}
        out = _yt_module._format_video_entry(entry, index=2)
        assert "2. **Bare**" in out
        assert "https://www.youtube.com/watch?v=xyz" in out

    def test_format_no_id(self):
        entry = {"title": "No URL"}
        out = _yt_module._format_video_entry(entry, index=1)
        assert "1. **No URL**" in out
        assert "watch?v=" not in out

    def test_format_prefers_explicit_url(self):
        # Tab entries have an explicit url= pointing at the tab; respect it.
        entry = {
            "id": "UCxxx",
            "title": "Channel - Videos",
            "url": "https://www.youtube.com/@handle/videos",
        }
        out = _yt_module._format_video_entry(entry, index=1)
        assert "https://www.youtube.com/@handle/videos" in out
        assert "watch?v=UCxxx" not in out


class TestChannelTabListingDetection:
    @pytest.mark.asyncio
    async def test_tab_listing_emits_hint(self, monkeypatch):
        # All entries are nested playlists (_type='playlist') → tab listing.
        # webpage_url carries the tab URL on real yt-dlp output, so the
        # formatter renders that link rather than a constructed watch URL.
        info = {
            "_type": "playlist",
            "id": "UCxxx",
            "title": "TestChannel",
            "channel": "TestChannel",
            "channel_id": "UCxxx",
            "channel_url": "https://www.youtube.com/@TestChannel",
            "webpage_url": "https://www.youtube.com/@TestChannel",
            "entries": [
                {
                    "_type": "playlist",
                    "id": "UCxxx",
                    "title": "TestChannel - Videos",
                    "webpage_url": "https://www.youtube.com/@TestChannel/videos",
                },
                {
                    "_type": "playlist",
                    "id": "UCxxx",
                    "title": "TestChannel - Shorts",
                    "webpage_url": "https://www.youtube.com/@TestChannel/shorts",
                },
            ],
        }

        def fake_extract(url, limit):
            del url, limit
            return info
        monkeypatch.setattr(_yt_module, "_extract_flat_sync", fake_extract)

        result = await youtube(
            action="channel",
            url="https://www.youtube.com/@TestChannel",
        )
        assert "hint:" in result.lower()
        assert "/videos" in result
        # Body heading reflects the tab nature
        assert "## Tabs" in result
        # Tab URLs render via explicit url=, not via watch?v=
        assert "/@TestChannel/videos" in result
        assert "watch?v=UCxxx" not in result

    @pytest.mark.asyncio
    async def test_video_listing_no_tab_hint(self, monkeypatch):
        # Different ids → not a tab listing
        def fake_extract(url, limit):
            del url, limit
            return _CHANNEL_INFO
        monkeypatch.setattr(_yt_module, "_extract_flat_sync", fake_extract)

        result = await youtube(
            action="channel",
            url="https://www.youtube.com/@MKBHD/videos",
        )
        # Heading says "Recent uploads" not "Tabs"
        assert "## Recent uploads" in result
        # No tab-routing hint
        assert "tab list" not in result.lower()


# ---------------------------------------------------------------------------
# Search action
# ---------------------------------------------------------------------------

_SEARCH_INFO = {
    "_type": "playlist",
    "id": "ytsearch10:quantum entanglement",
    "title": "ytsearch10:quantum entanglement",
    "entries": [
        {
            "_type": "url",
            "id": f"vid{i}",
            "title": f"Quantum result {i}",
            "duration": 600 + i * 60,
            "view_count": 50_000 + i * 1000,
            "uploader": "Some Channel",
            "url": f"https://www.youtube.com/watch?v=vid{i}",
        }
        for i in range(5)
    ],
}


class TestSearchActionDispatcher:
    @pytest.mark.asyncio
    async def test_search_returns_results(self, monkeypatch):
        observed = {"url": None, "limit": None}

        def fake_extract(url, limit):
            observed["url"] = url
            observed["limit"] = limit
            return _SEARCH_INFO
        monkeypatch.setattr(_yt_module, "_extract_flat_sync", fake_extract)

        result = await youtube(
            action="search",
            query="quantum entanglement",
            limit=5,
        )
        # Built the right yt-dlp URL
        assert observed["url"] == "ytsearch5:quantum entanglement"
        assert observed["limit"] == 5
        # Frontmatter and body
        assert "search: quantum entanglement" in result
        assert "returned_results: 5" in result
        # _fence_content emits the title heading itself; body just lists results
        assert "Search: quantum entanglement" in result  # in fence header
        assert "## Results (5)" in result
        assert "Quantum result 0" in result
        assert "https://www.youtube.com/watch?v=vid0" in result

    @pytest.mark.asyncio
    async def test_search_no_query(self):
        result = await youtube(action="search")
        assert "Error" in result and "query" in result.lower()

    @pytest.mark.asyncio
    async def test_search_empty_query(self):
        result = await youtube(action="search", query="   ")
        assert "Error" in result

    @pytest.mark.asyncio
    async def test_search_limit_clamped(self, monkeypatch):
        observed = {"limit": None}

        def fake_extract(url, limit):
            del url
            observed["limit"] = limit
            return _SEARCH_INFO
        monkeypatch.setattr(_yt_module, "_extract_flat_sync", fake_extract)

        await youtube(action="search", query="foo", limit=999)
        assert observed["limit"] == 200
        await youtube(action="search", query="foo", limit=0)
        assert observed["limit"] == 1

    @pytest.mark.asyncio
    async def test_search_empty_results(self, monkeypatch):
        info = dict(_SEARCH_INFO)
        info["entries"] = []

        def fake_extract(url, limit):
            del url, limit
            return info
        monkeypatch.setattr(_yt_module, "_extract_flat_sync", fake_extract)

        result = await youtube(action="search", query="zzznotreallyanything")
        assert "Results (0)" in result
        assert "(no results)" in result

    @pytest.mark.asyncio
    async def test_search_bot_detection_error(self, monkeypatch):
        from yt_dlp.utils import ExtractorError  # type: ignore[import-not-found]
        exc = ExtractorError("Sign in to confirm you're not a bot.")

        def fake_extract(url, limit):
            del url, limit
            raise exc
        monkeypatch.setattr(_yt_module, "_extract_flat_sync", fake_extract)

        result = await youtube(action="search", query="anything")
        assert "Error" in result
        assert "bot" in result.lower()


# ---------------------------------------------------------------------------
# yt-dlp transcript fallback
# ---------------------------------------------------------------------------

class TestPickCaptionTrack:
    def test_manual_wins_over_auto(self):
        subs = {"en": [{"ext": "json3"}]}
        auto = {"en": [{"ext": "json3"}]}
        track, lang, is_gen = _yt_module._pick_caption_track(subs, auto, ["en"])
        assert track == subs["en"]
        assert lang == "en"
        assert is_gen is False

    def test_auto_used_when_no_manual(self):
        _, lang, is_gen = _yt_module._pick_caption_track(
            subs={}, auto={"en": [{"ext": "json3"}]}, languages=["en"],
        )
        assert lang == "en"
        assert is_gen is True

    def test_language_priority(self):
        # Try "fr" first (not present), fall through to "en"
        subs = {"en": [{"ext": "json3"}]}
        track, lang, _ = _yt_module._pick_caption_track(
            subs, {}, ["fr", "en"],
        )
        assert lang == "en"
        assert track == subs["en"]

    def test_no_match_returns_none(self):
        track, lang, _ = _yt_module._pick_caption_track({}, {}, ["en"])
        assert track is None
        assert lang is None


class TestYtDlpTranscriptFallback:
    @pytest.mark.asyncio
    async def test_fallback_success_with_manual_subs(self, monkeypatch):
        info = {
            "subtitles": {
                "en": [
                    {"ext": "vtt", "url": "https://example.com/foo.vtt"},
                    {"ext": "json3", "url": "https://example.com/foo.json3"},
                ],
            },
            "automatic_captions": {},
        }

        def fake_extract(url):
            del url
            return info
        monkeypatch.setattr(_yt_module, "_extract_video_info_sync", fake_extract)

        async def fake_fetch(url):
            assert url == "https://example.com/foo.json3"
            return tuple([
                _yt_module._FallbackSnippet(start=0.0, duration=2.0, text="hello"),
                _yt_module._FallbackSnippet(start=2.0, duration=2.0, text="world"),
            ])
        monkeypatch.setattr(_yt_module, "_fetch_and_parse_json3", fake_fetch)

        result = await _yt_module._yt_dlp_transcript_fallback("vid", ["en"])
        assert result is not None
        assert result.language_code == "en"
        assert result.is_generated is False
        assert len(result.snippets) == 2

    @pytest.mark.asyncio
    async def test_fallback_returns_none_when_no_track(self, monkeypatch):
        def fake_extract(url):
            del url
            return {"subtitles": {}, "automatic_captions": {}}
        monkeypatch.setattr(_yt_module, "_extract_video_info_sync", fake_extract)

        result = await _yt_module._yt_dlp_transcript_fallback("vid", ["en"])
        assert result is None

    @pytest.mark.asyncio
    async def test_fallback_returns_none_when_no_json3(self, monkeypatch):
        # Track exists but only has VTT, no JSON3
        info = {
            "subtitles": {
                "en": [{"ext": "vtt", "url": "https://example.com/foo.vtt"}],
            },
            "automatic_captions": {},
        }
        def fake_extract(url):
            del url
            return info
        monkeypatch.setattr(_yt_module, "_extract_video_info_sync", fake_extract)

        result = await _yt_module._yt_dlp_transcript_fallback("vid", ["en"])
        assert result is None

    @pytest.mark.asyncio
    async def test_fallback_returns_none_when_extract_raises(self, monkeypatch):
        def fake_extract(url):
            del url
            raise RuntimeError("yt-dlp blew up")
        monkeypatch.setattr(_yt_module, "_extract_video_info_sync", fake_extract)

        result = await _yt_module._yt_dlp_transcript_fallback("vid", ["en"])
        assert result is None

    @pytest.mark.asyncio
    async def test_fallback_returns_none_when_fetch_raises(self, monkeypatch):
        info = {
            "subtitles": {
                "en": [{"ext": "json3", "url": "https://example.com/foo.json3"}],
            },
            "automatic_captions": {},
        }
        def fake_extract(url):
            del url
            return info
        monkeypatch.setattr(_yt_module, "_extract_video_info_sync", fake_extract)

        async def fake_fetch(url):
            del url
            raise RuntimeError("network error")
        monkeypatch.setattr(_yt_module, "_fetch_and_parse_json3", fake_fetch)

        result = await _yt_module._yt_dlp_transcript_fallback("vid", ["en"])
        assert result is None


class TestFallbackInDispatcher:
    @pytest.mark.asyncio
    async def test_potoken_triggers_fallback(self, monkeypatch):
        from youtube_transcript_api import PoTokenRequired

        def fake_fetch_sync(video_id, languages):
            del languages
            raise PoTokenRequired(video_id)
        monkeypatch.setattr(_yt_module, "_fetch_transcript_sync", fake_fetch_sync)

        async def fake_fallback(video_id, languages):
            del video_id, languages
            return _yt_module._FallbackTranscript(
                snippets=tuple([
                    _yt_module._FallbackSnippet(0.0, 2.0, "Fallback content."),
                ]),
                language_code="en",
                is_generated=False,
            )
        monkeypatch.setattr(
            _yt_module, "_yt_dlp_transcript_fallback", fake_fallback,
        )

        result = await youtube(
            action="transcript",
            url="https://www.youtube.com/watch?v=jNQXAC9IVRw",
        )
        # Frontmatter records that the fallback was used
        assert "api: yt-dlp (fallback)" in result
        assert "Fallback content." in result

    @pytest.mark.asyncio
    async def test_request_blocked_triggers_fallback(self, monkeypatch):
        from youtube_transcript_api import RequestBlocked

        def fake_fetch_sync(video_id, languages):
            del languages
            raise RequestBlocked(video_id)
        monkeypatch.setattr(_yt_module, "_fetch_transcript_sync", fake_fetch_sync)

        async def fake_fallback(video_id, languages):
            del video_id, languages
            return _yt_module._FallbackTranscript(
                snippets=tuple([
                    _yt_module._FallbackSnippet(0.0, 2.0, "Captions via yt-dlp."),
                ]),
                language_code="en",
                is_generated=True,
            )
        monkeypatch.setattr(
            _yt_module, "_yt_dlp_transcript_fallback", fake_fallback,
        )

        result = await youtube(
            action="transcript",
            url="https://www.youtube.com/watch?v=jNQXAC9IVRw",
        )
        assert "api: yt-dlp (fallback)" in result
        assert "transcript_kind: auto" in result
        # Note must explain both directions of the recovery
        assert "RequestBlocked" in result
        assert "android_vr" in result

    @pytest.mark.asyncio
    async def test_ip_blocked_triggers_fallback_with_caveat(self, monkeypatch):
        # IpBlocked is a subclass of RequestBlocked; the note should
        # specifically mention HTTP 429 + the IP-reputation caveat
        from youtube_transcript_api import IpBlocked

        def fake_fetch_sync(video_id, languages):
            del languages
            raise IpBlocked(video_id)
        monkeypatch.setattr(_yt_module, "_fetch_transcript_sync", fake_fetch_sync)

        async def fake_fallback(video_id, languages):
            del video_id, languages
            return _yt_module._FallbackTranscript(
                snippets=tuple([
                    _yt_module._FallbackSnippet(0.0, 2.0, "Recovered."),
                ]),
                language_code="en",
                is_generated=False,
            )
        monkeypatch.setattr(
            _yt_module, "_yt_dlp_transcript_fallback", fake_fallback,
        )

        result = await youtube(
            action="transcript",
            url="https://www.youtube.com/watch?v=jNQXAC9IVRw",
        )
        assert "IpBlocked" in result
        assert "429" in result
        # The note should warn that subsequent calls may still hit the wall
        assert "may hit the same wall" in result.lower() or "IP reputation" in result

    @pytest.mark.asyncio
    async def test_fallback_failure_surfaces_original_error(self, monkeypatch):
        from youtube_transcript_api import PoTokenRequired

        def fake_fetch_sync(video_id, languages):
            del languages
            raise PoTokenRequired(video_id)
        monkeypatch.setattr(_yt_module, "_fetch_transcript_sync", fake_fetch_sync)

        async def fake_fallback(video_id, languages):
            del video_id, languages
            return None
        monkeypatch.setattr(
            _yt_module, "_yt_dlp_transcript_fallback", fake_fallback,
        )

        result = await youtube(
            action="transcript",
            url="https://www.youtube.com/watch?v=jNQXAC9IVRw",
        )
        assert "Error" in result
        assert "PoToken" in result

    @pytest.mark.asyncio
    async def test_other_errors_skip_fallback(self, monkeypatch):
        # TranscriptsDisabled is content-side; fallback shouldn't be tried
        from youtube_transcript_api import TranscriptsDisabled
        fallback_called = {"yes": False}

        def fake_fetch_sync(video_id, languages):
            del languages
            raise TranscriptsDisabled(video_id)
        monkeypatch.setattr(_yt_module, "_fetch_transcript_sync", fake_fetch_sync)

        async def fake_fallback(video_id, languages):
            del video_id, languages
            fallback_called["yes"] = True
            return None
        monkeypatch.setattr(
            _yt_module, "_yt_dlp_transcript_fallback", fake_fallback,
        )

        result = await youtube(
            action="transcript",
            url="https://www.youtube.com/watch?v=jNQXAC9IVRw",
        )
        assert "Error" in result
        assert "disabled" in result.lower()
        assert fallback_called["yes"] is False


class TestParseJson3:
    @pytest.mark.asyncio
    async def test_parse_normal(self, monkeypatch):
        # Mock httpx.AsyncClient.get to return canned JSON3
        captured_url = {"url": None}

        class _FakeResp:
            status_code = 200
            def raise_for_status(self):
                pass
            def json(self):
                return {
                    "events": [
                        {
                            "tStartMs": 1200,
                            "dDurationMs": 2160,
                            "segs": [{"utf8": "Hello "}, {"utf8": "world"}],
                        },
                        {
                            "tStartMs": 5320,
                            "dDurationMs": 2660,
                            "segs": [{"utf8": "Second cue."}],
                        },
                        # No segs → skipped
                        {"tStartMs": 8000, "dDurationMs": 1000},
                        # Empty text → skipped
                        {"tStartMs": 9000, "dDurationMs": 1000, "segs": [{"utf8": " "}]},
                    ],
                }

        class _FakeClient:
            def __init__(self, **_):
                pass
            async def __aenter__(self):
                return self
            async def __aexit__(self, *_):
                return None
            async def get(self, url):
                captured_url["url"] = url
                return _FakeResp()

        import httpx
        monkeypatch.setattr(httpx, "AsyncClient", _FakeClient)

        snippets = await _yt_module._fetch_and_parse_json3(
            "https://example.com/foo.json3",
        )
        assert captured_url["url"] == "https://example.com/foo.json3"
        assert len(snippets) == 2
        assert snippets[0].text == "Hello world"
        assert snippets[0].start == 1.2
        assert snippets[0].duration == 2.16
        assert snippets[1].text == "Second cue."
