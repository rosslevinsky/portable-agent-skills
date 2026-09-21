# Architecture — a guide for whoever changes these programs

Not a brief, and not a step. Nothing in a run sends a worker or a dispatcher here: this is
for somebody about to change `review_panel.py`, `review_panel_run.py` or the skill text, who
would otherwise have to infer the boundaries from ten thousand lines of Python.

## Where the control flow lives

Four pieces, each owning one thing.

- **`SKILL.md`** — the interview that produces `job.json`, the adapter config, one command,
  and reading `report.md` back to the owner. It dispatches nothing.
- **The driver, `review_panel_run.py`** — the loop and **every worker spawn**: bootstrap, the
  run lock, the attempt protocol, landing a reply, resume, providers, storage headroom, and
  the disposable working copies. It imports the engine and calls its stages **in-process**, so
  no stage outlives the process that owns the run. Its command surface is four verbs — `run`,
  `status`, `resolve-attempt`, `resolve-unit` — and `run` is filled in when the first
  argument is none of them.
- **The engine, `review_panel.py`** — stage logic, and **it never spawns a worker**: `plan`,
  `route`, `cluster`, `synthesize`, `report`, plus `check` for parsing one reply. It does run
  `git`, which is not the same thing and is the distinction the placement rule below turns
  on. It reads `result.json` and `error.txt` and never asks who produced them or how many
  attempts it took.
- **The supervisor, `review_runner.py`** — bounds one worker: one launch, its transcript, its
  status line. The driver calls it once per attempt, and it knows nothing about the run
  around it. Its path comes from `--supervisor`; otherwise the driver falls back to the copy
  in the `diff-review` skill beside this one, so a host that installed the two elsewhere
  passes the flag rather than being stuck.

**The rule that places a new thing: disk is the state, and every fact is derived by replaying
the immutable records on it.** No counter, no cache and no marker file is authoritative where
the records disagree with it. Three consequences settle nearly every placement question.

- Anything that **dispatches a review unit** — spawns a worker, waits for it, adjudicates
  its outcome — is the driver's. The line is worker dispatch and not processes in general:
  the engine runs `git` itself through `subprocess.run`, to ask a repository what it tracks.
  What the engine must never acquire is knowledge of how a unit gets launched, so that it
  stays runnable by hand over a directory somebody else filled in.
- Anything that **reads units' answers and builds the next stage's inputs** is the engine's.
- Anything **derived** — an allowance, a provider's health, what may still be retried — is
  computed by replaying records rather than stored as a count. A stored count plus a crash
  between the write and the record is how a resumed run reaches a confident wrong answer.

**The engine never learns how a unit is spawned**, and the clearest worked example is
`WRITE_CAPABLE_KINDS` in the driver: the one enumeration of the unit kinds whose brief
permits the worker to write. The working directory, which of a slot's two command lines is
used, the run-wide serialization of reproductions, the disk reservation and a copy's
retention are all read off that one set. Membership turns on whether the brief **permits**
writing, not on whether the work is expected to produce anything worth keeping — the copy is
containment. A read-only mode is not uniformly enforced everywhere, so a unit told to write
freely and handed the pinned snapshot rewrites the tree every other unit was measured
against, and the next integrity check refuses the whole run after the reading and
verification rounds have been paid for.

The supervisor is the smallest of the four and gains nothing from this skill beyond two
opt-in flags, `--status-detail` and `--max-capture-bytes`; a caller passing neither sees its
bytes exactly.

## The stage machine

`units.json` carries a `stage`, and that marker is the run's only commit record. Nothing keys
on a directory or a payload existing, so a round that legitimately produced zero units is
still a committed round.

| Marker | What the loop does next |
|---|---|
| `reading` | the reading round — readers, auditors, the capability probe — then `route` |
| `verification` | the verification round, then `cluster` |
| `clustered` | the clustering round, then `synthesize` |
| `synthesized` | the synthesis round, then `report` |
| `reported` | nothing: the run is finished |

