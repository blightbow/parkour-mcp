# YouTube Transcript Search Design

This document captures the architectural decisions for adding BM25 search,
time-range filtering, and explicit window retrieval to the `youtube`
tool's `transcript` action. Step 3 of the YouTube tool implementation
sequencing (steps 1 and 2 shipped the `video` and basic `transcript`
actions; step 3 layers a Tantivy index and search action args on top).

## Why a separate cache

`_pipeline.py#_CacheEntry` defines a write-once Tantivy schema with three
fields (`body`, `heading`, `idx`). Extending it to add `start_seconds`
and `end_seconds` fast fields would force a global schema change
affecting every cached document type (markdown pages, GitHub blobs,
Reddit threads, Discourse topics, MediaWiki articles). The fields only
make sense for time-indexed content; the rest of the codebase has no
use for them.

Solution: a sibling cache `_TranscriptCache` with its own schema. Same
2Q discipline as `_PageCache`, same group-eviction semantics, but a
distinct entry shape. Lives in `parkour_mcp/youtube.py` initially. If a
second time-series source ever appears (podcasts via a sibling tool,
say), promote both classes to `_pipeline.py` at that point.

## Cache layer

### `_TranscriptCache`

Mirrors `_pipeline.py#_PageCache`:

- 2Q with probation FIFO + protected LRU.
- Default `MAX_ENTRIES = 8` (matches `_PageCache`).
- `get(url)`: probation hit promotes to protected; protected hit refreshes LRU.
- `store(url, ...)`: in-place replace if URL exists in either queue, else admit to probation.
- `_evict()`: prefer probation, fall back to protected LRU tail.
- Group-aware eviction: when victim has a `group` tag, all entries sharing
  that tag (across both queues, both this cache and `_PageCache`) evict
  together.

Group key convention: `f"yt:{video_id}"`. The same key tags the
`_PageCache` entry created by the `video` action when it eventually
populates the page cache. This means a `transcript` cache entry and a
`video` cache entry for the same video evict together, keeping the two
representations of one source coherent.

Cross-cache group eviction note: the existing `_PageCache._evict()`
walks `_probation` and `_protected` only. Step 3 must extend the
eviction path so a group eviction triggered in either cache walks both
caches. Implementation: a small `_evict_group(group_key)` helper that
both caches' eviction paths call after identifying the victim's group.

### `_TranscriptEntry`

Holds:

- `url`, `video_id`, `language_code`, `is_generated`
- `segments: tuple[Segment, ...]` — raw segments from `youtube-transcript-api`
- `windows: tuple[Window, ...]` — coalesced ~30s windows (built at
  store-time, not lazily)
- `chunking_strategy: str` — "sentence" or "time_window"
- `group: str | None`
- Lazy: `_tantivy_index`, `_built: bool`

Windows build at store time because they're cheap (pure-Python coalescing
with no I/O) and the renderer needs them for the no-search response. The
Tantivy index builds lazily on first `search()` call so the basic
`transcript` action (no search) skips the indexing cost.

## Tantivy schema

| Field | Type | Stored | Indexed | Fast | Tokenizer | Notes |
|---|---|---|---|---|---|---|
| `body` | text | no | yes | no | `default` | Concatenated segment text within the window |
| `chapter` | text | no | yes | no | `default` | Title of the chapter containing the window's start time, or empty |
| `body_unspaced` | text | no | yes | no | `unspaced_ngram` | The unspaced-script runs of `body`, multi-valued |
| `chapter_unspaced` | text | no | yes | no | `unspaced_ngram` | The unspaced-script runs of `chapter`, multi-valued |
| `idx` | unsigned | yes | yes | no | (n/a) | Window index. The only field retrieved from search results |
| `start_seconds` | f64 | no | yes | yes | (n/a) | Window start; `fast=True` enables range queries and time-ordering |
| `end_seconds` | f64 | no | yes | yes | (n/a) | Window end; same |

Schema decisions:

- **Default tokenizer matches `SEARCH_GRAMMAR_DOC`**. The existing
  documentation tells callers there's no stemming and to search for both
  `"prompt"` and `"prompts"` if they want either. Using a different
  tokenizer here would create a confusing inconsistency between
  `WebFetchIncisive`'s `search=` and `Youtube`'s `transcript:search=`.
- **`chapter` is parsed via `parse_query_lenient`**, same as `body`, so
  callers can use the same query syntax (phrase, fuzzy, AND/OR) on
  chapter titles as on transcript text. `chapter="intro"` matches
  windows whose chapter title contains "intro" via the default
  tokenizer.
