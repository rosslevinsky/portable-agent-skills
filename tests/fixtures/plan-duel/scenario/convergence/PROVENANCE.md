# Provenance

Encodes the artifact contract of the superseded v1 duel workflow — the behavioural golden these fixtures hold the engine to. That contract is not a tag you have to fetch: it is spelled out in the skill's own `SKILL.md`, `round.md` and `summary.md`, which ship beside this file. These are **synthetic** fixtures, not a recorded v1 run: plan bodies are deterministic filler (>=200 bytes, with a `# ` title), and judge `SCORE:`/`PREFERRED:` values are hand-chosen to drive one specific v1 exit path. The engine snapshots/renames/scores exactly as v1's SKILL.md + round.md + summary.md specify; the integration test asserts that contract.

Scenario: **convergence**. Judge scores 6, 7, 8; round 3 reaches score >= 8 with N >= 3, so v1's convergence exit fires. `PREFERRED: A` -> the controller (Claude) wins. `MISSED REJECTIONS: none`, so summary.md omits the Missed rejections section.
