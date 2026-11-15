# parkour-mcp development tasks

# Pack Claude Desktop Extension bundle
pack:
    mkdir -p dist
    npx @anthropic-ai/mcpb pack . dist/parkour-mcp.mcpb

# If manifest.dev.json exists (gitignored), its keys are deep-merged over
# manifest.json in a separate dist/parkour-mcp-dev.mcpb, e.g.
# {"version": "2.5.0-dev.1"}. An overlay older than manifest.json is refused
# before packing (see _uat-overlay-fresh). Assumes Claude Desktop on macOS.
# Pack the bundle and open it in Claude Desktop for local UAT
uat: _uat-overlay-fresh pack
    #!/usr/bin/env bash
    set -euo pipefail
    if [[ ! -f manifest.dev.json ]]; then
        open dist/parkour-mcp.mcpb
        exit 0
    fi
    mkdir -p dist/uat
    jq -s '.[0] * .[1]' manifest.json manifest.dev.json > dist/uat/manifest.json
    cp dist/parkour-mcp.mcpb dist/parkour-mcp-dev.mcpb
    zip -qj dist/parkour-mcp-dev.mcpb dist/uat/manifest.json
    echo "Overlaid manifest.dev.json: version $(jq -r .version dist/uat/manifest.json)"
    open dist/parkour-mcp-dev.mcpb

# Run unit tests (mocked, excludes live)
test *args:
    uv run pytest {{args}}

# Run live integration tests
test-live:
    uv run pytest -m live

# Run performance regression tests against captured fixtures
test-perf:
    uv run pytest -m perf

# Run the pipeline benchmark (pass --update-baselines or --capture-fixtures as args)
benchmark *args:
    uv run python3 scripts/benchmark_pipeline.py {{args}}

# Regenerate README examples
readme:
    uv run python3 scripts/regenerate_readme_examples.py

# Regenerate tool/server icon SVGs from Noto fonts (downloads fonts on first run)
icons:
    uv run python3 scripts/generate_icons.py

# Check that prose-derived facts in CLAUDE.md / docs/ still match source.
# Cog --check exits 1 if regenerated content would differ; drift check exits
# 1 if any anchored symbol has drifted past its sig: in drift.lock. The last
# two steps check what neither covers: manifest.json's tool names, and
# backticked `path#Symbol` citations, which drift reads as plain text.
docs-drift:
    uv run cog --check --check-fail-msg='Run `just docs-drift-fix` to regenerate.' CLAUDE.md README.md docs/frontmatter-standard.md
    drift check
    uv run python3 scripts/check_manifest_tools.py
    uv run python3 scripts/check_doc_citations.py

# Regenerate cog blocks; still need to manually drift link --doc-is-still-accurate
# any drifted anchors (see .claude/skills/drift/SKILL.md for the relink workflow).
docs-drift-fix:
    uv run cog -r CLAUDE.md README.md docs/frontmatter-standard.md
    @echo "Cog regenerated. Run 'drift check' next; if anchors are stale, follow the relink workflow."

# Run vulture dead-code scan on production code (honors .vulture_whitelist.py).
# Vulture exits 3 on findings, which fails this recipe. The same scan gates
# version-tag pushes from scripts/git-hooks/pre-push, so a finding left here
# blocks a release for anyone who has run `just install-hooks`. Real findings
# should be fixed at the source (or, for genuine vulture blind spots, added
# to .vulture_whitelist.py with a comment explaining why the finding is
# unreachable to vulture).
lint-deep:
    uv run vulture parkour_mcp/ .vulture_whitelist.py

# Reproduce the ty language server's hint-only diagnostics from the shell.
# "Code is unreachable" and "`x` is unused" are LSP hints with no rule name, so
# `ty check` never prints them (astral-sh/ty#4033). Two stand-ins get close:
# redundant-condition-strict reports the always-true/false condition that makes
# a block unreachable, on the condition's line rather than the dead block's,
# and ruff's ARG rules report unused arguments. Unreachable code after a
# return or raise has no stand-in. Advisory only: both rules carry known false
# positives, so this always exits 0 and gates nothing. Pass paths to narrow it.
lint-hints *paths="parkour_mcp/":
    uv run ty check --exit-zero --output-format concise --warn redundant-condition-strict {{paths}}
    uv run ruff check --exit-zero --output-format concise --select ARG {{paths}}

