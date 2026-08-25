---
name: diff-review
description: >
  Independent, diff-first code review of a change set. A dedicated reviewer reads
  the diff first with fresh context — no reliance on the implementation rationale —
  and reports correctness findings without editing the tree. Distinct from author
  self-review: it is the second, adversarial pair of eyes at a phase or PR
  boundary, focused on whether the code is actually correct. Use when the user
  invokes /diff-review, or says "review this diff independently", "do an
  independent code review", "review the branch before I merge".
---

# Diff Review — Independent Diff-First Code Review

_Classification: Degraded — independence has three rungs. Strongest is a reviewer whose **model differs from the diff's author** (context- *and* model-independent — a different model has different blind spots); next is a fresh independent sub-agent in the same runtime (context-independent); weakest is an in-context deliberate reset (read the diff as an outsider, ignoring the rationale that produced it). Rung 1 launches the other runtime under a bundled **Python 3** supervisor (`review_runner.py`); where Python 3 or the other runtime's CLI is absent, the skill falls open to rung 2. Coverage is always preserved down this ladder; only the strength of independence varies with what the host offers._

_Progress: observable — rungs 2–3 return in one shot; rung 1's background reviewer streams into an **append-only display log** a human may `tail -f` while it runs. The log is read by nothing on the correctness path: the supervisor (`review_runner.py`) judges liveness from the child's live stream **in-process** (never by re-reading the log), the authoritative verdict comes from a **separate findings file**, and termination is the supervisor's own (child exit / idle timeout / wall-clock deadline) — so the review completes identically whether or not anyone watches the log._

## Overview

Provide a **second, independent** pass over a change set — the adversarial reviewer,
not the author. It is deliberately different from author self-review (`cyw`): where
self-review is broad and benefits from knowing *why* the code was written this way,
this review is stronger precisely because it does **not** know or trust that
rationale. It reads the diff on its own terms and asks whether the code is correct.

**Strongest with a second runtime installed.** When the host has another runtime's CLI available
(e.g. Claude reviewing Codex-authored code, or the reverse), the review runs in the runtime that
did *not* write the code, so a **different model** — different training, different blind spots —
examines it, catching classes of defect the authoring model is systematically likely to miss. With
only one runtime it falls open to a fresh **same-model** reviewer (independent context, same model),
so coverage is preserved either way — see the independence ladder in Step 2.

Two properties are non-negotiable:

- **Diff-first, fresh context.** Review the diff as an outsider would, without the
  implementation conversation coloring judgment.
- **No tree edits — enforced, not requested.** The reviewer *reports*; it never fixes. On rung 1
  this is a hard read-only bound via per-instance flags (Codex `-s read-only -c approval_policy="never"`;
  Claude `--permission-mode plan`) that block the model's own edit and shell paths — not merely a
  prompt asking it not to. (Those flags do **not** constrain user-configured hooks, plugins, or MCP
  servers, which run outside the sandbox; for an airtight boundary, run the reviewer under an
  OS-level read-only mount or with customizations disabled.) Findings go back to the author/gate.

## Step 1 — Select the diff

Establish exactly what is under review, read-only:

- Uncommitted work: the staged and unstaged changes, **and untracked files** — a file
  authored during this work is outside every `git diff` range and reaches the commit
  unread. List them with `git status --porcelain -uall` and read them directly. **Never
  `git add -A -N`** to pull them into a diff: it writes to the index, changes what
  `git status` shows the user afterwards, and is a modification by a skill whose whole
  contract is that it makes none.
- A branch/PR: the diff against its base (e.g. `git diff <base>...<head>`).
- A single commit or range when asked.

If the scope is ambiguous, ask the user which range to review. If operating
autonomously (no user available), resolve the base as the **merge-base against the
repository's default branch**; use `@{upstream}` only where it tracks a *different*
branch, never this branch's own remote counterpart — on a pushed branch that base is
HEAD, so the selected diff is empty, and with unpushed commits on top it is a silent
subset of the branch. State the resolved base in the noted assumption.

**An empty selection is a stop, not a clean result.** If the selected diff is empty,
review the staged (then unstaged, then intent-added untracked) changes; if those are
empty too, report **no diff under review** and stop. Reporting "nothing substantive was
found" with `blocking_count: 0` is how a gate passes code nobody read.

## Step 2 — Establish an independent review context

