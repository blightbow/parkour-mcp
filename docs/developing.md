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

Releasing also needs actionlint and shellcheck, which gate every version
tag (see [Workflow linting](#workflow-linting)):

```bash
brew install actionlint shellcheck
```

Then install semgrep, which `uv sync` does *not* provide:

```bash
brew install semgrep      # or: uv tool install semgrep
```

`tests/test_semgrep_rules.py` runs the project ruleset in `.semgrep/` as
part of the normal suite. It shells out to the binary via `shutil.which()`
and never imports the package, so semgrep is a tool, not a dependency —
and deliberately absent from the `dev` dependency group. Declaring it there
dragged its hard `mcp==1.23.3` pin into our resolve, holding the lockfile
six minors below what a fresh consumer install of parkour-mcp gets. That
pin is intentional on semgrep's side ([semgrep#11476][sg-pin]) and stays
exact even in the in-flight bump to `1.28.1` ([semgrep#11808][sg-bump]);
running semgrep in its own environment is what its maintainers recommend
for exactly this conflict ([semgrep#11506][sg-extra]).

**A missing binary fails the suite rather than skipping it.** These rules
are a security gate (SSRF precedence, content fencing), and a gate that
quietly vanishes on a machine without the binary is worse than no gate —
the suite goes green and nothing reports that the checks never ran. To run
without it, opt out explicitly:

```bash
PARKOUR_SKIP_SEMGREP=1 uv run pytest
```

[sg-pin]: https://github.com/semgrep/semgrep/issues/11476
[sg-bump]: https://github.com/semgrep/semgrep/pull/11808
[sg-extra]: https://github.com/semgrep/semgrep/issues/11506

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

## Debugging live behavior

`parkour-mcp call` invokes one registered tool from the shell through the
production path: profile naming, the rate limiter, the transport with its
identity policy, the caches, and the frontmatter. Nothing is reimplemented,
so what you observe is what the MCP server would have done. Prefer it to an
ad-hoc `httpx` or `curl` reproduction, which differs from production in the
HTTP version, header set, and TLS fingerprint, and which bypasses the
limiter the code enforces.

```bash
# Run a tool; its output goes to stdout
uv run parkour-mcp --profile code call ArXiv '{"action": "search", "query": "ti:attention"}'

# Record every outbound exchange as JSON lines (- for stderr)
uv run parkour-mcp call ArXiv '{"action": "paper", "query": "2505.10465"}' --trace -

# Build the first request and stop before sending it (zero network cost)
uv run parkour-mcp call ArXiv '{"action": "search", "query": "abs:\"vision tokens\""}' --dry-run --as-curl

# Render each exchange as a curl command behind its rate-limit contract
uv run parkour-mcp call WebFetchIncisive '{"url": "https://example.com"}' --as-curl

# Same call over HTTP/1.1 on both transports, changing nothing else
uv run parkour-mcp call WebFetchIncisive '{"url": "https://example.com"}' --http1 --as-curl

# Record the responses as a fixture tests can mount
uv run parkour-mcp call ArXiv '{"action": "paper", "query": "2505.10465"}' --record tests/fixtures/arxiv_paper.json
```

Tool names accept the internal key (`arxiv`), the Claude Code name
(`ArXiv`), or the Desktop name (`arxiv`), case-insensitively. Arguments
are one JSON object, or `-` to read it from stdin.

`PARKOUR_TRACE=<path>` (or `-`) turns the same wire trace on inside the
MCP server, so an incident in Claude Desktop or Claude Code yields the
request as sent, the status, the locating response headers (`server`,
`via`, `x-cache`, `age`, `retry-after`, and the rate-limit family), the
elapsed time, and the limiter that governed the call. That record is what
`--as-curl` and `--dry-run` read; see `parkour_mcp/_trace.py`.

`--http1` exists because a WAF coherence gate scores the HTTP version
against the claimed identity, and the vendors this project has met want
opposite pairings (`TECH_DEBT.md`, generic fetch transport). Changing the
version and nothing else is how to learn which pairing an origin refuses;
the trace records the version that was actually negotiated.

`--record FILE` writes the call's responses as a fixture, with the
wire-encoding headers removed since the transport already decoded the
body. `tests/_replay.py#mount_fixture` mounts it as respx routes inside
`respx.mock`, so a live observation becomes an offline regression test
without transcribing the response by hand.

Two rules the harness enforces rather than documents. It never bypasses a
rate limiter: every `RateLimiter` carries its contract (`name`, `policy`,
`url`), and `--as-curl` prints that contract with the `sleep` a loop must
add back, or says plainly that no in-process limiter governed the request.
And it never invents an equivalent client: the curl command is rendered
from the record of what the tool sent. On the generic path the tool sends
wreq's Chrome fingerprint, which curl cannot reproduce, and the rendering
says so, because a refusal seen by curl is not evidence about the tool.

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

## Workflow linting

Changes under `.github/workflows/` are linted with
[actionlint](https://github.com/rhysd/actionlint), which also runs
[shellcheck](https://www.shellcheck.net/) over every `run:` script:

```bash
brew install actionlint shellcheck
just lint-workflows
```

It catches what a YAML parser cannot: expression type errors, references to
steps or outputs that do not exist, and untrusted event fields such as a PR
title interpolated straight into a shell script. The recipe requires both
binaries, because actionlint skips the shellcheck pass silently when
shellcheck is not on PATH.

`just tag` and the `pre-push` hook both run it before a version tag is
created or pushed, so a missing binary blocks a release. `release.yml` runs
only once a tag is pushed, which is otherwise the first moment a workflow
defect would surface.

Actions are pinned to a commit SHA with the release as a trailing comment
(`uses: owner/repo@<sha> # vX.Y.Z`), and `.github/dependabot.yml` proposes
updates weekly. Keep that shape when adding an action: a tag or branch ref
can be moved by whoever controls the upstream repository, and the release
job holds the OIDC rights to publish to PyPI and the MCP Registry.

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

The recipe first runs the no-network checks: `just docs-drift`,
[`just lint-workflows`](#workflow-linting), and the version-file sync. It
then runs the mocked suite (fast, ~15 s, includes `pytest-ruff` lint
across `parkour_mcp/`, `tests/`, and `scripts/`) and finally the live
suite. If either fails, no tag is created and nothing
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

Live tests declare the third-party hosts they need with
`@pytest.mark.requires_live("<key>")`. Each host is probed once per run,
and a test whose host is not serving reports **ERROR** with
`liveness gate: <url> unreachable (<reason>)` rather than running. An
ERROR means "did not run"; a **FAILED** test ran and its assertions
failed, which is a regression.

If a host outage is confirmed, waive that host for one command by naming
its key:

```bash
PARKOUR_LIVE_ALLOW_DOWN=ultimacodex just tag v1.2.3
PARKOUR_LIVE_ALLOW_DOWN=ultimacodex git push origin v1.2.3
```

The waiver turns a failed probe for the named hosts into a skip, and the
run ends with a `liveness waiver` section stating how many tests each
waived host cost. It does nothing for a host that is serving, never
waives an assertion failure, and rejects an unknown key rather than
ignoring it. Separate several keys with commas; the known keys are in
`tests/conftest.py#_LIVE_PROBES`. Because it is an environment variable,
it applies only to the command it is set on.

Do **not** use `git push --no-verify`. It skips the whole hook: the
docs-drift check, the dead-code scan, and the mocked suite as well as the
live one. A mocked-suite failure pushed that way produces a dead tag on
origin with no PyPI artifact and no GitHub Release.