- **The `*_unspaced` shadow fields carry the same two texts under a
  character n-gram analyzer.** The default tokenizer breaks on
  non-alphanumerics only, which is not a word boundary in Japanese,
  Chinese, Thai and their neighbours, so without them a caption track in
  one of those languages matches no query at all. Each query is parsed
  against a field and its shadow together. The runs, the analyzer, and
  why it is n-grams rather than a dictionary segmenter are all in
  `_pipeline.py` under "Unspaced-script indexing"; the index must be
  built through `_pipeline.py#_new_search_index`, which registers the
  analyzer that these fields name.
- **`idx` is the only stored field**. Window text, timestamps, and
  ancestry are reconstructed from the Python-side `windows` tuple keyed
  by `idx`. Mirrors `_pipeline.py#_CacheEntry` exactly.
- **`start_seconds` and `end_seconds` are separate fields**. Tantivy
  range queries on a single field handle "starts after X" cleanly;
  "overlaps [A, B]" is a two-clause `BooleanQuery`. No reason to
  conflate them.

Document writes happen in a single batch, single commit, single reload —
matches `_pipeline.py#_CacheEntry._ensure_built` exactly.

## Action arg surface

New parameters on the `youtube` dispatcher (active when `action="transcript"`):

```python
search: Optional[str] = None,
windows: Optional[list[int]] = None,
start_seconds: Optional[float] = None,
end_seconds: Optional[float] = None,
order: Literal["score", "time"] = "score",
```

Existing transcript params (`languages`, `timestamps`) continue to apply
to all retrieval shapes.

### Validation matrix

| Arguments set | Behavior | Frontmatter |
|---|---|---|
| (none of search/windows/start_seconds/end_seconds) | Full-transcript render (current step-2 behavior) | `total_windows`, `total_segments`, `chunking_strategy` |
| `windows=[i, j, ...]` | Explicit lookup by index; out-of-range indices skipped with a `note` | `requested_windows`, `total_windows`, `unknown_windows` if any |
| `search="query"` | BM25 over `body`; results ranked by `score` (default) or `start_seconds` ascending (`order="time"`) | `total_windows`, `matched_windows`, `search`, optional `warning` from `parse_query_lenient` |
| `start_seconds=` and/or `end_seconds=` | Range filter on `[start_seconds, end_seconds]` with the standard "overlaps" semantics; without `search` it's a pure time-range listing | `total_windows`, `matched_windows`, `start_seconds`, `end_seconds` echoed |
| `search="query"` + range | Composes via `BooleanQuery`: BM25 ∧ range | `matched_windows`, `search`, range echoed |

### Mutual exclusion

- `search` and `windows` are mutually exclusive. The dispatcher returns
  an `Error: ...` string analogous to `web_fetch_direct`'s
  `search`/`slices` handling. `windows` is an explicit lookup; mixing it
  with a query is incoherent.
- `start_seconds`/`end_seconds` may NOT be combined with `windows`.
  Range filtering on top of an explicit index list serves no purpose.
- `order="time"` with `windows=...` is silently ignored: explicit-index
  lookup returns results in the order the caller specified.

### Range semantics

A window is a match when `window.start_seconds < end_seconds` AND
`window.end_seconds > start_seconds`. This is the standard half-open
interval overlap. `start_seconds` defaults to 0; `end_seconds` defaults
to `+inf`. Both inclusive on the lower bound, both exclusive on the
upper, matching how `tantivy.Query.range_query` constructs intervals.

If `start_seconds > end_seconds`, the dispatcher returns
`Error: start_seconds must be <= end_seconds`.

If the time range falls entirely outside the transcript's bounds, the
response renders normally with `matched_windows: []` and a frontmatter
`note` explaining the empty result. Not an error.

## Frontmatter shapes

### Full-transcript fetch (no search/windows/range)

Unchanged from step 2. `total_windows`, `total_segments`,
`chunking_strategy` all present. Body is the full rendered transcript.

### Window retrieval

```yaml
---
source: https://www.youtube.com/watch?v=...
api: youtube-transcript-api
video_id: ...
transcript_language: en
transcript_kind: manual
total_windows: 23
requested_windows: [3, 7, 14]
matched_windows: [3, 7, 14]
unknown_windows: [99]   # only present if any indices were out of range
chunking_strategy: sentence
trust: ...
---
```

