"""Tests for claude_web_tools.mediawiki module."""

import httpx
import pytest
import respx

from claude_web_tools.mediawiki import (
    _clean_display_title,
    _detect_mediawiki,
    _fetch_mediawiki_page,
    _mediawiki_html_to_markdown,
)

from .conftest import (
    MEDIAWIKI_QUERY_RESPONSE,
    MEDIAWIKI_QUERY_MISSING_PAGE,
    MEDIAWIKI_PARSE_FULL_RESPONSE,
    MEDIAWIKI_PARSE_SECTIONS_RESPONSE,
    MEDIAWIKI_PARSE_SECTION_TEXT,
)


# --- _detect_mediawiki ---

class TestDetectMediawiki:
    @pytest.mark.asyncio
    async def test_returns_none_for_non_wiki_url(self):
        result = await _detect_mediawiki("https://example.com/page")
        assert result is None

    @pytest.mark.asyncio
    async def test_returns_none_for_empty_title(self):
        result = await _detect_mediawiki("https://example.com/wiki/")
        assert result is None

    @pytest.mark.asyncio
    @respx.mock
    async def test_detects_valid_mediawiki(self):
        respx.get("https://wiki.example.com/api.php").mock(
            return_value=httpx.Response(200, json=MEDIAWIKI_QUERY_RESPONSE)
        )

        result = await _detect_mediawiki("https://wiki.example.com/wiki/Test_Page")
        assert result is not None
        assert result["api_base"] == "https://wiki.example.com/api.php"
        assert result["page_title"] == "Test_Page"
        assert result["page_length"] == 5000
        assert result["sitename"] == "Test Wiki"
        assert result["generator"] == "MediaWiki 1.39.7"

    @pytest.mark.asyncio
    @respx.mock
    async def test_falls_back_to_w_api_php(self):
        respx.get("https://wiki.example.com/api.php").mock(
            return_value=httpx.Response(404)
        )
        respx.get("https://wiki.example.com/w/api.php").mock(
            return_value=httpx.Response(200, json=MEDIAWIKI_QUERY_RESPONSE)
        )

        result = await _detect_mediawiki("https://wiki.example.com/wiki/Test_Page")
        assert result is not None
        assert result["api_base"] == "https://wiki.example.com/w/api.php"

    @pytest.mark.asyncio
    @respx.mock
    async def test_returns_none_for_missing_page(self):
        respx.get("https://wiki.example.com/api.php").mock(
            return_value=httpx.Response(200, json=MEDIAWIKI_QUERY_MISSING_PAGE)
        )
        respx.get("https://wiki.example.com/w/api.php").mock(
            return_value=httpx.Response(200, json=MEDIAWIKI_QUERY_MISSING_PAGE)
        )

        result = await _detect_mediawiki("https://wiki.example.com/wiki/Nonexistent_Page")
        assert result is None

    @pytest.mark.asyncio
    @respx.mock
    async def test_returns_none_when_all_probes_fail(self):
        respx.get("https://wiki.example.com/api.php").mock(
            return_value=httpx.Response(500)
        )
        respx.get("https://wiki.example.com/w/api.php").mock(
            return_value=httpx.Response(500)
        )

        result = await _detect_mediawiki("https://wiki.example.com/wiki/Test_Page")
        assert result is None

    @pytest.mark.asyncio
    async def test_url_decodes_page_title(self):
        """Page titles with URL encoding should be decoded."""
        # This will fail the HTTP probe (no mock), but we can check the gate logic
        # by verifying it doesn't return None for a URL with /wiki/ and encoded title
        # We need to mock for a full test
        result = await _detect_mediawiki("https://example.com/not-a-wiki/page")
        assert result is None  # no /wiki/ in path

    @pytest.mark.asyncio
    @respx.mock
    async def test_url_encoded_title(self):
        respx.get("https://wiki.example.com/api.php").mock(
            return_value=httpx.Response(200, json=MEDIAWIKI_QUERY_RESPONSE)
        )

        result = await _detect_mediawiki("https://wiki.example.com/wiki/Ultima_VIII%20books")
        assert result is not None
        assert result["page_title"] == "Ultima_VIII books"

    @pytest.mark.asyncio
    @respx.mock
    async def test_network_timeout_returns_none(self):
        respx.get("https://wiki.example.com/api.php").mock(
            side_effect=httpx.ConnectTimeout("timeout")
        )
        respx.get("https://wiki.example.com/w/api.php").mock(
            side_effect=httpx.ConnectTimeout("timeout")
        )

        result = await _detect_mediawiki("https://wiki.example.com/wiki/Test_Page")
        assert result is None


