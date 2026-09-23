#!/usr/bin/env python3
"""Drive one review-panel run from a single command: plan, dispatch every round, land
every reply, call the engine's stages in-process, and resume a killed run from disk.

The engine (``review_panel.py``) spawns nothing and gains nothing from this file. It is
**imported** here and its stages are called in-process, so no engine work can outlive the
process that owns the run. Every spawn is this program's.

**Disk is the state, and every fact is derived by replaying immutable records.** No
counter, no cache and no marker file is authoritative where the records disagree with it.
That single rule is what makes a killed run resumable: there is nothing in memory whose
loss changes an answer.

Four things are worth knowing before reading the rest, because each one killed an earlier
shape of this program:

* **A file existing proves nothing about an attempt.** The supervisor creates its
  ``--findings`` path empty when it claims it, deletes it again on a failure, and writes
  its end marker into the display log *before* it routes the transcript. The only decided
  outcome is its final status line, which it prints last — so this program redirects that
  line into a per-attempt ``status.txt`` and treats **a complete, valid status record** as
  the one completion predicate.
* **A disposition is not a landing.** A crash between adjudicating an attempt and
  publishing the unit's answer must not cost the unit its answer, so every adoption pass
  replays unfinished publications instead of skipping adjudicated attempts.
* **Ownership is a lock the kernel releases, never a timestamp.** One lock for the whole
  run, held for this process's lifetime. Nothing here reads a clock to decide who owns a
  run directory; clocks are read only to decide whether an attempt is still *within its
  own deadline*.
* **A worker is handed opaque paths.** A verification unit's name spells the lane that
  raised its candidates, so neither its payload path nor its working directory may be the
  unit's directory.

Stdlib only, by the same rule as the engine.
"""

from __future__ import annotations

import argparse
import contextlib
import errno
import hashlib
import io
import json
import os
import re
import secrets
import shutil
import signal
import socket
import stat
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

# The engine lives beside this file. Its directory name carries a hyphen, so it is not a
# package and cannot be imported as one: the directory goes on ``sys.path`` and the module
# is imported by its own name, which is what the suites already do.
_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

import review_panel as engine  # noqa: E402

_PROG = "review_panel_run.py"

# Exit codes. 0 is a run that finished or was drained on request; 2 is a refusal, the
# engine's own code for one; 3 is a run that stopped part-way and can be resumed by
# running the same command again. The three are distinct because a caller scripting this
# needs to tell "done" from "not done yet" without reading prose.
EXIT_OK, EXIT_REFUSED, EXIT_STOPPED = 0, 2, 3


class DriverError(Exception):
    """Refused by name. Everything this program declines to do raises one of these, so the
    CLI has exactly one place that turns a refusal into a message and an exit code."""


class RunPaused(DriverError):
    """Something the host would not do, which decides nothing.

    **The run stops where it stands, exits non-zero and resumable, and names the path.**
    Raised rather than returned because every one of these sits in the middle of a sequence
    — adjudicate, then publish, then record — and the only safe thing to do with the rest of
    that sequence is not to run it. Its two subclasses are the two ways it happens; every
    caller that stops a run catches this base, because what to do about them is the same and
    only the sentence for the operator differs.
    """

    def __init__(self, path: Path | str, cause: BaseException, action: str):
        super().__init__(f"{path} could not be {action}: {cause}")
        self.path = Path(path)


class StorageFault(RunPaused):
    """A read or a write the volume could not take: no room, a read-only volume, a quota, a
    failing device.

    A storage failure is not a unit's answer and not a provider's outage, so nothing is
    inferred from an operation that did not happen.
    """

    def __init__(self, path: Path | str, cause: BaseException, action: str = "written"):
        super().__init__(path, cause, action)


class UnreadableRecord(RunPaused):
    """Something that is on disk and that the host refused to hand over.

    **An unreadable thing is not an absent thing.** A refusal answered with ``None``, ``[]``
    or "skipped" turns "I could not find out" into a confident negative — which deletes a
    live worker's directory, spends a unit's allowance, or publishes a report with a finding
    silently dropped out of it. So a refusal that is not the volume's still stops the run:
    an operator fixes what refused and runs the same command again.
    """

    def __init__(self, path: Path | str, cause: BaseException, action: str = "read"):
        super().__init__(path, cause, action)


# --------------------------------------------------------------------------- #
# small shared utilities
# --------------------------------------------------------------------------- #
def _resolved(path: Path) -> Path:
    """``path`` with both the directory and the leaf resolved, for comparing paths.

    Every comparison in this file goes through it. macOS answers ``/var`` with
    ``/private/var`` and Windows hands back 8.3 short names, so a run directory reached by
    one spelling fails a comparison against its own other spelling — and a containment test
    that fails open is a deletion outside the run directory.
    """
    try:
        return path.resolve()
    except (OSError, RuntimeError, ValueError):
        return path.absolute()


def _canonical(path: Path, what: str) -> Path:
    """``path`` with every symlink and junction on the way to it resolved, or a refusal.

    **The run's identity is one side of a path comparison, and this repo's rule is that both
    sides are resolved.** A run directory reached through a symlink and through its real
    path spells two different siblings, so a lock named from the spelling is two locks: two
    drivers each take one, each believes it owns the run, and they claim attempts for the
    same unit and overwrite each other's dispositions. Exclusive attempt-directory creation
    does not restore unit-level exclusion — it only decides which of the two wins each race.

    Non-strict, so a run directory that does not exist yet still resolves through its
    parents. A path the host cannot resolve at all is refused rather than falling back to
    the unresolved spelling, because the fallback is exactly the defect.
    """
    try:
        return path.resolve()
    except (OSError, RuntimeError, ValueError) as exc:
        raise DriverError(
            f"cannot resolve the {what} {path}: {exc}; a symlink loop on the way to it is "
            f"the usual cause, and an unresolved path cannot be used as this run's identity"
        ) from exc


def remove_tree(root: Path, inside: Path) -> None:
    """Remove a directory this program created, **including a hardened one**.

    A hardened snapshot (§7.3) is a tree nothing can delete from: on POSIX a directory
    without its write bit gives up no entry, so an ordinary recursive delete leaves the
    whole thing where it was and the next plan then refuses because the path exists. Write
    permission is restored first and the delete follows. Read and traverse permission are
    never removed by the hardening, so the walk itself always works.

    Whoever throws a finished run directory away by hand pays the same cost once, with
    ``chmod -R u+w`` or the platform's equivalent, before deleting it.

    **Best effort throughout, and that authorizes nothing**, because nothing downstream
    reads this as a fact. A tree it could not walk or could not remove is left where it is,
    and the caller either creates the same path again — where the host refuses, by name and
    loudly — or was clearing scratch nobody reads. It is the one call here that may end with
    the path still on disk, and no answer of any kind comes back from it.
    """
    try:
        os.stat(root)
    except OSError:
        # Nothing to walk, whatever the reason — the removal below tolerates the same, and
        # the sentence above is what makes both safe.
        return
    # **`inside` is the boundary, and it is the caller's to name.** Resolving `root` and
    # bounding the walk by THAT asks whether the tree contains itself, which it always does:
    # hand this a `root` that is a link to somewhere else, or one reached through a redirected
    # component above it, and every path under the far end answers "inside" and the chmod runs
    # there. The run directory is the thing a working copy must be under, and no argument
    # derived from `root` can stand in for it.
    #
    # Refused rather than narrowed: a root this cannot place is not walked and not removed.
    # The caller either creates the same path again — where the host refuses, by name and
    # loudly — or was clearing scratch nobody reads.
    # `resolve()` directly, NOT `_resolved`, and the difference is the whole guard.
    # `_resolved` falls back to the unresolved spelling when the host will not answer, which
    # is right where a comparison that cannot be made should simply not match — and fatal
    # here, because both sides fall back together: root and its parent then compare equal
    # lexically, both questions below pass, and `rmtree` follows the very link they exist to
    # refuse. A path this cannot place is a path it must not walk. Refused rather than
    # raised: this function is best-effort by contract, and a tree it could not walk is left
    # where it is, exactly as for one it could not read.
    try:
        base = root.resolve()
        seat = root.parent.resolve() / root.name
        bound = inside.resolve()
    except (OSError, RuntimeError, ValueError):
        return
    try:
        # **Two questions, and the second is not the first asked harder.**
        #
        # Does `root` SIT where it claims? The parent resolved with the leaf name put back on
        # it is where it claims; `base` is where it leads. A `root` that is a link answers
        # them differently, and the walk below follows where it leads — so a run directory's
        # `.partial` pointed at a sibling run had that sibling walked and its hardened
        # snapshot made writable, with `rmtree` then refusing the link and leaving the
        # permissions changed. Nothing about the boundary catches this: the sibling really is
        # inside the directory that holds both of them.
        if base != seat:
            return
        # And is it under the boundary the CALLER named? That is the question no property of
        # `root` can answer, because a redirect above the leaf moves the whole path before
        # any of it is read.
        if not base.is_relative_to(bound):
            return
    except (OSError, ValueError):
        return
    for path in [root, *root.rglob("*")]:
        try:
            mode = os.stat(path, follow_symlinks=False).st_mode
        except OSError:
            continue
        if stat.S_ISLNK(mode):
            continue
        # And the same question of every path the walk reaches, because a link inside a
        # contained root still leads out of it. `S_ISLNK` names one kind of thing that does
        # that and the kinds differ by platform: a Windows junction is a reparse point,
        # `rglob` descends whatever `is_dir()` accepts, and a chmod on the far side clears a
        # read-only attribute on files this run does not own — which the `rmtree` below
        # cannot put back, because it never touches them either. Asking where each path
        # RESOLVES answers for every kind at once and needs no claim about any of them.
        try:
            if not _resolved(path).is_relative_to(base):
                continue
        except (OSError, ValueError):
            continue
        with contextlib.suppress(OSError):
            os.chmod(path, mode | stat.S_IWUSR)
    shutil.rmtree(root, ignore_errors=True)


def _now() -> float:
    """Wall-clock seconds. Deliberately not a monotonic clock: every deadline here is
    compared against a time written to disk by a process that has since died, and a
    monotonic reading means nothing across a restart. The budget below is the one thing
    measured monotonically, because it measures *this* process's uptime."""
    return time.time()


def _absent(exc: OSError) -> bool:
    """Whether this failure means the path is genuinely **not there**. ``ENOENT``, and a
    parent component that is not a directory, which is the same answer reached one level
    up. Nothing else: a host that refused a read has not said the file is missing."""
    return isinstance(exc, (FileNotFoundError, NotADirectoryError))


def _refused(path: Path, exc: OSError, action: str = "read") -> RunPaused:
    """What a filesystem call this program could not complete becomes.

    **One function, so the rule below is stated once and cannot drift between its sites.** A
    storage errno pauses the run as the volume's failure; anything else pauses it as a
    record the host would not give up. Both decide nothing, and neither is ever an absence.
    """
    if _is_storage_exception(exc):
        return StorageFault(path, exc, action=action)
    return UnreadableRecord(path, exc, action=action)


def _stat_or_absent(path: Path, *, follow_links: bool = True) -> os.stat_result | None:
    """What the host says ``path`` IS, or ``None`` where it is genuinely not there.

    **`Path.exists`, `.is_file`, `.is_dir` and `.is_symlink` cannot be used to decide
    anything here, and this is what replaces them.** Each of them answers a question the
    host may refuse, and each turns that refusal into a plain ``False``. Which refusals — a
    symlink loop, a drive that is not ready, a name the platform cannot use, and on the
    newer interpreters every ``OSError`` there is — has moved between releases, and no
    version of any of them has ever distinguished a refusal from an absence. False is also
    the answer for a path that is simply not there, so the two arrive spelled the
    same and the caller reads "I could not find out" as "nothing is there". That confident
    negative is what publishes an error over a landed result, writes a resolution over an
    attempt already adjudicated, hands one grant file's name to two grants, and deletes a
    working directory the run is still keeping evidence in.

    ``follow_links=False`` asks about the link itself, for the guards that must not be
    answered by whatever the link points at.
    """
    try:
        return os.stat(path, follow_symlinks=follow_links)
    except OSError as exc:
        if _absent(exc):
            return None
        raise _refused(path, exc, action="examined") from exc


def _present(path: Path) -> bool:
    """Whether ``path`` is there at all, raising where the host will not say."""
    return _stat_or_absent(path) is not None


def _is_directory(path: Path) -> bool:
    """Whether ``path`` is a directory, raising where the host will not say."""
    info = _stat_or_absent(path)
    return info is not None and stat.S_ISDIR(info.st_mode)


def _is_junction(info: os.stat_result) -> bool:
    """Whether an ``lstat`` describes a Windows directory junction.

    **A junction is a link that no link test sees.** ``S_ISLNK`` is false for one and
    ``Path.is_symlink`` answers False, while ``is_dir`` follows it, so a walk that asks only
    those questions descends into a tree that is somewhere else entirely and records not one
    thing about the junction itself. The engine's inventory already answers this question and
    takes the same decision — record the junction, never follow it — so the two sides of one
    snapshot are measured by one rule.

    Read off the ``lstat`` the caller already took, which is exactly what
    ``os.path.isjunction`` reads and one stat fewer: a second call would also be a second
    moment, and the answer could change between them. ``st_reparse_tag`` exists on Windows
    alone, so every other platform answers False through the default.
    """
    return (getattr(info, "st_reparse_tag", 0)
            == getattr(stat, "IO_REPARSE_TAG_MOUNT_POINT", 0xA0000003))


def _walk_tree(root: Path) -> list[tuple[Path, os.stat_result]]:
    """Every entry under ``root``, each with its own ``lstat``, links never followed.

    **A directory this cannot list and an entry it cannot describe are refusals, not
    omissions.** ``Path.rglob`` swallows the failure and carries on, so a subtree the host
    would not hand over comes back as a tree that simply held nothing — and the caller then
    reports, hardens or verifies a set of files it never saw. ``root`` itself is not in the
    result; a caller that wants it already holds it.

    An entry that is genuinely gone by the time it is described — removed under the walk
    between the listing and the stat — is passed over, because there is nothing left to
    report and ``ENOENT`` is the one answer that means exactly that.

    **A junction is reported and never descended into**, the rule the engine's inventory
    already follows. Descended, it puts files that live outside ``root`` into the answer
    under names inside it, and leaves the junction itself out — so a tree that has had a
    junction added to it, or a measured directory replaced by one pointing at identical
    files somewhere else, comes back looking exactly like the tree that was measured.
    """
    found: list[tuple[Path, os.stat_result]] = []
    stack = [root]
    while stack:
        base = stack.pop()
        try:
            names = sorted(os.listdir(base))
        except OSError as exc:
            if _absent(exc):
                continue
            raise _refused(base, exc, action="listed") from exc
        for name in names:
            path = base / name
            try:
                info = os.stat(path, follow_symlinks=False)
            except OSError as exc:
                if _absent(exc):
                    continue
                raise _refused(path, exc, action="examined") from exc
            found.append((path, info))
            if stat.S_ISDIR(info.st_mode) and not _is_junction(info):
                stack.append(path)
    return found


def _read_json(path: Path) -> object | None:
    """The object at ``path``, or ``None`` where there is genuinely nothing to read.

    **The rule every filesystem read in this program obeys: a read either answers or
    raises.** ``None`` means the path is ABSENT, or holds something that is not a whole JSON
    object — the shape a process killed mid-write leaves, which every record here has to
    survive. It never means the host refused the read.

    Answering a refusal with ``None`` is the defect this rule exists to stop, and it is not
    a theoretical one: it deletes a live worker's directory, spends a unit's allowance on a
    failing disk, and replaces a deletion record with an empty one. A caller that genuinely
    has to tolerate an unreadable file says so **at the call**, with the reason; there are
    two, and both say why.
    """
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        if _absent(exc):
            return None
        raise _refused(path, exc) from exc
    except (UnicodeDecodeError, ValueError, RecursionError):
        return None


# **Every write this program makes goes through the engine's two writers**, which create a
# scratch file exclusively, write it with UTF-8 and `newline="\n"` pinned, and `os.replace`
# it into place — so a crash leaves the old file or none, never a half-written one, and no
# record here is ever read back through the host's locale.
#
# Bound by NAME rather than reached through the module, because `write_text` is also the
# name of a `pathlib.Path` method whose default codec is the host's: a call spelled
# `something.write_text(...)` is the shape `tests/test_encoding_hygiene.py` flags, and the
# two have nothing to do with each other beyond the name.
from review_panel import write_json as _engine_write_json  # noqa: E402
from review_panel import write_text as _engine_write_text  # noqa: E402


def _write_json(path: Path, obj: object) -> None:
    """The engine's writer, with a failure the disk caused turned into a run-wide stop.

    **Wrapped here and not in the engine.** The engine's stages are single commands that
    report a failure and end; this program is a loop that has to decide what a failed write
    means for the records around it, and the answer — stop, adjudicate nothing, name the
    path — is the loop's to make. Anything that is not the disk's doing propagates
    unchanged, because a value that will not serialize is a defect in this program and
    pausing the run would hide it.
    """
    try:
        _engine_write_json(path, obj)
    except (OSError, engine.ReviewPanelError) as exc:
        if _is_storage_exception(exc):
            raise StorageFault(path, exc) from exc
        raise


def _write_text(path: Path, text: str) -> None:
    """:func:`_write_json`'s rule for prose."""
    try:
        _engine_write_text(path, text)
    except (OSError, engine.ReviewPanelError) as exc:
        if _is_storage_exception(exc):
            raise StorageFault(path, exc) from exc
        raise


def drain_flags(rundir: Path, given: Path | None = None) -> tuple[Path, ...]:
    """Every path this run answers a drain request at.

    The canonical one first, and **the spelling the operator was given second**. Ownership
    canonicalizes the run directory, which is right — two spellings of one run are one run —
    but a suffix appended to an alias names a different sibling, not the alias's target. So
    an operator who follows `<rundir>.drain` for the `--rundir` they typed creates a file
    the canonical watcher never looks at, and the control does nothing. A control somebody
    can use exactly as documented and be ignored by is worse than no control, so both are
    watched and both are cleared.
    """
    paths = [rundir.with_name(rundir.name + DRAIN_SUFFIX)]
    # Compared as SPELLINGS, not as what they resolve to. The two resolve to the same run —
    # that is what makes it one run and one lock — so a comparison of the resolved paths
    # says "the same" for exactly the aliased case this exists to cover, and the operator's
    # own spelling would never be watched.
    if given is not None and given.name and str(given) != str(rundir):
        paths.append(given.with_name(given.name + DRAIN_SUFFIX))
    return tuple(paths)


def clear_drain_request(rundir: Path, given: Path | None = None) -> None:
    """Take a stale drain request off the disk.

    **Called the moment ownership is taken, before any lengthy startup work.** A request
    belongs to the run it was made against: left behind it would drain every later resume
    the moment it started, so a run could never be continued after being asked to stop.
    But planning copies a whole snapshot, and a request made during that is a request
    against THIS driver — clearing after it would delete what an operator had just asked
    for and dispatch anyway. Ownership is the line: before it, any request is somebody
    else's run; after it, every request is this one's.
    """
    canonical = drain_flags(rundir)[0]
    with contextlib.suppress(OSError):
        canonical.unlink()
    if given is None or not given.name or str(given) == str(rundir):
        return
    # The canonical flag is one-to-one with the run, and the lock is what makes that true.
    # The spelling an operator typed is not: an alias can be repointed, so `current.drain`
    # may belong to a run this driver does not own — and clearing it would erase a request
    # made against a live run, which is the defect this whole control keeps producing. So
    # the alias is cleared only while it still RESOLVES to this run.
    #
    # Ask that of the ALIAS, not of the flag's parent directory. Comparing parents was the
    # same question asked of the wrong thing: an alias normally lives in a different
    # directory from the run it points at — that is what makes it an alias — so the
    # comparison skipped every real one. The flag then stayed on disk while
    # :func:`drain_flags` went on watching it, and every resume through that spelling
    # drained the moment it started, forever.
    if _resolved(given) == _resolved(rundir):
        with contextlib.suppress(OSError):
            given.with_name(given.name + DRAIN_SUFFIX).unlink()


def _progress(rundir: Path, line: str) -> None:
    """Append one line to ``dispatch/progress.log``, the file a watching operator tails.
    Nothing on the correctness path reads it, so a failed append is ignored — but it is
    written before the thing it describes wherever the description is about a spawn, so a
    log that ends mid-round still names the last unit that was started."""
    stamp = time.strftime("%H:%M:%S", time.gmtime())
    try:
        target = rundir / engine.DISPATCH_DIR / "progress.log"
        target.parent.mkdir(parents=True, exist_ok=True)
        with open(target, "a", encoding="utf-8", newline="\n") as fh:
            fh.write(f"{stamp} {line}\n")
    except OSError:
        pass


# --------------------------------------------------------------------------- #
# 1. ownership — one lock for the whole run, taken before anything is inspected
# --------------------------------------------------------------------------- #
LOCK_SUFFIX = ".lock"
PARTIAL_SUFFIX = ".partial"
# How an operator asks a run to stop claiming, on every platform.
#
# **`SIGTERM` is not that mechanism everywhere, and pretending otherwise made a job red.**
# Windows delivers no POSIX signal: `signal.signal(SIGTERM, …)` is accepted there and the
# handler never runs, because the usual way to send one terminates the process outright
# rather than asking it anything. A drain that only works where signals do is a drain half
# the supported platforms do not have.
#
# So the portable request is a file: create `<rundir>.drain` and the loop stops claiming at
# its next look, lets what is running finish, and exits 0 resumable — the whole of §4's
# drain contract, without a signal. A sibling of the run directory for the lock's reason:
# the engine's run directory holds only what the engine writes.
#
# The request belongs to the run it was made against, so a driver removes it at startup.
DRAIN_SUFFIX = ".drain"


class RunLock:
    """Exclusive ownership of one run, held for this process's lifetime.

    ``<rundir>.lock`` — a **sibling** of the run directory, because the engine refuses a
    run directory holding anything it did not write. Created if absent and never deleted
    and never replaced: a lock file that gets removed is a lock two processes can both
    take, one holding a descriptor to a file that no longer has a name.

    **The kernel releases it when the process dies**, which is the whole reason there is no
    takeover command and no age heuristic anywhere in this program. A lock you can acquire
    needs no adoption; one you cannot acquire is held by a live driver.

    The contents — pid, host, start time — are diagnostic only. Nothing reads them to
    decide anything; they exist so a refusal can say who is holding the run.
    """

    def __init__(self, path: Path):
        self.path = path
        self._fd: int | None = None
        self._probe_fd: int | None = None

    # The two platform primitives, kept in one place so the refusal below can say which of
    # them is missing rather than failing at the call site.
    @staticmethod
    def _locking_available() -> str | None:
        if os.name == "nt":
            try:
                import msvcrt  # noqa: F401
            except ImportError:
                return "msvcrt is unavailable, so this host cannot lock a file at all"
            return None
        try:
            import fcntl  # noqa: F401
        except ImportError:
            return "fcntl is unavailable, so this host cannot lock a file at all"
        return None

    # **The byte that is locked, and where the diagnostic lives — which must not be the
    # same place.** Windows byte-range locks are mandatory: a second process cannot READ the
    # range the holder has locked, so a diagnostic written over the locked byte comes back
    # empty and the refusal names nobody. `flock` is whole-file and advisory, so POSIX never
    # showed it. The lock is byte 0 and the contents start at byte 1, which is outside every
    # lock this class takes and readable by anyone on both platforms.
    #
    # The pid is the whole value of that line: it is what tells an operator which process to
    # stop. Dropping it, or accepting a message without it, would leave them with a refusal
    # and nothing to do about it.
    _LOCK_BYTES = 1
    _CONTENTS_AT = 1

    @staticmethod
    def _take(fd: int) -> bool:
        """Try to take the lock on ``fd`` without blocking. True when it was taken."""
        if os.name == "nt":
            import msvcrt
            try:
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_NBLCK, RunLock._LOCK_BYTES)
            except OSError:
                return False
            return True
        import fcntl
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return False
        return True

    def acquire(self) -> None:
        missing = self._locking_available()
        if missing is not None:
            raise DriverError(
                f"this run needs exclusive ownership and {missing}; refusing before "
                f"anything is inspected"
            )
        try:
            fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
        except OSError as exc:
            raise DriverError(f"cannot open the run lock {self.path}: {exc}") from exc
        # Never inherited. A worker holding this descriptor would hold the run's ownership
        # for as long as it lived, so a driver that died would look alive.
        os.set_inheritable(fd, False)
        if not self._take(fd):
            held = ""
            with contextlib.suppress(OSError):
                os.lseek(fd, self._CONTENTS_AT, os.SEEK_SET)
                held = os.read(fd, 4096).decode("utf-8", "replace").strip("\x00").strip()
            os.close(fd)
            raise DriverError(
                f"{self.path} is held by a live driver"
                + (f" ({held})" if held
                   else " whose pid could not be read out of the lock file")
                + "; there is no takeover — wait for it, or stop it and run again"
            )
        self._fd = fd
        # **Locking that is unavailable or unreliable is refused; advisory locking is
        # not.** POSIX `flock` is advisory by design and that is the supported case, since
        # every cooperating driver takes it. What cannot be tolerated is a filesystem — a
        # network mount with no working lock manager — where two acquisitions both
        # succeed, because then the whole design's one exclusion is silently absent. A
        # second open file description in this same process is the cheapest probe for it:
        # on a working local filesystem it must fail.
        try:
            probe = os.open(self.path, os.O_RDWR, 0o600)
        except OSError:
            probe = -1
        if probe >= 0:
            os.set_inheritable(probe, False)
            if self._take(probe):
                os.close(probe)
                self.release()
                raise DriverError(
                    f"{self.path} can be locked twice at once, so locking on this "
                    f"filesystem does not exclude anything; put the run directory on a "
                    f"local filesystem"
                )
            os.close(probe)
        try:
            # Truncated to the lock byte and written after it, so the contents never sit in
            # the range a second process cannot read.
            os.ftruncate(self._fd, self._CONTENTS_AT)
            os.lseek(self._fd, self._CONTENTS_AT, os.SEEK_SET)
            os.write(self._fd, json.dumps({
                "pid": os.getpid(), "host": socket.gethostname(),
                "since": time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime()),
            }).encode("utf-8"))
        except OSError:
            # Diagnostics only. A lock that is held but could not describe itself is still
            # held, and failing here would trade the exclusion for a cosmetic.
            pass

    def release(self) -> None:
        """Drop the lock. The file itself stays, deliberately — see the class docstring."""
        if self._fd is None:
            return
        fd, self._fd = self._fd, None
        with contextlib.suppress(OSError):
            if os.name == "nt":
                import msvcrt
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_UNLCK, self._LOCK_BYTES)
        with contextlib.suppress(OSError):
            os.close(fd)

    def __enter__(self) -> "RunLock":
        self.acquire()
        return self

    def __exit__(self, *_exc) -> None:
        self.release()


