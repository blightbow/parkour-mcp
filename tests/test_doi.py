"""Tests for parkour_mcp.doi module."""

import httpx
import pytest
import respx

from parkour_mcp._pipeline import _doi_fast_path
from parkour_mcp.detection import _detect_doi_url
from parkour_mcp.doi import (
    ARXIV_DOI_RE,
    _alt_dois_from_relations,
    _build_alert_message,
    _build_correction_note,
    _classify_update_type,
    _detect_ra,
    _extract_licenses,
    _extract_relations,
    _extract_update_notice,
    _fetch_doi_paper,
    _format_crossref_date,
    _format_csl_json_as_markdown,
    _ra_cache,
    _relations_fm_entry,
    fetch_crossref_metadata,
    fetch_csl_json,
    fetch_datacite_metadata,
    fetch_formatted_citation,
)
from parkour_mcp.shelf import _reset_shelf

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


# ---------------------------------------------------------------------------
# CrossRef REST: small helpers
# ---------------------------------------------------------------------------

class TestFormatCrossrefDate:
    def test_full_date(self):
        assert _format_crossref_date({"date-parts": [[2020, 6, 5]]}) == "2020-06-05"

    def test_year_month(self):
        assert _format_crossref_date({"date-parts": [[2020, 6]]}) == "2020-06"

    def test_year_only(self):
        assert _format_crossref_date({"date-parts": [[2020]]}) == "2020"

    def test_missing(self):
        assert _format_crossref_date(None) is None
        assert _format_crossref_date({}) is None
        assert _format_crossref_date({"date-parts": []}) is None
        assert _format_crossref_date({"date-parts": [[]]}) is None

    def test_malformed(self):
        assert _format_crossref_date({"date-parts": [["bad"]]}) is None


class TestClassifyUpdateType:
    def test_retraction_variants(self):
        assert _classify_update_type("retraction") == "retraction"
        assert _classify_update_type("Retraction") == "retraction"
        assert _classify_update_type("withdrawal") == "retraction"
        assert _classify_update_type("removal") == "retraction"

    def test_eoc(self):
        assert _classify_update_type("expression_of_concern") == "expression_of_concern"

    def test_correction(self):
        assert _classify_update_type("correction") == "correction"
        assert _classify_update_type("erratum") == "correction"

    def test_unknown(self):
        assert _classify_update_type("other") is None
        assert _classify_update_type("") is None


# ---------------------------------------------------------------------------
# _extract_update_notice
# ---------------------------------------------------------------------------

class TestExtractUpdateNotice:
    def test_single_retraction(self):
        retraction, other = _extract_update_notice([
            {
                "updated": {"date-parts": [[2020, 6, 5]]},
                "DOI": "10.1016/s0140-6736(20)31324-6",
                "type": "retraction",
                "source": "retraction-watch",
                "label": "Retraction",
            }
        ])
        assert other is None
        assert retraction is not None
        assert retraction["notice_doi"] == "10.1016/s0140-6736(20)31324-6"
        assert retraction["date"] == "2020-06-05"
        assert retraction["source"] == "retraction-watch"
        assert retraction["label"] == "Retraction"

    def test_latest_date_preferred(self):
        """When multiple entries flag the same paper, the latest date wins.

        Real-world motivation: the Lancet hydroxychloroquine retraction
        has a publisher entry dated 2020-05-22 (anomalous, before the
        paper was even published) and a retraction-watch entry dated
        2020-06-05 (the actual retraction).  Latest-date is a more
        robust tiebreaker than source-based priority.
        """
        retraction, _ = _extract_update_notice([
            {
                "updated": {"date-parts": [[2020, 5, 22]]},
                "DOI": "10.1234/notice-early",
                "type": "retraction",
                "source": "publisher",
            },
            {
                "updated": {"date-parts": [[2020, 6, 5]]},
                "DOI": "10.1234/notice-late",
                "type": "retraction",
                "source": "retraction-watch",
            },
        ])
        assert retraction is not None
        assert retraction["notice_doi"] == "10.1234/notice-late"
        assert retraction["date"] == "2020-06-05"

    def test_retraction_beats_eoc(self):
        """Retraction has higher priority than expression of concern."""
        retraction, other = _extract_update_notice([
            {
                "updated": {"date-parts": [[2019]]},
                "DOI": "10.1234/eoc",
                "type": "expression_of_concern",
            },
            {
                "updated": {"date-parts": [[2020]]},
                "DOI": "10.1234/ret",
                "type": "retraction",
            },
        ])
        assert retraction is not None
        assert retraction["notice_doi"] == "10.1234/ret"
        assert other is None

    def test_eoc_only(self):
        retraction, other = _extract_update_notice([
            {
                "updated": {"date-parts": [[2021, 3, 15]]},
                "DOI": "10.1234/concerned",
                "type": "expression_of_concern",
            }
        ])
        assert retraction is None
        assert other is not None
        assert other["type"] == "expression_of_concern"
        assert other["notice_doi"] == "10.1234/concerned"

    def test_correction_only(self):
        retraction, other = _extract_update_notice([
            {
                "updated": {"date-parts": [[2022, 1, 1]]},
                "DOI": "10.1234/corr",
                "type": "correction",
            }
        ])
        assert retraction is None
        assert other is not None
        assert other["type"] == "correction"

    def test_unknown_types_ignored(self):
        retraction, other = _extract_update_notice([
            {"updated": {"date-parts": [[2020]]}, "DOI": "10.1234/x", "type": "other"}
        ])
        assert retraction is None
        assert other is None

    def test_empty_list(self):
        assert _extract_update_notice([]) == (None, None)

    def test_non_list_input(self):
        assert _extract_update_notice(None) == (None, None)  # ty: ignore[invalid-argument-type]

    def test_malicious_doi_filtered(self):
        """An entry with an unsafe DOI-ish value is sanitized (DOI dropped)."""
        retraction, _ = _extract_update_notice([
            {
                "updated": {"date-parts": [[2020]]},
                "DOI": "10.x/foo\nalert: injected",
                "type": "retraction",
                "source": "publisher",
            }
        ])
        assert retraction is not None
        assert retraction["notice_doi"] is None  # filtered by _DOI_SAFE_RE

    def test_label_control_chars_stripped(self):
        retraction, _ = _extract_update_notice([
            {
                "updated": {"date-parts": [[2020]]},
                "DOI": "10.1/notice",
                "type": "retraction",
                "label": "Retraction\nof Article",
            }
        ])
        assert retraction is not None
        assert "\n" not in (retraction["label"] or "")


