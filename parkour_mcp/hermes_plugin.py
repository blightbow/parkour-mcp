"""Hermes Agent plugin entrypoint for parkour.

parkour ships as one package with two entrypoints. Invoked as the
``parkour-mcp`` console script it runs the FastMCP stdio server; imported by
Hermes Agent through the ``hermes_agent.plugins`` entry point it is a native
plugin. Hermes discovers this module, calls :func:`register` once at startup,
and parkour's tools join the registry alongside Hermes' built-ins. The native
path matters because Hermes delivers a native tool's return string verbatim
into the model's context — parkour's frontmatter-first, fenced output reaches
the LLM document-shaped, where the MCP transport would JSON-escape it.

Why parkour owns its event loop
-------------------------------
parkour's tool coroutines carry module-global, event-loop-bound state: the
``common.RateLimiter`` locks, the research-shelf lock, per-repo cache locks.
Hermes' own sync->async bridge hands coroutines to a rotating set of
per-thread and per-call event loops, which would rebind those locks across
loops and raise ``RuntimeError``. So this adapter starts one private,
long-lived event loop on a daemon thread and marshals every call onto it via
:func:`asyncio.run_coroutine_threadsafe`. That reproduces the standalone MCP
server's one-process / one-loop execution model exactly, so the logic modules
need no awareness of the host. Handlers are therefore registered synchronous
(``is_async=False``): Hermes runs sync handlers in a worker thread, so
blocking on the result future is safe and never stalls the agent loop.

Replacing the host's web tools
------------------------------
By default parkour's tools register alongside Hermes' built-ins under their
own names. Two opt-in flags under ``plugins.entries.parkour`` in
``~/.hermes/config.yaml`` make parkour *replace* a built-in instead::

    plugins:
      entries:
        parkour:
          override_web_extract: true   # parkour fetch registers as web_extract
          override_web_search: true    # parkour Kagi search registers as web_search

When set, the tool registers under the host name with ``override=True`` and
its description switches to a variant that no longer positions parkour
against the built-in it has replaced. The flags are read at registration
time, so a change takes effect on the next Hermes start.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import logging
import threading
from collections.abc import Callable, Coroutine
from typing import Any

from mcp.server.fastmcp.tools.base import Tool as _FastMCPTool

from . import _apply_s2_enrichment, _build_description, _resolve_catalog
from .common import TOOL_NAMES, init_tool_names, s2_enabled

logger = logging.getLogger(__name__)

# The hermes profile resolves description placeholders against Hermes' built-in
# tool names; registration names are snake_case, which is the desktop profile's
# naming convention (TOOL_NAMES carries no separate "hermes" column because the
# names are identical).
_DESC_PROFILE = "hermes"
_NAME_PROFILE = "desktop"

# All parkour tools register under one toolset, so Hermes groups them in
# `/plugins`, the banner, and per-toolset config.
_TOOLSET = "parkour"

# Upper bound on a single tool call. guarded_fetch caps one HTTP fetch at 60s;
# a tool may chain a few fetches plus rate-limiter waits. Stays under Hermes'
# own 300s tool-call ceiling so this is the tighter, parkour-attributed bound.
_CALL_TIMEOUT = 180.0

_ToolFunc = Callable[..., Coroutine[Any, Any, str]]


# ---------------------------------------------------------------------------
# Private event loop — see module docstring
# ---------------------------------------------------------------------------

_loop: asyncio.AbstractEventLoop | None = None
_loop_lock = threading.Lock()


def _get_loop() -> asyncio.AbstractEventLoop:
    """Return parkour's private event loop, starting its daemon thread once."""
    global _loop
    if _loop is not None:
        return _loop
    with _loop_lock:
        if _loop is None:
            loop = asyncio.new_event_loop()
            threading.Thread(
                target=loop.run_forever,
                name="parkour-plugin-loop",
                daemon=True,
            ).start()
            _loop = loop
    return _loop


def _run(coro: Coroutine[Any, Any, str]) -> str:
    """Run a parkour tool coroutine on the private loop and return its result."""
    future = asyncio.run_coroutine_threadsafe(coro, _get_loop())
    try:
        return future.result(timeout=_CALL_TIMEOUT)
    except concurrent.futures.TimeoutError:
        # Ask the coroutine to wind down on the parkour loop instead of
        # leaking a runaway task; the handler reports the timeout upward.
        future.cancel()
        raise


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

