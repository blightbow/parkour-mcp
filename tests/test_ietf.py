"""Tests for parkour_mcp.ietf module."""

import pytest
import respx

from parkour_mcp.ietf import (
    _fetch_rfc_metadata,
    _fetch_rfc_paper,
    _format_rfc_paper,
    _format_rfc_list,
    _resolve_subseries,
    _search_rfcs,
    _subseries_label,
    _RFC_DOI_RE,
    ietf,
)
from parkour_mcp.detection import _detect_ietf_url
from parkour_mcp._pipeline import _ietf_fast_path
from parkour_mcp.shelf import _reset_shelf, _get_shelf


# ---------------------------------------------------------------------------
# Test fixtures — RFC Editor JSON
# ---------------------------------------------------------------------------

RFC_9110_META = {
    "draft": "draft-ietf-httpbis-semantics-19",
    "doc_id": "RFC9110",
    "title": "HTTP Semantics",
    "authors": ["R. Fielding, Ed.", "M. Nottingham, Ed.", "J. Reschke, Ed."],
    "format": ["HTML", "TEXT", "PDF", "XML"],
    "page_count": "194",
    "pub_status": "INTERNET STANDARD",
    "status": "INTERNET STANDARD",
    "source": "HTTP",
    "abstract": "The Hypertext Transfer Protocol (HTTP) is a stateless application-level protocol.",
    "pub_date": "June 2022",
    "keywords": ["Hypertext Transfer Protocol", "HTTP"],
    "obsoletes": ["RFC2818", "RFC7230", "RFC7231"],
    "obsoleted_by": [],
    "updates": ["RFC3864"],
    "updated_by": [],
    "see_also": ["STD0097"],
    "doi": "10.17487/RFC9110",
    "errata_url": "https://www.rfc-editor.org/errata/rfc9110",
}

RFC_1_META = {
    "draft": "",
    "doc_id": "RFC0001",
    "title": "Host Software",
    "authors": ["S. Crocker"],
    "format": ["ASCII", "HTML"],
    "page_count": "11",
    "pub_status": "UNKNOWN",
    "status": "UNKNOWN",
    "source": "Legacy",
    "abstract": "",
    "pub_date": "April 1969",
    "keywords": [""],
    "obsoletes": [],
    "obsoleted_by": [],
    "updates": [],
    "updated_by": [],
    "see_also": [],
    "doi": "10.17487/RFC0001",
    "errata_url": None,
}

# BibXML response for BCP 14 (two member RFCs)
BCP_14_BIBXML = """\
<?xml version='1.0' encoding='UTF-8'?>
<referencegroup anchor="BCP0014" target="https://www.rfc-editor.org/info/bcp14">
  <reference anchor="RFC2119" target="https://www.rfc-editor.org/info/rfc2119">
    <front>
      <title>Key words for use in RFCs to Indicate Requirement Levels</title>
      <author fullname="S. Bradner" initials="S." surname="Bradner"/>
      <date month="March" year="1997"/>
    </front>
    <seriesInfo name="BCP" value="14"/>
    <seriesInfo name="RFC" value="2119"/>
    <seriesInfo name="DOI" value="10.17487/RFC2119"/>
  </reference>
  <reference anchor="RFC8174" target="https://www.rfc-editor.org/info/rfc8174">
    <front>
      <title>Ambiguity of Uppercase vs Lowercase in RFC 2119 Key Words</title>
      <author fullname="B. Leiba" initials="B." surname="Leiba"/>
      <date month="May" year="2017"/>
    </front>
    <seriesInfo name="BCP" value="14"/>
    <seriesInfo name="RFC" value="8174"/>
    <seriesInfo name="DOI" value="10.17487/RFC8174"/>
  </reference>
</referencegroup>
"""

# Datatracker search response (Tastypie envelope)
DATATRACKER_SEARCH_RESPONSE = {
    "meta": {"limit": 10, "offset": 0, "total_count": 2},
    "objects": [
        {"name": "rfc9110", "title": "HTTP Semantics", "pages": 194},
        {"name": "rfc9111", "title": "HTTP Caching", "pages": 113},
    ],
}


# ---------------------------------------------------------------------------
# URL detection tests
# ---------------------------------------------------------------------------

