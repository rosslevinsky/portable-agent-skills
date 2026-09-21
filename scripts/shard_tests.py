#!/usr/bin/env python3
"""Run one shard of the test suite.

CI's long pole is a single `unittest discover`, and the only way to make one machine's
wall clock shorter is to stop giving it the whole suite. This splits the discovered tests
across N runners, deterministically, so every test runs on exactly one of them.

**Split by test CLASS, not by module.** One module is well over half the suite's runtime,
so a split by module can never be faster than that module — the shard holding it sets the
wall clock and the others finish early and wait. Classes are the smallest unit that can be
divided without breaking `setUpClass`, which is shared state a test may rely on and which
this has no business separating.

The assignment is round-robin over the class names in sorted order. Deterministic, so a
failure lands on the same shard when it is re-run; and total, because a class is assigned
by its position rather than by a rule that has to be kept in step with the suite — a test
file added tomorrow lands on a shard without anybody editing this.
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import unittest


def classes(start: str, pattern: str) -> dict[str, unittest.TestSuite]:
    """Every discovered test, grouped by the class that holds it, keyed by its full name.

    A discovery ERROR — a module that will not import — reaches here as a synthetic test
    case of its own, and is grouped and run like any other. That is deliberate: dropping
    what could not be loaded is how a broken module becomes a green shard.
    """
    grouped: dict[str, unittest.TestSuite] = {}

    def walk(suite):
        for test in suite:
            if isinstance(test, unittest.TestSuite):
                walk(test)
            else:
                key = f"{type(test).__module__}.{type(test).__qualname__}"
                grouped.setdefault(key, unittest.TestSuite()).addTest(test)

    walk(unittest.TestLoader().discover(start, pattern=pattern))
    return grouped


def shard_of(names: list[str], index: int, total: int) -> list[str]:
    """The names this shard owns. Sorted first, so the split does not move with the order
    discovery happened to return."""
    return sorted(names)[index - 1::total]


def run_all(total: int, passthrough: list[str]) -> int:
    """Every shard at once, as child processes, returning non-zero if any of them failed.

    CI gets its speed from running the shards on N MACHINES. A developer has one, so this
    is worth exactly what the machine's cores are worth and no more: measured on two cores,
    four shards take half the wall clock of four in a row, not a quarter. Half is still half.

    **Bytecode writing is off in the children**, and that is what makes running them at once
    safe rather than merely faster. Concurrent interpreters import the same modules and race
    to write the same `__pycache__` directories; the projection tests then find bytecode
    under `skills/` that the manifest does not classify, and a suite that passes alone fails
    in company. With it off there is nothing to clean up between runs either.

    Output is captured per shard and printed whole, in shard order, once everything is done.
    Streamed, four suites interleave line by line into something no one can read.
    """
    env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
    running = [
        (index, subprocess.Popen(
            [sys.executable, os.path.abspath(__file__),
             "--index", str(index), "--total", str(total), *passthrough],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
            encoding="utf-8", errors="replace", env=env))
        for index in range(1, total + 1)
    ]
    failed: list[int] = []
    for index, child in running:
        output, _ = child.communicate()
        sys.stdout.write(f"\n===== shard {index}/{total} =====\n{output}")
        if child.returncode != 0:
            failed.append(index)
    sys.stdout.write(
        f"\n{total} shards: {total - len(failed)} passed"
        + (f", {len(failed)} FAILED ({', '.join(str(i) for i in failed)})\n" if failed
           else "\n"))
    return 1 if failed else 0


def main(argv: list[str] | None = None) -> int:
    # Pinned FIRST, before anything can print. A shard's own output carries em dashes, and
    # --parallel writes the children's output through this process: under a C locale the
    # parent's stdout is ASCII, so relaying it raised UnicodeEncodeError after every shard
    # had already passed. Guarded and per-stream, because a harness may replace either with
    # an object that has no `reconfigure` — degrade, never abort.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, TypeError, ValueError, OSError):
            pass
    parser = argparse.ArgumentParser(description="Run one shard of the test suite.")
    parser.add_argument("--index", type=int,
                        help="1-based shard number; omit it when passing --parallel")
    parser.add_argument("--total", type=int, required=True, help="how many shards there are")
    parser.add_argument("--parallel", action="store_true",
                        help="run every shard of --total at once, as child processes, and "
                             "fail if any of them does. Worth what this machine's cores are "
                             "worth: on two cores it halves the wall clock of the same "
                             "shards run one after another")
    parser.add_argument("--start", default="tests", help="directory to discover from")
    parser.add_argument("--pattern", default="test_*.py", help="discovery pattern")
    parser.add_argument("--verbosity", type=int, default=1)
    args = parser.parse_args(argv)
    if args.total < 1:
        parser.error(f"--total {args.total} is not a number of shards")
    # Refused rather than resolved by preferring one. `--index 2 --parallel` is a caller
    # asking for two different runs, and picking either answers a question nobody asked.
    if args.parallel and args.index is not None:
        parser.error("--parallel runs every shard, so it takes no --index")
    if not args.parallel and args.index is None:
        parser.error("give --index for one shard, or --parallel for all of them")
    if args.parallel:
        passthrough = ["--start", args.start, "--pattern", args.pattern,
                       "--verbosity", str(args.verbosity)]
        return run_all(args.total, passthrough)
    if not 1 <= args.index <= args.total:
        parser.error(f"--index {args.index} is not between 1 and --total {args.total}")

    grouped = classes(args.start, args.pattern)
    mine = shard_of(list(grouped), args.index, args.total)
    suite = unittest.TestSuite()
    for name in mine:
        suite.addTests(grouped[name])
    print(f"shard {args.index}/{args.total}: {len(mine)} of {len(grouped)} classes, "
          f"{suite.countTestCases()} tests", flush=True)
    result = unittest.TextTestRunner(verbosity=args.verbosity).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main())
