---
name: cyw
description: "Multi-pass review of recent work to catch errors, gaps, and improvements. Use when the user invokes /cyw, or asks to 'check your work', 'review what you just did', 'is this correct/complete?', 'what's missing?', 'what needs fixing?'. Runs a structured 3-phase loop: critical review, fix, verify. Repeats up to 3 times, stopping early when no issues remain. Supports an embedded single-pass mode (argument: single-pass) for use inside phase gates."
---

# Check Your Work

Run a structured review loop on the work just completed in this conversation.

**Embedded single-pass mode:** when invoked from a phase gate (e.g. `plan-run`'s Gate
transition) or with the argument `single-pass`, run exactly **one** pass — Phase 1 → 2 → 3,
then stop. In this mode the loop-control rules do not apply: ignore only the zero-issue
stop/continue rules inside Phase 1 step 4 (still produce the numbered issue list) and
the "always continue" rule (Phase 3 step 6), and skip Phase 3's full test run (step 2) —
the calling gate owns the suite. But if this pass's Fix changes production code, re-run
the **scoped** test(s) for it before returning, since the gate's test pass ran *before*
your Fix. Everything else below applies unchanged. Standalone invocations use the full
loop.

**Loop control:** Repeat Phases 1–3 up to **3 passes**. At the start of each pass, print the header `### Pass N of 3`. Stop early (before Phase 2/3) when Phase 1 finds zero issues **on pass 2 or later** — a clean pass 1 still requires a confirming second review. After pass 3, stop regardless of remaining issues.

## Phase 1: Critical Review

1. Restate the original task in one sentence to anchor the review scope.
2. Re-read all files that were created or modified.
3. Check each change against:
   - **Correctness** — Does the logic do what was intended? Any bugs, wrong conditions, incorrect API usage?
   - **Completeness** — All requirements addressed? Files missed? Edge cases skipped?
   - **Consistency** — Matches existing naming conventions, patterns, and architecture?
   - **Code quality** — Clean, well-structured, maintainable? Any code smells or anti-patterns?
   - **Integration** — Correct imports? Are callers/consumers updated?
   - **Tests** — Existing tests still pass? New tests needed?
4. Produce a numbered list of issues found. **Each issue must name the concrete harm it
   causes, and the form that takes depends on what was reviewed.** For code: the input or
   condition that produces the wrong result. For prose, skills or docs: the reader who is
   misled and the wrong thing they do next. An observation you cannot put in either form
   is recorded as an observation and **not fixed in Phase 2** — a review that changes
   things it cannot show are wrong is how a clean pass makes work worse.
   - If **zero issues** and this is **pass 2 or later**: print "No issues found — work is complete." and **stop the loop**.
   - If **zero issues** on pass 1: still continue to pass 2 (a second clean review is required to confirm).

## Phase 2: Fix

Address every issue from Phase 1, stating which issue each fix resolves.

## Phase 3: Verify

1. Re-read each modified file.
2. If the work involved code changes, run the relevant test command to confirm nothing is broken.
3. Confirm every Phase 1 issue is resolved.
4. Confirm the original task is fully met.
5. Write a brief summary: issues found this pass, issues fixed, any remaining concerns.
6. **Always continue to the next pass** unless this was pass 3. Do NOT stop here even if all issues are resolved — "all issues fixed" is not the same as "Phase 1 found zero issues". The only early-stop is after Phase 1 of pass 2 or later, when zero issues are found. If this was pass 3 and issues remain, note them as **unresolved concerns** and stop.