# --------------------------------------------------------------------------- #
# the adapter config — every spawn's argv arrives here as DATA
# --------------------------------------------------------------------------- #
# The driver names no product anywhere. Each lane's command line is supplied by the
# caller, exactly as plan-duel takes its participants' commands, and this module only
# renders and runs it. A permission mode is per unit rather than per lane, so each lane
# declares two commands: the read-only one its readers and clusterers run under, and the
# write-capable one its verifiers, its synthesizers and the capability probe need.
PLACEHOLDER_OPEN, PLACEHOLDER_CLOSE = "⟪", "⟫"
_PLACEHOLDER_RE = re.compile(r"⟪([^⟪⟫]+)⟫")

# What this driver fills. `prompt` is the one-line instruction naming the payload by
# absolute path; `payload` and `schema` are that file and the unit's schema, both under an
# opaque per-attempt directory; `input` is that directory; `cwd` is the working directory
# the worker runs in; `transcript` is the path the supervisor collects the reply at, which
# only an `external-file` adapter needs.
PLACEHOLDERS = frozenset({"prompt", "payload", "schema", "input", "cwd", "transcript"})
# One of these must appear, or the worker is launched with no task at all — the failure
# plan-duel measured, where an accepted-and-ignored prompt mode ran a CLI with no prompt.
CARRIES_THE_TASK = frozenset({"prompt", "payload"})

READ_ONLY, WRITE_CAPABLE = "read_only", "write_capable"
MODES = (READ_ONLY, WRITE_CAPABLE)
RESULT_MODES = frozenset({"external-file", "stream-json-result-event", "stream-transcript"})

_LANE_KEYS = frozenset({"runtime", "model", "account", "adapter", "slots",
                        "provider_fault_patterns", READ_ONLY, WRITE_CAPABLE})
_LANE_REQUIRED = ("runtime", "model", "adapter", READ_ONLY, WRITE_CAPABLE)
# How many of a lane's workers run at once when the config does not say. The number sets how
# long a run takes and the right one is the account's rate limit, so a defaulted lane is
# SAID to be defaulted in the preview: a default nobody sees is a run length nobody chose.
SLOTS_DEFAULT = 2
_MODE_KEYS = frozenset({"command", "permission", "result_mode", "idle", "deadline"})
_MODE_REQUIRED = ("command", "permission")

# The kinds that write, and the ONE place that says so: the working directory, the
# permission mode, the run-wide serialization, the disk headroom and the copy's retention
# are all read off this set. The capability probe runs a build and a test suite, a verifier
# runs the reproductions it is handed as well as any it builds, and a synthesizer is told
# by its brief that its working directory is its own to write in.
#
# **A copy is containment, not scratch space, so what belongs here is every kind whose
# brief PERMITS writing — not only the kinds expected to write something useful.** A lane's
# read-only mode is not uniformly enforced: one runtime bounds the whole process in the
# kernel, another refuses the edit tools and every shell command it judges would change a
# file — a permission check, so an incidental write by a command judged read-only (a Python
# import dropping bytecode) still lands. Given the snapshot as its working directory,
# such a worker does what its brief says and rewrites the pinned tree every other unit of
# the run was measured against, and the next snapshot check refuses the whole run after the
# reading and verification rounds have already been paid for.
WRITE_CAPABLE_KINDS = frozenset({engine.PROBE_KIND, engine.VERIFIER_KIND,
                                 engine.SYNTHESIZER_KIND})


@dataclass(frozen=True)
class ModeSpec:
    command: tuple[str, ...]
    permission: str
    result_mode: str
    idle: float
    deadline: float


@dataclass(frozen=True)
class LaneSpec:
    runtime: str
    model: str
    account: str
    adapter: str
    modes: dict[str, ModeSpec]
    # How many of this lane's attempts may run at once. Write-capable units are further
    # limited to one at a time across both lanes, whatever this says.
    slots: int = SLOTS_DEFAULT
    slots_stated: bool = True
    # **The wording of a provider's refusal is the provider's to change**, so what counts as
    # one lives in the configuration and never in this file. Compiled at parse time, so a
    # pattern that cannot be compiled is refused before a run is planned rather than raising
    # in the middle of adjudicating an attempt.
    fault_patterns: tuple[re.Pattern, ...] = ()


def _parse_mode(lane: str, mode: str, raw: object) -> ModeSpec:
    where = f"lanes.{lane}.{mode}"
    if not isinstance(raw, dict):
        raise DriverError(f"{where} must be a JSON object")
    unknown = sorted(set(raw) - _MODE_KEYS)
    if unknown:
        raise DriverError(f"{where} has unknown key(s): {', '.join(unknown)}")
    for key in _MODE_REQUIRED:
        if key not in raw:
            raise DriverError(f"{where} is missing required key {key!r}")
    command = raw["command"]
    if (not isinstance(command, list) or not command
            or not all(isinstance(part, str) for part in command)):
        raise DriverError(f"{where}.command must be a non-empty list of strings")
    used: set[str] = set()
    for part in command:
        used.update(_PLACEHOLDER_RE.findall(part))
    unfilled = sorted(used - PLACEHOLDERS)
    if unfilled:
        spelled = ", ".join(f"{PLACEHOLDER_OPEN}{name}{PLACEHOLDER_CLOSE}"
                            for name in unfilled)
        raise DriverError(
            f"{where}.command uses marker(s) this driver never fills: {spelled} "
            f"(it fills: {', '.join(sorted(PLACEHOLDERS))})"
        )
    if not used & CARRIES_THE_TASK:
        spelled = ", ".join(f"{PLACEHOLDER_OPEN}{name}{PLACEHOLDER_CLOSE}"
                            for name in sorted(CARRIES_THE_TASK))
        raise DriverError(
            f"{where}.command carries no task: one of {spelled} has to appear in it, or "
            f"the worker is launched with nothing to do and fails for a reason that "
            f"points nowhere near the missing prompt"
        )
    permission = raw["permission"]
    if not isinstance(permission, str) or not permission.strip():
        raise DriverError(f"{where}.permission must be a non-empty string")
    result_mode = raw.get("result_mode", "stream-transcript")
    if result_mode not in RESULT_MODES:
        raise DriverError(
            f"{where}.result_mode must be one of: {', '.join(sorted(RESULT_MODES))}")
    def _seconds(key: str, default: float) -> float:
        value = raw.get(key, default)
        if not isinstance(value, (int, float)) or isinstance(value, bool) or value <= 0:
            raise DriverError(f"{where}.{key} must be a positive number of seconds")
        return float(value)
    return ModeSpec(command=tuple(command), permission=permission.strip(),
                    result_mode=result_mode, idle=_seconds("idle", 900.0),
                    deadline=_seconds("deadline", 3600.0))


def parse_adapter_config(data: str | dict) -> dict[str, LaneSpec]:
    """Parse the per-lane adapter configuration into ``{lane: LaneSpec}``.

    Never scraped out of prose: the input is a structured document, so a command line
    reaches this program the way data does and not the way a screenshot does.
    """
    if isinstance(data, str):
        try:
            obj = json.loads(data)
        except (json.JSONDecodeError, RecursionError) as exc:
            raise DriverError(f"adapter config is not valid JSON: {exc}") from exc
    else:
        obj = data
    if not isinstance(obj, dict) or not isinstance(obj.get("lanes"), dict):
        raise DriverError("adapter config must be a JSON object with a 'lanes' object")
    unknown = sorted(set(obj) - {"lanes"})
    if unknown:
        raise DriverError(f"adapter config has unknown key(s): {', '.join(unknown)}")
    raw_lanes = obj["lanes"]
    missing = [lane for lane in engine.LANES if lane not in raw_lanes]
    if missing:
        raise DriverError(f"adapter config is missing lane(s): {', '.join(missing)}")
    extra = sorted(set(raw_lanes) - set(engine.LANES))
    if extra:
        raise DriverError(f"adapter config has unknown lane(s): {', '.join(extra)}")
    lanes: dict[str, LaneSpec] = {}
    for lane in engine.LANES:
        raw = raw_lanes[lane]
        if not isinstance(raw, dict):
            raise DriverError(f"lanes.{lane} must be a JSON object")
        unknown = sorted(set(raw) - _LANE_KEYS)
        if unknown:
            raise DriverError(f"lanes.{lane} has unknown key(s): {', '.join(unknown)}")
        for key in _LANE_REQUIRED:
            if key not in raw:
                raise DriverError(f"lanes.{lane} is missing required key {key!r}")
        for key in ("runtime", "model", "adapter"):
            if not isinstance(raw[key], str) or not raw[key].strip():
                raise DriverError(f"lanes.{lane}.{key} must be a non-empty string")
        account = raw.get("account", raw["runtime"])
        if not isinstance(account, str) or not account.strip():
            raise DriverError(f"lanes.{lane}.account must be a non-empty string")
        slots = raw.get("slots", SLOTS_DEFAULT)
        if not isinstance(slots, int) or isinstance(slots, bool) or slots < 1:
            raise DriverError(f"lanes.{lane}.slots must be a whole number of at least 1: "
                              f"how many of this lane's workers may run at once")
        lanes[lane] = LaneSpec(
            runtime=raw["runtime"].strip(), model=raw["model"].strip(),
            account=account.strip(), adapter=raw["adapter"].strip(), slots=slots,
            slots_stated="slots" in raw,
            modes={mode: _parse_mode(lane, mode, raw[mode]) for mode in MODES},
            fault_patterns=_parse_fault_patterns(lane, raw.get("provider_fault_patterns")),
        )
    _refuse_a_configuration_the_report_could_not_describe(lanes)
    return lanes


def _parse_fault_patterns(lane: str, raw: object) -> tuple[re.Pattern, ...]:
    """The patterns that make a terminal failure this account's outage rather than a bad
    answer. Case-insensitive regular expressions, and an empty list is a valid answer: a
    caller who declares none has said that every failure here is the worker's until two of
    them explain nothing, which is the rule that still holds without any configuration."""
    if raw is None:
        return ()
    where = f"lanes.{lane}.provider_fault_patterns"
    if not isinstance(raw, list) or not all(isinstance(part, str) for part in raw):
        raise DriverError(f"{where} must be a list of strings")
    compiled = []
    for pattern in raw:
        if not pattern.strip():
            raise DriverError(f"{where} holds an empty pattern, which matches every failure")
        try:
            compiled.append(re.compile(pattern, re.IGNORECASE))
        except re.error as exc:
            raise DriverError(
                f"{where}: {pattern!r} is not a valid regular expression: {exc}") from exc
    return tuple(compiled)


def _refuse_a_configuration_the_report_could_not_describe(
        lanes: dict[str, LaneSpec]) -> None:
    """Refuse two lanes on one runtime with **different models**, before anything is planned.

    The report has one sentence for a one-runtime run and it says the candidates were
    checked by the same model, calling any disagreement a difference of context. That
    sentence is false of two models on one runtime, and the run would have no honest rung to
    record: it is not two runtimes either. Refusing the configuration keeps every rendered
    sentence true without touching the renderer — and it has to happen here, at startup,
    because a run that discovered it later would have already spent a whole reading round.
    """
    by_runtime: dict[str, set[str]] = {}
    for spec in lanes.values():
        by_runtime.setdefault(spec.runtime, set()).add(spec.model)
    for runtime, models in sorted(by_runtime.items()):
        if len(models) > 1:
            raise DriverError(
                f"both lanes run on {runtime!r} but name different models "
                f"({', '.join(sorted(models))}); the report describes a one-runtime run as "
                f"checked by the same model, which that is not, and it is not a two-runtime "
                f"run either. Give the two lanes one model, or two runtimes"
            )


def load_adapter_config(path: str | Path) -> dict[str, LaneSpec]:
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError as exc:
        raise DriverError(f"cannot read the adapter config {path}: {exc}") from exc
    return parse_adapter_config(text)


def lane_descriptions(lanes: dict[str, LaneSpec],
                      executed: "dict[str, LaneExecution] | None" = None) -> dict[str, dict]:
    """One adapter line and one permission line per lane, for the record and the refusal.

    **Configured, not observed — and then observed as well.** The adapter, runtime and model
    come from the pinned configuration, so a lane whose every attempt failed to launch still
    has a truthful record rather than an empty one; what actually ran is derived from the
    attempts and said beside it, so "configured, never executed" reads as what it is. The
    engine renders both strings verbatim.

    Both permission modes are stated, because readers run read-only and write-capable units
    run in a writable copy, and one winner's permission cannot stand for the lane.

    Separate from :func:`dispatch_record` because the refusal a stranded lane raises quotes
    these same strings, and it is raised at a round boundary where there is no record to
    write: what became of that lane's attempts is the whole of what an operator needs, and
    it must read the same both times.
    """
    described: dict[str, dict] = {}
    for lane, spec in lanes.items():
        ran = None if executed is None else executed.get(lane)
        if ran is None:
            did = ""
        elif not ran.executed:
            # **Prepared is not executed.** A lane every one of whose launches raised has a
            # spawn record per attempt and no supervisor ever ran, so the count of prepared
            # launches is said as what it is rather than standing in for work done.
            did = " — configured, never executed"
            # Told apart, because they are two different claims: one says the launch raised
            # and nothing ran, the other says nobody can tell. Summed, the stronger of the
            # two would be asserted about attempts that never earned it.
            if ran.prepared - ran.unknown:
                did += f" ({ran.prepared - ran.unknown} launch(es) never started)"
            if ran.unknown:
                did += (f" ({ran.unknown} launch(es) prepared with no record of whether "
                        f"the supervisor started)")
        else:
            did = (f" — {ran.executed} attempt(s) executed, "
                   f"{ran.landed} unit(s) landed")
            if ran.unknown:
                # Said, not folded into either count: a reader weighing what a lane
                # contributed needs to know how much of it is unaccounted for.
                did += (f", {ran.unknown} attempt(s) with no record of whether the "
                        f"supervisor started")
        described[lane] = {
            "adapter": f"{spec.adapter} ({spec.runtime}, {spec.model}){did}",
            "permission": "; ".join(
                f"{'read-only units' if mode == READ_ONLY else 'write-capable units'}: "
                f"{spec.modes[mode].permission}"
                + ("" if ran is None or mode in ran.modes else " (none ran)")
                for mode in MODES),
        }
    return described


def dispatch_record(lanes: dict[str, LaneSpec],
                    executed: "dict[str, LaneExecution] | None" = None) -> dict:
    """``dispatch.json``: the configuration always, and what executed where there is any.

    The lanes are described before the rung is derived, because deriving it is what refuses
    a run that reached only one of them, and that refusal quotes the descriptions.
    """
    described = lane_descriptions(lanes, executed)
    return {"rung": _derived_rung(lanes, executed, described), "lanes": described}


def no_landing_refusal(stranded: Sequence[str], described: dict[str, dict]) -> str:
    """The message a lane that landed nothing ends the run with, written once.

    Once, because it is raised from two places — the round boundary that finds it first and
    the record that would otherwise have to name a rung for it — and a user meeting the
    second should not be told something different from the first.

    **It says what happened and explains nothing further.** The true statement is narrow
    and is enough: no unit answered on this lane, so nothing it was asked to read reached
    the run and nothing it was asked to check was checked. What must never be said here is
    that the findings were checked where they were raised — routing addresses a candidate
    to the lane that did not raise it and keeps doing so, and a candidate whose verifier
    never answered is recorded unresolved rather than handed back to its finder. A message
    a user meets at the moment a run fails is the worst place in the program to explain it
    with a mechanism the program does not have.
    """
    named = "; ".join(f"lane {lane} landed no unit — {described[lane]['adapter']}"
                      for lane in stranded)
    return (
        f"{named}. A lane that lands nothing answered none of the units it was given: what "
        f"it was asked to read never reached this run, and every candidate addressed to it "
        f"is unresolved — so no report of this run could say a finding was checked by a "
        f"unit that did not raise it. Why each attempt ended as it did is recorded beside "
        f"that unit under {engine.DISPATCH_DIR}/ in the run directory. Put right what those "
        f"records name and plan a new run: this one cannot re-ask a unit it has already "
        f"answered with an error"
    )


# --------------------------------------------------------------------------- #
# 2. bootstrap and the stage machine
# --------------------------------------------------------------------------- #
# Which unit kinds each marker's round dispatches, and which engine stage follows it. The
# marker in `units.json` is the only thing consulted: nothing here keys on a directory or
# a payload existing, so a round that legitimately produced zero units is a committed
# round rather than one that gets replanned forever.
#
# **`engine.ROUTED_STAGE` is deliberately absent, and it is not a sixth round.** That value
# is written into `candidates.json`, never into `units.json`, and it is a TYPE TAG rather
# than a position: `_read_candidates` refuses a file whose `stage` is not `routed`, which is
# how it tells the engine's route record from some other JSON. A row for it here matched
# nothing — `_marker` reads `units.json` — while implying the run passes through six stages
# when `units.json` only ever holds these five.
REPORTED_STAGE = engine.REPORTED_STAGE
ROUNDS: dict[str, tuple[tuple[str, ...], str]] = {
    engine.READING_STAGE: ((engine.READER_KIND, engine.AUDITOR_KIND, engine.PROBE_KIND),
                           "route"),
    engine.VERIFICATION_STAGE: ((engine.VERIFIER_KIND,), "cluster"),
    engine.CLUSTERED_STAGE: ((engine.CLUSTERER_KIND,), "synthesize"),
    engine.SYNTHESIZED_STAGE: ((engine.SYNTHESIZER_KIND,), "report"),
}


def _marker(rundir: Path) -> str | None:
    doc = _read_json(rundir / engine.UNITS_FILE_NAME)
    if not isinstance(doc, dict) or not isinstance(doc.get("units"), list):
        return None
    stage = doc.get("stage")
    return stage if isinstance(stage, str) else None


def _units(rundir: Path) -> list[dict]:
    doc = _read_json(rundir / engine.UNITS_FILE_NAME)
    if not isinstance(doc, dict) or not isinstance(doc.get("units"), list):
        raise DriverError(f"{rundir / engine.UNITS_FILE_NAME} is not a units listing")
    return [unit for unit in doc["units"] if isinstance(unit, dict)]


def bootstrap(rundir: Path, job: Path | None) -> None:
    """Classify the target and, where there is no committed run, plan one.

    The classification deletes nothing it has not examined:

    ==============================  ==========================================
    on disk                         action
    ==============================  ==========================================
    ``<rundir>/units.json``         a committed run — resume
    ``<rundir>``, no ``units.json`` refused **by name**; nothing is deleted
    only ``<rundir>.partial``       planning never committed — remove and plan
    both                            ``<rundir>`` wins; the partial is removed
    neither                         a new run — plan
    ==============================  ==========================================

    Planning happens in ``<rundir>.partial`` and is committed by ``os.replace`` onto
    ``<rundir>``, which does not exist — so the directory move is a plain rename, which
    every supported platform performs. Replacing an *existing* directory is what Windows
    refuses, and the table above never does that.
    """
    partial = rundir.with_name(rundir.name + PARTIAL_SUFFIX)
    # Asked of the host with a refusal available, because the answer decides whether this
    # directory is a committed run or a fresh place to plan into. Read as "no committed run"
    # a refusal here plans a second time over a run that is already under way.
    if _present(rundir / engine.UNITS_FILE_NAME):
        if _present(partial):
            # The committed run wins. The partial is this program's own uncommitted
            # scratch and nothing has ever read it.
            remove_tree(partial, partial.parent)
        return
    if _present(rundir):
        raise DriverError(
            f"{rundir} exists and holds no {engine.UNITS_FILE_NAME}, so it is an "
            f"interrupted plan or somebody else's directory; it is refused by name and "
            f"nothing in it is deleted — move it aside and run again"
        )
    if job is None:
        raise DriverError(
            f"{rundir} does not exist and no --job was given, so there is nothing to plan")
    if _present(partial):
        remove_tree(partial, partial.parent)
    try:
        partial.mkdir(parents=True)
    except OSError as exc:
        raise DriverError(f"cannot create {partial}: {exc}") from exc
    # Copied in. The driver never treats a file already sitting in the target as its
    # input: a run directory is what this program writes, not a place to leave things.
    try:
        shutil.copyfile(job, partial / engine.JOB_FILE_NAME)
    except OSError as exc:
        remove_tree(partial, partial.parent)
        raise DriverError(f"cannot copy the job file into {partial}: {exc}") from exc
    code, out, err = _engine_stage("plan", str(partial / engine.JOB_FILE_NAME),
                                   "--rundir", str(partial))
    if code != 0:
        remove_tree(partial, partial.parent)
        raise DriverError(f"plan refused this job and nothing was committed:\n{err or out}")
    # The interview's record of which job fields nobody stated, where it wrote one beside the
    # job. Copied AFTER plan and never before: ``check_rundir`` refuses a run directory
    # holding anything but the job being loaded, and widening that guard to admit this file
    # would admit every other one too. Absent is the ordinary case and not a failure — a job
    # handed in ready-made has no notes — so nothing here refuses a run for the want of it.
    notes = job.with_name(engine.JOB_NOTES_FILE_NAME)
    if _present(notes):
        try:
            shutil.copyfile(notes, partial / engine.JOB_NOTES_FILE_NAME)
        except OSError as exc:
            remove_tree(partial, partial.parent)
            raise DriverError(
                f"cannot copy {notes} into {partial}: {exc}; it records which job fields the "
                f"interview chose, and a run that dropped it would report that nothing did"
            ) from exc
    # **Hardened before the rename commits**, so a committed run directory is always a
    # hardened one and there is no window in which readers could be measured against a tree
    # anything can still write to. Done here rather than by the engine because the engine
    # writes the snapshot as one of several outputs and this is a property of the whole run.
    harden_snapshot(partial / SNAPSHOT_DIR)
    try:
        os.replace(partial, rundir)
    except OSError as exc:
        raise DriverError(
            f"cannot commit the planned run directory onto {rundir}: {exc}") from exc
    _progress(rundir, f"planned {len(_units(rundir))} units")


# The claims the engine's stages take on a run directory. Enumerated rather than swept: a
# `.lock` this list does not name is not this program's to remove.
ENGINE_STAGE_CLAIMS = ("route", "cluster", "synthesize", "report")


def clear_engine_claims(rundir: Path) -> list[str]:
    """Remove the stage claims a killed engine stage left, and name what was cleared.

    **Phase 7 took a per-stage claim and disclosed the cost: a hard-killed stage strands it
    and the next run refuses by name until somebody removes it.** That phase also said what
    would fix it — a process that owns the run from end to end, which is this one. So this
    is where the reconciliation belongs, and the run lock is exactly what makes it safe: a
    driver that holds it knows no other driver is running a stage, and the engine's stages
    run only in-process, so a claim still on disk is one nobody is holding. The engine
    cannot reason that way about itself, which is why it refuses instead.

    Only a regular file, and only one of the enumerated names: a directory or a link at that
    path is something else, and it is left for the engine to refuse by name.

    A claim this cannot examine or cannot remove is passed over, and that decides nothing:
    the stage that runs next meets the same file, refuses over it by name, and says so. This
    clears a claim to spare an operator that refusal — it never authorizes the stage.
    """
    cleared = []
    for stage in ENGINE_STAGE_CLAIMS:
        claim = rundir / f"{stage}{engine.STAGE_LOCK_SUFFIX}"
        try:
            if not claim.is_file() or claim.is_symlink():
                continue
            claim.unlink()
        except OSError:
            continue
        cleared.append(claim.name)
    return cleared


def _engine_stage(*argv: str) -> tuple[int, str, str]:
    """Run one engine stage **in-process** and return ``(code, stdout, stderr)``.

    In-process is the requirement, not a convenience: a stage run as a child would outlive
    a driver that was killed, and then two processes would be writing one run directory
    while the lock says one owns it. The engine's entry point is called directly and its
    two streams are captured so the driver can put them in its own output.
    """
    out, err = io.StringIO(), io.StringIO()
    try:
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = engine.main(list(argv))
    except SystemExit as exc:  # pragma: no cover - the engine returns rather than exits
        code = exc.code if isinstance(exc.code, int) else 1
    except engine.ReviewPanelError as exc:
        return 2, out.getvalue(), f"{exc}\n"
    return int(code), out.getvalue(), err.getvalue()


# --------------------------------------------------------------------------- #
# 3. the attempt protocol
# --------------------------------------------------------------------------- #
ARGV_NAME = "argv.json"
STATUS_NAME = "status.txt"
# The supervisor's stderr, kept per attempt. Its one status line goes to stdout; what it
# says when it cannot get that far -- a usage error from a flag it does not know, a
# traceback -- goes here, and is the only account of a supervisor that ended silently.
STDERR_NAME = "stderr.txt"
# How much of that stderr an ended-silently status carries. A tail, because the reason
# is at the end of a traceback and a usage error is short.
STDERR_TAIL_BYTES = 2000
TRANSCRIPT_NAME = "transcript.md"
DISPLAY_NAME = "display.log"
REPLY_NAME = "reply.json"
DISPOSITION_NAME = "disposition.json"
RESOLUTION_NAME = "resolution.json"
LANDED_NAME = "landed.json"
# The operator's decision about a whole unit, written once, beside that unit's attempts.
# Distinct from an attempt's `resolution.json`, which is about one launch.
UNIT_RESOLUTION_NAME = "unit-resolution.json"

ADJUDICATED, RESOLVED, DECIDED = "adjudicated", "resolved", "decided"
ORPHAN_CLAIM, RUNNING, UNCERTAIN = "orphan-claim", "running", "uncertain"

# Grace **authorizes nothing.** Its expiry only moves an attempt to `uncertain`, which
# quarantines its unit and keeps its capacity reservation, because the worker may still be
# alive. It is not a licence to re-spawn, and no code path treats it as one.
GRACE_DEFAULT = 120.0

# What one attempt may make the supervisor hold. Generous enough that no real reply reaches
# it — the extractor scans the last four megabytes of a transcript, so a bound below that
# would throw away text the landing would have read — and finite, because four hundred
# attempts with no bound at all is a program whose memory is one worker's to choose.
MAX_CAPTURE_BYTES_DEFAULT = 8 << 20

ACCEPTED, REFUSED, NO_REPLY = "accepted", "refused", "no-reply"
WORKER_FAILED, LAUNCH_FAILED = "worker-failed", "launch-failed"
PROVIDER_UNAVAILABLE, INFRASTRUCTURE = "provider-unavailable", "infrastructure"
OPERATOR_FAILED, OPERATOR_RETRY = "operator-failed", "operator-retry"
OUTCOMES = (ACCEPTED, REFUSED, NO_REPLY, WORKER_FAILED, LAUNCH_FAILED,
            PROVIDER_UNAVAILABLE, INFRASTRUCTURE, OPERATOR_FAILED, OPERATOR_RETRY)
# The outcomes adjudication can only reach by reading a supervisor's status record, which
# makes each of them a proof that the supervisor ran. An operator's resolution is not one:
# it is a person deciding what to do about an attempt nobody could account for, and §6.4's
# provenance asks what executed, not what was given up on.
_FROM_A_STATUS = frozenset({ACCEPTED, REFUSED, NO_REPLY, WORKER_FAILED,
                            PROVIDER_UNAVAILABLE, INFRASTRUCTURE})

