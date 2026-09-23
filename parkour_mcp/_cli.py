"""``parkour-mcp call``: invoke one registered tool from the shell.

Audience: a developer reproducing a tool's live behaviour.

The call runs through the production path end to end: profile naming, the
rate limiter, the transport with its identity policy, the caches, and the
frontmatter.  Nothing is reimplemented, so what the CLI observes is what
the MCP server would have done.  The wire trace (``_trace.py``) records
every exchange the call makes; ``--as-curl`` renders those records and
``--dry-run`` stops the call at its first request.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
import sys
from collections.abc import Callable, Sequence
from typing import Any

from . import _trace
from .common import TOOL_NAMES, force_http1, init_tool_names

Catalog = Sequence[tuple[str, Callable[..., Any]]]


def _out(text: str = "") -> None:
    sys.stdout.write(text + "\n")


def _err(text: str) -> None:
    sys.stderr.write(text + "\n")


def add_call_parser(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser(
        "call",
        help="invoke one tool through the production path and exit",
        description=(
            "Invoke one registered tool with JSON arguments.  The call runs "
            "through the same limiter, transport, caches, and frontmatter as "
            "the MCP server; the tool's output goes to stdout."
        ),
    )
    parser.add_argument(
        "tool",
        help="tool name: the internal key (arxiv), the Claude Code name "
             "(ArXiv), or the Desktop name (arxiv); case-insensitive",
    )
    parser.add_argument(
        "args",
        nargs="?",
        default="{}",
        help="JSON object of tool arguments, or - to read it from stdin "
             "(default: {})",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="record the first outbound request and stop before sending it",
    )
    parser.add_argument(
        "--as-curl",
        action="store_true",
        help="after the call, print each recorded exchange as a curl "
             "command behind its rate-limit contract",
    )
    parser.add_argument(
        "--trace",
        metavar="FILE",
        help=f"write one JSON line per exchange to FILE (- for stderr); "
             f"same as setting {_trace.TRACE_ENV}",
    )
    parser.add_argument(
        "--http1",
        action="store_true",
        help="issue the call over HTTP/1.1 on both transports, changing "
             "nothing else",
    )
    parser.add_argument(
        "--record",
        metavar="FILE",
        help="write the call's responses to FILE as a fixture that "
             "tests/_replay.py mounts as respx routes",
    )


def resolve_tool(name: str, catalog: Catalog) -> tuple[str, Callable[..., Any]]:
    """Map any spelling of a tool name to its internal key and function."""
    wanted = name.strip().lower()
    by_key = dict(catalog)
    for key, func in catalog:
        spellings = {key, *TOOL_NAMES.get(key, {}).values()}
        if wanted in {s.lower() for s in spellings}:
            return key, func
    known = ", ".join(sorted(by_key))
    raise SystemExit(f"error: unknown tool {name!r}; known tools: {known}")


def _load_args(raw: str) -> dict[str, Any]:
    text = sys.stdin.read() if raw == "-" else raw
    try:
        loaded = json.loads(text or "{}")
    except json.JSONDecodeError as exc:
        raise SystemExit(f"error: tool arguments must be a JSON object: {exc}") from exc
    if not isinstance(loaded, dict):
        raise SystemExit("error: tool arguments must be a JSON object")
    return loaded


def run_call(args: argparse.Namespace, *, catalog: Catalog, profile: str) -> int:
    """Execute the ``call`` subcommand.  Returns the process exit status."""
    init_tool_names(profile)
    key, func = resolve_tool(args.tool, catalog)
    kwargs = _load_args(args.args)
    if args.trace:
        os.environ[_trace.TRACE_ENV] = args.trace

    display = TOOL_NAMES.get(key, {}).get(profile, key)
    result: str | None = None
    stopped: _trace.DryRunStop | None = None
    with (
        _trace.collect() as exchanges,
        _trace.dry_run(args.dry_run),
        _trace.capture() if args.record else contextlib.nullcontext(),
        force_http1() if args.http1 else contextlib.nullcontext(),
    ):
        try:
            result = asyncio.run(func(**kwargs))
        except _trace.DryRunStop as stop:
            stopped = stop

    if result is not None:
        _out(result)
    if stopped is not None and not args.as_curl:
        _err(f"# {stopped}")
    if args.record:
        written = _trace.write_fixture(exchanges, args.record)
        _err(f"# recorded {written} exchange(s) to {args.record}")

    if args.as_curl:
        if result is not None:
            _out()
        if not exchanges:
            _out(
                "# parkour-mcp --as-curl: no exchanges were recorded. The call "
                "was served from cache, or by a transport curl cannot express "
                "(a headless render, yt-dlp)."
            )
        for index, exchange in enumerate(exchanges, start=1):
            _out(_trace.render_curl(
                exchange, index=index, total=len(exchanges), tool=display,
            ))
            if index < len(exchanges):
                _out()
    return 0
