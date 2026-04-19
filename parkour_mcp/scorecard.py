"""OpenSSF Scorecard lookups via Google Open Source Insights (deps.dev).

Thin wrapper that extracts the overall score and assessment date from
``GET /v3/projects/github.com/{owner}/{repo}`` so ``github.py`` can
enrich repo and file frontmatter with a compact trust signal.

We deliberately do **not** hit ``api.securityscorecards.dev`` directly.
That endpoint is a CI-upload registry: repos that run the
``ossf/scorecard-action`` GitHub Action with ``publish_results: true``
push their own result, but repos that do not are either missing or
frozen to the last time they ran it.  The direct API returned a
2022-11-09 snapshot for curl/curl while deps.dev returned 2026-04-06.
deps.dev ingests OpenSSF's weekly server-side cron scan of the top
~1M critical projects (published to the ``openssf:scorecardcron``
BigQuery dataset), so it has far broader, fresher coverage.

Uses the shared ``_depsdev_get`` helper and 1 req/s limiter from
``common.py`` so Packages and Scorecard tools share one HTTP client
and politeness gate.
"""

import logging

from .common import _DEPSDEV_NOT_FOUND, _depsdev_get

logger = logging.getLogger(__name__)


# Session-lived lookup cache.  The BigQuery cron refreshes scorecards
# weekly, so memoizing within a server process is safe and makes the
# blob/file enrichment path free on repeat hits against the same repo.
# Cache value is ``(score, iso_date) | None``; ``None`` means upstream
# returned either 404 or a project with no scorecard subfield.
_cache: dict[tuple[str, str], tuple[float, str] | None] = {}


def _reset_cache() -> None:
    """Clear the scorecard cache.  Test hook only."""
    _cache.clear()


async def fetch_overall(owner: str, repo: str) -> tuple[float, str] | None:
    """Return the OpenSSF overall Scorecard score for ``owner/repo``.

    Returns ``(score, iso_date)`` when deps.dev has a scorecard entry,
    or ``None`` when it does not (404, missing ``scorecard`` field, or
    malformed response).  Transient errors (timeout, connection) also
    return ``None`` but do not populate the cache, so a later retry
    can succeed.

    The ISO date is the ``YYYY-MM-DD`` prefix of deps.dev's ``date``
    field; empty string when upstream omits it.  Callers pair the
    score and date through ``format_score`` for the frontmatter value.
    """
    key = (owner.lower(), repo.lower())
    if key in _cache:
        return _cache[key]

    # deps.dev requires URL-encoded project IDs; ``/`` in the path
    # must be percent-encoded or the service returns 404.
    path = f"/projects/github.com%2F{owner}%2F{repo}"
    result = await _depsdev_get(path)

    if isinstance(result, str):
        if result == _DEPSDEV_NOT_FOUND:
            _cache[key] = None
            return None
        # Transient error: don't cache so a retry can succeed.
        logger.debug("Scorecard fetch for %s/%s: %s", owner, repo, result)
        return None

    scorecard = result.get("scorecard") if isinstance(result, dict) else None
    if not isinstance(scorecard, dict):
        _cache[key] = None
        return None

    score = scorecard.get("overallScore")
    if not isinstance(score, (int, float)):
        _cache[key] = None
        return None

    raw_date = scorecard.get("date", "")
    iso_date = raw_date[:10] if isinstance(raw_date, str) else ""
    cached = (float(score), iso_date)
    _cache[key] = cached
    return cached


def format_score(score: float, iso_date: str) -> str:
    """Format a score and ISO date as ``"N/10 (@ YYYY-MM-DD)"``.

    Shared formatter so the GitHub and Packages tools produce an
    identical ``openssf_scorecard`` value.  The date clause is dropped
    when *iso_date* is empty (only happens if upstream omits it).
    ``@`` is shorthand for "assessed at"; the key name plus ISO date
    makes the relationship unambiguous without the verbose verb and
    keeps the value compact for the file-read hot path.
    """
    base = f"{score:g}/10"
    if iso_date:
        return f"{base} (@ {iso_date})"
    return base