Review with as little contamination from the authoring context as possible. Independence
has two dimensions: **context** (the reviewer does not see the authoring conversation) and
**model** (a *different* model has different blind spots than the author's). Prefer the
strongest rung the host offers, and **fall open** down the ladder — an unavailable reviewer
must never block the review:

1. **Different runtime (strongest — context- and model-independent).** If a runtime other
   than the author's is available, dispatch the review to it over the selected diff and
   collect its findings. A different model catches classes of defect the author's model is
   systematically blind to. Bound it and fail open (below).
2. **Same-model fresh reviewer (context-independent).** No other runtime available → spawn a
   fresh independent reviewer in the current runtime that reads only the selected diff and
   the minimal surrounding code, not this conversation.
3. **In-context deliberate reset (weakest).** Cannot spawn any independent reviewer →
   perform a **deliberate reset**: set aside the implementation rationale, re-read the diff
   from scratch as though seeing it for the first time, and judge it on the code alone. Note
   in the report that the review ran in-context (Degraded).

Because a spawned or cross-runtime reviewer does not inherit this skill, the prompt handed to
it must carry the Step 3 correctness checklist, the Step 4 report format, and the "report
only, never edit the tree" constraint.

**Rung 1 runs the different-model reviewer under a bundled supervisor.** Launch it through
`review_runner.py` (shipped beside this skill) — a short-lived Python 3 supervisor that owns the
reviewer process for **exactly the review's lifetime**, then reports and exits, so a single blocking
call is self-bounding and safe to wait on. Invoke it by its **absolute path** with a resolved Python 3
interpreter (it lives next to this `SKILL.md`), and pass **`--cwd <repo>`** so the reviewer runs in
the repository — a runtime whose diff flag doesn't change the working directory needs this. It tees the reviewer's stream to an optional **display
log** (a human may `tail -f` it in a separate pane), routes the authoritative **verdict** to a
*separate* findings file, and **bounds** the reviewer three ways: the child's **exit**, an **idle /
heartbeat timeout** (kills on a full window of silence, judged from the child's live stream — so a
long-but-*active* review runs to completion while a genuine hang dies early), and an absolute
**wall-clock deadline** backstop. It prints a one-line JSON status; on a status that means the
review did not happen — idle-timeout, deadline, launch error — or when **Python 3 or the other
runtime's CLI is unavailable**, **fall open** to rung 2 (or 3), recording the attempted rung, the
fallback, and the reason. **A missing verdict is not one of those.** The Codex adapter deliberately
ships no schema flag (see the adapter note below), so a review that returns good prose and no
parseable object is the *expected* outcome there, and it is a **successful** review — read the
findings as prose, as the enforcement paragraph below says. Falling open on it would throw away a
completed cross-model pass and redo it same-model, losing the one property rung 1 exists for. Keep the display, findings and verdict files **outside the repo worktree** (or gitignored)
and delete them after; never stage them. Liveness is judged from the child's live stream in-process,
so the display log stays read-by-nothing on the correctness path — the caller consumes the
reviewer's **full review transcript** (all its message text, assembled by the supervisor from the
stream into the findings file), not the raw event log, not its tool calls, and not the tree.

**The verdict is machine-read, so its shape is pinned where that is free — the narrative
always wins where it is not.** The object a gate keys on is `review-schema.json` (shipped
beside this `SKILL.md`): `findings[]` with `file` / `line` / `severity` / `summary` /
`failure_scenario`, plus `overall` and `blocking_count`. Pass `--verdict-json <v>` and the
supervisor writes that object to its own file; the one-line JSON status then reports
`verdict` (the path, or `null`) and `verdict_reason`. `--findings` **still receives the
reviewer's full narrative** — the verdict is written *alongside* the reasoning, never in
place of it, because the reasoning is what a human reads and what makes a finding
actionable.

> **Adapter note — why only one rung-1 command carries a schema flag (measured, not assumed).**
> The two runtimes put a schema-validated object in different places, and only one of them
> keeps the prose:
> **Claude** returns the object on its terminal result event (`structured_output`) while its
> assistant messages stay ordinary prose — two separate channels, so enforcement costs nothing
> and its command carries `--json-schema ⟪schema_json⟫`.
> **Codex** coerces *every* `agent_message` to the schema, so under `--output-schema` its
> running narration is replaced by a series of stub verdict objects with empty `findings`
> arrays — the reasoning is destroyed, not relocated. Its command therefore omits the flag and
> the prompt asks for a closing verdict object instead, which the supervisor extracts from the
> transcript. Unenforced there, and that is the correct trade: losing the reasoning is a
> regression, not a simplification.
> (`plan-duel`'s judge has no such conflict — it captures only the final message, so nothing
> narrative exists to lose, and both of its judge adapters enforce the schema.)

