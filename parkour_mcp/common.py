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

import httpcore
import httpx
from httpx._utils import get_environment_proxies

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
# Which of the two sets to send: be honest unless the destination is hostile
# to legitimate agent-with-human-oversight traffic.
#
# `_API_HEADERS` is the default and the larger set of callers.  It identifies
# the tool and a contact URL, which is what a well-behaved client owes an
# origin, and it asks for the content type the caller actually wants.
#
# `_FETCH_HEADERS` claims to be Chrome.  Reserve it for two situations, both
# of which are the origin's posture rather than our convenience:
#
# * Strict anti-bot WAFs.  Their heuristics are tuned against bulk scraping
#   for model training, and a human-directed single-page fetch is caught as
#   collateral.  The generic fetch path is the case: a caller named the URL
#   and is reading the result.
# * Origins that have withdrawn access to content they do not exclusively
#   license, Reddit being the worked example (see the Reddit OAuth section in
#   TECH_DEBT.md).
#
# Reaching for the browser identity anywhere else is a habit, not a
# requirement, and it costs something real: it is a lie a WAF can catch us in,
# and it sends an HTML `Accept` to endpoints serving JSON.  `discourse.py` sent
# it for months on that basis, and every endpoint it uses answers an honest
# client with 200.
#
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
        resp = await guarded_fetch(url, headers=_API_HEADERS, timeout=15.0)
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
    """Check whether an address is one we refuse to connect to.

    ``is_global`` carries the test.  It is false for every range IANA
    records as not globally reachable, which covers loopback, RFC 1918
    private space, link-local, RFC 6598 shared address space (carrier-grade
    NAT, and the range Alibaba Cloud serves its metadata endpoint from),
    the benchmarking and documentation ranges, and 240.0.0.0/4.

    The other two disjuncts are not redundant with it.  Multicast reports
    ``is_global`` true, and so does NAT64 (``64:ff9b::/96``) which
    ``is_reserved`` catches, so dropping either would newly admit an
    address the predicate is meant to refuse.
    """
    try:
        ip = ipaddress.ip_address(addr)
    except ValueError:
        return False
    return not ip.is_global or ip.is_multicast or ip.is_reserved


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
# Address-pinning transport
# ---------------------------------------------------------------------------


class FetchError(Exception):
    """Base for every outbound-fetch failure this package raises.

    Exists so callers can catch one hierarchy instead of a transport
    library's.  The generic path runs on ``wreq`` and the fast paths still run
    on httpx, and leaking either library's exception types across module
    boundaries is what made the transport unswappable the first time.
    """

    @property
    def label(self) -> str:
        """Short name for this failure, for user-facing error strings.

        Subclasses that wrap a library exception override this to name the
        underlying cause, so translating into this hierarchy does not flatten
        every network problem into one indistinguishable word.
        """
        return type(self).__name__


class BlockedAddress(FetchError, httpx.TransportError):
    """A connection target failed the address check.

    Dual-based during the wreq migration: `FetchError` is what new code
    catches, while ``httpx.TransportError`` keeps the existing
    ``except httpx.RequestError`` arms on the httpx paths working.  The httpx
    base comes off once `guarded_client` is ported.
    """


async def _resolve_and_check(host: str, port: int) -> list[str]:
    """Resolve *host* and reject it if any resolved address is refused.

    Rejecting on *any* refused address, rather than on the one the OS
    happens to return first, is deliberate: a name with both a public and a
    private record lets whoever controls the zone pick which one is used.

    Returns the resolved addresses, first one first.
    """
    try:
        ipaddress.ip_address(host)
    except ValueError:
        infos = await asyncio.get_running_loop().getaddrinfo(
            host, port, family=socket.AF_UNSPEC, type=socket.SOCK_STREAM,
        )
        addresses = [str(info[4][0]) for info in infos]
    else:
        addresses = [host]

    if not addresses:
        raise BlockedAddress(f"{host} did not resolve to any address.")

    if not _ALLOW_PRIVATE_IPS:
        for address in addresses:
            if _is_private_ip(address):
                _logger.debug("blocked connect: %s resolved to %s", host, address)
                raise BlockedAddress(
                    "Blocked request to private/reserved address "
                    f"({host} -> {address})."
                )
    return addresses


