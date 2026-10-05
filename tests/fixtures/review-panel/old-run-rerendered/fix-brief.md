# Fix brief

For an agent fixing the defects this review established or left open. `report.md` has the same defects with all their evidence; this keeps what is needed to fix them.

## Setup

- The review read a tree that is not a repository. Line numbers below refer to that tree; read each file there yourself.
- Build, as the capability probe ran it: yes, exit 0.
  ```
  python3 -m compileall -q .
  ```
- Tests, as the capability probe ran it: yes, exit 1.
  ```
  python3 -m unittest discover
  ```
- What the probe reported: Compiles; the suite runs and its one test fails to import.

## How to use this

- A defect (`D1`, `D2`, …) is one mistake. Its sites (`S1`, `S2`, …) are the places it has to be fixed: code, documents, tests. A defect is fixed when every one of its sites is.
- `established`: the review's check upheld the site, by running code or by reading it.
- `unresolved`: nothing settled the site. Confirm it before changing anything; the site says what stopped the review.
- Write the test that should fail first before the fix, and see it fail.
- A heading, *What goes wrong*, *Fix* and a site's note are one agent's reading of the run, and nothing checked them. Where a fix names an order, follow it.
- Established defects come first, then unresolved; within each, most severe first, then fewest sites. Refuted sites and defects are left out: there is nothing to fix at them.

## D2. Every total built on helper comes out twice as large as the contract says.

- Severity: blocker · Sites: 1
- **What goes wrong.** helper multiplies by two where the contract adds two, so every total built on it is too large.
- **Fix.** Return y + 2.

### S2 · `engine/core.py:5-6` · established · blocker · fix 1 line

- Test that should fail first: tests/test\_engine.py: call helper(3) and assert it returns 5.
- Reproduction, run, exit 0:
  ```
  python3 -c 'from engine.core import helper; print(helper(3))'
  ```
  - What it shows: Shows helper(3) returning 6.
  - Output:
    ```
    6
    ```

## D4. The tool exits 0 even when it did nothing.

- Severity: major · Sites: 1
- **What goes wrong.** main returns 0 whether or not it did anything, so a caller cannot see a failure.
- **Fix.** Return non-zero when no work was done.

### S4 · `run.py:1-2` · established · major · fix medium

- Test that should fail first: tests/test\_engine.py: call helper(3) and assert it returns 5.
- Reproduction, run, exit 0:
  ```
  python3 run.py
  ```
  - What it shows: Shows the tool exiting 0 having printed nothing.

## D7. The guide describes the engine without the helper's doubling, so a reader trusts it.

- Severity: minor · Sites: 1
- **What goes wrong.** The guide says nothing of what helper returns, so a reader trusts the wrong total.
- **Fix.** State what helper returns.

### S7 · `docs/guide.md:3` · established · minor · fix 1 line

- Test that should fail first: tests/test\_engine.py: call helper(3) and assert it returns 5.

## D1. A string passed to core raises instead of being refused with a message.

- Severity: major · Sites: 1 · Related: D2
- **What goes wrong.** core adds to x before anything checks x is a number, so a string raises.
- **Fix.** Refuse a non-number with a message before the addition.

### S1 · `engine/core.py:1-2` · unresolved · major · fix small

- Not settled (Needs a file outside the reviewed scope): Read the files it needs and confirm the claim before changing anything.
  - What stopped the check: Whether a string can reach core is not visible here.

## D3. util returns a constant string, so callers cannot tell two calls apart.

- Severity: minor · Sites: 1
- **What goes wrong.** util returns one constant whatever it is asked, so no caller can tell two calls apart.
- **Fix.** Return a value derived from the call.

### S3 · `engine/util.py:1-2` · unresolved · minor · fix small

- Not settled (Needs a run): Run the reproduction, or write the test, and confirm the failure before changing anything.
  - What stopped the check: Could not run it here.

## Tests to write

Inputs the code handles differently that no test in scope constructs. **None of these says the code is wrong.** Each is a test to write, at the lines that decide the input, under the test class that owes it.

## D5. A negative multiplier is never exercised.

- Test to write · Sites: 1

### S5 · `engine/core.py:5-6` · established · owed by tests/test\_engine.py

- Not exercised here: A negative multiplier is never exercised.
- No test constructs: helper distinguishes y below zero and no test constructs one.
- Proposed: Add a case in tests/test\_engine.py with y = -1.
- Test that should fail first: tests/test\_engine.py: call helper(3) and assert it returns 5.
- What the check found: No test constructs a negative y.