Where a flag *is* used, one schema file serves every runtime: the supervisor substitutes
`⟪schema_path⟫` (an absolute path, for a flag taking a file) or `⟪schema_json⟫` (the same
document inline, for a flag taking the schema as text) into the reviewer argv, so neither the
schema nor the review prompt is forked per runtime.

Enforcement is **degrade-only** everywhere. Rung 2 dispatches in-harness with no CLI flag at
all, and a reviewer that returns a good narrative without a parseable object is still a
**successful** review — the supervisor reports the missing verdict and the caller reads the
findings as prose. Never require the object.

`findings[]` is the authority and `blocking_count` is derived from it, so when the two
disagree the supervisor **recomputes the count** from the severities, writes the corrected
value, and says so in `verdict_reason` — publishing a known-wrong number with only a warning
attached would leave the trap armed for any gate that reads the count without reading the
note.

The heartbeat is only honest if the output flows **unbuffered end to end**: the reviewer runs in a
**per-event-flushed streaming mode** (the adapters pass `--json` / `--output-format stream-json
--include-partial-messages`), the supervisor reads it in raw chunks (`os.read`) and flushes each
chunk to the display log as it arrives — so the heartbeat ticks per chunk, not only on newline. Without the CLI's streaming mode, output block-buffers into the pipe and
a working review can look silent — so the streaming flag is not optional on rung 1.

> **Claude adapter (Claude is running this review):** rung 2 — run the review as an independent
> sub-agent (Agent tool, subagent_type general-purpose). Rung 1 — launch Codex through the supervisor,
> **read-only-enforced** (`-s read-only -c approval_policy="never"` — the sandbox rejects writes and the
> approval policy forbids escalation) with `--json` for a live heartbeat, returning its **full
> transcript** (`--result-mode stream-transcript`):
> `<python> <skill-dir>/review_runner.py --idle 900 --deadline 1800 --cwd <dir> --display <cap> --findings <f> --result-mode stream-transcript --verdict-json <v> -- codex exec --json -s read-only -c approval_policy="never" --skip-git-repo-check -C <dir> "<review prompt>"`.
> Deliberately **no** `--output-schema` here — see the asymmetry note above; ask for the
> verdict object in the prompt instead and let the supervisor extract it.
> **Codex adapter (Codex is running this review):** rung 2 — use the native `/review`, or a fresh
> `codex exec --skip-git-repo-check -s read-only -c approval_policy="never" -C <dir> "<review prompt>"`
> reviewer over the diff. **Both flags, on the fallback rung too** — `-s read-only` bounds the
> shell and the approval policy bounds the built-in edit tool, and a reviewer that can write
> is not a review. Rung 1 — launch Claude through the supervisor, **read-only-enforced**
> (`--permission-mode plan` blocks every edit path), returning its **full transcript**:
> `<python> <skill-dir>/review_runner.py --idle 900 --deadline 1800 --cwd <dir> --display <cap> --findings <f> --result-mode stream-transcript --schema <skill-dir>/review-schema.json --verdict-json <v> -- claude -p "<review prompt>" --add-dir <dir> --permission-mode plan --json-schema ⟪schema_json⟫ --output-format stream-json --include-partial-messages --verbose`.
> **Controller vs author / probe:** rung 1 wants a reviewer whose **model differs from the diff's
> author**. If you — the runtime running this skill — already differ from the author, review directly:
> you are the cross-model reviewer. Otherwise launch the *other* runtime as above, but only when its
> CLI is on PATH (probe `command -v <cli>`; native Windows `where <cli>` / `Get-Command <cli>`) and
> Python 3 is present; else fall open to rung 2.

**Waiting for the supervisor to finish — never poll for its process.** The completion
signal is the **one-line JSON status** the supervisor prints to stdout — that is the
authoritative result. The display log's final `[review_runner] end status=… exit=…` line
marks the same moment, but it is written *before* the verdict is routed, so its `status=`
can still be revised: a reviewer that exits cleanly without a successful terminal event
logs `end status=ok` and is then reported as `error` on stdout. Treat the marker as
"the supervisor has stopped", and the JSON line as "here is what happened". In order of
preference:

