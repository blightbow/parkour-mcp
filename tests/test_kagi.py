"""Tests for parkour_mcp.kagi — v1 search, v0 summarize island, error parsers."""

import json
from unittest.mock import patch, MagicMock

import httpx
import pytest
import requests
import respx

import parkour_mcp.kagi as kagi_mod
from parkour_mcp.kagi import (
    _check_balance,
    _extract_balance,
    _handle_v0_error,
    _handle_v1_error,
    search,
    summarize,
)


def _make_http_error(status_code: int, body: bytes = b"") -> requests.HTTPError:
    """Build a real requests.HTTPError with response attached, matching the
    shape kagiapi raises via ``response.raise_for_status()``."""
    response = requests.Response()
    response.status_code = status_code
    response._content = body
    response.url = "https://kagi.com/api/v0/search?q=test"
    err = requests.HTTPError(f"{status_code} Client Error for url: {response.url}")
    err.response = response
    return err


def _make_v1_http_status_error(status_code: int, body: dict | None = None) -> httpx.HTTPStatusError:
    """Build an httpx.HTTPStatusError matching the v1 envelope shape."""
    request = httpx.Request("POST", "https://kagi.com/api/v1/search")
    content = json.dumps(body).encode() if body is not None else b""
    response = httpx.Response(
        status_code,
        content=content,
        request=request,
        headers={"content-type": "application/json"} if body is not None else {},
    )
    return httpx.HTTPStatusError(
        f"{status_code} for {request.url}",
        request=request,
        response=response,
    )


# --- _extract_balance (v0) ---

class TestExtractBalance:
    def test_extracts_float_balance(self):
        assert _extract_balance({"meta": {"api_balance": 12.34}}) == 12.34

    def test_extracts_int_balance(self):
        assert _extract_balance({"meta": {"api_balance": 5}}) == 5.0

    def test_extracts_string_balance(self):
        assert _extract_balance({"meta": {"api_balance": "3.50"}}) == 3.50

    def test_returns_none_when_missing(self):
        assert _extract_balance({"meta": {}}) is None

    def test_returns_none_when_no_meta(self):
        assert _extract_balance({}) is None

    def test_returns_none_for_invalid_value(self):
        assert _extract_balance({"meta": {"api_balance": "not_a_number"}}) is None


# --- _check_balance and lockout (v0) ---

class TestCheckBalance:
    def setup_method(self):
        """Reset lockout state before each test."""
        kagi_mod._summarize_locked = False

    def test_no_warning_when_balance_healthy(self):
        warning = _check_balance({"meta": {"api_balance": 5.00}})
        assert warning is None

    def test_warning_when_balance_low(self):
        warning = _check_balance({"meta": {"api_balance": 0.50}})
        assert warning is not None
        assert "Kagi API balance low" in warning
        assert "$0.50" in warning

    def test_low_balance_sets_lockout(self):
        _check_balance({"meta": {"api_balance": 0.25}})
        assert kagi_mod._summarize_locked is True

    def test_healthy_balance_clears_lockout_for_non_summarize(self):
        kagi_mod._summarize_locked = True
        _check_balance({"meta": {"api_balance": 5.00}}, is_summarize=False)
        assert kagi_mod._summarize_locked is False

    def test_healthy_balance_does_not_clear_lockout_for_summarize(self):
        kagi_mod._summarize_locked = True
        _check_balance({"meta": {"api_balance": 5.00}}, is_summarize=True)
        assert kagi_mod._summarize_locked is True

    def test_no_meta_does_not_change_lockout(self):
        kagi_mod._summarize_locked = True
        _check_balance({})
        assert kagi_mod._summarize_locked is True

    def test_threshold_boundary_low(self):
        warning = _check_balance({"meta": {"api_balance": 0.99}})
        assert warning is not None
        assert kagi_mod._summarize_locked is True

    def test_threshold_boundary_at(self):
        warning = _check_balance({"meta": {"api_balance": 1.00}})
        assert warning is None
        assert kagi_mod._summarize_locked is False


# --- Lockout integration (v0 summarize) ---

