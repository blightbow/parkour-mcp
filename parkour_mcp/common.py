"""Shared constants and utilities for parkour-mcp."""

import asyncio
import ipaddress
import logging
import os
import platform
import socket
import time
from importlib.metadata import version as _pkg_version
from pathlib import Path
from urllib.parse import urlparse

import httpx

# ---------------------------------------------------------------------------
# Package / runtime versions (used in User-Agent strings)
# ---------------------------------------------------------------------------
_VERSION = _pkg_version("parkour-mcp")
_HTTPX_VERSION = _pkg_version("httpx")
_MARKDOWNIFY_VERSION = _pkg_version("markdownify")
_PYTHON_VERSION = platform.python_version()
_PLATFORM = platform.system()  # "Darwin", "Linux", "Windows"

# ---------------------------------------------------------------------------
# User-Agent strings
# ---------------------------------------------------------------------------
# Browser-spoofing identity for HTML page fetches (sites expect a browser).
# Everything that encodes the Chrome version is derived from _CHROME_MAJOR so
# the User-Agent and the Client-Hint headers can never drift out of sync.  WAFs
# weight UA-vs-Client-Hint *inconsistency*, not the version number itself, so
# coherence matters more than currency — bumping Chrome is a one-line change.
_CHROME_MAJOR = "149"

# The Sec-Fetch-* quad describes a top-level, user-initiated navigation with no
# referrer, which is exactly what a parkour fetch is (a human asked for this
# URL).  Sending them — plus the Client Hints a real Chrome always emits — is
# what clears modern WAF consistency checks: Akamai Bot Manager 403s an
# otherwise-Chrome request that arrives without them (and over HTTP/1.1; see
# guarded_fetch).
_FETCH_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        f"Chrome/{_CHROME_MAJOR}.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "sec-ch-ua": (
        f'"Chromium";v="{_CHROME_MAJOR}", '
        f'"Google Chrome";v="{_CHROME_MAJOR}", '
        '"Not_A Brand";v="99"'
    ),
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"macOS"',
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Upgrade-Insecure-Requests": "1",
}

def clean_env(name: str) -> str:
    """Read an env var, treating empty / whitespace-only / unsubstituted
    ``${...}`` templates as unset.

    The Claude Desktop mcpb runtime passes the literal template string
    (e.g. ``${user_config.GITHUB_TOKEN}``) through to the server's
    environment when an optional ``user_config`` field is not filled in
    by the user.  A naive ``os.environ.get`` treats that non-empty
    string as a real value — producing malformed Authorization headers
    and similarly broken configuration downstream.  This helper rejects
    those sentinel shapes so callers can cleanly fall back to filesystem
    config or unauthenticated mode.
    """
    val = os.environ.get(name, "").strip()
    if not val or val.startswith("${"):
        return ""
    return val


# Base directory for filesystem-config fallbacks (API keys, opt-in gates).
_CONFIG_DIR = Path.home() / ".config" / "parkour"


def _parse_truthy_env(name: str) -> bool:
    """True if env var *name* holds an affirmative value (``1`` / ``true`` / ``yes``).

    Case-insensitive and whitespace-tolerant.  Centralizes the opt-in idiom so
    every feature gate accepts the same affirmative set: a gate that rolls its
    own check can (and did) silently reject ``True`` / ``YES`` by forgetting to
    lowercase before comparing.
    """
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes")


def load_credential(env_var: str, config_path: Path) -> str:
    """Load a credential from an env var, falling back to a config file.

    Reads *env_var* through `clean_env` (which rejects unsubstituted ``${...}``
    templates), then falls back to the contents of *config_path*.  Returns an
    empty string when neither source supplies a value, so callers can treat
    ``""`` as "run unauthenticated".

    Callers pass their own path constant rather than a bare filename so the
    constant stays a module-level test seam (tests monkeypatch it to redirect
    the filesystem fallback).
    """
    if key := clean_env(env_var):
        return key
    if config_path.exists():
        return config_path.read_text().strip()
    return ""


