"""Tests for parkour_mcp.arxiv module."""

import sys

import httpx
import pytest
import respx

import parkour_mcp.arxiv  # noqa: F401

_arxiv_module = sys.modules["parkour_mcp.arxiv"]

from parkour_mcp._pipeline import _arxiv_fast_path  # noqa: E402
from parkour_mcp.arxiv import (  # noqa: E402
    _ARXIV_RETRY_AFTER_CAP,
    ARXIV_API_URL,
    _arxiv_request,
    _fetch_arxiv_paper,
    _format_arxiv_list,
    _format_arxiv_paper,
    _parse_arxiv_entry,
    _retry_delay,
    arxiv,
)
from parkour_mcp.detection import _detect_arxiv_url, _strip_version  # noqa: E402

# ---------------------------------------------------------------------------
# Atom XML test fixtures
# ---------------------------------------------------------------------------

ARXIV_SINGLE_ENTRY_XML = """\
<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom"
      xmlns:arxiv="http://arxiv.org/schemas/atom"
      xmlns:opensearch="http://a9.com/-/spec/opensearch/1.1/">
  <opensearch:totalResults>1</opensearch:totalResults>
  <opensearch:startIndex>0</opensearch:startIndex>
  <entry>
    <id>http://arxiv.org/abs/1706.03762v7</id>
    <title>Attention Is All You Need</title>
    <summary>The dominant sequence transduction models are based on complex recurrent or convolutional neural networks that include an encoder and a decoder.</summary>
    <published>2017-06-12T17:57:34Z</published>
    <updated>2023-08-02T01:18:13Z</updated>
    <author>
      <name>Ashish Vaswani</name>
      <arxiv:affiliation>Google Brain</arxiv:affiliation>
    </author>
    <author>
      <name>Noam Shazeer</name>
      <arxiv:affiliation>Google Brain</arxiv:affiliation>
    </author>
    <author>
      <name>Niki Parmar</name>
    </author>
    <arxiv:doi>10.5555/3295222.3295349</arxiv:doi>
    <arxiv:journal_ref>Advances in Neural Information Processing Systems 30 (NIPS 2017)</arxiv:journal_ref>
    <arxiv:comment>15 pages, 5 figures</arxiv:comment>
    <arxiv:primary_category term="cs.CL"/>
    <category term="cs.CL"/>
    <category term="cs.AI"/>
    <category term="cs.LG"/>
    <link href="http://arxiv.org/abs/1706.03762v7" rel="alternate" type="text/html"/>
    <link href="http://arxiv.org/pdf/1706.03762v7" rel="related" type="application/pdf" title="pdf"/>
  </entry>
</feed>
"""

ARXIV_MULTI_ENTRY_XML = """\
<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom"
      xmlns:arxiv="http://arxiv.org/schemas/atom"
      xmlns:opensearch="http://a9.com/-/spec/opensearch/1.1/">
  <opensearch:totalResults>2</opensearch:totalResults>
  <opensearch:startIndex>0</opensearch:startIndex>
  <entry>
    <id>http://arxiv.org/abs/1706.03762v7</id>
    <title>Attention Is All You Need</title>
    <summary>Transformers paper abstract.</summary>
    <published>2017-06-12T17:57:34Z</published>
    <updated>2023-08-02T01:18:13Z</updated>
    <author><name>Ashish Vaswani</name></author>
    <author><name>Noam Shazeer</name></author>
    <arxiv:primary_category term="cs.CL"/>
    <category term="cs.CL"/>
  </entry>
  <entry>
    <id>http://arxiv.org/abs/1810.04805v2</id>
    <title>BERT: Pre-training of Deep Bidirectional Transformers for Language Understanding</title>
    <summary>BERT abstract.</summary>
    <published>2018-10-11T00:00:00Z</published>
    <updated>2019-05-24T00:00:00Z</updated>
    <author><name>Jacob Devlin</name></author>
    <arxiv:primary_category term="cs.CL"/>
    <category term="cs.CL"/>
  </entry>
</feed>
"""

