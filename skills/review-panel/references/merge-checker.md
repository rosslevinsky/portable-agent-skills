# The merge checker

A snapshot of a code tree was read for defects, each finding was checked, and findings about
the same place were grouped into **sites**. Another agent then proposed that some sites are
**the same mistake made in several places**, and grouped them, stating the shared mechanism
once and how each site instantiates it. Nobody has checked that proposal. **You check it.**

You are not asked whether any site is real — that was settled per site, and each site's
outcome is given. You are asked whether the sites in each group are genuinely one mistake.

Your working directory is the snapshot. Open any file you need to decide. Write nothing.

## What to decide

For **every site in every group**: does this site instantiate the stated mechanism, such that
the stated corrective rule addresses its whole claim? Answer `fits` or `does_not_fit`, with a
one-sentence reason. Judge each site against the stated mechanism itself, not against the
other sites: a site that resembles a neighbor but not the mechanism does not fit.

For **each group**:
- `hidden_claims`: any site whose own material asserts an independent second mistake that the
  shared account and fix would hide. Name the site and the hidden claim. Empty if none.
- `fix_touches_refuted`: true if the corrective rule, applied as stated, would change code at
  a site whose outcome is refuted. Say which.

A shared symptom, topic or API is not a shared mechanism. A test that merely fails to exercise
a case is not a site of the code mistake; a test whose expected value encodes the mistake is.

## How to answer

End your reply with one JSON object valid against the result schema at the end of this
payload, and nothing after it. Its shape:

```json
{
  "groups": [
    {"group": "G1",
     "sites": [{"site": "S1", "verdict": "fits", "reason": "why S1 is this mistake"},
               {"site": "S2", "verdict": "does_not_fit", "reason": "why S2 is not"}],
     "hidden_claims": [{"site": "S1", "claim": "the second claim S1 also makes"}],
     "fix_touches_refuted": {"value": false, "sites": []}}
  ],
  "summary": "one paragraph: how many groups you upheld whole, how many sites you removed, and why"
}
```

Every group listed below gets exactly one entry, with one record for every one of its sites.
A reply that leaves out a group or a site, names one twice, or names one not listed is
discarded whole.
