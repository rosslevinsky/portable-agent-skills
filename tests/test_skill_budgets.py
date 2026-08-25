#!/usr/bin/env python3
"""A recorded word budget per skill, held EQUAL to what that skill measures today.

The prose a runtime reads is the product's real cost, and it regrows a paragraph at a time —
no single addition ever looks like the problem. A ratchet makes the aggregate visible at the
moment it moves: growth past the recorded number fails, and a deliberate growth requires
raising it in the same change, where a reviewer sees the two together.

`check_skill_budgets` is the CEILING, and fails in one direction only. The FLOOR is a test
here — `test_every_recorded_budget_equals_its_measured_count` — because slack satisfies a
ceiling, so a raise nobody reviewed disarms it silently. Together they hold the number at the
measured count, which means **shrinking prose means lowering the number too**.

**What is counted, and why not everything.** Every skill-root `*.md` plus
`references/**/*.md` — the prose a runtime reads while it works. Three exclusions share one
argument: the ratchet governs prose that accretes, not artifacts sized by what they do. A
`DECISIONS.md` is read by whoever is deciding whether to change a rule, so counting it would
score a relocation out of `SKILL.md` as zero reduction and budget-lock the ledgers. A bundled
engine at the skill root is executed, not read. And non-markdown under `references/` would
mean adding a schema field needed a budget raise.

`SKILL.md` alone was a fourth, unstated exclusion, and it made the ratchet avoidable:
plan-duel's `init.md`, `round.md` and `summary.md` are read by the engine every round and
were unbudgeted, so moving the last 45% of `SKILL.md` into `round.md` relocated 1044 words,
removed none, and passed.

**The v1 suite is excluded** and holds no budget. It is the superseded suite, on bugfix-only
support: a new check would put its files under a rule they were never written to.
"""
import json
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from validate_cross_runtime import (  # noqa: E402
    V1_SUITE_SKILLS,
    check_skill_budgets,
    iter_skill_roots,
    measure_skill_words,
    validate_skills,
)

SKILLS = REPO_ROOT / "skills"
BUDGETS = REPO_ROOT / "scripts" / "skill-budgets.json"


def _skill(root: Path, name: str, body: str = "", *, reference: str = "", ledger: str = ""):
    """Materialise a skill directory under `root`."""
    d = root / name
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text(body or "x " * 10, encoding="utf-8")
    if reference:
        (d / "references").mkdir()
        (d / "references" / "r.md").write_text(reference, encoding="utf-8")
    if ledger:
        (d / "DECISIONS.md").write_text(ledger, encoding="utf-8")
    return d


