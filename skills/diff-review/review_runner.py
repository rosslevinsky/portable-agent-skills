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
— including when the supervisor is itself signaled — while ``--help`` is an ordinary
argparse path. The status is ``ok`` | ``idle_timeout`` | ``deadline`` | ``error``, with a
``reason`` on every non-``ok``, and exit 0 only on a clean review.

**Two flags are opt-in, and that is the whole of their contract.** ``--status-detail`` adds
``terminal_detail`` and ``terminal_detail_source`` to the status line, and
``--max-capture-bytes`` bounds what is retained;
a caller that passes neither sees exactly the bytes it has always seen, key for key. They
are opt-in rather than always-on because adding a field to the status line is a change
every existing caller would have to absorb, and the reason for wanting them — telling a
provider outage apart from a bad answer — belongs to one caller.

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
import stat
import subprocess
import sys
import threading
import time
from pathlib import Path


def _emit(status, **extra):
    """Print the one-line JSON status contract; return the process exit code."""
    payload = {"status": status}
    payload.update(extra)
    # FLUSHED, because stdout is block-buffered whenever it is a pipe — which is how every
    # caller runs this — and any path that ends in `os._exit` skips the buffer entirely. The
    # one line this program contracts to print was lost that way.
    print(json.dumps(payload), flush=True)
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
    # A whole number the model spelled as 2.0 or "2" is a claim, not an absence. Read as an
    # absence it fell past the positive-claim floor below, and a verdict saying two blockers
    # over an empty findings list was published as 0 — what a gate reads as clean.
    if isinstance(claimed, bool):
        claimed = None
    elif isinstance(claimed, float):
        claimed = int(claimed) if claimed.is_integer() else None
    elif isinstance(claimed, str):
        # `int()` decides, never `isdigit()`. "++2" survives an lstrip of the signs, "\u00b2"
        # IS a digit to Python, and a 5000-digit string is refused by the interpreter's own
        # limit — each raised ValueError out of a review that had ALREADY SUCCEEDED, from
        # outside every handler here, and the finished work was reported as a crash.
        try:
            claimed = int(claimed.strip())
        except ValueError:
            claimed = None
    elif not isinstance(claimed, int):
        claimed = None

    # Written back HERE, before any path can return. The published verdict promises an
    # integer and the equal-claim return below touches nothing — so a verdict claiming "1"
    # beside one blocking finding agreed with itself and went out as a string, which a gate
    # comparing numbers cannot read.
    if claimed is not None:
        verdict["blocking_count"] = claimed

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
    deletion, and one behavior on every platform.
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
                # The GROUP id is the child's pid — start_new_session made it the leader —
                # and never `os.getpgid`, which raises once `wait()` below has reaped that
                # leader. It did, on the SIGTERM rung, and the SIGKILL rung then never ran
                # while a descendant still held the inherited pipes.
                os.killpg(proc.pid, signal.SIGKILL if hard else signal.SIGTERM)
            else:
                if proc.poll() is not None:
                    return
                proc.kill() if hard else proc.terminate()
        except (ProcessLookupError, PermissionError, OSError):
            # This rung could not signal; the next one still tries, since a group with no
            # leader can still hold members.
            pass
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
    except (ValueError, RecursionError):
        # RecursionError beside ValueError, as in _scan_verdict: the decoder recurses once
        # per nesting level, and it raised straight past a ValueError-only guard and killed
        # this reader thread, so every later event — the terminal one included — was lost.
        return
    if not isinstance(event, dict) or event.get("type") != "result":
        return
    with lock:
        cap = state.get("capture_cap")
        if cap and len(stripped.encode("utf-8", "replace")) > cap:
            # **This mode's result event is the reply, so the cap has to reach it too.**
            # The event is the whole of what this mode publishes: `--findings` is written
            # from its `result` payload and nothing else is kept. An event that arrived on
            # one complete line is already past the pending-line bound — that one measures
            # what has no end yet — so without this it is held whole, and the reviewer's
            # entire output sits in this process's memory, four hundred attempts at a time,
            # while the flag that asked for a bound reports nothing dropped.
            #
            # Bounded by the same rule as the assistant text: the payload's TAIL is what is
            # kept, because the closing object a caller extracts the answer from is its last
            # non-whitespace content and a head would never hold one.
            bounded, dropped = _bounded_result(event, cap)
            # Set rather than added: only the last result event is retained, so what the
            # notice names is what was cut from THAT one. A running total would describe an
            # event this program is no longer holding.
            state["capture_dropped"] = dropped
            state["capture_truncated"] = True
            state["last_result"] = bounded
        else:
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
        # isinstance, not `or {}`: a truthy non-object — a string, a list — passes that guard
        # and raises AttributeError on the next line, which kills the reader thread.
        item = event.get("item")
        if not isinstance(item, dict):
            return None
        if item.get("type") == "agent_message" and isinstance(item.get("text"), str):
            return item["text"]
        return None
    if event.get("type") == "assistant":  # Claude stream-json
        if event.get("parent_tool_use_id"):  # forwarded sub-agent text, not the main reviewer
            return None
        message = event.get("message")
        if not isinstance(message, dict):
            return None
        parts = [b.get("text") for b in (message.get("content") or [])
                 if isinstance(b, dict) and b.get("type") == "text" and isinstance(b.get("text"), str)]
        return "".join(parts) or None
    return None


# --- the two opt-in bounds ----------------------------------------------------
# How much of the terminal event's error text `terminal_detail` may carry. A status line is
# read by a program, not scrolled by a person, and an unbounded field puts a whole failing
# payload into it.
TERMINAL_DETAIL_MAX_BYTES = 2000

# What the retained tail says about what is no longer in front of it. PREPENDED, never
# appended: a reply's closing object is its last non-whitespace content, and anything after
# it means there is no closing object at all — so a marker on the end would break every
# capped reply that was otherwise intact.
TRUNCATION_NOTICE = ("[review_runner] {dropped} byte(s) of earlier output were dropped to "
                     "stay within --max-capture-bytes; what follows is the retained tail "
                     "and is NOT the whole review")


def _bounded(text, limit):
    """``text`` cut to ``limit`` bytes of UTF-8, with an ellipsis where anything was cut."""
    raw = text.encode("utf-8", "replace")
    if len(raw) <= limit:
        return text
    # "ignore" rather than "replace": the cut lands wherever the byte count runs out, and a
    # half character at the end is a fragment nobody reads. The ellipsis is what says
    # something was dropped.
    return raw[:limit].decode("utf-8", "ignore") + "…"


