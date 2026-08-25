# Cross-Platform Skill Portability Contract

This document defines the authoring standards for all skills in this directory.
Every skill must follow these conventions so that both Claude Code and Codex (or
any future runtime) can execute the same SKILL.md files with equivalent outcomes.

---

## Allowed Instruction Vocabulary

Use runtime-neutral verbs instead of branded tool names:

| Instead of | Write |
|---|---|
| "Use Glob" / "Use the Glob tool" | "Search for files matching `pattern`" |
| "Use Grep" / "Use the Grep tool" | "Search file contents for `pattern`" |
| "Use Read" / "Use the Read tool" | "Read the file" |
| "Use Edit" / "Use the Edit tool" | "Edit the file" / "Update the file" |
| "Use Write" / "Use the Write tool" | "Write the file" / "Create the file" |
| "Use the Bash tool" | "Run:" followed by the command, or "Run a shell command" |
| "Use the Agent tool" / "subagent_type" | "Spawn a sub-agent" or "Run as a parallel work unit if supported, otherwise sequentially" |
| "run_in_background" | "Run in the background if supported" |

When a specific shell command achieves the goal, provide the command directly
rather than naming a tool. For example: `grep -rn 'pattern' src/` rather than
"Use Grep to find pattern in src/".

---

## Banned Phrases

The following phrases must not appear in any skill file except in two
exempt contexts: **adapter notes** (blockquote lines labeled with "adapter")
and **classification declarations** (lines starting with `_Classification:`).

- `Glob tool`
- `Grep tool`
- `Read tool`
- `Edit tool`
- `Write tool`
- `Bash tool`
- `Agent tool`
- `subagent_type`
- `run_in_background`
- Hardcoded Claude-only or stale model labels used as normative instructions
  (e.g., `claude-sonnet-4-6`, `-m gpt-5.4` as fixed values)

---

## Companion-Skill Invocation

Refer to companion skills by name, not by slash-command syntax alone. Skill
invocation is **model-mediated** in both runtimes — "Run the `cyw` skill" is
instructional text the model may or may not act on, and neither runtime guarantees
skill-to-skill dispatch. Frame the fallback around **presence/invocation** (is the
skill installed? did it run?), not tool *capacity*:

| Instead of | Write |
|---|---|
| "Run `/cyw`" (as the only path) | "Run the `cyw` skill. If the `cyw` skill isn't installed or doesn't run, perform the equivalent review manually: re-read modified files, check correctness/completeness/consistency, and fix any issues found." |
| "Use `/tdd` if helpful" | "Use the `tdd` skill if available. Otherwise, follow TDD discipline manually: write failing tests first, then implement." |

Every companion-skill reference must include a deterministic fallback that
describes what to do if the skill isn't installed or doesn't run. The
manual-equivalent steps **are** the executed fallback when a companion is absent —
keep them; they are functional, not boilerplate.

---

## Agent Instruction File References

Both runtimes use instruction files but with different names. Always reference
both when mentioning project conventions:

- "Follow the project's `CLAUDE.md` / `AGENTS.md` conventions (whichever exists)"
- Never assume only one exists

---

## Parallel Execution

Parallelism is optional unless required for correctness. When desirable but
non-essential, use this pattern:

> "Run these in parallel if supported, otherwise sequentially."

Never treat parallel sub-agent execution as a mandatory prerequisite. If a
skill's core value depends on parallelism for performance but not correctness,
classify it as **Degraded** (not broken) when parallelism is unavailable.

---

## Independent Verification

Parallel execution is about speed. **Independence is about who judges**, and they are not
the same requirement: a skill can fan out ten readers and still have every finding graded by
the context that produced it. Where a skill asks for something to be checked, say which of
the two it needs.

A verifier's independence has three rungs, strongest first:

1. **A different model** — different blind spots, not merely a different context.
2. **A fresh unit in the same runtime** — free of the finder's sunk cost; same blind spots.
3. **A deliberate in-context reset** — read it as an outsider, ignoring the rationale that
   produced it. Weakest, and the only rung always available.

