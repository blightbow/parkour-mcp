"""Tests for claude_web_tools.fetch_js module — MediaWiki fast path only.

Browser-path tests are excluded because they require a real Playwright browser.
"""

import httpx
import pytest
import respx

from claude_web_tools.fetch_js import web_fetch_js

from .conftest import (
    MEDIAWIKI_QUERY_RESPONSE,
    MEDIAWIKI_PARSE_FULL_RESPONSE,
    MEDIAWIKI_PARSE_SECTIONS_RESPONSE,
    MEDIAWIKI_PARSE_SECTION_TEXT,
)


class TestWebFetchJsMediawikiFastPath:
    @pytest.mark.asyncio
    @respx.mock
    async def test_wiki_full_page(self):
        respx.get("https://wiki.example.com/api.php").mock(
            side_effect=[
                httpx.Response(200, json=MEDIAWIKI_QUERY_RESPONSE),
                httpx.Response(200, json=MEDIAWIKI_PARSE_FULL_RESPONSE),
            ]
        )

        result = await web_fetch_js("https://wiki.example.com/wiki/Test_Page")
        assert "title: Test Page" in result
        assert "site: Test Wiki" in result
        assert "generator: MediaWiki" in result
        assert "Section One" in result

    @pytest.mark.asyncio
    @respx.mock
    async def test_wiki_full_page_truncation_shows_sections(self):
        respx.get("https://wiki.example.com/api.php").mock(
            side_effect=[
                httpx.Response(200, json=MEDIAWIKI_QUERY_RESPONSE),
                httpx.Response(200, json=MEDIAWIKI_PARSE_FULL_RESPONSE),
            ]
        )

        # Very low token limit to force truncation
        result = await web_fetch_js(
            "https://wiki.example.com/wiki/Test_Page", max_tokens=5
        )
        assert "truncated:" in result
        assert "[content truncated]" in result

    @pytest.mark.asyncio
    @respx.mock
    async def test_wiki_section_fetch(self):
        respx.get("https://wiki.example.com/api.php").mock(
            side_effect=[
                # Detection probe
                httpx.Response(200, json=MEDIAWIKI_QUERY_RESPONSE),
                # Section list
                httpx.Response(200, json=MEDIAWIKI_PARSE_SECTIONS_RESPONSE),
                # Section content
                httpx.Response(200, json=MEDIAWIKI_PARSE_SECTION_TEXT),
            ]
        )

        result = await web_fetch_js(
            "https://wiki.example.com/wiki/Test_Page",
            section="Section Two",
        )
        assert "section: Section Two" in result
        assert "Content of section two" in result

    @pytest.mark.asyncio
    @respx.mock
    async def test_wiki_section_fetch_list(self):
        section_one_text = {
            "parse": {"text": {"*": "<h2>Section One</h2><p>Content one.</p>"}}
        }
        respx.get("https://wiki.example.com/api.php").mock(
            side_effect=[
                httpx.Response(200, json=MEDIAWIKI_QUERY_RESPONSE),
                httpx.Response(200, json=MEDIAWIKI_PARSE_SECTIONS_RESPONSE),
                httpx.Response(200, json=section_one_text),
                httpx.Response(200, json=MEDIAWIKI_PARSE_SECTION_TEXT),
            ]
        )

        result = await web_fetch_js(
            "https://wiki.example.com/wiki/Test_Page",
            section=["Section One", "Section Two"],
        )
        assert "sections:" in result
        assert "Content one" in result
        assert "Content of section two" in result

    @pytest.mark.asyncio
    async def test_non_wiki_url_no_mw_metadata(self):
        """Non-wiki URLs should not produce MediaWiki-specific frontmatter.

        This exercises the full pipeline (browser or error) but verifies that
        the MW fast path was not taken.
        """
        result = await web_fetch_js("https://example.com/page")
        assert "generator: MediaWiki" not in result

    @pytest.mark.asyncio
    @respx.mock
    async def test_wiki_api_failure_falls_to_browser(self):
        """If MW API fails, should fall through to browser path."""
        respx.get("https://wiki.example.com/api.php").mock(
            side_effect=httpx.ConnectError("fail")
        )
        respx.get("https://wiki.example.com/w/api.php").mock(
            side_effect=httpx.ConnectError("fail")
        )

        result = await web_fetch_js("https://wiki.example.com/wiki/Test_Page")
        # Should get a browser error (no Playwright mock), not a crash
        assert "Error:" in result

    @pytest.mark.asyncio
    @respx.mock
    async def test_section_string_normalized_to_list(self):
        """section='Foo' should behave identically to section=['Foo']."""
        respx.get("https://wiki.example.com/api.php").mock(
            side_effect=[
                httpx.Response(200, json=MEDIAWIKI_QUERY_RESPONSE),
                httpx.Response(200, json=MEDIAWIKI_PARSE_SECTIONS_RESPONSE),
                httpx.Response(200, json=MEDIAWIKI_PARSE_SECTION_TEXT),
            ]
        )

        result = await web_fetch_js(
            "https://wiki.example.com/wiki/Test_Page",
            section="Section Two",
        )
        assert "section: Section Two" in result
