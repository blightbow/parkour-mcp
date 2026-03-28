# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project

**kagi-research-mcp** — an MCP server providing a research synthesis pipeline for targeted content extraction from websites and research papers. Integrates Kagi, Semantic Scholar, arXiv, MediaWiki, and DOI resolution APIs into a unified tool suite for Claude Code and Claude Desktop.

## Commands

```bash
# Run mocked unit tests (default, excludes live tests)
uv run pytest

# Run a single test file or specific test
uv run pytest tests/test_arxiv.py
uv run pytest tests/test_arxiv.py::test_function_name

# Run live integration tests (hits real endpoints)
uv run pytest -m live

# Regenerate README examples (live endpoints + Reddit fixtures)
uv run python3 scripts/regenerate_readme_examples.py
```

## Architecture

### Module Layout (`kagi_research_mcp/`)

- **`__init__.py`** — MCP server entry point. Registers 8 tools with profile-specific names (PascalCase for `code`, snake_case for `desktop`). Description templates have placeholders replaced at registration time.
- **`_pipeline.py`** — Shared processing layer. Owns the fast-path detection chain, single-entry caching (`_WikiCache`, `_PageCache`), slicing, BM25 search, and section filtering.
- **`markdown.py`** — HTML→markdown conversion via custom `TextOnlyConverter`. Section extraction with fuzzy slug matching. Content fencing. Semantic truncation for markdown, hard truncation for structured formats.
- **`shelf.py`** — Research shelf implementation. All public methods guarded by `asyncio.Lock`.

API integration modules (each ~300-650 LOC, self-contained):
- **`kagi.py`** — Search and summarize via kagiapi. Balance tracking with low-credit lockout.
- **`fetch_direct.py`** — Static HTTP fetching with content-type detection. Routes URLs through fast-path chain before falling back to HTTP.
- **`fetch_js.py`** — Playwright browser automation with live app detection (Gradio, Streamlit). ReAct-style interaction chains. Falls back to HTTP if Playwright unavailable.
- **`arxiv.py`** — arXiv Atom API. Field-prefix query syntax. 3s rate limit.
- **`semantic_scholar.py`** — S2 API with optimized field sets per query type. 1s rate limit (higher with `S2_API_KEY`).
- **`doi.py`** — DOI resolution via content negotiation. Registration agency detection. DataCite enrichment (ORCID, affiliations, licenses).
- **`mediawiki.py`** — Wikipedia/MediaWiki API. Probes for api.php endpoint. Full-page fetch with downstream section filtering.
- **`reddit.py`** — Reddit fast path via `old.reddit.com` `.json` endpoint. URL rewriting, comment tree parsing, section-based comment navigation. 2s rate limit.
- **`common.py`** — Shared constants: dual User-Agent strategy (browser UA for HTML, API UA for structured endpoints), `RateLimiter` class.

### Key Concepts

**Fast paths**: When a URL belongs to a known API-backed source (Wikipedia, arXiv, Semantic Scholar, DOI, Reddit), the server can skip the generic HTTP-fetch-and-convert path and instead call the source's structured API directly. This is faster, yields richer metadata, and avoids scraping. The detection chain in `fetch_direct.py` tests URLs in priority order: arXiv → Semantic Scholar → DOI → Reddit → MediaWiki → generic HTTP fallback.

**Slicing**: Long pages are split into chunks (~1600-2000 chars) at semantic boundaries (headings, paragraph breaks) using `semantic-text-splitter`. Each slice records its "ancestry" — which heading hierarchy it belongs to. The slices are indexed with tantivy for BM25 keyword search, so callers can search within a cached page or request specific slices by index rather than re-fetching the whole document.

**Content fencing**: Tool output contains content fetched from the open web, which could include prompt injection attempts. Untrusted content is wrapped in visible fence markers (`┌─ untrusted content` / `└─ untrusted content`) with every line prefixed by `│`. This per-line provenance marking survives truncation and context compression. See `docs/frontmatter-standard.md` for the full spec.

**Frontmatter**: Tool responses begin with a YAML `---` block containing structured metadata — source URL, API origin, pagination state, and actionable hints for the calling agent. Frontmatter lives *outside* the content fence (it's trusted, server-generated metadata, never external data). `hint` suggests a same-tool follow-up, `see_also` points to a different tool, `note` is explanatory. `_build_frontmatter()` is the sole producer of `---` blocks.

**Research shelf**: An in-memory citation tracker that accumulates papers passively as the agent inspects them through arXiv, Semantic Scholar, or DOI tools. Keyed by DOI with cross-DOI deduplication (preprint vs. journal versions). Supports scoring, notes, and export to BibTeX/RIS/JSON. Session-scoped — it resets when the MCP server restarts.

### Other Patterns

**Single-entry caching**: Only one URL is cached at a time (auto-evicts on new URL). This keeps memory bounded while supporting the common workflow of fetching a page's table of contents, then drilling into specific sections or searching within it.

**Profiles**: The server registers its tools under different naming conventions depending on the `--profile` flag. `code` uses PascalCase (`WebFetchDirect`), `desktop` uses snake_case (`web_fetch_direct`). Tool descriptions also adapt — they reference sibling tools by their profile-appropriate names.

### Environment Variables

| Variable | Purpose |
|---|---|
| `KAGI_API_KEY` | Kagi API key (fallback: `~/.config/kagi/api_key`) |
| `S2_API_KEY` | Semantic Scholar API key (fallback: `~/.config/kagi/s2_api_key`) |
| `MCP_CONTACT_EMAIL` | Enables CrossRef "polite pool" (10 req/s vs 5 req/s) |
| `PLAYWRIGHT_BROWSER` | Override browser for JS rendering |

## Testing

- Tests use `respx` for HTTP mocking and `pytest-asyncio` (strict mode) for async support.
- Fixtures in `conftest.py` provide sample responses and disable rate limiters.
- `test_live.py` contains integration tests deselected by default; run with `-m live`.
- Each test module maps to its source module (e.g., `test_arxiv.py` → `arxiv.py`).
- `scripts/regenerate_readme_examples.py` regenerates README example outputs. Most examples hit live endpoints; Reddit examples use `respx`-mocked fixtures for deterministic, offline output. Run after changing tool output format to keep examples current.

## Conventions

- Parameter conflicts (e.g., `search` + `section`) resolve by picking the strongest signal and emitting a warning, never an error.
- Rate limiters (`common.py`) use `asyncio.Lock` to serialize concurrent API calls; the second caller sleeps only for the remaining interval.
- arXiv `/html/` URLs are intentionally NOT fast-pathed — they contain full rendered text worth slicing, unlike `/abs/` which is just metadata.
- Reddit fast path uses browser UA (`_FETCH_HEADERS`), not API UA — the `.json` endpoint is a page variant, not a formal API, and Reddit blocks bot UAs on unauthenticated requests. This is intentionally NOT the official Reddit API; it requires no OAuth, no API key, and no approval process.

## Technical Debt

See @./claude/TECH_DEBT.md for acknowledged warnings and deferred fixes. When opting not to fix a warning, document it there with the location, issue, and rationale.
