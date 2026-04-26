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

### `__init__.py` — `tools` list is local to `main()`

- **Source today**: the registered-tool list is a local variable inside
  `main()` (line ~488), not a module-level constant.
- **Why it matters**: `cog_helpers.tool_count()` reads the file as text
  and slices on a substring marker. Fragile — any reformat near the
  `tools = [...]` line breaks it.
- **Fix**: hoist the registration list to module scope as
  `_REGISTERED_TOOLS = (...)`, with the optional SemanticScholar entry
  computed separately so the gate stays explicit.

## Drift: upstream limitations affecting our coverage

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

### `CLAUDE.md` references `claude/` but the dir is `.claude/`

- **Where**: `CLAUDE.md` line 174 points to `@./claude/TECH_DEBT.md`.
- **Reality**: the file is at `.claude/TECH_DEBT.md`. The `@./` path does
  not resolve, so Claude does not auto-load the tech-debt doc.
- **Fix**: one-character edit (`claude/` → `.claude/`). Tracked here
  because exactly this kind of broken-path drift is what `lychee` would
  catch in CI; adding lychee to the docs-drift gate would prevent
  recurrence.

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
- **Validator drift caveat**: the field name `managerFilePatterns`
  is Renovate v38+. Older `npx renovate@<38` will reject the config
  with "disallowed fields"; pin to `npx renovate@latest` when
  validating locally. The GitHub App always runs the latest, so
  production validation is fine.
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

## Tooling we have not adopted yet but would help

- **lychee** for broken intra-repo links and dead `@./` references.
  Solves a different problem from Renovate's vendor tracking
  (existence vs upstream-drift); they complement each other.
- **markdownlint custom rule** rejecting `:\d+` line-number anchors in
  fenced code references — the satisficing-resistant guard from the
  reference doc's "Both projects" recommendation.
