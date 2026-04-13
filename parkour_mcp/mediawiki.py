"""MediaWiki detection, API-based page fetching, and dedicated tool."""

import logging
import html as html_mod
import re
import urllib.parse
from typing import Annotated, Optional, Union

import httpx
from pydantic import Field

from .common import _API_HEADERS, RateLimiter, tool_name
from .markdown import (
    md,
    _normalize_whitespace,
    _clean_headings,
    _build_frontmatter,
    _fence_content,
    _TRUST_ADVISORY,
)

logger = logging.getLogger(__name__)

_MEDIAWIKI_API_PATHS = ["/api.php", "/w/api.php"]

# Shared rate limiter for all MediaWiki API calls — backs the
# `_fetch_mediawiki_page` and `_search_mediawiki` helpers below.
# Back-fills a pre-existing gap: mediawiki.py had no client-side limiter
# before this commit.  1.0s matches the discipline used by the IETF
# Datatracker integration.
_mediawiki_limiter = RateLimiter(1.0)

# Sister Wikimedia projects that take language-prefix-free base hosts.
# Agents can pass `wiki="commons"` and get commons.wikimedia.org; for
# language-keyed projects like Wikipedia itself, use the language code
# directly (e.g. `wiki="en"` → en.wikipedia.org).
_WIKIMEDIA_ALIASES: dict[str, str] = {
    "commons":  "commons.wikimedia.org",
    "wikidata": "www.wikidata.org",
    "meta":     "meta.wikimedia.org",
    "species":  "species.wikimedia.org",
}

# Language code pattern for Wikipedia fast-path resolution.
# Catches en, de, zh, simple, zh-yue, pt-br, etc. — the common cases that
# should not require a probe round-trip to resolve the API base.
_WIKI_LANG_CODE_RE = re.compile(r"^[a-z]{2,3}(-[a-z]+)?$")

# Distinguishes URL-shaped title parameters from bare page titles.
_URL_SCHEME_RE = re.compile(r"^https?://", re.IGNORECASE)


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
    await _mediawiki_limiter.wait()
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


def _resolve_citeref_target(soup, target_id: str) -> Optional[dict]:
    """Resolve a #CITEREF target id to its bibliography entry.

    Looks up the element with the given id, walks to its parent (the
    bibliography <cite>/<li>/<dd> that carries the surrounding text), and
    extracts the plain text plus the first external link found inside.

    Returns None if the target is not present or has no usable parent.
    Returns {"text": str, "url"?: str, "title"?: str} otherwise.
    """
    target_el = soup.find(id=target_id)
    if not target_el:
        return None
    bib_el = target_el.parent
    if not bib_el:
        return None
    bib_text = re.sub(r"\s+", " ", bib_el.get_text(separator=" ", strip=True))
    entry: dict = {"text": bib_text}
    bib_ext = bib_el.find("a", class_="external")
    if bib_ext and bib_ext.get("href"):
        entry["url"] = bib_ext["href"]
        entry["title"] = bib_ext.get_text(strip=True)
    return entry


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
            "a", href=lambda h: h and h.startswith("#CITEREF")  # type: ignore[reportArgumentType]
        )
        sources = []
        for citeref_link in citeref_links:
            target_id = citeref_link["href"].lstrip("#")
            resolved = _resolve_citeref_target(soup, target_id)
            if resolved is not None:
                sources.append(resolved)
        if sources:
            entry["sources"] = sources

        citations.append(entry)

    return citations