# Lint .github/workflows/ with actionlint: expression and type errors, unknown
# step references, untrusted input interpolated into run: scripts, and the
# scripts themselves via shellcheck. actionlint drops the shellcheck pass
# without a word when shellcheck is not on PATH, so both are required here
# rather than letting the scan quietly cover less.
lint-workflows:
    #!/usr/bin/env bash
    set -euo pipefail
    missing=()
    for tool in actionlint shellcheck; do
        command -v "$tool" >/dev/null || missing+=("$tool")
    done
    if (( ${#missing[@]} )); then
        echo "lint-workflows: missing ${missing[*]}; install with: brew install ${missing[*]}" >&2
        exit 1
    fi
    actionlint
    echo "lint-workflows: .github/workflows/ clean"

# manifest.dev.json pins a version for the UAT bundle, and manifest.json moves
# under it with every release bump. An overlay written before the last bump
# still carries the version it was written for, so the UAT bundle installs as
# a downgrade of the release it is meant to test. Its being older than
# manifest.json is the sign: edit it for the current version, or touch it to
# confirm it is still right, and rerun.
[private]
_uat-overlay-fresh:
    #!/usr/bin/env bash
    set -euo pipefail
    [[ -f manifest.dev.json ]] || exit 0
    if [[ manifest.dev.json -ot manifest.json ]]; then
        echo "uat: manifest.dev.json is older than manifest.json, so its overlay may predate the last version bump." >&2
        echo "uat:   manifest.json      version $(jq -r .version manifest.json)" >&2
        echo "uat:   manifest.dev.json  version $(jq -r '.version // "(not set)"' manifest.dev.json)" >&2
        echo "uat: Update manifest.dev.json for the current version, or touch it if it is still correct, then rerun." >&2
        exit 1
    fi

# Install repo git hooks (one-time setup after cloning)
install-hooks:
    git config core.hooksPath scripts/git-hooks
    chmod +x scripts/git-hooks/*
    @echo "Git hooks installed. pre-push will run live tests on version tag pushes."

# Usage: just tag v1.2.3 — runs doc drift, workflow lint, and version drift
# checks, then the mocked and live suites, then creates the annotated tag (no
# push). Mocked suite runs
# before live because it's fast and includes pytest-ruff, which catches
# format/lint regressions that CI's `uv run pytest` would also fail on.
# Skipping this step let a ruff E402 regression escape to the v1.1.1 release
# tag. The sync check catches drift between pyproject.toml, manifest.json, and
# server.json before the tag escapes, since the CI workflow's tag-vs-pyproject
# check only sees the one file.
#
# docs-drift goes first: it is the cheapest gate (no network, ~1 s) and it is
# the only one that catches prose describing code that has since moved. It
# runs here rather than in CI because CI only sees a tag that already exists,
# and a stale doc is not worth deleting and recutting a tag over — it is worth
# never tagging in the first place.
#
# lint-workflows follows for the same reason: release.yml only runs when a tag
# is pushed, so a workflow defect otherwise surfaces as a failed release run on
# a tag that already exists. It needs actionlint and shellcheck installed.
tag version:
    @echo "Checking docs against code (cog, drift anchors, manifest tools)..."
    just docs-drift
    @echo "Linting .github/workflows/ (actionlint + shellcheck)..."
    just lint-workflows
    @echo "Checking pyproject.toml / manifest.json / server.json sync..."
    uv run python3 scripts/sync_versions.py --check
    @echo "Running mocked test suite (incl. ruff lint) before creating tag {{version}}..."
    uv run pytest
    @echo "Running live test suite before creating tag {{version}}..."
    uv run pytest -m live
    git tag -a "{{version}}" -m "{{version}}"
    @echo "Tag {{version}} created locally. Push with: git push origin {{version}}"

# Preview the release: show the next version commitizen would cut and the
# CHANGELOG entry git-cliff would assemble, without writing anything.
release-preview:
    @echo "Next version (from commits since last tag):"
    @uv run cz bump --get-next --yes
    @echo ""
    @echo "CHANGELOG.md entry that would be prepended:"
    @git cliff --tag "v$(uv run cz bump --get-next --yes 2>/dev/null)" --unreleased --strip all
