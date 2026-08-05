# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project

**parkour-mcp** — an MCP server providing a content exploration and research synthesis pipeline. Uses clean first-party APIs to surface and explore web content without summarization. Integrates Kagi, Semantic Scholar, arXiv, deps.dev, IETF, GitHub, MediaWiki, Reddit, Discourse, YouTube, and DOI resolution APIs into a unified tool suite for Claude Code and Claude Desktop.

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

<!-- [[[cog
import sys; sys.path.insert(0, "scripts")
from cog_helpers import tool_count
cog.outl(f"- **`__init__.py`** — MCP server entry point. Registers {tool_count(with_optional=True)}, with profile-specific names (PascalCase for `code`, snake_case for `desktop`). Description templates have placeholders replaced at registration time.")
]]] -->
- **`__init__.py`** — MCP server entry point. Registers 13 always-on tools, plus 1 optional (SemanticScholar, gated by S2_ACCEPT_TOS), with profile-specific names (PascalCase for `code`, snake_case for `desktop`). Description templates have placeholders replaced at registration time.
<!-- [[[end]]] -->
- **`detection.py`** — Pure, stdlib-only URL/identifier classification: the `_detect_*_url` predicates, regexes, ID extractors, and `RedditPageType` for arXiv, DOI, Semantic Scholar, IETF, and Reddit. Imported at module top by `fetch_direct.py`, `_pipeline.py`, and `kagi.py` (and by the source modules for their own internal use). Kept dependency-free so the fast-path dispatchers and sibling tools can classify a URL without loading a source module's transport stack (httpx, curl_cffi). Also hosts `HFUrlMatch` / `_detect_hf_url` for HuggingFace: gated, private, and nonexistent are all *response* properties on the Hub (it returns an identical 401 for the three), so nothing about classifying a Hub URL can consult a token and detection stays pure. GitHub is the exception that proves the rule — its detection stays in `github.py` because `_detect_github_url` consults `_get_github_token()` to auth-gate discussion URLs, so it is not stateless. MediaWiki and Discourse detect at fetch time, not from the URL.
- **`_pipeline.py`** — Shared processing layer. Owns the fast-path dispatch chain, multi-entry caching (`_WikiCache` LRU, `_PageCache` 2Q), slicing, BM25 search, and section filtering. URL detection itself lives in `detection.py`.
- **`markdown.py`** — HTML→markdown conversion. Two converters, deliberately: the generic path (`html_to_markdown`) runs the Rust-backed `htmd`, ~12–47× faster than the markdownify implementation it replaced in `da4f54a` while recovering +27.6% content on table-heavy pages; the custom `TextOnlyConverter` (markdownify + BS4) is retained solely for `mediawiki.py`, which applies BS4 transforms (navbox pruning, math extraction, footnote rewriting) before converting and so cannot hand raw HTML to htmd. Section extraction with fuzzy slug matching, preceded by `_promote_list_headings`: accordion and disclosure widgets nest each section's `<h2>` in an `<li>`, so the heading is lifted to line start and the item body dedented, or the line-start anchor misses every one of them. An explicit list marker is required, which is what keeps `# comment` lines in unfenced indented code blocks from being read as headings. Content fencing. Semantic truncation for markdown, hard truncation for structured formats. Also hosts `_plaintext_presplit`, the generic line-oriented cache presplitter with the issue-#6 circuit breaker: it has no source-specific behavior and more than one tool needs it (`github.py#_blob_presplit` uses it as the fallback under tree-sitter, `huggingface.py` uses it directly since none of the formats that tool reads have a grammar registered), so it lives at the common ancestor rather than in either tool.
- **`shelf.py`** — Research shelf implementation. All public methods guarded by `asyncio.Lock`.

