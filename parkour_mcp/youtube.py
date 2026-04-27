"""YouTube integration via yt-dlp (metadata) and youtube-transcript-api (captions).

Currently implements the ``video`` and ``transcript`` actions. Channel,
playlist, and search actions land in later commits per the implementation
sequencing in the design discussion.

URL detection covers ``youtube.com/watch``, ``youtu.be``, ``shorts``, ``clip``,
``@handle``, ``/channel/UC...``, ``/c/`` , ``/user/``, and ``/playlist``.
``music.youtube.com`` is intentionally excluded — it's deferred as a sibling
tool because the music-track shape (album/artist/track) differs meaningfully
from the video shape.

Transcript rendering uses a quality-aware coalescer that snaps window
boundaries to natural pauses (or sentence-end punctuation when caption
quality permits), then renders one of four output shapes — ``compact``
(default; sparse anchors plus outlier pause markers), ``absolute`` (per-line
timestamps), ``none`` (flat text, no timing), and ``structured`` (YAML).
"""

import asyncio
import logging
import re
from collections import OrderedDict
from dataclasses import dataclass
from typing import Annotated, Any, Literal, Optional

import tantivy
from pydantic import Field

from ._pipeline import register_group_cache
from .markdown import (
    FMEntries,
    _build_frontmatter,
    _fence_content,
    _TRUST_ADVISORY,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# URL detection
# ---------------------------------------------------------------------------
# Patterns target only youtube.com / youtu.be / m.youtube.com.
# music.youtube.com is intentionally NOT matched (deferred to a sibling tool).

# Video IDs are always exactly 11 chars in YouTube's base64-ish alphabet.
_VIDEO_ID = r"[A-Za-z0-9_-]{11}"

_YT_VIDEO_RE = re.compile(
    r"https?://"
    r"(?:"
        r"(?:www\.|m\.)?youtube\.com/(?:watch\?(?:[^#]*&)?v=|shorts/|embed/|v/)"
        r"|youtu\.be/"
    r")"
    rf"({_VIDEO_ID})",
    re.IGNORECASE,
)

# Clip URLs use a different identifier shape (variable length, e.g.
# ``UgkxAbCdEf12...``) and need their own pattern. yt-dlp resolves them
# to the underlying video on extraction; we just need to recognize the
# kind here so the dispatcher routes them to ``_video``.
_YT_CLIP_RE = re.compile(
    r"https?://(?:www\.|m\.)?youtube\.com/clip/([A-Za-z0-9_-]+)",
    re.IGNORECASE,
)

# Handle channels: /@handle (case-sensitive in canonical form).
_YT_CHANNEL_HANDLE_RE = re.compile(
    r"https?://(?:www\.|m\.)?youtube\.com/@([A-Za-z0-9._-]+)",
    re.IGNORECASE,
)

# Channel ID / vanity / legacy user URLs.
_YT_CHANNEL_ID_RE = re.compile(
    r"https?://(?:www\.|m\.)?youtube\.com/"
    r"(?:channel/(UC[A-Za-z0-9_-]{22})|c/([A-Za-z0-9._-]+)|user/([A-Za-z0-9._-]+))",
    re.IGNORECASE,
)

# Playlist IDs are variable-length, prefixed PL/UU/LL/FL/RD/WL/OL.
_YT_PLAYLIST_RE = re.compile(
    r"https?://(?:www\.|m\.)?youtube\.com/playlist\?(?:[^#]*&)?list=([A-Za-z0-9_-]+)",
    re.IGNORECASE,
)

# music.youtube.com — explicitly *excluded* from the youtube tool's scope.
# Detection here only exists so we can emit a clear "use a different tool"
# error instead of misidentifying as a regular video.
_YT_MUSIC_RE = re.compile(
    r"https?://music\.youtube\.com/",
    re.IGNORECASE,
)


def _detect_youtube_url(url: str) -> Optional[tuple[str, str]]:
    """Classify a YouTube URL.

    Returns ``(kind, identifier)`` on match, or ``None`` for non-YouTube
    URLs. Kinds: ``"video"``, ``"channel"``, ``"playlist"``, ``"music"``.
    The ``music`` kind is recognized only to produce an informative
    error; callers should treat it as out-of-scope.
    """
    if _YT_MUSIC_RE.search(url):
        return ("music", url)
    m = _YT_VIDEO_RE.search(url)
    if m:
        return ("video", m.group(1))
    m = _YT_CLIP_RE.search(url)
    if m:
        return ("video", m.group(1))
    m = _YT_CHANNEL_HANDLE_RE.search(url)
    if m:
        return ("channel", "@" + m.group(1))
    m = _YT_CHANNEL_ID_RE.search(url)
    if m:
        ident = m.group(1) or m.group(2) or m.group(3) or ""
        return ("channel", ident)
    m = _YT_PLAYLIST_RE.search(url)
    if m:
        return ("playlist", m.group(1))
    return None


# ---------------------------------------------------------------------------
# yt-dlp instance (lazy singleton, video mode)
# ---------------------------------------------------------------------------
# A single YoutubeDL instance per process is the recommended embedding
# pattern: PoToken caches and JS player solves are instance-scoped, so reuse
# avoids redundant work on subsequent calls. Channel/playlist/search actions
# (added in later commits) need different opts (extract_flat) and will get
# their own singleton.

_YDL_OPTS_VIDEO: dict[str, Any] = {
    "quiet": True,
    "no_warnings": True,
    "skip_download": True,
    "noplaylist": True,
    "extract_flat": False,
    "logger": logging.getLogger("yt_dlp"),
}

_ydl_video: Any = None


def _get_ydl_video() -> Any:
    """Return the lazily-constructed video-mode YoutubeDL singleton."""
    global _ydl_video
    if _ydl_video is None:
        from yt_dlp import YoutubeDL  # type: ignore[import-not-found]
        _ydl_video = YoutubeDL(_YDL_OPTS_VIDEO)
    return _ydl_video


# ---------------------------------------------------------------------------
# Error mapping
# ---------------------------------------------------------------------------

def _map_yt_dlp_error(exc: Exception) -> str:
    """Translate a yt-dlp exception to a user-facing error string.

    yt-dlp's exception hierarchy distinguishes only a few classes
    cleanly (geo, unavailable); bot detection, private, age-restricted,
    and members-only all surface as ``ExtractorError`` / ``DownloadError``
    with the relevant text in the message. Match on substrings.
    """
    try:
        from yt_dlp.utils import (  # type: ignore[import-not-found]
            DownloadError,
            ExtractorError,
            GeoRestrictedError,
            UnavailableVideoError,
        )
    except ImportError:
        # yt-dlp not importable — surface the raw type/message
        return f"Error: yt-dlp extraction failed ({type(exc).__name__})."

    if isinstance(exc, GeoRestrictedError):
        return "Error: Video is geo-restricted in this region."
    if isinstance(exc, UnavailableVideoError):
        return "Error: Video is unavailable."
    if isinstance(exc, (ExtractorError, DownloadError)):
        msg = str(exc).lower()
        if "sign in to confirm you" in msg or "confirm you're not a bot" in msg:
            return (
                "Error: YouTube blocked the request as suspected bot traffic. "
                "If on a residential connection, retry shortly. "
                "On cloud IPs, route through a residential proxy via HTTPS_PROXY."
            )
        if "private video" in msg:
            return "Error: Video is private."
        if "members-only" in msg or "members only" in msg:
            return "Error: Video is members-only and requires authentication."
        if "age" in msg and ("restrict" in msg or "confirm your age" in msg):
            return "Error: Video is age-restricted; cannot access without auth."
        if "video unavailable" in msg or "this video is not available" in msg:
            return "Error: Video unavailable."
        short = str(exc).splitlines()[0][:200]
        return f"Error: yt-dlp extraction failed ({type(exc).__name__}): {short}"
    short = str(exc).splitlines()[0][:200]
    return f"Error: yt-dlp extraction failed ({type(exc).__name__}): {short}"


# ---------------------------------------------------------------------------
# Format helpers
# ---------------------------------------------------------------------------

def _format_duration(seconds: Optional[float]) -> Optional[str]:
    """Render a seconds count as ``M:SS`` or ``H:MM:SS``."""
    if seconds is None:
        return None
    s = int(seconds)
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    return f"{h}:{m:02d}:{sec:02d}" if h else f"{m}:{sec:02d}"


def _format_upload_date(yyyymmdd: Optional[str]) -> Optional[str]:
    """Convert yt-dlp's ``YYYYMMDD`` date format to ISO ``YYYY-MM-DD``."""
    if not yyyymmdd or len(yyyymmdd) != 8 or not yyyymmdd.isdigit():
        return yyyymmdd
    return f"{yyyymmdd[:4]}-{yyyymmdd[4:6]}-{yyyymmdd[6:8]}"


def _captions_summary(info: dict) -> tuple[list[str], bool]:
    """Return ``(available_languages, has_auto_only)``.

    Manual and automatic captions are merged into a single sorted
    language list; the second element flags videos where only
    auto-generated captions exist (a reliable quality signal — see the
    transcript renderer plan for how this routes branching later).
    """
    manual = list((info.get("subtitles") or {}).keys())
    auto = list((info.get("automatic_captions") or {}).keys())
    langs = sorted(set(manual + auto))
    has_auto_only = bool(auto and not manual)
    return langs, has_auto_only


# ---------------------------------------------------------------------------
# Action: video
# ---------------------------------------------------------------------------

async def _video(url: str) -> str:
    """Fetch metadata + description for a single YouTube video URL."""
    ydl = _get_ydl_video()
    try:
        info = await asyncio.to_thread(ydl.extract_info, url, download=False)
    except Exception as exc:
        return _map_yt_dlp_error(exc)

    if info is None:
        return f"Error: yt-dlp returned no metadata for {url}"

    info = ydl.sanitize_info(info)
    if not isinstance(info, dict):
        return f"Error: Unexpected yt-dlp response shape for {url}"

    video_id = info.get("id") or ""
    title = info.get("title") or "Untitled"
    description = info.get("description") or ""

    captions_langs, captions_auto_only = _captions_summary(info)

    fm_entries = FMEntries({
        "title": title,
        "source": (
            f"https://www.youtube.com/watch?v={video_id}" if video_id else url
        ),
        "api": "yt-dlp",
        "video_id": video_id,
        "channel": info.get("channel") or info.get("uploader"),
        "channel_id": info.get("channel_id"),
        "channel_url": info.get("channel_url"),
        "duration": _format_duration(info.get("duration")),
        "upload_date": _format_upload_date(info.get("upload_date")),
        "view_count": info.get("view_count"),
        "like_count": info.get("like_count"),
        "language": info.get("language"),
        "live_status": info.get("live_status"),
        "availability": info.get("availability"),
        "captions_available": captions_langs or None,
        "captions_auto_only": True if captions_auto_only else None,
        "trust": _TRUST_ADVISORY,
    })

    fm = _build_frontmatter(fm_entries)
    body = description.strip() if description else "(no description)"
    return fm + "\n\n" + _fence_content(body, title=title)


# ---------------------------------------------------------------------------
# Transcript: data types and constants
# ---------------------------------------------------------------------------

TimestampMode = Literal["compact", "absolute", "none", "structured"]


@dataclass(frozen=True)
class Segment:
    """A single caption cue: start time, duration, and text.

    Mirrors the shape returned by ``youtube-transcript-api``'s
    ``FetchedTranscriptSnippet`` but as a frozen value object that is
    safe to share across the coalescer and renderers.
    """
    start: float
    duration: float
    text: str

    @property
    def end(self) -> float:
        return self.start + self.duration


@dataclass(frozen=True)
class Window:
    """A coalesced ~30s span of consecutive segments.

    Windows are the unit of presentation (one anchor per window) and the
    unit a future Tantivy index will treat as a document. A window's
    ``start`` and ``end`` come from its first and last segments.
    """
    start: float
    end: float
    segments: tuple[Segment, ...]


# Window coalescing band: target ~30s, with a [25, 35] tolerance band where
# we look for natural pause boundaries. Matches WhisperX Cut & Merge: cap
# at the upper bound, but prefer cuts at the largest pause within the band
# rather than at the time threshold itself.
_WINDOW_TARGET_DURATION = 30.0
_WINDOW_MIN_DURATION = 25.0
_WINDOW_MAX_DURATION = 35.0

# Inter-segment gap that earns a soft window boundary. Gaps shorter than
# this are treated as continuous speech.
_PAUSE_BOUNDARY = 1.0

# Punctuation density threshold for the quality gate: above this, treat
# the transcript as punctuated and route to the sentence-aware coalescer.
# 0.05 sentence-enders per word ≈ one sentence per 20 words, which is the
# floor for natural prose (typical English averages ~14 wpw per sentence).
_PUNCTUATION_DENSITY_THRESHOLD = 0.05

# Outlier-gap detection: pure rolling-median rule. For windows ≥ this many
# inter-segment gaps, compute the rolling median and flag gaps exceeding
# max(2 × median, 1.5s). Below the threshold, fall back to a fixed cutoff
# because the rolling median is unstable on small samples.
_OUTLIER_WINDOW = 10
_OUTLIER_MULTIPLE = 2.0
_OUTLIER_FLOOR = 1.5
_OUTLIER_FALLBACK = 3.0

# Sentence-final punctuation set used by the sentence-aware coalescer.
_SENTENCE_END = (".", "!", "?")


# ---------------------------------------------------------------------------
# Transcript: helpers
# ---------------------------------------------------------------------------

def _mmss(seconds: float) -> str:
    """Format ``seconds`` as zero-padded ``MM:SS`` or ``HH:MM:SS``."""
    s = int(seconds)
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    if h:
        return f"{h:02d}:{m:02d}:{sec:02d}"
    return f"{m:02d}:{sec:02d}"


def _median(xs: list[float]) -> float:
    """Statistical median over a non-empty list of floats."""
    if not xs:
        return 0.0
    s = sorted(xs)
    n = len(s)
    return s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2


def _punctuation_density(segments: list[Segment]) -> float:
    """Estimate sentence-ender density (per word) across all segments."""
    if not segments:
        return 0.0
    text = " ".join(s.text for s in segments)
    words = text.split()
    if not words:
        return 0.0
    enders = sum(1 for c in text if c in _SENTENCE_END)
    return enders / len(words)


def _segment_ends_sentence(seg: Segment) -> bool:
    """Whether this segment's text ends with sentence-final punctuation."""
    t = seg.text.rstrip()
    return bool(t) and t[-1] in _SENTENCE_END


def _detect_outlier_gaps(segments: list[Segment]) -> list[bool]:
    """Flag each inter-segment gap as an outlier or not.

    Returns a list aligned with ``segments`` where ``out[i]`` is ``True``
    iff the gap *after* segment ``i`` is unusually large. ``out[-1]`` is
    always ``False`` (the last segment has no following gap).

    For transcripts shorter than ``_OUTLIER_WINDOW`` gaps, applies a fixed
    threshold (``_OUTLIER_FALLBACK``) since the rolling median is unstable
    on small samples. For longer transcripts, computes a rolling median
    over a window of ``_OUTLIER_WINDOW`` gaps centered on each position
    and flags gaps exceeding ``max(_OUTLIER_MULTIPLE × median, _OUTLIER_FLOOR)``.
    """
    n = len(segments)
    if n < 2:
        return [False] * n

    gaps = [
        segments[i + 1].start - segments[i].end
        for i in range(n - 1)
    ]

    if len(gaps) < _OUTLIER_WINDOW:
        result = [g >= _OUTLIER_FALLBACK for g in gaps]
        result.append(False)
        return result

    half = _OUTLIER_WINDOW // 2
    out: list[bool] = []
    for i, gap in enumerate(gaps):
        lo = max(0, i - half)
        hi = min(len(gaps), lo + _OUTLIER_WINDOW)
        med = _median(gaps[lo:hi])
        threshold = max(_OUTLIER_MULTIPLE * med, _OUTLIER_FLOOR)
        out.append(gap >= threshold)
    out.append(False)
    return out


# ---------------------------------------------------------------------------
# Transcript: window coalescer (quality-aware, branched)
# ---------------------------------------------------------------------------

def coalesce_windows(
    segments: list[Segment],
    *,
    sentence_aware: bool,
    minimum: float = _WINDOW_MIN_DURATION,
    maximum: float = _WINDOW_MAX_DURATION,
    pause_boundary: float = _PAUSE_BOUNDARY,
) -> list[Window]:
    """Coalesce timed segments into ~30s windows.

    Walks segments in order, accumulating until the running duration
    enters the [minimum, maximum] tolerance band. Once in the band, cuts
    at the next natural boundary (sentence-end punctuation when
    ``sentence_aware`` is True, otherwise the next pause >= ``pause_boundary``).
    Forces a cut when adding the next segment would exceed ``maximum``.
    WhisperX Cut & Merge with a text-quality switch.

    The ``sentence_aware`` flag is the load-bearing branch: punctuated
    captions get cuts that respect prose structure; unpunctuated captions
    fall back to pure pause-based segmentation, which is the safest
    strategy when no linguistic signal is reliable.
    """
    if not segments:
        return []

    windows: list[Window] = []
    current: list[Segment] = []

    def _close(seg_list: list[Segment]) -> None:
        windows.append(Window(
            start=seg_list[0].start,
            end=seg_list[-1].end,
            segments=tuple(seg_list),
        ))

    for seg in segments:
        if not current:
            current.append(seg)
            continue

        # Would adding this segment exceed the upper bound? Cut first.
        prospective_dur = seg.end - current[0].start
        if prospective_dur > maximum:
            _close(current)
            current = [seg]
            continue

        # In the tolerance band: look for a natural boundary at the join.
        candidate_dur = current[-1].end - current[0].start
        if candidate_dur >= minimum:
            cut = False
            if sentence_aware and _segment_ends_sentence(current[-1]):
                cut = True
            else:
                gap = seg.start - current[-1].end
                if gap >= pause_boundary:
                    cut = True
            if cut:
                _close(current)
                current = [seg]
                continue

        current.append(seg)

    if current:
        _close(current)
    return windows


# ---------------------------------------------------------------------------
# Transcript: rendering modes
# ---------------------------------------------------------------------------

def _render_flat(windows: list[Window]) -> str:
    """Concatenate all segment text with single spaces; no timing."""
    parts = [s.text.strip() for w in windows for s in w.segments]
    return " ".join(p for p in parts if p)


def _render_absolute(windows: list[Window]) -> str:
    """Per-line absolute ``[MM:SS]`` timestamps."""
    lines = []
    for w in windows:
        for s in w.segments:
            text = s.text.strip()
            if text:
                lines.append(f"[{_mmss(s.start)}] {text}")
    return "\n".join(lines)


def _render_compact(windows: list[Window]) -> str:
    """Default rendering: anchor per window, segments on own lines, outlier
    pause markers between segments, blank lines between windows.

    Outlier detection runs over the FULL transcript so the rolling median
    is stable; per-window detection would oscillate on short windows.
    Inter-window gaps are not annotated because the next window's anchor
    already implies the transition.
    """
    if not windows:
        return ""

    all_segments = [s for w in windows for s in w.segments]
    outliers = _detect_outlier_gaps(all_segments)

    # Map (window_idx, in_window_idx) -> bool by walking the flat sequence
    flat_idx = 0
    outlier_at: dict[tuple[int, int], bool] = {}
    for wi, w in enumerate(windows):
        for si in range(len(w.segments)):
            outlier_at[(wi, si)] = outliers[flat_idx]
            flat_idx += 1

    lines: list[str] = []
    for wi, w in enumerate(windows):
        if wi > 0:
            lines.append("")  # blank line between windows
        lines.append(f"[{_mmss(w.start)}]")
        n = len(w.segments)
        for si, seg in enumerate(w.segments):
            text = seg.text.strip()
            if text:
                lines.append(text)
            # Inline pause marker only between segments WITHIN this window
            if si < n - 1 and outlier_at.get((wi, si), False):
                gap = w.segments[si + 1].start - seg.end
                lines.append(f"[+{int(round(gap))}s]")
    return "\n".join(lines)


def _render_structured(windows: list[Window]) -> str:
    """YAML list of segments with start/duration/text, for machine consumers."""
    import yaml
    data = []
    for w in windows:
        for s in w.segments:
            data.append({
                "t": round(s.start, 2),
                "d": round(s.duration, 2),
                "text": s.text,
            })
    return yaml.dump(data, default_flow_style=False, allow_unicode=True, sort_keys=False)


def render_transcript(
    windows: list[Window],
    *,
    mode: TimestampMode = "compact",
) -> str:
    """Render coalesced windows in the requested timestamp mode."""
    if mode == "none":
        return _render_flat(windows)
    if mode == "absolute":
        return _render_absolute(windows)
    if mode == "structured":
        return _render_structured(windows)
    return _render_compact(windows)


# ---------------------------------------------------------------------------
# Transcript: cache entry with lazy Tantivy index
# ---------------------------------------------------------------------------
# Sized to match _PageCache; see docs/youtube-transcript-search.md for the
# full schema and lifecycle rationale.

_TRANSCRIPT_CACHE_MAX_ENTRIES = 8


class _TranscriptEntry:
    """A cached transcript with eagerly-built windows and lazy Tantivy index.

    Construction populates ``segments`` and ``windows`` immediately —
    they're cheap pure-Python computation that the basic transcript-render
    path needs anyway. The Tantivy index builds lazily on first ``search``
    call, so the no-search render path skips the indexing cost.

    Cache key is the canonical YouTube watch URL; language preference is
    NOT part of the key. First successful language fetch for a URL wins
    for the cache entry's lifetime. Acceptable for v1; cross-language
    workflows can clear the cache or hit yt-dlp directly.
    """

    __slots__ = (
        "url", "video_id", "language_code", "is_generated",
        "segments", "windows", "chunking_strategy", "group",
        "fetcher",
        "_tantivy_index", "_built",
    )

    _SCHEMA = None

    @classmethod
    def _get_schema(cls):
        """Build (or return cached) Tantivy schema for transcript indexing.

        Schema is write-once at the class level, mirroring
        ``_pipeline.py#_CacheEntry._get_schema``. ``idx`` is the only
        stored field — window text and timestamps reconstruct from the
        Python-side ``windows`` tuple keyed by ``idx``. Both
        ``start_seconds`` and ``end_seconds`` are fast fields so range
        queries can skip the inverted index and ``order_by_field`` works
        for time-ordered results.
        """
        if cls._SCHEMA is None:
            builder = tantivy.SchemaBuilder()
            builder.add_text_field("body", stored=False)
            builder.add_unsigned_field("idx", stored=True)
            builder.add_float_field("start_seconds", indexed=True, fast=True)
            builder.add_float_field("end_seconds", indexed=True, fast=True)
            cls._SCHEMA = builder.build()
        return cls._SCHEMA

    def __init__(
        self,
        url: str,
        video_id: str,
        language_code: str,
        is_generated: bool,
        segments: tuple[Segment, ...],
        windows: tuple[Window, ...],
        chunking_strategy: str,
        group: Optional[str] = None,
        fetcher: str = "youtube-transcript-api",
    ):
        self.url = url
        self.video_id = video_id
        self.language_code = language_code
        self.is_generated = is_generated
        self.segments = segments
        self.windows = windows
        self.chunking_strategy = chunking_strategy
        self.group = group
        self.fetcher = fetcher
        self._tantivy_index = None
        self._built = False

    @property
    def is_built(self) -> bool:
        """Whether ``_ensure_built`` has produced the Tantivy index.

        Does NOT trigger build, so introspection paths can read state
        without paying the indexing cost.
        """
        return self._built

    def _ensure_built(self) -> None:
        """Build the Tantivy index over windows; idempotent."""
        if self._built or not self.windows:
            return
        schema = self._get_schema()
        self._tantivy_index = tantivy.Index(schema)
        writer = self._tantivy_index.writer()
        for i, window in enumerate(self.windows):
            body_text = " ".join(seg.text for seg in window.segments)
            writer.add_document(tantivy.Document(
                body=body_text,
                idx=i,
                start_seconds=float(window.start),
                end_seconds=float(window.end),
            ))
        writer.commit()
        self._tantivy_index.reload()
        self._built = True

    def search(
        self,
        query_str: Optional[str] = None,
        *,
        start_seconds: Optional[float] = None,
        end_seconds: Optional[float] = None,
        order: str = "score",
        limit: int = 50,
    ) -> tuple[list[int], list[str]]:
        """BM25 + time-range search over windows.

        Returns ``(matched_window_indices, parse_warnings)``. The
        warnings list mirrors ``_pipeline.py#_CacheEntry.search`` and
        carries any ``parse_query_lenient`` errors for the dispatcher to
        surface in frontmatter.

        Composition rules:
        - ``query_str`` only: BM25 over body, ranked by score.
        - range only: all windows whose ``[start, end]`` interval
          overlaps ``[start_seconds, end_seconds)``.
        - both: ``BooleanQuery`` of body AND range, ranked by score.
        - neither: matches all windows (``Query.all_query()``).

        ``order='time'`` sorts by ``start_seconds`` ascending instead of
        BM25 score; only meaningful when a query is set, but harmless
        otherwise (results are already chronological).
        """
        self._ensure_built()
        if not self._tantivy_index or not self.windows:
            return [], []

        schema = self._get_schema()
        warnings: list[str] = []

        body_query = None
        if query_str:
            body_query, errors = self._tantivy_index.parse_query_lenient(
                query_str, default_field_names=["body"],
            )
            if errors:
                warnings = [str(e) for e in errors]

        # Window overlaps [start, end) iff start_seconds < end AND end_seconds > start.
        # Half-open semantics match how Tantivy range_query treats inclusive
        # vs exclusive bounds, and avoid the off-by-one issues a closed
        # interval would have at chapter boundaries.
        range_clauses = []
        if end_seconds is not None:
            range_clauses.append((
                tantivy.Occur.Must,
                tantivy.Query.range_query(
                    schema, "start_seconds", tantivy.FieldType.Float,
                    -1e18, end_seconds,
                    include_lower=True, include_upper=False,
                ),
            ))
        if start_seconds is not None:
            range_clauses.append((
                tantivy.Occur.Must,
                tantivy.Query.range_query(
                    schema, "end_seconds", tantivy.FieldType.Float,
                    start_seconds, 1e18,
                    include_lower=False, include_upper=False,
                ),
            ))

        if body_query is not None and range_clauses:
            clauses = [(tantivy.Occur.Must, body_query)] + range_clauses
            query = tantivy.Query.boolean_query(clauses)
        elif body_query is not None:
            query = body_query
        elif range_clauses:
            query = tantivy.Query.boolean_query(range_clauses)
        else:
            query = tantivy.Query.all_query()

        searcher = self._tantivy_index.searcher()
        if order == "time":
            # Tantivy defaults order_by_field to descending; transcripts
            # read forward in time, so flip to ascending here.
            results = searcher.search(
                query, limit=limit,
                order_by_field="start_seconds",
                order=tantivy.Order.Asc,
            )
        else:
            results = searcher.search(query, limit=limit)
        matched = [searcher.doc(addr)["idx"][0] for _score, addr in results.hits]
        return matched, warnings


class _TranscriptCache:
    """2Q cache for transcript entries.

    Mirrors ``_pipeline.py#_PageCache`` semantics: probation FIFO +
    protected LRU, scan-resistant promotion on second access, group-aware
    eviction across registered caches via ``_pipeline._evict_group``.
    Lives in ``youtube.py`` rather than ``_pipeline.py`` because the
    schema is YouTube-specific; promote when a second time-series source
    appears.
    """

    def __init__(self, max_entries: int = _TRANSCRIPT_CACHE_MAX_ENTRIES):
        self._probation: OrderedDict[str, _TranscriptEntry] = OrderedDict()
        self._protected: OrderedDict[str, _TranscriptEntry] = OrderedDict()
        self._max_entries = max_entries

    def _total(self) -> int:
        return len(self._probation) + len(self._protected)

    def get(self, url: str) -> Optional[_TranscriptEntry]:
        entry = self._protected.get(url)
        if entry is not None:
            self._protected.move_to_end(url)
            return entry
        entry = self._probation.get(url)
        if entry is not None:
            del self._probation[url]
            self._protected[url] = entry
            self._protected.move_to_end(url)
            return entry
        return None

    def store(self, url: str, entry: _TranscriptEntry) -> None:
        if url in self._protected:
            self._protected[url] = entry
            self._protected.move_to_end(url)
            return
        if url in self._probation:
            self._probation[url] = entry
            self._probation.move_to_end(url)
            return
        while self._total() >= self._max_entries:
            self._evict()
        self._probation[url] = entry

    def _evict(self) -> None:
        # Local import dodges the circular pull at module-load time
        # (this module already imports register_group_cache from _pipeline,
        # but _evict_group is only needed inside this method).
        from ._pipeline import _evict_group
        victim_queue = self._probation if self._probation else self._protected
        if not victim_queue:
            return
        oldest_url = next(iter(victim_queue))
        oldest = victim_queue[oldest_url]
        if oldest.group is not None:
            # Evict locally first so the caller's loop terminates even when
            # ``self`` is not in ``_group_caches`` (test-local instances),
            # then fan out for cross-cache atomicity.
            self._evict_group_local(oldest.group)
            _evict_group(oldest.group)
        else:
            del victim_queue[oldest_url]

    def _evict_group_local(self, group_key: str) -> list[str]:
        """Evict every entry tagged with ``group_key`` from both queues."""
        evicted: list[str] = []
        for q in (self._probation, self._protected):
            to_remove = [u for u, e in q.items() if e.group == group_key]
            for u in to_remove:
                evicted.append(u)
                del q[u]
        return evicted

    def clear(self) -> None:
        """Drop every entry from both queues."""
        self._probation.clear()
        self._protected.clear()


_transcript_cache = _TranscriptCache()
register_group_cache(_transcript_cache)


# ---------------------------------------------------------------------------
# Transcript: error mapping
# ---------------------------------------------------------------------------

def _map_transcript_error(exc: Exception) -> str:
    """Translate a youtube-transcript-api exception to a user-facing string.

    Order matters: more-specific subclasses are checked before their
    superclasses (``IpBlocked`` before ``RequestBlocked``).
    """
    try:
        from youtube_transcript_api import (
            AgeRestricted,
            CouldNotRetrieveTranscript,
            InvalidVideoId,
            IpBlocked,
            NoTranscriptFound,
            PoTokenRequired,
            RequestBlocked,
            TranscriptsDisabled,
            VideoUnavailable,
            YouTubeRequestFailed,
        )
    except ImportError:
        return f"Error: youtube-transcript-api error ({type(exc).__name__})."

    if isinstance(exc, IpBlocked):
        return (
            "Error: YouTube blocked the transcript request based on IP "
            "reputation. If running from a cloud IP (AWS/GCP/Azure/etc.), "
            "configure HTTPS_PROXY to a residential proxy."
        )
    if isinstance(exc, RequestBlocked):
        return (
            "Error: YouTube blocked the transcript request as suspected "
            "bot traffic. Retry shortly, or configure HTTPS_PROXY to a "
            "residential proxy if blocks persist."
        )
    if isinstance(exc, PoTokenRequired):
        return (
            "Error: This video's captions require a Botguard PoToken; "
            "youtube-transcript-api has no current workaround. A yt-dlp "
            "fallback path is on the roadmap."
        )
    if isinstance(exc, TranscriptsDisabled):
        return "Error: The uploader has disabled transcripts for this video."
    if isinstance(exc, NoTranscriptFound):
        return (
            "Error: No transcript available in the requested language(s). "
            "Try omitting the languages= argument to fall back to the "
            "video's default caption track."
        )
    if isinstance(exc, AgeRestricted):
        return (
            "Error: Video is age-restricted; transcript unavailable "
            "without authentication."
        )
    if isinstance(exc, VideoUnavailable):
        return "Error: Video unavailable."
    if isinstance(exc, InvalidVideoId):
        return "Error: Invalid YouTube video ID."
    if isinstance(exc, YouTubeRequestFailed):
        short = str(exc).splitlines()[0][:200]
        return f"Error: YouTube request failed: {short}"
    if isinstance(exc, CouldNotRetrieveTranscript):
        short = str(exc).splitlines()[0][:200]
        return f"Error: Could not retrieve transcript ({type(exc).__name__}): {short}"
    short = str(exc).splitlines()[0][:200]
    return f"Error: Transcript fetch failed ({type(exc).__name__}): {short}"


# ---------------------------------------------------------------------------
# Action: transcript
# ---------------------------------------------------------------------------

def _fetch_transcript_sync(video_id: str, languages: list[str]):
    """Sync wrapper around YouTubeTranscriptApi().fetch().

    Lives at module scope so ``asyncio.to_thread`` can pickle it cleanly
    on platforms that need it. The library itself is sync-only.
    """
    from youtube_transcript_api import YouTubeTranscriptApi
    api = YouTubeTranscriptApi()
    return api.fetch(video_id, languages=languages)


# ---------------------------------------------------------------------------
# Transcript: yt-dlp fallback path
# ---------------------------------------------------------------------------
# When youtube-transcript-api raises RequestBlocked or PoTokenRequired,
# yt-dlp's caption code path hits a different Innertube endpoint and may
# succeed where the dedicated library failed. yt-dlp pulls caption-track
# URLs into ``info["subtitles"]`` and ``info["automatic_captions"]``;
# the JSON3 format is YouTube's native timed-text JSON and is the
# cleanest target to parse without an external library.

@dataclass(frozen=True)
class _FallbackSnippet:
    """Duck-typed equivalent of FetchedTranscriptSnippet for the fallback."""
    start: float
    duration: float
    text: str


@dataclass(frozen=True)
class _FallbackTranscript:
    """Duck-typed equivalent of FetchedTranscript for the fallback."""
    snippets: tuple[_FallbackSnippet, ...]
    language_code: str
    is_generated: bool


def _extract_video_info_sync(url: str) -> Any:
    """Fetch full video info via the video-mode YoutubeDL singleton.

    Captures subtitle / automatic_caption URLs; reuse of the singleton
    means the PoToken cache and JS player solve carry across the
    ``video`` and ``transcript`` fallback paths on the same video.
    """
    ydl = _get_ydl_video()
    info = ydl.extract_info(url, download=False)
    return ydl.sanitize_info(info)


def _pick_caption_track(
    subs: dict, auto: dict, languages: list[str],
) -> tuple[Optional[list[dict]], Optional[str], bool]:
    """Pick the best track for the requested language preference list.

    Manual captions win over auto-generated. Returns
    ``(track_formats, language_code, is_generated)`` or
    ``(None, None, False)`` if no language matches either dict.
    """
    for lang in languages:
        if lang in subs:
            return subs[lang], lang, False
    for lang in languages:
        if lang in auto:
            return auto[lang], lang, True
    return None, None, False


async def _fetch_and_parse_json3(url: str) -> tuple[_FallbackSnippet, ...]:
    """HTTP-GET YouTube's JSON3 timed-text feed and parse to snippets.

    JSON3 events are ``{"tStartMs", "dDurationMs", "segs": [{"utf8": ...}, ...]}``.
    The ``segs`` array can split a single utterance across multiple text
    fragments; concatenation rebuilds the cue. Empty cues (no segs or
    blank text) are skipped so they don't pollute the segment list.
    """
    import httpx
    async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as c:
        resp = await c.get(url)
        resp.raise_for_status()
        data = resp.json()
    snippets: list[_FallbackSnippet] = []
    for ev in data.get("events", []):
        if "segs" not in ev:
            continue
        text = "".join(seg.get("utf8", "") for seg in ev["segs"]).strip()
        if not text:
            continue
        start = (ev.get("tStartMs") or 0) / 1000.0
        duration = (ev.get("dDurationMs") or 0) / 1000.0
        snippets.append(_FallbackSnippet(start=start, duration=duration, text=text))
    return tuple(snippets)


async def _yt_dlp_transcript_fallback(
    video_id: str, languages: list[str],
) -> Optional[_FallbackTranscript]:
    """Best-effort caption fetch via yt-dlp + raw HTTP.

    Returns ``None`` when any link in the chain fails: yt-dlp couldn't
    extract, no caption track matched the language preferences, no JSON3
    format on the chosen track, fetch error, or parse error. Callers
    fall back to the original transcript-api error message in that case
    rather than masking the failure.
    """
    url = f"https://www.youtube.com/watch?v={video_id}"
    try:
        info = await asyncio.to_thread(_extract_video_info_sync, url)
    except Exception:
        return None
    if not info or not isinstance(info, dict):
        return None

    subs = info.get("subtitles") or {}
    auto = info.get("automatic_captions") or {}
    track, lang, is_generated = _pick_caption_track(subs, auto, languages)
    if track is None:
        return None

    json3_url = next(
        (f["url"] for f in track if f.get("ext") == "json3" and f.get("url")),
        None,
    )
    if not json3_url:
        return None

    try:
        snippets = await _fetch_and_parse_json3(json3_url)
    except Exception:
        return None
    if not snippets:
        return None

    return _FallbackTranscript(
        snippets=snippets,
        language_code=lang or "?",
        is_generated=is_generated,
    )


def _build_transcript_entry(
    canonical_url: str,
    video_id: str,
    fetched,
    fetcher: str = "youtube-transcript-api",
) -> _TranscriptEntry:
    """Construct an entry from a fetched-transcript object.

    The ``fetched`` argument duck-types: it must expose ``snippets``
    (iterable of objects with ``start``, ``duration``, ``text``),
    ``language_code``, and ``is_generated``. Both ``FetchedTranscript``
    (the youtube-transcript-api type) and ``_FallbackTranscript`` (the
    yt-dlp fallback shim) satisfy this contract.

    Caption cues often contain embedded newlines for display wrapping (a
    single utterance rendered across two visual lines on the player).
    Those newlines aren't semantic and break readability when rendered;
    internal whitespace collapses to single spaces here so each segment
    presents as one coherent line in compact and absolute output.
    """
    snippets = list(fetched.snippets)
    segments = tuple(
        Segment(
            start=float(s.start),
            duration=float(s.duration),
            text=" ".join(s.text.split()),
        )
        for s in snippets
    )
    is_auto = bool(fetched.is_generated)
    density = _punctuation_density(list(segments))
    sentence_aware = (not is_auto) and density >= _PUNCTUATION_DENSITY_THRESHOLD
    windows = tuple(coalesce_windows(list(segments), sentence_aware=sentence_aware))
    chunking_strategy = "sentence" if sentence_aware else "time_window"
    return _TranscriptEntry(
        url=canonical_url,
        video_id=video_id,
        language_code=fetched.language_code,
        is_generated=is_auto,
        segments=segments,
        windows=windows,
        chunking_strategy=chunking_strategy,
        group=f"yt:{video_id}",
        fetcher=fetcher,
    )


def _base_transcript_fm(entry: _TranscriptEntry) -> FMEntries:
    """Build the frontmatter fields shared across all transcript responses."""
    return FMEntries({
        "source": entry.url,
        "api": entry.fetcher,
        "video_id": entry.video_id,
        "transcript_language": entry.language_code,
        "transcript_kind": "auto" if entry.is_generated else "manual",
        "total_windows": len(entry.windows),
        "chunking_strategy": entry.chunking_strategy,
        "trust": _TRUST_ADVISORY,
    })


def _render_full_transcript_response(
    entry: _TranscriptEntry,
    timestamps: TimestampMode,
) -> str:
    """Render the entire transcript (matches step 2 behavior)."""
    body = render_transcript(list(entry.windows), mode=timestamps)
    fm_entries = _base_transcript_fm(entry)
    fm_entries["total_segments"] = len(entry.segments)
    fm_entries["duration"] = _format_duration(
        entry.segments[-1].end if entry.segments else None,
    )
    fm = _build_frontmatter(fm_entries)
    title = f"Transcript ({entry.language_code})"
    return fm + "\n\n" + _fence_content(body, title=title)


def _render_window_retrieval_response(
    entry: _TranscriptEntry,
    requested: list[int],
    timestamps: TimestampMode,
) -> str:
    """Render specific windows by index, preserving caller's order and dedup."""
    total = len(entry.windows)
    seen: set[int] = set()
    in_order: list[int] = []
    unknown: list[int] = []
    for i in requested:
        if i in seen:
            continue
        seen.add(i)
        if 0 <= i < total:
            in_order.append(i)
        else:
            unknown.append(i)

    matched = [entry.windows[i] for i in in_order]
    body = render_transcript(matched, mode=timestamps)

    fm_entries = _base_transcript_fm(entry)
    fm_entries["requested_windows"] = list(requested)
    fm_entries["matched_windows"] = in_order
    if unknown:
        fm_entries["unknown_windows"] = unknown
    if not in_order:
        fm_entries.append("note", (
            f"None of the requested windows are valid "
            f"(range: 0..{total - 1})."
        ))

    fm = _build_frontmatter(fm_entries)
    title = f"Transcript ({entry.language_code})"
    return fm + "\n\n" + _fence_content(body, title=title)


def _build_context_hint(matched: list[int], total: int) -> str:
    """Suggest [i-1, i, i+1] for each matched index, clamped and deduped."""
    context: set[int] = set()
    for i in matched:
        for j in (i - 1, i, i + 1):
            if 0 <= j < total:
                context.add(j)
    return f"windows={sorted(context)} for context around matches"


def _render_search_response(
    entry: _TranscriptEntry,
    query: Optional[str],
    start_seconds: Optional[float],
    end_seconds: Optional[float],
    order: str,
    timestamps: TimestampMode,
) -> str:
    """Render BM25 / time-range / combined search results."""
    matched_indices, warnings = entry.search(
        query,
        start_seconds=start_seconds,
        end_seconds=end_seconds,
        order=order,
    )

    matched_windows = [entry.windows[i] for i in matched_indices]
    body = render_transcript(matched_windows, mode=timestamps)

    fm_entries = _base_transcript_fm(entry)
    fm_entries["matched_windows"] = matched_indices
    if query is not None:
        fm_entries["search"] = query
    if start_seconds is not None:
        fm_entries["start_seconds"] = start_seconds
    if end_seconds is not None:
        fm_entries["end_seconds"] = end_seconds
    if order != "score":
        fm_entries["order"] = order
    for w in warnings:
        fm_entries.append("warning", w)

    if matched_indices:
        fm_entries.append(
            "hint",
            _build_context_hint(matched_indices, len(entry.windows)),
        )
    elif start_seconds is not None or end_seconds is not None:
        last_end = entry.segments[-1].end if entry.segments else 0
        fm_entries.append("note", (
            f"No windows match the time range. "
            f"Transcript spans 0..{int(last_end)} seconds."
        ))

    fm = _build_frontmatter(fm_entries)
    title = f"Transcript ({entry.language_code})"
    return fm + "\n\n" + _fence_content(body, title=title)


async def _transcript(
    url: str,
    languages: list[str],
    timestamps: TimestampMode,
    *,
    search: Optional[str] = None,
    windows: Optional[list[int]] = None,
    start_seconds: Optional[float] = None,
    end_seconds: Optional[float] = None,
    order: str = "score",
) -> str:
    """Fetch / cache / render a YouTube transcript per the requested shape."""
    detected = _detect_youtube_url(url)
    if detected is None:
        return f"Error: Not a recognized YouTube URL: {url}"
    if detected[0] == "music":
        return (
            "Error: music.youtube.com URLs are out of scope for this tool."
        )
    if detected[0] != "video":
        return (
            f"Error: URL is a {detected[0]}, not a video. "
            "The transcript action only accepts video URLs."
        )
    video_id = detected[1]
    canonical_url = f"https://www.youtube.com/watch?v={video_id}"

    entry = _transcript_cache.get(canonical_url)
    if entry is None:
        fetcher_name = "youtube-transcript-api"
        try:
            fetched = await asyncio.to_thread(
                _fetch_transcript_sync, video_id, languages,
            )
        except Exception as exc:
            # When the dedicated library is blocked or hits the PoToken
            # wall, yt-dlp's caption code path can sometimes succeed.
            # Limit the fallback to those specific exceptions so genuine
            # content-side failures (TranscriptsDisabled, NoTranscriptFound,
            # AgeRestricted, VideoUnavailable, InvalidVideoId) surface as-is.
            try:
                from youtube_transcript_api import (
                    PoTokenRequired, RequestBlocked,
                )
            except ImportError:
                return _map_transcript_error(exc)
            if isinstance(exc, (RequestBlocked, PoTokenRequired)):
                fetched = await _yt_dlp_transcript_fallback(video_id, languages)
                if fetched is None:
                    return _map_transcript_error(exc)
                fetcher_name = "yt-dlp (fallback)"
            else:
                return _map_transcript_error(exc)
        if not list(fetched.snippets):
            return "Error: Transcript fetched but contains no segments."
        entry = _build_transcript_entry(
            canonical_url, video_id, fetched, fetcher=fetcher_name,
        )
        _transcript_cache.store(canonical_url, entry)

    if windows is not None:
        return _render_window_retrieval_response(entry, windows, timestamps)
    if search or start_seconds is not None or end_seconds is not None:
        return _render_search_response(
            entry, search, start_seconds, end_seconds, order, timestamps,
        )
    return _render_full_transcript_response(entry, timestamps)


# ---------------------------------------------------------------------------
# Actions: channel and playlist (yt-dlp flat extraction)
# ---------------------------------------------------------------------------
# Channel and playlist URLs both resolve through yt-dlp's
# ``extract_flat='in_playlist'`` mode, which returns a ``_type='playlist'``
# dict with stub entries (id, title, url, sometimes duration / view_count).
# A single shared YoutubeDL is intentionally NOT reused: list-mode opts
# (especially ``playlistend``) vary per call, and these actions are far
# less hot than ``video``, so per-call construction is cheap enough.

_LIST_LIMIT_DEFAULT = 30
_LIST_LIMIT_MAX = 200


def _extract_flat_sync(url: str, limit: int) -> Any:
    """Run yt-dlp flat extraction synchronously.

    Lives at module scope so ``asyncio.to_thread`` can pickle it cleanly
    on platforms that need it. Caps the playlistend opt server-side; the
    formatter caps client-side too as a defense-in-depth in case yt-dlp
    over-delivers.
    """
    from yt_dlp import YoutubeDL  # type: ignore[import-not-found]
    opts = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "extract_flat": "in_playlist",
        "noplaylist": False,
        "playlistend": limit,
        "logger": logging.getLogger("yt_dlp"),
    }
    ydl = YoutubeDL(opts)
    info = ydl.extract_info(url, download=False)
    return ydl.sanitize_info(info)


