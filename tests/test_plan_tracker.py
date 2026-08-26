"""The plan tracker check: one isolated case per rule.

`execution.md` is a checkbox list whose links point at the phase documents. Several rules are
about the DIRECTORY — a link must resolve to a real phase document, and every `phase-*.md`
entry must be listed — which a single flat fixture file cannot express, so each case below
builds a throwaway plan directory.

Every case violates EXACTLY ONE rule and asserts a finding COUNT of 1: a fixture that trips
two rules proves neither. Each has additionally been mutation-verified. That is not ceremony:
this repo has a documented history of suites green for the wrong reason.

**Why a unittest suite and not the validator's fixture harness.** Three of these cases create
symlinks, which needs elevation on Windows, and they are guarded here rather than skipped
there.
"""

import contextlib
import io
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import check_plan_tracker  # noqa: E402
from check_plan_tracker import check_tracker, run_tracker_check  # noqa: E402

# Symlink creation needs elevation or developer mode on Windows, so the link cases are
# skipped there rather than weakened. Skipping states the gap; asserting less would hide it
# on the platform where the tracker check was previously never exercised at all.
CAN_SYMLINK = True
try:
    with tempfile.TemporaryDirectory() as _probe:
        _p = Path(_probe)
        (_p / "target").mkdir()
        (_p / "link").symlink_to(_p / "target", target_is_directory=True)
except (OSError, NotImplementedError):  # pragma: no cover - platform dependent
    CAN_SYMLINK = False

NEEDS_SYMLINKS = unittest.skipUnless(CAN_SYMLINK,
                                     "this platform/user cannot create symlinks")

T_HEAD = ("# Execution: T\n\n"
          "_Execution tracker for [plan.md](./plan.md). The orchestrator owns "
          "this file._\n\n## Phases\n\n")
T_VALID = ("- [x] [Phase 1: A](./phase-01-a.md)\n"
           "- [ ] [Phase 2: B](./phase-02-b.md)\n")


class TrackerBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

    def write_plan(self, name: str, body: str, docs=()) -> Path:
        plan_dir = self.root / name
        plan_dir.mkdir()
        for doc in docs:
            (plan_dir / doc).write_text("# Phase\n", encoding="utf-8")
        tracker = plan_dir / "execution.md"
        tracker.write_text(T_HEAD + body, encoding="utf-8")
        return tracker