API integration modules, each self-contained:
- **`kagi.py`** — Search via the v1 API over httpx with Bearer auth, supporting `workflow` (search/images/videos/news/podcasts), `lens_id`, `page`, and flat `region`/`after`/`before` filters that assemble into v1's `filters` object. The `kagi_summarize` tool is unregistered pending Kagi shipping `/summarize` on v1; the backing `summarize()` function, the `kagiapi` dep, and the v0 balance-lockout helpers (`get_client`, `_extract_balance`, `_check_balance`, `_summarize_locked`, `_handle_v0_error`) stay dormant in this file for re-registration. v1 spec lives under `https://kagi.com/api/docs/`: overview `openapi.md`, search `openapi/search.md` and operation `openapi/search/search.md`, extract `openapi/extract.md` and operation `openapi/extract/extractcontent.md`, full bundles `_bundle/openapi.yaml?download` and `_bundle/openapi.json?download`. Fetch via `curl`; `web_fetch_direct` currently rejects `text/markdown` and `application/yaml` content types.
- **`fetch_direct.py`** — The `web_fetch_direct` tool (WebFetchIncisive). Static HTTP fetching with content-type detection. Routes URLs through the fast-path chain; falls back to static HTTP, or to the headless-browser renderer when `requires_js=True` / `actions` is set.
- **`fetch_js.py`** — `_render_js`, the headless-browser render path for `web_fetch_direct`'s `requires_js` mode. Not a registered tool. Playwright automation with live-app detection (Gradio, Streamlit) and ReAct-style `actions` chains; `web_fetch_direct` owns the fast paths and SSRF check before dispatching here.
- **`arxiv.py`** — arXiv Atom API. Field-prefix query syntax. 3s rate limit.
- **`semantic_scholar.py`** — S2 API with optimized field sets per query type. 1s rate limit (higher with `S2_API_KEY`).
- **`doi.py`** — DOI resolution via content negotiation. Registration agency detection. DataCite enrichment (ORCID, affiliations, licenses).
- **`mediawiki.py`** — Wikipedia/MediaWiki API. Probes for api.php endpoint. Full-page fetch with downstream section filtering.
- **`reddit.py`** — Reddit fast path via the `oauth.reddit.com` API with a *userless* OAuth token. Reddit retired the unauthenticated `.json` endpoints on 2026-05-29; access now requires a bearer token. Mirrors redlib (`redlib-org/redlib`, `src/oauth.rs` + `src/client.rs`): two-tier token acquisition (`_mint_mobile_token` against the Android-app loid grant, `_mint_web_token` generic-web fallback), background refresh daemon (`_token_daemon`, refreshes ~120s before expiry), reactive refresh on 401 (`_force_refresh_token`), and in-band JSON error mapping (`_check_reddit_json_error`: suspended / quarantined / gated / private / banned). Client IDs are Reddit's own first-party app credentials (`_ANDROID_OAUTH_CLIENT_ID`, `_WEB_OAUTH_CLIENT_ID`), not a third party's. The returned JSON shape is identical to the old `.json` endpoint, so the formatters (`_format_comment_thread`, `_format_listing`, `_build_comment_section_tree`, `_split_by_comments`) are unchanged. URL rewriting, comment tree parsing, section-based comment navigation, and search-result listings (global `/search/` and subreddit-scoped `/r/SUB/search/`, each hit carrying its subreddit and a fetchable permalink). `detection.py#_detect_reddit_url` preserves search query params (`q` and friends) and strips a caller-appended `.json` so the fetcher never doubles it into a 400. 2s rate limit; token cache is session-scoped.
<!-- [[[cog
import sys; sys.path.insert(0, "scripts")
from cog_helpers import action_list
cog.outl(f"- **`github.py`** — GitHub REST API integration. {action_list('parkour_mcp.github._VALID_ACTIONS')}. Three-tier auth (env → config file → unauthenticated). Per-resource rate limit tracking. URL detection for fast-path chain covering blob (with line anchors), tree, issue, PR, wiki, commit, compare, releases, org/user profiles, gist, and `raw.githubusercontent.com`. Source code sectionization via tree-sitter CodeSplitter. CITATION.cff parsing for research shelf integration. OpenSSF Scorecard enrichment on `repo` and `file` actions via `scorecard.py`.")
]]] -->
- **`github.py`** — GitHub REST API integration. 9 actions: search_issues, search_code, search_repos, repo, tree, issue, pull_request, file, issue_templates. Three-tier auth (env → config file → unauthenticated). Per-resource rate limit tracking. URL detection for fast-path chain covering blob (with line anchors), tree, issue, PR, wiki, commit, compare, releases, org/user profiles, gist, and `raw.githubusercontent.com`. Source code sectionization via tree-sitter CodeSplitter. CITATION.cff parsing for research shelf integration. OpenSSF Scorecard enrichment on `repo` and `file` actions via `scorecard.py`.
<!-- [[[end]]] -->
<!-- [[[cog
import sys; sys.path.insert(0, "scripts")
from cog_helpers import action_list
cog.outl(f"- **`huggingface.py`** — HuggingFace Hub integration. {action_list('parkour_mcp.huggingface._VALID_ACTIONS')}. One flagship call (`/api/models/<repo>?blobs=true&expand=…`, ~55 ms) frontloads params, dtype fingerprint, quant summary, gated/private state, lineage, per-file sizes and LFS checksums. Optional bearer auth (`HF_TOKEN`, fallback `~/.config/parkour/hf_token`). Rate-limit tracking parses RFC 9651 structured fields (`ratelimit: \"api\";r=495;t=140`), *not* GitHub's `X-RateLimit-*`. Weight files are described, never downloaded: `_describe_weight_file` returns size, `lfs.sha256`, and the byte-range recipe for reading a safetensors header. The quant analysis (`analyze_quant`) is the substance — it derives effective bits-per-weight and **suppresses it rather than guessing** whenever the Hub's own numbers cannot support it. Four preconditions, each with a verified public repo that fails only that gate: packed storage counts reported as parameter counts (the U32 guard), repos shipping more than one checkpoint set (the `(directory, group)` partition with its `\"singles\"` bucket), diffusers pipelines, and repos with no safetensors block. Presence-not-truthiness on `quantization_config`, since a present-but-emptied dict is its own signal. `quant_audit=True` adds one call to read the base's native format. Design spec and the evidence behind every gate: `docs/huggingface-tool.md`.")
]]] -->
- **`huggingface.py`** — HuggingFace Hub integration. 5 actions: model, file, tree, search, org. One flagship call (`/api/models/<repo>?blobs=true&expand=…`, ~55 ms) frontloads params, dtype fingerprint, quant summary, gated/private state, lineage, per-file sizes and LFS checksums. Optional bearer auth (`HF_TOKEN`, fallback `~/.config/parkour/hf_token`). Rate-limit tracking parses RFC 9651 structured fields (`ratelimit: "api";r=495;t=140`), *not* GitHub's `X-RateLimit-*`. Weight files are described, never downloaded: `_describe_weight_file` returns size, `lfs.sha256`, and the byte-range recipe for reading a safetensors header. The quant analysis (`analyze_quant`) is the substance — it derives effective bits-per-weight and **suppresses it rather than guessing** whenever the Hub's own numbers cannot support it. Four preconditions, each with a verified public repo that fails only that gate: packed storage counts reported as parameter counts (the U32 guard), repos shipping more than one checkpoint set (the `(directory, group)` partition with its `"singles"` bucket), diffusers pipelines, and repos with no safetensors block. Presence-not-truthiness on `quantization_config`, since a present-but-emptied dict is its own signal. `quant_audit=True` adds one call to read the base's native format. Design spec and the evidence behind every gate: `docs/huggingface-tool.md`.
<!-- [[[end]]] -->
- **`ietf.py`** — IETF RFC and Internet-Draft integration. 4 tool actions (rfc, search, draft, subseries). RFC Editor per-document JSON for metadata and relationship chains (obsoletes/updates). IETF Datatracker REST API for search with status/WG filtering. BibXML service for subseries (STD/BCP/FYI) resolution. Native DOI tracking (`10.17487/RFC{N}`). 1s Datatracker rate limit.
<!-- [[[cog
import sys; sys.path.insert(0, "scripts")
from cog_helpers import ecosystem_list
cog.outl(f"- **`packages.py`** — deps.dev (Google Open Source Insights) integration. 5 tool actions (package, version, dependencies, project, advisory). Covers {ecosystem_list()}. Version history, license detection, security advisories (GHSA/CVE with CVSS), resolved dependency graphs with native constraints, OpenSSF Scorecard, OSS-Fuzz coverage, and SLSA provenance. Shares the deps.dev HTTP client (`_depsdev_get`) and 1s limiter with `scorecard.py` via `common.py`. No auth required. Body content fenced (contributor-supplied fields are injection vectors).")
]]] -->
- **`packages.py`** — deps.dev (Google Open Source Insights) integration. 5 tool actions (package, version, dependencies, project, advisory). Covers 7 ecosystems: pypi, npm, cargo, go, maven, nuget, rubygems. Version history, license detection, security advisories (GHSA/CVE with CVSS), resolved dependency graphs with native constraints, OpenSSF Scorecard, OSS-Fuzz coverage, and SLSA provenance. Shares the deps.dev HTTP client (`_depsdev_get`) and 1s limiter with `scorecard.py` via `common.py`. No auth required. Body content fenced (contributor-supplied fields are injection vectors).
<!-- [[[end]]] -->
- **`discourse.py`** — Discourse forum integration. 3 tool actions (topic, search, latest). Detects Discourse instances via `x-discourse-route` response header (post-fetch, not URL-based). Two-request topic assembly: first page inline + batch remaining via `post_ids[]`. Raw author markdown via `include_raw=true`. Per-host rate limiting via lazy-initialized dict. Quote BBCode → blockquote conversion, `upload://` ref cleanup. Post-aware BM25 splitting and reply-threaded section trees. No auth required.
- **`youtube.py`** — YouTube integration via yt-dlp (metadata + chapters + comments) and youtube-transcript-api (captions). Two tool actions on the main tool: video (metadata + description) and transcript (caption text with timing). The transcript action supports BM25 search, half-open time-range filtering, chapter-scoped search, explicit window retrieval, and four output shapes (compact / absolute / none / structured). Quality-aware coalescer: punctuated captions get sentence-aware window cuts, auto-captions get pause-aware time-window cuts, both bounded to a [25s, 35s] tolerance band (WhisperX Cut & Merge). Outlier pause markers via rolling-median detection over a 10-segment window. Per-video Tantivy index with `body`, `chapter`, `idx`, `start_seconds`, `end_seconds` schema; built lazily on first `search=`. Chapters fetch concurrently with the transcript via yt-dlp; render as frontmatter `chapters:` list and as `## [MM:SS] Title` headings in compact mode. `_TranscriptCache` is a 2Q sibling of `_PageCache` with cross-cache group eviction via `_pipeline.py#_evict_group` so video-metadata and transcript entries sharing a `yt:{video_id}` group key drop together. URL detection for `watch`, `youtu.be`, `shorts`, `clip`, `embed`, `@handle`, `/channel/UC...`, `/c/`, `/user/`, `/playlist`. `music.youtube.com` deliberately excluded (deferred sibling tool). yt-dlp transcript fallback recovers from `RequestBlocked` / `IpBlocked` (likely) and `PoTokenRequired` (only with a yt-dlp PoToken provider plugin); fallback emits a frontmatter `note:` describing both the original failure and the recovery. Single shared `YoutubeDL` instance for video mode. Sync extraction wrapped via `asyncio.to_thread`. Bot-detection / private / age-restricted / geo-restricted errors mapped to user-facing strings. No auth required. See `docs/youtube-transcript-search.md` for the full schema, action-arg matrix, and frontmatter shapes. Comment fetching lives in a sibling `YoutubeComments` tool registered separately.
- **`scorecard.py`** — OpenSSF Scorecard client. Reads the `scorecard.overallScore` and `scorecard.date` fields from deps.dev's `/v3/projects/github.com/{owner}/{repo}` endpoint via the shared `_depsdev_get` helper in `common.py`. Returns `(score, iso_date)` for frontmatter enrichment on `github:repo`, `github:file`, and `packages:project` via the shared `format_score()` helper that emits `"N/10 (@ YYYY-MM-DD)"`. Uses deps.dev (not `api.securityscorecards.dev`) because the webapp is a CI-upload registry with sparse, frequently-stale coverage: deps.dev ingests OpenSSF's weekly server-side cron scan of the top ~1M projects and has broader, fresher data. Session-lived per-repo cache; silent degrade on 404 / missing scorecard / network error (missing key omitted, not nulled).
- **`common.py`** — Shared constants and helpers: dual User-Agent strategy (browser UA for HTML, API UA for structured endpoints), `RateLimiter` class, shared deps.dev HTTP client (`_depsdev_get`, `_depsdev_limiter`) used by both `packages.py` and `scorecard.py`, `s2_enabled()` gate, `_LANGUAGE_MAP` for file extension → syntax highlight language.

