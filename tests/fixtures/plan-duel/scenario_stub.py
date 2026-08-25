#!/usr/bin/env python3
"""Scenario-driven stub 'CLI' for the plan-duel engine's END-TO-END parity tests.

Unlike ``stub_cli.py`` (which exercises the low-level process-exec seam), this stub lets a
golden *scenario* fixture drive the whole run loop: on each dispatch the engine invokes it
with the round + role, and it lands that scenario's canned artifact in the workdir. The
engine's real subprocess path is fully exercised while spawning NO branded participant CLI.

Invoked ONLY as ``[sys.executable, scenario_stub.py, ...]`` — a plain Python script, never a
shebang/exec-bit file — so it runs identically on the Windows CI runner.

    scenario_stub.py --scenario-dir DIR --role {agent_a|agent_b|judge}
                     --round N --workdir WD

Default behavior: copy the scenario's canned input for (role, round) into the workdir at the
engine-expected path:

    agent_a round N -> DIR/inputs/plan-a-round-N.md  -> WD/plan-a.md
    agent_b round N -> DIR/inputs/plan-b-round-N.md  -> WD/plan-b.md
    judge   round N -> DIR/inputs/judge-round-N.md   -> WD/judge-round-N.md

For agent roles with N>=1 a small rejections file is also synthesized.

Optional ``DIR/script.json`` maps ``"role:round"`` to an override so a scenario can exercise
the failure seams:

    {"short": true}          write a <200 B file (triggers the agent halt / fallback)
    {"stray": "NAME"}        also write a >=200 B recent WD/NAME (round-0 B fallback)
    {"missing": true}        write nothing (empty/failed judge -> JudgeOutputError)
    {"symlink": "NAME"}      point the output at WD/NAME instead of writing it, the
                             substitution an exists()+size gate cannot see through
                             (POSIX only; callers skip the case on Windows)
"""

import argparse
import json
import sys
from pathlib import Path


def _load_overrides(scenario_dir: Path) -> dict:
    script = scenario_dir / "script.json"
    if script.is_file():
        return json.loads(script.read_text(encoding="utf-8"))
    return {}


def _dest_for(role: str, round_n: int, workdir: Path) -> Path:
    if role == "agent_a":
        return workdir / "plan-a.md"
    if role == "agent_b":
        return workdir / "plan-b.md"
    if role == "judge":
        return workdir / f"judge-round-{round_n}.md"
    raise SystemExit(f"scenario_stub: unknown role {role!r}")


def _canned_source(scenario_dir: Path, role: str, round_n: int) -> Path:
    inputs = scenario_dir / "inputs"
    if role == "agent_a":
        return inputs / f"plan-a-round-{round_n}.md"
    if role == "agent_b":
        return inputs / f"plan-b-round-{round_n}.md"
    return inputs / f"judge-round-{round_n}.md"


def main() -> int:
    parser = argparse.ArgumentParser(prog="scenario_stub")
    parser.add_argument("--scenario-dir", required=True)
    parser.add_argument("--role", required=True)
    parser.add_argument("--round", type=int, required=True)
    parser.add_argument("--workdir", required=True)
    args = parser.parse_args()

    scenario_dir = Path(args.scenario_dir)
    workdir = Path(args.workdir)
    role = args.role
    round_n = args.round

    overrides = _load_overrides(scenario_dir)
    override = overrides.get(f"{role}:{round_n}", {})

    dest = _dest_for(role, round_n, workdir)

    if override.get("missing"):
        return 0  # write nothing at all

    link_target = override.get("symlink")
    if link_target:
        # Exits 0 having "produced" the file — as a link to something already there.
        dest.symlink_to(workdir / link_target)
        return 0

    if override.get("short"):
        dest.write_text("too short\n", encoding="utf-8")
    else:
        source = _canned_source(scenario_dir, role, round_n)
        dest.write_bytes(source.read_bytes())

    stray = override.get("stray")
    if stray:
        (workdir / stray).write_text(
            "Recovered stray plan body. " * 12, encoding="utf-8"
        )

    if role in ("agent_a", "agent_b") and round_n >= 1:
        side = "a" if role == "agent_a" else "b"
        (workdir / f"rejections-{side}-round-{round_n}.md").write_text(
            f"Rejections for {side} round {round_n}: none of consequence.\n",
            encoding="utf-8",
        )

    return 0


if __name__ == "__main__":
    sys.exit(main())
