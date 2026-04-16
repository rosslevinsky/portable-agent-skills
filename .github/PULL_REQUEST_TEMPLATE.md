<!-- Thanks for contributing! Before submitting, please confirm the checklist below. -->

## Summary

<!-- One or two sentences: what does this PR change and why? -->

## Changes

<!-- Bullet list of concrete changes. Link to related issues with "Fixes #123" if applicable. -->

-

## Portability checklist

- [ ] Skill text uses runtime-neutral vocabulary (no branded tool names outside `> **adapter**` blockquotes)
- [ ] Every companion-skill reference (e.g. `` `cyw` skill ``) has a nearby fallback for runtimes that can't invoke it
- [ ] Any user-prompting step has an autonomous fallback
- [ ] If this skill is Degraded or Runtime-limited, it declares `_Classification:` near the top of its `SKILL.md`
- [ ] No private paths, project-specific identifiers, or hardcoded vendor attribution

## Test plan

- [ ] `python scripts/validate_cross_runtime.py skills/` passes
- [ ] `python scripts/validate_cross_runtime.py --test-fixtures tests` passes
- [ ] `bash tests/test_installer.sh` passes
- [ ] If this changes user-visible behaviour, README / CHANGELOG updated
