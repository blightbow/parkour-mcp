"""Tests for kagi_research_mcp.fetch_direct module."""

import httpx
import pytest
import respx

from kagi_research_mcp.fetch_direct import (
    web_fetch_direct,
    web_fetch_sections,
)
from kagi_research_mcp._pipeline import _wiki_cache, _page_cache

from .conftest import (
    SAMPLE_HTML_PAGE,
    SAMPLE_JSON_CONTENT,
    SAMPLE_PLAIN_TEXT,
    MEDIAWIKI_QUERY_RESPONSE,
    MEDIAWIKI_PARSE_FULL_RESPONSE,
)


@pytest.fixture(autouse=True)
def clear_caches():
    """Ensure each test starts with empty caches."""
    yield
    _wiki_cache.clear()
    _page_cache.clear()


# --- web_fetch_direct ---

class TestWebFetchDirectMarkdown:
    """Tests for the markdown output path."""

    @pytest.mark.asyncio
    @respx.mock
    async def test_html_returns_markdown_with_frontmatter(self):
        respx.get("https://example.com/page").mock(
            return_value=httpx.Response(
                200,
                text=SAMPLE_HTML_PAGE,
                headers={"content-type": "text/html"},
            )
        )

        result = await web_fetch_direct("https://example.com/page")
        assert result.startswith("---")
        assert "trust:" in result
        assert "source:" in result
        assert "│ # Main Heading" in result
        # Should NOT contain XML
        assert "<document" not in result

    @pytest.mark.asyncio
    @respx.mock
    async def test_json_returns_frontmatter_with_raw_body(self):
        respx.get("https://example.com/data.json").mock(
            return_value=httpx.Response(
                200,
                text=SAMPLE_JSON_CONTENT,
                headers={"content-type": "application/json"},
            )
        )

        result = await web_fetch_direct("https://example.com/data.json")
        assert "content_type: json" in result
        assert '"key": "value"' in result

    @pytest.mark.asyncio
    @respx.mock
    async def test_plain_text_returns_frontmatter_with_raw_body(self):
        respx.get("https://example.com/file.txt").mock(
            return_value=httpx.Response(
                200,
                text=SAMPLE_PLAIN_TEXT,
                headers={"content-type": "text/plain"},
            )
        )

        result = await web_fetch_direct("https://example.com/file.txt")
        assert "content_type: plain text" in result
        assert "First paragraph" in result

    @pytest.mark.asyncio
    @respx.mock
    async def test_xml_returns_frontmatter_with_raw_body(self):
        xml_content = "<root><item>test</item></root>"
        respx.get("https://example.com/data.xml").mock(
            return_value=httpx.Response(
                200,
                text=xml_content,
                headers={"content-type": "application/xml"},
            )
        )

        result = await web_fetch_direct("https://example.com/data.xml")
        assert "content_type: xml" in result
        assert "<root>" in result

    @pytest.mark.asyncio
    @respx.mock
    async def test_html_section_extraction(self):
        respx.get("https://example.com/page").mock(
            return_value=httpx.Response(
                200,
                text=SAMPLE_HTML_PAGE,
                headers={"content-type": "text/html"},
            )
        )

        result = await web_fetch_direct("https://example.com/page", section="Second Section")
        assert "Second Section" in result
        assert "│ ## Second Section" in result
        # Second Section has a child Subsection — note should warn about depth
        assert "note:" in result
        assert "Subsections are separate entries" in result

    @pytest.mark.asyncio
    @respx.mock
    async def test_html_truncation_includes_section_list(self):
        # Build a page with multiple sections that will exceed token limit
        sections_html = "".join(
            f"<h2>Section {i}</h2><p>{'Content ' * 50}</p>" for i in range(10)
        )
        html = f"<html><head><title>Big</title></head><body>{sections_html}</body></html>"
        respx.get("https://example.com/big").mock(
            return_value=httpx.Response(
                200, text=html, headers={"content-type": "text/html"}
            )
        )

        result = await web_fetch_direct("https://example.com/big", max_tokens=100)
        assert "truncated:" in result
        assert "Sections:" in result  # sections list rendered inside fence

    @pytest.mark.asyncio
    @respx.mock
    async def test_non_html_truncation(self):
        long_json = '{"data": "' + "x" * 10000 + '"}'
        respx.get("https://example.com/big.json").mock(
            return_value=httpx.Response(
                200, text=long_json, headers={"content-type": "application/json"}
            )
        )

        result = await web_fetch_direct("https://example.com/big.json", max_tokens=100)
        assert "truncated:" in result


