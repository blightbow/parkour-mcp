"""``parkour-mcp call``: the production path from the shell."""

import argparse
import json

import httpx
import pytest
import respx

import parkour_mcp
from parkour_mcp import _cli, _trace
from parkour_mcp.arxiv import ARXIV_API_URL

from .test_arxiv import ARXIV_SINGLE_ENTRY_XML

CATALOG = parkour_mcp._ALWAYS_ON_TOOLS


def _parse(argv: list[str]) -> argparse.Namespace:
    return parkour_mcp._build_parser().parse_args(argv)


class TestParser:
    def test_no_subcommand_keeps_the_server_invocation(self):
        args = _parse(["--profile", "code"])
        assert args.command is None
        assert args.profile == "code"

    def test_call_arguments(self):
        args = _parse([
            "call", "ArXiv", '{"action": "paper"}', "--dry-run", "--as-curl",
            "--http1", "--record", "out.json",
        ])
        assert args.command == "call"
        assert args.tool == "ArXiv"
        assert json.loads(args.args) == {"action": "paper"}
        assert args.dry_run and args.as_curl and args.http1
        assert args.record == "out.json"


class TestResolveTool:
    @pytest.mark.parametrize("spelling", ["arxiv", "ArXiv", "ARXIV"])
    def test_every_profile_spelling_resolves(self, spelling):
        key, func = _cli.resolve_tool(spelling, CATALOG)
        assert key == "arxiv"
        assert func is parkour_mcp.arxiv

    def test_fetch_tool_by_code_name(self):
        key, _ = _cli.resolve_tool("WebFetchIncisive", CATALOG)
        assert key == "web_fetch_direct"

    def test_unknown_tool_lists_the_known_ones(self):
        with pytest.raises(SystemExit, match="unknown tool 'nope'.*arxiv"):
            _cli.resolve_tool("nope", CATALOG)


class TestRunCall:
    @respx.mock
    def test_runs_the_tool_through_the_production_path(self, capsys):
        route = respx.get(ARXIV_API_URL).mock(
            return_value=httpx.Response(200, text=ARXIV_SINGLE_ENTRY_XML)
        )
        args = _parse(["call", "arxiv", '{"action": "search", "query": "ti:attention"}'])
        status = _cli.run_call(args, catalog=CATALOG, profile="code")
        assert status == 0
        assert route.called
        out = capsys.readouterr().out
        assert "Attention Is All You Need" in out
        assert out.startswith("---")

    @respx.mock
    def test_as_curl_prints_the_contract_behind_the_command(self, capsys):
        respx.get(ARXIV_API_URL).mock(
            return_value=httpx.Response(200, text=ARXIV_SINGLE_ENTRY_XML)
        )
        args = _parse([
            "call", "arxiv", '{"action": "search", "query": "ti:attention"}', "--as-curl",
        ])
        _cli.run_call(args, catalog=CATALOG, profile="code")
        out = capsys.readouterr().out
        assert "# rate limit: arXiv API, one request every 3 s." in out
        assert "sleep 3;" in out
        assert "exchange 1 of 1, ArXiv" in out
        assert "search_query=ti%3Aattention" in out
        assert "curl -sS" in out

    @respx.mock
    def test_dry_run_sends_nothing_and_renders_the_request(self, capsys):
        route = respx.get(ARXIV_API_URL).mock(return_value=httpx.Response(200))
        args = _parse([
            "call", "arxiv", '{"action": "paper", "query": "2505.10465"}',
            "--dry-run", "--as-curl",
        ])
        _cli.run_call(args, catalog=CATALOG, profile="code")
        assert not route.called
        out = capsys.readouterr().out
        assert "not sent (dry run)" in out
        assert "id_list=2505.10465" in out
        assert "# rate limit: arXiv API" in out

    def test_trace_flag_sets_the_sink(self, tmp_path, monkeypatch):
        monkeypatch.delenv(_trace.TRACE_ENV, raising=False)
        path = tmp_path / "t.jsonl"

        async def stub(**_kwargs):
            return "ok"

        args = _parse(["call", "stub", "{}", "--trace", str(path)])
        _cli.run_call(args, catalog=[("stub", stub)], profile="code")
        assert __import__("os").environ[_trace.TRACE_ENV] == str(path)

    @respx.mock
    def test_record_writes_a_fixture_of_the_responses(self, tmp_path, capsys):
        respx.get(ARXIV_API_URL).mock(
            return_value=httpx.Response(200, text=ARXIV_SINGLE_ENTRY_XML)
        )
        path = tmp_path / "arxiv.json"
        args = _parse([
            "call", "arxiv", '{"action": "search", "query": "ti:attention"}',
            "--record", str(path),
        ])
        _cli.run_call(args, catalog=CATALOG, profile="code")
        document = json.loads(path.read_text())
        assert len(document["exchanges"]) == 1
        assert "Attention Is All You Need" in document["exchanges"][0]["body_text"]
        assert "recorded 1 exchange(s)" in capsys.readouterr().err

    @respx.mock
    def test_http1_reaches_the_transport(self, monkeypatch):
        from parkour_mcp import common

        seen: list[bool] = []
        real = common.guarded_client

        def spy(**kwargs):
            seen.append(kwargs["http2"])
            return real(**kwargs)

        monkeypatch.setattr(common, "guarded_client", spy)
        respx.get(ARXIV_API_URL).mock(
            return_value=httpx.Response(200, text=ARXIV_SINGLE_ENTRY_XML)
        )
        args = _parse(["call", "arxiv", '{"action": "search", "query": "x"}', "--http1"])
        _cli.run_call(args, catalog=CATALOG, profile="code")
        assert seen == [False]

    def test_bad_json_is_a_usage_error(self):
        args = _parse(["call", "arxiv", "not json"])
        with pytest.raises(SystemExit, match="JSON object"):
            _cli.run_call(args, catalog=CATALOG, profile="code")
