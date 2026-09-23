"""Wire trace for outbound fetches.

Audience: a developer reproducing live behaviour of a tool.

Every request the two fetch functions make (``common.py#guarded_fetch`` for
API hosts, ``_transport.py#guarded_fetch`` for the generic path) is recorded
here as one :class:`Exchange`: the request as built, what came back, and
which rate limiter, if any, governed the call.  That record is the single
source for three consumers:

* the JSON-lines sink selected by ``PARKOUR_TRACE`` (a path, or ``-`` for
  stderr), which runs inside the MCP server as well as the CLI;
* ``parkour-mcp call --as-curl``, which renders each exchange as the
  equivalent ``curl`` command behind a rate-limit warning derived from the
  limiter that governed it;
* ``parkour-mcp call --dry-run``, which records the request and stops
  before anything is sent.

Nothing here changes what is sent.  A trace is a witness, not a knob.
"""

from __future__ import annotations

import contextlib
import dataclasses
import json
import os
import shlex
import sys
import time
from collections.abc import Iterator, Mapping
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .common import RateLimiter

TRACE_ENV = "PARKOUR_TRACE"

# Response headers worth keeping on every record: the ones that locate a
# response (which edge, which cache tier, how old) and the ones that carry
# an upstream's own throttling vocabulary.  Everything else is noise for the
# questions a trace exists to answer.
_TRACE_RESPONSE_HEADERS = (
    "server",
    "via",
    "x-served-by",
    "x-cache",
    "x-cache-hits",
    "age",
    "retry-after",
    "content-type",
    "content-length",
    "content-encoding",
    "cf-ray",
    "cf-mitigated",
    "ratelimit",
    "x-ratelimit-limit",
    "x-ratelimit-remaining",
    "x-ratelimit-reset",
    "location",
)

# Request headers curl sets on its own; repeating them would be wrong
# (``host``) or misleading (``content-length``).
_CURL_OMITTED_REQUEST_HEADERS = frozenset({"host", "content-length"})


class DryRunStop(BaseException):
    """Raised by a fetch function in dry-run mode after recording the request.

    Derives from ``BaseException`` so the ``except Exception`` fall-throughs
    that let a fast path yield to the next detector cannot swallow it: a dry
    run must stop at the first request, not at the first request the
    dispatcher was willing to give up on.
    """

    def __init__(self, url: str) -> None:
        super().__init__(f"dry run stopped before sending {url}")
        self.url = url


@dataclass(frozen=True)
class LimiterRecord:
    """The rate limiter that governed an exchange, as the trace saw it."""

    name: str
    interval: float
    policy: str | None
    url: str | None
    waited: float
    """Seconds the limiter held this call before it was sent."""


@dataclass
class Exchange:
    """One request and its response, as the fetch function saw them."""

    ts: str
    transport: str
    """``httpx`` (API hosts, honest identity) or ``wreq`` (generic path)."""
    method: str
    url: str
    """The URL as sent, after query encoding."""
    request_headers: dict[str, str]
    """For ``httpx``, the full header set as built.  For ``wreq``, only the
    headers this codebase set: the browser emulation adds its own, which are
    not observable from here."""
    status: int | None = None
    http_version: str | None = None
    response_headers: dict[str, str] = field(default_factory=dict)
    body_bytes: int | None = None
    remote_addr: str | None = None
    elapsed_ms: float | None = None
    limiter: LimiterRecord | None = None
    emulation: str | None = None
    """Browser fingerprint the transport presented, when it is not honest."""
    error: str | None = None
    dry_run: bool = False
    started: float | None = field(default=None, repr=False)
    """Monotonic clock at send time; kept out of the emitted record."""


# ---------------------------------------------------------------------------
# Context
# ---------------------------------------------------------------------------
# A limiter notes itself here when it releases a call, and the next exchange
# consumes the note.  A fetch that no limiter preceded therefore records
# ``limiter=None``, which is the honest answer: the arXiv paper action's
# HEAD against arxiv.org/html, for instance, runs outside the API limiter.
_pending_limiter: ContextVar[tuple[Any, float] | None] = ContextVar(
    "parkour_trace_pending_limiter", default=None
)
_dry_run: ContextVar[bool] = ContextVar("parkour_trace_dry_run", default=False)
_collector: ContextVar[list[Exchange] | None] = ContextVar(
    "parkour_trace_collector", default=None
)


def note_limiter(limiter: RateLimiter, waited: float) -> None:
    """Called by ``RateLimiter.wait`` so the next exchange can name it."""
    _pending_limiter.set((limiter, waited))


@contextlib.contextmanager
def dry_run(enabled: bool = True) -> Iterator[None]:
    """Record requests and stop before sending, for the duration of the block."""
    token = _dry_run.set(enabled)
    try:
        yield
    finally:
        _dry_run.reset(token)


@contextlib.contextmanager
def collect() -> Iterator[list[Exchange]]:
    """Gather every exchange recorded inside the block into a list."""
    exchanges: list[Exchange] = []
    token = _collector.set(exchanges)
    try:
        yield exchanges
    finally:
        _collector.reset(token)


# ---------------------------------------------------------------------------
# Recording
# ---------------------------------------------------------------------------

def _take_limiter() -> LimiterRecord | None:
    pending = _pending_limiter.get()
    if pending is None:
        return None
    _pending_limiter.set(None)
    limiter, waited = pending
    return LimiterRecord(
        name=limiter.name or "unnamed",
        interval=limiter.min_interval,
        policy=limiter.policy,
        url=limiter.url,
        waited=waited,
    )


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds")


