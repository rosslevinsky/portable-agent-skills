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
