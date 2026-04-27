"""Tests for parkour_mcp.youtube module."""

import sys

import pytest
import tantivy

import parkour_mcp.youtube  # noqa: F401
_yt_module = sys.modules["parkour_mcp.youtube"]


@pytest.fixture(autouse=True)
def _clear_transcript_cache():
    """Reset the module-level transcript cache between tests.

    Without this, fake-fetch tests using the same URL would silently
    cache-hit the entry created by an earlier test, never invoking the
    monkeypatched fetcher.
    """
    _yt_module._transcript_cache.clear()
    yield
    _yt_module._transcript_cache.clear()

from parkour_mcp.youtube import (  # noqa: E402
    Segment,
    Window,
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
        assert "IP" in out and "residential proxy" in out.lower()

    def test_request_blocked(self):
        from youtube_transcript_api import RequestBlocked
        err = RequestBlocked("vid")
        out = _map_transcript_error(err)
        assert "bot" in out.lower()

    def test_po_token_required(self):
        from youtube_transcript_api import PoTokenRequired
        err = PoTokenRequired("vid")
        out = _map_transcript_error(err)
        assert "PoToken" in out

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
        assert "title: MKBHD" in result
        assert "channel: MKBHD" in result
        assert "channel_id: UCBJycsmduvYEL83R_U4JriQ" in result
        assert "follower_count: 18500000" in result
        assert "total_videos: 1500" in result
        assert "returned_videos: 5" in result
        assert "Quality Tech Videos." in result
        assert "Recent uploads (5)" in result
        assert "Video 0" in result
        assert "https://www.youtube.com/watch?v=vid00" in result
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
        assert "title: Curated Reading" in result
        assert "uploader: Some User" in result
        assert "last_updated: 2026-03-01" in result
        assert "total_items: 12" in result
        assert "returned_items: 3" in result
        assert "Items (3)" in result
        assert "Item 0" in result

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
