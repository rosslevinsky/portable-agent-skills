---
name: plan-duel
description: >
  Iterative plan refinement between Claude and Codex. Each agent writes an initial
  plan using the plan-init methodology, then they alternately critique and
  incorporate the other's best ideas, with a judge checking for convergence after
  each round. Stops at convergence (≥8/10), stagnation (no score improvement over
  3 consecutive rounds), or after 10 rounds. Writes a summary.md with a score
  trajectory, a pointer to the winning plan, and a breakdown of remaining
  differences with pros/cons. Supports resume from an interrupted run. Use when
  the user invokes /plan-duel, or says "duel the plans", "run plan duel",
  "cross-compare plans with codex". Argument: the problem statement (inline text
  or a file path), or the path to an existing plan-duels workdir to resume.
---

# Plan Duel — Orchestrator

_Classification: Runtime-limited — the controller must be a runtime with sub-agent orchestration and multi-file skill reading. This implementation provides adapter notes for a Claude controller and a Codex participant via `codex exec`. A reverse path that uses Codex as the controller and Claude as the participant is not implemented because it would need a corresponding non-interactive `claude` CLI adapter._

Delegates each phase to the sibling documents in this skill directory.
This file owns: argument parsing, working directory setup, the refinement
loop, and exit conditions. Everything else is delegated.

This workflow generates and refines competing plans from a problem statement.
It does not directly compare two already-written `plan.md` files as separate
duel inputs.

Throughout the duel, plans are labeled **A** and **B** with no attribution.
Agent A is Claude; Agent B is Codex. This mapping is never revealed to the
agents or the judge — only the summary step resolves A/B to real names.

---

## Step 0 — Pre-flight

Run: `command -v codex`.
If not found, print: `Error: codex CLI not found on PATH. Install it before
running plan-duel.` and stop.

Set `skill_dir` to the absolute path of the directory containing this
`SKILL.md` file. Set `plan_init_skill_path` to the absolute path of the
`plan-init/SKILL.md` file in the parent directory of `skill_dir`. If the runtime
does not expose the loaded skill's path directly, locate the installed
`plan-duel/SKILL.md` under the runtime's user or project skill directories, then
derive `skill_dir` from that path. If `plan_init_skill_path` does not exist,
stop and report the missing path.

---

## Step 1 — Establish state

### Resume path (argument is an existing directory containing problem.md)

0. If `{workdir}/summary.md` exists, print its contents and stop —
   the duel is already complete.
1. Set `workdir` to that path. Read `problem.md` as the problem statement.
2. Find the highest round N where **both** `plan-a-round-N.md` and
   `plan-b-round-N.md` exist. That is `last_completed_round`.
   If no such round exists (init was interrupted before snapshotting),
   delete all `plan-*.md`, `rejections-*.md`, `judge-*.md`,
   `codex-prompt-*.txt`, and `codex-*-status.md` files in workdir,
   log each deletion, print `Init incomplete — restarting from round 0.`,
   and jump to New run path step 3 (run init.md).
3. Delete any files numbered higher than `last_completed_round`:
   `plan-{a,b}-round-*.md`, `rejections-{a,b}-round-*.md`,
   `judge-round-*.md`, `codex-prompt-*.txt`, `codex-round-*-status.md`
   where the round number exceeds `last_completed_round`.
   Log each deletion.
4. Copy `plan-a-round-{last_completed_round}.md` → `plan-a.md`
   and `plan-b-round-{last_completed_round}.md` → `plan-b.md`.
5. Set `start_round` = `last_completed_round + 1`.
6. Print: `Resuming in {workdir} from round {start_round}.`

### New run path (anything else)

1. Resolve problem statement and optional explicit path:
   - If the argument is a path to a directory that does **not** contain
     `problem.md`, treat it as an explicit workdir path. Read any remaining
     arguments or prompt text as the problem statement.
   - File path that exists (not a directory) → read it as the problem statement.
   - Inline text → use directly as the problem statement.
   - No argument → ask the user and wait.
2. Create workdir:
   - If an explicit workdir path was given in step 1, use it:
     ```bash
     mkdir -p "{explicit_path}"
     ```
   - Otherwise, generate a kebab-case slug from the problem statement
     (3–5 meaningful words, lowercase, hyphens — e.g. "redesign the auth
     middleware for SSO support" → `auth-middleware-sso`).
     If `plans/duels/{slug}/problem.md` already exists, append `-2`, `-3`,
     etc. until the path is unused.
     ```bash
     mkdir -p "plans/duels/{slug}"
     ```
   Write problem statement to `{workdir}/problem.md`. Print the workdir path.
3. Read `init.md` from this skill directory and follow its instructions
   (round 0 — initial plans).
4. Set `start_round` = 1.

Both paths produce: `workdir`, `start_round`. Proceed to Step 2.

---

## Step 2 — Refinement loop

Convention: `score(N)` = first integer from the `SCORE:` line in
`{workdir}/judge-round-N.md`. If the file is missing or unparseable,
treat as 0 and log: `Warning: could not parse score at round N — treating as 0`.

If `start_round` > 10, set `rounds_run` = 10, `stopped_due_to` = Maximum
rounds, and skip to Step 3.

For each round N from `start_round` to 10:

1. Print `### Round N of up to 10`.
2. Read `round.md` from this skill directory and follow its instructions.
3. Parse `score(N)` from `{workdir}/judge-round-N.md`.
4. Print: `Round N complete — score X/10 | A: NNNN words, B: NNNN words`
   (word counts via `wc -w < "{workdir}/plan-a-round-N.md"` and
   `wc -w < "{workdir}/plan-b-round-N.md"`).
5. Check exit conditions in order:
   - **Converge:** N ≥ 3 and score(N) ≥ 8 → print `Convergence reached at
     round N (score: X/10).` and exit loop.
     _Why N ≥ 3: avoid trusting a high score before plans have cross-pollinated._
   - **Stagnate:** N ≥ 4 — compute `recent_best` = max of scores from
     rounds N, N−1, N−2, and `prior_best` = max of all scores from rounds 1
     through N−3. If `recent_best` ≤ `prior_best` → print `Stagnation
     detected — best score in last 3 rounds (Y/10) has not exceeded prior
     peak (Z/10). Stopping early.` and exit loop.
     _Why window-based: a single dip doesn't trigger exit; only a sustained
     failure to beat the previous best does._
   - **Max rounds:** N = 10 → print `Maximum rounds reached (score: X/10).`
     and exit loop.
   - Otherwise continue to N+1.

Record `stopped_due_to` as one of: Convergence / Stagnation / Maximum rounds.
Set `rounds_run` = last N.

---

## Step 3 — Summary

Read `summary.md` from this skill directory and follow its instructions.
