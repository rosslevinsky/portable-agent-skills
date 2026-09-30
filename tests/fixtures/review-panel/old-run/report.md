# Review panel report

Generated 2026-09-27 22:44:41 UTC · 7 files from a tree that is not a repository

review-panel was given this prompt:

> Find every place the helper's result is wrong, and every document that says so.

This run read the 7 files under the job's root as 3 areas from the 3 subjects the job named, under the 2 lenses the job names. The rest of the job is in the appendix, at [this link](#the-job): the root, what was excluded and the instruction each reader was handed, together with the job itself and what it takes to run the same audit again.

Those fields were settled before the panel read anything, and not by the panel: either in an interview with whoever asked for the audit, or handed over ready-made as a job file. Nothing in the run directory records which.

## 1. Report description

A panel of agents read this tree in bounded pieces. Whatever one agent raised was sent to another agent that had not raised it, to be checked.
A **defect** here is one thing to fix. Reports that describe the same problem are merged into one defect, and the appendix sets out how they were merged, so the count can be challenged.
**Established** means a checker that had not raised the claim upheld it. **Unresolved** means nothing settled it, and the group it sits under says what would. **Refuted** means a check dismissed it, so there is nothing to do about it; it is listed in the appendix with the reason.
The **appendix** at the end is the record of the run rather than the work it found. Nothing in it is needed to fix anything.

