# Changelog

All notable changes to this project are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
Versions use [Calendar Versioning](https://calver.org/) in the form
`vYYYY.MM.MICRO` — e.g. `v2026.04.0` is the first release cut in April 2026.
A MICRO bump in the same month indicates a follow-up release; a new month
starts from `.0` again.

## [2026.10.2] - 2026-10-07

### Added

- **`/handoff` writes a restart prompt you can pick up after clearing the context.** It saves
  the goal, what is done and what is next, the decisions already made, the traps found and
  the uncommitted work to `~/.handoff/<project>.md`, and ends with one line to paste after
  `/clear`; the fresh session reads the file and carries on. It names files by path rather
  than restating them. A later run for the same project replaces its own file and never
  another project's, even when two projects share a folder name. Where it cannot write the
  file, it prints the prompt instead.
- **`/xc` cross-checks a document with a different model.** Give it a plan, a phase
  breakdown or any document. It runs a `cyw` pass, has another model review the document,
  and decides each finding on its merits. Every finding it rejects goes back to the
  reviewer once, with the reason; one still disputed after that is listed for you and not
  applied. It applies what survives and runs the full `cyw` loop again. The reviewer is
  launched the way `/diff-review` launches its own, so a different model needs that skill
  and Python 3; without one, `/xc` uses a fresh reviewer on your own model, and with no
  separate reviewer at all it stops and says so rather than presenting its own review as a
  second opinion.

### Changed

- **`/plan-init` and `/plan-phase` offer `/xc` when they finish.** Each asks whether to have
  a different model cross-check what it just wrote, before its next step. An unattended run
  never starts the review, which costs time and money; it names it as an optional step.
- **`/diff-review` gives a different-model review with only one CLI installed.** Name a
  backend that runs your own CLI on another model, and the review runs there instead of
  falling back to a second copy of the same model; `/plan-run`'s phase gate gets the same
  through it. A backend written for the other CLI still launches that CLI, and a backend
  whose model name matches your own is not used as the different-model reviewer. Model
  names are compared as text, so the report names both models and says their independence
  is unverified: two spellings of one model would pass as different.

## [2026.10.1] - 2026-10-05

### Added

- **`/review-panel` writes up a run of hundreds of defects in full.** The write-up round
  used to hand one worker every defect in the run at once, and on a run of a few hundred
  defects that worker could return nothing, leaving the report without write-ups or
  sections. The rounds that check findings, group them and write them up now size each unit
  when they plan it, splitting the work so a unit stays within its limits. The limits have
  defaults and can be set per lane for each of those three rounds in the adapter config;
  the merge rounds keep their own fixed limits. One finding's file or one defect is never
  split, so an item larger than the limits on its own is still sent alone. A lane can also
  be given a hard limit. A finding to check goes only to the lane that did not raise it, so
  over that lane's hard limit it is not sent; work to group or write up goes to a lane whose
  hard limit takes it, and is not sent only when it is over every lane's. The steps that
  merge section names, summarize and write the overview always run on the first lane, under
  its limits. Either way the report says what was not done.
- **Write-ups come in batches, under headings in the project's own words, with an
  overview.** Defects are written up a folder at a time. Each batch names the sections its
  defects belong under, and a later step merges names that mean the same thing into one
  list for the report, and the report prints an overview of the whole run. A batch that
  fails costs only its own defects their write-ups, and the report says which defects were
  not written up and why.
- **`review_panel_run.py resynthesize <rundir> --adapter <config>` writes a finished run's
  write-ups again.** It starts from the findings the run already read, checked and grouped,
  and runs only the write-up rounds again; those workers still read the code as they write.
  Use it when a run's write-ups came back empty or partial — runs made by 2026.10.0 included
  — after lowering the sizes in the adapter config if a unit was too large. It has a time
  budget of its own, picks up where it stopped if interrupted, and replaces the report only
  once the new one is complete.
- **The run says how much work each round is.** The preview before a run starts says how
  many units the first round runs, each later round says how many it planned, and `status`
  counts the units of each kind.

### Changed

- **A `/review-panel` run started with 2026.10.0 has to finish on 2026.10.0.** This version
  refuses to resume such a run before its report is written, and says so. Once that run's
  report exists, `resynthesize` from this version can write its write-ups again.
- **Links between defects follow a new rule in a new write-up.** A write-up's link to
  another defect is kept when the two share a file or were written up in the same batch;
  sharing a section heading alone no longer keeps it. Reports written by 2026.10.0 re-render with
  their links as they were.
- **The write-up part of `findings.json` has a new shape for runs written by this version.**
  It lists the write-up units, and the overview, instead of one unit and a run-wide summary;
  and a write-up worker's reply no longer carries a summary of its own. Anything that reads
  `findings.json` with its own tools needs updating for new runs, and for a 2026.10.0 run
  once `resynthesize` has written it again; a 2026.10.0 run left as it is keeps its old
  shape.
- **Grouping a large area splits it by file, and a group never spans two files there.** In
  an area too large for one unit, findings are grouped one part at a time, and a reply that
  puts findings from two files in one group is refused for that part.

## [2026.10.0] - 2026-10-04

### Changed

- **`/review-panel`'s adapter guide says every worker runs outside any git repository.** A
  runtime that refuses to start outside a repository or a folder it trusts has to be told to
  skip that check in both of a lane's commands; for Codex that is `--skip-git-repo-check`.
  Without it every worker on that lane fails within seconds.

### Fixed

- **`/review-panel` no longer spends a unit on checks of a paused account.** When a lane's
  account paused, each check of whether it had recovered ran as one of the lane's units and
  was charged to it, so three failed checks could turn that unit into a permanent error that
  resuming the run did not undo. A failed check now charges nothing. A unit an earlier
  version already failed this way stays failed; plan a new run to have it read.
- **A paused `/review-panel` lane says why.** The pause message quotes the end of the latest
  failing worker's display log, where a runtime that would not start explains itself. It no
  longer says the paused units were charged nothing, which was not true.
- **`/review-panel` keeps a reader whose reply holds more findings than it said it would.**
  A reader states how many findings it is returning, and a reply that came back with more
  than that number used to be thrown away whole, losing that reader's area. Now only a reply
  with fewer findings than it stated is refused; a longer one is kept, and each finding in it
  is still checked on its own.
- **`/review-panel`'s write-up round skips what needs no write-up.** A defect dismissed
  everywhere it was reported, and a missing test, no longer go to the round that writes
  headings, fixes and groupings. The report lists them as before, under Refuted and in the
  coverage sections, and does not count them as anything the round failed to write.

## [2026.09.8] - 2026-10-03

### Changed

- **`/commit` adds a `Co-Authored-By` trailer only when your instructions ask for one.** It
  used to add one whenever the tool running it supplied an identity. Now it adds none unless
  your own or your project's instructions ask for it, and never when they say not to.
- **`/tdd` reports in plain prose.** In place of a fixed summary form, it says what the new
  tests cover and leave out, where they live, the command that runs them, and which tests it
  ran to check nothing else broke, so the report no longer implies a full-suite run that did
  not happen.
- **`/tdd` follows a plan's rule on which tests to run.** When `/plan-run` hands its test loop
  to `/tdd`, the plan's rule for which tests to run while working now wins over `/tdd`'s own
  rule of running the full suite, or at least the affected layer, after every green.
- **`/plan-phase`'s evidence record has a "Ran by" line.** It records where the phase ran (the
  supervisor with its backend and model, a sub-agent, or the current session) and whether the
  cross-model review was skipped on request. `/plan-run` already asked for both; the template
  now has a place to put them.

