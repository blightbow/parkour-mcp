"""Kagi search and summarization tools.

Search runs against the v1 public-preview API via httpx. Summarize remains
on the v0 surface via the legacy kagiapi client until the /summarize
endpoint lands on v1; the v0 helpers (`get_client`, `_extract_balance`,
`_check_balance`, `_summarize_locked`, `_handle_v0_error`) are dormant
load-bearing for that second tool and retire when the migration completes.
"""

import logging
from pathlib import Path
from typing import Annotated, Any, Literal, Optional

import httpx
from kagiapi import KagiClient
from pydantic import Field

from .common import _API_USER_AGENT, clean_env, tool_name
from .markdown import (
    FMEntries,
    _append_frontmatter_entry,
    _build_frontmatter,
    _fence_content,
    _TRUST_ADVISORY,
)

logger = logging.getLogger(__name__)

CONFIG_PATH = Path.home() / ".config" / "parkour" / "kagi_api_key"

_NO_KEY_MSG = (
    "Error: API key not found. "
    "Create ~/.config/parkour/kagi_api_key or set KAGI_API_KEY env var."
)

# ---------------------------------------------------------------------------
# v1 public-preview API
# ---------------------------------------------------------------------------

_V1_BASE = "https://kagi.com/api/v1"
_V1_TIMEOUT = 30.0

_WorkflowType = Literal["search", "images", "videos", "news", "podcasts"]

# Workflow → primary result key in the v1 response's data object.  Confirmed
# against the live endpoint: every workflow uses the singular form (video,
# not videos), and the workflow name in the request body uses the plural form
# (videos, not video).  data may carry additional secondary categories per
# workflow (video_creator, interesting_news, podcast_creator, infobox,
# related_search) — only related_search is surfaced here.
_WORKFLOW_RESULT_KEY: dict[str, str] = {
    "search": "search",
    "videos": "video",
    "news": "news",
    "images": "image",
    "podcasts": "podcast",
}
_VALID_WORKFLOWS = frozenset(_WORKFLOW_RESULT_KEY)


# ---------------------------------------------------------------------------
# v0 dormant island (powers summarize until /summarize lands on v1)
# ---------------------------------------------------------------------------

_LOW_BALANCE_THRESHOLD = 1.00  # dollars

# Session lockout for summarize. Flipped True by v0 responses with low
# balance; cleared by v0 responses with healthy balance on non-summarize
# calls. v1 search does not touch this — v1 dropped meta.api_balance.
_summarize_locked: bool = False


def _extract_balance(response: dict) -> Optional[float]:
    """Extract api_balance from a v0 Kagi response."""
    meta = response.get("meta", {})
    balance = meta.get("api_balance")
    if balance is not None:
        try:
            return float(balance)
        except (TypeError, ValueError):
            pass
    return None


def _check_balance(response: Any, is_summarize: bool = False) -> Optional[str]:
    """Check balance on a v0 response, update lockout state, return warning or None.

    Non-summarize calls clear the lockout if balance has recovered.
    """
    global _summarize_locked
    balance = _extract_balance(response)
    if balance is None:
        return None

    if balance < _LOW_BALANCE_THRESHOLD:
        _summarize_locked = True
        return (
            f"Kagi API balance low: ${balance:.2f} remaining. "
            f"Add funds at https://kagi.com/settings?p=billing"
        )
    else:
        if not is_summarize:
            _summarize_locked = False
        return None


def _handle_v0_error(e: Exception) -> str:
    """Format a kagiapi (v0) exception into a user-facing error string.

    v0 error envelope: ``{"error": [{"code": 101, "msg": "..."}]}``.
    requests.Response.__bool__ returns False for 4xx/5xx — compare with
    ``is not None``.
    """
    response = getattr(e, "response", None)
    status_code = getattr(response, "status_code", None)

    if response is not None:
        response_text = getattr(response, "text", None)
        if response_text:
            try:
                import json
                body = json.loads(response_text)
                errors = body.get("error") or []
                if errors and isinstance(errors, list):
                    kagi_msg = errors[0].get("msg", "")
                    if "Insufficient credit" in kagi_msg:
                        return "Error: Insufficient API credits. Add funds at https://kagi.com/settings?p=billing_api"
            except (json.JSONDecodeError, KeyError, IndexError):
                pass

    if status_code == 401:
        return "Error: Invalid API key. Check ~/.config/parkour/kagi_api_key or KAGI_API_KEY env var."
    if status_code == 402:
        return "Error: Insufficient API credits. Add funds at https://kagi.com/settings?p=billing_api"
    return f"Error: {e}"


