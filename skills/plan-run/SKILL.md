---
name: plan-run
description: >
  Execute a plan produced by /plan-init + /plan-phase — the current planning suite.
  Reads plan.md + the execution.md checkbox tracker + the current phase doc (never
  phases.md), and runs phases in order, resuming from the first unticked box and
  re-reading that phase document before executing it. Runs the per-phase gate (scoped
  tests + web-verify for UI + a single-pass cyw author review + diff-review, cross-runtime
  when a second runtime is available, on any diff with reviewable code), fills each phase's
  evidence record, makes at most one commit of its own work per phase, and assembles an
  as-built.md drift report. Refuses a plan that
  carries no `Format: v2` marker. Use when the user invokes /plan-run, or says
  "execute the plan", "run the plan", "continue the plan".
---

# Execute Plan (v2)

_Progress: observable — the orchestrator hands each delegated phase worker (and its independent-review sub-agent) an append-only per-phase progress file; non-blocking and read by nothing on the correctness path. See "Delegation" below._

## Overview

Work through a v2 plan created by `/plan-init` + `/plan-phase`. `execution.md` is a
checkbox list — one box per phase, each linking that phase's document. Execute phases in
order, starting at the **first unticked box**, and tick that box when the phase's gate
passes. Commit and push at the end of every phase.

**Two levels of durable record, the child authoritative.** The phase document is the real
state — its Work, Tests and Gate boxes are ticked as the work proceeds — and
the tracker is a derived index pointing at where the run is. Step 3 reads which window a
resumed run is in off the disk and the remote.

**Parallelism — sequential by default, allowed within bounds.** Sequential execution is
always correct and is the default. But both runtimes (Claude and Codex) can dispatch
sub-agents, so either **may** — and **should when clearly prudent** — run work in parallel,
subject to one hard safety bound:

- **Read-only work may always run in parallel** — independent analysis, exploration, or an
  independent review of already-finished work needs no isolation.
- **Concurrent writers must be isolated** — each in its own copy of the working tree; never
  run concurrent writers against one shared working tree, whose git index, lock, and commit
  boundary race and corrupt even on disjoint files.
- **Isolation covers files and the git index only** — phases that share non-file state (a
  database, ports, caches, generated artifacts, lockfiles, or test-env state) must not run
  as concurrent writers even when isolated.
- **If the writers can't be isolated, run them sequentially.**

That is the whole bound, and it is deliberately a rule rather than a procedure. Where a plan
has independent phases, `/plan-phase` says so above the phase list and names the phase that
**reconciles** them; that phase's own document carries the reconciliation — read it there.
This skill defines no branch naming, no merge order and no ancestry assertion: a written
procedure that is never executed is a procedure nobody has checked.

**Authority rule:** The phase documents and `execution.md` are the execution plan; the
codebase is the reality check. If a document references paths, APIs, or structures that
don't match the codebase, correct the document first (smallest accurate correction),
then continue against the corrected document. Here "the document" means the **phase doc or
`execution.md`** (Satisfy says who may correct which) — **never `plan.md`**, which `plan-init`
fixed as a stable reference.
If reality diverges from `plan.md` itself, do not edit it; record the drift in the phase's
evidence record and in `as-built.md`.

---

## Delegation — who runs each step

You are the **orchestrator**. Every step below is yours by default: run it here, in this
conversation, one phase at a time. Where sub-agents exist you **may** instead hand a phase's
implementation to a **worker** — a fresh sub-agent starting with a clean context, so context
stops accumulating across phases and a later phase is not coloured by an earlier one's
rationalization.

Writing `execution.md`, the independent review, and the commit and push are never
delegated — each stated at its own step below. A **reconciling** phase is not delegated at
all (Satisfy).

A worker receives exactly three on-disk paths — `plan.md`, `execution.md`, and its own phase
document — plus this skill, and nothing from this conversation, because everything durable
already lives on disk. Its brief and its `DONE` / `BLOCKED` result contract are in
`references/phase-worker-contract.md`; read that when you delegate.

**Delegate on context; parallelize on isolation.** Run a phase yourself when it is small or
tightly coupled; hand it to a worker when its implementation detail would bloat this context,
**or** when its output is cheaply verifiable by an objective gate. No sub-agents available →
run everything yourself, always. Whether workers then run *concurrently* is a **different**
decision, settled by the isolation bound above and by nothing else — collapse the two and
"parallelize when it's big" fans concurrent writers over shared state.

> **Claude adapter:** dispatch a phase with the Agent tool (subagent_type general-purpose),
> passing the worker's brief — the three input paths **and the result contract from
> `references/phase-worker-contract.md`**, which a general-purpose sub-agent does not load on
> its own — and read the result from the sub-agent's final message; in-harness there is no
> flag that can enforce its shape.
> **Codex adapter:** in an interactive session spawn a per-phase worker agent
> conversationally; autonomously (no user available), script it per phase with `codex exec -s workspace-write -c
> approval_policy=never -C <dir> --output-schema <this skill>/references/phase-worker-schema.json
> --output-last-message <file> "<the worker's brief — the three input paths and the result
> contract from references/phase-worker-contract.md>" < /dev/null`, which does enforce it.
> **The brief is that trailing positional argument.** It is spelled out because the
> paragraph below says the prompt is already in argv, and a template with nowhere to put it
> reads as though the flags alone were the command — dispatching a worker with no brief at
> all, which fails for reasons that point nowhere near the missing prompt. Those two permission
> flags are **not** optional, and neither is the redirect: `codex exec` reads stdin even
> when the prompt is already in argv, so a scripted call that leaves stdin open blocks
> before it reaches the model — a hang with no output to diagnose it by. Close stdin
> however the shell spells it (`< /dev/null`; `$null |` in PowerShell).

