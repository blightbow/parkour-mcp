"""Tests for kagi_research_mcp.doi module."""

import httpx
import pytest
import respx

from kagi_research_mcp.doi import (
    DOI_URL_RE,
    ARXIV_DOI_RE,
    _detect_doi_url,
    _detect_ra,
    _ra_cache,
    _fetch_doi_paper,
    fetch_formatted_citation,
    fetch_csl_json,
    fetch_datacite_metadata,
    _format_csl_json_as_markdown,
)
from kagi_research_mcp._pipeline import _doi_fast_path
from kagi_research_mcp.shelf import _reset_shelf


# ---------------------------------------------------------------------------
# URL detection
# ---------------------------------------------------------------------------

class TestDetectDoiUrl:
    def test_standard_doi_url(self):
        assert _detect_doi_url("https://doi.org/10.1234/foo") == "10.1234/foo"

    def test_dx_doi_url(self):
        assert _detect_doi_url("https://dx.doi.org/10.5281/zenodo.123") == "10.5281/zenodo.123"

    def test_http_scheme(self):
        assert _detect_doi_url("http://doi.org/10.6084/m9.figshare.123") == "10.6084/m9.figshare.123"

    def test_non_doi_url(self):
        assert _detect_doi_url("https://arxiv.org/abs/1706.03762") is None

    def test_doi_url_with_query_params(self):
        assert _detect_doi_url("https://doi.org/10.1234/foo?type=bar") == "10.1234/foo?type=bar"


class TestArxivDoiRegex:
    def test_matches_arxiv_doi(self):
        m = ARXIV_DOI_RE.match("10.48550/arXiv.1706.03762")
        assert m is not None
        assert m.group(1) == "1706.03762"

    def test_versioned_arxiv_doi(self):
        m = ARXIV_DOI_RE.match("10.48550/arXiv.1706.03762v7")
        assert m is not None
        assert m.group(1) == "1706.03762v7"

    def test_non_arxiv_doi(self):
        assert ARXIV_DOI_RE.match("10.1234/foo") is None


# ---------------------------------------------------------------------------
# fetch_formatted_citation
# ---------------------------------------------------------------------------

SAMPLE_APA_CITATION = (
    "Vaswani, A., Shazeer, N., Parmar, N., Uszkoreit, J., Jones, L., "
    "Gomez, A. N., Kaiser, Ł., & Polosukhin, I. (2017). "
    "Attention is all you need. Advances in Neural Information Processing Systems, 30."
)


class TestFetchFormattedCitation:
    @pytest.mark.asyncio
    @respx.mock
    async def test_success(self):
        respx.get("https://doi.org/10.48550/arXiv.1706.03762").mock(
            return_value=httpx.Response(200, text=SAMPLE_APA_CITATION)
        )
        result = await fetch_formatted_citation("10.48550/arXiv.1706.03762")
        assert result is not None
        assert "Vaswani" in result

    @pytest.mark.asyncio
    @respx.mock
    async def test_http_error_returns_none(self):
        respx.get("https://doi.org/10.9999/nonexistent").mock(
            return_value=httpx.Response(404)
        )
        result = await fetch_formatted_citation("10.9999/nonexistent")
        assert result is None

    @pytest.mark.asyncio
    @respx.mock
    async def test_timeout_returns_none(self):
        respx.get("https://doi.org/10.1234/timeout").mock(
            side_effect=httpx.ReadTimeout("timed out")
        )
        result = await fetch_formatted_citation("10.1234/timeout")
        assert result is None

    @pytest.mark.asyncio
    @respx.mock
    async def test_empty_response_returns_none(self):
        respx.get("https://doi.org/10.1234/empty").mock(
            return_value=httpx.Response(200, text="   ")
        )
        result = await fetch_formatted_citation("10.1234/empty")
        assert result is None


# ---------------------------------------------------------------------------
# fetch_csl_json
# ---------------------------------------------------------------------------

SAMPLE_CSL_JSON = {
    "type": "article",
    "DOI": "10.48550/ARXIV.1706.03762",
    "title": "Attention Is All You Need",
    "author": [
        {"family": "Vaswani", "given": "Ashish"},
        {"family": "Shazeer", "given": "Noam"},
    ],
    "issued": {"date-parts": [[2017]]},
    "publisher": "arXiv",
}


