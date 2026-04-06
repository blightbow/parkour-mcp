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
- **Frontmatter dicts are not transport vehicles.**  Only values that
  belong in the final frontmatter output should be placed in the
  `fm_entries` dict.  Do not use the dict to shuttle data (e.g. page
  titles) to downstream functions that will pop it out for other
  purposes.  If a value needs to reach multiple consumers, pass it
  as a separate parameter.  This prevents accidental leakage of
  untrusted data into frontmatter when a pop step is missed.

## Content Fencing

Untrusted content (fetched web pages, API responses, user-generated text)
is wrapped by `_fence_content()` (`markdown.py`) with box-drawing
delimiters and per-line `│` provenance marking.  This is a datamarking
defense (see Microsoft Spotlighting) that provides a continuous trust
signal throughout the content.

### Protections

- **Separator lines** — empty `│` lines are inserted immediately inside
  both fence boundaries (`┌─` and `└─`).  This prevents content that
  contains fence marker strings from visually connecting to the real
  fence delimiters.
- **Label sanitization** — page titles and section names rendered inside
  fences are passed through `_sanitize_label()`, which replaces
  non-printable characters (newlines, tabs, control codes) with spaces
  using `str.isprintable()`.  This prevents structure injection via
  crafted headings.  Applied at two choke points:
  - `_fence_content(title=)` — sanitizes before rendering as `# {title}`
  - `_extract_sections_from_markdown()` — sanitizes heading names at
    extraction time, protecting all downstream consumers (section lists,
    ancestry breadcrumbs)
- **Title outside frontmatter** — page titles are passed as a separate
  `title` parameter to `_process_markdown_sections()` and rendered
  inside the fenced zone, never in frontmatter.  Functions that build
  frontmatter must not include untrusted titles in the `fm_entries` dict.

### Fenced output structure

```
┌─ untrusted content
│
│ # Page Title (sanitized)
│
│ content line 1
│ content line 2
│
└─ untrusted content
```

## SSRF Protection

Outbound HTTP fetches in `fetch_direct.py` and `fetch_js.py` are guarded
by `check_url_ssrf()` (`common.py`), which resolves hostnames and checks
all addresses against private/loopback/reserved/link-local ranges (IPv4
and IPv6).  This prevents the MCP server from being used to probe
internal networks or cloud metadata endpoints.

- Applied before all generic HTTP fetches (after fast-path detection, so
  API-backed sources like GitHub and arXiv are unaffected)
- Set `MCP_ALLOW_PRIVATE_IPS=1` to disable for local network crawling
- DNS resolution uses `socket.getaddrinfo()` with `AF_UNSPEC` to cover
  both IPv4 and IPv6
- DNS failures pass through — httpx reports the error naturally

Playwright (`fetch_js.py`) additionally blocks cross-origin navigation
after the initial page load via `page.route()`, preventing JavaScript
redirects from steering the browser to internal services.

## Hint Field Types

Four distinct fields carry guidance.  Use the right one:

| Field      | Scope              | Purpose                                          | Example |
|------------|--------------------|--------------------------------------------------|---------|
| `hint`     | Same tool          | Operational guidance for what to try next         | `Use paper action with a paper ID for full details` |
| `see_also` | Different tool     | Cross-tool reference to a complementary resource  | `ARXIV:1706.03762v7 with SemanticScholar for citations` |
| `note`     | Explanatory        | Why something is the way it is                    | `Section listing is not applicable for API-sourced paper data` |
| `alert`    | Precautionary      | Retroactive invalidation of prior output          | `retracted 2020-06-05 — notice: 10.1016/S0140-6736(20)31324-6 (retraction-watch)` |

`_build_frontmatter()` skips `None` values, so hints that only apply
conditionally can be passed as `None` and will be omitted cleanly.

### `alert` vs. `note` — intentional separation

`note:` is additive/informational — it explains why something is the
way it is.  `alert:` is reserved for the rare case where tool output
**retroactively changes the trustworthiness of prior output in this
session OR calls in-flight synthesis into question.**  Keeping `alert:`
rare preserves its signal value; ordinary tool advice belongs on
`note:` so `alert:` remains load-bearing.

Inclusion criteria:

- **Retraction** of a paper the agent is inspecting → `alert:` (the
  fact) plus `note:` (the side-effect, e.g. "tracked in retracted shelf
  bucket").
- **Expression of concern** → `alert:` with softer wording ("validity
  called into question").
- **Correction** → stays on `note:` (amends, does not invalidate).
- Future additions require the same retroactive-invalidation semantic;
  this is not a bucket for loud warnings generally.

The value in `alert:` uses only structurally-validated fields (dates
constrained to ISO format, DOIs regex-checked, source values from a
closed enum).  Free-form text from external APIs (e.g. CrossRef's
`label` field) is rendered inside the content fence, never in `alert:`.

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
| `alert`    | Retraction or expression-of-concern notice from CrossRef (conditional) |
| `note`     | Shelving side-effect (retracted bucket routing) or correction notice (conditional) |
| `relation` | CrossRef preprint↔version linkage (conditional) |
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
| `alert`    | Retraction or expression-of-concern notice from CrossRef (conditional) |
| `note`     | Shelving side-effect (retracted bucket routing) or correction notice (conditional) |
| `relation` | CrossRef preprint↔version linkage (conditional) |
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

### DOI fast path (via fetch tools)

`doi.org` URLs intercepted by `web_fetch_direct`/`web_fetch_js` resolve
via content negotiation (CSL-JSON) plus CrossRef REST enrichment.  arXiv
DOIs are delegated to the arXiv handler; RFC DOIs (`10.17487/RFC{N}`)
are delegated to the IETF handler.

| Field      | Description |
|------------|-------------|
| `title`    | Paper title from CSL-JSON |
| `source`   | Canonical `https://doi.org/{doi}` URL |
| `api`      | `DOI` |
| `trust`    | Trust advisory for fenced content |
| `alert`    | Retraction or expression-of-concern notice from CrossRef (conditional) |
| `note`     | Shelving side-effect (retracted bucket routing) or correction notice (conditional) |
| `relation` | CrossRef preprint↔version linkage (conditional) |
| `see_also` | SemanticScholar cross-reference for citation counts and references |
| `shelf`    | Research shelf tracking status |

### IETF tool

**`rfc` action:**

| Field        | Description |
|--------------|-------------|
| `title`      | RFC title |
| `source`     | Canonical RFC Editor URL |
| `api`        | `IETF (RFC Editor)` |
| `status`     | RFC status (e.g. `INTERNET STANDARD`, `PROPOSED STANDARD`, `INFORMATIONAL`, `UNKNOWN`) |
| `doi`        | Native RFC DOI (`10.17487/RFC{N}`) |
| `full_text`  | Pointer to `.html` URL for full text with search/slices |
| `see_also`   | SemanticScholar cross-reference for citation data |
| `subseries`  | STD/BCP/FYI membership label (conditional, from `see_also` field) |
| `obsoletes`  | List of RFCs this one obsoletes (conditional) |
| `obsoleted_by` | List of RFCs that obsolete this one (conditional) |
| `updates`    | List of RFCs this one updates (conditional) |
| `updated_by` | List of RFCs that update this one (conditional) |
| `note`       | "RFC predates the current status system" when `pub_status` is `UNKNOWN` (conditional) |
| `shelf`      | Research shelf tracking status |

**`search` action:**

| Field          | Description |
|----------------|-------------|
| `api`          | `IETF (Datatracker)` |
| `action`       | `search` |
| `query`        | Search terms |
| `total_results`| Total result count |
| `hint`         | Guidance to use `rfc` action for full details |

**`draft` action:**

| Field    | Description |
|----------|-------------|
| `title`  | Draft title |
| `source` | Datatracker URL |
| `api`    | `IETF (Datatracker)` |
| `state`  | IESG or document state |
| `see_also` | Pointer to archived HTML for full text (conditional) |

**`subseries` action:**

| Field          | Description |
|----------------|-------------|
| `source`       | RFC Editor info page URL |
| `api`          | `IETF (BibXML)` |
| `subseries`    | Subseries label (e.g. `BCP 14`, `STD 97`) |
| `member_count` | Number of constituent RFCs |
| `see_also`     | Guidance to use `rfc` action for member details |

**IETF fast path (via fetch tools):**

`rfc-editor.org` and `datatracker.ietf.org` URLs intercepted by
`web_fetch_direct` produce the same frontmatter as the corresponding
IETF tool actions.  RFC DOIs (`10.17487/RFC{N}`) resolved via `doi.org`
are delegated to the IETF handler rather than generic DOI content
negotiation, preserving relationship chains and subseries metadata.

### Packages tool (deps.dev)

**`package` action:**

| Field             | Description |
|-------------------|-------------|
| `source`          | deps.dev package URL |
| `api`             | `deps.dev` |
| `ecosystem`       | Display label (e.g. `PyPI`, `Cargo`, `npm`) |
| `default_version` | Current default/stable version |
| `versions`        | Total version count |
| `note`            | Deprecation or advisory warning (conditional) |
| `hint`            | Guidance to use `dependencies` action |
| `see_also`        | Guidance to use `project` action for OpenSSF Scorecard (conditional) |

**`version` action:**

| Field       | Description |
|-------------|-------------|
| `source`    | deps.dev version URL |
| `api`       | `deps.dev` |
| `ecosystem` | Display label |
| `advisories`| Count of known security advisories |
| `note`      | Advisory count warning (conditional) |
| `hint`      | Guidance to use `advisory` or `dependencies` action |
| `see_also`  | Guidance to use `project` action for OpenSSF Scorecard (conditional) |

**`dependencies` action:**

| Field            | Description |
|------------------|-------------|
| `api`            | `deps.dev` |
| `ecosystem`      | Display label |
| `action`         | `dependencies` |
| `package`        | `name@version` |
| `direct_deps`    | Count of direct dependencies |
| `transitive_deps`| Count of transitive dependencies |
| `hint`           | Guidance to use `version` action for dependency details |

**`project` action:**

| Field            | Description |
|------------------|-------------|
| `source`         | GitHub project URL |
| `api`            | `deps.dev` |
| `action`         | `project` |
| `scorecard_score`| OpenSSF Scorecard overall score (e.g. `8.2/10`) (conditional) |
| `hint`           | Guidance to use GitHub tool for README/issues |

**`advisory` action:**

| Field    | Description |
|----------|-------------|
| `api`    | `deps.dev` |
| `action` | `advisory` |
| `source` | OSV vulnerability URL |
| `hint`   | Guidance to use `version` action to check affected versions |

All Packages tool actions fence their body content because upstream
fields (`deprecatedReason`, `description`, link URLs) originate from
package contributors and are potential injection vectors.

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

**`commit` (fast path only):**

| Field    | Description |
|----------|-------------|
| `source` | GitHub commit URL |
| `api`    | `GitHub` |
| `type`   | `commit` |
| `trust`  | Trust advisory for fenced content |

**`compare` (fast path only):**

| Field    | Description |
|----------|-------------|
| `source` | GitHub compare URL |
| `api`    | `GitHub` |
| `type`   | `compare` |
| `status` | `ahead`, `behind`, `diverged`, or `identical` |
| `trust`  | Trust advisory for fenced content |

**`wiki` (fast path only):**

| Field    | Description |
|----------|-------------|
| `source` | GitHub wiki page URL |
| `api`    | `GitHub (wiki)` |
| `trust`  | Trust advisory for fenced content |

**`releases` (fast path only):**

| Field    | Description |
|----------|-------------|
| `source` | GitHub releases URL |
| `api`    | `GitHub` |
| `type`   | `releases` (list) or `release` (single tag) |
| `hint`   | Drill-in guidance for specific release tags (list view only) |
| `trust`  | Trust advisory for fenced content |

**`org` / `user` (fast path only):**

| Field    | Description |
|----------|-------------|
| `source` | GitHub org/user profile URL |
| `api`    | `GitHub` |
| `type`   | `organization` or `user` |
| `trust`  | Trust advisory for fenced content |

**GitHub fast path (via fetch tools):**

GitHub URLs intercepted by `web_fetch_direct` and `web_fetch_js` produce
the same frontmatter as the corresponding GitHub tool actions. The fast
path additionally populates the 2Q page cache with presplit content:

- **Blob URLs** (`/blob/`, `raw.githubusercontent.com`): cached with
  CodeSplitter presplit (AST-aware function/class boundaries) for BM25
  search within source code. Line anchor fragments (`#L45`, `#L45-L100`)
  slice output to the requested range with a `lines:` frontmatter field.
- **Issue/PR URLs**: cached with comment-boundary presplit (one slice per
  `ic_*` or `rc_*` heading) for per-comment BM25 search
- **Wiki URLs** (`/wiki/{page}`): raw markdown fetched from the wiki git
  repo; root URL defaults to the Home page
- **Commit/compare URLs**: rendered via Commits/Compare API with stats
  and file lists
- **Release URLs**: list or single-tag via Releases API with notes and
  asset download counts
- **Org/user profile URLs** (`github.com/{name}`): rendered via Orgs or
  Users API with recently active repos
- **Repo/tree/gist URLs**: served directly, no cache population needed
- **Unsupported paths** (`/blame`, `/actions`, `/projects`):
  return descriptive errors instead of falling through to HTML

`web_fetch_sections` on GitHub URLs returns:

- **Blob**: tree-sitter code definition tree (kind, name, line range, docstrings)
- **Issue**: comment tree with IDs, authors, role badges, timestamps
- **PR**: review comments grouped by file + regular comments
- **Repo/tree/gist**: redirect hint to the GitHub tool