> **Live progress (non-blocking).** A worker's tool output is buffered and opaque until it
> returns, so a long phase can look stalled. Create a per-phase append-only progress file —
> `plans/<slug>/progress/phase-<id>.log` — and hand its path to the worker, asking for one
> timestamped line per meaningful step: `[+MM:SS] work item 3 done`, elapsed from the phase's
> start, the convention `plan-duel`'s log uses. When the worker returns, append one line
> yourself — its exit status and elapsed time — so the log ends with an outcome even when
> the worker could not write its own last line. Gitignore that directory so the throwaway
> log never lands in a commit. Nothing on the correctness path reads it, so the run
> completes identically whether or not anyone watches; a failed write is ignored. The review
> sub-agent is not handed one — it runs read-only and cannot write it, and `diff-review`'s
> supervisor already streams its output. **Claude adapter:** tail it with the Monitor tool,
> narrating each new line. **Any runtime:** `tail -f` it in another pane.

One invariant holds whoever runs the work, and with the never-delegated three above it is why
delegation changes speed but not outcome: within each phase the order is **author
self-review → independent review → commit**, and a reconciling phase still **pushes** even
when it commits nothing of its own. Publish owns the rest.

---

## Step 1 — Locate the plan and confirm it is v2

If the user provided a path argument, use it. Otherwise search for `plans/**/plan.md`;
if exactly one exists use it, else list them and ask which to run (autonomously: choose
the only incomplete `Format: v2` plan, or the most recently modified one, and note the
assumption). Read `plan.md` in full.

**Optional opt-out.** A `--no-cross-review` flag on the invocation — or a note in `plan.md`
saying the same — tells Gate to begin its independent review at **rung 2** of `diff-review`'s
independence ladder: a fresh same-model reviewer, skipping the cross-runtime rung 1. Coverage
is unchanged; the only thing given up is model-independence, and Gate records that it was.
`diff-review` has no flag of its own to receive this and needs none — the choice is which rung
Gate asks for, so it is made here and nothing is passed through.

**Confirm the `Format: v2` marker** (the `| Format | v2 |` row). If it is absent, this
is a v1 plan: do **not** run it. Tell the user to use `/plan-run-v1` instead. If operating
autonomously, stop and report: "plan is not `Format: v2` — refusing to avoid a v1/v2
collision."

Then locate **`execution.md`** in the same directory (never `phases.md` — this skill
never reads `phases.md`).

**That directory is what every `plans/<slug>/` below means** — the directory the plan file
you just read lives in, whatever it turns out to be. Discovery accepts a plan anywhere, so
a plan at `docs/proposal.md` has its `execution.md`, its phase documents and its
`as-built.md` in `docs/`, and this skill never invents a `plans/` directory to put them in.
`/plan-phase` states the same rule about its own writes and is where those files came from;
without it stated here too, nine literal-looking paths in the steps below read as a
requirement rather than the shorthand they are.

If `execution.md` does not exist, stop and tell the user:
"This plan has no execution tracker yet. Run `/plan-phase <path>` first."

**Then confirm the tracker is the checkbox shape** — it carries no format marker, so the
shape is what you check. Two refusals, both loud:

- A tracker written as `- phase:` entries is a **superseded shape**. Stop and report: "this
  plan's tracker is a superseded `- phase:` record and cannot be run by this skill."
- A tracker with **no checkbox lines at all** is malformed, not finished. Stop and report
  it; never read zero boxes as *all phases complete*.

Read `execution.md` in full.

**Preflight the tracker.** If the pack's tracker check is present in this repo, confirm
the tracker is well-formed before executing anything:

```bash
# native Windows: py -3 instead of python3
python3 scripts/check_plan_tracker.py plans/<slug>/execution.md
```

Fix any reported structural issue (correcting the document first, per the authority
rule) before proceeding. If the tracker check is not present, trust the structure
`/plan-phase` emitted and continue.

**One finding is not yours to fix silently.** `'<name>' is in this directory but no checkbox
links it` names a phase document that this run would never execute — so the tracker and the
directory disagree about what the plan *is*. Deleting the document destroys unexecuted work
and its evidence record; appending a box for it quietly enlarges the plan you were asked to
run, and its position decides when it runs. **Stop and ask the user** which the plan is. This
is the one preflight finding where both obvious repairs are wrong.

---

## Step 2 — Survey the phase list

Read **every column-0 `- [ ]` / `- [x]` line in the file**, in order: those boxes are the
phase list wherever they sit, since every one must be a canonical phase link and every phase
document in the directory must appear exactly once. The `## Phases` heading is a convention
for readers, not a parsed boundary. **If there are none, stop** (Step 1). Prose between the
boxes — which phases are independent, what reconciles them — is advisory and is not parsed.

