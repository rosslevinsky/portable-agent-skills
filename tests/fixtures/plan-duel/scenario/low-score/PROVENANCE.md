# Provenance

Encodes the artifact contract of the superseded v1 duel workflow — the behavioural golden these fixtures hold the engine to. That contract is not a tag you have to fetch: it is spelled out in the skill's own `SKILL.md`, `round.md` and `summary.md`, which ship beside this file. These are **synthetic** fixtures, not a recorded v1 run: plan bodies are deterministic filler (>=200 bytes, with a `# ` title), and judge `SCORE:`/`PREFERRED:` values are hand-chosen to drive one specific v1 exit path. The engine snapshots/renames/scores exactly as v1's SKILL.md + round.md + summary.md specify; the integration test asserts that contract.

Scenario: **low/unparseable-score**. Round 1's judge file is NON-empty but has no parseable `SCORE:` line, so v1 treats it as 0 and prints `Warning: could not parse score at round 1 — treating as 0`. Rounds 2, 3 score 5, 8; round 3 converges. This exercises watch-item (c): a non-empty judge output with no score is a warning + 0, NOT a halt.
