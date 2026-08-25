# tests/

Several coexisting test suites live here, covering the pack's halves and hosts. No
count is given on purpose — a fixed number in prose goes stale the first time a suite
is added, and each suite below is discoverable by its own glob.

- **`test_validate_*.md`** — fixture files that exercise the rules in
  `scripts/validate_cross_runtime.py`
- **Python suites** — every `test_*.py` below. `test_plan_duel_engine.py` also runs
  stub-CLI scenarios from `fixtures/plan-duel/`

The table is kept honest by `test_skill_content.py`, which fails if a suite exists on
disk and is not named here. It named only a handful of them for a long time, which is how a
reader learned what this directory contains by running it instead of reading this.

| Suite | What it asserts |
| --- | --- |
| `test_decisions_ledger.py` | A `DECISIONS.md` holds rationale that has LEFT the runtime path, not a second copy of it |
| `test_encoding_hygiene.py` | Every text file read or written by shipped Python names its encoding |
| `test_extract_frames.py` | `extract-frames.sh` must not report success over a caller's bad argument |
| `test_install_py.py` | The single-file Python installer: it installs what it says, removes only what it recorded, records ownership before it touches a file, and reports an install that does not match the pack |
| `test_plan_duel_engine.py` | Deterministic unit tests for the stdlib-only plan-duel engine (Phase 1) |
| `test_plan_tracker.py` | The plan tracker check: one isolated case per rule of `execution.md`, ported out of the validator's fixture harness so the rules run on Windows |
| `test_plugin_manifest.py` | `plugin.json` against Agent Plugins 1.0.0: the canonical `$schema`, the name's character rules, and — the one with teeth — that its `version` equals the CHANGELOG's, so the pack cannot advertise a version it is not |
| `test_review_runner.py` | Unit tests for the stdlib-only diff-review supervisor, ``review_runner.py`` |
| `test_shipped_schemas.py` | Contract tests for the structured-output schemas the skills ship |
| `test_skill_budgets.py` | A recorded word budget per skill, held equal to what that skill measures — a ceiling against growth and a floor against slack |
| `test_skill_content.py` | Assertions about what the shipped skill text tells a runtime to *do* |
| `test_skill_traversal.py` | One answer to "which files belong to a skill" — the budget and the prose rules must name the same set |

They all run in CI (`.github/workflows/validate.yml`), on Ubuntu, macOS and Windows
alike — one job each, the same suites. There is no PowerShell suite and no
two-interpreter Windows matrix, because there is no PowerShell left: `install.ps1`,
`install.Tests.ps1` and `ci-windows.ps1` went with the shell installers, and one
Python installer needs no parity harness. Every suite here is picked up by the
existing `python -m unittest discover -s tests -p 'test_*.py'` step, so adding one
needs no workflow change.

## Running locally

```bash
python3 scripts/validate_cross_runtime.py skills/            # lint the pack
python3 scripts/validate_cross_runtime.py --test-fixtures tests  # test the linter
python3 -m unittest discover -s tests -p 'test_*.py'  # every test_*.py suite in this directory
```

All suites must pass before a PR is eligible to merge.

---


## `test_install_py.py`

Runs `install.py` against temporary directories — it **never** touches a real
`~/.claude` or `~/.agents`. Organised around the promises the installer makes rather
than its functions.

| Group | What it confirms |
|---|---|
| Installs what it says | Every discovered skill lands with its subdirectories; a directory without a `SKILL.md` is not a skill; the manifest lists what landed and names the version |
| Owns only what it recorded | A user's own skill survives uninstall; only listed names are removed; with no manifest, nothing is touched |
| Ownership is recorded first | A manifest that cannot be written stops the run before anything is copied; a failed copy leaves the skill owned, so a re-run repairs it rather than being refused |
| A name that could inject an entry is refused | The manifest is line-oriented, so a newline, a leading `#`, a Windows-reserved name or an NTFS `:` stream never reaches it |
| Verify establishes what it claims | A retired skill, a skill not yet installed, and a half-copied one are each a non-zero exit, not a remark |
| Links are unlinked, never followed | Removing a linked skill leaves its target alone — the hazard that cost `install.ps1` a hand-written `Remove-SkillPath` |
| Reads a legacy install | A manifest written by the shell installers is understood, updatable and removable |
| The version travels | Read from `CHANGELOG.md`, `[Unreleased]` skipped, `unknown` as the honest last resort — verified from a copy with no git at all |
| Both runtimes | The defaults name one directory per runtime, each with its own manifest |
| Three gaps the old suite named | A manifest line cannot reach outside the target; a retired skill is pruned from disk, not only from the manifest; an unowned skill is not replaced without `--force` |

