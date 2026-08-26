# AGENTS.md

Skills are Markdown that a model obeys. Changing one changes behaviour, and most of the
guards here fail in ways that do not explain themselves.

## Before you finish, run all three

```bash
python3 scripts/validate_cross_runtime.py skills/
python3 scripts/validate_cross_runtime.py --test-fixtures tests
python3 -m unittest discover -s tests -p 'test_*.py'
```

`python3`, not `python` — macOS ships no bare `python`. On native Windows, `py -3`.

## Five things that will bite you

**Editing prose fails the build unless you move a number.** Every skill has a word count in
`scripts/skill-budgets.json` that must *equal* what it measures now. Growing past it fails
the validator; shrinking below it and leaving the number alone fails a test. Move the number
in the same change as the prose. Never trim assertions or split a file to duck it.

**Adding a skill needs two edits nothing discovers.** Add it to README's skill table *and*
bump the skill count there, and give it an entry in `skill-budgets.json`. Miss either and
the run fails without saying why.

**A skill file may not reference anything outside its own directory.** The installer ships
`skills/<name>/` alone, so a link to `PORTABILITY.md` or a path climbing out with `../` is
dead on an installed machine. Do not add a helpful cross-reference to a root document.

**The `-v1` skills duplicate their current counterparts on purpose.** Read the parity ledger
in `CONTRIBUTING.md` before deduplicating anything. A fix to one side of a mirrored block
lands in its mirror in the same change, and several differences between the two generations
are deliberate rather than drift.

**Do not write branded tool names into skill text.** No `Glob tool`, `Read tool`,
`subagent_type`, `run_in_background`. Say what to do — "search for files matching `pattern`"
— so the same file runs under any runtime. Adapter notes are the one exception.

## A green run is not proof

The validator checks the mechanical rules. It does **not** check hardcoded model names,
autonomous fallbacks on user-prompting steps, or naming both `CLAUDE.md` and `AGENTS.md`
when a skill refers to project instruction files. Read for those yourself.

`PORTABILITY.md` is the authoring contract and closes with the full list of what a green run
does not cover. `CONTRIBUTING.md` is the procedure.

## Editing a skill you are running

If you were asked to improve a skill you have loaded, you are editing the *installed copy*
under `~/.claude/skills/<name>/` or `~/.agents/skills/<name>/`, which git does not track.
Port the change to `skills/<name>/SKILL.md` here, run the gate above, then `python3
install.py` to put the reviewed version back.

There is no symlink install, deliberately: a linked install would make the installed path
the repo file, and you would be rewriting the instructions you are executing.
