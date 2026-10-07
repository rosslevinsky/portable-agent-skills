---
name: handoff
description: "Write a restart prompt for the current work to a file, so a fresh session can pick the work up after the context is cleared. Records the goal, what is done and what is next, the decisions already made, the traps found and the uncommitted work, then prints one line to paste after clearing. Use when the user invokes /handoff, or says 'give me a restart prompt so I can clear context', 'write a handoff', 'I want to clear context and continue'."
---

# Handoff

Write what a fresh session needs to continue this work into a file, and end with the one
line the user pastes after `/clear`.

_Classification: Degraded — where the runtime cannot write outside the workspace (a sandbox that forbids it), the prompt is printed in one fenced block instead of saved, and the user pastes that block._

## Step 1 — Choose the file

- `<project>` is the folder name of the repository root (`git rev-parse --show-toplevel`),
  or of the working directory outside a repository.
- The file is `~/.handoff/<project>.md`, where `~` is the user's home folder
  (`%USERPROFILE%` on Windows). Resolve it to an absolute path, and create the folder if it
  is missing.
- **Before every write**, read the first line of the candidate file if it exists. Overwrite
  it only when that line is exactly `Project: <this project's absolute path>`. Any other
  first line — another path, an empty line, anything unrecognized — means the file is not
  this project's: **never overwrite** it. Prefix the name with the next parent folder
  (`<parent>-<project>.md`, then `<grandparent>-<parent>-<project>.md`) and check again,
  until the name is free or this project's own. Stop at the filesystem root or a drive (a
  name never contains `/` or `C:`): if the parent folders run out first, write nothing and
  print the prompt as Step 3's fallback does.

## Step 2 — Write the prompt

The first line is exactly `Project: <absolute path of the project>`. Then write, for a
reader who has none of this conversation:

- **Written** — the date and time, the branch, and the last commit
  (`git log -1 --oneline`).
- **Goal** — what the work is for.
- **Done** — what is finished, and how it was verified.
- **Next** — the next concrete steps, in order.
- **Decisions already made, and why** — so the next session does not reopen them.
- **Traps found** — what failed or misled, and what to do instead.
- **Files to read first** — paths, including the project's `CLAUDE.md` / `AGENTS.md`
  where either exists. Describe a file by its path and what it is for; never restate its
  contents, which the next session reads for itself and a paraphrase gets wrong.
- **Uncommitted work** — `git status --short`, and which of those changes are this work's.
- **Open questions** for the user.
- A closing instruction: before acting on any of it, check the repository's current state
  (`git status`, `git log -1`), because the file may be older than the tree.

Be concrete: name files, commands and commit hashes. The next session can read the code;
it cannot read this conversation. Never write a secret, key, token, password or credential
into the file, which sits in the home folder as plain text: name where a credential is
kept, never its value.

Do not stage, commit or push anything. The file lives outside the repository.

## Step 3 — Report

When the file was written, end with one short sentence telling the user to paste the next
line after `/clear`, then exactly this line, with the file's absolute path in it:

```text
Read <absolute path> and continue from it.
```

If the file cannot be written, say why in one sentence and print the whole prompt in one
fenced block instead. The user pastes that block after `/clear`.