def _extract_inline_citations(html: str) -> list[dict]:
    """Extract in-prose author-date citations from MediaWiki HTML.

    Walks every ``<a href="#CITEREF...">`` anchor that appears outside the
    numbered references block (``.mw-references-wrap``), dedupes by target
    id (first-encounter wins), and resolves each to its bibliography
    entry.  Returns a list of:

        {
            "key":       "CITEREFFranzén2005",      # raw anchor id
            "href":      "#CITEREFFranzén2005",     # exact fragment as it
                                                    # appears in the markdown
                                                    # link, for verbatim lookup
            "shorthand": "Franzén (2005)",          # visible link text
            "text":      "<full bibliography entry>",
            "url":       "https://example.com/...", # optional external link
            "title":     "Book Title",              # optional
        }

    Inline CITEREFs are intentionally tracked separately from the
    numbered-footnote ``sources`` field produced by ``_extract_citations``:
    the two mechanisms share resolution logic but serve different
    retrieval modes.  Inline refs are the natural provenance of author-date
    shortcuts embedded directly in prose (``Franzén (2005)``), which the
    markdown pass preserves verbatim as ``[Franzén (2005)](#CITEREFFranzén2005)``.
    """
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "html.parser")

    # Skip anchors inside the numbered footnote block — those already feed
    # _extract_citations' sources field and would otherwise be
    # double-counted here.
    for el in soup.select(".mw-references-wrap"):
        el.decompose()

    seen: set[str] = set()
    entries: list[dict] = []

    for a in soup.find_all(
        "a", href=lambda h: h and h.startswith("#CITEREF")  # type: ignore[reportArgumentType]
    ):
        href_attr = a["href"]
        # BeautifulSoup returns a list for multi-valued attrs; href is
        # single-valued in valid HTML but ty flags the union.
        href = href_attr if isinstance(href_attr, str) else href_attr[0]
        target_id = href.lstrip("#")
        if target_id in seen:
            continue
        seen.add(target_id)

        shorthand = re.sub(r"\s+", " ", a.get_text(separator=" ", strip=True))
        resolved = _resolve_citeref_target(soup, target_id)
        if resolved is None:
            # Anchor points at a bibliography entry we can't find — skip,
            # since the whole point of lookup is to return the full entry.
            continue

        entry: dict = {
            "key": target_id,
            "href": href,
            "shorthand": shorthand,
            "text": resolved["text"],
        }
        if "url" in resolved:
            entry["url"] = resolved["url"]
        if "title" in resolved:
            entry["title"] = resolved["title"]
        entries.append(entry)

    return entries


# Matches the native markdown form that _mediawiki_html_to_markdown leaves
# for inline author-date shortcuts: [visible text](#CITEREFFoo2005).
# Used by the fast path to count + sample keys for the JIT advisory.
_INLINE_CITEREF_MD_RE = re.compile(r"\[([^\]]+)\]\((#CITEREF[^)\s]+)\)")


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


def _format_inline_citations(citations: list[dict]) -> str:
    """Format inline author-date citations as a compact bibliography block.

    Each entry becomes:

        [Franzén (2005)](#CITEREFFranzén2005)
        : Franzén, Torkel (2005). Gödel's Theorem: An Incomplete Guide...
        : **[Book Title](https://example.com/book)**

    The first line reproduces the same markdown link shape that appears
    in the page body, so the output ties directly back to the inline
    reference the caller is looking up.
    """
    lines: list[str] = []
    for c in citations:
        lines.append(f"[{c['shorthand']}]({c['href']})")
        lines.append(f": {c['text']}")
        if "url" in c and "title" in c:
            lines.append(f": **[{c['title']}]({c['url']})**")
        lines.append("")
    return "\n".join(lines).rstrip()


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

    # Remove navigation templates and sister-project boxes — these are
    # link-farm grids (e.g. {{Integers}}, {{WWII}}) that produce massive
    # walls of links with no prose content.  navbox alone can be 10-37%
    # of the total HTML.  Sister-project boxes ("Wikiquote has...",
    # "Look up ... in Wiktionary") add minor noise to search results.
    for selector in [".navbox", ".navbox-styles", ".sistersitebox"]:
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

    # Replace math elements with LaTeX source.  MediaWiki renders <math> as
    # MathML + an <img> fallback.  markdownify converts both, producing noise
    # like "{\displaystyle x^2} [Image: {\displaystyle x^2}]".  Extract the
    # TeX annotation and discard the rest.
    for math_el in soup.find_all("math"):
        annotation = math_el.find("annotation", encoding="application/x-tex")
        if annotation and annotation.string:
            latex = annotation.string.strip()
            math_el.replace_with(f" ${latex}$ ")
        else:
            # No annotation — use alt text from the companion fallback image
            alt = math_el.get("alttext", "")
            math_el.replace_with(f" ${alt}$ " if alt else "")
    # Remove orphaned math fallback images (class="mwe-math-fallback-image-*")
    for img in soup.find_all("img", class_=re.compile(r"mwe-math-fallback")):
        img.decompose()

    # Clean heading markup (removes .mw-editsection, unwraps inline tags)
    _clean_headings(soup)

    markdown = md(str(soup), heading_style="ATX")
    # Collapse triple+ newlines
    markdown = re.sub(r'\n{3,}', '\n\n', markdown).strip()
    return markdown