class TestFetchCslJson:
    @pytest.mark.asyncio
    @respx.mock
    async def test_success(self):
        respx.get("https://doi.org/10.48550/arXiv.1706.03762").mock(
            return_value=httpx.Response(200, json=SAMPLE_CSL_JSON)
        )
        result = await fetch_csl_json("10.48550/arXiv.1706.03762")
        assert result is not None
        assert result["title"] == "Attention Is All You Need"
        assert result["author"][0]["family"] == "Vaswani"

    @pytest.mark.asyncio
    @respx.mock
    async def test_http_error_returns_none(self):
        respx.get("https://doi.org/10.9999/missing").mock(
            return_value=httpx.Response(406)
        )
        result = await fetch_csl_json("10.9999/missing")
        assert result is None

    @pytest.mark.asyncio
    @respx.mock
    async def test_timeout_returns_none(self):
        respx.get("https://doi.org/10.1234/slow").mock(
            side_effect=httpx.ReadTimeout("timed out")
        )
        result = await fetch_csl_json("10.1234/slow")
        assert result is None


# ---------------------------------------------------------------------------
# _fetch_doi_paper
# ---------------------------------------------------------------------------

class TestFetchDoiPaper:
    @pytest.fixture(autouse=True)
    def _use_fresh_shelf(self):
        _reset_shelf()
        yield
        _reset_shelf()

    @pytest.mark.asyncio
    @respx.mock
    async def test_formats_csl_json(self):
        respx.get("https://doi.org/10.6084/m9.figshare.5616445").mock(
            side_effect=[
                httpx.Response(200, json=SAMPLE_CSL_JSON),
                httpx.Response(200, text=SAMPLE_APA_CITATION),
            ]
        )
        result = await _fetch_doi_paper("10.6084/m9.figshare.5616445")
        assert "Attention Is All You Need" in result
        assert "Vaswani" in result
        assert "api: DOI" in result
        assert "## Citation" in result

    @pytest.mark.asyncio
    @respx.mock
    async def test_both_fail_returns_error(self):
        respx.get("https://doi.org/10.9999/gone").mock(
            return_value=httpx.Response(404)
        )
        result = await _fetch_doi_paper("10.9999/gone")
        assert "Error" in result
        assert "Could not resolve" in result

    @pytest.mark.asyncio
    @respx.mock
    async def test_csl_only_no_citation(self):
        """When citation fetch fails but CSL-JSON succeeds, output is still complete."""
        respx.get("https://doi.org/10.1234/partial").mock(
            side_effect=[
                httpx.Response(200, json=SAMPLE_CSL_JSON),
                httpx.Response(406),
            ]
        )
        result = await _fetch_doi_paper("10.1234/partial")
        assert "Attention Is All You Need" in result
        assert "## Citation" not in result

    @pytest.mark.asyncio
    @respx.mock
    async def test_tracks_on_shelf(self):
        respx.get("https://doi.org/10.6084/m9.figshare.5616445").mock(
            side_effect=[
                httpx.Response(200, json=SAMPLE_CSL_JSON),
                httpx.Response(200, text=SAMPLE_APA_CITATION),
            ]
        )
        result = await _fetch_doi_paper("10.6084/m9.figshare.5616445")
        assert "shelf:" in result


# ---------------------------------------------------------------------------
# _doi_fast_path
# ---------------------------------------------------------------------------

class TestDoiFastPath:
    @pytest.fixture(autouse=True)
    def _use_fresh_shelf(self):
        _reset_shelf()
        yield
        _reset_shelf()

    @pytest.mark.asyncio
    @respx.mock
    async def test_doi_url_intercepted(self):
        respx.get("https://doi.org/10.6084/m9.figshare.5616445").mock(
            side_effect=[
                httpx.Response(200, json=SAMPLE_CSL_JSON),
                httpx.Response(200, text=SAMPLE_APA_CITATION),
            ]
        )
        result = await _doi_fast_path("https://doi.org/10.6084/m9.figshare.5616445")
        assert result is not None
        assert "Attention Is All You Need" in result
        assert "api: DOI" in result

    @pytest.mark.asyncio
    async def test_non_doi_url_returns_none(self):
        result = await _doi_fast_path("https://example.com/page")
        assert result is None

    @pytest.mark.asyncio
    async def test_dx_doi_url_detected(self):
        """dx.doi.org URLs should also be detected."""
        # Will fail at content negotiation but should not return None
        result = await _doi_fast_path("https://dx.doi.org/10.9999/test")
        # Should attempt resolution, not return None
        assert result is not None  # returns error string, not None


# ---------------------------------------------------------------------------
# RA detection
# ---------------------------------------------------------------------------