# What a status line's text has to name for the failure to be storage rather than the
# worker's. A full disk reaches the caller as `routing failed: [Errno 28] ...`, which read
# as an ordinary worker failure would charge a unit for the host running out of room — and
# four of those would publish "this unit failed" as the unit's answer.
# The phrases, matched anywhere and in any case: each is several words and names nothing
# else. The errno NAMES are matched separately, as whole upper-case tokens -- `eio` is
# inside `fileio.c`, `audio` and a user called Deion, and a substring match on it paused a
# run over an engine rejection that quoted a path, then re-dispatched the unit on every
# resume until it hit the launch ceiling.
_STORAGE_PHRASES = ("no space left", "read-only file system", "disk quota exceeded")
_STORAGE_NAME_TEXT = re.compile(r"\b(?:ENOSPC|EROFS|EIO|EDQUOT)\b")
# The same four faults as the numbers a message carries them by. Matched with the number
# ENDED rather than as bare text, because `errno 5` is a prefix of `errno 51` and of every
# other number that starts with it: read as a substring, a fault that is not one of these
# four pauses a run that should have refused it.
_STORAGE_ERRNO_TEXT = re.compile(
    r"errno\s*(?:" + "|".join(
        str(code) for code in sorted(
            code for code in (getattr(errno, name, None)
                              for name in ("ENOSPC", "EROFS", "EIO", "EDQUOT"))
            if code is not None)) + r")\b")
# The same four faults as numbers, for an exception this program caught itself rather than
# a line of prose a supervisor printed. Asked first, because an error number is what the
# host actually said and the text beside it is the C library's translation of it, which a
# locale may have changed.
_STORAGE_ERRNOS = frozenset(
    code for code in (getattr(errno, name, None)
                      for name in ("ENOSPC", "EROFS", "EIO", "EDQUOT"))
    if code is not None)


def _names_storage_fault(text: str) -> bool:
    """Whether some prose names one of the four faults that are the host's and not a
    worker's. One matcher for both callers — a supervisor's status line and this program's
    own failed write — so the two can never disagree about what a full disk looks like."""
    lowered = text.lower()
    if any(phrase in lowered for phrase in _STORAGE_PHRASES):
        return True
    if _STORAGE_NAME_TEXT.search(text) is not None:
        return True
    return _STORAGE_ERRNO_TEXT.search(lowered) is not None


def _is_storage_exception(exc: BaseException) -> bool:
    """Whether this failure is the disk's.

    The error number is asked of the whole chain rather than the outermost exception: the
    engine's writers wrap an ``OSError`` in a refusal of their own, so the number that says
    ENOSPC is one or two links down. The text is the fallback, for a host or a wrapper that
    kept only the message.
    """
    seen, current = 0, exc
    while current is not None and seen < 8:
        if isinstance(current, OSError) and current.errno in _STORAGE_ERRNOS:
            return True
        current = current.__cause__ or current.__context__
        seen += 1
    return _names_storage_fault(str(exc))


def _host_refused(exc: BaseException) -> OSError | None:
    """The host's refusal underneath a failure of some other type, or ``None``.

    **The four storage errnos are not the only refusals** (§7.0). A permission taken off a
    record, a sharing violation, a mount that went away: each is a read that could not be
    made, and none of them says anything about the thing being read. The engine wraps them
    in a refusal of its own, so the ``OSError`` that carries the answer is one or two links
    down the chain — asked the same way :func:`_is_storage_exception` asks, and for the same
    reason.

    An absence is not a refusal: ``ENOENT`` means the file is genuinely not there, which is
    an answer, and a caller that meets one is deciding from a fact rather than from silence.
    """
    seen, current = 0, exc
    while current is not None and seen < 8:
        if isinstance(current, OSError) and not _absent(current):
            return current
        current = current.__cause__ or current.__context__
        seen += 1
    return None


# What the supervisor says when it could not hold a line whole. Matched rather than
# imported: the two ship as separate skills, either can be installed without the other, and
# an import across that line would make each one's presence the other's requirement.
_CAPTURE_OVERFLOW = "capture overflow"

# Replay-derived allowances (§5.3 of the design). Every one of these is counted from the
# dispositions on disk; none is stored, so a resumed run reaches the same numbers.
REPLY_ALLOWANCE = 1        # re-dispatches charged by `refused` and `no-reply`
FAILURE_ALLOWANCE = 2      # re-dispatches charged by `worker-failed` and `launch-failed`
CHARGING_ATTEMPTS = 4      # charging attempts, then `error.txt`
LAUNCH_CEILING = 8         # launches of any kind, then the unit is QUARANTINED, not failed

_CHARGES_REPLY = frozenset({REFUSED, NO_REPLY})
_CHARGES_FAILURE = frozenset({WORKER_FAILED, LAUNCH_FAILED})


@dataclass(frozen=True)
class Attempt:
    unit: str
    index: int
    path: Path
    argv: dict | None
    status: dict | None
    disposition: dict | None
    resolution: dict | None
    state: str

    @property
    def name(self) -> str:
        return f"a{self.index}"


def attempt_dirs(rundir: Path, unit: str) -> list[Path]:
    """Every claimed attempt directory of one unit, in launch order.

    Enumerated by name rather than by a counter: the counter would be a second authority,
    and a crash between incrementing it and claiming the directory would hand the same
    name to two spawns.

    **An enumeration that failed is not a unit with no attempts.** Every guard downstream —
    eligibility (§3.3), the capacity count, the reclamation sweep — reads an empty list as
    "nothing is running here", so a refused `iterdir` authorizes a second live attempt and
    the deletion of the first one's input, output and working directories. The guard on the
    record cannot help: this is what finds the record.
    """
    base = rundir / engine.DISPATCH_DIR / unit
    try:
        entries = list(base.iterdir())
    except OSError as exc:
        if _absent(exc):
            return []
        raise _refused(base, exc, action="enumerated") from exc
    found = []
    for entry in entries:
        # The same rule one level in. An `a<k>` the host will not describe is not an entry
        # that is no attempt: dropped here it is dropped from eligibility, from the capacity
        # count and from the reclamation sweep, which is the hole the guarded listing above
        # exists to close, reopened on a single entry.
        if re.fullmatch(r"a\d+", entry.name) and _is_directory(entry):
            found.append(entry)
    return sorted(found, key=lambda path: int(path.name[1:]))


GRANT_PREFIX, GRANT_SUFFIX = "grant-", ".json"


def grant_files(base: Path) -> list[Path]:
    """This unit's operator grant records, by name, oldest name first.

    **Listed rather than globbed.** ``Path.glob`` swallows a directory it may not list and
    answers the empty set, which is the same answer as a unit nobody granted anything: the
    grant an operator wrote disappears and the unit stays quarantined at a ceiling they
    already raised, and the NEXT grant is written under a name this one already holds,
    replacing it. A directory that is genuinely not there holds no grants; anything else
    the host will not do stops the run.
    """
    try:
        names = os.listdir(base)
    except OSError as exc:
        if _absent(exc):
            return []
        raise _refused(base, exc, action="enumerated") from exc
    return sorted(base / name for name in names
                  if name.startswith(GRANT_PREFIX) and name.endswith(GRANT_SUFFIX))


def read_status(path: Path) -> dict | None:
    """The supervisor's decided outcome, or ``None`` where there is not one yet.

    ``status.txt`` is created empty by the redirect, so absent, empty and truncated are one
    state and the predicate is **a complete, valid status record** rather than a file
    existing. The supervisor contracts to print exactly one JSON line, last, flushed; this
    reads the last line that parses as an object carrying ``status``, so a partially
    written line is simply not a record.

    **One of the two places that tolerates an unreadable file, and here is why.** Everywhere
    else a refused read raises, because answering it as an absence authorizes something —
    a deletion, a charge, a second attempt. This authorizes nothing: no status is what an
    attempt still running looks like, so the attempt stays counted against its lane, drifts
    to `uncertain` when its window passes, and quarantines its unit for an operator. Every
    outcome of not knowing is the cautious one.
    """
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    for line in reversed(text.splitlines()):
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            obj = json.loads(line)
        except (ValueError, RecursionError):
            continue
        if isinstance(obj, dict) and isinstance(obj.get("status"), str):
            return obj
    return None


def _record_absent(path: Path) -> bool:
    """Whether ``path`` is genuinely not there, as opposed to there and unreadable.

    :func:`_read_json` answers ``None`` for both, which is right for every reader that only
    wants the record — but not for a caller about to act on the record's ABSENCE. A host
    that refused the read has not said the file is missing, and treating its refusal as an
    absence is how a deletion gets authorized by a record nobody could read.
    """
    try:
        path.lstat()
    except FileNotFoundError:
        return True
    except OSError:
        # The host would not say. Present is the answer that authorizes nothing.
        return False
    return False


def _argv_complete(raw: object) -> bool:
    """Whether ``argv.json`` is the full spawn record. Written atomically before the
    launch, so anything less means the claim was made and the launch never described —
    which is an ``orphan-claim`` and never something to adjudicate."""
    if not isinstance(raw, dict):
        return False
    required = ("argv", "adapter", "lane", "account", "generation", "permission",
                "cwd", "kind", "probe", "spawn_time", "deadline", "token", "transcript")
    return all(key in raw for key in required)


def read_attempt(rundir: Path, unit: str, path: Path, *, grace: float,
                 now: float | None = None) -> Attempt:
    """One attempt's whole state, read from disk.

    The order of the tests is the contract. A resolution is read **before** a status,
    because an operator's decision takes precedence over a status arriving after it; and
    both are read before any clock, because a clock only ever decides between *running* and
    *uncertain* and never decides an outcome.
    """
    now = _now() if now is None else now
    argv = _read_json(path / ARGV_NAME)
    status = read_status(path / STATUS_NAME)
    disposition = _read_json(path / DISPOSITION_NAME)
    resolution = _read_json(path / RESOLUTION_NAME)
    index = int(path.name[1:])
    if isinstance(disposition, dict) and disposition.get("outcome") in OUTCOMES:
        state = ADJUDICATED
    elif isinstance(resolution, dict) and resolution.get("action") in ("retry", "fail"):
        state = RESOLVED
    elif status is not None:
        state = DECIDED
    elif not _argv_complete(argv):
        state = ORPHAN_CLAIM
    else:
        window = float(argv["spawn_time"]) + float(argv["deadline"]) + grace
        state = RUNNING if now <= window else UNCERTAIN
    return Attempt(unit=unit, index=index, path=path,
                   argv=argv if isinstance(argv, dict) else None,
                   status=status,
                   disposition=disposition if isinstance(disposition, dict) else None,
                   resolution=resolution if isinstance(resolution, dict) else None,
                   state=state)


def read_attempts(rundir: Path, unit: str, *, grace: float,
                  now: float | None = None) -> list[Attempt]:
    now = _now() if now is None else now
    return [read_attempt(rundir, unit, path, grace=grace, now=now)
            for path in attempt_dirs(rundir, unit)]


# --------------------------------------------------------------------------- #
# 5.1 extraction — the closing object, or nothing
# --------------------------------------------------------------------------- #
_CLOSING_FENCE = re.compile(r"\n?[ \t]*`{3,}[ \t]*\Z")
# A transcript is bounded by the supervisor, but a pathological one must not turn into a
# quadratic scan. Both bounds are generous enough that no real reply reaches them.
_EXTRACT_TAIL_BYTES = 4 << 20
_EXTRACT_MAX_STARTS = 20000


def closing_object(transcript: str) -> str | None:
    """The JSON object that is the **last non-whitespace content** of ``transcript``.

    Returned as the exact substring, so the reply is copied byte for byte and never
    repaired. A closing code fence is allowed after it, because the worker contract asks
    for the object last and a runtime may wrap it.

    **Anything else after it means there is no closing object.** Reaching further back
    would land an earlier example as the unit's answer, which is the one failure mode worth
    more than every reply this rule costs: an example in the middle of a transcript parses
    perfectly and says something nobody claimed.
    """
    tail = transcript[-_EXTRACT_TAIL_BYTES:] if len(transcript) > _EXTRACT_TAIL_BYTES \
        else transcript
    tail = tail.rstrip()
    tail = _CLOSING_FENCE.sub("", tail).rstrip()
    # A fast path and nothing more. The rule is the decode below consuming the tail to its
    # last character, which no tail ending in anything else can satisfy; this only saves
    # scanning a long transcript that obviously cannot qualify. Removing it changes no
    # answer, which is why the rule is asserted against the decode and not against this.
    if not tail.endswith("}"):
        return None
    decoder = json.JSONDecoder()
    starts = 0
    index = tail.find("{")
    while index != -1:
        starts += 1
        if starts > _EXTRACT_MAX_STARTS:
            return None
        try:
            value, end = decoder.raw_decode(tail, index)
        except (ValueError, RecursionError):
            value, end = None, -1
        if isinstance(value, dict) and end == len(tail):
            return tail[index:end]
        index = tail.find("{", index + 1)
    return None


# --------------------------------------------------------------------------- #
# 3.4 dispositions — adjudicate once, immutably
# --------------------------------------------------------------------------- #
def _fault_text(status: dict) -> str:
    """Everything a status line says about why it failed, lower-cased for matching.

    ``terminal_detail`` is the supervisor's opt-in field carrying the terminal event's own
    error text; ``reason`` is what every caller has always received. Both are read, so this
    classifies as well as the supervisor lets it and no better.
    """
    parts = [str(status.get(key, "") or "") for key in ("reason", "terminal_detail")]
    return " ".join(parts).lower()


def _is_storage_fault(status: dict) -> bool:
    return _names_storage_fault(_fault_text(status))


def _is_capture_overflow(status: dict) -> bool:
    """Whether the supervisor could not hold what the worker produced.

    The one thing that must not happen here is a silently shorter transcript being landed as
    a unit's answer, so the supervisor refuses to shorten one and says so instead. Read as a
    worker failure this would charge a unit for the size of somebody's output; it is
    infrastructure, it charges nothing, and the unit is tried again.
    """
    return _CAPTURE_OVERFLOW in _fault_text(status)


# The supervisor's own refusals, verbatim from review_runner.py. Matched against `reason`,
# which is otherwise never matched (see `_terminal_detail`): that rule keeps a PROVIDER's
# prose from being classified through this supervisor's account of it, and these strings
# are the supervisor's account of ITSELF -- fixed text, written before any worker ran.
_SUPERVISOR_REFUSALS = ("reviewer CLI not found on PATH", "launch failed:",
                        "is a relative path and --cwd was given")


def _is_supervisor_refusal(status: dict) -> bool:
    """Whether the supervisor refused to start a worker at all, in its own words."""
    reason = status.get("reason")
    return isinstance(reason, str) and any(mark in reason for mark in _SUPERVISOR_REFUSALS)


def _is_supervisor_exit(status: dict) -> bool:
    """Whether this record was written by the driver for a supervisor that exited without
    one. The field is the driver's, never the supervisor's, so its presence is the test."""
    return "supervisor_exit" in status


def _stderr_tail(path: Path) -> str:
    """The last ``STDERR_TAIL_BYTES`` of a supervisor's stderr, or an empty string.

    Tolerant of a refused read, like :func:`read_status` and for the same reason: this
    decides nothing, it only explains, and an explanation that could not be read is an
    empty one rather than a stop.
    """
    try:
        with open(path, "rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            handle.seek(max(0, size - STDERR_TAIL_BYTES))
            return handle.read().decode("utf-8", "replace").strip()
    except OSError:
        return ""


def _terminal_detail(status: dict) -> str | None:
    """The terminal event's own error text, or ``None`` where the failure named nothing.

    Read only from the supervisor's opt-in field, never from ``reason``: ``reason`` is this
    supervisor's own account of what it saw — "reviewer exited 1" — and matching a
    provider's wording against it would classify one program's prose as another's.
    """
    detail = status.get("terminal_detail")
    if isinstance(detail, str) and detail.strip():
        return detail
    return None


def _explains_nothing(status: dict) -> bool:
    """Whether this failure explained itself nowhere.

    **Unclassifiable is not benign.** A terminal failure carrying no detail at all — the
    shape an outage takes when the CLI writes its reason to stderr — is the worker's once
    and the provider's twice, because two unexplained terminal failures in one generation
    are far likelier to be an outage than two bad answers.

    Asked only of a status that CARRIES the field. A supervisor that was never asked for the
    detail has not said the failure explained nothing; it has said nothing, and counting its
    silence would pause a provider over a flag nobody passed.

    **An event name is not explanatory text.** The supervisor's last extraction rule falls
    back to the terminal event's type, so a failure whose whole event is
    ``{"type": "turn.failed"}`` arrives here carrying the string ``turn.failed`` — which
    names the failure and explains nothing about it. Read as an explanation it makes every
    such failure classifiable, so this count never reaches two and the breaker never fires
    on the exact outage it exists for. The supervisor says which rule answered, and only a
    message is an explanation.
    """
    if "terminal_detail" not in status:
        return False
    return (_terminal_detail(status) is None
            or status.get("terminal_detail_source") == "event-type")


def _is_provider_fault(status: dict, patterns: Sequence[re.Pattern]) -> bool:
    detail = _terminal_detail(status)
    return detail is not None and any(pattern.search(detail) for pattern in patterns)


def adjudicate(ctx: "Run", attempt: Attempt) -> dict:
    """Decide one attempt's outcome and write ``disposition.json``, once, immutably.

    **Every category is read from the status record first and the transcript only after**,
    so the categories cannot overlap. A missing transcript is never evidence of a failed
    launch: the launch either produced a status record or it did not, and that question is
    settled before anything on disk beside it is opened.
    """
    record: dict = {"attempt": attempt.name, "unit": attempt.unit,
                    "at": time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime())}
    if attempt.state == RESOLVED:
        action = attempt.resolution.get("action")
        record["outcome"] = OPERATOR_RETRY if action == "retry" else OPERATOR_FAILED
        record["reason"] = str(attempt.resolution.get("reason", ""))
        record["stopped_confirmed"] = bool(attempt.resolution.get("stopped_confirmed"))
        # A status that arrives after an operator has decided is recorded and never
        # adjudicated: the operator's record is the one that stands.
        record["superseded_status"] = attempt.status is not None
    else:
        status = attempt.status or {}
        if status.get("status") != "ok":
            # **The order is the contract, and storage comes first.** A host that ran out of
            # room is never the provider's fault or the worker's — and a full disk read as a
            # provider fault would pause that account, spend the run's three probes against
            # it and drain, all while the thing to fix is the volume. Then the provider,
            # whose wording the configuration declares; and only what is left is the
            # worker's. A failure that named nothing at all is the worker's too, but it is
            # MARKED, so the replay can see that this run has twice been told nothing.
            if _is_storage_fault(status):
                record["outcome"] = INFRASTRUCTURE
                # Read back by the run's own reconciliation, which is where a storage fault
                # turns into a stop. Kept as a field of the record rather than a fact in
                # memory, so a resumed run can see which attempts met one.
                record["storage_fault"] = True
            elif _is_capture_overflow(status):
                record["outcome"] = INFRASTRUCTURE
            elif _is_supervisor_refusal(status):
                # The supervisor never launched a worker: the lane's command names a program
                # that is not on PATH, or one that could not be started. That is the
                # adapter's mistake, not the unit's, and filing it as a worker failure spent
                # every unit's launch allowance on that lane and ended the run in the one
                # refusal that cannot be resumed -- taking the other lane's finished work
                # with it, over a typo. Infrastructure, charged to nobody, and the run stops
                # where the operator can fix the adapter and run the same command again.
                record["outcome"] = INFRASTRUCTURE
                record["supervisor_refusal"] = True
                lane = attempt.lane if getattr(attempt, "lane", None) else \
                    ctx.unit_row(attempt.unit).get("lane", "?")
                ctx.request_pause(
                    f"lane {lane}'s supervisor could not start a worker for {attempt.unit}: "
                    f"{status.get('reason')}. Nothing was charged. Fix that lane's command in "
                    "the adapter file, then run the same command again")
            elif _is_supervisor_exit(status):
                # The supervisor ended without saying what happened to its worker, which
                # is nobody's answer: not the worker's, which may have finished or never
                # started, and not the provider's. Charged to nobody, and the run stops,
                # because the one way this happens in practice -- a supervisor that
                # crashes before its status line -- happens to every attempt alike,
                # and a run that carried on would spend every unit's launches on it. The
                # stderr tail is the whole account there is, so it is in the reason.
                record["outcome"] = INFRASTRUCTURE
                record["supervisor_exit"] = status.get("supervisor_exit")
                lane = attempt.lane if getattr(attempt, "lane", None) else \
                    ctx.unit_row(attempt.unit).get("lane", "?")
                tail = str(status.get("stderr_tail") or "").strip()
                ctx.request_pause(
                    f"lane {lane}'s supervisor exited {status.get('supervisor_exit')} for "
                    f"{attempt.unit} {attempt.name} without reporting a status. Nothing was "
                    f"charged. Its stderr ends: {tail or '(nothing)'}. Fix what it names, "
                    f"then run the same command again")
            elif _is_provider_fault(status, ctx.fault_patterns(attempt)):
                record["outcome"] = PROVIDER_UNAVAILABLE
            else:
                record["outcome"] = WORKER_FAILED
                if _explains_nothing(status):
                    record["unexplained"] = True
            record["reason"] = str(status.get("reason") or status.get("status") or "")
            detail = _terminal_detail(status)
            if detail is not None:
                record["terminal_detail"] = detail
        else:
            # The path this attempt RECORDED, never one rebuilt from the unit's name: the
            # reply is collected under the attempt's opaque token, which is the only place
            # the worker was ever told about.
            transcript = Path((attempt.argv or {}).get("transcript", ""))
            try:
                text = transcript.read_text(encoding="utf-8", errors="replace")
            except OSError as exc:
                record["outcome"] = INFRASTRUCTURE
                record["reason"] = f"the transcript could not be read: {exc}"
                if _is_storage_exception(exc):
                    record["storage_fault"] = True
                text = None
            if text is not None:
                found = closing_object(text)
                if found is None and status.get("capture_truncated"):
                    # **A closing object larger than the retained tail is infrastructure,
                    # never an older object.** The extractor already refuses to reach past
                    # the end of a transcript, so the reply is simply gone — and calling
                    # that `no-reply` would charge the unit's reply allowance for a bound
                    # this program set. Truncation drops from the front, so a transcript
                    # that still ENDS in its object lands normally and never reaches here.
                    record["outcome"] = INFRASTRUCTURE
                    record["reason"] = ("the reply was larger than the supervisor retained, "
                                        "so its closing object was cut; an earlier object "
                                        "is never promoted in its place")
                elif found is None:
                    record["outcome"] = NO_REPLY
                    record["reason"] = ("the transcript does not end with a JSON object, "
                                        "so nothing in it is this unit's answer")
                else:
                    _write_text(attempt.path / REPLY_NAME, found)
                    try:
                        outcome = engine.check_result(ctx.rundir, attempt.unit,
                                                      json.loads(found))
                    # `RecursionError` alongside the engine's own refusals: a reply nested
                    # thousands deep raises it rather than a ValueError, and one escaping
                    # here would end the whole run over one worker's answer.
                    except (engine.ReviewPanelError, ValueError, RecursionError,
                            OSError) as exc:
                        # **The check reads the run directory, so it can fail the way the
                        # volume fails.** `check_result` opens `units.json` and the unit's
                        # own records, and an EIO on any of them arrives here as the
                        # engine's refusal — indistinguishable in type from a reply the
                        # engine rejected. Charged as a rejection it spends the reply
                        # allowance twice and publishes `error.txt`: the disk's fault
                        # becomes the unit's answer, which is the one thing §7.1 forbids.
                        if _is_storage_exception(exc):
                            record["outcome"] = INFRASTRUCTURE
                            record["storage_fault"] = True
                            record["reason"] = (f"the reply could not be checked because a "
                                                f"read failed: {exc}")
                        else:
                            # **A read that was refused is not a reply that was rejected**
                            # (§7.0), and the volume's four errnos are not the only way a
                            # read is refused: a permission taken off `areas.json`, a
                            # Windows sharing violation on `units.json`, a mount that went
                            # away. Every one of those arrives here wearing the same type
                            # as a reply the engine would not take, and read as that it
                            # charges the reply allowance for two answers nobody ever
                            # looked at and then publishes `error.txt` as the unit's own.
                            # The refusal stops the run where it stands instead, so
                            # restoring the access and running the same command again
                            # re-checks the reply already on disk and lands it.
                            refusal = _host_refused(exc)
                            if refusal is not None:
                                raise _refused(
                                    Path(getattr(refusal, "filename", None)
                                         or ctx.rundir), refusal) from exc
                            record["outcome"] = REFUSED
                            record["reason"] = str(exc)
                    else:
                        record["outcome"] = ACCEPTED
                        record["reply_path"] = str(
                            (attempt.path / REPLY_NAME).relative_to(ctx.rundir).as_posix())
                        record["rejected"] = list(outcome.rejected)
    # What this disposition means the unit should publish, decided here rather than later,
    # from the replay of every disposition INCLUDING this one. A crash immediately after
    # this write must leave the next pass able to finish the landing without re-deciding
    # anything — which it can only do if the intention is part of the immutable record.
    others = [(att.index, att.disposition)
              for att in read_attempts(ctx.rundir, attempt.unit, grace=ctx.grace)
              if att.disposition is not None and att.index != attempt.index]
    ordered = [record for _index, record
               in sorted([*others, (attempt.index, record)], key=lambda pair: pair[0])]
    record["intended_publication"] = _intended(ordered, ctx.granted(attempt.unit))
    _write_json(attempt.path / DISPOSITION_NAME, record)
    _progress(ctx.rundir, f"{attempt.unit} {attempt.name} -> {record['outcome']}")
    if record.get("storage_fault"):
        # The half a category cannot carry on its own: `infrastructure` says this attempt
        # charged nothing and is not the unit's answer, and the run still has to stop,
        # because the next write is against the same volume.
        ctx.note_storage_fault(f"{attempt.unit} {attempt.name}: "
                               f"{record.get('terminal_detail') or record.get('reason', '')}")
    return record


# --------------------------------------------------------------------------- #
# 5.3 allowances — derived by replay, never stored
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Budget:
    launches: int
    reply_charges: int
    failure_charges: int
    charging: int
    ceiling: int

    @property
    def exhausted(self) -> bool:
        """Whether the unit has spent its allowances and its answer is ``error.txt``."""
        return (self.reply_charges > REPLY_ALLOWANCE
                or self.failure_charges > FAILURE_ALLOWANCE
                or self.charging >= CHARGING_ATTEMPTS)

    @property
    def at_ceiling(self) -> bool:
        """Whether the non-charging path has run out of room. A unit that reaches it is
        **quarantined, not failed**: repeated infrastructure faults are not its answer."""
        return self.launches >= self.ceiling


def replay(dispositions: Sequence[dict], granted: int = 0) -> Budget:
    """Count one unit's allowances from the dispositions on disk.

    ``provider-unavailable``, ``infrastructure`` and an operator-authorized retry charge
    nothing **and do not count toward the charging ceiling** — a quota failure on the
    fourth launch must not both charge nothing and exhaust the unit, which is the
    contradiction this shape removes. They still count as launches, which is what the
    separate hard stop bounds.
    """
    reply = sum(1 for d in dispositions if d.get("outcome") in _CHARGES_REPLY)
    failure = sum(1 for d in dispositions if d.get("outcome") in _CHARGES_FAILURE)
    charging = sum(1 for d in dispositions
                   if d.get("outcome") in (_CHARGES_REPLY | _CHARGES_FAILURE))
    return Budget(launches=len(dispositions), reply_charges=reply,
                  failure_charges=failure, charging=charging,
                  ceiling=LAUNCH_CEILING + granted)


