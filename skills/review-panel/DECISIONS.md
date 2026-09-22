# review-panel — decisions

Every rule here refuses something a reader would expect to be allowed, or allows something a
reader would expect to be bounded, or names a property the skill does not have. None is an
oversight. Nothing in this file is read at run time — the engine, `SKILL.md` and the briefs
under `references/` are the whole runtime path — and it exists so that a change to one of
these rules starts from what the rule is actually for.

The number of rules is deliberately not stated. Nothing reads this file while the skill runs,
so no check would ever contradict a count written at the top, and a figure nothing can
contradict drifts silently from the list beneath it.

## A drive-shaped first component is refused on every host

**Proposed.** `a:b.txt` is an ordinary relative filename on Linux and macOS, so only Windows
should refuse it as a drive path. Refusing it everywhere means that name can be neither
excluded nor assigned to an area.

**Declined.** A job file is authored on one machine and run on another, and the rule that
makes that safe is that a path means the same thing wherever it is read. Refuse the shape only
where the host would misread it, and the same job file becomes valid on one platform and
invalid on another — which is exactly the failure the whole normalizer exists to prevent. The
refusal is also the conservative direction: it rejects a name, where the alternative silently
reinterprets one.

**What would change it.** Nothing about the host. A job format that could mark a path as
literal, so the drive test is answered by the file rather than guessed from the text, is the
only change that reaches this.

## A verification batch is not size-bounded

**Proposed.** Every reader payload is held to a ceiling of two thousand lines and two hundred
kilobytes. A verification batch is built the same way and has no ceiling at all, so a large
one can pass what every other payload could not.

**Declined.** The ceiling exists so that an area too large for one reader gets split into
several. A verification batch cannot be split the same way: the unit's job is to weigh one set
of findings against each other, and half the findings in each of two units answers a different
question from the one that was asked. Adding a bound with no split behind it converts an
oversized batch into a failed run.

**What would change it.** A real batch that exceeds what the model can read, which would give
the bound a number to be and force the routing question to be answered rather than deferred.
Until then the cap would only ever fire on a run that would have succeeded.

## Below the supported floor there is nowhere to put the message

**Proposed.** The prerequisites promise `Python 3.10+ required`. On Python 3.7 the engine
instead dies at a syntax error, so the promised message never reaches the person who needs it.

**Declined.** The engine's own version guard runs after the interpreter has read the file, and
the assignment expression that fails to parse is one of many constructs an interpreter that
old cannot handle. Reaching the guard first means shipping a separate launcher written in the
oldest syntax the pack might ever meet, maintained forever against a case that gets rarer
every year. The supported floor is documented where somebody looks before installing.

**What would change it.** Evidence that people actually arrive here on such an interpreter —
a supported runtime that bundles one, rather than a machine somebody could in principle be
using.

## A lens heading inside the problem statement is not rewritten

**Proposed.** The problem statement is copied into a payload verbatim, so one that contains
its own `## Lens` heading produces a payload with two of them. Only the brief's wording about
the last section tells the reader which is theirs.

**Declined.** Verbatim is the property that matters. A payload is built from exactly four
things — the brief, the problem statement as written, the area's file list, and the lens — and
a test reconstructs the bytes from those four inputs to prove nothing else got in. Rewriting a
heading inside the author's own text breaks that reconstruction and starts a category of edits
with no natural end. The ordering rule already resolves the ambiguity: the lens is last, and
the brief says so.

**What would change it.** A reader misattributing a lens in a real run. The fix then is a
delimiter the author cannot accidentally type, not an edit to their prose.

## Evidence is bounded where it is rendered, not where it is parsed

**Proposed.** The brief caps a verifier's evidence output at four kilobytes, but the parser
accepts any length and the cap is applied only when the report is written. A verifier that
ignores the cap is accepted.

**Declined.** The cap is a statement about the report, which is what a person reads, and it is
applied there — keeping the tail, which is the part that carries the failure. Refusing an
oversized result at the parse boundary throws away a verdict that is otherwise complete and
correct, because a field it barely uses came back long. Rejecting whole work over a formatting
overrun is the worse trade of the two.

