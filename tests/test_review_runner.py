"""Unit tests for the stdlib-only diff-review supervisor, ``review_runner.py``.

These exercise the real supervision paths by launching short Python child processes:
clean/stale/erroring verdicts, the two result modes, the idle + deadline bounds, and — the
one that matters most — that the liveness heartbeat resets per *chunk*.

The engine lives at ``skills/diff-review/review_runner.py``. That directory name has a
hyphen, so it is NOT importable as a package; it goes on ``sys.path`` by an ABSOLUTE path
derived from THIS file. Timing margins are generous so the suite stays deterministic on CI.
"""

import ast
import contextlib
import errno
import io
import json
import os
import re
import shutil
import subprocess
import signal
import threading
import sys
import tempfile
import time
import unittest
import inspect
import unittest.mock
from pathlib import Path

_ENGINE_DIR = Path(__file__).resolve().parent.parent / "skills" / "diff-review"
if str(_ENGINE_DIR) not in sys.path:
    sys.path.insert(0, str(_ENGINE_DIR))

import review_runner  # noqa: E402

PY = sys.executable


def _run(*args):
    """Call the runner in-process; return the parsed JSON status line."""
    buf, err = io.StringIO(), io.StringIO()
    # stderr too. argparse prints its usage block to the real stderr BEFORE raising
    # SystemExit, so a bad-argv case dumped a usage message into the middle of an
    # otherwise-passing run — which reads exactly like a crash and taught anyone watching
    # the suite to ignore it.
    with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(err):
        review_runner.main(list(args))
    return json.loads(buf.getvalue().strip().splitlines()[-1])


def _parsed(*argv):
    """Build an args namespace through the REAL parser, the way a caller reaches it.

    Hand-rolling a namespace would drift from the parser's own defaults, and the defaults
    are part of what the pre-flight is asked about.
    """
    captured = []
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        with unittest.mock.patch.object(review_runner, "run",
                                        lambda a: captured.append(a) or 0):
            review_runner.main(list(argv))
    assert captured, "main never reached run"
    return captured[0]


class ExternalFileMode(unittest.TestCase):
    def test_clean_verdict_ok(self):
        with tempfile.TemporaryDirectory() as d:
            f = str(Path(d) / "findings.txt")
            res = _run("--idle", "5", "--deadline", "10", "--findings", f,
                       "--result-mode", "external-file",
                       "--", PY, "-c", "import sys; open(sys.argv[1],'w').write('VERDICT')", f)
            self.assertEqual(res["status"], "ok")
            self.assertEqual(Path(f).read_text(), "VERDICT")

    def test_no_write_is_error(self):
        # A child that exits 0 without writing the verdict must NOT report ok.
        with tempfile.TemporaryDirectory() as d:
            f = str(Path(d) / "findings.txt")
            res = _run("--idle", "5", "--deadline", "10", "--findings", f,
                       "--result-mode", "external-file", "--", PY, "-c", "pass")
            self.assertEqual(res["status"], "error")

    def test_stale_file_is_error(self):
        # A pre-existing findings file is refused BEFORE launch rather than deleted, so
        # stale content can never be passed off as a fresh verdict. The file is left
        # exactly where it was — this supervisor removes only what it created.
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "findings.txt"
            f.write_text("STALE")
            res = _run("--idle", "5", "--deadline", "10", "--findings", str(f),
                       "--result-mode", "external-file", "--", PY, "-c", "pass")
            self.assertEqual(res["status"], "error")
            self.assertEqual(f.read_text(), "STALE")

    def test_nonzero_exit_is_error(self):
        with tempfile.TemporaryDirectory() as d:
            f = str(Path(d) / "findings.txt")
            res = _run("--idle", "5", "--deadline", "10", "--findings", f,
                       "--result-mode", "external-file", "--", PY, "-c",
                       "import sys; open(sys.argv[1],'w').write('V'); sys.exit(3)", f)
            self.assertEqual(res["status"], "error")


class StreamJsonMode(unittest.TestCase):
    def test_success_extracts_payload_not_wrapper(self):
        with tempfile.TemporaryDirectory() as d:
            f = str(Path(d) / "findings.txt")
            child = (r'import json; '
                     r'print(json.dumps({"type":"assistant","x":1})); '
                     r'print(json.dumps({"type":"result","subtype":"success",'
                     r'"is_error":False,"result":"LGTM"}))')
            res = _run("--idle", "5", "--deadline", "10", "--findings", f,
                       "--result-mode", "stream-json-result-event", "--", PY, "-c", child)
            self.assertEqual(res["status"], "ok")
            self.assertEqual(Path(f).read_text().strip(), "LGTM")  # payload, not the JSON envelope

    def test_is_error_true_rejected(self):
        # is_error overrides even a "success" subtype.
        with tempfile.TemporaryDirectory() as d:
            f = str(Path(d) / "findings.txt")
            child = (r'import json; print(json.dumps({"type":"result",'
                     r'"subtype":"success","is_error":True,"result":"boom"}))')
            res = _run("--idle", "5", "--deadline", "10", "--findings", f,
                       "--result-mode", "stream-json-result-event", "--", PY, "-c", child)
            self.assertEqual(res["status"], "error")

    def test_non_success_subtype_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            f = str(Path(d) / "findings.txt")
            child = (r'import json; print(json.dumps({"type":"result",'
                     r'"subtype":"error_during_execution","is_error":False,"result":"partial"}))')
            res = _run("--idle", "5", "--deadline", "10", "--findings", f,
                       "--result-mode", "stream-json-result-event", "--", PY, "-c", child)
            self.assertEqual(res["status"], "error")

    def test_missing_subtype_rejected(self):
        # Affirmative success required: a result with no subtype does not prove success.
        with tempfile.TemporaryDirectory() as d:
            f = str(Path(d) / "findings.txt")
            child = (r'import json; print(json.dumps({"type":"result",'
                     r'"is_error":False,"result":"unproven"}))')
            res = _run("--idle", "5", "--deadline", "10", "--findings", f,
                       "--result-mode", "stream-json-result-event", "--", PY, "-c", child)
            self.assertEqual(res["status"], "error")

    def test_success_then_error_resolves_to_error(self):
        # The FINAL terminal result wins: a later error invalidates an earlier success.
        with tempfile.TemporaryDirectory() as d:
            f = str(Path(d) / "findings.txt")
            child = (r'import json; '
                     r'print(json.dumps({"type":"result","subtype":"success","is_error":False,"result":"LGTM"})); '
                     r'print(json.dumps({"type":"result","subtype":"error_during_execution","is_error":True,"result":"boom"}))')
            res = _run("--idle", "5", "--deadline", "10", "--findings", f,
                       "--result-mode", "stream-json-result-event", "--", PY, "-c", child)
            self.assertEqual(res["status"], "error")

    def test_missing_result_is_error(self):
        with tempfile.TemporaryDirectory() as d:
            f = str(Path(d) / "findings.txt")
            child = r'import json; print(json.dumps({"type":"assistant","x":1}))'
            res = _run("--idle", "5", "--deadline", "10", "--findings", f,
                       "--result-mode", "stream-json-result-event", "--", PY, "-c", child)
            self.assertEqual(res["status"], "error")


class StreamTranscript(unittest.TestCase):
    def test_concatenates_codex_agent_messages_skipping_reasoning(self):
        with tempfile.TemporaryDirectory() as d:
            f = str(Path(d) / "findings.txt")
            child = (r'import json; '
                     r'print(json.dumps({"type":"item.completed","item":{"type":"agent_message","text":"First finding."}})); '
                     r'print(json.dumps({"type":"item.completed","item":{"type":"reasoning","text":"internal"}})); '
                     r'print(json.dumps({"type":"item.completed","item":{"type":"agent_message","text":"Second finding."}})); '
                     r'print(json.dumps({"type":"turn.completed"}))')
            res = _run("--idle", "5", "--deadline", "10", "--findings", f,
                       "--result-mode", "stream-transcript", "--", PY, "-c", child)
            self.assertEqual(res["status"], "ok")
            self.assertEqual(Path(f).read_text().strip(), "First finding.\n\nSecond finding.")  # paragraph-joined

    def test_concatenates_claude_assistant_text_skipping_tools_and_result(self):
        with tempfile.TemporaryDirectory() as d:
            f = str(Path(d) / "findings.txt")
            child = (r'import json; '
                     r'print(json.dumps({"type":"assistant","message":{"content":[{"type":"text","text":"Review A."}]}})); '
                     r'print(json.dumps({"type":"assistant","message":{"content":[{"type":"tool_use","id":"x"},{"type":"text","text":"Review B."}]}})); '
                     r'print(json.dumps({"type":"result","subtype":"success","is_error":False,"result":"Review A. Review B."}))')
            res = _run("--idle", "5", "--deadline", "10", "--findings", f,
                       "--result-mode", "stream-transcript", "--", PY, "-c", child)
            self.assertEqual(res["status"], "ok")
            self.assertEqual(Path(f).read_text().strip(), "Review A.\n\nReview B.")  # result event not duplicated

    def test_subagent_forwarded_text_is_excluded(self):
        with tempfile.TemporaryDirectory() as d:
            f = str(Path(d) / "findings.txt")
            child = (r'import json; '
                     r'print(json.dumps({"type":"assistant","parent_tool_use_id":"t1","message":{"content":[{"type":"text","text":"SUBAGENT"}]}})); '
                     r'print(json.dumps({"type":"assistant","message":{"content":[{"type":"text","text":"MAIN review"}]}})); '
                     r'print(json.dumps({"type":"result","subtype":"success","is_error":False,"result":"MAIN review"}))')
            res = _run("--idle", "5", "--deadline", "10", "--findings", f,
                       "--result-mode", "stream-transcript", "--", PY, "-c", child)
            self.assertEqual(res["status"], "ok")
            self.assertEqual(Path(f).read_text().strip(), "MAIN review")

    def test_terminal_failure_rejects_partial_transcript(self):
        # Non-empty text + exit 0, but a terminal turn.failed must NOT become an "ok" verdict.
        with tempfile.TemporaryDirectory() as d:
            f = str(Path(d) / "findings.txt")
            child = (r'import json; '
                     r'print(json.dumps({"type":"item.completed","item":{"type":"agent_message","text":"partial output"}})); '
                     r'print(json.dumps({"type":"turn.failed"}))')
            res = _run("--idle", "5", "--deadline", "10", "--findings", f,
                       "--result-mode", "stream-transcript", "--", PY, "-c", child)
            self.assertEqual(res["status"], "error")

    def test_no_terminal_event_is_error(self):
        # Non-empty text + exit 0 but NO terminal event → must NOT be accepted (could be truncated).
        with tempfile.TemporaryDirectory() as d:
            f = str(Path(d) / "findings.txt")
            child = r'import json; print(json.dumps({"type":"item.completed","item":{"type":"agent_message","text":"partial output"}}))'
            res = _run("--idle", "5", "--deadline", "10", "--findings", f,
                       "--result-mode", "stream-transcript", "--", PY, "-c", child)
            self.assertEqual(res["status"], "error")

    def test_claude_error_result_rejects_transcript(self):
        with tempfile.TemporaryDirectory() as d:
            f = str(Path(d) / "findings.txt")
            child = (r'import json; '
                     r'print(json.dumps({"type":"assistant","message":{"content":[{"type":"text","text":"some review text"}]}})); '
                     r'print(json.dumps({"type":"result","subtype":"error_during_execution","is_error":True,"result":"boom"}))')
            res = _run("--idle", "5", "--deadline", "10", "--findings", f,
                       "--result-mode", "stream-transcript", "--", PY, "-c", child)
            self.assertEqual(res["status"], "error")

    def test_no_text_output_is_error(self):
        with tempfile.TemporaryDirectory() as d:
            f = str(Path(d) / "findings.txt")
            # reasoning only + a successful terminal → still error, because there is no findings text.
            child = (r'import json; '
                     r'print(json.dumps({"type":"item.completed","item":{"type":"reasoning","text":"x"}})); '
                     r'print(json.dumps({"type":"turn.completed"}))')
            res = _run("--idle", "5", "--deadline", "10", "--findings", f,
                       "--result-mode", "stream-transcript", "--", PY, "-c", child)
            self.assertEqual(res["status"], "error")


class PartialTranscriptIsPreserved(unittest.TestCase):
    """A failed run keeps the reviewer's text, at a path that is NOT the findings file.

    `--findings` means "a review completed": it is written only on success and its path is
    reported only on success, so a caller may test the file instead of the status and still
    be right. Writing a truncated review there would make a review that got through three of
    twelve files look like a clean review of all twelve. The text is still worth keeping — a
    long cross-model review that dies on a usage limit is expensive to lose — so it goes
    beside the findings file under its own name, and the status line names it under its own
    key.
    """

    CHILD = (r'import json; '
             r'print(json.dumps({"type":"item.completed","item":'
             r'{"type":"agent_message","text":"partial output"}})); '
             r'print(json.dumps({"type":"turn.failed"}))')

    def _failed_run(self, d):
        f = str(Path(d) / "findings.txt")
        res = _run("--idle", "5", "--deadline", "10", "--findings", f,
                   "--result-mode", "stream-transcript", "--", PY, "-c", self.CHILD)
        return f, res

    def test_the_findings_file_is_still_absent_and_unreported(self):
        with tempfile.TemporaryDirectory() as d:
            f, res = self._failed_run(d)
            self.assertEqual(res["status"], "error")
            self.assertIsNone(res["findings"],
                              "a failed run named a findings file, which means a review")
            self.assertFalse(Path(f).exists(),
                             "the findings file survived a failed run; a caller that tests "
                             "for it reads a truncated review as a complete one")

    def test_the_text_is_kept_beside_it_and_named_in_the_status(self):
        with tempfile.TemporaryDirectory() as d:
            f, res = self._failed_run(d)
            kept = res.get("partial_findings")
            self.assertIsNotNone(kept, f"the reviewer's text was discarded: {res}")
            self.assertNotEqual(kept, f, "the partial went to the findings path itself")
            self.assertIn("partial output", Path(kept).read_text(encoding="utf-8"))

    def test_the_kept_file_says_it_is_not_a_review(self):
        with tempfile.TemporaryDirectory() as d:
            _, res = self._failed_run(d)
            head = Path(res["partial_findings"]).read_text(encoding="utf-8").splitlines()[0]
            self.assertIn("INCOMPLETE", head,
                          f"nothing at the top marks this as a failed run: {head!r}")

    def test_a_successful_run_keeps_nothing_extra(self):
        """Anti-vacuity: a guard that fired on every run would satisfy the three above."""
        with tempfile.TemporaryDirectory() as d:
            f = str(Path(d) / "findings.txt")
            child = (r'import json; '
                     r'print(json.dumps({"type":"item.completed","item":'
                     r'{"type":"agent_message","text":"a real review"}})); '
                     r'print(json.dumps({"type":"turn.completed"}))')
            res = _run("--idle", "5", "--deadline", "10", "--findings", f,
                       "--result-mode", "stream-transcript", "--", PY, "-c", child)
            self.assertEqual(res["status"], "ok")
            self.assertIsNone(res.get("partial_findings"))
            self.assertEqual(list(Path(d).iterdir()), [Path(f)],
                             "a successful run left a partial file beside its findings")

    def test_nothing_is_written_when_the_reviewer_produced_no_text(self):
        with tempfile.TemporaryDirectory() as d:
            f = str(Path(d) / "findings.txt")
            child = r'import json; print(json.dumps({"type":"turn.failed"}))'
            res = _run("--idle", "5", "--deadline", "10", "--findings", f,
                       "--result-mode", "stream-transcript", "--", PY, "-c", child)
            self.assertEqual(res["status"], "error")
            self.assertIsNone(res.get("partial_findings"))
            self.assertEqual(list(Path(d).iterdir()), [],
                             "an empty partial file was written for a silent reviewer")

    @unittest.skipUnless(hasattr(os, "symlink"), "no symlinks on this host")
    def test_a_symlink_at_the_partial_path_is_never_written_through(self):
        """The claim on `--findings` refuses a planted symlink; this path cannot claim, so
        it refuses to write instead. Preserving text is never worth following a link
        somebody else placed."""
        with tempfile.TemporaryDirectory() as d:
            target = Path(d) / "elsewhere.txt"
            target.write_text("untouched", encoding="utf-8")
            f = str(Path(d) / "findings.txt")
            try:
                os.symlink(target, Path(f + ".partial"))
            except (OSError, NotImplementedError):
                self.skipTest("this host does not permit creating a symlink")
            res = _run("--idle", "5", "--deadline", "10", "--findings", f,
                       "--result-mode", "stream-transcript", "--", PY, "-c", self.CHILD)
            self.assertEqual(res["status"], "error")
            self.assertIsNone(res.get("partial_findings"))
            self.assertEqual(target.read_text(encoding="utf-8"), "untouched")


class Bounds(unittest.TestCase):
    def test_idle_timeout(self):
        with tempfile.TemporaryDirectory() as d:
            f = str(Path(d) / "findings.txt")
            res = _run("--idle", "1", "--deadline", "20", "--findings", f,
                       "--result-mode", "external-file", "--", PY, "-c",
                       "import time; time.sleep(8)")
            self.assertEqual(res["status"], "idle_timeout")

    def test_deadline_fires_while_active(self):
        with tempfile.TemporaryDirectory() as d:
            f = str(Path(d) / "findings.txt")
            child = ("import sys,time\n"
                     "while True:\n"
                     "    sys.stdout.write('x\\n'); sys.stdout.flush(); time.sleep(0.05)")
            res = _run("--idle", "100", "--deadline", "1", "--findings", f,
                       "--result-mode", "external-file", "--", PY, "-c", child)
            self.assertEqual(res["status"], "deadline")

    def test_chunk_heartbeat_no_newline_survives(self):
        # The child streams dots with NO newline for ~3s, longer than the 1s idle window.
        # A per-line heartbeat would kill it; a per-chunk heartbeat keeps it alive.
        with tempfile.TemporaryDirectory() as d:
            f = str(Path(d) / "findings.txt")
            child = ("import sys,time\n"
                     "for _ in range(20):\n"
                     "    sys.stdout.write('.'); sys.stdout.flush(); time.sleep(0.15)\n"
                     "open(sys.argv[1],'w').write('DONE')")
            # idle window (2s) is comfortably larger than the 0.15s write cadence, so the
            # per-chunk heartbeat keeps it alive even under CI scheduling jitter.
            res = _run("--idle", "2", "--deadline", "30", "--findings", f,
                       "--result-mode", "external-file", "--", PY, "-c", child, f)
            self.assertEqual(res["status"], "ok")
            self.assertEqual(Path(f).read_text(), "DONE")


class Robustness(unittest.TestCase):
    def test_cli_not_found_is_error(self):
        with tempfile.TemporaryDirectory() as d:
            f = str(Path(d) / "findings.txt")
            res = _run("--idle", "5", "--deadline", "10", "--findings", f,
                       "--result-mode", "external-file",
                       "--", "definitely-not-a-real-cli-xyzzy", "arg")
            self.assertEqual(res["status"], "error")

    def test_child_stdin_is_devnull_not_a_hang(self):
        # A reviewer CLI that reads stdin must get immediate EOF, not block forever
        # (a real cross-model dogfood hung here until stdin was redirected to DEVNULL).
        with tempfile.TemporaryDirectory() as d:
            f = str(Path(d) / "findings.txt")
            child = "import sys; sys.stdin.read(); open(sys.argv[1],'w').write('EOF-OK')"
            res = _run("--idle", "3", "--deadline", "15", "--findings", f,
                       "--result-mode", "external-file", "--", PY, "-c", child, f)
            self.assertEqual(res["status"], "ok")  # not idle_timeout
            self.assertEqual(Path(f).read_text(), "EOF-OK")

    def test_nonfinite_timeout_is_error(self):
        # --idle nan would make every idle comparison false, disabling supervision.
        with tempfile.TemporaryDirectory() as d:
            f = str(Path(d) / "findings.txt")
            res = _run("--idle", "nan", "--deadline", "10", "--findings", f,
                       "--result-mode", "external-file", "--", PY, "-c", "pass")
            self.assertEqual(res["status"], "error")

    def test_invalid_invocation_still_emits_json(self):
        # Missing required --findings: argparse would exit(2); the runner must still emit JSON.
        res = _run("--result-mode", "external-file", "--", PY, "-c", "pass")
        self.assertEqual(res["status"], "error")

    def test_every_nonok_has_reason(self):
        with tempfile.TemporaryDirectory() as d:
            f = str(Path(d) / "findings.txt")
            res = _run("--idle", "5", "--deadline", "10", "--findings", f,
                       "--result-mode", "external-file", "--", PY, "-c", "pass")
            self.assertEqual(res["status"], "error")
            self.assertTrue(res.get("reason"))


VERDICT = '{"findings": [], "overall": "clean", "blocking_count": 0}'
SHIPPED_SCHEMA = str(_ENGINE_DIR / "review-schema.json")


def _echo_argv_child(dest_index, value_index):
    """A child that writes one of its own argv elements to a file — proves substitution.

    Writes UTF-8 explicitly. The schema carries em dashes, and a child writing under
    Windows' default text encoding would be round-tripping the payload through cp1252
    rather than testing what the runner actually passed.
    """
    return (
        f"import sys, io; "
        f"io.open(sys.argv[{dest_index}],'w',encoding='utf-8')"
        f".write(sys.argv[{value_index}])"
    )


def _read_utf8(path):
    """Read a file as UTF-8 — never the platform default, which differs on Windows."""
    return Path(path).read_text(encoding="utf-8")


