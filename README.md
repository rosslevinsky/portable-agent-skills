# Portable Agent Skills

[![Validate](https://github.com/rosslevinsky/portable-agent-skills/actions/workflows/validate.yml/badge.svg)](https://github.com/rosslevinsky/portable-agent-skills/actions/workflows/validate.yml)
[![License: Apache 2.0](https://img.shields.io/badge/License-Apache%202.0-blue.svg)](LICENSE)

A collection of portable, cross-runtime agent skills for [Claude Code](https://docs.anthropic.com/en/docs/agents-and-tools/claude-code/overview) and [Codex CLI](https://github.com/openai/codex). Each skill is a standalone workflow document (`SKILL.md`) that both runtimes can execute with equivalent outcomes.

## What is this?

AI coding agents benefit from reusable, well-shaped workflows — "write a failing test first, then implement," "audit this codebase for security issues," "break a large task into committable phases." This repository packages those workflows as plain-markdown `SKILL.md` files that both **Claude Code** (Anthropic) and **Codex CLI** (OpenAI) can invoke, with equivalent behavior enforced by an automated portability contract.

One install command drops 17 skills into the right places for both runtimes. CI enforces a contract that forbids runtime-specific tool names, requires a fallback wherever a skill leans on a companion skill, and flags private paths before they ship.

## How to use these skills

The skills in this pack compose into a simple end-to-end workflow. If you only read one section of this README, read this one.

**How you invoke one.** You type these to the agent, in the same box you type anything else
— never at a shell prompt. `/name` is Claude Code's shorthand, and this README uses it
throughout. Neither runtime loads a skill from a command table; both decide from the skill's
own description, so naming it in an ordinary sentence works in either: "run the cyw skill",
"security review this codebase", "verify the UI". If a skill you wanted does not trigger, say
its name. None of this works until the skills are installed — see
[Installation](#installation).

### `/cyw` — check your work, any time, anywhere

`/cyw` runs a structured critical-review → fix → verify loop over whatever you just did. It is the single most useful skill in this pack. Use it after *any* non-trivial change — a bug fix, a refactor, a plan, a migration script, a commit message. It does not require a plan or phase structure; it just reviews the recent turn.

**Run it more than once.** The skill already loops internally (up to 3 passes, stopping early once a *second or later* pass finds zero issues — a clean first pass still triggers a confirming review), but a *fresh* `/cyw` invocation — started as a separate call, not another pass inside the same one — starts from a clean context rather than one already shaped by the first review, and tends to surface different things. This is an observation from using it, not a measured result. Each fresh pass costs another round of model time, so spend the second and third on changes where being wrong would be expensive.

### `/clarify` — understand anything, grounded and honest

`/clarify` explains something you don't understand — a concept, a term, code, a doc, an error, or an explanation that just didn't land. **Type it bare and it explains the last response**, with no round-trip asking which part you meant. Point it at something instead and it finds where that thing actually lives — this conversation, pasted text, a doc, code, or a link — reads that source, and explains it in plain, jargon-free English: what it is and why it matters first, any unavoidable term defined in the same sentence, the easy-to-miss part called out, everything else pruned. Its rule is *grounded or honest* — it never invents an explanation of something it can't actually read or verify; when it can't ground the referent, it tells you what's missing and asks. Works with or without a repository.

### The planning cycle

For work bigger than a one-shot edit, the intended flow is:

```
/plan-init  →  /plan-phase  →  /plan-run
```

- **`/plan-init <task>`** — interviews you, explores the codebase, and writes `plans/<slug>/plan.md`: goal, checkable success criteria, constraints, non-goals, affected files. It stamps the plan `Format: v2`, registers it in the `plans/README.md` index, and adds a visual-verification success criterion when UI is in scope. It does *not* break the work into phases.
- **`/plan-phase <path>`** — reads the plan and proposes an ordered **phase list** for your approval. Where phases are genuinely independent it says so in prose and names the phase that reconciles them; everything else is simply sequential, which is the common case. It then writes one phase document per phase plus the `execution.md` tracker — a checkbox list, one box per phase. A phase document is Goal, Work, Tests, Verification, two gate boxes and a compact **evidence record** — under 350 words of structure — and each phase is independently committable.
- **`/plan-run <path>`** — executes phases in order, resuming from the **first unticked
  box** and re-reading that phase's document before running it (so a crash at any point is
  recoverable: the phase document's own checkboxes are the state, and the tracker is a
  derived index). Mid-phase test runs stay filtered to the change, and each phase makes
  **at most** one commit + push — none at all when it stages nothing, and one more only for
  a CI remediation, which goes through the gate like any other change. That gate runs scoped
  tests plus **`/web-verify`** (screenshot-first UI verification) where the phase has UI, and
  a single-pass **`/cyw`** author review plus **`/diff-review`** (independent, diff-first
  review — cross-runtime when a second runtime is available) wherever the phase produced a
  reviewable diff; a phase that only touched plan metadata skips both and records why. It
  fills each evidence record, and assembles an `as-built.md` drift report for a non-trivial
  plan. Safe to restart — already-completed phases are skipped.

Insert `/cyw` freely between steps. Common spots: after `/plan-init` (sanity-check the plan before breaking it down), after `/plan-phase` (sanity-check the breakdown before executing), and after `/plan-run` finishes (final sweep).

Two of the skills the gate calls also stand on their own — **`/web-verify`** and
**`/diff-review`** — and **`/demo-video`** takes over once the feature is finished. All three
are described below.

### Generations: the current suite and `-v1`

The three planning skills above are the **current** suite. The generation they replaced
ships alongside them as **`/plan-init-v1`**, **`/plan-phase-v1`** and **`/plan-run-v1`**, on
open-ended **bugfix-only** support — no removal date, and no new features backported.

**Start new work with `/plan-init`.** Use a `-v1` skill only to finish a plan already in
flight under it.

**What the current suite changed:**

| | `-v1` | current |
|---|---|---|
| Plan marker | none | `plan.md` carries a `Format: v2` row and a `Suite` row |
| Status rows in the plan | `Phase` / `State` / `Blocker` / `Last updated`, written once and then left — live status is in `phases.md` | none; nothing edits `plan.md` after it is written, and drift is recorded separately |
| Execution tracker | `phases.md` | `execution.md`, one checkbox per phase linking that phase's document |
| Phase document | Goal / Entry Criteria / Tasks / Tests / Verification / Exit Criteria / Commit | Goal / Work / Tests / Verification / Gate / Evidence — Work, Tests and Gate are checkboxes; Verification is the commands to run, Evidence a short record filled in afterwards |
| Who reviews a phase | the agent reviews its own work, `cyw` looping until a pass finds zero issues | the same self-review cut to a single `cyw` pass — deliberately lighter — **and** `diff-review`, a second reviewer that judges the diff on the code alone |
| Test cadence | the original, heavier gate | filtered tests while iterating, the phase's scoped tests once, the full suite only at a reconciling or final phase |
| Commits | one per phase, skipped when the phase stages nothing | the same, plus one extra permitted for a CI fix, which goes through the gate like any other change |
| UI work | no special handling | `plan-init` adds a visual-verification success criterion, and a UI phase gets a spec item plus a visual-verification gate box that runs `web-verify` |
| Where a phase runs | in the one conversation | may be handed to a fresh worker per phase, so context does not pile up across phases |
| At the end of the run | — | `plan-run` writes an `as-built.md` recording where the work departed from the plan — for a non-trivial plan only (three or more phases, or any plan with independent phases) |

The row that matters most is the review one. Under `-v1` a phase is committed on the
strength of the agent checking its own work. The current suite adds a second reviewer, and
how independent that reviewer is depends on the host: another runtime is best, a fresh
sub-agent in the same runtime is next, and where neither can be spawned it falls to the same
context re-reading the diff from scratch with the rationale set aside. Only the last of those
has seen the reasoning behind the code. Coverage is the same either way; the strength of the
second opinion is not.

**The two suites cannot collide,** because each acts only on its own tracker: the current
suite acts only on a plan whose `plan.md` carries the `Format: v2` marker, and reads and
writes `execution.md`; the `-v1` suite only ever touches `phases.md`, and refuses a
`Format: v2` plan by pointing you at the current suite. Both trackers are checkbox lists —
the **filename** is what separates them, and it is checked by name rather than inferred
from a file's contents.

### `/plan-duel <task>` — two models write the plan

Swap it in for the plan-writing step when you have both Claude and Codex available. It runs from **either** runtime as the controller, with the other as the participant: each writes a plan following the condensed v2 methodology embedded in the skill (mirroring `/plan-init`'s content model), then they iteratively critique and refine against each other. Three exits: **convergence** (judge score ≥ 8/10, from round 3 onward), **stagnation** (no score improvement over 3 consecutive rounds), or the **10-round cap**. It produces a winning plan stamped `Format: v2` — feed it to `/plan-phase`. The duel is driven by a bundled, stdlib-only Python engine (`plan_duel.py`), so it has two prerequisites: a **Python 3.10+** interpreter, and **both** runtimes' CLIs on `PATH` — the three roles span the controller's own CLI (Agent A and the judge) as well as the participant's, and the engine resolves all three up front and halts naming any that are missing. **Budget for it before you start:** a round is three model calls (two plans, one judge), so a duel that runs to the cap is around thirty, each reading and writing a full plan document. That is minutes of wall clock and real spend on a metered plan. Use it on work where the plan itself is the risk, not on routine changes.

### Cross-model adversarial review (install both Claude and Codex)

The independent-review step is stronger when you have **both the `claude` and `codex` CLIs
installed**. `/diff-review` — and the `/plan-run` gate that runs it on every phase with a
reviewable diff — hands the
review to the runtime that *didn't* write the code: **Codex reviews Claude's work, or Claude
reviews Codex's.** A different model has different training and different blind spots, so it flags
bugs, wrong assumptions, and missed edge cases the authoring model is systematically unlikely to
catch on its own. It is an **adversarial** second opinion, not a restatement of the author's own view.

This is the review-time counterpart to `/plan-duel` at plan time. Install both runtimes and you get
a second, differently-minded model at the two highest-leverage moments — **designing the plan**
(`/plan-duel`) and **reviewing the code** (`/diff-review`).

It degrades in steps, each of which still works, and runs in either direction (Claude-driven or Codex-driven):

- **Both runtimes present** → the review runs cross-model, in the *other* runtime, via a small
  bundled Python 3 supervisor that streams the reviewer's output live and bounds it (idle/heartbeat
  timeout + deadline) so a hung reviewer never blocks you.
- **Only one runtime (or no Python 3)** → it falls back to a fresh **same-model** reviewer — still
  independent of the authoring conversation, just not a different model.
- **No independent reviewer can be spawned at all** → the review still happens, in the authoring
  context, by deliberately setting the rationale aside and re-reading the diff as an outsider. It
  is reported as in-context, because that reviewer has seen the reasoning. Coverage holds all the
  way down; only the strength of the second opinion varies with what the host offers.

Either way, only **blocker/major** findings gate a commit; style nits are recorded as non-blocking
follow-ups. Turn the cross-model rung off for a run with `/plan-run --no-cross-review`.

**Five skills here could all be called "a review", and they are not interchangeable.**
[`REVIEWS.md`](REVIEWS.md) sets them side by side: who reads the work, how much that reader
knows, whether a second model is involved at all, and — the part most people never ask —
who gets to challenge a finding once somebody raises it. Read it if you are choosing between
them, or want to know what a given review is actually worth.

### `/review-panel` — many readers who cannot see each other, and nobody checks their own work

The heaviest correctness review in the pack, and the one to use when being wrong would be
expensive. You name a set of files and state what you are worried about. Everything else
follows from two rules that no other skill here applies.

**Nobody reads alone, and no reader sees another.** The files are split into areas, and every
area is read by **two agents at once**, each given a different angle to read for and neither
able to see the other's work or even the other's instructions. Two independent readings of the
same code find different things; two readings that can see each other converge on the same
things.

**Nobody checks their own finding.** Every finding goes to an agent that did **not** raise it,
which settles the claim by *running* it in a separate copy of the tree rather than by arguing
about it, and reports the command, the exit status and the output. A claim nothing can run is
judged by reading the code instead, and says which of the two it was; a run that only searched
the source is labeled as that rather than as a run of your code. With both runtimes installed,
that challenger is the other model.

One round asks a different question — **which shapes of input none of your tests construct** —
and what it finds is reported as a test to write rather than as a defect, because a missing
test is not a failure and a report that files it as one buries it among the dismissals.

Those two rules give you something nothing else in the pack offers: **it proves it read
everything you gave it.** Every file is accounted for, and the run fails and names the path if anything
was left unassigned. Every other review here is bounded by something — a diff, a set of
components chosen by judgment — and none of them can tell you what they missed.

It reports and never edits your tree. It needs Python 3.10+ for its bundled engine, and it is
not a cheap run: two readers per area, plus a check for every finding. Use it before publishing
something, not at every commit.

It does not replace the two reviews above it. `/diff-review` is anchored to a change set and
`/security-review-codebase` sweeps a whole tree for vulnerabilities; this one reads whatever
you point it at, for whatever problem you state.

### The rest of the pack

Every skill here can be invoked on its own, whether or not you use the planning cycle. Six of
them have not been described yet:

- **`/tdd <feature>`** — red/green/refactor, enforced. It writes the failing tests first and
  **confirms they actually fail** before writing any implementation, then implements the
  minimum that passes, then cleans up. Use it where getting the behavior right matters more
  than getting it quickly.
- **`/commit`** — stages the paths your change actually touched (named, never a `git add -A`
  sweep of the tree), reviews that diff, and writes the message from what it read. It stops at
  the commit unless you asked to publish: "commit and push" or "push my changes" push, a bare
  `/commit` does not. Give it paths to stage something narrower.
- **`/security-review-codebase`** — audits the whole checked-in codebase for concrete,
  exploitable vulnerabilities, not general code smells. Single-pass by default; ask for a
  *deep* or *thorough* review and it splits the codebase into components, reviews each
  separately, then follows data across the boundaries between them. Neither mode writes
  anything into the repository it is auditing: the single-pass review writes no files at all
  and hands you its report directly, and deep mode's working files go to a directory under
  your OS temp path, checked to be outside the audited tree, whose absolute path it prints.
- **`/web-verify`** — proves a web UI renders and behaves by looking at it, because a passing
  unit test does not mean the page shows what it should. It finds the Playwright setup the
  repo already has, drives the app through the flow under review, captures a screenshot at
  each state (and frames from a video where ffmpeg is present), then inspects the images
  against assertions **anchored** to content-bearing elements — a heading with the right
  text, a table with rows — never "the page returned 200" or "a container exists", which
  pass while the page is blank. A saved screenshot is evidence to inspect, never a
  conclusion: nothing counts as verified until the expected content is confirmed in an
  actual image. It never installs Playwright; without one it hands you a manual click-through
  checklist and says the verification ran in degraded mode. `/plan-run` runs it on every
  phase that has UI.
- **`/demo-video`** — records a guided-tour walkthrough of a finished feature, driven slowly
  with pauses, with timed subtitles derived from `as-built.md`. Two prerequisites degrade it
  differently. Without **ffmpeg** you still get Playwright's own video with a subtitle file
  beside it; only muxed-in captions, music and frame extraction are lost. Without
  **Playwright** there is no driver to capture anything with, so you get a narration script
  and a storyboard you assemble from stills of your own — not screenshots it took for you. It
  writes subtitles, not speech; narration audio is out of scope.
- **`/extract-hooks`** — audits `.tsx` files, finds the logic that is not layout, and moves it
  into `use*.ts` custom hooks without changing behavior. React and TypeScript only.

## Skill Inventory

One row per skill: what it does, when to use it, and how it behaves across the two
runtimes.

| Skill | What it does, and when to use it | Classification |
|---|---|---|
| `cyw` | Multi-pass "check your work" review loop. After any change, and again when being wrong would be expensive | Full |
| `clarify` | Explains something that didn't land, grounded in wherever it lives — this conversation, pasted text, docs, code. Typed bare, it explains the last response | Full |
| `plan-init` | Interviews you and writes a `Format: v2` plan registered in `plans/README.md`. The start of any non-trivial feature or refactor | Full |
| `plan-phase` | Breaks a plan into an ordered phase list plus the `execution.md` tracker. After `/plan-init` or `/plan-duel` | Full |
| `plan-run` | Executes the phases in order with a per-phase gate, and writes `as-built.md` for a non-trivial plan. After `/plan-phase` | Full |
| `plan-duel` | Two models write competing plans and refine them against each other; needs Python 3.10+ and **both** CLIs. In place of `/plan-init` when the plan itself is the risk | Degraded |
| `diff-review` | Diff-first review by a second reviewer, as independent as the host allows and cross-model when both CLIs are present; never edits the tree. Before a merge, and inside the `/plan-run` gate | Degraded |
| `review-panel` | Blind multi-agent sweep of a file set: bounded areas, readers who see nothing of each other, every finding checked by one who did not raise it; reports, never edits. Before publishing something. Needs a host that runs two workers at once and refuses one that cannot | Runtime-limited |
| `security-review-codebase` | Whole-codebase audit for exploitable vulnerabilities; single-pass by default, hierarchical deep mode on request | Degraded |
| `tdd` | Red/green/refactor, with the failing test confirmed before any implementation. When getting the behavior right matters more than getting it quickly | Full |
| `commit` | Stages the paths the change touched, never a sweep of the tree, and writes the message from the diff; pushes only when asked | Full |
| `web-verify` | Screenshots a running web UI and inspects the images against anchored assertions. After changing UI; needs an existing Playwright setup | Degraded |
| `demo-video` | Records a guided-tour walkthrough of a finished feature with timed subtitles. When the feature is done and you want to show it | Degraded |
| `extract-hooks` | Moves non-UI logic out of `.tsx` components into custom hooks. React and TypeScript only | Full |
| `plan-init-v1` | Superseded: writes a v1 plan (bugfix-only support) | Full |
| `plan-phase-v1` | Superseded: breaks a v1 plan into phases plus `phases.md` (bugfix-only) | Full |
| `plan-run-v1` | Superseded: executes a `phases.md`-driven plan (bugfix-only). Only to finish a plan already in flight under v1 | Full |

Not sure which of the review skills you want? Read [`REVIEWS.md`](REVIEWS.md).

**Classifications** say what changes between the two runtimes:

- **Full** — the same outcome in both.
- **Degraded** — finishes in both, but where a runtime or the host lacks a capability the
  skill takes a lesser route and says so: a manual checklist instead of screenshots, a
  same-model reviewer instead of a cross-model one.
- **Runtime-limited** — takes no lesser route. Where the host cannot supply what the skill
  needs, it refuses and names what was missing, because a weaker run would not be the thing
  the skill promises. The limitation is declared at the top of the skill file.

## Installation

```bash
git clone https://github.com/rosslevinsky/portable-agent-skills.git
cd portable-agent-skills
python3 install.py
```

One installer, the same command on Linux, macOS and Windows (`py -3 install.py` on
native Windows). It copies every skill to `~/.claude/skills/` (Claude Code) and
`~/.agents/skills/` (Codex CLI's documented user skills directory, shared with several
other agents). To install somewhere else, set `CLAUDE_SKILLS_DIR` and `CODEX_SKILLS_DIR`,
or pass `--target DIR`, repeatable, for one directory instead of the two defaults. To
update, `git pull` and run the same command again: installing and updating are one
operation, and there is no separate `--update`.

**Or install it as an Agent Plugin.** This repository is also a valid [Agent
Plugins](https://agent-plugins.org) 1.0.0 plugin — a `plugin.json` at the root, skills
under `skills/`, which is the layout it already had. Any client of that standard can load
it without going through `install.py` at all. The standard is vendor-neutral, which is why
this pack adopts it and not a single vendor's plugin format: choosing one would pick a side
between the two runtimes these skills exist to serve equally.

```bash
python3 install.py              # install, or update an existing install
python3 install.py --verify     # is the install intact?
python3 install.py --uninstall  # remove only what this installer recorded
python3 install.py --force      # replace a same-name skill it did not install
python3 install.py --target DIR # one directory instead of the two defaults
```

**`install.py` requires Python 3.10+**, and two skills need it whether or not you use the
installer: `plan-duel` refuses to run without it, and `diff-review`'s cross-model rung
needs it too. Needing it at
install time reports that once, clearly, instead of leaving you with a skill that fails days
later. Install as a plugin or by hand and you need no interpreter — every other skill in the
pack is plain Markdown.

Three things worth knowing about how it behaves:

- **It removes skills it installed that this pack no longer ships.** A retired skill would
  otherwise keep loading for ever — the runtimes find skills by looking for directories,
  with no list to consult. Only directories recorded in its own manifest are removed, so
  anything you created is untouched.
- **It will not replace a skill directory it did not install.** If you have your own
  `cyw/`, an update skips it and says so; `--force` overrides.
- **An interrupted install is repaired by running it again.** A skill is replaced in
  place: what is there is removed, then the new one is copied in. So an interruption can
  leave a skill incomplete — `--verify` reports it, and another run replaces it. Ownership
  is recorded *before* anything is copied, which is what makes the repeat an ordinary run
  rather than one that refuses directories the installer itself created.

### Windows

Same command, via the Python launcher:

```powershell
git clone https://github.com/rosslevinsky/portable-agent-skills.git
cd portable-agent-skills
py -3 install.py
```

Skills land in `%USERPROFILE%\.claude\skills` and `%USERPROFILE%\.agents\skills`.
There is no PowerShell script and no execution-policy prompt to work around.

Prefer `py -3` over `python3` on Windows. A machine without Python still has
`python3.exe` on PATH — an App Execution Alias that opens the Microsoft Store instead of
running anything — so a probe for `python3` finds something that is not an interpreter.

If you run Claude Code or Codex **inside WSL**, install from inside WSL too: it writes to
your Linux home, which the native Windows app does not read.

## Manual installation (if the installer fails)

The installer is only a convenience. A skill is just a directory containing a
`SKILL.md` (a few skills carry extra files alongside it), and both runtimes load
a skill simply by finding its directory in the right place. So if `install.py`
won't run — no Python, a locked-down machine — you can install by hand: **copy each
skill directory** from this repo's `skills/` into the runtime's skills directory.

Target directories:

| Runtime | macOS / Linux | Windows |
|---|---|---|
| Claude Code | `~/.claude/skills/` | `%USERPROFILE%\.claude\skills\` |
| Codex CLI | `~/.agents/skills/` | `%USERPROFILE%\.agents\skills\` |

The result you are aiming for is one directory per skill, e.g.
`~/.claude/skills/commit/SKILL.md`, `~/.claude/skills/cyw/SKILL.md`, and so on.

**macOS / Linux** — copy every skill into both runtimes:

```bash
mkdir -p ~/.claude/skills ~/.agents/skills
cp -R skills/* ~/.claude/skills/
cp -R skills/* ~/.agents/skills/
```

**Windows (PowerShell)**:

```powershell
New-Item -ItemType Directory -Force "$HOME\.claude\skills", "$HOME\.agents\skills" | Out-Null
Copy-Item -Recurse -Force skills\* "$HOME\.claude\skills\"
Copy-Item -Recurse -Force skills\* "$HOME\.agents\skills\"
```

**Windows (File Explorer)** — open the repo's `skills` folder, select all the
skill folders, and copy them. Then paste into `%USERPROFILE%\.claude\skills`:
paste that path into the address bar and press Enter (the `.claude` folder
usually already exists, since Claude Code creates it on first run). If the
`skills` subfolder isn't there yet, the easiest way to create the dot-folders
is the PowerShell `New-Item` line above — Explorer is awkward about folder names
that start with a dot. Repeat for `%USERPROFILE%\.agents\skills`.

Notes:

- **Install only some skills** by copying just the directories you want
  (e.g. `cp -R skills/cyw skills/commit ~/.claude/skills/`).
- **Copy whole directories, not just `SKILL.md`** — a couple of skills
  (e.g. `plan-duel`) ship companion files next to it.
- A hand-install skips the ownership manifest (`.installed-by-portable-agent-skills`)
  that `install.py` writes. The skills still work; only `--verify` and the
  installer's safe `--uninstall` rely on it. To uninstall a hand-installed
  skill, just delete its directory from the target.

## Installing a previous release

Every release is tagged `vYYYY.MM.MICRO`, and any tagged release stays installable
indefinitely. **Uninstall before you check out**, so the removal is done by the installer
that owns the manifest — the one that knows what it put there:

```bash
git fetch --tags
git tag --list                     # every release, oldest first
python3 install.py --uninstall     # Windows: py -3 install.py --uninstall
git checkout v2026.06.0            # or whichever you want back
./install.sh                       # THAT release's installer — see below
```

**Uninstall before you check out.** Both commands come from the tree you are standing in,
and the two steps need different trees. The uninstall has to run the installer you are
leaving, because it is the one whose manifest records what is on your machine; after the
checkout that manifest is still there but the installer reading it is an older one. An older
installer does not prune — it installs its own skills, writes a manifest listing only those,
and leaves every skill it has never heard of on disk, unowned, where a later `--uninstall`
skips it and a later run refuses to replace it without `--force`. Uninstalling first gives
you that version's skill set exactly.

**Then run whatever installer that tag shipped**, which is why the last line above is not
`python3 install.py`. `install.py` exists from `v2026.08.0` onward, and the older tags do
not carry it:

| Tag | Installer it ships |
| --- | --- |
| `v2026.08.0` and later | `install.py` |
| `v2026.06.0` | `install.sh`, `install.ps1` |
| `v2026.04.0` | `install.sh` only |

So on `v2026.06.0` the last step is `./install.sh` — under WSL or Git Bash on Windows — or
`.\install.ps1`; on `v2026.04.0` there is no PowerShell installer at all. `ls install.*`
after the checkout answers it without having to trust this table.

**On Windows, `install.sh` from those tags needs one extra step.** They predate this
repository's `.gitattributes`, so a default clone — Git for Windows sets
`core.autocrlf=true` — writes the script with CRLF, and bash refuses it two different ways:
`./install.sh` gives `env: 'bash\r': No such file or directory`, and `bash install.sh` gives
`set: pipefail: invalid option name`. Restore that one file with the line endings it was
written with:

```bash
git -c core.autocrlf=false checkout -- install.sh
```

On `v2026.06.0` you can use `.\install.ps1` and skip this entirely — and if script
execution is blocked, run it once as `powershell -ExecutionPolicy Bypass -File
.\install.ps1`. The block is not about where the file came from: the Windows client
default is `Restricted`, which refuses every `.ps1` whatever its origin, and the
`RemoteSigned` default on Server keys on a mark-of-the-web that a `git clone` never
writes. So a reader on Server will not hit this at all, and one on a client will hit it
however they got the file. On `v2026.04.0` the shell script is the only installer, so the
checkout above is the whole path.

To read an old release without disturbing what you have installed, clone it to a scratch
directory and point the installer at throwaway targets:

```bash
git clone --branch v2026.06.0 --depth 1 \
  https://github.com/rosslevinsky/portable-agent-skills.git /tmp/pas-old
CLAUDE_SKILLS_DIR=/tmp/pas-claude CODEX_SKILLS_DIR=/tmp/pas-codex \
  /tmp/pas-old/install.sh
```

`install.sh`, not `install.py`, for the reason above: that is the installer `v2026.06.0`
ships. Every release honors both `*_SKILLS_DIR` variables, so this works whichever one you
land on — set them both. Left unset, the installer writes to the live install you were
trying not to touch: `~/.claude/skills` either way, plus a second directory that moved
between releases — `$HOME/.agents/skills` from `v2026.08.0` on, and a `.codex` one before
that.

The [changelog](CHANGELOG.md) says what changed in each release.

## Uninstall

```bash
python3 install.py --uninstall   # Windows: py -3 install.py --uninstall
```

Only removes skills installed by this pack, tracked via an ownership manifest. A skill you
created yourself is never in that manifest, so a same-named directory of your own is left
untouched. Note the manifest records the *directory name*, not its contents: if you edited
a skill this pack installed, uninstall still removes it, edits and all.

## Development Setup

**The way to iterate on a skill is to edit it here and reinstall**:

```bash
python3 install.py
```

Installing 17 skills is a copy of about 55,000 words — fast enough to be the inner loop.
The runtimes read the installed copy, not this checkout, so an edit here changes nothing
until it is reinstalled.

### Editing skills from inside an agent session

You are running Claude Code or Codex and ask it to improve one of its own skills. Which
file it edits depends on where the session is. In any other project the only copy it can
see is the installed one — `~/.claude/skills/<name>/SKILL.md` or
`~/.agents/skills/<name>/SKILL.md` — so that is what it edits, and nothing git tracks has
changed. In a session opened inside this checkout it may edit `skills/<name>/SKILL.md` here
instead. Ask it which, or diff both.

The workflow for skill authors who iterate this way:

1. Let the agent edit the skill during a normal session.
2. Diff the installed copy against this repo and port the change over, reading it as you
   go:
   ```bash
   diff -ru ~/.claude/skills/<name> skills/<name>
   ```
3. Move the skill's word budget. `scripts/skill-budgets.json` records each skill's measured
   size; the validator fails a skill that has grown past its number, and the test suite
   holds every number equal to the measurement, so an edit in either direction fails until
   the number is moved. Measure it rather than typing it:
   ```bash
   python3 -c "import sys; sys.path.insert(0, 'scripts'); from pathlib import Path; \
   from validate_cross_runtime import measure_skill_words; \
   print(measure_skill_words(Path('skills/<name>')))"
   ```
   The `-v1` skills carry no budget.
4. Run the gate in this repo:
   ```bash
   python3 scripts/validate_cross_runtime.py skills/
   python3 scripts/validate_cross_runtime.py --test-fixtures tests
   python3 scripts/shard_tests.py --total 4 --parallel
   ```
   The last line runs the same suites `python3 -m unittest discover -s tests -p 'test_*.py'`
   runs, split across your cores; either form is fine.
5. `python3 install.py` to put the reviewed version back on your machine, then start a new
   agent session so that copy is the one in use.
6. Commit, push, open a PR. CI re-runs the validator and the suites on three operating
   systems.

Step 2 is deliberate: an edit an agent made to its own instructions is worth reading
before it becomes the instructions. Step 3 is the one people miss, and a red run there
says nothing about whether the edit is right — the budget is a size ratchet, and it only
says the file grew.

### Checking an install

```bash
python3 install.py --verify
```

It answers one question — *does this install match this pack?* — with three answers: **0**
when it matches, **1** when something is installed and does not, and **2** when there is
nothing to compare (no manifest here, or a `--source` holding no skills). `1` and `2` are
kept apart because "your install is broken" and "you have not installed this" want opposite
responses. Every way of not matching is reported, because this is the command the repair
procedure relies on and a state it cannot see is a state nobody fixes.

| | |
|---|---|
| `MISSING` | listed in the manifest and not on disk |
| `RETIRED` | installed, and this pack no longer ships it |
| `NOT YET` | this pack ships it and it is not installed |
| `DIFFERS` | a shipped file is absent, a different size, a different content, or not in this pack |
| `VERSION` | the files are from a different release than this one |

It compares **paths, then sizes, then bytes** — and in that order, so the cheap answers
settle most of it. A path list catches a skill that is missing or has gained a file. Sizes
catch what an interrupted copy leaves, since the destination name is created before the
bytes are written. Only where the name and size already agree does it read the two files,
which is what catches an edit that happens to preserve a file's length — for a pack of
instruction files that matters, because a skill is text a model obeys.

If you edited a skill in place, `DIFFERS` will report it. That is the answer to the
question asked, not an accusation: your install no longer matches the pack.

Running `python3 install.py` with no flag reconciles everything above.

### Consuming from another repo

If you maintain another repository for machine setup, keep this pack as the single source
of truth and point that setup at this checkout — run `python3 install.py` from it, or
`--target` a directory of your choosing.

## Validation

Run the portability validator and test suite:

```bash
python3 scripts/validate_cross_runtime.py skills/          # Check all skills
python3 scripts/validate_cross_runtime.py --test-fixtures tests  # Run fixture tests
python3 -m unittest discover -s tests -p 'test_*.py'        # Every Python suite
```

Those three commands are the whole gate, and CI runs them on **Ubuntu, macOS and
Windows**, the suites split into four shards per platform, plus a Linux pass under the C
locale, which is the closest stand-in for the Windows console encoding. Windows is where a
path-separator or text-encoding mistake actually surfaces, so it runs the same set rather
than a subset. A **private** fork runs Linux only, because its runner minutes are billed;
it gets all three platforms by starting the workflow by hand with its `all_platforms` input
(`gh workflow run validate.yml --ref <branch> -f all_platforms=true`). Nothing on any
command path is PowerShell, so there is no PowerShell suite.

**One skill does ship PowerShell, and the suite runs it under every PowerShell it finds.**
`security-review-codebase/references/hierarchical-mode.md` carries a Windows PowerShell 5.1
block that picks a report directory outside the audited repository. It is instructions an
agent runs, not a file CI executes, so the validator does not reach it; the unit suite
does, parsing the block and running its path resolver under each PowerShell on the host.
On a Linux runner that is PowerShell 7; on a Windows runner it is PowerShell 7 and Windows
PowerShell 5.1 both, and 5.1 is the one a user's machine runs. With no PowerShell on the
host those cases skip and say so.

There is a fourth command, and it is a tool rather than a gate:

```bash
python3 scripts/check_plan_tracker.py <your-plan-directory>
```

It reads an `execution.md` checkbox tracker — or every one under a directory — and reports
a phase document the tracker never links, which is a phase that would never be executed.
Point it at a single file or at the directory holding your plans. `plan-phase` and
`plan-run` tell a runtime to run it, so it is part of the pack; it is not in the list above
because a clone with no plan directory has nothing for it to read.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for a complete walkthrough (adding a new
skill, template, classification guidance, fixture authoring, PR checklist).

Quick version:

1. Fork and clone the repository
2. Create a feature branch
3. Make your changes in `skills/<skill-name>/SKILL.md`
4. **Adding a skill? Two edits are not discovered for you** — add it to the Skill
   Inventory table and bump the count in this README, and record its word count in
   `scripts/skill-budgets.json`. The validator fails the run without both.
5. Run `python3 scripts/validate_cross_runtime.py skills/` — must pass with zero errors
6. Run `python3 scripts/validate_cross_runtime.py --test-fixtures tests` — all fixtures must pass
7. Run `python3 -m unittest discover -s tests -p 'test_*.py'` — every Python suite must pass
8. Open a pull request — CI runs all checks automatically

### Portability expectations

All skills follow the [PORTABILITY.md](PORTABILITY.md) contract, and the validator enforces
most of it — a PR fails on a branded tool name, a companion reference with no fallback, a
missing classification or progress posture, a skill file reaching outside its own directory,
or a private path.

**Three rules it does not check**: hardcoded model names, an autonomous fallback on every
user-prompting step, and naming both `CLAUDE.md` and `AGENTS.md` for project instruction
files. Those are caught in review, so a green validator run is not proof a skill honors the
contract. `PORTABILITY.md` closes with the full coverage list.

## License

[Apache License 2.0](LICENSE)