# (label, tracker body, phase documents on disk, expected diagnostic substring)
REJECTS = [
    # A column-0 `- [` line is the canonical form or it is an error. Silent
    # non-recognition is the failure mode that matters: a typo'd box would simply vanish
    # from the list, and a phase that is not in the list is never executed.
    ("uppercase-X-is-not-canonical",
     "- [X] [Phase 1: A](./phase-01-a.md)\n- [ ] [Phase 2: B](./phase-02-b.md)\n",
     ("phase-02-b.md",), "not the canonical form"),
    ("dash-box-is-not-canonical",
     "- [-] [Phase 1: A](./phase-01-a.md)\n- [ ] [Phase 2: B](./phase-02-b.md)\n",
     ("phase-02-b.md",), "not the canonical form"),
    ("checkbox-without-a-link",
     "- [ ] Phase 1: A\n- [ ] [Phase 2: B](./phase-02-b.md)\n",
     ("phase-02-b.md",), "not the canonical form"),
    # A VALID label on purpose, so the link pattern is the only thing rejecting this line
    # — otherwise the case would stay green with target checking removed.
    ("link-outside-the-phase-name-pattern",
     "- [ ] [Phase 1: Notes](./notes-01.md)\n- [ ] [Phase 2: B](./phase-02-b.md)\n",
     ("phase-02-b.md",), "not the canonical form"),
    ("link-escaping-the-plan-directory",
     "- [ ] [Phase 1: A](../other/phase-01-a.md)\n"
     "- [ ] [Phase 2: B](./phase-02-b.md)\n",
     ("phase-02-b.md",), "not the canonical form"),
    # A slug is kebab-case, so an empty or doubled segment is malformed. The target is
    # deliberately NOT created: the line is rejected on shape, before existence.
    ("malformed-slug-with-an-empty-segment",
     "- [ ] [Phase 1: A](./phase-01--.md)\n- [ ] [Phase 2: B](./phase-02-b.md)\n",
     ("phase-02-b.md",), "not the canonical form"),
    # The link has to resolve to a real phase document.
    ("link-target-missing", T_VALID, ("phase-02-b.md",), "not a regular file"),
    # A phase document on disk that no box links is a phase that never runs.
    ("phase-document-not-listed", T_VALID,
     ("phase-01-a.md", "phase-02-b.md", "phase-03-c.md"), "no checkbox links it"),
    ("phase-listed-twice",
     "- [x] [Phase 1: A](./phase-01-a.md)\n- [x] [Phase 1: A](./phase-01-a.md)\n"
     "- [ ] [Phase 2: B](./phase-02-b.md)\n",
     ("phase-01-a.md", "phase-02-b.md"), "listed twice"),
    # The single most important rule in the design. Zero boxes must be a hard stop, never
    # "all phases complete": that is the disaster the retired marker mechanism nearly
    # caused, a tracker the runner misreads as finished while finalising a plan whose work
    # never ran.
    ("zero-checkboxes", "", (), "no phase checkboxes"),
    # Legacy is identified POSITIVELY. "Zero boxes is an error" and "no boxes means legacy"
    # otherwise describe the same file, so neither a half-migrated tracker nor a stray
    # prose bullet may be waved through as a finished record.
    ("half-migrated-mixes-both-shapes",
     "- [x] [Phase 1: A](./phase-01-a.md)\n\n- phase: phase-02-b\n  status: pending\n",
     ("phase-01-a.md",), "mixes checkbox phases"),
    ("legacy-looking-prose-bullet-naming-no-document",
     "- phase: denotes the superseded syntax\n", (), "no phase checkboxes"),
    # `str.splitlines()` breaks on VT, FF, NEL and U+2028, none of which end a Markdown
    # line. A header and a checkbox separated by one of them are ONE line to every
    # renderer, so a checker that splits there sees boxes nobody else does.
    ("vertical-tab-is-not-a-line-ending",
     "intro\v- [ ] [Phase 1: A](./phase-01-a.md)\n", (), "no phase checkboxes"),
    ("u2028-is-not-a-line-ending",
     "intro\u2028- [ ] [Phase 1: A](./phase-01-a.md)\n", (), "no phase checkboxes"),
    # Unicode decimals are digits to `\d` and to `int()`, so an ordinal written in
    # Arabic-Indic numerals matched the pattern and compared equal to its filename.
    ("non-ascii-digits-in-the-link",
     "- [ ] [Phase 1: A](./phase-\u0660\u0661-a.md)\n"
     "- [ ] [Phase 2: B](./phase-02-b.md)\n",
     ("phase-02-b.md",), "not the canonical form"),
    # The delimiter ban — cheaper than parsing. With no fence, comment or code span
    # anywhere in the file, no region and no line can be hiding something else.
    ("code-fence-in-a-tracker", T_VALID + "```\n",
     ("phase-01-a.md", "phase-02-b.md"), "contains '```'"),
    ("tilde-fence-in-a-tracker", T_VALID + "~~~\n",
     ("phase-01-a.md", "phase-02-b.md"), "contains '~~~'"),
    ("html-comment-opener-in-a-tracker", T_VALID + "<!-- retired\n",
     ("phase-01-a.md", "phase-02-b.md"), "contains '<!--'"),
    # Pinned on its own: an earlier fixture paired `<!--` and `-->` on one line, so
    # deleting the closing delimiter from the ban list left the suite green.
    ("standalone-closing-comment-delimiter", T_VALID + "--> resumed\n",
     ("phase-01-a.md", "phase-02-b.md"), "contains '-->'"),
    # The two ordinals are separate patterns, so a fixture for one proves nothing about
    # the other.
    ("over-long-link-ordinal",
     "- [ ] [Phase 1: A](./phase-0000000001-a.md)\n"
     "- [ ] [Phase 2: B](./phase-02-b.md)\n",
     ("phase-02-b.md",), "not the canonical form"),
    # A slug segment is bounded, so a pathological name is rejected on shape and never
    # reaches the filesystem at all. Forty-one characters is one over the bound and far
    # under any OS limit, so a loosened pattern fails this case by ACCEPTING it rather
    # than by raising ENAMETOOLONG — a mutant killed by a crash proves nothing.
    ("over-long-slug-segment",
     "- [ ] [Phase 1: A](./phase-01-" + "a" * 41 + ".md)\n"
     "- [ ] [Phase 2: B](./phase-02-b.md)\n",
     ("phase-02-b.md",), "not the canonical form"),
    # `*` and `+` are bullet markers too. Scanning only `- [` meant a reader saw a checkbox
    # that the checker ignored and the runner never executed.
    ("asterisk-bullet-checkbox",
     "- [x] [Phase 1: A](./phase-01-a.md)\n"
     "* [ ] [Phase 2: B](./phase-02-b.md)\n",
     ("phase-01-a.md",), "not the canonical form"),
    ("plus-bullet-checkbox",
     "- [x] [Phase 1: A](./phase-01-a.md)\n"
     "+ [ ] [Phase 2: B](./phase-02-b.md)\n",
     ("phase-01-a.md",), "not the canonical form"),
    # `.rstrip()` strips Unicode whitespace, quietly normalising a line the canonical form
    # does not admit. Only ASCII space and tab may follow the link.
    ("non-ascii-trailing-whitespace",
     "- [ ] [Phase 1: A](./phase-01-a.md)\u00a0\n"
     "- [ ] [Phase 2: B](./phase-02-b.md)\n",
     ("phase-02-b.md",), "not the canonical form"),
]


