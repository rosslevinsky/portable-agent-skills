# Which review skill does what

Five skills in this pack could each be called "a review", and the phase gate in `/plan-run`
runs two of them. They are not interchangeable. Each skill's own file describes only that
skill and does not compare it with the others; this page does.

Read the chart below to decide which skill to use.

"Review" here covers everything from a model re-reading its own work to two different
models each trying to disprove the other's findings.

## Three meanings of "adversarial"

The word can mean three different things. The pack varies the first two freely. The third,
an attack on a finding somebody has already raised, is rare, and it is the difference
between these skills that matters.

- **The reviewer has fresh context and does not know the history behind the work.** The
  author's reasoning is not visible to it, so it cannot be persuaded by it.
- **The reviewer is a different model from the one that produced the work.** A different
  model fails in different places. This needs both the `claude` and `codex` command-line
  tools installed.
- **The reviewer has been told to find something wrong.** It is trying to prove that a bug
  exists or that the design as a whole is unsuitable, rather than reading the work over.

## Three questions pick the skill

1. **What is being read?** A plan, a change set, or a whole tree of files.
2. **How far is the reviewer from the author?** The same conversation, a separate context,
   or a different model. Cost rises with each step, because each step is another separate
   context to run.
3. **What has to be true before a finding counts?** It was asserted from reading, confirmed
   by running the code or a test, or it survived an attempt to disprove it.

## Skills compared

| Skill | What it reads | Cost | Needs both runtimes? | Who reads the work | Reader adversarial? | Who checks a finding | Is the check adversarial, and what it takes |
|---|---|---|---|---|---|---|---|
| `/cyw` | The change you just made | Lowest. One pass, inside the conversation that did the work | No | One model: the one that wrote the code, which knows every reason it was written that way | **No.** It is checking its own work | Nobody separate | **No check exists.** A finding must name a concrete harm; one that cannot is recorded and deliberately left unfixed |
| `/diff-review` | One change set | One separate reviewer | Optional. With both installed, the reviewer is the other model | One model, which is never shown the authoring conversation | **Yes, toward the code** | Nobody | **No check exists.** The finder is the only one who assesses it, and runs the confirming test where one exists |
| `/security-review-codebase` | The whole tree | One pass, or four to eight component reviews in deep mode | No. One model throughout | One model: whichever one is hosting the run, with a fresh context per component in deep mode | **Yes, toward the code.** It looks for vulnerabilities by category | A work unit that did not produce the finding. Same model, fresh context | **Yes.** It must show a concrete attack path — input, route, line — and rate its confidence at 0.8 or higher. Many findings are expected to fail |
| `/plan-duel` | A plan, before any code exists | Highest. Three model calls per round, up to ten rounds | **Yes.** Both command-line tools must be installed | Two models, each writing a plan and criticizing the other's. Each one sees the other's plan | **Yes, in both directions.** Criticism is the mechanism | The opposing model, every round | **Yes.** It must make an argument that convinces a third judge, which scores the plan out of ten |
| `/review-panel` | Any set of files you name | Two readers per area, plus one check for every finding | Optional. With both installed, the checker is the other model | Two models, both reading every area. Neither can see the other's work. Each is given a different angle to read for | **No, and deliberately.** They read independently; the challenge happens at the next stage | Never the finder. The other model, or the claim run in a separate copy of the tree | **Yes.** It must reproduce the claim and report the command, exit status and output, or else refute it |

A review has two stages: reading the work, and checking a finding somebody raised. The two
are independent, and the two adversarial columns show it. Read those columns across rather
than down. **Only `/plan-duel` is adversarial in both**, and it gets there by letting the two
models see each other's plans, so neither reads independently. `/review-panel` makes the
opposite choice: readers who cannot see each other, and the adversarial work aimed at the
findings instead.

## `/plan-run`'s phase gate, which you do not choose

`/plan-run` is not a review skill, and it is left out of the chart above on purpose. It
executes a plan, and at the end of **every phase** it stops and runs reviews for you. That
makes it where most reviewing in this pack happens. It is worth understanding separately,
because it does not have the shape of a single review.

**It runs two reviews, and they are opposites.**

1. **The author checks its own work** with `/cyw`, cut down to a single pass. The model that
   just wrote the code re-reads it, knowing every reason it was written that way. Not
   adversarial, and not meant to be.