# --- _clean_display_title ---

class TestCleanDisplayTitle:
    def test_strips_html_tags(self):
        assert _clean_display_title("<i>Ultima VIII</i> books") == "Ultima VIII books"

    def test_decodes_html_entities(self):
        assert _clean_display_title("Vol.&#160;II") == "Vol. II"

    def test_normalizes_nbsp(self):
        assert _clean_display_title("Vol.\u00a0II") == "Vol. II"

    def test_combined_tags_and_entities(self):
        assert _clean_display_title("<i>Ultima&#160;VIII</i> books") == "Ultima VIII books"

    def test_plain_title_unchanged(self):
        assert _clean_display_title("Test Page") == "Test Page"


# --- _fetch_mediawiki_page ---

class TestFetchMediawikiPage:
    @pytest.mark.asyncio
    @respx.mock
    async def test_full_page_fetch(self):
        respx.get("https://wiki.example.com/api.php").mock(
            return_value=httpx.Response(200, json=MEDIAWIKI_PARSE_FULL_RESPONSE)
        )

        result = await _fetch_mediawiki_page(
            "https://wiki.example.com/api.php", "Test_Page"
        )
        assert result is not None
        assert result["title"] == "Test Page"
        assert "Section One" in result["html"]
        assert "Section Two" in result["html"]
        assert len(result["sections_meta"]) == 2

    @pytest.mark.asyncio
    @respx.mock
    async def test_section_fetch_by_name(self):
        # First call returns section list, second returns section content
        route = respx.get("https://wiki.example.com/api.php")
        route.side_effect = [
            httpx.Response(200, json=MEDIAWIKI_PARSE_SECTIONS_RESPONSE),
            httpx.Response(200, json=MEDIAWIKI_PARSE_SECTION_TEXT),
        ]

        result = await _fetch_mediawiki_page(
            "https://wiki.example.com/api.php",
            "Test_Page",
            sections=["Section Two"],
        )
        assert result is not None
        assert "Section Two" in result["html"]

    @pytest.mark.asyncio
    @respx.mock
    async def test_section_fetch_strips_html_from_names(self):
        """Section names like '<i>Honor Lost</i>' should match plain 'Honor Lost'."""
        sections_resp = {
            "parse": {
                "displaytitle": "Test",
                "sections": [
                    {"index": "1", "line": "<i>Fancy Name</i>", "level": "2"},
                ],
            }
        }
        section_text_resp = {
            "parse": {"text": {"*": "<h2>Fancy Name</h2><p>Content.</p>"}}
        }

        route = respx.get("https://wiki.example.com/api.php")
        route.side_effect = [
            httpx.Response(200, json=sections_resp),
            httpx.Response(200, json=section_text_resp),
        ]

        result = await _fetch_mediawiki_page(
            "https://wiki.example.com/api.php",
            "Test_Page",
            sections=["Fancy Name"],
        )
        assert result is not None
        assert "Fancy Name" in result["html"]

    @pytest.mark.asyncio
    @respx.mock
    async def test_section_html_entity_nbsp_matches(self):
        """API section 'Vol.&nbsp;II' (HTML entity) should match request for 'Vol. II'."""
        sections_resp = {
            "parse": {
                "displaytitle": "Test",
                "sections": [
                    {"index": "1", "line": "Vol.&nbsp;II", "level": "2"},
                ],
            }
        }
        section_text_resp = {
            "parse": {"text": {"*": "<h2>Vol. II</h2><p>Volume two content.</p>"}}
        }

        route = respx.get("https://wiki.example.com/api.php")
        route.side_effect = [
            httpx.Response(200, json=sections_resp),
            httpx.Response(200, json=section_text_resp),
        ]

        result = await _fetch_mediawiki_page(
            "https://wiki.example.com/api.php",
            "Test_Page",
            sections=["Vol. II"],
        )
        assert result is not None
        assert "Volume two content" in result["html"]

    @pytest.mark.asyncio
    @respx.mock
    async def test_section_combined_html_tags_and_entities(self):
        """API section '<i>Parables, Vol.&nbsp;I</i>' should match 'Parables, Vol. I'."""
        sections_resp = {
            "parse": {
                "displaytitle": "Test",
                "sections": [
                    {"index": "1", "line": "<i>Parables, Vol.&nbsp;I</i>", "level": "2"},
                ],
            }
        }
        section_text_resp = {
            "parse": {"text": {"*": "<p>Parable content.</p>"}}
        }

        route = respx.get("https://wiki.example.com/api.php")
        route.side_effect = [
            httpx.Response(200, json=sections_resp),
            httpx.Response(200, json=section_text_resp),
        ]

        result = await _fetch_mediawiki_page(
            "https://wiki.example.com/api.php",
            "Test_Page",
            sections=["Parables, Vol. I"],
        )
        assert result is not None
        assert "Parable content" in result["html"]

    @pytest.mark.asyncio
    @respx.mock
    async def test_section_unicode_nbsp_matches(self):
        """API section with Unicode \\u00a0 should also match 'Vol. II'."""
        sections_resp = {
            "parse": {
                "displaytitle": "Test",
                "sections": [
                    {"index": "1", "line": "Vol.\u00a0II", "level": "2"},
                ],
            }
        }
        section_text_resp = {
            "parse": {"text": {"*": "<h2>Vol. II</h2><p>Volume two content.</p>"}}
        }

        route = respx.get("https://wiki.example.com/api.php")
        route.side_effect = [
            httpx.Response(200, json=sections_resp),
            httpx.Response(200, json=section_text_resp),
        ]

        result = await _fetch_mediawiki_page(
            "https://wiki.example.com/api.php",
            "Test_Page",
            sections=["Vol. II"],
        )
        assert result is not None
        assert "Volume two content" in result["html"]

    @pytest.mark.asyncio
    @respx.mock
    async def test_section_not_found_returns_empty_html(self):
        route = respx.get("https://wiki.example.com/api.php")
        route.side_effect = [
            httpx.Response(200, json=MEDIAWIKI_PARSE_SECTIONS_RESPONSE),
        ]

        result = await _fetch_mediawiki_page(
            "https://wiki.example.com/api.php",
            "Test_Page",
            sections=["Nonexistent Section"],
        )
        assert result is not None
        assert result["html"] == ""