def _intended(dispositions: Sequence[dict], granted: int) -> str:
    """What the unit should publish given every disposition it holds.

    ``result`` where the last one accepted, ``error`` where the unit has spent its
    allowances, ``none`` while it still has a retry coming.
    """
    if dispositions and dispositions[-1].get("outcome") == ACCEPTED:
        return "result"
    if any(d.get("outcome") == OPERATOR_FAILED for d in dispositions):
        return "error"
    budget = replay(dispositions, granted)
    # **Exhaustion first, and the order is the whole point.** A unit can reach both at once
    # — six infrastructure faults and then two replies the engine would not take — and the
    # ceiling asked first answers "quarantine" for a unit that has actually spent its
    # allowances. It then has no intention to publish and no eligibility either, and
    # granting further launches does not repair it, because the grant raises the ceiling
    # and the charging count is what is spent. Exhaustion is terminal; the ceiling is only
    # a stop on the path that charges nothing.
    if budget.exhausted:
        return "error"
    # Quarantine, not failure: repeated infrastructure faults are not the unit's answer.
    # The exit is `resolve-unit`.
    return "none"


# --------------------------------------------------------------------------- #
# 3.5 / 5.2 landing — a disposition is not a landing
# --------------------------------------------------------------------------- #
def unit_dir(rundir: Path, unit: str) -> Path:
    return rundir / engine.UNITS_DIR / unit


def landed(rundir: Path, unit: str) -> dict | None:
    """The record naming which disposition produced this unit's publication, or ``None``.

    **A unit is terminal only when its publication exists and names the disposition that
    produced it.** The publication alone is not enough: a crash between adjudicating and
    publishing must not cost the unit its answer, so a unit whose landing record is absent
    is one the next adoption pass finishes rather than one it walks past.
    """
    record = _read_json(rundir / engine.DISPATCH_DIR / unit / LANDED_NAME)
    if not isinstance(record, dict):
        return None
    which = record.get("publication")
    name = engine.RESULT_NAME if which == "result" else engine.ERROR_NAME
    if which not in ("result", "error") or not _present(unit_dir(rundir, unit) / name):
        return None
    return record


def publish(rundir: Path, unit: str, attempt: Attempt | None, intention: str,
            reason: str = "") -> None:
    """Write the unit's terminal file, then the record naming what produced it.

    Both files land temp-then-``os.replace``. The order matters and is the opposite of the
    intuitive one: the publication first, the record second, so the window a crash can land
    in is "published but unrecorded" — which the next pass finishes idempotently — rather
    than "recorded but unpublished", which would read as terminal with no answer on disk.
    """
    target = unit_dir(rundir, unit)
    target.mkdir(parents=True, exist_ok=True)
    result, error = target / engine.RESULT_NAME, target / engine.ERROR_NAME
    # Both guards ask the host with a refusal available. Read as "nothing is there", a
    # refusal lands the opposite terminal file beside the one already on disk, and the unit
    # then carries both a result and an error — the one state the protocol has no reading
    # for, and the state every later pass refuses over without being able to undo.
    if intention == "result":
        if _present(error):
            raise DriverError(
                f"{unit} already holds {engine.ERROR_NAME}, so its accepted reply cannot "
                f"be landed beside it; a unit is exactly one of complete or failed")
        source = attempt.path / REPLY_NAME
        try:
            text = source.read_text(encoding="utf-8")
        except OSError as exc:
            raise DriverError(f"cannot read {source} to land it: {exc}") from exc
        _write_text(result, text)
    else:
        if _present(result):
            raise DriverError(
                f"{unit} already holds {engine.RESULT_NAME}, so an error cannot be landed "
                f"beside it; a unit is exactly one of complete or failed")
        _write_text(error, reason or "the unit spent its allowances without an answer\n")
    _write_json(rundir / engine.DISPATCH_DIR / unit / LANDED_NAME, {
        "publication": intention,
        "attempt": attempt.name if attempt is not None else None,
        "outcome": (attempt.disposition or {}).get("outcome") if attempt is not None
                   else OPERATOR_FAILED,
        "at": time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime()),
    })
    _progress(rundir, f"{unit} landed {intention}")


def unit_resolution(rundir: Path, unit: str) -> dict | None:
    """The operator's decision about this whole unit, or ``None``.

    A record rather than an action, so a kill between deciding and publishing is a
    publication the next adoption pass finishes — the same order every other terminal
    outcome already uses, and the reason a disposition is written before a landing.
    """
    record = _read_json(rundir / engine.DISPATCH_DIR / unit / UNIT_RESOLUTION_NAME)
    if isinstance(record, dict) and record.get("action") == "fail":
        return record
    return None


def replay_publication(rundir: Path, unit: str, grace: float = GRACE_DEFAULT) -> bool:
    """Finish a landing an interruption left half-made. True once the unit is terminal.

    The whole of "a disposition is not a landing", in one place, because two callers need
    it: the adoption pass, and `resolve-unit`, which must not commit a decision over a
    publication that is already half-done.
    """
    if landed(rundir, unit) is not None:
        return True
    # The operator's decision about the unit comes first: it is the one intention that is
    # not any attempt's, and a kill between recording it and publishing the error is what
    # this replay exists to finish.
    decision = unit_resolution(rundir, unit)
    if decision is not None:
        publish(rundir, unit, None, "error",
                f"The operator failed this unit: {decision.get('reason', '')}\n")
        return True
    for attempt in read_attempts(rundir, unit, grace=grace):
        intention = (attempt.disposition or {}).get("intended_publication")
        if intention in ("result", "error"):
            reason = (_exhaustion_reason([a.disposition for a
                                          in read_attempts(rundir, unit, grace=grace)
                                          if a.disposition is not None])
                      if intention == "error" else "")
            publish(rundir, unit, attempt, intention, reason)
            return True
    return False


def _exhaustion_reason(dispositions: Sequence[dict]) -> str:
    lines = ["This unit spent its allowances without an answer the engine accepts.", ""]
    for record in dispositions:
        lines.append(f"{record.get('attempt')}: {record.get('outcome')} — "
                     f"{record.get('reason', '')}".rstrip(" —"))
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------- #
# 6. providers — one incident per generation, a pause, a probe, and no fallback
# --------------------------------------------------------------------------- #
# A provider outage looks exactly like a run of bad answers: the supervisor reduces a quota
# refusal to "the reviewer exited 1", so a driver that retries per unit spends every pending
# unit's allowance against an account that is already refusing, and four hundred units land
# as terminal failures. The report then reads as a review that found nothing wrong.
#
# **Incidents are grouped by a generation stamped at spawn, and that is the whole trick.**
# Keying an incident on the first failing attempt groups nothing, because the attempts that
# matter were launched BEFORE anyone knew of the outage and had no incident to belong to. A
# generation is recorded in every attempt's `argv.json` when it is launched, so every
# attempt in flight when an outage begins carries the same one and is the same incident by
# construction — including the ones that report minutes later.
GENERATIONS_PREFIX = "generations-"
# Two unexplained terminal failures in one generation, on one lane, pause the provider. Two
# failures that say nothing are far likelier to be an outage than two bad answers.
UNEXPLAINED_PAUSES_AT = 2
# Probes that completed and said nothing either way. After this many, the run stops rather
# than probing an account that never answers the question.
UNINFORMATIVE_PROBES_ALLOWED = 3
PROBE_BACKOFF_DEFAULT = 60.0

_PROBE_INFORMATIVE = frozenset({ACCEPTED, REFUSED, NO_REPLY})
_PROBE_UNINFORMATIVE = frozenset({WORKER_FAILED, INFRASTRUCTURE, LAUNCH_FAILED})


@dataclass(frozen=True)
class ProviderState:
    """One account's breaker, derived by replaying the dispositions on disk.

    ``breaker.json`` would be a cache and the dispositions the authority; there is no cache,
    because the only thing a cache buys here is a crash between committing a
    ``provider-unavailable`` and updating it — which resumes straight back into the outage.
    """

    account: str
    generation: int
    paused: str                 # why, or "" while the provider is working
    drain: str                  # why the run must stop, or ""
    close_generation: bool      # a probe got a real answer: this incident is over
    probe_in_flight: bool
    probe_unaccountable: bool   # a probe nobody can account for blocks any replacement
    uninformative_probes: int
    last_probe_at: float


def provider_state(account: str, generation: int,
                   attempts: Sequence[Attempt]) -> ProviderState:
    """The breaker for one account, from every attempt ever launched against it.

    Only attempts launched **at the current generation** decide anything. A failure carrying
    an older one belongs to an incident a successful probe already closed, and reading it as
    a recurrence would re-pause a provider that has since recovered — which is the whole
    reason the count of unexplained failures is keyed to the generation as well.
    """
    current = [attempt for attempt in attempts
               if _generation_of(attempt) == generation]
    probes = [attempt for attempt in current if (attempt.argv or {}).get("probe")]
    ordinary = [attempt for attempt in current if not (attempt.argv or {}).get("probe")]

    def outcome(attempt: Attempt) -> str | None:
        return (attempt.disposition or {}).get("outcome")

    informative = [a for a in probes if outcome(a) in _PROBE_INFORMATIVE]
    uninformative = [a for a in probes if outcome(a) in _PROBE_UNINFORMATIVE]
    probe_refused = [a for a in probes if outcome(a) == PROVIDER_UNAVAILABLE]
    in_flight = any(a.disposition is None and a.state in (RUNNING, DECIDED) for a in probes)
    unaccountable = any(a.state in (UNCERTAIN, ORPHAN_CLAIM) for a in probes)
    last_probe = max((float((a.argv or {}).get("spawn_time") or 0.0) for a in probes),
                     default=0.0)
    common = dict(account=account, generation=generation, probe_in_flight=in_flight,
                  probe_unaccountable=unaccountable,
                  uninformative_probes=len(uninformative), last_probe_at=last_probe)

    # **A probe that got a real answer ends the incident, and is asked first.** Earlier
    # probes of the same generation may have said nothing at all, and a count of those
    # deciding anything after the provider has answered would stop a run that had just
    # recovered.
    if informative:
        return ProviderState(paused="", drain="", close_generation=True, **common)

    unavailable = [a for a in ordinary if outcome(a) == PROVIDER_UNAVAILABLE]
    unexplained: dict[str, int] = {}
    for attempt in current:
        if (attempt.disposition or {}).get("unexplained"):
            lane = str((attempt.argv or {}).get("lane"))
            unexplained[lane] = unexplained.get(lane, 0) + 1

    drain = ""
    if probe_refused:
        # The probe is the question "has it recovered?", and this is the provider answering
        # no. Not a failure — the run exits resumable and an operator starts it again later.
        drain = ("the probe found this provider still unavailable, so there is nothing to "
                 "wait for in this run")
    elif unavailable and generation > 0:
        drain = (f"this provider failed again after it recovered once "
                 f"(generation {generation}), which is a recurrence rather than one outage")
    elif len(uninformative) >= UNINFORMATIVE_PROBES_ALLOWED:
        drain = (f"{len(uninformative)} probes completed without saying whether this "
                 f"provider works")

    paused = ""
    if unavailable:
        paused = (f"{len(unavailable)} attempt(s) launched in generation {generation} "
                  f"reported this provider unavailable")
    else:
        for lane in sorted(unexplained):
            if unexplained[lane] >= UNEXPLAINED_PAUSES_AT:
                paused = (f"{unexplained[lane]} terminal failures on lane {lane} in "
                          f"generation {generation} explained nothing")
                break
    return ProviderState(paused=paused, drain=drain, close_generation=False, **common)


def _generation_of(attempt: Attempt) -> int:
    raw = (attempt.argv or {}).get("generation")
    return raw if isinstance(raw, int) and not isinstance(raw, bool) else -1


def probe_due(state: ProviderState, now: float, backoff: float) -> bool:
    """Whether a paused provider may be probed right now.

    **One probe at a time, and an unaccountable one blocks any replacement.** A probe whose
    outcome nobody can prove may still be running, and a second one started beside it is
    exactly the thing the whole attempt protocol refuses to do anywhere else. It also does
    not count toward the three, because it never finished.
    """
    return bool(state.paused) and not state.drain and not state.probe_in_flight \
        and not state.probe_unaccountable \
        and state.uninformative_probes < UNINFORMATIVE_PROBES_ALLOWED \
        and now >= state.last_probe_at + backoff


# --------------------------------------------------------------------------- #
# 6.4 dispatch.json — configured always, executed where there is anything to say
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class LaneExecution:
    """What one lane actually did, derived from its attempts.

    ``prepared`` and ``executed`` are two different facts and the record keeps them apart: a
    spawn writes its ``argv.json`` before it launches, so a launch that raised leaves a full
    spawn record behind and nothing ever ran. Counting those as executions is how a lane
    whose supervisor never started once reads as a lane that worked.

    ``unknown`` is the third: an attempt prepared, never shown to have started, and never
    shown not to have. A kill between the spawn record and the launch leaves exactly that,
    and so does an operator failing an attempt whose supervisor never printed a status.
    §6.4 asks for **proven** execution, so what cannot be proved is counted as itself rather
    than rounded up into work the run can claim it did.
    """

    prepared: int
    executed: int
    unknown: int
    landed: int
    modes: frozenset


def executed_provenance(run: "Run") -> dict[str, LaneExecution]:
    """Per lane, what executed — counted from the attempts and from nothing else.

    **Proven execution, not a prepared launch, and a landed result, not a published
    error.** An attempt is an execution unless its own disposition says the supervisor could
    not be started; a unit landed on this lane only where what it published is a result. A
    unit whose every attempt failed publishes ``error.txt``, which names the attempt that
    produced it — read as a landing it would raise the rung, and the report would claim an
    independence no answer in it came from.
    """
    prepared = {lane: 0 for lane in run.lanes}
    executed = {lane: 0 for lane in run.lanes}
    unknown = {lane: 0 for lane in run.lanes}
    landings = {lane: 0 for lane in run.lanes}
    modes: dict[str, set] = {lane: set() for lane in run.lanes}
    for unit in run.units():
        lane = unit.get("lane")
        if lane not in prepared:
            continue
        record = landed(run.rundir, unit["id"]) or {}
        answered = record.get("attempt") if record.get("publication") == "result" else None
        for attempt in run.attempts(unit["id"]):
            if attempt.argv is None:
                continue
            prepared[lane] += 1
            disposition = attempt.disposition or {}
            if (disposition.get("launched") is False
                    or disposition.get("outcome") == LAUNCH_FAILED):
                continue
            # **What proves an execution is the supervisor's own record, or an outcome read
            # out of one.** Everything else is a launch this program asked for and cannot
            # show happened — and the difference is visible: kill a driver between the spawn
            # record and the launch, fail the attempt, and counting it as an execution has
            # the run report a supervisor that started nothing as one that ran.
            if attempt.status is None and disposition.get("outcome") not in _FROM_A_STATUS:
                unknown[lane] += 1
                continue
            executed[lane] += 1
            modes[lane].add(WRITE_CAPABLE if unit.get("kind") in WRITE_CAPABLE_KINDS
                            else READ_ONLY)
            if attempt.name == answered:
                landings[lane] += 1
    return {lane: LaneExecution(prepared=prepared[lane], executed=executed[lane],
                                unknown=unknown[lane], landed=landings[lane],
                                modes=frozenset(modes[lane]))
            for lane in run.lanes}


def _derived_rung(lanes: dict[str, LaneSpec],
                  executed: dict[str, LaneExecution] | None,
                  described: dict[str, dict]) -> str:
    """The rung, as **the lowest any landed unit ran at** — or a refusal where there is no
    rung to name.

    Configured is the ceiling and what landed is the floor: a run whose second lane never
    landed a unit did not get two of anything, whatever it was set up to do, and the report
    would otherwise claim an independence the run did not have.

    **A lane that landed nothing ends the run rather than lowering it.** Rule 3 is that a
    finding goes to a unit that did not raise it, and a lane that answered nothing checked
    nothing it was given. There is no status on the page that survives that, so the engine
    accepts no rung for it and this refuses rather than naming one. By the time a record is
    written the run has usually been stopped already — :meth:`Run.refuse_a_stranded_lane`
    asks the same question at each round boundary — and this is the backstop for the callers
    that reach here by another path.
    """
    configured = (engine.RUNG_TWO_RUNTIMES
                  if len({spec.runtime for spec in lanes.values()}) > 1
                  else engine.RUNG_ONE_RUNTIME)
    if executed is None:
        return configured
    working = [lane for lane, record in executed.items() if record.landed]
    if len(working) < 2:
        raise DriverError(no_landing_refusal(
            [lane for lane in sorted(lanes) if lane not in working], described))
    if len({lanes[lane].runtime for lane in working}) > 1:
        return engine.RUNG_TWO_RUNTIMES
    return engine.RUNG_ONE_RUNTIME


# --------------------------------------------------------------------------- #
# 7.3 the snapshot — evidence only while it is unchanged
# --------------------------------------------------------------------------- #
SNAPSHOT_DIR = "snapshot"
INVENTORY_NAME = "inventory.json"
_DIGEST_CHUNK = 1 << 20


def _digest(path: Path) -> str | None:
    """The file's SHA-256, or ``None`` where the file is no longer there. The engine
    measured the snapshot with the same algorithm over the same bytes, which is what makes
    the two comparable at all.

    **A read the host refused raises rather than answering.** A digest nobody could take is
    not a digest that differs: answered as a failed comparison it tells an operator their
    completed run is unusable and to plan a new one, over a fault that a resume would step
    straight past.
    """
    digest = hashlib.sha256()
    try:
        with open(path, "rb") as fh:
            while chunk := fh.read(_DIGEST_CHUNK):
                digest.update(chunk)
    except OSError as exc:
        if _absent(exc):
            return None
        raise _refused(path, exc) from exc
    return digest.hexdigest()


def _recorded(name: str) -> str:
    """A path spelled the way ``inventory.json`` spells it. The engine escapes a lone
    surrogate before writing a name into a record, because no UTF-8 write takes one; a
    walked name has to be escaped the same way or a file with an undecodable name reads as
    one that was added."""
    return name.encode("utf-8", "backslashreplace").decode("utf-8")


def snapshot_files(snapshot: Path) -> dict[str, Path]:
    """Every entry under the snapshot a verification has to account for, keyed by its
    recorded relative path: the regular files, **every symlink, and every junction**.

    A link is followed for neither the walk nor the digest. The snapshot is a copy of
    inventoried regular files, so a link inside it arrived afterwards — and it changes what
    the tree imports and what a build does while every measured file still hashes exactly as
    it was measured. Skipped, it is invisible to the one check whose job is to notice that.
    Kept here, it is an addition, or at a measured path a file that was replaced by one.

    **A junction is a link that reads as a directory**, so it is kept for exactly the same
    reason and by the same rule: passed over as an ordinary directory it is invisible here,
    and a measured directory replaced by one pointing at identical files outside the
    snapshot verifies clean while every reader reads a tree nobody measured.

    **Nothing here may be skipped, because this set IS the guarantee that the tree the
    readers were measured against is the tree that is there.** A walk that passed over a
    directory it could not list would report every measured file under it as deleted — the
    right direction for the wrong reason, telling an operator to throw away a run over a
    permission — while an *added* file in that same directory is reported as nothing at all,
    which is the whole defect this check exists to catch. So a directory that will not list
    and an entry that will not describe itself both stop the run where it stands, resumably,
    with the path named.
    """
    found: dict[str, Path] = {}
    for path, info in _walk_tree(snapshot):
        mode = info.st_mode
        if stat.S_ISDIR(mode) and not _is_junction(info):
            continue
        if not (stat.S_ISLNK(mode) or stat.S_ISREG(mode) or _is_junction(info)):
            # A device node, a socket or a fifo. `plan` copies regular files and nothing
            # else, so one of these arrived afterwards and IS a change to the snapshot.
            # Named as a refusal rather than recorded as a file, because the digest below
            # would open it and a reader waiting on a fifo never returns.
            raise DriverError(
                f"{path} is under the snapshot and is neither a file, a link nor a "
                f"directory, so it arrived after the tree was measured; plan a new run")
        found[_recorded(path.relative_to(snapshot).as_posix())] = path
    return found


def verify_snapshot(rundir: Path) -> list[str]:
    """What has changed under ``snapshot/`` since ``plan`` measured it, as prose lines.

    **The file set as well as the digests.** A modified file changes what a citation points
    at; an added one changes what a build does, and no digest anywhere would notice it. A
    deleted one takes a reader's subject away. All three are the same defect — the readers
    were measured against a tree that is no longer the tree — so all three are reported.
    """
    inventory = _read_json(rundir / INVENTORY_NAME)
    if not isinstance(inventory, dict) or not isinstance(inventory.get("files"), list):
        return [f"{rundir / INVENTORY_NAME} is not the engine's inventory, so the snapshot "
                f"cannot be checked against what was measured"]
    # ``context`` is what the snapshot carries for a build and nobody reads; it is checked
    # like the rest, because a rewritten lockfile changes what every later build installs.
    context = inventory.get("context", [])
    expected = {str(entry.get("path")): str(entry.get("sha256"))
                for entry in [*inventory["files"], *(context if isinstance(context, list) else [])]
                if isinstance(entry, dict)}
    present = snapshot_files(rundir / SNAPSHOT_DIR)
    problems = []
    for rel in sorted(set(expected) - set(present)):
        problems.append(f"{rel} is gone from the snapshot")
    for rel in sorted(set(present) - set(expected)):
        problems.append(f"{rel} was added to the snapshot and was never measured")
    for rel in sorted(set(expected) & set(present)):
        # Asked of the link itself and with a refusal available. A path this cannot describe
        # is not a path that is no link: answered False, a measured file replaced by a
        # symlink to an identical copy outside the snapshot digests clean and verifies.
        info = _stat_or_absent(present[rel], follow_links=False)
        if info is None:
            problems.append(f"{rel} is gone from the snapshot")
            continue
        if stat.S_ISLNK(info.st_mode) or _is_junction(info):
            # Never followed. A digest taken through the link would be the target's, so a
            # measured file replaced by a link to an identical copy elsewhere would verify
            # while every reader was reading a file outside the snapshot. A junction is
            # named as one because it is the shape an operator has to go and look at: it
            # reads as a directory everywhere else, so "symlink" would send them looking
            # for something they will not find.
            kind = "junction" if _is_junction(info) else "symlink"
            problems.append(f"{rel} was replaced by a {kind} after it was measured")
            continue
        found = _digest(present[rel])
        if found is None:
            problems.append(f"{rel} is gone from the snapshot")
        elif found != expected[rel]:
            problems.append(f"{rel} was rewritten after it was measured")
    return problems


def harden_snapshot(snapshot: Path) -> None:
    """Take write permission off every file and every directory of the snapshot.

    **What this guarantees is not the same on every platform, and the difference is stated
    rather than papered over.** On POSIX a directory without its write bit accepts no new
    entry and gives none up, so nothing can be added, removed or rewritten. On Windows
    ``os.chmod`` sets one attribute — read-only — which stops a file being rewritten or
    deleted and does **not** stop a file being created in a directory. That is exactly why
    :func:`verify_snapshot` checks the file set and not only the digests: the check is what
    holds everywhere, and the permissions are what makes the mistake hard to make.

    Files first and directories after, because a directory that has lost its write bit is
    one whose files can no longer be chmod'd on POSIX.

    A run directory hardened this way is not removable with an ordinary recursive delete on
    POSIX until write permission is restored, which is the cost of the guarantee and is paid
    once, by whoever throws the run away — :func:`remove_tree` is how this program does it.

    **Every skip here is deliberate and none of them authorizes anything**, because this
    call produces no fact: a path it could not describe or could not chmod is left writable,
    and what a reader is measured against is decided by :func:`verify_snapshot`, which reads
    the same tree with no tolerance at all. Hardening makes the mistake hard to make; the
    verification is what makes it impossible to miss.
    """
    directories: list[Path] = []
    stack = [snapshot]
    while stack:
        path = stack.pop()
        try:
            info = os.stat(path, follow_symlinks=False)
        except OSError:
            continue
        # **Neither kind of link is followed, and a junction is why the walk is written out
        # rather than handed to `rglob`.** `rglob` leaves a symlinked directory alone and
        # descends a junction, which reads as an ordinary directory to every test it makes —
        # so a junction under the snapshot would have this program take write permission off
        # files that are somebody else's, outside the run entirely.
        if stat.S_ISLNK(info.st_mode) or _is_junction(info):
            continue
        if stat.S_ISDIR(info.st_mode):
            directories.append(path)
            try:
                stack.extend(path / name for name in os.listdir(path))
            except OSError:
                pass
            continue
        with contextlib.suppress(OSError):
            os.chmod(path, info.st_mode & ~0o222)
    for path in sorted(directories, reverse=True):
        try:
            mode = os.stat(path, follow_symlinks=False).st_mode
        except OSError:
            continue
        with contextlib.suppress(OSError):
            os.chmod(path, mode & ~0o222)


# --------------------------------------------------------------------------- #
# 7.4 disk headroom — stop claiming, never fail a unit for the volume
# --------------------------------------------------------------------------- #
# A copy of the snapshot is what a write-capable unit runs in, and a build inside it grows.
# Both numbers are defaults a caller can move; neither is a measurement of any particular
# project, and a build that needs more than the margin fails inside its own copy, where it
# is the unit's answer rather than the run's.
DISK_FLOOR_DEFAULT = 256 << 20
BUILD_MARGIN_DEFAULT = 256 << 20


def tree_bytes(root: Path) -> int:
    """How much of the volume a directory holds, as the sum of its regular files' sizes.
    Links are counted as nothing rather than followed, so a link out of the tree cannot make
    the answer arbitrary. An entry the host will not describe, and a directory it will not
    list, are both passed over rather than raising: this is arithmetic for a cap and a
    reservation, the error is always an under-count, and an under-count keeps a copy the run
    would otherwise drop. Nothing here is ever read as a statement about a unit."""
    total = 0
    for path in root.rglob("*"):
        try:
            info = os.stat(path, follow_symlinks=False)
        except OSError:
            continue
        if stat.S_ISREG(info.st_mode):
            total += info.st_size
    return total


def free_bytes(path: Path) -> int | None:
    """Free space on the volume holding ``path``, or ``None`` where it cannot be asked.

    ``None`` is not zero and is never treated as a reason to stop: a host that will not
    answer the question has not said the disk is full, and refusing to claim on it would
    stop every write-capable unit of the run for a missing answer.
    """
    try:
        return shutil.disk_usage(path).free
    except (OSError, ValueError):
        return None


# --------------------------------------------------------------------------- #
# 10. worker-visible paths — opaque, input and working directory both
# --------------------------------------------------------------------------- #
# A token is 16 hex characters. Recognized by shape wherever a directory has to be judged
# without a record beside it, so nothing this program did not name can be swept up.
_TOKEN_RE = re.compile(r"[0-9a-f]{16}")


def _new_token() -> str:
    return secrets.token_hex(8)


def _spawn_refused(path: Path, exc: OSError, action: str, message: str) -> DriverError:
    """What a write that a spawn makes BEFORE its claim raises when the host refuses it.

    Two answers, the same two the working-copy preparation gives: the volume's failure is
    a resumable stop that names the path, and everything else is a refusal. Every write a
    spawn makes before claiming an attempt — the token directory, the unit's dispatch
    directory, the claim itself — is one an operator fixes by freeing space and running the
    same command again, and a full disk raised as a plain refusal exits 2, which reads as
    "do not retry", over exactly that failure. Raised bare it is a traceback, which reads as
    a defect in this program.
    """
    if _is_storage_exception(exc):
        return StorageFault(path, exc, action=action)
    return DriverError(message)


