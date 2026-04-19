"""OpenSSF Scorecard lookups against api.securityscorecards.dev.

Thin client used to enrich GitHub repository views with the overall
Scorecard rating.  The upstream endpoint is unauthenticated, CDN-fronted,
and returns the same shape deps.dev passes through; see
``_format_project`` in ``packages.py`` for the richer breakdown variant.

Kept deliberately small: callers want a single float for frontmatter,
not the full per-check tree.
"""

import logging

import httpx

from .common import _API_HEADERS, RateLimiter

logger = logging.getLogger(__name__)

_SCORECARD_BASE = "https://api.securityscorecards.dev"

# Politeness limiter.  The service advertises no rate limit and is
# edge-cached, but we keep parity with other sibling clients that all run
# a 1 req/s gate.
_scorecard_limiter = RateLimiter(1.0)

# Session-lived lookup cache.  Scorecard results refresh at most weekly
# upstream, so memoizing within a server process is safe and makes the
# blob/file enrichment path free on repeat hits against the same repo.
# A ``None`` entry means "upstream returned no score" and we cache that too
# so an agent walking a 404 repo doesn't replay the miss on every file.
_cache: dict[tuple[str, str], float | None] = {}


def _reset_cache() -> None:
    """Clear the scorecard cache.  Test hook only."""
    _cache.clear()


async def fetch_overall(owner: str, repo: str) -> float | None:
    """Return the OpenSSF overall Scorecard score for ``owner/repo``.

    Returns the 0-10 score when the project has been scanned, or ``None``
    for 404, network error, or malformed response.  Callers use the
    ``None`` signal to omit the frontmatter key entirely rather than
    emitting a null / "unknown" placeholder.
    """
    key = (owner.lower(), repo.lower())
    if key in _cache:
        return _cache[key]

    await _scorecard_limiter.wait()
    url = f"{_SCORECARD_BASE}/projects/github.com/{owner}/{repo}"
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(url, headers=_API_HEADERS)
    except (httpx.TimeoutException, httpx.RequestError) as exc:
        logger.debug("Scorecard fetch failed for %s/%s: %s", owner, repo, exc)
        return None  # transient: skip cache so a later call can retry

    if resp.status_code != 200:
        _cache[key] = None
        return None

    try:
        data = resp.json()
    except ValueError:
        _cache[key] = None
        return None

    score = data.get("score") if isinstance(data, dict) else None
    result = float(score) if isinstance(score, (int, float)) else None
    _cache[key] = result
    return result