def get_client() -> Optional[KagiClient]:
    """Create a Kagi v0 client. Used only by summarize until /summarize migrates."""
    api_key = get_api_key()
    if not api_key:
        return None
    return KagiClient(api_key=api_key)


# ---------------------------------------------------------------------------
# v1 error parser
# ---------------------------------------------------------------------------

def _handle_v1_error(e: Exception) -> str:
    """Format an httpx (v1) exception into a user-facing error string.

    v1 error envelope::

        {"errors": [{"code": "general.unauthorized",
                     "url": "https://kagi.com/api#todo",
                     "message": "Unauthorized"}]}

    Codes are dotted strings; the structural shape applies on all non-2xx
    responses observed so far.
    """
    if isinstance(e, httpx.TimeoutException):
        return "Error: Kagi API request timed out."

    if isinstance(e, httpx.HTTPStatusError):
        status_code = e.response.status_code
        try:
            body = e.response.json()
        except ValueError:
            body = None

        if isinstance(body, dict):
            errors = body.get("errors") or []
            if isinstance(errors, list) and errors:
                err = errors[0] if isinstance(errors[0], dict) else {}
                code = err.get("code") or ""
                message = err.get("message") or ""
                if code == "general.unauthorized":
                    return (
                        "Error: Invalid API key. "
                        "Check ~/.config/parkour/kagi_api_key or KAGI_API_KEY env var."
                    )
                if "insufficient_credit" in code or "billing" in code:
                    return (
                        "Error: Insufficient API credits. "
                        "Add funds at https://kagi.com/settings?p=billing_api"
                    )
                if message and code:
                    return f"Error: Kagi v1: {message} ({code})"
                if message:
                    return f"Error: Kagi v1: {message}"

        if status_code == 401:
            return (
                "Error: Invalid API key. "
                "Check ~/.config/parkour/kagi_api_key or KAGI_API_KEY env var."
            )
        if status_code == 402:
            return (
                "Error: Insufficient API credits. "
                "Add funds at https://kagi.com/settings?p=billing_api"
            )
        return f"Error: Kagi v1 returned HTTP {status_code}."

    if isinstance(e, httpx.RequestError):
        return f"Error: Kagi API request failed: {type(e).__name__}"

    return f"Error: {e}"


# ---------------------------------------------------------------------------
# Key handling (shared)
# ---------------------------------------------------------------------------

def get_api_key() -> str:
    """Load API key from config file or environment."""
    if key := clean_env("KAGI_API_KEY"):
        return key
    if CONFIG_PATH.exists():
        return CONFIG_PATH.read_text().strip()
    return ""


# ---------------------------------------------------------------------------
# v1 search
# ---------------------------------------------------------------------------

def _format_result_line(item: dict) -> str:
    """Format a single v1 result item as ``[title](url) - snippet (time)``.

    Snippet and time are optional and omitted cleanly when absent (image
    workflow items lack snippet, infobox items lack time).
    """
    title = item.get("title", "Untitled")
    item_url = item.get("url", "")
    snippet = item.get("snippet", "")
    published = item.get("time")
    line = f"[{title}]({item_url})"
    if snippet:
        line += f" - {snippet}"
    if published:
        line += f" ({published})"
    return line


