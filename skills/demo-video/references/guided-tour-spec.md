# Guided-Tour Playwright Spec Pattern

A demo tour is different from a test: it optimizes for a **human watching**, not for
speed. Slow every step down, pause long enough to read, and make the element in focus
obvious. Below is the shape to follow — adapt selectors and steps to the actual feature.

## Principles

- **One clear action per step.** Navigate, or click, or type — not three at once.
- **Pause to read.** After each meaningful state change, wait long enough (e.g. 1.5–2.5s)
  for a viewer to absorb what happened. These pauses also give each subtitle a stable
  time window.
- **Slow motion.** Run with slow-motion enabled so pointer moves and transitions are
  legible.
- **Highlight the focus.** Scroll the target into view and visually emphasize it (border,
  overlay, or the framework's highlight helper) before acting.
- **Stable anchors.** Drive by role/label/test-id/visible text, not brittle CSS chains —
  a tour that desyncs from the UI is worse than none.
- **Deterministic data.** Use a seeded/known state so the tour shows the same thing every
  run.

## Shape (pseudocode — adapt to the project's Playwright setup)

```
// Launch with slow motion and video recording enabled via the project's config
// (e.g. use: { launchOptions: { slowMo: 600 }, video: 'on' }).

async function step(page, caption, action) {
  // record caption + start time for subtitle derivation (see subtitles.md)
  await action(page);
  await page.waitForTimeout(PAUSE_MS); // long enough to read
}

test('guided tour', async ({ page }) => {
  await step(page, 'Open the settings page', async p => {
    await p.goto('/settings');
    await p.getByRole('heading', { name: 'Settings' }).waitFor();
  });

  await step(page, 'Toggle dark mode', async p => {
    const toggle = p.getByRole('switch', { name: /dark mode/i });
    await toggle.scrollIntoViewIfNeeded();
    await toggle.click();
  });

  await step(page, 'The theme applies immediately', async p => {
    await p.locator('html[data-theme="dark"]').waitFor();
  });
});
```

## Recording

Enable video in the Playwright config (`video: 'on'` or `'retain-on-failure'` inverted to
always-on for the tour) and let the run write to the gitignored output directory. Keep the
per-step captions and their start/duration — `subtitles.md` turns them into a timed
subtitle track.

## Fallback

If there is no Playwright/frontend setup to drive, do not install one. The tour cannot be
recorded — not merely skipped, because the tool that would capture the frames is the one
missing. **So say where the stills come from rather than writing "capture screenshots",
which names no one to capture them:** build the storyboard from images the user already
has, or ask them to take one per step against the step list above. Pair that with the
narration script; storyboard + script is the Degraded deliverable, and the report says
which half is missing and why.