class SchemaSubstitution(unittest.TestCase):
    """ONE schema file, two argv forms — the runtimes disagree on how it is passed."""

    def test_path_marker_becomes_an_absolute_file_path(self):
        with tempfile.TemporaryDirectory() as d:
            f = str(Path(d) / "findings.txt")
            res = _run("--idle", "5", "--deadline", "10", "--findings", f,
                       "--result-mode", "external-file", "--schema", SHIPPED_SCHEMA,
                       "--", PY, "-c", _echo_argv_child(1, 2), f, "⟪schema_path⟫")
            self.assertEqual(res["status"], "ok")
            written = _read_utf8(f)
            self.assertTrue(Path(written).is_absolute())
            self.assertTrue(written.endswith("review-schema.json"))

    def test_json_marker_becomes_the_compact_inline_document(self):
        with tempfile.TemporaryDirectory() as d:
            f = str(Path(d) / "findings.txt")
            res = _run("--idle", "5", "--deadline", "10", "--findings", f,
                       "--result-mode", "external-file", "--schema", SHIPPED_SCHEMA,
                       "--", PY, "-c", _echo_argv_child(1, 2), f, "⟪schema_json⟫")
            self.assertEqual(res["status"], "ok")
            written = _read_utf8(f)
            self.assertNotIn("\n", written)  # single argv-safe line
            self.assertEqual(json.loads(written), json.loads(_read_utf8(SHIPPED_SCHEMA)))

    def test_marker_embedded_in_a_larger_argument_is_substituted(self):
        with tempfile.TemporaryDirectory() as d:
            f = str(Path(d) / "findings.txt")
            _run("--idle", "5", "--deadline", "10", "--findings", f,
                 "--result-mode", "external-file", "--schema", SHIPPED_SCHEMA,
                 "--", PY, "-c", _echo_argv_child(1, 2), f, "--schema=⟪schema_path⟫")
            written = _read_utf8(f)
            # Asserted structurally, not as a leading "/": an absolute path starts with
            # a drive letter on Windows.
            self.assertTrue(written.startswith("--schema="))
            self.assertTrue(Path(written[len("--schema="):]).is_absolute())
            self.assertTrue(written.endswith("review-schema.json"))

    def test_marker_without_a_schema_is_a_hard_error(self):
        # The caller asked for enforcement; launching an UNENFORCED review instead
        # would misreport what actually ran.
        with tempfile.TemporaryDirectory() as d:
            f = str(Path(d) / "findings.txt")
            res = _run("--idle", "5", "--deadline", "10", "--findings", f,
                       "--result-mode", "external-file",
                       "--", PY, "-c", "pass", "⟪schema_json⟫")
            self.assertEqual(res["status"], "error")
            self.assertIn("--schema", res["reason"])

    def test_malformed_schema_fails_before_launch(self):
        with tempfile.TemporaryDirectory() as d:
            f = str(Path(d) / "findings.txt")
            bad = Path(d) / "bad.json"
            bad.write_text('{"type": "object",}')
            res = _run("--idle", "5", "--deadline", "10", "--findings", f,
                       "--result-mode", "external-file", "--schema", str(bad),
                       "--", PY, "-c", _echo_argv_child(1, 2), f, "⟪schema_json⟫")
            self.assertEqual(res["status"], "error")
            self.assertIn("not valid JSON", res["reason"])
            self.assertFalse(Path(f).exists())  # never launched

    def test_no_markers_means_the_argv_is_untouched(self):
        with tempfile.TemporaryDirectory() as d:
            f = str(Path(d) / "findings.txt")
            res = _run("--idle", "5", "--deadline", "10", "--findings", f,
                       "--result-mode", "external-file",
                       "--", PY, "-c", _echo_argv_child(1, 2), f, "plain-arg")
            self.assertEqual(res["status"], "ok")
            self.assertEqual(_read_utf8(f), "plain-arg")


class VerdictExtraction(unittest.TestCase):
    """The structured verdict is written ALONGSIDE the narrative, never instead of it."""

    @staticmethod
    def _transcript_child(text):
        """Child emitting one agent_message carrying ``text``, then a success terminal."""
        return (
            "import json; "
            f"text = {text!r}; "
            "print(json.dumps({'type':'item.completed',"
            "'item':{'type':'agent_message','text':text}})); "
            "print(json.dumps({'type':'turn.completed'}))"
        )

    def test_full_transcript_is_kept_and_the_verdict_extracted_beside_it(self):
        with tempfile.TemporaryDirectory() as d:
            f, v = str(Path(d) / "findings.txt"), str(Path(d) / "verdict.json")
            narrative = "I read every hunk.\n\n" + VERDICT
            res = _run("--idle", "5", "--deadline", "10", "--findings", f,
                       "--result-mode", "stream-transcript", "--verdict-json", v,
                       "--", PY, "-c", self._transcript_child(narrative))
            self.assertEqual(res["status"], "ok")
            # The reasoning survives in full — losing it would be a regression.
            self.assertIn("I read every hunk.", Path(f).read_text())
            self.assertIn(VERDICT, Path(f).read_text())
            self.assertEqual(res["verdict"], v)
            self.assertIsNone(res["verdict_reason"])
            self.assertEqual(json.loads(Path(v).read_text())["overall"], "clean")

    def test_missing_verdict_degrades_and_never_fails_the_review(self):
        # Rung 2 has no CLI flag to enforce the shape, so a good narrative with no
        # parseable object is still a SUCCESSFUL review.
        with tempfile.TemporaryDirectory() as d:
            f, v = str(Path(d) / "findings.txt"), str(Path(d) / "verdict.json")
            res = _run("--idle", "5", "--deadline", "10", "--findings", f,
                       "--result-mode", "stream-transcript", "--verdict-json", v,
                       "--", PY, "-c", self._transcript_child("Prose only, no object."))
            self.assertEqual(res["status"], "ok")
            self.assertIn("Prose only", Path(f).read_text())
            self.assertIsNone(res["verdict"])
            self.assertTrue(res["verdict_reason"])
            self.assertFalse(Path(v).exists())

    def test_unrelated_json_is_not_mistaken_for_a_verdict(self):
        with tempfile.TemporaryDirectory() as d:
            f, v = str(Path(d) / "findings.txt"), str(Path(d) / "verdict.json")
            res = _run("--idle", "5", "--deadline", "10", "--findings", f,
                       "--result-mode", "stream-transcript", "--verdict-json", v,
                       "--", PY, "-c",
                       self._transcript_child('The diff adds {"findings": 3} to config.'))
            self.assertEqual(res["status"], "ok")
            self.assertIsNone(res["verdict"])

    def test_last_verdict_object_wins(self):
        with tempfile.TemporaryDirectory() as d:
            f, v = str(Path(d) / "findings.txt"), str(Path(d) / "verdict.json")
            first = '{"findings": [], "overall": "draft", "blocking_count": 0}'
            final = '{"findings": [], "overall": "final", "blocking_count": 2}'
            res = _run("--idle", "5", "--deadline", "10", "--findings", f,
                       "--result-mode", "stream-transcript", "--verdict-json", v,
                       "--", PY, "-c",
                       self._transcript_child(f"{first}\n\nOn reflection:\n\n{final}"))
            self.assertEqual(res["status"], "ok")
            self.assertEqual(json.loads(Path(v).read_text())["overall"], "final")

    def test_verdict_keys_absent_from_the_status_when_not_requested(self):
        # Strictly additive: a caller that never asks for a verdict sees the old
        # one-line status contract unchanged.
        with tempfile.TemporaryDirectory() as d:
            f = str(Path(d) / "findings.txt")
            res = _run("--idle", "5", "--deadline", "10", "--findings", f,
                       "--result-mode", "stream-transcript",
                       "--", PY, "-c", self._transcript_child("Findings prose."))
            self.assertEqual(res["status"], "ok")
            self.assertNotIn("verdict", res)
            self.assertNotIn("verdict_reason", res)

    def test_external_file_mode_verdict_is_read_from_the_findings_file(self):
        with tempfile.TemporaryDirectory() as d:
            f, v = str(Path(d) / "findings.txt"), str(Path(d) / "verdict.json")
            res = _run("--idle", "5", "--deadline", "10", "--findings", f,
                       "--result-mode", "external-file", "--verdict-json", v,
                       "--", PY, "-c",
                       f"import sys; open(sys.argv[1],'w').write({VERDICT!r})", f)
            self.assertEqual(res["status"], "ok")
            self.assertEqual(json.loads(Path(v).read_text())["blocking_count"], 0)

    def test_structured_output_on_the_terminal_event_is_found(self):
        # The load-bearing case, confirmed live: a runtime honoring an inline schema
        # flag returns the validated object on its terminal result event while its
        # assistant text stays PROSE. A transcript-only scan finds nothing there.
        with tempfile.TemporaryDirectory() as d:
            f, v = str(Path(d) / "findings.txt"), str(Path(d) / "verdict.json")
            child = (
                "import json; "
                "print(json.dumps({'type':'assistant','message':{'content':["
                "{'type':'text','text':'I read every hunk. Nothing is wrong.'}]}})); "
                "print(json.dumps({'type':'result','subtype':'success','is_error':False,"
                "'result':'I read every hunk. Nothing is wrong.',"
                "'structured_output':{'findings':[],'overall':'clean','blocking_count':0}}))"
            )
            res = _run("--idle", "5", "--deadline", "10", "--findings", f,
                       "--result-mode", "stream-transcript", "--verdict-json", v,
                       "--", PY, "-c", child)
            self.assertEqual(res["status"], "ok")
            # Prose narrative preserved in findings; object recovered from the event.
            self.assertEqual(Path(f).read_text().strip(), "I read every hunk. Nothing is wrong.")
            self.assertEqual(json.loads(Path(v).read_text())["overall"], "clean")

    def test_an_object_alone_with_no_prose_is_still_a_completed_review(self):
        """Under an inline schema flag the runtime sometimes answers with the validated
        object and no text block at all. The transcript is then empty, and refusing it as
        "no text output" reports a completed review as a failure while the verdict sits on
        the terminal event. The object is the whole reply, so it is what gets published;
        only a run with neither prose nor an object produced no output."""
        with tempfile.TemporaryDirectory() as d:
            f, v = str(Path(d) / "findings.txt"), str(Path(d) / "verdict.json")
            child = (
                "import json; "
                "print(json.dumps({'type':'result','subtype':'success','is_error':False,"
                "'result':'',"
                "'structured_output':{'findings':[],'overall':'clean','blocking_count':0}}))"
            )
            res = _run("--idle", "5", "--deadline", "10", "--findings", f,
                       "--result-mode", "stream-transcript", "--verdict-json", v,
                       "--", PY, "-c", child)
            self.assertEqual(res["status"], "ok", res)
            self.assertEqual(json.loads(Path(f).read_text())["overall"], "clean")
            self.assertEqual(json.loads(Path(v).read_text())["overall"], "clean")
            bare = ("import json; print(json.dumps({'type':'result','subtype':'success',"
                    "'is_error':False,'result':''}))")
            f2, v2 = str(Path(d) / "findings2.txt"), str(Path(d) / "verdict2.json")
            res = _run("--idle", "5", "--deadline", "10", "--findings", f2,
                       "--result-mode", "stream-transcript", "--verdict-json", v2,
                       "--", PY, "-c", bare)
            self.assertEqual(res["status"], "error")
            self.assertEqual(res["reason"], "reviewer produced no text output")

    def test_terminal_result_payload_is_used_when_structured_output_is_absent(self):
        with tempfile.TemporaryDirectory() as d:
            f, v = str(Path(d) / "findings.txt"), str(Path(d) / "verdict.json")
            child = (
                "import json; "
                "print(json.dumps({'type':'assistant','message':{'content':["
                "{'type':'text','text':'Narrative only.'}]}})); "
                f"print(json.dumps({{'type':'result','subtype':'success','is_error':False,"
                f"'result':{VERDICT!r}}}))"
            )
            res = _run("--idle", "5", "--deadline", "10", "--findings", f,
                       "--result-mode", "stream-transcript", "--verdict-json", v,
                       "--", PY, "-c", child)
            self.assertEqual(res["status"], "ok")
            self.assertEqual(Path(f).read_text().strip(), "Narrative only.")
            self.assertEqual(json.loads(Path(v).read_text())["overall"], "clean")

    def test_enforced_object_beats_anything_merely_typed_in_the_transcript(self):
        with tempfile.TemporaryDirectory() as d:
            f, v = str(Path(d) / "findings.txt"), str(Path(d) / "verdict.json")
            typed = '{"findings": [], "overall": "typed", "blocking_count": 0}'
            child = (
                "import json; "
                f"print(json.dumps({{'type':'assistant','message':{{'content':["
                f"{{'type':'text','text':{typed!r}}}]}}}})); "
                "print(json.dumps({'type':'result','subtype':'success','is_error':False,"
                "'structured_output':{'findings':[],'overall':'enforced','blocking_count':0}}))"
            )
            _run("--idle", "5", "--deadline", "10", "--findings", f,
                 "--result-mode", "stream-transcript", "--verdict-json", v,
                 "--", PY, "-c", child)
            self.assertEqual(json.loads(Path(v).read_text())["overall"], "enforced")

    def test_wrong_blocking_count_is_corrected_from_the_findings(self):
        # A gate acts on blocking_count, so a miscount would under-gate a merge. The
        # count is DERIVED data, so it is recomputed rather than published wrong with
        # a warning nobody has to read. The correction is reported, not silent.
        with tempfile.TemporaryDirectory() as d:
            f, v = str(Path(d) / "findings.txt"), str(Path(d) / "verdict.json")
            wrong = json.dumps({
                "findings": [
                    {"file": "a.py", "line": 1, "severity": "blocker",
                     "summary": "s", "failure_scenario": "x"},
                    {"file": "b.py", "line": 2, "severity": "nit",
                     "summary": "s", "failure_scenario": "x"},
                ],
                "overall": "bad", "blocking_count": 0,
            })
            res = _run("--idle", "5", "--deadline", "10", "--findings", f,
                       "--result-mode", "stream-transcript", "--verdict-json", v,
                       "--", PY, "-c", self._transcript_child(wrong))
            self.assertEqual(res["status"], "ok")
            self.assertEqual(res["verdict"], v)  # still published
            self.assertIn("corrected to 1", res["verdict_reason"])
            # The file a gate reads carries the CORRECT count, not the claimed one.
            self.assertEqual(json.loads(Path(v).read_text())["blocking_count"], 1)

    def test_off_enum_severity_never_lowers_the_claimed_count(self):
        # Regression, and it was a merge-the-broken-thing bug: on the unenforced rungs
        # the severity string is unvalidated model output, so an exact-match recount
        # rewrote a two-blocker review to blocking_count 0 and a gate read it as clean.
        with tempfile.TemporaryDirectory() as d:
            f, v = str(Path(d) / "findings.txt"), str(Path(d) / "verdict.json")
            off_enum = json.dumps({
                "findings": [
                    {"file": "a.py", "line": 1, "severity": "Blocker",
                     "summary": "s", "failure_scenario": "x"},
                    {"file": "b.py", "line": 2, "severity": "critical",
                     "summary": "s", "failure_scenario": "x"},
                ],
                "overall": "bad", "blocking_count": 2,
            })
            res = _run("--idle", "5", "--deadline", "10", "--findings", f,
                       "--result-mode", "stream-transcript", "--verdict-json", v,
                       "--", PY, "-c", self._transcript_child(off_enum))
            self.assertEqual(res["status"], "ok")
            written = json.loads(Path(v).read_text())
            self.assertGreaterEqual(written["blocking_count"], 2)  # never lowered
            self.assertIn("unrecognized severity", res["verdict_reason"])
            self.assertIn("critical", res["verdict_reason"])
            # The sentence has to describe the rule the code implements. Claiming each
            # unrecognized finding "was counted as blocking" is the rule the docstring
            # records as tried and rejected, and it contradicts its own number whenever the
            # floor lands below the count of unknowns: a human reconciling "2 blocking"
            # against four findings fixed two and merged with the other two unaddressed.
            self.assertNotIn("each was counted as blocking", res["verdict_reason"])
            self.assertIn("NOT in it", res["verdict_reason"])

    def test_the_off_enum_note_never_claims_more_than_the_number_it_published(self):
        """Two unknowns, floor 1: a sentence reading "2 ... each was counted" beside a
        published 1 claims more than the number it published. Asserted on the reconciler directly — the floor only lands below
        the number of unknowns when the claim and the real count are both under it."""
        verdict = {
            "findings": [
                {"file": "a.py", "line": 1, "severity": "critical",
                 "summary": "s", "failure_scenario": "x"},
                {"file": "b.py", "line": 2, "severity": "critical",
                 "summary": "s", "failure_scenario": "x"},
            ],
            "overall": "bad", "blocking_count": 0,
        }
        reason = review_runner._reconcile_blocking_count(verdict)
        self.assertEqual(verdict["blocking_count"], 1)
        self.assertIn("floored at 1", reason)
        self.assertNotIn("each was counted", reason)

    def test_severity_case_and_padding_are_tolerated_in_the_recount(self):
        with tempfile.TemporaryDirectory() as d:
            f, v = str(Path(d) / "findings.txt"), str(Path(d) / "verdict.json")
            padded = json.dumps({
                "findings": [
                    {"file": "a.py", "line": 1, "severity": " MAJOR ",
                     "summary": "s", "failure_scenario": "x"},
                    {"file": "b.py", "line": 2, "severity": "Nit",
                     "summary": "s", "failure_scenario": "x"},
                ],
                "overall": "one major", "blocking_count": 1,
            })
            res = _run("--idle", "5", "--deadline", "10", "--findings", f,
                       "--result-mode", "stream-transcript", "--verdict-json", v,
                       "--", PY, "-c", self._transcript_child(padded))
            self.assertEqual(res["status"], "ok")
            self.assertIsNone(res["verdict_reason"])  # recognized, and already correct
            self.assertEqual(json.loads(Path(v).read_text())["blocking_count"], 1)

    def test_unknown_severity_still_raises_an_understated_count(self):
        # Fail closed in the other direction too: claimed 0, one real blocker plus one
        # unknown -> the count must rise to the derivable minimum, not stay at 0.
        with tempfile.TemporaryDirectory() as d:
            f, v = str(Path(d) / "findings.txt"), str(Path(d) / "verdict.json")
            mixed = json.dumps({
                "findings": [
                    {"file": "a.py", "line": 1, "severity": "blocker",
                     "summary": "s", "failure_scenario": "x"},
                    {"file": "b.py", "line": 2, "severity": "showstopper",
                     "summary": "s", "failure_scenario": "x"},
                ],
                "overall": "bad", "blocking_count": 0,
            })
            _run("--idle", "5", "--deadline", "10", "--findings", f,
                 "--result-mode", "stream-transcript", "--verdict-json", v,
                 "--", PY, "-c", self._transcript_child(mixed))
            self.assertEqual(json.loads(Path(v).read_text())["blocking_count"], 1)

    def test_a_verdict_whose_only_finding_is_critical_is_not_published_as_clean(self):
        # `critical` is not in the enum, and it is the obvious word for a model to reach
        # for. Both the derived count and the claimed one were 0, so max() published 0 and a
        # gate merged a review that had just reported a critical finding. The rule is
        # minimal: an underivable count must not be zero.
        with tempfile.TemporaryDirectory() as d:
            f, v = str(Path(d) / "findings.txt"), str(Path(d) / "verdict.json")
            only_critical = json.dumps({
                "findings": [
                    {"file": "a.py", "line": 1, "severity": "critical",
                     "summary": "s", "failure_scenario": "x"},
                ],
                "overall": "bad", "blocking_count": 0,
            })
            _run("--idle", "5", "--deadline", "10", "--findings", f,
                 "--result-mode", "stream-transcript", "--verdict-json", v,
                 "--", PY, "-c", self._transcript_child(only_critical))
            self.assertGreaterEqual(
                json.loads(Path(v).read_text())["blocking_count"], 1,
                "a review reporting a critical finding published as machine-clean")

    def test_a_genuinely_clean_review_still_reports_zero(self):
        # Anti-vacuity: refusing to publish 0 at all would satisfy the test above.
        with tempfile.TemporaryDirectory() as d:
            f, v = str(Path(d) / "findings.txt"), str(Path(d) / "verdict.json")
            clean = json.dumps({
                "findings": [
                    {"file": "a.py", "line": 1, "severity": "nit",
                     "summary": "s", "failure_scenario": "x"},
                ],
                "overall": "fine", "blocking_count": 0,
            })
            _run("--idle", "5", "--deadline", "10", "--findings", f,
                 "--result-mode", "stream-transcript", "--verdict-json", v,
                 "--", PY, "-c", self._transcript_child(clean))
            self.assertEqual(json.loads(Path(v).read_text())["blocking_count"], 0)

    def test_deeply_nested_json_does_not_convert_a_good_review_into_an_error(self):
        # raw_decode recurses once per nesting level, and RecursionError is not a
        # ValueError - so it raised straight past the scan loop, AFTER the supervision
        # loop had exited with every timeout already spent.
        deep = "{" * 6000 + "}" * 6000
        text = deep + "\n" + json.dumps(
            {"findings": [], "overall": "fine", "blocking_count": 0})
        self.assertIsNotNone(review_runner._scan_verdict(text))

    def test_the_scan_stays_fast_on_a_large_transcript(self):
        # It ran once per `{` in a document that reaches megabytes, and nothing bounded
        # it: --idle and --deadline both belong to the supervision loop, which has
        # already exited by then. Measured at 7.9s before, 0.05s after, on this input.
        verdict = json.dumps({"findings": [], "overall": "fine", "blocking_count": 0})
        big = ("prose {not a verdict} more prose " * 40000) + verdict
        started = time.monotonic()
        self.assertIsNotNone(review_runner._scan_verdict(big))
        self.assertLess(time.monotonic() - started, 2.0,
                        "the verdict scan is quadratic again")

    def test_the_last_verdict_still_wins(self):
        # Anti-regression: scanning from the end must not change which object is chosen.
        early = json.dumps({"findings": [], "overall": "early", "blocking_count": 0})
        final = json.dumps({"findings": [], "overall": "final", "blocking_count": 0})
        self.assertEqual(
            review_runner._scan_verdict(f"{early} ... prose ... {final}")["overall"],
            "final")

    @unittest.skipUnless(
        os.name == "posix",
        "POSIX only, and skipped rather than weakened. The fix reaps the child's PROCESS "
        "GROUP, and Windows has no group to reach — _terminate's own docstring declines "
        "to over-claim containment there for the same reason. A descendant that outlives "
        "the reviewer on Windows still holds the pipe until it exits; that limitation is "
        "unchanged by this commit and is documented at _reap_group. Asserting the POSIX "
        "outcome on Windows would fail for a real reason, and relaxing the assertion so "
        "both platforms pass would cost the coverage on the platform that HAS the fix.")
    def test_a_grandchild_holding_the_pipe_does_not_cost_the_review(self):
        # The reviewer exits CLEANLY and leaves a helper holding the inherited stdout pipe,
        # so the reader never sees EOF. _terminate cannot help — it returns early when the
        # child is already gone, which is this case exactly — so a supervisor that only
        # waits spends the whole 30s drain and reports the review with its tail missing.
        verdict = json.dumps({"findings": [], "overall": "fine", "blocking_count": 0})
        child = (
            "import json,subprocess,sys\n"
            f"v={verdict!r}\n"
            "sys.stdout.write(json.dumps({'type':'assistant',"
            "'message':{'content':[{'type':'text','text':v}]}})+chr(10))\n"
            "sys.stdout.write(json.dumps({'type':'result','subtype':'success',"
            "'is_error':False,'result':v})+chr(10))\n"
            "sys.stdout.flush()\n"
            "subprocess.Popen([sys.executable,'-c','import time; time.sleep(120)'])\n"
            "sys.exit(0)\n"
        )
        with tempfile.TemporaryDirectory() as d:
            f, v = str(Path(d) / "findings.txt"), str(Path(d) / "verdict.json")
            started = time.monotonic()
            out = _run("--idle", "30", "--deadline", "45", "--findings", f,
                       "--result-mode", "stream-transcript", "--verdict-json", v,
                       "--", PY, "-c", child)
            elapsed = time.monotonic() - started
        self.assertEqual(out["status"], "ok",
                         "a completed review was lost to a lingering grandchild")
        self.assertLess(elapsed, 20,
                        "the supervisor waited out the full drain instead of reaping "
                        "the group the exited child left behind")

    def test_verdict_with_a_non_list_findings_field_is_not_adopted(self):
        # A gate ITERATES findings; publishing a number there would crash it or
        # silently gate on nothing.
        with tempfile.TemporaryDirectory() as d:
            f, v = str(Path(d) / "findings.txt"), str(Path(d) / "verdict.json")
            res = _run("--idle", "5", "--deadline", "10", "--findings", f,
                       "--result-mode", "stream-transcript", "--verdict-json", v,
                       "--", PY, "-c", self._transcript_child(
                           '{"findings": 3, "overall": "x", "blocking_count": 0}'))
            self.assertEqual(res["status"], "ok")
            self.assertIsNone(res["verdict"])
            self.assertFalse(Path(v).exists())

    def test_a_previous_verdict_can_never_be_read_as_this_runs_output(self):
        """The invariant the old up-front `unlink` defended, kept a different way.

        A gate must never act on run 1's verdict believing it describes run 2. The `unlink`
        bought that by deleting the path before every early return, and needed a
        tracked-file guard bolted on so it would not delete source. Refusing the collision
        outright is stronger: run 2 never starts, so it cannot report anything at all.
        """
        with tempfile.TemporaryDirectory() as d:
            f, v = str(Path(d) / "findings.txt"), Path(d) / "verdict.json"
            v.write_text('{"findings": [], "overall": "STALE", "blocking_count": 0}')
            res = _run("--idle", "5", "--deadline", "10", "--findings", f,
                       "--result-mode", "external-file", "--verdict-json", str(v),
                       "--", PY, "-c", "pass", "⟪schema_json⟫")  # would error on the marker
            self.assertEqual(res["status"], "error")
            self.assertIn("already exists", res["reason"])
            # The run never began, so nothing it emitted refers to that file — and the
            # file itself is untouched, because it is not this run's to remove.
            self.assertNotIn("verdict", {k: v for k, v in res.items() if v})
            self.assertEqual(json.loads(v.read_text())["overall"], "STALE")

    def test_the_refusal_precedes_every_other_check(self):
        """`--idle 0` returning above the invalidation loop is the ordering this pins.

        The collision refusal has to come first for the same reason the `unlink` did: a
        check that returns earlier would let a run report on a path it never owned.
        """
        with tempfile.TemporaryDirectory() as d:
            f, v = Path(d) / "findings.txt", Path(d) / "verdict.json"
            f.write_text("STALE findings from the last review")
            v.write_text('{"findings": [], "overall": "STALE clean", "blocking_count": 0}')
            res = _run("--idle", "0", "--deadline", "1800", "--findings", str(f),
                       "--result-mode", "stream-transcript", "--verdict-json", str(v),
                       "--", PY, "-c", "pass")
            self.assertEqual(res["status"], "error")
            self.assertIn("already exists", res["reason"])
            self.assertNotIn("--idle", res["reason"])
            self.assertTrue(f.exists(), "a refused invocation deleted someone's file")
            self.assertTrue(v.exists())

    def test_a_positive_claim_is_never_lowered_to_zero(self):
        """The shape the docstring's "FAILS CLOSED" did not cover, and the one that ships.

        `blocking_count: 3` with an EMPTY findings array is what the unenforced runtime
        produces: the model totals its prose and then omits the structured list. Every
        severity present is recognized (there are none), so the off-enum floor never fires,
        `claimed != counted` fires instead, and three blockers were republished as
        `blocking_count: 0` — machine-clean, for a gate that reads the number.
        """
        with tempfile.TemporaryDirectory() as d:
            f, v = str(Path(d) / "findings.txt"), str(Path(d) / "verdict.json")
            contradictory = json.dumps(
                {"findings": [], "overall": "3 blockers", "blocking_count": 3})
            res = _run("--idle", "5", "--deadline", "10", "--findings", f,
                       "--result-mode", "stream-transcript", "--verdict-json", v,
                       "--", PY, "-c", self._transcript_child(contradictory))
            self.assertEqual(res["status"], "ok")
            self.assertGreaterEqual(
                json.loads(Path(v).read_text())["blocking_count"], 1,
                "a verdict claiming 3 blockers was published as machine-clean")
            self.assertIn("contradicts itself", res["verdict_reason"])
            self.assertIn("gate on the findings", res["verdict_reason"])

    def test_the_same_holds_when_every_finding_is_below_blocking(self):
        # Not only the empty-array shape: one `nit` beside `blocking_count: 2` is the
        # same contradiction — the reviewer counted something it did not list at that
        # severity, and zero is the one answer that cannot be right.
        with tempfile.TemporaryDirectory() as d:
            f, v = str(Path(d) / "findings.txt"), str(Path(d) / "verdict.json")
            contradictory = json.dumps({
                "findings": [
                    {"file": "a.py", "line": 1, "severity": "nit",
                     "summary": "s", "failure_scenario": "x"},
                ],
                "overall": "2 blockers", "blocking_count": 2,
            })
            res = _run("--idle", "5", "--deadline", "10", "--findings", f,
                       "--result-mode", "stream-transcript", "--verdict-json", v,
                       "--", PY, "-c", self._transcript_child(contradictory))
            self.assertGreaterEqual(
                json.loads(Path(v).read_text())["blocking_count"], 1)
            self.assertIn("contradicts itself", res["verdict_reason"])

    def test_a_positive_claim_may_still_be_lowered_to_another_positive(self):
        """Anti-vacuity: only the lowering to ZERO is refused, not lowering at all."""
        with tempfile.TemporaryDirectory() as d:
            f, v = str(Path(d) / "findings.txt"), str(Path(d) / "verdict.json")
            overcount = json.dumps({
                "findings": [
                    {"file": "a.py", "line": 1, "severity": "blocker",
                     "summary": "s", "failure_scenario": "x"},
                ],
                "overall": "bad", "blocking_count": 4,
            })
            res = _run("--idle", "5", "--deadline", "10", "--findings", f,
                       "--result-mode", "stream-transcript", "--verdict-json", v,
                       "--", PY, "-c", self._transcript_child(overcount))
            self.assertEqual(json.loads(Path(v).read_text())["blocking_count"], 1)
            self.assertIn("corrected to 1", res["verdict_reason"])
            self.assertNotIn("contradicts itself", res["verdict_reason"])

    def test_a_zero_claim_over_zero_findings_is_still_published_as_zero(self):
        """Anti-vacuity: the floor must not fire on a review that really is clean."""
        with tempfile.TemporaryDirectory() as d:
            f, v = str(Path(d) / "findings.txt"), str(Path(d) / "verdict.json")
            clean = json.dumps(
                {"findings": [], "overall": "clean", "blocking_count": 0})
            res = _run("--idle", "5", "--deadline", "10", "--findings", f,
                       "--result-mode", "stream-transcript", "--verdict-json", v,
                       "--", PY, "-c", self._transcript_child(clean))
            self.assertEqual(json.loads(Path(v).read_text())["blocking_count"], 0)
            self.assertIsNone(res["verdict_reason"])

    def test_a_missing_blocking_count_over_no_findings_is_still_zero(self):
        """Anti-vacuity: an ABSENT claim is not a positive claim."""
        with tempfile.TemporaryDirectory() as d:
            f, v = str(Path(d) / "findings.txt"), str(Path(d) / "verdict.json")
            no_count = json.dumps(
                {"findings": [], "overall": "clean", "blocking_count": None})
            _run("--idle", "5", "--deadline", "10", "--findings", f,
                 "--result-mode", "stream-transcript", "--verdict-json", v,
                 "--", PY, "-c", self._transcript_child(no_count))
            self.assertEqual(json.loads(Path(v).read_text())["blocking_count"], 0)

    def test_consistent_blocking_count_reports_no_warning(self):
        with tempfile.TemporaryDirectory() as d:
            f, v = str(Path(d) / "findings.txt"), str(Path(d) / "verdict.json")
            good = json.dumps({
                "findings": [
                    {"file": "a.py", "line": 1, "severity": "major",
                     "summary": "s", "failure_scenario": "x"},
                ],
                "overall": "one major", "blocking_count": 1,
            })
            res = _run("--idle", "5", "--deadline", "10", "--findings", f,
                       "--result-mode", "stream-transcript", "--verdict-json", v,
                       "--", PY, "-c", self._transcript_child(good))
            self.assertIsNone(res["verdict_reason"])

    def test_a_review_with_no_verdict_object_writes_no_verdict_file(self):
        # A gate reads this path, so a file must exist there only when a verdict was
        # actually extracted. (The stale-file half of this is now impossible by
        # construction: a run whose verdict path already exists refuses to start.)
        with tempfile.TemporaryDirectory() as d:
            f, v = str(Path(d) / "findings.txt"), Path(d) / "verdict.json"
            res = _run("--idle", "5", "--deadline", "10", "--findings", f,
                       "--result-mode", "stream-transcript", "--verdict-json", str(v),
                       "--", PY, "-c", self._transcript_child("Prose only, no object."))
            self.assertEqual(res["status"], "ok")
            self.assertIsNone(res["verdict"])
            self.assertFalse(v.exists())

    def test_no_verdict_is_written_when_the_review_itself_failed(self):
        with tempfile.TemporaryDirectory() as d:
            f, v = str(Path(d) / "findings.txt"), str(Path(d) / "verdict.json")
            res = _run("--idle", "5", "--deadline", "10", "--findings", f,
                       "--result-mode", "external-file", "--verdict-json", v,
                       "--", PY, "-c", "pass")
            self.assertEqual(res["status"], "error")
            self.assertFalse(Path(v).exists())


