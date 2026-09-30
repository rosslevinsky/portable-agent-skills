#!/usr/bin/env python3
"""Cross-platform stub 'CLI' for the plan-duel engine's process-exec tests.

Stands in for a real participant / judge CLI so the ``unittest`` suite never spawns a branded
tool. Invoked ONLY as ``[sys.executable, stub_cli.py, ...]`` — a plain Python script, never a
shebang/exec-bit file — so it runs identically on the Windows CI runner.

Every behavior is argument-driven and stdlib-only. The flags model the shapes the engine must
handle:

  * ``--write-file PATH --content TEXT`` / ``--min-bytes N`` — an *agent* CLI that
    writes its artifact file directly (padded to N bytes when asked). The engine's
    agent-capture policy reads this FILE, not stdout.
  * ``--stdout TEXT`` / ``--stdout-bytes N`` — noise/transcript on stdout. Used to
    prove the judge capture never trusts raw stdout.
  * ``--echo-arg VALUE --echo-file PATH`` — writes VALUE verbatim to PATH. With a
    ``$SHELL``-style VALUE this proves argv-list execution.
  * ``--cwd-file PATH`` — writes ``os.getcwd()`` to PATH, proving the ``cwd`` anchor.
  * ``--append PATH --content TEXT`` — appends TEXT to PATH (progress-file shape).
  * ``--sleep SECONDS`` — blocks, to exercise the timeout path.
  * ``--exit-code N`` — final exit status (defaults 0), to exercise failure paths.
  * ``--stderr TEXT`` — diagnostics on stderr, which no capture policy may keep.
  * ``--read-stdin-to PATH`` — reads stdin to end-of-file and writes how many bytes
    arrived to PATH. Hangs on an open stdin, which is the property it probes.
  * ``--spawn-grandchild PIDFILE`` — starts a long-sleeping child of its own and writes
    that child's pid to PIDFILE, so a test can see whether a kill reached the tree.
  * ``--verbose`` — accepted and ignored: an operational flag, the kind a resume may add.
  * ``--env-digest NAME=PATH`` (repeatable) — writes the SHA-256 of variable NAME's value to
    PATH, or ``unset``, so a test can see a value arrive without that value landing on disk.

Order of operations is fixed: grandchild, stdin, sleep, then side-effect writes (digests
first), then stdout and stderr, then exit.
"""

import argparse
import hashlib
import os
import subprocess
import sys
import time
from pathlib import Path


def _padded(content: str, min_bytes: int | None) -> str:
    if not min_bytes:
        return content
    while len(content.encode("utf-8")) < min_bytes:
        content += "x"
    return content


def main() -> int:
    parser = argparse.ArgumentParser(prog="stub_cli")
    parser.add_argument("--write-file")
    parser.add_argument("--content", default="")
    parser.add_argument("--min-bytes", type=int)
    parser.add_argument("--append")
    parser.add_argument("--stdout")
    parser.add_argument("--stdout-bytes", type=int)
    parser.add_argument("--echo-arg")
    parser.add_argument("--echo-file")
    parser.add_argument("--cwd-file")
    parser.add_argument("--sleep", type=float)
    parser.add_argument("--exit-code", type=int, default=0)
    parser.add_argument("--stderr")
    parser.add_argument("--read-stdin-to")
    parser.add_argument("--spawn-grandchild")
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("--env-digest", action="append", default=[])
    args = parser.parse_args()

    if args.spawn_grandchild is not None:
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"],
                                 stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL)
        Path(args.spawn_grandchild).write_text(str(child.pid), encoding="utf-8")

    if args.read_stdin_to is not None:
        received = sys.stdin.buffer.read()
        Path(args.read_stdin_to).write_text(str(len(received)), encoding="utf-8")

    if args.sleep:
        time.sleep(args.sleep)

    for pair in args.env_digest:
        name, _, path = pair.partition("=")
        value = os.environ.get(name)
        digest = ("unset" if value is None
                  else hashlib.sha256(value.encode("utf-8")).hexdigest())
        Path(path).write_text(digest, encoding="utf-8")

    if args.append is not None:
        # Append-only; never truncates a shared progress log.
        with open(args.append, "a", encoding="utf-8", newline="") as handle:
            handle.write(args.content)

    if args.write_file is not None:
        Path(args.write_file).write_text(
            _padded(args.content, args.min_bytes), encoding="utf-8"
        )

    if args.echo_arg is not None and args.echo_file is not None:
        Path(args.echo_file).write_text(args.echo_arg, encoding="utf-8")

    if args.cwd_file is not None:
        Path(args.cwd_file).write_text(os.getcwd(), encoding="utf-8")

    if args.stdout is not None:
        sys.stdout.write(args.stdout)
    if args.stdout_bytes:
        sys.stdout.write("y" * args.stdout_bytes)
    if args.stderr is not None:
        sys.stderr.write(args.stderr)

    return args.exit_code


if __name__ == "__main__":
    sys.exit(main())