class TestWebFetchDirectErrors:
    @pytest.mark.asyncio
    @respx.mock
    async def test_timeout_error(self):
        respx.get("https://example.com/slow").mock(
            side_effect=httpx.ConnectTimeout("timeout")
        )

        result = await web_fetch_direct("https://example.com/slow")
        assert "Error:" in result
        assert "timed out" in result

    @pytest.mark.asyncio
    @respx.mock
    async def test_http_404_error(self):
        respx.get("https://example.com/missing").mock(
            return_value=httpx.Response(404)
        )

        result = await web_fetch_direct("https://example.com/missing")
        assert "Error:" in result
        assert "404" in result

    @pytest.mark.asyncio
    @respx.mock
    async def test_unsupported_content_type(self):
        respx.get("https://example.com/file.bin").mock(
            return_value=httpx.Response(
                200,
                content=b"\x00\x01",
                headers={"content-type": "application/octet-stream"},
            )
        )

        result = await web_fetch_direct("https://example.com/file.bin")
        assert "Error:" in result
        assert "Unsupported content type" in result

    @pytest.mark.asyncio
    @respx.mock
    async def test_empty_html_body(self):
        respx.get("https://example.com/empty").mock(
            return_value=httpx.Response(
                200,
                text="<html><body></body></html>",
                headers={"content-type": "text/html"},
            )
        )

        result = await web_fetch_direct("https://example.com/empty")
        assert "Error:" in result
        assert "No content" in result

    @pytest.mark.asyncio
    @respx.mock
    async def test_connection_error(self):
        respx.get("https://example.com/down").mock(
            side_effect=httpx.ConnectError("connection refused")
        )

        result = await web_fetch_direct("https://example.com/down")
        assert "Error:" in result
        assert "ConnectError" in result


class TestWebFetchDirectParameterDowngrade:
    """Tests for soft parameter downgrades instead of hard errors."""

    @pytest.mark.asyncio
    @respx.mock
    async def test_section_plus_footnotes_ignores_footnotes(self):
        """section + footnotes should honor section and warn about footnotes."""
        respx.get("https://example.com/page").mock(
            return_value=httpx.Response(
                200,
                text=SAMPLE_HTML_PAGE,
                headers={"content-type": "text/html"},
            )
        )
        result = await web_fetch_direct(
            "https://example.com/page", section="Second Section", footnotes=[1, 2]
        )
        # Section extraction should succeed
        assert "│ ## Second Section" in result
        # Footnotes warning should appear in frontmatter
        assert "warning:" in result
        assert "footnotes parameter ignored" in result


class TestWebFetchDirectMediawikiFastPath:
    @pytest.mark.asyncio
    @respx.mock
    async def test_wiki_url_uses_api(self):
        """MediaWiki URLs should hit the API, not the full HTTP fetch."""
        # Mock the detection probe
        respx.get("https://wiki.example.com/api.php").mock(
            side_effect=[
                # Detection probe
                httpx.Response(200, json=MEDIAWIKI_QUERY_RESPONSE),
                # Full page parse
                httpx.Response(200, json=MEDIAWIKI_PARSE_FULL_RESPONSE),
            ]
        )

        result = await web_fetch_direct("https://wiki.example.com/wiki/Test_Page")
        assert "│ # Test Page" in result
        assert "site: Test Wiki" in result
        assert "generator: MediaWiki" in result

    @pytest.mark.asyncio
    @respx.mock
    async def test_wiki_api_failure_falls_through(self):
        """If the MW API probe fails, should fall through to normal HTTP fetch."""
        respx.get("https://wiki.example.com/api.php").mock(
            side_effect=httpx.ConnectError("fail")
        )
        respx.get("https://wiki.example.com/w/api.php").mock(
            side_effect=httpx.ConnectError("fail")
        )
        respx.get("https://wiki.example.com/wiki/Test_Page").mock(
            return_value=httpx.Response(
                200,
                text=SAMPLE_HTML_PAGE,
                headers={"content-type": "text/html"},
            )
        )

        result = await web_fetch_direct("https://wiki.example.com/wiki/Test_Page")
        # Should still return content via normal fetch
        assert "│ # Main Heading" in result

    @pytest.mark.asyncio
    @respx.mock
    async def test_section_param_as_string(self):
        """section='Foo' should work the same as section=['Foo']."""
        respx.get("https://example.com/page").mock(
            return_value=httpx.Response(
                200,
                text=SAMPLE_HTML_PAGE,
                headers={"content-type": "text/html"},
            )
        )

        result = await web_fetch_direct("https://example.com/page", section="Second Section")
        assert "│ ## Second Section" in result

    @pytest.mark.asyncio
    @respx.mock
    async def test_section_param_as_list(self):
        """section=['A', 'B'] should fetch multiple sections."""
        respx.get("https://example.com/page").mock(
            return_value=httpx.Response(
                200,
                text=SAMPLE_HTML_PAGE,
                headers={"content-type": "text/html"},
            )
        )

        result = await web_fetch_direct(
            "https://example.com/page", section=["Second Section", "Subsection"]
        )
        # Multi-section content appears inside the fence
        assert "│ ## Second Section" in result
        assert "│ ### Subsection" in result


