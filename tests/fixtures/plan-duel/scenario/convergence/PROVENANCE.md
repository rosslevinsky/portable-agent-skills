# Provenance

Encodes the artifact contract spelled out in the skill's own `SKILL.md`, `round.md` and `summary.md`, which ship beside this file. These are **synthetic** fixtures, not a recorded run: plan bodies are deterministic filler (>=200 bytes, with a `# ` title), and judge `SCORE:`/`PREFERRED:` values are hand-chosen to drive one specific exit path. The engine snapshots/renames/scores exactly as SKILL.md + round.md + summary.md specify; the integration test asserts that contract.

Scenario: **convergence**. Judge scores 6, 7, 8; round 3 reaches score >= 8 with N >= 3, so v1's convergence exit fires. `PREFERRED: A` -> the controller (Claude) wins. `MISSED REJECTIONS: none`, so summary.md omits the Missed rejections section.
