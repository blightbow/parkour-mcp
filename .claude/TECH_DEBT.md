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

## Structural tradeoffs

### `<header>` stripped from all pages — loses real h1s on spec docs

- **Location**: `parkour_mcp/markdown.py:44` (`_NOISE_TAGS`) → `_HTMD_SKIP_TAGS` at line 154, passed to htmd's `skip_tags` option.
- **Issue**: `<header>` is decomposed on every page as site chrome. Spec documents (WHATWG HTML Living Standard and likely others) use `<header>` semantically for the document's primary h1 and metadata block, so the real title and subtitle are discarded along with the site-chrome content the strip targets on typical pages.
- **Why deferred**: `<header>` is correctly site-chrome for ~99% of the open web; leaking nav/branding h1s into body output would be a worse default. Fixing the spec-doc case structurally needs either (a) context-sensitive stripping (strip `<header>` at nav depth but not at document root) or (b) a per-site escape hatch. Both are significantly more involved than the affected-page count justifies.
- **Mitigation**: The title ladder falls through to `<title>` / `og:title` via `_extract_head_title` when no h1 survives outside fenced code (see `TestHtmlTitleExtraction`). For WHATWG this yields `"HTML Standard"` from `<title>`. The in-body visual subtitle ("Living Standard — Last Updated…") is still lost but has low information value.
