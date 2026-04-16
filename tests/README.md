# tests/

Two coexisting test suites live here, covering the two halves of the pack:

- **`test_installer.sh`** — end-to-end smoke tests for `install.sh`
- **`test_validate_*.md`** — fixture files that exercise the rules in
  `scripts/validate_cross_runtime.py`

Both suites run in CI (`.github/workflows/validate.yml`).

## Running locally

```bash
python scripts/validate_cross_runtime.py skills/            # lint the pack
python scripts/validate_cross_runtime.py --test-fixtures tests  # test the linter
bash tests/test_installer.sh                                # test the installer
```

All three must pass before a PR is eligible to merge.

---

## `test_installer.sh`

A single bash script (~96 assertions) that exercises `install.sh` against
temporary directories — it **never** touches a real `~/.claude` or
`~/.codex`. The `CLAUDE_SKILLS_DIR` / `CODEX_SKILLS_DIR` env vars redirect
installs into `mktemp -d` sandboxes.

| Section | What it confirms |
|---|---|
| Fresh install | All discovered skills land in both target dirs |
| plan-duel companion files | `init.md`, `round.md`, `summary.md` accompany `SKILL.md` |
| Manifest files | `.installed-by-portable-agent-skills` is written with one skill per line |
| Idempotency | A second `--update` produces an identical file list |
| Unowned-skill protection | Install refuses to overwrite a same-name skill it didn't create; preserves the user's version; `--force` overrides; partial conflicts roll back cleanly |
| Uninstall scoping | `--uninstall` removes only manifest-tracked dirs; custom user skills survive |
| Dry-run | Creates no directories, prints what would happen, defaults Codex target to `~/.codex/skills` |
| `--link` mode | Produces symlinks (needed for the agent-edit round-trip) |
| Custom env vars | Honours `CLAUDE_SKILLS_DIR` / `CODEX_SKILLS_DIR` |
| Manifest metadata | Records `source-commit:` and `installed-at:` headers |
| `--verify` | Passes on a clean install, fails on missing skills, fails when not installed, flags source skills not yet present in the manifest |
| `--help` | Mentions `--verify` and `--uninstall` |
| Auto-discovery | The discovered skill count matches what `skills/` actually contains |

## `test_validate_*.md` fixtures

Each markdown file in this directory is a deliberately-shaped input
designed to fire (or not fire) a single check in the validator. The
`--test-fixtures` mode reads them and confirms each behaves as labelled —
it's a test-the-linter harness.

Paired positive / negative fixtures:

| Fixture | Expected outcome |
|---|---|
| `test_validate_banned_phrases.md` | **Fails** — uses branded tool names (`Use the Glob tool`, `Use the Agent tool`) in normative prose |
| `test_validate_clean.md` | **Passes** — same ideas in portable phrasing; banned terms only appear inside an adapter blockquote |
| `test_validate_multiline_adapter.md` | **Passes** — confirms the validator tracks multi-line `>` adapter blocks and keeps exempting them across lines |
| `test_validate_missing_classification.md` | **Fails** — a Degraded/Runtime-limited skill with no `_Classification:` line |
| `test_validate_no_fallback.md` | **Fails** — references the `cyw` skill without a nearby "if unavailable..." line |
| `test_validate_with_fallback.md` | **Passes** — same reference, fallback present |
| `test_validate_private_paths.md` | **Fails** — contains absolute `/home/<user>` paths, project names, etc. |
| `test_validate_hardcoded_attribution.md` | **Fails** — hardcoded vendor co-author email |
| `test_validate_stale_runtime_claim.md` | **Fails** — outdated "Codex cannot…" wording |
| `test_validate_stale_codex_skill_path.md` | **Fails** — `~/.agents/skills` instead of `$HOME/.codex/skills` (or `$CODEX_HOME/skills`) |
| `test_validate_unknown_skill_reference.md` | **Fails** — references a skill name that isn't in `skills/` |
| `test_validate_plan_duel_relative_prompt.md` | **Fails** — workdir-relative companion skill path like `../plan-init/SKILL.md` |

Two additional fixture checks don't need files — they build a temp skills
tree in-memory and verify `discover_degraded_or_limited` and
`check_companion_files` (both exercise the plan-duel classification
rules).

## Why two styles coexist

- The `.md` fixtures are the cheap way to add a new validator rule: write
  one failing file, register it in `run_test_fixtures()`, done.
- The `.sh` suite covers install behaviours that only reveal themselves
  end-to-end (file perms, symlinks, manifest writes, preflight rollback).

## Adding a new fixture

1. Create `tests/test_validate_<scenario>.md`. Positive fixtures must pass
   the specific check in isolation; negative fixtures must fail it.
2. Register it in `run_test_fixtures()` in
   `scripts/validate_cross_runtime.py`.
3. Re-run `python scripts/validate_cross_runtime.py --test-fixtures tests`
   to confirm the fixture behaves as intended.
