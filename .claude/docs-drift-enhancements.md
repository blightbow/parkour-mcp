# Documentation-drift tooling — code enhancements wishlist

Tracking source changes that would let Cog and Drift do their jobs better
across `CLAUDE.md`, `README.md`, and `docs/`. Add entries when an
introspection helper has to work around a source shape; remove entries when
the upstream change lands.

## Choosing between Cog and Drift

POC findings on which tool wins where the two overlap.  Drift's pre-1.0
schema churn is *not* counted against it; the choice is on functionality.

| Doc claim shape | Tool | Why |
|---|---|---|
| "There are N actions: a, b, c" | **Cog** | Cog regenerates the correct count *and* the list. Drift would flag that `_VALID_ACTIONS` changed but leave the prose wrong; you would still need to edit by hand. |
| "github.py is ~1600 LOC" | **Cog** | Drift has no concept of file size. Cog reads `Path.open()` line count. |
| "7 ecosystems: pypi, npm, …" | **Cog** | Same as actions: derived value, regenerable. |
| Generated CLI `--help` block | **Cog** | Same shape: derive once, regenerate. |
| "`_build_frontmatter()` is the sole producer of `---` blocks" | **Drift** | Behavioral assertion, not derivable. Drift fires when `_build_frontmatter`'s body changes meaningfully. |
| "Untrusted content is wrapped by `_fence_content()`" | **Drift** | Same: anchor prose to function, signal change in behavior. |
| "`FMEntries.__setitem__` raises `TypeError` on protected keys" | **Drift** | Anchored to `FMEntries`. If someone weakens the guard, `drift check` flags the docstring claim. |
| Decorator-only changes (e.g. adding `@property`) | **Cog** with `inspect.signature` | Drift's tree-sitter query does not cover `decorated_definition` (see Drift section above), so decorator changes are silent to it. |

Heuristic: **does the claim contain a value derived from source?**  If yes,
Cog generates it.  If no, Drift gates whether the claim is still factual.

## Cog: source shapes that resist clean introspection

### `packages.py`, `discourse.py`, `ietf.py` — actions dispatched inline

- **Source today**: handlers do `if action == "x": ... elif action == "y": ...`.
  No module-level `_VALID_ACTIONS` tuple to import.
- **Why it matters**: `scripts/cog_helpers.action_list()` can pull
  `_VALID_ACTIONS` from `github.py` directly. The other three modules need
  their action list either hardcoded in the cog block (defeats the point)
  or AST-parsed from the dispatch chain (fragile). Result: the action
  counts in CLAUDE.md/README.md for these three modules stay manual.
- **Fix**: introduce `_VALID_ACTIONS = (...)` at module scope mirroring
  `github.py`'s pattern, and consume from the dispatch and the tool-arg
  description.

### `__init__.py` — `tools` list is local to `main()` ✓ resolved

