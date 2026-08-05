# Changelog

All notable changes to this project are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
Versions use [Calendar Versioning](https://calver.org/) in the form
`vYYYY.MM.MICRO` — e.g. `v2026.04.0` is the first release cut in April 2026.
A MICRO bump in the same month indicates a follow-up release; a new month
starts from `.0` again.

## [2026.08.0] - 2026-08-05

Three changes reached `main` after the initial release without ever being tagged or
written up. This entry documents them and gives that state a tag, so it stays
retrievable — see [Installing a previous
release](README.md#installing-a-previous-release). Nothing here is new work.

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

### Changed

- **The validator's private-path check now uses universally private shapes.** The check
  exists to stop workspace-specific names reaching a published skill, so a pattern that
  is private to one workspace rather than to any workspace does not belong in a file that
  ships — publishing the guard would publish the very thing it guards. Those names now
  live in a local list the validator reads when present and which is never committed.
  The CI step duplicating the check was removed: the validator already covers it, and a
  second copy of a rule is a second place for it to drift.

## [2026.04.0] - 2026-04-16

Initial release.

[2026.08.0]: https://github.com/rosslevinsky/portable-agent-skills/releases/tag/v2026.08.0
[2026.04.0]: https://github.com/rosslevinsky/portable-agent-skills/releases/tag/v2026.04.0