ARXIV_EMPTY_XML = """\
<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom"
      xmlns:opensearch="http://a9.com/-/spec/opensearch/1.1/">
  <opensearch:totalResults>0</opensearch:totalResults>
  <opensearch:startIndex>0</opensearch:startIndex>
</feed>
"""


@pytest.fixture(autouse=True)
def _disable_arxiv_rate_limit(monkeypatch):
    """Disable the 3s rate limiter in unit tests."""
    monkeypatch.setattr(_arxiv_module._arxiv_limiter, "min_interval", 0.0)


# ---------------------------------------------------------------------------
# _detect_arxiv_url
# ---------------------------------------------------------------------------

class TestDetectArxivUrl:
    def test_abs_url(self):
        assert _detect_arxiv_url("https://arxiv.org/abs/1706.03762") == "1706.03762"

    def test_pdf_url(self):
        assert _detect_arxiv_url("https://arxiv.org/pdf/1706.03762") == "1706.03762"

    def test_versioned_url(self):
        assert _detect_arxiv_url("https://arxiv.org/abs/1706.03762v7") == "1706.03762v7"

    def test_export_subdomain(self):
        assert _detect_arxiv_url("https://export.arxiv.org/abs/1706.03762") == "1706.03762"

    def test_http_scheme(self):
        assert _detect_arxiv_url("http://arxiv.org/abs/1706.03762") == "1706.03762"

    def test_html_url_returns_none(self):
        """HTML URLs must NOT be intercepted — they serve full rendered papers."""
        assert _detect_arxiv_url("https://arxiv.org/html/1706.03762") is None

    def test_non_arxiv_url(self):
        assert _detect_arxiv_url("https://example.com/paper/1706.03762") is None

    def test_s2_url(self):
        assert _detect_arxiv_url("https://www.semanticscholar.org/paper/204e3073") is None

    def test_five_digit_id(self):
        assert _detect_arxiv_url("https://arxiv.org/abs/2301.12345") == "2301.12345"

    def test_url_with_query_params(self):
        assert _detect_arxiv_url("https://arxiv.org/abs/1706.03762?context=cs.CL") == "1706.03762"


# ---------------------------------------------------------------------------
# _strip_version
# ---------------------------------------------------------------------------

class TestStripVersion:
    def test_strips_v1(self):
        assert _strip_version("2501.16496v1") == "2501.16496"

    def test_strips_v7(self):
        assert _strip_version("1706.03762v7") == "1706.03762"

    def test_no_version_noop(self):
        assert _strip_version("2501.16496") == "2501.16496"

    def test_high_version(self):
        assert _strip_version("1234.56789v42") == "1234.56789"


# ---------------------------------------------------------------------------
# _parse_arxiv_entry
# ---------------------------------------------------------------------------

class TestParseArxivEntry:
    def _get_entry(self, xml_str: str):
        """Parse XML and return the first <entry> element."""
        import defusedxml.ElementTree as ET
        root = ET.fromstring(xml_str)
        ns = "http://www.w3.org/2005/Atom"
        return root.find(f"{{{ns}}}entry")

    def test_all_fields(self):
        entry = self._get_entry(ARXIV_SINGLE_ENTRY_XML)
        assert entry is not None
        data = _parse_arxiv_entry(entry)

        assert data["id"] == "1706.03762v7"
        assert data["title"] == "Attention Is All You Need"
        assert "dominant sequence" in data["abstract"]
        assert len(data["authors"]) == 3
        assert data["authors"][0]["name"] == "Ashish Vaswani"
        assert data["authors"][0]["affiliations"] == ["Google Brain"]
        assert data["authors"][2]["affiliations"] == []  # Niki Parmar has no affiliation
        assert data["primary_category"] == "cs.CL"
        assert "cs.AI" in data["categories"]
        assert "cs.LG" in data["categories"]
        assert data["published"] == "2017-06-12T17:57:34Z"
        assert data["updated"] == "2023-08-02T01:18:13Z"
        assert data["doi"] == "10.5555/3295222.3295349"
        assert "NIPS 2017" in data["journal_ref"]
        assert data["comment"] == "15 pages, 5 figures"
        assert len(data["links"]) == 2

    def test_minimal_entry(self):
        entry = self._get_entry(ARXIV_MULTI_ENTRY_XML)
        assert entry is not None
        data = _parse_arxiv_entry(entry)

        assert data["id"] == "1706.03762v7"
        assert data["doi"] is None
        assert data["journal_ref"] is None
        assert data["comment"] is None