Print a brief status report:
```
Plan: <title>
Phases complete:   N / M          (ticked boxes / total boxes)
Next:              Phase X of M — <phase document>
```

If every box is ticked, skip to Step 4 (final verification).

---

## Step 3 — Run each phase, from the first unticked box

Every phase is four transitions in order — **Select, Satisfy, Gate, Publish** — repeated
until every box in `execution.md` is ticked. Each carries a **worker-safe** or
**orchestrator-only** tag; the instructions do not change with the tag.

### Select _(orchestrator-only)_

Re-read **`execution.md` and the phase document from disk**, every time — never from
memory, since a tracker correction or an outside edit lands there and not in your context.
Walk the tracker in order to the **first unticked box**, and decide from what that phase
document says whether the phase still needs running. Note `git rev-parse HEAD` as the
phase's starting point for orientation while you read its diff; nothing downstream may
depend on it, and Publish does not.

Two boxes cannot be true yet, because **Publish** owns them: any CI-round box, and any
post-publish box naming a repository, ref and commit. Ignore them here.

- **Any other box unticked** → the phase is incomplete. Go to **Satisfy**.
- **Everything else ticked** → the work is done and only bookkeeping is outstanding. Do not
  re-run the phase and do not simply tick its box. Read which window the crash left you in
  off **observable state**, in this order — never off commit identity, since a phase that
  took Publish's remediation path legitimately has more than one commit:

  1. **Files this phase owns are dirty outside `plans/<slug>/`** → the crash landed before a
     commit, this phase's or a remediation's. Re-enter at **Satisfy**'s verification, scoped
     to what is dirty, then **Gate**, then **Publish**.
  2. **Otherwise the push destination is not at `HEAD`** → the crash landed between the commit
     and the push. Run **Publish's push** — the guarded one, not a bare `git push`: a resumed
     run is exactly when the checkout is detached or sitting on the default branch, and those
     checks are the reason Publish spends fifteen lines on them. Then continue to 3; the push
     is what starts CI.
  3. **Otherwise** → the crash landed after the push, or while waiting on CI. Re-check any
     CI-round box **against the repository, ref and commit it names**; once it holds, finish
     **Publish**. If that round is **red**, take Publish's remediation path inside this phase.
     If no such round exists at all, branch on why — only the first case is a wait. A push you
     can make would produce it → run Publish, then wait. The box names another repository →
     only this phase's own Work pushes there, so re-read it. Back to **Satisfy** in exactly
     one case: the Work describes that push and it never happened — and **scoped to that push
     alone**, the way branch 1 scopes to what is dirty. When it lands, run **Publish's push
     block alone** — not the whole of Publish, whose finalization commit is already made and
     which would otherwise add a second one carrying nothing but a tick. (The v1 suite
     states this outright, having measured that second commit; v2 ended the branch without
     naming a continuation and inherited the default. Named rather than cited: an installed
     skill is self-contained and cannot reach a sibling skill's file.) Never re-run
     Work whose box is already ticked: an unscoped return either does nothing and lands you
     back here, or repeats an action Satisfy's repeat guard does not cover, because that
     guard is scoped to *unchecked* items. Do not read that as "everything is ticked" — the
     box that put you here may not be, and the section above exempts post-publish boxes too.
     If the scoped push runs and still no round appears, **stop and report** rather than
     returning here a second time.
     Every other reading is **stop and report** too — the Work describes no such push, or
     describes one that was made and produced no round — because Satisfy would otherwise
     invent a push to another repository, or repeat one already made. Neither → **stop and
     report**, and never tick it off another commit's run.

  **Branches 2 and 3 are not "the tree is clean."** Metadata left dirty *inside*
  `plans/<slug>/` is expected at every one of these points and proves nothing: the previous
  phase's tick is deliberately uncommitted, and this phase's own ticked boxes and evidence
  record are written before the commit that carries them. A phase whose only output is that
  metadata commits nothing at all and still reaches 3.

  **And if what is dirty outside `plans/<slug>/` is work this phase does not own**, branch 1
  is the wrong reading — it would review someone else's change as this phase's and sweep it
  into this phase's commit. Compare against the phase's Work and its evidence record's
  changed line, then **stop and ask** — when ownership is unclear, and equally when it is
  clearly not this phase's. "Not branch 1" is not permission to fall through: 2 and 3 end at
  a `git add -A` that commits the file you just decided this phase does not own.

**A phase that stopped stays unticked**, with the reason in its document (Blocking behavior).
A phase deliberately **skipped** gets its box **ticked**, the reason likewise in its document
— the work is settled either way, which is what a ticked box means.

### Satisfy _(worker-safe, except a reconciling phase)_

Do the phase's Work and get its Verification green.

**Check the document against the codebase before editing.** If it references paths, APIs or
structures that do not exist as described, make the smallest accurate correction and note
what changed. Correcting the **phase doc** is worker-safe; correcting **`execution.md` is
orchestrator-only**, like every write to it — a delegated worker reports the divergence in its
result's `deviations` and continues, and you apply the tracker correction when you take the
result, before the review. A divergence reported and never applied selects the next phase off
a stale entry.

**Work through every item, ticking each box as it is done** (`- [ ]` → `- [x]`) — never in a
batch at the end. Every checkbox is state a resumed run reads; one left open because nobody
was told to tick it makes a finished phase look unfinished. Follow the project's `AGENTS.md`
/ `CLAUDE.md` conventions, or the surrounding code's.

**Before repeating an unchecked external or irreversible action — a tag, a release, a PR, a
published artifact — check whether its outcome already exists; if it does, tick the box
instead of acting.** A tick is a record, not a lock: a crash between the action and the tick
is exactly the case that leaves an unticked box behind a completed action.

**Timing.** During the work run only the new or affected tests; once the Work boxes are
complete, run each Verification command once. Diagnose and fix failures and re-run until
clean — a failure is a blocker even if your change did not cause it (fix it, or surface it
through the blocking protocol if it is demonstrably pre-existing and out of scope). Tick each
box in **Tests** as its test is written and passing.

**Waiting for a long command.** A command that outruns one foreground call runs under the
runtime's background-execution facility where that facility reports the exit. Where it does
not, launch the command once and, in a **later call**, poll for an artifact it writes. Never
poll a command-line pattern (`pgrep -f`, `pkill -f`, `ps | grep`): the shell running your
poll holds the command text in its own argv, so the pattern matches the poller and the loop
never exits. Have the command append a marker carrying its exit status, so the marker is the
outcome and not just the end:

```bash
# Launch call, detached so the work outlives it. Literal paths: shell state does not survive.
# The command itself is in /abs/path/work.sh, written with your file tool, one command per line.
rm -f /abs/path/work.log; set -m; S=$(command -v setsid || true)
$S nohup bash -c 'set +e; bash -eo pipefail /abs/path/work.sh; printf "\nWORK-EXIT rc=%s\n" "$?"' \
  > /abs/path/work.log 2>&1 < /dev/null &
# A later call. Bounded — a poll that can spin forever is the orphan this rule prevents.
for i in $(seq 1 240); do grep -q '^WORK-EXIT rc=' /abs/path/work.log 2>/dev/null && break; sleep 15; done
```

The marker cannot match the poller because `grep` reads a file, not the process table;
removing the old log first stops a previous run's marker matching. `set -m` gives the
background job its own process group, `nohup` ignores the hangup, and `setsid` puts it in
its own session where the command exists (Linux; macOS ships none). A runtime that kills a
timed-out call's process group cannot reach the work on any host; one that kills the whole
session cannot reach it only where `setsid` ran — which is one more reason the runtime's own
background facility comes first. The command lives in a file so nothing re-expands it:
a `$(…)` in a quoted command string would run in the launching shell, not the work's. The
outer `bash -c` starts with `set +e`, so `$?` is captured whatever errexit the launching
environment exports; `bash -eo pipefail` runs the file so a sequence stops at its first
failing step and a failed producer in a pipe (`npm test | tee`) is not hidden by its
consumer, and a missing file is a non-zero marker rather than a silent non-start. One
command per line in that file: errexit exempts `a` in `a && b`, so a failed test on a
chained line lets the next line report success. `printf`'s leading newline
lands the marker on its own line after output with no trailing newline. Where you also need to know the work is still alive, write its
PID to a file at launch and poll `kill -0` on it — a PID cannot match a pattern, and it
tells you liveness only, never the outcome: the marker still carries that. (PowerShell:
`Select-String -Quiet` and `Get-Process -Id`; the rule is the marker, not the syntax.)

Two things the marker's absence means. **A non-zero exit from the poll is the poll dying or
timing out, not the work failing** — the work's outcome is the `rc=` in the marker, and a
killed watcher's exit reads exactly like a failed suite. And **no result is reported before
the work has reported its exit** — the runtime's, where its facility reports one, or the
marker's where you polled: a phase reported complete with its log part-way is not complete.
Never add a trailing `&` inside a call already run in the background — the outer shell
returns at once, the call is reported finished, and the work runs on unobserved.

**Testing discipline (red → green, self-sufficient).** For every item that adds new behaviour:

- **Order.** Logic, APIs and utilities: write the failing test **first**; UI and wiring: tests
  alongside. No new behaviour ships without tests — not optional.
- **Red for the right reason.** Confirm the new test fails because the behaviour is *absent* —
  an import/attribute error for something new, or a **failing assertion** when extending an
  API that already exists — not from a broken test. If it passes immediately, the behaviour
  already exists — stop and note it instead of adding code.
- **Minimum green.** Write the least code that passes, then re-run only the affected test(s).
- **Never fake green.** Do not weaken or delete assertions, edit the test to fit the code, or
  add `# noqa` / `eslint-disable` / test-only branches in production code to force a pass —
  fix the implementation.
- **UI (optional).** You may drive the spec and glance at a screenshot as you build, to
  shorten the write → render → adjust cycle. That glance is a convenience, never the gate:
  the authoritative frame inspection is `web-verify` at **Gate**.

The `tdd` skill automates this loop where it is available; otherwise follow the steps above
directly. Nothing here commits — Publish makes the phase's single commit.

**Cheap static checks belong here, not in CI.** Run the project's linter, and a scoped
type-check where the project has one, in **check-only** mode over the changed surface: a
lint-only failure deferred to CI costs a whole remote round. A formatter that rewrites files
runs before review, never as a silent gate side effect.

After any file move, search the repo for stale references and update them.

**A reconciling phase brings the work together first, and is never delegated** — you run it
end to end. The merge spans phases and no single worker can see the set it reconciles, and
splitting the phase around it leaves the review range and the merge task owned by nobody. Its
Work names the merge. **Re-read the reconciled phases' documents and their Evidence records
from disk first** — that material is what you reconcile, and if those phases were delegated
their workers' context is gone. **Record the pre-merge SHA in the phase document before
merging**: Gate reviews against it, and shell state does not survive the step. Resolve conflicts as an
ordinary edit, and satisfy yourself that every piece of work the phase names is actually
present before verifying — a green suite cannot detect a feature that simply is not there, so
a partial merge passes silently. Its Verification runs the **full** suite against the combined
result, not the last piece's checks. Work produced in list order on one branch — the default
— has nothing to merge and none of this applies.

### Gate _(split: the first three axes worker-safe; the independent review, and the dispatch, re-review and adjudication of any fix pass, orchestrator-only)_

Run each applicable axis; each must pass before the phase can complete.

**Where a skill named below is unavailable, do the equivalent manual pass it bundles** —
`web-verify`'s UI checklist across the mandatory viewports, `cyw`'s re-read of the modified
files for correctness, completeness and consistency. That substitution holds for every named
skill below, including Step 4.

- **Tests** — green, no regressions (Satisfy covered this).
- **Visual/behavioral (UI phases)** — run the `web-verify` skill, or the manual equivalent
  above. A produced artifact alone never counts as verified.
- **Author review** — one pass over your own change: the `cyw` skill in **embedded
  single-pass mode**, or the manual equivalent above. If the fix changes production code,
  re-run the **scoped** test(s): Satisfy ran before it.
- **Independent review** — run the `diff-review` skill over the phase's diff as a **separate
  fresh reviewer**, which never sees the author's reasoning and never re-runs suites the
  author already ran green. Where no independent reviewer can be reached at all, do the
  equivalent yourself in a deliberate context reset: re-read the diff with fresh eyes,
  ignoring the implementation rationale, and address correctness findings. How independent
  that reviewer is, how it is bounded, what it does when the other runtime stalls, and what
  each severity means are `diff-review`'s to decide — do not re-derive them here. The one
  thing decided *here* is the starting rung: with `--no-cross-review` in effect (Step 1),
  start at rung 2 and note in the evidence record that the cross-model rung was skipped by
  request, so a reader can tell an opted-out run from one where the other runtime was absent.

**Both review axes are skipped when the phase produced no reviewable diff** — its only changes
are metadata under `plans/<slug>/`, as for a pure closing or verification-gate phase. Tick
their boxes anyway, with the reason (`— skipped: no reviewable diff`): a box left open because
its axis was skipped **by design** is indistinguishable, to a resumed run, from work that never
happened. **A phase that merged is not automatically that case.** A clean merge of
already-reviewed work carries no new code, but where resolving a conflict meant *authoring* a
resolution there is a reviewable diff, and it is `git diff <pre-merge-sha>` against the
**working tree**, using the SHA Satisfy recorded. Not `<pre-merge-sha>..HEAD`: that compares
*commits*, and the phase has not committed yet — Publish does — so the tempting two-dot form
drops everything written since the merge and leaves the one piece of hand-authored code in a
reconciliation the one piece nothing reviews. **Run `git add -A -N` first**: a file authored
while resolving the conflict is untracked, so it is outside `git diff <pre-merge-sha>`
entirely and the reviewer never sees it — and Publish's `git add -A` then commits it. The
intent-add leaves the merge state alone (`git ls-files -u` stays empty).

**The gate is: no open blocker/major before the commit.** "Clean" means no open blocker/major,
not zero findings — minor/nits go in the evidence record as non-blocking follow-ups, never
re-dispatched, because a peer model's style nit must not stall an autonomous run. On a
blocker/major, **record it into the phase document** as a "Review findings to address" block —
the same durable on-disk channel the BLOCKED path uses — then run a **scoped fix pass**: that
finding, the tests for the surface it touches, and `web-verify` if the fix changed rendered
UI, never a re-run of the phase. If the phase was delegated, **re-dispatch the worker** for
that pass; that is why the findings go in the
document rather than into a message, since the document is what a fresh worker reads. Then
re-review. Each round **replaces** the previous block; delete the block once a re-review is
clean, so a later round cannot reprocess stale findings.

**Cap the loop at 2 review passes**, then adjudicate what is still open yourself: **fix** it
(re-running the scoped test(s), plus `web-verify` when rendered UI changed), or **refute it
with evidence** recorded in the document. A reviewer-labelled blocker is cleared by a fix or
an evidenced refutation and never downgraded by fiat. One you can neither fix nor refute stays
**open**: leave the phase's box unticked, record it, and surface it — escalating to a human
where there is one, otherwise reporting rather than committing. Never commit past an open
blocker/major.

**Then fill the phase's evidence record** — the phase's **outcome** (shipped / partial /
skipped and what it delivered, the line `as-built.md` reads), the changed surface, the
verification commands and their outcomes, deviations (including the doc-vs-reality
corrections made in Satisfy), and any non-blocking follow-ups. Reference frames, screenshots and reports **by path or CI URL only**;
never inline a heavy artifact and never commit one. This record is what a human reads and what
`as-built.md` is assembled from — the independent review reads the diff, not this.

**Delegated, the evidence record is still yours to write.** The worker *reports* it — the
`DONE` object's `summary`, `changed_surface`, `verification` and `deviations` are the four
fields above — and you write it, adding what the worker could not know because it happens
after the worker returns: the independent review. One author per section; the contract tells
the worker the same thing.

**Tick every gate box you can establish.** Delegated, this splits too: the worker ticks its
own work, tests and author review, and cannot tick the **independent review** box, which
happens after it returns — you tick that one once your review is clean. Leave only what
Publish owns.

### Publish _(orchestrator-only)_

**A phase makes at most one commit of its own work, and one push.** A reconciling merge is not
the phase's own authored change and is exempt; a CI remediation below is the one explicit
exception. Never a mid-phase, checkpoint or WIP commit, and never a second bite. *At most*,
not exactly one: a phase that stages nothing makes none, which the guard below handles.

**Inspect the staged diff, and stop on it.** A **mechanical check**, not a re-review of
intent, which the independent review just did — but a real stop: read the diff and decide,
because a diff printed and then committed on the next line is not an inspection of anything.
Confirm only that no heavy artifact (screenshot, video, trace, frames, report) and no
unexpected path is staged. Since `git add -A` sweeps whatever else was dirty, this is the
last moment to catch an unrelated edit; fix and re-stage before going on.

**Two blocks, and the split is the point.** The first ends at the diff; the second runs
only after you have read it. A single block with the commit inside it cannot be stopped at
— an agent executes it as one unit and the commit lands whatever the diff held, which is
the failure the paragraph above describes.

```bash
git add -A
git diff --staged        # STOP HERE. Read it. Nothing below runs until you have.
```

Now decide. If anything unexpected is staged, fix and re-stage before going on. Then:

```bash
if git diff --staged --quiet; then
  echo "Nothing to commit for this phase (verification/reconciling) — skipping commit."
else
  git commit -m "<conventional-commit subject + why, from the phase's Goal and evidence>"
fi
# Push when the branch tip is not yet on its push destination. Deliberately OUTSIDE the
# branch above, and deliberately stateless, so a resumed run recomputes the same answer
# from the repository alone. It asks whether the TIP is published, not whether this phase
# advanced the branch, so an unrelated unpushed commit gets published too. Assumes the
# conventional `origin` with a same-named branch — substitute your own destination ref
# otherwise (`@{push}` resolves it where an upstream is configured).
BRANCH=$(git rev-parse --abbrev-ref HEAD)
DEFAULT=$(git symbolic-ref --quiet --short refs/remotes/origin/HEAD 2>/dev/null | sed 's#^origin/##')
# Two destinations a phase must never publish to, both SKIPPED rather than failed — the
# work is committed either way, and a run that stops here would strand it.
if [ "$BRANCH" = "HEAD" ]; then
  # Detached: `--abbrev-ref HEAD` answers the literal string, so `git push origin HEAD` has
  # no destination ref to resolve. Check out a branch, then push.
  echo "Detached HEAD — committed but NOT pushing. Check out a branch and push."
elif [ "$BRANCH" = "${DEFAULT:-main}" ] || [ "$BRANCH" = "main" ] || [ "$BRANCH" = "master" ]; then
  # Plan execution belongs on a feature branch; pushing each phase to the trunk publishes
  # work in progress. `DEFAULT` is empty where `origin/HEAD` was never set, hence the
  # literal fallbacks beside it.
  echo "On default branch '$BRANCH' — committed but NOT pushing. Move this work to a feature branch."
elif [ "$(git rev-parse HEAD)" != "$(git rev-parse --verify --quiet "origin/$BRANCH")" ]; then
  git push origin HEAD
fi
```

> **Adapter note — this block is `sh`/Bash syntax.** The `if`/`elif`, `$(...)` and `[ ... ]`
> forms do not parse in `cmd` or PowerShell, so on a native-Windows shell run it under Git
> Bash or WSL, or carry out the same four decisions with your shell's own syntax: skip the
> push on a detached `HEAD`; skip it on the default branch; otherwise push when the tip is
> not already on `origin/<branch>`; and commit only when something is staged. **The
> decisions are the contract, not the spelling** — a completed phase that cannot run this
> block is a phase left uncommitted and unticked, which is the one outcome the ordering rule
> below exists to prevent.

**A skipped push is not a completed Publish.** Both guards above skip rather than fail, so
the work is committed and nothing is lost — but the commit is local, and the rule below says
plainly what a tick before a push costs. Do **not** tick: stop and report that the run is on
the default branch, or detached, and that the work needs moving to a feature branch. It
resumes from here once it is.

**Then tick any post-publish box in the phase document, and then this phase's box in
`execution.md`** — never before the commit and push above. **Commit → push → tick is the whole
ordering rule, and every part of it is load-bearing.** A box ticked before the commit claims a
phase is settled while its work is uncommitted, and a resumed run believes it and walks
straight past. One ticked before the push strands the commit locally, so the CI round the
phase may be waiting on never starts. And child before index, because a ticked box makes the
run skip this phase forever — a crash between the two would strand the authoritative document
claiming work nothing will revisit.

The tick lands uncommitted, which is correct: `plan-run` reads `execution.md` **from disk**,
so a resumed run sees it and the next phase's `git add -A` sweeps it up. Nothing else in the
tracker changes.

**A phase whose exit depends on CI blocks on its round.** Such a box — its verification can
only run in the CI/prod environment shape, where resolution, hoisting and pathing differ from
dev — is not satisfied at push time. Commit and push above, then **hold the tracker tick**
until the round is green.

**Watch the round the box names, not whichever round this push happened to start.** Take its
repository, ref and commit from the phase document: a phase whose own Work pushes a
*different* repository is gated by **that** round, while the push here may carry nothing but
plan metadata. If the phase pushed nothing, no new round exists; check the round for the
commit the phase ends at, and see Select's branch 3 for when there is none.

If CI is **red**, the phase is not finished and its box stays unticked. Fix it **inside this
phase**: record the failure and the fix as Work here, put the fix through Satisfy and Gate,
and land it as a **remediation commit** — the one explicit exception to one commit and one
push, the same exception Step 4 makes at final verification — whose push starts a fresh round.
Repeat until green, then tick. The exception is to the count, never to the gate: whether a
remediation gets reviewed must not depend on whether the process crashed.

**Do not create a separate remediation phase.** A phase appended after this one is
unreachable: the run always resumes at the **first unticked box**, and that is this phase, so
it would be re-entered forever while the remediation below it never ran. If the failure is
something this phase cannot fix, record it, leave the box unticked, and stop and report.

Then print `✓ Phase X complete.` and go to **Select** for the next phase. Do not pause between
phases unless something blocks the run.

---

## Blocking behavior

If anything blocks a phase: stop; do not advance. **Leave the phase's box unticked and
record the reason in the phase document** — that is the whole mechanism. There is no
`blocked` status in the tracker. Report to the user with precise detail. Resume
only after the blocker is resolved; if a narrow, justified decision resolves it safely,
make it, document it, then continue. If operating autonomously (no user available), take
the safest justified option, record it as an assumption in the phase doc, and continue.

If you must discard partial edits from a blocked attempt, **scope the cleanup to the
phase's own files**: revert only the specific paths you changed (`git restore -- <paths>`
for tracked edits; delete only files you created) — **never** a blanket `git reset --hard`
or `git checkout .`, which would also discard unrelated uncommitted work. Leave any
uncommitted changes the phase did not create untouched; if ownership is unclear, stop and
ask.