class ReviewerTextThatCannotBeEncoded(unittest.TestCase):
    """One unpaired surrogate is enough to discard a COMPLETED cross-model review.

    JSON permits a lone ``\\ud800`` escape and Python's decoder produces the lone surrogate
    faithfully, so it reaches the transcript intact. `write_text` then raises
    `UnicodeEncodeError` — a `ValueError`, not an `OSError` — so the routing block's
    `except OSError` did not see it and it escaped `run()` entirely. The caller fell open to
    a same-model reviewer: the one trade the skill says must never be made, over a single
    byte of prose.

    The two reads on this path were already tolerant. The writes were not.
    """

    SURROGATE = "\ud800"

    # `ascii()`, not `repr()`: the child source travels as ARGV, and under a C locale Python
    # encodes argv with the ASCII filesystem encoding — so a literal `é` in the source string
    # raises inside `Popen` before the test can test anything. `ascii()` escapes every
    # non-ASCII character to a sequence that evaluates back to the same string.

    @staticmethod
    def _codex_child(text):
        return (
            "import json; "
            f"text = {ascii(text)}; "
            "print(json.dumps({'type':'item.completed',"
            "'item':{'type':'agent_message','text':text}})); "
            "print(json.dumps({'type':'turn.completed'}))"
        )

    @staticmethod
    def _result_event_child(text):
        return (
            "import json; "
            f"text = {ascii(text)}; "
            "print(json.dumps({'type':'result','subtype':'success',"
            "'is_error':False,'result':text}))"
        )

    def test_the_transcript_mode_still_reports_a_successful_review(self):
        narrative = f"blocker at util.py:31 {self.SURROGATE} truncated escape"
        with tempfile.TemporaryDirectory() as d:
            f = str(Path(d) / "findings.txt")
            res = _run("--idle", "5", "--deadline", "10", "--findings", f,
                       "--result-mode", "stream-transcript",
                       "--", PY, "-c", self._codex_child(narrative))
            self.assertEqual(res["status"], "ok", res.get("reason"))
            written = Path(f).read_text(encoding="utf-8")
            self.assertIn("blocker at util.py:31", written)
            self.assertIn("truncated escape", written)
            self.assertNotIn(self.SURROGATE, written)

    def test_the_result_event_mode_survives_it_too(self):
        """The sibling write, at the other result mode — same bug, same fix."""
        payload = f"one major {self.SURROGATE} finding"
        with tempfile.TemporaryDirectory() as d:
            f = str(Path(d) / "findings.txt")
            res = _run("--idle", "5", "--deadline", "10", "--findings", f,
                       "--result-mode", "stream-json-result-event",
                       "--", PY, "-c", self._result_event_child(payload))
            self.assertEqual(res["status"], "ok", res.get("reason"))
            self.assertIn("one major", Path(f).read_text(encoding="utf-8"))

    def test_the_structured_verdict_still_lands_beside_it(self):
        """The whole product survives, not merely the status line."""
        verdict = json.dumps({
            "findings": [
                {"file": "util.py", "line": 31, "severity": "blocker",
                 "summary": "s", "failure_scenario": "x"},
            ],
            "overall": "bad", "blocking_count": 1,
        })
        narrative = f"I read every hunk {self.SURROGATE}\n\n{verdict}"
        with tempfile.TemporaryDirectory() as d:
            f, v = str(Path(d) / "findings.txt"), str(Path(d) / "verdict.json")
            res = _run("--idle", "5", "--deadline", "10", "--findings", f,
                       "--result-mode", "stream-transcript", "--verdict-json", v,
                       "--", PY, "-c", self._codex_child(narrative))
            self.assertEqual(res["status"], "ok", res.get("reason"))
            self.assertEqual(res["verdict"], v)
            self.assertEqual(json.loads(Path(v).read_text())["blocking_count"], 1)

    def test_an_ordinary_review_is_written_byte_for_byte(self):
        """Anti-vacuity: `errors="replace"` must not mangle text that encodes fine."""
        narrative = "a blocker at café.py:9 — the em dash and the é both survive"
        with tempfile.TemporaryDirectory() as d:
            f = str(Path(d) / "findings.txt")
            _run("--idle", "5", "--deadline", "10", "--findings", f,
                 "--result-mode", "stream-transcript",
                 "--", PY, "-c", self._codex_child(narrative))
            self.assertEqual(Path(f).read_text(encoding="utf-8"), narrative + "\n")


_RUNNER = _ENGINE_DIR / "review_runner.py"

# A stdout that cannot represent the ⟪…⟫ markers argparse interpolates into `--schema`'s
# help text. Two layers, and the difference between them matters:
#
#   PYTHONIOENCODING=ascii  FORCES it, on every platform. Without this the test is not a
#     test on Windows — `PYTHONUTF8=0` plus the locale variables leaves stdout UTF-8 on a
#     runner whose active code page is already UTF-8, so reverting the production pin would
#     leave this green on the one platform whose console encoding it exists for.
#   PYTHONUTF8=0 / LC_ALL / LANG  is the CI encoding proxy — additional coverage of the
#     real-world path, not the guarantee.
_ASCII_PROXY_ENV = {
    "PYTHONIOENCODING": "ascii",
    "PYTHONUTF8": "0",
    "LC_ALL": "C",
    "LANG": "C",
}


def _ascii_console_env():
    env = dict(os.environ)
    env.update(_ASCII_PROXY_ENV)  # overwrites an inherited PYTHONIOENCODING, deliberately
    return env