# ---------------------------------------------------------------------------
# Dedicated MediaWiki tool
# ---------------------------------------------------------------------------
# The fast path in ``_pipeline.py`` handles URL-in, content-out requests
# routed through fetch_direct/fetch_js.  The tool below is the action-
# dispatch surface for deeper investigation — title-based fetch, native
# wiki search, and unified references (footnotes + inline citations)
# lookup.
#
# Parameter naming: this tool uses ``title`` (page identifier) and
# ``query`` (search terms) rather than the codebase-standard overloaded
# ``query``.  See ``docs/query-parameter-overload.md`` for the rationale.


def _canonicalize_title_for_cache(title: str) -> str:
    """Normalize a page title to match MediaWiki's canonical URL form.

    Replaces spaces with underscores and upper-cases the leading
    character (MediaWiki forces sentence-case on the first letter of
    article-namespace titles).  This catches the 90% case where a caller
    passes ``"new york city"`` and a later URL-keyed fetch of
    ``https://en.wikipedia.org/wiki/New_York_City`` should hit the same
    cache entry.
    """
    title = title.strip().replace(" ", "_")
    if title and title[0].islower():
        title = title[0].upper() + title[1:]
    return title


async def _resolve_wiki_base(wiki: str) -> tuple[str, str]:
    """Resolve a ``wiki`` parameter to ``(host, api_base)``.

    Accepts:
    - Language code (``"en"``) → ``en.wikipedia.org`` + ``/w/api.php``
    - Sister-project alias (``"commons"``) → mapped host + ``/w/api.php``
    - Hostname (``"en.wikipedia.org"``) → used directly, assumes ``/w/api.php``
    - Full URL (``"https://wiki.archlinux.org"``) → scheme stripped, probed

    For Wikipedia language editions and Wikimedia sister projects the
    API path ``/w/api.php`` is assumed (Wikimedia convention).  Unknown
    hosts are probed via ``_detect_mediawiki`` so bare installs using
    ``/api.php`` are also supported.

    Raises ``ValueError`` when the wiki cannot be resolved.
    """
    wiki = wiki.strip()
    if not wiki:
        raise ValueError("wiki parameter cannot be empty")

    # Fast path: Wikipedia language code like "en", "de", "zh-yue".
    if _WIKI_LANG_CODE_RE.fullmatch(wiki):
        host = f"{wiki}.wikipedia.org"
        return host, f"https://{host}/w/api.php"

    # Sister-project alias (commons, wikidata, etc.)
    if wiki in _WIKIMEDIA_ALIASES:
        host = _WIKIMEDIA_ALIASES[wiki]
        return host, f"https://{host}/w/api.php"

    # Strip scheme if the caller passed a full URL.
    if "://" in wiki:
        parsed = urllib.parse.urlparse(wiki)
        if not parsed.netloc:
            raise ValueError(f"Could not parse wiki URL: {wiki!r}")
        host = parsed.netloc
    else:
        host = wiki

    # Short-circuit for Wikimedia-hosted sites: they always use
    # ``/w/api.php``.  This avoids an unnecessary probe round-trip for
    # callers that pass a bare hostname like ``"en.wikipedia.org"`` or
    # ``"commons.wikimedia.org"`` instead of a language code.
    if (
        host == "wikipedia.org"
        or host == "wikimedia.org"
        or host.endswith(".wikipedia.org")
        or host.endswith(".wikimedia.org")
    ):
        return host, f"https://{host}/w/api.php"

    # Probe unknown hosts via ``_detect_mediawiki`` using a synthesized
    # URL to a well-known page.  This handles bare MediaWiki installs
    # that put their API at ``/api.php`` rather than ``/w/api.php``.
    probe_url = f"https://{host}/wiki/Main_Page"
    info = await _detect_mediawiki(probe_url)
    if info is None:
        raise ValueError(
            f"Could not resolve wiki {wiki!r} — no MediaWiki API found "
            f"on {host}"
        )
    return host, info["api_base"]


