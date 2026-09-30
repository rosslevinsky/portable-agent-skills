# Provenance

Encodes the artifact contract spelled out in the skill's own `SKILL.md`, `round.md` and `summary.md`, which ship beside this file. These are **synthetic** fixtures, not a recorded run: plan bodies are deterministic filler (>=200 bytes, with a `# ` title), and judge `SCORE:`/`PREFERRED:` values are hand-chosen to drive one specific exit path. The engine snapshots/renames/scores exactly as SKILL.md + round.md + summary.md specify; the integration test asserts that contract.

Focused seam: round-1's judge process produces NO output file, so capture_judge_message raises JudgeOutputError and the run halts (never crashes uncaught, never treats an empty judge as valid). Distinct from the low-score scenario, where a NON-empty judge with no SCORE is a warning + 0. It isolates one seam rather than driving a duel to one of its exits.
