---
name: review-panel
description: >
  Blind multi-agent correctness sweep of a file set. Many readers each take one bounded
  area and see nothing of each other's work; every finding is then checked by an agent
  that did not raise it, and a claim that can be run is run. Reports findings and what
  nobody read; never edits the tree it reads. Distinct from diff-review (anchored to a
  change set), cyw (the author checking itself) and security-review-codebase (one lens,
  no verification step). Use when the user invokes /review-panel, or says "sweep these
  files for defects", "review the codebase with a panel", "blind review", "have several
  agents read this independently". Argument: the problem statement (inline text or a
  file path), or a job file; with neither, the skill interviews for one.
---

# Review Panel — Blind Multi-Agent Correctness Sweep

_Classification: Runtime-limited — this skill needs a host that can run **two workers at
once** and refuses one that cannot; on such a host it works the same under either runtime.
The requirement is the product, not a speed-up: every finding is judged by a unit that did
not raise it, so one worker doing both jobs is not a weaker review but a different and
misleading one, and there is nothing underneath to degrade to. Within that requirement
there are two rungs. Stronger is **two runtimes**: two models read every area blind, and
each finding is verified by the *other* model, so the report may claim model divergence.
Weaker is **one runtime**: two fresh contexts of the same model, and the report says context
divergence and nothing stronger. Coverage is the same at both; only what the report may
claim varies, and the rung follows the configuration, which is pinned for the run. A lane
that lands no unit ends the run with a refusal naming it — not a third rung. The
run is two bundled stdlib-only **Python 3.10+** programs — the stage engine
`review_panel.py` and the driver `review_panel_run.py` that owns the loop — a prerequisite
the skill checks before it dispatches anything. `git` is the other one, wherever the tree
under audit is a repository: the inventory asks git what is tracked, and planning refuses
without it. And the `diff-review` skill, installed beside this one: its `review_runner.py`
supervises every worker. If that skill is unavailable the run stops before dispatching and
names it; there is no equivalent to substitute, so install it beside this one._

_Progress: observable — the driver appends one line to `dispatch/progress.log` in the run
directory for every spawn, every adjudication and every landing, beside whatever each
worker's supervisor streams to its own display log. Nothing on the correctness path reads
either: the engine's stages read `result.json`, `error.txt` and `dispatch.json`, never the
log. Tail it while the run goes, or ask `review_panel_run.py status <rundir>`, which takes
no lock and writes nothing._

## Overview

Take a problem statement and a set of files, divide the files into bounded areas, and have
many agents read them — none seeing another's work — for what is wrong. Then send every
finding to an agent that did not raise it, which reproduces the claim where it can be run
and otherwise judges it by reading. The output is one structure and two renderings of it:
every defect with a location, a plain-language consequence, a severity, what the stranger
decided and the source it cites quoted from the pinned tree, plus every path nobody read.
Which unit raised what is kept to an appendix, out of the material somebody reads while
fixing. **This skill reports; it never edits, stages or commits in the tree it reads.**

**A clean sweep is not evidence a tree is sound, and a long report is not a thorough one** —
yield is not recall, and the report says so itself under `Report description`, where the
person who has to act on it will read it.

**Your part is three steps: the job, one command, the summary.** The driver plans the run,
launches every unit, lands every reply, calls the engine's stages in order and writes the
report; a killed run resumes from the files on disk with no work redone and none lost.
Nothing below asks you to dispatch a unit by hand. `references/dispatch.md` holds the
adapter config in full, the reasoning behind each flag, and what to do when a run stops.

**The interpreter is the prerequisite, and its absence is a message, not a crash.** Before
anything is dispatched, locate a Python 3.10+ interpreter — on Windows the launcher `py -3`
first, then `python3`; elsewhere `python3` — counting a candidate only if it prints a
version. If none does, report `Python 3.10+ required` and stop, dispatching no reader:
without the engine there is no partition to read against, no routing of findings, and no
report to write, so the sweep has nothing to hand a reader. Both programs re-check the
version at startup and exit with the same `Python 3.10+ required` message.

## The four rules

1. **Bounded area.** One agent gets one chunk it can read closely, not skim.
2. **Blind.** A payload file contains no other unit's output. It is a property of the
   file, so a test asserts it rather than a prompt requesting it.
3. **Verified by a stranger.** A finding never goes back to its finder — and at the
   two-runtime rung, it goes to the **other model**.
4. **Run it if it can be run.** `references/verifier.md` holds the rule in full — what
   counts as evidence, when a verdict must be `unresolved`, and why a search of the source
   is `documentary` rather than `executed` — and it is the file the verifier is handed.

