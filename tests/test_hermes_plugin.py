"""Tests for the Hermes Agent plugin entrypoint (parkour_mcp/hermes_plugin.py).

Covers the registration glue and the sync->async handler bridge. Real tool
functions are only used for schema generation (pure introspection, no
network); the handler bridge is exercised with stub coroutines.
"""

import pytest

from parkour_mcp import _ALWAYS_ON_TOOLS, hermes_plugin
from parkour_mcp.common import TOOL_NAMES, init_tool_names

_ALWAYS_ON_DESKTOP_NAMES = {
    TOOL_NAMES[name]["desktop"] for name, _ in _ALWAYS_ON_TOOLS
}


@pytest.fixture(autouse=True)
def _restore_tool_names():
    """register() calls init_tool_names('desktop'); restore conftest's choice.

    conftest.py initializes the process-wide display-name lookup to the
    'code' profile for the whole session. A test that calls register() flips
    it to 'desktop', which would leak PascalCase->snake_case into every later
    test's hint/see_also assertions.
    """
    yield
    init_tool_names("code")


class _FakeCtx:
    """Minimal stand-in for Hermes' PluginContext — captures register_tool calls."""

    def __init__(self):
        self.tools: list[dict] = []

    def register_tool(self, *, name, toolset, schema, handler,
                      is_async=False, description="", override=False, **kwargs):
        del kwargs
        self.tools.append({
            "name": name, "toolset": toolset, "schema": schema,
            "handler": handler, "is_async": is_async, "description": description,
            "override": override,
        })

    def by_name(self, name: str) -> dict:
        return next(t for t in self.tools if t["name"] == name)


def _register(monkeypatch, *, s2=False, override_extract=False,
              override_search=False) -> _FakeCtx:
    """Run register() against a fake ctx with deterministic gates.

    The S2 opt-in and the override flags are both environmental (env var /
    config file), so all three are pinned here rather than left to the host.
    _apply_s2_enrichment mutates the module-global TOOL_DESCRIPTIONS; it is
    stubbed out so the s2=True path does not leak description edits into
    other tests.
    """
    monkeypatch.setattr(hermes_plugin, "s2_enabled", lambda: s2)
    monkeypatch.setattr(hermes_plugin, "_apply_s2_enrichment", lambda: None)
    monkeypatch.setattr(hermes_plugin, "_read_override_flags",
                        lambda: (override_extract, override_search))
    ctx = _FakeCtx()
    hermes_plugin.register(ctx)
    return ctx


def test_register_adds_all_always_on_tools(monkeypatch):
    """Every always-on tool registers under its snake_case (desktop) name."""
    ctx = _register(monkeypatch, s2=False)
    names = {t["name"] for t in ctx.tools}
    assert names == _ALWAYS_ON_DESKTOP_NAMES
    assert "semantic_scholar" not in names


def test_register_omits_semantic_scholar_when_opted_out(monkeypatch):
    """semantic_scholar stays unregistered unless the S2 ToS gate is set."""
    ctx = _register(monkeypatch, s2=False)
    assert len(ctx.tools) == len(_ALWAYS_ON_TOOLS)


def test_register_includes_semantic_scholar_when_opted_in(monkeypatch):
    """With the S2 gate set, semantic_scholar joins the catalog."""
    ctx = _register(monkeypatch, s2=True)
    names = {t["name"] for t in ctx.tools}
    assert "semantic_scholar" in names
    assert len(ctx.tools) == len(_ALWAYS_ON_TOOLS) + 1


def test_all_tools_share_the_parkour_toolset(monkeypatch):
    ctx = _register(monkeypatch, s2=False)
    assert {t["toolset"] for t in ctx.tools} == {"parkour"}


def test_handlers_register_synchronous(monkeypatch):
    """Handlers must register sync — the bridge owns the event loop, so
    Hermes' own async dispatch (which rotates loops) is deliberately bypassed.
    """
    ctx = _register(monkeypatch, s2=False)
    assert all(t["is_async"] is False for t in ctx.tools)


def test_schemas_are_well_formed(monkeypatch):
    """Each schema carries a name, a non-empty description, and an object
    parameter schema — the shape Hermes' registry expects.
    """
    ctx = _register(monkeypatch, s2=False)
    for tool in ctx.tools:
        schema = tool["schema"]
        assert schema["name"] == tool["name"]
        assert isinstance(schema["description"], str) and schema["description"].strip()
        params = schema["parameters"]
        assert params["type"] == "object"
        assert "properties" in params


