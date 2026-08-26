# Provenance

Encodes the artifact contract of the superseded v1 duel workflow — the behavioural golden these fixtures hold the engine to. That contract is not a tag you have to fetch: it is spelled out in the skill's own `SKILL.md`, `round.md` and `summary.md`, which ship beside this file. These are **synthetic** fixtures, not a recorded v1 run: plan bodies are deterministic filler (>=200 bytes, with a `# ` title), and judge `SCORE:`/`PREFERRED:` values are hand-chosen to drive one specific v1 exit path. The engine snapshots/renames/scores exactly as v1's SKILL.md + round.md + summary.md specify; the integration test asserts that contract.

Focused seam (watch-item c): round-1's judge process produces NO output file, so capture_judge_message raises JudgeOutputError and the run halts (never crashes uncaught, never treats an empty judge as valid). Distinct from the low-score scenario, where a NON-empty judge with no SCORE is a warning + 0. Not one of the 7 golden scenarios.
