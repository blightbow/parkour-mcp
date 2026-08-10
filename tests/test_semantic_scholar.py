"""Tests for parkour_mcp.semantic_scholar module."""

import sys

import httpx
import pytest
import respx

import parkour_mcp.semantic_scholar  # noqa: F401

# Alias the module before importing the same-named function
_s2_module = sys.modules["parkour_mcp.semantic_scholar"]

from parkour_mcp._pipeline import _s2_fast_path  # noqa: E402
from parkour_mcp.detection import _detect_s2_url  # noqa: E402
from parkour_mcp.semantic_scholar import (  # noqa: E402
    _DETAIL_FIELDS,
    S2_BASE_URL,
    _fetch_s2_paper,
    _format_paper_detail,
    _get_s2_api_key,
    _s2_request,
    semantic_scholar,
)

from .conftest import (  # noqa: E402
    S2_AUTHOR_DETAIL_RESPONSE,
    S2_AUTHOR_PAPERS_RESPONSE,
    S2_AUTHOR_SEARCH_RESPONSE,
    S2_PAPER_DETAIL_RESPONSE,
    S2_PAPER_SEARCH_RESPONSE,
    S2_REFERENCE_RESPONSE,
    S2_SNIPPET_CORPUS_RESPONSE,
    S2_SNIPPET_RESPONSE,
    S2_TEXT_AVAILABILITY_FULLTEXT,
    S2_TEXT_AVAILABILITY_NONE,
)

# ---------------------------------------------------------------------------
# _detect_s2_url
# ---------------------------------------------------------------------------

class TestDetectS2Url:
    def test_standard_url_with_slug(self):
        url = "https://www.semanticscholar.org/paper/Attention-Is-All-You-Need-Vaswani-Shazeer/204e3073870fae3d05bcbc2f6a8e263d9b72e776"
        assert _detect_s2_url(url) == "204e3073870fae3d05bcbc2f6a8e263d9b72e776"

    def test_url_without_slug(self):
        url = "https://www.semanticscholar.org/paper/204e3073870fae3d05bcbc2f6a8e263d9b72e776"
        assert _detect_s2_url(url) == "204e3073870fae3d05bcbc2f6a8e263d9b72e776"

    def test_url_without_www(self):
        url = "https://semanticscholar.org/paper/Attention-Is-All-You-Need-Vaswani-Shazeer/204e3073870fae3d05bcbc2f6a8e263d9b72e776"
        assert _detect_s2_url(url) == "204e3073870fae3d05bcbc2f6a8e263d9b72e776"

    def test_url_with_query_params(self):
        url = "https://www.semanticscholar.org/paper/Attention-Is-All-You-Need-Vaswani-Shazeer/204e3073870fae3d05bcbc2f6a8e263d9b72e776?sort=relevance"
        assert _detect_s2_url(url) == "204e3073870fae3d05bcbc2f6a8e263d9b72e776"

    def test_non_s2_url(self):
        assert _detect_s2_url("https://arxiv.org/abs/1706.03762") is None

    def test_s2_non_paper_url(self):
        assert _detect_s2_url("https://www.semanticscholar.org/author/1234") is None

    def test_http_scheme(self):
        url = "http://www.semanticscholar.org/paper/204e3073870fae3d05bcbc2f6a8e263d9b72e776"
        assert _detect_s2_url(url) == "204e3073870fae3d05bcbc2f6a8e263d9b72e776"


# ---------------------------------------------------------------------------
# _get_s2_api_key
# ---------------------------------------------------------------------------

class TestGetS2ApiKey:
    def test_env_var_precedence(self, monkeypatch, tmp_path):
        monkeypatch.setenv("S2_API_KEY", "env-key-123")
        assert _get_s2_api_key() == "env-key-123"

    def test_config_file_fallback(self, monkeypatch, tmp_path):
        monkeypatch.delenv("S2_API_KEY", raising=False)
        key_file = tmp_path / "s2_api_key"
        key_file.write_text("file-key-456\n")
        monkeypatch.setattr(_s2_module, "S2_CONFIG_PATH", key_file)
        assert _get_s2_api_key() == "file-key-456"

    def test_missing_key(self, monkeypatch, tmp_path):
        monkeypatch.delenv("S2_API_KEY", raising=False)
        monkeypatch.setattr(_s2_module, "S2_CONFIG_PATH", tmp_path / "nonexistent")
        assert _get_s2_api_key() == ""

    def test_env_var_overrides_config_file(self, monkeypatch, tmp_path):
        monkeypatch.setenv("S2_API_KEY", "env-key")
        key_file = tmp_path / "s2_api_key"
        key_file.write_text("file-key")
        monkeypatch.setattr(_s2_module, "S2_CONFIG_PATH", key_file)
        assert _get_s2_api_key() == "env-key"


