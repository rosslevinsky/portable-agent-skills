---
name: plan-phase
description: >
  Work breakdown for a plan — the current planning suite. Reads a `Format: v2`
  plan.md, proposes an ordered phase list (noting in prose which phases are
  independent and which reconciles them), then writes one phase document per phase
  plus the checkbox execution.md tracker (never phases.md). UI
  phases get a spec item and a visual-verify gate box; every phase carries a compact
  evidence record. Use when the user invokes /plan-phase, or says "break this plan into
  phases", "WBS the plan", "create the execution.md tracker".
---

# Work Breakdown Structure (v2)

## Overview

Read a `Format: v2` plan document, propose an ordered **phase list** for approval, then
produce v2 phase documents and an **`execution.md`** tracker. The original plan is
never modified.

Two things distinguish this from `/plan-phase-v1`:

1. **The phase document is the durable state; the tracker is a derived index.** Every
   item of Work, every Test and every Gate box in a phase document is a checkbox ticked as
   the work proceeds, and `execution.md` holds one checkbox per phase, ticked when that phase's
   gate passes. There are no status values and no scheduling keys anywhere: two levels of
   record, the child authoritative, is what makes a crash at any point recoverable.
   Phases that are genuinely independent are noted in **prose** above the list; that is
   advisory and is not parsed.
2. **A distinct tracker filename — `execution.md`, never `phases.md`.** With `plan.md`'s
   `Format: v2` marker this is the entire v1↔v2 non-collision mechanism: v2 skills act
   only on `Format: v2` plans and read/write `execution.md`; the v1 skills only ever
   touch `phases.md`.

Each phase is independently committable and sized for review cost. Every phase it emits carries a
per-phase gate whose independent-review axis is a **cross-model** `diff-review` when a second
runtime is installed (Codex reviewing Claude's code or the reverse — see Step 5), giving at review
time the same different-model scrutiny `/plan-duel` gives at plan time. The document
contract (phase-document shape and tracker structure) is defined in
`references/v2-templates.md` — emit against it exactly. Where the pack's tracker check is
present in the repository you are working in, `scripts/check_plan_tracker.py` machine-checks
what you emit; it is not installed with the skills, so in another project it will not be
there and the templates are the whole contract.

---

## Step 1 — Locate the plan and confirm it is v2

If the user provided a path argument, use it. Otherwise search for `plans/**/plan.md`;
if exactly one exists use it, else list them and ask which to break down (autonomously:
choose the only `Format: v2` plan, or the most recently modified one, and note the
assumption). Read the plan in full.

**Confirm the `Format: v2` marker** — the `| Format | v2 |` row in the Status table.
If it is absent, this is a v1 plan: do **not** proceed. Tell the user to run
`/plan-phase-v1` instead (which writes `phases.md`). If operating autonomously, stop and
report: "plan is not `Format: v2` — refusing to avoid a v1/v2 tracker collision."

**Output directory:** phase documents and `execution.md` are always written into the
**same directory as the plan file you just read** — every `plans/<slug>/` path below names
that directory, whatever it turns out to be. Discovery above accepts a plan anywhere, so
when the plan lives outside `plans/` (say `docs/proposal.md`) the phase documents and
`execution.md` go beside it, in `docs/`, never into an invented `plans/<slug>/`. That is
where `/plan-run` looks: it reads `execution.md` from the plan's own directory, so a
tracker written anywhere else is a tracker nothing will ever find.

---

## Step 2 — Explore for breakdown context and shared surfaces

Explore the plan's affected areas in the codebase (read the key files, find natural
seams, note existing tests and migration concerns), verifying every path against
reality. In addition, so you can say honestly which phases are independent, identify:

- The **affected-file set** each prospective phase would touch.
- **Shared surfaces** — coupling that hides behind disjoint file lists: shared
  APIs/contracts, schema/migrations, generated files, lockfiles, global/theme CSS,
  shared test fixtures, and shared test-env state.

---

## Step 3 — Design the phase list (internal)

