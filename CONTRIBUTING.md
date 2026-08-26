# Contributing

How to add a skill, change one, and pass the gate.

## Prerequisites

- **Python 3.10+** — for the validator, the tests and the installer. Commands here say
  `python3`: macOS has shipped no `python` since 12.3, and Debian and Ubuntu need
  `python-is-python3`. On native Windows, the current Python Install Manager provides
  `python3`; with an older python.org installer use the `py -3` launcher instead. CI says
  `python`, because `actions/setup-python` puts it on `PATH`.
- **Bash** — only for `skills/web-verify/references/extract-frames.sh`, the one shell script
  the pack ships, and the suite that exercises it. `test_extract_frames.py` skips itself
  when no working bash is found.
- **git**

## Repository layout

```
portable-agent-skills/
├── skills/<skill-name>/SKILL.md     # One directory per skill
├── scripts/validate_cross_runtime.py
├── scripts/check_plan_tracker.py    # Checks an execution.md tracker
├── scripts/skill-budgets.json       # Recorded word count per skill (the size ratchet)
├── scripts/private-identifiers.txt.example  # Copy to .txt and add names private to your
│                                            # workspace; the copy is gitignored
├── tests/                           # Validator fixtures, installer and engine suites
├── install.py                       # The installer: Linux, macOS and Windows
├── plugin.json                      # Agent Plugins 1.0.0 manifest
├── PORTABILITY.md                   # Authoring contract — read this first
└── .github/workflows/validate.yml
```

## Adding a skill

1. Create `skills/<your-skill-name>/SKILL.md` from this template:

   ```markdown
   ---
   name: <your-skill-name>
   description: >
     What this skill does and when to use it. Include trigger phrases
     ("Use when the user invokes /<your-skill-name>, or says 'X', 'Y', 'Z'").
   ---

   # <Human-Readable Title>

   ## Overview

   The outcome of running this skill.

   ## Step 1 — <first action>

   Keep instructions runtime-neutral. Use verbs ("Search for files matching
   `pattern`", "Read the file"), not branded tool names.

   If a step needs user input, give it an autonomous fallback:

   > Ask the user for X. If operating autonomously (no user available),
   > assume Y and note the assumption.
   ```

2. Pick a **classification** — **Full**, **Degraded** or **Runtime-limited**, defined under
   "Portability Classifications" in [PORTABILITY.md](PORTABILITY.md). Full needs no
   declaration; the other two need a line near the top of the file:

   `_Classification: Degraded — one line on what degrades._`

3. **Two manual steps, because everything else is discovered.** `install.py`, the validator
   and the tests pick up `skills/<name>/SKILL.md` on their own. You must still:

   - add the skill to README's table **and** bump its skill count — the validator's
     `check_readme_inventory` rule fails the run if you don't;
   - add it to `scripts/skill-budgets.json` with its current word count (skill-root `*.md`
     plus `references/**/*.md`) — a skill with no recorded budget fails the same run.

## Changing a skill

Preserve the core workflow. Portability fixes are welcome; workflow redesign belongs in a
separate proposal. If you rename or remove a companion-skill reference (`cyw`, `tdd`,
`plan-init`), run the validator — cross-reference checks flag stale mentions elsewhere.

### Editing a skill from inside an agent session

Asking an agent to improve its own skill edits the **installed copy** under
`~/.claude/skills/<name>/` or `~/.agents/skills/<name>/`, which git does not track. Carry it
back deliberately:

1. Diff that file against `skills/<name>/SKILL.md` here and port the change over.
2. Run the gate (below).
3. `python3 install.py` to put the reviewed version back on your machine.

There is no symlink install, so this is manual on purpose: a linked install would make the
installed path the repo file, and an agent asked to improve a skill would be rewriting the
instructions it is executing. An edit an agent made to its own instructions is worth reading
before it becomes the instructions.

## The two planning generations

`plan-init` / `plan-phase` / `plan-run` are the current suite. The superseded generation
ships alongside as `plan-init-v1` / `plan-phase-v1` / `plan-run-v1`.

They **coexist without collision**: the current skills act only on a `plan.md` stamped
`| Format | v2 |` and use an `execution.md` tracker, never `phases.md`; the `-v1` skills
track state in `phases.md` and never touch `execution.md`. There is no migration path — the
marker plus the distinct filename is the whole mechanism.