That table is the driver's `ROUNDS`, which maps each marker to the unit kinds its round
dispatches and the engine stage that closes it. **Those five are every value `units.json`
ever holds.**

**`ROUTED_STAGE` is a sixth constant and not a sixth stage**, which is worth knowing before
it misleads you the way it has misled others. `"routed"` is written into `candidates.json`
and never into `units.json`, and it is a **type tag** rather than a position:
`_read_candidates` refuses a file whose `stage` is not `routed`, beside checking it carries
a candidate list, a unit list and a probe record. That is how the engine tells its own route
record from some other JSON in the directory. Reading it as a progress marker suggests a
stage the loop passes through, and there is none.

**One ending is not in the table, and it is the driver's alone.** `refuse_a_stranded_slot`
runs at every round boundary, before anything is spawned, and ends the run when a slot has
landed nothing **and** has nothing pending that could change that. It is not a stage and
writes no marker: the run stops where it stood, with no report, naming the slot and pointing
at its per-attempt records. The reason is rule 3 rather than throughput — a slot that
answered none of its units checked none of the findings addressed to it, so no page the run
could write would be true.

Both halves of that predicate are load-bearing and were settled by review. Without *landed
nothing*, a slot having a bad round ends the run. Without *nothing pending*, a run that is
**resumed** between the reading round and `route` is refused one step before the stage that
would have given the silent slot its verifier — so the guard returns early at a boundary
whose round has nothing left to finish, which is exactly the state a resumed run is in
there. Such a round has nothing to spawn either, which is why returning early costs the
earlier refusal nothing. `_derived_rung` keeps the same refusal as a backstop for a run that
somehow reaches the report stage stranded, and both raise one shared message.

Every stage writes its outputs first and replaces `units.json` last. So **a stage keys its
refusal to the marker rather than to its own artifacts**: entered while the marker still
names the stage before it, `_reclaim` takes back that stage's partial outputs — the files and
unit directories it was about to write, and the process-named `.tmp` scratch beside them,
each enumerated by the caller rather than swept — and it runs again. That is what makes a
killed run recoverable without a person listing files and guessing.

**The stage's own claim file is the exception, and it is not reclaimed.** `_claim_stage`
creates `<stage>.lock` with an exclusive create and, finding one already there, raises and
tells the operator to delete it — because a stage that took over a claim it found would be
two stages deleting each other's work. Removing it automatically is the **driver's**,
`clear_engine_claims`, and only because the driver holds the run lock and so knows no stage
is running. Call an engine stage by hand after a kill and you get the refusal, not the
cleanup.

**Two stages are not purely marker-keyed**, and anyone changing recovery has to know both.
`report` asks `published_report` about its own files first: the stamp plus all three outputs
is a report that finished, so it is preserved and the marker advanced over it rather than
rendered a second time — which is also what makes a recovered report byte-identical, since
the stamp carries the timestamp the report was generated with. Fewer than three files beside
the stamp is an interrupted publication and is reclaimed like anything else. And `plan`
refuses a run directory holding anything but the job file it was given, by name, before there
is any marker to consult.

Two vocabularies of the driver's sit beside the marker, both on disk.

- **What a unit landed** is one of three: `UNIT_COMPLETE` (a `result.json` that decoded),
  `UNIT_FAILED` (an `error.txt`, or a result that cannot be read as one), `UNIT_MISSING`
  (neither file written). The third is the state a unit must never be left in, and the
  driver's replay of unfinished publications is what stops it becoming permanent.
- **An attempt's state** is read by `read_attempt`, and **the order of its tests is the
  contract**. A parsed disposition makes it `adjudicated`; failing that, an operator
  resolution makes it `resolved`; failing that, a complete status record makes it `decided`.
  Only when none of those holds does the attempt's own launch metadata answer: an incomplete
  `argv.json` is `orphan-claim`, and otherwise the clock separates `running` from `uncertain`
  at spawn time plus deadline plus grace. A disposition outranks a status because an
  operator's decision has to beat one arriving after it, and the clock is consulted last
  because it never decides an outcome — only whether an unfinished attempt is still worth
  waiting for. Nothing in the chain reads a file's existence as a signal. Its
  **disposition**, written once and never rewritten, is one of `accepted`,
  `refused`, `no-reply`, `worker-failed`, `launch-failed`, `provider-unavailable`,
  `infrastructure`, `operator-failed`, `operator-retry`. A unit is terminal only when its
  publication exists **and names the disposition that produced it**.

