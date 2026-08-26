# Test Fixture: Stale Runtime Claim (negative)

One line per pattern, so each can be killed independently. A single line tripping two
patterns proves only that *something* matched — remove either pattern and the fixture still
rejects, which is how the third one below went uncovered entirely.

Codex cannot act as controller because it lacks a controller role.

Codex lacks sub-agent spawning.

Not supported in single-agent runtimes (e.g., Codex).
