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


def _extract_citations(html: str) -> list[dict]:
    """Extract numbered citations from MediaWiki HTML.

    Parses the main <ol class="references"> block and returns a list of
    citation dicts, 1-indexed by position:
        [{"n": 1, "text": "...", "url": "...", "title": "..."}, ...]

    url/title are present only when the citation contains an external link.

    For author-date short footnotes (e.g. "Simpson 2003, p. 8"), resolves
    the #CITEREF link to the full bibliography entry and includes it as
    "source" with its own url/title if available.
    """
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "html.parser")

    # Find the largest ol.references block (skip small note groups)
    ref_lists = soup.select("ol.references")
    if not ref_lists:
        return []

    main_ol = max(ref_lists, key=lambda ol: len(ol.select("li")))
    citations = []

    for i, li in enumerate(main_ol.select("li"), 1):
        ref_text_el = li.select_one(".reference-text")
        if not ref_text_el:
            continue

        text = ref_text_el.get_text(separator=" ", strip=True)
        # Normalize internal whitespace
        text = re.sub(r"\s+", " ", text)

        entry: dict = {"n": i, "text": text}

        # Extract first external link (the source URL)
        ext_link = ref_text_el.find("a", class_="external")
        if ext_link and ext_link.get("href"):
            entry["url"] = ext_link["href"]
            entry["title"] = ext_link.get_text(strip=True)

        # Resolve author-date shorthand via #CITEREF links
        citeref_links = ref_text_el.find_all(
            "a", href=lambda h: h and h.startswith("#CITEREF")
        )
        sources = []
        for citeref_link in citeref_links:
            target_id = citeref_link["href"].lstrip("#")
            target_el = soup.find(id=target_id)
            if not target_el:
                continue
            bib_el = target_el.parent
            bib_text = re.sub(
                r"\s+", " ", bib_el.get_text(separator=" ", strip=True)
            )
            source: dict = {"text": bib_text}
            bib_ext = bib_el.find("a", class_="external")
            if bib_ext and bib_ext.get("href"):
                source["url"] = bib_ext["href"]
                source["title"] = bib_ext.get_text(strip=True)
            sources.append(source)
        if sources:
            entry["sources"] = sources

        citations.append(entry)

    return citations


def _format_citations(citations: list[dict]) -> str:
    """Format citations as compact markdown footnote references.

    Each entry becomes:
      [^N]: [title](url)              — direct URL citation
      [^N]: text                       — plain text citation
      [^N]: text — **[title](url)**    — short footnote with resolved source
    """
    lines = []
    for c in citations:
        if "url" in c and "title" in c:
            line = f"[^{c['n']}]: [{c['title']}]({c['url']})"
        else:
            line = f"[^{c['n']}]: {c['text']}"

        # Append resolved bibliography sources for author-date shorthand
        for source in c.get("sources", []):
            if "url" in source and "title" in source:
                line += f" — **[{source['title']}]({source['url']})**"
            else:
                line += f" — *{source['text']}*"

        lines.append(line)
    return "\n".join(lines)


def _mediawiki_html_to_markdown(html: str) -> str:
    """Convert MediaWiki HTML to clean markdown.

    Removes TOC, scripts, and styles; cleans headings before conversion.
    """
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "html.parser")

    # Remove MediaWiki noise elements
    for selector in ["#toc", ".toc", "script", "style", ".mw-editsection"]:
        for el in soup.select(selector):
            el.decompose()

    # Convert inline citation markers [1], [2] → [^1], [^2] (markdown footnotes).
    # Skip non-numeric refs like [nb 1] (nota bene / notes group).
    for sup in soup.select("sup.reference"):
        text = sup.get_text(strip=True)
        m = re.match(r"\[(\d+)\]", text)
        if m:
            sup.replace_with(f"[^{m.group(1)}]")
        else:
            sup.decompose()

    # Remove the expanded footnote block — citations are available via the
    # citation parameter. Selector is stable (used by Wikipedia's PCS).
    for el in soup.select(".mw-references-wrap"):
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
