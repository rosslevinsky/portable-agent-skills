---
name: commit
description: >
  Stage all changes (or specified files), write a descriptive commit message
  based on a diff review, commit, and push to origin. Use when the user
  invokes /commit, or says "stage and commit", "commit and push", "commit
  everything", "push my changes", or similar. Optional argument: a list of
  specific files/paths to stage instead of all changes.
---

# Commit and Push

## Overview

1. Assess the current state
2. Check for sensitive files
3. Stage changes
4. Write a commit message
5. Commit
6. Push to origin

---

## Step 1 — Assess current state

Run these in parallel if supported, otherwise sequentially:

```bash
git status                        # see what's staged, unstaged, untracked
git diff HEAD                     # full diff of all changes
git log --oneline -5              # recent commits to match message style
git branch --show-current         # confirm current branch
```

If `git status` shows nothing to commit (clean working tree), stop and tell the user there is nothing to commit.

---

## Step 2 — Check for sensitive files

Before staging, scan the list of changed/untracked files for anything that looks sensitive:

- `.env`, `.env.*`, `*.env`
- `credentials*`, `secrets*`, `*_key.*`, `*_secret.*`
- `*.pem`, `*.p12`, `*.pfx`, `*.key`
- `id_rsa`, `id_ed25519`, `*.pub` (private key counterparts)

Exception:
- `.secrets.baseline` is generally safe to commit (it is detector metadata, not secret values). Stage it unless the user explicitly asks not to.

If any other sensitive-looking files appear, **do not stage them**. Warn the user explicitly and list the files. Continue staging everything else.

---

## Step 3 — Stage changes

**If the user specified files/paths as an argument to /commit:**

```bash
git add <specified files>
```

**Otherwise, stage all changes:**

```bash
git add -A
```

After staging, run `git diff --cached --stat` to confirm exactly what will be committed.

If there are untracked or unstaged files that seem unrelated to the main
change, **do not silently skip them**. Stage everything with `git add -A`,
then note the unrelated files in the commit message or ask the user if they
want a separate commit. Never drop files without telling the user.

---

## Step 4 — Write the commit message

Analyze `git diff HEAD` (or the cached diff if changes were already staged before calling this skill). Consider:

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

```bash
git commit -m "$(cat <<'EOF'
<subject>

<body if needed>

<optional Co-Authored-By trailer>
EOF
)"
```

Always use a HEREDOC to avoid quoting issues. If the commit is rejected by a pre-commit hook, fix the underlying issue — do not use `--no-verify`.

---

## Step 6 — Push to origin

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
- Which branch was pushed to
- Any sensitive files that were skipped (if any)