def _format_video_entry(entry: dict, *, index: int) -> str:
    """Format a single flat-extract entry as a numbered list item.

    URL preference order: ``webpage_url`` (yt-dlp's canonical, populated
    on ``_type='playlist'`` tab entries), then ``url`` (populated on
    ``_type='url'`` video entries), then a constructed watch URL from
    ``id``. Tab entries set ``id`` to the parent channel's UC id rather
    than a video id, so the constructed-URL fallback would be wrong for
    them — explicit URLs are the safer source.
    """
    vid = entry.get("id") or ""
    title = entry.get("title") or "(no title)"
    duration = entry.get("duration")
    view_count = entry.get("view_count")
    uploader = entry.get("uploader") or entry.get("channel")
    explicit_url = entry.get("webpage_url") or entry.get("url")

    head = f"{index}. **{title}**"
    meta_bits = []
    if duration is not None:
        meta_bits.append(_format_duration(duration) or "")
    if view_count is not None:
        meta_bits.append(f"{view_count:,} views")
    if uploader:
        meta_bits.append(uploader)
    meta_bits = [m for m in meta_bits if m]
    if meta_bits:
        head += f" ({', '.join(meta_bits)})"

    lines = [head]
    if explicit_url and isinstance(explicit_url, str) and explicit_url.startswith("http"):
        lines.append(f"   {explicit_url}")
    elif vid:
        lines.append(f"   https://www.youtube.com/watch?v={vid}")
    return "\n".join(lines)


