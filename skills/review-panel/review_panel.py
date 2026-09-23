#!/usr/bin/env python3
"""review-panel engine — stdlib-only, cross-platform.

Five subcommands, each a stage of a blind multi-agent correctness sweep:

    plan       <job> --rundir <dir>  -> areas, closure proof, reading payloads
    route      <rundir>              -> candidate findings, verification payloads
    cluster    <rundir>              -> one clustering payload per area, after the verdicts
    synthesize <rundir>              -> one payload judging every defect, after the grouping
    report     <rundir>              -> the report

``synthesize`` is OPTIONAL: ``report`` runs whether or not it happened, and reads its result
where it did. Every other stage is required, and each refuses by name rather than assuming
what the one before it should have written.

**``units.json``'s ``stage`` is the run's only commit record.** Every stage writes its
outputs first and replaces ``units.json`` last — ``reading`` to ``verification`` to
``clustered`` to ``synthesized`` — so the marker moves only once the work behind it is on
disk. Each stage keys its refusal to that marker and to nothing else: a stage entered while
the marker still names the one before it takes back its own partial outputs, by a manifest
it enumerates, and runs again, because nothing it left was ever committed. A stage whose
marker has moved refuses by name and writes nothing.

**``report`` is the exception and writes no marker at all.** Its completion lives in its own
three files and the stamp beside them: all three present with the stamp is a report that
finished, fewer with the stamp is a publication interrupted part-way, and any of them with
no stamp is a report published under rules that kept no such record. A marker in
``units.json`` would be a completion recorded in a file four stages write, so any of them
could undo it — a synthesis that read the run before the report ran commits its own marker
afterwards, and the published report loses the only thing protecting it from a plain second
report. Making that safe needs one owner across stages, which a single stage invocation is
not; what one stage alone writes needs no such owner.

**The marker is not exclusion, so each stage claims the run directory as well.** Two stages
can read one marker and both decide the leftovers are theirs, and the second then deletes
what the first committed — so a stage creates ``<stage>.lock`` exclusively before it reads
the marker and holds it through the marker write. What a reclaim may take is narrow on
purpose: the paths that stage writes, and a directory only when everything in it is
something that stage writes. Anything else is refused by name, because a refusal costs a
minute and a recursive delete costs a file.

**A stage that is KILLED leaves its claim behind, and the next run of that stage refuses
until somebody removes it.** That is the cost of exclusion a single command can hold: a
stage that took back a claim it found would be two stages deleting each other's work again,
and exclusion the kernel drops on death has to be held open by a process that owns the run
from end to end, which one stage invocation is not. Everything else an interrupted stage
leaves — its outputs, its scratch, its unit directories — is taken back without help.

The engine never spawns anything: dispatch belongs to the runtime driving the sweep, which
reads each unit's ``payload.md`` and writes ``result.json`` beside it. That is what keeps
this module small and runtime-neutral by construction.

Design rules:
  * Standard library ONLY. Python 3.10+.
  * No branded CLI name anywhere in this file.
  * ``encoding="utf-8"`` pinned on every read and write; ``pathlib`` for paths.

Built so far: ``plan`` and ``route`` are complete. ``plan`` loads the job strictly, checks
the run directory, enumerates and measures the tree, partitions it into areas under a
closure proof, writes the snapshot (a file copy, never a worktree), ``inventory.json`` and
``areas.json``, then one unit directory per reader and per auditor under ``units/`` —
``payload.md`` and ``schema.json`` — listed in ``units.json``, and prints the preview.
``route`` reads every reading unit's result strictly, records each unit as complete,
failed or missing, turns every finding into its own candidate with its one raiser, routes
each candidate to exactly one verification unit addressed away from its finder, and
writes ``candidates.json`` plus the verification units. ``cluster`` reads every verification
result and writes one clustering unit per area that raised anything, asking which of that
area's candidates are the same defect. ``report`` reads every verification and clustering
result strictly, gives every candidate exactly one status and puts it in exactly one
cluster — proving the clusterer's grouping is a partition first, and falling back to one
candidate per cluster for an area whose unit it cannot believe — reads the rung and the
adapter per lane from the ``dispatch.json`` the dispatcher wrote, and writes three files:
``findings.json``, ``report.md``, and ``report.html`` converted from it. The document
separates established defects from unresolved ones and groups each by the tiers the
synthesis round named where that round returned an answer, most severe and cheapest-fix
first inside a tier; then the units that failed or returned nothing by area with their
files, and every path nobody read.
"""

from __future__ import annotations

import argparse
import contextlib
import errno
import hashlib
import tempfile
from datetime import datetime, timezone
import json
import os
import re
import shlex
import shutil
import stat
import subprocess
import sys
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Callable, Iterable, Sequence

_PROG = "review_panel.py"

# What `plan` prints in front of the directory it used, so a caller reading stdout can take
# the path to the stage after it. Spelled once, because the skill quotes it and a test
# matches it.
RUNDIR_LINE = "Run directory: "


class ReviewPanelError(Exception):
    """Base class for every error the engine raises deliberately."""


class JobError(ReviewPanelError):
    """The job file is malformed: a missing field, an unknown key, a bad path. Names the
    field, never the line — the job is data, and the reader fixes a field."""


class RunDirError(ReviewPanelError):
    """The run directory cannot be used: inside the audited root or a git repository, or
    not empty."""


class StorageStop(RunDirError):
    """The volume under the run directory failed a read or a write.

    **Its own class because every tolerant handler in this file has to let it past.** Those
    handlers exist so that one bad unit costs one unit rather than the whole stage, and that
    reasoning holds only while the fault says something about the unit it was met on. A
    device error says nothing about it, will meet the next unit the same way, and recorded
    as that unit's failure it publishes "nothing was found here" over an answer that is
    sitting on disk intact.
    """


def require_python(min_major: int = 3, min_minor: int = 10, *,
                   current: tuple[int, ...] | None = None) -> None:
    """Fail loud (clear message + non-zero exit) on a too-old interpreter.

    Without this guard an older interpreter raises an opaque traceback deep in a later
    stdlib call. ``current`` overrides the detected version so both branches are testable.
    """
    if current is None:
        current = sys.version_info[:2]
    if tuple(current[:2]) < (min_major, min_minor):
        found = ".".join(str(part) for part in current[:2])
        sys.stderr.write(
            f"Python {min_major}.{min_minor}+ required (found {found}). "
            f"Install a newer interpreter and re-run.\n"
        )
        raise SystemExit(1)


# --------------------------------------------------------------------------- #
# job — strict hand-parsing of job.json
# --------------------------------------------------------------------------- #
# There is no JSON Schema validator in the stdlib and no runtime flag ever sees a file an
# agent wrote, so the job is parsed by hand, strictly: an unknown key or a missing field
# fails BY NAME, and nothing is defaulted here. Defaults live in the prose layer, which
# says when it applied one; a default applied silently in the engine would make a job file
# that does not say what ran.
PARTITION_MODES = frozenset({"file", "subject"})
MIN_LENSES = 2
# Paths and directory prefixes, never shell globs: glob semantics differ across shells and
# platforms, and a `[` that is a character class on one host is a literal on another.
GLOB_CHARS = "*?["
PREFIX_MARK = "/"

_JOB_REQUIRED_KEYS = ("problem", "root", "exclude", "partition", "lenses")
# Optional, and absent-is-allowed rather than absent-is-filled: a job with no `files`
# reviews the whole tree under root, which is a different job from one that lists every
# file in it, and the record has to be able to say which was asked for.
# `coverage` is the one question a job can take off the table. A security review wants
# defects and not 348 missing-test entries beside them, and no tuning of where an auditor
# goes gets that to zero for a job that never wanted the question asked. Absent means the
# engine decides, as it always has.
# `questions` is what the owner wants answered beyond the defects: free text, carried as
# written, and never put in a payload. The readers answer the problem statement with
# findings; the operator answers the questions afterwards, in `report-notes.json`.
_JOB_OPTIONAL_KEYS = ("files", "coverage", "questions")
_JOB_KEYS_FILE = frozenset((*_JOB_REQUIRED_KEYS, *_JOB_OPTIONAL_KEYS))
_JOB_KEYS_SUBJECT = frozenset((*_JOB_REQUIRED_KEYS, *_JOB_OPTIONAL_KEYS, "areas"))
_AREA_REQUIRED_KEYS = ("name", "paths")
# The three optional keys are absent-is-allowed, never absent-is-filled: present with the
# wrong shape they are errors, absent they leave the dataclass field at its "not given"
# value, which for `lenses` is None so a later stage can tell inherited from declared.
_AREA_KNOWN_KEYS = frozenset({"name", "paths", "also_read", "remainder", "lenses"})


@dataclass(frozen=True)
class Area:
    """One declared subject area.

    ``paths`` and ``also_read`` hold normalized entries; an entry ending in ``/`` is a
    directory prefix and claims everything under it, any other entry names one file.
    ``lenses`` is ``None`` when the area declares none and so reads under the job's.
    """

    name: str
    paths: tuple[str, ...]
    also_read: tuple[str, ...] = ()
    remainder: bool = False
    lenses: tuple[str, ...] | None = None


@dataclass(frozen=True)
class Job:
    """A loaded job. ``root`` is absolute as given and resolved; every other path is
    relative to it, forward-slashed, with a trailing ``/`` marking a directory prefix.

    ``files``, when non-empty, is the explicit list of files to review in place of
    everything under root, so a subset of a large repository needs no copy of itself in a
    mirror directory. Every entry names one file: a directory prefix is refused, because
    a list that can claim a whole directory is the enumeration it replaces."""

    path: Path
    problem: str
    root: Path
    exclude: tuple[str, ...]
    partition: str
    lenses: tuple[str, ...]
    areas: tuple[Area, ...] = ()
    files: tuple[str, ...] = ()
    # ``None`` where the job said nothing, which is not the same as ``True``: the record
    # has to be able to say the round was decided rather than asked for.
    coverage: bool | None = None
    # ``None`` where the job asked none. Never handed to a unit: see :data:`_JOB_OPTIONAL_KEYS`.
    questions: str | None = None


def is_prefix(entry: str) -> bool:
    """True when a normalized entry names a directory prefix rather than one file."""
    return entry.endswith(PREFIX_MARK)


def _is_drive_component(part: str) -> bool:
    """``C:`` or ``C:x`` — a Windows drive, absolute or drive-relative; neither is under root."""
    return len(part) >= 2 and part[1] == ":" and part[0].isalpha()


def _resolve(path: Path, what: str, error: type[ReviewPanelError]) -> Path:
    """``Path.resolve`` that fails as the engine's own error, naming the path and the cause.

    A symlink loop raises RuntimeError before Python 3.13 and OSError under strict mode
    after it; an unreadable parent raises PermissionError on any version; an embedded NUL
    byte raises ValueError. Left uncaught, each escapes ``plan`` as a traceback with exit 1
    instead of the exit-2 refusal the caller is told to expect.
    """
    try:
        return path.resolve()
    except (OSError, RuntimeError, ValueError) as exc:
        raise error(f"cannot resolve {what} {path}: {exc}") from exc


def _lstat_or_absent(path: Path, what: str,
                     error: type[ReviewPanelError]) -> os.stat_result | None:
    """What ``path`` itself is, ``None`` where it is genuinely not there, and this engine's
    refusal where the host would not say.

    **This is what the guards use instead of ``Path.exists`` and its siblings.** Each of
    those answers a question the host may refuse and turns the refusal into a plain
    ``False``. Which refusals — a symlink loop, a drive that is not ready, a name the
    platform cannot use, and on the newer interpreters every ``OSError`` there is — has
    moved between releases, and none of them has ever told a refusal from an absence: the
    answer is the same False a path that is simply not there gets. A guard reading the two as one decides "nothing is in the way" for a path
    nobody can look at, and then writes there. The link is never followed, because what is
    AT a path is what a guard on that path is about.
    """
    try:
        return os.lstat(path)
    except (FileNotFoundError, NotADirectoryError):
        return None
    except OSError as exc:
        raise error(f"cannot examine {what} {path}: {_os_fault(exc)}") from exc


def normalize_path(raw: object, field: str) -> str:
    """Normalize one job path: forward slashes, relative to root, no ``..``, no glob.

    Windows separators are accepted on the way in and never written back out, so a job
    authored on one host reads identically on another. A trailing separator survives as
    the prefix marker. The root itself (``.``, ``./``, an empty string) is refused: an
    exclusion of the root would exclude everything, and an area of the root is file mode.
    """
    if not isinstance(raw, str):
        raise JobError(f"field '{field}' must be a string path (found {type(raw).__name__})")
    for char in GLOB_CHARS:
        if char in raw:
            raise JobError(
                f"field '{field}' contains glob character {char!r} in {raw!r}: the job "
                f"takes paths and directory prefixes, never globs"
            )
    text = raw.replace("\\", "/")
    parts = [part for part in text.split("/") if part not in ("", ".")]
    # The drive test runs on the first component that SURVIVES normalization, not on the
    # raw text: `.` components are dropped just above, so `./C:/x` tested raw looks
    # relative and comes out `C:/x`, absolute on Windows. A leading `/` is a property of
    # the raw text, since normalization drops the empty component it produces.
    if text.startswith("/") or (parts and _is_drive_component(parts[0])):
        raise JobError(
            f"field '{field}' is absolute ({raw!r}); every path is relative to 'root'"
        )
    if ".." in parts:
        raise JobError(
            f"field '{field}' climbs out of 'root' with '..' ({raw!r}); every path lies "
            f"inside the root"
        )
    if not parts:
        raise JobError(
            f"field '{field}' names the root itself ({raw!r}); name a file or a directory "
            f"under it"
        )
    normalized = "/".join(parts)
    if text.endswith("/"):
        normalized += PREFIX_MARK
    return normalized


def _require_object(raw: object, field: str) -> dict:
    if not isinstance(raw, dict):
        raise JobError(f"'{field}' must be a JSON object (found {type(raw).__name__})")
    return raw


def _check_keys(obj: dict, field: str, known: frozenset[str], required: Sequence[str],
                error: type[ReviewPanelError] = JobError) -> None:
    """The strict-object rule the job and every result share: an unknown key or a missing
    field fails by name, as ``error`` — the job's or the result's, since the reader of the
    message fixes a different file in each case."""
    unknown = sorted(set(obj) - known)
    if unknown:
        listed = ", ".join(f"'{key}'" for key in unknown)
        raise error(
            f"unknown key {listed} in '{field}' (allowed: {', '.join(sorted(known))})"
        )
    for key in required:
        if key not in obj:
            raise error(f"'{field}' is missing required field '{key}'")


def _parse_text(raw: object, field: str, error: type[ReviewPanelError] = JobError) -> str:
    if not isinstance(raw, str) or not raw.strip():
        raise error(f"field '{field}' must be non-empty text")
    # Checked here, where the field has a name: an escaped lone surrogate otherwise passes
    # and crashes the first write that carries it.
    return _utf8(raw, field, error)


def _parse_paths(raw: object, field: str) -> tuple[str, ...]:
    if not isinstance(raw, list):
        raise JobError(f"field '{field}' must be a list of paths (found {type(raw).__name__})")
    return tuple(normalize_path(item, f"{field}[{i}]") for i, item in enumerate(raw))


def _parse_lenses(raw: object, field: str) -> tuple[str, ...]:
    if not isinstance(raw, list):
        raise JobError(f"field '{field}' must be a list of lens names (found {type(raw).__name__})")
    for i, item in enumerate(raw):
        _parse_text(item, f"{field}[{i}]")
    if len(raw) < MIN_LENSES:
        raise JobError(
            f"field '{field}' must list at least {MIN_LENSES} lenses, one per reader "
            f"(found {len(raw)})"
        )
    return tuple(raw)


def _parse_area(raw: object, field: str) -> Area:
    obj = _require_object(raw, field)
    _check_keys(obj, field, _AREA_KNOWN_KEYS, _AREA_REQUIRED_KEYS)
    name = _parse_text(obj["name"], f"{field}.name")
    paths = _parse_paths(obj["paths"], f"{field}.paths")
    also_read = _parse_paths(obj["also_read"], f"{field}.also_read") if "also_read" in obj else ()
    remainder = False
    if "remainder" in obj:
        if not isinstance(obj["remainder"], bool):
            raise JobError(f"field '{field}.remainder' must be true or false")
        remainder = obj["remainder"]
    lenses = _parse_lenses(obj["lenses"], f"{field}.lenses") if "lenses" in obj else None
    if not paths and not remainder:
        raise JobError(
            f"field '{field}.paths' is empty and the area is not the remainder: a subject "
            f"with no files is a concern mis-stated as a subject; state it as a lens"
        )
    return Area(name=name, paths=paths, also_read=also_read, remainder=remainder, lenses=lenses)


def _check_areas_consistent(areas: Sequence[Area]) -> None:
    """The cross-area rules: one remainder at most, unique names, no path claimed twice."""
    remainders = [f"areas[{i}]" for i, area in enumerate(areas) if area.remainder]
    if len(remainders) > 1:
        raise JobError(
            f"only one area may be the remainder; marked on {', '.join(remainders)}"
        )
    seen_names: dict[str, str] = {}
    for i, area in enumerate(areas):
        key = area.name.casefold()
        if key in seen_names:
            raise JobError(
                f"field 'areas[{i}].name' {area.name!r} repeats {seen_names[key]!r}; area "
                f"names must be distinct, and distinct on a case-insensitive host"
            )
        seen_names[key] = area.name
    claimed: dict[str, str] = {}
    for i, area in enumerate(areas):
        for j, entry in enumerate(area.paths):
            field = f"areas[{i}].paths[{j}]"
            if entry in claimed:
                raise JobError(
                    f"path {entry!r} is declared by both '{claimed[entry]}' and '{field}'; "
                    f"an area claims a path once, and a path read from beside another "
                    f"area belongs in 'also_read'"
                )
            claimed[entry] = field


def _check_paths_consistent(entries: Sequence[tuple[str, str]]) -> None:
    """Rules over every path in the job at once, wherever it was declared.

    ``entries`` pairs each normalized path with the field it came from. Two rules:
    a name that appears both as a file and as a prefix is an ambiguity, not a guess; and
    two names that differ only by case cannot both exist on a case-insensitive host, so
    the job cannot mean both.
    """
    by_stem: dict[str, list[tuple[str, str]]] = {}
    for entry, field in entries:
        by_stem.setdefault(entry.rstrip(PREFIX_MARK), []).append((entry, field))
    for stem, group in by_stem.items():
        forms = {entry for entry, _ in group}
        if len(forms) > 1:
            where = ", ".join(f"'{field}'" for _, field in group)
            raise JobError(
                f"{stem!r} is declared both as a file and as a prefix {stem + PREFIX_MARK!r} "
                f"({where}); a path is one or the other"
            )
    by_fold: dict[str, dict[str, str]] = {}
    for entry, field in entries:
        stem = entry.rstrip(PREFIX_MARK)
        by_fold.setdefault(stem.casefold(), {}).setdefault(entry, field)
    for variants in by_fold.values():
        if len(variants) > 1:
            listed = ", ".join(f"{entry!r} ('{field}')" for entry, field in variants.items())
            raise JobError(
                f"paths differ only by case: {listed}; a case-insensitive host cannot "
                f"hold both"
            )


def _parse_file_list(obj: dict) -> tuple[str, ...]:
    """Parse the optional ``files`` list: the same normalizer every other job path goes
    through, so a drive-shaped first component is refused on every host exactly as it is
    in ``exclude``, and then one rule of its own — each entry names a file.

    An empty list is refused rather than read as the whole tree: the two are different
    jobs, and a job file says what ran.
    """
    files = _parse_paths(obj["files"], "files")
    if not files:
        raise JobError(
            "field 'files' is present and empty; omit it to review everything under "
            "'root', or list the files to review"
        )
    for i, entry in enumerate(files):
        if is_prefix(entry):
            raise JobError(
                f"field 'files[{i}]' {entry!r} names a directory prefix; 'files' lists "
                f"files one by one, so narrow the scope with 'root' and 'exclude' instead"
            )
        # Git lists nothing inside `.git` and the walk refuses to descend into it, so
        # neither of those sources can put repository metadata in the snapshot. A list
        # names paths directly and would, and a reader that can reach refs is reading
        # more than the snapshot — which is the property every unit rests on. Checked on
        # the NAME, before anything is stat'ed, so an unreadable directory cannot turn
        # this refusal into one about something else.
        if any(part.casefold() == ".git" for part in entry.split(PREFIX_MARK)):
            raise JobError(
                f"field 'files[{i}]' {entry!r} names repository metadata; the snapshot "
                f"holds source, and a reader that can reach refs is not reading it alone"
            )
    return files


def load_job(path: str | Path) -> Job:
    """Read and strictly validate ``job.json``; raise :class:`JobError` naming the field.

    ``root`` must be absolute and an existing directory. It is never resolved against the
    job file's own location: the job file sits in the run directory and moves with it,
    and a root that moved with the file would silently change which tree is audited.
    """
    job_path = Path(path)
    try:
        text = job_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise JobError(f"cannot read job file {job_path}: {exc}") from exc
    except UnicodeDecodeError as exc:
        raise JobError(f"cannot decode job file {job_path} as UTF-8: {exc}") from exc
    try:
        obj = json.loads(text)
    except (ValueError, RecursionError) as exc:
        # ValueError covers a JSONDecodeError and an integer past the interpreter's digit
        # limit; RecursionError, nesting too deep to decode.
        raise JobError(f"job file is not valid JSON: {exc}") from exc
    obj = _require_object(obj, "job")

    partition = obj.get("partition")
    if "partition" not in obj:
        raise JobError("'job' is missing required field 'partition'")
    if not isinstance(partition, str) or partition not in PARTITION_MODES:
        raise JobError(
            f"field 'partition' must be one of: {', '.join(sorted(PARTITION_MODES))} "
            f"(found {partition!r})"
        )
    known = _JOB_KEYS_SUBJECT if partition == "subject" else _JOB_KEYS_FILE
    required = (*_JOB_REQUIRED_KEYS, "areas") if partition == "subject" else _JOB_REQUIRED_KEYS
    _check_keys(obj, "job", known, required)

    problem = _parse_text(obj["problem"], "problem")
    raw_root = _parse_text(obj["root"], "root")
    if not Path(raw_root).is_absolute():
        raise JobError(
            f"field 'root' must be absolute (found {raw_root!r}); the job file moves, the "
            f"tree does not"
        )
    root = _resolve(Path(raw_root), "field 'root'", JobError)
    try:
        is_dir = root.is_dir()
    except OSError as exc:
        raise JobError(f"cannot read field 'root' {root}: {exc}") from exc
    if not is_dir:
        raise JobError(f"field 'root' does not name a directory: {root}")
    exclude = _parse_paths(obj["exclude"], "exclude")
    lenses = _parse_lenses(obj["lenses"], "lenses")
    files = _parse_file_list(obj) if "files" in obj else ()
    coverage = obj.get("coverage")
    if "coverage" in obj and not isinstance(coverage, bool):
        raise JobError(
            f"field 'coverage' must be true or false (found {type(coverage).__name__}); "
            f"it says whether to ask which inputs no test constructs, and leaving it out "
            f"lets the engine decide from the tree"
        )
    questions = _parse_text(obj["questions"], "questions") if "questions" in obj else None

    areas: tuple[Area, ...] = ()
    if partition == "subject":
        raw_areas = obj["areas"]
        if not isinstance(raw_areas, list) or not raw_areas:
            raise JobError("field 'areas' must be a non-empty list of areas")
        areas = tuple(_parse_area(item, f"areas[{i}]") for i, item in enumerate(raw_areas))
        _check_areas_consistent(areas)

    entries: list[tuple[str, str]] = [(e, f"exclude[{i}]") for i, e in enumerate(exclude)]
    entries += [(e, f"files[{i}]") for i, e in enumerate(files)]
    for i, area in enumerate(areas):
        entries += [(e, f"areas[{i}].paths[{j}]") for j, e in enumerate(area.paths)]
        entries += [(e, f"areas[{i}].also_read[{j}]") for j, e in enumerate(area.also_read)]
    _check_paths_consistent(entries)

    return Job(
        path=job_path, problem=problem, root=root, exclude=exclude,
        partition=partition, lenses=lenses, areas=areas, files=files,
        coverage=coverage, questions=questions,
    )


# The one entry a run directory may already hold: the prose writes the job into the run
# directory before ``plan`` reads it, so a run directory holding only that file is the
# normal case, not a stale run.
JOB_FILE_NAME = "job.json"
# Where the job's values came from, which the job file itself cannot say: the engine refuses
# an unknown key, and a job file is a statement of what ran rather than of how it was
# decided. The interview writes this beside the job and the driver copies it in AFTER
# ``plan`` — ``check_rundir`` refuses a run directory holding anything but the job, and
# widening that guard to admit a second file would admit every other one too.
#
# Absent is the ordinary case and means only that nothing recorded the choices: a job handed
# in ready-made has none, and so does one the interview wrote before this file existed. The
# report says that rather than concluding which, because the absence cannot tell them apart.
JOB_NOTES_FILE_NAME = "job-notes.json"
# One per field the notes may carry, and one of these three per field. Only ``defaulted``
# renders a mark: a reader wants to know which values nobody chose, and marking the two that
# were chosen would put a parenthesis on nearly every line to say "as asked".
JOB_NOTE_ORIGINS = ("stated", "defaulted", "interview")
# The job fields the notes may speak for, which is every field an owner decides. ``problem``
# is not among them: it is the one field with no default, so a note about it could only ever
# say ``stated``.
JOB_NOTE_FIELDS = ("root", "exclude", "partition", "lenses", "files", "coverage")
# What the operator — whoever ran the panel — wrote after reading the report: answers to the
# job's questions, corrections to what the panel concluded, and caveats about the run. An
# INPUT to `report`, written by a person or an agent after the first report exists, and never
# one of the files `--rerender` replaces: the engine reads it and writes nothing to it.
REPORT_NOTES_FILE_NAME = "report-notes.json"
REPORT_NOTES_KEYS = ("answers", "corrections", "caveats")
REPORT_NOTE_ANSWER_KEYS = ("question", "answer", "defects")
REPORT_NOTE_CORRECTION_KEYS = ("target", "reason")
# The build check's two answers, which a correction may name in place of a defect. "This
# tree builds: no" is not a defect and is still something an operator can know is wrong.
BUILD_CHECK_TARGETS = ("build", "tests")


def mint_rundir(job_path: str | Path) -> Path:
    """A run directory nobody has to invent a name for.

    A caller choosing the name is a caller choosing another run's name: two runs on one box
    picked the same one, and the second wrote its job file into the first's directory before
    the emptiness check could refuse it -- the guard fired, but after a write. A name minted
    from the clock and the job's own digest cannot collide, and the directory is created
    here and empty by construction, so the check below has nothing to refuse.
    """
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    digest = hashlib.sha256(Path(job_path).read_bytes()).hexdigest()[:8]
    parent = Path(tempfile.gettempdir()) / "review-panel"
    # A predictable name in a shared directory, which on a shared host is somebody else's to
    # have created first. Made private, and refused where it is not ours: a link, a file, or
    # a directory another account owns is a place that account controls, and a run planned
    # there runs with that account reading along.
    try:
        parent.mkdir(mode=0o700, exist_ok=True)
        info = os.lstat(parent)
    except OSError as exc:
        raise RunDirError(f"cannot use {parent} for run directories: {exc}; pass --rundir") from exc
    if not stat.S_ISDIR(info.st_mode):
        raise RunDirError(f"{parent} is not a directory this program created; pass --rundir")
    if hasattr(os, "getuid") and info.st_uid != os.getuid():
        raise RunDirError(f"{parent} belongs to another account; pass --rundir")
    # `exist_ok=False` is the point: the directory is claimed by creating it, so two runs
    # racing for one name cannot both believe they hold it. The suffix covers the same job
    # planned twice inside one second, which is rare and still must not collide.
    for suffix in ("", *(f"-{n}" for n in range(2, 100))):
        minted = parent / f"{stamp}-{digest}{suffix}"
        try:
            minted.mkdir(exist_ok=False)
            return minted
        except FileExistsError:
            continue
        except OSError as exc:
            raise RunDirError(f"cannot create {minted}: {exc}") from exc
    raise RunDirError(f"could not mint a run directory under {parent}: "
                      f"{stamp}-{digest} and 98 suffixes are all taken")


def check_rundir(rundir: str | Path, job: Job) -> Path:
    """Refuse a run directory inside the audited root or inside any git repository, bare or
    not, or one already holding anything but the job being loaded, as a regular file
    literally named ``job.json``. A unit's git runs in the snapshot and walks up to the
    first repository it finds, so a run directory inside a repository hands every unit that
    repository's history and refs.

    Every other entry is refused by name, and a symlink is refused whatever it resolves
    to — an exemption keyed on where a link points lets a link planted under the
    engine's own scratch name pass, and the first write then follows it into the audited
    tree. Returns the resolved path; creates nothing.
    """
    resolved = _resolve(Path(rundir), "--rundir", RunDirError)
    if resolved == job.root or job.root in resolved.parents:
        raise RunDirError(
            f"--rundir {resolved} is inside root {job.root}; the run directory lives "
            f"outside the audited tree"
        )
    # By file identity as well as by spelling: on a case-insensitive volume `.../Tree/run`
    # lies inside root `.../tree`. A file system with no inode numbers skips it.
    try:
        root_stat = os.stat(job.root)
    except OSError as exc:
        raise RunDirError(f"cannot read root {job.root}: {exc}") from exc
    if root_stat.st_ino:
        for candidate in (resolved, *resolved.parents):
            try:
                found = os.stat(candidate)
            except (FileNotFoundError, NotADirectoryError):
                # The run directory itself need not exist yet, so a name that is not there
                # is genuinely not root. Nothing else is: a candidate this cannot stat is a
                # candidate this cannot place, and skipping it answers "outside root" for a
                # directory that may be inside it — the one answer this check exists to
                # refuse. Named instead, so the operator fixes the path or the permission.
                continue
            except OSError as exc:
                raise RunDirError(
                    f"cannot read {candidate} on the way up from --rundir {resolved}: "
                    f"{_os_reason(exc)}; the check that --rundir is outside root cannot be "
                    f"made, and it is not assumed"
                ) from exc
            if (found.st_dev, found.st_ino) == (root_stat.st_dev, root_stat.st_ino):
                raise RunDirError(
                    f"--rundir {resolved} is inside root {job.root}, spelled another way; "
                    f"the run directory lives outside the audited tree"
                )
    repository = _repository_above(resolved)
    if repository is not None:
        raise RunDirError(
            f"--rundir {resolved} is inside the git repository at {repository}; a unit's git "
            f"would reach that repository's history and refs from the snapshot, so give a run "
            f"directory outside any repository"
        )
    # One net over the whole inspection, and every question inside it asked with a refusal
    # available: ``iterdir`` raises PermissionError outright on a directory the caller
    # cannot list, and what a path IS is asked of ``lstat`` rather than of ``Path.exists``
    # and its siblings, whose answer for a path the host would not describe is a plain
    # False on the interpreters that suppress the error.
    # False there clears this guard for a directory nobody can look inside, which is how a
    # run comes to be planned into somebody else's.
    try:
        top = _lstat_or_absent(resolved, "--rundir", RunDirError)
        if top is not None:
            if not stat.S_ISDIR(top.st_mode):
                raise RunDirError(f"--rundir {resolved} exists and is not a directory")
            job_file = _resolve(job.path, "job file", RunDirError)
            for entry in sorted(resolved.iterdir(), key=lambda p: p.name):
                info = _lstat_or_absent(entry, "--rundir entry", RunDirError)
                if info is not None and stat.S_ISLNK(info.st_mode):
                    raise RunDirError(
                        f"--rundir {resolved} holds a symlink ({entry.name}); the run "
                        f"directory holds nothing the engine did not write"
                    )
                if entry.name != JOB_FILE_NAME:
                    raise RunDirError(
                        f"--rundir {resolved} is not empty (holds {entry.name}); give a "
                        f"fresh directory rather than mixing runs"
                    )
                if (info is None or not stat.S_ISREG(info.st_mode)
                        or _resolve(entry, "--rundir entry", RunDirError) != job_file):
                    raise RunDirError(
                        f"--rundir {resolved} holds a {JOB_FILE_NAME} that is not the job "
                        f"being loaded ({job_file}); give a fresh directory rather than "
                        f"mixing runs"
                    )
    except OSError as exc:
        raise RunDirError(f"cannot read --rundir {resolved}: {exc}") from exc
    return resolved


# --------------------------------------------------------------------------- #
# inventory — what is in scope, enumerated without following anything
# --------------------------------------------------------------------------- #
class InventoryError(ReviewPanelError):
    """The tree cannot be enumerated or read as the job describes: an exclusion naming
    nothing, a file that cannot be read, a root with nothing to read."""


class PartitionError(ReviewPanelError):
    """The areas do not close over the inventory: a file in no area, a file in two, an
    ``also_read`` set that cannot fit. Names the path, never drops it."""


@dataclass(frozen=True)
class Skipped:
    """An inventoried entry that is recorded and not read: a symlink (never followed), a
    nested repository (git lists it as one entry), an index entry missing from disk."""

    path: str
    reason: str


# What ``plan`` found when it asked git where the tree stands. Every outcome is one of
# these, because a report that omitted the line would read as though the snapshot came
# from a commit somebody could check out.
COMMIT_STATES = ("commit", "unborn", "not-a-repository", "unreadable")


@dataclass(frozen=True)
class Commit:
    """Where the snapshot was taken from.

    ``state`` is one of :data:`COMMIT_STATES`. ``commit`` carries the full forty-hex
    ``HEAD`` in the ``commit`` state and is empty in every other; ``dirty`` says whether
    the work tree carries uncommitted changes and is ``None`` where there is no work tree
    to ask about, so an unknown is never rendered as clean.

    ``committed`` is that commit's own date, and it is here rather than derived at render
    time because it is a fact about the TREE: two runs over one tree record the same one,
    which is what lets the run directory stay byte-identical between them. It says how old
    the reviewed code is — a sha alone cannot, and "eight files from commit 832123fd" tells
    a reader holding the report nothing about whether they are looking at last week's work
    or last year's. Empty wherever there is no commit to ask about.
    """

    state: str
    commit: str = ""
    dirty: bool | None = None
    committed: str = ""


@dataclass(frozen=True)
class Coverage:
    """How the reviewed set and the repository's tracked set differ, in both directions.

    Compared against the **repository** root, so a job rooted at a subdirectory still
    names the callers above it; ``root`` is where the job root sits inside the repository
    and is empty when they are the same directory. Both lists are repository-relative.

    ``tracked_not_reviewed`` is what the report's reachability warning is rendered from.
    The comparison is against files git **tracks**, and the reviewed set is not a subset
    of those, because ``_git_entries`` reviews non-ignored untracked files too — so a run
    that reviews an untracked file while excluding a tracked caller passes a subset test
    and fails this one, which is the case the warning exists for.

    ``reviewed_not_tracked`` is the other direction and answers a different question: a
    snapshot file no commit carries. The commit record cannot say it, because ``status``
    reports nothing about an ignored file, so an ignored file named in the job's own list
    would otherwise be attributed to a clean ``HEAD`` that does not contain it.

    ``state`` is ``computed`` when the comparison was actually carried out and
    ``unavailable`` when it could not be, outside a repository or wherever git declined to
    answer. Empty lists therefore mean "nothing was missed" only in the first state;
    folding the two together would report a failed measurement as full coverage.
    """

    state: str = "unavailable"
    root: str = ""
    tracked_not_reviewed: tuple[str, ...] = ()
    reviewed_not_tracked: tuple[str, ...] = ()


@dataclass(frozen=True)
class Listing:
    """The enumerated tree before anything is read. ``source`` is ``git`` for a tracked
    plus non-ignored-untracked listing, ``walk`` for a sorted directory walk, ``list``
    for the job's own explicit file list.

    ``context`` is the LOCKFILES git tracks under root that are outside the review —
    excluded, or not named in a ``files`` list. They are copied into the snapshot and
    assigned to nobody, because the snapshot is also the tree the probe and the verifiers
    build in, and a snapshot without the lockfile installs different dependencies from the
    ones the repository pins, so every build result after that describes a tree nobody has.
    Nothing else outside the review is copied: any worker may open anything in the
    snapshot, so the snapshot holds what the job chose and no more, and a lockfile names
    package versions and nothing else. Only git supplies them; a walk has no tracked set."""

    source: str
    files: tuple[str, ...]
    skipped: tuple[Skipped, ...]
    excluded: tuple[str, ...]
    context: tuple[str, ...] = ()


@dataclass(frozen=True)
class FileEntry:
    path: str
    bytes: int
    lines: int
    sha256: str


@dataclass(frozen=True)
class Inventory:
    """The listing, measured: every file's size, line count and digest, and one digest
    over the whole — so a later change can refuse to certify a moved tree — with the
    commit the tree stood at and the tracked files the reviewed set left out."""

    source: str
    files: tuple[FileEntry, ...]
    skipped: tuple[Skipped, ...]
    excluded: tuple[str, ...]
    tree_sha256: str
    commit: Commit = Commit(state="unreadable")
    coverage: Coverage = Coverage()
    # Copied into the snapshot and read by nobody; see :class:`Listing`. Not in
    # ``tree_sha256``, which digests what was reviewed.
    context: tuple[FileEntry, ...] = ()


def matches(entry: str, path: str) -> bool:
    """Whether a normalized job entry claims an inventory path: a prefix claims everything
    under its directory, any other entry claims exactly one file."""
    return path.startswith(entry) if is_prefix(entry) else path == entry


def _is_repository_marker(entry: Path) -> bool:
    """Whether a ``.git`` entry marks a repository: a directory holding ``HEAD``, or a linked
    worktree's file starting ``gitdir:``. Git passes over any other ``.git`` and keeps looking
    upward, and a check that refuses on git's behalf must too. A ``.git`` this user cannot
    look inside, or cannot reach through a link, counts as a repository: git cannot read it
    either, so nothing can say it is not one. ``os.stat`` rather than ``Path.is_file``,
    which on Python 3.13 turns the permission error into a plain False.

    **Only "not there" is False.** A permission error is one reason a ``.git`` cannot be
    examined and the volume failing is another, and neither is evidence there is no
    repository — answered False, both hand ``check_rundir`` a confident "outside every
    repository" for a directory a unit's git will then find itself inside.
    """
    try:
        mode = os.stat(entry).st_mode
    except (FileNotFoundError, NotADirectoryError):
        return False
    except OSError:
        return True
    try:
        if stat.S_ISDIR(mode):
            return stat.S_ISREG(os.stat(entry / "HEAD").st_mode)
        if stat.S_ISREG(mode):
            with open(entry, "rb") as fh:
                return fh.read(len(b"gitdir:")) == b"gitdir:"
    except (FileNotFoundError, NotADirectoryError):
        return False
    except OSError:
        return True
    return False


def _is_git_directory(path: Path) -> bool:
    """Whether ``path`` is itself a git directory, as a bare repository or a work tree's
    ``.git`` is: ``HEAD`` beside ``objects/`` and ``refs/``. Git looks at each directory on
    the way up as well as at its ``.git``, so a check with only the second misses a bare
    repository.

    Absent is the only False, by :func:`_is_repository_marker`'s rule: a piece that is not
    there says this is not a git directory, and a piece that cannot be stat'd says only that
    the question went unanswered — which must not read as "no repository here".
    """
    try:
        return (stat.S_ISREG(os.stat(path / "HEAD").st_mode)
                and stat.S_ISDIR(os.stat(path / "objects").st_mode)
                and stat.S_ISDIR(os.stat(path / "refs").st_mode))
    except (FileNotFoundError, NotADirectoryError):
        return False
    except OSError:
        return True


def _repository_above(path: Path) -> Path | None:
    """The nearest directory at or above ``path`` that is a repository, or ``None``: one whose
    ``.git`` marks a repository, or one that is itself a git directory. Read from the file
    system, not from git's messages, which change with the host's language."""
    for candidate in (path, *path.parents):
        if _is_repository_marker(candidate / ".git") or _is_git_directory(candidate):
            return candidate
    return None


# What tells git which repository to use instead of discovering it from its working directory:
# ``git rev-parse --local-env-vars``, and the ceiling that stops discovery early. Inherited,
# they make git answer about another repository than the one root sits in.
_GIT_LOCATION_VARIABLES = frozenset({
    "GIT_ALTERNATE_OBJECT_DIRECTORIES", "GIT_CONFIG", "GIT_CONFIG_PARAMETERS", "GIT_CONFIG_COUNT",
    "GIT_OBJECT_DIRECTORY", "GIT_DIR", "GIT_WORK_TREE", "GIT_IMPLICIT_WORK_TREE", "GIT_GRAFT_FILE",
    "GIT_INDEX_FILE", "GIT_NO_REPLACE_OBJECTS", "GIT_REPLACE_REF_BASE", "GIT_PREFIX",
    "GIT_SHALLOW_FILE", "GIT_COMMON_DIR", "GIT_CEILING_DIRECTORIES",
})


def _git_environment() -> dict[str, str]:
    return {name: value for name, value in os.environ.items()
            if name.upper() not in _GIT_LOCATION_VARIABLES}


def _git_entries(root: Path, *, tracked_only: bool = False) -> list[str] | None:
    """Tracked plus non-ignored untracked entries under ``root`` — tracked alone with
    ``tracked_only`` — or ``None`` when git is absent or no repository holds ``root``. A
    repository git cannot read is a refusal, and so is a root inside a git directory.

    NUL-delimited on purpose: without ``-z`` git quotes a name holding a newline or a
    non-ASCII byte, and the quoted form names no file. Read as bytes and decoded once,
    explicitly — a ``text=True`` here would decode with the host's locale. A nested
    checkout arrives as ``dir/``; the mark is dropped and ``lstat`` classifies it.
    """
    env = _git_environment()
    try:
        probe = subprocess.run(_git_argv(root, "rev-parse", "--is-inside-work-tree"),
                               capture_output=True, env=env)
    except OSError as exc:
        repository = _repository_above(root)
        if repository is not None:
            raise InventoryError(
                f"git cannot be run ({exc}), and root sits in the repository at {repository}; "
                f"install git, or give a root outside it, rather than let a plain walk copy the "
                f"files git would ignore"
            ) from exc
        return None
    if probe.returncode != 0 or probe.stdout.strip() != b"true":
        repository = _repository_above(root)
        if repository is not None and probe.returncode != 0:
            # Git ran and could not read a repository the tree sits in (dubious ownership, a
            # corrupt or unreadable .git). A walk in its place would copy every file git ignores.
            detail = probe.stderr.decode("utf-8", errors="replace").strip()
            raise InventoryError(
                f"git cannot read the repository at {repository}: {detail}; fix it, or give a "
                f"root outside it, rather than let a plain walk copy the files git would ignore"
            )
        if repository is not None:
            # Git answers false inside a git directory or a bare repository, where a walk
            # would snapshot the repository's own refs, objects and config.
            raise InventoryError(
                f"root {root} is not in a work tree but sits in the repository at {repository}; "
                f"give the work tree, or a root outside the repository"
            )
        return None
    entries: list[str] = []
    listings = [["ls-files", "-z"]]
    if not tracked_only:
        listings.append(["ls-files", "-z", "--others", "--exclude-standard"])
    for listing in listings:
        proc = subprocess.run(_git_argv(root, *listing), capture_output=True, env=env)
        if proc.returncode != 0:
            detail = proc.stderr.decode("utf-8", errors="replace").strip()
            raise InventoryError(f"git {' '.join(listing)} failed under {root}: {detail}")
        for raw in proc.stdout.split(b"\0"):
            if not raw:
                continue
            try:
                entries.append(raw.decode("utf-8").rstrip(PREFIX_MARK))
            except UnicodeDecodeError as exc:
                raise InventoryError(f"file name under {root} is not UTF-8: {raw!r}") from exc
    return entries


# Git tidies its own storage on its own schedule, and what prompts it is having just been
# asked something. This engine asks several times over the audited root, so without these a
# run can leave a `maintenance.lock` and a repacked object store in somebody's repository --
# which is how one CI job went red on a test asserting the audited tree comes back
# byte-identical. Nothing is damaged by it, and git does the same after `git status`; what
# it costs is the promise. This engine copies the tree into `snapshot/` precisely so it
# never touches the original, and an inspection that mutates what it inspects is not one.
#
# `-c` is git's OWN option and is ignored after the subcommand, so these lead the argv.
_GIT_QUIET_HOUSEKEEPING = ("-c", "gc.auto=0", "-c", "maintenance.auto=false")


def _git_argv(root: str | Path, *rest: str) -> list[str]:
    """Every git this engine runs, built in one place.

    One place rather than five so a call site added later cannot forget the housekeeping
    flags -- the failure would be a repository quietly written to, which nothing reports.
    """
    return ["git", *_GIT_QUIET_HOUSEKEEPING, "-C", str(root), *rest]


def _run_git(argv: Sequence[str], env: dict[str, str]):
    """Run git and hand back the completed process, or ``None`` when it could not be run.

    Both probes below produce a record or a warning, and neither may end a run that would
    otherwise have succeeded — so a git that cannot be launched is the same answer as a git
    that answered non-zero. Every call in THOSE probes goes through here, because catching
    only the first leaves the rest able to raise out of a function whose contract says it
    does not.

    Not every git this engine runs: the inventory's two callers run :mod:`subprocess`
    directly and mean to raise, since a git that cannot list a repository's files is a run
    that cannot start rather than a record that goes unwritten. What every git does share is
    :func:`_git_argv`, which is where the argument list is built.
    """
    try:
        return subprocess.run(list(argv), capture_output=True, env=env)
    except (OSError, ValueError):
        # ValueError covers the argument-encoding failure: a path decoded here and handed
        # back to git is encoded with the filesystem encoding, and under an ASCII locale a
        # non-ASCII repository name raises UnicodeEncodeError, which is not an OSError.
        # This suite runs under LC_ALL=C on purpose, so that locale is not hypothetical.
        return None


def _git_commit(root: Path) -> Commit:
    """Ask git which commit ``root``'s work tree stands at, and whether it is dirty.

    Never raises: every failure is a state instead. A tree outside a repository is an
    ordinary job, and the report's snippets have to say which of these they came from —
    a commit anyone can check out, a repository with no commit yet, a tree that is not a
    repository at all, or a git that could not answer.

    ``status`` is asked about the whole work tree rather than about ``root`` alone,
    because a snippet quoted from a subdirectory is still unreachable by commit when a
    sibling file is uncommitted.
    """
    env = _git_environment()
    inside = _run_git(_git_argv(root, "rev-parse", "--is-inside-work-tree"), env)
    # "git could not answer" and "there is no repository" are different facts, and only
    # the file system can tell them apart: git answers the same non-zero for a repository
    # whose ownership it distrusts as for a directory that is in none. Reported as the
    # same state, a report would tell a reader the tree is not version-controlled when
    # what happened is that git refused to look.
    if inside is None or inside.returncode != 0 or inside.stdout.strip() != b"true":
        return Commit(state="unreadable" if _repository_above(root) is not None
                      else "not-a-repository")
    # Both flags pinned rather than left to the tree's own configuration: with
    # ``status.showUntrackedFiles=no``, or submodule changes configured away, the same
    # dirty tree reports clean and the record says a commit carries bytes no commit has.
    status = _run_git(_git_argv(root, "status", "--porcelain", "-z",
                                "--untracked-files=all", "--ignore-submodules=none"), env)
    if status is None or status.returncode != 0:
        return Commit(state="unreadable")
    dirty = bool(status.stdout.strip())
    # ``--verify`` and the ``^{commit}`` peel, because plain ``rev-parse HEAD`` falls back
    # to reading its argument as a path name: an unborn repository holding a work-tree file
    # called ``HEAD`` exits zero, and the record then claims a commit spelled "HEAD".
    head = _run_git(_git_argv(root, "rev-parse", "--verify", "--quiet", "HEAD^{commit}"), env)
    if head is None or head.returncode != 0:
        # Which failure it was, since the two render differently: a repository with no
        # commits has nothing to quote from, while one whose HEAD will not resolve though
        # commits exist is a git that could not answer.
        history = _run_git(_git_argv(root, "rev-list", "-n", "1", "--all"), env)
        if history is not None and history.returncode == 0 and not history.stdout.strip():
            return Commit(state="unborn", dirty=dirty)
        return Commit(state="unreadable", dirty=dirty)
    sha = head.stdout.decode("utf-8", "replace").strip()
    # The commit's own date, strict ISO-8601 so it sorts and parses without a format to
    # agree on. One more invocation, once per run, against the several hundred this stage
    # already makes -- and a failure here costs the date and nothing else, because a
    # missing date is a shorter sentence while a failed run is no report at all.
    when = _run_git(_git_argv(root, "show", "-s", "--format=%cI", f"{sha}^{{commit}}"), env)
    dated = (when.stdout.decode("utf-8", "replace").strip()
             if when is not None and when.returncode == 0 else "")
    return Commit(state="commit", commit=sha, dirty=dirty, committed=dated)


def _git_tracked(root: Path) -> Coverage | None:
    """Every file git **tracks** in the repository holding ``root``, repository-relative,
    with where ``root`` sits inside it — or ``None`` when the answer cannot be had.

    Tracked only, and not :func:`_git_entries`'s tracked-plus-untracked listing, for the
    reason :class:`Unreviewed` gives. Never raises, for the reason :func:`_git_commit`
    does not: this produces a warning, and a warning that could abort a run is worse than
    the gap it reports.
    """
    env = _git_environment()
    top = _run_git(_git_argv(root, "rev-parse", "--show-toplevel"), env)
    if top is None or top.returncode != 0:
        return None
    # Exactly one terminator, and decoded the way the file system spells a name rather
    # than as UTF-8. A directory whose name ends in a space or a newline is valid on a
    # POSIX host, and trimming either names a different directory — which then lists
    # nothing, and an answer nobody could compute must not read as full coverage. The
    # decode matters twice over: this string is handed straight back to git, so it has to
    # survive the round trip through the filesystem encoding, which ``os.fsdecode`` gives
    # and a UTF-8 decode does not.
    raw_top = top.stdout
    toplevel = os.fsdecode(raw_top[:-1] if raw_top.endswith(b"\n") else raw_top)
    if not toplevel:
        return None
    listing = _run_git(_git_argv(toplevel, "ls-files", "-z"), env)
    if listing is None or listing.returncode != 0:
        return None
    # A set: ``ls-files`` emits a conflicted path once per index stage, and what is
    # promised is a subtraction over paths rather than a count of index entries. Decoded
    # losslessly, so two names that differ only outside UTF-8 stay two names; the escaping
    # that makes them writable happens once, where the record is written.
    tracked = {os.fsdecode(raw).rstrip(PREFIX_MARK)
               for raw in listing.stdout.split(b"\0") if raw}
    try:
        prefix = _resolve(root, "root", InventoryError).relative_to(
            _resolve(Path(toplevel), "the repository root", InventoryError)).as_posix()
    except (ValueError, ReviewPanelError):
        # Not under the toplevel after resolution, or a path that cannot be resolved at
        # all. Either way there is no answer, and a warning must not be able to end a run
        # that would otherwise have succeeded.
        return None
    return Coverage(state="computed", root="" if prefix == "." else prefix,
                    tracked_not_reviewed=tuple(sorted(tracked)))


def _coverage(root: Path, reviewed: Sequence[str]) -> Coverage:
    """The two differences between the reviewed set and the repository's tracked set,
    both spelled against the repository root.

    An answer that could not be computed comes back as ``unavailable`` rather than as two
    empty lists: "nothing was missed" and "nobody could tell" are different sentences, and
    only one of them is a reason not to widen the scope.
    """
    found = _git_tracked(root)
    if found is None:
        return Coverage()
    prefix = f"{found.root}/" if found.root else ""
    tracked = set(found.tracked_not_reviewed)
    covered = {f"{prefix}{rel}" for rel in reviewed}
    return replace(
        found,
        tracked_not_reviewed=tuple(sorted(tracked - covered)),
        reviewed_not_tracked=tuple(sorted(covered - tracked)),
    )


_IGNORED_REASON = "ignored by git"


def _git_ignored(root: Path) -> list[tuple[str, bool]]:
    """What git ignores under ``root``, each with whether it is a directory, a wholly ignored
    directory as one entry, so the report's *Not read* stays every path nobody read. Called
    only where :func:`_git_entries` found the repository."""
    listing = ["ls-files", "-z", "--others", "--ignored", "--exclude-standard", "--directory"]
    proc = subprocess.run(_git_argv(root, *listing), capture_output=True,
                          env=_git_environment())
    if proc.returncode != 0:
        detail = proc.stderr.decode("utf-8", errors="replace").strip()
        raise InventoryError(f"git {' '.join(listing)} failed under {root}: {detail}")
    entries: set[tuple[str, bool]] = set()
    for raw in proc.stdout.split(b"\0"):
        if not raw:
            continue
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise InventoryError(f"file name under {root} is not UTF-8: {raw!r}") from exc
        # Git marks a wholly ignored directory with a trailing slash. Kept, since only a
        # directory may answer a directory-prefix exclusion.
        entries.add((text.rstrip(PREFIX_MARK), text.endswith(PREFIX_MARK)))
    return sorted(entries)


def _not_a_unit_directory(target: Path) -> bool:
    """Whether something a stage cannot reclaim is already at a unit directory's name.

    A link of any kind is a refusal: it is the one thing at that path no stage wrote, so
    there is nothing there to take back, and a dangling one would let the guard pass and the
    ``mkdir`` fail later — after earlier directories of the same batch had landed. A regular
    file is a refusal for the same reason.

    Asked of the link itself and with a refusal available. ``Path.exists`` follows the link
    and turns a path the host will not describe into False, which clears this guard for a
    name nobody can look at.
    """
    info = _lstat_or_absent(target, "the unit directory", RunDirError)
    return info is not None and (not stat.S_ISDIR(info.st_mode) or _is_junction(target))


def _is_junction(path: Path) -> bool:
    """Whether ``path`` is a Windows directory junction. A junction is neither a symlink to
    ``is_symlink`` nor ``S_ISLNK`` to ``lstat``, so every check that must never follow a
    link asks this too. ``os.path.isjunction`` is 3.12+; before it, ``lstat`` carries the
    reparse tag on Windows, and a junction's is the mount-point tag. False elsewhere."""
    isjunction = getattr(os.path, "isjunction", None)
    if isjunction is not None:
        return isjunction(path)
    try:
        tag = getattr(os.lstat(path), "st_reparse_tag", 0)
    except OSError:
        # False authorizes nothing on its own. This answers what a path IS, and no caller
        # acts on the answer without touching the path again: each one either lstats it in
        # the same expression and raises, or goes on to open, remove or create at it, where
        # the same refusal is met and reported, with the reason on it. The reach is narrow
        # too: this branch runs only where ``os.path.isjunction`` is absent, and that
        # function answers False on the same error.
        return False
    return tag == getattr(stat, "IO_REPARSE_TAG_MOUNT_POINT", 0xA0000003)


def _walk_entries(root: Path) -> list[str]:
    """A sorted walk that descends into no symlink, no junction and no ``.git``; each is
    listed so the inventory records it. An unreadable directory is an error, not a silent
    gap.

    The per-child question is the one thing tolerated here, and it authorizes nothing: a
    child the host will not describe is descended into rather than recorded as a link, and
    ``os.walk`` — which follows no link and raises through ``fail`` on a listing it cannot
    do — then refuses by name. Tolerated the other way it would record a real directory as
    a link and leave every file under it out of the inventory."""

    def fail(exc: OSError) -> None:
        raise InventoryError(f"cannot list {exc.filename}: {exc.strerror}") from exc

    entries: list[str] = []
    for dirpath, dirnames, filenames in os.walk(root, onerror=fail, followlinks=False):
        base = Path(dirpath)
        dirnames.sort()
        filenames.sort()
        descend = []
        for name in dirnames:
            child = base / name
            if child.is_symlink() or _is_junction(child) or name == ".git":
                entries.append(child.relative_to(root).as_posix())
            else:
                descend.append(name)
        dirnames[:] = descend
        entries.extend((base / name).relative_to(root).as_posix() for name in filenames)
    return entries


# A directory git lists as one entry: a nested checkout, a submodule, or (walk mode) the
# ``.git`` directory itself. Recorded under these reasons, and excludable as a directory.
_NESTED_REASON = "nested repository"
_GIT_DIR_REASON = "repository metadata"
_JUNCTION_REASON = "junction"
_DIRECTORY_REASONS = frozenset({_NESTED_REASON, _GIT_DIR_REASON, _JUNCTION_REASON})


def _exclusion_hits(exclude: Sequence[str], rel: str) -> list[int]:
    return [i for i, entry in enumerate(exclude) if matches(entry, rel)]


def _linked_ancestor(root: Path, rel: str, cache: dict[str, bool]) -> bool:
    """Whether any directory component of ``rel`` below root is itself a symlink or a
    directory junction.

    The entry's own ``lstat`` sees only the last component: a tracked ``dir/file.py``
    whose ``dir`` has since become a link to a directory outside root lstats as a
    regular file out there, and would be read and copied from outside the tree. Each
    ancestor is checked once per enumeration; the answer for a directory is the same
    for every file under it.
    """
    parts = rel.split("/")[:-1]
    for depth in range(1, len(parts) + 1):
        ancestor = "/".join(parts[:depth])
        linked = cache.get(ancestor)
        if linked is None:
            try:
                linked = stat.S_ISLNK(os.lstat(root / ancestor).st_mode) or _is_junction(root / ancestor)
            except FileNotFoundError:
                linked = False
            except OSError as exc:
                raise InventoryError(f"cannot stat {ancestor}: {exc.strerror}") from exc
            cache[ancestor] = linked
        if linked:
            return True
    return False


def _classify(root: Path, rel: str, ancestors: dict[str, bool]) -> Skipped | None:
    """``None`` for a regular file to read; otherwise why the entry is recorded and not
    read. Uses ``lstat`` so a symlink is seen as itself and never followed, on the entry
    and on every directory above it below root."""
    if _linked_ancestor(root, rel, ancestors):
        return Skipped(rel, "symlink")
    if _is_junction(root / rel):
        return Skipped(rel, _JUNCTION_REASON)
    try:
        mode = os.lstat(root / rel).st_mode
    except FileNotFoundError:
        return Skipped(rel, "missing")
    except OSError as exc:
        raise InventoryError(f"cannot stat {rel}: {exc.strerror}") from exc
    if stat.S_ISLNK(mode):
        return Skipped(rel, "symlink")
    if stat.S_ISDIR(mode):
        return Skipped(rel, _GIT_DIR_REASON if Path(rel).name == ".git" else _NESTED_REASON)
    if not stat.S_ISREG(mode):
        return Skipped(rel, "not a regular file")
    if Path(rel).name == ".git":
        # A linked worktree's ``.git`` is a FILE holding a gitdir pointer to the real
        # repository; copied into the snapshot it would let a reader's git reach refs the
        # tree does not contain — the experiment's case-8 leak by another door.
        return Skipped(rel, _GIT_DIR_REASON)
    return None


def _require_listable(rel: str) -> None:
    """Refuse, by name, a path no record can carry."""
    if any(ch < " " or ch == "\x7f" for ch in rel):
        # A newline in a name renders as two entries in a payload's file list, or as a
        # forged line in the report's "not read" section; excluded and skipped paths are
        # rendered too, so the rule covers every enumerated path, not only files, and
        # renaming is the only way past it.
        raise InventoryError(f"path {rel!r} contains a control character; rename it")
    try:
        rel.encode("utf-8")
    except UnicodeEncodeError:
        # A walk hands over a name that is not valid UTF-8 surrogate-escaped, and every record
        # the engine writes is UTF-8: refused here, by name, as git mode refuses it.
        raise InventoryError(f"path {rel!r} is not valid UTF-8; rename it") from None


# A listed entry the owner named and the engine cannot read is a refusal, not a silent
# skip: the other two sources enumerated the tree themselves, so a path they classify away
# was never asked for, while every one of these was. A symlink and a junction are the
# exception — "recorded and never followed" is the rule for every source, and following one
# here would read outside root.
_LISTED_REFUSALS = {
    "missing": "is not in the tree",
    _NESTED_REASON: "is a directory",
    _GIT_DIR_REASON: "is repository metadata",
    "not a regular file": "is not a regular file",
}


def _check_listed_root(job: Job) -> None:
    """The refusal ``_git_entries`` makes that list mode would otherwise never reach: a
    root at or inside a git directory, where every file to read is a ref, an object or a
    config, and the snapshot would be a copy of the repository's insides."""
    # The basename first, because a damaged metadata directory is still a directory full
    # of refs and `_is_git_directory` answers no for one missing a piece.
    if job.root.name.casefold() == ".git":
        raise InventoryError(
            f"root {job.root} is a git directory; give the work tree, or a root outside "
            f"the repository"
        )
    found = _repository_above(job.root)
    if found is not None and _is_git_directory(found):
        raise InventoryError(
            f"root {job.root} is the git directory at {found} or sits inside it; give the "
            f"work tree, or a root outside the repository"
        )


def enumerate_files(job: Job) -> Listing:
    """Enumerate what the job puts in scope: its own ``files`` list where it has one,
    otherwise git's view of ``job.root`` where the root is inside a work tree, otherwise a
    sorted walk. Symlinks are recorded and never followed; an excluded path is recorded
    as excluded, never dropped; an exclusion that matches nothing is refused by name,
    since a listed path the report can never show under *not read* is a silent gap.

    """
    tree: list[str] | None = None
    if job.files:
        _check_listed_root(job)
        raw: list[str] | None = list(job.files)
        source = "list"
        # Only for the files a build needs beside the listed ones. A list names its own
        # scope and never needed git, so a host where git cannot answer still plans; its
        # snapshot then holds the listed files alone.
        try:
            tree = _git_entries(job.root, tracked_only=True)
        except InventoryError:
            tree = None
    else:
        raw = _git_entries(job.root)
        source = "git" if raw is not None else "walk"
        if raw is None:
            raw = _walk_entries(job.root)
        else:
            tree = _git_entries(job.root, tracked_only=True)
    files: list[str] = []
    skipped: list[Skipped] = []
    excluded: list[str] = []
    matched = [False] * len(job.exclude)
    ancestors: dict[str, bool] = {}
    for rel in sorted(set(raw)):
        _require_listable(rel)
        hits = _exclusion_hits(job.exclude, rel)
        # Classified only when not already excluded, so an excluded path the host will not
        # even stat is still just excluded. A nested checkout is listed as one entry and
        # is a directory, so the owner's natural spelling, ``vendor/``, must exclude it.
        why = None if hits else _classify(job.root, rel, ancestors)
        if why is not None and why.reason in _DIRECTORY_REASONS:
            hits = _exclusion_hits(job.exclude, rel + PREFIX_MARK)
        for i in hits:
            matched[i] = True
        if hits:
            excluded.append(rel)
            continue
        if why is not None and source == "list" and why.reason in _LISTED_REFUSALS:
            raise InventoryError(
                f"field 'files' names {rel!r}, which {_LISTED_REFUSALS[why.reason]} under "
                f"root {job.root}"
            )
        if why is None:
            files.append(rel)
        else:
            skipped.append(why)
    if source == "git":
        for rel, is_directory in _git_ignored(job.root):
            _require_listable(rel)
            # A directory is matched in its directory spelling, which every prefix covering it
            # also matches; a file only in its own, so a directory prefix never claims a file.
            hits = _exclusion_hits(job.exclude, rel + PREFIX_MARK if is_directory else rel)
            for i in hits:
                matched[i] = True
            if hits:
                excluded.append(rel)
            else:
                skipped.append(Skipped(rel, _IGNORED_REASON))
        excluded.sort()
        skipped.sort(key=lambda entry: entry.path)
    for i, entry in enumerate(job.exclude):
        if not matched[i]:
            raise InventoryError(
                f"field 'exclude[{i}]' {entry!r} matches nothing under root; a prefix must "
                f"name a directory in the inventory and a path must name a file in it"
            )
    if not files:
        raise InventoryError(
            f"root {job.root} holds no files to read ({len(excluded)} excluded, "
            f"{len(skipped)} skipped)"
        )
    return Listing(source=source, files=tuple(files), skipped=tuple(skipped),
                   excluded=tuple(excluded),
                   context=_context_files(job.root, tree, set(files), ancestors))


def _context_files(root: Path, tree: list[str] | None, in_scope: set[str],
                   ancestors: dict[str, bool]) -> tuple[str, ...]:
    """The lockfiles git tracks under ``root`` that are not in scope: what the snapshot
    carries beside the review so a build inside it installs what the repository pins. See
    :class:`Listing`.

    A path that is not a plain regular file — a link, a nested checkout, repository
    metadata — is left out, by the same classification the in-scope files pass. So is one
    the host will not describe: it is out of scope, nobody reads it, and refusing the plan
    over a file the owner excluded would make an exclusion the thing that breaks a run.
    """
    if tree is None:
        return ()
    out: list[str] = []
    for rel in sorted(set(tree) - in_scope):
        if any(ch < " " or ch == "\x7f" for ch in rel):
            continue
        if not is_lockfile(rel):
            continue
        try:
            rel.encode("utf-8")
            if _classify(root, rel, ancestors) is None:
                out.append(rel)
        except (InventoryError, UnicodeEncodeError):
            continue
    return tuple(out)


_CHUNK = 1 << 20


def _open_regular(path: Path, rel: str):
    """Open ``path`` for reading only while it is still the regular file ``_classify`` saw.

    The tree can change between classifying an entry and reading it: a symlink swapped in
    would be followed out of root, and a named pipe would hold the read open. ``O_NOFOLLOW``
    and ``O_NONBLOCK`` settle both where the host has them; elsewhere an ``lstat`` stands
    in for the first."""
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    changed = f"{rel} became a symlink while plan was reading the tree; re-run plan"
    if not nofollow and stat.S_ISLNK(os.lstat(path).st_mode):
        raise InventoryError(changed)
    flags = os.O_RDONLY | nofollow | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_BINARY", 0)
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        if nofollow and exc.errno in (errno.ELOOP, errno.EMLINK):
            raise InventoryError(changed) from exc
        raise
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise InventoryError(f"{rel} is no longer a regular file; re-run plan")
        return os.fdopen(fd, "rb")
    except BaseException:
        os.close(fd)
        raise


def _read_measuring(path: Path, rel: str, sink=None) -> FileEntry:
    """Read one file once, chunked: digest, byte count and line count together, and a
    copy into ``sink`` when one is given. A last line without a newline still counts."""
    digest = hashlib.sha256()
    size = lines = 0
    last = b""
    try:
        with _open_regular(path, rel) as fh:
            while chunk := fh.read(_CHUNK):
                digest.update(chunk)
                size += len(chunk)
                lines += chunk.count(b"\n")
                last = chunk[-1:]
                if sink is not None:
                    sink.write(chunk)
    except OSError as exc:
        raise InventoryError(f"cannot read {rel}: {exc.strerror}") from exc
    if size and last != b"\n":
        lines += 1
    return FileEntry(path=rel, bytes=size, lines=lines, sha256=digest.hexdigest())


def measure_files(job: Job, listing: Listing) -> Inventory:
    """Measure every listed file and digest the whole, without writing anything, then ask
    git the two questions only this stage can answer: which commit the tree stands at, and
    how the reviewed set and the repository's tracked set differ.

    Asked **after** the bytes are read, and that ordering is the point. A file edited
    between the question and the read would leave a record saying ``clean`` beside a
    snapshot holding bytes no commit carries; asked afterwards, the same edit makes the
    tree dirty and the record says so. The window on the other side is closed already —
    ``write_snapshot`` re-digests every copy and refuses one that no longer matches.
    """
    entries = tuple(_read_measuring(job.root / rel, rel) for rel in listing.files)
    tree = hashlib.sha256()
    for entry in entries:
        tree.update(f"{entry.sha256}  {entry.path}\n".encode("utf-8"))
    return Inventory(
        source=listing.source, files=entries, skipped=listing.skipped,
        excluded=listing.excluded, tree_sha256=tree.hexdigest(),
        commit=_git_commit(job.root),
        coverage=_coverage(job.root, listing.files),
        context=_measure_context(job.root, listing.context, listing.files),
    )


def _measure_context(root: Path, context: Sequence[str],
                     in_scope: Sequence[str]) -> tuple[FileEntry, ...]:
    """Measure the out-of-scope files the snapshot carries, leaving out any it cannot hold.

    Left out rather than refused, for the reason :func:`_context_files` gives: nobody reads
    these, so one the host will not read, or one whose name differs only by case from
    another path and cannot share a case-insensitive run volume with it, costs the build
    that file and nothing else. An in-scope collision is still refused, by
    :func:`_check_case_collisions`."""
    def prefixes(path: str) -> list[str]:
        parts = path.casefold().split("/")
        return ["/".join(parts[:i]) for i in range(1, len(parts))]

    scope_files = {path.casefold() for path in in_scope}
    directories = {prefix for path in (*in_scope, *context) for prefix in prefixes(path)}
    folded: dict[str, int] = {}
    for rel in context:
        folded[rel.casefold()] = folded.get(rel.casefold(), 0) + 1
    out: list[FileEntry] = []
    for rel in context:
        # A file clashes with another file of the same folded name, with a directory of
        # that name, and — through its own directories — with a file named like one.
        if (rel.casefold() in scope_files or rel.casefold() in directories
                or folded[rel.casefold()] > 1
                or any(prefix in scope_files for prefix in prefixes(rel))):
            continue
        try:
            out.append(_read_measuring(root / rel, rel))
        except InventoryError:
            continue
    return tuple(out)


# --------------------------------------------------------------------------- #
# writing — every file lands by atomic replace
# --------------------------------------------------------------------------- #
# The longest file name the common file systems take, in bytes.
_NAME_MAX = 255


def _writable(text: str) -> str:
    """A string every record can carry: a name decoded with ``os.fsdecode`` may hold a
    lone surrogate, which no UTF-8 write accepts. Escaped rather than dropped — a coverage
    record that silently loses a path is the gap it exists to report."""
    return text.encode("utf-8", "backslashreplace").decode("utf-8")


def _temp_beside(path: Path) -> Path:
    """The scratch name a write goes through before ``os.replace`` lands it. Carries the
    pid so it cannot collide with a real file the snapshot is also copying. A name already
    at the file-name limit is shortened to leave room for that suffix; the create is
    exclusive, so a clash is a refusal, never an overwrite."""
    suffix = f".{os.getpid()}.tmp"
    encoded = path.name.encode("utf-8", "surrogateescape")
    room = _NAME_MAX - len(suffix)
    name = path.name if len(encoded) <= room else encoded[:room].decode("utf-8", "ignore")
    return path.with_name(name + suffix)


def _create_scratch(tmp: Path, what: str, *, binary: bool = False):
    """Open the scratch file with exclusive create, so a path already at that name — a
    file or a link — is a refusal and is never truncated or followed. The refusal is
    raised here, before any ``_discard``, because a scratch path the engine did not
    create is not the engine's to remove.

    ``newline="\\n"`` so the bytes on disk are the bytes written on every host: without it
    a text write on Windows lands ``\\r\\n``, a payload no longer equals the string it was
    rendered from, and two hosts derive two different ``areas.json`` from one tree."""
    try:
        if binary:
            return open(tmp, "xb")
        return open(tmp, "x", encoding="utf-8", newline="\n")
    except FileExistsError as exc:
        raise InventoryError(
            f"cannot write {what}: {tmp} already exists; the run directory holds nothing "
            f"the engine did not write"
        ) from exc
    except OSError as exc:
        raise InventoryError(f"cannot write {what}: {exc}") from exc


def write_json(path: Path, obj: object) -> None:
    """Write JSON beside the target, then replace: a crash leaves the old file or none,
    never a half-written one. Sorted keys and a fixed indent keep two runs byte-equal."""
    tmp = _temp_beside(path)
    fh = _create_scratch(tmp, str(path))
    try:
        with fh:
            fh.write(json.dumps(obj, indent=2, sort_keys=True) + "\n")
        os.replace(tmp, path)
    except OSError as exc:
        _discard(tmp)
        raise InventoryError(f"cannot write {path}: {exc}") from exc
    except BaseException:
        # Anything else, a value that will not encode or serialize included, still takes
        # its scratch file with it.
        _discard(tmp)
        raise


def write_text(path: Path, text: str) -> None:
    """``write_json``'s rule for prose: exclusive scratch, then replace, UTF-8 pinned."""
    tmp = _temp_beside(path)
    fh = _create_scratch(tmp, str(path))
    try:
        with fh:
            fh.write(text)
        os.replace(tmp, path)
    except OSError as exc:
        _discard(tmp)
        raise InventoryError(f"cannot write {path}: {exc}") from exc
    except BaseException:
        # As in write_json: any failure takes the scratch file with it.
        _discard(tmp)
        raise


def _discard(tmp: Path) -> None:
    """Remove a scratch file the engine created and a failed write left; the failure
    itself is what is reported."""
    try:
        tmp.unlink()
    except OSError:
        pass


# The scratch suffix `_temp_beside` spells: a pid between the file's own name and `.tmp`.
_SCRATCH_TAIL = ".tmp"
_PID_ONLY = re.compile(r"[0-9]+\Z", re.ASCII)
# What a stage's claim on a run directory is called: the stage's own name and this. One per
# stage rather than one for the run, because what two stages must not do at once is the same
# stage twice, and the marker already keeps different stages in order.
STAGE_LOCK_SUFFIX = ".lock"


def _scratch_beside(paths: Sequence[Path]) -> list[Path]:
    """Every scratch file an interrupted write of one of ``paths`` could have left.

    ``_temp_beside`` names it ``<name>.<pid>.tmp``, and the pid belongs to the process that
    died — so a later run cannot spell the name and has to recognize it. Each name is
    matched against the files it could belong to, which is what keeps this from being a
    sweep: a directory entry matching none of the names the caller enumerated is not
    returned, whatever it is.

    Left behind, those files are not inert. The pid comes round again, and
    ``_create_scratch`` creates exclusively, so the next write of the same file under the
    same pid is refused by a file nobody remembers making.

    A ``.previous.`` backup is deliberately not matched. Its middle is not a bare pid, and
    it is the only copy of something that was published — a report somebody may have
    annotated — so it is left for an operator to decide about.

    A directory that cannot be listed contributes nothing rather than raising: this is a
    tidy-up before the real work, and the write that follows reports what it cannot do.
    """
    by_parent: dict[Path, set[str]] = {}
    for path in paths:
        by_parent.setdefault(path.parent, set()).add(path.name + ".")
    found = []
    for parent, leads in by_parent.items():
        try:
            entries = list(parent.iterdir())
        except OSError:
            continue
        for entry in entries:
            name = entry.name
            if not name.endswith(_SCRATCH_TAIL):
                continue
            for lead in leads:
                if name.startswith(lead) and _PID_ONLY.match(
                        name[len(lead):-len(_SCRATCH_TAIL)]):
                    found.append(entry)
                    break
    return found


def _unexpected(entry: Path, allowed: frozenset[str]) -> bool:
    """Whether one entry inside a directory is something the stage does not write there.

    A name in ``allowed``, or the scratch a write of that name goes through, and a plain
    file: anything else — a link, a junction, a directory, a name nobody put in the set —
    means the directory holds more than this stage's own work.

    An entry the host will not describe answers "unexpected", which is the refusing
    direction: the stage declines to take that directory back and names what stopped it.
    Nothing here can clear a directory for deletion on a question that went unanswered.
    """
    name = entry.name
    if name.endswith(_SCRATCH_TAIL):
        head = name[:-len(_SCRATCH_TAIL)]
        stem, _dot, pid = head.rpartition(".")
        name = stem if _PID_ONLY.match(pid) else name
    if name not in allowed:
        return True
    return entry.is_symlink() or _is_junction(entry) or not entry.is_file()


def _inside(root: Path, path: Path) -> bool:
    """Whether ``path`` really lands under ``root``, with **both sides resolved**.

    A leaf check cannot answer this. ``units/`` itself can be a link or a junction, and then
    every check on ``units/cluster-area-01`` passes while the directory it names lives
    outside the run — where a recursive removal is somebody else's files. The parent is
    resolved and the leaf name put back on it, so a link at the leaf is judged by where it
    SITS rather than by where it points; the leaf is never removed anyway.

    Both sides, because one is not enough: macOS answers ``/var`` with ``/private/var`` and
    Windows hands back 8.3 short names, so a run directory reached by either spelling would
    fail a comparison against its own unresolved self. Resolution also follows a junction on
    Windows, which is why there is one containment check here rather than a second kind of
    link test beside it.

    Anything that cannot be resolved answers False: a path this cannot place is a path it
    must not delete.
    """
    try:
        base = root.resolve()
        here = path.parent.resolve() / path.name
    except (OSError, RuntimeError, ValueError):
        return False
    return here != base and here.is_relative_to(base)


def _reclaim(what: str, root: Path, files: Sequence[Path] = (),
             dirs: Sequence[tuple[Path, frozenset[str]]] = (),
             scratch: Sequence[Path] = ()) -> None:
    """Take back one stage's own partial outputs, so the stage can run again.

    The marker in ``units.json`` is the commit record, so a stage entered while the marker
    still names the stage before it wrote nothing anybody has read. What it did leave —
    a claim file, a unit directory, a scratch file beside either — would otherwise refuse
    the re-run that the interruption asks for.

    ``files`` are removed with their scratch; ``scratch`` keeps its file and loses only the
    scratch beside it, which is what the marker itself needs: ``units.json`` is the one file
    a stage must never take back. ``dirs`` pairs a directory with **every name the stage
    writes inside it**.

    **A path's name does not make its contents the stage's.** A directory is removed only
    when everything in it is one of those names; anything else is refused by name, which is
    what an operator's notes inside a unit directory get. Trading that refusal for a
    recursive delete is the one trade this is not allowed to make: a refusal costs somebody
    a minute and a deletion costs them the file.

    **Every list is enumerated by the caller from what that stage is about to write.**
    Never a listing of the run directory: a sweep would delete whatever an operator or
    another stage put there.

    A link or a junction is left alone — it is not something a stage wrote, and the caller's
    own refusal names it — and so is anything this cannot remove, which is reported rather
    than worked around. **Every refusal is decided before the first removal**, so a reclaim
    that says no has changed nothing, which is what the stages around it promise too.
    """
    # Every refusal first, before a single removal, so a reclaim that says no has changed
    # nothing — the rule the stages around it already keep.
    #
    # Containment before anything else, because it is the one check whose failure is a
    # deletion outside the run directory altogether. It covers the scratch list too: that
    # search reads a path's PARENT, so a redirected ancestor would have it find, and
    # discard, files nobody here has any claim on.
    #
    # Every question about what a path IS below is tolerated in exactly one direction: a
    # path the host will not describe is passed over, never taken back. The removal is then
    # the thing that meets the same refusal, and it reports it by name — so nothing here is
    # ever deleted on an answer that was not given.
    for path in (*files, *(path for path, _allowed in dirs), *scratch):
        if not _inside(root, path):
            raise RunDirError(
                f"{path} resolves outside {root}, so it is not this stage's to take back; "
                f"a link or a junction on the way to it is the usual cause"
            )
    for path in files:
        if path.is_symlink() or _is_junction(path):
            continue
        if path.is_dir():
            raise RunDirError(
                f"{path} is a directory and {what} writes a file there, so it is not this "
                f"stage's to take back; move it aside and run again"
            )
    taking: list[Path] = []
    for path, allowed in dirs:
        if path.is_symlink() or _is_junction(path) or not path.is_dir():
            continue
        try:
            entries = sorted(path.iterdir())
        except OSError as exc:
            raise RunDirError(
                f"cannot read {path} to take it back: {_os_reason(exc)}"
            ) from exc
        held = [entry.name for entry in entries if _unexpected(entry, allowed)]
        if held:
            raise RunDirError(
                f"{path} holds {held[0]!r}, which {what} did not write there, so the "
                f"directory is not this stage's to take back; move it aside and run again"
            )
        taking.append(path)
    for leftover in _scratch_beside((*files, *scratch)):
        _discard(leftover)
    for path in files:
        if path.is_symlink() or _is_junction(path):
            continue
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        except OSError as exc:
            raise RunDirError(
                f"cannot take back {path}, which an interrupted {what} left behind: "
                f"{_os_reason(exc)}"
            ) from exc
    for path in taking:
        try:
            shutil.rmtree(path)
        except OSError as exc:
            raise RunDirError(
                f"cannot take back {path}, which an interrupted {what} left behind: "
                f"{_os_reason(exc)}"
            ) from exc


def _claim_stage(rundir: Path, stage: str) -> Path:
    """Claim the run directory for one stage, exclusively, before it reads the marker.

    **The claim is the exclusion, and it has to be held across everything the stage does.**
    Keying a refusal on the marker is what makes an interrupted stage resumable, and it is
    also what stops the marker excluding anybody: two stages can read the same marker and
    both decide the run directory is theirs to take back, and the second then deletes the
    first's committed payloads. Exclusive creation is the one thing here two processes
    cannot both do, so the stage takes it first — before the marker, before any reclaim —
    and releases it after the marker write.

    **What this does NOT survive is the stage being killed.** The file is left behind and
    the next run refuses by name, naming the file so an operator can clear it. Exclusion
    that the kernel drops when a process dies has to be held open for the owner's whole
    life, and one engine command is a stage, not a run: the process that owns a run from
    end to end is the one that can hold it. That is a real limit and it is stated rather
    than worked around, because a stage that reclaimed a claim it found would be back to
    two stages deleting each other's work.
    """
    lock = rundir / f"{stage}{STAGE_LOCK_SUFFIX}"
    try:
        with open(lock, "x", encoding="utf-8"):
            pass
    except FileExistsError as exc:
        raise RunDirError(
            f"{lock} exists, so another {stage} is working this run directory. If none is, "
            f"one was killed: delete that file and run again"
        ) from exc
    except OSError as exc:
        # The run directory itself is what is missing, nine times in ten. Said in the words
        # the marker read would have used, because that is the actual complaint and the
        # claim only happens to be what met it first. This picks a SENTENCE and nothing
        # else: the write above already failed, the stage is refused either way, and a
        # directory that cannot be described just gets the other of the two messages.
        if not rundir.is_dir():
            raise RunDirError(
                f"{rundir / UNITS_FILE_NAME} is missing; run plan first"
            ) from exc
        raise InventoryError(f"cannot write {lock}: {exc}") from exc
    return lock


# What ``plan`` writes into a run directory, and so all a failed ``plan`` may take back.
_PLAN_OUTPUTS = frozenset({"snapshot", "inventory.json", "areas.json", "units", "units.json", JOB_FILE_NAME})
_PLAN_FILES = ("inventory.json", "areas.json", "units.json", JOB_FILE_NAME)


def _remove_what_was_written(rundir: Path, *, keep_job: bool, made_rundir: bool) -> None:
    """Put a run directory back as a failed ``plan`` found it: the names ``plan`` writes, but
    the job file when the job was loaded from the run directory itself, this process's
    scratch files for them, and the directory itself when ``plan`` created it. Called only
    once ``plan`` holds the ``snapshot/`` claim, so no other plan wrote any of those names.
    Left behind, they refuse the re-run the failure asks for as a mixed run. Any other entry
    is left alone. Best-effort: the failure is what is reported."""
    try:
        entries = list(rundir.iterdir())
    except OSError:
        return
    scratch = {_temp_beside(rundir / name).name for name in _PLAN_FILES}
    # ``snapshot/`` goes last: while it stands no other plan can claim the directory, so
    # everything removed before it is this plan's own.
    entries.sort(key=lambda entry: entry.name == "snapshot")
    for entry in entries:
        if keep_job and entry.name == JOB_FILE_NAME:
            continue
        if entry.name not in _PLAN_OUTPUTS and entry.name not in scratch:
            continue
        try:
            if entry.is_dir() and not entry.is_symlink() and not _is_junction(entry):
                shutil.rmtree(entry)
            else:
                entry.unlink()
        except OSError:
            pass
    if made_rundir:
        try:
            rundir.rmdir()
        except OSError:
            pass


def _check_case_collisions(inventory: Inventory) -> None:
    """Refuse two inventoried paths that differ only by case, by name and before any
    write: distinct on a case-sensitive source, they are one path on a case-insensitive
    run volume, where the second copy overwrites the first while ``inventory.json``
    claims both. The rule the job already applies to its declared paths."""
    by_fold: dict[str, list[str]] = {}
    for entry in inventory.files:
        by_fold.setdefault(entry.path.casefold(), []).append(entry.path)
    for variants in by_fold.values():
        if len(variants) > 1:
            listed = ", ".join(repr(path) for path in variants)
            raise InventoryError(
                f"inventory paths differ only by case: {listed}; a case-insensitive run "
                f"volume cannot hold both copies, so exclude one or snapshot elsewhere"
            )
    # A file and a directory clash the same way: on such a volume the file `A` lands, and
    # the directory `a/` the next copy needs cannot be made.
    directories: dict[str, str] = {}
    for entry in inventory.files:
        parts = entry.path.split("/")
        for i in range(1, len(parts)):
            prefix = "/".join(parts[:i])
            directories.setdefault(prefix.casefold(), prefix)
    for entry in inventory.files:
        clash = directories.get(entry.path.casefold())
        if clash is not None:
            raise InventoryError(
                f"inventory path {entry.path!r} and directory {clash!r} differ only by case; "
                f"a case-insensitive run volume cannot hold both, so exclude one or snapshot "
                f"elsewhere"
            )


def _claim_snapshot(rundir: Path) -> None:
    """``plan``'s first write: create ``snapshot/`` exclusively. Two plans that both passed the
    run-directory check cannot both create it, so the one that loses stops here having
    written nothing, and a failure after this point can take back ``plan``'s outputs knowing
    no other plan wrote any of them."""
    snapshot = rundir / "snapshot"
    try:
        snapshot.mkdir(parents=True)
    except FileExistsError as exc:
        raise RunDirError(
            f"{snapshot} already exists; another plan is writing this run directory, so give "
            f"a fresh one"
        ) from exc
    except OSError as exc:
        raise RunDirError(f"cannot create {snapshot}: {exc}") from exc


def _source_readable(path: Path, rel: str) -> bool:
    """Whether ``path`` still reads from start to end, for telling which end of a failed
    copy failed."""
    try:
        _read_measuring(path, rel)
    except InventoryError:
        return False
    return True


def write_snapshot(job: Job, inventory: Inventory, rundir: str | Path, *,
                   claimed: bool = False, taken: str | None = None) -> Path:
    """Copy every inventoried file to ``<rundir>/snapshot/`` and write ``inventory.json``.

    A file copy, never a worktree: a worktree shares refs with the audited repository,
    and a reader that could reach them is not reading the snapshot alone. Each copy is
    re-digested as it is written and must match what was measured and partitioned; a
    file that changed in between is refused by name rather than snapshotted as something
    the areas were not built from. The tracked lockfiles outside the review are copied too,
    so a build in the snapshot installs what the repository pins (see :class:`Listing`);
    nothing else outside the review is. One of those that changed, vanished or stopped reading
    since it was measured is left out instead, and ``inventory.json`` lists only the ones
    copied.

    ``claimed`` says the caller has already checked the run directory and the case
    collisions and claimed ``snapshot/``, as ``plan`` does so that it knows which failures
    follow a write of its own.

    ``taken`` is when the snapshot was made, recorded so the report can say how old its
    RAW DATA is. It is the one clock in a run directory, and it is here deliberately: a
    report re-rendered weeks later says when it was rendered, and without this there was
    nothing to tell a reader the reading underneath it was a month old. The cost is that
    two runs over one tree no longer write byte-identical run directories — they differ on
    this field and nothing else, which is what the suite asserts rather than the equality
    it used to. Passed in rather than read here, so a caller that needs two runs to match
    can hand over the same stamp.
    """
    if claimed:
        rundir = Path(rundir)
    else:
        rundir = check_rundir(rundir, job)
        _check_case_collisions(inventory)
        _claim_snapshot(rundir)
    snapshot = rundir / "snapshot"
    in_scope = len(inventory.files)
    kept: list[FileEntry] = []
    for n, entry in enumerate(inventory.files + inventory.context):
        source = job.root / entry.path
        target = snapshot / entry.path
        tmp = _temp_beside(target)
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise InventoryError(f"cannot snapshot {entry.path}: {exc}") from exc
        sink = _create_scratch(tmp, entry.path, binary=True)
        try:
            with sink:
                try:
                    copied = _read_measuring(source, entry.path, sink)
                except InventoryError:
                    # The one error type covers both ends of the copy. Only the SOURCE
                    # failing is a reason to leave an out-of-scope file out; a run
                    # directory that cannot take the write refuses the plan, or a full
                    # disk would quietly drop the lockfile the copy is for.
                    if n < in_scope or _source_readable(source, entry.path):
                        raise
                    copied = None
            if copied is None or copied.sha256 != entry.sha256:
                if n >= in_scope:
                    # Nobody reads an out-of-scope file, so one that changed or went away
                    # since it was measured costs the build that file, as one the host
                    # would not read did when it was measured.
                    _discard(tmp)
                    continue
                raise InventoryError(
                    f"{entry.path} changed while the snapshot was being taken; the areas "
                    f"were built from the earlier bytes, so re-run plan"
                )
            shutil.copymode(source, tmp)
            os.replace(tmp, target)
        except OSError as exc:
            _discard(tmp)
            raise InventoryError(f"cannot snapshot {entry.path}: {exc}") from exc
        except ReviewPanelError:
            _discard(tmp)
            raise
        if n >= in_scope:
            kept.append(entry)
    write_json(rundir / "inventory.json", {
        "source": inventory.source,
        "files": [asdict(entry) for entry in inventory.files],
        "skipped": [asdict(entry) for entry in inventory.skipped],
        "excluded": list(inventory.excluded),
        "tree_sha256": inventory.tree_sha256,
        # Copied for the build, reviewed by nobody. The snapshot check reads these beside
        # ``files``, since a changed lockfile changes a build as much as a changed source.
        # Only those actually copied: the check compares the snapshot against this list.
        "context": [asdict(entry) for entry in kept],
        # When these bytes were taken. The only field in a run directory that is about the
        # moment rather than about the tree, and the report's subtitle is what it is for.
        "taken": taken or "",
        "commit": asdict(inventory.commit),
        # Escaped here and nowhere earlier: the comparison above needs the name the file
        # system spells, and this is the one place it has to become bytes a record can
        # carry. A lone surrogate from ``os.fsdecode`` reaches no UTF-8 write otherwise.
        "coverage": {
            "state": inventory.coverage.state,
            "root": _writable(inventory.coverage.root),
            "tracked_not_reviewed": [_writable(p) for p in inventory.coverage.tracked_not_reviewed],
            "reviewed_not_tracked": [_writable(p) for p in inventory.coverage.reviewed_not_tracked],
        },
    })
    return rundir


# --------------------------------------------------------------------------- #
# partition — bounded areas under one closure proof
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Ceiling:
    """What one reader is handed at most. Both bind: an area fits when it is under both."""

    lines: int
    bytes: int


# Starting values, tuned by the live smoke and changed together with the fixtures —
# never to meet a cost target.
DEFAULT_CEILING = Ceiling(lines=2000, bytes=200 * 1024)
# An auditor's ceiling is its own, and larger. What it reads is one subject and the tests
# about it -- typically one big file and one small one -- where an area is many files
# CHOSEN to fit. Measuring the first against the second flagged nearly every real job: a
# 2,988-line service class is the ordinary case for the trees this is pointed at, and a
# flag that always fires is one the eye learns to skip past.
#
# Derived from whatever ceiling the job is running under rather than written as a second
# literal, so narrowing one narrows both and the two cannot drift apart.
AUDIT_CEILING_MULTIPLE = 3


def audit_ceiling(ceiling: Ceiling) -> Ceiling:
    """What one coverage auditor may be asked to read, over the ceiling one area may hold."""
    return Ceiling(lines=ceiling.lines * AUDIT_CEILING_MULTIPLE,
                   bytes=ceiling.bytes * AUDIT_CEILING_MULTIPLE)
# The two model lanes. Every area is read by both: its lens list is dealt to them in turn,
# so a list of two gives each lane one lens and a list of four gives each two.
LANES = ("A", "B")
# One auditor per lane, each asking what shape of input no test constructs — for EACH area
# the plan marked audited, so an auditor is bounded the way a reader is. Which areas those
# are differs by partition mode and is settled in :func:`partition`; see
# :func:`auditor_areas`.
AUDITORS = len(LANES)


@dataclass(frozen=True)
class SubjectLink:
    """One test that is about a source file, and which rule said so — ``name`` or
    ``mention``. Carried rather than collapsed because the two are not equally certain,
    and an auditor told which is which can weigh a test that merely names its subject
    against one whose own name declares it."""

    test: str
    how: str


@dataclass(frozen=True)
class TestedFile:
    """One source file in an area that some test in scope is about, with those tests.

    The tests need not sit in this area, and usually do not: a Java test lives under
    ``src/test`` and its subject under ``src/main``, which the file-mode packing puts in
    different areas nearly every time.
    """

    path: str
    tests: tuple[SubjectLink, ...]


@dataclass(frozen=True)
class PlannedArea:
    """One area as ``plan`` emits it. ``subject`` is the declared name in subject mode and
    ``None`` in file mode; ``part`` numbers the pieces of a subject that was split, and
    is ``None`` when it was not. ``lines`` and ``bytes`` include ``also_read``; ``files``
    alone count toward closure. ``oversize`` marks the one exception to the ceiling: a
    single file over it, which is never split.

    ``tested`` is this area's own source files that a test in scope is about, and is what a
    coverage auditor is given. It is separate from ``also_read`` on purpose: ``also_read``
    is read by the area's READERS and is bounded by their ceiling, so putting test subjects
    in it would make readers open another area's code and would be refused outright beside
    a file that is already over the ceiling on its own.

    ``audited`` is whether a coverage auditor is dispatched for this area. Decided at
    partition time, where the job's mode is known, and carried here so that every later
    caller asks one question instead of re-deriving a rule that differs by mode.
    """

    id: str
    subject: str | None
    part: int | None
    files: tuple[str, ...]
    also_read: tuple[str, ...]
    lenses: tuple[str, ...]
    lines: int
    bytes: int
    oversize: bool
    tested: tuple[TestedFile, ...] = ()
    audited: bool = False
    # What the auditor actually has to READ: its tested files plus the tests about them,
    # which is not the area's own total — a test usually lives in another area, so an
    # auditor's reading load is its share of this area plus files from elsewhere.
    #
    # Over the ceiling this is FLAGGED and never refused, the same answer the readers get
    # for a single file over the ceiling. A run whose largest class is 2,988 lines against a
    # 2,000-line ceiling is the ordinary case for the trees this is pointed at, and refusing
    # it would mean the one file most worth auditing is the one file that cannot be.
    audit_lines: int = 0
    audit_bytes: int = 0
    audit_oversize: bool = False


def _total(paths, sizes: dict[str, tuple[int, int]]) -> tuple[int, int]:
    return sum(sizes[p][0] for p in paths), sum(sizes[p][1] for p in paths)


def _fits(total: tuple[int, int], room: Ceiling) -> bool:
    return total[0] <= room.lines and total[1] <= room.bytes


def _pack(files, sizes: dict[str, tuple[int, int]], room: Ceiling) -> list[tuple[tuple[str, ...], bool]]:
    """The file-mode rule, over any file set.

    Directories are visited in sorted order of their path components. A directory whose
    whole subtree fits is placed into the first open area with room, else opens a new
    one; a directory over the ceiling is split by its own children, files and
    subdirectories alike, in the same order. A single file over the ceiling becomes its
    own area flagged oversize and receives nothing else; a file is never split. Two
    implementations following this paragraph derive the same areas.
    """
    tree: dict = {}
    for path in files:
        node = tree
        parts = path.split("/")
        for name in parts[:-1]:
            node = node.setdefault(name, {})
        node[parts[-1]] = path
    areas: list[list[str]] = []
    totals: list[tuple[int, int]] = []
    oversize: list[bool] = []

    # Both walks are iterative: a legal path over a thousand directories deep otherwise runs
    # out of stack. Each keeps the order the recursive form visited in.
    def files_under(node) -> list[str]:
        out: list[str] = []
        stack = [iter(node.values())]
        while stack:
            child = next(stack[-1], None)
            if child is None:
                stack.pop()
            elif isinstance(child, dict):
                stack.append(iter(child.values()))
            else:
                out.append(child)
        return out

    def place(group: list[str]) -> None:
        size = _total(group, sizes)
        for i, area in enumerate(areas):
            if not oversize[i] and _fits((totals[i][0] + size[0], totals[i][1] + size[1]), room):
                area.extend(group)
                totals[i] = (totals[i][0] + size[0], totals[i][1] + size[1])
                return
        areas.append(list(group))
        totals.append(size)
        oversize.append(False)

    def visit(top: dict) -> None:
        stack: list[tuple[dict, object]] = [(top, None)]
        while stack:
            node, names = stack[-1]
            if names is None:
                whole = files_under(node)
                if _fits(_total(whole, sizes), room):
                    place(whole)
                    stack.pop()
                    continue
                names = iter(sorted(node))
                stack[-1] = (node, names)
            name = next(names, None)
            if name is None:
                stack.pop()
                continue
            child = node[name]
            if isinstance(child, dict):
                stack.append((child, None))
            elif _fits(sizes[child], room):
                place([child])
            else:
                areas.append([child])
                totals.append(sizes[child])
                oversize.append(True)

    visit(tree)
    return [(tuple(sorted(area)), flag) for area, flag in zip(areas, oversize)]


def _assign_subjects(job: Job, sizes: dict[str, tuple[int, int]], excluded) -> list[tuple]:
    """Every inventoried file to exactly one declared area, by path or prefix; the
    unclaimed to the remainder, else refused by name; a file claimed twice refused by
    name. Returns ``(subject, files, also_read, lenses)`` per area that holds a file."""
    paths = sorted(sizes)
    claims: dict[str, list[tuple[Area, str]]] = {p: [] for p in paths}
    for i, area in enumerate(job.areas):
        for j, entry in enumerate(area.paths):
            hits = [p for p in paths if matches(entry, p)]
            if not hits:
                raise PartitionError(
                    f"field 'areas[{i}].paths[{j}]' {entry!r} matches nothing in the "
                    f"inventory; a prefix must name a directory in it and a path a file"
                )
            for p in hits:
                claims[p].append((area, entry))
    remainder = next((area for area in job.areas if area.remainder), None)
    owned: dict[str, list[str]] = {area.name: [] for area in job.areas}
    for p in paths:
        claimants = claims[p]
        if len(claimants) > 1:
            listed = " and ".join(f"'{area.name}' ({entry})" for area, entry in claimants)
            raise PartitionError(
                f"{p!r} is claimed by two areas: {listed}; areas are disjoint, and a path "
                f"read from beside another area belongs in 'also_read'"
            )
        if claimants:
            owned[claimants[0][0].name].append(p)
        elif remainder is not None:
            owned[remainder.name].append(p)
        else:
            raise PartitionError(
                f"{p!r} is in no area and the job declares no remainder; claim it, "
                f"exclude it, or mark one area 'remainder'"
            )
    groups = []
    for i, area in enumerate(job.areas):
        own = set(owned[area.name])
        also: set[str] = set()
        for j, entry in enumerate(area.also_read):
            field = f"areas[{i}].also_read[{j}]"
            hits = [p for p in paths if matches(entry, p)]
            shut = [p for p in excluded if matches(entry, p)]
            if shut:
                raise PartitionError(
                    f"field '{field}' {entry!r} names an excluded path ({shut[0]}); an "
                    f"excluded path is outside the review's scope, so no reader is sent to it"
                )
            if not hits:
                raise PartitionError(
                    f"field '{field}' {entry!r} matches nothing in the inventory"
                )
            mine = [p for p in hits if p in own]
            if mine:
                raise PartitionError(
                    f"field '{field}' {entry!r} names the area's own file ({mine[0]}); "
                    f"'also_read' is for paths that belong to another area"
                )
            also.update(hits)
        if not own:
            # Only a remainder can get here — every other area matched at least one path.
            continue
        lenses = job.lenses if area.lenses is None else area.lenses
        groups.append((area.name, tuple(owned[area.name]), tuple(sorted(also)), lenses))
    return groups


def _tests_and_sources(paths: Sequence[str]) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """The in-scope paths split into the tests and everything else. A test is never another
    test's subject: admitting one would let an auditor raise a coverage gap against a test
    file, which is not a question anybody asked."""
    tests = tuple(p for p in paths if is_test_path(p))
    return tests, tuple(p for p in paths if not is_test_path(p))


def settle_tests(tests: Sequence[str], sources: Sequence[str],
                 read: Callable[[str], str]) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """The split again, with the DIRECTORY signal allowed to yield.

    ``is_test_path`` answers true when any parent directory is named `test`, `tests`,
    `spec`, `specs` or `__tests__`. That is right for most Python trees, where
    `tests/helpers.py` carries no marker in its name and is test support all the same —
    and wrong wherever `tests` is an ordinary package name. A tree whose dev controllers
    live in a `tests` package had 94 auditors planned over them, and every one of those
    controllers was also missing from the source set the readers were partitioned over.

    So the directory signal stands unless BOTH of the things that could confirm it are
    absent: the file's own name says nothing (:func:`_named_as_test`), and it is about no
    file in scope. A file that fails both is production code in a directory with a
    misleading name, and it becomes a source.

    Only the weak candidates are read, and only against the definite sources — a test is
    never a test's subject, so a candidate resolving to another candidate would not make
    it a test either. One pass settles it: demotion only ENLARGES the source set, which
    can add links and never take one away, so nothing demoted here would resolve on a
    second look.
    """
    weak = [path for path in tests if not _named_as_test(path)]
    if not weak:
        return tuple(tests), tuple(sources)
    resolved = subject_map(weak, sources, read)
    demoted = {path for path in weak if not resolved[path]}
    if not demoted:
        return tuple(tests), tuple(sources)
    return (tuple(p for p in tests if p not in demoted),
            tuple(sorted({*sources, *demoted})))


def _assign_tested(areas: Sequence[PlannedArea],
                   subjects: dict[str, tuple[tuple[str, str], ...]],
                   sizes: dict[str, tuple[int, int]],
                   ceiling: Ceiling = DEFAULT_CEILING) -> tuple[PlannedArea, ...]:
    """Each area's own source files that a test is about, with those tests.

    Inverts the map. It is derived per TEST and applied per SOURCE, because several tests of
    one file and one test of several files are both ordinary, and a mapping stored the other
    way round would have to overwrite one of them.
    """
    by_source: dict[str, list[SubjectLink]] = {}
    for test, links in sorted(subjects.items()):
        for source, how in links:
            by_source.setdefault(source, []).append(SubjectLink(test=test, how=how))
    out = []
    for area in areas:
        tested = tuple(TestedFile(path=path, tests=tuple(by_source[path]))
                       for path in area.files if path in by_source)
        # Each file once. A test about two of this area's files is listed under both and
        # read once, so counting it twice would report a load nobody carries.
        reading = {entry.path for entry in tested}
        reading |= {link.test for entry in tested for link in entry.tests}
        load = (sum(sizes[p][0] for p in reading), sum(sizes[p][1] for p in reading))
        out.append(replace(area, tested=tested, audited=bool(tested),
                           audit_lines=load[0], audit_bytes=load[1],
                           audit_oversize=bool(tested)
                           and not _fits(load, audit_ceiling(ceiling))))
    return tuple(out)


def _read_from(root: str | Path) -> Callable[[str], str]:
    return lambda path: _read_source(Path(root) / path)


def partition(job: Job, inventory: Inventory, ceiling: Ceiling = DEFAULT_CEILING,
              subjects: dict[str, tuple[tuple[str, str], ...]] | None = None) -> tuple[PlannedArea, ...]:
    """Divide the inventory into areas under the ceiling, in the job's mode, and prove
    closure before returning. In subject mode the declared areas are the starting set and
    an oversize one is split inside itself by the file-mode rule, every part carrying the
    area's ``also_read``; an ``also_read`` set that cannot fit beside its area is refused
    by name rather than trimmed, and so is one beside a single file over the ceiling,
    since an oversize file reads alone.

    **Where a coverage auditor goes is settled here**, because this is where the mode is
    known. In file mode nothing in the job says which test covers which file, so it is
    derived: every test is resolved to the source files it is about, and an area is audited
    when it CONTAINS one of those files. In subject mode the job author already said it,
    through ``also_read``, and the rule is left as it was — an area is audited when it owns
    a test file.

    ``subjects`` is the derived map, injectable so a caller that already holds one — or a
    test that wants a particular one — need not have it recomputed from the tree.
    """
    sizes = {entry.path: (entry.lines, entry.bytes) for entry in inventory.files}
    if job.partition == "file":
        groups = [(None, tuple(sorted(sizes)), (), job.lenses)]
    else:
        groups = _assign_subjects(job, sizes, inventory.excluded)
    planned: list[tuple] = []
    for subject, files, also_read, lenses in groups:
        extra = _total(also_read, sizes)
        room = Ceiling(lines=ceiling.lines - extra[0], bytes=ceiling.bytes - extra[1])
        # Every part carries the whole also_read set, so a file that fits the ceiling on
        # its own but not beside also_read has no part it can go in.
        crowded = [p for p in files if _fits(sizes[p], ceiling) and not _fits(sizes[p], room)]
        # `< 0`: a set that exactly fills the ceiling still leaves room for an empty owned
        # file, and any owned file with content is `crowded`.
        if also_read and (room.lines < 0 or room.bytes < 0 or crowded):
            raise PartitionError(
                f"'also_read' of area {subject!r} ({', '.join(also_read)}) is {extra[0]} "
                f"lines / {extra[1]} bytes and cannot fit under the ceiling "
                f"({ceiling.lines} lines / {ceiling.bytes} bytes) beside "
                f"{crowded[0] if crowded else 'the area'}; trim it or split the area"
            )
        parts = _pack(files, sizes, room)
        for k, (part_files, oversize) in enumerate(parts, 1):
            if oversize and also_read:
                # The flagged exception is one file by itself; ``also_read`` beside it
                # would make a multi-file payload over the ceiling with no flag to say so.
                alone = sizes[part_files[0]]
                raise PartitionError(
                    f"'also_read' of area {subject!r} ({', '.join(also_read)}) cannot "
                    f"read beside {part_files[0]}, which is over the ceiling on its own "
                    f"({alone[0]} lines / {alone[1]} bytes); an oversize file reads alone, "
                    f"so drop the 'also_read' or split the area around it"
                )
            total = _total(part_files + also_read, sizes)
            part = k if subject is not None and len(parts) > 1 else None
            planned.append((subject, part, part_files, also_read,
                            lenses, total, oversize))
    width = max(2, len(str(len(planned))))
    areas = tuple(
        PlannedArea(
            id=f"area-{n:0{width}d}", subject=subject, part=part, files=files,
            also_read=also_read, lenses=lenses, lines=total[0], bytes=total[1],
            oversize=oversize,
        )
        for n, (subject, part, files, also_read, lenses, total, oversize) in enumerate(planned, 1)
    )
    check_closure(areas, list(sizes))
    if job.partition != "file":
        # Subject mode's link is authored, so nothing is derived over it. The auditor rule
        # stays what it has always been there: an area is audited when it owns a test file,
        # and the payload reaches that test's subject through the job's own ``also_read``.
        return tuple(replace(area, audited=any(is_test_path(p) for p in area.files))
                     for area in areas)
    tests, sources = _tests_and_sources(sorted(sizes))
    if subjects is None:
        read = _read_from(job.root)
        # Settled BEFORE the map is built, so one split feeds both the map and everything
        # that reads `tested` off the planned areas. A caller that injected a map has
        # already decided the split and is left alone.
        tests, sources = settle_tests(tests, sources, read)
        subjects = subject_map(tests, sources, read)
    return _assign_tested(areas, subjects, sizes, ceiling)


def check_closure(areas: Sequence[PlannedArea], files: Sequence[str]) -> None:
    """Both directions: every inventoried file in exactly one area, and no area holding a
    file the inventory does not. A violation names the path; nothing is ever dropped."""
    expected = set(files)
    owner: dict[str, str] = {}
    for area in areas:
        for path in area.files:
            if path not in expected:
                raise PartitionError(
                    f"{path!r} in {area.id} is not in the inventory; an area holds only "
                    f"inventoried files"
                )
            if path in owner:
                raise PartitionError(
                    f"{path!r} is in two areas, {owner[path]} and {area.id}; areas are "
                    f"pairwise disjoint"
                )
            owner[path] = area.id
    missing = sorted(expected - set(owner))
    if missing:
        more = f" (and {len(missing) - 1} more)" if len(missing) > 1 else ""
        raise PartitionError(
            f"{missing[0]!r} is in no area{more}; every inventoried file is read by one"
        )


def write_job_copy(rundir: Path, job: Job) -> None:
    """The run directory is self-describing: ``route`` and ``report`` read the problem
    statement from ``<rundir>/job.json`` and never from the conversation. The prose writes
    the job there before ``plan``; a job loaded from anywhere else is copied in, byte for
    byte, so a run reads the same whichever way it was started. ``check_rundir`` has
    already established that a ``job.json`` present here IS the job being loaded."""
    target = rundir / JOB_FILE_NAME
    if _resolve(job.path, "job file", RunDirError) == target:
        return
    try:
        data = job.path.read_bytes()
    except OSError as exc:
        raise JobError(f"cannot read job file {job.path}: {exc}") from exc
    tmp = _temp_beside(target)
    sink = _create_scratch(tmp, str(target), binary=True)
    try:
        with sink:
            sink.write(data)
        os.replace(tmp, target)
    except OSError as exc:
        _discard(tmp)
        raise InventoryError(f"cannot write {target}: {exc}") from exc


def write_areas(rundir: Path, job: Job, areas: Sequence[PlannedArea],
                ceiling: Ceiling = DEFAULT_CEILING) -> None:
    write_json(rundir / "areas.json", {
        "partition": job.partition,
        "ceiling": asdict(ceiling),
        "areas": [asdict(area) for area in areas],
    })


def _derived_subjects(areas: Sequence[PlannedArea]) -> dict[str, tuple[tuple[str, str], ...]]:
    """The subject map back out of the planned areas, for printing.

    Recovered from what was planned rather than recomputed from the tree: the preview must
    show the links the run will actually use, and a second derivation is a second thing that
    can disagree with the first.
    """
    out: dict[str, list[tuple[str, str]]] = {}
    for area in areas:
        for entry in area.tested:
            for link in entry.tests:
                out.setdefault(link.test, []).append((entry.path, link.how))
    return {test: tuple(sorted(links)) for test, links in out.items()}


# Said before anything runs, on the preview and on a run started with `--go` alike, because
# a lockfile outside the review is still in the snapshot for the build, and any worker may
# open it.
COPIED_OUTSIDE_SCOPE_LINE = (
    "{n} lockfile(s) outside the review are copied into the snapshot so a build there "
    "installs the versions the repository pins; any worker may open them")
# The files that pin what a build installs. Copied even when excluded or unlisted: each
# names package versions and where to fetch them, and a build without it resolves versions
# the repository never pinned.
LOCKFILE_NAMES = frozenset({
    "package-lock.json", "npm-shrinkwrap.json", "yarn.lock", "pnpm-lock.yaml", "bun.lock",
    "bun.lockb", "Cargo.lock", "poetry.lock", "Pipfile.lock", "uv.lock", "pdm.lock",
    "Gemfile.lock", "composer.lock", "go.sum", "mix.lock", "pubspec.lock", "Podfile.lock",
    "packages.lock.json", "gradle.lockfile", "flake.lock",
})


def is_lockfile(rel: str) -> bool:
    return rel.rsplit("/", 1)[-1] in LOCKFILE_NAMES


def copied_outside_scope_line(context: Sequence[str]) -> str:
    """The line saying what the snapshot carries beside the review, or ``""`` for nothing."""
    return COPIED_OUTSIDE_SCOPE_LINE.format(n=len(context)) if context else ""


def preview(job: Job, inventory: Inventory, areas: Sequence[PlannedArea],
            ceiling: Ceiling = DEFAULT_CEILING) -> str:
    """What the owner sees before any unit is dispatched: N areas, the readers each gets,
    the total reading units — readers summed over the areas plus the auditors — the map from
    each test to the code it was resolved to, and the one capability probe beside them."""
    n = len(areas)
    readers = sum(len(area.lenses) for area in areas)
    parts = {s: sum(1 for a in areas if a.subject == s) for s in {a.subject for a in areas}}
    out = [
        f"{n} area{'s' if n != 1 else ''} from {len(inventory.files)} files "
        f"({inventory.source}); {len(inventory.excluded)} excluded, "
        f"{len(inventory.skipped)} skipped"
    ]
    copied = copied_outside_scope_line([e.path for e in inventory.context])
    if copied:
        out.append(copied)
    for area in areas:
        label = area.subject or ""
        if area.part is not None:
            label += f" (part {area.part} of {parts[area.subject]})"
        out.append(
            f"  {area.id}  {label:<28} {len(area.lenses)} readers  {area.lines:>6} lines  "
            f"{area.bytes / 1024:>7.1f} KiB  {len(area.files)} files"
            + ("  OVERSIZE: one file over the ceiling, never split" if area.oversize else "")
        )
        if area.audit_oversize:
            out.append(
                f"    OVERSIZE AUDIT: its auditor reads {area.audit_lines} lines "
                f"({len(area.tested)} tested files and their tests), over the "
                f"{audit_ceiling(ceiling).lines}-line auditor ceiling; flagged, never split"
            )
    present = {area.subject for area in areas}
    for area in job.areas:
        if area.remainder and area.name not in present:
            out.append(f"  remainder {area.name!r} received no file and is not an area")
    auditors = auditor_plan_of(inventory, job.coverage is False)
    audited = auditor_areas(areas) if auditors.dispatch else ()
    # Whether subjects were DERIVED at all, which is the job's mode and not whether any
    # happened to resolve. A file-mode run where nothing resolved is the case most in need
    # of both the map and an accurate reason, and keying either on the result would withhold
    # them exactly there.
    deriving = job.partition == "file"
    derived = _derived_subjects(areas)
    # A test that resolved to nothing is in the map with an empty link, so the line below
    # names it rather than leaving the owner to notice an absence.
    if deriving:
        for entry in inventory.files:
            if is_test_path(entry.path):
                derived.setdefault(entry.path, ())
    running = len(audited) * AUDITORS
    out.append(f"reading units: {readers + running} = {readers} readers + {running} auditors")
    # The auditor count on its own line, with the rule that produced it. Buried in the sum
    # above, 94 auditors nobody wanted read as a large run rather than as a round to turn
    # off, and the owner found out after paying for them.
    out.append(f"coverage auditors: {running} — {len(audited)} of {len(areas)} areas, "
               f"{AUDITORS} per area"
               + ("; audited because a test in scope is about a file in it" if deriving
                  else "; audited because the area owns a test file"))
    # Said before anything is dispatched, because this is the owner's cheapest moment to
    # fix it: a run whose tests were left out of scope is one edit to the job away from the
    # answer the auditor exists to give.
    #
    # Per area where any area has a test, and once for the run where none does. They are
    # different facts: the first says this area's code has no test beside it, and the second
    # says the job put no test in scope at all — which is usually a scoping mistake and
    # never a statement about the code.
    if auditors.dispatch:
        covered = {area.id for area in audited}
        for area in areas:
            if area.id not in covered:
                out.append(f"  no auditor for {area.id}: no file in it is "
                           + ("the subject of a test in scope" if deriving
                              else "named as a test"))
        # The map itself, one line per test, because this is the owner's cheapest moment to
        # disagree with it. A link derived from a MENTION is a guess by construction — a
        # test names what it mocks as well as what it exercises — and the only way to tell
        # a wrong guess from a right one is for somebody who knows the code to read the
        # line. A job that wants to say it by hand says so in subject mode.
        for test, links in sorted(derived.items()):
            if links:
                out.append(f"  {test} → " + ", ".join(
                    f"{source} ({SUBJECT_HOW_SAID[how]})" for source, how in links))
            else:
                out.append(f"  {test} → test in scope, subject not in scope: not audited")
    else:
        out.append(f"  no auditor: {_auditor_absence(auditors)}")
    # Counted apart from the reading units because it is not one: the probe reads no area
    # and raises no finding. It is still an agent the owner is about to pay for, so the
    # preview says so before anything is dispatched.
    out.append("one capability probe, dispatched in the same round")
    return "\n".join(out) + "\n"


# --------------------------------------------------------------------------- #
# units — one payload per reader and per auditor, blind by construction
# --------------------------------------------------------------------------- #
# A payload is built from four things and nothing else: the brief, the problem statement
# verbatim, the area assignment as snapshot-relative paths, and the lens. Blindness is a
# property of the file — a test reconstructs the bytes from those four inputs — so no
# other unit's output, no file contents and no source path can be in it. The lens is the
# LAST section, so everything before it is byte-identical across an area's readers.
READER_KIND = "reader"
AUDITOR_KIND = "auditor"
# One unit of round one answers a different question from every other: not what is wrong
# here, but whether this tree can be built and its tests run at all. It raises no finding
# and is routed nowhere; its answer reaches every verifier payload and is recorded in the
# route record, which is where the report will read it, so that a count of established
# findings is never read as a count of validated ones.
PROBE_KIND = "probe"
PROBES = 1
PROBE_UNIT_ID = f"probe-{LANES[0]}"
PROBE_SCHEMA_NAME = "probe-schema.json"
UNITS_DIR = "units"
# The dispatcher's own directory. The engine never READS it — a unit directory holds
# everything the protocol names — and touches it in exactly one place: a redo removes
# what it told the dispatcher to put there for the units it takes back.
DISPATCH_DIR = "dispatch"
UNITS_FILE_NAME = "units.json"
PAYLOAD_NAME = "payload.md"
SCHEMA_NAME = "schema.json"
# Everything a stage writes inside one unit directory, and so all such a directory may hold
# for a stage to take it back. A result or an error file is the DISPATCHER's, and notes
# somebody left are nobody's but theirs: either one means the directory is more than an
# interrupted stage's leftovers, and it is refused rather than deleted.
UNIT_CONTENTS = frozenset({PAYLOAD_NAME, SCHEMA_NAME})
READER_SCHEMA_NAME = "reader-schema.json"
PROBLEM_HEADING = "\n## Problem statement\n"
AREA_HEADING = "\n## Area "
FILES_LINE = "\nYour files, relative to the snapshot (read every one):\n"
ALSO_READ_HEADING = "\n### Also read\n"
ALSO_READ_LINE = "\nAnother area's for responsibility; a finding here is filed under that area:\n"
LENS_HEADING = "\n## Lens\n"
RESULT_SCHEMA_HEADING = "\n## Result schema\n"


def _schema_section(schema: dict | None) -> list[str]:
    """The unit's result schema, inline, as payload pieces; none when no schema is given.
    A unit runs with the snapshot, or a copy of it, as its working directory, and nothing
    points it at the run directory — so the schema it must satisfy travels inside its
    payload rather than being somewhere it would have to be told to look. Nothing STOPS a
    unit opening that directory; the adapters bound where it may write, not what it may
    read, and the property this skill claims is narrower than "no payload carries another
    unit's output" — a verification payload carries what a reader raised, a clustering
    payload carries what the verifiers concluded, and a synthesis payload carries both,
    because each round is BUILT on the settled output of the one before it. What no payload
    may carry is a SIBLING's answer to the question this unit is being asked. The copy
    beside the payload is for the dispatcher and the engine. Each renderer places it by position
    while assembling, never by searching rendered text: the problem statement and a
    candidate's failure are verbatim owner and worker text and may hold any heading.
    Constant across a run, so it moves no blindness or byte-equality property."""
    if schema is None:
        return []
    body = json.dumps(schema, indent=2, sort_keys=True)
    return [RESULT_SCHEMA_HEADING, "\nThe one JSON object your reply ends with must be valid against this:\n\n",
            "```json\n", body, "\n```\n",
            "\nIf the instruction that spawned you names a reply file, that file is your answer "
            "INSTEAD of the object above: write the same object to that path, then reply with only "
            "that path and the number of items you wrote. A message can be cut short where a file "
            "is not.\n"]


# The reader brief's lens catalog: one ``### <lens>`` entry per lens it describes. The
# renderer copies the assigned entry under LENS_HEADING and drops the catalog, so a
# payload never carries the other reader's question — blindness is a property of the
# payload bytes, and a hint about the other lens is a hint about the other reader.
LENSES_HEADING = "\n## Lenses\n"
_LENS_ENTRY_RE = re.compile(r"^### (.+)$", re.MULTILINE)
AUDITOR_FILES_LINE = "\nThe files of your area, relative to the snapshot:\n"
AUDITOR_ALSO_READ_LINE = ("\nAnother area owns these, and a test of yours exercises them: "
                          "follow the test to its subject here, and file the finding in the "
                          "subject.\n")
TEST_INVENTORY_HEADING = "\n## Test inventory\n"
NO_TESTS_SENTENCE = "No file in this area is named as a test."
# What a file-mode auditor is given instead of its area's whole file list: the area's own
# source files that some test in scope is about, each with those tests.
#
# The untested files of the same area are deliberately ABSENT. An auditor that can see one
# beside a tested file writes the finding this bounding exists to stop — "no test constructs
# this input", true of every line of it, and true because the file has no test rather than
# because of anything the auditor read.
AUDITOR_TESTED_LINE = ("\nThe files of your area that a test in scope is about, and the "
                       "tests that are about them. Your findings go in THESE files and "
                       "nowhere else; a test listed here may be listed for another auditor "
                       "too, which is not your concern.\n")
# The by-name rule for the auditor's test inventory, stated in coverage-auditor.md in the
# same words: a test-named directory component, or a test-named file.
_TEST_DIRS = frozenset({"test", "tests", "spec", "specs", "__tests__"})
_TEST_NAME_RE = re.compile(r"^(test_.*|.*_test\..+|.*\.(test|spec)\..+|conftest\.py)$")


class CompanionError(ReviewPanelError):
    """A file the engine ships beside itself — a brief, the reader schema — is missing or
    unreadable. Names the file: the installer ships the skill directory whole, so an
    absence means a partial copy, not a bad job."""


@dataclass(frozen=True)
class Unit:
    """One work unit of the reading round. ``lens`` is ``None`` for an auditor, which is
    given an area but not a question to read it through; both are ``None`` for the probe,
    which reads no area at all."""

    id: str
    kind: str
    area: str | None
    lane: str
    lens: str | None


def is_test_path(path: str) -> bool:
    """Whether a snapshot-relative path is named as a test, by the rule above."""
    parts = path.split("/")
    return any(part in _TEST_DIRS for part in parts[:-1]) or bool(_TEST_NAME_RE.match(parts[-1]))


# How a test file's name says what it is a test OF, in the shapes the ecosystems here
# actually use: `FooTest.java` and `FooSpec.scala`, `test_foo.py`, `foo_test.go`,
# `foo.test.ts`. Stripped from the file's stem, longest marker first so `Tests` is not
# read as `Test` with a stray `s`.
#
# `is_test_path` is a WIDER rule than this one and deliberately stays so: a Java test is
# recognized by sitting under `src/test/java`, and its name matches nothing here. The two
# answer different questions — whether a file is a test, and what it is a test of — and a
# file can be the first without being the second.
_SUBJECT_MARKERS_TRAILING = ("ITCase", "Tests", "Test", "Specs", "Spec", "IT",
                             "_tests", "_test", "_spec", ".test", ".spec")
_SUBJECT_MARKERS_LEADING = ("test_", "spec_")

def _named_as_test(path: str) -> bool:
    """Whether the file's OWN NAME says it is a test, by either of the two name rules.

    ``_TEST_NAME_RE`` covers the spellings that carry no subject — `conftest.py`,
    `test_*` — and a stem that loses something to :func:`_subject_stem` carries a marker
    that names one. Deliberately not the directory rule: this is the question
    :func:`settle_tests` asks when the directory is the only evidence there is.
    """
    return (bool(_TEST_NAME_RE.match(path.rsplit("/", 1)[-1]))
            or _subject_stem(path) != _stem_of(path))


SUBJECT_BY_NAME = "name"
SUBJECT_BY_MENTION = "mention"
# How the link was made, in the auditor's own terms rather than the engine's field names.
# A mention is the weaker claim and says so: a test names what it mocks as well as what it
# exercises, and an auditor that knows which it is holding can weigh it.
SUBJECT_HOW_SAID = {SUBJECT_BY_NAME: "named for it", SUBJECT_BY_MENTION: "mentions it"}


def _subject_stem(path: str) -> str:
    """A test file's stem with its test marker removed, or the stem unchanged when it
    carries none. ``src/test/java/LeaseServiceMoveOutTest.java`` gives
    ``LeaseServiceMoveOut``."""
    stem = _stem_of(path)
    for marker in _SUBJECT_MARKERS_LEADING:
        if stem.startswith(marker):
            return stem[len(marker):]
    for marker in _SUBJECT_MARKERS_TRAILING:
        if stem.endswith(marker) and len(stem) > len(marker):
            return stem[:-len(marker)]
    return stem


def _stem_of(path: str) -> str:
    """A file name up to its first extension, with a leading dot kept.

    ``.eslintrc.json`` is ``.eslintrc`` and not the empty string. The distinction matters
    because an empty stem begins every other string: it would make a config file a candidate
    subject for every test in the tree, and ``\\b\\b`` matches at any word boundary, so it
    would be MENTIONED by every test as well.
    """
    name = path.split("/")[-1]
    cut = name.index(".", 1) if "." in name[1:] else len(name)
    return name[:cut]


def _extension(path: str) -> str:
    name = path.split("/")[-1]
    return name[name.rindex("."):] if "." in name[1:] else ""


def _shared_lead(a: str, b: str) -> int:
    """How many leading directory components two paths have in common."""
    first, second = a.split("/")[:-1], b.split("/")[:-1]
    shared = 0
    for one, other in zip(first, second):
        if one != other:
            break
        shared += 1
    return shared


def subjects_by_name(test: str, sources: Sequence[str]) -> tuple[str, ...]:
    """The source file a test's NAME says it is about: the source whose stem begins the
    test's stripped stem, ranked.

    Three keys, in order, and each is there because the one above it leaves a tie a real
    tree produces. **The same language first**: ``foo_test.go`` beside a ``web/foo.ts``
    and a ``go/foo.go`` matches both stems, and only one of them is in the language the
    test is written in. **Then the longest stem**: a tree holding ``Lease.java`` and
    ``LeaseService.java`` answers ``LeaseServiceMoveOutTest`` with both, and the shorter
    is a coincidence of spelling. **Then the nearest directory**, which settles a project
    that keeps one class name in two packages.

    Extension is a preference and not a filter, so a Kotlin test of a Java class still
    finds it. That link is visible in the plan preview like every other, which is where a
    wrong one is meant to be caught.

    At most one file comes back: a name encodes one subject, which is the whole reason
    :func:`subjects_by_mention` runs beside this rather than after it.
    """
    stem = _subject_stem(test)
    # An empty stem on either side establishes nothing: it begins every string, so it would
    # match everything rather than something. Guarded here as well as in :func:`_stem_of`,
    # because a test file may legitimately reduce to nothing once its marker is stripped —
    # ``test_.py`` does — and that is a test with no subject, not a test of every file.
    hits = [s for s in sources if _stem_of(s) and stem.startswith(_stem_of(s))] if stem else []
    if not hits:
        return ()
    return (max(hits, key=lambda s: (_extension(s) == _extension(test),
                                     len(_stem_of(s)),
                                     _shared_lead(s, test))),)


@dataclass(frozen=True)
class _MockDialect:
    """How one language spells "this class is a stand-in, not the thing under test".

    A table entry rather than code so adding a language is a row. What it cannot be is one
    rule for every language, and the reason is worth stating because it looks like an
    oversight: **Java names a mocked class as a TOKEN and Python and JavaScript name it as
    a STRING** -- `@Mock PaymentService x` against `patch("app.PaymentService")` and
    `jest.mock("./PaymentService")`. Blanking string literals is what makes Java correct,
    because a literal holding `@Mock` otherwise votes; doing it to Python would erase every
    mock target in the file. So `blank_literals` is per dialect, and the two families
    genuinely differ.

    A language with no entry filters nothing and every mention survives, which is the
    behavior these rules narrow rather than a gap in them.
    """

    comments: tuple[str, ...]
    # Where the strings are. EVERY row needs these whether or not it blanks them, because
    # :func:`_strip_comments` matches literals and comments together so a comment opener
    # inside a string cannot start a comment. A row that lists none gets a comment rule
    # that erases the rest of a line holding a URL or a `#`.
    literals: tuple[str, ...]
    # Whether a literal's CONTENTS also stop voting. Orthogonal to the above: the strings
    # are located either way, and this says only what they are replaced by.
    blank_literals: bool
    # Patterns for text that names a type without running it. Each must be **bounded to the
    # statement terminator** -- `[^;\n]*`, never `[^\n]*$` or `.*` -- or a dropped import
    # takes any statement sharing its line with it, and a construction is gone from the
    # predicate entirely. Each must equally begin at :data:`_STATEMENT_START` rather than at
    # `^`, or the SECOND import on a line survives and keeps every link by itself. The two
    # rules are one rule: a statement, not a line.
    drop_lines: tuple[str, ...]
    split: str
    # ``{stem}`` templates, filled by :func:`_for_stem`. A statement matching one DECLARES
    # the class as a stand-in.
    mocks: tuple[str, ...]
    # ``{stem}`` templates. A statement matching one EXERCISES the class, and settles the
    # whole statement whatever else is in it: one declaration can hold a mock and a real
    # instance, and the mock's marker must not speak for the constructor beside it.
    constructs: tuple[str, ...]


_C_COMMENTS = (r"/\*.*?\*/", r"//[^\n]*")
_C_LITERALS = (r'"(?:\\.|[^"\\\n])*"', r"'(?:\\.|[^'\\\n])*'")

# Where a statement may begin: a line start, or just past a terminator. Every `drop_lines`
# pattern opens with this. `^` alone reaches only the FIRST statement on a line, so
# `import {Helper} from "./support"; import {Thing} from "./Thing";` leaves the second import
# standing -- and an import names the class while matching no `mocks` pattern, so that one
# surviving piece keeps the link and the mock rule stops firing for the whole file.
#
# A lookbehind rather than a consuming `(?:^|;)`, and the reason is the delimiter the NEXT
# match needs. Consumed, it is gone: over `import a.A; import b.B; import c.C;` the first
# match eats the `;` that ` import b.B` would have had to start from, so the MIDDLE import
# survives, names its class, matches no `mocks` pattern and keeps the link by itself -- the
# very defect this anchor exists to close, reappearing one import along. Matching nothing
# leaves every terminator available to the match after it. Fixed-width, which is all Python
# allows, and `re.MULTILINE` is what makes the `^` half mean a line rather than the text.
_STATEMENT_START = r"(?:^|(?<=;))"

_JAVA_DIALECT = _MockDialect(
    comments=_C_COMMENTS,
    literals=(r'"""(?:\\.|[^\\])*?"""',) + _C_LITERALS,
    blank_literals=True,
    # A plain import names a type and says nothing about running it. `import static` names a
    # MEMBER, and a test that static-imports one calls it, so that line stays and counts.
    # `.*?;` stops at the terminator, which is what makes it safe to delete: a declaration
    # sharing the line survives. `.*?` rather than `[^\n]*?` because a Java import may wrap.
    drop_lines=(_STATEMENT_START + r"[ \t]*(?:import(?!\s+static\b)|package)\b.*?;",),
    split=r"[;{}]",
    mocks=(
        # A package qualifier is the same annotation written out in full.
        r"@(?:[\w.]*\.)?Mock\b", r"@(?:[\w.]*\.)?MockBean\b",
        # The call must be the WHOLE call: `mock(X.class, CALLS_REAL_METHODS)` runs the real
        # implementation through the mock, which Mockito documents, so a second argument
        # means this is no longer a plain stand-in.
        r"\bmock\s*\(\s*(?:[\w.]*\.)?{stem}\s*\.class\s*\)",
        r"\bMockito\s*\.\s*mock\s*\(\s*(?:[\w.]*\.)?{stem}\s*\.class\s*\)",
    ),
    # `@Spy` is deliberately not a mock: a spy is a real instance with some methods stubbed,
    # so its unstubbed code runs. Nor is `@InjectMocks`, which names the subject the mocks
    # are injected INTO. Neither is listed here either -- a statement matching no mock
    # pattern already keeps the link.
    constructs=(r"\bnew\s+(?:[\w.]*\.)?{stem}\s*\(",),
)

# Python patches a dotted PATH inside a string, so literals must survive to be READ -- but
# they are still listed, because the comment rule has to know where they are. A `#` inside a
# string is not a comment, and reading it as one erases the rest of the line along with any
# construction standing on it.
#
# Statements are split on newlines because Python has no terminator. A call wrapped over
# lines therefore splits, and its pieces stop reading as one mock declaration, which again
# keeps the link.
_PYTHON_DIALECT = _MockDialect(
    comments=(r"#[^\n]*",),
    # Triple quotes first, or `"""a"""` reads as an empty string followed by loose text.
    literals=(r'"""(?:\\.|[^\\])*?"""', r"'''(?:\\.|[^\\])*?'''") + _C_LITERALS,
    blank_literals=False,
    drop_lines=(_STATEMENT_START + r"[ \t]*import\s+[\w., \t]+",
                _STATEMENT_START + r"[ \t]*from\s+[\w.]+\s+import\b[^;\n]*"),
    split=r"[\n]",
    mocks=(
        r"\bpatch(?:\.object)?\s*\(\s*['\"][\w.]*\b{stem}\b",
        r"\bpatch(?:\.object)?\s*\(\s*{stem}\b",
        r"\bmocker\s*\.\s*patch\s*\(\s*['\"][\w.]*\b{stem}\b",
        r"\bcreate_autospec\s*\(\s*{stem}\b",
        r"\b(?:MagicMock|Mock|AsyncMock)\s*\(\s*spec(?:_set)?\s*=\s*{stem}\b",
    ),
    # Python constructs without a keyword, so a bare call IS construction -- and a bare call
    # is also how it is mocked, which is why no rule here matches `{stem}(` on its own. What
    # each of these adds is the POSITION the result goes to: assigned, returned, or handed
    # straight to another call. The third is the one the other rows get for free, since they
    # spell construction with `new` or a declarator and an unanchored search finds that
    # anywhere in the statement. Without it, `consume({stem}())` matches nothing and a
    # statement holding a mock and a real instance is settled by the mock -- a DROP, and the
    # file it names then goes to no auditor at all.
    #
    # The trailing `\(` is what keeps this off the mock spellings: `patch({stem})`,
    # `patch.object({stem}, ...)` and `Mock(spec={stem})` all name the class without calling
    # it. Widening to a bare `[(,\[]\s*{stem}\b` would match every one of them.
    constructs=(r"=\s*(?:[\w.]*\.)?{stem}\s*\(", r"\breturn\s+(?:[\w.]*\.)?{stem}\s*\(",
                r"[(,\[]\s*(?:[\w.]*\.)?{stem}\s*\("),
)

# `jest.mock` and `vi.mock` name a MODULE PATH in a string, so literals survive here too.
# One dialect serves JavaScript, TypeScript and both React extensions: `.jsx` and `.tsx`
# are those languages with JSX syntax, and React has no mocking idiom of its own.
_JS_DIALECT = _MockDialect(
    comments=_C_COMMENTS,
    # A template literal is a string too, and the one most likely to hold a URL. Left out,
    # the `//` in it opens a comment and the rest of the line goes with it.
    literals=_C_LITERALS + (r"`(?:\\.|[^`\\])*`",),
    blank_literals=False,
    # Three narrowings, and they are one rule: what gets deleted has to be an IMPORT, not a
    # line, and not a statement with an import-shaped word in it.
    #
    # Bounded at the `;`, so the construction in `import {X} from "./X"; const r = new X();`
    # survives -- and the braces leave with the import text, which matters more here than
    # elsewhere, since `split` includes `{` and `}` and an import left standing breaks into
    # a bare `X` piece that matches no mock pattern and keeps every link. `import` must be
    # followed by WHITESPACE, because `import(` is a call and
    # `import("./h").then(() => new Thing())` is a construction. And a declaration counts
    # only when its initializer IS a `require` call: one that merely contains a require may
    # be passing it to a constructor, as `const real = new Thing(require("./config"))` does.
    # The tail stops at a comma as well, so the second declarator in
    # `const cfg = require("./c"), real = new Thing()` survives its neighbour.
    drop_lines=(_STATEMENT_START + r"[ \t]*import[ \t]+[^;\n]*",
                _STATEMENT_START + r"[ \t]*(?:const|let|var)\b[^;\n]*?(?<![=!<>])=[ \t]*"
                r"require\s*\([^;,\n]*"),
    split=r"[;{}\n]",
    mocks=(
        r"\b(?:jest|vi)\s*\.\s*mock\s*\(\s*['\"][^'\"]*\b{stem}\b",
        r"\b(?:jest|vi)\s*\.\s*mocked\s*\(\s*{stem}\b",
        r"\bsinon\s*\.\s*(?:stub|createStubInstance|mock)\s*\(\s*{stem}\b",
        r"\btd\s*\.\s*replace\s*\(\s*['\"][^'\"]*\b{stem}\b",
    ),
    constructs=(r"\bnew\s+(?:[\w.]*\.)?{stem}\s*\(",),
)

# The weakest fit of the six, and it says so. GoogleMock has no annotation: a mock is a
# SUBCLASS, `class MockFoo : public Foo { MOCK_METHOD(...) };`, so the only place the real
# class is named is the inheritance clause. `NiceMock<MockFoo>` never names `Foo` at all --
# `MockFoo` has no word boundary before `Foo`, so it is not even a mention and nothing here
# has to rule it out. A hand-rolled fake that does not follow the `Mock` naming convention
# is invisible to this, which keeps the link.
_CPP_DIALECT = _MockDialect(
    comments=_C_COMMENTS,
    literals=(r'R"\([^)]*\)"',) + _C_LITERALS,
    blank_literals=True,
    # The one row that keeps a bare `^`: a preprocessor directive owns its whole line by the
    # language's own rule, so nothing can share one with it in either direction.
    drop_lines=(r"^[ \t]*#\s*include\b[^\n]*$",),
    split=r"[;{}]",
    mocks=(r"\bclass\s+\w*Mock\w*\s*:\s*public\s+(?:[\w:]*::)?{stem}\b",
           r"\bstruct\s+\w*Mock\w*\s*:\s*public\s+(?:[\w:]*::)?{stem}\b"),
    constructs=(r"\bnew\s+(?:[\w:]*::)?{stem}\s*\(",
                r"\bmake_(?:unique|shared)\s*<\s*(?:[\w:]*::)?{stem}\s*>",
                r"\b(?:[\w:]*::)?{stem}\s+\w+\s*[({]"),
)

# Keyed by what :func:`_fence_language` answers, so the extension table has one spelling in
# this file and cannot drift into two.
_MOCK_DIALECTS = {
    "java": _JAVA_DIALECT,
    "python": _PYTHON_DIALECT,
    "javascript": _JS_DIALECT, "typescript": _JS_DIALECT,
    "jsx": _JS_DIALECT, "tsx": _JS_DIALECT,
    "cpp": _CPP_DIALECT, "c": _CPP_DIALECT,
}


def _for_stem(pattern: str, word: str) -> str:
    """Fill a dialect's ``{stem}`` placeholder.

    A plain replace rather than :meth:`str.format`, because these templates are REGEXES and
    regexes are full of braces: a repetition like ``{2,3}`` makes ``format`` raise, and so
    does a character class holding one -- C++'s ``[({]`` did, on every C++ file, before a
    test caught it. Whoever adds the next dialect should not have to know to escape them.
    """
    return pattern.replace("{stem}", word)


def _alternation(patterns: tuple[str, ...]) -> str:
    """Several patterns as one, so a scan takes the EARLIEST match rather than the first
    pattern's. Both callers depend on that and one of them silently did not have it."""
    return "|".join(f"(?:{pattern})" for pattern in patterns)


def _blank_literals(text: str, dialect: _MockDialect) -> str:
    """The same text with every string literal emptied, quotes kept.

    Used for the `constructs` view of a piece, never for the `mocks` one. A row that keeps
    its literals keeps them because a class NAMED inside a string is how that language
    spells a mock -- `patch("app.Thing")` is the whole of Python's detection. A class
    CONSTRUCTED inside a string is a different matter: `Mock(spec=Thing, name="Thing()")`
    builds nothing, and reading the text as code keeps a link the test never earned.

    A piece can hold an unterminated quote, since a retained literal is still split on the
    row's terminators: a pattern needing both quotes then matches nothing and the piece is
    used as it stands, which is the answer it had before this ran.

    ONE alternation, exactly as :func:`_strip_comments` does it, and for the same reason:
    the first opener has to win. Applied one form after another, the double-quote pattern
    runs before the single-quote one and matches from a `"` inside one single-quoted string
    to the `"` inside the next, emptying the code between them --
    `withArgs('"').returns(new Thing('"'))` loses its construction, which is a DROP.
    """
    if not dialect.literals:
        return text
    return re.sub(_alternation(dialect.literals), '""', text, flags=re.DOTALL)


def _strip_comments(text: str, dialect: _MockDialect) -> str:
    """Comments out, strings located first -- the order a lexer reads them.

    Comments cannot be stripped by themselves. A `//` inside a URL and a `#` inside a
    string are not comment openers, and a pattern that reads them as one erases the rest of
    the line along with any construction standing on it -- a DROP, the direction that
    silently loses a file to every auditor.

    Blanking the literals first answers that only where the row can afford it, and two of
    the four cannot: Java and C++ name a mocked class as a token, but Python and JavaScript
    name it INSIDE a string, so blanking there erases the markers themselves and the rule
    never fires. So literals and comments are matched in ONE pass with the literals first,
    and `blank_literals` decides only what a literal is replaced BY. Every row then gets a
    comment rule that cannot fire inside a string, whichever kind of row it is.

    The two alternatives are named groups so the branch is read off the match rather than
    inferred from the text; a dialect pattern using either name would collide, which is why
    neither name is a word a regex for source code would carry.
    """
    parts = []
    if dialect.literals:
        parts.append(f"(?P<literal>{_alternation(dialect.literals)})")
    if dialect.comments:
        parts.append(f"(?P<comment>{_alternation(dialect.comments)})")
    if not parts:
        return text
    has_literals = bool(dialect.literals)

    def replace(match: "re.Match[str]") -> str:
        if has_literals and match.group("literal") is not None:
            return '""' if dialect.blank_literals else match.group(0)
        return " "

    return re.sub("|".join(parts), replace, text, flags=re.DOTALL)


def _statements(text: str, dialect: _MockDialect) -> list[str]:
    """Text as declaration-sized pieces, with what must not vote removed first.

    Read per PHYSICAL LINE this answered wrongly in four ordinary ways at once: an import of
    the mocked class names it and carries no marker, so it kept every link -- and a mocked
    collaborator from another package is always imported; an annotation on its own line
    above the field, the commonest formatting Java has, was disconnected from the type it
    annotates; two declarations sharing a line let the first one's marker speak for the
    second's class; and a comment naming an annotation decided the answer for code beside it.

    Order is what makes this correct: strings are located BEFORE comments are stripped,
    which is the order a lexer reads them, and :func:`_strip_comments` is where that
    happens. Then the imports go, and only then is the text cut into pieces -- so a
    `drop_lines` pattern deletes text no piece will ever see, which is why every one of
    them stops at the statement terminator rather than the end of the line.

    What this cannot see anywhere is a brace or terminator INSIDE an annotation's arguments
    -- `@MockBean(classes = {Foo.class})` splits, and the pieces stop reading as one mock
    declaration. That keeps a link that could have been dropped, and closing it means
    matching parentheses rather than splitting on them.
    """
    text = _strip_comments(text, dialect)
    for pattern in dialect.drop_lines:
        text = re.sub(pattern, "", text, flags=re.MULTILINE | re.DOTALL)
    return [piece for piece in re.split(dialect.split, text) if piece.strip()]


def _mock_only(text: str, stem: str, language: str) -> bool:
    """Whether every statement naming ``stem`` declares it as a stand-in.

    A test that only mocks a class is evidence of nothing about it, and an auditor handed
    the link can only answer in the negative -- at length, and about code the test never
    runs. One lane returned 30 such findings from a single link of this shape, each one
    resting on the observation that the test never instantiates the class.

    **The quantifier is `all`, and the direction is deliberate.** A test that mocks a class
    in one case and builds it in another does exercise it, so the question is whether the
    mock declarations account for EVERY statement that names it. Everything this cannot read
    as a mock declaration keeps the link, and that asymmetry is the whole safety argument:
    no regex parses a language, so an unparsed shape must cost some findings a reader can
    see and dismiss rather than a file silently lost to every auditor.

    A language with no dialect filters nothing.
    """
    dialect = _MOCK_DIALECTS.get(language)
    if dialect is None:
        return False
    word = re.escape(stem)
    naming = [piece for piece in _statements(text, dialect)
              if re.search(rf"\b{word}\b", piece)]
    if not naming:
        return False
    # The two halves read the same pieces through different eyes, and they have to. `mocks`
    # reads the piece whole, because a row that keeps its literals names its mock target
    # inside one. `constructs` reads code, so it reads the piece with the literals emptied:
    # a constructor spelled inside a string builds nothing.
    built = [_blank_literals(piece, dialect) for piece in naming]
    if any(re.search(_for_stem(pattern, word), piece)
           for pattern in dialect.constructs for piece in built):
        return False
    return all(any(re.search(_for_stem(marker, word), piece) for marker in dialect.mocks)
               for piece in naming)


def subjects_by_mention(text: str, sources: Sequence[str],
                        test: str = "") -> tuple[str, ...]:
    """The source files a test's TEXT names, each stem as a whole word.

    Deliberately over-inclusive, and run BESIDE the name rule rather than only when that
    finds nothing. One test file exercising two classes can only ever name one of them in
    its own name, so a rule that stops at the first answer misses the second every time —
    and a missed subject is a file audited by nobody.

    What it costs is the other direction: a test names the classes it imports as well as
    the one it exercises. That is why every link is carried with how it was derived, and
    why the plan preview prints the map before anything is dispatched.

    The one shape that is always wrong is dropped rather than carried: a class this test
    only ever mocks. See :func:`_mock_only` and :class:`_MockDialect`. It is dropped and not
    marked, because a third link kind would have to be rendered, explained in the preview
    and carried through ``PlannedArea``, all for a link nothing should act on.

    ``test`` is the test file's own path, which says how to read its text. Absent, the
    sources' extension stands in: the text is about that code, so it is almost always the
    same language, and a wrong guess only costs the filter — every mention survives.
    """
    language = _fence_language(test) or _fence_language(sources[0] if sources else "")
    return tuple(s for s in sources
                 if _stem_of(s) and re.search(rf"\b{re.escape(_stem_of(s))}\b", text)
                 and not _mock_only(text, _stem_of(s), language))


def subject_map(tests: Sequence[str], sources: Sequence[str],
                read: Callable[[str], str]) -> dict[str, tuple[tuple[str, str], ...]]:
    """Every test to the source files it is about, each with how the link was derived.

    ``sources`` is the in-scope files that are NOT tests: a test is never another test's
    subject, and admitting one would let an auditor raise gaps against a test file.

    A test that resolves to nothing comes back with an empty tuple rather than being left
    out, so a caller can tell a test this could not place from one it was never given.
    """
    out: dict[str, tuple[tuple[str, str], ...]] = {}
    for test in tests:
        named = subjects_by_name(test, sources)
        links = [(s, SUBJECT_BY_NAME) for s in named]
        # Read whether or not the name answered. A name encodes ONE subject, so stopping
        # here would lose every further file a test covers — which is a source file audited
        # by nobody. Planning already reads each file once to measure and digest it, so this
        # is a second read of the tests alone.
        #
        # **A file this cannot read stops the stage, and empty text is never stood in for
        # it.** Empty text is spelled exactly like a test that mentions nothing: the mention
        # pass finds no subject, the source files only this test covers go to no auditor,
        # and nothing anywhere says a file went unread. Worse where the answer is acted on
        # — :func:`settle_tests` calls this on the candidates whose own NAME said nothing,
        # so resolving to nothing there is what turns a test into a source file, and an
        # unreadable one stops being a test at all while the coverage it stands for is
        # audited by nobody. ``measure_files`` read this same tree moments earlier under
        # exactly this rule; a second reader of it does not get a softer one.
        try:
            text = read(test)
        except OSError as exc:
            _stop_on_a_storage_fault(f"the test {test}", exc)
            raise InventoryError(f"cannot read {test}: {_os_reason(exc)}") from exc
        links += [(s, SUBJECT_BY_MENTION)
                  for s in subjects_by_mention(text, sources, test)
                  if s not in named]
        out[test] = tuple(links)
    return out


def auditor_areas(areas: Sequence[PlannedArea]) -> tuple[PlannedArea, ...]:
    """The areas a coverage auditor is dispatched for, as :func:`partition` decided.

    The first rule of this panel is that a unit gets one bounded chunk it can read closely,
    and the auditor was the single exception — it received every path in the tree. Two
    auditors given that same list and that same question returned thirty-seven findings and
    nine, with no code site in common: a question wide enough that two careful readers of it
    agree on nothing is a question with no stopping rule in it, and each run answers a
    slightly different one. Bounding it is what makes two auditors comparable, area by area.

    **What bounds it is the tested code, not the test file.** Dispatching to whichever area
    happened to HOLD a test put the auditor in the wrong place nearly every time: over one
    run, seven of ten tests sat in an area that did not contain the class they test, so five
    areas were asked about code no test in scope targets — 206 of 315 findings amounting to
    "this file has no tests" — while the area holding the class four of those tests are
    about got no auditor, because no test file sat in it.

    The rule is not re-derived here. It differs by partition mode, and the mode is known in
    exactly one place; asking the area is what keeps one answer from being computed twice.
    """
    return tuple(area for area in areas if area.audited)


# What the auditor round is, once the question has been asked. The auditor is the one unit
# whose access is the whole manifest, and its question is narrower than its access: which
# inputs the tests never construct. With no test in scope that question has no answer, and
# an agent holding the whole manifest and no answerable question has the means to invent a
# second general defect sweep — unbounded, unpartitioned, and duplicating what the readers
# already did blind. Two runs of one job then differ by the size of whatever it improvised.
#
# So the answer is decided HERE and the unit is not dispatched. A brief telling the agent
# not to improvise relies on the agent obeying it; not dispatching removes the opportunity.
AUDIT_DISPATCH = "dispatch"
AUDIT_TESTS_OUT_OF_SCOPE = "tests-out-of-scope"
AUDIT_NO_TESTS = "no-tests"
AUDIT_SCOPE_UNKNOWN = "scope-unknown"
# The fifth answer, and the only one that is not about the tree: the job said not to ask.
AUDIT_DECLINED = "declined"
AUDIT_DECLINED_WORDS = "the job declined the coverage question"


@dataclass(frozen=True)
class AuditorPlan:
    """Whether the auditors run, and what the report says when they do not.

    ``elsewhere`` holds the test files the repository tracks that this run did not review,
    which is the difference between a scoping mistake the owner can fix in seconds and a
    project that has no tests. Both read as "no tests in the inventory" from inside the
    payload, and only one of them is a statement about the code.
    """

    state: str
    elsewhere: tuple[str, ...] = ()

    @property
    def dispatch(self) -> bool:
        return self.state == AUDIT_DISPATCH


def plan_auditors(paths: Iterable[str], coverage_state: str,
                  tracked_not_reviewed: Sequence[str],
                  declined: bool = False) -> AuditorPlan:
    """The auditor round's answer, from the reviewed paths and the repository comparison.

    Primitive arguments rather than an inventory, because this is asked twice from two
    shapes of the same facts: ``plan`` holds an :class:`Inventory` and the report holds the
    snapshot it wrote. One rule, two callers, and no second copy that can drift from it.

    Four answers, not three. Where the repository comparison did not run — outside a
    repository, or wherever git declined — nothing here knows whether tests exist outside
    the reviewed set, and reporting that as "no test file was found" would be the very
    mistake this function exists to stop: reading an empty inventory as a claim about the
    code. It says the scope is unknown instead.
    """
    # First, because it is the one answer that is about the JOB rather than the tree. A
    # run over a tree full of tests whose owner does not want the question asked is not a
    # run with no tests in it, and the four answers below are all about the tree.
    if declined:
        return AuditorPlan(AUDIT_DECLINED)
    if any(is_test_path(path) for path in paths):
        return AuditorPlan(AUDIT_DISPATCH)
    if coverage_state != "computed":
        return AuditorPlan(AUDIT_SCOPE_UNKNOWN)
    elsewhere = tuple(path for path in tracked_not_reviewed if is_test_path(path))
    if elsewhere:
        return AuditorPlan(AUDIT_TESTS_OUT_OF_SCOPE, elsewhere)
    return AuditorPlan(AUDIT_NO_TESTS)


def _auditor_absence(plan: AuditorPlan) -> str:
    """Why no auditor ran, in one clause, framed by whoever prints it.

    One wording for the preview and the report. The owner sees it before dispatch, where it
    is one edit to the job away from being fixed, and a reader sees the same sentence in a
    report weeks later — and a sentence kept in two places is a sentence that drifts.
    """
    if plan.state == AUDIT_DISPATCH:
        # Reached only from the reporting side, and only for a run directory this engine did
        # not write: a file in scope is named as a test, so the round had a question, and the
        # listing carries no unit to have put it to. Stated as what is on disk rather than
        # guessed at, because nothing here knows what made it.
        return "no auditor is listed for this run, though a file in scope is named as a test"
    if plan.state == AUDIT_DECLINED:
        return (f"{AUDIT_DECLINED_WORDS}, so no auditor was planned however many tests "
                f"this tree has")
    if plan.state == AUDIT_TESTS_OUT_OF_SCOPE:
        return (f"no file in scope is named as a test, and the repository tracks "
                f"{_plural(len(plan.elsewhere), 'test file')} that "
                f"{'was' if len(plan.elsewhere) == 1 else 'were'} not in scope")
    if plan.state == AUDIT_NO_TESTS:
        return "no file in scope is named as a test, and the repository tracks none"
    return ("no file in scope is named as a test, and the reviewed set was not compared "
            "against what the repository tracks, so whether tests exist elsewhere is "
            "unknown")


def auditor_plan_of(inventory: Inventory, declined: bool = False) -> AuditorPlan:
    """:func:`plan_auditors` over an :class:`Inventory`, for the planning side."""
    return plan_auditors((entry.path for entry in inventory.files),
                         inventory.coverage.state, inventory.coverage.tracked_not_reviewed,
                         declined)


def auditor_plan_of_snapshot(inventory: dict, declined: bool = False) -> AuditorPlan:
    """:func:`plan_auditors` over the snapshot the report reads, for the reporting side.

    ``declined`` comes off the job the run directory keeps, because it is the one input
    here that is not a fact about the tree and the snapshot therefore cannot hold it.
    """
    coverage = inventory.get("coverage") or {}
    return plan_auditors((entry["path"] for entry in inventory["files"]),
                         coverage.get("state", "unavailable"),
                         coverage.get("tracked_not_reviewed", ()),
                         declined)


def assign_units(areas: Sequence[PlannedArea], *, auditors: bool = True) -> tuple[Unit, ...]:
    """One reader per (area, lens), the lenses dealt to the lanes in turn, then one auditor
    per lane for each area the plan marked audited — where there is a question to put to
    them at all — and one capability probe. Reader and auditor counts are both derived here
    and declared nowhere."""
    units: list[Unit] = []
    for area in areas:
        dealt = {lane: 0 for lane in LANES}
        for i, lens in enumerate(area.lenses):
            lane = LANES[i % len(LANES)]
            dealt[lane] += 1
            units.append(Unit(id=f"{area.id}-{lane}{dealt[lane]}", kind=READER_KIND,
                              area=area.id, lane=lane, lens=lens))
    for area in auditor_areas(areas) if auditors else ():
        for lane in LANES:
            units.append(Unit(id=f"audit-{area.id}-{lane}", kind=AUDITOR_KIND,
                              area=area.id, lane=lane, lens=None))
    # Exactly one probe, whatever the partition: the question it answers is about the tree,
    # not about an area, so a second would ask the same thing twice. It takes the first lane
    # because some lane has to run it and nothing about the answer depends on which.
    units.append(Unit(id=PROBE_UNIT_ID, kind=PROBE_KIND, area=None, lane=LANES[0], lens=None))
    return tuple(units)


def _companion(name: str) -> Path:
    return Path(__file__).resolve().parent / name


def load_brief(name: str) -> str:
    """The brief ``references/<name>.md`` shipped beside the engine, verbatim."""
    path = _companion("references") / f"{name}.md"
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise CompanionError(f"cannot read the {name} brief {path}: {exc}") from exc


def load_schema(name: str) -> dict:
    """A result schema shipped beside the engine, as the object a unit's ``schema.json``
    is a copy of."""
    path = _companion(name)
    try:
        schema = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        raise CompanionError(f"cannot read the schema {path}: {exc}") from exc
    if not isinstance(schema, dict):
        raise CompanionError(f"the schema {path} is not a JSON object")
    return schema


def load_reader_schema() -> dict:
    return load_schema(READER_SCHEMA_NAME)


@dataclass(frozen=True)
class Companions:
    """What the engine ships beside itself and every payload is built from. Loaded before
    the first write, so a partial copy of the skill refuses with nothing on disk."""

    reader_brief: str
    auditor_brief: str
    probe_brief: str
    reader_schema: dict
    probe_schema: dict


def load_companions() -> Companions:
    return Companions(
        reader_brief=load_brief("reader"),
        auditor_brief=load_brief("coverage-auditor"),
        probe_brief=load_brief("probe"),
        reader_schema=load_reader_schema(),
        probe_schema=load_schema(PROBE_SCHEMA_NAME),
    )


def _listed(paths) -> str:
    return "\n" + "".join(f"- {p}\n" for p in paths)


def split_reader_brief(brief: str) -> tuple[str, dict[str, str]]:
    """The reader brief as its shared body and its lens catalog, ``{lens: text}`` keyed
    by the entry's heading casefolded. The catalog is the ``## Lenses`` section and
    runs to the next ``## `` heading or the end; a brief without one has no catalog."""
    body, sep, rest = brief.partition(LENSES_HEADING)
    if not sep:
        return brief, {}
    end = rest.find("\n## ")
    catalog, after = (rest, "") if end == -1 else (rest[:end], rest[end:])
    entries = _LENS_ENTRY_RE.split(catalog)[1:]  # [name, text, name, text, ...]
    lenses = {name.strip().casefold(): text.strip("\n")
              for name, text in zip(entries[::2], entries[1::2])}
    return body + after, lenses


def render_reader_payload(brief: str, problem: str, area: PlannedArea, lens: str, schema: dict | None = None) -> str:
    """The whole of a reader's payload, from exactly these four inputs and the run's
    constant result schema, which sits just before the lens so the lens stays last. Of
    the brief's lens catalog only the assigned entry is rendered; a lens the catalog
    does not describe is rendered by name alone, which the brief tells the reader how to
    read."""
    body, catalog = split_reader_brief(brief)
    out = [body.rstrip("\n"), "\n", PROBLEM_HEADING, "\n", problem, "\n",
           AREA_HEADING, area.id, "\n", FILES_LINE, _listed(area.files)]
    if area.also_read:
        out += [ALSO_READ_HEADING, ALSO_READ_LINE, _listed(area.also_read)]
    out += _schema_section(schema)
    out += [LENS_HEADING, "\n", lens, "\n"]
    text = catalog.get(lens.strip().casefold())
    if text:
        out += ["\n", text, "\n"]
    return "".join(out)


def render_auditor_payload(brief: str, problem: str, area: PlannedArea,
                           inventory: Inventory, ceiling: Ceiling = DEFAULT_CEILING, schema: dict | None = None) -> str:
    """The whole of one auditor's payload: the brief, the problem, and — where the plan
    derived them — the area's own source files that a test in scope is about, each with the
    tests that are about it and how each was matched, then those tests as a flat list. Where
    it did not, the area's files, the files another area owns that its tests reach, and
    which of the two lists are named as tests. No file contents either way.

    ONE area, and the same area a pair of readers was given. :func:`auditor_areas` says why
    the whole manifest stopped being the payload; what follows from it here is that an
    auditor's finding and a reader's finding at one site arrive from the same area, so the
    clustering round can merge them or keep them apart on purpose rather than by accident.

    Measured against the ceiling like an area's payload, and with its schema section in it,
    since that is what the auditor is handed: a list of paths is not free, and an area of a
    thousand one-line test files lists each of them twice. Over the ceiling is a refusal by
    name — a limit stops the run and says so; it never trims."""
    lines = {entry.path: entry.lines for entry in inventory.files}
    out = [brief.rstrip("\n"), "\n", PROBLEM_HEADING, "\n", problem, "\n",
           AREA_HEADING, area.id, "\n"]
    if area.tested:
        out.append(AUDITOR_TESTED_LINE)
        out.append("\n")
        for entry in area.tested:
            out.append(f"- {entry.path} ({lines[entry.path]} lines)\n")
            for link in entry.tests:
                out.append(f"    - {link.test} ({lines[link.test]} lines) — "
                           f"{SUBJECT_HOW_SAID[link.how]}\n")
        # The tests again as a flat list, because the auditor is asked to open every one of
        # them and a nested list is a thing to be read rather than worked through. Named
        # once each: a test about two of these files appears under both above.
        tests = sorted({link.test for entry in area.tested for link in entry.tests})
        out.append(TEST_INVENTORY_HEADING)
        out.append(_listed(f"{p} ({lines[p]} lines)" for p in tests))
    else:
        out.append(AUDITOR_FILES_LINE)
        out.append(_listed(f"{p} ({lines[p]} lines)" for p in area.files))
        if area.also_read:
            out += [ALSO_READ_HEADING, AUDITOR_ALSO_READ_LINE,
                    _listed(f"{p} ({lines[p]} lines)" for p in area.also_read)]
        # Both lists, because a test reaching its subject through ``also_read`` is exactly
        # the case the section exists for, and an inventory that named only the owned half
        # would tell the auditor to read a test it had not been shown.
        tests = [p for p in sorted(set(area.files) | set(area.also_read)) if is_test_path(p)]
        out.append(TEST_INVENTORY_HEADING)
        out.append(_listed(f"{p} ({lines[p]} lines)" for p in tests) if tests
                   else f"\n{NO_TESTS_SENTENCE}\n")
    out += _schema_section(schema)
    text = "".join(out)
    size = (len(text.splitlines()), len(text.encode("utf-8")))
    over = [f"{size[0] - ceiling.lines} lines" if size[0] > ceiling.lines else "",
            f"{size[1] - ceiling.bytes} bytes" if size[1] > ceiling.bytes else ""]
    if any(over):
        raise PartitionError(
            f"the auditor payload for {area.id} is {' and '.join(o for o in over if o)} "
            f"over the ceiling ({size[0]} of {ceiling.lines} lines, {size[1]} of "
            f"{ceiling.bytes} bytes; {len(area.tested) if area.tested else len(area.files)} "
            f"files, {len(tests)} named as tests); "
            f"exclude paths or raise the ceiling"
        )
    return text


def render_probe_payload(brief: str, problem: str, schema: dict | None = None) -> str:
    """The whole of the probe's payload, from exactly these two inputs and the run's probe
    schema: the brief, the problem statement verbatim, and the schema.

    No area, no file list and no lens — the probe is not reading for defects, and a payload
    that assigned it files would invite it to. The problem statement is there for the same
    reason it is in every other payload: the owner's words are what the run is about, and
    they travel verbatim."""
    return "".join([brief.rstrip("\n"), "\n", PROBLEM_HEADING, "\n", problem, "\n",
                    *_schema_section(schema)])


def measure_payload(text: str) -> tuple[int, int]:
    """``(lines, bytes)`` of a rendered payload, counting a last line without a newline
    exactly as the inventory counts a file, so one number means one thing everywhere.

    Recorded on every unit, never enforced on one: ``DECISIONS.md`` declines a size bound
    on a verification batch and names a real batch past what a model can read as what
    would change that answer. These are the numbers that answer it.
    """
    raw = text.encode("utf-8")
    return raw.count(b"\n") + (1 if raw and not raw.endswith(b"\n") else 0), len(raw)


def write_units(rundir: Path, job: Job, inventory: Inventory, areas: Sequence[PlannedArea],
                companions: Companions | None = None) -> tuple[Unit, ...]:
    """Write ``units/<id>/payload.md`` and ``schema.json`` for every unit, then
    ``units.json`` listing them with lane, lens and area. A unit carries the schema its own
    kind answers against: the readers and the auditors the reader schema, the probe its own.
    Every file lands by the exclusive-create-then-replace rule; a planted path is a refusal
    by name. ``plan`` passes companions it loaded before its first write; a direct caller may
    omit them."""
    if companions is None:
        companions = load_companions()
    auditors = auditor_plan_of(inventory, job.coverage is False)
    units = assign_units(areas, auditors=auditors.dispatch)
    by_id = {area.id: area for area in areas}
    # Rendered before the first directory is made: the payloads that can refuse — an
    # auditor's, over the ceiling — then leave nothing behind. An area's two auditors get
    # the same bytes, which is what makes their answers comparable.
    # Not rendered at all where no auditor runs: a ceiling refusal over a payload nobody
    # is handed would stop a run for the size of a question that was never going to be put.
    auditor_text = {
        area.id: render_auditor_payload(companions.auditor_brief, job.problem, area,
                                        inventory, schema=companions.reader_schema)
        for area in (auditor_areas(areas) if auditors.dispatch else ())
    }
    probe_text = render_probe_payload(companions.probe_brief, job.problem,
                                      schema=companions.probe_schema)
    listing = []
    for unit in units:
        unit_dir = rundir / UNITS_DIR / unit.id
        try:
            unit_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise InventoryError(f"cannot create {unit_dir}: {exc}") from exc
        if unit.kind == READER_KIND:
            text = render_reader_payload(companions.reader_brief, job.problem,
                                         by_id[unit.area], unit.lens, schema=companions.reader_schema)
            schema = companions.reader_schema
        elif unit.kind == PROBE_KIND:
            text = probe_text
            schema = companions.probe_schema
        else:
            text = auditor_text[unit.area]
            schema = companions.reader_schema
        write_text(unit_dir / PAYLOAD_NAME, text)
        write_json(unit_dir / SCHEMA_NAME, schema)
        lines, size = measure_payload(text)
        listing.append({
            **asdict(unit),
            "payload": f"{UNITS_DIR}/{unit.id}/{PAYLOAD_NAME}",
            "schema": f"{UNITS_DIR}/{unit.id}/{SCHEMA_NAME}",
            "payload_lines": lines,
            "payload_bytes": size,
        })
    write_json(rundir / UNITS_FILE_NAME, {"stage": "reading", "lanes": list(LANES), "units": listing})
    return units


# --------------------------------------------------------------------------- #
# route — candidates from the reading results, verification units routed away
# --------------------------------------------------------------------------- #
# A reading result is hand-parsed against the reader schema's vocabulary the way the job
# is, and for the same reason: no runtime flag ever sees a file an agent wrote, so the
# engine is the only enforcer. An unknown key, a missing field, a wrong type, a severity
# outside the four levels or a location outside the snapshot makes the UNIT invalid — it
# is recorded failed with the rule named, and nothing in it is defaulted or salvaged.
RESULT_NAME = "result.json"
ERROR_NAME = "error.txt"
CANDIDATES_FILE_NAME = "candidates.json"
VERIFIER_SCHEMA_NAME = "verifier-schema.json"
COVERAGE_VERIFIER_SCHEMA_NAME = "coverage-verifier-schema.json"
VERIFIER_KIND = "verifier"
# The two questions a batch can put, and the two ladders a finding travels on. A reader
# asks what is WRONG with this code; the coverage auditor asks what input NO TEST
# constructs. Both answer in one finding shape, and for one generation of this engine both
# were routed, verified and reported as the same thing — so every coverage finding went to
# a verifier whose brief asks whether a claimed failure is real, and came back refuted for
# the only reason it could: a missing test is not a failure. Both agents were right by
# their briefs, and the run filed real, named, missing tests under the one heading that
# tells a reader nothing there is work.
#
# The question is not stored on a candidate. It is READ off the raiser's kind, which the
# route record already carries — one fact in one place, which is why the two can never
# disagree about which ladder a finding is on. See :func:`asks_of`.
DEFECT_ASKS = "defect"
COVERAGE_ASKS = "coverage"
ASKS = (DEFECT_ASKS, COVERAGE_ASKS)
# What a coverage batch's id ends with, so the kind is visible in the run directory and in
# every line that names a unit.
COVERAGE_BATCH_SUFFIX = "-coverage"
READING_STAGE = "reading"
VERIFICATION_STAGE = "verification"
ROUTED_STAGE = "routed"
UNIT_COMPLETE, UNIT_FAILED, UNIT_MISSING = "complete", "failed", "missing"
# The vocabularies, each equal to the shipped schema's — a test holds them equal, so the
# engine cannot accept what a runtime's structured output rejects or the reverse.
SEVERITIES = ("blocker", "major", "minor", "nit")
VERDICT_STATUSES = ("reproduced", "confirmed_by_reading", "refuted", "unresolved")
# The two statuses that put a finding in front of somebody to fix. They are the ones that
# need the test that should be red first; the other two have nothing to write one against.
ESTABLISHED_STATUSES = ("reproduced", "confirmed_by_reading")
# A coverage gap is judged on one question — does any test in scope construct this input? —
# and the defect statuses are answers to a different one. `reproduced` and
# `confirmed_by_reading` would both be claims about whether the code is wrong, which a gap
# does not assert: a branch can be perfectly correct today and still have no test. So the
# statuses are its own, and the engine refuses each set on the other's batch.
COVERAGE_GAP_CONFIRMED = "gap_confirmed"
COVERAGE_GAP_REFUTED = "gap_refuted"
COVERAGE_STATUSES = (COVERAGE_GAP_CONFIRMED, COVERAGE_GAP_REFUTED, "unresolved")
# The one that puts work in front of somebody: a gap that stood is a test to write, and it
# names that test the way an established defect does.
COVERAGE_ESTABLISHED = (COVERAGE_GAP_CONFIRMED,)
FIX_SIZES = ("1 line", "small", "medium", "large")
UNRESOLVED_REASONS = ("needs_a_run", "needs_a_file_outside_the_scope",
                      "needs_a_product_decision", "blocked_by_the_environment")
# What the engine found when it compared a reader's quotation against the pinned tree at
# the lines that reader cited. The engine NEVER repairs the range on the strength of this:
# mechanical extraction would then faithfully print the wrong lines under a heading nobody
# could check, which is worse than printing the right lines under a warning.
QUOTE_MATCHES, QUOTE_DIFFERS, QUOTE_UNREADABLE = "matches", "differs", "unreadable"
QUOTE_STATES = (QUOTE_MATCHES, QUOTE_DIFFERS, QUOTE_UNREADABLE)
# A reader invents its findings, so no set exists to prove its list complete against — the
# check that catches a short clustering or synthesis reply has nothing to compare here. The
# worker therefore declares its own count and is held to it.
#
# **This does not catch a truncated reply.** A reply cut off inside the array is not valid
# JSON and fails to decode long before this runs. Closing such an object by hand would make
# it parse while its array stayed short, which is why the protocol forbids repairing a
# result at all. What this catches is a worker that MISCOUNTS: one whose object is
# well-formed and whose array is shorter than the number it stated in the same object. That
# is the only completeness handle a reading round has, which is why it is kept.
#
# Named in the schema and REQUIRED there, so an enforced runtime always sends it; optional
# to the parser, which is why the required tuple below is the shorter one. The asymmetry
# runs the permissive way on purpose: results written before the field existed stay
# readable, and no reply a runtime produced is refused for lacking it.
READER_COUNT_KEY = "finding_count"
READER_RESULT_KEYS = ("finding_count", "findings", "summary")
READER_RESULT_REQUIRED = ("findings", "summary")
FINDING_KEYS = ("file", "line_start", "line_end", "severity", "consequence", "failure",
                "direction", "fix_size", "quote", "reproduction")
REPRODUCTION_KEYS = ("argv", "cwd", "expect")
VERIFIER_RESULT_KEYS = ("verdicts", "summary")
VERDICT_KEYS = ("candidate", "status", "evidence", "rationale", "revision", "test_first",
                "unresolved_reason")
# One more field on a coverage verdict, and the only thing that can refute a gap: the test
# that already constructs the input. A refutation naming no test is an opinion about the
# code, which is not what was asked.
COVERED_BY_KEY = "covered_by"
# The files an unresolved verdict says would settle it, as PATHS rather than as a sentence.
# `needs_a_file_outside_the_scope` is the commonest unresolved reason there is, and the
# brief has always asked the verifier to name the file in its rationale — where nothing can
# add it up. A third of one run's unresolved defects hung on the same three or four files,
# and the report could not say so, because counting them meant reading free prose and
# guessing which words were paths. An owner deciding whether to widen the next job's scope
# needs the price of each file, and this is where that number comes from.
NEEDS_FILES_KEY = "needs_files"
# The reason that field belongs to. Named once: the enum below and the rules that read it
# must mean the same value.
NEEDS_A_FILE = "needs_a_file_outside_the_scope"
VERDICT_KEYS = VERDICT_KEYS + (NEEDS_FILES_KEY,)
COVERAGE_VERDICT_KEYS = VERDICT_KEYS + (COVERED_BY_KEY,)
# Known to the parser, and NOT required by it — the asymmetry READER_COUNT_KEY states. An
# enforced runtime always sends it, and a verdict written before the field existed stays
# readable with no files named. Present and empty is a different thing and is refused
# below: a verdict claiming a file outside the scope would settle it, and declining to say
# which, is a claim with its own evidence withheld.
VERDICT_REQUIRED = tuple(k for k in VERDICT_KEYS if k != NEEDS_FILES_KEY)
COVERAGE_VERDICT_REQUIRED = tuple(k for k in COVERAGE_VERDICT_KEYS if k != NEEDS_FILES_KEY)
STATUSES_FOR = {DEFECT_ASKS: VERDICT_STATUSES, COVERAGE_ASKS: COVERAGE_STATUSES}
ESTABLISHED_FOR = {DEFECT_ASKS: ESTABLISHED_STATUSES, COVERAGE_ASKS: COVERAGE_ESTABLISHED}
VERDICT_KEYS_FOR = {DEFECT_ASKS: VERDICT_KEYS, COVERAGE_ASKS: COVERAGE_VERDICT_KEYS}
VERDICT_REQUIRED_FOR = {DEFECT_ASKS: VERDICT_REQUIRED,
                        COVERAGE_ASKS: COVERAGE_VERDICT_REQUIRED}
# What a run actually did, which the status alone does not say. `reproduced` means a
# command ran and showed the failure — and a command that greps the source for a string it
# does not hold shows the failure just as truly as one that calls the method and prints the
# wrong number. Both are legitimate; they are not the same evidence, and a report counting
# them together tells a reader ten reproductions ran when six exercised any code.
#
# `executed` ran the code under review. `documentary` inspected the tree without running it
# — a search for text, a listing, a checksum — and shows what is written rather than what
# happens.
#
# Named in the schema and REQUIRED there, optional to the parser, by the rule
# READER_COUNT_KEY states: an enforced runtime always sends it, a verdict written before
# the field existed stays readable, and the report counts what did not say apart rather
# than guessing which kind it was.
RUN_KINDS = ("executed", "documentary")
RUN_KIND_KEY = "run_kind"
# ``shows`` is what the run establishes, in the verifier's own words. Required, because a
# command and its output with nothing saying what they were meant to prove leaves the
# reader to work the connection out of prose further down the entry -- and where one broad
# grep is attached to five defects there is no single connection to find.
EVIDENCE_KEYS = ("argv", "cwd", "exit_status", "output", "truncated", RUN_KIND_KEY, "shows")
EVIDENCE_REQUIRED = ("argv", "cwd", "exit_status", "output", "truncated", "shows")
REVISION_KEYS = ("severity", "rationale")
PROBE_ANSWERS = ("yes", "no", "unknown")
PROBE_RESULT_KEYS = ("build", "tests", "summary")
PROBE_ATTEMPT_KEYS = ("answer", "argv", "cwd", "exit_status", "output", "truncated")
PROBE_ATTEMPTS = ("build", "tests")
CANDIDATES_HEADING = "\n## Candidates\n"
PROBE_HEADING = "\n## Execution capability\n"
PROBE_UNKNOWN_WORD = "unknown"
# The affirmative answer, named rather than spelled at each use: the report compares against
# it to notice a capability the work never got to use.
PROBE_YES_WORD = "yes"
# What the payload tells a verifier about the reader's quotation. A differing quote is the
# one that changes what the verifier should do first, so it says so and says what the engine
# did NOT do about it.
QUOTE_PAYLOAD_LINES = {
    QUOTE_MATCHES: "The source at those lines is what the reader quoted.",
    QUOTE_DIFFERS: ("The source at those lines is NOT what the reader quoted. The range may "
                    "be wrong; the engine did not change it. Find what the reader described "
                    "before judging the claim, and say where it is."),
    QUOTE_UNREADABLE: ("The engine could not read those lines from the pinned tree, so the "
                       "reader's quotation was not checked against them."),
}
NO_REPRODUCTION_LINE = "Proposed reproduction: none"


class ResultError(ReviewPanelError):
    """A unit's result does not satisfy its schema's vocabulary. Names the unit and the
    field; the unit is recorded failed, never repaired."""


class OutOfScope(ResultError):
    """A finding located in a file the snapshot carries and the job left out of scope.

    Not a broken rule: the snapshot carries the repository's lockfiles beside the review so
    a build installs what it pins, and a reader following a call can land in one. The owner excluded the file to be told
    nothing about it, so the finding is dropped and only counted — listed with the
    rejections it would print a finding the owner asked not to see, and it would mark a
    unit that answered correctly as one that did not."""


@dataclass(frozen=True)
class Finding:
    """One finding as a reader stated it, with its location normalized to the snapshot
    path and its reproduction, if any, as the schema's object."""

    file: str
    line_start: int
    line_end: int
    severity: str
    consequence: str
    failure: str
    direction: str
    fix_size: str
    quote: str
    reproduction: dict | None


@dataclass(frozen=True)
class UnitState:
    """What one reading unit came back as: exactly one of complete, failed or missing,
    with the reason where it is not complete and the findings where it is.

    ``rejected`` is the findings inside a COMPLETE unit that the engine could not read, one
    message each. A unit with rejections is complete — its other findings stand and became
    candidates — so this is what keeps the ones thrown away from being thrown away in
    silence, the way a rejected verdict is named on a batch that otherwise answered.
    """

    id: str
    state: str
    reason: str | None
    findings: tuple[Finding, ...]
    rejected: tuple[str, ...] = ()
    # Findings located in a file the job left out of scope: dropped, and only counted.
    dropped: int = 0


@dataclass(frozen=True)
class Raised:
    """One raising of a candidate: which unit, lane and lens, the area that unit read,
    and that reader's own proposal — severity, consequence, direction, fix size,
    reproduction and the source it quotes at the cited lines are the raiser's, kept per
    raiser rather than merged, since the engine infers no value."""

    unit: str
    lane: str
    lens: str | None
    kind: str
    area: str | None
    severity: str
    consequence: str
    direction: str
    fix_size: str
    quote: str
    reproduction: dict | None


@dataclass(frozen=True)
class Candidate:
    """One reader's finding, carried whole to verification. Nothing groups before a verdict:
    two readers describing one defect are two candidates, each checked from its own
    description by a lane that did not raise it. ``raised_by`` therefore holds exactly one
    raiser, and ``area`` is the area closure assigned the file to, so every candidate has
    exactly one and the batch key is total."""

    id: str
    area: str
    file: str
    line_start: int
    line_end: int
    failure: str
    raised_by: tuple[Raised, ...]
    # One of :data:`QUOTE_STATES`, filled in after the candidates are built because only
    # then is there a run directory to read the pinned tree from. It reaches the verifier's
    # payload as a declared input and the report as a flag.
    quote_check: str = QUOTE_UNREADABLE


@dataclass(frozen=True)
class Batch:
    """One verification unit: the candidates of one (area, finder, question) triple,
    addressed to ``lane``, with the routing rule that chose it written out.

    ``asks`` is the question this batch puts, one of :data:`ASKS`. It is part of the key
    because the two questions cannot share a brief: a verifier asked whether a claimed
    failure is real will refute a coverage gap every time, correctly, for a reason that has
    nothing to do with whether the gap exists.
    """

    id: str
    area: str
    finder: str
    lane: str
    candidates: tuple[str, ...]
    routing: str
    asks: str = DEFECT_ASKS


@dataclass(frozen=True)
class Verdict:
    candidate: str
    status: str
    evidence: dict | None
    rationale: str
    revision: dict | None
    test_first: str | None
    unresolved_reason: str | None
    # False on the verdict :func:`parse_verifier_result` writes in place of one it could
    # not read. Such a verdict's ``rationale`` is the engine's own diagnostic — it quotes
    # the offending field, the vocabulary that field is drawn from and the candidate id —
    # so it is the engine talking, not a verifier. The coverage section wants it; a payload
    # that promises to carry no status, no severity and no candidate id must not, and
    # :func:`verified_rationales` is where the two part company.
    from_verifier: bool = True
    # The test that already constructs the input, on a coverage verdict that refuted a gap.
    # ``None`` on every other verdict and on every defect verdict, where there is no such
    # question to answer.
    covered_by: str | None = None
    # The files this verdict says would settle it, where its reason is that the answer is
    # outside the reviewed scope. Empty on every other verdict, and on one written before
    # the field existed.
    needs_files: tuple[str, ...] = ()


def _utf8(text: str, field: str, error: type[ReviewPanelError] = ResultError) -> str:
    """A JSON escape can spell a lone surrogate (``\\ud800``) that the decoder accepts and
    a UTF-8 write refuses. Every string a result carries is checked here, at parse, so
    the unit fails by field name; unchecked, the payload write raised after the
    verification directories existed and left a run directory that refused a retry.
    ``dispatch.json`` is checked the same way, as a :class:`DispatchError`: unchecked,
    the report write raised and left ``report.md.<pid>.tmp`` beside the run."""
    try:
        text.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise error(
            f"field '{field}' is not UTF-8 encodable ({exc.reason} at index {exc.start})"
        ) from None
    return text


def _text(raw: object, field: str, *, empty_ok: bool = False) -> str:
    if not isinstance(raw, str) or (not empty_ok and not raw.strip()):
        kind = "text" if empty_ok else "non-empty text"
        raise ResultError(f"field '{field}' must be {kind} (found {type(raw).__name__})")
    return _utf8(raw, field)


def _integer(raw: object, field: str, minimum: int | None = None) -> int:
    if not isinstance(raw, int) or isinstance(raw, bool):
        raise ResultError(f"field '{field}' must be an integer (found {type(raw).__name__})")
    if minimum is not None and raw < minimum:
        raise ResultError(f"field '{field}' must be at least {minimum} (found {raw})")
    return raw


def _one_of(raw: object, field: str, allowed: Sequence[str]) -> str:
    if not isinstance(raw, str) or raw not in allowed:
        raise ResultError(
            f"field '{field}' must be one of: {', '.join(allowed)} (found {raw!r})"
        )
    return raw


def _argv(raw: object, field: str) -> list[str]:
    if not isinstance(raw, list):
        raise ResultError(f"field '{field}' must be a list of strings (found {type(raw).__name__})")
    for i, item in enumerate(raw):
        if not isinstance(item, str):
            raise ResultError(f"field '{field}[{i}]' must be a string (found {type(item).__name__})")
        _utf8(item, f"{field}[{i}]")
    if not raw or not raw[0].strip():
        # An empty program name runs nothing, so evidence naming it is no evidence.
        raise ResultError(f"field '{field}' must not be empty; the first element is the program")
    return list(raw)


def _snapshot_parts(raw: object, field: str) -> list[str]:
    """The components of a snapshot-relative path from a result, normalized as the job's
    paths are: forward slashes, no ``.``, nothing absolute, no ``..``. Empty means the
    snapshot root, which a location may not name and a working directory may."""
    text = _text(raw, field, empty_ok=True).replace("\\", "/")
    parts = [part for part in text.split("/") if part not in ("", ".")]
    if text.startswith("/") or (parts and _is_drive_component(parts[0])):
        raise ResultError(f"field '{field}' {raw!r} is absolute; a path is relative to the snapshot")
    if ".." in parts:
        raise ResultError(f"field '{field}' {raw!r} climbs out of the snapshot with '..'")
    return parts


def _location(raw: object, field: str, files: frozenset[str] | set[str],
              context: frozenset[str] | set[str] = frozenset()) -> str:
    # A name exactly as the inventory holds it is that file, before any normalizing: a POSIX
    # file named `a\b.py` or `C:notes.py` is otherwise unreachable, or read as another.
    if isinstance(raw, str) and raw in files:
        return raw
    if isinstance(raw, str) and raw in context:
        raise OutOfScope(f"field '{field}' {raw!r} is outside the review's scope")
    parts = _snapshot_parts(raw, field)
    if not parts:
        raise ResultError(f"field '{field}' {raw!r} names no file in the snapshot")
    path = "/".join(parts)
    if path in context and path not in files:
        raise OutOfScope(f"field '{field}' {path!r} is outside the review's scope")
    if path not in files:
        raise ResultError(
            f"field '{field}' {path!r} is not in the snapshot; a finding is located in a "
            f"file the readers were given"
        )
    return path


def _working_dir(raw: object, field: str, files: frozenset[str] | set[str]) -> str:
    parts = _snapshot_parts(raw, field)
    if not parts:
        return "."
    path = "/".join(parts)
    if not any(f.startswith(path + "/") for f in files):
        raise ResultError(
            f"field '{field}' {path!r} is not a directory in the snapshot; a reproduction "
            f"runs inside the snapshot"
        )
    return path


def _nullable_text(raw: object, field: str) -> str | None:
    """A worker string that may be absent, spelled ``null`` rather than as the empty string:
    an empty field reads as an answer given, and these fields' rules turn on whether one was."""
    return None if raw is None else _text(raw, field)


def _nullable_one_of(raw: object, field: str, allowed: Sequence[str]) -> str | None:
    return None if raw is None else _one_of(raw, field, allowed)


def _parse_reproduction(raw: object, field: str, files, cwd_files=None) -> dict | None:
    """``cwd_files`` is the whole snapshot, where ``files`` may be an auditor's narrowed set.
    Narrowing says where a finding may be FILED; a reproduction may run from any directory
    the snapshot has. Checked against the narrowed set, ``src/test`` was refused for an
    auditor asked about ``src/main``, and the missing-test finding it was raising never
    became a candidate."""
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ResultError(f"field '{field}' must be an object or null (found {type(raw).__name__})")
    _check_keys(raw, field, frozenset(REPRODUCTION_KEYS), REPRODUCTION_KEYS, ResultError)
    return {
        "argv": _argv(raw["argv"], f"{field}.argv"),
        "cwd": _working_dir(raw["cwd"], f"{field}.cwd",
                            cwd_files if cwd_files is not None else files),
        "expect": _text(raw["expect"], f"{field}.expect", empty_ok=True),
    }


def _site_of(raw: object) -> str:
    """The location a finding CLAIMS, for naming it in a rejection.

    A verdict names a candidate and a rejection can quote that id. A finding has no id —
    it is the thing that mints one — so the only handle a reader has for "which of the
    fourteen in this reply" is where it says it is. Read defensively and off the RAW item,
    because the item is the thing that failed to parse: any field may be missing or the
    wrong type, and a rejection that raised while describing a rejection would take the
    unit down for the reason this whole arrangement exists to stop.
    """
    if not isinstance(raw, dict):
        return ""
    where = raw.get("file")
    if not isinstance(where, str) or not where.strip():
        return ""
    start, end = raw.get("line_start"), raw.get("line_end")
    if isinstance(start, int) and not isinstance(start, bool):
        if isinstance(end, int) and not isinstance(end, bool) and end != start:
            return f"{where}:{start}-{end}"
        return f"{where}:{start}"
    return where


def _parse_finding(raw: object, field: str, files, cwd_files=None,
                   context=frozenset()) -> Finding:
    if not isinstance(raw, dict):
        raise ResultError(f"'{field}' must be a JSON object (found {type(raw).__name__})")
    if context and "file" in raw:
        # Asked before any other rule, so a finding in an excluded file is dropped whatever
        # else is wrong with it, and no rejection ever prints where it was.
        try:
            _location(raw["file"], f"{field}.file", files, context)
        except OutOfScope:
            raise
        except ResultError:
            pass
    _check_keys(raw, field, frozenset(FINDING_KEYS), FINDING_KEYS, ResultError)
    start = _integer(raw["line_start"], f"{field}.line_start", 1)
    end = _integer(raw["line_end"], f"{field}.line_end", 1)
    if end < start:
        raise ResultError(f"field '{field}.line_end' ({end}) is before line_start ({start})")
    return Finding(
        file=_location(raw["file"], f"{field}.file", files, context),
        line_start=start,
        line_end=end,
        severity=_one_of(raw["severity"], f"{field}.severity", SEVERITIES),
        consequence=_text(raw["consequence"], f"{field}.consequence"),
        failure=_text(raw["failure"], f"{field}.failure"),
        direction=_text(raw["direction"], f"{field}.direction", empty_ok=True),
        fix_size=_one_of(raw["fix_size"], f"{field}.fix_size", FIX_SIZES),
        # Non-empty for the same reason the consequence is: the quote is collected so the
        # cited range can be checked against the snapshot, and an empty one answers nothing
        # about whether the range is right.
        quote=_text(raw["quote"], f"{field}.quote"),
        reproduction=_parse_reproduction(raw["reproduction"], f"{field}.reproduction", files, cwd_files),
    )


def _check_declared_count(obj: dict, found: int) -> None:
    """Hold a reading result to the count its own writer declared.

    ``bool`` is an ``int`` in Python, so it is excluded by name: ``True`` would otherwise
    pass as a declared count of one.
    """
    # ABSENT is the only tolerated case. An explicit null is a present field with a wrong
    # value, and `obj.get` alone would read the two the same way and wave it through.
    if READER_COUNT_KEY not in obj:
        return
    declared = obj[READER_COUNT_KEY]
    if isinstance(declared, bool) or not isinstance(declared, int) or declared < 0:
        raise ResultError(
            f"field '{READER_COUNT_KEY}' must be a non-negative whole number "
            f"(found {declared!r})"
        )
    if declared != found:
        raise ResultError(
            f"the reply declares {declared} findings and carries {found}; a reply that "
            f"lost part of its array is this unit's failure, not a shorter list"
        )


def parse_reader_result(obj: object, unit_id: str, files, cwd_files=None,
                        context=frozenset()) -> tuple[tuple[Finding, ...], tuple[str, ...], int]:
    """Strictly parse one reading result, returning its findings, the rejections, and how
    many findings were dropped for lying outside the review's scope.

    ``files`` is where a finding may be located. ``context`` is the rest of the snapshot:
    a finding there is not a broken rule but one the owner asked not to see, so it is
    counted and dropped — see :class:`OutOfScope`.

    **A finding that breaks a rule costs itself and no other.** The rules are unchanged and
    none is relaxed; what changes is the blast radius, and it is the treatment a
    verification batch has already — see :func:`parse_verifier_result`. A reading unit is
    one dispatch of many independent observations, and one finding the engine cannot read
    says nothing about the thirteen beside it. Failing the unit over it threw all of them
    away and reported the area as unread, which is a far larger claim than the one the
    error supports: on one run a single optional sub-field on a single finding cost 27
    findings, about nine percent of that run's candidates.

    A rejected finding raises no candidate — unlike a verdict, there is nothing for it to
    resolve to, because it is the thing that would have minted the id. It is named instead,
    by where it says it is, and the coverage section prints that so nothing is thrown away
    in silence.

    What still fails the unit is a result whose SHAPE cannot be trusted: not an object, an
    unknown or missing top-level key, ``findings`` not a list, or an array shorter than the
    count the reply declared for itself. In each of those the engine cannot say what it is
    holding, and reading findings out of it would be guesswork.
    """
    try:
        if not isinstance(obj, dict):
            raise ResultError(f"result must be a JSON object (found {type(obj).__name__})")
        _check_keys(obj, "result", frozenset(READER_RESULT_KEYS), READER_RESULT_REQUIRED, ResultError)
        _text(obj["summary"], "summary", empty_ok=True)
        if not isinstance(obj["findings"], list):
            raise ResultError(f"field 'findings' must be a list (found {type(obj['findings']).__name__})")
        # Against what ARRIVED, not against what parsed. The declared count is the reply's
        # own claim about how many items it wrote, so it catches a reply that lost part of
        # its array -- a different failure from a finding the engine could not read, and one
        # that rejecting findings must not start reporting. Comparing it to the accepted
        # count would fail the unit for exactly the reason this function no longer does.
        _check_declared_count(obj, len(obj["findings"]))
        findings: list[Finding] = []
        rejected: list[str] = []
        dropped = 0
        for i, item in enumerate(obj["findings"]):
            try:
                findings.append(_parse_finding(item, f"findings[{i}]", files,
                                               cwd_files=cwd_files, context=context))
            except OutOfScope:
                dropped += 1
            except ResultError as exc:
                site = _site_of(item)
                rejected.append(f"findings[{i}]{f' ({site})' if site else ''}: {exc}")
        return tuple(findings), tuple(rejected), dropped
    except ResultError as exc:
        raise ResultError(f"{unit_id}: {exc}") from None


def _is_regular_file(path: Path) -> bool:
    """Whether ``path`` is a regular file, raising whatever stops the answer. ``Path.is_file``
    turns a permission error into False on Python 3.14, which would record a unit the engine
    cannot read as one that wrote nothing."""
    try:
        return stat.S_ISREG(os.stat(path).st_mode)
    except (FileNotFoundError, NotADirectoryError):
        return False


def _os_reason(exc: OSError) -> str:
    """An OS error's reason without the path it carries: two runs of one tree write one
    report."""
    return exc.strerror or type(exc).__name__


def _os_fault(exc: OSError) -> str:
    """An OS error's reason **with its number**, still without the path.

    The number is what a caller classifies by. A stage's refusal reaches the driver as text
    — the exception is gone by then — and the driver tells a fault of the volume from every
    other refusal by reading that text: a run it can pause and resume once the disk is
    fixed, against one it must refuse. ``EIO`` written as its translation alone is
    "Input/output error", which names nothing the driver matches, so a transient device
    fault examining a stage's output ends a completed run instead of pausing it. The
    translation is also the C library's and a locale may change it; the number is what the
    host actually said.
    """
    reason = _os_reason(exc)
    return f"[Errno {exc.errno}] {reason}" if exc.errno is not None else reason


# No room, a read-only volume, a quota, a failing device. A fault of the host, which is
# never a unit's answer and never this stage's verdict on anything.
_STORAGE_ERRNOS = frozenset(
    code for code in (getattr(errno, name, None)
                      for name in ("ENOSPC", "EROFS", "EIO", "EDQUOT"))
    if code is not None)


def _stop_on_a_storage_fault(what: str, exc: OSError) -> None:
    """Refuse the whole stage where ``exc`` is the volume failing, rather than letting it be
    adjudicated as something about the unit it was met on.

    **A unit the engine cannot read is that unit's failure — but only where the reason is
    about that unit.** A permission or a malformed file is a stable fact of the file, and
    recording it keeps the stage going and the report honest. A device error is not: it says
    nothing about this unit, it will meet the next one just the same, and recorded as a
    failed unit it publishes "nothing was found here" for a reader whose answer was sitting
    on disk the whole time. The errno is carried into the message so a caller that reads
    this stage's output — the driver, which pauses the run and resumes it once the volume is
    fixed — can tell this refusal from every other one.
    """
    if exc.errno in _STORAGE_ERRNOS:
        raise StorageStop(
            f"cannot read {what}: {_os_fault(exc)}; the volume holding "
            f"the run directory is failing, so this stage has decided nothing — put it right "
            f"and run the same command again"
        ) from exc


def _read_unit_file(rundir: Path, unit_id: str) -> tuple[str, object]:
    """What one unit landed, before its schema is applied: ``(UNIT_COMPLETE, object)``
    for a ``result.json`` that decoded, else ``(UNIT_FAILED, reason)`` or
    ``(UNIT_MISSING, reason)``. The one rule shared by every unit kind.

    ``result.json`` and ``error.txt`` together is a refusal naming the unit: the protocol
    says one or the other, and a dispatcher that wrote both is not following it, so the
    engine guesses neither way. A result that is unreadable or not JSON is a FAILED unit
    with that reason — recorded, and never dropped from the report.
    """
    unit_dir = rundir / UNITS_DIR / unit_id
    result, error = unit_dir / RESULT_NAME, unit_dir / ERROR_NAME
    try:
        has_result, has_error = _is_regular_file(result), _is_regular_file(error)
    except OSError as exc:
        # One unit directory the engine cannot search is that unit's failure, as an
        # unreadable result is, not the end of the stage — unless the volume is what
        # refused, which is nobody's failure and stops the stage instead.
        _stop_on_a_storage_fault(f"the directory of unit {unit_id}", exc)
        return UNIT_FAILED, f"cannot read the unit directory: {_os_reason(exc)}"
    if has_result and has_error:
        raise RunDirError(
            f"unit {unit_id} holds both {RESULT_NAME} and {ERROR_NAME}; a unit is "
            f"exactly one of complete or failed, so remove the one that is wrong"
        )
    if has_error:
        try:
            text = error.read_text(encoding="utf-8", errors="replace").strip()
        except OSError as exc:
            _stop_on_a_storage_fault(f"{ERROR_NAME} of unit {unit_id}", exc)
            text = f"unreadable: {_os_reason(exc)}"
        first = text.splitlines()[0] if text else ""
        return UNIT_FAILED, f"{ERROR_NAME}: {first[:200]}"
    if not has_result:
        return UNIT_MISSING, f"neither {RESULT_NAME} nor {ERROR_NAME} was written"
    # The decoder raises more than JSONDecodeError: a result nested thousands deep
    # raises RecursionError. Every one of them is that unit's failure, recorded, and
    # never a crash that aborts the stage with no unit state written.
    try:
        return UNIT_COMPLETE, json.loads(result.read_text(encoding="utf-8"))
    except OSError as exc:
        # **The one that matters most.** This unit's answer is on disk, whole, and the
        # device would not give it up; charged to the unit, an accepted finding vanishes
        # from every later round and from the report, and the stage still exits 0.
        _stop_on_a_storage_fault(f"{RESULT_NAME} of unit {unit_id}", exc)
        return UNIT_FAILED, f"cannot read {RESULT_NAME}: {_os_reason(exc)}"
    except UnicodeDecodeError as exc:
        return UNIT_FAILED, f"cannot read {RESULT_NAME}: {exc}"
    except ValueError as exc:
        return UNIT_FAILED, f"{RESULT_NAME} is not valid JSON: {exc}"
    except RecursionError:
        return UNIT_FAILED, f"{RESULT_NAME} nests deeper than the JSON decoder can parse"


def read_unit_results(rundir: Path, units: Sequence[dict], files,
                      context=frozenset()) -> tuple[UnitState, ...]:
    """Classify every reading unit as exactly one of complete, failed or missing: the
    landing rule of :func:`_read_unit_file`, then the reader schema's vocabulary, a
    refusal from which is a FAILED unit with the rule named.

    Where a finding may be LOCATED is per unit rather than per run — see
    :func:`_locations_for`, which narrows an auditor to the files it was asked about."""
    audited = _read_audited(rundir)
    states: list[UnitState] = []
    for unit in units:
        state, payload = _read_unit_file(rundir, unit["id"])
        if state != UNIT_COMPLETE:
            states.append(UnitState(unit["id"], state, payload, ()))
            continue
        try:
            findings, rejected, dropped = parse_reader_result(
                payload, unit["id"], _locations_for(unit, files, audited),
                cwd_files=files | context, context=context)
        except ResultError as exc:
            states.append(UnitState(unit["id"], UNIT_FAILED, str(exc), ()))
            continue
        states.append(UnitState(unit["id"], UNIT_COMPLETE, None, findings, rejected,
                                dropped))
    return tuple(states)


def build_candidates(states: Sequence[UnitState], units: Sequence[dict],
                     owner: dict[str, str]) -> tuple[Candidate, ...]:
    """One candidate per finding, in a deterministic order, each keeping its one raiser.

    Nothing groups here. Two readers describing one defect stay two candidates, so each is
    verified from its own description by the lane that did not raise it — two independent
    cross-model checks — and the fact that both found it survives in the artifacts. Grouping
    first collapsed that into one candidate with one verdict and made the co-discovery
    unrecoverable. ``owner`` maps every snapshot file to the area closure assigned it to.
    """
    by_id = {unit["id"]: unit for unit in units}
    rows: list[tuple[tuple, dict, Finding]] = []
    for state in states:
        unit = by_id[state.id]
        for ordinal, finding in enumerate(state.findings):
            # The key ends with the unit id and the finding's position in it, so two
            # byte-identical findings from different readers still have a total order and
            # the ids never depend on which result landed first.
            rows.append(((finding.file, finding.line_start, finding.line_end, finding.failure,
                          unit["id"], ordinal), unit, finding))
    rows.sort(key=lambda row: row[0])
    width = max(3, len(str(len(rows))))
    candidates: list[Candidate] = []
    for n, (_key, unit, finding) in enumerate(rows, 1):
        raised = Raised(
            unit=unit["id"], lane=unit["lane"], lens=unit["lens"], kind=unit["kind"],
            area=unit["area"], severity=finding.severity, consequence=finding.consequence,
            direction=finding.direction, fix_size=finding.fix_size, quote=finding.quote,
            reproduction=finding.reproduction,
        )
        candidates.append(Candidate(
            id=f"cand-{n:0{width}d}", area=owner[finding.file], file=finding.file,
            line_start=finding.line_start, line_end=finding.line_end, failure=finding.failure,
            raised_by=(raised,),
        ))
    return tuple(candidates)


def _physical_lines(text: str) -> list[str]:
    """A file's lines as an editor counts them: split on newlines and nothing else.

    ``str.splitlines`` also breaks on form feeds, vertical tabs and the Unicode separators,
    so a source file carrying a form feed — which real code does, as a page break — would be
    numbered one line higher from there on. Every line number this engine prints and every
    range it checks would then disagree with the editor the reader opens the file in, which
    is the one thing a line number has to do.

    Callers pass text read with :func:`_read_source`, which does NOT translate newlines,
    so this splits exactly where the inventory counted a line break: on the LF bytes and
    nowhere else. Universal-newline reading would turn a lone carriage return into one of
    those, and a file holding one would then be two lines to the inventory and three here —
    a citation of the second line quoting the third, and a snippet numbering it wrong.

    A trailing carriage return is dropped because CRLF is one line ending: the inventory
    counted its LF once, and leaving the CR on would end every line of a Windows file with
    a character its author did not write.
    """
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    return [line[:-1] if line.endswith("\r") else line for line in lines]


def _read_source(path: Path) -> str:
    """A snapshot file as its bytes are, with no newline translation. Paired with
    :func:`_physical_lines`: together they are the one line definition this engine uses, and
    it is the inventory's."""
    with open(path, encoding="utf-8", errors="replace", newline="") as fh:
        return fh.read()


def _comparable(text: str) -> str:
    """Source reduced to what a quotation and an extraction can be compared on: blank lines
    dropped and every line stripped. A reader that re-indented what it quoted, or trimmed a
    trailing newline, has still quoted the same lines — flagging that would make the check
    noise, and a check that cries wolf is one nobody reads. What survives the reduction is
    the thing the check is for: a range pointing at different code entirely."""
    return "\n".join(line.strip() for line in text.strip().splitlines() if line.strip())


@dataclass(frozen=True)
class QuoteCheck:
    """One quotation compared with the pinned tree: the state, the source the cited range
    actually holds, and — where it differs — the first quoted line the range could not
    account for beside the line the range holds where the match stalled.

    Those last two are what a dispatcher acts on. The first line of a quotation is a poor
    thing to print: an abbreviated quote pins the first line of its range by construction,
    so showing it displays two identical lines and says nothing about what went wrong.

    The two POSITIONS are carried beside the two texts because the texts alone can be
    equal. The comparison is in-order, so a quotation asking for a line more times than the
    range holds it stalls on a line whose text matches the one it stopped at — a closing
    brace against a closing brace. Printed without positions that is two identical lines
    again, which is the thing the paragraph above exists to prevent.

    ``quoted_at`` is 1-based within the quotation and ``quoted_of`` its length, both after
    the reduction :func:`_comparable` applies. ``found_at`` is a LINE NUMBER IN THE FILE,
    not an index into the range: blank lines are dropped before the comparison, so the two
    do not correspond and only one of them is a thing a person can go and look at.
    """

    state: str
    cited: str | None
    quoted_line: str | None = None
    found_line: str | None = None
    quoted_at: int | None = None
    quoted_of: int | None = None
    found_at: int | None = None


def _abbreviates(quoted: Sequence[str], cited: Sequence[str]) -> tuple[bool, int, int]:
    """Whether ``quoted`` is an in-order selection from ``cited`` that pins both its ends,
    with the positions where a failed match stalled.

    The reader schema lets a long range be quoted by its first and last lines rather than in
    full, which keeps a payload short on a forty-line citation. The comparison has to know
    that permission or it reports every abbreviated quote as a wrong range — a warning
    telling a reader to go and check a location that was right, printed often enough to
    bury the ones that are not.

    Two conditions, and the second is what keeps the check worth running: every quoted line
    appears in the range in the order given, AND the range's own first and last lines are
    among them. The endpoints are what a range IS, so a quotation that pins both has
    established the thing this check exists to establish. A wrong range fails on them.
    """
    i = 0
    for n, line in enumerate(quoted):
        while i < len(cited) and cited[i] != line:
            i += 1
        if i == len(cited):
            return False, n, min(len(cited), max(0, i - 1))
        i += 1
    # Reported against the END that is wrong, never against the quotation's position in
    # it. A one-line quote of a long range pins the first line and not the last, and
    # blaming the first would print the same line twice and name nothing.
    if not quoted or quoted[0] != cited[0]:
        return False, 0, 0
    if quoted[-1] != cited[-1]:
        return False, len(quoted) - 1, len(cited) - 1
    return True, 0, 0


def compare_quote(snapshot: Path, file: str, line_start: int, line_end: int,
                  quote: str) -> QuoteCheck:
    """One quotation against the pinned tree at the lines it cites.

    ``unreadable`` is its own answer and is not folded into ``differs``: a file the engine
    could not open, or a range past the end of one, says nothing about whether the reader
    was right, and the source is ``None`` there because there is none to show.

    The comparison lives in its own function because two stages ask for it and they must
    give one answer. ``route`` records it on every candidate; ``check`` prints it while the
    dispatcher can still send the unit back, which is the only moment a wrong range can be
    fixed rather than flagged. Two implementations of "differs" would eventually disagree
    about which ranges are wrong, and the later one is the one nobody could act on.

    What counts as a match is :func:`_abbreviates`, and it is the rule the reader schema
    states rather than a looser reading of it: a quotation may leave out the middle of a
    long range and may not leave out either end.
    """
    try:
        lines = _physical_lines(_read_source(snapshot / file))
    except OSError as exc:
        # Tolerated because ``unreadable`` is not a benign value here: it is rendered as
        # itself, so the report says the citation could not be checked instead of implying
        # it was checked and matched. The volume failing is the one reason that says nothing
        # about this file, would meet every later citation the same way, and would turn a
        # run's whole quotation check into a page of "unreadable".
        _stop_on_a_storage_fault(f"{file} in the snapshot", exc)
        return QuoteCheck(QUOTE_UNREADABLE, None)
    if line_start < 1 or line_end > len(lines):
        return QuoteCheck(QUOTE_UNREADABLE, None)
    cited = "\n".join(lines[line_start - 1:line_end])
    # Both sides reduced the same way before anything is compared: re-indenting what you
    # quoted, or dropping a blank line, is still quoting the same lines.
    #
    # Numbered while the reduction happens, because afterwards it cannot be: dropping the
    # blank lines is exactly what makes an index into `kept` stop being a line number. The
    # pairing is what lets a mismatch name a line somebody can open the file and read.
    numbered = [(n, line.strip())
                for n, line in enumerate(lines[line_start - 1:line_end], line_start)
                if line.strip()]
    kept = [line for _n, line in numbered]
    quoted = _comparable(quote).splitlines()
    if not kept:
        # A range of nothing but blank lines. It can only be quoted with nothing, and
        # there are no endpoints to pin.
        state = QUOTE_MATCHES if not quoted else QUOTE_DIFFERS
        return QuoteCheck(state, cited, quoted[0] if quoted else None, None,
                          1 if quoted else None, len(quoted) or None, None)
    ok, at_quote, at_cited = _abbreviates(quoted, kept)
    if ok:
        return QuoteCheck(QUOTE_MATCHES, cited)
    return QuoteCheck(QUOTE_DIFFERS, cited,
                      quoted[at_quote] if at_quote < len(quoted) else None,
                      kept[at_cited] if at_cited < len(kept) else None,
                      at_quote + 1 if at_quote < len(quoted) else None,
                      len(quoted),
                      numbered[at_cited][0] if at_cited < len(numbered) else None)


def check_quotes(snapshot: Path, candidates: Sequence[Candidate]) -> tuple[Candidate, ...]:
    """Every candidate's quoted source against the pinned tree at the lines it cites.

    Section 6 asks for this because a wrong line range is otherwise undetectable: the report
    extracts snippets mechanically, so a citation off by twenty lines prints twenty lines of
    innocent code under the heading of a real defect. The quotation is what makes the range
    checkable, and the engine records what it found rather than moving the range to fit —
    the reader saw something, and only the reader knows where.
    """
    out = []
    for cand in candidates:
        checked = compare_quote(snapshot, cand.file, cand.line_start, cand.line_end,
                                cand.raised_by[0].quote)
        out.append(replace(cand, quote_check=checked.state))
    return tuple(out)


def asks_of_kind(kind: str) -> str:
    """Which question a candidate raised by a unit of this kind puts to its verifier."""
    return COVERAGE_ASKS if kind == AUDITOR_KIND else DEFECT_ASKS


def asks_of(cand: dict) -> str:
    """A candidate record's question, read off its raiser's kind.

    Not stored a second time on the candidate. The kind is already written down — the route
    record carries which unit raised each candidate and what kind that unit was — and a
    fact kept in two fields is a fact that can disagree with itself. Every candidate has
    exactly one raiser, so this is total.
    """
    return asks_of_kind(cand["raised_by"][0]["kind"])


def route(candidates: Sequence[Candidate]) -> tuple[Batch, ...]:
    """One batch per (area, finder, question), the finder being the lane that raised its
    candidates, addressed to the other lane. Every candidate has exactly one raiser, so
    every batch is addressed to a lane that raised none of it, and each candidate is in
    exactly one batch and gets exactly one verdict.

    The question is part of the key because it decides which brief the batch is read with.
    A coverage gap in a defect batch is judged against "is this failure real?", and the
    honest answer to that about a missing test is no — which is how a run's real, named,
    missing tests came to sit in the appendix under a heading saying nothing there is work.
    Reader batches are unchanged, down to their ids; a coverage batch is the same key with
    the kind on the end of its name, so which question a unit was asked is visible in the
    run directory without opening anything.
    """
    keyed: dict[tuple[str, str, str], list[str]] = {}
    for cand in candidates:
        raiser = cand.raised_by[0]
        keyed.setdefault((cand.area, raiser.lane, asks_of_kind(raiser.kind)), []).append(cand.id)
    order = {lane: i for i, lane in enumerate(LANES)}
    asked = {question: i for i, question in enumerate(ASKS)}
    batches: list[Batch] = []
    for area, finder, question in sorted(keyed, key=lambda k: (k[0], order[k[1]], asked[k[2]])):
        lane = next(s for s in LANES if s != finder)
        coverage = question == COVERAGE_ASKS
        routing = (
            f"{'coverage gaps ' if coverage else ''}raised by lane {finder} in {area}; "
            f"addressed to the other lane, {lane}, which raised none of these"
            + ("; asked whether any test in scope constructs the input, not whether the "
               "code is wrong" if coverage else "")
        )
        batches.append(Batch(
            id=f"verify-{area}-{finder}{COVERAGE_BATCH_SUFFIX if coverage else ''}",
            area=area, finder=finder, lane=lane, asks=question,
            candidates=tuple(keyed[(area, finder, question)]), routing=routing))
    return tuple(batches)


def _distinct(values) -> list:
    out: list = []
    for value in values:
        if value not in out:
            out.append(value)
    return out


def _payload_field(label: str, text: str) -> list[str]:
    """One worker string in a verification payload: a single line inline after its label,
    more than one fenced, so a line such as ``### cand-002`` cannot read as another
    candidate."""
    lines = text.splitlines()
    if len(lines) <= 1:
        return [f"{label}{lines[0] if lines else ''}\n"]
    return [f"{label.rstrip()}\n", *_fenced(lines)]


def _candidate_sections(candidates: Sequence[dict]) -> list[str]:
    """Every candidate's own block: its id, where it points, what the engine found when it
    compared the quotation, the failure as raised, and every distinct proposal its raisers
    made. Who raised it — unit, lane, lens, the summary — is never here: a verifier judges
    the claim, not its author.

    Shared by both verification payloads because a candidate reads the same way whichever
    question is being put about it. What differs between the two payloads is the brief above
    these blocks and, for coverage, the tests listed beside them.
    """
    out: list[str] = []
    for cand in candidates:
        raised = cand["raised_by"]
        where = (f"line {cand['line_start']}" if cand["line_start"] == cand["line_end"]
                 else f"lines {cand['line_start']}-{cand['line_end']}")
        out += [f"\n### {cand['id']}\n", f"\nLocation: {cand['file']}, {where}\n",
                f"{QUOTE_PAYLOAD_LINES[cand['quote_check']]}\n",
                *_payload_field("Failure: ", cand["failure"]),
                f"Proposed severity: {'; '.join(_distinct(r['severity'] for r in raised))}\n"]
        for direction in _distinct(r["direction"] for r in raised):
            out += _payload_field("Direction: ", direction)
        repros = _distinct(r["reproduction"] for r in raised if r["reproduction"] is not None)
        if not repros:
            out.append(f"{NO_REPRODUCTION_LINE}\n")
        for repro in repros:
            out += ["Proposed reproduction:\n",
                    f"- argv: {json.dumps(repro['argv'])}\n",
                    *_payload_field("  cwd: ", repro["cwd"]),
                    *_payload_field("  expect: ", repro["expect"])]
    return out


def render_verifier_payload(brief: str, problem: str, candidates: Sequence[dict], probe: Probe,
                            schema: dict | None = None) -> str:
    """The whole of a verifier's payload from exactly these inputs: the brief, the problem
    statement verbatim, what the capability probe established about the tree, and each
    candidate's id, location, failure, and every distinct proposal its raisers made. Who
    raised it — unit, lane, lens, the reader's summary — is not rendered: the verifier judges
    the claim, not its author.

    ``probe`` is a declared input and has no default. The payload's byte reconstruction from
    its inputs is what makes blindness a property of the file rather than a request, and a
    fifth input that could be left out would be a fifth input the test does not cover."""
    out = [brief.rstrip("\n"), "\n", PROBLEM_HEADING, "\n", problem, "\n",
           *_probe_section(probe), CANDIDATES_HEADING, *_candidate_sections(candidates)]
    out += _schema_section(schema)
    return "".join(out)


def render_coverage_verifier_payload(brief: str, problem: str, candidates: Sequence[dict],
                                     probe: Probe, tests: Sequence[str],
                                     schema: dict | None = None) -> str:
    """The whole of a coverage verifier's payload: its own brief, the problem statement
    verbatim, what the probe established, **the tests in scope for this area**, and the
    candidates.

    ``tests`` is the input a defect payload has no use for and this one cannot work without.
    The question is whether any test in scope constructs the named input, and a verifier
    that has to guess which files are tests answers a different, vaguer question — the one
    that made every coverage finding in a real run come back refuted for want of a brief
    that asked this. It is a declared input with no default, like ``probe``, because an
    input that may be omitted is one the byte reconstruction can silently miss.
    """
    out = [brief.rstrip("\n"), "\n", PROBLEM_HEADING, "\n", problem, "\n",
           *_probe_section(probe), TEST_INVENTORY_HEADING]
    out.append(_listed(tests) if tests else f"\n{NO_TESTS_SENTENCE}\n")
    out += [CANDIDATES_HEADING, *_candidate_sections(candidates)]
    out += _schema_section(schema)
    return "".join(out)


def _parse_evidence(raw: object, field: str) -> dict | None:
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ResultError(f"field '{field}' must be an object or null (found {type(raw).__name__})")
    _check_keys(raw, field, frozenset(EVIDENCE_KEYS), EVIDENCE_REQUIRED, ResultError)
    if not isinstance(raw["truncated"], bool):
        raise ResultError(f"field '{field}.truncated' must be true or false")
    return {
        "argv": _argv(raw["argv"], f"{field}.argv"),
        "cwd": _text(raw["cwd"], f"{field}.cwd", empty_ok=True),
        "exit_status": _integer(raw["exit_status"], f"{field}.exit_status"),
        "output": _text(raw["output"], f"{field}.output", empty_ok=True),
        "truncated": raw["truncated"],
        RUN_KIND_KEY: _nullable_one_of(raw.get(RUN_KIND_KEY), f"{field}.{RUN_KIND_KEY}",
                                       RUN_KINDS),
        # Not ``empty_ok``: whitespace is not a sentence, and accepted it would render an
        # empty lead-in above the command that reads as a rendering fault rather than as a
        # worker that skipped the field.
        "shows": _text(raw["shows"], f"{field}.shows"),
    }


# What a path may not contain if it is a path at all. A verifier asked for the file that
# would settle a claim can answer with a description of one instead — "<caller of
# handleDispute (webhook handler) - not present; locate with grep -rn ...>" arrived as a
# single entry and was accepted, because normalizing separators is all it takes to turn a
# sentence into something shaped like a path. It then stood in the report's table of files
# to widen the scope to, beside real paths, where it can be neither opened nor counted.
#
# A space is the discriminator that matters; the brackets and quotes catch the same answer
# wearing punctuation. A real path can hold a space, and one named here would be dropped —
# which is the trade, and it falls the safe way: a path this refuses costs one entry in a
# table, and a sentence this admits costs the table its meaning.
_NOT_IN_A_PATH = frozenset(' \t\n\r<>"\'')


def _parse_needs_files(raw: object, field: str) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """The paths an unresolved verdict names as what would settle it, and the entries
    refused as prose rather than paths.

    Not checked against the snapshot, and that is the point: a file this review covers is
    one the verifier could have opened, so every path here is by definition outside the
    reviewed set. What IS checked is that each is a relative path and not a sentence —
    normalized the way a job's paths are, so the table that adds them up counts one file
    once however the verifier spelled the separator.

    A refused entry is DROPPED rather than raised on, so that one bad entry beside a good
    one costs the bad one alone. Emptying the list is what the caller acts on, under the
    rule it already applies to a verdict that names no file at all.
    """
    if raw is None:
        return (), ()
    if not isinstance(raw, list):
        raise ResultError(f"field '{field}' must be a list of paths or null "
                          f"(found {type(raw).__name__})")
    out: list[str] = []
    refused: list[str] = []
    for i, item in enumerate(raw):
        # A non-string entry is refused on the same terms as prose: `null` beside a good
        # path cost the whole verdict, against the promise two lines up.
        try:
            text = _text(item, f"{field}[{i}]", empty_ok=True)
        except ResultError:
            refused.append(repr(item))
            continue
        if _NOT_IN_A_PATH & set(text):
            refused.append(text)
            continue
        # Refused, not raised: the docstring's promise, and `_snapshot_parts` raises for a
        # `..`, an absolute path or a drive. Left to propagate, one such entry beside a good
        # one rejected the WHOLE verdict -- the finding was filed as blocked by the
        # environment, dropped from the table that prices open claims, and the verifier's
        # rationale thrown away -- which is the opposite of costing the bad entry alone. An
        # entry that names no file is refused on the same terms.
        try:
            parts = _snapshot_parts(item, f"{field}[{i}]")
        except ResultError:
            refused.append(text)
            continue
        if not parts:
            refused.append(text)
            continue
        path = "/".join(parts)
        if path not in out:
            out.append(path)
    return tuple(out), tuple(refused)


def _parse_revision(raw: object, field: str) -> dict | None:
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ResultError(f"field '{field}' must be an object or null (found {type(raw).__name__})")
    _check_keys(raw, field, frozenset(REVISION_KEYS), REVISION_KEYS, ResultError)
    return {
        "severity": _one_of(raw["severity"], f"{field}.severity", SEVERITIES),
        "rationale": _text(raw["rationale"], f"{field}.rationale"),
    }


# A verdict's nullable fields, and the status under which `null` is the value the
# acceptance rules below settle on — the only legal one for every field but `evidence`,
# and the assumed one for that. An omitted key there carries no information the engine
# does not already hold, so it is filled in rather than refused.
#
# This is not leniency about the contract — every rule below still applies, and a field
# whose value is NOT determined (a `test_first` on an unresolved verdict, evidence on a
# refuted one) is still required to be present. It is about what a missing key costs. One
# verifier omitted `unresolved_reason` on 18 verdicts whose status was not `unresolved`,
# which made `null` the only legal value; the unit was rejected whole, and 18 verdicts the
# engine could read perfectly well resolved `unresolved` instead. Left alone that run would
# have reported 87 established / 52 unresolved against a true 105 / 34 — a fifth of the
# headline lost to a field that says nothing.
# A refuted gap is a refuted finding by another name, so it fixes the same three fields.
_REFUSING = ("refuted", COVERAGE_GAP_REFUTED)
_DETERMINED_NULL = {
    "unresolved_reason": lambda status: status != "unresolved",
    "test_first": lambda status: status in _REFUSING,
    "revision": lambda status: status in _REFUSING,
    # Evidence is the one entry here that is a DEFAULT rather than the only legal value:
    # `confirmed_by_reading` may carry a documentary run (see the rule below), so an
    # evidence key that is present is read. Absent, it still means nothing ran, which is
    # what the overwhelming majority of readings are, and filling it costs nothing.
    "evidence": lambda status: status == "confirmed_by_reading",
    COVERED_BY_KEY: lambda status: status != COVERAGE_GAP_REFUTED,
    # Not filled from the status: a verdict may be unresolved for any of four reasons and
    # only one of them names files. The rule that decides it is the REASON, which is not
    # what this table is keyed by, so the field is simply optional and defaults to none.
}


def _fill_determined(raw: dict, field: str, keys: Sequence[str]) -> dict:
    """``raw`` with any omitted key whose value its status already fixes set to ``None``.

    The status itself is never filled in: it is the fact everything else is read against,
    and a verdict without one says nothing at all.

    ``keys`` bounds what may be filled to the fields this batch's verdicts HAVE. Filling
    ``covered_by`` on a defect verdict would add a key that verdict has no place for, and
    the check that follows would then refuse it as unknown — a refusal manufactured here
    rather than found in what the verifier wrote.
    """
    status = raw.get("status")
    if not isinstance(status, str):
        return raw
    missing = {key for key, determined in _DETERMINED_NULL.items()
               if key in keys and key not in raw and determined(status)}
    return {**raw, **dict.fromkeys(missing)} if missing else raw


def _parse_verdict(raw: object, field: str, batch: dict[str, bool],
                   asks: str = DEFECT_ASKS) -> Verdict:
    """One verdict, against the vocabulary and the rules of the question its batch put.

    ``asks`` chooses both. A defect verdict and a coverage verdict are answers to different
    questions and their statuses do not overlap, so a coverage status on a reader's
    candidate — or a defect status on a gap — is refused here by name, the same way a
    verdict naming a candidate its batch never held is. Nothing is translated between them:
    a verifier that answered the wrong question did not answer this one.
    """
    if not isinstance(raw, dict):
        raise ResultError(f"'{field}' must be a JSON object (found {type(raw).__name__})")
    keys = VERDICT_KEYS_FOR[asks]
    required = VERDICT_REQUIRED_FOR[asks]
    allowed = STATUSES_FOR[asks]
    # Asked BEFORE the field check, because a verdict answering the other question usually
    # carries the other question's fields too, and "unknown key 'covered_by'" names a
    # symptom where this names the mistake: a verifier read the wrong brief, or the batch
    # was dispatched with it.
    # `isinstance` first, and not for tidiness: `in` against a set raises TypeError on an
    # unhashable value, so a verifier answering `"status": []` or `{}` took the whole run down
    # here -- before the validation below could refuse it by name -- and a resume re-read the
    # same saved transcript and fell over again. A non-string status is a malformed verdict,
    # not a cross-question one, so it belongs to `_one_of` below, which names the field and
    # the values it accepts.
    if isinstance(raw.get("status"), str) and \
            raw["status"] in (set(VERDICT_STATUSES) | set(COVERAGE_STATUSES)) - set(allowed):
        other = COVERAGE_ASKS if asks == DEFECT_ASKS else DEFECT_ASKS
        raise ResultError(
            f"field '{field}.status' {raw['status']!r} is a {other} verdict on a {asks} "
            f"batch; this batch asks "
            + ("whether any test in scope constructs the input, and answers in "
               if asks == COVERAGE_ASKS else
               "whether the claimed failure is real, and answers in ")
            + f"{', '.join(allowed)}"
        )
    raw = _fill_determined(raw, field, keys)
    _check_keys(raw, field, frozenset(keys), required, ResultError)
    candidate = _text(raw["candidate"], f"{field}.candidate")
    if candidate not in batch:
        raise ResultError(f"field '{field}.candidate' {candidate!r} is not in this batch")
    status = _one_of(raw["status"], f"{field}.status", allowed)
    try:
        evidence = _parse_evidence(raw["evidence"], f"{field}.evidence")
        rationale = _text(raw["rationale"], f"{field}.rationale")
        revision = _parse_revision(raw["revision"], f"{field}.revision")
        test_first = _nullable_text(raw["test_first"], f"{field}.test_first")
        reason = _nullable_one_of(raw["unresolved_reason"], f"{field}.unresolved_reason",
                                  UNRESOLVED_REASONS)
        covered_by = (_nullable_text(raw[COVERED_BY_KEY], f"{field}.{COVERED_BY_KEY}")
                      if asks == COVERAGE_ASKS else None)
        stated = NEEDS_FILES_KEY in raw
        needs_files, refused_files = _parse_needs_files(
            raw.get(NEEDS_FILES_KEY), f"{field}.{NEEDS_FILES_KEY}")
    except ResultError as exc:
        raise ResultError(f"{exc} (candidate {candidate})") from None
    if asks == DEFECT_ASKS:
        # The acceptance rules. Rule 4 of the sweep: a claim that can be run is run, and no
        # evidence means unresolved — never "confirmed by reading".
        if status == "reproduced" and evidence is None:
            raise ResultError(
                f"'{field}' ({candidate}) is reproduced with no evidence; a reproduction "
                f"produces argv, exit status and output, or the verdict is unresolved"
            )
        if status == "confirmed_by_reading" and batch[candidate]:
            raise ResultError(
                f"'{field}' ({candidate}) is confirmed_by_reading but the candidate carries a "
                f"proposed reproduction; a claim that can be run is run, and one that could "
                f"not execute is unresolved, never confirmed by reading"
            )
        # A DOCUMENTARY run is admitted here and an executed one is not, because they are
        # not the same act. Running the code and watching it fail is a reproduction, and
        # calling that a reading throws away the strongest thing the verifier has. Searching
        # the tree to check that a reading holds is still the reading — and a verifier that
        # does it, as they routinely do, had no status left to write: two verdicts were
        # rejected for exactly this, and their candidates fell to unresolved with nothing
        # wrong with the claims. `run_kind` must SAY documentary; unstated is refused with
        # executed, since a rule that reads silence as the permissive answer is not a rule.
        if (status == "confirmed_by_reading" and evidence is not None
                and evidence.get(RUN_KIND_KEY) != "documentary"):
            raise ResultError(
                f"'{field}' ({candidate}) is confirmed_by_reading with evidence whose "
                f"{RUN_KIND_KEY} is {evidence.get(RUN_KIND_KEY)!r}; a claim whose code was "
                f"RUN is reproduced or refuted, and only a documentary run — a search that "
                f"checks a reading — stands beside a reading"
            )
        if status == "refuted" and evidence is None and batch[candidate]:
            raise ResultError(
                f"'{field}' ({candidate}) is refuted with nothing run, but the candidate carries a "
                f"proposed reproduction; a claim that can be run is refuted by running it, and one "
                f"that could not run is unresolved"
            )
    else:
        # A gap is settled by naming a test, not by running one. So there is no rule here
        # about a proposed reproduction: an auditor proposes one where it can, and a
        # verifier that reads the tests instead has still answered the question asked.
        if status == COVERAGE_GAP_REFUTED and covered_by is None:
            raise ResultError(
                f"'{field}' ({candidate}) refutes the gap and names no test in "
                f"'{COVERED_BY_KEY}'; a test in scope constructing the input is the only "
                f"thing that refutes a coverage gap, and a refutation without one is an "
                f"opinion about whether the code is wrong, which is not the question"
            )
        if status != COVERAGE_GAP_REFUTED and covered_by is not None:
            raise ResultError(
                f"'{field}' ({candidate}) is {status} and names a covering test; only a "
                f"refuted gap has one, and a gap that stands is a test still to write"
            )
    if status in _REFUSING and revision is not None:
        raise ResultError(
            f"'{field}' ({candidate}) is {status} and revises the severity; a refuted finding "
            f"keeps its proposal"
        )
    # An established finding is work somebody is about to do, and the test that should be red
    # before the fix lands is what turns a reading two models agreed on into something a third
    # person can check. A refuted one has nothing to fix, so a test beside it claims a defect
    # the verdict just denied. A confirmed gap IS that test, named the same way.
    if status in ESTABLISHED_FOR[asks] and test_first is None:
        raise ResultError(
            f"'{field}' ({candidate}) is {status} with no test_first; an established finding "
            f"names the test that should be red before the fix lands"
        )
    if status in _REFUSING and test_first is not None:
        raise ResultError(
            f"'{field}' ({candidate}) is {status} and names a test_first; a refuted finding has "
            f"nothing to fix, so there is no test to write first"
        )
    # The reason is collected so the unresolved set can be grouped by what would settle each,
    # and an environment failure told apart from an open question about the code. Neither is
    # possible from a reason the verifier did not give, and neither is guessable.
    if status == "unresolved" and reason is None:
        raise ResultError(
            f"'{field}' ({candidate}) is unresolved with no unresolved_reason; say which of "
            f"{', '.join(UNRESOLVED_REASONS)} would settle it"
        )
    if status != "unresolved" and reason is not None:
        raise ResultError(
            f"'{field}' ({candidate}) is {status} and names an unresolved_reason; only an "
            f"unresolved verdict has something left to settle"
        )
    # Present and empty is refused; ABSENT is not. A verdict from before the field existed
    # names nothing and is read as it always was; one that writes the field and leaves it
    # empty is saying a file outside the scope settles this and declining to say which,
    # which is the one shape that makes the table below quietly short.
    if stated and reason == NEEDS_A_FILE and not needs_files:
        # The refused entries are named here because this is the only place a dispatcher
        # learns of them: one refused beside a path that stood costs nothing and says
        # nothing, and one that emptied the list is the whole reason this verdict is going
        # back. Quoted as written, so the verifier can see which answer was not a path.
        prose = (f" (refused as prose rather than a path: {'; '.join(refused_files)})"
                 if refused_files else "")
        raise ResultError(
            f"'{field}' ({candidate}) needs a file outside the scope and names none in "
            f"'{NEEDS_FILES_KEY}'{prose}; give the path that would settle it, so the cost of "
            f"widening the scope can be counted rather than read out of prose"
        )
    if needs_files and reason != NEEDS_A_FILE:
        raise ResultError(
            f"'{field}' ({candidate}) names files in '{NEEDS_FILES_KEY}' and its reason is "
            f"{reason!r}; only {NEEDS_A_FILE} is settled by a file this review does not cover"
        )
    return Verdict(candidate=candidate, status=status, evidence=evidence,
                   rationale=rationale, revision=revision, test_first=test_first,
                   unresolved_reason=reason, covered_by=covered_by,
                   needs_files=needs_files)


def _candidate_of(raw: object) -> str | None:
    """The candidate id a malformed verdict names, when it names one readably.

    Without it a rejected verdict cannot be attributed, and the whole unit is the only
    honest thing to fail.
    """
    if isinstance(raw, dict) and isinstance(raw.get("candidate"), str):
        return raw["candidate"]
    return None


def parse_verifier_result(obj: object, unit_id: str, batch: dict[str, bool],
                          asks: str = DEFECT_ASKS) -> tuple[tuple[Verdict, ...], tuple[str, ...]]:
    """Strictly parse one verification result, returning its verdicts and the rejections.

    ``batch`` maps each candidate id in the unit to whether it carries a proposed
    reproduction. Every candidate gets exactly one verdict: one missing, one repeated, or
    one for a candidate outside the batch rejects the RESULT by name — silence is not
    ``unresolved``, and nothing is invented here.

    **A verdict that breaks a rule costs its own candidate and no other.** The rules are
    unchanged and none is relaxed; what changes is the blast radius. A batch is one
    dispatch of many independent judgments, and one verdict the engine cannot read says
    nothing about the twenty beside it — a verifier that omits a field on one candidate
    still answered the rest. Failing the unit over it turned every one of those answers
    into ``unresolved``, which reads to a person as "nobody checked this" when somebody
    did. The rejected candidate resolves ``unresolved`` as a candidate NO verdict came back
    for — the stand-in written for it is not an answer and :func:`resolve` does not count
    it as one — and the coverage section names it with the parse error, so nothing is
    quietly upgraded and the reader can see exactly what was thrown away.

    What still fails the unit is a result whose SHAPE cannot be trusted: not an object,
    ``verdicts`` not a list, an unknown top-level key, a candidate answered twice, or one
    answered that was never in the batch. In each of those the engine cannot say which
    judgment belongs to which candidate, and attributing them would be guesswork.
    """
    try:
        if not isinstance(obj, dict):
            raise ResultError(f"result must be a JSON object (found {type(obj).__name__})")
        _check_keys(obj, "result", frozenset(VERIFIER_RESULT_KEYS), VERIFIER_RESULT_KEYS, ResultError)
        _text(obj["summary"], "summary", empty_ok=True)
        if not isinstance(obj["verdicts"], list):
            raise ResultError(f"field 'verdicts' must be a list (found {type(obj['verdicts']).__name__})")
        verdicts: list[Verdict] = []
        rejected: list[str] = []
        for i, item in enumerate(obj["verdicts"]):
            field = f"verdicts[{i}]"
            try:
                verdicts.append(_parse_verdict(item, field, batch, asks))
                continue
            except ResultError as exc:
                named = _candidate_of(item)
                # A verdict naming a candidate outside the batch is the routing failing,
                # not one judgment being unreadable, and it is refused as it always was.
                if named is None or named not in batch:
                    raise
                rejected.append(f"{named}: {exc}")
                # **A stand-in is "no answer", not an answer.** It carries no settling
                # reason: a reason is what a verifier who looked at the candidate says
                # would settle it, and nothing readable came back from this one. Given one
                # here, the defect was filed under that reason's heading with the parse
                # error printed as the checker's rationale — a verdict nobody gave. The
                # rationale is kept, because ``findings.json`` is the record and the
                # diagnostic is the true reason; ``from_verifier`` is what keeps it out of
                # every payload and every rendering that speaks for a checker.
                verdicts.append(Verdict(
                    candidate=named, status="unresolved", evidence=None,
                    rationale=f"the engine could not read this verdict — {exc}",
                    revision=None, test_first=None,
                    unresolved_reason=None,
                    from_verifier=False))
        seen: set[str] = set()
        for verdict in verdicts:
            if verdict.candidate in seen:
                raise ResultError(f"candidate {verdict.candidate!r} has a verdict twice; exactly one each")
            seen.add(verdict.candidate)
        for candidate in batch:
            if candidate not in seen:
                raise ResultError(
                    f"candidate {candidate!r} has no verdict; every candidate in the batch "
                    f"gets exactly one"
                )
        return tuple(verdicts), tuple(rejected)
    except ResultError as exc:
        raise ResultError(f"{unit_id}: {exc}") from None


@dataclass(frozen=True)
class Probe:
    """What the capability probe established, or the explicit unknown that stands where it
    did not. ``state`` is the unit's, by the same three-way rule every other unit is read
    by; ``build``, ``tests`` and ``summary`` are the parsed answers and are ``None`` for
    every state but complete.

    A probe that failed, never landed, or was never planned is not dropped and does not
    default to ``no``: the tree might build perfectly well and nobody asked it to. The
    difference between *it does not build* and *nobody found out* is the whole reason the
    probe exists, so it survives into the verifier payloads and into the route record as the
    word unknown with the reason beside it.
    """

    unit: str
    state: str
    reason: str | None
    build: dict | None
    tests: dict | None
    summary: str | None


def _parse_probe_attempt(raw: object, field: str, *, status_decides: bool) -> dict:
    """One probed capability. ``status_decides`` says whether the exit status settles the
    answer, which is true of the build and false of the tests: a suite that ran and failed
    exits non-zero and still answers ``yes``, because the question there is whether anything
    can execute, not whether it passes."""
    if not isinstance(raw, dict):
        raise ResultError(f"field '{field}' must be a JSON object (found {type(raw).__name__})")
    _check_keys(raw, field, frozenset(PROBE_ATTEMPT_KEYS), PROBE_ATTEMPT_KEYS, ResultError)
    if not isinstance(raw["truncated"], bool):
        raise ResultError(f"field '{field}.truncated' must be true or false")
    answer = _one_of(raw["answer"], f"{field}.answer", PROBE_ANSWERS)
    argv = [] if raw["argv"] == [] else _argv(raw["argv"], f"{field}.argv")
    status = None if raw["exit_status"] is None else _integer(raw["exit_status"], f"{field}.exit_status")
    # The two rules that keep an answer and its evidence from contradicting each other. A
    # `yes` or a `no` is a claim about something that ran, and nothing ran without a command;
    # an exit status without a command is a status belonging to no process.
    if answer != PROBE_UNKNOWN_WORD and not argv:
        raise ResultError(
            f"field '{field}' answers {answer!r} with an empty argv; only {PROBE_UNKNOWN_WORD!r} "
            f"may say that nothing was run"
        )
    if not argv and status is not None:
        raise ResultError(
            f"field '{field}' reports exit status {status} with an empty argv; nothing ran, "
            f"so nothing exited"
        )
    if argv and status is None:
        raise ResultError(
            f"field '{field}' names a command and no exit status; a command that ran has one, "
            f"and a program that could not start has the launcher's"
        )
    # Where the status settles the answer, an answer that disagrees with it is not a judgment
    # call, it is a contradiction: a build reported as succeeding by a command that exited
    # non-zero would put "the tree builds" in front of every verifier on the strength of a
    # failed compile.
    if status_decides and status is not None:
        if answer == "yes" and status != 0:
            raise ResultError(
                f"field '{field}' answers 'yes' and exits {status}; a build that succeeded "
                f"exits 0, so say 'no' or 'unknown'"
            )
        if answer == "no" and status == 0:
            raise ResultError(
                f"field '{field}' answers 'no' and exits 0; a command that exited 0 did not "
                f"fail, so say 'yes' or 'unknown'"
            )
    return {
        "answer": answer,
        "argv": argv,
        "cwd": _text(raw["cwd"], f"{field}.cwd", empty_ok=True),
        "exit_status": status,
        "output": _text(raw["output"], f"{field}.output", empty_ok=True),
        "truncated": raw["truncated"],
    }


def parse_probe_result(obj: object, unit_id: str) -> dict:
    """Strictly parse the capability probe's result against the probe schema's vocabulary,
    the way a reading result is parsed and for the same reason: no runtime flag ever sees a
    file an agent wrote. Raises :class:`ResultError` naming the unit and the field."""
    try:
        if not isinstance(obj, dict):
            raise ResultError(f"result must be a JSON object (found {type(obj).__name__})")
        _check_keys(obj, "result", frozenset(PROBE_RESULT_KEYS), PROBE_RESULT_KEYS, ResultError)
        out = {name: _parse_probe_attempt(obj[name], name, status_decides=name == "build")
               for name in PROBE_ATTEMPTS}
        out["summary"] = _text(obj["summary"], "summary", empty_ok=True)
        return out
    except ResultError as exc:
        raise ResultError(f"{unit_id}: {exc}") from None


def read_probe_result(rundir: Path, units: Sequence[dict]) -> Probe:
    """The probe unit's answer, or the explicit unknown standing for it. Never raises for a
    probe that did not come back: a run whose tree could not be probed still completes, and
    says so."""
    # ``plan`` writes exactly one, so the first is the one. A listing with two is a run
    # directory this engine did not write, and the answer to give there is still an answer
    # rather than a stop: nothing downstream is safer for the report having refused.
    planned = [unit for unit in units if unit.get("kind") == PROBE_KIND]
    if not planned:
        return Probe(PROBE_UNIT_ID, UNIT_MISSING,
                     f"{UNITS_FILE_NAME} lists no {PROBE_KIND} unit", None, None, None)
    unit_id = planned[0]["id"]
    try:
        state, payload = _read_unit_file(rundir, unit_id)
    except StorageStop:
        # Never softened: the volume failing is not a probe that came back unhelpful, and
        # the next thing this stage writes is going to the same volume.
        raise
    except RunDirError as exc:
        # A unit holding both result.json and error.txt refuses the run for every other
        # kind, because a dispatcher that wrote both is not following the protocol. For the
        # probe it is a failed probe instead: a retry after a failure is exactly how both
        # files appear, the reason still reaches the route record and the summary, and
        # stopping here would trade a weaker answer for no answer at all.
        return Probe(unit_id, UNIT_FAILED, _writable(str(exc)), None, None, None)
    if state != UNIT_COMPLETE:
        return Probe(unit_id, state, _writable(payload), None, None, None)
    try:
        parsed = parse_probe_result(payload, unit_id)
    except ResultError as exc:
        # Escaped, because this reason is the first worker-derived text the engine renders
        # into a PAYLOAD rather than into a JSON record. A diagnostic quotes its offending
        # input — an unknown key, say — and that input can hold a lone surrogate no UTF-8
        # write accepts, which crashed route after the batch directories existed.
        return Probe(unit_id, UNIT_FAILED, _writable(str(exc)), None, None, None)
    return Probe(unit_id, UNIT_COMPLETE, None, parsed["build"], parsed["tests"], parsed["summary"])


def _probe_attempt_lines(label: str, attempt: dict) -> list[str]:
    """One probed capability as payload pieces. Every string is the probe's own text, so it
    goes through the same fencing a candidate's failure does: an answer line cannot open a
    heading, and ``argv`` is rendered as JSON, which is one line whatever it holds."""
    out = [f"- {label}: {attempt['answer']}\n"]
    if not attempt["argv"]:
        out.append("  nothing was run\n")
        return out
    out.append(f"  argv: {json.dumps(attempt['argv'])}\n")
    out += _payload_field("  cwd: ", attempt["cwd"])
    out.append(f"  exit status: {'not started' if attempt['exit_status'] is None else attempt['exit_status']}\n")
    return out


def _probe_section(probe: Probe) -> list[str]:
    """The probe's answer as a verifier payload reads it. A declared input of that payload,
    not an aside: it is why a verifier bothers to construct a reproduction, or knows not to.

    A probe that did not come back renders the word unknown and the reason, never silence
    and never a `no` — a verifier told nothing would read the tree as unrunnable and report
    unresolved for claims it could have run.

    The captured output is deliberately not rendered. It is bounded at four kilobytes and
    would be repeated into every batch, where it instructs nobody: what a verifier acts on
    is the answer, the command that produced it and the probe's own summary. The full output
    stays in the unit's ``result.json`` for whatever reads the run directory next."""
    out = [PROBE_HEADING, "\nWhat a separate unit found out about this tree before your batch was "
                          "built, by trying it. It read no code for defects and raised no finding.\n\n"]
    if probe.state != UNIT_COMPLETE:
        out.append(f"- Whether the tree builds: {PROBE_UNKNOWN_WORD}\n")
        out.append(f"- Whether its tests run: {PROBE_UNKNOWN_WORD}\n")
        out += _payload_field(f"  the probe unit {probe.state}: ", probe.reason or "no reason recorded")
        out.append("\nUnknown is not no. Judge each candidate on its own terms, and where you can "
                   "run something, run it.\n")
        return out
    out += _probe_attempt_lines("Whether the tree builds", probe.build)
    out += _probe_attempt_lines("Whether its tests run", probe.tests)
    out += _payload_field("- What the probe reported: ", probe.summary or "")
    return out


@dataclass(frozen=True)
class RouteCompanions:
    """What ``route`` ships beside the engine and every verification payload is built
    from. Loaded before the first write, as ``plan``'s are."""

    verifier_brief: str
    verifier_schema: dict
    coverage_brief: str
    coverage_schema: dict

    def brief_for(self, asks: str) -> str:
        return self.coverage_brief if asks == COVERAGE_ASKS else self.verifier_brief

    def schema_for(self, asks: str) -> dict:
        return self.coverage_schema if asks == COVERAGE_ASKS else self.verifier_schema


def load_route_companions() -> RouteCompanions:
    return RouteCompanions(
        verifier_brief=load_brief("verifier"),
        verifier_schema=load_schema(VERIFIER_SCHEMA_NAME),
        coverage_brief=load_brief("coverage-verifier"),
        coverage_schema=load_schema(COVERAGE_VERIFIER_SCHEMA_NAME),
    )


def _read_run_json(rundir: Path, name: str) -> object:
    path = rundir / name
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise RunDirError(f"{path} is missing; run plan first") from None
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        raise RunDirError(f"cannot read {path}: {exc}") from exc


def _read_units_at_stage(rundir: Path, stage: str | Sequence[str], hint: str) -> dict:
    """The units listing, refused unless it is at one of the stages this caller can read.

    More than one is accepted for ``report`` alone, and for one reason: the synthesis round
    is optional, so a run that stops after clustering and a run that went through synthesis
    are both complete runs to report on. Every other stage takes exactly one.
    """
    stages = (stage,) if isinstance(stage, str) else tuple(stage)
    doc = _read_run_json(rundir, UNITS_FILE_NAME)
    if not isinstance(doc, dict) or not isinstance(doc.get("units"), list):
        raise RunDirError(f"{rundir / UNITS_FILE_NAME} is not a units listing")
    if doc.get("stage") not in stages:
        raise RunDirError(
            f"{rundir / UNITS_FILE_NAME} is at stage {doc.get('stage')!r}, not "
            f"{' or '.join(repr(name) for name in stages)}; {hint}"
        )
    return doc


def coverage_declined(job: dict) -> bool:
    """Whether the job took the coverage question off the table.

    Read off the job the run directory keeps, because it is the one input to the auditor
    answer that is a decision rather than a fact about the tree, and the snapshot records the
    tree. A job file that predates the field says nothing, which is the engine deciding.
    """
    return job.get("coverage") is False


def _read_job(rundir: Path) -> dict:
    """The job this run answers, from the copy in the run directory.

    Checked for the shapes the report renders and no further — deliberately NOT through
    :func:`load_job`, which resolves ``root`` against the filesystem and refuses a directory
    that is not there. A report is rendered weeks after its run, on a machine where that tree
    may have moved or may never have existed, and it is built from the run directory alone. A
    reader asking what the job said must not be answered with a refusal about a path nothing
    on this page depends on.
    """
    doc = _read_run_json(rundir, JOB_FILE_NAME)
    try:
        if not isinstance(doc["problem"], str) or not doc["problem"].strip():
            raise TypeError("problem")
        for key in ("root", "partition"):
            if not isinstance(doc[key], str):
                raise TypeError(key)
        # Each rendered as its own line of "The job", so a record that cannot carry one is
        # refused here rather than rendering a line that silently says nothing was asked for.
        for key in ("exclude", "lenses"):
            if not isinstance(doc[key], list):
                raise TypeError(key)
        # Each lens is rendered as prose and tagged, and every tag the appendix cites is
        # looked up in that list, so an entry that is not text is refused rather than
        # rendered as whatever ``str`` would make of it.
        if not doc["lenses"] or not all(isinstance(text, str) for text in doc["lenses"]):
            raise TypeError("lenses")
        for key in ("files", "areas"):
            if key in doc and not isinstance(doc[key], list):
                raise TypeError(key)
        if "coverage" in doc and not isinstance(doc["coverage"], bool):
            raise TypeError("coverage")
        if "questions" in doc and not isinstance(doc["questions"], str):
            raise TypeError("questions")
    except (KeyError, TypeError, AttributeError) as exc:
        raise RunDirError(f"{rundir / JOB_FILE_NAME} is not a job this report can "
                          f"describe: {exc}") from exc
    return doc


def _read_problem(rundir: Path) -> str:
    job = _read_run_json(rundir, JOB_FILE_NAME)
    if not isinstance(job, dict) or not isinstance(job.get("problem"), str) or not job["problem"].strip():
        raise RunDirError(f"{rundir / JOB_FILE_NAME} carries no problem statement")
    return job["problem"]


def _read_job_notes(rundir: Path) -> dict[str, str] | None:
    """Where each job field came from, or ``None`` where nothing recorded it.

    **A malformed notes file is refused rather than ignored.** Dropped, the report would say
    no record of the interview's choices is here — which is a statement about the run
    directory, and one that is false when a record is sitting in it unread.
    """
    path = rundir / JOB_NOTES_FILE_NAME
    if _lstat_or_absent(path, JOB_NOTES_FILE_NAME, RunDirError) is None:
        return None
    doc = _read_run_json(rundir, JOB_NOTES_FILE_NAME)
    if not isinstance(doc, dict):
        raise RunDirError(f"{path} is not a JSON object naming where each job field came from")
    for field, origin in doc.items():
        if field not in JOB_NOTE_FIELDS:
            raise RunDirError(
                f"{path} names {field!r}, which is not a job field it can speak for; "
                f"the fields are: {', '.join(JOB_NOTE_FIELDS)}"
            )
        if origin not in JOB_NOTE_ORIGINS:
            raise RunDirError(
                f"{path} records {field!r} as {origin!r}; each field is one of: "
                f"{', '.join(JOB_NOTE_ORIGINS)}"
            )
    return doc


@dataclass(frozen=True)
class ReportNotes:
    """``report-notes.json`` as checked against this run's defects.

    ``answers`` holds ``{"question", "answer", "defects"}`` with ``defects`` a tuple of ids;
    ``corrections`` holds ``{"target", "reason"}``; ``caveats`` holds strings. Every id in
    either is one ``findings.json`` carries as a defect, or a :data:`BUILD_CHECK_TARGETS`
    word for a correction."""

    answers: tuple[dict, ...]
    corrections: tuple[dict, ...]
    caveats: tuple[str, ...]

    def corrected(self) -> frozenset[str]:
        return frozenset(entry["target"] for entry in self.corrections)


def _read_report_notes(rundir: Path, defects: Sequence[str]) -> ReportNotes | None:
    """The operator's notes, or ``None`` where there are none.

    **Refused by name rather than ignored when malformed**, for the reason
    ``job-notes.json`` is: dropped, the report would say nobody wrote any notes while a
    file of them sits in the run directory unread. An unknown key, a missing field, empty
    text, a correction without a reason and an id this run has no defect for are each
    refused. A correction is checked against the ids alone, so a correction written for
    one clustering and read against another names whatever now carries that id.
    """
    path = rundir / REPORT_NOTES_FILE_NAME
    if _lstat_or_absent(path, REPORT_NOTES_FILE_NAME, RunDirError) is None:
        return None
    doc = _read_run_json(rundir, REPORT_NOTES_FILE_NAME)
    known = frozenset(defects)

    def defect_id(raw: object, field: str) -> str:
        named = _parse_text(raw, field, RunDirError)
        if named not in known:
            raise RunDirError(f"field '{field}' names {named!r}, and no defect in "
                              f"{FINDINGS_NAME} carries that id")
        return named

    def entries(key: str) -> list:
        raw = doc.get(key, [])
        if not isinstance(raw, list):
            raise RunDirError(f"field '{key}' must be a list (found {type(raw).__name__})")
        return raw

    try:
        if not isinstance(doc, dict):
            raise RunDirError("it must be a JSON object holding any of: "
                              + ", ".join(REPORT_NOTES_KEYS))
        _check_keys(doc, "report notes", frozenset(REPORT_NOTES_KEYS), (), RunDirError)
        answers = []
        for i, raw in enumerate(entries("answers")):
            field = f"answers[{i}]"
            if not isinstance(raw, dict):
                raise RunDirError(f"field '{field}' must be an object")
            _check_keys(raw, field, frozenset(REPORT_NOTE_ANSWER_KEYS),
                        ("question", "answer"), RunDirError)
            cited = raw.get("defects", [])
            if not isinstance(cited, list):
                raise RunDirError(f"field '{field}.defects' must be a list of defect ids")
            answers.append({
                "question": _parse_text(raw["question"], f"{field}.question", RunDirError),
                "answer": _parse_text(raw["answer"], f"{field}.answer", RunDirError),
                "defects": tuple(defect_id(d, f"{field}.defects[{j}]")
                                 for j, d in enumerate(cited)),
            })
        corrections, aimed = [], set()
        for i, raw in enumerate(entries("corrections")):
            field = f"corrections[{i}]"
            if not isinstance(raw, dict):
                raise RunDirError(f"field '{field}' must be an object")
            _check_keys(raw, field, frozenset(REPORT_NOTE_CORRECTION_KEYS),
                        REPORT_NOTE_CORRECTION_KEYS, RunDirError)
            target = _parse_text(raw["target"], f"{field}.target", RunDirError)
            if target not in BUILD_CHECK_TARGETS:
                target = defect_id(target, f"{field}.target")
            # One mark per entry, pointing at one correction: two for one target would
            # leave the mark pointing at whichever a reader happened to find first.
            if target in aimed:
                raise RunDirError(f"field '{field}.target' corrects {target!r} a second "
                                  f"time; say everything about it in one correction")
            aimed.add(target)
            corrections.append({"target": target, "reason": _parse_text(
                raw["reason"], f"{field}.reason", RunDirError)})
        caveats = tuple(_parse_text(raw, f"caveats[{i}]", RunDirError)
                        for i, raw in enumerate(entries("caveats")))
        if not (answers or corrections or caveats):
            raise RunDirError("it records nothing; remove it, or add an answer, a "
                              "correction or a caveat")
    except RunDirError as exc:
        raise RunDirError(f"{path}: {exc}") from None
    return ReportNotes(tuple(answers), tuple(corrections), caveats)


def _read_area_tests(rundir: Path) -> dict[str, tuple[str, ...]]:
    """Every area to the test-named files a coverage verifier for it can open.

    **The same tests the AUDITOR was given**, which is the whole point: a verifier deciding
    whether a gap is real has to look where the auditor looked, and a gap raised over a test
    the verifier cannot open can only ever come back unresolved. Where the plan derived the
    subjects, that is the tests under the area's ``tested`` entries — wherever in the
    partition those tests happen to sit. Where it did not, it is the area's own test files
    and the ones its ``also_read`` reaches, which is what a subject-mode auditor is handed.

    Read from ``areas.json`` rather than recomputed from the candidates, because the answer
    has to be the files the auditor could have looked at — including the tests it found
    nothing in. A payload listing only the tests some gap already mentions would let a
    verifier refute nothing it was not already pointed at.
    """
    areas = _read_run_json(rundir, "areas.json")
    try:
        return {area["id"]: tuple(sorted(
            {link["test"] for entry in area["tested"] for link in entry["tests"]}
            if area.get("tested") else
            {path for path in (*area["files"], *area.get("also_read", ()))
             if is_test_path(path)}))
            for area in areas["areas"]}
    except (KeyError, TypeError) as exc:
        raise RunDirError(f"areas.json under {rundir} is not the engine's: {exc}") from exc


def _read_context(rundir: Path) -> frozenset[str]:
    """The snapshot files the job left out of scope, off ``inventory.json``."""
    inventory = _read_run_json(rundir, "inventory.json")
    try:
        return frozenset(entry["path"] for entry in inventory["context"])
    except (KeyError, TypeError) as exc:
        raise RunDirError(f"inventory.json under {rundir} is not the engine's: {exc}") from exc


def _read_audited(rundir: Path) -> dict[str, frozenset[str]]:
    """Every audited area to the source files its auditor may file a finding in.

    An area with no ``tested`` entry is absent rather than empty — a subject-mode run, or
    one planned before the field existed, has no narrowed set and its auditor lands by the
    same rule as a reader.
    """
    areas = _read_run_json(rundir, "areas.json")
    try:
        return {area["id"]: frozenset(entry["path"] for entry in area["tested"])
                for area in areas["areas"] if area.get("tested")}
    except (KeyError, TypeError) as exc:
        raise RunDirError(f"areas.json under {rundir} is not the engine's: {exc}") from exc


def _locations_for(unit: dict, files, audited: dict[str, frozenset[str]]):
    """Where one unit's findings may be located.

    A reader may file anywhere in the snapshot, as it always could. **An auditor may file
    only in the files it was asked about**, which is what makes one test reaching two
    subjects safe: the same test is read by two auditors, and neither can raise the other's
    gap, so a gap cannot arrive twice from units that cannot see each other.

    A finding outside the set costs itself and the unit lands with the rest, by the same
    rule as a location outside the snapshot — the auditor answered, and one misplaced
    finding is not grounds for throwing the other thirty away.
    """
    if unit.get("kind") != AUDITOR_KIND:
        return files
    return audited.get(unit.get("area"), files)


def _read_owner(rundir: Path) -> dict[str, str]:
    """Every snapshot file to the area closure assigned it to. One source for both the
    location check and the attribution: closure put every inventoried file in exactly
    one area, so ``areas.json`` IS the file set, and a location that passes the check
    always has an owner."""
    areas = _read_run_json(rundir, "areas.json")
    try:
        return {path: area["id"] for area in areas["areas"] for path in area["files"]}
    except (KeyError, TypeError) as exc:
        raise RunDirError(f"areas.json under {rundir} is not the engine's: {exc}") from exc


def _claim_candidates(path: Path) -> None:
    """``route``'s first write: create ``candidates.json`` exclusively, empty, before any batch
    directory. Two routes that both passed the checks cannot both create it, so the one that
    loses stops having written nothing, and a later failure's clean-up removes only this
    route's writes; a route with no verification batch has nothing else to collide on. The
    route record replaces the empty file once the batches are written.

    What it still refuses, now that an interrupted route's claim is reclaimed rather than
    refused, is a route running **at the same time** as this one, and a link at that name,
    which exclusive creation refuses rather than following out of the run directory."""
    try:
        with open(path, "x", encoding="utf-8"):
            pass
    except FileExistsError as exc:
        raise RunDirError(
            f"{path} already exists, so another route is claiming this run directory or a "
            f"link is in the way; route claims that file before it writes anything"
        ) from exc
    except OSError as exc:
        raise InventoryError(f"cannot write {path}: {exc}") from exc


def write_route(rundir: Path, units_doc: dict, states: Sequence[UnitState],
                candidates: Sequence[Candidate], batches: Sequence[Batch],
                companions: RouteCompanions, problem: str, probe: Probe,
                tests_of: dict[str, tuple[str, ...]] | None = None) -> None:
    """Claim ``candidates.json``, write one ``units/<id>/`` per batch, then the route record
    into ``candidates.json``, then ``units.json`` at the verification stage listing the
    reading units and the batches. Every file lands by the exclusive-create-then-replace
    rule, and a failure part-way takes back what this route wrote.

    **What an interrupted route left is reclaimed, not refused.** The caller holds route's
    claim on the run directory and reached here with the marker still at the reading stage,
    which together say no other route is working and none has committed — so the candidates
    file and the batch directories on disk are an interrupted route's and are this one's to
    take back. The manifest is this route's own batches and its own record file; a directory
    holding anything route does not write is refused rather than deleted."""
    candidates_path = rundir / CANDIDATES_FILE_NAME
    for batch in batches:
        target = rundir / UNITS_DIR / batch.id
        if _not_a_unit_directory(target):
            raise RunDirError(
                f"{target} already exists and is a link or a file rather than a unit "
                f"directory this stage wrote; move it aside and route again"
            )
    _reclaim("route", rundir, (candidates_path,),
             tuple((rundir / UNITS_DIR / batch.id, UNIT_CONTENTS) for batch in batches),
             (rundir / UNITS_FILE_NAME,))
    created: list[Path] = []
    _claim_candidates(candidates_path)
    try:
        by_id = {cand.id: asdict(cand) for cand in candidates}
        listing = list(units_doc["units"])
        for batch in batches:
            unit_dir = rundir / UNITS_DIR / batch.id
            try:
                unit_dir.mkdir(parents=True)
            except OSError as exc:
                raise InventoryError(f"cannot create {unit_dir}: {exc}") from exc
            created.append(unit_dir)
            held = [by_id[cid] for cid in batch.candidates]
            schema = companions.schema_for(batch.asks)
            if batch.asks == COVERAGE_ASKS:
                # The tests the AUDITORS saw, not only the tests of the area the batch was
                # filed under. Routing files a finding under the area that owns the source
                # it names; a coverage finding is usually raised from a tests area that
                # reached that source through `also_read`, so the two differ by design. Keyed
                # on the batch's area alone, the verifier was handed the source owner's tests
                # -- often none -- while being told to check the supplied tests, and a gap a
                # test already covered came back confirmed. The batch's own area stays first;
                # every raiser's area follows, sorted, and a test named twice appears once.
                raisers = sorted({r["area"] for cand in held for r in cand["raised_by"]}
                                 - {batch.area})
                in_scope = tuple(dict.fromkeys(
                    t for area in (batch.area, *raisers)
                    for t in (tests_of or {}).get(area, ())))
                text = render_coverage_verifier_payload(
                    companions.coverage_brief, problem, held, probe, in_scope, schema=schema)
            else:
                text = render_verifier_payload(companions.verifier_brief, problem, held,
                                               probe, schema=schema)
            write_text(unit_dir / PAYLOAD_NAME, text)
            write_json(unit_dir / SCHEMA_NAME, schema)
            lines, size = measure_payload(text)
            listing.append({
                "id": batch.id, "kind": VERIFIER_KIND, "area": batch.area, "lane": batch.lane,
                "lens": None, "finder": batch.finder, "candidates": list(batch.candidates),
                "routing": batch.routing, "asks": batch.asks,
                "payload": f"{UNITS_DIR}/{batch.id}/{PAYLOAD_NAME}",
                "schema": f"{UNITS_DIR}/{batch.id}/{SCHEMA_NAME}",
                "payload_lines": lines,
                "payload_bytes": size,
            })
        by_unit = {unit["id"]: unit for unit in units_doc["units"]}
        write_json(candidates_path, {
            "stage": ROUTED_STAGE,
            # Its own record, not a row among the reading units: the probe raises no finding,
            # so counting it as one would move the reading-unit totals the rung sentence is
            # derived from, and a run whose probe failed would read as a run that read less.
            "probe": asdict(probe),
            "units": [{
                "id": state.id, "kind": by_unit[state.id]["kind"], "area": by_unit[state.id]["area"],
                "lane": by_unit[state.id]["lane"], "lens": by_unit[state.id]["lens"],
                "state": state.state, "reason": state.reason, "findings": len(state.findings),
                # The findings this unit lost, carried into the route record so the report
                # can name them. A count alone would say a unit raised eleven where it
                # wrote fourteen, with nothing saying where the other three went.
                "rejected": list(state.rejected),
                "dropped": state.dropped,
            } for state in states],
            "candidates": [by_id[cand.id] for cand in candidates],
        })
        write_json(rundir / UNITS_FILE_NAME, {
            "stage": VERIFICATION_STAGE, "lanes": units_doc.get("lanes", list(LANES)), "units": listing,
        })
    except BaseException:
        # A failure part-way takes back every batch directory this route created and the
        # candidates file it claimed: left behind, they refuse the retry as a second route,
        # and the claim is why neither can be another route's. A directory that will not
        # come away is left on disk and decides nothing — the retry reclaims that name, and
        # refuses it by name if it holds anything this stage did not write.
        for unit_dir in created:
            shutil.rmtree(unit_dir, ignore_errors=True)
        _discard(candidates_path)
        raise


def _plural(n: int, noun: str, plural: str | None = None) -> str:
    """``plural`` is for a noun an `s` does not pluralize — `directory`/`directories`."""
    return f"{n} {noun if n == 1 else (plural or noun + 's')}"


def route_summary(states: Sequence[UnitState], candidates: Sequence[Candidate],
                  batches: Sequence[Batch], probe: Probe) -> str:
    counts = {state: sum(1 for s in states if s.state == state)
              for state in (UNIT_COMPLETE, UNIT_FAILED, UNIT_MISSING)}
    out = [
        f"{_plural(len(candidates), 'candidate')} from {_plural(len(states), 'reading unit')}: "
        f"{counts[UNIT_COMPLETE]} complete, {counts[UNIT_FAILED]} failed, {counts[UNIT_MISSING]} missing"
    ]
    for state in states:
        if state.state != UNIT_COMPLETE:
            out.append(f"  {state.id}  {state.state}: {state.reason}")
    if probe.state == UNIT_COMPLETE:
        out.append(f"capability probe: builds {probe.build['answer']}, tests run {probe.tests['answer']}")
    else:
        out.append(f"capability probe: {PROBE_UNKNOWN_WORD} ({probe.unit} {probe.state}: {probe.reason})")
    out.append(f"{_plural(len(batches), 'verification unit')}")
    for batch in batches:
        out.append(f"  {batch.id}  {_plural(len(batch.candidates), 'candidate')}  "
                   f"finder {batch.finder} -> lane {batch.lane}"
                   + ("  (coverage gaps)" if batch.asks == COVERAGE_ASKS else ""))
    return "\n".join(out) + "\n"


# --------------------------------------------------------------------------- #
# dispatch.json — the dispatcher's record of what ran each lane
# --------------------------------------------------------------------------- #
# The engine never spawns, so it cannot know which adapter ran a lane or under what
# permission; the dispatcher writes that down, and the report states it and nothing
# stronger. Parsed by name like the job: a record that says less than this, or whose rung
# the lanes contradict, is a refusal — a report that guessed the rung would claim an
# independence the run did not have.
# Two rungs and no third. One context as both finder and verifier breaks rule 3 — a
# finding goes to a unit that did not raise it — which every status on the page rests on,
# so a run that ends that way has no rung here: the driver refuses it, and this refuses a
# record naming one.
DISPATCH_FILE_NAME = "dispatch.json"
RUNG_TWO_RUNTIMES = "two-runtimes"
RUNG_ONE_RUNTIME = "one-runtime"
RUNGS = (RUNG_TWO_RUNTIMES, RUNG_ONE_RUNTIME)
DISPATCH_KEYS = ("rung", "lanes")
LANE_RECORD_KEYS = ("adapter", "permission")


class DispatchError(ReviewPanelError):
    """``dispatch.json`` is missing or does not say what ran each lane. Names the field."""


@dataclass(frozen=True)
class LaneRecord:
    """What ran one lane, in the dispatcher's words: the adapter and the permission mode
    it actually applied. Free text on purpose — the engine cannot verify a sandbox, so it
    renders the claim verbatim rather than a vocabulary it could not check."""

    adapter: str
    permission: str


@dataclass(frozen=True)
class DispatchRecord:
    rung: str
    lanes: dict[str, LaneRecord]


def parse_dispatch(obj: object) -> DispatchRecord:
    """Strictly parse the dispatcher's record; raise :class:`DispatchError` by name.

    One cross-check ties the rung to the lanes: ``two-runtimes`` claims two models, so both
    lanes recording one adapter contradicts it. ``one-runtime`` is checked no further, and a
    record naming it with two adapters is accepted rather than refused — this engine spawns
    nothing, so what it is given is all it knows about what ran, and refusing a record it
    cannot check would refuse true ones with false.
    A record naming any other rung is refused by name: a run where one context was both
    finder and verifier has no rung here, and nothing it produced is a report this renders.
    """
    if not isinstance(obj, dict):
        raise DispatchError(f"the dispatch record must be a JSON object (found {type(obj).__name__})")
    _check_keys(obj, "dispatch", frozenset(DISPATCH_KEYS), DISPATCH_KEYS, DispatchError)
    rung = obj["rung"]
    if not isinstance(rung, str) or rung not in RUNGS:
        raise DispatchError(f"field 'rung' must be one of: {', '.join(RUNGS)} (found {rung!r})")
    raw_lanes = obj["lanes"]
    if not isinstance(raw_lanes, dict):
        raise DispatchError(f"field 'lanes' must be a JSON object (found {type(raw_lanes).__name__})")
    _check_keys(raw_lanes, "lanes", frozenset(LANES), LANES, DispatchError)
    lanes: dict[str, LaneRecord] = {}
    for lane in LANES:
        raw, field = raw_lanes[lane], f"lanes.{lane}"
        if not isinstance(raw, dict):
            raise DispatchError(f"field '{field}' must be a JSON object (found {type(raw).__name__})")
        _check_keys(raw, field, frozenset(LANE_RECORD_KEYS), LANE_RECORD_KEYS, DispatchError)
        lanes[lane] = LaneRecord(
            adapter=_utf8(_parse_text(raw["adapter"], f"{field}.adapter", DispatchError),
                          f"{field}.adapter", DispatchError),
            permission=_utf8(_parse_text(raw["permission"], f"{field}.permission", DispatchError),
                             f"{field}.permission", DispatchError),
        )
    adapters = sorted({record.adapter for record in lanes.values()})
    if rung == RUNG_TWO_RUNTIMES and len(adapters) == 1:
        raise DispatchError(
            f"rung {RUNG_TWO_RUNTIMES!r} claims two models, but both lanes record the same "
            f"adapter ({adapters[0]!r}); record the rung that ran"
        )
    return DispatchRecord(rung=rung, lanes=lanes)


def load_dispatch(rundir: Path) -> DispatchRecord:
    path = rundir / DISPATCH_FILE_NAME
    try:
        obj = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise DispatchError(
            f"{path} is missing; the driver writes it from what the run actually did — "
            f"the rung, and per lane the adapter and permission mode that ran it — as its "
            f"last act before this stage, so a run directory without one never reached "
            f"report"
        ) from None
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        raise DispatchError(f"cannot read {path}: {exc}") from exc
    except RecursionError:
        # The decoder raises this, not JSONDecodeError, on a document nested thousands
        # deep; a refusal by name, like every other unreadable record, never a traceback.
        raise DispatchError(f"{path} nests deeper than the JSON decoder can parse") from None
    return parse_dispatch(obj)


# --------------------------------------------------------------------------- #
# cluster — one defect per group of candidates, by judgment, after verification
# --------------------------------------------------------------------------- #
# The merge happens here rather than before verification because two descriptions of one
# defect, each checked by the lane that did not raise it, are two independent checks; and
# because a verifier's rationale states the mechanism precisely where reader prose often
# does not, so identity is easier to judge afterwards. The unit groups and does nothing
# else: it cannot revise a severity, cannot call a candidate wrong, and cannot remove one.
CLUSTERER_KIND = "clusterer"
CLUSTERER_SCHEMA_NAME = "clusterer-schema.json"
CLUSTERED_STAGE = "clustered"
CLUSTER_UNIT_PREFIX = "cluster-"
# ``D`` and an unpadded number — ``D7``. A defect id is what a person says out loud, writes
# in a ticket and types into a search box, so it is short enough to hold in the head; zero
# padding buys column alignment nobody reads and costs exactly that. It is the defect's ONE
# identity: the structure, the prose and the page all spell it this way, because two
# spellings of one defect leave a reader unable to match the document against
# ``findings.json``. The unit that produced the grouping is ``cluster-<area>``, which shares
# no prefix with this, so neither can be read as the other.
CLUSTER_ID_PREFIX = "D"
CLUSTERER_RESULT_KEYS = ("clusters", "summary")
CLUSTER_KEYS = ("members", "consequence", "split_reason")
TO_GROUP_HEADING = "\n## Candidates to group\n"
NO_CHECK_LINE = "What the check found: it did not come back; judge this one from its own description."
UNGROUPED_NOTE = ("The area was not clustered: every candidate in it is reported on its own, "
                  "one candidate per cluster, so a defect that two readers both found is "
                  "shown twice rather than merged.")


@dataclass(frozen=True)
class ClusterUnit:
    """One clustering unit: one area's candidates of one kind, addressed to one lane.

    ``asks`` separates the two. A coverage gap and a defect at the same site are not the
    same thing to merge — one is a test to write and the other is code to fix — so they are
    grouped apart, and a clusterer is never handed a list mixing them.
    """

    id: str
    area: str
    lane: str
    candidates: tuple[str, ...]
    asks: str = DEFECT_ASKS


@dataclass(frozen=True)
class ClusterState:
    """What one clustering unit came back as: exactly one of complete, failed or missing,
    with the reason where it is not complete and the proved partition where it is."""

    id: str
    area: str
    state: str
    reason: str | None
    clusters: tuple[dict, ...]
    summary: str | None
    asks: str = DEFECT_ASKS


@dataclass(frozen=True)
class Cluster:
    """One defect, or one coverage gap. ``members`` are the candidates that describe it,
    ``grouped`` says whether a clustering unit put them together or the engine fell back to
    one candidate per cluster because that lane's unit could not be believed, and ``asks``
    says which of the two it is."""

    id: str
    area: str
    members: tuple[str, ...]
    consequence: str
    split_reason: str | None
    grouped: bool
    asks: str = DEFECT_ASKS


@dataclass(frozen=True)
class Ungrouped:
    """An area left unmerged, and why. Named in the report: a reader counting defects has
    to know where the count is inflated, and by what."""

    area: str
    unit: str
    state: str
    reason: str
    asks: str = DEFECT_ASKS


@dataclass(frozen=True)
class Clustering:
    """Every candidate in exactly one cluster, plus the areas that got there by degrading."""

    clusters: tuple[Cluster, ...]
    ungrouped: tuple[Ungrouped, ...]


@dataclass(frozen=True)
class ClusterCompanions:
    """What ``cluster`` ships beside the engine and every clustering payload is built
    from. Loaded before the first write, as ``plan``'s and ``route``'s are."""

    clusterer_brief: str
    clusterer_schema: dict


def load_cluster_companions() -> ClusterCompanions:
    return ClusterCompanions(
        clusterer_brief=load_brief("clusterer"),
        clusterer_schema=load_schema(CLUSTERER_SCHEMA_NAME),
    )


def plan_clusters(candidates: Sequence[dict]) -> tuple[ClusterUnit, ...]:
    """One unit per (area, kind) that raised something, areas in name order.

    A candidate's area owns its file and a file belongs to exactly one area, so two
    candidates in different areas cannot be the same site and no cluster can span areas.
    An area that raised nothing gets no unit: a payload with an empty candidate list asks
    a worker nothing.

    Kind separates the lists for the same reason routing does: two auditors can name one
    gap, so coverage findings need grouping too — but a gap merged with a defect at the
    same lines would be one entry that is both a test to write and code to fix, and the
    report has to put it in exactly one place.

    The lanes take the areas in turn. Neither lane is a stranger to an area's candidates —
    both lanes read every area — so the routing rule that keeps a finder away from its own
    finding has nothing to bite on here, and the reason to spread the work is that no one
    model's sense of what counts as the same defect then shapes the whole report.
    """
    keyed: dict[tuple[str, str], list[str]] = {}
    for cand in candidates:
        keyed.setdefault((cand["area"], asks_of(cand)), []).append(cand["id"])
    asked = {question: i for i, question in enumerate(ASKS)}
    return tuple(
        ClusterUnit(id=f"{CLUSTER_UNIT_PREFIX}{area}"
                       f"{COVERAGE_BATCH_SUFFIX if question == COVERAGE_ASKS else ''}",
                    area=area, lane=LANES[n % len(LANES)], asks=question,
                    candidates=tuple(keyed[(area, question)]))
        for n, (area, question) in enumerate(sorted(keyed, key=lambda k: (k[0], asked[k[1]])))
    )


def verified_rationales(candidates: Sequence[dict], holder: dict[str, dict],
                        states: Sequence[VerificationState]) -> dict[str, str | None]:
    """Every candidate to the rationale its verifier wrote, or ``None`` where no verifier
    wrote one — the unit failed, never landed, or its verdict for this candidate was one
    the engine could not read.

    Deliberately not :func:`resolve`'s rationale, which for an unanswered candidate reads
    "no verdict was received: verification unit <id> failed" — a sentence naming a unit,
    in the one payload this round keeps free of unit names.

    **A rejected verdict's rationale is the engine's, not a worker's**, and it is written
    to be read in the coverage section: it quotes the field that broke, the vocabulary that
    field is drawn from — ``blocker, major, minor, nit``, or the status names — and the
    candidate id. Passed on here it would carry all three into payloads that state in their
    own docstrings that they carry none of them, by a route no sweep for the words would
    find. ``None`` is the right answer because it is the true one: no check this stage can
    quote came back for that candidate, and both payloads already say exactly that.
    """
    by_unit = {state.id: state for state in states}
    out: dict[str, str | None] = {}
    for cand in candidates:
        state = by_unit[holder[cand["id"]]["id"]]
        if state.state != UNIT_COMPLETE:
            out[cand["id"]] = None
            continue
        verdict = next(v for v in state.verdicts if v.candidate == cand["id"])
        out[cand["id"]] = verdict.rationale if verdict.from_verifier else None
    return out


def render_clusterer_payload(brief: str, problem: str, candidates: Sequence[dict],
                             rationales: dict[str, str | None], schema: dict | None = None) -> str:
    """The whole of a clustering payload from exactly these inputs: the brief, the problem
    statement verbatim, and each candidate's id, location, failure, the distinct
    consequences and directions its raisers proposed, their proposed severities, and what
    the agent that checked it said about the mechanism.

    No unit id, no lane, no lens and no verdict status. Who raised a candidate is not
    evidence that two candidates are the same defect, and a status is a correctness
    judgment this stage is explicitly not making — shown one, a clusterer starts separating
    refuted claims from established ones, which is a grouping by something other than
    identity. ``rationales`` is a declared input with no default, for the reason the probe
    is one in the verification payload: an input that may be omitted is an input the byte
    reconstruction can silently miss.
    """
    out = [brief.rstrip("\n"), "\n", PROBLEM_HEADING, "\n", problem, "\n", TO_GROUP_HEADING]
    for cand in candidates:
        raised = cand["raised_by"]
        where = (f"line {cand['line_start']}" if cand["line_start"] == cand["line_end"]
                 else f"lines {cand['line_start']}-{cand['line_end']}")
        out += [f"\n### {cand['id']}\n", f"\nLocation: {cand['file']}, {where}\n",
                *_payload_field("Failure: ", cand["failure"])]
        for consequence in _distinct(r["consequence"] for r in raised):
            out += _payload_field("Consequence: ", consequence)
        for direction in _distinct(r["direction"] for r in raised):
            out += _payload_field("Direction: ", direction)
        out.append(f"Proposed severity: {'; '.join(_distinct(r['severity'] for r in raised))}\n")
        rationale = rationales[cand["id"]]
        out += [f"{NO_CHECK_LINE}\n"] if rationale is None else _payload_field("What the check found: ", rationale)
    out += _schema_section(schema)
    return "".join(out)


def _parse_cluster(raw: object, field: str, handed: frozenset[str],
                   placed: dict[str, str]) -> dict:
    """One cluster, with the two partition rules that can be decided from it alone: an id
    this unit was never handed, and an id already placed in another cluster.

    The consequence's LENGTH is not among them, and that is deliberate. The schema bounds
    it at 120 characters and the brief asks for twelve words, because the consequence is
    both the defect's heading and the widest column of the index — unbounded it ran to a
    144-character median and a 245-character maximum, and the index stopped being
    scannable. Refusing a reply here for a long one would cost the whole AREA its
    clustering: every candidate in it would go unmerged and be reported on its own, which
    is a far worse document than one heading that wraps. Both runtimes honour the bound as
    an instruction — measured per dispatch lane on a 151-defect run, 88 characters at the
    longest from one and 107 from the other, none over — so the schema is where it belongs
    and this parser stays permissive on purpose.
    """
    if not isinstance(raw, dict):
        raise ResultError(f"field '{field}' must be a JSON object (found {type(raw).__name__})")
    _check_keys(raw, field, frozenset(CLUSTER_KEYS), CLUSTER_KEYS, ResultError)
    if not isinstance(raw["members"], list) or not raw["members"]:
        raise ResultError(
            f"field '{field}.members' must be a non-empty list of candidate ids; an empty "
            f"cluster names no defect"
        )
    members: list[str] = []
    for index, member in enumerate(raw["members"]):
        name = _text(member, f"{field}.members[{index}]")
        if name not in handed:
            raise ResultError(
                f"field '{field}.members[{index}]' names {name!r}, which this unit was not "
                f"handed; a clustering unit groups the candidates in its own payload and no others"
            )
        if name in placed:
            raise ResultError(
                f"candidate {name} is in two clusters, '{placed[name]}' and '{field}'; every "
                f"candidate belongs to exactly one defect"
            )
        placed[name] = field
        members.append(name)
    return {
        "members": members,
        "consequence": _text(raw["consequence"], f"{field}.consequence"),
        "split_reason": _nullable_text(raw["split_reason"], f"{field}.split_reason"),
    }


def parse_clusterer_result(obj: object, unit_id: str, handed: Sequence[str]) -> dict:
    """Strictly parse a clustering result against the clusterer schema's vocabulary, and
    prove it is a partition of exactly the candidates the unit was handed.

    A dropped id, a repeated id or one the payload never listed fails the WHOLE unit. Part
    of a partition is not a smaller partition: believing the clusters that parsed and
    quietly leaving the rest of the candidates loose would give the report a grouping
    nobody produced. The engine's answer to a failed unit is no merging at all, which is
    the direction that keeps every finding visible.
    """
    try:
        if not isinstance(obj, dict):
            raise ResultError(f"result must be a JSON object (found {type(obj).__name__})")
        _check_keys(obj, "result", frozenset(CLUSTERER_RESULT_KEYS), CLUSTERER_RESULT_KEYS, ResultError)
        if not isinstance(obj["clusters"], list):
            raise ResultError(f"field 'clusters' must be a list (found {type(obj['clusters']).__name__})")
        wanted = frozenset(handed)
        placed: dict[str, str] = {}
        clusters = [_parse_cluster(raw, f"clusters[{index}]", wanted, placed)
                    for index, raw in enumerate(obj["clusters"])]
        missing = sorted(wanted - set(placed))
        if missing:
            raise ResultError(
                f"every candidate this unit was handed belongs to exactly one cluster, and "
                f"{len(missing)} of {len(wanted)} are in none: {', '.join(missing)}"
            )
        return {"clusters": clusters, "summary": _text(obj["summary"], "summary", empty_ok=True)}
    except ResultError as exc:
        raise ResultError(f"{unit_id}: {exc}") from None


def read_clustering_results(rundir: Path, units: Sequence[dict]) -> tuple[ClusterState, ...]:
    """Classify every clustering unit as exactly one of complete, failed or missing, by the
    same landing rule every other kind is read by. A unit holding both ``result.json`` and
    ``error.txt`` refuses the run here, as it does for a reader and a verifier: the probe is
    the one kind that survives it, because its answer is folded into payloads mid-pipeline
    and a stop there would strand a whole reading round.
    """
    states: list[ClusterState] = []
    for unit in units:
        asks = unit.get("asks", DEFECT_ASKS)
        state, payload = _read_unit_file(rundir, unit["id"])
        if state != UNIT_COMPLETE:
            states.append(ClusterState(unit["id"], unit["area"], state,
                                       _writable(str(payload)), (), None, asks))
            continue
        try:
            parsed = parse_clusterer_result(payload, unit["id"], unit["candidates"])
        except ResultError as exc:
            states.append(ClusterState(unit["id"], unit["area"], UNIT_FAILED,
                                       _writable(str(exc)), (), None, asks))
            continue
        states.append(ClusterState(unit["id"], unit["area"], UNIT_COMPLETE, None,
                                   tuple(parsed["clusters"]), parsed["summary"], asks))
    return tuple(states)


def check_clustering(candidates: Sequence[dict], units: Sequence[dict]) -> None:
    """Every clustering unit against the area it names: at most one per area, and exactly
    that area's candidates in it. Refused by name before any result is read, as
    :func:`check_routing` refuses the same shape one stage earlier.

    This is what makes the report's arithmetic true rather than merely stated. The
    partition check proves a reply against the ids its own unit was HANDED, so a listing
    that swapped two areas' candidate lists would pass every per-unit check and the total
    would still come out right, while the report showed one defect merged across two areas
    that cannot share a site. Two units for one area would likewise leave one of them
    deciding the area and the other recorded nowhere.

    A unit that is simply ABSENT for an area is not refused here: that is the degrade, and
    it costs the area its merging and nothing else. The asymmetry is the phase's whole
    premise — a missing answer is safe, a wrong one is not.
    """
    by_area: dict[tuple[str, str], list[str]] = {}
    for cand in candidates:
        by_area.setdefault((cand["area"], asks_of(cand)), []).append(cand["id"])
    seen: dict[tuple[str, str], str] = {}
    for unit in units:
        # The row's own shape first. A listing this engine did not write can carry a null
        # candidate list or no area at all, and reaching the comparison below with either
        # raises out of the run as a traceback instead of the refusal this promises.
        if (not isinstance(unit.get("area"), str) or not isinstance(unit.get("candidates"), list)
                or not all(isinstance(cid, str) for cid in unit["candidates"])):
            raise RunDirError(
                f"clustering unit {unit.get('id', '(unnamed)')!s} has no area or no list of "
                f"candidate ids; {UNITS_FILE_NAME} is not the engine's"
            )
        # The pair, not the area. Coverage gaps and defects from one area are grouped by
        # two units on purpose, so "one unit per area" would refuse the shape this stage
        # writes; what must still hold is one unit per area per question.
        lane = (unit["area"], unit.get("asks", DEFECT_ASKS))
        area, question = lane
        named = area + (f" ({question})" if question != DEFECT_ASKS else "")
        if lane in seen:
            raise RunDirError(
                f"clustering units {seen[lane]} and {unit['id']} both name area {named}; "
                f"cluster writes one per area per kind, so {UNITS_FILE_NAME} is not the "
                f"engine's"
            )
        seen[lane] = unit["id"]
        if lane not in by_area:
            raise RunDirError(
                f"clustering unit {unit['id']} names area {named}, which raised no candidate; "
                f"{UNITS_FILE_NAME} is not the engine's"
            )
        if sorted(unit["candidates"]) != sorted(by_area[lane]):
            raise RunDirError(
                f"clustering unit {unit['id']} is listed with candidates that are not "
                f"{named}'s ({len(unit['candidates'])} listed, {len(by_area[lane])} in it); "
                f"every candidate belongs to exactly one cluster of its own area, so "
                f"{UNITS_FILE_NAME} is not the engine's"
            )


def build_clusters(candidates: Sequence[dict], units: Sequence[dict],
                   states: Sequence[ClusterState]) -> Clustering:
    """Every candidate in exactly one cluster, whatever came back.

    Where an area's unit is complete its grouping is used; where it failed, never landed or
    is not listed at all, that area degrades to one candidate per cluster and says so. The
    degrade never drops a candidate, which is the whole reason it is the fallback: showing
    a defect twice costs a reader a minute, and merging two defects into one deletes the
    second with nothing left to show that it happened.

    Clusters are ordered by area and then by their lowest member, and ids are assigned from
    that order, so the run names a defect the same way however the clusterer listed them.
    That order is the FORMATION order and never the ranked one: a severity revision
    reorders the index, and an id assigned from the ranking would then name a different
    defect in the re-rendered document than it named in the one somebody cited.

    ``units`` has been through :func:`check_clustering`, so each listed unit holds exactly
    its own area's candidates and no area has two: that is what makes every candidate land
    in exactly one cluster here, whichever branch it takes.
    """
    by_area: dict[tuple[str, str], list[dict]] = {}
    for cand in candidates:
        by_area.setdefault((cand["area"], asks_of(cand)), []).append(cand)
    consequence_of = {cand["id"]: cand["raised_by"][0]["consequence"] for cand in candidates}
    by_unit_area = {(unit["area"], unit.get("asks", DEFECT_ASKS)): unit for unit in units}
    state_of = {(state.area, state.asks): state for state in states}
    asked = {question: i for i, question in enumerate(ASKS)}
    rows: list[tuple[str, tuple[str, ...], str, str | None, bool, str]] = []
    ungrouped: list[Ungrouped] = []
    for lane in sorted(by_area, key=lambda k: (k[0], asked[k[1]])):
        area, question = lane
        state = state_of.get(lane)
        if state is not None and state.state == UNIT_COMPLETE:
            for cluster in state.clusters:
                rows.append((area, tuple(sorted(cluster["members"])), cluster["consequence"],
                             cluster["split_reason"], True, question))
            continue
        unit = by_unit_area.get(lane)
        suffix = COVERAGE_BATCH_SUFFIX if question == COVERAGE_ASKS else ""
        ungrouped.append(Ungrouped(
            area=area,
            unit=unit["id"] if unit else f"{CLUSTER_UNIT_PREFIX}{area}{suffix}",
            state=state.state if state else UNIT_MISSING,
            reason=(state.reason if state and state.reason
                    else f"{UNITS_FILE_NAME} lists no clustering unit for {area}{suffix}"),
            asks=question,
        ))
        rows += [(area, (cand["id"],), consequence_of[cand["id"]], None, False, question)
                 for cand in by_area[lane]]
    # Ordered by area then lowest member, as before, with the question last: a defect and a
    # gap in one area keep their own formation order, and neither renumbers the other.
    rows.sort(key=lambda row: (row[0], asked[row[5]], row[1]))
    clusters = tuple(
        Cluster(id=f"{CLUSTER_ID_PREFIX}{n}", area=area, members=members,
                consequence=consequence, split_reason=split_reason, grouped=grouped,
                asks=question)
        for n, (area, members, consequence, split_reason, grouped, question)
        in enumerate(rows, 1)
    )
    return Clustering(clusters=clusters, ungrouped=tuple(ungrouped))


def write_clusters(rundir: Path, units_doc: dict, candidates: Sequence[dict],
                   rationales: dict[str, str | None], units: Sequence[ClusterUnit],
                   companions: ClusterCompanions, problem: str) -> None:
    """Write one ``units/<id>/`` per clustering unit, then ``units.json`` at the clustering
    stage listing everything that came before it and them. Every file lands by the
    exclusive-create-then-replace rule, and a failure part-way takes back what this stage
    wrote. ``units.json`` is rewritten last, so the stage marker moves only once the
    payloads are on disk.

    A unit directory an interrupted cluster left is reclaimed rather than refused: the
    caller holds cluster's claim and reached here with the marker still at the verification
    stage, so no other cluster is working and none has committed. The manifest is the units
    this call is about to write and nothing wider, and a directory holding anything cluster
    does not write is refused rather than deleted."""
    for unit in units:
        target = rundir / UNITS_DIR / unit.id
        if _not_a_unit_directory(target):
            raise RunDirError(
                f"{target} already exists and is a link or a file rather than a unit "
                f"directory this stage wrote; move it aside and cluster again"
            )
    _reclaim("cluster", rundir, (),
             tuple((rundir / UNITS_DIR / unit.id, UNIT_CONTENTS) for unit in units),
             (rundir / UNITS_FILE_NAME,))
    created: list[Path] = []
    try:
        by_id = {cand["id"]: cand for cand in candidates}
        listing = list(units_doc["units"])
        for unit in units:
            unit_dir = rundir / UNITS_DIR / unit.id
            try:
                unit_dir.mkdir(parents=True)
            except OSError as exc:
                raise InventoryError(f"cannot create {unit_dir}: {exc}") from exc
            created.append(unit_dir)
            text = render_clusterer_payload(companions.clusterer_brief, problem,
                                            [by_id[cid] for cid in unit.candidates], rationales,
                                            schema=companions.clusterer_schema)
            write_text(unit_dir / PAYLOAD_NAME, text)
            write_json(unit_dir / SCHEMA_NAME, companions.clusterer_schema)
            lines, size = measure_payload(text)
            listing.append({
                "id": unit.id, "kind": CLUSTERER_KIND, "area": unit.area, "lane": unit.lane,
                "lens": None, "candidates": list(unit.candidates), "asks": unit.asks,
                "payload": f"{UNITS_DIR}/{unit.id}/{PAYLOAD_NAME}",
                "schema": f"{UNITS_DIR}/{unit.id}/{SCHEMA_NAME}",
                "payload_lines": lines,
                "payload_bytes": size,
            })
        write_json(rundir / UNITS_FILE_NAME, {
            "stage": CLUSTERED_STAGE, "lanes": units_doc.get("lanes", list(LANES)), "units": listing,
        })
    except BaseException:
        # Best effort, deciding nothing: a directory that will not come away is left on
        # disk, and the retry reclaims that name or refuses it for what it holds.
        for unit_dir in created:
            shutil.rmtree(unit_dir, ignore_errors=True)
        raise


def cluster_summary(units: Sequence[ClusterUnit], states: Sequence[VerificationState]) -> str:
    out = [f"{_plural(len(states), 'verification unit')}: "
           f"{sum(1 for s in states if s.state == UNIT_COMPLETE)} complete, "
           f"{sum(1 for s in states if s.state == UNIT_FAILED)} failed, "
           f"{sum(1 for s in states if s.state == UNIT_MISSING)} missing"]
    out.append(f"{_plural(len(units), 'clustering unit')}")
    for unit in units:
        out.append(f"  {unit.id}  {_plural(len(unit.candidates), 'candidate')}  lane {unit.lane}")
    return "\n".join(out) + "\n"


# --------------------------------------------------------------------------- #
# synthesize — one reading of the defect list nothing else can compute
# --------------------------------------------------------------------------- #
SYNTHESIZER_KIND = "synthesizer"
SYNTHESIZER_SCHEMA_NAME = "synthesizer-schema.json"
SYNTHESIZED_STAGE = "synthesized"
SYNTHESIS_UNIT_PREFIX = "synth-"
# ONE unit for the whole round, which is how "exactly one unit names the tiers" is kept.
# The alternative — a unit per batch of defects, each naming its own vocabulary and the
# results merged — is the obvious design and the wrong one: two batches describing one
# theme in two phrasings give a reader fourteen headings where the run has seven themes,
# which is a longer list rather than a grouping. Splitting the round is the thing to do when
# a payload outgrows what a model can read, and it costs each unit a second copy of the whole
# index rather than costing any cross-reference: the payload separates that index from the
# defects this unit judges precisely so a split round can still cite a defect it was not
# handed.
SYNTHESIS_UNIT_ID = f"{SYNTHESIS_UNIT_PREFIX}{LANES[0]}"
SYNTHESIZER_RESULT_KEYS = ("tiers", "defects", "summary")
SYNTHESIS_KEYS = ("defect", "tier", "what_goes_wrong", "fix", "cross_references")
DEFECT_INDEX_HEADING = "\n## The defect index\n"
TO_JUDGE_HEADING = "\n## Your defects\n"


@dataclass(frozen=True)
class SynthesisUnit:
    """The synthesis round's one unit: every defect in the run, addressed to one lane."""

    id: str
    lane: str
    defects: tuple[str, ...]


@dataclass(frozen=True)
class SynthesisState:
    """What the synthesis unit came back as: exactly one of complete, failed or missing,
    with the reason where it is not complete and the proved assignment where it is.

    ``rejected`` is the entries inside a COMPLETE result the engine could not read, one
    message each. Those defects carry no judgment and fall back to being grouped by their
    status, and this is what keeps that from being silent — the same shape
    :func:`parse_verifier_result` uses for one unreadable verdict.
    """

    id: str
    state: str
    reason: str | None
    tiers: tuple[str, ...]
    assignments: dict[str, dict | None]
    rejected: tuple[str, ...]
    summary: str | None


@dataclass(frozen=True)
class Synthesis:
    """The round as the report reads it: every defect id to its judgment or ``None``, and
    the record of the round itself that ``findings.json`` carries."""

    assignments: dict[str, dict | None]
    record: dict


@dataclass(frozen=True)
class SynthesisCompanions:
    """What ``synthesize`` ships beside the engine and its payload is built from. Loaded
    before the first write, as ``plan``'s, ``route``'s and ``cluster``'s are."""

    synthesizer_brief: str
    synthesizer_schema: dict


def load_synthesis_companions() -> SynthesisCompanions:
    return SynthesisCompanions(
        synthesizer_brief=load_brief("synthesizer"),
        synthesizer_schema=load_schema(SYNTHESIZER_SCHEMA_NAME),
    )


def plan_synthesis(clusters: Sequence[Cluster]) -> tuple[SynthesisUnit, ...]:
    """One unit holding every defect, or none at all where the run found nothing.

    A run with no defect gets no unit for the reason an area that raised nothing gets no
    clustering unit: a payload with an empty list asks a worker nothing, and the answer it
    would have to return is the empty one.
    """
    if not clusters:
        return ()
    return (SynthesisUnit(id=SYNTHESIS_UNIT_ID, lane=LANES[0],
                          defects=tuple(cluster.id for cluster in clusters)),)


def synthesis_material(clustering: Clustering, candidates: Sequence[dict],
                       rationales: dict[str, str | None]) -> tuple[dict, ...]:
    """Every defect as the payload states it: its id, the heading the clustering round
    wrote, why it was kept apart where it was, and for each site inside it the failure as a
    reader stated it, the directions its raisers proposed, the fix sizes they estimated and
    what the agent that checked it said about the mechanism.

    **No status, no severity and no candidate id.** A tier is what these defects break, and
    a severity is how bad one is — shown the second, a synthesizer starts grouping by it
    and returns the ranking the index already has. The status is left out for the reason
    the clustering payload leaves it out: the account of a defect is written from what the
    readers and the checker said, and a judgment stage handed verdicts starts writing about
    the verdicts. It is also what keeps this round free of ``dispatch.json``, which the
    dispatcher writes once the LAST round has landed — and this is now that round.
    """
    by_id = {cand["id"]: cand for cand in candidates}
    out: list[dict] = []
    for cluster in clustering.clusters:
        members = sorted((by_id[cid] for cid in cluster.members),
                         key=lambda cand: (cand["file"], cand["line_start"], cand["line_end"],
                                           cand["id"]))
        out.append({
            "id": cluster.id,
            "consequence": cluster.consequence,
            "split_reason": cluster.split_reason,
            "members": [{
                "file": member["file"],
                "line_start": member["line_start"],
                "line_end": member["line_end"],
                "failure": member["failure"],
                "directions": _distinct(r["direction"] for r in member["raised_by"]),
                "fix_sizes": _distinct(r["fix_size"] for r in member["raised_by"]),
                "rationale": rationales[member["id"]],
            } for member in members],
        })
    return tuple(out)


def render_synthesizer_payload(brief: str, problem: str, index: Sequence[dict],
                               defects: Sequence[dict], schema: dict | None = None) -> str:
    """The whole of a synthesis payload from exactly these inputs: the brief, the problem
    statement verbatim, the whole defect index — every id and consequence, so any defect
    can be cited — and the full material for the defects this unit judges.

    ``index`` and ``defects`` are separate declared inputs even where one unit holds every
    defect and they cover the same list. What a payload may never carry is another unit's
    SYNTHESIS: a defect's id and consequence are settled facts this round is built on, as
    the clustering payload is built on the verifiers' rationales, while a tier, a narrative
    or a fix is the judgment this round exists to produce and no unit may be shown
    another's. Keeping the two inputs apart is what makes that property checkable by
    rebuilding the bytes.
    """
    out = [brief.rstrip("\n"), "\n", PROBLEM_HEADING, "\n", problem, "\n",
           DEFECT_INDEX_HEADING, "\n"]
    for entry in index:
        out += _payload_field(f"- {entry['id']}: ", entry["consequence"])
    out.append(TO_JUDGE_HEADING)
    for defect in defects:
        out += [f"\n### {defect['id']}\n", "\n",
                *_payload_field("Consequence: ", defect["consequence"])]
        if defect["split_reason"] is not None:
            out += _payload_field("Kept apart from a defect at the same site because: ",
                                  defect["split_reason"])
        for member in defect["members"]:
            where = (f"line {member['line_start']}"
                     if member["line_start"] == member["line_end"]
                     else f"lines {member['line_start']}-{member['line_end']}")
            out.append(f"\nAt {member['file']}, {where}:\n")
            out += _payload_field("- Failure: ", member["failure"])
            for direction in member["directions"]:
                out += _payload_field("- Direction: ", direction)
            out.append(f"- Proposed fix size: {'; '.join(member['fix_sizes'])}\n")
            out += ([f"- {NO_CHECK_LINE}\n"] if member["rationale"] is None
                    else _payload_field("- What the check found: ", member["rationale"]))
    out += _schema_section(schema)
    return "".join(out)


def _parse_tiers(raw: object) -> tuple[str, ...]:
    """The run's whole vocabulary, named once. Empty is refused and so is a repeat: a reply
    with no tiers placed every defect under a name it never declared, and one naming a tier
    twice has already produced the two-phrasings-of-one-theme the round exists to prevent.
    Both fail the unit rather than one defect, because neither is about a defect."""
    if not isinstance(raw, list):
        raise ResultError(f"field 'tiers' must be a list (found {type(raw).__name__})")
    if not raw:
        raise ResultError(
            "field 'tiers' is empty; the run's tier names are what every assignment below "
            "chooses from, so an empty list assigns nothing"
        )
    tiers: list[str] = []
    for index, tier in enumerate(raw):
        name = _text(tier, f"tiers[{index}]")
        if name in tiers:
            raise ResultError(
                f"field 'tiers[{index}]' names {name!r} twice; one theme spelled two ways is "
                f"the grouping this round exists to prevent"
            )
        tiers.append(name)
    return tuple(tiers)


def _parse_synthesis(raw: object, field: str, tiers: Sequence[str]) -> dict:
    """One defect's judgment, checked against the run's own vocabulary.

    A tier outside ``tiers`` is refused here rather than accepted and rendered: one
    vocabulary per run is the property, and a name the unit invented after declaring its
    list is a name no other defect can be filed beside. A cross-reference to the defect
    itself is refused too — the engine's later check asks whether the two defects share a
    file or a line range, which a defect trivially does with itself, so nothing downstream
    would catch it and the page would carry a defect pointing at its own heading.
    """
    if not isinstance(raw, dict):
        raise ResultError(f"field '{field}' must be a JSON object (found {type(raw).__name__})")
    _check_keys(raw, field, frozenset(SYNTHESIS_KEYS), SYNTHESIS_KEYS, ResultError)
    defect = _text(raw["defect"], f"{field}.defect")
    tier = _text(raw["tier"], f"{field}.tier")
    if tier not in tiers:
        raise ResultError(
            f"field '{field}.tier' names {tier!r}, which is not one of this run's tiers "
            f"({', '.join(repr(name) for name in tiers)}); every defect is filed under a "
            f"name the reply declared"
        )
    if not isinstance(raw["cross_references"], list):
        raise ResultError(
            f"field '{field}.cross_references' must be a list "
            f"(found {type(raw['cross_references']).__name__})"
        )
    references: list[str] = []
    for index, reference in enumerate(raw["cross_references"]):
        name = _text(reference, f"{field}.cross_references[{index}]")
        if name == defect:
            raise ResultError(
                f"field '{field}.cross_references[{index}]' names {name!r}, which is this "
                f"defect; a defect is not read beside itself"
            )
        if name in references:
            raise ResultError(
                f"field '{field}.cross_references[{index}]' names {name!r} twice"
            )
        references.append(name)
    return {
        "defect": defect,
        "tier": tier,
        "what_goes_wrong": _text(raw["what_goes_wrong"], f"{field}.what_goes_wrong"),
        "fix": _text(raw["fix"], f"{field}.fix"),
        "cross_references": references,
    }


def _defect_of(raw: object) -> str | None:
    """The defect an entry claims to answer for, or ``None`` where it does not say."""
    if isinstance(raw, dict) and isinstance(raw.get("defect"), str):
        return raw["defect"]
    return None


def parse_synthesizer_result(obj: object, unit_id: str, handed: Sequence[str]) -> dict:
    """Strictly parse the synthesis result, and prove it answers for exactly the defects the
    unit was handed.

    **The partition is proved before the judgment is believed**, exactly as the clustering
    round is: a dropped id, a repeated id or one the payload never listed fails the WHOLE
    unit, and the run is then grouped by status as it was before this round existed. Part
    of an assignment is not a smaller assignment — believing the entries that parsed would
    give the report a grouping nobody produced, over a defect list nobody agreed to.

    **An entry the engine cannot read costs its own defect and no other.** The rules are
    unchanged and none is relaxed; what changes is the blast radius, for the reason
    :func:`parse_verifier_result` gives about one unreadable verdict. The round is one
    dispatch of many independent judgments, and an entry with an empty narrative or a tier
    the reply never declared says nothing about the twenty beside it. That defect keeps no
    tier and no prose, falls back to its status group, and the rejection is recorded by
    name so nothing is quietly thrown away.

    What still fails the unit is a result whose SHAPE cannot be trusted: not an object, an
    unknown top-level key, a tier list that is empty or repeats itself, an entry that is not
    an object or does not say which defect it answers for, and any breach of the partition.
    In each of those the engine cannot say which judgment belongs to which defect, and
    attributing them would be guesswork.
    """
    try:
        if not isinstance(obj, dict):
            raise ResultError(f"result must be a JSON object (found {type(obj).__name__})")
        _check_keys(obj, "result", frozenset(SYNTHESIZER_RESULT_KEYS),
                    SYNTHESIZER_RESULT_KEYS, ResultError)
        tiers = _parse_tiers(obj["tiers"])
        if not isinstance(obj["defects"], list):
            raise ResultError(f"field 'defects' must be a list (found {type(obj['defects']).__name__})")
        wanted = frozenset(handed)
        assignments: dict[str, dict | None] = {}
        rejected: list[str] = []
        for index, raw in enumerate(obj["defects"]):
            field = f"defects[{index}]"
            named = _defect_of(raw)
            if named is not None and named in assignments:
                raise ResultError(
                    f"defect {named} is answered twice, and '{field}' is the second; every "
                    f"defect gets exactly one entry"
                )
            if named is None:
                # Not attributable. Parsed so the refusal names the field that is actually
                # wrong — a missing key, a defect id that is not text, an entry that is not
                # an object — rather than a sentence about attribution. Every one of those
                # raises; the line after it is the guard against a shape that somehow did
                # not, since an entry the engine cannot attribute must never pass silently.
                _parse_synthesis(raw, field, tiers)
                raise ResultError(f"field '{field}' does not say which defect it answers for")
            if named not in wanted:
                raise ResultError(
                    f"field '{field}' names {named!r}, which this unit was not handed; a "
                    f"synthesis unit judges the defects in its own payload and no others"
                )
            try:
                assignments[named] = _parse_synthesis(raw, field, tiers)
            except ResultError as exc:
                rejected.append(_writable(f"{named}: {exc}"))
                assignments[named] = None
        missing = sorted(wanted - set(assignments), key=_id_rank)
        if missing:
            raise ResultError(
                f"every defect this unit was handed gets exactly one entry, and "
                f"{len(missing)} of {len(wanted)} have none: {', '.join(missing)}"
            )
        return {"tiers": list(tiers), "assignments": assignments, "rejected": rejected,
                "summary": _text(obj["summary"], "summary", empty_ok=True)}
    except ResultError as exc:
        raise ResultError(f"{unit_id}: {exc}") from None


def read_synthesis_result(rundir: Path, units: Sequence[dict]) -> SynthesisState | None:
    """The synthesis unit as exactly one of complete, failed or missing, or ``None`` where
    the run has no such unit at all. A unit holding both ``result.json`` and ``error.txt``
    refuses the run here, as it does for every kind but the probe."""
    if not units:
        return None
    unit = units[0]
    state, payload = _read_unit_file(rundir, unit["id"])
    if state != UNIT_COMPLETE:
        return SynthesisState(unit["id"], state, _writable(str(payload)), (), {}, (), None)
    try:
        parsed = parse_synthesizer_result(payload, unit["id"], unit["defects"])
    except ResultError as exc:
        return SynthesisState(unit["id"], UNIT_FAILED, _writable(str(exc)), (), {}, (), None)
    return SynthesisState(unit["id"], UNIT_COMPLETE, None, tuple(parsed["tiers"]),
                          parsed["assignments"], tuple(parsed["rejected"]), parsed["summary"])


def check_synthesis(clusters: Sequence[Cluster], units: Sequence[dict]) -> None:
    """The synthesis listing against the run's own defects: at most one unit, holding
    exactly the defect ids this run has. Refused by name before any result is read, as
    :func:`check_clustering` refuses the same shape one stage earlier.

    The partition check proves a reply against the ids its own unit was HANDED, so a
    listing naming a different set of defects would pass it and the report would carry a
    grouping over a defect list this run never produced. A unit that is simply ABSENT is
    not refused: that is the round not having been run, which costs the report its tiers
    and nothing else.
    """
    if not units:
        return
    if len(units) > 1:
        raise RunDirError(
            f"synthesis units {units[0].get('id', '(unnamed)')!s} and "
            f"{units[1].get('id', '(unnamed)')!s} both name the synthesis round; synthesize "
            f"writes one unit, so {UNITS_FILE_NAME} is not the engine's"
        )
    unit = units[0]
    # The row's own shape first. A listing this engine did not write can carry a null
    # defect list, and reaching the comparison below with one raises out of the run as a
    # traceback instead of the refusal this promises.
    if (not isinstance(unit.get("defects"), list)
            or not all(isinstance(did, str) for did in unit["defects"])):
        raise RunDirError(
            f"synthesis unit {unit.get('id', '(unnamed)')!s} has no list of defect ids; "
            f"{UNITS_FILE_NAME} is not the engine's"
        )
    ids = [cluster.id for cluster in clusters]
    if sorted(unit["defects"]) != sorted(ids):
        raise RunDirError(
            f"synthesis unit {unit['id']} is listed with defects that are not this run's "
            f"defects ({len(unit['defects'])} listed, {len(ids)} in the run); the round is "
            f"handed every defect exactly once, so {UNITS_FILE_NAME} is not the engine's"
        )


def build_synthesis(units: Sequence[dict], state: SynthesisState | None) -> Synthesis | None:
    """The round as the report reads it, or ``None`` where the run has no synthesis round.

    A round that failed, never landed or returned something the engine could not believe
    leaves every defect unjudged and says which unit it was and what state it came back in.
    **Nothing is dropped**: a judgment stage cannot remove a defect from the run, so the
    degrade costs the report its tiers and its narrative and costs the run nothing — the
    same asymmetry the clustering round is built on, one stage later.
    """
    if not units:
        return None
    unit = units[0]
    if state is None or state.state != UNIT_COMPLETE:
        return Synthesis(assignments={}, record={
            "unit": unit["id"],
            "state": state.state if state is not None else UNIT_MISSING,
            "reason": state.reason if state is not None else None,
            "tiers": [], "rejected": [], "summary": None,
        })
    return Synthesis(assignments=dict(state.assignments), record={
        "unit": unit["id"],
        "state": state.state,
        "reason": state.reason,
        "tiers": list(state.tiers),
        "rejected": list(state.rejected),
        "summary": state.summary,
    })


CROSS_REF_ABSENT = "no defect in this run carries that id"
CROSS_REF_APART = "the two defects touch no file in common"


def resolve_cross_references(defect: str, references: Sequence[str],
                             files_of: dict[str, frozenset[str]],
                             tier_of: dict[str, str | None] | None = None,
                             ) -> tuple[list[str], list[dict]]:
    """A defect's cross-references split into the ones that survive checking and the ones
    dropped, each dropped one with the reason it was dropped.

    Two checks, and they are the whole of what can be checked here. **The id must name a
    defect in this run** — a reference to something that does not exist resolves to
    nothing, and rendered it would send a reader looking for a heading that is not on the
    page. **The two defects must touch a file in common, or sit under one tier**: a
    reference sharing neither rests on nothing the engine can see, and the brief already
    tells the writer that sitting in one file is not by itself a connection.

    The tier half is what the file half was standing in for. A same-file rule alone refused
    22 links in one report -- among them a reverse endpoint with no idempotency key beside
    a lost-response refund being retried, and a three-file refund chain, which are the
    cross-file connections a reader most wants. The synthesis round assigns tiers to group
    chains exactly like those, so two defects it put under one theme are connected by that
    round's own judgment. An ABSENT tier is not a shared one: a run with no synthesis round
    has none at all, and reading absence as agreement would turn this check into "keep
    everything" on precisely the runs with the least to go on.

    ``plan.md`` states the second as "the same file, or an overlapping line range". Those
    are one test rather than two: two ranges in different files cannot overlap, so every
    pair an overlap would admit shares a file already, and testing the ranges as well would
    only ADD a way to accept a pair in two different files — which is the thing the check
    exists to refuse. The file test is therefore the whole disjunction, and writing it as
    two clauses would suggest a reference could pass on ranges alone.

    What this cannot check is whether the two defects are related **in the way the prose
    says**. That would need a stage that read both, which is a fifth round and is not in
    this plan. A surviving reference is therefore checked, not verified, and the reference
    the round is trusted for is the one it does not make.
    """
    mine = files_of.get(defect, frozenset())
    tiers = tier_of or {}
    my_tier = tiers.get(defect)
    kept: list[str] = []
    dropped: list[dict] = []
    for reference in references:
        if reference not in files_of:
            dropped.append({"defect": reference, "reason": CROSS_REF_ABSENT})
        elif mine & files_of[reference]:
            kept.append(reference)
        elif my_tier is not None and tiers.get(reference) == my_tier:
            kept.append(reference)
        else:
            dropped.append({"defect": reference, "reason": CROSS_REF_APART})
    return kept, dropped


def write_synthesis(rundir: Path, units_doc: dict, units: Sequence[SynthesisUnit],
                    material: Sequence[dict], companions: SynthesisCompanions,
                    problem: str) -> None:
    """Write ``units/<id>/`` for the synthesis unit, then ``units.json`` at the synthesis
    stage listing everything that came before it and it. Every file lands by the
    exclusive-create-then-replace rule, and a failure part-way takes back what this stage
    wrote. ``units.json`` is rewritten last, so the stage marker moves only once the
    payload is on disk.

    A unit directory an interrupted synthesize left is reclaimed rather than refused: the
    caller holds synthesize's claim and reached here with the marker still at the clustering
    stage, so no other synthesize is working and none has committed. The manifest is the
    unit this call is about to write and nothing wider, and a directory holding anything
    synthesize does not write is refused rather than deleted."""
    for unit in units:
        target = rundir / UNITS_DIR / unit.id
        if _not_a_unit_directory(target):
            raise RunDirError(
                f"{target} already exists and is a link or a file rather than a unit "
                f"directory this stage wrote; move it aside and synthesize again"
            )
    _reclaim("synthesize", rundir, (),
             tuple((rundir / UNITS_DIR / unit.id, UNIT_CONTENTS) for unit in units),
             (rundir / UNITS_FILE_NAME,))
    created: list[Path] = []
    try:
        by_id = {defect["id"]: defect for defect in material}
        index = [{"id": defect["id"], "consequence": defect["consequence"]}
                 for defect in material]
        listing = list(units_doc["units"])
        for unit in units:
            unit_dir = rundir / UNITS_DIR / unit.id
            try:
                unit_dir.mkdir(parents=True)
            except OSError as exc:
                raise InventoryError(f"cannot create {unit_dir}: {exc}") from exc
            created.append(unit_dir)
            text = render_synthesizer_payload(
                companions.synthesizer_brief, problem, index,
                [by_id[did] for did in unit.defects],
                schema=companions.synthesizer_schema)
            write_text(unit_dir / PAYLOAD_NAME, text)
            write_json(unit_dir / SCHEMA_NAME, companions.synthesizer_schema)
            lines, size = measure_payload(text)
            listing.append({
                "id": unit.id, "kind": SYNTHESIZER_KIND, "area": None, "lane": unit.lane,
                "lens": None, "defects": list(unit.defects),
                "payload": f"{UNITS_DIR}/{unit.id}/{PAYLOAD_NAME}",
                "schema": f"{UNITS_DIR}/{unit.id}/{SCHEMA_NAME}",
                "payload_lines": lines,
                "payload_bytes": size,
            })
        write_json(rundir / UNITS_FILE_NAME, {
            "stage": SYNTHESIZED_STAGE, "lanes": units_doc.get("lanes", list(LANES)),
            "units": listing,
        })
    except BaseException:
        # Best effort, deciding nothing: a directory that will not come away is left on
        # disk, and the retry reclaims that name or refuses it for what it holds.
        for unit_dir in created:
            shutil.rmtree(unit_dir, ignore_errors=True)
        raise


def synthesis_summary(units: Sequence[SynthesisUnit], clusters: Sequence[Cluster]) -> str:
    out = [f"{_plural(len(clusters), 'defect')} to judge"]
    for unit in units:
        out.append(f"  {unit.id}  {_plural(len(unit.defects), 'defect')}  lane {unit.lane}")
    if not units:
        out.append("  no synthesis unit: the run found nothing to judge")
    return "\n".join(out) + "\n"


# --------------------------------------------------------------------------- #
# report — every candidate's one status, and what nobody read
# --------------------------------------------------------------------------- #
REPORT_NAME = "report.md"
FINDINGS_NAME = "findings.json"
# Where the stamp lives between the moment it is read and the moment it is published, and
# **what says this run has been reported**. Its own file, and deliberately NOT one of
# report's outputs: a report interrupted part-way leaves those to be reclaimed, and a stamp
# reclaimed with them would send the rebuilt report back to the clock — a recovered report a
# minute later than the one it replaces.
#
# The stamp is written before the first of the three files and nothing is written after the
# last, so the three states are read off the files and nothing else. All three present with
# the stamp beside them is a report that finished. Some of them present with the stamp is a
# publication that was interrupted, which the next report reclaims and redoes. Any of them
# present with NO stamp is a report published under rules that kept no such record, which is
# somebody's and needs ``--rerender`` to replace.
#
# It is report's own file for a second reason: ``units.json`` is written by four stages, so
# a completion recorded there is a completion another stage can overwrite — a synthesis that
# read the run before the report ran commits its own marker afterwards and the published
# report loses the only thing protecting it. The stamp is what no other stage writes, so it
# is what no other stage can undo, and it is why the marker below is a record of where the
# run got to rather than the thing that protects the report.
REPORT_STAMP_NAME = "report-stamp.json"

# The terminal marker. ``report`` advances ``units.json`` to it as its LAST write, after all
# three files are published and the publication lock is released, so a driver reading the
# marker alone knows a finished run from one that died before its last step.
#
# **It is safe because one owner holds the run across every stage.** Written by a stage
# invocation that owns nothing but itself, a terminal marker in the file four stages commit
# with is one any of them can undo: a synthesis that read ``clustered`` before the report ran
# commits ``synthesized`` afterwards and the terminal marker is simply gone. That is why the
# marker arrives with the driver (``review_panel_run.py``), which holds one lock for the
# whole run, and why the stamp above — not this — is what refuses to overwrite a published
# report. A hand-run report on a directory a driver owns is what the driver's lock and the
# skill text both forbid.
REPORTED_STAGE = "reported"


# Every clock this engine writes, in one place, because two of them appear side by side in the
# subtitle and a reader compares them: the render stamp and the moment the snapshot was taken.
# Spelled to the SECOND — two runs of one tree minutes apart are told apart by the minute, but
# two renderings of one run are not, and a reader asking which of two reports is the later one
# was given two identical-looking stamps.
CLOCK_FORMAT = "%Y-%m-%d %H:%M:%S UTC"
# The same shape as a pattern. Public, because the engine reads its own stamp back with it
# and every OTHER reader of a clock this engine wrote — a test, a later stage — has to derive
# from the same place. Re-spelled by hand in five, adding seconds to the format left all five
# matching nothing, and a regex that matches nothing fails loudly only where it is asserted.
CLOCK_PATTERN = r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} UTC"


def clock_now() -> str:
    """The current moment, in the one format this engine writes a clock in."""
    return datetime.now(timezone.utc).strftime(CLOCK_FORMAT)


def _committed_clock(raw: str) -> str:
    """A commit's own date and time, in the format every other clock on the page uses.

    Stored as strict ISO-8601 carrying the COMMITTER's offset, because that is what git hands
    over and what sorts. Rendered in UTC, because the subtitle puts it beside a stamp that is
    in UTC and a reader comparing the two should not have to do the arithmetic to find out
    whether the tree is older than the run.

    A value this cannot read renders as its date alone rather than as nothing: a record from
    an older run, or from a git that answered in another shape, still says which day.
    """
    try:
        return datetime.fromisoformat(raw).astimezone(timezone.utc).strftime(CLOCK_FORMAT)
    except (TypeError, ValueError):
        return raw[:10] if isinstance(raw, str) else ""


# The subtitle's first field, exactly as :func:`render_report` writes it. Engine to engine:
# this file writes the line and this file reads it back, so it is a private round trip and
# not a parse of somebody else's format.
_GENERATED_LINE = re.compile(rf"^Generated ({CLOCK_PATTERN})\s*(?:·|$)", re.MULTILINE)


def _previous_stamp(report: Path) -> str | None:
    """When the report being replaced said it was generated, or ``None``.

    The fallback, for a run directory that carries a report and no stamp file beside it. It
    is read from the REPORT and never from ``findings.json``, which is the structure and
    carries no clock and no path: two runs over one tree write it byte for byte the same,
    and the run directory holds exactly one clock, in ``inventory.json``. A stamp stored
    there to make ``--rerender`` reproducible broke both of those at once.

    It cannot be the only answer, because a recovery reclaims the report: an interrupted
    report is rebuilt from a directory whose ``report.md`` is gone or is half of one, and
    there is nothing there to read the stamp off.

    Every failure answers ``None`` -- no file, unreadable, or a subtitle that does not
    match -- because the caller then reads the clock, which is what a first render does
    anyway. A re-render over a report somebody edited should still produce one.
    """
    try:
        found = _GENERATED_LINE.search(report.read_text(encoding="utf-8"))
    except OSError:
        return None
    return found.group(1) if found else None


# The stamp exactly as `render_report` writes it, so what is read back is a stamp and not
# whatever else ended up in that file. Built from :data:`CLOCK_PATTERN` rather than spelled
# again: hand-written, it went on matching the old shape after the format gained seconds, and
# every stamp then read back as "not a stamp" — which is indistinguishable from no stamp at
# all, so a finished report looked unpublished and the next run rewrote it.
_STAMP_FORM = re.compile(rf"\A{CLOCK_PATTERN}\Z", re.ASCII)


def read_report_stamp(rundir: Path) -> str | None:
    """When this run's report says it was generated, from the file that outlives a recovery.

    ``findings.json`` cannot hold it and neither can ``report.md``: both are report's own
    outputs, so a report interrupted between them is rebuilt from a run directory that has
    lost whichever of them it was going to read. The stamp is written before any of the
    three is published and read back by every later rendering of the same run, which is what
    makes a recovered report byte-identical to the one that was lost rather than a minute
    later.

    ``units.json`` cannot hold it either: every stage rewrites that file from the three
    fields it carries, so a fourth would survive only until the next stage.

    Every failure answers ``None`` — no file, unreadable, or a value that is not a stamp —
    because the caller then reads the clock, which is what a first rendering does anyway.
    """
    try:
        raw = json.loads((rundir / REPORT_STAMP_NAME).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError):
        return None
    if not isinstance(raw, dict):
        return None
    stamp = raw.get("generated")
    return stamp if isinstance(stamp, str) and _STAMP_FORM.match(stamp) else None


def write_report_stamp(rundir: Path, generated: str) -> None:
    """Persist the stamp before anything is published, so every later rendering of this run
    — a re-render, or a report rebuilt after an interrupted one — states the same time."""
    write_json(rundir / REPORT_STAMP_NAME, {"generated": generated})


def reported(rundir: Path) -> bool:
    """Whether ``units.json`` carries the terminal marker.

    A listing that is **absent** is not a reported run, because the caller's next move —
    publish, or refuse — has to be decided from what is there rather than from an exception.
    A listing that is **there and will not be read** is a different thing and is refused:
    answered False it clears the one guard that keeps a second report off a published one,
    over prose somebody may have annotated.
    """
    return _units_stage(rundir) == REPORTED_STAGE


def _units_stage(rundir: Path) -> str | None:
    path = rundir / UNITS_FILE_NAME
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise RunDirError(f"cannot read {path}: {_os_fault(exc)}") from exc
    except (UnicodeDecodeError, ValueError, RecursionError):
        # Decoded, not read: the bytes came back and are not a listing. That is a fact about
        # the file rather than an unanswered question, and it is not a reported run.
        return None
    return doc.get("stage") if isinstance(doc, dict) else None


REPORTABLE_STAGES = (CLUSTERED_STAGE, SYNTHESIZED_STAGE, REPORTED_STAGE)


def advance_to_reported(rundir: Path) -> None:
    """Move the marker to :data:`REPORTED_STAGE`, and change nothing else.

    **The listing written back is the one on disk at this moment**, not the one the report
    read when it started. A redo that landed while the report was preparing has already
    replaced that file; writing back the copy report read would restore unit records the
    redo deleted, which is a stale listing committed by a stage that never owned it.

    **And a marker that moved somewhere report cannot report from is a refusal, not
    something to overwrite.** A redo resets the run to an earlier stage and deletes the
    published outputs; a report that finished rendering before that happened is holding a
    rendering of data the run no longer has, so marking it reported would publish one run's
    structure over another's reset. The caller's recovery then takes the rendering back,
    which is what leaves the redo's state whole. Under the driver this cannot arise at all —
    one owner holds the run across every stage — and it is cheap enough to refuse here
    anyway: an engine stage run outside the driver takes no such lock.

    Idempotent, so a run already reported is left exactly as it is: this is the last write
    of the stage and it is also what a recovery re-runs on its own.
    """
    doc = _read_run_json(rundir, UNITS_FILE_NAME)
    if not isinstance(doc, dict) or not isinstance(doc.get("units"), list):
        raise RunDirError(f"{rundir / UNITS_FILE_NAME} is not a units listing")
    if doc.get("stage") == REPORTED_STAGE:
        return
    if doc.get("stage") not in REPORTABLE_STAGES:
        raise RunDirError(
            f"{rundir / UNITS_FILE_NAME} moved to stage {doc.get('stage')!r} while this "
            f"report was preparing, which is not a stage a report is written from; the "
            f"rendering is taken back rather than recorded over a run that was reset"
        )
    write_json(rundir / UNITS_FILE_NAME, {**doc, "stage": REPORTED_STAGE})


def published_report(rundir: Path, outputs: Sequence[Path]) -> bool:
    """Whether this run directory already holds a report that finished.

    All three outputs, and the stamp that was written before the first of them. Nothing is
    written after the last, so that combination cannot be a publication caught part-way:
    a report interrupted between two of the three leaves the stamp and less than three
    files, and the next report reclaims and redoes it.

    The stamp is what separates this from a report published under rules that kept no
    record of it, which is somebody's and is refused rather than replaced. Asked of the
    files rather than of any marker, because report's own artifacts are the only state no
    other stage writes.
    """
    # Each output is asked about with a refusal available. Answered False for a path the
    # host would not describe, this says a finished report is not there and the stage
    # rewrites it — over prose somebody may have annotated.
    return (read_report_stamp(rundir) is not None
            and all(_lstat_or_absent(path, "the report output", RunDirError) is not None
                    for path in outputs))


# The lock report publishes under, spelled from the stage's name so the file `_claim_stage`
# creates and the file a redo refuses over cannot drift apart. A file of its own, because
# every other file this stage writes is one a re-render must be able to move aside.
REPORT_LOCK_NAME = f"report{STAGE_LOCK_SUFFIX}"
# The index column that holds a defect id, named once for the writer and once for the
# conversion that reads it back. The page turns a cell in THIS column into a link to its
# defect and leaves every other cell alone: a short file name is an arbitrary string, and a
# file called `D10` in the by-file table is not defect D10.
DEFECT_COLUMN = "Defect"
# The report's top-level sections, by name. The NUMBER in front of each is not here: which
# sections a report has depends on the run — a run that corroborated nothing writes no
# corroborated section at all — so a number spelled into a heading would be wrong on the
# first report that skipped one. :class:`_Sections` counts them as they are written.
# What the block quote under the title is. Named for the command the reader ran, because
# the statement is the thing they typed and nothing the panel decided.
PROMPT_LEAD_IN = "review-panel was given this prompt:"

SECTION_DESCRIPTION = "Report description"
# The job the run answers: what was asked for, and what it takes to ask for it again. In the
# APPENDIX, beside the record of what ran it, by the same rule as the path legend — nothing in
# it is needed to fix a defect, and on a subject-mode job it runs to a screen. What a reader
# needs where the statement is, they get from the pointer under it, which carries the shape of
# the run in one sentence and names this section: a report that carried the statement alone
# said nowhere that the statement was one field of a job with several, and a reader who had not
# written the job could not tell how the tree was divided or what each reader read for.
SUBSECTION_THE_JOB = "The job"
# The shape of the run, at the top where a reader meets it before the lists. Only ever a
# table of COUNTS: the three views below it are the lists a reader works down, and a fourth
# would be a second order of one piece of work.
SUBSECTION_BY_TIER = "Defects by tier"
# The limits of the method, stated to the person who has to act on the page. It is written
# here rather than left to whoever runs the panel, because the reader of a report is usually
# not that person and is usually not in the room when the run finishes.
SUBSECTION_LIMITS = "What this report does not tell you"
# The synthesis round's own paragraph about the run, which the round writes for this report.
SUBSECTION_OVERVIEW = "The judgment round's overview"
# What the operator wrote in `report-notes.json`: its own section, directly after the
# description, because the answers to the job's questions are what an owner asked for.
SECTION_OPERATOR = "The operator's notes"
SUBSECTION_ANSWERS = "Answers to the owner's questions"
SUBSECTION_CORRECTIONS = "The operator's corrections"
SUBSECTION_CAVEATS = "Caveats about the whole run"
CORRECTED_MARK = "Corrected by the operator"
SECTION_INDEX = "Indices"
SECTION_ESTABLISHED = "Established defects"
SECTION_UNRESOLVED = "Unresolved"
# The corroborated set's heading takes the rung's own word for breadth, so a run of one
# runtime says contexts where a two-runtime run says models.
SECTION_CORROBORATED = "Corroborated by "
# Coverage gaps get a section of their own, and it is not in the appendix: a named missing
# test is work. On the defect ladder they had nowhere to go but the refuted list, whose
# heading tells a reader that nothing under it is work — and a run put eighteen real,
# named, missing tests there.
SECTION_COVERAGE_GAPS = "Tests to write"
SECTION_APPENDIX = "Appendix"
# The subsections, which are numbered by their parent where they are two ways into one
# thing and named plainly where they are parts of the appendix.
SUBSECTION_LEGEND = "Path legend"
SUBSECTION_RANKED = "Every defect — most severe first"
SUBSECTION_BY_FILE = "By file"
SUBSECTION_REFUTED = "Refuted"
SUBSECTION_COVERED = "Coverage gaps a test already covers"
SUBSECTION_OUTSIDE = "What one more file would settle"
SUBSECTION_VERIFIERS = "What each verification unit returned"
SUBSECTION_NOTES = "Clustering notes"
SUBSECTION_SYNTHESIS = "The synthesis round"
SUBSECTION_HOW_IT_RAN = "How this ran"
SUBSECTION_COVERAGE = "Coverage"
SUBSECTION_PROVENANCE = "Provenance"


class _Sections:
    """The report's section numbers, counted as the sections are written.

    A long report a person refers to by part needs its parts named, and the number is the
    name: "section 4" is what somebody says to somebody else. It is COUNTED rather than
    written into each heading because the section list is not fixed — see the note above
    :data:`SECTION_DESCRIPTION` — and a hand-numbered list goes wrong silently, one
    heading at a time, on exactly the runs nobody renders twice.
    """

    def __init__(self) -> None:
        self.n = 0

    def top(self, title: str, count: int | None = None) -> str:
        """The next top-level heading. ``count`` is how many entries sit under it, which
        a reader wants before deciding whether to read the section at all."""
        self.n += 1
        return f"\n## {self.n}. {_counted(title, count)}\n\n"

    def sub(self, title: str) -> str:
        """A subsection of the section just opened.

        Unnumbered, like every other subsection in the report. Only these two were ever
        lettered, so `2a` and `2b` were the one place a reader had to learn a second
        numbering scheme — and it named nothing the title did not: a subsection is referred
        to by what it is called, and the section it sits under is the heading above it.
        """
        return f"\n### {title}\n\n"


def _counted(title: str, count: int | None) -> str:
    """A heading and, where the section holds a countable set, its size.

    The count is the fact a reader acts on first — thirty-nine unresolved defects is a
    different document from three — and putting it in the heading is what stops them
    counting rows to find out.
    """
    return title if count is None else f"{title} ({count})"


# A defect's metadata, as ONE line under its heading rather than as three bullets. Each
# field is announced by a bold label and the fields are joined by a middle dot, and the
# labels are what make the line readable back: the dot is a character a file name may
# hold, while `*` reaches the prose escaped, so only the engine can write a label. The
# names are here rather than spelled inline because the writer and the pattern the page
# conversion matches this line with are both built from them.
DEFECT_META_LABELS = ("Severity", "Corroboration", "Location", "Fix size", "Verification")
DEFECT_META_SEP = " · "
# How much of a commit sha goes beside a defect and beside a snippet. Ten characters is
# what a person types and what separates any two commits in a tree this size; the whole
# sha is stated once, under "How this ran".
COMMIT_ABBREV = 10
# The two axes spec section 3 asks for, kept apart so neither can imply the other.
# Axis A is DISCOVERY BREADTH, and the stored value is the lane fact the run actually
# establishes: a candidate carries exactly one raiser, so ``both`` can only mean its
# cluster holds candidates raised by both lanes. Which words a rung permits for that —
# both models, both contexts, or no claim at all where one context was both finder and
# verifier — is the renderer's, which is why no rung vocabulary is stored here.
# Axis B is VERIFICATION STRENGTH and follows the evidence rather than the status: a
# refuted verdict reached by running the code was still reached by running.
AXIS_A_ONE, AXIS_A_BOTH = "one", "both"
AXIS_B_RUNNING, AXIS_B_READING, AXIS_B_UNRESOLVED = "by running", "by reading", "unresolved"
# A DEFECT's status, which is its cluster's and not any one member's: established when any
# member is, refuted only when every member is, unresolved otherwise. The asymmetry is the
# point — one established member makes a defect real however its siblings came back, and one
# unresolved member is not enough to unsettle a defect something else established.
DEFECT_ESTABLISHED, DEFECT_REFUTED, DEFECT_UNRESOLVED = "established", "refuted", "unresolved"
# What the report calls each settling reason, and the two groups for the cases the
# vocabulary has no word for. Every unresolved defect sits under exactly one of these: a
# defect appearing in two groups would be counted twice by anyone triaging from them.
UNRESOLVED_GROUPS = {
    "needs_a_run": "Needs a run",
    "needs_a_file_outside_the_scope": "Needs a file outside the reviewed scope",
    "needs_a_product_decision": "Needs a product decision",
    "blocked_by_the_environment": "Blocked by the environment",
}
MIXED_SETTLING = "More than one thing would settle this"
UNKNOWN_SETTLING = "No verdict was received"
# The three named prose parts a judged defect renders, and the line that carries its
# surviving cross-references. They replace the `Reported:`, `Direction:` and
# verdict-rationale bullets, which stay in `findings.json` — the narrative and the
# fix are written FROM those three, so keeping both would print one fact twice in two
# voices, one of them checked and one of them not.
WHAT_GOES_WRONG_LABEL = "What goes wrong."
FIX_LABEL = "Fix."
TEST_FIRST_LABEL = "Test that should fail first."
RELATED_LABEL = "Related."
# The fourth prose part, and the only one an UNRESOLVED defect has that an established one
# does not. It carries the checker's own rationale, which on an unresolved verdict is
# defined as "what was tried and what stopped it" — the one fact the reader of this section
# needs and the one the narrative above cannot supply, because the narrative is written
# about the claim and this is written about the limit of what could be established. On an
# established defect the same rationale IS redundant with the narrative and stays in the
# provenance record; that asymmetry is the whole reason this label exists.
UNSETTLED_LABEL = "What is not settled."
# Where a defect the round could not place goes. It is still in the body — a judgment
# stage may not delete a defect — and the heading says why it has no tier rather than
# filing it under one the round never gave it. The entry's own refusal is named under
# :data:`SUBSECTION_SYNTHESIS`, which is where the round's failures are collected.
UNTIERED_GROUP = "The round returned no usable answer for these"
# What the report says when the round failed, never landed, or came back as something the
# engine could not believe. Naming the grouping matters as much as naming the unit: a
# reader who sees status headings and knows a synthesis round exists cannot otherwise tell
# a run whose round was never dispatched from one whose round was thrown away.
SYNTHESIS_DEGRADED = ("No tier was assigned, so the defects above are grouped by their "
                      "status, exactly as they are on a run with no synthesis round.")
# The one sentence that marks the synthesised prose as a reading, printed where the body
# begins. Every other claim in the document traces to a unit that checked it or to the
# engine's own records; these trace to one agent's impression, and they are the most
# confident-sounding sentences on the page. It is written ONCE and only where there is
# something to disclaim — a disclaimer on a report carrying no synthesised prose is a
# sentence that says nothing, and a reader who meets one learns to skip the next.
SYNTHESIS_DISCLAIMER = (
    f"The **tier** headings below, and each defect's **{WHAT_GOES_WRONG_LABEL[:-1]}**, "
    f"**{FIX_LABEL[:-1]}** and **{RELATED_LABEL[:-1]}**, are one agent's reading of the "
    f"defects beneath them. Nothing checked them. Everything else in this report traces to "
    f"a unit that verified it or to the engine's own records, so read the grouping and "
    f"those three lines as a proposal and the rest as a finding."
)
# What a rung lets the report call Axis A: two models where two runtimes ran, two contexts
# of one model where one did, and nothing stronger than the rung the record names.
RUNG_BREADTH = {
    RUNG_TWO_RUNTIMES: ("one model", "both models"),
    RUNG_ONE_RUNTIME: ("one context", "both contexts"),
}
# What a record keeps of each raiser. Its consequence, direction and fix size are lifted to
# the candidate, which carries exactly one raiser; what is left is the provenance and the
# two things only the raiser can say — the severity it proposed and the source it quoted.
RAISER_KEYS = ("unit", "lane", "lens", "kind", "area", "severity", "quote", "reproduction")
_FIX_RANK = {size: i for i, size in enumerate(FIX_SIZES)}
# The rung names the prose layer's ladder, read from the record the dispatcher wrote and
# never inferred. What the rung lets the report claim is DERIVED in :func:`_rung_line`
# from what landed, never asserted: a reader that never returned means its area was not
# read by both lanes, and a candidate both lanes raised had no other model to go to.
RUNG_NAMES = {
    RUNG_TWO_RUNTIMES: "two runtimes",
    RUNG_ONE_RUNTIME: "one runtime with sub-agents",
}
# The engine cannot see a sandbox, so it repeats the dispatcher's record and says whose
# words they are. The one containment fact it can state is its own: every payload it
# wrote names snapshot paths and nothing under the source root. Whether a reproduction
# ran in a disposable copy is the dispatcher's to record in the permission text.
CONTAINMENT_LINE = (
    "Containment: the adapter and permission for each lane above are as the dispatcher "
    "recorded them, and the engine did not verify them. All the engine can state is that "
    "it spawned nothing and checked no sandbox, and that the files its payloads list are "
    "snapshot paths, none under the source root. It claims nothing stronger."
)
EVERY_UNIT_RETURNED = "Every unit returned a valid result."
# The report keeps at most this much of a reproduction's output, the tail: the brief asks
# the verifier for the same bound, and the parser does not enforce it.
EVIDENCE_OUTPUT_BOUND = 4096
# What the report says about a quotation that did not match, and about one it could not
# check. The first is a warning a reader acts on — the lines below may not be the lines the
# finding is about — and the engine says what it did NOT do, so nobody reads the snippet as
# a correction.
# It says "the quotation", not "the code below". The snippet moved ABOVE the prose, and an
# unresolved defect renders none at all, so a warning that points downward at code pointed
# at the next defect's heading on every established entry and at nothing whatsoever on every
# unresolved one.
QUOTE_FLAG = ("The source quoted with this report does not match what the pinned tree "
              "holds at those lines. The line range may be wrong. The engine left the range "
              "exactly as the report gave it, so read the location itself before acting on "
              "the quotation.")
QUOTE_UNCHECKED = ("The engine could not read those lines from the pinned tree, so the "
                   "quotation was not checked and no source is shown.")
# A snippet's context and its ceiling. The pad is what makes a one-line citation readable —
# a lone line tells nobody what it sits inside — and the ceiling is what stops a finding
# that cites four hundred lines from putting four hundred lines in front of a reader.
# Bounded where it is RENDERED, not where it is read: the range the reader cited is a fact
# about the finding and is stated whole either way.
SNIPPET_PAD = 2
SNIPPET_MAX_LINES = 40
SNIPPET_CUT_MARKER = "… cut here; the range above is what the finding cites …"
_SEVERITY_RANK = {level: i for i, level in enumerate(SEVERITIES)}
_STATUS_RANK = {status: i for i, status in enumerate(
    ("reproduced", "confirmed_by_reading", "unresolved", "refuted"))}
_AXIS_B_RANK = {axis: i for i, axis in enumerate(
    (AXIS_B_RUNNING, AXIS_B_READING, AXIS_B_UNRESOLVED))}
# A defect's place within its severity. Established first because it is work somebody can
# start, unresolved next because it is a question somebody can answer, refuted last because
# it is already closed.
_DEFECT_RANK = {status: i for i, status in enumerate(
    (DEFECT_ESTABLISHED, DEFECT_UNRESOLVED, DEFECT_REFUTED))}


def extract_snippet(snapshot: Path, record_file: str, line_start: int,
                    line_end: int) -> dict | None:
    """The lines a finding cites, taken from the pinned tree rather than from what anyone
    wrote about them.

    A quotation is a worker's account of the source; this is the source. ``None`` where the
    file could not be read or the range falls outside it, which is the same condition
    :func:`check_quotes` reports as ``unreadable`` — and the report says so rather than
    printing nothing and letting a reader assume there was nothing to print.

    The pad gives a one-line citation something to sit in. The ceiling cuts a long one and
    says it did; the cut is at the END, so what the finding cites is never the part that
    disappears.
    """
    try:
        lines = _physical_lines(_read_source(snapshot / record_file))
    except OSError as exc:
        # Tolerated for the reason the docstring gives: the report prints the condition
        # rather than an empty space, so no reader takes a missing snippet for an empty
        # file. The volume failing is excluded, because it says nothing about this file and
        # the report it is about to publish would carry no source at all.
        _stop_on_a_storage_fault(f"{record_file} in the snapshot", exc)
        return None
    if line_start < 1 or line_end > len(lines) or line_end < line_start:
        return None
    first = max(1, line_start - SNIPPET_PAD)
    last = min(len(lines), line_end + SNIPPET_PAD)
    kept = lines[first - 1:last]
    truncated = len(kept) > SNIPPET_MAX_LINES
    return {"first_line": first, "lines": kept[:SNIPPET_MAX_LINES], "truncated": truncated,
            "cites_from": line_start, "cites_to": line_end}


class ReportError(ReviewPanelError):
    """The verification results contradict the routing the run directory records. A stop
    naming the unit and the candidate, not a failed unit: a verifier answering for a
    candidate it was never handed saw something other than its payload."""


@dataclass(frozen=True)
class VerificationState:
    """What one verification unit came back as: exactly one of complete, failed or
    missing, with the reason where it is not complete and the verdicts where it is.

    ``rejected`` is the verdicts inside a COMPLETE unit that the engine could not read,
    one message each. Those candidates resolve ``unresolved`` like any other, and this is
    what keeps that from being silent: the coverage section names each one, so a reader
    sees the difference between a candidate nobody checked and one whose answer arrived
    unreadable.

    ``summary`` is the unit's own paragraph about the batch as a whole — what it checked
    and what the batch showed. It is carried because the coverage section states it above
    the gaps the unit answered, where a fact true of forty gaps is said once instead of
    forty times inside forty rationales.
    """

    id: str
    state: str
    reason: str | None
    verdicts: tuple[Verdict, ...]
    rejected: tuple[str, ...] = ()
    summary: str | None = None


@dataclass(frozen=True)
class Resolved:
    """One candidate's one status: the verdict of the unit that answered it, or
    ``unresolved`` with the reason when that unit failed or never landed.

    ``test_first`` and ``unresolved_reason`` are the verifier's own two fields, carried
    through because the record the report is built from needs them and nothing between
    here and there would otherwise hold them. A unit that never answered supplies neither:
    ``unresolved_reason`` says what would settle a candidate a verifier looked at, and no
    verifier looked at this one.
    """

    candidate: str
    unit: str
    lane: str
    status: str
    evidence: dict | None
    rationale: str
    revision: dict | None
    test_first: str | None
    unresolved_reason: str | None
    # The verifier's third own field, carried for the same reason as the other two: a
    # coverage gap the check refuted names the test that covers it, and nothing between
    # here and the report would otherwise hold it.
    covered_by: str | None
    # The files this verdict named as what would settle it. Carried like the other three
    # verifier-owned fields: the appendix adds them up across the run, and nothing between
    # here and there would otherwise hold them.
    needs_files: tuple[str, ...]
    # Whether a verdict actually came back. An unresolved candidate a verifier answered for
    # and one whose unit never landed are both unresolved, and only this tells them apart —
    # which is what lets the report say the second in plain words instead of printing the
    # id of the unit that failed into the material somebody reads to fix something. False
    # as well for a candidate whose unit came back and whose verdict could not be read:
    # what stands in for that verdict is the engine's, and nobody answered.
    answered: bool


def _leaked_candidates(obj: object, batch: dict[str, bool]) -> list[str]:
    """Candidate ids a raw result names that its batch never held, read before the strict
    parse so this one condition can stop the report rather than fail the unit."""
    if not isinstance(obj, dict) or not isinstance(obj.get("verdicts"), list):
        return []
    return [item["candidate"] for item in obj["verdicts"]
            if isinstance(item, dict) and isinstance(item.get("candidate"), str)
            and item["candidate"] not in batch]


def read_verification_results(rundir: Path, batches: Sequence[dict],
                              reproducible: dict[str, bool]) -> tuple[VerificationState, ...]:
    """Classify every verification unit as exactly one of complete, failed or missing.

    A result whose SHAPE the parser refuses — a missing verdict, a repeated one, a
    candidate outside the batch — fails the WHOLE unit, so every candidate in its batch
    resolves ``unresolved``, the answered ones included: silence is not a verdict, and a
    batch answered short is not partly right. A single unreadable VERDICT inside an
    otherwise well-formed result costs its own candidate alone; see
    :func:`parse_verifier_result` for why the two are different. ``reproducible`` maps
    every candidate id to whether it carries a proposed reproduction.
    """
    states: list[VerificationState] = []
    for unit in batches:
        state, payload = _read_unit_file(rundir, unit["id"])
        if state != UNIT_COMPLETE:
            states.append(VerificationState(unit["id"], state, payload, ()))
            continue
        batch = {cid: reproducible[cid] for cid in unit["candidates"]}
        leaked = _leaked_candidates(payload, batch)
        if leaked:
            raise ReportError(
                f"unit {unit['id']} answers for {leaked[0]!r}, which is not in its batch "
                f"({', '.join(unit['candidates'])}); the verifier saw something other than "
                f"its payload, so the routing this run directory records is not what ran"
            )
        try:
            verdicts, rejected = parse_verifier_result(payload, unit["id"], batch,
                                                       unit.get("asks", DEFECT_ASKS))
        except ResultError as exc:
            states.append(VerificationState(unit["id"], UNIT_FAILED, str(exc), ()))
            continue
        # Read here rather than returned by the parse, whose two-value shape a dozen
        # callers unpack. The parse has already proved the field is text; this normalizes
        # it the way every other worker string is normalized before it is carried.
        states.append(VerificationState(unit["id"], UNIT_COMPLETE, None, verdicts, rejected,
                                        _text(payload["summary"], "summary", empty_ok=True)))
    return tuple(states)


def check_routing(candidates: Sequence[dict], batches: Sequence[dict]) -> dict[str, dict]:
    """Every candidate to the one verification unit that holds it. ``route`` put each in
    exactly one, so a candidate in none, in two, or listed by a batch and absent from
    ``candidates.json`` is a run directory the engine did not write — refused by name,
    before any result is read."""
    known = {cand["id"] for cand in candidates}
    holder: dict[str, dict] = {}
    for batch in batches:
        for cid in batch["candidates"]:
            if cid not in known:
                raise RunDirError(
                    f"verification unit {batch['id']} lists candidate {cid}, which "
                    f"{CANDIDATES_FILE_NAME} does not hold; {UNITS_FILE_NAME} is not the engine's"
                )
            if cid in holder:
                raise RunDirError(
                    f"candidate {cid} is in two verification units, {holder[cid]['id']} and "
                    f"{batch['id']}; {UNITS_FILE_NAME} is not the engine's"
                )
            holder[cid] = batch
    for cid in sorted(known - set(holder)):
        raise RunDirError(f"candidate {cid} is in no verification unit; {UNITS_FILE_NAME} is not the engine's")
    return holder


def resolve(candidates: Sequence[dict], holder: dict[str, dict],
            states: Sequence[VerificationState]) -> dict[str, Resolved]:
    """Every candidate to exactly one status, ``holder`` being :func:`check_routing`'s map
    of candidate id to the batch that holds it."""
    by_unit = {state.id: state for state in states}
    out: dict[str, Resolved] = {}
    for cand in candidates:
        cid = cand["id"]
        batch = holder[cid]
        state = by_unit[batch["id"]]
        if state.state == UNIT_COMPLETE:
            verdict = next(v for v in state.verdicts if v.candidate == cid)
            # Answered only where a VERIFIER answered. The stand-in the parser writes for a
            # verdict it could not read is engine text, and counting it as an answer put
            # the parse error on the page as the checker's own words; the coverage section
            # is where that text belongs, and it is there by name.
            out[cid] = Resolved(cid, batch["id"], batch["lane"], verdict.status, verdict.evidence,
                                verdict.rationale, verdict.revision, verdict.test_first,
                                verdict.unresolved_reason, verdict.covered_by,
                                verdict.needs_files, verdict.from_verifier)
        else:
            out[cid] = Resolved(
                cid, batch["id"], batch["lane"], "unresolved", None,
                f"no verdict was received: verification unit {batch['id']} {state.state} ({state.reason})",
                None, None, None, None, (), False,
            )
    return out


def _where(cand: dict) -> str:
    if cand["line_start"] == cand["line_end"]:
        return f"line {cand['line_start']}"
    return f"lines {cand['line_start']}-{cand['line_end']}"


@dataclass(frozen=True)
class Findings:
    """The run's findings as data: one record per candidate, one per cluster, and one per
    lane clustering touched — with the defects and the coverage gaps kept apart.

    Every rendering reads this, so a value the prose states and a value a consumer reads
    cannot drift apart — they are one field, computed once, in :func:`build_findings`.
    ``rung`` travels with the records because a reader of the file alone has to know what
    checked each status before believing it.
    """

    rung: str
    # The synthesis round, or ``None`` where the run has no such round at all. A record
    # here with a state other than ``complete`` is a round that was dispatched and could
    # not be believed, which is a different fact from a run that never had one: the first
    # is a gap the report names, the second is a shorter pipeline.
    synthesis: dict | None
    # Where the snapshot was taken from. A line number means nothing without it, so it
    # travels with the records rather than being fetched beside them — a rendering built
    # from this file alone can still say which tree its snippets came from.
    commit: dict
    # DEFECTS only, both of them. A coverage gap is not a defect and does not belong on the
    # ladder a defect is ranked, established or refuted on: it makes no claim about the code
    # being wrong, so "refuted" said of it means "a test covers this", which is the opposite
    # of the thing that word means two sections up. Every consumer that ranks, counts or
    # renders defects reads these two and is correct without knowing coverage exists.
    candidates: tuple[dict, ...]
    clusters: tuple[dict, ...]
    clustering: tuple[dict, ...]
    # The coverage ladder, same two shapes. Empty on a run with no auditor, which is why
    # nothing here branches on whether the round ran.
    coverage: tuple[dict, ...] = ()
    coverage_clusters: tuple[dict, ...] = ()
    # Two aggregates over BOTH ladders, each the same shape as ``clustering``: one record
    # per verification unit, and one per file outside the scope that open work hangs on.
    # Computed once here for the same reason every defect fact is — two renderings of one
    # number cannot disagree when there is only one number.
    verification: tuple[dict, ...] = ()
    outside_scope: tuple[dict, ...] = ()


def _most_severe(levels: Sequence[str]) -> str:
    return sorted(levels, key=_SEVERITY_RANK.__getitem__)[0]


def _strongest(axes: Sequence[str]) -> str:
    """The strongest Axis B any member reached. Running beats reading beats unresolved."""
    return sorted(axes, key=_AXIS_B_RANK.__getitem__)[0]


def _axis_b(status: str, evidence: dict | None) -> str:
    """Verification strength, which follows the evidence and not the status. A refuted
    verdict that ran the code was reached by running, and an established one that did not
    is still only read — letting the status decide is exactly the conflation section 3
    exists to end."""
    if status == "unresolved":
        return AXIS_B_UNRESOLVED
    return AXIS_B_RUNNING if evidence is not None else AXIS_B_READING


# What the report CLAIMS about how a check was reached, which is not what the engine
# stores. `axis_b` follows the presence of evidence, so a `grep` and a run of the code both
# land on `by running` -- and in one real report every one of 89 runs was a search, while
# every defect entry said the code had been run. The summary at the top of that report drew
# the distinction and the entries did not, so the strongest-looking claim on the page was
# the least honest one.
#
# A rename of the claim and not of the record: `axis_b` keeps its three values, so
# `findings.json`, the ranking and the counts are untouched.
_DOCUMENTARY_WORDS = "by reading, with a documentary run"
_UNSTATED_RUN_WORDS = "by a run of a kind the verdict did not state"


def _verification_words(axis_b: str, evidence: dict | None) -> str:
    """The reader-facing phrase for one verification method.

    Only `executed` earns `by running`. A documentary run is reading -- a search, a listing,
    a checksum -- so it says so, and still names the run, because the run is evidence and a
    reader deciding whether to trust the claim wants to know one happened.
    """
    if axis_b != AXIS_B_RUNNING:
        return axis_b
    kind = (evidence or {}).get(RUN_KIND_KEY)
    if kind == "executed":
        return AXIS_B_RUNNING
    return _DOCUMENTARY_WORDS if kind == "documentary" else _UNSTATED_RUN_WORDS


def _defect_verification_words(cluster: dict, members: Sequence[dict]) -> str:
    """The same phrase for a whole defect, from the member whose method the defect's own
    `axis_b` came from. Two places print this claim, and a fix to one of them alone is a
    disagreement between the header and the line below it."""
    # Both documentary and executed evidence carry `by running`, so the FIRST member
    # matching the axis is not necessarily the one that earned the defect's claim: a
    # documentary member ahead of an executed one made the header say a grep had settled a
    # defect something was run for, and reversing the members changed the header.
    candidates = [r for r in members if r["axis_b"] == cluster["axis_b"]]
    for record in candidates:
        if (record["evidence"] or {}).get(RUN_KIND_KEY) == "executed":
            return _verification_words(cluster["axis_b"], record["evidence"])
    for record in candidates:
        return _verification_words(cluster["axis_b"], record["evidence"])
    return cluster["axis_b"]


def _clustering_records(candidates: Sequence[dict], clustering: Clustering,
                        states: Sequence[ClusterState]) -> tuple[dict, ...]:
    """One record per (area, kind) that raised anything: the clustering unit that answered
    for it, its state, the clusterer's summary where it answered, and the reason where it
    did not.

    A per-cluster record cannot hold either the summary or the reason, so without this list
    a rendering that wants them needs a second input — and the rule both renderings are
    built on is that there is only one. ``listed`` says whether ``units.json`` names a unit
    for the area at all, which is what separates a unit that failed from an area that was
    never dispatched one; the counts the coverage section reports are over units.
    """
    state_of = {(state.area, state.asks): state for state in states}
    ungrouped_of = {(entry.area, entry.asks): entry for entry in clustering.ungrouped}
    asked = {question: i for i, question in enumerate(ASKS)}
    out: list[dict] = []
    for lane in sorted({(cand["area"], asks_of(cand)) for cand in candidates},
                       key=lambda k: (k[0], asked[k[1]])):
        state, entry = state_of.get(lane), ungrouped_of.get(lane)
        # build_clusters made an Ungrouped entry for exactly the lanes that did not come
        # back usable, so its words are reused rather than derived a second time here.
        out.append({
            "area": lane[0],
            "asks": lane[1],
            "unit": entry.unit if entry is not None else state.id,
            "listed": state is not None,
            "state": entry.state if entry is not None else state.state,
            "reason": entry.reason if entry is not None else None,
            "summary": state.summary if state is not None else None,
        })
    return tuple(out)


def _verification_records(records: Sequence[dict],
                          states: Sequence[VerificationState],
                          batches: Sequence[dict] = ()) -> tuple[dict, ...]:
    """One record per verification unit: what it was handed, and how its answers fell.

    The established count over one set of files swung by a third between two runs of one
    job, and the swing was entirely here — one unit answering `unresolved` to fifteen of
    twenty-six while another answered it to none of twenty. Every verdict was already in
    the report, one row per candidate, which is the form in which nobody can see that. A
    per-unit count is the same data at the size the variance happens at.

    ``state`` rides along because a unit that failed or never landed also produces a column
    of unresolved, and that is not a verifier's judgment about anything. Reading the two
    together is what tells a spread of opinion from a unit that did not answer.

    ``finder`` is the lane whose findings the unit was handed, off the unit row in
    ``units.json``. Routing keys a batch on (area, finder, question), so every candidate a
    unit holds was raised by one lane — which is what makes the unresolved share here
    readable as a question about that lane's findings rather than only about this
    verifier. Taken from the listing and not counted off the records, so a unit that
    failed or never landed still says whose work went unanswered.

    ``summary`` is the unit's own paragraph, carried for the coverage section; see
    :class:`VerificationState`.
    """
    finder_of = {batch["id"]: batch.get("finder") for batch in batches}
    by_unit: dict[str, list[dict]] = {}
    for record in records:
        answered_by = record["verified_by"]
        if answered_by is not None:
            by_unit.setdefault(answered_by["unit"], []).append(record)
    state_of = {state.id: state for state in states}
    out: list[dict] = []
    for unit in sorted(by_unit):
        held = by_unit[unit]
        state = state_of.get(unit)
        counts = {status: sum(1 for r in held if r["status"] == status)
                  for status in (*VERDICT_STATUSES, *COVERAGE_STATUSES)}
        established = sum(counts[status] for status in
                          (*ESTABLISHED_STATUSES, *COVERAGE_ESTABLISHED))
        refuted = counts["refuted"] + counts[COVERAGE_GAP_REFUTED]
        out.append({
            "unit": unit,
            "asks": COVERAGE_ASKS if unit.endswith(COVERAGE_BATCH_SUFFIX) else DEFECT_ASKS,
            "finder": finder_of.get(unit),
            "candidates": len(held),
            "established": established,
            "unresolved": counts["unresolved"],
            "refuted": refuted,
            "state": state.state if state is not None else UNIT_MISSING,
            "reason": state.reason if state is not None else None,
            "summary": state.summary if state is not None else None,
        })
    return tuple(out)


# What the entry names, once its guessed directories are taken off: the last component
# without its final extension. `api/server/PaymentController.java` and `PaymentController`
# are the same answer written by two verifiers with different amounts of the tree in front
# of them.
def _class_named(path: str) -> str:
    base = path.rsplit("/", 1)[-1]
    return base.rsplit(".", 1)[0] or base


def _outside_scope_records(records: Sequence[dict]) -> tuple[dict, ...]:
    """Every class outside the reviewed set that open work hangs on, with how much of this
    run's open work hangs on it, and every path the run's verdicts guessed it at.

    Widening a job's scope until it covers every caller is how a bounded run stops being
    one, which is why the answer to "pull these in" is usually no. This is the other half
    of that answer: a class named by nine open claims and one named by one cost the same
    to add and are not the same decision, and without the count an owner saying no is
    saying it blind.

    **Keyed by the class rather than by the path, because the path is the part the verifier
    could not check.** A file outside the reviewed set is one nothing in this run can open,
    so every spelling here is a guess; one report priced a controller under
    `api/server/...` at 12 defects and the same class under `api/lib/...` at 2, which is
    one decision worth 14 split into two neither of which reads as the one to take. A
    verdict that can name the class and not the path at all is the same answer with the
    guess left out, and it lands in the same row.

    The trade is that two genuinely different files sharing a base name — two `index.ts` —
    merge into one row. Every path is kept in the row against exactly that: the merge
    states its working, and a reader who knows they are two files can see both spellings.

    Counted in DEFECTS rather than candidates, because a defect is the unit of work the
    decision is about — two reports of one problem are one thing that would be settled. The
    defects are a UNION and not a sum: one defect whose verdicts guessed both spellings
    would otherwise be bought twice.
    """
    by_class: dict[str, set[str]] = {}
    paths_of: dict[str, set[str]] = {}
    for record in records:
        for path in record.get("needs_files", ()):
            name = _class_named(path)
            by_class.setdefault(name, set()).add(record["cluster_id"])
            paths_of.setdefault(name, set()).add(path)
    return tuple(
        {"class": name, "paths": sorted(paths_of[name]),
         "defects": sorted(by_class[name], key=_id_rank)}
        for name in sorted(by_class, key=lambda n: (-len(by_class[n]), n))
    )


def build_findings(dispatch: DispatchRecord, candidates: Sequence[dict],
                   resolved: dict[str, Resolved], clustering: Clustering,
                   cluster_states: Sequence[ClusterState], commit: dict,
                   snippets: dict[str, dict | None],
                   synthesis: Synthesis | None,
                   verification_states: Sequence[VerificationState] = (),
                   batches: Sequence[dict] = ()) -> Findings:
    """Every qualifier a rendering can show, carried as its own field.

    The severity rule the report ranks by is applied once, here: the verifier's revision
    where it made one, else the most severe proposal.

    That one raiser is also why ``consequence``, ``direction`` and ``fix_size`` are
    candidate-level fields lifted off it rather than a list: they are its proposal, and
    ``raised_by`` therefore carries only what stays the raiser's own — who it was, what it
    read, the severity IT proposed, the source it quoted and the reproduction it offered.
    Carrying the lifted three in both places would be one fact in two spellings, free to
    drift.

    Axis A is read off the cluster because that is the only place the fact lives: a
    candidate carries exactly one raiser, so ``both`` means the cluster holds candidates
    raised by both lanes. A cluster aggregates over its ESTABLISHED members alone — an
    unestablished sibling must not lend a cluster a severity nothing verified — and falls
    back to all of them where none is established, since a cluster still needs one. No
    aggregate hides a member: each keeps its own status and its own Axis B.

    ``synthesis`` is a declared input with no default, for the reason ``rationales`` has
    none in the clustering payload: an input that may be omitted is an input a caller can
    silently forget, and forgetting this one renders a report with every judgment missing
    and nothing saying so.

    The synthesis round's answer is lifted onto the cluster it is about, so a rendering
    reads one record per defect rather than joining two lists at render time — the drift
    ``findings.json`` exists to prevent. All five fields are written on every cluster
    whatever the round did, because the shape of this file must not depend on what a run
    found: a defect nothing judged says so with nulls and two empty lists.

    A cross-reference is **checked here rather than where it is rendered**, for the same
    reason: checking it at render time would make two renderings decide separately which
    references stand, and :func:`resolve_cross_references` says what the check is. The
    ones that failed are kept beside the ones that stand, so the stage that renders them
    can say what was dropped instead of showing a shorter list and nothing else.

    A cluster also carries what the index and the body's grouping read: its own status, the
    one reason that would settle it where its unresolved members agree on one, the largest
    fix any member needs, and the location of its lowest member, which is where a reader
    opens the file.
    """
    cluster_of = {cid: cluster for cluster in clustering.clusters for cid in cluster.members}
    lane_of = {cand["id"]: cand["raised_by"][0]["lane"] for cand in candidates}
    axis_a_of = {
        cluster.id: AXIS_A_BOTH if len({lane_of[cid] for cid in cluster.members}) > 1 else AXIS_A_ONE
        for cluster in clustering.clusters
    }
    records: list[dict] = []
    for cand in candidates:
        answer = resolved[cand["id"]]
        raiser = cand["raised_by"][0]
        proposed = _most_severe([r["severity"] for r in cand["raised_by"]])
        revision = answer.revision
        status = answer.status
        evidence = answer.evidence
        records.append({
            "id": cand["id"],
            "area": cand["area"],
            "file": cand["file"],
            "line_start": cand["line_start"],
            "line_end": cand["line_end"],
            "asks": asks_of(cand),
            "severity_final": revision["severity"] if revision is not None else proposed,
            "severity_proposed": proposed,
            "revision": revision,
            "status": status,
            "axis_a": axis_a_of[cluster_of[cand["id"]].id],
            "axis_b": _axis_b(status, evidence),
            "raised_by": [{key: entry[key] for key in RAISER_KEYS}
                          for entry in cand["raised_by"]],
            "verified_by": {"lane": answer.lane, "unit": answer.unit},
            "consequence": raiser["consequence"],
            "failure": cand["failure"],
            "direction": raiser["direction"],
            "fix_size": raiser["fix_size"],
            "rationale": answer.rationale,
            "evidence": evidence,
            "test_first": answer.test_first,
            # The test that already covers a refuted gap. ``None`` on every defect record
            # and on every gap that stood: the question only a coverage verifier is asked.
            "covered_by": answer.covered_by,
            "needs_files": list(answer.needs_files),
            "unresolved_reason": answer.unresolved_reason,
            "cluster_id": cluster_of[cand["id"]].id,
            "quote_check": cand["quote_check"],
            "snippet": snippets.get(cand["id"]),
            "answered": answer.answered,
            # How many OTHER candidates share this defect. The renderer shows a member's own
            # location only where there is another one to tell it apart from.
            "siblings": len(cluster_of[cand["id"]].members) - 1,
        })
    by_id = {record["id"]: record for record in records}
    # Every file each defect touches, which is what a cross-reference is checked against.
    # Built over the whole run before any record is written, because the check is between
    # two defects and the second may be one this loop has not reached.
    files_of = {cluster.id: frozenset(by_id[cid]["file"] for cid in cluster.members)
                for cluster in clustering.clusters}
    # And the tier each sits under, for the same reason and over the same whole run. Empty
    # where the synthesis round did not run, which is not the same as every defect sharing
    # one tier -- see :func:`resolve_cross_references`.
    # `assignments` maps a defect to None where the round's entry for it could not be read
    # -- an undeclared tier, an empty narrative -- which costs that defect its tier and no
    # other. So the value is checked, not the key: a rejected entry has a key and no tier.
    tier_of: dict[str, str | None] = {}
    for cluster in clustering.clusters:
        entry = synthesis.assignments.get(cluster.id) if synthesis is not None else None
        tier_of[cluster.id] = entry["tier"] if entry else None
    clusters = []
    for cluster in clustering.clusters:
        members = [by_id[cid] for cid in cluster.members]
        judged = synthesis.assignments.get(cluster.id) if synthesis is not None else None
        kept_refs, dropped_refs = resolve_cross_references(
            cluster.id, judged["cross_references"] if judged else (), files_of, tier_of)
        # The lane's own vocabulary. A gap "established" is a test to write and a gap
        # "refuted" is one already written; the words are the report's three, and reading
        # them off the defect statuses would file every gap as unresolved.
        established = [m for m in members if m["status"] in ESTABLISHED_FOR[cluster.asks]]
        refusing = "refuted" if cluster.asks == DEFECT_ASKS else COVERAGE_GAP_REFUTED
        held = {m["status"] for m in members}
        if established:
            status = DEFECT_ESTABLISHED
        elif held == {refusing}:
            status = DEFECT_REFUTED
        else:
            status = DEFECT_UNRESOLVED
        # None sorts last and means a member a verifier never answered for. Kept in the
        # list because it is a THIRD case: a defect nothing settled because no verdict
        # arrived is not a defect whose reports disagree about what would settle it, and
        # filing the two together would send somebody looking for a disagreement that is
        # not there.
        reasons = sorted({m["unresolved_reason"] for m in members
                          if m["status"] == "unresolved"},
                         key=lambda reason: (reason is None, reason or ""))
        primary = min(members, key=lambda m: (m["file"], m["line_start"], m["line_end"], m["id"]))
        clusters.append({
            "id": cluster.id,
            "area": cluster.area,
            "asks": cluster.asks,
            "members": list(cluster.members),
            "consequence": cluster.consequence,
            "split_reason": cluster.split_reason,
            "grouped": cluster.grouped,
            "severity": _most_severe([m["severity_final"] for m in (established or members)]),
            "axis_a": axis_a_of[cluster.id],
            "axis_b": _strongest([m["axis_b"] for m in members]),
            "status": status,
            # One reason only where every unresolved member gives the same one. Where they
            # differ the defect has no single settling question, and saying so beats picking
            # one of them and filing the defect under a question it only half answers. The
            # list beside it is the same computation, kept whole so the renderer can tell
            # an empty answer from a disagreement.
            "unresolved_reasons": reasons,
            "unresolved_reason": reasons[0] if len(reasons) == 1 else None,
            "fix_size": max((m["fix_size"] for m in members), key=_FIX_RANK.__getitem__),
            "file": primary["file"],
            "line_start": primary["line_start"],
            "line_end": primary["line_end"],
            "tier": judged["tier"] if judged else None,
            "what_goes_wrong": judged["what_goes_wrong"] if judged else None,
            "fix": judged["fix"] if judged else None,
            # The references that survived checking, and the ones that did not with the
            # reason each was dropped. A dropped reference is recorded rather than silently
            # discarded: the round is asked for connections, and a reader who is never told
            # one was refused cannot tell a defect that stands alone from one whose only
            # stated connection failed. Rendering the names is phase 6's, which is the
            # stage that renders a cross-reference at all.
            "cross_references": kept_refs,
            "dropped_cross_references": dropped_refs,
        })
    # Split last, on the one field that says which ladder each is on, so everything above
    # is computed once for both. Every consumer of ``candidates`` and ``clusters`` ranks,
    # counts and renders DEFECTS, and is right without knowing coverage exists.
    coverage = tuple(r for r in records if r["asks"] == COVERAGE_ASKS)
    defects = tuple(r for r in records if r["asks"] == DEFECT_ASKS)
    gap_clusters = tuple(c for c in clusters if c["asks"] == COVERAGE_ASKS)
    defect_clusters = tuple(c for c in clusters if c["asks"] == DEFECT_ASKS)
    return Findings(dispatch.rung, synthesis.record if synthesis else None,
                    commit, defects, defect_clusters,
                    _clustering_records(candidates, clustering, cluster_states),
                    coverage, gap_clusters,
                    # Over both ladders: a coverage batch is a verification unit like any
                    # other, and a gap can be open on a file outside the scope too.
                    _verification_records(records, verification_states, batches),
                    _outside_scope_records(records))


def findings_document(findings: Findings) -> dict:
    """``findings.json`` as it lands. :func:`write_json` sorts keys, fixes the indent and
    escapes non-ASCII, so two runs over one tree write the same bytes and a lone surrogate
    in a worker's text cannot stop the write.

    **No clock and no path.** This is the structure, and two runs over one tree write it
    byte for byte the same; the run directory holds exactly one clock, in
    ``inventory.json``, saying when the tree was read. A rendered-at stamp put here to make
    ``--rerender`` reproducible broke both properties at once -- it is a fact about the
    rendering, so it belongs to the rendering, and :func:`_previous_stamp` reads it back
    from there.
    """
    return {
        "rung": findings.rung,
        "synthesis": findings.synthesis,
        "commit": findings.commit,
        "candidates": list(findings.candidates),
        "clusters": list(findings.clusters),
        "clustering": list(findings.clustering),
        "coverage": list(findings.coverage),
        "coverage_clusters": list(findings.coverage_clusters),
        "verification": list(findings.verification),
        "outside_scope": list(findings.outside_scope),
    }


def _unit_label(unit: dict) -> str:
    if unit["kind"] == VERIFIER_KIND:
        return f"{unit['id']} (verifier for {unit['area']}, addressed to lane {unit['lane']})"
    if unit["kind"] == AUDITOR_KIND:
        return f"{unit['id']} (auditor, lane {unit['lane']})"
    return f"{unit['id']} (reader, lane {unit['lane']}, lens {json.dumps(unit['lens'])})"


# The fence's language, by extension. NAMING a language costs nothing and lets every
# renderer downstream of this file colour it properly; this file colours only the one it
# has a tokenizer for, which `_PAINTED_LANGUAGES` names.
_FENCE_LANGUAGES = {
    ".java": "java", ".py": "python", ".ts": "typescript", ".tsx": "tsx",
    ".js": "javascript", ".jsx": "jsx", ".kt": "kotlin", ".go": "go", ".rb": "ruby",
    ".rs": "rust", ".c": "c", ".h": "c", ".cpp": "cpp", ".cs": "csharp",
    ".sql": "sql", ".sh": "bash", ".yml": "yaml", ".yaml": "yaml", ".xml": "xml",
    ".json": "json", ".php": "php", ".swift": "swift", ".scala": "scala",
}


# Colour, baked in at render time. No runtime library: `report.html` is one file somebody
# opens out of a run directory, so a CDN highlighter would render plain wherever there is no
# network. Only a language with a tokenizer here is coloured; everything else falls through
# and renders exactly as it did before, which is the honest failure rather than a wrong one.
_JAVA_KEYWORDS = frozenset("""abstract assert boolean break byte case catch char class const
continue default do double else enum extends final finally float for goto if implements import
instanceof int interface long native new package private protected public return short static
strictfp super switch synchronized this throw throws transient try void volatile while var
record yield sealed permits true false null""".split())

_CODE_TOKEN = re.compile(r"""
    (?P<c>//[^\n]*|/\*.*?\*/)
  | (?P<s>"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*')
  | (?P<a>@[A-Za-z_]\w*)
  | (?P<n>\b\d[\w.]*\b)
  | (?P<w>\b[A-Za-z_]\w*\b)
""", re.X | re.S)

_CODE_GUTTER = re.compile(r"^(\s*\d+\s*\|)(.*)$")


# The languages this file can actually tokenize. A fence may NAME any language -- that is
# what lets a renderer downstream colour it -- but only a name in here is coloured HERE, and
# painting Java's keywords over Python or SQL would be worse than leaving them plain.
_PAINTED_LANGUAGES = frozenset({"java"})


def _painted(source: str, lang: str) -> str:
    """One fenced block, HTML-escaped and coloured. Unknown language: escaped, uncoloured."""
    if lang not in _PAINTED_LANGUAGES:
        return _html_entities(source)
    def paint(text: str) -> str:
        out, last = [], 0
        for m in _CODE_TOKEN.finditer(text):
            out.append(_html_entities(text[last:m.start()]))
            kind, word = m.lastgroup, m.group()
            cls = ("k" if word in _JAVA_KEYWORDS else "t" if word[:1].isupper() else "") \
                if kind == "w" else kind
            body = _html_entities(word)
            out.append(f'<span class="hl-{cls}">{body}</span>' if cls else body)
            last = m.end()
        out.append(_html_entities(text[last:]))
        return "".join(out)
    def body(text: str, inside: bool) -> tuple[str, bool]:
        """One line's code, painted, and whether a block comment is still open after it.

        The tokenizer matches `/* ... */` whole, and never sees one: the gutter is a
        per-line prefix, so the source reaches it one line at a time and a comment spanning
        two of them is two lines of ordinary code. Java code carries multi-line comments as
        a matter of course, and every keyword inside one came out coloured as though it ran.
        """
        if inside:
            closing = text.find("*/")
            if closing < 0:
                return f'<span class="hl-c">{_html_entities(text)}</span>', True
            head = text[:closing + 2]
            return (f'<span class="hl-c">{_html_entities(head)}</span>'
                    + paint(text[closing + 2:]), False)
        # An opener with no closer after it on this line: everything from it is comment,
        # and the next line continues inside one. `rfind` because a line may close an
        # earlier comment and open another.
        opening = text.rfind("/*")
        if opening >= 0 and text.find("*/", opening) < 0:
            return (paint(text[:opening])
                    + f'<span class="hl-c">{_html_entities(text[opening:])}</span>', True)
        return paint(text), False

    lines, inside = [], False
    for line in source.split("\n"):
        gutter = _CODE_GUTTER.match(line)
        code, inside = body(gutter.group(2) if gutter else line, inside)
        if gutter:
            code = (f'<span class="hl-g">{_html_entities(gutter.group(1))}</span>' + code)
        lines.append(code)
    return "\n".join(lines)


def _fence_language(path: str | None) -> str:
    """The language tag for a path, or none where the extension is not one we can name."""
    if not path:
        return ""
    dot = path.rfind(".")
    return _FENCE_LANGUAGES.get(path[dot:].lower(), "") if dot != -1 else ""


def _fenced(lines: Sequence[str], indent: str = "  ", lang: str = "") -> list[str]:
    """Worker text as a fenced block indented under the item it belongs to, the fence one
    backtick longer than any run the text holds so no line of it can close the fence
    early. Inside it nothing is a heading, a list item or a status line, whatever the
    text says."""
    longest = max((len(run) for line in lines for run in re.findall(r"`+", line)), default=0)
    fence = "`" * max(3, longest + 1)
    return [f"{indent}{fence}{lang}\n", *(f"{indent}{line}\n" for line in lines),
            f"{indent}{fence}\n"]


def _item(prefix: str, text: str, tail: str | None = None) -> list[str]:
    """One list item carrying a string a worker or the dispatcher wrote. A single line
    renders inline after ``prefix``; more than one is fenced, so a newline followed by
    ``- Status: reproduced`` or ``## Coverage`` inside a rationale, a failure or an
    output cannot forge the report's structure. ``tail`` is the engine's own sentence
    that follows the string.

    Only ``text`` is protected here. A PREFIX is the engine's own sentence, and any worker
    string a caller builds into one has to go through :func:`_one_line` first — the fence
    below is behind the prefix and cannot reach it. That is not theoretical: an ``argv`` of
    ``["python3", "-c", "<!--"]`` is a valid reproduction, and JSON quoting escapes nothing
    a Markdown viewer reads as markup.
    """
    lines = text.splitlines()
    # A single line opening markup is fenced too: `<!--` inline hides everything after it in a
    # viewer that renders HTML.
    if len(lines) <= 1 and "<" not in text:
        line = _entities(lines[0]) if lines else ""
        return [f"{prefix}{line}{'. ' + tail if tail else ''}\n"]
    out = [f"{prefix.rstrip()}\n", *_fenced(lines)]
    if tail:
        out.append(f"  {tail}\n")
    return out


def _recorded(prefix: str, text: str) -> list[str]:
    """One thing the dispatcher wrote down about how a lane ran, always fenced.

    :func:`_item` decides between inline and fenced from the text, which is right for
    worker prose — a one-line rationale reads better on the line it belongs to. It is wrong
    here, because these four lines sit together and are read as a set: whichever of them
    happened to hold a ``<`` came out as a code block while its neighbour stayed inline,
    and one holding a backtick came out inline with the backtick escaped. Three renderings
    of one kind of line, chosen by what somebody typed.

    Fenced is the form that needs no escaping at all, so it is also the only one that shows
    the dispatcher's text exactly as recorded — which is what this section is for. It is
    what a command line wants anyway, and these are command lines.
    """
    return [f"{prefix.rstrip()}\n", *_fenced(text.splitlines() or [""])]


def _bounded_output(output: str) -> tuple[str, bool]:
    data = output.encode("utf-8")
    if len(data) <= EVIDENCE_OUTPUT_BOUND:
        return output, False
    # The cut moves forward past a partial character, rather than decoding it into a
    # replacement character that would push the kept bytes past the bound.
    start = len(data) - EVIDENCE_OUTPUT_BOUND
    while start < len(data) and data[start] & 0xC0 == 0x80:
        start += 1
    return data[start:].decode("utf-8"), True


# The language tag on a captured-output fence. The page folds these away by default; a
# source snippet is what a reader came to look at and stays open.
EVIDENCE_FENCE_LANG = "console"
# What the folded block is called when it is closed. Says what is inside it, so a
# reader knows whether to open it.
EVIDENCE_FOLD_SUMMARY = "Captured output"


def _shell_line(argv: Sequence[str]) -> str:
    """The command as somebody would type it.

    It rendered as a JSON array, so a command holding a pipe, a backslash or a `>` arrived
    as `\\\\|` and `&gt;=` -- unreadable, and not a thing anyone could paste. `shlex.quote`
    quotes only what a shell would need quoted, which is the same rule a person follows.
    """
    # A newline INSIDE an argument cannot survive: a code span is one line, and collapsing
    # the whitespace turns `python3 -c 'print(1)\nprint(2)'` into two statements on one
    # line, which is a syntax error for anyone who copies it. The shell line is offered for
    # the commands it can represent, and the rest keep the form that preserves them.
    if any("\n" in part or "\r" in part for part in argv):
        return ""
    return " ".join(shlex.quote(part) for part in argv)


def _as_code(text: str) -> str:
    """Worker text in a code span, or as prose where a span cannot hold it.

    The content is escaped like every other worker string on an open line, and a code span
    is NOT an exception to that. :func:`render_html` converts a span with
    ``<code>{content}</code>`` and undoes only the writer's BACKSLASH escaping -- HTML's is
    kept, which is what lets non-fenced text pass straight through. So a span whose content
    skipped the escape puts a raw ``<!--`` into both documents, and worker text that can
    open an HTML comment can close the page around everything after it.

    A BACKTICK cannot be held either way: the span closes on it and the rest becomes loose
    text, so that case renders as prose.
    """
    flat = _one_line(text)
    return flat if "`" in flat else f"`{flat}`"


def _render_evidence(evidence: dict, seen: set[str] | None = None) -> list[str]:
    """One run, under the defect it settles.

    ``seen`` is the commands this DEFECT has already printed. Verifiers run one broad
    search per batch and attach it to every verdict in it, so the same grep and the same
    output appeared verbatim under five defects and three times under one -- which is what
    made the collection read as a pile of unrelated greps rather than as evidence.
    """
    command = _shell_line(evidence["argv"])
    kind = evidence.get(RUN_KIND_KEY)
    # The WHOLE run, not its command. Keyed on the command alone, one command run twice --
    # before a fix and after it, the ordinary way to show the fix works -- collapsed into
    # one, and the second run's directory, exit status and output disappeared.
    identity = json.dumps([evidence["argv"], evidence["cwd"], evidence["exit_status"],
                           evidence["output"], evidence["truncated"]], sort_keys=True)
    if seen is not None and identity in seen:
        return [f"- Evidence{f' ({kind})' if kind else ''}: "
                f"{_one_line(evidence['shows'])} Same run as above.\n"]
    if seen is not None:
        seen.add(identity)
    output, cut = _bounded_output(evidence["output"])
    note = "output truncated by the verifier" if evidence["truncated"] else "output complete"
    if cut:
        note += f"; the report keeps the last {EVIDENCE_OUTPUT_BOUND} bytes"
    # argv and cwd are JSON strings: one line each, whatever they hold.
    # argv and cwd are the verifier's, and they land in a prefix the fence below cannot
    # reach, so they go through _one_line like any other worker string on an open line.
    # The kind rides in the label rather than the note, because it is what the line is
    # about: a reader scanning evidence blocks is deciding which ones show the code doing
    # something wrong, and that is this word and not the exit status.
    # What the run establishes, first and in the verifier's own words. A command and its
    # output never say why they were run, and one broad search attached to five defects has
    # no single reason a reader can infer -- so this leads the block.
    #
    # The exit status and the truncation TRAIL it, in a parenthetical. They are the
    # supervisor keeping records; a fixer reads the sentence and the command first, and
    # leading with the bookkeeping put two numbers in front of both.
    where = _one_line(evidence["cwd"])
    place = "" if where in ("", ".") else f", in {where}"
    shown = _as_code(command) if command else f"argv {_one_line(json.dumps(evidence['argv']))}"
    out = [f"- Evidence{f' ({kind})' if kind else ''}: "
           f"{_one_line(evidence['shows'])} "
           f"{shown}{place} "
           f"(exit {evidence['exit_status']}, {note})\n"]
    if output:
        # Labelled, so the page can fold it and the snippets stay open. It is a true label
        # rather than a presentation flag smuggled into the prose -- the block IS captured
        # console output -- and it is in both documents, so neither carries a fact the
        # other lacks.
        out += _fenced(output.splitlines(), lang=EVIDENCE_FENCE_LANG)
    return out


def _one_line(text: str) -> str:
    """Worker text where exactly one line can go — a heading, a table cell, the label a
    fenced block hangs off.

    Two things are neutralized, because a heading has no fence to hide behind. Every run of
    whitespace collapses to one space, so a newline followed by ``## Coverage`` cannot open
    a section and a newline inside a cell cannot start a row. Then ``&``, ``<`` and ``>``
    are escaped, so a tag cannot do the same thing: Markdown passes raw HTML through
    untouched, and a ``consequence`` reading ``</h4><h2>Coverage</h2>`` would otherwise
    close this document's heading and open a section of the worker's own. :func:`_item`
    refuses to inline a string holding ``<`` for exactly that reason; this is the same rule
    where fencing is not available.
    """
    return _entities(_collapse(text))


def _collapse(text: str) -> str:
    """The whitespace half of :func:`_one_line`, for a caller that needs one line and does
    its own escaping."""
    return " ".join(text.split())


# Where a line OPENS A BLOCK, and the one character that has to stop for it to be a
# paragraph instead. These are deliberately not in :data:`_MD_ACTIVE`: a `-` mid-sentence is
# a hyphen and a `#` is part of somebody's issue number, so escaping every one of them would
# put backslashes through ordinary prose.
#
# Each branch ends ON the character to neutralize and checks the rest with a lookahead, so
# the match's last character is the one the caller rewrites. Each is also the WHOLE rule and
# not the first letter of it — `#5 in the queue` is not a heading, `--force is ignored` is
# not a break, and neither is escaped. Markdown's other openers need no branch here because
# `_one_line` has already escaped them: `*`, `_` and `` ` `` behind a backslash, `<` and `>`
# as entities.
_MD_BLOCK_LEAD = re.compile(
    r"^(?:"
    r"#(?=#{0,5}(?: |$))"               # a heading — up to six hashes, then a space or the end
    r"|[-+](?= |$)"                     # a list item
    r"|-(?= ?(?:- ?){2,}$)"             # a thematic break, which is dashes and nothing else
    r"|\d{1,9}[.)](?= |$)"              # a numbered list item
    r"|~(?=~~)"                         # a fenced block
    r")")


def _paragraph(text: str) -> str:
    """Worker text as a paragraph of its own — one line, with nothing of the engine's in
    front of it.

    :func:`_one_line` neutralizes what changes meaning INSIDE a line, which is the whole of
    the hazard for a heading, a table cell or an `_item` prefix: each of those has the
    engine's own text in front of the worker's. A line that BEGINS with a worker's text has
    one more, and it is the larger one. A consequence of ``# D2. Stolen`` is a heading the
    engine did not write: the scan that looks for a defect's entry line stops at it, that
    defect loses its anchor, and the forged heading takes the id the next defect was going
    to carry — leaving two elements on the page with one id and every link to it resolving
    to whichever a viewer picks. ``- D3 — invented verdict`` does the same to a refuted
    defect, whose only claim on its id is a list item.

    The character is written as the character reference for itself. A backslash escape
    would need :func:`_unescape` to know about it, and a reader of ``report.md`` would see
    the backslash; an entity is what this prose already carries for ``&``, ``<`` and ``>``
    — Markdown does not read it as markup and the page renders it as what the worker wrote.
    """
    return _no_block_lead(_one_line(text))


def _no_block_lead(line: str) -> str:
    """One already-escaped line that cannot open a block.

    Split from :func:`_paragraph` because the other line a worker's text begins — the
    decode under a defect's metadata — is COMPOSED from pieces that are each escaped
    already. Running :func:`_one_line` over the finished line again would turn the ``&`` of
    an entity it just wrote into ``&amp;``, so the neutralization has to be available
    without the escaping that normally precedes it.
    """
    lead = _MD_BLOCK_LEAD.match(line)
    if lead is None:
        return line
    cut = lead.end() - 1
    return f"{line[:cut]}&#{ord(line[cut])};{line[cut + 1:]}"


# Markdown's own active characters, escaped wherever text the engine did not write enters
# the prose. Without this a file named ``a**b**c.py`` renders as ``abc.py`` — in the page AND
# in any Markdown viewer — and a reproduction's ``echo `whoami``` turns into a code span,
# changing the command somebody copies out of the report. The backslash goes first: it is the
# escape character, so escaping it after anything else would escape the escapes.
# Every character that changes what Markdown renders when it appears inside a line. The
# underscore is here because `__init__.py` otherwise displays as `init.py`, and the bracket
# because a command like `grep '[a-z](foo)' src.txt` forms a link out of ordinary regex —
# no accident of prose required. Both cost a backslash on screen in the prose; a file name
# or a command that changes on its way to the reader costs more than that.
_MD_ACTIVE = "\\`*_[|"


def _html_entities(text: str) -> str:
    """HTML's three characters and nothing else, for fenced content — which the prose keeps
    raw, so the conversion is the first and only place it is escaped."""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _entities(text: str) -> str:
    """Text the engine did not write, made safe for the prose.

    Two jobs, one boundary. HTML's characters are escaped because the page is converted from
    this prose and passes non-fenced text through untouched. Markdown's are escaped because
    the prose is a document in its own right and a worker's asterisk is not emphasis.

    **Every character of the report outside a fence goes through this**, which is what the
    conversion rests on; a test holds that invariant over a rendered report. What comes out
    is decoded once, on the way into the page.
    """
    for char in _MD_ACTIVE:
        text = text.replace(char, "\\" + char)
    return _html_entities(text)


def _term_cell(text: str) -> str:
    """One table cell holding a TERM from the engine's own vocabulary — a severity, a
    status, a fix size, a corroboration breadth.

    A column reading `blocker` / `established` / `one model` is a column of fragments from
    a sentence nobody finished, so a term starts with a capital here. The transformation is
    applied only to the closed vocabularies this program defines, and never to a string a
    worker wrote: deciding it from the SHAPE of the text was tried and it changes what
    somebody said. `a & b are never both set.` is a rationale opening on an identifier and
    it came out as `A & b`, and a legend short name can be a bare directory, so `engine/sub`
    decoded to `Sub` — a directory no disk holds.

    An id is left exactly as spelled even here, because the vocabularies are not the only
    thing that reaches a term column: a raiser's proposed severity is one of them, and an
    unknown value renders as itself rather than being rewritten.
    """
    if not text or not text[:1].islower():
        return _one_line(text)
    head = text.split(" ", 1)[0]
    if any(ch in head for ch in "./(\\`") or any(ch.isdigit() for ch in head):
        return _one_line(text)
    return _one_line(text[0].upper() + text[1:])


def _cell(text: str) -> str:
    """One table cell, spelled as it was given. :func:`_one_line` already escaped the pipe
    along with the rest of Markdown's active characters, so there is nothing left to do
    here and doing it again would leave the backslashes on show.

    This is the DEFAULT, and a cell is only routed elsewhere where the caller knows what
    kind of value the column holds. Worker prose, file names, short names and locations all
    come through here untouched, which is what keeps the report quoting what it was given.
    """
    return _one_line(text)


_WHITESPACE_ESCAPES = {" ": "\\s", "\t": "\\t", "\n": "\\n", "\r": "\\r",
                       "\v": "\\v", "\f": "\\f"}


def _spelled(path: str) -> str:
    """A path written so that no two different paths can read as one.

    The legend's whole job is to decode a short name, so the spelling beside it has to
    survive the page. :func:`_collapse` turns every run of whitespace into one space and
    drops it at the ends, which two paths differing only in that run reach as the same
    string — and the reader is then told that ``file.py`` and ``file.py (1)`` are both
    ``a/ file.py``, which is the collision the short name was lengthened to avoid, moved
    one column to the right.

    A path that survives collapse unchanged is printed AS IT IS, which is every ordinary
    path including one holding single spaces. Only a path that would not survive is
    written with its whitespace escaped, so the escapes appear where they are carrying
    meaning and nowhere else.
    """
    if _collapse(path) == path:
        return path
    return "".join(_WHITESPACE_ESCAPES.get(char, char) for char in path)


def _path_suffixes(path: str) -> list[str]:
    """Every spelling of ``path`` from its base name up to the whole thing, shortest first
    — the order a name is chosen in."""
    parts = path.split("/")
    return ["/".join(parts[-depth:]) for depth in range(1, len(parts) + 1)]


def _short_names(paths: Sequence[str]) -> dict[str, str]:
    """A short name for every path the report prints, and no two alike ON THE PAGE.

    Each path gets the shortest suffix of directory components that no other path in the
    document shares, chosen for that path ALONE. `z/b/c.py` beside `x/a/c.py` and
    `y/a/c.py` prints as `b/c.py`: one directory already tells it apart, and lengthening it
    to match what its neighbours needed is a directory a reader reads for nothing. A
    collision rendered as one name is two defects that look like one location.

    The comparison is over the RENDERED name, not the raw path, because that is what a
    reader sees. :func:`_one_line` collapses every run of whitespace, so `left/ file.py`
    and `right/file.py` have different base names — nothing here would look twice at them —
    and both reach the page as `file.py`, with the legend then decoding one visible name to
    two files.

    A path no suffix singles out falls back to its full spelling, which cannot collide with
    a name chosen above it: a shorter name was only taken where exactly one path bore it.
    `b.py` beside `a/b.py` is that case — `b.py` names two paths, so `a/b.py` is spelled in
    full and `b.py` is then free to be itself.

    Where even two full paths render alike there is no longer spelling left to try, and the
    second one carries a numbered discriminator instead. It is not a path and does not
    pretend to be one; the row beside it in the legend carries the spelling. A document
    showing one name for two files is worse than one showing a name no filesystem would
    give.
    """
    unique = sorted(set(paths))
    # How many paths each candidate name would stand for. Counted over the whole document
    # at once, so a name is rejected by a path in another part of it just as by a neighbour.
    bearers: dict[str, int] = {}
    for path in unique:
        for suffix in _path_suffixes(path):
            rendered = _one_line(suffix)
            bearers[rendered] = bearers.get(rendered, 0) + 1
    out: dict[str, str] = {}
    in_full: list[str] = []
    for path in unique:
        for suffix in _path_suffixes(path):
            if bearers[_one_line(suffix)] == 1:
                out[path] = suffix
                break
        else:
            in_full.append(path)
    taken = {_one_line(name) for name in out.values()}
    for path in in_full:
        name, index = path, 1
        # The counter keeps climbing past a name already in the document, so a tree that
        # really does hold a file called `a/file.py (1)` cannot collide with the marker.
        while _one_line(name) in taken:
            name, index = f"{path} ({index})", index + 1
        out[path] = name
        taken.add(_one_line(name))
    return out


class PathNames:
    """The short name every path in the document is printed under, and the legend that
    decodes them.

    Built once, from the paths the report is about to print, and consulted wherever one
    renders. That is what makes ONE spelling of a file possible: shortening at each call
    site instead would let two of them disagree about the same file, which is the thing
    section 9 forbids whichever spelling is chosen.
    """

    def __init__(self, paths: Sequence[str]) -> None:
        self._names = _short_names(paths)

    def short(self, path: str) -> str:
        """The name this path is printed under. One the collector never gathered is
        printed in FULL rather than guessed at: a name invented here would have no legend
        row to decode it, and a full path standing beside its own short form is visible
        both to a reader and to the completeness test over a rendered report."""
        return self._names.get(path, path)

    def rows(self) -> list[tuple[str, str]]:
        """The legend, short name first — the order a reader looks one up in."""
        return sorted((short, full) for full, short in self._names.items())


def _at(record: dict, names: PathNames) -> str:
    """``file:line`` or ``file:line-line``, the one spelling of a location the tables use.

    The file is its SHORT name, and the path legend at the top of the report decodes every
    one of them. One spelling document-wide is the rule and the short name is that
    spelling: a full path in every index row is most of the row, and the part a reader is
    looking for is the base name at the end of it. A path is relative to the
    REVIEWED root here and in every other list but one: :func:`_reachability` names
    directories above that root, which have no spelling relative to it, and says so where
    the two roots differ.

    The string is RAW, and every caller escapes it the way its own document needs — once.
    A file named ``<h2>Coverage.py`` is a valid POSIX name and passes every check the
    inventory makes, so this is not the engine's string and cannot go unescaped onto an open
    line or into an ``_item`` prefix, neither of which a fence reaches. Escaping it here
    instead would escape it twice wherever a caller escapes properly.
    """
    return f"{names.short(record['file'])}:{_lines(record)}"


def _lines(record: dict) -> str:
    """The line or line range half of a location, which both spellings of it share."""
    return (str(record["line_start"]) if record["line_start"] == record["line_end"]
            else f"{record['line_start']}-{record['line_end']}")


def _in_full(record: dict, commit: dict) -> str:
    """The same location spelled out, with the tree it was read from — the line that sits
    under a defect's metadata.

    A short name is only safe where a reader can get back to the path, and the legend is
    at the top of a long document. This is the decode in place: the one defect's own full
    path, beside the commit its snippet came from. The legend says the same thing for
    every name at once; these two are the only places the RENDERER spells a path in full,
    and each exists to decode a name rather than to state a location. A sentence quoted from
    a worker is the exception the renderer does not control: it is reproduced as it was
    written, paths and all, because shortening it would mean editing a quotation.

    The path is spelled the way the legend spells it — :func:`_spelled` — so the two
    cannot read as different files, and escaped here because it lands on an open line.

    The finished line goes through :func:`_no_block_lead` as well. A file name may begin
    with a list marker — ``- dir/core.py`` is a path a filesystem will give you — and this
    line is indented under the metadata bullet, so such a name opens a NESTED list item
    instead: the page then shows the path with its first characters missing, and the scan
    that finds this line to exempt it from the one-spelling rule does not recognize it at
    all. Escaping the pieces is not enough when the composition is what opens the block.
    """
    return _no_block_lead(
        f"{_cell(_spelled(record['file']))}:{_lines(record)} — {_commit_words(commit, True)}")


def _breadth(cluster: dict, rung: str) -> str:
    """Corroboration in words, in the vocabulary the rung permits: two models where two
    runtimes ran, two contexts of one model where one did."""
    one, both = RUNG_BREADTH[rung]
    if cluster["axis_a"] == AXIS_A_BOTH:
        return f"{both}, {_plural(len(cluster['members']), 'report')}"
    return one


def _settles(cluster: dict) -> str:
    """The heading of the group an unresolved defect sits under, and it sits under exactly
    one.

    Three cases, not two. Members that agree name their reason. Members that disagree have
    no single settling question and get the group that says so, rather than being filed
    twice or filed under a question they only half answer. And a defect whose verifier
    never answered has no reason at all — a different thing again, and the one a reader
    can act on by re-running the unit rather than by making a decision.
    """
    reasons = cluster["unresolved_reasons"]
    if reasons == [None]:
        return UNKNOWN_SETTLING
    if len(reasons) != 1:
        return MIXED_SETTLING
    return UNRESOLVED_GROUPS[reasons[0]]


def _settling(reason: str) -> str:
    """One settling reason as a phrase in a sentence, not as the engine's enum key.

    `needs_a_file_outside_the_scope` is a value this program matches on; the reader who has
    to act on it never chose that spelling and cannot be expected to parse it. The words are
    the section headings' own, so a member line and the heading it sits under say one thing
    in one vocabulary — lower-cased, because this one lands after a colon and mid-sentence.

    An unknown value renders as itself rather than raising: the settling reason is bounded
    by the verifier schema, and a report that refuses to print because a new term was added
    there is a worse failure than one that prints the term.
    """
    named = UNRESOLVED_GROUPS.get(reason)
    return reason if named is None else named[0].lower() + named[1:]


def _id_rank(cluster_id: str) -> tuple[int, str]:
    """A defect id ordered by its NUMBER rather than by its spelling.

    ``D<n>`` carries no zero padding, so a plain string sort reads ``D10`` as sitting
    between ``D1`` and ``D2``. Two orderings break a tie on the id — the ranked index and
    the largest-clusters list — and a reader scanning either reads the ids as a numbered
    sequence. The fallback keeps the sort total for anything not of that shape, so a
    hand-built structure orders rather than raising.
    """
    digits = cluster_id[len(CLUSTER_ID_PREFIX):]
    return (int(digits) if digits.isdigit() else -1, cluster_id)


def _defect_order(cluster: dict) -> tuple:
    """Severity first, then the cheapest fix within it, then file and line. Section 9's
    ordering, computed and never triaged — an agent asked to rank these would make the
    report a judgment the run cannot reproduce.

    **Status is not a key here, because nothing ranked by this is refuted.** A list holding
    dismissed claims beside live ones needs status as its first key inside a severity, or a
    refuted blocker with a one-line fix outranks an established blocker with a large one and
    work nobody will do sits at the top of the list of work to do. Keeping refuted defects
    out of the ranked views dissolves that conflict instead of tuning it, and leaves one rule
    a reader can hold: most severe first, cheapest first within that.
    """
    return (_SEVERITY_RANK[cluster["severity"]],
            _FIX_RANK[cluster["fix_size"]], cluster["file"], cluster["line_start"],
            _id_rank(cluster["id"]))


def _is_work(cluster: dict) -> bool:
    """Whether this defect is something to do. A refuted claim is not: it was checked and
    dismissed, and it belongs where a reader can audit that judgment rather than in the
    views that answer "what do I fix"."""
    return cluster["status"] != DEFECT_REFUTED


def _commit_words(commit: dict, abbrev: bool = False) -> str:
    """Where a snippet came from, in words the record can support. A tree that is not a
    repository, or is dirty, says so: a line number against an unnamed tree is a line
    number against nothing, and a clean-looking sha beside a dirty tree is worse than no
    sha at all.

    ``abbrev`` cuts the sha to the first :data:`COMMIT_ABBREV` characters, which is what
    goes beside a defect and beside a snippet — every entry in the document carries one,
    and forty characters of hex repeated a hundred times is forty characters nobody reads
    a hundred times. The whole sha is stated once, under "How this ran", which is where a
    reader who needs to check out the tree looks.
    """
    state = commit.get("state")
    if state != "commit":
        return {"unborn": "a repository with no commit yet",
                "not-a-repository": "a tree that is not a repository",
                }.get(state, "a tree the engine could not ask git about")
    sha = commit.get("commit", "")
    if abbrev:
        sha = sha[:COMMIT_ABBREV]
    dirty = commit.get("dirty")
    if dirty is None:
        return f"commit {sha}, cleanliness unknown"
    return f"commit {sha}{', with uncommitted changes' if dirty else ''}"


def _render_snippet(snippet: dict, commit: dict, path: str | None = None) -> list[str]:
    """The cited lines, numbered, from the pinned tree. Fenced like any other block the
    engine did not write, and the fence is longer than any run of backticks the source
    holds — source is exactly the kind of text that carries them."""
    width = len(str(snippet["first_line"] + len(snippet["lines"]) - 1))
    body = [f"{snippet['first_line'] + n:>{width}} | {line}"
            for n, line in enumerate(snippet["lines"])]
    if snippet["truncated"]:
        body.append(SNIPPET_CUT_MARKER)
    # No label at all. The line directly above the block already gives the full path and
    # the commit; a "Source:" bullet between them and the code says nothing the reader has
    # not just read, and costs a line in every defect on the page.
    return _fenced(body, lang=_fence_language(path))


def _where(path: str, lo: int, hi: int, names: PathNames, snippet: dict | None = None) -> str:
    """One snippet block's own location, spelled the way every other location is.

    A defect's metadata names ONE site, and a defect can hold reports of two. The block
    that survives the fold is not always the one that site names, so the block says which
    lines it actually is rather than borrowing the line above it.

    **And it says which of those lines the finding is about**, where the two differ. A block
    is padded by :data:`SNIPPET_PAD` so that a one-line citation has something to sit in, so a
    defect located at `483-485` prints source headed `481-487` — two numbers that disagree,
    directly under each other, with nothing on the page explaining why. The pad is in the
    record already as ``cites_from``/``cites_to``; this is only the renderer saying so.
    """
    spelled = f"{names.short(path)}:{lo}" if lo == hi else f"{names.short(path)}:{lo}-{hi}"
    if snippet is None:
        return spelled
    first, last = snippet.get("cites_from"), snippet.get("cites_to")
    if first is None or last is None or (first, last) == (lo, hi):
        return spelled
    cites = first if first == last else f"{first}-{last}"
    return f"{spelled} — {SNIPPET_PAD} lines of context either side; the finding cites {cites}"


def _agrees(member: str, defect: str) -> bool:
    """Whether a member's own verdict is the one its defect is filed under.

    The two vocabularies are different on purpose — a candidate is `reproduced` or
    `confirmed_by_reading`, a defect is `established` — so they cannot be compared
    directly, and comparing them as strings makes every member look like a disagreement.
    """
    if defect == DEFECT_ESTABLISHED:
        return member in ESTABLISHED_STATUSES
    return member == defect


def _render_member(record: dict, commit: dict, names: PathNames,
                   moved: bool = False, verification: bool = True,
                   settled: str | None = None,
                   aggregate: str = DEFECT_ESTABLISHED,
                   ran: set[str] | None = None) -> tuple[list[str], list[str]]:
    """One candidate inside its defect, carrying no run machinery at all.

    Returns its CHECK LINES and its body separately. The defect gathers every member's
    check lines under one `Checks.` label, so each is a row in a labelled list rather than
    an unlabelled bullet -- which is what the old shape produced, and what a reader could
    not identify: a lone `by reading` under nothing, repeating the header field above it.

    Section 10: candidate ids, lane letters, unit ids and lens names are audit data and
    belong in the provenance appendix, not in the text somebody reads while fixing this.
    What is left is what a fixer uses — what fails, what to do about it, what was concluded
    and what ran — and every string of it is a worker's, so it goes through :func:`_item`.

    ``verification`` says this member's outcome is worth stating: either the run settled
    its defects in more than one way, or THIS defect's reports did not all come back the
    same. Where neither holds, the outcome is the same four words on every line and the
    document has already said them.

    ``moved`` says the defect carries the synthesis round's account of itself, so the three
    fields that account was written FROM — the reader's failure text, the reader's
    direction and the checker's rationale — are in the provenance appendix instead of
    here. Printing both would put one fact on the page twice in two voices, one of them
    checked and one of them not, and the narrative is the one a reader would believe.
    Nothing else moves: the snippet, the evidence and the quote warning are what the claim
    RESTS on rather than prose about it, and the warning in particular says the cited lines
    may not be the lines the finding is about.

    ``settled`` is the reason a heading above this member already gives — the unresolved
    section groups by it — so that member's own line for it is not written twice. A member
    whose reason DIFFERS from the heading's still writes one, which is the case the
    suppression must not swallow: a defect two checkers could not settle for two different
    reasons is grouped under neither.
    """
    # The location rides with the member only where the defect holds more than one. It is
    # what tells two reports of one site apart — without it, two members whose prose matches
    # and whose line ranges do not render identically, and the dedupe below drops one range
    # out of the document entirely.
    where = f" ({_one_line(_at(record, names))})" if record["siblings"] else ""
    # With the `Reported` line gone the verdict line is the member's first, so the location
    # rides with THAT instead — as a lead rather than a trailing parenthesis, which would
    # sit beside the axis's own and read as part of it.
    # The location leads EVERY check line, whether the defect merges one report or five.
    # Printed only for siblings before, which is backwards for a reader: one report is the
    # common case, and the line then degenerated to a bare `by reading` -- a fragment
    # under no label, saying what the header field above it had just said.
    at = f"{_one_line(_at(record, names))} · "
    out: list[str] = [] if moved else _item(f"- Reported{where}: ", record["failure"])
    if record["quote_check"] == QUOTE_DIFFERS:
        out.append(f"- **{QUOTE_FLAG}**\n")
    elif record["quote_check"] == QUOTE_UNREADABLE:
        out.append(f"- {QUOTE_UNCHECKED}\n")
    if not moved and record["direction"].strip():
        out += _item("- Direction: ", record["direction"])
    # A PROPOSED reproduction is a reader's suggestion for how to check a claim. Once
    # something actually ran, the evidence below is what the claim rests on and the
    # suggestion is a superseded guess — it goes to the provenance appendix, where the rest
    # of what each unit said lives. Where nothing ran it is the most actionable line here:
    # it is how the next person settles the question, so it stays.
    if record["evidence"] is None:
        for raiser in record["raised_by"]:
            if raiser["reproduction"] is not None:
                repro = raiser["reproduction"]
                # Nested under the `Reported` line it elaborates — and at the MARGIN once
                # that line has moved to the appendix, because the bullet it would nest
                # under is then the defect's `Fix.` or `Related.`, and a reader takes an
                # indented bullet as belonging to the one above it.
                indent = "- " if moved else "  - "
                out += _item(f"{indent}Proposed reproduction, not run: "
                             f"argv {_one_line(json.dumps(repro['argv']))}, "
                             f"cwd {_one_line(repro['cwd'])}, expect: ", repro["expect"])
    # The snippet is NOT rendered here. A defect's evidence belongs under the location that
    # names it, above the prose about it, and de-duplicated across the reports that cite
    # overlapping ranges -- all of which is the defect's to do, not one member's.
    # Everything below goes to `checks` rather than to `out`: the verdict on each report,
    # and the notes that belong under it. A defect gathers them under one `Checks.` label
    # so a line saying `unresolved` is a row in a table of checks rather than a fragment.
    checks: list[str] = []
    if not record["answered"]:
        # Section 10: the unit that failed, and why, is coverage's to name. Here it is one
        # plain sentence, because a person reading this to fix something cannot act on a
        # unit id and should not have to skip over one.
        # `unresolved (unresolved)` said the engine's enum twice and then said the same
        # fact a third time in the plain sentence that follows it — the same doubling the
        # verdict line below dropped, in the branch nobody looked at. **This one has never
        # been seen render**: it needs a verification unit that failed or never landed, and
        # neither run available when it was changed had an unanswered candidate. It is
        # fixed for consistency with the line below and not on evidence of harm, which is
        # the honest thing to say about it.
        checks.append(f"  - {at}no verdict came back; the unit that would have answered "
                      f"it is named under Coverage\n")
    else:
        # `confirmed_by_reading (by reading)` says one thing twice, once in the engine's own
        # enum. The enum is not the reader's vocabulary, and on a run where nothing could be
        # executed the phrase is the same on every report -- which the top of the document
        # states once. So the line carries the status only where it varies, and where it
        # does, in words rather than in the enum.
        # The member's own outcome, and it is TWO facts. `axis_b` is the verification
        # METHOD and never the verdict -- a refuted report and an upheld one can both be
        # `by reading` -- so a line carrying the axis alone cannot say a check dismissed
        # this report, and an established defect then hides the refuted sibling describing
        # the same site. The status leads where this member does not agree with what the
        # section says about the defect; the axis follows where the axis says something.
        # Deduplicated, because an unresolved member's axis IS its status and printing both
        # is the doubling this line exists without.
        told: list[str] = []
        for part in (None if _agrees(record["status"], aggregate) else record["status"],
                     _verification_words(record["axis_b"], record["evidence"])
                     if verification else None):
            if part and part not in told:
                told.append(part)
        # Suppressing the outcome left a bare location introducing nothing, so the line was
        # dropped entirely -- and with it the only per-report row the defect had. Under a
        # `Checks.` label the method alone is a legible row rather than a fragment, so the
        # fallback names it instead of rendering nothing.
        if not told:
            told = [_verification_words(record["axis_b"], record["evidence"])]
        outcome = _one_line(", ".join(told))
        # The file an unresolved verdict named, on the line that says it is unresolved.
        # The verifier already named it -- it is where the "one more file would settle"
        # table comes from -- and this is where somebody deciding whether to chase it is
        # reading, rather than an appendix table three sections down.
        wants = [_one_line(path) for path in record.get("needs_files", ())]
        if wants:
            outcome += f" · would be settled by {', '.join(wants)}"
        if moved:
            checks.append(f"  - {at}{outcome}\n")
        else:
            checks += _item(f"  - {at}{outcome} — ", record["rationale"])
        # Where the member's own facts hang. They NEST under the verdict line while there is
        # one; with it gone they move to the margin and take the location as a lead, because
        # an indented bullet reads as belonging to the bullet above it and the bullet above
        # it is then the defect's `Related.` or `Fix.` — which is how a severity revision
        # came to render as a sub-point of a cross-reference list, and how two members'
        # settling reasons came to stack under one another with nothing saying whose was
        # whose. It is the same correction the proposed reproduction below already carries.
        # One fixed depth, under the check line this note belongs to. Every check line
        # renders, so there is always a line above for a note to hang from, which is what
        # makes a single indent correct. Choosing the depth from whether a verdict line
        # survived puts a severity revision under the defect's cross-reference list
        # instead, and stacks two reports' settling reasons with nothing saying whose is
        # whose.
        indent, lead = "    - ", ""
        if record["revision"] is not None:
            checks += _item(f"{indent}{lead}Severity revised to {record['revision']['severity']} "
                            f"from {record['severity_proposed']}: ",
                            record["revision"]["rationale"])
        # The reason in the words the section headings use, not the engine's enum:
        # `needs_a_file_outside_the_scope` is a key this program matches on and not
        # anybody's English, and it is the reader who has to act on what it says.
        if record["unresolved_reason"] not in (None, settled):
            checks.append(f"{indent}{lead}What would settle it: "
                          f"{_settling(record['unresolved_reason'])}\n")
        # Promoted to a named prose part of the defect where the round wrote one, so the
        # three parts a reader works from sit together; nested under the verdict otherwise.
        if not moved and record["test_first"] is not None:
            checks += _item(f"{indent}Test that should fail first: ", record["test_first"])
        if record["evidence"] is not None:
            out += _render_evidence(record["evidence"], ran)
    return checks, out


# How long a one-line label may be before it stops being one. A consequence is written to be
# read as a sentence in the body; in a table cell the same 250 characters wrap to four lines
# and take the column beside them with them.
#
# Measured against 99 real headings rather than chosen: at 110 only 43% of labels came out
# as a complete thought and the rest trailed off mid-clause; at 150, 93% do. A table where
# almost every row ends in an ellipsis is one a reader stops trusting, and the width saved
# by the tighter bound bought nothing but that.
_LABEL_LIMIT = 150
# A sentence end: a full stop, question mark or exclamation, then space, then a capital.
# Bounded that way so `engine/core.py. ` ends a sentence and `cf. the loop` does not.
_SENTENCE_END = re.compile(r"(?<=[.?!])\s+(?=[A-Z0-9\"'`(])")
# Where a long sentence can be cut and still read as a finished one. A consequence names an
# effect and then explains it — "…is later reversed, because the retry path…" — and the
# effect alone is the label. Measured on the same 99: NONE of them had a second sentence, so
# the sentence rule above never fired on real input and truncation was doing all the work.
_CLAUSE_BREAK = re.compile(
    r",\s+(?:because|so|which|and|but|since|when|where|after|before|leaving|causing|"
    r"meaning|then|while|until)\b|;\s+|\s+—\s+")


def _short(text: str) -> str:
    """A one-line label from a consequence written to be a sentence.

    The full sentence is the win — it is what makes a heading say what a person
    experiences rather than what the code does — and it is kept, in the body heading,
    which is where somebody reads it. What a table cell and a list entry need is something
    that fits on the line: the first sentence, and a second only where the first is too
    short to locate anything on its own. Section 9's preference is one.

    Truncation is the last resort and is marked, because a label silently cut mid-clause
    reads as a complete thought that says the wrong thing.
    """
    flat = _collapse(text)
    if len(flat) <= _LABEL_LIMIT and not _SENTENCE_END.search(flat):
        return flat
    sentences = _SENTENCE_END.split(flat)
    label = sentences[0]
    # A very short first sentence — "The totals are wrong." — locates nothing by itself, so
    # the second comes with it where both still fit. Never a third.
    if len(sentences) > 1 and len(label) < _LABEL_LIMIT // 2:
        # Taken even when the pair overruns and has to be cut. "The totals are wrong."
        # locates nothing; "The totals are wrong. The monthly rollup double-counts any
        # invoice that was amended…" locates it, and the cut is marked.
        label = f"{label} {sentences[1]}"
    if len(label) <= _LABEL_LIMIT:
        return label
    # A clause break inside the budget gives a label that reads as finished. The floor stops
    # it taking an opening fragment — "When the gateway drops." says less than a cut sentence
    # would — so a break in the first third is ignored and the truncation below runs instead.
    breaks = [m.start() for m in _CLAUSE_BREAK.finditer(label)
              if _LABEL_LIMIT // 3 <= m.start() <= _LABEL_LIMIT]
    if breaks:
        return label[:breaks[-1]].rstrip(" ,;:—") + "."
    # Last resort, and marked: a label silently cut mid-clause reads as a complete thought
    # that says the wrong thing.
    cut = label[:_LABEL_LIMIT].rsplit(" ", 1)[0].rstrip(" ,;:")
    return f"{cut}…"


def _unsettled(members: Sequence[dict], names: PathNames) -> list[str]:
    """What stopped each checker from settling this defect, in the checker's own words.

    The verifier schema defines an unresolved rationale as "what was tried and what stopped
    it", and for the commonest reason — the answer lives in a file this review did not cover
    — it requires the missing file to be NAMED there. That sentence is the whole content of
    an unresolved defect: it is what turns "somebody should look at this" into "read
    EntityLockService.unlock and this is decided either way". Nothing else in the record
    carries it; the settling reason is one of four fixed terms and says only which KIND of
    thing is missing.

    Only members a checker actually answered. A member whose unit never landed has an
    engine sentence in this field naming the unit, which is not a checker's account of
    anything — :func:`_render_member` says that in plain words on the member's own line, and
    printing the engine's sentence here as though a reader could act on it would be the
    report's own voice pretending to be a verdict. Members that were REFUTED are left out
    for the same reason in reverse: their rationale says why the claim is wrong, which is a
    settled answer and not an open question.

    Deduplicated on the rendered text, like the tests and the member blocks: two lanes that
    could not settle one site for one reason routinely write one account of it.
    """
    out: list[str] = []
    for record in members:
        if not record["answered"] or record["status"] != "unresolved":
            continue
        rationale = record["rationale"]
        if not rationale or not rationale.strip():
            continue
        # The location only where the defect holds more than one report, and for the reason
        # the member blocks carry it: two accounts of two different sites read as one
        # contradictory account of a single site without it.
        where = f"({_one_line(_at(record, names))}) " if record["siblings"] else ""
        rendered = "".join(_item(f"- **{UNSETTLED_LABEL}** {where}", rationale))
        if rendered not in out:
            out.append(rendered)
    return out


def _render_defect(cluster: dict, members: Sequence[dict], rung: str,
                   commit: dict, names: PathNames, level: int = 3,
                   verification: bool = True, unsettled: bool = False,
                   corrected: bool = False) -> list[str]:
    """One defect in full: its id and a one-line label as the heading, the whole
    consequence under it, one line of metadata, and every member's own verdict — an
    established defect never hides the refuted or unresolved sibling that describes the
    same site.

    **The heading is the label, not the sentence.** A consequence is written to be read as
    a sentence and the good ones run past 200 characters; as a heading that is a line
    nobody scans and an entry in a contents list nobody reads. The id leads it, so a
    reader arriving from the index or from a cross-reference lands on the name they
    followed. Where the label had to cut the sentence, the whole of it is the line under
    the heading, which is where section 8 wanted it read — and where it did not, that line
    would be the same sentence a second time and is not written.

    **The metadata is one line.** Severity, corroboration and location were three bullets
    saying in three lines what fits on one; fix size and the verification axis ride with
    them because nothing else states the axis PER DEFECT. The field renders only where the
    run settled its defects more than one way; where it settled them all the same way, the
    summary's own breakdown at the top of the report is what states it, once, and a field
    repeating that on every entry costs a reader attention to tell them nothing.
    """
    # One level below a section AND below a group heading. The unresolved section groups
    # its defects by what would settle each, so a defect at the same level as its group
    # would make the two indistinguishable to a reader and to anything parsing the
    # document.
    # The location is a NAME of code, so it is set as code: shaded, in the monospace face,
    # and visually the same thing as the snippet it points into. Unless the name holds a
    # BACKTICK, which a POSIX file name may: the escape that keeps it out of the prose is a
    # backslash, and a code span closes on the backtick behind it -- `a\`b.py:1` came out
    # as `a\` followed by loose text, which is a different file from the one cited. Left as
    # prose there, where the escape does its job; the anchor pattern accepts either.
    located = _one_line(_at(cluster, names))
    fields = [cluster["severity"], _breadth(cluster, rung),
              located if "`" in located else f"`{located}`",
              cluster["fix_size"]]
    labels = list(DEFECT_META_LABELS[:4])
    # `Verification` is stated per defect only where the run has more than one answer for
    # it. On a run where nothing could be executed it reads `by reading` on every defect
    # alike, which is a property of the RUN and is already stated twice at the top -- and a
    # field with one value costs a reader attention on every entry to tell them nothing.
    if verification:
        fields.append(_defect_verification_words(cluster, members))
        labels.append(DEFECT_META_LABELS[4])
    # `strict` because a field added without its label, or the other way round, would
    # drop silently here — and the line would then stop matching `_MD_DEFECT_ENTRY`,
    # leaving every defect on the page unanchored for a reason nothing points at.
    meta = DEFECT_META_SEP.join(f"**{label}** {value}"
                                for label, value in zip(labels, fields, strict=True))
    label = _one_line(_short(cluster["consequence"]))
    sentence = _one_line(cluster["consequence"])
    out = [f"\n{'#' * level} {cluster['id']}. {label}\n\n"]
    # The line below the heading exists to carry what the heading had to CUT, so it renders
    # only where the heading cut something. Where the whole consequence fits a heading the
    # two are the same sentence, and printing both put it on the page twice on every defect
    # in the report. The condition is the truncation and not the round: a run whose
    # synthesis degraded keeps the sentence on exactly these terms.
    if sentence != label:
        # The sentence lands on a line of its own with nothing of the engine's in front
        # of it, so a leading `#` or `-` would be the worker writing this document's
        # structure rather than its prose.
        out.append(f"{_no_block_lead(sentence)}\n\n")
    out += [f"- {meta}\n",
            # The decode, in place: the short name above is only safe where a reader can
            # get back to the path without leaving the entry.
            f"  {_in_full(cluster, commit)}\n"]
    # Under the metadata and above everything the panel said, so a reader meets it before
    # acting on the entry. A pointer and not the reason: the reason is in one place.
    if corrected:
        out.append(f"- **{CORRECTED_MARK}.** See "
                   f"[{SUBSECTION_CORRECTIONS}]({_anchor(SUBSECTION_CORRECTIONS)}).\n")
    # The evidence, directly under the location that names it and ABOVE the prose about
    # it. The heading already says in plain words what goes wrong, so a reader has decided
    # whether this defect is theirs before reaching here; what they want next is the code,
    # while the location is still on screen. Putting the narrative first makes them hold it
    # in their head, scroll past it to the source, and scroll back to act on it.
    # One block per SITE. Two reports of one defect routinely cite ranges that overlap
    # without matching exactly -- 977-994 and 979-994 are the same piece of code read twice
    # -- so comparing the rendered text keeps both and prints the middle of it twice. Ranges
    # are collected per file, sorted, and one is dropped only where another CONTAINS it.
    # Containment and not overlap: 10-15 beside 14-19 overlaps, and keeping the wider of
    # the two throws away source the other cited -- silently, with no truncation marker,
    # under a heading that says those lines are what the defect is about. Two ranges that
    # merely touch are two pieces of code and both render.
    # An ESTABLISHED member's snippet only, which is what leaves the unresolved section
    # with no code in it at all. That was measured before it was decided: quoting the cited
    # lines there took the section from 355 lines to 889 — 28 lines a defect — and the code
    # it added is not the code that settles anything.
    # A reader of that section is being sent to a DIFFERENT file, named in the settling
    # account; the lines the claim was made about are described in words by the mechanism
    # paragraph and linked, exact, by the location above it.
    cited: list[tuple[str, int, int, dict, str | None]] = []
    for record in members:
        snip = record["snippet"]
        if snip is None or record["status"] not in ESTABLISHED_STATUSES:
            continue
        lo = snip["first_line"]
        cited.append((record.get("file") or "", lo, lo + len(snip["lines"]) - 1,
                      snip, record.get("file")))
    kept: list[tuple[str, int, int, dict, str | None]] = []
    # Sorted so a container always arrives before what it contains: same file, then the
    # earliest start, then the widest of the ones that start together.
    for entry in sorted(cited, key=lambda e: (e[0], e[1], -e[2])):
        if kept and kept[-1][0] == entry[0] and entry[2] <= kept[-1][2]:
            continue
        kept.append(entry)
    # Each block says which lines it is, unless there is exactly one and the line above
    # already named it. A defect can cite two files -- one member's report established and
    # another's refuted -- and the snippet that survives need not be the one the defect's
    # own location names, so an unlabelled block under that location shows a reader one
    # file's source under another file's heading.
    labelled = len(kept) > 1 or any(
        _where(path, lo, hi, names) != _at(cluster, names) for path, lo, hi, _s, _p in kept)
    for _path, lo, hi, snip, path in kept:
        if labelled:
            # The snippet reaches the LABEL and never the test above it: that test compares
            # the block's location against the defect's to decide whether a label is needed,
            # and a clause about the pad appended to one side would make them differ always.
            out.append(f"  {_one_line(_where(_path, lo, hi, names, snip))}\n")
        out += _render_snippet(snip, commit, path)
    if cluster["split_reason"]:
        out += _item("- Kept separate from another report at this location: ",
                     cluster["split_reason"])
    # The three named prose parts, where the synthesis round wrote them, in the order
    # somebody works in: what happens, what to do about it, what should fail before the fix.
    judged = cluster["tier"] is not None
    if judged:
        out += _item(f"- **{WHAT_GOES_WRONG_LABEL}** ", cluster["what_goes_wrong"])
        # What stopped the check, for a defect nothing settled. It sits BETWEEN the
        # mechanism and the fix because that is the order somebody works in here: what is
        # claimed, what would decide whether it holds, and what to do if it does. One entry
        # per checker, deduplicated the way the tests below are, since two lanes describing
        # one site routinely give one account of what was missing.
        if unsettled:
            out += _unsettled(members, names)
        out += _item(f"- **{FIX_LABEL}** ", cluster["fix"])
        # The test is the verifier's, so it is one per member; deduplicated for the reason
        # the member blocks below are, since two lanes describing one site name one test.
        tests: list[str] = []
        for record in members:
            named = record["test_first"]
            if named is None:
                continue
            where = f"({_one_line(_at(record, names))}) " if record["siblings"] else ""
            rendered = "".join(_item(f"- **{TEST_FIRST_LABEL}** {where}", named))
            if rendered not in tests:
                tests.append(rendered)
        out += tests
        # Only the references that survived checking. The ones that did not are named,
        # with the reason each was dropped, under "The synthesis round" in the appendix —
        # a reader who is never told one was refused cannot tell a defect that stands
        # alone from one whose only stated connection failed.
        if cluster["cross_references"]:
            named_refs = ", ".join(f"[{did}](#{did})" for did in cluster["cross_references"])
            out.append(f"- **{RELATED_LABEL}** {named_refs}\n")
    # Two lanes that described one defect in the same words render one entry, not two
    # identical ones. Nothing is hidden by that: how many reports there were is the
    # corroboration line above, and which units made them is the provenance appendix.
    # Where two members differ at all — a different failure, a different verdict, a
    # different rationale — both render, which is what keeps every member's own verdict
    # visible.
    seen: list[str] = []
    # The reason the GROUP HEADING above already states, where there is exactly one. A
    # member line repeating it under a heading that just said it costs a reader a line per
    # report and tells them nothing; a member whose reason differs from the group's — a
    # defect two checkers could not settle for two different reasons — still renders it.
    settled = cluster["unresolved_reason"] if unsettled else None
    # A member states its own outcome where the members of THIS defect do not all share
    # one. The suppression above is about the defect's `Verification` field, which says
    # nothing when every established defect in the run was settled the same way; a member
    # line is a different fact at a different scope, and one defect routinely holds a
    # report that was reproduced, one a check refuted and one nothing settled. Deciding
    # the member line by the defect field's variance hid all three behind bare sentences,
    # so a reader could not tell which of a defect's three reports had been upheld.
    outcomes = {record["axis_b"] for record in members}
    # The checks of every report, under one label, then the bodies. Deduplicated on the
    # PAIR: two reports of one site whose prose matches are one body, and dropping a check
    # line with it would lose a verdict the run actually returned.
    # TWO passes, and the order is the point. The first renders every member with no
    # evidence suppression at all and drops the duplicates; the second renders only the
    # survivors, sharing one record of what has been printed.
    #
    # Suppressing first defeats the dedupe: two members identical in every way then differ
    # only in that the second says "Same run as above", so both survive and the same check,
    # failure and direction print twice. The suppression is about one defect's repetition,
    # which is a question that can only be asked once the repetitions are gone.
    kept: list[dict] = []
    for record in members:
        lines, body = _render_member(record, commit, names, judged,
                                     verification or len(outcomes) > 1, settled,
                                     cluster["status"])
        rendered = ("".join(lines), "".join(body))
        if rendered not in seen:
            seen.append(rendered)
            kept.append(record)
    # The runs THIS defect has printed. Scoped to the defect because that is the unit a
    # reader takes in at once: the same broad search under two different defects is a fact
    # each of them needs, and under one defect three times it is noise.
    ran: set[str] = set()
    checked: list[str] = []
    bodies: list[str] = []
    for record in kept:
        lines, body = _render_member(record, commit, names, judged,
                                     verification or len(outcomes) > 1, settled,
                                     cluster["status"], ran)
        checked.append("".join(lines))
        bodies.append("".join(body))
    block = ["- **Checks.**\n", *checked] if any(checked) else []
    return out + block + bodies


def _render_index(clusters: Sequence[dict], rung: str, refuted: int,
                  names: PathNames, sections: _Sections,
                  corrected: frozenset[str] = frozenset()) -> list[str]:
    """The first of the three views of the defect list: one table, every defect that is
    work, most severe first and cheapest first within that. Section 9 forbids a second
    table that differs only by a filter, so this is the only place the list is ranked.

    **It states its own ordering.** A ranked table whose rule a reader has to infer is a
    table they re-sort by hand, and the rule here is not the obvious one — cost breaks ties
    inside a severity, which is what makes the first rows the ones to start on.

    It is a SUBSECTION of the index section rather than a section of its own. It and the
    by-file table are two ways into one list, and as sibling headings they read as two
    separate things a reader has to decide between.
    """
    out = [sections.sub(SUBSECTION_RANKED),
           "Every defect that needs work, most severe first and, within a severity, "
           "cheapest fix first. So the first rows are the blockers, in the order to take "
           "them on.\n"]
    if refuted:
        out.append(f"\n{_plural(refuted, 'refuted defect')} "
                   f"{'is' if refuted == 1 else 'are'} not here. Nothing needs doing about "
                   f"{'it' if refuted == 1 else 'them'}, so "
                   f"{'it is' if refuted == 1 else 'they are'} listed under Refuted, in the "
                   f"appendix, with the reason {'it' if refuted == 1 else 'each'} was "
                   f"dismissed.\n")
    out += [f"\n| {DEFECT_COLUMN} | Consequence | Severity | Status | Location | Fix size "
            f"| Corroboration |\n", "|---|---|---|---|---|---|---|\n"]
    for cluster in clusters:
        # Marked here as well as on the entry: this is the list a reader works down, and
        # one who acts from the row never opens the entry that carries the mark.
        mark = (f", [corrected by the operator]({_anchor(SUBSECTION_CORRECTIONS)})"
                if cluster["id"] in corrected else "")
        out.append(f"| [{cluster['id']}](#{cluster['id']}) | "
                   f"{_cell(_short(cluster['consequence']))} | "
                   f"{_term_cell(cluster['severity'])} | {_term_cell(cluster['status'])}{mark} | "
                   f"{_cell(_at(cluster, names))} | "
                   f"{_term_cell(cluster['fix_size'])} | {_term_cell(_breadth(cluster, rung))} |\n")
    return out


def _render_by_file(clusters: Sequence[dict], by_id: dict, names: PathNames,
                    sections: _Sections) -> list[str]:
    """The second view, and the reason it exists rather than being a sort of the first: one
    person claims a file and closes what is in it in one change."""
    # Every MEMBER's file, not just the defect's primary one. A cluster can hold reports
    # in two files, and listing it under one of them hides the other from the person
    # allocating the work — which is the only thing this table is for. A defect counts once
    # per file it touches.
    by_file: dict[str, list[dict]] = {}
    ranges: dict[str, list[str]] = {}
    for cluster in clusters:
        for record in [by_id[cid] for cid in cluster["members"]]:
            held = by_file.setdefault(record["file"], [])
            if cluster not in held:
                held.append(cluster)
            where = (str(record["line_start"]) if record["line_start"] == record["line_end"]
                     else f"{record['line_start']}-{record['line_end']}")
            seen = ranges.setdefault(record["file"], [])
            if where not in seen:
                seen.append(where)
    out = [sections.sub(SUBSECTION_BY_FILE),
           "The same defects, grouped by file, so one person can take a file and close "
           "every defect listed under it in one change. A defect reported in two files is "
           "listed under both.\n\n",
           "| File | Defects | Most severe | Lines |\n", "|---|---|---|---|\n"]
    # Ordered by the name the column actually shows. Sorting by the full path would leave
    # a reader a column that is not in any order they can see.
    for path in sorted(by_file, key=lambda p: (names.short(p), p)):
        held = by_file[path]
        out.append(f"| {_cell(names.short(path))} | {len(held)} | "
                   f"{_term_cell(_most_severe([c['severity'] for c in held]))} | "
                   f"{_cell(', '.join(ranges[path]))} |\n")
    return out


def _by_tier(clusters: Sequence[dict], tiers: Sequence[str]) -> list[tuple[str | None, list[dict]]]:
    """The defects under their tiers, in the order the round declared them.

    The declared order is the rendered order, and it is the round's own: the tier list is
    returned as "the order a reader should work through them", so re-sorting it here would
    throw away the one piece of ordering the round was asked for. A tier holding nothing —
    a run whose second theme is entirely refuted defects — writes no heading, because the
    section it would head is empty.

    ``()`` for ``tiers`` — no round, or one that came back unbelievable — returns a single
    unnamed group holding everything, which is the flat list this report rendered before
    the round existed. That is what makes the degrade a change of nothing rather than a
    branch each caller has to remember.
    """
    if not tiers:
        return [(None, list(clusters))]
    held: dict[str, list[dict]] = {tier: [] for tier in tiers}
    untiered: list[dict] = []
    for cluster in clusters:
        (held[cluster["tier"]] if cluster["tier"] in held else untiered).append(cluster)
    out: list[tuple[str | None, list[dict]]] = [(tier, held[tier]) for tier in tiers if held[tier]]
    if untiered:
        out.append((UNTIERED_GROUP, untiered))
    return out


def _render_body(clusters: Sequence[dict], by_id: dict, rung: str,
                 commit: dict, names: PathNames, sections: _Sections,
                 tiers: Sequence[str] = (), disclaim: bool = False,
                 corrected: frozenset[str] = frozenset()) -> list[str]:
    """The third view: the defects themselves, grouped by status and then by tier, with
    severity order inside. Section 9 refuses a body that claims an ordering it does not
    have, so neither grouping is an ordering: the status is a fact the engine computed, the
    tier is the name one agent gave what these defects break, and the ORDER inside a tier is
    the same computed priority the index uses.

    The tier is added inside the status sections rather than replacing them. It says what a
    defect breaks; established, unresolved and refuted say whether anybody checked it, and a
    reader who loses the second cannot tell a confirmed blocker from a claim nothing
    settled. This is what overturns criterion 32 of the previous plan, which forbade
    thematic grouping outright; the as-built there records the decision.
    """
    def members(cluster):
        return [by_id[cid] for cid in cluster["members"]]

    # Whether `Verification` varies across the defects that carry an entry. One value means
    # it is a fact about the run, not about any defect, and the run states it twice already.
    entried = [c for c in clusters if c["status"] == DEFECT_ESTABLISHED]
    verification = len({c["axis_b"] for c in entried}) > 1

    def grouped(held: Sequence[dict], level: int) -> list[str]:
        """One status section's defects, under a tier heading each where the round named
        any. With no tiers the defects sit at the section's own level, exactly as they did
        before the round existed."""
        out: list[str] = []
        for name, group in _by_tier(held, tiers):
            if name is None:
                for cluster in group:
                    out += _render_defect(cluster, members(cluster), rung, commit, names,
                                          level, verification,
                                          corrected=cluster["id"] in corrected)
                continue
            out.append(f"\n{'#' * level} {_counted(_one_line(name), len(group))}\n")
            for cluster in group:
                out += _render_defect(cluster, members(cluster), rung, commit, names,
                                      level + 1, verification,
                                      corrected=cluster["id"] in corrected)
        return out

    out: list[str] = []
    established = [c for c in clusters if c["status"] == DEFECT_ESTABLISHED]
    out.append(sections.top(SECTION_ESTABLISHED, len(established)))
    # Where the body begins, and said once for the whole document: the register is the
    # clustering notes', which mark a judgment as a judgment in the same plain way.
    if disclaim:
        out.append(f"{SYNTHESIS_DISCLAIMER}\n\n")
    if not established:
        out.append("Nothing was established.\n")
    else:
        out.append("Each was confirmed by a checker that had not raised it. A defect keeps "
                   "every report of it, including a report that a check refuted.\n")
    out += grouped(established, 3)

    unresolved = [c for c in clusters if c["status"] == DEFECT_UNRESOLVED]
    out.append(sections.top(SECTION_UNRESOLVED, len(unresolved)))
    if not unresolved:
        out.append("Nothing was left unresolved.\n")
    else:
        # It names the part that is a VERDICT. The disclaimer above marks the round's
        # narrative, fix and links as one agent's unchecked reading and is printed once for
        # the whole document, at the top of the established section; a reader who arrives
        # here from the index has not passed it, and the one part of this section they are
        # meant to act on is the one part of it that is not the round's.
        #
        # It is scoped to "where a checker answered", because that is the only place the
        # settling account can come from. A defect under `No verdict was received` has no
        # answered member and so carries none, and a sentence promising one on every entry
        # would leave a reader unable to tell an engine that dropped the part from a unit
        # that never returned -- which is the distinction that group heading exists to draw.
        out.append(f"Nothing here is established: each is a claim a check could not "
                   f"settle. Where a checker answered, the entry carries "
                   f"**{UNSETTLED_LABEL}** This is that checker's own account of what "
                   f"stopped it; for most of these it names the one file or contract that "
                   f"would decide the claim. Where no checker answered, the group says so, the "
                   f"entry says a verdict never came back, and Coverage names the unit "
                   f"that owes one. The defects are grouped by what would settle each, and "
                   f"then by what each breaks. Every defect here sits under exactly one "
                   f"group, so a group's size counts pieces of work, not mentions.\n")
    # The settling reason stays the PRIMARY grouping: it is a fact the verifiers returned
    # and it is what a person does next. The tier names the substance inside it, which is
    # what turns one group of thirty-nine into groups somebody can take one of — and where
    # the round named nothing the four fixed terms stand alone, as they always have.
    groups: dict[tuple[str, str | None], list[dict]] = {}
    for cluster in unresolved:
        groups.setdefault((_settles(cluster), cluster["tier"] if tiers else None), []).append(cluster)
    settling = [*UNRESOLVED_GROUPS.values(), MIXED_SETTLING, UNKNOWN_SETTLING]
    rank = {tier: n for n, tier in enumerate(tiers)}
    for key in sorted(groups, key=lambda k: (settling.index(k[0]), rank.get(k[1], len(tiers)))):
        held = groups[key]
        title = key[0] if key[1] is None else f"{key[0]} — {_one_line(key[1])}"
        out.append(f"\n### {_counted(title, len(held))}\n")
        # The defect body, with the settling account added to it. This section is NOT a
        # table and must not become one, which was tried: the four columns worth putting in
        # one -- the id, the consequence, the severity and the location -- are all columns
        # section 2a already ranks, for every defect on the page including these. A table
        # here is a third copy of facts the reader has twice, re-sorted, and it is the one
        # treatment in the report that would show LESS than the record holds: every
        # unresolved cluster carries a populated mechanism, fix and settling account, and
        # each of its candidates a rationale naming the file that would decide it. Nobody
        # can pick one of these up from four columns, because the three things they need
        # are exactly the fields a table leaves out.
        #
        # And the length a table would save is not there to save. Carrying the settling
        # account, this section is SHORTER than the body treatment alone was -- the settling
        # reason renders once per group rather than once per member, and the evidence
        # de-duplication and the constant-field suppression above apply here unchanged.
        for cluster in held:
            out += _render_defect(cluster, members(cluster), rung, commit, names, 4,
                                  verification, unsettled=True,
                                  corrected=cluster["id"] in corrected)
    return out


def _carries_a_reading(cluster: dict) -> bool:
    """Whether this defect puts any of the round's own prose on the page.

    The disclaimer follows what RENDERS, not what the round returned, and the two are not
    the same defect by defect. A work defect shows `What goes wrong.`, `Fix.` and `Related.`
    once it has a tier. A REFUTED defect is one compact line and shows none of that — except
    a surviving cross-reference, which renders beneath it, and which is the round's judgment
    as much as any other part of it.

    Reading only the work defects left a run where the round's one usable answer was a
    refuted defect's reference: the link rendered, and the sentence saying nobody checked it
    did not. A reader then has an unchecked claim in the voice of a checked one, which is the
    thing the disclaimer exists to prevent.
    """
    if _is_work(cluster):
        return cluster["tier"] is not None
    return bool(cluster["cross_references"])


def _render_corroborated(clusters: Sequence[dict], rung: str,
                         sections: _Sections) -> list[str]:
    """The defects two independent readers each found, named together.

    **This is not a fourth view of the defect list**, and the difference is what keeps
    section 9's bound intact rather than bending it. The three views each carry facts a
    reader works from — the index ranks, the by-file table allocates, the body is the
    defects themselves — and what makes a second filtered table of those facts harmful is
    that it gives the same list a second ordering: a reader who works this one first works
    by confidence where the index ranks by severity and cost. So this section carries no
    severity, no location, no fix size, no status and no ordering of its own. It names a
    set, in the order the defects were raised, and says where the facts are. There is
    nothing in it to work from, and nothing to reconcile against the index.

    The membership is the engine's own: a defect whose candidates came from both lanes,
    which is what the index's corroboration column already states one row at a time. The
    section states it once, as a set, which a column cannot.

    Refuted defects are left out for the reason they are left out of the ranked views:
    they are not work — and the empty case says so in the same words, because a run whose
    one both-lane defect was refuted did not fail to corroborate anything.
    """
    _, both = RUNG_BREADTH[rung]
    named = sorted((c for c in clusters if _is_work(c) and c["axis_a"] == AXIS_A_BOTH),
                   key=lambda c: _id_rank(c["id"]))
    out = [sections.top(f"{SECTION_CORROBORATED}{both}", len(named))]
    if not named:
        # Scoped to the set this section is about, which is the work. Saying "no defect was
        # raised by both" would be FALSE on a run where one was and was then refuted — the
        # line above drops refuted defects, so the sentence has to drop them too or it
        # denies a thing that happened.
        out.append(f"Nothing left to address was raised by {both}: every defect that needs "
                   f"work came from a single reader.\n")
        return out
    out.append(f"Each of these was raised independently by a unit in each lane. Both "
               f"units read blind, and neither saw the other's output. That is all this "
               f"section says. What each defect is, how severe it is and where it is are "
               f"in the sections above, and nothing is ranked here.\n\n")
    # Two columns, and the id is a link into the defect's own entry. A severity or a
    # location column here would be the duplication this section is written to avoid: every
    # defect in it is in the ranked index, which already carries both. The test a table has
    # to pass is whether it carries something the index does not -- which is why the refuted
    # table below has four columns and this one has two. Refuted defects are in no ranked
    # view at all, so their row is the only place those facts appear.
    out += [f"| {DEFECT_COLUMN} | Consequence |\n", "|---|---|\n"]
    out += [f"| [{cluster['id']}](#{cluster['id']}) | "
            f"{_cell(_short(cluster['consequence']))} |\n" for cluster in named]
    return out


def _render_refuted(clusters: Sequence[dict], by_id: dict, names: PathNames,
                    corrected: frozenset[str] = frozenset()) -> list[str]:
    """The claims a check dismissed, each with the reason that dismissed it.

    A TABLE, one row per defect: which defect, what it said, where, and why it was
    dismissed. It earns its four columns where the corroborated table two sections up earns
    only two — a refuted defect is not work, so it is in no ranked view, and this row is the
    only place any of those facts appear. What is bounded here is the MATERIAL, not the row:
    a refuted claim gets its reason and the connections it named, and none of the apparatus
    a defect in the body carries.

    The row is also where its ANCHOR lives. A refuted defect has no heading of its own, so
    a bare id in the first cell is what a link from anywhere else in the document lands on.

    In the appendix rather than among the defects, because this section is the only one in
    the report that is not work. Ranked beside live defects it distorted the ranking — the
    order had to put status above cost to keep dismissed claims off the top — and read
    beside them it costs a person time on every pass down the list. Here it is what it is:
    the record of what was raised and settled, kept so nobody re-treads it and so the
    judgment can be argued with.
    """
    refuted = [c for c in clusters if c["status"] == DEFECT_REFUTED]
    out = [f"\n### {_counted(SUBSECTION_REFUTED, len(refuted))}\n\n"]
    if not refuted:
        out.append("Nothing was refuted.\n")
        return out
    out.append("Claims that were checked and dismissed, each with the reason, so nobody "
               "goes over them again. Nothing here needs work.\n\n")
    out.append(f"\n| {DEFECT_COLUMN} | Consequence | Location | Why it was dismissed |\n")
    out.append("|---|---|---|---|\n")
    for cluster in refuted:
        # Collapsed, not escaped: _item escapes what it inlines, and doing both shows the
        # reader the entities instead of the text.
        reason = " ".join(_collapse(by_id[cid]["rationale"] or "")
                          for cid in cluster["members"])
        # The warning survives the compact form. A refutation is exactly where a wrong
        # location does the most damage: the claim is dismissed, and the lines the verifier
        # read were not the lines the reader meant.
        misquoted = any(by_id[cid]["quote_check"] == QUOTE_DIFFERS
                        for cid in cluster["members"])
        # A connection the check KEPT, in the cell with the reason and behind the same
        # label the body uses. Without the label it is a link glued to the end of a
        # sentence with nothing saying what it is; without the link at all, the only
        # cross-references a refuted defect could produce were the REFUSED ones, named in
        # the appendix -- so the report stated what it had thrown away and not what it had
        # allowed. A refuted claim is where a connection earns its keep: it says the
        # dismissal covers this site and not the live defect beside it.
        refs = ""
        if cluster["cross_references"]:
            refs = (f" **{RELATED_LABEL}** "
                    + ", ".join(f"[{did}](#{did})" for did in cluster["cross_references"]))
        flag = f" {QUOTE_FLAG}" if misquoted else ""
        # In the last cell and never beside the id: the id alone in the first cell is what
        # gives this row its anchor on the page.
        mark = (f" **{CORRECTED_MARK}**; see "
                f"[{SUBSECTION_CORRECTIONS}]({_anchor(SUBSECTION_CORRECTIONS)})."
                if cluster["id"] in corrected else "")
        out.append(f"| {cluster['id']} | {_cell(_short(cluster['consequence']))} | "
                   f"{_cell(_at(cluster, names))} | {_cell(reason)}{flag}{refs}{mark} |\n")
    return out


# What a `test_first` sentence names as the test to write. Every token that could be a
# path or an identifier, so the sentence is read left to right and the first thing shaped
# like a test wins.
_TEST_TOKEN = re.compile(r"[\w./\\-]+")
# The words a test file or class is named by in the languages the dialect table already
# covers: `test_engine`, `engine_test`, `foo.test.ts`, `foo.spec.js`, `PaymentServiceTest`,
# `RefundLedgerSpec`. Java's `...IT` is deliberately absent -- `it` is an ordinary English
# word and matching it reads half the prose in the field as a class name.
_TEST_WORDS = frozenset({"test", "tests", "spec", "specs"})
# A camel-cased class name: the marker at the END with something before it, or `Test` at
# the FRONT of a longer name, which is how Python's own unittest classes are spelled. Both
# require the name to be longer than the marker, so the bare word `Test` in a sentence is
# not a class and `PaymentServiceTest`, `HTTPTest` and `TestPaymentService` all are.
_CAMEL_TEST = re.compile(r"^Test(?=[A-Z])|\w(?:Test|Tests|Spec|Specs)$")
# The heading for gaps whose entry names no test to write. They are grouped rather than
# dropped: an entry nobody can file is still a gap somebody has to close.
GAPS_NO_TEST_NAMED = "Where the entry names no test"


def _test_named(text: str) -> tuple[str | None, str | None]:
    """The test a `test_first` sentence says should be red, as (key, spelling).

    The key is what groups two entries; the spelling is what the heading shows. A path and
    the class inside it are ONE key -- `tests/PaymentServiceTest.java` and
    `PaymentServiceTest` are the same class named by two verifiers with different amounts
    of the tree in front of them -- and the heading keeps whichever spelling the section
    met first, which is the more useful one to hand somebody in a language where the file
    is the subject.

    `test_first` is free prose and this is a reading of it, not a parse. It is bounded on
    purpose: a token must be shaped like a file or an identifier AND carry one of the
    naming conventions above, so `write a test that...` does not file every gap in the run
    under `test`. A sentence this cannot read leaves its gap under
    :data:`GAPS_NO_TEST_NAMED` rather than under a guess.
    """
    for raw in _TEST_TOKEN.findall(text):
        token = raw.strip("./-")
        if not token:
            continue
        base = token.replace("\\", "/").rsplit("/", 1)[-1]
        stem = base.rsplit(".", 1)[0] if "." in base else base
        parts = [part for part in re.split(r"[._-]", stem) if part]
        if not parts:
            continue
        # One bare word is a class only where it is camel-cased into one. Without this a
        # sentence's own "test" and "tests" are subjects.
        if len(parts) < 2 and not _CAMEL_TEST.search(stem):
            continue
        if (parts[0].lower() in _TEST_WORDS or parts[-1].lower() in _TEST_WORDS
                or _CAMEL_TEST.search(parts[-1])):
            return stem, token
    return None, None


def _gap_order(record: dict) -> tuple:
    """Gaps by file, then by line. Not by severity: a gap is a test somebody sits down and
    writes, and the useful order is the order they open the files in."""
    return (record["file"], record["line_start"], record["line_end"], record["id"])


# Gaps are a coverage question only, so these are the coverage statuses and no other. The
# order is what a reader can DO about each: write the test, check the claim, nothing.
_GAP_PRIMARY_RANK = {COVERAGE_GAP_CONFIRMED: 0, "unresolved": 1, COVERAGE_GAP_REFUTED: 2}


def _gap_primary(cluster: dict, by_id: dict) -> dict:
    """The member a gap cluster is reported FROM: the one that established it.

    Two auditors naming one gap can be answered differently — one challenger finds the test
    that already covers it, the other confirms the gap stands — and a single confirming
    answer is what makes the cluster established. Reading whichever member happened to be
    listed first therefore printed a refuting rationale under an established heading, and
    dropped the ``test_first`` the reader is there to write.

    Where nothing established it, the one still OPEN comes next, and a refutation last. An
    unresolved cluster read off its refuting member printed why a test already covers the gap
    under a heading saying nobody could tell — hiding the account of what is still to check,
    and the test that member proposed. The order is what a reader can act on: write this
    test, then check this claim, then nothing to do.

    `min` is stable, so members of equal rank keep the order they were clustered in.
    """
    members = [by_id[cid] for cid in cluster["members"]]
    return min(members, key=lambda m: _GAP_PRIMARY_RANK.get(m["status"], len(_GAP_PRIMARY_RANK)))


def _gap_entry(cluster: dict, by_id: dict, names: PathNames) -> list[str]:
    """One gap: where it is, the input no test constructs, the test the auditor proposed,
    and what the check found. Four facts and no apparatus — a gap carries no severity
    ranking, no corroboration axis and no snippet, because the thing to do with it is
    write the test, and none of those change that.

    Read off the first member. Two auditors naming one gap is why coverage is clustered at
    all, and their accounts of it are the same account twice; the second adds nothing a
    reader about to write the test can use.
    """
    primary = _gap_primary(cluster, by_id)
    # `_one_line`, like every other caller of `_at`: the string it returns is RAW by
    # contract, and a path is a REVIEWED repository's bytes. `<img src=x onerror=...>.py` is
    # a legal POSIX name, and bare it reached `report.html` as a live tag -- the reviewed
    # tree scripting the page its reviewer opens. This was the one call site that took it
    # bare; the escape is the same one the tables and the defect entries already use.
    out = [f"- **{_one_line(_at(cluster, names))}** — {_one_line(_short(cluster['consequence']))}\n"]
    out += _item("  - No test constructs: ", primary["failure"])
    out += _item("  - The auditor proposed: ", primary["direction"])
    if primary["test_first"]:
        out += _item(f"  - {TEST_FIRST_LABEL} ", primary["test_first"])
    # Only a checker's words are printed as what the check found. A record nobody answered
    # carries the engine's own diagnostic in this field -- the stand-in for a verdict it
    # could not read -- and printed here it reads as a checker's rationale; the coverage
    # section names that candidate and the parse error, and is the one place they belong.
    if primary["rationale"] and primary.get("answered", True):
        out += _item("  - What the check found: ", primary["rationale"])
    if primary["quote_check"] == QUOTE_DIFFERS:
        out.append(f"  - {QUOTE_FLAG}\n")
    return out


# What a coverage verifier said about the batch as a whole, above the gaps it answered.
COVERAGE_BATCH_LABEL = "Across the batch"


def _batch_notes(clusters: Sequence[dict], by_id: dict,
                 said: dict[str, str]) -> list[str]:
    """The batch-wide paragraph of every check that answered a gap in this group, once.

    A verifier looking at forty gaps in one class learns things true of all forty — the
    class has no test file at all, the suite never instantiates it — and asked to put them
    in each gap's rationale it writes them forty times. ``summary`` is the field for them,
    and a group heading is the one place it can be stated once: above a single gap it would
    be a fact about thirty-nine others. A check that wrote nothing states nothing.
    """
    out: list[str] = []
    for unit in _distinct(
            (by_id[cid]["verified_by"] or {}).get("unit")
            for cluster in clusters for cid in cluster["members"]):
        if unit in said:
            out += _item(f"- **{COVERAGE_BATCH_LABEL}** ({unit}): ", said[unit])
    return [*out, "\n"] if out else out


def _render_coverage_gaps(findings: Findings, names: PathNames,
                          sections: _Sections) -> list[str]:
    """The coverage ladder: gaps that stood, and gaps nothing settled.

    Its own section, and not a corner of the defect body, because a gap makes no claim that
    the code is wrong. On the defect ladder the only honest verdict a defect verifier could
    give one was `refuted` — missing tests are not failures — and the refuted appendix says
    in its own heading that nothing under it is work. A named missing test IS work, so it is
    here, above the appendix, listed the way somebody about to write the tests would want
    it: by file, then by line.

    Gaps a test already covers are not here. They are in the appendix with the name of the
    test that covers each, for the same reason a refuted defect is: settled, and worth
    keeping so nobody re-treads it.

    **Grouped by the test class that owes the cases, not by the file the gap is in.**
    Somebody writing the missing tests works one class at a time, and the fact worth seeing
    — that one test class owes ten cases — is invisible when the same ten are spread over
    as many headings as there are source files. Every entry already names the class it
    proposes, so :func:`_test_named` reads it off rather than asking for it twice. Each
    entry still carries its own location, so nothing is lost by dropping the file heading.
    """
    gaps = sorted(findings.coverage_clusters, key=_gap_order)
    by_id = {record["id"]: record for record in findings.coverage}
    standing = [c for c in gaps if c["status"] == DEFECT_ESTABLISHED]
    unresolved = [c for c in gaps if c["status"] == DEFECT_UNRESOLVED]
    out = [sections.top(SECTION_COVERAGE_GAPS, len(standing))]
    out.append("Inputs the code handles differently that no test in scope constructs. "
               "**None of these says the code is wrong.** A branch can be correct today with "
               "no test guarding it, and a test is what keeps it correct. Each is a test to "
               "write, at the lines that decide the input, under the test class that owes "
               "it.\n")
    if not standing and not unresolved:
        out.append("\nNo coverage gap stood: every one raised is covered by a test named in "
                   "the appendix.\n")
        return out
    said = {row["unit"]: row["summary"] for row in findings.verification if row["summary"]}
    groups: dict[str, list[dict]] = {}
    spelling: dict[str, str] = {}
    for cluster in standing:
        primary = _gap_primary(cluster, by_id)
        key, named = _test_named(primary["test_first"] or "")
        groups.setdefault(key or "", []).append(cluster)
        if key and key not in spelling:
            spelling[key] = named
    # Most cases first, which is the order the section exists to show; the unreadable ones
    # last, because they are the group nobody can act on as a group.
    ordered = sorted((key for key in groups if key), key=lambda k: (-len(groups[k]), k))
    for key in [*ordered, *([""] if "" in groups else [])]:
        held = groups[key]
        out.append(f"\n#### {_one_line(spelling.get(key, GAPS_NO_TEST_NAMED))}\n\n")
        out += _batch_notes(held, by_id, said)
        for cluster in held:
            out += _gap_entry(cluster, by_id, names)
    if unresolved:
        out.append(f"\n#### {_counted('Gaps nothing settled', len(unresolved))}\n\n")
        out.append("A check could not tell whether a test constructs the input. Grouped by "
                   "what would settle each, as the unresolved defects are.\n")
        groups: dict[str, list[dict]] = {}
        # `_settles`, as the defect section: two checkers naming different settling
        # questions is a disagreement, and a gap filed under "no verdict was received" for
        # it sent the reader to re-run units that had answered.
        for cluster in unresolved:
            groups.setdefault(_settles(cluster), []).append(cluster)
        for heading in sorted(groups):
            out.append(f"\n##### {_counted(heading, len(groups[heading]))}\n\n")
            for cluster in groups[heading]:
                out += _gap_entry(cluster, by_id, names)
    return out


def _render_covered(findings: Findings, names: PathNames) -> list[str]:
    """The gaps a check answered by naming the test that covers them.

    A table, one row per gap, and the test is the column that matters: a refutation of a
    coverage gap that names no test is refused by the engine, so every row here has one.
    In the appendix beside Refuted, because it is settled and is not work.
    """
    covered = [c for c in sorted(findings.coverage_clusters, key=_gap_order)
               if c["status"] == DEFECT_REFUTED]
    by_id = {record["id"]: record for record in findings.coverage}
    out = [f"\n### {_counted(SUBSECTION_COVERED, len(covered))}\n\n"]
    if not covered:
        out.append("No coverage gap was answered with a test that covers it.\n")
        return out
    out.append("Raised as untested and settled by a check that named the test that sends "
               "that input. Nothing here needs work.\n\n")
    out.append("\n| Gap | Input | Location | The test that covers it |\n")
    out.append("|---|---|---|---|\n")
    for cluster in covered:
        member = by_id[cluster["members"][0]]
        tests = " ".join(_collapse(by_id[cid]["covered_by"] or "")
                         for cid in cluster["members"])
        out.append(f"| {cluster['id']} | {_cell(_short(member['failure']))} | "
                   f"{_cell(_at(cluster, names))} | {_cell(tests)} |\n")
    return out


# What the "where" column says for an entry that named a class and no path. A verifier
# outside whose scope the file sits may not be able to see where it lives, and the row says
# that rather than leaving a cell empty, which reads as a spelling the engine lost.
OUTSIDE_NO_PATH = "named without a path"


def _render_outside_scope(findings: Findings, names: PathNames) -> list[str]:
    """Every class outside the reviewed set that open work hangs on, most claims first.

    The answer to "widen the scope" is usually no, and it is no for a good reason: a run
    that pulls in every caller its verdicts mention stops being a bounded run. This table is
    the other half of that answer rather than an argument against it. A class nine open
    defects hang on and one that one hangs on cost the same to add to the next job and are
    not the same decision, and an owner who has to read forty-seven entries to tell them
    apart is deciding without the number.

    One row per class, with every path the run's verdicts guessed it at beside it:
    :func:`_outside_scope_records` says why the class is the key and the path is not.

    Counted in DEFECTS, because that is the unit of work being bought. It is a floor and not
    a promise: a file that would settle nine claims may settle them as refutations.
    """
    rows = findings.outside_scope
    out = [f"\n### {_counted(SUBSECTION_OUTSIDE, len(rows))}\n\n"]
    if not rows:
        out.append("No unresolved verdict named a file outside the reviewed scope.\n")
        return out
    out.append("Files this review did not cover that a verifier named as what would settle "
               "an open claim, most claims first. There is one row per class, however many "
               "paths the verdicts guessed for it; none of those files could be opened from "
               "here. The count is how many defects that one file would answer, so adding "
               "one file to the next job's scope is a decision with a number attached. It "
               "is a floor: a file that would settle a claim may settle it either way.\n\n")
    out.append(f"\n| Outside the scope | Named at | Defects it would settle | Which |\n")
    out.append("|---|---|---|---|\n")
    for row in rows:
        listed = ", ".join(f"[{did}](#{did})" for did in row["defects"])
        # The class alone is not a spelling of where the file is, so a row whose only entry
        # was the bare class says so instead of repeating the name into the next column.
        guessed = [path for path in row["paths"] if path != row["class"]]
        where = ", ".join(_cell(names.short(path)) for path in guessed) or OUTSIDE_NO_PATH
        out.append(f"| {_cell(row['class'])} | {where} | {len(row['defects'])} | "
                   f"{listed} |\n")
    return out


# What the finder column says for a unit no listing row names — a run directory whose
# `units.json` predates the field. Empty would read as a lane that raised nothing.
UNKNOWN_FINDER = "not recorded"
SUBSECTION_VERIFIER_NOTES = "What each unit said about its batch"
VERIFIER_NOTES_NONE = "No verification unit returned a summary."


def _render_verifier_variance(findings: Findings) -> list[str]:
    """One row per verification unit: what it was handed and how its answers fell.

    The established count over one set of files swung by a third between two runs of one
    job, and every bit of the swing was here — one unit answering `unresolved` to fifteen
    of twenty-six while another answered it to none of twenty. Both runs' verdicts were in
    the report already, one row per candidate in the provenance table, which is the form in
    which nobody can see it. This is the same data at the size the variance happens at.

    Ordered by the share that came back unresolved, so the unit that answered fewest
    questions is the first row. No threshold decides what counts as a wide spread — a
    threshold here would be a number nobody chose — the order just puts the outlier where
    it is read first, and the range is stated beneath.

    A unit that failed or never landed also produces a column of unresolved, and that is
    not a verifier's judgment about anything. Its state is in the row, because reading the
    two together is what separates a spread of opinion from a unit that did not answer.

    **The finder's lane is the second column.** Routing hands a unit the findings of one
    lane, so an unresolved share that runs from 0% to 62% across a run's units is as much a
    question about whose findings those were as about who checked them, and the column is
    what lets a reader ask it. Each unit's own paragraph follows the table, for the reason
    the coverage section states its verifiers': a field the engine reads and nothing prints
    is a paragraph the worker was asked for and nobody sees.
    """
    rows = findings.verification
    out = [f"\n### {_counted(SUBSECTION_VERIFIERS, len(rows))}\n\n"]
    if not rows:
        out.append("No verification unit answered for any candidate.\n")
        return out
    out.append("How each unit's answers were split. The units of one run asked the same "
               "kind of question of comparable batches, so a unit that settles far fewer "
               "than the rest tells you about that unit, not about the code it read. Each "
               "was handed one lane's findings, never its own, so the second column says "
               "whose work the row is about.\n\n")
    out.append("\n| Unit | Findings from | Handed | Established | Unresolved | Refuted |\n")
    out.append("|---|---|---|---|---|---|\n")
    def share(row):
        return row["unresolved"] / row["candidates"] if row["candidates"] else 0.0
    ordered = sorted(rows, key=lambda r: (-share(r), r["unit"]))
    for row in ordered:
        label = row["unit"]
        if row["state"] != UNIT_COMPLETE:
            # Said in the row, not inferred from a column of unresolved that looks like
            # thirty considered answers.
            label += f" — {row['state']}, so nothing here is a verdict"
        out.append(f"| {label} | {row['finder'] or UNKNOWN_FINDER} | {row['candidates']} | "
                   f"{row['established']} | {row['unresolved']} | {row['refuted']} |\n")
    answered = [row for row in ordered if row["candidates"] and row["state"] == UNIT_COMPLETE]
    if len(answered) > 1:
        top, bottom = answered[0], answered[-1]
        if share(top) != share(bottom):
            out.append(f"\nThe share left unresolved ranges from {share(bottom):.0%} "
                       f"({bottom['unit']}, "
                       f"{bottom['unresolved']} of {bottom['candidates']}) to "
                       f"{share(top):.0%} ({top['unit']}, {top['unresolved']} of "
                       f"{top['candidates']}).\n")
    out.append(f"\n#### {SUBSECTION_VERIFIER_NOTES}\n\n")
    said = [row for row in ordered if row["summary"]]
    if not said:
        out.append(f"{VERIFIER_NOTES_NONE}\n")
    for row in said:
        out += _item(f"- {row['unit']}: ", row["summary"])
    return out


def _render_clustering_notes(findings: Findings, names: PathNames) -> list[str]:
    """Section 2's audit of the merge. The one place candidate ids belong in prose: what is
    being explained here is a judgment — why two reports of one site were merged or kept
    apart — and the ids are what make that judgment checkable."""
    clusters = findings.clusters
    out = [f"\n### {SUBSECTION_NOTES}\n\n",
           f"Every candidate is in exactly one cluster: {_plural(len(findings.candidates), 'candidate')} "
           f"in {_plural(len(clusters), 'cluster')}, none dropped and none counted twice.\n"]
    merged = sorted((c for c in clusters if len(c["members"]) > 1),
                    key=lambda c: (-len(c["members"]), _id_rank(c["id"])))
    out.append("\n#### The largest clusters\n\n")
    if not merged:
        out.append("Nothing merged: every cluster holds one candidate.\n")
    for cluster in merged[:5]:
        out.append(f"- {cluster['id']} — {_plural(len(cluster['members']), 'candidate')} "
                   f"({', '.join(cluster['members'])}): "
                   f"{_one_line(_short(cluster['consequence']))}\n")
    split = [c for c in clusters if c["split_reason"]]
    out.append("\n#### Kept apart at one location\n\n")
    if not split:
        out.append("No candidates at one location were split.\n")
    for cluster in split:
        out += _item(f"- {cluster['id']} ({', '.join(cluster['members'])}) at "
                     f"{_one_line(_at(cluster, names))}: ", cluster["split_reason"])
    reported = [e for e in findings.clustering if e["summary"]]
    out.append("\n#### What each clustering unit reported\n\n")
    if not reported:
        out.append("No clustering unit returned a summary.\n")
    for entry in reported:
        out += _item(f"- {entry['area']}: ", entry["summary"])
    return out


def _render_provenance(findings: Findings, tags: dict[str, str]) -> list[str]:
    """Every candidate id, lane letter, unit id and lens name in the document, in one place
    nobody has to read to fix anything. Section 10: this is audit data, and keeping it here
    is what lets the body above be written for the person doing the work.

    What this section does NOT carry is the three raw fields — what the reader said
    failed, the direction it proposed, what the checker concluded. Reprinting all three
    against every candidate costs 40,368 of the appendix's 59,771 words on a 216-candidate
    run: an unsearchable second copy of fields ``findings.json`` already holds against the
    same candidate id, in the same directory, in a form a program can read. Leaving them
    there holds this section to 3,571 words instead of 53,921, with every candidate still
    listed. The intro sentence below is what keeps that a move rather than a loss — it
    names the file the full text is in, so a reader who wants a worker's own words knows
    where they are.

    That sentence says EVERYTHING each unit wrote rather than listing the three fields,
    because a sentence that enumerates goes quietly false the moment a fourth thing stops
    being printed here. There is already a fourth: the reproduction a raiser proposed and a
    run superseded, which ``findings.json`` holds and this section does not.

    The row is therefore the TRACE and not the content: which defect, which candidate,
    which unit raised it under which lens, what severity it proposed, and which unit
    answered. That is what cannot be recovered from ``findings.json`` by eye, and it is
    the whole reason this section exists.

    The lens is referenced by a short tag and spelled out under **The job**, two subsections
    up. A lens sentence runs to about 95 characters and a run has two of them; spelling one out
    on every raiser line said the same two things 216 times. It is spelled out THERE rather
    than in a legend here, because a lens is a field of the job and one legend per place that
    cites a lens is the same text twice — the tag and its meaning now sit in one appendix, a
    screen apart.
    """
    # BOTH ladders. The section is the audit trail — which unit raised what and which
    # answered it — and a coverage gap raised by an auditor and answered by a coverage
    # verifier is exactly the kind of thing an audit of the run has to be able to find.
    raised = (*findings.candidates, *findings.coverage)
    by_id = {record["id"]: record for record in raised}
    out = [f"\n### {SUBSECTION_PROVENANCE}\n\n",
           "Which unit raised what, and which answered it. Nothing here is needed to fix a "
           "defect; it is here so the run can be audited. Everything each unit wrote is in "
           "`findings.json` beside this report, under the same candidate id, where it can be "
           "searched: what it reported, the direction and any reproduction it proposed, and "
           "what the checker concluded.\n",
           f"Below, a reader's lens is cited by its tag. **{SUBSECTION_THE_JOB}**, two "
           f"subsections up in this appendix, spells each one out.\n"]
    # One row per candidate. The defect's own sentence is three sections up and is not
    # repeated here: the id is what a reader follows back.
    out.append("\n| Defect | Candidate | Raised by | Proposed | Answered by |\n")
    out.append("|---|---|---|---|---|\n")
    for cluster in (*findings.clusters, *findings.coverage_clusters):
        for cid in cluster["members"]:
            record = by_id[cid]
            for raiser in record["raised_by"]:
                if raiser["kind"] == AUDITOR_KIND:
                    who = f"{raiser['unit']} (auditor, lane {raiser['lane']})"
                else:
                    # Looked up on the lens EXACTLY as the record carries it, which is the
                    # string the job declared: the tags are built from the job, so a lookup
                    # against a rendered form of the same text would miss every time.
                    tag = tags.get(raiser["lens"] or "", "")
                    who = f"{raiser['unit']} (lane {raiser['lane']}{', ' + tag if tag else ''})"
                addressed = record["verified_by"]
                if not record["answered"]:
                    ans = f"{addressed['unit']} (lane {addressed['lane']}) — nothing usable"
                else:
                    ans = f"{addressed['unit']} (lane {addressed['lane']})"
                out.append(f"| {cluster['id']} | {cid} | {who} | {_term_cell(raiser['severity'])} | {ans} |\n")
    return out


def _render_synthesis_notes(findings: Findings) -> list[str]:
    """What the judgment round returned, and every place it fell short.

    Three things nothing else in the document can say. **The round's own state**, which is
    what tells a reader the tiers are missing because a unit failed rather than because the
    round was never run — and with it what the grouping now is, so nobody has to work that
    out from the headings. **An entry the engine refused**, which costs one defect its tier
    and is named so the loss is not silent. And **a cross-reference that was dropped**,
    with the reason: a reader who is never told one was refused cannot tell a defect that
    stands alone from one whose only stated connection failed.

    Nothing here is needed to fix a defect, which is why it is in the appendix — and a run
    with no synthesis round writes none of it, because there is no round to report on.
    """
    record = findings.synthesis
    if record is None:
        return []
    out = [f"\n### {SUBSECTION_SYNTHESIS}\n\n",
           f"What the judgment round returned. The tier headings, "
           f"**{WHAT_GOES_WRONG_LABEL[:-1]}**, **{FIX_LABEL[:-1]}** and "
           f"**{RELATED_LABEL[:-1]}** came from this round, and nothing else in this report "
           f"did.\n\n"]
    if record["state"] != UNIT_COMPLETE:
        out += _item(f"- {record['unit']} — {record['state']}: ",
                     record["reason"] or "no reason was recorded", SYNTHESIS_DEGRADED)
        return out
    out.append(f"- {record['unit']} — complete: {_plural(len(record['tiers']), 'tier')} "
               f"named for {_plural(len(findings.clusters), 'defect')}.\n")
    for message in record["rejected"]:
        out += _item("- an entry could not be read, and only its own defect is affected: ",
                     message, "That defect carries no tier and no account of itself, and "
                     "is grouped by its status instead.")
    for cluster in findings.clusters:
        for dropped in cluster["dropped_cross_references"]:
            out.append(f"- {cluster['id']} cited {_one_line(dropped['defect'])}, which was "
                       f"dropped: {dropped['reason']}.\n")
    return out


OVERVIEW_LEAD = ("One paragraph the judgment round wrote about the run as a whole. It is "
                 "a reading, and nobody checked it: only the defects it names were "
                 "verified, each on its own entry below.")


def _render_judgment_overview(findings: Findings) -> list[str]:
    """The synthesis round's own paragraph, under the counts it is about.

    Written for a person reading the report and, until this was rendered, printed nowhere.
    It is labeled as a reading because it is one: the round saw the verified defects and
    wrote about them, and nothing checked what it wrote.

    **Nothing is written unless the round completed and said something.** A round that
    failed or never landed leaves the document a run without the round would have, and
    the appendix is where its state is said; a heading over an empty paragraph would say
    the round had nothing to say, which is a different claim.
    """
    record = findings.synthesis
    said = (record.get("summary") or "") if record is not None else ""
    if record is None or record["state"] != UNIT_COMPLETE or not said.strip():
        return []
    return [f"\n### {SUBSECTION_OVERVIEW}\n\n", f"{OVERVIEW_LEAD}\n\n",
            f"{_paragraph(said)}\n"]


OPERATOR_LEAD = ("Written by whoever ran the panel, after the run, with this report, the "
                 "code and the owner's questions in view. It is their own reading and nothing "
                 "checked it: only the defects it cites were verified, each on its own entry. "
                 "The counts in the section above are the panel's, and nothing here changes "
                 "them.")
_BUILD_CHECK_WORDS = {"build": ("whether this tree builds", "build"),
                      "tests": ("whether its tests run", "tests")}


def _render_operator_notes(notes: ReportNotes, job: dict, findings: Findings, probe: dict,
                           sections: _Sections) -> list[str]:
    """``report-notes.json``, as its own section, labeled as the operator's reading.

    Everything in it is text somebody wrote rather than a fact the engine computed, so each
    piece goes through the same escaping a worker's text does, and a question is a heading
    only behind the engine's own ``Q<n>.``: a heading that opened on a defect id would claim
    that defect's anchor.
    """
    out = [sections.top(SECTION_OPERATOR), f"{OPERATOR_LEAD}\n"]
    asked = job.get("questions")
    if notes.answers or asked:
        out.append(sections.sub(SUBSECTION_ANSWERS))
        if asked:
            out.append("The job asked:\n\n")
            out += [f"> {_entities(line)}\n" for line in asked.splitlines()]
            out.append("\n")
        if not notes.answers:
            out.append("The operator recorded no answer.\n")
        for n, entry in enumerate(notes.answers, 1):
            out.append(f"\n#### Q{n}. {_one_line(entry['question'])}\n\n")
            for part in re.split(r"\n\s*\n", entry["answer"].strip()):
                out.append(f"{_paragraph(part)}\n\n")
            if entry["defects"]:
                out.append("Cites " + ", ".join(f"[{did}](#{did})" for did in entry["defects"])
                           + ".\n")
    if notes.corrections:
        out.append(sections.sub(SUBSECTION_CORRECTIONS))
        out.append("Each is the operator's reason for disagreeing with what the panel "
                   "concluded. The entry it names carries a mark pointing here, and is "
                   "otherwise as the panel left it.\n\n")
        status = {c["id"]: c["status"] for c in findings.clusters}
        for entry in notes.corrections:
            target = entry["target"]
            if target in _BUILD_CHECK_WORDS:
                question, key = _BUILD_CHECK_WORDS[target]
                answer = ((probe.get(key) or {}).get("answer", PROBE_UNKNOWN_WORD)
                          if probe.get("state") == UNIT_COMPLETE else PROBE_UNKNOWN_WORD)
                what = f"To the build check's answer on {question}, which was {answer}"
            else:
                what = f"To [{target}](#{target}), marked {status[target]} by the panel"
            out.append(f"- {what}: {_one_line(entry['reason'])}\n")
    if notes.caveats:
        out.append(sections.sub(SUBSECTION_CAVEATS))
        out += [f"- {_one_line(caveat)}\n" for caveat in notes.caveats]
    return out


def _plural_states(states: Sequence[str]) -> str:
    """``states`` is each unit's state name, so a reading unit as ``candidates.json``
    records it and a verification unit as the engine holds it count the same way."""
    return (f"{len(states)} — {states.count(UNIT_COMPLETE)} complete, "
            f"{states.count(UNIT_FAILED)} failed, {states.count(UNIT_MISSING)} missing")


def _run_kinds(runs: Sequence[dict]) -> str:
    """The runs split by what they did, or nothing where there were none.

    A verdict written before the field existed states no kind. It is counted in its own
    part rather than assumed to be either, so the parts always sum to the number beside
    them and no run is labelled by a guess.
    """
    if not runs:
        return ""
    kinds = [evidence.get(RUN_KIND_KEY) for evidence in runs]
    parts = [f"{kinds.count(kind)} {kind}" for kind in RUN_KINDS if kinds.count(kind)]
    unstated = kinds.count(None)
    if unstated:
        parts.append(f"{unstated} of a kind the verdict did not state")
    return " — " + ", ".join(parts) if parts else ""


def _executability(probe: dict, findings: Findings,
                   corrected: frozenset[str] = frozenset()) -> list[str]:
    """Section 7, as three facts that are never inferred from one another.

    CAPABILITY is the probe's: whether this tree builds and whether its tests run. ATTEMPTS
    is how many candidates proposed a reproduction — how much there was to execute. RAN is
    how many verdicts came back carrying evidence. A tree that builds cleanly can still
    yield nothing to run, and a verifier that refuted a claim executed perfectly well, so
    the honesty sentence keys on RAN and the probe and never on the count of ``reproduced``
    verdicts.

    **Where the capability was found is part of the fact.** The probe and the verifiers are
    both told to work in a disposable copy of the snapshot, so the probe's answer is about
    the environment the reproductions run in — and when the two disagree, the report is the
    only place a reader can see that they do. A real run said *this tree builds — yes* and
    then ran none of 207 proposed reproductions, because the snapshot held no build system
    and the probe had answered for something else. Two true-looking sentences, four
    thousand lines apart, and nothing to reconcile them. Where the probe reports a
    capability and not one reproduction could use it, that is now said where the capability
    is claimed, because a reader who believes the first sentence stops asking.
    """
    proposed = sum(1 for r in findings.candidates
                   if any(x["reproduction"] is not None for x in r["raised_by"]))
    runs = [record["evidence"] for record in findings.candidates
            if record["evidence"] is not None]
    ran = len(runs)
    if probe.get("state") == UNIT_COMPLETE:
        build = (probe.get("build") or {}).get("answer", PROBE_UNKNOWN_WORD)
        tests = (probe.get("tests") or {}).get("answer", PROBE_UNKNOWN_WORD)
        out = [f"- Executability, as a separate unit found it in a disposable copy of the "
               f"snapshot: this tree builds — {build}; its tests run — {tests}.\n"]
        if PROBE_YES_WORD in (build, tests) and proposed and not ran:
            out.append(
                f"  - **That capability reached none of the work.** "
                f"{_plural(proposed, 'reproduction')} "
                f"{'was' if proposed == 1 else 'were'} proposed and none ran. The probe "
                f"and the verifiers are told to work in a copy of the same snapshot, so "
                f"either the snapshot does not hold what a reproduction needs, or the "
                f"probe answered for a different tree. Read the line above as what the "
                f"probe found, not as what this run could do.\n")
        # The probe's own account, under the answer it explains and after the qualifier,
        # which has to stay next to the claim it qualifies. A bare `no` tells a reader the
        # build failed and not why, and the why is what decides whether it was the tree or
        # the environment the probe ran in.
        if probe.get("summary"):
            out += _item("  - What the probe reported: ", probe["summary"])
    else:
        build = tests = PROBE_UNKNOWN_WORD
        out = [f"- Executability: unknown. The capability probe is recorded as "
               f"{probe.get('state')}, so "
               f"nobody found out whether this tree builds or whether its tests run. "
               f"Unknown is not no.\n"]
    # Beside the answer it corrects and after everything the engine says about that answer,
    # because the probe's own line and its qualifier are the panel's and stay as they were.
    which = [word for target, word in (("build", "whether it builds"),
                                       ("tests", "whether its tests run"))
             if target in corrected]
    if which:
        out.append(f"  - **{CORRECTED_MARK}**, on {' and '.join(which)}; see "
                   f"[{SUBSECTION_CORRECTIONS}]({_anchor(SUBSECTION_CORRECTIONS)}).\n")
    out.append(f"- Reproductions: {proposed} proposed, {ran} run{_run_kinds(runs)}. "
               f"Whether this tree builds and its tests run, how many reproductions were "
               f"proposed and how many ran are three separate facts, and each is counted "
               f"on its own.\n")
    # Said once, where the numbers are, because "documentary" is this report's word and a
    # reader meeting it beside a defect has nowhere to look it up.
    if any(evidence.get(RUN_KIND_KEY) == "documentary" for evidence in runs):
        out.append("  - **Documentary** means the command inspected the tree without "
                   "running the code under review: a search for a string, a listing, a "
                   "checksum. It is evidence, and it is judged by the same standard. It is "
                   "not a run of the code, which is why the two are counted separately.\n")
    # `0 proposed, 1 run` reads as impossible and is not. A reader proposes a reproduction
    # for the claims it can think of one for; a verifier is free to devise its own, and
    # counting only what was proposed makes its work look like an arithmetic error. Seen in
    # a live report, where those two numbers sat side by side with nothing reconciling them.
    if ran > proposed:
        out.append(f"  - {_plural(ran - proposed, 'of those runs was', 'of those runs were')} "
                   f"devised by a verifier for a claim whose reader proposed none, which is "
                   f"why the second number can exceed the first.\n")
    if ran:
        return out
    # Nothing ran, and WHY is read from the two facts that decide it rather than from one
    # boolean over both. A tree where neither answer came back yes is a tree nothing
    # established anything could be done in; a tree where one did, with nothing proposed, is
    # a run the readers gave nothing to try; and a proposal that did not run is neither, and
    # blaming the readers for it would be false.
    if proposed:
        out.append(f"- **Nothing was executed**: {_plural(proposed, 'reproduction')} "
                   f"proposed and none ran, so what would have settled them is still "
                   f"open.\n")
    elif build != "yes" and tests != "yes":
        out.append("- **Nothing could be executed**: nothing showed that anything in "
                   "this tree can be built or run, and no candidate proposed a "
                   "reproduction.\n")
    else:
        out.append("- **Nothing was executed**: no candidate proposed a reproduction, so "
                   "nothing was tried.\n")
    out.append("  Every established finding below was established by reading. A count "
               "of them counts claims confirmed by reading, not claims confirmed by "
               "running the code.\n")
    return out


# How many directories the reachability line names before it stops naming them. The list
# is there to point at where an unreviewed caller most likely sits, and a reader stops
# reading a list of directories long before twenty.
_ADJACENT_CEILING = 12


def _adjacent(reviewed: Sequence[str], missing: Sequence[str], root: str) -> list[tuple[str, int]]:
    """Directories holding BOTH a reviewed file and an unreviewed tracked one, commonest
    first, then by name.

    This is the answer to "where would a caller of this code be that nobody read", which is
    what the warning is for. A directory with nothing reviewed in it is a part of the
    repository the job did not go near, and naming those is how the enumeration got long:
    they are the bulk of any subset review and the least informative thing in it.

    ``reviewed`` is spelled against the reviewed root and ``missing`` against the
    repository root, so the job root's prefix is put back before they are compared. Skipping
    that makes every directory look disjoint and the list come back empty on exactly the
    runs it is for.
    """
    prefix = f"{root}/" if root else ""
    reviewed_dirs = {prefix + path.rsplit("/", 1)[0] if "/" in path else prefix.rstrip("/")
                     for path in reviewed}
    counts: dict[str, int] = {}
    for path in missing:
        parent = path.rsplit("/", 1)[0] if "/" in path else ""
        if parent in reviewed_dirs:
            counts[parent] = counts.get(parent, 0) + 1
    return sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))


def _reachability_dirs(inventory: dict) -> list[tuple[str, int]]:
    """The directories the reachability warning names, commonest first. Read by the warning
    itself and by :func:`_printed_paths`, which gathers what the legend has to decode, so
    the two cannot end up naming different sets."""
    coverage = inventory["coverage"]
    if coverage["state"] != "computed" or not coverage["tracked_not_reviewed"]:
        return []
    reviewed = [entry["path"] for entry in inventory["files"]]
    return _adjacent(reviewed, coverage["tracked_not_reviewed"], coverage["root"])


def _reachability(inventory: dict, names: PathNames) -> list[str]:
    """Section 12's warning. It fires on ``tracked − reviewed`` being non-empty, not on the
    reviewed set being a strict subset of what is tracked: the listing includes non-ignored
    untracked files, so a run that reviews an untracked file while excluding a tracked
    caller is not a subset and a subset test would drop the warning exactly where it is
    needed. What the scope cannot answer is reachability — whether those files reach the
    defects below.

    **A count and a direction, never the enumeration.** A subset review of a large
    repository leaves nearly all of it unreviewed, and listing every such file put 10,695
    bullets into one section of a real report: it ran from line 7 to line 10,715, pushed
    every finding below line 10,700, and took the file to 1.5 MB. The fact is worth stating
    and the list is worth nothing — a reader who wants the enumeration can take the
    difference themselves, and every one of them already knows their own repository is
    bigger than the part they asked for. What a reader cannot work out is where the
    unreviewed files sit RELATIVE to what was read, so that is what is named.

    The directory list is the ONE place the document spells a path against a different
    base. It has to: the comparison names files ABOVE the reviewed root, which have no
    spelling relative to it, so it is made against the repository root. The short names
    hide the difference and the legend cannot state it — one excluded file is the directory
    `sub/vendor` here and the file `vendor/lib.py` under *Not read*, and the legend spells
    each in full against its own base with nothing on the row to say which. So the sentence
    names where the reviewed root sits, which is what lets a reader tell the legend's two
    halves apart. Where the roots coincide the clause is left out: explaining a difference
    that does not exist is a sentence a reader has to read for nothing.
    """
    coverage = inventory["coverage"]
    if coverage["state"] != "computed":
        return ["- Reachability: the reviewed set was not compared with the files the "
                "repository tracks, so it is unknown whether anything that reaches these "
                "defects was left out.\n"]
    missing = coverage["tracked_not_reviewed"]
    if not missing:
        return ["- Reachability: every tracked file was in the reviewed set.\n"]
    reviewed = [entry["path"] for entry in inventory["files"]]
    base = (f" The directories below, and their full paths in the legend, are relative to "
            f"the repository root; every other path in this report is relative to the "
            f"reviewed root, which sits at `{_one_line(coverage['root'])}/` inside it."
            if coverage["root"] else "")
    out = [f"- **Reachability**: {_plural(len(missing), 'tracked file')} "
           f"{'is' if len(missing) == 1 else 'are'} not in the reviewed set, while "
           f"{_plural(len(reviewed), 'file')} {'is' if len(reviewed) == 1 else 'are'}, "
           f"so nothing below can say whether they reach these defects.{base}\n"]
    adjacent = _reachability_dirs(inventory)
    if not adjacent:
        out.append("  - None of them sits in a directory this run read, so any unreviewed "
                   "caller is in a part of the repository the job did not go near at all.\n")
        return out
    shown = adjacent[:_ADJACENT_CEILING]
    out.append(f"  - Unreviewed files sit beside reviewed ones in "
               f"{_plural(len(adjacent), 'directory', 'directories')}, which is where an "
               f"unreviewed caller is most likely to be:\n")
    out += [f"    - {_one_line(names.short(name) or '.')} — {_plural(count, 'file')}\n"
            for name, count in shown]
    if len(adjacent) > len(shown):
        out.append(f"    - and {len(adjacent) - len(shown)} more.\n")
    return out


PATH_LEGEND_HEADING = f"\n### {SUBSECTION_LEGEND}\n\n"


def _printed_paths(inventory: dict, files_of: dict, reading: Sequence[dict],
                   findings: Findings, work: Sequence[dict], by_id: dict) -> list[str]:
    """Every path this report is about to print, and nothing else.

    The legend is rendered from these, so a path gathered here that never reaches the
    document is a row decoding a name nobody reads, and one the document prints that is
    missing here is spelled in full beside its own short form. It MIRRORS the renderer
    rather than being read back out of it, because the legend sits above the body and the
    body is what decides which paths appear. A test over a rendered report holds the two
    together in both directions, which is the only thing that keeps the mirror honest.

    Every cluster's own location renders somewhere — the index and the body for work, the
    refuted list for the rest — and a MEMBER's file renders in the by-file table and beside
    a member inside a defect that holds more than one, both of which are the work clusters
    and neither of which renders at all when there is no work to show.
    """
    paths = [cluster["file"] for cluster in findings.clusters]
    if findings.candidates and work:
        paths += [by_id[cid]["file"] for cluster in work for cid in cluster["members"]]
    # Every coverage gap renders its own location too — the standing ones under their file
    # heading, the covered ones in the appendix table — and a path the document prints and
    # the legend does not decode is a short name a reader cannot place.
    paths += [cluster["file"] for cluster in findings.coverage_clusters]
    paths += list(inventory["excluded"])
    paths += [entry["path"] for entry in inventory["skipped"]]
    for unit in reading:
        if unit["state"] != UNIT_COMPLETE and unit["area"] is not None:
            paths += list(files_of[unit["area"]])
    paths += [name for name, _ in _reachability_dirs(inventory)[:_ADJACENT_CEILING] if name]
    return paths


def _render_limits() -> list[str]:
    """What a panel cannot establish, said on the page a person reads.

    Yield is not recall, and this method improves the first. A reader holding a long,
    well-organized report reads thoroughness into it, and the two numbers move
    independently: the count of defects goes up with more areas, more lenses and more
    agents willing to raise a doubt, while whether the one bug that matters is among them
    does not follow it.

    **Unconditional, and unconditionally the same words.** It is true at every rung and of
    every tree, so nothing about a run varies it; a caveat that appeared only on thin runs
    would read as an apology for that run rather than as a property of the method.
    """
    return [
        f"\n### {SUBSECTION_LIMITS}\n\n",
        "**Reporting more defects is not the same as catching more of the bugs that are "
        "there, and a panel improves the first.** Measured over one tree "
        "against five production bugs its owner already knew about: three runs found 3, "
        "then 2, then 1 of the five, while the count of defects they reported went 29, "
        "then 100, then 147. The reports got steadily better and the chance of one naming "
        "a particular bug you already have got worse.\n\n",
        # This paragraph sits BEFORE the bullets, not after them. The page conversion is
        # not a Markdown parser: a paragraph following a list continues inside the last
        # `<li>`, so a closing caveat placed at the end would render as part of the final
        # bullet and read as scoped to it.
        "This page is organized, honest about its coverage and plainly written. That "
        "makes it more convincing than a rough one, and leaves it just as incomplete. "
        "Read it as a set of claims that were raised and checked, never as a verdict on "
        "the tree.\n\n",
        "Two things follow, and making a report nicer fixes neither.\n\n",
        "- **A clean sweep is not evidence this tree is sound.** It is evidence only of what "
        "a set of agents raised while reading a set of areas. Nothing here looks "
        "specifically for the defects that matter most to you. The only way to find out "
        "whether a run would have caught one is to test the run against a bug you "
        "already know about.\n",
        "- **A long report is not a thorough one.** Volume comes from more areas, more "
        "lenses and more agents willing to raise a doubt. Whether the hard defect is in "
        "here is a different question from how many entries there are, and it is the "
        "number of entries that goes up.\n\n",
        # No single-asterisk emphasis anywhere in here: the page conversion recognizes
        # `**` alone, so one pair of stars reaches a reader as two literal characters.
    ]


def _render_by_tier(findings: Findings) -> list[str]:
    """One row per tier, one column per severity, counts in the cells.

    At four hundred defects the ranked index is four hundred rows and the by-file view is a
    directory listing; a reader arriving at either can work through the list and still not
    know what SHAPE the run has — which theme holds the blockers and which holds forty
    nits. The synthesis round already produced the grouping that answers it, and counting
    it costs nothing.

    Counts and not entries, deliberately. The report allows exactly three views of the
    defect list and this is not a fourth: there is nothing here to work down, and the
    severity columns are a census rather than a second ranking.

    **Written only where the round produced tiers.** Without them every defect falls in one
    unnamed group, and a table of one row is the line of totals the summary states four
    bullets above.
    """
    tiers = (findings.synthesis or {}).get("tiers") or ()
    if not tiers:
        return []
    grouped = _by_tier(findings.clusters, tiers)
    out = [f"\n### {SUBSECTION_BY_TIER}\n\n",
           "What the run found, grouped by the themes the judgment round named. The table "
           "holds counts only; the three views below are the lists to work from.\n\n",
           "\n| Tier | " + " | ".join(level.capitalize() for level in SEVERITIES)
           + " | Defects |\n",
           "|---|" + "---|" * (len(SEVERITIES) + 1) + "\n"]
    for tier, held in grouped:
        counts = [sum(1 for c in held if c["severity"] == level) for level in SEVERITIES]
        out.append(f"| {_cell(tier or UNTIERED_GROUP)} | "
                   + " | ".join(str(n) for n in counts)
                   + f" | {len(held)} |\n")
    return out


def _render_legend(names: PathNames) -> list[str]:
    """The table that decodes every short name in the document.

    It is what makes the shortening safe rather than lossy: a reader who cannot place
    `core.py` has one table to look in, and two files of one name are two rows here with
    the suffix that tells them apart already in the name above.
    """
    rows = names.rows()
    out = [PATH_LEGEND_HEADING]
    if not rows:
        out.append("No file or directory is named in this report.\n")
        return out
    out.append("Every file and directory this report names, under the short name the "
               "report uses for it. A path is spelled in full only here and under the defect "
               "it locates, so a name that appears twice below is the same file both times. "
               "Two things are copied as they were rather than written by the report, and "
               "keep their own spelling: a sentence quoted from a worker, and the job printed "
               "under **The job**. Shortening a path in either would be editing a record, and "
               "the job has to stay something a reader can paste. Where this page would "
               "otherwise lose the whitespace in a path, that whitespace is escaped below, "
               "so the spelling names one file and no other.\n\n")
    out += ["| Short name | Full path |\n", "|---|---|\n"]
    out += [f"| {_cell(short)} | {_cell(_spelled(full))} |\n" for short, full in rows]
    return out


def _lens_tags(job: dict, areas: Sequence[dict]) -> dict[str, str]:
    """Every lens this run could cite, keyed by its text, under the short tag the document
    names it by.

    Ordered by where the job DECLARES a lens — the job's own list first, then each area's
    list in area order — and never by where a raiser first mentions one. The job block in
    section 1 and the provenance table in the appendix both render from this one map, so a
    tag means the same lens in both. Numbered from the raisers instead, the appendix could
    hand ``L1`` to the lens the block above it called ``L2``, and a reader following a tag
    from one to the other would be reading the wrong instruction.

    Every reader's lens comes from its area's list in ``areas.json``, so this covers every
    lens a raiser can carry. A job lens no area kept — one every area overrode — is still
    tagged: it is a field of the job, and the block states the job.
    """
    tags: dict[str, str] = {}
    for text in (*job["lenses"], *(lens for area in areas for lens in area["lenses"])):
        tags.setdefault(text, f"L{len(tags) + 1}")
    return tags


def _lens_items(texts: Sequence[str], tags: dict[str, str], indent: str) -> list[str]:
    """One bullet per lens, tagged, at the depth the list sits at."""
    return [f"{indent}- **{tags[text]}** {_one_line(text)}\n" for text in texts]


def _tracked_total(inventory: dict) -> int | None:
    """How many files the repository tracks, or ``None`` where nobody asked git.

    Derived rather than stored, from the two directions ``plan`` recorded: what is tracked
    and unreviewed, plus what was reviewed less the part of it git does not track. Storing a
    third number beside those two would be a number that could disagree with them.
    """
    coverage = inventory["coverage"]
    if coverage["state"] != "computed":
        return None
    return (len(inventory["files"]) - len(coverage["reviewed_not_tracked"])
            + len(coverage["tracked_not_reviewed"]))


def _by_subject(areas: Sequence[dict]) -> list[tuple[str, int, int, tuple[str, ...]]]:
    """Each declared subject in area order: its name, how many files its parts hold between
    them, how many areas it became, and the lens list its readers were handed."""
    out: list[tuple[str, int, int, tuple[str, ...]]] = []
    seen: dict[str, int] = {}
    for area in areas:
        name = area["subject"] or ""
        if name in seen:
            subject, files, parts, lenses = out[seen[name]]
            out[seen[name]] = (subject, files + len(area["files"]), parts + 1, lenses)
            continue
        seen[name] = len(out)
        out.append((name, len(area["files"]), 1, tuple(area["lenses"])))
    return out


def _marked(field: str, notes: dict[str, str] | None) -> str:
    """The mark a defaulted field carries, and nothing for a field somebody chose."""
    return " (default)" if notes is not None and notes.get(field) == "defaulted" else ""


def _shape_of_the_run(job: dict, inventory: dict, areas: Sequence[dict]) -> str:
    """What this run read, as how much, cut how, under how many instructions — one sentence.

    It is the one part of the job that does NOT wait in the appendix, and the reason is that
    section 1 counts areas: the rung line says how many were read by both models and the
    coverage line how many earned an auditor. Whether an area is a slice the engine cut or a
    subject somebody named is what those numbers mean, so a reader sent to the appendix to
    find out is reading numbers they cannot interpret where they are printed.
    """
    read = (f"the {_plural(len(job['files']), 'file')} the job names" if job.get("files")
            else f"the {_plural(len(inventory['files']), 'file')} under the job's root")
    cut = (f"{_plural(len(areas), 'area')} from the "
           f"{_plural(len(_by_subject(areas)), 'subject')} the job named"
           if job["partition"] == "subject" else
           f"{_plural(len(areas), 'area')} the engine cut along directory boundaries")
    return (f"This run read {read} as {cut}, under the "
            f"{_plural(len(job['lenses']), 'lens', 'lenses')} the job names.")


def _job_pointer(job: dict, notes: dict[str, str] | None, inventory: dict,
                 areas: Sequence[dict]) -> list[str]:
    """The paragraph under the problem statement, which is what makes the appendix a
    destination rather than a place a reader never learns about.

    Three things, and it is short because everything past them is one jump away: that the
    statement is one field of a job; the shape of the run, because section 1's own counts are
    over areas; and **who settled the rest** — the fields are somebody's answers, given before
    the panel read anything, and a reader who assumes the panel chose its own scope reads every
    coverage claim on the page wrongly.
    """
    out = [f"\n{_shape_of_the_run(job, inventory, areas)} The rest of the job is in the "
           f"appendix, at [this link]({_anchor(SUBSECTION_THE_JOB)}): the root, what was "
           f"excluded and the instruction each reader was handed, together with the job "
           f"itself and what it takes to run the same audit again.\n\n"]
    # Who decided. Said differently according to what the run directory can support: with the
    # interview's record beside the job this is a fact, and without it the honest sentence is
    # that the fields were settled beforehand and nothing says by which route.
    if notes is not None:
        out.append("Those fields were settled before the panel read anything, in an interview "
                   "with whoever asked for the audit. Where nobody answered, the skill applied "
                   "its own default, and that section marks each one.\n")
    else:
        out.append("Those fields were settled before the panel read anything, and not by the "
                   "panel: either in an interview with whoever asked for the audit, or handed "
                   "over ready-made as a job file. Nothing in the run directory records which.\n")
    return out


RERUN_HEADING = "To run this audit again"
# What the printed copy of the job says instead of the root. The page states exactly one
# absolute path — the run directory — so the operator's filesystem is not in a document read
# somewhere else, and the one field that would put it there is the one field a reader has to
# retype anyway: the tree is on THEIR disk, at THEIR path.
ROOT_PLACEHOLDER = "/absolute/path/to/the/tree"


def _printed_job(job: dict) -> list[str]:
    """The job itself, as JSON, so it survives the run directory.

    **A reader will not have `job.json`.** The report is what gets sent on — pasted into a
    message, attached to a ticket, read on another machine weeks later — and every instruction
    for running the audit again named a file that is not travelling with it. Printed here, the
    job can be copied out of the page and handed straight back to a runtime, and the audit is
    reproducible from the document alone.

    Rendered from the record the run directory keeps, with the key order the author wrote,
    which is the order the format's own documentation uses. ``root`` is the one substitution:
    see :data:`ROOT_PLACEHOLDER`.
    """
    shown = {key: (ROOT_PLACEHOLDER if key == "root" else value) for key, value in job.items()}
    return [f"\nThe job, to paste back in when you do not have the file itself. `root` is "
            f"the only field replaced: the tree is on your disk, at your path, and the only "
            f"path on this page from the machine the audit ran on is the run directory "
            f"above:\n",
            *_fenced(json.dumps(shown, indent=2, ensure_ascii=False).splitlines(),
                     indent="", lang="json")]


def _rerun_instructions(job: dict, rundir_stated: bool) -> list[str]:
    """What it takes to run the same audit again, in full, where a reader will look for it.

    **Under a heading of its own, and that is structural rather than decorative.** A paragraph
    written straight after a bullet list is folded INTO the last bullet by the page conversion,
    which closes a list on a heading or a margin fence and on nothing else — so the recipe
    rendered as a continuation of the coverage line. A fourth-level heading is what the rest of
    the appendix already uses for a part inside a subsection, it closes the list, and it stays
    out of the contents list, which holds sections and subsections only.

    The job file is named as a file and the run directory is not spelled a second time: the
    page states exactly one absolute path, under "How this ran" directly above this, and a
    copy of it inside a command would make it two. What this page genuinely cannot supply is
    said rather than left as a command that does not work — the adapter configuration was
    never in the run directory, and a recipe that implied otherwise would fail on the line
    nobody warned about.
    """
    where = ("the run directory named above" if rundir_stated
             else "the run directory this report was built from")
    return [f"\n#### {RERUN_HEADING}\n",
            *_printed_job(job),
            f"\nIf you still have the run directory, the same job is `{JOB_FILE_NAME}` "
            f"in {where}. "
            f"Copy it out before running: the driver creates the run directory itself and "
            f"refuses one that already holds a run, so a new run cannot be planned in the "
            f"old directory.\n",
            *_fenced([f"cp <run directory>/{JOB_FILE_NAME} ./{JOB_FILE_NAME}",
                      "python3 review_panel_run.py run \\",
                      f"        --job ./{JOB_FILE_NAME} \\",
                      "        --rundir <a directory that does not exist yet> \\",
                      "        --adapter <your adapter configuration> \\",
                      "        --go"], indent=""),
            f"\nThree things this page cannot fill in. `--rundir` has to name a directory that "
            f"does not exist yet, outside the reviewed tree and outside every git repository. "
            f"`--adapter` is the configuration saying which runtime runs each lane. What ran "
            f"this time is described under **{SUBSECTION_HOW_IT_RAN}** above, but the "
            f"configuration itself was never in the run directory. And the tree has to be the "
            f"one this report was read from, at the commit and tree sha256 named there: the job "
            f"pins which files are read and not which bytes they hold, so the same job over a "
            f"changed tree is a different audit.\n"]


def _render_the_job(job: dict, notes: dict[str, str] | None, inventory: dict,
                    areas: Sequence[dict], tags: dict[str, str],
                    rundir_stated: bool = False, answered: bool = False) -> list[str]:
    """The appendix subsection: the job the run answers, one line per field, and what it takes
    to ask for it again.

    Rendered from ``job.json``, ``job-notes.json``, ``areas.json`` and ``inventory.json`` and
    from nothing a worker wrote, so it cannot disagree with what ran — every count of areas or
    of files is read off the record of the partition rather than described. The problem
    statement under the title is one of these fields, and a report that carried it alone said
    nowhere that the rest existed: a reader who had not written the job could not tell how the
    tree was divided, what each reader was told to read for, which files were in scope, or where
    the file is that would let them run the same audit again.

    In the APPENDIX, and reachable from a pointer under the statement, by the same rule as the
    path legend: nothing here is needed to fix a defect, and this is where a reader auditing the
    run is already looking. The pointer is what makes that a move rather than a burial.

    **The root directory is named as a field and not as a path.** The page states exactly one
    absolute path — the run directory, so a reader has a way back to the evidence — and the
    operator's filesystem is otherwise not in a document that is read somewhere else. The
    root is one line away, in the job file this block names.
    """
    out = [f"\n### {SUBSECTION_THE_JOB}\n\n",
           f"What this run was asked for. The problem statement under the title is one of these "
           f"fields, quoted there because it is the one a reader acts on; the rest are here.\n"]
    # Said here rather than as a line of the list, because it is about the list and not a
    # field in it. A reader scanning the fields would read it as one.
    out.append(f"A field marked (default) is one nobody stated and the interview filled in.\n"
               if notes is not None else
               f"Which fields the owner asked for and which the interview filled in is not "
               f"recorded: there is no `{JOB_NOTES_FILE_NAME}` beside the job, so a job handed "
               f"in ready-made and one the interview wrote look the same here.\n")
    out.append("\n- Read for: the statement under the title, carried verbatim into every "
               "reader's payload.\n")
    # The root as a FIELD, not as a path: see the note above.
    out.append(f"- Tree: the root the job names. It is in `{JOB_FILE_NAME}`, not on this "
               f"page: the only path on this page from the machine the audit ran on is the "
               f"run directory above.{_marked('root', notes)}\n")
    if job.get("files"):
        tracked = _tracked_total(inventory)
        # Why the reachability line below counts so much of the repository as unreviewed. A
        # list in place of the tree is the fact that explains it, and the report never said
        # a list had been given.
        rest = (f"; the tree tracks {_plural(tracked, 'file')} in all"
                if tracked is not None else "")
        out.append(f"- Scope: the {_plural(len(job['files']), 'file')} the job names, in "
                   f"place of everything under its root{rest}."
                   f"{_marked('files', notes)}\n")
    else:
        out.append(f"- Scope: everything under the job's root.{_marked('files', notes)}\n")
    if job["exclude"]:
        out.append(f"- Excluded: {_plural(len(job['exclude']), 'path')} the job named; "
                   f"**Not read**, under Coverage below, lists the files they took out."
                   f"{_marked('exclude', notes)}\n")
    else:
        out.append(f"- Excluded: nothing.{_marked('exclude', notes)}\n")
    out += _divided(job, areas, tags, notes)
    # Which list a reader was handed is the area's, not always the job's, so the sentence
    # cannot claim every area read under the lenses below it. Only a subject-mode job can
    # override: an area is a declared subject there, and in file mode the engine makes them.
    overridden = job["partition"] == "subject" and any(
        tuple(lenses) != tuple(job["lenses"]) for _s, _f, _p, lenses in _by_subject(areas))
    each = (" of the list that applies to it (the job's, unless the area above names its own)"
            if overridden else "")
    out.append(f"- Read with {_plural(len(job['lenses']), 'lens', 'lenses')}. A lens is the "
               f"instruction a reader is handed for how to read its area. Every area was "
               f"read once per lens{each}, and the lenses were assigned to the two model "
               f"lanes in turn.{_marked('lenses', notes)}\n")
    out += _lens_items(job["lenses"], tags, "  ")
    out.append(f"- Coverage: {_coverage_choice(job, areas)}{_marked('coverage', notes)}\n")
    if job.get("questions"):
        where = (f"answered under **{SECTION_OPERATOR}**" if answered else
                 f"and no answer was recorded in `{REPORT_NOTES_FILE_NAME}`")
        out.append(f"- Questions: put to whoever ran the panel rather than to its readers, "
                   f"{where}.\n")
    out += _rerun_instructions(job, rundir_stated)
    return out


def _divided(job: dict, areas: Sequence[dict], tags: dict[str, str],
             notes: dict[str, str] | None) -> list[str]:
    """How the files in scope became areas. The mode decides what an area IS, and so what
    every per-area count in this report is counting — which the report never stated."""
    mark = _marked("partition", notes)
    if job["partition"] != "subject":
        return [f"- Divided: by file — the engine cut the files in scope into "
                f"{_plural(len(areas), 'area')} along directory boundaries, under a size "
                f"ceiling.{mark}\n"]
    subjects = _by_subject(areas)
    # SUBJECTS that were split, not the pieces they were split into: a subject cut in three is
    # one subject split, and counting the pieces claims three of them were.
    split = sum(1 for _s, _f, parts, _l in subjects if parts > 1)
    how = (f"the engine split {split} of them for size, giving {_plural(len(areas), 'area')}"
           if split else "one area each")
    out = [f"- Divided: by subject — the job named {_plural(len(subjects), 'subject')}; "
           f"{how}.{mark}\n"]
    for subject, files, _parts, lenses in subjects:
        own = tuple(lenses) != tuple(job["lenses"])
        tail = ", read with its own lenses in place of the job's:" if own else ""
        out.append(f"  - {_one_line(subject)} — {_plural(files, 'file')}{tail}\n")
        if own:
            out += _lens_items(lenses, tags, "    ")
    return out


def _coverage_choice(job: dict, areas: Sequence[dict]) -> str:
    """Whether the job asked for the coverage round, declined it, or left it to the tree —
    and, where it could run, how much of the tree earned an auditor."""
    if coverage_declined(job):
        return ("declined by the job, so no auditor was planned however many tests the tree "
                "holds.")
    earned = sum(1 for area in areas if area["audited"])
    asked = "asked for by the job" if job.get("coverage") else "left to the tree"
    return f"{asked} — {earned} of {_plural(len(areas), 'area')} earned an auditor."


def _rung_line(dispatch: DispatchRecord, reading: Sequence[dict], areas: Sequence[dict],
               findings: Findings) -> str:
    """The rung and what it lets the report claim, counted from the data. An area counts
    as read by both lanes only when both its readers completed; a candidate counts as
    checked by the other lane when a verdict came back, and as left unresolved otherwise —
    a failed or missing verification unit included. Every candidate carries one raiser, so
    every one of them has an other lane and there is no third case to count."""
    units = _plural_states([u["state"] for u in reading])
    readers: dict[str, list[bool]] = {}
    for unit in reading:
        if unit["kind"] == READER_KIND:
            readers.setdefault(unit["area"], []).append(unit["state"] == UNIT_COMPLETE)
    both_read = sum(1 for area in areas if readers.get(area["id"]) and all(readers[area["id"]]))
    unresolved = sum(1 for record in findings.candidates if record["status"] == "unresolved")
    other = len(findings.candidates) - unresolved
    name, coverage = RUNG_NAMES[dispatch.rung], f"{both_read} of {_plural(len(areas), 'area')}"
    if dispatch.rung == RUNG_TWO_RUNTIMES:
        return (f"{name}. Reading units: {units}; {coverage} read by both models. "
                f"Candidates: {len(findings.candidates)} — {other} checked by the other model, "
                f"{unresolved} left unresolved. Where the lanes disagree, the disagreement "
                f"is between two different models.")
    return (f"{name}. Reading units: {units}; {coverage} read by both contexts. "
            f"Candidates: {len(findings.candidates)} — {other} checked by a fresh context of "
            f"the same model, not the one that raised them, {unresolved} left unresolved. "
            f"Where the lanes disagree, the disagreement is between two contexts of one "
            f"model, and the report claims no more than that.")


def render_report(job: dict, dispatch: DispatchRecord, inventory: dict, areas: Sequence[dict],
                  findings: Findings, reading: Sequence[dict], batches: Sequence[dict],
                  states: Sequence[VerificationState], probe: dict,
                  *, rundir: Path | None = None, generated: str | None = None,
                  job_notes: dict[str, str] | None = None,
                  report_notes: ReportNotes | None = None) -> str:
    """The whole report from the run directory's data and nothing else — no clock, and no
    path but the run directory it names — so two runs over one tree into one run directory
    render byte-identical reports, and two into different ones differ on that line alone.

    The run directory is the one fact here that is about the run rather than about the
    tree, and it is stated because a report outlives the session that made it: without it
    a reader holding the page three weeks later has no way back to the payloads, the
    per-unit results or the transcripts. That is a deliberate narrowing of the rule above
    and not an exception to it — every OTHER path in this document is relative to the
    reviewed root, and a second absolute path appearing anywhere is the defect this
    sentence catches.

    ``job`` is ``job.json`` as the run directory keeps it, and the report's first subsection
    is that record — the problem statement is one of its fields, and the rest of them say how
    the tree was divided, what each reader was told to read for and what was in scope. It is
    the whole record rather than the statement alone because a field the renderer cannot see
    is a field the report cannot state, and the statement on its own is what a reader who had
    not written the job was left with. ``job_notes`` is ``job-notes.json`` where the interview
    left one, saying which fields nobody chose; absent, no field is marked.
    ``report_notes`` is ``report-notes.json`` where the operator wrote one. It adds a section
    and a mark beside what it corrects, and changes no count: the counts are what the panel
    found, and stay comparable between runs.

    ``findings`` is every defect fact the report states: the candidate records, the cluster
    records and one record per area clustering touched. Nothing about a defect is computed
    here, which is what keeps ``report.md`` and ``findings.json`` from disagreeing.

    ``reading`` is every reading unit's state as ``candidates.json`` recorded it;
    ``batches`` the verification units as ``units.json`` lists them; ``probe`` the capability
    probe's record from the same file. The probe is kept out of the reading-unit counts, which
    are what the rung sentence claims coverage from — but it is a unit, and a failed one is a
    gap the coverage section has to name, or the report says every unit returned a valid
    result while one did not.

    ``rundir`` is where the run's payloads, results and transcripts sit; the report states
    it so a reader holding the page weeks later has a way back to them. ``generated`` is
    when this rendering was made, for the subtitle under the title. Both are keyword-only
    and default to nothing, because neither is a fact about the run's findings: a caller
    rendering from data it has already read — a test, or a re-render of a directory it was
    handed by another name — has no run directory to state and no claim to make about a
    clock, and omitting each is the honest answer for it. ``job_notes`` is keyword-only for
    a different reason: absent is the run's ordinary state and not a caller's omission.
    Every positional argument is required exactly because a report missing one of them would
    be wrong rather than shorter.

    **The clock is passed IN and never read here.** Two runs over one tree render the same
    bytes but for the lines that are about the run rather than about the tree, and a
    renderer that called the clock itself would make that untestable — the difference
    between two renderings would no longer be a thing a caller could hold still.
    """
    problem = job["problem"]
    # One map for the whole document: the job block spells each lens out under its tag and the
    # provenance table cites the tag. Built here rather than in either of them, because the
    # two agreeing is the only thing that makes the tag a reference at all.
    tags = _lens_tags(job, areas)
    files_of = {area["id"]: area["files"] for area in areas}
    by_id = {record["id"]: record for record in findings.candidates}
    ordered = sorted(findings.clusters, key=_defect_order)
    # `ordered` ranks the work. Refuted defects are ranked by nothing: they are listed in
    # the appendix in id order, which is the order they were raised in.
    work = [c for c in ordered if _is_work(c)]
    refuted_count = len(ordered) - len(work)
    # Every area clustering touched, split the two ways the coverage section reports them:
    # the units units.json listed, which are what the unit counts are over, and the areas
    # that came back unusable or were never dispatched one, which are what cost a merge.
    listed = [entry for entry in findings.clustering if entry["listed"]]
    ungrouped = [entry for entry in findings.clustering if entry["state"] != UNIT_COMPLETE]
    # Gathered before a line is written, because the legend is printed above the body that
    # decides which paths appear in it.
    names = PathNames(_printed_paths(inventory, files_of, reading, findings, work, by_id))
    # The subtitle: what this report is OF, on one line under the title. A report outlives
    # the session that made it, and a reader holding one has to know which tree it was read
    # from before a single defect in it means anything. The source facts are deterministic
    # -- how many files, at which commit -- and are stated whatever the caller passed; the
    # clock is not, so it is stated only where a caller hands one over. Rendered as an
    # ordinary paragraph and set smaller by the page's `h1 + p` rule, because Markdown has
    # no size and a heading would put it in the contents list.
    # What this report is OF. The commit's DATE rides with it where the record has one:
    # a sha says which tree and nothing about how old it is, and "eight files from commit
    # 832123fd" leaves a reader holding the page unable to tell last week's work from last
    # year's. Only here — every defect and every snippet names the same commit, and a date
    # repeated on each of them is a date nobody reads.
    dated = _one_line(_committed_clock(inventory["commit"].get("committed") or ""))
    said = [f"{_plural(len(inventory['files']), 'file')} from "
            f"{_commit_words(inventory['commit'], True)}"
            + (f", committed {dated}" if dated else "")]
    # WHEN THE READING HAPPENED, which is not when the page was drawn. A report re-rendered
    # weeks after its run said "Generated <today>" and nothing else about time, so a month
    # old reading read as this morning's. Stated only where the two differ by a day or
    # more -- on the run that wrote the report, saying both is one fact twice.
    taken = _one_line(inventory.get("taken", ""))
    if taken and (generated is None or taken[:10] != _one_line(generated)[:10]):
        said.insert(0, f"Read {taken}")
    if generated is not None:
        said.insert(0, f"Generated {_one_line(generated)}")
    out = ["# Review panel report\n", f"\n{' · '.join(said)}\n",
           f"\n{PROMPT_LEAD_IN}\n\n"]
    # Verbatim as prose, not as markup: the statement is whatever the job said, and a job
    # naming an HTML element should read as that name rather than become one.
    out += [f"> {_entities(line)}\n" for line in problem.splitlines()]

    # Directly under the statement, because that is what it is about. The detail is in the
    # appendix and this is what keeps the move from trading one obstruction for a dead end:
    # it carries the shape of the run in one sentence, says who settled the rest, and names
    # the section. A bare "see the appendix" would put the original defect back.
    out += _job_pointer(job, job_notes, inventory, areas)

    sections = _Sections()
    out.append(sections.top(SECTION_DESCRIPTION))
    # The document explained to somebody who has never seen one. Everything below assumes
    # the vocabulary — defect, established, unresolved, appendix — and a reader who has to
    # infer it from the sections reads the first third of the report twice.
    # The method, never a coverage claim: what was actually read, checked and reached is
    # the rung line below, and saying any of it twice puts the two out of step. Nor is
    # blindness claimed here — the rung is what permits that word, and the rung line makes
    # the claim once.
    out.append("A panel of agents read this tree in bounded pieces. Whatever one agent "
               "raised was sent to another agent that had not raised it, to be checked.\n")
    out.append("A **defect** here is one thing to fix. Reports that describe the same "
               "problem are merged into one defect, and the appendix sets out how they "
               "were merged, so the count can be challenged.\n")
    out.append("**Established** means a checker that had not raised the claim upheld it. "
               "**Unresolved** means nothing settled it, and the group it sits under "
               "says what would. **Refuted** means a check dismissed it, so there is "
               "nothing to do about it; it is listed in the appendix with the reason.\n")
    # What the appendix holds is set out at the top of the appendix itself; what a reader
    # needs here is only that there is one and that skipping it costs them nothing.
    out.append("The **appendix** at the end is the record of the run rather than the work "
               "it found. Nothing in it is needed to fix anything.\n\n")
    counts = {status: sum(1 for c in findings.clusters if c["status"] == status)
              for status in (DEFECT_ESTABLISHED, DEFECT_REFUTED, DEFECT_UNRESOLVED)}
    out.append(f"- By status: {counts[DEFECT_ESTABLISHED]} established, "
               f"{counts[DEFECT_REFUTED]} refuted, "
               f"{counts[DEFECT_UNRESOLVED]} unresolved.\n")
    corrected = report_notes.corrected() if report_notes is not None else frozenset()
    disputed = [c["id"] for c in sorted(findings.clusters, key=lambda c: _id_rank(c["id"]))
                if c["id"] in corrected]
    if disputed:
        out.append(f"  - The operator corrected {len(disputed)} of these: "
                   f"{', '.join(f'[{did}](#{did})' for did in disputed)}. The counts are "
                   f"the panel's own and stay as they are; the corrections are under "
                   f"**{SECTION_OPERATOR}**.\n")
    verdicts = {status: sum(1 for r in findings.candidates if r["status"] == status)
                for status in VERDICT_STATUSES}
    out.append(f"- Candidates behind them: {len(findings.candidates)} — "
               f"{verdicts['reproduced']} reproduced, "
               f"{verdicts['confirmed_by_reading']} confirmed by reading, "
               f"{verdicts['refuted']} refuted, {verdicts['unresolved']} unresolved.\n")
    # Section 2 asks for the merge's arithmetic in the report rather than only in the code:
    # a reader counting defects can check the two numbers against each other.
    out.append(f"- Defects: {len(findings.clusters)} from "
               f"{_plural(len(findings.candidates), 'candidate')}; "
               f"every candidate is in exactly one cluster.\n")
    # Counted apart, and never folded into the line above. A coverage gap is not a defect
    # and adding the two would give a reader one number that means two things.
    if findings.coverage_clusters:
        gaps = {status: sum(1 for c in findings.coverage_clusters if c["status"] == status)
                for status in (DEFECT_ESTABLISHED, DEFECT_REFUTED, DEFECT_UNRESOLVED)}
        parts = [f"{gaps[DEFECT_ESTABLISHED]} standing",
                 f"{gaps[DEFECT_REFUTED]} covered by a test already",
                 f"{gaps[DEFECT_UNRESOLVED]} unresolved"]
        out.append(f"- Coverage gaps: {len(findings.coverage_clusters)} from "
                   f"{_plural(len(findings.coverage), 'finding')} — {', '.join(parts)}. "
                   f"They are tests to write, not defects, and are counted apart from the "
                   f"line above.\n")
    if ungrouped:
        out.append(f"- {_plural(len(ungrouped), 'area')} left unclustered, so a defect "
                   f"raised twice there is counted twice: "
                   f"{', '.join(entry['area'] for entry in ungrouped)}.\n")
    out.append(f"- Reading units: {_plural_states([u['state'] for u in reading])}.\n")
    out.append(f"- Verification units: {_plural_states([s.state for s in states])}.\n")
    if listed:
        out.append(f"- Clustering units: {_plural_states([e['state'] for e in listed])}.\n")
    # The three facts a reader needs before any finding: what this run could execute, what
    # it could not reach, and at what rung it ran. They sat under "How this ran", above the
    # first defect and below four lines of adapter strings; the rung is what every claim
    # below is bounded by, so it belongs with the counts it bounds.
    out.append(f"- Rung: {_rung_line(dispatch, reading, areas, findings)}\n")
    out += _executability(probe, findings, corrected)
    out += _reachability(inventory, names)
    # The legend itself is in the appendix; this is the pointer to it. One row per file the
    # report names is hundreds or thousands of lines on a real tree, and sitting here it
    # stood between the summary a reader came for and the first defect they came to fix.
    # It is a decoder -- consulted when a short name is unfamiliar and ignored otherwise --
    # which is what the appendix is for. Moving it without leaving this line would trade
    # one obstruction for a dead end.
    out.append(f"- This report names every file by a short name; the **{SUBSECTION_LEGEND}**, "
               f"in the appendix, gives each one's full path.\n")
    out += _render_judgment_overview(findings)
    out += _render_by_tier(findings)
    # Last under the description, and after the counts rather than before them: it is a
    # caveat about the numbers a reader has just taken in, and the sentence about volume
    # lands on the total they are holding.
    out += _render_limits()
    if report_notes is not None:
        out += _render_operator_notes(report_notes, job, findings, probe, sections)

    # One section, two ways in. They were sibling headings, which said they were two
    # sections of the report rather than one list a reader can enter by rank or by file.
    out.append(sections.top(SECTION_INDEX))
    if not findings.candidates:
        lead, empty = "No candidate was raised.", ("Nothing to rank.", "Nothing to gather.")
    elif not work:
        lead = (f"Nothing to do: the {_plural(refuted_count, 'defect')} raised was checked "
                f"and dismissed. It is "
                f"under Refuted, in the appendix, with the reason."
                if refuted_count == 1 else
                f"Nothing to do: every one of the {_plural(refuted_count, 'defect')} "
                f"raised was checked and dismissed. They are under Refuted, in the "
                f"appendix, with the reason for each.")
        empty = ("Nothing to do; see Refuted, in the appendix.",) * 2
    else:
        # Short, because each way in states its own rule under its own heading; saying it
        # here as well puts one sentence in two places where an edit touches half of them.
        lead, empty = "One list of defects, with two ways in: by rank or by file.", None
    out.append(f"{lead}\n")
    if empty is None:
        out += _render_index(work, dispatch.rung, refuted_count, names, sections, corrected)
        out += _render_by_file(work, by_id, names, sections)
    else:
        # Both ways in are WRITTEN even with nothing to put in them. A subsection that
        # vanishes gives the page's contents list a different skeleton on every run, and
        # gives a reader no way to tell "no defects" from "this part was dropped".
        out.append(sections.sub(SUBSECTION_RANKED) + f"{empty[0]}\n")
        out.append(sections.sub(SUBSECTION_BY_FILE) + f"{empty[1]}\n")
    # The round's vocabulary, in the order it declared it, or nothing where the round was
    # not run or could not be believed. The disclaimer is scoped to what actually renders:
    # a report carrying no synthesised prose has nothing to disclaim, and a sentence that
    # is always there says nothing.
    tiers = tuple(findings.synthesis["tiers"]) if findings.synthesis else ()
    disclaim = any(_carries_a_reading(cluster) for cluster in ordered)
    out += _render_body(ordered, by_id, dispatch.rung, findings.commit, names,
                        sections, tiers, disclaim, corrected)
    out += _render_corroborated(ordered, dispatch.rung, sections)
    # Above the appendix, because a named missing test is work. Written only where the
    # coverage round produced something: a section that is always there and usually empty
    # teaches a reader to skip it.
    if findings.coverage_clusters:
        out += _render_coverage_gaps(findings, names, sections)

    # Everything below is the record of the run rather than the work it found. Section 9's
    # three views and the defects themselves are above; a person fixing something never has
    # to come down here, and a person auditing the run never has to read past it. Its parts
    # are SUBSECTIONS of it: as siblings they would be the appendix only by where they sat,
    # and a reader scanning the section list could not tell which headings were work and
    # which were the record of the run.
    out.append(sections.top(SECTION_APPENDIX))
    out.append("The record of the run rather than the work: what was dismissed, how the "
               "duplicates were merged, what was asked for and what ran where, what was not "
               "read, and which unit raised what. Nothing below is needed to fix a defect.\n")
    out += _render_refuted(ordered, by_id, names, corrected)
    if findings.coverage_clusters:
        out += _render_covered(findings, names)
    out += _render_verifier_variance(findings)
    out += _render_outside_scope(findings, names)
    out += _render_clustering_notes(findings, names)
    out += _render_synthesis_notes(findings)
    out += _render_legend(names)

    # The rung is stated ONCE, in the summary, because every claim in the report is bounded
    # by it. Repeating it here would put the same sentence in two places, where an edit to
    # one is an edit to half of them.
    out += [f"\n### {SUBSECTION_HOW_IT_RAN}\n\n"]
    # Where the evidence for all of the above is. A report outlives the session that made
    # it, and without this line a reader holding one three weeks later has no way back to
    # the payloads, the per-unit results or the transcripts it was built from.
    if rundir is not None:
        out.append(f"- Run directory, holding every payload, result and transcript this "
                   f"report was built from: `{_one_line(str(rundir))}`\n")
    # The job file is NOT a bullet here. It is what **The job**, directly below, is about, and
    # that subsection carries the whole rerun recipe — naming it twice would put one fact in
    # two places where an edit to one is an edit to half of them.
    for lane in LANES:
        record = dispatch.lanes[lane]
        out += _recorded(f"- Lane {lane} adapter, as recorded by the dispatcher:", record.adapter)
        out += _recorded(f"- Lane {lane} permission, as recorded by the dispatcher:",
                         record.permission)
    out.append(f"- {CONTAINMENT_LINE}\n")
    out.append(f"- Read: {_plural(len(inventory['files']), 'file')} ({inventory['source']}), tree "
               f"sha256 {inventory['tree_sha256']}; {len(inventory['excluded'])} excluded, "
               f"{len(inventory['skipped'])} skipped.\n")
    # The whole sha, once. Every defect and every snippet names the first ten characters,
    # which is what a person types; this is the line somebody checks the tree out from.
    out.append(f"- Pinned at: {_commit_words(findings.commit)}.\n")

    # Directly after the record of what ran it, because the two are halves of one answer —
    # what was asked, and what machinery answered it — and because the rerun instructions
    # below reach back to the run directory, the commit and the adapters named just above.
    out += _render_the_job(job, job_notes, inventory, areas, tags, rundir is not None,
                           report_notes is not None and bool(report_notes.answers))

    out.append(f"\n### {SUBSECTION_COVERAGE}\n\n")
    out.append("Partitioning put every file in scope into exactly one area, so a gap here is "
               "a unit that failed or returned nothing valid, or a path the job left out.\n")
    # An auditor that was never dispatched is not a unit that failed, so it is stated here
    # rather than in the list below — and stated it must be: the round is the one that
    # answers which inputs the tests never construct, and a report that omitted it reads as
    # one whose auditors found nothing.
    #
    # WHETHER it ran is read off the LISTING; why it did not is read off the snapshot. Every
    # other claim in this section is a fact about the units that ran, and this one has to be
    # the same fact, or a tree with no test-named file whose auditors did run prints "No
    # auditor ran" directly above the line naming what `audit-B` returned.
    if not any(unit["kind"] == AUDITOR_KIND for unit in reading):
        # Read off the job the renderer already holds, rather than passed in beside it: the
        # field is the job's, and two paths to one field is a way for the coverage line in
        # section 1 and this sentence to contradict each other.
        absence = _auditor_absence(
            auditor_plan_of_snapshot(inventory, coverage_declined(job)))
        out.append(f"\nNo auditor ran: {absence}. "
                   f"The round that asks which inputs the tests never construct did not run, "
                   f"and nothing below answers that question.\n")
    out.append("\n#### Units that failed or returned nothing\n\n")
    gaps = [u for u in reading if u["state"] != UNIT_COMPLETE]
    failed_batches = [(b, s) for b, s in zip(batches, states) if s.state != UNIT_COMPLETE]
    probe_state = probe.get("state")
    # A unit that came back COMPLETE can still hold a verdict the engine could not read,
    # and that candidate is unresolved. Saying every unit returned a valid result above a
    # list of what did not is the contradiction this clause exists to prevent. A READING
    # unit can hold one the same way, and a finding rejected there is a stronger gap than a
    # rejected verdict: no candidate exists to carry it, so nothing downstream would mention
    # it at all.
    rejected_any = (any(state.rejected for state in states)
                    or any(unit.get("rejected") for unit in reading))
    # The synthesis round is a unit like the others, so a round that never landed, failed or
    # had an entry refused is a unit that did not return a valid result. Printing the line
    # above the appendix note naming that round is the same contradiction as printing it
    # above a list of failed batches.
    synthesis = findings.synthesis
    synthesis_clean = synthesis is None or (
        synthesis["state"] == UNIT_COMPLETE and not synthesis["rejected"])
    if (not gaps and not failed_batches and probe_state == UNIT_COMPLETE
            and not ungrouped and not rejected_any and synthesis_clean):
        out.append(f"{EVERY_UNIT_RETURNED}\n")
    if probe_state != UNIT_COMPLETE:
        out += _item(f"- {probe.get('unit', PROBE_UNIT_ID)} (capability probe) — "
                     f"{probe_state}: ", str(probe.get("reason") or "no reason was recorded"),
                     "No finding is affected. What is not known is whether this tree builds "
                     "and whether its tests run, so nothing below was executed on the strength "
                     "of that answer.")
    # A reason can carry a worker's text — an unknown key is quoted by name — so it
    # goes through _item like a finding's fields.
    for unit in gaps:
        prefix = f"- {_one_line(_unit_label(unit))} — {unit['state']}: "
        if unit["area"] is None:
            out += _item(prefix, unit["reason"], "Read the area manifest and the test inventory, not an area.")
        else:
            out += _item(prefix, unit["reason"], f"Read {unit['area']}:")
            out += [f"  - {_one_line(names.short(path))}\n" for path in files_of[unit["area"]]]
    for batch, state in failed_batches:
        out += _item(f"- {_one_line(_unit_label(batch))} — {state.state}: ", state.reason,
                     f"Its candidates are reported unresolved: {', '.join(batch['candidates'])}.")
    # A verdict the engine could not read inside a unit that otherwise came back. Its
    # candidate is unresolved like any other, and this is what stops that being silent: a
    # reader can tell a candidate nobody checked from one whose answer arrived unreadable,
    # and the verifier's own contract error is on the page where somebody will see it.
    for batch, state in zip(batches, states):
        for message in state.rejected:
            out += _item(f"- {_one_line(_unit_label(batch))} — one verdict could not be "
                         f"read, and only its own candidate lost its verdict: ", message,
                         "Every other verdict in that unit stands.")
    # A finding the engine could not read inside a reading unit that otherwise came back.
    # It raised no candidate, so unlike a rejected verdict there is nothing downstream that
    # mentions it -- this line is the whole of what the reader ever learns about it, which
    # is why it names where the finding said it was and not only the rule it broke.
    #
    # The site is the worker's OWN spelling, not a short name, and that is not an oversight:
    # one of the rules that rejects a finding is a location outside the snapshot, so the
    # path may name no file the legend decodes. Shortening it would mean pretending to
    # resolve something that does not resolve. It falls under the one exemption the
    # one-spelling rule already carries -- a sentence quoted from a worker is reproduced as
    # it was written, paths and all.
    for unit in reading:
        for message in unit.get("rejected", ()):
            out += _item(f"- {_one_line(_unit_label(unit))} — one finding could not be "
                         f"read, and was not raised: ", message,
                         "Every other finding in that unit stands.")
    # A clustering unit that did not come back costs its area the merge and nothing else, so
    # it is a gap of a different kind from the two above: no finding is lost and no verdict
    # changes, and the report says which count is inflated rather than leaving the reader to
    # wonder why one defect appears twice.
    for entry in ungrouped:
        out += _item(f"- {entry['unit']} (clustering, {entry['area']}) — {entry['state']}: ",
                     entry["reason"], UNGROUPED_NOTE)

    out.append("\n#### Not read\n\n")
    if not inventory["excluded"] and not inventory["skipped"]:
        out.append("Nothing was excluded and nothing was skipped.\n")
    if inventory["excluded"]:
        out.append(f"- Excluded by the job ({len(inventory['excluded'])}):\n")
        out += [f"  - {_one_line(names.short(path))}\n" for path in inventory["excluded"]]
    if inventory["skipped"]:
        out.append(f"- Skipped, recorded and never followed ({len(inventory['skipped'])}):\n")
        out += [f"  - {_one_line(names.short(entry['path']))} — {_one_line(entry['reason'])}\n"
                for entry in inventory["skipped"]]
    # A count and nothing more: the owner left these files out to hear nothing about them,
    # and the count is what keeps the dropping from being silent.
    dropped = sum(unit.get("dropped", 0) for unit in reading)
    if dropped:
        out.append(f"- {_plural(dropped, 'finding')} located in files outside the review's "
                   f"scope {'was' if dropped == 1 else 'were'} dropped.\n")
    out += _render_provenance(findings, tags)
    return "".join(out)


def _release(lock: Path) -> bool:
    """Remove the report lock, saying whether it went.

    A lock that outlives its run refuses every later report of that directory. The refusal
    names the file so an operator can clear it — but a run that could not release its own
    says so at the time, rather than leaving the next one to discover it and guess whether
    a report is really in progress.
    """
    try:
        lock.unlink()
    except FileNotFoundError:
        return True
    except OSError:
        return False
    return True


def _released(lock: Path) -> None:
    """Release a stage's claim, and say so on stderr when it will not go.

    The stage's own outcome is what the caller reports; a claim that outlived its stage
    refuses the next run of that stage, and whoever is reading needs to know which file to
    clear rather than discovering it an hour later.
    """
    if not _release(lock):
        sys.stderr.write(f"{_PROG}: {lock} could not be removed; delete it or the next run "
                         f"of this stage will refuse\n")


def _replaceable(path: Path) -> None:
    """``report``'s check on one of its own three files: it may overwrite what it wrote,
    and nothing else.

    A symlink is refused because the write would land wherever it points, which is outside
    the run directory the stage is allowed to touch; a directory is refused because the
    replace cannot happen and the name was never ``report``'s to take. A missing file is
    fine — a re-render after a failed first run is one case the stage exists for, and a
    report interrupted between two of the three is the other.

    It runs on every report and not only on ``--rerender``, because the marker decides
    whether a report has committed and the presence of a file decides nothing. What is left
    for the flag to decide is a run already at the ``reported`` marker.

    **A path nobody can examine is refused, not allowed.** This decides whether it is safe
    to write over what is at a name, and the only answer that permits the write is a
    regular file or nothing at all. Asked through ``Path.exists`` and ``Path.is_symlink``,
    a path the host will not describe comes back as no path in the way — so the stage goes
    on to publish over whatever is actually there, and the two checks that follow it read
    the same three paths the same way and clear it too.
    """
    info = _lstat_or_absent(path, "the report output", RunDirError)
    if info is not None and not stat.S_ISREG(info.st_mode):
        raise RunDirError(
            f"{path} is in the way and is not a file this stage wrote; --rerender "
            f"overwrites its own report and findings file and nothing else"
        )



# --------------------------------------------------------------------------- #
# report.html — the report, converted; never a second report
# --------------------------------------------------------------------------- #
HTML_NAME = "report.html"
# Where a defect's way back goes: the RANKED list it was read from, which is the index and
# not the contents list below. The two are different things — the index is one of the three
# views of the defect list, and the contents is navigation over the document's sections —
# and this id names the first.
CONTENTS_ID = "index"
# The way back from a defect, and short: it sits at the end of every defect heading in the
# report, so a sentence there is a sentence repeated a hundred and fifty times. The arrow is
# the one the hover affordance uses, so the two ways back read as the same gesture.
BACK_TO_INDEX = "\u2191 Index"
# The page's own contents list, which the prose does not have and does not need. A contents
# list is navigation rather than a fact, so it is the conversion's to add, exactly as the
# id on a heading and the link back to the index already are — and `report.md` lacking one
# does not put the two documents out of step. It lists the SECTIONS and never the defects:
# repeating those would give the page a second ordering of the one list the index ranks,
# which is what section 9's bound refuses.
CONTENTS_TITLE = "Contents"
_CONTENTS_LEVELS = (2, 3)
# A heading's section number, which is the document's own count and moves whenever a
# section is added or dropped. An id built from it would move with it and break every link
# somebody saved, so the number comes off before the id is made — and the same pattern is
# what recognizes the index section whatever number it carries.
_SECTION_NUMBER = re.compile(r"^\d+[a-z]?\. ")
# And the count it carries, which moves with the run. Both come off before an id is made,
# for the same reason: a link somebody saved should still land on the same section after
# the next run over the same tree.
_SECTION_COUNT = re.compile(r" \(\d+\)$")
_SLUG_SEPARATORS = re.compile(r"[^a-z0-9]+")
# No syntax highlighting, by decision: a tokenizer written here would be approximately right
# for three languages and wrong for the rest, and the commit shown beside a snippet is worth
# more than approximate color. The style sheet is inline because the page is one file
# somebody opens out of a run directory, with nothing beside it to load.
HTML_STYLE = """\
:root { color-scheme: light dark; }
/* One measure for the whole page: prose and tables alike. A narrow column for prose beside
   a wide one for tables leaves the text pinned to the left of a half-empty screen, and a
   table squeezed into a reading measure has to be read sideways. Wide, and the same wide. */
body { font: 16px/1.55 system-ui, sans-serif; margin: 0 auto; max-width: 72em; padding: 2rem 1rem; }
/* Worker text and file names have no length a page can rely on. Break inside a long word
   in a cell, where a column is narrow; let prose wrap on word boundaries as usual. */
body, h1, h2, h3, h4, td { overflow-wrap: break-word; }
/* A column heading is a short label and must never be hyphenated down the middle:
   "Corroboration" broken as "Corrobora/tion" is the table telling the reader it ran out of
   room. Headings keep their width, the short value columns keep theirs, and the one long
   column takes whatever is left. */
th { white-space: nowrap; }
/* Only a table genuinely wider than the page scrolls, and it scrolls inside its own box
   rather than widening the document. */
.scroll { overflow-x: auto; }
table { border-collapse: collapse; width: 100%; margin: 1rem 0; }
th, td { border: 1px solid #8884; padding: 0.35rem 0.5rem; text-align: left; vertical-align: top; }
/* Short columns stay short so the long one gets the room it needs. */
td code, th code { white-space: nowrap; }
pre { background: #7f7f7f1a; border: 1px solid #8883; border-radius: 4px; padding: 0.75rem;
      overflow-x: auto; white-space: pre-wrap; }
/* Seven colours, defined for both schemes. The gutter takes tango's decimal-literal green,
   which is what a pandoc-rendered report gives a line number and reads well against code
   without competing with it. Unselectable, so copying a snippet does not drag the numbers
   along with it. */
.hl-g { color: #40a070; user-select: none; }
.hl-c { color: #6a7b86; font-style: italic; }
.hl-k { color: #a4478c; }
.hl-s { color: #2e7d55; }
.hl-n { color: #b06000; }
.hl-t { color: #2b6cb0; }
.hl-a { color: #8a6d3b; }
@media (prefers-color-scheme: dark) {
  .hl-g { color: #5fbf8f; }
  .hl-c { color: #7d8b99; } .hl-k { color: #d58fc4; } .hl-s { color: #7fc9a0; }
  .hl-n { color: #e0a052; } .hl-t { color: #7fb3e0; } .hl-a { color: #c9a227; }
}
code, pre { font-family: ui-monospace, monospace; font-size: 0.9em; }
/* An inline name of code is shaded so it reads as a thing and not as a word. A fence
   carries its own background already, so the spans inside one take none of their own. */
code { background: #7f7f7f24; border-radius: 3px; padding: 0.08em 0.32em; }
pre code, pre code span { background: none; padding: 0; }
blockquote { border-left: 3px solid #8884; margin: 0; padding-left: 1rem; }
/* The two ways back read as one thing, so they are set as one thing: the link at the end of
   a defect heading and the one a section heading carries. They sat at 0.75em and 0.6em, one
   of them faded and the other not, which made the same gesture look like two. */
.back, .toc-jump::after { font-size: 0.7em; font-weight: normal; opacity: 0.6; }
.back { margin-left: 0.5rem; }
/* A section heading is a way back to the contents, and it SAYS SO WHETHER OR NOT the pointer
   is over it — `::after` and not `:hover::after`. Shown only on hover it was a different
   thing from the arrow on a defect heading, which is a real element and always there: one
   gesture that the page advertised in one place and hid in the other, so a reader who never
   happened to hover learned that a heading was clickable only by accident. It is also
   invisible to anyone reading without a pointer at all.

   The arrow is CSS's own escape for U+2191 and the backslash is DOUBLED to reach the
   stylesheet: in a Python string `\2191` is an octal escape, and it read as U+0011
   followed by "91" — so the affordance said "\x11 91 contents". */
.toc-jump { cursor: pointer; }
.toc-jump::after { content: " \\2191 Contents"; }
nav.contents { border: 1px solid #8884; padding: 0.5rem 1rem; margin: 1.5rem 0; scroll-margin-top: 1rem; }
h2, h3 { scroll-margin-top: 1rem; }
nav.contents ul { margin: 0.25rem 0; padding-left: 1.25rem; }
h2 { border-bottom: 1px solid #8884; padding-bottom: 0.2rem; margin-top: 2.5rem; }
h4 { margin-top: 2rem; }
/* The title block: the title and the one paragraph directly under it. Centred together,
   because they are what the page IS rather than the first thing it says — and the subtitle
   is set smaller for the same reason: when it was made, how many files, at which commit.
   Selected structurally rather than by a class, so the prose stays prose in report.md and
   the page needs no markup of its own. Presentation only, and it stops at these two: every
   heading below is a section of the document and reads from the left like its body. */
/* The title block: the title and the one paragraph under it sit TOGETHER, then the
   document starts. The stamp belongs to the title, not to the prose below it, so the gap
   above it is closed to nothing and the gap below it is the one that separates the block
   from the body. */
h1 { text-align: center; margin-bottom: 0.2rem; }
h1 + p { font-size: 0.85em; opacity: 0.75; text-align: center;
         margin-top: 0; margin-bottom: 3rem; }
"""
_MD_HEADING = re.compile(r"^(#{1,6}) +(.*)$")
# How far below a heading its defect's entry line may sit. The writer emits the whole
# consequence and a blank line between the two, so the scan has to look PAST a line that is
# not the entry rather than give up at it. Bounded all the same, and stopped by the next
# heading, so a defect can only take the entry line that belongs to it.
_ENTRY_WINDOW = 6
_MD_FENCE = re.compile(r"^( *)(`{3,}) *([A-Za-z0-9+#_-]*) *$")
_MD_BULLET = re.compile(r"^( *)- +(.*)$")
# A defect names itself in its own heading, and in the first cell of its index row. That
# id is the engine's own — never derived from heading text, which is a worker's prose and
# which two defects may legitimately share.
# Built from :data:`CLUSTER_ID_PREFIX` rather than spelled again, so the id has exactly one
# definition in this file. The patterns that READ an id and the writer that mints it must
# agree, and a pattern spelled separately drifts from it silently: every defect on the page
# loses its id and every index link points at nothing, while a test whose fixture spells the
# id the way the writer does still passes.
_DEFECT_ID = re.escape(CLUSTER_ID_PREFIX) + r"\d+"
_MD_DEFECT = re.compile(rf"^({_DEFECT_ID}) — ")
# A defect's heading: the id, then the label. The id leads it because the engine writes
# that prefix and a worker cannot — under the old shape the heading was the consequence
# alone, so the id had to be read off the line below and a bullet elsewhere that merely
# looked like that line could claim it.
_MD_DEFECT_HEADING = re.compile(rf"^({_DEFECT_ID})\. ")
# A defect's own entry line — the metadata the writer puts under the heading, matched
# whole and built from the engine's own vocabularies, so a heading is confirmed as a
# defect's rather than assumed to be one.
#
# Read off the LABELS, which is what makes the line unforgeable: `*` reaches the prose
# escaped, so a consequence or a file name holding the separator cannot open a field of its
# own. The location is bounded by its SHAPE — anything, ending in the line or line range
# `_at` writes — and not by holding no whitespace. A file system admits a space in a name,
# and `engine/my core.py` matched nothing here, so that defect's heading carried no id at
# all and the index link jumped past it.
def _md_label(n: int) -> str:
    """One field's bold label, as the pattern below matches it. Escaped, because the label
    is a name and not a pattern: a label that gained a character with a meaning in a regex
    would silently change what this matches, and what it stops matching is every defect's
    anchor."""
    return re.escape(f"**{DEFECT_META_LABELS[n]}** ")


_MD_DEFECT_ENTRY = re.compile(
    r"^" + _md_label(0) + r"(?:" + "|".join(SEVERITIES) + r")"
    + re.escape(DEFECT_META_SEP) + _md_label(1) + r".+"
    # The location renders as code, so a backtick may sit either side of it. Without this
    # the line stops matching, every defect on the page loses its id, and every link into
    # one breaks -- silently, because nothing checks that an anchor exists.
    + re.escape(DEFECT_META_SEP) + _md_label(2) + r"`?.+:\d+(?:-\d+)?`?"
    + re.escape(DEFECT_META_SEP) + _md_label(3) + r"(?:"
    + "|".join(re.escape(size) for size in FIX_SIZES) + r")"
    # The last field is dropped where it does not vary, so the line has to match with and
    # without it -- an anchor that depends on a field being present is an anchor that
    # disappears the moment the field stops earning its place.
    + r"(?:" + re.escape(DEFECT_META_SEP) + _md_label(4) + r".+)?$")
# A backtick a worker wrote arrives escaped, and the lookbehind is what stops a span
# opening on it — without it, `\`x\`` matches from the escaped backtick and swallows the
# text as code. Emphasis needs no such guard and is not given one: escaping puts a backslash
# between a worker's two stars, so they are never adjacent and this pattern cannot reach
# them. The protection there is the escaping, not the regex.
_MD_CODE = re.compile(r"(?<!\\)`([^`]*)`")
_MD_EMPHASIS = re.compile(r"\*\*")
# The one link the writer emits: a defect's surviving cross-reference, pointing at that
# defect's own anchor. Bounded to a defect id on BOTH sides and to an unescaped bracket —
# `[` is one of the characters every worker string arrives behind a backslash, so nothing
# in this document but the writer above can produce this shape.
_MD_DEFECT_LINK = re.compile(rf"(?<!\\)\[({_DEFECT_ID})\]\(#({_DEFECT_ID})\)")
# A link to a SECTION, whose target is a slug rather than a defect id. Lower-case by
# construction — see :func:`_slug` — which is what keeps it from matching a defect link, and
# the link text is free: a pointer reading "this link" says less than the sentence around it
# would by repeating a heading a reader has not reached yet.
_MD_SECTION_LINK = re.compile(r"(?<!\\)\[([^\]\n]+)\]\(#([a-z0-9][a-z0-9-]*)\)")


def _split_cells(row: str) -> list[str]:
    """A table row, split where the writer meant a column break and nowhere else. A pipe the
    writer escaped is a pipe in somebody's prose: splitting on it shifts every field after it
    under the wrong heading, which is a table that reads plausibly and is wrong."""
    cells, held, escaped = [], [], False
    for char in row:
        if escaped:
            held.append(char)
            escaped = False
        elif char == "\\":
            held.append(char)
            escaped = True
        elif char == "|":
            cells.append("".join(held))
            held = []
        else:
            held.append(char)
    cells.append("".join(held))
    return cells


_MD_ESCAPED = re.compile(r"\\([" + re.escape(_MD_ACTIVE) + r"])")


def _unescape(text: str) -> str:
    """Undo the writer's Markdown escaping, once, on the way into the page.

    Only the characters the writer escapes. A backslash before anything else is somebody's
    text — a diagnostic quoting a lone surrogate as ``\\ud800`` would otherwise arrive at the
    reader as ``ud800``, naming a key that is not the one that broke. HTML's escaping is not
    undone either: the page keeps it, which is why non-fenced text passes straight through.
    """
    return _MD_ESCAPED.sub(r"\1", text)


def _inline(text: str, defined: frozenset[str] | set[str] = frozenset(),
            sections: dict[str, str] | None = None) -> str:
    """The inline shapes the report writes, and then the writer's escaping undone.

    Order is the point. Emphasis and code spans are matched FIRST, while every ``*`` and
    backtick that a worker supplied is still behind a backslash and cannot match — so only
    the engine's own markup becomes a tag. The escapes come off afterwards, leaving a file
    named ``a**b**c.py`` with its stars and a reproduction's backticks in the command
    somebody is about to copy.

    ``defined`` is every defect id this page anchors, and a cross-reference becomes a link
    only when the id it names is one of them — the same bound the index column carries. An
    id nothing defines renders as the bare id rather than as a link to nowhere: a reader who
    follows a link and lands on the page they were already on cannot tell that from a page
    that failed to move.

    ``sections`` is every section and subsection title to the id it carries, and it does two
    things. A **bold span naming a title becomes a link to it** — the prose points a reader at
    a named section in half a dozen places, and on the page every one of them was a bold phrase
    the reader had to go and find by scrolling. And an ordinary Markdown link to an **id** the
    document anchors becomes that link, which is how a pointer can read "this link" rather than
    repeating the section's name in a sentence that already named it.

    Both are bound the way a defect id is: a title or an id nothing anchors renders as plain
    text, so no link goes nowhere. Safe to key on a bold span because only the ENGINE can write
    one — a worker's asterisk reaches the prose escaped, and the escape is still on at this
    point in the pass.
    """
    text = _MD_DEFECT_LINK.sub(
        lambda m: (f'<a href="#{m.group(2)}">{m.group(1)}</a>'
                   if m.group(1) == m.group(2) and m.group(2) in defined else m.group(1)),
        text)
    # A section link, after the defect one and distinguishable from it: a defect id is an
    # upper-case prefix and digits, and a section id is what `_slug` makes, which lower-cases.
    # The text is whatever the sentence needs; only the TARGET is checked.
    anchored = set((sections or {}).values())
    text = _MD_SECTION_LINK.sub(
        lambda m: (f'<a href="#{m.group(2)}">{m.group(1)}</a>'
                   if m.group(2) in anchored else m.group(1)),
        text)
    text = _MD_CODE.sub(lambda m: f"<code>{m.group(1)}</code>", text)
    parts = _MD_EMPHASIS.split(text)
    if len(parts) > 1:
        named = sections or {}
        text = "".join(
            part if n % 2 == 0
            else (f'<a href="#{named[part]}"><strong>{part}</strong></a>' if part in named
                  else f"<strong>{part}</strong>")
            for n, part in enumerate(parts))
    return _unescape(text)


def _defect_anchors(lines: Sequence[str]) -> dict[int, str]:
    """Which heading lines name a defect, and which defect. The id is the heading's own,
    and the entry line below confirms that this heading really is a defect's.

    Two claims, and both are needed. The heading carries the id, so no other heading can
    take it: the sections that LIST clusters — the clustering notes, the provenance
    appendix — head their entries with the id and an em dash, and a worker's prose reaches
    a heading escaped and cannot open with `D7. ` on its own. And the entry line has to be
    there, or a heading that merely started that way would anchor a defect the document
    never wrote up.

    The scan looks PAST a line that is not the entry, because the whole consequence sits
    between the two. That sentence is a worker's, and :func:`_paragraph` is what keeps it a
    paragraph — without it a consequence beginning `# ` would end this scan at a heading the
    worker wrote and hand that worker the next defect's id. The scan stops at the next
    heading and at :data:`_ENTRY_WINDOW` lines, so a defect can take only the entry line
    written for it.
    """
    out: dict[int, str] = {}
    for n, line in enumerate(lines):
        heading = _MD_HEADING.match(line)
        named = _MD_DEFECT_HEADING.match(heading.group(2)) if heading else None
        if not named:
            continue
        for ahead in lines[n + 1:n + 1 + _ENTRY_WINDOW]:
            if _MD_HEADING.match(ahead):
                break
            bullet = _MD_BULLET.match(ahead)
            if bullet and _MD_DEFECT_ENTRY.match(bullet.group(2)):
                out[n] = named.group(1)
                break
    return out


_MD_BARE_DEFECT_ID = re.compile(r"D\d+")
_MD_TABLE_DEFECT_ROW = re.compile(r"^\| *(D\d+) *\|")


def _defect_ids_defined(lines: Sequence[str], anchors: dict[int, str]) -> set[str]:
    """Every defect id this page will carry an anchor for.

    A link to an id nothing defines goes nowhere and says nothing about why, so an index
    cell is checked against this set before it becomes one. Two things claim an id: a
    heading that took one off the entry line below it, and a top-level bullet that names
    one — a refuted defect's line, which has no heading of its own.

    Fenced text is skipped for the reason the conversion skips it everywhere else: what a
    worker's output holds is not this document's markup, and a bullet inside a fence
    defines nothing.
    """
    out = set(anchors.values())
    fence: str | None = None
    for line in lines:
        opening = _MD_FENCE.match(line.rstrip())
        if fence is not None:
            if opening and len(opening.group(2)) >= len(fence):
                fence = None
            continue
        if opening:
            fence = opening.group(2)
            continue
        bullet = _MD_BULLET.match(line.rstrip())
        named = _MD_DEFECT.match(bullet.group(2)) if bullet else None
        if named and len(bullet.group(1)) < 2:
            out.add(named.group(1))
        # A table row naming a defect with a BARE id defines it too: a REFUTED defect is
        # set out in a row rather than under a heading, and a link into one has to know the
        # row will carry the anchor. A row whose id is a LINK is a reference, not a
        # definition, which is what keeps the index from claiming every id on the page.
        row = _MD_TABLE_DEFECT_ROW.match(line)
        if row:
            out.add(row.group(1))
    return out


def _anchor(title: str) -> str:
    """The fragment a link to this section has to name, built by the same function that puts
    the id on the heading — so the writer and the page cannot disagree about it.

    A fresh ``taken`` set, which yields the id a heading gets when nothing has claimed it.
    Two headings reducing to one slug would send the second to `<slug>-2` and leave this
    pointing at the first, so a test asserts every link the prose writes resolves to an id the
    document defines; that is the guard, rather than a comment hoping it never happens.
    """
    return f"#{_slug(title, set())}"


def _plain_title(title: str) -> str:
    """A heading without the number in front of it or the count after it — the name a reader
    calls the section by, and the name the prose cross-references it under. Both of those move
    with the run, and neither is part of what the section is CALLED."""
    return _SECTION_COUNT.sub("", _SECTION_NUMBER.sub("", title))


def _slug(title: str, taken: set[str]) -> str:
    """An id for a section heading, built from the words a reader sees.

    The section number and the count are left out (see :data:`_SECTION_NUMBER`). A title
    that reduces to nothing — one made entirely of punctuation — still needs an id, and a
    title whose id is already taken gets a numbered spelling rather than a second
    definition of one id: two headings sharing an id makes every link to it ambiguous.

    It cannot collide with a defect's id, which is :data:`CLUSTER_ID_PREFIX` and digits:
    this lower-cases, so an upper-case prefix can never come out of it.
    """
    base = _SLUG_SEPARATORS.sub("-", _plain_title(title).lower()).strip("-")
    base = base or "section"
    name, n = base, 1
    while name in taken:
        n += 1
        name = f"{base}-{n}"
    taken.add(name)
    return name


def _contents(lines: Sequence[str], anchors: dict[int, str]) -> list[tuple[int, int, str, str]]:
    """The page's contents list — every section and subsection heading, in order, with the
    id each of them will carry.

    **Sections and subsections only.** A defect has a heading of its own, and listing those
    here would make the contents a second copy of the index, ranked differently and saying
    the same thing — so a heading that anchors a defect is skipped, and the level bound
    leaves out the per-defect entries of the provenance appendix, which name defects
    without anchoring them.
    """
    taken = {CONTENTS_ID}
    out: list[tuple[int, int, str, str]] = []
    for n, line in enumerate(lines):
        heading = _MD_HEADING.match(line)
        if not heading or n in anchors:
            continue
        level = len(heading.group(1))
        if level not in _CONTENTS_LEVELS:
            continue
        text = heading.group(2)
        # The index keeps the id every defect's way back points at, whatever number the
        # section carries this run.
        ident = (CONTENTS_ID if _SECTION_NUMBER.sub("", text) == SECTION_INDEX
                 else _slug(text, taken))
        out.append((n, level, text, ident))
    return out


def _contents_html(entries: Sequence[tuple[int, int, str, str]]) -> str:
    """The contents list as nested lists, a subsection inside its section's item."""
    if not entries:
        return ""
    out = [f'<nav class="contents"><p><strong>{CONTENTS_TITLE}</strong></p>']
    depth, open_item = 0, []
    for _, level, text, ident in entries:
        want = level - 1
        while depth < want:
            out.append("<ul>")
            depth += 1
            open_item.append(False)
        while depth > want:
            if open_item.pop():
                out.append("</li>")
            out.append("</ul>")
            depth -= 1
        if open_item[-1]:
            out.append("</li>")
        out.append(f'<li><a href="#{ident}">{_inline(text)}</a>')
        open_item[-1] = True
    while depth:
        if open_item.pop():
            out.append("</li>")
        out.append("</ul>")
        depth -= 1
    out.append("</nav>")
    return "".join(out)


# The way back to the contents, from any section heading. Presentation only: it adds
# behaviour and drops nothing, which is the rule render_html works under.
HTML_BACK_TO_CONTENTS = """<script>
(function () {
  var toc = document.querySelector("nav.contents");
  if (!toc) return;
  document.querySelectorAll("h2, h3").forEach(function (h) {
    if (toc.contains(h)) return;
    h.classList.add("toc-jump");
    h.title = "Back to the contents";
    h.addEventListener("click", function (e) {
      if (e.target.closest("a")) return;
      toc.scrollIntoView({ behavior: "smooth", block: "start" });
    });
  });
})();
</script>"""


def render_html(markdown: str) -> str:
    """``report.html``: ``report.md``, converted.

    The page is not a second report. Nothing here decides what the report SAYS, so the two
    cannot disagree about a severity, a status, or a fact one of them forgot — which is
    what a second renderer written beside the first kept producing.

    One rule governs the whole of it: **this may add presentation and may never drop
    content.** An id on a heading, a link from the index, a way back to it — all additions.
    Nothing that arrives is discarded, which is why a fact cannot go missing on the way.

    The grammar is the report's own and no wider: headings, one block quote, tables, fenced
    blocks, bullets one level deep, bold and inline code. This is not a Markdown parser and
    is not trying to be one — it reads exactly what the writer above it emits, which is why
    it fits on a page and needs nothing installed.

    Escaping happened once already, when the prose was written: every character outside a
    fence is HTML-safe, so this passes it through and escapes only what a fence holds.
    Escaping again here would show the reader the entities instead of the text.
    """
    lines = markdown.split("\n")
    anchors = _defect_anchors(lines)
    heading_ids = set(anchors.values())
    claimed: set[str] = set()
    defined = _defect_ids_defined(lines, anchors)
    contents = _contents(lines, anchors)
    section_ids = {n: ident for n, _, _, ident in contents}
    # Every section title to the id it carries, so a bold cross-reference in the prose becomes
    # a link. Keyed on the title as a READER writes it — the number the section happens to
    # carry this run and the count in its heading are both off, because the prose points at a
    # section by name and neither of those is part of the name.
    titles = {_plain_title(text): ident for _n, _level, text, ident in contents}
    listed = False
    # A defect written up in full names itself in its heading, and that is where the id
    # lands. A REFUTED defect has no heading — it is one line in the appendix — so its line
    # is the only claimant and keeps it. Whichever reaches an id first keeps it, or the
    # document defines one id twice and a link resolves to whichever a viewer picks.
    # Every id the document has defined so far. A cluster is named in the index, in its own
    # entry, in the clustering notes and in the appendix; exactly one of those may carry the
    # id, and the first to reach it is the one the reader is sent to.
    claimed = set(anchors.values())
    out = ["<!DOCTYPE html>", '<html lang="en">', "<head>", '<meta charset="utf-8">',
           '<meta name="viewport" content="width=device-width, initial-scale=1">',
           "<title>Review panel report</title>", f"<style>{HTML_STYLE}</style>",
           "</head>", "<body>"]
    # What is open, innermost last: "ul", "li", "table", "blockquote", "p".
    stack: list[str] = []
    fence: str | None = None
    held: list[str] = []
    rows: list[list[str]] = []

    def close_to(keep: int = 0) -> None:
        while len(stack) > keep:
            out.append(f"</{stack.pop()}>")

    def flush_table() -> None:
        nonlocal rows
        if not rows:
            return
        head, body = rows[0], [r for r in rows[1:] if not set("".join(r).strip()) <= {"-", ""}]
        # ONE column carries a defect id, and a cell links only when the document defines
        # the id it names. Neither bound was reachable while every path was spelled in
        # full; short names make a file called `D10` render as the bare cell `D10`, and
        # linking that sends a reader to some other defect or to nothing at all.
        column = next((n for n, cell in enumerate(head)
                       if cell.strip() == DEFECT_COLUMN), None)
        out.append('<div class="scroll"><table><thead><tr>'
                   + "".join(f"<th>{_inline(c)}</th>" for c in head)
                   + "</tr></thead><tbody>")
        for row in body:
            # Read the RAW cell, not the converted one: by then a defect id has already
            # become a link, and a link never matches the bare-id test below.
            raw_first = row[0].strip() if row else ""
            # First definition wins. A defect id names a row in more than one table -- the
            # provenance appendix lists every defect again -- and two elements carrying one
            # id is invalid and sends a link to whichever the browser picks.
            defines = (column == 0 and _MD_BARE_DEFECT_ID.fullmatch(raw_first) is not None
                       and raw_first not in heading_ids and raw_first not in claimed)
            if defines:
                claimed.add(raw_first)
            cells = []
            for n, cell in enumerate(row):
                text = cell.strip()
                # The defining row does not link to itself.
                cells.append(f'<a href="#{text}">{text}</a>'
                             if n == column and text in defined and not defines
                             else _inline(cell, defined))
            # A row naming a defect with a bare id, where no heading claims that id, is
            # where the defect is set out -- a REFUTED one is a row of the appendix's
            # refuted table and has no heading anywhere -- so the row carries the anchor and
            # every link into it lands here.
            anchor = f' id="{raw_first}"' if defines else ""
            out.append(f"<tr{anchor}>" + "".join(f"<td>{c}</td>" for c in cells) + "</tr>")
        out.append("</tbody></table></div>")
        rows = []

    fence_lang = ""
    for n, raw in enumerate(lines):
        line = raw.rstrip()
        opening = _MD_FENCE.match(line)
        if fence is not None:
            if opening and len(opening.group(2)) >= len(fence):
                # No protective newline after the tag. The one a parser discards is the
                # one immediately after `<pre>`, and it discards it only from that
                # element's first child TEXT: the first child here is `<code>`, so a
                # newline written after it is kept and every snippet on the page opens on
                # a blank line. A block whose own first line is empty still keeps it,
                # because that line is in the content rather than in front of it.
                code = _painted(chr(10).join(held), fence_lang)
                cls = f' class="language-{fence_lang}"' if fence_lang else ""
                block = f"<pre><code{cls}>{code}</code></pre>"
                # Captured output folds; a source snippet does not. `<details>` keeps the
                # text in the document -- it is one element away, not one request away --
                # so the page still carries every byte the prose does.
                if fence_lang == EVIDENCE_FENCE_LANG:
                    block = (f"<details><summary>{EVIDENCE_FOLD_SUMMARY}</summary>"
                             f"{block}</details>")
                out.append(block)
                fence, held, fence_lang = None, [], ""
            else:
                held.append(raw[len(fence_indent):] if raw.startswith(fence_indent) else raw)
            continue
        if opening:
            flush_table()
            # A fence indented under a bullet belongs inside that bullet; one at the margin
            # closes whatever is open and stands on its own.
            if not opening.group(1):
                close_to(0)
            elif stack and stack[-1] == "p":
                close_to(len(stack) - 1)
            fence, fence_indent, held = opening.group(2), opening.group(1), []
            fence_lang = opening.group(3) or ""
            continue
        if not line.strip():
            flush_table()
            if stack and stack[-1] == "p":
                close_to(len(stack) - 1)
            continue
        heading = _MD_HEADING.match(line)
        if heading:
            flush_table()
            close_to(0)
            level, text = len(heading.group(1)), heading.group(2)
            # Above the first section, and below the title, which is what it is a contents
            # list OF.
            if not listed and level >= 2:
                out.append(_contents_html(contents))
                listed = True
            anchor = anchors.get(n) or section_ids.get(n)
            attr = f' id="{anchor}"' if anchor else ""
            # The way back belongs to a DEFECT. A section heading that carried one would
            # send a reader from the appendix to the ranked defect list they were not in.
            back = (f' <a class="back" href="#{CONTENTS_ID}">{BACK_TO_INDEX}</a>'
                    if n in anchors else "")
            out.append(f"<h{level}{attr}>{_inline(text, defined)}{back}</h{level}>")
            continue
        if line.startswith("|"):
            if stack and stack[-1] == "p":
                close_to(len(stack) - 1)
            rows.append(_split_cells(line.strip())[1:-1])
            continue
        flush_table()
        if line.startswith(">"):
            # `> ` with nothing after it has already lost its space to the strip above, so
            # the empty line of a multi-paragraph statement is `>` alone. Treated as prose
            # rather than a quote, it ends the quote and prints a stray angle bracket.
            if not (stack and stack[-1] == "blockquote"):
                close_to(0)
                out.append("<blockquote>")
                stack.append("blockquote")
            out.append(f"<p>{_inline(line[2:], defined)}</p>" if line.startswith("> ") else "")
            continue
        if stack and stack[-1] == "blockquote":
            close_to(0)
        bullet = _MD_BULLET.match(line)
        if bullet:
            # Two spaces per level, and as many levels as the writer used. Capped at two
            # levels this rendered a note indented under a check line as a SIBLING of it --
            # a severity revision becoming an independent check with no location, which is
            # the association the indent exists to carry.
            depth = len(bullet.group(1)) // 2
            while stack.count("ul") > depth + 1:
                close_to(len(stack) - 1)
            if stack and stack[-1] == "p":
                close_to(len(stack) - 1)
            # Down to the level this bullet sits at, however many were open above it: a
            # list that went two deep and comes back to one closes both the inner list and
            # the item holding it, or every bullet after it stays nested inside a note.
            while stack.count("ul") > depth + 1:
                close_to(len(stack) - 1)
                if stack and stack[-1] == "ul":
                    close_to(len(stack) - 1)
            if stack.count("ul") == depth:
                out.append("<ul>")
                stack.append("ul")
            elif stack and stack[-1] == "li":
                close_to(len(stack) - 1)
            text = bullet.group(2)
            named = _MD_DEFECT.match(text)
            owns = bool(named) and depth == 0 and named.group(1) not in claimed
            if owns:
                claimed.add(named.group(1))
            attr = f' id="{named.group(1)}"' if owns else ""
            back = (f' <a class="back" href="#{CONTENTS_ID}">{BACK_TO_INDEX}</a>'
                    if owns else "")
            out.append(f"<li{attr}>{_inline(text, defined, titles)}{back}")
            stack.append("li")
            continue
        if stack and stack[-1] == "li":
            # An indented line under a bullet continues it; nothing is dropped.
            out.append(f"<br>{_inline(line.strip(), defined, titles)}")
            continue
        if not (stack and stack[-1] == "p"):
            close_to(0)
            out.append("<p>")
            stack.append("p")
        out.append(_inline(line.strip(), defined, titles))
    if fence is not None:
        # A fence the writer never closed. Emitted in the SAME shape as a closed one --
        # `<code>`, the language class, the painting -- because one input rendering two
        # ways depending on how it ended is a difference a reader sees and nothing
        # explains. The engine's own writer always closes a fence; this branch is what a
        # hand-edited report.md reaches, and it is not a reason to show that reader a
        # different page.
        code = _painted(chr(10).join(held), fence_lang)
        cls = f' class="language-{fence_lang}"' if fence_lang else ""
        out.append(f"<pre><code{cls}>{code}</code></pre>")
    flush_table()
    close_to(0)
    # Every link on this page must land. The anchors are claimed by a heading or by a row,
    # and both depend on prose matching a pattern -- so a wording change can unanchor the
    # whole document without any other symptom. Checking it here is what turns that from a
    # silent breakage into a failed render.
    #
    # What it catches is a link landing on NOTHING, and not a link landing somewhere wrong.
    # A defect whose heading stops matching usually still has its id defined further down:
    # the provenance table names every defect in a bare first cell, and a bare first cell
    # claims the anchor, so the link resolves -- to an appendix row several sections past
    # the defect the reader asked for. Refusing THAT needs to know which table a row is in,
    # which this conversion deliberately does not: it reads one line at a time and is not a
    # Markdown parser. It is caught instead by asserting which ELEMENT carries each id,
    # over the run's own findings, which is a thing a test can see and this cannot.
    page = "".join(out)
    targets = set(re.findall(r'id="([^"]+)"', page))
    missing = sorted({m for m in re.findall(r'href="#([^"]+)"', page) if m not in targets})
    if missing:
        raise ReportError("the report links to anchors nothing defines: "
                          + ", ".join(missing[:10])
                          + (f" (and {len(missing) - 10} more)" if len(missing) > 10 else ""))
    out += [HTML_BACK_TO_CONTENTS, "</body>", "</html>", ""]
    return "\n".join(out)



def _read_candidates(rundir: Path) -> dict:
    doc = _read_run_json(rundir, CANDIDATES_FILE_NAME)
    if (not isinstance(doc, dict) or doc.get("stage") != ROUTED_STAGE
            or not isinstance(doc.get("candidates"), list) or not isinstance(doc.get("units"), list)
            or not isinstance(doc.get("probe"), dict)):
        raise RunDirError(f"{rundir / CANDIDATES_FILE_NAME} is not the engine's route record")
    return doc


def _read_inventory(rundir: Path) -> dict:
    doc = _read_run_json(rundir, "inventory.json")
    try:
        for key in ("files", "excluded", "skipped"):
            if not isinstance(doc[key], list):
                raise TypeError(key)
        for key in ("source", "tree_sha256"):
            if not isinstance(doc[key], str):
                raise TypeError(key)
        # The reachability warning and the commit line render from these, so a record
        # missing either is refused here rather than rendering a warning that silently
        # says nothing was missed.
        for key in ("coverage", "commit"):
            if not isinstance(doc[key], dict):
                raise TypeError(key)
        if not isinstance(doc["coverage"].get("tracked_not_reviewed"), list):
            raise TypeError("coverage.tracked_not_reviewed")
        # Both directions, because how many files the tree tracks is derived from the two of
        # them together: one of them missing is a total that is quietly short.
        if not isinstance(doc["coverage"].get("reviewed_not_tracked"), list):
            raise TypeError("coverage.reviewed_not_tracked")
        if not isinstance(doc["coverage"].get("state"), str):
            raise TypeError("coverage.state")
    except (KeyError, TypeError) as exc:
        raise RunDirError(f"inventory.json under {rundir} is not the engine's: {exc}") from exc
    return doc


def report_summary(states: Sequence[VerificationState], findings: Findings,
                   targets: Sequence[Path]) -> str:
    out = [f"rung {findings.rung}; verification units {_plural_states([s.state for s in states])}"]
    total = _plural(len(findings.candidates), "candidate")
    counts = {status: sum(1 for r in findings.candidates if r["status"] == status)
              for status in VERDICT_STATUSES}
    out.append(f"{total}: " + ", ".join(
        f"{counts[status]} {status}" for status in VERDICT_STATUSES))
    ungrouped = [e for e in findings.clustering if e["state"] != UNIT_COMPLETE]
    out.append(f"{_plural(len(findings.clusters), 'defect')} after clustering"
               + (f"; {_plural(len(ungrouped), 'area')} not clustered" if ungrouped else ""))
    out.append("wrote " + ", ".join(str(target) for target in targets))
    return "\n".join(out) + "\n"


# --------------------------------------------------------------------------- #
# redo — the last two rounds taken back off a run
# --------------------------------------------------------------------------- #
# Clustering and synthesis go back TOGETHER, and not for convenience. A defect's id is its
# POSITION in the clustering (see `build_clusters`), so regrouping the same candidates reuses
# D1 and D2 for different members. A synthesis kept across that passes every id check the
# engine has while describing defects that are no longer those defects.
REDO_KINDS = (CLUSTERER_KIND, SYNTHESIZER_KIND)
# Everything a redo publishes over. The lock is NOT here: it is a claim, not an output.
# The stamp IS, and for the opposite reason to the one that keeps it out of a recovery's
# manifest: a recovery rebuilds the report a run already had, while a redo re-dispatches
# rounds and the report that follows is a new rendering of new data, which should say so.
REDO_OUTPUTS = (REPORT_NAME, HTML_NAME, FINDINGS_NAME, REPORT_STAMP_NAME)

# What `route --redo` takes back, and it is EVERY round after reading rather than route's
# own. A candidate's id is its position in the routing, exactly as a defect's is its
# position in the clustering, so re-routing the same reading results renumbers the
# candidates -- and a verification, clustering or synthesis kept across that answers about
# `cand-003` while `cand-003` is now a different finding. Every one of those rounds reads a
# candidate id, so every one of them goes back.
#
# The reading round itself is untouched, which is the point: it is the expensive one, and
# re-dispatching ONE of its units and routing again is what this exists to make possible.
ROUTE_REDO_KINDS = (VERIFIER_KIND, CLUSTERER_KIND, SYNTHESIZER_KIND)
ROUTE_REDO_OUTPUTS = (CANDIDATES_FILE_NAME, *REDO_OUTPUTS)


def plan_redo(rundir: Path, units_doc: dict, kinds: Sequence[str] = REDO_KINDS,
              stage: str = VERIFICATION_STAGE, what: str = "clustering or synthesis",
              carry_on: str = "cluster") -> tuple[tuple[str, ...], dict]:
    """What a redo takes back, and the listing the stage runs with once it has — decided
    WITHOUT writing anything, so that every refusal below it still holds.

    Split from :func:`apply_redo` for that reason alone. `cluster` promises that everything
    which can refuse runs before anything is written, and a redo that deleted first would
    break the promise in the worst direction: a run stripped of its clustering by a refusal
    about the route record, which has nothing to do with clustering.

    Four things move together and the listing is the one that is easy to miss: every stage
    APPENDS to the listing it inherited, so unit directories cleared by hand leave their
    records behind and the stage then runs into a listing naming each unit twice. That is a
    refusal two stages later blaming ``units.json`` for not being the engine's, which is
    true and tells whoever reads it nothing about what they actually did.

    Nothing from an earlier round is touched. The route record, the snapshot, the inventory
    and every reading and verification unit are what a redo exists to REUSE.

    Refused while the report lock is held. That narrows a window rather than closing it: a
    report renders its text before it takes the lock, so one already past that point can
    still publish what it computed into a run this has since reset. Closing that needs the
    report to hold exclusion across reading and publishing, which is a change to the report
    and not to this.
    """
    lock = rundir / REPORT_LOCK_NAME
    if _is_regular_file(lock):
        raise RunDirError(
            f"{lock} exists, so a report is writing this run directory or one was "
            f"interrupted; --redo will not reset a run out from under a report"
        )
    taking = tuple(unit["id"] for unit in units_doc["units"]
                   if unit.get("kind") in kinds and isinstance(unit.get("id"), str))
    # Refused only where the run is ALREADY at the stage this resets to: there the plain
    # command really does run normally from here. A stage that has moved on with nothing
    # to take back is the other case -- a route over readers that all failed or found
    # nothing writes the verification stage with zero batches -- and refusing it here
    # while the plain command refuses the stage left the run with no command that would
    # accept it. The reset is what such a run needs, and there is nothing it would delete.
    if not taking and units_doc.get("stage") == stage:
        raise RunDirError(
            f"--redo found no {what} unit to take back, and "
            f"{UNITS_FILE_NAME} is at stage {units_doc.get('stage')!r} already; "
            f"{carry_on} runs normally from here"
        )
    kept = [unit for unit in units_doc["units"] if unit.get("kind") not in kinds]
    return taking, {"stage": stage,
                    "lanes": units_doc.get("lanes", list(LANES)), "units": kept}


def apply_redo(rundir: Path, taking: Sequence[str], doc: dict,
               outputs: Sequence[str] = REDO_OUTPUTS) -> None:
    """Take the planned units off the disk, and mark the run as no longer having them.

    Called where the first clustering unit directory would otherwise land, so everything
    that can refuse has already run. ``units.json`` is written LAST and at the verification
    stage, so a failure in the write that follows leaves a run that is cleanly unclustered
    rather than one whose records and directories disagree — and a plain `cluster` carries
    it forward from there.
    """
    # A directory that would not come away is left where it is, and that authorizes
    # nothing: the next run of the stage reclaims these names, and a directory holding
    # anything it did not write is refused there by name rather than deleted.
    #
    # `_inside` FIRST, and it is not a formality. `rmtree` refuses a link handed to it
    # directly, which covers a link at the leaf -- but `units/` or `dispatch/` is an
    # INTERMEDIATE component, and a link there resolves the whole path somewhere else
    # entirely, where `rmtree` then does exactly what it was asked. `_inside` resolves both
    # sides and answers False for anything it cannot place, so a path this cannot prove
    # belongs to the run is left alone rather than removed.
    for unit_id in taking:
        for parent in (UNITS_DIR, DISPATCH_DIR):
            target = rundir / parent / unit_id
            if _inside(rundir, target):
                shutil.rmtree(target, ignore_errors=True)
    for name in outputs:
        try:
            (rundir / name).unlink()
        except FileNotFoundError:
            pass
        except OSError as exc:
            raise RunDirError(f"cannot remove {rundir / name}: {_os_reason(exc)}") from exc
    write_json(rundir / UNITS_FILE_NAME, doc)


# --------------------------------------------------------------------------- #
# check — the engine's own parse, before a result is landed
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class QuotedRange:
    """One finding's citation, compared with the pinned tree: where it points, what the
    comparison found, and both texts so a reader can see which one is wrong.

    ``found`` is ``None`` where the state is ``unreadable``, because there is no source at
    that range to show.
    """

    file: str
    line_start: int
    line_end: int
    state: str
    # The first quoted line the cited range could not account for, and the line the range
    # holds where the match stalled. NOT the first line of the quotation: a quote may
    # abbreviate a long range to its ends, so its first line matches by construction and
    # printing it shows the dispatcher two identical lines and no information.
    quoted: str | None
    found: str | None
    # Where each of those two sits: the quoted one's place in the quotation, its length,
    # and the found one's LINE NUMBER in the file. The texts can be identical — an in-order
    # match that runs out of range stalls on a line spelled the same way as the one it
    # could not account for — and then the positions are the whole of the difference.
    quoted_at: int | None = None
    quoted_of: int | None = None
    found_at: int | None = None


def _first_line(text: str, width: int = 100) -> str:
    """The first line with anything on it, bounded, for a terminal where one line is what
    fits. Not :func:`_one_line`, which collapses a whole quotation into a single long line
    and escapes it for Markdown: this is printed to a console, where an escaped ``&lt;``
    would be a character the source does not hold.
    """
    for line in text.splitlines():
        if line.strip():
            line = line.strip()
            return line if len(line) <= width else line[:width - 1] + "…"
    return ""


def _quoted_where(quote: QuotedRange) -> str:
    """Where in the quotation the match stalled, as a label for the line printed after it.

    Empty where the position is unknown, so the line still prints rather than announcing a
    gap in the engine's own bookkeeping to somebody deciding about a worker's reply.
    """
    if quote.quoted_at is None or quote.quoted_of is None:
        return ""
    return f"line {quote.quoted_at} of {quote.quoted_of}: "


def _found_where(quote: QuotedRange) -> str:
    """The FILE line the range had reached, labelled as a file line.

    Spelled the way the citation above it is spelled, so the dispatcher can see at a glance
    whether the number is inside the range the finding claims.
    """
    return "" if quote.found_at is None else f"{quote.file} line {quote.found_at}: "


@dataclass(frozen=True)
class CheckOutcome:
    """What ``check`` establishes about one result: the rejections landing it would carry,
    and the quotation comparison for every finding it holds.

    ``quotes`` is empty rather than absent for a result that carries no finding — a
    verifier's, a clusterer's, the probe's — so one shape is read whatever the unit was.
    """

    rejected: tuple[str, ...]
    quotes: tuple[QuotedRange, ...] = ()
    dropped: int = 0


def check_result(rundir: Path, unit_id: str, payload: object) -> CheckOutcome:
    """Run the parse the engine WILL run on ``payload`` as unit ``unit_id``'s result, and
    compare every quotation in it with the tree the run is pinned to.

    Returns what landing it would cost — the rejections, empty where there are none — and
    raises :class:`ResultError` where the result would fail the unit whole.

    **The quotation comparison is here because ``route`` runs it too late to act on.** It
    is the one thing the engine checks that it checks after the single re-dispatch the
    protocol allows is gone, so a unit that cited thirteen wrong ranges could only be
    flagged in the report. Run at landing it is the same comparison, :func:`compare_quote`,
    at the moment the dispatcher can still send the unit back. A wrong quotation still does
    not refuse the result: it costs that finding's credibility and not the unit, and
    ``route`` still records what landed.

    **The point is that it is the same parse and not a second one.** A dispatcher's landing
    check was "the file parses and holds the count its reply claimed", which is strictly
    weaker than what the stage enforces an hour later: a result could land clean and be
    refused after the re-dispatch the protocol allows is no longer possible. A check that
    approximated the stage would reintroduce that gap one layer along, so this dispatches on
    the unit's own kind and calls the stage's parser with the stage's own inputs.

    Which is why it needs the RUN and not only the file. Half of what the stage enforces is
    about the run: whether a location names a file in the snapshot, whether a verdict
    answers for a candidate in its batch, whether a grouping partitions the ids that unit
    was handed. A checker given the JSON alone could not ask any of them.
    """
    units_doc = _read_run_json(rundir, UNITS_FILE_NAME)
    if not isinstance(units_doc, dict) or not isinstance(units_doc.get("units"), list):
        raise RunDirError(f"{rundir / UNITS_FILE_NAME} is not a units listing")
    listed = [u for u in units_doc["units"]
              if isinstance(u, dict) and u.get("id") == unit_id]
    if not listed:
        raise RunDirError(
            f"{rundir / UNITS_FILE_NAME} lists no unit {unit_id!r}; check names the unit "
            f"whose payload the worker was given"
        )
    if len(listed) > 1:
        # Refused rather than resolved by picking one. The entries can differ -- a
        # re-planned unit carries a different batch -- so checking against the wrong one
        # answers a question nobody asked, and answering it confidently is worse than
        # saying the listing is not the engine's. A stage reaches the same conclusion two
        # steps later; this says it here, where it is still actionable.
        raise RunDirError(
            f"{rundir / UNITS_FILE_NAME} lists unit {unit_id!r} {len(listed)} times; the "
            f"listing is not the engine's, so which entry this result answers is unknown"
        )
    unit = listed[0]
    kind = unit.get("kind")
    if kind in (READER_KIND, AUDITOR_KIND):
        owner = frozenset(_read_owner(rundir))
        context = _read_context(rundir)
        findings, rejected, dropped = parse_reader_result(
            payload, unit_id, _locations_for(unit, owner, _read_audited(rundir)),
            cwd_files=owner | context, context=context)
        snapshot = rundir / "snapshot"
        quotes = []
        for finding in findings:
            checked = compare_quote(snapshot, finding.file, finding.line_start,
                                    finding.line_end, finding.quote)
            quotes.append(QuotedRange(finding.file, finding.line_start, finding.line_end,
                                      checked.state, checked.quoted_line, checked.found_line,
                                      checked.quoted_at, checked.quoted_of, checked.found_at))
        return CheckOutcome(rejected, tuple(quotes), dropped)
    if kind == PROBE_KIND:
        parse_probe_result(payload, unit_id)
        return CheckOutcome(())
    if kind == VERIFIER_KIND:
        routed = _read_candidates(rundir)
        reproducible = {c["id"]: any(r["reproduction"] is not None for r in c["raised_by"])
                        for c in routed["candidates"]}
        # Indexed, not `.get` with a default, because the stage indexes: a candidate in
        # this unit that the route record does not hold is a run whose listing and record
        # disagree, and the stage refuses it. Defaulting here would make `check` accept a
        # result the stage rejects -- the gap this command exists to close, reopened inside
        # the command itself.
        missing = [cid for cid in unit.get("candidates", ()) if cid not in reproducible]
        if missing:
            raise RunDirError(
                f"unit {unit_id} is listed with {missing[0]!r}, which "
                f"{CANDIDATES_FILE_NAME} does not hold; the listing and the route record "
                f"disagree about this run"
            )
        batch = {cid: reproducible[cid] for cid in unit["candidates"]}
        # The listing's own question, defaulting to the defect one for a run routed before
        # coverage batches existed. Read from the row rather than derived from the id: the
        # value the stage will use is the one `check` has to use, or the gap this command
        # closes reopens between the two.
        _verdicts, rejected = parse_verifier_result(payload, unit_id, batch,
                                                    unit.get("asks", DEFECT_ASKS))
        return CheckOutcome(rejected)
    if kind == CLUSTERER_KIND:
        parse_clusterer_result(payload, unit_id, unit.get("candidates", ()))
        return CheckOutcome(())
    if kind == SYNTHESIZER_KIND:
        parsed = parse_synthesizer_result(payload, unit_id, unit.get("defects", ()))
        return CheckOutcome(tuple(parsed["rejected"]))
    raise RunDirError(f"unit {unit_id} has kind {kind!r}, which this engine cannot parse")


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=_PROG,
        description="Blind multi-agent correctness sweep: plan, route, cluster, "
                    "synthesize, report.",
    )
    sub = parser.add_subparsers(dest="command", required=True,
                                metavar="{plan,route,cluster,synthesize,report,check}")
    plan = sub.add_parser("plan", help="partition a job into blind reading units")
    plan.add_argument("job", help="path to job.json")
    plan.add_argument(
        "--rundir",
        help="run directory, outside root and any git repository, and empty (the job file "
             "itself may be there). Omit it and the engine mints one under the host's "
             "temporary directory, named from the clock and the job's digest, which is the "
             "only form that cannot collide with another run",
    )
    route = sub.add_parser("route", help="turn every reading result into candidates and "
                                        "route each to a verifier that did not raise it")
    route.add_argument("rundir", help="the run directory plan wrote, its reading units dispatched")
    route.add_argument("--redo", action="store_true",
                       help="take every round after reading back off a run that has "
                            "already routed, then route again: the reading results are "
                            "reused and only a unit you re-dispatch yourself changes. "
                            "Deletes the verification, clustering and synthesis units, "
                            "their dispatch directories, the route record and any "
                            "published report")
    cluster = sub.add_parser("cluster", help="group each area's verified candidates into "
                                             "defects, one clustering unit per area")
    cluster.add_argument("rundir", help="the run directory route wrote, its verification units "
                                        "dispatched")
    cluster.add_argument("--redo", action="store_true",
                         help="take clustering and synthesis back off a run that has "
                              "already had them, then cluster again: the reading and "
                              "verification rounds are reused and only the last two are "
                              "dispatched afresh. Deletes those units, their dispatch "
                              "directories and any published report")
    synthesize = sub.add_parser("synthesize", help="write the one unit that names this "
                                                   "run's tiers and accounts for every defect")
    synthesize.add_argument("rundir", help="the run directory cluster wrote, its clustering "
                                           "units dispatched")
    check = sub.add_parser("check", help="run the engine's own parse over a result before "
                                        "it is landed, so a reply that would be refused "
                                        "an hour later is refused now")
    check.add_argument("rundir", help="the run directory the unit belongs to")
    check.add_argument("unit", help="the unit whose payload the worker was given")
    check.add_argument("--result",
                       help="the file holding the worker's object (default: "
                            "<rundir>/dispatch/<unit>/reply.json, which is where a lane A "
                            "worker writes). Pass the path you extracted to for lane B")
    report = sub.add_parser("report", help="write the report from the verification, "
                                           "clustering and synthesis results and the record "
                                           "of what ran")
    report.add_argument("rundir", help="the run directory cluster and, where it ran, "
                                       "synthesize wrote, their units dispatched and "
                                       "dispatch.json written")
    report.add_argument("--rerender", action="store_true",
                        help="rewrite the report and the findings file from the run "
                             "directory as it stands, overwriting what report itself wrote "
                             "and nothing else; no agent is dispatched")
    return parser


def _run_plan(args: argparse.Namespace) -> int:
    # Everything that can refuse runs before anything is written: the inventory is
    # measured in memory, the partition proved over it, the briefs and schema the
    # payloads are built from read, and the auditor payload measured against the ceiling
    # with its schema section in it, exactly as write_units renders it. Only then does the
    # snapshot land, so a refusal from any of these checks leaves no run directory -- a
    # MINTED one included, which exists from the moment it is named and is removed below --
    # and a failure once writing has begun takes back what was written.
    # Whether the directory is THIS call's to take back. A minted one exists from the
    # moment it is named -- claiming it is how two runs are kept out of one name -- so the
    # "did it exist before?" test below reads it as somebody else's and leaves it behind on
    # every refusal. The flag is the answer that test cannot give.
    minted: Path | None = None
    try:
        job = load_job(args.job)
        # Omitted: the engine mints one. A caller choosing the name is a caller choosing
        # another run's name, and the emptiness check below cannot help when the collision
        # happens before it runs.
        if args.rundir:
            rundir = check_rundir(args.rundir, job)
        else:
            minted = mint_rundir(args.job)
            rundir = check_rundir(minted, job)
        inventory = measure_files(job, enumerate_files(job))
        areas = partition(job, inventory)
        companions = load_companions()
        # Every payload that can refuse, rendered and thrown away, before the snapshot
        # lands. The set is the one write_units will render, not a wider one: a ceiling
        # refusal over an auditor payload nobody is handed would stop a run for the size of
        # a question that was never going to be put.
        for audited in (auditor_areas(areas)
                        if auditor_plan_of(inventory, job.coverage is False).dispatch else ()):
            render_auditor_payload(companions.auditor_brief, job.problem, audited, inventory,
                                   schema=companions.reader_schema)
        # Checked again at the last moment, then claimed. A refusal up to here follows no
        # write of this plan's, so nothing is taken back; after the claim, no other plan can
        # have written what the clean-up removes.
        rundir = check_rundir(rundir, job)
        _check_case_collisions(inventory)
        # What the clean-up keeps follows from where the job was loaded and whether the run
        # directory is there, never from a listing, which can name what another plan wrote
        # and took back before this plan claimed.
        keep_job = _resolve(job.path, "job file", RunDirError) == rundir / JOB_FILE_NAME
        # `os.path.exists` answers False for every error there is, and False here says
        # this call created the directory — which is what authorizes removing it on a
        # failure. A directory the caller made and this could not examine is not this
        # call's to delete.
        made_rundir = (minted is not None
                       or _lstat_or_absent(rundir, "--rundir", RunDirError) is None)
        _claim_snapshot(rundir)
        try:
            write_snapshot(job, inventory, rundir, claimed=True,
                           taken=clock_now())
            write_job_copy(rundir, job)
            write_areas(rundir, job, areas)
            write_units(rundir, job, inventory, areas, companions)
        except BaseException:
            _remove_what_was_written(rundir, keep_job=keep_job, made_rundir=made_rundir)
            raise
    except ReviewPanelError as exc:
        # A refusal between minting and the claim leaves an empty directory nobody will
        # ever look in. Removed only while it is still empty, and never for a directory
        # the caller named: this call made it, so this call is the only one that can know
        # nothing else has since put anything in it.
        if minted is not None:
            with contextlib.suppress(OSError):
                minted.rmdir()
        sys.stderr.write(f"{_PROG}: {exc}\n")
        return 2
    # Where it ran, FIRST, and on success only. A minted name cannot be guessed and every
    # stage after this one takes the directory as an argument, so a plan that printed a
    # preview and not the path left the caller to search the host's temporary directory —
    # ambiguous the moment two runs are planned close together, and the wrong answer is
    # another run's evidence.
    sys.stdout.write(f"{RUNDIR_LINE}{rundir}\n\n")
    sys.stdout.write(preview(job, inventory, areas))
    return 0


def _run_check(args: argparse.Namespace) -> int:
    """Answer one question — would the stage accept this? — and write nothing.

    Read-only by construction: it takes a path and a run directory and touches neither.
    A dispatcher runs it between writing a worker's reply and copying that reply into
    `units/`, which is the one moment a bad result can still be re-dispatched.
    """
    try:
        rundir = _resolve(Path(args.rundir), "rundir", RunDirError)
        source = (Path(args.result) if args.result
                  else rundir / DISPATCH_DIR / args.unit / "reply.json")
        try:
            raw = source.read_text(encoding="utf-8")
        except OSError as exc:
            raise RunDirError(f"cannot read {source}: {_os_reason(exc)}") from exc
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ResultError(f"{source} is not JSON: {exc}") from None
        outcome = check_result(rundir, args.unit, payload)
    except ReviewPanelError as exc:
        sys.stderr.write(f"{_PROG}: {exc}\n")
        return 2
    # Rejections are NOT a refusal. A finding or a verdict the engine cannot read costs
    # itself and the unit still lands, so a dispatcher told to re-dispatch over one would be
    # re-dispatching a unit that answered. They are printed because the operator should see
    # what the run is about to lose, and the exit code stays 0 because landing it is right.
    for message in outcome.rejected:
        sys.stdout.write(f"rejected: {message}\n")
    if outcome.dropped:
        sys.stdout.write(f"out of scope: {_plural(outcome.dropped, 'finding')} located in "
                         f"files the job left out, to be dropped\n")
    # The citations, every one of them, and the two texts wherever they disagree. A wrong
    # range is not a refusal either — it costs its own finding's credibility — but it is the
    # one thing worth re-dispatching a unit over, and this is the last moment that is
    # possible. Both first lines are printed because the dispatcher is deciding between two
    # different repairs: a range pointing at other code entirely, and a quotation that was
    # reflowed on the way back.
    if outcome.quotes:
        states = [quote.state for quote in outcome.quotes]
        counted = ", ".join(f"{states.count(state)} {state}" for state in QUOTE_STATES
                            if states.count(state))
        sys.stdout.write(f"quotations: {_plural(len(states), 'finding')} — {counted}\n")
        for quote in outcome.quotes:
            span = (f"line {quote.line_start}" if quote.line_start == quote.line_end
                    else f"lines {quote.line_start}-{quote.line_end}")
            sys.stdout.write(f"  {quote.state:<10} {quote.file} {span}\n")
            if quote.state != QUOTE_MATCHES:
                if quote.quoted is not None:
                    sys.stdout.write(f"      quoted: {_quoted_where(quote)}"
                                     f"{_first_line(quote.quoted)}\n")
                if quote.found is not None:
                    sys.stdout.write(f"      found:  {_found_where(quote)}"
                                     f"{_first_line(quote.found)}\n")
    sys.stdout.write(f"{args.unit}: the engine accepts this result"
                     + (f", with {_plural(len(outcome.rejected), 'rejection')}\n"
                        if outcome.rejected else "\n"))
    return 0


def _run_route(args: argparse.Namespace) -> int:
    # Everything that can refuse runs before anything is written: the listing must be at
    # the reading stage, the companions readable, every unit classified and every result
    # parsed, the candidates built and routed — and only then does the first batch directory
    # land.
    try:
        rundir = _resolve(Path(args.rundir), "rundir", RunDirError)
        # **The claim first, before the marker is even read.** A marker-keyed refusal is what
        # makes an interrupted route resumable and it excludes nobody: two routes can read
        # `reading`, and the second then takes back the first's committed batch directories.
        # Held through the marker write below, the claim is what makes "no route has
        # committed" mean "and none is committing".
        lock = _claim_stage(rundir, "route")
        try:
            # The marker, and nothing else. ``candidates.json`` on disk cannot say whether
            # this run has routed: route claims that file before it writes a single batch,
            # so a route killed between the claim and the marker leaves it sitting there
            # with nothing committed behind it, and a refusal keyed on it strands a run that
            # could simply route again. The marker moves last, which is what makes it the
            # answer.
            units_doc = _read_units_at_stage(
                rundir,
                (READING_STAGE, VERIFICATION_STAGE, CLUSTERED_STAGE, SYNTHESIZED_STAGE,
                 REPORTED_STAGE)
                if args.redo else READING_STAGE,
                "route runs once, after the reading units are dispatched"
                + (" — and --redo needs a run that has already routed" if args.redo else ""))
            taking: tuple[str, ...] = ()
            if args.redo:
                taking, units_doc = plan_redo(
                    rundir, units_doc, ROUTE_REDO_KINDS, READING_STAGE,
                    "verification, clustering or synthesis", "route")
            problem = _read_problem(rundir)
            owner = _read_owner(rundir)
            companions = load_route_companions()
            # The probe answers a different question from the reading units and is parsed
            # against a different schema, so it is separated here rather than inside the
            # reader parse: a probe result run through parse_reader_result would fail the
            # unit on every field.
            reading = [u for u in units_doc["units"] if u.get("kind") != PROBE_KIND]
            probe = read_probe_result(rundir, units_doc["units"])
            states = read_unit_results(rundir, reading, frozenset(owner),
                                       _read_context(rundir))
            candidates = check_quotes(rundir / "snapshot",
                                      build_candidates(states, reading, owner))
            batches = route(candidates)
            # Applied HERE, with the rest, and for the reason the clustering redo gives: a
            # run must not lose its routing to a refusal about a reading result. Everything
            # that can say no has said it by now.
            if taking:
                apply_redo(rundir, taking, units_doc, ROUTE_REDO_OUTPUTS)
            write_route(rundir, units_doc, states, candidates, batches, companions, problem,
                        probe, _read_area_tests(rundir))
        finally:
            _released(lock)
    except ReviewPanelError as exc:
        sys.stderr.write(f"{_PROG}: {exc}\n")
        return 2
    sys.stdout.write(route_summary(states, candidates, batches, probe))
    return 0


def _run_cluster(args: argparse.Namespace) -> int:
    # Everything that can refuse runs before anything is written: the listing must be at the
    # verification stage and unclustered, the route record readable, the companions
    # readable, the routing proved and every verification result parsed — and only then does
    # the first clustering unit directory land. A redo is DECIDED up here with the rest and
    # APPLIED down there with the rest, for the same reason: a run must not lose its
    # clustering to a refusal about the route record.
    try:
        rundir = _resolve(Path(args.rundir), "rundir", RunDirError)
        # Route's reasoning, one stage along: the marker makes an interrupted cluster
        # resumable and excludes nobody, so the claim is what keeps two clusters from taking
        # back each other's unit directories. Taken before the marker is read, held through
        # the marker write.
        lock = _claim_stage(rundir, "cluster")
        try:
            units_doc = _read_units_at_stage(
                rundir,
                (VERIFICATION_STAGE, CLUSTERED_STAGE, SYNTHESIZED_STAGE, REPORTED_STAGE)
                if args.redo else VERIFICATION_STAGE,
                "cluster runs once, after route and after the verification units are "
                "dispatched"
                + (" — and --redo needs a run that has already clustered" if args.redo else ""))
            taking: tuple[str, ...] = ()
            if args.redo:
                taking, units_doc = plan_redo(rundir, units_doc)
            routed = _read_candidates(rundir)
            problem = _read_problem(rundir)
            companions = load_cluster_companions()
            batches = [u for u in units_doc["units"] if u.get("kind") == VERIFIER_KIND]
            reproducible = {c["id"]: any(r["reproduction"] is not None for r in c["raised_by"])
                            for c in routed["candidates"]}
            holder = check_routing(routed["candidates"], batches)
            states = read_verification_results(rundir, batches, reproducible)
            rationales = verified_rationales(routed["candidates"], holder, states)
            units = plan_clusters(routed["candidates"])
            if taking:
                apply_redo(rundir, taking, units_doc)
            write_clusters(rundir, units_doc, routed["candidates"], rationales, units,
                           companions, problem)
        finally:
            _released(lock)
    except ReviewPanelError as exc:
        sys.stderr.write(f"{_PROG}: {exc}\n")
        return 2
    sys.stdout.write(cluster_summary(units, states))
    return 0


def _run_synthesize(args: argparse.Namespace) -> int:
    # Everything that can refuse runs before anything is written: the listing must be at the
    # clustering stage and unsynthesized, the route record and the companions readable, the
    # routing and the clustering listings proved and every verification and clustering
    # result read — and only then does the unit directory land.
    #
    # ``dispatch.json`` is deliberately NOT read here. The dispatcher writes it once the
    # last round has landed, and this is that round; requiring it would make the record of
    # how the run ran a prerequisite of the last thing that runs. Nothing in this payload
    # needs it — see :func:`synthesis_material` on what the payload states and what it
    # leaves out.
    try:
        rundir = _resolve(Path(args.rundir), "rundir", RunDirError)
        # Route's reasoning again: the claim before the marker, held through the marker
        # write, so a second synthesize cannot take back the unit this one just wrote.
        lock = _claim_stage(rundir, "synthesize")
        try:
            units_doc = _read_units_at_stage(
                rundir, CLUSTERED_STAGE,
                "synthesize runs once, after cluster and after the clustering units are "
                "dispatched")
            routed = _read_candidates(rundir)
            problem = _read_problem(rundir)
            companions = load_synthesis_companions()
            batches = [u for u in units_doc["units"] if u.get("kind") == VERIFIER_KIND]
            reproducible = {c["id"]: any(r["reproduction"] is not None for r in c["raised_by"])
                            for c in routed["candidates"]}
            holder = check_routing(routed["candidates"], batches)
            states = read_verification_results(rundir, batches, reproducible)
            rationales = verified_rationales(routed["candidates"], holder, states)
            cluster_units = [u for u in units_doc["units"] if u.get("kind") == CLUSTERER_KIND]
            check_clustering(routed["candidates"], cluster_units)
            cluster_states = read_clustering_results(rundir, cluster_units)
            clustering = build_clusters(routed["candidates"], cluster_units, cluster_states)
            material = synthesis_material(clustering, routed["candidates"], rationales)
            units = plan_synthesis(clustering.clusters)
            write_synthesis(rundir, units_doc, units, material, companions, problem)
        finally:
            _released(lock)
    except ReviewPanelError as exc:
        sys.stderr.write(f"{_PROG}: {exc}\n")
        return 2
    sys.stdout.write(synthesis_summary(units, clustering.clusters))
    return 0


def _run_report(args: argparse.Namespace) -> int:
    # Everything that can refuse runs before anything is written: this run must not already
    # hold a finished report, the listing must be at the clustering stage or the synthesis
    # one after it, the route record and the dispatch record readable and strict, the
    # routing and the clustering listings proved, every verification and clustering unit
    # classified, every candidate resolved to one status and placed in one cluster — and
    # only then do the three documents land.
    #
    # **Two records, and they answer different questions.** The stamp beside the three
    # outputs is what PROTECTS a published report: no other stage writes it, so no other
    # stage can undo it, and it is what the refusals below are keyed to. The marker in
    # `units.json` is what says how far the run GOT, for the one owner that holds the run
    # across every stage; it is advanced last, after the three files are published and the
    # lock released, and a run whose publication finished while its marker write did not is
    # finished here rather than refused.
    try:
        rundir = _resolve(Path(args.rundir), "rundir", RunDirError)
        target, data_target = rundir / REPORT_NAME, rundir / FINDINGS_NAME
        html_target = rundir / HTML_NAME
        outputs = (target, data_target, html_target)
        for path in outputs:
            _replaceable(path)
        # **What is on disk says which of three things happened, and a file alone says
        # none of them.** The stamp lands before the first of the three and nothing lands
        # after the last, so: all three and the stamp is a report that finished, and
        # replacing it would rewrite the structure under prose somebody may have annotated;
        # fewer than three with the stamp is a publication interrupted part-way, which this
        # report reclaims and redoes, and refusing there is what strands a run that could
        # simply be finished; anything present with no stamp at all was published under
        # rules that kept no such record, and is somebody's rather than this run's. The flag
        # is how somebody says they meant to replace either of the two that are refused.
        if not args.rerender:
            present = [path for path in outputs
                       if _lstat_or_absent(path, "the report output", RunDirError)
                       is not None]
            if present and read_report_stamp(rundir) is None:
                raise RunDirError(
                    f"{present[0]} is a published report this run kept no commit record "
                    f"for, so it may have been read and annotated; report runs once, so "
                    f"move it aside or pass --rerender to replace it"
                )
            if published_report(rundir, outputs) and reported(rundir):
                raise RunDirError(
                    f"{target} is a published report; report runs once, so move it aside "
                    f"or pass --rerender to rewrite it from the run directory as it stands"
                )
        units_doc = _read_units_at_stage(
            rundir, REPORTABLE_STAGES,
            "report runs once, after route and cluster and after the clustering units are "
            "dispatched")
        # **A publication that finished and a marker that did not is finished, not
        # refused.** The three files and the stamp are all there, so re-rendering would
        # rewrite a structure under prose somebody may have annotated; the only outstanding
        # work is the marker, which is taken under the publication lock like every other
        # write of this stage. Refusing here instead would strand a run one write from done
        # — the exact state a kill between the last file and the marker leaves.
        if not args.rerender and published_report(rundir, outputs) and not reported(rundir):
            lock = _claim_stage(rundir, "report")
            try:
                # **Asked again HERE, under the lock, of the disk.** The test above ran
                # before the exclusion, so what it saw can be gone: a redo takes the three
                # files away and commits an earlier marker, and a recovery that trusted the
                # earlier reading would write `reported` over a run with no report in it at
                # all. The stage test is not evidence the files are still there — it is a
                # different question about a different file.
                still = published_report(rundir, outputs)
                if still:
                    advance_to_reported(rundir)
            finally:
                _released(lock)
            if still:
                sys.stdout.write(
                    f"{target} was already published; the run is now recorded as "
                    f"{REPORTED_STAGE!r}.\n")
                return 0
            # The publication this recovery was going to record is gone, so there is
            # nothing to record. Carry on and render one: the ordinary path takes the lock
            # again and refuses there if a report has since been published.
        routed = _read_candidates(rundir)
        job = _read_job(rundir)
        job_notes = _read_job_notes(rundir)
        inventory = _read_inventory(rundir)
        areas = _read_run_json(rundir, "areas.json")
        if not isinstance(areas, dict) or not isinstance(areas.get("areas"), list):
            raise RunDirError(f"areas.json under {rundir} is not the engine's")
        dispatch = load_dispatch(rundir)
        batches = [u for u in units_doc["units"] if u.get("kind") == VERIFIER_KIND]
        reproducible = {c["id"]: any(r["reproduction"] is not None for r in c["raised_by"])
                        for c in routed["candidates"]}
        holder = check_routing(routed["candidates"], batches)
        states = read_verification_results(rundir, batches, reproducible)
        resolved = resolve(routed["candidates"], holder, states)
        cluster_units = [u for u in units_doc["units"] if u.get("kind") == CLUSTERER_KIND]
        check_clustering(routed["candidates"], cluster_units)
        cluster_states = read_clustering_results(rundir, cluster_units)
        clustering = build_clusters(routed["candidates"], cluster_units, cluster_states)
        synthesis_units = [u for u in units_doc["units"] if u.get("kind") == SYNTHESIZER_KIND]
        check_synthesis(clustering.clusters, synthesis_units)
        # ``check_synthesis`` above is what proves the listing is over THIS run's defects;
        # the parse then proves the reply is over the listing. Neither needs the clusters
        # again, so this takes the unit row and the state and nothing else.
        synthesis = build_synthesis(synthesis_units,
                                    read_synthesis_result(rundir, synthesis_units))
        snippets = {cand["id"]: extract_snippet(rundir / "snapshot", cand["file"],
                                                cand["line_start"], cand["line_end"])
                    for cand in routed["candidates"]}
        findings = build_findings(dispatch, routed["candidates"], resolved, clustering,
                                  cluster_states, inventory["commit"], snippets, synthesis,
                                  states, batches)
        # Checked against the defects just built, so a note naming a defect this run does
        # not have is refused before anything is published.
        report_notes = _read_report_notes(rundir, [c["id"] for c in findings.clusters])
        # All three files are one output and they publish under one lock, and so do the
        # stamp they state and the marker that commits them. The lock is a file of its own
        # and NOT one of the three: a claim that is also a published artifact stops being a
        # claim the moment it exists again, because a re-render's whole job is to move a
        # published artifact aside. Exclusive create is the only thing here two processes
        # cannot both do. A crash leaves it behind and the refusal names it, which is a
        # worse day for one operator than silently publishing one run's structure beside
        # another's prose is for everyone who reads the result.
        #
        # The data above was read WITHOUT the lock, and that is unchanged: a redo can still
        # reset the run between those reads and this publication. Closing that needs the
        # exclusion to span both, which is a different change from this one.
        lock = _claim_stage(rundir, "report")
        aside: dict[Path, Path] = {}
        reserved: list[Path] = []
        # Declared out here so the recovery can read it whatever went wrong. An unknown path
        # defaults to "it was already there", which is the conservative answer: it means
        # leave the file alone, and the only way to be wrong is to keep a file this call
        # never made rather than to delete one somebody else did.
        existed: dict[Path, bool] = {}
        try:
            # The completion check at the top of this stage ran BEFORE the lock, so two
            # first runs can both have passed it. Asked again here, under the lock, it means
            # what it says: without --rerender a report that finished while this one was
            # working is somebody's output, possibly annotated, and not this run's to
            # replace. Asked of the disk, not of what this call read a moment ago.
            # Asked of `published_report` ALONE, unlike the two checks before the lock.
            # The one state those checks let through — published, marker not yet advanced —
            # has already returned above, so anything published that reaches here finished
            # while this report was preparing, and is somebody's output rather than this
            # run's to replace. Adding the marker to this test would let exactly that be
            # overwritten.
            if not args.rerender and published_report(rundir, outputs):
                raise RunDirError(
                    f"{target} was published while this report was preparing; report runs "
                    f"once, so pass --rerender to replace what that report wrote"
                )
            # A leftover of THIS stage is still the stage's to refuse if it is a link or a
            # directory — what the file says about completion says nothing about its kind.
            for path in outputs:
                _replaceable(path)
            # The scratch files an interrupted report left beside its own four writes, taken
            # back under the lock and before the first of those writes. The pid comes round
            # again, and `_create_scratch` creates exclusively, so one left behind refuses a
            # write nobody can explain — and the stamp's is the earliest of the four, so a
            # reclaim any later than this would still be too late for it. Enumerated from
            # the paths this stage writes; the published files themselves are not taken,
            # because the move-aside below is what takes those back and it can put them
            # where it found them.
            #
            # **`units.json` is deliberately NOT among them, and this is the one entry worth
            # a sentence.** Report does write that file now — the terminal marker is its
            # last write — so by the ordinary rule its scratch would be report's to take
            # back. It is not, and the reason is what a synthesis does with the same name: a
            # synthesis holding its own claim writes `units.json.<pid>.tmp` and then
            # replaces it, and a report reclaiming that path in the window between deletes a
            # live scratch file, so the synthesis fails its commit while the report
            # succeeds. **What that costs is stated rather than traded away:** a report
            # hard-killed between creating its own marker scratch and replacing it strands
            # that file, and the next report running under the same pid — the pid comes
            # round again — is refused by name until somebody removes it. One operator
            # clearing a named file is the cheaper failure.
            _reclaim("report", rundir, (), (),
                     (rundir / REPORT_STAMP_NAME, target, data_target, html_target))
            # **The stamp is chosen and persisted under the same exclusion as the
            # publication, and read again now that this report owns it.** Chosen before the
            # lock it can be a losing report's, written over a winner's after the winner has
            # already published it — leaving the report and the stamp disagreeing, so the
            # next re-render moves the bytes the stamp exists to hold still. Every later
            # rendering of one run has to reproduce what it is replacing: a re-render
            # rewrites unchanged data, and a report rebuilt after an interrupted one is the
            # same rendering finished. Reading the clock a second time made that true only
            # while both landed inside one minute, and a slow runner turned it into a red
            # job on a change that touched no timestamp code.
            #
            # Its own file, which a reclaim does not take, so it survives the report being
            # taken back; ``report.md`` is the fallback for a run directory that carries a
            # report and no stamp file beside it.
            generated = read_report_stamp(rundir)
            wrote_stamp = generated is None
            if generated is None:
                generated = ((_previous_stamp(target) if args.rerender else None)
                             or clock_now())
                write_report_stamp(rundir, generated)
            # Rendered here rather than before the lock, because the stamp it states is
            # chosen here. Passed in rather than read inside the renderer, which is what
            # makes holding it still a thing a caller can do at all.
            #
            # The stamp this call wrote is taken back if the render fails. It is written
            # first so the three files can be judged against it, and a render that raised
            # left it standing over no report at all -- the next plain `report` then read
            # the run as already published and rendered nothing. A stamp that was already
            # there is somebody else's record and stays.
            try:
                text = render_report(job, dispatch, inventory, areas["areas"], findings,
                                     routed["units"], batches, states, routed["probe"],
                                     job_notes=job_notes, report_notes=report_notes,
                                     rundir=rundir, generated=generated)
            except BaseException:
                if wrote_stamp:
                    with contextlib.suppress(OSError):
                        (rundir / REPORT_STAMP_NAME).unlink()
                raise
            # A diagnostic quotes the offending input (an unknown key, say), and that input
            # can hold a lone surrogate no UTF-8 write accepts. Escaping keeps the report
            # landing — and deterministic — instead of a traceback beside a leftover scratch
            # file. findings.json needs no such pass: write_json escapes every non-ASCII
            # character.
            text = text.encode("utf-8", "backslashreplace").decode("utf-8")
            # Converted from the prose, not written beside it: one document is composed, and
            # the page is that document in another format. Nothing can be in one and not the
            # other, because there is only one place either of them is decided. Converted
            # AFTER the escape above, so the page inherits a prose that is already writable.
            page = render_html(text)
            published = ((data_target, findings_document(findings)), (target, text),
                         (html_target, page))
            # What was there when the lock was taken, so the recovery can put back that
            # state rather than the files it happened to touch. A path that did not exist is
            # part of the state: restoring only what was moved aside leaves a new rendering
            # beside an old one, which is the disagreement this arrangement exists to
            # prevent.
            existed.update({path: _lstat_or_absent(path, "the report output",
                                                   RunDirError) is not None
                            for path, _content in published})
            for path, _content in published:
                if not existed[path]:
                    continue
                spare = _temp_beside(path.with_name(path.name + ".previous"))
                # Exclusively, so a backup an interrupted run left under the same name — the
                # pid comes round again — is a refusal rather than something to overwrite
                # and then delete. Recorded as reserved before the move, so a move that
                # fails does not strand the empty file it just made.
                try:
                    with open(spare, "x", encoding="utf-8"):
                        pass
                    reserved.append(spare)
                    os.replace(path, spare)
                except OSError as exc:
                    raise RunDirError(
                        f"cannot take {path} aside to re-render it: {exc}"
                    ) from exc
                aside[path] = spare
            for path, content in published:
                if path is data_target:
                    write_json(path, content)
                else:
                    write_text(path, content)
            # **The marker last, after all three files are on disk.** It records where the
            # run got to, for the owner that reads it to decide what to do next; the stamp
            # beside the three files is what protects the publication, and it was written
            # before the first of them. So the order is stamp, files, marker: a kill
            # anywhere in it leaves a state the next run can name and finish, and no state
            # where the marker says reported and the files do not exist.
            #
            # The listing is re-read inside `advance_to_reported` rather than written back
            # from what this call read at the start, so a redo that landed meanwhile keeps
            # its own listing.
            advance_to_reported(rundir)
        except BaseException:
            # Back to the state this call found. A rendering that landed must never sit
            # beside one that did not: the three are views of one structure, and two of them
            # describing different data is the one state this stage exists to make
            # impossible.
            stranded: list[str] = []
            for path in (data_target, target, html_target):
                spare = aside.get(path)
                if spare is None:
                    # Already there and never moved: this call did not touch it. Only a path
                    # that did not exist when the lock was taken can have been created here.
                    if existed.get(path, True):
                        continue
                    try:
                        path.unlink()
                    except FileNotFoundError:
                        pass
                    except OSError:
                        # The file this call created cannot be removed, so it is left beside
                        # renderings of other data. Silence here is what would let a reader
                        # believe the failure changed nothing.
                        stranded.append(str(path))
                    continue
                try:
                    os.replace(spare, path)
                except OSError:
                    # The backup is NOT discarded. It is the only copy of what was there,
                    # annotations included, and deleting it would turn a misplaced file into
                    # a lost one. Named instead, so an operator can put it back by hand.
                    stranded.append(f"{path} (its previous content is at {spare})")
                else:
                    reserved.remove(spare)
            for spare in reserved:
                if spare not in aside.values():
                    _discard(spare)
            _release(lock)
            if stranded:
                raise RunDirError(
                    f"{'; '.join(sorted(stranded))} — the run directory now holds renderings "
                    f"of different data and all of them must be discarded"
                )
            raise
        for spare in aside.values():
            _discard(spare)
        _released(lock)
    except ReviewPanelError as exc:
        sys.stderr.write(f"{_PROG}: {exc}\n")
        return 2
    sys.stdout.write(report_summary(states, findings,
                                    (data_target, target, html_target)))
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entrypoint: pin the console encoding, guard the interpreter, parse, dispatch."""
    # Pinned FIRST, before anything can print: on a console whose code page cannot represent
    # an em dash the first such line raises UnicodeEncodeError while reporting. Guarded and
    # per-stream, because a harness may replace either stream with an object lacking
    # ``reconfigure`` — degrade, never abort.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, TypeError, ValueError, OSError):
            pass
    require_python(3, 10)
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "check":
        return _run_check(args)
    if args.command == "plan":
        return _run_plan(args)
    if args.command == "route":
        return _run_route(args)
    if args.command == "cluster":
        return _run_cluster(args)
    if args.command == "synthesize":
        return _run_synthesize(args)
    return _run_report(args)


if __name__ == "__main__":
    sys.exit(main())
