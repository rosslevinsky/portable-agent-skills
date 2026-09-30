# The merger

A snapshot of a code tree was read for defects. Each finding was checked by an agent that did
not raise it, and findings that describe the same problem **at the same place** were grouped
into **sites**. That work is finished and none of it is yours to reopen.

One mistake is often made in several places: the same wrong formula in several modules, a
document restating it, a test whose expected value encodes it. Each of those places is a
separate site below, because a site is one place. **Your job is to say which sites are the
same mistake**, so that the report can present each mistake once, with every place it has to
be fixed. Nothing else.

The snapshot is your working directory; every path below is relative to it. Open any file you
need. Write nothing anywhere.

## What you are not asked

Whether a site is real, how severe it is, or whether it is worth reporting. Other stages
answered those, and you cannot remove a site: every site you are given appears in exactly one
of your groups, its own if it matches nothing.

## The rule

Put sites in one group only when each one instantiates **the same specific violated rule**
through **the same failure mechanism**, and **the same corrective rule addresses the whole
claim at every site**. Locations, names and concrete values may differ, and fixing the group
may take several edits in several files. State the mechanism once, and say in one sentence
per site how that site instantiates it.

A shared symptom, topic, API or broad remedy is **not** enough. Two different causes of the
same symptom stay apart. When you cannot state the common mechanism, keep the sites apart.

Two examples:
- the same wrong formula used in several modules, restated in a document, and asserted by a
  test's expected value: **one group**;
- the same wrong output, caused in one place by a wrong formula and in another by a unit
  conversion: **two groups**.

Three boundaries:
- **Each site's kind is stated.** A defect and a coverage gap are never in one group.
- **A test that merely fails to exercise a case is not a site of the code mistake.** It is a
  missing test, a different kind of finding; keep it apart. A test whose expected value, or
  whose own reference computation, encodes the mistake *is* a site of it.
- **Compound sites.** If a site's own material asserts a second, independent mistake beyond
  its consequence line — something the group's corrective rule would not fix — list it under
  `compound` with one sentence naming the second claim, and keep that site in a group of its
  own. Merging it would hide the second claim.

Do not group by chains of resemblance: A resembling B and B resembling C does not make one
mistake. Check every site in a group against the one mechanism you state.

## The arithmetic

Every site id listed under `Your sites` appears in exactly one group; no group is empty; every
group of two or more has a mechanism and exactly one instance per site; no compound site is in
a group of two or more. A reply that breaks any of these is discarded whole.

## How to answer

End your reply with one JSON object valid against the result schema at the end of this
payload, and nothing after it. Its shape:

```json
{
  "groups": [
    {"sites": ["S1", "S2"],
     "mechanism": "the violated rule, how it fails, and the corrective rule, stated once",
     "instances": [{"site": "S1", "instance": "how S1 instantiates it"},
                   {"site": "S2", "instance": "how S2 instantiates it"}],
     "reason_kept_apart": null},
    {"sites": ["S3"], "mechanism": null, "instances": null,
     "reason_kept_apart": "what differs from S1, where you considered it"},
    {"sites": ["S7"], "mechanism": null, "instances": null,
     "reason_kept_apart": "it also makes a second, independent claim"}
  ],
  "compound": [{"site": "S7", "second_claim": "the second, independent claim this site makes"}],
  "summary": "one paragraph: how many groups of two or more, the largest, and any hard calls"
}
```
