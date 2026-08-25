#!/usr/bin/env python3
"""Portable Agent Skills — installer.

Copies each skill directory into the runtime skill directories, and records what it put
there so it can take it away again. Install, update, uninstall, verify.

    python3 install.py                 # install/update into both runtimes
    python3 install.py --uninstall     # remove exactly what a previous run installed
    python3 install.py --verify        # is the install intact?
    python3 install.py --target DIR    # one directory instead of the defaults

One implementation rather than a shell/PowerShell pair, so there is no parity to maintain.
Python is already a hard requirement of the pack, so requiring it here surfaces an existing
constraint at install time instead of at first use.

Four design decisions:

**Copy only; no symlink mode.** A linked install lets an agent edit the instructions it is
executing, and a directory junction let `--force` delete the pack's own `skills/`.

**Replace a skill wholesale; never merge into a live directory.** Nothing merges, so
nothing has to decide what to keep.

**An interrupted install is repaired by running the installer again.** Staging a copy and
swapping it in would make "a half-finished copy is never what a runtime reads" literally
true, at the cost of a startup scan naming every state an interruption can leave. The
procedure works because ownership is recorded before any file is touched and because
`--verify` compares the installed file set against the pack.

**No hashes for ownership; a digest for content.** Ownership is the directory: one skill,
one directory, wholly ours, so uninstall is "remove the names in the manifest". `--verify`
compares kind and digest because a skill is text a model obeys, and text can be rewritten
to the same length — so an in-place edit is reported, deliberately.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent
SKILLS_SRC = REPO_ROOT / "skills"

# Three states, because two cannot carry the meaning: "nothing is installed" and "the
# install is broken" need opposite responses, and a check claiming to confirm a match must
# not report success over a machine holding none of the pack.
MISMATCH = 1
NOTHING_TO_COMPARE = 2

MANIFEST_NAME = ".installed-by-portable-agent-skills"

# Names that cannot be recorded in a line-oriented manifest, or cannot be a directory on
# Windows. Defined once because the same grammar has to hold on the way IN (a discovered
# source name) and on the way OUT (a manifest line being acted on).
_WINDOWS_RESERVED = frozenset(
    ["CON", "PRN", "AUX", "NUL"]
    + [f"COM{d}" for d in "123456789"]
    + [f"LPT{d}" for d in "123456789"]
)
_ILLEGAL_CHARACTERS = frozenset('<>:"/\\|?*')


def default_targets() -> list[Path]:
    """Where the two runtimes read user-level skills from.

    `~/.agents/skills` is the documented Codex user scope, shared with Copilot and Cursor.
    `~/.codex/skills` holds configuration, not skills.
    """
    home = Path.home()
    return [
        Path(os.environ.get("CLAUDE_SKILLS_DIR") or home / ".claude" / "skills"),
        Path(os.environ.get("CODEX_SKILLS_DIR") or home / ".agents" / "skills"),
    ]


_VERSION_HEADING = re.compile(r"^##\s*\[([^\]]+)\]")


def pack_version() -> str:
    """The version string recorded in a target's manifest, read from ``CHANGELOG.md``.

    Read rather than stored in a `VERSION` file, which would be a second answer to a
    question the changelog already answers — two things to bump at release, one enforced and
    one not. The changelog heading is the enforced one: a release cannot be built until
    `## [{version}]` exists with a non-empty section.

    The changelog also travels, which is the point: deriving provenance from `git describe`
    records `unknown` for an install from a downloaded zip. `[Unreleased]` is skipped, so a
    working tree reports the last released version; git is a fallback, `unknown` the honest
    last resort.
    """
    try:
        text = (REPO_ROOT / "CHANGELOG.md").read_text(encoding="utf-8", errors="replace")
    except OSError:
        text = ""
    for line in text.splitlines():
        match = _VERSION_HEADING.match(line)
        if match and match.group(1).strip().lower() != "unreleased":
            return match.group(1).strip()
    try:
        result = subprocess.run(
            ["git", "-C", str(REPO_ROOT), "describe", "--tags", "--always", "--dirty"],
            # `encoding=`, not `text=True`: text mode decodes with the locale's encoding,
            # so a UTF-8 tag name raises UnicodeDecodeError — a ValueError, which the
            # `except` below does not catch — out of a lookup every install performs.
            capture_output=True, encoding="utf-8", errors="replace", timeout=10,
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        pass
    return "unknown"


def discover_skills(source: Path) -> list[str]:
    """Every directory under ``source`` holding a ``SKILL.md``, sorted.

    Discovery rather than a hardcoded list, so a new skill installs the day it is added.
    Plain sort on the name: the manifest must be byte-identical between machines, and
    locale-dependent ordering is not.
    """
    if not source.is_dir():
        return []
    return sorted(
        entry.name for entry in source.iterdir()
        if entry.is_dir() and (entry / "SKILL.md").is_file()
        and is_safe_name(entry.name)
    )


def unsafe_source_names(source: Path) -> list[str]:
    """Skill directories discovery had to drop, so the caller can report them.

    Separate from ``discover_skills`` because a silent skip is how a malformed name goes
    unnoticed: the install is simply short one skill and exits 0.
    """
    if not source.is_dir():
        return []
    return sorted(
        entry.name for entry in source.iterdir()
        if entry.is_dir() and (entry / "SKILL.md").is_file()
        and not is_safe_name(entry.name)
    )


def is_recordable_name(name: str) -> bool:
    """Can ``name`` be written as one manifest line, and can it only mean a child of the
    target?

    This is what **ownership** needs, and it is deliberately narrower than what a source pack
    may ship. Tightening the source grammar once silently disowned already-installed skills:
    a name that stopped being recordable was dropped from the manifest, never pruned, and no
    longer removable.

    A name qualifies when it survives the round trip through the manifest — no control
    character, no leading ``#``, no surrounding whitespace — and when it cannot leave the
    target: no separator, no drive, no root, no ``..``.
    """
    if not name or name in (".", ".."):
        return False
    if name != name.strip() or name.startswith("#"):
        return False
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in name):
        return False
    if "/" in name or "\\" in name:
        return False
    return Path(name).name == name and not Path(name).is_absolute()


def is_safe_name(name: str) -> bool:
    """May a source pack ship a skill directory called ``name``?

    Everything ``is_recordable_name`` requires, plus what a **Windows** directory can be.
    Applied at discovery, so a failing name reaches neither a manifest nor a filesystem.

    Beyond the recordable rules: none of ``<>:"|?*``, of which ``:`` also names an NTFS
    alternate data stream; no trailing ``.`` or space, which Windows silently strips,
    collapsing two entries onto one directory; and no reserved device name.
    """
    if not is_recordable_name(name):
        return False
    if name.endswith("."):
        return False
    if any(ch in _ILLEGAL_CHARACTERS for ch in name):
        return False
    return name.split(".")[0].upper() not in _WINDOWS_RESERVED


def _umask() -> int:
    """The process umask, read without leaving it changed. There is no way to just ask."""
    value = os.umask(0)
    os.umask(value)
    return value


class ManifestUnreadable(Exception):
    """The manifest exists and could not be read — which is not the same as absent."""


def read_manifest(target: Path) -> tuple[list[str], dict[str, str]]:
    """``(skill names, metadata)`` from a target's manifest; empty when there is none.

    Reads the format the shell installers wrote — ``#`` lines are metadata, everything else
    is a skill name — so an install made by those can still be updated or removed here.
    """
    path = target / MANIFEST_NAME
    names: list[str] = []
    meta: dict[str, str] = {}
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except FileNotFoundError:
        return names, meta
    except OSError as exc:
        # Present and unreadable. Reporting "no manifest" here lets the caller confuse
        # "nothing is owned" with "I could not find out", and those need opposite
        # behaviour — it is what let `--uninstall` exit 0 over a full install.
        raise ManifestUnreadable(f"{path}: {exc.strerror or exc}") from exc
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        if line.startswith("#"):
            body = line.lstrip("#").strip()
            if ":" in body:
                key, _, value = body.partition(":")
                meta[key.strip()] = value.strip()
            continue
        names.append(line)
    return names, meta


def write_manifest(target: Path, names: list[str], version: str) -> None:
    """Record what is installed here, written last so it describes a finished state.

    Written via a temporary file and one rename, so a reader never sees a partial list. A
    short manifest over a full install is worse than none: the next uninstall leaves the
    unlisted skills behind, permanently unowned.

    ``newline="\\n"`` because text mode otherwise translates to ``os.linesep``, and
    ``discover_skills`` claims this file is byte-identical between machines.
    """
    path = target / MANIFEST_NAME
    body = [
        "# Portable Agent Skills manifest",
        "# Written by install.py. Lines beginning with # are metadata; every other line",
        "# is a skill directory this installer owns and will remove on --uninstall.",
        f"# version: {version}",
        f"# skills: {len(names)}",
    ]
    body.extend(names)
    # `mkstemp`, not a name built from the pid. A predictable temporary path plus
    # `write_text` following a symlink lets a link planted at that name redirect the write
    # outside the target. `mkstemp` opens O_EXCL on a random name and returns a descriptor
    # rather than a path to re-resolve.
    fd, created = tempfile.mkstemp(prefix=f"{MANIFEST_NAME}.tmp-", dir=target)
    tmp = Path(created)
    mode = 0o666 & ~_umask()
    try:
        # Through the DESCRIPTOR wherever the platform allows it. `mkstemp` creates at 0600,
        # which is right for a secret and wrong for a record of what is installed — a shared
        # install left a manifest the owning user could not read, and an unreadable manifest
        # was treated as no manifest. Doing it by path opens a window for another writer to
        # swap in a symlink.
        if os.chmod in os.supports_fd:
            try:
                os.chmod(fd, mode)
            except OSError:
                # Best effort, exactly like the path branch below. A filesystem that
                # allows `mkstemp` and rejects `fchmod` — several network and container
                # filesystems do — would otherwise fail every install over a permission
                # bit. The manifest is written and correct either way.
                pass
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write("\n".join(body) + "\n")
        if os.chmod not in os.supports_fd:
            # Windows before 3.13, where the descriptor form raises. The path form is the
            # only option, and the window it opens needs a symlink in the user's own
            # target directory, which Windows requires privilege to create.
            try:
                os.chmod(tmp, mode)
            except OSError:
                pass
        # Closed before the replace: Windows refuses to rename a file that is still open.
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def _remove(path: Path, attempts: int = 5) -> None:
    """Remove a file, directory or link without ever following a link out of the tree.

    **The link test is `_is_link`, not `is_symlink`, and that is the whole point of it.** A
    Windows junction answers False to `is_symlink()` and True to `is_dir()`, so an
    `is_symlink() or is_file()` / `elif is_dir()` shape hands one to `shutil.rmtree`.
    `rmtree` does not descend it — it reads the reparse tag first and raises `Cannot call
    rmtree on a symbolic link` — so the cost is a failed update rather than a deleted
    checkout. It is still the wrong branch: the message names a symbolic link on a path
    nobody linked, it arrives only once the retry loop below has run out, and the skill is
    still installed afterwards. A junction is a link, and a link is detached.

    A detached link is removed with `unlink`, except that a Windows *directory* reparse
    point refuses it — `rmdir` detaches that one without touching what it points at.

    **Retried with backoff, because Windows refuses while a file is open** — and an agent
    with a `SKILL.md` loaded is exactly that. Short and bounded on purpose.
    """
    delay = 0.05
    for attempt in range(attempts):
        try:
            if _is_link(path):
                try:
                    path.unlink(missing_ok=True)
                except OSError:
                    os.rmdir(path)
            elif path.is_file():
                path.unlink(missing_ok=True)
            elif path.is_dir():
                shutil.rmtree(path)
            return
        except OSError:
            if attempt == attempts - 1:
                raise
            time.sleep(delay)
            delay *= 2


def install_skill(source: Path, target: Path, name: str) -> None:
    """Put ``name`` into ``target``, replacing whatever is there. Remove, then copy.

    Nothing is staged and nothing is moved aside. Staging buys one property — an interrupted
    update leaves the previous version intact — and costs another: every interruption
    produces a state that has to be *named* and reconciled at startup.

    What replaces it is a procedure: **an interrupted install is repaired by running the
    installer again.** That holds because ownership is recorded before any file is touched
    and because ``--verify`` compares the installed file set against the pack.

    The trade, stated plainly: a copy that dies part-way leaves the skill incomplete rather
    than leaving the old version in place, which is recoverable from the pack.
    """
    live = target / name
    _remove(live)
    shutil.copytree(source / name, live, symlinks=True)


def _overlaps(a: Path, b: Path) -> bool:
    """Do two resolved paths name the same directory, or one inside the other?

    `samefile` as well as `==`, because `resolve()` compares NAMES: a bind mount, a hard
    link or a container mount reaches one directory under two resolved paths.
    """
    def same(x: Path, y: Path) -> bool:
        if x == y:
            return True
        try:
            return x.exists() and y.exists() and os.path.samefile(x, y)
        except OSError:
            return False

    if same(a, b):
        return True
    # Ancestry by IDENTITY, not by name. `a in b.parents` compares resolved strings, so a
    # parent and child reached through two mounts of the same tree read as unrelated.
    # Comparing each ancestor by device and inode is the question a mount cannot answer
    # differently.
    return (any(same(parent, a) for parent in b.parents)
            or any(same(parent, b) for parent in a.parents))


def do_install(targets: list[Path], source: Path, force: bool = False) -> int:
    """Install every skill this pack ships into each target. Idempotent, and repeatable.

    **Ownership is recorded before any file is touched**, and that ordering is the whole of
    the recovery design. Copying first and writing the manifest last makes every failure in
    between produce directories the installer created and did not claim; the next run then
    refuses its own work, and the only way past it is `--force`, the flag that also deletes
    a user's own directory.

    The refusal itself still comes first: a directory this installer never recorded belongs
    to the user.
    """
    # A target that IS the source, contains it, or lives inside it: `live` would be the
    # source skill itself, so `_remove` deletes it and `copytree` then reads what is no
    # longer there. Resolved first, so `.../skills/../skills`, a symlink and the plain
    # spelling are all the same directory.
    source_real = source.resolve()
    overlapping = []
    unresolvable = []
    resolved: list[tuple[Path, Path]] = []
    for target in targets:
        try:
            candidate = target.resolve()
        except (OSError, RuntimeError):
            # `RuntimeError` as well: a SYMLINK LOOP is how `resolve()` reports that it
            # cannot get an answer, and it is not an OSError. A traceback is not a refusal.
            #
            # **Refused, not skipped past.** The comparisons below are what stop an install
            # deleting the pack it copies from, and a target whose real path is unknown
            # cannot be shown *not* to be the source. Continuing on to install into it
            # leaves the guard open in exactly the case it exists for.
            unresolvable.append(target)
            continue
        resolved.append((target, candidate))
        if _overlaps(candidate, source_real):
            overlapping.append(target)
    if unresolvable:
        for target in unresolvable:
            print(f"error: {target} cannot be resolved to a real path, so it cannot be "
                  f"compared against the skills source — refusing to install into it",
                  file=sys.stderr)
        return 1
    if overlapping:
        for target in overlapping:
            print(f"error: {target} is the skills source, or contains it — installing "
                  f"would delete the pack it is copying from", file=sys.stderr)
        return 1

    # And against EACH OTHER. Two targets that are the same directory, or one inside the
    # other, cannot both be installed to: the first install becomes an unowned directory
    # that the second deletes whole on its way past.
    for index, (first, first_real) in enumerate(resolved):
        for second, second_real in resolved[index + 1:]:
            if _overlaps(first_real, second_real):
                print(f"error: {first} and {second} are the same directory or one contains "
                      f"the other; installing to both would delete the first",
                      file=sys.stderr)
                return 1

    names = discover_skills(source)
    rejected = unsafe_source_names(source)
    for name in rejected:
        print(f"error: {name!r} is not a usable skill directory name and was skipped; it "
              f"could not be recorded in a manifest or created on Windows", file=sys.stderr)
    if not names:
        print(f"error: no skills found under {source}", file=sys.stderr)
        return 1
    version = pack_version()
    failed = bool(rejected)
    for target in targets:
        try:
            target.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            print(f"error: cannot create {target}: {exc}", file=sys.stderr)
            failed = True
            continue
        try:
            previously_owned, previous_meta = read_manifest(target)
        except ManifestUnreadable as exc:
            print(f"error: {exc}; refusing to install over a target whose ownership "
                  f"record cannot be read", file=sys.stderr)
            failed = True
            continue

        # 1. Decide what may be written. A directory this installer never recorded is the
        #    user's, and this is where that is refused — before the manifest claims it and
        #    long before anything is deleted.
        writable: list[str] = []
        for name in names:
            live = target / name
            # `exists()` follows a link and answers False for a dangling one, so it alone
            # reports a name as free while a broken link still holds it — and the link most
            # likely to be standing there on Windows is a junction, which `is_symlink()`
            # does not see. Occupancy is `exists()` OR `_is_link`, here and at every other
            # site asking the same question.
            if (live.exists() or _is_link(live)) and name not in previously_owned:
                if not force:
                    print(f"  {target}: SKIPPED {name} — a directory of that name is "
                          f"already here and no manifest of ours claims it. Re-run with "
                          f"--force to replace it", file=sys.stderr)
                    failed = True
                    continue
                print(f"  {target}: replacing unowned {name} (--force)")
            writable.append(name)

        # 2. Record the intent. From here on every one of these names is ours, so a copy
        #    that dies leaves a skill that is owned and incomplete — which `--verify` can
        #    see and a re-run repairs — rather than present and disclaimed.
        #
        #    It carries the version ALREADY recorded, not this pack's: nothing has been
        #    copied yet, so claiming this release would describe files that are not on disk.
        #    It also names what is owned NOW as well as what is about to be — a retired
        #    skill still on disk is ours until its removal succeeds.
        carried = (previous_meta.get("version") or previous_meta.get("source-version")
                   or version)
        retained = [name for name in previously_owned
                    if is_recordable_name(name) and name not in writable
                    and ((target / name).exists() or _is_link(target / name))]
        intent = sorted(set(writable) | set(retained))
        try:
            if intent:
                write_manifest(target, intent, carried)
            else:
                (target / MANIFEST_NAME).unlink(missing_ok=True)
        except OSError as exc:
            print(f"error: manifest for {target}: {exc}", file=sys.stderr)
            print(f"  {target}: nothing was installed — ownership could not be recorded "
                  f"first, and copying without it is what strands an install",
                  file=sys.stderr)
            failed = True
            continue

        # 3. Copy. Remove-then-copy, so this is the same work whether the skill was
        #    already there or not.
        installed: list[str] = []
        for name in writable:
            try:
                install_skill(source, target, name)
                installed.append(name)
            except OSError as exc:
                print(f"error: {name} -> {target}: {exc}", file=sys.stderr)
                failed = True

        # 4. RETIRED skills. A skill dropped from the pack has to leave the user's machine
        #    too: discovery globs for SKILL.md with no allowlist, so one left behind keeps
        #    loading for ever. Only names the PREVIOUS manifest claimed are pruned, read from
        #    `previously_owned`, captured before step 2 replaced the file on disk.
        survived_prune: list[str] = []
        for name in retained:
            if name in names:
                continue
            try:
                _remove(target / name)
                print(f"  {target}: pruned retired skill {name}")
            except OSError as exc:
                print(f"error: could not prune {name}: {exc}", file=sys.stderr)
                survived_prune.append(name)
                failed = True

        # 5. Settle the record. It drops what the prune removed, keeps what the prune
        #    could not, and stamps this release ONLY when every skill it names landed — a
        #    partial run keeps the version already recorded, because the files still on
        #    disk are that release's.
        final = sorted(set(writable) | set(survived_prune))
        stamped = version if (writable and installed == writable) else carried
        if final != intent or stamped != carried:
            try:
                if final:
                    write_manifest(target, final, stamped)
                else:
                    (target / MANIFEST_NAME).unlink(missing_ok=True)
            except OSError as exc:
                print(f"error: manifest for {target}: {exc}", file=sys.stderr)
                stamped = carried
                failed = True
        print(f"  {target}: {len(installed)} of {len(writable)} skill(s), version {stamped}")
    return 1 if failed else 0


def do_uninstall(targets: list[Path]) -> int:
    """Remove exactly the directories a previous run recorded — and nothing else."""
    failed = False
    for target in targets:
        try:
            names, meta = read_manifest(target)
        except ManifestUnreadable as exc:
            print(f"error: {exc}; refusing to remove anything, because what this "
                  f"installer owns here cannot be established", file=sys.stderr)
            failed = True
            continue
        if not names:
            print(f"  {target}: no manifest, nothing owned here")
            continue
        removed = 0
        for name in names:
            if not is_recordable_name(name):
                print(f"  {target}: INVALID manifest entry {name!r} — not a skill name, "
                      f"refusing to act on it", file=sys.stderr)
                failed = True
                continue
            path = target / name
            if not (path.exists() or _is_link(path)):
                continue
            try:
                _remove(path)
                removed += 1
            except OSError as exc:
                print(f"error: could not remove {path}: {exc}", file=sys.stderr)
                failed = True
        # The record outlives what could not be removed. Deleting it unconditionally leaves
        # the skill on disk owned by nothing, so the next `--uninstall` reports nothing owned
        # and exits 0 while the skill is still installed. A name is dropped when its directory
        # is gone, and only then.
        remaining = [
            name for name in names
            if not is_recordable_name(name)
            or (target / name).exists() or _is_link(target / name)
        ]
        try:
            if remaining:
                version = meta.get("version") or meta.get("source-version") or "unknown"
                write_manifest(target, remaining, version)
            else:
                (target / MANIFEST_NAME).unlink(missing_ok=True)
        except OSError as exc:
            print(f"error: could not update the manifest in {target}: {exc}",
                  file=sys.stderr)
            failed = True
        print(f"  {target}: removed {removed} of {len(names)} listed skill(s)")
    return 1 if failed else 0


def _is_link(path: Path) -> bool:
    """True when ``path`` is a symlink or a Windows junction.

    Two questions, because Python answers them separately: ``is_symlink()`` does not report a
    junction, and ``Path.is_junction()`` arrived in **3.12** while this pack supports
    **3.10+**, so the fallback asks the OS itself.

    **The fallback reads the reparse TAG, not the reparse bit.** Every reparse point sets
    FILE_ATTRIBUTE_REPARSE_POINT — a cloud-storage placeholder directory, a ProjFS root and
    a deduplicated file all carry it, and none of them is a link. Testing the bit alone
    calls an ordinary synced directory a link, and `_remove` then tries to *detach* it:
    ``unlink`` refuses a directory, ``rmdir`` refuses a non-empty one, and an update that
    used to succeed fails instead. A user whose skills sit under a synced home directory
    meets that on every skill they have.

    It matters because a junction is the Windows shape of the hazard `--link` was removed
    over: a skill root pointing at the source means editing the installed instructions edits
    the source.
    """
    try:
        if path.is_symlink():
            return True
    except OSError:
        return False
    is_junction = getattr(path, "is_junction", None)
    if is_junction is not None:
        try:
            return bool(is_junction())
        except OSError:
            return False
    # Python 3.10 / 3.11. `lstat()` describes the link itself where `stat()` would follow it
    # and report on the target. Both attributes below are Windows-only, so a missing one
    # answers no rather than raising.
    try:
        st = path.lstat()
    except OSError:
        return False
    if not getattr(st, "st_file_attributes", 0) & 0x400:  # FILE_ATTRIBUTE_REPARSE_POINT
        return False
    return getattr(st, "st_reparse_tag", 0) in (
        0xA000000C,   # IO_REPARSE_TAG_SYMLINK
        0xA0000003,   # IO_REPARSE_TAG_MOUNT_POINT — a junction
    )


def _entries(root: Path):
    """Yield every path under ``root``, WITHOUT descending through a link.

    ``rglob`` follows a directory symlink, which on a loop never terminates and on a link
    into the source would walk the pack itself.
    """
    stack = [root]
    while stack:
        current = stack.pop()
        try:
            children = sorted(current.iterdir())
        except OSError:
            continue
        for child in children:
            yield child
            if child.is_dir() and not _is_link(child):
                stack.append(child)


def tree(root: Path) -> dict[str, tuple[str, str | None]]:
    """``root`` as ``{relative path: (kind, digest)}`` — the whole shape, in one mapping.

    The KIND is compared as seriously as the content: a link is never a copy, whatever it
    points at. A digest rather than a size, because a size cannot see a same-length
    substitution and a skill is text a model obeys.

    The root itself is the ``""`` entry, so a skill directory replaced by a link is an
    ordinary difference. Paths are relative and slash-separated so the two sides compare on
    Windows. A path this cannot read becomes ``("unreadable", reason)``, which will not equal
    what the pack ships: a check that cannot see something must say so.
    """
    def entry(path: Path) -> tuple[str, str | None]:
        try:
            if _is_link(path):
                return ("link", None)
            if path.is_dir():
                return ("dir", None)
            return ("file", hashlib.sha256(path.read_bytes()).hexdigest())
        except OSError as exc:
            return ("unreadable", exc.strerror or str(exc))

    out = {"": entry(root)}
    for child in _entries(root):
        out[child.relative_to(root).as_posix()] = entry(child)
    return out


def _differences(shipped: dict, landed: dict) -> list[str]:
    """Every way ``landed`` fails to be ``shipped``, read off one comparison.

    Absent, extra, wrong kind, wrong content and unreadable all fall out of comparing two
    mappings, rather than being four passes each with its own edge cases.
    """
    out: list[str] = []
    for relative in sorted(set(shipped) | set(landed)):
        want, got = shipped.get(relative), landed.get(relative)
        label = relative or "."
        if want == got:
            continue
        if want is None:
            out.append(f"{label} (not in this pack)")
        elif got is None:
            out.append(f"{label} (missing)")
        elif got[0] == "unreadable":
            out.append(f"{label} (could not be read: {got[1]})")
        elif want[0] != got[0]:
            out.append(f"{label} (a {got[0]} where the pack ships a {want[0]})")
        else:
            out.append(f"{label} (different content)")
    return out


def do_verify(targets: list[Path], source: Path) -> int:
    """Does this install match the pack? Every skill present, complete, current, and no more.

    An install on an older release is reported too. That is a mismatch with *this* pack even
    though nothing is broken, and saying so is the point: this is the command the repair
    procedure relies on, and a state it cannot see is a state nobody will fix.
    """
    available = set(discover_skills(source))
    if not available:
        # A mistyped `--source` names a directory holding no skills, and every question
        # below is then asked against an empty pack, so a fresh target "matches" it.
        print(f"error: no skills found under {source} — nothing to verify against",
              file=sys.stderr)
        return NOTHING_TO_COMPARE
    expected_version = pack_version()
    problems = 0
    nothing_installed = 0
    for target in targets:
        try:
            names, meta = read_manifest(target)
        except ManifestUnreadable as exc:
            print(f"  {target}: UNREADABLE {exc}")
            problems += 1
            continue
        if not names:
            # Its OWN exit code, neither success nor mismatch. One failure code makes "you
            # have not installed this" indistinguishable from "your install is broken";
            # reporting success would have a check that claims to confirm a match say yes
            # to a machine holding none of the pack.
            print(f"  {target}: no manifest — nothing installed by this installer")
            nothing_installed += 1
            continue
        missing = [n for n in names if not (target / n).is_dir()]
        # A name already reported MISSING is not also RETIRED: two lines for one skill
        # reads as two problems and offers two remedies. MISSING is the actionable one.
        stale = [n for n in names if n not in available and n not in set(missing)]
        absent = [n for n in sorted(available) if n not in names]
        # A skill root that is a LINK is not an installed copy, whatever is behind it —
        # walking through it and comparing the referent reports a clean install of a tree
        # pointed at the source, which is the condition `--link` was removed to prevent,
        # reached through the command that certifies. `_is_link` covers the Windows
        # junction spelling too.
        linked = [n for n in names
                  if n not in missing and _is_link(target / n)]
        # Both directions, in one comparison: a path the pack ships and the install lacks,
        # a path the install holds and the pack does not, a link where a file belongs, and
        # bytes that differ are the same question asked of two mappings.
        differs = []
        for name in names:
            if name in missing or name in linked or name not in available:
                continue
            gaps = _differences(tree(source / name), tree(target / name))
            if gaps:
                differs.append((name, gaps))
        version = meta.get("version") or meta.get("source-version") or "unknown"
        print(f"  {target}: {len(names)} listed, version {version}")
        for name in missing:
            print(f"    MISSING   {name} is listed but not on disk")
        for name in linked:
            print(f"    LINKED    {name} is a link, not a copy — this installer only "
                  f"copies, so editing it would edit whatever it points at")
        for name in stale:
            print(f"    RETIRED   {name} is installed but no longer in this pack")
        for name in absent:
            print(f"    NOT YET   {name} ships in this pack but is not installed")
        for name, gaps in differs:
            print(f"    DIFFERS   {name}: {len(gaps)} path(s) differ from this pack; "
                  f"first: {gaps[0]} — re-run the installer")
        outdated = version != expected_version
        if outdated:
            print(f"    VERSION   recorded {version}, this pack is {expected_version}")
        problems += (len(missing) + len(linked) + len(stale) + len(absent) + len(differs)
                     + (1 if outdated else 0))
    # A real mismatch outranks an absent install: it is the one that needs repairing rather
    # than installing, and with several targets the actionable answer is the one to report.
    if problems:
        return MISMATCH
    return NOTHING_TO_COMPARE if nothing_installed else 0


def _path_arg(value: str) -> Path:
    """A path argument that is not the empty string.

    `Path("")` is `PosixPath(".")` — not an error, and not None. So `--target ""`, which is
    what an unset shell variable expands to, means "wherever you are standing": it installs
    the whole pack plus the manifest into the current directory and exits 0.

    An empty string is not a missing argument, and the two have to be told apart before
    anything is written.
    """
    if not value.strip():
        raise argparse.ArgumentTypeError(
            "expected a path, got an empty string (an unset shell variable?); "
            "an empty path means the current directory, which is never what was meant")
    return Path(value)


MINIMUM_PYTHON = (3, 10)


def python_too_old(version_info=None) -> str:
    """The refusal for an interpreter below :data:`MINIMUM_PYTHON`, or ``""``.

    The floor is pack-wide and not this function's doing; what was missing is the *check*.
    Without it, an older interpreter copies every skill and then dies at ``write_manifest``
    on a keyword it does not have, leaving the directories on disk with **no manifest** —
    the orphaned state the ownership ordering exists to prevent.

    ``version_info`` is injectable so both branches are testable from either interpreter.
    """
    actual = tuple(sys.version_info[:2]) if version_info is None else tuple(version_info[:2])
    if actual >= MINIMUM_PYTHON:
        return ""
    need = ".".join(str(part) for part in MINIMUM_PYTHON)
    have = ".".join(str(part) for part in actual)
    return (f"install.py: Python {need}+ is required; this is {have}. Nothing was "
            f"installed. The pack already requires {need}+ (plan-duel's engine refuses to "
            f"run without it), so this is an existing constraint reported before any files "
            f"are copied rather than a traceback part-way through.")


def main(argv=None) -> int:
    # Checked FIRST, before argument parsing and before any file is touched, because the
    # failure it prevents is a half-done install rather than a bad exit code.
    refusal = python_too_old()
    if refusal:
        print(refusal, file=sys.stderr)
        return 2

    # Every mode prints an em dash, and this is the FIRST command a user runs — on Windows
    # a console code page such as cp932 cannot represent one, so redirecting the output
    # raises UnicodeEncodeError while reporting a perfectly good install. Pinned to utf-8
    # so the bytes do not depend on the locale, `replace` so this can never itself raise.
    # Guarded because a caller may have replaced either stream with an object that has no
    # `reconfigure`, or one that refuses.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):  # pragma: no cover - depends on host stream
            pass

    parser = argparse.ArgumentParser(
        description="Install the portable agent skills.",
        epilog="With no mode flag, installs or updates.",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--uninstall", action="store_true",
                      help="remove the skills a previous run recorded")
    mode.add_argument("--verify", action="store_true",
                      help="report whether the install matches this pack")
    parser.add_argument("--target", action="append", type=_path_arg, metavar="DIR",
                        help="install here instead of the runtime defaults; repeatable")
    parser.add_argument("--force", action="store_true",
                        help="replace a skill directory this installer did not record")
    parser.add_argument("--source", type=_path_arg, default=SKILLS_SRC,
                        help=f"the skills directory to install from (default: {SKILLS_SRC})")
    args = parser.parse_args(argv)

    targets = args.target if args.target else default_targets()

    if args.uninstall:
        return do_uninstall(targets)
    if args.verify:
        return do_verify(targets, args.source)
    return do_install(targets, args.source, force=args.force)


if __name__ == "__main__":
    sys.exit(main())
