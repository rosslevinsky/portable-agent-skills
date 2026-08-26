#!/usr/bin/env python3
"""review_runner.py — supervise ONE adversarial (cross-model) code review.

Launches a reviewer CLI as a child process and supervises it for exactly as long as the
review runs, then reports and exits. It:

  * tees the child's raw stream to an optional **display log** (append-only, best-effort)
    and updates a liveness heartbeat on **every chunk** received,
  * routes the authoritative **verdict** to a *separate* findings file,
  * enforces an **idle/heartbeat timeout**, an absolute **wall-clock deadline** and child
    **exit**,
  * owns the child's PID/process-group, so the kill and the exit status are real.

Liveness is judged from the child's live stream the supervisor reads in-process — never by
re-reading the display log, which stays a pure, read-by-nothing artifact.

**It writes only files it creates, and removes only those.** ``--findings`` and
``--verdict-json`` must not already exist; the run refuses to start otherwise. So a gate
can never read a previous run's verdict as this one's. ``--display`` is the documented
exception: append-only, shared and never removed.

The output chain is unbuffered end to end: the reviewer runs per-event-flushed, and the
supervisor reads raw chunks (``os.read``) so the heartbeat ticks per chunk rather than per
newline. A silent window is therefore a genuine stall, not buffering.

Stdlib only. The reviewer command is passed as argv DATA after ``--``; no branded CLI name
is baked in here, and ``cmd[0]`` is resolved with ``shutil.which`` so Windows ``.cmd``
shims work. Every SUPERVISED run prints exactly one JSON ``{"status": ...}`` line to stdout
— including when the supervisor is itself signalled — while ``--help`` is an ordinary
argparse path. The status is ``ok`` | ``idle_timeout`` | ``deadline`` | ``error``, with a
``reason`` on every non-``ok``, and exit 0 only on a clean review.

Known limitation — native Windows batch shims: if ``shutil.which`` resolves the reviewer to
a ``.cmd``/``.bat``, Windows runs it through the shell, which reinterprets ``%VAR%`` / ``&``
outside Python's quoting. Prefer a non-shim executable, or run under WSL/Git-Bash.
"""
import argparse
import codecs
import contextlib
import itertools
import json
import math
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path


def _emit(status, **extra):
    """Print the one-line JSON status contract; return the process exit code."""
    payload = {"status": status}
    payload.update(extra)
    print(json.dumps(payload))
    return 0 if status == "ok" else 1


# --- structured-output schema plumbing ---------------------------------------
# ONE schema file (``review-schema.json``, shipped beside this script) feeds both runtimes,
# which disagree on how a schema is passed: one flag takes a FILE PATH, the other takes it
# INLINE. Rather than duplicating the document per runtime — or making the caller shell out
# to `cat`, which no native-Windows caller can do — the adapter writes a placeholder in the
# reviewer argv and this substitutes it.
PLACEHOLDER_OPEN = "⟪"
PLACEHOLDER_CLOSE = "⟫"
SCHEMA_PATH_MARKER = f"{PLACEHOLDER_OPEN}schema_path{PLACEHOLDER_CLOSE}"
SCHEMA_JSON_MARKER = f"{PLACEHOLDER_OPEN}schema_json{PLACEHOLDER_CLOSE}"

# A decoded object is only accepted as the verdict when it carries EVERY required
# top-level field, in a usable shape (see ``_is_verdict``). Strictness is safe here because
# a missing verdict degrades — the narrative transcript is still the primary product —
# whereas adopting unrelated JSON out of a transcript would be silently wrong.
VERDICT_KEYS = ("findings", "overall", "blocking_count")


