"""Live integration tests that hit real endpoints.

Skipped by default. Run with:
    uv run pytest tests/test_live.py -v
    uv run pytest -m live -v
"""

import pytest

from claude_web_tools.fetch_direct import web_fetch_direct
from claude_web_tools.fetch_js import web_fetch_js
from claude_web_tools.mediawiki import (
    _detect_mediawiki,
    _fetch_mediawiki_page,
    _mediawiki_html_to_markdown,
)
from claude_web_tools.markdown import _extract_sections_from_markdown

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

    @pytest.mark.asyncio
    async def test_section_fetch_by_name(self):
        info = await _detect_mediawiki(WIKI_URL)
        assert info is not None

        page = await _fetch_mediawiki_page(
            info["api_base"], info["page_title"], sections=["Honor Lost"]
        )
        assert page is not None
        assert len(page["html"]) > 0

    @pytest.mark.asyncio
    async def test_multiple_section_fetch(self):
        info = await _detect_mediawiki(WIKI_URL)
        assert info is not None

        page = await _fetch_mediawiki_page(
            info["api_base"],
            info["page_title"],
            sections=["Honor Lost", "The Spell of Divination"],
        )
        assert page is not None
        assert len(page["html"]) > 0


# --- MediaWiki HTML → markdown ---

class TestLiveMediawikiMarkdown:
    @pytest.mark.asyncio
    async def test_full_page_to_markdown(self):
        info = await _detect_mediawiki(WIKI_URL)
        page = await _fetch_mediawiki_page(info["api_base"], info["page_title"])
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
        assert result.startswith("---")
        assert "title:" in result
        assert "site: Ultima Codex" in result
        assert "generator: MediaWiki" in result
        assert "truncated:" in result
        assert "sections:" in result
        assert "Honor Lost" in result
        assert "[content truncated]" in result

    @pytest.mark.asyncio
    async def test_wiki_single_section(self):
        result = await web_fetch_direct(WIKI_URL, section="Honor Lost", max_tokens=500)
        assert "section: Honor Lost" in result
        assert "Meltzars" in result

    @pytest.mark.asyncio
    async def test_wiki_multiple_sections(self):
        result = await web_fetch_direct(
            WIKI_URL,
            section=["Honor Lost", "The Spell of Divination"],
            max_tokens=500,
        )
        assert "sections:" in result
        assert "Honor Lost" in result
        assert "The Spell of Divination" in result

    @pytest.mark.asyncio
    async def test_wiki_cite_true_returns_xml(self):
        result = await web_fetch_direct(WIKI_URL, cite=True, max_tokens=200)
        assert "<document" in result
        assert "<span" in result

    @pytest.mark.asyncio
    async def test_json_endpoint(self):
        result = await web_fetch_direct("https://httpbin.org/json")
        assert "content_type: json" in result
        assert "slideshow" in result

    @pytest.mark.asyncio
    async def test_html_endpoint_markdown_default(self):
        result = await web_fetch_direct("https://httpbin.org/html")
        assert result.startswith("---")
        assert "title:" in result
        assert "Herman Melville" in result
        assert "<document" not in result  # not XML

    @pytest.mark.asyncio
    async def test_html_endpoint_cite_true(self):
        result = await web_fetch_direct("https://httpbin.org/html", cite=True)
        assert "<document" in result
        assert "Herman Melville" in result

    @pytest.mark.asyncio
    async def test_404_returns_error(self):
        result = await web_fetch_direct("https://httpbin.org/status/404")
        assert "Error:" in result
        assert "404" in result


# --- web_fetch_js ---

class TestLiveWebFetchJs:
    @pytest.mark.asyncio
    async def test_wiki_full_page_via_api(self):
        """MediaWiki fast path should return content without launching browser."""
        result = await web_fetch_js(WIKI_URL, max_tokens=200)
        assert "title:" in result
        assert "site: Ultima Codex" in result
        assert "generator: MediaWiki" in result
        assert "truncated:" in result
        assert "sections:" in result
        assert "Honor Lost" in result

    @pytest.mark.asyncio
    async def test_wiki_section_fetch_via_api(self):
        result = await web_fetch_js(WIKI_URL, section="Honor Lost", max_tokens=1000)
        assert "section: Honor Lost" in result
        assert "Meltzars" in result
        # Should NOT contain browser: key (fast path skips browser)
        assert "browser:" not in result

    @pytest.mark.asyncio
    async def test_wiki_multiple_sections_via_api(self):
        result = await web_fetch_js(
            WIKI_URL,
            section=["Honor Lost", "The Spell of Divination"],
            max_tokens=1000,
        )
        assert "sections:" in result
        assert "Honor Lost" in result

    @pytest.mark.asyncio
    async def test_non_wiki_uses_browser(self):
        """Non-wiki URL should fall through to browser path."""
        result = await web_fetch_js("https://httpbin.org/html", max_tokens=500)
        assert "browser:" in result
        assert "generator: MediaWiki" not in result