class OneCasePerRule(TrackerBase):
    def test_each_rule_reports_exactly_one_finding(self):
        for label, body, docs, expected in REJECTS:
            with self.subTest(case=label):
                tracker = self.write_plan(label, body, docs)
                found, notices = check_tracker(tracker)
                self.assertEqual(
                    len(found), 1,
                    f"{label} should report exactly one finding containing "
                    f"{expected!r}, got: {found!r}")
                self.assertIn(expected, found[0])
                self.assertFalse(
                    notices,
                    f"{label} is not legacy and must produce no notice, got: {notices!r}")


class PathsThatAreNotRegularFiles(TrackerBase):
    """Cases the table cannot express, because a path is not a regular file at all.

    A symlink is rejected for the same reason the pattern pins `./`: the phase document
    must live in THIS plan directory, and a link resolving elsewhere satisfies "exists"
    while the work sits in another plan. The same applies in the other direction — an
    UNLISTED symlink or directory is still a phase that never runs, so the on-disk
    inventory must not filter by kind before reconciling.
    """

    def _odd(self, name: str, make, body: str, docs=("phase-02-b.md",)):
        plan_dir = self.root / name
        plan_dir.mkdir()
        for doc in docs:
            (plan_dir / doc).write_text("# Phase\n", encoding="utf-8")
        (plan_dir / "execution.md").write_text(T_HEAD + body, encoding="utf-8")
        make(plan_dir)
        return check_tracker(plan_dir / "execution.md")

    def _elsewhere(self) -> Path:
        target = self.root / "elsewhere.md"
        target.write_text("# Phase\n", encoding="utf-8")
        return target

    def _assert_one(self, label, found, expected):
        self.assertEqual(len(found), 1,
                         f"{label} should report exactly one finding containing "
                         f"{expected!r}, got: {found!r}")
        self.assertIn(expected, found[0])

    def test_linked_target_is_a_directory(self):
        found, _ = self._odd("linked-dir", lambda d: (d / "phase-01-a.md").mkdir(),
                             T_VALID)
        self._assert_one("linked target is a directory", found, "not a regular file")

    @NEEDS_SYMLINKS
    def test_linked_target_is_a_symlink(self):
        elsewhere = self._elsewhere()
        found, _ = self._odd("linked-link",
                             lambda d: (d / "phase-01-a.md").symlink_to(elsewhere),
                             T_VALID)
        self._assert_one("linked target is a symlink", found, "not a regular file")

    def test_unlisted_phase_document_is_a_directory(self):
        found, _ = self._odd("unlisted-dir", lambda d: (d / "phase-03-c.md").mkdir(),
                             "- [ ] [Phase 2: B](./phase-02-b.md)\n")
        self._assert_one("unlisted phase document is a directory", found,
                         "no checkbox links it")

    @NEEDS_SYMLINKS
    def test_unlisted_phase_document_is_a_symlink(self):
        elsewhere = self._elsewhere()
        found, _ = self._odd("unlisted-link",
                             lambda d: (d / "phase-03-c.md").symlink_to(elsewhere),
                             "- [ ] [Phase 2: B](./phase-02-b.md)\n")
        self._assert_one("unlisted phase document is a symlink", found,
                         "no checkbox links it")


