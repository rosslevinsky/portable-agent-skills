---
name: plan-run-v1
description: >
  Execute a superseded v1 plan — one driven by phases.md rather than execution.md. Use
  when the user invokes /plan-run-v1, or is continuing an existing v1 plan produced by
  /plan-init-v1 + /plan-phase-v1; a plan carrying the `Format: v2` marker belongs to
  /plan-run. Reads the master plan and phases.md execution tracker, identifies
  incomplete phases, and executes each phase in order: checks entry criteria, completes
  tasks, runs verification, runs /cyw, reviews the staged diff, commits and pushes to
  origin, then marks the phase done in docs. Updates phases.md status block throughout.
  Skips already-completed phases so it is safe to restart.
---

# Execute Plan

## Overview

Work through a phased plan created by `/plan-init-v1` + `/plan-phase-v1`. Execute each
incomplete phase in sequence. Never skip a phase. Commit and push to origin at the
end of each phase. Update `phases.md` status block and checkboxes throughout so
progress is always visible and the plan is restartable.

**Authority rule:** The phase documents are the execution plan. The actual codebase
is the reality check. If a phase document references paths, APIs, or structures that
don't match the codebase, do not blindly follow the stale doc — correct the doc first
(smallest accurate correction), then continue execution against the corrected doc.
Here "the doc" means the **phase document or `phases.md`** — **never `plan.md`**,
which `/plan-init-v1` fixed as a stable reference. If reality diverges from `plan.md`
itself, do not edit it; record the drift as a note in the phase document (and in
`phases.md` if it affects later phases).

---

## Step 1 — Locate the plan

If the user provided a path argument, use it. Otherwise:
- Search for plan files matching `plans/**/plan.md`
- If exactly one exists, use it
- If multiple exist, list them and ask the user which one to run.
  If operating autonomously, choose the only incomplete plan; if all are
  incomplete, choose the most recently modified one and note the assumption.

Read the master `plan.md` in full.

If `plan.md` carries a `Format: v2` marker, stop: this is the v1 runner and cannot
execute a v2 plan. Tell the user to run `/plan-run` instead (with
`/plan-phase` for the work breakdown if it has not been done yet).

Then locate `phases.md` in the same directory as `plan.md`. If `phases.md` does not
exist, stop and tell the user:
"This plan has not been broken down into phases yet. Run `/plan-phase-v1 <path>` first."

Read `phases.md` in full.

---

## Step 2 — Survey the phases

Parse the Phases section of `phases.md` and separate into:
- **Complete** — lines starting with `- [x]`
- **Incomplete** — lines starting with `- [ ]`

Print a brief status report:
```
Plan: <title>
Phases complete:   N / M
Next phase:        Phase X — <Name>
```

If all phases are complete, skip to Step 4 (final verification).

---

## Step 3 — Execute each incomplete phase

For each incomplete phase, in order from first to last:

### 3a — Re-read before starting

At the start of every phase — even if you just finished the prior one — re-read:
1. `phases.md`
2. The phase document for the phase you are about to start

Do not rely on memory. Re-reading ensures you're working from the current state of
both documents, not a stale mental model.

**Then check whether this phase is actually unstarted.** 3h commits and pushes before 3i
ticks anything, so an interrupted run leaves one ambiguous state: work committed,
bookkeeping pending.

The test is **the phase document's own boxes**, not the working tree. A clean tree means
nothing on its own: the files a phase will touch are also clean before it starts, so
"clean" would mark every phase of a fresh run complete — skipping the work, the tests and
the review, and ticking the whole plan done having executed nothing. Ask instead: are this
phase's boxes already ticked in its own document — and **which** boxes, because the answer
decides where you re-enter:

- **Any Task or Test box unticked** → the phase is incomplete. Carry on to 3b and run it.
- **Tasks and Tests ticked, but the Exit Criteria are not** → the work exists and has not
  been *gated*. Re-enter at **3e**: run the verification, then 3f's review, then 3g. A tick
  on the last test is not a record that the suite was run afterwards, and the crash window
  between them is real — resuming at 3h here would commit and push work that nothing
  checked, which is the one outcome the gate exists to prevent.
- **Tasks, Tests and Exit Criteria all ticked, `phases.md` still `- [ ]`** → the work is
  done and gated, and only bookkeeping is outstanding, whether or not the push got as far as
  the remote. Go to **3i** to finish the bookkeeping, then run **3h's push block alone** —
  the guarded one, not the whole of 3h and not a bare `git push` — so a commit a crash
  stranded locally gets published rather than left behind.

  Not 3h whole. Its commit is **not** a no-op here: the only way to reach this branch is
  for 3i to have been interrupted after ticking an Exit Criterion, so `git add -A` stages
  that tick and `git diff --staged --quiet` is false. Measured, the else arm then creates a
  second commit carrying nothing but the tick, under the phase's own message ("Add schema
  migration") — and anything else dirty in the tree with it, since the text had just said
  the commit would do nothing. The paragraph below says as much: dirty bookkeeping is
  *expected* at this point.