**What would change it.** Oversized evidence causing harm before it is rendered — memory
pressure on a real run, say, or a verdict whose meaning depends on the part that gets cut. The
bound would then belong at the boundary, with the truncation note still explaining itself.

## A worker is not separated from its siblings, and cannot be made to be

**Proposed.** A payload contains no other unit's output, and a test proves it by
reconstructing the bytes from the payload's declared inputs. The run directory should hold
the same line: a worker reads its own payload, its own schema and the snapshot, and nothing a
sibling unit produced. Dispatch should enforce that, rather than the report claiming it.

**Declined — the boundary is not enforceable on any dispatch line here.** Measured, with a
marker planted in one reader unit's landed result and each dispatch line pointed at it from
the working directory it is given. The read-only sandbox returned the file in a handful of
milliseconds with no approval: that mode bounds what a worker may **write**, and says nothing
about what it may read. The command-line worker given an explicit list of directories
returned it too, and said in its own reply that the file lay outside every directory it had
been handed and that nothing had blocked the read — that flag widens a working set rather
than denying what is outside it. A fresh sub-agent of the driving runtime returned it on its
first tool call; a sub-agent inherits its parent's file access, and there is no boundary
between them to begin with.

Moving the bytes does not help. Holding each result outside the run directory until the round
closes relocates them, and a worker that can read the whole file system can read where they
were relocated to — along with the dispatch transcripts, the scratch the dispatcher works in,
and everything else on the host. A file-placement scheme buys an inconvenience and is reported
as a property, which is the worse of the two outcomes.

**What the run directory does guarantee, stated as narrowly as it is true.** `check_rundir`
refuses a run directory inside the audited root, inside any repository bare or not, or holding
anything but the job file it was handed — every other entry refused by name, and a symlink
refused whatever it resolves to. That is a guarantee about **where the directory sits and what
it contains**, and nothing more: what lies in the worker's own working directory is this run's
files, so the failure that destroys a run — unrelated material sitting where the report is
written and read as though it were part of the job — cannot happen by accident.

It is not a guarantee about what a worker can reach. The measurement above is unrestricted
reads, and the run directory holds a copy of the job, which names the audited root: a worker
that wanted the real repository's refs could open them directly, and the same is true of
another run's artifacts anywhere on the host. Two things are open, then, and both are stated
rather than papered over: within one run a worker is not separated from its siblings, and
nothing outside the run directory is out of reach either.

**What would change it.** A worker sandbox that bounds reads as well as writes — a per-unit
mount or jail that makes the payload, the schema and the snapshot the only paths that exist.
Then the boundary is a property of the dispatch rather than of what a worker happens to look
at, and it can be tested the way payload blindness already is.

## A clustering unit cannot drop a candidate it judges spurious

**Proposed.** Of everything in the run, the unit that groups an area's candidates is the
best placed to spot a bad one: it has every candidate in front of it at once, each with the
rationale of the agent that checked it, and it can see where three of them are restatements
of one taste. Let it return the ids it judges to be noise and leave them out of the report,
which is shorter and easier to act on for it.

**Declined.** Grouping wrongly and judging wrongly leave different amounts behind, and only
one of them leaves nothing. Split what should have been merged and a reader sees one defect
written up twice, notices the repetition and moves on. Merge what should have been split and
one description stands where two were — worse, and the reason the brief spends most of its
length on identity — but both candidates are still in the run, in one cluster, each with its
own verdict beside it. Judge wrongly here and a finding a reader raised and a stranger
confirmed leaves the report with nothing to say that it was ever there — not a line, not a
count that fails to add up. Its result would still be on disk in the run directory, which is
not the same as being in the document somebody actually reads. The person reading the report
is the one deciding what to act on, and a finding they cannot see is a decision made for
them by an agent that was asked a different question. So every candidate the unit is handed
comes back in exactly one cluster, the engine proves that before it uses the answer, and an
answer that fails the proof costs the area its merging rather than its findings.

**What would change it.** A real report abandoned half-read because of what it repeats,
with a grouping that was demonstrably good — that would show the volume costing more than
the findings are worth, which nothing so far does. Even then the answer is an ordering
rather than a deletion: what the run thinks is noise sits at the end, where it is still
there to be counted and argued with.

