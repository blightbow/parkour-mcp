---
description: Cut a new release (stage bump, review, commit, tag). Push stays manual.
---

Walk the user through the parkour-mcp release flow. This stages and
tags locally; the user pushes when ready (yubikey required).

## Step 1: Preflight

Run these in parallel and report briefly:

- `git status` (working tree must be clean)
- `git log origin/main..HEAD --oneline` (show the commits that will ship)
- `uv run cz bump --get-next --yes` (next version from commits since last tag)

Abort if working tree is dirty or no commits ahead of origin/main.
Flag to the user instead of proceeding.

Also spot-check that every `feat:` / `fix:` / `refactor:` / `perf:`
commit in the range carries a `Why:` trailer. Run
`git log origin/main..HEAD --format='%h %s%n%(trailers:key=Why,only)'`
and flag any commits that are missing one. `Why:` trailers are the
source of release-note prose; commits without them fall back to the
bare subject which produces weaker changelog entries.

CAVEAT: git's `%(trailers)` only recognizes trailers in the final
contiguous block of the message. The house style puts a blank line
between `Why:` and `Co-Authored-By:`, which splits them, so this command
reports `Why:` as missing on commits that actually have one. git-cliff
uses its own footer parser and extracts them correctly. Do not act on a
"missing" result here alone — confirm against the Step 2 git-cliff
preview (if the bullet renders as the `Why:` prose, the trailer is fine;
if it renders as the bare subject, it is genuinely absent).

## Step 2: Preview

Show the user what will land:

```
uv run cz bump --dry-run --yes
LAST_FINAL=$(git tag --list 'v*' --merged HEAD --sort=-v:refname | grep -E '^v[0-9]+\.[0-9]+\.[0-9]+$' | head -1)
git cliff --tag v<NEXT> "$LAST_FINAL..HEAD"
```

where `<NEXT>` is the version from step 1. Together these preview the
version bump and the assembled CHANGELOG entry without writing
anything. The range starts at `$LAST_FINAL`, the most recent final
`vX.Y.Z` tag, rather than `--unreleased`: when a release is cut after
intervening RC tags, `--unreleased` would span only the post-RC
commits, so the final's entry would omit the bulk of the work. Pause;
let the user approve or ask for commit-message edits (via
`git commit --amend` or `git rebase -i`) before you stage.

If the user wants a public RC (finals-only is the default), they can
ask for it explicitly. For an RC, step 3's `cz bump` becomes:

```
uv run cz bump --version-files-only --yes --prerelease rc
```

commitizen's `pep440` scheme emits a zero-based counter — the first RC
of `2.0.0` is `2.0.0rc0` (PEP 440, which `uv build` and PyPI accept;
`sync_versions.py` translates it to `2.0.0-rc.0` for manifest.json). Do
NOT pass `--prerelease-offset`: it shifts only the first RC and is
silently ignored on every later bump, leaving a misleading no-op in
config. An RC also does NOT prepend to CHANGELOG.md — that file tracks
final releases only, and git-cliff folds an RC's commits into the next
final section. For an RC, skip the `git cliff ... --prepend` line in
step 3; the RC's GitHub Release notes are assembled by CI.

## Step 3: Stage the bump

```
uv run cz bump --version-files-only --yes
```

commitizen bumps `project.version` in `pyproject.toml` only. It does
NOT commit or tag. `--yes` is safe here because the user saw the
preview in step 2 and approved.

Then run the downstream steps that populate the rest of the release
commit:

```
NEXT=$(uv run python3 -c "import tomllib; print(tomllib.load(open('pyproject.toml','rb'))['project']['version'])")
LAST_FINAL=$(git tag --list 'v*' --merged HEAD --sort=-v:refname | grep -E '^v[0-9]+\.[0-9]+\.[0-9]+$' | head -1)
git cliff --tag "v$NEXT" "$LAST_FINAL..HEAD" --prepend CHANGELOG.md   # finals only — skip this line for an RC
uv run python3 scripts/sync_versions.py
```

- `git cliff --tag "v$NEXT" "$LAST_FINAL..HEAD" --prepend` parses
  Conventional Commits from that range, extracts `Why:` trailers as
  user-facing narrative, and prepends an assembled `## [$NEXT] <date>`
  section to `CHANGELOG.md`. The range is anchored to the last final
  `vX.Y.Z` tag (not `--unreleased`) so a final cut after RC tags still
  spans the whole body of work. **For an RC, skip this line entirely** —
  CHANGELOG.md tracks final releases only.
