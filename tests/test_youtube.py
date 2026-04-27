"""Tests for parkour_mcp.youtube module (step 1: video action only)."""

import sys

import pytest

import parkour_mcp.youtube  # noqa: F401
_yt_module = sys.modules["parkour_mcp.youtube"]

from parkour_mcp.youtube import (  # noqa: E402
    _captions_summary,
    _detect_youtube_url,
    _format_duration,
    _format_upload_date,
    _map_yt_dlp_error,
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

        # Frontmatter fields
        assert "title: Me at the zoo" in result
        assert "video_id: jNQXAC9IVRw" in result
        assert "channel: jawed" in result
        assert "duration: 0:19" in result
        assert "upload_date: 2005-04-23" in result
        assert "view_count: 365129877" in result
        assert "language: en" in result
        # Description in body
        assert "The first video on YouTube" in result
        # Trust advisory (content fence) is present
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
