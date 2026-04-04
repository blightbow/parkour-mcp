"""Tests for kagi_research_mcp.fetch_js module.

Browser-path tests are excluded because they require a real Playwright browser.
Covers: MediaWiki fast path, search/slices, footnotes, content-type pre-check.
"""

import httpx
import pytest
import respx

from kagi_research_mcp.fetch_js import web_fetch_js
from kagi_research_mcp._pipeline import _wiki_cache, _page_cache

from .conftest import (
    MEDIAWIKI_QUERY_RESPONSE,
    MEDIAWIKI_PARSE_FULL_RESPONSE,
    MEDIAWIKI_PARSE_WITH_CITATIONS,
)


@pytest.fixture(autouse=True)
def clear_caches():
    """Ensure each test starts with empty caches."""
    yield
    _wiki_cache.clear()
    _page_cache.clear()


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
        assert "│ # Test Page" in result
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

        result = await web_fetch_js(
            "https://wiki.example.com/wiki/Test_Page",
            section="Section Two",
        )
        assert "│ ## Section Two" in result
        assert "Content of section two" in result

    @pytest.mark.asyncio
    @respx.mock
    async def test_wiki_section_fetch_list(self):
        respx.get("https://wiki.example.com/api.php").mock(
            side_effect=[
                httpx.Response(200, json=MEDIAWIKI_QUERY_RESPONSE),
                httpx.Response(200, json=MEDIAWIKI_PARSE_FULL_RESPONSE),
            ]
        )

        result = await web_fetch_js(
            "https://wiki.example.com/wiki/Test_Page",
            section=["Section One", "Section Two"],
        )
        # Multi-section content appears inside the fence
        assert "│ ## Section One" in result
        assert "Content of section one" in result
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
                httpx.Response(200, json=MEDIAWIKI_PARSE_FULL_RESPONSE),
            ]
        )

        result = await web_fetch_js(
            "https://wiki.example.com/wiki/Test_Page",
            section="Section Two",
        )
        assert "│ ## Section Two" in result