def _is_tab_listing(entries: list[dict]) -> bool:
    """Detect a channel-tab listing.

    yt-dlp returns the channel's tabs as entries when given a bare
    channel URL (without /videos, /shorts, /streams suffix). Tab
    entries carry ``_type='playlist'`` (they're nested playlists),
    while video entries carry ``_type='url'``. If every entry is a
    nested playlist, treat the response as a tab listing.
    """
    if not entries:
        return False
    return all(e.get("_type") == "playlist" for e in entries)


def _channel_fm_and_body(info: dict, limit: int) -> tuple[FMEntries, str]:
    """Build frontmatter + body for a channel listing."""
    title = info.get("title") or info.get("channel") or "Untitled"
    description = info.get("description") or ""
    entries = list(info.get("entries") or [])[:limit]
    channel_id = info.get("channel_id") or info.get("uploader_id")
    tab_listing = _is_tab_listing(entries)

    fm = FMEntries({
        "title": title,
        "source": (
            info.get("webpage_url")
            or info.get("channel_url")
            or info.get("uploader_url")
            or ""
        ),
        "api": "yt-dlp",
        "channel": info.get("channel") or info.get("uploader"),
        "channel_id": channel_id,
        "follower_count": info.get("channel_follower_count"),
        "total_videos": info.get("playlist_count"),
        "returned_videos": len(entries),
        "trust": _TRUST_ADVISORY,
    })
    if tab_listing:
        fm.append("hint", (
            "URL returned the channel's tab list. Append /videos, "
            "/shorts, /streams, /playlists, or /podcasts to scope to "
            "a specific tab's entries."
        ))

    body_parts: list[str] = []
    if description.strip():
        body_parts.append(description.strip())
        body_parts.append("")
    heading = "Tabs" if tab_listing else "Recent uploads"
    body_parts.append(f"## {heading} ({len(entries)})")
    body_parts.append("")
    if entries:
        for i, entry in enumerate(entries, 1):
            body_parts.append(_format_video_entry(entry, index=i))
            body_parts.append("")
    else:
        body_parts.append("(no entries)")
    return fm, "\n".join(body_parts).rstrip()