class TestWebFetchDirectFragmentExtraction:
    """Tests for URL fragment → section extraction."""

    @pytest.mark.asyncio
    @respx.mock
    async def test_fragment_extracts_matching_section(self):
        """URL#second-section should extract 'Second Section'."""
        respx.get("https://example.com/page").mock(
            return_value=httpx.Response(
                200,
                text=SAMPLE_HTML_PAGE,
                headers={"content-type": "text/html"},
            )
        )

        result = await web_fetch_direct("https://example.com/page#second-section")
        assert "source: https://example.com/page#second-section" in result
        assert "│ ## Second Section" in result
        assert "Another paragraph" in result

    @pytest.mark.asyncio
    @respx.mock
    async def test_fragment_strips_from_fetch_url(self):
        """Fragment should be stripped before HTTP fetch (only example.com/page is fetched)."""
        route = respx.get("https://example.com/page").mock(
            return_value=httpx.Response(
                200,
                text=SAMPLE_HTML_PAGE,
                headers={"content-type": "text/html"},
            )
        )

        await web_fetch_direct("https://example.com/page#second-section")
        assert route.called

    @pytest.mark.asyncio
    @respx.mock
    async def test_fragment_no_match_shows_sections_with_slugs(self):
        """Unmatched fragment should show available sections with slug IDs."""
        respx.get("https://example.com/page").mock(
            return_value=httpx.Response(
                200,
                text=SAMPLE_HTML_PAGE,
                headers={"content-type": "text/html"},
            )
        )

        result = await web_fetch_direct("https://example.com/page#nonexistent")
        assert "source: https://example.com/page#nonexistent" in result
        assert "sections_not_found:" in result
        assert '"nonexistent"' in result
        assert "(#" in result  # slugs should be present in section list

    @pytest.mark.asyncio
    @respx.mock
    async def test_explicit_section_overrides_fragment(self):
        """Explicit section parameter should take precedence over URL fragment."""
        respx.get("https://example.com/page").mock(
            return_value=httpx.Response(
                200,
                text=SAMPLE_HTML_PAGE,
                headers={"content-type": "text/html"},
            )
        )

        result = await web_fetch_direct(
            "https://example.com/page#subsection", section="Second Section"
        )
        # Fragment dropped from source: explicit section= overrode it
        assert "source: https://example.com/page\n" in result
        assert "warning: URL fragment #subsection was ignored; explicit section parameter takes precedence" in result
        assert "│ ## Second Section" in result


SAMPLE_PYTHON_FILE = """\
import os
import sys

def hello():
    print("hello")

def greet(name):
    print(f"hello {name}")

class MyApp:
    def __init__(self):
        self.running = False

    def start(self):
        self.running = True

    def stop(self):
        self.running = False

if __name__ == "__main__":
    app = MyApp()
    app.start()
"""


