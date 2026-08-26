# Anchored Assertions & Silent-Failure Patterns

A UI check is only useful if it **fails when the UI is wrong**. Many common checks
pass even when the page is broken — they are *silent failures*. Anchor every
assertion to specific, content-bearing evidence so a regression cannot slip through.

## The core idea

- **Silent-pass check:** asserts something that is true even for a broken page.
- **Anchored check:** asserts specific expected content, so a blank, errored, or
  half-rendered page fails the check.

## Silent-failure patterns → anchored equivalents

| Silent (avoid) | Why it passes on a broken page | Anchored (use) |
|---|---|---|
| Assert the page returned HTTP 200 | An error UI still returns 200 | Assert the specific expected heading/text is visible |
| Assert a container/`<div>` exists | Empty and error states still render the container | Assert the container holds the expected item(s) or copy |
| Assert "no exception was thrown" | Rendering nothing throws nothing | Assert a known post-render element is present and visible |
| Assert element count `>= 0` | Always true | Assert the exact/known count for the seeded data |
| Assert text is "not empty" | A spinner or placeholder is non-empty | Assert the exact expected value or a stable substring |
| Wait a fixed time then continue | Masks slow/never renders | Wait *for* a specific element/state, with a timeout that fails |
| Screenshot then pass | An artifact existing proves nothing | View the screenshot and confirm expected content, then pass |

## Rules of thumb

1. **Assert presence of expected content, not absence of errors.** "The dashboard
   shows 3 rows for the seeded account" beats "no error was thrown".
2. **Wait for a state, never for a duration.** Anchor waits to a selector, text, or
   network-idle condition that fails loudly on timeout.
3. **Pin to stable anchors.** Prefer roles, labels, test-ids, or user-visible text
   over brittle nth-child/CSS chains.
4. **One visible, load-bearing assertion per checkpoint.** If the page rendered
   wrong, at least one anchored assertion must go red.
5. **Distinguish empty from broken.** Assert the difference between an intentional
   empty state ("No items yet") and a failed render (blank / error).
6. **Treat the screenshot as evidence to read.** Capture it, then confirm the
   anchored content is actually visible in it before declaring success.

## Applying these

When writing or reviewing the verification flow, scan each step and ask: *"Would
this step still pass if the page rendered nothing, an error, or a spinner?"* If yes,
re-anchor it to specific expected content.
