# diff-review — decisions

One rule in the supervisor throws away work on purpose, which is the kind of thing a reader
proposes softening without knowing what it is protecting. Nothing here runs: the review works
from `SKILL.md`, `review_runner.py` and the schema. This is for whoever wants to change that
rule.

## A failed terminal event discards the review, complete or not

**Proposed.** When the reviewer's final event reports failure, the supervisor reports the
whole run as an error and writes no findings — even where the reviewer had already produced a
complete review and the failure arrived afterwards. A review that finished should not be lost
to something that happened after it finished.

**Declined.** The supervisor cannot tell those two cases apart. A review cut off halfway and a
review that finished and then hit a failure produce the same thing on the wire: some
assistant text, then a terminal event saying it went wrong. Nothing in the stream marks the
review as complete — completeness is a judgment about content, and the supervisor
deliberately makes no judgments about content. Keeping the text whenever it *looks* finished
means sometimes reporting a truncated review as a clean one, which is the single trade this
skill says never to make. Discarding is the conservative direction: it costs a re-run, where
the other error costs a defect shipped on the strength of a review that never happened.

Worth knowing, for anyone reading the code to fix this: the failure almost never arrives the
way the proposal assumes. Recorded runs show the terminal event reporting success in its type
field and carrying the failure in a separate error flag — a usage limit, an interrupted
session — so a fix written against the type field alone would change nothing that happens in
practice.

**The text is not lost, though.** Discarding the verdict need not take the reviewer's words
with it. A failed transcript-mode run writes what the reviewer had produced to
the findings path plus `.partial` — or the next free name after it, since the path is claimed
and never overwritten, so an earlier failure's transcript survives a later one — and names the
path it actually used in the status line under its own key, while
`findings` stays null. So the conservative answer is unchanged — nothing is accepted as clean,
and the exit status is what it was — and a person can still read what the reviewer managed
before it stopped. The separate name is the point: `--findings` means a review completed, and
a caller that tests for that file rather than reading the status must not be able to find a
truncated review there.

**What would change the rest of it.** A way to know a review is complete without reading it —
a runtime that reports how its turn ended, or a structured result the reviewer fills in only
when it is done. Then "complete, then failed" stops being a guess, and the supervisor can keep
a finished review instead of preserving it under a name that warns you about it.