# ---------------------------------------------------------------------------
# _arxiv_request
# ---------------------------------------------------------------------------

class TestArxivRequest:
    @respx.mock
    @pytest.mark.asyncio
    async def test_successful_request(self):
        respx.get(ARXIV_API_URL).mock(
            return_value=httpx.Response(200, text=ARXIV_SINGLE_ENTRY_XML)
        )
        result = await _arxiv_request({"id_list": "1706.03762"})
        assert isinstance(result, list)
        assert len(result) == 1
        assert result[0]["title"] == "Attention Is All You Need"

    @respx.mock
    @pytest.mark.asyncio
    async def test_empty_response(self):
        respx.get(ARXIV_API_URL).mock(
            return_value=httpx.Response(200, text=ARXIV_EMPTY_XML)
        )
        result = await _arxiv_request({"search_query": "nonexistent"})
        assert isinstance(result, list)
        assert len(result) == 0

    @respx.mock
    @pytest.mark.asyncio
    async def test_retry_on_503(self, monkeypatch):
        monkeypatch.setattr(_arxiv_module, "_ARXIV_RETRY_BACKOFF", 0.0)
        route = respx.get(ARXIV_API_URL)
        route.side_effect = [
            httpx.Response(503),
            httpx.Response(200, text=ARXIV_SINGLE_ENTRY_XML),
        ]
        result = await _arxiv_request({"id_list": "1706.03762"})
        assert isinstance(result, list)
        assert len(result) == 1

    @respx.mock
    @pytest.mark.asyncio
    async def test_503_exhausted_retries(self, monkeypatch):
        monkeypatch.setattr(_arxiv_module, "_ARXIV_RETRY_BACKOFF", 0.0)
        respx.get(ARXIV_API_URL).mock(return_value=httpx.Response(503))
        result = await _arxiv_request({"id_list": "1706.03762"})
        assert isinstance(result, str)
        assert "503" in result

    @respx.mock
    @pytest.mark.asyncio
    async def test_timeout(self):
        respx.get(ARXIV_API_URL).mock(side_effect=httpx.TimeoutException("timed out"))
        result = await _arxiv_request({"id_list": "1706.03762"})
        assert isinstance(result, str)
        assert "timed out" in result

    @respx.mock
    @pytest.mark.asyncio
    async def test_http_error(self):
        respx.get(ARXIV_API_URL).mock(return_value=httpx.Response(500))
        result = await _arxiv_request({"id_list": "1706.03762"})
        assert isinstance(result, str)
        assert "500" in result

    @respx.mock
    @pytest.mark.asyncio
    async def test_503_honors_retry_after(self, monkeypatch):
        """A 503 carrying Retry-After waits that long, not the doubling backoff."""
        slept: list[float] = []

        async def fake_sleep(seconds):
            slept.append(seconds)

        monkeypatch.setattr(_arxiv_module.asyncio, "sleep", fake_sleep)
        route = respx.get(ARXIV_API_URL)
        route.side_effect = [
            httpx.Response(503, headers={"Retry-After": "7"}),
            httpx.Response(200, text=ARXIV_SINGLE_ENTRY_XML),
        ]
        result = await _arxiv_request({"id_list": "1706.03762"})
        assert isinstance(result, list)
        assert slept == [7.0]

    @pytest.mark.parametrize("status", [406, 429])
    @respx.mock
    @pytest.mark.asyncio
    async def test_load_shedding_is_reported_not_retried(self, status):
        """406 and 429 are arXiv's system-wide load shedding.

        The window lasts minutes and requests made inside it extend it, so
        the tool makes exactly one request and names the cause so the
        caller does not rewrite a query that was never the problem.
        """
        route = respx.get(ARXIV_API_URL).mock(return_value=httpx.Response(status))
        result = await _arxiv_request({"search_query": 'abs:"vision tokens"'})
        assert isinstance(result, str)
        assert result.startswith("Error:")
        assert str(status) in result
        assert "system-wide" in result
        assert "not a problem with the query" in result
        assert route.call_count == 1


