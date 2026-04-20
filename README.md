# Portable Agent Skills

[![Validate](https://github.com/rosslevinsky/portable-agent-skills/actions/workflows/validate.yml/badge.svg)](https://github.com/rosslevinsky/portable-agent-skills/actions/workflows/validate.yml)
[![License: Apache 2.0](https://img.shields.io/badge/License-Apache%202.0-blue.svg)](LICENSE)

A collection of portable, cross-runtime agent skills for [Claude Code](https://docs.anthropic.com/en/docs/agents-and-tools/claude-code/overview) and [Codex CLI](https://github.com/openai/codex). Each skill is a standalone workflow document (`SKILL.md`) that both runtimes can execute with equivalent outcomes.

## What is this?

AI coding agents benefit from reusable, well-shaped workflows — "write a failing test first, then implement," "audit this codebase for security issues," "break a large task into committable phases." This repository packages those workflows as plain-markdown `SKILL.md` files that both **Claude Code** (Anthropic) and **Codex CLI** (OpenAI) can invoke, with equivalent behaviour guaranteed by an automated portability contract.

One install command drops 11 skills into the right places for both runtimes. CI enforces a contract that forbids runtime-specific tool names, requires autonomous fallbacks, and flags private paths before they ship. Edit a skill in a symlinked install and the edit lands back in this repo, ready to commit.

## How to use these skills

The skills in this pack compose into a simple end-to-end workflow. If you only read one section of this README, read this one.

### `/cyw` — check your work, any time, anywhere

`/cyw` runs a structured critical-review → fix → verify loop over whatever you just did. It is the single most useful skill in this pack. Reach for it after *any* non-trivial change — a bug fix, a refactor, a plan, a migration script, a commit message. It does not require a plan or phase structure; it just reviews the recent turn.

**Run it more than once.** The skill already loops internally (up to 3 passes, stopping early when a pass finds zero issues), but in practice a *fresh* `/cyw` invocation — started as a separate call, not another pass inside the same one — often surfaces things the internal loop missed. The reason is not obvious, but the effect is real: treat a second or third fresh `/cyw` as cheap insurance on anything important.

### The standard planning cycle

For work bigger than a one-shot edit, the intended flow is:

```
/plan-init  →  /plan-phase  →  /plan-run
```

- **`/plan-init <task>`** — interviews you, explores the codebase, and writes `plans/<slug>/plan.md`: goal, checkable success criteria, constraints, non-goals, affected files. It does *not* break the work into phases.
- **`/plan-phase <path>`** — reads the plan, proposes a phase structure for your approval, then writes one phase document per phase plus a `phases.md` execution tracker. Each phase is independently committable.
- **`/plan-run <path>`** — executes incomplete phases in order. Per phase: checks entry criteria, does the work, runs verification, runs `/cyw`, marks the phase done, reviews the diff, commits, pushes. Safe to restart — already-completed phases are skipped.

Insert `/cyw` freely between stages. Common spots: after `/plan-init` (sanity-check the plan before breaking it down), after `/plan-phase` (sanity-check the breakdown before executing), and after `/plan-run` finishes (final sweep). `/plan-run` already calls `/cyw` at the end of each phase, but a fresh invocation at the end of the whole plan often finds more.

### Variants

- **`/plan-duel <task>`** — swap in for `/plan-init` if you have both Claude and Codex subscriptions. Claude and Codex each write a plan, then iteratively critique and refine against each other until a judge scores them as converged (or ≥10 rounds). Produces a winning `plan.md` you can hand to `/plan-phase` as usual. Requires the `codex` CLI on `PATH`.
- **`/plan-and-do <task>`** — lightweight single-pass alternative for small jobs (≤~8 checklist items, one commit). Interviews, plans briefly, executes, runs `/cyw`, commits. If scope grows during execution, it will tell you to switch to the full `/plan-init` cycle.

### Quick decision guide

| Situation | Start with |
|---|---|
| Just finished any change and want a sanity check | `/cyw` (and don't hesitate to run it again) |
| Small, self-contained task, one commit | `/plan-and-do` |
| Non-trivial feature or refactor | `/plan-init` → `/plan-phase` → `/plan-run` |
| Same, but you want Claude + Codex to sharpen the plan | `/plan-duel` → `/plan-phase` → `/plan-run` |

## Skill Inventory

| Skill | Description | Classification |
|---|---|---|
| `commit` | Stage, commit, and push with structured messages | Full |
| `cyw` | Multi-pass "check your work" review loop | Full |
| `extract-hooks` | Extract non-UI logic from React components into custom hooks | Full |
| `tdd` | Test-driven development workflow (red/green/refactor) | Full |
| `plan-init` | Create a structured project plan document | Full |
| `plan-phase` | Break a plan into ordered execution phases | Full |
| `plan-run` | Execute a phased plan | Full |
| `plan-and-do` | Lightweight plan + execute in one pass | Full |
| `plan-duel` | Iterative plan refinement between two agents | Runtime-limited |
| `security-review-codebase` | Full sequential codebase security audit | Degraded |
| `security-review-codebase-hierarchical` | Hierarchical multi-agent security review | Runtime-limited |

**Classifications:**
- **Full** — Works in both runtimes with equivalent outcomes
- **Degraded** — Works in both runtimes, but one may lose non-essential capabilities (e.g., parallelism)
- **Runtime-limited** — Cannot honestly provide equivalent behavior in all runtimes; limitations are declared in the skill file

## Installation

```bash
git clone https://github.com/rosslevinsky/portable-agent-skills.git
cd portable-agent-skills
./install.sh
```

This copies all skills to `~/.claude/skills/` (Claude Code) and
`$HOME/.codex/skills/` (Codex CLI's default user skills directory —
`$CODEX_HOME/skills` if `CODEX_HOME` is set).

### Custom install locations

```bash
CLAUDE_SKILLS_DIR=/path/to/claude/skills CODEX_SKILLS_DIR=/path/to/codex/skills ./install.sh
```

## Update

```bash
cd portable-agent-skills
git pull
./install.sh --update
```

## Uninstall

```bash
./install.sh --uninstall
```

Only removes skills installed by this pack (tracked via an ownership manifest). User-created skills with the same directory names are left untouched.

## Development Setup

For local development, use `--link` to symlink skills instead of copying. Edits to `skills/` are immediately reflected in the runtime directories, and — just as importantly — edits the agent makes to its installed skill files land directly in this repo:

```bash
./install.sh --link
```

Each installed skill becomes a **dir-level symlink** into this repo
(e.g. `~/.claude/skills/commit → <repo>/skills/commit/`). Both Claude
Code and Codex read this shape correctly. One consequence: edits and
`rm`s *inside* an installed skill dir resolve through the symlink and
affect this repo's working tree. Recoverable via git, but worth knowing.

### Editing skills from inside an agent session

The common case: you're running Claude Code or Codex and ask it to modify one of its own skills. Where the edit lands depends on how skills are installed.

- **Symlink install** (`./install.sh --link`, or the symlink-based dotfiles setup): the installed path *is* the repo file. The agent edits `skills/<name>/SKILL.md` in this repo directly.
- **Copy install** (default `./install.sh`): the agent edits a detached copy under `~/.claude/skills/` or `~/.codex/skills/`. Those edits do not flow back to git.

Recommended workflow for skill authors who iterate via an agent:

1. Install with `./install.sh --link` once (or use a dotfiles setup that symlinks from this repo).
2. Let the agent edit the skill during a normal session.
3. In this repo, run the gate:
   ```bash
   python scripts/validate_cross_runtime.py skills/
   bash tests/test_installer.sh
   ```
   If you installed via `./install.sh --link` (not via dotfiles symlinks),
   also run `./install.sh --verify` to confirm the installed pack matches
   source. Dotfiles-symlink users can skip `--verify` — it relies on an
   install manifest that only `install.sh` writes.
4. Commit, push, open a PR. CI re-runs the validator and installer tests.

If you are on a copy install and the agent edited the installed file directly (either `~/.claude/skills/<name>/SKILL.md` for Claude Code or `~/.codex/skills/<name>/SKILL.md` for Codex), diff that file against `skills/<name>/SKILL.md` here and port the changes over before committing — the installed copy is not tracked by git.

### Dry run

Preview what the installer would do without making changes:

```bash
./install.sh --dry-run
```

### Existing same-name skills

The installer will not overwrite an existing same-name skill unless that skill
was previously installed by this pack and is listed in the ownership manifest.
If you intentionally want to replace an existing skill, pass `--force`.

### Consuming from another repo

If you maintain another repository for machine setup, keep this skill pack as
the single source of truth and point that setup at this checkout. For editable
development, use `./install.sh --link` so runtime skill directories contain
symlinks back to this repository.

```bash
./install.sh --link
```

## Validation

Run the portability validator and test suite:

```bash
python scripts/validate_cross_runtime.py skills/          # Check all skills
python scripts/validate_cross_runtime.py --test-fixtures tests  # Run fixture tests
bash tests/test_installer.sh                               # Installer smoke tests
```

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for a complete walkthrough (adding a new
skill, template, classification guidance, fixture authoring, PR checklist).

Quick version:

1. Fork and clone the repository
2. Create a feature branch
3. Make your changes in `skills/<skill-name>/SKILL.md`
4. Run `python scripts/validate_cross_runtime.py skills/` — must pass with zero errors
5. Run `python scripts/validate_cross_runtime.py --test-fixtures tests` — all fixtures must pass
6. Run `bash tests/test_installer.sh` — installer tests must pass
7. Open a pull request — CI runs all checks automatically

### Portability expectations

All skills must follow the [PORTABILITY.md](PORTABILITY.md) contract:

- Use runtime-neutral instruction vocabulary (no branded tool names)
- Include deterministic fallbacks for companion-skill references
- Include autonomous fallbacks for user-prompting steps
- Reference both `CLAUDE.md` and `AGENTS.md` for project instruction files
- Declare classification for Degraded or Runtime-limited skills
- No private paths, project-specific references, or hardcoded model names

## License

[Apache License 2.0](LICENSE)