# Honest UA for structured API endpoints (MediaWiki, etc.) that expect
# machine clients to identify themselves.  Follows RFC 9110 §10.1.5 and
# Wikimedia User-Agent policy.
#
# Format: product/version (comment) http-library/version renderer/version
# Optional mailto: enables CrossRef "polite pool" (10 req/s vs 5 req/s).
_CONTACT_EMAIL = clean_env("MCP_CONTACT_EMAIL")
_CONTACT_PART = f" mailto:{_CONTACT_EMAIL};" if _CONTACT_EMAIL else ""
_API_USER_AGENT = (
    f"parkour-mcp/{_VERSION} "
    f"(MCP content tool;{_CONTACT_PART} +https://github.com/blightbow/parkour-mcp) "
    f"httpx/{_HTTPX_VERSION} markdownify/{_MARKDOWNIFY_VERSION} "
    f"Python/{_PYTHON_VERSION} {_PLATFORM}"
)

_API_HEADERS = {
    "User-Agent": _API_USER_AGENT,
    "Accept": "application/json",
}

# ---------------------------------------------------------------------------
# File extension → syntax highlight language
# ---------------------------------------------------------------------------
_LANGUAGE_MAP: dict[str, str] = {
    ".py": "python", ".js": "javascript", ".ts": "typescript",
    ".jsx": "javascript", ".tsx": "typescript",
    ".go": "go", ".rs": "rust", ".rb": "ruby",
    ".java": "java", ".kt": "kotlin", ".scala": "scala",
    ".c": "c", ".h": "c", ".cpp": "cpp", ".hpp": "cpp", ".cc": "cpp",
    ".sh": "bash", ".bash": "bash", ".zsh": "zsh",
    ".yaml": "yaml", ".yml": "yaml", ".json": "json",
    ".toml": "toml", ".xml": "xml", ".html": "html", ".css": "css",
    ".md": "markdown", ".sql": "sql", ".r": "r",
    ".swift": "swift", ".m": "objectivec",
}


def _classify_content_type(content_type: str) -> str | None:
    """Coarsely classify an HTTP Content-Type.

    Returns ``"html"``, ``"json"``, ``"xml"``, ``"plain text"``, or None
    for an unsupported type.  XHTML counts as HTML; the priority order
    means a type is never classified as both XML and HTML.
    """
    if "text/html" in content_type or "application/xhtml" in content_type:
        return "html"
    if "application/json" in content_type or "text/json" in content_type:
        return "json"
    if "application/xml" in content_type or "text/xml" in content_type:
        return "xml"
    if "text/plain" in content_type:
        return "plain text"
    return None


# ---------------------------------------------------------------------------
# Rate limiter
# ---------------------------------------------------------------------------

class RateLimiter:
    """Async rate limiter with minimum interval between calls."""

    def __init__(self, min_interval: float):
        self._lock = asyncio.Lock()
        self._last: float = 0.0
        self.min_interval = min_interval

    async def wait(self) -> None:
        async with self._lock:
            elapsed = time.monotonic() - self._last
            if elapsed < self.min_interval:
                await asyncio.sleep(self.min_interval - elapsed)
            self._last = time.monotonic()


# ---------------------------------------------------------------------------
# deps.dev shared client
# ---------------------------------------------------------------------------
# The Packages and Scorecard tools both read from Google's Open Source
# Insights (deps.dev).  One limiter, one fetch helper.  1 req/s is a
# politeness floor: deps.dev publishes no formal limit, and its ToS
# defers to "reasonable" use.

_DEPSDEV_BASE = "https://api.deps.dev/v3"
_DEPSDEV_NOT_FOUND = "Error: Not found on deps.dev."
_depsdev_limiter = RateLimiter(1.0)


async def _depsdev_get(path: str) -> dict | str:
    """GET a deps.dev API path.  Returns parsed JSON or an error string.

    Callers distinguishing 404 from other errors should compare against
    ``_DEPSDEV_NOT_FOUND`` rather than matching a free-form substring.
    """
    await _depsdev_limiter.wait()
    url = f"{_DEPSDEV_BASE}{path}"
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(url, headers=_API_HEADERS)
    except httpx.TimeoutException:
        return "Error: deps.dev API request timed out."
    except httpx.RequestError as exc:
        return f"Error: deps.dev API request failed: {type(exc).__name__}"

    if resp.status_code == 200:
        try:
            data = resp.json()
        except ValueError:
            return "Error: Unexpected response format from deps.dev."
        if not isinstance(data, dict):
            return "Error: Unexpected response format from deps.dev."
        return data
    if resp.status_code == 404:
        return _DEPSDEV_NOT_FOUND
    return f"Error: deps.dev API returned HTTP {resp.status_code}."