**A worker cannot pause to ask** — it returns `BLOCKED{question, options, recommendation}`
and stops. Leave the phase's box unticked, surface the question (autonomously: take its
recommendation), record the question **and the answer** into the phase document, and
re-dispatch a **fresh** worker, which re-runs the phase from the now-updated document so the
recorded decision drives it rather than stale mid-phase edits.

Its cleanup follows the same ownership rule: revert **only** the paths its result lists — and
if it reported `changed_surface: "none"` while the tree still shows edits the phase appears
to own, stop and ask.

**Untick every box the reverted work had ticked** — Work, Test or Gate. The contract puts
this duty on the worker, which discharges it whenever the worker reverts its own edits; on
the branch where it hands you a path list instead, it reverted nothing, so the sentence is
vacuous and the duty lands here. A box that outlives the work behind it is worse than no box
at all: the fresh worker you dispatch next reads it as done and skips work that no longer
exists — and then the phase gates, commits and ticks with that work simply missing. The
scoped tests and the independent review are a partial backstop and neither is obliged to
notice absent work, which is exactly why the duty is written down rather than inferred.

---

## Step 4 — Final verification (all phases complete)

When every box in `execution.md` is ticked — including any phase deliberately skipped,
whose box is ticked with the reason in its document, so a plan containing one is not left
in limbo — run the full Success Criteria from `plan.md`. For each
criterion, run the specified command or check the condition and note pass/fail. If a prior
committed full-suite run (a reconciling phase, or a following no-diff verification-gate
phase) is still valid — the current **HEAD, the exact command, and the environment all match
that run** — reuse its results for the matching criteria instead of re-running; any
intervening commit, including a CI remediation, invalidates the reuse.

