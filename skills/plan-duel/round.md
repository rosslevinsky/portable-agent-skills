# Plan Duel — Critique Round

Uses: `workdir`, `N`.

The orchestrator reads and parses `judge-round-N.md` after this document
completes. Do not check exit conditions here — just do the work.

---

## Part A — Cross-critique and update

Both agents read plan files directly. Plan A = `plan-a.md` (being improved
by Agent A), Plan B = `plan-b.md` (being improved by Agent B).
Run **simultaneously**:

---

### Agent A (controller runtime sub-agent)

> **Claude adapter:** Use the Agent tool with subagent_type=general-purpose
> with the prompt below.
> **Codex adapter:** Write the prompt below to `{workdir}/controller-prompt-N.txt`,
> then run: `codex exec --skip-git-repo-check -C "{workdir}" "$(cat "{workdir}/controller-prompt-N.txt")" < /dev/null`

Resolve all variables before sending: substitute the actual round number for
every occurrence of `N`, the actual workdir path for `{workdir}`, and resolve
the round context line — read `{workdir}/judge-round-{N-1}.md` and extract
the first integer from its `SCORE:` line. If the file is missing or N=1, use
"This is the first critique round."; otherwise use "The judge scored
convergence at {score}/10 last round."

> You are a senior software architect refining a plan through competitive
> critique. This is round N of up to 10. {round context line}
>
> Two competing plans are saved as files in `{workdir}/`:
> - `plan-a.md` — Plan A (the one you are improving)
> - `plan-b.md` — Plan B (the reference plan)
> - `problem.md` — the problem statement
>
> Read all three files. Then:
> 1. Identify the strongest elements of Plan B that are missing or weaker
>    in Plan A.
> 2. Identify weaknesses or gaps in Plan B that Plan A handles better —
>    preserve these strengths.
> 3. Produce a revised Plan A incorporating the best of Plan B while keeping
>    Plan A's own strengths.
> 4. List what you deliberately chose NOT to adopt from Plan B and why —
>    be specific; do not silently omit anything.
>
> Write the complete revised Plan A to `{workdir}/plan-a.md` (overwrite).
> Write rejection notes (item 4) to `{workdir}/rejections-a-round-N.md`.

Verify `plan-a.md` ≥200 bytes. If not — halt: "Agent A update failed at
round N."

---

### Agent B (participant runtime)

The Agent B prompt mirrors the Agent A prompt above with Plan A and Plan B
swapped. Resolve all variables the same way: substitute the actual round
number for every `N`, the actual workdir path for `{workdir}`, and resolve
the round context line the same way (read prior judge file from disk).

Pre-create output files so the participant's file-write tooling does not fail
on read-verify of nonexistent paths:

```bash
touch "{workdir}/plan-b.md" "{workdir}/rejections-b-round-N.md"
```

Write `{workdir}/codex-prompt-N.txt`:

```
You are a senior software architect refining a plan through competitive
critique. This is round N of up to 10. {resolved round context line}

Two competing plans are saved as files:
- {workdir}/plan-a.md — Plan A (the reference plan)
- {workdir}/plan-b.md — Plan B (the one you are improving)
- {workdir}/problem.md — the problem statement

Read all three files. Then:
1. Identify the strongest elements of Plan A that are missing or weaker
   in Plan B.
2. Identify weaknesses or gaps in Plan A that Plan B handles better —
   preserve these strengths.
3. Produce a revised Plan B incorporating the best of Plan A while keeping
   Plan B's own strengths.
4. List what you deliberately chose NOT to adopt from Plan A and why —
   be specific; do not silently omit anything.

Write the complete revised Plan B to {workdir}/plan-b.md (overwrite).
Write rejection notes (item 4) to {workdir}/rejections-b-round-N.md.

IMPORTANT: When writing or overwriting files, use shell commands
(e.g. cat > file << 'EOF' ... EOF) rather than patch-based tools,
to avoid read-verify errors on new or fully-rewritten files.
```

Invoke the participant runtime's CLI in non-interactive mode:

> **Codex adapter:** `codex exec --skip-git-repo-check -C "{workdir}" "$(cat "{workdir}/codex-prompt-N.txt")" < /dev/null > "{workdir}/codex-round-N-status.md"`
> **Other runtimes:** Adapt the invocation to the participant's CLI.

