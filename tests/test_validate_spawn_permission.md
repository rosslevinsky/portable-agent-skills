# Test Fixture: Spawned CLI With an Inherited Sandbox

> **Codex adapter:** script the worker per phase with `codex exec -C
> <dir> --output-last-message <file>` so its result lands on disk.

An adapter command that inherits the default just as silently:

```json
{
  "agent_b": {
    "command": ["codex", "exec", "--skip-git-repo-check", "-C", "⟪workdir⟫", "⟪prompt⟫"],
    "stdout": "file"
  }
}
```

A sandbox mode with no approval pin is defeasible, so this fails too:

`codex exec -s read-only -C <dir> "<review prompt>"`
