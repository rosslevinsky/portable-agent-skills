#!/usr/bin/env python3
"""Tests for `install.py`, the single-file installer.

Organised around the promises the installer makes, because those are what a reader needs to
trust and what a future edit must not quietly withdraw:

  * it installs what it says, into every target, and records it;
  * it removes **only** what it recorded, so a user's own skill survives;
  * an interrupted install is REPAIRED BY RUNNING IT AGAIN. A partial copy does become the
    live directory, and that is the design: ownership is recorded before any file is touched,
    so the half-copied skill is claimed by the manifest, `--verify` reports it, and one re-run
    restores it byte-identical. Asserted by killing a copy mid-flight, not assumed;
  * a link is unlinked, never followed — the hazard that cost the retired PowerShell
    installer a hand-written `Remove-SkillPath`;
  * an install made by the shell installers can still be read, updated and removed.

**These tests could not be written red first**, because the code they cover is new. So the
ones that carry real load are mutation-tested instead — `MutationProofs` breaks the
implementation in a specific way and asserts the relevant test notices.

Nothing here touches a real skills directory. Every case runs against a temporary tree.
"""

import contextlib
import glob
import io
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import unittest.mock
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import install  # noqa: E402

# Symlink creation needs elevation or developer mode on Windows, so the link cases are
# skipped there rather than weakened. Skipping states the gap; asserting less would hide
# it on the platform where the hazard was originally found.
CAN_SYMLINK = True
try:
    with tempfile.TemporaryDirectory() as _probe:
        _p = Path(_probe)
        (_p / "target").mkdir()
        (_p / "link").symlink_to(_p / "target", target_is_directory=True)
except (OSError, NotImplementedError):  # pragma: no cover - platform dependent
    CAN_SYMLINK = False

NEEDS_SYMLINKS = unittest.skipUnless(CAN_SYMLINK, "this platform/user cannot create symlinks")


def _current_umask() -> int:
    """Read the umask without leaving it changed."""
    value = os.umask(0)
    os.umask(value)
    return value


def make_source(root: Path, names=("alpha", "beta")) -> Path:
    """A miniature pack: directories holding a SKILL.md, which is what discovery looks for."""
    source = root / "skills"
    for name in names:
        skill = source / name
        (skill / "references").mkdir(parents=True)
        (skill / "SKILL.md").write_text(f"# {name}\n", encoding="utf-8")
        (skill / "references" / "notes.md").write_text(f"{name} notes\n", encoding="utf-8")
    # A directory with no SKILL.md is not a skill and must not be installed.
    (source / "not-a-skill").mkdir()
    (source / "not-a-skill" / "README.md").write_text("bystander\n", encoding="utf-8")
    return source


class InstallBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.source = make_source(self.root)
        self.target = self.root / "target"
        self.target.mkdir()

    def install(self, source=None, target=None):
        return install.do_install([target or self.target], source or self.source)

    def manifest(self, target=None):
        return install.read_manifest(target or self.target)


class ItInstallsWhatItSays(InstallBase):
    def test_every_skill_lands_with_its_subdirectories(self):
        self.assertEqual(self.install(), 0)
        self.assertEqual((self.target / "alpha" / "SKILL.md").read_text(encoding="utf-8"),
                         "# alpha\n")
        self.assertEqual(
            (self.target / "beta" / "references" / "notes.md").read_text(encoding="utf-8"),
            "beta notes\n",
            "a skill's subdirectories must travel with it — references/ is shipped content")

    def test_a_directory_without_a_skill_md_is_not_a_skill(self):
        """Discovery is 'holds a SKILL.md', not 'is a directory under skills/'."""
        self.install()
        self.assertFalse((self.target / "not-a-skill").exists())
        self.assertNotIn("not-a-skill", self.manifest()[0])

    def test_the_manifest_lists_what_landed_and_names_the_version(self):
        self.install()
        names, meta = self.manifest()
        self.assertEqual(names, ["alpha", "beta"])
        self.assertTrue(meta.get("version"), "the manifest must record which version this is")

    def test_reinstalling_is_idempotent(self):
        self.install()
        (self.target / "alpha" / "SKILL.md").write_text("edited by hand\n", encoding="utf-8")
        self.install()
        self.assertEqual((self.target / "alpha" / "SKILL.md").read_text(encoding="utf-8"),
                         "# alpha\n", "an update must replace the installed copy")
        self.assertEqual(self.manifest()[0], ["alpha", "beta"])

    def test_a_retired_skill_is_removed_on_update(self):
        """A skill dropped from the pack must not keep installing for everyone.

        It goes through the manifest rather than by comparing directories: uninstall of the
        old set happens implicitly because the new manifest no longer lists it, and the next
        uninstall would otherwise leave it permanently unowned.
        """
        self.install()
        shutil.rmtree(self.source / "beta")
        self.install()
        self.assertEqual(self.manifest()[0], ["alpha"])
        install.do_uninstall([self.target])
        self.assertFalse((self.target / "alpha").exists())


class ItOwnsOnlyWhatItRecorded(InstallBase):
    def test_a_users_own_skill_survives_uninstall(self):
        """The reason the manifest exists at all.

        Without a record of what was installed, uninstall would have to guess from the
        directory, and a guess deletes work the user put there.
        """
        self.install()
        mine = self.target / "my-own-skill"
        mine.mkdir()
        (mine / "SKILL.md").write_text("mine\n", encoding="utf-8")
        install.do_uninstall([self.target])
        self.assertTrue((mine / "SKILL.md").is_file(),
                        "uninstall removed a directory it never installed")

    def test_uninstall_removes_only_listed_names(self):
        self.install()
        install.write_manifest(self.target, ["alpha"], "test")
        install.do_uninstall([self.target])
        self.assertFalse((self.target / "alpha").exists())
        self.assertTrue((self.target / "beta").exists(),
                        "an unlisted directory must be left alone even if this pack ships it")

    def test_uninstall_with_no_manifest_removes_nothing(self):
        self.install()
        (self.target / install.MANIFEST_NAME).unlink()
        install.do_uninstall([self.target])
        self.assertTrue((self.target / "alpha").exists(),
                        "with no record of ownership the answer is to touch nothing")