class TestSummarizeLockout:
    """v1 search never touches the balance machinery (v1 dropped api_balance),
    so only the summarize-side lockout behaviors remain testable here.
    """

    def setup_method(self):
        kagi_mod._summarize_locked = False

    @pytest.mark.asyncio
    async def test_summarize_blocked_when_locked(self):
        kagi_mod._summarize_locked = True
        result = await summarize(url="https://example.com")
        assert "temporarily disabled" in result
        assert "low API balance" in result

    @pytest.mark.asyncio
    async def test_summarize_warns_on_low_balance(self):
        mock_client = MagicMock()
        mock_client.summarize.return_value = {
            "meta": {"api_balance": 0.10},
            "data": {"output": "Summary text here."},
        }

        with patch.object(kagi_mod, "get_client", return_value=mock_client):
            result = await summarize(url="https://example.com")

        assert "balance_warning:" in result
        assert "Summary text here." in result
        assert "$0.10" in result
        assert kagi_mod._summarize_locked is True


# --- _handle_v0_error (kagiapi-shaped exceptions) ---

class TestHandleV0Error:
    def test_recognizes_insufficient_credit_in_400_body(self):
        # Kagi v0 returns 400 (not 402) for wallet exhaustion; the structured
        # error code lives in the response body. requests.Response.__bool__
        # returns False for 4xx, so the body branch must guard with `is not None`.
        body = (
            b'{"meta":{"api_balance":0.0},"data":null,'
            b'"error":[{"code":101,"msg":"Insufficient credit to perform this request."}]}'
        )
        result = _handle_v0_error(_make_http_error(400, body))
        assert "Insufficient API credits" in result

    def test_recognizes_401_via_status_code(self):
        result = _handle_v0_error(_make_http_error(401))
        assert "Invalid API key" in result

    def test_recognizes_402_via_status_code(self):
        result = _handle_v0_error(_make_http_error(402))
        assert "Insufficient API credits" in result

    def test_falls_through_on_unrecognized_status(self):
        result = _handle_v0_error(_make_http_error(503))
        assert "503" in result

    def test_handles_exception_without_response(self):
        # Network errors (timeouts, DNS failures) raise without a response object.
        result = _handle_v0_error(requests.ConnectionError("connection refused"))
        assert "connection refused" in result


# --- _handle_v1_error (httpx-shaped exceptions) ---

class TestHandleV1Error:
    def test_unauthorized_via_envelope_code(self):
        body = {
            "meta": {"trace": "abc", "ms": 2, "node": "us-east4"},
            "data": None,
            "errors": [{
                "code": "general.unauthorized",
                "url": "https://kagi.com/api#todo",
                "message": "Unauthorized",
            }],
        }
        result = _handle_v1_error(_make_v1_http_status_error(401, body))
        assert "Invalid API key" in result

    def test_insufficient_credit_via_envelope_code(self):
        body = {
            "meta": {"trace": "abc", "ms": 1, "node": "us-east4"},
            "data": None,
            "errors": [{
                "code": "billing.insufficient_credit",
                "url": "https://kagi.com/api#todo",
                "message": "Insufficient credit",
            }],
        }
        result = _handle_v1_error(_make_v1_http_status_error(402, body))
        assert "Insufficient API credits" in result

    def test_other_envelope_message_passes_through(self):
        body = {
            "meta": {"trace": "abc", "ms": 1, "node": "us-east4"},
            "data": None,
            "errors": [{
                "code": "search.invalid_workflow",
                "url": "https://kagi.com/api#todo",
                "message": "Unknown workflow 'foo'",
            }],
        }
        result = _handle_v1_error(_make_v1_http_status_error(400, body))
        assert "Unknown workflow" in result
        assert "search.invalid_workflow" in result

    def test_fallback_to_status_code_without_envelope(self):
        result = _handle_v1_error(_make_v1_http_status_error(503))
        assert "503" in result
        assert "Kagi v1" in result

    def test_unauthorized_via_bare_401(self):
        # 401 with no parseable body still maps to the invalid-key message.
        result = _handle_v1_error(_make_v1_http_status_error(401))
        assert "Invalid API key" in result

    def test_timeout_message(self):
        result = _handle_v1_error(httpx.ReadTimeout("read timeout"))
        assert "timed out" in result

    def test_request_error_names_class(self):
        result = _handle_v1_error(httpx.ConnectError("connection refused"))
        assert "ConnectError" in result


# --- v1 search end-to-end (respx mocks) ---

@pytest.fixture
def _kagi_key(monkeypatch):
    """Provide a stable API key without touching the real config file."""
    monkeypatch.setenv("KAGI_API_KEY", "test-key")
    yield


