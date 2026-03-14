"""MediaWiki detection and API-based page fetching."""

import asyncio
import logging
import html as html_mod
import re
import urllib.parse
from typing import Optional

import httpx

from .common import _FETCH_HEADERS
from .markdown import md, _normalize_whitespace, _clean_headings

logger = logging.getLogger(__name__)

_MEDIAWIKI_API_PATHS = ["/api.php", "/w/api.php"]


async def _detect_mediawiki(url: str) -> Optional[dict]:
    """Detect if a URL points to a MediaWiki page and return API metadata.

    Gate: only probes if '/wiki/' is in the URL path.

    Returns {api_base, page_title, page_length, sitename, generator} or None.
    """
    parsed = urllib.parse.urlparse(url)

    if "/wiki/" not in parsed.path:
        return None

    # Extract page title from path after /wiki/
    wiki_idx = parsed.path.index("/wiki/")
    page_title = urllib.parse.unquote(parsed.path[wiki_idx + 6:]).strip("/")
    if not page_title:
        return None

    base_url = f"{parsed.scheme}://{parsed.netloc}"

    async with httpx.AsyncClient(timeout=10.0, follow_redirects=True) as client:
        for api_path in _MEDIAWIKI_API_PATHS:
            api_base = base_url + api_path
            try:
                resp = await client.get(
                    api_base,
                    params={
                        "action": "query",
                        "meta": "siteinfo",
                        "titles": page_title,
                        "prop": "info",
                        "format": "json",
                    },
                    headers=_FETCH_HEADERS,
                )
                resp.raise_for_status()
                data = resp.json()

                # Validate response structure
                query = data.get("query", {})
                pages = query.get("pages", {})
                siteinfo = query.get("general", {})

                # Check that we got a valid page (not a missing page with id=-1)
                page_data = None
                for _pid, pdata in pages.items():
                    if "missing" not in pdata:
                        page_data = pdata
                        break

                if page_data is None:
                    continue

                return {
                    "api_base": api_base,
                    "page_title": page_title,
                    "page_length": page_data.get("length", 0),
                    "sitename": siteinfo.get("sitename", ""),
                    "generator": siteinfo.get("generator", ""),
                }

            except Exception:
                continue

    return None


def _clean_display_title(raw: str) -> str:
    """Clean a MediaWiki displaytitle: strip HTML tags, decode entities, normalize whitespace."""
    text = re.sub(r'<[^>]+>', '', raw)
    text = html_mod.unescape(text)
    text = _normalize_whitespace(text).strip()
    return text


async def _fetch_mediawiki_page(
    api_base: str,
    page_title: str,
    sections: Optional[list[str]] = None,
) -> Optional[dict]:
    """Fetch a MediaWiki page via the API.

    If sections is provided, fetches only those sections by index (concurrently).
    Returns {title, html, sections_meta} or None.
    """
    async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
        if sections:
            # First get section list to map names to indices
            resp = await client.get(
                api_base,
                params={
                    "action": "parse",
                    "page": page_title,
                    "format": "json",
                    "prop": "sections|displaytitle",
                },
                headers=_FETCH_HEADERS,
            )
            resp.raise_for_status()
            data = resp.json()
            parse = data.get("parse", {})
            section_list = parse.get("sections", [])

            # Map requested names to section indices
            # Strip HTML tags and normalize whitespace (API returns e.g.
            # "<i>Honor Lost</i>" and "Vol.&nbsp;II")
            name_to_index = {}
            for sec in section_list:
                raw_name = sec.get("line", "")
                clean_name = _clean_display_title(raw_name)
                name_to_index[clean_name] = sec.get("index", "")

            # Resolve indices for requested sections
            fetch_tasks = []
            for sec_name in sections:
                idx = name_to_index.get(_normalize_whitespace(sec_name))
                if idx is not None:
                    fetch_tasks.append(_fetch_section(client, api_base, page_title, idx))

            # Fetch all sections concurrently
            html_parts = await asyncio.gather(*fetch_tasks)

            return {
                "title": _clean_display_title(parse.get("displaytitle", page_title)),
                "html": "\n".join(h for h in html_parts if h),
                "sections_meta": section_list,
            }
        else:
            # Full page fetch
            resp = await client.get(
                api_base,
                params={
                    "action": "parse",
                    "page": page_title,
                    "format": "json",
                    "prop": "text|displaytitle|sections",
                },
                headers=_FETCH_HEADERS,
            )
            resp.raise_for_status()
            data = resp.json()
            parse = data.get("parse", {})

            return {
                "title": _clean_display_title(parse.get("displaytitle", page_title)),
                "html": parse.get("text", {}).get("*", ""),
                "sections_meta": parse.get("sections", []),
            }


async def _fetch_section(
    client: httpx.AsyncClient,
    api_base: str,
    page_title: str,
    section_index: str,
) -> str:
    """Fetch a single section's HTML from the MediaWiki API."""
    resp = await client.get(
        api_base,
        params={
            "action": "parse",
            "page": page_title,
            "format": "json",
            "prop": "text",
            "section": section_index,
        },
        headers=_FETCH_HEADERS,
    )
    resp.raise_for_status()
    sec_data = resp.json()
    return sec_data.get("parse", {}).get("text", {}).get("*", "")


def _mediawiki_html_to_markdown(html: str) -> str:
    """Convert MediaWiki HTML to clean markdown.

    Removes TOC, scripts, and styles; cleans headings before conversion.
    """
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "html.parser")

    # Remove MediaWiki noise elements
    for selector in ["#toc", ".toc", "script", "style"]:
        for el in soup.select(selector):
            el.decompose()

    # Clean heading markup (removes .mw-editsection, unwraps inline tags)
    _clean_headings(soup)

    markdown = md(str(soup), heading_style="ATX")
    # Collapse triple+ newlines
    markdown = re.sub(r'\n{3,}', '\n\n', markdown).strip()
    return markdown