class HelpOnANonUtf8Console(unittest.TestCase):
    """``--help`` is documented as an ordinary argparse path; it must stay one.

    Run as a REAL subprocess, because the defect is in how the interpreter opened
    ``sys.stdout`` — an in-process test inherits the suite's already-UTF-8 stream. The
    offending characters are not literals at the help-text lines: they reach it through
    ``SCHEMA_PATH_MARKER``/``SCHEMA_JSON_MARKER``, built at import. The raise lands *inside*
    ``parse_args``, which is why the stream has to be pinned before it.

    The child's stdout encoding is FORCED rather than inferred from a locale — see
    ``_ASCII_PROXY_ENV``. A locale-only proxy is a no-op on a UTF-8 Windows console.
    """

    def test_help_prints_usage_and_exits_zero(self):
        env = _ascii_console_env()
        # Pin the width argparse formats to. It wraps with `textwrap`, which breaks long
        # words, so a host with a narrow COLUMNS could split the marker the assertion
        # below looks for — a failure about terminal size, not about encoding.
        env["COLUMNS"] = "100"
        proc = subprocess.run(
            [sys.executable, str(_RUNNER), "--help"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            stdin=subprocess.DEVNULL, env=env, timeout=60,
        )
        stdout = proc.stdout.decode("utf-8", "replace")
        stderr = proc.stderr.decode("utf-8", "replace")
        self.assertEqual(
            proc.returncode, 0,
            f"--help failed on an ASCII stdout:\nstdout={stdout}\nstderr={stderr}")
        self.assertNotIn("UnicodeEncodeError", stdout + stderr)
        self.assertIn("usage:", stdout)
        # Pinned to UTF-8 rather than merely error-replaced: a caller reading the help to
        # learn the marker spelling gets the marker, not two question marks.
        self.assertIn("⟪schema_path⟫", stdout)

    def test_an_invalid_argument_still_reaches_the_invalid_invocation_path(self):
        # Pins the OTHER argparse exit: a bad argument must still land on the runner's own
        # `invalid runner invocation` contract under an ASCII console.
        #
        # It does NOT prove the sys.stderr half of the pin, and must not be read as doing
        # so. argparse ESCAPES a non-ASCII argument value before writing, so `--idle ⟪`
        # reaches stderr as ASCII text and the stream's encoding never comes into it; the
        # usage block printed on error carries metavars only, never the help text holding
        # the ⟪…⟫ markers. And a non-ASCII argv element cannot even be passed: under
        # LC_ALL=C — how CI runs this suite — `subprocess` encodes argv with the ASCII
        # filesystem encoding and `_fork_exec` raises before the child starts. So the value
        # here is plain ASCII and the stderr pin stays defensive rather than exercised.
        env = _ascii_console_env()
        env["COLUMNS"] = "100"
        proc = subprocess.run(
            [sys.executable, str(_RUNNER), "--idle", "not-a-number", "--", PY, "-c", "pass"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            stdin=subprocess.DEVNULL, env=env, timeout=60,
        )
        stdout = proc.stdout.decode("utf-8", "replace")
        stderr = proc.stderr.decode("utf-8", "replace")
        combined = stdout + stderr
        self.assertNotIn(
            "UnicodeEncodeError", combined,
            f"argparse's stderr error path crashed on an ASCII console:\n{combined}")
        # The supervisor catches argparse's SystemExit and answers with its own contract,
        # so the intended landing is `invalid runner invocation` — NOT argparse's exit 2,
        # and NOT the outer unexpected-error path the missing pin would divert it to.
        self.assertNotIn("unexpected:", combined)
        self.assertEqual(proc.returncode, 1, f"expected the invalid-invocation path:\n{combined}")
        status = json.loads(stdout.strip().splitlines()[0])
        self.assertEqual(status["status"], "error")
        self.assertEqual(status["reason"], "invalid runner invocation")
        # argparse's own diagnostic still reaches stderr, naming the flag it rejected.
        self.assertIn("--idle", stderr)

    def test_a_supervised_run_still_emits_its_status_line(self):
        # The pin must not disturb the one-line JSON contract the caller parses.
        with tempfile.TemporaryDirectory() as d:
            f = str(Path(d) / "findings.txt")
            proc = subprocess.run(
                [sys.executable, str(_RUNNER), "--idle", "10", "--deadline", "30",
                 "--findings", f, "--result-mode", "external-file",
                 "--", PY, "-c", "import sys; open(sys.argv[1],'w').write('VERDICT')", f],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                stdin=subprocess.DEVNULL, env=_ascii_console_env(), timeout=60,
            )
            status = json.loads(proc.stdout.decode("utf-8").strip().splitlines()[-1])
            self.assertEqual(status["status"], "ok")
            self.assertEqual(proc.returncode, 0)


class DisplayDecoderChunks(unittest.TestCase):
    """The decode contract at PRESCRIBED boundaries — the scheduler is not in the loop.

    Each case is a split the reader will genuinely see: ``os.read`` returns whatever has
    arrived, so a boundary lands wherever the child's flush (or the 64 KiB cap) put it.
    Driving the real object with chosen chunks is deterministic on every platform, which
    racing a child against a reader thread is not.
    """

    def _feed(self, *chunks):
        """Every chunk in order, then the EOF flush — exactly what ``reader()`` does."""
        decoder = review_runner._display_decoder()
        out = "".join(decoder.decode(c) for c in chunks)
        return out + decoder.decode(b"", final=True)

    def test_a_three_byte_character_split_after_one_byte(self):
        self.assertEqual(self._feed(b"before\xe2", b"\x80\x94after"), "before—after")

    def test_a_three_byte_character_split_after_two_bytes(self):
        self.assertEqual(self._feed(b"before\xe2\x80", b"\x94after"), "before—after")

    def test_a_four_byte_character_split_across_three_chunks(self):
        # U+1F600: two of its four bytes arrive alone. Reviewers do write emoji.
        self.assertEqual(self._feed(b"a\xf0", b"\x9f", b"\x98\x80b"), "a\U0001F600b")

    def test_one_byte_at_a_time_still_reassembles(self):
        text = "— done ✓"
        self.assertEqual(self._feed(*[bytes([b]) for b in text.encode("utf-8")]), text)

    def test_a_truncated_final_character_is_flushed_as_a_replacement(self):
        # The decoder holds these bytes until the flush tells it the stream ended. Without
        # that flush the tail vanishes, with nothing in the log to say anything was lost.
        self.assertEqual(self._feed(b"tail\xe2"), "tail�")

    def test_an_undecodable_byte_is_replaced_rather_than_raising(self):
        # `errors="replace"`, not strict: the display path is best-effort, and a raise
        # inside the reader thread would take the JSONL capture down with it.
        self.assertEqual(self._feed(b"a\xffb"), "a�b")

    def test_the_case_still_discriminates_against_the_old_per_chunk_decode(self):
        # The regression, spelled out. Decoding each chunk independently turns ONE
        # character into three replacements; if these two ever agree, the split above has
        # stopped testing anything and the chunks need rewriting.
        chunks = (b"before\xe2", b"\x80\x94after")
        independent = "".join(c.decode("utf-8", "replace") for c in chunks)
        self.assertEqual(independent, "before���after")
        self.assertNotEqual(independent, self._feed(*chunks))


def reader_wiring(source: str, func: str = "_read_stdout") -> dict:
    """What the stdout reader does with the display decoder and the chunks it reads, by
    DATA FLOW.

    Nothing here is spelled as a name. The decoder is whatever ``_display_decoder()`` was
    assigned to; the chunk is whatever ``os.read(...)`` was assigned to. Rename either and
    every answer below is unchanged — which is the point. Comparing receiver names against a
    fixed set broke the suite on a rename while letting a per-chunk ``reset()`` pass.

    Returns, for the one function that matters:

    ``decoders``       every name the decoder reaches, aliases included — an ``alias =
                       display_decoder`` followed by ``alias.reset()`` is the regression
                       wearing a second name
    ``builds``         how many times ``_display_decoder()`` is called
    ``built_in_loop``  whether any of those calls sits inside a loop, i.e. per chunk
    ``chunks``         names bound to an ``os.read(...)`` result
    ``fed``            EVERY name bound to a read result is passed to that decoder's
                       ``decode`` — so a second read added and never fed is caught
    ``flushed``        ``decode(b"", final=True)`` is called on it for the EOF tail;
                       ``final=False`` does not count
    ``other_methods``  anything called on the decoder that is not ``decode`` — a
                       ``reset()`` between chunks throws away the held partial character
    ``chunk_decoded``  the chunk is a ``decode`` receiver itself, i.e. decoded alone

    ``raw`` and ``buf`` on the JSONL path are deliberately outside all of this: they are
    line-scoped by construction — the reader splits on ``b"\\n"`` before decoding — so a
    split inside a line never reaches them. The rule is about the chunk **as read**.
    """
    tree = ast.parse(source, filename=str(_RUNNER))
    found = [n for n in ast.walk(tree)
             if isinstance(n, ast.FunctionDef) and n.name == func]
    if len(found) != 1:
        raise AssertionError(f"expected exactly one `def {func}`, found {len(found)} — "
                             "these assertions would be reading an arbitrary one of them")
    fn = found[0]

    def _is_call_to(node, name):
        return (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                and node.func.id == name)

    def _is_call_to_attr(node, obj, attr):
        return (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr == attr and isinstance(node.func.value, ast.Name)
                and node.func.value.id == obj)

    def _bound_names(predicate):
        return {t.id for n in ast.walk(fn) if isinstance(n, ast.Assign)
                for t in n.targets if isinstance(t, ast.Name) and predicate(n.value)}

    decoders = _bound_names(lambda v: _is_call_to(v, "_display_decoder"))
    chunks = _bound_names(lambda v: _is_call_to_attr(v, "os", "read"))

    # Follow plain aliases to a fixed point, both sides. `alias = display_decoder` makes
    # `alias` the same object, and every question below has to be asked of it too.
    for names in (decoders, chunks):
        while True:
            grown = names | _bound_names(
                lambda v: isinstance(v, ast.Name) and v.id in names)
            if grown == names:
                break
            names |= grown

    builds = [n for n in ast.walk(fn) if _is_call_to(n, "_display_decoder")]
    in_loop = [c for loop in ast.walk(fn) if isinstance(loop, (ast.For, ast.While))
               for c in ast.walk(loop) if _is_call_to(c, "_display_decoder")]
    reads = [n for n in ast.walk(fn) if _is_call_to_attr(n, "os", "read")]

    on_decoder = [n for n in ast.walk(fn)
                  if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                  and isinstance(n.func.value, ast.Name) and n.func.value.id in decoders]
    # ATTRIBUTE ACCESS, not just calls: `reset = display_decoder.reset` followed by
    # `reset()` never appears as a call on the decoder, so a call-only scan sees nothing
    # while the reset happens on every chunk.
    reached = {n.attr for n in ast.walk(fn) if isinstance(n, ast.Attribute)
               and isinstance(n.value, ast.Name) and n.value.id in decoders}

    return {
        "decoders": decoders,
        "builds": len(builds),
        "built_in_loop": bool(in_loop),
        "chunks": chunks,
        "reads": len(reads),
        # Existential over the alias set, because every name in it holds the same bytes:
        # `chunk = data` then `decode(chunk)` feeds the read just as `decode(data)` does.
        # What stops a SECOND, unfed read hiding behind that is `reads`, asserted below —
        # one read site, so there is only ever one set of bytes to account for.
        "fed": bool(chunks) and any(
            c.func.attr == "decode"
            and any(isinstance(a, ast.Name) and a.id in chunks for a in c.args)
            for c in on_decoder),
        "flushed": any(
            c.func.attr == "decode"
            and any(k.arg == "final" and isinstance(k.value, ast.Constant)
                    and k.value.value is True for k in c.keywords)
            and any(isinstance(a, ast.Constant) and a.value == b"" for a in c.args)
            for c in on_decoder),
        "other_methods": reached - {"decode"},
        "chunk_decoded": any(
            isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
            and n.func.attr == "decode" and isinstance(n.func.value, ast.Name)
            and n.func.value.id in chunks
            for n in ast.walk(fn)),
    }


class DisplayDecoderWiring(unittest.TestCase):
    """`_Stream._read_stdout` must actually USE it. Every test above passes if it decodes chunks itself.

    Source assertions, in the shape `tests/test_skill_budgets.py` already uses for the
    budget check: the end-to-end pair below exercises the real path but depends on thread
    scheduling for its split, so the guarantee that the seam is wired in lives here.
    """

    def setUp(self):
        self.source = _RUNNER.read_text(encoding="utf-8")
        self.wiring = reader_wiring(self.source, func="_read_stdout")

    def test_the_decoder_is_built_once_and_never_inside_the_read_loop(self):
        self.assertEqual(
            self.wiring["builds"], 1,
            "the reader must build exactly one display decoder: none means it went "
            "back to decoding each chunk on its own, and more than one means a chunk is "
            "being decoded against a fresh decoder, which is the same bug spelled twice.")
        self.assertFalse(
            self.wiring["built_in_loop"],
            "a decoder built inside the read loop is a fresh decoder per chunk — the "
            "regression exactly, and one a call COUNT of 1 cannot see.")
        # Deliberately NOT `len(decoders) == 1`: `decoders` now holds every alias the
        # object reaches, and an alias is not by itself a defect. `builds` is the count
        # that answers "how many decoders exist".

    def test_the_stderr_drain_is_wired_the_same_way(self):
        """The second reader must not be exempt from the rule the first one has.

        `stderr` moved to its own pipe so a warning could no longer land mid-JSONL-line
        and split it. That put a SECOND decode loop in the file — and this class only ever
        checked `run`, so the new one could have re-introduced the per-chunk decoder the
        whole class exists to forbid, silently. It lives at module level rather than inside
        `run` precisely so both stay single-read, single-decoder, and both are now checked.
        """
        wiring = reader_wiring(self.source, func="_drain_stderr")
        self.assertEqual(wiring["builds"], 1,
                         "_drain_stderr must build exactly one display decoder")
        self.assertFalse(wiring["built_in_loop"],
                         "a decoder built inside the stderr read loop is a fresh decoder "
                         "per chunk — the same regression, in the newer of the two loops")
        self.assertEqual(wiring["reads"], 1, "one stderr read site")
        # `fed`, not `chunk_decoded`: the latter means "decoded ALONE, not split first",
        # which is False for `run` too — it splits on newlines before decoding. `fed` is
        # the property both loops must have, that the bytes read reach the decoder at all.
        self.assertTrue(wiring["fed"],
                        "the bytes read from stderr must be fed to that decoder")
        self.assertTrue(wiring["flushed"],
                        "stderr needs the same EOF flush as stdout: a child killed "
                        "mid-character otherwise loses its last bytes from the display log")
        self.assertFalse(wiring["other_methods"],
                         f"unexpected decoder methods: {wiring['other_methods']}")

    def test_the_chunk_the_reader_reads_is_fed_to_that_decoder(self):
        self.assertTrue(self.wiring["chunks"], "no `os.read(...)` result is bound at all")
        self.assertEqual(
            self.wiring["reads"], 1,
            "one read site, so one stream of bytes to account for. A second `os.read` "
            "whose result never reaches the decoder loses whatever it consumed, and no "
            "name-scoped check can tell the two apart once they share a name.")
        self.assertTrue(
            self.wiring["fed"],
            "the bytes `os.read` returned must reach the carried decoder; if they reach "
            "something else, every chunk test above is exercising an object the reader "
            "does not use.")

    def test_the_decoder_is_flushed_when_the_stream_ends(self):
        self.assertTrue(
            self.wiring["flushed"],
            "`decode(b\"\", final=True)` at EOF is what renders a sequence the child "
            "truncated; without it the tail silently disappears from the log.")

    def test_nothing_resets_the_decoder_between_chunks(self):
        self.assertEqual(
            self.wiring["other_methods"], set(),
            "`decode` is the only thing the reader may call on the carried decoder. A "
            "`reset()` throws away the incomplete character it is holding, which is the "
            "original defect with the fix still sitting there looking correct.")

    def test_the_raw_chunk_is_never_decoded_on_its_own(self):
        self.assertFalse(
            self.wiring["chunk_decoded"],
            "decoding the chunk directly is the regression: a boundary mid-character "
            "becomes replacement characters in the log a human reads.")

    def test_these_assertions_fail_on_the_mutations_they_exist_for(self):
        """Hand-mutate the engine and confirm each check answers the way it claims to.

        A source assertion nobody has watched fail is a source assertion that may be
        reading the wrong function. The rename case is here for the opposite reason: it
        must change *nothing*, and under the name-comparison version it changed everything.
        """
        src = _RUNNER.read_text(encoding="utf-8")
        feed = "self.write_display(display_decoder.decode(data))"
        self.assertIn(feed, src, "the mutation base moved; update these cases")
        indent = " " * 16

        with self.subTest("a reset between chunks"):
            m = reader_wiring(src.replace(feed, f"display_decoder.reset()\n{indent}{feed}"))
            self.assertEqual(m["other_methods"], {"reset"})

        with self.subTest("the same reset, reached through an alias"):
            m = reader_wiring(src.replace(
                feed, f"alias = display_decoder\n{indent}alias.reset()\n{indent}{feed}"))
            self.assertEqual(m["other_methods"], {"reset"})

        with self.subTest("a flush that does not finalize"):
            m = reader_wiring(src.replace('decode(b"", final=True)', 'decode(b"", final=False)'))
            self.assertFalse(m["flushed"])

        with self.subTest("a second read nobody feeds to the decoder"):
            m = reader_wiring(src.replace(
                feed, f"spare = os.read(fd, 8)\n{indent}{feed}"))
            self.assertEqual(m["reads"], 2)

        with self.subTest("the reset reached through a bound method, never called on it"):
            m = reader_wiring(src.replace(
                feed, f"reset = display_decoder.reset\n{indent}reset()\n{indent}{feed}"))
            self.assertEqual(m["other_methods"], {"reset"})

        with self.subTest("an alias of the chunk still counts as feeding it"):
            m = reader_wiring(src.replace(
                feed,
                f"same = data\n{indent}self.write_display(display_decoder.decode(same))"))
            self.assertTrue(m["fed"])

        with self.subTest("a decoder rebuilt per chunk"):
            m = reader_wiring(
                src.replace(feed, f"display_decoder = _display_decoder()\n{indent}{feed}"))
            self.assertEqual(m["builds"], 2)
            self.assertTrue(m["built_in_loop"])

        with self.subTest("the chunk decoded on its own"):
            m = reader_wiring(src.replace(
                feed, 'self.write_display(data.decode("utf-8", "replace"))'))
            self.assertTrue(m["chunk_decoded"])
            self.assertFalse(m["fed"])

        with self.subTest("locals renamed, and nothing else"):
            renamed = re.sub(r"\bdisplay_decoder\b", "dec", re.sub(r"\bdata\b", "chunk", src))
            m = reader_wiring(renamed)
            self.assertEqual(m["builds"], 1)
            self.assertFalse(m["built_in_loop"])
            self.assertTrue(m["fed"] and m["flushed"])
            self.assertEqual(m["other_methods"], set())
            self.assertFalse(m["chunk_decoded"])


def _split_writer_child():
    """A child whose one agent_message line is flushed in two writes that split an em dash.

    The supervisor reads with ``os.read``, so a chunk boundary is wherever the bytes
    happened to arrive — the 64 KiB cap and a flush boundary produce the identical split.
    The sleep is what makes the two arrivals separate reads rather than one coalesced
    buffer. It is scheduling-dependent and therefore NOT the guarantee: a reader starved
    for the whole window would read both halves at once and pass without exercising the
    split. ``DisplayDecoderChunks`` is where that guarantee lives; this pair exists to
    show the whole path — child, ``os.read``, log file — carrying a real one.
    """
    return (
        "import json, sys, time\n"
        "line = json.dumps({'type': 'item.completed', 'item': "
        "{'type': 'agent_message', 'text': 'before\\u2014after'}}, "
        "ensure_ascii=False).encode('utf-8') + b'\\n'\n"
        "cut = line.index('\\u2014'.encode('utf-8')) + 1\n"  # 1 of the em dash's 3 bytes
        "sys.stdout.buffer.write(line[:cut]); sys.stdout.buffer.flush()\n"
        "time.sleep(0.6)\n"
        "sys.stdout.buffer.write(line[cut:]); sys.stdout.buffer.flush()\n"
        "sys.stdout.buffer.write(json.dumps({'type': 'turn.completed'}).encode('utf-8'))\n"
        "sys.stdout.buffer.write(b'\\n'); sys.stdout.buffer.flush()\n"
    )


class DisplayLogDecoding(unittest.TestCase):
    """End to end: a real child, a real pipe, a real log file.

    The integration half of the pair. Determinism lives in ``DisplayDecoderChunks`` and the
    wiring in ``DisplayDecoderWiring``; what these add is that the bytes survive the whole
    path, including the log file's own encoding.
    """

    def _run_split(self, d):
        f, log = str(Path(d) / "findings.txt"), Path(d) / "display.log"
        res = _run("--idle", "5", "--deadline", "30", "--findings", f,
                   "--display", str(log), "--result-mode", "stream-transcript",
                   "--", PY, "-c", _split_writer_child())
        self.assertEqual(res["status"], "ok")
        return f, log

    def test_a_character_split_across_reads_is_intact_in_the_display_log(self):
        with tempfile.TemporaryDirectory() as d:
            _, log = self._run_split(d)
            text = log.read_text(encoding="utf-8")
            self.assertIn("before—after", text)
            self.assertNotIn("\ufffd", text)

    def test_the_jsonl_path_is_unaffected_by_the_same_split(self):
        # Green before and after: the JSONL reader accumulates raw BYTES and decodes per
        # line, so a split inside a line never reaches it. Asserted anyway, because the
        # display fix runs in the same reader and must leave this path alone.
        with tempfile.TemporaryDirectory() as d:
            f, _ = self._run_split(d)
            self.assertEqual(Path(f).read_text(encoding="utf-8").strip(), "before—after")

    def test_a_truncated_final_character_is_reported_not_dropped(self):
        # A child that dies mid-character leaves bytes an incremental decoder is holding.
        # They must be flushed at EOF: a silently dropped tail is worse than a visible
        # replacement character, because nothing in the log says anything was lost.
        with tempfile.TemporaryDirectory() as d:
            f, log = str(Path(d) / "findings.txt"), Path(d) / "display.log"
            child = ("import sys; open(sys.argv[1], 'w').write('V'); "
                     "sys.stdout.buffer.write(b'tail\\xe2'); sys.stdout.buffer.flush()")
            res = _run("--idle", "5", "--deadline", "30", "--findings", f,
                       "--display", str(log), "--result-mode", "external-file",
                       "--", PY, "-c", child, f)
            self.assertEqual(res["status"], "ok")
            self.assertIn("tail\ufffd", log.read_text(encoding="utf-8"))


class TheStatusLineSurvivesAnUndrainedStderr(unittest.TestCase):
    """The display log was closed on stdout's drain alone, under a live stderr thread.

    A reviewer that exits cleanly but leaves a helper holding the inherited stderr gives
    ``drained=True`` and ``err_drained=False``. Closing the shared handle there turned
    ``_drain_stderr``'s next write into an uncaught ``ValueError``, and the thread traceback
    printed AHEAD of the JSON status line. A caller capturing with ``2>&1`` — the shape
    SKILL.md's polling recipe invites — then never got parseable JSON and read a finished
    ``status: ok`` review as a hang.

    A real subprocess with a combined capture, because that IS the defect. The transcript is
    large on purpose: the window is however much work ``run()`` still has to do after the
    close, and a small one did not fire it.
    """

    def test_the_combined_capture_is_parseable_json(self):
        with tempfile.TemporaryDirectory() as d:
            helper = Path(d) / "helper.py"
            helper.write_text(
                "import sys, time\n"
                "end = time.time() + 10\n"
                "while time.time() < end:\n"
                "    sys.stderr.write('helper noise\\n'); sys.stderr.flush()\n"
                "    time.sleep(0.001)\n",
                encoding="utf-8")
            child = Path(d) / "child.py"
            child.write_text(
                "import json, os, subprocess, sys\n"
                "print(json.dumps({'type': 'item.completed', 'item': "
                "{'type': 'agent_message', 'text': 'x' * 8_000_000}}), flush=True)\n"
                "print(json.dumps({'type': 'turn.completed'}), flush=True)\n"
                # Inherits stderr, does NOT inherit stdout: stdout must reach EOF so the
                # asymmetry between the two drains is what the test exercises.
                "subprocess.Popen([sys.executable, %r], stdout=subprocess.DEVNULL)\n"
                % str(helper),
                encoding="utf-8")
            proc = subprocess.run(
                [sys.executable, str(_RUNNER),
                 "--idle", "30", "--deadline", "45",
                 "--findings", str(Path(d) / "findings.txt"),
                 "--display", str(Path(d) / "display.log"),
                 "--verdict-json", str(Path(d) / "verdict.json"),
                 "--result-mode", "stream-transcript",
                 "--", sys.executable, str(child)],
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL, timeout=120)
            combined = proc.stdout.decode("utf-8", "replace")
            self.assertNotIn("ValueError: I/O operation on closed file", combined)
            try:
                status = json.loads(combined.strip().splitlines()[-1])
            except (json.JSONDecodeError, IndexError):  # pragma: no cover - failure path
                self.fail(f"the combined capture is not parseable JSON:\n{combined[:2000]}")
            self.assertEqual(status["status"], "ok")
            # The whole capture, not merely its last line: a caller reading the stream as
            # one object is the case that broke.
            self.assertEqual(json.loads(combined.strip())["status"], "ok")


class TheProgramCheckedIsTheProgramRun(unittest.TestCase):
    """`os.path.isfile(cmd[0])` and `Popen(cmd, cwd=...)` did not resolve the same path.

    `os.path.isfile` answers relative to the SUPERVISOR's working directory. On POSIX the
    exec happens after the chdir, so `Popen` resolves a relative program path against
    `--cwd` instead — a different file, checked in one place and run from another. On
    Windows CreateProcess resolves it against the calling process's directory, so the two
    platforms did not even agree with each other.

    Refused rather than resolved against `--cwd`. `--cwd` is the checkout being reviewed,
    and running a program out of it is exactly what the PATH-only lookup above exists to
    prevent. A caller naming a program by path can name it absolutely.
    """

    @staticmethod
    def _marker_script(root, tag):
        (root / "tools").mkdir(parents=True, exist_ok=True)
        script = root / "tools" / "rev.py"
        script.write_text(
            "import pathlib, sys\n"
            f"pathlib.Path({str(root / 'RAN.txt')!r}).write_text({tag!r})\n",
            encoding="utf-8")
        return script

    def _supervise(self, *args, cwd):
        proc = subprocess.run(
            [PY, str(_RUNNER), *args],
            capture_output=True, text=True, cwd=str(cwd),
            stdin=subprocess.DEVNULL, timeout=120)
        return json.loads(proc.stdout.strip().splitlines()[-1])

    @unittest.skipIf(os.name != "posix", "the ./ launcher shape is POSIX")
    def test_a_relative_program_with_cwd_is_refused_and_nothing_runs(self):
        with tempfile.TemporaryDirectory() as a, tempfile.TemporaryDirectory() as b:
            here, elsewhere = Path(a), Path(b)
            for root, tag in ((here, "CHECKED"), (elsewhere, "EXECUTED")):
                self._marker_script(root, tag)
                launcher = root / "tools" / "rev"
                launcher.write_text(
                    f"#!/bin/sh\nexec {PY} {root / 'tools' / 'rev.py'}\n",
                    encoding="utf-8")
                os.chmod(launcher, 0o755)
            res = self._supervise(
                "--idle", "5", "--deadline", "10",
                "--findings", str(here / "out.md"),
                "--result-mode", "external-file",
                "--cwd", str(elsewhere),
                "--", "./tools/rev", cwd=here)
            self.assertEqual(res["status"], "error")
            self.assertIn("--cwd", res["reason"])
            self.assertFalse((here / "RAN.txt").exists(),
                             "the refusal ran the checked program anyway")
            self.assertFalse((elsewhere / "RAN.txt").exists(),
                             "the refusal ran the program under --cwd")

    @unittest.skipIf(os.name != "posix", "the ./ launcher shape is POSIX")
    def test_a_relative_program_without_cwd_runs_the_file_that_was_checked(self):
        """Anti-vacuity: refusing every relative path would satisfy the test above."""
        with tempfile.TemporaryDirectory() as a:
            here = Path(a)
            self._marker_script(here, "CHECKED")
            launcher = here / "tools" / "rev"
            launcher.write_text(
                f"#!/bin/sh\nexec {PY} {here / 'tools' / 'rev.py'}\n", encoding="utf-8")
            os.chmod(launcher, 0o755)
            self._supervise(
                "--idle", "5", "--deadline", "10",
                "--findings", str(here / "out.md"),
                "--result-mode", "external-file",
                "--", "./tools/rev", cwd=here)
            self.assertEqual((here / "RAN.txt").read_text(), "CHECKED")

    def test_an_absolute_program_with_cwd_still_runs_in_that_directory(self):
        """Anti-vacuity: `--cwd` itself must keep working — it is how the reviewer is
        pointed at the checkout under review."""
        with tempfile.TemporaryDirectory() as a, tempfile.TemporaryDirectory() as b:
            here, elsewhere = Path(a), Path(b)
            child = here / "child.py"
            child.write_text(
                "import os, pathlib\n"
                f"pathlib.Path({str(here / 'out.md')!r}).write_text(os.getcwd())\n",
                encoding="utf-8")
            res = self._supervise(
                "--idle", "5", "--deadline", "10",
                "--findings", str(here / "out.md"),
                "--result-mode", "external-file",
                "--cwd", str(elsewhere),
                "--", PY, str(child), cwd=here)
            self.assertEqual(res["status"], "ok", res.get("reason"))
            self.assertEqual(
                Path(os.path.realpath((here / "out.md").read_text())),
                Path(os.path.realpath(elsewhere)))


class ItOwnsOnlyWhatItCreates(unittest.TestCase):
    """The supervisor writes files it creates and removes only those. Nothing else.

    This replaces ~115 lines that asked git whether an output path was tracked, so the
    unconditional up-front `unlink` would not destroy source. That guard had to reconstruct
    an answer git owns and failed open three times over three rounds — on a git error, on
    repository discovery, on a case-folding filesystem.

    Refusing a path that already exists removes the question. Anything at those paths
    afterwards was created by this run, which is stronger than the invariant the `unlink`
    defended: a gate cannot read a previous run's verdict as this one's.
    """

    def _child_writing(self, path, text="one blocker\n"):
        return f"import pathlib; pathlib.Path({str(path)!r}).write_text({text!r})"

    # --- the refusal ------------------------------------------------------------

    def test_an_existing_findings_path_refuses_the_run_and_is_left_alone(self):
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "src.py"
            f.write_text("print('real source')\n", encoding="utf-8")
            res = _run("--idle", "0", "--deadline", "10", "--findings", str(f),
                       "--result-mode", "external-file", "--", "no-such-cli-xyz")
            self.assertEqual(res["status"], "error")
            self.assertIn("already exists", res["reason"])
            self.assertEqual(f.read_text(encoding="utf-8"), "print('real source')\n")

    def test_an_existing_verdict_path_refuses_the_run_too(self):
        with tempfile.TemporaryDirectory() as d:
            v = Path(d) / "notes.md"
            v.write_text("real notes\n", encoding="utf-8")
            res = _run("--idle", "5", "--deadline", "10",
                       "--findings", str(Path(d) / "out.md"), "--verdict-json", str(v),
                       "--result-mode", "external-file", "--", "no-such-cli-xyz")
            self.assertEqual(res["status"], "error")
            self.assertIn("already exists", res["reason"])
            self.assertEqual(v.read_text(encoding="utf-8"), "real notes\n")

    def test_a_directory_at_the_output_path_is_refused_rather_than_walked_into(self):
        with tempfile.TemporaryDirectory() as d:
            target = Path(d) / "findings"
            target.mkdir()
            (target / "keep.txt").write_text("someone's file\n", encoding="utf-8")
            res = _run("--idle", "5", "--deadline", "10", "--findings", str(target),
                       "--result-mode", "external-file", "--", "no-such-cli-xyz")
            self.assertEqual(res["status"], "error")
            self.assertIn("already exists", res["reason"])
            self.assertTrue((target / "keep.txt").is_file())

    @unittest.skipUnless(os.name == "posix", "symlink creation differs on Windows")
    def test_a_dangling_symlink_counts_as_existing(self):
        """`lexists`, so a link is refused rather than followed to its target."""
        with tempfile.TemporaryDirectory() as d:
            link = Path(d) / "findings.txt"
            link.symlink_to(Path(d) / "nowhere")
            res = _run("--idle", "5", "--deadline", "10", "--findings", str(link),
                       "--result-mode", "external-file", "--", "no-such-cli-xyz")
            self.assertEqual(res["status"], "error")
            self.assertIn("already exists", res["reason"])
            self.assertTrue(link.is_symlink(), "the link itself was removed")

    def test_no_git_call_is_made_at_all(self):
        """The point of the restructure, asserted rather than described.

        Spawning git on every invocation to decide whether deleting is allowed is what
        this replaces. Nothing here asks anything about a repository.
        """
        spawned = []
        real_run = subprocess.run

        def recording_run(argv, *a, **kw):
            spawned.append(argv)
            return real_run(argv, *a, **kw)

        with tempfile.TemporaryDirectory() as d:
            out = Path(d) / "findings.txt"
            child = self._child_writing(out)
            with unittest.mock.patch.object(subprocess, "run", recording_run):
                res = _run("--idle", "5", "--deadline", "10", "--findings", str(out),
                           "--result-mode", "external-file", "--", PY, "-c", child)
        self.assertEqual(res["status"], "ok", res.get("reason"))
        self.assertEqual(
            [a for a in spawned if a and str(a[0]).endswith("git")], [],
            "the supervisor still shells out to git")

    def test_the_two_flags_may_not_name_the_same_path(self):
        with tempfile.TemporaryDirectory() as d:
            same = str(Path(d) / "both.json")
            res = _run("--idle", "5", "--deadline", "10", "--findings", same,
                       "--verdict-json", same,
                       "--result-mode", "external-file", "--", "no-such-cli-xyz")
            self.assertEqual(res["status"], "error")
            self.assertIn("same path", res["reason"])

    def test_the_two_flags_may_not_differ_only_in_case(self):
        """One file on Windows and macOS; the refusal has to fire on every platform."""
        with tempfile.TemporaryDirectory() as d:
            res = _run("--idle", "5", "--deadline", "10",
                       "--findings", str(Path(d) / "findings.json"),
                       "--verdict-json", str(Path(d) / "FINDINGS.JSON"),
                       "--result-mode", "external-file", "--", "no-such-cli-xyz")
            self.assertEqual(res["status"], "error")
            self.assertIn("same path", res["reason"])

    # --- what it still does, so the refusal is not a wall ------------------------

    def test_an_ordinary_run_into_fresh_paths_succeeds(self):
        """Anti-vacuity: refusing everything would satisfy every test above."""
        with tempfile.TemporaryDirectory() as d:
            out = Path(d) / ".review" / "findings.txt"
            res = _run("--idle", "5", "--deadline", "10", "--findings", str(out),
                       "--result-mode", "external-file",
                       "--", PY, "-c", self._child_writing(out))
            self.assertEqual(res["status"], "ok", res.get("reason"))
            self.assertEqual(out.read_text(encoding="utf-8"), "one blocker\n")

    def test_a_run_inside_a_git_repository_is_no_different(self):
        """There is no repository question any more — a checkout is just a directory."""
        if not shutil.which("git"):
            self.skipTest("needs git")
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            for argv in (["init", "-q"], ["config", "user.email", "t@e.invalid"],
                         ["config", "user.name", "t"]):
                subprocess.run(["git", *argv], cwd=root, check=True, capture_output=True)
            (root / "src.py").write_text("x\n", encoding="utf-8")
            subprocess.run(["git", "add", "src.py"], cwd=root, check=True,
                           capture_output=True)
            subprocess.run(["git", "commit", "-qm", "s"], cwd=root, check=True,
                           capture_output=True)
            out = root / ".review" / "findings.txt"
            res = _run("--idle", "5", "--deadline", "10", "--findings", str(out),
                       "--result-mode", "external-file",
                       "--", PY, "-c", self._child_writing(out))
            self.assertEqual(res["status"], "ok", res.get("reason"))
            self.assertEqual(
                subprocess.run(["git", "status", "--porcelain", "--", "src.py"],
                               cwd=root, capture_output=True, text=True).stdout, "")

    # --- cleanup: only what this run made, and only when it failed ---------------

    def test_a_failed_run_removes_the_files_it_created(self):
        """So the refuse-if-it-exists rule cannot trap a caller who retries."""
        with tempfile.TemporaryDirectory() as d:
            out, v = Path(d) / "findings.txt", Path(d) / "verdict.json"
            # Writes the findings file, then exits non-zero: created by this run, and
            # the run is not a success.
            child = self._child_writing(out) + "; import sys; sys.exit(3)"
            res = _run("--idle", "5", "--deadline", "10", "--findings", str(out),
                       "--verdict-json", str(v), "--result-mode", "external-file",
                       "--", PY, "-c", child)
            self.assertEqual(res["status"], "error")
            self.assertFalse(out.exists(), "a failed run left its own output behind")
            self.assertFalse(v.exists())

    def test_the_retry_after_that_failure_starts_cleanly(self):
        """The whole reason the cleanup exists, driven end to end."""
        with tempfile.TemporaryDirectory() as d:
            out = Path(d) / "findings.txt"
            failing = self._child_writing(out) + "; import sys; sys.exit(3)"
            self.assertEqual(
                _run("--idle", "5", "--deadline", "10", "--findings", str(out),
                     "--result-mode", "external-file", "--", PY, "-c",
                     failing)["status"], "error")
            res = _run("--idle", "5", "--deadline", "10", "--findings", str(out),
                       "--result-mode", "external-file", "--", PY, "-c",
                       self._child_writing(out, "the real review\n"))
            self.assertEqual(res["status"], "ok", res.get("reason"))
            self.assertEqual(out.read_text(encoding="utf-8"), "the real review\n")

    def test_a_successful_run_keeps_both_outputs(self):
        """Anti-vacuity: deleting unconditionally would satisfy the two tests above."""
        verdict = json.dumps(
            {"findings": [], "overall": "clean", "blocking_count": 0})
        with tempfile.TemporaryDirectory() as d:
            out, v = Path(d) / "findings.txt", Path(d) / "verdict.json"
            child = (
                "import json; "
                f"text = {('I read every hunk.' + chr(10) + chr(10) + verdict)!r}; "
                "print(json.dumps({'type':'item.completed',"
                "'item':{'type':'agent_message','text':text}})); "
                "print(json.dumps({'type':'turn.completed'}))"
            )
            res = _run("--idle", "5", "--deadline", "10", "--findings", str(out),
                       "--verdict-json", str(v), "--result-mode", "stream-transcript",
                       "--", PY, "-c", child)
            self.assertEqual(res["status"], "ok", res.get("reason"))
            self.assertTrue(out.is_file())
            self.assertTrue(v.is_file())

    def test_a_refusal_before_the_run_removes_nothing(self):
        """The refusal path must not delete the OTHER flag's path either."""
        with tempfile.TemporaryDirectory() as d:
            existing, other = Path(d) / "findings.txt", Path(d) / "verdict.json"
            existing.write_text("someone's file\n", encoding="utf-8")
            other.write_text("someone else's file\n", encoding="utf-8")
            res = _run("--idle", "5", "--deadline", "10", "--findings", str(existing),
                       "--verdict-json", str(other), "--result-mode", "external-file",
                       "--", "no-such-cli-xyz")
            self.assertEqual(res["status"], "error")
            self.assertEqual(existing.read_text(encoding="utf-8"), "someone's file\n")
            self.assertEqual(other.read_text(encoding="utf-8"),
                             "someone else's file\n")

    def test_an_early_error_after_the_check_still_creates_nothing(self):
        """`--idle 0` returns before the launch: no file appears, none is removed."""
        with tempfile.TemporaryDirectory() as d:
            out = Path(d) / "findings.txt"
            res = _run("--idle", "0", "--deadline", "10", "--findings", str(out),
                       "--result-mode", "external-file", "--", PY, "-c", "pass")
            self.assertEqual(res["status"], "error")
            self.assertIn("--idle", res["reason"])
            self.assertFalse(out.exists())



class OwnershipIsTakenNotObserved(unittest.TestCase):
    """The four ways "refuse if it exists" can still lose ownership of its output.

    Refusing to start on a path that already exists answers "was anything here a moment
    ago?". It never answers "is this mine?" — and between the check and the create, the
    answer can change.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)

    def test_the_claim_is_an_exclusive_create_not_a_check(self):
        """`lexists` then write-by-name is check-then-act; O_EXCL is one syscall.

        Between the two, anything may put a symlink or a hardlink at the path and the
        later write follows it. POSIX requires O_CREAT|O_EXCL to fail on a symlink, which
        is exactly the case a check made before the write cannot enforce.
        """
        source = inspect.getsource(review_runner.run)
        self.assertIn("O_EXCL", source,
                      "ownership is still being observed rather than taken")

    def test_a_relative_output_path_with_cwd_is_refused_rather_than_guessed(self):
        """Two directories, one name. The supervisor resolves against its own cwd while
        the child runs in --cwd, so each created or looked for a different file: the run
        reported that the reviewer wrote nothing and left the real output behind."""
        out = _run(*["--idle", "5", "--deadline", "10", "--cwd", str(self.dir), "--findings", "findings.md",
                           "--result-mode", "stream-transcript", "--", "true"])
        self.assertEqual(out.get("status"), "error")
        self.assertIn("absolute", out.get("reason", ""))

    def test_display_is_in_the_same_path_guard(self):
        """It was compared for findings against verdict only. Sharing a path with
        --display in external-file mode let the display handle wrap start/end markers
        around the reviewer's write, and the corrupted file was read as the result."""
        shared = self.dir / "same.md"
        out = _run(*["--idle", "5", "--deadline", "10", "--display", str(shared), "--findings", str(shared),
                           "--result-mode", "stream-transcript", "--", "true"])
        self.assertEqual(out.get("status"), "error")
        self.assertIn("same path", out.get("reason", ""))

    def test_an_interrupted_run_removes_what_it_created(self):
        """Otherwise the retry is refused for a collision this program caused.

        The signal handler exits through os._exit, which skips anything registered to run
        at exit, so without cleanup here every interruption blocks the rerun.
        """
        handler = inspect.getsource(review_runner._Interrupts._on_signal)
        before_exit = handler.split("os._exit(1)  # must not", 1)[0]
        self.assertIn("os.unlink", before_exit,
                      "the signal path exits without removing what the run created")



class AFailedLaunchReleasesWhatItClaimed(unittest.TestCase):
    """Claiming the outputs and then failing to launch left them behind.

    The supervisor takes `--findings` and `--verdict-json` with `O_CREAT | O_EXCL`, which
    is how it can say "this is mine" rather than "nothing was here a moment ago". A refused
    CLAIM already unlinks what it made. A refused LAUNCH did not — so two empty files
    survived, and the next attempt refused a path it had not created. The retry then failed
    for a reason that had nothing to do with the retry, and the operator deleted files by
    hand to run the same command again. A bad `--cwd` is enough to reach it.
    """

    def _run(self, findings, verdict, cwd):
        return subprocess.run(
            [sys.executable, str(_RUNNER), "--idle", "5", "--deadline", "5",
             "--cwd", str(cwd), "--findings", str(findings),
             "--verdict-json", str(verdict), "--result-mode", "stream-transcript",
             "--", sys.executable, "-c", "pass"],
            capture_output=True, text=True, encoding="utf-8")

    def test_a_launch_failure_leaves_no_claimed_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            findings, verdict = d / "f.txt", d / "v.json"
            proc = self._run(findings, verdict, d / "not-a-directory")
            self.assertIn("launch failed", proc.stdout + proc.stderr)
            self.assertFalse(findings.exists(), "the claimed findings file was left behind")
            self.assertFalse(verdict.exists(), "the claimed verdict file was left behind")

    def test_the_same_command_can_simply_be_run_again(self):
        """The consequence that matters: the retry is not refused for the first failure."""
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            findings, verdict = d / "f.txt", d / "v.json"
            self._run(findings, verdict, d / "not-a-directory")
            again = self._run(findings, verdict, d / "still-not-a-directory")
            self.assertNotIn("already exists", again.stdout + again.stderr,
                             "the retry was refused over residue from the first attempt")


class MalformedStreamLinesDoNotKillTheReader(unittest.TestCase):
    """A line the decoder cannot handle is skipped, never raised. The reader thread dying
    loses every later event, so a partial transcript goes out as the review."""

    DEEP = "[" * 100000 + "]" * 100000

    def test_a_deeply_nested_line_is_skipped_in_every_capture(self):
        state = {"last_result": None, "transcript": [], "terminal": None, "terminal_event": None}
        lock = threading.Lock()
        for mode in ("stream-json-result-event", "stream-transcript"):
            with self.subTest(mode=mode):
                review_runner._consume_jsonl(self.DEEP, mode, state, lock)
        self.assertIsNone(state["last_result"])
        self.assertEqual(state["transcript"], [])
        self.assertIsNone(state["terminal"])
        # Anti-vacuity: an ordinary terminal line after it still registers.
        review_runner._consume_jsonl(json.dumps({"type": "turn.completed"}),
                                     "stream-transcript", state, lock)
        self.assertEqual(state["terminal"], "ok")

    def test_an_event_whose_item_or_message_is_not_an_object_is_skipped(self):
        for event in ({"type": "item.completed", "item": "agent_message"},
                      {"type": "item.completed", "item": [1, 2]},
                      {"type": "assistant", "message": ["text"]},
                      {"type": "assistant", "message": "text"}):
            with self.subTest(event=event):
                self.assertIsNone(review_runner._event_text(event))
        self.assertEqual(review_runner._event_text(
            {"type": "item.completed", "item": {"type": "agent_message", "text": "hello"}}), "hello")


class ABlockingCountTheModelWroteAsAnotherType(unittest.TestCase):
    """`blocking_count` is what a gate reads. A claim spelled 2.0 or "2" is still a claim,
    and dropping it publishes zero for a review that said otherwise."""

    def reconcile(self, claimed):
        verdict = {"findings": [], "overall": "clean", "blocking_count": claimed}
        note = review_runner._reconcile_blocking_count(verdict)
        return verdict["blocking_count"], note

    def test_a_positive_claim_in_another_type_is_not_published_as_clean(self):
        for claimed in (2.0, "2", " 2 "):
            with self.subTest(claimed=claimed):
                count, note = self.reconcile(claimed)
                self.assertEqual(count, 1)
                self.assertIsNotNone(note)

    def test_a_claim_that_is_no_whole_number_is_still_no_claim(self):
        for claimed in (2.5, "two", True, None):
            with self.subTest(claimed=claimed):
                self.assertEqual(self.reconcile(claimed)[0], 0)


class TheKillLadderReachesTheGroupAfterTheLeaderIsReaped(unittest.TestCase):
    """`os.getpgid` is unusable once `wait()` has reaped the leader, and the group is what
    holds the inherited pipes: the SIGKILL rung never left the ground."""

    class _Proc:
        pid = 4321

        def __init__(self):
            self.polls = [None, None]

        def poll(self):
            return self.polls.pop(0) if self.polls else 0

        def wait(self, timeout=None):
            return 0

        def terminate(self):
            pass

        def kill(self):
            pass

    @unittest.skipUnless(hasattr(os, "killpg"), "no process groups on this platform")
    def test_both_rungs_signal_the_group_by_the_child_pid(self):
        sent = []

        def killpg(pgid, sig):
            sent.append((pgid, sig))

        def getpgid(pid):
            raise ProcessLookupError(3, "No such process")

        with unittest.mock.patch.object(review_runner.os, "name", "posix"), \
             unittest.mock.patch.object(review_runner.os, "killpg", killpg), \
             unittest.mock.patch.object(review_runner.os, "getpgid", getpgid):
            review_runner._terminate(self._Proc())
        self.assertEqual([sig for _pgid, sig in sent], [signal.SIGTERM, signal.SIGKILL])
        self.assertEqual({pgid for pgid, _sig in sent}, {4321})


def _run_lines(func="run"):
    """Line numbers, within one function, of the calls and assignments whose ORDER is the
    guarantee. Asserted on the source because these windows are microseconds wide: a test
    that delivered a signal into one would be a race, and a green run would prove nothing.
    """
    tree = ast.parse(_RUNNER.read_text(encoding="utf-8"), filename=str(_RUNNER))
    found = [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == func]
    assert len(found) == 1, f"expected one `def {func}`, found {len(found)}"
    fn = found[0]
    out = {"signal.signal": [], "os.open": [], "subprocess.Popen": [],
           "guard.arm": [], "guard.claiming": [], "guard.report": []}
    for node in ast.walk(fn):
        if isinstance(node, ast.Call):
            called = node.func
            if isinstance(called, ast.Attribute) and isinstance(called.value, ast.Name):
                out.setdefault(f"{called.value.id}.{called.attr}", []).append(node.lineno)
            elif isinstance(called, ast.Name):
                out.setdefault(called.id, []).append(node.lineno)
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if (isinstance(target, ast.Subscript) and isinstance(target.value, ast.Name)
                        and target.value.id == "signal_state"
                        and isinstance(target.slice, ast.Constant)):
                    out.setdefault(f"signal_state[{target.slice.value}]", []).append(node.lineno)
                if isinstance(target, ast.Attribute) and isinstance(target.value, ast.Name):
                    out.setdefault(f"{target.value.id}.{target.attr}", []).append(node.lineno)

    def outside_nested(node):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.FunctionDef):
                continue  # a nested def is not run's own flow
            yield child
            yield from outside_nested(child)

    out["_emit in run itself"] = [
        n.lineno for n in outside_nested(fn)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == "_emit"]
    return out


class TheSupervisorIsInterruptibleAtEveryStep(unittest.TestCase):
    """Three windows where a signal, or the lack of a handler, cost more than the run."""

    def test_the_handlers_are_armed_before_the_outputs_are_claimed(self):
        lines = _run_lines()
        self.assertTrue(lines["guard.arm"] and lines["os.open"])
        self.assertLess(min(lines["guard.arm"]), min(lines["os.open"]),
                        "a signal between the claim and the arming leaves the claimed files "
                        "behind, and the retry is refused for a collision this run caused")

    def test_the_claim_window_is_recorded_rather_than_blocked(self):
        """A signal mask closes this window on POSIX and does nothing at all on Windows,
        where the handler runs just the same. Recording covers both."""
        lines = _run_lines()
        claiming = lines["guard.claiming"]
        # Set once before the claim and cleared on every way out of it — the refusals
        # included, which is why this counts the span and not the statements.
        self.assertGreaterEqual(len(claiming), 2)
        self.assertLess(min(claiming), min(lines["os.open"]))
        self.assertGreater(max(claiming), max(lines["os.open"]))
        self.assertNotIn("signal.pthread_sigmask", lines,
                         "a held mask is INHERITED by the child if it ever spans the spawn, "
                         "and on Windows it is not held at all")

    def test_every_report_after_arming_goes_through_the_one_helper(self):
        """Storing the payload before printing is what lets the handler stand aside. A
        `return _emit(...)` that skips it can be cut off with no status line at all."""
        lines = _run_lines()
        armed = min(lines["guard.arm"])
        self.assertEqual([n for n in lines["_emit in run itself"] if n > armed], [],
                         "an exit after the handlers are armed prints without storing its "
                         "payload first")


class AVerdictPathTheSupervisorCannotPrepare(unittest.TestCase):
    """The verdict is additive and never fatal — including when its directory is missing."""

    def test_a_verdict_directory_that_cannot_be_made_does_not_refuse_the_review(self):
        with tempfile.TemporaryDirectory() as d:
            findings = str(Path(d) / "findings.txt")
            verdict = str(Path(d) / "no" / "such" / "dir" / "verdict.json")
            child = (r'import json; print(json.dumps({"type":"item.completed","item":'
                     r'{"type":"agent_message","text":"the review"}})); '
                     r'print(json.dumps({"type":"turn.completed"}))')
            res = _run("--idle", "10", "--deadline", "20", "--findings", findings,
                       "--verdict-json", verdict, "--result-mode", "stream-transcript",
                       "--", PY, "-c", child)
            self.assertEqual(res["status"], "ok", res)
            self.assertIn("the review", Path(findings).read_text(encoding="utf-8"))

    def test_a_verdict_directory_that_is_a_file_drops_the_verdict_with_a_reason(self):
        with tempfile.TemporaryDirectory() as d:
            blocker = Path(d) / "blocker"
            blocker.write_text("not a directory\n", encoding="utf-8")
            findings = str(Path(d) / "findings.txt")
            child = (r'''import json; print(json.dumps({"type":"item.completed","item":'''
                     r'''{"type":"agent_message","text":"the review"}})); '''
                     r'''print(json.dumps({"type":"turn.completed"}))''')
            res = _run("--idle", "10", "--deadline", "20", "--findings", findings,
                       "--verdict-json", str(blocker / "verdict.json"),
                       "--result-mode", "stream-transcript", "--", PY, "-c", child)
            self.assertEqual(res["status"], "ok", res)
            self.assertIn("verdict directory", res["verdict_reason"] or "")


class AVerdictTooDeepToWriteIsReportedNotFatal(unittest.TestCase):
    """`json.dumps` recurses once per level when it indents; the review is already done."""

    def test_a_verdict_write_that_recurses_is_a_reason_not_an_error(self):
        real_dumps = json.dumps

        def dumps(obj, *args, **kwargs):
            if kwargs.get("indent") == 2:  # the verdict write, and nothing else
                raise RecursionError("maximum recursion depth exceeded")
            return real_dumps(obj, *args, **kwargs)

        with tempfile.TemporaryDirectory() as d:
            findings = str(Path(d) / "findings.txt")
            verdict = str(Path(d) / "verdict.json")
            child = (r'import json; print(json.dumps({"type":"item.completed","item":'
                     r'{"type":"agent_message","text":' + repr(VERDICT) + r'}})); '
                     r'print(json.dumps({"type":"turn.completed"}))')
            with unittest.mock.patch.object(review_runner.json, "dumps", dumps):
                res = _run("--idle", "10", "--deadline", "20", "--findings", findings,
                           "--verdict-json", verdict, "--result-mode", "stream-transcript",
                           "--", PY, "-c", child)
            self.assertEqual(res["status"], "ok", res)
            self.assertIsNotNone(res["verdict_reason"])


class ACliInTheCheckoutDoesNotHideTheRealOne(unittest.TestCase):
    """Windows searches the current directory first; the copy on PATH is still the one asked for."""

    def test_the_resolver_takes_the_later_entry_and_never_the_current_directory(self):
        with tempfile.TemporaryDirectory() as d:
            empty, holding = Path(d) / "empty", Path(d) / "holding"
            empty.mkdir(); holding.mkdir()
            program = holding / "reviewer"
            program.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            program.chmod(0o755)
            search = os.pathsep.join([str(empty), str(holding)])

            self.assertEqual(review_runner._which_outside_cwd("reviewer", search), str(program))
            # The same entry, once it IS the current directory: the tree under review does
            # not get to supply the program sent to read it.
            with unittest.mock.patch.object(review_runner.os, "getcwd", lambda: str(holding)):
                self.assertIsNone(review_runner._which_outside_cwd("reviewer", search))

    @unittest.skipUnless(os.name == "posix", "a symlink needs privileges on Windows")
    def test_an_alias_of_the_current_directory_is_still_the_current_directory(self):
        """Spelling is not identity. A junction, a symlink or a differently cased Windows
        path names the same directory, and accepting it runs the checkout's own copy."""
        with tempfile.TemporaryDirectory() as d:
            holding = Path(d) / "holding"
            holding.mkdir()
            program = holding / "reviewer"
            program.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            program.chmod(0o755)
            alias = Path(d) / "alias"
            os.symlink(holding, alias)
            with unittest.mock.patch.object(review_runner.os, "getcwd", lambda: str(holding)):
                self.assertIsNone(review_runner._which_outside_cwd("reviewer", str(alias)))

    def test_pathext_applies_to_a_name_that_already_has_an_extension(self):
        with tempfile.TemporaryDirectory() as d:
            holding = Path(d) / "bin"
            holding.mkdir()
            # Spelled as PATHEXT spells it. Windows' filesystem is case-insensitive and
            # this one is not, so the case here is an artifact of where the test runs.
            shim = holding / "reviewer.v2.CMD"
            shim.write_text("@echo off\n", encoding="utf-8")
            shim.chmod(0o755)
            with unittest.mock.patch.object(review_runner.os, "name", "nt"), \
                 unittest.mock.patch.dict(os.environ, {"PATHEXT": ".COM;.EXE;.BAT;.CMD"}):
                found = review_runner._which_outside_cwd("reviewer.v2", str(holding))
            self.assertEqual(found, str(shim))

    def test_the_rest_of_path_is_searched_when_the_first_hit_is_the_checkout(self):
        with tempfile.TemporaryDirectory() as d:
            elsewhere = Path(d) / "bin"
            elsewhere.mkdir()
            real = elsewhere / "reviewer"
            real.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            real.chmod(0o755)
            planted = Path.cwd() / "reviewer"

            def which_as_windows_does(cmd, mode=os.F_OK | os.X_OK, path=None):
                entries = (path or "").split(os.pathsep)
                if os.getcwd() not in entries:  # Windows looks here first, always
                    return str(planted)
                return str(real)

            with unittest.mock.patch.object(review_runner.shutil, "which", which_as_windows_does), \
                 unittest.mock.patch.dict(os.environ, {"PATH": str(elsewhere)}):
                res = _run("--idle", "5", "--deadline", "10",
                           "--findings", str(Path(d) / "findings.txt"),
                           "--result-mode", "external-file", "--", "reviewer")
            self.assertNotIn("not found on PATH", (res.get("reason") or ""), res)


class AHeldPipeIsNotAFailedReview(unittest.TestCase):
    """A descendant holding stdout keeps the reader from EOF. Where the reviewer's own
    terminal event already arrived, the stream ended and only the pipe is open — and on
    Windows there is no process group to reap it with."""

    def test_payload_complete_asks_the_mode_what_it_reads(self):
        lock = threading.Lock()
        transcript = {"terminal": "ok", "last_result": None}
        self.assertTrue(review_runner._payload_complete("stream-transcript", transcript, lock))
        self.assertFalse(review_runner._payload_complete(
            "stream-transcript", {"terminal": None, "last_result": None}, lock))
        self.assertTrue(review_runner._payload_complete(
            "stream-json-result-event", {"terminal": None, "last_result": {"type": "result"}}, lock))
        self.assertFalse(review_runner._payload_complete(
            "external-file", {"terminal": "ok", "last_result": None}, lock))

    @unittest.skipUnless(os.name == "posix", "the child inherits stderr the POSIX way")
    def test_a_descendant_holding_stderr_is_reaped_rather_than_left_running(self):
        reaped = []
        real_reap = review_runner._reap_group

        def counting_reap(proc):
            reaped.append(proc.pid)
            return real_reap(proc)

        with tempfile.TemporaryDirectory() as d:
            helper = Path(d) / "helper.py"
            helper.write_text("import time\ntime.sleep(15)\n", encoding="utf-8")
            child = Path(d) / "child.py"
            child.write_text(
                "import json, subprocess, sys\n"
                "print(json.dumps({'type': 'item.completed', 'item': "
                "{'type': 'agent_message', 'text': 'the review'}}), flush=True)\n"
                "print(json.dumps({'type': 'turn.completed'}), flush=True)\n"
                # Inherits stderr only: stdout reaches EOF, stderr stays held.
                "subprocess.Popen([sys.executable, %r], stdout=subprocess.DEVNULL)\n" % str(helper),
                encoding="utf-8")
            with unittest.mock.patch.object(review_runner, "_reap_group", counting_reap):
                res = _run("--idle", "40", "--deadline", "60",
                           "--findings", str(Path(d) / "findings.txt"),
                           "--result-mode", "stream-transcript", "--", PY, str(child))
            self.assertEqual(res["status"], "ok", res)
            self.assertTrue(reaped, "the survivor holding stderr was never reaped")


class AFifoAsTheDisplayLogCannotStopTheRun(unittest.TestCase):
    """`--display` is watched, not owned — the one output this supervisor shares and never
    removes. Opening a FIFO waits for a reader, and a full one blocks every write: either
    stops a supervised review dead, with no status line and neither timer running, which is
    the one thing the skill promises watching the log cannot do."""

    @unittest.skipUnless(hasattr(os, "mkfifo"), "no FIFOs on this platform")
    def test_the_open_fails_or_declines_rather_than_waiting(self):
        with tempfile.TemporaryDirectory() as d:
            fifo = str(Path(d) / "watch.fifo")
            os.mkfifo(fifo)
            with self.assertRaises(OSError):  # no reader: ENXIO now, a wait forever before
                review_runner._open_display(fifo)
            reader = os.open(fifo, os.O_RDONLY | os.O_NONBLOCK)
            try:
                self.assertIsNone(review_runner._open_display(fifo))
            finally:
                os.close(reader)

    def test_a_regular_file_is_still_appended_to(self):
        with tempfile.TemporaryDirectory() as d:
            log = Path(d) / "display.log"
            log.write_text("first\n", encoding="utf-8")
            handle = review_runner._open_display(str(log))
            self.assertIsNotNone(handle)
            handle.write("second\n")
            handle.close()
            self.assertEqual(log.read_text(encoding="utf-8"), "first\nsecond\n")

    @unittest.skipUnless(hasattr(os, "mkfifo"), "no FIFOs on this platform")
    def test_a_review_completes_though_the_display_log_is_a_fifo(self):
        with tempfile.TemporaryDirectory() as d:
            fifo = str(Path(d) / "watch.fifo")
            os.mkfifo(fifo)
            findings = str(Path(d) / "findings.txt")
            child = ("import json\n"
                     'print(json.dumps({"type": "item.completed", "item": '
                     '{"type": "agent_message", "text": "the review"}}))\n'
                     'print(json.dumps({"type": "turn.completed"}))')
            # A SUBPROCESS, because the defect is an unbounded wait: in-process it would
            # hang the whole suite rather than fail one test.
            proc = subprocess.run(
                [PY, str(_RUNNER), "--idle", "10", "--deadline", "20",
                 "--findings", findings, "--display", fifo,
                 "--result-mode", "stream-transcript", "--", PY, "-c", child],
                capture_output=True, text=True, timeout=60)
            status = json.loads(proc.stdout.strip().splitlines()[-1])
            self.assertEqual(status["status"], "ok", proc.stdout)
            self.assertIn("the review", Path(findings).read_text(encoding="utf-8"))


def _harness(directory, patch_source):
    """Write a script that runs the supervisor in-process with one thing replaced.

    A subprocess, because every window under test ends in `os._exit`: run in-process, a red
    here would take the whole suite down with it rather than fail one test.
    """
    path = Path(directory) / "harness.py"
    path.write_text(
        "import os, signal, subprocess, sys, time\n"
        f"sys.path.insert(0, {str(_RUNNER.parent)!r})\n"
        "import review_runner\n"
        + patch_source +
        "raise SystemExit(review_runner.main(sys.argv[1:]))\n",
        encoding="utf-8")
    return str(path)


_A_REVIEW = ("import json\n"
             'print(json.dumps({"type": "item.completed", "item": '
             '{"type": "agent_message", "text": "the review"}}))\n'
             'print(json.dumps({"type": "turn.completed"}))')


class TheReviewerDoesNotInheritThisSupervisorsSignalMask(unittest.TestCase):
    """Whatever this supervisor blocks for itself, the child inherits across fork and exec.
    A reviewer that starts with SIGTERM blocked cannot shut down when asked, so every
    cancellation waits out the grace period and lands as SIGKILL."""

    @unittest.skipUnless(os.path.exists("/proc/self/status"), "needs /proc to read the mask")
    def test_the_child_starts_with_sigterm_and_sigint_deliverable(self):
        with tempfile.TemporaryDirectory() as d:
            findings = str(Path(d) / "findings.txt")
            child = ("import json\n"
                     "blocked = 0\n"
                     "for line in open('/proc/self/status'):\n"
                     "    if line.startswith('SigBlk:'):\n"
                     "        blocked = int(line.split()[1], 16)\n"
                     "text = 'SIGTERM=%s SIGINT=%s' % (bool(blocked >> 14 & 1), "
                     "bool(blocked >> 1 & 1))\n"
                     'print(json.dumps({"type": "item.completed", "item": '
                     '{"type": "agent_message", "text": text}}))\n'
                     'print(json.dumps({"type": "turn.completed"}))')
            res = _run("--idle", "10", "--deadline", "20", "--findings", findings,
                       "--result-mode", "stream-transcript", "--", PY, "-c", child)
            self.assertEqual(res["status"], "ok", res)
            self.assertEqual(Path(findings).read_text(encoding="utf-8").strip(),
                             "SIGTERM=False SIGINT=False")


class AnInterruptWhileTheChildIsBeingSpawned(unittest.TestCase):
    """The window between `Popen` returning and the child being stored. A handler that
    exits here leaves a reviewer running that nothing can reach: `start_new_session` has
    already detached its signal fate, and neither timer is over it any more."""

    @unittest.skipUnless(os.name == "posix", "delivers a real SIGTERM")
    def test_the_reviewer_is_terminated_and_the_claim_released(self):
        with tempfile.TemporaryDirectory() as d:
            harness = _harness(d,
                "real = subprocess.Popen\n"
                "def popen_then_signal(*a, **kw):\n"
                "    proc = real(*a, **kw)\n"
                "    print('CHILD_PID=%d' % proc.pid, file=sys.stderr, flush=True)\n"
                "    os.kill(os.getpid(), signal.SIGTERM)\n"
                "    time.sleep(0.05)\n"
                "    return proc\n"
                "subprocess.Popen = popen_then_signal\n")
            findings = str(Path(d) / "findings.txt")
            proc = subprocess.run(
                [PY, harness, "--idle", "30", "--deadline", "60", "--findings", findings,
                 "--result-mode", "stream-transcript",
                 "--", PY, "-c", "import time; time.sleep(30)"],
                capture_output=True, text=True, timeout=90)
            status = json.loads(proc.stdout.strip().splitlines()[-1])
            self.assertEqual(status["status"], "error", proc.stdout)
            self.assertIn("interrupted", status["reason"] or "")
            self.assertFalse(Path(findings).exists(), "the claim survived the interrupt")
            pid = int(re.search(r"CHILD_PID=(\d+)", proc.stderr).group(1))
            for _ in range(60):
                try:
                    os.kill(pid, 0)
                except OSError:
                    return
                time.sleep(0.05)
            os.kill(pid, signal.SIGKILL)
            self.fail("the reviewer was left running with nothing supervising it")


class ASignalAtTheFinishLineStillPrintsTheStatus(unittest.TestCase):
    """Between deciding the status and printing it. Exiting here prints nothing at all —
    `os._exit` skips the buffer — and printing from the handler races the real print and
    emits a second line. Exactly one status line is the contract."""

    @unittest.skipUnless(os.name == "posix", "delivers a real SIGTERM")
    def test_one_status_line_survives_a_signal_in_the_last_window(self):
        with tempfile.TemporaryDirectory() as d:
            harness = _harness(d,
                "real_emit = review_runner._emit\n"
                "def emit_after_a_signal(status, **extra):\n"
                "    os.kill(os.getpid(), signal.SIGTERM)\n"
                "    time.sleep(0.05)\n"
                "    return real_emit(status, **extra)\n"
                "review_runner._emit = emit_after_a_signal\n")
            findings = str(Path(d) / "findings.txt")
            proc = subprocess.run(
                [PY, harness, "--idle", "10", "--deadline", "20", "--findings", findings,
                 "--result-mode", "stream-transcript", "--", PY, "-c", _A_REVIEW],
                capture_output=True, text=True, timeout=90)
            lines = [line for line in proc.stdout.splitlines() if line.startswith('{"status"')]
            self.assertEqual(len(lines), 1, f"stdout was {proc.stdout!r}")
            self.assertEqual(json.loads(lines[0])["status"], "ok", proc.stdout)


class AVerdictPathTheClaimSkippedIsNotDeleted(unittest.TestCase):
    """`--verdict-json` whose directory could not be prepared is never claimed, so the
    ending has no business deleting whatever is at that path by the time it finishes."""

    def test_a_file_another_writer_put_there_survives_the_run(self):
        with tempfile.TemporaryDirectory() as d:
            vdir = Path(d) / "vdir"
            vdir.mkdir()
            verdict = vdir / "verdict.json"
            findings = str(Path(d) / "findings.txt")
            real_mkdir = review_runner.Path.mkdir

            def mkdir_but_not_there(self, *a, **kw):
                if str(self) == str(vdir):
                    raise PermissionError(13, "temporarily unavailable")
                return real_mkdir(self, *a, **kw)

            child = (f"open({str(verdict)!r}, 'w').write('{{}}')\n" + _A_REVIEW)
            with unittest.mock.patch.object(review_runner.Path, "mkdir", mkdir_but_not_there):
                res = _run("--idle", "10", "--deadline", "20", "--findings", findings,
                           "--verdict-json", str(verdict), "--result-mode", "stream-transcript",
                           "--", PY, "-c", child)
            self.assertEqual(res["status"], "ok", res)
            self.assertIn("verdict directory", res["verdict_reason"] or "")
            self.assertTrue(verdict.exists(),
                            "a file this run never created was deleted on the way out")


class ABlockingCountThatIntCannotRead(unittest.TestCase):
    """`isdigit()` is not `int()`. Each of these reached `int()` and raised, out of a review
    that had already succeeded, from outside every handler in the program."""

    def test_strings_int_refuses_are_read_as_no_claim(self):
        for text in ("++2", "²", "9" * 5000, " ", "2.5"):
            with self.subTest(text=text):
                verdict = {"findings": [{"severity": "major"}], "blocking_count": text}
                review_runner._reconcile_blocking_count(verdict)  # must not raise
                self.assertEqual(verdict["blocking_count"], 1)

    def test_a_matching_claim_is_published_as_the_integer_it_promises(self):
        verdict = {"findings": [{"severity": "major"}], "blocking_count": "1"}
        self.assertIsNone(review_runner._reconcile_blocking_count(verdict))
        self.assertIsInstance(verdict["blocking_count"], int)
        self.assertEqual(verdict["blocking_count"], 1)

    def test_a_matching_zero_claim_is_published_as_an_integer_too(self):
        verdict = {"findings": [], "blocking_count": "0"}
        self.assertIsNone(review_runner._reconcile_blocking_count(verdict))
        self.assertIsInstance(verdict["blocking_count"], int)


class TheSameDirectoryByIdentityNotBySpelling(unittest.TestCase):
    """What counts as "the current directory" when excluding it from a PATH search."""

    @unittest.skipUnless(os.name == "posix", "a symlink needs privileges on Windows")
    def test_an_alias_is_the_same_directory(self):
        with tempfile.TemporaryDirectory() as d:
            real = Path(d) / "repo"
            real.mkdir()
            alias = Path(d) / "alias"
            os.symlink(real, alias)
            self.assertTrue(review_runner._same_directory(str(alias), str(real)))

    def test_two_directories_differing_only_in_case_are_not_the_same(self):
        with tempfile.TemporaryDirectory() as d:
            upper = Path(d) / "Repo"
            upper.mkdir()
            if (Path(d) / "repo").exists():
                self.skipTest("case-insensitive filesystem, where these ARE one directory")
            lower = Path(d) / "repo"
            lower.mkdir()
            self.assertFalse(
                review_runner._same_directory(str(upper), str(lower)),
                "folding case here excludes a legitimate PATH entry, and the review drops "
                "to a same-model reviewer over a program that was installed all along")


class TheStandAsideAtTheFinishLineIsBounded(unittest.TestCase):
    """Standing aside so the status can print must not make the supervisor unkillable when
    the print itself cannot finish — a stdout nobody drains, say."""

    @unittest.skipUnless(os.name == "posix", "delivers real signals")
    def test_a_second_signal_ends_a_run_whose_print_never_completes(self):
        with tempfile.TemporaryDirectory() as d:
            harness = _harness(d,
                "real_emit = review_runner._emit\n"
                "def emit_that_stalls(status, **extra):\n"
                "    os.kill(os.getpid(), signal.SIGTERM)\n"
                "    time.sleep(0.05)\n"
                "    os.kill(os.getpid(), signal.SIGTERM)\n"
                "    time.sleep(5)\n"
                "    return real_emit(status, **extra)\n"
                "review_runner._emit = emit_that_stalls\n")
            findings = str(Path(d) / "findings.txt")
            proc = subprocess.run(
                [PY, harness, "--idle", "10", "--deadline", "20", "--findings", findings,
                 "--result-mode", "stream-transcript", "--", PY, "-c", _A_REVIEW],
                capture_output=True, text=True, timeout=90)
            self.assertEqual(proc.returncode, 1, proc.stdout)
            self.assertNotIn('{"status"', proc.stdout,
                             "the second signal was ignored as well, so a caller that "
                             "insists cannot cancel this run at all")


class AnInterruptWhileTheOutputsAreBeingClaimed(unittest.TestCase):
    """The claim creates a file and then records it. A handler that exits in between cannot
    see what it must remove, and the leftover refuses the retry. Green before the change on
    POSIX, where a mask covered it; this is what covers Windows, and what guards the
    replacement everywhere."""

    @unittest.skipUnless(os.name == "posix", "delivers a real SIGTERM")
    def test_nothing_is_left_behind_and_one_status_line_is_printed(self):
        with tempfile.TemporaryDirectory() as d:
            harness = _harness(d,
                "real_open = os.open\n"
                "def open_then_signal(path, flags, *a, **kw):\n"
                "    fd = real_open(path, flags, *a, **kw)\n"
                "    if flags & os.O_EXCL:\n"
                "        os.kill(os.getpid(), signal.SIGTERM)\n"
                "        time.sleep(0.05)\n"
                "    return fd\n"
                "os.open = open_then_signal\n")
            findings = str(Path(d) / "findings.txt")
            proc = subprocess.run(
                [PY, harness, "--idle", "10", "--deadline", "20", "--findings", findings,
                 "--result-mode", "stream-transcript", "--", PY, "-c", _A_REVIEW],
                capture_output=True, text=True, timeout=90)
            lines = [line for line in proc.stdout.splitlines() if line.startswith('{"status"')]
            self.assertEqual(len(lines), 1, f"stdout was {proc.stdout!r}")
            self.assertIn("interrupted", json.loads(lines[0])["reason"] or "")
            self.assertFalse(Path(findings).exists(), "the claim survived the interrupt")


class ThePreflightAnswersBeforeAnythingExists(unittest.TestCase):
    """Everything the pre-flight decides is decided before a file is created and before a
    handler is armed. That is what lets it refuse by raising: there is nothing to clean up,
    and `run` alone prints the status line."""

    def test_a_clean_request_resolves_the_program_and_makes_the_output_directory(self):
        with tempfile.TemporaryDirectory() as d:
            findings = Path(d) / "nested" / "deeper" / "findings.txt"
            args = _parsed("--idle", "5", "--deadline", "10", "--findings", str(findings),
                           "--result-mode", "stream-transcript",
                           "--", PY, "-c", "pass")
            cmd, verdict_unavailable = review_runner._preflight(args)
            self.assertEqual(cmd[0], os.path.abspath(PY))
            self.assertEqual(cmd[1:], ["-c", "pass"])
            self.assertIsNone(verdict_unavailable)
            self.assertTrue(findings.parent.is_dir(), "the findings directory is essential")

    def test_each_refusal_raises_with_the_reason_a_caller_would_read(self):
        with tempfile.TemporaryDirectory() as d:
            existing = Path(d) / "already.txt"
            existing.write_text("someone else's\n", encoding="utf-8")
            one = str(Path(d) / "one.txt")
            cases = [
                (("--findings", one, "--verdict-json", one), "name the same path"),
                (("--cwd", d, "--findings", "relative.txt"), "is relative"),
                (("--findings", str(existing)), "already exists"),
                (("--idle", "0", "--findings", one), "finite positive"),
                (("--findings", one), "no reviewer command given"),
            ]
            for extra, expected in cases:
                with self.subTest(expected=expected):
                    argv = ["--deadline", "10",
                            "--result-mode", "stream-transcript", *extra]
                    if expected != "no reviewer command given":
                        argv += ["--", PY, "-c", "pass"]
                    else:
                        argv += ["--"]
                    with self.assertRaises(review_runner._Refused) as caught:
                        review_runner._preflight(_parsed(*argv))
                    self.assertIn(expected, str(caught.exception))

    def test_a_program_that_is_not_installed_is_refused_by_name(self):
        with tempfile.TemporaryDirectory() as d:
            args = _parsed("--idle", "5", "--deadline", "10",
                           "--findings", str(Path(d) / "findings.txt"),
                           "--result-mode", "stream-transcript",
                           "--", "definitely-not-installed-abcxyz")
            with self.assertRaises(review_runner._Refused) as caught:
                review_runner._preflight(args)
            self.assertIn("not found on PATH", str(caught.exception))

    def test_a_verdict_directory_it_cannot_make_is_reported_not_raised(self):
        with tempfile.TemporaryDirectory() as d:
            blocker = Path(d) / "blocker"
            blocker.write_text("not a directory\n", encoding="utf-8")
            args = _parsed("--idle", "5", "--deadline", "10",
                           "--findings", str(Path(d) / "findings.txt"),
                           "--verdict-json", str(blocker / "verdict.json"),
                           "--result-mode", "stream-transcript",
                           "--", PY, "-c", "pass")
            cmd, verdict_unavailable = review_runner._preflight(args)
            self.assertIsNotNone(cmd)
            self.assertIn("verdict directory", verdict_unavailable or "")

    def test_a_refusal_creates_nothing(self):
        with tempfile.TemporaryDirectory() as d:
            findings = Path(d) / "nested" / "findings.txt"
            args = _parsed("--idle", "0", "--deadline", "10", "--findings", str(findings),
                           "--result-mode", "stream-transcript",
                           "--", PY, "-c", "pass")
            with self.assertRaises(review_runner._Refused):
                review_runner._preflight(args)
            self.assertFalse(findings.parent.exists(),
                             "a refused request left a directory behind")


def _stream_state(**over):
    """The reader threads' shared record, as the outcome step finds it."""
    base = {"last_activity": 0.0, "last_result": None, "transcript": [],
            "terminal": None, "terminal_event": None}
    base.update(over)
    return base


class TheOutcomeIsDecidedFromWhatArrived(unittest.TestCase):
    """`_route_outcome` turns "the child exited" into a status and a findings file. The three
    result modes disagree about what counts as the review, and this is the only step that
    knows the difference, and these reach it without a whole supervised run."""

    def _args(self, directory, mode="stream-transcript"):
        return _parsed("--idle", "5", "--deadline", "10",
                       "--findings", str(Path(directory) / "findings.txt"),
                       "--result-mode", mode, "--", PY, "-c", "pass")

    def test_a_complete_transcript_is_written_and_reported_ok(self):
        with tempfile.TemporaryDirectory() as d:
            args = self._args(d)
            status, reason, text, _partial = review_runner._route_outcome(
                args, "ok", None, True, 0,
                _stream_state(transcript=["the review"], terminal="ok"), threading.Lock())
            self.assertEqual((status, reason), ("ok", None))
            self.assertEqual(text, "the review")
            self.assertEqual(Path(args.findings).read_text(encoding="utf-8"), "the review\n")

    def test_an_undrained_pipe_is_fatal_only_when_the_terminal_event_never_arrived(self):
        """The rule DR12 asked for, asked of the step that decides it."""
        for terminal, expected in (("ok", "ok"), (None, "error")):
            with self.subTest(terminal=terminal), tempfile.TemporaryDirectory() as d:
                args = self._args(d)
                status, reason, _, _partial = review_runner._route_outcome(
                    args, "ok", None, False, 0,
                    _stream_state(transcript=["the review"], terminal=terminal),
                    threading.Lock())
                self.assertEqual(status, expected, reason)
                if expected == "error":
                    self.assertIn("did not drain", reason)

    def test_an_empty_file_in_external_file_mode_is_no_verdict(self):
        with tempfile.TemporaryDirectory() as d:
            args = self._args(d, mode="external-file")
            Path(args.findings).touch()
            status, reason, text, _partial = review_runner._route_outcome(
                args, "ok", None, True, 0, _stream_state(), threading.Lock())
            self.assertEqual(status, "error")
            self.assertIn("wrote no verdict", reason)
            self.assertIsNone(text)

    def test_a_findings_path_that_cannot_be_written_is_a_routing_failure(self):
        with tempfile.TemporaryDirectory() as d:
            args = self._args(d)
            Path(args.findings).mkdir()  # a directory standing where the file should go
            status, reason, _, _partial = review_runner._route_outcome(
                args, "ok", None, True, 0,
                _stream_state(transcript=["the review"], terminal="ok"), threading.Lock())
            self.assertEqual(status, "error")
            self.assertIn("routing failed", reason)

    def test_a_reviewer_that_exited_badly_keeps_its_own_reason(self):
        with tempfile.TemporaryDirectory() as d:
            args = self._args(d)
            status, reason, _, _partial = review_runner._route_outcome(
                args, "ok", None, True, 3,
                _stream_state(transcript=["the review"], terminal="ok"), threading.Lock())
            self.assertEqual(status, "error")
            self.assertIn("exited 3", reason)


class TheVerdictIsPublishedBesideTheFindings(unittest.TestCase):
    """Additive and never instead: every way this step can fail is a reason, not a status."""

    def _args(self, directory):
        return _parsed("--idle", "5", "--deadline", "10",
                       "--findings", str(Path(directory) / "findings.txt"),
                       "--verdict-json", str(Path(directory) / "verdict.json"),
                       "--result-mode", "stream-transcript", "--", PY, "-c", "pass")

    def test_a_verdict_in_the_narrative_is_written_out(self):
        with tempfile.TemporaryDirectory() as d:
            args = self._args(d)
            path, _reason = review_runner._publish_verdict(
                args, "ok", None, VERDICT, _stream_state(), threading.Lock())
            self.assertEqual(path, args.verdict_json)
            written = json.loads(Path(path).read_text(encoding="utf-8"))
            for key in ("findings", "overall", "blocking_count"):
                self.assertIn(key, written)

    def test_a_narrative_with_no_verdict_object_is_a_reason(self):
        with tempfile.TemporaryDirectory() as d:
            args = self._args(d)
            path, reason = review_runner._publish_verdict(
                args, "ok", None, "prose, and no object anywhere in it",
                _stream_state(), threading.Lock())
            self.assertIsNone(path)
            self.assertIn("no verdict object", reason)
            self.assertFalse(Path(args.verdict_json).exists())

    def test_a_directory_it_could_not_prepare_is_carried_through_untouched(self):
        with tempfile.TemporaryDirectory() as d:
            args = self._args(d)
            already = "could not create the verdict directory: [Errno 13] nope"
            path, reason = review_runner._publish_verdict(
                args, "ok", already, VERDICT, _stream_state(), threading.Lock())
            self.assertIsNone(path)
            self.assertEqual(reason, already)
            self.assertFalse(Path(args.verdict_json).exists())

    def test_a_failed_review_publishes_no_verdict(self):
        with tempfile.TemporaryDirectory() as d:
            args = self._args(d)
            path, reason = review_runner._publish_verdict(
                args, "error", None, VERDICT, _stream_state(), threading.Lock())
            self.assertIsNone(path)
            self.assertIsNone(reason)
            self.assertFalse(Path(args.verdict_json).exists())


def _reviewer(script):
    """A real child, with the pipes and the session the supervisor gives one."""
    extra = {"start_new_session": True} if os.name == "posix" else {}
    return subprocess.Popen([PY, "-c", script], stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, stdin=subprocess.DEVNULL,
                            bufsize=0, **extra)


class TheStreamWatchesTheReviewerWhileItRuns(unittest.TestCase):
    """`_Stream` is the reviewer's output while it runs: both reader threads, the record they
    fill in, and the display log they tee to. The two clocks over it — silence, and total
    time — are what these ask, without a whole supervised review."""

    def _args(self, directory, display=None, mode="stream-transcript"):
        argv = ["--idle", "5", "--deadline", "30",
                "--findings", str(Path(directory) / "findings.txt"),
                "--result-mode", mode]
        if display:
            argv += ["--display", display]
        return _parsed(*argv, "--", PY, "-c", "pass")

    def test_the_idle_clock_starts_with_the_readers_not_at_construction(self):
        """Opening the display log is I/O of unknown duration — a network path, a large
        append. Charged to the child as silence, it spends the first idle window before the
        child can speak into it, and a slow log then kills a healthy review.

        Asserted on the state rather than by timing a deliberately slow open, which would be
        a race dressed up as a test.
        """
        with tempfile.TemporaryDirectory() as d:
            proc = _reviewer("import time; time.sleep(5)")
            self.addCleanup(proc.kill)
            stream = review_runner._Stream(proc, self._args(d))
            stream.state["last_activity"] = 0.0  # as if construction were long ago
            before = time.monotonic()
            stream.start()
            self.assertGreaterEqual(
                stream.state["last_activity"], before,
                "the idle clock still starts at construction, so display setup is charged "
                "to the child as silence it never had a chance to break")
            self.assertAlmostEqual(stream.state["last_activity"], stream.started,
                                   delta=0.5, msg="the two clocks must start together")

    def test_silence_past_the_idle_window_is_an_idle_timeout(self):
        with tempfile.TemporaryDirectory() as d:
            stream = review_runner._Stream(_reviewer("import time; time.sleep(30)"),
                                           self._args(d))
            stream.start()
            status, reason = stream.watch(0.5, 30)
            drained, exit_code = stream.settle(status)
            self.assertEqual(status, "idle_timeout", reason)
            self.assertIn("no output for", reason)
            self.assertTrue(drained, "the reader never reached EOF after the kill")
            self.assertIsNotNone(exit_code, "the reviewer was left running")

    def test_a_reviewer_that_never_stops_talking_still_meets_the_deadline(self):
        """The two clocks are independent: output resets one and never touches the other."""
        with tempfile.TemporaryDirectory() as d:
            chatty = ("import time\n"
                      "while True:\n"
                      "    print('tick', flush=True)\n"
                      "    time.sleep(0.05)\n")
            stream = review_runner._Stream(_reviewer(chatty), self._args(d))
            stream.start()
            status, reason = stream.watch(30, 0.6)
            stream.settle(status)
            self.assertEqual(status, "deadline", reason)
            self.assertIn("no completion within", reason)

    def test_what_arrived_is_recorded_and_the_log_brackets_it(self):
        with tempfile.TemporaryDirectory() as d:
            log = Path(d) / "display.log"
            events = ("import json\n"
                      'print(json.dumps({"type": "item.completed", "item": '
                      '{"type": "agent_message", "text": "the review"}}), flush=True)\n'
                      'print(json.dumps({"type": "turn.completed"}), flush=True)\n')
            stream = review_runner._Stream(_reviewer(events),
                                           self._args(d, display=str(log)))
            stream.start()
            status, reason = stream.watch(10, 30)
            drained, exit_code = stream.settle(status)
            self.assertEqual((status, drained, exit_code), ("ok", True, 0), reason)
            self.assertEqual(stream.state["transcript"], ["the review"])
            self.assertEqual(stream.state["terminal"], "ok")
            text = log.read_text(encoding="utf-8")
            self.assertIn("[review_runner] start", text)
            self.assertIn("end status=ok exit=0 drained=True", text)

    def test_a_display_log_it_cannot_open_costs_only_the_log(self):
        with tempfile.TemporaryDirectory() as d:
            blocker = Path(d) / "blocker"
            blocker.write_text("not a directory\n", encoding="utf-8")
            stream = review_runner._Stream(_reviewer("print('hello', flush=True)"),
                                           self._args(d, display=str(blocker / "log")))
            self.assertIsNone(stream.display_fh, "a log it cannot open is not a log")
            stream.start()
            status, reason = stream.watch(10, 30)
            drained, _exit_code = stream.settle(status)
            self.assertEqual(status, "ok", reason)
            self.assertTrue(drained)


class TheInterruptsObjectOwnsTheOneStatusLine(unittest.TestCase):
    """Cancellation in one place: what to terminate, what to remove, and the rule that exactly
    one JSON status line is printed however the run ends. That rule broke three times while it
    was spread across `run` — printing nothing, printing twice, and printing before a handler
    could know a status existed — so these ask the object for it directly."""

    def _guard(self):
        guard = review_runner._Interrupts()
        self.addCleanup(guard.restore)  # never leave the runner holding our handler
        return guard

    def test_reporting_prints_one_line_then_disarms(self):
        guard = self._guard()
        before = signal.getsignal(signal.SIGTERM)
        guard.arm()
        self.assertIsNot(signal.getsignal(signal.SIGTERM), before, "arm installed nothing")
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = guard.report("ok", reason=None, findings="/tmp/findings.txt")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(buf.getvalue().strip())["status"], "ok")
        self.assertEqual(len(buf.getvalue().strip().splitlines()), 1)
        self.assertTrue(guard.reported)
        self.assertEqual(guard.payload["status"], "ok",
                         "the payload must be stored before the print, or a signal in that "
                         "window ends a finished review in silence")
        self.assertIs(signal.getsignal(signal.SIGTERM), before, "the handlers stayed armed")

    def test_a_print_that_raises_still_restores_the_handlers(self):
        """An in-process caller must not be left holding a handler that ignores cancellation
        because stdout happened to be closed."""
        guard = self._guard()
        before = signal.getsignal(signal.SIGTERM)
        guard.arm()
        with unittest.mock.patch.object(review_runner, "_emit",
                                        side_effect=BrokenPipeError(32, "closed")):
            with self.assertRaises(BrokenPipeError):
                guard.report("ok")
        self.assertIs(signal.getsignal(signal.SIGTERM), before,
                      "a raising print left the handlers installed")
        self.assertTrue(guard.reported)

    def test_release_removes_what_was_claimed_and_nothing_else(self):
        with tempfile.TemporaryDirectory() as d:
            mine, theirs = Path(d) / "mine.txt", Path(d) / "theirs.txt"
            mine.write_text("mine\n", encoding="utf-8")
            theirs.write_text("someone else's\n", encoding="utf-8")
            guard = self._guard()
            guard.claimed(str(mine))
            guard.claimed(str(Path(d) / "never-created.txt"))  # already gone is not an error
            guard.release()
            self.assertFalse(mine.exists())
            self.assertTrue(theirs.exists(), "it removed a file it never created")

    def test_a_deferred_signal_is_reported_and_the_claims_released(self):
        with tempfile.TemporaryDirectory() as d:
            claimed = Path(d) / "findings.txt"
            claimed.write_text("half a review\n", encoding="utf-8")
            guard = self._guard()
            guard.arm()
            guard.claimed(str(claimed))
            guard.interrupted = signal.SIGTERM
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                code = guard.deferred(None)
            self.assertEqual(code, 1)
            line = json.loads(buf.getvalue().strip())
            self.assertEqual(line["status"], "error")
            self.assertIn("interrupted by SIGTERM", line["reason"])
            self.assertFalse(claimed.exists(), "an interrupted run left its claim behind")