The registration list is now hoisted to module scope as
`_ALWAYS_ON_TOOLS` (tuple of `(internal_name, callable)` pairs) and
`_OPTIONAL_TOOLS` (tuple of internal names; SemanticScholar is the
only one, lazy-imported inside `main()` only when `S2_ACCEPT_TOS` is
set so opt-out installs don't pay for the S2 client).
`cog_helpers.tool_count()` now imports the constants instead of
slicing source; `scripts/check_manifest_tools.py` diffs the resolved
profile-mapped names against `manifest.json`.

## Drift: upstream limitations affecting our coverage

### Inline `@./` references are not implemented

- **What the docs say**: drift's README describes writing
  `@./src/file.ts#Symbol` in a doc body and having `drift link` stamp it.
  The vendored skill pinned at `b96d58f` says the same.
- **What the binary does**: nothing. PR #18 (`a3cfbbb`, 2026-04-10)
  removed the inline scanner along with frontmatter anchors, and v0.8.0
  onward contains no `@./` handling. Upstream corrected its skill in #32
  but not the README. A doc whose only anchors are inline reports `ok`.
- **Consequence here**: a backticked `path#Symbol` citation is plain text
  to drift, so `scripts/check_doc_citations.py` resolves those instead
  (existence only, not staleness). Bind prose that should go stale when
  code changes with `drift link <doc> <target>`.
- **Do not write `@./` in CLAUDE.md** even inside a code span for drift's
  sake: outside a code span it is a Claude Code import and loads the whole
  file into context, and inside one it does nothing for drift.

### Python decorator changes are silent

- **Drift fingerprint scope**: `src/queries/python.scm` matches
  `function_definition` and `class_definition` only. The wrapping
  `decorated_definition` is outside the fingerprinted subtree.
- **Effect**: adding or removing `@property`, `@asynccontextmanager`,
  `@pytest.fixture`, etc. above an unchanged function body does **not**
  trigger `drift check`. parkour_mcp uses these heavily.
- **Mitigation upstream**: a one-line addition to the .scm query to
  match `decorated_definition` (and walk inward to capture the inner
  symbol name). Worth filing as an upstream issue against
  `fiberplane/drift`.
- **Mitigation locally**: when prose is anchored to a decorated symbol
  whose decorator semantics matter (e.g. `@property` that defines public
  surface), prefer Cog with `inspect.signature` introspection, which sees
  the decorated form, over Drift.

## Project hygiene that supports drift gating

### `manifest.json` tool list synced via check script

`manifest.json`'s `tools` array is hand-curated for description prose
(Claude Desktop's tool picker shows these to end users) but the *names*
must match the union of `_ALWAYS_ON_TOOLS` and `_OPTIONAL_TOOLS`.
`scripts/check_manifest_tools.py` diffs them and exits 1 on drift; the
`just docs-drift` recipe and the GHA workflow run it alongside cog and
drift checks.  JSON has no comment syntax so cog can't operate inline,
but the check closes the loop on the most error-prone form of drift
(missing or stale tool names) without forcing description prose into
Python.

## Validated patterns

### Cog-in-markdown-tables: outer BEGIN/END markers (Variant D)

GFM spec §4.10 ("Tables (extension)") states: *"The table is broken at
the first empty line, or beginning of another block-level structure."*
HTML-comment blocks (CommonMark §4.6 type 2) are block-level, so any
`<!-- ... -->` placed inside a GFM table terminates the table at that
point.  cmark-gfm, pulldown-cmark, marked, remark-gfm, and
commonmark-java all enforce this identically — there is no popular
renderer that handles it differently.

The only working pattern is **outer-marker injection**: `<!-- [[[cog
... ]]] -->` and `<!-- [[[end]]] -->` flank the entire table from the
outside, and cog regenerates the whole table — header, separator row,
and data rows — as a single block.  This is the same shape adopted by
`terraform-docs --output-mode inject`, `markdown-magic`, `embedme`,
and Ned Batchelder's own profile README.  See
`scripts/cog_helpers.render_tool_table()` for the implementation.

### Tabulate output trimmed to the docs/ aesthetic via a post-processor

`tabulate(..., tablefmt="github")` pads every column — including the
last — to the widest cell, producing a wall of trailing whitespace
before the closing pipe on every row.  The hand-written tables in
`docs/` use a different convention: internal columns aligned across
rows, last column sized to its header, data rows ragged.  Source
readability matters because this project's markdown is read by humans,
agents, and `cat` alike, not just rendered to HTML.

`scripts/cog_helpers._human_align()` rewrites tabulate output to match.
Not a library feature anywhere — tabulate#392 (closed wontfix) and
prettier#12074 (open since 2022) are the upstream threads where this
exact ask was made and declined; both maintainers explicitly recommend
post-processing.

### Width-adaptive table rendering

Two table-format aesthetics serve different cases.  Uniform-padded grid
(closing pipes align vertically) reads sharpest when every row fits within
the viewer's line width.  Ragged-last-column avoids the wall of trailing
whitespace that lands when one long row dominates everyone else's pad.
Neither is universally optimal — the choice should follow the data.

`scripts/cog_helpers.render_table_adaptive()` picks per table at render
time: tabulate's uniform output for narrow tables (max row width ≤
threshold), the `_human_align` ragged-last variant for wide ones.
Default threshold 120 — the print-width shared by Black, JetBrains, and
`glow`.  In this repo: README's tool table lands wide (~250-char rows,
ragged); `docs/frontmatter-standard.md`'s protected-keys table lands
narrow (~98-char rows, uniform).

