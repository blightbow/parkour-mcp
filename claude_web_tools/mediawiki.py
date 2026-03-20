"""MediaWiki detection and API-based page fetching."""

import logging
import html as html_mod
import re
import urllib.parse
from typing import Optional

import httpx

from .common import _API_HEADERS
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
                    headers=_API_HEADERS,
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
) -> Optional[dict]:
    """Fetch a full MediaWiki page via the API.

    Always fetches the complete page; section filtering is handled downstream.
    Returns {title, html, sections_meta} or None.
    """
    async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
        resp = await client.get(
            api_base,
            params={
                "action": "parse",
                "page": page_title,
                "format": "json",
                "prop": "text|displaytitle|sections",
            },
            headers=_API_HEADERS,
        )
        resp.raise_for_status()
        data = resp.json()
        parse = data.get("parse", {})

        return {
            "title": _clean_display_title(parse.get("displaytitle", page_title)),
            "html": parse.get("text", {}).get("*", ""),
            "sections_meta": parse.get("sections", []),
        }


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

    # Remove citation/reference noise:
    #   - sup.reference: inline markers like [1], [2]
    #   - .mw-references-wrap: the footnote block at the end of sections
    # These selectors are stable — used by Wikipedia's own Page Content
    # Service (PCS) to identify reference sections in mobile rendering.
    for selector in ["sup.reference", ".mw-references-wrap"]:
        for el in soup.select(selector):
            el.decompose()

    # Remove Cite error paragraphs (MediaWiki rendering artefact when
    # <ref group=…> tags lack a matching {{reflist}} in section scope)
    for p in soup.find_all("p"):
        if p.get_text(strip=True).startswith("Cite error:"):
            p.decompose()

    # Clean heading markup (removes .mw-editsection, unwraps inline tags)
    _clean_headings(soup)

    markdown = md(str(soup), heading_style="ATX")
    # Collapse triple+ newlines
    markdown = re.sub(r'\n{3,}', '\n\n', markdown).strip()
    return markdown