def _playlist_fm_and_body(info: dict, limit: int) -> tuple[FMEntries, str]:
    """Build frontmatter + body for a playlist listing."""
    title = info.get("title") or "Untitled"
    description = info.get("description") or ""
    entries = list(info.get("entries") or [])[:limit]

    fm = FMEntries({
        "title": title,
        "source": (
            info.get("webpage_url")
            or info.get("uploader_url")
            or ""
        ),
        "api": "yt-dlp",
        "uploader": info.get("uploader"),
        "uploader_id": info.get("uploader_id"),
        "last_updated": _format_upload_date(info.get("modified_date")),
        "total_items": info.get("playlist_count"),
        "returned_items": len(entries),
        "trust": _TRUST_ADVISORY,
    })

    body_parts: list[str] = []
    if description.strip():
        body_parts.append(description.strip())
        body_parts.append("")
    body_parts.append(f"## Items ({len(entries)})")
    body_parts.append("")
    if entries:
        for i, entry in enumerate(entries, 1):
            body_parts.append(_format_video_entry(entry, index=i))
            body_parts.append("")
    else:
        body_parts.append("(no entries)")
    return fm, "\n".join(body_parts).rstrip()


async def _channel(url: str, limit: int) -> str:
    """Fetch a channel's recent uploads via yt-dlp flat extraction."""
    try:
        info = await asyncio.to_thread(_extract_flat_sync, url, limit)
    except Exception as exc:
        return _map_yt_dlp_error(exc)
    if info is None:
        return f"Error: yt-dlp returned no metadata for {url}"
    if not isinstance(info, dict):
        return f"Error: Unexpected yt-dlp response shape for {url}"
    fm_entries, body = _channel_fm_and_body(info, limit)
    fm = _build_frontmatter(fm_entries)
    title = fm_entries.get("title") or "Untitled"
    return fm + "\n\n" + _fence_content(body, title=str(title))


