"""Deterministic unit tests for the stdlib-only plan-duel engine. No CLI is spawned.

The engine lives at ``skills/plan-duel/plan_duel.py``. That directory name has a
hyphen, so it is NOT importable as a package; it goes on ``sys.path`` by an ABSOLUTE
path derived from THIS file, so the suite runs from any working directory.
"""

import contextlib
import io
import json
import math
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import re
import unittest
import unittest.mock
from pathlib import Path

_ENGINE_DIR = Path(__file__).resolve().parent.parent / "skills" / "plan-duel"
if str(_ENGINE_DIR) not in sys.path:
    sys.path.insert(0, str(_ENGINE_DIR))

import plan_duel  # noqa: E402

# Process-exec fixtures: a cross-platform stub CLI (invoked as
# [sys.executable, stub_cli.py, ...], never a shebang/exec-bit script) and the
# committed state/ workdir templates that resume/cleanup tests copy to tempdirs.
_FIXTURES = Path(__file__).resolve().parent / "fixtures" / "plan-duel"
_STUB = _FIXTURES / "stub_cli.py"
_STATE_FIXTURES = _FIXTURES / "state"


def _stub_argv(*args):
    """Argv list that runs the stub CLI cross-platform (exercises the argv path)."""
    return [sys.executable, str(_STUB), *args]


class _TempWorkdirMixin:
    """Provides throwaway temp workdirs and copied-from-fixture workdirs."""

    def _tmpdir(self):
        tmp = Path(tempfile.mkdtemp(prefix="pd-test-")).resolve()
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        return tmp

    def _load_state_fixture(self, case):
        src = _STATE_FIXTURES / case
        dst = self._tmpdir() / "wd"
        shutil.copytree(src, dst)
        return dst.resolve()


# --------------------------------------------------------------------------- #
# Interpreter guard
# --------------------------------------------------------------------------- #
class RequirePythonTests(unittest.TestCase):
    def test_older_interpreter_exits_nonzero_with_clear_message(self):
        import io
        import contextlib

        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            with self.assertRaises(SystemExit) as ctx:
                plan_duel.require_python(3, 10, current=(3, 9))
        self.assertNotEqual(ctx.exception.code, 0)
        self.assertIn("Python 3.10+ required", stderr.getvalue())

    def test_new_enough_interpreter_does_not_raise(self):
        # Should simply return without raising.
        plan_duel.require_python(3, 10, current=(3, 12))
        plan_duel.require_python(3, 10, current=(3, 10))


# --------------------------------------------------------------------------- #
# Template rendering
# --------------------------------------------------------------------------- #
class RenderTemplateTests(unittest.TestCase):
    def test_happy_path_single_placeholder(self):
        self.assertEqual(
            plan_duel.render_template("Hello ⟪name⟫", {"name": "World"}),
            "Hello World",
        )

    def test_repeated_and_multiple_placeholders(self):
        out = plan_duel.render_template(
            "⟪who⟫ meets ⟪who⟫ at ⟪place⟫",
            {"who": "Ada", "place": "Bletchley"},
        )
        self.assertEqual(out, "Ada meets Ada at Bletchley")

    def test_literal_braces_pass_through_untouched(self):
        # {approach}/{workdir} are literal prose that must NOT be treated as
        # placeholders (this is why the engine uses ⟪name⟫, not str.format).
        out = plan_duel.render_template(
            "Do ⟪action⟫ with {approach} and {workdir}",
            {"action": "plan"},
        )
        self.assertEqual(out, "Do plan with {approach} and {workdir}")

    def test_unresolved_placeholder_raises_naming_every_marker(self):
        with self.assertRaises(plan_duel.TemplateError) as ctx:
            plan_duel.render_template("⟪a⟫ and ⟪b⟫ and ⟪c⟫", {"a": "x"})
        msg = str(ctx.exception)
        self.assertIn("⟪b⟫", msg)
        self.assertIn("⟪c⟫", msg)
        # The resolved one must NOT be reported.
        self.assertNotIn("⟪a⟫", msg)

    def test_value_containing_marker_text_is_not_re_expanded(self):
        # A substituted value that itself looks like a marker must be left as a
        # literal, never re-scanned as an unresolved placeholder.
        out = plan_duel.render_template("X ⟪a⟫", {"a": "⟪b⟫"})
        self.assertEqual(out, "X ⟪b⟫")

    def test_template_error_is_a_plan_duel_error(self):
        self.assertTrue(issubclass(plan_duel.TemplateError, plan_duel.PlanDuelError))


# --------------------------------------------------------------------------- #
# Adapter-config parsing
# --------------------------------------------------------------------------- #
def _valid_config():
    return {
        "agent_a": {
            "command": ["exe-a", "-p", "⟪prompt⟫", "--add-dir", "⟪workdir⟫"],
            "stdout": "file",
            "prompt_mode": "arg",
            "cwd": "workdir",
            "placeholders": ["prompt", "workdir"],
        },
        "agent_b": {
            "command": ["exe-b", "run", "-C", "⟪workdir⟫", "⟪prompt⟫"],
            "stdout": "file",
            "placeholders": ["prompt", "workdir"],
        },
        "judge": {
            "command": ["exe-a", "-p", "⟪prompt⟫"],
            "stdout": "clean-last-message",
            "placeholders": ["prompt"],
        },
    }


class AdapterConfigParseTests(unittest.TestCase):
    def test_valid_spec_from_json_string(self):
        specs = plan_duel.parse_adapter_config(json.dumps(_valid_config()))
        self.assertEqual(set(specs), {"agent_a", "agent_b", "judge"})
        a = specs["agent_a"]
        self.assertEqual(a.command, ("exe-a", "-p", "⟪prompt⟫", "--add-dir", "⟪workdir⟫"))
        self.assertEqual(a.stdout, "file")
        self.assertEqual(a.prompt_mode, "arg")
        self.assertEqual(a.cwd, "workdir")
        self.assertEqual(a.placeholders, ("prompt", "workdir"))

    def test_valid_spec_from_dict(self):
        specs = plan_duel.parse_adapter_config(_valid_config())
        self.assertEqual(specs["judge"].stdout, "clean-last-message")

    def test_prompt_mode_defaults_to_arg(self):
        specs = plan_duel.parse_adapter_config(_valid_config())
        self.assertEqual(specs["agent_b"].prompt_mode, "arg")

    def test_cwd_defaults_to_none(self):
        specs = plan_duel.parse_adapter_config(_valid_config())
        self.assertIsNone(specs["agent_b"].cwd)

    def test_missing_required_role_is_rejected(self):
        cfg = _valid_config()
        del cfg["judge"]
        with self.assertRaises(plan_duel.AdapterConfigError) as ctx:
            plan_duel.parse_adapter_config(cfg)
        self.assertIn("judge", str(ctx.exception))

    def test_unknown_top_level_role_is_rejected(self):
        cfg = _valid_config()
        cfg["referee"] = cfg["judge"]
        with self.assertRaises(plan_duel.AdapterConfigError) as ctx:
            plan_duel.parse_adapter_config(cfg)
        self.assertIn("referee", str(ctx.exception))

    def test_missing_command_key_is_rejected(self):
        cfg = _valid_config()
        del cfg["agent_a"]["command"]
        with self.assertRaises(plan_duel.AdapterConfigError) as ctx:
            plan_duel.parse_adapter_config(cfg)
        self.assertIn("command", str(ctx.exception))

    def test_missing_stdout_key_is_rejected(self):
        cfg = _valid_config()
        del cfg["agent_b"]["stdout"]
        with self.assertRaises(plan_duel.AdapterConfigError) as ctx:
            plan_duel.parse_adapter_config(cfg)
        self.assertIn("stdout", str(ctx.exception))

    def test_unknown_stdout_mode_is_rejected_with_allowed_modes(self):
        cfg = _valid_config()
        cfg["judge"]["stdout"] = "transcript"
        with self.assertRaises(plan_duel.AdapterConfigError) as ctx:
            plan_duel.parse_adapter_config(cfg)
        msg = str(ctx.exception)
        self.assertIn("transcript", msg)
        self.assertIn("file", msg)
        self.assertIn("clean-last-message", msg)

    def test_unknown_role_key_is_rejected(self):
        cfg = _valid_config()
        cfg["agent_a"]["turbo"] = True
        with self.assertRaises(plan_duel.AdapterConfigError) as ctx:
            plan_duel.parse_adapter_config(cfg)
        self.assertIn("turbo", str(ctx.exception))

    def test_invalid_prompt_mode_is_rejected(self):
        cfg = _valid_config()
        cfg["agent_a"]["prompt_mode"] = "telepathy"
        with self.assertRaises(plan_duel.AdapterConfigError) as ctx:
            plan_duel.parse_adapter_config(cfg)
        self.assertIn("telepathy", str(ctx.exception))

    def test_invalid_cwd_anchor_is_rejected(self):
        cfg = _valid_config()
        cfg["agent_a"]["cwd"] = "home"
        with self.assertRaises(plan_duel.AdapterConfigError) as ctx:
            plan_duel.parse_adapter_config(cfg)
        self.assertIn("home", str(ctx.exception))

    def test_command_must_be_nonempty_list_of_strings(self):
        cfg = _valid_config()
        cfg["agent_a"]["command"] = "exe-a -p"
        with self.assertRaises(plan_duel.AdapterConfigError):
            plan_duel.parse_adapter_config(cfg)

        cfg = _valid_config()
        cfg["agent_a"]["command"] = []
        with self.assertRaises(plan_duel.AdapterConfigError):
            plan_duel.parse_adapter_config(cfg)

        cfg = _valid_config()
        cfg["agent_a"]["command"] = ["exe-a", 7]
        with self.assertRaises(plan_duel.AdapterConfigError):
            plan_duel.parse_adapter_config(cfg)

    def test_command_using_undeclared_placeholder_is_rejected(self):
        cfg = _valid_config()
        cfg["agent_a"]["command"] = ["exe-a", "⟪prompt⟫", "⟪secret⟫"]
        # placeholders only declares prompt + workdir → ⟪secret⟫ is undeclared.
        with self.assertRaises(plan_duel.AdapterConfigError) as ctx:
            plan_duel.parse_adapter_config(cfg)
        self.assertIn("secret", str(ctx.exception))

    def test_unhashable_scalar_fields_raise_clean_error_not_typeerror(self):
        # A malformed non-string (unhashable) value for a scalar field must fail
        # loud with AdapterConfigError, never a raw TypeError from `x in frozenset`.
        for field_name in ("stdout", "prompt_mode", "cwd"):
            cfg = _valid_config()
            cfg["agent_a"][field_name] = []
            with self.assertRaises(plan_duel.AdapterConfigError):
                plan_duel.parse_adapter_config(cfg)

    def test_invalid_json_string_is_rejected(self):
        with self.assertRaises(plan_duel.AdapterConfigError):
            plan_duel.parse_adapter_config("{ not valid json ")

    def test_top_level_must_be_object(self):
        with self.assertRaises(plan_duel.AdapterConfigError):
            plan_duel.parse_adapter_config("[]")


# --------------------------------------------------------------------------- #
# Score parsing
# --------------------------------------------------------------------------- #
class ParseScoreTests(unittest.TestCase):
    def test_plain_integer(self):
        self.assertEqual(plan_duel.parse_score("SCORE: 8"), 8)

    def test_score_out_of_ten_form(self):
        self.assertEqual(plan_duel.parse_score("SCORE: 8/10"), 8)

    def test_ten(self):
        self.assertEqual(plan_duel.parse_score("SCORE: 10"), 10)

    def test_leading_prose_lines_before_score(self):
        text = (
            "The plans have converged nicely.\n"
            "SCORE: 7\n\n"
            "DIFFERENCES:\n1. Something. **Stronger: A** — reason\n"
            "PREFERRED: A\n"
        )
        self.assertEqual(plan_duel.parse_score(text), 7)

    def test_first_score_line_wins(self):
        self.assertEqual(plan_duel.parse_score("SCORE: 4\nSCORE: 9"), 4)

    def test_first_score_line_unparseable_does_not_fall_through(self):
        # "first SCORE: line" semantics: if the FIRST SCORE line has no integer,
        # the result is None even when a LATER line does — the later line is
        # never consulted.
        self.assertIsNone(plan_duel.parse_score("SCORE: N/A\nSCORE: 9"))

    def test_missing_score_line_returns_none(self):
        self.assertIsNone(plan_duel.parse_score("no score here\nPREFERRED: A"))

    def test_non_integer_score_returns_none(self):
        self.assertIsNone(plan_duel.parse_score("SCORE: —"))
        self.assertIsNone(plan_duel.parse_score("SCORE: N/A"))


# --------------------------------------------------------------------------- #
# Exit-condition decisions
# --------------------------------------------------------------------------- #
class ConvergenceTests(unittest.TestCase):
    def test_converge_at_round_3_score_8(self):
        d = plan_duel.evaluate_exit(3, [3, 6, 8])
        self.assertTrue(d.stop)
        self.assertEqual(d.stopped_due_to, "Convergence")
        self.assertEqual(d.message, "Convergence reached at round 3 (score: 8/10).")

    def test_n_ge_3_gate_blocks_early_high_score(self):
        # Round 2 with a perfect score must NOT converge (avoid trusting a high
        # score before plans have cross-pollinated).
        self.assertIsNone(plan_duel.convergence_exit(2, 10))
        d = plan_duel.evaluate_exit(2, [10, 10])
        self.assertFalse(d.stop)

    def test_score_below_8_does_not_converge(self):
        self.assertIsNone(plan_duel.convergence_exit(3, 7))


class StagnationTests(unittest.TestCase):
    def test_stagnation_boundary_at_round_4(self):
        # rounds 1..4 = [7, 5, 6, 6]: recent_best = max(5,6,6) = 6,
        # prior_best = max(7) = 7; 6 <= 7 -> stagnate.
        d = plan_duel.evaluate_exit(4, [7, 5, 6, 6])
        self.assertTrue(d.stop)
        self.assertEqual(d.stopped_due_to, "Stagnation")
        self.assertEqual(
            d.message,
            "Stagnation detected — best score in last 3 rounds (6/10) has not "
            "exceeded prior peak (7/10). Stopping early.",
        )

    def test_equal_recent_and_prior_triggers_stagnation(self):
        d = plan_duel.evaluate_exit(4, [5, 5, 5, 5])
        self.assertTrue(d.stop)
        self.assertEqual(d.stopped_due_to, "Stagnation")
        self.assertEqual(
            d.message,
            "Stagnation detected — best score in last 3 rounds (5/10) has not "
            "exceeded prior peak (5/10). Stopping early.",
        )

    def test_recent_exceeding_prior_does_not_stagnate(self):
        # [5, 6, 7, 7]: recent = max(6,7,7)=7 > prior = max(5)=5 -> no stagnation.
        self.assertIsNone(plan_duel.stagnation_exit(4, [5, 6, 7, 7]))
        d = plan_duel.evaluate_exit(4, [5, 6, 7, 7])
        self.assertFalse(d.stop)

    def test_stagnation_not_checked_before_round_4(self):
        self.assertIsNone(plan_duel.stagnation_exit(3, [5, 5, 5]))


class MaxRoundsTests(unittest.TestCase):
    def test_max_rounds_at_round_10(self):
        scores = [1, 2, 3, 4, 5, 6, 6, 7, 7, 7]
        # N=10 score 7 (<8 so no converge); recent = max(r8,9,10)=7,
        # prior = max(r1..7)=6 -> 7>6 no stagnation; N=10 -> Maximum rounds.
        d = plan_duel.evaluate_exit(10, scores)
        self.assertTrue(d.stop)
        self.assertEqual(d.stopped_due_to, "Maximum rounds")
        self.assertEqual(d.message, "Maximum rounds reached (score: 7/10).")

    def test_no_exit_when_no_condition_met(self):
        d = plan_duel.evaluate_exit(2, [4, 5])
        self.assertFalse(d.stop)
        self.assertIsNone(d.stopped_due_to)
        self.assertIsNone(d.message)


class ExitOrderTests(unittest.TestCase):
    def test_convergence_takes_precedence_over_max_at_round_10(self):
        d = plan_duel.evaluate_exit(10, [9] * 10)
        self.assertEqual(d.stopped_due_to, "Convergence")
        self.assertEqual(d.message, "Convergence reached at round 10 (score: 9/10).")

    def test_stagnation_takes_precedence_over_max_at_round_10(self):
        # score_n 7 (<8, no converge); rounds all 7 -> recent (7) <= prior (7)
        # -> stagnation fires before the max-rounds check.
        d = plan_duel.evaluate_exit(10, [7] * 10)
        self.assertEqual(d.stopped_due_to, "Stagnation")

    def test_convergence_takes_precedence_over_stagnation(self):
        # Round 4, scores [8, 5, 5, 8]: convergence fires (N>=3, score(4)=8>=8)
        # AND stagnation would fire (recent max(5,5,8)=8 <= prior max(8)=8) —
        # convergence is checked first, so it wins.
        self.assertIsNotNone(plan_duel.stagnation_exit(4, [8, 5, 5, 8]))
        d = plan_duel.evaluate_exit(4, [8, 5, 5, 8])
        self.assertEqual(d.stopped_due_to, "Convergence")
        self.assertEqual(d.message, "Convergence reached at round 4 (score: 8/10).")


# --------------------------------------------------------------------------- #
# Snapshot naming + role/slug/winner resolution
# --------------------------------------------------------------------------- #
class NamingTests(unittest.TestCase):
    def test_plan_snapshot_name(self):
        self.assertEqual(plan_duel.plan_snapshot_name("a", 3), "plan-a-round-3.md")
        self.assertEqual(plan_duel.plan_snapshot_name("b", 0), "plan-b-round-0.md")

    def test_slugify_name_lowercases(self):
        self.assertEqual(plan_duel.slugify_name("Claude"), "claude")
        self.assertEqual(plan_duel.slugify_name("Codex"), "codex")
        self.assertEqual(plan_duel.slugify_name("Foo"), "foo")

    def test_parse_preferred(self):
        self.assertEqual(plan_duel.parse_preferred("PREFERRED: A"), "A")
        self.assertEqual(plan_duel.parse_preferred("PREFERRED: B\nmore text"), "B")
        self.assertIsNone(plan_duel.parse_preferred("no preference stated"))
        self.assertIsNone(plan_duel.parse_preferred("PREFERRED: C"))

    def test_resolve_winner_a_is_controller(self):
        name, filename = plan_duel.resolve_winner("A", "Claude", "Codex")
        self.assertEqual(name, "Claude")
        self.assertEqual(filename, "plan-claude.md")

    def test_resolve_winner_b_is_participant(self):
        name, filename = plan_duel.resolve_winner("B", "Claude", "Codex")
        self.assertEqual(name, "Codex")
        self.assertEqual(filename, "plan-codex.md")

    def test_resolve_winner_rejects_invalid_letter(self):
        with self.assertRaises(ValueError):
            plan_duel.resolve_winner("C", "Claude", "Codex")


# =========================================================================== #
# State/resume, cleanup, freeze, subprocess execution, capture, progress
# =========================================================================== #


# --------------------------------------------------------------------------- #
# IO / encoding helpers
# --------------------------------------------------------------------------- #
class IoHelpersTests(_TempWorkdirMixin, unittest.TestCase):
    def test_read_text_normalizes_crlf_and_cr(self):
        p = self._tmpdir() / "f.md"
        p.write_bytes(b"a\r\nb\rc\nd")
        self.assertEqual(plan_duel.read_text_normalized(p), "a\nb\nc\nd")

    def test_read_text_is_utf8(self):
        p = self._tmpdir() / "f.md"
        p.write_bytes("café ⟪x⟫\n".encode("utf-8"))
        self.assertEqual(plan_duel.read_text_normalized(p), "café ⟪x⟫\n")

    def test_write_text_utf8_default_lf(self):
        p = self._tmpdir() / "f.md"
        plan_duel.write_text_utf8(p, "x\r\ny\n")
        self.assertEqual(p.read_bytes(), b"x\ny\n")

    def test_write_text_utf8_explicit_crlf(self):
        p = self._tmpdir() / "f.md"
        plan_duel.write_text_utf8(p, "x\ny", newline="\r\n")
        self.assertEqual(p.read_bytes(), b"x\r\ny")

    def test_copy_bytes_is_byte_exact_preserving_crlf(self):
        d = self._tmpdir()
        src, dst = d / "s", d / "t"
        src.write_bytes(b"a\r\nb\x00\xff")
        plan_duel.copy_bytes(src, dst)
        self.assertEqual(dst.read_bytes(), b"a\r\nb\x00\xff")

    def test_file_size_bytes_counts_bytes_not_chars(self):
        p = self._tmpdir() / "f"
        p.write_bytes("é".encode("utf-8"))  # 2 bytes, 1 char
        self.assertEqual(plan_duel.file_size_bytes(p), 2)

    def test_tolerant_reader_replaces_undecodable_cli_bytes(self):
        # 0x92 is a cp1252 curly apostrophe — routine output from a Windows CLI,
        # and not valid UTF-8. A model's plan must not be thrown away over it.
        p = self._tmpdir() / "plan-b.md"
        p.write_bytes(b"Don\x92t drop this plan.\n")
        text = plan_duel.read_text_tolerant(p)
        self.assertEqual(text, "Don\ufffdt drop this plan.\n")

    def test_tolerant_reader_still_normalizes_crlf_and_cr(self):
        # Same newline contract as the strict reader — only the error policy differs.
        p = self._tmpdir() / "plan-b.md"
        p.write_bytes(b"a\r\nb\rc\nd")
        self.assertEqual(plan_duel.read_text_tolerant(p), "a\nb\nc\nd")

    def test_strict_reader_still_raises_on_a_malformed_engine_config(self):
        # The other direction of the split: engine-owned input (here an adapter
        # config) must still fail loudly rather than be silently accepted with
        # U+FFFD substituted into a role's argv.
        p = self._tmpdir() / "adapter.json"
        p.write_bytes(b'{"agent_a": {"command": ["cli", "\x92"]}}')
        with self.assertRaises(UnicodeDecodeError):
            plan_duel.read_text_normalized(p)


# --------------------------------------------------------------------------- #
# Artifact classification
# --------------------------------------------------------------------------- #
class ArtifactClassificationTests(unittest.TestCase):
    def test_artifact_round_parses_each_pattern(self):
        cases = {
            "plan-a-round-3.md": 3,
            "plan-b-round-0.md": 0,
            "rejections-a-round-12.md": 12,
            "rejections-b-round-2.md": 2,
            "judge-round-7.md": 7,
            "judge-prompt-4.txt": 4,
            "controller-prompt-1.txt": 1,
            "participant-prompt-5.txt": 5,
            "participant-round-9-status.md": 9,
            "participant-progress-6.md": 6,
        }
        for name, expected in cases.items():
            self.assertEqual(plan_duel.artifact_round(name), expected, name)

    def test_artifact_round_none_for_non_artifacts(self):
        for name in (
            "problem.md",
            "summary.md",
            "plan-a.md",
            "plan-b.md",
            "keep-me.txt",
            "state.json",
            "plan-a-round-.md",  # no number
        ):
            self.assertIsNone(plan_duel.artifact_round(name), name)

    def test_full_reset_artifact_matches_globs_including_live_plans(self):
        for name in (
            "plan-a.md",
            "plan-b.md",
            "plan-a-round-2.md",
            "rejections-b-round-1.md",
            "judge-round-3.md",
            "controller-prompt-0.txt",
            "judge-prompt-1.txt",
            "participant-prompt-0.txt",
            "participant-round-2-status.md",
            "participant-progress-0.md",
        ):
            self.assertTrue(plan_duel.is_full_reset_artifact(name), name)

    def test_full_reset_artifact_excludes_problem_summary_and_extras(self):
        for name in ("problem.md", "summary.md", "keep-me.txt", "state.json"):
            self.assertFalse(plan_duel.is_full_reset_artifact(name), name)


# --------------------------------------------------------------------------- #
# Cleanup
# --------------------------------------------------------------------------- #
class CopyBytesAtomicityTests(_TempWorkdirMixin, unittest.TestCase):
    def test_copy_leaves_no_temp_file_and_replaces_content(self):
        wd = self._tmpdir()
        src, dst = wd / "src.md", wd / "dst.md"
        src.write_bytes(b"NEW" * 100)
        dst.write_bytes(b"OLD")
        plan_duel.copy_bytes(src, dst)
        self.assertEqual(dst.read_bytes(), b"NEW" * 100)
        strays = [p.name for p in wd.iterdir() if p.name.startswith(".")]
        self.assertEqual(strays, [], f"temp artifact left behind: {strays}")

    def test_copy_preserves_bytes_exactly(self):
        wd = self._tmpdir()
        src, dst = wd / "src.md", wd / "dst.md"
        src.write_bytes(b"crlf\r\nand\xffbinary")
        plan_duel.copy_bytes(src, dst)
        self.assertEqual(dst.read_bytes(), b"crlf\r\nand\xffbinary")


class CleanupTests(_TempWorkdirMixin, unittest.TestCase):
    def test_higher_rounds_deletes_only_round_gt_threshold(self):
        wd = self._load_state_fixture("complete-with-higher")
        log = plan_duel.cleanup_higher_rounds(wd, 2)
        expected = {
            "controller-prompt-3.txt",
            "judge-prompt-3.txt",
            "judge-round-3.md",
            "participant-progress-3.md",
            "participant-prompt-3.txt",
            "participant-round-3-status.md",
            "plan-a-round-3.md",
            "rejections-a-round-3.md",
        }
        self.assertEqual(set(log), expected)
        for name in expected:
            self.assertFalse((wd / name).exists(), name)
        # Rounds 0-2, the live plans, problem.md and the non-artifact survive.
        for name in (
            "plan-a-round-2.md",
            "plan-b-round-2.md",
            "judge-round-2.md",
            "plan-a.md",
            "problem.md",
            "keep-me.txt",
        ):
            self.assertTrue((wd / name).exists(), name)

    def test_cleanup_logs_normalized_relative_names(self):
        wd = self._load_state_fixture("complete-with-higher")
        log = plan_duel.cleanup_higher_rounds(wd, 2)
        for name in log:
            self.assertNotIn("/", name)  # direct children only
            self.assertNotIn(os.sep, name)
            self.assertEqual(name, Path(name).name)

    def test_cleanup_never_recurses_into_subdirs(self):
        wd = self._load_state_fixture("nested-subdir")
        log = plan_duel.cleanup_higher_rounds(wd, 1)
        # Top-level round-5 stragglers are removed...
        self.assertIn("plan-a-round-5.md", log)
        self.assertIn("judge-round-5.md", log)
        self.assertFalse((wd / "plan-a-round-5.md").exists())
        # ...but the nested artifact-named files are untouched.
        self.assertTrue((wd / "sub" / "plan-a-round-9.md").exists())
        self.assertTrue((wd / "sub" / "judge-round-9.md").exists())
        self.assertNotIn("sub/plan-a-round-9.md", log)

    def test_cleanup_all_artifacts_full_reset(self):
        wd = self._load_state_fixture("init-incomplete")
        log = plan_duel.cleanup_all_artifacts(wd)
        self.assertEqual(
            set(log),
            {
                "plan-a.md",
                "controller-prompt-0.txt",
                "participant-prompt-0.txt",
                "participant-progress-0.md",
            },
        )
        self.assertFalse((wd / "plan-a.md").exists())
        self.assertTrue((wd / "problem.md").exists())
        self.assertTrue((wd / "keep-me.txt").exists())


# --------------------------------------------------------------------------- #
# State markers (state.json)
# --------------------------------------------------------------------------- #
class StateTests(_TempWorkdirMixin, unittest.TestCase):
    def test_save_and_load_round_trip(self):
        wd = self._tmpdir()
        state = plan_duel.RunState(
            controller_name="Claude",
            participant_name="Codex",
            rounds={
                0: plan_duel.RoundState(plans_snapshotted=True),
                1: plan_duel.RoundState(
                    plans_snapshotted=True, judge_completed=True, score=6
                ),
            },
        )
        plan_duel.save_state(wd, state)
        self.assertTrue((wd / "state.json").exists())
        loaded = plan_duel.load_state(wd)
        self.assertEqual(loaded.controller_name, "Claude")
        self.assertEqual(loaded.participant_name, "Codex")
        self.assertEqual(loaded.rounds[1].score, 6)
        self.assertTrue(loaded.rounds[1].judge_completed)
        self.assertFalse(loaded.rounds[0].judge_completed)

    def test_load_missing_returns_none(self):
        self.assertIsNone(plan_duel.load_state(self._tmpdir()))

    def test_load_corrupt_returns_none(self):
        wd = self._tmpdir()
        (wd / "state.json").write_text("{ not json", encoding="utf-8")
        self.assertIsNone(plan_duel.load_state(wd))

    def test_load_state_with_malformed_round_key_does_not_raise(self):
        # A non-integer round key must be skipped, not crash load_state.
        wd = self._tmpdir()
        (wd / "state.json").write_text(
            json.dumps({"rounds": {"oops": {"score": 5}, "2": {"score": 7}}}),
            encoding="utf-8",
        )
        loaded = plan_duel.load_state(wd)
        self.assertIsNotNone(loaded)
        self.assertEqual(set(loaded.rounds), {2})