Body renders the requested windows in the requested order, separated by
two blank lines. Each window includes its anchor (matches the compact
mode rendering rules from step 2).

### Search

```yaml
---
source: https://www.youtube.com/watch?v=...
api: youtube-transcript-api
video_id: ...
transcript_language: en
transcript_kind: auto
total_windows: 47
matched_windows: [3, 7, 14]
search: "elephants"
order: score                 # omitted when default
start_seconds: 60.0          # only when set
end_seconds: 120.0           # only when set
chunking_strategy: time_window
warning: "..."               # parse_query_lenient errors, if any
hint: "windows=[2,3,4,6,7,8,13,14,15] for context around matches"
trust: ...
---
```

Body renders the matched windows in the order returned by the searcher
(score order by default, `start_seconds` ascending when
`order="time"`). The `hint` for context retrieval is constructed by
the dispatcher: for each matched window `i`, suggest `[i-1, i, i+1]`
(clamped to valid indices, deduped).

## Edge cases

| Case | Behavior |
|---|---|
| Empty transcript (no snippets) | Error from step 2 path; cache not populated |
| Single-window video | All retrieval forms work; `search` may degenerate to "the only window matched" |
| Out-of-range window in `windows=[...]` | Filtered out, listed in frontmatter `unknown_windows`. Empty result returns full frontmatter with `matched_windows: []` and a `note` |
| Time range outside transcript bounds | `matched_windows: []` with a `note` explaining bounds |
| `search` with bad syntax | `parse_query_lenient` errors surface as frontmatter `warning` (matches existing convention in `_pipeline.py#_CacheEntry.search`) |
| Cache miss on `search=` (re-fetch path) | Treated as a normal first fetch + index build. Cost is paid once |
| Concurrent fetches of same URL | Last writer wins. Minor index-build duplication, no correctness issue |

## Caching lifecycle

1. **First `transcript` call** for a URL (no `search=`): fetch via
   `youtube-transcript-api`, build segments + windows, store in
   probation. Render and return. **No Tantivy index built yet.**
2. **Second call** with `search=`: cache hit, promote to protected,
   trigger `_ensure_built()` to construct the Tantivy index, run search,
   return ranked windows.
3. **Subsequent searches** on the same URL: cache hit (already
   protected), index already built, run search.