class LinksAreUnlinkedNeverFollowed(InstallBase):
    @NEEDS_SYMLINKS
    def test_removing_a_linked_skill_leaves_its_target_alone(self):
        """A recursive delete that follows a link deletes the target's contents, which is
        how a link at a skill name destroys the real checkout behind it. The correct removal
        for a link is to detach the link itself."""
        precious = self.root / "precious"
        precious.mkdir()
        (precious / "SKILL.md").write_text("keep me\n", encoding="utf-8")
        link = self.target / "linked"
        link.symlink_to(precious, target_is_directory=True)

        install._remove(link)

        self.assertFalse(link.exists())
        self.assertTrue((precious / "SKILL.md").is_file(),
                        "removal followed the link and destroyed the target")

    @NEEDS_SYMLINKS
    def test_uninstall_of_a_linked_skill_does_not_delete_the_source(self):
        """A linked install — however it was made — must be removable without data loss."""
        precious = self.root / "precious2"
        precious.mkdir()
        (precious / "SKILL.md").write_text("keep me too\n", encoding="utf-8")
        (self.target / "alpha").symlink_to(precious, target_is_directory=True)
        install.write_manifest(self.target, ["alpha"], "test")

        install.do_uninstall([self.target])

        self.assertFalse((self.target / "alpha").exists())
        self.assertTrue((precious / "SKILL.md").is_file())

    def test_a_junction_is_never_handed_to_the_recursive_delete(self):
        """A Windows junction answers False to `is_symlink()` and True to `is_dir()`, so a
        removal branching on `is_symlink()` hands it to `shutil.rmtree`. `rmtree` refuses
        it — it reads the reparse tag and raises rather than descending — so the cost is a
        failed update and not a deleted checkout. The branch is still wrong: the caller
        wanted the junction detached and gets an error naming a symbolic link instead, on a
        path nobody linked, after the retry loop has run out.

        Junctions cannot be created on this platform, so the shape is built directly: a path
        `_is_link` calls a link and `is_symlink` does not. Branching on `is_symlink()` again
        calls `rmtree` here and fails this test.
        """
        junction = self.target / "junction"
        junction.mkdir()
        (junction / "inside").write_text("x", encoding="utf-8")
        real_is_link = install._is_link

        with unittest.mock.patch.object(
                install, "_is_link",
                lambda path: Path(path) == junction or real_is_link(path)), \
             unittest.mock.patch.object(install.shutil, "rmtree") as rmtree:
            try:
                install._remove(junction, attempts=1)
            except OSError:
                pass  # `rmdir` refuses the non-empty stand-in; the call under test is above

        rmtree.assert_not_called()

    def test_a_directory_reparse_point_is_detached_with_rmdir(self):
        """`unlink` refuses a directory reparse point on Windows, so the removal falls
        through to `rmdir`, which detaches the link without touching what it points at.
        The stand-in is an empty directory for the same reason: `unlink` refuses it here
        too, which is the branch being exercised.

        **Anchored on which call removed it, not on the path being gone.** `shutil.rmtree`
        deletes an empty stand-in just as happily, so asserting only that the directory
        disappeared passes against the pre-fix branch as well and proves nothing.
        """
        stand_in = self.target / "dirlink"
        stand_in.mkdir()
        real_is_link = install._is_link

        with unittest.mock.patch.object(
                install, "_is_link",
                lambda path: Path(path) == stand_in or real_is_link(path)), \
             unittest.mock.patch.object(
                install.os, "rmdir", wraps=install.os.rmdir) as rmdir, \
             unittest.mock.patch.object(install.shutil, "rmtree") as rmtree:
            install._remove(stand_in)

        rmdir.assert_called_once_with(stand_in)
        rmtree.assert_not_called()
        self.assertFalse(stand_in.exists(), "the reparse point was never detached")

    def test_the_pre_312_fallback_reads_the_reparse_tag_and_not_the_bit(self):
        """Every reparse point sets FILE_ATTRIBUTE_REPARSE_POINT, and most of them are not
        links: a cloud-storage placeholder directory and a ProjFS root both carry the bit.
        On 3.10 and 3.11 there is no `is_junction()` to ask, so the fallback must read the
        reparse TAG.

        Testing the bit alone calls an ordinary synced directory a link, and `_remove` then
        tries to detach a real non-empty directory — `unlink` refuses a directory, `rmdir`
        refuses a non-empty one, and an update that used to succeed fails instead. A user
        whose skills sit under a synced home directory meets that on every skill they have.
        """
        reparse_bit = 0x400
        symlink, junction, cloud = 0xA000000C, 0xA0000003, 0x9000101A

        class FakeStat:
            def __init__(self, tag):
                self.st_file_attributes = reparse_bit
                self.st_reparse_tag = tag

        class PreThreeTwelvePath:
            """A 3.10/3.11 `Path`: no `is_junction`, and `lstat` sees a reparse point."""
            is_junction = None

            def __init__(self, tag):
                self._tag = tag

            def is_symlink(self):
                return False

            def lstat(self):
                return FakeStat(self._tag)

        self.assertTrue(install._is_link(PreThreeTwelvePath(symlink)))
        self.assertTrue(install._is_link(PreThreeTwelvePath(junction)))
        self.assertFalse(install._is_link(PreThreeTwelvePath(cloud)),
                         "a cloud-storage placeholder directory is not a link")

    @NEEDS_SYMLINKS
    def test_a_target_that_cannot_be_resolved_is_refused(self):
        """A symlink loop is how `resolve()` reports that it cannot answer. The comparisons
        against the source are what stop an install deleting the pack it copies from, and a
        target whose real path is unknown cannot be shown *not* to be the source — so it is
        refused. Skipping past it and installing anyway leaves that guard open in the one
        case it exists for.
        """
        looped = self.root / "loop-a"
        looped.symlink_to(self.root / "loop-b")
        (self.root / "loop-b").symlink_to(looped)
        err = io.StringIO()

        with contextlib.redirect_stderr(err):
            code = install.do_install([looped], self.source)

        self.assertEqual(code, 1)
        self.assertIn("cannot be resolved", err.getvalue())


class ItReadsAnInstallMadeByTheShellInstallers(InstallBase):
    """A user is not asked to uninstall with the old script before using this one."""

    LEGACY = (
        "# Portable Agent Skills manifest\n"
        "# installed-at: 2026-08-16T16:57:55Z\n"
        "# source-commit: unknown\n"
        "# source-version: unknown\n"
        "alpha\n"
        "beta\n"
    )

    def test_a_legacy_manifest_is_understood(self):
        (self.target / install.MANIFEST_NAME).write_text(self.LEGACY, encoding="utf-8")
        names, meta = self.manifest()
        self.assertEqual(names, ["alpha", "beta"])
        self.assertEqual(meta.get("source-version"), "unknown")

    def test_a_legacy_install_can_be_removed(self):
        for name in ("alpha", "beta"):
            (self.target / name).mkdir()
            (self.target / name / "SKILL.md").write_text("old\n", encoding="utf-8")
        (self.target / install.MANIFEST_NAME).write_text(self.LEGACY, encoding="utf-8")
        install.do_uninstall([self.target])
        self.assertFalse((self.target / "alpha").exists())
        self.assertFalse((self.target / install.MANIFEST_NAME).exists())


class TheVersionTravelsWithTheFiles(unittest.TestCase):
    """The defect this replaced: provenance read from git, absent from a downloaded copy.

    Measured on a real install before the change — `source-commit: unknown` and
    `source-version: unknown`, which is exactly the case where the manifest is the only
    record there is.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

    def _version_with_changelog(self, text):
        (self.root / "CHANGELOG.md").write_text(text, encoding="utf-8")
        with unittest.mock.patch.object(install, "REPO_ROOT", self.root):
            return install.pack_version()

    def test_the_version_comes_from_the_changelog_heading(self):
        self.assertEqual(
            self._version_with_changelog("# Changelog\n\n## [2026.04.0] - 2026-04-16\n"),
            "2026.04.0")

    def test_unreleased_is_skipped(self):
        """A working tree reports the last real release, not a word naming no release."""
        self.assertEqual(
            self._version_with_changelog(
                "# Changelog\n\n## [Unreleased]\n\n## [2026.04.0] - 2026-04-16\n"),
            "2026.04.0")

    def test_a_tree_with_no_changelog_and_no_git_says_unknown(self):
        """Honest last resort. It must never invent a version."""
        with unittest.mock.patch.object(install, "REPO_ROOT", self.root), \
                unittest.mock.patch.object(install.subprocess, "run",
                                           side_effect=OSError("no git")):
            self.assertEqual(install.pack_version(), "unknown")

    def test_the_real_pack_reports_a_version_from_a_copy_with_no_git(self):
        """End to end, on this repository's own changelog, with `.git` unreachable."""
        shutil.copy(REPO_ROOT / "CHANGELOG.md", self.root / "CHANGELOG.md")
        with unittest.mock.patch.object(install, "REPO_ROOT", self.root):
            version = install.pack_version()
        self.assertNotEqual(version, "unknown")
        self.assertNotIn("unreleased", version.lower())


