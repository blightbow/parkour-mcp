"""Tests for claude_web_tools.fetch_direct module."""

import httpx
import pytest
import respx

from claude_web_tools.fetch_direct import (
    web_fetch_direct,
    web_fetch_sections,
    _extract_text_spans,
    _build_document_xml,
)
from bs4 import BeautifulSoup

from .conftest import (
    SAMPLE_HTML_PAGE,
    SAMPLE_JSON_CONTENT,
    SAMPLE_PLAIN_TEXT,
    MEDIAWIKI_QUERY_RESPONSE,
    MEDIAWIKI_PARSE_FULL_RESPONSE,
)


# --- _extract_text_spans ---

class TestExtractTextSpans:
    def test_extracts_paragraphs(self):
        soup = BeautifulSoup(SAMPLE_HTML_PAGE, "html.parser")
        spans = _extract_text_spans(soup)
        assert len(spans) > 0
        assert any("paragraph" in s for s in spans)

    def test_skips_short_fragments(self):
        html = "<body><p>Short</p><p>This is a much longer paragraph that should be included.</p></body>"
        soup = BeautifulSoup(html, "html.parser")
        spans = _extract_text_spans(soup)
        assert not any(s == "Short" for s in spans)

    def test_removes_script_style_nav(self):
        html = """<body>
        <script>var x = 1;</script>
        <style>.foo {}</style>
        <nav><a href="/">Long enough nav link text here</a></nav>
        <p>This is the actual content that should be extracted from the page.</p>
        </body>"""
        soup = BeautifulSoup(html, "html.parser")
        spans = _extract_text_spans(soup)
        assert not any("var x" in s for s in spans)
        assert not any(".foo" in s for s in spans)
        assert any("actual content" in s for s in spans)

    def test_deduplicates_nested_elements(self):
        html = """<body>
        <div><p>This is a paragraph that appears inside a div element container.</p></div>
        </body>"""
        soup = BeautifulSoup(html, "html.parser")
        spans = _extract_text_spans(soup)
        # The text should appear only once despite div+p nesting
        matching = [s for s in spans if "paragraph that appears" in s]
        assert len(matching) == 1

    def test_prefers_main_content(self):
        html = """<body>
        <aside><p>Sidebar content that is long enough to be extracted normally.</p></aside>
        <main><p>Main content that should be the primary extraction target here.</p></main>
        </body>"""
        soup = BeautifulSoup(html, "html.parser")
        spans = _extract_text_spans(soup)
        assert any("Main content" in s for s in spans)
        assert not any("Sidebar" in s for s in spans)


# --- _build_document_xml ---

class TestBuildDocumentXml:
    def test_basic_structure(self):
        xml = _build_document_xml("Title", ["Span one", "Span two"], "http://example.com", "text/html")
        assert '<document index="1">' in xml
        assert "<source>Title</source>" in xml
        assert '<span index="1-1">Span one</span>' in xml
        assert '<span index="1-2">Span two</span>' in xml
        assert 'key="destination_url"' in xml
        assert 'key="mime_type"' in xml

    def test_xml_escaping(self):
        xml = _build_document_xml("A & B", ["<script>"], "http://x.com?a=1&b=2", "text/html")
        assert "A &amp; B" in xml
        assert "&lt;script&gt;" in xml
        assert "a=1&amp;b=2" in xml

    def test_custom_doc_index(self):
        xml = _build_document_xml("T", ["S"], "http://x.com", "text/html", doc_index=3)
        assert '<document index="3">' in xml
        assert '<span index="3-1">' in xml


# --- web_fetch_direct ---

class TestWebFetchDirectCiteTrue:
    """Tests for the legacy cite=True (XML) path."""

    @pytest.mark.asyncio
    @respx.mock
    async def test_html_cite_returns_xml(self):
        respx.get("https://example.com/page").mock(
            return_value=httpx.Response(
                200,
                text=SAMPLE_HTML_PAGE,
                headers={"content-type": "text/html"},
            )
        )

        result = await web_fetch_direct("https://example.com/page", cite=True)
        assert "<document" in result
        assert "<source>" in result
        assert "<span" in result

    @pytest.mark.asyncio
    @respx.mock
    async def test_json_cite_returns_xml(self):
        respx.get("https://example.com/data.json").mock(
            return_value=httpx.Response(
                200,
                text=SAMPLE_JSON_CONTENT,
                headers={"content-type": "application/json"},
            )
        )

        result = await web_fetch_direct("https://example.com/data.json", cite=True)
        assert "<document" in result
        assert "key" in result

    @pytest.mark.asyncio
    @respx.mock
    async def test_cite_truncation(self):
        long_text = "x" * 10000
        html = f"<html><body><p>{long_text}</p></body></html>"
        respx.get("https://example.com/big").mock(
            return_value=httpx.Response(
                200, text=html, headers={"content-type": "text/html"}
            )
        )

        result = await web_fetch_direct("https://example.com/big", cite=True, max_tokens=100)
        assert "<!-- truncated:" in result


class TestWebFetchDirectMarkdown:
    """Tests for the default cite=False (markdown) path."""

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
        assert "title: Main Heading" in result
        assert "source:" in result
        assert "Main Heading" in result
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
        assert "section:" in result

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
        assert "sections:" in result
        assert "[content truncated]" in result

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
        assert "[content truncated]" in result


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
        assert "title: Test Page" in result
        assert "site: Test Wiki" in result
        assert "generator: MediaWiki" in result

    @pytest.mark.asyncio
    @respx.mock
    async def test_wiki_cite_true_skips_api(self):
        """cite=True should skip the MediaWiki fast path."""
        respx.get("https://wiki.example.com/wiki/Test_Page").mock(
            return_value=httpx.Response(
                200,
                text=SAMPLE_HTML_PAGE,
                headers={"content-type": "text/html"},
            )
        )

        result = await web_fetch_direct(
            "https://wiki.example.com/wiki/Test_Page", cite=True
        )
        assert "<document" in result

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
        assert "title:" in result
        assert "Main Heading" in result

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
        assert "section: Second Section" in result

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
        assert "sections:" in result


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
        assert "section: Second Section" in result
        assert 'matched_fragment: "#second-section"' in result
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
        assert "section: Second Section" in result
        assert "matched_fragment" not in result


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
        assert "sections:" in result
        assert "(#main-heading)" in result
        assert "(#second-section)" in result
        assert "(#subsection)" in result
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
        assert "section: Second Section" in result
        assert 'matched_fragment: "#second-section"' in result
        # Full tree should still be shown for context
        assert "sections:" in result

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
        assert "sections:" in result

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
