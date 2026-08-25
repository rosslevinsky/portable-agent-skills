# Test Fixture: Unterminated Code Fence, mid-file (must FAIL)

The companion fixture beside this one has a single fence, which is the easy shape to
detect. This one has three, and the unbalanced one is the
**first** — the case the scan used to re-pair its way past in silence.

A fence opens here and is never closed:

```bash
python3 install.py

Ordinary prose. To the scan, this is still inside the block above.

```bash
python3 install.py --verify
```

The opener on the second block carries an info string, so it cannot act as a closer and
is swallowed as code. The bare delimiter after it closes the FIRST block instead, and
everything from here down re-pairs as though the file were balanced.

```bash
python3 install.py --uninstall
```

Five delimiters, an odd number, so one fence is unterminated. The state machine ends
cleanly and reports nothing; parity is what catches it.
