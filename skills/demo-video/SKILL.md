---
name: demo-video
description: >
  On-demand guided-tour walkthrough video of a built feature — the last skill in the
  v2 workflow. Drives an existing Playwright / frontend setup through the primary flow
  slowly with pauses, records video, and derives timed subtitles from as-built.md and
  test-step timing; optional background music. Never bootstraps Playwright and never
  claims success from "a video exists" alone. Without ffmpeg it still ships Playwright's
  native video plus a sidecar subtitle file; it degrades to ordered screenshots only when
  no video can be recorded at all. Narration audio is out of scope — it writes subtitles,
  not speech. Use when the user invokes /demo-video, or says "make a demo video",
  "record a walkthrough", "generate a guided tour of the feature".
---

# Demo Video — Guided-Tour Walkthrough

_Classification: Degraded — the guided-tour spec and its subtitle/narration script are produced in any runtime; recording the walkthrough video needs a Playwright/frontend setup, and muxing captions, adding music, and extracting change-point frames need ffmpeg. Without ffmpeg the skill still delivers Playwright's native `.webm` plus a sidecar subtitle file; without Playwright video it degrades to ordered screenshots + the narration script text. It is only Runtime-limited if no walkthrough artifact can be produced at all._

## Overview

Produce a guided-tour walkthrough video of a feature that has already been built: a
slow, paced Playwright tour of the primary flow, recorded to video, with subtitles
derived from `as-built.md` and the tour's step timing, and optional background music.

This is a standalone, **on-demand** skill and the last piece of the v2 workflow. It
reuses the same video infrastructure as the `web-verify` skill (if that skill is
unavailable, drive the existing Playwright setup directly) and uses `as-built.md` as the
narration source.

Two rules:

- **Never bootstrap tooling.** It uses the repo's existing Playwright/frontend setup and
  degrades when it is absent — it does not install anything.
- **A produced file is not success.** The tour must actually show the feature working;
  when video can't be rendered, the honest fallback is ordered screenshots + the
  narration script, not a claim that "a video exists."

TTS/voice narration is **out of scope**: this skill produces subtitles, not audio.

## Step 1 — Check prerequisites (never bootstrap)

Without installing anything, check for:

- An existing **Playwright / frontend** setup (as the `web-verify` skill detects). If it
  is unavailable there is no driver to capture frames with, so the recorded tour is not
  merely skipped — nothing can produce one. Degrade to a **narration script**, plus a
  storyboard built from stills the user already has or captures by hand, and say which
  half is missing and why. Do not describe the fallback as "ordered screenshots" without
  saying where they come from: the tool that would have taken them is the one absent.
- **ffmpeg**, for frame extraction, re-encoding, subtitle muxing, and music. If absent,
  still keep Playwright's **natively recorded `.webm`** and ship it with a sidecar
  subtitle file (`.vtt`/`.srt`) — only the muxed-in captions, music, and frame
  extraction are skipped (Degraded). Fall back to ordered screenshots + a
  subtitle/script file only when Playwright video recording itself is unavailable.
- **`as-built.md`** as the narration source. If it is missing, ask the user for a short
  tour outline; if operating autonomously, derive the outline from the plan's success
  criteria and note the assumption.

## Step 2 — Author the guided-tour spec

Write a Playwright guided-tour spec that walks the primary flow **slowly**: one clear
action per step, an explicit wait/pause long enough to read at each step, and a highlight
of the element in focus. See `references/guided-tour-spec.md` for the pattern (slow-motion
config, per-step pauses, stable anchors).

## Step 3 — Record and derive subtitles

Record the tour to video. Derive one **caption per step** from `as-built.md` (what the
step demonstrates) timed to that step's start and duration, and write a standard subtitle
file (`.vtt` or `.srt`). See `references/subtitles.md` for the timing derivation and the
optional-music note.

## Step 4 — Render (optional, heavier layer)

If ffmpeg is available, optionally extract change-point frames, encode/re-encode the
final video, mux in the subtitles, and add optional background music. If ffmpeg is
absent, deliver Playwright's natively recorded `.webm` alongside the sidecar subtitle
file from Step 3 (captions ride as a separate file rather than muxed in); fall back to
the screenshots + subtitle/script text only when no video was recorded at all.

## Step 5 — Artifact retention

All output — video, extracted frames, subtitles, music — is heavy and disposable. Ensure
the repo gitignores it, scoped to the output directory (`demo-video-output/`, including any
frames subdirectory under it) — not bare repo-wide globs like `*.webm`/`*.vtt`/`*.srt`,
which can silently untrack committed media or caption assets elsewhere in the repo.
Reference the result by path or CI URL only; never commit it.

## Reporting

Report: the flow(s) toured, whether a rendered video was produced or the skill degraded
to screenshots + script, the subtitle/script path, and the artifact paths/URLs.

## References

- `references/guided-tour-spec.md` — the guided-tour Playwright spec pattern (slow-motion,
  per-step pauses, highlights).
- `references/subtitles.md` — deriving timed subtitles from `as-built.md` + step timing,
  the optional-music note, and why narration audio is out of scope.
