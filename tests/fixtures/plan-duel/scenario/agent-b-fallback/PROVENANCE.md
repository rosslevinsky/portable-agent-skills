# Provenance

Encodes the artifact contract spelled out in the skill's own `SKILL.md`, `round.md` and `summary.md`, which ship beside this file. These are **synthetic** fixtures, not a recorded run: plan bodies are deterministic filler (>=200 bytes, with a `# ` title), and judge `SCORE:`/`PREFERRED:` values are hand-chosen to drive one specific exit path. The engine snapshots/renames/scores exactly as SKILL.md + round.md + summary.md specify; the integration test asserts that contract.

Focused seam: round-0 agent B writes a <200 B `plan-b.md` plus a >=200 B recent `stray-plan.md`. The run loop catches the round-0 AgentOutputError, calls recover_agent_b_round0 (which adopts the stray as plan-b.md and logs `Fallback: used stray-plan.md as plan-b.md.`), then continues to a normal convergence at round 3. It isolates one seam rather than driving a duel to one of its exits.
