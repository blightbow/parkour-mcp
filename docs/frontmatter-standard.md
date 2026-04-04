# YAML Frontmatter Standard

All MCP tool responses use YAML frontmatter to provide structured metadata
and next-action guidance.  This document defines the conventions so that
all tools remain consistent as the codebase evolves.

## Purpose

Frontmatter serves three roles:

1. **Metadata** — source URL, API origin, pagination totals.
   Gives the LLM structured context about what it just received.
2. **Trust boundary** — the `trust` field marks fenced outputs as
   untrusted external content.  All trusted signals (truncation
   hints, balance warnings, section lists) live in frontmatter,
   never inside the fenced content zone.
3. **Next-action hints** — nudges toward productive follow-up tool
   calls.  These are the primary mechanism for guiding multi-step
   research workflows.

## Rules

- `_build_frontmatter()` (`markdown.py`) is the **sole mechanism**
  for producing `---` blocks.  Never hand-build YAML.
- **Formatters produce body content only.**  Frontmatter is prepended
  by the caller, not by `_format_*` functions.
- **Error strings have no frontmatter.**  They are already
  self-describing.
- **Pagination stays in body text.**  It is content, not metadata.
- **No document-sourced data in frontmatter.**  Page titles, section
  headings, and other values extracted from external content belong
  inside the fenced content zone, not in frontmatter fields.
  Frontmatter is the trusted metadata boundary — placing
  attacker-controlled strings there (e.g. a crafted `<title>` tag)
  allows them to inherit the trust of tool-generated metadata and
  opens a frontmatter injection vector.  See the trust boundary
  discussion in *Purpose* above.

## Hint Field Types

Three distinct fields carry guidance.  Use the right one:

| Field      | Scope              | Purpose                                          | Example |
|------------|--------------------|--------------------------------------------------|---------|
| `hint`     | Same tool          | Operational guidance for what to try next         | `Use paper action with a paper ID for full details` |
| `see_also` | Different tool     | Cross-tool reference to a complementary resource  | `ARXIV:1706.03762v7 with SemanticScholar for citations` |
| `note`     | Explanatory        | Why something is the way it is                    | `Section listing is not applicable for API-sourced paper data` |

`_build_frontmatter()` skips `None` values, so hints that only apply
conditionally can be passed as `None` and will be omitted cleanly.

## List Values

Any frontmatter field can be a list.  `_build_frontmatter()` renders
single-item lists as scalars and multi-item lists as YAML sequences:

```yaml
# Single item — rendered as scalar
warning: HTML full text is not available for this paper

# Multiple items — rendered as list
warning:
  - Fragment could not be resolved
  - footnotes parameter ignored — use footnotes as the sole parameter to retrieve bibliography entries
```

## Parameter Conflicts

When incompatible parameters are combined, the tool picks the
strongest signal and warns about ignored parameters rather than
rejecting the request outright.  This avoids wasting a round-trip.

- `section` + `footnotes`: section extraction runs; footnotes ignored
  with warning
- `search`/`slices` + `footnotes`: search/slices runs; footnotes
  ignored with warning
- `search` + `slices`: mutually exclusive (hard error — no clear
  winner)
- `search`/`slices` + `section`: mutually exclusive (hard error —
  fundamentally different modes)

## Required Fields by Tool

### Fetch tools (`web_fetch_direct`, `web_fetch_sections`, `web_fetch_js`, GitHub fast path)

Always present:

| Field    | Description |
|----------|-------------|
| `source` | Canonical URL |
| `trust`  | Trust advisory for fenced content |

Note: page titles are rendered inside the fenced content zone as a
markdown heading, not in frontmatter.  This prevents attacker-controlled
data from appearing in the trusted metadata block.

Conditional:

| Field              | When |
|--------------------|------|
| `site`             | MediaWiki pages |
| `generator`        | MediaWiki pages |
| `content_type`     | Non-HTML content (json, xml, plain) |
| `truncated`        | Content exceeds `max_tokens` |
| `warning`          | Fragment could not be resolved, parameter conflicts, or other advisory |
| `footnotes_only`   | Footnote-only responses |
| `total_slices`     | BM25 search or slice retrieval |
| `search`           | BM25 search query |
| `matched_slices`   | BM25 search results |
| `slices`           | Slice retrieval indices |
| `hint`             | BM25 search, slice retrieval, and `web_fetch_sections` |
| `note`             | Section extraction depth warning (when subsections exist) |
| `shelf`            | Research shelf tracking status (auto-tracked papers) |
| `api`              | API origin identifier (e.g. `GitHub`, `GitHub (raw)`, `Reddit (.json)`, `arXiv`) |
| `language`         | GitHub blob: file extension (e.g. `py`, `ts`) |
| `definitions`      | GitHub blob via `web_fetch_sections`: count of extracted code definitions |
| `type`             | GitHub issue/PR: `issue` or `pull_request` |
| `state`            | GitHub issue/PR: `open`, `closed`, or `merged` |

### Kagi tools (`kagi_search`, `kagi_summarize`)

