# ruff 0.16 migration plan

Working document for the `chore/ruff-0.16-migration` branch. Supersedes the
triage in `TECH_DEBT.md#ruff-016-migration--184-findings-deliberately-deferred`,
which was directionally right on volume and wrong on two classifications. Fold
this back into `TECH_DEBT.md` (or delete that entry) when the branch lands.

## What actually changed upstream

ruff 0.16.0 grew the default rule set from 59 rules to 413. Both the release
notes and the migration blog frame this as pure addition. The release notes'
Breaking changes section says only "Ruff now enables a much larger set of rules
by default (413, up from 59)", with no mention of anything being withdrawn, and
the blog's "Better default rule set" section likewise describes only additions
before offering `select = ["E4", "E7", "E9", "F"]` as the revert.

The blog's one line addressing existing configs is:

> Even if you're already using `select` or `extend-select`, we hope that this
> will draw your attention to helpful rules that you previously hadn't
> discovered.

That is an invitation to look at new rules, not a statement about impact. It
does not warn that `extend-select` users lose 18 rules, which is what actually
happens here.

Measured directly, by diffing `linter.rules.enabled` from
`ruff check --isolated --show-settings` on both versions:

| | 0.15.8 | 0.16.0 |
|---|---|---|
| Rules enabled by default | 59 | 413 |
| Rules **dropped** from the default | | 18 |

The 18 dropped rules are the entire reason this migration needs judgement
rather than a `--fix` run:

```
E401 E402 E701 E702 E703 E711 E712 E713 E714
E721 E731 E741 E742 E743 F403 F405 F406 F722
```

Every one of them is **stable, non-deprecated, and non-removed** in 0.16
(verified via `ruff rule <code> --output-format json`). They were not retired.
They were dropped from the default *selection*, and all 18 fall inside the old
`E4` / `E7` / `F` default prefixes.

This repo configures lint with `extend-select`, which extends whatever the
default happens to be. So the upgrade does two things at once: it switches on
354 new rules, and it silently switches off 18 rules the repo has been
enforcing since inception. The second half is invisible in the finding count,
because a rule that stops running produces no findings.

## Why the 18 were dropped: what upstream actually published

Short answer: **upstream published no per-rule justification for any of the 18.**
This was checked against primary sources rather than inferred, because the
secondary summaries circulating about this release are wrong on attribution.

### What exists

