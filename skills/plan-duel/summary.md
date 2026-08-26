# Plan Duel — Summary Format

This file documents the `summary.md` format the engine emits at the end of a duel.
The engine assembles the summary directly from the final judge round and the plan
snapshots — this file is the format reference, not a rendered prompt.

Notation: a placeholder the engine substitutes is shown with the double-angle marker
(for example ⟪workdir⟫); a value the engine computes at summary time is shown in
single angle brackets (for example `<rounds_run>`).

---

## Identity resolution

Agent A is the controller runtime (⟪controller_name⟫); Agent B is the participant
runtime (⟪participant_name⟫). The A/B → runtime-name mapping is applied only here, in
the summary — it is never revealed to the agents or the judge during the rounds. The
lowercase file slugs are ⟪controller_slug⟫ and ⟪participant_slug⟫ (e.g. a runtime name
`Foo` → slug `foo`); the renamed plan files are plan-⟪controller_slug⟫.md and
plan-⟪participant_slug⟫.md.

---

## Winner resolution

From the final round's judge output (⟪workdir⟫/judge-round-`<rounds_run>`.md), whose
`preferred` field is `"A"` or `"B"`:

- `preferred: A` → winner is ⟪controller_name⟫, winner file plan-⟪controller_slug⟫.md
- `preferred: B` → winner is ⟪participant_name⟫, winner file plan-⟪participant_slug⟫.md

The judge round file is a JSON verdict matching `judge-schema.json` (`score`,
`differences`, `missed_rejections`, `preferred`, `justification`). A file written
before that schema landed carries the older line markers instead (`SCORE:`,
`DIFFERENCES:`, `MISSED REJECTIONS:`, `PREFERRED:`); the engine reads either, so a
resume over an older workdir resolves the winner identically.

Only the winning plan is stamped with the v2 markers (the `| Format | v2 |` row and
the `| Suite | plan-init / plan-phase / plan-run |` row) so it can be
fed to `/plan-phase`. The losing plan is left untouched. The verdict's
`differences` entries render as the numbered
`<topic>: Plan A: … Plan B: … **Stronger: A/B/Equal** — <reason>` lines shown below,
in which `Plan A` / `Plan B` and `Stronger: A` / `Stronger: B` are then rewritten to
the concrete runtime names; that rewrite is scoped to the differences block only.
`missed_rejections` renders as a bullet list, or omits its section entirely when
empty.

---

## Emitted layout

```markdown
# Plan Duel Summary

**Problem:** ⟪workdir⟫/problem.md
**Rounds run:** <rounds_run> (0 = initial plans, 1–<rounds_run> = critique rounds)
**Stopped due to:** <stopped_due_to>
**Winner:** <winner_name> → ⟪workdir⟫/<winner_file> (stamped `Format: v2` — feed it to `/plan-phase`)

## Score trajectory

| Round | Score | ⟪controller_name⟫ words | ⟪participant_name⟫ words |
|---|---|---|---|
| 0 | — | NNNN | NNNN |
| 1 | X | NNNN | NNNN |
...

## Why <winner_name> won

<justification — the verdict's justification paragraph>

(If the duel ran 5 or more rounds, a note is appended here observing that both plans
have heavily incorporated each other's ideas, so the winner reflects structural and
clarity differences more than fundamental approach divergence.)

## Remaining differences

<the rendered differences block, with Plan A/B rewritten to the runtime names>

## Missed rejections

(This section is emitted only when the judge reported missed rejections — an empty
`missed_rejections` array, or the older `MISSED REJECTIONS: none`, omits it.)

## All files

- Problem:             ⟪workdir⟫/problem.md
- ⟪controller_name⟫'s final plan: ⟪workdir⟫/plan-⟪controller_slug⟫.md
- ⟪participant_name⟫'s final plan: ⟪workdir⟫/plan-⟪participant_slug⟫.md
- Round snapshots:     plan-a-round-N.md, plan-b-round-N.md
- Rejection notes:     rejections-a-round-N.md, rejections-b-round-N.md
- Judge assessments:   judge-round-N.md (one per round)
- This summary:        ⟪workdir⟫/summary.md
```
