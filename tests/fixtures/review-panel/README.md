# fixtures/review-panel/

Fixtures read by `tests/test_review_panel_engine.py` and by nothing else: job files,
inventory trees and unit directories the engine suite feeds to `review_panel.py`. Each
lands with the engine stage that reads it. Nothing here is a runtime artifact, and no
branded CLI is spawned over it — the suite drives the engine with stub workers.
