"""The shard runner, and the one property that makes sharding safe to use.

A split suite is only as trustworthy as the promise that nothing fell between the shards.
A test that lands on no shard does not fail — it is simply never run, and CI is green
because of it. That is the failure this file exists to make impossible, so the property is
asserted over the REAL discovery rather than over a fixture: a module added tomorrow is
covered by these assertions the moment it exists.
"""
import os
import re
import shutil
import subprocess
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import shard_tests  # noqa: E402

TOTALS = (1, 2, 3, 4, 7)


class EveryTestLandsOnExactlyOneShard(unittest.TestCase):
    """Total and disjoint, over this suite's own discovery."""

    @classmethod
    def setUpClass(cls):
        cls.grouped = shard_tests.classes(str(REPO_ROOT / "tests"), "test_*.py")
        cls.names = sorted(cls.grouped)

    def test_the_discovery_found_something_to_split(self):
        """Anti-vacuity. Every assertion below is satisfied by an empty suite."""
        self.assertGreater(len(self.names), 20, self.names[:5])

    def test_the_shards_are_a_partition_at_every_width(self):
        for total in TOTALS:
            with self.subTest(total=total):
                seen = []
                for index in range(1, total + 1):
                    seen.extend(shard_tests.shard_of(self.names, index, total))
                self.assertEqual(sorted(seen), self.names,
                                 "a class landed on no shard, or on two")
                self.assertEqual(len(seen), len(set(seen)), "a class ran twice")

    def test_every_test_case_is_carried_not_only_every_class(self):
        """The partition is over CLASSES; what has to be total is TESTS. A grouping that
        dropped a case would still split its classes cleanly."""
        total_cases = sum(s.countTestCases() for s in self.grouped.values())
        loaded = unittest.TestLoader().discover(str(REPO_ROOT / "tests"), "test_*.py")
        self.assertEqual(total_cases, loaded.countTestCases(),
                         "grouping by class lost or duplicated test cases")

    def test_the_split_does_not_move_between_runs(self):
        """Deterministic, so a failure re-runs on the shard that produced it."""
        for total in TOTALS:
            with self.subTest(total=total):
                once = [shard_tests.shard_of(self.names, i, total) for i in range(1, total + 1)]
                again = [shard_tests.shard_of(list(reversed(self.names)), i, total)
                         for i in range(1, total + 1)]
                self.assertEqual(once, again, "the split follows discovery order")

    def test_the_widths_are_balanced_to_within_one_class(self):
        """Round-robin over a sorted list, so no shard can be handed two more classes than
        another — which is what stops one runner setting the wall clock alone."""
        for total in TOTALS:
            with self.subTest(total=total):
                sizes = [len(shard_tests.shard_of(self.names, i, total))
                         for i in range(1, total + 1)]
                self.assertLessEqual(max(sizes) - min(sizes), 1, sizes)