def reserve_token(rundir: Path) -> str:
    """A token no other attempt can be given, reserved by creating its input directory.

    Sixteen hex characters from ``secrets`` collide with a probability nobody would meet,
    and "nobody would meet it" is not the same claim as "it cannot happen" — two attempts
    sharing a token share a working directory, and one would then be answering out of the
    other's tree. The exclusive create costs one syscall and turns the improbable into the
    impossible, which is cheaper than the sentence explaining why the risk was accepted.
    """
    base = rundir / "in"
    try:
        base.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise _spawn_refused(base, exc, "created", f"cannot create {base}: {exc}") from exc
    for _ in range(64):
        token = _new_token()
        try:
            (base / token).mkdir()
        except FileExistsError:
            continue
        except OSError as exc:
            raise _spawn_refused(
                base / token, exc, "reserved",
                f"cannot reserve a worker directory in {base}: {exc}") from exc
        return token
    raise DriverError(  # pragma: no cover - 64 collisions in a row is not reachable
        f"cannot find an unused worker token under {base}")


def _make_writable(root: Path) -> None:
    """Restore user write on a copied tree, files and directories both.

    The snapshot a copy comes from is read-only, and a reproduction has to be able to
    rewrite a file. Executable bits are preserved: a copy whose scripts stopped being
    executable would fail a build for a reason that has nothing to do with the code.

    **A restoration that did not happen fails the preparation** (§5.4). Suppressed, this
    returns success over a copy that is still read-only: the worker then cannot write its
    own fixtures, and that failure is charged to it as its behavior — the unit answers for
    something this program failed to do. Only a path that vanished under the walk is passed
    over, because there is nothing there left to restore.

    The walk is the same rule: a directory that will not list leaves its whole subtree
    read-only, and skipping it would return success over exactly the copy this is for.
    """
    try:
        top = os.stat(root, follow_symlinks=False)
    except OSError as exc:
        raise _refused(root, exc, action="examined") from exc
    for path, info in [(root, top), *_walk_tree(root)]:
        # A link of either kind is left exactly as it is: a chmod through one lands on
        # whatever it points at, which is not this copy and not this program's to change.
        if stat.S_ISLNK(info.st_mode) or _is_junction(info):
            continue
        try:
            os.chmod(path, info.st_mode | stat.S_IWUSR)
        except OSError as exc:
            if _absent(exc):
                continue
            raise _refused(path, exc, action="made writable") from exc


def prepare_worker_paths(rundir: Path, unit: dict,
                         token: str) -> tuple[Path, Path, Path]:
    """The opaque input directory and working directory for one attempt.

    **Every path a worker is given is opaque, not just its payload.** A verification unit
    is named for the area and the lane that *raised* its candidates, so handing a worker
    ``dispatch/verify-area-01-A/a0/copy`` as its working directory tells it exactly what the
    payload is built to withhold. A token names both, and the token-to-unit mapping stays
    on this side of the line.

    Read-only units keep the snapshot as their working directory, which names nothing.

    **The place the reply is collected is opaque as well**, and that is not a detail: an
    adapter whose runtime writes its own answer file has to be told where to put it, and
    the attempt directory is exactly the name this rule exists to withhold.
    """
    source = rundir / engine.UNITS_DIR / unit["id"]
    inbox = rundir / "in" / token
    inbox.mkdir(parents=True, exist_ok=True)
    outbox = rundir / "out" / token
    outbox.mkdir(parents=True, exist_ok=True)
    for name in (engine.PAYLOAD_NAME, engine.SCHEMA_NAME):
        try:
            shutil.copyfile(source / name, inbox / name)
        except OSError as exc:
            # **A required input that did not arrive stops the preparation.** Both files are
            # the worker's whole task — the brief and the shape of the answer — and a copy
            # that failed leaves the inbox empty or half written while every small record
            # around it succeeds. The worker then launches with no instructions, and the
            # missing or malformed answer it gives spends the unit's allowances: the unit is
            # charged for a file this program failed to put there. A storage fault pauses
            # the run instead of refusing it, because the next write is against the same
            # volume and there is nothing here for an operator to resolve.
            if _is_storage_exception(exc):
                raise StorageFault(inbox / name, exc) from exc
            raise
    if unit.get("kind") in WRITE_CAPABLE_KINDS:
        work = rundir / "work" / token
        # Read as "no copy yet", a refusal here would copy a whole snapshot over a copy a
        # worker may already be running in, so the question is asked with a refusal
        # available and an unexaminable path pauses the run instead.
        if not _present(work):
            work.parent.mkdir(parents=True, exist_ok=True)
            # The copy comes from the hardened snapshot (§7.3) and `copytree` carries those
            # modes over, so between these two statements the tree is read-only. A process
            # that dies in that window leaves one behind, which is why every sweep that
            # removes a worker's copy goes through `remove_tree` and never a plain
            # `ignore_errors` delete: the latter cannot take a read-only tree away on any
            # platform and gives up silently, so the copies accumulate for the life of the
            # volume and the retention cap bounds nothing.
            shutil.copytree(rundir / "snapshot", work, symlinks=True)
            _make_writable(work)
        return inbox, outbox, work
    return inbox, outbox, rundir / "snapshot"


def _index(rundir: Path, token: str, unit: str, attempt: str) -> None:
    """Record which unit a token belongs to. Driver-private, and the only thing that can
    map a worker's opaque directory back to the unit it answered."""
    path = rundir / engine.DISPATCH_DIR / "index.json"
    current = _read_json(path)
    table = current if isinstance(current, dict) else {}
    table[token] = {"unit": unit, "attempt": attempt}
    _write_json(path, table)


def _release_worker_paths(rundir: Path, attempt: Attempt, *, landed: bool = False) -> None:
    """Remove one attempt's worker-visible directories.

    **Deletion needs two facts, not one: the unit is terminal, and that attempt's execution
    has ended** — a complete status record, or an operator's attestation. The supervisor
    deliberately detaches its worker, so a copy removed on terminality alone is a working
    directory taken out from under a process that is still running in it. A copy this
    cannot prove is finished with is kept, to the end of the run, and named in ``status``.
    """
    argv = attempt.argv or {}
    token = argv.get("token")
    if not isinstance(token, str) or not token:
        return
    # `out/<token>/` is deliberately NOT here. It holds the worker's own narrative, which
    # is what a human reads when a unit failed, and it is a text file rather than a tree.
    #
    # Both removals are best effort and neither produces a fact: a tree the host would not
    # give up stays on disk, costs the volume its bytes, and is named by the reclamation
    # sweep on a later pass. What may never be tolerated is the DECISION above — whether
    # this copy is evidence — and that one is read with a refusal available.
    remove_tree(rundir / "in" / token, rundir)
    # Only the attempt that LANDED the unit's answer, which is what the retention rule is
    # about: a refused reply is not a verdict this run holds, and keeping every refused
    # attempt's copy would keep a whole snapshot per attempt for evidence nothing cites.
    if landed and keeps_evidence(attempt):
        # **A copy whose landed verdict names a run that was actually performed is kept
        # whole**, because the copy IS the archive: a verdict says `bash reproduce.sh` and
        # does not name the fixture that script reads, so there is no partial archive to
        # keep instead. What bounds it is the retention cap, which drops copies
        # highest-severity-last and names every one it drops — never this path, which would
        # be deleting evidence the moment its unit finished.
        return
    remove_tree(rundir / "work" / token, rundir)


def keeps_evidence(attempt: Attempt) -> bool:
    """Whether this attempt's landed reply names a run that was actually executed.

    Read from the reply the attempt landed, because that is where the claim is: a verdict's
    evidence declares `run_kind`, and a capability probe's build and test attempts declare
    an exit status. **A reply that is not JSON keeps its copy**, and that asymmetry is
    deliberate: the cost of keeping a copy nobody needed is disk, and the cost of the other
    mistake is a reproduction nobody can run again. A reply the host refused to read is not
    that case and never reaches here — it stops the run instead, because deciding a deletion
    from it would be exactly the mistake this asymmetry is guarding against.
    """
    reply = attempt.path / REPLY_NAME
    # Asked with a refusal available, which is what makes the sentence above true. A reply
    # the host will not describe, answered "not there", returns False without the read below
    # ever happening — and False here DELETES the working copy the verdict reproduces from.
    if not _present(reply):
        return False
    obj = _read_json(reply)
    if obj is None:
        return True
    if not isinstance(obj, dict):
        return False
    for verdict in obj.get("verdicts", []) if isinstance(obj.get("verdicts"), list) else []:
        evidence = verdict.get("evidence") if isinstance(verdict, dict) else None
        if isinstance(evidence, dict) and evidence.get(engine.RUN_KIND_KEY) == "executed":
            return True
    for name in engine.PROBE_ATTEMPTS:
        ran = obj.get(name)
        if isinstance(ran, dict) and ran.get("exit_status") is not None:
            return True
    return False


# Windows' CREATE_NEW_PROCESS_GROUP. Spelled as the number because the name exists only on
# Windows Pythons, and this module is imported on every platform.
_NEW_PROCESS_GROUP = 0x00000200


def _own_process_group() -> dict:
    """The Popen keywords that put a supervisor in a process group of its own.

    The point is the same on both platforms and the spelling differs. `start_new_session`
    is POSIX-only, and on Windows the supervisor otherwise shares the console's group -- so a
    Ctrl-C meant as a drain reached every supervisor, killed every in-flight worker, and
    charged each a worker failure, against the promise that what is running finishes. The
    stop request this program honors is the drain FILE, which works everywhere; a console
    signal is never how a worker is told anything.
    """
    if os.name == "posix":
        return {"start_new_session": True}
    if os.name == "nt":
        return {"creationflags": _NEW_PROCESS_GROUP}
    return {}


def _execution_ended(attempt: Attempt) -> bool:
    """Whether this attempt's worker can no longer be running.

    **One predicate, used by everything that asks the question** — the capacity reservation,
    the write-capable serialization and the copy cleanup — because two spellings of it
    disagreed: a lane stayed reserved for an operator-failed attempt that a late status had
    since proved finished, while the count of running writers ignored that same attempt and
    let a second write-capable worker start beside one that might still be live.

    A complete status record is the design's own definition of an execution that ended, and
    an operator's attestation is the other. A launch that raised never started anything.
    """
    if attempt.status is not None:
        return True
    for record in (attempt.resolution or {}, attempt.disposition or {}):
        if record.get("stopped_confirmed"):
            return True
    disposition = attempt.disposition or {}
    # `launched: False` is what the spawn writes when the launch itself raised, whatever
    # that raising is adjudicated as; the outcome covers a record written before that field
    # existed.
    if disposition.get("launched") is False or disposition.get("outcome") == LAUNCH_FAILED:
        return True
    return False


def _reserves_capacity(attempt: Attempt) -> bool:
    """Whether this attempt still holds its slot. The same question as :func:`_execution_ended`,
    asked the other way round, so the two can never answer differently."""
    return not _execution_ended(attempt)


# --------------------------------------------------------------------------- #
# 5.4 copies — the copy IS the archive, and a protected one is not ours to remove
# --------------------------------------------------------------------------- #
# What the run keeps of the copies whose verdicts name a run that was performed. Zero means
# unbounded. A copy is a whole snapshot, so a long run with many reproductions would
# otherwise keep one tree per landed verdict for as long as the run directory exists.
KEEP_REPRO_BYTES_DEFAULT = 4 << 30
# Where the run says what it kept, what it could not remove, and what the cap cost. The
# dropped list is appended to and never rewritten: it is the record of a deletion, and a
# reader asking why a reproduction is missing has nowhere else to look.
COPIES_RECORD_NAME = "copies.json"
# A copy the run cannot rank — a capability probe's, or a verification unit whose
# candidates the route record no longer names. It sorts below every severity, so the cap
# takes it first: what the rule protects is the evidence a defect cites, and an unranked
# copy is cited by no defect entry.
UNRANKED = len(engine.SEVERITIES)


@dataclass(frozen=True)
class Copy:
    """One attempt's working directory, as the retention rule sees it."""

    unit: str
    attempt: str
    token: str
    path: Path
    bytes: int
    rank: int
    protected: bool

    @property
    def severity(self) -> str:
        return engine.SEVERITIES[self.rank] if self.rank < UNRANKED else "unranked"

    def named(self) -> dict:
        return {"unit": self.unit, "attempt": self.attempt,
                "token": self.token, "bytes": self.bytes, "severity": self.severity}


def _most_severe_rank(levels: Sequence[str]) -> int:
    ranked = [engine.SEVERITIES.index(level) for level in levels
              if level in engine.SEVERITIES]
    return min(ranked) if ranked else UNRANKED


