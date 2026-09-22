# Changelog

All notable changes to this project are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
Versions use [Calendar Versioning](https://calver.org/) in the form
`vYYYY.MM.MICRO` — e.g. `v2026.04.0` is the first release cut in April 2026.
A MICRO bump in the same month indicates a follow-up release; a new month
starts from `.0` again.

## [2026.09.3] - 2026-09-21

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

[2026.09.3]: https://github.com/rosslevinsky/portable-agent-skills/releases/tag/v2026.09.3
[2026.09.2]: https://github.com/rosslevinsky/portable-agent-skills/releases/tag/v2026.09.2
[2026.09.1]: https://github.com/rosslevinsky/portable-agent-skills/releases/tag/v2026.09.1
[2026.09.0]: https://github.com/rosslevinsky/portable-agent-skills/releases/tag/v2026.09.0
[2026.08.0]: https://github.com/rosslevinsky/portable-agent-skills/releases/tag/v2026.08.0
[2026.06.0]: https://github.com/rosslevinsky/portable-agent-skills/releases/tag/v2026.06.0
[2026.04.0]: https://github.com/rosslevinsky/portable-agent-skills/releases/tag/v2026.04.0