### Key Concepts

**Fast paths**: When a URL belongs to a known API-backed source (Wikipedia, arXiv, Semantic Scholar, DOI, Reddit, GitHub, HuggingFace, Discourse), the server can skip the generic HTTP-fetch-and-convert path and instead call the source's structured API directly. This is faster, yields richer metadata, and avoids scraping. The pre-fetch detection chain in `fetch_direct.py` tests URLs in priority order: arXiv → Semantic Scholar → IETF → DOI → Reddit → GitHub → HuggingFace → MediaWiki → generic HTTP fallback. The detector predicates themselves live in `detection.py` (pure, stdlib-only); `fetch_direct.py` and `_pipeline.py` import them at module top and only the heavyweight `_fetch_*` handlers stay lazily imported. Discourse uses post-fetch detection via the `x-discourse-route` response header — after the initial HTTP fetch, the header is checked and the URL is re-fetched via the JSON API if detected.

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
| `HF_TOKEN` | HuggingFace token (fallback: `~/.config/parkour/hf_token`). Optional — unlocks gated/private repos and raises the rate limit |
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

**Posture depends on which section the bullet lands in.** The trailer is read as a bullet under a heading, alongside its siblings, and the heading does not supply a missing subject:

- `feat:` → **Added**. Lead with what now exists, and name it. Someone scanning this section wants to learn the capability; they have no prior expectation to correct.
- `fix:` / `refactor:` / `perf:` → **Fixed** / **Changed**. Leading with the problem reads naturally, because the change only means something relative to the behavior it replaces.

