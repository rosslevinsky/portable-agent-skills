# Deep mode — hierarchical multi-component security review

The optional deep mode for `security-review-codebase`, for **large or complex codebases**.
It reuses the **Security Categories, False Positive Filtering, confidence threshold (≥ 0.8),
finding format, and severity guidelines from `SKILL.md`** — this file adds only the
orchestration: architecture understanding → attack-surface mapping → per-component
sub-reviews → cross-component analysis → synthesis, producing a persistent artifact set.

Run the per-component reviews as **parallel sub-agents where the runtime supports concurrent
work units; otherwise review each component sequentially in this context**. Sequential
execution keeps full coverage — it loses only parallelism and fresh-context-per-component
hygiene.

## Output directory setup

Before any analysis, set up the output directory. **It goes outside the worktree.** This is a
read-only audit of someone else's repository: it may not add files to their tree, and it
may not edit their `.gitignore` to hide the ones it added. Nothing here writes inside the
project at all, so there is nothing to ignore.

1. Determine the project root with `git rev-parse --show-toplevel` — it resolves the
   working-tree root correctly even inside a git worktree or submodule, where `.git` is a
   file (a gitdir pointer) rather than a directory, so a literal search for a `.git/`
   directory would miss it. **If the tree is not a git repository, use the directory the
   audit was pointed at** — resolved to an absolute path. A fallback that hunts for the
   nearest ancestor holding a `.git` entry is unreachable by construction: an ancestor with
   one is a repository, so `git rev-parse` would already have answered from inside it, and
   in a genuinely non-git tree there is nothing for that search to find. The root names the
   run and scopes the audit; it is not where output lands.
