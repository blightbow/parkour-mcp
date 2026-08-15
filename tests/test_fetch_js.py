"""Tests for parkour_mcp.fetch_js — the requires_js headless-render path.

Reached via web_fetch_direct(..., requires_js=True) / actions=[...]. Most
browser-path tests are excluded because they require a real Playwright
browser; the navigation-contract tests below substitute a fake playwright
whose surface stops at the first branch under test, so the status check and
the wait strategy are covered without installing one.
Covers: MediaWiki fast path under requires_js, search/slices, content-type
pre-check, navigation contract.
"""

import importlib

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


# --- Navigation contract (fake playwright) ---

class _FakeResponse:
    def __init__(self, status):
        self.status = status


class _FakePage:
    """Records how it was navigated and replays a canned response.

    Only the surface `_render_js` touches before it decides, which is the
    point: a fake that stops where the branch under test does cannot drift
    into asserting the rest of the render.
    """

    def __init__(self, status, html):
        self._status = status
        self._html = html
        self.goto_kwargs: dict = {}

    async def route(self, pattern, handler):
        pass

    async def goto(self, url, **kwargs):
        self.goto_kwargs = kwargs
        return _FakeResponse(self._status)

    async def content(self):
        return self._html

    async def query_selector(self, selector):
        return None

    async def wait_for_load_state(self, state, timeout=None):
        pass

    async def title(self):
        return "Fake"


class _FakeContext:
    def __init__(self, page):
        self._page = page

    async def new_page(self):
        return self._page


class _FakeBrowser:
    def __init__(self, page):
        self._page = page

    async def new_context(self, **kwargs):
        return _FakeContext(self._page)

    async def close(self):
        pass


class _FakeLauncher:
    def __init__(self, page):
        self._page = page

    async def launch(self, **kwargs):
        return _FakeBrowser(self._page)


class _FakePlaywright:
    def __init__(self, page):
        self.webkit = _FakeLauncher(page)


class _FakePlaywrightCM:
    def __init__(self, page):
        self._page = page

    async def __aenter__(self):
        return _FakePlaywright(self._page)

    async def __aexit__(self, *exc):
        return False


class TestRenderNavigationContract:
    """The render path used to discard the navigation status entirely, so a
    server that refused the request had its error page rendered, fenced and
    returned as though it were the document.  Observed on bbs.nga.cn, whose
    403 body is a login wall: the static path refused it correctly while the
    requires_js path presented the wall as the thread."""

    @pytest.fixture
    def fake_page(self, monkeypatch):
        def _install(status, html):
            page = _FakePage(status, html)
            # fetch_js is banned from module-level import (it drags in
            # playwright), so it is absent from sys.modules until the first
            # render.  Import it here rather than at file scope, which would
            # trip the same ban in the linter.
            mod = importlib.import_module("parkour_mcp.fetch_js")
            monkeypatch.setattr(
                mod, "async_playwright", lambda: _FakePlaywrightCM(page)
            )
            monkeypatch.setattr(
                mod, "_detect_playwright_browser", lambda p: ("webkit", "WebKit")
            )
            return page
        return _install

    @pytest.mark.asyncio
    @respx.mock
    async def test_error_status_is_not_rendered_as_content(self, fake_page):
        respx.head("https://walled.example.com/thread").mock(
            return_value=httpx.Response(403)
        )
        fake_page(403, "<html><body><p>You may need to log in</p></body></html>")

        result = await web_fetch_direct(
            "https://walled.example.com/thread", requires_js=True
        )
        assert result.startswith("Error: HTTP 403")
        # The body survives, because "403" alone does not distinguish a login
        # wall from a bot challenge from a dead link.
        assert "You may need to log in" in result
        assert "untrusted content" in result
        # And it is not dressed as a successful fetch.
        assert "source: https://walled.example.com/thread" not in result

    @pytest.mark.asyncio
    @respx.mock
    async def test_error_page_without_text_stays_bare(self, fake_page):
        """A challenge page builds its body from script, so the extract can
        be empty.  An empty fence would be noise, not provenance."""
        respx.head("https://walled.example.com/thread").mock(
            return_value=httpx.Response(403)
        )
        fake_page(403, "<html><body></body></html>")

        result = await web_fetch_direct(
            "https://walled.example.com/thread", requires_js=True
        )
        assert result.strip() == (
            "Error: HTTP 403 for https://walled.example.com/thread"
        )

    @pytest.mark.asyncio
    @respx.mock
    async def test_navigation_does_not_wait_for_subresources(self, fake_page):
        """`load` blocks on every subresource and fails hard at the full
        timeout when one never settles, while the networkidle wait that
        follows is capped and extracts anyway.  Same hazard, so the same
        tolerance."""
        respx.head("https://ok.example.com/page").mock(
            return_value=httpx.Response(403)
        )
        page = fake_page(200, "<html><body><h1>Hi</h1><p>Body</p></body></html>")

        await web_fetch_direct("https://ok.example.com/page", requires_js=True)
        assert page.goto_kwargs.get("wait_until") == "domcontentloaded"

    @pytest.mark.asyncio
    @respx.mock
    async def test_ok_status_passes_the_gate(self, fake_page):
        """The gate must not swallow the ordinary case.  The fake stops
        short of a full render, so this asserts only that navigation got
        past the status check — anything further would be asserting the
        fake, not the code."""
        respx.head("https://ok.example.com/page").mock(
            return_value=httpx.Response(403)
        )
        fake_page(200, "<html><body><h1>Hi</h1><p>Real content here</p></body></html>")

        result = await web_fetch_direct(
            "https://ok.example.com/page", requires_js=True
        )
        assert not result.startswith("Error: HTTP")