class _PinningBackend(httpcore.AsyncNetworkBackend):
    """Network backend that resolves once and connects to what it validated.

    Validating a URL string and letting a lower layer resolve it again is
    the shape behind three separate bypasses: the two layers can disagree on
    how to encode an internationalized name, DNS can change between the two
    lookups, and a redirect can introduce a destination the first check
    never saw.  Resolving here removes the second lookup, so none of the
    three has anywhere to happen.

    Only installed when no proxy is configured.  With a proxy the backend is
    handed the *proxy's* address rather than the destination's, so pinning
    here would check the wrong host.  ``_GuardedTransport`` covers that case
    a layer up.

    Decorates the backend httpcore already selected rather than naming a
    concrete one, so the connection behaviour stays httpcore's and only the
    address decision is ours.  That also keeps whichever async library
    httpcore picked, instead of pinning the choice to one of them.
    """

    def __init__(self, inner: httpcore.AsyncNetworkBackend) -> None:
        self._inner = inner

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options=None,
    ):
        # ``host`` arrives already IDNA-encoded by httpx, so this resolves
        # exactly the name the connection would otherwise have used.
        addresses = await _resolve_and_check(host, port)

        # Pinning to one address bypasses the Happy Eyeballs fallback
        # (RFC 6555) that anyio performs when it resolves the name itself,
        # so walk the list here instead.  Without this a host whose first
        # address is unreachable, the dual-stack case the RFC exists for,
        # fails outright where it would previously have connected.
        #
        # Falling through costs no reach: _resolve_and_check refuses the
        # whole name when any single address is refused, so every address
        # reaching this loop has already passed the check.
        #
        # Order is the resolver's, which on both glibc and macOS is already
        # RFC 6724 destination-address sorted.  anyio re-sorts to force an
        # IPv6 address first, which discards that.  The parallel race is
        # deliberately not reproduced: attempts are sequential, so a
        # blackholed first address costs its connect timeout rather than
        # RFC 8305's 250 ms stagger.  TLS is applied by a separate httpcore
        # step carrying the original hostname, so SNI and certificate
        # verification are unaffected either way.
        last = len(addresses) - 1
        for index, address in enumerate(addresses):
            try:
                return await self._inner.connect_tcp(
                    address, port, timeout=timeout,
                    local_address=local_address, socket_options=socket_options,
                )
            except (httpcore.ConnectError, httpcore.ConnectTimeout):
                if index == last:
                    raise
                _logger.debug(
                    "connect to %s (%s) failed; trying next address", address, host,
                )
        raise AssertionError("unreachable: _resolve_and_check rejects an empty list")

    async def connect_unix_socket(
        self, path: str, timeout: float | None = None, socket_options=None,
    ):
        # No address to check: a unix socket names a filesystem path, and
        # httpx only reaches this when a caller configures uds= explicitly.
        return await self._inner.connect_unix_socket(
            path, timeout=timeout, socket_options=socket_options,
        )

    async def sleep(self, seconds: float) -> None:
        await self._inner.sleep(seconds)


class _GuardedTransport(httpx.AsyncHTTPTransport):
    """Transport that address-checks every destination, including redirects.

    Two layers, because they see different things:

    * Without a proxy the check lives in :class:`_PinningBackend`, which
      connects to the address it validated.  Nothing can change between the
      check and the connection.
    * With a proxy the backend only ever sees the proxy, so the check moves
      to ``handle_async_request``, which sees the real destination.  That
      check is advisory: the proxy performs the resolution that actually
      reaches the network, and no local check can bind it.  ``pinned``
      records which of the two applies so callers can say so.

    httpx calls the transport once per redirect hop, so both layers cover
    redirect chains with no redirect-specific code.
    """

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        pool = self._pool
        # httpx swaps in a proxy pool when a proxy is configured, and both
        # proxy classes subclass AsyncConnectionPool, so a bare isinstance
        # would match them too.
        if isinstance(pool, httpcore.AsyncConnectionPool) and not isinstance(
            pool, (httpcore.AsyncHTTPProxy, httpcore.AsyncSOCKSProxy)
        ):
            self.pinned = True
            pool._network_backend = _PinningBackend(pool._network_backend)
        else:
            self.pinned = False

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        if not self.pinned:
            # Proxied: the backend never sees this host, so check it here.
            host = request.url.host
            if host:
                default_port = 443 if request.url.scheme == "https" else 80
                await _resolve_and_check(host, request.url.port or default_port)
        return await super().handle_async_request(request)


def guarded_client(**kwargs) -> httpx.AsyncClient:
    """Build an ``httpx.AsyncClient`` whose destinations are address-checked.

    The sanctioned way to construct an outbound client.  A bare
    ``httpx.AsyncClient`` resolves and connects with no address check, so
    every caller-reachable destination must come through here.

    Environment proxies are reproduced explicitly.  httpx computes
    ``allow_env_proxies = trust_env and transport is None``, so passing a
    transport at all makes it skip ``HTTPS_PROXY`` and friends entirely.
    Left alone that would silently drop a configured egress proxy, which in
    a deployment where the proxy *is* the egress control removes that
    control, and would also leave every transport reporting ``pinned`` when
    a proxy is in play.

    Accepts the same keyword arguments as ``httpx.AsyncClient``.
    """
    # httpx routes these around the guard rather than through it: an
    # explicit `proxy=` builds a plain AsyncHTTPTransport for the mount, and
    # `transport=` / `mounts=` replace ours outright.  Refusing them is
    # louder than silently returning a client that does not check anything.
    for unsupported in ("transport", "mounts"):
        if unsupported in kwargs:
            raise TypeError(
                f"guarded_client() does not accept {unsupported}=: it would "
                "replace the transport that performs the address check"
            )

    kwargs.setdefault("timeout", 30.0)
    http2 = kwargs.pop("http2", False)
    verify = kwargs.pop("verify", True)
    explicit_proxy = kwargs.pop("proxy", None)

    def _transport(proxy: str | None = None) -> _GuardedTransport:
        return _GuardedTransport(http2=http2, verify=verify, proxy=proxy)

    if explicit_proxy is not None:
        mounts: dict[str, httpx.AsyncBaseTransport | None] = {
            "all://": _transport(explicit_proxy),
        }
    else:
        # A None value means "reach this pattern directly" (NO_PROXY), which
        # still wants a guarded transport, just an unproxied one.
        mounts = {
            pattern: _transport(proxy_url)
            for pattern, proxy_url in get_environment_proxies().items()
        }
    return httpx.AsyncClient(transport=_transport(), mounts=mounts, **kwargs)