def _terminal_detail_parts(event, limit=TERMINAL_DETAIL_MAX_BYTES):
    """The terminal event's own error text, and **which rule produced it**.

    ``(text, source)``, where ``source`` is ``"message"``, ``"error"`` or ``"event-type"``
    and both are ``None`` where the event carries nothing at all. One function rather than
    two, because a second walk of the same event would be a second contract to keep in step
    with this one.

    **The third rule's text is a name, not an explanation, and the source is how a caller
    tells them apart.** A failure whose event is ``{"type": "turn.failed"}`` explained
    itself nowhere — its reason went to stderr, if anywhere — and a caller that reads
    ``"turn.failed"`` as the failure's own account of itself can never see that. It then
    classifies an outage as an ordinary bad answer, every time, and whatever it does about
    repeated unexplained failures never fires.
    """
    if not isinstance(event, dict):
        return None, None
    text, source = None, None
    message = event.get("message")
    if isinstance(message, str) and message.strip():
        text, source = message, "message"
    else:
        error = event.get("error")
        if isinstance(error, dict):
            nested = error.get("message")
            if isinstance(nested, str) and nested.strip():
                text, source = nested, "error"
        elif isinstance(error, str) and error.strip():
            text, source = error, "error"
    if text is None:
        kind = event.get("type")
        if isinstance(kind, str) and kind.strip():
            text, source = kind, "event-type"
    if text is None:
        return None, None
    return _bounded(text.strip(), limit), source


def _terminal_detail(event, limit=TERMINAL_DETAIL_MAX_BYTES):
    """The terminal event's own error text, or ``None`` where the event carries none.

    **The extraction contract is stated rather than guessed at**, because a caller
    classifying a failure by this text needs to know what it is reading: the top-level
    ``message``, then a nested error's message, then the event type alone. Nothing reaches
    further — a scan for "the most error-looking string in the event" would hand a caller a
    field that means something different on each runtime, and a classification built on it
    would be wrong in a way nobody could see.

    The third rule is why a failure that produced a terminal event always has *some* detail:
    a caller can then read an ABSENT detail as "this failure explained itself nowhere",
    which is a different thing from a failure it can classify. Which rule answered is
    reported beside the text — see :func:`_terminal_detail_parts`.
    """
    return _terminal_detail_parts(event, limit)[0]


def _reduced_event(event, cap):
    """What is kept of a terminal event larger than the cap: its head, and nothing else.

    The head is what ``terminal_detail`` is extracted from, so the reason the run failed
    survives. Everything else — a ``structured_output`` object among it — does not, which is
    the point of a cap: the verdict then falls back to the scan of the reviewer's own text
    rather than being read out of a payload this program refused to hold.
    """
    text, source = _terminal_detail_parts(event, min(cap, TERMINAL_DETAIL_MAX_BYTES))
    kept = {"type": event.get("type")}
    # **Only real error text becomes a message.** The last extraction rule answers with the
    # event's own type, and writing that into `message` would make what is kept claim the
    # failure explained itself — a claim the original event never made, and one a caller
    # classifying failures then cannot see through.
    if source in ("message", "error"):
        kept["message"] = text
    for key in ("subtype", "is_error"):
        if key in event:
            kept[key] = event[key]
    return kept


def _bounded_result(event, cap):
    """What is kept of a ``result`` event larger than the cap, and how many bytes went.

    ``(event, dropped)``. The fields that decide the outcome survive by
    :func:`_reduced_event` — the type, the success flags, and the error text
    ``terminal_detail`` is read from — and the payload keeps its **tail**, since the answer
    a caller extracts is the last object in it.

    Everything else the runtime put on the event is gone, ``structured_output`` included,
    which is the same trade :func:`_reduced_event` makes and for the same reason: a cap that
    kept a field because it was useful would not be a cap. The verdict then falls back to
    the scan of the text that was kept.
    """
    kept = _reduced_event(event, cap)
    payload = event.get("result")
    dropped = 0
    if isinstance(payload, str):
        raw = payload.encode("utf-8", "replace")
        if len(raw) > cap:
            dropped = len(raw) - cap
            # "ignore", so the kept tail is never LONGER than what was cut from it — the
            # same reason the transcript bound gives.
            payload = raw[dropped:].decode("utf-8", "ignore")
        kept["result"] = payload
    return kept, dropped


def _noticed(text, state):
    """``text`` with the truncation notice in front of it where anything was dropped.

    One function for every publication a cap can shorten, so a reader is never told a review
    is whole by the file that is missing the most. Called with the lock held.
    """
    if text and state.get("capture_truncated"):
        return TRUNCATION_NOTICE.format(dropped=state.get("capture_dropped", 0)) + "\n\n" + text
    return text


def _bound_transcript(state):
    """Keep the retained assistant text inside the cap, dropping from the FRONT.

    From the front is the whole rule. The worker contract puts the answer at the END of a
    transcript, so dropping the tail silently turns a complete reply into one that stops
    mid-sentence and no caller can tell the two apart. Dropping the front loses context and
    keeps the answer, and the notice prepended to what is left says so.

    Called with the lock held.
    """
    cap = state.get("capture_cap")
    if not cap:
        return
    parts = state["transcript"]
    while parts and state.get("transcript_bytes", 0) > cap:
        before = state["transcript_bytes"]
        raw = parts[0].encode("utf-8", "replace")
        over = before - cap
        if len(raw) <= over:
            parts.pop(0)
            state["transcript_bytes"] -= len(raw)
            state["capture_dropped"] = state.get("capture_dropped", 0) + len(raw)
            continue
        # "ignore", so the kept tail is never LONGER than what was cut from it: a partial
        # character at the front is a fragment nobody reads, while rendering it as U+FFFD
        # can take more bytes than the cut removed.
        kept = raw[over:].decode("utf-8", "ignore")
        parts[0] = kept
        state["transcript_bytes"] -= (len(raw) - len(kept.encode("utf-8", "replace")))
        state["capture_dropped"] = state.get("capture_dropped", 0) + over
        # **A pass that removed nothing ends the loop.** This runs on a reader thread, so a
        # loop that cannot make progress is not a slow supervisor — it is one that never
        # reports at all, and the caller then waits out a deadline for an attempt that
        # finished. Being a few bytes over a cap is the smaller of the two by a long way.
        if state["transcript_bytes"] >= before:
            break
    if state.get("capture_dropped"):
        state["capture_truncated"] = True


def _retained_transcript(state):
    """The transcript as it will be published, with the truncation notice where one is due.

    One function for both publications — the successful review's findings and a failed run's
    preserved partial — because a notice on one and not the other is a reader being told the
    review is whole by the file that is missing the most.

    Called with the lock held.
    """
    return _noticed("\n\n".join(state["transcript"]).strip(), state)


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
    except (ValueError, RecursionError):
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
            cap = state.get("capture_cap")
            if cap and len(stripped.encode("utf-8", "replace")) > cap:
                # Over the cap: its head is kept for `terminal_detail` and the transcript is
                # marked truncated, because a caller told nothing about this would read a
                # reply assembled from a stream this program declined to hold whole.
                state["terminal_event"] = _reduced_event(event, cap)
                state["capture_truncated"] = True
            else:
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
    except (ValueError, RecursionError):
        return
    text = _event_text(event)
    if text:
        with lock:
            state["transcript"].append(text)
            if state.get("capture_cap"):
                state["transcript_bytes"] = (state.get("transcript_bytes", 0)
                                             + len(text.encode("utf-8", "replace")))
                _bound_transcript(state)


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
                # Tolerated only because nothing derives a fact from stderr: it reaches the
                # display log and nothing else, the liveness clock is stamped by the stdout
                # reader too, and no outcome is decided from what arrived here.
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


