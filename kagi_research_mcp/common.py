"""Shared constants and utilities for kagi-research-mcp."""

import asyncio
import ipaddress
import logging
import os
import platform
import socket
import time
from importlib.metadata import version as _pkg_version
from urllib.parse import urlparse

# ---------------------------------------------------------------------------
# Package / runtime versions (used in User-Agent strings)
# ---------------------------------------------------------------------------
_VERSION = _pkg_version("kagi-research-mcp")
_HTTPX_VERSION = _pkg_version("httpx")
_MARKDOWNIFY_VERSION = _pkg_version("markdownify")
_PYTHON_VERSION = platform.python_version()
_PLATFORM = platform.system()  # "Darwin", "Linux", "Windows"

# ---------------------------------------------------------------------------
# User-Agent strings
# ---------------------------------------------------------------------------
# Browser-spoofing UA for HTML page fetches (sites expect a browser)
_FETCH_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
}

# Honest UA for structured API endpoints (MediaWiki, etc.) that expect
# machine clients to identify themselves.  Follows RFC 9110 §10.1.5 and
# Wikimedia User-Agent policy.
#
# Format: product/version (comment) http-library/version renderer/version
# Optional mailto: enables CrossRef "polite pool" (10 req/s vs 5 req/s).
_CONTACT_EMAIL = os.environ.get("MCP_CONTACT_EMAIL", "")
_CONTACT_PART = f" mailto:{_CONTACT_EMAIL};" if _CONTACT_EMAIL else ""
_API_USER_AGENT = (
    f"kagi-research-mcp/{_VERSION} "
    f"(MCP content tool;{_CONTACT_PART} +https://github.com/blightbow/kagi-research-mcp) "
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
# SSRF protection
# ---------------------------------------------------------------------------

_logger = logging.getLogger(__name__)

# Set MCP_ALLOW_PRIVATE_IPS=1 to allow fetching from private/internal networks.
_ALLOW_PRIVATE_IPS = os.environ.get("MCP_ALLOW_PRIVATE_IPS", "").strip() in ("1", "true", "yes")


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
        if _is_private_ip(str(ip)):
            return f"Error: Blocked request to private/reserved address ({hostname})."
        return None
    except ValueError:
        pass  # hostname is a DNS name, resolve it

    # Resolve hostname and check all addresses
    try:
        addrinfos = socket.getaddrinfo(hostname, None, socket.AF_UNSPEC, socket.SOCK_STREAM)
    except socket.gaierror:
        return None  # DNS failure — let httpx handle and report the error

    for family, _, _, _, sockaddr in addrinfos:
        addr = sockaddr[0]
        if _is_private_ip(addr):
            _logger.debug("SSRF block: %s resolved to private address %s", hostname, addr)
            return f"Error: Blocked request to private/reserved address ({hostname} -> {addr})."

    return None
