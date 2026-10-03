---
name: tdd
description: >
  Test-driven development workflow. Enforces red/green/refactor discipline:
  write failing tests first, implement the minimum code to pass, then clean up.
  Use when the user invokes /tdd with a feature to implement (e.g., "/tdd implement
  the vocab export endpoint", "/tdd add useMyHook"). Never skips ahead to
  implementation before confirming tests fail.
---

# TDD Workflow

## Overview

Phases run in strict order. **Never proceed to the next phase without running
tests and confirming the expected outcome.**

1. **Understand** — read existing code, tests, and project structure before writing anything
2. **Red** — write failing test(s); confirm they fail for the right reason
3. **Green** — write the minimum implementation to make tests pass
4. **Refactor** — clean up; confirm tests still pass
5. **Report** — summarize what's covered and what's missing

---

## Phase 1 — Understand

Read the code and tests nearest the feature before writing anything. Work out what behavior
must exist, which layer it lives in (pure function, API/DB, hook, component, full flow), and
which runner and command test that layer in this project. Follow the nearest existing tests'
placement, naming, fixtures and assertion style, and reuse the project's test helpers
(conftest.py fixtures, MSW handlers, render wrappers) instead of adding new infrastructure.

---

## Phase 2 — Red (write failing tests)

**Rules:**
- Write tests before any implementation
- Tests must exercise behavior that does not exist yet — this is intentional
- Run tests; confirm the failure is caused by that absent behavior, not by a broken test
- If tests pass immediately, the feature already exists — stop and report that to the user

Write the tests in the appropriate location following the project's existing conventions
for test file placement and naming.

**After writing tests — run them and confirm RED:**

Show the failure output. A correct red takes either shape:

- **Something that does not exist yet** — the name, the attribute, or the *signature*
  you are calling has nothing behind it: `ImportError`, `ModuleNotFoundError`,
  `Cannot find module`, `AttributeError`, a `TypeError` naming an argument the function
  does not accept yet, or a `NotImplementedError` raised by a stub. These are the common
  shapes, not a closed set: what makes a red correct is that the *behavior* is absent,
  not which exception carries the news. Adding a parameter to an existing function reds
  as a `TypeError`, and that is a legitimate red — not a broken test to be repaired.
- **New behavior on an API that already exists** — a **failing assertion**: the call
  runs and returns the old answer. This is the usual red when extending existing code,
  and it is a valid red, not a broken test.

Either one means proceed to Phase 3. If it fails for any other reason (syntax error,
wrong import path, fixture or setup error), fix the test first — the test must be
correct before the implementation begins.

---

## Phase 3 — Green (minimum implementation)

Write the **minimum** code to make the failing tests pass:

- No extra features, no future-proofing
- No refactoring of adjacent code
- Follow existing patterns in the same file/module (naming, error handling, async style)

**After implementing — run tests and confirm GREEN:**

All tests in the new file must pass. No previously passing tests may regress.
Run the full test suite (or at minimum the affected layer) to confirm no regressions.
When this loop runs inside a plan that sets its own rule for which tests to run while
working, that rule wins.

If tests still fail, fix the implementation — do not modify the tests to make them pass.

---

## Phase 4 — Refactor (optional)

If the implementation has obvious duplication, poor naming, or violates project conventions:

- Clean it up
- Run tests again to confirm they still pass
- Do not add new behavior during refactor

If nothing needs cleanup, skip this phase.

---

## Phase 5 — Report

After tests are green, report what the new tests cover and what they leave out, which files
they live in, the exact command that runs them, and which tests you ran to check for
breakage and whether they passed.

---

## Important constraints

- Do not skip running tests between phases — the red→green transition is the whole point
- Do not add `eslint-disable`, `# noqa`, or test-specific conditionals in production code
  to make tests pass
- Do not modify tests to make them pass — fix the implementation instead
- Use existing test utilities and fixtures — do not duplicate infrastructure that already exists
- Follow the project's `CLAUDE.md` / `AGENTS.md` conventions (whichever exists) throughout