## The engine starts no worker, and that survived a driver being built

**Proposed.** A Python program drives every round now, so the reason to keep workers out of
the engine has gone with the hand loop that needed it. Fold the loop back in: one program,
one place to look, and no boundary to explain.

**Declined, and the boundary is narrower than it looks.** Three reasons commonly given for
it are not reasons, and saying so is most of what this entry is for.
Runtime neutrality does not require it — the adapter file supplies that, and the driver
names no product anywhere. Spawning nothing did not keep the engine small; it is the same
order of magnitude as the sibling that spawns, and which is larger has already changed once.
And dispatch inside the engine would not cost the report its determinism, because the report
is a pure function of run-directory data whatever started the workers.

What the boundary actually buys is testability, and it is worth the explanation. The engine
decides what a defect is, how files are divided and how a report renders, and **none of that
can start a process** — so the whole of it is provable without launching a model, which is
most of this skill's suite. Fold the two together and every one of those tests inherits
locks, provider outages and disk accounting. The second thing it buys is the resume: the
engine's stages are the units of work recorded on disk, and keeping *what a stage means*
apart from *how a worker is launched* is what makes a stage atomic enough to resume to.

They are not two processes. The driver imports the engine and calls its stages **in-process**
under the run lock it already holds — a subprocess would mean two programs writing one run
directory while the lock says one owns it. So the boundary costs a module import and an
explanation, and nothing at runtime.

**What would change it.** A stage that genuinely cannot be decided without knowing how its
workers were launched. Nothing has needed that yet, and the placement rule in
`references/architecture.md` exists to keep the next thing from needing it by accident.

## A synthesis unit cannot drop a defect it judges unimportant

**Proposed.** The round that assigns tiers sees every defect at once, with what each is and
what the check concluded. That is the vantage point from which a defect that is not worth a
reader's attention is obvious, and the round is already writing the order the reader works
in. Let it leave one out, or file it under a tier that means "ignore this", and the document
stops spending a heading on something nobody will act on.

**Declined.** The same rule as the clustering unit's, one stage later, and the argument is
the same argument with more to lose. A defect reaching this round has been raised by a
reader, PUT to a stranger that did not raise it, and merged into a cluster under a proof
that no candidate was dropped. Three stages have been built to make sure a finding cannot
quietly disappear. A fourth that could delete one would undo all of it at the last step, and
at the step where the least evidence remains: the round is handed no status and no severity,
so the thing it would be deleting is a defect it cannot see the standing of. So the
assignment is proved a partition of exactly the ids the round was handed before it is
believed, and a unit that failed, never landed or returned a non-partition leaves the run
grouped by status rather than dropping what it could not place. A tier the reply never
declared costs that one defect its whole assignment — the tier, the narrative, the fix and
the references it named — because an entry filed under a name the reply did not declare is
one the engine cannot place, not a good entry with one bad field. **The defect itself is
untouched**: it falls back to its status group carrying everything the earlier rounds
established about it, and the report names the refusal.

**What would change it.** Nothing about the volume, which is an ordering problem and has an
ordering answer: a tier the round declares LAST is still a tier, and a reader who stops
reading has still been shown what they stopped before. What would change it is a stage that
could establish a defect is not real. This round is not that stage — not because it cannot
open a file, since its brief gives it a disposable copy of the snapshot and tells it to read
what it needs, but because **nothing checks what it returns.** A verification is a claim one
agent makes and another settles; a synthesis is a claim nothing settles, which is why its
prose is marked as a reading. Letting it delete a defect would give the one unchecked stage
in the run the last word over three checked ones.

## The synthesis payload grows with the run, and one unit is why it grows linearly

**Proposed.** Every synthesis payload carries the whole defect index so that any defect can
be cited in a cross-reference. Split the round across several units for the usual reasons —
smaller payloads, work in parallel, a failure costing less — and each of them carries that
index again.

