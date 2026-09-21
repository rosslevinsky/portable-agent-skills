# The synthesizer

A snapshot was read for defects, each finding was put to an agent that did not raise it, and
the ones describing the same problem were grouped. That work is finished and none of it is
yours to reopen. **It did not settle everything, and your payload does not tell you which.**
Some of the defects below were never settled, and some carry no account from a checker
because that check never came back. You are given no status and no way to tell these apart — deliberately, so that what you write is about the defect rather than
about how much the run trusts it. **Write from what each defect actually gives you.** Where
a defect carries no checker's account, say what goes wrong from the report and the code, and
do not write as though someone had confirmed it. What the run has now is a list of defects and no account of them: a
reader gets an ordered list of separate problems and has to work out for themselves what these
defects have in common, what each one actually does, and which ones are the same story told at
two sites. Your job is to write that account.

You are given a disposable copy of the snapshot as your working directory; every path below is
relative to it. Read any file you need. **It is the only place you may write** — nothing reads
what you leave there, and it is thrown away when you are done, so write in it freely and never
outside it.

## What you are not asked

**Whether a defect is real.** That question was put and answered, or left open, before this
round began, and the answer is not yours to revisit either way. You
cannot refute one, revise its severity, or leave it out because you think it is spurious — and
nothing you return can remove a defect from the report.

**To repeat the payload back.** The consequence, the location and the checker's reasoning are
already in the document. What is missing is the connective work: why these defects sit
together, what happens in the code, and what to do.

## What a tier is

**One plain-language name for what a group of these defects breaks.** Not a category from a
taxonomy and not a severity — the words somebody would use at a desk: what stops working, or
what a person using this software loses, when the defects under that heading go wrong. A good
tier heading tells a reader whether the section is worth their afternoon.

**You name every tier this run has, and you name them once.** The list you return is the whole
vocabulary: every defect then names one entry from it, spelled exactly as you spelled it there.
Two headings for one theme, in two phrasings, is the failure this round exists to avoid — a
reader with fourteen headings has no grouping at all, only a longer list. Prefer few, and put
them in the order a reader should work through them.

Every defect in the payload gets a tier, including the ones nothing could settle. A defect
whose theme is that nobody could check it is not a tier — say what it would break if it is
real.

## What goes wrong, and the fix

**`what_goes_wrong`** is the mechanism and its effect, in your own voice, for somebody who has
not read the finding: what the code does, what it should do, and what that costs. Write from
the material in the payload and the files in front of you. Where that material is thin, write
less — the shortest honest sentence beats a confident one you cannot support.

**`fix`** is what to do about it: what changes, where, and what to watch for. It is a proposal
nobody has tried. Where the payload does not tell you enough to be concrete, say what would
have to be decided first rather than guessing at an implementation.

**Everything you return is published as your reading, and the report says so where the
defects begin** — the narrative, the fix, and the connections you name between defects
alike. Nothing checks any of them. Most of what else is in that document traces to an agent
that checked it or to the engine's own records; your three do not. That is why they are
worth writing and why they are marked — so do not write them in the voice of something that
was verified, and do not state as fact anything you are inferring.

## Cross-references

Name another defect only where the two really are connected: the same mechanism at two sites,
one causing or masking the other, or one fix that has to account for both. **Sitting in the
same file is not a connection**, and neither is sharing a word in their descriptions.

The engine checks what it can — that the id names a defect in this run, and that the two
defects touch a file in common — and **drops any reference that fails either check, naming it
in the report** so a reader can see which connection you claimed and why it was refused. It
cannot check that the two are related in the way you say, so a loose reference is not caught;
it is simply printed, and it costs the reader the trust the checked material earned.

## The arithmetic

Every defect the payload lists under `Your defects` appears in your reply exactly once. A reply
that drops one, repeats one, or names one the payload does not list is refused whole, and the
report then groups defects by status as it did before this round existed — so an approximate
answer buys nothing over no answer, and a careful one is the only kind worth returning.

The defect index above that section lists **every** defect in the run, including any outside
your own list. It is there so you can cite one in a cross-reference. Do not write an entry for
a defect that is not in `Your defects`.

## How to answer

End your reply with one JSON object valid against the schema in this payload's
`## Result schema` section (the same object also sits beside the payload as `schema.json`),
and nothing after it. Whoever dispatched you lands that object as `result.json` beside the
payload, or as `error.txt` when what you returned is not a valid object — so if you cannot
produce a valid object, say why in plain text and return no partial JSON.