class TheRunnerRefusesAnImpossibleShard(unittest.TestCase):
    """An out-of-range index is a workflow that will silently run nothing, or run one
    shard's tests twice while another's never run. It exits non-zero instead."""

    def run_it(self, *args, env=None):
        """``env`` is merged over this process's own, which is how a case here reaches a
        console encoding this one does not have."""
        return subprocess.run(
            [sys.executable, str(REPO_ROOT / "scripts" / "shard_tests.py"), *args],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            cwd=str(REPO_ROOT), env={**os.environ, **env} if env else None,
        )

    def test_an_index_past_the_total_is_refused(self):
        done = self.run_it("--index", "4", "--total", "3")
        self.assertEqual(done.returncode, 2, done.stdout + done.stderr)
        self.assertIn("--index", done.stderr)

    def test_a_zero_index_is_refused(self):
        self.assertEqual(self.run_it("--index", "0", "--total", "3").returncode, 2)

    def test_a_real_shard_runs_and_says_what_it_took(self):
        done = self.run_it("--index", "1", "--total", "3",
                           "--pattern", "test_plan_tracker.py", "--verbosity", "0")
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertRegex(done.stdout, r"shard 1/3: \d+ of \d+ classes, \d+ tests")


    def test_one_shard_or_all_of_them_and_never_both(self):
        """Refused rather than resolved by preferring one. `--index 2 --parallel` asks for
        two different runs, and answering either is answering a question nobody asked."""
        both = self.run_it("--index", "2", "--total", "3", "--parallel")
        self.assertEqual(both.returncode, 2, both.stdout + both.stderr)
        self.assertIn("takes no --index", both.stderr)
        neither = self.run_it("--total", "3")
        self.assertEqual(neither.returncode, 2, neither.stdout + neither.stderr)
        self.assertIn("--parallel for all of them", neither.stderr)

    def test_parallel_runs_every_shard_and_reports_each(self):
        done = self.run_it("--total", "3", "--parallel",
                           "--pattern", "test_plan_tracker.py", "--verbosity", "0")
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        for index in (1, 2, 3):
            self.assertIn(f"===== shard {index}/3 =====", done.stdout)
            self.assertRegex(done.stdout, rf"shard {index}/3: \d+ of \d+ classes")
        self.assertIn("3 shards: 3 passed", done.stdout)

    def test_a_failing_shard_fails_the_whole_parallel_run(self):
        """A runner that reported its own success while a child failed would turn a red
        suite green, which is the one thing a test runner may never do."""
        done = self.run_it("--total", "2", "--parallel", "--start", "nowhere-at-all",
                           "--verbosity", "0")
        self.assertNotEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertIn("FAILED", done.stdout + done.stderr)

    def test_the_children_write_no_bytecode_beside_the_skills(self):
        """What makes running them at once safe rather than only faster. Concurrent
        interpreters race to write the same `__pycache__`, and the projection tests then
        find bytecode under `skills/` that the manifest does not classify — a suite that
        passes alone failing in company."""
        for stale in (REPO_ROOT / "skills").rglob("__pycache__"):
            shutil.rmtree(stale, ignore_errors=True)
        done = self.run_it("--total", "2", "--parallel",
                           "--pattern", "test_plan_tracker.py", "--verbosity", "0")
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertEqual(list((REPO_ROOT / "skills").rglob("__pycache__")), [])

    def test_it_relays_a_child_s_output_on_a_console_that_cannot_spell_it(self):
        """A shard's own output carries em dashes and `--parallel` writes the children's
        output through this process. Under a C locale that raised UnicodeEncodeError after
        every shard had already passed — a green suite reported as a crash."""
        done = self.run_it("--total", "2", "--parallel",
                           "--pattern", "test_plan_tracker.py", "--verbosity", "0",
                           env={"PYTHONUTF8": "0", "LC_ALL": "C", "LANG": "C"})
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertNotIn("UnicodeEncodeError", done.stdout + done.stderr)


class TheWorkflowAndTheRunnerAgreeOnTheWidth(unittest.TestCase):
    """The shard count is written twice in the workflow — once as the matrix, once as the
    `--total` handed to this runner — and the two going out of step is silent both ways.
    Raise the matrix to five and leave `--total 4`, and the fifth job runs a shard that does
    not exist while the suite is still split four ways; lower the matrix and leave `--total`
    alone, and a quarter of the tests are assigned to a job nobody starts. Green either way.

    Parsed with a regular expression rather than a YAML reader, because this repository is
    stdlib-only and the two lines being compared are simple enough to read literally.
    """

    @classmethod
    def setUpClass(cls):
        cls.text = (REPO_ROOT / ".github" / "workflows" / "validate.yml").read_text(
            encoding="utf-8")

    def test_every_matrix_is_the_width_its_runner_is_told(self):
        widths = [len(found.split(",")) for found
                  in re.findall(r"^\s*shard: \[([^\]]+)\]\s*$", self.text, re.MULTILINE)]
        totals = [int(found) for found in re.findall(r"--total (\d+)", self.text)]
        self.assertTrue(widths, "no shard matrix found; the parse, not the workflow, broke")
        self.assertEqual(len(widths), len(totals),
                         "a sharded job is missing its matrix or its --total")
        self.assertEqual(set(widths), set(totals),
                         f"matrix widths {widths} against --total {totals}")

    def test_the_width_is_one_the_partition_is_asserted_at(self):
        """A width this file never exercises is a width nothing has checked is total."""
        width = len(re.search(r"^\s*shard: \[([^\]]+)\]\s*$",
                              self.text, re.MULTILINE).group(1).split(","))
        self.assertIn(width, TOTALS, f"CI runs {width} shards; TOTALS is {TOTALS}")

if __name__ == "__main__":
    unittest.main()
