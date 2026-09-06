# Phase-Worker Contract

The brief for a **worker** — a fresh sub-agent that executes exactly one phase of a v2 plan
with a clean context — and the result it returns. `../SKILL.md` is the procedure; this file
is only the hand-off, so nothing here restates a step.

## Inputs

Three required on-disk paths carry the **work**, and nothing from the orchestrator's
conversation does. (The **procedure** is `../SKILL.md`, which you read alongside them.)

1. `plan.md` — goal, success criteria, constraints, non-goals.
2. `execution.md` — the phase checkbox list. **Read-only for you.**
3. Your own phase document — its Work, Tests, Verification commands and Gate boxes. Its
   **Evidence record you report, and do not write**; see "What you do".

A fourth is passed: an append-only **progress file**. Add one line per meaningful step,
prefixed `[+MM:SS]` from your start. Nothing reads it for decisions; if the write fails,
continue.

Everything durable is on disk. If you feel you are missing context, you are missing a file
to read, not a memory.

## What you do

Run the `SKILL.md` transitions marked **worker-safe**, ticking every box in your own
document as you go — Work, Tests, and the Gate boxes you can establish — each the moment it
is true, never batched at the end. Those boxes are what a resumed run reads.

**Check first whether this is a re-dispatch.** If your phase document carries a "Review
findings to address" block and its Work boxes are already ticked, run a **scoped
address-findings pass**: fix only those findings, re-run only the tests scoped to what you
touched (and `web-verify` — or its manual checklist where unavailable — only if the fix
changed rendered UI), report the refreshed evidence in your result, and return. Do not
re-run the phase.

Leave every change **uncommitted**. Never write `execution.md`, never run the independent
review, never commit: those are orchestrator-only. Where the **tracker** is what diverges
from reality, say so in `deviations` rather than correcting it — the orchestrator applies
that correction when it takes your result.

**The Evidence record is orchestrator-only too: you report it, it writes it.** The `DONE`
fields below are that report, field for field — `summary` is the outcome, `changed_surface`
the changed surface, `verification` the commands and what they returned, `deviations` the
doc-vs-reality corrections. Writing the record yourself puts two authors on one section, and
the orchestrator's copy lands second regardless; it also has to add what you cannot know —
the independent review, which runs after you return.

**Your result comes after the work, never during it.** A command that outruns one call is
waited on as Satisfy's "Waiting for a long command" says — the runtime's background facility
where it reports the exit, else a marker the command writes carrying its exit status, never
a process-name match — and `DONE` is not returned before that exit is reported. The reported
exit, or the marker's `rc=`, is the work's outcome; a PID captured at launch tells you only
that the work is alive, and the poll's own exit tells you nothing about it.

## The result

Your final message **is** the result — data for the orchestrator, not prose for a human: one
JSON object matching `phase-worker-schema.json` (beside this file), with no surrounding text
and no fence. The two shapes are mutually exclusive and the schema enforces that rather than
describing it — a `DONE` cannot carry a `question`, a `BLOCKED` cannot claim a `verification`.

**DONE** — the work is complete and left uncommitted:

```json
{
  "outcome": {
    "result": "DONE",
    "summary": "<one or two sentences on what changed>",
    "changed_surface": "<files / sections touched>",
    "verification": "<the exact commands run and their outcomes>",
    "deviations": "<doc-vs-reality corrections, or \"none\">"
  }
}
```

**BLOCKED** — a decision could not be resolved from the three inputs:

```json
{
  "outcome": {
    "result": "BLOCKED",
    "changed_surface": "<files this attempt touched and did not revert, or \"none\">",
    "question": "<the single decision that cannot be resolved from the three inputs>",
    "options": "<the viable choices, briefly>",
    "recommendation": "<the worker's best option and why>"
  }
}
```

Return `BLOCKED` rather than guessing whenever a decision is genuinely under-specified, and
never half-finish. **Block *before* making any edit that depends on the unresolved
decision**, so the re-dispatched worker starts genuinely fresh.

On `DONE`, `changed_surface` records what you did; on `BLOCKED` it is a **cleanup handle** —
revert your own edits and report `"none"` where you can, since you know what you touched and
the orchestrator does not, and otherwise list the paths so it reverts exactly those instead
of resetting the tree. **Whatever you revert, untick the boxes it had ticked** — Work, Test
or Gate. A box that outlives the work behind it is worse than no box at all: the
fresh worker reads it as done and skips work that no longer exists.

The `outcome` wrapper is load-bearing, not decoration: both runtimes reject a
structured-output schema whose root is a union, and accept one nested a level down.

## On-disk state is authoritative, not the return formatting

The ticked boxes and the working tree are the phase's real outcome; this object only reports
it. So the orchestrator **parses leniently** — the shape is guaranteed only for a worker
spawned as a CLI subprocess under a structured-output flag, and nothing enforces it
in-harness — reading the object where it is there, the fields out of the message where it is
not, and never failing a completed phase over its result *formatting*. And it **checks the
evidence, not the shape**: a `DONE` whose `summary`, `changed_surface` or `verification` says
nothing substantive is treated as `BLOCKED` and re-dispatched rather than committed, and so is
a `BLOCKED` whose `question` **or `recommendation`** says nothing substantive — an autonomous
orchestrator acts on that recommendation, so an empty one is not a decision it can take. The
schema guarantees only that the fields are present and non-empty, only on the CLI path;
whether they are *meaningful* stays the orchestrator's judgement in both modes.
