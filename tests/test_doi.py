"""Tests for kagi_research_mcp.doi module."""

import httpx
import pytest
import respx

from kagi_research_mcp.doi import (
    DOI_URL_RE,
    ARXIV_DOI_RE,
    _detect_doi_url,
    fetch_formatted_citation,
    fetch_csl_json,
)


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
