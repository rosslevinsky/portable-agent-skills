# Dispatch

How a worker is launched, and by what. The driver performs all of it, so nobody reads this
to run a review: read it to write the adapter config, and to answer a run that stopped.

## The adapter config

The driver names no product anywhere. Each slot's command line arrives as **data** and the
driver only renders and runs it, so the same run works under any runtime whose CLI can be
spelled here. Permission is per unit rather than per slot — readers and clusterers read,
while verifiers, the synthesis unit and the capability probe run builds and reproductions
and write — so each slot declares two commands.

```json
{
  "slots": {
    "A": {
      "runtime": "runtime-a",
      "model": "model-a-v2",
      "account": "team-account",
      "adapter": "fresh worker processes of the driving runtime",
      "provider_fault_patterns": ["usage limit", "rate limit", "5\\d\\d from the API"],
      "read_only": {
        "command": ["runtime-a", "exec", "--sandbox", "read-only", "-C", "⟪cwd⟫", "⟪prompt⟫"],
        "permission": "read-only sandbox, enforced by the runtime",
        "result_mode": "stream-transcript",
        "idle": 900,
        "deadline": 3600
      },
      "write_capable": {
        "command": ["runtime-a", "exec", "--sandbox", "workspace-write", "-C", "⟪cwd⟫", "⟪prompt⟫"],
        "permission": "writes confined to the disposable copy it is given"
      }
    },
    "B": {
      "runtime": "runtime-b",
      "model": "model-b-v1",
      "adapter": "the other runtime's command line",
      "read_only": {
        "command": ["runtime-b", "--print", "⟪prompt⟫", "--add-dir", "⟪input⟫", "--no-edits"],
        "permission": "edit tools blocked; a shell command it runs can still write"
      },
      "write_capable": {
        "command": ["runtime-b", "--print", "⟪prompt⟫", "--add-dir", "⟪cwd⟫", "--allow-edits"],
        "permission": "writes confined to the disposable copy it is given"
      }
    }
  }
}
```

Slot B above takes every default.

- `slots` holds exactly `A` and `B`. `runtime`, `model`, `adapter` and both mode objects are
  required; `account` defaults to `runtime`, and `provider_fault_patterns` to none.
- `adapter` is free text and is rendered verbatim in the report, so write what a reader
  months later needs: the mechanism, not just the product.
- `account` is what an outage is counted against. Two slots on one subscription share an
  account, and pausing one pauses both — which is correct, and the reason the key exists
  separately from `runtime`.
- `provider_fault_patterns` are case-insensitive regular expressions matched against the
  terminal event's own error text. **The wording of a provider's refusal is the provider's
  to change**, so what counts as one lives here and never in the code. Declaring none is a
  valid answer: every failure is then the worker's until two of them explain nothing, which
  pauses the account anyway.
- Each mode object takes `command` and `permission`, and optionally `result_mode`
  (default `stream-transcript`), `idle` (900 s) and `deadline` (3600 s). An hour is not
  generous: a reader on a slower model can take more than half of one.
- `permission` is free text too, and the report prints both modes side by side. The engine
  cannot verify a sandbox, so it renders the claim rather than a vocabulary it could not
  check — which means an inaccurate one is a false statement in the report.

**Two slots on one runtime naming different models is refused at startup, before anything
is planned.** The report has one sentence for a one-runtime run and it says the candidates
were checked by the same model, calling any disagreement a difference of context. That is
false of two models, and it is not a two-runtime run either. Give the two slots one model,
or two runtimes.

### What the driver fills in

`⟪prompt⟫`, `⟪payload⟫`, `⟪schema⟫`, `⟪input⟫`, `⟪cwd⟫`, `⟪transcript⟫`. A command using a
marker the driver does not fill is refused when the config is parsed; so is one carrying
neither `⟪prompt⟫` nor `⟪payload⟫`, which would launch a worker with nothing to do and fail
for a reason pointing nowhere near the missing task.

`⟪prompt⟫` is one line naming the payload by absolute path — *Read `<path>` in full and do
exactly what it says.* — and **never the payload itself**, which can pass the operating
system's argument limit and holds quotes, backticks and file names from the audited tree.
`⟪input⟫` and `⟪cwd⟫` are opaque per-attempt tokens: no argument a worker can read tells it
which slot found what.

### What the driver adds around the command

