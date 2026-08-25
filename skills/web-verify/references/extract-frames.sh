#!/usr/bin/env bash
#
# extract-frames.sh — TEMPLATE change-point frame extractor for web-verify.
#
# Extracts frames from a recorded UI walkthrough video (e.g. a Playwright *.webm)
# at visual change points, so you can inspect what the UI actually did without
# scrubbing the whole video. This is the OPTIONAL, heavier layer of web-verify —
# screenshots remain the first-class artifact. If ffmpeg is not installed, skip
# this entirely and rely on per-checkpoint screenshots.
#
# This is a template: copy/adapt it, or run it as-is. It writes only into the
# output directory (which should be gitignored) and never modifies the source.
#
# Usage:
#   extract-frames.sh <input-video> [output-dir] [scene-threshold]
#
#   <input-video>     Path to the recorded video (e.g. test-results/flow.webm)
#   [output-dir]      Where to write frames (default: ./web-verify-frames)
#   [scene-threshold] Scene-change sensitivity 0.0-1.0 (default: 0.30;
#                     lower = more frames, higher = only big changes)
#
# Every run writes into a FRESH run-<timestamp>-<pid> subdirectory of the output
# directory, and prints the path it used. The output directory is reused across
# runs by design, so frames written straight into it are indistinguishable from
# the last run's — see the comment above RUNDIR below.
#
# Exit codes:
#   0  frames extracted
#   1  bad args, missing input video, or the run directory could not be created
#   2  ffmpeg is not installed
#   3  ffmpeg ran and failed (unreadable or undecodable video, bad threshold)
#   4  ffmpeg reported success and produced no frames at all
#
# 3 is deliberately distinct from 1: for a tool whose whole job is verification, a
# caller must be able to tell "you passed me nonsense" from "ffmpeg could not decode
# this video". Left to `set -e` an ffmpeg failure propagates ffmpeg's own status,
# which is none of the codes above and differs per failure mode — measured on ffmpeg
# 6.1: 183 invalid data, 234 a bad threshold, 8 an unknown filter, 251 an unwritable
# output — so a caller has nothing stable to branch on.
#
# 4 is distinct from 3 for the same reason one level up: ffmpeg exits 0 having written
# nothing at all for an input it can open but cannot decode. "It failed" and "it
# succeeded and produced nothing" are different facts about the video, and only one of
# them is worth a retry. Without its own code this case exited 0 and announced frames
# that were not there.

set -euo pipefail

INPUT="${1:-}"
OUTDIR="${2:-./web-verify-frames}"
THRESHOLD="${3:-0.30}"

if [ -z "$INPUT" ]; then
  echo "usage: extract-frames.sh <input-video> [output-dir] [scene-threshold]" >&2
  exit 1
fi

# The scene threshold is a fraction: ffmpeg's `gt(scene,N)` compares against a change score
# in 0.0-1.0, so anything outside that range makes every frame fail the test. The run then
# falls through to the 1 fps fallback, succeeds, and exits 0 — reporting a clean extraction
# over a caller's mistake, with frames nobody asked for.
#
# Checked HERE, with the other argument validation and before the ffmpeg probe and the
# input-file test below. Argument shape is knowable without touching the filesystem, so
# this way the caller gets the most specific error; the other order answers "input video
# not found" to a request whose real problem was the threshold.
# Pattern matching rather than arithmetic: POSIX `test` compares integers only, and awk or
# bc would be a second dependency in a script whose whole point is that it needs ffmpeg and
# nothing else. A leading `-` is rejected by the digits-and-dot class, so only the upper
# bound needs its own case.
#
# The grammar accepted is plain decimal: `0`, `1`, `0.30`, `.5`, `1.000`. Spellings ffmpeg
# would also take — `+0.5`, `00.5`, `1e-1` — are refused. That is a deliberate limit, not an
# oversight: the refusal names the offending value and prints the usage line, so the caller
# writes `0.5` and moves on. A wider grammar is more surface to get wrong, and the failure
# it would prevent is a clear error message, not a silent success.
case "$THRESHOLD" in
  ''|.|*[!0-9.]*|*.*.*)  THRESHOLD_OK=false ;;   # empty, bare dot, non-numeric, two dots
  0|0.*|.[0-9]*)         THRESHOLD_OK=true ;;    # 0, 0.30, .30 — below one
  1|1.)                  THRESHOLD_OK=true ;;    # exactly one
  1.*)                   case "${THRESHOLD#1.}" in   # 1.000 is one; 1.5 is not
                           *[!0]*) THRESHOLD_OK=false ;;
                           *)      THRESHOLD_OK=true ;;
                         esac ;;
  *)                     THRESHOLD_OK=false ;;   # 2, 30, 1e-1 — above one or not a number