class MeasureTests(unittest.TestCase):
    def test_counts_skill_md_and_references_but_not_the_ledger(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = _skill(Path(tmp), "demo", "a b c", reference="d e", ledger="f g h i j k")
            self.assertEqual(measure_skill_words(d), 5)

    def test_counts_a_skill_root_companion_markdown_file(self):
        """The exclusion that was never stated, and made the ratchet avoidable.

        A companion beside `SKILL.md` — plan-duel's `init.md` / `round.md` /
        `summary.md`, which the engine resolves by name and feeds the models every
        round — went uncounted, so prose moved into one read as a reduction.
        """
        with tempfile.TemporaryDirectory() as tmp:
            d = _skill(Path(tmp), "demo", "a b c")
            (d / "round.md").write_text("d e f g", encoding="utf-8")
            self.assertEqual(measure_skill_words(d), 7)

    def test_relocating_prose_into_a_companion_is_not_a_reduction(self):
        """The evasion itself, asserted end to end rather than by its parts."""
        with tempfile.TemporaryDirectory() as tmp:
            d = _skill(Path(tmp), "demo", "one two three four five six")
            before = measure_skill_words(d)
            (d / "SKILL.md").write_text("one two three", encoding="utf-8")
            (d / "round.md").write_text("four five six", encoding="utf-8")
            self.assertEqual(measure_skill_words(d), before)

    def test_non_markdown_under_references_is_not_counted(self):
        """A schema and a shell script do not grow by accretion, so they are not budgeted."""
        with tempfile.TemporaryDirectory() as tmp:
            d = _skill(Path(tmp), "demo", "a b c", reference="d e")
            (d / "references" / "schema.json").write_text(
                '{"a": "' + " ".join(f"w{i}" for i in range(200)) + '"}', encoding="utf-8"
            )
            (d / "references" / "helper.sh").write_text(
                "# " + " ".join(f"w{i}" for i in range(200)), encoding="utf-8"
            )
            self.assertEqual(measure_skill_words(d), 5)

    def test_a_skill_with_no_references_directory_is_just_its_body(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = _skill(Path(tmp), "demo", "a b c d")
            self.assertEqual(measure_skill_words(d), 4)


class RatchetDirectionTests(unittest.TestCase):
    """The three directions, which are the whole contract."""

    def _run(self, words: int, budget: int) -> list[str]:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            skills = root / "skills"
            _skill(skills, "demo", " ".join(f"w{i}" for i in range(words)))
            budgets = root / "budgets.json"
            budgets.write_text(json.dumps({"demo": budget}), encoding="utf-8")
            return check_skill_budgets(skills, budgets)

    def test_growth_past_the_recorded_budget_fails(self):
        errors = self._run(words=120, budget=100)
        self.assertEqual(len(errors), 1, errors)
        self.assertIn("demo", errors[0])
        self.assertIn("120", errors[0])
        self.assertIn("100", errors[0])

    def test_the_same_growth_passes_when_the_budget_is_raised_with_it(self):
        self.assertEqual(self._run(words=120, budget=120), [])

    def test_shrinkage_always_passes(self):
        self.assertEqual(self._run(words=40, budget=100), [])

    def test_landing_exactly_on_the_budget_passes(self):
        self.assertEqual(self._run(words=100, budget=100), [])


class ExemptionTests(unittest.TestCase):
    def test_growth_confined_to_a_ledger_never_fails(self):
        """The exemption that makes relocation worth doing."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            skills = root / "skills"
            _skill(skills, "demo", "a b c", ledger=" ".join(f"w{i}" for i in range(5000)))
            budgets = root / "budgets.json"
            budgets.write_text(json.dumps({"demo": 3}), encoding="utf-8")
            self.assertEqual(check_skill_budgets(skills, budgets), [])

    def test_a_v1_skill_is_not_checked_even_when_it_has_no_budget(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            skills = root / "skills"
            _skill(skills, "plan-run-v1", " ".join(f"w{i}" for i in range(9000)))
            _skill(skills, "demo", "a b c")
            budgets = root / "budgets.json"
            budgets.write_text(json.dumps({"demo": 3}), encoding="utf-8")
            self.assertEqual(check_skill_budgets(skills, budgets), [])


class CoverageTests(unittest.TestCase):
    """A budget file that silently omits a skill is a ratchet with a hole in it."""

    def test_a_non_v1_skill_without_a_budget_is_an_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            skills = root / "skills"
            _skill(skills, "demo", "a b c")
            _skill(skills, "unbudgeted", "a b c")
            budgets = root / "budgets.json"
            budgets.write_text(json.dumps({"demo": 3}), encoding="utf-8")
            errors = check_skill_budgets(skills, budgets)
            self.assertEqual(len(errors), 1, errors)
            self.assertIn("unbudgeted", errors[0])

    def test_a_budget_for_a_skill_that_no_longer_exists_is_an_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            skills = root / "skills"
            _skill(skills, "demo", "a b c")
            budgets = root / "budgets.json"
            budgets.write_text(json.dumps({"demo": 3, "deleted": 100}), encoding="utf-8")
            errors = check_skill_budgets(skills, budgets)
            self.assertEqual(len(errors), 1, errors)
            self.assertIn("deleted", errors[0])

    def test_a_v1_skill_may_not_carry_a_budget(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            skills = root / "skills"
            _skill(skills, "demo", "a b c")
            _skill(skills, "plan-run-v1", "a b c")
            budgets = root / "budgets.json"
            budgets.write_text(json.dumps({"demo": 3, "plan-run-v1": 3}), encoding="utf-8")
            errors = check_skill_budgets(skills, budgets)
            self.assertEqual(len(errors), 1, errors)
            self.assertIn("plan-run-v1", errors[0])


class FailClosedTests(unittest.TestCase):
    """Every way the budgets file can be wrong must produce an error, never silence.

    The ratchet reads one datum. If a missing, corrupt, or mistyped budgets file resolved to
    "no errors", the check would report success precisely when it had nothing to check —
    which is worse than not having it, because the green run is evidence of nothing.
    """

    def _check(self, write) -> list[str]:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            skills = root / "skills"
            _skill(skills, "demo", "a b c")
            budgets = root / "budgets.json"
            write(budgets)
            return check_skill_budgets(skills, budgets)

    def test_a_missing_budgets_file_is_an_error(self):
        errors = self._check(lambda p: None)
        self.assertEqual(len(errors), 1, errors)
        self.assertIn("missing", errors[0])

    def test_unparseable_json_is_an_error(self):
        errors = self._check(lambda p: p.write_text("{not json", encoding="utf-8"))
        self.assertEqual(len(errors), 1, errors)
        self.assertIn("not parseable JSON", errors[0])

    def test_invalid_utf8_is_an_error_rather_than_a_lossy_decode(self):
        """Regression: a lossy reader here reinterpreted a corrupt file as valid."""
        errors = self._check(lambda p: p.write_bytes(b'{"demo": 3, "x": "\xff"}'))
        self.assertEqual(len(errors), 1, errors)
        self.assertIn("not valid UTF-8", errors[0])

    def test_a_json_array_is_an_error(self):
        errors = self._check(lambda p: p.write_text('["demo"]', encoding="utf-8"))
        self.assertEqual(len(errors), 1, errors)
        self.assertIn("expected a JSON object", errors[0])

    def test_a_non_integer_budget_is_an_error(self):
        for bad in ('"120"', "12.5", "true"):
            with self.subTest(budget=bad):
                errors = self._check(
                    lambda p, b=bad: p.write_text('{"demo": ' + b + "}", encoding="utf-8")
                )
                self.assertEqual(len(errors), 1, errors)
                self.assertIn("must be an integer", errors[0])


class WiringTests(unittest.TestCase):
    """The check must be REACHED. Every other test here passes if the call is deleted."""

    def test_validate_skills_actually_surfaces_a_budget_breach(self):
        """Integration, not syntax: drive the real entry point and look for the real error.

        The AST assertion below proves only that the call is *written*. It would stay green
        if the call were moved into an unused nested function, put behind an unreachable
        branch, or shadowed locally by a no-op — so the binding and the reachability are
        checked here, by running `validate_skills` against a tree whose one skill is over
        budget and requiring the breach to come back in its errors.

        Containment, not equality: the fixture tree has no README or PORTABILITY.md, so
        other checks legitimately fire too. Only the budget breach is this test's business.
        """
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            skills = root / "skills"
            _skill(skills, "demo", " ".join(f"w{i}" for i in range(120)))
            (root / "scripts").mkdir()
            (root / "scripts" / "skill-budgets.json").write_text(
                json.dumps({"demo": 100}), encoding="utf-8"
            )
            errors = validate_skills(skills, root)
        self.assertTrue(
            any("exceeds its recorded budget" in e for e in errors),
            f"validate_skills did not surface the budget breach; got: {errors}",
        )

    def test_validate_skills_calls_check_skill_budgets(self):
        import ast

        source = (REPO_ROOT / "scripts" / "validate_cross_runtime.py").read_text(
            encoding="utf-8"
        )
        tree = ast.parse(source)
        wired = [
            node
            for fn in ast.walk(tree)
            if isinstance(fn, ast.FunctionDef) and fn.name == "validate_skills"
            for node in ast.walk(fn)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "check_skill_budgets"
        ]
        self.assertEqual(
            len(wired),
            1,
            "check_skill_budgets must be called exactly once from validate_skills(). This "
            "is the weaker half of the pair — it catches a deleted or duplicated call, "
            "while the integration test above catches an unreachable or shadowed one.",
        )


class ShippedBudgetsTests(unittest.TestCase):
    """The real file, against the real tree — the anchor against a vacuous ratchet."""

    def test_the_shipped_budgets_cover_the_real_tree(self):
        self.assertEqual(check_skill_budgets(SKILLS, BUDGETS), [])

    def test_every_recorded_budget_equals_its_measured_count(self):
        """The ratchet's FLOOR.

        `check_skill_budgets` only asks whether a skill is OVER its number, and slack
        satisfies that — so raising every budget disarms the check for thousands of words.
        Measured on the shipped tree before this test existed: tripling all thirteen numbers
        left the validator, the fixture harness and the whole unit suite green.

        The contract is that a recorded budget records TODAY'S count, so any slack is a raise
        nobody reviewed. A regression guard, not a defect demonstration: it passes the moment
        it is written, because every budget already sits at exactly zero slack.

        A budget naming a skill that is not on disk is not skipped, but this test is not what
        catches it: `measure_skill_words` returns 0 for a missing directory, so a recorded 0
        passes here on 0 == 0. `test_every_non_v1_skill_has_a_budget_and_no_v1_skill_does`
        closes that case by asserting the recorded names ARE the names on disk.
        """
        recorded = json.loads(BUDGETS.read_text(encoding="utf-8"))
        drift = {}
        for name, budget in sorted(recorded.items()):
            measured = measure_skill_words(SKILLS / name)
            if measured != budget:
                drift[name] = {"recorded": budget, "measured": measured}
        self.assertEqual(
            drift, {},
            "a recorded budget must equal today's measured count — slack is a ceiling nobody "
            "reviewed, a shortfall is prose that grew without one. Move the number in the "
            f"same change as the prose: {drift}")

    def test_every_non_v1_skill_has_a_budget_and_no_v1_skill_does(self):
        recorded = set(json.loads(BUDGETS.read_text(encoding="utf-8")))
        # `iter_skill_roots`, not a second `*/SKILL.md` glob: "which skills exist" is the
        # question the budget file answers, and a budget recorded for a skill the validator
        # refuses to walk (a symlinked root) would be a budget over nothing.
        on_disk = {p.parent.name for p in iter_skill_roots(SKILLS)}
        self.assertEqual(recorded, on_disk - V1_SUITE_SKILLS)
        self.assertEqual(recorded & V1_SUITE_SKILLS, set())
        self.assertGreater(len(recorded), 0, "no budgets recorded — discovery is broken")


if __name__ == "__main__":
    unittest.main()