# --------------------------------------------------------------------------- #
# Resume scan + decision
# --------------------------------------------------------------------------- #
class ResumeTests(_TempWorkdirMixin, unittest.TestCase):
    def test_last_completed_round_ignores_incomplete_higher_round(self):
        wd = self._load_state_fixture("complete-with-higher")
        # round 3 has only plan-a snapshot -> not complete.
        self.assertEqual(plan_duel.last_completed_round(wd), 2)

    def test_last_completed_round_none_when_no_complete_round(self):
        wd = self._load_state_fixture("init-incomplete")
        self.assertIsNone(plan_duel.last_completed_round(wd))

    def test_compute_resume_normal_path(self):
        wd = self._load_state_fixture("complete-with-higher")
        plan = plan_duel.compute_resume(wd)
        self.assertFalse(plan.complete)
        self.assertFalse(plan.init_incomplete)
        self.assertEqual(plan.last_completed_round, 2)
        self.assertEqual(plan.start_round, 3)
        self.assertEqual(plan.message, f"Resuming in {wd} from round 3.")
        self.assertEqual(
            plan.copies,
            [
                (wd / "plan-a-round-2.md", wd / "plan-a.md"),
                (wd / "plan-b-round-2.md", wd / "plan-b.md"),
            ],
        )

    def test_apply_resume_normal_deletes_and_copies(self):
        wd = self._load_state_fixture("complete-with-higher")
        plan = plan_duel.compute_resume(wd)
        log = plan_duel.apply_resume(plan)
        self.assertIn("plan-a-round-3.md", log)
        self.assertFalse((wd / "plan-a-round-3.md").exists())
        # Live plans were reset to the round-2 snapshots.
        self.assertEqual(
            (wd / "plan-a.md").read_bytes(),
            (wd / "plan-a-round-2.md").read_bytes(),
        )
        self.assertTrue((wd / "keep-me.txt").exists())

    def test_compute_resume_init_incomplete(self):
        wd = self._load_state_fixture("init-incomplete")
        plan = plan_duel.compute_resume(wd)
        self.assertTrue(plan.init_incomplete)
        self.assertFalse(plan.complete)
        self.assertEqual(plan.start_round, 1)
        self.assertEqual(plan.message, "Init incomplete — restarting from round 0.")
        self.assertTrue(any("stale" in a.lower() for a in plan.audit))

    def test_apply_resume_init_incomplete_full_reset(self):
        wd = self._load_state_fixture("init-incomplete")
        plan = plan_duel.compute_resume(wd)
        log = plan_duel.apply_resume(plan)
        self.assertIn("plan-a.md", log)
        self.assertFalse((wd / "plan-a.md").exists())
        self.assertTrue((wd / "problem.md").exists())

    def _init_incomplete_with_snapshot_a(self, *, size=None, truncate_to=None):
        # Round 0 died at Agent B: Plan A was snapshotted, Plan B never landed, so no
        # round is complete. ``truncate_to`` simulates a byte copy cut off part-way.
        wd = self._load_state_fixture("init-incomplete")
        live = wd / "plan-a.md"
        live.write_bytes(b"A" * (size if size is not None else 500))
        data = live.read_bytes()
        if truncate_to is not None:
            data = data[:truncate_to]
        (wd / "plan-a-round-0.md").write_bytes(data)
        return wd

    def test_compute_resume_reuses_validated_round0_plan_a(self):
        wd = self._init_incomplete_with_snapshot_a(size=500)
        plan = plan_duel.compute_resume(wd)
        self.assertTrue(plan.init_incomplete)
        self.assertTrue(plan.reuse_plan_a)
        self.assertEqual(
            plan.message,
            "Init incomplete — reusing the validated round-0 Plan A; "
            "re-running Plan B only.",
        )

    def test_compute_resume_discards_truncated_round0_plan_a(self):
        # A snapshot below the agent-output gate can only be a partial write; it must
        # not be trusted just because the file exists.
        wd = self._init_incomplete_with_snapshot_a(size=500, truncate_to=10)
        plan = plan_duel.compute_resume(wd)
        self.assertTrue(plan.init_incomplete)
        self.assertFalse(plan.reuse_plan_a)
        self.assertEqual(plan.message, "Init incomplete — restarting from round 0.")
        self.assertTrue(any("unproven" in a.lower() for a in plan.audit))

    def test_compute_resume_rejects_large_but_incomplete_snapshot(self):
        # The case a size gate alone cannot catch: a pre-atomic copy interrupted well
        # PAST the 200-byte gate. It is only provably complete if it matches the live
        # plan byte-for-byte.
        wd = self._init_incomplete_with_snapshot_a(size=5000, truncate_to=3000)
        plan = plan_duel.compute_resume(wd)
        self.assertGreater(
            plan_duel.file_size_bytes(wd / "plan-a-round-0.md"),
            plan_duel.MIN_AGENT_OUTPUT_BYTES,
        )
        self.assertFalse(plan.reuse_plan_a)
        self.assertTrue(any("unproven" in a.lower() for a in plan.audit))

    def test_compute_resume_rejects_snapshot_with_no_live_plan_to_prove_it(self):
        wd = self._init_incomplete_with_snapshot_a(size=500)
        (wd / "plan-a.md").unlink()
        plan = plan_duel.compute_resume(wd)
        self.assertFalse(plan.reuse_plan_a)

    def test_apply_resume_spares_only_the_reused_plan_a(self):
        wd = self._init_incomplete_with_snapshot_a(size=500)
        plan = plan_duel.compute_resume(wd)
        log = plan_duel.apply_resume(plan)
        self.assertNotIn("plan-a-round-0.md", log)
        self.assertTrue((wd / "plan-a-round-0.md").exists())
        # Everything else still goes, including the stale live plan.
        self.assertIn("plan-a.md", log)
        self.assertFalse((wd / "plan-a.md").exists())

    def test_apply_resume_deletes_untrusted_plan_a_snapshot(self):
        wd = self._init_incomplete_with_snapshot_a(size=10)
        plan = plan_duel.compute_resume(wd)
        log = plan_duel.apply_resume(plan)
        self.assertIn("plan-a-round-0.md", log)
        self.assertFalse((wd / "plan-a-round-0.md").exists())

    def test_stale_live_plans_overwritten_and_audited(self):
        wd = self._load_state_fixture("stale-plans")
        plan = plan_duel.compute_resume(wd)
        self.assertEqual(plan.last_completed_round, 1)
        self.assertEqual(plan.start_round, 2)
        self.assertTrue(any("stale" in a.lower() for a in plan.audit))
        plan_duel.apply_resume(plan)
        self.assertEqual((wd / "plan-a.md").read_text(encoding="utf-8"), "ROUND1-A\n")
        self.assertEqual((wd / "plan-b.md").read_text(encoding="utf-8"), "ROUND1-B\n")

    def test_missing_judge_edge_is_resumed_and_audited(self):
        wd = self._load_state_fixture("missing-judge")
        plan = plan_duel.compute_resume(wd)
        # v1 outcome: round 2 counts as complete by plan snapshots -> resume at 3.
        self.assertEqual(plan.last_completed_round, 2)
        self.assertEqual(plan.start_round, 3)
        self.assertTrue(any("judge" in a.lower() for a in plan.audit))

    def test_summary_present_is_complete(self):
        wd = self._load_state_fixture("summary-complete")
        plan = plan_duel.compute_resume(wd)
        self.assertTrue(plan.complete)

    def test_resume_decision_identical_with_or_without_state_json(self):
        wd = self._load_state_fixture("complete-with-higher")
        without = plan_duel.compute_resume(wd)
        # A matching state.json must not change the v1-golden decision (only audit).
        plan_duel.save_state(
            wd,
            plan_duel.RunState(
                controller_name="Claude",
                participant_name="Codex",
                rounds={
                    1: plan_duel.RoundState(True, True, 6),
                    2: plan_duel.RoundState(True, True, 7),
                },
            ),
        )
        with_state = plan_duel.compute_resume(wd)
        self.assertEqual(without.last_completed_round, with_state.last_completed_round)
        self.assertEqual(without.start_round, with_state.start_round)
        self.assertEqual(without.init_incomplete, with_state.init_incomplete)
        self.assertEqual(without.copies, with_state.copies)
        self.assertEqual(without.message, with_state.message)
        # Cleanup must never delete the state marker (parity-safe deletion log).
        plan_duel.apply_resume(with_state)
        self.assertTrue((wd / "state.json").exists())


# --------------------------------------------------------------------------- #
# Freeze per-round inputs
# --------------------------------------------------------------------------- #
class FreezeTests(_TempWorkdirMixin, unittest.TestCase):
    def test_freeze_snapshots_live_and_later_edit_does_not_leak(self):
        wd = self._tmpdir()
        (wd / "plan-a.md").write_text("AAA", encoding="utf-8")
        (wd / "plan-b.md").write_text("BBB", encoding="utf-8")
        frozen = plan_duel.freeze_round_inputs(wd, 1)
        self.assertEqual(frozen.round, 1)
        self.assertEqual(frozen.plan_a, wd / "plan-a-round-0.md")
        self.assertEqual(frozen.plan_a.read_text(encoding="utf-8"), "AAA")
        self.assertEqual(frozen.plan_b.read_text(encoding="utf-8"), "BBB")
        # Agent A revises the live plan; the frozen reference the 2nd agent reads
        # must NOT change (this is the v1 "simultaneous" guarantee).
        (wd / "plan-a.md").write_text("AAA-revised", encoding="utf-8")
        self.assertEqual(frozen.plan_a.read_text(encoding="utf-8"), "AAA")

    def test_freeze_does_not_overwrite_existing_immutable_snapshot(self):
        wd = self._tmpdir()
        (wd / "plan-a.md").write_text("DIFF", encoding="utf-8")
        (wd / "plan-b.md").write_text("DIFF", encoding="utf-8")
        (wd / "plan-a-round-0.md").write_text("ORIG", encoding="utf-8")
        (wd / "plan-b-round-0.md").write_text("ORIG", encoding="utf-8")
        plan_duel.freeze_round_inputs(wd, 1)
        self.assertEqual((wd / "plan-a-round-0.md").read_text(encoding="utf-8"), "ORIG")


# --------------------------------------------------------------------------- #
# Process execution (argv-list subprocess against the stub)
# --------------------------------------------------------------------------- #
class RunCliTests(_TempWorkdirMixin, unittest.TestCase):
    def test_resolve_executable_absolute_path(self):
        self.assertEqual(
            plan_duel.resolve_executable(sys.executable),
            str(Path(sys.executable).resolve()),
        )

    def test_resolve_executable_missing_raises(self):
        with self.assertRaises(plan_duel.CliNotFoundError):
            plan_duel.resolve_executable("definitely-not-a-real-cli-xyz")

    def test_run_cli_writes_file_via_argv_list(self):
        wd = self._tmpdir()
        out = wd / "plan-a.md"
        status = wd / "status.md"
        result = plan_duel.run_cli(
            _stub_argv("--write-file", str(out), "--content", "hello"),
            stdout_to=status,
        )
        self.assertEqual(result.returncode, 0)
        self.assertEqual(out.read_text(encoding="utf-8"), "hello")

    def test_run_cli_is_argv_list_not_shell(self):
        # A shell would expand $SHELL; argv-list leaves it literal.
        wd = self._tmpdir()
        echo = wd / "echo.txt"
        plan_duel.run_cli(
            _stub_argv("--echo-arg", "$SHELL", "--echo-file", str(echo))
        )
        self.assertEqual(echo.read_text(encoding="utf-8"), "$SHELL")

    def test_run_cli_honors_cwd_anchor(self):
        wd = self._tmpdir()
        cwd_out = wd / "cwd.txt"
        plan_duel.run_cli(
            _stub_argv("--cwd-file", str(cwd_out)),
            cwd=wd,
        )
        self.assertEqual(
            Path(cwd_out.read_text(encoding="utf-8")).resolve(), wd.resolve()
        )

    def test_run_cli_captures_stdout_bytes_when_no_file(self):
        result = plan_duel.run_cli(_stub_argv("--stdout", "captured-out"))
        self.assertEqual(result.stdout_bytes, b"captured-out")

    def test_run_cli_nonzero_exit_raises(self):
        with self.assertRaises(plan_duel.CliExecutionError):
            plan_duel.run_cli(_stub_argv("--exit-code", "3"))

    def test_run_cli_timeout_raises(self):
        with self.assertRaises(plan_duel.CliTimeoutError):
            plan_duel.run_cli(_stub_argv("--sleep", "5"), timeout=0.4)


# --------------------------------------------------------------------------- #
# Per-adapter output-capture policy
# --------------------------------------------------------------------------- #
class AgentCaptureTests(_TempWorkdirMixin, unittest.TestCase):
    def _agent_argv(self, out, *, content="x", min_bytes=None, exit_code=0, sleep=None):
        args = ["--write-file", str(out), "--content", content]
        if min_bytes is not None:
            args += ["--min-bytes", str(min_bytes)]
        if exit_code:
            args += ["--exit-code", str(exit_code)]
        if sleep is not None:
            args += ["--sleep", str(sleep)]
        return _stub_argv(*args)

    def test_run_agent_success_returns_output_file(self):
        wd = self._tmpdir()
        out = wd / "plan-a.md"
        got = plan_duel.run_agent(
            self._agent_argv(out, min_bytes=250),
            out,
            side="a",
            round_n=1,
            status_to=wd / "status.md",
        )
        self.assertEqual(got, out)
        self.assertGreaterEqual(plan_duel.file_size_bytes(out), 200)

    def test_run_agent_rejects_short_output_with_exact_round0_halt(self):
        wd = self._tmpdir()
        out = wd / "plan-a.md"
        with self.assertRaises(plan_duel.AgentOutputError) as ctx:
            plan_duel.run_agent(
                self._agent_argv(out, content="tiny"), out, side="a", round_n=0
            )
        self.assertEqual(
            str(ctx.exception), "Agent A plan generation failed at round 0."
        )

    def test_run_agent_short_output_uses_update_halt_for_round_n(self):
        wd = self._tmpdir()
        out = wd / "plan-b.md"
        with self.assertRaises(plan_duel.AgentOutputError) as ctx:
            plan_duel.run_agent(
                self._agent_argv(out, content="tiny"), out, side="b", round_n=4
            )
        self.assertEqual(str(ctx.exception), "Agent B update failed at round 4.")

    def test_run_agent_missing_output_surfaces_agent_status_tail(self):
        # The sandbox-rejection shape: the CLI EXITS ZERO, writes no plan, and
        # explains why on stdout. The halt must carry that explanation, not just
        # "failed at round 0".
        wd = self._tmpdir()
        out = wd / "plan-b.md"
        status = wd / "participant-round-0-status.md"
        explanation = "Unable to create plan-b.md: the workspace is read-only."
        with self.assertRaises(plan_duel.AgentOutputError) as ctx:
            plan_duel.run_agent(
                _stub_argv("--stdout", explanation),
                out,
                side="b",
                round_n=0,
                status_to=status,
            )
        self.assertFalse(out.exists())
        self.assertEqual(
            ctx.exception.halt_message, "Agent B plan generation failed at round 0."
        )
        self.assertIn(explanation, str(ctx.exception))
        self.assertIn("Agent B plan generation failed at round 0.", str(ctx.exception))

    def test_run_agent_keeps_bare_halt_when_status_is_empty(self):
        # An empty status stream adds no diagnostic value, so the halt line stays
        # byte-identical to the golden.
        wd = self._tmpdir()
        out = wd / "plan-a.md"
        status = wd / "status.md"
        with self.assertRaises(plan_duel.AgentOutputError) as ctx:
            plan_duel.run_agent(
                self._agent_argv(out, content="tiny"),
                out,
                side="a",
                round_n=0,
                status_to=status,
            )
        self.assertIsNone(ctx.exception.cause)
        self.assertEqual(
            str(ctx.exception), "Agent A plan generation failed at round 0."
        )

    def test_status_tail_collapses_and_truncates(self):
        wd = self._tmpdir()
        status = wd / "status.md"
        status.write_text("head\n\n" + "z" * 900 + "\ntail line", encoding="utf-8")
        tail = plan_duel.status_tail(status, max_chars=100)
        self.assertTrue(tail.startswith("last output: …"))
        self.assertTrue(tail.endswith("tail line"))
        self.assertNotIn("\n", tail)
        self.assertNotIn("head", tail)

    def test_status_tail_none_without_file(self):
        self.assertIsNone(plan_duel.status_tail(None))
        self.assertIsNone(plan_duel.status_tail(self._tmpdir() / "absent.md"))

    def test_status_tail_falls_back_to_captured_bytes(self):
        # An agent given no status path (Agent A) still has its explanation in the
        # captured stdout; stderr is the last resort when stdout says nothing.
        self.assertIn(
            "no room left",
            plan_duel.status_tail(None, stdout_bytes=b"no room left on device"),
        )
        self.assertIn(
            "transcript tail",
            plan_duel.status_tail(None, stdout_bytes=b"   ", stderr_bytes=b"transcript tail"),
        )

    def test_status_tail_survives_non_utf8_output(self):
        # A decode error must never replace the halt it is trying to explain.
        wd = self._tmpdir()
        status = wd / "status.md"
        status.write_bytes(b"\xff\xfe binary garbage")
        self.assertIsNotNone(plan_duel.status_tail(status))
        self.assertIsNotNone(plan_duel.status_tail(None, stdout_bytes=b"\xff\xfe oops"))

    def test_run_agent_reports_agent_a_explanation_without_status_file(self):
        wd = self._tmpdir()
        out = wd / "plan-a.md"
        with self.assertRaises(plan_duel.AgentOutputError) as ctx:
            plan_duel.run_agent(
                _stub_argv("--stdout", "cannot write: workspace is read-only"),
                out,
                side="a",
                round_n=0,
            )
        self.assertIn("workspace is read-only", str(ctx.exception))

    def test_run_agent_rejects_a_directory_at_the_output_path(self):
        # A directory's ``st_size`` is 4096 on Linux, clearing the >=200 B gate, so a
        # size-only check accepts it and ``copy_bytes`` dies later with a bare
        # ``IsADirectoryError``. The 4096 is FORCED, not assumed: Windows reports a
        # smaller size, where a size-only check would raise for the wrong reason and
        # this test would pass against the very code it exists to reject.
        wd = self._tmpdir()
        out = wd / "plan-a.md"
        out.mkdir()
        real_size = plan_duel.file_size_bytes

        def sized(path, *args, **kwargs):
            return 4096 if Path(path) == out else real_size(path, *args, **kwargs)

        with unittest.mock.patch.object(plan_duel, "file_size_bytes", sized):
            with self.assertRaises(plan_duel.AgentOutputError) as ctx:
                plan_duel.run_agent(
                    _stub_argv("--stdout", "wrote a directory"),
                    out,
                    side="a",
                    round_n=0,
                    status_to=wd / "status.md",
                )
        self.assertEqual(
            ctx.exception.halt_message, "Agent A plan generation failed at round 0."
        )

    def test_run_agent_translates_a_read_failure_on_the_output_file(self):
        # The other half: a REGULAR file that clears the size gate but cannot be read.
        # Validating by stat alone lets ``run_agent`` succeed and ``copy_bytes`` raise a
        # bare ``PermissionError`` from outside the diagnostic path. Patched rather than
        # chmod'd because a 0o000 file is still readable by root and on Windows, so a
        # permission fixture would not exercise this on two of the three CI platforms.
        wd = self._tmpdir()
        out = wd / "plan-a.md"
        real_read_bytes = Path.read_bytes

        def denied(self_path, *args, **kwargs):
            if self_path == out:
                raise PermissionError(13, "Permission denied")
            return real_read_bytes(self_path, *args, **kwargs)

        with unittest.mock.patch.object(Path, "read_bytes", denied):
            with self.assertRaises(plan_duel.AgentOutputError) as ctx:
                plan_duel.run_agent(
                    self._agent_argv(out, min_bytes=250),
                    out,
                    side="b",
                    round_n=2,
                    status_to=wd / "status.md",
                )
        self.assertEqual(
            ctx.exception.halt_message, "Agent B update failed at round 2."
        )

    def test_run_agent_nonzero_exit_halts(self):
        wd = self._tmpdir()
        out = wd / "plan-a.md"
        with self.assertRaises(plan_duel.AgentOutputError) as ctx:
            plan_duel.run_agent(
                self._agent_argv(out, min_bytes=250, exit_code=2),
                out,
                side="a",
                round_n=2,
            )
        self.assertIn("Agent A update failed at round 2.", str(ctx.exception))

    def test_run_agent_timeout_halts(self):
        wd = self._tmpdir()
        out = wd / "plan-a.md"
        with self.assertRaises(plan_duel.AgentOutputError):
            plan_duel.run_agent(
                self._agent_argv(out, min_bytes=250, sleep=5),
                out,
                side="a",
                round_n=1,
                timeout=0.4,
            )

    def test_recover_agent_b_round0_uses_recent_md_and_excludes_named(self):
        wd = self._tmpdir()
        (wd / "problem.md").write_text("p" * 300, encoding="utf-8")
        (wd / "plan-a.md").write_text("a" * 300, encoding="utf-8")
        (wd / "participant-round-0-status.md").write_text("s" * 300, encoding="utf-8")
        stray = wd / "codex-plan.md"
        stray.write_text("real plan " * 40, encoding="utf-8")  # >200 bytes
        msg = plan_duel.recover_agent_b_round0(wd, now=time.time())
        self.assertEqual(msg, "Fallback: used codex-plan.md as plan-b.md.")
        self.assertEqual((wd / "plan-b.md").read_bytes(), stray.read_bytes())

    def test_recover_agent_b_round0_never_adopts_a_plan_snapshot(self):
        # Plan A's round-0 snapshot now lands BEFORE Agent B runs, so it is always
        # recent and large. Adopting it would make Plan B a silent copy of Plan A.
        wd = self._tmpdir()
        (wd / "problem.md").write_text("p" * 300, encoding="utf-8")
        (wd / "plan-a.md").write_text("a" * 300, encoding="utf-8")
        (wd / "plan-a-round-0.md").write_text("PLAN A " * 60, encoding="utf-8")
        self.assertIsNone(plan_duel.recover_agent_b_round0(wd, now=time.time()))
        self.assertFalse((wd / "plan-b.md").exists())

    @unittest.skipUnless(os.name == "posix", "symlink semantics differ on Windows")
    def test_recover_agent_b_round0_never_follows_a_link_out_of_the_workdir(self):
        """A link in the workdir is a sandbox escape, and the engine is the unconfined half.

        A participant confined to the workdir can still create a link inside it. The
        scan's `is_file()`/`stat()` and `copy_bytes`' read all dereference, so an outside
        file's bytes become Plan B. `copy_bytes` writes a real regular file, so no later
        lstat guard can see it.
        """
        wd = self._tmpdir()
        outside = wd.parent / "private-notes.txt"
        outside.write_text("SECRET OUTSIDE THE WORKDIR " * 40, encoding="utf-8")
        (wd / "problem.md").write_text("p" * 300, encoding="utf-8")
        (wd / "plan-a.md").write_text("a" * 300, encoding="utf-8")
        os.symlink(outside, wd / "scratch.md")
        self.assertIsNone(plan_duel.recover_agent_b_round0(wd, now=time.time()))
        self.assertFalse((wd / "plan-b.md").exists())

    def test_recover_agent_b_round0_still_finds_a_real_stray_beside_snapshots(self):
        wd = self._tmpdir()
        (wd / "plan-a-round-0.md").write_text("PLAN A " * 60, encoding="utf-8")
        stray = wd / "codex-plan.md"
        stray.write_text("real plan " * 40, encoding="utf-8")
        msg = plan_duel.recover_agent_b_round0(wd, now=time.time())
        self.assertEqual(msg, "Fallback: used codex-plan.md as plan-b.md.")
        self.assertEqual((wd / "plan-b.md").read_bytes(), stray.read_bytes())

    def test_recover_agent_b_round0_none_when_only_old_or_short_or_excluded(self):
        wd = self._tmpdir()
        now = time.time()
        (wd / "problem.md").write_text("p" * 300, encoding="utf-8")  # excluded
        short = wd / "tiny.md"
        short.write_text("short", encoding="utf-8")  # < 200 bytes
        old = wd / "old-plan.md"
        old.write_text("old plan " * 40, encoding="utf-8")
        os.utime(old, (now - 600, now - 600))  # older than 5 min
        self.assertIsNone(plan_duel.recover_agent_b_round0(wd, now=now))


class PreflightTests(_TempWorkdirMixin, unittest.TestCase):
    def _specs(self, **clis):
        base = {"agent_a": sys.executable, "agent_b": sys.executable, "judge": sys.executable}
        base.update(clis)
        return plan_duel.parse_adapter_config(
            {
                role: {"command": [cli, "--version"], "stdout": "file"}
                for role, cli in base.items()
            }
        )

    def test_preflight_passes_when_every_cli_resolves(self):
        self.assertIsNone(plan_duel.preflight_executables(self._specs()))

    def test_preflight_names_the_missing_cli_and_its_roles(self):
        specs = self._specs(agent_b="definitely-not-a-real-cli-xyz")
        with self.assertRaises(plan_duel.CliNotFoundError) as ctx:
            plan_duel.preflight_executables(specs)
        self.assertIn("definitely-not-a-real-cli-xyz", str(ctx.exception))
        self.assertIn("agent_b", str(ctx.exception))

    def test_preflight_groups_roles_sharing_one_missing_cli(self):
        specs = self._specs(
            agent_a="definitely-not-a-real-cli-xyz",
            judge="definitely-not-a-real-cli-xyz",
        )
        with self.assertRaises(plan_duel.CliNotFoundError) as ctx:
            plan_duel.preflight_executables(specs)
        self.assertIn("agent_a, judge", str(ctx.exception))

    def test_resume_that_spawns_nothing_does_not_require_the_clis(self):
        # All rounds complete but summary.md missing: the resume only has to write the
        # summary, so a missing CLI must not block recovering it.
        wd = self._tmpdir() / "wd"
        wd.mkdir()
        (wd / "problem.md").write_text("p" * 300, encoding="utf-8")
        for side in ("a", "b"):
            for rnd in range(0, 11):
                (wd / f"plan-{side}-round-{rnd}.md").write_text(
                    "x" * 300, encoding="utf-8"
                )
        msgs = []
        rc = plan_duel.execute(
            argument=str(wd),
            specs=self._specs(agent_b="definitely-not-a-real-cli-xyz"),
            controller_name="Claude",
            participant_name="Codex",
            emit=msgs.append,
        )
        self.assertEqual(rc, 0)
        self.assertTrue((wd / "summary.md").is_file())

    def test_new_run_halts_before_spending_a_plan_run(self):
        # The whole point: a missing participant CLI must not cost a full Plan A.
        wd = self._tmpdir() / "wd"
        with self.assertRaises(plan_duel.CliNotFoundError):
            plan_duel.execute(
                argument="Design something.",
                workdir_arg=str(wd),
                specs=self._specs(agent_b="definitely-not-a-real-cli-xyz"),
                controller_name="Claude",
                participant_name="Codex",
                emit=lambda _m: None,
            )
        self.assertFalse(wd.exists())


class JudgeCaptureTests(_TempWorkdirMixin, unittest.TestCase):
    def test_judge_capture_reads_clean_file_not_transcript_stdout(self):
        # The stub writes a CLEAN last-message file (real SCORE 7) while also
        # emitting a noisy "transcript" to stdout carrying a bogus SCORE 99.
        wd = self._tmpdir()
        judge_file = wd / "judge-round-1.md"
        status = wd / "status.md"
        argv = _stub_argv(
            "--write-file",
            str(judge_file),
            "--content",
            "SCORE: 7\n\nPREFERRED: A\n",
            "--stdout",
            "== transcript ==\nSCORE: 99 (echoed prompt template)\n",
        )
        message = plan_duel.capture_judge_message(
            argv, judge_file, status_to=status, round_n=1
        )
        self.assertEqual(plan_duel.parse_score(message), 7)

    def test_judge_capture_redirect_stdout_mode(self):
        # A clean-stdout runtime: the engine redirects stdout into the message file.
        wd = self._tmpdir()
        judge_file = wd / "judge-round-1.md"
        argv = _stub_argv("--stdout", "SCORE: 8\n\nPREFERRED: B\n")
        message = plan_duel.capture_judge_message(
            argv, judge_file, redirect_stdout=True, round_n=1
        )
        self.assertEqual(plan_duel.parse_score(message), 8)
        self.assertEqual(plan_duel.parse_preferred(message), "B")

    def test_judge_capture_tolerates_undecodable_bytes_in_the_message(self):
        # The judge file is written by a third-party CLI. One cp1252 byte in its
        # prose must not abort the round before the SCORE line is ever parsed.
        wd = self._tmpdir()
        judge_file = wd / "judge-round-1.md"
        judge_file.write_bytes(b"SCORE: 7\n\nThe plan\x92s scope.\n\nPREFERRED: A\n")
        argv = _stub_argv("--stdout", "transcript noise")
        message = plan_duel.capture_judge_message(
            argv, judge_file, status_to=wd / "status.md", round_n=1
        )
        self.assertEqual(plan_duel.parse_score(message), 7)
        self.assertIn("\ufffd", message)

    def test_judge_capture_missing_message_raises(self):
        wd = self._tmpdir()
        judge_file = wd / "judge-round-1.md"
        # Stub writes nothing to judge_file (only stdout noise).
        argv = _stub_argv("--stdout", "noise only")
        with self.assertRaises(plan_duel.JudgeOutputError):
            plan_duel.capture_judge_message(
                argv, judge_file, status_to=wd / "status.md", round_n=1
            )

    def test_judge_capture_nonzero_exit_raises(self):
        wd = self._tmpdir()
        judge_file = wd / "judge-round-1.md"
        argv = _stub_argv(
            "--write-file", str(judge_file), "--content", "SCORE: 7\n",
            "--exit-code", "1",
        )
        with self.assertRaises(plan_duel.JudgeOutputError):
            plan_duel.capture_judge_message(
                argv, judge_file, status_to=wd / "status.md", round_n=1
            )


# --------------------------------------------------------------------------- #
# Progress file (optional, non-blocking, append-only)
# --------------------------------------------------------------------------- #
class ProgressTests(_TempWorkdirMixin, unittest.TestCase):
    def test_append_progress_never_clobbers_prior_line(self):
        wd = self._tmpdir()
        progress = wd / "participant-progress-1.md"
        plan_duel.append_progress(progress, "line one\n")
        plan_duel.append_progress(progress, "line two\n")
        self.assertEqual(
            progress.read_text(encoding="utf-8"), "line one\nline two\n"
        )

    def test_run_cli_stdout_append_mode_does_not_truncate(self):
        wd = self._tmpdir()
        progress = wd / "participant-progress-1.md"
        plan_duel.append_progress(progress, "seed\n")
        plan_duel.run_cli(
            _stub_argv("--stdout", "streamed\n"),
            stdout_to=progress,
            stdout_append=True,
        )
        self.assertEqual(progress.read_text(encoding="utf-8"), "seed\nstreamed\n")

    def test_agent_outcome_identical_with_progress_on_or_off(self):
        def run_once(enable_progress):
            wd = self._tmpdir()
            out = wd / "plan-a.md"
            argv = _stub_argv(
                "--write-file", str(out), "--content", "PLAN", "--min-bytes", "250"
            )
            plan_duel.run_agent(argv, out, side="a", round_n=1,
                                status_to=wd / "status.md")
            if enable_progress:
                plan_duel.append_progress(wd / "participant-progress-1.md", "note\n")
            return out.read_bytes()

        self.assertEqual(run_once(False), run_once(True))


# =========================================================================== #
# Summary assembly, winner stamping, scoped rewrite, end-to-end loop
# =========================================================================== #

_SCENARIOS = _FIXTURES / "scenario"
_SCENARIO_STUB = _FIXTURES / "scenario_stub.py"


def _sample_judge(score="8", preferred="A", missed="none"):
    return (
        f"SCORE: {score}\n\n"
        "DIFFERENCES:\n"
        "1. Auth: Plan A uses JWT. Plan B uses sessions. **Stronger: A** — stateless.\n"
        "2. Rollback: Plan A skips it. Plan B has a plan. **Stronger: B** — safer.\n\n"
        f"MISSED REJECTIONS: {missed}\n\n"
        f"PREFERRED: {preferred}\n"
        "Plan A is clearer; Plan B hand-waves rollout. Both reference the store.\n"
    )


# --------------------------------------------------------------------------- #
# Judge-field extraction
# --------------------------------------------------------------------------- #
class ExtractJudgeFieldsTests(unittest.TestCase):
    def test_extracts_all_fields(self):
        f = plan_duel.extract_judge_fields(_sample_judge())
        self.assertEqual(f.score, 8)
        self.assertEqual(f.preferred, "A")
        self.assertEqual(f.missed_rejections, "none")
        self.assertIn("1. Auth:", f.differences)
        self.assertIn("2. Rollback:", f.differences)
        self.assertNotIn("MISSED REJECTIONS", f.differences)
        self.assertNotIn("PREFERRED", f.differences)
        self.assertTrue(f.justification.startswith("Plan A is clearer"))

    def test_missed_rejections_non_none_preserved(self):
        f = plan_duel.extract_judge_fields(
            _sample_judge(missed="Plan A dropped idempotency keys.")
        )
        self.assertEqual(f.missed_rejections, "Plan A dropped idempotency keys.")

    def test_unparseable_score_is_none(self):
        text = "The plans still diverge.\n\nDIFFERENCES:\n1. x\n\nPREFERRED: A\nbecause.\n"
        f = plan_duel.extract_judge_fields(text)
        self.assertIsNone(f.score)
        self.assertEqual(f.preferred, "A")

    def test_differences_none_form(self):
        text = "SCORE: 9\n\nDIFFERENCES: none\n\nMISSED REJECTIONS: none\n\nPREFERRED: A\nok.\n"
        f = plan_duel.extract_judge_fields(text)
        self.assertEqual(f.differences, "none")


def _sample_verdict(score=8, preferred="A", missed=None, differences=None):
    """The schema-shaped judge verdict, as the CLIs emit it (bare, no fence)."""
    return json.dumps(
        {
            "score": score,
            "differences": differences
            if differences is not None
            else [
                {
                    "topic": "Auth",
                    "plan_a": "uses JWT",
                    "plan_b": "uses sessions",
                    "stronger": "A",
                    "reason": "stateless",
                },
                {
                    "topic": "Rollback",
                    "plan_a": "skips it",
                    "plan_b": "has a plan",
                    "stronger": "B",
                    "reason": "safer",
                },
            ],
            "missed_rejections": missed if missed is not None else [],
            "preferred": preferred,
            "justification": "Plan A is clearer; Plan B hand-waves rollout.",
        }
    )