class BothRuntimesAreInstalledTo(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

    def test_the_defaults_name_a_directory_for_each_runtime(self):
        """`~/.agents/skills` is Codex's documented user scope, shared with several other
        agents; `~/.codex/skills` holds configuration, not skills."""
        with unittest.mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("CLAUDE_SKILLS_DIR", None)
            os.environ.pop("CODEX_SKILLS_DIR", None)
            targets = [str(p) for p in install.default_targets()]
        self.assertEqual(len(targets), 2)
        self.assertTrue(any(p.endswith(os.path.join(".claude", "skills")) for p in targets))
        self.assertTrue(any(p.endswith(os.path.join(".agents", "skills")) for p in targets))

    def test_each_target_gets_its_own_manifest(self):
        """So a run that finishes one target and dies on the other is VISIBLE rather than
        hidden: each directory describes itself truthfully."""
        source = make_source(self.root)
        a, b = self.root / "a", self.root / "b"
        with unittest.mock.patch.dict(
                os.environ, {"CLAUDE_SKILLS_DIR": str(a), "CODEX_SKILLS_DIR": str(b)}):
            install.main(["--source", str(source)])
        for target in (a, b):
            names, meta = install.read_manifest(target)
            self.assertEqual(names, ["alpha", "beta"])
            self.assertTrue(meta.get("version"))


class VerifyReportsWithoutRepairing(InstallBase):
    def test_a_clean_install_verifies(self):
        self.install()
        self.assertEqual(install.do_verify([self.target], self.source), 0)

    def test_a_missing_skill_is_reported_as_a_failure(self):
        self.install()
        shutil.rmtree(self.target / "alpha")
        self.assertEqual(install.do_verify([self.target], self.source), 1)

    def test_a_skill_not_yet_installed_is_a_failure(self):
        """The pack ships it and this target does not have it: that is not a match.

        A state an update fixes is still a state the check exists to report, and a gate that
        keys on the exit code learns nothing from a line of text.
        """
        self.install()
        install.write_manifest(self.target, ["alpha"], "test")
        self.assertEqual(install.do_verify([self.target], self.source), 1)


class MutationProofs(InstallBase):
    """The tests above could not be written red first, so they are checked by breaking the
    code and confirming the right one notices.

    Two review rounds turned on tests that passed while asserting nothing — including one
    that spawned a shell which could not run. A new suite with no demonstrated failure is in
    that category until someone shows otherwise.
    """

    def test_copying_before_recording_ownership_is_caught(self):
        """The stranded install is prevented by REFUSING TO COPY when the record cannot be
        written. This mutates that away — ownership recorded only at the end — and confirms
        the bad state is reachable again, which is what makes
        `OwnershipIsRecordedBeforeAnyFileIsTouched` a real test rather than a description.

        The mutation is applied to `write_manifest` itself rather than by obstructing the
        manifest path: an obstructed path is refused at the READ, before the ordering under
        test is reached.
        """
        calls = []

        def record_only_at_the_end(target, names, version):
            calls.append(names)
            if len(calls) == 1:
                return  # the mutation: the intent write does nothing
            raise OSError(28, "no space left on device")  # and the settle write fails

        with unittest.mock.patch.object(install, "write_manifest", record_only_at_the_end):
            install.do_install([self.target], self.source)

        self.assertTrue(
            (self.target / "alpha").is_dir(),
            "the mutation did not reach the copy step, so it proves nothing")
        self.assertEqual(
            install.read_manifest(self.target)[0], [],
            "skills are on disk with no record of who owns them — the state the ordering "
            "exists to prevent, so the ordering test is measuring something real")

    def test_uninstalling_by_directory_listing_is_caught(self):
        """If uninstall walked the directory instead of the manifest, a user's own skill
        would go with it — which `ItOwnsOnlyWhatItRecorded` must see."""
        self.install()
        mine = self.target / "my-own-skill"
        mine.mkdir()
        (mine / "SKILL.md").write_text("mine\n", encoding="utf-8")

        def uninstall_everything(targets):
            for target in targets:
                for entry in list(target.iterdir()):
                    if entry.name != install.MANIFEST_NAME:
                        install._remove(entry)
            return 0

        with unittest.mock.patch.object(install, "do_uninstall", uninstall_everything):
            install.do_uninstall([self.target])
        self.assertFalse(mine.exists(),
                         "the mutation left the user's skill alone, so the ownership "
                         "test is not actually exercising ownership")


class ThreeGapsTheOldSuiteNamed(unittest.TestCase):
    """Behaviours the bash suite covered that the first draft of `install.py` dropped.

    Found by READING the suite being deleted rather than deleting it. All three were
    reproduced before they were fixed, which is the only reason to trust the fix — and the
    reason a replacement should be read against what it replaces.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.source = make_source(self.root)
        self.target = self.root / "target"
        self.target.mkdir()

    def test_a_manifest_line_cannot_reach_outside_the_target(self):
        """The manifest is an editable text file whose every line drives a removal.

        A line reading `../outside` made `--uninstall` delete a sibling of the skills
        directory. The bash installer reported such a line as `INVALID:`, which is why the
        old suite had a case for it.
        """
        outside = self.root / "outside"
        outside.mkdir()
        (outside / "precious.txt").write_text("keep\n", encoding="utf-8")
        (self.target / install.MANIFEST_NAME).write_text(
            "# manifest\n../outside\n", encoding="utf-8")

        install.do_uninstall([self.target])

        self.assertTrue((outside / "precious.txt").is_file(),
                        "a manifest entry escaped the target and deleted a sibling")

    def test_unsafe_names_are_rejected_in_every_shape(self):
        for name in ("../outside", "..", ".", "", "/etc", "a/b", "a\\b"):
            self.assertFalse(install.is_safe_name(name), f"{name!r} was accepted")
        for name in ("cyw", "plan-run", "security-review-codebase"):
            self.assertTrue(install.is_safe_name(name), f"{name!r} was rejected")

    def test_a_retired_skill_is_pruned_from_disk_not_only_from_the_manifest(self):
        """Otherwise it loads for ever and nothing can remove it.

        Discovery globs for SKILL.md with no allowlist, so a skill dropped from the pack
        keeps being read. Rewriting the manifest without it makes it worse: it is then
        unowned, so a later uninstall will not touch it either.
        """
        install.do_install([self.target], self.source)
        shutil.rmtree(self.source / "beta")
        install.do_install([self.target], self.source)
        self.assertFalse((self.target / "beta").exists(),
                         "a retired skill was dropped from the manifest but left on disk")

    def test_only_previously_owned_names_are_pruned(self):
        """The prune must not become a second way to delete a user's directory."""
        install.do_install([self.target], self.source)
        mine = self.target / "my-own-skill"
        mine.mkdir()
        (mine / "SKILL.md").write_text("mine\n", encoding="utf-8")
        install.do_install([self.target], self.source)
        self.assertTrue((mine / "SKILL.md").is_file())

    def test_an_unowned_skill_is_not_replaced_without_force(self):
        """A directory this installer never recorded belongs to the user.

        An ordinary update silently overwrote it, with nothing to say it had happened.
        """
        (self.target / "alpha").mkdir()
        (self.target / "alpha" / "SKILL.md").write_text("MINE\n", encoding="utf-8")

        result = install.do_install([self.target], self.source)

        self.assertEqual((self.target / "alpha" / "SKILL.md").read_text(encoding="utf-8"),
                         "MINE\n", "an update replaced a directory it did not own")
        self.assertEqual(result, 1, "skipping a skill must be reported as a failure")
        self.assertNotIn("alpha", install.read_manifest(self.target)[0],
                         "a skill that was skipped must not be claimed by the manifest")

    def test_force_replaces_an_unowned_skill(self):
        (self.target / "alpha").mkdir()
        (self.target / "alpha" / "SKILL.md").write_text("MINE\n", encoding="utf-8")
        install.do_install([self.target], self.source, force=True)
        self.assertEqual((self.target / "alpha" / "SKILL.md").read_text(encoding="utf-8"),
                         "# alpha\n")


class OwnershipSurvivesAFailure(InstallBase):
    """A skill that is still on disk must still be claimed by the manifest.

    Ownership here is nothing but the manifest: there are no hashes and no per-skill record,
    and a directory this installer does not claim is one it will not touch. So dropping a
    name while its directory is still there permanently converts an installed skill into one
    nothing can remove and nothing can update without `--force`, while the user is told the
    opposite.

    Both directions were wrong the same way round: the record was rewritten from what
    SUCCEEDED rather than from what is actually there.
    """

    def _remove_that_fails_for(self, name):
        real_remove = install._remove

        def fake_remove(path):
            if path.name == name:
                raise OSError(13, "Permission denied")
            return real_remove(path)

        return unittest.mock.patch.object(install, "_remove", fake_remove)

    def test_uninstall_keeps_the_record_of_what_it_could_not_remove(self):
        self.install()
        with self._remove_that_fails_for("alpha"):
            result = install.do_uninstall([self.target])

        self.assertEqual(result, 1, "a failed removal must be reported as a failure")
        self.assertTrue((self.target / "alpha").is_dir(), "the fixture did not hold")
        names, _meta = self.manifest()
        self.assertIn(
            "alpha", names,
            "the manifest was deleted while alpha was still installed — a second "
            "--uninstall now prints 'nothing owned here' and exits 0 over a skill that is "
            "still on disk, and the next install refuses it without --force")
        self.assertNotIn(
            "beta", names,
            "beta was removed successfully and must not still be claimed")

    def test_uninstall_removes_the_manifest_when_everything_went(self):
        """The positive control: the fix must not simply stop deleting the manifest."""
        self.install()
        self.assertEqual(install.do_uninstall([self.target]), 0)
        self.assertFalse(
            (self.target / install.MANIFEST_NAME).exists(),
            "a clean uninstall leaves nothing behind, manifest included")

    def test_a_failed_update_keeps_owning_the_skill_it_left_on_disk(self):
        """The same defect on the install path.

        `install_skill` stages beside the destination, so a copy that dies leaves the LIVE
        directory untouched — the skill is still installed, at its previous version.
        Rewriting the manifest from the successes alone then orphans it, so one transient
        file lock on Windows makes an installed skill unremovable.
        """
        self.install()
        real_copytree = shutil.copytree
        depth = {"n": 0}

        def failing_copytree(*args, **kwargs):
            depth["n"] += 1
            try:
                result = real_copytree(*args, **kwargs)
            finally:
                depth["n"] -= 1
            if depth["n"] == 0 and Path(args[0]).name == "alpha":
                raise OSError(28, "No space left on device")
            return result

        with unittest.mock.patch.object(install.shutil, "copytree", failing_copytree):
            result = install.do_install([self.target], self.source)

        self.assertEqual(result, 1, "a failed copy must be reported as a failure")
        self.assertTrue((self.target / "alpha").is_dir(), "the fixture did not hold")
        self.assertIn(
            "alpha", self.manifest()[0],
            "the update dropped alpha from the manifest while leaving it installed")

    def test_the_manifest_is_the_same_bytes_on_every_platform(self):
        """`discover_skills` sorts by plain name so this file is byte-identical between runs
        on different machines. The sort was the only half that was true.

        `write_text` in text mode translates `\\n` to `os.linesep`, so Windows wrote CRLF and
        the same install produced different bytes depending on who ran it. Asserted on the
        bytes, since that is the claim; reading was never affected, which is why nothing
        noticed.
        """
        self.install()
        raw = (self.target / install.MANIFEST_NAME).read_bytes()
        self.assertNotIn(b"\r\n", raw, "the manifest was written with CRLF line endings")
        self.assertEqual(
            raw.decode("utf-8").splitlines()[-2:], ["alpha", "beta"],
            "the fixture no longer describes what is written")

    def test_a_skipped_unowned_directory_is_still_not_claimed(self):
        """The boundary the fix must not cross.

        Keeping ownership of what survives must not become claiming whatever is present: a
        directory this installer never recorded belongs to the user, and an update that skips
        it must leave it unclaimed.
        """
        (self.target / "gamma").mkdir()
        (self.target / "gamma" / "SKILL.md").write_text("MINE\n", encoding="utf-8")
        install.do_install([self.target], self.source)
        self.assertNotIn("gamma", self.manifest()[0])


class OutputSurvivesANonUtf8Console(unittest.TestCase):
    """The first command a user runs must not die on the encoding of its own report.

    Every mode of this installer prints an em dash. On a Windows box whose console code page
    is cp932 or cp949 — or under the CI encoding proxy, which is stricter still — `sys.stdout`
    gets a codec that cannot represent one, and printing the report raises
    `UnicodeEncodeError` after the work is done.

    Run as a subprocess with output redirected, because that is the condition: an interactive
    console and a pipe get different encodings, and an in-process test would inherit this
    suite's stdout rather than the one a user has.
    """

    ASCII_ENV = {"PYTHONUTF8": "0", "LC_ALL": "C", "LANG": "C"}

    def _run(self, *args) -> subprocess.CompletedProcess:
        env = os.environ.copy()
        env.update(self.ASCII_ENV)
        return subprocess.run(
            [sys.executable, str(REPO_ROOT / "install.py"), *args],
            # `encoding=`, not `text=True`. Text mode decodes with the PARENT's locale
            # encoding, and the parent here is a test run under the very ASCII locale it is
            # imposing on the child — so reading a report containing an em dash raised
            # UnicodeDecodeError in this method, not in install.py. Decoding as UTF-8 is also
            # the claim: install.py pins its output to UTF-8.
            capture_output=True, encoding="utf-8", errors="replace",
            env=env, cwd=str(REPO_ROOT),
        )

    def test_every_mode_prints_its_report_under_an_ascii_locale(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "target"
            target.mkdir()
            for mode in (["--verify"], ["--uninstall"], []):
                with self.subTest(mode=mode or ["(install)"]):
                    proc = self._run(*mode, "--target", str(target))
                    self.assertNotIn(
                        "UnicodeEncodeError", proc.stderr,
                        f"install.py {' '.join(mode) or '(no flag)'} died encoding its own "
                        f"output; main() must reconfigure the streams before anything "
                        f"prints:\n{proc.stderr}")
                    # The subject here is ENCODING, not the exit code: every mode has to
                    # print its report without dying on an em dash. `--verify` against an
                    # empty target legitimately exits 1, so what is asserted is that the
                    # report was produced, not what it concluded.
                    self.assertNotEqual(
                        proc.stdout.strip(), "",
                        f"the mode printed nothing at all\n{proc.stderr}")
                    self.assertIn(
                        proc.returncode, (0, install.MISMATCH, install.NOTHING_TO_COMPARE),
                        f"exit {proc.returncode}\n{proc.stdout}\n{proc.stderr}")


class AnInterpreterBelowTheFloorIsRefusedBeforeAnyFileMoves(unittest.TestCase):
    """The floor was real and unchecked, so it surfaced as a half-done install.

    `write_manifest` passes `newline="\\n"` to `Path.write_text`, a keyword added in 3.10, and
    it runs AFTER every skill has been copied. Reproduced on a real 3.9.25 before the guard
    existed: 16 skills copied, then a `TypeError`, exit 1, and no manifest — 16 directories
    the installer does not own, which is the orphan state the ownership fixes exist to prevent.
    """

    def test_the_floor_is_the_one_the_manifest_write_actually_needs(self):
        self.assertEqual(install.MINIMUM_PYTHON, (3, 10),
                         "the floor is set by Path.write_text(newline=), added in 3.10")

    def test_an_old_interpreter_is_refused_and_a_new_one_is_not(self):
        for version in ((3, 8), (3, 9)):
            with self.subTest(version=version):
                refusal = install.python_too_old(version)
                self.assertIn("3.10+ is required", refusal)
                self.assertIn("Nothing was installed", refusal)
        for version in ((3, 10), (3, 12), (4, 0)):
            with self.subTest(version=version):
                self.assertEqual(install.python_too_old(version), "")

    def test_main_refuses_before_parsing_arguments(self):
        """Ordered so a bad interpreter is named ahead of a bad argument.

        Asserted with an argv `main` would otherwise reject: if the version check moved
        below `parse_args`, this would fail on the argument instead.
        """
        with unittest.mock.patch.object(install.sys, "version_info", (3, 9, 0)):
            with unittest.mock.patch.object(install.sys, "stderr", io.StringIO()) as err:
                code = install.main(["--not-a-real-flag"])
        self.assertEqual(code, 2)
        self.assertIn("3.10+ is required", err.getvalue())

    @unittest.skipUnless(shutil.which("python3.9"), "no 3.9 interpreter to check against")
    def test_a_real_old_interpreter_copies_nothing(self):
        """The behavioural half: the guard has to run before the copy, not just return 2."""
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "t"
            proc = subprocess.run(
                ["python3.9", str(REPO_ROOT / "install.py"), "--target", str(target)],
                capture_output=True, text=True, encoding="utf-8")
        self.assertEqual(proc.returncode, 2, proc.stdout + proc.stderr)
        self.assertIn("3.10+ is required", proc.stderr)
        self.assertFalse(target.exists(),
                         "the guard ran too late: files were copied before it refused")


class APruneIsAChangeEvenWhenNothingNewLands(InstallBase):
    """The manifest is written from what is there — including when the only work was a prune.

    `recorded` was written only `if installed`, so an update whose sole effect was removing a
    retired skill deleted the directory and left the manifest still naming it. `--verify` then
    reports MISSING for a skill the installer itself removed.

    Reachable without any failure being simulated: a user replaces one skill with their own
    copy (so it is unowned and gets SKIPPED without `--force`) while another is retired from
    the pack. Nothing installs, one thing is pruned.
    """

    def _install_then_retire_beta_with_alpha_unowned(self):
        self.assertEqual(self.install(), 0)
        install.write_manifest(self.target, ["beta"], "test")   # alpha now unowned
        shutil.rmtree(self.source / "beta")                     # beta retired from the pack
        return install.do_install([self.target], self.source)

    def test_the_manifest_stops_naming_a_skill_the_prune_deleted(self):
        self._install_then_retire_beta_with_alpha_unowned()
        self.assertFalse((self.target / "beta").exists(), "the prune is the precondition")
        self.assertNotIn(
            "beta", self.manifest()[0],
            "the manifest names a directory this run deleted: --verify calls that MISSING "
            "and --uninstall walks a name with nothing behind it")

    def test_an_unowned_skill_is_still_not_claimed_by_the_rewrite(self):
        """Positive control: writing the manifest on a prune must not widen ownership."""
        self._install_then_retire_beta_with_alpha_unowned()
        self.assertTrue((self.target / "alpha").is_dir())
        self.assertNotIn("alpha", self.manifest()[0],
                         "alpha was skipped as unowned; a prune must not adopt it")


class ThePruneRecordsWhatIsThereAndWhatWasCopied(InstallBase):
    """Two variants the first `pruned_any` fix missed.

    Both are about the same question — the manifest is a record of what is installed, and
    of which release put it there — and both were reachable without simulating a failure.
    """

    def test_a_retired_skill_whose_directory_is_already_gone_leaves_the_manifest(self):
        """`pruned_any` was set only when there was something to remove.

        Owned, retired from the pack, and its directory deleted by hand. Nothing to prune
        and nothing to copy, so the manifest went unwritten and kept naming a directory that
        is not there — which `--verify` reports as MISSING for ever.
        """
        self.assertEqual(self.install(), 0)
        install.write_manifest(self.target, ["beta"], "test")   # alpha unowned -> nothing copies
        shutil.rmtree(self.source / "beta")                     # beta retired
        shutil.rmtree(self.target / "beta")                     # and already deleted by hand
        # Asserted, not discarded: `alpha` is unowned here so the run SKIPS it and exits 1.
        # A bare call hides that, and a test that does not know why its own fixture fails
        # cannot notice when the reason changes.
        self.assertEqual(install.do_install([self.target], self.source), 1)
        self.assertNotIn(
            "beta", self.manifest()[0],
            "the manifest still names a directory that is not on disk, and nothing will "
            "ever remove the entry: --verify calls it MISSING and uninstall finds nothing")

    def test_a_prune_that_leaves_nothing_owned_DELETES_the_manifest(self):
        """Zero owned skills is not a manifest saying zero; it is no manifest.

        A record naming no skills is one `do_uninstall` reads as "no manifest, nothing owned
        here" while the file sits right there — describing nothing, and removable by nothing.
        `do_uninstall` deletes the manifest in exactly this state; this is the same state
        reached from the other direction.
        """
        self.assertEqual(self.install(), 0)
        install.write_manifest(self.target, ["beta"], "test")   # alpha unowned -> nothing copies
        shutil.rmtree(self.source / "beta")                     # beta retired: pruned, nothing left
        self.assertEqual(install.do_install([self.target], self.source), 1)
        self.assertFalse(
            (self.target / install.MANIFEST_NAME).exists(),
            "a manifest naming zero skills was left behind: it describes nothing and "
            "nothing will ever remove it")

    def test_a_run_that_copied_nothing_keeps_the_version_already_recorded(self):
        """The version says which release put these files here.

        A run that copies nothing leaves the files on disk as the previous release's. Stamping
        the new version claims an update that did not happen, and the manifest is the only
        record of which release a user is actually running.

        **The reachable shape is a prune that FAILS.** `recorded` is `installed` plus
        previously-owned directories that survive, so for it to be non-empty while nothing was
        copied, a retired skill's directory has to still be there — which happens when its
        removal is refused. With the removal succeeding, the target owns nothing and the case
        above applies instead.
        """
        self.assertEqual(self.install(), 0)
        install.write_manifest(self.target, ["beta"], "2026.01.0")   # alpha unowned
        shutil.rmtree(self.source / "beta")                          # retired: prune it

        def refuse(path):
            raise PermissionError(13, "used by another process")

        with unittest.mock.patch.object(install, "_remove", refuse):
            self.assertEqual(install.do_install([self.target], self.source), 1)
        self.assertIn("beta", self.manifest()[0],
                      "the prune was refused, so the skill is still installed and owned")
        self.assertEqual(
            self.manifest()[1].get("version"), "2026.01.0",
            "the manifest claims a release that never copied a file into this target")

    def test_the_summary_line_reports_the_version_the_manifest_records(self):
        """One tool, one answer. It used to print two.

        The summary printed the PACK's version while the manifest recorded the previous
        one, so the same run reported `version 2026.09.0` and stored `2026.01.0`.
        """
        self.assertEqual(self.install(), 0)
        install.write_manifest(self.target, ["beta"], "2026.01.0")   # alpha unowned
        shutil.rmtree(self.source / "beta")

        def refuse(path):
            raise PermissionError(13, "used by another process")

        out = io.StringIO()
        with unittest.mock.patch.object(install, "_remove", refuse), \
                unittest.mock.patch.object(install.sys, "stdout", out):
            install.do_install([self.target], self.source)
        self.assertIn("version 2026.01.0", out.getvalue())
        self.assertEqual(self.manifest()[1].get("version"), "2026.01.0")

    def test_a_run_that_did_copy_records_the_new_version(self):
        """Positive control, so the fix cannot freeze the version for ever."""
        self.assertEqual(self.install(), 0)
        install.write_manifest(self.target, ["alpha", "beta"], "2026.01.0")
        self.assertEqual(self.install(), 0)
        self.assertNotEqual(self.manifest()[1].get("version"), "2026.01.0")


class VerifyNamesEachProblemOnce(InstallBase):
    """One skill, listed, gone from disk and gone from the pack — one line, not two.

    MISSING and RETIRED were both printed for such a name, which reads as two problems with
    two different remedies. MISSING is the survivor: it is the actionable one and the one
    that sets the exit code.
    """

    def test_a_name_that_is_both_gone_and_retired_reports_only_missing(self):
        self.assertEqual(self.install(), 0)
        shutil.rmtree(self.target / "beta")      # listed, no longer on disk
        shutil.rmtree(self.source / "beta")      # and no longer in the pack
        out = io.StringIO()
        with unittest.mock.patch.object(install.sys, "stdout", out):
            install.do_verify([self.target], self.source)
        printed = out.getvalue()
        self.assertIn("MISSING   beta", printed)
        self.assertNotIn("RETIRED   beta", printed,
                         "one name, one problem, one remedy")

    def test_a_genuinely_retired_skill_is_still_reported(self):
        """Positive control, so the de-duplication cannot silence RETIRED altogether."""
        self.assertEqual(self.install(), 0)
        shutil.rmtree(self.source / "beta")      # still ON DISK, just dropped from the pack
        out = io.StringIO()
        with unittest.mock.patch.object(install.sys, "stdout", out):
            install.do_verify([self.target], self.source)
        self.assertIn("RETIRED   beta", out.getvalue())


class AnEmptyPathArgumentIsRefusedRatherThanReadAsTheCwd(unittest.TestCase):
    """`Path("")` is `Path(".")`, so an empty value means "wherever you are standing".

    `install.py --target ""` installed all sixteen skill directories plus the ownership
    manifest into the current directory and exited 0; `--uninstall --target ""` then removed
    them from there. `""` is what an unset shell variable expands to.

    Run as a subprocess from a scratch directory on purpose: the point of the test is what
    happens to the current working directory.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.scratch = Path(self._tmp.name)

    def _run(self, *args):
        return subprocess.run(
            [sys.executable, str(REPO_ROOT / "install.py"), *args],
            cwd=str(self.scratch), capture_output=True, text=True, encoding="utf-8")

    def test_an_empty_target_or_source_is_a_usage_error(self):
        for flag in ("--target", "--source"):
            with self.subTest(flag=flag):
                proc = self._run(flag, "")
                self.assertEqual(proc.returncode, 2, proc.stdout + proc.stderr)
                self.assertIn("empty string", proc.stderr)

    def test_nothing_was_written_into_the_working_directory(self):
        """The consequence, not just the exit code."""
        self._run("--target", "")
        self.assertEqual(
            sorted(p.name for p in self.scratch.iterdir()), [],
            "the refusal came too late: files were written into the cwd")

    def test_an_ordinary_target_still_installs(self):
        """Positive control, so the guard cannot degrade into refusing every path."""
        proc = self._run("--target", str(self.scratch / "real"))
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertTrue((self.scratch / "real" / "clarify" / "SKILL.md").is_file())



class ANameThatCouldInjectAManifestEntryIsRefused(InstallBase):
    """The manifest is line-oriented, so a name is only as safe as the line it becomes.

    `is_safe_name` guarded the READ side from the beginning. Nothing guarded the WRITE side,
    and a source name never passed through it at all. POSIX permits a newline in a directory
    name, so one skill could become two manifest entries and `--uninstall` would delete a
    directory nobody installed — a user's own `victim/` and its contents were removed, and
    the command exited 0.
    """

    def test_a_newline_in_a_source_name_cannot_become_two_entries(self):
        hostile = self.source / "harmless\ninjected"
        try:
            hostile.mkdir()
        except OSError:
            # Windows refuses the name outright — `WinError 123` — so the hostile fixture
            # cannot be BUILT there and this end-to-end case has nothing to run against.
            # That is the platform removing the hazard, not the guard working, so it is
            # skipped rather than passed. The grammar itself is asserted directly by
            # `test_the_name_grammar_rejects_the_shapes_that_break_a_manifest_or_windows`,
            # which needs no filesystem and runs everywhere.
            self.skipTest("this filesystem cannot hold a name containing a newline")
        (hostile / "SKILL.md").write_text("# x\n", encoding="utf-8")
        victim = self.target / "injected"
        victim.mkdir()
        (victim / "notes.txt").write_text("MY DATA\n", encoding="utf-8")

        self.assertEqual(self.install(), 1, "a name that cannot be recorded is a failure")
        names, _ = self.manifest()
        self.assertNotIn("injected", names,
                         "a second entry was synthesised from one skill's name")
        self.assertTrue(victim.exists(), "the user's own directory must survive the install")

        install.do_uninstall([self.target])
        self.assertEqual((victim / "notes.txt").read_text(encoding="utf-8"), "MY DATA\n",
                         "uninstall deleted a directory the installer never created")

    def test_the_name_grammar_rejects_the_shapes_that_break_a_manifest_or_windows(self):
        for bad in ("with\nnewline", "with\rcarriage", "with\ttab", " leading", "trailing ",
                    "#comment", "trailing.", "colon:stream", "star*", "question?",
                    "CON", "nul", "LPT1", "aux.md", "pipe|bar", "quote\"mark"):
            with self.subTest(name=bad):
                self.assertFalse(install.is_safe_name(bad), f"{bad!r} must not reach a manifest")
        for good in ("plan-run", "plan-run-v1", "web-verify", "tdd", "a"):
            with self.subTest(name=good):
                self.assertTrue(install.is_safe_name(good), f"{good!r} is an ordinary skill name")


class OwnershipIsRecordedBeforeAnyFileIsTouched(InstallBase):
    """One property replaces a recovery state machine: a failure leaves an OWNED state.

    The installer used to copy first and record last, so every failure between the two
    produced a directory it had created and did not claim. The next run then refused its own
    work — `SKIPPED … no manifest of ours claims it` — and only `--force` got past it, which
    is the flag that also deletes a user's own directory. A blocked manifest path left two
    skills live and unowned, and an ordinary retry installed zero of them and exited 1.
    """

    def test_a_retry_after_a_failed_manifest_write_is_an_ordinary_run(self):
        blocked = self.target / install.MANIFEST_NAME
        blocked.mkdir()
        (blocked / "in-the-way").write_text("x\n", encoding="utf-8")
        self.assertEqual(self.install(), 1, "a manifest that cannot be written is a failure")

        shutil.rmtree(blocked)
        self.assertEqual(self.install(), 0,
                         "the retry refused the directories the installer itself created")
        names, _ = self.manifest()
        self.assertEqual(sorted(names), ["alpha", "beta"])

    def test_nothing_is_copied_when_ownership_cannot_be_recorded(self):
        """The whole of the fix, in one assertion: no record, no copy.

        A copy that lands without a record is a directory the installer created and does not
        claim, and the next run refuses it. Refusing to start is the only outcome that leaves
        nothing to strand.
        """
        blocked = self.target / install.MANIFEST_NAME
        blocked.mkdir()
        self.assertEqual(self.install(), 1)
        self.assertFalse((self.target / "alpha").exists(),
                         "files were copied without a record of who owns them")

    def test_a_failed_copy_leaves_the_skill_owned_so_a_rerun_repairs_it(self):
        real = shutil.copytree
        def fail_on_beta(src, dst, *args, **kwargs):
            if Path(src).name == "beta":
                raise OSError(28, "simulated: no space left on device")
            return real(src, dst, *args, **kwargs)
        with unittest.mock.patch.object(shutil, "copytree", fail_on_beta):
            self.assertEqual(self.install(), 1)
        names, _ = self.manifest()
        self.assertIn("beta", names,
                      "a skill whose copy failed must stay owned, or the retry refuses it")
        self.assertEqual(self.install(), 0, "the repair is an ordinary re-run")
        self.assertTrue((self.target / "beta" / "SKILL.md").is_file())


class VerifyEstablishesWhatItClaims(InstallBase):
    """`--verify` says the install "matches this pack". It only ever checked presence.

    Two halves were unenforced. A retired skill still installed and a newly shipped skill
    absent were both PRINTED and both exited 0. And nothing looked inside a directory, so a
    half-copied skill verified clean — which matters a great deal now that the installer
    copies in place.
    """

    def test_a_set_mismatch_is_a_failure_not_a_remark(self):
        self.assertEqual(self.install(), 0)
        retired = self.target / "retired"
        retired.mkdir()
        (retired / "SKILL.md").write_text("# retired\n", encoding="utf-8")
        names, meta = self.manifest()
        install.write_manifest(self.target, sorted(names + ["retired"]),
                               meta.get("version", "test"))
        self.assertEqual(install.do_verify([self.target], self.source), 1,
                         "a skill installed that the pack no longer ships is a mismatch")

    def test_a_skill_shipped_but_not_installed_is_a_failure(self):
        self.assertEqual(self.install(), 0)
        newer = self.source / "gamma"
        newer.mkdir()
        (newer / "SKILL.md").write_text("# gamma\n", encoding="utf-8")
        self.assertEqual(install.do_verify([self.target], self.source), 1,
                         "a skill this pack ships and the target lacks is a mismatch")

    def test_a_half_copied_skill_is_seen(self):
        self.assertEqual(self.install(), 0)
        (self.target / "alpha" / "references" / "notes.md").unlink()
        self.assertEqual(install.do_verify([self.target], self.source), 1,
                         "a skill missing a shipped file must not verify clean")



class TheManifestWriteCannotBeAimedAtAnotherFile(InstallBase):
    """The temporary file it writes through was a predictable name in a writable directory.

    `write_manifest` wrote to `<manifest>.tmp-<pid>` and `write_text` follows a symlink. A
    link planted at that name — the pid is guessable, and a wrong guess simply waits for the
    next run — redirected the write outside the target, truncating whatever it pointed at
    before `os.replace` moved the link into place.
    """

    def test_a_symlink_at_the_temporary_path_is_not_written_through(self):
        outside = self.root / "precious.conf"
        outside.write_text("PRECIOUS\n", encoding="utf-8")
        planted = self.target / f"{install.MANIFEST_NAME}.tmp-{os.getpid()}"
        try:
            planted.symlink_to(outside)
        except (OSError, NotImplementedError):
            self.skipTest("this platform/user cannot create symlinks")
        install.write_manifest(self.target, ["alpha"], "test")
        self.assertEqual(outside.read_text(encoding="utf-8"), "PRECIOUS\n",
                         "the write followed a planted link out of the target")
        self.assertFalse((self.target / install.MANIFEST_NAME).is_symlink(),
                         "the manifest itself is now a link to somewhere else")
        self.assertEqual(install.read_manifest(self.target)[0], ["alpha"])


class VerifyReportsWhatItCouldNotRead(InstallBase):
    """An unreadable file verified CLEAN, because the byte check skipped what it could not open.

    `_content_mismatches` caught `OSError` and `continue`d. With the path present and the size
    matching, no other check had anything to say either — so `--verify` exited 0 having never
    looked at the file. Reproduced with `chmod 000` on an installed `SKILL.md`.
    """

    @unittest.skipUnless(os.name == "posix", "chmod 000 does not deny the owner on Windows")
    @unittest.skipIf(os.geteuid() == 0 if hasattr(os, "geteuid") else False,
                     "root reads through mode 000")
    def test_a_file_that_cannot_be_read_is_a_mismatch(self):
        self.assertEqual(self.install(), 0)
        self.assertEqual(install.do_verify([self.target], self.source), 0, "control")
        name = sorted(p.name for p in self.source.iterdir() if p.is_dir())[0]
        victim = self.target / name / "SKILL.md"
        victim.chmod(0o000)
        self.addCleanup(victim.chmod, 0o644)
        self.assertEqual(
            install.do_verify([self.target], self.source), 1,
            "a check that cannot read a file must say so, not certify it")


class VerifyComparesKindNotOnlySize(InstallBase):
    """A file swapped for a same-size symlink to equal content verified clean.

    `_shape` recorded size alone and `read_bytes()` follows a link, so a regular file replaced
    by a symlink whose link text was the same length and whose referent compared equal passed
    both checks. The skill-root guard catches this one directory up; this is the same
    substitution at any depth INSIDE a skill.
    """

    @unittest.skipUnless(os.name == "posix", "symlink creation needs elevation on Windows")
    def test_a_nested_file_replaced_by_a_symlink_is_a_mismatch(self):
        """The size check cannot see this one, which is the whole point.

        `lstat` on a symlink reports the length of its TARGET PATH, so the substitution is
        invisible to a size comparison exactly when that path length equals the file's size.
        The source file is written to that length deliberately — a link of a convenient
        length would make this test pass against the unfixed code.
        """
        name = sorted(p.name for p in self.source.iterdir() if p.is_dir())[0]
        origin = (self.source / name / "SKILL.md").resolve()
        # Content sized to match the link text exactly, so size comparison agrees.
        origin.write_bytes(b"x" * len(str(origin).encode()))
        self.assertEqual(self.install(), 0)
        self.assertEqual(install.do_verify([self.target], self.source), 0, "control")

        landed = self.target / name / "SKILL.md"
        landed.unlink()
        landed.symlink_to(origin)
        self.assertEqual(landed.lstat().st_size, origin.stat().st_size,
                         "the fixture must make size comparison agree, or it proves nothing")
        self.assertEqual(landed.read_bytes(), origin.read_bytes(),
                         "and content comparison agrees too, because reads follow the link")
        self.assertEqual(
            install.do_verify([self.target], self.source), 1,
            "a link is never a copy, at any depth inside a skill")


class VerifyRefusesASkillRootThatIsALink(InstallBase):
    """A manifest-owned skill directory replaced by a link verified clean.

    `_shape()` walks THROUGH a link and compares the referent, so pointing an installed skill
    at the source made every path and every size agree and `--verify` reported the install
    sound. That is the exact condition `--link` was removed to prevent — editing the installed
    instructions edits the source — reachable through the command whose job is to certify that
    it has not happened.

    `is_symlink()` also answers for a Windows junction, which is what made `--force` able to
    delete the pack's own `skills/` through it.
    """

    @unittest.skipUnless(os.name == "posix", "symlink creation needs elevation on Windows")
    def test_a_symlinked_skill_root_is_a_failure_not_a_clean_verify(self):
        self.assertEqual(self.install(), 0)
        self.assertEqual(install.do_verify([self.target], self.source), 0,
                         "the control: an ordinary install verifies clean")
        name = sorted(p.name for p in self.source.iterdir() if p.is_dir())[0]
        landed = self.target / name
        shutil.rmtree(landed)
        landed.symlink_to((self.source / name).resolve(), target_is_directory=True)
        self.assertEqual(
            install.do_verify([self.target], self.source), 1,
            "a skill root that is a link is not an installed copy, whatever is behind it")


class VerifySeesAnIncompleteCopy(InstallBase):
    """A path set cannot establish completeness, and completeness is the whole claim.

    Removing the staged swap was paid for by `--verify` being able to see a half-copied
    skill. It could not: a copy that creates the destination filename and then fails while
    writing its bytes leaves every path present — 1 byte of a 500-byte `SKILL.md`, install
    exited 1, verify exited 0.

    Sizes rather than hashes: `os.lstat` is one call per file, reads nothing, and catches the
    failure a copy can actually produce. A user who edited a skill in place is reported too,
    and that is correct — the question `--verify` answers is whether the install matches the
    pack.
    """

    def test_a_truncated_file_is_not_clean(self):
        self.assertEqual(self.install(), 0)
        landed = self.target / "alpha" / "SKILL.md"
        landed.write_text("", encoding="utf-8")
        self.assertEqual(install.do_verify([self.target], self.source), 1,
                         "a file with the right name and the wrong bytes verified clean")

    def test_nothing_installed_at_all_has_its_own_answer(self):
        """Not success, and not the same failure as a broken install.

        One failure code makes "you have not installed this" indistinguishable from "your
        install is broken", and those need opposite responses. Reporting success was the
        other half of the same problem — a check claiming to confirm a match said yes to a
        machine holding none of the pack. Three answers, so neither meaning borrows the
        other's.
        """
        self.assertEqual(
            install.do_verify([self.root / "never-installed"], self.source),
            install.NOTHING_TO_COMPARE,
            "an empty target must be its own answer, not success and not a mismatch")

    def test_a_broken_install_outranks_an_absent_one(self):
        """With several targets, the actionable answer is the one to report."""
        self.assertEqual(self.install(), 0)
        (self.target / "alpha" / "SKILL.md").write_text("", encoding="utf-8")
        self.assertEqual(
            install.do_verify([self.root / "never-installed", self.target], self.source),
            install.MISMATCH,
            "a real mismatch was reported as merely nothing-installed")

    def test_an_install_left_on_an_older_version_is_a_mismatch(self):
        self.assertEqual(self.install(), 0)
        names, _ = self.manifest()
        install.write_manifest(self.target, names, "1900.01.0")
        self.assertEqual(install.do_verify([self.target], self.source), 1,
                         "the files are this pack's but the record says another release")


class OwnershipOutlivesAChangeToTheNameGrammar(InstallBase):
    """Tightening what may be INSTALLED must not disown what was already installed.

    The strict grammar was applied to manifest entries as well as to source names, so a skill
    recorded by an earlier release under a name the new rules reject was dropped from the
    record on the next update: never pruned, and no longer removable — with `legacy:name`,
    update 0, verify 0, uninstall 0, directory still there.

    Two questions, separated: *may this be installed from a source pack* is strict, and *can
    this name be written as one manifest line and not escape the target* is what ownership
    actually needs.
    """

    def test_a_legacy_name_the_new_grammar_rejects_is_still_owned_and_removable(self):
        legacy = self.target / "legacy:name"
        try:
            legacy.mkdir()
        except OSError:
            self.skipTest("this filesystem cannot hold that name")
        (legacy / "SKILL.md").write_text("old\n", encoding="utf-8")
        install.write_manifest(self.target, ["legacy:name"], "2026.06.0")

        self.assertFalse(install.is_safe_name("legacy:name"),
                         "the source grammar must still reject it")
        self.assertTrue(install.is_recordable_name("legacy:name"),
                        "but it can be written as one line and cannot escape the target")

        self.install()
        self.assertNotIn("legacy:name", install.discover_skills(self.source))
        self.assertFalse(legacy.exists(),
                         "owned and no longer in the pack: it should have been pruned")

    def test_a_name_that_cannot_be_written_as_a_line_is_never_recordable(self):
        for bad in ("two\nlines", "#comment", " leading", "trailing ", "../escape",
                    "a/b", "..", ""):
            with self.subTest(name=bad):
                self.assertFalse(install.is_recordable_name(bad))
        for ok in ("legacy:name", "plan-run", "CON", "trailing."):
            with self.subTest(name=ok):
                self.assertTrue(install.is_recordable_name(ok),
                                "recordable is about the line and the target, not Windows")


class ThePackCannotBeInstalledOverItself(InstallBase):
    """`--source X --target X --force` deleted the pack. All sixteen skills.

    Remove-then-copy made it certain: `live` IS the source directory, `_remove` deletes it,
    and `copytree` then reads what is no longer there — 16 directories before, 0 after. The
    shell installers guarded this explicitly, and the guard was not carried across.
    """

    def test_installing_the_source_onto_itself_is_refused(self):
        self.assertEqual(install.do_install([self.source], self.source, force=True), 1,
                         "the installer accepted its own source as a target")
        self.assertTrue((self.source / "alpha" / "SKILL.md").is_file(),
                        "the source pack was deleted")

    def test_a_target_containing_the_source_is_refused(self):
        self.assertEqual(install.do_install([self.source.parent], self.source, force=True), 1)
        self.assertTrue((self.source / "alpha" / "SKILL.md").is_file())

    def test_the_same_directory_reached_by_a_different_spelling_is_refused(self):
        spelled = self.source.parent / "skills" / ".." / "skills"
        self.assertEqual(install.do_install([spelled], self.source, force=True), 1)
        self.assertTrue((self.source / "alpha" / "SKILL.md").is_file())


class TheManifestIsReadableByWhoeverCanReadTheSkills(InstallBase):
    """`mkstemp` closed a symlink race and opened a permissions one.

    It creates at mode 0600 and nothing chmods the descriptor before `os.replace`, so the
    manifest a root or shared install leaves behind cannot be read by the user who owns the
    skills. `read_manifest` treats an unreadable file exactly like an absent one, so
    `--uninstall` then prints "no manifest, nothing owned here" and exits 0 over a full
    install.
    """

    def test_the_manifest_is_not_private_to_the_writer(self):
        self.install()
        mode = (self.target / install.MANIFEST_NAME).stat().st_mode & 0o777
        expected = 0o666 & ~_current_umask()
        self.assertEqual(mode, expected,
                         f"manifest is {mode:o}; an ordinary file here would be {expected:o}")

    def test_a_manifest_that_cannot_be_read_is_not_reported_as_absent(self):
        self.install()
        manifest = self.target / install.MANIFEST_NAME
        manifest.chmod(0o000)
        self.addCleanup(manifest.chmod, 0o644)
        if os.access(manifest, os.R_OK):
            self.skipTest("running as a user that ignores file permissions")
        self.assertEqual(install.do_uninstall([self.target]), 1,
                         "an unreadable manifest was reported as no manifest, and "
                         "uninstall exited 0 over a full install")
        self.assertTrue((self.target / "alpha").is_dir(),
                        "it removed skills it could not confirm it owned")


if __name__ == "__main__":
    unittest.main()