The example above is a `fix:`, so it opens on the defect. A `feat:` trailer written in that shape produces an Added bullet that opens on the old problem and can finish without ever naming the thing added. That happened in v2.1.0 and was rewritten by hand at review; it is cheaper to get right at commit time. The `feat:` shape:

```
feat(huggingface): add Hub model and quant tool

Adds a HuggingFace tool with five actions, fast-path interception for
huggingface.co URLs, and a quantization analysis that refuses to guess.

Why: a new HuggingFace tool inspects Hub models in a single call: architecture, parameter count, checkpoint size, gated state, base-model lineage, and per-file checksums, plus the effective bits-per-weight of a quantized release.
```

`chore:`, `docs:`, `test:`, `style:`, `build:`, `ci:`, `revert:`, `release:` take **no** `Why:`, which is stronger than "do not need one". Those types either skip the changelog entirely or render from their subject, so a `Why:` written on one is silently discarded: the prose goes nowhere and nobody finds out. `docs:` and `test:` do appear (under Documentation / Miscellaneous) but from the commit *subject*, so that is where their user-facing wording belongs.

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

- `manifest.json:version` translated to strict SemVer 2.0 (Claude Desktop rejects PEP 440 pre-release forms). `2.0.0rc0` becomes `2.0.0-rc.0`.
- `server.json:version` verbatim (MCP Registry accepts PEP 440).

