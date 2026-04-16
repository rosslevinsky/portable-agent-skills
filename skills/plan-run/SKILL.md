---
name: plan-run
description: >
  Execute a project plan produced by /plan-init + /plan-phase. Reads the master
  plan and phases.md execution tracker, identifies incomplete phases, and executes
  each phase in order: checks entry criteria, completes tasks, runs verification,
  runs /cyw, marks the phase done in docs, reviews the staged diff, then commits
  and pushes to origin. Updates phases.md status block throughout. Skips
  already-completed phases so it is safe to restart. Use when the user invokes
  /plan-run, or says "execute the plan", "run the plan", "start the plan",
  "continue the plan". Argument: path to a plan.md file (phases.md is inferred
  from the same directory). If no argument, scan plans/ and ask which plan to run.
---

# Execute Plan

## Overview

Work through a phased plan created by `/plan-init` + `/plan-phase`. Execute each
incomplete phase in sequence. Never skip a phase. Commit and push to origin at the
end of each phase. Update `phases.md` status block and checkboxes throughout so
progress is always visible and the plan is restartable.

**Authority rule:** The phase documents are the execution plan. The actual codebase
is the reality check. If a phase document references paths, APIs, or structures that
don't match the codebase, do not blindly follow the stale doc — correct the doc first
(smallest accurate correction), then continue execution against the corrected doc.

---

## Step 1 — Locate the plan

If the user provided a path argument, use it. Otherwise:
- Search for plan files matching `plans/**/plan.md`
- If exactly one exists, use it
- If multiple exist, list them and ask the user which one to run.
  If operating autonomously, choose the only incomplete plan; if all are
  incomplete, choose the most recently modified one and note the assumption.

Read the master `plan.md` in full.

Then locate `phases.md` in the same directory as `plan.md`. If `phases.md` does not
exist, stop and tell the user:
"This plan has not been broken down into phases yet. Run `/plan-phase <path>` first."

Read `phases.md` in full.

---

## Step 2 — Survey the phases

Parse the Phases section of `phases.md` and separate into:
- **Complete** — lines starting with `- [x]`
- **Incomplete** — lines starting with `- [ ]`

Print a brief status report:
```
Plan: <title>
Phases complete:   N / M
Next phase:        Phase X — <Name>
```

If all phases are complete, skip to Step 4 (final verification).

---

## Step 3 — Execute each incomplete phase

For each incomplete phase, in order from first to last:

### 3a — Re-read before starting

At the start of every phase — even if you just finished the prior one — re-read:
1. `phases.md`
2. The phase document for the phase you are about to start

Do not rely on memory. Re-reading ensures you're working from the current state of
both documents, not a stale mental model.

### 3b — Check entry criteria

Read the Entry Criteria section of the phase document. Confirm every item is true
before doing any work. If an entry criterion is not satisfied, stop and report it
rather than proceeding.

### 3c — Verify assumptions against the codebase

Before editing any file, verify that the paths, APIs, and structures referenced in
the phase doc actually exist as described. Search for the referenced files and
check their contents.

If the phase doc diverges from reality:
1. Make the smallest accurate correction to the phase doc (or phases.md) first
2. Document what changed and why, in a comment or note in the doc itself
3. Then continue execution against the corrected doc

### 3d — Complete the tasks

Work through every task in the checklist. For each task:
- Do the actual work (edit files, write code, etc.)
- After completing the task, update the phase document to check it off:
  Change `- [ ]` to `- [x]`

Follow all conventions defined in the project's `AGENTS.md`/`CLAUDE.md` (or
equivalent). If no agent-instruction file exists, follow the existing patterns found
in the codebase.

**Testing discipline:** For every task that introduces new behaviour:
- API endpoints, business logic, utilities: write the failing test first (TDD),
  then implement until it passes. Use the `tdd` skill if available; otherwise
  follow TDD discipline manually.
- UI and wiring code: write tests alongside the implementation.
- No new behaviour ships without tests. This is not optional.

After any path, module, or file move: run a repo-wide search for stale references
(imports, scripts, configs, docs) and update them before moving on.

### 3e — Run verification

Run every command listed in the phase's Verification section. If any command fails:
1. Diagnose the failure (read error output, check affected files)
2. Fix the issue
3. Re-run until all verification commands pass
4. Do not proceed until verification is clean

### 3f — Check your work

Run the `cyw` skill now. This performs a structured review of everything changed
in this phase: correctness, completeness, consistency, integration, and test
coverage. If the `cyw` skill is unavailable, perform the equivalent review
manually: re-read all modified files, check correctness/completeness/consistency,
and fix any issues found.

Treat every issue found as a blocker. Fix all issues before proceeding.
Do not commit until the review finds zero issues or explicitly clears the phase.

### 3g — Pre-completion gate

Before updating any docs or committing, confirm all four are true:

1. **Tasks complete** — every task in the phase checklist is checked off.
2. **Tests written and passing** — every test listed in the phase's Tests section exists,
   passes, and no previously passing tests have regressed.
3. **Verification passed** — all verification commands ran clean.
4. **Review clean** — `cyw` skill (or manual review) found zero issues.

### 3h — Mark phase complete

Update the phase document:
- Change `_Status: pending_` to `_Status: complete_`

Update `phases.md`:
- Change the phase line from `- [ ]` to `- [x]`
- Update the Status table: set Phase to the next phase (or "All complete"), set
  Last updated to today's date, clear any Blocker

Then return to the phase document and check off every item in the Exit Criteria section.

### 3i — Commit the phase

```bash
git add -A
git diff --staged
git commit -m "<commit message from phase document>"
git push origin HEAD
```

Review the staged diff before committing: confirm every change (code and docs) matches
what the phase was supposed to do. No stray edits, no debug code, no unintended files.
If anything unexpected appears, stop, fix it, and re-stage before committing and pushing.

If the phase document's commit message needs minor adjustment (e.g., the actual
scope changed slightly), update it to accurately reflect what was done.

### 3j — Report and continue

Print a one-line progress update:
```
✓ Phase X complete. (N of M done)
```

Then proceed immediately to the next incomplete phase. Do not pause between phases
unless a blocking issue was encountered.

---

## Blocking behavior

If anything blocks completion of a phase:
- Stop. Do not advance to the next phase.
- Update the `phases.md` Status table:
  - State → "Blocked"
  - Blocker → precise description: what is blocked, what was verified, what decision is needed
- Report to the user with the same detail.

Only resume after the blocker is resolved. If a narrow, justified decision resolves
the blocker safely, make it, document it in the phase doc or phases.md, then continue.

---

## Step 4 — Final verification (all phases complete)

When all phases are checked off, run the full Success Criteria from `plan.md`.
For each criterion:
- Run the specified command or check the specified condition
- Note pass / fail

---

## Step 5 — Update phases.md status

Update the Status table in `phases.md`:
- Phase → "All phases complete"
- State → "Complete"
- Blocker → "None"
- Last updated → today's date

---

## Step 6 — Final report

Print a completion summary:
- Total phases completed (this session vs. previously)
- Success criteria results (pass/fail for each)
- Any unresolved concerns or follow-up items discovered during execution

If any success criteria failed, list them explicitly and suggest next steps.
