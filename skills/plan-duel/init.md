# Plan Duel — Round 0: Initial Plans

Perform the task described below and write only its output file.

Two competing plans are being generated for the same problem, labeled A and B with
no attribution. Each is produced independently from the problem statement in
⟪workdir⟫/problem.md.

## Agent A

Follow the plan methodology below to produce a plan for the problem in
⟪workdir⟫/problem.md. You are operating autonomously — there is no user to
interview, so make reasonable assumptions wherever a planner would normally ask,
and note each assumption in the plan where it applies.

Write the complete plan document to ⟪workdir⟫/plan-a.md.

## Agent B

Follow the plan methodology below to produce a plan for the problem in
⟪workdir⟫/problem.md. You are operating autonomously — there is no user to
interview, so make reasonable assumptions wherever a planner would normally ask,
and note each assumption in the plan where it applies.

Write the complete plan document to ⟪workdir⟫/plan-b.md.

## Plan methodology (condensed v2)

_Provenance: condensed from the plan-init skill's content model — a fix to
either side lands in its mirror._

Produce one structured plan document that a later work-breakdown step can split
into executable phases. Work only from ⟪workdir⟫/problem.md plus whatever the
problem statement itself references. Explore that material before writing:
identify what must change, what must keep working, and which patterns already
exist. Do not interview anyone and do not register the plan in any index — write
only the plan file.

The document carries these sections, in this order:

1. **Title** — `# Plan: <human-readable title>`.
2. **Goal** — one to three sentences: what is being built or changed and why,
   with a concrete desired end state.
3. **Success Criteria** — a checklist (`- [ ]`) of observable, testable
   conditions that prove the work is done. Each item is falsifiable: a specific
   test command, a specific behavior to verify, or a specific check to run. If
   the work involves a web UI, include a visual-verification criterion (the
   target flow verified across viewports, with screenshots inspected — not
   merely produced).
4. **Technical Constraints** — what the implementation must respect: existing
   patterns and conventions to follow, dependencies or APIs that cannot change,
   performance or compatibility requirements, anything that would block a merge
   if violated.
5. **Non-Goals** — what the plan explicitly does NOT address, even if related.
   At least one entry.
6. **Assumptions** — anything the plan rests on that was not given in the problem
   statement, and what would change if it turned out to be wrong. Include this
   section only when there is something to record; omit it entirely otherwise.
7. **Affected Areas** — real file paths, grouped as: **Will change** (files
   edited or created), **Must stay consistent** (callers/consumers that must
   keep working), and **Tests** (test files needing new or changed coverage;
   prefer TDD — the failing test is written before the implementation that
   makes it pass).

Keep the plan breakdown-unaware: no phases and no grouping — work
breakdown happens later, outside the duel. Do not add version-marker rows
(Format/Suite) yourself; the duel engine stamps the winning plan with the v2
markers when the duel completes.

## Writing files

When writing or overwriting a file, write its complete contents in a single
whole-file write rather than applying incremental patches, to avoid read-verify
errors on newly created or fully-rewritten files.