class TestWebFetchDirectGitHubLineAnchors:
    """Tests for GitHub #L45 and #L45-L100 line anchor handling."""

    @pytest.fixture(autouse=True)
    def _clear_caches(self):
        yield
        _page_cache.clear()

    @pytest.mark.asyncio
    @respx.mock
    async def test_single_line_anchor(self):
        """#L4 should return just line 4."""
        respx.get(
            "https://raw.githubusercontent.com/owner/repo/main/app.py"
        ).mock(return_value=httpx.Response(200, text=SAMPLE_PYTHON_FILE))

        result = await web_fetch_direct(
            "https://github.com/owner/repo/blob/main/app.py#L4"
        )
        assert "lines: 4-4 of" in result
        assert "4 | def hello():" in result
        # Should NOT contain surrounding lines
        assert "import os" not in result
        assert "def greet" not in result

    @pytest.mark.asyncio
    @respx.mock
    async def test_line_range_anchor(self):
        """#L4-L6 should return lines 4 through 6."""
        respx.get(
            "https://raw.githubusercontent.com/owner/repo/main/app.py"
        ).mock(return_value=httpx.Response(200, text=SAMPLE_PYTHON_FILE))

        result = await web_fetch_direct(
            "https://github.com/owner/repo/blob/main/app.py#L4-L6"
        )
        assert "lines: 4-6 of" in result
        assert "4 | def hello():" in result
        assert '5 |     print("hello")' in result
        assert "6 |" in result

    @pytest.mark.asyncio
    @respx.mock
    async def test_line_range_beyond_file_returns_error(self):
        """#L9000-L9999 on a short file should return an error."""
        respx.get(
            "https://raw.githubusercontent.com/owner/repo/main/app.py"
        ).mock(return_value=httpx.Response(200, text=SAMPLE_PYTHON_FILE))

        result = await web_fetch_direct(
            "https://github.com/owner/repo/blob/main/app.py#L9000-L9999"
        )
        assert "Error:" in result
        assert "9000" in result

    @pytest.mark.asyncio
    @respx.mock
    async def test_line_range_partial_overlap_warns(self):
        """#L20-L100 on a 23-line file should clamp with a warning."""
        respx.get(
            "https://raw.githubusercontent.com/owner/repo/main/app.py"
        ).mock(return_value=httpx.Response(200, text=SAMPLE_PYTHON_FILE))

        result = await web_fetch_direct(
            "https://github.com/owner/repo/blob/main/app.py#L20-L100"
        )
        assert "lines: 20-23 of" in result
        assert "warning:" in result
        assert "file ends at line 23" in result

    @pytest.mark.asyncio
    @respx.mock
    async def test_line_range_reversed_returns_error(self):
        """#L100-L50 should return an error for reversed range."""
        respx.get(
            "https://raw.githubusercontent.com/owner/repo/main/app.py"
        ).mock(return_value=httpx.Response(200, text=SAMPLE_PYTHON_FILE))

        result = await web_fetch_direct(
            "https://github.com/owner/repo/blob/main/app.py#L100-L50"
        )
        assert "Error:" in result
        assert "Invalid line range" in result

    @pytest.mark.asyncio
    @respx.mock
    async def test_line_anchor_not_treated_as_section(self):
        """#L45 should not produce sections_not_found."""
        respx.get(
            "https://raw.githubusercontent.com/owner/repo/main/app.py"
        ).mock(return_value=httpx.Response(200, text=SAMPLE_PYTHON_FILE))

        result = await web_fetch_direct(
            "https://github.com/owner/repo/blob/main/app.py#L1"
        )
        assert "sections_not_found" not in result
        assert "lines: 1-1 of" in result

    @pytest.mark.asyncio
    @respx.mock
    async def test_non_line_fragment_still_becomes_section(self):
        """#some-heading should still be treated as section request."""
        respx.get("https://example.com/page").mock(
            return_value=httpx.Response(
                200,
                text=SAMPLE_HTML_PAGE,
                headers={"content-type": "text/html"},
            )
        )

        result = await web_fetch_direct("https://example.com/page#second-section")
        assert "│ ## Second Section" in result