**A failing test here is a blocker, not a result to record.** Fix it, re-run, and commit the
remediation as its own commit — the run is not complete until the tests are green. **The fix
goes through the gate first**, as a phase's CI remediation does: every Gate axis that
applies to it — scoped tests, the author pass, `web-verify` if it changed rendered UI, and
the independent review. It is the last production change in the plan, on the commit a reader
lands on. (A non-test success criterion that fails for scope reasons is still reported per
Step 7.)

---

## Step 5 — Assemble `as-built.md`

For a **non-trivial** plan (≥ 3 phases, or any plan with independent phases), assemble
`plans/<slug>/as-built.md` using `references/as-built-template.md`. Its first heading is
**"As-Built Spec and Drift Report."** Assemble it from the per-phase outcome/deviation
notes captured in the filled evidence records plus a **drift report** comparing the final
result against `plan.md`'s success criteria (met / partially met / not met, with a
one-line reason). Reference artifacts by path/CI-URL only. For a trivial plan, skip
`as-built.md` and note that it was skipped.

---

## Step 6 — Commit the run's finalization

There is no status block to update: a fully ticked list already *means* all phases complete.
What is still outstanding is the run's **final commit**. `as-built.md` (Step 5) and the last
phase's tracker tick were both written *after* that phase's commit, so without a commit
here they never reach the remote — a teammate pulling origin would see no drift report and a
tracker still showing the last phase open.

