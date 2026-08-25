# Test Fixture: Sibling-Skill Path (must FAIL)

A repo-rooted path to another skill. It resolves in a clone of this repository and in
nothing a user installs, because the installer ships each `skills/<name>/` directory on its
own — there is no `skills/` parent above an installed skill to descend from.

Run the supervisor at `skills/diff-review/review_runner.py` before continuing.

See `skills/plan-duel/references/judge-prompt.md` for the rubric.

This fixture deliberately contains **no dot-dot escape**, so it can only be rejected by the
sibling-path rule. A draft that closed with a sentence about the dot-dot spelling and
was rejected for that instead — passing the harness while proving nothing, which is the
bare-truthiness failure the fixture assertions were converted away from.
