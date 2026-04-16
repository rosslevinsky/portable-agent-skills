---
name: cyw
description: "Multi-pass review of recent work to catch errors, gaps, and improvements. Use when the user invokes /cyw, or asks to 'check your work', 'review what you just did', 'is this correct/complete?', 'what's missing?', 'what needs fixing?'. Runs a structured 3-phase loop: critical review, fix, verify. Repeats up to 3 times, stopping early when no issues remain."
---

# Check Your Work

Run a structured review loop on the work just completed in this conversation.

**Loop control:** Repeat Phases 1–3 up to **3 passes**. At the start of each pass, print the header `### Pass N of 3`. Stop early (before Phase 2/3) if Phase 1 finds zero issues. After pass 3, stop regardless of remaining issues.

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
4. Produce a numbered list of issues found.
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