Dirty *bookkeeping* proves nothing either way and is expected at this point: a phase-document
status or an Exit Criteria tick written just before the crash is exactly what 3i writes.

**The third branch names three sections rather than saying "everything".** Entry Criteria are
a fourth section of boxes and nothing here ever ticks them — 3b *confirms* they are true,
which is not the same edit. So "everything ticked" described a state a completed phase never
reaches; and because the first branch asks only about Task and Test boxes, a finished phase
matched no branch at all and the ladder ran out of instructions exactly where it was meant to
resume.

### 3b — Check entry criteria

Read the Entry Criteria section of the phase document. Confirm every item is true
before doing any work. If an entry criterion is not satisfied, stop and report it
rather than proceeding.

### 3c — Verify assumptions against the codebase

Before editing any file, verify that the paths, APIs, and structures referenced in
the phase doc actually exist as described. Search for the referenced files and
check their contents.

If the phase doc diverges from reality:
1. Make the smallest accurate correction to the phase doc (or phases.md) first
2. Document what changed and why, in a comment or note in the doc itself
3. Then continue execution against the corrected doc

### 3d — Complete the tasks

Work through every task in the checklist. For each task:
- Do the actual work (edit files, write code, etc.)
- After completing the task, update the phase document to check it off:
  Change `- [ ]` to `- [x]`

Follow all conventions defined in the project's `AGENTS.md`/`CLAUDE.md` (or
equivalent). If no agent-instruction file exists, follow the existing patterns found
in the codebase.

**Testing discipline (red → green, self-sufficient).** For every task that adds new behaviour:

- **Order.** Logic, APIs, and utilities: write the failing test **first**; UI and wiring:
  tests alongside. No new behaviour ships without tests — not optional.
- **Red for the right reason.** Run the new test and confirm it fails because the behaviour is
  *absent* — an import/attribute error for something new, or a **failing assertion** when
  extending an API that already exists — not from a broken test. If it passes
  immediately, the behaviour already exists — stop and note it instead of adding code.
- **Minimum green, no regressions.** Write the least code that passes, then run the affected
  suite and confirm nothing previously green broke.
- **Never fake green.** Don't weaken/delete assertions, edit the test to fit the code, or add
  `# noqa` / `eslint-disable` / test-only branches in production code to force a pass — fix the
  implementation instead.
- **Tick the box in the phase document's Tests section** as each test is written and passing,
  the same way task boxes are ticked above — one at a time, never in a batch at the end. Those
  boxes are not decoration: the Exit Criteria assert against them, and 3a reads them to decide
  where an interrupted run resumes. A box nobody was told to tick makes finished work look
  unstarted, and the resume re-runs it.

The `tdd` skill automates this loop if available; otherwise follow the steps above directly.

After any path, module, or file move: run a repo-wide search for stale references
(imports, scripts, configs, docs) and update them before moving on.

### 3e — Run verification

Run every command listed in the phase's Verification section. If any command fails:
1. Diagnose the failure (read error output, check affected files)
2. Fix the issue
3. Re-run until all verification commands pass
4. Do not proceed until verification is clean

A command that outruns one foreground call runs under the runtime's background-execution
facility where that facility reports the exit; otherwise launch it once and, in a later call,
poll for a marker it writes. Never poll a command-line pattern (`pgrep -f`, `pkill -f`,
`ps | grep`): the shell running your poll holds the command text in its own argv, so the
pattern matches the poller and the loop never exits. Put the command in a file, so nothing
re-expands it, and launch it in its own process group (`set -m`) and, where `setsid`
exists, its own session, under `nohup`, so a runtime that kills a timed-out call's group
does not kill the work, with `bash -eo pipefail`, so a
sequence stops at its first failing step and a pipe does not hide one (one command per
line: errexit exempts `a` in `a && b`); have it append a marker
that carries its exit status, bound the poll, and read the work's outcome from that marker
— a non-zero exit from the poll itself is the poll dying, not the verification failing:

```bash
# the command itself is in /abs/path/work.sh, written with your file tool, one command per line
rm -f /abs/path/work.log; set -m; S=$(command -v setsid || true)
$S nohup bash -c 'set +e; bash -eo pipefail /abs/path/work.sh; printf "\nWORK-EXIT rc=%s\n" "$?"' \
  > /abs/path/work.log 2>&1 < /dev/null &
# later call
for i in $(seq 1 240); do grep -q '^WORK-EXIT rc=' /abs/path/work.log 2>/dev/null && break; sleep 15; done
```