class TestDetectIetfUrl:
    def test_rfc_editor_json(self):
        result = _detect_ietf_url("https://www.rfc-editor.org/rfc/rfc9110.json")
        assert result == {"type": "rfc", "number": 9110}

    def test_rfc_editor_html_falls_through(self):
        # Body-text suffixes are intentionally NOT intercepted so the generic
        # HTTP+markdown pipeline handles section= and search= against the
        # rendered RFC text.  See parkour-mcp#7.
        assert _detect_ietf_url("https://www.rfc-editor.org/rfc/rfc768.html") is None

    def test_rfc_editor_txt_falls_through(self):
        assert _detect_ietf_url("https://www.rfc-editor.org/rfc/rfc9110.txt") is None

    def test_rfc_editor_xml_falls_through(self):
        assert _detect_ietf_url("https://www.rfc-editor.org/rfc/rfc9110.xml") is None

    def test_rfc_editor_pdf_falls_through(self):
        assert _detect_ietf_url("https://www.rfc-editor.org/rfc/rfc9110.pdf") is None

    def test_rfc_editor_bare(self):
        result = _detect_ietf_url("https://www.rfc-editor.org/rfc/rfc1")
        assert result == {"type": "rfc", "number": 1}

    def test_rfc_editor_trailing_slash(self):
        result = _detect_ietf_url("https://www.rfc-editor.org/rfc/rfc9110/")
        assert result == {"type": "rfc", "number": 9110}

    def test_datatracker_rfc(self):
        result = _detect_ietf_url("https://datatracker.ietf.org/doc/rfc9110/")
        assert result == {"type": "rfc", "number": 9110}

    def test_datatracker_draft(self):
        result = _detect_ietf_url(
            "https://datatracker.ietf.org/doc/draft-ietf-httpbis-semantics/"
        )
        assert result == {"type": "draft", "name": "draft-ietf-httpbis-semantics"}

    def test_non_ietf_url(self):
        assert _detect_ietf_url("https://example.com/rfc9110") is None

    def test_arxiv_url_not_matched(self):
        assert _detect_ietf_url("https://arxiv.org/abs/1706.03762") is None


class TestRfcDoiRegex:
    def test_matches_rfc_doi(self):
        m = _RFC_DOI_RE.match("10.17487/RFC9110")
        assert m is not None
        assert m.group(1) == "9110"

    def test_matches_padded_doi(self):
        m = _RFC_DOI_RE.match("10.17487/RFC0768")
        assert m is not None
        assert m.group(1) == "0768"

    def test_case_insensitive(self):
        m = _RFC_DOI_RE.match("10.17487/rfc9110")
        assert m is not None

    def test_no_match_arxiv_doi(self):
        assert _RFC_DOI_RE.match("10.48550/arXiv.2501.16496") is None


# ---------------------------------------------------------------------------
# Metadata fetch tests
# ---------------------------------------------------------------------------

class TestFetchRfcMetadata:
    @respx.mock
    @pytest.mark.asyncio
    async def test_success(self):
        respx.get("https://www.rfc-editor.org/rfc/rfc9110.json").respond(
            200, json=RFC_9110_META,
        )
        result = await _fetch_rfc_metadata(9110)
        assert result is not None
        assert result["title"] == "HTTP Semantics"
        assert result["status"] == "INTERNET STANDARD"

    @respx.mock
    @pytest.mark.asyncio
    async def test_not_found(self):
        respx.get("https://www.rfc-editor.org/rfc/rfc99999.json").respond(404)
        result = await _fetch_rfc_metadata(99999)
        assert result is None


# ---------------------------------------------------------------------------
# Format tests
# ---------------------------------------------------------------------------

class TestFormatRfcPaper:
    def test_basic_format(self):
        body = _format_rfc_paper(RFC_9110_META)
        assert "# RFC 9110: HTTP Semantics" in body
        assert "R. Fielding" in body
        assert "June 2022" in body
        assert "INTERNET STANDARD" in body
        assert "RFC2818" in body  # obsoletes chain
        assert "RFC3864" in body  # updates chain

    def test_unknown_status_rfc(self):
        body = _format_rfc_paper(RFC_1_META)
        assert "# RFC 1: Host Software" in body
        assert "UNKNOWN" in body


