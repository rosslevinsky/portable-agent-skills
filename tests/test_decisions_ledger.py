#!/usr/bin/env python3
"""A `DECISIONS.md` holds rationale that has LEFT the runtime path, not a second copy of it.

Each skill may carry a `DECISIONS.md` beside its `SKILL.md`: the defeated alternatives, the
threat models, the "why not the other way" paragraphs a reviewer needs when deciding whether
to change a rule, and a runtime following the rule does not. The whole point is that the text
moved — a paragraph living in both places costs the context it was supposed to save and adds
a second copy to keep in step.

So: every ledger paragraph must appear in no `SKILL.md`, in no `references/*.md`, and in no
other ledger. Comparison is on whitespace-normalised text, because relocating a paragraph
re-wraps it and a line-by-line check would miss every real duplicate.

Deliberately NOT checked here: private paths and hardcoded attribution. `validate_skills()`
runs `sweep_content_hygiene()` over every file under `skills/`, ledgers included.
"""
import re
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SKILLS = REPO_ROOT / "skills"

sys.path.insert(0, str(REPO_ROOT / "scripts"))
import validate_cross_runtime as vcr  # noqa: E402

# Short blocks are headings, list fragments and one-line asides. They collide across
# documents for uninteresting reasons ("## Goal"), so the comparison starts above the length
# where a match means someone actually moved prose.
MIN_WORDS = 15


# Per-line decoration that repeats down a block: shell/Markdown comment markers and
# blockquote markers. Relocated rationale often LIVED as `# ` comment lines inside a bash
# fence, and whitespace-only normalisation leaves those prefixes interleaved through the
# text — so the same prose re-added in that shape would not substring-match the plain
# paragraph in the ledger, and this guard would pass while the duplication was back.
_LINE_MARKER = re.compile(r"^[ \t]*(?:[>#]+[ \t]?)+", re.MULTILINE)


def _normalise(text: str) -> str:
    return re.sub(r"\s+", " ", _LINE_MARKER.sub("", text)).strip()


def _paragraphs(path: Path) -> list[str]:
    """Blank-line-separated blocks worth comparing, whitespace-normalised."""
    out = []
    for block in re.split(r"\n\s*\n", path.read_text(encoding="utf-8")):
        block = block.strip()
        if not block:
            continue
        # No "skip blocks starting with #" rule: it would skip a fenced comment block
        # wholesale, which is precisely the shape this guard has to see. Headings are one
        # short line and MIN_WORDS drops them anyway.
        if len(_normalise(block).split()) < MIN_WORDS:
            continue
        out.append(_normalise(block))
    return out


def _rel(path: Path, root: Path) -> Path:
    """`path` relative to `root` where it is under it, else unchanged (temp-dir fixtures)."""
    try:
        return path.relative_to(root)
    except ValueError:
        return path


def skill_reference_pairs(skills_dir: Path) -> list[tuple[Path, Path]]:
    """Every ``(SKILL.md, companion .md)`` pair under ``skills_dir``.

    Scope comes from the validator's traversal, not from a glob written here. The glob it
    replaces was ``references/*.md`` -- markdown at exactly one level, under exactly one
    directory name -- which is one of the four incompatible answers this phase collapsed. A
    test holding its own copy of a scope rule stops testing the moment production's moves,
    and it stops silently, which is the same failure the production drift had.
    """
    return [
        (skill_md, path)
        for skill_md in vcr.iter_skill_roots(skills_dir)
        for path, relative, suffix in vcr.walk_tree_files(skill_md.parent)
        if suffix == ".md" and str(relative) != "SKILL.md"
    ]


def duplication_offences(skills_dir: Path, root: Path | None = None) -> list[str]:
    """Paragraphs carried by BOTH a skill body and one of its own references.

    Split out of the test that asserts on it so the same loop — discovery, paragraph
    splitting, comparison — can be driven against a fixture tree. Asserting on a
    substring helper instead would leave the loop itself untested, and a glob that
    matched nothing would then pass as silently as a clean tree.
    """
    root = root or skills_dir
    offences = []
    for skill_md, ref in skill_reference_pairs(skills_dir):
        ref_body = _normalise(ref.read_text(encoding="utf-8"))
        skill_body = _normalise(skill_md.read_text(encoding="utf-8"))
        for para in _paragraphs(skill_md):
            if para in ref_body:
                offences.append(
                    f"{_rel(skill_md, root)} -> also in {_rel(ref, root)}: {para[:90]}..."
                )
        for para in _paragraphs(ref):
            if para in skill_body:
                offences.append(
                    f"{_rel(ref, root)} -> also in {_rel(skill_md, root)}: {para[:90]}..."
                )
    return offences


