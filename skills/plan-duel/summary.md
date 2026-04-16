# Plan Duel — Summary

Uses: `workdir`, `rounds_run`, `stopped_due_to`.

---

## Resolve identity

Throughout the duel, Agent A = Claude, Agent B = Codex. This mapping is
applied now — all output from this point uses real names.

---

## Extract final judge output

Read `{workdir}/judge-round-{rounds_run}.md`. Extract:
- `final_score` — first integer from the `SCORE:` line
- `differences` — the full `DIFFERENCES:` block
- `missed_rejections` — the `MISSED REJECTIONS:` value
- `preferred` — the letter after `PREFERRED:` (A or B)
- `justification` — the paragraph following the `PREFERRED:` line

Resolve winner:
- `PREFERRED: A` → winner = Claude, winner_file = `plan-claude.md`
- `PREFERRED: B` → winner = Codex,  winner_file = `plan-codex.md`

---

## Rename final plans

Copy `{workdir}/plan-a.md` → `{workdir}/plan-claude.md`
Copy `{workdir}/plan-b.md` → `{workdir}/plan-codex.md`

---

## Build score trajectory table

For each round N from 0 to `rounds_run`, get word counts:
```bash
wc -w < "{workdir}/plan-a-round-N.md"
wc -w < "{workdir}/plan-b-round-N.md"
```

For rounds 1+, also parse the score: first integer from the `SCORE:` line
in `{workdir}/judge-round-N.md` (0 if missing or unparseable).
Round 0 has no judge score — display "—" in the score column.

---

## Write summary.md

In the `differences` block, replace "Plan A" with "Claude", "Plan B"
with "Codex", "Stronger: A" with "Stronger: Claude", and "Stronger: B"
with "Stronger: Codex" throughout.

Write `{workdir}/summary.md`:

```markdown
# Plan Duel Summary

**Problem:** {workdir}/problem.md
**Rounds run:** {rounds_run} (0 = initial plans, 1–{rounds_run} = critique rounds)
**Stopped due to:** {stopped_due_to}
**Winner:** {Claude or Codex} → {workdir}/{winner_file}

## Score trajectory

| Round | Score | Claude words | Codex words |
|-------|-------|--------------|-------------|
| 0     | —     | NNNN         | NNNN        |
| 1     | X     | NNNN         | NNNN        |
...

## Why {Claude/Codex} won

{justification}

{If rounds_run ≥ 5: "Note: after {rounds_run} rounds of mutual critique,
both plans have heavily incorporated each other's ideas; the winner reflects
structural and clarity differences more than fundamental approach divergence."}

## Remaining differences

{differences block with Plan A/B replaced by Claude/Codex}

## Missed rejections

{Include this section only if missed_rejections ≠ "none"}

## All files

- Problem:             {workdir}/problem.md
- Claude's final plan: {workdir}/plan-claude.md
- Codex's final plan:  {workdir}/plan-codex.md
- Round snapshots:     plan-a-round-N.md, plan-b-round-N.md
- Rejection notes:     rejections-a-round-N.md, rejections-b-round-N.md
- Judge assessments:   judge-round-N.md (one per round)
- This summary:        {workdir}/summary.md
```

---

## Print and finish

Print the full contents of `summary.md` to the user.

Do not summarize plan contents — the user can read the files directly.