# ---------------------------------------------------------------------------
# _s2_request
# ---------------------------------------------------------------------------

class TestS2Request:
    @pytest.mark.asyncio
    @respx.mock
    async def test_success(self):
        respx.get(f"{S2_BASE_URL}/paper/search").mock(
            return_value=httpx.Response(200, json={"data": []})
        )
        result = await _s2_request("/paper/search", {"query": "test"})
        assert isinstance(result, dict)
        assert result == {"data": []}

    @pytest.mark.asyncio
    @respx.mock
    async def test_404(self):
        respx.get(f"{S2_BASE_URL}/paper/invalid").mock(
            return_value=httpx.Response(404, json={"error": "Paper not found"})
        )
        result = await _s2_request("/paper/invalid")
        assert "Not found" in result

    @pytest.mark.asyncio
    @respx.mock
    async def test_403_with_key(self, monkeypatch):
        monkeypatch.setenv("S2_API_KEY", "my-key")
        respx.get(f"{S2_BASE_URL}/paper/search").mock(
            return_value=httpx.Response(403)
        )
        result = await _s2_request("/paper/search", {"query": "test"})
        assert "rejected the configured API key" in result
        assert "malformed, revoked, or deactivated" in result
        assert "api-key-form" in result

    @pytest.mark.asyncio
    @respx.mock
    async def test_403_without_key(self, monkeypatch):
        monkeypatch.delenv("S2_API_KEY", raising=False)
        monkeypatch.setattr(_s2_module, "S2_CONFIG_PATH", Path("/nonexistent/path"))
        respx.get(f"{S2_BASE_URL}/paper/search").mock(
            return_value=httpx.Response(403)
        )
        result = await _s2_request("/paper/search", {"query": "test"})
        assert "403" in result
        assert "rejected the configured API key" not in result

    @pytest.mark.asyncio
    @respx.mock
    async def test_400_surfaces_api_error_detail(self):
        """A bad field name is diagnosable only from the API's own body."""
        respx.get(f"{S2_BASE_URL}/paper/search").mock(
            return_value=httpx.Response(
                400, json={"error": "Unrecognized or unsupported fields: [bogus]"}
            )
        )
        result = await _s2_request("/paper/search", {"query": "test"})
        assert "400" in result
        assert "Unrecognized or unsupported fields: [bogus]" in result

    @pytest.mark.asyncio
    @respx.mock
    async def test_400_without_parseable_body_stays_generic(self):
        respx.get(f"{S2_BASE_URL}/paper/search").mock(
            return_value=httpx.Response(400, text="<html>gateway</html>")
        )
        result = await _s2_request("/paper/search", {"query": "test"})
        assert result == "Error: Semantic Scholar API returned HTTP 400."

    @pytest.mark.asyncio
    @respx.mock
    async def test_error_detail_is_single_line_and_bounded(self):
        """The detail is embedded in a one-line 'Error: ...' string."""
        respx.get(f"{S2_BASE_URL}/paper/search").mock(
            return_value=httpx.Response(
                400, json={"error": "line one\nline two   " + "x" * 500}
            )
        )
        result = await _s2_request("/paper/search", {"query": "test"})
        assert "\n" not in result
        assert "line one line two" in result
        assert len(result) < 400

    @pytest.mark.asyncio
    @respx.mock
    async def test_429_without_key(self, monkeypatch):
        monkeypatch.delenv("S2_API_KEY", raising=False)
        monkeypatch.setattr(_s2_module, "S2_CONFIG_PATH", Path("/nonexistent/path"))
        monkeypatch.setattr(_s2_module, "_S2_MAX_RETRIES", 0)
        respx.get(f"{S2_BASE_URL}/paper/search").mock(
            return_value=httpx.Response(429)
        )
        result = await _s2_request("/paper/search", {"query": "test"})
        assert "Rate limited" in result
        assert "S2_API_KEY" in result
        assert "api-key-form" in result

    @pytest.mark.asyncio
    @respx.mock
    async def test_429_with_key(self, monkeypatch):
        monkeypatch.setenv("S2_API_KEY", "my-key")
        monkeypatch.setattr(_s2_module, "_S2_MAX_RETRIES", 0)
        respx.get(f"{S2_BASE_URL}/paper/search").mock(
            return_value=httpx.Response(429)
        )
        result = await _s2_request("/paper/search", {"query": "test"})
        assert "Rate limited" in result
        assert "Try again" in result
        assert "S2_API_KEY" not in result

    @pytest.mark.asyncio
    @respx.mock
    async def test_429_retry_then_success(self, monkeypatch):
        monkeypatch.setattr(_s2_module, "_S2_RETRY_BACKOFF", 0.0)
        route = respx.get(f"{S2_BASE_URL}/paper/search")
        route.side_effect = [
            httpx.Response(429),
            httpx.Response(200, json={"data": []}),
        ]
        result = await _s2_request("/paper/search", {"query": "test"})
        assert isinstance(result, dict)
        assert result == {"data": []}
        assert route.call_count == 2

    @pytest.mark.asyncio
    @respx.mock
    async def test_429_exhausts_retries(self, monkeypatch):
        monkeypatch.setattr(_s2_module, "_S2_MAX_RETRIES", 2)
        monkeypatch.setattr(_s2_module, "_S2_RETRY_BACKOFF", 0.0)
        monkeypatch.setenv("S2_API_KEY", "my-key")
        route = respx.get(f"{S2_BASE_URL}/paper/search")
        route.mock(return_value=httpx.Response(429))
        result = await _s2_request("/paper/search", {"query": "test"})
        assert "Rate limited" in result
        assert route.call_count == 3  # initial + 2 retries

    @pytest.mark.asyncio
    @respx.mock
    async def test_timeout(self):
        respx.get(f"{S2_BASE_URL}/paper/search").mock(
            side_effect=httpx.ReadTimeout("timed out")
        )
        result = await _s2_request("/paper/search", {"query": "test"})
        assert "timed out" in result