The last group exists because those three behaviours were covered by the bash suite,
dropped by the first draft of `install.py`, and found by **reading** the suite being
deleted rather than deleting it. All three were reproduced before they were fixed.

`MutationProofs` is the reason to trust the rest. These tests could not be written red
first — the code was new — so the load-bearing ones were checked by breaking the
implementation and confirming the right test noticed.

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
| `test_validate_gate_skill_no_fallback.md` | **Fails** — references the `web-verify` / `diff-review` gate skills without a nearby fallback |
| `test_validate_with_fallback.md` | **Passes** — same reference, fallback present |
| `test_validate_private_paths.md` | **Fails** — contains absolute home-directory paths, project names, etc. |
| `test_validate_hardcoded_attribution.md` | **Fails** — hardcoded vendor co-author email |
| `test_validate_stale_runtime_claim.md` | **Fails** — outdated "Codex cannot…" wording |
| `test_validate_codex_skill_path.md` | **Fails** — points at `~/.codex/skills` instead of the documented `$HOME/.agents/skills` |
| `test_validate_unknown_skill_reference.md` | **Fails** — references a skill name that isn't in `skills/` |
| `test_validate_plan_duel_relative_prompt.md` | **Fails** — workdir-relative companion skill path like `../plan-init/SKILL.md` |
| `test_validate_readme_inventory.md` | **Fails** — a README-style skill-inventory table that lists a retired skill, omits a real one, and hardcodes a stale skill count |
| `test_validate_readme_inventory_clean.md` | **Passes** — the same table in sync with the skills listing, count matching |
| `test_validate_progress_observable.md` | **Passes** — a sub-agent-dispatching skill declaring a valid `_Progress: observable` posture |
| `test_validate_progress_bounded.md` | **Passes** — the other valid posture, `_Progress: bounded` |
| `test_validate_progress_missing.md` | **Fails** — a dispatcher skill with no `_Progress:` line |
| `test_validate_progress_invalid.md` | **Fails** — a `_Progress:` line with an unknown posture value |
| `test_validate_spawn_permission.md` | **Fails** — three ways: a shell `codex exec` and a JSON one, each with arguments but no sandbox mode, plus a sandbox named without its approval-policy pin |
| `test_validate_spawn_permission_clean.md` | **Passes** — explicit sandbox modes on every invocation, alongside a bare `codex exec` prose mention that is correctly not treated as one |
| `test_validate_skill_root_doc_ref.md` | **Fails** — skill text references a repo-root doc (`PORTABILITY.md`) that is not installed with the skill |
| `test_validate_skill_selfcontained_clean.md` | **Passes** — a `references/` doc whose links stay inside its own skill (single-`../` allowance, README/CHANGELOG exclusion) |
| `test_validate_skill_sibling_path.md` | **Fails** — a repo-rooted `skills/<other>/…` path, the self-containment defect in the spelling the `../` escape rule cannot see. Carries no dot-dot, so only the sibling rule can reject it |
| `test_validate_skill_references_escape.md` | **Fails** — the `references/`-level escape threshold: one `../` is allowed there, two and three are not. Asserted on the reported level count, since at depth 0 the same file is rejected for a different reason |
| `test_validate_unterminated_fence.md` | **Fails** — a code fence that is never closed, so every line below it reads as code and a `_Classification:` there is quoted rather than declared |
| `test_validate_unterminated_fence_midfile.md` | **Fails** — three fences with the *first* one unterminated. The scan re-pairs past a missing closer and ends clean, so this case is caught by delimiter parity rather than by the state machine; the single-fence fixture above could never exercise it |
| `test_validate_v1_routing_unqualified_self.md` | **Fails** — a v1 skill body referring to its own suite unqualified (`everything after /plan-init`); intra-suite references, self-references included, must be `-v1`-qualified |
| `test_validate_v1_routing_qualified_redirect.md` | **Fails** — the opposite direction: a `Format: v2` refusal guard whose forward redirect has been `-v1`-qualified, when canonical means v2 |
| `test_validate_v1_routing_clean.md` | **Passes** — both directions right in one file: `-v1`-qualified intra-suite references, unqualified forward redirect |
| `test_validate_repeated_reference_fallback.md` | a companion's fallback stated once, then referenced repeatedly - the relaxed rule must accept it |


