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

# Usage: just tag v1.2.3 — runs mocked + live suites, then creates annotated tag (no push).
# Mocked suite runs first because it's fast and includes pytest-ruff, which
# catches format/lint regressions that CI's `uv run pytest` would also fail on.
# Skipping this step let a ruff E402 regression escape to the v1.1.1 release tag.
tag version:
    @echo "Running mocked test suite (incl. ruff lint) before creating tag {{version}}..."
    uv run pytest
    @echo "Running live test suite before creating tag {{version}}..."
    uv run pytest -m live
    git tag -a "{{version}}" -m "{{version}}"
    @echo "Tag {{version}} created locally. Push with: git push origin {{version}}"