Every attempt is launched through the supervisor the diff-review skill ships,
found beside this one by default and overridable with `--supervisor`. The driver supplies
`--idle`, `--deadline`, `--cwd`, `--display`, `--findings`, `--result-mode` and **both of
the supervisor's opt-in flags**, and neither is optional here:

- `--status-detail` puts the terminal event's own error text on the status line. Without it
  a quota refusal arrives as *the reviewer exited 1*, and every pending unit spends its
  allowance against an account that is already refusing.
- `--max-capture-bytes` bounds each retained representation separately. Without it one
  worker's runaway output is held whole in the supervisor's memory, four hundred times over.
  Over the cap, assistant text is dropped **from the front** with the marker prepended, so a
  capped reply still ends in its closing object; a reply whose closing object was itself cut
  is infrastructure, never an older object promoted in its place. A single output line
  larger than the cap cannot be cut without changing what it says, so it ends the attempt
  as a capture overflow, which the driver also files as infrastructure and charges to
  nobody.

### Choosing a result mode

- `stream-transcript` — the supervisor concatenates the worker's message text and the
  driver takes the **last** JSON object in it, allowing a closing code fence. The default,
  and the one to reach for.
- `external-file` — the worker writes `⟪transcript⟫` itself. Use it where the runtime can
  be told to emit the object alone, with a schema flag pointed at the unit's own
  `⟪schema⟫`: the file is then copied rather than parsed out of prose.
- `stream-json-result-event` — the supervisor reads a terminal result event. Available, and
  the wrong choice here: an enforced object lands on that event for the reviewer schema the
  supervisor knows, not for a unit's.

**Never a verdict-extraction flag of the supervisor's own.** That extraction is keyed to
diff-review's verdict fields, not to a unit's schema, so it cannot land one of these
replies.

## When a run stops

`review_panel_run.py status <rundir>` prints what the run directory says, writes nothing,
and takes no lock — so it answers while a run is going, which is when an operator wants it.

An attempt whose outcome **cannot be proven** — no complete status record past its own
deadline and grace, or a claim with no spawn record — quarantines its unit. It is never
re-spawned and never landed, and the run stops and names it, because the worker may still
be alive and writing.

```
review_panel_run.py resolve-attempt <rundir> <unit> <attempt> --retry|--fail \
    --reason "…" [--stopped-confirmed]
review_panel_run.py resolve-unit <rundir> <unit> --grant-launches <n>|--fail --reason "…" \
    [--stopped-confirmed]
```

- Both **require `--stopped-confirmed`**, your attestation that the old supervisor and its
  worker are gone. The driver cannot establish it: the supervisor detaches its worker and
  says so. Without the attestation either command refuses — `--retry` because a second
  worker would start beside a live one, `--fail` because the attempt's capacity
  reservation would never be released, which at one worker per slot holds the slot for the
  rest of the run. With it, `--fail` publishes the unit's error, releases the reservation,
  and that attempt's working copy becomes deletable.
- `resolve-unit --fail` asks for the same attestation while the unit has an attempt nobody
  can account for, and with it fails that attempt too, each with its own record. The
  reservation belongs to the attempt, not the unit, so a unit failed over one released
  nothing and held the slot for the rest of the run. At the launch ceiling, where every
  attempt is adjudicated, it asks for nothing.
- `resolve-unit --grant-launches` is the way out of the launch-limit quarantine, which a
  unit reaches when repeated infrastructure faults — not its own answers — used up its
  launches. The grant is itself a durable record, so replaying the run reaches the same
  ceiling every time.
- Every resolution is written once and takes precedence over a status arriving later, which
  is recorded as superseded. Nothing ever fabricates a supervisor status. The one record the
  driver writes itself is for a supervisor it started and saw exit without one: marked as
  the driver's, carrying the exit code and the end of stderr, written only over an empty
  file, and adjudicated as infrastructure that stops the run.

A **paused** run is not a quarantined unit and needs no resolution: a storage fault, a host
refusal or a provider outage stops the run having adjudicated nothing. Free the space,
restore the permission, restart the provider, and run the same command again.

One ending is neither, and it names a slot: *slot B landed no unit*. That slot answered
none of the units it was given, so nothing it was asked to read reached the run and every
candidate addressed to it is unresolved — no report could say a finding was checked by a
unit that did not raise it. The driver stops at the first round boundary where nothing is
left that could change it, so the rounds that could not have rescued it are not spawned. It
is not resumable: those units are already answered by an error. Read `dispatch/<unit>/` for
what each attempt did, put that right, and plan a new run.