class TestWebFetchJsSearchSlices:
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

        result = await web_fetch_js(
            "https://wiki.example.com/wiki/Test_Page",
            search="section",
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

        result = await web_fetch_js(
            "https://wiki.example.com/wiki/Test_Page",
            slices=[0],
        )
        assert "total_slices:" in result
        assert "--- slice 0" in result

    @pytest.mark.asyncio
    async def test_search_and_slices_mutually_exclusive(self):
        result = await web_fetch_js(
            "https://example.com/page",
            search="foo",
            slices=[0],
        )
        assert "Error:" in result
        assert "mutually exclusive" in result

    @pytest.mark.asyncio
    async def test_search_and_section_mutually_exclusive(self):
        result = await web_fetch_js(
            "https://example.com/page",
            search="foo",
            section="Bar",
        )
        assert "Error:" in result
        assert "mutually exclusive" in result

    @pytest.mark.asyncio
    @respx.mock
    async def test_footnotes_with_section_warns(self):
        """section + footnotes should honor section and warn about footnotes."""
        respx.get("https://wiki.example.com/api.php").mock(
            side_effect=[
                httpx.Response(200, json=MEDIAWIKI_QUERY_RESPONSE),
                httpx.Response(200, json=MEDIAWIKI_PARSE_FULL_RESPONSE),
            ]
        )

        result = await web_fetch_js(
            "https://wiki.example.com/wiki/Test_Page",
            section="Section Two",
            footnotes=[1, 2],
        )
        assert "footnotes parameter ignored" in result
        assert "Content of section two" in result

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
        await web_fetch_js(
            "https://wiki.example.com/wiki/Test_Page",
            search="section",
        )

        # Second call should hit cache (no more mocked responses needed)
        result = await web_fetch_js(
            "https://wiki.example.com/wiki/Test_Page",
            slices=[0],
        )
        assert "--- slice 0" in result


class TestWebFetchJsFootnotes:
    """Tests for footnote retrieval via MediaWiki fast path."""

    @pytest.mark.asyncio
    @respx.mock
    async def test_footnotes_returns_citations(self):
        """footnotes= should return formatted citation entries."""
        respx.get("https://wiki.example.com/api.php").mock(
            side_effect=[
                httpx.Response(200, json=MEDIAWIKI_QUERY_RESPONSE),
                httpx.Response(200, json=MEDIAWIKI_PARSE_WITH_CITATIONS),
            ]
        )

        result = await web_fetch_js(
            "https://wiki.example.com/wiki/Test_Page",
            footnotes=[1, 2],
        )
        assert "footnotes_only: True" in result
        assert "First reference source" in result
        assert "Second reference source" in result

    @pytest.mark.asyncio
    @respx.mock
    async def test_footnotes_not_found_shows_available(self):
        """Requesting nonexistent footnotes should show available range."""
        respx.get("https://wiki.example.com/api.php").mock(
            side_effect=[
                httpx.Response(200, json=MEDIAWIKI_QUERY_RESPONSE),
                httpx.Response(200, json=MEDIAWIKI_PARSE_WITH_CITATIONS),
            ]
        )

        result = await web_fetch_js(
            "https://wiki.example.com/wiki/Test_Page",
            footnotes=[99],
        )
        assert "footnotes_not_found" in result
        assert "footnotes_available" in result

    @pytest.mark.asyncio
    async def test_footnotes_non_wiki_returns_error(self):
        """Non-wiki URL should return error about MediaWiki requirement."""
        result = await web_fetch_js(
            "https://example.com/page",
            footnotes=[1],
        )
        assert "Error:" in result
        assert "MediaWiki" in result


class TestWebFetchJsContentTypePrecheck:
    """Tests for content-type HEAD pre-check that skips Playwright."""

    @pytest.mark.asyncio
    @respx.mock
    async def test_json_url_skips_browser(self):
        """JSON content-type should bypass Playwright and return directly."""
        respx.head("https://api.example.com/data.json").mock(
            return_value=httpx.Response(200, headers={"content-type": "application/json"})
        )
        respx.get("https://api.example.com/data.json").mock(
            return_value=httpx.Response(200, text='{"key": "value"}',
                                       headers={"content-type": "application/json"})
        )

        result = await web_fetch_js("https://api.example.com/data.json")
        assert "content_type: json" in result
        assert "JavaScript rendering was skipped" in result
        assert '"key": "value"' in result

    @pytest.mark.asyncio
    @respx.mock
    async def test_plain_text_url_skips_browser(self):
        """Plain text content-type should bypass Playwright."""
        respx.head("https://example.com/file.txt").mock(
            return_value=httpx.Response(200, headers={"content-type": "text/plain"})
        )
        respx.get("https://example.com/file.txt").mock(
            return_value=httpx.Response(200, text="Hello world",
                                       headers={"content-type": "text/plain"})
        )

        result = await web_fetch_js("https://example.com/file.txt")
        assert "content_type: plain text" in result
        assert "JavaScript rendering was skipped" in result
        assert "Hello world" in result

    @pytest.mark.asyncio
    @respx.mock
    async def test_xml_url_skips_browser(self):
        """XML content-type should bypass Playwright."""
        respx.head("https://example.com/feed.xml").mock(
            return_value=httpx.Response(200, headers={"content-type": "application/xml"})
        )
        respx.get("https://example.com/feed.xml").mock(
            return_value=httpx.Response(200, text="<root><item>test</item></root>",
                                       headers={"content-type": "application/xml"})
        )

        result = await web_fetch_js("https://example.com/feed.xml")
        assert "content_type: xml" in result
        assert "JavaScript rendering was skipped" in result

    @pytest.mark.asyncio
    @respx.mock
    async def test_head_failure_falls_through(self):
        """If HEAD request fails, should fall through to browser path."""
        respx.head("https://example.com/page").mock(
            side_effect=httpx.ConnectError("fail")
        )

        result = await web_fetch_js("https://example.com/page")
        # Should NOT have the pre-check warning — fell through to browser
        assert "JavaScript rendering was skipped" not in result

    @pytest.mark.asyncio
    @respx.mock
    async def test_actions_bypass_precheck(self):
        """When actions are provided, HEAD pre-check should be skipped."""
        respx.head("https://api.example.com/data.json").mock(
            return_value=httpx.Response(200, headers={"content-type": "application/json"})
        )

        result = await web_fetch_js(
            "https://api.example.com/data.json",
            actions=[{"action": "click", "selector": "button"}],
        )
        # Should NOT have the pre-check warning — actions bypass pre-check
        assert "JavaScript rendering was skipped" not in result

    @pytest.mark.asyncio
    @respx.mock
    async def test_wait_for_bypasses_precheck(self):
        """When wait_for is provided, HEAD pre-check should be skipped."""
        respx.head("https://api.example.com/data.json").mock(
            return_value=httpx.Response(200, headers={"content-type": "application/json"})
        )

        result = await web_fetch_js(
            "https://api.example.com/data.json",
            wait_for=".loaded",
        )
        # Should NOT have the pre-check warning — wait_for bypasses pre-check
        assert "JavaScript rendering was skipped" not in result
