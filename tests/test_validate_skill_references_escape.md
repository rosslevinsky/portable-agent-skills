# Test Fixture: `references/`-level Escape (must FAIL at depth 1)

The threshold this fixture exists for. A file under `references/` reaches its own skill
root with a single `../`, so one is allowed — the clean fixture beside this one proves
that. Two leaves the skill entirely, and nothing was testing it.

Read `../../plan-duel/references/judge-prompt.md` for the rubric.

Copy the template from `../../../templates/phase.md` before starting.

Both paths are inside no installed skill. Three skills ship a `references/` directory, so
this is the live shape, not a hypothetical one: it is the same document depth every one of
them is judged at.
