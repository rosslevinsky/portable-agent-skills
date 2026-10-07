---
name: xc
description: "Cross-check a document with a different model, settling every disagreement before anything changes. Runs a cyw pass, has another model review the document, decides each finding on its merits (accept, or reject with a reason the reviewer may answer once), applies what stands, and runs cyw again. Built for plans and phase documents; works on any document. Use when the user invokes /xc, or says 'cross-check this', 'get a second opinion', 'see what codex thinks', 'arbitrate with codex'. Argument: the file(s) to review; with none, the document this conversation just produced. For a code diff at a merge boundary, use diff-review instead."
---

# Cross-check

Get a second opinion on a document from a model that did not write it, settle every
disagreement on the evidence, and apply only what survives.

_Classification: Degraded — the reviewer has two rungs. Strongest is a **different model**, launched through `diff-review`'s bundled `review_runner.py`; next is a fresh reviewer in the same runtime. There is no third rung: an in-context reset is what `cyw` already did in Step 1, so where neither reviewer can be reached the skill reports that and stops rather than present a self-review as a second opinion._

_Progress: observable — rung 1's reviewer streams into the supervisor's append-only display log while it runs; rung 2 returns in one shot._

## Step 1 — Anchor and self-check

1. Name the target files. With no argument, use the document this conversation just
   produced; if that is ambiguous, ask (autonomously: the most recently modified document
   the conversation touched, and say so). Name too any context documents the caller names
   — a master plan behind its phase documents. Never edit a context document.
2. Write one sentence saying what the document is for and who acts on it. A reviewer
   without it judges the wrong thing.
3. If a `cyw` pass ran on these files earlier in this conversation and none of them has
   changed since, skip this pass and say so. Otherwise run the `cyw` skill in single-pass
   mode on the target. If `cyw` isn't installed or doesn't run, re-read the target for
   correctness, completeness and consistency and fix what you find. The reviewer's
   attention belongs on what you could not see.

## Step 2 — Get the review

**Pick the reviewer, strongest first.**

1. **A different model.** Either the other runtime's CLI, or this runtime's CLI on a
   backend the user names whose model is not yours; a named backend's `harness` picks
   which CLI launches. This needs the `diff-review` skill installed beside this skill,
   Python 3, and that CLI on PATH. If any is unavailable, or the supervisor reports a
   status other than `ok`, record why and take rung 2. A review with no parseable verdict
   object is still a review: read its findings as prose. Launch it with the command
   `diff-review` gives for launching that CLI, with this skill's reviewer prompt in place
   of that skill's, `diff-review`'s `review-schema.json` for the verdict where that command
   takes one,
   `--backend <name>` when a backend is named, and `--cwd` set to the repository holding
   the target (its own directory, outside one).
2. **A fresh reviewer in this runtime**, given the reviewer prompt and nothing from this
   conversation: a sub-agent, or where the host has none, a fresh launch of this runtime's
   CLI with `diff-review`'s read-only flags (with `diff-review` absent, that launch is
   unavailable). Never a native code-review command — it reviews the working tree's
   changes, not the document.
3. Neither → stop. Report that no independent reviewer was available, what was missing,
   and that the Step 1 pass is the only review this document has had.

**The reviewer prompt** carries exactly:
- the target paths and the one-sentence purpose, and any context documents' paths as
  read-only context: read them, and report where a target falls short of them;
- the lens. For a plan or phase document: does following it reach the goal; steps
  missing, misordered or unverifiable; success criteria a builder could tick while the
  goal is unmet; assumptions stated as fact; a cheaper route to the same result. For
  anything else: whether its reader can act on it correctly;
- the burden of proof: every finding names the reader or builder who goes wrong and what
  they do next. A concern without one is labeled an observation;
- report only, never edit a file;
- close with one JSON object: `findings` (each with `file`, `line`, `severity` —
  `blocker`, `major`, `minor` or `nit` — `summary`, and `failure_scenario` holding that
  consequence), `overall` (the verdict in prose) and `blocking_count` (blockers plus
  majors). It is the shape of `diff-review`'s `review-schema.json`, spelled here so a
  sub-agent needs no other skill.

Keep the supervisor's files outside the repository and delete them when done.

## Step 3 — Settle each finding

Decide every finding on the document and the code, never on who wrote what:

- **Accept** — the failure is real.
- **Reject** — with the concrete reason: where the document already handles it, or the
  fact that makes the premise false. Disagreeing is not a reason.
- **Partial** — the failure is real and the suggested fix is not; say what you will do
  instead.

Send every rejection and partial back once, in one fresh launch on the same rung and
backend, carrying each finding, your decision and your reason. Ask the reviewer to concede or
answer with evidence it has not yet given, and re-decide on what comes back. A finding
still disputed after that is an **open disagreement**: it is not applied, and it goes to
the user with each position in one line.

Apply a nit only when it costs nothing.

## Step 4 — Apply and re-check

Edit the target for every accepted finding, and for every partial one the send-back
settled; an open disagreement is never applied. Then run the `cyw` skill — the full loop,
not single-pass — over what changed, and tell the final `cyw` which findings are open so it
leaves them alone. If `cyw` isn't installed or doesn't run, do its full loop by hand:
review for correctness, completeness and consistency, fix, re-verify, and repeat until a
pass finds nothing — a clean first pass still gets a confirming second — at most three
passes.

## Step 5 — Report

- Which reviewer ran: the other CLI or this one, with the backend's name and model or on
  the CLI's own sign-in, and on this runtime's own CLI your own model beside it, stating
  their independence as unverified; or rung 2, and why rung 1 was not reached.
- A table: finding, severity, decision, reason, applied.
- Open disagreements for the user to decide. Autonomously: leave them unapplied and list
  them.
- What the final `cyw` pass changed.