**Declined for now, and the shape of the growth is why.** The payload is the brief, the
problem statement and the inline schema — a **fixed part that does not grow at all** — plus
the defect index and the per-defect detail. Measured over the suite's standard fixture, the
fixed part is the large majority of a small run's payload, and the index holds a **steady
sixth** of the part that does vary, at every size from three defects to three hundred,
because an index entry and a defect's own detail block both grow with the count.

**One unit therefore grows linearly, and the index can never crowd out the detail it sits
beside.** Splitting the round into `k` units makes each carry the whole index again, so the
total is **O(kN)** — quadratic only where `k` grows with the defect count, not merely because
a split exists. Fixing `k` keeps it linear with a larger constant, and costs no
cross-reference: every payload carries the whole index precisely so a unit can cite a defect
it was not handed.

**The byte counts are deliberately not written here.** The brief is part of every payload, so
any figure recorded in this file goes stale the next time the brief is edited — and it does
not announce that it has, because nothing reads this file while the skill runs. What is
recorded instead is the command that answers the question at the tree you are standing on:

```python
# from the repository root
import sys; sys.path.insert(0, "skills/review-panel")
import review_panel as r
payload = r.render_synthesizer_payload(
    r.load_brief("synthesizer"), problem, index, defects,
    schema=r.load_schema(r.SYNTHESIZER_SCHEMA_NAME))
len(payload.encode("utf-8"))
```

Every unit the engine writes also records its own `payload_bytes` and `payload_lines` in
`units.json`, so a real run states its own size without anyone measuring anything.

**What would change it.** A run whose payload the model cannot read in one piece. The answer
then is to split the round and accept the constant, in that order: measure first, because the
number decides it and the intuition about quadratic growth does not.

## A cross-reference is checked, and checking is not the same as being true

**Proposed.** The report drops a cross-reference whose id names no defect in this run, and
one whose two defects share no file. Having checked those, print the connection plainly: the
document already says these two defects are related.

**Declined — the check is narrower than the claim, and the report says so.** What the engine
can see for itself is that the id resolves and that the two defects touch a file in common.
What the prose asserts is that the two are related **in the way it says** — the same
mechanism at two sites, one causing or masking the other, one fix that has to account for
both. Nothing in this run establishes that. Confirming it would need a stage that read both
defects and the code around them, which is a fifth round and does not exist. So a surviving
reference is **checked, not verified**, and the sentence at the top of the body that marks
the round's prose as one agent's reading covers the references too. A reference the check
refuses is dropped and named, so a reader can see what was refused rather than only what was
allowed.

**What would change it.** A round whose answer something else checks. This one can already
open both files — its brief gives it the snapshot and tells it to read what it needs — so
the gap is not access, it is that nothing settles what it concludes from them. Short of
that, the honest move is the one already made: check what is checkable, print what survives,
name what did not, and say plainly that the rest is a reading.

## A worker that outlives the run that started it is not collected

**Proposed.** Stopping a run should stop the workers it started. A sweep interrupted part-way
can leave agents reading, reproducing and spending quota against a run directory nobody is
waiting on.

**Declined.** Nothing in this skill can reach them. The engine starts no process but git, so
it has nothing to signal and no list of children to signal it with. A worker the harness
spawned belongs to the harness and is the harness's to end. A worker launched through the
supervising program is bounded by that program's deadline, which collects it eventually
rather than promptly — and only while that program is itself alive, which the rung decides:
where both slots are contexts of the host, no supervisor is in the picture at all and no
deadline applies to either. And the instruction that would do the collecting would have to
run after the thing that would execute it has been stopped, which is the one moment no prose
in a skill file runs. Writing a clean-up step anyway would be a promise the run cannot keep,
and a promise that reads as a guarantee is worse than a stated limit: somebody would stop a
sweep believing it had been tidied up.

**What would change it.** An engine that starts the workers itself, which would give it
process handles and a place to put a signal handler — the entry above is where that question
lives, and this limit is one of the things that would be bought with it. Short of that, a
harness that exposed a cancellation the dispatching agent could register against, so the
stopping and the collecting happen in the layer that owns the processes.

## A coverage gap is not judged on the defect scale, and does not borrow its words

**Considered:** routing a coverage finding to the ordinary verifier, which already exists,
already knows the tree, and already answers in four statuses the report can render. One
brief, one schema, one ladder.

