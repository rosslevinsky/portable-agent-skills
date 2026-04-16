# Plan Duel — Round 0: Initial Plans

Uses: `workdir`, `plan_init_skill_path`.

Both agents write their plan files directly. Run **simultaneously**:

---

## Agent A (controller runtime)

Spawn a sub-agent to generate Plan A. The controller runtime runs this
sub-agent using its native orchestration mechanism:

> **Claude adapter:** Use the Agent tool with subagent_type=general-purpose
> with the prompt below.
> **Codex adapter:** Write the prompt below to `{workdir}/controller-prompt-0.txt`,
> then run: `codex exec --skip-git-repo-check -C "{workdir}" "$(cat "{workdir}/controller-prompt-0.txt")" < /dev/null`

Prompt for the sub-agent:

> Read the plan-init skill from `{plan_init_skill_path}` and follow
> its methodology to produce a plan for the problem in `{workdir}/problem.md`.
> You are operating autonomously — there is no user to interview, so make
> reasonable assumptions where plan-init would normally ask questions, and note
> each assumption in the plan.
>
> Write the complete plan document to `{workdir}/plan-a.md`.

---

## Agent B (participant runtime)

Pre-create output files so the participant's file-write tooling does not fail
on read-verify of nonexistent paths:

```bash
touch "{workdir}/plan-b.md"
```

Write `{workdir}/codex-prompt-0.txt`:

```
Read the plan-init skill from {plan_init_skill_path}
and follow its methodology to produce a plan for the problem in {workdir}/problem.md.
You are operating autonomously — no user to interview. Make reasonable assumptions
where plan-init would normally ask questions, and note each assumption in the plan.

Write the complete plan document to {workdir}/plan-b.md.

IMPORTANT: When writing or overwriting files, use shell commands
(e.g. cat > file << 'EOF' ... EOF) rather than patch-based tools,
to avoid read-verify errors on new or fully-rewritten files.
```

Invoke the participant runtime's CLI in non-interactive mode:

> **Codex adapter:** `codex exec --skip-git-repo-check -C "{workdir}" "$(cat "{workdir}/codex-prompt-0.txt")" < /dev/null > "{workdir}/codex-round-0-status.md"`
> **Other runtimes:** Adapt the invocation to the participant's CLI.

---

## Validate and snapshot

**Agent A:** If `plan-a.md` is missing or under 200 bytes — halt: "Agent A
plan generation failed at round 0."

**Agent B:** If `plan-b.md` is missing or under 200 bytes:
- Fallback: scan workdir for any `.md` file other than `problem.md`,
  `plan-a.md`, and `codex-round-0-status.md`, written in the last 5 minutes,
  ≥200 bytes.
  If found, copy to `plan-b.md` and log:
  `Fallback: used {filename} as plan-b.md.`
- If still not found — halt: "Agent B plan generation failed at round 0."

Copy `plan-a.md` → `plan-a-round-0.md`
Copy `plan-b.md` → `plan-b-round-0.md`

Print: `Round 0 complete — initial plans written | A: NNNN words, B: NNNN words`
(word counts via `wc -w < "{workdir}/plan-a.md"` and `wc -w < "{workdir}/plan-b.md"`).