class TestSearchV1:
    @pytest.mark.asyncio
    @respx.mock
    async def test_formats_search_and_related_results(self, _kagi_key):
        respx.post("https://kagi.com/api/v1/search").mock(
            return_value=httpx.Response(
                200,
                json={
                    "meta": {"trace": "t1", "ms": 100, "node": "us-east4"},
                    "data": {
                        "search": [
                            {
                                "url": "https://en.wikipedia.org/wiki/Steve_Jobs",
                                "title": "Steve Jobs - Wikipedia",
                                "snippet": "Co-founder of Apple Inc.",
                                "time": "2012-02-23T07:00:59Z",
                                "props": {"language": "en"},
                            },
                            {
                                "url": "https://example.com/jobs",
                                "title": "Jobs profile",
                                "snippet": "A biography.",
                                "props": {},
                            },
                        ],
                        "related_search": [
                            {"url": "/search?q=apple", "title": "apple", "snippet": ""},
                            {"url": "/search?q=ipod", "title": "ipod", "snippet": ""},
                        ],
                    },
                },
            )
        )

        result = await search("steve jobs", limit=2)

        assert "Steve Jobs - Wikipedia" in result
        assert "https://en.wikipedia.org/wiki/Steve_Jobs" in result
        assert "Co-founder of Apple Inc." in result
        assert "2012-02-23T07:00:59Z" in result
        # Result without a `time` should not emit an empty parenthesized suffix.
        assert "Jobs profile" in result
        assert "A biography." in result
        assert "Related searches: apple, ipod" in result

    @pytest.mark.asyncio
    @respx.mock
    async def test_no_results_emits_widen_query_hint(self, _kagi_key):
        respx.post("https://kagi.com/api/v1/search").mock(
            return_value=httpx.Response(
                200,
                json={
                    "meta": {"trace": "t2", "ms": 50, "node": "us-east4"},
                    "data": {"search": []},
                },
            )
        )

        result = await search("unlikely query", limit=5)

        assert "No results found." in result
        assert "Widen the query" in result

    @pytest.mark.asyncio
    @respx.mock
    async def test_sends_bearer_auth_and_post_body(self, _kagi_key):
        route = respx.post("https://kagi.com/api/v1/search").mock(
            return_value=httpx.Response(
                200,
                json={"meta": {"trace": "t3", "ms": 1, "node": "x"}, "data": {"search": []}},
            )
        )

        await search("test", limit=3)

        assert route.called
        sent = route.calls.last.request
        assert sent.headers["authorization"] == "Bearer test-key"
        body = json.loads(sent.content)
        assert body == {"query": "test", "limit": 3}

    @pytest.mark.asyncio
    @respx.mock
    async def test_401_routes_through_v1_error_handler(self, _kagi_key):
        respx.post("https://kagi.com/api/v1/search").mock(
            return_value=httpx.Response(
                401,
                json={
                    "meta": {"trace": "t4", "ms": 1, "node": "x"},
                    "data": None,
                    "errors": [{
                        "code": "general.unauthorized",
                        "url": "https://kagi.com/api#todo",
                        "message": "Unauthorized",
                    }],
                },
            )
        )

        result = await search("test")
        assert "Invalid API key" in result

    @pytest.mark.asyncio
    async def test_missing_key_short_circuits(self, monkeypatch, tmp_path):
        monkeypatch.delenv("KAGI_API_KEY", raising=False)
        monkeypatch.setattr(kagi_mod, "CONFIG_PATH", tmp_path / "missing")
        result = await search("test")
        assert "API key not found" in result


# --- v1 search args (workflow / lens_id / page / region / after / before) ---