**Declined.** The two questions have different answers, and the verifier that asks *is this
failure real?* answers correctly about a missing test every time: it is not one. A gap
asserts nothing about the code being wrong — a branch can be perfectly correct today and
have nothing guarding it, which is usually why nobody noticed the test was absent. Run
through the defect brief, every gap came back `refuted`, and `refuted` is rendered under a
heading that tells the reader nothing beneath it is work. Real, named, missing tests were
filed as dismissed claims, which is not a rendering problem: the pipeline asked the wrong
question and then believed the answer.

So the statuses are the gap's own — the branch exists and no test in scope builds the input,
or a named test does, or nothing could tell — and the engine refuses each set on the other's
batch rather than translating between them. A verdict that answered the wrong question did
not answer this one, and a translation layer would be the engine deciding what a worker
meant.

**What would change it.** A finding shape that carried its own question, so one brief could
branch on it without the router having to. That is a larger change than it sounds: the
shape is the reader schema, and every reader would then be writing a field only one unit
ever fills.

## An auditor is bounded by the area that owns the test, not by where the tests are read

**Considered:** giving the coverage round to every area whose payload can reach a test,
including one whose only test arrives through `also_read`. It is the same agent asking the
same question, and more areas asking it finds more gaps.

**Declined.** A gap would then be raised twice, from two directions, by two units that
cannot see each other — and the clustering round cannot merge them, because clusters never
span areas. Two entries for one missing test, in two sections, with nothing in the report
saying they are the same thing. Ownership is the rule that makes the round total without
making it overlap: the auditor sits where the tests are, and follows a test to a subject
another area owns through the `also_read` list its readers already carry.

**What would change it.** Clusters that may span areas, which is a change to the closure
proof rather than to this round — every candidate belongs to exactly one area today, and
that is what makes the batch key total.

## The file that would settle a claim is a field, not a sentence to be parsed

**Considered:** leaving the unresolved reason as it was — the verifier names the file in its
rationale — and having the report read those rationales to build the table of what one more
file would settle. No schema change, no new field, and the information is already written
down.

**Declined.** It is written down in prose, and a table built by pattern-matching prose is a
table that is confidently wrong: a class name is not a path, a sentence can name two files
or none, and a package is not a file. The table exists to be spent against — an owner
deciding whether to widen the next run's scope — and a wrong number there is worse than no
number, because no number invites reading the entries and a wrong one does not. Everywhere
else this engine refuses to infer what a worker meant; inferring it here would be the same
mistake with money attached.

**What would change it.** Nothing about parsing. A verdict that named the file some other
structured way — a reference to an inventory entry rather than a path — would change the
shape of the field and not the decision to have one.

## An abbreviated quotation is a quotation, and the ends are what is checked

**Considered:** requiring a reader to quote the whole of a cited range, which is what the
comparison had always demanded, and dropping the schema's permission to abbreviate a long
one to its first and last lines.

**Declined.** A forty-line citation would then carry forty lines into a payload that is
already the largest thing a reader is handed, and the permission exists because of that. The
comparison was simply behind the schema, and a warning raised on every abbreviated
quotation is a warning nobody acts on — which costs the ones that are real, since they sit
among them.

What is checked instead is what a range actually is: every quoted line appears in the range
in the order given, and the range's own first and last lines are among them. A quotation
that pins both ends has established the thing the check exists to establish. Leaving out
the middle is permitted; leaving out either end is not, and a one-line quotation of a
multi-line range fails because one line cannot be both ends.

**What would change it.** A reader that quoted by digest rather than by text, which would
make the check exact and the payload small at once — and would require the reader to compute
something rather than copy it, which is a different kind of instruction to obey.

## A `needs_files` entry holding a space is prose, even when it is a filename

**Considered:** admitting an entry with a space in it, since a real path may hold one, and
discriminating prose from a path some other way — by trying to open it, or by asking whether
it looks like a sentence.

**Declined.** Neither alternative is available where the check runs. Every path in this field
is by definition outside the reviewed set, so there is nothing to open it against; and a
description of a file is written in the same characters a file name is, which leaves no test
short of understanding the sentence. A space is the one signal that is cheap and almost always
right, and the angle brackets and quotes catch the same answer wearing punctuation.

