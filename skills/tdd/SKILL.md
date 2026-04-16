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

Before writing any code:

### 1a — Read the feature spec

The argument passed to `/tdd` is the feature to implement. Parse it to identify:
- What behaviour needs to exist
- What layer it lives in (API, business logic, UI component, hook, utility)
- What the expected inputs and outputs are

### 1b — Discover the project structure

Do not assume paths. Explore the actual codebase:

- Find the test runner config: look for `pytest.ini`, `pyproject.toml` (pytest section),
  `vitest.config.*`, `jest.config.*`
- Find existing test directories: search for directories matching `**/tests/` and
  `**/__tests__/`, and files matching `**/*.test.*` and `**/*.spec.*`
- Find the nearest existing code in the relevant layer and read it
- Find existing tests for similar features — identify patterns to follow (naming,
  fixture usage, assertion style)

### 1c — Identify the test layer

Based on what you find, determine:

| Feature type | Typical test layer | Notes |
|---|---|---|
| Pure function / utility | Unit test | Fastest, no I/O |
| API endpoint / DB interaction | Integration test | Needs DB fixture |
| React hook | Frontend unit (renderHook) | Mock network with MSW or similar |
| React component | Frontend unit | Render + assert |
| Full user flow | E2E | Manual or Playwright |

Confirm which test runner and commands apply to this project before proceeding.

### 1d — Find reusable test utilities

Before writing anything, check for:
- Backend: conftest.py files with fixtures (sessions, clients, factories, users)
- Frontend: test utility wrappers, MSW handlers, mock servers

Note what's available — do not reinvent what already exists.

---

## Phase 2 — Red (write failing tests)

**Rules:**
- Write tests before any implementation
- Tests must import the not-yet-existing module/function/component — this is intentional
- Run tests; confirm failure is "module not found" or "cannot find module" or
  "attribute does not exist" — not a logic error or syntax error in the test itself
- If tests pass immediately, the feature already exists — stop and report that to the user

Write the tests in the appropriate location following the project's existing conventions
for test file placement and naming.

**After writing tests — run them and confirm RED:**

Show the failure output. If it says `ImportError`, `ModuleNotFoundError`, or
`Cannot find module`, that's the expected red state. Proceed to Phase 3.

If it fails for any other reason (syntax error, wrong assertion, etc.), fix the test
first — the test must be correct before the implementation begins.

---

## Phase 3 — Green (minimum implementation)

Write the **minimum** code to make the failing tests pass:

- No extra features, no future-proofing
- No refactoring of adjacent code
- Follow existing patterns in the same file/module (naming, error handling, async style)

**After implementing — run tests and confirm GREEN:**

All tests in the new file must pass. No previously passing tests may regress.
Run the full test suite (or at minimum the affected layer) to confirm no regressions.

If tests still fail, fix the implementation — do not modify the tests to make them pass.

---

## Phase 4 — Refactor (optional)

If the implementation has obvious duplication, poor naming, or violates project conventions:

- Clean it up
- Run tests again to confirm they still pass
- Do not add new behaviour during refactor

If nothing needs cleanup, skip this phase.

---

## Phase 5 — Report

After tests are green, summarize:

```
## TDD Summary: <feature name>

**Tests written:** <count> tests in <file(s)>
**Test layer:** <unit / integration / E2E>
**Test command:** <exact command to run these tests>
**All passing:** yes / no

**What's covered:**
- <behaviour 1>
- <behaviour 2>

**Known gaps (not covered by these tests):**
- <gap 1 if any>
```

---

## Important constraints

- Do not skip running tests between phases — the red→green transition is the whole point
- Do not add `eslint-disable`, `# noqa`, or test-specific conditionals in production code
  to make tests pass
- Do not modify tests to make them pass — fix the implementation instead
- Use existing test utilities and fixtures — do not duplicate infrastructure that already exists
- Follow the project's `CLAUDE.md` / `AGENTS.md` conventions (whichever exists) throughout
