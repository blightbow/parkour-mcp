#!/usr/bin/env python3
"""Refresh ``_DEFAULT_LENSES`` in ``parkour_mcp/kagi.py`` from upstream Kagi docs.

The Kagi v1 ``lens_id`` parameter accepts built-in lens slugs that the
search API derives from the display names enumerated in
``kagisearch/kagi-docs/docs/kagi/features/lenses.md``.  We snapshot the
catalog into ``kagi.py`` between ``# --- DEFAULT_LENSES (generated; do
not edit between markers) ---`` and ``# --- END DEFAULT_LENSES ---`` so
the runtime carries no remote dependency.  Run this script when Kagi
adds a default lens; commit the resulting diff.

Operating modes:

* (default) Fetch, parse, rewrite the kagi.py block, exit 0.
* ``--check`` Fetch, parse, compare against the in-tree snapshot.  Exit
  1 if upstream has diverged.  Useful for CI drift gating.
* ``--dry-run`` Fetch, parse, print the rendered block to stdout.  No
  file writes; exit 0 always.

Slug derivation: lowercase the display name and preserve internal
spaces.  Underscore variants do not engage the lens at runtime.  This
matches the empirical behavior of the v1 ``lens_id`` parameter.
"""

from __future__ import annotations

import re
import sys
import urllib.request
from pathlib import Path

UPSTREAM_URL = (
    "https://raw.githubusercontent.com/kagisearch/kagi-docs/"
    "refs/heads/main/docs/kagi/features/lenses.md"
)
KAGI_PY = Path(__file__).resolve().parent.parent / "parkour_mcp" / "kagi.py"
BEGIN_MARKER = "# --- DEFAULT_LENSES (generated; do not edit between markers) ---"
END_MARKER = "# --- END DEFAULT_LENSES ---"

# Bullet lines look like ``- **Forums**: search forums from around the web.``
_BULLET_RE = re.compile(r"^\s*-\s+\*\*([^*]+)\*\*:\s*(.+?)\s*$")

# Heuristic for the "activation-gated" sub-list inside ## Default Lenses.
_GATE_PROSE = "need to be activated"


def _fetch_lenses_md() -> str:
    request = urllib.request.Request(
        UPSTREAM_URL,
        headers={"User-Agent": "parkour-mcp/generate_kagi_lenses"},
    )
    with urllib.request.urlopen(request, timeout=30) as resp:
        return resp.read().decode("utf-8")


def _slug_for(name: str) -> str:
    """Lowercase the display name and preserve internal spaces."""
    return name.lower()


def _parse_default_lenses(markdown: str) -> list[dict[str, object]]:
    """Pull the ## Default Lenses section apart into ordered lens entries.

    Returns a list of {name, slug, purpose, always_on} dicts in document
    order (always-on group first, activation-gated group second).
    """
    section_match = re.search(
        r"## Default Lenses\b(.*?)(?=\n## )",
        markdown, flags=re.DOTALL,
    )
    if not section_match:
        raise SystemExit(
            "Could not find '## Default Lenses' section in upstream markdown."
        )
    section = section_match.group(1)

    # Split on the activation-gate prose so we know which lenses are
    # always-on vs require user enablement.
    split = re.split(r"^.*" + _GATE_PROSE + r".*$", section, maxsplit=1, flags=re.MULTILINE)
    always_block = split[0]
    gated_block = split[1] if len(split) > 1 else ""

    def _collect(block: str, always_on: bool) -> list[dict[str, object]]:
        out: list[dict[str, object]] = []
        for line in block.splitlines():
            m = _BULLET_RE.match(line)
            if not m:
                continue
            name = m.group(1).strip()
            purpose = m.group(2).strip()
            out.append({
                "name": name,
                "slug": _slug_for(name),
                "purpose": purpose,
                "always_on": always_on,
            })
        return out

    lenses = _collect(always_block, always_on=True) + _collect(gated_block, always_on=False)

    if not lenses:
        raise SystemExit(
            "Parsed zero lenses — upstream format may have changed. "
            "Check the section excerpt and adjust the parser."
        )
    return lenses


def _render_block(lenses: list[dict[str, object]]) -> str:
    lines = [BEGIN_MARKER, '_DEFAULT_LENSES: list[dict[str, str | bool]] = [']
    for lens in lenses:
        name = str(lens["name"]).replace('"', '\\"')
        slug = str(lens["slug"]).replace('"', '\\"')
        purpose = str(lens["purpose"]).replace('"', '\\"')
        always_on = "True" if lens["always_on"] else "False"
        lines.append(
            f'    {{"name": "{name}", "slug": "{slug}", '
            f'"purpose": "{purpose}", "always_on": {always_on}}},'
        )
    lines.append("]")
    lines.append(END_MARKER)
    return "\n".join(lines)


def _replace_block(source: str, new_block: str) -> str:
    pattern = re.compile(
        re.escape(BEGIN_MARKER) + r".*?" + re.escape(END_MARKER),
        flags=re.DOTALL,
    )
    if not pattern.search(source):
        raise SystemExit(
            f"Markers not found in {KAGI_PY}; expected\n"
            f"  {BEGIN_MARKER}\n  ...\n  {END_MARKER}"
        )
    return pattern.sub(new_block, source, count=1)


def main(argv: list[str]) -> int:
    check_mode = "--check" in argv
    dry_run = "--dry-run" in argv

    upstream = _fetch_lenses_md()
    lenses = _parse_default_lenses(upstream)
    new_block = _render_block(lenses)

    if dry_run:
        print(new_block)
        return 0

    current_source = KAGI_PY.read_text()
    new_source = _replace_block(current_source, new_block)

    if check_mode:
        if current_source == new_source:
            print(f"kagi.py: _DEFAULT_LENSES in sync ({len(lenses)} lenses)")
            return 0
        print(
            f"kagi.py: _DEFAULT_LENSES is stale (upstream has {len(lenses)} lenses). "
            f"Run scripts/generate_kagi_lenses.py to refresh.",
            file=sys.stderr,
        )
        return 1

    if current_source == new_source:
        print(f"kagi.py: no change ({len(lenses)} lenses)")
        return 0

    KAGI_PY.write_text(new_source)
    print(f"kagi.py: refreshed _DEFAULT_LENSES ({len(lenses)} lenses)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
