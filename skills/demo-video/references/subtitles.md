# Subtitles, Timing, and Optional Music

Subtitles are derived, not hand-authored: the narration text comes from `as-built.md`
and the timing comes from the tour's per-step start/duration. This keeps the walkthrough
honest — captions describe what was actually built and shown.

## Deriving captions

1. **Text** — for each tour step, pull a one-line description from `as-built.md`: the
   "What was built" summary and the per-phase outcomes are the source. Keep each caption
   short (a viewer reads it in the step's pause window).
2. **Timing** — each step recorded its start time and duration (see
   `guided-tour-spec.md`). Start times must be **normalized to the video's zero point**:
   capture `t0` at recording start (Playwright begins recording at context/page creation,
   before the first step) and subtract it from every step's start. A raw wall-clock start
   (`Date.now()`) is not video-relative and lands every caption early by the pre-step-1
   launch/first-navigation offset. Using these normalized times, map caption *i* to
   `[start_i, start_i + duration_i]`. If a step is too short to read its caption, extend
   the step's pause rather than truncating text.

## Subtitle file

Write a standard subtitle file so any player (and CI preview) can show it. WebVTT
example:

```
WEBVTT

00:00:00.000 --> 00:00:02.500
Open the settings page

00:00:02.500 --> 00:00:05.000
Toggle dark mode

00:00:05.000 --> 00:00:07.500
The theme applies immediately
```

`.srt` is equivalent with comma decimal separators and numbered cues. Save into the
gitignored output directory.

## Optional background music

Music is optional and additive. If used, mux a royalty-free track at low volume under the
tour with ffmpeg, and keep the music file in the gitignored output/asset directory — never
commit it. Absence of music must not fail the tour.

## Out of scope: TTS / voice narration

This skill writes subtitles, not audio. The narration script — the caption text — is the
deliverable; nothing here renders it to speech, and there is no flag that will. If you
want voice, take the subtitle file to a TTS tool yourself.

## Degraded fallback

When ffmpeg is unavailable but Playwright recorded the tour, still deliver its **native
`.webm`** with the subtitle file as a **sidecar** — players load an external `.vtt`/`.srt`
without muxing, so you lose only the muxed-in captions, music, and frame extraction. Only
when no video was recorded at all (no Playwright video) fall back to the **subtitle/script
text alongside an ordered storyboard** so the walkthrough is still readable end-to-end —
and name where those stills come from, since the absent driver is what would have taken
them: images the user already has, or one per step captured by hand. Either way the result
is a real deliverable, not a failure.
