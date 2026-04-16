---
name: plan-and-do
description: >
  Lightweight planning + execution in a single pass. For smaller jobs that don't
  warrant a full plan-init / plan-phase / plan-run workflow. Interviews the user
  briefly, explores the codebase, prints a checklist, executes it, runs /cyw, then
  commits. Use when the user invokes /plan-and-do, or says "plan and do", "just do
  it with a plan", "quick plan then execute". For large feature sets with multiple
  independently committable phases, use /plan-init + /plan-phase + /plan-run instead.
---

# Plan and Do

## Overview

Understand the task, make a short plan, execute it, verify, and commit — all in one
pass. No phase breakdown, no separate approval step for structure, no mid-task
interruptions unless something is genuinely blocked.

If at any point during execution you find the scope has grown beyond ~8 checklist
items, or you need multiple independent commits, stop and tell the user: "This has
grown larger than expected — I'd recommend switching to `/plan-init` for a proper
phased breakdown." Do not silently continue into a large unstructured commit.

---

## Step 1 — Understand the task

The user's argument (everything after `/plan-and-do`) is the task description. If no
argument was given, ask what needs to be done and wait. If operating autonomously
(no user available) and no argument was given, stop and report: "No task description
provided — cannot proceed autonomously without input."

Parse what the user has already told you:
- Core objective
- Any explicit constraints or non-goals
- Any explicit done criteria

---

## Step 2 — Ask one round of clarifying questions (if needed)

In a **single message**, ask only what is genuinely unclear. Cover:

- **Done criteria** — how will you know it's working? Any specific test or manual
  check that must pass?
- **Hard constraints** — any technical decision already made that can't change?
- **Scope limits** — anything this should explicitly not touch?

If the task description already answers all of these, skip this step entirely and
proceed to Step 3.

Wait for the user's answer before proceeding. If operating autonomously (no user
available), proceed with reasonable assumptions based on the task description and
note each assumption explicitly.

---

## Step 3 — Check for framework/library standards

Before exploring the main codebase:

1. Read `AGENTS.md` and `CLAUDE.md` in the current repo (if present).
2. Check `package.json`, `pyproject.toml`, or other project config for sibling
   framework repos or local standards. For each sibling found:
   - Read its `docs/technical-standards.md` if it exists
3. If the task involves building on a sibling framework, check for a canonical
   demo or example directory.
4. Skim project config for quality gates that run at commit time: pre-commit
   hooks (`.husky/pre-commit`, `lint-staged` in `package.json`,
   `.pre-commit-config.yaml`) or validation scripts like `scripts/validate.sh`.
   Knowing what will run helps you avoid being surprised at Step 9.

These documents take precedence over patterns inferred from reading code alone.

---

## Step 4 — Explore the codebase

Read the files relevant to the task. Identify:
- Files that will change
- Files that are important context (callers, tests, dependencies)
- Existing patterns and conventions to follow
- Existing tests covering the affected area

Search the codebase for relevant files and read them. Do not skip this step.

---

## Step 5 — Present the checklist and proceed

Print the plan to the user in this format:

```
Goal: <one sentence>

Checklist:
- [ ] tests: <failing test for X behaviour — write this first>
- [ ] <implement X — what file, what change, what outcome>
- [ ] <another task with no new behaviour, no tests: item needed>
- [ ] ...

Done when:
- <specific verifiable condition, e.g. test command>
- <another condition>
```

Keep the checklist to 3–8 items. Each item should be specific enough to act on
without re-reading context. For any change that introduces new behaviour, include a
`tests:` item placed immediately **before** the implementation task it covers — this
enforces TDD order (failing test first, then implementation).

If the checklist would need more than ~8 items, or requires multiple logically
independent commits, stop here and recommend `/plan-init` instead.

If anything in the plan is surprising or risky, flag it and wait for the user's
confirmation before proceeding. Otherwise, proceed immediately — do not ask for
approval on routine work.

---

## Step 6 — Execute the checklist

Work through every item in order. For each item:

- Do the actual work (edit files, write code, run commands, etc.)
- Check it off mentally as you go (you don't need to maintain a file)

**Standards to follow:**
- Follow all conventions in `AGENTS.md` / `CLAUDE.md` (if present)
- For new logic, API endpoints, and utilities: write the failing test first (TDD),
  then implement. Use the `tdd` skill if available; otherwise follow TDD discipline
  manually. For UI and wiring: write tests alongside.
- After any file move or rename: search for stale references (imports, configs,
  docs) and update them before moving on.

**Committing mid-checklist:** If the checklist has 6+ items with a clear logical
boundary between them (e.g., backend complete, then frontend), commit at that
boundary rather than waiting until the end. Use a focused commit message for that
chunk, then continue.

**If you hit a genuine blocker** (missing information, conflicting constraints,
failing test you can't diagnose):
- Stop. Do not proceed past the blocked item.
- Report to the user: what is blocked, what you verified, what decision is needed.

---

## Step 7 — Verify

Run the checks from the "Done when" section of your plan. If any fail:
1. Diagnose the failure
2. Fix the issue
3. Re-run until clean
4. Do not proceed until all pass

---

## Step 8 — Check your work

Run the `cyw` skill. If the skill is unavailable, perform the equivalent review
manually: re-read all modified files, check correctness/completeness/consistency,
and fix any issues found. Treat every issue as a blocker. Fix all issues before
committing. Do not commit until the review finds zero issues or explicitly clears
the work.

---

## Step 9 — Commit

Review the staged diff: confirm every change matches what the checklist covered. No
stray edits, no debug code, no unintended files.

```bash
git add -A
git diff --staged
git commit -m "<imperative summary under 72 chars>"
git push origin HEAD
```

Write the commit message to accurately reflect what was done.

---

## Step 10 — Report

Print a brief summary:
- What was done (condensed)
- Verification results
- Commit message(s)