### Fixed

- **`/plan-init` links a plan from the plans index the way its template does.** A plan kept
  under the plans directory is linked relative to the index file in that directory, as
  `./<slug>/`. The instructions also gave a second form that does not resolve from inside the
  directory, and one of their sentences broke off mid-way; both are corrected.
- **`/plan-run-v1` stops and asks on a resume state none of its steps creates.** When a phase's
  Exit Criteria are partly ticked and its own work files are also modified, it now asks you
  rather than re-running the phase's checks on edits it did not make.
- **`/security-review-codebase`'s deep mode no longer depends on a background setting some
  Claude Code configurations do not have.** Under Claude it now says only to wait for every
  component review before the cross-component pass.

## [2026.09.7] - 2026-10-01

### Fixed

- **Files the skills' agents save are readable the way your other files are.** Every answer
  an agent saves through the shared supervisor — `/diff-review`'s findings and verdict file,
  `/plan-duel`'s plans and judge files, `/review-panel`'s per-unit transcripts, a `/plan-run`
  phase worker's answer, a reply kept word for word, and what is kept of an interrupted run —
  was created readable only by you. New ones now follow your usual file permissions, like
  anything else you create, and so do `/plan-duel`'s round snapshots and final named plan. A
  plan or snapshot `/plan-duel` overwrites keeps the permissions it has, so one written before
  this release stays readable only by you until you change it (on Linux or macOS, `chmod 644`
  and the file's name). The display log, which holds the agent's raw tool output, stays
  readable only by you.
- **`/review-panel` orders merge-check groups by number.** In a run with ten or more groups,
  G10 no longer comes before G2.
- **`/review-panel`'s path legend covers every file a coverage gap prints.** In a gap that spans
  several files, the other files now print under their short names with a row in the legend,
  like the first, instead of as full paths.
- **`/review-panel`'s result check refuses a malformed merge unit by name.** A merge unit listed
  without a list of site ids is refused with a message saying the unit listing is not the
  engine's, instead of stopping with a bare or misleading error.
- **The installer writes your backends file only after a clean install.** An install that is
  refused or partly fails no longer creates the backends file or the key file beside it; the
  next install that succeeds does.

## [2026.09.6] - 2026-09-30

### Added

- **The agents a skill starts can run on a model you choose, open-weight models included.**
  Name a **backend** — an entry in `~/.portable-agent-skills/backends.json` giving the tool
  to start (Claude Code or Codex), the model, the provider that serves it, and the
  environment variable that holds its key — and the agent runs on it. This covers the
  reviewer in `/diff-review`, the planners and the judge in `/plan-duel`, both groups of
  agents in `/review-panel`, a `/plan-run` phase worker, and a component reviewer in deep
  `/security-review-codebase`. Name no backend and an agent uses its tool's own sign-in, as
  before. An agent the host runs inside its own session always uses the host's model.
  `BACKENDS.md` explains the file, the ways a model can get its credentials, and how to set
  up OpenRouter and Fireworks. `PORTABLE_AGENT_SKILLS_BACKENDS` points to a file kept
  somewhere else.
- **Keys stay in your environment.** A backend names the variable that holds its key; the
  key itself is never written in the backends file. An agent whose key variable is unset or
  empty is refused before it starts. The message names the variable the agent reads, such as
  `ANTHROPIC_AUTH_TOKEN`, and the agent or backend it belongs to; that backend's entry says
  which of your variables feeds it. `/plan-duel` and
  `/review-panel` then stop; `/diff-review`, `/plan-run` and `/security-review-codebase` fall
  back to the host's own model and say so. One run can mix both: one planner on your Claude
  login, the other on a model served by Fireworks.
- **Each skill says which model ran.** `/diff-review`'s report names the backend and model,
  or says the reviewer used its tool's sign-in. `/plan-duel`'s summary gains a **Models**
  line. `/plan-run`'s evidence record and deep `/security-review-codebase`'s report say how
  each worker was started.
- **The installer sets up a starting backends file and a key file.** A default install
  writes the backends file when you have none, with eleven backends: GLM-5.3, DeepSeek
  V4.1 Flash and Kimi K3, for Claude Code and for Codex, through OpenRouter and Fireworks,
  all set up to run the model in the US. The OpenRouter entries use its US address, which
  needs an OpenRouter Business or Enterprise plan; the Fireworks entries use its US-only
  models, which cost 1.5 times its standard price. A twelfth, Codex with GLM-5.3 through OpenRouter, is
  switched off because its answers do not reliably match the format a skill asks for; its
  entry says so. Beside it goes a key file with `OPENROUTER_API_KEY` and `FIREWORKS_API_KEY`
  left empty for you to fill in — keys.env, readable only by you, on Linux and macOS, and
  keys.ps1 on Windows — and a .gitignore that names both. Where that folder already has a
  .gitignore, the installer adds the names it lacks at the end; where the .gitignore cannot
  protect the key file, it writes no key file and says why. The key file does nothing until
  you load it from your shell startup file; the key file's own first lines show the line to
  add. With
  `PORTABLE_AGENT_SKILLS_BACKENDS` set, it writes the backends file there and no key file.
  The installer never rewrites either file once it exists, never reads your keys, never
  edits a shell startup file, and leaves both in place on uninstall.
- **Comments in the backends file.** JSON has none, so an entry whose name starts with `//`
  is a comment. That is how the installed file explains itself, and how you switch an entry
  off without deleting it.
- **`/review-panel` can compare two models inside one tool.** Its two groups of agents may
  both use Claude Code, say, one on Opus and one on Kimi K3, as long as every command in both
  groups passes the model through. The report names this setup "one runtime running two
  models", and names the reverse, two tools running the same model, "two runtimes running one
  model".
- **`/review-panel` writes a fix brief beside the report.** It is a short version of the
  report for an agent that will fix the code, as Markdown, as JSON, and as one file per
  defect so each can go to its own agent. For each place to fix it gives the location, what
  is wrong there, the test that should fail first and, where the run has one, a command that
  reproduces the problem and what it shows. Missing tests come after the defects. It leaves out your own
  notes on the report, so tell the fixing agent about any defect you marked as wrong.
- **`skills/diff-review/review_runner.py` takes a backend on its command line.** For anyone
  who runs it directly: `--backend` starts the agent on a named backend, `--env` and
  `--env-from-parent` pass settings and keys by name, and `--resolve-backend` prints what a
  backend resolves to without starting anything. A new `raw-stdout` result mode keeps the
  agent's reply byte for byte and refuses one larger than `--max-capture-bytes`.

### Changed

- **`/plan-duel` now needs `/diff-review` installed.** Every agent it starts goes through
  the program `/diff-review` ships for starting an agent, watching it and stopping it at its
  time limit. Without it the duel stops before creating anything and says what is missing;
  `--supervisor` points to a copy elsewhere.
- **`/plan-run` and deep `/security-review-codebase` start their workers the same way.**
  Without `/diff-review`, the work goes to the host's own sub-agent, or runs in the current
  session where the host has none; when that program reports a failure, it runs in the
  current session. The record says which way it ran. A component reviewer is
  stopped after an hour, or after 15 minutes with no output, and leaves its full transcript
  in the run folder. A `/plan-run` worker's time limit is set per phase.
- **A resumed `/plan-duel` or `/review-panel` run refuses a changed agent.** In
  `/plan-duel`, changing the tool, model, backend or key variable of an agent whose results
  the resume keeps — or editing that backend's entry, or any setting in the agent's own
  `env` — is refused, naming what changed. `/review-panel` refuses a resume when any group's
  tool, model, account, backend, backend entry, settings or key variable differ from what
  the run started with, without naming which. Other edits still resume,
  such as a longer time limit or a different number of workers at once. Editing a backend's
  entry during a run stops the next agent on it from starting, and the run stops and names
  the backend.
- **`/review-panel` reports one mistake once, with every place it has to be fixed.** The
  report calls a mistake a **defect** and each place to fix it a **site**. After findings are
  grouped by place, one group of agents proposes which sites are the same mistake and the
  other checks each site against that claim; only sites the check upholds are joined.
- **`/review-panel`'s report puts the defects first and the evidence after them.** A defect
  counts as established when any of its sites is, and as refuted only when all are. Evidence
  that several sites share is printed once. Each part of a defect's written summary has a
  length cap; a summary that runs over any of them is dropped for that defect alone —
  heading, tier, account, fix, site notes and related links together — and the defect is
  grouped by its status instead, with a note saying so. A run
  folder from the previous release still re-renders, with each site as its own defect.
- **`/review-panel`'s findings.json has a new shape.** The `clusters` and
  `coverage_clusters` lists are replaced by `sites` and `defects`, and `coverage_sites` and
  `coverage_defects`, with sites numbered `S1`, … and defects `D1`, …; `merge` and
  `merge_check` record the two new steps. A tool that reads the old lists needs updating,
  and re-rendering an older run folder writes the new shape.
- **Running `/review-panel`'s stages by hand takes two more steps.** Between `cluster` and
  `synthesize` come `merge` and `merge-check`, each with its units dispatched before the
  next; `synthesize` refuses a run that has only been clustered. The bundled driver runs
  them for you.
- **`/review-panel`'s "What this report does not tell you" describes the run you have.** It
  says how many files in scope were read, excluded or skipped, and how many sites were
  checked but not settled, then says briefly what no run can tell you: which bugs it missed.

### Fixed

- **`/diff-review` states the limits of a reviewer's read-only settings more fully**, and the
  new `BACKENDS.md` states them too; the README gives a shorter account. Claude Code's `--permission-mode plan` is its own permission check,
  and your settings can widen it. Codex's `-s read-only` has the operating system block shell
  writes on Linux and macOS, and less reliably on native Windows. Neither covers hooks,
  plugins or MCP servers. Only a read-only copy or mount of the tree guarantees the reviewer
  changes nothing.
- **`skills/diff-review/review_runner.py` no longer follows a symbolic link at its
  display-log path**, so an agent able to write in that folder cannot send the log to a file
  outside it.

## [2026.09.5] - 2026-09-23

### Added

- **`/review-panel` keeps what whoever ran it knows after the run.** Answers to the owner's
  questions, corrections to findings that are wrong, and caveats about the run go in a
  report-notes.json file in the run directory, and `report --rerender` prints them: in a
  section of their own after the report's description, labeled as that person's reading,
  with a mark on every corrected defect, and on the build line where whether the tree
  builds or whether its tests run is corrected. The
  counts do not change and no finding is edited. A job may carry `questions` beside what it
  asks the panel to find; no reader is shown them.
- **`/review-panel`'s report prints two summaries the run already saved.** The judgment
  round's paragraph about the run appears under the opening counts, labeled as a reading
  nobody checked, and the capability probe's own account appears under the line saying
  whether the tree builds, so a failed build says why. `report --rerender` adds both to a
  report from an earlier run of this release; a run directory from the previous release is
  not read (see the lanes entry below).

### Changed

- **`/review-panel` hands over the report rather than summarizing it in the conversation.**
  When a run finishes, the agent that ran it records its answers to your questions, any
  finding it can show is wrong and any caveat about the run in the report itself, renders
  the report again, and points you at report.md with a line or two: the rung, the counts by
  status, and what its notes answer or correct. It no longer lists each established finding,
  what was refuted or left unresolved, and every coverage gap in the conversation; all of
  that is in the report.
- **`/review-panel` calls its two configurations lanes, and a lane has slots.** A lane is
  one runtime, model and account; its slots are how many of its workers run at once. The
  adapter config's top-level key is now `lanes`, and each lane takes `slots`, which
  replaces the driver's `--capacity` flag and its default of one worker per lane. The flag
  is gone: drop it from a saved run command, which otherwise stops with an unrecognized
  argument. `slots`
  defaults to two; the skill tells you the default and asks whether you want another number,
  and a resume may change it, for example after hitting a rate limit. The preview prints
  each lane's slots, whether they were defaulted, and its share of the work before anything
  runs. An adapter config or a run directory from the previous release is not read: rewrite
  the config and start a new run.
- **`/review-panel` builds with the versions the repository pins.** A lockfile the job
  excluded, or left off a `files` list, was missing from the snapshot the capability probe
  and the verifiers build in, so an install resolved newer dependencies and the build
  results described a tree nobody has. Inside a repository the snapshot now holds every
  lockfile git tracks as well as the files under review, and nothing else outside the
  review; the preview, and a run started with `--go`, say how many lockfiles are copied
  beside it. The probe installs exactly what a lockfile pins (`npm ci`,
  `pnpm install --frozen-lockfile`, `cargo build --locked`) and names the lockfile it used,
  so a stale lockfile can now fail a build that resolving afresh would have passed.
- **The CI workflow runs Linux alone in a private repository.** A public repository still
  runs the suite on Linux, macOS and Windows on every event. In a private one, where macOS
  and Windows minutes are billed, `.github/workflows/validate.yml` runs Linux only; a manual
  run with its new `all_platforms` input set to true runs all three. A private fork that
  takes this workflow stops testing on macOS and Windows unless it asks.
- **The test suite runs `/security-review-codebase`'s PowerShell block under every
  PowerShell on the host.** It already parsed the block and ran its path resolver under one
  PowerShell; it now does so under each it finds, which on a Windows runner includes Windows
  PowerShell 5.1, and checks that the block is ASCII. An edit that breaks the block only
  under 5.1 now fails the suite.

### Fixed

- **`/diff-review` no longer says plan mode lets a reviewer write through the shell, and
  `/review-panel`'s example adapter config describes its permission to match.** Claude's
  `--permission-mode plan` refuses any shell command it judges would change a file, as well
  as its edit tools. It is a permission check rather than an
  operating-system boundary: a command it judges read-only can still write, and hooks,
  plugins and MCP servers run outside it, so a review that must not touch the tree still
  wants a read-only mount or customizations disabled.
- **`/security-review-codebase`'s Windows block parses under Windows PowerShell 5.1.** Its
  comments and one error message carried em dashes, and 5.1 reads a script file without a
  byte-order mark in the ANSI code page, where one of an em dash's bytes is a closing
  quote: the string ended early and the block did not parse. The block is ASCII now.
- **`/review-panel`'s report explains itself in plainer English.** The fixed sentences that
  introduce each section and line are reworded; no heading, count, path, link or status
  word changed. Four of them were wrong and now match the numbers beside them: the probe
  line read "The capability probe missing" when the probe never landed, one refuted defect
  was called "them", an empty corroboration line pointed "below" at defects above it, and
  the capability line said "successes" where it counts reproductions that ran.
- **`/diff-review`'s supervisor stops its reviewer before an unexpected exception ends
  it.** The reviewer runs in a session of its own, so a supervisor that died of an error
  nobody handled, with the reviewer still running, left it running with no clock over it,
  while the status line printed on the way out told the caller the execution had ended. The
  child is now stopped and the claimed output files released before the exception goes any
  further.

## [2026.09.4] - 2026-09-22

### Changed

- **`review_panel_run.py --extend` is an absolute grant.** The number is hours beyond
  `--max-hours`. Running the same command again grants nothing more, and asking for more is a
  larger number. The flag added its hours on every invocation, and the run's advice on a
  spent budget was to raise it with `--extend` and run again, so a run restarted often
  enough with that flag left in its command line had no limit.
  A run started under the previous release with a grant already recorded keeps that grant
  until its next restart passes `--extend`, which then replaces it, and `--extend 0` now
  takes the grant back where it was ignored before: a restart command that carries
  `--extend 0` from habit ends the run's extra hours. A negative number is refused.
- **`review_panel_run.py resolve-unit --fail` asks for `--stopped-confirmed`** while the unit
  has an attempt nobody can account for, and with it fails that attempt too, so the slot comes
  free. Without the attestation the unit's error was published while the attempt kept
  reserving the slot's capacity for the rest of the run. The command takes `--grace`, the
  same number the run reads attempt states with, so which attempts count as unaccounted
  for is judged the way the run judges it.
- **The README and the contributor documents are written in plainer English.** Every rule,
  number and reason in them is kept, with three deliberate exceptions. The README and
  CONTRIBUTING no longer explain why an install is a copy rather than a link. Both gain
  steps in their section on editing a skill from inside an agent session, which now says
  which copy the agent edits and adds the word-budget step. And the README's decision guide
  and skill inventory are one table, with its installer section no longer repeating itself.

### Fixed

- **`/diff-review`'s supervisor accepts a reply that is the verdict object alone.** Under a
  schema flag, Claude sometimes answers with the validated object and no prose. Transcript
  mode reported that completed review as "reviewer produced no text output". The object is
  now published as the findings, and only a reply with neither prose nor an object is no
  output.
- **A line over `--max-capture-bytes` ends the diff-review supervisor's run as a capture
  overflow, however its bytes arrived.** Whether an over-cap line was kept as its tail or
  ended the run depended on where the pipe reads fell. One rule now: a single line larger
  than the cap is an overflow, and the bound that kept an over-cap result or terminal event
  as its tail is gone. A cap smaller than one line of the reviewer's output, which no
  default sets, now ends the run where it once succeeded with a notice.
- **The review-panel driver records a supervisor that exits without reporting a status.** It
  keeps each supervisor's stderr beside the attempt, writes the exit code and the end of that
  stderr as the attempt's status, and stops the run naming it. Such an attempt was read as a
  worker still running until its deadline and grace had passed, about an hour at the
  defaults, and a supervisor that crashed before printing it, such as one run under a Python
  too old for it, did that to every attempt. Recorded only where the exit says the worker
  is gone: on POSIX, for an ordinary exit code, by the driver process that started the
  supervisor. A supervisor a signal killed, one started by a driver since restarted, and
  any supervisor on Windows, where a terminated process reports an ordinary exit code, are
  left to the deadline as before.
- **A full disk while reserving or claiming a review-panel attempt is a resumable stop**,
  exit 3, the same answer a full disk during the working-copy preparation gets. It was a
  refusal, exit 2, or a traceback.
- **An unreadable checker verdict is reported as no answer.** When one verdict in a batch
  could not be parsed, the report filed that defect as blocked by the environment and showed
  the engine's parse error where the checker's rationale goes. It now lands in the same words
  the report uses for a candidate whose unit never answered, and the parse error appears once,
  under Coverage.

## [2026.09.3] - 2026-09-22

### Added

- **`/review-panel` — a new skill, and the heaviest correctness review in the pack.** You
  point it at a set of files and say what you are worried about. It splits them into areas
  and reads every area with **two agents at once, neither of which is handed anything the
  other produced**, each given a different angle to read for. Blindness here is a property of
  what each worker is given, not a sandbox: nothing stops a worker reading the run directory,
  and the skill says so where it lists what the run claims. Every finding is then handed to an agent that did
  **not** raise it, which tries to settle the claim by *running* it in a throwaway copy of
  the tree rather than by arguing — and reports the command it ran, the exit status and the
  output. Name a different runtime in each slot of the adapter file and that challenger is
  the other model; the run pins the pairing you wrote, not whatever happens to be installed.

  Two things make it different from every other review here. **Nobody ever checks their own
  finding.** And **every file you gave it is accounted for**: the run fails by name if a path
  was left unassigned, and the report names the files no reader reached — which is a check on
  the assignment rather than a promise that every file was read, since a reading unit can
  still fail. `/security-review-codebase` lists what it did not review; this is the one that
  refuses to finish without an answer for every file. It reports; it never
  edits your tree.

  **You run it with one command and read the report.** Write a small job file saying what you
  are worried about and which tree to read, write an adapter file naming the two worker
  command lines, and start it; it plans the run, launches every worker, lands every reply and
  writes the report. Stop it at any point by creating the file it names on its first line —
  what is running finishes and the run exits where you can restart it, picking up with no
  work redone and none lost.

  Needs Python 3.10+ for its bundled engine, `git` whenever the tree it reads is a
  repository, the `diff-review` skill installed beside it (its worker supervisor lives
  there), and **a host that can run two workers at once**.
  On one that cannot, it refuses and says which slot answered nothing, rather than producing a
  report. That is deliberate: every finding here is judged by a worker that did not raise it,
  so a single worker doing both jobs is not a weaker review — it is a different one, and the
  document would have to disclaim the only thing it exists to establish.

  It does not replace anything. `/diff-review` is anchored to a change set,
  `/security-review-codebase` sweeps a whole tree for vulnerabilities, and this reads
  whatever you name for whatever problem you state.

  **What it hands you is a document written to be fixed from**, with numbered sections you
  can refer to by part. It opens by saying what the document is — what a defect here means,
  what established, unresolved and refuted mean — alongside what the run established and
  what it could not: the counts, whether anything could be built or run, and which tracked
  files the scope never reached. Files are named by a short name throughout, with one legend
  table decoding every one of them, so an index row is a name you can read rather than a
  path that fills the line. Then an index of every defect there is something to do about —
  most severe first, cheapest fix first within that — a by-file view for whoever takes a
  file and closes what is in it, and the defects themselves.

  **The defects are grouped by what they break**, under plain-language headings a person
  would use, inside the established and unresolved sections rather than as a flat list in
  severity order. Each carries a short id you can cite in a sentence and one line of
  metadata, and where that grouping round returned an answer, two more things written to be
  acted on: what goes wrong, and the fix. The test that should fail first is there whenever
  the agent that checked the finding wrote one. A defect that should be
  read beside another links to it. Claims a challenger dismissed, the run's own machinery
  and the raw notes the narrative was written from go to an appendix; nothing you need to
  fix something is down there.

  **A missing test is reported as a test to write, not as a defect and not as a dismissed
  claim.** One round of the panel reads your tests and asks which shapes of input none of
  them constructs; what it finds goes to a section of its own, by file and then by line,
  each entry carrying the input nothing tries, the test the reader proposed, and what the
  checker found when it went looking for a test that already covers it. It is checked by a
  stranger like everything else, but against a different question — *does any test in scope
  build this input?* — because asking whether a missing test is a failure has only one
  answer, and it is the wrong one. Beside the Markdown report the run writes the same facts
  as JSON, and the same document as an HTML page with a contents list and links.

  **The report carries the job that produced it**, printed as JSON in an appendix section
  along with the command that runs it again — so whoever was sent the report can re-run the
  same audit without the run directory or the job file, given the same tree and an adapter
  file of their own. The page says outright which pieces it cannot supply. The root is
  replaced by a placeholder, since the tree is on their disk at their path.

  **The narrative, the fix and the links are marked as one agent's reading**, in a sentence
  where the defects begin, because nothing checked them. Everything else in the report traces to a
  worker that verified it or to the engine's own records, and the report does not let the
  two sound alike. A connection between two defects is checked as far as it can be — the
  other defect has to exist and the two have to touch a file in common — and one that fails
  is dropped and named rather than printed. Whether they are related *in the way the prose
  says* is not something this run establishes, and it says so.

  **It is honest about what it could not do**, which is the part a polished report makes
  easy to forget. A count of established defects is never presented as a count of validated
  ones: if nothing could be executed, that is said in the same breath as the number. A
  command that searched the source without running it counts as evidence and is labelled as
  what it is, so ten reproductions are never reported as ten runs of your code when only
  six of them were.
  It also tells you plainly that finding *more* is not the same as finding *yours* —
  measured over one tree, successive runs reported far more defects while catching fewer of
  five bugs already known to its owner.

  **And it prices what it left open.** Where a checker could not settle a claim because the
  answer lives in a file you did not put in scope, the appendix lists those files with how
  many defects each one would settle — so pulling one more file into the next run is a
  decision with a number against it rather than a guess. Beside it, one line per checking
  agent with how its answers fell, because two agents asked comparable questions and
  answering far fewer of them is a fact about that agent and not about your code.

- **`/diff-review` — two opt-in flags for a reviewer that stops answering.** `--status-detail`
  puts the provider's own refusal text on the status line, so a quota message arrives as what
  it is rather than as *the reviewer exited 1* — which matters when every remaining request
  would spend itself against an account that is already refusing. `--max-capture-bytes`
  bounds how much of a reply is held in memory, and drops from the front rather than the
  back, so a capped reply still ends in the object the caller is looking for. Both default to
  off, and pass neither and the capture limits are what they were. The status object is not
  quite: it gains `partial_findings` naming the kept transcript whenever a failed
  transcript-mode run left one, which is new in this release and arrives without either
  flag. And `blocking_count` in the verdict is now read as a count when the reviewer wrote
  it as a string or a whole-number float, and written back as an integer; a gate that read
  it as text before will see a number.

- **`/diff-review` keeps what a failed reviewer managed to say.** When a reviewer dies part
  way through a transcript-mode run, the text it produced is written beside your `--findings`
  path under a `.partial` name, and the status object names it under `partial_findings`. The
  findings path itself stays empty, because that path means a review completed and a
  truncated review filed there reads as a clean one. The name is claimed rather than
  overwritten, so a second failure in the same directory takes the next free name and both
  transcripts survive; nothing already at the path is truncated, and a pipe sitting there
  cannot stall the run.

- **`REVIEWS.md`, a plain-English comparison of every review skill in the pack**, linked from
  the README. Five skills here could all be called "a review" and they are not
  interchangeable. It sets them side by side: who reads the work, how much that reader
  already knows, whether a second model is involved at all, and who is allowed to challenge
  a finding once somebody raises it — which is the question that actually separates them, and
  the one nobody asks.

### Changed

- **The bundled CI workflow's jobs are renamed, and a fork that requires the old names by
  branch protection will block every merge until it is updated.** `.github/workflows/validate.yml`
  used to define `validate` and `windows`. It now defines `tested`, `suite`, `non-utf8` and `gate`.
  **Require `gate` and nothing else**: it stands for every job above it, so a shard or a
  platform can be added or removed without a required check going stale in either direction.
  The suite is now sharded four ways on each of Linux, macOS and Windows, with a separate
  four-shard run under a C locale; and a push to `main` whose tree has already been proved
  green by the pull request behind it skips the test jobs, which the proving run establishes
  by recording the tree it actually checked out.

### Fixed

A little over a hundred defects, found by reading every skill against its own code and
running the engines against hostile input. Grouped by what you would notice:

- **The installer no longer reads "I cannot look at this" as "there is nothing here."** A
  path it could not stat — a permission it lacks, a name the filesystem refuses — was treated
  the same as a path that does not exist. Installing then reported success over a skill it
  had not replaced, and removing reported nothing to remove. It now says which target it
  could not read, marks that skill skipped, and carries on with the rest. The same correction went through the bundled engines, which had the
  same shape in their own file reads: absence is only a missing file or a missing directory,
  and every other failure says so rather than answering the question wrongly.

- **`/plan-duel` — interruptions and odd input no longer end a duel badly.** A duel resumed
  after a crash now refuses cleanly rather than deleting work when the roles have changed,
  and never publishes a plan from a round that did not finish. A score it cannot parse is
  treated as unscored rather than fatal, and a whole-number score written with a decimal point is now
  accepted where it used to be discarded — which can change when a duel converges. Where a
  judge's reply carries both the `SCORE:`/`PREFERRED:` markers and a JSON example, the
  markers win; the example used to.

  Files that are not UTF-8, deeply nested JSON and a directory with no problem statement are
  each refused by name, with the reason, instead of crashing or half-running. A file saved as
  UTF-8 **with** a byte-order mark now reads correctly rather than failing — PowerShell
  writes them. A long problem statement typed straight into the command is read as text, not
  as a filename.

  **On Windows, two participant commands that ran before are now refused before dispatch.**
  One that resolves to a `.cmd` or `.bat` wrapper: point the adapter at the program the
  wrapper invokes, or run the duel under WSL. And one that resolves to the directory the
  duel was started from rather than to an entry on `PATH` — Windows searches the current
  directory first, and that directory is normally the repository being planned, so a
  program planted there would run with the adapters' flags. Name the CLI by absolute path,
  or start the duel from another directory.

  Two adapter changes worth knowing if you wrote your own: an adapter containing a
  placeholder the skill does not recognize is now rejected rather than passed through, and a
  participant declaring `stdout: clean-last-message` has its output written to the plan file
  instead of the status file.

  When a winner is stamped, the plan's line endings are preserved, the real status section is
  chosen rather than an indented example of one, the write is atomic, and a stamp that did
  not succeed is reported as such instead of assumed.
- **`/diff-review` — a hung or crashing reviewer can no longer take the review with it.** The
  supervisor cleans up processes that outlive the reviewer, handles being interrupted while
  it is mid-record, and no longer blocks forever on a pipe nobody drains. A verdict it cannot
  write is reported as exactly that, rather than as a failed review — so a completed review is
  never thrown away over a bookkeeping problem. A reviewer program sitting in the checked-out
  tree is still never executed, but the rest of your `PATH` is now searched for a real one.
- **`/plan-run` and `/plan-run-v1` — commit, push and waiting all got more careful.** A commit
  your git hooks reject now stops the block in failure instead of sailing on. A file changed
  by a gate is re-staged by name. Waiting for a long-running command was already bounded; it
  now **fails** when the finish marker never arrives, instead of falling out of the loop and
  carrying on as though the work had succeeded, and the native-Windows equivalents are
  spelled out. During a phase it runs the tests it touched rather than the whole module
  holding them, which is several times faster where a module holds one slow class.

  **Two changes to when a phase publishes.** The default branch is now asked of the remote's
  advertised HEAD first, with this clone's `origin/HEAD` as the fallback where the remote
  does not answer, rather than read from that local ref alone — a clone that never fetched
  the default branch has no such ref, and where your default branch was named neither
  `main` nor `master`, the guard meant to keep work off the trunk let every phase push
  straight to it. And whether the
  branch tip is already published is now asked of the remote by exact ref, so a branch
  deleted or rewound elsewhere no longer matches a stale local copy and silently skips the
  push.

  **Resuming is more careful in both generations.** A v1 phase resumed with its exit criteria
  only partly ticked, and a v2 plan resumed from outside `plans/<slug>/` with modified work
  files, each take a different bookkeeping and verification path than before.
- **`/web-verify` — frame extraction stops mangling paths.** An output directory whose name
  begins with `-` is treated as a path, a `%` in a path reaches `ffmpeg` escaped, and `ffmpeg`
  no longer reads from the shell's standard input — so a loop feeding it filenames no longer
  loses every line after the first. It now says that extracting frames
  needs Bash as well as `ffmpeg`.
- **`/security-review-codebase` and `/diff-review` now say where the heavier review lives.**
  The security audit stays about security: a correctness defect it notices on the way is
  mentioned to you in one line and never filed as a finding, and it names `/review-panel`
  as the skill for a whole-tree correctness sweep. `/diff-review` does the same at the end
  of a review where the change set warrants more than one reader; neither launches it.
- **`/commit`, `/security-review-codebase`, `/plan-init`, `/plan-phase`, `/demo-video` — smaller
  corrections.** Unstaging in a repository with no commits yet works on a file edited after
  staging, and each unstage failure names itself. **The security review's deep mode now
  refuses to put its report inside the tree it is auditing.** It resolves the temporary
  directory it writes to — `TMPDIR`, or `/tmp` — by where the path actually leads rather
  than how it is spelled, and where even the fallback lies inside the audited tree it stops
  with "no temp directory outside the audited tree; set TMPDIR". A project at `/tmp/proj`
  is unaffected — `/tmp` was and is outside it. What stops now is an audit rooted at `/tmp`
  itself, or a `TMPDIR` that spells a path outside the tree and resolves inside it through
  a link or a `..`; before, that report was written into the code under review. A plan is
  indexed by its own path rather than a generated name. The walkthrough spec records the timing
  its subtitles are derived from. The plan-tracker checker compares phase filenames by
  Windows' own case rules on Windows, so a tracker link whose case differs from the phase
  file on disk is matched rather than reported as an unlisted phase, and the tracker itself
  is found whatever its case.
- **Documentation that disagreed with the code now matches it.** Around twenty places where a
  skill promised behavior its own program did not have — a prerequisite that named only
  Python when git was needed too, a cross-reference pointing at the wrong step, a claim about
  what a reviewer is prevented from doing that was broader than the truth. `/plan-duel`'s
  prerequisites also named the wrong construct as the one Python 2 stops at.

## [2026.09.2] - 2026-09-06

### Changed

- `plan-run` now says how to wait for a command that outruns a single call, in the
  Satisfy step and in the phase-worker contract; `plan-run-v1` says the same in its
  verification step. A wait built on a process-name match (`pgrep -f`, `pkill -f`,
  `ps | grep`) never finishes under an agent harness, because the pattern sits in the
  polling shell's own argv and matches itself — a finished suite left the loop spinning
  and, once killed, was reported as a failure. The skills now say: use the runtime's
  background facility where it reports the exit; otherwise launch once, have the command
  append a marker carrying its exit status, and poll for that marker from a later call,
  bounded. A worker returns no `DONE` before the work has reported its exit, and reads the
  work's outcome from that exit or the marker rather than from the poll's own exit. And
  never add a trailing `&` inside a call already run through the runtime's background
  facility: the call is reported finished while the work runs on unobserved.

## [2026.09.1] - 2026-09-06

### Changed

- `plan-run` always writes a per-phase progress file now, rather than offering to. A
  delegated phase worker's output is invisible until it returns, so a long phase looked the
  same as a hung one; the worker now appends one timestamped line per step — `[+MM:SS]`
  from the phase's start, as `plan-duel`'s log already does — and the orchestrator appends
  the worker's exit status and elapsed time when it returns, so the log ends with an
  outcome even when the worker could not write its own last line. Nothing on the
  correctness path reads it and a failed write is ignored, so the run's result is unchanged
  whether or not anyone watches. The review sub-agent is no longer handed the file: it runs
  read-only and could never write it.
- `CONTRIBUTING.md` states that a change to the release machinery is reviewed on its own
  pull request, not at release time.

### Fixed

- `CONTRIBUTING.md` said the pack contains no PowerShell — the same sentence corrected in
  `README.md` in `2026.09.0`, in a third place. `security-review-codebase` ships a Windows
  PowerShell 5.1 block; the sentence now says so and points at `README.md`'s account.

## [2026.09.0] - 2026-09-05

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

## [2026.08.0] - 2026-08-26

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

[2026.10.2]: https://github.com/rosslevinsky/portable-agent-skills/releases/tag/v2026.10.2
[2026.10.1]: https://github.com/rosslevinsky/portable-agent-skills/releases/tag/v2026.10.1
[2026.10.0]: https://github.com/rosslevinsky/portable-agent-skills/releases/tag/v2026.10.0
[2026.09.8]: https://github.com/rosslevinsky/portable-agent-skills/releases/tag/v2026.09.8
[2026.09.7]: https://github.com/rosslevinsky/portable-agent-skills/releases/tag/v2026.09.7
[2026.09.6]: https://github.com/rosslevinsky/portable-agent-skills/releases/tag/v2026.09.6
[2026.09.5]: https://github.com/rosslevinsky/portable-agent-skills/releases/tag/v2026.09.5
[2026.09.4]: https://github.com/rosslevinsky/portable-agent-skills/releases/tag/v2026.09.4
[2026.09.3]: https://github.com/rosslevinsky/portable-agent-skills/releases/tag/v2026.09.3
[2026.09.2]: https://github.com/rosslevinsky/portable-agent-skills/releases/tag/v2026.09.2
[2026.09.1]: https://github.com/rosslevinsky/portable-agent-skills/releases/tag/v2026.09.1
[2026.09.0]: https://github.com/rosslevinsky/portable-agent-skills/releases/tag/v2026.09.0
[2026.08.0]: https://github.com/rosslevinsky/portable-agent-skills/releases/tag/v2026.08.0
[2026.06.0]: https://github.com/rosslevinsky/portable-agent-skills/releases/tag/v2026.06.0
[2026.04.0]: https://github.com/rosslevinsky/portable-agent-skills/releases/tag/v2026.04.0
