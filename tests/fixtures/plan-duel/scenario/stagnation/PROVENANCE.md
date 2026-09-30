# Provenance

Encodes the artifact contract spelled out in the skill's own `SKILL.md`, `round.md` and `summary.md`, which ship beside this file. These are **synthetic** fixtures, not a recorded run: plan bodies are deterministic filler (>=200 bytes, with a `# ` title), and judge `SCORE:`/`PREFERRED:` values are hand-chosen to drive one specific exit path. The engine snapshots/renames/scores exactly as SKILL.md + round.md + summary.md specify; the integration test asserts that contract.

Scenario: **stagnation**. Judge scores 7, 5, 6, 6. At round 4 the best of the last 3 rounds (max(5,6,6)=6) does not exceed the prior peak (max(7)=7), so v1's stagnation exit fires. `PREFERRED: B` -> the participant (Codex) wins. `MISSED REJECTIONS` is non-`none`, so summary.md INCLUDES the Missed rejections section.