async def search(
    query: Annotated[str, Field(
        description=(
            "Search query string. Supports operators: site:example.com "
            "(restrict to a domain), filetype:pdf (restrict to a file "
            "type), intitle:term (match in page title), inurl:term "
            '(match in URL), "exact phrase" (exact match), '
            "+term / -term (require / exclude), (A AND B) / (A OR B) "
            "(boolean grouping), * (wildcard word substitution)."
        ),
    )],
    limit: Annotated[int, Field(
        description=(
            "Maximum number of results to return. Default 5; the v1 "
            "API caps this at 1024 per page. Caps the response size "
            "only — Kagi still selects its top results internally."
        ),
        ge=1, le=1024,
    )] = 5,
    *,
    workflow: Annotated[Optional[_WorkflowType], Field(
        description=(
            "Result category. Omit (or pass null) for the default "
            "'search' workflow, which returns web results with "
            "related-query suggestions. Other workflows surface only "
            "their named primary category: 'images' returns image "
            "hits, 'videos' returns video hits, 'news' returns news "
            "articles, 'podcasts' returns podcast episodes. The "
            "frontmatter source label reflects the workflow ('kagi "
            "videos: <query>')."
        ),
    )] = None,
    lens_id: Annotated[Optional[str], Field(
        description=(
            "Kagi Lens to apply. A lens scopes the search to user-"
            "configured site, keyword, and region rules before any "
            "filters set here take effect. Accepts a built-in lens "
            "slug, a shareable lens ID (the ID portion of "
            "https://kagi.com/lenses/<id>), or the full lens URL. "
            "Lenses must be configured first at "
            "https://kagi.com/settings/lenses (and made shareable for "
            "non-built-in lenses); without that setup, no value is "
            "applicable here."
        ),
    )] = None,
    page: Annotated[Optional[int], Field(
        description=(
            "Page number, 1-indexed, in the range 1..10. Page size is "
            "controlled by 'limit': page=2 with limit=10 returns "
            "results 11..20. Omit (or pass null) for the first page."
        ),
        ge=1, le=10,
    )] = None,
    region: Annotated[Optional[str], Field(
        description=(
            "ISO 3166-1 alpha-2 country code (e.g. 'US', 'DE', 'JP') "
            "that localizes results to the named region. See "
            "https://help.kagi.com/api/regions for the supported set. "
            "Overrides any region carried by 'lens_id'."
        ),
    )] = None,
    after: Annotated[Optional[str], Field(
        description=(
            "ISO 8601 date 'YYYY-MM-DD' (e.g. '2025-01-01'). Returns "
            "only results published or updated on or after this date. "
            "Overrides any date floor carried by 'lens_id'."
        ),
    )] = None,
    before: Annotated[Optional[str], Field(
        description=(
            "ISO 8601 date 'YYYY-MM-DD' (e.g. '2025-12-31'). Returns "
            "only results published or updated on or before this date. "
            "Overrides any date ceiling carried by 'lens_id'."
        ),
    )] = None,
) -> str:
    """Search the web using Kagi's curated v1 search index."""
    api_key = get_api_key()
    if not api_key:
        return _NO_KEY_MSG

    if workflow is not None and workflow not in _VALID_WORKFLOWS:
        return (
            f"Error: Invalid workflow {workflow!r}. "
            f"Must be one of: {', '.join(sorted(_VALID_WORKFLOWS))}."
        )

    if page is not None and not 1 <= page <= 10:
        return f"Error: page must be between 1 and 10 (got {page})."

    body: dict[str, Any] = {"query": query, "limit": limit}
    if workflow is not None:
        body["workflow"] = workflow
    if lens_id is not None:
        body["lens_id"] = lens_id
    if page is not None:
        body["page"] = page

    filters: dict[str, Any] = {}
    if region:
        filters["region"] = region
    if after:
        filters["after"] = after
    if before:
        filters["before"] = before
    if filters:
        body["filters"] = filters

    headers = {
        "User-Agent": _API_USER_AGENT,
        "Accept": "application/json",
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    try:
        async with httpx.AsyncClient(timeout=_V1_TIMEOUT) as client:
            resp = await client.post(
                f"{_V1_BASE}/search",
                headers=headers,
                json=body,
            )
            resp.raise_for_status()
    except Exception as e:
        logger.exception("Error during v1 search")
        return _handle_v1_error(e)

    try:
        response = resp.json()
    except ValueError:
        return "Error: Unexpected response format from Kagi v1 search."

    data = response.get("data") or {}
    if not isinstance(data, dict):
        return "Error: Unexpected response format from Kagi v1 search."

    # v1 returns category-keyed arrays under data, not the v0 t-coded flat
    # list. The primary category depends on workflow; related_search is the
    # search-workflow companion (other workflows have their own secondary
    # arrays like video_creator/interesting_news that we skip here).
    primary_key = _WORKFLOW_RESULT_KEY.get(workflow or "search", "search")
    primary_items = data.get(primary_key, [])
    related_items = data.get("related_search", [])

    results: list[str] = [
        _format_result_line(item)
        for item in primary_items
        if isinstance(item, dict)
    ]

    related_titles = [
        r.get("title", "")
        for r in related_items
        if isinstance(r, dict) and r.get("title")
    ]

    output_parts: list[str] = []
    if results:
        output_parts.append("Results:")
        for i, result in enumerate(results, 1):
            output_parts.append(f"{i}. {result}")
    else:
        output_parts.append("No results found.")

    if related_titles:
        output_parts.append("")
        output_parts.append(f"Related searches: {', '.join(related_titles)}")

    content = "\n".join(output_parts)

    fm_entries = FMEntries({
        "source": f"kagi {workflow or 'search'}: {query}",
        "trust": _TRUST_ADVISORY,
    })

    if results:
        _append_frontmatter_entry(
            fm_entries, "hint",
            f"Drill into a result URL with {tool_name('web_fetch_sections')} "
            f"to scout layout, or {tool_name('web_fetch_direct')} for body content.",
        )
    else:
        _append_frontmatter_entry(
            fm_entries, "hint",
            'Widen the query: drop site: or filetype: qualifiers, quote '
            'phrases as "exact match", or replace +required terms with '
            "looser alternatives.",
        )

    fm = _build_frontmatter(fm_entries)
    return fm + "\n\n" + _fence_content(content)


# ---------------------------------------------------------------------------
# v0 summarize (dormant; unregistered until /summarize lands on v1)
# ---------------------------------------------------------------------------

async def summarize(
    url: Optional[str] = None,
    text: Optional[str] = None,
    summary_type: Literal["summary", "takeaway"] = "summary"
) -> str:
    """Summarize content from a URL or text using Kagi's Universal Summarizer.

    Supports web pages, PDFs, YouTube videos, audio files, and documents.
    Use this when WebFetch fails due to agent blacklisting or access restrictions.

    Args:
        url: URL to summarize (PDFs, YouTube, articles, audio)
        text: Raw text to summarize (alternative to url)
        summary_type: Output format - "summary" for prose, "takeaway" for bullet points
    """
    if _summarize_locked:
        return (
            "Error: kagi_summarize is temporarily disabled due to low API balance. "
            "Summarization requests are expensive and the remaining balance may not "
            "cover the cost. Use a kagi_search call to recheck the balance, or add "
            "funds at https://kagi.com/settings?p=billing"
        )

    client = get_client()
    if not client:
        return _NO_KEY_MSG

    if not url and not text:
        return "Error: Either 'url' or 'text' must be provided."

    if url and text:
        return "Error: Provide either 'url' or 'text', not both."

    if summary_type not in ("summary", "takeaway"):
        return "Error: summary_type must be 'summary' or 'takeaway'."

    try:
        if url:
            response = client.summarize(url=url, summary_type=summary_type, target_language="EN")
        else:
            assert text is not None  # guarded by earlier url/text validation
            response = client.summarize(text=text, summary_type=summary_type, target_language="EN")
    except Exception as e:
        logger.exception("Error during summarization")
        return _handle_v0_error(e)

    # Extract summary
    content = response.get("data", {}).get("output", "")

    if not content:
        return "Error: No summary returned from API."

    fm_entries = FMEntries({
        "source": url or "text input",
        "trust": _TRUST_ADVISORY,
    })
    balance_warning = _check_balance(response, is_summarize=True)
    if balance_warning:
        fm_entries["balance_warning"] = balance_warning

    if url:
        _append_frontmatter_entry(
            fm_entries, "hint",
            f"To recover specifics the summary discarded, pass the same "
            f"URL to {tool_name('web_fetch_direct')} with section= or "
            "search= for targeted retrieval.",
        )

    fm = _build_frontmatter(fm_entries)
    return fm + "\n\n" + _fence_content(content)
