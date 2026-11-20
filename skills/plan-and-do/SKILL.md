---
name: plan-and-do
description: >
  Runs the whole planning cycle in one command: plan-init, an xc cross-check of the plan,
  plan-phase, an xc cross-check of the proposed phase list, the phase files, and an xc
  cross-check of those files. Stops there so you can look, or with --go continues into
  plan-run. Holds only the order and the stops; each step loads its own skill fresh. Use
  when the user invokes /plan-and-do, or says "plan and build this", "plan, phase and
  cross-check this", "run the whole planning cycle". Argument: the task description,
  optionally preceded by --go.
---

# Plan and Do

Run the planning skills in order, with a cross-check by another model after each one,
from a single command.

_Classification: Degraded — where the `xc` skill is not installed, the three cross-checks are skipped and the run says so; the plan, the phases and the build are unchanged._

## How each step runs

**Load each skill fresh, at the moment its step starts, and follow that skill's own
instructions.** Never work from a remembered version of a skill: a step run from memory is
shorter and looser than the skill itself, and that is the failure this skill exists to
avoid.

> **Claude adapter:** start each step by calling the Skill tool with the skill's name.
> **Codex adapter:** start each step by reading that skill's `SKILL.md` from the directory
> it is installed in, beside this skill.

This skill holds only the order, the stops and the arguments. Everything else is in the
skills it calls.

**Calling this skill is the request to run every cross-check below.** Where a skill offers
the `xc` skill and says not to run it autonomously, run it anyway: the user asked for it by
calling this skill. Every other autonomous rule in the skills it calls still applies.

**A missing skill.** If the `xc` skill is unavailable, skip each cross-check, say so at that
step, and continue. If `plan-init`, `plan-phase` or `plan-run` is unavailable, stop and
name it: the cycle cannot run without it.

**After each cross-check**, confirm that `xc` printed its report: which reviewer ran, and
each finding's decision. If it did not, say that the cross-check did not happen, and why,
before continuing.

## Arguments

- The task description, passed to `plan-init`. With none, `plan-init` asks for one.
- `--go` before it: after the last cross-check, continue into `plan-run` instead of stopping.

## The steps

1. **Plan.** Run `plan-init` with the task description. Its interview is a stop: wait for
   the user's answers.
2. **Cross-check the plan.** When `plan-init` offers `xc` on `plan.md`, run it.
3. **Propose the phases.** Run `plan-phase` on the plan. Go as far as its proposed phase
   list, before it asks for approval.
4. **Cross-check the proposal.** Write the proposed list to a file outside the repository,
   named `proposed-phases.md`. Never name it `phase-*.md`, and never put it in the plan's
   directory: the tracker check reads every `phase-*.md` there. Run `xc` on that file with
   `plan.md` as context, then present the list as it stands after `xc`.
5. **Approve.** The approval `plan-phase` asks for is a stop: wait for the user.
6. **Write the phases.** Let `plan-phase` write the phase documents and `execution.md`.
7. **Cross-check the phase files.** When `plan-phase` offers `xc` on them, run it, with
   `plan.md` as context.
8. **Stop or build.** Without `--go`, stop: report the plan's path, the three cross-check
   results, any stop passed without the user under a skill's autonomous rule, and the next
   step, `/plan-run <the plan file>`. With `--go`, report the same, then run `plan-run` on
   the plan.