def test_handler_returns_coroutine_result():
    """The sync wrapper runs an async tool on the private loop and returns
    its string result.
    """
    async def stub_tool(**kwargs):
        del kwargs
        return "frontmatter-first output"

    handler = hermes_plugin._make_handler(stub_tool, "stub")
    assert handler({}) == "frontmatter-first output"


def test_handler_forwards_args():
    """Tool arguments arrive as a dict and are splatted onto the coroutine."""
    async def echo_tool(*, url, limit=10):
        return f"{url}:{limit}"

    handler = hermes_plugin._make_handler(echo_tool, "echo")
    assert handler({"url": "x", "limit": 3}) == "x:3"


def test_handler_catches_exceptions():
    """A crashing tool must yield a parkour-style error string, never raise —
    Hermes fails the whole tool call if a handler propagates an exception.
    """
    async def boom_tool(**kwargs):
        del kwargs
        raise ValueError("kaboom")

    handler = hermes_plugin._make_handler(boom_tool, "boom")
    result = handler({})
    assert result.startswith("Error: parkour tool 'boom' failed:")
    assert "ValueError: kaboom" in result


def test_handler_ignores_host_kwargs():
    """Hermes may pass task_id / session_id as **kwargs; parkour tools take
    neither, so the wrapper must drop them rather than forward them.
    """
    async def stub_tool(**kwargs):
        del kwargs
        return "ok"

    handler = hermes_plugin._make_handler(stub_tool, "stub")
    assert handler({}, task_id="t1", session_id="s1") == "ok"


# --- built-in tool override -------------------------------------------------

def test_override_off_keeps_native_parkour_names(monkeypatch):
    """With both flags off, the fetch/search tools keep parkour's own names
    and nothing claims an override.
    """
    ctx = _register(monkeypatch)
    names = {t["name"] for t in ctx.tools}
    assert {"web_fetch_incisive", "kagi_search"} <= names
    assert "web_extract" not in names and "web_search" not in names
    assert all(t["override"] is False for t in ctx.tools)


def test_override_web_extract_replaces_the_builtin(monkeypatch):
    """override_web_extract registers the fetch tool as web_extract with the
    override flag; the native web_fetch_incisive name is not used.
    """
    ctx = _register(monkeypatch, override_extract=True)
    names = {t["name"] for t in ctx.tools}
    assert "web_extract" in names
    assert "web_fetch_incisive" not in names
    assert ctx.by_name("web_extract")["override"] is True
    # search is untouched when only the extract flag is set
    assert "kagi_search" in names
    assert ctx.by_name("kagi_search")["override"] is False


def test_override_web_search_replaces_the_builtin(monkeypatch):
    """override_web_search registers the Kagi search tool as web_search with
    the override flag; the native kagi_search name is not used.
    """
    ctx = _register(monkeypatch, override_search=True)
    names = {t["name"] for t in ctx.tools}
    assert "web_search" in names
    assert "kagi_search" not in names
    assert ctx.by_name("web_search")["override"] is True
    assert "web_fetch_incisive" in names
    assert ctx.by_name("web_fetch_incisive")["override"] is False


def test_override_leaves_other_tools_additive(monkeypatch):
    """Overriding the web tools must not flag unrelated tools for override."""
    ctx = _register(monkeypatch, override_extract=True, override_search=True)
    overridden = {t["name"] for t in ctx.tools if t["override"]}
    assert overridden == {"web_extract", "web_search"}


def test_override_descriptions_drop_self_reference(monkeypatch):
    """Once parkour's tool *is* the built-in, its description must stop
    positioning itself against the sibling it replaced.
    """
    ctx = _register(monkeypatch, override_extract=True, override_search=True)
    extract_desc = ctx.by_name("web_extract")["description"]
    assert "Unlike web_extract" not in extract_desc
    assert "two fetch tools" not in extract_desc
    search_desc = ctx.by_name("web_search")["description"]
    assert "alternative to web_search" not in search_desc


def test_override_schemas_stay_well_formed(monkeypatch):
    """Override registration must still produce valid object schemas whose
    name matches the (host) registration name.
    """
    ctx = _register(monkeypatch, override_extract=True, override_search=True)
    for tool in ctx.tools:
        schema = tool["schema"]
        assert schema["name"] == tool["name"]
        assert schema["parameters"]["type"] == "object"
        assert schema["description"].strip()