def _subset(headers: Mapping[str, str]) -> dict[str, str]:
    lowered = {k.lower(): v for k, v in headers.items()}
    return {k: lowered[k] for k in _TRACE_RESPONSE_HEADERS if k in lowered}


def begin(
    transport: str,
    method: str,
    url: str,
    request_headers: Mapping[str, str],
    *,
    emulation: str | None = None,
) -> Exchange:
    """Open a record for a request about to be sent.

    Consumes the pending limiter note, so it must be called once per request
    and before ``finish`` or ``fail``.  In dry-run mode the record is emitted
    immediately and :class:`DryRunStop` is raised.
    """
    exchange = Exchange(
        ts=_now(),
        transport=transport,
        method=method.upper(),
        url=url,
        request_headers={k.lower(): v for k, v in request_headers.items()},
        limiter=_take_limiter(),
        emulation=emulation,
    )
    if _dry_run.get():
        exchange.dry_run = True
        _emit(exchange)
        raise DryRunStop(url)
    exchange.started = time.monotonic()
    return exchange


def finish(
    exchange: Exchange,
    *,
    status: int,
    http_version: str | None,
    response_headers: Mapping[str, str],
    body_bytes: int | None,
    remote_addr: str | None = None,
) -> None:
    """Complete and emit a record for a response that arrived."""
    exchange.status = status
    exchange.http_version = http_version
    exchange.response_headers = _subset(response_headers)
    exchange.body_bytes = body_bytes
    exchange.remote_addr = remote_addr
    exchange.elapsed_ms = _elapsed(exchange)
    _emit(exchange)


def fail(exchange: Exchange, error: BaseException) -> None:
    """Complete and emit a record for a request that produced no response."""
    exchange.error = f"{type(error).__name__}: {error}"
    exchange.elapsed_ms = _elapsed(exchange)
    _emit(exchange)


def _elapsed(exchange: Exchange) -> float | None:
    if exchange.started is None:
        return None
    return round((time.monotonic() - exchange.started) * 1000, 1)


def _emit(exchange: Exchange) -> None:
    collected = _collector.get()
    if collected is not None:
        collected.append(exchange)
    sink = os.environ.get(TRACE_ENV, "").strip()
    if not sink:
        return
    record = dataclasses.asdict(exchange)
    record.pop("started")
    line = json.dumps(record, separators=(",", ":"))
    if sink == "-":
        sys.stderr.write(line + "\n")
        sys.stderr.flush()
        return
    with open(sink, "a", encoding="utf-8") as fh:
        fh.write(line + "\n")


# ---------------------------------------------------------------------------
# curl rendering
# ---------------------------------------------------------------------------

def render_curl(exchange: Exchange, *, index: int, total: int, tool: str | None = None) -> str:
    """Render one exchange as a commented ``curl`` command.

    The comment block carries the rate-limit contract the request ran under,
    because a curl command is what a caller pastes into a loop, and the loop
    is exactly where parkour's in-process limiter no longer applies.  The
    contract comes from the limiter record, so the number and its source are
    the code's, not the reader's research.
    """
    lines: list[str] = []
    what = f"{exchange.method} {exchange.url}"
    outcome = "not sent (dry run)" if exchange.dry_run else _outcome(exchange)
    origin = f", {tool}" if tool else ""
    lines.append(f"# parkour-mcp --as-curl: exchange {index} of {total}{origin}, {exchange.ts}")
    lines.append(f"# {what}: {outcome}")
    lines.extend(_limit_comment(exchange.limiter))
    if exchange.emulation:
        lines.append(
            f"# transport: parkour sent this with wreq's {exchange.emulation} "
            "fingerprint and the headers that emulation adds. curl's TLS "
            "identity differs, so a refusal seen by curl is not evidence "
            "about the tool, nor the reverse."
        )
    lines.append(_curl_command(exchange))
    return "\n".join(lines)


def _outcome(exchange: Exchange) -> str:
    if exchange.error:
        return exchange.error
    version = f"{exchange.http_version} " if exchange.http_version else ""
    elapsed = f", {exchange.elapsed_ms:.0f} ms" if exchange.elapsed_ms is not None else ""
    return f"{version}{exchange.status}{elapsed}"


def _limit_comment(limiter: LimiterRecord | None) -> list[str]:
    if limiter is None:
        return [(
            "# rate limit: no in-process limiter governed this request, so a "
            "loop around it has no floor but the one you set. Read the "
            "origin's terms before composing one."
        )]
    interval = limiter.interval
    sleep = int(interval) if interval == int(interval) else interval
    lines = [f"# rate limit: {limiter.name}, one request every {interval:g} s."]
    if limiter.policy:
        lines.append(f"#   {limiter.policy}" + (f" ({limiter.url})" if limiter.url else ""))
    elif limiter.url:
        lines.append(f"#   {limiter.url}")
    lines.append(
        "#   parkour enforces this in-process; a shell loop must add it back:"
    )
    lines.append(f"#     for u in ...; do curl ... \"$u\"; sleep {sleep}; done")
    return lines


def _curl_command(exchange: Exchange) -> str:
    head = "curl -sS"
    if exchange.http_version == "HTTP/2":
        head += " --http2"
    elif exchange.http_version == "HTTP/1.1":
        head += " --http1.1"
    if exchange.method == "HEAD":
        head += " -I"
    elif exchange.method != "GET":
        head += f" -X {exchange.method}"
    options = [head]
    for name, value in exchange.request_headers.items():
        if name in _CURL_OMITTED_REQUEST_HEADERS:
            continue
        if name == "user-agent":
            options.append(f"-A {shlex.quote(value)}")
        else:
            options.append(f"-H {shlex.quote(f'{name}: {value}')}")
    options.append(shlex.quote(exchange.url))
    return " \\\n  ".join(options)
