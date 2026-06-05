#!/usr/bin/env python3
"""Refresh ``_REGION_CODES`` in ``parkour_mcp/kagi.py`` from upstream Kagi docs.

The Kagi v1 ``filters.region`` accepts a closed set of region codes whose
canonical source is the regional bangs table in
``kagisearch/kagi-docs``.  We snapshot that set into ``kagi.py`` between
``# --- REGION_CODES (generated; do not edit between markers) ---`` and
``# --- END REGION_CODES ---`` markers so the runtime carries no remote
dependency.  Run this script when Kagi adds a region code; commit the
resulting diff.

Operating modes:

* (default) Fetch, parse, rewrite the kagi.py block, exit 0.
* ``--check`` Fetch, parse, compare against the in-tree snapshot.  Exit
  1 if upstream has diverged.  Useful for CI drift gating.
* ``--dry-run`` Fetch, parse, print the rendered block to stdout.  No
  file writes; exit 0 always.

``int`` is filtered out: bangs.md documents it as the International
setting but the v1 search API rejects it for ``filters.region``.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import httpx

UPSTREAM_URL = (
    "https://raw.githubusercontent.com/kagisearch/kagi-docs/"
    "refs/heads/main/docs/kagi/features/bangs.md"
)
KAGI_PY = Path(__file__).resolve().parent.parent / "parkour_mcp" / "kagi.py"
BEGIN_MARKER = "# --- REGION_CODES (generated; do not edit between markers) ---"
END_MARKER = "# --- END REGION_CODES ---"
# Codes the bangs list documents but filters.region rejects.
EXCLUDED_CODES = {"int"}


def _fetch_bangs_md() -> str:
    """Download the upstream bangs.md verbatim."""
    resp = httpx.get(
        UPSTREAM_URL,
        headers={"User-Agent": "parkour-mcp/generate_kagi_regions"},
        timeout=30,
        follow_redirects=True,
    )
    resp.raise_for_status()
    return resp.text


def _parse_regional_bangs(markdown: str) -> dict[str, str]:
    """Extract the ``### Regional bangs`` table → code → display name."""
    section_match = re.search(
        r"### Regional bangs\b.*?(?=\n### |\Z)",
        markdown, flags=re.DOTALL,
    )
    if not section_match:
        raise SystemExit(
            "Could not find '### Regional bangs' section in upstream markdown. "
            "Upstream format may have changed."
        )
    section = section_match.group(0)

    out: dict[str, str] = {}
    for line in section.splitlines():
        if "|" not in line:
            continue
        parts = [p.strip() for p in line.split("|")]
        # Discard empty leading / trailing cells from `| foo | bar |` rows.
        cells = [p for p in parts if p != ""]
        if len(cells) < 2:
            continue
        code, name = cells[0], cells[1]
        # Skip the header row and the `---|---` separator row.
        if code == "Bang" or re.fullmatch(r"-+", code):
            continue
        if code in EXCLUDED_CODES:
            continue
        out[code] = name

    if not out:
        raise SystemExit(
            "Parsed zero region codes — upstream table format may have changed. "
            "Check the section excerpt and adjust the parser."
        )
    return out


def _render_block(codes: dict[str, str]) -> str:
    """Render the BEGIN..END block exactly as kagi.py should carry it."""
    lines = [BEGIN_MARKER, "_REGION_CODES: dict[str, str] = {"]
    for code, name in codes.items():
        safe_name = name.replace("\\", "\\\\").replace('"', '\\"')
        lines.append(f'    "{code}": "{safe_name}",')
    lines.append("}")
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

    upstream = _fetch_bangs_md()
    codes = _parse_regional_bangs(upstream)
    new_block = _render_block(codes)

    if dry_run:
        print(new_block)
        return 0

    current_source = KAGI_PY.read_text()
    new_source = _replace_block(current_source, new_block)

    if check_mode:
        if current_source == new_source:
            print(f"kagi.py: _REGION_CODES in sync ({len(codes)} codes)")
            return 0
        print(
            f"kagi.py: _REGION_CODES is stale (upstream has {len(codes)} codes). "
            f"Run scripts/generate_kagi_regions.py to refresh.",
            file=sys.stderr,
        )
        return 1

    if current_source == new_source:
        print(f"kagi.py: no change ({len(codes)} codes)")
        return 0

    KAGI_PY.write_text(new_source)
    print(f"kagi.py: refreshed _REGION_CODES ({len(codes)} codes)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