Design the phases first (foundation before consumers; tests mandatory per phase;
risky changes isolated; a final verification-gate phase). **Every phase that introduces
new behaviour is test-first:** for logic, API endpoints, and utilities the failing test
is written before the implementation (TDD); for UI and wiring, tests are written
alongside — no phase is complete until its tests pass. This ordering flows into each
emitted phase doc's Tests section (the template's test-first note is load-bearing —
keep it). Then decide which, if any, are genuinely **independent**.

**Independent phases are rare, and that is correct.** Almost every phase executed so far
has been a single sequential unit. Note independence when it is real; do not reach for it
to signal that work is parallelisable in principle. The test:

- Phases are independent only if their affected-file sets are **disjoint** *and* none of
  them shares a surface with another. **Disjoint files is a filter, not proof of
  independence** — any shared surface means run them in order, because isolating writers
  covers *files* and cannot reconcile shared non-file state like a DB, ports, or caches.
- Where phases are independent, **say so in prose above the phase list**, and name the
  phase that **reconciles** them. That phase's own document carries the reconciliation:
  its Work brings the work together and its Verification runs the full suite. Nothing
  about this is a key in the tracker.
- A phase that changes the **build / dependency-resolution / CI topology** can't be
  verified in the dev environment alone (resolution and hoisting differ from CI); make
  **"the CI round for the commit this phase ends at is green"** one of its Gate boxes —
  naming the repository and ref it runs in — and keep it an ordinary sequential phase.

Size each phase so its diff is cheap to review — reviewability is a first-class
constraint. Target 3–8 phases.

---

## Step 4 — Propose the breakdown and get approval

**Before writing any files**, present the proposed phase list in one message: the
ordered phases; which (if any) are independent of one another and why (disjoint files,
no shared surface); which shared surfaces forced sequencing; and which phase reconciles
each independent set. Invite the user to add, remove, merge, re-order, or re-scope
phases, or to make an independent set strictly sequential.

Wait for approval; revise and re-present until confirmed. If operating autonomously
(no user available), proceed with the proposed graph and note that it was not
user-reviewed.

---

## Step 5 — Write phase documents

**Before writing anything into `plans/<slug>/`, check whether that plan directory already
holds an execution tracker *or any* `phase-*.md`.** This check belongs here, ahead of the
first write — not beside the tracker write in Step 6 — because Step 5 recreates every phase
document. A guard that only protects `execution.md` still lets a literal execution destroy a
finished plan's phase documents and their evidence before it stops. If
`plans/<slug>/execution.md` exists, **or the directory holds any `phase-*.md`**, do not
overwrite anything. Three cases, all destructive to get wrong:

- **It is a checkbox tracker** — the plan is live and possibly mid-run; its unticked
  boxes are exactly where the run resumes. Overwriting resets that progress. Ask the user
  whether to re-plan or resume; autonomously, stop and report rather than discarding
  execution state.
- **It is a superseded `- phase:` tracker** — the directory is the finished record of a run
  executed under an older shape. Writing fresh documents over it destroys that record.
  Leave it alone; start a new plan directory instead.
- **`phase-*.md` documents with no tracker beside them at all** — an earlier run of this
  skill stopped between Step 5 and Step 6. This is the case a tracker-only guard waves
  straight through, and the one where the damage is worst: with no tracker there is nothing
  to resume from, so nothing looks wrong until the evidence records are already overwritten.
  Stop and ask; do not assume an unfinished directory is a scratch one.

For each approved phase N, create `plans/<slug>/phase-<NN>-<name>.md` from the **phase
document template** in `references/v2-templates.md`. Fill:

- **Goal / Work / Tests / Verification / Gate / Evidence**, using real codebase paths.
  **Verification commands are scoped to the phase's changed surface** — the full suite
  appears only in a reconciling phase or a final verification-gate phase, never in an
  ordinary phase. Include the phase's cheap per-surface static checks (linter check-only,
  and a scoped type-check where the project has one); defer only genuinely
  environment-bound checks to CI.