async def _playlist(url: str, limit: int) -> str:
    """Fetch a playlist's items via yt-dlp flat extraction."""
    try:
        info = await asyncio.to_thread(_extract_flat_sync, url, limit)
    except Exception as exc:
        return _map_yt_dlp_error(exc)
    if info is None:
        return f"Error: yt-dlp returned no metadata for {url}"
    if not isinstance(info, dict):
        return f"Error: Unexpected yt-dlp response shape for {url}"
    fm_entries, body = _playlist_fm_and_body(info, limit)
    fm = _build_frontmatter(fm_entries)
    title = fm_entries.get("title") or "Untitled"
    return fm + "\n\n" + _fence_content(body, title=str(title))


def _search_fm_and_body(
    info: dict, query: str, limit: int,
) -> tuple[FMEntries, str]:
    """Build frontmatter + body for a search-results listing.

    yt-dlp's ``ytsearch{N}:`` URL returns a ``_type='playlist'`` whose
    entries are video stubs — same shape as ``channel`` and
    ``playlist``, just framed as search results. The formatter mirrors
    those but presents the heading as "Results" and surfaces ``query``
    in frontmatter so callers can echo what they asked for.
    """
    entries = list(info.get("entries") or [])[:limit]
    fm = FMEntries({
        "api": "yt-dlp",
        "search": query,
        "returned_results": len(entries),
        "trust": _TRUST_ADVISORY,
    })

    body_parts: list[str] = []
    body_parts.append(f"## Results ({len(entries)})")
    body_parts.append("")
    if entries:
        for i, entry in enumerate(entries, 1):
            body_parts.append(_format_video_entry(entry, index=i))
            body_parts.append("")
    else:
        body_parts.append("(no results)")
    return fm, "\n".join(body_parts).rstrip()