`execution.md` is a **checkbox list**: one box per phase, each linking that phase's document.
No status values, no scheduling keys, no marker of its own. The **phase document is the
durable state** — its Work, Tests and Gate boxes are ticked as work proceeds — and the
tracker is a derived index. That is what lets `plan-run` resume from the first unticked box
and self-heal after a crash.

One rule about the two generations is a *skill-text* rule rather than a procedure, and
lives in the contract instead: inside a `-v1` skill, references to siblings must be
`-v1`-qualified while a forward redirect to the current suite must not be. See
[PORTABILITY.md](PORTABILITY.md), "v2 Planning-Workflow Contract".

`scripts/check_plan_tracker.py` checks the tracker. The schema of record is
`skills/plan-phase/references/v2-templates.md`, which ships with the pack. **Change the
tracker shape and you change the schema, the checker and its fixtures in one commit.** Its
cases build throwaway plan directories in `tests/test_plan_tracker.py` rather than committed
`.md` files, because several rules are about the *directory* — a link must resolve to a real
phase document, and every phase document must be listed.

**Two properties of that checker are what keep it honest. Do not trade either away.**

- **Scope is decided by filename.** A directory scan inspects only files named
  `execution.md`. Deciding it from a file's *contents* makes every parsing bug present as
  silently skipped validation: a green run that checked nothing.
- **Nothing is parsed around.** Fences and HTML comments are rejected outright, and the file
  is split only on CommonMark line endings. If a new rule seems to need a Markdown model,
  ban the construct instead — and check first whether the rule earns its keep. The threat
  model is a **typo**, not an adversary: `plan-phase` writes the tracker, `plan-run` reads
  it, and a human reviews the diff.

## Parity ledger — intentionally mirrored blocks

Some text is duplicated rather than shared, because a skill must stay self-contained once
installed. **A fix to one side of a mirrored block lands in its mirror in the same change.**

- **The v1/v2 skill pairs** — `plan-init-v1` ↔ `plan-init`, `plan-phase-v1` ↔ `plan-phase`,
  `plan-run-v1` ↔ `plan-run`. A correctness fix that applies to both generations is
  backported, adapted to the other side's vocabulary: v1 knows only `phases.md`, and no
  v2-marker awareness may enter a v1 skill — **except** a read-only refuse-and-redirect
  guard, where a v1 skill detects a `Format: v2` plan solely to stop and point at the v2
  skill, never to act on it.
- **The TDD red/green block** — duplicated by design across the plan-execution skills. Do
  not dedupe into a shared reference.
- **Execution policy does not cross.** The v2 skills use a batched test cadence (filtered
  tests while iterating; the phase's scoped tests once; the full suite only at a reconciling
  or final phase), a **single-pass** embedded `cyw` at the gate, and **at most one commit per
  phase** — none when the phase stages nothing, and one extra only for a CI fix, which is an
  exception to the count and never to the gate. Both review axes are skipped together, and
  only when the phase has no reviewable diff. v1 keeps its original heavier gate. Do not
  backport this to v1, and do not re-sync v2's gate to match v1's.
- **v1 has no independent review axis, on purpose.** v2's gate runs `cyw` over the author's
  own change *and* `diff-review` as a separate reviewer that never sees the author's
  reasoning. v1's gate has only the first, so a v1 phase commits on the author reviewing
  their own work. That asymmetry is **not** a defect: v1 is the superseded suite, and adding
  a review axis to a deprecated path is feature work on the thing users are meant to leave.
  Note that v1's gate is heavier in *test cadence* and lighter in *independence* — two
  separate axes, and only the first is a policy divergence.
- **Shape does not cross.** v2's `plan-run` runs a phase as four transitions (**Select,
  Satisfy, Gate, Publish**) and v2's `plan-phase` emits a six-section phase document
  (**Goal / Work / Tests / Verification / Gate / Evidence**). v1 keeps its lettered sub-steps
  and its **Goal / Entry Criteria / Tasks / Tests / Verification / Exit Criteria / Commit**
  document. These are the same lifecycle written differently, so porting one into the other
  is a rewrite. A correctness fix *inside* one of those sections is still backported — check
  first that it is a correctness fix and not a piece of the execution policy above.
- **Plan document and index.** v2's `plan.md` carries only `Format` and `Suite` rows, because
  nothing edits it after `plan-init` writes it; v1 keeps `Phase` / `State` / `Blocker` /
  `Last updated`. v2's `plans/README.md` index has no `Status` column. Do not restore mutable
  status fields to v2, and do not remove v1's.
