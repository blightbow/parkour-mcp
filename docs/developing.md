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
```

The mocked suite (default `pytest` run) uses `respx` to stub HTTP calls and
runs in under 30 seconds. Live tests are opt-in via the `-m live` marker and
hit real endpoints — they require network access and some of them skip
gracefully if optional credentials (`GITHUB_TOKEN`, etc.) aren't available.

## Dead-code scanning

```bash
just lint-deep
```

Runs `vulture` against `parkour_mcp/` to surface unused functions, variables,
and branches that no production caller reaches. Cross-file analysis of this
shape is deliberately outside ruff's scope, so vulture fills the gap.

The scan is advisory — it prints findings but does not fail the recipe
while a known backlog of real findings still exists. Drop the leading `-`
in the `lint-deep` recipe once the backlog is cleared to convert it into
a hard gate.

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
that aren't safe to hand to GitHub Actions. Without a gate, format drift
could sneak into a tagged release before anyone noticed. Two gates catch it:

### 1. Preemptive: `just tag`

```bash
just tag v1.2.3
git push origin v1.2.3   # still manual, respects the Yubikey workflow
```

The recipe runs `pytest -m live` *before* creating the annotated tag. If
the live suite fails, no tag is created and nothing needs to be cleaned up.
Push is deliberately left as a separate manual step.

### 2. Safety net: `pre-push` hook

If a `v*` tag is pushed without going through `just tag` — e.g. someone
creates the tag manually with `git tag` and runs `git push --tags` — the
`pre-push` hook re-runs the live suite and blocks the push on failure.
Branch pushes are unaffected; the hook exits immediately without running
any tests when no version tag is in the push refspec.

Requires `just install-hooks` to have been run in this clone.

### Upstream outages

If live tests fail because an external endpoint is genuinely down (not a
format regression in our code), `git push --no-verify` bypasses the
`pre-push` hook. Verify the failure is actually upstream before using that
escape hatch — the whole point of the gate is to force a deliberate
override for a broken release.
