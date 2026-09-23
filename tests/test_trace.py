"""Wire trace: what a fetch records, when it stops, and how it renders."""

import dataclasses
import json

import httpx
import pytest
import respx

from parkour_mcp import _trace
from parkour_mcp.common import RateLimiter, guarded_fetch

URL = "https://export.arxiv.org/api/query"
ATOM = {"content-type": "application/atom+xml; charset=utf-8"}


@pytest.fixture(autouse=True)
def _no_sink(monkeypatch):
    monkeypatch.delenv(_trace.TRACE_ENV, raising=False)


# ---------------------------------------------------------------------------
# Recording on the httpx (API host) path
# ---------------------------------------------------------------------------

class TestHttpxRecord:
    @respx.mock
    @pytest.mark.asyncio
    async def test_records_the_request_as_sent_and_the_response(self):
        respx.get(URL).mock(return_value=httpx.Response(
            406, headers={"server": "Varnish", "via": "1.1 varnish", **ATOM},
        ))
        with _trace.collect() as seen:
            await guarded_fetch(
                URL, params={"search_query": 'abs:"vision tokens"', "max_results": 1},
                headers={"User-Agent": "parkour-test", "Accept": "application/atom+xml"},
            )
        assert len(seen) == 1
        ex = seen[0]
        assert ex.transport == "httpx"
        assert ex.method == "GET"
        # The URL is the encoded form that went on the wire, not the params dict.
        assert ex.url == f"{URL}?search_query=abs%3A%22vision+tokens%22&max_results=1"
        assert ex.request_headers["user-agent"] == "parkour-test"
        assert ex.request_headers["accept"] == "application/atom+xml"
        assert ex.status == 406
        assert ex.response_headers["server"] == "Varnish"
        assert ex.response_headers["via"] == "1.1 varnish"
        assert "content-type" in ex.response_headers
        assert ex.body_bytes == 0
        assert ex.elapsed_ms is not None
        assert ex.error is None
        assert ex.dry_run is False

    @respx.mock
    @pytest.mark.asyncio
    async def test_records_a_transport_failure(self):
        respx.get(URL).mock(side_effect=httpx.ConnectError("refused"))
        with _trace.collect() as seen, pytest.raises(httpx.ConnectError):
            await guarded_fetch(URL)
        assert len(seen) == 1
        assert seen[0].status is None
        assert seen[0].error == "ConnectError: refused"

    @respx.mock
    @pytest.mark.asyncio
    async def test_limiter_note_attaches_once(self):
        """The limiter that released a call is named on that exchange only.

        The next fetch, with no ``wait`` before it, records ``limiter=None``:
        that is the honest description of the arXiv paper action's HEAD
        against arxiv.org/html, which runs outside the API limiter.
        """
        respx.get(URL).mock(return_value=httpx.Response(200, headers=ATOM))
        limiter = RateLimiter(
            0.0, name="arXiv API", policy="three seconds", url="https://example/tou",
        )
        with _trace.collect() as seen:
            await limiter.wait()
            await guarded_fetch(URL)
            await guarded_fetch(URL)
        first, second = seen
        assert first.limiter is not None
        assert first.limiter.name == "arXiv API"
        assert first.limiter.interval == 0.0
        assert first.limiter.policy == "three seconds"
        assert first.limiter.url == "https://example/tou"
        assert second.limiter is None


class TestDryRun:
    @respx.mock
    @pytest.mark.asyncio
    async def test_records_then_stops_before_sending(self):
        route = respx.get(URL).mock(return_value=httpx.Response(200, headers=ATOM))
        with _trace.collect() as seen, _trace.dry_run(), pytest.raises(_trace.DryRunStop):
            await guarded_fetch(URL, params={"id_list": "2505.10465"})
        assert not route.called
        assert len(seen) == 1
        assert seen[0].dry_run is True
        assert seen[0].url == f"{URL}?id_list=2505.10465"
        assert seen[0].status is None

    def test_stop_is_not_an_exception_subclass(self):
        # Fast paths fall through on `except Exception`; a dry run must not
        # be one of the things they are willing to fall through on.
        assert not issubclass(_trace.DryRunStop, Exception)


