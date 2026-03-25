"""DOI content negotiation and resolution helpers.

Provides async functions for fetching citation metadata via DOI content
negotiation (doi.org) and the DataCite REST API.  These are used for:
  - Passive enrichment of arXiv and Semantic Scholar paper responses
  - The DOI URL fast-path handler (P5)
  - DataCite metadata enrichment (P6)
"""

import asyncio
import logging
import re
import time
from typing import Optional

import httpx

from .common import _API_USER_AGENT

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Rate limiter — shared across all doi.org content negotiation calls.
# CrossRef polite pool: 10 req/s with mailto, 5 req/s without.
# DataCite via doi.org: 1,000/5min (~3.3/s).
# Conservative default: 5/sec (0.2s interval).
# ---------------------------------------------------------------------------
_doi_rate_lock = asyncio.Lock()
_doi_last_request: float = 0.0
_DOI_MIN_INTERVAL = 0.2  # seconds between doi.org requests


# ---------------------------------------------------------------------------
# URL detection
# ---------------------------------------------------------------------------
DOI_URL_RE = re.compile(
    r'https?://(?:dx\.)?doi\.org/(10\.\S+)',
    re.IGNORECASE,
)

ARXIV_DOI_RE = re.compile(
    r'^10\.48550/arXiv\.(.+)$',
    re.IGNORECASE,
)


def _detect_doi_url(url: str) -> Optional[str]:
    """Extract a bare DOI from a doi.org URL, or None."""
    m = DOI_URL_RE.search(url)
    return m.group(1) if m else None


# ---------------------------------------------------------------------------
# Content negotiation
# ---------------------------------------------------------------------------

async def _doi_rate_wait() -> None:
    """Enforce minimum interval between doi.org requests."""
    global _doi_last_request
    async with _doi_rate_lock:
        elapsed = time.monotonic() - _doi_last_request
        if elapsed < _DOI_MIN_INTERVAL:
            await asyncio.sleep(_DOI_MIN_INTERVAL - elapsed)
        _doi_last_request = time.monotonic()


async def fetch_formatted_citation(
    doi: str, *, style: str = "apa", timeout: float = 5.0,
) -> Optional[str]:
    """Fetch a pre-formatted citation string via DOI content negotiation.

    Uses the ``text/x-bibliography`` content type with the specified CSL
    style (default APA).  The doi.org server runs citeproc and returns a
    ready-to-paste citation string.

    Returns the citation string, or None on any failure.
    Designed for concurrent use with asyncio.gather — never raises.
    """
    await _doi_rate_wait()
    try:
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
            resp = await client.get(
                f"https://doi.org/{doi}",
                headers={
                    "User-Agent": _API_USER_AGENT,
                    "Accept": f"text/x-bibliography; style={style}",
                },
            )
            if resp.status_code == 200:
                text = resp.text.strip()
                return text if text else None
            logger.debug("DOI citation fetch HTTP %d for %s", resp.status_code, doi)
            return None
    except Exception as e:
        logger.debug("DOI citation fetch failed for %s: %s", doi, e)
        return None


async def fetch_csl_json(
    doi: str, *, timeout: float = 5.0,
) -> Optional[dict]:
    """Fetch structured CSL-JSON metadata via DOI content negotiation.

    Returns parsed JSON dict with fields like ``author``, ``title``,
    ``DOI``, ``issued``, ``publisher``, ``type``, ``abstract``, etc.

    Returns None on any failure.  Designed for concurrent use — never raises.
    """
    await _doi_rate_wait()
    try:
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
            resp = await client.get(
                f"https://doi.org/{doi}",
                headers={
                    "User-Agent": _API_USER_AGENT,
                    "Accept": "application/vnd.citationstyles.csl+json",
                },
            )
            if resp.status_code == 200:
                return resp.json()
            logger.debug("DOI CSL-JSON fetch HTTP %d for %s", resp.status_code, doi)
            return None
    except Exception as e:
        logger.debug("DOI CSL-JSON fetch failed for %s: %s", doi, e)
        return None