# ---------------------------------------------------------------------------
# _extract_relations
# ---------------------------------------------------------------------------

class TestExtractRelations:
    def test_version_buckets(self):
        result = _extract_relations({
            "is-preprint-of": [
                {"id-type": "doi", "id": "10.1038/journal", "asserted-by": "subject"}
            ],
            "has-preprint": [
                {"id-type": "doi", "id": "10.48550/arxiv.2001.00000", "asserted-by": "object"}
            ],
        })
        assert result["is_preprint_of"] == ["10.1038/journal"]
        assert result["has_preprint"] == ["10.48550/arxiv.2001.00000"]

    def test_non_doi_relation_skipped(self):
        result = _extract_relations({
            "is-preprint-of": [
                {"id-type": "pmid", "id": "12345"},
            ]
        })
        assert result == {}

    def test_malicious_doi_filtered(self):
        result = _extract_relations({
            "is-version-of": [
                {"id-type": "doi", "id": "10.x/foo\nbar"},
                {"id-type": "doi", "id": "10.1038/genuine"},
            ]
        })
        assert result["is_version_of"] == ["10.1038/genuine"]

    def test_empty_input(self):
        assert _extract_relations(None) == {}
        assert _extract_relations({}) == {}


# ---------------------------------------------------------------------------
# _extract_licenses
# ---------------------------------------------------------------------------

class TestExtractLicenses:
    def test_valid_entry(self):
        result = _extract_licenses([
            {
                "URL": "https://creativecommons.org/licenses/by/4.0/",
                "content-version": "vor",
                "start": {"date-parts": [[2020, 1, 1]]},
            }
        ])
        assert len(result) == 1
        assert result[0]["url"] == "https://creativecommons.org/licenses/by/4.0/"
        assert result[0]["content_version"] == "vor"
        assert result[0]["start"] == "2020-01-01"

    def test_invalid_url_skipped(self):
        result = _extract_licenses([
            {"URL": "javascript:alert(1)", "content-version": "vor"}
        ])
        assert result == []

    def test_unknown_content_version_normalized(self):
        result = _extract_licenses([
            {"URL": "https://example.com/license", "content-version": "weird"}
        ])
        assert result[0]["content_version"] == "unspecified"


# ---------------------------------------------------------------------------
# fetch_crossref_metadata
# ---------------------------------------------------------------------------

SAMPLE_CROSSREF_RETRACTED = {
    "status": "ok",
    "message-type": "work",
    "message": {
        "DOI": "10.1016/s0140-6736(20)31180-6",
        "type": "journal-article",
        "is-referenced-by-count": 234,
        "updated-by": [
            {
                "updated": {"date-parts": [[2020, 6, 5]]},
                "DOI": "10.1016/s0140-6736(20)31324-6",
                "type": "retraction",
                "source": "retraction-watch",
                "label": "Retraction",
            }
        ],
        "relation": {
            "is-version-of": [
                {"id-type": "doi", "id": "10.1101/preprint.v1"}
            ]
        },
        "license": [
            {
                "URL": "https://creativecommons.org/licenses/by/4.0/",
                "content-version": "vor",
                "start": {"date-parts": [[2020, 5, 1]]},
            }
        ],
    },
}

