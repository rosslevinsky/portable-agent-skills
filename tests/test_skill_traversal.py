#!/usr/bin/env python3
"""One answer to "which files belong to a skill", asserted against every consumer.

Eleven functions in ``validate_cross_runtime.py`` and four test files each answered that
question independently, and they disagreed. The disagreement is the defect, not any single
answer: a file the ratchet counts but no rule scans still ships, costing word budget while
being governed by nothing.

Measured on a fixture skill carrying one file of each shape, against the unfixed validator:

===========================  ====================  ====================
file in the skill            a prose rule sees it  the budget counts it
===========================  ====================  ====================
``SKILL.md``                 yes                   yes
``references/deep/x.md``     **no**                **yes**
``templates/guide.md``       no                    no
``GUIDE.MD``                 no                    no
``references/helper.py``     no                    -- (engines unbudgeted)
===========================  ====================  ====================

Row 2 is the whole phase in one line. Discovery stopped at ``references/*.md`` while the
ratchet used ``references/**/*.md``, so the two named different file sets and nothing
compared them.

**Most of these are defect demonstrations**, run against the unfixed validator first and
failing there. The rest are **regression guards** that pass the moment they are written, and
each says so in its own docstring.

The equality assertion in :class:`TheBudgetAndTheRulesAgree` is the one that must never be
deleted. The individual cases can each be satisfied by a local fix, and a local fix is how
this defect regenerated at whichever site the last fix had not touched.
"""
import ast
import contextlib
import io
import os
import sys
import tempfile
import unittest
import unittest.mock
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import validate_cross_runtime as vcr  # noqa: E402
from validate_cross_runtime import (  # noqa: E402
    UnreadableTree,
    check_self_contained_skill_refs,
    discover_skill_artifacts,
    measure_skill_words,
    validate_skills,
    walk_tree_files,
)

VALIDATOR = REPO_ROOT / "scripts" / "validate_cross_runtime.py"

SKILLS = REPO_ROOT / "skills"

FRONTMATTER = """---
name: demo
description: A demo skill for traversal tests.
---
# Demo
"""


def _skill(root: Path) -> Path:
    """A skill carrying one file of every shape the traversal must decide about."""
    d = root / "skills" / "demo"
    (d / "references" / "deep").mkdir(parents=True)
    (d / "templates").mkdir()
    (d / "SKILL.md").write_text(FRONTMATTER, encoding="utf-8")
    (d / "references" / "shallow.md").write_text("shallow prose", encoding="utf-8")
    (d / "references" / "deep" / "nested.md").write_text("nested prose", encoding="utf-8")
    (d / "templates" / "guide.md").write_text("template prose", encoding="utf-8")
    (d / "GUIDE.MD").write_text("shouty prose", encoding="utf-8")
    (d / "references" / "helper.py").write_text("import os\n", encoding="utf-8")
    return d


def _discovered_paths(skills_dir: Path) -> set[str]:
    """Normalise discovery output to a set of skill-relative path strings."""
    out = set()
    for entry in discover_skill_artifacts(skills_dir):
        text = entry if isinstance(entry, str) else str(entry)
        out.add(text.split("demo/", 1)[-1] if "demo/" in text else text)
    return out


def _counted(skill_dir: Path, relative: str) -> bool:
    """Does the ratchet count this file? Established by removal, not by reading the glob."""
    target = skill_dir / relative
    before = measure_skill_words(skill_dir)
    body = target.read_bytes()
    target.unlink()
    after = measure_skill_words(skill_dir)
    target.write_bytes(body)
    return before != after


