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


class TestUnusableBrowserDiagnosis:
    """"Not installed" and "installed at the wrong revision" need different
    fixes and look identical through ``executable_path``, which reports a path
    that simply is not there.  Reporting the second as the first sends the
    reader hunting for a missing download instead of a version skew."""

    class _FakeBrowserType:
        def __init__(self, path):
            self._path = path

        @property
        def executable_path(self):
            if self._path is None:
                raise RuntimeError("engine cannot be placed")
            return self._path

    def _browser_info(self, root, expected, present):
        """Build a browser_info map over a fake Playwright registry root."""
        for name, revisions in present.items():
            for revision in revisions:
                (root / f"{name}-{revision}").mkdir(parents=True, exist_ok=True)
        return {
            name: (
                name.title(),
                self._FakeBrowserType(
                    None
                    if revision is None
                    else str(root / f"{name}-{revision}" / "pw_run.sh")
                ),
            )
            for name, revision in expected.items()
        }

    def _describe(self, *args):
        mod = importlib.import_module("parkour_mcp.fetch_js")
        return mod._describe_unusable_browsers(*args)

    def test_stale_revisions_are_named_not_reported_as_absent(self, tmp_path):
        """The real failure: browsers on disk, from an older Playwright.

        Both revisions here are genuine — ``webkit-2227`` ships with
        Playwright v1.57.0 and ``chromium-1228`` with v1.61.x, against a
        v1.62.0 package wanting 2336 and 1234.
        """
        info = self._browser_info(
            tmp_path / "ms-playwright",
            {"webkit": "2336", "chromium": "1234"},
            {"webkit": ["2227"], "chromium": ["1228"]},
        )
        message = self._describe(info)
        assert "No Playwright browser installed" not in message
        assert "needs webkit-2336, found webkit-2227" in message
        assert "needs chromium-1234, found chromium-1228" in message

    def test_the_fix_command_names_the_stale_engines(self, tmp_path):
        """The message has to be actionable without a second diagnosis step."""
        info = self._browser_info(
            tmp_path / "ms-playwright",
            {"webkit": "2336", "chromium": "1234"},
            {"webkit": ["2227"]},
        )
        assert "playwright install webkit" in self._describe(info)

    def test_an_empty_registry_still_reports_absence(self, tmp_path):
        """A fresh machine genuinely has nothing, and needs the install hint."""
        info = self._browser_info(
            tmp_path / "ms-playwright", {"webkit": "2336"}, {}
        )
        message = self._describe(info)
        assert "No Playwright browser installed" in message
        assert "playwright install webkit" in message

    def test_a_current_engine_beside_a_stale_one_is_not_reported(self, tmp_path):
        """Only engines whose wanted revision is absent are stale.

        ``playwright install`` leaves old revisions in place, so the wanted
        one sitting beside its predecessors is the normal post-upgrade state.
        """
        info = self._browser_info(
            tmp_path / "ms-playwright",
            {"webkit": "2336", "chromium": "1234"},
            {"webkit": ["2227", "2336"], "chromium": ["1228"]},
        )
        message = self._describe(info)
        assert "needs webkit" not in message
        assert "needs chromium-1234, found chromium-1228" in message
        assert "playwright install chromium" in message

    def test_the_remedy_works_from_any_environment(self, tmp_path):
        """Browsers live in a shared, version-keyed cache, not in a virtualenv.

        The Claude Desktop bundle has no activated environment to run a bare
        ``playwright install`` in, so the remedy is pinned to the version and
        runnable from anywhere.
        """
        info = self._browser_info(
            tmp_path / "ms-playwright", {"webkit": "2336"}, {"webkit": ["2227"]}
        )
        message = self._describe(info)
        assert "uvx --from playwright==" in message
        assert "playwright install webkit" in message

    def test_the_auto_install_flag_is_named(self, tmp_path):
        """A caller who would rather not run a command needs to know the gate."""
        info = self._browser_info(tmp_path / "ms-playwright", {"webkit": "2336"}, {})
        assert "MCP_AUTO_INSTALL_BROWSER=1" in self._describe(info)

    def test_an_engine_with_no_executable_path_is_skipped(self, tmp_path):
        """Playwright refuses a path for an engine it cannot place at all."""
        info = self._browser_info(
            tmp_path / "ms-playwright",
            {"webkit": "2336", "firefox": None},
            {"webkit": ["2227"]},
        )
        message = self._describe(info)
        assert "firefox" not in message
        assert "needs webkit-2336" in message