class TestRetryDelay:
    def _resp(self, **headers):
        return httpx.Response(503, headers=headers)

    def test_backoff_doubles_without_retry_after(self, monkeypatch):
        monkeypatch.setattr(_arxiv_module, "_ARXIV_RETRY_BACKOFF", 3.0)
        assert [_retry_delay(self._resp(), n) for n in range(3)] == [3.0, 6.0, 12.0]

    def test_numeric_retry_after_wins(self):
        assert _retry_delay(self._resp(**{"Retry-After": "5"}), 2) == 5.0

    def test_retry_after_is_capped(self):
        assert _retry_delay(self._resp(**{"Retry-After": "3600"}), 0) == _ARXIV_RETRY_AFTER_CAP

    def test_http_date_retry_after_falls_back_to_backoff(self, monkeypatch):
        monkeypatch.setattr(_arxiv_module, "_ARXIV_RETRY_BACKOFF", 3.0)
        resp = self._resp(**{"Retry-After": "Wed, 23 Sep 2026 04:00:00 GMT"})
        assert _retry_delay(resp, 1) == 6.0


# ---------------------------------------------------------------------------
# _format_arxiv_paper
# ---------------------------------------------------------------------------

class TestFormatArxivPaper:
    def test_full_paper(self):
        import defusedxml.ElementTree as ET
        root = ET.fromstring(ARXIV_SINGLE_ENTRY_XML)
        entry = root.find(f"{{{_arxiv_module._ATOM_NS}}}entry")
        assert entry is not None
        data = _parse_arxiv_entry(entry)
        output = _format_arxiv_paper(data)

        assert "# Attention Is All You Need" in output
        assert "Ashish Vaswani (Google Brain)" in output
        assert "Noam Shazeer (Google Brain)" in output
        assert "Niki Parmar" in output
        assert "cs.CL" in output
        assert "cs.AI" in output
        assert "NIPS 2017" in output
        assert "15 pages, 5 figures" in output
        assert "SemanticScholar" in output
        assert "## Abstract" in output

    def test_arxiv_doi_synthesized(self):
        """arXiv DOI is deterministically synthesized from the ID, versionless."""
        data = {"id": "1706.03762v7", "title": "Test"}
        output = _format_arxiv_paper(data)
        # DOI must be versionless — DataCite doesn't register versioned DOIs
        assert "**arXiv DOI:** [10.48550/arXiv.1706.03762]" in output
        assert "https://doi.org/10.48550/arXiv.1706.03762" in output
        assert "v7" not in output.split("arXiv DOI")[1].split("\n")[0]  # no version in DOI line

    def test_publisher_doi_shown_separately(self):
        """When publisher DOI is distinct from arXiv DOI, both are shown."""
        import defusedxml.ElementTree as ET
        root = ET.fromstring(ARXIV_SINGLE_ENTRY_XML)
        entry = root.find(f"{{{_arxiv_module._ATOM_NS}}}entry")
        assert entry is not None
        data = _parse_arxiv_entry(entry)
        output = _format_arxiv_paper(data)
        assert "**arXiv DOI:**" in output
        assert "**Publisher DOI:** [10.5555/3295222.3295349]" in output

    def test_publisher_doi_dedup_when_matches_arxiv(self):
        """When publisher DOI equals synthesized arXiv DOI, only show once."""
        data = {"id": "1706.03762", "title": "Test", "doi": "10.48550/arXiv.1706.03762"}
        output = _format_arxiv_paper(data)
        assert "**arXiv DOI:**" in output
        assert "**Publisher DOI:**" not in output

    def test_no_publisher_doi(self):
        """Paper without publisher DOI shows only arXiv DOI."""
        data = {"id": "2301.12345", "title": "Test", "doi": None}
        output = _format_arxiv_paper(data)
        assert "**arXiv DOI:** [10.48550/arXiv.2301.12345]" in output
        assert "**Publisher DOI:**" not in output

    def test_empty_paper(self):
        output = _format_arxiv_paper({})
        assert "# Untitled" in output


