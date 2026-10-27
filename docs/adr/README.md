# Architecture Decision Records

This directory is for contributors and for coding agents. It holds Architecture
Decision Records (ADRs): dated records of a design decision, the reasoning and
evidence behind it, and how it was meant to be carried out.

An ADR is a historical document, not a living specification. It describes
intent and methodology as they stood when it was written, and it is expected
to disagree with the code eventually. The current state of the system is
described elsewhere in `docs/`, `CLAUDE.md`, and `.claude/TECH_DEBT.md`, and
those documents are kept true by drift and cog. An ADR is never edited to
match the code.

## Rules for editing an ADR

1. **Above `## Implementation Notes`, the text is frozen once `status` is
   `accepted` or `rejected`.** Do not reword it, correct it against the
   code, or bring it up to date. Fixing a typo or a broken link is the one
   exception, and the commit that makes the fix must carry a line
   `ADR-Amend: ADR-NNNN` in its message.
2. **Record a divergence by appending an entry** to `## Implementation Notes`.
   Entries are never edited after they are written, and new entries go last.
3. **Reverse a decision by writing a new ADR.** Then set the old ADR's
   `status` to `superseded` and its `superseded_by` to the new id, and append
   an Implementation Notes entry that points to the new ADR. To retire a
   decision with no replacement, set `status` to `deprecated` and append an
   entry giving the reason. `status` and `superseded_by` are the only
   front-matter fields that change after acceptance.
4. **Never bind an ADR with drift.** Write code references as
   `path#Symbol` without an `@./` prefix, and do not run `drift link` on
   files in this directory. A binding would flag the record as stale and
   invite exactly the rewrite these rules forbid.

While `status` is `proposed`, the whole document may still be revised.

`scripts/check_adr_records.py` enforces these rules against each ADR's git
history, from the first version whose status left `proposed`. It runs in
`just docs-drift`, in the docs-drift workflow, and in the test suite, and
needs full history: a shallow clone fails the check. Claude Code also asks
before editing anything under `docs/adr/` (`.claude/settings.json`), which
covers its file tools and recognized shell writes but not every script, so
the check is the authoritative guard.

An ADR is accepted when the commit that sets `status: accepted` lands on
`main`, and `accepted` is that date. Until then the ADR lives on a branch, and
a rebase there can rewrite any commit it cites. Accepting on merge means every
hash the frozen text cites is already permanent.

## Status

Status values follow MADR:

- `proposed` → `accepted` or `rejected`.
- `accepted` → `deprecated` or `superseded`.

There is no `implemented` status. An ADR records a decision, not its
delivery; delivery is recorded as `Shipped` entries under Implementation
Notes, and git tags and `CHANGELOG.md` remain the record of what each
release contains.

## Front matter

```yaml
id: ADR-NNNN
title: <noun phrase>
# status: proposed | accepted | rejected | deprecated | superseded
status: proposed
proposed: YYYY-MM-DD
accepted: null            # YYYY-MM-DD once accepted
supersedes: []            # ADR ids this one replaces
superseded_by: null       # ADR id that replaced this one
append_only_from: "## Implementation Notes"
```

`accepted` is the date the record froze. It is deliberately not MADR's
`date` field, which means "last updated".

## Implementation Notes entries

```markdown
### YYYY-MM-DD — <short title> (Deviation | Clarification | Abandoned | Shipped)

- **Frozen text:** <section name or quoted sentence the entry departs from>
- **What happened:** <what implementation found>
- **Instead:** <what was done, with commit hashes>
```

- **Deviation**: the plan was changed in practice.
- **Clarification**: the plan was ambiguous, and this entry records how it was read.
- **Abandoned**: part of the plan was dropped, with the reason.
- **Shipped**: work carrying out the decision was released. A decision
  delivered across several releases gets one entry per release, in this
  shape instead of the bullets above:

```markdown
### YYYY-MM-DD — <what shipped> (Shipped)

- **Release:** <tag, e.g. v2.3.0>
- **Commits:** <hashes on main>
```

## Sections

An ADR has these sections, in this order. Everything before
`## Implementation Notes` freezes at acceptance.

- **Context**: the situation and constraints that forced the decision.
- **Decision**: what was decided, stated so it can be checked.
- **Considered Options**: the alternatives, with what each gains and loses.
- **Plan**: how the decision is to be carried out.
- **Consequences**: what the decision was expected to cause. These are
  predictions, not observations.
- **Evidence**: measurements behind the decision, dated, with the method used
  and a list of what was not verified.
- **Reopen If**: the conditions under which the decision should be revisited.
- **Implementation Notes**: append-only, as described above.

The format is a Nygard ADR with MADR's status vocabulary, plus an
append-only log modelled on IETF RFC errata and placed where a Kubernetes
Enhancement Proposal puts its Implementation History.