class TestBrowserAutoInstall:
    """The install gate and the driver seam behind it.

    ``playwright install`` has no Python implementation — it shells to a Node
    runtime bundled inside the wheel — so the seam is what lets a browser be
    fetched with no ``playwright`` console script on PATH.
    """

    def _mod(self):
        return importlib.import_module("parkour_mcp.fetch_js")

    def _info(self, tmp_path, expected, present):
        for name, revisions in present.items():
            for revision in revisions:
                (tmp_path / f"{name}-{revision}").mkdir(parents=True, exist_ok=True)

        class _FakeBrowserType:
            def __init__(self, path):
                self.executable_path = path

        return {
            name: (
                name.title(),
                _FakeBrowserType(str(tmp_path / f"{name}-{revision}" / "pw_run.sh")),
            )
            for name, revision in expected.items()
        }

    def test_a_skew_installs_only_the_engines_that_are_behind(self, tmp_path):
        info = self._info(
            tmp_path,
            {"webkit": "2336", "chromium": "1234", "firefox": "1538"},
            {"webkit": ["2227"], "chromium": ["1234"]},
        )
        assert self._mod()._engines_to_install(info) == ["webkit"]

    def test_a_bare_absence_installs_only_webkit(self, tmp_path, monkeypatch):
        """Three downloads to satisfy one render buys nothing.

        webkit matches ``_detect_playwright_browser``'s footprint preference.
        """
        monkeypatch.delenv("PLAYWRIGHT_BROWSER", raising=False)
        info = self._info(tmp_path, {"webkit": "2336", "chromium": "1234"}, {})
        assert self._mod()._engines_to_install(info) == ["webkit"]

    def test_playwright_browser_pins_which_engine_is_fetched(self, tmp_path, monkeypatch):
        monkeypatch.setenv("PLAYWRIGHT_BROWSER", "chromium")
        info = self._info(tmp_path, {"webkit": "2336", "chromium": "1234"}, {})
        assert self._mod()._engines_to_install(info) == ["chromium"]

    def test_an_unsubstituted_manifest_template_leaves_the_gate_shut(self, monkeypatch):
        """manifest.json passes this as ``${user_config.MCP_AUTO_INSTALL_BROWSER}``.

        If Claude Desktop ever hands the template through unsubstituted, the
        server must read it as "off" — the gate guards a ~100 MB download, so
        the fail-safe direction is closed.
        """
        from parkour_mcp.common import _parse_truthy_env

        mod = self._mod()
        monkeypatch.setenv(mod._AUTO_INSTALL_ENV, "${user_config.MCP_AUTO_INSTALL_BROWSER}")
        assert _parse_truthy_env(mod._AUTO_INSTALL_ENV) is False

    def test_the_toggle_being_off_reads_as_off(self, monkeypatch):
        """A boolean user_config left unticked substitutes as "false"."""
        from parkour_mcp.common import _parse_truthy_env

        mod = self._mod()
        monkeypatch.setenv(mod._AUTO_INSTALL_ENV, "false")
        assert _parse_truthy_env(mod._AUTO_INSTALL_ENV) is False
        monkeypatch.setenv(mod._AUTO_INSTALL_ENV, "true")
        assert _parse_truthy_env(mod._AUTO_INSTALL_ENV) is True

    @pytest.mark.asyncio
    async def test_an_unrunnable_installer_reports_rather_than_raises(
        self, tmp_path, monkeypatch
    ):
        """The render path returns strings; a missing driver must not escape."""
        mod = self._mod()
        monkeypatch.setattr(
            mod, "compute_driver_executable",
            lambda: (str(tmp_path / "absent-node"), str(tmp_path / "cli.js")),
        )
        failure = await mod._install_browsers(["webkit"])
        assert failure is not None
        assert "could not run the Playwright installer" in failure