## How a test resolves to the code it is about

`subject_map` answers this by running two rules and taking their **union**.

**The name rule**, `subjects_by_name`. The test's stem loses its marker — a leading `test_`
or `spec_`, or a trailing `ITCase`, `Tests`, `Test`, `Specs`, `Spec`, `IT`, `_tests`,
`_test`, `_spec`, `.test`, `.spec`, longest first so `Tests` is not read as `Test` with a
stray `s` — and a source file matches when **the stripped test stem starts with that
source's stem**, `stem.startswith(_stem_of(s))`, and not the other way round. So
`LeaseServiceMoveOutTest.java` selects `LeaseService.java`, while `FooTest.java` does **not**
select `FooBar.java`. Ties break on three keys in order: **exact extension equality** — the
file suffix, so a `.kt` test of a `.java` class scores nothing here and is settled by the
keys below it — then the longer source stem, then the more shared leading directories. At
most one file comes back, because a name encodes one subject.

**The mention rule**, `subjects_by_mention`: every source file whose stem the test's text
names as a whole word.

**Why unioned rather than chained.** One test file can exercise two classes and name only one
of them in its filename, so a rule that stops at the first answer misses the second every
time — and a missed subject is a source file audited by nobody. The cost runs the other way:
a test names what it imports as well as what it runs. That is why each link is carried with
how it was derived — `SUBJECT_BY_NAME` and `SUBJECT_BY_MENTION`, shown to the auditor as
*named for it* and *mentions it* through `SUBJECT_HOW_SAID` — and why the plan preview prints
the whole map before anything is dispatched.

## What the mock filter removes, and the ways it is known to fail

One mention shape is worth dropping: a class the test only ever mocks. `_mock_only` drops
it. Mechanically, the filter cuts the text into declaration-sized pieces (`_statements`),
keeps the pieces naming the class, and answers yes when **every** one of those pieces matches
some `mocks` pattern and **no** piece matches a `constructs` pattern. Every pattern is an
unanchored `re.search`, so it asks whether a mock spelling appears *somewhere* inside a piece.

**There is no safe direction here, and you should not go looking for one.** The obvious claim
— that anything the patterns cannot read keeps its link — is false, and so is the narrower
version of it about the predicate alone. What holds is much smaller: **a piece that matches
no `mocks` pattern at all fails the `all` and keeps the link.** That is the whole guarantee.
It says nothing about a piece that matches one, and a piece can match a mock spelling while
also containing a real use the `constructs` patterns do not recognize.

**Two ways it is known to drop a link that should have been kept.** Both reproduced; what is
written here is what is **known**, not what is possible, and another shape is likelier than
not.

1. **A mock spelling and a use `constructs` cannot read, in one piece.** The mock answers for
   both. Reproduced twice, each in Python, whose `constructs` reads the **position** a new
   instance goes to — assigned, returned, or handed straight to another call:
   `patch("app.svc.Thing"); real = svc.Thing()` builds the class inside the module the stem
   names, and `patch("app.Thing"); Thing().charge()` uses the instance without storing it.
2. **A JavaScript template literal with another nested inside it.** `_strip_comments` finds
   strings by pattern and a pattern cannot balance `${ }`, so the inner backtick closes the
   outer match and the text between the two is read as code. A `//` there opens a comment and
   the rest of the line goes with it, construction and all: in
   ``const u = `a ${`https://example.test`} b`; const real = new Thing();`` the construction
   is erased and `Thing` is dropped. The same seam runs the other way in an *unnested*
   template: a comment inside `${ }` sits within the literal, so it is never stripped and its
   text votes. Closing either means lexing the nesting rather than matching it.