# ---------------------------------------------------------------------------
# SSRF protection
# ---------------------------------------------------------------------------

_logger = logging.getLogger(__name__)

# Set MCP_ALLOW_PRIVATE_IPS=1 to allow fetching from private/internal networks.
_ALLOW_PRIVATE_IPS = _parse_truthy_env("MCP_ALLOW_PRIVATE_IPS")


def _is_private_ip(addr: str) -> bool:
    """Check whether an IP address string is private, loopback, or reserved."""
    try:
        ip = ipaddress.ip_address(addr)
    except ValueError:
        return False
    return ip.is_private or ip.is_loopback or ip.is_reserved or ip.is_link_local


def check_url_ssrf(url: str) -> str | None:
    """Validate a URL against SSRF risks before fetching.

    Resolves the hostname to IP addresses and checks each against
    private/loopback/reserved/link-local ranges (IPv4 and IPv6).

    Returns an error string if the URL is blocked, or None if it is safe.
    Disabled when MCP_ALLOW_PRIVATE_IPS=1 is set in the environment.
    """
    if _ALLOW_PRIVATE_IPS:
        return None

    parsed = urlparse(url)
    hostname = parsed.hostname
    if not hostname:
        return None  # let httpx handle malformed URLs

    # Fast check: if hostname is already an IP literal
    try:
        ip = ipaddress.ip_address(hostname)
    except ValueError:
        pass  # hostname is a DNS name, resolve it
    else:
        if _is_private_ip(str(ip)):
            return f"Error: Blocked request to private/reserved address ({hostname})."
        return None

    # Resolve hostname and check all addresses
    try:
        addrinfos = socket.getaddrinfo(hostname, None, socket.AF_UNSPEC, socket.SOCK_STREAM)
    except socket.gaierror:
        return None  # DNS failure — let httpx handle and report the error

    for family, _, _, _, sockaddr in addrinfos:
        addr = str(sockaddr[0])
        if _is_private_ip(addr):
            _logger.debug("SSRF block: %s resolved to private address %s", hostname, addr)
            return f"Error: Blocked request to private/reserved address ({hostname} -> {addr})."

    return None


# ---------------------------------------------------------------------------
# URL scheme allowlist
# ---------------------------------------------------------------------------

# The only schemes any fetcher accepts.  Deliberately independent of
# check_url_ssrf and of MCP_ALLOW_PRIVATE_IPS: that variable opts into
# private *hosts* for local network crawling, and must never also opt into
# schemes that do not describe a network destination at all.
_ALLOWED_URL_SCHEMES = frozenset({"http", "https"})


def check_url_scheme(url: str) -> str | None:
    """Validate that *url* uses a fetchable scheme.

    ``file://`` is the case this exists for.  httpx rejects it, so the
    static path is safe on its own, but the headless-browser path hands the
    URL to ``page.goto()``, which reads local files and returns their
    contents as page text.

    No address-based guard can catch that: a ``file://`` URL has no
    hostname, so ``check_url_ssrf`` finds nothing to resolve and passes it
    through.  Scheme is the only property that distinguishes it.

    Returns an error string if the scheme is rejected, or None if allowed.
    """
    scheme = urlparse(url).scheme.lower()
    if not scheme:
        return "Error: URL must be absolute and begin with http:// or https://."
    if scheme not in _ALLOWED_URL_SCHEMES:
        return (
            f"Error: Unsupported URL scheme '{scheme}://'. "
            "Only http and https can be fetched."
        )
    return None


# ---------------------------------------------------------------------------
# Tool display names — profile-aware lookup for hint/note/see_also strings
# ---------------------------------------------------------------------------

