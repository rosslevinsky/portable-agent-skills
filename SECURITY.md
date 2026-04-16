# Security Policy

## Reporting a vulnerability

If you believe you've found a security issue in Portable Agent Skills — for
example, a bug in `install.sh` that could be abused to overwrite user files
outside the configured skills directory, or a validator bypass that lets
unsafe skill content slip through CI — please report it **privately** via
GitHub Security Advisories:

1. Go to [Security → Report a vulnerability](https://github.com/rosslevinsky/portable-agent-skills/security/advisories/new).
2. Describe the issue, affected versions (commit or tag), and reproduction
   steps.

We'll acknowledge your report within a reasonable window and keep you
updated as we investigate.

Please **do not** open a public issue for security problems — that makes
exploitation easier before a fix can ship.

## Scope

This repository contains:

- Markdown workflow documents (`SKILL.md`) — no runtime code, no network
  calls, no credentials handling.
- A Bash installer (`install.sh`) that writes into user-configurable skill
  directories.
- A Python validator (`scripts/validate_cross_runtime.py`) that reads
  skill files and tests fixtures.

Relevant concerns include:

- **Installer**: path traversal, symlink attacks on the install target,
  accidental overwrite of user data.
- **Validator**: crafted fixture files that cause uncontrolled recursion,
  resource exhaustion, or false negatives on banned phrases.
- **Skill content**: prompt-injection vectors embedded in a skill file
  that could mislead an agent into harmful actions.

Out of scope: reports about the Claude Code or Codex runtimes themselves
(report those to the respective runtime projects).

## Supported versions

The `main` branch and the most recent tagged release receive fixes. Older
releases are best-effort.
