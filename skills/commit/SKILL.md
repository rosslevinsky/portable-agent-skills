---
name: commit
description: >
  Stage the paths a change touched — named, never a `git add -A` sweep of the
  tree — then write a descriptive commit message based on a diff review and
  commit. Pushes only when the request asked to publish: "commit and push" or
  "push my changes" push; "stage and commit" or a bare /commit stop at the
  commit. Use when the user invokes /commit, or says any of those. Optional
  argument: a list of specific files/paths to stage instead.
---

# Commit and Push

## Overview

1. Assess the current state
2. Check for sensitive files
3. Stage changes
4. Write a commit message
5. Commit
6. Push to origin — **only when the request asked for it**

---

## Step 1 — Assess current state

Run these in parallel if supported, otherwise sequentially:

```bash
git status                        # see what's staged, unstaged, untracked
git diff HEAD                     # full diff of all changes
git log --oneline -5              # recent commits to match message style
git branch --show-current         # confirm current branch
```

In a repository with no commits yet, `git diff HEAD` and `git log` both fail: there is no
`HEAD`. Read `git status` and the files themselves instead, and skip style-matching —
there is no history to match.

If `git status` shows nothing to commit (clean working tree): there is nothing to stage,
but there may still be something to publish. If the request asked to push — "push my
changes" — go straight to **Step 6**, which pushes whatever commits the branch is
already ahead by. Otherwise stop and tell the user there is nothing to commit.

---

## Step 2 — Check for sensitive files

Before staging, scan for anything that looks sensitive. Scan **two** lists, not one: the
changed and untracked files in the working tree, **and whatever is already staged**.

```bash
git status --short                # working tree: changed and untracked
git diff --cached --name-only     # the index AS IT ALREADY IS, before this skill adds to it
```

The second list is the one that gets missed. `git add` adds to the index; it does not
replace it. Anything staged before this skill ran — by an earlier command, an interrupted
`git add -p`, an aborted commit — is already inside the commit about to be made, and naming
paths in Step 3 does not exclude it. So a `.env` staged ten minutes ago is committed by a
path-scoped `/commit` that never mentioned it, past a check that only ever read the working
tree.

Apply everything below to both lists:

- `.env`, `.env.*`, `*.env`
- `credentials*`, `secrets*`, `*_key.*`, `*_secret.*`
- `*.pem`, `*.p12`, `*.pfx`, `*.key`
- `id_rsa`, `id_ed25519` (private keys); a `*.pub` file is the public half — not
  itself secret and safe to stage, but treat it as a hint that its private
  counterpart may be nearby

Exception:
- `.secrets.baseline` is generally safe to commit (it is detector metadata, not secret values). Stage it unless the user explicitly asks not to.

If any other sensitive-looking files appear, **do not stage them**. Warn the user explicitly and list the files. Continue staging everything else.

For a match that is **already staged**, not staging it is not enough — it is in. Remove it
from the index and say so:

```bash
git restore --staged <path>       # older git: git reset HEAD <path>
```

**In a repository with no commits, both of those fail** — `fatal: could not resolve HEAD`,
because each names a commit to restore the index *from* and there is not one yet. That is
the same unborn repository this skill handles above, and a first commit is exactly where a
stray `.env` is most likely to be sitting. Use the form that needs no history:

```bash
git rm --cached <path>            # unstages without a HEAD; leaves the file on disk
```

---

## Step 3 — Stage changes

**If the user specified files/paths as an argument to /commit:**

```bash
git add <specified files>
```

**Then re-run Step 2's scan over `git diff --cached --name-only` and unstage any match.**
A directory argument expands to files Step 2 excluded: `/commit config/` where `config/.env`
is new stages the secret, and Step 2's "do not stage them" was decided before this `git add`
existed to undo it.

**Otherwise, stage the paths this change touched** — name them, rather than sweeping the
tree with `git add -A`. A working tree often carries unrelated work: a scratch file, a
half-finished edit elsewhere, output from a tool that ran earlier. A sweep takes all of
it, and leaves no trace that it did — the commit simply contains more than it should.

```bash
git add <the paths this change touched>
```

After staging, run `git diff --cached --stat` to confirm exactly what will be committed.
Compare it against the paths you meant to stage. Anything extra either was in the index
before you started (Step 2) — a path-scoped commit does **not** exclude it — or a directory
argument expanded to it. Do not attribute it to the first without checking: that reading is
what let a newly-created secret inside a named directory pass as pre-existing. Unstage it or
ask.

