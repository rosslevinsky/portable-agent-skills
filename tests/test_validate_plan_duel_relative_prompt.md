# Test Fixture: Workdir-Relative Companion Skill Reference (negative)

Load the plan-init skill from the relative path `../plan-init/SKILL.md` and
use it to generate the initial plan for each participant.

This is not portable when the participant process runs with `-C "{workdir}"`,
because the path resolves relative to the generated workdir rather than the
installed skill directory.