The second is the predicate's *input* rather than the predicate, and its class is wider than
the one case: text mangled before any piece exists. `_strip_comments` matching literals and
comments in one pass, and every `drop_lines` pattern running from `_STATEMENT_START` to the
terminator, are what hold the rest of that class off — **patterns holding, not a structural
guarantee**, which is the whole of what the nested template demonstrates. A new row inherits
the shape of both answers and none of their coverage, and keeping one clear is most of what
"Adding a language" below is about.

Treat the filter as a heuristic that removes a common kind of noise, and expect it to be
wrong. A statement that splits mid-declaration stops reading as a mock and keeps its link,
which is the direction to be wrong in.

## Adding a language

`_MOCK_DIALECTS` is one row per language, and **a language with no row filters nothing**:
every mention survives. That is what makes the table safe to grow one row at a time.

1. **Key the row by what `_fence_language` answers** for the extension, not by the extension
   itself, so the extension table has one spelling in the file and cannot drift into two.
   That answer comes from `_FENCE_LANGUAGES`; a language missing from it answers with the
   empty string, so add the extension there first or the row never fires.
2. **Fill the seven fields of `_MockDialect`.** Every one is a tuple of regexes except
   `blank_literals`, a flag, and `split`, a single regex. `_statements` applies them in this
   order, and the order is what makes the fields legible: `_strip_comments` matches
   `literals` and `comments` **together, literals first**, then each `drop_lines` match is
   deleted, then what is left is cut on `split`. `split` is the language's statement
   terminator, written as a character class, and `mocks` and `constructs` are matched
   against the pieces that come out.
3. **List the `literals` even if the row does not blank them.** The two fields answer
   different questions. `literals` says where the strings are, and every row needs it,
   because a `//` inside a URL and a `#` inside a string are not comment openers — read as
   one, they erase the rest of the line and any construction standing on it. `blank_literals`
   says only what a matched string is replaced **by**: Java and C++ blank theirs, because
   they name a mocked class as a bare token and a literal holding `@Mock` would otherwise
   vote; Python and JavaScript keep theirs, because they name the mock target *inside* a
   string and blanking would erase every marker in the file. Getting this backwards for a
   new row disables the row or loses files, in that order.
4. **Bound every `drop_lines` pattern to the statement at both ends** — begin it at
   `_STATEMENT_START`, never a bare `^`, and stop it at the terminator, never at the end of
   the line. Both halves are one rule, and each costs a link on its own. It
   is the one field that deletes executable code, and it runs **before** the text is split,
   so whatever it removes is gone from the predicate entirely. Three ways that costs a link,
   all reproduced. A pattern ending `.*` runs past the newline — the substitution uses
   `re.MULTILINE | re.DOTALL`, so `^` and `$` anchor to a line but `.` does not stop at one,
   and `^require\b.*$` over `instance_double(Foo)` / `require "support"` / `x = Foo.new`
   takes the construction with it. Writing `[^\n]*` fixes only that: a dropped **line** still
   takes anything sharing it, so `require "support"; x = Foo.new` loses the construction even
   with `^require\b[^\n]*$`. And a bare `^` stops at the first statement on the line, so
   `require "a"; require "b"` leaves the second one standing and that piece alone keeps the
   link. All three are answered by `_STATEMENT_START + r"require\b[^;\n]*"`.
   **Match an import, not a statement with an import-shaped word in it**, for the same
   reason: a pattern reading any declaration that *contains* `require(` deletes
   `const real = new Thing(require("./config"))`, and one reading `import` as a prefix
   deletes a dynamic `import("./helper").then(() => new Thing())`. Both are constructions,
   and both went silently.
   **Emptying the field instead is not the cheaper answer it looks like**, whatever a
   language where imports share lines suggests: over the four rows that ship it turns five
   correct answers into wrong ones and rescues none, because a mocked collaborator is always
   imported, and that import names the class while matching no `mocks` pattern — so the piece
   fails the `all` and the rule never fires at all. Run that measurement yourself before
   trusting either answer; it is the two mock-rule test classes with `drop_lines=()`
   substituted into every row.
