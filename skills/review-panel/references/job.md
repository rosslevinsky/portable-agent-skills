# The job

Elicitation is prose; the job is data. This file is the interview that produces a job, the
defaults applied when nobody is there to answer, and the `job.json` format the engine reads
strictly — nothing in the engine fills a field in, so a job file says exactly what ran.

## The interview

Ask the clarifying questions in **one message**, and only about what the handed-in text left
unclear — skip anything it already answered. Five things:

1. **The problem.** What the owner wants found. It is carried verbatim into every reader's
   payload and the engine never interprets it. It is the one field with no default.
2. **Root and exclusions.** The directory the sweep reads — written into the job as an
   **absolute** path — and any paths under it to leave out. An excluded path is still
   classified: the report lists it under *not read*. The snapshot every worker works in
   holds the files under review and nothing else, except that inside a repository every
   lockfile git tracks is copied too, excluded or not, because the probe and the verifiers
   build there and an install needs it; the run says how many before it starts. Where the owner wants a **subset** of a
   large repository, ask for the files by name and write them as `files` rather than asking
   for a directory holding copies of them: a copy is a second tree to keep in step, and
   `plan` records the commit of the tree it actually read.
3. **The partition.** By **file** — the engine builds areas along directory boundaries under
   a size ceiling — or by **subject**, where the owner names the areas and the paths each
   covers, and may mark one `remainder` for everything the others leave. A subject with no
   files is almost always a concern mis-stated as a subject — "Windows", "does the prose
   match the code" — and is a lens, not an area; say so.
4. **The lenses.** How each reader reads the same files. The default is two: bottom-up from
   the code, and top-down from the contract. One entry per reader, at least two, so every
   area is read by both model lanes; an area may carry its own list.
5. **The run directory.** Where the snapshot, the units and the report go. It must lie
   outside root and outside every git repository; propose one and let the owner move it.
   **The job file does not live in it**: the driver creates the directory itself and
   copies `job.json` in, and refuses one already sitting at that path, so write the job
   beside your own work.

**Restate before writing.** Restate the job as a defect audit — *these files, read for what is
wrong, answered as findings: a location, a failure and a severity* — and ask the owner to
confirm. The job needs **at least one finding-shaped statement**: something to find. Questions
may come with it — "is this approach right", "why is the cache keyed this way" — and go in
`questions`, which no reader is shown: you answer them after the run, in `report-notes.json`
(`references/report-format.md`). A job that only asks, naming nothing to find, stops here with
that reason, before anything is dispatched: a reader's unit of output is a finding, and
dispatching such a job produces findings about the wrong thing.

**Preview before dispatching.** The reading round is the readers summed over the areas —
each area's own lens count, two by default — plus **two auditors for every area that earned
one**, which may be no area at all, plus exactly one capability probe whatever the
partition. There is no formula in the area count either: in file mode it falls out of the
size ceiling, and in subject mode it is the declared areas after any oversize one is split,
so nobody can predict the total from the job alone. Plan, then show the owner what it
reports. Verification units come later and are not previewed.

**What "what it reports" means.** The driver's `--go`-less run prints the run directory,
the stage and a count per unit kind, and stops there. The rest is not printed — it is in
the run directory that run just planned, in three files, and that is where to look before
answering the owner. `units.json` lists every unit: the readers each area gets, and which
areas earned an auditor. `areas.json` holds the partition, the size ceiling and each area's
test-to-code links, which is what an auditor was planned from. `inventory.json` holds the
files in scope and, beside them, what was **excluded** — the paths your own exclusions took
out, as paths — and what was **skipped**, each of those with the reason it was not read. If
the owner changes the partition after seeing any of it, rewrite `job.json` and plan again
into a run directory that does not yet exist.

## Operating autonomously

If operating autonomously (no user available), assume: partition by file; root is the
repository root, or the working directory outside a repository; no exclusions; the two
default lenses; a fresh run directory under the host's temporary directory. Note each
assumption when writing the job. The problem statement is never assumed: a text with none
is refused, since nothing can guess what the owner wants found. The confirmation is skipped,
but the defect-audit test still applies — text that only asks questions, naming nothing to
find, is refused with that reason. A question beside something to find goes in `questions`.

## The format

`job.json` is one JSON object. The engine hand-parses it: an **unknown key** or a **missing
field** is refused by name, and an optional key present with the wrong shape is refused
too — optional means absent-is-allowed, never absent-is-filled.