def _same_directory(first, second):
    """True when both names reach the same directory, asked by identity where the OS can say.

    `os.path.samefile` compares device and inode — file id on Windows — so a junction, a
    symlink and a differently cased spelling of one directory all answer true, while two
    directories that merely look alike on a case-sensitive filesystem answer false.

    NOT `_same_path`, which folds case unconditionally. That conservatism is right for output
    paths, where a false match costs a refusal the caller can read; here a false match costs
    the installed reviewer — the search rejects it and the review drops to a same-model rung
    over a program that was there all along.
    """
    try:
        return os.path.samefile(first, second)
    except (OSError, ValueError):
        pass
    return (os.path.normcase(os.path.realpath(first))
            == os.path.normcase(os.path.realpath(second)))


def _which_outside_cwd(program, search_path):
    """Resolve `program` along `search_path`, never accepting a copy in the current directory.

    `shutil.which` cannot answer this: on Windows it searches the current directory first
    whatever path it is handed, so asking it a second time returns the same checkout copy.
    Entries are tried in order with the platform's executable extensions, and an entry that
    IS the current directory is skipped — the tree under review must never supply the
    program sent to read it.
    """
    cwd = os.getcwd()
    exts = [""]
    if os.name == "nt":
        # PATHEXT is ";"-separated on Windows whatever `os.pathsep` reads as here, and it
        # applies to a name that ALREADY carries an extension: `reviewer.v2` resolves to
        # `reviewer.v2.cmd`, exactly as the platform's own lookup does. Only a name already
        # ending in one of these extensions is taken as final.
        pathext = [e for e in os.environ.get("PATHEXT", ".COM;.EXE;.BAT;.CMD").split(";") if e]
        suffix = os.path.splitext(program)[1]
        if not (suffix and any(suffix.lower() == e.lower() for e in pathext)):
            exts = pathext + [""]
    for entry in search_path.split(os.pathsep):
        if not entry:
            continue
        try:
            # IDENTITY, not spelling. `C:\\WORK\\repo` and a junction both name the current
            # directory while comparing unequal as text, and accepting one of those entries
            # runs the checkout's own copy — the single thing this search exists to prevent.
            if _same_directory(entry, cwd):
                continue
        except (OSError, ValueError):
            continue
        for ext in exts:
            candidate = os.path.join(entry, program + ext)
            if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
                return candidate
    return None


def _open_display(path):
    """Open the display log for appending, or return None when the path cannot be one.

    Plain `open()` is wrong here for two reasons that are the same reason: it WAITS. Opening
    a FIFO blocks until a reader arrives, and once one goes away a full pipe blocks every
    write — so a supervisor told to log somewhere unusual stops dead, printing no status
    line and running neither the idle timer nor the deadline. That is precisely what the
    skill promises watching the log cannot do to a review.

    O_NONBLOCK makes both the open and the writes fail instead of waiting, and the handle is
    kept only while `fstat` reports a regular file. O_BINARY keeps Windows from translating
    newlines underneath the text layer, which would translate them again. The caller treats
    every display step as best-effort, so failing here costs the log and nothing else.
    """
    flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND
    flags |= getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_BINARY", 0)
    fd = os.open(path, flags, 0o600)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            os.close(fd)
            return None
        return open(fd, "a", encoding="utf-8", closefd=True)
    except OSError:
        with contextlib.suppress(OSError):
            os.close(fd)
        raise


def _payload_complete(mode, state, lock):
    """Whether the reviewer's own end-of-stream marker is already in hand.

    A descendant holding the inherited stdout keeps the reader from EOF, and Windows has no
    process group to reap it with. Where the terminal event — or, in result-event mode, the
    result itself — has arrived, the stream ended and only the pipe is still open: a review
    that finished, reported as an error because a helper outlived it. External-file mode
    answers False, since its payload is the file and nothing in the stream speaks for it.
    """
    with lock:
        if mode == "stream-transcript":
            return state["terminal"] is not None
        if mode == "stream-json-result-event":
            return state["last_result"] is not None
    return False


class _Refused(Exception):
    """A request this supervisor will not start, carrying the reason as its message."""


