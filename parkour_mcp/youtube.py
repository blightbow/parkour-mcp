"""YouTube integration via yt-dlp (metadata) and youtube-transcript-api (captions).

Step 1 surface: ``video`` action only — metadata + description for a YouTube
video URL. Channel, playlist, transcript, and search actions land in later
commits per the implementation sequencing in the design discussion.

URL detection covers ``youtube.com/watch``, ``youtu.be``, ``shorts``, ``clip``,
``@handle``, ``/channel/UC...``, ``/c/`` , ``/user/``, and ``/playlist``.
``music.youtube.com`` is intentionally excluded — it's deferred as a sibling
tool because the music-track shape (album/artist/track) differs meaningfully
from the video shape.
"""

import asyncio
import logging
import re
from typing import Annotated, Any, Optional

from pydantic import Field

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
# MCP-facing dispatcher
# ---------------------------------------------------------------------------

async def youtube(
    action: Annotated[str, Field(
        description=(
            "The operation to perform. "
            "video: fetch video metadata + description from a YouTube URL."
        ),
    )],
    url: Annotated[Optional[str], Field(
        description=(
            "YouTube URL for the 'video' action. "
            "Accepts watch, youtu.be, shorts, clip, embed, and v/ forms."
        ),
    )] = None,
) -> str:
    """YouTube integration via yt-dlp."""
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
    return f"Error: Unknown action '{action}'. Valid actions: video"