1. **The proposal** ([discussion #23203], by `ntBre`, ruff maintainer) states the
   goal: "reducing the amount of configuration required to start using Ruff,
   while also capturing the benefits of rules that are currently disabled by
   default", and expanding "beyond the primary focus on 'correctness' rules in
   the current default set to include rules that are consistent with
   widely-accepted Python idioms or styles." No rule-level reasoning.
2. **The docs overview** says the default omits "any stylistic rules that
   overlap with the use of a formatter, like `ruff format` or Black." This
   plausibly covers at most `E401`, `E701`, `E702`, `E703` out of the 18. It
   does not explain `E711`, `E712`, `E721`, `F403`, `F405`, or `F722`.
3. **The source** (`crates/ruff_linter/src/settings/mod.rs:357`,
   `DEFAULT_SELECTORS`) is a flat, hand-curated, rule-by-rule enumeration with
   no doc comment and no stated criteria.
4. **Per-rule drop reasoning exists only for preview-era drops**, and only for
   rules that were *newly added and then withdrawn*: [PR #23879] drops
   `PERF401`, `PERF403`, `PLR1714`, `RET504`, `TRY300`; [PR #27029] drops
   `TRY004`. Both argue in Clippy's severity vocabulary ("bump it down to
   Pedantic", "just a bit more pedantic than expected"). Neither touches the 18.
5. **The stabilization PR** ([PR #27035]) is two sentences and contains no
   rationale at all.
6. **The criteria do not exist yet.** The blog closes the default-rules section
   with: "We view this work as closely tied to our longstanding goal of [rule
   recategorization], so look forward to upcoming developments in this area."
   [Issue #1774] is that goal: a three-year-old, still-open proposal to adopt
   Clippy-style categories (correctness, suspicious, style, complexity, perf,
   pedantic, nursery) plus Python-specific ones. `charliermarsh` agreed to "kick
   off a re-categorization effort" and noted "I need to give some thought to the
   right categorization since this will be hard to undo." It was never resolved.

   This is the load-bearing finding. The 0.16 default set was chosen *ahead of*
   the taxonomy that would justify it. That is why [PR #23879] reaches for
   Clippy's vocabulary informally ("bump it down to Pedantic") instead of citing
   a ruff category: there is no ruff category to cite. There is no principled
   framework behind the 18 drops because the framework is still an open issue.

### Ruff documented exactly this kind of change last time

This is not a standard nobody holds ruff to. It is ruff's own, from the last
time they narrowed the default set. `BREAKING_CHANGES.md` for **0.1.0** carries
an entry titled "Remove formatter-conflicting rules from the default rule set"
([PR #7900]):

> Previously, Ruff enabled all implemented rules in Pycodestyle (`E`) by
> default. Ruff now only includes the Pycodestyle prefixes `E4`, `E7`, and `E9`
> to exclude rules that conflict with automatic formatters. Consequently, the
> stable rule set no longer includes `line-too-long` (`E501`) and
> `mixed-spaces-and-tabs` (`E101`). [...] This change only affects those using
> Ruff under its default rule set. Users that include `E` in their `select` will
> experience no change in behavior.

That entry names the removed rules, gives the rationale, and states explicitly
who is and is not affected. It is a model of how to announce this.

The **0.16.0** entry in the same file is titled "New default rules" and reads in
full: "Ruff now enables a much larger set of rules by default (413, up from
59)." No withdrawal is mentioned, and no impact statement is offered for `select`
or `extend-select` users, who this time genuinely are affected.

So the 18 drops are undocumented in all four canonical places: `BREAKING_CHANGES.md`,
the release notes, the migration blog, and the Default Rules page. The gap is a
deviation from ruff's own established practice, not merely an omission.

> **Partly resolved upstream on 2026-07-27.** `BREAKING_CHANGES.md` and the
> 0.16.0 release notes now name all 18. See "Upstream outcome" below. The
> paragraph above is retained as the state that justified filing.

### Narrowing is rare, so this is not a pedantic complaint

Checked explicitly, because if upstream narrowed the default set every other
release without comment, then one undocumented instance would not be worth
raising and the pin would be harder to justify on principle. Dumping the enabled
rule set for every minor release from 0.1.0 to 0.16.0 and diffing consecutively:

| Releases | Default rules |
|---|---|
| 0.1.0 through 0.7.0 | 60 |
| 0.8.0 through 0.15.0 | 59 |
| 0.16.0 | 413 |

Only two releases removed anything: 0.8.0 (1 rule) and 0.16.0 (18). Every other
minor release removed nothing at all, so the set sat unchanged for fifteen
releases.

The 0.8.0 removal is not a coverage loss. It is `E999` (`syntax-error`) moving
from a selectable rule to an unconditional diagnostic. Verified: 0.16.0 still
reports a syntax error under `--isolated` with no configuration, despite `E999`
not being in the default set.

So 0.16.0 is the **first genuine narrowing of the default set since the one
`BREAKING_CHANGES.md` documents**, and the two were handled differently. That is
the whole basis for treating this as a real gap rather than nitpicking.

### How the 18 came to be dropped

The pre-0.16 default was a **prefix** selection, `F`, `E4`, `E7`, `E9`, set by
[issue #7572] whose entire stated purpose was "Remove the formatting specific
lint rules from the default rule set." The 18 were therefore never individually
chosen; they were swept in because they happen to live under those prefixes.

The 0.16 set was rebuilt as an explicit per-rule enumeration. The proposal's
config has a `# Current defaults` block listing the 41 old-default rules that
were retained. 59 minus 41 is exactly our 18. So the drops were the result of a
deliberate rule-by-rule review, but the reasoning behind each was never written
down publicly.

The practical consequence: restoring them is not overriding a considered
upstream judgment about `E402` or `F405` specifically. It is declining a
silent, unexplained narrowing of a set this repo has enforced since inception.

### Attribution correction

Several summaries of this release attribute per-rule reasons to ruff, for
example that `E402` was dropped because "people usually know what they're
doing", `E711` because it is "commonly used with SQLAlchemy", `E731` because
lambdas are "nice sometimes", and `E741` because it "isn't a huge problem".
Those lines are **comments in `hauntsaninja`'s personal work config**, posted in
the discussion thread as one data point among many. They are not ruff's
justification and should not be cited as such.

### Measured: the 18 have no replacement coverage

A probe file violating all 18 conditions was run against 0.16 defaults.
**Zero of the 18 were reported.** The only diagnostics were incidental (`I001`,
`F401`, `F811`). Re-selecting the 18 fires all 18. There is no consolidation
happening here and no newer rule absorbing the old ones: the drop is a pure
loss of coverage.

Worth singling out: `F722` (syntax error in a forward annotation) is a genuine
correctness bug, and it is silent by default in 0.16. That sits awkwardly beside
the release blog's framing that the new defaults "catch severe issues, including
syntax errors and immediate runtime errors."

**This repo has one backstop.** `ty` reports the `F722` case as
`invalid-syntax-in-forward-annotation`, and `ty` gates the suite via
`pytest-ty`. So 1 of the 18 is independently covered here. The other 17 are not
covered by anything.

### Nobody downstream has noticed either

Searched for prior art before concluding this was ours to find. Kagi across the
open web and the `forums` lens, plus ruff's own issue tracker since the release,
turn up **no report of the withdrawals** from anyone. Secondary coverage of
0.16 is uniformly about the expansion (CI breaking, "413 rules", how to opt
out), and at least one write-up gets it outright wrong, claiming ruff "decided
to enable all rules that are considered stable by default", which would be
roughly 900, not 413.

Post-release issue traffic runs entirely in the opposite direction: [#27177]
("remove all rules without an automated fix from default rules"), [Issue #27197]
(`DTZ`), [#27195] (`CPY001` on empty files), [#27145] and [#27149] (`I001`
surprises). Every one asks ruff to enable *less*. None observes that something
was disabled.

That asymmetry is predictable and is the whole reason this entry exists: a rule
switched on breaks your build and gets filed within hours, while a rule switched
off produces silence, which is indistinguishable from passing. A maintainer
reply in [#27149] captures the prevailing frame: "I don't think we changed
anything about `I001` this release except enabling it by default, so it probably
wasn't running at all before."

**Filed upstream as [Issue #27199]** on 2026-07-26, asking for a
`BREAKING_CHANGES.md` entry for 0.16.0 in the shape of the 0.1.0 one. It carries
the rule list, both reproductions, the per-release narrowing survey, and the
0.1.0 precedent. It also flags `F722` as the one worth re-examining on the
merits rather than just documenting.

Nothing in the migration plan below depends on the outcome. A reply naming the
intended reasoning would let us record *why* each of the 18 was dropped rather
than just that it was, which would sharpen the `select` list's comments, but the
decision to pin stands either way.

### Upstream outcome

Closed on 2026-07-27 with no comment, two upvotes, and a documentation fix. Both
`BREAKING_CHANGES.md` and the 0.16.0 release notes now carry the same amended
sentence, which names every one of the 18:

> Note that this is primarily an expansion, but 18 of the more opinionated
> pycodestyle (`E`) and pyflakes (`F`) rules have been removed from the default
> set: `E401`, `E402`, `E701`, `E702`, `E703`, `E711`, `E712`, `E713`, `E714`,
> `E721`, `E731`, `E741`, `E742`, `E743`, `F403`, `F405`, `F406`, and `F722`.

That is the discoverability problem solved. A reader upgrading can now find out
what stopped running without diffing two rule sets. Two of the three asks landed
(the rule list, and mirroring into the release notes). The migration blog was not
given the same treatment.

**What did not land, and why it still matters for us:**

1. **No impact statement.** The 0.1.0 entry closed with "Users that include `E`
   in their `select` will experience no change in behavior." The 0.16.0 entry has
   no equivalent, so an `extend-select` user still gets no signal that they are
   the affected cohort. This is the half of the ask that would have told a reader
   *whether to act*, and it is the half that governs our config decision.

2. **"More opinionated" is inaccurate for at least two of the 18.** Verified
   against CPython 3.14, not inferred:

   - `F406` flags `from x import *` inside a function. CPython rejects it
     outright: `SyntaxError: import * only allowed at module level`.
   - `F722` flags a malformed forward annotation. Evaluating it raises
     `SyntaxError: Forward reference must be an expression -- got 'int int'`.

   Neither is a matter of taste. Both detect code Python itself refuses. The
   contrast is sharp against `F403` / `F405`, which flag *valid* code
   (`from os.path import *` then `join(...)` runs fine) and where "opinionated"
   is a fair description. So the blanket characterization is right for most of
   the set and wrong for the two that are not style judgements at all.

**Bearing on the plan:** none of this changes the decision to pin with `select`,
and it mildly strengthens it. The upstream rationale is now a single adjective
covering 18 rules, two of which it does not describe. That is not a judgement we
can defer to per-rule, so the `select` list's comments should state our own
reasoning rather than cite ruff's. `F722` and `F406` in particular are worth
keeping on the grounds that they catch invalid Python, which is a stronger and
more durable justification than "the old default had them."

### Follow-up: upstream conceded both, and named its actual method

A clarification request on the two mischaracterized rules drew a substantive
reply from `ntBre` and a new tracking issue, [Issue #27213] (labels:
`rule-selection`, `tracking`).

- **`F406` is conceded as an oversight.** It now sits under "Add to defaults"
  in #27213, annotated "This is actually a syntax error and should be documented
  and implemented as such."
- **`F722` was an intentional omission**, on the reasoning that "it's primarily
  important for type checkers, and I think type checkers usually emit such
  diagnostics themselves." It sits under "Needs decision" and is open to
  reconsideration.

Two things follow that matter more than the two rules.

**The selection criterion was the rule's own documentation.** `ntBre` on `F406`:
"I based my decision on our rule docs, which didn't mention the syntax error, and
I grouped it conceptually with `F405` and `F403`, which were more related to a
Ruff limitation." So the earlier finding stands but sharpens: there is no written
taxonomy (that is still [Issue #1774], unresolved), and in its absence the
working method was to read each rule's doc page and judge from it. That is why
`F406` slipped. Its doc page omits the `SyntaxError`, so the curation inherited
the gap. A rule's documentation quality determined its default status.

That is a better answer than "no criteria", and it is a caution for us: reading
a rule's doc page is exactly how we are pricing rules here, and it demonstrably
under-describes at least one of them. Where a rule's behavior matters to a
decision, test it rather than trusting the summary.

**Upstream's versioning policy validates the `<0.17.0` ceiling.** From #27213:
"Our current versioning policy means that we have to wait for any changes to the
defaults until the next minor release." Default-set changes land in minors. Our
dependency ceiling is set at exactly that boundary, so the next round of
churn (including `F406` returning) arrives as a deliberate bump rather than a
resolver outcome.

**For our `select` list:** keep both. `F406` because upstream now agrees it is a
syntax error. `F722` because upstream's own argument for dropping it (type
checkers cover it) is satisfied *here* by the `ty` gate, which means selecting it
in ruff is cheap redundancy on a real error rather than a lone line of defense.

[Issue #27213]: https://github.com/astral-sh/ruff/issues/27213

### The default set is still moving

[Issue #27197], filed the day after this assessment by `pganssle`, a CPython
`datetime` maintainer, argues the newly-defaulted `DTZ` rules are "extremely
misguided" and should be opt-in. `DTZ` does not currently fire in this repo, so
there is no action item, but it is direct evidence that the 0.16 default set is
contested and still settling within days of release. That is an argument for
pinning rather than inheriting, independent of the 18.

[discussion #23203]: https://github.com/astral-sh/ruff/discussions/23203
[PR #23879]: https://github.com/astral-sh/ruff/pull/23879
[PR #27029]: https://github.com/astral-sh/ruff/pull/27029
[PR #27035]: https://github.com/astral-sh/ruff/pull/27035
[issue #7572]: https://github.com/astral-sh/ruff/issues/7572
[Issue #27197]: https://github.com/astral-sh/ruff/issues/27197
[Issue #1774]: https://github.com/astral-sh/ruff/issues/1774
[rule recategorization]: https://github.com/astral-sh/ruff/issues/1774

## Not everything here is the default-set change

Three of the rules in the finding list arrive via a separate 0.16 change:
`PLR0917`, `ISC004`, and `FURB192` were **stabilized out of preview** in this
release. `PLR0917` accounts for 22 of the 184 findings and would fire on any
0.16 upgrade regardless of the default-set strategy, because this repo already
selects `PLR` and was simply not getting the rule while it was in preview.
`ISC004` (6) and `FURB192` (1) compound both changes: newly stable *and* newly
default. Pinning the rule set does not make these go away, so they belong in
the fix phases either way.

Also landed in 0.16: suppression comments now take a space after the colon
([PR #27123]). Worth knowing before writing new directives, though it does not
invalidate the existing ones.

[PR #27123]: https://github.com/astral-sh/ruff/pull/27123

## Corrections to the existing triage

**1. The 24 `RUF100` findings are not "legitimate new signal". They are fallout
from `E402` being dropped.**

All 24 read `Unused 'noqa' directive (non-enabled: E402)`. They are unused only
because `E402` is no longer enabled. Fifteen of them sit in `tests/conftest.py`,
where they are load-bearing:

```python
init_tool_names("code")

import parkour_mcp.semantic_scholar  # noqa: E402
```

Those imports must follow `init_tool_names("code")`, because the source modules
capture tool display names at import time to build their description templates.
The `noqa` documents a real ordering invariant. Autofixing `RUF100` would delete
all 24 directives, and per this repo's own note the `RUF100` autofix removes the
entire trailing comment including prose. The invariant would become both
undocumented and unenforced.

**2. `RUF059` does not resolve the `_matched_meta` entry in `TECH_DEBT.md`.**

`RUF059` respects `dummy-variable-rgx`, so the underscore-prefixed
`_matched_meta` in `fetch_direct.py#_sections_response` is still unreported.
That tech-debt entry stands unchanged.

**3. The `B018` call on `.vulture_whitelist.py` was correct.** Confirmed false
positive: the file is bare name references by design and is parsed, never
imported.

## The highest-leverage move

Restoring the 18 dropped rules removes 24 findings and introduces **zero** new
ones. The codebase is already clean under all 18. Verified:

```
ruff check ... --extend-select E401,E402,E701,...,F722 --statistics
  184 findings -> 160 findings
```

This is worth stating plainly, because it inverts the usual shape of a lint
migration: the single biggest reduction in the finding count comes from turning
*more* enforcement on, not from waiving anything.

## Strategic decision: keep `extend-select`, pin the ruff version

**Recommendation: keep `extend-select`, restore the 18 inside it, and let the
dependency ceiling be the drift guard.**

This reverses an earlier recommendation in this document, which argued for
replacing `extend-select` with an explicit `select`. That argument rested on one
premise: the enforced rule set is defined by subtraction from a *moving* upstream
default, so upstream can withdraw a rule and nothing notices. The premise was
true when ruff floated in transitively via `pytest-ruff`. It is no longer true.
`pyproject.toml` now declares `ruff>=0.16.0,<0.17.0` directly, and upstream only
changes defaults on a minor release, so the default set cannot move inside our
dependency range without a deliberate edit to the ceiling. Drift is solved by
the dependency, more cheaply than by re-deriving 413 rules.

With that premise gone, `select` only buys control, and it charges three ways.

**It makes our judgement the ceiling rather than the floor.** An adversarial
review of the nine exclusions drafted under the `select` plan found six weak or
wrong, four of them checkably false in under a minute. Under `select` that
judgement *is* the enforced set. Under `extend-select` upstream's curation is
the floor and ours only adds.

**Omissions never expire.** `RUF100` makes a `noqa` self-expiring: a suppression
that stops suppressing becomes an error. Nothing does that for a rule that was
simply never selected. `select` would move ~260 rules from "enforced" to
"silently absent, no per-rule reason, nothing will ever flag it." That is the
exact failure mode this document spends 300 lines documenting in *upstream's*
behavior and which we filed [Issue #27199] about. Reproducing it one level down,
in our own config, would be indefensible.

**It forfeits upstream's corrections.** `F406` is already queued to return to the
defaults in 0.17 as a direct result of our report. Under `extend-select` that
arrives on the version bump. Under `select` it arrives only if someone notices.

### The posture argument, which is the decisive one

Under `select`, declining a rule is free and invisible: leave it off a list.
Under `extend-select`, declining requires an `ignore` entry with a stated reason,
in one enumerable place a reviewer can audit.

This repo is substantially LLM-maintained, and an LLM facing a wall of findings
is biased toward whatever makes them disappear. `select` makes that bias
frictionless and untraceable. `extend-select` forces it to leave a mark. That is
the same reasoning that replaced 51 false `noqa` with a declarative ban list in
`ec1a81a`: prefer the arrangement where the lazy path costs something.

### Measured

| Posture | Findings | Composition |
|---|---|---|
| `select`, conservative | 22 | all `PLR0917` |
| `extend-select` + restored 18 | 160 | 81 isort (autofix), 22 `PLR0917`, 17 vulture false positives, **40 real judgement calls** |

Forty genuine decisions buys the entire remaining 0.16 default set.

### The carve-outs were an artifact of the wrong posture

Five of the nine exclusions agonised over under the `select` plan are **moot**
under `extend-select`, because ruff does not enable those rules by default in the
first place: `PERF401`, `PLW0603`, `RUF001`/`RUF002`/`RUF003`, `ASYNC109`, and
`TC001`-`TC003`. Verified against `--show-settings` on 0.16.0.

That matters beyond the bookkeeping. Two of the reasons written for those
carve-outs were false, and the worst of them (claiming `RUF001`-`RUF003` had to
be waived because the repo emits box-drawing fence glyphs, when box-drawing
characters cannot trigger those rules at all and the 16 real findings were `×`
and `–`) never needed to exist. `select` forced a from-scratch re-derivation of
upstream's entire curation, and manufacturing 260 justifications is exactly the
pressure that produces confident, plausible, false ones.

## Config comments that have gone stale

The `[tool.ruff.lint]` comment block documents deliberate exclusions. Three were
false or expired.

- **`TC`**: the comment said `TC` is absent because it "moves imports into
  `if TYPE_CHECKING:`, which breaks the runtime pydantic annotations on the tool
  signatures". The hazard is real and better evidenced than the comment claimed:
  the MCP SDK resolves signatures with `inspect.signature(..., eval_str=True)`,
  so a TYPE_CHECKING-only name is missing from `__globals__` and registration
  raises `InvalidSignature`. But the blanket phrasing is wrong. `TC004` is the
  inverse rule and catches exactly that breakage; it is default-enabled and
  welcome. Fixed.
- **`ANN`**: still correctly absent, not in the 0.16 defaults. Verified, no
  change needed.
- **`BLE001`**: **the reason expired in the release this migration adopts.** The
  comment justifies the ignore on the grounds that the rule "accepts only
  re-raise or error-level logging" while this repo's idiom is
  `logger.debug(..., exc_info=True)`. Ruff 0.16.0 stabilized an exemption for
  exactly that idiom. Verified by running both versions on the documented
  pattern: 0.15.8 flags it, 0.16.0 does not.

  Worse, the surviving findings do not match the comment's description at all.
  Across `parkour_mcp/`, **zero** flagged handlers use the `exc_info` idiom the
  comment defends, and the large majority log nothing whatever
  (`except Exception: return None`). The comment's backstop claim is also false:
  it asserts S110 and SIM105 catch silent swallows, and both report zero
  findings here, because S110 matches only `except: pass`.

  So the ignore is not protecting a documented idiom. It is concealing a few
  dozen silent exception swallows in an async network client. This is the
  highest-severity finding in the migration and it is not a lint preference; see
  Phase 6.

## Hazard: the isort autofix destroys `noqa: PLC0415` directives

This is the sharpest trap in the migration and it must be handled before the
mechanical phase, not after.

`I001` is newly enabled by default and fires 81 times. Its autofix rewraps
imports to the 88-character default line length. Four function-scope imports
carry a `# noqa: PLC0415` plus a prose reason and run 102 to 127 characters, so
the fix wraps them into parenthesized form:

```python
# before (107 chars)
from .arxiv import _fetch_arxiv_paper  # noqa: PLC0415  # hoisting closes an arxiv->doi->arxiv loop

# after ruff check --fix
from .arxiv import (
    _fetch_arxiv_paper,  # hoisting closes an arxiv->doi->arxiv loop
)
```

**The `# noqa: PLC0415` is silently dropped.** Only the prose survives, relocated
onto the imported name. `PLC0415` then fires at all four sites, which is exactly
where the 4 phantom `PLC0415` findings in a naive `--fix` run come from.

These four are the genuine circular-import suppressions, the survivors of the
`ec1a81a` audit that replaced 51 false `noqa` with the declarative ban list.
They are the most carefully verified suppressions in the repo, and a careless
`--fix` deletes them while leaving a comment that still *claims* the site is
suppressed. That is the confabulation failure mode reintroduced by tooling.

Affected sites: `doi.py:696`, `doi.py:707`, `mediawiki.py:663`, `mediawiki.py:746`.

Mitigation, in preference order:
1. Before running any autofix, restructure the four sites so the directive
   survives: move the prose to a preceding plain comment and leave a short
   `from .arxiv import _fetch_arxiv_paper  # noqa: PLC0415` under 88 chars.
   This also matches the repo's reader-posture comment convention.
2. Alternatively, adopt 0.16's preceding-line form,
   `# ruff: ignore[PLC0415]`, which is not attached to the wrapped line.
   0.15 also added block suppression, `# ruff: disable[PLC0415]` /
   `# ruff: enable[PLC0415]`, which survives rewrapping for the same reason.
   Both are worth weighing against option 1, which keeps the existing spelling.
3. Either way, gate the phase on a diff check: after autofix, assert the count
   of `noqa: PLC0415` directives is unchanged.

Note for any future suppression audit: 0.16 accepts both `# noqa: RULE` and
`# ruff: ignore[RULE]`, inline or on the preceding line. Verified that `RUF100`
polices both spellings, so the self-expiring property carries over, but a grep
for suppressions must now match both.

## Classification of the 184 findings

Counts are at HEAD config against ruff 0.16.0 over
`parkour_mcp tests scripts .vulture_whitelist.py`.

### Tier 0: dissolved by config, no code change (24)

| Rule | N | Action |
|---|---|---|
| `RUF100` | 24 | Restore the 18 dropped rules. Directives become live again. |

### Tier 1: mechanical, safe autofix, verified green (84)

| Rule | N | Notes |
|---|---|---|
| `I001` | 81 | isort. **Gated on the `PLC0415` hazard above.** |
| `RUF023` | 2 | `__slots__` ordering, `_pipeline.py`, `youtube.py`. |
| `FURB188` | 1 | `str.removesuffix()`, `detection.py:236`. |

Trial run applied these, preserved the `conftest.py` ordering invariant, and the
functional suite stayed green at 1514 passed.

### Tier 2: false positive, fix the config (17)

| Rule | N | Action |
|---|---|---|
| `B018` | 17 | Add `B018` to `per-file-ignores` for `.vulture_whitelist.py` beside the existing `F821`, with a comment naming why (bare name references are the format vulture requires). |

### Tier 3: real defects and real simplifications (36)

Fix at the source. None of these should be suppressed.

| Rule | N | Assessment |
|---|---|---|
| `RUF059` | 14 | Two populations. **4 are genuinely dead code**: `state` / `display_state` unpacked from `_build_issue_markdown` and `_build_pr_markdown` in `github.py` and `_pipeline.py`. The value is already carried inside `extra_fm` (`github.py:1829`, `:1949`) and all four call sites discard it, so the correct fix is to drop the element from the return tuple, not to rename it. The other 10 are test-local unpacking, honest underscore-prefix renames. |
| `ISC004` | 6 | All six are deliberate multi-line prose in list literals, not missing commas. But the rule exists because the two are indistinguishable to a reader, so wrap each in explicit parens. Real PoLA improvement, not a waiver. |
| `FURB162` | 4 | `datetime.fromisoformat(s.replace("Z", "+00:00"))`. `requires-python = ">=3.12"` and `fromisoformat` has handled `Z` natively since 3.11, so the replace is dead. `discourse.py` x3, `github.py` x1. |
| `RUF015` | 4 | Unnecessary iterable allocation for first element. Test code. |
| `PERF102` | 2 | `.items()` where only values are used. |
| `RUF012` | 2 | Mutable class default, `test_kagi.py`. |
| `FURB192` | 1 | `sorted()[0]` to `min()`, `huggingface.py:410`. |
| `PIE810` | 1 | Multiple `endswith` to a single tuple call, `mediawiki.py:527`. |
| `RUF046` | 1 | `int(round(gap))`, `round()` already returns int, `youtube.py:841`. |
| `PLW1510` | 1 | `subprocess.run` without explicit `check=`, `test_semgrep_rules.py:40`. Decide the intended behavior rather than defaulting. |

### Tier 4: needs a decision (22)

| Rule | N | Assessment |
|---|---|---|
| `PLR0917` | 22 | Too many positional arguments. Newly stabilized out of preview. Splits cleanly: **16 internal helpers** where adding a `*` separator is a straightforward improvement, and **6 MCP tool entry points** (`arxiv:445`, `huggingface:1726`, `ietf:579`, `mediawiki:837`, `semantic_scholar:490`, `youtube:2230`) whose arity is pydantic/schema-driven. |
| `EXE001` | 1 | `scripts/regenerate_readme_examples.py` has a shebang but no execute bit. Either `chmod +x` or drop the shebang. Trivial but it is a real inconsistency. |

On the 6 tool entry points: the config already ignores `PLR0913` with the
rationale "arity is schema/pydantic-driven". Extending that ignore to `PLR0917`
would be the easy opt-out and should be resisted. The better fix is to add `*`
so the parameters are genuinely keyword-only, which is how the MCP framework
invokes them anyway. That converts an asserted claim into an enforced one.

**Risk to verify first**: confirm FastMCP builds its schema correctly from a
signature containing `*`. Prove it with one tool before converting all six. If
it breaks, the fallback is a targeted ignore with a reason that names the
framework constraint, not a blanket group waiver.

## Phased implementation

Findings decrease monotonically; the full `uv run pytest` only goes green at the
end, because `pytest-ruff` fails on any outstanding finding. The gate at each
intermediate boundary is therefore the *functional* suite
(`uv run pytest -o addopts="-m 'not live and not perf'"`) plus the expected
finding count. Do not batch phases.

**Phase 1: pin the version, restore the 18, correct the comments.** Declare
`ruff>=0.16.0,<0.17.0` in the dev group (the drift guard). Keep `extend-select`
and add `E4`/`E7`/`E9`/`F` to it, which restores the 18 dropped rules and
retires the 24 `RUF100` findings by making those directives live again. Rewrite
the `TC` comment and the `extend-select` rationale. Expect 184 to 160.

**Phase 2: defuse the `PLC0415` hazard.** Restructure the four over-long
suppressed imports so the directive survives isort rewrapping. Verify: the four
sites still carry `noqa: PLC0415`, each line is under 88 chars, and `PLC0415`
reports nothing. No autofix has run yet at this point.

**Phase 3: config-level false positive.** Add `B018` to `.vulture_whitelist.py`
per-file-ignores, with a comment naming what vulture requires. Expect 160 to 143.

**Phase 4: mechanical autofix.** Run `ruff check --fix`, which resolves `I001`
(81), `RUF023` (2), `FURB188` (1). Verify: `noqa: PLC0415` count unchanged,
`conftest.py` import ordering unchanged, functional suite green. Expect 143 to
59.

**Phase 5: source fixes.** Work `RUF059`, `ISC004`, `FURB162`, `RUF015`,
`PERF102`, `RUF012`, `FURB192`, `PIE810`, `RUF046`, `PLW1510` rule by rule, not
file by file, so each commit carries one reviewable rationale. The `RUF059`
dead-tuple-element removal is a genuine small refactor across two builders and
four call sites and deserves its own commit. Expect 59 to 23.

**Phase 6: `PLR0917`.** Prove the FastMCP keyword-only question on one tool
first, then the 16 internal helpers, then the 6 entry points. Expect 23 to 1.

**Phase 7: `EXE001` and cleanup.** Resolve the shebang. Update `TECH_DEBT.md`:
delete the migration entry, keep the `_matched_meta` entry (unaffected), and
refresh the checker-authority section to mention the `# ruff: ignore` spelling.
Full `uv run pytest` green here.

### Out of band: the `BLE001` audit

Not a phase, because it is not a lint migration. Dropping the `BLE001` ignore
exposes a few dozen `except Exception` handlers that swallow silently in an
async network client, concentrated in `youtube.py`. Some resolve by adding
`exc_info=True` to an existing `logger.debug`; the rest are genuine
error-handling decisions about what should propagate. That is its own branch
with its own review, and it should not be smuggled into a lint sweep. What
belongs in *this* branch is deleting the false comment, since its stated
premise is contradicted by the pinned ruff version.

### Deferred: adopting beyond the default

`D` (pydocstyle), `PT` (flake8-pytest-style), and `N` (pep8-naming) are not in
ruff's 0.16 default set, so `extend-select` does not pull them in and none of
this branch depends on them. Each has a real case, documented in the adversarial
review section below, and each is a separate decision:

- `D` costs 31 on `parkour_mcp/` with `convention = "google"` (not the 1651 that
  an unconfigured repo-wide count suggests), and `D417` in particular enforces
  the one thing a next session cannot reconstruct from partial context.
- `PT` costs 98, dominated by `PT018` composite assertions, which converts an
  ambiguous pytest failure into a located one.
- `N` costs 4 after `extend-ignore-names = ["ET"]`.

## Adversarial review of the exclusions

The `select` plan required a reason for declining each rule group. Those reasons
were put to an adversarial review whose brief was to argue against them and to
look specifically for rules that benefit LLM-assisted development or that an LLM
would be motivated to avoid. Every claim below was then re-verified here rather
than taken on the reviewer's word.

The headline result is that the reasons were unreliable, and in a familiar way.

**`RUF001`-`RUF003`: the stated reason was false.** It claimed the rules had to
be waived because the repo deliberately emits box-drawing fence glyphs. Box
drawing characters have no ASCII confusable and cannot trigger those rules at
all; a probe of `┌─ │ └` returns "All checks passed!". The 16 real findings are
13 × `×` (U+00D7, from prose like "2× baseline") and 3 × `–` (U+2013). Nothing to
do with the fence.

This is the `ec1a81a` failure reproduced one level up: a confident, plausible,
checkable, false justification attached to a suppression, written by the same
process that produced 51 false `noqa` comments, and refutable in under a minute
by anyone who ran the check. Under `extend-select` the carve-out is moot anyway
(these rules are not default-enabled), which is itself the argument: the wrong
posture manufactured a demand for a justification that did not need to exist.

**`N`: the reason described code that is not in the repo.** It cited
`xml.etree.ElementTree as ET` as the blocker. That construct produces no finding
in 0.16 at all, being in ruff's built-in import-convention allowlist. The three
hits are `defusedxml.ElementTree`, cleared by `extend-ignore-names = ["ET"]`.

**`DTZ`: the cited source was misrepresented.** [Issue #27197] was cited to
reject all ten DTZ rules. Its author, a CPython `datetime` maintainer,
explicitly recommends keeping `DTZ002`, `DTZ003`, and `DTZ004`. A critic was
used to go further than the critic asked, on a group that costs zero here.

**`D`: priced with the wrong economics.** The 1651 headline is dominated by test
files already covered by `per-file-ignores`, and no `convention` was set. On
`parkour_mcp/` with `convention = "google"` it is 31.

**`TC`, `ASYNC109`, `PERF401`: conclusions sound, reasons weak.** The `TC`
hazard is real but better evidenced by the MCP SDK's `eval_str=True` signature
resolution than by the comment's phrasing. `PERF401`'s exclusion survives on
[#21891] (open, labelled `bug`, benchmarks showing the suggestion is slower),
which is a stronger argument than "ruff dropped it".

### The methodology had a blind spot

Pricing candidate rules by marginal finding count cannot see rules that fire
zero times, because they generate no wall of findings to price. Verified free on
this codebase right now: `PLE` (the pylint **error** category, while `PLR`, the
refactor one, was already selected), `ASYNC100`/`ASYNC210`/`ASYNC221`/`ASYNC230`
(blocking calls in async functions, on a codebase that is httpx-async end to
end), `RUF006` (dangling asyncio task, directly relevant to the Reddit token
refresh daemon), `W605`, `T100`, `RUF013`.

All are in the 0.16 default set, so `extend-select` adopts every one of them at
no cost and no argument. Under the `select` plan each would have had to be
noticed, justified, and listed, and none of them was. A review organised by
marginal cost is structurally blind to the cheapest coverage available, which is
the mirror image of the `D` error, where the same method overweighted a number
that one config line dissolves.

## Verification gates

- `uv run pytest` green at each phase boundary (this runs ruff and ty via
  `pytest-ruff` / `pytest-ty` in `addopts`, so lint is part of the suite).
- `just lint-deep` before the final commit, since vulture gates tag pushes and
  the `RUF059` tuple change removes a returned value it may have opinions about.
- Suppression count audit at the end, grepping both `noqa:` and
  `ruff: ignore[` spellings, confirming each surviving directive still
  suppresses something real (`RUF100` enforces this automatically once the rule
  set is pinned).

## Reproduction

```bash
uv lock --upgrade-package ruff && uv sync
uv run ruff check parkour_mcp tests scripts .vulture_whitelist.py --statistics
```

To re-derive the dropped-rule list:

```bash
for v in 0.15.8 0.16.0; do
  uvx ruff@$v check --isolated --show-settings probe.py \
    | sed -n '/^linter.rules.enabled = \[/,/^\]/p' \
    | grep -oE '\([A-Z]+[0-9]*\)' | tr -d '()' | sort -u > r_$v.txt
done
comm -23 r_0.15.8.txt r_0.16.0.txt   # the 18
```

[PR #7900]: https://github.com/astral-sh/ruff/pull/7900
[#27177]: https://github.com/astral-sh/ruff/issues/27177
[#27195]: https://github.com/astral-sh/ruff/issues/27195
[#27145]: https://github.com/astral-sh/ruff/issues/27145
[#27149]: https://github.com/astral-sh/ruff/issues/27149
[Issue #27199]: https://github.com/astral-sh/ruff/issues/27199