class TestSink:
    @respx.mock
    @pytest.mark.asyncio
    async def test_writes_one_json_line_per_exchange(self, tmp_path, monkeypatch):
        path = tmp_path / "trace.jsonl"
        monkeypatch.setenv(_trace.TRACE_ENV, str(path))
        respx.get(URL).mock(return_value=httpx.Response(200, headers=ATOM))
        await guarded_fetch(URL)
        await guarded_fetch(URL)
        lines = path.read_text().splitlines()
        assert len(lines) == 2
        record = json.loads(lines[0])
        assert record["transport"] == "httpx"
        assert record["url"] == URL
        assert record["status"] == 200

    @respx.mock
    @pytest.mark.asyncio
    async def test_dash_writes_to_stderr(self, capsys, monkeypatch):
        monkeypatch.setenv(_trace.TRACE_ENV, "-")
        respx.get(URL).mock(return_value=httpx.Response(200, headers=ATOM))
        await guarded_fetch(URL)
        err = capsys.readouterr().err
        assert json.loads(err.strip())["status"] == 200


# ---------------------------------------------------------------------------
# curl rendering
# ---------------------------------------------------------------------------

def _exchange(**overrides) -> _trace.Exchange:
    base = _trace.Exchange(
        ts="2026-09-23T04:05:42.174+00:00",
        transport="httpx",
        method="GET",
        url=f"{URL}?search_query=abs%3A%22vision+tokens%22&max_results=1",
        request_headers={
            "host": "export.arxiv.org",
            "accept-encoding": "gzip, deflate",
            "connection": "keep-alive",
            "user-agent": "parkour-mcp/2.4.0 (test)",
            "accept": "application/atom+xml",
        },
        status=406,
        http_version="HTTP/2",
        elapsed_ms=131.0,
    )
    return dataclasses.replace(base, **overrides)


class TestRenderCurl:
    def test_limited_exchange_carries_the_contract_and_the_sleep(self):
        limiter = _trace.LimiterRecord(
            name="arXiv API", interval=3.0,
            policy="arXiv asks for one request every three seconds",
            url="https://info.arxiv.org/help/api/tou.html", waited=0.0,
        )
        out = _trace.render_curl(_exchange(limiter=limiter), index=1, total=1, tool="ArXiv")
        assert "# rate limit: arXiv API, one request every 3 s." in out
        assert "arXiv asks for one request every three seconds" in out
        assert "https://info.arxiv.org/help/api/tou.html" in out
        assert "parkour enforces this in-process; a shell loop must add it back" in out
        assert "sleep 3;" in out
        assert "exchange 1 of 1, ArXiv" in out
        assert "HTTP/2 406, 131 ms" in out

    def test_command_reproduces_the_request_bytes(self):
        out = _trace.render_curl(_exchange(), index=1, total=1)
        command = out[out.index("\ncurl ") + 1:]
        assert command.startswith("curl -sS")
        assert "--http2" in command
        assert "-A 'parkour-mcp/2.4.0 (test)'" in command
        assert "-H 'accept: application/atom+xml'" in command
        assert "-H 'accept-encoding: gzip, deflate'" in command
        assert "-H 'host:" not in command
        assert "'https://export.arxiv.org/api/query?search_query=abs%3A%22vision+tokens%22&max_results=1'" in command

    def test_unlimited_exchange_says_so(self):
        out = _trace.render_curl(_exchange(limiter=None), index=1, total=1)
        assert "no in-process limiter governed this request" in out
        assert "sleep" not in out

    def test_fractional_interval_is_printed_as_is(self):
        limiter = _trace.LimiterRecord(
            name="doi.org", interval=0.2, policy=None, url=None, waited=0.0,
        )
        out = _trace.render_curl(_exchange(limiter=limiter), index=1, total=1)
        assert "one request every 0.2 s." in out
        assert "sleep 0.2;" in out

    def test_wreq_exchange_warns_about_the_fingerprint(self):
        out = _trace.render_curl(
            _exchange(transport="wreq", emulation="Chrome149", http_version="HTTP/1.1"),
            index=1, total=1,
        )
        assert "wreq's Chrome149 fingerprint" in out
        assert "a refusal seen by curl is not evidence about the tool" in out
        assert "--http1.1" in out

    def test_dry_run_and_head_render(self):
        out = _trace.render_curl(
            _exchange(method="HEAD", status=None, http_version=None, dry_run=True),
            index=2, total=2,
        )
        assert "not sent (dry run)" in out
        assert "exchange 2 of 2" in out
        assert "-I" in out
        assert "--http" not in out