class TestWebFetchDirectRawGitHub:
    """Tests for raw.githubusercontent.com routing through GitHub fast path."""

    @pytest.fixture(autouse=True)
    def _clear_caches(self):
        yield
        _page_cache.clear()

    @pytest.mark.asyncio
    @respx.mock
    async def test_raw_github_gets_line_numbers(self):
        """raw.githubusercontent.com URLs should get line-numbered output."""
        respx.get(
            "https://raw.githubusercontent.com/owner/repo/main/app.py"
        ).mock(return_value=httpx.Response(200, text=SAMPLE_PYTHON_FILE))

        result = await web_fetch_direct(
            "https://raw.githubusercontent.com/owner/repo/main/app.py"
        )
        assert "api: GitHub (raw)" in result
        assert "1 | import os" in result

    @pytest.mark.asyncio
    @respx.mock
    async def test_raw_github_populates_cache(self):
        """raw.githubusercontent.com should populate the page cache."""
        respx.get(
            "https://raw.githubusercontent.com/owner/repo/main/app.py"
        ).mock(return_value=httpx.Response(200, text=SAMPLE_PYTHON_FILE))

        await web_fetch_direct(
            "https://raw.githubusercontent.com/owner/repo/main/app.py"
        )
        cached = _page_cache.get(
            "https://raw.githubusercontent.com/owner/repo/main/app.py"
        )
        assert cached is not None


class TestWebFetchDirectGitHubWiki:
    """Tests for GitHub wiki page handling."""

    @pytest.fixture(autouse=True)
    def _clear_caches(self):
        yield
        _page_cache.clear()

    @pytest.mark.asyncio
    @respx.mock
    async def test_wiki_page_fetched(self):
        """Wiki page should be fetched as raw markdown."""
        respx.get(
            "https://raw.githubusercontent.com/wiki/owner/repo/Getting-Started.md"
        ).mock(return_value=httpx.Response(200, text="# Getting Started\n\nWelcome!"))

        result = await web_fetch_direct(
            "https://github.com/owner/repo/wiki/Getting-Started"
        )
        assert "api: GitHub (wiki)" in result
        assert "Welcome!" in result

    @pytest.mark.asyncio
    @respx.mock
    async def test_wiki_root_defaults_to_home(self):
        """Wiki root URL should fetch Home.md."""
        route = respx.get(
            "https://raw.githubusercontent.com/wiki/owner/repo/Home.md"
        ).mock(return_value=httpx.Response(200, text="# Home\n\nWiki home page."))

        result = await web_fetch_direct("https://github.com/owner/repo/wiki")
        assert route.called
        assert "Wiki home page" in result

    @pytest.mark.asyncio
    @respx.mock
    async def test_wiki_page_404(self):
        """Missing wiki page should return clean error naming the page."""
        respx.get(
            "https://raw.githubusercontent.com/wiki/owner/repo/Nonexistent.md"
        ).mock(return_value=httpx.Response(404))

        result = await web_fetch_direct(
            "https://github.com/owner/repo/wiki/Nonexistent"
        )
        assert "Error:" in result
        assert "Nonexistent" in result
        assert "not found" in result

    @pytest.mark.asyncio
    @respx.mock
    async def test_wiki_root_404_no_wiki(self):
        """Wiki root 404 should report that the wiki doesn't exist."""
        respx.get(
            "https://raw.githubusercontent.com/wiki/owner/repo/Home.md"
        ).mock(return_value=httpx.Response(404))

        result = await web_fetch_direct("https://github.com/owner/repo/wiki")
        assert "Error:" in result
        assert "does not have a wiki" in result


class TestWebFetchDirectGitHubCommit:
    """Tests for GitHub commit handling."""

    @pytest.fixture(autouse=True)
    def _clear_caches(self):
        yield
        _page_cache.clear()

    @pytest.mark.asyncio
    @respx.mock
    async def test_commit_rendered(self):
        """Commit URL should render via API."""
        respx.get("https://api.github.com/repos/owner/repo/commits/abc1234").mock(
            return_value=httpx.Response(200, json={
                "sha": "abc1234def5678",
                "commit": {
                    "message": "Fix the widget\n\nLonger description here.",
                    "author": {"name": "Alice", "date": "2026-01-15T10:00:00Z"},
                },
                "stats": {"total": 5, "additions": 3, "deletions": 2},
                "files": [
                    {"filename": "widget.py", "status": "modified", "additions": 3, "deletions": 2},
                ],
            })
        )

        result = await web_fetch_direct(
            "https://github.com/owner/repo/commit/abc1234"
        )
        assert "type: commit" in result
        assert "Alice" in result
        assert "Fix the widget" in result
        assert "widget.py" in result