def ledgers() -> list[Path]:
    """The ledger is the file named ``DECISIONS.md`` AT a skill root -- the validator's rule.

    ``LEDGER_FILENAME`` and ``iter_skill_roots`` are both imported rather than restated: the
    ledger used to be recognised by basename in one place and by nothing in another, so a
    ``references/DECISIONS.md`` was budgeted while the root one was not.
    """
    return sorted(
        skill_md.parent / vcr.LEDGER_FILENAME
        for skill_md in vcr.iter_skill_roots(SKILLS)
        if (skill_md.parent / vcr.LEDGER_FILENAME).is_file()
    )


def runtime_documents() -> list[Path]:
    """What a runtime actually reads: every markdown file installed with the skill.

    Was ``*/SKILL.md`` plus ``*/references/*.md``. The installer ships ``skills/<name>/``
    whole, so a document at any depth is on the runtime path; the two globs described a
    narrower tree than the one that actually gets installed.
    """
    return sorted(
        path
        for skill_md in vcr.iter_skill_roots(SKILLS)
        for path, relative, suffix in vcr.walk_tree_files(skill_md.parent)
        # The ledger is not on the runtime path — that is this whole file's premise, and
        # the comparison below is ledger-against-runtime, so including it would compare
        # every ledger with itself and report each of its own paragraphs as a duplicate.
        if suffix == ".md" and str(relative) != vcr.LEDGER_FILENAME
    )