# ---------------------------------------------------------------------------
# semantic_scholar — search
# ---------------------------------------------------------------------------

class TestSemanticScholarSearch:
    @pytest.mark.asyncio
    @respx.mock
    async def test_keyword_search(self):
        respx.get(f"{S2_BASE_URL}/paper/search").mock(
            return_value=httpx.Response(200, json=S2_PAPER_SEARCH_RESPONSE)
        )
        result = await semantic_scholar("search", "attention mechanism transformers")
        assert result.startswith("---\n")
        assert "api: Semantic Scholar" in result
        assert "action: search" in result
        assert "hint:" in result
        assert "Attention is All you Need" in result
        assert "Vaswani" in result
        assert "1,542" in result  # total in pagination hint

    @pytest.mark.asyncio
    @respx.mock
    async def test_empty_results(self):
        respx.get(f"{S2_BASE_URL}/paper/search").mock(
            return_value=httpx.Response(200, json={"total": 0, "data": []})
        )
        result = await semantic_scholar("search", "xyznonexistent")
        assert "No papers found" in result

    @pytest.mark.asyncio
    @respx.mock
    async def test_pagination(self):
        respx.get(f"{S2_BASE_URL}/paper/search").mock(
            return_value=httpx.Response(200, json=S2_PAPER_SEARCH_RESPONSE)
        )
        result = await semantic_scholar("search", "attention", offset=10, limit=5)
        assert "offset" in result.lower() or "paginate" in result.lower()


# ---------------------------------------------------------------------------
# semantic_scholar — paper
# ---------------------------------------------------------------------------

