---
name: plan-run-v1
description: >
  v1 plan runner, superseded by the canonical suite. Use when the user invokes
  /plan-run-v1 against a plan that predates the Format: v2 marker.
---

# Execute Plan (v1)

Work through a phased plan created by `/plan-init-v1` + `/plan-phase-v1`. Execute
each incomplete phase in order, committing at the end of every phase.

If `plan.md` carries a `Format: v2` marker, stop: this is the v1 runner and cannot
execute a v2 plan. Tell the user to run `/plan-run` instead (with `/plan-phase`
for the work breakdown if it has not been done yet).

Then locate `phases.md` in the same directory as `plan.md`. If `phases.md` does not
exist, stop and tell the user:
"This plan has not been broken down into phases yet. Run `/plan-phase-v1 <path>` first."