class LegacyIsIdentifiedPositively(TrackerBase):
    """Every `- phase:` line must be a well-formed entry naming a phase document HERE.

    Matching a bare token let one malformed line retire a whole tracker from checking: a
    traversing slug, trailing junk, or an empty entry each read as "legacy". These keep a
    real phase document on disk on purpose — that is what makes the SLUG the reason for
    rejection rather than mere non-existence — and it costs a second, cascading "not
    listed" finding, so they assert the ABSENCE OF A LEGACY NOTICE rather than a finding
    count. That absence is exactly what goes red if the strictness is reverted.
    """

    CASES = [
        # The traversal target exists, one directory up: only the slug pattern (which
        # admits no path separator) stands between it and a skipped tracker.
        ("legacy-entry-traversing-out-of-the-plan-directory",
         "- phase: ../elsewhere/phase-01-a\n", ()),
        ("legacy-entry-with-trailing-junk",
         "- phase: phase-01-a (superseded)\n",
         ("phase-01-a.md", "phase-01-a (superseded).md")),
        ("legacy-entry-that-is-empty",
         "- phase: phase-01-a\n- phase:\n", ("phase-01-a.md",)),
        # A well-formed slug naming NO document. Without this the missing-target half of
        # the predicate had no case of its own: every other entry here is rejected by the
        # slug pattern first, so dropping the existence check stayed green.
        ("legacy-entry-naming-a-missing-document",
         "- phase: phase-01-a\n", ()),
        # `\s` and `.strip()` both admit Unicode whitespace. A vertical tab in the list
        # marker, or NEL where the space belongs, normalised a malformed line into a
        # well-formed entry and retired the whole file from checking.
        ("legacy-entry-with-a-vertical-tab-list-marker",
         "-\vphase: phase-01-a\n", ("phase-01-a.md",)),
        ("legacy-entry-separated-by-NEL",
         "- phase:\u0085phase-01-a\n", ("phase-01-a.md",)),
    ]

    def setUp(self):
        super().setUp()
        (self.root / "elsewhere").mkdir()
        (self.root / "elsewhere" / "phase-01-a.md").write_text("# Phase\n",
                                                               encoding="utf-8")

    def _assert_not_legacy(self, label, found, notices):
        self.assertFalse(notices,
                         f"{label} must NOT be classified as legacy; notices {notices!r}")
        self.assertTrue(any("no phase checkboxes" in e for e in found),
                        f"{label} must NOT be classified as legacy; errors {found!r}")

    def test_malformed_entries_do_not_retire_a_tracker(self):
        for label, body, docs in self.CASES:
            with self.subTest(case=label):
                tracker = self.write_plan(label, body, docs)
                self._assert_not_legacy(label, *check_tracker(tracker))

    @NEEDS_SYMLINKS
    def test_a_legacy_entry_naming_a_symlink_does_not_retire_a_tracker(self):
        elsewhere = self.root / "elsewhere.md"
        elsewhere.write_text("# Phase\n", encoding="utf-8")
        tracker = self.write_plan("legacy-entry-naming-a-symlink",
                                  "- phase: phase-01-a\n", ())
        (tracker.parent / "phase-01-a.md").symlink_to(elsewhere)
        self._assert_not_legacy("legacy-entry-naming-a-symlink", *check_tracker(tracker))

    def test_a_commented_out_legacy_entry_does_not_retire_a_tracker(self):
        """Block delimiters are rejected BEFORE a file can be classified as anything.

        A comment can hide the very entries the legacy test reads, so a file carrying one
        is not identifiable as a legacy record at all.
        """
        hidden = self.write_plan("commented-out-legacy",
                                 "<!--\n- phase: phase-01-a\n-->\n", ("phase-01-a.md",))
        found, notices = check_tracker(hidden)
        self.assertFalse(notices)
        self.assertTrue(any("contains '<!--'" in e for e in found),
                        f"got errors {found!r}, notices {notices!r}")

    def test_a_real_legacy_record_is_reported_and_skipped(self):
        """Reported by path, skipped, and NOT an error.

        The phase documents are on disk both because identification requires it — every
        `- phase:` slug must name a real phase document — and so that silence proves the
        early return happens before the directory rules rather than merely agreeing.
        """
        legacy = self.write_plan(
            "legacy-record",
            "- phase: phase-01-a\n  status: done\n  shared-surfaces: none\n\n"
            "- phase: phase-02-b\n  status: done\n  shared-surfaces: none\n",
            ("phase-01-a.md", "phase-02-b.md"))
        found, notices = check_tracker(legacy)
        self.assertFalse(found)
        self.assertEqual(len(notices), 1)
        self.assertIn("legacy", notices[0])


