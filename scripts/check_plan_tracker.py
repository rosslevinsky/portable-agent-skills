#!/usr/bin/env python3
"""Plan tracker check — is a plan's ``execution.md`` well formed?

`plan-phase` writes `plans/<slug>/execution.md`, a checkbox list linking to one phase
document per phase. `plan-run` then executes the plan by reading it: find the first unticked
box, open its phase document, do the work, tick the box. The tracker is the run's state, so a
malformed one is not cosmetic — a box written with `*` instead of `-`, or indented, is a
phase that silently never runs, and a `phase-*.md` no box links is a whole phase the run
skips without a word.

    python3 scripts/check_plan_tracker.py plans/<slug>/execution.md   # one tracker
    python3 scripts/check_plan_tracker.py plans/                      # every execution.md

**Why this is not in `validate_cross_runtime.py`.** That script checks that skills are
portable — no private paths, no POSIX-only assumptions, no unbounded `codex exec`. This
checks one document format the planning suite invented, for typos. The two share a language
and nothing else, and bundling them meant `plan-phase` told its reader to "run the pack's
validator" over their plan.

Its rules are exercised by `tests/test_plan_tracker.py`, a **unittest** suite rather than the
validator's internal fixture harness, which runs on Ubuntu and macOS only — so every rule
here was unverified on Windows, the platform `plan-run` is most likely to be on when it reads
a tracker it did not write.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path


# The phase document is the durable state — every task, test and exit criterion is a box
# ticked as work proceeds — so `execution.md` is a derived INDEX, and needs no grammar.
#
# Scope is decided by FILENAME, never by file contents. Gating the check on a
# `| Format | v2 |` row parsed out of the document turned every parser bug into silently
# skipped validation. A directory scan therefore inspects only files named `execution.md`,
# and this checker interprets no Markdown.
#
# The threat model is a TYPO, not an adversary: `plan-phase` writes this file, `plan-run`
# reads it, and a human reviews the diff. Rules that cost nothing (an ASCII character class,
# a length bound) are here; rules that would cost another scan over the text are not.
_TRACKER_SLUG = r"phase-[0-9]{2,9}-[a-z0-9]{1,40}(?:-[a-z0-9]{1,40}){0,15}"
TRACKER_BOX = re.compile(r"^- \[([ x])\] \[[^\]]+\]\(\./(" + _TRACKER_SLUG + r"\.md)\)[ \t]*$")
# The superseded `- phase:` shape, identified POSITIVELY rather than inferred from "no
# boxes". Those two conditions otherwise describe the same file, so a truncated new tracker
# would be waved through as a finished record. Every such line must be a well-formed entry
# naming a real phase document here — a bare token let `- phase: ../elsewhere/x` traverse
# out of the plan and left an empty `- phase:` unrecognised.
TRACKER_LEGACY_LINE = re.compile(r"^-[ \t]+phase:[ \t]*(.*?)[ \t]*$")
TRACKER_LEGACY_SLUG = re.compile(_TRACKER_SLUG)
# Banned rather than parsed: a fence or an HTML comment can hide a whole region of the
# file, and no real tracker has ever contained one.
TRACKER_BANNED = ("```", "~~~", "<!--", "-->")


def check_tracker(filepath: Path) -> tuple[list[str], list[str]]:
    """Check one execution tracker. Returns ``(errors, notices)``.

    Five rules, none of which needs a Markdown model: no fence or comment delimiter appears
    anywhere; every column-0 checkbox line is the exact canonical form; its link resolves to
    a regular file in this plan directory and appears once; every ``phase-*.md`` here is
    listed; and there is at least one checkbox. A superseded ``- phase:`` tracker is
    reported as a notice and skipped.
    """
    try:
        text = filepath.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return ([f"  Cannot read {filepath}: {exc}"], [])
    # CommonMark line endings only. `str.splitlines()` also breaks on VT, FF and U+2028,
    # so a header and a checkbox separated by one would read as two lines here and one to
    # every renderer.
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")

    at = f"  {filepath}"
    errors = [f"{at}:{i}: contains '{d}'; a tracker carries no code fences and no HTML "
              f"comments, so no region of it can be hidden from a reader or this checker"
              for i, line in enumerate(lines, 1) for d in TRACKER_BANNED if d in line]
    # `*` and `+` are bullet markers too: scanning only `- [` meant a reader saw a checkbox
    # this checker ignored and `plan-run` never executed.
    boxed = [i for i, line in enumerate(lines, 1)
             if line[:1] in ("-", "*", "+") and line[1:3] == " ["]
    legacy = [m.group(1) for line in lines if (m := TRACKER_LEGACY_LINE.match(line))]
    if (legacy and not boxed and not errors
            and all(TRACKER_LEGACY_SLUG.fullmatch(name)
                    and not (filepath.parent / f"{name}.md").is_symlink()
                    and (filepath.parent / f"{name}.md").is_file() for name in legacy)):
        return ([], [f"{at}: legacy tracker ({len(legacy)} superseded '- phase:' entries)"
                     f" — not checked"])
    if legacy and boxed:
        errors.append(f"{at}: mixes checkbox phases with superseded '- phase:' entries; a "
                      f"half-migrated tracker is neither shape")

    phases: dict[str, int] = {}
    for idx in boxed:
        match = TRACKER_BOX.match(lines[idx - 1])
        if not match:
            errors.append(f"{at}:{idx}: not the canonical form "
                          f"'- [ ] [<name>](./phase-<NN>-<slug>.md)'; a line this checker "
                          f"cannot read is a phase that silently leaves the list")
            continue
        target = match.group(2)
        if target in phases:
            errors.append(f"{at}:{idx}: '{target}' is listed twice (first at line "
                          f"{phases[target]}); the list is the execution order")
            continue
        phases[target] = idx
        linked = filepath.parent / target
        if linked.is_symlink() or not linked.is_file():
            errors.append(f"{at}:{idx}: '{target}' is not a regular file here; a link "
                          f"resolving elsewhere, or nowhere, cannot be executed")

    if not phases:
        errors.append(f"{at}: no phase checkboxes; zero is a hard error, never 'all "
                      f"phases complete' — reading an empty tracker as finished would "
                      f"finalise a plan whose work never ran")
    # Every `phase-*.md` ENTRY, whatever kind it is: filtering to regular files here let an
    # unlisted symlink or directory — a phase that would never be executed — pass unnoticed.
    for name in sorted({p.name for p in filepath.parent.glob("phase-*.md")} - set(phases)):
        errors.append(f"{at}: '{name}' is in this directory but no checkbox links it; a "
                      f"phase missing from the tracker is never executed")
    return (errors, [])


def run_tracker_check(target: Path) -> int:
    """Check ``target`` (a tracker file, or every tracker under a directory); return an
    exit code.

    A directory scan inspects **only** files named ``execution.md``. Anything looser and
    every phase document is examined as a candidate tracker — and a v1 ``phases.md``, now
    the same checkbox shape, would be dragged into a suite that has no business reading it.
    An explicit file path is checked whatever it is called, which is how ``plan-phase``
    verifies a tracker it has just written.
    """
    if target.is_dir():
        files = sorted(target.rglob("execution.md"))
    elif target.is_file():
        files = [target]
    else:
        print(f"Path not found: {target}")
        return 1

    errors: list[str] = []
    notices: list[str] = []
    for f in files:
        found, noted = check_tracker(f)
        errors.extend(found)
        notices.extend(noted)
    # Printed, never silent: a skipped file the operator cannot see is indistinguishable
    # from a checked one, which is the failure this whole redesign is against.
    for notice in notices:
        print(notice)
    if errors:
        print(f"Tracker validation FAILED ({len(errors)} issues):")
        for error in errors:
            print(error)
        return 1
    print(f"Tracker validation passed ({len(files) - len(notices)} tracker(s) checked, "
          f"{len(notices)} legacy skipped).")
    return 0

def main() -> int:
    # Diagnostics quote the files they check, and a plan's headings are full of em dashes.
    # On a console whose encoding cannot represent one — a non-UTF-8 default, which is what
    # the CI encoding proxy simulates — printing a finding would raise UnicodeEncodeError
    # and take down the run *reporting* the problem rather than the problem itself. Reads
    # are pinned to utf-8 at the call site; this pins the other end of the pipe. Guarded
    # because a caller may have replaced sys.stdout with an object that has no reconfigure.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):  # pragma: no cover - depends on host stream
            pass

    parser = argparse.ArgumentParser(
        description="Check a plan's execution.md checkbox tracker")
    parser.add_argument(
        "path", type=Path,
        help="an execution.md to check, or a directory to scan (e.g. plans/). A directory "
             "scan inspects only files named execution.md; a superseded '- phase:' tracker "
             "is reported by path and skipped.")
    args = parser.parse_args()
    return run_tracker_check(args.path)


if __name__ == "__main__":
    sys.exit(main())