**When the interview applies a default, write `job-notes.json` beside the job as well.**
Telling the owner in the conversation is a message and not a record: the report states the
job, and without this file it cannot say which of those values anybody chose. One JSON object,
mapping any of `root`, `exclude`, `partition`, `lenses`, `files` and `coverage` to `stated`
(the handed-in text said so), `defaulted` (nothing said, so this file's default) or
`interview` (the owner answered) — for example
`{"root": "defaulted", "partition": "defaulted", "lenses": "defaulted", "files": "stated"}`.
It never goes in `job.json`, which the engine reads strictly and which says what ran rather
than how it was decided. A job handed over ready-made needs no notes file — and note what the
report can then say, which is less than it sounds: only that nothing recorded where the values
came from. **It cannot tell a ready-made job from one an interview wrote without notes**, and it
does not guess, so skipping the file is not self-documenting.

| Key | Value |
|---|---|
| `problem` | Non-empty text, verbatim. |
| `root` | An absolute path to a directory. A relative one is refused: the job file moves with its run directory, the tree does not. |
| `exclude` | A list of paths and prefixes, possibly empty. |
| `partition` | `"file"` or `"subject"`. |
| `lenses` | At least two lens names, one per reader. |
| `areas` | Subject mode only, and refused in file mode. A non-empty list. |
| `files` | Optional. A non-empty list of files to review in place of everything under `root`. |
| `coverage` | Optional. `false` takes the coverage question off the table entirely. Absent, the engine decides from the tree. |
| `questions` | Optional. Non-empty text, carried as written. No reader is shown it; the operator answers it after the run, in `report-notes.json`. |

Each area: `name` (distinct, and distinct ignoring case); `paths`; optionally `also_read`;
optionally `remainder: true` on at most one area, which may then have empty `paths`;
optionally `lenses`, at least two, replacing the job's for that area.

**Paths.** Relative to root; forward slashes — backslashes are accepted and normalized on
the way in; no `..`, nothing absolute, and never the root itself. An entry ending in `/` is
a **directory prefix** claiming everything under it; any other entry names **one file**. No
`*`, `?` or `[`: these are paths and prefixes, never shell globs, because glob semantics
differ across shells and platforms. Refused by name: a name declared both as a file and as
a prefix; two paths that differ only by case, which a case-insensitive host cannot hold; a
path in two areas' `paths`; an area with empty `paths` that is not the remainder. Against
the tree, `plan` refuses by name an entry that matches nothing in the inventory — a prefix
must name a directory in it — and a file two areas' prefixes both claim. A remainder that
receives no file is not an area, and `plan` says so.

**`files`.** Every entry names **one file**: a directory prefix is refused, because a list
that can claim a whole directory is the enumeration it replaces. The entries are normalized
and checked exactly as `exclude` is, so a job written on one host reads the same on another,
and a name that is not in the tree is refused by name rather than quietly skipped — the owner
asked for that file. The rest of the repository is still measured against the list:
`plan` records every tracked file the list leaves out, so the scope's blind spot is a number
in the run directory rather than something a reader has to work out. The snapshot holds
the listed files and the tracked lockfiles, so a build over a small list usually cannot run.
Exclusions still apply to the listed set and still have to match something, and `.git` is
not listable — the
snapshot holds source, and a reader that can reach refs is not reading it alone.

**`coverage`.** A second round asks which inputs no test in scope constructs, and answers
in missing tests rather than in defects. `false` means no auditor is planned however many
tests the tree has — the answer a security review wants, where hundreds of missing-test
entries sit beside the defects it was run for and bury them. Ask only where the owner's
problem statement is plainly about one kind of defect; otherwise leave it out and let the
tree decide.

**`also_read`** lists paths the area's readers open as well — a test beside its subject, a
document beside the code it describes — that belong to another area for responsibility.
They count toward the area's payload ceiling and not toward closure, and a finding in one
is attributed to the area that owns it. They must lie inside root, name nothing excluded
and nothing already the area's own, and fit under the ceiling beside every part of the
area; a set that cannot is refused by name rather than trimmed. An oversize file reads
alone: an area whose part is one file over the ceiling may carry no `also_read`.

### File mode

```json
{
  "problem": "Find every place the installer can leave a half-written file behind.",
  "root": "/projects/my-project",
  "exclude": ["vendor/", "docs/generated/"],
  "partition": "file",
  "lenses": ["bottom-up from the code", "top-down from the contract"]
}
```

### A subset of a large repository

`root` stays the repository, and `files` names what to review inside it:

```json
{
  "problem": "Find every place the installer can leave a half-written file behind.",
  "root": "/projects/my-project",
  "exclude": [],
  "partition": "file",
  "lenses": ["bottom-up from the code", "top-down from the contract"],
  "files": ["install.py", "scripts/bootstrap.py", "scripts/verify.py"]
}
```

### Subject mode

```json
{
  "problem": "Find every place the installer can leave a half-written file behind.",
  "root": "/projects/my-project",
  "exclude": ["vendor/"],
  "partition": "subject",
  "lenses": ["bottom-up from the code", "top-down from the contract"],
  "areas": [
    {"name": "installer", "paths": ["install.py", "scripts/"],
     "also_read": ["tests/test_install.py"]},
    {"name": "docs", "paths": ["README.md", "docs/"],
     "lenses": ["does the prose match the code", "what does a first-time reader miss"]},
    {"name": "rest", "paths": [], "remainder": true}
  ]
}
```