def _substitute_schema(cmd, schema):
    """Replace the schema placeholders in ``cmd``; return (argv, error_reason).

    The document is decoded and re-serialized compactly rather than passed through verbatim,
    so a malformed schema is caught HERE instead of inside a spawned CLI's flag parser. A
    marker with no ``--schema`` (or an unreadable one) is a hard error: the caller asked for
    enforcement, so silently launching an unenforced review would misreport what ran.
    """
    markers_used = any(
        SCHEMA_PATH_MARKER in part or SCHEMA_JSON_MARKER in part for part in cmd
    )
    if not markers_used:
        return cmd, None
    if not schema:
        return None, (
            f"reviewer argv uses {SCHEMA_PATH_MARKER}/{SCHEMA_JSON_MARKER} but no "
            f"--schema was given"
        )
    try:
        document = json.loads(Path(schema).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return None, f"--schema is missing, unreadable, or not valid JSON: {exc}"
    path_form = str(Path(schema).resolve())
    json_form = json.dumps(document, separators=(",", ":"))
    return [
        part.replace(SCHEMA_PATH_MARKER, path_form).replace(
            SCHEMA_JSON_MARKER, json_form
        )
        for part in cmd
    ], None


def _is_verdict(obj):
    """True if ``obj`` is a dict carrying every required field in a usable shape.

    Key presence alone is not enough: a caller ITERATES ``findings`` to gate a merge, so an
    object whose ``findings`` is a number would be published and then crash downstream. The
    check stays structural rather than a full schema validation — a real verdict must never
    be discarded over a slip in one finding's field.
    """
    return (
        isinstance(obj, dict)
        and all(k in obj for k in VERDICT_KEYS)
        and isinstance(obj.get("findings"), list)
        and isinstance(obj.get("overall"), str)
    )


# How many `{` positions to try, counted back from the end. A verdict is asked for at the
# CLOSE of the review, so the tail is where it is; this bounds a pathological document
# (one `{` per byte) without changing the answer for any real transcript.
_MAX_VERDICT_SCAN_STARTS = 20000


def _scan_verdict(text):
    """The LAST complete verdict object embedded in ``text``, or None.

    Last wins: in a narrative the final object is the verdict, not a sketch from
    earlier in the reasoning. Handles a bare object, a fenced one, and one closing a
    paragraph of prose.
    """
    if not text:
        return None
    try:
        whole = json.loads(text.strip())
    except (ValueError, RecursionError):
        pass
    else:
        if _is_verdict(whole):
            return whole

    # Scan the `{` positions from the END and stop at the first verdict — semantically
    # identical to scanning forward and keeping the last hit, but it stops there instead of
    # decoding every candidate. The forward form attempted one raw_decode per `{` in a
    # transcript that routinely runs to megabytes, making it quadratic, and it runs AFTER
    # the supervision loop has exited, so neither --idle nor --deadline bounds it.
    #
    # RecursionError is caught alongside ValueError because raw_decode recurses once per
    # nesting level: deeply nested JSON raised straight past `except ValueError` and turned
    # a COMPLETED review into status: error.
    decoder = json.JSONDecoder()
    starts = [index for index, char in enumerate(text) if char == "{"]
    for index in reversed(starts[-_MAX_VERDICT_SCAN_STARTS:]):
        try:
            candidate, _ = decoder.raw_decode(text, index)
        except (ValueError, RecursionError):
            continue
        if _is_verdict(candidate):
            return candidate
    return None


def _extract_verdict(text, event=None):
    """Resolve the structured verdict, preferring the ENFORCED channel.

    The two rung-1 runtimes deliver a schema-validated object very differently, and only one
    puts it in the transcript:

      1. ``structured_output`` on the terminal result event, where a runtime honoring an
         inline schema flag puts the validated object. That runtime's assistant text stays
         PROSE, so a text scan alone would silently find nothing.
      2. The same event's ``result`` payload, carrying that object as a JSON string.
      3. A scan of the reviewer's own text — the unenforced path.

    Checked in that order so the enforced object always wins over anything the model typed.
    """
    if isinstance(event, dict):
        structured = event.get("structured_output")
        if _is_verdict(structured):
            return structured
        payload = event.get("result")
        if isinstance(payload, str):
            found = _scan_verdict(payload)
            if found is not None:
                return found
    return _scan_verdict(text)


KNOWN_SEVERITIES = ("blocker", "major", "minor", "nit")
BLOCKING_SEVERITIES = ("blocker", "major")


def _reconcile_blocking_count(verdict):
    """Correct ``blocking_count`` from the findings; return a note if it changed.

    A phase gate acts on this number, so a model that miscounts its own findings would
    under- or over-gate a merge. The count is *derived* — ``findings`` is the authority — so
    publishing a known-wrong number and merely warning would leave the trap armed.

    **Recounting FAILS CLOSED, because on the unenforced rungs the severity strings are
    unvalidated model output.** Matching them exactly would let an off-enum spelling
    ("Blocker", "critical") fall out of the tally and rewrite a two-blocker review to
    ``blocking_count: 0``. So severities compare case-insensitively, and two rules hold:

    1. An UNDERIVABLE count (any unrecognized severity) is never lowered, and never zero;
       the unknown value is named instead and a human decides.
    2. A POSITIVE claim is never lowered to zero even when every severity IS recognized. 0 is
       what a merge gate reads as clean, so it is floored at 1 — enough to stop the gate, not
       enough to assert findings nobody listed.

    The verdict is never withheld over this: dropping a real review because the model fumbled
    arithmetic would be worse than fixing it.
    """
    counted, unknown = 0, []
    for finding in verdict.get("findings", []):
        raw = finding.get("severity") if isinstance(finding, dict) else None
        severity = raw.strip().lower() if isinstance(raw, str) else None
        if severity in BLOCKING_SEVERITIES:
            counted += 1
        elif severity not in KNOWN_SEVERITIES:
            unknown.append("<missing>" if raw is None else str(raw))

    claimed = verdict.get("blocking_count")
    if not isinstance(claimed, int) or isinstance(claimed, bool):
        claimed = None

    if unknown:
        # Cannot derive the count, so it must not be ZERO.
        #
        # `max(counted, claimed)` is not enough: a verdict whose findings are ALL off-enum
        # leaves both numbers at zero, so a review reporting one finding at severity
        # `critical` — the obvious word for a model to reach for, and not in the enum —
        # goes out as machine-clean and a gate merges it.
        #
        # Deliberately minimal. Counting every unknown AS blocking overshoots: three
        # non-issues spelled "informational" would gate a clean review on three phantom
        # blockers. Refusing to publish zero is the whole property a gate needs.
        floor = max(counted, 1)
        safe = floor if claimed is None else max(floor, claimed)
        verdict["blocking_count"] = safe
        listed = ", ".join(sorted(set(unknown))[:3])
        # Says what the two lines above actually do. Claiming "each was counted as
        # blocking" is the rule the paragraph above records as tried and REJECTED — and it
        # contradicted its own number: two unrecognized findings, "each counted", published
        # as 1. Worse the other way, a verdict with two real blockers beside a `critical`
        # went out as `blocking_count: 2` asserting the unknowns were included, so a human
        # reconciling the tally fixed the two blockers and merged with the others open.
        return (
            f"{len(unknown)} finding(s) carry an unrecognized severity ({listed}); "
            f"blocking_count could not be derived from them, so the count was floored at "
            f"{safe} rather than lowered — the unrecognized findings are NOT in it; "
            f"gate on the findings"
        )

    if claimed == counted:
        return None

    if claimed is not None and claimed > 0 and counted == 0:
        # A POSITIVE claim over zero derivable blockers, every severity recognized. The
        # off-enum floor above cannot fire, so the recount would publish ZERO for a verdict
        # that just said 3 — the shape the unenforced rungs produce, where the model totals
        # the blockers it described in prose and then emits `findings: []`. ZERO is what a
        # merge gate reads as "clean", so publishing it turns a review reporting blockers
        # into a pass.
        #
        # Floored at 1, not raised to the claim: the findings are the authority, and
        # inventing `blocking_count: 3` over an empty array asserts three findings a human
        # would go looking for. 1 says only what is certain.
        verdict["blocking_count"] = 1
        return (
            f"blocking_count was {claimed!r} but no finding is recorded at "
            f"blocker/major severity — the verdict contradicts itself. Floored at 1 "
            f"rather than lowered to 0, which a gate reads as clean; the findings list "
            f"does NOT account for the {claimed} claimed — gate on the findings and "
            f"read the narrative"
        )

    verdict["blocking_count"] = counted
    return (
        f"blocking_count was {claimed!r} but {counted} finding(s) are blocker/major; "
        f"corrected to {counted} from the findings"
    )


def _same_path(first, second):
    """True if both strings name the same file, comparing resolved absolute paths.

    ``os.path.realpath`` rather than ``samefile``: neither path need exist yet, and this is
    asked before anything is written. It resolves symlinks and ``..``.

    Compared case-folded as well as exactly, because `findings.json` and `FINDINGS.JSON` are
    ONE file on Windows and macOS and the refusal has to fire there. ``str.lower`` rather
    than ``os.path.normcase``: normcase is the identity on POSIX, and macOS is POSIX with a
    case-insensitive volume by default. The cost on a case-sensitive filesystem is refusing
    an invocation naming two files differing only in case — a clear message rather than a
    deletion, and one behaviour on every platform.
    """
    resolved_first, resolved_second = os.path.realpath(first), os.path.realpath(second)
    return (resolved_first == resolved_second
            or resolved_first.lower() == resolved_second.lower())


def _terminate(proc):
    """Best-effort kill of the child (its process group on POSIX); never raises.

    POSIX: SIGTERM then SIGKILL to the child's session group (start_new_session), which
    reaches ordinary grandchildren. A descendant that calls ``setsid()`` leaves that group
    and survives — and, still holding the stdout pipe, keeps the reader from seeing EOF, so
    the drain wait below runs its full timeout. The guarantee is group-wide, not absolute.

    **Both rungs go to the GROUP, and neither is conditional on the leader.** Returning as
    soon as ``proc.wait()`` succeeds after the SIGTERM is the common wedge: the leader
    exiting says nothing about descendants that inherited its pipes.

    Windows: the tree, via ``taskkill /F /T``, then the direct child. ``terminate()`` reaches
    what we spawned and nothing beneath it, and on Windows that is frequently a ``.cmd``
    shim — killing it leaves the model CLI running and spending.
    """
    if proc.poll() is not None:
        return
    if os.name != "posix":
        try:
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                           capture_output=True, timeout=5)
        except (OSError, subprocess.SubprocessError):
            pass  # fall through to terminate/kill, which is what this always did
    for hard in (False, True):
        try:
            if os.name == "posix":
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL if hard else signal.SIGTERM)
            else:
                if proc.poll() is not None:
                    return
                proc.kill() if hard else proc.terminate()
        except (ProcessLookupError, PermissionError, OSError):
            return
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass
        # No early return on POSIX: the leader is reaped and the group may not be.