class DecisionsLedgerTests(unittest.TestCase):
    def test_the_real_skills_tree_actually_has_ledgers_to_check(self):
        """Anchor against vacuity — all three assertions below iterate `ledgers()`.

        With none on disk each one compares nothing and passes, so the suite would report
        the ledger contract as upheld by a tree that had stopped having ledgers at all.
        `skills/plan-run` ships one, which is what makes zero a discovery failure rather
        than a clean result. The same anchor `skill_reference_pairs` already carries.
        """
        self.assertGreater(
            len(ledgers()), 0,
            "no DECISIONS.md found at any skill root — discovery is broken, and every "
            "ledger assertion below is passing having compared nothing")

    def test_no_ledger_paragraph_survives_on_the_runtime_path(self):
        runtime = {p: _normalise(p.read_text(encoding="utf-8")) for p in runtime_documents()}
        offences = []
        for ledger in ledgers():
            for para in _paragraphs(ledger):
                for doc, body in runtime.items():
                    if para in body:
                        offences.append(
                            f"{ledger.relative_to(REPO_ROOT)} -> also in "
                            f"{doc.relative_to(REPO_ROOT)}: {para[:90]}..."
                        )
        self.assertEqual(
            offences,
            [],
            "rationale moved to a ledger must be DELETED from the runtime path, not copied:"
            "\n  " + "\n  ".join(offences),
        )

    def test_no_paragraph_appears_in_two_ledgers(self):
        seen: dict[str, Path] = {}
        offences = []
        for ledger in ledgers():
            for para in _paragraphs(ledger):
                if para in seen:
                    offences.append(
                        f"{ledger.relative_to(REPO_ROOT)} duplicates "
                        f"{seen[para].relative_to(REPO_ROOT)}: {para[:90]}..."
                    )
                else:
                    seen[para] = ledger
        self.assertEqual(
            offences,
            [],
            'a relocated paragraph belongs to exactly one skill:\n  ' + "\n  ".join(offences),
        )

    def test_no_ledger_climbs_out_of_its_skill_directory(self):
        """`install.py` ships `skills/<name>/` only, so a `../` in a ledger points nowhere."""
        offences = [
            f"{p.relative_to(REPO_ROOT)}:{i}"
            for p in ledgers()
            for i, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1)
            if "../" in line
        ]
        self.assertEqual(offences, [], f"ledger escapes its skill directory: {offences}")

    def test_the_duplication_check_can_actually_fail(self):
        """A guard nobody has seen fail is a guard nobody has tested."""
        shared = " ".join(f"word{i}" for i in range(MIN_WORDS + 5))
        self.assertIn(_normalise(shared), _normalise(f"preamble\n{shared}\ntail"))
        self.assertNotIn(_normalise(shared), _normalise("something else entirely"))
        # And the re-wrap case the normalisation exists for: same prose, different line
        # breaks, must still be recognised as the same paragraph.
        rewrapped = shared.replace(" ", "\n  ", 3)
        self.assertEqual(_normalise(rewrapped), _normalise(shared))

    def test_no_paragraph_is_duplicated_between_a_skill_and_its_own_references(self):
        """A skill body and its own `references/` are the one pairing nothing else compares.

        The ledger tests above each check a `DECISIONS.md` against both, and never check those
        two against each other — so a paragraph copied from `SKILL.md` into a `references/` doc
        beside it has no guard at all.

        **This catches copy-paste only, and that is the whole claim.** The comparison is a
        substring test on normalised text, so the same rule restated in different words is
        invisible to it — which is exactly the shape the duplication found by hand had.
        Detecting *that* is out of scope: a redundancy detector produces candidates, and
        deciding which copy is the home is a judgement a check cannot make.
        """
        offences = duplication_offences(SKILLS, REPO_ROOT)
        self.assertEqual(
            offences,
            [],
            "a skill body and its references must not carry the same paragraph twice; one "
            "of them is the home:\n  " + "\n  ".join(offences),
        )

    def test_the_skill_versus_references_check_can_actually_fail(self):
        """The guard above has never fired on this tree, so prove it is able to.

        Driving :func:`duplication_offences` over a fixture tree rather than asserting on
        substring behaviour: a companion that only checked ``in`` would stay green if a
        glob stopped matching, and the guard would then pass vacuously beside it.
        """
        shared = " ".join(f"word{i}" for i in range(MIN_WORDS + 5))
        with tempfile.TemporaryDirectory() as tmp:
            skills = Path(tmp)
            refs = skills / "demo" / "references"
            refs.mkdir(parents=True)
            (skills / "demo" / "SKILL.md").write_text(
                f"# Demo\n\n{shared}\n", encoding="utf-8"
            )
            (refs / "companion.md").write_text(
                f"# Companion\n\npreamble\n\n{shared}\n", encoding="utf-8"
            )
            self.assertEqual(len(skill_reference_pairs(skills)), 1)
            fired = duplication_offences(skills)
            self.assertEqual(len(fired), 2, f"expected both directions, got {fired}")

            # The same rule restated is invisible to it — a recorded property, not a bug.
            (refs / "companion.md").write_text(
                "# Companion\n\npreamble\n\n"
                + " ".join(f"word{i}" for i in reversed(range(MIN_WORDS + 5)))
                + "\n",
                encoding="utf-8",
            )
            self.assertEqual(duplication_offences(skills), [])

    def test_the_real_skills_tree_actually_has_pairs_to_check(self):
        """Anchor against vacuity: the guard is only meaningful if discovery finds work.

        `skills/plan-run` and `skills/plan-phase` both ship a `references/` directory, so a
        zero here means a glob or a layout change broke discovery, not that the tree is
        clean.
        """
        pairs = skill_reference_pairs(SKILLS)
        self.assertGreater(
            len(pairs), 0, "no (SKILL.md, references/*.md) pairs found — discovery is broken"
        )

    def test_a_comment_prefixed_reintroduction_is_still_caught(self):
        """Regression: the shape the relocated rationale actually had.

        `plan-run`'s defeated-alternatives commentary lived as `# ` lines inside a bash
        fence. With whitespace-only normalisation those prefixes stayed interleaved through
        the text, so the same prose re-added in that shape did not substring-match the plain
        paragraph in the ledger and this guard passed while the duplication was back.
        """
        shared = " ".join(f"word{i}" for i in range(MIN_WORDS + 5))
        for prefix in ("# ", "#", "> ", "># "):
            with self.subTest(prefix=prefix):
                reintroduced = "\n".join(prefix + w for w in shared.split())
                self.assertIn(_normalise(shared), _normalise(reintroduced))


if __name__ == "__main__":
    unittest.main()
