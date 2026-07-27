"""Tests for parkour_mcp.fetch_js — the requires_js headless-render path.

Reached via web_fetch_direct(..., requires_js=True) / actions=[...]. Browser-
path tests are excluded because they require a real Playwright browser.
Covers: MediaWiki fast path under requires_js, search/slices, content-type
pre-check.
"""

import httpx
import pytest
import respx

from parkour_mcp._pipeline import _page_cache, _wiki_cache
from parkour_mcp.fetch_direct import web_fetch_direct

from ._output import (
    fenced_heading,
    split_output,
)
from .conftest import (
    MEDIAWIKI_PARSE_FULL_RESPONSE,
    MEDIAWIKI_QUERY_RESPONSE,
)


@pytest.fixture(autouse=True)
def clear_caches():
    """Ensure each test starts with empty caches."""
    yield
    _wiki_cache.clear()
    _page_cache.clear()


class TestRequiresJsMediawikiFastPath:
    """requires_js must not pre-empt the API-backed fast paths."""

    @pytest.mark.asyncio
    @respx.mock
    async def test_wiki_full_page(self):
        respx.get("https://wiki.example.com/api.php").mock(
            side_effect=[
                httpx.Response(200, json=MEDIAWIKI_QUERY_RESPONSE),
                httpx.Response(200, json=MEDIAWIKI_PARSE_FULL_RESPONSE),
            ]
        )

        result = await web_fetch_direct(
            "https://wiki.example.com/wiki/Test_Page", requires_js=True
        )
        fm, fence = split_output(result)
        # Security invariant: page title lives in the fence, not the frontmatter.
        assert "Test Page" not in fm
        assert fenced_heading(1, "Test Page") in fence
        assert "site: Test Wiki" in fm
        assert "generator: MediaWiki" in fm
        assert "Section One" in fence

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
        result = await web_fetch_direct(
            "https://wiki.example.com/wiki/Test_Page", requires_js=True, max_tokens=5
        )
        fm, _fence = split_output(result)
        assert "truncated:" in fm

    @pytest.mark.asyncio
    @respx.mock
    async def test_wiki_section_fetch(self):
        """Section filtering now uses full page fetch + local filtering."""
        respx.get("https://wiki.example.com/api.php").mock(
            side_effect=[
                httpx.Response(200, json=MEDIAWIKI_QUERY_RESPONSE),
                httpx.Response(200, json=MEDIAWIKI_PARSE_FULL_RESPONSE),
            ]
        )

        result = await web_fetch_direct(
            "https://wiki.example.com/wiki/Test_Page",
            section="Section Two",
            requires_js=True,
        )
        _fm, fence = split_output(result)
        assert fenced_heading(2, "Section Two") in fence
        assert "Content of section two" in fence

    @pytest.mark.asyncio
    @respx.mock
    async def test_wiki_section_fetch_list(self):
        respx.get("https://wiki.example.com/api.php").mock(
            side_effect=[
                httpx.Response(200, json=MEDIAWIKI_QUERY_RESPONSE),
                httpx.Response(200, json=MEDIAWIKI_PARSE_FULL_RESPONSE),
            ]
        )

        result = await web_fetch_direct(
            "https://wiki.example.com/wiki/Test_Page",
            section=["Section One", "Section Two"],
            requires_js=True,
        )
        _fm, fence = split_output(result)
        # Multi-section content appears inside the fence
        assert fenced_heading(2, "Section One") in fence
        assert "Content of section one" in fence
        assert "Content of section two" in fence

    @pytest.mark.asyncio
    async def test_non_wiki_url_no_mw_metadata(self):
        """Non-wiki URLs should not produce MediaWiki-specific frontmatter.

        This exercises the full pipeline (browser or error) but verifies that
        the MW fast path was not taken.
        """
        result = await web_fetch_direct("https://example.com/page", requires_js=True)
        assert "generator: MediaWiki" not in result

    @pytest.mark.asyncio
    @respx.mock
    async def test_wiki_api_failure_falls_to_browser(self):
        """If MW API fails, should fall through to the browser path."""
        respx.get("https://wiki.example.com/api.php").mock(
            side_effect=httpx.ConnectError("fail")
        )
        respx.get("https://wiki.example.com/w/api.php").mock(
            side_effect=httpx.ConnectError("fail")
        )

        result = await web_fetch_direct(
            "https://wiki.example.com/wiki/Test_Page", requires_js=True
        )
        # Should get a browser error (no Playwright mock), not a crash
        assert "Error:" in result

    @pytest.mark.asyncio
    @respx.mock
    async def test_section_string_normalized_to_list(self):
        """section='Foo' should behave identically to section=['Foo']."""
        respx.get("https://wiki.example.com/api.php").mock(
            side_effect=[
                httpx.Response(200, json=MEDIAWIKI_QUERY_RESPONSE),
                httpx.Response(200, json=MEDIAWIKI_PARSE_FULL_RESPONSE),
            ]
        )

        result = await web_fetch_direct(
            "https://wiki.example.com/wiki/Test_Page",
            section="Section Two",
            requires_js=True,
        )
        _fm, fence = split_output(result)
        assert fenced_heading(2, "Section Two") in fence


