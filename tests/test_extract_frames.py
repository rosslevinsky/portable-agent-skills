#!/usr/bin/env python3
"""`extract-frames.sh` must not report success over a caller's bad argument.

The scene threshold is a fraction — ffmpeg's `gt(scene,N)` compares against a change score in
0.0–1.0 — so a value outside that range makes every frame fail the test. The script then falls
through to its 1 fps fallback, which succeeds, and exits **0**: a clean run, a directory of
frames nobody asked for, and no signal that the argument was wrong.

The guard is checked **before** the `command -v ffmpeg` probe and the input-file test, and
that ordering is what these tests pin. Argument shape is knowable without touching the
filesystem, and it is the only ordering testable without a real video — otherwise "input video
not found" fires first and a threshold test passes whether or not the guard exists.
"""
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "skills" / "web-verify" / "references" / "extract-frames.sh"


def working_bash():
    """A bash that can actually RUN something, or `None`.

    Not `shutil.which("bash")`. Windows ships `C:\\Windows\\System32\\bash.exe` — the WSL
    launcher — which exists on `PATH` whether or not a distribution is installed. Without
    one it exits non-zero and writes nothing to stderr, so a suite that trusted `which`
    ran every case against a shell that never started and reported the empty output as a
    failed assertion. Ask the question that matters: run something trivial and see.

    Duplicated in the other new test module rather than shared, because these suites are
    each self-contained by design and a `tests/` package would be a bigger change than
    the eight lines it saved.
    """
    for candidate in (os.environ.get("BASH"), shutil.which("bash")):
        if not candidate:
            continue
        try:
            probe = subprocess.run([candidate, "-c", "printf ok"], capture_output=True,
                                   text=True, encoding="utf-8", errors="replace", timeout=60)
        except (OSError, subprocess.SubprocessError):
            continue
        if probe.returncode == 0 and probe.stdout.strip() == "ok":
            return candidate
    return None


BASH = working_bash()


