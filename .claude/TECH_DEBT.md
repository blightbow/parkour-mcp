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

- **ruff** is the only gate. CI runs `uv run pytest`, and `pytest-ruff` lints
  every file in `testpaths` as part of that run, so a ruff finding fails the
  build. Config lives in `[tool.ruff.lint]`.
- **ty** is configured (`[tool.ty.src]`) and expected to stay clean, but is
  **not** wired into CI — run `uv run ty check parkour_mcp/ scripts/ tests/`
  by hand. Suppressions use `# ty: ignore[rule]` and must carry a reason.
- **vulture** (`just lint-deep`) gates version-tag pushes via
  `scripts/git-hooks/pre-push`, which is the only place it runs — CI's
  tag-push job is `uv run pytest` and nothing more. Findings are fixed at the
  source; `.vulture_whitelist.py` takes false positives only, each with a
  comment naming what vulture cannot see.

Suppressions therefore use `# noqa: RULE` for ruff and `# ty: ignore[rule]`
for ty, each with a reason. A suppression in any other checker's dialect is
inert — nothing here consumes it.

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

### `html_to_markdown` on megapages — the dominant generic-HTTP latency

- **Location**: `parkour_mcp/markdown.py:82` (`html_to_markdown`) via `markdownify` + BeautifulSoup4
- **Measured cost** (see `scripts/benchmark_baselines.json`):
  - PEP 8 (48 KB markdown): ~88 ms
  - ECMAScript spec (3 MB markdown): ~6,940 ms
  - WHATWG HTML spec (6 MB markdown): **~17,200 ms**
- **Scope**: generic HTTP path only. Every fast path bypasses `html_to_markdown` entirely.
- **Context**: An audit discovered `web_fetch_sections` wasn't populating `_page_cache`, so every `sections → direct` flow re-ran `html_to_markdown` — paying this cost twice. That gap is now closed (see `tests/test_perf.py` for regression coverage). But the underlying single-call cost remains the dominant latency for large-page generic-HTTP flows.
- **Why deferred**: The cache fix removes the worst-case duplication. The remaining single-call cost is paid only once per page per session and is rare in practice (megapages are outliers). A remediation would be non-trivial: replace the BeautifulSoup-based `TextOnlyConverter` with a faster HTML parser (e.g. `selectolax`, `html5-parser`, or `lxml`) or cap the converter input size before parsing. Worth doing when a real regression or user report justifies the effort.
- **Regression guard**: `tests/test_perf.py::test_html_to_markdown` asserts wall-clock stays within 2× of the captured baseline. Raises an alarm if a refactor accidentally pessimises the HTML→markdown step.

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

## Structural tradeoffs

### `<header>` stripped from all pages — loses real h1s on spec docs

- **Location**: `parkour_mcp/markdown.py:44` (`_NOISE_TAGS`) → `_HTMD_SKIP_TAGS` at line 154, passed to htmd's `skip_tags` option.
- **Issue**: `<header>` is decomposed on every page as site chrome. Spec documents (WHATWG HTML Living Standard and likely others) use `<header>` semantically for the document's primary h1 and metadata block, so the real title and subtitle are discarded along with the site-chrome content the strip targets on typical pages.
- **Why deferred**: `<header>` is correctly site-chrome for ~99% of the open web; leaking nav/branding h1s into body output would be a worse default. Fixing the spec-doc case structurally needs either (a) context-sensitive stripping (strip `<header>` at nav depth but not at document root) or (b) a per-site escape hatch. Both are significantly more involved than the affected-page count justifies.
- **Mitigation**: The title ladder falls through to `<title>` / `og:title` via `_extract_head_title` when no h1 survives outside fenced code (see `TestHtmlTitleExtraction`). For WHATWG this yields `"HTML Standard"` from `<title>`. The in-body visual subtitle ("Living Standard — Last Updated…") is still lost but has low information value.

## Outbound fetch hardening — fast paths bypass `guarded_fetch`

- **Location**: every API/fast-path module builds and calls its own client directly: `arxiv.py`, `doi.py`, `semantic_scholar.py`, `ietf.py`, `mediawiki.py`, `github.py`, `huggingface.py`, `reddit.py` (`curl_cffi`), `discourse.py`, `youtube.py`, `packages.py`, `scorecard.py` (the latter two via the shared deps.dev client in `common.py#_depsdev_get`). Only `parkour_mcp/_pipeline.py` (generic HTTP), `fetch_direct.py`, and `fetch_js.py` route through `common.py#guarded_fetch`.
- **Issue**: `guarded_fetch` layers three caps the fast paths therefore skip: the Content-Length gate, the streaming size cap, and the always-on `asyncio.timeout(60.0)` wall-clock deadline (see *Outbound request defenses* in `docs/frontmatter-standard.md`). A first-party API that hangs mid-stream or returns an unexpectedly large body is bounded only by each module's per-request `timeout=` (connect/read budget), not by a whole-request deadline or a size ceiling. SSRF is not in scope here — these hit fixed first-party hosts, not caller-supplied URLs — but the wall-clock deadline and size caps are generic robustness properties that currently apply unevenly across the codebase.
- **Why deferred**: the fast paths target trusted, well-behaved first-party endpoints (arxiv.org, the GitHub / HuggingFace / deps.dev / Datatracker / RFC Editor APIs, oauth.reddit.com), so practical exposure is low, and several modules need bespoke clients anyway (reddit's `curl_cffi` Safari impersonation to clear the JA3 filter, the shared deps.dev client and limiter). Threading `guarded_fetch` through eleven modules with differing client construction is a non-trivial refactor for a low-probability failure mode.
- **How to evaluate**: revisit if a real hang / oversize incident surfaces from a first-party API, or when the shared HTTP-client-factory idea from the cross-cutting abstraction audit is picked up. The cheapest improvement is to give every outbound path the wall-clock deadline at minimum (it always applies in `guarded_fetch`, even when size caps are disabled); the fuller fix is a shared `_api_client` factory that wraps the caps so all paths inherit them uniformly.