async def _search(query: str, limit: int) -> str:
    """Search YouTube for videos matching ``query``.

    Builds a ``ytsearch{N}:`` URL and runs it through the flat-extract
    path. ytsearch is unofficial — yt-dlp constructs the request against
    YouTube's same Innertube endpoints used by the website's search
    page, so result quality matches what the user would see browsing
    youtube.com/results?search_query=...
    """
    # Strip the query of leading/trailing whitespace; yt-dlp will URL-encode
    # the rest but rejects empty queries with a confusing message.
    query = query.strip()
    if not query:
        return "Error: 'query' must be a non-empty string for action='search'."
    search_url = f"ytsearch{limit}:{query}"
    try:
        info = await asyncio.to_thread(_extract_flat_sync, search_url, limit)
    except Exception as exc:
        return _map_yt_dlp_error(exc)
    if info is None:
        return f"Error: yt-dlp returned no results for query: {query}"
    if not isinstance(info, dict):
        return "Error: Unexpected yt-dlp response shape for search."
    fm_entries, body = _search_fm_and_body(info, query, limit)
    fm = _build_frontmatter(fm_entries)
    return fm + "\n\n" + _fence_content(body, title=f"Search: {query}")


# ---------------------------------------------------------------------------
# MCP-facing dispatcher
# ---------------------------------------------------------------------------