State the ladder, never a single rung: name the strongest rung the skill wants and what it
falls back to. Coverage is preserved all the way down — only the strength of the judgement
varies with what the host offers. A skill that *requires* rung 1 or 2 is not portable,
because no runtime is obliged to provide either.

Independence buys the one thing prose cannot: a verifier who is not the finder. Prose can
ask a context to refute its own finding; it cannot make that a second opinion. What prose
*can* carry is the burden of proof, and that half belongs in the skill whatever rung it
reaches — a finding stands on a stated mechanism (for code, the input that produces the
wrong result; for prose, the reader who is misled and what they do next), not on the
reviewer's confidence.

Where this pack implements it: `diff-review` states the full ladder and reports which rung
it reached; `plan-run`'s phase gate reaches it by calling that skill; `plan-duel` spawns the
judge as a third role, so the verdict comes from a spawn that authored neither plan — rung 2
against the controller's own agent, rung 1 against the other runtime's.

`security-review-codebase`'s deep mode deliberately does **not**, and it is the useful
counter-example. It fans out per-component sub-agents and then synthesizes their reports;
no verifier stands between a finding and the final document. That is breadth, not
independence — a skill that fans out is not thereby verified.

## Autonomous Fallback

Any step that normally asks the user to choose, confirm, or supply missing
context must include a deterministic fallback path:

> "Ask the user for X. If operating autonomously (no user available), assume Y
> and note the assumption."

This ensures skills can complete when invoked by another agent or in
non-interactive pipelines.

---

## Inline Adapter Notes

Where runtime-specific guidance is truly unavoidable (e.g., a skill that
orchestrates both runtimes), isolate it into a clearly labeled block:

```markdown
> **Claude adapter:** Use the Agent tool with subagent_type=general-purpose.
> **Codex adapter:** Run via `codex exec ...`.
```

Rules for adapter notes:
- Never create separate file forks (`SKILL-claude.md` / `SKILL-codex.md`)
- Keep adapter notes as small as possible
- The surrounding instructions must be runtime-neutral
- Adapter notes and classification declarations are the only places where
  banned phrases may appear
- **A spawned command states its own file permission — it never inherits one.** Derive
  the permission from what that role is *required* to do: write-scoped for an agent that
  must produce a file, read-only for one that must not. `codex exec` defaults are
  directory-trust dependent (read-only in an untrusted directory, writable in a trusted
  one), so an unflagged command silently behaves differently on each user's machine —
  and an agent that cannot write still exits 0, so the failure surfaces late and looks
  like something else.
- **State the sandbox *and* pin `approval_policy=never`.** The sandbox governs the
  model's *shell commands* — under `-s read-only` a shell redirect fails with
  `Read-only file system`. A built-in patch/edit tool is not a shell command, so the
  approval policy governs it instead: a `-s read-only` spawn with approvals at their
  default still wrote a file, and wrote nothing once the policy was pinned to `never`.
  Two write paths, two controls — state both. These spawns are non-interactive anyway,
  so there is no human present to answer an approval request.
  `scripts/validate_cross_runtime.py` enforces both halves for `codex exec`; `claude -p`
  is exempt only because its default withholds edit permission deterministically rather
  than by directory.

**At-parity accelerators (additive, never required).** Tool *capacity* is now roughly
at parity across runtimes — both offer subagents, a native review, and parallel work
units. An adapter note MAY *use* these as accelerators, but they are never a prerequisite:
the sequential, single-agent path stays the correct default and must remain fully
specified. Two accelerator shapes recur:

