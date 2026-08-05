# Changelog

All notable changes to this project are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
Versions use [Calendar Versioning](https://calver.org/) in the form
`vYYYY.MM.MICRO` — e.g. `v2026.04.0` is the first release cut in April 2026.
A MICRO bump in the same month indicates a follow-up release; a new month
starts from `.0` again.

## [2026.06.0] - 2026-06-01

Windows support. This release shipped at the time and was documented afterwards, so its
tag was created later than its date — the tag marks the month the release came out, not
the day it was written up. See [Installing a previous
release](README.md#installing-a-previous-release) to return to it.

### Added

- **A native Windows PowerShell installer.** `install.ps1` is a copy-only port of
  `install.sh` for Claude Code and Codex CLI running natively on Windows, outside WSL.
  It honours the same `CLAUDE_SKILLS_DIR` / `CODEX_SKILLS_DIR` overrides and writes a
  byte-identical (LF) ownership manifest, so the two installers are interchangeable.
  There is deliberately no `-Link` mode: symlinks need elevated privileges on Windows,
  so the linked development workflow stays on `install.sh --link` under WSL or Git Bash.
  `tests/install.Tests.ps1` is a Pester 5 suite mirroring `test_installer.sh`'s coverage.
- **A how-to-use guide in the README** — `/cyw` as the universal sanity check, the
  planning cycle, and a decision table mapping common situations to a starting skill.

## [2026.04.0] - 2026-04-16

Initial release.

[2026.06.0]: https://github.com/rosslevinsky/portable-agent-skills/releases/tag/v2026.06.0
[2026.04.0]: https://github.com/rosslevinsky/portable-agent-skills/releases/tag/v2026.04.0
