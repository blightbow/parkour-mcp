"""Live integration tests that hit real endpoints.

Skipped by default. Run with:
    uv run pytest tests/test_live.py -v
    uv run pytest -m live -v
"""

import pytest

from parkour_mcp.fetch_direct import web_fetch_direct
from parkour_mcp.mediawiki import (
    _detect_mediawiki,
    _fetch_mediawiki_page,
    _mediawiki_html_to_markdown,
)
from parkour_mcp.markdown import _extract_sections_from_markdown

from ._output import (
    assert_fenced,
    fenced_heading,
    fenced_line,
    split_output,
)

pytestmark = pytest.mark.live

WIKI_URL = "https://wiki.ultimacodex.com/wiki/Ultima_VIII_books"


# --- MediaWiki detection ---

class TestLiveMediawikiDetection:
    @pytest.mark.asyncio
    async def test_detects_ultimacodex_wiki(self):
        result = await _detect_mediawiki(WIKI_URL)
        assert result is not None
        assert result["api_base"] == "https://wiki.ultimacodex.com/api.php"
        assert result["page_title"] == "Ultima_VIII_books"
        assert result["page_length"] > 0
        assert result["sitename"] == "Ultima Codex"
        assert "MediaWiki" in result["generator"]

    @pytest.mark.asyncio
    async def test_non_wiki_url_returns_none_fast(self):
        result = await _detect_mediawiki("https://httpbin.org/html")
        assert result is None


# --- MediaWiki page fetch ---

class TestLiveMediawikiPageFetch:
    @pytest.mark.asyncio
    async def test_full_page_fetch(self):
        info = await _detect_mediawiki(WIKI_URL)
        assert info is not None

        page = await _fetch_mediawiki_page(info["api_base"], info["page_title"])
        assert page is not None
        assert page["title"]
        assert len(page["html"]) > 1000
        assert len(page["sections_meta"]) > 10



# --- MediaWiki HTML → markdown ---

class TestLiveMediawikiMarkdown:
    @pytest.mark.asyncio
    async def test_full_page_to_markdown(self):
        info = await _detect_mediawiki(WIKI_URL)
        assert info is not None
        page = await _fetch_mediawiki_page(info["api_base"], info["page_title"])
        assert page is not None
        md = _mediawiki_html_to_markdown(page["html"])

        assert len(md) > 1000
        assert "\n\n\n" not in md  # no triple newlines

        sections = _extract_sections_from_markdown(md)
        names = [s["name"] for s in sections]
        assert "Honor Lost" in names
        assert "The Spell of Divination" in names


# --- web_fetch_direct ---

class TestLiveWebFetchDirect:
    @pytest.mark.asyncio
    async def test_wiki_full_page_truncated(self):
        result = await web_fetch_direct(WIKI_URL, max_tokens=200)
        fm, fence = split_output(result)

        # Frontmatter: trusted server-generated metadata only.
        assert "site: Ultima Codex" in fm
        assert "generator: MediaWiki" in fm
        assert "truncated:" in fm

        # Security invariant: attacker-controlled page title must not
        # leak into the trusted frontmatter zone.
        assert "Ultima VIII" not in fm
        assert "title:" not in fm

        # Fenced content: title as heading, sections list, body text.
        assert_fenced(result)
        assert fenced_heading(1, "Ultima VIII books") in fence
        assert fenced_line("Sections:") in fence
        assert "Honor Lost" in fence

    @pytest.mark.asyncio
    async def test_wiki_single_section(self):
        result = await web_fetch_direct(WIKI_URL, section="Honor Lost", max_tokens=500)
        _fm, fence = split_output(result)
        assert_fenced(result)
        # Section rendered as a markdown heading inside the fence.
        assert fenced_heading(4, "Honor Lost") in fence
        assert "Meltzars" in fence

    @pytest.mark.asyncio
    async def test_wiki_multiple_sections(self):
        result = await web_fetch_direct(
            WIKI_URL,
            section=["Honor Lost", "The Spell of Divination"],
            max_tokens=2000,
        )
        _fm, fence = split_output(result)
        assert_fenced(result)
        assert fenced_heading(4, "Honor Lost") in fence
        assert fenced_heading(5, "The Spell of Divination") in fence

    @pytest.mark.asyncio
    async def test_json_endpoint(self):
        result = await web_fetch_direct("https://httpbin.org/json")
        assert "content_type: json" in result
        assert "slideshow" in result

    @pytest.mark.asyncio
    async def test_html_endpoint_markdown_default(self):
        result = await web_fetch_direct("https://httpbin.org/html")
        _fm, fence = split_output(result)
        assert_fenced(result)
        # Title rendered as a heading inside the fence.
        assert fenced_heading(1, "Herman Melville - Moby-Dick") in fence
        assert "<document" not in result  # not XML

    @pytest.mark.asyncio
    async def test_404_returns_error(self):
        result = await web_fetch_direct("https://httpbin.org/status/404")
        assert "Error:" in result
        assert "404" in result


