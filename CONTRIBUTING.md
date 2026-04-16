# Contributing

Thank you for helping keep Portable Agent Skills working across runtimes. This
guide covers how to add a new skill, modify an existing one, and pass the
publication gate.

## Prerequisites

- Python 3.10+ (for the validator; CI uses 3.11)
- Bash (for the installer and installer tests)
- `git`

## Repository layout

```
portable-agent-skills/
├── skills/<skill-name>/SKILL.md     # One directory per skill
├── scripts/validate_cross_runtime.py
├── tests/                           # Validator fixtures + installer smoke tests
├── install.sh
├── README.md
├── PORTABILITY.md                   # Authoring contract (read this first)
└── .github/workflows/validate.yml   # CI
```

## Adding a new skill

1. Create the directory and `SKILL.md`:

   ```bash
   mkdir -p skills/<your-skill-name>
   $EDITOR skills/<your-skill-name>/SKILL.md
   ```

2. Use this minimal template:

   ```markdown
   ---
   name: <your-skill-name>
   description: >
     One-paragraph description of what this skill does and when to use it.
     Include trigger phrases ("Use when the user invokes /<your-skill-name>,
     or says 'X', 'Y', 'Z'").
   ---

   # <Human-Readable Title>

   ## Overview

   Explain the outcome of running this skill.

   ## Step 1 — <first action>

   Keep instructions runtime-neutral. Use verbs ("Search for files matching
   `pattern`", "Read the file"), not branded tool names.

   If this step requires user input, include an autonomous fallback:

   > Ask the user for X. If operating autonomously (no user available),
   > assume Y and note the assumption.

   ## Step 2 — ...
   ```

3. Pick a **classification** (see `PORTABILITY.md` for full definitions):

   - **Full** — works equivalently in both runtimes. No declaration needed.
   - **Degraded** — works in both, but one loses non-essential capability
     (e.g., parallelism). Declare near the top:

     ```markdown
     _Classification: Degraded — one-line explanation of what degrades._
     ```

   - **Runtime-limited** — cannot honestly provide equivalent behaviour in
     all runtimes. Must declare and must name a deterministic fallback skill
     for the limited runtime.

4. **Auto-discovery:** once `skills/<name>/SKILL.md` exists, both the
   installer and the validator pick it up automatically. No list edits are
   required.

## Modifying an existing skill

- Preserve the core workflow. Portability remediation is fine; workflow
  redesign belongs in a separate proposal.
- If you rename, remove, or replace a companion-skill reference (`cyw`, `tdd`,
  `plan-init`), run the validator — cross-reference checks will flag stale
  mentions elsewhere.

### Editing a skill from inside an agent session

The realistic workflow is asking Claude Code or Codex to edit its own skill.
For that round-trip to reach GitHub, the installed skill files must be
symlinks back to this repo — otherwise the agent edits a stranded copy.

- Install with `./install.sh --link` (or use a dotfiles setup that symlinks
  `skills/<name>` into `~/.claude/skills` and `~/.codex/skills`).
- Confirm with `readlink ~/.claude/skills/<name>` — the installed skill
  directory itself should be a symlink into this repo.
- Let the agent edit the skill during a session, then from this repo run the
  gate (`validate_cross_runtime.py`, `test_installer.sh`), commit, and push.
  `install.sh --verify` is useful only if you installed via `install.sh --link`;
  it reads an install manifest that the dotfiles-symlink path does not write.

If the installed file is a regular file (copy install), port the agent's edits
back to `skills/<name>/SKILL.md` manually before committing — the installed
copy is not tracked by git.

## Running the validator and tests locally

```bash
python scripts/validate_cross_runtime.py skills/            # Portability + genericity
python scripts/validate_cross_runtime.py --test-fixtures tests  # Fixture tests
bash tests/test_installer.sh                                # Installer smoke tests
./install.sh --verify                                       # Installed-pack health check
```

All four must pass before a PR is eligible to merge. CI runs the validator and
installer smoke tests on Ubuntu.

## Portability contract (the short version)

Read `PORTABILITY.md` for the full authoring contract. The most common reasons
a PR fails CI:

- Branded tool names used as normative instructions (`Use the Glob tool`,
  `subagent_type`, etc.) outside adapter notes
- User-prompting step without an autonomous fallback
- Companion-skill reference (`` `cyw` skill ``, `` `tdd` skill ``) without a
  nearby fallback instruction in the surrounding ~6-line window
- Degraded/Runtime-limited skill without a `_Classification:` declaration
- Stale Codex install path guidance such as `~/.agents/skills` instead of
  `$HOME/.codex/skills` (or `$CODEX_HOME/skills`)
- Private paths, project-specific identifiers, hardcoded model names, or
  vendor attribution emails leaking into skill text

## Writing test fixtures

Validator fixtures live in `tests/` as `.md` files:

- `test_validate_<scenario>.md` — one positive or negative fixture per check
- Positive fixtures must pass the specific check in isolation
- Negative fixtures must fail the specific check

After adding a fixture, register it in `run_test_fixtures()` in
`scripts/validate_cross_runtime.py` so the fixture harness runs it.

## Pull request checklist

- [ ] New or changed skill text passes the validator
- [ ] `tests/test_installer.sh` passes locally
- [ ] `./install.sh --verify` reports a clean install
- [ ] If you added or renamed a skill, cross-reference checks still pass
- [ ] CHANGELOG/README updated if user-visible behaviour changed