**Do not hand-edit manifest.json or server.json version fields.** The sync script is the single writer. `just tag` runs `sync_versions.py --check` as a pre-tag gate and the CI workflow re-runs it before doing anything else.

### Pre-releases

Public RCs are supported end-to-end. commitizen's `version_scheme = "pep440"` emits a zero-based RC counter, so the first RC of `2.0.0` is `2.0.0rc0` — a form `uv build` and PyPI accept, and `sync_versions.py` translates it to strict SemVer (`2.0.0-rc.0`) in `manifest.json` for Claude Desktop. To cut an RC, the `/release` slash command accepts an explicit opt-in and passes `--prerelease rc` to `cz bump`. CHANGELOG.md tracks final releases only: an RC gets no section of its own, and git-cliff folds an RC's commits into the next final. Both the local CHANGELOG prepend and the CI release-note step anchor their commit range to the last final `vX.Y.Z` tag, so a final cut after one or more intervening RC tags still spans the whole range since the previous final.

## Conventions

- Parameter conflicts (e.g., `search` + `section`) resolve by picking the strongest signal and emitting a warning, never an error.
- Rate limiters (`common.py`) use `asyncio.Lock` to serialize concurrent API calls; the second caller sleeps only for the remaining interval.
- arXiv `/html/` URLs are intentionally NOT fast-pathed — they contain full rendered text worth slicing, unlike `/abs/` which is just metadata.
- Reddit fast path authenticates with a *userless* OAuth token against `oauth.reddit.com`, minted via Reddit's own logged-out mobile-app grant (mirroring redlib). It uses `curl_cffi` with a Safari TLS profile (`_IMPERSONATE_PROFILE`) to clear Reddit's JA3 filter, plus spoofed Android device headers on the token mint. This is the userless/anonymous OAuth lane (no user account, no API key, no app registration), NOT a registered-app integration. The unauthenticated `.json` endpoints it formerly used were retired by Reddit on 2026-05-29.
- Discourse fast path uses post-fetch header detection (`x-discourse-route`), not URL pattern matching. This is the only fast path that operates after the initial HTTP fetch rather than before it. Per-host rate limiting via `_discourse_limiters` dict (lazy-initialized, 1s default).

## Code references

Cite code by `path#Symbol` (Drift-style anchor), not `path:line`. Symbol anchors survive renames, line shifts, and refactors; line numbers drift the moment the file changes and end up pointing at the wrong code in the next session. Examples:

- `parkour_mcp/markdown.py#FMEntries` (class)
- `parkour_mcp/markdown.py#_build_frontmatter` (function)
- `parkour_mcp/_pipeline.py#_PageCache` (class with method-level scope written as `#_PageCache.stats`)
- `docs/frontmatter-standard.md#multi-contributor-keys-protected` (markdown heading slug)

`path:line` is correct only when the target has no symbol name (a comment, blank line, log line) or when emitting a permalink to a pinned SHA where the line is frozen by the URL. The convention applies independently of whether Drift is gating the specific reference.

## Technical Debt

See @./.claude/TECH_DEBT.md for acknowledged warnings and deferred fixes. When opting not to fix a warning, document it there with the location, issue, and rationale.

Source-shape changes that would let Cog and Drift cover more of the doc surface are tracked separately in @./.claude/docs-drift-enhancements.md.
