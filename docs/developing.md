# Developing parkour-mcp

Notes for working on parkour-mcp itself — test layout, release flow, and the
guard rails that keep format drift out of tagged releases.

## First-time setup

After cloning, install the repo git hooks:

```bash
just install-hooks
```

This sets `core.hooksPath` to `scripts/git-hooks`, so the hooks travel with
the repo instead of living in the untracked `.git/hooks/` directory. The
only hook currently installed is a `pre-push` guard for version tags —
see [Cutting a release](#cutting-a-release) below.

## Running tests

```bash
# Unit tests (mocked, no network)
uv run pytest

# Live integration tests (hits real endpoints)
uv run pytest -m live

# Performance regression tests against captured fixtures
uv run pytest -m perf

# Pack Claude Desktop Extension bundle
just pack

# Pack and open in Claude Desktop (macOS only — for local mcpb UAT)
just uat
```

The mocked suite (default `pytest` run) uses `respx` to stub HTTP calls and
runs in under 30 seconds. Live tests are opt-in via the `-m live` marker and
hit real endpoints — they require network access and some of them skip
gracefully if optional credentials (`GITHUB_TOKEN`, etc.) aren't available.

`just uat` packs the mcpb bundle into `dist/parkour-mcp.mcpb` and opens it
with `open` so Claude Desktop picks it up for local install. Useful for
manual UAT against a candidate build before tagging.

## Tool icons

Tool and server icons are generated from Noto fonts (SIL OFL 1.1) by
`scripts/generate_icons.py`. SVGs land in `parkour_mcp/assets/icons/` and
are loaded as `data:` URIs at server startup (the MCP `Icon` spec only
permits `https://` or `data:` sources). The mapping from internal tool
key to SVG filename and source glyph lives in `parkour_mcp/__init__.py`
under `_ICON_FILES`.

```bash
# Regenerate all icons (downloads Noto fonts to a gitignored cache on first run)
just icons
```

To add an icon for a new tool, append the tool key + glyph to
`scripts/generate_icons.py`, run `just icons`, and add the matching entry
to `_ICON_FILES` in `__init__.py`. Icons are shipped as package data
(`[tool.setuptools.package-data]` in `pyproject.toml`) so they ride along
in both the wheel and the mcpb bundle.

## Dead-code scanning

```bash
just lint-deep
```

Runs `vulture` against `parkour_mcp/` to surface unused functions, variables,
and branches that no production caller reaches. Cross-file analysis of this
shape is deliberately outside ruff's scope, so vulture fills the gap.

The recipe is a **hard gate** — vulture exits non-zero on any finding,
failing the recipe. Real findings should be fixed at the source (delete
dead code, wire up orphaned callers, rename loop variables). For the
narrow set of genuine vulture blind spots (decorator-registered handlers,
base-class method dispatch, test-only hooks), add an entry to
`.vulture_whitelist.py` with a comment explaining why the finding is
unreachable to vulture.

`tests/` is intentionally **not** scanned: pytest idioms (`pytestmark`,
autouse fixtures, `mock.return_value` attribute writes) produce ~45 false
positives with essentially no signal, so the cost/benefit is negative.

False positives for code vulture cannot see through (decorator-registered
handlers, base-class method dispatch, test-only hooks) live in
`.vulture_whitelist.py`. Real findings must not be added to the whitelist —
they get fixed in the source. To regenerate the candidate list for triage:

```bash
uv run vulture parkour_mcp/ --make-whitelist
```

Then copy **only** the false positives over, with a comment explaining why
each one is unreachable to vulture.

## Cutting a release

Live tests don't run in CI — they need real API endpoints and credentials
that aren't safe to hand to GitHub Actions. The mocked suite does run in
CI on tag push (via the `Release` workflow's `uv run pytest` step), but
catching a mocked-suite regression at tag-push time means a dead tag on
origin with no release artifact. Two gates catch both classes of failure
before anything hits a remote:

### 1. Preemptive: `just tag`

```bash
just tag v1.2.3
git push origin v1.2.3   # still manual, respects the Yubikey workflow
```

The recipe runs the mocked suite *first* (fast, ~15 s, includes
`pytest-ruff` lint across `parkour_mcp/`, `tests/`, and `scripts/`) and
then the live suite. If either fails, no tag is created and nothing
needs to be cleaned up. Push is deliberately left as a separate manual
step.

The mocked run is the same command CI executes on tag push, so anything
that trips it locally would also fail CI and leave a broken release —
see the v1.1.1 ruff-E402 regression that the previous "live-only" gate
missed. Keep both steps in sync with the CI workflow.

### 2. Safety net: `pre-push` hook

If a `v*` tag is pushed without going through `just tag` — e.g. someone
creates the tag manually with `git tag` and runs `git push --tags` — the
`pre-push` hook re-runs both suites (mocked first, then live) and blocks
the push on failure. Branch pushes are unaffected; the hook exits
immediately without running any tests when no version tag is in the push
refspec.

Requires `just install-hooks` to have been run in this clone.

### Upstream outages

If the **live** suite fails because an external endpoint is genuinely
down (not a format regression in our code), `git push --no-verify`
bypasses the `pre-push` hook. Verify the failure is actually upstream
before using that escape hatch — the whole point of the gate is to force
a deliberate override for a broken release.

Do **not** use `--no-verify` to skip a **mocked** suite failure. Those
are the same tests CI runs; bypassing the hook just produces a dead tag
on origin with no PyPI artifact and no GitHub Release. Fix the
underlying issue locally instead.