# --------------------------------------------------------------------------- #
# JSON verdict decoding (the schema-enforced contract)
# --------------------------------------------------------------------------- #
class ParseJudgeJsonTests(unittest.TestCase):
    def test_bare_object_is_decoded(self):
        obj = plan_duel.parse_judge_json(_sample_verdict())
        self.assertIsNotNone(obj)
        self.assertEqual(obj["score"], 8)

    def test_fenced_object_is_decoded(self):
        # A runtime with NO schema flag still answers in JSON (the prompt asks for
        # it) but may fence it. That must parse, not fall through to the markers.
        text = "Here is the verdict:\n\n```json\n" + _sample_verdict() + "\n```\n"
        obj = plan_duel.parse_judge_json(text)
        self.assertEqual(obj["preferred"], "A")

    def test_object_wrapped_in_prose_is_decoded(self):
        text = "Assessment follows. " + _sample_verdict() + " Let me know."
        self.assertEqual(plan_duel.parse_judge_json(text)["score"], 8)

    def test_last_qualifying_object_wins(self):
        # In a transcript the FINAL object is the answer; an earlier sketch is not.
        text = _sample_verdict(score=2) + "\n\nOn reflection:\n" + _sample_verdict(score=9)
        self.assertEqual(plan_duel.parse_judge_json(text)["score"], 9)

    def test_unrelated_json_is_not_adopted_as_a_verdict(self):
        self.assertIsNone(plan_duel.parse_judge_json('{"unrelated": true}'))

    def test_a_single_incidental_key_is_not_enough_to_be_a_verdict(self):
        # A legacy judge file whose justification QUOTES a JSON payload must not have
        # that payload adopted as the verdict just because it happens to say "score".
        quoted = '... the endpoint returns {"score": 0.82, "id": 17} to the caller ...'
        self.assertIsNone(plan_duel.parse_judge_json(quoted))

    def test_two_keys_still_qualify_so_a_degraded_verdict_survives(self):
        # The bar is 2, not "all": the judge file IS the duel's product, so a partial
        # verdict must still parse rather than silently score the round 0.
        self.assertIsNotNone(plan_duel.parse_judge_json('{"score": 3, "preferred": "B"}'))

    def test_non_json_returns_none(self):
        self.assertIsNone(plan_duel.parse_judge_json("SCORE: 7\n\nPREFERRED: A\n"))
        self.assertIsNone(plan_duel.parse_judge_json(""))

    def test_truncated_object_returns_none_rather_than_raising(self):
        self.assertIsNone(plan_duel.parse_judge_json('{"score": 8, "preferred": '))


class JsonScoreAndPreferredTests(unittest.TestCase):
    def test_score_and_preferred_from_the_verdict(self):
        text = _sample_verdict(score=6, preferred="B")
        self.assertEqual(plan_duel.parse_score(text), 6)
        self.assertEqual(plan_duel.parse_preferred(text), "B")

    def test_legacy_markers_still_parse_unchanged(self):
        text = _sample_judge(score=4, preferred="B")
        self.assertEqual(plan_duel.parse_score(text), 4)
        self.assertEqual(plan_duel.parse_preferred(text), "B")

    def test_boolean_score_is_not_an_integer_score(self):
        # bool is an int subclass in Python, so an unguarded isinstance would score
        # `"score": true` as 1 — a silently wrong round rather than a warned one.
        self.assertIsNone(plan_duel.parse_score('{"score": true, "preferred": "A"}'))

    def test_verdict_missing_score_falls_through_to_the_marker_parser(self):
        # A degraded verdict must not be NARROWER than the legacy path: if the object
        # lacks a usable score but the text still carries a SCORE: line, use it.
        text = '{"preferred": "A", "justification": "x"}\n\nSCORE: 7\n'
        self.assertEqual(plan_duel.parse_score(text), 7)

    def test_verdict_with_unusable_preferred_falls_through(self):
        text = '{"score": 5, "preferred": "maybe"}\n\nPREFERRED: B\n'
        self.assertEqual(plan_duel.parse_preferred(text), "B")

    def test_unparseable_input_degrades_to_none_for_both(self):
        self.assertIsNone(plan_duel.parse_score("the plans still diverge"))
        self.assertIsNone(plan_duel.parse_preferred("the plans still diverge"))

    def test_string_score_is_read_leniently(self):
        self.assertEqual(plan_duel.parse_score('{"score": "8/10", "preferred": "A"}'), 8)


class BothPreferredPathsResolveTheWinnerIdentically(unittest.TestCase):
    """`summary.md` says so; until this they did not.

    The JSON path upper-cases and strips, so `"preferred": "b"` resolves. The legacy
    marker path demanded a bare uppercase `PREFERRED: B`, so `**PREFERRED:** B`
    resolved to nothing and the winner DEFAULTED TO A — the loser published as the
    winner, with one warning line to notice.
    """

    RESOLVING = (
        ("PREFERRED: A\n", "A"),
        ("PREFERRED: B\n", "B"),
        ("PREFERRED: B", "B"),                      # no trailing newline
        ("PREFERRED: B\r\n", "B"),                  # CRLF, unnormalized
        ("preferred: b\n", "B"),
        ("Preferred: A\n", "A"),
        ("PREFERRED: b\n", "B"),
        ("**PREFERRED:** B\n", "B"),
        ("**PREFERRED: B**\n", "B"),
        ("**Preferred:** Plan B\n", "B"),
        ("PREFERRED: Plan A\n", "A"),
        ("PREFERRED: plan a\n", "A"),
        ("  PREFERRED: B\n", "B"),
        ("PREFERRED: B  \n", "B"),                  # trailing spaces
        ("PREFERRED: B.\n", "B"),                   # a full stop
        ("PREFERRED : B\n", "B"),                   # space before the colon
        ("PREFERRED: B\nmore text\n", "B"),         # prose on a LATER line
        ("Some prose.\n\nPREFERRED: Plan B\n\nMore prose.\n", "B"),
        # An EXPLICIT side followed by its own explanation. Rejecting these is not
        # neutral: the caller defaults to A, so `PREFERRED: B because …` publishes
        # plan A against an explicit B verdict.
        ("PREFERRED: B because it scopes the migration\n", "B"),
        ("PREFERRED: A because it is simpler\n", "A"),
        ("PREFERRED: B — tighter scope\n", "B"),
        ("PREFERRED: B, it handles rollback\n", "B"),
        ("PREFERRED: B: the migration is staged\n", "B"),
        ("PREFERRED: Plan B (the rollback plan)\n", "B"),
        ("**PREFERRED:** B — see the note below\n", "B"),
        # A line that names no side does not stop a later one that does.
        ("PREFERRED: a compromise was not available\n\nPREFERRED: B\n", "B"),
    )

    # Every one of these must resolve to nothing — the letter is an English article or
    # part of a word, not a side, as in `Preferred: a compromise between both plans.`
    NOT_RESOLVING = (
        "no preference stated",
        "PREFERRED: C",
        "PREFERRED: AB",
        "Preferred: approach B\n",
        "Preferred: a compromise between both plans.\n",
        "Preferred: a merge of both approaches\n",
        "Preferred: a blend of A and B\n",
        "PREFERRED: b ut the tradeoff is real\n",
        "Preferred: an approach from each\n",
        # A CAPITALISED sentence must not resolve to a side, or a judge writing this
        # line early and `PREFERRED: B because it is simpler.` later publishes plan A.
        # The prose form now requires a connector, and the test for connector
        # membership is whether the word can follow the article "a".
        "PREFERRED: A compromise between both plans.\n",
        "PREFERRED: A compromise between both plans was considered.\n",
        "PREFERRED: A merge of the two\n",
        "PREFERRED: A hybrid approach\n",
        "preferred: both, honestly\n",
        "I preferred the second plan.\n",
        "The preferred outcome depends on budget.\n",
        "The PREFERRED: B line was missing from round 2.\n",  # not at line start
    )

    # A `PREFERRED:` label whose value names no side — distinct from "no such line",
    # and reported differently: the engine says it could not read the line rather
    # than defaulting quietly.
    UNREADABLE_LABELS = (
        "PREFERRED: C",
        "PREFERRED: AB",
        "Preferred: a compromise between both plans.\n",
        "Preferred: a merge of both approaches\n",
        "PREFERRED: b ut the tradeoff is real\n",
    )

    def test_every_decorated_form_resolves(self):
        for text, expected in self.RESOLVING:
            with self.subTest(text=text):
                self.assertEqual(plan_duel.parse_preferred(text), expected)

    def test_the_answer_is_always_upper_case(self):
        """`resolve_winner` raises ValueError on anything but 'A'/'B'."""
        for text, expected in self.RESOLVING:
            with self.subTest(text=text):
                side = plan_duel.parse_preferred(text)
                self.assertEqual(side, expected)
                plan_duel.resolve_winner(side, "Claude", "Codex")  # must not raise

    def test_the_json_path_agrees_on_every_one_of_them(self):
        """The claim being asserted: same verdict, same winner, either encoding."""
        for text, expected in self.RESOLVING:
            with self.subTest(text=text):
                as_json = '{"score": 8, "preferred": "%s"}' % expected.lower()
                self.assertEqual(plan_duel.parse_preferred(as_json),
                                 plan_duel.parse_preferred(text))

    def test_prose_that_merely_mentions_a_preference_still_resolves_to_nothing(self):
        """Case folding plus an optional `Plan ` prefix made the English article `a` a vote.

        `Preferred: a compromise between both plans.` resolved to A and published the
        wrong plan. The side letter must be the last thing on its line (bar `**`, a full
        stop and whitespace) — what the JSON contract already requires of `preferred`.
        """
        for text in self.NOT_RESOLVING:
            with self.subTest(text=text):
                self.assertIsNone(plan_duel.parse_preferred(text))

    def test_a_decorated_marker_yields_the_justification_too(self):
        """The side and the justification must be found by ONE definition of the line.

        A case-sensitive `startswith` on `PREFERRED:` resolved the winner from a
        decorated marker while the justification came back empty and the marker line
        leaked into the preceding block.
        """
        verdict = ("SCORE: 8\nDIFFERENCES:\n1. scope\nMISSED REJECTIONS: none\n"
                   "**PREFERRED:** B\nA is broad; B is specific.\n")
        fields = plan_duel.extract_judge_fields(verdict)
        self.assertEqual(fields.preferred, "B")
        self.assertEqual(fields.justification, "A is broad; B is specific.")
        self.assertEqual(fields.differences, "1. scope")
        self.assertEqual(fields.missed_rejections, "none")
        self.assertNotIn("PREFERRED", fields.missed_rejections)

    def test_the_undecorated_marker_extraction_is_unchanged(self):
        """Anti-vacuity: the plain form must keep parsing exactly as it did."""
        verdict = ("SCORE: 8\nDIFFERENCES:\n1. scope\nMISSED REJECTIONS: none\n"
                   "PREFERRED: B\nA is broad; B is specific.\n")
        fields = plan_duel.extract_judge_fields(verdict)
        self.assertEqual(
            (fields.preferred, fields.justification, fields.differences,
             fields.missed_rejections),
            ("B", "A is broad; B is specific.", "1. scope", "none"))

    def test_prose_before_the_marker_does_not_become_the_marker(self):
        """A sentence starting `Preferred:` must not steal the justification either."""
        verdict = ("SCORE: 8\nDIFFERENCES:\n1. scope\n"
                   "Preferred: a compromise was not available.\n"
                   "MISSED REJECTIONS: none\nPREFERRED: B\nB is specific.\n")
        fields = plan_duel.extract_judge_fields(verdict)
        self.assertEqual(fields.preferred, "B")
        self.assertEqual(fields.justification, "B is specific.")

    def test_a_side_with_an_explanation_keeps_its_justification_line(self):
        """The trailing text is the explanation; the paragraph under it still follows."""
        verdict = ("SCORE: 8\nDIFFERENCES:\n1. scope\nMISSED REJECTIONS: none\n"
                   "PREFERRED: B because it scopes the migration\n"
                   "B stages the cutover; A does it in one step.\n")
        fields = plan_duel.extract_judge_fields(verdict)
        self.assertEqual(fields.preferred, "B")
        self.assertEqual(fields.justification,
                         "B stages the cutover; A does it in one step.")

    def test_an_unreadable_preference_line_is_told_apart_from_no_line_at_all(self):
        """"I could not read it" and "there was none" are different, and are said so."""
        for text in self.UNREADABLE_LABELS:
            with self.subTest(text=text):
                reading = plan_duel.read_preferred_marker(text)
                self.assertIsNone(reading.side)
                self.assertIsNotNone(
                    reading.unreadable,
                    "a PREFERRED label that names no side was reported as absent")
                self.assertIn("PREFERRED", reading.unreadable.upper())

    def test_text_with_no_preference_line_reports_nothing_to_read(self):
        """Anti-vacuity: not every unresolved text is an unreadable label."""
        for text in ("no preference stated", "I preferred the second plan.\n",
                     "SCORE: 8\n"):
            with self.subTest(text=text):
                reading = plan_duel.read_preferred_marker(text)
                self.assertIsNone(reading.side)
                self.assertIsNone(reading.unreadable)

    def test_a_resolved_line_reports_no_unreadable_one(self):
        reading = plan_duel.read_preferred_marker(
            "PREFERRED: a compromise was not available\n\nPREFERRED: B\n")
        self.assertEqual(reading.side, "B")
        self.assertIsNone(reading.unreadable)


class AnUnreadablePreferenceLineIsNeverASilentDefault(_TempWorkdirMixin,
                                                      unittest.TestCase):
    """The winner must never be defaulted quietly off a line the engine could not read.

    `write_summary` defaults to A with a warning when no side resolves. "No parseable
    PREFERRED line" is true when the judge wrote none and misleading when it wrote one
    the engine declined to read — and only the second is fixable by editing that line.
    """

    def _workdir(self, preferred_line):
        wd = self._tmpdir() / "wd"
        wd.mkdir()
        body = "This is a plan body sentence. " * 10 + "\n"
        (wd / "problem.md").write_text("Problem.\n", encoding="utf-8")
        for side in ("a", "b"):
            (wd / f"plan-{side}.md").write_text(body, encoding="utf-8")
            for n in (0, 1):
                (wd / plan_duel.plan_snapshot_name(side, n)).write_text(
                    body, encoding="utf-8")
        (wd / "judge-round-1.md").write_text(
            f"SCORE: 8\n{preferred_line}\n", encoding="utf-8")
        return wd

    def _messages(self, preferred_line):
        wd = self._workdir(preferred_line)
        msgs = []
        plan_duel.write_summary(
            workdir=wd, rounds_run=1, stopped_due_to="Convergence",
            controller_name="Claude", participant_name="Codex", emit=msgs.append)
        return "\n".join(msgs)

    def test_it_quotes_the_line_it_could_not_read(self):
        joined = self._messages("PREFERRED: a compromise between both plans.")
        self.assertIn("a compromise between both plans", joined)
        self.assertIn("NOT read", joined)
        self.assertIn("judge-round-1.md", joined)

    def test_it_does_not_claim_there_was_no_line(self):
        joined = self._messages("PREFERRED: a compromise between both plans.")
        self.assertNotIn("no parseable PREFERRED line", joined)

    def test_a_genuinely_absent_line_still_gets_the_original_message(self):
        """Anti-vacuity: the two messages must not collapse into one."""
        joined = self._messages("The plans remain far apart.")
        self.assertIn("no parseable PREFERRED line", joined)

    def test_a_readable_line_warns_about_neither(self):
        joined = self._messages("PREFERRED: B because it scopes the migration")
        self.assertNotIn("no parseable PREFERRED line", joined)
        self.assertNotIn("NOT read", joined)

    def test_the_explained_side_is_the_one_published(self):
        """Publishing plan A here would name the wrong winner."""
        wd = self._workdir("PREFERRED: B because it scopes the migration")
        plan_duel.write_summary(
            workdir=wd, rounds_run=1, stopped_due_to="Convergence",
            controller_name="Claude", participant_name="Codex", emit=lambda _m: None)
        summary = (wd / "summary.md").read_text(encoding="utf-8")
        self.assertIn("**Winner:** Codex", summary)
        self.assertIn("| Format | v2 |",
                      (wd / "plan-codex.md").read_text(encoding="utf-8"))


class ScoreRangeTests(unittest.TestCase):
    """An out-of-range number is not a score — treated as 0, and named in the warning.

    ``convergence_exit`` fires on ``score >= 8``, so a judge answering 50 would end the
    duel at round 3 on a value the rubric cannot produce. Clamping to 10 would converge
    just as wrongly and do it silently.
    """

    def test_boundaries_are_inclusive(self):
        for value in (0, 5, 10):
            self.assertEqual(plan_duel.parse_score(f"SCORE: {value}"), value)
            self.assertEqual(
                plan_duel.parse_score(f'{{"score": {value}, "preferred": "A"}}'), value
            )

    def test_out_of_range_is_not_a_usable_score_in_either_contract(self):
        for text in ("SCORE: 50", "SCORE: 11",
                     '{"score": 50, "preferred": "A"}',
                     '{"score": 11, "preferred": "A"}'):
            with self.subTest(text=text):
                self.assertIsNone(plan_duel.parse_score(text))

    def test_out_of_range_does_not_converge_the_duel(self):
        # The whole point. Before the range check this reported
        # "Convergence reached at round 3 (score: 50/10)".
        score = plan_duel.parse_score("SCORE: 50")
        self.assertIsNone(plan_duel.convergence_exit(3, 0 if score is None else score))

    def test_raw_score_still_reports_what_the_judge_wrote(self):
        self.assertEqual(plan_duel.raw_score("SCORE: 50"), 50)
        self.assertEqual(plan_duel.raw_score('{"score": 50, "preferred": "A"}'), 50)
        self.assertIsNone(plan_duel.raw_score("no score at all"))

    def test_warning_names_the_number_when_one_was_written(self):
        # Reporting "could not parse" would send a user hunting for a missing line in
        # a file that plainly shows a number.
        message = plan_duel.score_warning("SCORE: 50", 4)
        self.assertIn("score 50 at round 4", message)
        self.assertIn("outside the 0–10 rubric", message)
        self.assertIn("treating as 0", message)

    def test_warning_keeps_the_original_wording_when_nothing_parsed(self):
        self.assertEqual(
            plan_duel.score_warning("the plans still diverge", 4),
            "Warning: could not parse score at round 4 — treating as 0",
        )

    def test_out_of_range_json_falls_through_to_a_valid_marker_score(self):
        # The degrade path must never be narrower: a usable marker score still wins
        # over an unusable JSON one.
        self.assertEqual(
            plan_duel.parse_score('{"score": 99, "preferred": "A"}\n\nSCORE: 6\n'), 6
        )


class JudgeFieldsFromVerdictTests(unittest.TestCase):
    def test_verdict_fields_render_into_the_marker_shaped_block(self):
        # One downstream path: the JSON array renders into the same numbered lines the
        # marker contract produced, so rewrite_differences and summary.md are unchanged.
        f = plan_duel.extract_judge_fields(_sample_verdict())
        self.assertEqual(f.score, 8)
        self.assertEqual(f.preferred, "A")
        self.assertEqual(f.missed_rejections, "none")
        self.assertIn("1. Auth: Plan A: uses JWT. Plan B: uses sessions.", f.differences)
        self.assertIn("**Stronger: A** — stateless", f.differences)
        self.assertIn("2. Rollback:", f.differences)
        self.assertTrue(f.justification.startswith("Plan A is clearer"))

    def test_rendered_differences_survive_the_scoped_name_rewrite(self):
        f = plan_duel.extract_judge_fields(_sample_verdict())
        out = plan_duel.rewrite_differences(f.differences, "Claude", "Codex")
        self.assertIn("**Stronger: Claude**", out)
        self.assertIn("**Stronger: Codex**", out)
        self.assertNotIn("Plan A", out)
        self.assertNotIn("Plan B", out)

    def test_empty_differences_array_renders_as_none(self):
        f = plan_duel.extract_judge_fields(_sample_verdict(differences=[]))
        self.assertEqual(f.differences, "none")

    def test_missed_rejections_array_renders_as_bullets(self):
        f = plan_duel.extract_judge_fields(
            _sample_verdict(missed=["A dropped idempotency keys.", "B dropped retries."])
        )
        self.assertEqual(
            f.missed_rejections,
            "- A dropped idempotency keys.\n- B dropped retries.",
        )

    def test_empty_missed_array_is_the_none_the_summary_omits_on(self):
        # A judge could write "none — no rejection files yet": semantically none, but
        # not the literal the summary keys on, so it emitted a spurious section. An
        # empty array cannot be ambiguous.
        f = plan_duel.extract_judge_fields(_sample_verdict(missed=[]))
        self.assertEqual(f.missed_rejections, plan_duel._MISSED_NONE)

    def test_unpunctuated_fields_render_as_clean_prose(self):
        f = plan_duel.extract_judge_fields(
            _sample_verdict(
                differences=[
                    {"topic": "X", "plan_a": "does it", "plan_b": "does not.",
                     "stronger": "Equal", "reason": "both fine"}
                ]
            )
        )
        self.assertIn("Plan A: does it. Plan B: does not. **Stronger: Equal**", f.differences)
        self.assertNotIn("does not..", f.differences)

    def test_degraded_verdict_still_yields_usable_fields(self):
        # Missing keys must not raise; the summary is still written, just thinner.
        f = plan_duel.extract_judge_fields('{"score": 3, "preferred": "B"}')
        self.assertEqual(f.score, 3)
        self.assertEqual(f.preferred, "B")
        self.assertEqual(f.justification, "")
        self.assertEqual(f.missed_rejections, "none")

    def test_legacy_marker_extraction_is_untouched(self):
        f = plan_duel.extract_judge_fields(_sample_judge())
        self.assertEqual(f.score, 8)
        self.assertEqual(f.preferred, "A")
        self.assertIn("1. Auth:", f.differences)
        self.assertNotIn("MISSED REJECTIONS", f.differences)

    def test_legacy_file_quoting_json_keeps_its_marker_content(self):
        # A judge file whose justification quotes a plan's JSON payload must not lose
        # its differences block and justification to a false-positive verdict adoption
        # — which produced a summary.md with both sections empty, silently.
        text = (
            "SCORE: 7\n\n"
            "DIFFERENCES:\n"
            "1. Auth: Plan A: uses JWT. Plan B: uses sessions. **Stronger: A** — x\n\n"
            "MISSED REJECTIONS: none\n\n"
            "PREFERRED: A\n"
            'Plan A is clearer. Its endpoint returns {"score": 0.82, "id": 17} to the '
            "caller, which Plan B never specifies.\n"
        )
        f = plan_duel.extract_judge_fields(text)
        self.assertEqual(f.score, 7)
        self.assertEqual(f.preferred, "A")
        self.assertIn("1. Auth:", f.differences)
        self.assertIn("Plan A is clearer", f.justification)

    def test_a_verdict_missing_a_field_never_blanks_the_marker_value(self):
        # The overlay is per-field: adopting an object must never be DESTRUCTIVE, so a
        # verdict carrying only score/preferred leaves the marker prose in place.
        text = (
            '{"score": 9, "preferred": "B"}\n\n'
            "DIFFERENCES:\n"
            "1. Scope: Plan A: broad. Plan B: narrow. **Stronger: B** — focus\n\n"
            "PREFERRED: B\n"
            "The narrower plan is executable.\n"
        )
        f = plan_duel.extract_judge_fields(text)
        self.assertEqual(f.score, 9)
        self.assertIn("1. Scope:", f.differences)
        self.assertIn("narrower plan is executable", f.justification)

    def test_json_verdict_fields_still_win_over_markers_when_present(self):
        # The overlay must not become a no-op: a real verdict's fields take priority.
        text = (
            "DIFFERENCES:\n1. Stale: Plan A: old. Plan B: old. **Stronger: A** — no\n\n"
            "PREFERRED: A\nStale justification.\n\n" + _sample_verdict(preferred="B")
        )
        f = plan_duel.extract_judge_fields(text)
        self.assertEqual(f.preferred, "B")
        self.assertIn("1. Auth:", f.differences)
        self.assertNotIn("Stale:", f.differences)
        self.assertTrue(f.justification.startswith("Plan A is clearer"))

    def test_equivalent_verdicts_yield_identical_fields_in_both_contracts(self):
        # A JSON verdict and the marker text saying the SAME thing must be
        # indistinguishable to every downstream consumer: summary assembly, the winner,
        # the score trajectory. Both inputs are written here so they are genuinely
        # equivalent — which the scenario-level test cannot assert without also
        # testing fixture spelling.
        as_json = _sample_verdict(
            score=7,
            preferred="B",
            missed=["A dropped idempotency keys."],
            differences=[
                {"topic": "Auth", "plan_a": "uses JWT", "plan_b": "uses sessions",
                 "stronger": "A", "reason": "stateless"},
            ],
        )
        markers = (
            "SCORE: 7\n\n"
            "DIFFERENCES:\n"
            "1. Auth: Plan A: uses JWT. Plan B: uses sessions. **Stronger: A** — stateless\n\n"
            "MISSED REJECTIONS: - A dropped idempotency keys.\n\n"
            "PREFERRED: B\n"
            "Plan A is clearer; Plan B hand-waves rollout.\n"
        )
        self.assertEqual(
            plan_duel.extract_judge_fields(as_json),
            plan_duel.extract_judge_fields(markers),
        )


# --------------------------------------------------------------------------- #
# Structured-output schema companion (one file, two argv forms)
# --------------------------------------------------------------------------- #
class SchemaPlaceholderTests(_TempWorkdirMixin, unittest.TestCase):
    def test_shipped_schema_resolves_to_both_forms(self):
        values = plan_duel.schema_placeholder_values(_ENGINE_DIR)
        self.assertEqual(
            set(values), {"schema_path", "schema_json"},
        )
        self.assertTrue(values["schema_path"].endswith(plan_duel.JUDGE_SCHEMA_NAME))
        self.assertTrue(Path(values["schema_path"]).is_absolute())
        # The inline form is the SAME document, compacted to one argv-safe line.
        document = json.loads(values["schema_json"])
        # encoding PINNED: the schema contains em dashes, and Windows' default cp1252
        # would mangle them here and fail a comparison the engine itself gets right,
        # because it reads the file as UTF-8.
        self.assertEqual(
            document,
            json.loads(Path(values["schema_path"]).read_text(encoding="utf-8")),
        )
        self.assertNotIn("\n", values["schema_json"])

    def test_shipped_schema_declares_the_judge_contract(self):
        document = json.loads(
            plan_duel.schema_placeholder_values(_ENGINE_DIR)["schema_json"]
        )
        self.assertEqual(
            set(document["required"]),
            {"score", "differences", "missed_rejections", "preferred", "justification"},
        )
        self.assertEqual(document["properties"]["preferred"]["enum"], ["A", "B"])
        # The rubric's 0–10 range is CONSTRAINED, not merely described: an out-of-range
        # score would trip the >= 8 convergence exit on a malformed verdict. Both
        # shipped runtimes honor minimum/maximum.
        self.assertEqual(document["properties"]["score"]["minimum"], 0)
        self.assertEqual(document["properties"]["score"]["maximum"], 10)

    def test_missing_or_malformed_schema_yields_no_values(self):
        self.assertEqual(plan_duel.schema_placeholder_values(None), {})
        empty = self._tmpdir()
        self.assertEqual(plan_duel.schema_placeholder_values(empty), {})
        (empty / plan_duel.JUDGE_SCHEMA_NAME).write_text("{not json", encoding="utf-8")
        self.assertEqual(plan_duel.schema_placeholder_values(empty), {})

    def test_context_exposes_both_forms_to_argv_rendering(self):
        ctx = plan_duel.DuelContext(
            Path("/wd").resolve(), "Claude", "Codex", _ENGINE_DIR
        )
        values = ctx.values(round_n=1, prompt="p")
        spec = plan_duel.parse_adapter_config(
            {
                role: {
                    "command": ["cli", "--schema-file", "⟪schema_path⟫",
                                "--schema-inline", "⟪schema_json⟫"],
                    "stdout": "file",
                    "placeholders": ["schema_path", "schema_json"],
                }
                for role in ("agent_a", "agent_b", "judge")
            }
        )["judge"]
        argv = plan_duel.render_argv(spec, values)
        self.assertTrue(argv[2].endswith(plan_duel.JUDGE_SCHEMA_NAME))
        self.assertEqual(json.loads(argv[4])["title"], "plan-duel judge verdict")

    def test_context_without_a_skill_dir_simply_omits_the_markers(self):
        ctx = plan_duel.DuelContext(Path("/wd").resolve(), "Claude", "Codex", None)
        values = ctx.values(round_n=1, prompt="p")
        self.assertNotIn("schema_path", values)
        self.assertNotIn("schema_json", values)


class PreflightSchemaTests(_TempWorkdirMixin, unittest.TestCase):
    def _specs(self, judge_command):
        return plan_duel.parse_adapter_config(
            {
                "agent_a": {"command": ["cli", "a"], "stdout": "file"},
                "agent_b": {"command": ["cli", "b"], "stdout": "file"},
                "judge": {"command": judge_command, "stdout": "file"},
            }
        )

    def test_adapter_not_using_the_markers_needs_no_schema(self):
        plan_duel.preflight_schema(self._specs(["cli", "judge"]), {})

    def test_missing_schema_halts_naming_the_marker(self):
        for marker in ("⟪schema_path⟫", "⟪schema_json⟫"):
            with self.subTest(marker=marker):
                with self.assertRaises(plan_duel.PlanDuelError) as ctx:
                    plan_duel.preflight_schema(self._specs(["cli", marker]), {})
                self.assertIn(marker, str(ctx.exception))
                self.assertIn(plan_duel.JUDGE_SCHEMA_NAME, str(ctx.exception))

    def test_resolved_schema_passes(self):
        values = plan_duel.schema_placeholder_values(_ENGINE_DIR)
        plan_duel.preflight_schema(self._specs(["cli", "⟪schema_path⟫"]), values)

    def test_execute_halts_before_creating_a_workdir(self):
        # The point of the pre-flight: a missing schema costs nothing, rather than
        # surfacing at the judge dispatch two paid plan generations later. argv[0] is a
        # REAL executable so preflight_executables passes and the schema check is
        # demonstrably what halts.
        target = self._tmpdir() / "never-created"
        specs = plan_duel.parse_adapter_config(
            {
                "agent_a": {"command": [sys.executable, "-c", "pass"], "stdout": "file"},
                "agent_b": {"command": [sys.executable, "-c", "pass"], "stdout": "file"},
                "judge": {
                    "command": [sys.executable, "-c", "pass", "⟪schema_json⟫"],
                    "stdout": "file",
                },
            }
        )
        with self.assertRaises(plan_duel.PlanDuelError) as ctx:
            plan_duel.execute(
                argument="A problem.",
                workdir_arg=str(target),
                specs=specs,
                controller_name="Claude",
                participant_name="Codex",
                skill_dir=None,
                emit=lambda _m: None,
            )
        self.assertIn(plan_duel.JUDGE_SCHEMA_NAME, str(ctx.exception))
        self.assertNotIsInstance(ctx.exception, plan_duel.CliNotFoundError)
        self.assertFalse(target.exists())

    def test_replaying_a_finished_duel_needs_no_schema(self):
        # A resume that spawns NOTHING must stay schema-free, exactly as it already
        # stays CLI-free: requiring the companion here would block a user from
        # re-reading a completed duel's summary.md.
        wd = self._tmpdir() / "wd"
        wd.mkdir()
        (wd / "problem.md").write_text("A problem.\n", encoding="utf-8")
        (wd / "summary.md").write_text("# Plan Duel Summary\n\nAlready done.\n",
                                       encoding="utf-8")
        msgs = []
        rc = plan_duel.execute(
            argument=str(wd),
            specs=self._specs(["cli", "⟪schema_path⟫"]),  # schema needed, none available
            controller_name="Claude",
            participant_name="Codex",
            skill_dir=None,
            emit=msgs.append,
        )
        self.assertEqual(rc, 0)
        self.assertIn("Already done.", "\n".join(msgs))


