"""Generators imported by cog blocks in CLAUDE.md, README.md, and docs/.

Pattern lifted from scientific-python/cookie: keep cog blocks in prose to
1-3 lines that import and call into here, so the introspection code stays
under ruff/ty/test scope rather than hiding inside HTML-comment markers.
"""

from __future__ import annotations

from pathlib import Path


def action_list(module_attr: str) -> str:
    """Render an action tuple as `9 actions: a, b, c`.

    *module_attr* is dotted-path to a tuple, e.g. ``parkour_mcp.github._VALID_ACTIONS``.
    """
    mod_name, attr = module_attr.rsplit(".", 1)
    import importlib
    actions = getattr(importlib.import_module(mod_name), attr)
    return f"{len(actions)} actions: {', '.join(actions)}"


def ecosystem_list() -> str:
    """Render the deps.dev ecosystem set as `7 ecosystems: pypi, npm, ...`."""
    from parkour_mcp.packages import _VALID_ECOSYSTEMS
    eco = [e.strip() for e in _VALID_ECOSYSTEMS.split(",")]
    return f"{len(eco)} ecosystems: {', '.join(eco)}"


def loc(rel_path: str) -> int:
    """Line count for a repo-relative path."""
    return sum(1 for _ in Path(rel_path).open())


def render_tool_table() -> str:
    """Render the README tool table from ``scripts/tools.toml`` + introspection.

    Description templates may contain ``{actions}`` and ``{ecosystems_count}``
    placeholders.  ``actions_attr`` / ``ecosystems_attr`` import a module
    constant; ``actions = [...]`` / ``ecosystems = "..."`` hand-pin a value
    (used until the underlying module exposes a ``_VALID_ACTIONS`` tuple).

    Output is a bordered GFM table (matching the rest of ``docs/``) including
    header and separator rows so the cog block lives entirely outside the
    table — HTML comments inside a GFM table terminate it (spec §4.10), so
    the only working pattern is outer-marker injection regenerating the
    whole table at once.
    """
    import importlib
    import tomllib

    from tabulate import tabulate

    data = tomllib.loads(Path("scripts/tools.toml").read_text())
    rows: list[tuple[str, str, str]] = []
    for tool in data["tool"]:
        ctx: dict[str, str] = {}
        if "actions_attr" in tool:
            mod_path, attr = tool["actions_attr"].rsplit(".", 1)
            actions = getattr(importlib.import_module(mod_path), attr)
            ctx["actions"] = f"{len(actions)} actions: {', '.join(actions)}"
        elif "actions" in tool:
            actions = tool["actions"]
            ctx["actions"] = f"{len(actions)} actions: {', '.join(actions)}"
        if "ecosystems_attr" in tool:
            mod_path, attr = tool["ecosystems_attr"].rsplit(".", 1)
            eco_str = getattr(importlib.import_module(mod_path), attr)
            ecosystems = [e.strip() for e in eco_str.split(",")]
            ctx["ecosystems_count"] = str(len(ecosystems))
        desc = tool["description"].format(**ctx) if ctx else tool["description"]
        rows.append((tool["name"], tool["pascal"], desc))
    return tabulate(rows, headers=["Tool Name", "Claude Code Tool Name", "Description"], tablefmt="github")


def tool_count(*, with_optional: bool = False) -> str:
    """Render the registered-tool count from parkour_mcp/__init__.py.

    The base set is the always-on tool tuple in ``main()``.  When
    *with_optional* is true, the SemanticScholar entry (gated by
    ``S2_ACCEPT_TOS``) is included with a note.
    """
    src = Path("parkour_mcp/__init__.py").read_text()
    marker = 'tools: list[tuple[str, Callable[..., Any]]] = ['
    start = src.index(marker) + len(marker)
    end = src.index("]", start)
    base = sum(1 for line in src[start:end].splitlines() if line.strip().startswith("("))
    if with_optional:
        return f"{base} always-on tools, plus 1 optional (SemanticScholar, gated by S2_ACCEPT_TOS)"
    return f"{base} tools"