class TestFormatRfcList:
    def test_list_format(self):
        results = DATATRACKER_SEARCH_RESPONSE["objects"]
        assert isinstance(results, list)
        body = _format_rfc_list(results, total=2, offset=0)
        assert "1. **RFC 9110**: HTTP Semantics" in body
        assert "2. **RFC 9111**: HTTP Caching" in body

    def test_pagination_hint(self):
        results = DATATRACKER_SEARCH_RESPONSE["objects"]
        assert isinstance(results, list)
        body = _format_rfc_list(results, total=42, offset=0)
        assert "40 more results available" in body


# ---------------------------------------------------------------------------
# Paper fetch with shelf tracking
# ---------------------------------------------------------------------------

class TestFetchRfcPaper:
    @pytest.fixture(autouse=True)
    def reset_shelf(self):
        _reset_shelf()
        yield
        _reset_shelf()

    @respx.mock
    @pytest.mark.asyncio
    async def test_basic_paper(self):
        respx.get("https://www.rfc-editor.org/rfc/rfc9110.json").respond(
            200, json=RFC_9110_META,
        )
        # Mock DOI citation fetch (fire-and-forget, may fail)
        respx.route(method="GET", host="doi.org").respond(404)
        respx.route(method="GET", host="data.crossref.org").respond(404)

        result = await _fetch_rfc_paper(9110)
        assert "HTTP Semantics" in result
        assert "IETF (RFC Editor)" in result
        assert "10.17487/RFC9110" in result
        assert "STD 97" in result  # subseries label
        assert "trust:" in result  # fenced RFC body must declare its untrust

        # Verify shelf tracking
        shelf = _get_shelf()
        records = await shelf.list_all()
        assert len(records) == 1
        assert records[0].doi == "10.17487/RFC9110"
        assert records[0].source_tool == "ietf"

    @respx.mock
    @pytest.mark.asyncio
    async def test_unknown_pub_status_note(self):
        respx.get("https://www.rfc-editor.org/rfc/rfc1.json").respond(
            200, json=RFC_1_META,
        )
        respx.route(method="GET", host="doi.org").respond(404)
        respx.route(method="GET", host="data.crossref.org").respond(404)

        result = await _fetch_rfc_paper(1)
        assert "predates the current status system" in result

    @respx.mock
    @pytest.mark.asyncio
    async def test_not_found(self):
        respx.get("https://www.rfc-editor.org/rfc/rfc99999.json").respond(404)
        result = await _fetch_rfc_paper(99999)
        assert "Error" in result


# ---------------------------------------------------------------------------
# Search tests
# ---------------------------------------------------------------------------

class TestSearchRfcs:
    @respx.mock
    @pytest.mark.asyncio
    async def test_search(self):
        respx.get("https://datatracker.ietf.org/api/v1/doc/document/").respond(
            200, json=DATATRACKER_SEARCH_RESPONSE,
        )
        results, total = await _search_rfcs("HTTP")
        assert len(results) == 2
        assert total == 2


# ---------------------------------------------------------------------------
# Subseries resolution tests
# ---------------------------------------------------------------------------

class TestResolveSubseries:
    @respx.mock
    @pytest.mark.asyncio
    async def test_bcp14_resolution(self):
        respx.get(
            "https://bib.ietf.org/public/rfc/bibxml9/reference.BCP.0014.xml"
        ).respond(200, text=BCP_14_BIBXML)

        result = await _resolve_subseries("BCP14")
        assert result is not None
        assert "BCP 14" in result
        assert "RFC 2119" in result
        assert "RFC 8174" in result
        assert "S. Bradner" in result

    @respx.mock
    @pytest.mark.asyncio
    async def test_zero_padded_input(self):
        respx.get(
            "https://bib.ietf.org/public/rfc/bibxml9/reference.STD.0097.xml"
        ).respond(200, text=BCP_14_BIBXML)  # reuse fixture for structure

        result = await _resolve_subseries("STD0097")
        assert result is not None

    @respx.mock
    @pytest.mark.asyncio
    async def test_not_found(self):
        respx.get(
            "https://bib.ietf.org/public/rfc/bibxml9/reference.STD.9999.xml"
        ).respond(404)

        result = await _resolve_subseries("STD9999")
        assert result is None

    @pytest.mark.asyncio
    async def test_invalid_input(self):
        result = await _resolve_subseries("not-a-subseries")
        assert result is None


class TestSubseriesLabel:
    def test_extracts_std(self):
        assert _subseries_label(["STD0097"]) == "STD 97"

    def test_extracts_bcp(self):
        assert _subseries_label(["BCP0014"]) == "BCP 14"

    def test_no_subseries(self):
        assert _subseries_label([]) is None

    def test_ignores_non_subseries(self):
        assert _subseries_label(["RFC9110"]) is None