1. **Run it in the foreground and read stdout.** Correct whenever the host's own
   command timeout comfortably exceeds `--deadline`. It often does not: a real
   cross-model review of a substantial diff runs 7–10 minutes, and some hosts cap a
   single command well below `--deadline`'s default of 1800s. A capped host will cut
   the *call* while the supervisor keeps running — which looks exactly like a hang.
2. **Run it with your runtime's background-execution facility**, which reports when the
   process exits. This is the reliable choice on a capped host, and it is what
   `plan-duel` already does for the same reason. Do not shorten `--deadline` to fit a
   cap instead: that kills legitimate long reviews rather than the hung ones.
3. **If you must poll, wait on the status artifact — never on a process pattern.**
   Redirect the status to a file, then poll until it holds *parseable JSON* (a shell
   redirect creates the file empty at launch, so existence alone is not completion).
   Bound the loop, and check the supervisor is still alive, since a supervisor that is
   signalled before it can write leaves the file empty forever.

**Never wait with a command-line pattern match** (`pgrep -f`, `pkill -f`, `ps | grep`).
Those match full command lines, and an agent harness typically runs each shell command as
`bash -c '<the entire command text>'` — so the pattern you are searching for is present in
the polling shell's *own* argv and matches itself. The loop can never observe its exit
condition, and the result is a multi-hour phantom "the review is hung" report about a
supervisor that finished cleanly. If you have no alternative, capture the PID at launch
and poll `kill -0 "$PID"`: a PID cannot self-match.

Before concluding a review is hung, check its **artifacts** — the status file, the display
log's `end` marker, the mtime of `--findings` — not whether a process appears to be alive.

## Step 3 — Read the diff for correctness

Go hunk by hunk. For each change, ask what could make it wrong — do not assume it
works because it was just written:

- **Correctness / logic:** wrong conditions, off-by-one, inverted checks, mishandled
  return values, incorrect API/contract usage.
- **Edge cases:** empty/null/large inputs, error and failure paths, concurrency and
  ordering, boundary values.
- **Contract & integration:** callers/consumers updated to match — open the unchanged
  files that reference the changed code to confirm this, since an un-updated caller
  lives outside the diff; interface, schema, or serialization changes are
  backward-safe; no dangling references after moves.
- **Security:** untrusted input handling, authz/authn, injection, secret exposure.
- **Tests:** does new behavior have tests that would actually fail if the behavior
  regressed? Are the assertions anchored, not silent?
- **Consistency:** matches surrounding conventions and patterns.

Prefer confirmed, reproducible findings over speculation; when a concern is
uncertain, say so and state what would confirm it. **Where the change ships with tests you
can run, run the one that would confirm a finding before filing it, and say which findings
were confirmed that way.** Stating a failure scenario and demonstrating one are different
claims, and the report cannot be told apart from the outside.

## Step 4 — Report findings (no edits)

Emit a findings list, most-severe first. For each finding:

- **Severity**, by this rubric (a phase gate keys on it, so label accurately):
  - **blocker** — wrong in a way that breaks correctness, security, or data integrity; must not merge.
  - **major** — a real correctness or robustness defect that should be fixed before merge, but not catastrophic.
  - **minor** — a small correctness or clarity issue that is safe to defer.
  - **nit** — style or preference; no correctness impact.
- Location (`file:line`).
- What is wrong and the concrete failure it causes (inputs → wrong result).
- A suggested direction — not an applied edit.

If nothing substantive is found, say so plainly rather than inventing nits. Do not
modify the working tree under any circumstance; the author or the phase gate applies
fixes.

**Where the review is dispatched to another runtime, the reviewer also returns this list
as a JSON object** matching `review-schema.json`: `findings` (an array of
`file` / `line` / `severity` / `summary` / `failure_scenario`), `overall`, and
`blocking_count` — the number of `blocker` plus `major` findings, which is what a phase
gate acts on. The prose above is what the object's fields carry, not a second report:
write the reasoning, and the same content lands in the structured fields. When the
reviewer runs in-harness (rung 2) there is no flag to enforce the shape, so ask for the
object in the prompt and fall back to reading the prose list if it does not arrive.

## Scope note

The single, bounded **rung-1 cross-runtime pass** above is a normal part of this skill and runs
by default when another runtime is available. What stays **separate and user-triggered** is a
*heavier* review — a multi-pass, multi-agent, or cloud-based audit (e.g. an "ultra" / deep
review): **recommend** that when the change warrants deeper scrutiny, but do not launch it
automatically from this skill.
