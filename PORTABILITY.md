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

Refer to companion skills by name, not by slash-command syntax alone:

| Instead of | Write |
|---|---|
| "Run `/cyw`" (as the only path) | "Run the `cyw` skill. If direct skill invocation is unavailable, perform the equivalent review manually: re-read modified files, check correctness/completeness/consistency, and fix any issues found." |
| "Use `/tdd` if helpful" | "Use the `tdd` skill if available. Otherwise, follow TDD discipline manually: write failing tests first, then implement." |

Every companion-skill reference must include a deterministic fallback that
describes what to do if the skill cannot be invoked.

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

---

## Genericity Requirements

Published skills must not contain private paths, project-specific references, or
user identity information. The following patterns are rejected by the validator:

- Absolute home directory paths (e.g., `/home/<user>`)
- Private repository paths (e.g., `dotfiles/claude/skills`, `~/projects/gh/main`)
- Project-specific identifiers (e.g., project names, framework-specific config
  files, or framework-scoped package names)

Use generic language instead: "check `package.json` or project config for sibling
repos" rather than naming specific frameworks or projects.

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

## Verifying a skill pack

Skills classified as **Degraded** or **Runtime-limited** declare `_Classification:`
at the top of their `SKILL.md`. Run `python scripts/validate_cross_runtime.py
skills/` to verify every rule in this document against the pack.