2. Create the run directory under the OS temporary directory, and use that exact absolute
   path as `<run-dir>` throughout. On a POSIX shell:

   ```bash
   # The base is CHECKED, not assumed. TMPDIR may be unset; it may hold a relative path;
   # and it may point inside the very repository being audited. Any of those would put the
   # whole report back in the user's tree — the one thing this must not do.
   root=$(git rev-parse --show-toplevel 2>/dev/null)
   # Outside a git repository that prints nothing, so apply step 1's fallback HERE — the
   # directory the audit was pointed at, which is the one you are in. It has to be after
   # the call: an assignment made before it is overwritten by the line above.
   [ -n "$root" ] || root=$PWD
   # An EMPTY root is still not a harmless default, so the guard stays: the containment
   # test below would become `case "$base/" in /*)`, which matches every absolute path, so
   # TMPDIR is always discarded and the run directory loses the project name.
   [ -n "$root" ] || { echo "project root not resolved — see step 1" >&2; exit 1; }
   base="${TMPDIR:-/tmp}"
   case "$base" in /*) ;; *) base=/tmp ;; esac          # absolute
   case "$base/" in "$root"/*) base=/tmp ;; esac        # and outside the audited tree
   run="$base/security-review-$(basename "$root")-$(date +%Y%m%d-%H%M%S)-$$"
   mkdir "$run" || exit 1   # plain mkdir: it FAILS on an existing directory rather than
                            # silently adopting one, so a run never inherits another's files
   echo "$run"
   ```

   On a native-Windows shell, the same three checks against `$env:TEMP` — absolute, outside
   the audited tree, and a fresh directory. Written out rather than summarised as
   "checked the same way", which is worse than saying nothing because it reads as a
   guarantee while performing none of the checks:

   ```powershell
   # No .Trim() on the call: outside a git repository this returns nothing, and
   # (nothing).Trim() is "You cannot call a method on a null-valued expression" rather
   # than the fallback step 1 promises.
   $root = git rev-parse --show-toplevel 2>$null
   # Step 1's fallback, applied HERE for the same reason as the POSIX block: assigning
   # $root before the call cannot survive it.
   if (-not $root) { $root = (Get-Location).Path }
   # An EMPTY root is still not a harmless default: the containment test below would
   # compare against nothing and never discard $env:TEMP, so a temp path inside the
   # audited tree would be accepted.
   if (-not $root) { throw "project root not resolved — see step 1" }
   $root = $root.Trim()
   $name = Split-Path -Leaf $root

   # Compare on ONE separator. `git rev-parse` prints the root with forward slashes even
   # on Windows, while %TEMP% comes back with backslashes, so a raw StartsWith between the
   # two never matches — the containment check would be inert on the platform it targets.
   function ConvertTo-Comparable($p) { ($p -replace '/', '\').TrimEnd('\') + '\' }

   # Absolute, and outside the repository being audited — a TEMP redirected inside it
   # would put the whole report in the user's tree, which this skill must never do.
   #
   # The fallback is NOT GetTempPath(): on Windows that reads %TMP% then %TEMP%, so a
   # poisoned TEMP fails the check and then comes straight back as the "safe" answer.
   # Fall back to a path that cannot be the one just rejected, and re-check it.
   function Test-SafeBase($candidate, $repoRoot) {
       if (-not $candidate) { return $false }
       # Fully qualified — drive-qualified or UNC. NOT IsPathRooted, which accepts a
       # drive-relative '\foo' that resolves against the current drive's working
       # directory and so names no fixed location. NOT IsPathFullyQualified either:
       # that is .NET Core only, and Windows PowerShell 5.1 is in the support matrix.
       if ($candidate -notmatch '^([A-Za-z]:[\\/]|\\\\)') { return $false }
       return -not (ConvertTo-Comparable $candidate).StartsWith(
           (ConvertTo-Comparable $repoRoot), [StringComparison]::OrdinalIgnoreCase)
   }
   # Built by interpolation, not Join-Path, and each rung guarded on its variable being
   # set. Join-Path resolves through the PowerShell drive provider: given an unset
   # variable it raises a terminating parameter-binding error, so a machine with no
   # LOCALAPPDATA aborts here instead of falling through to the next rung — failing
   # hardest in the stripped-down environment the chain exists to survive.
   $base = $env:TEMP
   if (-not (Test-SafeBase $base $root) -and $env:LOCALAPPDATA) {
       $base = "$env:LOCALAPPDATA\Temp"
   }
   if (-not (Test-SafeBase $base $root) -and $env:SystemDrive) {
       $base = "$env:SystemDrive\Windows\Temp"
   }
   if (-not (Test-SafeBase $base $root)) {
       throw "No temp directory outside the audited repository; set TEMP and re-run."
   }
   $run = Join-Path $base "security-review-$name-$(Get-Date -Format yyyyMMdd-HHmmss)-$PID"
   # -ErrorAction Stop is what makes this behave like the bash `mkdir ... || exit 1`.
   # Omitting -Force is necessary but NOT sufficient: an existing directory is a
   # non-terminating error, so by default PowerShell prints it, carries on, and the run
   # adopts the colliding directory — the exact outcome dropping -Force is meant to
   # prevent. `New-Item` does not throw on its own; `-ErrorAction Stop` is what makes it.
   New-Item -Path $run -ItemType Directory -ErrorAction Stop | Out-Null
   $run
   ```

   `-Path` is required — without it `New-Item` prompts for one instead of creating
   anything — and `-Force` is deliberately absent, since it would re-adopt an existing
   directory, which is exactly what the plain `mkdir` above refuses. Do not reach for
   `mkdir -p ...` under `cmd`: it would create a stray directory literally named `-p`.

   **Seconds and the process id, not minutes.** Two runs started in the same minute would
   otherwise share a directory, and the second would read the first's component reports
   into its synthesis — a report mixing two codebases' findings, with nothing saying so.
   `web-verify`'s `extract-frames.sh` already resolves it this way; this is the same
   answer, not a second one.
3. **Print the absolute run-directory path**, at the start and again with the final report.
   It is outside the project, so a user who is not told the path cannot find the output.

All intermediate files and the final report are written inside the run directory.

## Phase 0 — Documentation & architecture discovery

Read every architectural guidance file you can find: `CLAUDE.md` / `AGENTS.md` (root and any
subdirectory variants), `README.*`, any `architecture.*` / `ARCHITECTURE.*`, and files under
`docs/`. Understand the intended security model — what is trusted vs untrusted, where the
trust boundaries are, and what sensitive operations exist. Write a brief summary to
`<run-dir>/00-architecture.md`; it anchors every later phase.

## Phase 1 — Attack surface mapping

Map the attack surface: the tech stack/frameworks/languages; entry points (HTTP handlers, CLI
parsers, message/queue consumers, file processors, webhook handlers); trust boundaries (auth
middleware, authorization checks, input-validation layers); high-risk patterns (subprocess
calls, eval/exec, deserialization, file I/O on user-controlled paths, raw SQL construction,
template rendering); and sanitization patterns already in use. Write `<run-dir>/01-attack-surface.md`
with an entry-points table (location, input source, trust level), a trust-boundary map, and a
high-risk-pattern inventory with file paths.

## Phase 2 — Component decomposition

Divide the codebase into **4–8 logical components** at natural security boundaries (e.g. API
layer, auth system, file processing, database access, background jobs, admin interface). For
each, record a slug, a one-sentence description, and the file globs that belong to it. Write
the plan to `<run-dir>/02-plan.md` as a checklist:

```
## Components

- [ ] api-layer — HTTP request handlers and routing (src/api/**)
- [ ] auth — Authentication and session management (src/auth/**)
- [ ] db — Database models and query construction (src/models/**)
- [ ] file-processing — User-uploaded file handling (src/files/**)
```

**Every file in the audited tree lands in exactly one component.** Check the globs against the
tree once the list is drafted. A file no glob matches gets its own component, or goes under a
`## Not reviewed` heading in `02-plan.md` with the reason and again in the final report. A
decomposition that silently drops a directory turns "no findings" into a statement about code
nobody read.

## Phase 3 — Per-component sub-reviews

Read `<run-dir>/01-attack-surface.md` into context — you embed it in every component review.
Each component gets the **same inputs and the same output**; only *who* runs it differs.

**Each component review receives:** the component name and its file globs; the full
attack-surface document; the Security Categories and False Positive Filtering rules from
`SKILL.md`; the finding format from `SKILL.md`; and — for a dispatched sub-agent — the
instruction NOT to write files (return findings as text only).

**Sequential, single-agent (default, always works).** Walk the components in
`<run-dir>/02-plan.md` one at a time. For each: load the inputs above, review **only** that
component's files against the categories, write its findings to
`<run-dir>/03-<component-slug>.md`, then flip its checkbox to `[x]` in `02-plan.md`. A single
in-context agent cannot purge its own context between components, so treat those persisted
`03-<component-slug>.md` files — not your working memory — as the source of truth for
Phases 4 and 5 rather than assuming each component's context stays isolated. This is the
portable path — it keeps full coverage and only forgoes the
wall-clock win of parallelism.

**Parallel accelerator (where sub-agents exist).** Instead of the loop, dispatch one sub-agent
per component concurrently, each with the inputs above; as each returns, write its
`<run-dir>/03-<component-slug>.md` and flip its checkbox.

> **Claude adapter:** Launch all component sub-agents in a single message (one Agent tool call
> per component, all in the same response). Run them as foreground agents so you receive all
> results before Phase 4. Do not use `run_in_background: true`.

> **Codex adapter:** Dispatch each component review as `codex exec -s read-only -c
> approval_policy="never" --skip-git-repo-check -C <dir> "<prompt>"`. The read-only sandbox is
> what keeps the no-files promise; asking a reviewer not to write does not. Where a runtime
> cannot bound a reader that way, the orchestrator writes every file and the reader returns
> text only.

When every component checkbox in `02-plan.md` is `[x]`, proceed to Phase 4.

## Phase 4 — Cross-component data-flow analysis

Performed by the orchestrator, not the component reviewers (each component review is scoped to
one component and cannot see cross-boundary flows). This is deep mode's distinctive value.
Review all component findings, then do targeted reads at component interfaces — the points
where data crosses from one component into another — and ask:

1. Does untrusted input from an entry point flow through multiple components before reaching a
   sensitive sink?
2. Are there privilege-escalation paths that span components (e.g. low-privilege data enters
   the auth component and is used to make an authorization decision)?
3. Are there IDOR or access-control gaps visible only when two components are considered
   together?
4. Is data sanitized in one component but then passed unsanitized to a second component that
   re-uses it in a dangerous context?

Write any new findings to `<run-dir>/04-cross-component.md`, applying the same False Positive
Filtering and confidence threshold (≥ 0.8) from `SKILL.md`.

## Phase 5 — Synthesis & final report

1. Collect all findings from the Phase 3 component files and the Phase 4 cross-component file.
2. Deduplicate — merge findings flagged by more than one component into a single finding.
3. Apply a final false-positive pass.
4. Sort by severity (HIGH → MEDIUM). LOW is not reported — see the severity
   guidelines in `SKILL.md`.
5. Write `<run-dir>/REPORT.md` — the final deliverable — then print it to the user.

Final report structure:

```markdown
# Security Review: <Project Name>
Date: <YYYY-MM-DD>
Run directory: <the absolute path printed at setup>

## Summary
<total finding counts by severity>

## Findings

<findings sorted HIGH → MEDIUM, in the SKILL.md finding format>

## Reviewed Components
<list of components reviewed with file scope>
```