# --------------------------------------------------------------------------- #
# the run — everything above, wired to one owned run directory
# --------------------------------------------------------------------------- #
class Run:
    """One owned run directory, its configuration, and the loop over its rounds."""

    def __init__(self, rundir: Path, lanes: dict[str, LaneSpec], supervisor: Path, *,
                 grace: float = GRACE_DEFAULT, poll: float = 2.0,
                 max_hours: float | None = None, out=None, given: Path | None = None,
                 probe_backoff: float = PROBE_BACKOFF_DEFAULT,
                 max_capture_bytes: int = MAX_CAPTURE_BYTES_DEFAULT,
                 keep_repro_bytes: int = KEEP_REPRO_BYTES_DEFAULT,
                 disk_floor: int = DISK_FLOOR_DEFAULT,
                 build_margin: int = BUILD_MARGIN_DEFAULT):
        self.rundir = rundir
        # The spelling the operator typed, when it is not the canonical one. Carried only so
        # the drain request works at the path the documentation names for them.
        self.given = given
        self.lanes = lanes
        self.supervisor = supervisor
        self.grace = grace
        self.poll = poll
        self.max_hours = max_hours
        self.out = out if out is not None else sys.stdout
        self.probe_backoff = probe_backoff
        self.max_capture_bytes = max_capture_bytes
        self.keep_repro_bytes = max(0, keep_repro_bytes)
        self.disk_floor = max(0, disk_floor)
        self.build_margin = max(0, build_margin)
        self.draining = False
        # Why, when it was this program's own decision rather than an operator's. A run that
        # stopped because a provider is down and one that was asked to stop are the same
        # exit and the same resumability, and an operator who cannot tell them apart will
        # start the second one straight back into the outage.
        self.drain_reason = ""
        # Why the run stopped over something that is not a unit's fault. Separate from the
        # drain: both stop claiming, and only this one exits non-zero, because a caller
        # scripting this has to tell "asked to stop" from "cannot be trusted to continue".
        self.pause_reason = ""
        self._storage_faults: list[str] = []
        self._blocked_on_disk = ""
        # Once per round: the cap is enforced the first time a round finds itself short
        # of room, not on every poll, because the sweep measures every copy.
        self._swept_for_room = False
        self._paused_said: set[str] = set()
        self._unreadable_said: set[str] = set()
        # Each supervisor this process started, with the attempt it was started for: the
        # exit of a child is a fact only its parent can read, and it is read in `reap`.
        self._children: list[tuple[subprocess.Popen, Path, str]] = []
        self._units_cache: list[dict] | None = None
        # **A per-pass cache, and the reason is scale.** Eligibility, the capacity count,
        # the quarantine test and the drain test each walk a unit's attempts, so an
        # uncached pass re-reads every attempt of every unit five times per poll — at four
        # hundred units that is millions of reads a minute and the loop stops keeping up
        # with the workers it is supposed to be starting. Cleared at the top of every
        # adoption pass and invalidated per unit by every write, so nothing here is ever
        # read from memory across a change.
        self._attempt_cache: dict[str, list[Attempt]] = {}
        self._landed_cache: dict[str, dict | None] = {}
        self._provider_cache: dict[str, ProviderState] | None = None
        # The snapshot is read-only from the moment the run directory commits, so its size
        # is measured once; the candidate severities come from the route record, which is
        # written once and never edited.
        self._snapshot_bytes: int | None = None
        self._candidate_severities: dict[str, int] | None = None
        self._budget_started = time.monotonic()
        self._budget_base, self._extended = self._read_budget()
        self._budget_written = time.monotonic()

    # -- small accessors ---------------------------------------------------- #
    def say(self, line: str) -> None:
        self.out.write(line + "\n")
        with contextlib.suppress(Exception):
            self.out.flush()

    def units(self) -> list[dict]:
        if self._units_cache is None:
            self._units_cache = _units(self.rundir)
        return self._units_cache

    def unit_row(self, unit_id: str) -> dict:
        for unit in (self._units_cache or self.units()):
            if unit.get("id") == unit_id:
                return unit
        raise DriverError(f"{self.rundir / engine.UNITS_FILE_NAME} lists no unit {unit_id!r}")

    def attempts(self, unit_id: str) -> list[Attempt]:
        cached = self._attempt_cache.get(unit_id)
        if cached is None:
            cached = read_attempts(self.rundir, unit_id, grace=self.grace)
            self._attempt_cache[unit_id] = cached
        return cached

    def terminal(self, unit_id: str) -> bool:
        if unit_id not in self._landed_cache:
            self._landed_cache[unit_id] = landed(self.rundir, unit_id)
        return self._landed_cache[unit_id] is not None

    def invalidate(self, unit_id: str | None = None) -> None:
        """Forget what was read. Called by every write, so the next question is answered
        from disk — the state is on disk and this cache is only ever a pass's worth."""
        # Always, whichever unit changed: the breaker is derived from EVERY unit's attempts,
        # so one unit's new disposition is what pauses a provider for all of them.
        self._provider_cache = None
        if unit_id is None:
            self._attempt_cache.clear()
            self._landed_cache.clear()
            self._units_cache = None
        else:
            self._attempt_cache.pop(unit_id, None)
            self._landed_cache.pop(unit_id, None)

    def granted(self, unit_id: str) -> int:
        """Extra launches an operator granted this unit, summed from durable records.

        Each grant is its own file, so the records are appended to and never edited, and a
        replay reaches the same ceiling every time.
        """
        total = 0
        for path in grant_files(self.rundir / engine.DISPATCH_DIR / unit_id):
            record = _read_json(path)
            if isinstance(record, dict) and isinstance(record.get("launches"), int):
                total += max(0, record["launches"])
        return total

    # -- the time budget ---------------------------------------------------- #
    def _read_budget(self) -> tuple[float, float]:
        """``(spent, extended)`` from ``budget.json``. Two numbers, not one: an extension is
        credit granted, and subtracting it from the spend cannot represent more credit than
        has been spent — one hour spent and two hours granted bought one hour."""
        record = _read_json(self.rundir / "budget.json")
        if not isinstance(record, dict):
            return 0.0, 0.0
        def _number(key: str) -> float:
            value = record.get(key)
            return float(value) if isinstance(value, (int, float)) else 0.0
        return _number("seconds"), _number("extended")

    def spent(self) -> float:
        """Accumulated **driver uptime**, across restarts. Downtime is not counted: a run
        left alone overnight has not spent its budget, and each process measures its own
        share with a monotonic clock so a system clock change cannot move it."""
        return self._budget_base + (time.monotonic() - self._budget_started)

    def save_budget(self, force: bool = False) -> None:
        if not force and time.monotonic() - self._budget_written < 60.0:
            return
        self._budget_written = time.monotonic()
        with contextlib.suppress(engine.ReviewPanelError, OSError):
            _write_json(self.rundir / "budget.json",
                        {"seconds": round(self.spent(), 3),
                         "extended": round(self._extended, 3)})

    def extend(self, hours: float) -> None:
        """Let this run use ``hours`` beyond ``--max-hours``, as an absolute figure.

        The grant is kept as its own number and persisted, so every hour asked for is an
        hour given — including on a run that has barely spent anything, where taking the
        grant off the spend would throw most of it away.

        **Set, never added.** The run's own advice on a spent budget is to run the same
        command again, and an operator who does that has `--extend` in the command line
        every time — on every restart after a crash, a pause or a drain, not only the one
        that asked for more time. Added on each invocation, the same flag granted a fresh
        extension per restart, and a run restarted enough times had no limit at all. Asking
        for more is a larger number.
        """
        self._extended = max(0.0, hours) * 3600.0
        self.save_budget(force=True)

    def over_budget(self) -> bool:
        return (self.max_hours is not None
                and self.spent() >= self.max_hours * 3600.0 + self._extended)

    # -- the drain request --------------------------------------------------- #
    @property
    def drain_flag(self) -> Path:
        """The canonical request path — the one `status` and startup print."""
        return drain_flags(self.rundir)[0]

    def stop_requested(self) -> bool:
        """Whether this run has been asked to stop claiming.

        Two ways in, and the file is the one that works everywhere: a `SIGTERM` handler
        where signals are delivered, and `<rundir>.drain` on every platform including the
        one where they are not. Latched once read, so the answer cannot change under a pass
        and the reason is announced once.

        **A flag the host will not describe stops the run rather than being passed over.**
        Read as "no stop was requested", it claims and launches more attempts after an
        operator asked it not to — and the operator, who has the run's only control and used
        it exactly as documented, has no way to tell that from a driver that simply has not
        looked yet. Stopping is also what they asked for.
        """
        if self.draining:
            return True
        found = None
        for path in drain_flags(self.rundir, self.given):
            if _present(path):
                found = path
                break
        if found is not None:
            self.draining = True
            self.say(f"draining: {found} is present, so no new attempts will be claimed")
            _progress(self.rundir, "drain requested by file")
        return self.draining

    # -- 3.3 eligibility ---------------------------------------------------- #
    def quarantined(self, unit_id: str) -> str | None:
        """Why this unit cannot be worked on, or ``None``.

        Derived, like everything else: an attempt whose outcome nobody can prove, or a
        launch ceiling reached. Both have exactly one exit, and it is an operator command
        that writes a durable record.
        """
        if self.terminal(unit_id) or unit_resolution(self.rundir, unit_id) is not None:
            return None
        for attempt in self.attempts(unit_id):
            if attempt.state in (UNCERTAIN, ORPHAN_CLAIM):
                return (f"{attempt.name} is {attempt.state}: its outcome cannot be proven, "
                        f"so it is never re-spawned and never landed")
        budget = replay([a.disposition for a in self.attempts(unit_id)
                         if a.disposition is not None], self.granted(unit_id))
        # Asked in the same order `_intended` asks it: a unit that has spent its allowances
        # is not quarantined, it is a unit whose answer is the error the next adoption pass
        # publishes. Naming it quarantined would send an operator to `resolve-unit` over a
        # unit that needs nothing.
        if budget.exhausted:
            return None
        if budget.at_ceiling:
            return (f"{budget.launches} launches with no answer, which is the hard stop; "
                    f"repeated infrastructure faults are not this unit's answer")
        return None

    def eligible(self, unit_id: str) -> bool:
        if self.terminal(unit_id) or self.quarantined(unit_id) is not None:
            return False
        if unit_resolution(self.rundir, unit_id) is not None:
            # Decided against, and the publication is a pass away. Authorizing another
            # launch here would spend a launch on a unit whose answer is already written.
            return False
        attempts = self.attempts(unit_id)
        for attempt in attempts:
            # A unit with anything still in flight, or decided and not yet adjudicated, has
            # an active attempt. Spare capacity is NOT authorization: a retry is authorized
            # by the previous attempt's disposition and by nothing else.
            if attempt.state in (RUNNING, UNCERTAIN, ORPHAN_CLAIM, DECIDED, RESOLVED):
                return False
        dispositions = [a.disposition for a in attempts if a.disposition is not None]
        if dispositions and dispositions[-1].get("outcome") == ACCEPTED:
            return False
        budget = replay(dispositions, self.granted(unit_id))
        return not budget.exhausted and not budget.at_ceiling

    def reserving(self) -> dict[str, int]:
        """How many of each lane's slots are spoken for.

        ``uncertain`` and ``orphan-claim`` **keep their reservation** — the worker may
        still be alive — and so does an operator-failed attempt with no attestation. A
        reservation released on a guess is a second worker started beside a live one.
        """
        counts = {lane: 0 for lane in engine.LANES}
        for unit in self.units():
            lane = unit.get("lane", engine.LANES[0])
            for attempt in self.attempts(unit["id"]):
                if _reserves_capacity(attempt):
                    counts[lane] = counts.get(lane, 0) + 1
        return counts

    def writers_running(self) -> int:
        """Write-capable attempts in flight, run-wide.

        Serialized across both lanes, not per lane: separate directories are not separate
        ports, caches or credentials, so two reproductions at once can fail each other for
        environmental reasons and the verdict would read as the code's fault.
        """
        total = 0
        for unit in self.units():
            if unit.get("kind") not in WRITE_CAPABLE_KINDS:
                continue
            for attempt in self.attempts(unit["id"]):
                # The SAME predicate the lane reservation uses. Two spellings of "may still
                # be running" let an unattested operator-failed writer hold a slot while
                # this count ignored it, so a second reproduction started beside a worker
                # that may still have had the ports, caches and credentials they share.
                if _reserves_capacity(attempt):
                    total += 1
        return total

    # -- 3.1 spawn ---------------------------------------------------------- #
    def spawn(self, unit: dict, probe: bool = False) -> bool:
        """Claim an attempt for ``unit`` and launch it. False when nothing was claimed.

        The caller has to be able to tell an aborted spawn from a claimed one, because a
        spawn that stops on a drain request is not work in flight and not work refused —
        it is work not started.

        ``probe`` marks this attempt as the one question asked of a paused provider. It is
        an ordinary attempt in every other respect — a probe that answers lands its unit —
        and it is recorded at spawn, because what a probe is asking about is the generation
        it was launched into.
        """
        unit_id = unit["id"]
        named = unit.get("lane")
        if named not in self.lanes:
            raise DriverError(
                f"{unit_id} is listed against lane {named!r}, which the adapter config "
                f"does not describe; the listing and the configuration disagree")
        lane = self.lanes[named]
        mode = WRITE_CAPABLE if unit.get("kind") in WRITE_CAPABLE_KINDS else READ_ONLY
        spec = lane.modes[mode]
        base = self.rundir / engine.DISPATCH_DIR / unit_id
        try:
            base.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise _spawn_refused(
                base, exc, "created",
                f"cannot create the dispatch directory for {unit_id}: {exc}") from exc
        # **Everything slow happens BEFORE the claim.** A write-capable unit's working
        # directory is a copy of the whole snapshot, and preparing it after the claim puts
        # a directory copy inside the window between claiming an attempt and describing it
        # — so a kill during that copy leaves a claim nobody can describe, which quarantines
        # the unit and stops the run for an operator. It is not a narrow window: it is as
        # long as the copy. Done first, a kill here leaves an `in/` and `work/` directory
        # no attempt names, which is disk rather than a stopped run, and the claim and its
        # record become two adjacent writes, which is the order §3.1 asks for.
        token = reserve_token(self.rundir)
        try:
            inbox, outbox, cwd = prepare_worker_paths(self.rundir, unit, token)
        except OSError as exc:
            # The volume's failure is resumable -- free space, run the same command again --
            # and exits as a stop, not a refusal, so the operator is told which it is.
            # Anything else is refused by name with nothing claimed, so the run stops where
            # an operator can see why rather than in a traceback with a half-made copy
            # behind it.
            raise _spawn_refused(
                self.rundir, exc, "copied",
                f"cannot prepare a working directory for {unit_id}: {exc}") from exc
        transcript = outbox / TRANSCRIPT_NAME
        # **Asked AGAIN, here, with the preparation done and the claim one line away.**
        # Moving the copy before the claim closed one window and opened another: the copy
        # can take minutes, so a signal or an expiring budget arriving inside it would be
        # answered by claiming and launching a worker that then runs for its whole deadline
        # — after this program had said nothing more would start. Nothing is claimed yet, so
        # aborting here leaves only the prepared directories, which are removed below.
        if self.stop_requested() or self.over_budget():
            # Tidying, and it decides nothing: no attempt was claimed, so a directory that
            # would not come away is a leak the reclamation sweep names on a later pass and
            # never a record anything reads.
            for path in (inbox, outbox, self.rundir / "work" / token):
                remove_tree(path, self.rundir)
            _progress(self.rundir, f"{unit_id} not claimed: the run was asked to stop "
                                   f"while its working directory was being prepared")
            return False
        # The claim: exclusive `mkdir`, which is the one thing two processes cannot both
        # do. The index walks up from the highest existing name rather than a counter,
        # because a counter is a second authority and a crash between incrementing it and
        # claiming would hand one name to two spawns.
        index = len(attempt_dirs(self.rundir, unit_id))
        attempt_path = None
        while attempt_path is None:
            candidate = base / f"a{index}"
            try:
                candidate.mkdir()
            except FileExistsError:
                index += 1
                continue
            except OSError as exc:
                raise _spawn_refused(candidate, exc, "claimed",
                                     f"cannot claim {candidate}: {exc}") from exc
            attempt_path = candidate
        values = {
            "input": str(inbox),
            "payload": str(inbox / engine.PAYLOAD_NAME),
            "schema": str(inbox / engine.SCHEMA_NAME),
            "cwd": str(cwd),
            "transcript": str(transcript),
            "prompt": (f"Read {inbox / engine.PAYLOAD_NAME} in full and do exactly what "
                       f"it says."),
        }
        rendered = [_PLACEHOLDER_RE.sub(lambda m: values[m.group(1)], part)
                    for part in spec.command]
        argv = [sys.executable, str(self.supervisor),
                "--idle", str(spec.idle), "--deadline", str(spec.deadline),
                "--cwd", str(cwd), "--display", str(attempt_path / DISPLAY_NAME),
                "--findings", str(transcript), "--result-mode", spec.result_mode,
                # **Both opt-in flags, on every spawn, and neither is optional here.**
                # Without the detail a quota refusal reaches this program as "the reviewer
                # exited 1" and every pending unit spends its allowance against an account
                # that is already refusing. Without the cap one worker's runaway output is
                # held whole in the supervisor's memory, four hundred times over.
                "--status-detail",
                "--max-capture-bytes", str(self.max_capture_bytes),
                "--", *rendered]
        record = {
            "argv": argv, "adapter": lane.adapter, "lane": unit.get("lane"),
            "account": lane.account, "generation": self.generation(lane.account),
            "permission": spec.permission, "cwd": str(cwd), "kind": unit.get("kind"),
            # The PROVIDER probe of the breaker, which is a different thing from the
            # engine's capability-probe unit kind. Recorded at spawn because an incident is
            # grouped by what an attempt was launched into, never by what was known when it
            # reported.
            "probe": bool(probe),
            "spawn_time": _now(), "deadline": spec.deadline, "token": token,
            "transcript": str(transcript),
            "unit": unit_id, "attempt": attempt_path.name,
        }
        # **Atomically, and BEFORE the launch.** A claim with no complete spawn record is
        # an `orphan-claim` — an attempt nobody can describe — which quarantines its unit
        # rather than being quietly reused.
        _write_json(attempt_path / ARGV_NAME, record)
        _index(self.rundir, token, unit_id, attempt_path.name)
        _progress(self.rundir, f"{unit_id} {attempt_path.name} spawning "
                               f"(lane {unit.get('lane')}, {mode}"
                               f"{', provider probe' if probe else ''})")
        try:
            # stdout is redirected into the attempt's own `status.txt`, inherited by the
            # child. The supervisor prints its one status line last and flushes it, so the
            # outcome record lands even when this driver is gone by the time it does.
            # stderr goes to a file of its own beside it: a supervisor that exits before
            # that line has said why on stderr and nowhere else, and discarded, its death
            # read as a worker still running until its deadline and grace had passed.
            handle = open(attempt_path / STATUS_NAME, "wb")
            try:
                errors = open(attempt_path / STDERR_NAME, "wb")
            except OSError:
                handle.close()
                raise
        except OSError as exc:
            # **The same outcome as a failed spawn, because that is what this is.** The
            # attempt directory and a complete `argv.json` are already on disk when this
            # runs, and nothing has been started. Raised as a plain refusal it left no
            # record that nothing launched, so the next pass read the attempt as running,
            # then `uncertain`, and asked an operator to reconcile a worker that never
            # existed.
            return self._record_no_launch(unit_id, attempt_path, index, exc)
        try:
            with handle, errors:
                child = subprocess.Popen(
                    argv, stdout=handle, stderr=errors,
                    stdin=subprocess.DEVNULL, cwd=str(self.rundir),
                    **_own_process_group())
        except (OSError, ValueError) as exc:
            return self._record_no_launch(unit_id, attempt_path, index, exc)
        self._children.append((child, attempt_path, unit_id))
        self.invalidate(unit_id)
        return True

    def reap_children(self) -> None:
        """Forget the supervisors that have exited, and record the ones that said nothing.

        **An exit with no status is an ended execution, and it is written down here
        because nothing else can see it.** The attempt's state is read from disk, where a
        supervisor that died before printing its status line is indistinguishable from one
        still working: it stays `running` until its deadline and grace pass, an hour at the
        defaults, and every attempt after it on that lane waits its turn behind a process
        that is not there. A supervisor that crashes before printing it -- one run under a
        Python too old for it, for instance -- does that to every attempt.

        Only the process that spawned the supervisor can know it exited, so this is the
        one place the driver writes a status record, and only over an empty one: the
        child has exited, so nothing else will write there, and a record that is already
        complete stands. A resumed driver never spawned the child and cannot tell; for it
        the deadline is still the answer, which is what the deadline is for. So is the
        deadline on Windows, and for a supervisor a signal killed anywhere, because those
        exits say nothing about whether the worker is still running.
        """
        still = []
        for child, attempt_path, unit_id in self._children:
            code = child.poll()
            if code is None:
                still.append((child, attempt_path, unit_id))
                continue
            try:
                # Read as bytes first, so a file the host refuses to hand over is told apart
                # from one holding no record. `read_status` answers None for both, and
                # writing over a file this program could not read would put a record on top
                # of one that may already be there.
                (attempt_path / STATUS_NAME).read_bytes()
            except OSError as exc:
                self.say(f"{unit_id} {attempt_path.name}: the supervisor exited {code} and "
                         f"its status file could not be read: {exc}")
                continue
            if read_status(attempt_path / STATUS_NAME) is not None:
                continue
            if os.name != "posix" or code < 0:
                # **Killed from outside, and its worker may be alive.** The supervisor starts
                # its reviewer in a session of its own, so a kill of the supervisor -- the
                # memory killer, an operator's kill -9 -- leaves that reviewer running with
                # nothing to stop it. A record here would count as proof the execution ended
                # and release the slot, and the resumed run would start a second worker
                # beside the live one, which is the exact thing the `--stopped-confirmed`
                # attestation exists to prevent. Left unrecorded, the attempt reaches
                # `uncertain` at its deadline and waits for that attestation. On POSIX a
                # death by signal is a negative code and an exit on the supervisor's own
                # path, which has stopped its reviewer or never started one, is not. Windows
                # reports a terminated process with an ordinary exit code, so there the two
                # cannot be told apart and nothing is recorded: the deadline is the answer,
                # as it is for a resumed driver.
                _progress(self.rundir, f"{unit_id} {attempt_path.name} supervisor exited "
                                       f"{code} without a status; its worker may still be "
                                       f"running, so the attempt keeps its slot until its "
                                       f"deadline")
                continue
            record = {
                "status": "error",
                "reason": f"the supervisor exited {code} without reporting a status",
                "supervisor_exit": code,
                "stderr_tail": _stderr_tail(attempt_path / STDERR_NAME),
            }
            try:
                with open(attempt_path / STATUS_NAME, "ab") as handle:
                    # On its own line: whatever partial line the supervisor left is not
                    # a record, and the reader takes the last line that is one.
                    handle.write(b"\n" + json.dumps(record).encode("utf-8") + b"\n")
            except OSError as exc:
                # Left unrecorded rather than raised: the attempt then reads as running
                # and reaches `uncertain` at its deadline, which is the cautious answer,
                # and a refused write is reported where the operator reads.
                self.say(f"{unit_id} {attempt_path.name}: the supervisor exited {code} "
                         f"without a status and the record could not be written: {exc}")
                continue
            _progress(self.rundir, f"{unit_id} {attempt_path.name} supervisor exited "
                                   f"{code} without a status")
            self.invalidate(unit_id)
        self._children = still

    def _record_no_launch(self, unit_id: str, attempt_path: Path, index: int,
                          exc: BaseException) -> bool:
        """Adjudicate an attempt that was claimed and never started. Returns ``True``,
        because a claim that was adjudicated is a spawn that happened and the caller counts
        it.

        The one outcome the supervisor cannot record, because it never ran — and there is
        more than one way to reach it: the spawn itself can fail, and so can opening the
        file the spawn was going to write its status into.

        **A launch the volume stopped is not a launch the supervisor failed.** §5.3 gives
        `launch-failed` a failure allowance because it means the supervisor could not start
        — not because the disk broke. Read as that, three storage faults publish
        `error.txt` as this unit's answer while the thing to fix is the volume. Classified
        before the charging disposition is built, so what is recorded charges nothing, and
        the fault is noted so the run pauses.

        **Computed whole, then committed once.** This wrote a provisional record and then
        unlinked and replaced it, which makes "immutable" a claim replay cannot rely on: a
        kill between the two leaves a unit whose last disposition says publish nothing while
        its allowances are spent — neither eligible nor terminal, and nothing any pass can
        finish — and a kill after the unlink loses an adjudication that had already been
        made.
        """
        storage = isinstance(exc, OSError) and _is_storage_exception(exc)
        failure = {
            "attempt": attempt_path.name, "unit": unit_id,
            "outcome": INFRASTRUCTURE if storage else LAUNCH_FAILED,
            "reason": f"the supervisor could not be started: {exc}",
            # Nothing was started, whichever of the two this is. The predicate every
            # cleanup and capacity question asks reads this: without it the attempt
            # holds its slot and protects a working copy for the rest of the run,
            # waiting on a worker that does not exist.
            "launched": False,
            "at": time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime()),
        }
        if storage:
            failure["storage_fault"] = True
        others = [(att.index, att.disposition) for att in self.attempts(unit_id)
                  if att.disposition is not None and att.index != index]
        ordered = [record for _index, record
                   in sorted([*others, (index, failure)], key=lambda pair: pair[0])]
        failure["intended_publication"] = _intended(ordered, self.granted(unit_id))
        _write_json(attempt_path / DISPOSITION_NAME, failure)
        _progress(self.rundir, f"{unit_id} {attempt_path.name} -> {failure['outcome']}")
        if storage:
            # Noted and acted on in the same pass, so the claim after this one does not
            # go out against the same failing volume.
            self.note_storage_fault(f"{unit_id} {attempt_path.name}: "
                                    f"{failure['reason']}")
            self.reconcile_storage()
        self.invalidate(unit_id)
        return True

    def generation(self, account: str) -> int:
        """Which generation of ``account`` a spawn belongs to.

        Derived by replay, like every other fact here: the count of incidents a successful
        probe has closed. It is recorded in every attempt's spawn record, so an attempt
        launched before anyone knew of an outage still belongs to the incident it ran into.
        """
        path = self.rundir / engine.DISPATCH_DIR / f"{GENERATIONS_PREFIX}{account}.json"
        record = _read_json(path)
        if isinstance(record, dict) and isinstance(record.get("closed"), int):
            return max(0, record["closed"])
        return 0

    def accounts(self) -> dict[str, list[str]]:
        """Each provider account, and the lanes configured against it. Two lanes on one
        account pause together, because it is the account that is refusing."""
        found: dict[str, list[str]] = {}
        for lane, spec in self.lanes.items():
            found.setdefault(spec.account, []).append(lane)
        return found

    def fault_patterns(self, attempt: Attempt) -> tuple:
        """What counts as this attempt's provider refusing, from the configuration."""
        spec = self.lanes.get((attempt.argv or {}).get("lane"))
        return spec.fault_patterns if spec is not None else ()

    def providers(self) -> dict[str, ProviderState]:
        """Every account's breaker, derived from the attempts, cached for one pass."""
        if self._provider_cache is None:
            by_account: dict[str, list[Attempt]] = {a: [] for a in self.accounts()}
            for unit in self.units():
                for attempt in self.attempts(unit["id"]):
                    account = (attempt.argv or {}).get("account")
                    if account in by_account:
                        by_account[account].append(attempt)
            self._provider_cache = {
                account: provider_state(account, self.generation(account), attempts)
                for account, attempts in by_account.items()}
        return self._provider_cache

    def request_drain(self, reason: str) -> None:
        """Stop claiming, run-wide, and say why. Running attempts finish and the run exits
        resumable; nothing rolls into the next round."""
        if self.draining:
            return
        self.draining = True
        self.drain_reason = reason
        self.say(f"draining: {reason}")
        _progress(self.rundir, f"drain: {reason}")

    # -- 7.1-7.2 storage ----------------------------------------------------- #
    def request_pause(self, reason: str) -> None:
        """Stop the run over something that is nobody's answer — a full disk, a read-only
        volume — and exit **non-zero**, resumable, naming what failed.

        It borrows the drain's stop-claiming half and not its exit status, and the split is
        the whole point. A drain is an operator asking for a clean stop and is a success; a
        pause is this program saying the run cannot be trusted to continue, and a caller who
        cannot tell the two apart from the exit code will restart a run straight back into
        the fault.
        """
        if self.pause_reason:
            return
        self.pause_reason = reason
        self.draining = True
        self.say(f"paused: {reason}")
        _progress(self.rundir, f"pause: {reason}")

    def note_storage_fault(self, where: str) -> None:
        """Record that an attempt's failure was the volume's, for the reconciliation below.

        Kept per process rather than replayed from the dispositions, deliberately: a
        disposition marked with a storage fault stays on disk for ever, and a run resumed
        after the disk was fixed would read it and pause again before doing any work. Each
        fault stops the run once — the pass that first adjudicates it — and never again.
        """
        if where not in self._storage_faults:
            self._storage_faults.append(where)

    def reconcile_storage(self) -> None:
        """Turn a storage fault into a stop. Beside the breaker's reconciliation, for the
        same reason: the facts are already written as dispositions, so a pass that runs
        twice reaches the same place."""
        if not self._storage_faults or self.pause_reason:
            return
        self.request_pause(
            f"the volume holding {self.rundir} failed a write for "
            + "; ".join(self._storage_faults)
            + ". Nothing was charged and no unit was answered by it: free space or make "
              "the volume writable, then run the same command again")

    # -- 7.4 disk headroom --------------------------------------------------- #
    def snapshot_bytes(self) -> int:
        if self._snapshot_bytes is None:
            self._snapshot_bytes = tree_bytes(self.rundir / SNAPSHOT_DIR)
        return self._snapshot_bytes

    def headroom(self) -> str:
        """Why a write-capable unit cannot be claimed right now, or ``""``.

        A copy of the snapshot plus a margin for what a build puts in it, against free space
        less a floor. **Retained and protected copies are not counted as room that could be
        freed**: this program never deletes one to make space — a protected copy is not its
        to remove and a retained one is the evidence a verdict cites — so the space they
        hold is gone as far as this question goes, which is what free space already says.

        Below the floor the units are **not claimed**, never failed: a volume with no room
        is not a unit's answer, and a unit that was never claimed charges nothing and is
        still there when there is room again.
        """
        free = free_bytes(self.rundir)
        if free is None:
            return ""
        needed = self.snapshot_bytes() + self.build_margin
        available = free - self.disk_floor
        if needed <= available:
            return ""
        return (f"a working copy needs about {needed} bytes and {free} are free, of which "
                f"{self.disk_floor} are held back as a floor")

    # -- 5.4 copies ---------------------------------------------------------- #
    def candidate_severities(self) -> dict[str, int]:
        """Each candidate's proposed severity as a rank, from the route record.

        **Only a record that is there is remembered.** The route record is written once and
        never edited, which is what makes it safe to hold — but it does not exist until
        routing writes it, and the reading round runs entirely before that. A run resumed
        mid-reading with a copy on disk asks this question, gets the empty answer that is
        correct at that moment, and would then hold it for the rest of the process: every
        copy ranks as unrankable, and the retention cap drops copies by name instead of by
        severity, keeping a nit's reproduction and deleting a blocker's. Absent is not a
        fact about the candidates, so it is not cached.
        """
        if self._candidate_severities is None:
            doc = _read_json(self.rundir / engine.CANDIDATES_FILE_NAME)
            listed = doc.get("candidates") if isinstance(doc, dict) else None
            if not isinstance(listed, list):
                return {}
            table: dict[str, int] = {}
            for candidate in listed:
                if not isinstance(candidate, dict):
                    continue
                raised = candidate.get("raised_by")
                table[str(candidate.get("id"))] = _most_severe_rank(
                    [str(row.get("severity")) for row in raised
                     if isinstance(row, dict)] if isinstance(raised, list) else [])
            self._candidate_severities = table
        return self._candidate_severities

    def copy_rank(self, unit_id: str, attempt: Attempt) -> int:
        """How severe the worst thing this copy is evidence for is.

        The verifier's revision where it made one and the raiser's proposal otherwise, which
        is the same rule the report ranks a defect by. A copy the run cannot rank sorts
        below every severity — see :data:`UNRANKED`.
        """
        revised: dict[str, str] = {}
        reply = _read_json(attempt.path / REPLY_NAME)
        verdicts = reply.get("verdicts") if isinstance(reply, dict) else None
        for verdict in verdicts if isinstance(verdicts, list) else []:
            if not isinstance(verdict, dict):
                continue
            revision = verdict.get("revision")
            if isinstance(revision, dict) and revision.get("severity") in engine.SEVERITIES:
                revised[str(verdict.get("candidate"))] = str(revision["severity"])
        proposed = self.candidate_severities()
        ranks = []
        listed = self.unit_row(unit_id).get("candidates")
        for cid in listed if isinstance(listed, list) else []:
            if str(cid) in revised:
                ranks.append(engine.SEVERITIES.index(revised[str(cid)]))
            elif str(cid) in proposed:
                ranks.append(proposed[str(cid)])
        return min(ranks) if ranks else UNRANKED

    def copies(self) -> tuple[list[Copy], list[Copy]]:
        """``(protected, retained)`` — every working copy still on disk, and why it is.

        **Protected is not retention.** A copy whose attempt cannot be shown to have stopped
        is not evidence being kept; it is a directory that is not this program's to remove,
        because the supervisor detaches its worker and that worker may still be running in
        it. So it is carried to the end of the run, named in the record, and excluded from
        the cap — a cap that counted it would delete the evidence of a unit that finished in
        order to stay under a budget spent by one that did not.
        """
        protected: list[Copy] = []
        retained: list[Copy] = []
        for unit in self.units():
            unit_id = unit["id"]
            record = landed(self.rundir, unit_id) if self.terminal(unit_id) else None
            answered = (record or {}).get("attempt")
            for attempt in self.attempts(unit_id):
                token = (attempt.argv or {}).get("token")
                if not isinstance(token, str) or not token:
                    continue
                work = self.rundir / "work" / token
                # A copy the host will not describe is not a copy that is gone. Skipped, it
                # is absent from `status` — where an operator reads which reproductions the
                # run is holding and which directories a live worker is still inside — and
                # absent from the retention arithmetic that is supposed to bound it.
                if not _is_directory(work):
                    continue
                if not _execution_ended(attempt):
                    protected.append(Copy(unit=unit_id, attempt=attempt.name, token=token,
                                          path=work, bytes=tree_bytes(work),
                                          rank=self.copy_rank(unit_id, attempt),
                                          protected=True))
                elif (record is not None and attempt.name == answered
                        and keeps_evidence(attempt)):
                    retained.append(Copy(unit=unit_id, attempt=attempt.name, token=token,
                                         path=work, bytes=tree_bytes(work),
                                         rank=self.copy_rank(unit_id, attempt),
                                         protected=False))
        return protected, retained

    def sweep_copies(self) -> None:
        """Hold retention to ``--keep-repro-bytes``, dropping **highest-severity-last**.

        Most severe first, kept while the running total fits; from the first copy that does
        not fit, that one and every less severe copy after it are deleted and **named in the
        run's record**. A reader who finds a reproduction missing has one place to look and
        a reason there.
        """
        self._finish_dropping()
        protected, retained = self.copies()
        retained.sort(key=lambda copy: (copy.rank, copy.unit, copy.attempt))
        kept: list[Copy] = []
        dropped: list[Copy] = []
        total = 0
        for copy in retained:
            if dropped or (self.keep_repro_bytes
                           and total + copy.bytes > self.keep_repro_bytes):
                dropped.append(copy)
                continue
            total += copy.bytes
            kept.append(copy)
        # **The intent is recorded before the tree is removed, and the removal is confirmed
        # after.** A kill or a fault between `rmtree` and the record destroys evidence and
        # leaves no account of why it is gone — and nothing can reconstruct one, because the
        # copy that would have been enumerated is exactly what was deleted. Written first,
        # an interrupted deletion is an entry whose directory is still there, which the next
        # pass finishes: the shape §3.5 already uses for a publication.
        self._record_copies(protected, kept, dropped)
        for copy in dropped:
            remove_tree(copy.path, self.rundir)
            self.say(f"dropped the working copy of {copy.unit} {copy.attempt} "
                     f"({copy.severity}, {copy.bytes} bytes): retention is capped at "
                     f"{self.keep_repro_bytes} bytes")
            _progress(self.rundir, f"{copy.unit} {copy.attempt} copy dropped over the cap")
        if dropped:
            self._finish_dropping()

    def _finish_dropping(self) -> None:
        """Complete the deletions the record says were begun, idempotently.

        Every entry the cap wrote is marked done once its directory is gone; one whose
        directory is still there is a deletion an interruption left half-made, and it is
        finished here rather than decided again. Run before the cap measures anything, so a
        copy that is on its way out is not weighed as one the run is keeping.
        """
        path = self.rundir / engine.DISPATCH_DIR / COPIES_RECORD_NAME
        record = _read_json(path)
        history = record.get("dropped") if isinstance(record, dict) else None
        if not isinstance(history, list):
            return
        changed = False
        for entry in history:
            if not isinstance(entry, dict) or entry.get("removed"):
                continue
            token = entry.get("token")
            # Only a token shape, like every other sweep here: this record is read back
            # after a crash, and a name it does not recognize is not a path to delete.
            if isinstance(token, str) and _TOKEN_RE.fullmatch(token):
                work = self.rundir / "work" / token
                if _is_directory(work):
                    remove_tree(work, self.rundir)
                    self.say(f"finished dropping the working copy of {entry.get('unit')} "
                             f"{entry.get('attempt')}, which an interruption left on disk")
                    _progress(self.rundir, f"{entry.get('unit')} {entry.get('attempt')} "
                                           f"copy deletion completed after an interruption")
                # Both questions are asked with a refusal available, because the entry this
                # writes is the run's only account of a directory nothing can enumerate any
                # more: a path the host would not describe, read as gone, is recorded as
                # deleted while it is still on disk and nothing ever looks at it again.
                if _present(work):
                    # **Done means gone.** A removal the host refused leaves the entry open,
                    # so the next pass tries again; marking it complete here would leave a
                    # directory on disk that the record says is not.
                    continue
            entry["removed"] = True
            changed = True
        if changed:
            _write_json(path, {**record, "dropped": history})

    def _record_copies(self, protected: Sequence[Copy], kept: Sequence[Copy],
                       dropped: Sequence[Copy]) -> None:
        """Write ``dispatch/copies.json`` when it would say something new.

        The dropped list is **appended to**: it records deletions, and rewriting it from
        what is on disk now would erase the only account of what a cap removed. Each entry
        carries ``removed``, which is the one thing about it that is ever revised — it is
        written before the tree is deleted and turned true once it is gone, so an entry the
        next pass finds still false is a deletion to finish rather than one to re-decide.

        **One entry per copy, however many passes it takes.** A removal the host keeps
        refusing — a file still open under it, which is the ordinary Windows case — leaves
        the directory on disk, where the next pass enumerates it and decides to drop it
        again. Appended each time, one blocked deletion becomes a record that reads as many,
        and the reconciliation this record exists for stops being idempotent.
        """
        path = self.rundir / engine.DISPATCH_DIR / COPIES_RECORD_NAME
        current = _read_json(path)
        before = current if isinstance(current, dict) else {}
        history = before.get("dropped")
        history = list(history) if isinstance(history, list) else []
        pending = {(entry.get("unit"), entry.get("attempt"), entry.get("token"))
                   for entry in history
                   if isinstance(entry, dict) and not entry.get("removed")}
        for copy in dropped:
            if (copy.unit, copy.attempt, copy.token) in pending:
                continue
            history.append({**copy.named(), "removed": False,
                            "at": time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime())})
        record = {
            "cap_bytes": self.keep_repro_bytes,
            "retained": [copy.named() for copy in kept],
            "retained_bytes": sum(copy.bytes for copy in kept),
            "protected": [copy.named() for copy in protected],
            "dropped": history,
        }
        # A run with no copies at all says nothing and writes nothing: a record of three
        # empty lists is a file every reading round would create and no reader would want.
        if record == before or not (before or protected or kept or history):
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        _write_json(path, record)

    def reclaim_leaked_tokens(self) -> list[str]:
        """Remove the worker directories a kill during preparation left behind.

        A spawn creates ``in/<token>/``, ``out/<token>/`` and ``work/<token>/`` **before** it
        claims the attempt, so a kill in that window leaves directories no attempt names —
        and a write-capable one is a whole copy of the snapshot. Nothing else collects them:
        they belong to no unit, so no landing, no cleanup and no operator command reaches
        them.

        **Every launched worker's token is on disk before the launch**, in that attempt's
        ``argv.json``, so a directory named by no attempt was never handed to anybody. Only
        the token shape is swept, and only under this run's own three directories: anything
        else there is not this program's to delete.

        **An attempt record that exists and could not be read stops the whole sweep.** The
        record is what names an attempt's token, so a read the host refused leaves a live
        worker's input, output and working directory looking exactly like a leak — and this
        would delete all three out from under it. Deletion needs two facts (§5.4) and an
        unreadable record establishes neither; a leak that survives an outage costs disk,
        and the other mistake costs a running worker its tree.
        """
        known, unreadable = set(), []
        for unit in self.units():
            for attempt in self.attempts(unit["id"]):
                token = (attempt.argv or {}).get("token")
                if isinstance(token, str):
                    known.add(token)
                elif not _record_absent(attempt.path / ARGV_NAME):
                    unreadable.append(f"{unit['id']} {attempt.name}")
        if unreadable:
            for name in unreadable:
                if name in self._unreadable_said:
                    continue
                self._unreadable_said.add(name)
                self.say(f"nothing is being reclaimed while {name}'s spawn record cannot be "
                         f"read: it names the directories its worker is using, and a sweep "
                         f"that cannot read it cannot tell them from a leak")
                _progress(self.rundir, f"{name}: unreadable spawn record; no reclamation")
            return []
        removed = []
        for area in ("in", "out", "work"):
            base = self.rundir / area
            try:
                entries = sorted(base.iterdir())
            except OSError:
                # The other tolerated read, and the same reason: skipping an area this
                # program cannot list deletes nothing. It is the sweep's own directory, not
                # a record anything is decided from, and a leak that outlives a bad volume
                # costs disk where the other mistake costs a running worker its tree. The
                # per-entry questions below are tolerated on the same terms and in the same
                # direction: an entry nobody can describe is passed over, not removed.
                continue
            for entry in entries:
                if (entry.name in known or entry.is_symlink() or not entry.is_dir()
                        or not _TOKEN_RE.fullmatch(entry.name)):
                    continue
                remove_tree(entry, self.rundir)
                removed.append(f"{area}/{entry.name}")
        return removed

    def _close_generation(self, account: str, generation: int,
                          by: str = "a probe answered") -> None:
        """Record that ``account``'s current incident is over.

        Closing is what makes every failure carrying the old number a closed incident rather
        than a recurrence, and it is what puts the unexplained-failure count back to zero —
        so a historical pair cannot re-pause a provider that has since recovered.

        ``by`` says what closed it, because the two answers are not the same claim: a probe
        closing one is this program observing the provider work, and a resume closing one is
        an operator asserting it (§6.3 and the resume rule below).
        """
        path = self.rundir / engine.DISPATCH_DIR / f"{GENERATIONS_PREFIX}{account}.json"
        if self.generation(account) > generation:
            return  # already closed; replaying it would skip a generation nobody used
        _write_json(path, {"closed": generation + 1, "closed_by": by,
                           "at": time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime())})
        self.say(f"{account}: {by}, so incident {generation} is closed and its units are "
                 f"eligible again")
        _progress(self.rundir, f"{account} generation {generation} closed: {by}")
        self._provider_cache = None

    def reset_drained_providers(self) -> None:
        """Close, once, the incident a previous run stopped on. Called at startup only.

        **A drained provider cannot recover on its own, and without this a resume does
        nothing at all.** The dispositions that drained the run are immutable and belong to
        a generation only a successful probe advances — so every later run re-derives the
        same drain, refuses to probe (`probe_due` declines while the drain stands), finds
        nothing eligible, and exits 0 having reviewed nothing. A run that silently does
        nothing and reports success is the failure this whole section exists to prevent.

        **An operator who starts the run again is asserting the provider is fixed**, which
        is the same assertion a probe would go out to test, so the reset is theirs to make
        by restarting and needs no command of its own. It is written as the ordinary
        generation record: a durable fact, written once, that a replay reads back rather
        than re-derives — so a poll cannot re-apply it, and the run that follows either
        works or meets the outage again as a recurrence at the new generation.
        """
        for account, state in self.providers().items():
            if state.drain:
                self._close_generation(account, state.generation,
                                       by="this run was started again over a stopped one")

    def reconcile_providers(self) -> None:
        """Act on the breakers: close what a probe answered, drain what cannot continue.

        Nothing here decides an outcome — every fact was already written as a disposition —
        so a pass that runs twice reaches the same place, and a pass that never ran leaves
        the next one with everything it needs.
        """
        for account, state in self.providers().items():
            if state.close_generation:
                self._close_generation(account, state.generation)
        for account, state in self.providers().items():
            if state.drain:
                self.request_drain(f"{account}: {state.drain}")
            elif state.paused and account not in self._paused_said:
                self._paused_said.add(account)
                self.say(f"{account} is paused: {state.paused}. Its units are not eligible "
                         f"and have charged nothing; one probe will ask whether it works")
                _progress(self.rundir, f"{account} paused: {state.paused}")
            elif not state.paused:
                self._paused_said.discard(account)

    # -- 7.3 the snapshot ---------------------------------------------------- #
    def check_snapshot(self) -> None:
        """Stop the run if the snapshot is no longer the tree the readers were measured
        against, and harden it again either way.

        **A changed snapshot is not resumable and does not pause**: pausing says come back
        when the volume is fixed, and there is nothing here a driver can fix. Every citation
        in the report points at these bytes, and a report built half from one tree and half
        from another says nothing a reader can act on. So it is refused by name, with the
        paths, for a person to decide about.

        **A check that could not be made is not a modification.** The verification raises
        rather than reporting a difference when the host refuses it a file or the inventory,
        and that exception travels to the loop's pause: a transient fault on the volume must
        not tell an operator their completed run is unusable and to plan a new one. The
        refusal below is for a snapshot this program actually read and found changed.

        Re-hardening on the way through costs a chmod per file and covers a snapshot planned
        by a driver that predates this rule, or one somebody made writable by hand.
        """
        problems = verify_snapshot(self.rundir)
        if problems:
            raise DriverError(
                f"the snapshot under {self.rundir / SNAPSHOT_DIR} is no longer what "
                f"{INVENTORY_NAME} measured, so nothing read against it can be trusted:\n"
                + "\n".join(f"  {line}" for line in problems[:20])
                + (f"\n  ... and {len(problems) - 20} more" if len(problems) > 20 else "")
                + "\nPlan a new run; this one cannot be continued")
        harden_snapshot(self.rundir / SNAPSHOT_DIR)

    # -- 4. the adoption pass ------------------------------------------------ #
    def _finish_publication(self, unit_id: str) -> None:
        """Land the answer an adjudicated attempt intended, if the unit has not landed one.

        The first attempt in launch order carrying an intention wins, so a late attempt
        cannot displace an earlier unit's answer.
        """
        if self.terminal(unit_id):
            return
        if replay_publication(self.rundir, unit_id, self.grace):
            self.invalidate(unit_id)

    def adopt(self, only: Sequence[dict] | None = None) -> None:
        """Bring the run directory up to date without spawning anything.

        In order: finish publications a crash interrupted, adjudicate what has decided,
        publish what that decided, and release the worker directories of attempts that are
        provably finished. Every step is idempotent, because every one of them is what a
        resumed run performs before it does anything else.
        """
        # First, so a supervisor that exited without a status is adjudicated in this pass
        # rather than the next: the record it gets is what step 2 reads.
        self.reap_children()
        self.invalidate()
        for unit in (self.units() if only is None else only):
            unit_id = unit["id"]
            # 1. Replay unfinished publications. Never re-adjudicated: the disposition
            #    already said what this unit's answer is, and re-deciding it is how a
            #    crash between deciding and publishing turns into a different answer.
            self._finish_publication(unit_id)
            # 2. Adjudicate. A resolved attempt is adjudicated the same way a decided one
            #    is; the operator's record is simply the input.
            for attempt in self.attempts(unit_id):
                if attempt.state in (DECIDED, RESOLVED):
                    adjudicate(self, attempt)
                    self.invalidate(unit_id)
            # 3. Publish what step 2 decided, under step 1's rule.
            self._finish_publication(unit_id)
            # 4. Release worker directories, on the two-fact rule.
            if self.terminal(unit_id):
                answered = (landed(self.rundir, unit_id) or {}).get("attempt")
                for attempt in self.attempts(unit_id):
                    if _execution_ended(attempt):
                        _release_worker_paths(self.rundir, attempt,
                                              landed=attempt.name == answered)
        # 5. Recompute the breakers from what step 2 just wrote, and act on them. Last,
        #    because everything they are derived from is a disposition, and a pause read
        #    before this pass adjudicated would be one round out of date.
        self.reconcile_providers()
        # 6. A storage fault charged nothing and answered no unit; this is the half that
        #    stops the run. Beside the breakers because it is the same kind of step —
        #    derived state turning into a decision about the run rather than about a unit.
        self.reconcile_storage()
        # 7. The run-wide reconciliations, on a full pass only. Both walk every unit and
        #    both measure directories, so running them on the scoped pass a round does
        #    every poll would spend more time measuring copies than dispatching workers.
        if only is None:
            for name in self.reclaim_leaked_tokens():
                self.say(f"removed {name}, which a kill during preparation left behind")
                _progress(self.rundir, f"reclaimed the leaked directory {name}")
            self.sweep_copies()

    # -- 4. one round -------------------------------------------------------- #
    def dispatch_round(self, kinds: Sequence[str]) -> None:
        mine = [unit for unit in self.units() if unit.get("kind") in kinds]
        self.say(f"round: {len(mine)} unit(s) of kind {', '.join(kinds)}")
        # Reset HERE, where the round begins, and not inside the loop below, whose every
        # pass is one poll. Reset per poll, a round held back for room with work still in
        # flight measured every copy on disk at every poll interval — the very cost that
        # keeps the sweep off the scoped pass — while `_blocked_on_disk` below is per poll
        # on purpose, because it is the answer of the pass that set it.
        self._swept_for_room = False
        while True:
            self.adopt(mine)
            self.save_budget()
            claimed = 0
            self._blocked_on_disk = ""
            if not (self.stop_requested() or self.over_budget()):
                reserved = self.reserving()
                writers = self.writers_running()
                providers = self.providers()
                probed: set[str] = set()
                for unit in mine:
                    # **Asked before every claim, not once per pass.** Read once above, a
                    # signal arriving during the first spawn still launches the rest of the
                    # batch — the whole of a large capacity — after this program has already
                    # said that nothing more will be claimed.
                    if self.stop_requested() or self.over_budget():
                        break
                    lane = unit.get("lane", engine.LANES[0])
                    claim, probe = self.claim_decision(unit, reserved, writers,
                                                       providers, probed)
                    if not claim:
                        continue
                    if not self.spawn(unit, probe=probe):
                        # Aborted, not claimed: the stop condition arrived inside the
                        # preparation. Nothing is in flight for this unit and the rest of
                        # the batch is not started either.
                        break
                    if probe:
                        probed.add(self.lanes[lane].account)
                    reserved[lane] = reserved.get(lane, 0) + 1
                    if unit.get("kind") in WRITE_CAPABLE_KINDS:
                        writers += 1
                    claimed += 1
            # **The test is "nothing is in flight and nothing was claimed", not "nothing is
            # eligible".** A unit can be eligible and permanently unable to start — its
            # lane's slot held by an `uncertain` attempt whose worker may still be
            # alive, which is a reservation nothing releases without an operator. Asking
            # about eligibility instead spins here for ever on a run that should have
            # stopped and named the attempt nobody can account for.
            if claimed == 0 and not self._in_flight(mine) and not self._waiting_to_probe(mine):
                # **Nothing can run, and the reason may be the volume rather than the
                # work.** A round that ends here with units held back for room would
                # otherwise report them as units with no answer, which sends an operator to
                # `resolve-unit` over a disk. The pause names the volume and exits non-zero.
                if self._blocked_on_disk:
                    self.request_pause(
                        f"there is not enough room to run a write-capable unit: "
                        f"{self._blocked_on_disk}. Nothing was claimed and nothing was "
                        f"charged; free space and run the same command again")
                return
            time.sleep(self.poll)

    def claim_decision(self, unit: dict, reserved: dict[str, int], writers: int,
                       providers: dict[str, ProviderState], probed: set,
                       now: float | None = None) -> tuple[bool, bool]:
        """Whether to claim an attempt for ``unit`` now, and whether it is the probe.

        One method rather than a run of conditions inside the loop, because what it answers
        — claim or not, and which rule said so — has to be askable without driving a whole
        round. A round takes a poll interval per pass and spawns real workers; the rules
        below are decided per unit and are what a test needs to be able to see.

        **A paused provider's units are simply not eligible**, and exactly one of them goes
        out as the probe that asks whether it works again. They have charged nothing, so
        waiting costs nothing; what is never done is moving them to the other runtime, since
        a mixed run is one the report cannot describe truthfully.
        """
        lane = unit.get("lane", engine.LANES[0])
        if reserved.get(lane, 0) >= self.lanes[lane].slots:
            return False, False
        if unit.get("kind") in WRITE_CAPABLE_KINDS and writers > 0:
            return False, False
        if not self.eligible(unit["id"]):
            return False, False
        if unit.get("kind") in WRITE_CAPABLE_KINDS:
            # **Asked after eligibility, not before.** A unit that has already landed needs
            # no copy, and a disk question asked about it would record the run as held back
            # for room it never needed — which stops a round where nothing was waiting.
            # Asked before the claim and never after: a unit refused for room charges
            # nothing and is claimable again the moment there is room. Failing it here
            # would make the volume the unit's answer, which is the whole of §7.
            short = self.headroom()
            if short:
                # Before holding the round back, enforce the retention cap once. A copy over
                # `--keep-repro-bytes` is already declared surplus — `sweep_copies` exists to
                # delete it — so the space it holds is reclaimable, and counting it as gone
                # stops a round for room that was never really taken. This is NOT the rule in
                # `headroom` bending: that rule is about a copy the run still owes somebody,
                # a protected one or the evidence a verdict cites, and neither is touched
                # here.
                #
                # Once per round, not per poll. The sweep measures every copy, which is why
                # the full pass carries it and a round's scoped pass does not; doing it again
                # on each poll would spend the round measuring instead of dispatching.
                if not self._swept_for_room:
                    self._swept_for_room = True
                    self.sweep_copies()
                    short = self.headroom()
            if short:
                self._blocked_on_disk = short
                return False, False
        spec = self.lanes.get(lane)
        state = providers.get(spec.account) if spec is not None else None
        if state is not None and state.paused:
            if spec.account in probed or not probe_due(
                    state, _now() if now is None else now, self.probe_backoff):
                return False, False
            return True, True
        return True, False

    def _in_flight(self, units: Sequence[dict]) -> bool:
        for unit in units:
            for attempt in self.attempts(unit["id"]):
                if attempt.state in (RUNNING, DECIDED):
                    return True
        return False

    def _waiting_to_probe(self, units: Sequence[dict]) -> bool:
        """Whether a paused provider has a probe coming that its back-off has not reached.

        Without this the round ends the moment a provider pauses — nothing is claimable and
        nothing is in flight — and reports every one of its units as stuck, when what is
        actually true is that the run is waiting to ask one question. A provider that can
        never be probed again (three uninformative answers, or one nobody can account for)
        is NOT waiting, and the round ends as it should.
        """
        if self.stop_requested() or self.over_budget():
            return False
        now = _now()
        for account, state in self.providers().items():
            if not state.paused or state.drain or state.probe_in_flight:
                continue
            if state.probe_unaccountable or \
                    state.uninformative_probes >= UNINFORMATIVE_PROBES_ALLOWED:
                continue
            if now >= state.last_probe_at + self.probe_backoff:
                continue  # due now, so the pass above found nothing eligible to send
            lanes = set(self.accounts().get(account, ()))
            if any(unit.get("lane") in lanes and self.eligible(unit["id"])
                   for unit in units):
                return True
        return False

    def unfinished(self, kinds: Sequence[str]) -> list[tuple[str, str]]:
        """Units of this round that have no terminal publication, with why."""
        out = []
        for unit in self.units():
            if unit.get("kind") not in kinds or self.terminal(unit["id"]):
                continue
            out.append((unit["id"], self.quarantined(unit["id"]) or "no answer landed"))
        return out

    def refuse_a_stranded_lane(self, kinds: Sequence[str]) -> None:
        """Refuse at this boundary if a lane has landed nothing and has no unit left that
        could change that.

        The same refusal the record makes at the end, made as soon as it is true. A lane is
        stranded when it has answered no unit **and** every unit addressed to it is already
        terminal: no later round re-addresses a unit that has published, so what it has
        landed at this boundary is what it will have landed at the report. Clustering and
        synthesis are model time, and spending both on a run that cannot be published buys
        the operator nothing but the wait.

        **Pending is what keeps the recovery case, and it is the whole of this rule.** A
        lane whose readers all failed has landed nothing at the end of the reading round and
        is NOT refused here: routing addresses every candidate the other lane raised to the
        lane that did not raise it, so its verification units are where it answers, and it
        is stranded only once those are terminal too. Asked on "landed nothing" alone this
        would end the run one round before the round that rescues it.

        **And the units to ask about are the ones that will exist, which is why a round with
        nothing left to finish is not asked at all.** A stage transition is what creates the
        next round's units, and a round whose own units are all terminal is a round whose
        transition has not run yet: the verification unit that answers for a silent lane is
        the thing `route` is about to write. Only a RESUMED run is ever at a boundary in that
        state — an uninterrupted one runs the stage and moves on within the same pass — so a
        question asked there is answered from a listing that is one stage out of date, and it
        strands a run a single engine call from its recovery. Let the transition happen and
        ask at the next boundary, where those units are on disk. Skipping costs nothing it
        was meant to save: a round with nothing left to finish has nothing to spawn either,
        and what this exists to prevent is a spawn.

        The check runs at every boundary rather than at a chosen one, because which round
        leaves a lane with nothing pending depends on what the run found: a run that raised
        no candidate at all addresses no verifier anywhere. Where that leaves nothing to ask
        at any boundary, the record at the end is the backstop and it refuses the same way.
        """
        if not self.unfinished(kinds):
            return
        executed = executed_provenance(self)
        pending = {lane: 0 for lane in self.lanes}
        for unit in self.units():
            lane = unit.get("lane")
            if lane in pending and not self.terminal(unit["id"]):
                pending[lane] += 1
        stranded = [lane for lane in sorted(self.lanes)
                    if not executed[lane].landed and not pending[lane]]
        if stranded:
            raise DriverError(no_landing_refusal(
                stranded, lane_descriptions(self.lanes, executed)))

    # -- 4. the loop --------------------------------------------------------- #
    def loop(self) -> int:
        """Carry the run to its report, or stop and say why.

        **A fault of the host ends the run here rather than where it happened.** Every
        driver read and write sits in the middle of a sequence — adjudicate, publish, record
        — and the one safe thing to do with the rest of that sequence is not to run it, so
        the operation raises and this catches it. Nothing has been inferred from a write
        that did not happen or a record nobody could read, and the exit is non-zero and
        resumable.
        """
        try:
            # Before the first round and nowhere else: a provider a previous run stopped on
            # is given one generation to prove it works, on the operator's say-so that they
            # restarted it. Inside the loop this would undo a recurrence as fast as the
            # breaker could find one.
            self.reset_drained_providers()
            return self._run_rounds()
        except RunPaused as exc:
            self.request_pause(f"{exc}; nothing was adjudicated from it")
            self.say("the run is resumable: put right what the host refused — free space, "
                     "make the volume writable, restore the permission — and run the same "
                     "command again")
            # The budget write goes to the same volume, so it is allowed to fail here. A
            # spend of a few minutes is not worth turning a named pause into a traceback.
            with contextlib.suppress(RunPaused, engine.ReviewPanelError, OSError):
                self.save_budget(force=True)
            return EXIT_STOPPED

    def _run_rounds(self) -> int:
        while True:
            marker = _marker(self.rundir)
            if marker == REPORTED_STAGE:
                self.say(f"reported: {self.rundir / engine.REPORT_NAME}")
                self.save_budget(force=True)
                return EXIT_OK
            if marker not in ROUNDS:
                raise DriverError(
                    f"{self.rundir / engine.UNITS_FILE_NAME} is at stage {marker!r}, "
                    f"which this driver does not know how to continue from")
            # **At startup and at every round boundary**, which is what this line is: the
            # first turn of this loop is a run starting, and every later one is a round
            # about to begin. A snapshot that changed under the readers stops the run —
            # every citation in the report is against these bytes, and a reader measured
            # against other ones cannot be told apart from a reader that was wrong.
            self.check_snapshot()
            kinds, stage = ROUNDS[marker]
            # One pass over the WHOLE run before the round's own passes, which are scoped
            # to the round's units. A previous round's unit cannot be unfinished — the
            # marker does not advance until every one of them is terminal — but a resumed
            # run has read nothing yet, and this is where it reads it.
            self.adopt()
            # Between adopting and spawning, which is the last moment before this round
            # costs anything.
            self.refuse_a_stranded_lane(kinds)
            self.dispatch_round(kinds)
            # **And again now the round's workers have run, before anything consumes what
            # they produced.** The check above is the boundary this round STARTED at; a
            # worker that altered the tree during the round is caught by nobody until the
            # next boundary, and the last round has no next boundary — the marker reaches
            # `reported` and the loop returns. So the round that matters most, the one whose
            # answers are published, is the one this would otherwise never check. On Windows
            # a read-only attribute does not stop a file being created, which is exactly why
            # §7.3 verifies the file set and not only the digests.
            self.check_snapshot()
            # **A stop is reported before a quarantine is**, and the order is the point: a
            # run that stopped claiming because it was asked to, or because its budget ran
            # out, leaves units with no answer for that reason and not for a defect. Naming
            # them as quarantined would send an operator to `resolve-attempt` over a run
            # that needs nothing but to be started again.
            if self.stop_requested() or self.over_budget():
                self.say((f"paused: {self.pause_reason}; the run is resumable"
                          if self.pause_reason else
                          (f"drained: {self.drain_reason}; the run is resumable"
                           if self.drain_reason else
                           "drained on request; the run is resumable")) if self.draining
                         else f"the time budget of {self.max_hours} h is spent; run again "
                              f"with --extend <hours beyond the limit>, larger than "
                              f"{self._extended / 3600.0:g}")
                for unit_id, why in self.unfinished(kinds):
                    self.say(f"  not finished: {unit_id} — {why}")
                self.save_budget(force=True)
                # Never rolls into the next round: a signal asking a run to stop must not
                # be answered by starting the next one's work. **A pause is not a drain**:
                # both stop claiming, and only the drain is a success.
                return EXIT_OK if self.draining and not self.pause_reason else EXIT_STOPPED
            stuck = self.unfinished(kinds)
            if stuck:
                for unit_id, why in stuck:
                    self.say(f"stopped: {unit_id} — {why}")
                self.say("the run is resumable: resolve these, then run the same command "
                         "again")
                self.save_budget(force=True)
                return EXIT_STOPPED
            if stage == "report":
                # `dispatch.json` is committed first, because `report` loads it before it
                # renders anything.
                _write_json(self.rundir / engine.DISPATCH_FILE_NAME,
                            dispatch_record(self.lanes, executed_provenance(self)))
            # Under this driver's lock, so a claim on disk is one nobody holds.
            for name in clear_engine_claims(self.rundir):
                self.say(f"cleared {name}, which a killed {name.split('.')[0]} left behind")
                _progress(self.rundir, f"cleared the stranded claim {name}")
            code, out, err = _engine_stage(stage, str(self.rundir))
            if out.strip():
                self.say(out.rstrip())
            if code != 0:
                # **A stage that met the volume is the volume's failure, not the run's.**
                # The marker is the engine's own last write and a unit's answer is its own
                # read, so a failing device reaches this program as a stage refusal rather
                # than as a `RunPaused` of its own — and read as an ordinary refusal it
                # would end the run non-resumably over something a gigabyte of free space or
                # a remount fixes. The engine's message carries the path, so the pause names
                # it by quoting it.
                if _names_storage_fault(err or out):
                    self.request_pause(
                        f"{stage} could not read or write: {(err or out).strip()}")
                    self.say("the run is resumable: free space, or make the volume "
                             "writable, and run the same command again")
                    with contextlib.suppress(RunPaused, engine.ReviewPanelError, OSError):
                        self.save_budget(force=True)
                    return EXIT_STOPPED
                raise DriverError(f"{stage} refused this run:\n{err or out}")
            # Re-read rather than assume: the marker is the commit record, and a stage that
            # returned 0 without moving it would otherwise loop here forever.
            moved = _marker(self.rundir)
            if moved == marker:
                raise DriverError(
                    f"{stage} returned success without advancing the marker, which is "
                    f"still {marker!r}; the run directory is not in a state this driver "
                    f"can continue from")


