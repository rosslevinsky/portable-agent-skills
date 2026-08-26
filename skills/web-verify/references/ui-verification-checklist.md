# Manual UI-Verification Checklist

Use this checklist in two situations:

1. As the **fallback** when the repo has no Playwright/frontend automation to drive
   (web-verify never installs one).
2. As the **human review** pass over screenshots a runtime could not view itself.

Work through each item for every route/flow under verification. Record the outcome
(pass / fail / not-applicable) — an unchecked item is not a pass.

## Render integrity

- [ ] The page reaches a fully-rendered state (no perpetual spinner, no blank
      white screen, no skeleton left in place).
- [ ] The expected primary content is visibly present (specific headings, data,
      or copy — not just an empty container).
- [ ] No error boundary, stack trace, 404/500 page, or "something went wrong"
      state is shown.
- [ ] No obvious layout breakage: no overlapping elements, off-screen content,
      collapsed containers, or unstyled (flash-of-unstyled) content.
- [ ] Images, icons, and fonts load (no broken-image glyphs, no fallback system
      font where a brand font is expected).

## Behavior

- [ ] The primary interaction for the flow works end-to-end (submit, navigate,
      open, filter — whatever the change targets).
- [ ] State changes are reflected in the UI (e.g. a created item appears, a toggle
      persists, a count updates).
- [ ] Validation / error messaging appears for invalid input and clears on valid
      input.
- [ ] Navigation away and back preserves or correctly resets state as intended.

## Responsiveness

- [ ] Desktop viewport (e.g. 1280×720) renders correctly.
- [ ] Mobile viewport (e.g. 390×844) renders correctly — nav collapses as designed,
      no horizontal scroll, tap targets usable.

## Accessibility smoke

- [ ] Keyboard focus is visible and moves in a sensible order through interactive
      elements.
- [ ] Interactive controls have accessible names (labels/aria) — not icon-only with
      no text alternative.
- [ ] Text has adequate contrast against its background in the captured state.

## Console / network

- [ ] No uncaught errors in the browser console for the flow.
- [ ] No failed requests (4xx/5xx) for resources the flow depends on.

## Recording the result

For each route/flow, note: what was checked, the viewport(s), pass/fail per section,
and (if screenshots exist) the artifact path or CI URL. A flow is "verified" only
when every applicable item passed.
