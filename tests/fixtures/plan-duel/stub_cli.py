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

Order of operations is fixed: sleep, then side-effect writes, then stdout, then exit.
"""

import argparse
import os
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
    args = parser.parse_args()

    if args.sleep:
        time.sleep(args.sleep)

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

    return args.exit_code


if __name__ == "__main__":
    sys.exit(main())
