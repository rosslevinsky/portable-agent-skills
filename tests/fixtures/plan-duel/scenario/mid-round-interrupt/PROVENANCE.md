# Provenance

Encodes the artifact contract spelled out in the skill's own `SKILL.md`, `round.md` and `summary.md`, which ship beside this file. These are **synthetic** fixtures, not a recorded run: plan bodies are deterministic filler (>=200 bytes, with a `# ` title), and judge `SCORE:`/`PREFERRED:` values are hand-chosen to drive one specific exit path. The engine snapshots/renames/scores exactly as SKILL.md + round.md + summary.md specify; the integration test asserts that contract.

Scenario: **mid-round-interrupt** (resume). Rounds 0-2 are complete; round 3 was interrupted, leaving a `plan-a-round-3.md` + `judge-round-3.md` straggler and stale live plans. v1 finds last_completed_round=2, deletes the round-3 stragglers (logged), restores the round-2 snapshots to the live plans, prints `Resuming in {workdir} from round 3.`, and runs a fresh round 3 (from `inputs/`) that converges at score 8. The fresh round-3 snapshot/judge overwrite the straggler content.