- **Emit only the boxes that apply.** The Gate section always carries the two review boxes —
  author review completed, independent review with no open blocker/major — and those two are
  ticked with a reason even when the phase produced no reviewable diff. A **UI** phase also
  gets a spec item under Work and a visual-verification box (run the `web-verify` skill;
  where unavailable, its bundled manual checklist across the mandatory viewports). A phase
  whose verification needs the **CI/prod environment shape** also gets a CI box naming the
  repository and ref — that is the whole mechanism, and it needs no tracker state. A phase
  with neither gets neither: an inapplicable box is one nobody can tick, and `plan-run`
  reads any open box as unfinished work.
- **Never emit a box that restates another, or one that cannot be true.** No "every task
  above is checked off", no universal "the previous phase is done" (the tracker's order
  already says it), and never "this phase's box is ticked in `execution.md`" — `plan-run`
  reads the document to decide that, so as a criterion it is circular.
- **Every box is ticked as it becomes true, never in a batch at the end.** Say so explicitly
  in any phase whose Work includes an irreversible action (creating a tag, a release, a PR),
  and write that item so its outcome is *checkable*: on resume, `plan-run` looks for the
  outcome before repeating the action.

---

## Step 6 — Write the `execution.md` tracker

Create `plans/<slug>/execution.md` (never `phases.md`) from the **tracker template** in
`references/v2-templates.md`: one checkbox per phase, in execution order, each linking its
phase document. It carries **no** format marker, no status values and no scheduling keys.
It must satisfy the validator's rules:

- every column-0 checkbox line is the exact canonical form —
  `- [ ] [<Name>](./phase-<NN>-<slug>.md)`, or the same with `- [x] `, where `<NN>` is at
  least two digits and `<slug>` is kebab-case;
- each link resolves to an existing regular file in the same plan directory — not a
  symlink — and no phase is listed twice;
- every `phase-*.md` entry in that directory appears in the list exactly once;
- there is **at least one checkbox** — zero is a hard error, never "all phases complete";
- no code fence or HTML comment appears anywhere in the file. They are banned rather than
  parsed: either can hide a whole region from the checker while a reader sees it plainly.

Which phases are independent, and which phase reconciles them, goes in **prose above the
list**. It is advisory and is not parsed. Do not use a `###` sub-heading for it — a blank
line does not end a `###` section, so the reconciling phase would read as another member.

**Put every checkbox under the one `## Phases` heading.** Neither the checker nor `plan-run`
parses that heading — both read every column-0 checkbox in the file, in order — so a box
placed under some other heading is executed anyway, in list position, which is unlikely to
be what its author meant.

The orchestrator owns this file; phases never write it.

**Machine-check the tracker before finishing.** If the pack's tracker check is present in
this repo (you are working inside portable-agent-skills), run it against the file you
just wrote and fix every reported issue before continuing:

```bash
# native Windows: py -3 instead of python3
python3 scripts/check_plan_tracker.py plans/<slug>/execution.md
```

**One of its findings has a destructive wrong fix.** `'<name>' is in this directory but no
checkbox links it` names a `phase-*.md` the tracker does not reference. Fix it by **adding
the checkbox**, in that phase's right position. Never delete the document to silence the
check: if you did not write it this run it belongs to a plan you have not read, and deleting
it discards that plan's evidence record. If you cannot place it confidently, stop and ask.

If the tracker check is not present (the usual case when running inside another
project), re-read the tracker against the rules above and confirm each by hand — the emitted tracker
must satisfy the same contract either way.

---

## Step 7 — Report to the user

Print a summary: the location of `execution.md` and the phase files; a one-line
description of each phase (and which, if any, are independent of one another); the
total checklist items; and the next step: "Run `/plan-run <the plan file you read>` to
execute the phases." Name its real path — a literal `plans/<slug>/plan.md` points the
runner at a file that does not exist whenever the plan came from anywhere else.

## References

- `references/v2-templates.md` — the v2 phase-document template (Goal / Work / Tests /
  Verification / Gate / Evidence, and what is deliberately not in it) and the `execution.md`
  checkbox tracker template.
