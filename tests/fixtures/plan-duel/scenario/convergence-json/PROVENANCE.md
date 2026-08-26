# Provenance

The **JSON-verdict twin** of the `convergence` scenario. Same problem statement, same
plan bodies, same scores (6, 7, 8) and same `PREFERRED: A` — the ONLY difference is that
each `judge-round-N.md` is the schema-enforced JSON verdict a CLI emits under its
structured-output flag, instead of the pre-schema `SCORE:` / `DIFFERENCES:` /
`MISSED REJECTIONS:` / `PREFERRED:` line markers.

Pairing the two scenarios is the backward-compatibility proof: `convergence` is left
**byte-for-byte unchanged** so the old contract keeps being exercised end to end, and the
integration test asserts that this twin drives the run loop to an **identical**
`summary.md` — same exit reason, same winner, same score trajectory, same rendered
differences after the scoped A/B→name rewrite. A judge round written before the schema
landed and one written after are therefore interchangeable, which is what makes a resume
over an older workdir safe.

The judge files are **bare single-line objects** with no markdown fence and no prose,
because that is exactly what both CLIs produce with the schema flag alone (verified
live). The engine's tolerance for fenced/prose-wrapped JSON is covered by unit tests
rather than here, since no adapter in the pack produces that shape.