esac
if [ "$THRESHOLD_OK" != true ]; then
  echo "scene threshold must be a number from 0.0 to 1.0 (got: $THRESHOLD)" >&2
  echo "usage: extract-frames.sh <input-video> [output-dir] [scene-threshold]" >&2
  exit 1
fi

if ! command -v ffmpeg >/dev/null 2>&1; then
  echo "ffmpeg not found — skip frame extraction and rely on screenshots instead." >&2
  exit 2
fi

if [ ! -f "$INPUT" ]; then
  echo "input video not found: $INPUT" >&2
  exit 1
fi

mkdir -p "$OUTDIR"

# Each run gets its own subdirectory, and every path below is scoped to it. Writing
# into "$OUTDIR" directly hands back the previous run's work as if it were this one's:
# ffmpeg restarts the change-%04d numbering each time, so a shorter second run leaves
# the first run's higher-numbered frames sitting beside its own, and the count below —
# which globs the directory rather than counting what this run produced — sees them,
# suppresses the fallback, and reports success over stale files. Clearing change-*.png
# instead would still leave sample-*.png from an earlier fallback behind.
RUNDIR="$OUTDIR/run-$(date +%Y%m%d-%H%M%S)-$$"
if ! mkdir "$RUNDIR"; then
  echo "could not create run directory: $RUNDIR" >&2
  exit 1
fi

# Primary pass: emit a frame at each scene change above the threshold.
# -vsync vfr keeps only the selected frames; showinfo logs their timestamps
# (it emits at info level, so this pass runs at -loglevel info to surface them).
# -y so a run can never stop on an overwrite prompt. ffmpeg does not prompt for an
# image2 sequence, so this changes nothing today; it states the requirement instead
# of leaving it to a behaviour someone has to know.
if ! ffmpeg -y -hide_banner -loglevel info -i "$INPUT" \
  -vf "select='gt(scene,${THRESHOLD})',showinfo" \
  -vsync vfr "$RUNDIR/change-%04d.png"; then
  # Name the run directory on the way out. ffmpeg may already have written frames
  # before it failed, and those are partial and must not be mistaken for a result —
  # a caller cannot delete or inspect what it was never told the path of, and with
  # concurrent runs it cannot tell which directory was this invocation's.
  echo "ffmpeg failed extracting change-point frames from: $INPUT (partial output: $RUNDIR)" >&2
  exit 3
fi

count="$(find "$RUNDIR" -maxdepth 1 -name 'change-*.png' | wc -l | tr -d ' ')"

# Fallback: if scene detection found too few frames (e.g. a subtle flow),
# also sample one frame per second so nothing important is missed.
if [ "$count" -lt 2 ]; then
  echo "only $count change-point frame(s); adding a 1 fps sample as fallback." >&2
  if ! ffmpeg -y -hide_banner -loglevel warning -i "$INPUT" \
    -vf "fps=1" "$RUNDIR/sample-%04d.png"; then
    echo "ffmpeg failed extracting fallback frames from: $INPUT (partial output: $RUNDIR)" >&2
    exit 3
  fi
  # RECOUNT. ffmpeg exits 0 having written nothing at all for an input it can open but
  # cannot decode, so without this the script announced "Frames written to: ..." over an
  # empty directory and returned success. The caller's next move is to read the frames,
  # and finding none is a confusing way to learn there are none.
  count="$(find "$RUNDIR" -maxdepth 1 \( -name 'change-*.png' -o -name 'sample-*.png' \) | wc -l | tr -d ' ')"
fi

if [ "$count" -eq 0 ]; then
  echo "no frames were extracted from: $INPUT (empty output: $RUNDIR)" >&2
  echo "ffmpeg reported success and produced nothing, so there is nothing to inspect." >&2
  exit 4
fi

echo "Frames written to: $RUNDIR"
echo "Inspect each frame and confirm the expected UI content is actually present —"
echo "a produced frame is evidence to read, not proof on its own."