# --------------------------------------------------------------------------- #
# Scoped Plan A/B -> name rewrite
# --------------------------------------------------------------------------- #
class RewriteDifferencesTests(unittest.TestCase):
    def test_scoped_rewrite_of_labels_and_stronger(self):
        diff = (
            "1. Auth: Plan A uses JWT. Plan B uses sessions. **Stronger: A** — x.\n"
            "2. Risk: Plan B mitigates. **Stronger: B** — y."
        )
        out = plan_duel.rewrite_differences(diff, "Claude", "Codex")
        self.assertIn("Claude uses JWT", out)
        self.assertIn("Codex uses sessions", out)
        self.assertIn("**Stronger: Claude**", out)
        self.assertIn("**Stronger: Codex**", out)
        self.assertNotIn("Plan A", out)
        self.assertNotIn("Plan B", out)
        self.assertNotIn("Stronger: A", out)

    def test_rewrite_is_scoped_not_global_over_summary(self):
        # The rewrite applies ONLY to the differences field. A "Plan A" mention in the
        # justification must survive untouched, proving the substitution never runs
        # globally over the whole summary.
        f = plan_duel.extract_judge_fields(_sample_judge())
        self.assertIn("Plan A", f.justification)  # justification is NOT rewritten
        rewritten = plan_duel.rewrite_differences(f.differences, "Claude", "Codex")
        self.assertNotIn("Plan A", rewritten)
        # justification is left intact by the (scoped) differences rewrite
        self.assertIn("Plan A is clearer", f.justification)

    def test_single_pass_does_not_re_scan_a_name_starting_with_a_token(self):
        # A controller name overlapping a later token ("Bo" starts with the "B" that
        # "Stronger: B" matches) must NOT be double-rewritten: the single left-to-right
        # pass consumes "Stronger: A" -> "Stronger: Bo" and never re-scans it.
        diff = "1. X: Plan A vs Plan B. **Stronger: A** — a. 2. **Stronger: B** — b."
        out = plan_duel.rewrite_differences(diff, "Bo", "Codex")
        self.assertIn("**Stronger: Bo**", out)
        self.assertNotIn("**Stronger: Codexo**", out)  # the corruption we prevent
        self.assertIn("**Stronger: Codex**", out)


# --------------------------------------------------------------------------- #
# Winner-only v2 stamping
# --------------------------------------------------------------------------- #
class StampWinnerPlanTests(unittest.TestCase):
    def test_inserts_full_status_block_when_absent(self):
        plan = "# My Plan\n\nIntro.\n\n## Goal\n\nDo it.\n"
        out = plan_duel.stamp_winner_plan(plan)
        self.assertIn("## Status", out)
        self.assertIn("| Format | v2 |", out)
        self.assertIn(
            "| Suite | plan-init / plan-phase / plan-run |", out
        )
        # The two markers are the WHOLE table: plan.md is never edited again, so a
        # status cell here could only ever be stale.
        for key in ("Phase", "State", "Blocker", "Last updated"):
            self.assertNotIn(f"| {key} |", out)
        # Inserted directly beneath the title, above the intro.
        self.assertLess(out.index("## Status"), out.index("Intro."))

    def test_augments_existing_status_table_at_top(self):
        plan = (
            "# My Plan\n\n## Status\n\n| Field | Value |\n|---|---|\n"
            "| Owner | ross |\n\n## Goal\n\nx\n"
        )
        out = plan_duel.stamp_winner_plan(plan)
        self.assertIn("| Format | v2 |", out)
        self.assertIn(
            "| Suite | plan-init / plan-phase / plan-run |", out
        )
        # Format/Suite are inserted at the TOP of the existing table, and a row the
        # agent invented that is NOT a status field is left alone beneath them.
        self.assertLess(out.index("| Format | v2 |"), out.index("| Owner | ross |"))
        # Only one Status heading (no duplicate block).
        self.assertEqual(out.count("## Status"), 1)

    def test_strips_the_stale_by_construction_status_rows(self):
        # An agent-written plan may carry v1's mutable status fields. Nothing updates
        # them afterwards, so stamping drops them rather than freezing a lie into the
        # plan; rows below the table are untouched.
        plan = (
            "# My Plan\n\n## Status\n\n| Field | Value |\n|---|---|\n"
            "| Phase | Not yet broken down |\n| State | Planning |\n"
            "| Blocker | None |\n| Last updated | 2026-07-07 |\n\n## Goal\n\nx\n"
        )
        out = plan_duel.stamp_winner_plan(plan)
        for key in ("Phase", "State", "Blocker", "Last updated"):
            self.assertNotIn(f"| {key} |", out)
        # Stripping every row must not leave a header and separator with nothing
        # under them — the two markers take their place.
        self.assertIn("|---|---|\n| Format | v2 |\n| Suite |", out)
        self.assertIn("## Goal", out)

    def test_strips_even_when_both_markers_are_already_present(self):
        # `to_insert` is empty because Format and Suite are already present, so an
        # implementation that strips only while inserting leaves the stale rows behind.
        # Mutation-verified: guarding the strip on `"Format" not in existing_keys`
        # passes every other test in this class.
        plan = (
            "# My Plan\n\n## Status\n\n| Field | Value |\n|---|---|\n"
            "| Format | v2 |\n| Suite | plan-init / plan-phase / plan-run |\n"
            "| State | Planning |\n| Owner | ross |\n\n## Goal\n\nx\n"
        )
        out = plan_duel.stamp_winner_plan(plan)
        self.assertNotIn("| State | Planning |", out)
        self.assertEqual(out.count("| Format | v2 |"), 1)
        self.assertEqual(out.count("| Suite |"), 1)
        self.assertIn("| Owner | ross |", out)  # a non-status row is left alone

    def test_corrects_a_stale_format_value(self):
        # An agent that wrote `| Format | v1 |` itself must not survive the stamp:
        # /plan-phase refuses a plan whose marker is not v2, while the summary
        # advertises the winner as v2. Keying on the row's PRESENCE left v1 in place.
        plan = (
            "# My Plan\n\n## Status\n\n| Field | Value |\n|---|---|\n"
            "| Format | v1 |\n| Suite | plan-init / plan-phase / plan-run |\n"
            "\n## Goal\n\nx\n"
        )
        out = plan_duel.stamp_winner_plan(plan)
        self.assertIn("| Format | v2 |", out)
        self.assertNotIn("| Format | v1 |", out)
        self.assertEqual(out.count("| Format |"), 1)  # corrected, not duplicated
        self.assertEqual(out.count("## Status"), 1)

    def test_corrects_a_stale_suite_value(self):
        # Same defect on the other row: a Suite naming the v1 skills is wrong for a
        # plan the summary points /plan-phase at.
        plan = (
            "# My Plan\n\n## Status\n\n| Field | Value |\n|---|---|\n"
            "| Format | v2 |\n| Suite | plan-init-v1 / plan-phase-v1 / plan-run-v1 |\n"
            "\n## Goal\n\nx\n"
        )
        out = plan_duel.stamp_winner_plan(plan)
        self.assertIn("| Suite | plan-init / plan-phase / plan-run |", out)
        self.assertNotIn("plan-init-v1", out)
        self.assertEqual(out.count("| Suite |"), 1)

    def test_does_not_edit_a_status_table_quoted_inside_a_fence(self):
        # A duel about planning produces plans that SHOW a status table in an example.
        # Rewriting the example's value edits documentation the engine does not own.
        # The example must come back byte-identical while the plan's OWN table below
        # it is corrected.
        plan = (
            "# My Plan\n\nEvery plan starts with:\n\n"
            "```markdown\n"
            "## Status\n\n| Field | Value |\n|---|---|\n| Format | v1 |\n"
            "| Suite | plan-init-v1 / plan-phase-v1 / plan-run-v1 |\n"
            "```\n\n"
            "## Status\n\n| Field | Value |\n|---|---|\n| Format | v1 |\n"
            "| Owner | ross |\n\n## Goal\n\nx\n"
        )
        out = plan_duel.stamp_winner_plan(plan)
        # The fenced example is untouched, both rows.
        self.assertIn("```markdown\n## Status\n\n| Field | Value |\n|---|---|\n"
                      "| Format | v1 |\n"
                      "| Suite | plan-init-v1 / plan-phase-v1 / plan-run-v1 |\n```", out)
        # The plan's own table — the one after the fence — is the one that got stamped.
        self.assertIn("|---|---|\n| Format | v2 |\n| Suite | plan-init / plan-phase / "
                      "plan-run |\n| Owner | ross |", out)
        self.assertEqual(out.count("| Format | v2 |"), 1)
        self.assertEqual(out.count("| Format | v1 |"), 1)  # only the example's

    def test_a_fenced_example_alone_does_not_count_as_the_plans_status_block(self):
        # With no real table anywhere, the fenced example must not be mistaken for
        # one: the plan gets a fresh block under its title and the fence is left alone.
        plan = (
            "# My Plan\n\nEvery plan starts with:\n\n"
            "```markdown\n## Status\n\n| Field | Value |\n|---|---|\n| Format | v1 |\n"
            "```\n\n## Goal\n\nx\n"
        )
        out = plan_duel.stamp_winner_plan(plan)
        self.assertIn("```markdown\n## Status\n\n| Field | Value |\n|---|---|\n"
                      "| Format | v1 |\n```", out)
        self.assertIn("| Format | v2 |", out)
        # The engine's own block lands under the title, above the fence.
        self.assertLess(out.index("| Format | v2 |"), out.index("```markdown"))

    def test_an_over_indented_apparent_opener_does_not_fence_what_follows(self):
        # CommonMark: a fence may carry at most three spaces of indentation; at four it
        # is code CONTENT. Reading it as an opener hides the plan's real table, so the
        # plan gets a SECOND `## Status` block while its existing table keeps `v1`.
        plan = (
            "# My Plan\n\nIndented sample line:\n\n"
            "    ```markdown\n\n"
            "## Status\n\n| Field | Value |\n|---|---|\n| Format | v1 |\n"
            "| Owner | ross |\n\n## Goal\n\nx\n"
        )
        out = plan_duel.stamp_winner_plan(plan)
        self.assertEqual(out.count("## Status"), 1)
        self.assertIn("|---|---|\n| Format | v2 |\n| Suite | plan-init / plan-phase / "
                      "plan-run |\n| Owner | ross |", out)
        self.assertNotIn("| Format | v1 |", out)

    def test_an_over_indented_apparent_closer_does_not_un_fence_what_follows(self):
        # The same leniency backwards: safe for an OPENER, wrong for a CLOSER. A
        # four-space-indented ``` inside a fence is content, and reading it as a closer
        # un-fences the example below it, which then gets rewritten while the plan's
        # own table keeps `v1`.
        plan = (
            "# My Plan\n\nEvery plan starts with:\n\n"
            "```markdown\n"
            "    ```\n"
            "## Status\n\n| Field | Value |\n|---|---|\n| Format | v1 |\n"
            "```\n\n"
            "## Status\n\n| Field | Value |\n|---|---|\n| Format | v1 |\n"
            "| Owner | ross |\n\n## Goal\n\nx\n"
        )
        out = plan_duel.stamp_winner_plan(plan)
        # The example inside the fence is untouched...
        self.assertIn("```markdown\n    ```\n## Status\n\n| Field | Value |\n|---|---|\n"
                      "| Format | v1 |\n```", out)
        # ...and the plan's own table below the fence is the one that got stamped.
        self.assertIn("|---|---|\n| Format | v2 |\n| Suite | plan-init / plan-phase / "
                      "plan-run |\n| Owner | ross |", out)
        self.assertEqual(out.count("## Status"), 2)  # the example's and the plan's own
        self.assertEqual(out.count("| Format | v1 |"), 1)  # only the example's

    def test_a_backtick_fence_whose_info_string_holds_a_backtick_is_not_a_fence(self):
        # CommonMark: a backtick fence's info string may not contain a backtick, so
        # this line is a paragraph. Treating it as an unterminated opener hides the
        # rest of the document from the stamp.
        plan = (
            "# My Plan\n\n"
            "```js`x``` is what the linter prints.\n\n"
            "## Status\n\n| Field | Value |\n|---|---|\n| Format | v1 |\n"
            "| Owner | ross |\n\n## Goal\n\nx\n"
        )
        out = plan_duel.stamp_winner_plan(plan)
        self.assertEqual(out.count("## Status"), 1)
        self.assertIn("|---|---|\n| Format | v2 |\n| Suite | plan-init / plan-phase / "
                      "plan-run |\n| Owner | ross |", out)
        self.assertNotIn("| Format | v1 |", out)

    def test_corrects_an_owned_row_whose_key_differs_only_in_case(self):
        # `| format | v1 |` is the same row to a reader. Matching case-sensitively
        # keeps it and adds `| Format | v2 |` beside it, leaving the file asserting
        # both formats at once.
        plan = (
            "# My Plan\n\n## Status\n\n| Field | Value |\n|---|---|\n"
            "| format | v1 |\n| SUITE | whatever |\n| Owner | ross |\n\n## Goal\n\nx\n"
        )
        out = plan_duel.stamp_winner_plan(plan)
        self.assertNotIn("| format | v1 |", out)
        self.assertNotIn("| SUITE | whatever |", out)
        self.assertEqual(out.count("| Format | v2 |"), 1)
        self.assertEqual(out.count("| Suite |"), 1)
        self.assertIn("| Owner | ross |", out)

    def test_leaves_an_already_correct_plan_unchanged(self):
        # The correction must be a correction, not a rewrite: a plan already
        # carrying both correct rows comes back byte-identical.
        plan = (
            "# My Plan\n\n## Status\n\n| Field | Value |\n|---|---|\n"
            "| Format | v2 |\n| Suite | plan-init / plan-phase / plan-run |\n"
            "| Owner | ross |\n\n## Goal\n\nx\n"
        )
        self.assertEqual(plan_duel.stamp_winner_plan(plan), plan)

    def test_does_not_duplicate_existing_rows(self):
        plan = (
            "# My Plan\n\n## Status\n\n| Field | Value |\n|---|---|\n"
            "| Format | v2 |\n| Suite | plan-init / plan-phase / plan-run |\n"
        )
        out = plan_duel.stamp_winner_plan(plan)
        self.assertEqual(out.count("| Format | v2 |"), 1)
        self.assertEqual(out.count("| Suite |"), 1)

    def test_status_heading_without_table_gets_a_table_not_a_duplicate_heading(self):
        # A "## Status" heading with no table beneath it must gain a table in place
        # (with the load-bearing rows), NOT a second "## Status" block under the title.
        plan = "# My Plan\n\n## Status\n\nSome prose, no table.\n\n## Goal\n\nx\n"
        out = plan_duel.stamp_winner_plan(plan)
        self.assertEqual(out.count("## Status"), 1)  # no duplicate heading
        self.assertIn("| Format | v2 |", out)
        self.assertIn(
            "| Suite | plan-init / plan-phase / plan-run |", out
        )
        # The table lands under the existing heading, above the next section.
        self.assertLess(out.index("| Format | v2 |"), out.index("## Goal"))

    def test_stamping_is_idempotent(self):
        plan = "# My Plan\n\nIntro.\n\n## Goal\n\nDo it.\n"
        once = plan_duel.stamp_winner_plan(plan)
        twice = plan_duel.stamp_winner_plan(once)
        self.assertEqual(once, twice)  # re-stamping the winner adds nothing

    def test_stripping_is_idempotent(self):
        # The strip path must also converge: a second pass over an already-stripped
        # table is a no-op, so a resumed run cannot mangle the winner.
        plan = (
            "# My Plan\n\n## Status\n\n| Field | Value |\n|---|---|\n"
            "| State | Planning |\n| Owner | ross |\n\n## Goal\n\nx\n"
        )
        once = plan_duel.stamp_winner_plan(plan)
        twice = plan_duel.stamp_winner_plan(once)
        self.assertEqual(once, twice)


class FrozenPlaceholderTests(unittest.TestCase):
    """The frozen-snapshot placeholders that let round.md preserve v1 simultaneity."""

    def test_inventory_lists_frozen_placeholders(self):
        # round.md points agent reads at ⟪frozen_a⟫/⟪frozen_b⟫; the
        # documented inventory MUST list them or the companion placeholder-drift
        # check would flag a false positive.
        self.assertIn("frozen_a", plan_duel.PLACEHOLDERS)
        self.assertIn("frozen_b", plan_duel.PLACEHOLDERS)

    def test_context_supplies_frozen_paths_for_the_prior_round(self):
        ctx = plan_duel.DuelContext(
            workdir=Path("/wd").resolve(),
            controller_name="Claude",
            participant_name="Codex",
        )
        vals = ctx.values(round_n=3, prompt="")
        # Round N reads the immutable round-(N-1) snapshots.
        self.assertEqual(vals["frozen_a"], str(Path("/wd").resolve() / "plan-a-round-2.md"))
        self.assertEqual(vals["frozen_b"], str(Path("/wd").resolve() / "plan-b-round-2.md"))
        # Every marker the engine advertises for a round is resolvable (fail-loud check).
        plan_duel.render_template("⟪frozen_a⟫ ⟪frozen_b⟫ ⟪workdir⟫ ⟪round⟫", vals)


_SKILL_DIR = Path(__file__).resolve().parent.parent / "skills" / "plan-duel"


class SelectRoleSectionTests(unittest.TestCase):
    """The engine hands each role ONLY its own section (v1 sent tailored prompts)."""

    _TEMPLATE = (
        "Preamble line.\n\n"
        "## Agent A\n\nDo A. Write plan-a.md.\n\n"
        "## Agent B\n\nDo B. Write plan-b.md.\n\n"
        "## Judge\n\nScore it. SCORE: [integer]\n\n"
        "## Writing files\n\nWhole-file writes only.\n"
    )

    def test_extracts_only_the_named_role_plus_shared_sections(self):
        out = plan_duel.select_role_section(self._TEMPLATE, "Agent A")
        self.assertIn("Preamble line.", out)  # shared preamble kept
        self.assertIn("Do A. Write plan-a.md.", out)  # own section kept
        self.assertIn("Whole-file writes only.", out)  # shared trailer kept
        self.assertNotIn("Do B", out)  # other agent dropped
        self.assertNotIn("SCORE:", out)  # judge rubric dropped

    def test_judge_gets_rubric_not_agent_write_instructions(self):
        out = plan_duel.select_role_section(self._TEMPLATE, "Judge")
        self.assertIn("SCORE: [integer]", out)
        self.assertNotIn("Write plan-a.md", out)
        self.assertNotIn("Write plan-b.md", out)

    def test_missing_role_section_raises(self):
        with self.assertRaises(plan_duel.PlanDuelError):
            plan_duel.select_role_section("## Agent A\n\nx\n", "Judge")


class RoleAwarePromptTests(unittest.TestCase):
    """ctx.prompt renders each role's OWN section from the real companion templates."""

    def _ctx(self):
        return plan_duel.DuelContext(
            workdir=Path("/wd").resolve(),
            controller_name="Claude",
            participant_name="Codex",
            skill_dir=_SKILL_DIR,
        )

    @staticmethod
    def _fwd(text):
        # Paths render with the OS-native separator (``Path('/wd').resolve()`` → ``D:\\wd``
        # on Windows). Normalize to ``/`` so the assertions below are separator-agnostic
        # and pass on windows-latest.
        return text.replace("\\", "/")

    def test_round0_agents_get_mirrored_write_targets(self):
        ctx = self._ctx()
        a = self._fwd(ctx.prompt("agent_a", 0))
        b = self._fwd(ctx.prompt("agent_b", 0))
        self.assertIn("/wd/plan-a.md", a)
        self.assertNotIn("/wd/plan-b.md", a)  # A must not be told to write B's plan
        self.assertIn("/wd/plan-b.md", b)
        self.assertNotIn("/wd/plan-a.md", b)

    def test_critique_agents_read_frozen_write_live_and_dont_see_judge_rubric(self):
        ctx = self._ctx()
        a = self._fwd(ctx.prompt("agent_a", 2))
        # Reads the immutable prior-round snapshots (round 1), writes its live plan.
        self.assertIn("/wd/plan-a-round-1.md", a)  # ⟪frozen_a⟫
        self.assertIn("/wd/plan-b-round-1.md", a)  # ⟪frozen_b⟫ (reference)
        self.assertIn("/wd/plan-a.md", a)  # writes its live plan
        # No judge rubric leaked to an agent. Anchored on text that EXISTS in the judge
        # section, so the assertion cannot pass vacuously if that section is reworded.
        self.assertNotIn("neutral technical adjudicator", a)
        self.assertNotIn("Write the complete revised Plan B", a)  # not B's task
        b = self._fwd(ctx.prompt("agent_b", 2))
        self.assertIn("/wd/plan-b.md", b)
        self.assertNotIn("neutral technical adjudicator", b)
        self.assertNotIn("Write the complete revised Plan A", b)  # not A's task

    def test_judge_prompt_is_the_rubric_only(self):
        ctx = self._ctx()
        j = ctx.prompt("judge", 2)
        # The judge answers with the schema's JSON verdict, so the rubric names those
        # fields rather than the pre-schema SCORE:/PREFERRED: line markers.
        self.assertIn("`score`", j)
        self.assertIn("`preferred`", j)
        self.assertIn("`justification`", j)
        # The judge scores the post-critique LIVE plans, not the critique instructions.
        self.assertNotIn("Write the complete revised Plan A", j)
        self.assertNotIn("Write the complete revised Plan B", j)
        # The judge prompt must not carry the frozen prior-round snapshot references
        # (those belong to the critique agents) — it scores the live revisions.
        self.assertNotIn("plan-a-round-1.md", j)
        self.assertNotIn("plan-b-round-1.md", j)

    def test_no_residual_markers_in_any_rendered_role_prompt(self):
        ctx = self._ctx()
        for role, rnd in (("agent_a", 0), ("agent_b", 0),
                          ("agent_a", 2), ("agent_b", 2), ("judge", 2)):
            rendered = ctx.prompt(role, rnd)
            self.assertEqual(
                plan_duel.find_placeholders(rendered), set(),
                f"unresolved ⟪⟫ marker in {role} round {rnd}: {rendered!r}",
            )
            self.assertFalse(rendered.startswith("[plan-duel] role="),
                             f"{role} round {rnd} hit the generic fallback")

    def test_round0_prompt_is_self_contained_on_embedded_methodology(self):
        # The round-0 prompt must carry the condensed v2 methodology inline —
        # no instruction to read an external plan-init skill file, no external
        # skill-path placeholder, and zero unresolved ⟪⟫ markers.
        ctx = self._ctx()
        for role in ("agent_a", "agent_b"):
            rendered = ctx.prompt(role, 0)
            self.assertEqual(
                plan_duel.find_placeholders(rendered), set(),
                f"unresolved ⟪⟫ marker in {role} round 0",
            )
            self.assertIn("## Plan methodology", rendered)
            self.assertIn("Success Criteria", rendered)
            self.assertIn(
                "note each assumption", rendered,
                f"{role} round 0 lost the autonomous-assumption instruction",
            )
            self.assertNotIn("Read the plan-init skill", rendered)
            self.assertNotIn("skill from", rendered)  # no external skill-path fetch


# --------------------------------------------------------------------------- #
# Summary assembly (pure)
# --------------------------------------------------------------------------- #
class AssembleSummaryTests(unittest.TestCase):
    def _assemble(self, *, rounds_run=3, missed="none"):
        return plan_duel.assemble_summary(
            workdir_display="/wd",
            rounds_run=rounds_run,
            stopped_due_to="Convergence",
            controller_name="Claude",
            participant_name="Codex",
            controller_slug="claude",
            participant_slug="codex",
            winner_name="Claude",
            winner_file="plan-claude.md",
            trajectory=[(0, None, 10, 12), (1, 6, 11, 13), (2, 7, 12, 14), (3, 8, 13, 15)],
            justification="Claude wins on clarity.",
            differences_rewritten="1. Claude broader. **Stronger: Claude** — x.",
            missed_rejections=missed,
        )

    def test_sections_in_order_and_round0_dash(self):
        out = self._assemble()
        for marker in (
            "# Plan Duel Summary",
            "**Stopped due to:** Convergence",
            "## Score trajectory",
            "| Round | Score | Claude words | Codex words |",
            "| 0 | — | 10 | 12 |",
            "| 3 | 8 | 13 | 15 |",
            "## Why Claude won",
            "## Remaining differences",
            "## All files",
        ):
            self.assertIn(marker, out)
        # ordering
        self.assertLess(out.index("## Score trajectory"), out.index("## Why Claude won"))
        self.assertLess(
            out.index("## Why Claude won"), out.index("## Remaining differences")
        )
        self.assertLess(
            out.index("## Remaining differences"), out.index("## All files")
        )

    def test_missed_section_omitted_when_none(self):
        self.assertNotIn("## Missed rejections", self._assemble(missed="none"))

    def test_missed_section_included_when_present(self):
        out = self._assemble(missed="Dropped idempotency keys.")
        self.assertIn("## Missed rejections", out)
        self.assertIn("Dropped idempotency keys.", out)

    def test_five_round_note_gating(self):
        self.assertNotIn("mutual critique", self._assemble(rounds_run=4))
        five = self._assemble(rounds_run=5)
        self.assertIn("after 5 rounds of mutual critique", five)

    def test_winner_line_routes_to_current_plan_phase_only(self):
        out = self._assemble()
        self.assertIn("feed it to `/plan-phase`", out)
        # Never the superseded suite: a duel plan is stamped `Format: v2`, which
        # `/plan-phase-v1` refuses outright, so routing there would dead-end.
        self.assertNotIn("/plan-phase-v1", out)


# --------------------------------------------------------------------------- #
# CLI surface
# --------------------------------------------------------------------------- #
class CliSurfaceTests(unittest.TestCase):
    def test_help_does_not_list_removed_skill_path_flag(self):
        self.assertNotIn("--plan-init-skill-path", plan_duel.build_parser().format_help())

    def test_removed_skill_path_flag_is_rejected(self):
        parser = plan_duel.build_parser()
        with open(os.devnull, "w", encoding="utf-8") as devnull:
            real_stderr = sys.stderr
            sys.stderr = devnull
            try:
                with self.assertRaises(SystemExit) as ctx:
                    parser.parse_args(["problem", "--plan-init-skill-path", "/x"])
            finally:
                sys.stderr = real_stderr
        self.assertEqual(ctx.exception.code, 2)


# --------------------------------------------------------------------------- #
# End-to-end run loop over the golden scenario matrix (the primary parity gate)
# --------------------------------------------------------------------------- #
class _ScenarioDriverMixin(_TempWorkdirMixin):
    """Drives the engine end-to-end over a scenario fixture via the real subprocess
    path (argv-list -> scenario_stub.py), spawning no branded CLI."""

    def _specs(self, scenario_dir):
        def command(role):
            return [
                sys.executable,
                str(_SCENARIO_STUB),
                "--scenario-dir",
                str(scenario_dir),
                "--role",
                role,
                "--round",
                "⟪round⟫",
                "--workdir",
                "⟪workdir⟫",
            ]

        return plan_duel.parse_adapter_config(
            {
                role: {
                    "command": command(role),
                    "stdout": "file",
                    "placeholders": ["round", "workdir"],
                }
                for role in ("agent_a", "agent_b", "judge")
            }
        )

    def _run_new(self, name, problem):
        scenario = _SCENARIOS / name
        wd = self._tmpdir() / "wd"
        msgs = []
        rc = plan_duel.execute(
            argument=problem,
            workdir_arg=str(wd),
            specs=self._specs(scenario),
            controller_name="Claude",
            participant_name="Codex",
            emit=msgs.append,
        )
        return rc, wd.resolve(), msgs

    def _run_resume(self, name):
        scenario = _SCENARIOS / name
        wd = self._tmpdir() / "wd"
        shutil.copytree(scenario / "workdir", wd)
        wd = wd.resolve()
        msgs = []
        rc = plan_duel.execute(
            argument=str(wd),
            specs=self._specs(scenario),
            controller_name="Claude",
            participant_name="Codex",
            emit=msgs.append,
        )
        return rc, wd, msgs

    def _summary(self, wd):
        return (wd / "summary.md").read_text(encoding="utf-8")

    def _names(self, wd):
        return sorted(p.name for p in wd.iterdir() if p.is_file())


class ScenarioConvergenceTests(_ScenarioDriverMixin, unittest.TestCase):
    def test_convergence(self):
        rc, wd, msgs = self._run_new("convergence", "Design the notification service.")
        self.assertEqual(rc, 0)
        self.assertIn("### Round 1 of up to 10", msgs)
        self.assertIn("Convergence reached at round 3 (score: 8/10).", msgs)
        summary = self._summary(wd)
        self.assertIn("**Stopped due to:** Convergence", summary)
        self.assertIn("**Winner:** Claude", summary)
        self.assertIn("/plan-claude.md", summary)
        # score trajectory rows 0..3 (round 0 dash)
        self.assertIn("| 0 | — |", summary)
        self.assertIn("| 3 | 8 |", summary)
        self.assertNotIn("## Missed rejections", summary)
        # scoped rewrite applied in the differences block
        self.assertIn("**Stronger: Claude**", summary)
        self.assertNotIn("Plan A", summary)
        # exact snapshot set present
        names = self._names(wd)
        for n in range(0, 4):
            self.assertIn(f"plan-a-round-{n}.md", names)
            self.assertIn(f"plan-b-round-{n}.md", names)
        for n in range(1, 4):
            self.assertIn(f"judge-round-{n}.md", names)
        self.assertIn("plan-claude.md", names)
        self.assertIn("plan-codex.md", names)
        # winner-only stamping
        self.assertIn("| Format | v2 |", (wd / "plan-claude.md").read_text(encoding="utf-8"))
        self.assertNotIn("| Format | v2 |", (wd / "plan-codex.md").read_text(encoding="utf-8"))

    def test_progress_file_emitted_at_agent_and_judge_points_append_only(self):
        # Decision-5 progress file: emitted per round at the agent and judge spawn
        # points, append-only, read by nothing on the correctness path — so the outcome
        # is identical to a run whose progress file no one watches. Not an artifact.
        rc, wd, _ = self._run_new("convergence", "Design the notification service.")
        self.assertEqual(rc, 0)
        # Round 0 progress: both agent spawn points recorded (append-only => >= 2 lines).
        p0 = (wd / "participant-progress-0.md").read_text(encoding="utf-8").splitlines()
        self.assertGreaterEqual(len(p0), 2)
        self.assertTrue(any("Plan A" in ln for ln in p0))
        self.assertTrue(any("Plan B" in ln for ln in p0))
        # A critique round records both agents AND the judge spawn point.
        p1 = (wd / "participant-progress-1.md").read_text(encoding="utf-8").splitlines()
        self.assertTrue(any("critiquing Plan A" in ln for ln in p1))
        self.assertTrue(any("critiquing Plan B" in ln for ln in p1))
        self.assertTrue(any("judging" in ln for ln in p1))
        # The progress file is NOT parsed as a plan/judge artifact and carries no score.
        self.assertIsNone(plan_duel.parse_score("\n".join(p1)))