# Standard proxy variables, in the spellings httpx honours via trust_env.
_PROXY_ENV_VARS = (
    "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY",
    "http_proxy", "https_proxy", "all_proxy",
)


def proxy_in_effect() -> bool:
    """Whether outbound requests may be routed through a proxy.

    A proxy performs the resolution that actually reaches the network, so
    the address check degrades from pinned to advisory whenever one is
    configured.  Callers surface this so the weaker guarantee is visible
    rather than assumed.

    Approximate by design: it does not evaluate ``NO_PROXY``, so a
    per-host exemption still reports True.  Over-reporting a caveat is the
    safe direction to be wrong in.
    """
    return any(os.environ.get(var) for var in _PROXY_ENV_VARS)


_PROXY_DEGRADED_WARNING = (
    "private-address protection degraded: a proxy is configured, so the "
    "proxy resolves and connects, and the address check could not be "
    "enforced at the socket"
)


def proxy_warning() -> str | None:
    """The degradation warning when a proxy is configured, else None.

    Lives here so every tool that fetches a caller-supplied host reports
    the same caveat in the same words.  ``_build_frontmatter`` drops
    ``None``, so callers can pass the result through unconditionally.
    """
    return _PROXY_DEGRADED_WARNING if proxy_in_effect() else None


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


class ResponseTooLarge(FetchError):
    """Raised when a response exceeds the size cap."""


async def guarded_fetch(
    url: str,
    *,
    method: str = "GET",
    params: dict | None = None,
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
    HTTP/1.1-only origins).  If an origin negotiates HTTP/2 and then violates
    the protocol (a rare server bug, or a stale pooled connection), the fetch
    retries once on HTTP/1.1, the more battle-hardened transport, before
    surfacing the error.

    That HTTP/2 default is a tradeoff between two WAF vendors that want
    opposite things, not a universally safer choice.  Both score the
    *coherence* of the claimed identity against the observed transport
    fingerprint, and they disagree about which pairing is incoherent:

    * Akamai Bot Manager reads HTTP/1.1 carrying a modern-Chrome User-Agent
      as internally inconsistent and 403s it, so it wants HTTP/2.
    * Cloudflare zones running a strict Managed Challenge (the 403 carries
      ``cf-mitigated: challenge``) compare our HTTP/2 SETTINGS frame and
      header ordering against Chrome's and refuse the mismatch, so they want
      HTTP/1.1.  This is a per-zone sensitivity setting, not a Cloudflare
      default: ``support.nzxt.com`` and ``support.discord.com`` challenge us
      on HTTP/2 and serve us on HTTP/1.1, while ``support.zendesk.com`` and
      ``developers.cloudflare.com`` serve us on either.

    No choice of default satisfies both, because the underlying problem is
    that httpx emits the ``h2`` library's fingerprint while the User-Agent
    claims Chrome.  The HTTP/1.1 retry above does not rescue the Cloudflare
    case either: a challenge is a well-formed 403, not a
    ``RemoteProtocolError``.  Only a genuine browser fingerprint resolves it,
    which is what ``wreq`` supplies on the generic path.  See
    TECH_DEBT.md for the migration assessment.

    Returns a fully-buffered ``httpx.Response`` (i.e. ``response.text`` works
    synchronously after this call).

    Raises:
        ResponseTooLarge: body exceeded *max_bytes* (only when not ``None``)
        httpx.TimeoutException: per-phase or wall-clock timeout
        httpx.HTTPStatusError: non-2xx status (caller must opt in via raise_for_status)
        httpx.RequestError: connection / DNS / TLS failure
    """
    # Honest identification is the default.  Every caller of this function is
    # now a fixed-host API path (the generic browser-facing path moved to
    # `_transport.py`), and those identify rather than impersonate; see the
    # header-selection policy above `_FETCH_HEADERS`.  A caller that genuinely
    # needs the browser identity passes it explicitly, which makes the lie a
    # visible decision at the call site rather than an inherited default.
    if headers is None:
        headers = dict(_API_HEADERS)

    async def _attempt(http2: bool) -> httpx.Response:
        async with guarded_client(
            follow_redirects=follow_redirects,
            timeout=timeout,
            http2=http2,
        ) as client, client.stream(
            method, url, headers=headers, params=params,
        ) as resp:
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
