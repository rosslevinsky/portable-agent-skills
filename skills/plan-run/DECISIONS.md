# plan-run — decisions

Why parts of this skill are shaped the way they are. Nothing here is an instruction and
nothing on the runtime path reads it: a run works from `SKILL.md` and the three references
alone. This is for whoever proposes changing one of these rules — several of them look
gratuitous until you know which simpler version was tried and how it failed, and each has
been re-proposed by at least one review round since.

## Why the push check is what it is

The check in `Publish` is:

```bash
BRANCH=$(git rev-parse --abbrev-ref HEAD)
if [ "$(git rev-parse HEAD)" != "$(git rev-parse --verify --quiet "origin/$BRANCH")" ]; then
  git push origin HEAD
fi
```

Three simpler formulations look right and are not.

**Gating the push on whether a commit was made.** A reconciling phase carries its merge even
when it committed nothing of its own, and gating there would strand it locally while the
tracker calls the phase done.

**Reusing a SHA recorded earlier in the phase.** Shell state does not outlive the tool call
that set it, and `Select` enters `Publish` without having run the earlier transitions.

**`git branch -r --contains HEAD`.** It proves only that *some* remote branch has the commit,
so a branch fast-forwarded onto a pushed lane branch reads as published while the destination
is still behind. Comparing against the destination ref instead also gets the new-branch case
right for free: a ref that does not exist yet compares unequal, so the branch gets pushed.

**And one accepted limit.** The check asks whether the tip is published, not whether this
phase advanced the branch, so an unrelated unpushed commit gets published too. That is
accepted, because the alternative needs a pre-crash SHA and an unpublished tip is the
deadlock this prevents.

## Why the tracker carries no format marker

`plan.md`'s `| Format | v2 |` row plus the tracker's filename — `execution.md`, never
`phases.md` — is the whole v1↔v2 non-collision mechanism. A marker inside the tracker would
decide scope from the file's *contents*, which is how a parsing bug once became a silently
skipped check.

## Why a phase watches the round its box names

A phase whose own Work pushes a different repository — a release branch on a public remote,
say — is gated by that repository's round, while the push at the end of `Publish` may carry
nothing but plan metadata. Ticking the box off *this* repository's green run is the same
failure as never checking it at all: both record a verification that did not happen.

## Why the phase document is authoritative and the tracker derived

Two levels of record, the child authoritative, is what makes a crash recoverable at any point
with no status field to keep in step. A partly-ticked document *is* "in progress", at finer
grain than any status value could express, and a box that never got ticked is corroborated
against the document rather than walked past.

## Why the worker's two permission flags are not optional

A worker's whole job is editing the tree, and the default sandbox mode is read-only for an
untrusted directory — so without an explicit write scope the worker returns a clean exit
having written nothing, which is indistinguishable from a phase with no work in it. Pinning
the approval policy keeps that grant bounded to the named directory instead of letting the
model escalate out of it.

## Why blocking has no status word in the tracker

Blocking is a conversation with a human, not durable machine state. A status word records far
less than a sentence in the phase document the next run is going to re-read anyway, and two
places to look for "why did this stop" is one too many.

## Why the tracker tick is left uncommitted, and why nothing else in it changes

A resumed run reads `execution.md` from disk rather than from git history, so the tick is
visible immediately and the next phase's staging sweeps it up. There is no status block and no
count to store either: "N of M" is derivable by counting boxes, and a stored count is a second
copy of the same fact that can disagree with the first.

## Why every gate box gets ticked, including a skipped axis

A box nobody was assigned to tick is worse than a missing rule. A resumed run reads it as
unfinished and re-runs a phase that is already done — so an axis skipped by design and an axis
that never ran must not look the same on disk.

## Why the Codex worker dispatch keeps `--output-schema`

It looks free to delete. Deleting it is wrong, for a reason that only shows up when measured.

Codex coerces **every** `agent_message` under that flag. A real phase worker, run twice,
identical but for it:

```
without   4 narration messages, then the result object
          "The new unit test is correctly red: ModuleNotFoundError: No module named 'greet'"
with      5 messages, all five complete DONE objects — the first claiming the phase
          finished before the worker had decided what to do
```

So the flag destroys the work log and replaces it with four false claims of completion. That
reads like a reason to drop it, and `diff-review` **does** drop it for exactly that cost: a
reviewer's reasoning *is* its product, so coercing it destroys the deliverable.

Here the trade runs the other way, and the difference is what the dispatch is for. A worker's
product is the working tree; the returned object only reports it, and **only the final message
is ever read** — `--output-last-message` on this path, the sub-agent's final message
in-harness. Nothing reads an intermediate one, so those false `DONE`s are unreachable rather
than dangerous. Nor is narration the debugging channel: that is the per-phase progress file,
written by explicit tool actions, which coercion never touches.

The asymmetry between the runtimes is measured rather than assumed, and it is why the two
skills settle this differently. Claude keeps prose in its assistant messages and puts the
object on a separate structured-output channel, so enforcement costs it nothing. Codex has one
channel, and the schema takes it.