class TestSemanticScholarPaper:
    @pytest.mark.asyncio
    @respx.mock
    async def test_by_id(self):
        paper_id = "204e3073870fae3d05bcbc2f6a8e263d9b72e776"
        respx.get(f"{S2_BASE_URL}/paper/{paper_id}").mock(
            return_value=httpx.Response(200, json=S2_PAPER_DETAIL_RESPONSE)
        )
        result = await semantic_scholar("paper", paper_id)
        assert "Attention is All you Need" in result
        assert "Vaswani" in result
        assert "10.48550/arXiv.1706.03762" in result
        assert "Abstract" in result

    @pytest.mark.asyncio
    @respx.mock
    async def test_by_doi_prefix(self):
        doi = "DOI:10.48550/arXiv.1706.03762"
        respx.get(f"{S2_BASE_URL}/paper/{doi}").mock(
            return_value=httpx.Response(200, json=S2_PAPER_DETAIL_RESPONSE)
        )
        result = await semantic_scholar("paper", doi)
        assert "Attention is All you Need" in result

    @pytest.mark.asyncio
    @respx.mock
    async def test_by_s2_url(self):
        s2_url = "https://www.semanticscholar.org/paper/Attention-Is-All-You-Need-Vaswani-Shazeer/204e3073870fae3d05bcbc2f6a8e263d9b72e776"
        paper_id = "204e3073870fae3d05bcbc2f6a8e263d9b72e776"
        respx.get(f"{S2_BASE_URL}/paper/{paper_id}").mock(
            return_value=httpx.Response(200, json=S2_PAPER_DETAIL_RESPONSE)
        )
        result = await semantic_scholar("paper", s2_url)
        assert "Attention is All you Need" in result

    @pytest.mark.asyncio
    @respx.mock
    async def test_not_found(self):
        respx.get(f"{S2_BASE_URL}/paper/nonexistent").mock(
            return_value=httpx.Response(404, json={"error": "not found"})
        )
        result = await semantic_scholar("paper", "nonexistent")
        assert "Not found" in result

    @pytest.mark.asyncio
    @respx.mock
    async def test_fetch_s2_paper_frontmatter(self):
        paper_id = "204e3073870fae3d05bcbc2f6a8e263d9b72e776"
        respx.get(f"{S2_BASE_URL}/paper/{paper_id}").mock(
            return_value=httpx.Response(200, json=S2_PAPER_DETAIL_RESPONSE)
        )
        result = await _fetch_s2_paper(paper_id)
        assert "---" in result
        assert "api: Semantic Scholar" in result
        assert "source:" in result
        assert "see_also:" in result
        assert "ARXIV:1706.03762" in result

    @pytest.mark.asyncio
    @respx.mock
    async def test_crossref_retraction_surfaces_banner_and_alert(self):
        """An S2 paper whose DOI is reported retracted by CrossRef
        surfaces a banner, alert: fm key, and lands in the retracted
        shelf bucket."""
        from parkour_mcp.shelf import _get_shelf, _reset_shelf
        _reset_shelf()
        try:
            paper_id = "204e3073870fae3d05bcbc2f6a8e263d9b72e776"
            respx.get(f"{S2_BASE_URL}/paper/{paper_id}").mock(
                return_value=httpx.Response(200, json=S2_PAPER_DETAIL_RESPONSE)
            )
            # DOI from S2_PAPER_DETAIL_RESPONSE is 10.48550/arXiv.1706.03762
            respx.get(
                "https://api.crossref.org/works/10.48550/arXiv.1706.03762"
            ).mock(return_value=httpx.Response(200, json={
                "status": "ok",
                "message-type": "work",
                "message": {
                    "DOI": "10.48550/arxiv.1706.03762",
                    "type": "journal-article",
                    "is-referenced-by-count": 0,
                    "updated-by": [
                        {
                            "updated": {"date-parts": [[2024, 3, 1]]},
                            "DOI": "10.5555/notice.2024.001",
                            "type": "retraction",
                            "source": "retraction-watch",
                            "label": "Retraction",
                        }
                    ],
                    "relation": {},
                    "license": [],
                },
            }))
            result = await _fetch_s2_paper(paper_id)
            assert "[RETRACTED]" in result
            assert "alert:" in result
            assert "2024-03-01" in result
            assert "note:" in result
            assert "retracted shelf bucket" in result
            shelf = _get_shelf()
            active, retracted = await shelf.counts()
            assert active == 0
            assert retracted == 1
        finally:
            _reset_shelf()


# ---------------------------------------------------------------------------
# semantic_scholar — paper includes citation counts
# ---------------------------------------------------------------------------

class TestSemanticScholarPaperCitationCounts:
    @pytest.mark.asyncio
    @respx.mock
    async def test_paper_includes_influential_count(self):
        paper_id = "204e3073870fae3d05bcbc2f6a8e263d9b72e776"
        detail = dict(S2_PAPER_DETAIL_RESPONSE)
        detail["influentialCitationCount"] = 4542
        respx.get(f"{S2_BASE_URL}/paper/{paper_id}").mock(
            return_value=httpx.Response(200, json=detail)
        )
        result = await semantic_scholar("paper", paper_id)
        assert "120,000" in result
        assert "4,542 influential" in result

    @pytest.mark.asyncio
    @respx.mock
    async def test_paper_action_includes_frontmatter(self):
        """The paper action should include YAML frontmatter like the URL interception path."""
        paper_id = "204e3073870fae3d05bcbc2f6a8e263d9b72e776"
        respx.get(f"{S2_BASE_URL}/paper/{paper_id}").mock(
            return_value=httpx.Response(200, json=S2_PAPER_DETAIL_RESPONSE)
        )
        result = await semantic_scholar("paper", paper_id)
        assert result.startswith("---\n")
        assert "api: Semantic Scholar" in result
        assert f"source: https://www.semanticscholar.org/paper/{paper_id}" in result
        assert "see_also:" in result
        assert "ARXIV:1706.03762" in result

    @pytest.mark.asyncio
    @respx.mock
    async def test_paper_without_arxiv_id(self):
        """Paper without arXiv ID should only have DOI hint, not arXiv hint."""
        paper_id = "204e3073870fae3d05bcbc2f6a8e263d9b72e776"
        detail = dict(S2_PAPER_DETAIL_RESPONSE)
        detail["externalIds"] = {"DOI": "10.1234/test"}
        respx.get(f"{S2_BASE_URL}/paper/{paper_id}").mock(
            return_value=httpx.Response(200, json=detail)
        )
        result = await semantic_scholar("paper", paper_id)
        assert result.startswith("---\n")
        assert "ArXiv" not in result.split("---")[1]  # no arXiv hint in frontmatter
        assert "doi.org/10.1234/test" in result  # DOI hint present


