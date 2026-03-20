"""Shared constants for claude-web-tools."""

import platform
from importlib.metadata import version as _pkg_version

# ---------------------------------------------------------------------------
# Package / runtime versions (used in User-Agent strings)
# ---------------------------------------------------------------------------
_VERSION = _pkg_version("claude-web-tools")
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
_API_USER_AGENT = (
    f"claude-web-tools/{_VERSION} "
    f"(MCP content tool; +https://github.com/blightbow/claude-web-tools) "
    f"httpx/{_HTTPX_VERSION} markdownify/{_MARKDOWNIFY_VERSION} "
    f"Python/{_PYTHON_VERSION} {_PLATFORM}"
)

_API_HEADERS = {
    "User-Agent": _API_USER_AGENT,
    "Accept": "application/json",
}