def _transcript_event(text):
    return json.dumps({"type": "assistant",
                       "message": {"content": [{"type": "text", "text": text}]}})


def _child_printing(*lines):
    """A reviewer that prints the given JSONL lines and exits."""
    return "import sys\n" + "".join(
        f"sys.stdout.write({line!r} + chr(10))\nsys.stdout.flush()\n" for line in lines)


class TheStatusLineKeepsItsShapeUnlessAsked(unittest.TestCase):
    """The compatibility half of the two opt-in flags.

    Both add a key to the one line this program contracts to print, and a caller that reads
    that line as a fixed record would have to absorb the change. So neither is on by
    default, and this is the assertion that says so — key for key, not "roughly the same".
    """

    HISTORICAL = {"status", "reason", "exit_code", "elapsed_s", "findings"}

    def _ok_run(self, directory, *extra):
        child = _child_printing(_transcript_event('{"verdict": "fine"}'),
                                json.dumps({"type": "turn.completed"}))
        return _run("--idle", "10", "--deadline", "20",
                    "--findings", str(Path(directory) / "findings.txt"),
                    "--result-mode", "stream-transcript", *extra, "--", PY, "-c", child)

    def test_a_caller_passing_neither_flag_gets_the_keys_it_always_got(self):
        with tempfile.TemporaryDirectory() as d:
            line = self._ok_run(d)
            self.assertEqual(line["status"], "ok", line)
            self.assertEqual(set(line), self.HISTORICAL,
                             "the status line grew a key for a caller that asked for none")

    def test_the_verdict_keys_are_still_the_only_other_addition(self):
        with tempfile.TemporaryDirectory() as d:
            line = self._ok_run(d, "--verdict-json", str(Path(d) / "verdict.json"))
            self.assertEqual(set(line), self.HISTORICAL | {"verdict", "verdict_reason"})

    def test_each_flag_adds_exactly_its_own_keys(self):
        with tempfile.TemporaryDirectory() as d:
            detail = self._ok_run(d, "--status-detail")
            self.assertEqual(set(detail),
                             self.HISTORICAL | {"terminal_detail",
                                                "terminal_detail_source"})
        with tempfile.TemporaryDirectory() as d:
            capped = self._ok_run(d, "--max-capture-bytes", "100000")
            self.assertEqual(set(capped), self.HISTORICAL | {"capture_truncated"})
            self.assertFalse(capped["capture_truncated"])

    def test_a_cap_of_zero_is_refused_rather_than_acted_on(self):
        with tempfile.TemporaryDirectory() as d:
            line = _run("--idle", "10", "--deadline", "20",
                        "--findings", str(Path(d) / "findings.txt"),
                        "--result-mode", "stream-transcript",
                        "--max-capture-bytes", "0", "--", PY, "-c", "pass")
            self.assertEqual(line["status"], "error")
            self.assertIn("--max-capture-bytes", line["reason"])