**Check what you are staging first.** This commit should carry `as-built.md` and metadata under
`plans/<slug>/`, nothing else. Anything beyond that — production code from a Step-4 remediation
a crash interrupted before its gate — has **not** been verified or reviewed, and `git add -A`
would sweep it in silently. This is the plan-level counterpart of Select's branch 1, and it
carries branch 1's ownership question too: decide whose the content is *before* deciding what
to do with it. If this plan owns it, put it through the gate it missed, **Gate** in full, then
commit it **on its own** — a gated change is its own commit, not a passenger on the
finalization commit below, whose subject says it carries `as-built.md`. If it is work this
plan does not own, **or ownership is unclear**, `git restore --staged` those paths so the
index holds only `plans/<slug>/` again — then **carry on with the commit below** and report
those paths to the user after it. Do not gate them and do not commit them. Only an
affirmative "this plan owns it" licenses the gate; unclear is not a yes, here for the same
reason it is not one in branch 1.

**What stops is that work, not this step.** Measured: a fresh reader given this arm and one
ambiguously-owned file staged, unstaged, and stopped, leaving `as-built.md` uncommitted —
the loss the paragraph above exists to prevent, reached by obeying it. Withholding the drift
report over some *other* file's ownership trades a question anyone can answer for the one
thing a teammate pulling `origin` needs. Then reuse Publish's empty-diff guard and its
destination check.

