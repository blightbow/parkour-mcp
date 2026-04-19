# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project

**parkour-mcp** — an MCP server providing a content exploration and research synthesis pipeline. Uses clean first-party APIs to surface and explore web content without summarization. Integrates Kagi, Semantic Scholar, arXiv, deps.dev, IETF, GitHub, MediaWiki, Reddit, Discourse, and DOI resolution APIs into a unified tool suite for Claude Code and Claude Desktop.

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

# Pack Claude Desktop Extension bundle
just pack

# Preview next release (version + CHANGELOG entry), no writes
just release-preview
```

## Architecture

### Module Layout (`parkour_mcp/`)

- **`__init__.py`** — MCP server entry point. Registers 12 tools with profile-specific names (PascalCase for `code`, snake_case for `desktop`). Description templates have placeholders replaced at registration time.
- **`_pipeline.py`** — Shared processing layer. Owns the fast-path detection chain, multi-entry caching (`_WikiCache` LRU, `_PageCache` 2Q), slicing, BM25 search, and section filtering.
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
- **`github.py`** — GitHub REST API integration. 7 tool actions (search_issues, search_code, repo, tree, issue, pull_request, file). Three-tier auth (env → config file → unauthenticated). Per-resource rate limit tracking. URL detection for fast-path chain covering blob (with line anchors), tree, issue, PR, wiki, commit, compare, releases, org/user profiles, gist, and `raw.githubusercontent.com`. Source code sectionization via tree-sitter CodeSplitter. CITATION.cff parsing for research shelf integration. OpenSSF Scorecard enrichment on `repo` and `file` actions via `scorecard.py`. ~1600 LOC.
- **`ietf.py`** — IETF RFC and Internet-Draft integration. 4 tool actions (rfc, search, draft, subseries). RFC Editor per-document JSON for metadata and relationship chains (obsoletes/updates). IETF Datatracker REST API for search with status/WG filtering. BibXML service for subseries (STD/BCP/FYI) resolution. Native DOI tracking (`10.17487/RFC{N}`). 1s Datatracker rate limit.
- **`packages.py`** — deps.dev (Google Open Source Insights) integration. 5 tool actions (package, version, dependencies, project, advisory). Covers 7 ecosystems (npm, PyPI, Go, Maven, Cargo, NuGet, RubyGems). Version history, license detection, security advisories (GHSA/CVE with CVSS), resolved dependency graphs with native constraints, OpenSSF Scorecard, OSS-Fuzz coverage, and SLSA provenance. No auth required. 1s politeness rate limit. Body content fenced (contributor-supplied fields are injection vectors). ~480 LOC.
- **`discourse.py`** — Discourse forum integration. 3 tool actions (topic, search, latest). Detects Discourse instances via `x-discourse-route` response header (post-fetch, not URL-based). Two-request topic assembly: first page inline + batch remaining via `post_ids[]`. Raw author markdown via `include_raw=true`. Per-host rate limiting via lazy-initialized dict. Quote BBCode → blockquote conversion, `upload://` ref cleanup. Post-aware BM25 splitting and reply-threaded section trees. No auth required. ~490 LOC.
- **`scorecard.py`** — OpenSSF Scorecard client. Queries `api.securityscorecards.dev/projects/github.com/{owner}/{repo}` directly (not via deps.dev). Returns the overall 0-10 score for frontmatter enrichment on `github:repo` and `github:file`. Session-lived per-repo cache; silent degrade on 404 / network error (missing key omitted, not nulled). Unauthenticated, CDN-fronted. ~60 LOC.
- **`common.py`** — Shared constants: dual User-Agent strategy (browser UA for HTML, API UA for structured endpoints), `RateLimiter` class, `s2_enabled()` gate, `_LANGUAGE_MAP` for file extension → syntax highlight language.

### Key Concepts

**Fast paths**: When a URL belongs to a known API-backed source (Wikipedia, arXiv, Semantic Scholar, DOI, Reddit, GitHub, Discourse), the server can skip the generic HTTP-fetch-and-convert path and instead call the source's structured API directly. This is faster, yields richer metadata, and avoids scraping. The pre-fetch detection chain in `fetch_direct.py` tests URLs in priority order: arXiv → Semantic Scholar → IETF → DOI → Reddit → GitHub → MediaWiki → generic HTTP fallback. Discourse uses post-fetch detection via the `x-discourse-route` response header — after the initial HTTP fetch, the header is checked and the URL is re-fetched via the JSON API if detected.

