# parkour-mcp development tasks

# Pack Claude Desktop Extension bundle
pack:
    mkdir -p dist
    npx @anthropic-ai/mcpb pack . dist/parkour-mcp.mcpb

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

# Install repo git hooks (one-time setup after cloning)
install-hooks:
    git config core.hooksPath scripts/git-hooks
    chmod +x scripts/git-hooks/*
    @echo "Git hooks installed. pre-push will run live tests on version tag pushes."

# Usage: just tag v1.2.3 — runs live tests, then creates annotated tag (no push)
tag version:
    @echo "Running live tests before creating tag {{version}}..."
    uv run pytest -m live
    git tag -a "{{version}}" -m "{{version}}"
    @echo "Tag {{version}} created locally. Push with: git push origin {{version}}"