class TheBudgetAndTheRulesAgree(unittest.TestCase):
    """The assertion that outlives every local fix.

    Each case in the classes below can be satisfied on its own, and that is how this defect
    kept coming back: a fix was local by construction, so the disagreement reappeared at
    whichever site the last fix had not touched. This test compares the two sets directly, so
    it fails whenever any consumer is widened without the other.
    """

    def test_every_counted_markdown_file_is_also_scanned_by_the_rules(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            skill = _skill(root)
            discovered = _discovered_paths(root / "skills")
            counted_but_unscanned = sorted(
                rel
                for rel in (
                    "references/shallow.md",
                    "references/deep/nested.md",
                    "templates/guide.md",
                    "GUIDE.MD",
                )
                if _counted(skill, rel) and rel not in discovered
            )
        self.assertEqual(
            counted_but_unscanned, [],
            "these files cost word budget and are governed by no prose rule, which is the "
            f"exact shape of a file that ships unchecked: {counted_but_unscanned}")


class DiscoveryReachesEveryShippedFile(unittest.TestCase):
    """Three traversal gaps, each demonstrated, each failing against the unfixed file."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.skill = _skill(self.root)
        self.discovered = _discovered_paths(self.root / "skills")
        self.addCleanup(self._tmp.cleanup)

    def test_markdown_below_references_is_scanned(self):
        """The ratchet already counts it -- `rglob` there, `glob` in discovery."""
        self.assertIn("references/deep/nested.md", self.discovered)

    def test_markdown_outside_references_is_scanned(self):
        """`skills/<name>/templates/x.md` is scanned by no rule and ships."""
        self.assertIn("templates/guide.md", self.discovered)

    def test_an_uppercase_suffix_is_still_markdown(self):
        """Case folded once, in the walk, so no caller can forget it.

        `GUIDE.MD` is invisible to both mechanisms today -- note this contradicts the parked
        branch's docstring, which claimed it "was discovered and budgeted". Measured against
        the unfixed file, it is neither.
        """
        self.assertIn("GUIDE.MD", self.discovered)

    def test_a_python_helper_under_references_is_discovered(self):
        """Engine discovery globs the skill root only, so a helper one level down
        is checked by nothing -- no encoding rule, no attribution rule, no spawn rule."""
        self.assertIn("references/helper.py", self.discovered)


class DiscoveryNamesEachArtifactOnce(unittest.TestCase):
    """A duplicate in a list of clean artifacts is silent, and it stayed silent.

    Without this, `skills/plan-duel` yields `judge-schema.json` SIX times — once per file
    walked — and every existing test went on passing, because each rule ran six times over a
    file that produces no findings.

    The cost is not cosmetic. Per-artifact rules run per entry, so the day that file does
    produce a finding the operator gets it six times. And the list is the inventory every
    whole-tree check is driven from.
    """

    def test_the_real_tree_discovers_no_artifact_twice(self):
        artifacts = discover_skill_artifacts(SKILLS)
        duplicated = sorted({a for a in artifacts if artifacts.count(a) > 1})
        self.assertEqual(
            duplicated, [],
            f"discovery named these more than once, so every per-artifact rule runs over "
            f"them more than once: {duplicated}")

    def test_a_skill_carrying_every_shape_at_once_still_names_each_once(self):
        """The fixture the real tree cannot provide: markdown, python and a schema together.

        `plan-duel` is the only skill with the schema today, so the real-tree assertion above
        would go quiet the moment that skill changed. This one builds the collision itself.
        """
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            skill = _skill(root)
            (skill / vcr.PLAN_DUEL_SCHEMA).write_text("{}", encoding="utf-8")
            (skill / "extra.py").write_text("x = 1\n", encoding="utf-8")
            artifacts = discover_skill_artifacts(root / "skills")
        duplicated = sorted({a for a in artifacts if artifacts.count(a) > 1})
        self.assertEqual(duplicated, [])
        self.assertIn(f"demo/{vcr.PLAN_DUEL_SCHEMA}", artifacts)


class BuildResidueIsExcludedOnce(unittest.TestCase):
    """A REGRESSION GUARD, and the filter CI can never exercise.

    It passes today only by accident: discovery's glob is too narrow to reach into
    `__pycache__` at all. A shared `rglob("*")` walk removes that immunity, so this guard
    starts carrying real load exactly when the traversal is unified.

    `__pycache__` is gitignored, so a fresh checkout never has one — but any machine that has
    run `plan_duel.py` does. A broken residue filter is therefore green on every runner and
    red only locally, which is the inverse of the usual platform trap.
    """

    def test_compiled_cache_is_not_discovered_as_a_skill_artifact(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            skill = _skill(root)
            (skill / "__pycache__").mkdir()
            (skill / "__pycache__" / "demo.cpython-312.pyc").write_bytes(b"\x00\x01binary")
            (skill / "__pycache__" / "stale.md").write_text("residue", encoding="utf-8")
            discovered = _discovered_paths(root / "skills")
            counted = _counted(skill, "__pycache__/stale.md")
        leaked = sorted(p for p in discovered if "__pycache__" in p)
        self.assertEqual(leaked, [], f"build residue reached a prose rule: {leaked}")
        self.assertFalse(counted, "build residue was counted against the word budget")


class SymlinksAreRefusedNotFollowed(unittest.TestCase):
    """Validating a link validates whatever it resolves to, which defeats self-containment.

    The refusal must be reported rather than silent, and it must apply to every suffix: a
    symlinked `judge-schema.json` or `guide.txt` escapes a skill exactly as a markdown one
    does, and a suffix filter here missed both.
    """

    def test_a_symlinked_artifact_is_not_silently_followed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            skill = _skill(root)
            outside = root / "outside.md"
            outside.write_text("prose from beyond the skill", encoding="utf-8")
            link = skill / "references" / "linked.md"
            link.symlink_to(outside)
            counted = _counted(skill, "references/linked.md")
        self.assertFalse(
            counted,
            "a symlink was followed and its target counted, so a skill can carry prose that "
            "does not travel with it when installed")


class ASymlinkedSkillRootIsRefused(unittest.TestCase):
    """"Which skills exist" is a separate question, and it had no guard at all.

    Six call sites asked it, every one spelled ``glob("*/SKILL.md")``, and none refused a
    link. So the file walk could refuse every symlink *inside* a skill while the skill itself
    was a symlink pointing out of the repository.

    Demonstrated against the unfixed file: a ``skills/sneaky`` linked to a directory outside
    the tree had both its ``SKILL.md`` and its ``references/payload.md`` discovered and
    counted, so the validator's guarantees silently became claims about files the repository
    does not contain.
    """

    def _tree(self, root: Path) -> Path:
        outside = root / "elsewhere" / "sneaky"
        (outside / "references").mkdir(parents=True)
        (outside / "SKILL.md").write_text(FRONTMATTER, encoding="utf-8")
        (outside / "references" / "payload.md").write_text("prose from outside", encoding="utf-8")
        skills = root / "skills"
        skills.mkdir()
        (skills / "sneaky").symlink_to(outside, target_is_directory=True)
        return skills

    def test_a_symlinked_skill_root_is_not_discovered(self):
        with tempfile.TemporaryDirectory() as tmp:
            skills = self._tree(Path(tmp))
            discovered = discover_skill_artifacts(skills)
        self.assertEqual(
            discovered, [],
            "a skill root linked out of the repository was walked, so files the repo does "
            f"not contain were validated and budgeted as if it did: {discovered}")

    def test_the_refusal_is_reported_rather_than_silent(self):
        """A skill silently skipped is indistinguishable from one that passed."""
        from validate_cross_runtime import symlinked_skill_roots

        with tempfile.TemporaryDirectory() as tmp:
            skills = self._tree(Path(tmp))
            refused = [p.name for p in symlinked_skill_roots(skills)]
        self.assertEqual(refused, ["sneaky"])


class TheDepthDistinctionSurvivesTheWalk(unittest.TestCase):
    """`check_self_contained_skill_refs` consumes the walk's SHAPE, not its output.

    It takes `at_skill_root` and uses it to decide whether escaping the skill costs one
    `../` or two. A shared traversal that returns a flat list of paths and drops the depth
    would silently relax that threshold, letting a `references/` file reference out of its
    own skill and pass. This is the one consumer a flattening refactor breaks invisibly.
    """

    def test_one_dot_dot_means_different_things_at_the_two_depths(self):
        """A REGRESSION GUARD: it passes today, and must keep passing.

        The discriminating case is a SINGLE `../`. Two levels escape from either depth, so a
        `../../` fixture proves nothing about the parameter -- a draft of this test
        used one and could not have detected a flattening refactor at all.

        One `../` from the skill root leaves the skill. One `../` from `references/` arrives
        at the skill root, still inside. If a shared walk stops supplying `at_skill_root`,
        whichever default it picks makes one of these two answers silently wrong.
        """
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            skill = _skill(root)
            probe = skill / "references" / "probe.md"
            probe.write_text("See [a sibling](../sibling.md).", encoding="utf-8")
            at_root = check_self_contained_skill_refs(probe, depth=0)
            below = check_self_contained_skill_refs(probe, depth=1)
        self.assertTrue(
            at_root,
            "one `../` from the skill root escapes the skill and must be flagged")
        self.assertEqual(
            below, [],
            "one `../` from `references/` reaches the skill root and is still inside; "
            "flagging it would make every legitimate back-reference an error")


# Every function in the validator allowed to touch the filesystem directly, and why. A
# function absent from this map may not call `glob`, `rglob`, `iterdir`, `os.walk`,
# `os.scandir` or `os.listdir` -- it must go through the shared traversal instead.
#
# The reasons are part of the data, not decoration: the failure this whole phase exists to
# remove is a second answer to a question that already had one, and the only way to see that
# a new walker is a second answer is to read what question it thinks it is asking.
DIRECT_FILESYSTEM_WALKERS = {
    "_walk_tree":
        "THE primitive. os.walk, one place, with the residue prune, the case fold, the "
        "symlink refusal and the OSError that rglob swallows.",
    "iter_skill_roots":
        "'Which skills exist' -- a different question from 'which files belong to one', "
        "and the glob is over skill roots, not over a skill's contents.",
    "symlinked_skill_roots":
        "The reporting twin of iter_skill_roots. Same predicate on purpose: a refusal one "
        "function makes and another does not mention is how the refusal went silent.",
}


class TheConsumerSetIsClosed(unittest.TestCase):
    """The test the parked branch could not have had, and the one its claim needed.

    That branch stated it had covered every consumer. It had covered five of six it named,
    named one function that exists nowhere in the repository, and missed six that do — and
    nothing failed, because "did we reach them all" was a claim in a docstring rather than an
    assertion over the source.

    So the closure is asserted as an enumerated list rather than a default: a new walker
    FAILS this test until someone writes down which question it asks. Widening the allowlist
    to make the error go away is the one response that is always wrong.
    """

    def _direct_walkers(self):
        """Function name -> the filesystem calls it makes, taken from the AST, not a grep.

        ``ast.walk`` is deliberately not counted: it walks a syntax tree.
        ``check_engine_portability`` calls it, and a name-only match reports that function as
        a filesystem consumer -- a test that did exactly that, which is a
        false positive that teaches a reader to widen the allowlist.
        """
        found = {}
        tree = ast.parse(VALIDATOR.read_text(encoding="utf-8"))
        for fn in ast.walk(tree):
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for node in ast.walk(fn):
                if not isinstance(node, ast.Call):
                    continue
                func = node.func
                if not isinstance(func, ast.Attribute):
                    continue
                owner = func.value.id if isinstance(func.value, ast.Name) else None
                if func.attr in ("glob", "rglob", "iterdir"):
                    found.setdefault(fn.name, set()).add(func.attr)
                elif func.attr in ("walk", "scandir", "listdir") and owner != "ast":
                    # Every `.walk` EXCEPT `ast.walk`, rather than only `os.walk`. Naming
                    # the one allowed API left `Path.walk()` -- added in 3.12, and a
                    # perfectly ordinary way to write a second traversal -- outside the
                    # closed set entirely.
                    found.setdefault(fn.name, set()).add(func.attr)
        return found

    def test_no_function_walks_the_filesystem_outside_the_declared_set(self):
        walkers = self._direct_walkers()
        undeclared = sorted(set(walkers) - set(DIRECT_FILESYSTEM_WALKERS))
        self.assertEqual(
            undeclared, [],
            "these functions walk the filesystem themselves, so they hold their own answer "
            "to a question the shared traversal already answers. Route them through it, or "
            "-- if the question really is different -- add them to DIRECT_FILESYSTEM_WALKERS "
            f"with the reason: {undeclared}")

    def test_the_declared_set_has_no_stale_entries(self):
        """An allowlist that outlives its entry is how the next one gets waved through.

        A name here for a function that no longer walks (or no longer exists) makes the list
        look considered while it is stale, and the next reader adds to it rather than
        questioning it.
        """
        walkers = self._direct_walkers()
        stale = sorted(set(DIRECT_FILESYSTEM_WALKERS) - set(walkers))
        self.assertEqual(
            stale, [],
            f"declared as direct walkers but no longer walking: {stale}")

    def test_every_exemption_states_which_question_it_asks(self):
        empty = sorted(k for k, v in DIRECT_FILESYSTEM_WALKERS.items() if len(v.split()) < 8)
        self.assertEqual(
            empty, [],
            f"an exemption without a stated question is an exemption nobody can review: "
            f"{empty}")


class TheRefusalReachesTheReport(unittest.TestCase):
    """A refusal nobody reports is indistinguishable from no refusal at all.

    Demonstrated: without the pairing, `iter_skill_roots`
    refused a symlinked skill and `walk_tree_files` refused a symlinked file, both correctly
    and both in silence, while `symlinked_skill_roots` and `walk_tree_symlinks` -- the two
    generators whose entire purpose is to say so -- were called by no production code. A test
    called them, so the wiring looked present. A real run skipped the skill and printed
    nothing, and the count of validated skills is not printed anywhere a reader would notice
    one missing.

    That is the phase's own defect shape, reintroduced by the phase's own fix: a guard whose
    report exists but is reachable from nothing.
    """

    def _tree(self, root: Path) -> Path:
        outside = root / "elsewhere"
        outside.mkdir()
        (outside / "payload.md").write_text("prose from outside", encoding="utf-8")
        linked_skill = outside / "sneaky"
        linked_skill.mkdir()
        (linked_skill / "SKILL.md").write_text(FRONTMATTER, encoding="utf-8")

        skills = root / "skills"
        skills.mkdir()
        (skills / "sneaky").symlink_to(linked_skill, target_is_directory=True)
        real = skills / "demo"
        (real / "references").mkdir(parents=True)
        (real / "SKILL.md").write_text(FRONTMATTER, encoding="utf-8")
        (real / "references" / "borrowed.md").symlink_to(outside / "payload.md")
        return skills

    def test_a_refused_skill_root_is_named_in_the_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            skills = self._tree(Path(tmp))
            report = "\n".join(validate_skills(skills, Path(tmp)))
        self.assertIn("sneaky", report)
        self.assertIn("skill root is a symlink", report)

    def test_a_refused_file_inside_a_skill_is_named_in_the_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            skills = self._tree(Path(tmp))
            report = "\n".join(validate_skills(skills, Path(tmp)))
        self.assertIn("borrowed.md", report)
        self.assertIn("is a symlink", report)


class ASymlinkedDirectoryIsNotDescended(unittest.TestCase):
    """The case a per-entry `is_symlink()` filter cannot catch.

    A file *inside* a symlinked directory is not itself a symlink. Filter entry by entry and
    every one passes — the refusal has to happen at the descent, which is why the walk prunes
    `dirnames` rather than testing what it yields.

    A REGRESSION GUARD: it passes against the unfixed file, so its non-vacuity was
    established by mutation. Setting ``followlinks=True`` alone left it GREEN, because the
    walk holds two independent locks and either is sufficient: `os.walk`'s own refusal, and
    the explicit `dirnames` prune. It goes red only when both are removed together.

    That limit is stated rather than papered over: this asserts the PROPERTY — prose behind a
    link is not the skill's — and cannot see the loss of either lock alone.
    """

    def test_a_link_pointing_back_INSIDE_the_skill_is_still_refused_and_reported(self):
        """The regression a containment test introduced, caught by review before it landed.

        A first attempt decided descent by asking whether the link resolved outside the tree.
        `demo/alias -> references` resolves INSIDE, so it passed that test and was treated as
        an ordinary directory: neither refused nor reported, with its contents reachable under
        two names. On Windows the same test let a junction resolving to its own parent be
        descended until the path length gave out.

        A real directory resolves to exactly where it sits. That is the property, and it does
        not care whether a link points in or out.
        """
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            skill = _skill(root)
            (skill / "alias").symlink_to(skill / "references", target_is_directory=True)
            links = {r.as_posix() for _p, r in vcr.walk_tree_symlinks(skill)}
            walked = {r.as_posix() for _p, r, _s in walk_tree_files(skill)}
            report = "\n".join(vcr.check_refused_symlinks(root / "skills"))
        self.assertIn("alias", links, "an internal link was not reported as refused")
        self.assertEqual(
            [w for w in walked if w.startswith("alias/")], [],
            "a linked directory was descended, so its files are reachable under two names "
            "and counted twice against the word budget")
        self.assertIn("alias", report)

    def test_files_under_a_linked_directory_are_neither_walked_nor_counted(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            skill = _skill(root)
            outside = root / "elsewhere"
            outside.mkdir()
            (outside / "smuggled.md").write_text("a b c d e f g", encoding="utf-8")
            (skill / "borrowed").symlink_to(outside, target_is_directory=True)

            walked = {r.as_posix() for _p, r, _s in walk_tree_files(skill)}
            discovered = _discovered_paths(root / "skills")
            words_with = measure_skill_words(skill)
            (skill / "borrowed").unlink()
            words_without = measure_skill_words(skill)

        self.assertEqual(
            [w for w in walked if "smuggled" in w], [],
            "the walk descended a symlinked directory, so files outside the skill were "
            "treated as the skill's own")
        self.assertEqual([d for d in discovered if "smuggled" in d], [])
        self.assertEqual(
            words_with, words_without,
            "prose behind a symlinked directory was charged to this skill's word budget")


class ALinkedRootIsRefusedToo(unittest.TestCase):
    """The guard was on the descendants and not on the thing they descend from.

    `followlinks=False` governs what the walk descends INTO; `is_dir()` dereferences, so a
    root that was itself a link was walked in full. `iter_skill_roots` refused a linked
    *skill*, but the two consumers that walk `skills/` whole and the hygiene sweep that walks
    the repository had no such guard -- and a validator whose guarantees quietly become
    claims about a different tree is the failure this phase exists to remove, one level above
    where it was fixed.
    """

    def test_a_linked_root_raises_rather_than_walking_the_target(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            real = root / "elsewhere"
            (real / "sneaky").mkdir(parents=True)
            (real / "sneaky" / "SKILL.md").write_text(FRONTMATTER, encoding="utf-8")
            linked = root / "skills"
            linked.symlink_to(real, target_is_directory=True)
            with self.assertRaises(UnreadableTree) as caught:
                list(walk_tree_files(linked))
        self.assertIn("is a link", str(caught.exception))

    def test_an_awkwardly_spelled_root_is_not_mistaken_for_a_link(self):
        """The link test compares two spellings of a location, so only one may be canonical.

        Refusing a real directory is the more damaging direction of this rule: it stops the
        run rather than letting something through, and the operator's only clue is the word
        "link" about a directory that plainly is not one. `Path(".")` compared `/cwd` with
        `/cwd/.` and was refused; a trailing separator and a `..` component did the same.
        """
        refused = []
        with tempfile.TemporaryDirectory() as tmp:
            skill = Path(tmp) / "skills" / "demo"
            skill.mkdir(parents=True)
            (skill / "SKILL.md").write_text(FRONTMATTER, encoding="utf-8")
            previous = os.getcwd()
            os.chdir(tmp)
            try:
                for spelling in (Path("."), Path("skills"), Path("skills/"),
                                 Path("skills/../skills"), Path(tmp) / "skills"):
                    try:
                        list(walk_tree_files(spelling))
                    except UnreadableTree:
                        refused.append(str(spelling))
            finally:
                os.chdir(previous)
        self.assertEqual(
            refused, [],
            f"these are real directories, refused as links because of how they are "
            f"spelled: {refused}")

    def test_the_refusal_is_an_error_line_not_a_traceback(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "elsewhere").mkdir()
            (root / "skills").symlink_to(root / "elsewhere", target_is_directory=True)
            argv = ["validate_cross_runtime.py", str(root / "skills")]
            with unittest.mock.patch.object(sys, "argv", argv), \
                    unittest.mock.patch.object(vcr, "__file__", str(VALIDATOR)):
                buffer = io.StringIO()
                with contextlib.redirect_stdout(buffer):
                    with self.assertRaises(SystemExit) as exit_info:
                        vcr.main()
        self.assertEqual(exit_info.exception.code, 1)
        self.assertIn("is a link", buffer.getvalue())
        self.assertNotIn("Traceback", buffer.getvalue())


class AnUnreadableDirectoryIsReportedNotSwallowed(unittest.TestCase):
    """`rglob` returns a short list and no error, so the run is green and the evidence absent.

    A directory at mode ``0o000`` inside a skill yields fewer files from `rglob` with no
    exception raised, so every check passes over prose it never opened.

    The error is INJECTED rather than produced by `chmod`, because `chmod(0o000)` does not
    prevent reading for root, and on Windows does not prevent reading at all — a test relying
    on it silently stops testing on two of the three CI platforms.
    """

    def _skill_with_a_subdirectory(self, root: Path) -> Path:
        skill = root / "skills" / "demo"
        (skill / "references").mkdir(parents=True)
        (skill / "SKILL.md").write_text(FRONTMATTER, encoding="utf-8")
        (skill / "references" / "shallow.md").write_text("prose", encoding="utf-8")
        return skill

    def test_an_os_error_during_the_walk_becomes_an_exception(self):
        real_walk = os.walk

        def failing_walk(top, onerror=None, followlinks=False, **kwargs):
            for entry in real_walk(top, onerror=onerror, followlinks=followlinks, **kwargs):
                yield entry
            if onerror is not None:
                onerror(PermissionError(13, "Permission denied", str(top)))

        with tempfile.TemporaryDirectory() as tmp:
            skill = self._skill_with_a_subdirectory(Path(tmp))
            with unittest.mock.patch.object(vcr.os, "walk", failing_walk):
                with self.assertRaises(UnreadableTree) as caught:
                    list(walk_tree_files(skill))
        self.assertIn("partially scanned", str(caught.exception))

    def test_the_exception_is_reported_as_an_error_line_not_a_traceback(self):
        """A traceback is a non-zero exit that does not say which directory.

        That is the failure `check_shipped_files_decode` exists to remove, so a walk that
        reintroduces it one function away has undone the fix rather than extended it.
        """
        real_walk = os.walk

        def failing_walk(top, onerror=None, followlinks=False, **kwargs):
            yield from real_walk(top, onerror=onerror, followlinks=followlinks, **kwargs)
            if onerror is not None:
                onerror(PermissionError(13, "Permission denied", str(top)))

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._skill_with_a_subdirectory(root)
            argv = ["validate_cross_runtime.py", str(root / "skills")]
            with unittest.mock.patch.object(vcr.os, "walk", failing_walk), \
                    unittest.mock.patch.object(sys, "argv", argv), \
                    unittest.mock.patch.object(vcr, "__file__", str(VALIDATOR)):
                buffer = io.StringIO()
                with contextlib.redirect_stdout(buffer):
                    with self.assertRaises(SystemExit) as exit_info:
                        vcr.main()
        self.assertEqual(exit_info.exception.code, 1)
        self.assertIn("could not be read", buffer.getvalue())
        self.assertNotIn("Traceback", buffer.getvalue())

    def test_real_permissions_where_the_platform_honours_them(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            skill = self._skill_with_a_subdirectory(root)
            locked = skill / "references"
            try:
                locked.chmod(0o000)
                honoured = False
                try:
                    list(locked.iterdir())
                except PermissionError:
                    honoured = True
                if not honoured:
                    self.skipTest("this platform/user can read a 0o000 directory")
                with self.assertRaises(UnreadableTree):
                    list(walk_tree_files(skill))
            finally:
                locked.chmod(0o755)


class RelativePathsAreResolvedNotCounted(unittest.TestCase):
    """Counting ``../`` is not resolving a path, and it was wrong in both directions.

    Measured against the two regexes this replaced (`\\.\\./` at the skill root,
    `\\.\\./\\.\\./` below it): three of eight probes were false negatives and one was a
    false positive. The false positive matters most for the rule's future -- a check that
    flags a path which stays inside the skill is one an author learns to write around, and a
    rule authors route around has stopped being enforcement.

    Every row below FAILS against the unfixed file in the direction its comment names.
    """

    CASES = [
        # (written on the line, the file's depth below its skill root, does it leave)
        ("../sibling.md", 0, True),              # unchanged: the plain case both agree on
        ("../sibling.md", 1, False),             # unchanged: one `../` from references/
        ("../..", 1, True),                      # was passed: no trailing slash to match
        (".././../x.md", 1, True),               # was passed: the `..`s are not adjacent
        ("..\\..\\outside.md", 1, True),         # was passed: backslashes are separators too
        ("..\\outside.md", 0, True),             # was passed: same, at the skill root
        ("sub/../../SKILL.md", 1, False),        # was FLAGGED: resolves to the skill root
        ("tmp/../../outside.md", 0, True),       # unchanged: the phase document's own case
        ("references/deep/x.md", 0, False),      # no `..` at all
        ("https://example.com/a/../b", 0, False),   # a server resolves this, not the disk
        ("dots ... and wait.. no", 0, False),       # prose that is not a path segment
        # Depth 2, the case widening discovery created. `references/deep/guide.md` linking
        # to its own skill root was reported as escaping while the caller had only a
        # boolean to describe where the file sat.
        ("../../SKILL.md", 2, False),
        ("../../../outside.md", 2, True),
        # A path glued to command syntax. The token is one run of characters, so its first
        # segment was `--output=..`, which is not `..`, and nothing was counted.
        ("--output=../../outside.md", 1, True),
        ("TARGET=../outside.md", 0, True),
        ("C:..\\outside.md", 0, True),           # Windows drive-relative
        # Percent-encoded dot segments: `%2e%2e/` is `../` to whatever resolves the link.
        ("%2e%2e/%2e%2e/outside.md", 1, True),
        # Absolute paths are not relative to the skill at all, and were SKIPPED on the
        # reasoning that the private-path patterns catch them. They do not.
        ("/tmp/../../outside.md", 0, True),
        ("C:\\work\\..\\outside.md", 0, True),
        # A query string is resolved by whatever serves the URL, not by the filesystem, so
        # the path is what precedes it. These two fail in OPPOSITE directions, which is why
        # neither "test the whole token for a scheme" nor "test each candidate" is enough.
        ("guide.md?next=../../outside.md", 1, False),
        ("../../outside.md?mirror=https://example.com", 1, True),
        ("https://example.com/?next=../../outside.md", 0, False),
    ]

    def test_each_case_is_judged_by_where_it_lands(self):
        wrong = []
        with tempfile.TemporaryDirectory() as tmp:
            probe = Path(tmp) / "probe.md"
            for token, depth, escapes in self.CASES:
                probe.write_text(f"See [x]({token}).", encoding="utf-8")
                flagged = bool(check_self_contained_skill_refs(probe, depth=depth))
                if flagged != escapes:
                    wrong.append(
                        f"{token!r} depth={depth}: escapes={escapes} flagged={flagged}")
        self.assertEqual(
            wrong, [],
            "the rule disagreed with where the path actually resolves:\n  " + "\n  ".join(wrong))

    def test_the_diagnostic_says_how_far_out_the_path_lands(self):
        """`../..` under `references/` is not visibly an escape until someone counts."""
        with tempfile.TemporaryDirectory() as tmp:
            probe = Path(tmp) / "probe.md"
            probe.write_text("See [x](../../../far.md).", encoding="utf-8")
            errors = check_self_contained_skill_refs(probe, depth=1)
        self.assertEqual(len(errors), 1)
        self.assertIn("2 levels above the skill directory", errors[0])

    def test_an_absolute_path_is_named_as_absolute_not_counted_in_levels(self):
        """A true sentence about the wrong thing sends the reader to the wrong rule."""
        with tempfile.TemporaryDirectory() as tmp:
            probe = Path(tmp) / "probe.md"
            probe.write_text("See [x](/tmp/../../outside.md).", encoding="utf-8")
            errors = check_self_contained_skill_refs(probe, depth=0)
        self.assertEqual(len(errors), 1)
        self.assertIn("absolute path", errors[0])
        self.assertNotIn("levels above", errors[0])


if __name__ == "__main__":
    unittest.main()
