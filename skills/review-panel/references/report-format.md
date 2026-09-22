# The report

`report` is the last stage and the only one that reads the dispatcher's record. **It** is
what writes `findings.json`, from everything the stages before it left in the run directory
— one record per candidate, one per cluster, one per area clustering touched, and the
synthesis round's own record where that round ran — renders `report.md`
from that structure, and converts that file into `report.html`. The page is the prose in
another format, not a second report written beside it: nothing decides what the page says
except what the report already said, so the two cannot disagree about a **fact**. The page
carries one thing the prose does not — a contents list — and that is navigation rather than
a fact, added by the conversion the way ids and back-links already are. All three land in
the run directory, from what is already there: `job.json`, `job-notes.json` where the
interview left one, `inventory.json`, `areas.json`,
`units.json`, `candidates.json`, every verification, clustering and synthesis unit's
`result.json` or `error.txt`, `dispatch.json`, and `snapshot/`, which is where the source
quoted beside each established defect is read from. Nothing else: no clock, no absolute
path, so two runs over one tree write byte-identical files whatever order their units landed
in.

The synthesis round is **optional**, and `report` is where that shows: the document is
always grouped by status, and a round that came back with a usable answer adds its tiers as
a level inside those sections. A round whose unit was written adds no tiers unless a
usable result landed, and the appendix names that unit and says what the grouping is
instead. A unit with neither a result nor an error beside it is recorded as
**missing** — nobody dispatched it, or something stopped its worker before it wrote
anything, which from the run directory look the same — and one whose worker wrote an error is recorded as **failed**, with the
diagnostic it left; the report says which, because "nobody ran it" and "it ran and broke"
are different things to know about a round. There is no synthesis record where no unit was
written: a run where `synthesize` was never invoked, and equally one where it ran over a
tree that raised nothing, since a run with no defects has nothing to judge.
The round itself writes only its unit and the listing beside it; its answer reaches
`findings.json` because **`report` reads the result and records it there**, which is why the
document is a pure function of the run directory and not of anything the round did while it
ran — one answer, one reader.

It runs once. Any of the three already present is a refusal, because somebody may have
annotated what the first run wrote; `--rerender` is how you say you meant it, and it
replaces only those three and refuses anything else in their way. All three publish
together — a failure puts back the state the call found. The exit code says whether
execution completed — `0` for the documents written, `2` for a refusal — and never whether
the tree is clean.

The engine is the authority on the shape of its own output. What a status means, how a
verdict becomes one, what each section holds and what the stage refuses are all stated
where they are enforced, in the engine and in the schemas the payloads carry. What is
written down here is the one file the engine cannot produce and the one instruction it
cannot carry out.

## `dispatch.json`

The engine never spawns, so it cannot know which adapter ran a slot or under what
permission. The driver writes that down, once, in the run directory, from what the run
actually did, and `report` states it and nothing stronger. One JSON object, parsed by name like the job — an
unknown key, a missing field, an empty value or a string the report could not write in UTF-8
is refused by name, and nothing is defaulted:

```json
{
  "rung": "two-runtimes",
  "slots": {
    "A": {"adapter": "fresh sub-agents of the driving runtime", "permission": "read-only"},
    "B": {"adapter": "the other runtime's command line under the read-only supervisor",
          "permission": "read-only sandbox to read; verification in a disposable copy with write"}
  }
}
```

- `rung` is `two-runtimes` or `one-runtime` — the two rungs the skill declares, and which
  one a record names follows the configuration the run was planned against, which is pinned
  and cannot change under it. There is no third: a run where one context was both finder
  and verifier satisfies none of what a status on the page means, and the driver refuses
  such a run rather than recording a weaker rung for it.
- `slots` has exactly `A` and `B`; each records `adapter` (the runtime and the mechanism
  that ran the slot's units, in the dispatcher's words) and `permission` (the containment
  it actually applied). Both are free text on purpose: the engine cannot verify a sandbox,
  so it renders the claim verbatim rather than a vocabulary it could not check.
