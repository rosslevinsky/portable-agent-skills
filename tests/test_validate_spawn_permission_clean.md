# Test Fixture: Spawned CLIs That State Their Permission

A worker that must edit the tree says so, even wrapped across lines:

> **Codex adapter:** script the worker per phase with `codex exec -s
> workspace-write -c approval_policy=never -C <dir> --output-last-message <file>`.

A reviewer that must NOT write says the opposite, just as explicitly:

`codex exec --json -s read-only -c approval_policy="never" -C <dir> "<review prompt>"`

A bare mention in prose is not an invocation — a fresh `codex exec` reviewer is fine.

Adapter commands carry the flag as argv data:

```json
{
  "agent_a": {
    "command": ["codex", "exec", "-s", "workspace-write", "-c", "approval_policy=never", "-C", "⟪workdir⟫", "⟪prompt⟫"],
    "stdout": "file"
  },
  "judge": {
    "command": ["codex", "exec", "-s", "read-only", "-c", "approval_policy=never", "-C", "⟪workdir⟫", "⟪prompt⟫"],
    "stdout": "file"
  }
}
```
