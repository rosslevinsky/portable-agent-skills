---
name: plan-init
description: >
  Create a structured project plan document for a task. Interviews the user to
  fill gaps, explores the codebase for context, then produces a master plan with
  goal, success criteria (checkable tests/conditions), technical constraints,
  non-goals, and affected areas. Does NOT break the plan into phases — use
  /plan-phase for that. Use when the user invokes /plan-init, or says "make a plan",
  "create a plan", "plan this out", "let's plan before we start".
---

# Create Project Plan

## Overview

Produce a structured plan document for the given task. The document becomes the
anchor for all future work breakdown and execution. Once written, it is never
modified by subsequent skills — it is a stable reference.

---

## Step 1 — Understand the initial task

The user's argument (everything after `/plan-init`) is the task description. If no
argument was given, ask: "What task do you want to plan?" and wait for the answer.
If operating autonomously (no user available) and no argument was given, stop and
report: "No task description provided — cannot proceed autonomously without input."

Parse the task description to extract what the user has already told you:
- The core objective
- Any explicit constraints
- Any explicit success criteria
- Any explicit non-goals

---

## Step 2 — Interview the user

Before doing any codebase exploration, ask the user clarifying questions in a
**single message**. Only ask about things genuinely unclear or missing from the
task description — skip any question the user already answered.

Cover these areas (omit any that are already clear):

- **Scope** — Is there anything specific this should or should not touch? Any
  related systems or concerns that aren't obvious from the description?

- **Done criteria** — How will you know it's working? Any specific tests, benchmarks,
  or manual checks that must pass? Anything currently broken that this must fix?

- **Hard constraints** — Any technical decisions already made that can't change?
  (specific library, API shape, DB schema, backwards compatibility requirement)

- **Non-goals** — What should this plan explicitly not address, even if related?

- **Output location** — Propose a location: "I'll write the plan to
  `plans/<slug>/plan.md` in the current directory — does that work, or would you
  prefer a different path?"

Wait for the user's answers before proceeding. Do not explore the codebase yet.

If operating autonomously (no user available), proceed with reasonable assumptions
based on the task description and note each assumption explicitly in the plan.

---

## Step 3 — Explore the codebase

Now explore the parts of the codebase relevant to the task. Identify:
- Files, modules, and components that will need to change
- Files that won't change but are important context (callers, dependencies, tests)
- Existing patterns and conventions to preserve
- Any existing tests that currently cover the affected area

Search the codebase for relevant files and read them. Do not skip this step — the
constraints and affected-areas sections of the plan depend on what you find here.

### Always check for framework/library standards

Before exploring the main codebase, check for standards documents in any referenced
framework or library:

1. Read `AGENTS.md` and `CLAUDE.md` in the current repo (if present).
2. Check `package.json`, `pyproject.toml`, or other project config for references to
   sibling repos or framework dependencies. For each sibling repo found:
   - Read its `AGENTS.md` — it may contain an explicit "Building on X" section
   - Read its `docs/technical-standards.md` if it exists
3. If the task involves building on a sibling framework, check for a canonical demo
   or example directory before designing any architecture.

These documents take precedence over any patterns you infer from reading code alone.

### Check for project quality gates

If the project has validation or quality gate scripts, verify they are properly
configured for the affected source tree:

1. Check whether `scripts/validate.sh` or an equivalent quality gate exists.
2. Check whether pre-commit hooks are configured (e.g., `.husky/pre-commit`,
   `lint-staged` in `package.json`, `.pre-commit-config.yaml`).
3. If quality gates exist, verify they cover the directories affected by this task.
   If not, note the gap as a constraint or prerequisite in the plan.

---

## Step 4 — Derive the plan content

From the task description, user interview answers, and codebase exploration, determine:

**Goal** — One to three sentences. What is being done and why? What is the desired
end state? Keep it concrete.

**Success criteria** — A checklist of observable, testable conditions that prove the
work is done. Each item should be falsifiable: a specific test command, a specific
behavior to verify, or a specific check to run. Examples:
- All tests pass with no regressions (`<test command>`)
- Feature X behaves like Y when Z happens
- No build errors (`<build command>`)
- Manual check: <specific UI or API behavior to verify>

**Technical constraints** — Things the implementation must respect. Include:
- Architectural rules from project agent instructions (e.g., AGENTS.md/CLAUDE.md) (if relevant)
- Patterns already in use that should be followed
- Dependencies or APIs that cannot change
- Performance or compatibility requirements
- Anything that would block a PR if violated

**Non-goals** — What this plan explicitly does NOT address. Include anything the user
stated plus anything implied by the scope. At least one entry required.

**Affected areas** — A list of real file paths that will change or must stay
consistent. Use actual paths from the codebase. Group by:
- "Will change" — files that get edited or created
- "Must stay consistent" — callers/consumers that must still work
- "Tests" — test files that need new or changed tests. For new behaviour, prefer
  TDD: the failing test is written before the implementation that makes it pass.

---

## Step 5 — Derive the plan slug and output path

Use the output location the user confirmed in Step 2. If they accepted the default,
generate a short kebab-case slug from the task description (3–5 words max) and use
`plans/<slug>/plan.md`. Examples: "auth-refactor", "vocab-export-endpoint".

---

## Step 6 — Write the plan document

Create the directory and write the plan file:

```markdown
# Plan: <human-readable title>

## Status

| Field | Value |
|---|---|
| Phase | Not yet broken down |
| State | Planning |
| Blocker | None |
| Last updated | <date> |

## Goal

<1–3 sentence description of what is being built/changed and why.>

## Success Criteria

- [ ] <specific test command or verifiable condition>
- [ ] <another condition>
- [ ] <...>

## Technical Constraints

- <constraint 1>
- <constraint 2>
- <...>

## Non-Goals

- <thing explicitly out of scope>
- <...>

## Affected Areas

**Will change:**
- `path/to/file.py` — reason
- `path/to/component.tsx` — reason

**Must stay consistent:**
- `path/to/shared/thing` — reason

**Tests** _(TDD preferred: write failing tests before the implementation that makes them pass)_**:**
- `path/to/test_file.py` — what behaviour it covers

---

_Phases: not yet broken down — run /plan-phase to generate phase documents._
```

---

## Step 7 — Report to the user

Print a brief summary:
- The plan file path
- The goal (one sentence)
- How many success criteria were identified
- How many files are in "will change"
- Next step: "Run `/plan-phase <path>` to break this into executable phases."
