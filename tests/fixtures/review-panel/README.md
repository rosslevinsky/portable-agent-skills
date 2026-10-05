# fixtures/review-panel/

Fixtures read by `tests/test_review_panel_engine.py` and by nothing else: job files,
inventory trees and unit directories the engine suite feeds to `review_panel.py`. Each
lands with the engine stage that reads it. Nothing here is a runtime artifact, and no
branded CLI is spawned over it — the suite drives the engine with stub workers.

`old-run/` is a finished run directory — units through synthesis, `report-notes.json`, and
the `report.md`, `report.html` and `findings.json` the engine rendered from it — written by
the engine as it was before defects could have several sites. Every defect in it is one
site, and its notes cite those ids. It cannot be rebuilt once the renderer changes, so it
is kept as written: a re-render works on a copy, and the `report.md` here is the baseline a
re-render is compared with. The unit directories hold `result.json` alone; the payloads and
schemas `units.json` names were left out because nothing that renders reads them.

`v2026.10.0-run/` is a finished run directory written by the engine as released in
v2026.10.0, driven by that release's own engine-suite helpers and stub tables: two merge
units (the merge over its reply ceiling, so batched), one merge check upholding S1 and S2 as
one defect, one synthesis unit `synth-A` whose reply chose its own two tiers, and the
`report` that release published. It was generated in a temporary directory, which is the
path its report names. As in `old-run/`, the unit directories hold `result.json` alone.

`old-run-rerendered/` holds the files the v2026.10.0 release's `report --rerender` wrote over
a copy of `old-run/`. It is what `old-run/` re-renders as, which differs from the `report.md` stored in
`old-run/` because that one predates sites. Both directories are baselines: a re-render with
the current engine must produce the same text, the line naming the run directory aside.
