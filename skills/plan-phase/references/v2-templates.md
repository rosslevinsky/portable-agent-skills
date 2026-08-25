# Templates — phase documents and the `execution.md` tracker

This is the detail behind `plan-phase`. Emit against these templates exactly. This file is
the **schema of record** for the execution tracker. Where the pack's tracker check is present
in the repository, `scripts/check_plan_tracker.py` machine-checks that shape — it is not
installed with the skills, so elsewhere this file is the whole contract.

The design in one line: **the phase document is the durable state, and `execution.md` is a
derived index.** Every item of Work, every Test and every Gate box in a phase document is a
checkbox ticked as the work proceeds; the tracker holds one box per phase, ticked when that
phase's gate passes. Two levels of record, the child authoritative — which is what makes a crash at
any point recoverable without any status field.

---

## Phase document template

Each `phase-<NN>-<name>.md` uses this structure. **Emit only the sections and boxes that
apply** — a phase with no UI gets no UI box, a phase gated by no CI round gets no CI box. An
inapplicable box is indistinguishable, to a resumed run, from work that never happened.

~~~markdown
# Phase <N>: <Human-Readable Name>

## Goal

<One or two sentences: what this phase accomplishes and why it matters.>

## Work

- [ ] <Specific item: what file, what change, what outcome>
- [ ] <A genuine prerequisite to check first is an item too — but never "the previous phase
      is done", which the tracker already says>
- [ ] <For a UI phase: write/extend the UI spec that drives the changed flow>

## Tests

_Logic, APIs and utilities: write the failing test first. UI and wiring: alongside._

- [ ] `<path/to/test_file>` — <behaviour it covers>

## Verification

_Scoped to this phase's changed surface, run once after the Work boxes are complete.
Include the cheap per-surface static checks (linter check-only; a scoped type-check where
the project has one). The full suite belongs only to a reconciling phase or a final
verification-gate phase._

```bash
<command(s) scoped to this phase's changed surface>
```

<Anything tests cannot catch, if there is any.>

## Gate

- [ ] Author review completed.
- [ ] Independent review has no open blocker/major.
- [ ] <UI phases only> Visual verification passed across the mandatory viewports.
- [ ] <Only where a CI round gates this phase> CI green — `<repository>` `<ref>`, the commit
      this phase ends at. Ticked after the push, never before.

## Evidence

_Filled at the gate; what a human reads and what `as-built.md` is assembled from. The
independent review reads the diff, not this._

- **Outcome:** <shipped / partial / skipped, and in a sentence what the phase actually
  delivered, plus any risk worth watching. This is the line `as-built.md` reads.>
- **Changed:** <files / modules touched>
- **Verified:** <commands run and their outcomes>
- **Deviations:** <doc-vs-reality corrections made during execution, or "none">
- **Follow-ups:** <non-blocking minors/nits, and any post-cap blocker/major disposition —
  fixed, or refuted-with-evidence — or "none">
- **Artifacts:** <frames / screenshots / reports, by path or CI URL only — never inline,
  never committed>
~~~

That is the whole document: **under 350 words of structure before any work goes into it.**
Everything cut from earlier versions was cut for one of three reasons.

- **It restated a box above it.** "Every task above is checked off", "all tests pass", "all
  verification commands pass" — a summary box is true exactly when the boxes it summarises
  are, so it adds a second record to keep in step and nothing else.
- **It was circular.** "This phase's box is ticked in `execution.md`" cannot be true when
  `plan-run` reads the document to decide whether to tick that box.
- **It was boilerplate the phase did not need.** A universal "prior phase completed" entry
  box duplicates the tracker's ordering; a UI or CI criterion on a phase with neither is a
  box nobody can tick; a suggested commit message is written better at commit time, from the
  Goal and the evidence, than guessed at breakdown time.

**No status line and no field block.** The checkboxes *are* the status, at finer grain than
any word: a partly-ticked document is what "in progress" means, and it is what a resumed run
reads to decide whether to re-execute the phase or simply tick its tracker box.

**Every box is read the same way** — Work as each item is done, Tests as each passes, Gate as
each axis clears, including an axis legitimately *skipped*, which is settled work and is
ticked with the reason. `plan-run` reads any open box as unfinished, so a box nobody was
assigned to tick makes a finished phase look incomplete and gets it re-executed. Tick each as
it becomes true, never in a batch at the end.

A phase whose verification can only run in the CI/prod environment shape needs no special
machinery: give it the CI box above and **name the repository and ref**, since a phase whose
own Work pushes a different repository is not gated by this one's round. A crash while
waiting leaves that box unticked, so a resumed run re-checks the round instead of treating
the phase as finished.

**A ticked phase document in an older shape is a finished record.** Never re-emit or
revalidate one to match this template: its boxes are ticked, its work is settled, and its
filled "Review Packet" is the same evidence this template now calls **Evidence**.
`as-built.md` assembles from both without translation.

---

## `execution.md` tracker template

The tracker is one checkbox per phase, in execution order, each linking its phase document.
It holds no status values, no scheduling keys and no format marker.

~~~markdown
# Execution: <human-readable title>

_Execution tracker for [<plan filename>](./<plan filename>). The orchestrator owns this file._

## Phases

<Optional prose: note here which phases are independent of one another, and which phase
reconciles them. This is advisory and is not parsed.>

- [ ] [Phase 1: <Name>](./phase-01-<name>.md)
- [ ] [Phase 2: <Name>](./phase-02-<name>.md)
- [ ] [Phase 3: <Name>](./phase-03-<name>.md)
~~~
