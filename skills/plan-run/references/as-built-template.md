# As-Built Template

`plan-run` assembles `plans/<slug>/as-built.md` from material it already produced — each
phase document's filled **Evidence** record — plus a drift report against `plan.md`'s
success criteria. It is **assembled, not re-derived**: the per-phase table's two columns
come straight from Evidence's `Outcome` and `Deviations` lines rather than from
re-inspecting the code.

Emit for a non-trivial plan (≥ 3 phases, or any plan with independent phases). Reference all artifacts
by path or CI URL only — never inline screenshots, videos, traces, or reports.

~~~markdown
# As-Built Spec and Drift Report — <plan title>

_Assembled by /plan-run on <date>. Source of intent: [`plan.md`](./plan.md);
execution record: [`execution.md`](./execution.md)._

## What was built

<2–5 sentences describing the delivered result as it actually is — the as-built spec,
not the original intent.>

## Per-phase outcomes

| Phase | Outcome | Deviations from plan |
|---|---|---|
| <phase-01-slug> | <shipped / partial / skipped> | <deviation, or "none"> |
| <phase-02-slug> | <...> | <...> |
| <phase-03-slug> (reconciles 01–02) | <...> | <...> |
| … | … | … |

## Drift report vs. plan success criteria

For each success criterion in `plan.md`, its final status:

| Success criterion | Status | Note |
|---|---|---|
| <criterion 1> | met / partially met / not met | <one-line reason> |
| <criterion 2> | … | … |

## Artifacts

- <name>: <path or CI URL>  <!-- e.g. screenshots, videos, coverage reports -->
- <name>: <path or CI URL>

## Open items / follow-ups

- <anything deferred, flaky, or worth a follow-up — or "none">
~~~

The per-phase outcomes come from each phase document's filled **Evidence** record — its
`Outcome` and `Deviations` lines, verbatim. A phase document written under an older shape carries
the same content under the heading **Review Packet**; read it as it stands and never
re-emit it. The drift table is the one genuinely new synthesis step: walk `plan.md`'s
success criteria and record how the delivered result measures up.