def _reap_group(proc):
    """Kill whatever is LEFT of the child's process group; never raises.

    :func:`_terminate` cannot do this: it returns immediately when the child is already gone,
    which is exactly the case here. The reviewer exited **cleanly** and a grandchild it left
    behind still holds the inherited stdout pipe, so the reader never sees EOF, the drain
    runs its full timeout, and the tail of a COMPLETED review goes missing.

    The group id is ``proc.pid``: ``start_new_session`` makes the child a group leader, and
    the GROUP outlives the leader while it still has members. ``os.getpgid`` is unusable
    because ``poll()`` has already reaped the child.

    POSIX only, and Windows is UNCHANGED by this — said plainly because the fix reads as
    platform-neutral and is not.
    """
    if os.name != "posix":
        return
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        pass


def _capture_result(line, state, lock):
    """Record the raw payload of any ``type == "result"`` event; last (terminal) one wins.

    Validity is decided later by ``_valid_verdict`` against the FINAL result, so a
    ``success`` followed by an ``error`` result correctly resolves to the error.
    """
    stripped = line.strip()
    if not stripped:
        return
    try:
        event = json.loads(stripped)
    except ValueError:
        return
    if not isinstance(event, dict) or event.get("type") != "result":
        return
    with lock:
        state["last_result"] = event


def _valid_verdict(event):
    """Return the verdict text iff ``event`` is an *affirmatively* successful result.

    Requires ``subtype == "success"`` (never an absent/unknown subtype) and ``is_error``
    not True, with a non-empty string payload. Anything else → None (caller falls open).
    """
    if not isinstance(event, dict):
        return None
    if event.get("subtype") != "success":
        return None
    if event.get("is_error") is True:
        return None
    payload = event.get("result")
    if not isinstance(payload, str) or not payload.strip():
        return None
    return payload