**Slicing**: Long pages are split into chunks (~1600-2000 chars) at semantic boundaries (headings, paragraph breaks) using `semantic-text-splitter`. Each slice records its "ancestry" — which heading hierarchy it belongs to. The slices are indexed with tantivy for BM25 keyword search, so callers can search within a cached page or request specific slices by index rather than re-fetching the whole document.

**Content fencing**: Tool output contains content fetched from the open web, which could include prompt injection attempts. Untrusted content is wrapped in visible fence markers (`┌─ untrusted content` / `└─ untrusted content`) with every line prefixed by `│`. This per-line provenance marking survives truncation and context compression. See `docs/frontmatter-standard.md` for the full spec.

**Frontmatter**: Tool responses begin with a YAML `---` block containing structured metadata — source URL, API origin, pagination state, and actionable hints for the calling agent. Frontmatter lives *outside* the content fence (it's trusted, server-generated metadata, never external data). `hint` suggests a same-tool follow-up, `see_also` points to a different tool, `note` is explanatory. `_build_frontmatter()` is the sole producer of `---` blocks.

**Research shelf**: An in-memory citation tracker that accumulates papers passively as the agent inspects them through arXiv, Semantic Scholar, DOI, or GitHub tools. GitHub repos with a `CITATION.cff` are tracked using the DOI from the preferred-citation block; repos without a CFF or DOI use a synthetic `github:owner/repo` key. Keyed by DOI with cross-DOI deduplication (preprint vs. journal versions). Supports scoring, notes, and export to BibTeX/RIS/JSON. Session-scoped — it resets when the MCP server restarts.

### Other Patterns

**2Q page cache**: `_PageCache` uses a scan-resistant two-queue eviction policy (probation FIFO + protected LRU, default 8 entries). New URLs land in probation; a second access (search, section, slices) promotes them to protected. Eviction prefers probation, so one-hit pages are evicted cheaply while drilled-into pages persist. Group-aware eviction removes all entries sharing a group key (e.g. gist files) when any member is the eviction victim. `_WikiCache` uses a simpler multi-entry LRU (default 5 entries).

**Profiles**: The server registers its tools under different naming conventions depending on the `--profile` flag. `code` uses PascalCase (`WebFetchDirect`), `desktop` uses snake_case (`web_fetch_direct`). Tool descriptions also adapt — they reference sibling tools by their profile-appropriate names.

### Environment Variables

| Variable | Purpose |
|---|---|
| `KAGI_API_KEY` | Kagi API key (fallback: `~/.config/parkour/kagi_api_key`) |
| `S2_API_KEY` | Semantic Scholar API key (fallback: `~/.config/parkour/s2_api_key`) |
| `MCP_CONTACT_EMAIL` | Enables CrossRef "polite pool" (10 req/s vs 5 req/s) |
| `GITHUB_TOKEN` | GitHub personal access token (fallback: `~/.config/parkour/github_token`). 5000 req/hr vs 60/hr unauthenticated |
| `S2_ACCEPT_TOS` | Set to `1` to enable Semantic Scholar integration (also: `~/.config/parkour/s2_accept_tos` file) |
| `PLAYWRIGHT_BROWSER` | Override browser for JS rendering |
| `MCP_ALLOW_PRIVATE_IPS` | Set to `1` to allow fetching from private/loopback/link-local IPs (default: blocked) |

## Testing

- Tests use `respx` for HTTP mocking and `pytest-asyncio` (strict mode) for async support.
- Fixtures in `conftest.py` provide sample responses and disable rate limiters.
- `test_live.py` contains integration tests deselected by default; run with `-m live`.
- Each test module maps to its source module (e.g., `test_arxiv.py` → `arxiv.py`).
- `scripts/regenerate_readme_examples.py` regenerates README example outputs. Most examples hit live endpoints; Reddit examples use `respx`-mocked fixtures for deterministic, offline output. Run after changing tool output format to keep examples current.

## Release process

Releases use **git-cliff** for CHANGELOG assembly and **commitizen** for version bumping from Conventional Commits. Local flow is driven by the `/release` slash command in `.claude/commands/release.md`; CI (`.github/workflows/release.yml`) handles build + publish on tag push.

Install git-cliff locally via `brew install git-cliff`. CI installs it via `taiki-e/install-action@git-cliff` (it's a Rust binary, not pip-installable).

### Why: commit trailers

Every `feat:`, `fix:`, `refactor:`, and `perf:` commit MUST include a `Why:` trailer stating user-visible impact in a single flowing sentence. **The trailer is the source of the corresponding CHANGELOG.md bullet.** git-cliff extracts `Why:` trailers as the user-facing prose for each entry. Commits without a `Why:` trailer fall back to the bare subject, which produces weaker release notes. Example:

```
fix(pipeline): surface tantivy parse warnings in search frontmatter

Tantivy emits structured warnings for malformed query syntax but the
pipeline was discarding them, leaving callers with zero-result searches
and no hint why.

Why: queries with unsupported operators now report the parse error in the response frontmatter instead of returning empty.
```

Write `Why:` as a single logical line (wrap in your editor, but no hard newlines in the value). git-cliff preserves multi-line trailers verbatim and the Tera template flattens them, but single-line is the path of least resistance.

`chore:`, `docs:`, `test:`, `style:`, `build:`, `ci:`, `revert:`, `release:` do not need `Why:`. `docs:` and `test:` still appear in the changelog (under Documentation / Miscellaneous) using their commit subject as the bullet text.

### Commit type to CHANGELOG section mapping

| Commit prefix | Section |
|---|---|
| `feat:` | Added |
| `refactor:` / `perf:` | Changed |
| `fix:` | Fixed |
| any type with `(security)` scope | Security |
| `docs:` | Documentation |
| `test:` | Miscellaneous |
| `chore:`, `build:`, `ci:`, `style:`, `release:` | (skipped) |

**Security scope convention**: Conventional Commits has no `security` type, so security-relevant changes piggyback on existing types via a `(security)` scope. Examples: `test(security): enforce SSRF precedence`, `fix(security): harden content fence`, `chore(security): update supply-chain allowlist`. Any commit with `(security)` in its scope routes to the Security section regardless of type.

### Version file discipline

`pyproject.toml:project.version` is the single source of truth (PEP 440). `scripts/sync_versions.py` mirrors it to:

- `manifest.json:version` translated to strict SemVer 2.0 (Claude Desktop rejects PEP 440 pre-release forms). `1.2.0rc1` becomes `1.2.0-rc.1`.
- `server.json:version` verbatim (MCP Registry accepts PEP 440).

**Do not hand-edit manifest.json or server.json version fields.** The sync script is the single writer. `just tag` runs `sync_versions.py --check` as a pre-push gate and the CI workflow re-runs it before doing anything else.

### Pre-releases

Public RCs are supported end-to-end. commitizen's `version_scheme = "pep440"` emits forms like `1.2.0rc1` that `uv build` and PyPI accept, and `sync_versions.py` translates those to strict SemVer (`1.2.0-rc.1`) in `manifest.json` for Claude Desktop. To cut an RC, the `/release` slash command accepts an explicit opt-in and passes `--prerelease rc` to `cz bump`.

## Conventions

- Parameter conflicts (e.g., `search` + `section`) resolve by picking the strongest signal and emitting a warning, never an error.
- Rate limiters (`common.py`) use `asyncio.Lock` to serialize concurrent API calls; the second caller sleeps only for the remaining interval.
- arXiv `/html/` URLs are intentionally NOT fast-pathed — they contain full rendered text worth slicing, unlike `/abs/` which is just metadata.
- Reddit fast path uses browser UA (`_FETCH_HEADERS`), not API UA — the `.json` endpoint is a page variant, not a formal API, and Reddit blocks bot UAs on unauthenticated requests. This is intentionally NOT the official Reddit API; it requires no OAuth, no API key, and no approval process.
- Discourse fast path uses post-fetch header detection (`x-discourse-route`), not URL pattern matching. This is the only fast path that operates after the initial HTTP fetch rather than before it. Per-host rate limiting via `_discourse_limiters` dict (lazy-initialized, 1s default).

## Technical Debt

See @./claude/TECH_DEBT.md for acknowledged warnings and deferred fixes. When opting not to fix a warning, document it there with the location, issue, and rationale.