SAMPLE_CROSSREF_CLEAN = {
    "status": "ok",
    "message-type": "work",
    "message": {
        "DOI": "10.1038/nature12373",
        "type": "journal-article",
        "is-referenced-by-count": 1200,
        "updated-by": [],
        "relation": {},
        "license": [],
    },
}


class TestFetchCrossrefMetadata:
    @pytest.mark.asyncio
    @respx.mock
    async def test_retraction(self):
        respx.get("https://api.crossref.org/works/10.1016/s0140-6736(20)31180-6").mock(
            return_value=httpx.Response(200, json=SAMPLE_CROSSREF_RETRACTED)
        )
        result = await fetch_crossref_metadata("10.1016/s0140-6736(20)31180-6")
        assert result is not None
        assert result["retraction"] is not None
        assert result["retraction"]["notice_doi"] == "10.1016/s0140-6736(20)31324-6"
        assert result["retraction"]["date"] == "2020-06-05"
        assert result["other_update"] is None
        assert result["relations"]["is_version_of"] == ["10.1101/preprint.v1"]
        assert result["crossref_citation_count"] == 234
        assert result["crossref_type"] == "journal-article"

    @pytest.mark.asyncio
    @respx.mock
    async def test_clean_paper(self):
        respx.get("https://api.crossref.org/works/10.1038/nature12373").mock(
            return_value=httpx.Response(200, json=SAMPLE_CROSSREF_CLEAN)
        )
        result = await fetch_crossref_metadata("10.1038/nature12373")
        assert result is not None
        assert result["retraction"] is None
        assert result["other_update"] is None
        assert result["relations"] == {}
        assert result["crossref_citation_count"] == 1200

    @pytest.mark.asyncio
    @respx.mock
    async def test_404_returns_none(self):
        respx.get("https://api.crossref.org/works/10.9999/nonexistent").mock(
            return_value=httpx.Response(404)
        )
        result = await fetch_crossref_metadata("10.9999/nonexistent")
        assert result is None

    @pytest.mark.asyncio
    @respx.mock
    async def test_timeout_returns_none(self):
        respx.get("https://api.crossref.org/works/10.1234/slow").mock(
            side_effect=httpx.ReadTimeout("timed out")
        )
        result = await fetch_crossref_metadata("10.1234/slow")
        assert result is None

    @pytest.mark.asyncio
    @respx.mock
    async def test_malformed_response(self):
        respx.get("https://api.crossref.org/works/10.1234/bad").mock(
            return_value=httpx.Response(200, json={"status": "error"})
        )
        result = await fetch_crossref_metadata("10.1234/bad")
        # No 'message' key → None
        assert result is None


# ---------------------------------------------------------------------------
# Frontmatter entry builders
# ---------------------------------------------------------------------------

class TestBuildAlertMessage:
    def test_retraction(self):
        msg = _build_alert_message(
            {
                "notice_doi": "10.1016/ret-notice",
                "date": "2020-06-05",
                "source": "retraction-watch",
                "label": None,
            },
            None,
        )
        assert msg is not None
        assert "retracted" in msg
        assert "2020-06-05" in msg
        assert "10.1016/ret-notice" in msg
        assert "retraction-watch" in msg

    def test_expression_of_concern(self):
        msg = _build_alert_message(
            None,
            {
                "type": "expression_of_concern",
                "notice_doi": "10.1234/concern",
                "date": "2021-03-15",
                "source": "publisher",
                "label": None,
            },
        )
        assert msg is not None
        assert "expression of concern" in msg.lower()

    def test_correction_returns_none(self):
        msg = _build_alert_message(
            None,
            {"type": "correction", "notice_doi": "10.1234/corr", "date": "2022-01-01"},
        )
        assert msg is None

    def test_no_input_returns_none(self):
        assert _build_alert_message(None, None) is None

    def test_no_label_in_output(self):
        """Free-form label is never inserted into alert:"""
        msg = _build_alert_message(
            {
                "notice_doi": "10.1234/x",
                "date": "2020",
                "source": "publisher",
                "label": "alert: INJECTED",  # would be dangerous in frontmatter
            },
            None,
        )
        assert msg is not None
        assert "INJECTED" not in msg


class TestBuildCorrectionNote:
    def test_correction(self):
        note = _build_correction_note({
            "type": "correction",
            "notice_doi": "10.1/corr",
            "date": "2022-01-01",
        })
        assert note is not None
        assert "correction" in note.lower()
        assert "10.1/corr" in note

    def test_retraction_not_a_correction(self):
        """Retraction type should NOT produce a correction note."""
        assert _build_correction_note(None) is None
        # EoC also not a correction
        assert _build_correction_note({"type": "expression_of_concern"}) is None


