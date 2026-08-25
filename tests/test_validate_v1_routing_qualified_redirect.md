---
name: plan-phase-v1
description: >
  v1 work breakdown, superseded by the canonical suite. Use when the user invokes
  /plan-phase-v1 against a plan that predates the Format: v2 marker.
---

# Break Plan Into Phases (v1)

Read the plan document in full.

If the plan carries a `Format: v2` marker, stop: v2 plans are broken down by
`/plan-phase-v1` (which writes an `execution.md` tracker), not this v1 skill.
Tell the user to run `/plan-phase-v1` instead.

Write the phase documents next to the plan, so `/plan-run-v1` finds them there.