class TheTerminalEventsOwnErrorText(unittest.TestCase):
    """`--status-detail`, and the contract it extracts by.

    A caller telling a provider outage apart from a bad answer reads this field, so what it
    holds has to be stated rather than discovered: the top-level message, then a nested
    error's message, then the event type alone. An absent detail is its own answer — the
    failure explained itself nowhere — and these hold that apart from the rest.
    """

    def test_the_top_level_message_wins(self):
        detail = review_runner._terminal_detail(
            {"type": "turn.failed", "message": "quota exhausted",
             "error": {"message": "something else"}})
        self.assertEqual(detail, "quota exhausted")

    def test_a_nested_errors_message_is_next(self):
        detail = review_runner._terminal_detail(
            {"type": "turn.failed", "error": {"message": "rate limit reached"}})
        self.assertEqual(detail, "rate limit reached")

    def test_the_event_type_alone_is_the_last_resort(self):
        self.assertEqual(review_runner._terminal_detail({"type": "turn.failed"}),
                         "turn.failed")

    def test_no_event_at_all_is_null(self):
        self.assertIsNone(review_runner._terminal_detail(None))
        self.assertIsNone(review_runner._terminal_detail({}))

    def test_the_detail_is_bounded(self):
        detail = review_runner._terminal_detail({"type": "x", "message": "e" * 50_000})
        self.assertLessEqual(len(detail.encode("utf-8")),
                             review_runner.TERMINAL_DETAIL_MAX_BYTES + 4)
        self.assertTrue(detail.endswith("…"), "nothing says the text was cut")

    def test_a_failure_that_explained_itself_nowhere_reports_null(self):
        """The shape an outage takes today: the CLI writes its reason to stderr, which
        reaches the display log alone, and exits non-zero. A caller must be able to see that
        the failure named nothing — it is what tells an unclassifiable failure from a
        classified one."""
        with tempfile.TemporaryDirectory() as d:
            child = "import sys; sys.stderr.write('usage limit\\n'); sys.exit(1)"
            line = _run("--idle", "10", "--deadline", "20",
                        "--findings", str(Path(d) / "findings.txt"),
                        "--result-mode", "stream-transcript", "--status-detail",
                        "--", PY, "-c", child)
            self.assertEqual(line["status"], "error")
            self.assertIn("terminal_detail", line)
            self.assertIsNone(line["terminal_detail"])
            self.assertIsNone(line["terminal_detail_source"],
                              "a failure with no terminal event named a rule that answered")

    def test_the_source_names_which_rule_answered(self):
        """The text alone cannot be classified: the last rule's answer is a NAME, and a
        caller that reads a name as the failure's account of itself can never see a failure
        that explained itself nowhere but stderr."""
        parts = review_runner._terminal_detail_parts
        self.assertEqual(parts({"type": "turn.failed", "message": "quota exhausted",
                                "error": {"message": "something else"}}),
                         ("quota exhausted", "message"))
        self.assertEqual(parts({"type": "turn.failed", "error": {"message": "rate limit"}}),
                         ("rate limit", "error"))
        self.assertEqual(parts({"type": "turn.failed"}), ("turn.failed", "event-type"))
        self.assertEqual(parts({}), (None, None))
        self.assertEqual(parts(None), (None, None))

    def test_an_event_with_no_error_text_says_on_the_wire_that_it_is_a_name(self):
        """End to end, because this is the pair a caller's breaker reads: an outage whose
        CLI wrote its reason to stderr leaves a bare terminal event, and the status line has
        to say that `turn.failed` is the event's name rather than its explanation."""
        with tempfile.TemporaryDirectory() as d:
            child = _child_printing(_transcript_event("half a review"),
                                    json.dumps({"type": "turn.failed"}))
            line = _run("--idle", "10", "--deadline", "20",
                        "--findings", str(Path(d) / "findings.txt"),
                        "--result-mode", "stream-transcript", "--status-detail",
                        "--", PY, "-c", child)
            self.assertEqual(line["status"], "error")
            self.assertEqual(line["terminal_detail"], "turn.failed")
            self.assertEqual(line["terminal_detail_source"], "event-type",
                             "an event name was reported as the failure's own error text")

    def test_a_terminal_failure_event_carries_its_own_text_through(self):
        with tempfile.TemporaryDirectory() as d:
            child = _child_printing(
                _transcript_event("half a review"),
                json.dumps({"type": "turn.failed",
                            "error": {"message": "usage limit reached for this account"}}))
            line = _run("--idle", "10", "--deadline", "20",
                        "--findings", str(Path(d) / "findings.txt"),
                        "--result-mode", "stream-transcript", "--status-detail",
                        "--", PY, "-c", child)
            self.assertEqual(line["status"], "error")
            self.assertEqual(line["terminal_detail"],
                             "usage limit reached for this account")