# ---------------------------------------------------------------------------
# _format_arxiv_list
# ---------------------------------------------------------------------------

class TestFormatArxivList:
    def test_empty_list(self):
        assert _format_arxiv_list([], None, 0) == "No papers found."

    def test_numbered_list(self):
        papers = [
            {
                "id": "1706.03762",
                "title": "Attention Is All You Need",
                "authors": [{"name": "Vaswani", "affiliations": []}],
                "primary_category": "cs.CL",
            },
            {
                "id": "1810.04805",
                "title": "BERT",
                "authors": [
                    {"name": "Devlin", "affiliations": []},
                    {"name": "Chang", "affiliations": []},
                ],
                "primary_category": "cs.CL",
            },
        ]
        output = _format_arxiv_list(papers, total=None, offset=0)
        assert "1. **Attention Is All You Need** [cs.CL]" in output
        assert "2. **BERT** [cs.CL]" in output
        assert "arXiv:1706.03762" in output
        assert "Devlin et al." in output

    def test_pagination_hint(self):
        papers = [{"id": "1", "title": "T", "authors": [], "primary_category": "cs.AI"}]
        output = _format_arxiv_list(papers, total=50, offset=0)
        assert "Showing 1-1 of 50 results" in output

    def test_hint_suppressed(self):
        """include_hint=False should omit the embedded markdown hint."""
        papers = [{"id": "1", "title": "T", "authors": [], "primary_category": "cs.AI"}]
        output = _format_arxiv_list(papers, total=None, offset=0, include_hint=False)
        assert "paper action" not in output.lower()
        assert "SemanticScholar" not in output


# ---------------------------------------------------------------------------
# _fetch_arxiv_paper
# ---------------------------------------------------------------------------

