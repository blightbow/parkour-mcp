# Technical Debt

Acknowledged warnings and deferred fixes. Each entry includes the source, the issue, and why it was deferred.

## Reddit OAuth — deliberate deviations from redlib

The Reddit fast path was rebuilt on `oauth.reddit.com` userless tokens after Reddit retired the unauthenticated `.json` endpoints on 2026-05-29. The implementation mirrors redlib (`redlib-org/redlib`, `src/oauth.rs` + `src/client.rs`), which we treat as the domain expert. A few intentional simplifications diverge from redlib; they are choices, not bugs.

### Collapsed token-acquisition retry loop

- **Location**: `parkour_mcp/reddit.py#_authenticate`.
- **Deviation**: redlib's `Oauth::new` retries the mobile grant up to 5 times (5s apart), falls back to the web grant, and `process::exit`s after 10 total failures. We attempt each tier once and return a graceful error string on total failure.
- **Why**: redlib is a long-running web server where a boot-time retry loop is appropriate. parkour mints inside an interactive tool call, where a 25s+ stall is worse UX than a fast, honest failure (the caller surfaces the error and can retry). The background daemon and the reactive 401 refresh still recover from transient failures after the first success.

### No proactive `x-ratelimit-remaining` tracking

- **Location**: `parkour_mcp/reddit.py#_reddit_api_get` (and the absence of redlib client.rs `OAUTH_RATELIMIT_REMAINING` accounting).
- **Deviation**: redlib reads the `x-ratelimit-remaining` header on every response and spawns a token rollover when it drops below 10, to dodge per-IP limits across many concurrent users. We do not track it.
- **Why**: parkour is a single-user sidecar gated by a 2s limiter (~30 req/min), comfortably under Reddit's userless ceiling (~100 req/min). The proactive accounting would add module state and complexity for a limit a single user does not approach. The daemon (proactive, time-based) plus 401-reactive refresh cover token freshness. Revisit if parkour ever fans out concurrent Reddit fetches.

### redd.it short-link resolution is best-effort

- **Location**: `parkour_mcp/reddit.py#_resolve_redd_it`.
- **Deviation**: redlib resolves share/short links through a dedicated `canonical_path` HEAD-walk against multiple bases with full retry. We do a single authenticated HEAD on the `redd.it` URL (token attached when available, unauthenticated fallback otherwise) and read the redirect target.
- **Why**: short links are a small fraction of Reddit traffic and the single HEAD currently resolves them. If Reddit starts gating `redd.it` redirects the way it gated `.json`, port redlib's `canonical_path` HEAD-walk.

### Token cache ignores the requested page's quarantine state

- **Location**: `parkour_mcp/reddit.py#_fetch_reddit_json` (no quarantine opt-in cookie).
- **Deviation**: redlib sends a `_options` cookie opting into quarantined/gated content. We do not, so quarantined subreddits map to a `_check_reddit_json_error` error string rather than rendering.
- **Why**: rendering quarantined content silently is a surprising default for a research sidecar; surfacing the quarantine status as an explicit error is the more PoLA-aligned behavior. Add the opt-in cookie behind a flag if a real use case needs quarantined threads.
- **NSFW is treated oppositely, on purpose**: quarantine is Reddit's explicit *warning* state for rule-breaking communities, so gating it behind an error is the unsurprising default. Ordinary 18+ (NSFW) content is not that — it is everywhere on Reddit, and the rest of the toolkit (Kagi, the generic fetch) never filters adult content. So NSFW is *included* by default; search defaults to `include_over_18=1` (`detection.py#_detect_reddit_url`) and direct subreddit/thread fetches already surface it untouched. The astonishing thing would be Reddit search silently returning a SFW subset when a Kagi search beside it does not. A caller can still pass `include_over_18=0` for SFW search.

## Static-analysis findings (opted not to fix)

Which checker is authoritative, so this section is not read as covering more
than it does:

- **ruff** gates in CI. The tag-push job runs `uv run pytest`, and `pytest-ruff`
  lints every file in `testpaths` as part of that run, so a ruff finding fails
  the build. Config lives in `[tool.ruff.lint]`.
- **ty** gates too, via `pytest-ty` in the same `addopts`, so a type error
  fails the build exactly like a lint error. Suppressions use
  `# ty: ignore[rule]` and must carry a reason. The editor/agent LSP is a
  separate, faster, and *less reliable* signal — it is edit-scoped and can go
  stale or misreport under concurrent worktree activity — so the suite run,
  not the LSP, is authoritative when they disagree.
- **RUF100** (unused-noqa) is selected, which makes a `noqa` self-expiring: one
  that stops suppressing anything becomes an error. Note its autofix deletes
  the *whole* trailing comment, prose included, so re-home any explanation as
  a plain comment rather than losing it.
- **Both suppression spellings count.** Since 0.16 ruff accepts
  `# ruff: ignore[RULE]` as well as `# noqa: RULE`, inline or on the preceding
  line, and 0.15 added block form (`# ruff: disable[RULE]` /
  `# ruff: enable[RULE]`). RUF100 polices all of them, but a *grep*-based audit
  must match every spelling or it will under-report.
- **A directive count is not a health check.** The isort autofix rewraps an
  import past the line limit into parenthesised form and carries a trailing
  `noqa` onto the imported-name line, where the rule does not report. The
  comment survives `grep` and silently stops suppressing. Measured on this repo:
  0 PLC0415 findings before a naive autofix, 8 after, with the directive count
  unchanged at 6 throughout. Gate on the finding count, never the comment count.
  Keep suppressed imports short enough not to wrap; see the four sites in
  `doi.py` and `mediawiki.py`, whose reasons live in a block comment above.