class WhatIsRetainedIsBounded(unittest.TestCase):
    """`--max-capture-bytes`, and the one rule everything here turns on: reviewer text is
    dropped from the FRONT with the notice PREPENDED.

    Appending it would break every capped reply that was otherwise intact, because the
    answer is the last non-whitespace content of the transcript and anything after it means
    there is no answer at all. These are the cases where a plausible implementation lands
    the wrong answer rather than no answer."""

    def _run_capped(self, directory, child, cap, *extra):
        findings = Path(directory) / "findings.txt"
        line = _run("--idle", "10", "--deadline", "30", "--findings", str(findings),
                    "--result-mode", "stream-transcript", "--max-capture-bytes", str(cap),
                    "--status-detail", *extra, "--", PY, "-c", child)
        text = findings.read_text(encoding="utf-8") if findings.exists() else None
        return line, text

    @staticmethod
    def _closing_object(text):
        """The JSON object that is the last non-whitespace content, or None — the rule the
        caller lands a reply by, restated here so these tests assert what it will see."""
        tail = text.rstrip()
        if not tail.endswith("}"):
            return None
        decoder = json.JSONDecoder()
        index = tail.find("{")
        while index != -1:
            try:
                value, end = decoder.raw_decode(tail, index)
            except ValueError:
                value, end = None, -1
            if isinstance(value, dict) and end == len(tail):
                return value
            index = tail.find("{", index + 1)
        return None

    def test_an_oversized_reply_keeps_its_closing_object_and_says_it_was_cut(self):
        """Several lines, each inside the cap, whose text together is over it: the bound
        that applies is the transcript's, and it drops from the front. (A single line over
        the cap is a different case — refused whole, below.)"""
        with tempfile.TemporaryDirectory() as d:
            child = _child_printing(
                *(_transcript_event("x" * 800) for _ in range(4)),
                _transcript_event(json.dumps({"findings": [], "summary": "done"})),
                json.dumps({"type": "turn.completed"}))
            line, text = self._run_capped(d, child, 2000)
            self.assertEqual(line["status"], "ok", line)
            self.assertTrue(line["capture_truncated"])
            self.assertEqual(self._closing_object(text),
                             {"findings": [], "summary": "done"},
                             "the marker was appended, or the tail was dropped instead of "
                             "the front — either way the reply no longer lands")
            self.assertTrue(text.startswith("[review_runner]"),
                            "the retained tail does not say it is only a tail")

    def test_a_closing_object_over_the_cap_is_refused_and_never_promotes_an_earlier_one(self):
        """Losing later content must never promote an earlier one. The real answer is one
        line over the cap, so the run ends as an overflow: no findings are published, and
        in particular the earlier example object is never left on disk as the reply."""
        with tempfile.TemporaryDirectory() as d:
            earlier = json.dumps({"findings": [], "summary": "an example, not the answer"})
            child = _child_printing(
                _transcript_event(earlier),
                _transcript_event(json.dumps({"findings": ["y" * 4000], "summary": "real"})),
                json.dumps({"type": "turn.completed"}))
            line, text = self._run_capped(d, child, 1000)
            self.assertEqual(line["status"], "error", line)
            self.assertIn("capture overflow", line["reason"])
            self.assertIsNone(text, "an earlier object was published as the reply")
            partial = Path(line["partial_findings"]).read_text(encoding="utf-8")
            self.assertIn("an example, not the answer", partial,
                          "the text that fit was not preserved beside the failure")

    def test_a_line_that_never_ends_is_an_overflow_not_a_shorter_transcript(self):
        with tempfile.TemporaryDirectory() as d:
            child = ("import sys, time\n"
                     "sys.stdout.write('{' + 'z' * 300000)\n"
                     "sys.stdout.flush()\n"
                     "time.sleep(20)\n")
            line, _text = self._run_capped(d, child, 1000)
            self.assertEqual(line["status"], "error")
            self.assertIn("capture overflow", line["reason"])

    def test_an_oversized_terminal_event_is_an_overflow_like_any_other_line(self):
        """The terminal event is one JSONL line, and a line over the cap is refused whole:
        keeping a head of it would report a run as complete out of an event this program
        declined to hold."""
        with tempfile.TemporaryDirectory() as d:
            child = _child_printing(
                _transcript_event(json.dumps({"findings": [], "summary": "done"})),
                json.dumps({"type": "result", "subtype": "success", "is_error": False,
                            "message": "the model stopped early",
                            "structured_output": {"padding": "q" * 6000}}))
            line, _text = self._run_capped(d, child, 2500)
            self.assertEqual(line["status"], "error", line)
            self.assertIn("capture overflow", line["reason"])

    def test_a_line_over_the_cap_ends_the_run_however_its_bytes_arrived(self):
        """The rule is about the LINE, not about the read that delivered it. A pipe hands
        the reader whatever bytes are there, so the same over-cap line can arrive whole in
        one read or split across two — and a bound judged only on the pending fragment
        answers differently to the two, keeping the line as a trimmed tail in the first case
        and ending the run in the second. One reviewer output, one outcome."""
        payload = "x" * 3000 + json.dumps({"findings": [], "summary": "done"})
        event = _transcript_event(payload)
        whole = _child_printing(event, json.dumps({"type": "turn.completed"}))
        split = ("import sys, time\n"
                 f"sys.stdout.write({event[:1500]!r})\n"
                 "sys.stdout.flush()\n"
                 "time.sleep(1.0)\n"
                 f"sys.stdout.write({event[1500:]!r} + chr(10))\n"
                 f"sys.stdout.write({json.dumps({'type': 'turn.completed'})!r} + chr(10))\n"
                 "sys.stdout.flush()\n")
        for delivery, child in (("one write", whole), ("two writes", split)):
            with self.subTest(delivery=delivery), tempfile.TemporaryDirectory() as d:
                line, _text = self._run_capped(d, child, 1000)
                self.assertEqual(line["status"], "error",
                                 f"delivered in {delivery}, an over-cap line was kept: {line}")
                self.assertIn("capture overflow", line["reason"])

    def test_trailing_malformed_output_does_not_take_the_capture_with_it(self):
        with tempfile.TemporaryDirectory() as d:
            child = _child_printing(
                _transcript_event(json.dumps({"findings": [], "summary": "done"})),
                json.dumps({"type": "turn.completed"}),
                "{not json at all", "")
            line, text = self._run_capped(d, child, 100000)
            self.assertEqual(line["status"], "ok", line)
            self.assertEqual(self._closing_object(text),
                             {"findings": [], "summary": "done"})

    def test_a_cut_landing_inside_a_character_leaves_no_broken_one(self):
        """The trim is measured in bytes and the text is characters, so the cut lands
        mid-character as readily as anywhere else. A fragment rendered as U+FFFD would
        reach a human as mojibake and, worse, could grow the retained tail past the cap it
        was being trimmed to."""
        state = {"transcript": ["é" * 500, '{"a": 1}'], "capture_cap": 301,
                 "transcript_bytes": 1000 + len('{"a": 1}')}
        review_runner._bound_transcript(state)
        kept = "".join(state["transcript"])
        self.assertNotIn("�", kept, "the cut left a broken character behind")
        self.assertTrue(kept.endswith('{"a": 1}'), "the cut took the tail, not the front")
        self.assertLessEqual(len(kept.encode("utf-8")), 301)
        self.assertTrue(state["capture_truncated"])

    # -- the other two result modes ------------------------------------------ #
    def _run_capped_mode(self, directory, child, cap, mode, findings_name="findings.txt"):
        findings = Path(directory) / findings_name
        line = _run("--idle", "10", "--deadline", "30", "--findings", str(findings),
                    "--result-mode", mode, "--max-capture-bytes", str(cap),
                    "--status-detail", "--", PY, "-c", child)
        text = findings.read_text(encoding="utf-8") if findings.exists() else None
        return line, text

    def test_an_oversized_result_event_is_an_overflow_in_this_mode_too(self):
        """The bound has to reach every mode that retains something, and this mode retains
        the whole terminal event: `--findings` is written from its payload, so an event held
        whole is the reviewer's entire output in this process's memory while the flag that
        asked for a bound reports nothing dropped. The event is one line, and the reader
        refuses a line over the cap before any mode sees it.
        """
        with tempfile.TemporaryDirectory() as d:
            payload = "x" * 5000 + json.dumps({"findings": [], "summary": "done"})
            child = _child_printing(json.dumps(
                {"type": "result", "subtype": "success", "is_error": False,
                 "result": payload}))
            line, text = self._run_capped_mode(d, child, 2000,
                                               "stream-json-result-event")
            self.assertEqual(line["status"], "error", line)
            self.assertIn("capture overflow", line["reason"])
            self.assertIsNone(text, "a payload this program refused to hold was published")

    def test_a_result_event_inside_the_cap_is_retained_exactly_as_it_arrived(self):
        """The other half: the bound is a bound and not a rewrite. A reply that fits keeps
        its bytes, and nothing says anything was dropped."""
        with tempfile.TemporaryDirectory() as d:
            payload = json.dumps({"findings": [], "summary": "done"})
            child = _child_printing(json.dumps(
                {"type": "result", "subtype": "success", "is_error": False,
                 "result": payload}))
            line, text = self._run_capped_mode(d, child, 2000,
                                               "stream-json-result-event")
            self.assertEqual(line["status"], "ok", line)
            self.assertFalse(line["capture_truncated"])
            self.assertEqual(text, payload)

    def test_a_verdict_file_larger_than_the_cap_is_read_by_its_tail_and_left_alone(self):
        """external-file mode's answer is the file the reviewer wrote, and this program does
        not own it: it is not rewritten, and the caller reads all of it. What the cap bounds
        is what this process HOLDS — read whole, a findings file of any size arrives in
        memory here. The tail is what is read, because the verdict object a scan is looking
        for is the file's last content.
        """
        with tempfile.TemporaryDirectory() as d:
            findings = Path(d) / "findings.txt"
            verdict = {"findings": [], "overall": "ship it", "blocking_count": 0}
            body = ("y" * 20000) + "\n" + json.dumps(verdict)
            child = ("import pathlib\n"
                     f"pathlib.Path({str(findings)!r}).write_text({body!r}, "
                     "encoding='utf-8')\n")
            line = _run("--idle", "10", "--deadline", "30", "--findings", str(findings),
                        "--verdict-json", str(Path(d) / "verdict.json"),
                        "--result-mode", "external-file", "--max-capture-bytes", "1000",
                        "--status-detail", "--", PY, "-c", child)
            self.assertEqual(line["status"], "ok", line)
            self.assertEqual(findings.read_text(encoding="utf-8"), body,
                             "the reviewer's own file was rewritten")
            # What the cap is FOR, asserted at the read itself: nothing else on this path
            # can show it, because the file on disk is complete either way and a status
            # line cannot say how much memory this process was holding.
            held = review_runner._read_verdict_file(findings, findings.stat().st_size,
                                                    1000)
            self.assertLessEqual(len(held.encode("utf-8")), 1000,
                                 "a 20,000-byte file was held whole against a 1,000-byte "
                                 "cap")
            self.assertTrue(held.rstrip().endswith("}"),
                            "the head was kept, so the object a scan looks for is gone")
            # The scan still finds the object, because the tail is where it is.
            self.assertEqual(line["verdict"], str(Path(d) / "verdict.json"),
                             line.get("verdict_reason"))
            self.assertFalse(line["capture_truncated"],
                             "the published answer is the whole file, so nothing was cut "
                             "from what a caller reads")

    def test_a_verdict_file_within_the_cap_is_read_whole(self):
        """The half that keeps the bound from becoming a shortener: a file under the cap is
        read exactly as it was, by the same code path."""
        with tempfile.TemporaryDirectory() as d:
            findings = Path(d) / "findings.txt"
            body = json.dumps({"findings": [], "overall": "ship it",
                               "blocking_count": 0})
            child = ("import pathlib\n"
                     f"pathlib.Path({str(findings)!r}).write_text({body!r}, "
                     "encoding='utf-8')\n")
            line = _run("--idle", "10", "--deadline", "30", "--findings", str(findings),
                        "--verdict-json", str(Path(d) / "verdict.json"),
                        "--result-mode", "external-file", "--max-capture-bytes", "100000",
                        "--", PY, "-c", child)
            self.assertEqual(line["status"], "ok", line)
            self.assertEqual(findings.read_text(encoding="utf-8"), body)
            self.assertEqual(line["verdict"], str(Path(d) / "verdict.json"),
                             line.get("verdict_reason"))

    def test_the_notice_is_prepended_to_a_failed_runs_preserved_text_too(self):
        with tempfile.TemporaryDirectory() as d:
            child = _child_printing(
                *(_transcript_event("x" * 800) for _ in range(4)),
                json.dumps({"type": "turn.failed", "message": "gave up"}))
            line, _text = self._run_capped(d, child, 2000)
            self.assertEqual(line["status"], "error")
            kept = Path(line["partial_findings"]).read_text(encoding="utf-8")
            self.assertIn("dropped to stay within --max-capture-bytes", kept,
                          "the file missing the most is the one that does not say so")


