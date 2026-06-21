# Kagi query presets

A **query preset** is a reusable, locally-defined bundle of search operators
and filters that the search tool applies on top of a caller's query. It is
parkour's answer to a recurring need that Kagi lenses only partly serve.

## Why presets exist (vs. Kagi lenses)

A Kagi *lens* is a stored search profile, but it lives server-side and its
site allow/deny list is **capped at 10 sites**. An operator-based site list
(`site:a OR site:b OR ...`) has no such cap. A preset stores that operator
block, plus the structured filters, in a local file you control, version, and
share. Presets are similar to lenses but local, and their `site:` list is
unbounded.

Presets and lenses compose: a preset can also pin a `lens_id`, and an
explicit `lens_id` argument still works alongside a preset. Note that a
preset's `site:` fragment intersects (ANDs) with any active lens, so a
conflicting pair can return empty; the response frontmatter warns when both
are in play.

## The registry file

A single YAML file maps preset slugs to definitions. Its location follows the
config-dir resolver (`parkour_mcp/common.py#app_config_dir`):

- Linux / macOS: `~/.config/kagi_presets.yaml` under the parkour config dir
  (`~/.config/parkour/kagi_presets.yaml`), honoring `XDG_CONFIG_HOME`.
- Windows: `%APPDATA%\parkour\kagi_presets.yaml`.
- Override the whole config dir with `PARKOUR_CONFIG_DIR`.

The `kagi://presets` MCP resource prints the exact resolved path for the
current machine, so there is no need to guess it cross-platform.

```yaml
rust-blogs:
  name: Rust ecosystem blogs        # optional human label
  fragment: 'site:without.boats OR site:fasterthanli.me OR site:matklad.github.io'
  region: us                        # optional structured filters
  after: '2024-01-01'
  before: null
  lens_id: null
  workflow: null

infosec:
  fragment: 'site:krebsonsecurity.com OR site:schneier.com'
```

### Fields

| Field | Meaning |
|---|---|
| `name` | Optional human label, shown in the `kagi://presets` listing and the applied-preset note. |
| `fragment` | Query-operator block prefixed to the caller's query. Typically a `site:` allow-list, but any operator syntax is allowed. |
| `region` / `after` / `before` / `lens_id` / `workflow` | Structured filters, identical in meaning and validation to the search tool's own arguments. |

Every field is optional. A fragment-only preset is a pure operator bundle; a
filter-only preset is a reusable filter set.

## Seeding a starter file

You do not have to write the registry from scratch. `parkour-mcp --init`
(`parkour_mcp/kagi.py#seed_presets_file`) creates the config dir (mode `0700`,
via `parkour_mcp/common.py#ensure_dir`) and writes a commented
`kagi_presets.yaml` template if one does not already exist, then prints the
path and exits. It **never** overwrites an existing registry, so it is safe to
re-run.

`--init` is reachable on every install type:

- **CLI / dev installs:** `parkour-mcp --init` (the console script), or
  `python -m parkour_mcp --init`.
- **Claude Desktop / `.mcpb` bundle:** the extension is an on-disk uv project
  with its own venv at
  `~/Library/Application Support/Claude/Claude Extensions/local.mcpb.<author>.<name>/`
  (`%APPDATA%\Claude\Claude Extensions\` on Windows), so the same command runs
  against the bundle's interpreter. The Claude Desktop UI has no affordance for
  it, but a terminal does. The `kagi://presets` resource prints the exact
  command for the running install (it interpolates `sys.executable`, so the
  interpreter path is correct without guessing) and also renders the starter
  template copy-paste-ready for hand-editing.

The template lives in one place (`parkour_mcp/kagi.py#_PRESETS_TEMPLATE`), so
the written file and the displayed one never drift. The seeded template is
entirely commented, so it activates no presets until you uncomment an example
or add your own.

## How a preset is applied

Use the search tool's `preset` argument
(`parkour_mcp/kagi.py#search`). Two things happen:

1. **Fragment assembly** (`parkour_mcp/kagi.py#_assemble_preset_query`). The
   fragment is prefixed to your query. If the fragment carries a top-level
   `OR`, it is parenthesized first (`parkour_mcp/kagi.py#_group_fragment`) so
   the alternatives bind tighter than the implicit AND with your terms. This
   reuses the same depth scanner that powers the ungrouped-OR warning
   (`parkour_mcp/kagi.py#_has_ungrouped_or`), so a fragment that is already
   grouped is not double-wrapped, and your own bare `OR` is still flagged.

   ```text
   fragment: site:a OR site:b
   query:    async runtime
   sent:     (site:a OR site:b) async runtime
   ```

2. **Filter merge** (`parkour_mcp/kagi.py#_merge_preset_filters`). For each
   structured field, an explicit argument wins; otherwise the preset's value
   fills the gap. When an explicit argument overrides a differing preset
   value, the response frontmatter emits a `warning` naming the overridden
   fields, so precedence is never silent.

The frontmatter `source` line shows your original query with a
`(preset 'slug')` marker; a `note` summarizes what the preset contributed
(`parkour_mcp/kagi.py#_preset_note`).

## Validation

Loading is strict and the errors are user-facing
(`parkour_mcp/kagi.py#_load_presets`):

- A missing file is not an error; it simply means no presets are defined.
- Malformed YAML, a non-mapping document, or a non-mapping preset body fails
  with a message naming the file.
- An unknown field is rejected and named (so `regon:` does not silently
  vanish).
- `region` is checked against the valid region set and `workflow` against the
  valid workflow set, with a precise error rather than a later Kagi 400.
  Dates flow through to Kagi's own validation, matching how the explicit
  `after` / `before` arguments behave.
- An unknown preset name passed to `preset` returns an error listing the
  defined presets (no silent fallback, unlike an unknown Kagi lens slug).

## Discovery

The `kagi://presets` MCP resource
(`parkour_mcp/kagi.py#kagi_presets_markdown`) lists the defined presets and,
when none exist, prints the resolved registry path and a format template.
Resources are autonomously readable only in clients that expose them as
tools (Claude Code); in other profiles the registry is edited and referenced
directly.