def _event_text(event):
    """Extract the assistant's human-visible text from one JSONL stream event.

    Handles both runtimes' *complete-message* events — Codex ``agent_message`` items and
    Claude ``assistant`` messages — and returns None for partial, reasoning, tool, and
    result events, so the transcript is the reviewer's full output with no duplication.
    """
    if not isinstance(event, dict):
        return None
    if event.get("type") == "item.completed":  # Codex --json
        item = event.get("item") or {}
        if item.get("type") == "agent_message" and isinstance(item.get("text"), str):
            return item["text"]
        return None
    if event.get("type") == "assistant":  # Claude stream-json
        if event.get("parent_tool_use_id"):  # forwarded sub-agent text, not the main reviewer
            return None
        parts = [b.get("text") for b in ((event.get("message") or {}).get("content") or [])
                 if isinstance(b, dict) and b.get("type") == "text" and isinstance(b.get("text"), str)]
        return "".join(parts) or None
    return None


def _capture_terminal(line, state, lock):
    """Note the reviewer's terminal success/failure from a result / turn event.

    Claude ends with a ``result`` event (``subtype``/``is_error``); Codex ends with
    ``turn.completed`` (ok) or ``turn.failed`` (failed). Last terminal wins. A partial
    transcript followed by a terminal failure must NOT be accepted as a verdict.
    """
    stripped = line.strip()
    if not stripped:
        return
    try:
        event = json.loads(stripped)
    except ValueError:
        return
    if not isinstance(event, dict):
        return
    kind = event.get("type")
    verdict = None
    if kind == "result":  # Claude terminal
        verdict = "ok" if (event.get("subtype") == "success" and event.get("is_error") is not True) else "failed"
    elif kind == "turn.completed":  # Codex success terminal
        verdict = "ok"
    elif kind in ("turn.failed", "error"):  # Codex / generic failure terminal
        verdict = "failed"
    if verdict is not None:
        with lock:
            state["terminal"] = verdict
            # Keep the event itself, not just its ok/failed verdict: a runtime that
            # honors an inline schema flag returns the validated object on THIS event
            # (``structured_output``) while its assistant text stays prose.
            state["terminal_event"] = event


def _capture_transcript(line, state, lock):
    """Append one complete event's assistant text to the running transcript."""
    stripped = line.strip()
    if not stripped:
        return
    try:
        event = json.loads(stripped)
    except ValueError:
        return
    text = _event_text(event)
    if text:
        with lock:
            state["transcript"].append(text)


def _consume_jsonl(line, mode, state, lock):
    """Feed one JSONL line to the capture appropriate for the result mode."""
    if mode == "stream-json-result-event":
        _capture_result(line, state, lock)
    elif mode == "stream-transcript":
        _capture_transcript(line, state, lock)
        _capture_terminal(line, state, lock)


def _display_decoder():
    """A UTF-8 decoder for the display log, carried ACROSS chunks.

    Module level so a test can drive prescribed chunk boundaries through the exact object the
    reader uses; racing a child's flushes against the reader thread would be
    scheduling-dependent, and a test that can pass against the regression proves nothing.

    Incremental, ``utf-8``, and ``"replace"`` so a genuinely undecodable byte still reaches
    the log as U+FFFD instead of raising inside a best-effort display path. Feed it every
    chunk in order and flush once at EOF with ``decode(b"", final=True)`` — the flush is what
    renders a sequence the child truncated.
    """
    return codecs.getincrementaldecoder("utf-8")("replace")


def _drain_stderr(stream, write_display, state, lock, done):
    """Tee the child's stderr to the display log; it never reaches the JSONL parser.

    Its own pipe, because merging it into stdout let a warning land in the MIDDLE of a JSONL
    line: a pipe write above PIPE_BUF is not atomic, so the two descriptors interleave and
    the line stops parsing. When that line was the terminal result event, a completed review
    was reported as an error and thrown away.
    """
    decoder = _display_decoder()
    fd = stream.fileno()
    try:
        while True:
            try:
                data = os.read(fd, 65536)
            except OSError:
                break
            if not data:
                break
            with lock:
                state["last_activity"] = time.monotonic()
            write_display(decoder.decode(data))
    finally:
        tail = decoder.decode(b"", final=True)
        if tail:
            write_display(tail)
        done.set()