# --------------------------------------------------------------------------- #
# 8. operator reconciliation — both commands require the lock
# --------------------------------------------------------------------------- #
def resolve_attempt(rundir: Path, unit: str, attempt_name: str, *, action: str,
                    reason: str, stopped_confirmed: bool,
                    grace: float = GRACE_DEFAULT) -> str:
    """The only way out of ``uncertain`` and ``orphan-claim``.

    ``--retry`` requires the attestation that the old supervisor and its worker are gone,
    because this program cannot establish it: the supervisor detaches its worker
    deliberately. Without the attestation the command refuses rather than starting a second
    worker beside a live one.

    ``--fail`` requires it too. Without the attestation the attempt's capacity reservation
    was kept, because execution may continue -- and nothing ever released it, which at one
    slot per lane stopped the lane for the rest of the run.
    """
    if action == "retry" and not stopped_confirmed:
        raise DriverError(
            "--retry needs --stopped-confirmed: the supervisor detaches its worker, so "
            "nothing here can establish that the old one has stopped. Confirm it yourself "
            "or use --fail")
    if action == "fail" and not stopped_confirmed:
        # The same rule as `--retry`, and for the same reason taken one step further. A
        # `--fail` without the attestation kept the capacity reservation on purpose, because
        # execution may continue -- and nothing ever released it. At one slot per lane,
        # that is the lane stopped for the rest of the run: every later
        # unit on it refused on every resume, and the only way out failing each of them by
        # hand and discarding its work. For a write-capable unit it was the run's one writer
        # lane and so every write-capable unit. Either way the operator has the same two
        # answers -- confirm the worker has stopped, or wait out its deadline -- and asking
        # for one of them here is what keeps the run from needing a third.
        raise DriverError(
            f"--fail on {unit} needs --stopped-confirmed: without it the attempt's capacity "
            "reservation is never released, and at one slot per lane that stops the lane "
            "for the rest of the run. Confirm the worker has stopped, or wait out its "
            "deadline; the run then continues past it")
    if not reason.strip():
        raise DriverError("--reason is required: a resolution is a durable record of a "
                          "judgment, and one with no reason records nothing")
    path = rundir / engine.DISPATCH_DIR / unit / attempt_name
    if not _is_directory(path):
        raise DriverError(f"{path} is not an attempt of {unit}")
    # Both records are written once and never edited, so both guards are asked with a
    # refusal available. Read as "no record is there", a refusal writes a second judgment
    # over the first — and with `--retry`, starts a worker beside the one the disposition
    # this could not read had already accounted for.
    if _present(path / DISPOSITION_NAME):
        raise DriverError(
            f"{unit} {attempt_name} is already adjudicated; a disposition is written once "
            f"and never edited")
    if _present(path / RESOLUTION_NAME):
        raise DriverError(f"{unit} {attempt_name} already carries a resolution")
    # **Only an attempt nobody can account for.** A resolution supersedes any status that
    # arrives later, so one written over an attempt that is still inside its own deadline
    # throws away the outcome that attempt is about to report — and, with `--retry`, starts
    # a second worker beside a live one. `--grace` is how an operator who knows better says
    # so, because it is the same number the run reads the state with.
    state = read_attempt(rundir, unit, path, grace=grace).state
    if state not in (UNCERTAIN, ORPHAN_CLAIM):
        raise DriverError(
            f"{unit} {attempt_name} is {state}, and resolution is for an attempt whose "
            f"outcome cannot be proven. Wait for its deadline and grace to pass, or pass a "
            f"shorter --grace if you know it has stopped")
    _write_json(path / RESOLUTION_NAME, {
        "action": action, "reason": reason.strip(),
        "stopped_confirmed": bool(stopped_confirmed),
        "at": time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime()),
    })
    _progress(rundir, f"{unit} {attempt_name} resolved by operator: {action}")
    return f"{unit} {attempt_name}: recorded an operator {action}"