Endorsed but never landed upstream: `wooorm` proposed exactly this
chooser in `remarkjs/remark-gfm#46` (Jan 2023): *"There are currently
two ways to display tables.  I can see 'dynamically' switching between
them as an improvement."*  The two display modes already exist as the
`alignDelimiters` boolean in `markdown-table`; the heuristic is the
new piece.

### Drift relink-gate after AST-changed-but-behavior-same edits

When a code change alters an anchored symbol's AST (adding a class
attribute, reordering methods, splitting a function body) without
changing its semantic meaning, `drift check` reports STALE — the AST
fingerprint moved even though the prose is still accurate.  The
relink-gate exists for exactly this case:

1. `drift link <doc-path>` — refuses, prints the doc section and
   current code side-by-side for review.
2. Read both; confirm the doc's prose is still factual.
3. `drift link <doc-path> --doc-is-still-accurate` — refreshes the
   `sig:` hash in `drift.lock`.
4. `drift check` — verifies clean.

Skip the review step at your peril.  Drift can't tell a cosmetic
refactor from a real behavioral edit; the gate forces explicit ack
that the prose still describes the code.  We exercised this when
hoisting `FMEntries.PROTECTED_ORDER` — the class's AST changed (new
attribute) but its protected-keys behavior didn't.

### Tabulate + a TOML registry collapses the description-ergonomics tax

Earlier draft: "cog forces description prose out of the README into a
Python list of tuples."  Resolved by reading per-tool prose from
`scripts/tools.toml` and rendering with `tabulate(..., tablefmt="github")`.
TOML is editable by anyone; doc-only contributors never touch Python.
Tabulate also computes column widths from data, so a longer tool name
won't silently break alignment.  All other markdown tables in `docs/`
already use bordered GFM (`| col | col |`), so adopting tabulate's
default format converged the README onto project convention rather
than diverging from it.

## Out-of-repo drift (vendored content)

### Vendored Claude Code skills tracked via Renovate

- **State**: `.claude/skills/drift/SKILL.md` is vendored from
  `fiberplane/drift` (MIT). The pinned upstream SHA lives in a
  `renovate: ...` HTML comment at the top of the file alongside
  human-readable attribution.
- **Mechanism**: Renovate's `customManagers` regex parses the
  annotation and watches `main` of the upstream repo via the
  `git-refs` datasource.  When upstream advances past the pinned
  digest, Renovate opens a PR updating the digest; we re-read the
  upstream SKILL.md and either accept the bump (re-vendor the
  content) or close the PR (decline the upstream change for now).
- **Config**: `renovate.json` at repo root, scoped via
  `enabledManagers: ["custom.regex"]` so the bot only watches our
  skill annotations and ignores everything else.
- **`git-refs` needs a full URL**: the datasource runs `git ls-remote`
  on `packageName`, which must be a fully qualified URL. The
  annotation's `depName` is the short `owner/repo`, so the manager's
  `packageNameTemplate` expands it to `https://github.com/<owner>/<repo>`.
  Without that template every lookup fails.
- **Failures surface on the Dependency Dashboard issue.**
  `dependencyDashboard` is enabled because nothing else reports a
  failed lookup: without it the only record is the job log on
  developer.mend.io, and the manager can fail on every run while the
  repo shows no activity at all. A silent bot looks the same as a bot
  with nothing to update; check the dashboard issue, not the absence of
  PRs.
- **Validating locally**: the field name `managerFilePatterns` is
  Renovate v38+, and older versions reject the config with "disallowed
  fields". `npx renovate@<version> --platform=local --dry-run=lookup`
  runs the real lookup against the working tree and reports the digest
  the bot would propose, which schema validation alone does not
  exercise.
- **Why not the other shapes**: `simonw/skills/cogapp-markdown` was
  considered and dropped — repo has no LICENSE file, so vendoring is
  legally murky regardless of the skill's quality. The cog
  conventions are simple enough to live in `cog_helpers.py` plus the
  CLAUDE.md cog blocks themselves.