### 3f — Check your work

Run the `cyw` skill now. This performs a structured review of everything changed
in this phase: correctness, completeness, consistency, integration, and test
coverage. If the `cyw` skill is unavailable, perform the equivalent review
manually: re-read all modified files, check correctness/completeness/consistency,
and fix any issues found.

Treat every issue found as a blocker. Fix all issues before proceeding.
Do not commit until the review finds zero issues or explicitly clears the phase.

### 3g — Pre-completion gate

Before updating any docs or committing, confirm all four are true:

1. **Tasks complete** — every task in the phase checklist is checked off.
2. **Tests written and passing** — every test listed in the phase's Tests section exists,
   passes, and no previously passing tests have regressed.
3. **Verification passed** — all verification commands ran clean.
4. **Review clean** — `cyw` skill (or manual review) found zero issues.

### 3h — Commit and push the phase

**The work is published before it is marked done.** A phase ticked off first, then
interrupted, leaves `phases.md` claiming a phase is complete while its code sits
uncommitted — and a resumed run reads that tick and walks straight past. Committing and
pushing first inverts the failure: a crash after this step leaves finished work published
and only the bookkeeping outstanding, which 3i's resume branch picks up.

**Guard against an empty diff** — a verification-only phase may stage nothing, and
an unconditional `git commit` would error ("nothing to commit") and halt the run:

**Two blocks, and the split is the point.** The first ends at the diff; the second runs
only once you have read it. With the commit inside the same block there is nothing to stop
at — the block executes as one unit and the commit lands whatever the diff held.

```bash
git add -A
git diff --staged        # STOP HERE. Read it. Nothing below runs until you have.
```

Confirm every change matches what the phase was supposed to do: no stray edits, no debug
code, no unintended files. Fix and re-stage if anything unexpected appears. Then:

> **Adapter note — this block is `sh`/Bash syntax.** The `if`/`elif`, `$(...)` and
> `[ ... ]` forms do not parse in `cmd` or PowerShell, so on a native-Windows shell run it
> under Git Bash or WSL, or carry out the same decisions with your shell's own syntax:
> commit only when something is staged, and push only when the tip is not already
> published. **The decisions are the contract, not the spelling** — a completed phase that
> cannot run this block is a phase left uncommitted.

```bash
if git diff --staged --quiet; then
  echo "Nothing to commit for this phase (verification-only) — skipping commit."
else
  git commit -m "<commit message from phase document>"
fi
# The push sits OUTSIDE that branch, and asks the repository rather than remembering what
# just happened: a crash between the commit and the push leaves a resumed run with nothing
# staged, and a push nested in the `else` above would then never run — stranding the very
# commit 3a's resume check looks for.
branch="$(git rev-parse --abbrev-ref HEAD)"
default="$(git symbolic-ref --quiet --short refs/remotes/origin/HEAD 2>/dev/null | sed 's#^origin/##')"
# Two destinations to skip — never fail on them, since the work is committed either way.
if [ "$branch" = "HEAD" ]; then
  # Detached: `--abbrev-ref HEAD` answers the literal string, so `git push origin HEAD`
  # has no destination ref to resolve.
  echo "Detached HEAD — committed but NOT pushing. Check out a branch and push."
elif [ "$branch" = "${default:-main}" ] || [ "$branch" = "main" ] || [ "$branch" = "master" ]; then
  # Plan execution belongs on a feature branch; pushing each phase to main/master
  # publishes work-in-progress to the trunk.
  echo "On default branch '$branch' — committed but NOT pushing. Move this work to a feature branch."
elif [ "$(git rev-parse HEAD)" != "$(git rev-parse --verify --quiet "origin/$branch")" ]; then
  git push origin HEAD
fi
```

**A skipped push is not a completed 3h.** Both guards skip rather than fail, so the work is
committed and nothing is lost — but it is local. Do not go on to 3i: stop and report that
the run is on the default branch, or detached, and that the work needs moving to a feature
branch. 3a's check resumes the phase from here once it is.

If the phase document's commit message needs minor adjustment (e.g., the actual
scope changed slightly), update it to accurately reflect what was done.

### 3i — Mark phase complete

In this order, because the `phases.md` tick is the edit a resumed run reads as "skip this
phase" — anything left after it is never returned to:

1. In the phase document: change `_Status: pending_` to `_Status: complete_`, and check off
   every item in the Exit Criteria section.
