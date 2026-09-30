# Provenance

Encodes the artifact contract spelled out in the skill's own `SKILL.md`, `round.md` and `summary.md`, which ship beside this file. These are **synthetic** fixtures, not a recorded run: plan bodies are deterministic filler (>=200 bytes, with a `# ` title), and judge `SCORE:`/`PREFERRED:` values are hand-chosen to drive one specific exit path. The engine snapshots/renames/scores exactly as SKILL.md + round.md + summary.md specify; the integration test asserts that contract.

Scenario: **init-interrupt** (resume). The workdir has a stale `plan-a.md` and round-0 prompt/progress files but NO completed round (no plan-a/b-round-N pair). v1 full-resets (deletes plan-*/prompt/progress artifacts, keeping problem.md and unrelated files), prints `Init incomplete — restarting from round 0.`, re-runs round 0, then converges at round 3. Continuation inputs live in `inputs/`.
