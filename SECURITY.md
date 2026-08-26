# Security Policy

## Reporting a vulnerability

If you believe you've found a security issue in Portable Agent Skills — for
example, a bug in `install.py` that could be abused to overwrite user files
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

- Markdown workflow documents (`SKILL.md`) and their `references/` —
  instructions, not runtime code; no credentials handling.
- One stdlib-only Python installer (`install.py`) that copies skill
  directories into user-configurable skill directories. It replaced the bash
  and PowerShell installers in `v2026.08.0`; earlier tags still ship those,
  so a report against `install.sh` or `install.ps1` is about a released tag
  rather than current `main`. The pack can also be installed by an
  [Agent Plugins](https://agent-plugins.org) 1.0.0 client reading
  `plugin.json`, which does not run `install.py` at all.
- Two stdlib-only Python programs that read paths you give them.
  `scripts/validate_cross_runtime.py` walks a skill tree and **refuses** any
  entry that resolves somewhere other than where it sits, rather than
  following it. `scripts/check_plan_tracker.py` reads an `execution.md`
  tracker and resolves the phase documents it links, requiring each to be a
  regular file in that same plan directory.
- Two stdlib-only Python programs that make no network calls and handle no
  credentials, but do launch another CLI as a child process:
  `skills/plan-duel/plan_duel.py` (the duel engine) and
  `skills/diff-review/review_runner.py` (the reviewer supervisor).
- One bash script, `skills/web-verify/references/extract-frames.sh`, which
  invokes `ffmpeg` over a video path you supply and writes into an output
  directory you supply.
- The project's own test suites and CI, which are published rather than kept
  back: twelve Python suites under `tests/`, two stub CLIs under
  `tests/fixtures/plan-duel/` that stand in for a real runtime, and
  `.github/workflows/validate.yml`. They are not part of an install and a
  user never runs them, but they are executable code in the published tree
  and reports against them are in scope.

Relevant concerns include:

- **Installer**: path traversal, symlink attacks on the install target,
  accidental overwrite of user data.
- **Validator and tracker checker**: crafted files that cause uncontrolled
  recursion, resource exhaustion, or false negatives on banned phrases; a
  link that escapes the refusal above and pulls an external tree into the
  scan.
- **Subprocess supervisors**: the argv handed to a spawned CLI, the sandbox and
  approval flags it is pinned to, and whether a hostile or malformed reply from
  that child can be made to escape its declared permissions.
- **Shell script**: an unquoted path or a hostile filename reaching
  `extract-frames.sh`'s `ffmpeg` invocation, or output written outside the
  directory it was given.
- **Skill content**: prompt-injection vectors embedded in a skill file
  that could mislead an agent into harmful actions.

Out of scope: reports about the Claude Code or Codex runtimes themselves
(report those to the respective runtime projects).

## Supported versions

The `main` branch and the most recent tagged release receive fixes. Older
releases are best-effort.