- `sync_versions.py` mirrors the new `project.version` into
  `manifest.json` (translated to strict SemVer for Claude Desktop) and
  `server.json` (PEP 440 verbatim for MCP Registry).

## Step 4: Review

- Show `git status` to confirm the changed set (pyproject, CHANGELOG,
  manifest, server).
- Show `git diff` for the new CHANGELOG.md entry specifically.
- git-cliff renders the `Why:` trailer as the bullet text. If any
  trailer was written loosely at commit time, this is the last chance
  to tighten it. Options: amend the commit's trailer and re-run
  `git cliff --tag --unreleased --prepend CHANGELOG.md` (after
  reverting the previous CHANGELOG edit), or hand-edit CHANGELOG.md
  directly. The latter is simpler for small wording tweaks.
- **Edits here reach the GitHub Release.** The workflow's notes step
  reads the `## [$NEXT]` section out of CHANGELOG.md and only falls
  back to regenerating from commits when no such section exists (which
  is the RC case, since CHANGELOG.md tracks finals only). So the file
  in the repo and the release body cannot disagree — but it also means
  a final release with no CHANGELOG section, or with a section whose
  heading does not match the tag's version exactly, fails CI at the
  release-notes step rather than publishing something wrong.
- Sanity: every `feat:` / `fix:` / `refactor:` / `perf:` commit in
  the range should produce a bullet in the matching section. Flag any
  that are missing (probably means the commit's type didn't match any
  `commit_parsers` rule in `pyproject.toml`).

## Step 5: Commit, then tag

Order matters: commit before tag so the tag points at the release
commit, not its parent.

```
git add pyproject.toml manifest.json server.json uv.lock CHANGELOG.md   # drop CHANGELOG.md for an RC
git commit -m "release: v<NEXT>"
just tag v<NEXT>
```

Stage the release files explicitly. Do NOT `git add -A`: an untracked
directory in the tree (e.g. a local `deploy/`) would otherwise be swept
into the release commit. The release touches `pyproject.toml`,
`manifest.json`, `server.json`, `uv.lock` (cz rebuilds it), and — finals
only — `CHANGELOG.md`. An RC does not modify `CHANGELOG.md`, so omit it.
Confirm the staged set with `git status` before committing.

`just tag` runs `just docs-drift` (cog blocks, drift anchors, manifest
tool list), `sync_versions.py --check`, the mocked test suite (with ruff
lint), and the live test suite before creating the annotated tag. Expect
~1-2 minutes for live tests.

A `docs-drift` failure means a doc describes code that has since moved.
Read the doc section and the code side by side, fix the prose if it is
wrong, then restamp with `drift link <doc-path> --doc-is-still-accurate`.
A cog mismatch is regenerated with `just docs-drift-fix`. Do not reach
for `--no-verify`: no tag exists yet at this point, so there is nothing
to rescue by pushing past it.

## Step 6: Hand off

Remind the user (substitute the current branch — releases need not be
cut from `main`):

> Tag created locally. Push the current branch and the tag with:
>
>     git push origin <branch> --follow-tags
>
> The release workflow fires on the tag push and handles: uv build,
> PyPI OIDC publish, mcpb pack, GitHub Release creation (notes read from
> the CHANGELOG.md section for this version, or assembled by git-cliff
> over the range since the last final tag when there is none — the RC
> case), server.json mcpb asset coordinates, MCP Registry publish.
> Watch the run at: https://github.com/blightbow/parkour-mcp/actions

The trigger is the tag, not the branch: the workflow keys on
`refs/tags/v*` and runs regardless of which branch the tag is pushed
from. Verified end to end for an RC cut from an integration branch
(`v2.0.0rc1` from `integration/2.0.0`, 2026-06-03): PyPI pre-release
upload, GitHub pre-release (`--prerelease`, not marked Latest), and MCP
Registry publish all succeeded. So cutting a public RC off a long-lived
integration branch before it merges to `main` is a supported flow.

Do not push. Do not trigger the workflow. The user pushes.
