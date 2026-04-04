"""GitHub API integration for repository, issue, PR, and code lookup.

Provides a standalone GitHub tool with search, issue/PR viewing, file
fetching, and repo metadata — plus URL detection for the fast-path chain
in fetch_direct.py and fetch_js.py.

Uses httpx directly (no wrapper library) for consistency with the rest of
the codebase.  Authentication is optional: unauthenticated requests get
60 req/hr on the core API; a GITHUB_TOKEN bumps that to 5000/hr.
"""

import asyncio
import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import httpx

from .common import _API_USER_AGENT, RateLimiter

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

GITHUB_CONFIG_PATH = Path.home() / ".config" / "kagi" / "github_token"
_GITHUB_API_VERSION = "2022-11-28"

_NO_TOKEN_MSG = (
    "Rate limited. Unauthenticated GitHub API allows only 60 requests/hour.\n"
    "Set GITHUB_TOKEN env var or create ~/.config/kagi/github_token "
    "with a personal access token.\n"
    "No special scopes needed for public repos. "
    "See: https://github.com/settings/tokens"
)

# ---------------------------------------------------------------------------
# Rate limiter — 1 request per second baseline politeness
# ---------------------------------------------------------------------------

_github_limiter = RateLimiter(1.0)

# ---------------------------------------------------------------------------
# Authentication
# ---------------------------------------------------------------------------


def _get_github_token() -> str:
    """Load GitHub token from env var, config file, or return empty string."""
    if key := os.environ.get("GITHUB_TOKEN"):
        return key
    if GITHUB_CONFIG_PATH.exists():
        return GITHUB_CONFIG_PATH.read_text().strip()
    return ""


def _github_headers(accept: str = "application/vnd.github.v3+json") -> dict:
    """Build request headers with optional auth and API version pinning."""
    headers = {
        "User-Agent": _API_USER_AGENT,
        "Accept": accept,
        "X-GitHub-Api-Version": _GITHUB_API_VERSION,
    }
    token = _get_github_token()
    if token:
        headers["Authorization"] = f"token {token}"
    return headers


# ---------------------------------------------------------------------------
# Rate limit tracking (per-resource: "core" vs "search")
# ---------------------------------------------------------------------------

@dataclass
class _GitHubRateLimit:
    """Parsed rate limit state from GitHub response headers."""
    limit: int = 0
    remaining: int = 0
    reset_epoch: float = 0.0
    resource: str = "core"

    @classmethod
    def from_headers(cls, headers: httpx.Headers) -> Optional["_GitHubRateLimit"]:
        """Parse X-RateLimit-* headers, or None if absent."""
        try:
            return cls(
                limit=int(headers["x-ratelimit-limit"]),
                remaining=int(headers["x-ratelimit-remaining"]),
                reset_epoch=float(headers["x-ratelimit-reset"]),
                resource=headers.get("x-ratelimit-resource", "core"),
            )
        except (KeyError, ValueError):
            return None


# Per-resource rate limit state (updated after each request)
_rate_limits: dict[str, _GitHubRateLimit] = {}


# ---------------------------------------------------------------------------
# Core HTTP request
# ---------------------------------------------------------------------------

_MAX_RETRIES = 3
_RETRY_BACKOFF = 1.0  # seconds; constant backoff (gh CLI pattern)

# Link header pagination regex
_LINK_RE = re.compile(r'<([^>]+)>;\s*rel="([^"]+)"')


def _next_page_url(link_header: Optional[str]) -> Optional[str]:
    """Extract the 'next' URL from a Link response header."""
    if not link_header:
        return None
    for match in _LINK_RE.finditer(link_header):
        if match.group(2) == "next":
            return match.group(1)
    return None


async def _github_request(
    method: str,
    path: str,
    params: Optional[dict] = None,
    accept: str = "application/vnd.github.v3+json",
) -> dict | list | str:
    """Core HTTP call to the GitHub API.

    Returns parsed JSON (dict or list) on success, or an error string on
    failure.  Retries on 5xx with constant backoff (max 3 attempts).
    Tracks rate limit state per-resource from response headers.
    """
    url = f"https://api.github.com{path}" if path.startswith("/") else path

    for attempt in range(_MAX_RETRIES + 1):
        await _github_limiter.wait()

        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.request(
                    method, url,
                    headers=_github_headers(accept),
                    params=params,
                )
        except httpx.TimeoutException:
            return "Error: GitHub API request timed out."
        except httpx.RequestError as e:
            return f"Error: GitHub API request failed - {type(e).__name__}"

        # Track rate limit state
        rl = _GitHubRateLimit.from_headers(response.headers)
        if rl:
            _rate_limits[rl.resource] = rl

        # Success
        if response.status_code in (200, 201):
            return response.json()
        if response.status_code == 204:
            return {}

        # 404
        if response.status_code == 404:
            return "Error: Not found on GitHub."

        # 403 — rate limit or auth/scope issue
        if response.status_code == 403:
            if rl and rl.remaining == 0:
                if _get_github_token():
                    return f"Error: Rate limited. Resets at epoch {int(rl.reset_epoch)}."
                return f"Error: {_NO_TOKEN_MSG}"
            # Scope issue — check accepted vs actual scopes
            needed = response.headers.get("x-accepted-oauth-scopes", "")
            has = response.headers.get("x-oauth-scopes", "")
            if needed:
                return (
                    f"Error: HTTP 403 — this endpoint requires the "
                    f"'{needed}' scope. Your token has: '{has or 'none'}'."
                )
            return "Error: HTTP 403 Forbidden."

        # 422 — validation error (bad search query, etc.)
        if response.status_code == 422:
            try:
                body = response.json()
                errors = body.get("errors", [])
                if errors:
                    msg = errors[0].get("message", body.get("message", ""))
                    return f"Error: Invalid request — {msg}"
                return f"Error: Invalid request — {body.get('message', 'Unprocessable Entity')}"
            except Exception:
                return "Error: HTTP 422 Unprocessable Entity."

        # 5xx — retry
        if response.status_code >= 500:
            if attempt < _MAX_RETRIES:
                logger.info(
                    "GitHub %d on %s, retry %d after %.1fs",
                    response.status_code, path, attempt + 1, _RETRY_BACKOFF,
                )
                await asyncio.sleep(_RETRY_BACKOFF)
                continue
            return f"Error: GitHub API returned HTTP {response.status_code}."

        # Other 4xx
        return f"Error: GitHub API returned HTTP {response.status_code}."

    return "Error: GitHub API request failed."