- One cross-check ties the rung to the slots: `two-runtimes` claims two models, so both
  slots naming one adapter is refused. `one-runtime` is checked no further, and a record
  naming it with two adapters is accepted: the engine renders what it is given and cannot
  know what ran. The report then claims only what that rung allows.

## The job, in the appendix

**The job** is an appendix subsection: one line per field, then **the job itself as JSON**, then
the commands that run it again. So a reader who did not write the job can tell how the tree was
divided, what each reader read for and what was in scope — and can re-run the audit from the
document, on the same tree and with an adapter configuration of their own. Those are the two
things the block says outright that it cannot supply, along with a fresh run directory; the
sentence to keep honest is that one, not a claim of reproducibility from the page alone.

Printing the JSON is the point: the report is what gets sent on, and an instruction naming
`job.json` names a file that is not travelling with it. `root` is the one field substituted, by
the literal `/absolute/path/to/the/tree`, which keeps the page down to a single absolute path.
Note that this is a well-formed absolute path: a job pasted back unedited is *accepted* and then
fails on the missing tree, rather than being refused on the field — one clause of warning is
cheaper than the confusion.

It sits in the appendix rather than beside the problem statement, and the paragraph under the
statement is what keeps that a move rather than a burial. **What that paragraph must go on saying
is in `DECISIONS.md`**, under *The job is in the appendix, not beside the problem statement* —
kept in one place because a shortened pointer that satisfies a partial list here is exactly the
regression that entry exists to refuse.

Every line is rendered from `job.json`, `job-notes.json`, `areas.json` and `inventory.json` and
from nothing a unit wrote, which is what stops it disagreeing with what ran.

**The printed JSON keeps its own spelling of every path.** It is one of two places that do; the
other is a sentence quoted from a worker, and the reason is the same — the one-spelling rule
governs what the renderer *composes*, and shortening a path in a reproduced record would be
editing it. A short name also names nothing outside this document, and the `files` list is what
tells a run which files to read. The path legend states both exemptions where a reader meets them.

## `job-notes.json`

The job file cannot say **where its own values came from**: the engine refuses an unknown key,
and the file is a statement of what ran rather than of how it was decided. So the interview
writes `job-notes.json` beside it, mapping a job field to `stated`, `defaulted` or `interview`.
**`job.md` is the contract** — it is written there, by the interview, and giving its shape twice
is how the two spellings drift apart. What is here is only what the *report* does with it.

Only `defaulted` puts a mark on a field's line — `(default)`. Marking the two that somebody chose
would put a parenthesis on nearly every line to say "as asked". But **the file's mere presence
changes two sentences**, whatever its values: with it, the report says an interview settled these
fields and that the section marks the defaults; without it, that nothing in the run directory
records which. So a notes file recording only `stated` values still earns its place — it renders
no mark and makes a true statement the report could not otherwise make.

**Absent is the ordinary case**, and it means only that nothing recorded the choices — a job
handed in ready-made has no notes, and neither has one written before the file existed. The block
says that rather than concluding which, because the absence cannot tell them apart. A notes file
that is *present and malformed* is refused instead of ignored: dropped, the report would say no
record of the choices is here, which is false while one sits in the directory unread. An unknown
field name or an unknown value is refused by name.

The driver copies it **after** `plan`, never before. `plan` refuses a run directory holding
anything but the job being loaded, and widening that guard to admit this file would admit every
other one too.

## Summarizing for the owner

Read `report.md` and say, in a few lines: the rung and what it lets the run claim; the
counts by status; each established finding in one line — location, failure, status; how
many were refuted or left unresolved and why, when a whole batch was; and every coverage gap
by name. Point at `report.md` by path for the rest. Never inline a snapshot file, an
evidence block or a payload: the report is the record, and the summary is the pointer to it.