# Canonical mapping from internal tool key to profile-specific display names.
# The ``code`` profile's PascalCase form doubles as the human-readable display
# title surfaced in client UIs regardless of which profile is active — this is
# the convention the README and tool docstrings have used since day one.
TOOL_NAMES: dict[str, dict[str, str]] = {
    "search": {"code": "KagiSearch", "desktop": "kagi_search"},
    "web_fetch_sections": {"code": "WebFetchSections", "desktop": "web_fetch_sections"},
    "web_fetch_direct": {"code": "WebFetchIncisive", "desktop": "web_fetch_incisive"},
    "semantic_scholar": {"code": "SemanticScholar", "desktop": "semantic_scholar"},
    "arxiv": {"code": "ArXiv", "desktop": "arxiv"},
    "research_shelf": {"code": "ResearchShelf", "desktop": "research_shelf"},
    "github": {"code": "GitHub", "desktop": "github"},
    "huggingface": {"code": "HuggingFace", "desktop": "huggingface"},
    "ietf": {"code": "IETF", "desktop": "ietf"},
    "packages": {"code": "Packages", "desktop": "packages"},
    "discourse": {"code": "Discourse", "desktop": "discourse"},
    "mediawiki": {"code": "MediaWiki", "desktop": "mediawiki"},
    "youtube": {"code": "Youtube", "desktop": "youtube"},
    "youtube_comments": {"code": "YoutubeComments", "desktop": "youtube_comments"},
}

# Populated by init_tool_names() at startup; keyed by internal tool name.
_TOOL_DISPLAY_NAMES: dict[str, str] = {}


def init_tool_names(profile: str) -> None:
    """Populate display-name lookup from TOOL_NAMES for the given profile.

    Called once per entrypoint — the MCP server's main(), the Hermes plugin's
    register() — and from test conftest.py.
    """
    assert profile in ("code", "desktop"), f"Unknown profile: {profile!r}"
    _TOOL_DISPLAY_NAMES.clear()
    _TOOL_DISPLAY_NAMES.update(
        {key: names[profile] for key, names in TOOL_NAMES.items()}
    )


# ---------------------------------------------------------------------------
# Semantic Scholar opt-in gate
# ---------------------------------------------------------------------------

_S2_TOS_CONFIG_PATH = _CONFIG_DIR / "s2_accept_tos"


def s2_enabled() -> bool:
    """Return True only if the user has explicitly opted in to Semantic Scholar.

    Checks (in order):
    1. ``S2_ACCEPT_TOS`` environment variable (any truthy value: 1/true/yes)
    2. Presence of ``~/.config/parkour/s2_accept_tos`` file

    The gate is intentionally separate from ``S2_API_KEY``: having a key does
    not imply awareness of the license terms, and S2 functions without one
    (at reduced rate limits).
    """
    if _parse_truthy_env("S2_ACCEPT_TOS"):
        return True
    return _S2_TOS_CONFIG_PATH.is_file()


def tool_name(key: str) -> str:
    """Return the profile-appropriate display name for a tool.

    Asserts that init_tool_names() has been called and *key* is valid.
    """
    assert _TOOL_DISPLAY_NAMES, (
        "tool_name() called before init_tool_names() — "
        "call init_tool_names(profile) at startup or in test conftest.py"
    )
    assert key in _TOOL_DISPLAY_NAMES, (
        f"Unknown tool key {key!r} — "
        f"valid keys: {', '.join(sorted(_TOOL_DISPLAY_NAMES))}"
    )
    return _TOOL_DISPLAY_NAMES[key]


# ---------------------------------------------------------------------------
# Defense-in-depth HTTP fetch — Content-Length gate, streaming size cap,
# wall-clock deadline
# ---------------------------------------------------------------------------

# Default maximum response body size: 5 MiB.  Generous enough for any page a
# human would read; small enough to reject Socrata-style API payloads that
# embed hundreds of megabytes of metadata alongside a handful of rows.
_MAX_RESPONSE_BYTES = 5 * 1024 * 1024

# Larger cap reserved for section-extraction fetches, where the output is a
# heading tree rather than page content.  Accommodates monolithic "one-page"
# specifications (e.g. WHATWG HTML Living Standard, ECMAScript, C++ draft)
# that routinely exceed the 5 MiB content-output cap.  The wall-clock
# deadline still applies, so slow-drip firehoses are still rejected — this
# only relaxes the size gate for callers that don't emit the body to context.
_MAX_SECTIONS_RESPONSE_BYTES = 50 * 1024 * 1024

# Absolute wall-clock deadline for the entire fetch (connect + download).
# httpx's ``timeout`` is per-phase — a slow-dripping server that sends one
# byte every 29 s will never trip a 30 s read timeout.  This caps total time.
_FETCH_DEADLINE_SECONDS = 60.0


class ResponseTooLarge(Exception):
    """Raised when a response exceeds the size cap."""


