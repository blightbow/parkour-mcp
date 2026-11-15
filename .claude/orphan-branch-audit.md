# Orphaned branch audit — working brief

This file is the handoff between Claude Code sessions on different devices working on
`chore/orphan-branch-audit`. Read it whole before acting. Update it as you work, and
commit it so that the next session starts from what you found. Delete it in the
commit that finishes the audit; it does not belong on `main`.

## Goal

Find work that was committed but never reached `main`, decide for each item whether
its intent still applies, and only after that decide how to land it. **No
cherry-picks, merges, or branch deletions until the intent audit for that item is
recorded here and the user has agreed.**

An "orphan" is a commit reachable from some branch (local or remote, on any device)
but not from `main`, and with no patch-equivalent or reimplementation on `main`.

## Hard rules

- Do not delete `origin/integration/2.0.0`. Its tip `dfb12da` is the only ref holding
  the six orphans below. If it has to go, create `archive/integration-2.0.0` at
  `dfb12da` first.
- Do not push. The user pushes (Yubikey).
- Branches that exist only on one device are the main unknown. Each device runs the
  inventory below, and the results go in the device table.

## Inventory procedure (run on every device)

```zsh
git fetch --prune origin
# Local branches with no upstream, or ahead of their upstream: these may exist only on this device
git for-each-ref --format='%(refname:short) %(upstream:short) %(upstream:track)' refs/heads
# Classify every local and remote branch against main
for b in $(git for-each-ref --format='%(refname:short)' refs/heads refs/remotes/origin | grep -v -e '^origin$' -e '/HEAD$' -e '^main$' -e '^origin/main$'); do
  git merge-base --is-ancestor "$b" main && { echo "MERGED     $b"; continue; }
  n=$(git cherry main "$b" | grep -c '^+')
  [ "$n" -eq 0 ] && echo "PATCH-EQ   $b" || echo "ORPHAN($n) $b"
done
# For each ORPHAN, compare subjects against main to catch reimplementations
git log --format=%s main..<branch> | while IFS= read -r s; do printf '%s => ' "$s"; git log --oneline -F --grep="$s" main | head -1; echo; done
# Stashes are orphans too
git stash list
```

A subject match on `main` is only a lead. Confirm it by reading the code.

## Device inventory

