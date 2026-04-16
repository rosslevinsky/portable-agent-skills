# Test Fixture: Multi-line Adapter Block

Launch component reviews in parallel if supported.

> **Claude adapter:** Launch all component sub-agents in a single message (one
> Agent tool call per component, all in the same response). Run them as
> foreground agents so you receive all results before proceeding.
> Do not use `run_in_background: true`.

Each component review must include the attack surface document.
