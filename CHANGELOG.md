# Changelog

All notable changes to this project are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
Versions use [Calendar Versioning](https://calver.org/) in the form
`vYYYY.MM.MICRO` — e.g. `v2026.04.0` is the first release cut in April 2026.
A MICRO bump in the same month indicates a follow-up release; a new month
starts from `.0` again.

## [2026.09.0]

### Security

- `security-review-codebase`'s deep mode told a Codex-driven reader to dispatch each
  component review as a shell string with the prompt in double quotes. The prompt carries
  the attack-surface document, which is built from the repository under audit, so a `$(…)`
  or a backtick in that content would have run on the auditor's machine before the
  read-only sandbox existed. The adapter note now shows an argv list with the prompt as one
  element, and says why.

### Fixed

- The same adapter note left standard input open. `codex exec` reads stdin even when the
  prompt is already in argv, so a scripted deep review blocked before it reached the model,
  with no output to diagnose the hang by. The note now says to close it.
- `security-review-codebase`'s deep-mode reference promised that running the component
  reviews sequentially "keeps full coverage" and loses only parallelism. The skill's own
  classification says otherwise: on a very large codebase one accumulating context can
  thin the later reviews. The reference now says what the classification says, in both
  places it had made the claim.
- `README.md` said the pack contains no PowerShell. `security-review-codebase` ships a
  Windows PowerShell 5.1 block that picks a report directory outside the audited tree.
  The sentence now says what is true: no command path is PowerShell, one skill ships some,
  and nothing statically checks it.

## [2026.08.0] - 2026-08-25

Sixteen skills, up from eleven. The planning workflow is rebuilt around a checkbox tracker
and a second, independent code review at every phase, with the previous generation kept
alongside under `-v1` names so a plan already in progress still runs. One Python installer
replaces the two shell ones.

**Before you update, read `Removed` and the two notes below it.** An ordinary
`python3 install.py` replaces every skill this pack owns and prunes the two it has retired,
without a prompt — and if you are a Codex CLI user, the skills directory has moved and your
old one is left behind.

### Added

- **`/diff-review`** — an independent, diff-first code review. Where `/cyw` is the author
  re-reading their own work, this is a second reviewer that reads the diff without the
  implementation rationale and reports correctness findings without editing anything. With a
  second runtime installed it runs there, so a *different model* examines the code; with one
  runtime it uses a fresh reviewer, and failing that a deliberate in-context reset. It says
  when it had to fall back to that last one, because a reviewer that has seen the reasoning
  is a weaker check.
- **`/web-verify`** — screenshot-first verification of a running web UI. Drives an existing
  Playwright setup and inspects the images against stated assertions. It never installs
  Playwright into a repository that lacks one; without it you get a manual checklist.
- **`/demo-video`** — a guided-tour walkthrough video of a built feature, with subtitles
  timed from the test steps. Without ffmpeg it still produces Playwright's own video plus a
  subtitle file. It writes subtitles, not speech.
- **`/clarify`** — explains something in plain English, from the conversation, a pasted
  document, code, or a link. Invoked bare it explains the last response. No repository
  needed.
- **The pack installs as an Agent Plugin.** A `plugin.json` at the repository root makes it
  installable by any [Agent Plugins](https://agent-plugins.org) 1.0.0 client, alongside
  `install.py` rather than instead of it. The standard discovers skills as
  `skills/<name>/SKILL.md`, which is the layout the pack already had.
- **`AGENTS.md`** — the traps that bite an agent editing a skill in a clone of this
  repository: the per-skill size limit, the two edits adding a skill needs that nothing
  discovers, the rule that a skill file may not reference anything outside its own
  directory, and the deliberate duplication between the two planning generations.
- **Machine-read skill outputs have a schema of record.** The `/plan-duel` judge verdict,
  the `/diff-review` findings object and the phase-worker result each ship a JSON Schema
  beside the skill. Where the spawned runtime takes a schema flag it is pinned and enforced;
  where it does not, the prompt asks for the object and a good narrative without a parseable
  one is still a successful result.

### Changed

- **One installer, in Python.** `install.py` replaces `install.sh` and `install.ps1`, and
  runs the same way on Linux, macOS and Windows. It reads the ownership manifest the shell
  installers wrote, so an install made by either can be updated or removed by this one.

  **Three flags are gone.** `--update` has no replacement and needs none — a plain
  `python3 install.py` installs or updates. `--dry-run` and `--link` have no replacement at
  all. If you script against the installer, check for those before updating.

  **It needs Python 3.10 or newer.** Installing as a plugin, or by copying skill directories
  by hand, needs no interpreter — but three skills have prerequisites at *use* time, however
  you installed them. `/plan-duel` runs a bundled Python engine **and needs both runtimes'
  CLIs on `PATH`**, so Python alone is not enough for it. `/diff-review` needs Python for its
  strongest cross-runtime mode and works without it at a weaker one. `/web-verify`'s optional
  frame extraction needs bash and ffmpeg, and degrades to a checklist without them. Every
  other skill is Markdown and needs nothing installed.
- **Codex CLI users: the skills directory has moved, and nothing migrates it.** The old
  installer wrote to `~/.codex/skills`; that path holds configuration, and the documented
  user scope — shared with several other runtimes — is `~/.agents/skills`. `install.py`
  writes there instead. **Your old directory is left exactly as it was**, with its eleven
  skills and its manifest — an install that nothing maintains any more, and a stale copy of
  skills that have since changed. Clear it out with the new installer, which reads what the
  old one recorded:

  ```bash
  python3 install.py --uninstall --target ~/.codex/skills
  ```

  Run it before or after updating. **It deletes each skill directory the old manifest
  recorded, whole** — so a file you added inside one, or an edit you made to one, goes with
  it. A skill directory you created yourself is not in that manifest and is left alone.
  Copy anything you want to keep out of those eleven directories first.
- **An update prunes what the pack retired**, and replaces what it still ships. A plain
  install removes skills the manifest records as ours but the source no longer carries, and
  overwrites the rest wholesale. Skills you installed yourself are untouched.
- **The planning cycle is `/plan-init` → `/plan-phase` → `/plan-run`, rebuilt.** A plan
  carries a `Format: v2` marker; work breakdown writes one document per phase plus a
  checkbox `execution.md`; a run resumes from the first unticked box. Each phase ends at a
  gate that runs the phase's scoped tests, a single `/cyw` author pass and `/diff-review`,
  and records a short evidence block. A UI phase additionally runs `/web-verify`. Nothing
  edits `plan.md` after it is written; where the work departed from the plan is recorded in
  an `as-built.md` at the end of a non-trivial run.
- **`/plan-init` writes two things it did not before**: for a plan under `plans/`, a row in a
  `plans/README.md` discovery index, creating that file if it is absent; and, when UI is in
  scope, a visual-verification success criterion in the plan itself.
- **The previous planning generation is available as `/plan-init-v1`, `/plan-phase-v1` and
  `/plan-run-v1`.** They are the skills that shipped under the plain names in `2026.06.0`,
  driven by `phases.md` rather than `execution.md`. A plan already underway keeps working;
  new work belongs to the current suite. The two are kept apart by the `Format: v2` marker
  on `plan.md` — the current skills refuse a plan without it, the `-v1` skills stop and
  redirect when they find one — and by the tracker filename, which is how each suite finds
  its own state without reading the other's.
- **`/plan-duel` is a bundled Python engine and runs in either direction.** The round loop,
  judging and resume logic moved out of prose into `plan_duel.py`, stdlib-only, so a resumed
  duel now replays its exit condition against what is on disk instead of leaving it to a
  model to reconstruct. Either runtime can be the controller, so the duel runs whichever one
  you start from. A run bounds every spawn with a timeout, refuses a workdir that already
  holds a duel rather than overwriting it, and states each role's file permission explicitly
  instead of inheriting the runtime's default.
- **`/security-review-codebase` absorbed the hierarchical mode.** Deep mode is now a
  reference the one skill loads when the codebase warrants per-component review.
  Single-pass writes nothing to disk, and deep mode writes outside the repository it is
  auditing.

  **It will also report differently.** A committed secret is now reportable rather than
  excluded, values from a CLI argument or the environment are trusted less, LOW-severity
  findings are suppressed by one stated rule instead of three sections disagreeing, a
  fresh-context pass filters false positives before you see them, and a clean report now
  names what was reviewed and what was not — so "nothing found" tells you its scope.
- **`/cyw` run on its own no longer stops after one clean pass.** A pass that finds nothing
  now needs a confirming second review before it stops, so a standalone run is longer than
  it was. Invoked from a phase gate — or with the argument `single-pass` — it runs exactly
  one pass instead.
- **`/extract-hooks` treats a declined candidate as a decision**, listing it once rather
  than re-arguing it on the next run, and now reports a hook whose logic no test exercised,
  rather than letting a green suite stand as evidence for code nothing covered.
- **`--verify` compares file contents**, by digest and kind, so a skill edited in place is
  reported rather than counted as present.
- **The project's own tests and CI ship.** Twelve Python suites, two stub CLIs, and a CI
  workflow that runs the validator, the fixture corpus and every suite on Ubuntu, macOS and
  Windows. None of it is part of an install; it is what a fork inherits to check its own
  changes.

### Removed

- **`plan-and-do`** — its testing tenets moved into the `plan-run` skills, where the work
  actually happens, so the discipline now applies during execution rather than in a separate
  document you had to remember to open.
- **`security-review-codebase-hierarchical`** — folded into `security-review-codebase` as
  its deep mode, at `references/hierarchical-mode.md`. Ask for a deep, thorough or
  hierarchical review and the one skill loads it; nothing is lost but the second name. If
  you ran the old skill, note that it wrote a run directory into the repository it was
  auditing and edited that repository's `.gitignore` to hide it. Deep mode writes to a
  temporary directory outside the audited repository and prints the absolute path.
- **`install.sh` and `install.ps1`**, replaced by `install.py`. Earlier tags still carry
  them.

**Both retired skills are pruned from your machine by an ordinary `python3 install.py`,
without a prompt.** So is any edit you made inside a skill directory this pack owns —
ownership is recorded as a directory *name*, and an update removes the directory before
copying the new version in, so a change you made to `cyw/SKILL.md` or any other pack skill
goes with it. Skills you created yourself are untouched. **Copy anything you want to keep
before you update.**

### Fixed

Four defects in skills you have been running since `2026.06.0`:

- **`/plan-run` no longer pushes to your default branch.** It ran `git push origin HEAD`
  after committing a phase, so an unattended run on `main` published every phase straight to
  the trunk — and from a detached `HEAD` that command has no destination and simply failed.
  It now derives the default branch and skips rather than fails, and a skipped push stops
  the run instead of ticking the tracker over an unpublished commit.
- **`/commit` stops after committing** unless you asked to publish. It ran
  `git push origin <current-branch>` as part of every invocation; "stage and commit" no
  longer pushes, while "push my changes" still works when there is nothing to stage. It also
  stages by named path instead of sweeping the whole tree, and surfaces unrelated files
  before they are committed rather than after. Three smaller fixes ride with it: a secret
  already staged before you invoked the skill is now caught rather than waved through, a
  secret reached by expanding a directory is caught too, a public key is no longer treated as
  one, and the message no longer goes through a shell heredoc — which does not exist under
  `cmd` or PowerShell — so committing works the same way on Windows. It also handles a
  repository with no commit yet, where the diff command it ran had nothing to compare.
- **`/plan-phase` writes beside the plan you gave it.** It accepted a plan anywhere and then
  created `plans/<slug>/phase-NN-*.md` literally, so a plan in `docs/` had its phase
  documents filed where nothing would look for them. It also refuses to overwrite an
  existing plan directory, and that check now runs before the first write rather than after.
- **`/tdd` accepts a failing assertion as red.** It recognised a missing module or a missing
  attribute and told you to fix the test for anything else — including a test that failed on
  the assertion it was written to fail on, which is the usual red when you extend an existing
  function rather than add a new one. It now takes any failure showing the *behaviour* is
  absent, a failing assertion among them, and says so rather than leaving you to infer it.

## [2026.06.0] - 2026-06-01

Windows support. This release shipped at the time and was documented afterwards, so its
tag was created later than its date — the tag marks the month the release came out, not
the day it was written up. See [Installing a previous
release](README.md#installing-a-previous-release) to return to it.

### Added

- **A native Windows PowerShell installer.** `install.ps1` is a copy-only port of
  `install.sh` for Claude Code and Codex CLI running natively on Windows, outside WSL.
  It honours the same `CLAUDE_SKILLS_DIR` / `CODEX_SKILLS_DIR` overrides and writes a
  byte-identical (LF) ownership manifest, so the two installers are interchangeable.
  There is deliberately no `-Link` mode: symlinks need elevated privileges on Windows,
  so the linked development workflow stays on `install.sh --link` under WSL or Git Bash.
  `tests/install.Tests.ps1` is a Pester 5 suite mirroring `test_installer.sh`'s coverage.
- **A how-to-use guide in the README** — `/cyw` as the universal sanity check, the
  planning cycle, and a decision table mapping common situations to a starting skill.

## [2026.04.0] - 2026-04-16

Initial release.

[2026.09.0]: https://github.com/rosslevinsky/portable-agent-skills/releases/tag/v2026.09.0
[2026.08.0]: https://github.com/rosslevinsky/portable-agent-skills/releases/tag/v2026.08.0
[2026.06.0]: https://github.com/rosslevinsky/portable-agent-skills/releases/tag/v2026.06.0
[2026.04.0]: https://github.com/rosslevinsky/portable-agent-skills/releases/tag/v2026.04.0