The trade falls the safe way. A path this refuses costs one row in the table pricing what the
run left open. A sentence this admits costs the table its meaning: it cannot be opened, it
cannot be counted, and it sits beside real paths looking like one of them.

**What would change it.** A verifier that returned the file as a structured object rather than
a string, so that "this is a path" is something the reply states rather than something the
engine infers from the characters.

## A refused entry is silent unless it empties the list

**Proposed.** Report every refused entry to the dispatcher, so a verifier that answered one of
three in prose is told about it.

**Declined.** There is no channel for it that does not lie. The only per-verdict report the
engine carries is the rejection list, and both the report and `check` render an entry there as
a verdict that could not be read and cost its candidate — which would be false for a verdict
that was read perfectly and lost one row of a table. Adding a second channel means a second
thing every renderer has to know about, for an event that costs one row.

So a refused entry beside a path that stood is dropped silently, and one that empties the list
is named in full — in the refusal that was already going to fire, under the rule about a
verdict that needs a file outside the scope and names none. That is the case where the entry
actually cost something, and it is the case a dispatcher can act on.

**What would change it.** A per-verdict note channel distinct from rejection, which would earn
its place if anything else ever needed one.

## An unstated `run_kind` is refused with an executed one

**Proposed.** Treat evidence whose `run_kind` is absent as documentary on a
`confirmed_by_reading` verdict, since a verifier that ran the code would more likely say so.

**Declined.** The permission exists for one act — searching the tree to check a reading — and
that act is narrow enough to be named. Reading silence as the permissive answer would make the
rule enforce nothing: a verifier that ran the code under review and omitted the field lands the
strongest evidence it has under the weakest status, which is the error the rule is for.
Refusing costs a verdict and says exactly which field would have saved it.

**What would change it.** Making `run_kind` required on evidence, which would remove the
silence rather than interpret it — and would refuse every verdict written before the field
existed.

## A wrong quotation is not searched for elsewhere in the file

**Considered:** reporting the range the quotation *would* have matched, so a citation off by
twenty lines names its real location instead of only failing.

**Declined.** The comparison reads the cited slice and nothing else, and that bound is what
makes it cheap enough to run over every finding in a result at landing time. Searching a file
for a quotation is a different operation with a different cost, and it has no answer for the
common case — a quotation that matches nowhere, because it was reflowed rather than misplaced.

What is reported instead is where the two came apart: the position in the quotation, and the
line number in the file the range had reached. That distinguishes the two repairs a dispatcher
is choosing between, which is what the printout is for.

**What would change it.** A reader that cited by digest, the same change that reaches the
abbreviation rule above.
## A test's subject is derived, and the looser half of the derivation still runs

**Considered:** resolving a test to its subject by name alone, and using the text of the
test only where the name answers nothing.

**Declined.** A file name encodes one subject. A test exercising two classes can name one of
them in its own name, and a rule that stops as soon as it has an answer misses the second
every time — which is a source file audited by nobody, the failure this whole arrangement
exists to remove. So the mention pass runs BESIDE the name rule rather than after it, and
the two answers are unioned.

What that costs is the other direction, and it is real: a test names the classes it mocks
and imports as well as the one it exercises, so a source file can be linked to a test that
merely stands next to it. Two things hold it: every link carries which rule produced it, and
the plan preview prints the whole map before a single unit is dispatched. A person who knows
the code reading one line is the only thing that can tell a wrong guess from a right one, and
that is the moment to do it.

The weakness is sharpest where a file stem is an ordinary word — a `run.py` is linked by any
test whose text contains `run`. It is bounded by "mentioned in a test in scope", which is far
tighter than the rule this replaced, where every file in an area was the auditor's business.

**What would change it.** A job that states the links, which is what subject mode already is.

## Subject mode derives nothing

**Proposed.** Derive test-to-subject links in both partition modes, so one rule decides where
an auditor goes however the areas were chosen.

