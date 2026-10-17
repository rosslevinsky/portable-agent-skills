## Writing the failing test

This run asks for failing tests, and states its threshold under `## Tests to write`. Write
one for each candidate you establish at or above it on the severity you settle on; for every
other candidate `test` is null. Your copy is where you write it, as it is where you run a
reproduction; the snapshot stays untouched.

Write the test in your copy the way the payload's test conventions say. Prefer a new file,
one per candidate, never one another candidate in the batch writes, and never edit the code
under review. Name a new file and its tests after the behavior they check, as the
conventions' `naming` line says, never after anything from this review: no candidate, defect
or unit id, and no word such as review or verify. List every file you wrote in `files`.

Run it once. It is red only when it fails by assertion, for the reason you state: an import
error or a typo also exits non-zero, and is not red. A red test is a run of the code, so the
verdict is `reproduced`. Copy into `expected_failure` the exact text the failing run printed
that shows the defect, short enough to survive the output bound: the engine looks for it in
`output` and sets the test aside where it is missing. Keep `output` to the evidence's 4 KiB
tail, marking `truncated`. In `fails_because`, say in one sentence which assertion fails and
why that is the defect.

A test that passes on the snapshot is evidence against the claim, so reconsider the verdict.
Where nothing can run, write the test anyway, give the command that would run it, and say
`written_not_run` with a null exit status.
