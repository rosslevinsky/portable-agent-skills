# Test Fixture: Backends Doc Reference (negative)

For the backend recipes, see the repo's `BACKENDS.md`.

This must be rejected: `BACKENDS.md` lives at the repo root and is not installed alongside
the skill, so an installed skill file that points at it points at nothing. A skill inlines
the backend shape it needs instead.