class ScenarioJsonVerdictTests(_ScenarioDriverMixin, unittest.TestCase):
    """The schema-enforced JSON verdict drives the run loop identically to the markers.

    The `convergence-json` fixture is the `convergence` fixture with ONLY the judge files
    swapped to bare JSON objects. Same summary from both is the compatibility proof:
    whichever contract a workdir was written under, a resume over it is safe.
    """

    def test_json_verdicts_produce_the_same_outcome_as_the_markers(self):
        rc, wd, msgs = self._run_new(
            "convergence-json", "Design the notification service."
        )
        self.assertEqual(rc, 0)
        self.assertIn("Convergence reached at round 3 (score: 8/10).", msgs)
        summary = self._summary(wd)
        self.assertIn("**Stopped due to:** Convergence", summary)
        self.assertIn("**Winner:** Claude", summary)
        self.assertIn("| 0 | — |", summary)
        self.assertIn("| 3 | 8 |", summary)
        # The verdict's differences array rendered, then name-rewritten in scope.
        self.assertIn("**Stronger: Claude**", summary)
        self.assertIn("**Stronger: Codex**", summary)
        self.assertNotIn("Plan A", summary)
        # An empty missed_rejections array omits the section, exactly as `none` did.
        self.assertNotIn("## Missed rejections", summary)
        # No warning fired: the score and winner parsed cleanly from JSON.
        self.assertFalse([m for m in msgs if "could not parse score" in m])
        self.assertFalse([m for m in msgs if "no parseable PREFERRED" in m])

    def test_summary_matches_the_marker_scenario_outside_the_differences_prose(self):
        # Everything the ENGINE computes — header, winner line, score trajectory, the
        # missed-rejections decision, the all-files block — must be byte-identical across
        # the two contracts. The differences block is excluded because its wording is the
        # JUDGE's prose; asserting on it would test fixture punctuation, not the engine.
        # Its content is asserted below, and field equality in JudgeFieldsFromVerdictTests.
        _, wd_markers, _ = self._run_new("convergence", "Design the notification service.")
        _, wd_json, _ = self._run_new(
            "convergence-json", "Design the notification service."
        )
        heading = "## Remaining differences"

        def split(wd):
            text = self._summary(wd).replace(str(wd), "<WD>")
            head, _, rest = text.partition(heading)
            block, _, tail = rest.partition("\n## ")
            return head, block, tail

        head_m, block_m, tail_m = split(wd_markers)
        head_j, block_j, tail_j = split(wd_json)
        self.assertEqual(head_m, head_j)
        self.assertEqual(tail_m, tail_j)
        # Same differences, same verdicts, same name rewrite — only spacing differs.
        for token in ("Scope", "Testing", "broader coverage", "faster feedback",
                      "**Stronger: Claude**", "**Stronger: Codex**"):
            self.assertIn(token, block_m)
            self.assertIn(token, block_j)

    def test_judge_files_on_disk_really_are_json(self):
        # Guards the fixture itself: if these ever regressed to markers the parity test
        # above would still pass while proving nothing about the JSON path.
        _, wd, _ = self._run_new("convergence-json", "Design the notification service.")
        for round_n in (1, 2, 3):
            raw = (wd / f"judge-round-{round_n}.md").read_text(encoding="utf-8")
            self.assertIsNotNone(plan_duel.parse_judge_json(raw))
            self.assertNotIn("SCORE:", raw)


class ScenarioStagnationTests(_ScenarioDriverMixin, unittest.TestCase):
    def test_stagnation(self):
        rc, wd, msgs = self._run_new("stagnation", "Refactor the billing pipeline.")
        self.assertEqual(rc, 0)
        self.assertIn(
            "Stagnation detected — best score in last 3 rounds (6/10) has not "
            "exceeded prior peak (7/10). Stopping early.",
            msgs,
        )
        summary = self._summary(wd)
        self.assertIn("**Stopped due to:** Stagnation", summary)
        self.assertIn("**Winner:** Codex", summary)  # PREFERRED B
        self.assertIn("/plan-codex.md", summary)
        # missed rejections non-none -> section present
        self.assertIn("## Missed rejections", summary)
        self.assertIn("idempotency key", summary)
        # winner-only stamping (participant wins)
        self.assertIn("| Format | v2 |", (wd / "plan-codex.md").read_text(encoding="utf-8"))
        self.assertNotIn("| Format | v2 |", (wd / "plan-claude.md").read_text(encoding="utf-8"))
        # 4 rounds
        self.assertIn("| 4 | 6 |", summary)


class ScenarioMaxRoundsTests(_ScenarioDriverMixin, unittest.TestCase):
    def test_max_rounds(self):
        rc, wd, msgs = self._run_new("max-rounds", "Plan the data migration.")
        self.assertEqual(rc, 0)
        self.assertIn("Maximum rounds reached (score: 7/10).", msgs)
        summary = self._summary(wd)
        self.assertIn("**Stopped due to:** Maximum rounds", summary)
        self.assertIn("**Rounds run:** 10", summary)
        # >=5 note present
        self.assertIn("after 10 rounds of mutual critique", summary)
        names = self._names(wd)
        for n in range(1, 11):
            self.assertIn(f"judge-round-{n}.md", names)
        for n in range(0, 11):
            self.assertIn(f"plan-a-round-{n}.md", names)


class WriteSummaryMissingFinalJudgeTests(_TempWorkdirMixin, unittest.TestCase):
    def test_missing_final_judge_degrades_not_crashes(self):
        # Resuming a round-10 duel interrupted before its judge (snapshots are written
        # first) reaches write_summary(rounds_run=10) with judge-round-10.md ABSENT.
        # Like every other judge read this must degrade — warn, score 0, still emit
        # summary.md — not crash with an uncaught FileNotFoundError.
        wd = (self._tmpdir() / "wd")
        wd.mkdir()
        body = "This is a plan body sentence. " * 10 + "\n"
        (wd / "problem.md").write_text("Problem statement.\n", encoding="utf-8")
        for side in ("a", "b"):
            (wd / f"plan-{side}.md").write_text(body, encoding="utf-8")
            for n in range(0, 11):  # rounds 0..10 snapshotted (trajectory needs them)
                (wd / f"plan-{side}-round-{n}.md").write_text(body, encoding="utf-8")
        # judge-round-10.md deliberately absent (the interrupted-final-judge edge).
        msgs = []
        path = plan_duel.write_summary(
            workdir=wd,
            rounds_run=10,
            stopped_due_to="Maximum rounds",
            controller_name="Claude",
            participant_name="Codex",
            emit=msgs.append,
        )
        self.assertTrue(path.is_file())
        self.assertIn(
            "Warning: could not parse score at round 10 — treating as 0", msgs
        )
        # The missing judge also means no parseable PREFERRED line: the engine must warn
        # (not silently) before defaulting the winner to A.
        self.assertIn(
            "Warning: no parseable PREFERRED line at round 10 — "
            "defaulting the winner to A (Claude)",
            msgs,
        )
        summary = (wd / "summary.md").read_text(encoding="utf-8")
        self.assertIn("**Stopped due to:** Maximum rounds", summary)
        # preferred is None -> defaults to A -> the controller wins, and is stamped.
        self.assertIn("**Winner:** Claude", summary)
        self.assertIn(
            "| Format | v2 |", (wd / "plan-claude.md").read_text(encoding="utf-8")
        )


class WriteSummaryMissingSnapshotTests(_TempWorkdirMixin, unittest.TestCase):
    """The other two files write_summary reads without guarding, and what they cost.

    A missing round SNAPSHOT or LIVE PLAN raised a bare `FileNotFoundError` from the last
    step of a duel already paid for: no summary, a raw traceback, and every round of
    model output on disk with nothing pointing at it. A snapshot gap is what a workdir
    looks like after a partial cleanup or an interrupted resume.
    """

    JUDGE = "SCORE: 8\n\nPREFERRED: A\n"

    def _workdir(self, *, drop_snapshot=None, drop_live=None):
        wd = self._tmpdir() / "wd"
        wd.mkdir()
        body = "This is a plan body sentence. " * 10 + "\n"
        (wd / "problem.md").write_text("Problem statement.\n", encoding="utf-8")
        for n in (0, 1, 2):
            for side in ("a", "b"):
                if drop_snapshot == (side, n):
                    continue
                (wd / plan_duel.plan_snapshot_name(side, n)).write_text(
                    body, encoding="utf-8")
        for n in (1, 2):
            (wd / f"judge-round-{n}.md").write_text(self.JUDGE, encoding="utf-8")
        for side in ("a", "b"):
            if drop_live == side:
                continue
            (wd / f"plan-{side}.md").write_text(body, encoding="utf-8")
        return wd

    def _write(self, wd):
        msgs = []
        path = plan_duel.write_summary(
            workdir=wd, rounds_run=2, stopped_due_to="Convergence",
            controller_name="Claude", participant_name="Codex", emit=msgs.append,
        )
        return path, msgs

    def test_a_missing_snapshot_is_a_dash_not_a_crash(self):
        wd = self._workdir(drop_snapshot=("a", 1))
        path, _ = self._write(wd)
        self.assertTrue(path.is_file(), "the duel was paid for and produced no summary")
        summary = path.read_text(encoding="utf-8")
        # Round 1's Claude cell is the one that could not be counted; Codex's was.
        self.assertIn("| 1 | 8 | — | 60 |", summary)
        # Every other row still carries real numbers.
        self.assertIn("| 2 | 8 | 60 | 60 |", summary)

    def test_a_missing_live_plan_is_a_warned_skip(self):
        wd = self._workdir(drop_live="b")
        path, _ = self._write(wd)
        self.assertTrue(path.is_file())
        self.assertFalse(
            (wd / "plan-codex.md").exists(),
            "a final plan was invented for a live plan that was not there")
        self.assertTrue((wd / "plan-claude.md").is_file(),
                        "the surviving side's plan was dropped too")

    def test_the_skip_is_said_out_loud(self):
        wd = self._workdir(drop_live="b")
        _, msgs = self._write(wd)
        joined = "\n".join(msgs)
        self.assertIn("plan-b.md", joined)
        self.assertIn("plan-codex.md", joined)

    def test_a_missing_winner_plan_still_names_the_winner(self):
        """The winner's own live plan is gone: the summary is still the product."""
        wd = self._workdir(drop_live="a")
        path, _ = self._write(wd)
        summary = path.read_text(encoding="utf-8")
        self.assertIn("**Winner:** Claude", summary)

    def test_a_failed_copy_never_stamps_whatever_was_already_there(self):
        """The stamp is only ever applied to a file THIS run wrote.

        A missing `plan-a.md` used to raise. Now the copy is skipped and execution
        continues to the stamp, which reads `plan-claude.md` — and a previous run may
        have left one there. It would be stamped `| Format | v2 |` and named this duel's
        winner, publishing an older plan as a result it had no part in.
        """
        wd = self._workdir(drop_live="a")
        stale = wd / "plan-claude.md"
        stale.write_text("# A PLAN FROM AN EARLIER RUN\n", encoding="utf-8")
        path, msgs = self._write(wd)
        self.assertEqual(stale.read_text(encoding="utf-8"),
                         "# A PLAN FROM AN EARLIER RUN\n",
                         "a stale plan was stamped as this duel's winner")
        self.assertTrue(path.is_file(), "the summary is still the product")
        joined = "\n".join(msgs)
        self.assertIn("plan-claude.md", joined)
        self.assertIn("not written by this run", joined)

    def test_the_stamp_still_lands_when_the_copy_succeeded(self):
        """Anti-vacuity: refusing to stamp at all would satisfy the test above."""
        wd = self._workdir()
        self._write(wd)
        self.assertIn("| Format | v2 |",
                      (wd / "plan-claude.md").read_text(encoding="utf-8"))
        self.assertNotIn("| Format | v2 |",
                         (wd / "plan-codex.md").read_text(encoding="utf-8"))

    def test_the_written_set_is_matched_by_path_not_by_basename(self):
        """A plan that WAS written must be stamped however its name is spelled.

        `written_finals` held `destination.name` while `resolve_winner` yields
        `plan-{slug}.md` — equal only while the slug is a bare component. A slug with a
        separator copies into a subdirectory and never matches, leaving the winner
        unstamped while the summary reports a v2 plan that is not one. A set keyed on a
        fragment of a path is wrong regardless of `require_safe_slug`.
        """
        wd = self._workdir()
        winner = wd / "plan-claude.md"
        recorded = []
        real_copy = plan_duel.copy_bytes

        def recording_copy(src, dst):
            recorded.append(Path(dst))
            return real_copy(src, dst)

        self.addCleanup(setattr, plan_duel, "copy_bytes", real_copy)
        plan_duel.copy_bytes = recording_copy
        self._write(wd)

        self.assertIn(winner, recorded, "the premise: this plan really was written")
        self.assertIn("| Format | v2 |", winner.read_text(encoding="utf-8"))
        # A bare basename is NOT what identifies the winner.
        self.assertTrue(all(isinstance(p, Path) for p in recorded))

    def test_an_intact_workdir_is_unchanged(self):
        """Anti-vacuity: dashing every cell and skipping every copy would pass above."""
        wd = self._workdir()
        path, msgs = self._write(wd)
        summary = path.read_text(encoding="utf-8")
        # Round 0's SCORE cell is a dash by design; no WORD-COUNT cell may be one.
        self.assertIn("| 0 | — | 60 | 60 |", summary)
        self.assertIn("| 1 | 8 | 60 | 60 |", summary)
        self.assertIn("| 2 | 8 | 60 | 60 |", summary)
        self.assertTrue((wd / "plan-claude.md").is_file())
        self.assertTrue((wd / "plan-codex.md").is_file())
        self.assertEqual([m for m in msgs if "could not" in m], [])


class WinnerWriteBackTests(_TempWorkdirMixin, unittest.TestCase):
    def test_stamping_preserves_bytes_it_did_not_author(self):
        # The winner is READ, stamped and WRITTEN BACK. U+FFFD replacement is right for
        # parsing and wrong here: the copy a reader consumes would come back with the
        # CLI's cp1252 apostrophe replaced, though the stamp never needed that byte.
        wd = self._tmpdir() / "wd"
        wd.mkdir()
        body = b"This plan\x92s body sentence. "  # cp1252 apostrophe, not valid UTF-8
        plan_bytes = b"# My Plan\n\n" + body * 10 + b"\n"
        (wd / "problem.md").write_text("Problem statement.\n", encoding="utf-8")
        for side in ("a", "b"):
            (wd / f"plan-{side}.md").write_bytes(plan_bytes)
            for n in (0, 1):
                (wd / f"plan-{side}-round-{n}.md").write_bytes(plan_bytes)
        (wd / "judge-round-1.md").write_text(
            "SCORE: 9\n\nPREFERRED: A\n", encoding="utf-8"
        )
        plan_duel.write_summary(
            workdir=wd,
            rounds_run=1,
            stopped_due_to="Convergence",
            controller_name="Claude",
            participant_name="Codex",
            emit=lambda _msg: None,
        )
        winner = (wd / "plan-claude.md").read_bytes()
        self.assertIn(b"\x92", winner)  # the CLI's byte survived the round trip
        self.assertNotIn("\ufffd".encode("utf-8"), winner)
        self.assertIn(b"| Format | v2 |", winner)  # and it really was stamped


class ScenarioLowScoreTests(_ScenarioDriverMixin, unittest.TestCase):
    def test_unparseable_score_warns_and_treats_as_zero(self):
        rc, wd, msgs = self._run_new("low-score", "Introduce feature flags.")
        self.assertEqual(rc, 0)
        self.assertIn(
            "Warning: could not parse score at round 1 — treating as 0", msgs
        )
        summary = self._summary(wd)
        # round 1 shown as 0 in the trajectory, not a dash
        self.assertIn("| 1 | 0 |", summary)
        self.assertIn("**Stopped due to:** Convergence", summary)


class ScenarioInitInterruptTests(_ScenarioDriverMixin, unittest.TestCase):
    def test_init_interrupt_full_reset_then_completes(self):
        rc, wd, msgs = self._run_resume("init-interrupt")
        self.assertEqual(rc, 0)
        self.assertIn("Init incomplete — restarting from round 0.", msgs)
        # full-reset deletion log (direct children, sorted by name)
        for deleted in (
            "Deleted controller-prompt-0.txt",
            "Deleted participant-progress-0.md",
            "Deleted participant-prompt-0.txt",
            "Deleted plan-a.md",
        ):
            self.assertIn(deleted, msgs)
        # preserved files survive
        self.assertTrue((wd / "keep-me.txt").exists())
        self.assertTrue((wd / "problem.md").exists())
        summary = self._summary(wd)
        self.assertIn("**Stopped due to:** Convergence", summary)
        self.assertIn("**Winner:** Claude", summary)


class ScenarioInitReusePlanATests(_ScenarioDriverMixin, unittest.TestCase):
    def test_resume_after_round0_agent_b_failure_does_not_regenerate_plan_a(self):
        # Same workdir as the full-reset case, plus the validated Plan A snapshot a
        # round 0 that died at Agent B now leaves behind.
        scenario = _SCENARIOS / "init-interrupt"
        wd = self._tmpdir() / "wd"
        shutil.copytree(scenario / "workdir", wd)
        wd = wd.resolve()
        # A real round 0 that died at Agent B leaves the live plan and its snapshot
        # byte-identical — that match is what proves the snapshot is complete.
        marker = "REUSED-PLAN-A " * 40
        (wd / "plan-a.md").write_text(marker, encoding="utf-8")
        (wd / "plan-a-round-0.md").write_text(marker, encoding="utf-8")

        msgs = []
        rc = plan_duel.execute(
            argument=str(wd),
            specs=self._specs(scenario),
            controller_name="Claude",
            participant_name="Codex",
            emit=msgs.append,
        )

        self.assertEqual(rc, 0)
        self.assertIn(
            "Init incomplete — reusing the validated round-0 Plan A; "
            "re-running Plan B only.",
            msgs,
        )
        # Agent A never ran at round 0, so the snapshot is untouched...
        self.assertEqual(
            (wd / "plan-a-round-0.md").read_text(encoding="utf-8"), marker
        )
        # ...and it was never deleted on the way in.
        self.assertNotIn("Deleted plan-a-round-0.md", msgs)
        # The rest of the reset still happened, and the duel still completed.
        self.assertIn("Deleted plan-a.md", msgs)
        self.assertTrue((wd / "keep-me.txt").exists())
        self.assertIn("**Stopped due to:** Convergence", self._summary(wd))


class ScenarioMidRoundInterruptTests(_ScenarioDriverMixin, unittest.TestCase):
    def test_mid_round_interrupt_resumes_and_cleans_stragglers(self):
        rc, wd, msgs = self._run_resume("mid-round-interrupt")
        self.assertEqual(rc, 0)
        self.assertIn("Deleted judge-round-3.md", msgs)
        self.assertIn("Deleted plan-a-round-3.md", msgs)
        self.assertTrue(any(m.startswith("Resuming in ") and m.endswith("from round 3.")
                            for m in msgs))
        self.assertIn("### Round 3 of up to 10", msgs)
        summary = self._summary(wd)
        self.assertIn("**Rounds run:** 3", summary)
        self.assertIn("**Stopped due to:** Convergence", summary)
        # the fresh round-3 snapshot replaced the straggler content
        self.assertNotIn("STRAGGLER", (wd / "plan-a-round-3.md").read_text(encoding="utf-8"))


class ScenarioWinnerStampingTests(_ScenarioDriverMixin, unittest.TestCase):
    def test_winner_only_stamping_augments_winner_leaves_loser(self):
        rc, wd, _ = self._run_new("winner-stamping", "Add SSO to the admin console.")
        self.assertEqual(rc, 0)
        winner = (wd / "plan-codex.md").read_text(encoding="utf-8")  # PREFERRED B
        loser = (wd / "plan-claude.md").read_text(encoding="utf-8")
        # winner: existing ## Status table gains Format/Suite at the top and loses
        # the mutable status rows the agent wrote
        self.assertIn("| Format | v2 |", winner)
        self.assertLess(winner.index("| Format | v2 |"), winner.index("## Goal"))
        self.assertNotIn("| State | Planning |", winner)
        self.assertEqual(winner.count("## Status"), 1)
        # loser: has its own ## Status table but was NOT stamped — untouched, so its
        # mutable rows are still there
        self.assertIn("## Status", loser)
        self.assertNotIn("| Format | v2 |", loser)
        self.assertIn("| State | Planning |", loser)


class ScenarioSeamTests(_ScenarioDriverMixin, unittest.TestCase):
    def test_round0_agent_b_fallback_recovers_and_completes(self):
        rc, wd, msgs = self._run_new("agent-b-fallback", "Design the cache layer.")
        self.assertEqual(rc, 0)
        self.assertIn("Fallback: used stray-plan.md as plan-b.md.", msgs)
        self.assertIn("**Stopped due to:** Convergence", self._summary(wd))

    def test_empty_judge_output_halts(self):
        scenario = _SCENARIOS / "judge-empty"
        wd = self._tmpdir() / "wd"
        with self.assertRaises(plan_duel.JudgeOutputError):
            plan_duel.execute(
                argument="Design the audit log.",
                workdir_arg=str(wd),
                specs=self._specs(scenario),
                controller_name="Claude",
                participant_name="Codex",
                emit=lambda *a: None,
            )

    def test_resume_over_completed_round_missing_its_judge_re_judges_it(self):
        # An interrupted round leaves plan snapshots WITHOUT a judge file (snapshots are
        # written first). On resume that round is last_completed_round, so run_duel
        # preloads scores for the earlier rounds.
        #
        # The round is COMPLETE; its score is a real number nobody wrote down. Calling it
        # 0 rewrites the trajectory — firing a stagnation that never happened or hiding a
        # convergence that did — so it is RE-JUDGED. Every round in 1..start_round-1 must
        # still land in the score map, or building the stagnation window (which indexes
        # EVERY round) raises KeyError.
        scenario = _SCENARIOS / "convergence"
        inputs = scenario / "inputs"
        wd = (self._tmpdir() / "wd").resolve()
        wd.mkdir()
        (wd / "problem.md").write_text("Design the notification service.\n", encoding="utf-8")
        # Seed rounds 0..2 from the convergence canned inputs; round 2 is complete
        # by plan snapshots but its judge file is ABSENT (the interrupted edge).
        for n in (0, 1, 2):
            for side in ("a", "b"):
                (wd / f"plan-{side}-round-{n}.md").write_bytes(
                    (inputs / f"plan-{side}-round-{n}.md").read_bytes()
                )
        (wd / "judge-round-1.md").write_bytes((inputs / "judge-round-1.md").read_bytes())
        # NOTE: no judge-round-2.md — the interrupted round.
        for side in ("a", "b"):
            (wd / f"plan-{side}.md").write_bytes(
                (inputs / f"plan-{side}-round-2.md").read_bytes()
            )

        msgs = []
        # Must NOT raise KeyError; resumes at round 3 and converges (judge-3 = 8).
        rc = plan_duel.execute(
            argument=str(wd),
            specs=self._specs(scenario),
            controller_name="Claude",
            participant_name="Codex",
            emit=msgs.append,
        )
        self.assertEqual(rc, 0)
        self.assertIn(f"Resuming in {wd} from round 3.", msgs)
        # The missing round-2 judge is re-run, not scored 0 — and said out loud.
        self.assertIn(
            "Round 2 is complete but its judge verdict is missing or unreadable — "
            "re-judging that round rather than scoring it 0.",
            msgs,
        )
        self.assertNotIn(
            "Warning: could not parse score at round 2 — treating as 0", msgs
        )
        self.assertTrue((wd / "judge-round-2.md").is_file())
        self.assertIn("Convergence reached at round 3 (score: 8/10).", msgs)
        self.assertTrue((wd / "summary.md").is_file())
        # And the recovered score reaches the trajectory a reader sees, rather than a 0.
        self.assertIn("| 2 | 7 |", (wd / "summary.md").read_text(encoding="utf-8"))

    def test_resume_already_complete_prints_summary_and_stops(self):
        # v1 Step 1.0: a workdir whose summary.md exists is already done — print it
        # and stop, dispatching NO agents/judge (bogus specs must go untouched).
        wd = self._load_state_fixture("summary-complete")
        msgs = []
        rc = plan_duel.execute(
            argument=str(wd),
            specs={},
            controller_name="Claude",
            participant_name="Codex",
            emit=msgs.append,
        )
        self.assertEqual(rc, 0)
        self.assertEqual(len(msgs), 1)
        self.assertIn("Already done.", msgs[0])


# --------------------------------------------------------------------------- #
# Observability — run-level progress.log, elapsed labels, heartbeat, terminator
# --------------------------------------------------------------------------- #
class ProgressLogTests(_TempWorkdirMixin, unittest.TestCase):
    def _ctx(self, wd, *, started=True):
        ctx = plan_duel.DuelContext(wd, "Claude", "Codex", None)
        if started:
            ctx.started_monotonic = time.monotonic()
        return ctx

    def test_elapsed_label_none_is_zero(self):
        ctx = self._ctx(self._tmpdir(), started=False)
        self.assertEqual(plan_duel._elapsed_label(ctx), "+00:00")

    def test_elapsed_label_formats_minutes_seconds(self):
        ctx = self._ctx(self._tmpdir(), started=False)
        ctx.started_monotonic = time.monotonic() - 75.0
        self.assertRegex(plan_duel._elapsed_label(ctx), r"^\+01:1\d$")

    def test_progress_writes_per_round_file_and_progress_log(self):
        wd = self._tmpdir()
        plan_duel._progress(self._ctx(wd), 1, "round 1: judging")
        # per-round file keeps the byte-for-byte v1 content (no timestamp)
        self.assertEqual(
            (wd / "participant-progress-1.md").read_text(encoding="utf-8"),
            "round 1: judging\n",
        )
        # run-level progress.log is timestamped; the message already carries "round N:"
        # so there is no doubled round tag
        log = (wd / plan_duel.PROGRESS_LOG_NAME).read_text(encoding="utf-8")
        self.assertRegex(log, r"^\[\+\d\d:\d\d\] round 1: judging\n$")

    def test_progress_write_failure_is_swallowed(self):
        ctx = self._ctx(self._tmpdir())

        def boom(*_a, **_k):
            raise OSError("disk full")

        self.addCleanup(setattr, plan_duel, "append_progress", plan_duel.append_progress)
        plan_duel.append_progress = boom
        # An observability write failure must never propagate / abort the duel.
        plan_duel._progress(ctx, 1, "round 1: judging")
        plan_duel._append_progress_log_line(ctx, "still judging")

    def test_completion_terminator_uses_state_score_and_omits_summary(self):
        wd = self._tmpdir()
        state = plan_duel.RunState(
            "Claude", "Codex", {3: plan_duel.RoundState(True, True, 8)}
        )
        plan_duel._write_completion_terminator(self._ctx(wd), 3, "convergence", state)
        log = (wd / plan_duel.PROGRESS_LOG_NAME).read_text(encoding="utf-8")
        self.assertIn("duel complete — exit=convergence score=8 → summary.md", log)

    def test_completion_terminator_missing_score_treated_as_zero(self):
        wd = self._tmpdir()
        plan_duel._write_completion_terminator(
            self._ctx(wd), 3, "max-rounds", plan_duel.RunState("Claude", "Codex", {})
        )
        self.assertIn(
            "score=0", (wd / plan_duel.PROGRESS_LOG_NAME).read_text(encoding="utf-8")
        )

    def test_recover_agent_b_round0_ignores_progress_log(self):
        wd = self._tmpdir()
        (wd / "problem.md").write_text("p" * 300, encoding="utf-8")
        (wd / "plan-a.md").write_text("a" * 300, encoding="utf-8")
        # A fresh, >200 B progress.log must never be adopted as plan-b.md: it is a
        # .log file, outside the .md-only recovery scan.
        (wd / plan_duel.PROGRESS_LOG_NAME).write_text("log " * 100, encoding="utf-8")
        self.assertIsNone(plan_duel.recover_agent_b_round0(wd, now=time.time()))
        self.assertFalse((wd / "plan-b.md").exists())

    def test_progress_log_full_reset_but_not_round_artifact(self):
        # Encodes the resume contract: deleted on init-incomplete full reset, kept by
        # higher-round cleanup (so a normal resume keeps appending).
        self.assertTrue(plan_duel.is_full_reset_artifact(plan_duel.PROGRESS_LOG_NAME))
        self.assertIsNone(plan_duel.artifact_round(plan_duel.PROGRESS_LOG_NAME))

    def test_cleanup_all_artifacts_deletes_progress_log(self):
        wd = self._tmpdir()
        (wd / "problem.md").write_text("p", encoding="utf-8")
        (wd / plan_duel.PROGRESS_LOG_NAME).write_text("activity\n", encoding="utf-8")
        deleted = plan_duel.cleanup_all_artifacts(wd)
        self.assertIn(plan_duel.PROGRESS_LOG_NAME, deleted)
        self.assertFalse((wd / plan_duel.PROGRESS_LOG_NAME).exists())
        self.assertTrue((wd / "problem.md").exists())

    def test_cleanup_higher_rounds_keeps_progress_log(self):
        wd = self._tmpdir()
        (wd / plan_duel.PROGRESS_LOG_NAME).write_text("activity\n", encoding="utf-8")
        (wd / "plan-a-round-3.md").write_text("x" * 10, encoding="utf-8")
        deleted = plan_duel.cleanup_higher_rounds(wd, 2)
        self.assertIn("plan-a-round-3.md", deleted)
        self.assertNotIn(plan_duel.PROGRESS_LOG_NAME, deleted)
        self.assertTrue((wd / plan_duel.PROGRESS_LOG_NAME).exists())

    def test_heartbeat_teardown_is_bounded_even_if_write_blocks(self):
        ctx = self._ctx(self._tmpdir())
        entered = threading.Event()
        release = threading.Event()
        self.addCleanup(release.set)

        def blocking_write(_ctx, _text):
            entered.set()
            release.wait(10)

        self.addCleanup(
            setattr, plan_duel, "_append_progress_log_line",
            plan_duel._append_progress_log_line,
        )
        plan_duel._append_progress_log_line = blocking_write
        for name, val in (
            ("HEARTBEAT_INTERVAL_SECONDS", 0.01),
            ("HEARTBEAT_JOIN_TIMEOUT_SECONDS", 0.3),
        ):
            self.addCleanup(setattr, plan_duel, name, getattr(plan_duel, name))
            setattr(plan_duel, name, val)

        start = time.monotonic()
        with plan_duel._heartbeat(ctx, 1, "judging"):
            entered.wait(2)  # let the beat thread reach the (blocked) write
        # A stuck write must not hang teardown: bounded join (~0.3s), not forever.
        self.assertLess(time.monotonic() - start, 2.0)


class ScenarioProgressLogTests(_ScenarioDriverMixin, unittest.TestCase):
    def _run_resume_seeded(self, name, sentinel):
        scenario = _SCENARIOS / name
        wd = self._tmpdir() / "wd"
        shutil.copytree(scenario / "workdir", wd)
        wd = wd.resolve()
        (wd / plan_duel.PROGRESS_LOG_NAME).write_text(sentinel + "\n", encoding="utf-8")
        msgs = []
        rc = plan_duel.execute(
            argument=str(wd),
            specs=self._specs(scenario),
            controller_name="Claude",
            participant_name="Codex",
            emit=msgs.append,
        )
        return rc, wd, msgs

    def test_progress_log_written_with_timestamps_and_terminator(self):
        rc, wd, _ = self._run_new("convergence", "Design the notification service.")
        self.assertEqual(rc, 0)
        lines = (wd / plan_duel.PROGRESS_LOG_NAME).read_text(
            encoding="utf-8"
        ).splitlines()
        self.assertTrue(lines)
        for ln in lines:  # every line carries the elapsed-time prefix
            self.assertRegex(ln, r"^\[\+\d\d:\d\d\] ")
        text = "\n".join(lines)
        self.assertIn("round 0: generating Plan A", text)
        self.assertIn("judging", text)
        self.assertTrue(
            any("duel complete — exit=" in ln and "→ summary.md" in ln for ln in lines)
        )

    def test_full_summary_goes_to_emit_not_progress_log(self):
        rc, wd, msgs = self._run_new("convergence", "Design the notification service.")
        self.assertEqual(rc, 0)
        self.assertTrue(any("**Winner:**" in m for m in msgs))  # reached emit
        log = (wd / plan_duel.PROGRESS_LOG_NAME).read_text(encoding="utf-8")
        self.assertNotIn("**Winner:**", log)  # but not the activity log
        self.assertNotIn("| Format | v2 |", log)

    def test_normal_resume_appends_to_existing_progress_log(self):
        rc, wd, _ = self._run_resume_seeded("mid-round-interrupt", "SENTINEL-PRIOR")
        self.assertEqual(rc, 0)
        log = (wd / plan_duel.PROGRESS_LOG_NAME).read_text(encoding="utf-8")
        self.assertIn("SENTINEL-PRIOR", log)  # survived the (non-full-reset) resume
        self.assertIn("duel complete — exit=", log)  # and new lines appended

    def test_init_incomplete_resume_recreates_progress_log(self):
        rc, wd, msgs = self._run_resume_seeded("init-interrupt", "SENTINEL-PRIOR")
        self.assertEqual(rc, 0)
        self.assertIn("Deleted progress.log", msgs)  # full reset removed the seed
        log = (wd / plan_duel.PROGRESS_LOG_NAME).read_text(encoding="utf-8")
        self.assertNotIn("SENTINEL-PRIOR", log)  # recreated fresh
        self.assertIn("duel complete — exit=", log)


