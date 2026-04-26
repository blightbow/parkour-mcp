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


def protected_keys() -> tuple[str, ...]:
    """Return ``FMEntries.PROTECTED_ORDER`` — the canonical-order tuple of
    multi-contributor keys.  Used to drive prose, count, and table cog
    blocks in ``docs/frontmatter-standard.md``.
    """
    from parkour_mcp.markdown import FMEntries
    return FMEntries.PROTECTED_ORDER


def protected_keys_inline() -> str:
    """Render the protected keys as inline prose: ``\\`a\\`, \\`b\\`, and \\`c\\```."""
    keys = [f"`{k}`" for k in protected_keys()]
    if len(keys) <= 1:
        return keys[0] if keys else ""
    if len(keys) == 2:
        return f"{keys[0]} and {keys[1]}"
    return ", ".join(keys[:-1]) + f", and {keys[-1]}"


_NUMBER_WORDS: dict[int, str] = {
    1: "One", 2: "Two", 3: "Three", 4: "Four", 5: "Five",
    6: "Six", 7: "Seven", 8: "Eight", 9: "Nine", 10: "Ten",
}


def protected_keys_count_word() -> str:
    """Render the protected keys count as a capitalized English word."""
    return _NUMBER_WORDS.get(len(protected_keys()), str(len(protected_keys())))


_FM_KEY_CONTRIBUTORS: dict[str, str] = {
    "hint": "pagination advisories, truncation drill-ins, search-parser guidance, fragment-resolution hints",
    "warning": "rate-limit advisories, balance warnings, parameter-conflict notices",
    "note": "shelving side-effects, behavior-explaining annotations, correction notices",
    "see_also": "cross-tool pointers, related-resource references",
    "alert": "retraction / expression-of-concern notices (retroactively invalidating prior output)",
}


def protected_keys_table() -> str:
    """Render the multi-contributor keys table for docs/frontmatter-standard.md.

    Key column is derived from FMEntries.PROTECTED_ORDER; the contributors
    column is hand-curated prose in this module (small, rarely-changing).
    """
    from tabulate import tabulate
    rows = [(f"`{k}`", _FM_KEY_CONTRIBUTORS[k]) for k in protected_keys()]
    return tabulate(rows, headers=["Key", "Typical contributors"], tablefmt="github")


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
