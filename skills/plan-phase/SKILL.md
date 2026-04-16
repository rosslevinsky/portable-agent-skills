---
name: plan-phase
description: >
  Work breakdown structure: reads a plan document and breaks it into ordered
  phases. Proposes the phase structure to the user for approval, then creates
  a phase document (with checklist) for each phase and writes a new phases.md
  execution tracker alongside the plan. The original plan document is never
  modified. Use when the user invokes /plan-phase, or says "break this down into
  phases", "create the phase documents", "do the work breakdown", "WBS this plan".
  Argument: path to a plan.md file (e.g. plans/auth-refactor/plan.md).
  If no argument, scan plans/ for plan.md files and ask which one.
---

# Work Breakdown Structure

## Overview

Read a plan document, analyze the scope, propose a phase structure for approval,
then produce phase documents and a `phases.md` execution tracker. The original
plan document is left untouched — it is a stable reference.

Each phase must be independently committable — it should leave the codebase in a
working state. Phases flow forward: earlier phases unblock later ones, never the reverse.

---

## Step 1 — Locate the plan

If the user provided a path argument, use it. Otherwise:
- Search for plan files matching `plans/**/plan.md`
- If exactly one exists, use it
- If multiple exist, list them and ask the user which one to break down.
  If operating autonomously, choose the only incomplete plan; if all are
  incomplete, choose the most recently modified one and note the assumption.
- If none exist, tell the user to run `/plan-init <task>` first, or provide any
  markdown file containing a goal and scope description

Read the plan document in full.

### Handling non-plan-init input

If the file was not produced by `/plan-init`, look for equivalent content:

| Expected section | Acceptable equivalents |
|---|---|
| Goal | Objective, Summary, Overview, Problem Statement |
| Success Criteria | Acceptance Criteria, Definition of Done, Tests |
| Affected Areas | Scope, Files, Components, Modules |
| Technical Constraints | Constraints, Requirements, Technical Notes |

If a section is missing entirely, note what couldn't be found and either:
- Ask the user to clarify (if the gap is material — e.g. no goal at all)
- Make a reasonable inference and proceed, noting the assumption

Do not refuse to proceed just because the format differs from plan-init output.

---

## Step 2 — Explore for breakdown context

Based on the plan's affected areas (or equivalent scope section), explore the codebase:
- Read the key files that will change
- Identify natural seams: what can be done independently, what has dependencies
- Look at existing test files to understand what tests currently exist vs. need to be written
- Note any migration concerns (DB schema, API contracts, translations, etc.)

**Verify every assumption against the actual codebase before designing phases.**
Do not assume a file exists or a path is correct — check it. If the plan's Affected
Areas list contains paths that don't exist or have moved, note the discrepancy.