- **Why an in-file annotation, not git-native metadata**: `git notes`
  attach metadata out-of-band but don't push by default, so the
  metadata silently desyncs across clones. Commit trailers (e.g.
  `Vendored-Sha: ...`) survive but require finding the introducing
  commit to read the pin. `.gitattributes` is for transformation
  hints, not arbitrary scalars. `git subtree` is whole-directory and
  noisy in history. The Parquet-style "metadata next to data"
  pattern (a comment block at the top of the file) co-locates the
  pin with the content it describes; one read instead of two; humans
  see it on `cat`, machines parse it with a regex; survives forks
  and history rewrites. Renovate, vendir, and peru all converge on
  this shape.

## Future cog/drift candidates

### Tables in `docs/frontmatter-standard.md`, `docs/guide.md`, `docs/query-parameter-overload.md`

`docs/` contains ~10 GFM tables across three files (frontmatter rules,
example response shapes, query-parameter-overload taxonomy).  Some are
descriptive prose and not drift candidates; some encode facts that
would benefit from cog derivation:

- `docs/frontmatter-standard.md` field tables (`hint`/`see_also`/`note`/
  `warning`/`alert` semantics) describe behavior anchored to
  `_build_frontmatter` and `FMEntries` — those symbols are already
  drift-anchored in `drift.lock`, but the *table cells* describing
  protected-key behavior would also drift if a new key joins the
  family.  Worth cogging once we have a constants source for the
  multi-contributor key list.
- `docs/query-parameter-overload.md` per-tool query-shape table is
  pure documentation of dispatch logic; would benefit from the same
  `_VALID_ACTIONS` hoists already tracked above.

Adopting cog there is a follow-up; the helper plumbing
(`render_tool_table`, tabulate, TOML registry) generalizes.

## Anti-patterns to avoid

The shared shape: ask of any drift gate, *what is the cheapest action
that makes this pass, and is that action sufficient?*  Gates whose
cheapest passing action also happens to be the right thing are
satisficing-resistant.  Gates whose cheapest pass is `touch` or
"acknowledge and dismiss" are not.

- **Recency-only drift gates** — assertions like "doc must be modified
  within N days" can be satisfied by `touch docs/foo.md` or any
  no-op edit.  They produce the cognitive exercise of a check (the
  agent goes through the motions) without the reality exercise of
  one (nothing was verified against source).  Use AST-fingerprint
  consistency checks (Drift's `sig:`) or content-derived equality
  (`cog --check`) instead — both fail unless the source actually
  matches the prose.
- **Line-number references in code citations.**  `path:line` rots on
  the first edit; CLAUDE.md enforces `path#Symbol` repo-wide.
- **Inline cog markers inside markdown table cells.**  GFM spec §4.10
  ("the table is broken at the first empty line, or beginning of
  another block-level structure") forbids it — HTML-comment blocks
  are block-level structures.  Use outer BEGIN/END markers flanking
  the entire table; cog regenerates the whole table as a unit.
- **Whole-document reformatters (`prettier`, `mdformat` default) for
  table-only fixes.**  They reformat all prose and would re-pad the
  cog-trimmed tables that `_human_align` deliberately trimmed.  Use
  targeted helpers, not document-wide tools.
- **Git-native metadata (`git notes`, commit trailers,
  `.gitattributes`, `git subtree`) for vendor-source pinning.**  None
  survive the round trip cleanly.  Co-locate the pin with the file
  as an HTML comment block (Renovate, vendir, peru all converge on
  this shape).
- **Trusting agent reports of upstream state without verification.**
  Agents condense; the condensation can be wrong (misclaimed
  Apache-2.0 license, stale Renovate field name, regex syntax that
  differs between Python and re2).  Verify with `gh api`,
  `npx <tool> --version`, or render output locally before committing
  to a claim about upstream.

## Tooling we have not adopted yet but would help

- **lychee** for broken intra-repo links and dead `@./` references.
  Solves a different problem from Renovate's vendor tracking
  (existence vs upstream-drift); they complement each other.
- **markdownlint custom rule** rejecting `:\d+` line-number anchors in
  fenced code references — the satisficing-resistant guard from the
  reference doc's "Both projects" recommendation.