5. **`mocks` and `constructs` are `{stem}` templates filled by `_for_stem`, not by
   `str.format`.** They are regexes, and regexes are full of braces: a `{2,3}` quantifier or
   a character class like `[({]` makes `format` raise. Whoever adds a dialect should not have
   to know to escape them. What gets substituted is `re.escape(stem)`, so `{stem}` arrives
   already escaped and a class name carrying a `.` or a `+` matches literally; write the
   pattern around it, never inside it.
6. **A `constructs` match settles the whole statement**, whatever else it holds. One
   declaration can carry a mock and a real instance, and the mock's marker must not speak for
   the constructor beside it.

Eight keys map to four dialects today: `java`; `python`; `javascript`, `typescript`, `jsx`
and `tsx`, which share one row because the JSX extensions are those same languages with JSX
syntax and React has no mocking idiom of its own; and `cpp` with `c`.

**Check the row against a tree, not against the regexes.** Run `plan` over a job whose files
are in that language and read the preview: it prints every test-to-source link and how each
was derived, so a mock-only link the row should have dropped is visible there, before
anything is dispatched. A row that drops a link it should have kept is the failure to hunt
for, and the preview is where it shows.

**Build the fixture in two steps, and confirm the first before adding the second.** Two
things can make a row look like it works when it has never run.

The name rule can answer instead of it: `subject_map` takes that answer as it stands and runs
`_mock_only` over the mention pass alone, so a filename-derived link never reaches the filter.
`FooTest.rb` beside `Foo.rb` is linked by name whatever the row does. Give the test a name
that resolves to nothing.

And the mention can fail to exist at all, which looks identical to the row working. The
mention pass matches the source's **stem, case-sensitively, as a whole word**, so a
`workflow_spec.rb` containing `x = Foo.new` raises no link to `lib/foo.rb` — lowercase stem,
no match — with or without a row. So: write the fixture with the construction only, run
`plan`, and **confirm the link is there**. Then add the mock and confirm it goes. A step that
never showed a link proved nothing.

## What each row is weak at

- **C++ is the weakest.** GoogleMock has no annotation — a mock is a subclass, so the only
  place the real class appears is an inheritance clause. A hand-rolled fake that does not
  follow the `Mock` naming convention is invisible to the row, which keeps the link.
- **Java** cannot see a brace or terminator inside an annotation's arguments:
  `@MockBean(classes = {Foo.class})` splits, and the pieces stop reading as one declaration.
  Closing that means matching parentheses rather than splitting on them.
- **JavaScript and TypeScript carry the nested-template drop above**, and in the other
  direction keep a link they could drop where an import wraps over lines: `split` includes
  `{` and `}`, so the names inside a multi-line `import { … }` become pieces of their own
  that no `drop_lines` pattern can reach and no `mocks` pattern matches.
- **Python carries the one open drop above**, and both of its reproductions. Its `constructs`
  reads the position a new instance goes to, so the class built through the module the stem
  names, or used without being stored, is invisible to it. Its weakness in the other
  direction — the one that keeps a link — is having no statement terminator: pieces split on
  newlines, and a call wrapped over lines stops reading as a single mock declaration.
- **Go is absent and cannot usefully be added.** Its fakes are hand-written types carrying no
  marker, so a row would have nothing to match.

## The backstop the table does not replace

The table is the per-language rule. The language-neutral one is already in every payload and
holds wherever there is no row: each link is labeled `named for it` or `mentions it`, and
`references/coverage-auditor.md` tells the auditor plainly that a test which merely mocks a
file is evidence of nothing about it. That instruction is obeyed unevenly — one model
answered a mock-only link with no findings while another returned thirty, each resting on the
observation that the test never instantiates the class. The table is there to make that
answer deterministic, not to take the instruction's place.