Use this exploration to inform phase granularity. Phases should be:
- Small enough to commit independently (1–6 hours of focused work)
- Large enough to be meaningful (not a single line change unless it's a critical gate)
- Ordered so each builds on the last without breaking anything

---

## Step 3 — Design the phases (internal)

Think through the full sequence before presenting anything to the user. Consider:

1. **Foundation first** — schema changes, new models, interface definitions before their consumers
2. **Tests are mandatory, not optional** — every phase that introduces new behaviour must include
   tests for that behaviour. For API endpoints, business logic, and utilities: prefer TDD
   (failing test written before implementation). For UI and wiring code: tests written
   alongside. No phase is complete without its tests passing.
3. **Backend before frontend** (usually) — or at least the API contract before the UI
4. **Risky changes isolated** — put anything with blast radius in its own phase
5. **Final phase = verification gate** — last phase runs all success criteria from the plan

Target 3–8 phases. Too few means each phase is too risky; too many means overhead.

For each phase determine:
- A short human-readable name (title case, e.g. "Add Schema Migration")
- A kebab-case filename slug (e.g. `add-schema-migration`)
- A one-sentence goal
- Entry criteria: what must be true before this phase can start
- A rough list of tasks (3–8 bullet points — specific but not yet fully detailed)
- Exit criteria: the formal definition of done for this phase (separate from the task checklist)
- The verification command(s) to run at the end

---

## Step 4 — Present proposed phases and ask for approval

**Before writing any files**, show the user the proposed breakdown in a single message:

```
Here's the proposed breakdown for "<plan title>" — N phases:

Phase 1: <Name>
  Goal: <one sentence>
  Entry: <what must be true before this phase starts>
  Tasks: <3–5 bullet points of what this phase covers>
  Verify: <test command>
  Exit: <formal done criteria>

Phase 2: <Name>
  ...

Does this breakdown look right? A few things you can tell me:
- Add, remove, or merge phases
- Move tasks between phases
- Change the scope of any phase
- Adjust the order

Reply "looks good" to proceed, or describe any changes.
```

Wait for the user's response. If they request changes, revise the breakdown
and re-present it. Repeat until they confirm. Do not create any files until
the user approves the structure.

If operating autonomously (no user available), proceed with the internally
designed phase breakdown and note the assumption that it was not user-reviewed.

---

## Step 5 — Write phase documents

For each approved phase N, create `plans/<slug>/phase-<NN>-<name>.md`
(zero-padded number, e.g. `phase-01-add-schema-migration.md`).

Each phase document uses this structure:

~~~markdown
# Phase <N>: <Human-Readable Name>

_Status: pending_

## Goal

<One sentence: what this phase accomplishes and why it matters.>

## Entry Criteria

Before starting this phase, confirm:
- [ ] <Prior phase committed, pushed to origin, and verified, or "This is the first phase">
- [ ] <Any specific precondition — e.g. "migration X applied", "API shape agreed">

## Tasks

- [ ] <Specific task: what file, what change, what outcome>
- [ ] <Another specific task>
- [ ] <...>

## Tests

_For logic, API endpoints, and utilities: write failing tests before implementation (TDD).
For UI and wiring: write tests alongside the code._

- [ ] `<path/to/test_file>` — <what behaviour it covers>
- [ ] <additional test file if needed>

## Verification

Run these after completing all tasks:

```bash
<test command 1>
<test command 2 if needed>
```

Also verify manually:
- <Specific thing to check that tests won't catch>

## Exit Criteria

This phase is complete only when ALL of the following are true:
- [ ] Every task above is checked off
- [ ] All tests listed in the Tests section are written and passing
- [ ] No previously passing tests have regressed
- [ ] All verification commands pass with no failures
- [ ] Run the `cyw` skill (or equivalent manual review) — finds zero issues
- [ ] <Any additional condition specific to this phase>
- [ ] phases.md phase checkbox updated to `[x]`

## Commit

```
<suggested commit message — imperative, under 72 chars>
```
~~~

Use real file paths from the codebase. Tasks should be specific enough that another
engineer could follow them without re-reading the plan. Follow all conventions
defined in the project's `CLAUDE.md` / `AGENTS.md` (whichever exists).

---

## Step 6 — Write phases.md

Create `plans/<slug>/phases.md` — the execution tracker. Do not modify plan.md.

~~~markdown
# Phases: <human-readable title>

_Execution tracker for [`plan.md`](./plan.md)_

## Status

| Field | Value |
|---|---|
| Phase | Phase 1 of N — <Phase 1 Name> |
| State | Ready to execute |
| Blocker | None |
| Last updated | <date> |

## Phases

- [ ] [Phase 1: <Name>](./phase-01-<name>.md)
- [ ] [Phase 2: <Name>](./phase-02-<name>.md)
- [ ] ...
~~~

---

## Step 7 — Report to the user

Print a summary:
- Location of `phases.md` and number of phase files created
- One-line description of each phase
- Total checklist items across all phases
- Next step: "Run `/plan-run plans/<slug>/plan.md` to execute all phases."