4. **Eviction**: if the entry's `group` shares a key with a `_PageCache`
   entry (the `video` action's frontmatter cache), evicting either
   triggers eviction of both.

## Tests

Tests live in `tests/test_youtube.py` alongside step 1+2 tests. New
classes:

- `TestTranscriptCache` — get / store / promotion / probation eviction /
  group eviction across caches.
- `TestTranscriptEntry` — windows built eagerly, index built lazily,
  `is_built` reflects state without forcing build.
- `TestTranscriptSchema` — schema fields, types, and flags via direct
  Tantivy schema interrogation.
- `TestTranscriptSearch` — BM25 query (single + multi-word), parse
  warning surfacing, score-ordered vs time-ordered, range filter,
  combined search + range, empty match.
- `TestTranscriptWindowRetrieval` — `windows=` action arg, including
  out-of-range handling.
- `TestTranscriptDispatchValidation` — mutually-exclusive arg
  combinations, range validation (`start > end`).

Mocks: continue using `_FakeFetchedTranscript` from step 2; extend with
multi-window fixtures sized to exercise the coalescer's window
boundaries (e.g. ~120s of segments producing 3-4 windows).

## When the yt-dlp fallback helps

The transcript action tries `youtube-transcript-api` first and falls
back to yt-dlp's caption code path on `RequestBlocked`, `IpBlocked`,
or `PoTokenRequired`. The two paths hit the same Innertube endpoint
chain (`watch-page` → `youtubei/v1/player` → `captionTracks[].baseUrl`)
but with materially different request fingerprints, and the fallback's
recovery rate depends on which exception triggered it.

### youtube-transcript-api 1.x (per `_settings.py`, `_transcripts.py`)

- Single fixed Innertube client: `ANDROID 20.10.38`.
- Default `requests` Session with only `Accept-Language: en-US`. User-Agent
  is the bare `python-requests/X.Y.Z` default — no browser or device spoof.
- `PoTokenRequired` is a reactive substring check
  (`if "&exp=xpe" in url:`) — no token generation, no JS engine.
- No multi-client retry. If the player call returns
  `LOGIN_REQUIRED + BOT_DETECTED`, raises `RequestBlocked` and stops.

### yt-dlp (per `_video.py`, `_base.py`)

- Multi-client rotation. Default is `android_vr,web_safari`.
- `android_vr` impersonates Oculus Quest 3:
  `com.google.android.apps.youtube.vr.oculus/1.65.10 (Linux; U; Android
  12L; eureka-user Build/SQ3A.220605.009.A1) gzip`. JS-less,
  `SUBS_PO_TOKEN_POLICY` defaults to `required=False` for this client.
- Detects both `xpe` and `xpv` PoToken experiments on caption URLs.
  When required and unavailable, skips that client's subs and tries
  the next. With a PoToken provider plugin (`bgutil-ytdlp-pot-provider`
  or similar): generates the Botguard token, succeeds.
- Optional `curl_cffi` impersonation defeats TLS fingerprinting.

### Recovery probabilities

| Exception | Cause | Fallback recovery |
|---|---|---|
| `RequestBlocked` | Bot-detection on transcript-api's plain `python-requests` UA + ANDROID client | **Likely**. yt-dlp's android_vr client + Quest 3 UA presents a different fingerprint and typically passes where transcript-api fails. |
| `IpBlocked` | HTTP 429 from the Innertube endpoint | **Possible**. Different request fingerprint helps, but the IP reputation persists; subsequent calls may hit the same wall. |
| `PoTokenRequired` | `xpe`/`xpv` experiment on the caption URL | **Only with a PoToken provider plugin installed.** yt-dlp without a plugin is in the same boat as transcript-api — it detects the same experiment and skips that client's subs. |

The `_FALLBACK_NOTES` map in `youtube.py` carries a short explanation
of each recovery path into the response frontmatter so the LLM caller
sees what was bypassed and why recovery was possible at all.

## Chapter integration

Chapter data comes from yt-dlp's ``info["chapters"]`` array. The
transcript action launches a chapter-fetch task in parallel with the
transcript fetch via ``asyncio.create_task``; the chapter task is
best-effort and degrades silently to ``[]`` on any failure (yt-dlp
unreachable, malformed entries, etc.). Wall-clock cost is dominated by
the slower of the two parallel fetches, so chapters add no measurable
latency.

Chapter titles render in three places:

1. **Frontmatter ``chapters:`` list** on a non-filtered transcript fetch,
   formatted as ``MM:SS Title`` for direct readability.
2. **``## [MM:SS] Title`` headings** in compact-mode body, emitted before
   the first window whose start crosses each chapter boundary. Window
   anchors (``[MM:SS]``) still appear; the heading provides semantic
   structure on top of the structural anchors.
3. **Tantivy ``chapter`` field** on each window doc, tagged with the
   title of the containing chapter. Enables ``chapter=`` filtering via
   a parsed query that composes with ``search=`` and time-range filters
   in a ``BooleanQuery`` MUST chain.

The compact-mode headings only render in the full-transcript view.
Window retrieval (``windows=``) and search results render
non-contiguous slices where mid-list chapter headings would land at
confusing positions; those views suppress the headings entirely. The
frontmatter ``chapters:`` list is still present for chapter discovery.

When ``chapter=`` filters to no matches, the response emits a
frontmatter ``note:`` listing the available chapter titles so the LLM
can retry with a valid filter rather than guessing.

## Deferred to follow-up commits

- **Multi-video index** for cross-corpus search. Per-video index is
  simpler and matches `_PageCache`. Cross-corpus needs a `video_id`
  keyword field and a different cache shape. Defer until a concrete use
  case (e.g. searching an entire playlist for a phrase) lands.
- **On-disk persistence**. RAM-only is sufficient for session-scoped
  research. If a long-running daemon mode emerges, schema versioning
  becomes load-bearing.
- **SaT (`wtpsplit`) for the unpunctuated branch**. Already deferred
  from step 2; the threshold for action is empirical evidence that the
  pause-aware time-window path produces visibly worse search results
  than the sentence-aware path on equivalent content.
- **PoToken plugin slot**. Already deferred. The yt-dlp transcript
  fallback path (step 6) is the gating step.

## Out-of-scope changes signaled by this doc

- This is not the place to revise the four render modes from step 2.
  `compact` / `absolute` / `none` / `structured` continue to apply
  identically to single-window, multi-window, and search-result
  rendering. The renderer is mode-agnostic to the caller's intent.
- This is not the place to change `SEARCH_GRAMMAR_DOC`. Transcript
  search uses the same grammar as the existing slicing pipeline.