| device | date | local-only branches | stashes | notes |
|---|---|---|---|---|
| A (this brief's origin) | 2026-11-14 | none (local `integration/2.0.0` is ahead 3, behind 6; the 3 are merged) | 0 | integration/2.0.0 created here 2026-06-20 from origin at rc3 (`f29daea`), so it never saw the six orphans |
| B | — | — | — | not yet run; probable author of the six orphans |

## How the six orphans happened

`origin/integration/2.0.0` forked from `main` at `f29daea` (release: v2.0.0rc3). Device A
created its local `integration/2.0.0` from origin on 2026-06-20, before the six commits
reached origin. It then added `f2ffee5`, `819038d`, `c95352d` and merged into `main`
(`1f46de3`). It never pulled the remote line, so locally the branch reads as fully
merged while the remote holds six commits found nowhere else.

## Remote-branch orphans found on device A

`origin/fix/reddit-thread-graphql` (5 commits, incl. ADR-0001) is active work, not an
orphan; it exists as a local branch on A as well. The dependabot branches are tooling
PRs and are out of scope. Otherwise, everything below is on `origin/integration/2.0.0`.

"Trial pick" is the result of cherry-picking each commit onto `main` in order, aborting
on conflict. A conflict after an earlier abort can come from the missing predecessor
rather than from `main`.

| # | sha | commit | on main? | trial pick |
|---|---|---|---|---|
| 1 | `b6aedcc` | build(release): make live tests advisory; add one-time pre-push bypass | no | conflict: release.md, .gitignore, justfile, pre-push |
| 2 | `f2aed7a` | ci: migrate deprecated Node 20 actions to Node 24 majors | no | clean |
| 3 | `8eb102d` | feat(kagi): warn on ungrouped top-level OR | no | clean |
| 4 | `b6009cc` | docs: correct and de-duplicate drifted doc claims | partly | conflict: TECH_DEBT.md, CLAUDE.md, doi.py |
| 5 | `2300674` | feat(config): resolve config dir XDG-first cross-platform | no | conflict: semantic_scholar.py, test_common.py |
| 6 | `dfb12da` | feat(kagi): add local query presets with a seedable registry | no | conflict: CLAUDE.md, __init__.py, common.py, kagi.py, test_common.py, test_kagi.py (some from #5 missing) |

Read the full intent with `git show <sha>`. The commit bodies are detailed.

### Per-item state (status: `open` until the user decides)

1. **Live tests advisory** — `open`. `main` still runs `pytest -m live` in `just tag`
   and in `pre-push`, and its only escape is `git push --no-verify`, which also skips
   the mocked gate. That is the exact problem the commit fixed, so the intent stands
   unless the user has changed their mind. The four conflicting files have changed since,
   so this needs reimplementing rather than picking.
2. **Node 24 actions** — `open`, **time-sensitive**. `main` is still on
   `actions/checkout@v4`, `actions/setup-node@v4`, `astral-sh/setup-uv@v6`. The last
   successful release run (v2.4.0, 2026-08-20) predates GitHub's 2026-09-16 Node 20
   removal, so the next release is the first to run without it. Before picking, check
   the current node24 majors; the commit's version choices date from June.
3. **Ungrouped OR warning** — `open`. Absent from `main`; `kagi.py` has had only lint
   commits since rc3. Before landing, confirm that Kagi v1 search still gives OR
   that precedence.
4. **Doc drift corrections** — `open`, partly superseded. Already fixed on `main` by
   other means: the htmd/markdownify split in CLAUDE.md and the TECH_DEBT htmd entry
   (rewritten as "RESOLVED"). Still drifted on `main` (confirmed by grep): the polite-pool
   claim in `common.py` and twice in `doi.py`; the `scripts/sync_versions.py` docstring
   naming python-semantic-release; no mention of `hermes_plugin.py` or the hermes profile in
   CLAUDE.md; the YouTube bullet in CLAUDE.md still says "Two tool actions". Rework these
   claim by claim against current code instead of picking.
5. **XDG-first config dir** — `rework`, in-tree. Absent from `main`;
   `common.py#_CONFIG_DIR`, `github.py#GITHUB_CONFIG_PATH`, and `kagi.py#CONFIG_PATH`
   still hardcode `~/.config/parkour`. The HuggingFace token path (`hf_token`) was added
   after this commit and must move onto the resolver as well.
   - Decided 2026-11-14: the resolver stays inside parkour. The user does not want it
     extracted as the standalone "etcnauseam" package for now. When reworking, drop the
     commit body's "can later be lifted out as etcnauseam" line, and don't add any
     packaging or API-surface scaffolding just to make extraction possible.
   - Still to confirm with the user before landing: XDG-first on macOS (keeping
     `~/.config` as the default), `%APPDATA%` on Windows, and the `PARKOUR_CONFIG_DIR`
     override. Nothing on `main` contradicts them.
6. **Kagi query presets** — `open`, depends on #5 (config dir, `--init` seeding at
   0700) and builds on #3 (OR grouping). Ships `docs/kagi-query-presets.md` and a
   `kagi://presets` resource. Decide after #3 and #5.

## Next steps

1. Device B: run the inventory, fill in its row, and add any new orphans to the table.
2. With the user: settle each item's `open` status (land / rework / drop) and record
   the reason inline.
3. Land the agreed items on separate branches off `main`; only #2 and #3 are likely clean picks.
4. After everything has landed or been dropped: archive-tag `origin/integration/2.0.0`, delete it,
   and delete this file.