class TwoWritersShareOneDisplayLog(unittest.TestCase):
    """The stdout reader and the stderr drainer tee to one handle. A cap counted outside a
    lock is a cap two threads overshoot by whatever each was holding, and the end marker —
    the log's only completion signal — must land whatever the cap says."""

    def _stream(self, directory, cap):
        args = _parsed("--idle", "5", "--deadline", "30",
                       "--findings", str(Path(directory) / "findings.txt"),
                       "--display", str(Path(directory) / "display.log"),
                       "--max-capture-bytes", str(cap),
                       "--result-mode", "stream-transcript", "--", PY, "-c", "pass")
        # No child: construction opens the display log and reads the args, and touches the
        # process only by storing it. A reviewer spawned here would be one this test has to
        # reap for a pipe it never reads.
        return review_runner._Stream(None, args), Path(directory) / "display.log"

    def test_concurrent_writers_stay_inside_the_cap_and_the_marker_still_lands(self):
        with tempfile.TemporaryDirectory() as d:
            cap = 4000
            stream, log = self._stream(d, cap)
            # 300 does not divide 4000, so one writer crosses the boundary and takes the
            # truncating path — the case where a cap counted outside a lock overshoots. `z`
            # appears in none of this program's own markers, so counting it counts exactly
            # the reviewer-derived bytes and nothing else.
            chunk = "z" * 300

            def spam():
                for _ in range(100):
                    stream.write_display(chunk)

            threads = [threading.Thread(target=spam) for _ in range(4)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
            stream.write_display("[review_runner] end status=ok\n", bounded=False)
            stream.display_fh.close()
            text = log.read_text(encoding="utf-8")
            self.assertIn("[review_runner] end status=ok", text,
                          "the cap swallowed the log's only completion signal")
            self.assertIn("display log capped", text, "no writer reached the cap at all")
            self.assertLessEqual(text.count("z"), cap,
                                 "four writers overshot a cap nothing serialized")


class AFailedReadIsNotTheEndOfTheStream(unittest.TestCase):
    """`os.read` raising is not EOF, and the difference decides what the status line claims.

    The reader sets its done flag on every exit path, so a read that failed reaches the
    outcome step looking exactly like a stream that ended: `drained` is True, whatever text
    arrived first is published, and a review the machine cut short is reported as one the
    reviewer finished. The status line is the only proof a caller has that an attempt
    completed, so this is the shape that makes a failure look like a clean result.
    """

    def _args(self, directory, mode="stream-transcript"):
        return _parsed("--idle", "5", "--deadline", "30",
                       "--findings", str(Path(directory) / "findings.txt"),
                       "--result-mode", mode, "--", PY, "-c", "pass")

    def test_a_read_that_fails_is_recorded_rather_than_taken_for_eof(self):
        with tempfile.TemporaryDirectory() as d:
            proc = _reviewer("import time; time.sleep(30)")
            self.addCleanup(proc.kill)
            stream = review_runner._Stream(proc, self._args(d))
            with unittest.mock.patch.object(review_runner.os, "read",
                                            side_effect=OSError(5, "I/O error")):
                stream.start()
                self.assertTrue(stream.done.wait(timeout=10),
                                "the reader never finished")
            self.assertIn("reading the reviewer's output failed",
                          stream.state.get("read_error") or "",
                          "a failed read left no trace, so EOF and a fault are one state")

    def test_the_watch_ends_the_run_under_the_faults_own_name(self):
        """Not an idle timeout. Nothing stamps the heartbeat once the reader is gone, so the
        idle clock would charge the child for silence that belongs to this program."""
        with tempfile.TemporaryDirectory() as d:
            proc = _reviewer("import time; time.sleep(30)")
            self.addCleanup(proc.kill)
            stream = review_runner._Stream(proc, self._args(d))
            stream.start()
            with stream.lock:
                stream.state["read_error"] = "reading the reviewer's output failed: [Errno 5]"
            status, reason = stream.watch(30, 30)
            stream.settle(status)
            self.assertEqual(status, "error", reason)
            self.assertIn("[Errno 5]", reason)

    def test_the_outcome_refuses_a_transcript_a_failed_read_cut_short(self):
        with tempfile.TemporaryDirectory() as d:
            args = self._args(d)
            status, reason, text, _partial = review_runner._route_outcome(
                args, "ok", None, True, 0,
                _stream_state(transcript=["half a review"], terminal=None,
                              read_error="reading the reviewer's output failed: "
                                         "[Errno 28] No space left on device"),
                threading.Lock())
            self.assertEqual(status, "error")
            self.assertIn("No space left on device", reason,
                          "a storage fault must reach the caller as itself, so a caller "
                          "counting the reviewer's bad answers does not charge it one")
            self.assertIsNone(text)

    def test_a_fault_after_the_reviewers_own_end_marker_costs_nothing(self):
        """The boundary the refusal must not cross. Once the terminal event is in hand the
        stream is over and only the pipe is still open — there is nothing left to lose."""
        with tempfile.TemporaryDirectory() as d:
            args = self._args(d)
            status, reason, text, _partial = review_runner._route_outcome(
                args, "ok", None, True, 0,
                _stream_state(transcript=["the review"], terminal="ok",
                              read_error="reading the reviewer's output failed: [Errno 5]"),
                threading.Lock())
            self.assertEqual((status, reason), ("ok", None))
            self.assertEqual(text, "the review")


class AVerdictFileThatCannotBeStattedIsNotAnAbsentOne(unittest.TestCase):
    """`exists()` answers False for a path it cannot stat, and external-file mode reads that
    as "the reviewer wrote no verdict" — charging a storage fault to the reviewer as a bad
    answer. The two states need different responses: one is retried, the other is not.
    """

    def _args(self, directory):
        return _parsed("--idle", "5", "--deadline", "10",
                       # Resolved, so the comparison inside the test has both sides in the
                       # same spelling: macOS answers `/var` with `/private/var`.
                       "--findings", str(Path(directory).resolve() / "findings.txt"),
                       "--result-mode", "external-file", "--", PY, "-c", "pass")

    def test_a_findings_path_that_cannot_be_examined_is_a_routing_failure(self):
        """ELOOP, because `Path.exists()` keeps a list of errors it reports as "not there"
        and that is one of them — asking it about a path is how a fault becomes an absence.
        `stat` has no such list, so only ENOENT answers."""
        with tempfile.TemporaryDirectory() as d:
            args = self._args(d)
            Path(args.findings).write_text("a real verdict\n", encoding="utf-8")
            wanted = os.path.abspath(args.findings)
            real_stat = Path.stat

            def refuse(self, *a, **kw):
                if os.path.abspath(os.fspath(self)) == wanted:
                    raise OSError(errno.ELOOP, "Too many levels of symbolic links")
                return real_stat(self, *a, **kw)

            with unittest.mock.patch.object(Path, "stat", refuse):
                status, reason, text, _partial = review_runner._route_outcome(
                    args, "ok", None, True, 0, _stream_state(), threading.Lock())
            self.assertEqual(status, "error")
            self.assertIn("routing failed", reason)
            self.assertNotIn("wrote no verdict", reason,
                             "a fault reading the file was charged to the reviewer")
            self.assertIsNone(text)


class ThePartialIsNeverWrittenThroughALinkNobodyChecked(unittest.TestCase):
    """`Path.is_symlink()` answers False for everything it cannot stat, and on a platform
    with no ``O_NOFOLLOW`` that False is the only thing between this and a write through a
    link somebody else placed.
    """

    def test_a_path_whose_kind_cannot_be_established_is_not_written(self):
        """ELOOP, because it is the error `Path.is_symlink()` reports as "not a link" — and
        it is raised by the one thing this refuses to write through. `lstat` has no such
        list: it answers, or it says nothing."""
        with tempfile.TemporaryDirectory() as d:
            # BOTH sides resolved, and resolved HERE: macOS answers `/var` with
            # `/private/var` and Windows with an 8.3 short name, and `realpath` inside the
            # patch below would call the very `lstat` it replaces.
            findings = Path(d).resolve() / "findings.txt"
            wanted = os.path.abspath(str(findings) + review_runner.PARTIAL_SUFFIX)
            real_lstat = os.lstat

            def refuse(path, *a, **kw):
                if os.path.abspath(os.fspath(path)) == wanted:
                    raise OSError(errno.ELOOP, "Too many levels of symbolic links")
                return real_lstat(path, *a, **kw)

            with unittest.mock.patch.object(review_runner.os, "lstat", refuse):
                kept = review_runner._preserve_partial(findings, "half a review", "why")
            self.assertIsNone(kept, "it wrote to a path it could not identify")
            self.assertFalse(Path(str(findings) + review_runner.PARTIAL_SUFFIX).exists())

    def test_an_absent_path_is_still_written(self):
        """The positive control: ENOENT is the one answer that means "not a link"."""
        with tempfile.TemporaryDirectory() as d:
            findings = Path(d) / "findings.txt"
            kept = review_runner._preserve_partial(findings, "half a review", "why")
            self.assertIsNotNone(kept)
            self.assertIn("half a review", Path(kept).read_text(encoding="utf-8"))


class APartialNeverOverwritesWhatIsAlreadyThere(unittest.TestCase):
    """The path is not this program's to claim, so it claims with ``O_EXCL`` and steps to the
    next name. ``O_TRUNC`` here destroyed the earlier failure's transcript -- the one thing
    preserving exists to keep -- and, on a hard link, the file at the other end of it."""

    def test_an_earlier_partial_survives_and_the_second_run_takes_the_next_name(self):
        with tempfile.TemporaryDirectory() as d:
            findings = Path(d) / "findings.txt"
            first = Path(str(findings) + review_runner.PARTIAL_SUFFIX)
            first.write_text("the FIRST run's transcript", encoding="utf-8")
            kept = review_runner._preserve_partial(findings, "the second run's transcript", "why")
            self.assertEqual(first.read_text(encoding="utf-8"), "the FIRST run's transcript")
            self.assertIsNotNone(kept)
            self.assertNotEqual(Path(kept), first)
            self.assertIn("the second run's transcript", Path(kept).read_text(encoding="utf-8"))

    def test_a_hard_link_at_the_path_is_not_written_through(self):
        with tempfile.TemporaryDirectory() as d:
            findings = Path(d) / "findings.txt"
            other = Path(d) / "somebody-elses.txt"
            other.write_text("not the review runner's to touch", encoding="utf-8")
            try:
                os.link(other, Path(str(findings) + review_runner.PARTIAL_SUFFIX))
            except (OSError, NotImplementedError, AttributeError) as exc:
                self.skipTest(f"no hard links here: {exc}")
            review_runner._preserve_partial(findings, "half a review", "why")
            self.assertEqual(other.read_text(encoding="utf-8"), "not the review runner's to touch")

    @unittest.skipUnless(hasattr(os, "mkfifo"), "POSIX only")
    def test_a_fifo_at_the_path_does_not_block_the_open(self):
        """``os.open`` on a FIFO for writing blocks until a reader arrives, and this runs
        after the deadline loop has ended -- so nothing would ever time it out. ``O_EXCL``
        answers EEXIST instead. The test would HANG rather than fail if that regressed, which
        is why the timeout is asserted around it rather than left to the suite."""
        with tempfile.TemporaryDirectory() as d:
            findings = Path(d) / "findings.txt"
            os.mkfifo(str(findings) + review_runner.PARTIAL_SUFFIX)
            done = threading.Event()
            box = {}
            def go():
                box["kept"] = review_runner._preserve_partial(findings, "half a review", "why")
                done.set()
            threading.Thread(target=go, daemon=True).start()
            self.assertTrue(done.wait(20), "the open blocked on the FIFO")
            kept = box["kept"]
            self.assertIsNotNone(kept, "the text was not preserved beside the FIFO")
            self.assertIn("half a review", Path(kept).read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
