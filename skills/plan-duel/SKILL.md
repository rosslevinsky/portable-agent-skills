---
name: plan-duel
description: >
  Iterative plan refinement between Claude and Codex. Each agent writes an initial
  plan using a condensed v2 plan methodology embedded in the skill (mirroring
  plan-init's content model), then they alternately critique and
  incorporate the other's best ideas, with a judge checking for convergence after
  each round. Three exits: convergence (score ≥8/10, from round 3 onward),
  stagnation (no score improvement over 3 consecutive rounds), or the 10-round
  cap. Writes a summary.md with a score
  trajectory, a pointer to the winning plan, and a breakdown of remaining
  differences with pros/cons. Supports resume from an interrupted run. Use when
  the user invokes /plan-duel, or says "duel the plans", "run plan duel",
  "cross-compare plans with codex" — the input is a problem statement the skill
  generates competing plans from, not two existing plans to compare against each
  other. Argument: the problem statement (inline text or a file path), or the path
  to an existing plan-duels workdir to resume.
---

# Plan Duel — Driver

_Classification: Degraded — the duel runs from **either** runtime as controller with
the other runtime as participant (both directions are implemented), but every LLM
judgment point is now a subprocess, so two hard prerequisites apply. (1) **Both**
runtimes' CLIs must be present on `PATH` — the three roles span the controller's own
CLI (Agent A and the judge) as well as the participant's; the engine resolves all
three via `shutil.which` and halts naming any that are missing. (2) A **Python
3.10+** interpreter must be available to run the engine (`plan_duel.py`); on absence
or an older interpreter the skill reports `Python 3.10+ required` and stops. The
controller and participant CLIs are supplied as argv **data** by the adapter blocks
below — no runtime name is hardcoded in the engine._

_Progress: observable via a run-level `progress.log` in the workdir — a single,
append-only, timestamped (`[+MM:SS]`) activity log the engine writes several lines to per
round — as it starts and finishes each step (plan generation, each critique, judging) —
plus a heartbeat line every ~15s while a spawn is still in flight (so a
fast spawn produces none) and a final `duel complete — exit=… score=… → summary.md`
terminator. It is best-effort and read by
nothing on the correctness path, so the duel's outcome is identical whether or not anyone
watches it. This is the one channel a controller can observe uniformly (both Claude and
Codex can poll a file; neither reliably streams a blocking subprocess's stdout live) — to
see it live, run the engine in the background and poll `progress.log` (step 5). The
per-round `participant-progress-N.md` files are still written for resume-cleanup
compatibility; human narration (including the full `summary.md`) goes to stdout, which the
engine line-buffers so it streams live._

This skill is a thin driver over `plan_duel.py`, a stdlib-only Python engine that
owns the entire duel: working-directory setup, the round-0 initial plans (each
agent follows the condensed v2 methodology embedded in `init.md`), the
critique/judge refinement loop, the exit conditions, resume, and `summary.md`. The
engine enforces three exits: **convergence** requires score ≥ 8 **and** round ≥ 3
(an early high score does not end the duel before round 3), **stagnation** exits
after no score improvement over 3 consecutive rounds, and a hard cap stops the
duel after 10 rounds. This `SKILL.md` only locates a Python interpreter, resolves
a few paths and the two runtime names, selects the adapter block, and runs the
engine.

Throughout the duel the two plans are labeled **A** and **B** with no attribution:
Agent A is the controller runtime, Agent B is the participant runtime. That mapping
is never revealed to the agents or the judge — only the final summary resolves A/B
to the concrete runtime names. The engine reads its prompts from `init.md` (round 0)
and `round.md` (critique rounds) and emits `summary.md` in the `summary.md` format.

This workflow generates and refines competing plans from a problem statement. It
does not directly compare two already-written `plan.md` files as separate inputs.

---

## Prerequisites

- **Python 3.10+.** Locate an interpreter, trying in order: on Windows the launcher
  `py -3` (latest installed 3.x) and then `python3`; elsewhere `python3` alone. A candidate
  counts only if it *prints a version* — resolving on `PATH` is not enough. If none works,
  report `Python 3.10+ required` and stop. The engine re-checks the
  version at startup and exits with the same `Python 3.10+ required` message if the
  interpreter it was launched with is older than 3.10.
  **`py -3` is probed first on Windows** because `python3` resolves there even when no
  Python is installed: it is a Microsoft Store alias that opens a download page instead of
  running anything. Probed first, it "succeeds", and the engine then never launches.
  **Bare `python` is deliberately not in that list.** Where it is Python 2, `plan_duel.py`
  dies *parsing* `from __future__ import annotations` — before any line of it runs — so the
  engine's own version guard never fires and the user gets a raw `SyntaxError` instead of
  the message above. A probe that can only produce the wrong error is worse than one fewer
  candidate.
- **Every role's CLI on `PATH`.** Before it creates a workdir or spends a single plan
  run, the engine resolves the CLI for all three roles (via `shutil.which`) and halts
  naming any that are missing and the roles needing them. A missing CLI therefore costs
  nothing rather than a wasted Plan A, and no manual pre-flight step is needed. On a
  resume the check runs before any cleanup, so a missing CLI never destroys existing
  artifacts; replaying a finished duel's `summary.md` needs no CLI at all.

---

## How to run

1. **Resolve paths.** Set `<skill_dir>` to the absolute path of the directory holding
   this `SKILL.md`. If the runtime does not expose the loaded skill's path directly,
   locate the installed `plan-duel/SKILL.md` under the runtime's user or project
   skill directories and derive `<skill_dir>` from it.
2. **Select the adapter.** Pick the adapter block below for whichever runtime is the
   controller (the runtime executing this skill). It fixes the two runtime names and
   the per-role argv commands.
3. **Materialize the adapter config.** Write the selected adapter JSON block verbatim
   to a file (for example `<adapter_config>` = a `plan-duel-adapter.json` in the
   current directory or a temporary directory). Pass its path with `--adapter-config`.
4. **Run the engine** with the located interpreter as an argv-list call (no shell
   redirection, pipes, or command substitution):

   ```text
   <python> <skill_dir>/plan_duel.py <problem-or-resume-dir> --adapter-config <adapter_config> --skill-dir <skill_dir> --controller-name <controller_name> --participant-name <participant_name>
   ```

   - New run: the positional argument is the problem statement (inline text or a file
     path). Omit `--workdir` to auto-create `plans/duels/<slug>/`, or add
     `--workdir <path>` to choose the directory — an empty or new one; a non-empty
     directory is refused.
     **Pick a scratch directory, not a tree you are relying on.** Only one of the two
     runtimes is confined to the workdir by the operating system; the other's grant bounds
     *approval*, not the filesystem, so its writing agent can reach outside (the mechanics
     are under "What that grant bounds"). The auto-created default lands **inside your
     working tree**, which is exactly the tree that matters — so on a repository you care
     about, name a `--workdir` somewhere disposable rather than taking the default.
   - Resume: pass an existing duel workdir (one containing `problem.md`) as the
     positional argument; the engine detects it and resumes, or prints the existing
     `summary.md` and stops if the duel already finished. A duel interrupted during
     round 0 resumes from whichever plan already validated — Plan A is snapshotted the
     moment it passes, so a round 0 that failed at Plan B re-runs Plan B alone instead
     of paying for Plan A twice.
   - `--timeout <seconds>` bounds **each** agent and judge spawn (default 1800), so a
     wedged CLI cannot hold the duel open forever. A spawn that outlives it is killed
     and halts the duel; the bound cannot be switched off, and a value that is not
     finite and positive is rejected. On POSIX the whole process group goes, so a runtime
     the CLI spawned goes with it. On Windows the process **tree** is ended via
     `taskkill /F /T`, which is best effort: a descendant re-parented by a shim that has
     already exited can survive, and the engine stops waiting on it rather than hanging.
   - **On Windows, prefer a participant CLI that is not a `.cmd`/`.bat` shim**, or run the
     duel under WSL or Git-Bash. Windows runs a shim through the shell, which reinterprets
     `%VAR%` and `&` in arguments — and the arguments here are whole prompts.
   - **For live progress, pass an explicit `--workdir <path>`** (so you know where
     `<workdir>/progress.log` will land) and run the engine in the **background** using
     your runtime's background-execution facility. The engine writes `progress.log` as it
     works (step 5). A foreground run is still correct, but it blocks the controller until
     the duel finishes (many minutes), so you can't relay progress mid-run even though the
     engine's stdout is line-buffered.
5. **Relay progress by polling `<workdir>/progress.log` as the engine runs.** It is a
   single, append-only, timestamped activity log: several lines per round marking each step
   (plan generation, each critique, judging) as it starts and finishes, a
   heartbeat line (`still …`) every ~15s while a spawn is still in flight (a fast spawn
   produces none), and a final `duel complete — exit=… score=… → summary.md` terminator. Read (or `tail`) it
   periodically to surface progress to the user. When the engine exits, relay the full
   `summary.md`, which it also prints to stdout at the end (the workdir path, per-round
   status lines, and exit reason are on stdout too; a resume of a finished duel prints the
   existing `summary.md` and stops).
   **Gitignore `progress.log`** so the throwaway log never lands in a commit. Nothing on the
   correctness path reads it — it is an activity trace, and the duel's result is
   `summary.md` and the plans beside it — so a run completes identically whether or not
   anyone watches. This matters more here than for a per-phase log, because the default
   workdir is inside the working tree.

---

## Adapters

Each adapter is a structured JSON object with exactly three roles — `agent_a`
(controller), `agent_b` (participant), and `judge` (controller's strongest model).
Every role gives an argv `command` (with `⟪prompt⟫` / `⟪workdir⟫` / `⟪round⟫` markers
the engine substitutes), a `stdout` capture mode (`file` = the CLI writes its
artifact directly; `clean-last-message` = the engine captures only the CLI's final
message), an optional `cwd` anchor, and the `placeholders` the command uses.

**No role pins a model** — that choice is the runtime's, and a pinned label goes stale.

**Every role states its file permission explicitly — never inherit the runtime's default.**
`agent_a` and `agent_b` are contractually required to write a plan file, so each command
grants write access. The `judge` is contractually required *not* to write (its prompt says
so), so each adapter pins it to that runtime's enforced read-only mode rather than trusting
a default to withhold the tools.

**What that grant bounds, and what it does not.** The two runtimes reach it by different
mechanisms, and only one is a filesystem bound:

- A **sandbox** (`-s workspace-write` anchored by `-C ⟪workdir⟫`) confines the process: a
  write outside the workdir fails at the OS level whatever the model attempts.
- A **permission mode** (`--permission-mode acceptEdits` with `--add-dir ⟪workdir⟫`) is
  not a sandbox. `--add-dir` *adds* the workdir to the set the tools may reach rather than
  restricting them to it, and `--allowedTools` names the tools that skip the approval
  prompt rather than the only tools available — so a granted shell still reaches outside.
  It bounds *approval*, not the filesystem.

So the sandboxed side is confined and the permission-mode side is only aimed. Run a duel
against a scratch workdir, not a tree you are relying on.

**A sandbox mode alone does not cover every write path.** The sandbox governs the
model's *shell commands* — under `-s read-only` a shell redirect fails with
`Read-only file system` (verified). But a runtime's built-in patch/edit tool is not a
shell command, so it is gated by the **approval policy** instead: with approvals left at
their default a `-s read-only` spawn still wrote a file, and the same spawn wrote nothing
once `approval_policy=never` was pinned (both verified). So a sandboxed command states
both — the sandbox bounds the shell, the approval policy bounds the edit tool. Together
they are what confine that runtime's writing agent to the workdir.

A default-inherited permission is what breaks first, which is why the writing roles never
inherit one: the default depends on whether the user has marked that directory trusted,
so an unflagged command is read-only on one machine and writable on the next. The failure
is silent — the CLI exits 0 having written nothing, so the run dies after the expensive
work rather than before it.

**The judge's verdict is a JSON object with an enforced schema.** The judge is the one
role whose output the engine *parses*, so its shape is pinned by the runtime's
structured-output flag rather than by asking the model to follow a text format. One
schema file — `judge-schema.json`, shipped beside this `SKILL.md` — is the single
source, and the engine exposes it as two argv placeholders because the runtimes take a
schema differently:

| Placeholder | Substituted with | Used by a CLI whose flag takes |
|---|---|---|
| `⟪schema_path⟫` | the absolute path of `judge-schema.json` | a **file** (e.g. `--output-schema <FILE>`) |
| `⟪schema_json⟫` | that same document as compact inline JSON | the schema **inline** (e.g. `--json-schema <schema>`) |

Both come from the one file, so the schema is never duplicated per runtime — and the
**prompt stays byte-identical across runtimes**, with the difference confined to the
adapter argv where it belongs. Neither capture mode changes: with the schema flag alone
each CLI emits the bare object exactly where the engine already reads it (the
last-message file, or redirected stdout). The engine parses JSON first and falls back to
the pre-schema `SCORE:` / `DIFFERENCES:` / `MISSED REJECTIONS:` / `PREFERRED:` line
markers, so a workdir written before the schema landed still resumes, and a runtime with
no schema flag at all still works (its judge answers in JSON because the prompt asks it
to — just unenforced). An adapter that references either placeholder while the schema
companion is missing or malformed halts up front, before any plan is generated.

### Claude adapter (Claude is the controller)

`--controller-name Claude`, `--participant-name Codex`.

```json
{
  "agent_a": {
    "command": ["claude", "-p", "⟪prompt⟫", "--permission-mode", "acceptEdits", "--allowedTools", "Bash Write", "--add-dir", "⟪workdir⟫"],
    "stdout": "file",
    "placeholders": ["prompt", "workdir"]
  },
  "agent_b": {
    "command": ["codex", "exec", "--skip-git-repo-check", "-s", "workspace-write", "-c", "approval_policy=never", "-C", "⟪workdir⟫", "⟪prompt⟫"],
    "stdout": "file",
    "cwd": "workdir",
    "placeholders": ["prompt", "workdir"]
  },
  "judge": {
    "command": ["claude", "-p", "⟪prompt⟫", "--permission-mode", "plan", "--add-dir", "⟪workdir⟫", "--json-schema", "⟪schema_json⟫"],
    "stdout": "clean-last-message",
    "placeholders": ["prompt", "workdir", "schema_json"]
  }
}
```

### Codex adapter (Codex is the controller)

`--controller-name Codex`, `--participant-name Claude`.

```json
{
  "agent_a": {
    "command": ["codex", "exec", "--skip-git-repo-check", "-s", "workspace-write", "-c", "approval_policy=never", "-C", "⟪workdir⟫", "⟪prompt⟫"],
    "stdout": "file",
    "cwd": "workdir",
    "placeholders": ["prompt", "workdir"]
  },
  "agent_b": {
    "command": ["claude", "-p", "⟪prompt⟫", "--permission-mode", "acceptEdits", "--allowedTools", "Bash Write", "--add-dir", "⟪workdir⟫"],
    "stdout": "file",
    "placeholders": ["prompt", "workdir"]
  },
  "judge": {
    "command": ["codex", "exec", "--skip-git-repo-check", "-s", "read-only", "-c", "approval_policy=never", "-C", "⟪workdir⟫", "--output-schema", "⟪schema_path⟫", "--output-last-message", "⟪workdir⟫/judge-round-⟪round⟫.md", "⟪prompt⟫"],
    "stdout": "file",
    "cwd": "workdir",
    "placeholders": ["prompt", "workdir", "round", "schema_path"]
  }
}
```

> **Adapter note — why the judge stays read-only while still producing a file.** The
> sandbox governs the *model's* shell commands; `--output-last-message` is written by the
> CLI process itself, so the judge can be denied write access and still land
> `judge-round-⟪round⟫.md` (verified end-to-end). That is the intended pairing, not an
> oversight: it enforces the judge prompt's "do not create, write, or edit any file"
> instruction at the process level.
> The Claude-adapter judge reaches the same posture by a different flag: `--permission-mode
> plan` is that CLI's read-only mode, refusing the edit tools outright rather than declining
> to grant them. Both halves verified — a spawn under it told to create a file creates
> nothing, and a judge spawn still returns its schema-conforming verdict, because that reply
> is the CLI's own final message, not a tool call.
> If a future CLI version ever routes its last-message write through the sandbox, the judge
> would fail with no output — the fix is to widen that one role to `workspace-write`, since
> the file it writes is inside the workdir.

### Other runtimes

Set `--controller-name` / `--participant-name` to the two runtime names and supply an
adapter block of the same shape — each role carrying the explicit permission its
contract needs (write for the two agents, withheld for the judge), anchored to the
workdir by whatever confinement that CLI offers: an `agent_a` that writes
`⟪workdir⟫/plan-a.md`, an
`agent_b` that writes `⟪workdir⟫/plan-b.md`, and a `judge` whose clean final message
lands in `⟪workdir⟫/judge-round-⟪round⟫.md` (either `stdout: "file"` via the CLI's
own last-message flag, or `stdout: "clean-last-message"` to let the engine capture
the CLI's final message). Each `command` passes the rendered prompt as `⟪prompt⟫`.

For the judge, add whichever structured-output form that CLI accepts — `⟪schema_path⟫`
for a flag taking a file, `⟪schema_json⟫` for one taking the schema inline — and declare
it in `placeholders`. A CLI offering neither still works: omit both markers and the judge
answers in JSON because the prompt asks for it, just without enforcement.
