# Review panel report

Generated 2026-10-04 20:50:12 UTC · 8 files from a tree that is not a repository

review-panel was given this prompt:

> Find every place the installer can leave a half-written file.

This run read the 8 files under the job's root as 1 area the engine cut along directory boundaries, under the 2 lenses the job names. The rest of the job is in the appendix, at [this link](#the-job): the root, what was excluded and the instruction each reader was handed, together with the job itself and what it takes to run the same audit again.

Those fields were settled before the panel read anything, and not by the panel: either in an interview with whoever asked for the audit, or handed over ready-made as a job file. Nothing in the run directory records which.

## 1. Report description

A panel of agents read this tree in bounded pieces. Whatever one agent raised was sent to another agent that had not raised it, to be checked.
A **defect** (`D1`, `D2`, …) is one mistake. A **site** (`S1`, `S2`, …) is one place — a function, a document passage, a test — where that mistake has to be fixed. Every site was read and checked on its own, and keeps its own outcome, severity, test and evidence. Each site's quoted source is shown in its defect's entry; the rest of its evidence is under **Evidence**, near the end, under the same number.
**Established** means a checker that had not raised the claim upheld it. **Unresolved** means nothing settled it, and the group it sits under says what would. **Refuted** means a check dismissed it, so there is nothing to do about it; it is listed under **Refuted** with the reason. A defect is placed under Established when any of its sites is, and under Refuted only when all of them are.
The **appendix** at the end is the record of the run rather than the work it found. Nothing in it is needed to fix anything.

- Defects by placement: 2 established, 0 refuted, 0 unresolved.
- Sites by outcome: 3 established, 0 refuted, 0 unresolved.
- Candidates behind them: 4 — 1 reproduced, 3 confirmed by reading, 0 refuted, 0 unresolved.
- Defects: 2 at 3 sites from 4 candidates; every candidate is at exactly one site and every site in exactly one defect; 1 defect has more than one site.
- Every line number in this report refers to a tree that is not a repository; **How this ran**, in the appendix, names it in full.
- Where things are: the defects, each with its account, its sites and the source at each site, are under **Established defects**, **Unresolved** and **Refuted**; everything else the review recorded about each site is under **Evidence**, by the site's number.
- Reading units: 4 — 3 complete, 1 failed, 0 missing.
- Verification units: 2 — 2 complete, 0 failed, 0 missing.
- Clustering units: 1 — 1 complete, 0 failed, 0 missing.
- Rung: two runtimes. Reading units: 4 — 3 complete, 1 failed, 0 missing; 1 of 1 area read by both models. Candidates: 4 — 4 checked by the other model, 0 left unresolved. Where the lanes disagree, the disagreement is between two different models.
- Executability, as a separate unit found it in a disposable copy of the snapshot: this tree builds — yes; its tests run — yes.
  - What the probe reported: Compiles; the suite runs and one test fails.
- Reproductions: 1 proposed, 1 run — 1 executed. Whether this tree builds and its tests run, how many reproductions were proposed and how many ran are three separate facts, and each is counted on its own.
- Reachability: the reviewed set was not compared with the files the repository tracks, so it is unknown whether anything that reaches these defects was left out.
- This report names every file by a short name; the **Path legend**, in the appendix, gives each one's full path.

### The judgment round's overview

One paragraph the judgment round wrote about the run as a whole. It is a reading, and nobody checked it: only the defects it names were verified, each on its own entry below.

Two tiers; one mistake made at two sites.

### Defects by tier

What the run found, grouped by the themes the judgment round named. The table holds counts only; the three views below are the lists to work from.


| Tier | Blocker | Major | Minor | Nit | Defects |
|---|---|---|---|---|---|
| A run stops instead of finishing | 1 | 0 | 0 | 0 | 1 |
| The total comes out wrong | 0 | 0 | 1 | 0 | 1 |

### What this report does not tell you

**What this run covered.** Readers covered all 8 files in scope.

**What no run can tell you: what it missed.** A panel reports what its readers happened to raise; it does not look for any particular bug, so one in a file it read can still be absent here, and a longer report is not a more complete one. To learn whether a run would catch a bug that matters to you, give it one you already know about and see whether it appears.

## 2. Indices

