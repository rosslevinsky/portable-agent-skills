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
import io
import json
import os
import re
import shutil
import subprocess
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
        # A pre-existing findings file is refused BEFORE launch (it used to be deleted),
        # so stale content can never be passed off as a fresh verdict. The file is left
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
        """Two unknowns, floor 1: the old sentence said "2 ... each was counted" and
        published 1. Asserted on the reconciler directly — the floor only lands below
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
        # child is already gone, which is this case exactly — so the supervisor used to wait
        # out the whole 30s drain and report the review with its tail missing.
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
        """`--idle 0` used to return above the invalidation loop; ordering still matters.

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
    """One unpaired surrogate used to discard a COMPLETED cross-model review.

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


def reader_wiring(source: str, func: str = "run") -> dict:
    """What ``run()`` does with the display decoder and the chunks it reads, by DATA FLOW.

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
    """`reader()` must actually USE it. Every test above passes if it decodes chunks itself.

    Source assertions, in the shape `tests/test_skill_budgets.py` already uses for the
    budget check: the end-to-end pair below exercises the real path but depends on thread
    scheduling for its split, so the guarantee that the seam is wired in lives here.
    """

    def setUp(self):
        self.source = _RUNNER.read_text(encoding="utf-8")
        self.wiring = reader_wiring(self.source)

    def test_the_decoder_is_built_once_and_never_inside_the_read_loop(self):
        self.assertEqual(
            self.wiring["builds"], 1,
            "run() must build exactly one display decoder: none means the reader went "
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
        feed = "write_display(display_decoder.decode(data))"
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
                feed, f"same = data\n{indent}write_display(display_decoder.decode(same))"))
            self.assertTrue(m["fed"])

        with self.subTest("a decoder rebuilt per chunk"):
            m = reader_wiring(
                src.replace(feed, f"display_decoder = _display_decoder()\n{indent}{feed}"))
            self.assertEqual(m["builds"], 2)
            self.assertTrue(m["built_in_loop"])

        with self.subTest("the chunk decoded on its own"):
            m = reader_wiring(src.replace(feed, 'write_display(data.decode("utf-8", "replace"))'))
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

        The old model spawned git on every invocation to decide whether it was allowed
        to delete. Nothing here asks anything about a repository.
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
        is exactly the case the old check was reaching for and could not enforce.
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

        The signal handler exits through os._exit and used to skip the cleanup, so
        removing the up-front invalidation made every interruption block the rerun.
        """
        source = inspect.getsource(review_runner.run)
        handler = source.split("def _on_signal(", 1)[1].split("os._exit(1)  # must not", 1)[0]
        self.assertIn("os.unlink", handler,
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


if __name__ == "__main__":
    unittest.main()
