# Technical Debt

Acknowledged warnings and deferred fixes. Each entry includes the source, the issue, and why it was deferred.

## Pyright warnings (opted not to fix)

### `fetch_direct.py` — `_matched_meta` not accessed

- **Location**: `_sections_response()`, line ~458
- **Issue**: `_matched_meta` is destructured from the return of `_filter_markdown_by_sections()` but never used.
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

### SaT (`wtpsplit`) for unpunctuated transcripts

- **Location**: `parkour_mcp/youtube.py#coalesce_windows` and the punctuation-density branch logic.
- **Issue**: Auto-generated captions lack punctuation, so the sentence-aware coalescer can't split on sentence boundaries. The pause-aware time-window fallback (WhisperX Cut & Merge with the `[25s, 35s]` tolerance band) handles unpunctuated input but produces less semantically-coherent windows than sentence-tokenized chunking would.
- **Why deferred**: `segment-any-text/wtpsplit`'s SaT model is the field's converged answer for sentence segmentation of unpunctuated text (~95ms per 1000 sentences on CPU, ONNX-deployable). But it adds an ONNX runtime dependency (~50MB model download) that we deferred until empirical evidence shows the pause-only branch actually produces visibly worse retrieval on auto-captions.
- **How to evaluate**: Compare retrieval quality on a corpus of auto-captioned videos: BM25 search recall using time-window coalescing vs. the same content coalesced via SaT-derived sentence boundaries. If the difference is meaningful, wire SaT in as the unpunctuated branch's coalescer.

## Structural tradeoffs

### `<header>` stripped from all pages — loses real h1s on spec docs

- **Location**: `parkour_mcp/markdown.py:44` (`_NOISE_TAGS`) → `_HTMD_SKIP_TAGS` at line 154, passed to htmd's `skip_tags` option.
- **Issue**: `<header>` is decomposed on every page as site chrome. Spec documents (WHATWG HTML Living Standard and likely others) use `<header>` semantically for the document's primary h1 and metadata block, so the real title and subtitle are discarded along with the site-chrome content the strip targets on typical pages.
- **Why deferred**: `<header>` is correctly site-chrome for ~99% of the open web; leaking nav/branding h1s into body output would be a worse default. Fixing the spec-doc case structurally needs either (a) context-sensitive stripping (strip `<header>` at nav depth but not at document root) or (b) a per-site escape hatch. Both are significantly more involved than the affected-page count justifies.
- **Mitigation**: The title ladder falls through to `<title>` / `og:title` via `_extract_head_title` when no h1 survives outside fenced code (see `TestHtmlTitleExtraction`). For WHATWG this yields `"HTML Standard"` from `<title>`. The in-body visual subtitle ("Living Standard — Last Updated…") is still lost but has low information value.
