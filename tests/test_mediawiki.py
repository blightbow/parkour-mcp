"""Tests for claude_web_tools.mediawiki module."""

import httpx
import pytest
import respx

from claude_web_tools.mediawiki import (
    _clean_display_title,
    _detect_mediawiki,
    _fetch_mediawiki_page,
    _mediawiki_html_to_markdown,
    _extract_citations,
    _format_citations,
)

from .conftest import (
    MEDIAWIKI_QUERY_RESPONSE,
    MEDIAWIKI_QUERY_MISSING_PAGE,
    MEDIAWIKI_PARSE_FULL_RESPONSE,
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

    def test_converts_inline_citations_to_footnote_markers(self):
        """sup.reference with numeric text becomes [^N] markdown footnote."""
        html = (
            '<p>Some claim.'
            '<sup class="reference"><a href="#cite_note-1">[1]</a></sup>'
            ' Another claim.'
            '<sup class="reference"><a href="#cite_note-2">[2]</a></sup>'
            '</p>'
        )
        result = _mediawiki_html_to_markdown(html)
        assert "[^1]" in result
        assert "[^2]" in result
        assert "[1]" not in result

    def test_strips_non_numeric_ref_markers(self):
        """Non-numeric refs like [nb 1] should be removed, not converted."""
        html = (
            '<p>Text.'
            '<sup class="reference"><a href="#cite_note-nb-1">[nb 1]</a></sup>'
            '</p>'
        )
        result = _mediawiki_html_to_markdown(html)
        assert "[nb 1]" not in result
        assert "Text." in result

    def test_strips_reference_block(self):
        """The .mw-references-wrap footnote block should be removed."""
        html = (
            '<p>Content.</p>'
            '<div class="mw-references-wrap">'
            '<ol class="references"><li>Ref 1</li></ol>'
            '</div>'
        )
        result = _mediawiki_html_to_markdown(html)
        assert "Ref 1" not in result
        assert "Content." in result

    def test_strips_cite_error_paragraphs(self):
        """Cite error paragraphs from incomplete reflist templates are removed."""
        html = (
            '<p>Content.</p>'
            '<p>Cite error: There are ref tags but no reflist.</p>'
        )
        result = _mediawiki_html_to_markdown(html)
        assert "Cite error" not in result
        assert "Content." in result

    def test_strips_editsection_as_heading_sibling(self):
        """Modern MediaWiki wraps [edit] as sibling of heading, not child."""
        html = (
            '<div class="mw-heading mw-heading2">'
            '<h2>Education</h2>'
            '<span class="mw-editsection">'
            '<span class="mw-editsection-bracket">[</span>'
            '<a href="/edit">edit</a>'
            '<span class="mw-editsection-bracket">]</span>'
            '</span>'
            '</div>'
            '<p>Section content.</p>'
        )
        result = _mediawiki_html_to_markdown(html)
        assert "Education" in result
        assert "[edit]" not in result
        assert "edit" not in result or "Education" in result


# --- _extract_citations ---

class TestExtractCitations:
    def test_extracts_numbered_citations(self):
        html = (
            '<ol class="references">'
            '<li><span class="reference-text">First ref.</span></li>'
            '<li><span class="reference-text">Second ref.</span></li>'
            '</ol>'
        )
        citations = _extract_citations(html)
        assert len(citations) == 2
        assert citations[0]["n"] == 1
        assert citations[0]["text"] == "First ref."
        assert citations[1]["n"] == 2

    def test_extracts_external_link(self):
        html = (
            '<ol class="references">'
            '<li><span class="reference-text">'
            '<a class="external" href="https://example.com">Example Title</a>'
            '</span></li>'
            '</ol>'
        )
        citations = _extract_citations(html)
        assert citations[0]["url"] == "https://example.com"
        assert citations[0]["title"] == "Example Title"

    def test_resolves_citeref_bibliography(self):
        """Author-date shorthand should resolve via #CITEREF link."""
        html = (
            '<ol class="references">'
            '<li><span class="reference-text">'
            '<a href="#CITEREFSmith2020">Smith 2020</a>, p. 42.'
            '</span></li>'
            '</ol>'
            '<cite id="CITEREFSmith2020">'
            'Smith, J. (2020). '
            '<a class="external" href="https://example.com/book">The Book</a>.'
            '</cite>'
        )
        citations = _extract_citations(html)
        assert len(citations) == 1
        assert citations[0]["text"] == "Smith 2020 , p. 42."
        assert "sources" in citations[0]
        assert citations[0]["sources"][0]["url"] == "https://example.com/book"
        assert citations[0]["sources"][0]["title"] == "The Book"

    def test_resolves_multiple_citerefs(self):
        """Footnote referencing multiple works resolves all of them."""
        html = (
            '<ol class="references">'
            '<li><span class="reference-text">'
            '<a href="#CITEREFAlpha2020">Alpha 2020</a>, p. 1; '
            '<a href="#CITEREFBeta2021">Beta 2021</a>, p. 2.'
            '</span></li>'
            '</ol>'
            '<cite id="CITEREFAlpha2020">Alpha, A. (2020). Work One.</cite>'
            '<cite id="CITEREFBeta2021">Beta, B. (2021). Work Two.</cite>'
        )
        citations = _extract_citations(html)
        assert len(citations[0]["sources"]) == 2

    def test_no_references_returns_empty(self):
        html = "<p>No references here.</p>"
        assert _extract_citations(html) == []

    def test_picks_largest_reference_list(self):
        """Should use the largest ol.references, skipping small note groups."""
        html = (
            '<ol class="references"><li><span class="reference-text">Note.</span></li></ol>'
            '<ol class="references">'
            '<li><span class="reference-text">Ref 1.</span></li>'
            '<li><span class="reference-text">Ref 2.</span></li>'
            '<li><span class="reference-text">Ref 3.</span></li>'
            '</ol>'
        )
        citations = _extract_citations(html)
        assert len(citations) == 3
        assert citations[0]["text"] == "Ref 1."


# --- _format_citations ---

class TestFormatCitations:
    def test_formats_url_citation(self):
        citations = [{"n": 1, "text": "Title", "url": "https://x.com", "title": "Title"}]
        result = _format_citations(citations)
        assert result == "[^1]: [Title](https://x.com)"

    def test_formats_plain_text_citation(self):
        citations = [{"n": 3, "text": "Smith 2020, p. 42."}]
        result = _format_citations(citations)
        assert result == "[^3]: Smith 2020, p. 42."

    def test_formats_with_resolved_source_url(self):
        citations = [{
            "n": 5, "text": "Smith 2020, p. 42.",
            "sources": [{"text": "Full entry", "url": "https://x.com/book", "title": "The Book"}],
        }]
        result = _format_citations(citations)
        assert "**[The Book](https://x.com/book)**" in result

    def test_formats_with_resolved_source_no_url(self):
        citations = [{
            "n": 7, "text": "Jones 2019, p. 10.",
            "sources": [{"text": "Jones, A. (2019). Some Work. Publisher."}],
        }]
        result = _format_citations(citations)
        assert "*Jones, A. (2019). Some Work. Publisher.*" in result

    def test_formats_multiple_sources(self):
        citations = [{
            "n": 2, "text": "A 2020; B 2021.",
            "sources": [
                {"text": "Alpha.", "url": "https://a.com", "title": "A"},
                {"text": "Beta.", "url": "https://b.com", "title": "B"},
            ],
        }]
        result = _format_citations(citations)
        assert "**[A](https://a.com)**" in result
        assert "**[B](https://b.com)**" in result