One list of defects, with two ways in: by rank or by file.

### Every defect — most severe first

Every defect that needs work, most severe first and, within a severity, cheapest fix first, then fewest sites. So the first rows are the blockers, in the order to take them on.

| Defect | Heading | Severity | Sites | Fix size |
|---|---|---|---|---|
| [D1](#D1) | An empty input stops the run with a traceback. | Blocker | 2 (2 established) | 1 line |
| [D2](#D2) | A missing value doubles the total. | Minor | 1 (1 established) | 1 line |

### By file

The same defects by file, so one person can take a file and close every site listed under it in one change. Fixing a file fixes those sites; a defect is closed only when all of its sites are.

| File | Most severe | Sites (defect) |
|---|---|---|
| core.py | Blocker | [S1](#S1-src) ([D1](#D1)), [S2](#S2-src) ([D1](#D1)), [S3](#S3-src) ([D2](#D2)) |

## 3. Established defects (2)

The **tier** headings below, and each defect's **What goes wrong**, **Fix** and **Related**, are one agent's reading of the defects beneath them. Nothing checked them. Everything else in this report traces to a unit that verified it or to the engine's own records, so read the grouping and those three lines as a proposal and the rest as a finding. Each defect's heading, and any **At this site** note under one of its sites, is that agent's reading too.

Each has at least one site a checker that had not raised it confirmed. Every site keeps its own outcome, so a site nothing settled, or one a check refuted, is shown as that beside the others.

### A run stops instead of finishing (1)

<a id="D1"></a>

#### D1. An empty input stops the run with a traceback.

- **2 sites**: 2 established · **Worst severity** blocker, at [S2](#S2-src) · **Largest fix** 1 line · **Areas** area-01
- **What goes wrong.** An empty input is indexed before anything checks it, so the call dies where it should have done nothing.
- **Fix.** Check for an empty input before indexing, at both sites.

| Site | Location | Outcome | Severity | Particular to this site | Test that should fail first |
|---|---|---|---|---|---|
| [S1](#S1-src) | core.py:2-3 | Established | Major |  | tests/test\_engine.py: call it with an empty list and assert it returns None. |
| [S2](#S2-src) | core.py:5 | Established | Blocker | Here the empty input is the argument list, at start-up. | tests/test\_engine.py: call it with an empty list and assert it returns None. |

**Source at each site.**

- <a id="S1-src"></a>**S1** · `core.py:2-3` — A run over an empty input stops with a traceback instead of finishing. · [evidence](#S1)
  core.py:1-5 — 2 lines of context either side; the finding cites 2-3
  ```python
  1 | def core(x):
  2 |     return x + 1
  3 | 
  4 | 
  5 | def helper(y):
  ```
- <a id="S2-src"></a>**S2** · `core.py:5` — Running the tool with no arguments fails instead of printing help. · [evidence](#S2)
  core.py:3-6 — 2 lines of context either side; the finding cites 5
  ```python
  3 | 
  4 | 
  5 | def helper(y):
  6 |     return y * 2
  ```

### The total comes out wrong (1)

<a id="D2"></a>

#### D2. A missing value doubles the total.

A missing value is doubled and the total comes out wrong.

- <a id="S3-src"></a>**Site** [S3](#S3) · `core.py:8` · **Outcome** established · **Severity** minor · **Fix size** 1 line
- **What goes wrong.** A missing value is doubled rather than skipped.
- **Fix.** Skip a missing value.
- **Test that should fail first.** tests/test\_engine.py: call it with an empty list and assert it returns None.

## 4. Unresolved (0)

Nothing was left unresolved.

## 5. Refuted (0)

Nothing was refuted.

## 6. Corroborated by both models (0)

Nothing left to address was raised by both models: every defect that needs work came from a single reader.

## 7. Evidence (3)

Everything else the review recorded about each site, in site order: its severity, corroboration and verification, every report's check, and the runs and their output. Each heading gives the site's number, location, outcome and defect. Shown in the defect's entry instead of here: the site's quoted source, its first test that should fail first and, for an unresolved site, what is not settled.

<a id="S1"></a>

### S1 · `core.py:2-3` · established · [D1](#D1)

- **Severity** major · **Corroboration** both models, 2 reports · **Location** `core.py:2-3` · **Fix size** 1 line · **Verification** by reading
- **The source quoted with all 2 reports does not match what the pinned tree holds at those lines. The line range may be wrong. The engine left the range exactly as the reports gave it, so read the location itself before acting on the quotation.**
- **Checks.**
  - core.py:2-3 · by reading (2 reports)

<a id="S2"></a>

### S2 · `core.py:5` · established · [D1](#D1)

- **Severity** blocker · **Corroboration** one model · **Location** `core.py:5` · **Fix size** 1 line · **Verification** by running
- **The source quoted with this report does not match what the pinned tree holds at those lines. The line range may be wrong. The engine left the range exactly as the report gave it, so read the location itself before acting on the quotation.**
- **Checks.**
  - core.py:5 · by running
    - Severity revised to blocker from major: Every invocation hits it.
- Evidence (executed): Shows the call raising IndexError on an empty argument list. `python3 run.py` (exit 1, output complete)
  ```console
  Traceback ...
  IndexError: list index out of range
  ```

<a id="S3"></a>

### S3 · `core.py:8` · established · [D2](#D2)

- **Severity** minor · **Corroboration** one model · **Location** `core.py:8` · **Fix size** 1 line · **Verification** by reading
- The engine could not read those lines from the pinned tree, so the quotation was not checked and no source is shown.
- **Checks.**
  - core.py:8 · by reading

## 8. Appendix

The record of the run rather than the work: how the duplicates were merged, what the evidence leaves out, what was asked for and what ran where, what was not read, and which unit raised what. Nothing below is needed to fix a defect.

### What each verification unit returned (2)

How each unit's answers were split. The units of one run asked the same kind of question of comparable batches, so a unit that settles far fewer than the rest tells you about that unit, not about the code it read. Each was handed one lane's findings, never its own, so the second column says whose work the row is about.


| Unit | Findings from | Handed | Established | Unresolved | Refuted |
|---|---|---|---|---|---|
| verify-area-01-A | A | 2 | 2 | 0 | 0 |
| verify-area-01-B | B | 2 | 2 | 0 | 0 |

#### What each unit said about its batch

- verify-area-01-A: read one, ran one
- verify-area-01-B: read two

### What one more file would settle (0)

No unresolved verdict named a file outside the reviewed scope.

### Clustering notes

Every candidate is in exactly one cluster: 4 candidates in 3 clusters, none dropped and none counted twice.

#### The largest clusters

- S1 — 2 candidates (cand-001, cand-002): A run over an empty input stops with a traceback instead of finishing.

#### Kept apart at one location

No candidates at one location were split.

#### What each clustering unit reported

- area-01: Three defects; the first was reported by both readers.

### The merge round

Which sites the merge round proposed as one mistake. A proposed group is reported as one defect only once a unit that did not propose it, on the other lane and in a fresh context, has checked it site by site, so both models took part; only the sites it upheld are that defect, and every other site is its own.

There were too many sites for one merge unit, so they were split into 2 batches by directory and merged within each. Merges across batches were not attempted: sites of one mistake that fall in different batches are never reported as one defect.

- merge-A-1 — complete: 1 group of several sites proposed over 2 sites, and 0 sites kept apart for a second claim.
- merge-A-1 reported: grouped by mechanism
- merge-A-2 — complete: 0 groups of several sites proposed over 1 site, and 0 sites kept apart for a second claim.
- merge-A-2 reported: grouped by mechanism
- mergecheck-B — complete: 1 group checked.
- mergecheck-B reported: Checked every group against its mechanism.
- G1 (S1, S2) is [D1](#D1), merged on: An empty input is indexed before it is checked; check it first.

### The synthesis round

What the judgment round returned. The tier headings, each defect heading it wrote, the **At this site** notes, **What goes wrong**, **Fix** and **Related** came from this round, and nothing else in this report did.

- synth-A — complete: 2 tiers named for 2 defects.

### What the evidence leaves out

Each site's evidence entry is every line its readers and checkers recorded, less exactly these:

- The site's **What goes wrong**, **Fix** and **Related**: its defect's own account replaces them.
- The line giving the site's full path and the commit: every site is at the one commit stated at the top, and the **Path legend** gives each path.
- The quoted source: moved to the site's place in its defect's entry.
- The site's first **Test that should fail first** and, for an unresolved site, **What is not settled**: shown in its defect's entry.
- 0 clustering notes on why a report was kept apart from another at one location, where the merge then put the site in a defect of several: moved here, below.
- 0 quoted sources identical to one quoted for another site, and 0 blocks of output identical to one printed under another site: each replaced by a pointer to the site that prints it.

### Path legend

Every file and directory this report names, under the short name the report uses for it. A path is spelled in full only here, so a name that appears twice below is the same file both times. Two things are copied as they were rather than written by the report, and keep their own spelling: a sentence quoted from a worker, and the job printed under **The job**. Shortening a path in either would be editing a record, and the job has to stay something a reader can paste. Where this page would otherwise lose the whitespace in a path, that whitespace is escaped below, so the spelling names one file and no other.

| Short name | Full path |
|---|---|
| README.md | README.md |
| core.py | engine/core.py |
| deep.py | engine/sub/deep.py |
| guide.md | docs/guide.md |
| lib.py | vendor/lib.py |
| run.py | run.py |
| test\_engine.py | tests/test\_engine.py |
| util.py | engine/util.py |

### How this ran

- Run directory, holding every payload, result and transcript this report was built from: `/tmp/rp-v2026.10.0/run`
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
- Read: 8 files (walk), tree sha256 492b7665762c6339b35a50b8dffd5be56cc46b47659a258c3edccbaab4aeb8bd; 0 excluded, 0 skipped.
- Pinned at: a tree that is not a repository.

### The job

What this run was asked for. The problem statement under the title is one of these fields, quoted there because it is the one a reader acts on; the rest are here.
Which fields the owner asked for and which the interview filled in is not recorded: there is no `job-notes.json` beside the job, so a job handed in ready-made and one the interview wrote look the same here.

- Read for: the statement under the title, carried verbatim into every reader's payload.
- Tree: the root the job names. It is in `job.json`, not on this page: the only path on this page from the machine the audit ran on is the run directory above.
- Scope: everything under the job's root.
- Excluded: nothing.
- Divided: by file — the engine cut the files in scope into 1 area along directory boundaries, under a size ceiling.
- Read with 2 lenses. A lens is the instruction a reader is handed for how to read its area. Every area was read once per lens, and the lenses were assigned to the two model lanes in turn.
  - **L1** bottom-up from the code
  - **L2** top-down from the contract
- Coverage: left to the tree — 1 of 1 area earned an auditor.

#### To run this audit again

The job, to paste back in when you do not have the file itself. `root` is the only field replaced: the tree is on your disk, at your path, and the only path on this page from the machine the audit ran on is the run directory above:
```json
{
  "problem": "Find every place the installer can leave a half-written file.",
  "root": "/absolute/path/to/the/tree",
  "exclude": [],
  "partition": "file",
  "lenses": [
    "bottom-up from the code",
    "top-down from the contract"
  ]
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

- audit-area-01-B (auditor, lane B) — failed: error.txt: the reply was not a JSON object. Read area-01:
  - README.md
  - guide.md
  - core.py
  - deep.py
  - util.py
  - run.py
  - test\_engine.py
  - lib.py

#### Not read

Nothing was excluded and nothing was skipped.

### Provenance

Which unit raised what, and which answered it. Nothing here is needed to fix a defect; it is here so the run can be audited. Everything each unit wrote is in `findings.json` beside this report, under the same candidate id, where it can be searched: what it reported, the direction and any reproduction it proposed, and what the checker concluded.
Below, a reader's lens is cited by its tag. **The job**, two subsections up in this appendix, spells each one out.

| Defect | Candidate | Raised by | Proposed | Answered by |
|---|---|---|---|---|
| D1 | cand-001 | area-01-A1 (lane A, L1) | Major | verify-area-01-A (lane B) |
| D1 | cand-002 | area-01-B1 (lane B, L2) | Major | verify-area-01-B (lane A) |
| D1 | cand-003 | area-01-A1 (lane A, L1) | Major | verify-area-01-A (lane B) |
| D2 | cand-004 | area-01-B1 (lane B, L2) | Minor | verify-area-01-B (lane A) |
