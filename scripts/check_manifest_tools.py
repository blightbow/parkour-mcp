"""Verify ``manifest.json``'s ``tools`` array matches the registered set.

The manifest's tool descriptions are user-facing prose tailored to Claude
Desktop's tool picker, so they stay hand-curated.  The *names*, however,
must match the union of always-on and opt-in registrations in
``parkour_mcp.__init__``: a missing entry hides a real tool from the
desktop picker, and a stale entry advertises something that won't show
up at runtime.

Exits 1 with a diff when names drift.  Wired into ``just docs-drift``
so PRs that add/remove a tool registration and forget the manifest fail
CI alongside cog-derived prose drift.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from parkour_mcp import _ALWAYS_ON_TOOLS, _OPTIONAL_TOOLS
from parkour_mcp.common import TOOL_NAMES, init_tool_names


def _expected_names() -> set[str]:
    init_tool_names("desktop")
    names: set[str] = set()
    for internal_name, _ in _ALWAYS_ON_TOOLS:
        names.add(TOOL_NAMES[internal_name]["desktop"])
    for internal_name in _OPTIONAL_TOOLS:
        names.add(TOOL_NAMES[internal_name]["desktop"])
    return names


def main() -> int:
    expected = _expected_names()
    manifest = json.loads(Path("manifest.json").read_text())
    actual = {t["name"] for t in manifest["tools"]}

    missing = expected - actual
    extra = actual - expected
    if missing or extra:
        if missing:
            print(
                f"manifest.json missing tools: {sorted(missing)}",
                file=sys.stderr,
            )
        if extra:
            print(
                f"manifest.json has unregistered tools: {sorted(extra)}",
                file=sys.stderr,
            )
        return 1
    print(f"manifest.json: {len(expected)} tools match registration")
    return 0


if __name__ == "__main__":
    sys.exit(main())