- **drift** and **cog** (`just docs-drift`, which also runs
  `check_manifest_tools.py`) gate tag creation from the `tag` recipe in
  the justfile, and again from `scripts/git-hooks/pre-push` for anyone
  who tags by hand instead. Both are pre-tag on purpose: CI only ever
  sees a tag that already exists, and a stale doc is not worth deleting
  and recutting a tag over. Neither runs in CI, so a fresh clone that
  tags and pushes without `just tag` is ungated — the same hole the
  hook paragraph below describes. A drift failure is resolved by
  reviewing the doc against the code and restamping with
  `drift link <doc> --doc-is-still-accurate`, never by restamping
  blind; `drift link` refuses a stale anchor without that flag
  precisely so the review cannot be skipped.
- **vulture** (`just lint-deep`) gates version-tag pushes via
  `scripts/git-hooks/pre-push`, which is the only place it runs — CI's
  tag-push job is `uv run pytest` plus the semgrep install that the test
  step now requires, and nothing more. Findings are fixed at the
  source; `.vulture_whitelist.py` takes false positives only, each with a
  comment naming what vulture cannot see.
- **semgrep** gates too, via `tests/test_semgrep_rules.py` in the same
  `pytest` run, against the project ruleset in `.semgrep/` (FMEntries
  construction, SSRF precedence, content fencing). Suppressions are
  `# nosemgrep: <rule-id>` with a reason. Unlike the others it is **not**
  in the `dev` dependency group — the test shells out to the binary and
  never imports the package, and declaring it dragged semgrep's hard
  `mcp==1.23.3` pin into our resolve. Install it out-of-tree
  (`brew install semgrep`); a missing binary fails the suite rather than
  skipping it, and `PARKOUR_SKIP_SEMGREP=1` is the explicit opt-out. See
  `docs/developing.md#first-time-setup`.

Suppressions therefore use `# noqa: RULE` for ruff, `# ty: ignore[rule]`
for ty, and `# nosemgrep: <rule-id>` for semgrep, each with a reason. A
suppression in any other checker's dialect is inert — nothing here
consumes it.

**Lazy imports have a declarative home; prefer it to a `noqa`.** PLC0415's own
docs recognise exactly three grounds for a function-scope import: avoiding a
circular dependency, deferring a costly module load, or skipping a dependency
entirely in some runtime environment. The last two are expressed centrally in
`[tool.ruff.lint.flake8-tidy-imports] banned-module-level-imports`, which
PLC0415 defers to, so those sites need no per-site suppression at all — and
TID253 enforces the converse, rejecting a top-level import of a listed module.
That bidirectionality is why the list cannot rot the way a comment can.

Only the first ground needs a `noqa`, because a module that must be lazy *in
one file* is legitimately top-level elsewhere and the ban list cannot express
that. Such a comment must name the specific line that closes the loop and be
reproducible: hoist the import, and `import parkour_mcp` must raise. Anything
weaker is a claim nobody will re-check. This convention exists because 55
suppressions were once added in the same commit that adopted the rule, with
templated rationales; an audit found 51 of them false.

**The hook is opt-in.** It only runs for developers who have run
`just install-hooks` (it sets `core.hooksPath`), so a fresh clone pushing a
tag is ungated. Moving the vulture and ty scans into CI would close that,
at the cost of failing a tag push after the tag already exists.

### `youtube.py` — two dead `except ImportError` guards

- **Location**: `youtube.py#_map_transcript_error` and the transcript fallback branch in `youtube.py#_youtube_transcript` (the two remaining `# noqa: PLC0415` sites in that file).
- **Issue**: each is `try: from youtube_transcript_api import (...) / except ImportError: <degrade>`. The handler cannot fire: `youtube-transcript-api` is a required dependency, and since the PLC0415 sweep the package is imported at module top anyway, so a missing install fails at `import parkour_mcp` long before either branch runs. The imports stay function-scope only because they are the sole statement of the `try`, so removing the `noqa` means removing the guard.
- **Why deferred**: deleting a `try/except` changes behavior in the broken-install case (hard `ImportError` at startup instead of a readable message at call time). That is defensible — PLC0415's own docs cite catching invalid imports regardless of whether a function runs as a benefit — but it is an error-handling decision, not the mechanical import cleanup the sweep was scoped to, and no test exercises either branch.
- **How to evaluate**: decide whether a missing required dependency should fail loudly at startup. If yes, delete both guards, hoist the imports, and the last two `noqa: PLC0415` in the file disappear with them.

### `fetch_direct.py` — `_matched_meta` not accessed

- **Location**: `fetch_direct.py#_sections_response`
- **Issue**: `_matched_meta` is destructured from the return of `_filter_markdown_by_sections()` but never used. No checker currently reports it — ruff's unused-variable rules permit the underscore prefix, and ty treats it as an intentional discard — so this entry exists to keep the observation from being rediscovered as if it were new.
- **Why deferred**: The variable captures section match metadata (ancestry paths, fragment matches) that may be useful in frontmatter enrichment later. Removing it would discard structured data we'll likely want when section responses gain richer diagnostics. Low-risk dead code in a display-only path.

## Performance bottlenecks to investigate

### `html_to_markdown` on megapages — RESOLVED, do not re-optimize

This entry previously named `html_to_markdown` "the dominant generic-HTTP
latency" at ~6,940 ms (ECMA-262) and ~17,200 ms (WHATWG HTML spec), and
proposed swapping the BeautifulSoup-based `TextOnlyConverter` for a faster
parser. **Both the numbers and the remediation are obsolete.** They were
written on 2026-04-10 (`e325efe`) against markdownify-era baselines, and
the port landed the next day.