```bash
git add -A
git diff --staged        # STOP HERE. The paragraph above is about THIS diff: anything
                         # beyond plans/<slug>/ has not been through a gate.
```

Once it is only what it should be — and **if the gate above changed a file, `git add -A`
again**, or the index still holds the version the gate rejected:

```bash
if git diff --staged --quiet; then
  echo "Nothing to finalize — as-built and the final tick are already committed."
else
  git commit -m "docs(plan): finalize as-built.md and mark plan complete"
fi
# OUTSIDE that branch, and the same checks as Publish: a crash between the commit and the
# push leaves a resumed run with nothing staged, so a nested push would never run. The two
# skipped destinations are Publish's, for Publish's reasons.
BRANCH=$(git rev-parse --abbrev-ref HEAD)
DEFAULT=$(git symbolic-ref --quiet --short refs/remotes/origin/HEAD 2>/dev/null | sed 's#^origin/##')
if [ "$BRANCH" = "HEAD" ]; then
  echo "Detached HEAD — committed but NOT pushing. Check out a branch and push."
elif [ "$BRANCH" = "${DEFAULT:-main}" ] || [ "$BRANCH" = "main" ] || [ "$BRANCH" = "master" ]; then
  echo "On default branch '$BRANCH' — committed but NOT pushing. Move this work to a feature branch."
elif [ "$(git rev-parse HEAD)" != "$(git rev-parse --verify --quiet "origin/$BRANCH")" ]; then
  git push origin HEAD
fi
```

