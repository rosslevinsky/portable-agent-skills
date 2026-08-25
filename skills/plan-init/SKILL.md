---
name: plan-init
description: >
  Create a structured project plan document for a task — the current planning
  suite. Interviews the user to fill gaps, explores the codebase, then produces a
  master plan.md stamped with the `Format: v2` marker and a `Suite` row, and —
  when the plan lives under plans/ — registers it in the plans/README.md
  discovery index. Stays breakdown-unaware
  (work breakdown is /plan-phase's job) and adds UI-verification success criteria
  when UI is in scope. Use when the user invokes /plan-init, or says "make a plan",
  "create a plan", "plan this out", "let's plan before we start".
---

# Create Project Plan (v2)

## Overview

Produce a structured plan document for the given task, marked as **v2** so the
`/plan-phase` and `/plan-run` skills recognize and operate on it. The
document becomes the anchor for all future work breakdown and execution. Once
written, it is never modified by subsequent skills — it is a stable reference.

This is the v2 variant of `/plan-init-v1`. Three differences change what the skill
*does*; the rest of the divergence between the two files is wording:

1. The plan's Status table is **replaced**, not extended: `Format: v2` and `Suite`
   are the only two rows, and v1's `Phase` / `State` / `Blocker` / `Last updated`
   are gone (Step 6 says why).
2. The plan is registered in the `plans/README.md` discovery index — **when it lives under
   `plans/`**. The index row is a relative link resolved from `plans/README.md`, so a plan
   the user asked for somewhere else has no row that would resolve (Step 7).
3. When UI is in scope, a visual-verification success criterion is added.

Everything else about the plan's content model is identical to v1. In particular,
this skill stays **breakdown-unaware**: it writes no phases and no grouping —
that is `/plan-phase`'s job.

---

## Step 1 — Understand the initial task

The user's argument (everything after `/plan-init`) is the task description. If
no argument was given, ask: "What task do you want to plan?" and wait for the answer.
If operating autonomously (no user available) and no argument was given, stop and
report: "No task description provided — cannot proceed autonomously without input."

Parse the task description to extract what the user has already told you:
- The core objective
- Any explicit constraints
- Any explicit success criteria
- Any explicit non-goals
- Whether the work involves a **web UI** (a frontend, pages, components, or
  user-visible visual behavior) — this decides the UI success criterion in Step 4.

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

- **UI in scope** — Does this change anything a user sees or interacts with? If so,
  which flows/routes matter most for verification?

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

First work through the framework/library standards check in the subsection below — it
takes precedence over any pattern you would infer from the code, so it has to precede
exploration rather than follow it. Then explore the parts of the codebase relevant to the
task. Identify:
- Files, modules, and components that will need to change
- Files that won't change but are important context (callers, dependencies, tests)
- Existing patterns and conventions to preserve
- Any existing tests that currently cover the affected area
- Whether a **web UI / Playwright** setup exists (informs the UI success criterion)

Search the codebase for relevant files and read them. Do not skip this step — the
constraints and affected-areas sections of the plan depend on what you find here.

### Always check for framework/library standards

Before exploring the main codebase, check for standards documents in any referenced
framework or library:

1. Read `AGENTS.md` and `CLAUDE.md` in the current repo (if present).
2. Check `package.json`, `pyproject.toml`, or other project config for references to
   sibling repos or framework dependencies. For each sibling repo found:
   - Read its `AGENTS.md` — it may contain an explicit "Building on X" section
   - Read any technical-standards or conventions document it publishes
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

**If the work involves a web UI**, add a visual-verification success criterion,
branching on whether a Playwright setup exists (the Step 3 finding):
- **With Playwright:** UI verified: the target flow's spec is green **N×** (default 3)
  across the mandatory viewport matrix, with frames/screenshots **inspected** (not merely
  produced) — checked with `/web-verify` at the phase gate.
- **Without Playwright:** UI verified by manual inspection: `/web-verify`'s bundled
  manual UI-verification checklist is completed across the mandatory viewports (its
  degraded mode) — no spec is required.

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
`plans/<slug>/plan.md`. Examples: "auth-refactor", "vocab-export-endpoint". No date
prefix.

---

## Step 6 — Write the plan document

Before writing, check whether **the path you are about to write** already exists — the one
resolved just above, which is `plans/<slug>/plan.md` only when the user accepted the default.
Checking that literal instead would look for a file the skill is not writing, find nothing,
and overwrite whatever the user actually named; on that branch there is no `<slug>` to
substitute either, because it is generated only on the default path. If it does, do
**not** overwrite it — pick a more specific slug (noting the change) or stop and ask
the user whether to resume or replace the existing plan.

Create the directory and write the plan file using the plan-document skeleton in
`references/plan-template.md`. The Status table is exactly two rows — `Format` and
`Suite`. Write no phases and no grouping.

**`## Assumptions`** is the section that autonomous mode's "note each assumption
explicitly in the plan" writes into. Include it whenever an interview question went
unanswered — the reader needs to know which parts of the plan rest on a guess, and which
would change if the guess is wrong. Omit the section entirely when every question was
answered; an empty heading says less than no heading.

`plan.md` is written once and never edited again: `/plan-phase` and `/plan-run` both read
it and leave it alone. So it carries no `State`, `Phase`, `Blocker` or `Last updated` cell
(v1 plans do) — a status field in an immutable document can only ever be stale. Live status
lives in `execution.md` and the phase documents, as checkboxes.

The `| Format | v2 |` row is the load-bearing marker: `/plan-phase` and
`/plan-run` act only on plans that carry it, and the v1 skills never do. Emit it
exactly as the two-cell table row shown in the template.

---

## Step 7 — Register the plan in the discovery index

**Only when the plan lives under `plans/`** — that is, the user accepted the default in
Step 2 and Step 5 generated a `<slug>`. Off that path there is no slug to link and no
index to belong to: skip this step, and **never create `plans/` to hold a row**. The row
snippet resolves relative to `plans/README.md`, so a plan written to `docs/proposal.md`
would be indexed as `plans/<slug>/` — a link to nothing, in a directory invented to hold
it. `/plan-phase` and `/plan-run` both refuse to invent that directory; this must too.

On the default path, maintain `plans/README.md` as the discovery index (it renders on
GitHub/GitLab), using the create/append snippets in `references/plan-template.md`:

- If `plans/README.md` does **not** exist, create it with the index title and table header
  from the template.
- Append one row for this plan using the row snippet from the template: the linked slug
  and one sentence. Do not rewrite existing rows for other plans.

The index carries no status column — a plan's own checkboxes are the progress record, and a
column here would be a second copy that goes stale.

---

## Step 8 — Report to the user

Print a brief summary:
- The plan file path
- The goal (one sentence)
- How many success criteria were identified (note if a UI-verification criterion was added)
- How many files are in "will change"
- That the plan is stamped `Format: v2`, and that it is indexed in `plans/README.md` —
  or, off the default path, that it was **not** indexed and why
- Next step: "Run `/plan-phase <path>` to break this into executable phases and generate the `execution.md` tracker."

---

## References

- `references/plan-template.md` — the plan-document skeleton (Step 6) and the
  discovery-index create/append snippets (Step 7).