Verify `plan-b.md` ≥200 bytes. If not — halt: "Agent B update failed at
round N."

---

## Snapshot

Copy `plan-a.md` → `plan-a-round-N.md`
Copy `plan-b.md` → `plan-b-round-N.md`

---

## Part B — Judge

Pre-create the judge output file:

```bash
touch "{workdir}/judge-round-N.md"
```

Run a judge agent using the controller runtime's most capable model:

> **Claude adapter:** Use the Agent tool with subagent_type=general-purpose,
> model: opus, with the prompt below.
> **Codex adapter:** Write the prompt below to `{workdir}/judge-prompt-N.txt`,
> then run: `codex exec --skip-git-repo-check -C "{workdir}" "$(cat "{workdir}/judge-prompt-N.txt")" < /dev/null`
> **Other runtimes:** Use the strongest available model for judging.

> You are a neutral technical adjudicator with deep expertise in software
> architecture and project planning. You have zero allegiance to either plan.
> Your only goal is accurate, rigorous assessment grounded in technical merit.
>
> Read from `{workdir}/`:
> - `plan-a.md` — Plan A
> - `plan-b.md` — Plan B
> - `problem.md` — the problem statement
> - The last 3 rounds of rejection files (or fewer if fewer exist):
>   `rejections-a-round-{max(1,N-2)..N}.md` and
>   `rejections-b-round-{max(1,N-2)..N}.md`.
>   Skip gracefully if any are missing. Ideas rejected across multiple
>   rounds carry more weight than one-round rejections.
>
> **Part 1 — Convergence score**
>
> Score on a scale of 0–10:
> - 0  = fundamentally different approaches or goals
> - 3  = same problem domain, divergent solutions
> - 5  = same high-level approach, meaningful differences in scope or method
> - 7  = broadly aligned, meaningful differences remain in sequencing, risk
>        handling, or implementation detail
> - 8  = no substantive differences — any remaining gaps are pure style or
>        wording with zero technical consequence
> - 10 = identical in all meaningful respects
>
> Be skeptical. Plans frequently appear to converge at a surface level while
> still diverging on sequencing, failure handling, rollback strategy, or
> specific implementation choices. If you can articulate any remaining
> difference that would cause a competent engineer to make a different
> decision, the score is at most 7. Do not round up. If in doubt, score lower.
>
> **Part 2 — Remaining differences**
>
> Identify every substantive difference. Ignore formatting, phrasing, trivial
> ordering. For each, state which plan's position is technically stronger and
> why. Say "Equal" if both are valid. Flag any rejection-file entries that
> appear to be mistakes — good ideas discarded that should have been kept.
>
> **Part 3 — Preferred plan**
>
> Evaluate on these dimensions in order of importance:
> 1. **Technical soundness** — correct approaches? Wrong assumptions or
>    architectural red flags?
> 2. **Completeness** — all aspects addressed, including edge cases?
> 3. **Feasibility** — realistic and actionable, or hand-waving hard parts?
> 4. **Risk coverage** — failure modes identified and mitigated?
> 5. **Clarity** — precise enough to execute without a follow-up conversation?
>
> Do not call it a tie. If substantially equivalent, prefer the marginally
> clearer or more complete one.
>
> Respond in exactly this format — no other text before or after:
>
> SCORE: [integer]
>
> DIFFERENCES:
> 1. [topic]: Plan A: {approach}. Plan B: {approach}. **Stronger: [A/B/Equal]** — [reason]
> 2. ...
> (Write `DIFFERENCES: none` if no substantive differences remain.)
>
> MISSED REJECTIONS: [list, or `none`]
>
> PREFERRED: [A or B]
> [One paragraph: concrete strengths of the winner, concrete weaknesses of
> the loser. Specific references to both plans required.]

Write the judge's full response to `{workdir}/judge-round-N.md`.

IMPORTANT: When writing or overwriting files, use shell commands
(e.g. cat > file << 'EOF' ... EOF) rather than patch-based tools,
to avoid read-verify errors on new or fully-rewritten files.