**A skipped push here is not a finished run**, for Publish's reason: the finalization
commit is local, so `as-built.md` and the last phase's tick never reach the remote. Do not
report the plan complete — report that it is complete locally and needs a branch it can be
pushed from. Nothing below applies until it has been.

**This commit moves `HEAD`, so it is the one any at-HEAD claim is about.** If the plan's
success criteria include a green CI round — "all jobs green on the final commit", or
anything else asserted of `HEAD` — the round for the commit `HEAD` now names is the round
they refer to, not the last phase's. Watch it before reporting completion, and confirm the
run you are reading is that commit's (compare its head SHA to `git rev-parse HEAD`; a run
list alone proves neither).

If **no** run exists for it, the push is not what is missing — the destination check above
already ran. Confirm the destination holds `HEAD`; if it does, no round is coming, so **stop
and report** rather than waiting, and never tick the criterion off another commit's run.

If it is **red**, fix it, put the fix through the gate as Step 4 requires — every applicable
Gate axis — then commit the remediation, push, and re-check. The run is not
complete until the commit a reader will land on is green.

---

## Step 7 — Final report

Print a completion summary: phases completed (this session vs. previously); the
success-criteria results (pass/fail for each); the `as-built.md` path (or that it was
skipped); and any unresolved concerns or follow-ups. If any success criterion failed,
list it explicitly and suggest next steps.

## References

- `references/phase-worker-contract.md` — the worker brief and its `DONE` / `BLOCKED`
  result contract. Read it when you delegate a phase.
- `references/phase-worker-schema.json` — the schema that contract's result matches.
- `references/as-built-template.md` — the "As-Built Spec and Drift Report" template.