# ---------------------------------------------------------------------------
# Tool action dispatch tests
# ---------------------------------------------------------------------------

class TestIetfTool:
    @respx.mock
    @pytest.mark.asyncio
    async def test_rfc_action_by_number(self):
        respx.get("https://www.rfc-editor.org/rfc/rfc9110.json").respond(
            200, json=RFC_9110_META,
        )
        respx.route(method="GET", host="doi.org").respond(404)
        respx.route(method="GET", host="data.crossref.org").respond(404)
        _reset_shelf()

        result = await ietf(action="rfc", query="9110")
        assert "HTTP Semantics" in result
        _reset_shelf()

    @respx.mock
    @pytest.mark.asyncio
    async def test_rfc_action_by_url(self):
        respx.get("https://www.rfc-editor.org/rfc/rfc768.json").respond(
            200, json=RFC_1_META,  # reuse for structure
        )
        respx.route(method="GET", host="doi.org").respond(404)
        respx.route(method="GET", host="data.crossref.org").respond(404)
        _reset_shelf()

        result = await ietf(
            action="rfc",
            query="https://www.rfc-editor.org/rfc/rfc768",
        )
        assert "IETF" in result
        _reset_shelf()

    @respx.mock
    @pytest.mark.asyncio
    async def test_search_action(self):
        respx.get("https://datatracker.ietf.org/api/v1/doc/document/").respond(
            200, json=DATATRACKER_SEARCH_RESPONSE,
        )
        result = await ietf(action="search", query="HTTP")
        assert "RFC 9110" in result
        assert "Datatracker" in result

    @respx.mock
    @pytest.mark.asyncio
    async def test_subseries_action(self):
        respx.get(
            "https://bib.ietf.org/public/rfc/bibxml9/reference.BCP.0014.xml"
        ).respond(200, text=BCP_14_BIBXML)

        result = await ietf(action="subseries", query="BCP14")
        assert "BCP 14" in result

    @pytest.mark.asyncio
    async def test_unknown_action(self):
        result = await ietf(action="invalid", query="test")
        assert "Error" in result
        assert "Unknown action" in result

    @pytest.mark.asyncio
    async def test_invalid_rfc_query(self):
        result = await ietf(action="rfc", query="not-an-rfc")
        assert "Error" in result


# ---------------------------------------------------------------------------
# Fast-path handler tests
# ---------------------------------------------------------------------------

class TestIetfFastPath:
    @respx.mock
    @pytest.mark.asyncio
    async def test_rfc_editor_url(self):
        respx.get("https://www.rfc-editor.org/rfc/rfc9110.json").respond(
            200, json=RFC_9110_META,
        )
        respx.route(method="GET", host="doi.org").respond(404)
        respx.route(method="GET", host="data.crossref.org").respond(404)
        _reset_shelf()

        result = await _ietf_fast_path("https://www.rfc-editor.org/rfc/rfc9110.json")
        assert result is not None
        assert "HTTP Semantics" in result
        _reset_shelf()

    @pytest.mark.asyncio
    async def test_non_ietf_url_returns_none(self):
        result = await _ietf_fast_path("https://example.com/something")
        assert result is None


# ---------------------------------------------------------------------------
# DOI delegation test (verifies doi.py routes RFC DOIs to IETF handler)
# ---------------------------------------------------------------------------

class TestDoiDelegation:
    @pytest.fixture(autouse=True)
    def reset_shelf(self):
        _reset_shelf()
        yield
        _reset_shelf()

    @respx.mock
    @pytest.mark.asyncio
    async def test_rfc_doi_delegates_to_ietf(self):
        """Verify that _fetch_doi_paper delegates 10.17487/RFC* to IETF handler."""
        from parkour_mcp.doi import _fetch_doi_paper

        respx.get("https://www.rfc-editor.org/rfc/rfc9110.json").respond(
            200, json=RFC_9110_META,
        )
        respx.route(method="GET", host="doi.org").respond(404)
        respx.route(method="GET", host="data.crossref.org").respond(404)

        result = await _fetch_doi_paper("10.17487/RFC9110")
        assert "IETF (RFC Editor)" in result
        assert "HTTP Semantics" in result
