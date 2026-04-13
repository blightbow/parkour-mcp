# Tool Parameter Naming: The `query` Overload

## Summary

Every dedicated tool in parkour-mcp (`arxiv`, `ietf`, `github`, `discourse`,
`packages`, `semantic_scholar`, `research_shelf`) accepts a single
`query: str` parameter whose semantic meaning varies per `action`.  This
is a codebase-wide convention that trades parameter-schema honesty for
flat-schema simplicity.  It works, but has accumulated enough friction to
be worth a deliberate revisit.

The MediaWiki tool is the first tool to break convention by splitting its
primary input into `title` (page lookup) and `query` (search).  This
document exists to capture the observation so a future normalization pass
has a starting point.

## Current state

| Tool | `query` parameter meaning |
|---|---|
| `ietf` | RFC number / URL / DOI (rfc) · keywords (search) · draft name / URL (draft) · subseries ID (subseries) |
| `arxiv` | arXiv query syntax (search) · arXiv ID / URL (paper) · category code (category) |
| `packages` | `ecosystem/name[@version]` (package/version/dependencies) · `github.com/owner/repo` (project) · advisory ID (advisory) |
| `github` | search string (search_issues/repos/code) · `owner/repo#number` (issue/pull_request) · `owner/repo/path` (file/tree) · `owner/repo` (repo/issue_templates) |
| `discourse` | topic URL (topic) · search string (search) · **ignored** (latest) |
| `research_shelf` | DOI · DOI + score value · format name · JSON string · section name |
| `semantic_scholar` | search string · paper/author ID · multiple other per-action meanings |

Each tool's `query` Field description is a multi-line per-action
documentation block.  The agent learns the meaning by reading the action
description and routing accordingly.

## Why it evolved this way

MCP schemas historically handle conditional-required fields clumsily.
`oneOf`/`anyOf` on required fields is technically supported but LLMs fill
flat schemas more reliably than conditional ones.  A single mandatory
`query` slot produces cleaner tool calls than three mutually-exclusive
optional strings where exactly one must be set per action.  The
convention is a pragmatic workaround for schema-generation and
LLM-reliability tradeoffs that existed when the first few tools shipped.

## The cost

1. **Semantic dishonesty.**  `mediawiki action="page" query="Gödel's
   incompleteness theorems"` reads as "the query is Gödel's incompleteness
   theorems" when really the input is a page title.  The word `query`
   implies search; using it for lookup inputs is misleading.

2. **"Ignored" hacks.**  Actions that take no input (e.g.,
   `discourse action="latest"`) have to document `query` as "ignored
   (pass any value)".  This is a smell: the schema is lying to the caller.

3. **Type erasure.**  Different semantic types (numbers, URLs,
   identifiers, keywords) collapse into `str` and are disambiguated in
   the dispatcher with regex/prefix detection.  Errors are generic
   ("could not parse X from query") instead of specific ("X action
   requires a Y identifier").

4. **Discovery friction.**  An agent reading the MCP schema can't
   determine what input a given action expects without reading the
   multi-line `query` description and cross-referencing against the
   `action` description.

5. **Documentation drift.**  Every new action added to a dedicated tool
   requires updating both the `action` enum description AND the `query`
   description.  The two fields co-evolve but are independently edited,
   which creates subtle inconsistencies over time.

## The MediaWiki departure

The MediaWiki tool ships with `title` and `query` split because its shape
is unusually clean:

- Two of three MVP actions (`page`, `references`) take a page identifier
  (title or URL) → `title: Optional[str]`
- One action (`search`) takes search terms → `query: Optional[str]`
- The dispatcher validates `action` → required-parameter mapping with
  specific error messages

Trade-offs accepted:

- Divergent from the rest of the codebase until a normalization pass
- One additional Optional parameter in the schema
- Naming proximity with the within-page BM25 `search=` parameter
  (disambiguated by action context)

Trade-offs gained:

- `title=` reads honestly for page lookup
- `query=` stays semantically correct for search (matches the rest of
  the codebase's use of `query` for search-shaped inputs)
- Specific per-action error messages replace the "could not parse X"
  generic
- The `discourse`-style "ignored" hack becomes impossible — parameters
  are typed by role

## Normalization path (future work)

A normalization pass would evaluate each tool against this decision
matrix:

| Question | If yes → |
|---|---|
| Do the actions' primary inputs have genuinely different semantic types? | Split |
| Does any action currently document its `query` as "ignored"? | Split (remove the phantom parameter entirely, or replace with a real typed param) |
| Do any actions use regex/prefix-based dispatch inside the handler to figure out what kind of input they received? | Split (move the dispatch to typed parameters) |
| Is the action set small and stable (2-4 actions, all sharing similar input)? | Keep `query` |

### Candidates for clear benefit from a split

- **`discourse`** — `latest` has a phantom `query` parameter; `topic`
  takes a URL, `search` takes keywords.  Clean split: `url` / `query` /
  nothing.
- **`github`** — the input types are already semantically distinct: repo
  selectors, search strings, file paths, issue refs.  A split into
  `repo`, `query`, `path`, `ref` would be more honest than the current
  polymorphic `query`.
- **`packages`** — ecosystem/name/version composite vs. advisory ID vs.
  repo URL are genuinely different.

### Possibly fine as-is

- **`arxiv`** — paper ID and category code are both short opaque tokens;
  they could share a slot without much loss.
- **`ietf`** — RFC numbers, draft names, subseries IDs are all short
  tokens of similar shape.

### Requires care

- **`research_shelf`** — the most overloaded; its `query` includes complex
  compound inputs like "DOI followed by space and integer value".  Any
  normalization would probably require per-action structured params
  (`doi`, `score`, `note_text`, etc.) rather than just a single split.
- **`semantic_scholar`** — many actions; some could share a slot, others
  shouldn't.  Needs its own evaluation.

## Timing and priority

**Low priority.**  The existing `query` convention works — agents fill it
reliably, tests pass, users rarely complain.  This is a cleanliness issue,
not a correctness issue.  The right time to revisit is:

1. When a new action lands that doesn't cleanly fit an existing tool's
   `query` slot (forcing either a wart or a split)
2. When adding a tool where the input types are genuinely heterogeneous
3. As part of a deliberate v2 tool-schema refactor

Recommend deferring until one of those triggers fires.  In the meantime,
new tools should feel free to split — each split is independently
valuable and sets precedent for the eventual normalization pass.

## References

- **MediaWiki tool:** `parkour_mcp/mediawiki.py` — first tool with the
  split (actions `page`, `search`, `references` using `title`/`query`
  distinction)
- **Existing tools using overloaded `query`:** `arxiv.py`, `ietf.py`,
  `github.py`, `discourse.py`, `packages.py`, `semantic_scholar.py`,
  `shelf.py`
