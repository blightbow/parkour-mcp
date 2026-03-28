# Technical Debt

Acknowledged warnings and deferred fixes. Each entry includes the source, the issue, and why it was deferred.

## Pyright warnings (opted not to fix)

### `fetch_direct.py` — `_matched_meta` not accessed

- **Location**: `_sections_response()`, line ~458
- **Issue**: `_matched_meta` is destructured from the return of `_filter_markdown_by_sections()` but never used.
- **Why deferred**: The variable captures section match metadata (ancestry paths, fragment matches) that may be useful in frontmatter enrichment later. Removing it would discard structured data we'll likely want when section responses gain richer diagnostics. Low-risk dead code in a display-only path.