class TestFetchArxivPaper:
    @respx.mock
    @pytest.mark.asyncio
    async def test_end_to_end_html_available(self):
        respx.get(ARXIV_API_URL).mock(
            return_value=httpx.Response(200, text=ARXIV_SINGLE_ENTRY_XML)
        )
        respx.head("https://arxiv.org/html/1706.03762v7").mock(
            return_value=httpx.Response(200)
        )
        result = await _fetch_arxiv_paper("1706.03762")
        assert "---" in result  # frontmatter
        assert "api: arXiv" in result
        assert "full_text:" in result
        assert "warning:" not in result
        assert "see_also:" in result
        assert "# Attention Is All You Need" in result

    @respx.mock
    @pytest.mark.asyncio
    async def test_end_to_end_html_unavailable(self):
        respx.get(ARXIV_API_URL).mock(
            return_value=httpx.Response(200, text=ARXIV_SINGLE_ENTRY_XML)
        )
        respx.head("https://arxiv.org/html/1706.03762v7").mock(
            return_value=httpx.Response(404)
        )
        result = await _fetch_arxiv_paper("1706.03762")
        assert "full_text:" not in result
        assert "warning:" in result
        assert "HTML full text is not available" in result
        assert "abstract and metadata" in result
        # Body should not include the HTML link
        assert "**HTML:**" not in result
        # S2 cross-reference should mention snippets as alternative
        assert "body text snippets" in result

    @respx.mock
    @pytest.mark.asyncio
    async def test_pdf_hint(self):
        respx.get(ARXIV_API_URL).mock(
            return_value=httpx.Response(200, text=ARXIV_SINGLE_ENTRY_XML)
        )
        respx.head("https://arxiv.org/html/1706.03762v7").mock(
            return_value=httpx.Response(200)
        )
        result = await _fetch_arxiv_paper("1706.03762", _pdf_url=True)
        assert "note:" in result
        assert "PDF" in result

    @respx.mock
    @pytest.mark.asyncio
    async def test_not_found(self):
        respx.get(ARXIV_API_URL).mock(
            return_value=httpx.Response(200, text=ARXIV_EMPTY_XML)
        )
        result = await _fetch_arxiv_paper("0000.00000")
        assert "Error" in result
        assert "No paper found" in result

    @respx.mock
    @pytest.mark.asyncio
    async def test_crossref_retraction_surfaces_banner_and_alert(self):
        """When CrossRef reports the paper's publisher DOI is retracted,
        the arXiv response surfaces a [RETRACTED] banner, alert: fm key,
        and the shelf entry lands in the retracted bucket."""
        from parkour_mcp.shelf import _get_shelf, _reset_shelf
        _reset_shelf()
        try:
            respx.get(ARXIV_API_URL).mock(
                return_value=httpx.Response(200, text=ARXIV_SINGLE_ENTRY_XML)
            )
            respx.head("https://arxiv.org/html/1706.03762v7").mock(
                return_value=httpx.Response(200)
            )
            # Publisher DOI in the arXiv XML is 10.5555/3295222.3295349
            respx.get(
                "https://api.crossref.org/works/10.5555/3295222.3295349"
            ).mock(return_value=httpx.Response(200, json={
                "status": "ok",
                "message-type": "work",
                "message": {
                    "DOI": "10.5555/3295222.3295349",
                    "type": "journal-article",
                    "is-referenced-by-count": 0,
                    "updated-by": [
                        {
                            "updated": {"date-parts": [[2024, 1, 15]]},
                            "DOI": "10.5555/notice.2024.001",
                            "type": "retraction",
                            "source": "publisher",
                            "label": "Retraction",
                        }
                    ],
                    "relation": {},
                    "license": [],
                },
            }))
            result = await _fetch_arxiv_paper("1706.03762")
            assert "[RETRACTED]" in result
            assert "alert:" in result
            assert "retracted 2024-01-15" in result
            assert "10.5555/notice.2024.001" in result
            assert "note:" in result
            assert "retracted shelf bucket" in result
            # Verify shelf state
            shelf = _get_shelf()
            active, retracted = await shelf.counts()
            assert active == 0
            assert retracted == 1
        finally:
            _reset_shelf()

    @respx.mock
    @pytest.mark.asyncio
    async def test_crossref_unavailable_does_not_break(self):
        """CrossRef enrichment is fail-open: a 500 error leaves the
        arXiv response intact with no banner/alert."""
        from parkour_mcp.shelf import _get_shelf, _reset_shelf
        _reset_shelf()
        try:
            respx.get(ARXIV_API_URL).mock(
                return_value=httpx.Response(200, text=ARXIV_SINGLE_ENTRY_XML)
            )
            respx.head("https://arxiv.org/html/1706.03762v7").mock(
                return_value=httpx.Response(200)
            )
            respx.get(
                "https://api.crossref.org/works/10.5555/3295222.3295349"
            ).mock(return_value=httpx.Response(500))
            result = await _fetch_arxiv_paper("1706.03762")
            assert "[RETRACTED]" not in result
            assert "alert:" not in result
            # Paper lands on the active shelf, not retracted
            shelf = _get_shelf()
            active, retracted = await shelf.counts()
            assert active == 1
            assert retracted == 0
        finally:
            _reset_shelf()


# ---------------------------------------------------------------------------
# arxiv() tool
# ---------------------------------------------------------------------------