def resolve_unit(rundir: Path, unit: str, *, grant: int | None,
                 fail: bool, reason: str, stopped_confirmed: bool = False,
                 grace: float = GRACE_DEFAULT) -> str:
    """The way out of the launch-limit quarantine.

    ``--grant-launches`` raises this unit's hard ceiling by a stated amount and is itself a
    durable record, so a replay reaches the same ceiling every time. ``--fail`` publishes
    the terminal error **without rewriting any historical disposition**: the record of what
    happened is never edited, only added to.

    ``--fail`` on a unit with an attempt nobody can account for needs the same attestation
    ``resolve-attempt`` needs, and for the same reason one level up: the unit's error is
    published either way, but an ``uncertain`` or ``orphan-claim`` attempt keeps its
    capacity reservation until something says its worker is gone, and a failed unit whose
    attempt still held its slot held it for the rest of the run. With the attestation those
    attempts are failed too, each with its own record, so the reservation ends.
    """
    base = rundir / engine.DISPATCH_DIR / unit
    if not _is_directory(base):
        raise DriverError(f"{base} is not a unit of this run")
    if not reason.strip():
        raise DriverError("--reason is required")
    if fail:
        # **Reconcile the pending publication BEFORE committing anything.** A unit whose
        # accepted result was published while its landing record was not looks unlanded, and
        # a decision committed on that reading is a durable intention nothing can carry out:
        # the result file is already there, publishing an error over it is refused, and
        # every later adoption picks the decision up and meets the same refusal — one unit
        # in that state stops the whole run and no command undoes it. So the order here is
        # the order this command keeps on its other path, and the order every terminal
        # outcome keeps: decide, then publish. **A command that refuses leaves nothing
        # behind.**
        if replay_publication(rundir, unit) or landed(rundir, unit) is not None:
            raise DriverError(f"{unit} has already landed its answer")
        target = unit_dir(rundir, unit)
        for name in (engine.RESULT_NAME, engine.ERROR_NAME):
            # Asked with a refusal available: read as "nothing is on disk", a terminal file
            # the host would not describe is one this command then publishes an error over,
            # and a unit carrying both a result and an error has no reading at all.
            if _present(target / name):
                # Reconciliation could not attribute it, so this rejects rather than
                # deciding over it: an answer nothing in the run accounts for is not the
                # operator's to overwrite from here.
                raise DriverError(
                    f"{target / name} is on disk and no disposition accounts for it, so "
                    f"this unit cannot be failed from here; move that file aside and run "
                    f"again, or leave it and let the run adopt it"
                )
        if unit_resolution(rundir, unit) is not None:
            raise DriverError(f"{unit} already carries an operator decision")
        # **An attempt nobody can account for outlives the unit's failure unless the
        # operator attests its worker is gone.** The capacity predicate reads the attempt,
        # never the unit: a unit-level decision says nothing about whether a detached worker
        # is still running, so releasing the slot on it would start a second worker beside
        # a live one. Asked here, before anything is written, with the same refusal
        # `resolve-attempt --fail` gives, because the operator has the same two answers.
        unproven = [attempt for attempt in read_attempts(rundir, unit, grace=grace)
                    if attempt.state in (UNCERTAIN, ORPHAN_CLAIM)]
        if unproven and not stopped_confirmed:
            names = ", ".join(attempt.name for attempt in unproven)
            raise DriverError(
                f"--fail on {unit} needs --stopped-confirmed: {names} cannot be accounted "
                f"for, and without the attestation its capacity reservation is never "
                f"released, which at one slot per lane stops the lane for the rest of "
                f"the run. Its deadline has already passed, so waiting releases nothing; "
                f"confirm the worker has stopped and pass --stopped-confirmed")
        # **The attempts' records before the unit's.** Each record on its own moves the unit
        # to the same end: an attested operator failure on an attempt is adjudicated on the
        # next adoption pass and publishes the unit's error, and the unit record below is
        # replayed the same way. Written in this order, a kill anywhere between leaves no
        # attempt still reserving its slot for a unit whose failure is already decided.
        for attempt in unproven:
            _write_json(attempt.path / RESOLUTION_NAME, {
                "action": "fail", "reason": reason.strip(), "stopped_confirmed": True,
                "at": time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime()),
            })
            _progress(rundir, f"{unit} {attempt.name} resolved by operator: fail, with "
                              f"the unit")
        # **The record first, the publication second.** Publishing straight from here left
        # an `error.txt` no adoption pass could attribute to anything: a kill before
        # `landed.json` and the unit is not terminal, while at the launch ceiling it stays
        # quarantined for ever and below it further work can still be authorized against the
        # operator's decision. Written once, the record is what makes the publication
        # replayable, so this call finishes it and a resumed run finishes it just the same.
        _write_json(base / UNIT_RESOLUTION_NAME, {
            "action": "fail", "reason": reason.strip(),
            "at": time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime()),
        })
        _progress(rundir, f"{unit} failed by operator: {reason.strip()[:60]}")
        publish(rundir, unit, None, "error",
                f"The operator failed this unit: {reason.strip()}\n")
        return f"{unit}: published the terminal error"
    if grant is None or grant <= 0:
        raise DriverError("--grant-launches needs a positive number of launches")
    # Named from the records that are there, and a listing that under-counts writes this
    # grant over one already on disk — which is an operator's raised ceiling silently gone.
    index = len(grant_files(base))
    _write_json(base / f"{GRANT_PREFIX}{index}{GRANT_SUFFIX}", {
        "launches": int(grant), "reason": reason.strip(),
        "at": time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime()),
    })
    _progress(rundir, f"{unit} granted {grant} more launch(es)")
    return f"{unit}: granted {grant} more launch(es)"


def status_report(run: Run) -> str:
    """What the run directory says, and nothing else. Read-only by construction."""
    lines = [f"run directory: {run.rundir}",
             f"stage: {_marker(run.rundir)}",
             f"budget spent: {run.spent() / 3600.0:.2f} h",
             # Named here as well as at startup: an operator reaching for `status` is
             # usually deciding whether to stop the run, and this is the path that does it.
             f"to stop a run in this directory: create {run.drain_flag}"]
    for account, state in sorted(run.providers().items()):
        if state.paused:
            lines.append(f"provider {account} is paused at generation {state.generation}: "
                         f"{state.paused}")
        if state.drain:
            lines.append(f"provider {account} stops this run: {state.drain}")
    protected: list[str] = []
    evidence: list[str] = []
    for unit in run.units():
        unit_id = unit["id"]
        attempts = run.attempts(unit_id)
        record = landed(run.rundir, unit_id)
        where = (f"landed {record['publication']} from {record['attempt']}" if record
                 else (run.quarantined(unit_id) or "open"))
        lines.append(f"  {unit_id} [{unit.get('kind')}/{unit.get('lane')}] {where}")
        for attempt in attempts:
            outcome = (attempt.disposition or {}).get("outcome", "")
            lines.append(f"    {attempt.name}: {attempt.state}"
                         + (f" ({outcome})" if outcome else ""))
            if record is not None and not _execution_ended(attempt):
                protected.append(f"{unit_id} {attempt.name}")
            token = (attempt.argv or {}).get("token")
            if (record is not None and record.get("attempt") == attempt.name
                    and isinstance(token, str) and token
                    and _is_directory(run.rundir / "work" / token)
                    and keeps_evidence(attempt)):
                evidence.append(f"{unit_id} {attempt.name} -> work/{token}")
    if protected:
        lines.append("protected working directories, kept because their execution cannot "
                     "be shown to have ended: " + ", ".join(protected))
    if evidence:
        lines.append("working copies kept because the verdict they landed names a run that "
                     "was executed: " + ", ".join(evidence))
    # What the retention cap removed. Read from the run's own record rather than derived,
    # because a deleted copy leaves nothing to derive it from — which is the whole reason
    # the record exists.
    record = _read_json(run.rundir / engine.DISPATCH_DIR / COPIES_RECORD_NAME)
    dropped = record.get("dropped") if isinstance(record, dict) else None
    for entry in dropped if isinstance(dropped, list) else []:
        if isinstance(entry, dict):
            lines.append(f"dropped over the retention cap: {entry.get('unit')} "
                         f"{entry.get('attempt')} ({entry.get('severity')}, "
                         f"{entry.get('bytes')} bytes) at {entry.get('at')}")
    short = run.headroom()
    if short:
        lines.append(f"no write-capable unit can be claimed: {short}")
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def default_supervisor() -> Path:
    """``review_runner.py``, in the sibling skill directory the pack installs beside this
    one. Overridable, because a host may install the two elsewhere."""
    return _HERE.parent / "diff-review" / "review_runner.py"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=_PROG,
        description="Drive one review-panel run: plan, dispatch, land, report.")
    sub = parser.add_subparsers(dest="command", required=True,
                                metavar="{run,status,resolve-attempt,resolve-unit}")
    run = sub.add_parser("run", help="plan or resume a run and carry it to a report")
    run.add_argument("--job", help="the job file to plan from; required for a new run")
    run.add_argument("--rundir", required=True, help="the run directory")
    run.add_argument("--adapter", required=True, help="the adapter config")
    run.add_argument("--go", action="store_true",
                     help="dispatch. Without it the run is planned, the preview printed, "
                          "and nothing is spawned. To stop a run that is going, create "
                          "the drain file this prints at startup — <rundir>.drain, for the "
                          "--rundir you gave and for its canonical spelling: it stops "
                          "claiming, lets what is running finish and exits resumable, on "
                          "every platform, and at any point in the run. SIGTERM does the "
                          "same where signals are delivered, but only once dispatch has "
                          "begun: during planning no handler is installed yet, so it "
                          "still ends the process. The file is the one that always works")
    run.add_argument("--grace", type=float, default=GRACE_DEFAULT,
                     help="seconds past an attempt's own deadline before its outcome is "
                          "called unprovable (default 120). It authorizes nothing")
    run.add_argument("--poll", type=float, default=2.0, help=argparse.SUPPRESS)
    run.add_argument("--max-hours", type=float, default=None,
                     help="stop claiming once this much driver uptime has accumulated "
                          "across restarts")
    run.add_argument("--extend", type=float, default=None,
                     help="let the run use this many hours beyond --max-hours. Absolute, "
                          "not added per invocation: the same command run again grants "
                          "nothing more, and asking for more is a larger number")
    run.add_argument("--supervisor", default=None,
                     help="path to review_runner.py (default: the sibling skill's)")
    run.add_argument("--probe-backoff", type=float, default=PROBE_BACKOFF_DEFAULT,
                     help="seconds between probes of a paused provider (default 60). One "
                          "probe at a time, and three that answer nothing end the run")
    run.add_argument("--max-capture-bytes", type=int, default=MAX_CAPTURE_BYTES_DEFAULT,
                     help="what one attempt may make the supervisor hold, per retained "
                          "representation. A reply larger than this loses its front, and a "
                          "reply whose closing object itself was cut is infrastructure "
                          "rather than the unit's answer")
    run.add_argument("--keep-repro-bytes", type=int, default=KEEP_REPRO_BYTES_DEFAULT,
                     help="how much of the working copies whose verdicts name a run that "
                          "was executed to keep (default 4 GiB; 0 keeps them all). Over "
                          "it, copies are dropped highest-severity-last and every one is "
                          "named in the run's record. A copy whose worker cannot be shown "
                          "to have stopped is never dropped and never counted")
    run.add_argument("--disk-floor-bytes", type=int, default=DISK_FLOOR_DEFAULT,
                     help="free space held back on the run's volume (default 256 MiB). "
                          "Below it no write-capable unit is claimed; none is failed")
    run.add_argument("--build-margin-bytes", type=int, default=BUILD_MARGIN_DEFAULT,
                     help="room reserved for what a build puts in a working copy, beyond "
                          "the copy itself (default 256 MiB)")
    status = sub.add_parser("status", help="print what the run directory says; writes "
                                           "nothing")
    status.add_argument("rundir")
    status.add_argument("--grace", type=float, default=GRACE_DEFAULT)
    status.add_argument("--adapter", default=None, help=argparse.SUPPRESS)
    ra = sub.add_parser("resolve-attempt",
                        help="record an operator's judgment on one attempt")
    ra.add_argument("rundir")
    ra.add_argument("unit")
    ra.add_argument("attempt")
    group = ra.add_mutually_exclusive_group(required=True)
    group.add_argument("--retry", action="store_true")
    group.add_argument("--fail", action="store_true")
    ra.add_argument("--reason", required=True)
    ra.add_argument("--grace", type=float, default=GRACE_DEFAULT,
                    help="the same grace the run reads attempt states with (default 120)")
    ra.add_argument("--stopped-confirmed", action="store_true",
                    help="your attestation that the old supervisor and its worker are "
                         "gone. --retry requires it")
    ru = sub.add_parser("resolve-unit", help="grant a unit more launches, or fail it")
    ru.add_argument("rundir")
    ru.add_argument("unit")
    # Mutually exclusive and required, so a command asking for both a grant and a failure is
    # a usage error rather than a silent choice between them.
    what = ru.add_mutually_exclusive_group(required=True)
    what.add_argument("--grant-launches", type=int, default=None)
    what.add_argument("--fail", action="store_true")
    ru.add_argument("--reason", required=True)
    ru.add_argument("--grace", type=float, default=GRACE_DEFAULT,
                    help="the same grace the run reads attempt states with (default 120)")
    ru.add_argument("--stopped-confirmed", action="store_true",
                    help="your attestation that the old supervisor and its worker are "
                         "gone. --fail requires it while the unit has an attempt nobody "
                         "can account for, and then fails that attempt too")
    return parser


_SUBCOMMANDS = ("run", "status", "resolve-attempt", "resolve-unit")


def _with_default_subcommand(argv: Sequence[str]) -> list[str]:
    """Let ``--job ... --rundir ... --adapter ... --go`` mean ``run``.

    The operator surface is four verbs, and ``run`` is the one nobody should have to type:
    the whole point of this program is that one command does a review. A leading flag is
    unambiguous — no verb starts with a dash — so the verb is filled in rather than
    demanded.
    """
    argv = list(argv)
    if argv and argv[0] not in _SUBCOMMANDS and argv[0] not in ("-h", "--help"):
        return ["run", *argv]
    return argv


def main(argv: Sequence[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, TypeError, ValueError, OSError):
            pass
    engine.require_python(3, 10)
    args = build_parser().parse_args(
        _with_default_subcommand(sys.argv[1:] if argv is None else argv))
    try:
        return _dispatch(args)
    except RunPaused as exc:
        # **Not a refusal, and the exit code is how a caller tells them apart.** A refusal
        # says this run cannot be continued; this says the host would not do something and
        # nothing was decided from it. Caught here as well as in the loop, because the same
        # faults reach bootstrap, `status` and the operator commands, which have no loop.
        sys.stderr.write(f"{_PROG}: {exc}\n"
                         f"{_PROG}: nothing was decided from it; put right what the host "
                         f"refused and run the same command again\n")
        return EXIT_STOPPED
    except DriverError as exc:
        sys.stderr.write(f"{_PROG}: {exc}\n")
        return EXIT_REFUSED
    except engine.ReviewPanelError as exc:
        sys.stderr.write(f"{_PROG}: {exc}\n")
        return EXIT_REFUSED


def _dispatch(args: argparse.Namespace) -> int:
    # Resolved, not merely made absolute: the lock's name is derived from this, and two
    # spellings of one directory must not yield two locks. See :func:`_canonical`.
    rundir = _canonical(Path(args.rundir), "run directory")
    if args.command == "run":
        # The lock is the run directory's SIBLING, so it needs the parent to exist -- and
        # bootstrap, which makes the run directory, runs under the lock. A fresh
        # `--rundir reviews/run1` with no `reviews/` yet was refused at "cannot open the run
        # lock" before anything could create it. Made here, for `run` only: `status` and the
        # resolve commands describe a run that exists, and should not leave a directory
        # behind when it does not.
        try:
            rundir.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise DriverError(f"cannot create {rundir.parent} for the run directory: {exc}") from exc
    lock = RunLock(rundir.with_name(rundir.name + LOCK_SUFFIX))
    if args.command == "run":
        return _command_run(args, rundir, lock)
    if args.command == "status":
        # **`status` does not take the lock, and that is deliberate.** It writes nothing, and
        # the moment an operator most wants it is while a run is going — which is exactly
        # when the lock is held. A status that refused then would answer only about runs
        # nobody is working on. What it costs is that a long listing can be read while the
        # driver is writing into it, so a line or two may be a moment stale; every other
        # command here takes the lock, because every other command writes.
        run = Run(rundir, load_adapter_config(args.adapter) if args.adapter
                  else _placeholder_lanes(), default_supervisor(), grace=args.grace,
                  given=Path(args.rundir).absolute())
        sys.stdout.write(status_report(run))
        return EXIT_OK
    with lock:
        if args.command == "resolve-attempt":
            sys.stdout.write(resolve_attempt(
                rundir, args.unit, args.attempt,
                action="retry" if args.retry else "fail", reason=args.reason,
                stopped_confirmed=args.stopped_confirmed, grace=args.grace) + "\n")
            return EXIT_OK
        sys.stdout.write(resolve_unit(rundir, args.unit, grant=args.grant_launches,
                                      fail=args.fail, reason=args.reason,
                                      stopped_confirmed=args.stopped_confirmed,
                                      grace=args.grace) + "\n")
        return EXIT_OK


def _placeholder_lanes() -> dict[str, LaneSpec]:
    """A configuration `status` can be constructed with. It reads the run directory and
    spawns nothing, so the commands it would have rendered are not its business."""
    mode = ModeSpec(command=("⟪prompt⟫",), permission="not read here",
                    result_mode="stream-transcript", idle=900.0, deadline=3600.0)
    return {lane: LaneSpec(runtime="", model="", account="", adapter="",
                           modes={READ_ONLY: mode, WRITE_CAPABLE: mode})
            for lane in engine.LANES}


def _command_run(args: argparse.Namespace, rundir: Path, lock: RunLock) -> int:
    lanes = load_adapter_config(args.adapter)
    # Resolved before it is checked, because it is checked HERE and run THERE: every
    # supervisor is launched with the run directory as its working directory, so a relative
    # spelling that exists beside the caller names nothing beside the run. The attempts then
    # produce no status at all and drift to `uncertain` while holding their lanes.
    supervisor = _canonical(Path(args.supervisor) if args.supervisor
                            else default_supervisor(), "supervisor")
    # A refusal here is not an absence, and the difference is what the operator is told:
    # "install it beside this skill" sends somebody to reinstall a file that is already
    # there and that this account cannot read.
    info = _stat_or_absent(supervisor)
    if info is None:
        raise DriverError(
            f"the supervisor is not at {supervisor}; install it beside this skill -- it is the "
            f"diff-review skill's review_runner.py, and this one needs both -- or pass "
            f"--supervisor")
    if not stat.S_ISREG(info.st_mode):
        raise DriverError(
            f"{supervisor} is not a file, so it is not the supervisor this run launches "
            f"every attempt with; pass --supervisor")
    if args.max_capture_bytes <= 0:
        raise DriverError("--max-capture-bytes must be a positive number of bytes; a cap "
                          "of zero bounds every reply to nothing")
    if args.probe_backoff < 0:
        raise DriverError("--probe-backoff cannot be negative")
    for flag, value in (("--keep-repro-bytes", args.keep_repro_bytes),
                        ("--disk-floor-bytes", args.disk_floor_bytes),
                        ("--build-margin-bytes", args.build_margin_bytes)):
        if value < 0:
            raise DriverError(f"{flag} cannot be negative")
    job = Path(args.job).absolute() if args.job else None
    # **The lock first, before anything is inspected.** Taking it after planning leaves
    # bootstrap unowned, and two drivers could then both see one `.partial` directory with
    # one of them deleting the other's live planning.
    given = Path(args.rundir).absolute()
    with lock:
        # **The first thing done under ownership, before any lengthy work.** A stale request
        # belongs to a run that is over; every request after this line belongs to this one.
        # Cleared any later, planning and its snapshot copy sit inside the window, and an
        # operator asking this driver to stop has the request they just made deleted and the
        # work dispatched anyway.
        clear_drain_request(rundir, given)
        # And said out loud, before it matters, because the path is not always the one an
        # operator would guess: `--rundir` may be an alias and ownership is canonical.
        if args.go:
            sys.stdout.write(f"To stop this run: create {drain_flags(rundir)[0]}\n")
            # Flushed, and this is the whole reason the line exists. stdout is
            # block-buffered whenever it is a pipe or a file, which is how a run of hours
            # is actually started; planning then runs under `redirect_stdout`, which swaps
            # the stream without flushing the real one, so the first thing to reach the
            # operator would be the round line AFTER planning. The window this names is the
            # one during planning. A path delivered after it is a path delivered too late.
            sys.stdout.flush()
        bootstrap(rundir, job)
        # Pinned at plan time and refused on a resume with a different one: a run whose
        # lanes changed part-way cannot be described truthfully by one record.
        _pin_adapter(rundir, lanes)
        if not args.go:
            sys.stdout.write(_preview(rundir, lanes))
            return EXIT_OK
        # A run started with `--go` shows no preview, and this is the one line of it that
        # is about what leaves the machine rather than how long the run takes.
        sys.stdout.write(_copied_outside_scope(rundir))
        sys.stdout.flush()
        run = Run(rundir, lanes, supervisor, grace=args.grace,
                  poll=args.poll, max_hours=args.max_hours, given=given,
                  probe_backoff=args.probe_backoff,
                  max_capture_bytes=args.max_capture_bytes,
                  keep_repro_bytes=args.keep_repro_bytes,
                  disk_floor=args.disk_floor_bytes,
                  build_margin=args.build_margin_bytes)
        # `is not None`, not truth: `--extend 0` is how an operator takes an earlier grant
        # back, and read as "not given" it was silently ignored. A negative number is
        # refused rather than clamped, because clamped to zero it revoked a grant nobody
        # meant to revoke.
        if args.extend is not None and args.extend < 0:
            raise DriverError("--extend must be zero or more hours; zero takes an earlier "
                              "grant back")
        if args.extend is not None:
            run.extend(args.extend)
        previous = _arm_drain(run)
        try:
            return run.loop()
        finally:
            run.save_budget(force=True)
            # Put the handlers back. Called in-process — which is how the suite calls it —
            # this would otherwise leave the caller's own interrupt handling replaced by
            # one that writes into a run that has finished.
            for signum, handler in previous:
                with contextlib.suppress(ValueError, OSError, TypeError):
                    signal.signal(signum, handler)


def _pin_adapter(rundir: Path, lanes: dict[str, LaneSpec]) -> None:
    """Record the configuration this run is planned against, and refuse a resume with a
    different one. A run whose lanes changed half way through has no honest provenance:
    the report would name one adapter for work two of them did."""
    fingerprint = {lane: {"runtime": spec.runtime, "model": spec.model,
                          "account": spec.account, "adapter": spec.adapter}
                   for lane, spec in lanes.items()}
    path = rundir / "adapter-pin.json"
    existing = _read_json(path)
    if existing is None:
        _write_json(path, fingerprint)
        return
    if existing != fingerprint:
        raise DriverError(
            f"{path} pins a different adapter configuration than the one given; a run "
            f"cannot change the lanes it is described by half way through")


def _copied_outside_scope(rundir: Path) -> str:
    """The line saying how many lockfiles outside the review the snapshot carries for the
    build, or nothing where it carries none."""
    inventory = _read_json(rundir / INVENTORY_NAME)
    context = inventory.get("context") if isinstance(inventory, dict) else None
    if not isinstance(context, list) or not context:
        return ""
    line = engine.copied_outside_scope_line(
        [e["path"] for e in context if isinstance(e, dict) and isinstance(e.get("path"), str)])
    return line + "\n" if line else ""


def _preview(rundir: Path, lanes: dict[str, LaneSpec]) -> str:
    """What ``--go`` would start, and **how it would be spread**. A unit count alone cannot
    say how long a run will take, so the shape of the run is printed per lane: how many
    units, how many at once, and which ones queue behind each other run-wide."""
    units = _units(rundir)
    kinds: dict[str, int] = {}
    for unit in units:
        kinds[unit.get("kind", "?")] = kinds.get(unit.get("kind", "?"), 0) + 1
    lines = [f"{engine.RUNDIR_LINE}{rundir}", "",
             f"stage: {_marker(rundir)}",
             f"units: {len(units)}"]
    copied = _copied_outside_scope(rundir)
    if copied:
        lines.append(copied.rstrip("\n"))
    for kind in sorted(kinds):
        lines.append(f"  {kind}: {kinds[kind]}")
    lines.append("")
    for lane, spec in lanes.items():
        mine = [u for u in units if u.get("lane", engine.LANES[0]) == lane]
        shared = sum(1 for u in mine if u.get("kind") in WRITE_CAPABLE_KINDS)
        free = len(mine) - shared
        lines.append(f"lane {lane}: {spec.slots} slot(s)"
                     f"{'' if spec.slots_stated else ' (the default; set slots to change it)'}; "
                     f"{free} unit(s) run up to "
                     f"{spec.slots} at a time, in about {-(-free // spec.slots)} "
                     f"wave(s)" + (f"; {shared} write-capable unit(s) run one at a time"
                                   if shared else ""))
    lines.append(f"Write-capable units ({', '.join(sorted(WRITE_CAPABLE_KINDS))}) run one at "
                 f"a time across both lanes, whatever the slots say: they build and run the "
                 f"tree, and separate copies still share ports, caches and credentials.")
    lines.append("")
    lines.append("Nothing was dispatched: pass --go to run it.")
    return "\n".join(lines) + "\n"


def _arm_drain(run: Run) -> list[tuple[int, object]]:
    """``SIGTERM`` stops all claiming run-wide, lets running attempts finish, and exits
    resumable. Drain is run-wide rather than per round, and it never rolls into the next
    round: a signal asking a run to stop must not be answered by starting more work.

    **This is the POSIX half of the drain, and it is only half.** A handler installed where
    signals are not delivered is a handler that never runs, so the request that works on
    every platform is the file :data:`DRAIN_SUFFIX` names — see `Run.stop_requested`. Both
    reach the same flag and mean the same thing.

    Returns what was installed before, so an in-process caller gets its own handling back.
    """
    def _drain(_signum, _frame):
        run.draining = True
        run.say("draining: no new attempts will be claimed")
    previous: list[tuple[int, object]] = []
    for name in ("SIGTERM", "SIGINT"):
        signum = getattr(signal, name, None)
        if signum is not None:
            try:
                previous.append((signum, signal.signal(signum, _drain)))
            except (ValueError, OSError):
                pass
    return previous


if __name__ == "__main__":
    sys.exit(main())
