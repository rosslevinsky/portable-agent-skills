---
name: web-verify
description: >
  Screenshot-first visual and behavioral verification of a running web UI. Use
  after building or changing UI to confirm it actually renders and behaves
  correctly — not just that unit tests pass. Detects an existing Playwright /
  frontend setup and drives it to capture screenshots (and, optionally,
  ffmpeg-extracted video frames), then inspects the images against anchored
  assertions. Never bootstraps Playwright into a repo that lacks it — it degrades
  to a manual UI-verification checklist. Use when the user invokes /web-verify, or
  says "verify the UI", "check it renders", "visually verify", "screenshot the app
  and confirm it works".
---

# Web Verify — Visual & Behavioral UI Verification

_Classification: Degraded — assertions and captures run in any runtime, but a runtime that cannot view images loses the visual-inspection step and falls back to assertion-only plus a manual screenshot review. Also requires a pre-existing Playwright/frontend setup; without one it degrades to a manual UI-verification checklist._

## Overview

Confirm a web UI is genuinely correct by exercising it and **looking at the
result**, not by trusting that a test or artifact exists.

**The load-bearing rule:** a green run or a saved screenshot/video is *evidence to
inspect*, never a conclusion. Visual verification is satisfied only when the
expected, content-bearing UI is confirmed present in an actual captured image (or,
where images can't be viewed, by anchored assertions plus a human screenshot
review). "A video/artifact was produced" never counts as verified on its own.

This skill **never installs Playwright or a frontend toolchain.** It uses what the
repo already has, and degrades cleanly when the tooling is absent.

## Step 1 — Detect prerequisites (never bootstrap)

Check, without installing anything, for:

- **Playwright**: a `@playwright/test` dependency in `package.json`, a
  `playwright.config.*` file, or an existing e2e/test directory that uses it.
- **A running/runnable frontend**: a framework dependency and a dev/build script
  in `package.json` (or the project config), or an already-running dev server.

Decision:

- **Both present** → proceed to Step 2 and drive the existing setup.
- **Playwright present but the frontend is not runnable** → do **not** bootstrap one. If a
  dev or `webServer` script exists, prefer pointing Playwright's `webServer` at it so
  Playwright manages its lifecycle; if you start it manually instead, launch it
  detached/in the background and tear it down once verification completes. Settle all of
  this *before* Step 3, which captures frames and needs the app already serving — the
  teardown itself belongs at the end, not here. If no such script exists — **or** the app still fails to start — switch to
  the manual UI-verification checklist in `references/ui-verification-checklist.md`, record
  **Degraded (manual)** mode, report what a human must click through, and skip Steps 3–5.
- **Playwright absent** → do **not** add it. Switch to the manual
  UI-verification checklist in `references/ui-verification-checklist.md`, record
  that verification ran in **Degraded (manual)** mode, and report what a human must
  click through. Skip Steps 3–5.

If it is unclear which flow or route to verify, ask the user. If operating
autonomously (no user available), verify the primary route and any route touched
by the change under review, and note the assumption.

## Step 2 — Define anchored assertions first

Before capturing anything, decide what "correct" looks like as **anchored
assertions** — checks tied to specific, content-bearing elements or text that fail
loudly when the UI is wrong. Avoid silent-pass checks (e.g. asserting only that a
container exists, or that the page returned 200). See
`references/anchored-assertions.md` for the silent-failure patterns to avoid and
the anchored equivalents to use.

## Step 3 — Capture (screenshots first-class)

Drive the existing Playwright setup through the target flow and capture a
**screenshot at every checkpoint** — screenshots are the primary, portable
artifact. Prefer full-page screenshots at each asserted state.

Optionally, for richer change-point evidence, record a video of the flow and
extract frames at visual change points with ffmpeg — this is a heavier, optional
layer. Use the template in `references/extract-frames.sh`; if ffmpeg is not
installed, skip frame extraction and rely on screenshots.

## Step 4 — Inspect the captures (do not skip)

Open and **view each screenshot / frame** and confirm the anchored content from
Step 2 is visually present and correct — right text, right layout, no error state,
no blank/placeholder render.

If the runtime cannot view images, fall back to: (a) keep the anchored assertions
as the machine-checked evidence, (b) emit the screenshots for a human to review,
and (c) mark the *visual* dimension **unverified (Degraded)** in the report rather
than claiming it passed.

## Step 5 — "N× green" release gate

For a UI-release gate, require **N consecutive fully-green runs**, default **N = 3**
(configurable — a quick spot-check may use 1; state the value used). All N must pass
assertions *and* visual inspection.

**In a runtime that cannot view images this gate cannot be passed here, and saying so is
the point.** Step 4 has already marked the visual dimension unverified; a gate demanding
visual inspection is then a criterion nobody in the loop can meet, and the failure mode is
that it gets quietly read as satisfied by the assertions alone. So report it as **N×
assertion-green plus a human screenshot review still outstanding — not yet passed**, and
name who has to do the looking. It converts to passed when that review comes back, not
when the run count reaches N.

Run the gate across the **mandatory matrix**:

- The browsers/viewports declared in the project's Playwright config, if any.
- If none are declared, default to **Chromium** at one desktop viewport
  (1280×720) and one mobile viewport (390×844).

State which browsers/viewports were actually exercised in the report.

## Step 6 — Artifact retention

All captures are disposable and **must not be committed**. Ensure the repo
gitignores them **scoped to the output directories** — `test-results/`,
`playwright-report/`, any frames output directory, and trace zips — never a bare
repo-wide media glob like `*.webm`, which silently untracks committed video or
caption assets elsewhere in the repo. Keep only the latest passing run's captures
plus any failing-run artifacts locally. In any evidence record or as-built note, reference
artifacts **by path or CI URL only** — never inline or commit them.

## Reporting

Report, concisely: the flow verified, the browsers/viewports used, N and how many
green runs were achieved, the anchored assertions checked, whether visual
inspection was performed (or Degraded-manual), and the artifact paths/URLs.

## References

- `references/ui-verification-checklist.md` — the manual UI-verification checklist
  (also the full fallback when Playwright is absent).
- `references/anchored-assertions.md` — silent-failure patterns and their anchored,
  fail-loud equivalents.
- `references/extract-frames.sh` — a template ffmpeg change-point frame-extraction
  script (optional, heavier layer).