# =========================================================================== #
# Startup, timeout and workdir lifecycle
# =========================================================================== #
_ENGINE = _ENGINE_DIR / "plan_duel.py"

# The CI encoding proxy: no UTF-8 mode, no UTF-8 locale. On this shape `sys.stdout` is
# ASCII, so any em dash the engine prints raises UnicodeEncodeError unless main() pins
# the stream itself. On Windows PYTHONUTF8=0 does it instead, dropping stdout to the
# console code page.
_ASCII_PROXY_ENV = {"PYTHONUTF8": "0", "LC_ALL": "C", "LANG": "C"}


class EngineStreamEncodingTests(_TempWorkdirMixin, unittest.TestCase):
    """A full duel must survive a stdout that cannot represent the engine's em dashes.

    Run as a REAL subprocess: the defect is in how the interpreter opened ``sys.stdout``,
    and an in-process test inherits the suite's already-UTF-8 stream. The crash lands
    after both plans are generated and snapshotted, destroying work already paid for.
    """

    def _adapter_config(self, path, scenario_dir):
        def command(role):
            return [
                sys.executable, str(_SCENARIO_STUB),
                "--scenario-dir", str(scenario_dir),
                "--role", role,
                "--round", "⟪round⟫",
                "--workdir", "⟪workdir⟫",
            ]

        path.write_text(
            json.dumps(
                {
                    role: {
                        "command": command(role),
                        "stdout": "file",
                        "placeholders": ["round", "workdir"],
                    }
                    for role in ("agent_a", "agent_b", "judge")
                }
            ),
            encoding="utf-8",
        )
        return path

    def test_full_duel_completes_on_an_ascii_stdout(self):
        tmp = self._tmpdir()
        cfg = self._adapter_config(tmp / "adapter.json", _SCENARIOS / "convergence")
        wd = tmp / "wd"
        env = dict(os.environ)
        env.pop("PYTHONIOENCODING", None)  # would mask the very default under test
        env.update(_ASCII_PROXY_ENV)
        proc = subprocess.run(
            [
                sys.executable, str(_ENGINE),
                "Design the notification service.",
                "--workdir", str(wd),
                "--adapter-config", str(cfg),
                "--controller-name", "Claude",
                "--participant-name", "Codex",
            ],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            stdin=subprocess.DEVNULL, env=env, timeout=120,
        )
        stderr = proc.stderr.decode("utf-8", "replace")
        self.assertEqual(proc.returncode, 0, f"engine failed under an ASCII stdout:\n{stderr}")
        self.assertNotIn("UnicodeEncodeError", stderr)
        # Pinned to UTF-8 rather than merely error-replaced: the em dash arrives intact,
        # so a controller parsing the narration reads the same bytes on every host.
        stdout = proc.stdout.decode("utf-8")
        self.assertIn("Round 0 complete —", stdout)
        self.assertIn("Convergence reached at round 3 (score: 8/10).", stdout)

    def test_a_halt_message_reaches_an_ascii_stderr(self):
        # AgentOutputError carries an em dash and TemplateError carries ⟪…⟫ markers;
        # stderr needs the same pin as stdout, or the engine dies reporting the failure
        # instead of reporting it.
        tmp = self._tmpdir()
        cfg = self._adapter_config(tmp / "adapter.json", _SCENARIOS / "judge-empty")
        env = dict(os.environ)
        env.pop("PYTHONIOENCODING", None)
        env.update(_ASCII_PROXY_ENV)
        proc = subprocess.run(
            [
                sys.executable, str(_ENGINE),
                "Design the audit log.",
                "--workdir", str(tmp / "wd"),
                "--adapter-config", str(cfg),
                "--controller-name", "Claude",
                "--participant-name", "Codex",
            ],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            stdin=subprocess.DEVNULL, env=env, timeout=120,
        )
        stderr = proc.stderr.decode("utf-8")
        self.assertEqual(proc.returncode, 1)
        self.assertNotIn("UnicodeEncodeError", stderr)
        self.assertIn("Judge produced no output at round 1.", stderr)