def _normalize_citeref_key(k: str) -> str:
    """Normalize a user-supplied CITEREF key to the bare anchor id form.

    Accepts ``"#CITEREFFoo2005"``, ``"CITEREFFoo2005"``, or bare
    ``"Foo2005"``; always returns ``"CITEREFFoo2005"``.
    """
    k = k.lstrip("#")
    if not k.startswith("CITEREF"):
        k = "CITEREF" + k
    return k


async def _search_mediawiki(
    api_base: str,
    query: str,
    limit: int,
    offset: int,
    namespace: int = 0,
) -> tuple[list[dict], int]:
    """Call MediaWiki ``action=query&list=search`` and return results.

    Each result dict contains title, pageid, size, wordcount, timestamp,
    and a normalized snippet where ``<span class="searchmatch">term</span>``
    is converted to ``**term**`` (markdown bold) before other HTML tags
    are stripped.
    """
    await _mediawiki_limiter.wait()
    params = {
        "action": "query",
        "list": "search",
        "srsearch": query,
        "srlimit": min(max(int(limit), 1), 50),
        "sroffset": max(int(offset), 0),
        "srnamespace": int(namespace),
        "srprop": "snippet|size|wordcount|timestamp",
        "format": "json",
    }
    async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as client:
        resp = await client.get(api_base, params=params, headers=_API_HEADERS)
        resp.raise_for_status()
        data = resp.json()

    query_data = data.get("query", {})
    searchinfo = query_data.get("searchinfo", {})
    total = int(searchinfo.get("totalhits", 0))
    raw_results = query_data.get("search", [])

    results: list[dict] = []
    for r in raw_results:
        snippet_html = r.get("snippet", "")
        # Preserve searchmatch highlights as bold before stripping tags.
        snippet = re.sub(
            r'<span class="searchmatch">([^<]*)</span>',
            r"**\1**",
            snippet_html,
        )
        snippet = re.sub(r"<[^>]+>", "", snippet)
        snippet = html_mod.unescape(snippet)
        snippet = _normalize_whitespace(snippet).strip()

        results.append({
            "title": r.get("title", ""),
            "pageid": r.get("pageid", 0),
            "size": r.get("size", 0),
            "wordcount": r.get("wordcount", 0),
            "timestamp": r.get("timestamp", ""),
            "snippet": snippet,
        })
    return results, total


def _format_mediawiki_search(
    results: list[dict],
    total: int,
    offset: int,
    query: str,
    host: str,
) -> str:
    """Format search results as a markdown numbered list."""
    if not results:
        return f"No results for **{query}** on {host}."

    lines: list[str] = [
        f"# Search results for **{query}**",
        f"Showing {offset + 1}–{offset + len(results)} of {total:,} on {host}.",
        "",
    ]
    for i, r in enumerate(results, start=offset + 1):
        title_slug = r["title"].replace(" ", "_")
        title_url = f"https://{host}/wiki/{urllib.parse.quote(title_slug, safe='_')}"
        lines.append(
            f"{i}. **[{r['title']}]({title_url})** · {r['wordcount']:,} words"
        )
        if r.get("snippet"):
            lines.append(f"   {r['snippet']}")
        lines.append("")
    return "\n".join(lines).rstrip()