def _make_handler(func: _ToolFunc, name: str) -> Callable[..., str]:
    """Wrap an async parkour tool as a synchronous Hermes tool handler.

    parkour tools already return frontmatter-first markdown and handle their
    own domain errors; this wrapper only catches the failure modes the bridge
    itself introduces — a timeout, or a crash before the tool's own error
    handling runs — and reports them in parkour's plain ``Error: ...`` style.
    """
    def handler(args: dict, **kwargs: Any) -> str:
        del kwargs  # Hermes passes task_id/session_id; parkour tools want neither
        try:
            return _run(func(**args))
        except concurrent.futures.TimeoutError:
            return f"Error: parkour tool '{name}' timed out after {_CALL_TIMEOUT:.0f}s."
        except Exception as exc:  # noqa: BLE001 — a tool handler must never raise
            logger.warning("parkour tool '%s' failed: %s", name, exc, exc_info=True)
            return f"Error: parkour tool '{name}' failed: {type(exc).__name__}: {exc}"

    return handler


def _schema_for(func: _ToolFunc, name: str, description: str) -> dict:
    """Build a Hermes tool schema from a parkour tool's type-hinted signature.

    FastMCP's ``Tool.from_function`` derives the JSON Schema parkour already
    relies on for the MCP server, keeping the type-hinted signatures the
    single source of truth for both entrypoints.
    """
    tool = _FastMCPTool.from_function(func, name=name, description=description)
    return {"name": name, "description": description, "parameters": tool.parameters}


def _read_override_flags() -> tuple[bool, bool]:
    """Read (override_web_extract, override_web_search) from config.yaml.

    parkour's per-plugin options live at ``plugins.entries.parkour`` — the
    same config namespace Hermes' own plugin_llm uses. Both default off,
    including when Hermes' config module is unavailable, so this module stays
    importable outside a Hermes runtime (parkour's own test suite imports it
    directly).
    """
    try:
        # Hermes-runtime-only module; absent in parkour's own environment.
        from hermes_cli.config import cfg_get, load_config  # type: ignore
    except ImportError:
        return False, False
    config = load_config() or {}
    extract = bool(cfg_get(config, "plugins", "entries", "parkour",
                           "override_web_extract", default=False))
    search = bool(cfg_get(config, "plugins", "entries", "parkour",
                          "override_web_search", default=False))
    return extract, search


def register(ctx: Any) -> None:
    """Hermes plugin entrypoint, discovered via the ``hermes_agent.plugins`` group.

    Called once at Hermes startup. Registers parkour's always-on tools — plus
    ``semantic_scholar`` when the S2 terms-of-service opt-in is set — into the
    host tool registry under the ``parkour`` toolset. With the override flags
    set (see module docstring), the fetch / search tools instead register
    under their Hermes built-in names, replacing them via ``override=True``.
    """
    # Populate the snake_case display-name lookup parkour tools use when they
    # emit hint / see_also strings in their own frontmatter.
    init_tool_names(_NAME_PROFILE)

    s2_on = s2_enabled()
    if s2_on:
        _apply_s2_enrichment()

    override_extract, override_search = _read_override_flags()
    override_names: dict[str, str] = {}
    if override_extract:
        override_names["web_fetch_direct"] = "web_extract"
    if override_search:
        override_names["search"] = "web_search"

    catalog = _resolve_catalog(s2_on)
    for internal_name, func in catalog:
        host_name = override_names.get(internal_name)
        name = host_name or TOOL_NAMES[internal_name][_NAME_PROFILE]
        description = _build_description(
            internal_name, _DESC_PROFILE,
            override_web_extract=override_extract,
            override_web_search=override_search,
        )
        ctx.register_tool(
            name=name,
            toolset=_TOOLSET,
            schema=_schema_for(func, name, description),
            handler=_make_handler(func, name),
            is_async=False,
            description=description,
            override=host_name is not None,
        )

    logger.info(
        "parkour plugin registered %d tools (override web_extract=%s, web_search=%s)",
        len(catalog), override_extract, override_search,
    )