class TimeoutFlagTests(_TempWorkdirMixin, unittest.TestCase):
    """``--timeout`` exists, defaults finite, and actually reaches every spawn."""

    def _parse_error(self, argv):
        parser = plan_duel.build_parser()
        with open(os.devnull, "w", encoding="utf-8") as devnull:
            real_stderr = sys.stderr
            sys.stderr = devnull
            try:
                with self.assertRaises(SystemExit) as ctx:
                    parser.parse_args(argv)
            finally:
                sys.stderr = real_stderr
        return ctx.exception.code

    def test_default_is_finite_and_positive(self):
        args = plan_duel.build_parser().parse_args(["problem"])
        self.assertIsInstance(args.timeout, float)
        self.assertTrue(math.isfinite(args.timeout))
        self.assertGreater(args.timeout, 0)

    def test_explicit_value_is_accepted(self):
        args = plan_duel.build_parser().parse_args(["problem", "--timeout", "90"])
        self.assertEqual(args.timeout, 90.0)

    def test_non_positive_and_non_finite_values_are_rejected(self):
        # A finite positive default is only half the guarantee: `type=float` alone
        # accepts "nan" and "inf", either of which silently restores the unbounded
        # spawn the flag exists to prevent.
        self.assertEqual(  # else an UNRECOGNIZED flag would exit 2 below for free
            plan_duel.build_parser().parse_args(["problem", "--timeout", "5"]).timeout,
            5.0,
        )
        for bad in ("0", "-1", "nan", "inf", "-inf", "abc"):
            with self.subTest(value=bad):
                self.assertEqual(self._parse_error(["problem", "--timeout", bad]), 2)

    def test_help_documents_the_flag(self):
        self.assertIn("--timeout", plan_duel.build_parser().format_help())

    def _sleeping_config(self, path, seconds):
        role = {
            "command": [
                sys.executable, str(_STUB),
                "--sleep", str(seconds),
                "--write-file", "⟪workdir⟫/plan-a.md",
                "--content", "x", "--min-bytes", "400",
            ],
            "stdout": "file",
            "placeholders": ["workdir"],
        }
        path.write_text(
            json.dumps({r: role for r in ("agent_a", "agent_b", "judge")}),
            encoding="utf-8",
        )
        return path

    def test_a_spawn_that_outlives_the_timeout_halts_the_duel(self):
        # The plumbing reached subprocess.run(timeout=) but no flag ever set it, so
        # every spawn was unbounded and CliTimeoutError unreachable from the CLI.
        tmp = self._tmpdir()
        cfg = self._sleeping_config(tmp / "adapter.json", 30)
        out, err = io.StringIO(), io.StringIO()
        started = time.monotonic()
        # StringIO has no `reconfigure`, so this also walks main()'s guarded
        # degrade-don't-abort path for a harness-replaced stream.
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = plan_duel.main([
                "Design the audit log.",
                "--workdir", str(tmp / "wd"),
                "--adapter-config", str(cfg),
                "--controller-name", "Claude",
                "--participant-name", "Codex",
                "--timeout", "0.5",
            ])
        elapsed = time.monotonic() - started
        self.assertEqual(rc, 1)
        self.assertIn("Agent A plan generation failed at round 0.", err.getvalue())
        self.assertIn("CLI timed out", err.getvalue())
        # Bounded by the timeout, not the child's own 30s sleep: proves the kill fired
        # rather than the run waiting the child out. The ceiling allows for the kill
        # ladder and the bounded drain on a slow runner.
        self.assertLess(elapsed, 25)

    def test_the_timeout_path_raises_the_agent_halt_with_its_cause(self):
        tmp = self._tmpdir()
        cfg = self._sleeping_config(tmp / "adapter.json", 30)
        specs = plan_duel.parse_adapter_config(
            (tmp / "adapter.json").read_text(encoding="utf-8")
        )
        with self.assertRaises(plan_duel.AgentOutputError) as ctx:
            plan_duel.execute(
                argument="Design the audit log.",
                workdir_arg=str(tmp / "wd"),
                specs=specs,
                controller_name="Claude",
                participant_name="Codex",
                emit=lambda *a: None,
                timeout=0.5,
            )
        self.assertEqual(
            ctx.exception.halt_message, "Agent A plan generation failed at round 0."
        )
        self.assertEqual(ctx.exception.cause, "CLI timed out")

    def _alive(self, pid):
        """Whether `pid` names a RUNNING process. A zombie has already exited.

        **POSIX only, and not merely for lack of an equivalent.** `os.kill` on Windows is
        `TerminateProcess` — signal 0 is not a probe there, so asking would answer it.
        Every caller is `skipUnless(os.name == "posix")`.
        """
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:  # pragma: no cover - alive but owned by someone else
            return True
        # Signal 0 succeeds for a zombie too, and an orphaned grandchild is a zombie
        # until whatever inherited it reaps it. Where the state is readable, read it.
        stat_path = f"/proc/{pid}/stat"
        if os.path.exists(stat_path):
            try:
                with open(stat_path, encoding="utf-8") as handle:
                    fields = handle.read().rsplit(") ", 1)[-1]
            except OSError:  # pragma: no cover - it exited while we looked
                return False
            return fields.split(" ", 1)[0] != "Z"
        return True

    def _assert_dies(self, pid, message, *, within=10.0):
        """Bounded wait for `pid` to go away, asserting on the process, not the clock.

        A kill is asynchronous — `killpg` returns once the signal is queued — so an
        immediate check races the teardown. Polling makes the green path cost
        milliseconds, while a kill that never happened leaves the process running for its
        full sleep and the bound expires. A timing assertion a no-op kill also satisfies
        does not.
        """
        deadline = time.monotonic() + within
        while self._alive(pid):
            if time.monotonic() >= deadline:
                self.fail(message)
            time.sleep(0.02)

    def _kill_quietly(self, pid):
        """POSIX-only cleanup, for the same reason as `_alive`."""
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass

    def _write(self, path, text):
        path.write_text(text, encoding="utf-8")
        return path

    def _read_pid(self, pidfile, *, within=10.0):
        """Bounded wait for a spawned process to record its own pid.

        The write races the parent returning — a leader that exits the instant it has
        spawned can get back here before its child reaches the `open`.
        """
        deadline = time.monotonic() + within
        while True:
            try:
                return int(pidfile.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                if time.monotonic() >= deadline:
                    self.fail(f"{pidfile.name} was never written")
                time.sleep(0.02)

    def test_a_timed_out_call_returns_on_every_platform(self):
        """The one timeout assertion that must hold on Windows, where the hazard lives.

        `subprocess.run(timeout=)` calls `communicate()` with no timeout after `kill()`
        there, so a survivor holding the pipe hangs the timeout path itself. Proving the
        call RETURNS, bounded, needs no PID probing — which is what keeps this runnable
        where `os.kill` is `TerminateProcess` rather than a question.
        """
        tmp = self._tmpdir()
        script = self._write(tmp / "sleeper.py", "import time\ntime.sleep(120)\n")
        started = time.monotonic()
        with self.assertRaises(plan_duel.CliTimeoutError):
            plan_duel.run_cli([sys.executable, str(script)], timeout=1.0)
        # Generous, because the point is "returns" versus "hangs forever", not latency:
        # the kill ladder and the bounded drain together cap at ~21s by construction.
        self.assertLess(time.monotonic() - started, 40)

    @unittest.skipUnless(os.name == "posix", "`_alive` probes with a signal; see its docstring")
    def test_timed_out_child_is_killed_not_left_running(self):
        # subprocess.run(timeout=) kills only the DIRECT child, and on Windows then
        # calls communicate() unbounded. Assert the child is DEAD rather than inferring
        # it from elapsed time: a `_terminate_child` that did nothing would still return
        # within the bounded drain and satisfy a timing assertion.
        tmp = self._tmpdir()
        pidfile = tmp / "child.pid"
        script = self._write(
            tmp / "sleeper.py",
            "import os, sys, time\n"
            "with open(sys.argv[1], 'w', encoding='utf-8') as fh:\n"
            "    fh.write(str(os.getpid()))\n"
            "    fh.flush()\n"
            "time.sleep(120)\n",
        )
        with self.assertRaises(plan_duel.CliTimeoutError):
            plan_duel.run_cli([sys.executable, str(script), str(pidfile)], timeout=1.0)
        pid = self._read_pid(pidfile)
        self.addCleanup(self._kill_quietly, pid)
        self._assert_dies(pid, "the timed-out child was left running")

    @unittest.skipUnless(os.name == "posix", "process groups are POSIX-only")
    def test_a_clean_run_does_not_signal_the_group(self):
        # The group kill is for the timeout and interrupt paths. Firing it after a CLI
        # exits 0 is a behaviour change, not tidying: `subprocess.run` signalled nothing
        # there, and a background helper the CLI deliberately left running in its group
        # would be killed for having been left. A drained success has no survivor holding
        # the pipes, so there is nothing the sweep could be for.
        tmp = self._tmpdir()
        pidfile = tmp / "helper.pid"
        helper = self._write(tmp / "helper.py", "import time\ntime.sleep(120)\n")
        leader = self._write(
            tmp / "leader.py",
            # DEVNULL, so the helper does not hold the pipes: the parent's read reaches
            # EOF when the leader exits and the call succeeds. The LEADER records the pid,
            # so a helper killed the instant it was spawned still leaves one to assert on
            # — otherwise this fails as "no pid file", a symptom rather than a statement.
            "import subprocess, sys\n"
            "child = subprocess.Popen([sys.executable, sys.argv[1]],\n"
            "                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)\n"
            "with open(sys.argv[2], 'w', encoding='utf-8') as fh:\n"
            "    fh.write(str(child.pid))\n"
            "sys.exit(0)\n",
        )
        result = plan_duel.run_cli(
            [sys.executable, str(leader), str(helper), str(pidfile)], timeout=30
        )
        self.assertEqual(result.returncode, 0)
        pid = self._read_pid(pidfile)
        self.addCleanup(self._kill_quietly, pid)
        time.sleep(0.3)  # a wrongly-fired kill would have landed well inside this
        self.assertTrue(
            self._alive(pid),
            "a clean run swept the child's process group and killed a background helper",
        )

    @unittest.skipUnless(os.name == "posix", "process groups are POSIX-only")
    def test_timeout_kills_a_descendant_that_outlived_the_direct_child(self):
        # The real shape of a wedged adapter: the CLI exits promptly but the runtime it
        # spawned keeps running AND holds the inherited stdout pipe, so the read blocks
        # though the direct child is gone. An escalation that stops when the leader exits
        # never signals that survivor — which is why `start_new_session` exists and both
        # rungs go to the GROUP unconditionally.
        tmp = self._tmpdir()
        pidfile = tmp / "grandchild.pid"
        grandchild = self._write(
            tmp / "grandchild.py",
            "import os, sys, time\n"
            "with open(sys.argv[1], 'w', encoding='utf-8') as fh:\n"
            "    fh.write(str(os.getpid()))\n"
            "    fh.flush()\n"
            "time.sleep(120)\n",
        )
        leader = self._write(
            tmp / "leader.py",
            # Inherits stdout/stderr into the grandchild, then exits 0 at once.
            "import subprocess, sys\n"
            "subprocess.Popen([sys.executable, sys.argv[1], sys.argv[2]])\n"
            "sys.exit(0)\n",
        )
        with self.assertRaises(plan_duel.CliTimeoutError):
            plan_duel.run_cli(
                [sys.executable, str(leader), str(grandchild), str(pidfile)], timeout=1.0
            )
        pid = self._read_pid(pidfile)
        self.addCleanup(self._kill_quietly, pid)
        self._assert_dies(
            pid,
            "a descendant outlived the timeout because the escalation stopped when the "
            "direct child exited",
        )


class ClearBeforeDispatchTests(_ScenarioDriverMixin, unittest.TestCase):
    """A CLI that exits 0 writing nothing must not have last round's plan adopted."""

    def _scenario_with_script(self, name, script):
        src = _SCENARIOS / name
        dst = self._tmpdir() / "scenario"
        shutil.copytree(src, dst)
        (dst / "script.json").write_text(json.dumps(script), encoding="utf-8")
        return dst

    def _run(self, scenario, wd):
        return plan_duel.execute(
            argument="Design the notification service.",
            workdir_arg=str(wd),
            specs=self._specs(scenario),
            controller_name="Claude",
            participant_name="Codex",
            emit=lambda *a: None,
        )

    def test_silent_no_op_agent_a_halts_instead_of_resnapshotting_round_1(self):
        scenario = self._scenario_with_script("convergence", {"agent_a:2": {"missing": True}})
        wd = self._tmpdir() / "wd"
        with self.assertRaises(plan_duel.AgentOutputError) as ctx:
            self._run(scenario, wd)
        self.assertEqual(
            ctx.exception.halt_message, "Agent A update failed at round 2."
        )
        wd = wd.resolve()
        self.assertFalse(
            (wd / "plan-a-round-2.md").exists(),
            "round 1's plan was snapshotted as round 2's revision",
        )
        # Round 1's own snapshot is untouched — the clearing is scoped to the LIVE file.
        self.assertTrue((wd / "plan-a-round-1.md").is_file())

    def test_silent_no_op_agent_b_halts_instead_of_resnapshotting_round_1(self):
        scenario = self._scenario_with_script("convergence", {"agent_b:2": {"missing": True}})
        wd = self._tmpdir() / "wd"
        with self.assertRaises(plan_duel.AgentOutputError) as ctx:
            self._run(scenario, wd)
        self.assertEqual(
            ctx.exception.halt_message, "Agent B update failed at round 2."
        )
        wd = wd.resolve()
        self.assertFalse((wd / "plan-b-round-2.md").exists())
        # Agent A ran first and succeeded, so its round-2 live plan is real work.
        self.assertTrue((wd / "plan-a.md").is_file())

    @unittest.skipUnless(os.name == "posix", "symlink creation needs privileges on Windows")
    def test_a_symlinked_output_is_not_accepted_as_a_fresh_revision(self):
        # Clearing the file makes "it exists" mean "this dispatch created it" — but not
        # that it WROTE anything. `exists()` and `st_size` both follow a symlink, so an
        # agent exiting 0 after pointing plan-a.md at the frozen round-1 snapshot gets
        # that snapshot snapshotted straight back as round 2's revision.
        scenario = self._scenario_with_script(
            "convergence", {"agent_a:2": {"symlink": "plan-a-round-1.md"}}
        )
        wd = self._tmpdir() / "wd"
        with self.assertRaises(plan_duel.AgentOutputError) as ctx:
            self._run(scenario, wd)
        self.assertEqual(ctx.exception.halt_message, "Agent A update failed at round 2.")
        wd = wd.resolve()
        self.assertFalse((wd / "plan-a-round-2.md").exists())

    @unittest.skipUnless(os.name == "posix", "symlink creation needs privileges on Windows")
    def test_round_0_rejects_a_symlinked_plan_the_same_way_a_critique_round_does(self):
        # `_require_regular_file` guards the CRITIQUE rounds; round 0 never reaches it, so
        # the substitution moves one round earlier. Agent B exits 0 having pointed
        # plan-b.md at plan-a.md, and `is_file()` looks through the link and snapshots
        # Plan A as Plan B. Both dispatch paths must reject the same thing.
        scenario = self._scenario_with_script(
            "convergence", {"agent_b:0": {"symlink": "plan-a.md"}}
        )
        wd = self._tmpdir() / "wd"
        with self.assertRaises(plan_duel.AgentOutputError) as ctx:
            self._run(scenario, wd)
        self.assertEqual(
            ctx.exception.halt_message, "Agent B plan generation failed at round 0."
        )
        wd = wd.resolve()
        self.assertFalse((wd / "plan-b-round-0.md").exists())

    def test_a_failed_round_0_snapshot_copy_halts_with_the_agents_own_message(self):
        # The check-to-copy race no pre-checking closes: the validated file can be
        # removed, replaced or locked before `copy_bytes`. Left untranslated, `run_agent`
        # has already returned and the copy raises a bare PermissionError from outside
        # the diagnostic path.
        real_copy = plan_duel.copy_bytes

        def failing(src, dst, *args, **kwargs):
            if Path(dst).name == "plan-a-round-0.md":
                raise PermissionError(13, "Permission denied")
            return real_copy(src, dst, *args, **kwargs)

        wd = self._tmpdir() / "wd"
        with unittest.mock.patch.object(plan_duel, "copy_bytes", failing):
            with self.assertRaises(plan_duel.AgentOutputError) as ctx:
                self._run(self._scenario_with_script("convergence", {}), wd)
        self.assertEqual(
            ctx.exception.halt_message, "Agent A plan generation failed at round 0."
        )

    def test_a_failed_critique_round_snapshot_copy_halts_the_same_way(self):
        # The same window on the other dispatch path, so neither is uniform by timing.
        real_copy = plan_duel.copy_bytes

        def failing(src, dst, *args, **kwargs):
            if Path(dst).name == "plan-b-round-2.md":
                raise PermissionError(13, "Permission denied")
            return real_copy(src, dst, *args, **kwargs)

        wd = self._tmpdir() / "wd"
        with unittest.mock.patch.object(plan_duel, "copy_bytes", failing):
            with self.assertRaises(plan_duel.AgentOutputError) as ctx:
                self._run(self._scenario_with_script("convergence", {}), wd)
        self.assertEqual(ctx.exception.halt_message, "Agent B update failed at round 2.")

    def test_each_round_snapshots_its_own_revision(self):
        rc, wd, _ = self._run_new("convergence", "Design the notification service.")
        self.assertEqual(rc, 0)
        for n in range(0, 4):
            self.assertTrue((wd / f"plan-a-round-{n}.md").is_file())
            self.assertTrue((wd / f"plan-b-round-{n}.md").is_file())
        # Each round's snapshot is that round's own revision, not a duplicate of the last.
        self.assertNotEqual(
            (wd / "plan-a-round-1.md").read_bytes(),
            (wd / "plan-a-round-2.md").read_bytes(),
        )

    def test_the_freeze_still_has_the_live_plans_to_copy_from(self):
        """The placement hazard, in the only shape that can catch it.

        Clearing must land AFTER `freeze_round_inputs`, because the freeze CREATES a
        missing round-(N-1) snapshot by copying the live plan. A normal run cannot prove
        that: every prior snapshot exists, the freeze copies nothing, and moving the clear
        ahead of it would pass just the same. The case that bites is the hand-built or
        partially-recovered workdir, where clearing first leaves the freeze nothing to read.
        """
        scenario = _SCENARIOS / "convergence"
        wd = self._tmpdir() / "wd"
        wd.mkdir()
        (wd / "problem.md").write_text("Design the notification service.\n", encoding="utf-8")
        live_a = (scenario / "inputs" / "plan-a-round-0.md").read_bytes()
        live_b = (scenario / "inputs" / "plan-b-round-0.md").read_bytes()
        (wd / "plan-a.md").write_bytes(live_a)
        (wd / "plan-b.md").write_bytes(live_b)
        # Deliberately NO plan-{a,b}-round-0.md: the freeze has to mint them.
        self.assertFalse((wd / "plan-a-round-0.md").exists())

        plan_duel.run_critique_round(
            workdir=wd,
            round_n=1,
            specs=self._specs(scenario),
            ctx=plan_duel.DuelContext(wd, "Claude", "Codex", None),
            emit=lambda *a: None,
            timeout=None,
            state=plan_duel.RunState("Claude", "Codex"),
        )

        # The frozen inputs are byte-copies of the live plans as they stood BEFORE the
        # clearing — which is only possible if the freeze ran first.
        self.assertEqual((wd / "plan-a-round-0.md").read_bytes(), live_a)
        self.assertEqual((wd / "plan-b-round-0.md").read_bytes(), live_b)
        self.assertTrue((wd / "plan-a-round-1.md").is_file())


class NewWorkdirResolutionTests(_TempWorkdirMixin, unittest.TestCase):
    """An explicit workdir is never silently written over; an auto name never collides."""

    def _chdir(self, target):
        previous = Path.cwd()
        os.chdir(target)
        self.addCleanup(os.chdir, str(previous))

    def test_non_empty_explicit_workdir_is_refused(self):
        wd = self._tmpdir() / "notes"
        wd.mkdir()
        (wd / "keep-me.txt").write_text("someone else's work\n", encoding="utf-8")
        with self.assertRaises(plan_duel.PlanDuelError):
            plan_duel._resolve_new_workdir(str(wd), "Design the audit log.")

    def test_empty_explicit_workdir_is_still_accepted(self):
        wd = self._tmpdir() / "fresh"
        wd.mkdir()
        self.assertEqual(
            plan_duel._resolve_new_workdir(str(wd), "Design the audit log."), Path(str(wd))
        )

    def test_missing_explicit_workdir_is_accepted(self):
        wd = self._tmpdir() / "not-yet"
        self.assertEqual(
            plan_duel._resolve_new_workdir(str(wd), "Design the audit log."), Path(str(wd))
        )

    def test_explicit_workdir_that_is_a_file_is_refused(self):
        target = self._tmpdir() / "notes.md"
        target.write_text("not a directory\n", encoding="utf-8")
        with self.assertRaises(plan_duel.PlanDuelError):
            plan_duel._resolve_new_workdir(str(target), "Design the audit log.")

    def test_execute_refuses_rather_than_writing_into_a_non_empty_workdir(self):
        tmp = self._tmpdir()
        wd = tmp / "notes"
        wd.mkdir()
        (wd / "keep-me.txt").write_text("someone else's work\n", encoding="utf-8")
        cfg = {
            role: {
                "command": [sys.executable, str(_STUB)],
                "stdout": "file",
            }
            for role in ("agent_a", "agent_b", "judge")
        }
        with self.assertRaises(plan_duel.PlanDuelError):
            plan_duel.execute(
                argument="Design the audit log.",
                workdir_arg=str(wd),
                specs=plan_duel.parse_adapter_config(cfg),
                controller_name="Claude",
                participant_name="Codex",
                emit=lambda *a: None,
            )
        self.assertFalse((wd / "problem.md").exists())
        self.assertEqual(
            (wd / "keep-me.txt").read_text(encoding="utf-8"), "someone else's work\n"
        )

    def test_auto_name_suffixes_on_any_existing_path(self):
        # The loop only skipped a directory that already held problem.md, so an
        # interrupted duel whose problem.md never landed was reused and its half-written
        # artifacts inherited by the next run.
        tmp = self._tmpdir()
        self._chdir(tmp)
        slug = plan_duel.problem_slug("Design the audit log.")
        existing = Path("plans") / "duels" / slug
        existing.mkdir(parents=True)
        (existing / "plan-a.md").write_text("half a plan\n", encoding="utf-8")
        self.assertEqual(
            plan_duel._resolve_new_workdir(None, "Design the audit log."),
            Path("plans") / "duels" / f"{slug}-2",
        )

    def test_auto_name_still_walks_past_several_existing_directories(self):
        tmp = self._tmpdir()
        self._chdir(tmp)
        slug = plan_duel.problem_slug("Design the audit log.")
        base = Path("plans") / "duels"
        (base / slug).mkdir(parents=True)
        (base / f"{slug}-2").mkdir()
        self.assertEqual(
            plan_duel._resolve_new_workdir(None, "Design the audit log."),
            base / f"{slug}-3",
        )

    def test_the_directory_is_reserved_by_the_call_not_left_for_later(self):
        # Check-then-create is the defect: two duels resolving the same slug both see the
        # candidate absent, both mkdir(exist_ok=True), and interleave their artifacts. The
        # property is that the call CREATES what it returns, so a second call cannot be
        # handed the same path.
        tmp = self._tmpdir()
        self._chdir(tmp)
        first = plan_duel._resolve_new_workdir(None, "Design the audit log.")
        self.assertTrue(first.is_dir(), "the call returned a path it had not reserved")
        second = plan_duel._resolve_new_workdir(None, "Design the audit log.")
        self.assertNotEqual(first, second)
        self.assertTrue(second.is_dir())

    def test_explicit_workdir_is_reserved_by_the_call_too(self):
        wd = self._tmpdir() / "fresh"
        self.assertEqual(plan_duel._resolve_new_workdir(str(wd), "Design it."), wd)
        self.assertTrue(wd.is_dir())
        self.assertEqual((wd / "problem.md").read_text(encoding="utf-8"), "Design it.\n")

    def test_an_existing_empty_explicit_workdir_is_reserved_not_merely_accepted(self):
        # mkdir cannot arbitrate this: the directory already exists, and refusing it would
        # break pre-creating a workdir, which is a documented workflow. So the reservation
        # is problem.md, created exclusively — two runs aimed at the same empty directory
        # cannot both proceed. Sequentially, the second call must be refused.
        wd = self._tmpdir() / "precreated"
        wd.mkdir()
        self.assertEqual(plan_duel._resolve_new_workdir(str(wd), "Design it."), wd)
        with self.assertRaises(plan_duel.PlanDuelError):
            plan_duel._resolve_new_workdir(str(wd), "A different problem entirely.")
        # The winner's statement stands; the loser wrote nothing over it.
        self.assertEqual((wd / "problem.md").read_text(encoding="utf-8"), "Design it.\n")

    def test_the_claim_itself_is_exclusive_and_never_overwrites(self):
        wd = self._tmpdir() / "precreated"
        wd.mkdir()
        self.assertTrue(plan_duel._claim_problem_md(wd, "first"))
        self.assertFalse(plan_duel._claim_problem_md(wd, "second"))
        self.assertEqual((wd / "problem.md").read_text(encoding="utf-8"), "first\n")

    def test_the_claim_writes_the_same_bytes_the_old_write_did(self):
        # A resume reads problem.md back, so moving the write must not move the bytes.
        claimed = self._tmpdir() / "claimed"
        claimed.mkdir()
        plan_duel._claim_problem_md(claimed, "Design it.\r\nSecond line.")
        reference = self._tmpdir() / "reference.md"
        plan_duel.write_text_utf8(reference, "Design it.\r\nSecond line.\n")
        self.assertEqual(
            (claimed / "problem.md").read_bytes(), reference.read_bytes()
        )

    @unittest.skipUnless(os.name == "posix", "symlink creation needs privileges on Windows")
    def test_a_dangling_symlink_at_an_explicit_workdir_is_refused(self):
        # `Path.exists()` follows the link and reports absent, so the path was accepted
        # and the later mkdir raised a bare FileExistsError instead of this refusal.
        wd = self._tmpdir() / "wd"
        wd.symlink_to(self._tmpdir() / "nowhere")
        with self.assertRaises(plan_duel.PlanDuelError):
            plan_duel._resolve_new_workdir(str(wd), "Design the audit log.")

    @unittest.skipUnless(os.name == "posix", "symlink creation needs privileges on Windows")
    def test_a_dangling_symlink_makes_the_auto_name_step_aside(self):
        tmp = self._tmpdir()
        self._chdir(tmp)
        slug = plan_duel.problem_slug("Design the audit log.")
        base = Path("plans") / "duels"
        base.mkdir(parents=True)
        (base / slug).symlink_to(tmp / "nowhere")
        self.assertEqual(
            plan_duel._resolve_new_workdir(None, "Design the audit log."),
            base / f"{slug}-2",
        )


class ReservedDeviceNamesCannotBecomeAWorkdir(unittest.TestCase):
    """A problem statement of `CON` produced `plans/duels/con`, which Windows cannot create.

    `CON`, `PRN`, `AUX`, `NUL`, `COM1`-`COM9` and `LPT1`-`LPT9` are device names on Windows
    at every directory level, so `mkdir` fails with an uncaught OSError before the duel has
    done anything. Nobody here develops on Windows, which is why this belongs in the code
    rather than in anyone's habits. The slug is lowercased and Windows matches these
    case-insensitively, so `con` is as reserved as `CON`; `CON.md` is reserved too.
    """

    def test_every_reserved_device_name_is_escaped(self):
        for name in ("CON", "PRN", "AUX", "NUL", "COM1", "COM9", "LPT1", "LPT9",
                     "con", "NuL"):
            with self.subTest(name=name):
                slug = plan_duel.problem_slug(name)
                self.assertNotIn(slug.split(".")[0].upper(),
                                 {"CON", "PRN", "AUX", "NUL"}
                                 | {f"COM{d}" for d in "123456789"}
                                 | {f"LPT{d}" for d in "123456789"},
                                 f"{name!r} slugified to {slug!r}, which Windows refuses")
                self.assertTrue(slug, "the slug must not be empty")

    def test_an_ordinary_statement_is_untouched(self):
        """The control: escaping must not reach words that merely start the same way."""
        self.assertEqual(plan_duel.problem_slug("Console rendering for the audit log"),
                         "console-rendering-audit-log")
        self.assertEqual(plan_duel.problem_slug("Auxiliary index compaction"),
                         "auxiliary-index-compaction")


class ResumeIntentTests(_TempWorkdirMixin, unittest.TestCase):
    """Resume intent comes from the POSITIONAL argument, never from ``--workdir``.

    `SKILL.md` documents resume as passing the duel workdir positionally, and `--workdir`
    only as choosing where a NEW run lands. While the engine scanned both, `--workdir`
    pointed at anything holding a `problem.md` was silently taken as a resume: the new
    problem statement was discarded and `apply_resume` DELETED files matching `plan-*.md`
    in a directory the user never meant to resume.
    """

    def _specs(self):
        return plan_duel.parse_adapter_config(
            {
                role: {"command": [sys.executable, str(_STUB)], "stdout": "file"}
                for role in ("agent_a", "agent_b", "judge")
            }
        )

    def _duel_looking_dir(self):
        wd = self._tmpdir() / "someone-elses-duel"
        wd.mkdir()
        (wd / "problem.md").write_text("their original problem\n", encoding="utf-8")
        (wd / "plan-a.md").write_text("their plan A\n" * 40, encoding="utf-8")
        return wd

    def test_workdir_pointing_at_a_problem_md_is_refused_not_resumed(self):
        wd = self._duel_looking_dir()
        with self.assertRaises(plan_duel.PlanDuelError) as ctx:
            plan_duel.execute(
                argument="A brand new and completely unrelated problem.",
                workdir_arg=str(wd),
                specs=self._specs(),
                controller_name="Claude",
                participant_name="Codex",
                emit=lambda *a: None,
            )
        # Nothing was deleted, and the original problem statement still stands.
        self.assertEqual(
            (wd / "problem.md").read_text(encoding="utf-8"), "their original problem\n"
        )
        self.assertTrue((wd / "plan-a.md").is_file())
        # The message has to say how to resume, since --workdir no longer does it.
        self.assertIn("positional", str(ctx.exception))

    def test_workdir_alone_says_how_to_resume_rather_than_demanding_a_problem(self):
        # `--workdir <a duel>` with no positional argument cannot resume now, so the
        # refusal has to name the positional form — otherwise the user is told only
        # "No problem statement provided", which is true and useless.
        wd = self._duel_looking_dir()
        with self.assertRaises(plan_duel.PlanDuelError) as ctx:
            plan_duel.execute(
                argument=None,
                workdir_arg=str(wd),
                specs=self._specs(),
                controller_name="Claude",
                participant_name="Codex",
                emit=lambda *a: None,
            )
        self.assertIn("positional", str(ctx.exception))
        self.assertTrue((wd / "plan-a.md").is_file())

    def test_the_positional_argument_still_resumes(self):
        wd = self._tmpdir() / "wd"
        shutil.copytree(_SCENARIOS / "mid-round-interrupt" / "workdir", wd)
        msgs = []
        rc = plan_duel.execute(
            argument=str(wd),
            specs=plan_duel.parse_adapter_config(
                {
                    role: {
                        "command": [
                            sys.executable, str(_SCENARIO_STUB),
                            "--scenario-dir", str(_SCENARIOS / "mid-round-interrupt"),
                            "--role", role,
                            "--round", "⟪round⟫",
                            "--workdir", "⟪workdir⟫",
                        ],
                        "stdout": "file",
                        "placeholders": ["round", "workdir"],
                    }
                    for role in ("agent_a", "agent_b", "judge")
                }
            ),
            controller_name="Claude",
            participant_name="Codex",
            emit=msgs.append,
        )
        self.assertEqual(rc, 0)
        self.assertTrue(any(m.startswith("Resuming in ") for m in msgs))




# --------------------------------------------------------------------------- #
# Resume: the winner a duel reports must not depend on where it was interrupted
# --------------------------------------------------------------------------- #
class _ResumeHarness(_TempWorkdirMixin):
    """`run_duel`'s preload and exit checks, driven end to end against the stub CLI.

    These cover the worst shape in the pack: a resumed duel reporting a DIFFERENT winner
    than the same duel run start to finish, with nothing in the output saying so. Two
    causes — a completed round whose judge file went missing being scored 0 rather than
    re-judged, and the exit condition being checked only after a round has run, so a duel
    that already converged runs one more and re-scores on it.

    The stub judge is deterministic, so any difference between an interrupted run and an
    uninterrupted one is the engine's.
    """

    def _specs(self, score=9, preferred="A"):
        """Agents that write their plan; a judge whose clean message is a fixed score."""
        return plan_duel.parse_adapter_config({
            "agent_a": {
                "command": [sys.executable, str(_STUB), "--write-file",
                            "⟪workdir⟫/plan-a.md", "--content", "A" * 400],
                "stdout": "file",
            },
            "agent_b": {
                "command": [sys.executable, str(_STUB), "--write-file",
                            "⟪workdir⟫/plan-b.md", "--content", "B" * 400],
                "stdout": "file",
            },
            "judge": {
                "command": [sys.executable, str(_STUB), "--stdout",
                            f"SCORE: {score}\n\nPREFERRED: {preferred}\n"],
                "stdout": "clean-last-message",
            },
        })

    def _ctx(self, wd):
        return plan_duel.DuelContext(
            workdir=wd, controller_name="Claude", participant_name="Codex"
        )

    def _seed(self, wd, through_round, score=9):
        """A workdir carrying rounds 1..through_round, complete with judge files."""
        wd.mkdir(parents=True, exist_ok=True)
        (wd / "problem.md").write_text("p" * 400, encoding="utf-8")
        for rnd in range(0, through_round + 1):
            for side in ("a", "b"):
                (wd / plan_duel.plan_snapshot_name(side, rnd)).write_text(
                    side * 400, encoding="utf-8")
            if rnd >= 1:
                (wd / f"judge-round-{rnd}.md").write_text(
                    f"SCORE: {score}\n\nPREFERRED: A\n", encoding="utf-8")
        for side in ("a", "b"):
            (wd / f"plan-{side}.md").write_text(side * 400, encoding="utf-8")
        return wd

    def _interrupted_state(self, wd, round_n):
        """`state.json` as an interruption mid-judge leaves it: plans in, judge not."""
        state = plan_duel.RunState("Claude", "Codex")
        state.rounds[round_n] = plan_duel.RoundState(
            plans_snapshotted=True, judge_completed=False, score=None)
        plan_duel.save_state(wd, state)
        return state

    def _run(self, wd, start_round, state=None, **kw):
        msgs = []
        rounds_run, stop = plan_duel.run_duel(
            workdir=wd, specs=self._specs(**kw), ctx=self._ctx(wd),
            start_round=start_round, emit=msgs.append, timeout=60,
            state=state if state is not None else plan_duel.RunState(),
        )
        return rounds_run, stop, msgs


class ResumeRunDuelTests(_ResumeHarness, unittest.TestCase):
    """`run_duel`'s preload and exit checks, driven end to end against the stub CLI."""

    def test_a_resume_that_already_converged_writes_no_further_round(self):
        # Rounds 1-3 all scored 9, so convergence (round >= 3, score >= 8) is already
        # satisfied by what is on disk. Running round 4 would re-score the duel on a
        # round that should never have happened.
        wd = self._seed(self._tmpdir() / "wd", 3, score=9)
        rounds_run, stop, _ = self._run(wd, 4)
        self.assertEqual(stop, plan_duel.CONVERGENCE_LABEL)
        self.assertEqual(rounds_run, 3)
        self.assertFalse(
            (wd / plan_duel.plan_snapshot_name("a", 4)).exists(),
            "round 4 ran on a duel that had already converged")

    def test_a_missing_judge_file_is_re_judged_rather_than_scored_zero(self):
        # Round 2's judge file is gone — an interruption between writing the plan
        # snapshots and writing the judge. Scoring it 0 rewrites the trajectory and can
        # fire stagnation that never happened.
        wd = self._seed(self._tmpdir() / "wd", 3, score=9)
        (wd / "judge-round-3.md").unlink()
        _, _, msgs = self._run(wd, 4)
        self.assertTrue((wd / "judge-round-3.md").is_file(),
                        "the missing judge file was not re-run")
        self.assertEqual(plan_duel.parse_score(
            (wd / "judge-round-3.md").read_text(encoding="utf-8")), 9)
        self.assertFalse([m for m in msgs if "could not be parsed" in m.lower()],
                         f"a re-judged round should not warn about a score: {msgs}")

    def test_a_partial_judge_file_is_cleared_before_the_re_judge(self):
        # Half a judge file is worse than none: it parses to nothing but is present, and
        # an adapter that writes the file itself would leave the stale bytes in place.
        wd = self._seed(self._tmpdir() / "wd", 3, score=9)
        (wd / "judge-round-3.md").write_text("SCOR", encoding="utf-8")
        self._run(wd, 4, state=self._interrupted_state(wd, 3))
        text = (wd / "judge-round-3.md").read_text(encoding="utf-8")
        self.assertNotIn("SCOR\n", text)
        self.assertEqual(plan_duel.parse_score(text), 9)

    def test_a_truncated_verdict_whose_SCORE_line_survived_is_still_re_judged(self):
        """The half of the interrupted-judge fix that was written and never wired up.

        The preload asked `judge_needs_rerun` only when `parse_score` had already failed,
        so a verdict killed mid-write was trusted whenever its `SCORE:` line landed first
        — which it does, being the first line the judge writes. The fragment
        `SCORE: 6\\n\\nDI` parses to 6 and carries no `PREFERRED:` line, so the resumed duel
        took a made-up score and defaulted the winner to A.

        The sibling cases above all use fragments that do NOT parse (`SCOR`, `SCORE:`),
        which is why the suite stayed green while this was open.
        """
        wd = self._seed(self._tmpdir() / "wd", 3, score=9)
        (wd / "judge-round-3.md").write_text("SCORE: 6\n\nDI", encoding="utf-8")
        self.assertEqual(
            plan_duel.parse_score("SCORE: 6\n\nDI"), 6,
            "the premise of this test is that the fragment DOES parse")
        self._run(wd, 4, state=self._interrupted_state(wd, 3))
        text = (wd / "judge-round-3.md").read_text(encoding="utf-8")
        self.assertNotIn("DI", text, "the truncated verdict was adopted rather than re-run")
        self.assertEqual(plan_duel.parse_score(text), 9)
        self.assertIn("PREFERRED: A", text)

    def test_an_earlier_rounds_missing_judge_is_not_re_judged(self):
        """Only the LAST completed round may be re-judged.

        `round.md` sends the judge to ⟪workdir⟫/plan-a.md and plan-b.md — the LIVE plans —
        and a resume restores those from the last completed round. Re-judging round 2 would
        score round 3's plans and file the verdict as round 2's: a confident wrong number
        where the old code left an obvious zero.
        """
        wd = self._seed(self._tmpdir() / "wd", 3, score=9)
        (wd / "judge-round-2.md").unlink()
        _, _, msgs = self._run(wd, 4)
        self.assertFalse((wd / "judge-round-2.md").exists(),
                         "an earlier round was re-judged against the wrong plans")
        self.assertTrue([m for m in msgs if "round 2" in m.lower()],
                        f"the fallback to 0 must still be said out loud: {msgs}")

    def test_a_stale_judge_file_is_gone_even_when_the_re_judge_writes_nothing(self):
        """The clear is what removes it — not the redirect that happens to truncate.

        With `stdout: "clean-last-message"` the engine opens the target for writing, so a
        stale file is truncated whether or not anything unlinked it. The shape that needs
        the explicit clear is `stdout: "file"`, where the CLI writes the file itself and a
        CLI that writes nothing leaves last time's bytes to be read as this round's verdict.
        """
        wd = self._seed(self._tmpdir() / "wd", 3, score=9)
        # Partial, so it is unparseable and the re-judge fires. A stale file that still
        # parses is indistinguishable from a real verdict and is left alone by design.
        (wd / "judge-round-3.md").write_text("SCORE:", encoding="utf-8")
        specs = plan_duel.parse_adapter_config({
            "agent_a": {"command": [sys.executable, str(_STUB), "--write-file",
                                    "⟪workdir⟫/plan-a.md", "--content", "A" * 400],
                        "stdout": "file"},
            "agent_b": {"command": [sys.executable, str(_STUB), "--write-file",
                                    "⟪workdir⟫/plan-b.md", "--content", "B" * 400],
                        "stdout": "file"},
            # Writes NOTHING: the shape of a refusal or a permission denial, neither of
            # which is a non-zero exit.
            "judge": {"command": [sys.executable, str(_STUB)], "stdout": "file"},
        })
        msgs = []
        # Round 4 then runs for real and the same silent judge halts it — correct, and
        # beside the point here. The preload has already happened.
        with self.assertRaises(plan_duel.JudgeOutputError):
            plan_duel.run_duel(
                workdir=wd, specs=specs, ctx=self._ctx(wd), start_round=4,
                emit=msgs.append, timeout=60,
                state=self._interrupted_state(wd, 3))
        self.assertFalse(
            (wd / "judge-round-3.md").exists(),
            "the stale verdict survived a re-judge that wrote nothing — it would have "
            "been read back as this round's score")
        self.assertTrue([m for m in msgs if "falls back to 0" in m],
                        f"a failed re-judge must say so: {msgs}")

    def test_a_complete_verdict_that_will_not_parse_is_kept_and_scored_zero(self):
        """v1's `score(N)` convention, and the reason it survives.

        A verdict the judge finished writing is the real one even when its score line is
        unusable, and its `PREFERRED:` line may still name a winner. Deleting it to ask
        again would replace a real answer with a different one.
        """
        wd = self._seed(self._tmpdir() / "wd", 3, score=9)
        verdict = "No number here.\n\nPREFERRED: B\n"
        (wd / "judge-round-3.md").write_text(verdict, encoding="utf-8")
        state = plan_duel.RunState("Claude", "Codex")
        state.rounds[3] = plan_duel.RoundState(
            plans_snapshotted=True, judge_completed=True, score=None)
        plan_duel.save_state(wd, state)
        _, _, msgs = self._run(wd, 4, state=state)
        self.assertEqual((wd / "judge-round-3.md").read_text(encoding="utf-8"), verdict,
                         "a finished verdict was thrown away and asked again")
        self.assertTrue([m for m in msgs if "treating as 0" in m],
                        f"the v1 fallback must still be announced: {msgs}")

    def test_every_preloaded_round_still_lands_in_scores(self):
        # The exit check indexes every round; a skipped entry raises KeyError instead of
        # reproducing v1's treat-as-0 behaviour. A judge that cannot be re-run must still
        # score, not vanish.
        wd = self._seed(self._tmpdir() / "wd", 3, score=5)
        (wd / "judge-round-3.md").unlink()
        rounds_run, stop, _ = self._run(wd, 4, score=5)
        self.assertIsInstance(rounds_run, int)
        self.assertEqual(stop, plan_duel.STAGNATION_LABEL)


class ARejudgeDegradesRatherThanHalting(_ResumeHarness, unittest.TestCase):
    """`_rejudge_round`'s own contract, applied to every way it can fail.

    A re-run that fails does NOT halt the duel: it degrades to v1's 0 and says so, because
    turning a recoverable resume into a halt would be worse than the defect it replaced.
    The except tuple named `JudgeOutputError`, `ProcessError` and `OSError`; a MISSING
    SCHEMA raises `TemplateError`, which is none of them.

    A resume past the round cap is where that bites: it skips `preflight_schema`, since a
    duel whose rounds are all complete needs only its summary written, so the unresolved
    `⟪schema_json⟫` marker surfaces at the judge dispatch instead.
    """

    def _schema_judge_specs(self):
        """A judge whose argv needs a schema the skill dir does not carry."""
        return plan_duel.parse_adapter_config({
            "agent_a": {"command": [sys.executable, str(_STUB), "--write-file",
                                    "⟪workdir⟫/plan-a.md", "--content", "A" * 400],
                        "stdout": "file"},
            "agent_b": {"command": [sys.executable, str(_STUB), "--write-file",
                                    "⟪workdir⟫/plan-b.md", "--content", "B" * 400],
                        "stdout": "file"},
            # `--echo-arg` is the stub flag that writes an arbitrary value verbatim, so
            # the marker travels through argv exactly as a real adapter's --output-schema
            # would, and the file it lands in proves it resolved.
            "judge": {"command": [sys.executable, str(_STUB), "--stdout",
                                  "SCORE: 9\n\nPREFERRED: A\n",
                                  "--echo-arg", "⟪schema_json⟫",
                                  "--echo-file", "⟪workdir⟫/schema-seen.txt"],
                      "stdout": "clean-last-message"},
        })

    def _past_the_cap(self, skill_dir):
        """A workdir interrupted after round 10's snapshots but before its judge."""
        wd = self._seed(self._tmpdir() / "wd", plan_duel.MAX_ROUNDS, score=9)
        (wd / f"judge-round-{plan_duel.MAX_ROUNDS}.md").unlink()
        state = plan_duel.RunState("Claude", "Codex")
        state.rounds[plan_duel.MAX_ROUNDS] = plan_duel.RoundState(
            plans_snapshotted=True, judge_completed=False, score=None)
        plan_duel.save_state(wd, state)
        ctx = plan_duel.DuelContext(
            workdir=wd, controller_name="Claude", participant_name="Codex",
            skill_dir=skill_dir)
        return wd, ctx, state

    def _resume(self, specs, skill_dir):
        wd, ctx, state = self._past_the_cap(skill_dir)
        msgs = []
        rounds_run, stop = plan_duel.run_duel(
            workdir=wd, specs=specs, ctx=ctx,
            start_round=plan_duel.MAX_ROUNDS + 1, emit=msgs.append,
            timeout=60, state=state)
        return wd, rounds_run, stop, msgs

    def test_a_missing_schema_degrades_the_way_a_missing_cli_does(self):
        # `skill_dir` exists but carries no judge-schema.json, so `schema_values()` is
        # empty and `⟪schema_json⟫` never resolves.
        empty_skill_dir = self._tmpdir()
        _, _, stop, msgs = self._resume(self._schema_judge_specs(), empty_skill_dir)
        self.assertEqual(stop, plan_duel.CONVERGENCE_LABEL)
        self.assertTrue(
            [m for m in msgs if "falls back to 0" in m],
            f"the failed re-judge must be announced, not raised: {msgs}")
        self.assertTrue([m for m in msgs if "schema_json" in m],
                        f"the message must name what was missing: {msgs}")

    def test_a_missing_cli_still_degrades_the_same_way(self):
        """The comparison the finding is stated against — unchanged by this fix."""
        specs = plan_duel.parse_adapter_config({
            "agent_a": {"command": [sys.executable, str(_STUB), "--write-file",
                                    "⟪workdir⟫/plan-a.md", "--content", "A" * 400],
                        "stdout": "file"},
            "agent_b": {"command": [sys.executable, str(_STUB), "--write-file",
                                    "⟪workdir⟫/plan-b.md", "--content", "B" * 400],
                        "stdout": "file"},
            "judge": {"command": ["plan-duel-no-such-cli-xyz"],
                      "stdout": "clean-last-message"},
        })
        _, _, stop, msgs = self._resume(specs, None)
        self.assertEqual(stop, plan_duel.CONVERGENCE_LABEL)
        self.assertTrue([m for m in msgs if "falls back to 0" in m], msgs)

    def test_a_working_schema_judge_is_not_degraded(self):
        """Anti-vacuity: swallowing everything would satisfy the two above.

        The real skill directory ships `judge-schema.json`, so the marker resolves and
        the re-judge produces a real verdict.
        """
        wd, _, stop, msgs = self._resume(self._schema_judge_specs(), _ENGINE_DIR)
        self.assertEqual(stop, plan_duel.CONVERGENCE_LABEL)
        self.assertEqual([m for m in msgs if "falls back to 0" in m], [],
                         f"a re-judge that could have run was degraded: {msgs}")
        self.assertIn("plan-duel judge verdict",
                      (wd / "schema-seen.txt").read_text(encoding="utf-8"),
                      "the schema never reached the judge's argv, so the two tests "
                      "above are not comparing what they claim to")
        self.assertEqual(
            plan_duel.parse_score(
                (wd / f"judge-round-{plan_duel.MAX_ROUNDS}.md").read_text(
                    encoding="utf-8")), 9)


class ResumeWinnerParityTests(_ResumeHarness, unittest.TestCase):
    """The assertion that catches the failure the others describe.

    A duel resumed from an interruption must report the SAME winner and score trajectory
    as the same duel run start to finish. Everything else here is a mechanism test.
    """

    def _execute(self, wd, argument, **kw):
        msgs = []
        rc = plan_duel.execute(
            argument=argument, workdir_arg=str(wd) if argument != str(wd) else None,
            specs=self._specs(**kw), controller_name="Claude",
            participant_name="Codex", emit=msgs.append, timeout=60,
        )
        return rc, msgs

    def _summary_facts(self, wd):
        """The three things a reader acts on: who won, why it stopped, and the scores.

        Paths are deliberately excluded — the two runs live in different directories, and
        comparing those would fail on a difference that means nothing.
        """
        text = (wd / "summary.md").read_text(encoding="utf-8")
        winner = re.search(r"\*\*Winner:\*\*\s*(\S+)", text)
        stopped = re.search(r"\*\*Stopped due to:\*\*\s*(.+)", text)
        rounds = re.search(r"\*\*Rounds run:\*\*\s*(\d+)", text)
        # The trajectory table: `| N | score | ... |`, score `—` for round 0.
        scores = re.findall(r"^\|\s*(\d+)\s*\|\s*(\S+)\s*\|", text, re.MULTILINE)
        return (winner.group(1) if winner else None,
                stopped.group(1).strip() if stopped else None,
                rounds.group(1) if rounds else None,
                scores)

    def _parity(self, score, preferred, interrupt_at):
        """Run to completion; then run the same duel resumed from `interrupt_at`."""
        whole = self._tmpdir() / "whole"
        rc, _ = self._execute(whole, "Design a thing that does a thing.",
                              score=score, preferred=preferred)
        self.assertEqual(rc, 0)

        resumed = self._tmpdir() / "resumed"
        shutil.copytree(whole, resumed)
        # Interrupt: drop the summary, the round that was in flight, and the judge file
        # of the round before it — the two states this phase is about, together.
        (resumed / "summary.md").unlink()
        for path in resumed.glob(f"*-round-{interrupt_at}.md"):
            path.unlink()
        (resumed / f"judge-round-{interrupt_at - 1}.md").unlink()
        rc, _ = self._execute(resumed, str(resumed), score=score, preferred=preferred)
        self.assertEqual(rc, 0)

        self.assertEqual(self._summary_facts(whole), self._summary_facts(resumed))

    def test_a_replay_exit_publishes_the_plans_of_the_round_that_stopped(self):
        """Rounds past the exit can exist.

        The replay stops at the round that actually ended the duel, but `apply_resume` had
        already restored the live plans from the workdir's NEWEST round. Publishing those
        would hand over a later round's plans under a summary naming an earlier one.
        """
        wd = self._seed(self._tmpdir() / "wd", 3, score=9)
        # A fourth round that should never have run, with distinguishable content.
        for side in ("a", "b"):
            (wd / plan_duel.plan_snapshot_name(side, 4)).write_text(
                f"round four {side}" + "x" * 400, encoding="utf-8")
            (wd / f"plan-{side}.md").write_text(
                f"round four {side}" + "x" * 400, encoding="utf-8")
        (wd / "judge-round-4.md").write_text("SCORE: 9\n\nPREFERRED: A\n", encoding="utf-8")
        rounds_run, stop, msgs = self._run(wd, 5)
        self.assertEqual((rounds_run, stop), (3, plan_duel.CONVERGENCE_LABEL))
        for side in ("a", "b"):
            self.assertNotIn("round four", (wd / f"plan-{side}.md").read_text(
                encoding="utf-8"), f"plan-{side}.md still holds round 4's content")
        self.assertTrue([m for m in msgs if "already stopped" in m],
                        f"the discarded rounds must be announced: {msgs}")

    def test_winner_parity_on_a_converged_duel(self):
        self._parity(score=9, preferred="A", interrupt_at=3)

    def test_winner_parity_on_a_stagnated_duel(self):
        self._parity(score=5, preferred="B", interrupt_at=4)




class ResumeOnlyTargetsADuelWorkdir(unittest.TestCase):
    """A resume DELETES; what it deletes must be a duel, not a directory that resembles one.

    `problem.md` was the whole test, and it is an ordinary filename. Pointed at a notes
    directory holding one beside its own `plan-*.md` drafts, the resume lost three real
    files: the cleanup globs (`plan-*.md`, `judge-*.md`, `rejections-*.md`,
    `participant-*`, `progress.log`) match an ordinary working directory. The `--workdir`
    route was closed earlier; the positional route was still open.
    """

    def _dir(self, root, name, files):
        d = Path(root) / name
        d.mkdir()
        for filename in files:
            (d / filename).write_text("x\n", encoding="utf-8")
        return d

    def test_a_directory_that_merely_looks_like_one_is_not_a_duel_workdir(self):
        with tempfile.TemporaryDirectory() as td:
            notes = self._dir(td, "notes", (
                "problem.md", "plan-outline.md", "judge-notes.md", "progress.log"))
            self.assertFalse(
                plan_duel._looks_like_duel_workdir(notes),
                "a notes directory holding problem.md was taken for a duel workdir, and a "
                "resume would delete plan-outline.md, judge-notes.md and progress.log")

    def test_problem_md_alone_is_not_enough(self):
        with tempfile.TemporaryDirectory() as td:
            self.assertFalse(
                plan_duel._looks_like_duel_workdir(self._dir(td, "bare", ("problem.md",))))

    def test_a_workdir_predating_the_marker_still_resumes(self):
        """Backward compatibility is the reason this is not just a marker check.

        `participant-prompt-0.txt` is what a round 0 killed at Agent A leaves behind, and
        it is the shape of evidence this branch accepts: a name the engine alone emits.
        `('problem.md', 'plan-a.md')` is the same pair a person keeping two draft plans
        has, and which the engine's own reset deletes.
        """
        with tempfile.TemporaryDirectory() as td:
            legacy = self._dir(td, "legacy", (
                "problem.md", "plan-a.md", "participant-prompt-0.txt"))
            self.assertTrue(plan_duel._looks_like_duel_workdir(legacy))

    def test_hand_written_plan_drafts_are_not_evidence_of_a_duel(self):
        """The exact directory the predicate exists to protect.

        `plan-a.md` / `plan-b.md` are ordinary filenames AND on the init-incomplete reset's
        deletion list, so counting them as proof took a resume from "someone keeps two
        drafts beside a problem statement" to "both were unlinked and replaced with
        generated round-0 plans".
        """
        with tempfile.TemporaryDirectory() as td:
            notes = self._dir(td, "redesign", ("problem.md", "plan-a.md", "plan-b.md"))
            self.assertFalse(plan_duel._looks_like_duel_workdir(notes))

    def test_the_marker_alone_is_enough(self):
        with tempfile.TemporaryDirectory() as td:
            marked = self._dir(td, "marked", ("problem.md", plan_duel.DUEL_MARKER_FILENAME))
            self.assertTrue(plan_duel._looks_like_duel_workdir(marked))

    def test_claiming_a_workdir_writes_the_marker(self):
        with tempfile.TemporaryDirectory() as td:
            fresh = Path(td) / "fresh"
            fresh.mkdir()
            self.assertTrue(plan_duel._claim_problem_md(fresh, "solve it"))
            self.assertTrue((fresh / plan_duel.DUEL_MARKER_FILENAME).is_file())

    def test_a_failed_marker_write_does_not_fail_the_claim(self):
        """The claim is what reserves the workdir; a read-only filesystem must not undo it."""
        with tempfile.TemporaryDirectory() as td:
            fresh = Path(td) / "ro"
            fresh.mkdir()
            real = Path.write_bytes

            def boom(self_path, data):
                if self_path.name == plan_duel.DUEL_MARKER_FILENAME:
                    raise OSError("read-only file system")
                return real(self_path, data)

            with unittest.mock.patch.object(Path, "write_bytes", boom):
                self.assertTrue(plan_duel._claim_problem_md(fresh, "solve it"))
            self.assertTrue((fresh / "problem.md").is_file())




class NegativeScoresDoNotConverge(unittest.TestCase):
    """The sign was dropped, so the worst score the rubric can express read as the best.

    `SCORE: -10` matched `\\d+` as `10`, which clears convergence_exit's `>= 8` and ends the
    duel at round 3. It also made the two score paths contradict each other: a JSON `-10`
    reached the isinstance(int) branch and was rejected as out-of-range, while the string
    `"-10"` came back as 10 and converged.
    """

    def test_a_negative_marker_score_is_not_read_as_positive(self):
        self.assertEqual(plan_duel._marker_score("SCORE: -10"), -10)
        self.assertEqual(plan_duel._marker_score("SCORE: 8"), 8)

    def test_a_negative_score_is_rejected_rather_than_converging(self):
        """Out of range, so it takes the unparseable path: 0, warned, duel continues."""
        self.assertIsNone(plan_duel._usable_score(plan_duel._marker_score("SCORE: -10")))
        self.assertEqual(plan_duel._usable_score(plan_duel._marker_score("SCORE: 8")), 8)

    def test_the_json_and_string_paths_now_agree(self):
        for raw, expected in ((-10, -10), ("-10", -10), (10, 10), ("10", 10)):
            with self.subTest(raw=raw):
                self.assertEqual(plan_duel._json_score({"score": raw}), expected)


class UnimplementedPromptModesAreRefused(unittest.TestCase):
    """A knob that was validated, stored on the spec, and then read by nothing.

    An adapter declaring `stdin` ran its CLI with no prompt at all, and failed somewhere
    pointing nowhere near the config responsible. Refusing a mode that does nothing is
    honest; accepting it is not.
    """

    def _config(self, **role_overrides):
        base = _valid_config()
        for role, extra in role_overrides.items():
            base[role] = {**base[role], **extra}
        return base

    def test_stdin_and_file_are_refused_by_name(self):
        for mode in ("stdin", "file"):
            with self.subTest(mode=mode):
                with self.assertRaises(plan_duel.AdapterConfigError) as caught:
                    plan_duel.parse_adapter_config(
                        self._config(agent_a={"prompt_mode": mode}))
                self.assertIn(mode, str(caught.exception))
                self.assertIn("argv", str(caught.exception))

    def test_arg_is_still_accepted_and_is_the_default(self):
        specs = plan_duel.parse_adapter_config(self._config(agent_a={"prompt_mode": "arg"}))
        self.assertEqual(specs["agent_a"].prompt_mode, "arg")
        specs = plan_duel.parse_adapter_config(_valid_config())
        self.assertEqual(specs["agent_a"].prompt_mode, "arg")

    def test_an_unhashable_prompt_mode_still_raises_a_clean_config_error(self):
        """Anti-regression: `x in frozenset` on a list raises TypeError, not our error."""
        with self.assertRaises(plan_duel.AdapterConfigError):
            plan_duel.parse_adapter_config(self._config(agent_a={"prompt_mode": ["arg"]}))




class WritesDoNotFollowSymlinks(unittest.TestCase):
    """The workdir is writable by the agents this engine dispatches.

    `_agent_output_is_usable` refuses to READ through a planted link, with lstat + S_ISREG.
    The write side used Path.write_bytes and a plain open, both of which follow one — so an
    agent could plant `summary.md` as a link outside the workdir and have the engine
    overwrite it, straight through the boundary the adapters' read-only flags advertise.
    """

    @unittest.skipUnless(os.name == "posix", "symlink creation differs on Windows; the "
                                             "O_NOFOLLOW path this guards is POSIX")
    def test_a_planted_link_does_not_redirect_a_write_outside_the_workdir(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            outside = root / "outside.txt"
            outside.write_text("PRECIOUS\n", encoding="utf-8")
            workdir = root / "duel"
            workdir.mkdir()
            (workdir / "summary.md").symlink_to(outside)
            with self.assertRaises(plan_duel.PlanDuelError):
                plan_duel.write_text_utf8(workdir / "summary.md", "duel output\n")
            self.assertEqual(outside.read_text(encoding="utf-8"), "PRECIOUS\n",
                             "the write followed the link and clobbered a file outside "
                             "the workdir")

    def test_an_ordinary_write_is_unaffected(self):
        """Anti-vacuity: refusing everything would satisfy the test above."""
        with tempfile.TemporaryDirectory() as td:
            target = Path(td) / "plan-a.md"
            plan_duel.write_text_utf8(target, "hello\n")
            self.assertEqual(target.read_text(encoding="utf-8"), "hello\n")

    def test_the_newline_convention_still_holds(self):
        with tempfile.TemporaryDirectory() as td:
            target = Path(td) / "crlf.md"
            plan_duel.write_text_utf8(target, "a\r\nb\rc\n")
            self.assertEqual(target.read_bytes(), b"a\nb\nc\n")

    # The two workdir files the guard did not cover. `write_text_utf8` — the only caller
    # of `open_no_follow` — is called by no production path, and the append branch had no
    # call site at all, so every assertion above tested a function the engine never runs.

    @unittest.skipUnless(os.name == "posix", "symlink creation differs on Windows")
    def test_save_state_does_not_write_through_a_planted_state_json_link(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            outside = root / "outside.txt"
            outside.write_text("PRECIOUS\n", encoding="utf-8")
            workdir = root / "duel"
            workdir.mkdir()
            (workdir / plan_duel.STATE_FILENAME).symlink_to(outside)
            plan_duel.save_state(workdir, plan_duel.RunState("Claude", "Codex"))
            self.assertEqual(outside.read_text(encoding="utf-8"), "PRECIOUS\n",
                             "save_state followed the link and clobbered a file "
                             "outside the workdir")
            self.assertFalse((workdir / plan_duel.STATE_FILENAME).is_symlink())

    @unittest.skipUnless(os.name == "posix", "symlink creation differs on Windows")
    def test_append_progress_refuses_a_planted_progress_log_link(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            outside = root / "outside.log"
            outside.write_text("PRECIOUS\n", encoding="utf-8")
            workdir = root / "duel"
            workdir.mkdir()
            (workdir / plan_duel.PROGRESS_LOG_NAME).symlink_to(outside)
            with self.assertRaises(plan_duel.PlanDuelError):
                plan_duel.append_progress(
                    workdir / plan_duel.PROGRESS_LOG_NAME, "[+00:00] round 1\n")
            self.assertEqual(outside.read_text(encoding="utf-8"), "PRECIOUS\n",
                             "the append followed the link and grew a file outside "
                             "the workdir")

    @unittest.skipUnless(os.name == "posix", "symlink creation differs on Windows")
    def test_the_winner_stamp_is_not_written_through_a_planted_link(self):
        """The third write the workdir exposes.

        `write_text_roundtrip` is the stamp's write-back, and it used `Path.write_bytes`,
        which follows a link. Normally `copy_bytes` has just `os.replace`d a fresh regular
        file over `plan-{slug}.md`, so there is nothing to follow — but a missing live plan
        is now a warned SKIP, so the stamp is reached with whatever was sitting at that
        path, including a link an earlier agent planted.
        """
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            outside = root / "outside.md"
            outside.write_text("PRECIOUS\n", encoding="utf-8")
            workdir = root / "duel"
            workdir.mkdir()
            (workdir / "plan-claude.md").symlink_to(outside)
            with self.assertRaises(plan_duel.PlanDuelError):
                plan_duel.write_text_roundtrip(
                    workdir / "plan-claude.md", "# Plan\n\n| Format | v2 |\n")
            self.assertEqual(outside.read_text(encoding="utf-8"), "PRECIOUS\n")

    def test_the_roundtrip_write_still_preserves_bytes_it_did_not_author(self):
        """Anti-vacuity, and the property the roundtrip exists for: no re-encoding."""
        with tempfile.TemporaryDirectory() as td:
            target = Path(td) / "plan-claude.md"
            # A cp1252 apostrophe: not valid UTF-8, and the round trip must return it
            # to disk untouched rather than as U+FFFD.
            target.write_bytes(b"This plan\x92s body.\r\n")
            plan_duel.write_text_roundtrip(
                target, plan_duel.read_text_roundtrip(target) + "tail\n")
            self.assertEqual(target.read_bytes(), b"This plan\x92s body.\ntail\n")

    def test_an_ordinary_append_still_accumulates(self):
        """Anti-vacuity: refusing every append would satisfy the test above."""
        with tempfile.TemporaryDirectory() as td:
            target = Path(td) / plan_duel.PROGRESS_LOG_NAME
            plan_duel.append_progress(target, "one\n")
            plan_duel.append_progress(target, "two\n")
            self.assertEqual(target.read_bytes(), b"one\ntwo\n")

    def test_an_appended_line_keeps_its_own_line_endings(self):
        """Append is byte-faithful: no newline translation, no re-encoding."""
        with tempfile.TemporaryDirectory() as td:
            target = Path(td) / plan_duel.PROGRESS_LOG_NAME
            plan_duel.append_progress(target, "a\r\nb\n")
            self.assertEqual(target.read_bytes(), b"a\r\nb\n")


class TheSymlinkGuardIsReachedFromTheEngineItself(_ResumeHarness, unittest.TestCase):
    """M19's real shape: the guard existed, was tested, and was wired to nothing.

    Every symlink assertion in the suite drove `write_text_utf8`, a function no production
    path calls. This drives a real critique round against the stub CLI with both
    `state.json` and `progress.log` planted as links out of the workdir, so what is
    asserted is the engine's own code path rather than a helper beside it.

    A refused activity write must still not abort the duel: `progress.log` is
    observation-only, and a duel that completed correctly must not be failed by a log line
    it could not append.
    """

    @unittest.skipUnless(os.name == "posix", "symlink creation differs on Windows")
    def test_a_real_round_writes_through_neither_planted_link(self):
        root = self._tmpdir()
        state_target = root / "outside-state.txt"
        state_target.write_text("PRECIOUS STATE\n", encoding="utf-8")
        log_target = root / "outside-progress.txt"
        log_target.write_text("PRECIOUS LOG\n", encoding="utf-8")

        wd = self._seed(root / "wd", 2, score=9)
        (wd / plan_duel.STATE_FILENAME).symlink_to(state_target)
        (wd / plan_duel.PROGRESS_LOG_NAME).symlink_to(log_target)

        rounds_run, stop, _ = self._run(wd, 3)

        self.assertEqual(rounds_run, 3)
        self.assertEqual(stop, plan_duel.CONVERGENCE_LABEL,
                         "a refused observation write aborted a correct duel")
        self.assertEqual(state_target.read_text(encoding="utf-8"), "PRECIOUS STATE\n",
                         "the engine's own save_state wrote through the planted link")
        self.assertEqual(log_target.read_text(encoding="utf-8"), "PRECIOUS LOG\n",
                         "the engine's own progress log wrote through the planted link")
        self.assertTrue((wd / f"judge-round-3.md").is_file(),
                        "the round did not actually run, so nothing was proven")

    @unittest.skipUnless(os.name == "posix", "symlink creation differs on Windows")
    def test_a_planted_per_round_progress_file_does_not_abort_the_round_either(self):
        """`_progress` writes two files; both must degrade, not halt."""
        root = self._tmpdir()
        outside = root / "outside-round.txt"
        outside.write_text("PRECIOUS\n", encoding="utf-8")
        wd = self._seed(root / "wd", 2, score=9)
        (wd / "participant-progress-3.md").symlink_to(outside)

        rounds_run, stop, _ = self._run(wd, 3)

        self.assertEqual(stop, plan_duel.CONVERGENCE_LABEL)
        self.assertEqual(rounds_run, 3)
        self.assertEqual(outside.read_text(encoding="utf-8"), "PRECIOUS\n")

    def test_an_unplanted_run_still_writes_both_files(self):
        """Anti-vacuity: an engine that wrote neither file would pass the two above."""
        wd = self._seed(self._tmpdir() / "wd", 2, score=9)
        self._run(wd, 3)
        self.assertTrue((wd / plan_duel.STATE_FILENAME).is_file())
        self.assertTrue((wd / plan_duel.PROGRESS_LOG_NAME).is_file())
        self.assertIn("round 3", (wd / plan_duel.PROGRESS_LOG_NAME).read_text(
            encoding="utf-8"))

    def test_a_real_round_reaches_both_guarded_writers(self):
        """The wiring itself, counted — "present" is what the guard already was.

        A behavioural assertion can be satisfied by an engine that stopped writing the file
        at all, so this records that one ordinary round actually enters `open_no_follow` in
        APPEND mode and `write_text_atomic`.
        """
        appends, atomics = [], []
        real_nofollow = plan_duel.open_no_follow
        real_atomic = plan_duel.write_text_atomic

        def counting_nofollow(path, data, *, append=False):
            (appends if append else atomics).append(Path(path).name)
            return real_nofollow(path, data, append=append)

        def counting_atomic(path, text):
            atomics.append(Path(path).name)
            return real_atomic(path, text)

        self.addCleanup(setattr, plan_duel, "open_no_follow", real_nofollow)
        self.addCleanup(setattr, plan_duel, "write_text_atomic", real_atomic)
        plan_duel.open_no_follow = counting_nofollow
        plan_duel.write_text_atomic = counting_atomic

        wd = self._seed(self._tmpdir() / "wd", 2, score=9)
        self._run(wd, 3)

        self.assertIn(plan_duel.PROGRESS_LOG_NAME, appends,
                      "open_no_follow's append branch still has no production caller")
        self.assertIn(plan_duel.STATE_FILENAME, atomics,
                      "save_state still writes state.json unguarded")


class RuntimeNamesMustNotCollapseToOneSlug(unittest.TestCase):
    """Both final plans are written as plan-{slug}.md.

    With one slug for two runtimes the second write lands on the first, one plan survives,
    and summary.md reports a winner and a loser as though both existed. Nothing downstream
    can notice: the survivor is a valid plan.
    """

    def test_names_differing_only_in_case_are_refused(self):
        with self.assertRaises(plan_duel.PlanDuelError) as caught:
            plan_duel.require_distinct_slugs("Claude", "claude")
        self.assertIn("same file slug", str(caught.exception))

    def test_identical_names_are_refused(self):
        with self.assertRaises(plan_duel.PlanDuelError):
            plan_duel.require_distinct_slugs("codex", "codex")

    def test_ordinary_pairs_are_allowed(self):
        # `("a", "b")` stays: it is role-ALIGNED, so each final copy is a self-copy and
        # nothing is overwritten. The class below measures that rather than assuming it.
        for a, b in (("claude", "codex"), ("Claude", "Codex"), ("gpt", "sonnet"),
                     ("a", "b")):
            with self.subTest(pair=(a, b)):
                plan_duel.require_distinct_slugs(a, b)


class ARuntimeSlugMustBeOneSafeFilenameComponent(_TempWorkdirMixin, unittest.TestCase):
    """The collision guard reads a slug as a name; nothing checked it was one.

    `slugify_name` lowercases and stops, so a runtime named `x/../../victim` produced
    `plan-x/../../victim.md` — neither `a`, `b`, nor `<a|b>-round-<n>`, so it walks past
    the collision check and lands OUTSIDE the workdir. A separator is enough on its own:
    `nested/claude` copies into a subdirectory, and the stamp then looks for it under the
    wrong name.

    The slug reaches the filesystem as `plan-{slug}.md` and every adapter's argv as a
    placeholder, so it is checked once, at startup, before either happens.
    """

    HOSTILE = {
        "nested/claude": "separator",
        "x/../../victim": "separator",
        "..": "traversal",
        ".": "traversal",
        "sub\\dir": "separator",
        "stream:2": "reserved character",   # NTFS alternate data stream
        "wild*card": "reserved character",
        "pipe|name": "reserved character",
        'quo"te': "reserved character",
        "q?mark": "reserved character",
        "less<than": "reserved character",
        "trailing ": "trailing space or dot",
        "trailing.": "trailing space or dot",
        " leading": "leading space",
        "": "empty",
        "bell\x07": "control character",
        "new\nline": "control character",
    }

    def test_every_hostile_shape_is_refused_on_either_side(self):
        for name in self.HOSTILE:
            with self.subTest(name=name):
                with self.assertRaises(plan_duel.PlanDuelError):
                    plan_duel.require_distinct_slugs(name, "Codex")
                with self.assertRaises(plan_duel.PlanDuelError):
                    plan_duel.require_distinct_slugs("Claude", name)

    def test_the_refusal_says_what_is_wrong_with_the_name(self):
        with self.assertRaises(plan_duel.PlanDuelError) as caught:
            plan_duel.require_distinct_slugs("x/../../victim", "Codex")
        message = str(caught.exception)
        self.assertIn("x/../../victim", message)
        self.assertIn("plan-", message)

    def test_ordinary_names_are_still_accepted(self):
        """Anti-vacuity, and it must not reject names people actually use."""
        for name in ("claude", "Codex", "GPT-5", "gpt_4o", "sonnet-4.5", "o3",
                     "my.reviewer", "клод", "モデル"):
            with self.subTest(name=name):
                plan_duel.require_distinct_slugs(name, "some-other-runtime")

    def test_the_cli_refuses_a_traversal_name_before_creating_anything(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            cfg = base / "adapter.json"
            cfg.write_text(json.dumps(_valid_config()), encoding="utf-8")
            buf, err = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(err):
                rc = plan_duel.main([
                    "Design the migration.",
                    "--workdir", str(base / "duel"),
                    "--adapter-config", str(cfg),
                    "--controller-name", "x/../../victim",
                    "--participant-name", "Codex",
                ])
            self.assertEqual(rc, 1)
            self.assertIn("x/../../victim", err.getvalue())
            self.assertEqual(list(base.iterdir()), [cfg],
                             "something was created before the refusal")

    def test_nothing_is_written_outside_a_workdir(self):
        """The consequence, driven rather than argued.

        Without the guard, `copy_bytes` puts its temp file two directories up. With it,
        the duel never starts, so the directory above the workdir is untouched.
        """
        base = self._tmpdir()
        workdir = base / "duel"
        workdir.mkdir()
        (workdir / "plan-a.md").write_text("THE CONTROLLER'S PLAN\n", encoding="utf-8")
        before = sorted(p.name for p in base.iterdir())
        with self.assertRaises(plan_duel.PlanDuelError):
            plan_duel.require_distinct_slugs("x/../../victim", "Codex")
        self.assertEqual(sorted(p.name for p in base.iterdir()), before)


class RuntimeNamesMustNotCollideWithTheEnginesOwnFiles(unittest.TestCase):
    """The other way a final plan can land on a file that already means something.

    `write_summary` does exactly two copies, in this order:

        copy_bytes(plan-a.md -> plan-{controller_slug}.md)
        copy_bytes(plan-b.md -> plan-{participant_slug}.md)

    so which slugs destroy something has a measured answer. A guard refusing any slug
    naming an engine file is too broad: it turns away `A`/`B`, where each copy is a
    SELF-copy. The rule is role-aware because the harm is:

    | controller | participant | destroyed          |
    |---|---|---|
    | A          | B           | nothing            |
    | A          | Codex       | nothing            |
    | Claude     | B           | nothing            |
    | B          | Codex       | plan-b.md — and copy 2 then reads the clobbered
    |            |             | file, so the PARTICIPANT'S PLAN IS LOST outright |
    | Claude     | A           | plan-a.md — the live plan A becomes B's plan     |
    | a-round-1  | Codex       | plan-a-round-1.md — a resume's frozen input      |

    So: controller `a` is fine and controller `b` is not; participant `b` is fine and
    participant `a` is not; a round-snapshot slug is never fine on either side.
    """

    LIVE_AND_SNAPSHOTS = {
        "plan-a.md": "LIVE-A", "plan-b.md": "LIVE-B",
        "plan-a-round-0.md": "SNAP-A0", "plan-b-round-0.md": "SNAP-B0",
        "plan-a-round-1.md": "SNAP-A1", "plan-b-round-1.md": "SNAP-B1",
    }

    def _destroyed_by(self, controller, participant):
        """Replay write_summary's two copies; return what stopped being itself."""
        workdir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, workdir, ignore_errors=True)
        for name, body in self.LIVE_AND_SNAPSHOTS.items():
            (workdir / name).write_text(body, encoding="utf-8")
        plan_duel.copy_bytes(
            workdir / "plan-a.md",
            workdir / f"plan-{plan_duel.slugify_name(controller)}.md")
        plan_duel.copy_bytes(
            workdir / "plan-b.md",
            workdir / f"plan-{plan_duel.slugify_name(participant)}.md")
        return sorted(
            name for name, body in self.LIVE_AND_SNAPSHOTS.items()
            if (workdir / name).read_text(encoding="utf-8") != body)

    DESTRUCTIVE = (("B", "Codex"), ("b", "Codex"), ("Claude", "A"), ("Claude", "a"),
                   ("B", "A"), ("a-round-1", "Codex"), ("Claude", "b-round-1"),
                   ("B-Round-0", "Codex"))
    SAFE = (("A", "B"), ("a", "b"), ("A", "Codex"), ("Claude", "B"),
            ("Claude", "Codex"))

    def test_every_pair_the_guard_refuses_really_does_destroy_a_file(self):
        """The guard is justified case by case, not by resemblance to a filename."""
        for controller, participant in self.DESTRUCTIVE:
            with self.subTest(pair=(controller, participant)):
                self.assertTrue(
                    self._destroyed_by(controller, participant),
                    "refused a pair that overwrites nothing")
                with self.assertRaises(plan_duel.PlanDuelError) as caught:
                    plan_duel.require_distinct_slugs(controller, participant)
                self.assertIn("plan-", str(caught.exception))

    def test_every_pair_the_guard_allows_really_does_destroy_nothing(self):
        """Including the role-ALIGNED pair, where both copies are self-copies.

        `A`/`B` overwrites nothing: `plan-a.md` is copied over itself, and so is
        `plan-b.md`. Refusing it turned away a legitimate naming for no benefit.
        """
        for controller, participant in self.SAFE:
            with self.subTest(pair=(controller, participant)):
                self.assertEqual(
                    self._destroyed_by(controller, participant), [],
                    "the premise is wrong: this pair DOES destroy something")
                plan_duel.require_distinct_slugs(controller, participant)

    def test_names_that_merely_resemble_one_are_still_allowed(self):
        """Anti-vacuity: the refusal is for exact filename collisions only."""
        for name in ("agent", "beta", "a-round", "a-round-x", "ab", "plan-a",
                     "a-round-3-x"):
            with self.subTest(name=name):
                plan_duel.require_distinct_slugs(name, "Codex")
                plan_duel.require_distinct_slugs("Claude", name)

    def test_the_refusal_names_the_file_it_would_have_overwritten(self):
        with self.assertRaises(plan_duel.PlanDuelError) as caught:
            plan_duel.require_distinct_slugs("B", "Codex")
        self.assertIn("plan-b.md", str(caught.exception))

    def test_a_role_aligned_duel_runs_end_to_end(self):
        """The reviewer's case, driven for real rather than argued.

        Controller `A`, participant `B`: both final plans land, both carry their own
        side's content, and only the winner is stamped.
        """
        workdir = Path(tempfile.mkdtemp()) / "wd"
        workdir.mkdir(parents=True)
        self.addCleanup(shutil.rmtree, workdir.parent, ignore_errors=True)
        (workdir / "problem.md").write_text("Problem.\n", encoding="utf-8")
        for side, body in (("a", "PLAN A BODY. " * 20), ("b", "PLAN B BODY. " * 20)):
            (workdir / f"plan-{side}.md").write_text(body, encoding="utf-8")
            for n in (0, 1):
                (workdir / plan_duel.plan_snapshot_name(side, n)).write_text(
                    body, encoding="utf-8")
        (workdir / "judge-round-1.md").write_text(
            "SCORE: 9\n\nPREFERRED: A\n", encoding="utf-8")

        plan_duel.require_distinct_slugs("A", "B")
        plan_duel.write_summary(
            workdir=workdir, rounds_run=1, stopped_due_to="Convergence",
            controller_name="A", participant_name="B", emit=lambda _m: None)

        winner = (workdir / "plan-a.md").read_text(encoding="utf-8")
        loser = (workdir / "plan-b.md").read_text(encoding="utf-8")
        self.assertIn("PLAN A BODY.", winner)
        self.assertIn("PLAN B BODY.", loser)
        self.assertIn("| Format | v2 |", winner)
        self.assertNotIn("| Format | v2 |", loser)
        # The round snapshots are untouched, so a resume still reads real inputs.
        self.assertIn("PLAN A BODY.",
                      (workdir / "plan-a-round-1.md").read_text(encoding="utf-8"))

    def test_the_cli_refuses_before_it_creates_a_workdir(self):
        """Reached, not merely present — the lesson of the symlink guard beside it.

        `require_distinct_slugs` runs before the resume scan and before any workdir is
        created, so the refusal must cost nothing and leave nothing behind.
        """
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            cfg = base / "adapter.json"
            cfg.write_text(json.dumps(_valid_config()), encoding="utf-8")
            buf, err = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(err):
                rc = plan_duel.main([
                    "Design the migration.",
                    "--workdir", str(base / "duel"),
                    "--adapter-config", str(cfg),
                    "--controller-name", "B",
                    "--participant-name", "Codex",
                ])
            self.assertEqual(rc, 1)
            self.assertIn("plan-b.md", err.getvalue())
            self.assertFalse((base / "duel").exists(),
                             "a workdir was created before the refusal")




class PromptDegradationsAreLoudAndNeverLeakTheRubric(unittest.TestCase):
    """Three silent degradations in a tool whose next action costs money.

    A duel that produced nothing useful because the prompt was twelve words looked, from
    outside, like one whose agents were unhelpful — and billed the same. The third was
    worse than silent: a template missing the requested role's heading fell back to the
    WHOLE template, so a competing agent received the rubric it was about to be judged
    against.
    """

    def _ctx(self, tmp, template=None, name="round.md"):
        skill_dir = None
        if template is not None:
            skill_dir = Path(tmp) / "skilldir"
            skill_dir.mkdir(exist_ok=True)
            (skill_dir / name).write_text(template, encoding="utf-8")
        workdir = Path(tmp) / "wd"
        workdir.mkdir(exist_ok=True)
        return plan_duel.DuelContext(workdir, "claude", "codex", skill_dir)

    def _prompt(self, ctx, role, round_n):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            return ctx.prompt(role, round_n), err.getvalue()

    def test_a_missing_role_section_does_not_hand_over_the_judge_rubric(self):
        template = ("# Round\n\n## Judge\n\n"
                    "SCORING RUBRIC: award 10 when the plan is exhaustive.\n")
        with tempfile.TemporaryDirectory() as tmp:
            prompt, err = self._prompt(self._ctx(tmp, template), "agent_a", 1)
            self.assertNotIn("RUBRIC", prompt.upper(),
                             "a competing agent was handed the judge's scoring rubric")
            self.assertIn("placeholder prompt", err)

    def test_a_missing_skill_dir_says_so(self):
        with tempfile.TemporaryDirectory() as tmp:
            prompt, err = self._prompt(self._ctx(tmp), "agent_a", 1)
            self.assertIn("placeholder", prompt.lower() + err.lower())
            self.assertIn("--skill-dir", err)

    def test_a_missing_template_file_says_so(self):
        with tempfile.TemporaryDirectory() as tmp:
            ctx = self._ctx(tmp, "# Init\n", name="init.md")   # round.md absent
            _prompt, err = self._prompt(ctx, "agent_a", 1)
            self.assertIn("round.md", err)
            self.assertIn("missing", err)

    def test_the_warning_is_not_repeated_for_the_same_cause(self):
        with tempfile.TemporaryDirectory() as tmp:
            ctx = self._ctx(tmp)
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                ctx.prompt("agent_a", 1)
                ctx.prompt("agent_a", 1)
            self.assertEqual(err.getvalue().count("--skill-dir"), 1,
                             "a line repeated per role per round teaches a reader to skim")

    def test_a_well_formed_template_still_renders_normally(self):
        """Anti-vacuity: warning on everything would satisfy the tests above."""
        template = ("# Round\n\n## Agent A\n\nWrite the plan.\n\n"
                    "## Judge\n\nSCORING RUBRIC: secret.\n")
        with tempfile.TemporaryDirectory() as tmp:
            prompt, err = self._prompt(self._ctx(tmp, template), "agent_a", 1)
            self.assertIn("Write the plan.", prompt)
            self.assertNotIn("RUBRIC", prompt.upper())
            self.assertEqual(err, "")




class SummaryIsWrittenAtomically(unittest.TestCase):
    """summary.md's EXISTENCE is the completion authority, so it must never be partial.

    compute_resume answers complete=True the moment the file is there, prints it, and exits
    0 without reading it. A plain write is not atomic, so a crash partway left a truncated
    summary that every later resume handed over as the result of a run that never finished.
    """

    def test_a_failed_write_leaves_no_summary_at_all(self):
        with tempfile.TemporaryDirectory() as td:
            target = Path(td) / "summary.md"
            real = os.replace

            def fail(src, dst, *a, **kw):
                raise OSError("crash between temp file and rename")

            with unittest.mock.patch.object(os, "replace", fail):
                with self.assertRaises(OSError):
                    plan_duel.write_text_atomic(target, "# Summary\n" * 500)
            self.assertFalse(
                target.exists(),
                "a partial summary.md survived, and a resume would read it as a "
                "finished duel")
            self.assertEqual(
                [p.name for p in Path(td).iterdir()], [],
                "the temp file was left behind")
            self.assertIs(os.replace, real)

    def test_a_successful_write_lands_whole(self):
        with tempfile.TemporaryDirectory() as td:
            target = Path(td) / "summary.md"
            plan_duel.write_text_atomic(target, "a\r\nb\rc\n")
            self.assertEqual(target.read_bytes(), b"a\nb\nc\n")

    def test_it_replaces_a_symlink_rather_than_writing_through_it(self):
        if os.name != "posix":
            self.skipTest("symlink creation differs on Windows")
        with tempfile.TemporaryDirectory() as td:
            outside = Path(td) / "outside.txt"
            outside.write_text("PRECIOUS\n", encoding="utf-8")
            workdir = Path(td) / "wd"
            workdir.mkdir()
            link = workdir / "summary.md"
            link.symlink_to(outside)
            plan_duel.write_text_atomic(link, "duel output\n")
            self.assertEqual(outside.read_text(encoding="utf-8"), "PRECIOUS\n")
            self.assertFalse(link.is_symlink())
            self.assertEqual(link.read_text(encoding="utf-8"), "duel output\n")




class AnInterruptedJudgeIsDetectable(unittest.TestCase):
    """judge_needs_rerun documented a state that production never wrote.

    Its second re-run condition is "the file exists but state.json says that round's judge
    never completed". Every RoundState for a critique round was created AFTER the judge
    returned, with judge_completed=True, so the condition could not fire — while a judge
    killed partway through leaves an unmarked non-empty file, which reads as a complete
    verdict.
    """

    def test_with_no_state_at_all_a_present_verdict_is_still_left_alone(self):
        """Deliberate and unchanged: without state.json there is nothing to read.

        The engine cannot tell a truncated write from a genuinely unparseable verdict by
        content, and the conservative reading keeps what the judge said. This fix only
        makes the case where state.json IS present work.
        """
        with tempfile.TemporaryDirectory() as td:
            wd = Path(td)
            (wd / "judge-round-2.md").write_text("SCORE: 7 truncated mid-", encoding="utf-8")
            self.assertFalse(plan_duel.judge_needs_rerun(wd, 2, None))

    def test_a_started_but_uncompleted_judge_is_re_run(self):
        with tempfile.TemporaryDirectory() as td:
            wd = Path(td)
            (wd / "judge-round-2.md").write_text("SCORE: 7 truncated mid-", encoding="utf-8")
            state = plan_duel.RunState("claude", "codex")
            state.rounds[2] = plan_duel.RoundState(
                plans_snapshotted=True, judge_completed=False)
            self.assertTrue(plan_duel.judge_needs_rerun(wd, 2, state),
                            "an interrupted judge was trusted as a complete verdict")

    def test_a_completed_judge_is_left_alone_even_when_unparseable(self):
        """Unchanged, and the reason matters: that verdict is the real one."""
        with tempfile.TemporaryDirectory() as td:
            wd = Path(td)
            (wd / "judge-round-2.md").write_text("PREFERRED: B\n", encoding="utf-8")
            state = plan_duel.RunState("claude", "codex")
            state.rounds[2] = plan_duel.RoundState(
                plans_snapshotted=True, judge_completed=True)
            self.assertFalse(plan_duel.judge_needs_rerun(wd, 2, state))


class RoundContextNeverContradictsTheRoundNumber(unittest.TestCase):
    """An unreadable prior score fell through to round 1's sentence.

    So an agent at round 5 was told "This is the first critique round" while the rest of its
    prompt said round 5, and invited to discard four rounds of critique. A missing score is
    missing information, not a fresh start.
    """

    def test_a_later_round_with_no_readable_prior_score_says_which_round_it_is(self):
        with tempfile.TemporaryDirectory() as td:
            wd = Path(td)
            (wd / "judge-round-4.md").write_text("no score here\n", encoding="utf-8")
            text = plan_duel._round_context(wd, 5)
            self.assertIn("round 5", text)
            self.assertNotIn("first critique round", text)

    def test_round_one_still_says_first(self):
        with tempfile.TemporaryDirectory() as td:
            self.assertEqual(plan_duel._round_context(Path(td), 1),
                             "This is the first critique round.")

    def test_a_readable_prior_score_is_still_reported(self):
        with tempfile.TemporaryDirectory() as td:
            wd = Path(td)
            (wd / "judge-round-2.md").write_text("SCORE: 7\n", encoding="utf-8")
            self.assertIn("7/10", plan_duel._round_context(wd, 3))


class TheWindowsTimeoutEndsTheTreeNotJustTheShim(unittest.TestCase):
    """On Windows the thing we spawned is often a `.cmd`, and killing it leaves the CLI.

    `shutil.which` resolves an npm-installed runtime to a `.cmd` shim, honouring `PATHEXT`.
    `terminate()` reaches the shim and nothing under it, so the Node process kept running —
    still holding the inherited stdout pipe and still spending on the model — while the duel
    reported the spawn killed. `taskkill /F /T` ends the tree using only the standard library.

    Asserted on the ARGV the branch builds, so it runs on every platform: a test that
    skipped everywhere but Windows would be a test nobody has ever seen pass.
    """

    def _terminate_under(self, os_name):
        calls = []

        class Proc:
            pid = 4321

            def poll(self):
                return None

            def terminate(self):
                calls.append(("terminate",))

            def kill(self):
                calls.append(("kill",))

            def wait(self, timeout=None):
                raise subprocess.TimeoutExpired("x", timeout)

        with unittest.mock.patch.object(plan_duel.os, "name", os_name), \
             unittest.mock.patch.object(plan_duel.subprocess, "run",
                                        lambda *a, **k: calls.append(("run", list(a[0])))):
            plan_duel._terminate_child(Proc(), group_leader=False)
        return calls

    def test_windows_ends_the_process_tree_first(self):
        calls = self._terminate_under("nt")
        run = [c for c in calls if c[0] == "run"]
        self.assertTrue(run, f"no taskkill was attempted: {calls}")
        self.assertEqual(run[0][1][:4], ["taskkill", "/F", "/T", "/PID"],
                         "the Windows branch must end the TREE, not just the shim")
        self.assertIn("4321", run[0][1], "it must name the process it spawned")

    def test_posix_does_not_reach_for_taskkill(self):
        calls = self._terminate_under("posix")
        self.assertFalse([c for c in calls if c[0] == "run"],
                         "POSIX kills the process GROUP; taskkill is not a POSIX program")


if __name__ == "__main__":
    unittest.main()