@unittest.skipIf(BASH is None, "no working bash to run the script with")
class SceneThreshold(unittest.TestCase):
    """Every case uses a NONEXISTENT input on purpose — see the module docstring."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ef-test-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.outdir = Path(self.tmp) / "frames"

    def _run(self, threshold):
        return subprocess.run(
            [BASH, str(SCRIPT), "definitely-not-a-video.webm", str(self.outdir), threshold],
            capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=60)

    def test_out_of_range_and_non_numeric_thresholds_are_refused(self):
        for threshold in ("-0.5", "1.5", "2", "30", "abc", ".", "0.1.2", "1e-1", "-1"):
            with self.subTest(threshold=threshold):
                result = self._run(threshold)
                self.assertEqual(result.returncode, 1,
                                 f"exit {result.returncode} for {threshold!r}")
                self.assertIn("scene threshold", result.stderr,
                              "the message must name the threshold — anything else means "
                              "the check runs after the input test and this passes for "
                              "the wrong reason")
                self.assertIn("usage:", result.stderr)

    def test_the_boundaries_themselves_are_accepted(self):
        """0.0 and 1.0 are valid thresholds; refusing them would be the opposite bug."""
        for threshold in ("0.0", "1.0", "0", "1", "0.30", ".5", "1.000"):
            with self.subTest(threshold=threshold):
                result = self._run(threshold)
                self.assertNotIn(
                    "scene threshold", result.stderr,
                    f"{threshold!r} is in range and was refused")

    def test_a_refused_threshold_leaves_the_filesystem_alone(self):
        """Asserted on the DISK, not on the message.

        Reading the message proves which check fired; it does not prove nothing was
        created. A guard that ran after `mkdir -p "$OUTDIR"` would still print the right
        error while leaving a directory behind — so the directory is what is checked.
        """
        result = self._run("1.5")
        self.assertEqual(result.returncode, 1)
        self.assertFalse(self.outdir.exists(),
                         "the output directory was created before the threshold was "
                         "validated — the guard is running too late")

    def test_the_refusal_happens_before_the_input_and_ffmpeg_checks(self):
        """The ordering, asserted on which check fires.

        A bad threshold, a missing input and a missing ffmpeg all exit non-zero, so the
        code alone cannot tell them apart. Reaching either later check means the guard is
        in the wrong place, and a caller with a real video and a bad threshold would get
        frames and a zero exit.
        """
        bad = self._run("1.5")
        self.assertNotIn("input video not found", bad.stderr)
        self.assertNotIn("ffmpeg not found", bad.stderr)

        # The control. Which later check answers depends on whether ffmpeg is installed —
        # it is probed first — and either answer proves the threshold was accepted.
        good = self._run("0.30")
        self.assertTrue(
            "input video not found" in good.stderr or "ffmpeg not found" in good.stderr,
            f"an in-range threshold should fall through, got: {good.stderr!r}")


class LineEndingsAreTheRepositorysDecisionNotTheClonesTests(unittest.TestCase):
    """A shell script checked out with CRLF is a shell script that does not run.

    Measured by cloning this repository with `core.autocrlf=true` — the Git for Windows
    installer default. The checkout gets 161 CRLF pairs and both ways of invoking the script
    die::

        $ ./extract-frames.sh     env: 'bash\\r': No such file or directory        exit 127
        $ bash extract-frames.sh  $'\\r': command not found
                                  set: pipefail: invalid option name               exit 2

    `web-verify/SKILL.md` tells the user to run this file, and it is the only shell script the
    pack ships. Whether the CI runner happens to check out LF is not the question: a user's
    clone is the artefact that matters, so the bytes are pinned in `.gitattributes`.

    Asserted through `git check-attr`, which reads the guarantee rather than a consequence of
    it — the bytes on a Linux host are LF whatever the attributes say. Skipped without git:
    the shipped suite runs inside a user's installed pack, which is nobody's repository.
    """

    @staticmethod
    def _tracked_shell_scripts():
        """Every tracked `*.sh`, or `None` when git cannot say."""
        try:
            proc = subprocess.run(
                ["git", "-C", str(REPO_ROOT), "ls-files", "-z", "*.sh"],
                capture_output=True, timeout=30)
        except (OSError, subprocess.SubprocessError):
            return None
        if proc.returncode != 0:
            return None
        # Bytes, then `os.fsdecode` — git writes raw path bytes and text mode would decode
        # them with the locale's codec. The same trap the validator was carrying.
        names = [os.fsdecode(p) for p in proc.stdout.split(b"\0") if p]
        return names or None

    def setUp(self):
        self.scripts = self._tracked_shell_scripts()
        if self.scripts is None:
            self.skipTest("not a git checkout — an installed pack is nobody's repository")

    def test_the_script_the_skill_names_is_among_them(self):
        """Guards the sweep below against passing because it found nothing."""
        self.assertIn("skills/web-verify/references/extract-frames.sh", self.scripts)

    def test_every_tracked_shell_script_is_pinned_to_lf(self):
        for rel in self.scripts:
            with self.subTest(script=rel):
                proc = subprocess.run(
                    ["git", "-C", str(REPO_ROOT), "check-attr", "eol", "--", rel],
                    capture_output=True, encoding="utf-8", errors="replace", timeout=30)
                self.assertEqual(proc.returncode, 0, proc.stderr)
                self.assertTrue(
                    proc.stdout.strip().endswith(": lf"),
                    f"{rel} has no eol attribute pinning it to LF, so a clone with "
                    f"core.autocrlf=true gets CRLF and bash refuses to run it. "
                    f"git said: {proc.stdout.strip()!r}")

    def test_no_tracked_shell_script_carries_crlf_on_disk(self):
        """The consequence, checked where it is real.

        On a LF host this restates what the attribute already bought. On the Windows job it
        is the assertion that matters, and it is the one a reader can act on.
        """
        for rel in self.scripts:
            with self.subTest(script=rel):
                self.assertNotIn(
                    b"\r\n", (REPO_ROOT / rel).read_bytes(),
                    f"{rel} is checked out with CRLF; bash dies on the carriage return")


if __name__ == "__main__":
    unittest.main()
