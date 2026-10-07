# Portable Agent Skills

[![Validate](https://github.com/rosslevinsky/portable-agent-skills/actions/workflows/validate.yml/badge.svg)](https://github.com/rosslevinsky/portable-agent-skills/actions/workflows/validate.yml)
[![License: Apache 2.0](https://img.shields.io/badge/License-Apache%202.0-blue.svg)](LICENSE)

A collection of portable, cross-runtime agent skills for [Claude Code](https://docs.anthropic.com/en/docs/agents-and-tools/claude-code/overview) and [Codex CLI](https://github.com/openai/codex). Each skill is a standalone workflow document (`SKILL.md`) that both runtimes can execute with equivalent outcomes.

## What is this?

AI coding agents benefit from reusable, well-shaped workflows — "write a failing test first, then implement," "audit this codebase for security issues," "break a large task into committable phases." This repository packages those workflows as plain-markdown `SKILL.md` files that both **Claude Code** (Anthropic) and **Codex CLI** (OpenAI) can invoke, with equivalent behavior enforced by an automated portability contract.

One install command copies 19 skills to where both runtimes look for them. CI enforces a
contract that forbids runtime-specific tool names, requires a fallback wherever a skill depends
on a companion skill, and flags private paths before they ship.

When a skill starts a second agent, that agent can run on a model you choose, including
open-weight models served by OpenRouter or Fireworks. See [Choosing the model a second agent
runs on](#choosing-the-model-a-second-agent-runs-on).

## How to use these skills

The skills in this pack fit together into one simple workflow, from plan to finished feature. If
you only read one section of this README, read this one.

**How you invoke one.** You type these to the agent, in the same box you type anything else
— never at a shell prompt. `/name` is Claude Code's shorthand, and this README uses it
throughout. Neither runtime loads a skill from a command table; both decide from the skill's
own description, so naming it in an ordinary sentence works in either: "run the cyw skill",
"security review this codebase", "verify the UI". If a skill you wanted does not trigger, say
its name. None of this works until the skills are installed — see
[Installation](#installation).

### `/cyw` — check your work, any time, anywhere

`/cyw` runs a structured critical-review → fix → verify loop over whatever you just did. It is the single most useful skill in this pack. Use it after *any* non-trivial change — a bug fix, a refactor, a plan, a migration script, a commit message. It does not require a plan or phase structure; it just reviews the recent turn.

**Run it more than once.** The skill already loops internally: up to 3 passes, stopping early
once a *second or later* pass finds zero issues, so a clean first pass still triggers a
confirming review. A *fresh* `/cyw` invocation, started as a separate call rather than another
pass inside the same one, starts from a clean context instead of one already shaped by the first
review. It tends to find different things. This is an observation from using it, not a measured
result. Each fresh pass costs another round of model time, so spend the second and third on
changes where being wrong would be expensive.

### `/clarify` — understand anything, grounded and honest

`/clarify` explains something you don't understand — a concept, a term, code, a doc, an error,
or an explanation that just didn't land. **Type it bare and it explains the last response**,
without first asking which part you meant. Point it at something instead and it finds where that
thing actually lives (this conversation, pasted text, a doc, code, or a link), reads that
source, and explains it in plain, jargon-free English: what it is and why it matters first, any
unavoidable term defined in the same sentence, the easy-to-miss part called out, everything else
left out. Its rule is *grounded or honest*: it never invents an explanation of something it
can't actually read or verify. When it can't find and read the thing you mean, it tells you
what's missing and asks. Works with or without a repository.

### The planning cycle

For work bigger than a one-shot edit, the intended flow is:

```
/plan-init  →  /plan-phase  →  /plan-run
```

- **`/plan-init <task>`** — interviews you, explores the codebase, and writes `plans/<slug>/plan.md`: goal, checkable success criteria, constraints, non-goals, affected files. It stamps the plan `Format: v2`, registers it in the `plans/README.md` index, and adds a visual-verification success criterion when UI is in scope. It does *not* break the work into phases.
- **`/plan-phase <path>`** — reads the plan and proposes an ordered **phase list** for your approval. Where phases are genuinely independent it says so in prose and names the phase that reconciles them; everything else is simply sequential, which is the common case. It then writes one phase document per phase plus the `execution.md` tracker — a checkbox list, one box per phase. A phase document is Goal, Work, Tests, Verification, two gate boxes and a compact **evidence record** — under 350 words of structure — and each phase is independently committable.
- **`/plan-run <path>`** — executes phases in order, resuming from the **first unticked
  box** and re-reading that phase's document before running it. So it can recover from a
  crash at any point: the phase document's own checkboxes hold the state, and the tracker is
  an index built from them. Mid-phase test runs stay filtered to the change. Each phase makes
  **at most** one commit + push: none at all when it stages nothing, and one more only for
  a CI fix, which goes through the gate like any other change. That gate runs scoped tests,
  plus **`/web-verify`** (screenshot-first UI verification) where the phase has UI. Wherever
  the phase produced a reviewable diff, the gate also runs a single-pass **`/cyw`** author
  review and **`/diff-review`** (independent, diff-first review, by a different model when
  one is available); a phase that only touched plan metadata skips both and records why.
  It fills each evidence record, and assembles an `as-built.md` drift report for a
  non-trivial plan. Safe to restart: already-completed phases are skipped.

Insert `/cyw` freely between steps. Common spots: after `/plan-init` (sanity-check the plan before breaking it down), after `/plan-phase` (sanity-check the breakdown before executing), and after `/plan-run` finishes (final sweep).

`/plan-init` and `/plan-phase` each end by asking whether to run **`/xc`** on what they just
wrote: a different model's review of the plan or the breakdown before any work starts. It
never runs unattended; see [Cross-model adversarial review](#cross-model-adversarial-review-a-second-cli-or-a-backend-on-another-model).

Two of the skills the gate calls also stand on their own — **`/web-verify`** and
**`/diff-review`** — and **`/demo-video`** takes over once the feature is finished. All three
are described below.

### Generations: the current suite and `-v1`

The three planning skills above are the **current** suite. The generation they replaced
ships alongside them as **`/plan-init-v1`**, **`/plan-phase-v1`** and **`/plan-run-v1`**, on
open-ended **bugfix-only** support — no removal date, and no new features backported.

**Start new work with `/plan-init`.** Use a `-v1` skill only to finish a plan already
started under it.

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

The row that matters most is the review one. Under `-v1` a phase is committed once the
agent has checked its own work, and nobody else has. The current suite adds a second
reviewer, and how independent that reviewer is depends on the host. A different model is
best — another runtime, or your own on a backend running another model — and a fresh
sub-agent in the same runtime is next. Where neither can be started, the same
context re-reads the diff from scratch with the rationale set aside. Only the last of those
has seen the reasoning behind the code. Coverage is the same either way; the strength of the
second opinion is not.

**The two suites cannot collide,** because each acts only on its own tracker: the current
suite acts only on a plan whose `plan.md` carries the `Format: v2` marker, and reads and
writes `execution.md`; the `-v1` suite only ever touches `phases.md`, and refuses a
`Format: v2` plan by pointing you at the current suite. Both trackers are checkbox lists —
the **filename** is what separates them, and it is checked by name rather than inferred
from a file's contents.

### `/plan-duel <task>` — two models write the plan

Use it in place of the plan-writing step when you have both Claude and Codex available. You can
start it from **either** runtime. The one you start it from is the controller, and the other
takes part as the participant. Each writes a plan following the condensed v2 methodology
embedded in the skill (mirroring `/plan-init`'s content model). Then, round by round, each
critiques the other's plan and revises its own. Three exits: **convergence** (judge score ≥
8/10, from round 3 onward), **stagnation** (no score improvement over 3 consecutive rounds), or
the **10-round cap**. It produces a winning plan stamped `Format: v2`; feed it to `/plan-phase`.
A bundled Python engine that uses only the standard library (`plan_duel.py`) runs the duel, so
it has three prerequisites: a **Python 3.10+** interpreter; the **`diff-review`** skill
installed beside it, because its bundled launcher starts every role; and, with the settings it
ships, **both** runtimes' CLIs on `PATH`. That last one is because the three roles use both
CLIs: the controller's own CLI runs Agent A and the judge, and the participant's CLI runs Agent
B. The engine looks up the CLI for all three roles before it starts, and halts naming any that
are missing. **Budget for it before you start:** a round is three model calls (two plans, one
judge), so a duel that runs to the cap makes around thirty, each reading and writing a full plan
document. That takes minutes and costs real money on a metered plan. Use it on work where the
plan itself is the risk, not on routine changes.

### Cross-model adversarial review (a second CLI, or a backend on another model)

The independent-review step is stronger when the reviewer is a **different model**: install
**both the `claude` and `codex` CLIs**, or point your own CLI at another model through a
backend. `/diff-review` — and the `/plan-run` gate that runs it on every phase with a
reviewable diff — hands the review to a model that *didn't* write the code: **Codex reviews
Claude's work, Claude reviews Codex's, or your own CLI reviews on the backend's model.** A
different model has different training and different blind spots, so it flags bugs, wrong
assumptions, and missed edge cases the authoring model is systematically unlikely to catch on
its own. It is an **adversarial** second opinion, not a restatement of the author's own view.

This does at review time what `/plan-duel` does at plan time. With a second model available
you get it at the moments where it matters most: **designing the plan** (`/plan-duel`),
**checking a plan or any document you already wrote** (`/xc`), and **reviewing the code**
(`/diff-review`).

**`/xc` is the same second opinion for a document.** It runs a `cyw` pass, has the other model
review the document, and decides each finding on its merits. A finding it rejects goes back to
the reviewer once, with the reason; one still disputed after that comes to you and is not
applied. It applies what survives and runs the full `cyw` loop again. It reaches the other
model the way `/diff-review` does. With no different model available it falls back to a fresh
reviewer running your own model, which has not seen the conversation; with no separate
reviewer at all it stops and says so rather than passing off its own review as a second
opinion.

When something is missing, `/diff-review` falls back in steps, and each step still works. It
runs in either direction (Claude-driven or Codex-driven):

- **Both runtimes present** → the review runs cross-model, in the *other* runtime, through a
  small bundled Python 3 program, the supervisor. It shows the reviewer's output as it arrives
  and stops the reviewer at a time limit (a timeout when no output arrives for too long, and an
  overall deadline), so a reviewer that hangs never blocks you.
- **One runtime plus a backend on another model** → the same review runs on your own CLI,
  on the backend's model (see
  [Choosing the model a second agent runs on](#choosing-the-model-a-second-agent-runs-on)).
  The report names both models and says their independence is unverified, because model
  names are compared as text and two spellings of one model would pass as different.
- **Only one runtime and no such backend (or no Python 3)** → it falls back to a fresh
  **same-model** reviewer — still independent of the authoring conversation, just not a
  different model.
- **No independent reviewer can be spawned at all** → the review still happens, in the authoring
  context, by deliberately setting the rationale aside and re-reading the diff as an outsider.
  It is reported as in-context, because that reviewer has seen the reasoning. At every step the
  reviewer reads the same diff; only the strength of the second opinion varies with what the
  host offers.

Either way, only **blocker/major** findings block a commit; style nits are recorded as non-blocking
follow-ups. Turn the cross-model step off for a run with `/plan-run --no-cross-review`.

**Six skills here could all be called "a review", and they are not interchangeable.**
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
the source is labeled as that rather than as a run of your code. When its two lanes run
different models — both runtimes, or one runtime running two models — the agent that checks a
finding runs on the other model.

One round asks a different question: **which shapes of input none of your tests construct**.
What it finds is reported as a test to write rather than as a defect, because a missing test
is not a failure, and a report that lists it as one hides it among the dismissed findings.

Those two rules give you something nothing else in the pack offers: **it proves it read
everything you gave it.** Every file is accounted for, and if anything was left unassigned the
run fails and names the path. Every other review here is limited by something it chose, such as
a diff or a set of components the agent picked, and none of them can tell you what they missed.

**You get two things back.** A report for you to read (`report.md`, and `report.html`, the
same content as a web page), where each defect is one mistake listed with every place it has to
be fixed. And a fix brief for a coding agent (`fix-brief.md`, plus one `fix-brief/D<n>.md` per
defect), holding only what an agent needs to make the fix. Hand the whole brief to one agent,
or one defect's file to each. Give them the tree the review read: the commit the brief names,
plus any uncommitted changes the review saw. The brief does not carry notes you add to the
report, so leave out, or tell the agent about, any defect you marked as wrong.

It never edits your tree. It needs Python 3.10+ for its bundled engine and the `diff-review`
skill installed beside it. It is not a cheap run: two readers per area, plus a check for every
finding. Use it before publishing something, not at every commit.

It does not replace `/diff-review` or `/security-review-codebase`. `/diff-review` is limited to a change set, and `/security-review-codebase` sweeps a whole tree for vulnerabilities; this one
reads whatever you point it at, for whatever problem you state.

### The rest of the pack

Every skill here can be invoked on its own, whether or not you use the planning cycle. Seven
of them have not been described yet:

- **`/handoff`** — when the context is full and the work is not, it writes a restart prompt
  for the current work to `~/.handoff/<project>.md`: the goal, what is done and what is next,
  the decisions already made, the traps found and the uncommitted work. It ends with one line
  to paste after `/clear`, and the fresh session picks the work up from the file. It never
  overwrites another project's file, and where it cannot write the file it prints the prompt
  instead.

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
  anything into the repository it is auditing. The single-pass review writes no files at all
  and hands you its report directly. Deep mode writes its working files to a directory under
  your OS temp path, checks that the directory is outside the audited tree, and prints its
  absolute path.
- **`/web-verify`** — proves a web UI renders and behaves by looking at it, because a passing
  unit test does not mean the page shows what it should. It finds the Playwright setup the
  repo already has, drives the app through the flow under review, captures a screenshot at
  each state (and frames from a video where ffmpeg is present). It then checks the images
  against assertions **anchored** to elements that carry content, such as a heading with the
  right text or a table with rows. It never accepts "the page returned 200" or "a container
  exists", which pass while the page is blank. A saved screenshot is evidence to inspect, never a
  conclusion: nothing counts as verified until the expected content is confirmed in an
  actual image. It never installs Playwright; without one it hands you a manual click-through
  checklist and says the verification ran in degraded mode. `/plan-run` runs it on every
  phase that has UI.
- **`/demo-video`** — records a guided-tour walkthrough of a finished feature, driven slowly
  with pauses, with timed subtitles derived from `as-built.md`. Two prerequisites degrade it
  differently. Without **ffmpeg** you still get Playwright's own video with a subtitle file
  beside it; you lose only captions merged into the video file, music and frame extraction. Without
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
| `plan-duel` | Two models write competing plans and refine them against each other; needs Python 3.10+, the `diff-review` skill and, as shipped, **both** CLIs (or one CLI with a role on a backend running another model). In place of `/plan-init` when the plan itself is the risk | Degraded |
| `diff-review` | Diff-first review by a second reviewer, as independent as the host allows and cross-model when a second CLI, or a backend running a different model, is available; never edits the tree. Before a merge, and inside the `/plan-run` gate | Degraded |
| `xc` | Cross-checks a document — a plan, a phase breakdown, a design note — with a different model: a `cyw` pass, the other model's review, each disagreement settled once, only the survivors applied, and `cyw` again. After `/plan-init` or `/plan-phase`, or before acting on any document | Degraded |
| `review-panel` | Blind multi-agent sweep of a file set: bounded areas, readers who see nothing of each other, every finding checked by one who did not raise it; writes a report for you and a fix brief for an agent, never edits. Before publishing something. Needs a host that runs two workers at once and refuses one that cannot | Runtime-limited |
| `security-review-codebase` | Whole-codebase audit for exploitable vulnerabilities; single-pass by default, hierarchical deep mode on request | Degraded |
| `tdd` | Red/green/refactor, with the failing test confirmed before any implementation. When getting the behavior right matters more than getting it quickly | Full |
| `commit` | Stages the paths the change touched, never a sweep of the tree, and writes the message from the diff; pushes only when asked | Full |
| `handoff` | Writes a restart prompt for the current work to a file and prints the one line to paste after `/clear`. When the context is full and the work is not | Degraded |
| `web-verify` | Screenshots a running web UI and inspects the images against anchored assertions. After changing UI; needs an existing Playwright setup | Degraded |
| `demo-video` | Records a guided-tour walkthrough of a finished feature with timed subtitles. When the feature is done and you want to show it | Degraded |
| `extract-hooks` | Moves non-UI logic out of `.tsx` components into custom hooks. React and TypeScript only | Full |
| `plan-init-v1` | Superseded: writes a v1 plan (bugfix-only support) | Full |
| `plan-phase-v1` | Superseded: breaks a v1 plan into phases plus `phases.md` (bugfix-only) | Full |
| `plan-run-v1` | Superseded: executes a `phases.md`-driven plan (bugfix-only). Only to finish a plan already started under v1 | Full |

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

**`install.py` requires Python 3.10+**, and three skills need it whether or not you use the
installer: `plan-duel` and `review-panel` refuse to run without it, and `diff-review`'s
cross-model step needs it too. Because the installer needs it as well, a missing interpreter
is reported once, clearly, at install time, instead of leaving you with a skill that fails
days later. Install as a plugin or by hand and you need no interpreter for the others — every other
skill in the pack is plain Markdown.

Four things worth knowing about how it behaves:

- **It removes skills it installed that this pack no longer ships.** A retired skill would
  otherwise keep loading forever — the runtimes find skills by looking for directories,
  with no list to consult. Only directories recorded in its own manifest are removed, so
  anything you created is untouched.
- **It will not replace a skill directory it did not install.** If you have your own
  `cyw/`, an update skips it and says so; `--force` overrides.
- **An interrupted install is repaired by running it again.** A skill is replaced in
  place: what is there is removed, then the new one is copied in. So an interruption can
  leave a skill incomplete — `--verify` reports it, and another run replaces it. Ownership
  is recorded *before* anything is copied, which is what makes the repeat an ordinary run
  rather than one that refuses directories the installer itself created.
- **It creates your backends file when you have none, and never touches one you have.** A
  default install (no `--target`) that succeeds writes `~/.portable-agent-skills/backends.json` from
  `backends.default.json`, and beside it an empty key file for you to fill in. See below.

### Choosing the model a second agent runs on

Five skills start a second agent: `diff-review` (its reviewer), `plan-duel` (both sides and the
judge), `review-panel` (each group of its readers and checkers), `plan-run` (a phase worker) and
`security-review-codebase` (a component reviewer). By default that agent runs on Claude Code's
or Codex's own sign-in. You can instead run it on another model by naming a **backend**,
including open-weight models such as GLM, DeepSeek and Kimi. Open-weight means the model's
weights are published, so many providers can serve it; you pay a provider such as OpenRouter or
Fireworks rather than Anthropic or OpenAI.

**A backend** is a named entry in one file you keep,
`~/.portable-agent-skills/backends.json`. It says four things: which CLI runs the agent (Claude
Code or Codex), which model, which provider serves it, and which environment variable holds
the provider's key. The key itself is never in the file. [BACKENDS.md](BACKENDS.md) describes
the file in full.

**You do not need any of this.** With no file, or no folder, every agent runs on your Claude
Code or Codex sign-in. Name no backend and the agent uses your sign-in. Sign-in and backends mix
freely: one side of a duel can run on your Claude login while the other runs on Kimi K3.

#### Set it up

1. **Install as usual** (`python3 install.py`). A default install creates, only if they do not
   exist yet:
   - `~/.portable-agent-skills/backends.json` — the backends listed below;
   - `~/.portable-agent-skills/keys.env` (`keys.ps1` on Windows) — each key empty. On Linux
     and macOS only you can read it; on Windows it takes your home folder's permissions, which
     by default admit only you;
   - `~/.portable-agent-skills/.gitignore` — naming both key files. If you already have one
     there, the installer adds whichever key file names it does not list, at the end, and
     changes nothing else in it. If that `.gitignore` is a symbolic link, which git does not
     read, the installer leaves it alone, creates no key file, and says why.

   It never overwrites or deletes any of them afterwards, on reinstall, upgrade or uninstall.
2. **Get an API key** from OpenRouter, Fireworks, or both, in each provider's account
   settings. You need only the provider whose backends you use.
3. **Put the key in the key file**, between the quotes:

   ```bash
   export FIREWORKS_API_KEY="<your Fireworks key>"
   export OPENROUTER_API_KEY="<your OpenRouter key>"
   ```

   On Windows, `keys.ps1` holds `$env:FIREWORKS_API_KEY = "..."` lines instead. Leave a line
   empty if you do not use that provider. An empty line still sets its variable to empty, over
   any value your startup file exported earlier, so delete the line instead if you already
   export that key elsewhere.
4. **Load the key file from your shell's startup file.** The key file's first lines show the
   line to add. The installer never edits your startup file. For bash (`~/.bashrc`) or zsh
   (`~/.zshrc`):

   ```bash
   [ -f ~/.portable-agent-skills/keys.env ] && . ~/.portable-agent-skills/keys.env
   ```

   For PowerShell, add `. $HOME\.portable-agent-skills\keys.ps1` to your `$PROFILE`.
5. **Open a new terminal, then start Claude Code or Codex from it.** A program started before
   that line has run never sees the keys. To check without printing a key:

   ```bash
   test -n "$OPENROUTER_API_KEY" && echo "OpenRouter key is set"
   test -n "$FIREWORKS_API_KEY"  && echo "Fireworks key is set"
   ```

   In PowerShell: `if ($env:OPENROUTER_API_KEY) { "OpenRouter key is set" }`.
6. **Check a backend resolves.** This reads the file and makes no network call:

   ```bash
   python3 ~/.claude/skills/diff-review/review_runner.py --resolve-backend kimik3-codex-fireworks
   ```

   It prints `"status": "ok"` and the backend, or names what is wrong. On Windows, run `py -3
   $HOME\.claude\skills\diff-review\review_runner.py --resolve-backend kimik3-codex-fireworks`.
   An install with `--target` creates no backends file; copy `backends.default.json` to
   `~/.portable-agent-skills/backends.json` yourself.
7. **Try one real run.** Ask for a small review: "Review my last commit with the reviewer on
   backend `kimik3-codex-fireworks`." The review report names the backend and model that ran.
   If the key is missing, the run is refused before it starts, naming the variable.

#### The installed backends

Names read `<model>-<cli>-<provider>`.

| Model | Claude Code | Codex |
|---|---|---|
| GLM-5.3 | `glm53-claude-openrouter`, `glm53-claude-fireworks` | `glm53-codex-fireworks` |
| DeepSeek V4.1 Flash | `deepseek41-claude-openrouter`, `deepseek41-claude-fireworks` | `deepseek41-codex-openrouter`, `deepseek41-codex-fireworks` |
| Kimi K3 | `kimik3-claude-openrouter`, `kimik3-claude-fireworks` | `kimik3-codex-openrouter`, `kimik3-codex-fireworks` |

Of the three models, Kimi K3 costs the most and DeepSeek V4.1 Flash the least.

A twelfth, Codex with GLM-5.3 on OpenRouter, is **set aside**: its name starts with `//`, so
nothing can select it. Its answers do not reliably match the format a skill asks for, so a judge
or a review-panel reader on it would fail. The same model works through Fireworks. The file's
`why` note says how to switch it back on.

**A reviewer can still change files.** The skills launch a reviewer with flags meant to stop
it writing, and neither CLI guarantees that it cannot:

- **Claude Code** (`--permission-mode plan`) checks each action itself, and your own settings
  can allow more. If they pre-approve every shell command, a model that
  decides to write a file through the shell can do so.
- **Codex** (`-s read-only`) has the operating system refuse shell writes on Linux and macOS,
  and less reliably on native Windows. Its own file-edit tool is checked only inside Codex,
  so a bug there could let a write through.

In practice a reviewer is told to change nothing and rarely tries. The realistic risks are a
model "fixing" a bug it found, or text in the reviewed code telling it to run a command.
Commit or stash work you cannot lose before reviewing code you did not write.

#### Using a backend in each skill

Ask in plain words and name the backend; the skill passes it on.

| Skill | What to say |
|---|---|
| `diff-review` | "Review this diff with the reviewer on backend `kimik3-codex-fireworks`." |
| `plan-duel` | "Duel this, with the Codex side on backend `deepseek41-codex-fireworks`." The skill adds the backend to that side's settings. |
| `review-panel` | "Run the panel with its second group of readers on backend `glm53-claude-fireworks`." The panel runs two groups of readers, called lanes, and each can take its own backend. |
| `plan-run` | "Run the plan, with phase workers on backend `kimik3-codex-fireworks`." |
| `security-review-codebase` | "Deep review, with component reviewers on backend `glm53-codex-fireworks`." |
| `xc` | "Cross-check this plan with the reviewer on backend `kimik3-claude-fireworks`." From Claude Code, that is the same CLI on another model. |

Pick a backend whose middle word matches the CLI that runs that agent: `-claude-` for an agent
Claude Code runs, `-codex-` for one Codex runs. A mismatch is refused before launch. Two skills
can use only a `-codex-` backend. `plan-run` starts a phase worker as a separate program only
when Codex runs the plan without you, and `security-review-codebase` only when Codex runs the
deep review. Under Claude Code their workers run Claude's own model.

Every skill says in its output which model actually ran. A backend applies only to an agent
started as a separate program. An agent your host starts itself — a Claude Code sub-agent, for
instance — runs the host's own model, and the skill says so.

#### Where your data goes

**Every installed backend asks its provider to run the model in the United States.** Each
*provider enforces that request differently, and some only partly, and the file carries these
*notes as `//` entries.

- **OpenRouter** — the backends use its US address, `us.openrouter.ai`, which needs a
  Business or Enterprise plan. OpenRouter sends those requests only to providers whose
  hardware is in the US, and fails rather than sending them anywhere else.
- **Fireworks** — Fireworks' normal service sends requests to data centers in several
  countries. The backends use its US-only models, the ones whose names end in `-us`, which
  Fireworks says "serve inference exclusively from the US" and prices at 1.5 times the
  normal rate. On an ordinary account the `-us` name is the only thing keeping a request in
  the US: a model name without it runs wherever Fireworks chooses, with no error. Only
  Fireworks Enterprise accounts can make the whole account refuse non-US requests.

**What is kept.** Fireworks stores no prompts or outputs for these models unless you opt in, and
Codex tells it not to keep conversations. OpenRouter stores no prompts or outputs unless you
turn on its logging, and currently skips that logging for requests through its US address even
when it is on. The provider OpenRouter picks follows its own policy, which is why step 2 below
matters.

**What nobody can check from outside.** No response says which machine answered it. All of
the above is each provider's published promise.

**To have the providers refuse, not just avoid, sending your data elsewhere** (do these once, by
hand):

1. **OpenRouter guardrail.** Under Workspaces → Default → Guardrails, create a guardrail
   whose *Data regions* allows only `us`, save it, and assign it to your API key under its
   *API Keys* section — a guardrail does nothing until assigned. A request that reaches the
   global address `openrouter.ai` by mistake is then refused with a 403.
2. **OpenRouter zero data retention.** In Settings → Privacy, turn on Zero Data Retention for
   *all other models* (the group GLM, DeepSeek and Kimi belong to), and check that *Private
   Input & Output Logging* is off. OpenRouter will then use only providers that keep nothing.
3. **Fireworks residency (Enterprise accounts only).** Settings → Governances → Data
   Residency → US, or `firectl policy residency set US`. On other plans that page does not
   exist; your safeguard is the `-us` model names, or reaching the same models through
   OpenRouter's US address instead.

#### Keeping keys safe

- The backends file names a key's **variable**, never its value, so it is safe to share or
  commit. The key file is not: only you can read it, the `.gitignore` beside it and this
  repository's own `.gitignore` both name it, and it should never be synced. Sync
  `backends.json` alone.
- The pack never reads the key file. Keys reach an agent only from the shell you started it
  in, which it inherits as any program does; the backend decides which of them the CLI
  presents to the provider.
- Every installed backend hands the CLI the provider's own key. A Claude Code backend also
  blanks `ANTHROPIC_API_KEY`, and a Codex backend names a provider of its own, so your Anthropic
  or OpenAI login is not sent to the provider.

#### When something goes wrong

| What you see | What it means |
|---|---|
| "these credentials read a variable that is not set, or is empty" | The key is not exported in the shell that started your agent. Fill it in, open a new terminal, start the agent from there. |
| "no backend named …; defined: …" | A typo in the name, or the entry is set aside with `//`. The message lists the names that exist. |
| "there is no backends file at …" | You named a backend but have no file. Run the installer, or copy `backends.default.json` there. |
| 429 "temporarily rate-limited upstream" | The provider is out of capacity for that model. Wait a minute, or use the same model on the other provider. |
| A 403 from `openrouter.ai` | Your US-only guardrail is working: the request went to the global address. Use a backend's `us.openrouter.ai` address. |
| A reviewer's verdict is unreadable | Some model and provider pairs do not reliably keep to a requested format. Use another backend for that role. |

To stop using a backend, name none. To set one aside, rename it `// name`; to remove all of
them, delete `backends.json` — nothing else depends on it. [BACKENDS.md](BACKENDS.md) has the
full format, the three ways a key can reach a model, and recipes for writing your own entries.

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
installer does not remove skills it no longer ships. It installs its own skills, writes a
manifest listing only those, and leaves every skill it has never heard of on disk, unowned.
A later `--uninstall` skips such a skill, and a later run refuses to replace it without
`--force`. Uninstalling first gives
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
a skill this pack installed, uninstall still removes it, edits and all. Your backends file
and anything else in `~/.portable-agent-skills` are left in place.

## Development Setup

**The way to iterate on a skill is to edit it here and reinstall**:

```bash
python3 install.py
```

Installing 19 skills is a copy of about 80,000 words, fast enough to run after every edit.
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
before it becomes the instructions. Step 3 is the one people miss. A failure there
says nothing about whether the edit is right: the budget checks size and nothing else, and
it only says the size changed.

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
bytes are written. Only where the name and size already agree does it read the two files.
That catches an edit that happens to keep a file's length the same. For a pack of
instruction files that matters, because a skill is text a model obeys.

If you edited a skill in place, `DIFFERS` will report it. That is the answer to the
question asked, not an accusation: your install no longer matches the pack.

Running `python3 install.py` with no flag fixes every case above.

### Consuming from another repo

If you maintain another repository for machine setup, keep this pack as the one place
the skills come from, and point that setup at this checkout — run `python3 install.py` from it, or
`--target` a directory of your choosing.

## Validation

Run the portability validator and test suite:

```bash
python3 scripts/validate_cross_runtime.py skills/          # Check all skills
python3 scripts/validate_cross_runtime.py --test-fixtures tests  # Run fixture tests
python3 -m unittest discover -s tests -p 'test_*.py'        # Every Python suite
```

Those three commands are the whole gate. CI runs them on **Ubuntu, macOS and
Windows**, with the suites split into four shards (parts run as separate jobs) per platform.
It also runs a Linux pass under the C locale, which is the closest stand-in for the Windows
console encoding. Windows is where a
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