# ---------------------------------------------------------------------------
# _format_paper_detail — author enrichment and BibTeX
# ---------------------------------------------------------------------------

class TestFormatPaperDetailEnriched:
    def test_orcid_rendered_as_link(self):
        result = _format_paper_detail(S2_PAPER_DETAIL_RESPONSE)
        assert "[ORCID](https://orcid.org/0000-0002-1234-5678)" in result

    def test_author_without_orcid_renders_normally(self):
        result = _format_paper_detail(S2_PAPER_DETAIL_RESPONSE)
        assert "Noam Shazeer" in result
        # Shazeer has no ORCID — should not have a link
        assert result.count("[ORCID]") == 1  # only Vaswani's

    def test_affiliations_rendered(self):
        result = _format_paper_detail(S2_PAPER_DETAIL_RESPONSE)
        assert "(Google Brain)" in result

    def test_bibtex_section_present(self):
        result = _format_paper_detail(S2_PAPER_DETAIL_RESPONSE)
        assert "## BibTeX" in result
        assert "```bibtex" in result
        assert "@Article{Vaswani2017AttentionIA" in result

    def test_bibtex_absent_when_missing(self):
        data = dict(S2_PAPER_DETAIL_RESPONSE)
        data["citationStyles"] = {}
        result = _format_paper_detail(data)
        assert "## BibTeX" not in result

    def test_authors_truncated_at_ten(self):
        data = dict(S2_PAPER_DETAIL_RESPONSE)
        data["authors"] = [
            {"authorId": str(i), "name": f"Author {i}"}
            for i in range(15)
        ]
        result = _format_paper_detail(data)
        assert "... and 5 more" in result
        assert "Author 0" in result
        assert "Author 9" in result
        assert "Author 10" not in result

    def test_mixed_author_metadata(self):
        """Authors with varying metadata: some have ORCIDs, some affiliations, some neither."""
        data = dict(S2_PAPER_DETAIL_RESPONSE)
        data["authors"] = [
            {"name": "Alice", "affiliations": ["MIT"], "externalIds": {"ORCID": "0000-0001-0000-0001"}},
            {"name": "Bob", "affiliations": [], "externalIds": {}},
            {"name": "Carol", "affiliations": ["Stanford"], "externalIds": {}},
        ]
        result = _format_paper_detail(data)
        assert "Alice (MIT) [ORCID](https://orcid.org/0000-0001-0000-0001)" in result
        assert "Bob" in result
        assert "Carol (Stanford)" in result

    def test_null_author_name_does_not_crash_concatenation(self):
        """An explicit null name still renders, and still picks up affiliations.

        `.get("name", "Unknown")` returns None for a present-but-null key, and
        the affiliation/ORCID suffixes are built by `+=` on that value.
        """
        data: dict = dict(S2_PAPER_DETAIL_RESPONSE)
        data["authors"] = [
            {"authorId": "1", "name": None, "affiliations": ["MIT"], "externalIds": {}},
        ]
        result = _format_paper_detail(data)
        assert "Unknown (MIT)" in result


# ---------------------------------------------------------------------------
# semantic_scholar — API field-set contract
# ---------------------------------------------------------------------------

# Naming any `X.<sub>` field on a nested S2 object replaces that object's
# default subselection rather than adding to it, and a bare `X` in the same
# list does not restore it. Only the identity key survives unconditionally
# (authorId / paperId). Verified against the live API on 2026-08-08:
#
#   authors                      -> authorId, name
#   authors,authors.affiliations -> authorId, affiliations      (name lost)
#   citations                    -> paperId, title
#   citations,citations.year     -> paperId, year               (title lost)
#   references,references.year   -> paperId, year               (title lost)
#
# Subselection is not universal: `journal.name` and `tldr.text` are rejected
# as unsupported fields, so a dotted name alone does not imply this rule.
#
# Values are the subfields the default selection provides beyond the identity
# key, i.e. exactly what a nested selection silently drops.
_S2_DEFAULT_SUBFIELDS = {
    "authors": ("name",),
    "citations": ("title",),
    "references": ("title",),
}