`da4f54a` (2026-04-11) replaced the converter with `htmd-py`, a binding
over the `htmd` Rust crate, and `8f41392` (2026-04-15) recaptured the
baselines. `4bd71ef` moved the pin from the `blightbow/htmd-py` fork to
upstream v0.1.2 once `lmmx/htmd#41` merged the text-only `Options` fields.
The entry was never updated to match, so it sat ~3 months describing a
pipeline that no longer existed.

Measured delta (from `8f41392`, `html_to_markdown_ms`):

| tier | markdownify | htmd | speedup |
|---|---:|---:|---:|
| small (PEP 8) | 88 ms | 7 ms | ~12× |
| medium (ECMA-262) | 6,936 ms | 202 ms | ~34× |
| pathological (WHATWG) | 17,200 ms | 362 ms | ~47× |

The port also *recovered* content rather than trading accuracy for speed —
the WHATWG fixture grew 6.34 MB → 8.08 MB (+27.6%) from htmd's layout-table
handling, which was the original motivation. Do not re-propose
`selectolax` / `lxml` for the generic path.

**But markdownify is still live on one path.** `markdown.py#TextOnlyConverter`
was deliberately retained, and `mediawiki.py:448` calls it via
`markdown.py#md` because the MediaWiki path applies BS4 transforms
(navbox pruning, math extraction, citation footnote rewriting) before
converting. So the old converter — and its cost curve — still backs every
Wikipedia fetch. That is **not** covered by the tables below: the
generic-HTTP tiers exercise htmd, and the only MediaWiki entry in
`fast_paths` is a 1.1 KB result. A large Wikipedia article is the one
un-benchmarked place the pre-port cost could still surface. Capturing a
megapage MediaWiki fixture is the cheap way to find out whether that
matters; nobody has.

### `MarkdownSplitter` is now the dominant generic-HTTP phase

- **Location**: `semantic-text-splitter` (`>=0.29.0`), driven from
  `parkour_mcp/_pipeline.py`; introduced in `db1519d`.
- **Measured cost** (`scripts/benchmark_baselines.json`, captured
  2026-08-05 on Darwin arm64 / Python 3.14.4):

  | tier | fetch | h2md | **split** | sections | ancestry | tantivy | total |
  |---|---:|---:|---:|---:|---:|---:|---:|
  | small | 97 | 4 | **1** | 1 | 0 | 4 | 106 ms |
  | medium | 613 | 266 | **2,196** | 41 | 98 | 14 | 3.2 s |
  | pathological | 612 | 562 | **5,059** | 112 | 122 | 27 | 6.5 s |

  `fetch` is network wall-clock against live endpoints and swings widely
  between captures; the phases after it are the ones worth reading.

  On the pathological tier the splitter is 78% of pipeline wall-clock, and
  the only phase above a second.

  Baselines and fixtures are captured in the **same run**. They were three
  days apart before (fixtures 2026-04-12, baselines 2026-04-15), so the
  recorded phase times described a different document than the one
  `test_perf.py` replays, and the gap read as an unexplained regression.
  `--update-baselines` and `--capture-fixtures` belong together.
- **Scope**: generic HTTP path only, as before — every fast path bypasses
  it. Fast-path end-to-end times are all well under 2.5 s (`fast_paths` in
  the same baseline file).
- **Why deferred**: 6.5 s end-to-end on the worst page on the open web is
  not a user-visible problem, and it is paid once per page per session
  behind `_PageCache`. There is no obvious cheaper substitute either: the
  splitter is what produces semantic slice boundaries and ancestry, which
  the BM25 index and every `slices=` / `section=` follow-up depend on.
  Chunking faster by splitting dumber would degrade retrieval, which is the
  product. Revisit only if a real report cites megapage latency.
- **Regression guard**: `tests/test_perf.py` covers **every** phase, not
  just conversion — `test_html_to_markdown`, `test_splitter`,
  `test_extract_sections`, `test_tantivy_index`, plus `test_full_pipeline`
  for the summed total (which catches a regression that hides by staying
  just inside per-phase tolerance). Default tolerance is 2× the captured
  baseline, overridable via `PERF_TOLERANCE`. `test_html_to_markdown_output_size`
  additionally pins byte-level output so a converter swap cannot quietly
  truncate — that guard exists because `html-to-markdown` 3.1.0 silently
  dropped 96% of the WHATWG fixture during the port
  (`kreuzberg-dev/html-to-markdown#275`).
- **Keeping this honest**: regenerate with
  `uv run python3 scripts/benchmark_pipeline.py --update-baselines --capture-fixtures`
  and update the tables above in the same commit. Pass both flags: baselines
  measured against one document and fixtures captured from another produce a
  gate that drifts on its own. The numbers here are prose and rot silently;
  the JSON is what `test_perf.py` asserts against, so a mismatch means this
  entry is wrong, not the suite.

## YouTube tool — deferred enhancements

### `music.youtube.com` sibling tool

- **Location**: `parkour_mcp/youtube.py#_YT_MUSIC_RE`, `_detect_youtube_url`, and the dispatcher's music-URL rejection branches.
- **Issue**: `music.youtube.com` URLs are recognized only to emit an explicit out-of-scope error. Music tracks have a different shape (album / artist / track / playlist semantics differ from regular video shape) that doesn't fit the existing `_video` / `_channel` / `_playlist` actions.
- **Why deferred**: Building a coherent music-track tool requires its own data model (track-level metadata, album grouping, artist disambiguation) that's larger than the scope of the regular YouTube tool. A sibling tool keeps the surface clean rather than bolting music-shaped responses onto the video tool. The current "explicit error with a note about the sibling tool" is honest about the gap.