def run(args):
    # THE OUTPUT PATHS MUST NOT ALREADY EXIST. That one rule is the whole ownership model.
    # A path that did not exist when the run started and exists now was brought into being
    # by THIS run, so it is the only thing the supervisor may ever remove — no git, no exit
    # codes, no ownership heuristics. The invariant it buys: a gate cannot read a previous
    # run's verdict as this one's, because a run that would have collided never started.
    #
    # EVERY pair, not just the two authoritative outputs. With `--display` omitted from the
    # comparison, pointing it at the `--findings` path lets the display handle wrap
    # start/end markers around the reviewer's write, and the supervisor then reads the
    # corrupted file as a successful result.
    for (first, first_flag), (second, second_flag) in itertools.combinations(
            ((args.findings, "--findings"), (args.verdict_json, "--verdict-json"),
             (args.display, "--display")), 2):
        if first and second and _same_path(first, second):
            return _emit("error", reason=(
                f"{first_flag} and {second_flag} name the same path, so they would "
                f"overwrite each other and whichever landed last would be read as both. "
                f"Give them separate paths"
            ))

    # A RELATIVE output path is refused when `--cwd` is given, rather than guessed at. The
    # child is launched with `cwd=args.cwd` while the supervisor resolves these paths against
    # its own working directory, so `--cwd /repo --findings findings.md` had the child create
    # `/repo/findings.md` and the supervisor look in the launch directory: it reported that
    # the reviewer wrote nothing, and a retry could then overwrite a pre-existing file
    # without ever having checked it.
    if args.cwd:
        for path, flag in ((args.findings, "--findings"),
                           (args.verdict_json, "--verdict-json"),
                           (args.display, "--display")):
            if path and not os.path.isabs(path):
                return _emit("error", reason=(
                    f"{flag} is relative ({path}) and --cwd is set, so the supervisor and "
                    f"the reviewer would resolve it against different directories. Pass "
                    f"an absolute path"
                ))


    # Two operations, deliberately, because they answer two different questions and the
    # ordering contract for one is not the ordering contract for the other.
    #
    # This is the REFUSAL: it runs before every other check, so a colliding path is reported
    # first and nothing else is attempted. `lexists`, so a dangling symlink counts as present
    # rather than being followed. It creates nothing.
    #
    # The CLAIM — exclusive creation, which is what actually establishes ownership — happens
    # later, immediately before the child can write.
    for path, flag in ((args.findings, "--findings"),
                       (args.verdict_json, "--verdict-json")):
        if path and os.path.lexists(path):
            return _emit("error", reason=(
                f"{flag} names {path}, which already exists. This supervisor writes only "
                f"files it creates and removes only those, so it will not take over a "
                f"path it did not make — whether that is source, someone else's output, "
                f"or a previous review. Delete it yourself if it is stale, or name a "
                f"path that does not exist"
            ))

    for name, val in (("--idle", args.idle), ("--deadline", args.deadline)):
        if not math.isfinite(val) or val <= 0:
            return _emit("error", reason=f"{name} must be a finite positive number")

    cmd = args.cmd[1:] if args.cmd and args.cmd[0] == "--" else list(args.cmd)
    if not cmd:
        return _emit("error", reason="no reviewer command given")

    cmd, schema_error = _substitute_schema(cmd, args.schema)
    if schema_error is not None:
        return _emit("error", reason=schema_error)

    # PATH only — never the current directory. On Windows `shutil.which` searches CWD first,
    # mirroring cmd.exe, and this supervisor's whole job is to review a checkout it does not
    # trust: a repository carrying `codex.exe` at its root would be RUN by the tool sent to
    # read it. An absolute or explicitly relative path stays honoured — that is the caller
    # naming a binary, not a checkout supplying one.
    if os.path.dirname(cmd[0]):
        # ...unless `--cwd` is also given, because then "explicitly relative" names two
        # different files. `os.path.isfile` below answers relative to the SUPERVISOR's
        # directory; `Popen(cmd, cwd=...)` on POSIX execs after the chdir, so it resolves
        # against `--cwd` — one file checked, another run. Windows resolves against the
        # calling process's directory instead, so the platforms disagreed with each other.
        #
        # Refused rather than resolved against `--cwd`: that directory is the checkout under
        # review. A caller naming a program by path can name it absolutely.
        if args.cwd and not os.path.isabs(cmd[0]):
            return _emit("error", reason=(
                f"reviewer program {cmd[0]!r} is a relative path and --cwd was given, "
                f"so it names one file to check and another to run. Pass an absolute "
                f"path, or a bare program name to be found on PATH"
            ))
        exe = cmd[0] if os.path.isfile(cmd[0]) else None
    else:
        search_path = os.environ.get("PATH", os.defpath)
        exe = shutil.which(cmd[0], path=search_path)
        if exe is not None and os.path.dirname(os.path.abspath(exe)) == os.getcwd():
            # Reached only when CWD is genuinely on PATH; treat it as not found rather
            # than silently running the checkout's copy.
            if os.getcwd() not in search_path.split(os.pathsep):
                exe = None
    if exe is None:
        return _emit("error", reason=f"reviewer CLI not found on PATH: {cmd[0]}")
    # ABSOLUTE, so the child execs the exact file that was just checked. Without this a
    # relative PATH entry leaves `Popen` to redo the resolution under whatever directory it
    # runs in. The check and the exec have to name one file, on both platforms.
    cmd[0] = os.path.abspath(exe)

    # The findings directory is essential; a failure here is fatal.
    try:
        if args.findings:
            Path(args.findings).parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return _emit("error", reason=f"setup failed: {exc}")

    popen_kw = dict(stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    stdin=subprocess.DEVNULL,  # a reviewer that probes stdin gets EOF, never hangs
                    bufsize=0, cwd=args.cwd or None)
    if os.name == "posix":
        popen_kw["start_new_session"] = True  # own process group, for a clean group kill
    # Claimed HERE, after every other refusal and immediately before the child can write.
    # "Creates nothing when it refuses" is the promise, so the claim has to be the last thing
    # that happens before it stops being able to keep it — claiming earlier leaves empty files
    # behind on every refusal path, and the next attempt is rejected for a collision this
    # program caused.
    #
    # OWNERSHIP IS TAKEN, NOT OBSERVED. An existence check followed by writes by name is
    # check-then-act: between the two, anything may put a symlink at that path. An existence
    # check can say "nothing was here a moment ago"; it cannot say "this is mine".
    # `O_CREAT | O_EXCL` says both in one syscall, and POSIX requires it to fail on a symlink.
    #
    # The handle is closed immediately rather than held: in external-file mode the reviewer
    # writes this path itself, often by rename.
    owned: list[Path] = []
    for path, flag in ((args.findings, "--findings"),
                       (args.verdict_json, "--verdict-json")):
        if not path:
            continue
        try:
            os.close(os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600))
        except FileExistsError:
            for created in owned:  # leave nothing behind from a refused start
                with contextlib.suppress(OSError):
                    created.unlink()
            return _emit("error", reason=(
                f"{flag} names {path}, which already exists. This supervisor writes only "
                f"files it creates and removes only those, so it will not take over a "
                f"path it did not make — whether that is source, someone else's output, "
                f"or a previous review. Delete it yourself if it is stale, or name a "
                f"path that does not exist"
            ))
        except OSError as exc:
            for created in owned:
                with contextlib.suppress(OSError):
                    created.unlink()
            return _emit("error", reason=f"{flag} ({path}) could not be created: {exc}")
        owned.append(Path(path))

    # Arm the signal handlers BEFORE spawning. A signal delivered between Popen returning
    # and the handlers being installed would otherwise break both contracts at once: no
    # JSON status printed, and the just-created child left ORPHANED — start_new_session
    # detaches its signal fate from ours (which is what makes the group kill clean), so
    # nothing reaps it and neither the idle timer nor the deadline governs it any more.
    # The handler reads the child out of a mutable holder, so it is correct both before
    # the child exists and after.
    signal_state = {"proc": None, "reported": False}

    def _on_signal(signum, _frame):
        if signal_state["reported"]:
            os._exit(1)  # status already emitted; never append a second one
        signal_state["reported"] = True
        child = signal_state["proc"]
        if child is not None:
            _terminate(child)
            # ...and then whatever the group still holds. `_terminate` returns immediately
            # when the leader is already gone, so on the signal path that early return was
            # the whole cleanup and a descendant still holding the inherited pipes survived
            # the supervisor by design. This is the function written for that case; the
            # normal exit path already calls it and the signal path did not.
            _reap_group(child)
        name = signal.Signals(signum).name if hasattr(signal, "Signals") else signum
        # The same cleanup a failed review does, because an interrupted review IS a failed
        # one and leaves the same residue. Removing the up-front invalidation made "refuse
        # if it exists" the guard — which means anything this run leaves behind blocks the
        # retry. SIGTERM during an external-file review left the reviewer's findings file in
        # place and the legitimate rerun was refused as "already exists".
        #
        # `os.unlink` is a single syscall and safe here: the handler is already committed to
        # `os._exit`, so a failure to remove leaves the operator a file they can delete.
        for path in owned:
            try:
                os.unlink(path)
            except OSError:
                pass
        print(json.dumps({
            "status": "error",
            "reason": f"supervisor interrupted by {name}",
            "exit_code": child.poll() if child is not None else None,
        }), flush=True)
        os._exit(1)  # must not fall through into the normal reporting path

    previous_handlers = []
    for _sig in (getattr(signal, "SIGTERM", None), getattr(signal, "SIGINT", None)):
        if _sig is not None:
            try:
                previous_handlers.append((_sig, signal.signal(_sig, _on_signal)))
            except (ValueError, OSError):
                pass  # not the main thread, or the platform disallows it — best effort

    def _restore_handlers():
        for _s, _p in previous_handlers:
            try:
                signal.signal(_s, _p)
            except (ValueError, OSError):
                pass

    try:
        proc = subprocess.Popen(cmd, **popen_kw)
    except (FileNotFoundError, OSError) as exc:
        signal_state["reported"] = True
        _restore_handlers()
        # Release the claimed outputs, exactly as a refused CLAIM already does. Taking
        # ownership and then failing to launch left two empty files behind — and the next
        # attempt refuses a path it did not create, so a retry failed for a reason that had
        # nothing to do with the retry. A bad `--cwd` is enough to reach this. Only files
        # THIS call created are removed, which is the same rule the refusal states.
        for created in owned:
            with contextlib.suppress(OSError):
                created.unlink()
        return _emit("error", reason=f"launch failed: {exc}")
    signal_state["proc"] = proc


    # Open the display log only AFTER a successful launch (so a failed launch leaves no
    # unmatched "start" marker), and treat every display step as best-effort.
    display_fh = None
    if args.display:
        try:
            Path(args.display).parent.mkdir(parents=True, exist_ok=True)
            display_fh = open(args.display, "a", encoding="utf-8")  # append: never truncate a shared log
        except OSError:
            display_fh = None  # display is best-effort; never abort a review for it

    def write_display(text):
        if display_fh is None:
            return
        try:
            display_fh.write(text)
            display_fh.flush()
        except OSError:
            pass

    write_display("[review_runner] start\n")

    state = {"last_activity": time.monotonic(), "last_result": None, "transcript": [],
             "terminal": None, "terminal_event": None}
    lock = threading.Lock()
    done = threading.Event()
    fd = proc.stdout.fileno()

    def reader():
        buf = b""
        # One decoder ACROSS chunks, because a read boundary falls wherever the bytes
        # happened to arrive — mid-character as readily as anywhere else. Decoding each
        # chunk on its own turned one em dash into three replacement characters in the log a
        # human reads. Reader-local: this thread is its only user, so it needs no lock.
        # Only the DISPLAY path needs this — the JSONL path accumulates raw bytes and
        # decodes whole lines, so a split inside a line never reaches it.
        display_decoder = _display_decoder()
        try:
            while True:
                try:
                    data = os.read(fd, 65536)
                except OSError:
                    break
                if not data:
                    break
                with lock:
                    state["last_activity"] = time.monotonic()  # heartbeat per CHUNK, not per line
                write_display(display_decoder.decode(data))
                if args.result_mode in ("stream-json-result-event", "stream-transcript"):
                    buf += data
                    while b"\n" in buf:
                        raw, buf = buf.split(b"\n", 1)
                        _consume_jsonl(raw.decode("utf-8", "replace"), args.result_mode, state, lock)
            if args.result_mode in ("stream-json-result-event", "stream-transcript") and buf.strip():
                _consume_jsonl(buf.decode("utf-8", "replace"), args.result_mode, state, lock)
        finally:
            # Flush whatever the decoder is still holding, on EVERY exit path. A child
            # killed mid-character leaves an incomplete sequence, and the decoder holds it
            # forever unless told the stream ended — so without this the tail disappears
            # from the log with nothing to say anything was lost.
            tail = display_decoder.decode(b"", final=True)
            if tail:
                write_display(tail)
            done.set()

    thread = threading.Thread(target=reader, daemon=True)
    thread.start()

    err_done = threading.Event()
    err_thread = threading.Thread(
        target=_drain_stderr,
        args=(proc.stderr, write_display, state, lock, err_done), daemon=True)
    err_thread.start()

    start = time.monotonic()
    status, reason = "ok", None
    while proc.poll() is None:
        now = time.monotonic()
        with lock:
            idle = now - state["last_activity"]
        if now - start >= args.deadline:
            status, reason = "deadline", f"no completion within {args.deadline:.0f}s"
            _terminate(proc)
            break
        if idle >= args.idle:
            status, reason = "idle_timeout", f"no output for {args.idle:.0f}s"
            _terminate(proc)
            break
        time.sleep(0.5)

    # Split what used to be one 30s wait. A reader that has not hit EOF a few seconds after
    # the child exited is not slow — it is blocked on a descendant still holding the pipe —
    # so reap the group and give it the rest of the budget. Same 30s ceiling, but a completed
    # review no longer loses its tail.
    drained = done.wait(timeout=5)
    if not drained:
        _reap_group(proc)
        drained = done.wait(timeout=25)
    exit_code = proc.poll()
    err_drained = err_done.wait(timeout=5)
    if drained:
        # Only close the shared pipe once the reader has finished — no close-under-reader.
        try:
            proc.stdout.close()
        except OSError:
            pass
    if err_drained:
        try:
            proc.stderr.close()
        except OSError:
            pass
    if display_fh:
        # ALWAYS write the end marker, drained or not. It is the display log's only
        # completion signal and callers are told to treat it as one, so making it conditional
        # conflates three different states. `drained` is reported rather than implied.
        write_display(
            f"[review_runner] end status={status} exit={exit_code} drained={drained}\n"
        )
        # CLOSING, however, stays behind the drain guard. An undrained reader is still live
        # and a descendant holding the inherited pipe can wake it at any moment; closing the
        # handle underneath it turns that write into an uncaught ValueError and drops the
        # rest of the stream. Writing is best-effort and safe; closing is not.
        #
        # BOTH drains, not just stdout's — separate waits, separate threads, the SAME handle.
        # A reviewer that exits cleanly but leaves a helper holding the inherited stderr gives
        # drained=True with err_drained=False, and the close then lands under a live
        # _drain_stderr whose next write raises. The thread traceback prints AHEAD of the JSON
        # status line, so a caller capturing with 2>&1 reads a finished review as a hang.
        if drained and err_drained:
            try:
                display_fh.close()
            except OSError:
                pass

    findings_text = None  # what actually landed in --findings, for verdict extraction
    # Every write below is REVIEWER-DERIVED text, and every one takes `errors="replace"` for
    # the same reason the reads on this path do. JSON permits an unpaired `\ud800` escape and
    # Python's decoder produces the lone surrogate faithfully, so it reaches here intact. A
    # plain `write_text` then raises `UnicodeEncodeError` — a `ValueError`, which the
    # `except OSError` below did not catch — and a COMPLETED cross-model review was reported
    # as an error, so the caller fell open to a same-model reviewer: the one trade the skill
    # says must never be made. U+FFFD in one word does not compare to that.
    try:
        if status == "ok":
            if not drained:
                status, reason = "error", "reader did not drain child output"
            elif exit_code not in (0, None):
                status, reason = "error", f"reviewer exited {exit_code}"
            elif args.result_mode == "stream-json-result-event":
                with lock:
                    last = state["last_result"]
                payload = _valid_verdict(last) if last is not None else None
                if payload is None:
                    status, reason = "error", "no successful terminal result event"
                else:
                    Path(args.findings).write_text(
                        payload, encoding="utf-8", errors="replace")
                    findings_text = payload
            elif args.result_mode == "stream-transcript":
                with lock:
                    transcript = "\n\n".join(state["transcript"]).strip()
                    terminal = state["terminal"]
                if terminal != "ok":
                    status, reason = "error", "no successful terminal event — review incomplete or failed"
                elif not transcript:
                    status, reason = "error", "reviewer produced no text output"
                else:
                    # The FULL transcript stays the findings payload. The structured
                    # verdict is written alongside it, never in place of it: the
                    # reviewer's reasoning is what a human reads, and dropping it to
                    # keep only a parsed object would be a regression, not a
                    # simplification.
                    Path(args.findings).write_text(
                        transcript + "\n", encoding="utf-8", errors="replace")
                    findings_text = transcript
            else:  # external-file: require a fresh, non-empty verdict file
                fp = Path(args.findings)
                if not fp.exists() or fp.stat().st_size == 0:
                    status, reason = "error", "reviewer wrote no verdict"
                else:
                    findings_text = fp.read_text(encoding="utf-8", errors="replace")
    except (OSError, ValueError) as exc:
        # ValueError alongside OSError: `UnicodeEncodeError` and `UnicodeDecodeError` are
        # ValueErrors. The `errors="replace"` above should mean nothing here can raise one,
        # but a decoding surprise landing in the module-level `except BaseException` is what
        # turned a completed review into `status: error` once already.
        status, reason = "error", f"routing failed: {exc}"

    # Structured verdict — strictly additive and NEVER fatal. Enforcement exists only
    # on the rung that has a CLI schema flag; the in-harness sub-agent rung has none,
    # so a review that produced a good narrative but no parseable object is still a
    # successful review. Report the miss instead of failing it.
    verdict_path, verdict_reason = None, None
    if args.verdict_json and status == "ok":
        with lock:
            terminal_event = state["terminal_event"] or state["last_result"]
        verdict = _extract_verdict(findings_text, terminal_event)
        if verdict is None:
            verdict_reason = "no verdict object matching the schema in the reviewer output"
        else:
            verdict_reason = _reconcile_blocking_count(verdict)
            try:
                Path(args.verdict_json).parent.mkdir(parents=True, exist_ok=True)
                Path(args.verdict_json).write_text(
                    json.dumps(verdict, indent=2) + "\n",
                    encoding="utf-8", errors="replace",
                )
                verdict_path = args.verdict_json
            # Same widening as the routing block above, and for the same reason. This one
            # is belt to that fix's braces — `json.dumps` escapes a surrogate to ASCII, so
            # it cannot currently raise — but this write is REPORTED, never fatal, and it
            # must stay that way for every failure rather than for one kind of failure.
            except (OSError, ValueError) as exc:
                verdict_reason = f"could not write the verdict file: {exc}"

    # The claim created this path empty to hold it. If no verdict was written into it, remove
    # it — "no verdict" has always meant "no verdict file", and a caller that tests for the
    # file would otherwise read an empty one as a verdict that exists.
    if args.verdict_json and verdict_path is None:
        with contextlib.suppress(OSError):
            Path(args.verdict_json).unlink()

    extra = {}
    if args.verdict_json:
        extra = {"verdict": verdict_path, "verdict_reason": verdict_reason}

    # A review that did not succeed leaves the workspace as it found it. `owned` is the only
    # deletion this program performs, and every path in it was verified ABSENT at startup —
    # so whatever is there now was created by this run. There is nothing to decide.
    #
    # It is also what keeps the refuse-if-it-exists rule from being a trap: without it a
    # failed review would leave files behind that block the next attempt.
    if status != "ok":
        for path in owned:
            try:
                path.unlink()
            except FileNotFoundError:
                pass
            except OSError:
                pass  # best-effort: the status line below is the report that matters

    # Disarm before reporting. The child is already dead or drained, so the handlers have
    # nothing left to protect — and a signal arriving between here and process exit would
    # otherwise fire one and append a SECOND status line to a run that has already reported.
    # The `reported` flag alone would not close that window.
    signal_state["reported"] = True
    _restore_handlers()

    return _emit(status, reason=reason, exit_code=exit_code,
                 elapsed_s=round(time.monotonic() - start, 1),
                 findings=args.findings if status == "ok" else None,
                 **extra)


