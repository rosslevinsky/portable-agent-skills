# Provenance

Encodes the artifact contract of the superseded v1 duel workflow — the behavioural golden these fixtures hold the engine to. That contract is not a tag you have to fetch: it is spelled out in the skill's own `SKILL.md`, `round.md` and `summary.md`, which ship beside this file. These are **synthetic** fixtures, not a recorded v1 run: plan bodies are deterministic filler (>=200 bytes, with a `# ` title), and judge `SCORE:`/`PREFERRED:` values are hand-chosen to drive one specific v1 exit path. The engine snapshots/renames/scores exactly as v1's SKILL.md + round.md + summary.md specify; the integration test asserts that contract.

Scenario: **max-rounds**. Judge scores 0..7 chosen so no round converges (all < 8) and none stagnates (each 3-round window beats the prior peak), so the loop runs the full 10 rounds and stops on v1's max-rounds exit (score 7). `rounds_run == 10 (>= 5)` so summary.md appends the verbatim mutual-critique note. `PREFERRED: A` -> Claude wins.