### PoToken provider plugin slot

- **Location**: `parkour_mcp/youtube.py#_yt_dlp_transcript_fallback` and `_map_transcript_error` (PoTokenRequired branch).
- **Issue**: When YouTube enforces the `xpe` / `xpv` Botguard PoToken experiment on a caption URL, neither `youtube-transcript-api` (which has no token-generation path) nor the bare `yt-dlp` fallback can recover. The only working solution is a yt-dlp PoToken provider plugin (e.g. `bgutil-ytdlp-pot-provider`) that generates the token via Botguard JS.
- **Why deferred**: PoToken plugins require external dependencies (a Node-compatible JS runtime + the plugin package) that meaningfully change the install footprint. The error message points users at the plugin path; if it sees real user demand, a future commit can add a config flag to enable plugin auto-discovery.
- **Mitigation**: The error message names `bgutil-ytdlp-pot-provider` specifically so users can resolve the issue themselves without code changes here. yt-dlp's plugin loading happens automatically when the plugin is installed in the user's environment.

### Transcript cache key ignores language preference

- **Location**: `parkour_mcp/youtube.py#_TranscriptCache` (keys: canonical YouTube watch URL).
- **Issue**: Cache key is the URL only; the `languages=` preference list is not part of the key. The first language successfully fetched for a URL wins for the cache entry's lifetime. Subsequent calls with a different `languages=` list cache-hit the entry from the first call rather than fetching a different track.
- **Why deferred**: Cross-language workflows on the same video are rare in practice (most callers want the default language). Including languages in the key would multiply cache entries per video and complicate the group-eviction key shape. Acceptable for v1.
- **Mitigation**: Documented in `docs/youtube-transcript-search.md`. Callers needing a different language can clear the cache or hit yt-dlp directly.

### Auto-translation fallback in transcript fetch