class TestBrowserOverride:
    """PLAYWRIGHT_BROWSER reaches us as free text.

    The MCPB user_config schema has no enum type — ``additionalProperties:
    false`` closes it to one — so the Claude Desktop settings field cannot
    constrain the value at the UI and every typo arrives here intact.
    """

    def _mod(self):
        return importlib.import_module("parkour_mcp.fetch_js")

    def _info(self, tmp_path, present):
        class _FakeBrowserType:
            def __init__(self, path):
                self.executable_path = path

        for name in present:
            (tmp_path / f"{name}-1").mkdir(parents=True, exist_ok=True)
        return {
            name: (name.title(), _FakeBrowserType(str(tmp_path / f"{name}-1" / "exe")))
            for name in ("webkit", "chromium", "firefox")
        }

    @pytest.mark.parametrize("value", ["auto", "AUTO", "  auto  ", ""])
    def test_auto_and_unset_express_no_preference(self, value, monkeypatch):
        """The manifest ships "auto" as the default, so it must mean unset."""
        monkeypatch.setenv("PLAYWRIGHT_BROWSER", value)
        assert self._mod()._browser_override() == ""

    @pytest.mark.parametrize("value", ["WEBKIT", " webkit ", "WebKit"])
    def test_case_and_padding_are_forgiven(self, value, monkeypatch):
        """A settings field invites both."""
        monkeypatch.setenv("PLAYWRIGHT_BROWSER", value)
        assert self._mod()._browser_override() == "webkit"

    @pytest.mark.parametrize(
        ("name", "engine"),
        [
            ("chrome", "chromium"),
            ("google-chrome", "chromium"),
            ("msedge", "chromium"),
            ("edge", "chromium"),
            ("chrome-canary", "chromium"),
            ("safari", "webkit"),
            ("gecko", "firefox"),
            ("moz-firefox", "firefox"),
        ],
    )
    def test_a_browser_name_resolves_to_the_engine_beneath_it(
        self, name, engine, monkeypatch
    ):
        """Footprint fallback answered "chrome" with webkit — the engine
        furthest from it — and did so even with chromium installed and ready.

        Playwright's own CLI documents --channel as a "Chromium distribution
        channel" over exactly these Chrome and Edge spellings, so resolving
        them reads its vocabulary rather than guessing.
        """
        monkeypatch.setenv("PLAYWRIGHT_BROWSER", name)
        preference = self._mod()._resolve_browser_preference()
        assert preference.engine == engine
        assert preference.warning is None
        # A channel is not an engine; the substitution is stated, not silent.
        assert preference.note is not None
        assert engine in preference.note

    @pytest.mark.parametrize("typo", ["webkitt", "chormium", "netscape", "opera"])
    def test_an_unrecognized_name_warns_and_renders_anyway(self, typo, monkeypatch):
        """Never an error: a bad preference must not refuse a fetch that works.

        "opera" is Chromium-derived but is not Playwright vocabulary, which is
        where the alias table deliberately stops.
        """
        monkeypatch.setenv("PLAYWRIGHT_BROWSER", typo)
        preference = self._mod()._resolve_browser_preference()
        assert preference.engine == ""
        assert preference.note is None
        assert typo in preference.warning
        assert "auto" in preference.warning

    @pytest.mark.parametrize("name", ["webkit", "chromium", "firefox"])
    def test_an_engine_name_is_taken_as_given(self, name, monkeypatch):
        monkeypatch.setenv("PLAYWRIGHT_BROWSER", name)
        assert self._mod()._resolve_browser_preference() == (name, None, None)

    def test_a_requested_engine_outranks_an_unrelated_skew(self, tmp_path, monkeypatch):
        """A machine can want chromium-1234 while holding 1228 and still be
        answering a request for firefox; the request is what must be met."""
        monkeypatch.setenv("PLAYWRIGHT_BROWSER", "firefox")
        mod = self._mod()

        class _FakeBrowserType:
            def __init__(self, path):
                self.executable_path = path

        (tmp_path / "chromium-1228").mkdir(parents=True)
        info = {
            "chromium": ("Chromium", _FakeBrowserType(str(tmp_path / "chromium-1234" / "e"))),
            "firefox": ("Firefox", _FakeBrowserType(str(tmp_path / "firefox-1538" / "e"))),
        }
        assert mod._engines_to_install(info) == ["firefox"]
        assert "playwright install firefox" in mod._describe_unusable_browsers(info)

    def test_a_missing_requested_engine_is_not_silently_substituted(self, tmp_path, monkeypatch):
        """Returning the override unchecked skipped the "none" branch, so the
        request bypassed the diagnosis and the auto-install gate alike and
        surfaced as a raw BrowserType.launch traceback."""
        monkeypatch.setenv("PLAYWRIGHT_BROWSER", "firefox")
        mod = self._mod()
        info = self._info(tmp_path, ["webkit"])

        class _FakePlaywright:
            webkit = info["webkit"][1]
            chromium = info["chromium"][1]
            firefox = info["firefox"][1]

        assert mod._detect_playwright_browser(_FakePlaywright()) == ("none", "None")
