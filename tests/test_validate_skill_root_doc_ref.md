# Test Fixture: Repo-Root Doc Reference (negative)

For the full contract, see the repo's `PORTABILITY.md`.

This must be rejected: `PORTABILITY.md` lives at the repo root and is not installed
alongside the skill, so an installed skill file cannot read it at runtime.
