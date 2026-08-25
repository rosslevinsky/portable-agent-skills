# Plan Duel — Critique Round

This is round ⟪round⟫ of up to 10. ⟪round_context⟫

The two competing plans are labeled A and B with no attribution.

## Agent A

Plan A's immutable snapshot from the end of the previous round is ⟪frozen_a⟫ and
Plan B's is ⟪frozen_b⟫. Work from these frozen snapshots — never from the other
agent's live, in-progress revision — so both sides refine against identical inputs.

Read ⟪frozen_a⟫, ⟪frozen_b⟫, and ⟪workdir⟫/problem.md. Then:

1. Identify the strongest elements of Plan B (⟪frozen_b⟫) that are missing or
   weaker in Plan A (⟪frozen_a⟫).
2. Identify weaknesses or gaps in Plan B that Plan A handles better — preserve
   these strengths.
3. Produce a revised Plan A incorporating the best of Plan B while keeping Plan A's
   own strengths.
4. List what you deliberately chose NOT to adopt from Plan B and why — be specific;
   do not silently omit anything.

Write the complete revised Plan A to ⟪workdir⟫/plan-a.md (overwrite) as a single
whole-file write, not an incremental patch (to avoid read-verify errors).
Write rejection notes (item 4) to ⟪workdir⟫/rejections-a-round-⟪round⟫.md.

## Agent B

Plan A's immutable snapshot from the end of the previous round is ⟪frozen_a⟫ and
Plan B's is ⟪frozen_b⟫. Work from these frozen snapshots — never from the other
agent's live, in-progress revision — so both sides refine against identical inputs.

Read ⟪frozen_a⟫, ⟪frozen_b⟫, and ⟪workdir⟫/problem.md. Then:

1. Identify the strongest elements of Plan A (⟪frozen_a⟫) that are missing or weaker
   in Plan B (⟪frozen_b⟫).
2. Identify weaknesses or gaps in Plan A that Plan B handles better — preserve these
   strengths.
3. Produce a revised Plan B incorporating the best of Plan A while keeping Plan B's
   own strengths.
4. List what you deliberately chose NOT to adopt from Plan A and why — be specific;
   do not silently omit anything.

Write the complete revised Plan B to ⟪workdir⟫/plan-b.md (overwrite) as a single
whole-file write, not an incremental patch (to avoid read-verify errors).
Write rejection notes (item 4) to ⟪workdir⟫/rejections-b-round-⟪round⟫.md.

## Judge

You are a neutral technical adjudicator with deep expertise in software
architecture and project planning. You have zero allegiance to either plan. Your
only goal is accurate, rigorous assessment grounded in technical merit.

Produce your assessment as your final reply only. Do NOT create, write, or edit any
file — output the assessment as your response text, nothing else.

Read the revisions just produced this round, from ⟪workdir⟫/:

- ⟪workdir⟫/plan-a.md — Plan A
- ⟪workdir⟫/plan-b.md — Plan B
- ⟪workdir⟫/problem.md — the problem statement
- The rejection files from the last up to 3 rounds (rounds max(1, ⟪round⟫−2)
  through ⟪round⟫): rejections-a-round-*.md and rejections-b-round-*.md. Skip
  gracefully if any are missing. Ideas rejected across multiple rounds carry more
  weight than one-round rejections.

**Part 1 — Convergence score**

Score on a scale of 0–10:

- 0  = fundamentally different approaches or goals
- 3  = same problem domain, divergent solutions
- 5  = same high-level approach, meaningful differences in scope or method
- 7  = broadly aligned, meaningful differences remain in sequencing, risk
       handling, or implementation detail
- 8  = no substantive differences — any remaining gaps are pure style or wording
       with zero technical consequence
- 10 = identical in all meaningful respects

Be skeptical. Plans frequently appear to converge at a surface level while still
diverging on sequencing, failure handling, rollback strategy, or specific
implementation choices. If you can articulate any remaining difference that would
cause a competent engineer to make a different decision, the score is at most 7. Do
not round up. If in doubt, score lower.

**Part 2 — Remaining differences**

Identify every substantive difference. Ignore formatting, phrasing, trivial
ordering. For each, state which plan's position is technically stronger and why. Say
"Equal" if both are valid. Flag any rejection-file entries that appear to be
mistakes — good ideas discarded that should have been kept.

**Part 3 — Preferred plan**

Evaluate on these dimensions in order of importance:

1. **Technical soundness** — correct approaches? Wrong assumptions or architectural
   red flags?
2. **Completeness** — all aspects addressed, including edge cases?
3. **Feasibility** — realistic and actionable, or hand-waving hard parts?
4. **Risk coverage** — failure modes identified and mitigated?
5. **Clarity** — precise enough to execute without a follow-up conversation?

Do not call it a tie. If substantially equivalent, prefer the marginally clearer or
more complete one.

Respond with a single JSON object and nothing else — no prose before or after it and
no markdown fence. It carries exactly these fields:

- `score` — the integer from Part 1.
- `differences` — one object per substantive difference from Part 2, each carrying
  `topic`, `plan_a`, `plan_b`, `stronger` (`"A"`, `"B"`, or `"Equal"`), and `reason`.
  Use an empty array only if no substantive differences remain.
- `missed_rejections` — the Part 2 rejection-file mistakes as an array of strings, or
  an empty array if there are none.
- `preferred` — `"A"` or `"B"` from Part 3.
- `justification` — one paragraph defending the choice at the depth you would write it
  in a review: concrete strengths of the winner, concrete weaknesses of the loser,
  specific references to both plans required. Do not compress it to a sentence.