# --- _mediawiki_html_to_markdown ---

class TestMediawikiHtmlToMarkdown:
    def test_basic_conversion(self):
        html = "<h2>Title</h2><p>Some content here.</p>"
        result = _mediawiki_html_to_markdown(html)
        assert "Title" in result
        assert "Some content here." in result

    def test_removes_edit_sections(self):
        html = '<h2>Title <span class="mw-editsection">[edit]</span></h2><p>Content.</p>'
        result = _mediawiki_html_to_markdown(html)
        assert "[edit]" not in result
        assert "mw-editsection" not in result

    def test_removes_toc(self):
        html = '<div id="toc"><h2>Contents</h2></div><h2>Real</h2><p>Content.</p>'
        result = _mediawiki_html_to_markdown(html)
        assert "Contents" not in result
        assert "Real" in result

    def test_removes_toc_class(self):
        html = '<div class="toc"><h2>Contents</h2></div><p>Content.</p>'
        result = _mediawiki_html_to_markdown(html)
        assert "Contents" not in result

    def test_removes_scripts_and_styles(self):
        html = '<script>alert("x")</script><style>.x{}</style><p>Content.</p>'
        result = _mediawiki_html_to_markdown(html)
        assert "alert" not in result
        assert ".x{}" not in result
        assert "Content." in result

    def test_collapses_extra_newlines(self):
        html = "<p>A</p><br><br><br><br><p>B</p>"
        result = _mediawiki_html_to_markdown(html)
        assert "\n\n\n" not in result