def _preflight(args):
    """Check the request, resolve the reviewer program, prepare the output directories.

    Every answer here is reached BEFORE anything is created and before any signal handler is
    armed, and that is what lets a refusal be an exception: there is nothing to clean up, and
    nothing in here has to know how a status line is printed. `run` turns a `_Refused` into
    the one line it prints.

    Returns the reviewer command with `cmd[0]` resolved to an absolute path, and the reason
    the verdict file is unavailable — None when it is available, since a verdict that cannot
    be written is reported rather than fatal.
    """
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
            raise _Refused((
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
                raise _Refused((
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
            raise _Refused((
                f"{flag} names {path}, which already exists. This supervisor writes only "
                f"files it creates and removes only those, so it will not take over a "
                f"path it did not make — whether that is source, someone else's output, "
                f"or a previous review. Delete it yourself if it is stale, or name a "
                f"path that does not exist"
            ))

    for name, val in (("--idle", args.idle), ("--deadline", args.deadline)):
        if not math.isfinite(val) or val <= 0:
            raise _Refused(f"{name} must be a finite positive number")

    # A cap of zero or less bounds everything to nothing, which is a run that can only ever
    # report an overflow. Refused here rather than acted on, so the caller learns it asked
    # for something that cannot succeed instead of reading a review that failed.
    if args.max_capture_bytes is not None and args.max_capture_bytes <= 0:
        raise _Refused("--max-capture-bytes must be a positive number of bytes")

    cmd = args.cmd[1:] if args.cmd and args.cmd[0] == "--" else list(args.cmd)
    if not cmd:
        raise _Refused("no reviewer command given")

    cmd, schema_error = _substitute_schema(cmd, args.schema)
    if schema_error is not None:
        raise _Refused(schema_error)

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
            raise _Refused((
                f"reviewer program {cmd[0]!r} is a relative path and --cwd was given, "
                f"so it names one file to check and another to run. Pass an absolute "
                f"path, or a bare program name to be found on PATH"
            ))
        exe = cmd[0] if os.path.isfile(cmd[0]) else None
    else:
        search_path = os.environ.get("PATH", os.defpath)
        exe = shutil.which(cmd[0], path=search_path)
        if exe is not None and _same_directory(os.path.dirname(os.path.abspath(exe)),
                                               os.getcwd()):
            if os.getcwd() not in search_path.split(os.pathsep):
                # The checkout's own copy, found because Windows searches the current
                # directory before PATH and not because anyone put it there. Search the REST
                # of PATH rather than reporting the program missing: refusing here dropped
                # the review to a same-model rung over a reviewer that was installed all
                # along. The copy in the tree is still never run.
                exe = _which_outside_cwd(cmd[0], search_path)
    if exe is None:
        raise _Refused(f"reviewer CLI not found on PATH: {cmd[0]}")
    # ABSOLUTE, so the child execs the exact file that was just checked. Without this a
    # relative PATH entry leaves `Popen` to redo the resolution under whatever directory it
    # runs in. The check and the exec have to name one file, on both platforms.
    cmd[0] = os.path.abspath(exe)

    # The findings directory is essential; a failure here is fatal.
    try:
        if args.findings:
            Path(args.findings).parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise _Refused(f"setup failed: {exc}")

    # The verdict's directory is NOT essential, so it is made here rather than at the write:
    # the claim below opens the path with O_CREAT, which fails with ENOENT on a missing
    # parent and refused the WHOLE review over an output that is additive by contract. A
    # directory that cannot be made drops the verdict with a reason instead.
    verdict_unavailable = None
    if args.verdict_json:
        try:
            Path(args.verdict_json).parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            verdict_unavailable = f"could not create the verdict directory: {exc}"
    return cmd, verdict_unavailable


class _Interrupts:
    """Cancellation: the handlers, the files this run created, and the one status line.

    Everything about being interrupted is here — what to terminate, what to remove, and the
    rule that EXACTLY ONE JSON status line is printed however the run ends. That rule was
    broken three times while it was spread across `run`: once printing nothing at all, once
    able to print twice, and once marking itself reported before the line was out. It is one
    object so that there is one place to get it right.
    """

    def __init__(self):
        self.proc = None             # the child, once there is one worth terminating
        self.owned = []              # every path THIS run created, and the only ones it removes
        self.payload = None          # the decided status, once its print is imminent
        self.reported = False        # the line is out; a handler must add nothing to it
        self.claiming = False        # mid-claim: record a signal, do not act on it
        self.spawning = False        # mid-spawn: likewise
        self.interrupted = None      # what a window recorded, for its owner to finish
        self.standing_aside = False  # the ONE-SHOT allowance while a status prints
        self._previous = []

    def arm(self):
        """Install the handlers, before anything exists that an interrupt would strand.

        A signal delivered before this is installed breaks both contracts at once: no JSON
        status printed, and — once there is a child — a reviewer left ORPHANED, since
        `start_new_session` has detached its signal fate from ours, so nothing reaps it and
        neither the idle clock nor the deadline governs it any more.
        """
        for sig in (getattr(signal, "SIGTERM", None), getattr(signal, "SIGINT", None)):
            if sig is not None:
                try:
                    self._previous.append((sig, signal.signal(sig, self._on_signal)))
                except (ValueError, OSError):
                    pass  # not the main thread, or the platform disallows it — best effort

    def restore(self):
        """Put back whatever was installed before `arm`."""
        for sig, previous in self._previous:
            try:
                signal.signal(sig, previous)
            except (ValueError, OSError):
                pass

    def claimed(self, path):
        """Record a path this run created, which is what makes removing it legitimate."""
        self.owned.append(Path(path))

    def release(self):
        """Remove what this run created, and only that.

        Every path recorded here was verified ABSENT at startup, so whatever stands there now
        was made by this run: there is nothing to decide. It is also what keeps
        refuse-if-it-exists from being a trap, since residue from a failed run would otherwise
        refuse the next attempt.
        """
        for created in self.owned:
            with contextlib.suppress(OSError):
                created.unlink()

    def report(self, status, **extra):
        """Print the one status line with the handler standing aside, then disarm.

        EVERY exit after `arm` goes through here. Storing the payload first is what tells
        `_on_signal` the print is imminent, so it steps aside rather than exiting silently;
        marking the run reported before the line was actually out told the handler the
        opposite, and a signal in that window ended a finished review in silence. `finally`,
        so a print that RAISES — a closed stdout — still leaves the caller's handlers as it
        found them.
        """
        self.payload = dict(status=status, **extra)
        try:
            return _emit(status, **extra)
        finally:
            self.reported = True
            self.restore()

    def deferred(self, child):
        """Finish what `_on_signal` recorded, now that the record it needed is complete."""
        name = (signal.Signals(self.interrupted).name if hasattr(signal, "Signals")
                else self.interrupted)
        if child is not None:
            _terminate(child)
            _reap_group(child)
        self.release()
        return self.report("error", reason=f"supervisor interrupted by {name}",
                           exit_code=child.poll() if child is not None else None)

    def _on_signal(self, signum, _frame):
        if self.reported:
            os._exit(1)  # status already emitted; never append a second one
        if self.payload is not None:
            # The run has decided its status and the print is the next thing that happens.
            # Let it: exiting here ends a COMPLETED review with no status line at all, and
            # printing the payload from here races that print and emits a second one.
            #
            # ONCE. Standing aside costs the microseconds a print takes — unless the print
            # cannot finish, as it cannot into a stdout nobody is draining, and then this
            # would make the supervisor unkillable. A caller that signals twice means it.
            if not self.standing_aside:
                self.standing_aside = True
                return
            os._exit(1)
        if self.claiming or self.spawning:
            # Mid-claim, or mid-spawn: in both, this handler would act on a record that is one
            # statement out of date — a file created but not yet recorded, a child that exists
            # but is not yet held here. Record the signal and return; the check after each of
            # those windows acts on it with the record complete.
            self.interrupted = signum
            return
        self.reported = True
        child = self.proc
        if child is not None:
            _terminate(child)
            # ...and then whatever the group still holds. `_terminate` returns immediately
            # when the leader is already gone, so on the signal path that early return was
            # the whole cleanup and a descendant still holding the inherited pipes survived
            # the supervisor by design. This is the function written for that case; the
            # normal exit path already calls it and the signal path did not.
            _reap_group(child)
        name = signal.Signals(signum).name if hasattr(signal, "Signals") else signum
        # The same cleanup a failed review does, because an interrupted review IS a failed one
        # and leaves the same residue. `os.unlink` is a single syscall and safe here: this
        # handler is already committed to `os._exit`, so a failure to remove leaves the
        # operator a file they can delete.
        for path in self.owned:
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


class _Stream:
    """The reviewer's output while it runs: two reader threads, the record they fill in, and
    the display log they tee to.

    ONE object because these are one mechanism, not a sequence of steps. The heartbeat the
    idle clock reads is stamped by the stdout reader on every chunk; the record the outcome is
    decided from is filled in by that same thread; and the display handle may be closed only
    once BOTH readers have finished with it. Handing those seven things between separate
    functions would describe them as independent, which is exactly what they are not.
    """

    def __init__(self, proc, args):
        self.proc = proc
        self.mode = args.result_mode
        # `capture_cap` lives in the shared record rather than being threaded through
        # `_consume_jsonl`: the capture functions are the only things that need it, they
        # already take this dict, and a caller that never passes the flag leaves it None and
        # reaches not one line of the bounding code. Every new key is read with `.get`, so a
        # record built without them — as a caller testing one step builds one — still works.
        self.cap = args.max_capture_bytes or None
        self.state = {"last_activity": time.monotonic(), "last_result": None,
                      "transcript": [], "terminal": None, "terminal_event": None,
                      "capture_cap": self.cap, "transcript_bytes": 0,
                      "capture_dropped": 0, "capture_truncated": False,
                      "capture_overflow": None, "display_bytes": 0,
                      "read_error": None}
        self.lock = threading.Lock()
        self.done = threading.Event()
        self.err_done = threading.Event()
        self.started = None
        # Constructed only AFTER a successful launch, so a failed launch leaves no unmatched
        # "start" marker in the log, and every display step here is best-effort.
        self.display_fh = None
        if args.display:
            try:
                Path(args.display).parent.mkdir(parents=True, exist_ok=True)
                self.display_fh = _open_display(args.display)  # append: never truncate a shared log
            except OSError:
                self.display_fh = None  # display is best-effort; never abort a review for it

    def write_display(self, text, bounded=True):
        """Tee ``text`` to the display log, inside the capture cap where one is set.

        ``bounded=False`` is this program's OWN markers — the start line and the end line
        carrying the status. The end marker is the display log's only completion signal and
        callers are told to treat it as one, so a cap that swallowed it would turn a
        finished review into a log that reads as a hang. What a cap is for is the reviewer's
        output, which is the part nobody can bound in advance.

        Two threads reach this — the stdout reader and the stderr drainer — so the running
        total is taken under the lock. Without it the cap is a race and the two threads
        overshoot it by whatever they happened to be holding.
        """
        if self.display_fh is None or not text:
            return
        if bounded and self.cap is not None:
            size = len(text.encode("utf-8", "replace"))
            with self.lock:
                written = self.state.get("display_bytes", 0)
                if written >= self.cap:
                    return
                self.state["display_bytes"] = written + size
                room = self.cap - written
            if size > room:
                text = (text.encode("utf-8", "replace")[:room].decode("utf-8", "ignore")
                        + f"\n[review_runner] display log capped at {self.cap} bytes; the "
                          f"rest of the reviewer's output is not logged\n")
        try:
            self.display_fh.write(text)
            self.display_fh.flush()
        except OSError:
            pass

    def start(self):
        """Start both readers, and the two clocks `watch` and the status line measure against.

        BOTH clocks start here rather than at construction. Opening the display log is I/O of
        unknown duration — a network path, a large append — and counting it as silence from
        the child would spend the first idle window before the child could speak into it, so a
        slow log alone could end a healthy review with `idle_timeout`.
        """
        self.write_display("[review_runner] start\n", bounded=False)
        self.state["last_activity"] = time.monotonic()
        threading.Thread(target=self._read_stdout, daemon=True).start()
        threading.Thread(target=_drain_stderr,
                         args=(self.proc.stderr, self.write_display, self.state, self.lock,
                               self.err_done), daemon=True).start()
        self.started = time.monotonic()

    def elapsed(self):
        """Seconds since the readers started, which is what the status line reports."""
        return 0.0 if self.started is None else time.monotonic() - self.started

    def _read_stdout(self):
        fd = self.proc.stdout.fileno()
        buf = b""
        # One decoder ACROSS chunks, because a read boundary falls wherever the bytes
        # happened to arrive — mid-character as readily as anywhere else. Decoding each
        # chunk on its own turned one em dash into three replacement characters in the log a
        # human reads. Reader-local: this thread is its only user, so it needs no lock.
        # Only the DISPLAY path needs this — the JSONL path accumulates raw bytes and
        # decodes whole lines, so a split inside a line never reaches it.
        display_decoder = _display_decoder()
        # A line this program could not hold whole is the one failure a cap must not hide.
        # Once it is set, reading CONTINUES — a reader that walked away would leave the child
        # blocked on a full pipe with nobody to notice — but nothing more is accumulated and
        # nothing more is parsed, because a transcript assembled out of the rest would be
        # shorter than the reviewer's own output with nothing to say so.
        overflowed = False
        try:
            while True:
                try:
                    data = os.read(fd, 65536)
                except OSError as exc:
                    # A FAILED READ IS NOT END OF STREAM. Breaking out silently sets `done`
                    # below on the way past, so the run is reported as drained and whatever
                    # arrived before the fault is published as the reviewer's whole answer —
                    # a review cut short by the machine, reported as one that finished.
                    # Recorded here and refused by `watch` and `_route_outcome`, which is
                    # where the two states can still be told apart.
                    with self.lock:
                        self.state["read_error"] = (
                            f"reading the reviewer's output failed: {exc}")
                    break
                if not data:
                    break
                with self.lock:
                    self.state["last_activity"] = time.monotonic()  # heartbeat per CHUNK, not per line
                self.write_display(display_decoder.decode(data))
                if overflowed or self.mode not in ("stream-json-result-event",
                                                   "stream-transcript"):
                    continue
                buf += data
                while b"\n" in buf:
                    raw, buf = buf.split(b"\n", 1)
                    _consume_jsonl(raw.decode("utf-8", "replace"), self.mode,
                                   self.state, self.lock)
                # Measured on what is still PENDING, which is what "cannot be reframed"
                # means: a line that arrived whole has been reframed already, and the
                # transcript and terminal-event bounds are what hold it from there. A line
                # that keeps growing with no end to it is the one this cannot do, and
                # dropping part of it would leave a JSON document that parses as something
                # its author did not write.
                if self.cap is not None and len(buf) > self.cap:
                    overflowed = True
                    buf = b""
                    with self.lock:
                        self.state["capture_overflow"] = (
                            f"one output line exceeded --max-capture-bytes ({self.cap}) "
                            f"before it ended, so it cannot be reframed")
            if (not overflowed and buf.strip()
                    and self.mode in ("stream-json-result-event", "stream-transcript")):
                _consume_jsonl(buf.decode("utf-8", "replace"), self.mode, self.state, self.lock)
        finally:
            # Flush whatever the decoder is still holding, on EVERY exit path. A child
            # killed mid-character leaves an incomplete sequence, and the decoder holds it
            # forever unless told the stream ended — so without this the tail disappears
            # from the log with nothing to say anything was lost.
            tail = display_decoder.decode(b"", final=True)
            if tail:
                self.write_display(tail)
            self.done.set()

    def watch(self, idle, deadline):
        """Poll until the reviewer exits or one of the two clocks runs out.

        Silence is measured from the last chunk that arrived, the deadline from the start, and
        neither is derived from the other: a reviewer that talks forever resets the first and
        never touches the second.
        """
        status, reason = "ok", None
        while self.proc.poll() is None:
            now = time.monotonic()
            with self.lock:
                silent = now - self.state["last_activity"]
                overflow = self.state.get("capture_overflow")
                read_failed = self.state.get("read_error")
            if read_failed and not _payload_complete(self.mode, self.state, self.lock):
                # The reader is gone, so nothing will stamp the heartbeat again and the idle
                # clock below would charge the child for silence that is this program's.
                # Ended here under the fault's own name: a storage or pipe failure is never
                # the reviewer's answer. Excused once the reviewer's own end-of-stream marker
                # is in hand, because then the stream is over and only the pipe is still open.
                status, reason = "error", read_failed
                _terminate(self.proc)
                break
            if overflow:
                # Ended here rather than left to finish: the rest of this run's output
                # cannot be parsed, so every second of it is spent against a result that
                # will be refused anyway. `_route_outcome` asks the same question for a
                # child that had already exited by the time the reader noticed.
                status, reason = "error", f"capture overflow: {overflow}"
                _terminate(self.proc)
                break
            if now - self.started >= deadline:
                status, reason = "deadline", f"no completion within {deadline:.0f}s"
                _terminate(self.proc)
                break
            if silent >= idle:
                status, reason = "idle_timeout", f"no output for {idle:.0f}s"
                _terminate(self.proc)
                break
            time.sleep(0.5)
        return status, reason

    def settle(self, status):
        """Wait for both readers, close what is safe to close, mark the log.

        Returns whether stdout drained, and the reviewer's exit code.
        """
        # Two waits rather than one 30s wait. A reader that has not hit EOF a few seconds after
        # the child exited is not slow — it is blocked on a descendant still holding the pipe —
        # so reap the group and give it the rest of the budget. Same 30s ceiling, and a single
        # wait spends all of it before reaping, which costs a completed review its tail.
        drained = self.done.wait(timeout=5)
        if not drained:
            _reap_group(self.proc)
            drained = self.done.wait(timeout=25)
        exit_code = self.proc.poll()
        err_drained = self.err_done.wait(timeout=5)
        if drained and not err_drained:
            # stdout reached EOF, so the reviewer itself is gone, and something still holds
            # stderr: the survivor _reap_group exists for, reached through the other pipe. The
            # stdout path above already does this; left alone here it kept running under an ok
            # status, still spending, and the display log is closed under its live writer.
            _reap_group(self.proc)
            err_drained = self.err_done.wait(timeout=25)
        if drained:
            # Only close the shared pipe once the reader has finished — no close-under-reader.
            try:
                self.proc.stdout.close()
            except OSError:
                pass
        if err_drained:
            try:
                self.proc.stderr.close()
            except OSError:
                pass
        if self.display_fh:
            # ALWAYS write the end marker, drained or not. It is the display log's only
            # completion signal and callers are told to treat it as one, so making it conditional
            # conflates three different states. `drained` is reported rather than implied.
            self.write_display(
                f"[review_runner] end status={status} exit={exit_code} drained={drained}\n",
                bounded=False,
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
                    self.display_fh.close()
                except OSError:
                    pass
        return drained, exit_code


PARTIAL_SUFFIX = ".partial"
PARTIAL_ALTERNATES = 8


def _preserve_partial(findings_path, transcript, reason):
    """Keep a failed run's reviewer text beside ``--findings``; return its path or None.

    NOT the findings path. That path means "a review completed" — it is written only on
    success and reported only on success, so a caller may test the file rather than the
    status line and still be right. A truncated review written there is worse than none: a
    reviewer that reached three of twelve files and said "nothing wrong in what I read"
    reads as a clean review of all twelve. The text is still worth keeping, because a
    cross-model review that dies on a usage limit is expensive to lose and the display log
    is not on the correctness path.

    It is claimed with ``O_EXCL`` and **never** ``O_TRUNC``, and it is NOT recorded in
    ``owned`` — recording it would have ``release()`` delete the one file this whole function
    exists to leave behind, and the cleanup on failure is exactly what it opts out of.

    Claiming exclusively means it cannot write to a path that already holds something, so a
    second failed run in a directory holding the first one's remains takes the next free name
    — ``.partial.1``, ``.partial.2`` — rather than either overwriting or giving up. Both runs
    keep their text, which is the property this function is for. Writing in place would not:
    ``O_TRUNC`` on an existing ``.partial`` destroys the earlier failure's transcript, and on
    a hard link it destroys the file at the other end. ``O_EXCL`` also means a FIFO left at
    the path returns ``EEXIST`` at once instead of blocking the open until a reader arrives —
    a hang with nothing left to time it out, since this runs after the deadline loop has ended.

    The ``lstat`` refusal stays in front of the claim even though ``O_EXCL`` would reject a
    symlink on its own. It is what makes a link at the path report "not preserved" rather
    than quietly writing the text one name along: a link somebody else placed is a reason to
    stop, not a name collision to step around.

    Never fatal, never able to change the outcome. A run that failed and could not keep its
    text has still failed, and for the same reason.
    """
    base = str(findings_path) + PARTIAL_SUFFIX
    banner = (f"[review_runner] INCOMPLETE REVIEW — the run failed: {reason}\n"
              f"[review_runner] This is what the reviewer had produced when it stopped. It "
              f"is NOT a completed review and must not be read as one.\n\n")
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        # `lstat`, not `Path.is_symlink()`, which answers False for everything it cannot
        # stat: a path whose kind is unknown would be written through the very link this
        # refuses to follow, on the platforms with no O_NOFOLLOW to catch it. ENOENT is the
        # only "not a link" this accepts; any other error leaves the text unpreserved, which
        # is the half of a best-effort that costs nothing but the text.
        try:
            mode = os.lstat(base).st_mode
        except FileNotFoundError:
            mode = 0
        if stat.S_ISLNK(mode):
            return None
        # Bounded, because a directory holding this many dead partials is a symptom and
        # walking it forever would be a second one. Giving up loses only the text.
        for candidate in (base, *(f"{base}.{n}" for n in range(1, PARTIAL_ALTERNATES + 1))):
            try:
                fd = os.open(candidate, flags, 0o600)
            except FileExistsError:
                continue     # a file, a FIFO, a symlink or a directory -- none of them ours
            try:
                os.write(fd, (banner + transcript + "\n").encode("utf-8", errors="replace"))
            finally:
                os.close(fd)
            return candidate
    except (OSError, ValueError):
        return None
    return None


def _read_verdict_file(path, size, cap):
    """The verdict file external-file mode produced, read inside the cap where one is set.

    **The file is the reviewer's own and is not this program's to shorten**, so nothing here
    rewrites it: it stays on disk exactly as the reviewer left it, and a caller reading it
    reads all of it. What a cap bounds is what this process HOLDS — a findings file of any
    size otherwise arrives whole in memory, four hundred supervisors at a time, which is the
    thing ``--max-capture-bytes`` exists to stop.

    The TAIL is what is read, because the only use of this text is the scan for the verdict
    object, and a closing object is a document's last content. And the run is NOT reported
    truncated for it: `capture_truncated` tells a caller that what was published is shorter
    than what the reviewer produced, and here the published answer is the untouched file.
    """
    if cap and size > cap:
        with open(path, "rb") as fh:
            fh.seek(size - cap)
            return fh.read().decode("utf-8", "replace")
    return path.read_text(encoding="utf-8", errors="replace")


def _route_outcome(args, status, reason, drained, exit_code, state, lock):
    """Decide the outcome from what actually arrived, and write it to ``--findings``.

    The three result modes disagree about what counts as the review — a final result event,
    the whole transcript, or a file the reviewer wrote itself — and this is the only step that
    knows the difference. Returns the status, its reason, and the text that landed in
    ``--findings``, which is what a verdict is then extracted from.

    A status that arrived here already failed (an idle timeout, a deadline) is carried through
    untouched: this step only ever decides the outcome of a run that got as far as finishing.
    """
    findings_text = None  # what actually landed in --findings, for verdict extraction
    partial = None        # a failed run's reviewer text, kept under its own name
    # Every write below is REVIEWER-DERIVED text, and every one takes `errors="replace"` for
    # the same reason the reads on this path do. JSON permits an unpaired `\ud800` escape and
    # Python's decoder produces the lone surrogate faithfully, so it reaches here intact. A
    # plain `write_text` then raises `UnicodeEncodeError` — a `ValueError`, which the
    # `except OSError` below did not catch — and a COMPLETED cross-model review was reported
    # as an error, so the caller fell open to a same-model reviewer: the one trade the skill
    # says must never be made. U+FFFD in one word does not compare to that.
    try:
        if status == "ok":
            with lock:
                overflow = state.get("capture_overflow")
                read_failed = state.get("read_error")
            if read_failed and not _payload_complete(args.result_mode, state, lock):
                # Asked before `drained`, which a failed read satisfies too — the reader sets
                # its done flag on every exit path — so an unreported read fault arrives here
                # looking exactly like a stream that ended.
                status, reason = "error", read_failed
            elif overflow:
                # Asked here as well as in the watch loop: a child that had already exited
                # when the reader hit the oversized line never reaches that loop again, and
                # the run would otherwise be reported ok over a stream this program stopped
                # parsing part-way.
                status, reason = "error", f"capture overflow: {overflow}"
            elif not drained and not _payload_complete(args.result_mode, state, lock):
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
                    # The notice goes in front of what is published, never on the end: this
                    # payload IS the reply, and anything after its closing object means the
                    # reply has no closing object at all.
                    with lock:
                        payload = _noticed(payload, state)
                    Path(args.findings).write_text(
                        payload, encoding="utf-8", errors="replace")
                    findings_text = payload
            elif args.result_mode == "stream-transcript":
                with lock:
                    transcript = _retained_transcript(state)
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
                try:
                    size = fp.stat().st_size
                except FileNotFoundError:
                    # The only error that means the reviewer wrote nothing. `exists()`
                    # answers False for a path it cannot stat as well, and charging a
                    # storage fault to the reviewer as "no verdict" is how a caller counting
                    # bad answers spends an allowance on a failure that was never its own.
                    # Anything else falls to the routing failure below, named.
                    size = 0
                if size == 0:
                    status, reason = "error", "reviewer wrote no verdict"
                else:
                    findings_text = _read_verdict_file(fp, size,
                                                       args.max_capture_bytes or None)
    except (OSError, ValueError) as exc:
        # ValueError alongside OSError: `UnicodeEncodeError` and `UnicodeDecodeError` are
        # ValueErrors. The `errors="replace"` above should mean nothing here can raise one,
        # but a decoding surprise landing in the module-level `except BaseException` is what
        # turned a completed review into `status: error` once already.
        status, reason = "error", f"routing failed: {exc}"
    # Whatever went wrong, the reviewer's own words are the expensive part. Kept for every
    # transcript-mode failure, not just a failed terminal event: an idle timeout and a
    # deadline arrive here already failed and carry the same half-written review.
    if status != "ok" and args.result_mode == "stream-transcript" and args.findings:
        with lock:
            transcript = _retained_transcript(state)
        if transcript:
            partial = _preserve_partial(args.findings, transcript, reason)
    return status, reason, findings_text, partial


def _publish_verdict(args, status, verdict_unavailable, findings_text, state, lock):
    """Extract the structured verdict and write it BESIDE the findings, never instead.

    Strictly additive and NEVER fatal. Enforcement exists only on the rung that has a CLI
    schema flag; the in-harness sub-agent rung has none, so a review that produced a good
    narrative but no parseable object is still a successful review, and the miss is reported
    rather than failing it. Returns the path written — None when nothing was — and the reason
    there is no verdict, which the caller reports as ``verdict_reason``.
    """
    verdict_path, verdict_reason = None, verdict_unavailable
    if args.verdict_json and status == "ok" and verdict_unavailable is None:
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
            # RecursionError beside the rest, for the same reason: `json.dumps` recurses
            # once per nesting level when it indents, and the decoder accepts objects deeper
            # than the encoder writes. This write is REPORTED, never fatal, and that has to
            # hold for every way it can fail rather than for the ones already seen.
            except (OSError, ValueError, RecursionError) as exc:
                verdict_reason = f"could not write the verdict file: {exc}"
    return verdict_path, verdict_reason


def run(args):
    try:
        cmd, verdict_unavailable = _preflight(args)
    except _Refused as refusal:
        # Nothing has been created and no handler is armed yet, so a refusal is only ever
        # this one line.
        return _emit("error", reason=str(refusal))

    popen_kw = dict(stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    stdin=subprocess.DEVNULL,  # a reviewer that probes stdin gets EOF, never hangs
                    bufsize=0, cwd=args.cwd or None)
    if os.name == "posix":
        popen_kw["start_new_session"] = True  # own process group, for a clean group kill
    # Armed BEFORE anything is claimed, which is earlier than the child needs and exactly
    # where the outputs need it: the claim below creates files, and a signal landing between
    # creating one and recording it left it on disk to refuse the retry.
    guard = _Interrupts()
    guard.arm()

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
    # RECORDED across the claim rather than blocked: creating the file and recording it in
    # `owned` is one action written as two statements, and a handler firing in between exits
    # without removing a file it cannot see, leaving residue that refuses the retry. A signal
    # mask would close that window on POSIX and do nothing whatever on Windows, where the
    # handler runs just the same — and a mask that ever spanned the spawn would be inherited
    # by the reviewer.
    guard.claiming = True
    for path, flag in ((args.findings, "--findings"),
                       (None if verdict_unavailable else args.verdict_json, "--verdict-json")):
        if not path:
            continue
        try:
            os.close(os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600))
        except FileExistsError:
            guard.release()
            # Cleaned up BEFORE reporting: a signal recorded during this refusal finds
            # nothing left to remove either way.
            guard.claiming = False
            return guard.report("error", reason=(
                f"{flag} names {path}, which already exists. This supervisor writes only "
                f"files it creates and removes only those, so it will not take over a "
                f"path it did not make — whether that is source, someone else's output, "
                f"or a previous review. Delete it yourself if it is stale, or name a "
                f"path that does not exist"
            ))
        except OSError as exc:
            guard.release()
            guard.claiming = False
            return guard.report("error", reason=f"{flag} ({path}) could not be created: {exc}")
        guard.claimed(path)
    guard.claiming = False
    if guard.interrupted is not None:
        return guard.deferred(None)

    # The spawn window is closed by RECORDING a signal, never by blocking one. Blocking here
    # looked right and was not: the child inherits the mask across fork and exec, so the
    # reviewer would start unable to handle the termination this supervisor later sends it,
    # and every cancellation would wait out the grace period and land as SIGKILL. Instead
    # the handler notes the signal while the child is invisible and returns, and the few
    # lines after the spawn do what it would have done.
    guard.spawning = True
    try:
        proc = subprocess.Popen(cmd, **popen_kw)
    except (FileNotFoundError, OSError, ValueError) as exc:
        # ValueError as well: a NUL byte anywhere in the argv raises it rather than OSError,
        # and escaping here skipped the release of the claimed files and reported a crash
        # instead of a launch that failed.
        guard.spawning = False
        # Release the claimed outputs, exactly as a refused CLAIM already does. Taking
        # ownership and then failing to launch left two empty files behind — and the next
        # attempt refuses a path it did not create, so a retry failed for a reason that had
        # nothing to do with the retry. A bad `--cwd` is enough to reach this. Only files
        # THIS call created are removed, which is the same rule the refusal states.
        guard.release()
        return guard.report("error", reason=f"launch failed: {exc}")
    guard.proc = proc
    guard.spawning = False
    if guard.interrupted is not None:
        # A signal arrived while the child was invisible to the handler, which recorded it
        # and returned. There is something to terminate now.
        return guard.deferred(proc)


    stream = _Stream(proc, args)
    stream.start()
    status, reason = stream.watch(args.idle, args.deadline)
    drained, exit_code = stream.settle(status)

    status, reason, findings_text, partial_findings = _route_outcome(
        args, status, reason, drained, exit_code, stream.state, stream.lock)

    verdict_path, verdict_reason = _publish_verdict(
        args, status, verdict_unavailable, findings_text, stream.state, stream.lock)

    # The claim created this path empty to hold it. If no verdict was written into it, remove
    # it — "no verdict" has always meant "no verdict file", and a caller that tests for the
    # file would otherwise read an empty one as a verdict that exists. Only where this run
    # CLAIMED it, though: a verdict whose directory could not be prepared is never claimed, and
    # deleting that path anyway removes a file this program did not create, which is the one
    # thing it promises never to do. Another writer can reach it while the review runs.
    if args.verdict_json and verdict_path is None and Path(args.verdict_json) in guard.owned:
        with contextlib.suppress(OSError):
            Path(args.verdict_json).unlink()

    extra = {}
    if args.verdict_json:
        extra = {"verdict": verdict_path, "verdict_reason": verdict_reason}
    # Reported only when one was written, so a run that kept nothing has the status line it
    # has always had. `findings` stays null on a failure: the two are different claims, and
    # a caller keying on `findings` must never see this path.
    if partial_findings:
        extra["partial_findings"] = partial_findings

    # **The two opt-in keys appear only for a caller that asked for them**, which is what
    # makes those flags additive. `partial_findings` is not one of them: it appears on any
    # failed transcript-mode run that kept text, flags or no flags, and a caller reading the
    # status line as a fixed set of keys should expect it there.
    #
    # `terminal_detail` is null where the failure explained itself NOWHERE — no terminal
    # event at all, which is what a reviewer writing its error to stderr leaves behind. That
    # is a distinct answer from a detail this program chose not to read, and a caller
    # classifying failures depends on being able to tell them apart.
    if args.status_detail:
        with stream.lock:
            event = stream.state.get("terminal_event") or stream.state.get("last_result")
        # Both keys, because the text alone cannot be classified. An event that carried no
        # error text at all is reported by NAME, and a caller reading that name as the
        # failure's account of itself cannot tell an outage from a bad answer — the source
        # says which of the three rules answered, so "turn.failed" is readable as the
        # failure explaining nothing rather than as an explanation.
        detail, source = _terminal_detail_parts(
            event, min(TERMINAL_DETAIL_MAX_BYTES, args.max_capture_bytes)
            if args.max_capture_bytes else TERMINAL_DETAIL_MAX_BYTES)
        extra["terminal_detail"] = detail
        extra["terminal_detail_source"] = source
    if args.max_capture_bytes:
        # Whether anything was dropped, said in the status line rather than left for the
        # caller to find by reading the transcript for a marker. A caller deciding what a
        # reply's absence means needs the answer as data.
        with stream.lock:
            extra["capture_truncated"] = bool(stream.state.get("capture_truncated"))

    # A review that did not succeed leaves the workspace as it found it. `owned` is the only
    # deletion this program performs, and every path in it was verified ABSENT at startup —
    # so whatever is there now was created by this run. There is nothing to decide.
    #
    # It is also what keeps the refuse-if-it-exists rule from being a trap: without it a
    # failed review would leave files behind that block the next attempt.
    if status != "ok":
        guard.release()

    # Disarm before reporting. The child is already dead or drained, so the handlers have
    # nothing left to protect — and a signal arriving between here and process exit would
    # otherwise fire one and append a SECOND status line to a run that has already reported.
    # The `reported` flag alone would not close that window.
    # Stored BEFORE the disarm, so the window between deciding and printing is covered by
    # the handler rather than by luck: a signal there ended a finished review with an
    # interruption error and no status of its own.
    payload = dict(status=status, reason=reason, exit_code=exit_code,
                   elapsed_s=round(stream.elapsed(), 1),
                   findings=args.findings if status == "ok" else None, **extra)
    return guard.report(status, **{key: value for key, value in payload.items()
                              if key != "status"})


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
    ap.add_argument("--status-detail", action="store_true",
                    help="add terminal_detail to the status line: the terminal event's own "
                         "error text — its top-level message, then a nested error's "
                         "message, then the event type alone — bounded, and null where the "
                         "failure explained itself nowhere. terminal_detail_source is added "
                         "beside it, naming which of those three answered, so an event name "
                         "is not read as an explanation. Opt-in: without it the status line "
                         "is exactly what it has always been")
    ap.add_argument("--max-capture-bytes", type=int, default=None,
                    help="cap each retained representation separately: the pending raw "
                         "line, the decoded reviewer text, the retained terminal event and "
                         "the display log. Reviewer text over the cap is dropped from the "
                         "FRONT with a notice prepended, and a line too large to reframe "
                         "ends the run rather than shortening the transcript in silence. "
                         "Opt-in: without it nothing is bounded and nothing is counted")
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