class TestArxivTool:
    @respx.mock
    @pytest.mark.asyncio
    async def test_search_action(self):
        respx.get(ARXIV_API_URL).mock(
            return_value=httpx.Response(200, text=ARXIV_MULTI_ENTRY_XML)
        )
        result = await arxiv(action="search", query="ti:attention AND cat:cs.CL")
        assert result.startswith("---\n")
        assert "api: arXiv" in result
        assert "action: search" in result
        assert "hint:" in result
        assert "Attention Is All You Need" in result
        assert "BERT" in result

    @respx.mock
    @pytest.mark.asyncio
    async def test_search_no_results(self):
        respx.get(ARXIV_API_URL).mock(
            return_value=httpx.Response(200, text=ARXIV_EMPTY_XML)
        )
        result = await arxiv(action="search", query="ti:nonexistent12345")
        assert "No papers found" in result

    @respx.mock
    @pytest.mark.asyncio
    async def test_paper_action_with_id(self):
        respx.get(ARXIV_API_URL).mock(
            return_value=httpx.Response(200, text=ARXIV_SINGLE_ENTRY_XML)
        )
        respx.head("https://arxiv.org/html/1706.03762v7").mock(
            return_value=httpx.Response(200)
        )
        result = await arxiv(action="paper", query="1706.03762")
        assert "Attention Is All You Need" in result
        assert "api: arXiv" in result

    @respx.mock
    @pytest.mark.asyncio
    async def test_paper_action_with_url(self):
        respx.get(ARXIV_API_URL).mock(
            return_value=httpx.Response(200, text=ARXIV_SINGLE_ENTRY_XML)
        )
        respx.head("https://arxiv.org/html/1706.03762v7").mock(
            return_value=httpx.Response(200)
        )
        result = await arxiv(action="paper", query="https://arxiv.org/abs/1706.03762")
        assert "Attention Is All You Need" in result

    @respx.mock
    @pytest.mark.asyncio
    async def test_category_action(self):
        respx.get(ARXIV_API_URL).mock(
            return_value=httpx.Response(200, text=ARXIV_MULTI_ENTRY_XML)
        )
        result = await arxiv(action="category", query="cs.CL")
        assert result.startswith("---\n")
        assert "api: arXiv" in result
        assert "action: category" in result
        assert "category: cs.CL" in result
        assert "hint:" in result
        assert "Attention Is All You Need" in result

    @pytest.mark.asyncio
    async def test_unknown_action(self):
        result = await arxiv(action="invalid", query="test")
        assert "Error" in result
        assert "Unknown action" in result


# ---------------------------------------------------------------------------
# Fast-path integration
# ---------------------------------------------------------------------------

class TestArxivFastPath:
    @respx.mock
    @pytest.mark.asyncio
    async def test_abs_url_triggers_api(self):
        respx.get(ARXIV_API_URL).mock(
            return_value=httpx.Response(200, text=ARXIV_SINGLE_ENTRY_XML)
        )
        respx.head("https://arxiv.org/html/1706.03762v7").mock(
            return_value=httpx.Response(200)
        )
        result = await _arxiv_fast_path("https://arxiv.org/abs/1706.03762")
        assert result is not None
        assert "Attention Is All You Need" in result

    @respx.mock
    @pytest.mark.asyncio
    async def test_pdf_url_triggers_api(self):
        respx.get(ARXIV_API_URL).mock(
            return_value=httpx.Response(200, text=ARXIV_SINGLE_ENTRY_XML)
        )
        respx.head("https://arxiv.org/html/1706.03762v7").mock(
            return_value=httpx.Response(200)
        )
        result = await _arxiv_fast_path("https://arxiv.org/pdf/1706.03762")
        assert result is not None
        assert "PDF" in result  # PDF hint in frontmatter

    @pytest.mark.asyncio
    async def test_html_url_returns_none(self):
        """HTML URLs must fall through to HTTP fetch."""
        result = await _arxiv_fast_path("https://arxiv.org/html/1706.03762")
        assert result is None

    @pytest.mark.asyncio
    async def test_non_arxiv_url_returns_none(self):
        result = await _arxiv_fast_path("https://example.com/page")
        assert result is None