- **Concurrency** — running approved independent phases or a review as parallel work units
  (e.g. "Codex may run an approved independent phase or its review as a subagent; otherwise
  execute sequentially"). This improves wall-clock only.
- **Context hygiene (fresh-context-per-phase delegation)** — an orchestrator hands each
  unit of work to a *fresh* sub-agent so context doesn't accumulate across units and later
  work isn't coloured by earlier-unit rationalization. For this to be sound the durable
  state must live on disk (so the fresh worker needs no conversation memory), and the
  orchestrator must keep shared-state writes, the independent review, and the commit for
  itself — never delegating them to the worker.

**Classification hinges on whether the accelerator is essential to the skill's value, not
on whether it happens to be used.** If the in-context, single-agent path yields an
*equivalent outcome* — the accelerator only improves speed or context hygiene — the skill
stays **Full** (e.g. `plan-run`: the plan still executes correctly in-context, just
with context accumulating). If the accelerator is *core to the skill's value* so its
absence changes the outcome, classify **Degraded** (e.g. `diff-review`, whose
fresh-context independence is the whole point).

---

## Progress Reporting

A skill that **dispatches sub-agents** (or parallel work units) must declare, on a
`_Progress:` line near the top of its `SKILL.md`, whether those dispatched jobs expose a
live progress channel. Two postures are allowed:

- **`_Progress: observable`** — the skill offers an **append-only, non-blocking progress
  file** so a long-running dispatched job is watchable while it runs.
- **`_Progress: bounded`** — the dispatched work is **request/response**: the sub-agent
  runs a single bounded task and returns its result, so there is nothing long-running to
  observe and no progress file is used (`security-review-codebase`).

Reach for `observable` only when the dispatched job is long-running **and** otherwise
opaque to the dispatcher until it returns. A bounded job that returns its result in one
shot gains nothing from a progress file — and a file added "for consistency" only costs
overhead, an artifact to clean up, and a false sense of live monitoring. Likewise skip it
where the runtime already streams the worker's output.

**Invariants for an `observable` progress file.** It is a comfort feature and must never
become a new way for the run to fail:

1. **Append-only** — writers only ever append; nothing truncates or rewrites. This makes
   it safe when several workers share one log, and a crash leaves a readable partial trail.
2. **Off the correctness path — read by nothing.** No control-flow decision may depend on
   its contents; the run must complete identically whether or not anyone reads it. Code
   that reads it back to decide what to do next has turned it into a correctness
   dependency and lost the safety this pattern buys.
3. **Dispatcher-owned boundaries, best-effort detail.** The reliable signal is the two
   lines the dispatcher controls — one before spawning, one after the job returns. Any
   intermediate lines the worker appends are a bonus; monitoring must not assume they are
   complete, nor read a missing one as trouble.
4. **Separate from results** — the log lives apart from the tracker, evidence records, and
   any committed artifact; gitignore its directory so a stale log never lands in a diff.
5. **Alive, not correct** — a line proves the job *reached* a step, not that it is still
   running or that it succeeded. The authoritative "done"/"outcome" signal stays the job's
   returned status.

**Emit from code where you can.** Where a bundled engine owns the dispatch (`plan-duel`),
emit the boundary lines from the spawn wrapper itself, as a side effect of spawning, so
emission is structurally guaranteed and does not depend on a model remembering to log.
Where the dispatch is prose (`plan-run` may hand a phase to a fresh worker), the boundary
lines the orchestrator writes are reliable and the worker's intermediate lines are
best-effort; that skill's Delegation section documents the per-phase path convention and
the orchestrator/worker split.

This is a **declaration contract, not a runtime guarantee.** A progress file is
deliberately off the correctness path, so nothing verifies at runtime that a prose worker
actually appended its lines — the point of the contract is that every dispatching skill
has *consciously chosen* the posture that fits its job shape. The validator flags any
sub-agent-dispatching skill that lacks a valid `_Progress:` declaration.

---

## Bundled Executables

A skill MAY ship an executable helper (for example a Python engine) alongside its
`SKILL.md`. The installer copies the whole skill directory, so any file next to
`SKILL.md` travels with it. Two rules keep a bundled executable portable:

- **No branded CLI names hardcoded in the executable.** Any runtime CLI the helper
  shells out to (`claude`, `codex`, …) must be supplied as **argv data** from the
  `SKILL.md` adapter note and passed through — never a string literal baked into the
  script. The validator scans a bundled engine (e.g. `plan-duel`'s `plan_duel.py`)
  for hardcoded branded CLIs and fails on them, exactly as it does for prose.
- **Invoke subprocesses via an argv list, never a shell string.** The helper must
  spawn with an argv list (no `shell=True`, no `$(...)` / redirects / heredocs), so
  the same call works under `cmd`, PowerShell, and `sh`. Locate the interpreter and
  the injected CLIs by name (e.g. `shutil.which`), build paths with `pathlib`, and
  pin `encoding="utf-8"` on file I/O so Windows console code pages and CRLF don't bite.

If the helper needs a language runtime the host may lack (e.g. Python 3.10+), the
`SKILL.md` must declare that prerequisite in its `_Classification:` line and report a
clear message on absence rather than crashing. The added prerequisite makes the skill
**Degraded**, not Full. Declare it even for a runtime the pack's own installer requires: a
plugin client that copies `skills/<name>/SKILL.md` into place never runs that installer, so
an installed skill cannot assume any interpreter is present.

---

## Windows Link Hazards in Bundled Tooling

Any shipped program that *deletes*, *walks* or *resolves* a path must assume a reparse point
— Windows' term for a junction or a symlink — may be standing on it. The obvious API is
wrong in every case below. Some fail with no error at all; the rest fail with a message
naming something the user never created, on the one platform they are least likely to be
able to reproduce on. `install.py` implements each of these, with the reason beside the
code.

- **Asking whether a path is a link takes three questions, not one.** `is_symlink()` answers
  false for a junction, which is the Windows spelling of the hazard. `Path.is_junction()`
  answers that one but arrived in **3.12**, so a program supporting 3.10+ needs a fallback —
  and the fallback must read the reparse **tag**, not the reparse bit. Every reparse point
  sets `FILE_ATTRIBUTE_REPARSE_POINT`: a cloud-storage placeholder directory, a ProjFS root
  and a deduplicated file all carry it and none of them is a link, so testing the bit alone
  calls an ordinary synced directory a link and the delete rule below then tries to detach
  it. Read `lstat().st_reparse_tag` and accept `IO_REPARSE_TAG_SYMLINK` (`0xA000000C`) or
  `IO_REPARSE_TAG_MOUNT_POINT` (`0xA0000003`). `lstat()` and never `stat()` — `stat()`
  follows the link and reports on its target.

- **A recursive delete wants a link to detach, not a directory to descend.** The ordinary
  `is_symlink() or is_file()` / `elif is_dir()` shape hands a junction to `shutil.rmtree`,
  because `is_symlink()` answers False for one while `is_dir()` answers True. `rmtree` does
  not then destroy what the junction points at — it reads the reparse tag before descending
  and raises `Cannot call rmtree on a symbolic link`. So the failure is loud rather than
  destructive, and that is the whole of the good news: it names a symbolic link on a path
  nobody linked, it arrives only after the caller's retry loop has run out, and the skill is
  still installed when it does. Use the link test above, detach the link itself (`unlink`,
  falling back to `rmdir` for a directory reparse point, which refuses `unlink`), and reach
  for the recursive delete only for a directory that is not a link.

- **A tree walk must not descend through one either.** `rglob` follows a directory symlink:
  on a cycle it never terminates, and on a link pointing back at the source it walks the
  program's own tree. Walk with an explicit stack and ask the question above before
  descending into each directory.

- **`exists()` is the wrong occupancy test, because it is false for a dangling link.** A
  link whose target is gone still holds the name and still has to be removed, but `exists()`
  follows it and reports nothing there — so a cleanup skips it and a collision check waves
  it through. Ask `exists()` **or the link test above** wherever the question is "does
  something hold this name". Not `exists() or is_symlink()`: on Windows the link holding
  that name is most likely a junction, which is the one thing `is_symlink()` does not see.

- **A symlink loop raises `RuntimeError`, not `OSError`.** That is how `Path.resolve()`
  reports that it cannot answer, so a guard catching only `OSError` turns an unanswerable
  path into a traceback instead of a refusal. Catch both and skip the path: a guard that
  declines to match is safe, a guard that crashes is not.

- **A predictable temporary filename plus `write_text` is a redirect.** `write_text` follows
  a symlink, so a link planted at a guessable name sends the write outside the directory.
  `tempfile.mkstemp` opens `O_EXCL` on a random name and hands back a descriptor rather than
  a path to re-resolve — keep working through that descriptor (`os.chmod` where
  `os.supports_fd` allows it) instead of reopening by name.

The rule these share: on Windows, ask what a path *is* before acting on it, and never let a
recursive operation decide for you.

---

## Structured Machine-Read Outputs

When a skill *parses* a model's output rather than showing it to a human, pin the shape
with a **JSON Schema** passed to the runtime's structured-output flag — not with a text
format the prompt asks the model to follow. A text contract is unenforced by
construction, and it degrades in ways that are hard to see: the pre-schema `plan-duel`
judge was scraped for a `SCORE:` line that also appeared in its own prompt as a template,
so parsing a raw transcript could match the *instruction* instead of the answer.

- **One schema file per contract, shipped as a skill companion.** It travels with the
  skill like any other file next to `SKILL.md`.
- **Expose both argv forms from that one file — never duplicate it per runtime.** The
  runtimes disagree on how a schema is passed: one flag takes a **file path**
  (`--output-schema <FILE>`), another takes the schema **inline as text**
  (`--json-schema <schema>`). The bundled executable substitutes `⟪schema_path⟫` and
  `⟪schema_json⟫` markers in the adapter argv, reading both from the same document.
  Making the caller shell out to `cat` instead is not portable — a native-Windows caller
  has no `$(...)`.
- **The prompt stays byte-identical across runtimes.** The per-runtime difference belongs
  in the adapter argv, which is the whole point of the adapter boundary. A schema that
  forces two prompts has been designed wrong.
- **Parse defensively and keep the prior contract as a fallback.** Read the schema's JSON
  first, fall back to whatever the contract was before, and degrade rather than crash on
  either — artifacts written before the schema landed must still resume, and a runtime
  with no schema flag still answers in JSON because the prompt asks it to.
- **Omit `$schema`.** A draft-2020-12 `$schema` ref that one runtime accepts is rejected
  outright by another (`no schema with key or ref …`), before any model call. Drop the
  key; both then accept the document. `minimum` / `maximum` / `enum` /
  `additionalProperties: false` are accepted by both.
- **Assume the strictest mode.** List every property in `required` and express an
  inapplicable field as a nullable type rather than by omitting the key.
- **For mutually exclusive shapes, nest the union — never root it.** Both runtimes reject
  a schema whose *root* is `anyOf` (a 400 before any model call: `input_schema.type: Field
  required` / `text.format.schema`), and both accept a union nested one level down under a
  wrapper key. That one wrapper is the difference between exclusivity the schema
  *enforces* and exclusivity only prose can ask for: asked to emit an illegal mixture of
  two branches, a model under a nested union is forced into one valid branch instead
  (verified on both). Prefer it over a flat object of nullable fields, which accepts
  contradictory results — `plan-run`'s `DONE` / `BLOCKED` worker result is the worked
  example.
- **Validate shipped schemas at build time.** `scripts/validate_cross_runtime.py` parses
  every `.json` under `skills/`, so a stray comma fails CI instead of failing inside a
  spawned CLI's flag parser mid-run.

**Enforcement is a property of the dispatch path, not of the contract — state the
asymmetry rather than papering over it.** A worker spawned as a CLI subprocess can be
given the schema flag; the same worker dispatched **in-harness as a sub-agent** cannot,
because there is no flag to pass. Both return the same object because the contract asks
for it; only one is guaranteed. Say which is which, and never fail completed work over
its result formatting.

**Check what enforcement does to the narrative before enabling it.** Where a skill
returns a reviewer's or judge's *reasoning* as well as a verdict, a schema flag can
destroy the reasoning instead of structuring it — this is measurable, so measure it. In
this pack, one runtime returns the validated object on its terminal result event while
its assistant messages stay prose (two channels; enforcement is free), while another
coerces **every** message to the schema, replacing running narration with a series of
stub objects (one channel; enforcement costs the whole narrative). Enable the flag on the
first, omit it on the second and extract the object from the closing message. Where only
a final message is captured — `plan-duel`'s judge — there is no narrative to lose and
enforcement is unconditionally correct.

---

## Shell Assumptions in Prose Skills

_Guidance only — not validator-enforced._ Shell snippets embedded in skill prose are
executed by whatever shell the host runtime provides. On macOS/Linux (and Windows via
WSL or Git Bash) that is a POSIX shell, but a **native-Windows** runtime may execute
commands under `cmd` or PowerShell, where common bash idioms silently break:

- **Heredocs** (`<<'EOF'`) do not exist in `cmd`/PowerShell. Prefer describing the
  file content to write (letting the agent use its file-writing tool) over piping a
  heredoc into a command.
- **Bash conditionals and guards** (`if [ ... ]`, `command -v x || ...`,
  `$?`-chaining with `&&`/`||`) parse differently or not at all outside POSIX shells.
  Keep snippet logic trivial, or state the *intent* ("skip the commit when nothing is
  staged") so a non-POSIX agent can translate it.
- **`ln -sfn` symlinks** need privileges or Developer Mode on Windows and don't exist
  in `cmd`. Treat symlink-based flows as POSIX-only and
  provide a copy-based alternative for native Windows.

A snippet that must run verbatim on every host should be limited to `git` and
other cross-platform CLIs invoked with plain arguments; anything shell-specific
belongs behind an adapter note or an intent description. The Python interpreter
is **not** one of those CLIs, for the reason below.

**Spelling the interpreter takes host knowledge, so no single spelling belongs on
that list.** Prefer `python3`: macOS has shipped no bare `python` since 12.3, and
on Debian and Ubuntu a bare `python` needs `python-is-python3`. Native Windows is
generation-dependent — older python.org installers provide `python.exe` and
`py.exe` and no `python3`, while the current Python Install Manager does ship a
`python3` command. So where a snippet must run verbatim on a host you do not
know, give the `py -3` launcher beside `python3`. Everywhere else — a command run
in a known checkout, prose, a CI file — plain `python3` stands alone and needs no
pairing. Bare `python` is legitimate only inside a documented **probe** that
confirms the version it found, never as the invocation a reader is told to run.

**A probe that runs on Windows must try `py -3` before `python3`, and must
require a version string rather than a successful resolution.** A Windows machine
with no Python at all still has `python3.exe` on `PATH`: it is an App Execution
Alias that opens the Microsoft Store. A probe ordered the other way therefore
"succeeds" on the one host where it has found nothing, and whatever it was
guarding never runs. `py -3` is absent unless a real Python is installed.

---

## Genericity Requirements

Published skills must not contain private paths, project-specific references, or
user identity information. The following patterns are rejected by the validator:

- Absolute home directory paths (e.g., `/home/<user>`) <!-- hygiene-exempt: documents the rule -->
- Private repository paths (e.g., `dotfiles/claude/skills`, `~/projects/gh/main`) <!-- hygiene-exempt: documents the rule -->
- Project-specific identifiers (e.g., project names, framework-specific config
  files, or framework-scoped package names)

Use generic language instead: "check `package.json` or project config for sibling
repos" rather than naming specific frameworks or projects.

The scan covers **every file in the repository**, not only `skills/` — installers,
workflows, tests and scripts are where a real home path gets pasted. A handful of
files legitimately carry a private-looking string because their job is to: the
pattern definitions themselves, and fixture data proving the check fires. Those
lines carry the marker `hygiene-exempt` in a trailing comment, and the scan skips
any line containing it:

```python
re.compile(r"/home/[^/\s]+"),  # hygiene-exempt: this IS the pattern
```

The marker is **per line**, and that is the shape to reach for: exempting a whole file is
how coverage shrinks without anyone noticing. Reach for it only when the string must be
present for the file to do its job; the fix for an ordinary private path is to remove the
path.

**Three files are exempt whole, and they are the complete list**, held in
`HYGIENE_ALLOWLIST` in the validator: `tests/test_validate_private_paths.md` and
`tests/test_validate_hardcoded_attribution.md`, the negative fixtures whose entire content
is the strings these checks look for, and `scripts/private-identifiers.txt`, which is a
workspace's own list and never ships. So `grep -rn "hygiene-exempt" .` finds every
*line*-level exemption but not those three — read the constant as well when you are
auditing coverage. **Do not add a fourth without a reason of the same kind**: a file whose
job is to contain the pattern, not a file that happens to.

**Names private to *your* workspace go in a local list, not in the validator.** Copy
`scripts/private-identifiers.txt.example` to `scripts/private-identifiers.txt` and add one
name per line — an employer, an internal repository, a machine name — and the validator
reads it when present and rejects those names too. Only the `.example` template is
committed: the filled-in file is ignored, because a guard that ships the strings it guards
publishes exactly what it was meant to withhold.

---

## Portability Classifications

Every skill artifact receives one of three classifications:

| Classification | Meaning |
|---|---|
| **Full** | Works in both runtimes with equivalent user-visible outcomes |
| **Degraded** | Works in both runtimes, but one loses non-essential capabilities (e.g., parallelism) |
| **Runtime-limited** | Cannot honestly provide equivalent behavior; must declare the limitation |

Skills classified as **Degraded** or **Runtime-limited** must include a
classification declaration near the top of their SKILL.md.

---

## v2 Planning-Workflow Contract

Two generations of the planning suite ship together. One rule about them binds **skill text**
and belongs here; the rest is repository maintenance and lives in
[CONTRIBUTING.md](CONTRIBUTING.md).

A v2 `plan.md` carries a `| Format | v2 |` Status-table row. The v2 skills act only on marked
plans, and the `-v1` skills stay **marker-agnostic in behaviour** — the marker never changes
what a v1 skill *does*. The one allowed exception is a **read-only refuse-and-redirect
guard**: `plan-phase-v1` / `plan-run-v1` may detect a `Format: v2` plan solely to stop and
point the user at `/plan-phase` / `/plan-run`, never to act on it.

**Inside a v1 skill, a reference to a sibling — or to the skill itself — must be
`-v1`-qualified**, because the unqualified names now belong to the v2 suite. A **forward
redirect** to that suite must be **unqualified**, because canonical means v2. The two are
told apart by paragraph: a block mentioning `Format: v2` is a redirect context and every
other block is intra-suite. `check_v1_suite_routing` enforces both directions, and its
granularity is the paragraph — a redirect paragraph reads every bare name in it as a
redirect.

The tracker format itself — `execution.md` against `phases.md`, the checkbox shape, why scope
is decided by filename and why nothing in a tracker is parsed around — is not a portability
rule. It is in CONTRIBUTING.md, beside the parity ledger, and the schema of record is
`skills/plan-phase/references/v2-templates.md`, which ships with the pack.

---

## Verifying a skill pack

Skills classified as **Degraded** or **Runtime-limited** declare `_Classification:`
at the top of their `SKILL.md`. Run `python3 scripts/validate_cross_runtime.py
skills/` to check the pack.

**What that run does and does not cover.** It enforces the mechanical rules above —
banned phrases, companion fallbacks, classification declarations, progress postures,
self-containment, private paths and the size ratchet. These are the rules in this document
it does **not** check, and a green run says nothing about them. **The list is maintained by
hand, so treat it as the known set rather than a proof**: adding a rule here without a
`check_*` function to match means adding it to this list too.

- **Autonomous fallbacks** (above) — the rule binds an author and is real, but no
  validator pattern matches it: a user-prompting step with no fallback passes. Note the
  contrast with **companion** fallbacks, which appear in the enforced list above and are
  genuinely checked. Read for this one in review.
- **Instruction-file references** (above) — naming both `CLAUDE.md` and `AGENTS.md` is
  unchecked too; a skill that mentions only one passes.
- **Shell assumptions** (above) — guidance only, by the note in that section.
- **SKILL.md-standard conformance** — `name` matching the directory is checked; the
  description length and the lean-`SKILL.md` shape are conventions kept by hand.
- **Hardcoded model labels** — stated under Banned Phrases, but there is no pattern for
  them, so a model name in a skill file passes. Read for it in review.
- **Separate file forks** — "never create `SKILL-claude.md` / `SKILL-codex.md`" (Inline
  Adapter Notes) has no check. A skill shipped as two runtime-specific files passes.
- **Classifying an interpreter-dependent helper** — `check_classification` verifies that a
  skill *declaring* Degraded or Runtime-limited does so on a real declaration line. It
  cannot know a skill *ought* to be Degraded because its bundled helper needs Python, so a
  Full skill shipping such a helper passes.