# ---------------------------------------------------------------------------
# URL detection
# ---------------------------------------------------------------------------

@dataclass
class GitHubUrlMatch:
    """Parsed components of a GitHub URL."""
    kind: str  # "blob", "tree", "issue", "pull", "discussion", "repo", "gist"
    owner: str = ""
    repo: str = ""
    number: Optional[int] = None
    ref: Optional[str] = None
    path: Optional[str] = None
    gist_id: Optional[str] = None


# Main github.com URL — captures owner, repo, and remaining path
_GITHUB_URL_RE = re.compile(
    r"https?://github\.com/([^/]+)/([^/]+?)(?:\.git)?(?:/(.*))?$",
    re.IGNORECASE,
)

# Sub-path patterns (applied after main pattern matches)
_BLOB_RE = re.compile(r"^blob/([^/]+)/(.+)$")
_TREE_RE = re.compile(r"^tree/([^/]+)/(.+)$")
_ISSUE_RE = re.compile(r"^issues/(\d+)(?:/.*)?$")
_PULL_RE = re.compile(r"^pull/(\d+)(?:/.*)?$")
_DISCUSSION_RE = re.compile(r"^discussions/(\d+)(?:/.*)?$")

# Gist URL
_GIST_URL_RE = re.compile(
    r"https?://gist\.github\.com/(?:[^/]+/)?([0-9a-f]+)",
    re.IGNORECASE,
)


def _detect_github_url(url: str) -> Optional[GitHubUrlMatch]:
    """Parse a GitHub URL into its components, or return None.

    Supports github.com repo URLs (blob, tree, issues, pull, discussions,
    repo root) and gist.github.com URLs.

    Discussion detection is gated on authentication — returns None for
    discussion URLs when no GITHUB_TOKEN is configured, allowing them to
    fall through to generic HTTP fetch rather than silently failing.
    """
    # Gist
    m = _GIST_URL_RE.match(url)
    if m:
        return GitHubUrlMatch(kind="gist", gist_id=m.group(1))

    # Main github.com
    m = _GITHUB_URL_RE.match(url)
    if not m:
        return None

    owner, repo, rest = m.group(1), m.group(2), m.group(3) or ""

    # Skip non-repo paths (e.g. github.com/settings, github.com/orgs/...)
    if owner in ("settings", "orgs", "marketplace", "explore",
                 "topics", "trending", "collections", "sponsors",
                 "features", "security", "enterprise", "pricing",
                 "login", "signup", "join", "new"):
        return None

    if not rest:
        return GitHubUrlMatch(kind="repo", owner=owner, repo=repo)

    # Blob (source file)
    bm = _BLOB_RE.match(rest)
    if bm:
        return GitHubUrlMatch(
            kind="blob", owner=owner, repo=repo,
            ref=bm.group(1), path=bm.group(2),
        )

    # Tree (directory)
    tm = _TREE_RE.match(rest)
    if tm:
        return GitHubUrlMatch(
            kind="tree", owner=owner, repo=repo,
            ref=tm.group(1), path=tm.group(2),
        )

    # Issue
    im = _ISSUE_RE.match(rest)
    if im:
        return GitHubUrlMatch(
            kind="issue", owner=owner, repo=repo,
            number=int(im.group(1)),
        )

    # Pull request
    pm = _PULL_RE.match(rest)
    if pm:
        return GitHubUrlMatch(
            kind="pull", owner=owner, repo=repo,
            number=int(pm.group(1)),
        )

    # Discussion (auth-gated)
    dm = _DISCUSSION_RE.match(rest)
    if dm and _get_github_token():
        return GitHubUrlMatch(
            kind="discussion", owner=owner, repo=repo,
            number=int(dm.group(1)),
        )

    return None


# ---------------------------------------------------------------------------
# Rate limit warning for frontmatter
# ---------------------------------------------------------------------------

def _rate_limit_warning() -> Optional[str]:
    """Return a warning string if rate limit is low and unauthenticated."""
    if _get_github_token():
        return None
    core = _rate_limits.get("core")
    if core and core.remaining < 10:
        return (
            f"GitHub API rate limit low ({core.remaining}/{core.limit} remaining). "
            "Set GITHUB_TOKEN for 5000 req/hr."
        )
    return None