- **Delegation is v2-only.** v2's `plan-run` tags each transition worker-safe or
  orchestrator-only and ships `references/phase-worker-contract.md`. v1 has no worker concept.
  Do not backport it; it is a capability, not a fix.
- **The duel's condensed methodology** — `skills/plan-duel/init.md` mirrors `plan-init`'s
  content model (goal / success criteria / constraints / non-goals / affected areas). A
  content-model change to either is reflected in the other.

**SKILL.md-standard conformance is done by hand.** New skills conform to the open Agent
Skills format manually — `name` matches the directory, `description` is at most 1024
characters, a lean `SKILL.md` with detail pushed to `references/`. Do not add a hard
dependency on an external validator or packager without confirming its specifics first.

## The gate

```bash
python3 scripts/validate_cross_runtime.py skills/                 # Portability + genericity
python3 scripts/validate_cross_runtime.py --test-fixtures tests   # Fixture corpus
python3 -m unittest discover -s tests -p 'test_*.py'              # Every Python suite
```

All three must pass before a PR is eligible to merge. CI runs exactly these on **Ubuntu,
macOS and Windows** — one job each. There is no PowerShell driver, because there is no
PowerShell. A fixture that creates a symlink needs a platform guard, since creating one
needs elevation on Windows; those cases live in `tests/test_plan_tracker.py` behind a
symlink probe rather than in the corpus.

**Two programs are not in that list and should not be added to it.**

`scripts/check_plan_tracker.py` is a tool you point at your own plan directory. A clone with
no such directory gets `Path not found`, which is an error rather than a check. Its rules
are covered by `tests/test_plan_tracker.py`, which *is* in the suite.

`python3 install.py --verify` checks an *installed* pack against its ownership manifest. It
answers with three states:

| Exit | Means |
|---|---|
| `0` | what is installed matches this pack |
| `1` | something is installed and does not match — missing, incomplete, retired, or an older release |
| `2` | nothing to compare: no manifest here, or `--source` names a directory holding no skills |

`1` and `2` must stay distinct: "you have not installed this" and "your install is broken"
need opposite responses. Reporting success for an empty machine would be worse than either.
With several targets, `1` outranks `2`.

## Writing test fixtures

Validator fixtures live in `tests/` as `test_validate_<scenario>.md` — one positive or
negative fixture per check. A positive fixture must pass its check in isolation; a negative
one must fail it.

After adding a fixture, register it in `run_test_fixtures()` in
`scripts/validate_cross_runtime.py` and add a row to the fixture table in `tests/README.md`
with its expected outcome. A companion-fallback fixture only fires once its skill name is in
`COMPANION_SKILLS`. Tracker cases do **not** belong in `run_test_fixtures()` — they live in
`tests/test_plan_tracker.py`.

Confirm the fixture is genuinely exercised with
`python3 scripts/validate_cross_runtime.py --test-fixtures tests`.

## Portability, the short version

Read `PORTABILITY.md` for the contract. The most common reasons a PR fails CI:

- Branded tool names as normative instructions (`Use the Glob tool`, `subagent_type`)
  outside adapter notes
- A companion-skill reference (`` `cyw` skill ``, `` `tdd` skill ``) without a nearby
  fallback instruction, within about six lines
- A Degraded or Runtime-limited skill with no `_Classification:` declaration
- Codex install guidance pointing at `~/.codex/skills` instead of `$HOME/.agents/skills`
- Private paths, project-specific identifiers, or vendor attribution emails in skill text

**Three rules bind you that CI does not check** — hardcoded model names, autonomous
fallbacks, and naming both `CLAUDE.md` and `AGENTS.md` — so review for them by hand.
"Verifying a skill pack" in [PORTABILITY.md](PORTABILITY.md) is the canonical coverage list,
and it explains why one of those three reads exactly like a rule that *is* checked.

## Pull request checklist

- [ ] New or changed skill text passes the validator
- [ ] The full Python suite passes locally
- [ ] You have said which runtime(s) you actually tried the skill in
- [ ] If you added or renamed a skill, cross-reference checks still pass
- [ ] README's table and skill count updated, and `skill-budgets.json` if sizes moved
- [ ] CHANGELOG updated if user-visible behaviour changed