def main(argv=None):
    # PIN THE ENCODING FIRST, before argparse can print anything. `--schema`'s help text
    # interpolates the ⟪…⟫ markers, so on a console that cannot represent them argparse's own
    # print raises UnicodeEncodeError *inside* parse_args — which `except SystemExit` does not
    # catch, turning a documented "prints usage" into `{"status": "error"}`. `stderr` needs it
    # too, because argparse writes usage errors there.
    #
    # No line buffering is requested: the stdout contract is one JSON line at exit, and a
    # stream rejecting that extra argument would take the encoding pin down with it. Guarded
    # and per-stream, because an in-process caller redirecting to StringIO has no
    # `reconfigure`.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, TypeError, ValueError, OSError):
            pass  # no reconfigure, a different signature, or a stream that refuses
    ap = argparse.ArgumentParser(description="Supervise one cross-model review.")
    ap.add_argument("--idle", type=float, default=900.0,
                    help="idle/heartbeat timeout in seconds (kill on this much total silence)")
    ap.add_argument("--deadline", type=float, default=1800.0,
                    help="absolute wall-clock deadline in seconds (backstop)")
    ap.add_argument("--cwd", help="working directory to launch the reviewer in; with "
                                  "this set the reviewer program must be an absolute "
                                  "path or a bare name found on PATH, never a relative "
                                  "path")
    ap.add_argument("--display", help="optional path for the append-only display log")
    ap.add_argument("--findings", required=True,
                    help="path the authoritative verdict is written to / verified at. "
                         "Must NOT already exist: the supervisor only ever writes and "
                         "removes files it created")
    ap.add_argument("--schema",
                    help="path to the JSON Schema for the reviewer's structured verdict; "
                         f"substituted into the reviewer argv wherever {SCHEMA_PATH_MARKER} "
                         f"(as a file path) or {SCHEMA_JSON_MARKER} (as inline JSON) appears")
    ap.add_argument("--verdict-json",
                    help="optional path for the structured verdict extracted from the "
                         "reviewer's output; --findings still receives the full narrative. "
                         "Best-effort: a missing verdict is reported, never fatal. Must "
                         "not already exist, like --findings, and may not name the same "
                         "path")
    ap.add_argument("--result-mode", required=True,
                    choices=["external-file", "stream-json-result-event", "stream-transcript"],
                    help="external-file: the child writes --findings itself; "
                         "stream-json-result-event: extract the final JSONL result event; "
                         "stream-transcript: concatenate all of the reviewer's message text")
    ap.add_argument("cmd", nargs=argparse.REMAINDER, help="-- <reviewer argv ...>")
    try:
        args = ap.parse_args(argv)
    except SystemExit as exc:
        if exc.code not in (0, None):  # 2 = argparse usage error; keep the JSON contract
            print(json.dumps({"status": "error", "reason": "invalid runner invocation"}))
            return 1
        raise
    return run(args)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit:
        # The normal exit path: main() has already printed the status contract, so re-raise
        # rather than reporting a second, bogus one. This clause must come first — SystemExit
        # is a BaseException, and catching it below would append a spurious
        # `{"status": "error", ...}` to every non-ok run.
        raise
    except BaseException as exc:  # never exit without emitting the JSON status contract
        # BaseException, not Exception: a Ctrl-C raises KeyboardInterrupt, which would
        # otherwise escape and leave the caller with no status line to read at all.
        print(json.dumps({"status": "error", "reason": f"unexpected: {exc!r}"}))
        sys.exit(1)