No worker is handed the source path, and none is handed a path that spells whose claim it
holds: planning copies the files under review, and the lockfiles, to a snapshot under the
run directory, and each attempt gets an opaque token naming its input directory and its
working directory both.

**Most files under `references/` are those briefs, not instructions to you.** The engine
loads one per unit kind at plan time and copies it into that unit's payload — which is what
rule 4 means by `verifier.md` being the file the verifier is handed. You never dispatch one
yourself; open one to see what a worker was actually asked. `job.md`, `dispatch.md` and
`report-format.md` are the three written for whoever runs the skill.
`references/architecture.md` is for none of them — it is a guide to the two programs for
somebody changing them, and no step of a run sends you there.

## Steps

1. **The job.** The argument is the problem statement — inline text, or a path to a file
   holding it — or a path to an existing job file, loaded as it stands: a file whose content
   is a JSON object is a job, any other file is the statement. Text becomes a job by the
   interview in `references/job.md`, which holds that interview, the defaults for a run
   with no user to answer, the restatement the owner confirms, and the `job.json` format
   you write the job in.
   **Two refusals are yours to get right**, because both are about where files sit rather
   than about what the job says. `root` is **absolute**: the job file is copied into the run
   directory and the tree does not move with it, so a relative root is refused, as is a root
   inside a git directory. And the run directory lies outside `root` and outside **every**
   git repository. Write `job.json` beside your own work and name the run directory on the
   command line; the driver creates it, and a directory already sitting there that holds no
   committed run is refused by name rather than deleted.

2. **Run it.** Write the adapter config first — one JSON file naming, per lane, the
   runtime, the model, how many workers run at once (`slots`, two unless the owner picks
   another number when you ask), and the command line to launch a worker with in each of the two permission
   modes. `references/dispatch.md` gives its shape and a worked example. Lanes A
   and B are what makes rule 3 true, so give them **two runtimes** where the host has two
   and one runtime with **one model** where it has one; two lanes on one runtime naming
   different models is refused before anything is planned, because no sentence the report
   can write about it is true. Then:

   ```
   <python> <this skill's dir>/review_panel_run.py run --job <job.json> \
       --rundir <dir> --adapter <adapter.json> --go
   ```

   Without `--go` it plans, prints the preview and exits 0 having spawned nothing; it never
   reads stdin. The preview counts one **capability probe** beside the readers — the single
   unit per run that establishes whether the tree builds and whether its tests run, which
   every verification payload then carries (`references/probe.md`). With `--go`, the run
   goes to a report. Watch `dispatch/progress.log` as it goes. To stop it, create the drain
   file it names on its first line: claiming stops, what is running finishes, and the run
   exits resumable — on every platform and at any point.

   **A finished run wrote `report.md`, and the driver's last line names it.** Exit 0 alone
   does not say so: a drained run and a `--go`-less preview exit 0 too, having written no
   report. The other two exits are not the same thing either. A **stop** is resumable —
   an attempt whose outcome cannot be proven, a storage fault, a host refusal, a spent time
   budget — so put right what the message names and run the same command again; where it
   names a unit, `resolve-attempt` and `resolve-unit` are the two ways to answer it, in
   `references/dispatch.md`. A **refusal** is not resumable: the snapshot changing under the
   readers is the one to expect, and nothing read against it can be trusted, so that run
   ends there and a new one is planned.

3. **Hand the report to the owner**, as `references/report-format.md` closes. Answers to
   the owner's questions, whether in the job or asked since, corrections to findings you
   can show are wrong, and caveats about the run go in `report-notes.json`, not in the
   conversation; `report --rerender` puts them in the report. Then point the owner at
   `report.md` by path; never inline a snapshot file, an evidence block or a payload.

## What the run claims

Every worker is a fresh process that was supplied no peer's answer, and nothing it is
handed names the unit that raised what it is checking. Workers are **not** prevented from
reading the run directory: blindness is a property of what each one is given, not of what it
could reach. Reproductions are serialized, one at a time across both lanes, because separate
directories are not separate ports, caches or credentials — and a failure that is the
environment's stays `unresolved` rather than becoming a verdict.

The rung the report names follows **the configuration, which is pinned for the life of the
run**: a resume naming a different runtime or model is refused by name, so no lane quietly
becomes a second context of the other's runtime and no rung drops below the one configured.
What a run can do instead is end without one. A lane that lands no unit answered nothing it
was given, so no finding in the run was checked by a unit that did not raise it — rule 3,
which every status on the page rests on. The driver stops at the first round boundary where
that is settled, names the lane, and writes no report. Otherwise the report states the rung,
both lanes' adapters and both permission modes, so what the run was allowed to claim is
readable months later from the report alone.
