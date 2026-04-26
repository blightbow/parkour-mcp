# parkour-mcp development tasks

# Pack Claude Desktop Extension bundle
pack:
    mkdir -p dist
    npx @anthropic-ai/mcpb pack . dist/parkour-mcp.mcpb

# Locally test mcpb (assumes Claude Desktop+MacOS)
uat: pack
    open dist/parkour-mcp.mcpb

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
# 1 if any anchored symbol has drifted past its sig: in drift.lock.
docs-drift:
    uv run cog --check --check-fail-msg='Run `just docs-drift-fix` to regenerate.' CLAUDE.md README.md docs/frontmatter-standard.md
    drift check

# Regenerate cog blocks; still need to manually drift link --doc-is-still-accurate
# any drifted anchors (see .claude/skills/drift/SKILL.md for the relink workflow).
docs-drift-fix:
    uv run cog -r CLAUDE.md README.md docs/frontmatter-standard.md
    @echo "Cog regenerated. Run 'drift check' next; if anchors are stale, follow the relink workflow."

# Run vulture dead-code scan on production code (honors .vulture_whitelist.py).
# Hard gate — vulture exits 3 on findings, which fails the recipe and
# any wrapping pipeline. Real findings should be fixed at the source
# (or, for genuine vulture blind spots, added to .vulture_whitelist.py
# with a comment explaining why the finding is unreachable to vulture).
lint-deep:
    uv run vulture parkour_mcp/ .vulture_whitelist.py

# Install repo git hooks (one-time setup after cloning)
install-hooks:
    git config core.hooksPath scripts/git-hooks
    chmod +x scripts/git-hooks/*
    @echo "Git hooks installed. pre-push will run live tests on version tag pushes."

# Usage: just tag v1.2.3 — runs version-drift check + mocked + live suites,
# then creates annotated tag (no push). Mocked suite runs first because it's
# fast and includes pytest-ruff, which catches format/lint regressions that
# CI's `uv run pytest` would also fail on. Skipping this step let a ruff E402
# regression escape to the v1.1.1 release tag. The sync check catches
# drift between pyproject.toml, manifest.json, and server.json before the
# tag escapes, since the CI workflow's tag-vs-pyproject check only sees
# the one file.
tag version:
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
