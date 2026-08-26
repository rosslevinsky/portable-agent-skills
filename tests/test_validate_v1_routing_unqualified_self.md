---
name: plan-init-v1
description: >
  v1 plan initializer, superseded by the canonical suite. Use when the user
  invokes /plan-init-v1 against a plan that predates the Format: v2 marker.
---

# Create Plan (v1)

## Step 1 — Understand the initial task

The user's argument (everything after `/plan-init`) is the task description. If no
argument was given, ask: "What task do you want to plan?" and wait for the answer.

## Step 2 — Hand off

Write the plan to `plans/<slug>/plan.md`.

- Next step: "Run `/plan-phase-v1 <path>` to break this into executable phases."