2. **A separate reviewer checks the diff** with `/diff-review`. That reviewer is never shown
   the authoring conversation, and it is a different model where both runtimes are
   installed. Adversarial.

So the gate is not somewhere between the two rows in the chart. It is **both rows, run one
after the other**, on every phase, automatically. That is what the gate is for: you get the
cheap self-check and the expensive independent check without having to remember either.

**What happens to a finding is the weak part.** Whichever review raised it, the loop is
capped at two passes, and then the **author** decides. The author is the one party whose own
code is in question, and the least independent challenger anywhere on this page. Three rules
keep that honest. The author may fix the finding. The author may refute it, with evidence
written into the phase document. The author may **never** quietly downgrade it. A finding it
can neither fix nor refute stays **open**: the phase's box is left unticked, and nothing is
committed past an open blocker.

This is the least independent check in the pack, and it is the one that decides whether a
commit happens.

## Notes on each row

**`/cyw` is the weakest, deliberately.** It runs inside the conversation that did the work
and knows the reasoning, so it is good at "did I do what I said" and bad at "was what I said
right". It has one real safeguard: an issue that cannot be stated as a concrete harm is
recorded and left unfixed, because a review that changes code it cannot show to be wrong
makes the work worse. A phase gate runs it cut down to a single pass.

**`/diff-review` is the only skill that reaches a different runtime.** At its strongest level
it launches the other tool's command-line program under a small bundled supervisor. Two
models are involved, one that wrote the code and one that reads it, but only one model
reviews. No stage tries to disprove the findings; the nearest thing is asking the finder to
run the confirming test and say which findings it confirmed. It degrades in steps: with one
runtime you get a separate reviewer of the same model, and if no separate reviewer can be
started at all, the review happens in the authoring context and is reported as having done
so.

**`/security-review-codebase` uses one model throughout.** This is the row most often
misread. Its sub-agents are separate contexts inside whatever runtime is hosting it, and
nothing in it dispatches to the other tool. Its finding check is stronger than it first
appears: a finding is routed to a work unit that did not produce it, on the stated grounds
that the context which found a vulnerability judges it poorly, with a real burden of proof
and the expectation that many findings will fail. Same model, separate context, not the
finder.

**`/plan-duel` works at the design stage, and is the only one where the two models can see
each other's work.** Mutual criticism is the mechanism, so independence is traded away on
purpose. The judge is a third party whose verdict must fit a fixed structure, and which plan
came from which model is withheld from everyone, the judge included, until the summary is
written.

**`/review-panel` does two things nothing else here does.** First, it proves its coverage:
every file is accounted for, and the run fails, naming the path, if anything was left
unassigned. Second, checking a finding is routed away from whoever raised it, to the other
model where two are available, or to running the claim in a separate copy of the tree. Both
models read every area, neither can see the other, and each is given a different angle. It
reports only; it never edits.

## What this pack does not give you

The chart above reads as more reassuring than it is, so this section says what is missing.

- **Independent readers are well covered. Independent *challengers* are not.** `/cyw` and
  `/diff-review`, the two you will reach for most often, do not challenge findings at all;
  whoever raised a finding is the only one who ever looks at it. `/security-review-codebase`
  routes the challenge away from the finder but cannot change models. And the gate that
  blocks a commit hands the finding to the author, as the section above describes.
- **Only one of them proves it looked everywhere.** A diff review is bounded by the diff.
  `/security-review-codebase` splits a codebase into components by judgment, and nothing
  checks that those components add up to the whole codebase. `/review-panel` is the only one
  that proves its coverage.
- **`/tdd` and `/web-verify` are not reviews**, and they are left out of this page on
  purpose. They produce evidence, a test that failed before it passed or a screenshot you can
  look at, which the review skills then use.

## Which one when

| Situation | Skill |
|---|---|
| You just finished something and the context is still loaded | `/cyw` |
| You are at a phase or pull-request boundary | `/diff-review`. `/plan-run` runs it and `/cyw` automatically at every gate |
| You face a large decision and no code exists yet | `/plan-duel` |
| You are about to publish | `/security-review-codebase` for vulnerabilities, `/review-panel` for correctness. They do not substitute for each other |

One overlap is worth watching: at a gate, `/cyw` and `/diff-review` both read the same diff.
They ask different questions of it, so the overlap is defensible, but it is the first place
you would notice duplicated effort.