class TestSearchV1Args:
    @pytest.mark.asyncio
    @respx.mock
    async def test_workflow_videos_renders_data_video(self, _kagi_key):
        respx.post("https://kagi.com/api/v1/search").mock(
            return_value=httpx.Response(
                200,
                json={
                    "meta": {"trace": "t", "ms": 1, "node": "x"},
                    "data": {
                        "video": [{
                            "url": "https://www.youtube.com/watch?v=abc",
                            "title": "Climate Change Explained",
                            "snippet": "A short overview.",
                            "time": "2024-03-15T10:00:00Z",
                            "props": {},
                        }],
                    },
                },
            )
        )

        result = await search("climate change", workflow="videos")

        assert "Climate Change Explained" in result
        assert "https://www.youtube.com/watch?v=abc" in result
        assert "kagi videos:" in result  # source label reflects workflow

    @pytest.mark.asyncio
    @respx.mock
    async def test_workflow_images_handles_missing_snippet(self, _kagi_key):
        # Image workflow items have no snippet field; formatter must omit
        # the dash-space prefix instead of emitting "[title](url) -".
        respx.post("https://kagi.com/api/v1/search").mock(
            return_value=httpx.Response(
                200,
                json={
                    "meta": {"trace": "t", "ms": 1, "node": "x"},
                    "data": {
                        "image": [{
                            "url": "https://example.com/photo.jpg",
                            "title": "Photo Title",
                            "image": {"url": "https://thumb.example.com/x.jpg"},
                            "props": {},
                        }],
                    },
                },
            )
        )

        result = await search("photo", workflow="images")

        assert "[Photo Title](https://example.com/photo.jpg)" in result
        # Specifically: no trailing " - " on the line for the image entry.
        assert "Photo Title](https://example.com/photo.jpg) -" not in result

    @pytest.mark.asyncio
    async def test_invalid_workflow_short_circuits(self, _kagi_key):
        # Static type rejects the Literal violation; the runtime guard
        # exists for callers that bypass static typing (MCP-incoming
        # values, **kwargs unpacking, dynamic dispatch).
        result = await search("test", workflow="audiobooks")  # ty: ignore[invalid-argument-type]
        assert "Invalid workflow" in result
        assert "audiobooks" in result

    @pytest.mark.asyncio
    @respx.mock
    async def test_lens_id_passes_through_to_body(self, _kagi_key):
        route = respx.post("https://kagi.com/api/v1/search").mock(
            return_value=httpx.Response(
                200,
                json={"meta": {"trace": "t", "ms": 1, "node": "x"}, "data": {"search": []}},
            )
        )

        await search("research", lens_id="academic-papers")

        sent = json.loads(route.calls.last.request.content)
        assert sent["lens_id"] == "academic-papers"

    @pytest.mark.asyncio
    @respx.mock
    async def test_page_passes_through_to_body(self, _kagi_key):
        route = respx.post("https://kagi.com/api/v1/search").mock(
            return_value=httpx.Response(
                200,
                json={"meta": {"trace": "t", "ms": 1, "node": "x"}, "data": {"search": []}},
            )
        )

        await search("paginated", page=3)

        sent = json.loads(route.calls.last.request.content)
        assert sent["page"] == 3

    @pytest.mark.asyncio
    async def test_page_out_of_range_short_circuits(self, _kagi_key):
        result = await search("test", page=11)
        assert "page must be between 1 and 10" in result

        result_zero = await search("test", page=0)
        assert "page must be between 1 and 10" in result_zero

    @pytest.mark.asyncio
    @respx.mock
    async def test_filters_assembled_from_region_after_before(self, _kagi_key):
        route = respx.post("https://kagi.com/api/v1/search").mock(
            return_value=httpx.Response(
                200,
                json={"meta": {"trace": "t", "ms": 1, "node": "x"}, "data": {"search": []}},
            )
        )

        await search("brexit", region="gb", after="2020-01-01", before="2020-12-31")

        sent = json.loads(route.calls.last.request.content)
        assert sent["filters"] == {
            "region": "gb",
            "after": "2020-01-01",
            "before": "2020-12-31",
        }

    @pytest.mark.asyncio
    @respx.mock
    async def test_no_filters_omits_filters_field(self, _kagi_key):
        route = respx.post("https://kagi.com/api/v1/search").mock(
            return_value=httpx.Response(
                200,
                json={"meta": {"trace": "t", "ms": 1, "node": "x"}, "data": {"search": []}},
            )
        )

        await search("plain query")

        sent = json.loads(route.calls.last.request.content)
        assert "filters" not in sent

    def test_regions_resource_markdown_shape(self):
        from parkour_mcp.kagi import kagi_regions_markdown
        md = kagi_regions_markdown()
        assert "# Kagi region codes" in md
        # Spot-check representative codes from each shape: ISO 3166 alpha-2
        # and the language-suffix variant.
        assert "| us | United States |" in md
        assert "| ca_fr | Canada (fr) |" in md
        # bangs.md lists `int` as a valid bang but the v1 search API
        # rejects it for filters.region; the resource must not advertise it.
        assert "| int |" not in md
        # The region-pins-language coupling has to be on the resource itself
        # since UAT kept asking about a separate language axis.
        assert "language" in md.lower()

    @pytest.mark.asyncio
    @respx.mock
    async def test_partial_filters_only_includes_set_fields(self, _kagi_key):
        route = respx.post("https://kagi.com/api/v1/search").mock(
            return_value=httpx.Response(
                200,
                json={"meta": {"trace": "t", "ms": 1, "node": "x"}, "data": {"search": []}},
            )
        )

        await search("recent", after="2025-01-01")

        sent = json.loads(route.calls.last.request.content)
        assert sent["filters"] == {"after": "2025-01-01"}