class WellFormedTrackersAreAccepted(TrackerBase):
    """Without this, every rejection above could be satisfied by a rule rejecting all."""

    CASES = [
        ("a well-formed tracker", T_VALID, ("phase-01-a.md", "phase-02-b.md")),
        # Independence is noted in PROSE above the list and is not parsed. A `###`
        # sub-heading would be wrong: in Markdown a blank line does not end a `###`
        # section, so the closing phase would read as another group member.
        ("prose above the list, and an indented checklist below it",
         "Phases 1 and 2 are independent; nothing reconciles them.\n\n" + T_VALID
         + "\nNotes:\n\n  - [ ] an indented note, not a phase\n",
         ("phase-01-a.md", "phase-02-b.md")),
        # Three digits, and a multi-segment kebab-case slug.
        ("a long-numbered phase with a multi-segment slug",
         "- [ ] [Phase 100: Long](./phase-100-a-longer-slug.md)\n",
         ("phase-100-a-longer-slug.md",)),
        # ...and the line endings that ARE line endings still are. Splitting only on
        # CRLF/CR/LF must not start rejecting a CRLF file, which is what a Windows editor
        # produces.
        ("a tracker with CRLF line endings",
         T_VALID.replace("\n", "\r\n"), ("phase-01-a.md", "phase-02-b.md")),
    ]

    def test_accepted(self):
        for label, body, docs in self.CASES:
            with self.subTest(case=label):
                tracker = self.write_plan(label, body, docs)
                found, notices = check_tracker(tracker)
                self.assertFalse(found, f"{label} must be accepted — errors {found!r}")
                self.assertFalse(notices,
                                 f"{label} must be accepted — notices {notices!r}")


class TheCommandLineBranch(TrackerBase):
    """Scope is decided by FILENAME, notices are PRINTED, and errors set the exit code.

    Exercised through `run_tracker_check` rather than by reading `main()`, because "the
    notice is printed" is the whole point of reporting legacy by path.
    """

    def setUp(self):
        super().setUp()
        self.write_plan("live", T_VALID, ("phase-01-a.md", "phase-02-b.md"))
        # A v1 `phases.md` sitting in the same tree. It is a checkbox tracker too, so the
        # two shapes no longer differ — the v1<->v2 non-collision is now the FILENAME
        # alone. Its links are dangling on purpose: if the scan ever widened past
        # `execution.md`, this would fail loudly instead of silently passing.
        self.v1 = self.root / "v1-plan"
        self.v1.mkdir()
        (self.v1 / "phases.md").write_text(
            T_HEAD + "- [ ] [Phase 1: Gone](./phase-01-gone.md)\n", encoding="utf-8")

    @staticmethod
    def _run(target):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = run_tracker_check(target)
        return code, buf.getvalue()

    def test_a_directory_scan_inspects_only_execution_md(self):
        code, out = self._run(self.root)
        self.assertEqual(code, 0,
                         f"a sibling v1 phases.md made the scan exit {code}: {out!r}")
        self.assertIn("1 tracker", out)

    def test_an_explicit_file_path_is_checked_whatever_it_is_called(self):
        """Which is how `plan-phase` verifies a tracker it has just written."""
        code, out = self._run(self.v1 / "phases.md")
        self.assertEqual(code, 1)
        self.assertIn("not a regular file", out)

    def test_a_legacy_tracker_is_printed_by_path_and_does_not_fail_the_run(self):
        self.write_plan("retired", "- phase: phase-01-a\n  status: done\n",
                        ("phase-01-a.md",))
        code, out = self._run(self.root)
        self.assertEqual(code, 0, f"exit {code}: {out!r}")
        self.assertIn("legacy", out)
        self.assertIn("retired", out)

    def test_a_missing_path_is_reported_rather_than_raising(self):
        code, out = self._run(self.root / "nope")
        self.assertEqual(code, 1)
        self.assertIn("not found", out)


class TheEntrypointIsEncodingSafe(unittest.TestCase):
    """The shipped entrypoints all reconfigure their streams; this one is no exception.

    Diagnostics quote plan headings, which carry em dashes. Under a non-UTF-8 console —
    what the CI encoding proxy simulates — printing a finding would raise
    UnicodeEncodeError and take down the run reporting the problem rather than the
    problem itself.
    """

    def test_main_reconfigures_both_streams(self):
        import ast
        src = Path(check_plan_tracker.__file__).read_text(encoding="utf-8")
        tree = ast.parse(src)
        main = next(n for n in tree.body
                    if isinstance(n, ast.FunctionDef) and n.name == "main")
        calls = [n for n in ast.walk(main)
                 if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                 and n.func.attr == "reconfigure"]
        self.assertTrue(calls, "main() must reconfigure its streams to utf-8")


if __name__ == "__main__":
    unittest.main()
