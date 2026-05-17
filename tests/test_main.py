"""Tests for the MCP server entry point's startup-time invariants.

The full ``main()`` flow runs ``mcp.run(transport='stdio')`` which blocks
on the protocol loop and isn't unit-testable directly. Anything that
must not crash at startup needs an explicit test here — pre-existing
test files exercise individual tool modules but not the registration
glue, which is where this commit's bug shipped past pyright/ty/ruff
and the existing 1249-test suite.
"""

import pytest

import parkour_mcp
from parkour_mcp import (
    _ALWAYS_ON_TOOLS,
    _OPTIONAL_TOOLS,
    PROFILE_VARS,
    TOOL_DESCRIPTIONS,
    _build_description,
)


_INTERNAL_NAMES = (
    [name for name, _ in _ALWAYS_ON_TOOLS]
    + list(_OPTIONAL_TOOLS)
)
_PROFILES = ("code", "desktop", "hermes")


@pytest.mark.parametrize("internal_name", _INTERNAL_NAMES)
@pytest.mark.parametrize("profile", _PROFILES)
def test_build_description_succeeds(internal_name, profile):
    """Every registered tool's description must format cleanly under both
    profiles. Catches unescaped braces in description prose (e.g. an
    ``ytsearch{N}:`` literal that ``.format()`` reads as a placeholder)
    before they reach ``mcp.add_tool`` at server startup.
    """
    desc = _build_description(internal_name, profile)
    assert isinstance(desc, str)
    assert desc.strip()


def test_main_reaches_run_without_error(monkeypatch):
    """End-to-end registration smoke test: ``main()`` must walk every
    tool in the registry, call ``_build_description``, and reach
    ``mcp.run`` without raising. ``mcp.run`` itself is short-circuited
    so the test doesn't block on the stdio loop.
    """
    monkeypatch.setattr("sys.argv", ["parkour-mcp", "--profile", "code"])
    called = {"yes": False}

    def fake_run(*args, **kwargs):
        del args, kwargs
        called["yes"] = True

    monkeypatch.setattr(parkour_mcp.mcp, "run", fake_run)
    parkour_mcp.main()
    assert called["yes"] is True


def test_no_tool_emits_an_output_schema(monkeypatch):
    """Every tool must register with structured output suppressed.

    The MCP SDK auto-wraps a ``-> str`` return into an ``outputSchema``
    plus a ``structuredContent`` {"result": ...} mirror, doubling the
    payload on the wire (see github.com/blightbow/parkour-mcp/issues/9).
    ``add_tool(..., structured_output=False)`` opts out; an ``output_schema``
    that is not None means the opt-out was dropped from a registration.
    """
    monkeypatch.setattr("sys.argv", ["parkour-mcp", "--profile", "code"])
    monkeypatch.setattr(parkour_mcp.mcp, "run", lambda *_a, **_k: None)
    parkour_mcp.main()

    registered = parkour_mcp.mcp._tool_manager.list_tools()
    assert registered, "main() registered no tools"
    offenders = [t.name for t in registered if t.output_schema is not None]
    assert not offenders, f"tools emit an outputSchema: {offenders}"


def test_tool_descriptions_have_no_orphan_placeholders():
    """Belt-and-suspenders: every ``{...}`` in every tool description
    must resolve under at least one profile — i.e. the descriptions
    should never carry a placeholder that isn't in PROFILE_VARS or the
    ``search_grammar`` injection.

    A placeholder name not present in any substitution dict surfaces
    as a KeyError from ``.format()`` only at registration time. Walking
    every description × every profile catches the issue independently
    of the test_build_description coverage.
    """
    for internal_name in _INTERNAL_NAMES:
        for profile in _PROFILES:
            # If this raises, the description has a bad placeholder
            _build_description(internal_name, profile)


def test_tool_descriptions_dict_keys_match_registry():
    """Every tool registered in the always-on tuple or the optional
    tuple must have an entry in TOOL_DESCRIPTIONS — otherwise
    _build_description raises KeyError on the dict lookup before even
    reaching ``.format()``.
    """
    for name in _INTERNAL_NAMES:
        assert name in TOOL_DESCRIPTIONS, (
            f"tool {name!r} is registered but missing from TOOL_DESCRIPTIONS"
        )


def test_profile_vars_cover_all_profiles():
    """PROFILE_VARS must define every profile the entrypoints use — 'code'
    and 'desktop' for the MCP server, 'hermes' for the plugin — and all of
    them must cover the same set of placeholder names. A placeholder present
    in one profile but not another would silently miss when that other
    profile renders.
    """
    assert set(PROFILE_VARS) == {"code", "desktop", "hermes"}
    reference = set(PROFILE_VARS["code"])
    for profile, profile_vars in PROFILE_VARS.items():
        keys = set(profile_vars)
        assert keys == reference, (
            f"profile {profile!r} placeholder keys differ from 'code': "
            f"missing={reference - keys}, extra={keys - reference}"
        )


def test_hermes_profile_descriptions_drop_anthropic_framing():
    """The hermes profile targets non-Claude hosts. Its descriptions must not
    claim parkour's sibling tools route through Anthropic's infrastructure —
    that prose is true only for Claude Code / Claude Desktop.
    """
    for internal_name in _INTERNAL_NAMES:
        desc = _build_description(internal_name, "hermes")
        assert "Anthropic" not in desc, (
            f"hermes-profile description for {internal_name!r} still "
            f"references Anthropic"
        )