class TestNestedFieldSelection:
    """No response fixture can catch a dropped subfield: the response shape is
    downstream of the request shape, so a hand-written fixture just asserts
    that we asked for the right thing while asking for the wrong thing.
    """

    def test_nested_selection_rerequests_default_subfields(self):
        from parkour_mcp import semantic_scholar as s2

        field_sets = {
            n: v for n, v in vars(s2).items()
            if n.endswith("_FIELDS") and isinstance(v, str)
        }
        assert field_sets, "no *_FIELDS constants found — did they get renamed?"

        for set_name, spec in field_sets.items():
            fields = [f.strip() for f in spec.split(",")]
            for parent, defaults in _S2_DEFAULT_SUBFIELDS.items():
                selected = [f for f in fields if f.startswith(f"{parent}.")]
                if not selected:
                    continue
                for sub in defaults:
                    assert f"{parent}.{sub}" in fields, (
                        f"{set_name} selects {selected} but not {parent}.{sub}; "
                        f"a nested selection replaces the default subselection, "
                        f"so {parent} entries come back without {sub}"
                    )

    def test_no_bare_parent_alongside_nested_selection(self):
        """A bare parent next to a nested selection is inert, and reads as if
        it still pulls the defaults. That misreading is what shipped."""
        from parkour_mcp import semantic_scholar as s2

        for set_name, spec in vars(s2).items():
            if not (set_name.endswith("_FIELDS") and isinstance(spec, str)):
                continue
            fields = [f.strip() for f in spec.split(",")]
            for parent in _S2_DEFAULT_SUBFIELDS:
                if any(f.startswith(f"{parent}.") for f in fields):
                    assert parent not in fields, (
                        f"{set_name} lists bare `{parent}` alongside a nested "
                        f"`{parent}.<sub>` selection; the bare name is "
                        "overridden, not merged"
                    )


# ---------------------------------------------------------------------------
# semantic_scholar — references
# ---------------------------------------------------------------------------

class TestSemanticScholarReferences:
    @pytest.mark.asyncio
    @respx.mock
    async def test_basic_references(self):
        paper_id = "204e3073870fae3d05bcbc2f6a8e263d9b72e776"
        respx.get(f"{S2_BASE_URL}/paper/{paper_id}/references").mock(
            return_value=httpx.Response(200, json=S2_REFERENCE_RESPONSE)
        )
        result = await semantic_scholar("references", paper_id)
        assert result.startswith("---\n")
        assert "api: Semantic Scholar" in result
        assert "action: references" in result
        assert "hint:" in result  # fixture has next=1, so more pages exist
        assert "Bahdanau" in result
        assert "Neural Machine Translation" in result


# ---------------------------------------------------------------------------
# semantic_scholar — author
# ---------------------------------------------------------------------------

class TestSemanticScholarAuthor:
    @pytest.mark.asyncio
    @respx.mock
    async def test_author_search(self):
        respx.get(f"{S2_BASE_URL}/author/search").mock(
            return_value=httpx.Response(200, json=S2_AUTHOR_SEARCH_RESPONSE)
        )
        result = await semantic_scholar("author_search", "Ashish Vaswani")
        assert result.startswith("---\n")
        assert "api: Semantic Scholar" in result
        assert "action: author_search" in result
        assert "Ashish Vaswani" in result
        assert "Google Brain" in result
        assert "1234" in result

    @pytest.mark.asyncio
    @respx.mock
    async def test_author_detail_with_papers(self):
        respx.get(f"{S2_BASE_URL}/author/1234").mock(
            return_value=httpx.Response(200, json=S2_AUTHOR_DETAIL_RESPONSE)
        )
        respx.get(f"{S2_BASE_URL}/author/1234/papers").mock(
            return_value=httpx.Response(200, json=S2_AUTHOR_PAPERS_RESPONSE)
        )
        result = await semantic_scholar("author", "1234")
        assert result.startswith("---\n")
        assert "api: Semantic Scholar" in result
        assert "action: author" in result
        assert "source: https://www.semanticscholar.org/author/1234" in result
        assert "Ashish Vaswani" in result
        assert "h-index:** 25" in result
        assert "Attention is All you Need" in result
        assert "Top Papers" in result

    @pytest.mark.asyncio
    @respx.mock
    async def test_author_not_found(self):
        respx.get(f"{S2_BASE_URL}/author/0000").mock(
            return_value=httpx.Response(404, json={"error": "not found"})
        )
        result = await semantic_scholar("author", "0000")
        assert "Not found" in result


# ---------------------------------------------------------------------------
# Invalid action
# ---------------------------------------------------------------------------

class TestSemanticScholarInvalidAction:
    @pytest.mark.asyncio
    async def test_unknown_action(self):
        result = await semantic_scholar("invalid_action", "test")
        assert "Unknown action" in result
        assert "invalid_action" in result
        assert "snippets" in result


# ---------------------------------------------------------------------------
# semantic_scholar — snippets
# ---------------------------------------------------------------------------