- By status: 3 established, 1 refuted, 2 unresolved.
  - The operator corrected 2 of these: [D2](#D2), [D6](#D6). The counts are the panel's own and stay as they are; the corrections are under **The operator's notes**.
- Candidates behind them: 8 — 2 reproduced, 2 confirmed by reading, 2 refuted, 2 unresolved.
- Defects: 6 from 8 candidates; every candidate is in exactly one cluster.
- Coverage gaps: 1 from 1 finding — 1 standing, 0 covered by a test already, 0 unresolved. They are tests to write, not defects, and are counted apart from the line above.
- Reading units: 8 — 8 complete, 0 failed, 0 missing.
- Verification units: 5 — 5 complete, 0 failed, 0 missing.
- Clustering units: 3 — 3 complete, 0 failed, 0 missing.
- Rung: two runtimes. Reading units: 8 — 8 complete, 0 failed, 0 missing; 3 of 3 areas read by both models. Candidates: 8 — 6 checked by the other model, 2 left unresolved. Where the lanes disagree, the disagreement is between two different models.
- Executability, as a separate unit found it in a disposable copy of the snapshot: this tree builds — yes; its tests run — yes.
  - What the probe reported: Compiles; the suite runs and its one test fails to import.
  - **Corrected by the operator**, on whether it builds; see [The operator's corrections](#the-operator-s-corrections).
- Reproductions: 2 proposed, 2 run — 2 executed. Whether this tree builds and its tests run, how many reproductions were proposed and how many ran are three separate facts, and each is counted on its own.
- Reachability: the reviewed set was not compared with the files the repository tracks, so it is unknown whether anything that reaches these defects was left out.
- This report names every file by a short name; the **Path legend**, in the appendix, gives each one's full path.

### The judgment round's overview

One paragraph the judgment round wrote about the run as a whole. It is a reading, and nobody checked it: only the defects it names were verified, each on its own entry below.

Two tiers: a wrong result, and documents that do not say what the code does.

### Defects by tier

What the run found, grouped by the themes the judgment round named. The table holds counts only; the three views below are the lists to work from.


| Tier | Blocker | Major | Minor | Nit | Defects |
|---|---|---|---|---|---|
| A result is wrong | 1 | 2 | 1 | 0 | 4 |
| The documents do not say what the code does | 0 | 0 | 2 | 0 | 2 |

### What this report does not tell you

**What this run covered.** Readers covered all 7 files in scope. 1 more file was excluded by the job or skipped. **Coverage**, in the appendix, names them. 2 defects were checked and not settled either way; see **Unresolved**.

**What no run can tell you: what it missed.** A panel reports what its readers happened to raise; it does not look for any particular bug, so one in a file it read can still be absent here, and a longer report is not a more complete one. To learn whether a run would catch a bug that matters to you, give it one you already know about and see whether it appears.

## 2. The operator's notes

Written by whoever ran the panel, after the run, with this report, the code and the owner's questions in view. It is their own reading and nothing checked it: only the defects it cites were verified, each on its own entry. The counts in the section above are the panel's, and nothing here changes them.

### Answers to the owner's questions

The job asked:

> Is the helper's doubling ever right?


#### Q1. Is the helper's doubling ever right?

No. D2 is the doubling, and D7 is the guide that never says so.

Cites [D2](#D2), [D7](#D7).

### The operator's corrections

Each is the operator's reason for disagreeing with what the panel concluded. The entry it names carries a mark pointing here, and is otherwise as the panel left it.

- To [D2](#D2), marked established by the panel: The contract changed last month; doubling was once right.
- To [D6](#D6), marked refuted by the panel: The README is shipped, so its claims are the product's.
- To the build check's answer on whether this tree builds, which was yes: It built only because nothing here is compiled.

### Caveats about the whole run

- The tree is a fixture, read as it stands.

## 3. Indices

One list of defects, with two ways in: by rank or by file.

### Every defect — most severe first

Every defect that needs work, most severe first and, within a severity, cheapest fix first. So the first rows are the blockers, in the order to take them on.

1 refuted defect is not here. Nothing needs doing about it, so it is listed under Refuted, in the appendix, with the reason it was dismissed.

| Defect | Consequence | Severity | Status | Location | Fix size | Corroboration |
|---|---|---|---|---|---|---|
| [D2](#D2) | Every total built on helper comes out twice as large as the contract says. | Blocker | Established, [corrected by the operator](#the-operator-s-corrections) | core.py:5-6 | 1 line | Both models, 2 reports |
| [D1](#D1) | A string passed to core raises instead of being refused with a message. | Major | Unresolved | core.py:1-2 | Small | Both models, 2 reports |
| [D4](#D4) | The tool exits 0 even when it did nothing. | Major | Established | run.py:1-2 | Medium | One model |
| [D7](#D7) | The guide describes the engine without the helper's doubling, so a reader trusts it. | Minor | Established | guide.md:3 | 1 line | One model |
| [D3](#D3) | util returns a constant string, so callers cannot tell two calls apart. | Minor | Unresolved | util.py:1-2 | Small | One model |

### By file

The same defects, grouped by file, so one person can take a file and close every defect listed under it in one change. A defect reported in two files is listed under both.

| File | Defects | Most severe | Lines |
|---|---|---|---|
| core.py | 2 | Blocker | 5-6, 1-2 |
| guide.md | 1 | Minor | 3 |
| run.py | 1 | Major | 1-2 |
| util.py | 1 | Minor | 1-2 |

## 4. Established defects (3)

The **tier** headings below, and each defect's **What goes wrong**, **Fix** and **Related**, are one agent's reading of the defects beneath them. Nothing checked them. Everything else in this report traces to a unit that verified it or to the engine's own records, so read the grouping and those three lines as a proposal and the rest as a finding.

Each was confirmed by a checker that had not raised it. A defect keeps every report of it, including a report that a check refuted.

### A result is wrong (2)

#### D2. Every total built on helper comes out twice as large as the contract says.

- **Severity** blocker · **Corroboration** both models, 2 reports · **Location** `core.py:5-6` · **Fix size** 1 line · **Verification** by running
  engine/core.py:5-6 — a tree that is not a repository
- **Corrected by the operator.** See [The operator's corrections](#the-operator-s-corrections).
  core.py:3-6 — 2 lines of context either side; the finding cites 5-6
  ```python
  3 | 
  4 | 
  5 | def helper(y):
  6 |     return y * 2
  ```
- **What goes wrong.** helper multiplies by two where the contract adds two, so every total built on it is too large.
- **Fix.** Return y + 2.
- **Test that should fail first.** (core.py:5-6) tests/test\_engine.py: call helper(3) and assert it returns 5.
- **Related.** [D5](#D5)
- **Checks.**
  - core.py:5-6 · by reading
  - core.py:5-6 · by running
    - Severity revised to blocker from major: Every total is wrong.
- Evidence (executed): Shows helper(3) returning 6. `python3 -c 'from engine.core import helper; print(helper(3))'` (exit 0, output complete)
  ```console
  6
  ```

#### D4. The tool exits 0 even when it did nothing.

- **Severity** major · **Corroboration** one model · **Location** `run.py:1-2` · **Fix size** medium · **Verification** by running
  run.py:1-2 — a tree that is not a repository
  ```python
  1 | def main():
  2 |     return 0
  ```
- **What goes wrong.** main returns 0 whether or not it did anything, so a caller cannot see a failure.
- **Fix.** Return non-zero when no work was done.
- **Test that should fail first.** tests/test\_engine.py: call helper(3) and assert it returns 5.
- **Checks.**
  - run.py:1-2 · by running
- Evidence (executed): Shows the tool exiting 0 having printed nothing. `python3 run.py` (exit 0, output complete)

### The documents do not say what the code does (1)

#### D7. The guide describes the engine without the helper's doubling, so a reader trusts it.

- **Severity** minor · **Corroboration** one model · **Location** `guide.md:3` · **Fix size** 1 line · **Verification** by reading
  docs/guide.md:3 — a tree that is not a repository
  guide.md:1-3 — 2 lines of context either side; the finding cites 3
  ```
  1 | # Guide
  2 | 
  3 | How the engine is used.
  ```
- **What goes wrong.** The guide says nothing of what helper returns, so a reader trusts the wrong total.
- **Fix.** State what helper returns.
- **Test that should fail first.** tests/test\_engine.py: call helper(3) and assert it returns 5.
- **Checks.**
  - guide.md:3 · by reading

## 5. Unresolved (2)

Nothing here is established: each is a claim a check could not settle. Where a checker answered, the entry carries **What is not settled.** This is that checker's own account of what stopped it; for most of these it names the one file or contract that would decide the claim. Where no checker answered, the group says so, the entry says a verdict never came back, and Coverage names the unit that owes one. The defects are grouped by what would settle each, and then by what each breaks. Every defect here sits under exactly one group, so a group's size counts pieces of work, not mentions.

### Needs a run — A result is wrong (1)

#### D3. util returns a constant string, so callers cannot tell two calls apart.

- **Severity** minor · **Corroboration** one model · **Location** `util.py:1-2` · **Fix size** small · **Verification** unresolved
  engine/util.py:1-2 — a tree that is not a repository
- **What goes wrong.** util returns one constant whatever it is asked, so no caller can tell two calls apart.
- **What is not settled.** Could not run it here.
- **Fix.** Return a value derived from the call.
- **Checks.**
  - util.py:1-2 · unresolved

### Needs a file outside the reviewed scope — A result is wrong (1)

#### D1. A string passed to core raises instead of being refused with a message.

- **Severity** major · **Corroboration** both models, 2 reports · **Location** `core.py:1-2` · **Fix size** small · **Verification** by reading
  engine/core.py:1-2 — a tree that is not a repository
- **What goes wrong.** core adds to x before anything checks x is a number, so a string raises.
- **What is not settled.** (core.py:1-2) Whether a string can reach core is not visible here.
- **Fix.** Refuse a non-number with a message before the addition.
- **Related.** [D2](#D2)
- **Checks.**
  - core.py:1-2 · unresolved
  - core.py:1-2 · refuted, by reading

## 6. Corroborated by both models (2)

Each of these was raised independently by a unit in each lane. Both units read blind, and neither saw the other's output. That is all this section says. What each defect is, how severe it is and where it is are in the sections above, and nothing is ranked here.

| Defect | Consequence |
|---|---|
| [D1](#D1) | A string passed to core raises instead of being refused with a message. |
| [D2](#D2) | Every total built on helper comes out twice as large as the contract says. |

## 7. Tests to write (1)

Inputs the code handles differently that no test in scope constructs. **None of these says the code is wrong.** A branch can be correct today with no test guarding it, and a test is what keeps it correct. Each is a test to write, at the lines that decide the input, under the test class that owes it.

#### tests/test\_engine.py

- **Across the batch** (verify-area-01-A-coverage): checked 1

- **core.py:5-6** — A negative multiplier is never exercised.
  - No test constructs: helper distinguishes y below zero and no test constructs one.
  - The auditor proposed: Add a case in tests/test\_engine.py with y = -1.
  - Test that should fail first. tests/test\_engine.py: call helper(3) and assert it returns 5.
  - What the check found: No test constructs a negative y.

## 8. Appendix

The record of the run rather than the work: what was dismissed, how the duplicates were merged, what was asked for and what ran where, what was not read, and which unit raised what. Nothing below is needed to fix a defect.

### Refuted (1)

Claims that were checked and dismissed, each with the reason, so nobody goes over them again. Nothing here needs work.


| Defect | Consequence | Location | Why it was dismissed |
|---|---|---|---|
| D6 | The README calls the tree small, which it may not stay. | README.md:3 | A description of a fixture is not a defect. **Corrected by the operator**; see [The operator's corrections](#the-operator-s-corrections). |

### Coverage gaps a test already covers (0)

No coverage gap was answered with a test that covers it.

### What each verification unit returned (5)

How each unit's answers were split. The units of one run asked the same kind of question of comparable batches, so a unit that settles far fewer than the rest tells you about that unit, not about the code it read. Each was handed one lane's findings, never its own, so the second column says whose work the row is about.


| Unit | Findings from | Handed | Established | Unresolved | Refuted |
|---|---|---|---|---|---|
| verify-area-01-A | A | 3 | 2 | 1 | 0 |
| verify-area-01-B | B | 3 | 1 | 1 | 1 |
| verify-area-01-A-coverage | A | 1 | 1 | 0 | 0 |
| verify-area-02-A | A | 1 | 1 | 0 | 0 |
| verify-area-02-B | B | 1 | 0 | 0 | 1 |

The share left unresolved ranges from 0% (verify-area-02-B, 0 of 1) to 33% (verify-area-01-A, 1 of 3).

#### What each unit said about its batch

- verify-area-01-A: checked 3
- verify-area-01-B: checked 3
- verify-area-01-A-coverage: checked 1
- verify-area-02-A: checked 1
- verify-area-02-B: checked 1

### What one more file would settle (0)

No unresolved verdict named a file outside the reviewed scope.

### Clustering notes

Every candidate is in exactly one cluster: 8 candidates in 6 clusters, none dropped and none counted twice.

#### The largest clusters

- D1 — 2 candidates (cand-003, cand-004): A string passed to core raises instead of being refused with a message.
- D2 — 2 candidates (cand-006, cand-007): Every total built on helper comes out twice as large as the contract says.

#### Kept apart at one location

No candidates at one location were split.

#### What each clustering unit reported

- area-01: 4 groups
- area-01: 1 groups
- area-02: 2 groups

### The synthesis round

What the judgment round returned. The tier headings, **What goes wrong**, **Fix** and **Related** came from this round, and nothing else in this report did.

- synth-A — complete: 2 tiers named for 6 defects.
- D2 cited D7, which was dropped: the two defects neither touch a file in common nor sit under one tier.

### Path legend

Every file and directory this report names, under the short name the report uses for it. A path is spelled in full only here and under the defect it locates, so a name that appears twice below is the same file both times. Two things are copied as they were rather than written by the report, and keep their own spelling: a sentence quoted from a worker, and the job printed under **The job**. Shortening a path in either would be editing a record, and the job has to stay something a reader can paste. Where this page would otherwise lose the whitespace in a path, that whitespace is escaped below, so the spelling names one file and no other.

| Short name | Full path |
|---|---|
| README.md | README.md |
| core.py | engine/core.py |
| guide.md | docs/guide.md |
| lib.py | vendor/lib.py |
| run.py | run.py |
| util.py | engine/util.py |

### How this ran

- Run directory, holding every payload, result and transcript this report was built from: `/tmp/rp-old-run/run`
- Lane A adapter, as recorded by the dispatcher:
  ```
  runtime-one sub-agent
  ```
- Lane A permission, as recorded by the dispatcher:
  ```
  read-only
  ```
- Lane B adapter, as recorded by the dispatcher:
  ```
  runtime-two command line
  ```
- Lane B permission, as recorded by the dispatcher:
  ```
  read-only sandbox
  ```
- Containment: the adapter and permission for each lane above are as the dispatcher recorded them, and the engine did not verify them. All the engine can state is that it spawned nothing and checked no sandbox, and that the files its payloads list are snapshot paths, none under the source root. It claims nothing stronger.
- Read: 7 files (walk), tree sha256 c46945e9d2fb9cd30c38348a247810f41edb639db0115b4367b75494900e245b; 1 excluded, 0 skipped.
- Pinned at: a tree that is not a repository.

### The job

What this run was asked for. The problem statement under the title is one of these fields, quoted there because it is the one a reader acts on; the rest are here.
Which fields the owner asked for and which the interview filled in is not recorded: there is no `job-notes.json` beside the job, so a job handed in ready-made and one the interview wrote look the same here.

- Read for: the statement under the title, carried verbatim into every reader's payload.
- Tree: the root the job names. It is in `job.json`, not on this page: the only path on this page from the machine the audit ran on is the run directory above.
- Scope: everything under the job's root.
- Excluded: 1 path the job named; **Not read**, under Coverage below, lists the files they took out.
- Divided: by subject — the job named 3 subjects; one area each.
  - engine — 4 files
  - docs — 2 files
  - rest — 1 file
- Read with 2 lenses. A lens is the instruction a reader is handed for how to read its area. Every area was read once per lens, and the lenses were assigned to the two model lanes in turn.
  - **L1** bottom-up from the code
  - **L2** top-down from the contract
- Coverage: left to the tree — 1 of 3 areas earned an auditor.
- Questions: put to whoever ran the panel rather than to its readers, answered under **The operator's notes**.

#### To run this audit again

The job, to paste back in when you do not have the file itself. `root` is the only field replaced: the tree is on your disk, at your path, and the only path on this page from the machine the audit ran on is the run directory above:
```json
{
  "problem": "Find every place the helper's result is wrong, and every document that says so.",
  "root": "/absolute/path/to/the/tree",
  "exclude": [
    "vendor/"
  ],
  "partition": "subject",
  "lenses": [
    "bottom-up from the code",
    "top-down from the contract"
  ],
  "areas": [
    {
      "name": "engine",
      "paths": [
        "engine/",
        "run.py"
      ],
      "also_read": [
        "tests/test_engine.py"
      ]
    },
    {
      "name": "docs",
      "paths": [
        "README.md",
        "docs/"
      ]
    },
    {
      "name": "rest",
      "paths": [],
      "remainder": true
    }
  ],
  "questions": "Is the helper's doubling ever right?"
}
```

If you still have the run directory, the same job is `job.json` in the run directory named above. Copy it out before running: the driver creates the run directory itself and refuses one that already holds a run, so a new run cannot be planned in the old directory.
```
cp <run directory>/job.json ./job.json
python3 review_panel_run.py run \
        --job ./job.json \
        --rundir <a directory that does not exist yet> \
        --adapter <your adapter configuration> \
        --go
```

Three things this page cannot fill in. `--rundir` has to name a directory that does not exist yet, outside the reviewed tree and outside every git repository. `--adapter` is the configuration saying which runtime runs each lane. What ran this time is described under **How this ran** above, but the configuration itself was never in the run directory. And the tree has to be the one this report was read from, at the commit and tree sha256 named there: the job pins which files are read and not which bytes they hold, so the same job over a changed tree is a different audit.

### Coverage

Partitioning put every file in scope into exactly one area, so a gap here is a unit that failed or returned nothing valid, or a path the job left out.

#### Units that failed or returned nothing

Every unit returned a valid result.

#### Not read

- Excluded by the job (1):
  - lib.py

### Provenance

Which unit raised what, and which answered it. Nothing here is needed to fix a defect; it is here so the run can be audited. Everything each unit wrote is in `findings.json` beside this report, under the same candidate id, where it can be searched: what it reported, the direction and any reproduction it proposed, and what the checker concluded.
Below, a reader's lens is cited by its tag. **The job**, two subsections up in this appendix, spells each one out.

| Defect | Candidate | Raised by | Proposed | Answered by |
|---|---|---|---|---|
| D1 | cand-003 | area-01-A1 (lane A, L1) | Minor | verify-area-01-A (lane B) |
| D1 | cand-004 | area-01-B1 (lane B, L2) | Major | verify-area-01-B (lane A) |
| D2 | cand-006 | area-01-B1 (lane B, L2) | Blocker | verify-area-01-B (lane A) |
| D2 | cand-007 | area-01-A1 (lane A, L1) | Major | verify-area-01-A (lane B) |
| D3 | cand-008 | area-01-B1 (lane B, L2) | Minor | verify-area-01-B (lane A) |
| D4 | cand-009 | area-01-A1 (lane A, L1) | Major | verify-area-01-A (lane B) |
| D6 | cand-001 | area-02-B1 (lane B, L2) | Minor | verify-area-02-B (lane A) |
| D7 | cand-002 | area-02-A1 (lane A, L1) | Minor | verify-area-02-A (lane B) |
| D5 | cand-005 | audit-area-03-A (auditor, lane A) | Major | verify-area-01-A-coverage (lane B) |
