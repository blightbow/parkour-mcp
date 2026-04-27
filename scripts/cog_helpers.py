"""Generators imported by cog blocks in CLAUDE.md, README.md, and docs/.

Pattern lifted from scientific-python/cookie: keep cog blocks in prose to
1-3 lines that import and call into here, so the introspection code stays
under ruff/ty/test scope rather than hiding inside HTML-comment markers.
"""

from __future__ import annotations

from collections.abc import Sequence
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


def _human_align(md_table: str) -> str:
    """Trim tabulate's uniform last-column pad to the human-aligned style.

    Tabulate sizes every column — including the last — to the widest cell
    in that column, leaving a wall of trailing whitespace before the
    closing ``|`` on every line.  The convention in this project's hand-
    written ``docs/`` tables is: pad internal columns across rows for
    alignment, but size the last column to its header and let data rows
    end ragged.  This rewrites tabulate output to match.

    Not a library feature anywhere: tabulate#392 (closed wontfix) and
    prettier#12074 (open since 2022) are the canonical upstream
    discussions; both maintainers point users at exactly this kind of
    post-processor.
    """
    lines = md_table.splitlines()
    if len(lines) < 2:
        return md_table
    header = lines[0]
    last_pipe = header.rfind("|")
    penul_pipe = header.rfind("|", 0, last_pipe)
    if penul_pipe < 0:
        return md_table
    last_cell_text = header[penul_pipe + 1 : last_pipe].strip()
    new_last_h = f" {last_cell_text} "
    new_header = header[: penul_pipe + 1] + new_last_h + "|"
    sep = lines[1]
    sep_penul = sep.rfind("|", 0, sep.rfind("|"))
    new_sep = sep[: sep_penul + 1] + ("-" * len(new_last_h)) + "|"
    new_data = []
    for line in lines[2:]:
        d_last = line.rfind("|")
        d_penul = line.rfind("|", 0, d_last)
        last_cell = line[d_penul + 1 : d_last].rstrip()
        new_data.append(line[: d_penul + 1] + last_cell + " |")
    return "\n".join([new_header, new_sep, *new_data])


def render_table_adaptive(
    headers: Sequence[str],
    rows: Sequence[Sequence[object]],
    *,
    threshold: int = 120,
) -> str:
    """Render a GFM table, switching style based on whether rows fit *threshold*.

    Two display modes (matching the boolean axis exposed in
    ``wooorm/markdown-table`` as ``alignDelimiters``):

    - **Narrow** (max row width <= *threshold*): uniform-padded GFM —
      tabulate's default.  Closing pipes align vertically; the table
      reads as a sharp grid.
    - **Wide** (max row width > *threshold*): internal columns aligned
      across rows but the last column trimmed to ``<content> |`` per
      row.  Avoids the wall of trailing whitespace that uniform padding
      produces when the longest row dominates everyone else's pad.

    The threshold framing was endorsed by ``wooorm`` in
    ``remarkjs/remark-gfm#46`` ("There are currently two ways to display
    tables. I can see 'dynamically' switching between them as an
    improvement.") but never landed upstream.  Default 120 matches the
    print-width convention shared by Black, JetBrains, and ``glow``.
    """
    from tabulate import tabulate

    uniform = tabulate(rows, headers=list(headers), tablefmt="github")
    max_width = max(len(line) for line in uniform.splitlines())
    if max_width <= threshold:
        return uniform
    return _human_align(uniform)


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
    return render_table_adaptive(
        ["Tool Name", "Claude Code Tool Name", "Description"], rows
    )


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
    rows = [(f"`{k}`", _FM_KEY_CONTRIBUTORS[k]) for k in protected_keys()]
    return render_table_adaptive(["Key", "Typical contributors"], rows)


def tool_count(*, with_optional: bool = False) -> str:
    """Render the registered-tool count from ``parkour_mcp._ALWAYS_ON_TOOLS``.

    When *with_optional* is true, ``_OPTIONAL_TOOLS`` is summarized
    alongside (currently just SemanticScholar, gated by ``S2_ACCEPT_TOS``).
    """
    from parkour_mcp import _ALWAYS_ON_TOOLS, _OPTIONAL_TOOLS
    base = len(_ALWAYS_ON_TOOLS)
    if with_optional:
        n_opt = len(_OPTIONAL_TOOLS)
        plural = "tool" if n_opt == 1 else "tools"
        return f"{base} always-on tools, plus {n_opt} optional (SemanticScholar, gated by S2_ACCEPT_TOS)" if n_opt == 1 else f"{base} always-on tools, plus {n_opt} optional {plural}"
    return f"{base} tools"
