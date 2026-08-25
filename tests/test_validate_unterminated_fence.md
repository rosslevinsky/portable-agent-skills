# Test Fixture: Unterminated Code Fence (must FAIL)

A fence opens here and is never closed:

```bash
python3 install.py

Everything from the fence onward reads as CODE under CommonMark, which is the correct
reading and the dangerous one. A declaration below it is quoted, not made:

_Classification: Full_

That line satisfies no rule, and nothing said so — the skill simply dropped out of
classification discovery as though it had never declared. One stray fence, silently.