class TestWebFetchDirectGitHubCompare:
    """Tests for GitHub compare handling."""

    @pytest.fixture(autouse=True)
    def _clear_caches(self):
        yield
        _page_cache.clear()

    @pytest.mark.asyncio
    @respx.mock
    async def test_compare_rendered(self):
        """Compare URL should render via API."""
        respx.get("https://api.github.com/repos/owner/repo/compare/v1.0...v2.0").mock(
            return_value=httpx.Response(200, json={
                "status": "ahead",
                "base_commit": {"sha": "aaa1111"},
                "commits": [
                    {"sha": "bbb2222", "commit": {"message": "Add feature"}},
                    {"sha": "ccc3333", "commit": {"message": "Fix bug"}},
                ],
                "files": [
                    {"filename": "app.py", "status": "modified", "additions": 10, "deletions": 3},
                ],
            })
        )

        result = await web_fetch_direct(
            "https://github.com/owner/repo/compare/v1.0...v2.0"
        )
        assert "type: compare" in result
        assert "status: ahead" in result
        assert "Add feature" in result
        assert "app.py" in result


class TestWebFetchDirectGitHubUnsupported:
    """Tests for unsupported GitHub paths returning clean errors."""

    @pytest.fixture(autouse=True)
    def _clear_caches(self):
        yield
        _page_cache.clear()

    @pytest.mark.asyncio
    async def test_blame_returns_error(self):
        result = await web_fetch_direct(
            "https://github.com/owner/repo/blame/main/src/app.py"
        )
        assert "Error:" in result
        assert "Blame" in result

    @pytest.mark.asyncio
    async def test_actions_returns_error(self):
        result = await web_fetch_direct(
            "https://github.com/owner/repo/actions"
        )
        assert "Error:" in result


class TestWebFetchDirectGitHubOrg:
    """Tests for GitHub org/user profile handling."""

    @pytest.fixture(autouse=True)
    def _clear_caches(self):
        yield
        _page_cache.clear()

    @pytest.mark.asyncio
    @respx.mock
    async def test_org_profile(self):
        """Org URL should render profile with repo list."""
        respx.get("https://api.github.com/orgs/myorg").mock(
            return_value=httpx.Response(200, json={
                "name": "My Organization",
                "description": "Building cool stuff",
                "public_repos": 42,
            })
        )
        respx.get("https://api.github.com/orgs/myorg/repos").mock(
            return_value=httpx.Response(200, json=[
                {"name": "project-a", "description": "Main project", "stargazers_count": 100, "language": "Python"},
                {"name": "project-b", "description": None, "stargazers_count": 5, "language": "Go"},
            ])
        )

        result = await web_fetch_direct("https://github.com/myorg")
        assert "type: organization" in result
        assert "My Organization" in result
        assert "project-a" in result
        assert "100" in result

    @pytest.mark.asyncio
    @respx.mock
    async def test_user_profile_fallback(self):
        """Personal account should fall back to /users/ endpoint."""
        respx.get("https://api.github.com/orgs/someuser").mock(
            return_value=httpx.Response(404, json={"message": "Not Found"})
        )
        respx.get("https://api.github.com/users/someuser").mock(
            return_value=httpx.Response(200, json={
                "name": "Some User",
                "bio": "Developer",
                "public_repos": 10,
            })
        )
        respx.get("https://api.github.com/users/someuser/repos").mock(
            return_value=httpx.Response(200, json=[])
        )

        result = await web_fetch_direct("https://github.com/someuser")
        assert "type: user" in result
        assert "Some User" in result

    @pytest.mark.asyncio
    async def test_system_page_not_intercepted(self):
        """System pages like /explore should not be intercepted."""
        from kagi_research_mcp.github import _detect_github_url
        assert _detect_github_url("https://github.com/explore") is None
        assert _detect_github_url("https://github.com/settings") is None


