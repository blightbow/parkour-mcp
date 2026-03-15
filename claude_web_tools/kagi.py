"""Kagi search and summarization tools."""

import logging
import os
from pathlib import Path
from typing import Optional

from kagiapi import KagiClient

logger = logging.getLogger(__name__)

CONFIG_PATH = Path.home() / ".config" / "kagi" / "api_key"

_NO_KEY_MSG = "Error: API key not found. Create ~/.config/kagi/api_key or set KAGI_API_KEY env var."

_LOW_BALANCE_THRESHOLD = 1.00  # dollars

# In-memory state: set True when any Kagi response shows balance < threshold.
# Locks out summarize (expensive) until a non-summarize call sees balance recovered.
_summarize_locked = False


def _extract_balance(response: dict) -> Optional[float]:
    """Extract api_balance from a Kagi API response, or None if absent."""
    meta = response.get("meta", {})
    balance = meta.get("api_balance")
    if balance is not None:
        try:
            return float(balance)
        except (TypeError, ValueError):
            pass
    return None


def _check_balance(response: dict, is_summarize: bool = False) -> Optional[str]:
    """Check balance from response, update lockout state, return warning or None.

    Non-summarize calls clear the lockout if balance has recovered.
    """
    global _summarize_locked
    balance = _extract_balance(response)
    if balance is None:
        return None

    if balance < _LOW_BALANCE_THRESHOLD:
        _summarize_locked = True
        return (
            f"<!-- warning: Kagi API balance low: ${balance:.2f} remaining. "
            f"Add funds at https://kagi.com/settings?p=billing -->\n"
        )
    else:
        # Balance is healthy — clear lockout (only non-summarize calls reach here
        # when locked, since summarize is blocked before the API call)
        if not is_summarize:
            _summarize_locked = False
        return None


def _handle_kagi_error(e: Exception) -> str:
    """Format a Kagi API exception into a user-facing error string."""
    error_msg = str(e)
    if "401" in error_msg or "Unauthorized" in error_msg:
        return "Error: Invalid API key. Check ~/.config/kagi/api_key or KAGI_API_KEY env var."
    if "402" in error_msg:
        return "Error: Insufficient API credits. Add funds at https://kagi.com/settings?p=billing"
    return f"Error: {error_msg}"


def get_api_key() -> str:
    """Load API key from config file or environment."""
    # Environment variable takes precedence
    if key := os.environ.get("KAGI_API_KEY"):
        return key
    # Fall back to config file
    if CONFIG_PATH.exists():
        return CONFIG_PATH.read_text().strip()
    return ""


def get_client() -> Optional[KagiClient]:
    """Create a Kagi client with the configured API key."""
    api_key = get_api_key()
    if not api_key:
        return None
    return KagiClient(api_key=api_key)


async def search(query: str, limit: int = 5) -> str:
    """Search the web using Kagi's curated search index.

    Use this as an alternative to the built-in WebSearch tool when WebSearch
    returns few or poor quality results. Kagi's index is independently curated,
    resistant to SEO spam, and may surface different sources. Returns raw search
    results with snippets and timestamps, plus related search suggestions.

    Args:
        query: The search query
        limit: Maximum number of results to return (default 5)
    """
    client = get_client()
    if not client:
        return _NO_KEY_MSG

    try:
        response = client.search(query, limit=limit)
    except Exception as e:
        logger.exception("Error during search")
        return _handle_kagi_error(e)

    # Parse results
    results = []
    related_searches = []

    for item in response.get("data", []):
        item_type = item.get("t")

        if item_type == 0:  # Search result
            title = item.get("title", "Untitled")
            item_url = item.get("url", "")
            snippet = item.get("snippet", "")
            published = item.get("published")

            # Format as markdown
            if published:
                results.append(f"[{title}]({item_url}) - {snippet} ({published})")
            else:
                results.append(f"[{title}]({item_url}) - {snippet}")

        elif item_type == 1:  # Related searches
            related_searches = item.get("list", [])

    # Build output
    output_parts = []

    if results:
        output_parts.append("Results:")
        for i, result in enumerate(results, 1):
            output_parts.append(f"{i}. {result}")
    else:
        output_parts.append("No results found.")

    if related_searches:
        output_parts.append("")
        output_parts.append(f"Related searches: {', '.join(related_searches)}")

    output = "\n".join(output_parts)

    warning = _check_balance(response, is_summarize=False)
    if warning:
        output = warning + output

    return output


async def summarize(
    url: Optional[str] = None,
    text: Optional[str] = None,
    summary_type: str = "summary"
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
            response = client.summarize(text=text, summary_type=summary_type, target_language="EN")
    except Exception as e:
        logger.exception("Error during summarization")
        return _handle_kagi_error(e)

    # Extract summary
    output = response.get("data", {}).get("output", "")

    if not output:
        return "Error: No summary returned from API."

    warning = _check_balance(response, is_summarize=True)
    if warning:
        output = warning + output

    return output