If there are untracked or unstaged files that seem unrelated to the main change, **do not
silently skip them and do not silently include them**. List them for the user and ask
whether they belong in this commit, a separate one, or neither. If operating autonomously
(no user available), leave them unstaged and say so in your summary — an unstaged file is
recoverable, an unwanted file inside a pushed commit is not.

---

## Step 4 — Write the commit message

Analyze `git diff HEAD` (or the cached diff if changes were already staged before calling this skill). Note that `git diff HEAD` omits the contents of files that are still untracked — inspect any remaining untracked files separately (`git status --short`, then read them) so the message accounts for them. Consider:

- **What changed** — which files, which systems/features
- **Why it changed** — infer from context, file names, and diff content
- **Scope** — is this a bug fix, new feature, refactor, rename, config change, dependency update, etc.

**Message format:**

```
<subject line — imperative, ≤72 chars, sentence case, no period>

<optional body — explain the why, not the what; wrap at 72 chars>

<optional Co-Authored-By trailer if the runtime provides a valid identity>
```

Include a `Co-Authored-By` trailer only when the runtime provides an explicit,
valid name and email identity for the current agent. If no such identity is
available, omit the trailer.

**Subject line rules:**
- Imperative mood: "Add", "Fix", "Rename", "Remove", "Update", not "Added", "Fixes", "Renaming"
- Sentence case: capitalize only the first word and proper nouns/acronyms
- No trailing period
- Specific, not generic: "Fix null check in auth middleware" not "Fix bug"
- If scope is obvious from a single file: "Fix off-by-one in pagination util"
- If scope spans a system: "Rename auth module and update all import paths"

**Body rules (omit if the subject fully explains the change):**
- Explain *why*, not *what* (the diff shows what)
- Wrap lines at 72 characters
- Use a blank line between subject and body

**Bad examples (do not write these):**
- "Update files"
- "Fix stuff"
- "WIP"
- "Changes"
- "Misc updates"

---

## Step 5 — Commit

The message is multi-line and will contain characters a shell wants to interpret, so
**never interpolate it into a quoted `-m` string.** Write it to a file with your
file-writing tool — not a shell redirect — and commit from that:

```bash
git commit -F <message-file>
```

This needs no shell quoting and works on every host. A POSIX shell can pipe a quoted
heredoc into `git commit -F -` instead, but heredocs do not exist in `cmd` or PowerShell,
so the message file is the one to reach for by default.

**Put that file under the OS temporary directory, never in the working tree** — `$TMPDIR`
or `/tmp` on POSIX, `%TEMP%` on Windows — with the process id in the name so two runs
cannot collide. A scratch file inside the repository is one an ignore rule does not cover
and a later `git add -A` sweeps into somebody's commit; this skill stages by name, but the
next command in the session may not. **Then delete it once `git commit` reports success**,
and say nothing about it: it is not an artifact, and a leftover message file is how the
*previous* commit's text ends up in the next one.

If the commit is rejected by a pre-commit hook, fix the underlying issue — do not use `--no-verify`.

---

## Step 6 — Push to origin

**Only when the request asked to publish.** "commit and push", "push my changes", "ship
this" — push. "stage and commit", "commit this", a bare `/commit` — stop after Step 5 and
say the commit is local. Publishing is not reversible the way a local commit is: an
unwanted commit is amended or reset, while a pushed one is on a remote other people and
other machines have already fetched. A request that named only committing has not asked
for that, and treating the two as one word takes the decision away from whoever made it.

Where nobody can be asked, the request itself decides — the rule above is not suspended by
the absence of a human. An instruction that already said to publish is the authorisation,
and having no one to re-confirm it with does not withdraw it: an autonomous run launched
with "commit and push" pushes. What must never happen is *resolving an ambiguity* by
pushing. Where the request named only committing, or is unclear, stop after Step 5, report
the commit and the branch, and let the next instruction decide.

Determine the current branch:

```bash
git branch --show-current
```

Push:

```bash
git push origin <current-branch>
```

If the push fails because the remote branch does not exist yet, use:

```bash
git push -u origin <current-branch>
```

**Never force-push** (`--force` / `-f`) unless the user has explicitly asked for it and acknowledged the risk.

If the push fails for any other reason (diverged history, permission error, etc.), report the error and the branch name to the user and stop. Do not attempt to resolve it automatically.

---

## After completion

Report:
- The commit hash and subject line (from `git log --oneline -1`)
- Which branch was pushed to, or that the commit is **local and unpushed**
- Any sensitive files that were skipped (if any)