2. In `phases.md`: update the Status table — set Phase to the next phase (or "All
   complete"), set Last updated to today's date, clear any Blocker.
3. In `phases.md`, **last**: change the phase line from `- [ ]` to `- [x]`.

These edits land **uncommitted**, which is correct: the next phase's `git add -A` sweeps
them up, and Step 5 commits whatever the last phase left. A resumed run reaching this step
with the work already published — 3a's check — does only what is above and moves on.

### 3j — Report and continue

Print a one-line progress update:
```
✓ Phase X complete. (N of M done)
```

Then proceed immediately to the next incomplete phase. Do not pause between phases
unless a blocking issue was encountered.

---

## Blocking behavior

If anything blocks completion of a phase:
- Stop. Do not advance to the next phase.
- Update the `phases.md` Status table:
  - State → "Blocked"
  - Blocker → precise description: what is blocked, what was verified, what decision is needed
- Report to the user with the same detail.

Only resume after the blocker is resolved. If a narrow, justified decision resolves
the blocker safely, make it, document it in the phase doc or phases.md, then continue.

If you must discard partial edits from a blocked attempt, **scope the cleanup to the
phase's own files**: revert only the specific paths you changed (`git restore -- <paths>`
for tracked edits; delete only files you created) — **never** a blanket `git reset --hard`
or `git checkout .`, which would also discard unrelated uncommitted work. Leave any
uncommitted changes the phase did not create untouched; if ownership is unclear, stop
and ask.

---

## Step 4 — Final verification (all phases complete)

When all phases are checked off, run the full Success Criteria from `plan.md`.
For each criterion:
- Run the specified command or check the specified condition
- Note pass / fail

---

## Step 5 — Update phases.md status, and commit the bookkeeping

Update the Status table in `phases.md` according to the Step 4 results:
- Phase → "All phases complete"
- Last updated → today's date
- If **every** success criterion passed: State → "Complete", Blocker → "None".
- If **any** success criterion failed: State → "Complete with failing criteria",
  Blocker → the specific criteria that failed. Do not report the plan as done —
  carry the failures into Step 6 and stop for the user.

**Then commit it.** The last phase's tick, its phase-document status, and this status block
were all written *after* the last phase's commit, so without a commit here none of them
reaches the remote: a teammate pulling origin would see a tracker still showing the final
phase open. Check what you are staging first — this commit carries plan bookkeeping and
nothing else. Anything beyond that has not been through a phase gate, so decide **whose it
is** before deciding what to do with it. If this plan owns it, put it through a phase gate in
full, then commit it on its own. If it is work this plan does not own, or ownership is
unclear, `git restore --staged` those paths so the index holds only plan bookkeeping again
— then **carry on with the commit below** and report those paths to the user after it. Do
not gate them and do not commit them. `git add -A` sweeps whatever else was dirty, and
committing it publishes someone else's change under this plan's message.

**What stops is that work, not this step.** Measured on the v2 counterpart, whose wording
here was the same: a fresh reader given this arm and one ambiguously-owned file staged,
unstaged, and stopped, leaving the bookkeeping commit unmade — the loss the paragraph above
exists to prevent, reached by obeying it. Withholding the tracker over some *other* file's
ownership trades a question anyone can answer for the one thing a teammate pulling `origin`
needs.

```bash
git add -A
git diff --staged        # STOP HERE. Plan bookkeeping only; see above.
```

Once it is — and **if putting that content through a gate changed a file, `git add -A`
again**, or the index still holds the version the gate rejected:

```bash
if git diff --staged --quiet; then
  echo "Nothing to finalize — the tracker and phase statuses are already committed."
else
  git commit -m "docs(plan): mark the plan complete"
fi
# OUTSIDE that branch, and the same two skipped destinations as 3h, for 3h's reasons: a
# crash between the commit and the push leaves a resumed run with nothing staged.
branch="$(git rev-parse --abbrev-ref HEAD)"
default="$(git symbolic-ref --quiet --short refs/remotes/origin/HEAD 2>/dev/null | sed 's#^origin/##')"
if [ "$branch" = "HEAD" ]; then
  echo "Detached HEAD — committed but NOT pushing. Check out a branch and push."
elif [ "$branch" = "${default:-main}" ] || [ "$branch" = "main" ] || [ "$branch" = "master" ]; then
  echo "On default branch '$branch' — committed but NOT pushing. Move this work to a feature branch."
elif [ "$(git rev-parse HEAD)" != "$(git rev-parse --verify --quiet "origin/$branch")" ]; then
  git push origin HEAD
fi
```

**A skipped push here is not a finished run.** Same rule as 3h: the bookkeeping is committed
but local, so the tracker a teammate pulls is still stale. Do not report the plan as
complete — report that it is complete locally and needs a branch it can be pushed from.

---

## Step 6 — Final report

Print a completion summary:
- Total phases completed (this session vs. previously)
- Success criteria results (pass/fail for each)
- Any unresolved concerns or follow-up items discovered during execution

If any success criteria failed, list them explicitly and suggest next steps.