The **checkbox execution tracker** is covered by a table of temp-*directory* cases in
`run_test_fixtures()` rather than by committed fixtures. Two of its rules are about
the directory — a checkbox's link must resolve to a real phase document, and every
phase document must be listed — which a single flat `.md` file cannot express. Each
case violates exactly one rule and asserts a finding count of **1** (a fixture tripping
two rules proves neither), and acceptance cases sit alongside them so a rule that
simply rejected everything could not satisfy the suite.

Three of the cases are about scope rather than shape, and they are the ones that matter
most. A **directory scan inspects only `execution.md`** — proven by a sibling v1
`phases.md` with deliberately dangling links, which would fail loudly if the scan ever
widened. A **superseded `- phase:` tracker is printed by path and skipped**, not
silently ignored — asserted through `run_tracker_check`'s captured output, because
"reported" is the whole point. And a file carrying **both** shapes is an error, not a
legacy record: "zero boxes is a hard error" and "no boxes means legacy" otherwise
describe the same file, so a half-migrated tracker would be waved through.

**Coverage here is proven by mutation, not by a green run.** Every branch of
`check_tracker` and `run_tracker_check` has been individually disabled and the suite
confirmed to go red — eleven mutants, each killed by a named case. Six branches of the
superseded grammar were found silently untested this way, after the suite had reported
green for months. A new rule is not covered until its case has killed its own mutant.

> **Run mutants with `PYTHONDONTWRITEBYTECODE=1`, or a green result may be a lie.**
> Python validates a cached `.pyc` against the source's **mtime and size**, and the usual
> mutant changes neither: flipping `==` to `!=` keeps the byte count identical, and
> restoring the file with `cp` in the same second keeps the timestamp. Measured: a
> restored module went on running the mutant, so a test written against the real source
> failed while the source was correct, and the readings taken that way had to be redone. `find . -name __pycache__ -prune -exec rm -rf
> {} +` clears it after the fact; the environment variable prevents it.

Several additional fixture checks don't need `.md` files — they build temp
skills trees in-memory: the discovery rules
(`discover_degraded_or_limited`, `discover_skill_artifacts`, dispatcher
discovery), the plan-duel companion/engine requirements, the
engine-portability and file-kind gating rules, the content-hygiene
sweep (non-artifact assets, mixed-encoding files, `__pycache__` exclusion),
the frontmatter-`name:`-equals-directory rule, and `-v1` description
disjointness (shared opening sentence and missing choose-me condition are
exercised separately, so neither can mask the other).

## Why two styles coexist

- The `.md` fixtures are the cheap way to add a new validator rule: write
  one failing file, register it in `run_test_fixtures()`, done.
- The `test_*.py` suites cover everything a document cannot state: install
  behaviour end-to-end (file modes, manifest writes, the ownership-before-copy
  ordering) and the engines the skills bundle.

There used to be a third style — a `.sh` suite driving `install.sh`, and a
Pester suite driving `install.ps1`. Both are gone with the shell installers.
One Python implementation has no parity to test, which is most of why it is
one implementation.

## Adding a new fixture

1. Create `tests/test_validate_<scenario>.md`. Positive fixtures must pass
   the specific check in isolation; negative fixtures must fail it.
2. Register it in `run_test_fixtures()` in
   `scripts/validate_cross_runtime.py`.
3. Re-run `python3 scripts/validate_cross_runtime.py --test-fixtures tests`
   to confirm the fixture behaves as intended.