class TestSemanticScholarSnippets:
    @pytest.mark.asyncio
    @respx.mock
    async def test_snippets_with_paper_id(self):
        paper_id = "204e3073870fae3d05bcbc2f6a8e263d9b72e776"
        respx.get(f"{S2_BASE_URL}/paper/{paper_id}").mock(
            return_value=httpx.Response(200, json=S2_TEXT_AVAILABILITY_FULLTEXT)
        )
        respx.get(f"{S2_BASE_URL}/snippet/search").mock(
            return_value=httpx.Response(200, json=S2_SNIPPET_RESPONSE)
        )
        result = await semantic_scholar(
            "snippets", "multi-head attention", paper_id=paper_id
        )
        assert result.startswith("---\n")
        assert "api: Semantic Scholar" in result
        assert "action: snippets" in result
        assert "hint:" in result
        assert f"paper: {paper_id}" in result
        assert "### Multi-Head Attention" in result
        assert "jointly attend" in result
        assert "### Scaled Dot-Product Attention" in result

    @pytest.mark.asyncio
    @respx.mock
    async def test_snippets_corpus_wide(self):
        respx.get(f"{S2_BASE_URL}/snippet/search").mock(
            return_value=httpx.Response(200, json=S2_SNIPPET_CORPUS_RESPONSE)
        )
        result = await semantic_scholar("snippets", "multi-head attention")
        assert result.startswith("---\n")
        assert "api: Semantic Scholar" in result
        assert "action: snippets" in result
        assert "## Attention is All you Need" in result
        assert "## BERT" in result
        assert "### Multi-Head Attention" in result
        assert "### Model Architecture" in result

    @pytest.mark.asyncio
    @respx.mock
    async def test_snippets_no_full_text(self):
        paper_id = "204e3073870fae3d05bcbc2f6a8e263d9b72e776"
        respx.get(f"{S2_BASE_URL}/paper/{paper_id}").mock(
            return_value=httpx.Response(200, json=S2_TEXT_AVAILABILITY_NONE)
        )
        result = await semantic_scholar(
            "snippets", "attention", paper_id=paper_id
        )
        assert "Full text is not available" in result
        assert "paper action" in result

    @pytest.mark.asyncio
    @respx.mock
    async def test_snippets_empty_results(self):
        paper_id = "204e3073870fae3d05bcbc2f6a8e263d9b72e776"
        respx.get(f"{S2_BASE_URL}/paper/{paper_id}").mock(
            return_value=httpx.Response(200, json=S2_TEXT_AVAILABILITY_FULLTEXT)
        )
        respx.get(f"{S2_BASE_URL}/snippet/search").mock(
            return_value=httpx.Response(200, json={"data": []})
        )
        result = await semantic_scholar(
            "snippets", "nonexistent topic", paper_id=paper_id
        )
        assert "No snippet matches found" in result

    @pytest.mark.asyncio
    @respx.mock
    async def test_snippets_abstract_kind_tagged(self):
        paper_id = "204e3073870fae3d05bcbc2f6a8e263d9b72e776"
        respx.get(f"{S2_BASE_URL}/paper/{paper_id}").mock(
            return_value=httpx.Response(200, json=S2_TEXT_AVAILABILITY_FULLTEXT)
        )
        respx.get(f"{S2_BASE_URL}/snippet/search").mock(
            return_value=httpx.Response(200, json=S2_SNIPPET_RESPONSE)
        )
        result = await semantic_scholar(
            "snippets", "attention", paper_id=paper_id
        )
        # The abstract snippet should be tagged with [abstract]
        assert "[abstract]" in result
        # Body snippets should NOT be tagged
        assert "[body]" not in result


# ---------------------------------------------------------------------------
# S2 fast path (URL interception)
# ---------------------------------------------------------------------------

class TestS2FastPath:
    @pytest.mark.asyncio
    @respx.mock
    async def test_s2_url_intercepted(self):
        paper_id = "204e3073870fae3d05bcbc2f6a8e263d9b72e776"
        respx.get(f"{S2_BASE_URL}/paper/{paper_id}").mock(
            return_value=httpx.Response(200, json=S2_PAPER_DETAIL_RESPONSE)
        )
        url = f"https://www.semanticscholar.org/paper/Attention-Is-All-You-Need/{paper_id}"
        result = await _s2_fast_path(url)
        assert result is not None
        assert "Attention is All you Need" in result
        assert "api: Semantic Scholar" in result

    @pytest.mark.asyncio
    async def test_non_s2_url_returns_none(self):
        result = await _s2_fast_path("https://example.com/some-page")
        assert result is None

    @pytest.mark.asyncio
    @respx.mock
    async def test_api_error_still_returns_result(self):
        """API errors should still return a result (not None) to avoid CAPTCHA fallback."""
        paper_id = "204e3073870fae3d05bcbc2f6a8e263d9b72e776"
        respx.get(f"{S2_BASE_URL}/paper/{paper_id}").mock(
            return_value=httpx.Response(500)
        )
        url = f"https://www.semanticscholar.org/paper/{paper_id}"
        result = await _s2_fast_path(url)
        assert result is not None
        assert "Error" in result


