"""Shared constants and utilities for kagi-research-mcp."""

import asyncio
import os
import platform
import time
from importlib.metadata import version as _pkg_version

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