**Declined.** In subject mode a person wrote the areas and wrote `also_read`, which is the
same statement — these tests belong with that code — made by somebody who knows the tree.
Deriving it again would silently overrule a deliberate choice with a guess, and the guess is
the thing with a known false-positive rate.

The rule that differs by mode is settled once, in `partition`, where the mode is known, and
carried on the area as `audited`. Every later caller asks the area rather than re-deriving
it, so the two rules cannot drift into three.

**What would change it.** A subject-mode job that asked for derivation explicitly, which is a
job-format change rather than a rule change.

## An auditor may file only in the files it was asked about

**Considered:** letting a coverage auditor locate a finding anywhere in the snapshot, as a
reader may.

**Declined.** It is what makes many-to-many safe. One test can be about two source files in
two areas, so the same test is listed for two auditors, and without this rule each could
raise the same gap against the other's file — two findings, from units that cannot see each
other, that no clustering round would know to merge.

A misplaced finding costs itself and the unit lands with the rest, by the same rule as a
location outside the snapshot. The auditor answered; one finding in the wrong file is not
grounds for discarding the thirty beside it.

**What would change it.** Nothing about the auditor. A clustering round that could merge
across areas would remove the duplication argument, and it would still leave the bounding
argument standing.

## An untested file is not shown to the auditor at all

**Proposed.** List every file of the area, marking which have tests, so the auditor can see
what it is not being asked about.

**Declined.** That is what produced the noise. An auditor that can see an untested file
beside a tested one writes "no test constructs this input" about it, which is true of every
line of a file nothing tests and is true because the file has no test rather than because of
anything the auditor read. One area returned 72 such findings about one file, and a verifier
then confirmed all 72 with the same sentence each time.

What a run is missing tests for is a question the report already answers from the inventory,
where it costs no agent anything.

**What would change it.** Nothing. A file with no test in scope is not a coverage gap the
panel can characterize; it is a fact about the job's scope.

## The job block names the root as a field and not as a path

**Proposed.** "The job" states every other field of the job it renders. The root directory is
the one a reader most wants — which tree was this? — so spell it out there.

**Declined.** The page states exactly one absolute path, the run directory, so that a reader
holding the report weeks later has a way back to the payloads and the transcripts. Everything
else is relative to the reviewed root, and a second absolute path is what the check on that
invariant exists to catch: a report is read on somebody else's machine, where the operator's
filesystem is not information but exposure.

**The printed job is where this now bites, and it is the only field substituted there.** That
block exists to be pasted back to a runtime, so every other field is verbatim; `root` renders as
a placeholder. The cost is real and small: one edit before the paste. It is also the field a
reader would most often have had to change anyway — the tree is on their disk, at their path,
and a job carrying somebody else's absolute root is a job that fails on the first run.

The commit and its date are not withheld by this: they are in the subtitle under the title,
and the whole sha is under "How this ran".

**What would change it.** A report meant to be read only where it was produced, which is the
opposite of what the page is for — or a decision that a pasteable job matters more than the
one-path property, which is the trade this entry is the record of. Nothing about the job format
reaches it.

## The job is in the appendix, not beside the problem statement

**Proposed.** The statement a reader acts on is quoted under the title. The rest of the job
decides what every coverage claim below it means, so put the whole of it there too, where
nobody can miss it.

**Declined.** The appendix is defined as the record of the run rather than the work it found,
and nothing in the job is needed to fix a defect — a subject-mode job runs to a screen of area
names, file counts and per-area lenses, which is a wall between the statement and the first
thing a reader came for. The path legend was moved out of that position for the same reason and
by the same rule.

What makes it a move and not a burial is the paragraph under the statement, and that paragraph
is the part to protect: it says the statement is one field of a job, names the section, says who
settled the other fields and when, and carries the shape of the run in one sentence. **That
sentence stays up front whatever else moves** — the summary counts areas, and whether an area is
a slice the engine cut or a subject somebody named is what those numbers mean, so a reader sent
to the appendix to find out is reading numbers they cannot interpret where they sit.

**What would change it.** A reader who cannot follow a pointer, which is a different document
from this one. Shortening the pointer to a bare "see the appendix" is what would undo it, and
that is the change to refuse.