class TestWebFetchDirectGitHubReleases:
    """Tests for GitHub releases handling."""

    @pytest.fixture(autouse=True)
    def _clear_caches(self):
        yield
        _page_cache.clear()

    @pytest.mark.asyncio
    @respx.mock
    async def test_releases_list(self):
        """Releases list URL should render recent releases."""
        respx.get("https://api.github.com/repos/owner/repo/releases").mock(
            return_value=httpx.Response(200, json=[
                {"tag_name": "v2.0", "name": "Version 2.0", "published_at": "2026-03-01T00:00:00Z", "prerelease": False},
                {"tag_name": "v1.0", "name": "Version 1.0", "published_at": "2026-01-01T00:00:00Z", "prerelease": False},
            ])
        )

        result = await web_fetch_direct("https://github.com/owner/repo/releases")
        assert "type: releases" in result
        assert "Version 2.0" in result
        assert "v2.0" in result
        assert "hint:" in result

    @pytest.mark.asyncio
    @respx.mock
    async def test_release_tag(self):
        """Specific tag URL should render full release notes."""
        respx.get("https://api.github.com/repos/owner/repo/releases/tags/v2.0").mock(
            return_value=httpx.Response(200, json={
                "name": "Version 2.0",
                "tag_name": "v2.0",
                "body": "## What's new\n\n- Feature A\n- Feature B",
                "published_at": "2026-03-01T00:00:00Z",
                "author": {"login": "maintainer"},
                "prerelease": False,
                "assets": [
                    {"name": "release.tar.gz", "size": 5242880, "download_count": 1000},
                ],
            })
        )

        result = await web_fetch_direct("https://github.com/owner/repo/releases/tag/v2.0")
        assert "type: release" in result
        assert "Version 2.0" in result
        assert "Feature A" in result
        assert "release.tar.gz" in result
        assert "1,000 downloads" in result


class TestWebFetchSections:
    """Tests for the web_fetch_sections tool."""

    @pytest.mark.asyncio
    @respx.mock
    async def test_returns_section_tree_with_slugs(self):
        respx.get("https://example.com/page").mock(
            return_value=httpx.Response(
                200,
                text=SAMPLE_HTML_PAGE,
                headers={"content-type": "text/html"},
            )
        )

        result = await web_fetch_sections("https://example.com/page")
        assert "│ - Main Heading" in result
        assert "(#main-heading)" in result
        assert "(#second-section)" in result
        assert "(#subsection)" in result
        assert "hint:" in result
        assert "section parameter" in result
        # Should NOT contain page content
        assert "paragraph" not in result

    @pytest.mark.asyncio
    @respx.mock
    async def test_fragment_resolves_against_tree(self):
        respx.get("https://example.com/page").mock(
            return_value=httpx.Response(
                200,
                text=SAMPLE_HTML_PAGE,
                headers={"content-type": "text/html"},
            )
        )

        result = await web_fetch_sections("https://example.com/page#second-section")
        assert "source: https://example.com/page#second-section" in result
        # Fragment match info no longer surfaced; full tree still shown
        assert "│ - " in result

    @pytest.mark.asyncio
    @respx.mock
    async def test_unmatched_fragment(self):
        respx.get("https://example.com/page").mock(
            return_value=httpx.Response(
                200,
                text=SAMPLE_HTML_PAGE,
                headers={"content-type": "text/html"},
            )
        )

        result = await web_fetch_sections("https://example.com/page#nonexistent")
        assert "source: https://example.com/page#nonexistent" in result
        assert "sections_not_found:" in result
        assert '"nonexistent"' in result
        assert "│ - " in result

    @pytest.mark.asyncio
    @respx.mock
    async def test_non_html_returns_error(self):
        respx.get("https://example.com/data.json").mock(
            return_value=httpx.Response(
                200,
                text='{"key": "value"}',
                headers={"content-type": "application/json"},
            )
        )

        result = await web_fetch_sections("https://example.com/data.json")
        assert "Error:" in result
        assert "HTML" in result

    @pytest.mark.asyncio
    @respx.mock
    async def test_no_sections_found(self):
        html = "<html><body><p>Just a paragraph, no headings.</p></body></html>"
        respx.get("https://example.com/flat").mock(
            return_value=httpx.Response(
                200, text=html, headers={"content-type": "text/html"},
            )
        )

        result = await web_fetch_sections("https://example.com/flat")
        assert "No sections found" in result