# ---------------------------------------------------------------------------
# semantic_scholar: fields= override routing
# ---------------------------------------------------------------------------

class TestFieldsParamRouting:
    """`fields=` is honored by four actions and fixed on two. The two that
    fix it must say so: silently returning a different field set than the
    caller asked for is the failure mode this covers.
    """

    @pytest.mark.asyncio
    @respx.mock
    async def test_search_honors_fields(self):
        route = respx.get(f"{S2_BASE_URL}/paper/search").mock(
            return_value=httpx.Response(200, json=S2_PAPER_SEARCH_RESPONSE)
        )
        result = await semantic_scholar("search", "attention", fields="paperId,title")
        assert route.calls.last.request.url.params["fields"] == "paperId,title"
        assert "warning:" not in result

    @pytest.mark.asyncio
    @respx.mock
    async def test_references_honors_fields(self):
        paper_id = "204e3073870fae3d05bcbc2f6a8e263d9b72e776"
        route = respx.get(f"{S2_BASE_URL}/paper/{paper_id}/references").mock(
            return_value=httpx.Response(200, json=S2_REFERENCE_RESPONSE)
        )
        await semantic_scholar("references", paper_id, fields="paperId,title")
        assert route.calls.last.request.url.params["fields"] == "paperId,title"

    @pytest.mark.asyncio
    @respx.mock
    async def test_paper_fixes_fields_and_warns(self):
        paper_id = "204e3073870fae3d05bcbc2f6a8e263d9b72e776"
        route = respx.get(f"{S2_BASE_URL}/paper/{paper_id}").mock(
            return_value=httpx.Response(200, json=S2_PAPER_DETAIL_RESPONSE)
        )
        result = await semantic_scholar("paper", paper_id, fields="title")

        sent = route.calls.last.request.url.params["fields"]
        assert sent == _DETAIL_FIELDS, "caller's fields= must not reach the API"
        assert "warning:" in result
        assert "fields=" in result
        # Soft downgrade, not an error: the full response still comes back.
        assert "## Abstract" in result

    @pytest.mark.asyncio
    @respx.mock
    async def test_paper_without_fields_emits_no_warning(self):
        paper_id = "204e3073870fae3d05bcbc2f6a8e263d9b72e776"
        respx.get(f"{S2_BASE_URL}/paper/{paper_id}").mock(
            return_value=httpx.Response(200, json=S2_PAPER_DETAIL_RESPONSE)
        )
        result = await semantic_scholar("paper", paper_id)
        assert "warning:" not in result

    @pytest.mark.asyncio
    @respx.mock
    async def test_snippets_fixes_fields_and_warns(self):
        respx.get(f"{S2_BASE_URL}/snippet/search").mock(
            return_value=httpx.Response(200, json=S2_SNIPPET_RESPONSE)
        )
        result = await semantic_scholar("snippets", "attention", fields="title")
        assert "warning:" in result
        assert "fields=" in result

    def test_description_documents_every_divergence(self):
        """`fields=` behaves three ways across six actions, and the caller
        picks an action before seeing a response. Substring checks on action
        names are worthless here (`search` and `author` both occur inside
        `author_search`), so assert on the behavioral claims instead.
        """
        import inspect

        from parkour_mcp.semantic_scholar import (
            _FIXED_FIELD_ACTIONS,
            semantic_scholar,
        )

        param = inspect.signature(semantic_scholar).parameters["fields"]
        desc = " ".join(
            m.description for m in param.annotation.__metadata__
            if getattr(m, "description", None)
        )
        assert desc, "fields= has no description"

        for action in _FIXED_FIELD_ACTIONS:
            assert action in desc, (
                f"{action} fixes the field set but the description does not "
                "name it, so a caller cannot predict the downgrade"
            )
        assert "gnored" in desc, "description does not say what happens to fields="
        assert "paper list" in desc, "author's record/paper-list split undocumented"
        assert "subselection" in desc, "nested-subfield replacement undocumented"

    @pytest.mark.asyncio
    @respx.mock
    async def test_url_fast_path_needs_no_warning(self):
        """_fetch_s2_paper is also the URL fast path, which has no fields=."""
        paper_id = "204e3073870fae3d05bcbc2f6a8e263d9b72e776"
        respx.get(f"{S2_BASE_URL}/paper/{paper_id}").mock(
            return_value=httpx.Response(200, json=S2_PAPER_DETAIL_RESPONSE)
        )
        result = await _fetch_s2_paper(paper_id)
        assert "warning:" not in result


from pathlib import Path  # noqa: E402  # needed for test_429_without_key