async def guarded_fetch(
    url: str,
    *,
    headers: dict[str, str] | None = None,
    timeout: float = 30.0,
    max_bytes: int | None = _MAX_RESPONSE_BYTES,
    deadline: float = _FETCH_DEADLINE_SECONDS,
    follow_redirects: bool = True,
) -> httpx.Response:
    """Fetch *url* with layered protection against oversized responses.

    1. **Content-Length gate** — if the server advertises a body larger than
       *max_bytes* via the ``Content-Length`` header, the request is rejected
       immediately without reading the body.  Skipped when *max_bytes* is
       ``None``.

    2. **Streaming size cap** — the body is read in chunks; if the cumulative
       size exceeds *max_bytes* mid-transfer, the stream is closed and
       ``ResponseTooLarge`` is raised.  Skipped when *max_bytes* is ``None``.

    3. **Wall-clock deadline** — an ``asyncio.timeout`` wraps the entire
       operation (connect + all reads).  If *deadline* seconds elapse, an
       ``httpx.TimeoutException`` propagates so callers can handle it the
       same way they already handle per-phase timeouts.  Always applies.

    Passing ``max_bytes=None`` disables layers 1 and 2 for callers whose
    output bound is the caller-supplied ``max_tokens`` (the GitHub blob
    fast path, for example).  Layer 3 still defends against slow-drip
    firehoses that per-phase timeouts can't catch.

    The request is issued over **HTTP/2** when the origin supports it (ALPN
    negotiation, with automatic, transparent fallback to HTTP/1.1 for
    HTTP/1.1-only origins).  Speaking HTTP/2 is required by some WAFs (e.g.
    Akamai Bot Manager) that treat an HTTP/1.1 request carrying a modern-Chrome
    User-Agent as internally inconsistent and 403 it.  If an origin negotiates
    HTTP/2 and then violates the protocol — a rare server bug, or a stale
    pooled connection — the fetch retries once on HTTP/1.1, the more
    battle-hardened transport, before surfacing the error.

    Returns a fully-buffered ``httpx.Response`` (i.e. ``response.text`` works
    synchronously after this call).

    Raises:
        ResponseTooLarge: body exceeded *max_bytes* (only when not ``None``)
        httpx.TimeoutException: per-phase or wall-clock timeout
        httpx.HTTPStatusError: non-2xx status (caller must opt in via raise_for_status)
        httpx.RequestError: connection / DNS / TLS failure
    """
    if headers is None:
        headers = dict(_FETCH_HEADERS)

    async def _attempt(http2: bool) -> httpx.Response:
        async with httpx.AsyncClient(
            follow_redirects=follow_redirects,
            timeout=timeout,
            http2=http2,
        ) as client, client.stream("GET", url, headers=headers) as resp:
            # Layer 1: Content-Length gate
            if max_bytes is not None:
                cl = resp.headers.get("content-length")
                if cl is not None:
                    try:
                        if int(cl) > max_bytes:
                            raise ResponseTooLarge(
                                f"Content-Length {cl} exceeds "
                                f"{max_bytes:,} byte limit"
                            )
                    except ValueError:
                        pass  # malformed header — fall through to streaming

            # Layer 2: streaming size cap
            chunks: list[bytes] = []
            total = 0
            async for chunk in resp.aiter_bytes(chunk_size=65_536):
                total += len(chunk)
                if max_bytes is not None and total > max_bytes:
                    raise ResponseTooLarge(
                        f"Response body exceeded {max_bytes:,} "
                        f"byte limit at {total:,} bytes"
                    )
                chunks.append(chunk)

            # Populate _content so .text / .json() work after the
            # stream context exits — same attr httpx uses internally.
            resp._content = b"".join(chunks)
        # The response object (headers, status_code, _content) survives the
        # context-manager exit; only the transport is closed.
        return resp

    try:
        async with asyncio.timeout(deadline):
            try:
                return await _attempt(http2=True)
            except httpx.RemoteProtocolError:
                # The origin negotiated HTTP/2 via ALPN, then broke the
                # protocol (a buggy server stack, or a stale pooled h2
                # connection).  HTTP/1.1 is the more battle-hardened
                # transport; retry once on it, sharing the same wall-clock
                # deadline, before letting the error surface.
                _logger.debug(
                    "HTTP/2 RemoteProtocolError for %s; retrying on HTTP/1.1", url
                )
                return await _attempt(http2=False)
    except TimeoutError:
        raise httpx.ReadTimeout(
            f"Wall-clock deadline of {deadline}s exceeded for {url}"
        )