- **Location**: `parkour_mcp/youtube.py#_fetch_transcript_sync` and `_no_transcript_response`.
- **Issue**: When a caller requests a language that's not a directly-fetchable track, the tool raises NoTranscriptFound rather than calling `transcript.translate(target).fetch()` to retrieve the auto-translation. The error body advises retry with a source-language code so the caller can translate downstream.
- **Why deferred**: Per [yt-dlp issue #13831](https://github.com/yt-dlp/yt-dlp/issues/13831) (maintainer comment 2026-01-06 by `bashonly`), YouTube specifically rate-limits HTTP 429 against auto-translated subtitle requests while leaving manual subtitles and original-language auto-captions unaffected. Implementing translation fallback would expose every non-source-language request to that documented rate-limit lane. The maintainer's workarounds (fresh browser cookies from a session that recently loaded auto-translated subs, or `--sleep-subtitles 60`) require user-environmental setup that doesn't fit a generic MCP path. Confirmed empirically: a single `.translate('en').fetch()` call from a residential IP drew IpBlocked while direct-source fetches on the same video succeeded cleanly.
- **Mitigation**: `captions_available` in the NoTranscriptFound response lists the browser-visible set (yt-dlp's `automatic_captions`, which is the cross-product of translatable sources and the player response's `translationLanguages` — same data the browser auto-translate menu uses). `captions_source` isolates the directly-fetchable subset that won't draw 429. The error body cites #13831 explicitly so the LLM knows why the wider list isn't actionable through this tool and converges on the source-language transcript on retry. (An earlier attempt sourced `captions_available` from youtube-transcript-api's `list()` instead, but that view under-reports `translation_languages` for some videos relative to the browser, so the swap traded an honest-but-noisy list for a quiet-but-incomplete one. Reverted; the noise is preferable when paired with a clear caveat.)

### SaT (`wtpsplit`) for unpunctuated transcripts

- **Location**: `parkour_mcp/youtube.py#coalesce_windows` and the punctuation-density branch logic.
- **Issue**: Auto-generated captions lack punctuation, so the sentence-aware coalescer can't split on sentence boundaries. The pause-aware time-window fallback (WhisperX Cut & Merge with the `[25s, 35s]` tolerance band) handles unpunctuated input but produces less semantically-coherent windows than sentence-tokenized chunking would.
- **Why deferred**: `segment-any-text/wtpsplit`'s SaT model is the field's converged answer for sentence segmentation of unpunctuated text (~95ms per 1000 sentences on CPU, ONNX-deployable). But it adds an ONNX runtime dependency (~50MB model download) that we deferred until empirical evidence shows the pause-only branch actually produces visibly worse retrieval on auto-captions.
- **How to evaluate**: Compare retrieval quality on a corpus of auto-captioned videos: BM25 search recall using time-window coalescing vs. the same content coalesced via SaT-derived sentence boundaries. If the difference is meaningful, wire SaT in as the unpunctuated branch's coalescer.

## `fetch_direct.py` — deferred enhancements

### Classifier rejects `text/markdown` and `application/yaml`

- **Location**: `parkour_mcp/common.py#_classify_content_type` (whitelist), with the rejection emitted in `parkour_mcp/fetch_direct.py#web_fetch_direct` when the classifier returns `None`.
- **Issue**: The classifier whitelists `text/html`, `application/json`, `application/xml`, and `text/plain`. Markdown (`text/markdown`) and YAML (`application/yaml`, `text/yaml`) return `None`, producing `Error: Unsupported content type '...'`. Both are machine-readable text formats, and the existing non-HTML branch in `web_fetch_direct` already renders raw text with frontmatter, so the data path could carry them trivially. Concrete affected target: Kagi's v1 API spec is served as `text/markdown` (`.md` flat pages) and `application/yaml` (bundle download); see the `kagi.py` bullet in `CLAUDE.md` for URLs. Sessions that need the source-of-truth Kagi spec currently have to shell out to `curl`.
- **Why deferred**: Out of scope for the documentation-first turn that surfaced it. The fix is small (two added branches in `_classify_content_type` returning new classifier labels), and `_SOURCE_EXT_MAP` in `common.py` already maps `.md` to `markdown` and `.yaml`/`.yml` to `yaml`, so the syntax-tag plumbing is in place.
- **Mitigation**: Fetch via `curl` until the classifier is extended.

## `kagi.py` — v0 dormant island

### kagiapi dep, summarize backing code, and balance-lockout helpers retained alongside unregistered tool

- **Location**: `parkour_mcp/kagi.py` (top-level `from kagiapi import KagiClient`; `get_client`, `_extract_balance`, `_check_balance`, `_summarize_locked`, `_handle_v0_error`, and the unregistered `summarize`), `pyproject.toml` (`kagiapi` declared in `dependencies`), `tests/test_kagi.py` (`TestExtractBalance`, `TestCheckBalance`, `TestSummarizeLockout::test_summarize_*`, `TestHandleV0Error`).
- **Issue**: `kagi_summarize` is unregistered on rc1 because v1 has no `/summarize` counterpart yet, but the backing code stays as scaffolding for re-registration. `summarize()` still calls `kagiapi.KagiClient.summarize()` against v0, so the dep, the balance-lockout helpers (`_extract_balance`, `_check_balance`, `_summarize_locked`), the v0 error parser (`_handle_v0_error`), and `get_client` all stay alive even though no registered tool exercises them. Tests preserve coverage on the dormant code so a future re-registration starts from a green suite.
- **Why deferred**: When Kagi ships `/summarize` on v1, the natural follow-up is one focused commit: rewrite `summarize()` against the v1 endpoint, decide whether v1's new billing signal warrants a similar lockout (v1 dropped `meta.api_balance` and the replacement billing flow hasn't shipped), re-register the tool in `__init__.py`, and delete the entire v0 island (kagiapi import + dep, `get_client`, balance helpers, v0 error parser) in the same pass. Removing the island now would force a stub or skip-marker on the dormant function, churn we'd undo on re-registration.
- **How to evaluate**: Watch the Kagi API changelog and the Kagi Discord `#api` forum for `/v1/summarize` landing. On landing, do the migration above and remove this entry.

## `manifest.json` — credential fields declare `sensitive: false` on purpose

- **Location**: `manifest.json` `user_config` — `KAGI_API_KEY`, `GITHUB_TOKEN`,
  `S2_API_KEY`. All three are secrets and all three declare
  `"sensitive": false`.
- **Issue**: `sensitive: true` is the correct declaration and buys two things
  per the MCPB spec — the settings UI masks the input, and the value is stored
  encrypted against an Electron `safeStorage` key held in the OS keychain
  (visible on macOS as the `Claude Safe Storage` keychain item). But Claude
  Desktop on Windows forwards the *stored ciphertext* into the server's
  environment instead of decrypting it first
  ([anthropics/claude-code#78296](https://github.com/anthropics/claude-code/issues/78296),
  open, labeled `bug` / `platform:windows` / `area:mcp` / `area:desktop`). The
  server receives `KAGI_API_KEY=__encrypted__:djEw…`, sends it as a bearer
  token, and reports "Invalid API key" while the settings UI plainly shows a
  key. Non-sensitive fields in the same `user_config` resolve correctly, which
  is what makes flipping the flag a working route around it.
- **Why deferred**: the flag is presently worse than useless on Windows — it
  is the difference between a key that works and one that cannot. The cost is
  real but bounded: the value is no longer masked in the settings UI, and it
  is stored as plaintext rather than ciphertext. It is *not* stored more
  loosely — `Claude Extensions Settings/` is `0700` with per-extension files
  at `0600` either way, which is stricter than the `~/.config/parkour/`
  fallback this project also supports. The masking loss was accepted
  explicitly by the maintainer.
- **How to evaluate**: watch #78296. When Claude Desktop decrypts `sensitive`
  values before `${user_config.*}` substitution, restore `"sensitive": true`
  on all three fields and delete this entry.
  `tests/test_manifest.py::TestCredentialSensitivity` pins the deviation so
  the restore is a deliberate act rather than a silent drift, and that test
  is deleted in the same change.
- **Upgrade caveat, unverified**: a user who set these fields while the
  manifest declared them `sensitive: true` may still have an
  `__encrypted__:` blob in `Claude Extensions Settings/<ext>.json`. Whether
  Claude Desktop rewrites that value when the declaration changes is untested
  — nobody here has a Windows box with a pre-existing install. If it does not,
  those users keep receiving ciphertext and `common.py#clean_env` accepts it,
  because it screens for the `${` sentinel and not this one.

## Structural tradeoffs

### `<header>` stripped from all pages — loses real h1s on spec docs

- **Location**: `parkour_mcp/markdown.py:44` (`_NOISE_TAGS`) → `_HTMD_SKIP_TAGS` at line 154, passed to htmd's `skip_tags` option.
- **Issue**: `<header>` is decomposed on every page as site chrome. Spec documents (WHATWG HTML Living Standard and likely others) use `<header>` semantically for the document's primary h1 and metadata block, so the real title and subtitle are discarded along with the site-chrome content the strip targets on typical pages.
- **Why deferred**: `<header>` is correctly site-chrome for ~99% of the open web; leaking nav/branding h1s into body output would be a worse default. Fixing the spec-doc case structurally needs either (a) context-sensitive stripping (strip `<header>` at nav depth but not at document root) or (b) a per-site escape hatch. Both are significantly more involved than the affected-page count justifies.
- **Mitigation**: The title ladder falls through to `<title>` / `og:title` via `_extract_head_title` when no h1 survives outside fenced code (see `TestHtmlTitleExtraction`). For WHATWG this yields `"HTML Standard"` from `<title>`. The in-body visual subtitle ("Living Standard — Last Updated…") is still lost but has low information value.

### Unspaced-script search matches n-gram sets, not substrings

- **Location**: `parkour_mcp/_pipeline.py` under "Unspaced-script indexing" (`_UNSPACED_RUN_RE`, `_unspaced_runs`, `_UNSPACED_ANALYZER`), consumed by `_pipeline.py#_CacheEntry` and `youtube.py#_TranscriptEntry`.
- **Issue**: tantivy's `NgramTokenizer` fixes every n-gram at position 0, which is documented behavior rather than a defect, so the phrase query the parser builds for a multi-token term cannot verify adjacency. A Japanese term is therefore matched as "this run contains all of these n-grams", not "this run contains this substring", and a run holding a term's characters in scrambled order is a false positive that can outrank the true match. Multi-valued runs bound the damage to a single punctuation-delimited clause (`TestUnspacedScriptSearch::test_term_must_fall_within_one_run` pins that), and the failure needs a run that carries every n-gram of the term without carrying the term, which is uncommon above two characters.
- **Why accepted**: closing it needs true positions, which means pre-computing n-grams in Python and indexing them under the `whitespace` tokenizer. The query side would then have to be rewritten into explicit bigram phrase syntax before reaching `parse_query_lenient`, which means hand-assembling query strings around caller-supplied operators — a large astonishment surface, and a regression risk for every existing Latin query, to buy precision on a retrieval task whose job is picking which of ~16 slices to read.
- **Also known, same design**: Thai, Lao, Khmer, Myanmar and Tibetan are included even though Lucene's CJK filter covers only Han, Hangul, Hiragana and Katakana and reaches for ICU's dictionary break iterator instead. An orthographic unit in those scripts is a cluster rather than a codepoint, so some tokens are bare combining marks. This is noise, not a correctness fault (matching is conjunctive, so a junk token constrains rather than loosens), and the comparison is against zero recall.
- **How to evaluate**: revisit if `quickwit-oss/tantivy-py#25` lands — a lindera or jieba build would supply real word tokens with real positions and retire the whole approach. It has been open since 2020 and PR #200 is draft, so do not plan around it. Note that a from-source build is the only route today, which the wheel-install path this project depends on cannot take.

## Generic fetch transport: httpx cannot win the WAF coherence check

Decision recorded 2026-08-16: migrate the generic fetch path from httpx to
`wreq`. Not yet implemented. The evidence is below so the decision can be
re-litigated on facts rather than re-derived.

### The defect: two WAF vendors want opposite things and httpx satisfies neither

- **Location**: `common.py#guarded_fetch`, `common.py#_FETCH_HEADERS`.
- **Issue**: modern WAFs score the *coherence* of the claimed identity (the
  User-Agent) against the observed transport fingerprint (TLS JA3/JA4, and the
  HTTP/2 SETTINGS frame plus header ordering). httpx emits the `h2` library's
  fingerprint while `_FETCH_HEADERS` claims Chrome, and that mismatch is
  detectable. The two vendors disagree about which pairing is incoherent, so no
  choice of `http2=` default satisfies both:
  - Akamai Bot Manager refuses HTTP/1.1 carrying a modern-Chrome User-Agent.
    Guarded by `test_live.py::test_guarded_fetch_clears_akamai_403`.
  - Cloudflare zones on a strict Managed Challenge (403 carrying
    `cf-mitigated: challenge`) refuse our HTTP/2 fingerprint. Per-zone, not a
    Cloudflare default: `support.nzxt.com` and `support.discord.com` challenge
    us and serve us on HTTP/1.1, while `support.zendesk.com` and
    `developers.cloudflare.com` serve us on either. `stackoverflow.com` refuses
    both, which no transport change fixes.
- **Why a fallback was rejected**: catching `cf-mitigated: challenge` and
  retrying with a browser fingerprint is worse than doing nothing. Cloudflare
  mints `__cf_bm` on the challenge response and binds it to the client
  fingerprint, so carrying it across the pivot presents a cookie issued to
  fingerprint A while showing fingerprint B, and dropping it still leaves a
  same-IP, same-URL, total-TLS-identity change within seconds. Real browsers
  never do that. The retry teaches a behavioral signature that per-request
  coherence does not repair.

### Why `wreq` over `curl_cffi`

Both clear the NZXT Cloudflare challenge and the Akamai PDF in
`test_live.py`; httpx clears only Akamai. `curl_cffi` looked like the obvious
pick (already a dependency for `reddit.py`, already has a test double in
`conftest.py#_FakeAsyncSession`) and was rejected on three findings:

- **`lexiforest/curl_cffi#798` (open): streaming has no backpressure.** libcurl
  reads at full line rate into an unbounded queue, so a slow consumer grows
  memory without bound, on both `Session` and `AsyncSession`. That attacks the
  exact guarantee this migration exists to preserve: `guarded_fetch`'s Layer 2
  would bound what we *keep*, not what gets *buffered*. Migrating to a client
  that weakens the size cap while fixing the WAF problem is a bad trade.
  `#319` (async streaming failing on session reuse) was closed as stale rather
  than fixed.
- **Fingerprint currency is freemium.** `curl-cffi update` pulls from
  `impersonate.pro`, the maintainer's commercial service; Chrome / Safari /
  Firefox are the free tier. Coupling our block-resistance to a third-party
  commercial service is a durability risk. `wreq` compiles 100+ profiles into
  the wheel with no external dependency.
- **Fingerprint fidelity bugs are open**, e.g. chrome impersonation sending the
  HTTP/2 priority header on HTTP/1.1 connections, which real Chrome does not.

Counterweight, recorded honestly: `curl_cffi` has roughly 116x the adoption
(40.6M monthly PyPI downloads against `wreq`'s 350k) and 6,320 stars against
1,425. Choosing `wreq` buys a cleaner tracker (6 open issues, all feature
requests, zero bugs) at the cost of a much smaller community when something
breaks. `wreq` also carries rename fragmentation: the dead `rnet` package still
draws 136k monthly downloads, 28% of the combined total, six months after its
final release.

**`wreq` is not pre-1.0 in the way its version implies.** It is the former
`rnet`, renamed 2026-03-26 to unify bindings under a common prefix
(`wreq-python`, `wreq-node`, `wreq-ruby`). `v3.0.0-rc22...v0.10.0` is
`ahead_by 23, behind_by 0`, and the v0.10.0 release notes are the accumulated
v3 changelog. The version reset is cosmetic, not a rewrite.

### Migration cost

Every `guarded_fetch` guarantee ports; nothing is functionally lost. Spiked and
verified against live endpoints:

| guarantee | replacement |
|---|---|
| Content-Length gate | `Response.content_length` |
| streaming size cap | `Response.stream()` async context manager |
| wall-clock deadline | `asyncio.timeout`, unchanged |
| SSRF address pinning | `DnsOptions.add_resolve(domain, [addrs])` |

- `_PinningBackend` and `_GuardedTransport` are **deleted, not adapted**: both
  subclass httpx/httpcore internals that libcurl and Rust have no seam for.
  Resolve-check-pin is the documented industry pattern for defeating DNS
  rebinding, not a workaround (Nette ships `getResolvedIPs()` specifically to
  feed `CURLOPT_RESOLVE`), so the architecture survives and only its vehicle
  changes.
- The hand-rolled RFC 6555 address walk in `_PinningBackend` goes away:
  `add_resolve` takes the whole validated address list and the library handles
  fallback.
- Redirects stop being free. httpx invokes the transport once per hop, which is
  what gives `_GuardedTransport` redirect coverage with no redirect-specific
  code. `wreq` needs `Policy.none()` plus a manual loop that re-pins per hop.
- `BlockedAddress` subclasses `httpx.TransportError` so every existing
  `except httpx.RequestError` arm catches it for free. 14 catch sites on the
  generic path were re-homed onto `FetchError`; the other 17 belong to fast-path
  modules and stay.
- respx cannot see a non-httpx client. Upper bound 237 mock sites across
  `test_fetch_direct.py` (146), `test_slicing.py` (43), `test_fetch_js.py` (41),
  `test_common.py` (7).

**Landed** in `2b3a892`, `dc304e6`, `953f618`, `b8ab7f7`. Two corrections to
what this entry originally predicted, both from measurement rather than
revision:

- **Zero respx sites were rewritten.** `build_client` is the only seam through
  which `_transport` reaches wreq, so the `conftest.py` double replaces it with
  a client that issues through httpx, which respx already intercepts. That was
  necessary rather than merely cheap: `test_fetch_direct.py` mocks generic hosts
  *and* `api.github.com` in one file, so a blanket migration would have broken
  the fast-path routes. The tradeoff is that the offline generic-path tests do
  not exercise wreq. Accepted, because respx never exercised httpx's transport
  either (it replaces it), so those tests were always pipeline tests wearing
  HTTP clothing. wreq's own behaviour is covered by `tests/test_transport.py`
  and the live classes in `test_live.py`.
- **Wiring surfaced two real defects**, not test artifacts: wreq's
  `TimeoutError` fell through to the catch-all instead of becoming
  `FetchTimeout`, and every network fault flattened to "TransportFailure" in
  user-facing strings. `FetchError.label` now carries the wrapped cause.

### What stays on httpx, and why that is not a liability

The remaining httpx consumers are safe indefinitely, because **the WAF
coherence gate fires on a lie, not on being a bot.** An honest
`parkour-mcp/… httpx/…` User-Agent makes no claim a TLS fingerprint can
contradict, so there is nothing to catch. Measured, not assumed:
`doi.org`, `datatracker.ietf.org`, and `www.rfc-editor.org` are all
Cloudflare-fronted and all answer the honest UA with 200.

So the endpoint is two stacks, permanently, and this supersedes the earlier
plan to port `guarded_client`:

- **`mediawiki.py` stays on httpx with its honest UA.** Porting it to wreq would
  buy nothing, and adopting a browser identity would violate the Wikimedia
  User-Agent policy the honest UA exists to satisfy. `guarded_client` therefore
  stays, and `BlockedAddress`'s `httpx.TransportError` base is **permanent, not
  transitional**.
- **`discourse.py` was the one fast path with the generic path's exposure
  profile** (browser identity plus caller-supplied host). Resolved in `b8ab7f7`
  by making it honest rather than by porting it: git history showed the
  masquerade arrived in the module's first commit with no block to justify it,
  and all four endpoint shapes answer an identified client with 200 across four
  independent instances. The header-selection policy now lives in `common.py`
  above the two constants.
- **`github.py` still sends the browser identity** to a fixed host that
  tolerates it and where token auth carries the real access. Contained; revisit
  only if GitHub starts challenging it.

### Still worth doing

**Both follow-ups landed.** `reddit.py` is ported and `curl-cffi` is gone from
the dependency list; the `guarded_fetch` sweep is done and recorded in the
outbound-hardening entry below. The port retires the open streaming-backpressure
issue (`lexiforest/curl_cffi#798`) and the freemium fingerprint-refresh
coupling to `impersonate.pro`, and leaves two HTTP stacks rather than three.
Reddit keeps the browser identity: it is the worked example of an origin that
has withdrawn access to content it does not exclusively license.

One wart the port had to work around, worth knowing before touching another
wreq caller: **wreq's exception types are flat.** Every one subclasses
`Exception` directly, with no `RequestException`-style base, so
`reddit.py#_WREQ_FETCH_ERRORS` enumerates them rather than catching a base.
A future wreq release adding a new error type will fall through that tuple.

### Two `wreq` footguns the migration must neutralize

- **Unknown keyword arguments are silently accepted**, on both the `Client`
  constructor and every per-request method. Passing `dns=` instead of
  `dns_options=` disables SSRF pinning and the request still succeeds to an
  unvalidated address. This is a fail-open on the parameter carrying the
  security property, and it was hit for real while spiking. Mitigation:
  construct the client in exactly one wrapper, and pin a test asserting
  `Response.remote_addr` matches the validated address. That assertion is a
  guarantee httpx cannot give us today at all, so the net position improves.
  Upstream fix assessed as tractable; see below.
- **`content_length` returns `0`, not `None`, when the origin omits it.** A
  naive `if cl > max_bytes` never fires, and `if not cl` conflates absent with
  empty. Handle absence explicitly.

### Upstream fix for the silent-kwarg fail-open

- **Root cause**: `wreq-python/src/macros.rs#extract_option` pulls named keys
  out of the mapping one at a time (`if let Ok(value) = ob.get_item(...)`) and
  never checks for leftovers. Systemic rather than local: 152 call sites across
  `src/client.rs`, `src/client/req.rs`, `src/http1.rs`, `src/http2.rs`,
  `src/tls.rs`, `src/proxy.rs`.
- **Shape of the fix**: add a `reject_unknown_keys(ob, &[...])` helper and an
  `extract_options!` wrapper macro taking the field list, so the list stays
  single-sourced and each `FromPyObject::extract` becomes one invocation. The
  152 statements collapse into 6 lists. Guard the helper on a `PyDict`
  downcast, since these extractors also run against pyclass instances.
- **Effort**: low and mechanical for the code. The real cost is the build
  environment (BoringSSL needs cmake, perl, libclang, a Rust toolchain, plus
  maturin) and a long first compile.
- **Acceptance odds: good.** External PRs merge regularly, including features
  from non-owners (`@0x1997` implementing `raise_for_status`, `@Averyy` fixing
  `KeyShare` registration). Python-level tests already exist under `tests/`
  (`request_test.py`, `dns_test.py`, and siblings), so the PR needs no Rust
  test harness. It is a breaking change by nature, which is cheapest to land
  while the project sits at 0.x.

## Outbound fetch hardening: two paths still bypass the caps

**Mostly resolved.** Every httpx caller now routes through
`common.py#guarded_fetch`: `arxiv.py`, `doi.py`, `ietf.py`,
`semantic_scholar.py`, `github.py`, `huggingface.py`, `mediawiki.py`,
`discourse.py`, the shared deps.dev helper in `common.py#_depsdev_get`, and the
`fetch_js.py` HEAD pre-check. `guarded_fetch` grew `params=` and `method=` to
make that possible without hand-encoding query strings at twenty-five call
sites. Each of them now has the Content-Length gate, the streaming size cap,
and the 60 s wall-clock deadline.

Sizes use the ceilings already established rather than opting out. API metadata
takes the 5 MiB default; the MediaWiki page parse and the GitHub raw blob take
the 50 MiB sections ceiling, because those two return a document rather than
metadata and a monolithic article legitimately exceeds the smaller cap.
Disabling the cap was the first attempt and was wrong for a reason worth
keeping: it trades falsely refusing a large *legitimate* response for having no
defense against a large *illegitimate* one, which is the whole point of the cap.

**Two paths still bypass the caps, both for structural reasons:**

- **`reddit.py`** builds a `wreq` `Client` directly, because it needs the Safari
  emulation to clear Reddit's JA3 filter. `_transport.py#guarded_fetch` has the
  caps but hardcodes the Chrome emulation, so Reddit cannot use it as-is.
  Closing this means letting `build_client` take an emulation, which is a small
  change nobody has needed yet.
- **`youtube.py`** goes through yt-dlp, which owns its own HTTP stack. Not
  reachable from either `guarded_fetch` without reimplementing what yt-dlp does.

Neither chooses its destination from caller input, so SSRF stays out of scope
for both; the exposure is a hang or an oversized body from a first-party host.

- **How to evaluate**: revisit if a real hang / oversize incident surfaces from a first-party API, or when the shared HTTP-client-factory idea from the cross-cutting abstraction audit is picked up. The cheapest improvement is to give every outbound path the wall-clock deadline at minimum (it always applies in `guarded_fetch`, even when size caps are disabled); the fuller fix is a shared `_api_client` factory that wraps the caps so all paths inherit them uniformly.