async def youtube(
    action: Annotated[str, Field(
        description=(
            "The operation to perform. "
            "video: fetch video metadata + description from a YouTube URL. "
            "transcript: fetch the caption transcript for a video URL, "
            "with optional BM25 search, time-range filtering, and "
            "explicit window retrieval. "
            "channel: list a channel's recent uploads. "
            "playlist: list a playlist's items. "
            "search: search YouTube for videos matching a free-text query."
        ),
    )],
    url: Annotated[Optional[str], Field(
        description=(
            "YouTube URL for video / transcript / channel / playlist actions. "
            "video / transcript: watch, youtu.be, shorts, clip, embed, v/. "
            "channel: /@handle, /channel/UC..., /c/, /user/, optionally "
            "with a /videos, /shorts, /streams, /playlists tab suffix. "
            "playlist: /playlist?list=... "
            "Not used for action='search' (use 'query=' instead)."
        ),
    )] = None,
    query: Annotated[Optional[str], Field(
        description=(
            "For action='search': free-text query string. yt-dlp's "
            "ytsearch{N}: routing handles URL encoding."
        ),
    )] = None,
    languages: Annotated[Optional[list[str]], Field(
        description=(
            "For 'transcript': caption language preference list, tried in "
            "order (e.g. ['en', 'en-US']). Defaults to ['en']."
        ),
    )] = None,
    timestamps: Annotated[TimestampMode, Field(
        description=(
            "For 'transcript': output shape. "
            "'compact' (default) emits sparse anchors plus inline markers "
            "for unusually long pauses, with each source caption cue on "
            "its own line. 'absolute' emits a per-line [MM:SS] prefix on "
            "every cue. 'none' returns flat text with no timing. "
            "'structured' returns a YAML list of {t, d, text} triples for "
            "machine consumers."
        ),
    )] = "compact",
    search: Annotated[Optional[str], Field(
        description=(
            "For 'transcript': BM25 query over window text. Mutually "
            "exclusive with 'windows='. Combine with start_seconds / "
            "end_seconds to restrict by time range."
        ),
    )] = None,
    windows: Annotated[Optional[list[int]], Field(
        description=(
            "For 'transcript': retrieve specific window indices "
            "(0-based). Mutually exclusive with 'search=' and "
            "incompatible with time-range filters. Out-of-range indices "
            "are reported in frontmatter rather than erroring."
        ),
    )] = None,
    start_seconds: Annotated[Optional[float], Field(
        description=(
            "For 'transcript': lower bound on a time-range filter, in "
            "seconds. Windows whose interval overlaps [start_seconds, "
            "end_seconds) match. Combine with 'search=' for a "
            "time-restricted query."
        ),
    )] = None,
    end_seconds: Annotated[Optional[float], Field(
        description=(
            "For 'transcript': upper bound on a time-range filter, in "
            "seconds. Half-open: a window starting exactly at "
            "end_seconds does not match."
        ),
    )] = None,
    order: Annotated[Literal["score", "time"], Field(
        description=(
            "For 'transcript' search: 'score' (default) ranks by BM25 "
            "relevance; 'time' sorts by start_seconds ascending. Only "
            "meaningful when a query or range is set."
        ),
    )] = "score",
    limit: Annotated[int, Field(
        description=(
            "For 'channel' / 'playlist': maximum number of entries to "
            "return. Default 30, capped at 200. yt-dlp's flat extraction "
            "respects this server-side via playlistend, so large "
            "channels don't pull every upload."
        ),
    )] = _LIST_LIMIT_DEFAULT,
) -> str:
    """YouTube integration via yt-dlp and youtube-transcript-api."""
    if action == "video":
        if not url:
            return "Error: 'url' is required for action='video'."
        kind = _detect_youtube_url(url)
        if kind is None:
            return f"Error: Not a recognized YouTube URL: {url}"
        if kind[0] == "music":
            return (
                "Error: music.youtube.com URLs are out of scope for this tool. "
                "Music tracks have a different shape (album/artist/track) and "
                "will be handled by a sibling tool."
            )
        if kind[0] != "video":
            return (
                f"Error: URL is a {kind[0]}, not a video. "
                f"The {kind[0]} action is not yet implemented."
            )
        return await _video(url)
    if action == "transcript":
        if not url:
            return "Error: 'url' is required for action='transcript'."
        if search and windows is not None:
            return (
                "Error: 'search' and 'windows' are mutually exclusive."
            )
        if windows is not None and (
            start_seconds is not None or end_seconds is not None
        ):
            return (
                "Error: 'windows' cannot be combined with time-range filters."
            )
        if (
            start_seconds is not None
            and end_seconds is not None
            and start_seconds > end_seconds
        ):
            return "Error: start_seconds must be <= end_seconds."
        return await _transcript(
            url,
            languages=languages or ["en"],
            timestamps=timestamps,
            search=search,
            windows=windows,
            start_seconds=start_seconds,
            end_seconds=end_seconds,
            order=order,
        )
    if action == "channel":
        if not url:
            return "Error: 'url' is required for action='channel'."
        kind = _detect_youtube_url(url)
        if kind is None:
            return f"Error: Not a recognized YouTube URL: {url}"
        if kind[0] == "music":
            return (
                "Error: music.youtube.com URLs are out of scope for this tool."
            )
        if kind[0] != "channel":
            return (
                f"Error: URL is a {kind[0]}, not a channel. "
                "Pass an /@handle, /channel/UC..., /c/, or /user/ URL."
            )
        return await _channel(url, limit=max(1, min(limit, _LIST_LIMIT_MAX)))
    if action == "playlist":
        if not url:
            return "Error: 'url' is required for action='playlist'."
        kind = _detect_youtube_url(url)
        if kind is None:
            return f"Error: Not a recognized YouTube URL: {url}"
        if kind[0] == "music":
            return (
                "Error: music.youtube.com URLs are out of scope for this tool."
            )
        if kind[0] != "playlist":
            return (
                f"Error: URL is a {kind[0]}, not a playlist. "
                "Pass a /playlist?list=... URL."
            )
        return await _playlist(url, limit=max(1, min(limit, _LIST_LIMIT_MAX)))
    if action == "search":
        if not query:
            return "Error: 'query' is required for action='search'."
        return await _search(
            query, limit=max(1, min(limit, _LIST_LIMIT_MAX)),
        )
    return (
        f"Error: Unknown action '{action}'. "
        "Valid actions: video, transcript, channel, playlist, search"
    )