class TestRequiresJsSearchSlices:
    """Tests for search/slices parameters via MediaWiki fast path."""

    @pytest.mark.asyncio
    @respx.mock
    async def test_wiki_search_returns_slices(self):
        """search= should populate cache via MW fast path and return slice results."""
        respx.get("https://wiki.example.com/api.php").mock(
            side_effect=[
                httpx.Response(200, json=MEDIAWIKI_QUERY_RESPONSE),
                httpx.Response(200, json=MEDIAWIKI_PARSE_FULL_RESPONSE),
            ]
        )

        result = await web_fetch_direct(
            "https://wiki.example.com/wiki/Test_Page",
            search="section",
            requires_js=True,
        )
        assert "search:" in result
        assert "total_slices:" in result

    @pytest.mark.asyncio
    @respx.mock
    async def test_wiki_slices_returns_specific(self):
        """slices=[0] should return the first slice from cached content."""
        respx.get("https://wiki.example.com/api.php").mock(
            side_effect=[
                httpx.Response(200, json=MEDIAWIKI_QUERY_RESPONSE),
                httpx.Response(200, json=MEDIAWIKI_PARSE_FULL_RESPONSE),
            ]
        )

        result = await web_fetch_direct(
            "https://wiki.example.com/wiki/Test_Page",
            slices=[0],
            requires_js=True,
        )
        assert "total_slices:" in result
        assert "--- slice 0" in result

    @pytest.mark.asyncio
    async def test_search_and_slices_mutually_exclusive(self):
        result = await web_fetch_direct(
            "https://example.com/page",
            search="foo",
            slices=[0],
            requires_js=True,
        )
        assert "Error:" in result
        assert "mutually exclusive" in result

    @pytest.mark.asyncio
    async def test_search_and_section_mutually_exclusive(self):
        result = await web_fetch_direct(
            "https://example.com/page",
            search="foo",
            section="Bar",
            requires_js=True,
        )
        assert "Error:" in result
        assert "mutually exclusive" in result

    @pytest.mark.asyncio
    @respx.mock
    async def test_cache_first_path(self):
        """Second slicing call should use cache without re-fetching."""
        respx.get("https://wiki.example.com/api.php").mock(
            side_effect=[
                httpx.Response(200, json=MEDIAWIKI_QUERY_RESPONSE),
                httpx.Response(200, json=MEDIAWIKI_PARSE_FULL_RESPONSE),
            ]
        )

        # First call populates cache
        await web_fetch_direct(
            "https://wiki.example.com/wiki/Test_Page",
            search="section",
            requires_js=True,
        )

        # Second call should hit cache (no more mocked responses needed)
        result = await web_fetch_direct(
            "https://wiki.example.com/wiki/Test_Page",
            slices=[0],
            requires_js=True,
        )
        assert "--- slice 0" in result


class TestRequiresJsContentTypePrecheck:
    """Content-type HEAD pre-check in _render_js that skips the browser."""

    @pytest.mark.asyncio
    @respx.mock
    async def test_json_url_skips_browser(self):
        """JSON content-type should bypass the browser and return directly."""
        respx.head("https://api.example.com/data.json").mock(
            return_value=httpx.Response(200, headers={"content-type": "application/json"})
        )
        respx.get("https://api.example.com/data.json").mock(
            return_value=httpx.Response(200, text='{"key": "value"}',
                                       headers={"content-type": "application/json"})
        )

        result = await web_fetch_direct(
            "https://api.example.com/data.json", requires_js=True
        )
        assert "content_type: json" in result
        assert "JavaScript rendering was skipped" in result
        assert '"key": "value"' in result

    @pytest.mark.asyncio
    @respx.mock
    async def test_plain_text_url_skips_browser(self):
        """Plain text content-type should bypass the browser."""
        respx.head("https://example.com/file.txt").mock(
            return_value=httpx.Response(200, headers={"content-type": "text/plain"})
        )
        respx.get("https://example.com/file.txt").mock(
            return_value=httpx.Response(200, text="Hello world",
                                       headers={"content-type": "text/plain"})
        )

        result = await web_fetch_direct(
            "https://example.com/file.txt", requires_js=True
        )
        assert "content_type: plain text" in result
        assert "JavaScript rendering was skipped" in result
        assert "Hello world" in result

    @pytest.mark.asyncio
    @respx.mock
    async def test_xml_url_skips_browser(self):
        """XML content-type should bypass the browser."""
        respx.head("https://example.com/feed.xml").mock(
            return_value=httpx.Response(200, headers={"content-type": "application/xml"})
        )
        respx.get("https://example.com/feed.xml").mock(
            return_value=httpx.Response(200, text="<root><item>test</item></root>",
                                       headers={"content-type": "application/xml"})
        )

        result = await web_fetch_direct(
            "https://example.com/feed.xml", requires_js=True
        )
        assert "content_type: xml" in result
        assert "JavaScript rendering was skipped" in result

    @pytest.mark.asyncio
    @respx.mock
    async def test_head_failure_falls_through(self):
        """If the HEAD request fails, should fall through to the browser path."""
        respx.head("https://example.com/page").mock(
            side_effect=httpx.ConnectError("fail")
        )

        result = await web_fetch_direct("https://example.com/page", requires_js=True)
        # Should NOT have the pre-check warning — fell through to the browser
        assert "JavaScript rendering was skipped" not in result

    @pytest.mark.asyncio
    @respx.mock
    async def test_actions_bypass_precheck(self):
        """When actions are provided, the HEAD pre-check should be skipped."""
        respx.head("https://api.example.com/data.json").mock(
            return_value=httpx.Response(200, headers={"content-type": "application/json"})
        )

        result = await web_fetch_direct(
            "https://api.example.com/data.json",
            actions=[{"action": "click", "selector": "button"}],
        )
        # Should NOT have the pre-check warning — actions bypass the pre-check
        assert "JavaScript rendering was skipped" not in result