# --- requires_js (headless-browser render) ---

class TestLiveRequiresJs:
    @pytest.mark.asyncio
    async def test_wiki_full_page_via_api(self):
        """MediaWiki fast path should return content without launching browser."""
        result = await web_fetch_direct(WIKI_URL, requires_js=True, max_tokens=200)
        fm, fence = split_output(result)

        assert "site: Ultima Codex" in fm
        assert "generator: MediaWiki" in fm
        assert "truncated:" in fm

        assert_fenced(result)
        assert fenced_heading(1, "Ultima VIII books") in fence
        assert fenced_line("Sections:") in fence
        assert "Honor Lost" in fence

    @pytest.mark.asyncio
    async def test_wiki_section_fetch_via_api(self):
        result = await web_fetch_direct(
            WIKI_URL, section="Honor Lost", requires_js=True, max_tokens=1000
        )
        _fm, fence = split_output(result)
        assert_fenced(result)
        assert fenced_heading(4, "Honor Lost") in fence
        assert "Meltzars" in fence
        # Should NOT contain browser: key (fast path skips browser)
        assert "browser:" not in result

    @pytest.mark.asyncio
    async def test_wiki_multiple_sections_via_api(self):
        result = await web_fetch_direct(
            WIKI_URL,
            section=["Honor Lost", "The Spell of Divination"],
            requires_js=True,
            max_tokens=2000,
        )
        _fm, fence = split_output(result)
        assert_fenced(result)
        assert fenced_heading(4, "Honor Lost") in fence
        assert fenced_heading(5, "The Spell of Divination") in fence

    @pytest.mark.asyncio
    async def test_non_wiki_uses_browser(self):
        """Non-wiki URL should fall through to the browser path."""
        result = await web_fetch_direct(
            "https://httpbin.org/html", requires_js=True, max_tokens=500
        )
        assert "browser:" in result
        assert "generator: MediaWiki" not in result

    @pytest.mark.asyncio
    async def test_premature_requires_js_emits_tip(self):
        """Mechanism #3: cold requires_js with no JS-shell evidence fires the tip."""
        result = await web_fetch_direct(
            "https://httpbin.org/html", requires_js=True, max_tokens=500
        )
        assert "tip:" in result
        assert "detects JavaScript-shell pages" in result


class TestLiveAkamaiHttp2:
    """HTTP/2 + browser-coherent headers clear Akamai Bot Manager 403s."""

    # Akamai-fronted PDF on Whirlpool's CDN.  Over HTTP/1.1 with a bare UA it
    # 403s; HTTP/2 plus the Sec-Fetch/Client-Hint headers clear it.  Asserted at
    # the guarded_fetch layer because PDF *content* support is a separate
    # content-type concern — here we only prove the transport-level block is gone.
    AKAMAI_PDF = (
        "https://www.maytag.com/content/dam/global/documents/"
        "201801/techsheet-w10828193-revd.pdf"
    )

    @pytest.mark.asyncio
    async def test_guarded_fetch_clears_akamai_403(self):
        from parkour_mcp.common import guarded_fetch

        resp = await guarded_fetch(self.AKAMAI_PDF, max_bytes=None)
        assert resp.status_code == 200
        assert resp.http_version == "HTTP/2"
        assert resp.content[:5] == b"%PDF-"
