# plan-init — Document Templates

Fill-in skeletons for `/plan-init`. This file is **output format only** — the
planning logic (Steps 1–5) lives in `SKILL.md`. Read it at Step 6 (write the plan) and
Step 7 (register it in the discovery index).

## Plan document (Step 6)

Write the plan from this skeleton to **the output path Step 5 resolved**, creating its
directory. That is `plans/<slug>/plan.md` by default, and wherever the user named instead
otherwise. Every literal `plans/<slug>/` below is the default spelled out, not a
requirement — the index row at the end of this file is qualified the same way, and for the
same reason. The Status
table's `| Format | v2 |` and `| Suite | … |` rows are the load-bearing v2 markers — emit
them exactly, and emit nothing else in that table (no status cell, per Step 6). Write no
phases and no grouping.

```markdown
# Plan: <human-readable title>

## Status

| Field | Value |
|---|---|
| Format | v2 |
| Suite | plan-init / plan-phase / plan-run |

## Goal

<1–3 sentence description of what is being built/changed and why.>

## Success Criteria

- [ ] <specific test command or verifiable condition>
- [ ] <another condition>
- [ ] <UI verification criterion, if UI is in scope>

## Technical Constraints

- <constraint 1>
- <constraint 2>

## Non-Goals

- <thing explicitly out of scope>

## Assumptions

_Only when a question went unanswered — omit the section entirely otherwise._

- <what was assumed, and what would change if it is wrong>

## Affected Areas

**Will change:**
- `path/to/file.py` — reason
- `path/to/component.tsx` — reason

**Must stay consistent:**
- `path/to/shared/thing` — reason

**Tests** _(TDD preferred: write failing tests before the implementation that makes them pass)_**:**
- `path/to/test_file.py` — what behaviour it covers

---

_Work breakdown lives in the phase documents and execution.md, produced by /plan-phase._
```

## Discovery-index rows (Step 7)

Maintain `plans/README.md` as the discovery index (it renders on GitHub/GitLab). If it
does **not** exist, create it with this title and table header:

```markdown
# Plans

Index of planning efforts in this repo (v2 workflow convention — human- and
machine-readable). Each row is a pointer, not a status line: a plan's own checkboxes
are the progress record.

| Plan | Description |
|---|---|
```

Append one row for this plan (do not rewrite rows for other plans):

```markdown
| [<slug>](./<slug>/) | <one-sentence description of the effort> |
```

The link is relative to `plans/README.md`, so this row only resolves for a plan that lives
under `plans/<slug>/`. A plan written anywhere else has no `<slug>` and is not indexed —
see Step 7.