async def _handle_page(
    title: str,
    wiki: str,
    section: Optional[list[str]],
    search: Optional[str],
    slices: Optional[Union[int, list[int]]],
    max_tokens: int,
) -> str:
    """Fetch a MediaWiki page by title (or URL) and return formatted content.

    Delegates to ``web_fetch_direct`` so the full fast-path machinery
    (caching, slicing, section filtering, fragment handling) is reused
    verbatim.  Title-to-URL synthesis is the only MediaWiki-specific
    work the handler does itself.
    """
    # Function-scope import to avoid fetch_direct ↔ mediawiki cycle.
    from .fetch_direct import web_fetch_direct

    if _URL_SCHEME_RE.match(title):
        url = title
    else:
        try:
            host, _api_base = await _resolve_wiki_base(wiki)
        except ValueError as e:
            return f"Error: {e}"
        canonical = _canonicalize_title_for_cache(title)
        url = f"https://{host}/wiki/{urllib.parse.quote(canonical, safe='_')}"

    return await web_fetch_direct(
        url,
        max_tokens=max_tokens,
        section=section,
        search=search,
        slices=slices,
    )


async def _handle_search(
    query: str,
    wiki: str,
    limit: int,
    offset: int,
    namespace: int,
) -> str:
    """Execute native MediaWiki full-text search and format the results."""
    try:
        host, api_base = await _resolve_wiki_base(wiki)
    except ValueError as e:
        return f"Error: {e}"

    try:
        results, total = await _search_mediawiki(
            api_base, query, limit, offset, namespace,
        )
    except Exception as e:
        logger.exception("MediaWiki search failed")
        return f"Error: Search request failed: {e}"

    body = _format_mediawiki_search(results, total, offset, query, host)
    fm_entries: dict = {
        "api": f"MediaWiki ({host})",
        "action": "search",
        "query": query,
        "total_results": total,
    }
    if namespace != 0:
        fm_entries["namespace"] = namespace
    if total > 0 and results:
        fm_entries["hint"] = (
            f"Use {tool_name('mediawiki')} action='page' title='<title>' "
            f"to retrieve full content for any result"
        )
    fm = _build_frontmatter(fm_entries)
    return fm + "\n\n" + _fence_content(body)