Always present:

| Field    | Description |
|----------|-------------|
| `source` | Search query (`kagi search: {query}`) or summarized URL / `text input` |
| `trust`  | Trust advisory for fenced content |

Conditional:

| Field              | When |
|--------------------|------|
| `balance_warning`  | Kagi API balance below $1.00 |

### arXiv tool

**`paper` action:**

| Field      | Description |
|------------|-------------|
| `title`    | Paper title |
| `source`   | Canonical arXiv abs URL |
| `api`      | `arXiv` |
| `full_text`| Pointer to `/html/` URL for full text (only when HTML render exists) |
| `warning`  | Emitted when HTML full text is unavailable (only abstract/metadata included) |
| `see_also` | SemanticScholar cross-reference; mentions body text snippets when HTML unavailable |
| `shelf`    | Research shelf tracking status |

**`search` and `category` actions:**

| Field      | Description |
|------------|-------------|
| `api`      | `arXiv` |
| `action`   | `search` or `category` |
| `query` / `category` | The search query or category code |
| `hint`     | Guidance to use `paper` action or SemanticScholar |

### Semantic Scholar tool

**`paper` action:**

| Field      | Description |
|------------|-------------|
| `title`    | Paper title |
| `source`   | S2 paper URL |
| `api`      | `Semantic Scholar` |
| `see_also` | ArXiv cross-reference (when arXiv ID available) |
| `shelf`    | Research shelf tracking status (or advisory when no DOI available) |

**`search` action:**

| Field   | Description |
|---------|-------------|
| `api`   | `Semantic Scholar` |
| `action`| `search` |
| `query` | Search terms |
| `total` | Total result count |
| `hint`  | Guidance to use `paper` or `snippets` actions |

**`references` action:**

| Field   | Description |
|---------|-------------|
| `api`   | `Semantic Scholar` |
| `action`| `references` |
| `paper` | Source paper ID |
| `hint`  | Pagination guidance (when more pages exist) |

**`author_search` action:**

| Field   | Description |
|---------|-------------|
| `api`   | `Semantic Scholar` |
| `action`| `author_search` |
| `query` | Search terms |
| `total` | Total result count |

**`author` action:**

| Field   | Description |
|---------|-------------|
| `api`   | `Semantic Scholar` |
| `action`| `author` |
| `source`| S2 author URL |

**`snippets` action:**

| Field   | Description |
|---------|-------------|
| `api`   | `Semantic Scholar` |
| `action`| `snippets` |
| `query` | Search terms |
| `paper` | Scoped paper ID (omitted for corpus-wide) |
| `hint`  | Guidance to use `paper` action for metadata |

### GitHub tool

**`repo` action:**

| Field    | Description |
|----------|-------------|
| `source` | GitHub repo URL |
| `api`    | `GitHub` |
| `hint`   | README truncation drill-in guidance (when README exceeds ~2000 tokens) |
| `shelf`  | Research shelf tracking status (from CITATION.cff or repo metadata) |

**`issue` and `pull_request` actions:**

| Field       | Description |
|-------------|-------------|
| `source`    | GitHub issue/PR URL |
| `api`       | `GitHub` |
| `type`      | `issue` or `pull_request` |
| `state`     | `open`, `closed`, or `merged` |
| `truncated` | When comment body exceeds ~5000 tokens |
| `hint`      | Pagination and search/section guidance |
| `trust`     | Trust advisory for fenced content |

**`file` action:**

| Field      | Description |
|------------|-------------|
| `source`   | GitHub blob URL |
| `api`      | `GitHub (raw)` |
| `language` | Detected language from file extension |
| `truncated`| When file exceeds `max_tokens` |

**`search_issues` and `search_code` actions:**

| Field   | Description |
|---------|-------------|
| `source`| GitHub search URL |
| `api`   | `GitHub` |
| `total` | Total result count |
| `hint`  | Pagination guidance (when more pages exist) |

**`tree` action:**

| Field   | Description |
|---------|-------------|
| `source`| GitHub tree URL |
| `api`   | `GitHub` |
| `hint`  | Guidance for file action drill-in |

**GitHub fast path (via fetch tools):**

GitHub URLs intercepted by `web_fetch_direct` and `web_fetch_js` produce
the same frontmatter as the corresponding GitHub tool actions. The fast
path additionally populates the 2Q page cache with presplit content:

- **Blob URLs**: cached with CodeSplitter presplit (AST-aware function/class
  boundaries) for BM25 search within source code
- **Issue/PR URLs**: cached with comment-boundary presplit (one slice per
  `ic_*` or `rc_*` heading) for per-comment BM25 search
- **Repo/tree/gist URLs**: served directly, no cache population needed

`web_fetch_sections` on GitHub URLs returns:

- **Blob**: tree-sitter code definition tree (kind, name, line range, docstrings)
- **Issue**: comment tree with IDs, authors, role badges, timestamps
- **PR**: review comments grouped by file + regular comments
- **Repo/tree/gist**: redirect hint to the GitHub tool