class TestDetectRA:
    @pytest.fixture(autouse=True)
    def _clear_ra_cache(self):
        _ra_cache.clear()
        yield
        _ra_cache.clear()

    @pytest.mark.asyncio
    @respx.mock
    async def test_datacite_prefix(self):
        respx.get("https://doi.org/doiRA/10.5281").mock(
            return_value=httpx.Response(200, json=[{"RA": "DataCite"}])
        )
        ra = await _detect_ra("10.5281/zenodo.123")
        assert ra == "DataCite"

    @pytest.mark.asyncio
    @respx.mock
    async def test_crossref_prefix(self):
        respx.get("https://doi.org/doiRA/10.1038").mock(
            return_value=httpx.Response(200, json=[{"RA": "Crossref"}])
        )
        ra = await _detect_ra("10.1038/nature12373")
        assert ra == "Crossref"

    @pytest.mark.asyncio
    @respx.mock
    async def test_prefix_cache_hit(self):
        route = respx.get("https://doi.org/doiRA/10.5281").mock(
            return_value=httpx.Response(200, json=[{"RA": "DataCite"}])
        )
        await _detect_ra("10.5281/zenodo.123")
        await _detect_ra("10.5281/zenodo.456")
        assert route.call_count == 1  # cached after first call

    @pytest.mark.asyncio
    @respx.mock
    async def test_api_failure_returns_none(self):
        respx.get("https://doi.org/doiRA/10.9999").mock(
            return_value=httpx.Response(500)
        )
        ra = await _detect_ra("10.9999/test")
        assert ra is None


# ---------------------------------------------------------------------------
# DataCite metadata
# ---------------------------------------------------------------------------

SAMPLE_DATACITE_RESPONSE = {
    "data": {
        "attributes": {
            "creators": [
                {
                    "name": "Cope, Jez",
                    "givenName": "Jez",
                    "familyName": "Cope",
                    "nameIdentifiers": [
                        {
                            "nameIdentifier": "https://orcid.org/0000-0003-3629-1383",
                            "nameIdentifierScheme": "ORCID",
                        }
                    ],
                },
                {
                    "name": "Hardeman, Megan",
                    "nameIdentifiers": [],
                },
            ],
            "rightsList": [
                {
                    "rights": "Creative Commons Attribution 4.0 International",
                    "rightsUri": "https://creativecommons.org/licenses/by/4.0/legalcode",
                    "rightsIdentifier": "cc-by-4.0",
                    "rightsIdentifierScheme": "SPDX",
                }
            ],
            "relatedIdentifiers": [
                {
                    "relatedIdentifier": "10.6084/m9.figshare.5616445",
                    "relatedIdentifierType": "DOI",
                    "relationType": "IsIdenticalTo",
                }
            ],
            "types": {"resourceTypeGeneral": "Audiovisual"},
        }
    }
}


class TestFetchDataciteMetadata:
    @pytest.mark.asyncio
    @respx.mock
    async def test_success(self):
        respx.get("https://api.datacite.org/dois/10.6084/m9.figshare.5616445").mock(
            return_value=httpx.Response(200, json=SAMPLE_DATACITE_RESPONSE)
        )
        result = await fetch_datacite_metadata("10.6084/m9.figshare.5616445")
        assert result is not None
        assert result["orcids"]["Cope, Jez"] == "0000-0003-3629-1383"
        assert "Hardeman, Megan" not in result["orcids"]
        assert result["license_id"] == "cc-by-4.0"
        assert "creativecommons.org" in result["license_url"]
        assert result["resource_type"] == "Audiovisual"
        assert len(result["related"]) == 1

    @pytest.mark.asyncio
    @respx.mock
    async def test_failure_returns_none(self):
        respx.get("https://api.datacite.org/dois/10.9999/fake").mock(
            return_value=httpx.Response(404)
        )
        result = await fetch_datacite_metadata("10.9999/fake")
        assert result is None


# ---------------------------------------------------------------------------
# CSL-JSON formatting with DataCite enrichment
# ---------------------------------------------------------------------------

class TestFormatCslJsonWithDatacite:
    def test_orcids_from_datacite(self):
        datacite = {
            "orcids": {"Vaswani, Ashish": "0000-0002-1234-5678"},
            "license_id": "cc-by-4.0",
            "license_url": "https://creativecommons.org/licenses/by/4.0/legalcode",
            "resource_type": "Dataset",
            "related": [],
        }
        result = _format_csl_json_as_markdown(SAMPLE_CSL_JSON, datacite=datacite)
        assert "[ORCID](https://orcid.org/0000-0002-1234-5678)" in result
        assert "cc-by-4.0" in result
        assert "Dataset" in result

    def test_without_datacite(self):
        result = _format_csl_json_as_markdown(SAMPLE_CSL_JSON)
        assert "ORCID" not in result
        # Falls back to CSL-JSON type
        assert "article" in result

    def test_spdx_license_preferred_over_copyright(self):
        csl = dict(SAMPLE_CSL_JSON)
        csl["copyright"] = "All rights reserved"
        datacite = {
            "orcids": {},
            "license_id": "cc-by-4.0",
            "license_url": "https://creativecommons.org/licenses/by/4.0/",
            "resource_type": None,
            "related": [],
        }
        result = _format_csl_json_as_markdown(csl, datacite=datacite)
        assert "cc-by-4.0" in result
        assert "All rights reserved" not in result