async def _handle_references(
    title: str,
    wiki: str,
    footnotes: Optional[list[int]],
    citations: Optional[list[str]],
    max_tokens: int,  # noqa: ARG001 — reserved for future truncation gating
) -> str:
    """Resolve footnotes and/or inline citations for a MediaWiki page.

    Returns both blocks in a single fence when both parameters are
    supplied, using the ``references_only: true`` umbrella flag in
    addition to the existing ``footnotes_only``/``citations_only``
    markers (downstream code may key off either).
    """
    # Function-scope import to avoid _pipeline ↔ mediawiki cycle.
    from ._pipeline import _cached_mediawiki_fetch

    if footnotes is None and citations is None:
        return (
            "Error: 'references' action requires footnotes= or citations= "
            "(or both). Use footnotes=[1,2] for numbered footnote lookup, "
            "or citations=['#CITEREFFoo2005'] for inline author-date "
            "CITEREF lookup."
        )

    if _URL_SCHEME_RE.match(title):
        url = title
    else:
        try:
            host, _api_base = await _resolve_wiki_base(wiki)
        except ValueError as e:
            return f"Error: {e}"
        canonical = _canonicalize_title_for_cache(title)
        url = f"https://{host}/wiki/{urllib.parse.quote(canonical, safe='_')}"

    try:
        wiki_info, wiki_page = await _cached_mediawiki_fetch(url)
    except Exception as e:
        logger.exception("MediaWiki references fetch failed")
        return f"Error: Could not fetch {url}: {e}"
    if not wiki_info or not wiki_page:
        return (
            f"Error: 'references' requires a MediaWiki page — {url} is not "
            "recognized as one."
        )

    html = wiki_page["html"]
    page_title = wiki_page["title"]

    body_blocks: list[str] = []
    fm_entries: dict = {
        "source": url,
        "trust": _TRUST_ADVISORY,
    }

    if footnotes is not None:
        all_footnotes = _extract_citations(html)
        if not all_footnotes:
            return f"Error: No numbered footnotes found on {url}"
        requested_fn = set(footnotes)
        selected_fn = [c for c in all_footnotes if c["n"] in requested_fn]
        not_found_fn = sorted(requested_fn - {c["n"] for c in selected_fn})
        if selected_fn:
            body_blocks.append(
                "## Footnotes\n\n" + _format_citations(selected_fn)
            )
        if not_found_fn:
            available = sorted(c["n"] for c in all_footnotes)
            fm_entries["footnotes_not_found"] = not_found_fn
            fm_entries["footnotes_available"] = f"1-{available[-1]}"
        fm_entries["footnotes_only"] = True

    if citations is not None:
        all_inline = _extract_inline_citations(html)
        if not all_inline:
            return f"Error: No inline citations found on {url}"
        by_key = {c["key"]: c for c in all_inline}
        selected_ic: list[dict] = []
        not_found_ic: list[str] = []
        for original in citations:
            norm = _normalize_citeref_key(original)
            match = by_key.get(norm)
            if match is None:
                not_found_ic.append(original)
            else:
                selected_ic.append(match)
        if selected_ic:
            body_blocks.append(
                "## Inline citations\n\n"
                + _format_inline_citations(selected_ic)
            )
        if not_found_ic:
            fm_entries["citations_not_found"] = not_found_ic
            fm_entries["citations_available_count"] = str(len(all_inline))
        fm_entries["citations_only"] = True

    # Umbrella flag only when both modes were actually requested.  The
    # existing ``footnotes_only``/``citations_only`` markers stay for
    # single-mode requests so downstream consumers don't need to learn a
    # new flag.
    if footnotes is not None and citations is not None:
        fm_entries["references_only"] = True

    fm = _build_frontmatter(fm_entries)
    body = "\n\n".join(body_blocks) if body_blocks else ""
    return fm + "\n\n" + _fence_content(body, title=page_title)