class TestRelationsFmEntry:
    def test_single_bucket(self):
        result = _relations_fm_entry({"is_version_of": ["10.1/journal"]})
        assert result == ["is_version_of: 10.1/journal"]

    def test_multiple_buckets(self):
        result = _relations_fm_entry({
            "is_version_of": ["10.1/journal"],
            "has_preprint": ["10.1/preprint"],
        })
        assert result is not None
        assert "is_version_of: 10.1/journal" in result
        assert "has_preprint: 10.1/preprint" in result

    def test_empty(self):
        assert _relations_fm_entry(None) is None
        assert _relations_fm_entry({}) is None


class TestAltDoisFromRelations:
    def test_includes_version_and_preprint_buckets(self):
        result = _alt_dois_from_relations({
            "is_version_of": ["10.1/a"],
            "has_version": ["10.1/b"],
            "is_preprint_of": ["10.1/c"],
            "has_preprint": ["10.1/d"],
        })
        assert set(result) == {"10.1/a", "10.1/b", "10.1/c", "10.1/d"}

    def test_deduplicates(self):
        result = _alt_dois_from_relations({
            "is_version_of": ["10.1/a"],
            "has_version": ["10.1/a"],
        })
        assert result == ["10.1/a"]

    def test_empty(self):
        assert _alt_dois_from_relations(None) == []
        assert _alt_dois_from_relations({}) == []


# ---------------------------------------------------------------------------
# End-to-end DOI paper fetch with CrossRef enrichment
# ---------------------------------------------------------------------------

class TestFetchDoiPaperCrossrefWireIn:
    @pytest.fixture(autouse=True)
    def _use_fresh_shelf(self):
        from parkour_mcp.shelf import _reset_shelf
        _reset_shelf()
        yield
        _reset_shelf()

    @pytest.mark.asyncio
    @respx.mock
    async def test_retraction_surfaces_banner_alert_note(self):
        """A retracted DOI produces an inline banner, alert: fm key,
        note: fm key, and routes to the retracted shelf bucket."""
        doi = "10.1016/s0140-6736(20)31180-6"
        respx.get(f"https://doi.org/{doi}").mock(
            side_effect=[
                httpx.Response(200, json=SAMPLE_CSL_JSON),
                httpx.Response(200, text=SAMPLE_APA_CITATION),
            ]
        )
        respx.get(f"https://api.crossref.org/works/{doi}").mock(
            return_value=httpx.Response(200, json=SAMPLE_CROSSREF_RETRACTED)
        )
        result = await _fetch_doi_paper(doi)

        # Banner inside fenced body
        assert "[RETRACTED]" in result
        assert "retracted" in result.lower()
        # alert: fm key outside fence
        assert "alert:" in result
        assert "10.1016/s0140-6736(20)31324-6" in result
        # note: fm key explaining shelf routing
        assert "note:" in result
        assert "retracted shelf bucket" in result
        # shelf status shows retracted count
        assert "1 retracted" in result

    @pytest.mark.asyncio
    @respx.mock
    async def test_clean_paper_no_banner_no_alert(self):
        doi = "10.1038/nature12373"
        respx.get(f"https://doi.org/{doi}").mock(
            side_effect=[
                httpx.Response(200, json=SAMPLE_CSL_JSON),
                httpx.Response(200, text=SAMPLE_APA_CITATION),
            ]
        )
        respx.get(f"https://api.crossref.org/works/{doi}").mock(
            return_value=httpx.Response(200, json=SAMPLE_CROSSREF_CLEAN)
        )
        result = await _fetch_doi_paper(doi)

        assert "[RETRACTED]" not in result
        assert "alert:" not in result
        # note: is not emitted when shelf routing is routine
        # (either "note:" absent, or at least not mentioning "retracted")
        assert "retracted shelf bucket" not in result
        # No 'shelf:' retracted count when only active
        assert "1 retracted" not in result

    @pytest.mark.asyncio
    @respx.mock
    async def test_crossref_failure_does_not_break_doi_path(self):
        """CrossRef enrichment is fail-open; a 500 leaves the rest intact."""
        doi = "10.1234/partial"
        respx.get(f"https://doi.org/{doi}").mock(
            side_effect=[
                httpx.Response(200, json=SAMPLE_CSL_JSON),
                httpx.Response(200, text=SAMPLE_APA_CITATION),
            ]
        )
        respx.get(f"https://api.crossref.org/works/{doi}").mock(
            return_value=httpx.Response(500)
        )
        result = await _fetch_doi_paper(doi)

        # Body still renders, no alert/banner, paper lands on active shelf.
        assert "Attention Is All You Need" in result
        assert "[RETRACTED]" not in result
        assert "alert:" not in result