async def mediawiki(
    action: Annotated[str, Field(
        description=(
            "The operation to perform. "
            "page: fetch a Wikipedia/MediaWiki article by title or URL. "
            "search: native full-text search across articles. "
            "references: resolve numbered footnotes and/or inline "
            "author-date citations on a specific article."
        ),
    )],
    title: Annotated[Optional[str], Field(
        description=(
            "Page identifier — article title "
            "(e.g. \"Gödel's incompleteness theorems\") or full URL. "
            "Required for 'page' and 'references' actions. "
            "When a full URL is supplied, the wiki= parameter is ignored."
        ),
    )] = None,
    query: Annotated[Optional[str], Field(
        description=(
            "Search terms — required for the 'search' action. "
            "Supports MediaWiki's native search operators."
        ),
    )] = None,
    wiki: Annotated[str, Field(
        description=(
            "Wiki instance: language code (\"en\", \"de\", \"simple\", "
            "\"zh-yue\"), sister-project alias (\"commons\", \"wikidata\", "
            "\"meta\", \"species\"), hostname "
            "(\"en.wikipedia.org\"), or full URL "
            "(\"https://wiki.archlinux.org\"). Default \"en\" (English "
            "Wikipedia). Ignored when title= is a full URL."
        ),
    )] = "en",
    section: Annotated[Optional[Union[str, list[str]]], Field(
        description=(
            "Section name or list of section names to extract "
            "(page action only). Matches heading text."
        ),
    )] = None,
    search: Annotated[Optional[str], Field(
        description=(
            "Within-page BM25 keyword search for the 'page' action — "
            "distinct from action='search' which does full-text wiki "
            "search across all articles."
        ),
    )] = None,
    slices: Annotated[Optional[Union[int, list[int]]], Field(
        description=(
            "Slice index or list of indices to retrieve from a cached "
            "page (page action only)."
        ),
    )] = None,
    footnotes: Annotated[Optional[Union[int, list[int]]], Field(
        description=(
            "Numbered footnote(s) to retrieve for the 'references' "
            "action. Accepts an int or list of ints matching the [^N] "
            "markers in rendered page content."
        ),
    )] = None,
    citations: Annotated[Optional[Union[str, list[str]]], Field(
        description=(
            "Inline author-date CITEREF key(s) to resolve for the "
            "'references' action. Accepts '#CITEREFFoo2005', "
            "'CITEREFFoo2005', or bare 'Foo2005'."
        ),
    )] = None,
    limit: Annotated[int, Field(
        description="Maximum search results to return (default 10, max 50).",
    )] = 10,
    offset: Annotated[int, Field(
        description="Starting position for search pagination.",
    )] = 0,
    namespace: Annotated[int, Field(
        description=(
            "MediaWiki namespace for search: 0=Article (default), 1=Talk, "
            "4=Project (Wikipedia:), 14=Category, 100=Portal."
        ),
    )] = 0,
    max_tokens: Annotated[int, Field(
        description="Limit on content length in approximate token count (default 5000).",
    )] = 5000,
) -> str:
    """Search and retrieve MediaWiki (Wikipedia, etc.) content via the API."""
    action = action.strip().lower()

    # Normalize the polymorphic section parameter to a list (or None).
    section_names: Optional[list[str]] = None
    if section is not None:
        section_names = [section] if isinstance(section, str) else list(section)

    if action == "page":
        # Check the "wrong parameter supplied" case first — it's the more
        # actionable error when the caller mistakenly used query= for a
        # page lookup.
        if query is not None:
            return (
                "Error: 'page' action uses title=, not query=. "
                "For full-text search across articles, use "
                "action='search' query='...'."
            )
        if not title:
            return (
                "Error: 'page' action requires the title parameter. "
                "Example: action='page' title=\"Gödel's incompleteness theorems\""
            )
        return await _handle_page(
            title, wiki, section_names, search, slices, max_tokens,
        )

    if action == "search":
        if title is not None:
            return (
                "Error: 'search' action uses query=, not title=. "
                "For fetching a specific page, use action='page' title='...'."
            )
        if not query:
            return (
                "Error: 'search' action requires the query parameter. "
                "Example: action='search' query='quantum entanglement'"
            )
        return await _handle_search(query, wiki, limit, offset, namespace)

    if action == "references":
        if query is not None:
            return (
                "Error: 'references' action uses title=, not query=. "
                "query= is only valid for the 'search' action."
            )
        if not title:
            return (
                "Error: 'references' action requires the title parameter "
                "identifying the page to look up. "
                "Example: action='references' title=\"Gödel's incompleteness theorems\" "
                "footnotes=[1,2]"
            )
        fn_list: Optional[list[int]] = None
        if footnotes is not None:
            fn_list = [footnotes] if isinstance(footnotes, int) else list(footnotes)
        cit_list: Optional[list[str]] = None
        if citations is not None:
            cit_list = [citations] if isinstance(citations, str) else list(citations)
        return await _handle_references(
            title, wiki, fn_list, cit_list, max_tokens,
        )

    return (
        f"Error: Unknown action {action!r}. "
        "Valid actions: page, search, references."
    )
