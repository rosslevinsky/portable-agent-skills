#!/usr/bin/env python3
"""plan-duel engine — stdlib-only, cross-platform duel state machine.

This module owns the deterministic heart of the plan-duel skill. It shells out (via
argv-list ``subprocess``) to whatever CLIs the ``SKILL.md`` adapter note injects for the
three LLM judgment points — generate Plan A, generate/critique Plan B, and judge. ``v1``
in the comments below names the prompt-driven implementation this replaced.

Design rules:
  * Standard library ONLY — no third-party imports, runtime or test. Python 3.10+.
  * NO branded/vendor CLI names are hardcoded here. The concrete CLI executables
    arrive as argv DATA from the adapter config, which this module only parses.
  * Prompt templates use an explicit ``⟪name⟫`` placeholder marker, never
    ``str.format`` — so literal braces like ``{approach}`` in the prompt text never
    collide — and rendering FAILS LOUD on any unresolved marker.
  * The adapter config is a STRUCTURED per-role spec (JSON), never scraped prose.

Known limitation — native Windows batch shims. If ``shutil.which`` resolves a participant
CLI to a ``.cmd``/``.bat`` (common for npm-installed CLIs, and this module resolves bare
names through ``which`` so ``PATHEXT`` is honoured), Windows runs it through the shell,
which reinterprets ``%VAR%`` / ``&`` in arguments outside Python's quoting — and the
arguments here are whole generated prompts. Prefer a non-shim executable, or run the duel
under WSL/Git-Bash on Windows.

Function groups, each spawning no subprocess so the ``unittest`` suite can exercise them
directly:

    guard      — require_python
    render     — render_template (+ TemplateError)
    config     — parse_adapter_config / RoleSpec (+ AdapterConfigError)
    decisions  — parse_score, convergence/stagnation/max-rounds exit checks
    naming     — plan_snapshot_name, slugify_name, parse_preferred, resolve_winner
    io         — read_text_normalized (strict, engine-owned) / read_text_tolerant
                 (CLI-written) / write_text_utf8 / copy_bytes (encoding-pinned)
    progress   — append_progress (optional, non-blocking, append-only log)
    artifacts  — artifact classification + auditable cleanup (higher-round / full-reset)
    state      — RunState / RoundState markers persisted to state.json
    resume     — scan_snapshots / compute_resume / apply_resume
    freeze     — freeze_round_inputs (immutable per-round agent inputs)
    exec       — resolve_executable / run_cli (argv-list subprocess, never a shell)
    capture    — run_agent / capture_judge_message / recover_agent_b_round0
    summary    — extract_judge_fields / stamp_winner_plan / rewrite_differences /
                 assemble_summary (winner-only v2 stamp, scoped A/B→name rewrite)
    run loop   — DuelContext / run_init_round / run_critique_round / run_duel /
                 write_summary / execute (the end-to-end state machine)

``execute`` is the whole engine in one call, and ``main`` is its argv front end: it
reserves or resumes a workdir, runs round 0 and the critique loop, and writes
``summary.md``.
"""

from __future__ import annotations

import argparse
import contextlib
import fnmatch
import json
import math
import errno
import os
import re
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Collection, Mapping, Sequence


# --------------------------------------------------------------------------- #
# Errors
# --------------------------------------------------------------------------- #
class PlanDuelError(Exception):
    """Base class for every error the plan-duel engine raises deliberately."""


class TemplateError(PlanDuelError):
    """Raised when a prompt template has unresolved ``⟪name⟫`` placeholders."""


class AdapterConfigError(PlanDuelError):
    """Raised when the adapter-config JSON is malformed or violates the schema."""


class ProcessError(PlanDuelError):
    """Base class for subprocess-execution failures (never silently accepted)."""


class CliNotFoundError(ProcessError):
    """Raised when an injected CLI executable cannot be resolved on ``PATH``."""


class CliExecutionError(ProcessError):
    """Raised when a spawned CLI exits non-zero."""


class CliTimeoutError(ProcessError):
    """Raised when a spawned CLI exceeds its timeout."""


class AgentOutputError(PlanDuelError):
    """Raised when an agent (Plan A/B) output is missing/short or its CLI failed.

    ``str()`` is the exact v1 halt line (e.g. ``Agent A plan generation failed at round
    0.``) so the user-visible message stays identical to the golden. Whenever a low-level
    ``cause`` is known — a non-zero exit, a timeout, or the tail of the agent's own output —
    it is appended after an em-dash and kept on the ``.cause`` attribute.
    """

    def __init__(self, halt_message: str, *, cause: str | None = None):
        self.halt_message = halt_message
        self.cause = cause
        super().__init__(halt_message if cause is None else f"{halt_message} — {cause}")


class JudgeOutputError(PlanDuelError):
    """Raised when the judge process failed or produced no clean final message."""


# --------------------------------------------------------------------------- #
# guard — interpreter version
# --------------------------------------------------------------------------- #
_PYVER_OVERRIDE_ENV = "PLAN_DUEL_PYTHON_VERSION_OVERRIDE"


def require_python(
    min_major: int = 3,
    min_minor: int = 10,
    *,
    current: tuple[int, ...] | None = None,
) -> None:
    """Fail loud (clear message + non-zero exit) on a too-old interpreter.

    Without this guard, an older interpreter would raise an opaque traceback deep in a later
    stdlib call. ``current`` (major, minor) overrides the detected version for tests, and the
    ``PLAN_DUEL_PYTHON_VERSION_OVERRIDE`` env var is honored when ``current`` is not passed,
    so the guard's message can be demonstrated without an old Python installed.
    """
    if current is None:
        override = os.environ.get(_PYVER_OVERRIDE_ENV)
        if override:
            try:
                current = tuple(int(part) for part in override.split(".")[:2])
            except ValueError:
                current = sys.version_info[:2]
        else:
            current = sys.version_info[:2]

    if tuple(current[:2]) < (min_major, min_minor):
        found = ".".join(str(part) for part in current[:2])
        sys.stderr.write(
            f"Python {min_major}.{min_minor}+ required (found {found}). "
            f"Install a newer interpreter and re-run.\n"
        )
        raise SystemExit(1)


# --------------------------------------------------------------------------- #
# render — prompt templates
# --------------------------------------------------------------------------- #
# Placeholders use the ⟪name⟫ marker (U+27EA / U+27EB angle brackets) rather than
# str.format so that literal braces in prompt bodies are never touched.
PLACEHOLDER_OPEN = "⟪"
PLACEHOLDER_CLOSE = "⟫"
_PLACEHOLDER_RE = re.compile(r"⟪([^⟪⟫]+)⟫")

# Canonical inventory of placeholder names the engine substitutes into the prompt
# and format templates (init.md / round.md / summary.md).
# Defined here so template authoring has a single reference; render_template does
# not require a value's name to appear here (it only requires every marker in a
# given template to be provided), but the inventory documents the vocabulary.
PLACEHOLDERS = frozenset(
    {
        "workdir",
        "round",  # the current round number N
        "round_context",  # the resolved "round context" sentence
        "controller_name",
        "participant_name",
        "controller_slug",
        "participant_slug",
        "prompt",  # a fully-rendered prompt passed into an argv/file/stdin slot
        "frozen_a",  # path to the immutable plan-a-round-(N-1) snapshot (critique reads)
        "frozen_b",  # path to the immutable plan-b-round-(N-1) snapshot (critique reads)
        "schema_path",  # filesystem path to the shipped judge schema
        "schema_json",  # the SAME schema as compact inline JSON text
    }
)

# The structured-output contract for the judge. ONE schema file ships beside this engine;
# the two placeholders above are the two argv FORMS of that one file, because the runtimes
# disagree on how a schema is passed — one takes a FILE PATH, the other INLINE JSON. One
# source for both forms keeps the PROMPT byte-identical across runtimes, with the difference
# confined to the adapter argv where it belongs.
JUDGE_SCHEMA_NAME = "judge-schema.json"
SCHEMA_PLACEHOLDERS = ("schema_path", "schema_json")


def find_placeholders(text: str) -> set[str]:
    """Return the set of ``⟪name⟫`` marker names present in ``text``."""
    return set(_PLACEHOLDER_RE.findall(text))


def render_template(template: str, values: Mapping[str, object]) -> str:
    """Substitute every ``⟪name⟫`` marker in ``template`` with ``str(values[name])``.

    Fails loud: if the template contains any marker whose name is absent from
    ``values``, raises :class:`TemplateError` naming EVERY unresolved placeholder,
    rather than emitting a half-substituted prompt. Substitution is single-pass —
    a substituted value that itself contains ``⟪…⟫`` text is left as a literal and
    never re-scanned.
    """
    required = find_placeholders(template)
    unresolved = sorted(name for name in required if name not in values)
    if unresolved:
        rendered_markers = ", ".join(
            f"{PLACEHOLDER_OPEN}{name}{PLACEHOLDER_CLOSE}" for name in unresolved
        )
        raise TemplateError(f"unresolved placeholder(s) in template: {rendered_markers}")

    return _PLACEHOLDER_RE.sub(lambda m: str(values[m.group(1)]), template)


def schema_placeholder_values(
    skill_dir: str | os.PathLike[str] | None, *, name: str = JUDGE_SCHEMA_NAME
) -> dict[str, str]:
    """Both argv forms of the shipped schema file, or ``{}`` when unavailable.

    Returns ``schema_path`` (an absolute path, for a CLI whose flag takes a file) and
    ``schema_json`` (the same document as compact single-line JSON, for a CLI whose flag
    takes it inline). The file is decoded and re-serialized rather than passed through
    verbatim, so a malformed schema fails HERE — before a run is paid for.

    ``{}`` when there is no ``skill_dir``, no such file, or invalid JSON; an adapter that
    uses the markers then gets a named pre-flight failure from :func:`preflight_schema`.
    """
    if not skill_dir:
        return {}
    path = Path(skill_dir) / name
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return {
        "schema_path": str(path.resolve()),
        "schema_json": json.dumps(document, separators=(",", ":")),
    }


def preflight_schema(specs: Mapping[str, RoleSpec], values: Mapping[str, object]) -> None:
    """Halt before any billable work if an adapter needs a schema that is missing.

    Without this, a missing or malformed ``judge-schema.json`` would surface as a bare
    ``unresolved placeholder(s) in template: ⟪schema_path⟫`` at the judge dispatch —
    that is, AFTER both plans have been generated and paid for. Mirrors
    :func:`preflight_executables`: name the problem up front, cost the user nothing.
    """
    used: set[str] = set()
    for role in REQUIRED_ROLES:
        spec = specs.get(role)
        if spec is None:
            continue
        for part in spec.command:
            used |= find_placeholders(part) & set(SCHEMA_PLACEHOLDERS)
    missing = sorted(name for name in used if name not in values)
    if missing:
        rendered = ", ".join(
            f"{PLACEHOLDER_OPEN}{name}{PLACEHOLDER_CLOSE}" for name in missing
        )
        raise PlanDuelError(
            f"adapter command needs {rendered} but the schema companion "
            f"'{JUDGE_SCHEMA_NAME}' is missing, unreadable, or not valid JSON "
            f"(it must ship beside plan_duel.py; pass --skill-dir to locate it)"
        )


# --------------------------------------------------------------------------- #
# config — adapter (per-role) specifications
# --------------------------------------------------------------------------- #
REQUIRED_ROLES = ("agent_a", "agent_b", "judge")
STDOUT_MODES = frozenset({"file", "clean-last-message"})
# Only "arg" is IMPLEMENTED. The rendered prompt reaches every adapter through an argv
# placeholder, and the subprocess runs with stdin=DEVNULL by deliberate decision: a CLI that
# reads stdin when its prompt is already in argv otherwise blocks forever, with no output to
# diagnose it by.
#
# "file" and "stdin" were accepted here, stored on the spec, and read by nothing, so a
# third-party adapter declaring "stdin" ran its CLI with no prompt. Refusing a mode that does
# nothing is honest; implement one at the dispatch site before re-listing it.
PROMPT_MODES = frozenset({"arg"})
DECLARED_BUT_UNIMPLEMENTED_PROMPT_MODES = frozenset({"file", "stdin"})
CWD_ANCHORS = frozenset({"workdir"})

_REQUIRED_ROLE_KEYS = ("command", "stdout")
_KNOWN_ROLE_KEYS = frozenset(
    {"command", "stdout", "prompt_mode", "cwd", "placeholders"}
)


@dataclass(frozen=True)
class RoleSpec:
    """A parsed adapter spec for one role (``agent_a`` / ``agent_b`` / ``judge``).

    Attributes:
        command: argv template (each element may contain ``⟪name⟫`` markers).
        stdout: how the CLI's result is captured — ``"file"`` (the CLI writes the
            artifact directly) or ``"clean-last-message"`` (capture only the CLI's
            final message, never a raw transcript).
        prompt_mode: how the rendered prompt reaches the CLI. ``"arg"`` — an argv
            placeholder — is the only implemented mode, and the default; ``"file"``
            and ``"stdin"`` are refused rather than silently ignored.
        cwd: working-directory anchor for the subprocess — ``None`` (inherit the
            engine's cwd) or ``"workdir"`` (run the CLI with the duel workdir as its
            cwd, e.g. a ``-C``-style participant).
        placeholders: the declared placeholder inventory for this role. When
            non-empty, every marker used in ``command`` must appear here.
    """

    command: tuple[str, ...]
    stdout: str
    prompt_mode: str = "arg"
    cwd: str | None = None
    placeholders: tuple[str, ...] = field(default_factory=tuple)


def _parse_role_spec(role: str, raw: object) -> RoleSpec:
    """Validate and construct one :class:`RoleSpec` (see schema in RoleSpec)."""
    if not isinstance(raw, dict):
        raise AdapterConfigError(f"role '{role}' must be a JSON object")

    unknown = sorted(set(raw) - _KNOWN_ROLE_KEYS)
    if unknown:
        raise AdapterConfigError(
            f"role '{role}' has unknown key(s): {', '.join(unknown)} "
            f"(allowed: {', '.join(sorted(_KNOWN_ROLE_KEYS))})"
        )

    for key in _REQUIRED_ROLE_KEYS:
        if key not in raw:
            raise AdapterConfigError(f"role '{role}' is missing required key '{key}'")

    command = raw["command"]
    if not isinstance(command, list) or not command:
        raise AdapterConfigError(
            f"role '{role}' 'command' must be a non-empty list of strings"
        )
    if not all(isinstance(part, str) for part in command):
        raise AdapterConfigError(
            f"role '{role}' 'command' must contain only strings"
        )
    command = tuple(command)

    stdout = raw["stdout"]
    if not isinstance(stdout, str) or stdout not in STDOUT_MODES:
        raise AdapterConfigError(
            f"role '{role}' has unknown stdout mode {stdout!r} "
            f"(expected one of: {', '.join(sorted(STDOUT_MODES))})"
        )

    prompt_mode = raw.get("prompt_mode", "arg")
    # `isinstance` before any membership test: a list or dict here is unhashable, and
    # `x in frozenset` answers that with TypeError rather than the clean configuration
    # error the caller can act on. An existing test pins exactly that.
    if not isinstance(prompt_mode, str) or prompt_mode not in PROMPT_MODES:
        if isinstance(prompt_mode, str) and prompt_mode in DECLARED_BUT_UNIMPLEMENTED_PROMPT_MODES:
            raise AdapterConfigError(
                f"role '{role}' asks for prompt_mode {prompt_mode!r}, which this engine "
                f"honours nowhere: the prompt reaches a CLI only through an argv "
                f"placeholder. It used to be accepted and then ignored, so the CLI ran "
                f"with no prompt at all. Put the prompt placeholder in 'command' and "
                f"use prompt_mode 'arg'."
            )
        raise AdapterConfigError(
            f"role '{role}' has unknown prompt_mode {prompt_mode!r} "
            f"(expected one of: {', '.join(sorted(PROMPT_MODES))})"
        )

    cwd = raw.get("cwd")
    if cwd is not None and (not isinstance(cwd, str) or cwd not in CWD_ANCHORS):
        raise AdapterConfigError(
            f"role '{role}' has unknown cwd anchor {cwd!r} "
            f"(expected one of: {', '.join(sorted(CWD_ANCHORS))}, or omit)"
        )

    placeholders_raw = raw.get("placeholders", [])
    if not isinstance(placeholders_raw, list) or not all(
        isinstance(part, str) for part in placeholders_raw
    ):
        raise AdapterConfigError(
            f"role '{role}' 'placeholders' must be a list of strings"
        )
    placeholders = tuple(placeholders_raw)

    if placeholders:
        declared = set(placeholders)
        used: set[str] = set()
        for part in command:
            used.update(find_placeholders(part))
        undeclared = sorted(used - declared)
        if undeclared:
            rendered = ", ".join(
                f"{PLACEHOLDER_OPEN}{name}{PLACEHOLDER_CLOSE}" for name in undeclared
            )
            raise AdapterConfigError(
                f"role '{role}' command uses undeclared placeholder(s): {rendered}"
            )

    return RoleSpec(
        command=command,
        stdout=stdout,
        prompt_mode=prompt_mode,
        cwd=cwd,
        placeholders=placeholders,
    )


def parse_adapter_config(data: str | dict) -> dict[str, RoleSpec]:
    """Parse the structured adapter config into a ``{role: RoleSpec}`` mapping.

    ``data`` is either a JSON string (as embedded in the ``SKILL.md`` adapter note) or an
    already-decoded ``dict``. The top level must be an object holding exactly the three
    roles in :data:`REQUIRED_ROLES`. Never scrapes commands out of markdown prose — the
    input is always a structured block.
    """
    if isinstance(data, str):
        try:
            obj = json.loads(data)
        except json.JSONDecodeError as exc:
            raise AdapterConfigError(f"adapter config is not valid JSON: {exc}") from exc
    else:
        obj = data

    if not isinstance(obj, dict):
        raise AdapterConfigError("adapter config top level must be a JSON object")

    unknown_roles = sorted(set(obj) - set(REQUIRED_ROLES))
    if unknown_roles:
        raise AdapterConfigError(
            f"adapter config has unknown role(s): {', '.join(unknown_roles)} "
            f"(allowed: {', '.join(REQUIRED_ROLES)})"
        )

    missing_roles = [role for role in REQUIRED_ROLES if role not in obj]
    if missing_roles:
        raise AdapterConfigError(
            f"adapter config is missing role(s): {', '.join(missing_roles)}"
        )

    return {role: _parse_role_spec(role, obj[role]) for role in REQUIRED_ROLES}


# --------------------------------------------------------------------------- #
# decisions — score parsing + exit conditions
# --------------------------------------------------------------------------- #
# The judge's verdict is a JSON object whose shape is ENFORCED by the runtime's
# structured-output flag. Every judge read below tries JSON first and falls back to the
# pre-schema line-marker contract (``SCORE:`` / ``DIFFERENCES:`` / ``MISSED REJECTIONS:`` /
# ``PREFERRED:``), so a resume over a pre-schema workdir — and a runtime whose CLI has no
# schema flag — keeps working unchanged.
_SCORE_LINE_RE = re.compile(r"^\s*SCORE:\s*(.*)$", re.MULTILINE)
# Signed. Without the `-?` a judge answering `SCORE: -10` yielded 10, which clears
# convergence_exit's `>= 8` and ended the duel at round 3 on the WORST score the rubric can
# express. It also made the two score paths disagree: a JSON `-10` was correctly rejected as
# out-of-range while the string `"-10"` came back as 10 and converged. A negative now lands
# in _usable_score's out-of-range path — treated as 0, warned about, duel continues.
_FIRST_INT_RE = re.compile(r"-?\d+")

# A decoded object is treated as the verdict only if it carries at least
# MIN_JUDGE_JSON_KEYS of these. Two, not one: a marker-contract judge file whose
# justification quotes a JSON payload would otherwise be adopted as the verdict, dropping
# the differences the markers actually carry. Two is also the smallest bar that still admits
# a genuinely degraded verdict such as ``{"score": 3, "preferred": "B"}`` — the judge file is
# the duel's product, so this parse degrades rather than rejects. (``review_runner`` requires
# ALL of its keys because its verdict is optional: a missed one there costs nothing.)
JUDGE_JSON_KEYS = frozenset(
    {"score", "differences", "missed_rejections", "preferred", "justification"}
)
MIN_JUDGE_JSON_KEYS = 2

CONVERGENCE_LABEL = "Convergence"
STAGNATION_LABEL = "Stagnation"
MAX_ROUNDS_LABEL = "Maximum rounds"
MAX_ROUNDS = 10

# The rubric's score range (round.md Part 1, mirrored by judge-schema.json's
# minimum/maximum). Enforced here too because the schema only binds the runtimes whose
# CLI has a schema flag — a resumed pre-schema workdir and any flagless runtime reach
# the exit checks through this parser instead.
SCORE_MIN = 0
SCORE_MAX = 10


def _is_judge_verdict(obj: object) -> bool:
    """True if ``obj`` looks like the judge's verdict rather than incidental JSON."""
    return (
        isinstance(obj, dict)
        and len(JUDGE_JSON_KEYS & set(obj)) >= MIN_JUDGE_JSON_KEYS
    )


def parse_judge_json(text: str) -> dict | None:
    """Return the judge's verdict object from ``text``, or ``None`` if there is none.

    With a schema flag both runtimes emit the bare object, so the common case is a single
    ``json.loads``. The scan below exists for runtimes with no such flag: their judge still
    answers in JSON but may wrap it in a fence or a sentence. The LAST qualifying object
    wins — in a transcript the final one is the answer. ``None`` for anything unparseable,
    so the caller degrades to the legacy marker parser rather than crashing.
    """
    stripped = text.strip()
    if not stripped:
        return None
    try:
        whole = json.loads(stripped)
    except ValueError:
        pass
    else:
        if _is_judge_verdict(whole):
            return whole

    decoder = json.JSONDecoder()
    found: dict | None = None
    for index, char in enumerate(text):
        if char != "{":
            continue
        try:
            candidate, _ = decoder.raw_decode(text, index)
        except ValueError:
            continue
        if _is_judge_verdict(candidate):
            found = candidate
    return found


def _json_score(obj: Mapping[str, object]) -> int | None:
    """The verdict object's ``score`` as an int, or ``None`` if unusable.

    ``bool`` is rejected explicitly: it is an ``int`` subclass in Python, so a
    ``"score": true`` would otherwise score the round 1.
    """
    value = obj.get("score")
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        number = _FIRST_INT_RE.search(value)
        if number is not None:
            return int(number.group(0))
    return None


def _marker_score(text: str) -> int | None:
    """The first integer on the first ``SCORE:`` line (the pre-schema contract)."""
    line = _SCORE_LINE_RE.search(text)
    if line is None:
        return None
    number = _FIRST_INT_RE.search(line.group(1))
    return int(number.group(0)) if number is not None else None


def _usable_score(value: int | None) -> int | None:
    """``value`` if it lies inside the rubric's range, else ``None``.

    An out-of-range number is NOT a score, and the difference matters: ``convergence_exit``
    fires on ``score >= 8``, so a judge answering ``50`` would end the duel at round 3 on a
    value the rubric cannot produce. Clamping to 10 would converge just as wrongly and do it
    silently, so an out-of-range value takes the same path as an unparseable one — treated as
    0, and warned about.

    The schema constrains this on both shipped adapters; this covers the paths a schema
    cannot reach: a resumed pre-schema workdir, and any runtime whose CLI has no schema flag.
    """
    if value is None or not (SCORE_MIN <= value <= SCORE_MAX):
        return None
    return value


def raw_score(text: str) -> int | None:
    """The integer the judge actually wrote, range-UNCHECKED — diagnostics only.

    Never used for a decision; :func:`score_warning` uses it so an out-of-range value
    can be named in the warning instead of being reported as unparseable, which would
    send a user to look for a missing line in a file that plainly shows a number.
    """
    obj = parse_judge_json(text)
    if obj is not None:
        value = _json_score(obj)
        if value is not None:
            return value
    return _marker_score(text)


def parse_score(text: str) -> int | None:
    """Return the judge's usable score: the JSON verdict's, else the ``SCORE:`` line.

    ``None`` means neither form carried an integer inside the rubric's range. Callers treat
    ``None`` as 0 and emit :func:`score_warning`. A JSON verdict whose ``score`` is missing
    or unusable still falls through to the marker parser, so the degrade path is never
    narrower than it was.
    """
    obj = parse_judge_json(text)
    if obj is not None:
        score = _usable_score(_json_score(obj))
        if score is not None:
            return score
    return _usable_score(_marker_score(text))


def score_warning(text: str, round_n: int) -> str:
    """The exact user-visible warning for a round whose score cannot be used.

    Two distinct causes, two distinct messages: nothing parseable at all, versus a
    number the judge did write that the rubric does not allow. Both are treated as 0.
    """
    written = raw_score(text)
    if written is not None:
        return (
            f"Warning: score {written} at round {round_n} is outside the "
            f"{SCORE_MIN}–{SCORE_MAX} rubric — treating as 0"
        )
    return f"Warning: could not parse score at round {round_n} — treating as 0"


@dataclass(frozen=True)
class ExitDecision:
    """The outcome of the per-round exit check.

    ``stopped_due_to`` is one of ``Convergence`` / ``Stagnation`` /
    ``Maximum rounds`` when ``stop`` is True (else ``None``), and ``message`` is
    the exact v1 user-visible line to print (else ``None``).
    """

    stop: bool
    stopped_due_to: str | None = None
    message: str | None = None


def _score_at(scores: Sequence[int], round_n: int) -> int:
    """Score for round ``round_n`` (rounds are 1-indexed; ``scores`` is 0-indexed)."""
    return scores[round_n - 1]


def convergence_exit(round_n: int, score_n: int) -> ExitDecision | None:
    """v1 convergence: round_n >= 3 AND score(N) >= 8.

    The N >= 3 gate avoids trusting a high score before the plans have had a
    chance to cross-pollinate.
    """
    if round_n >= 3 and score_n >= 8:
        return ExitDecision(
            True,
            CONVERGENCE_LABEL,
            f"Convergence reached at round {round_n} (score: {score_n}/10).",
        )
    return None


def stagnation_exit(round_n: int, scores: Sequence[int]) -> ExitDecision | None:
    """v1 stagnation: for round_n >= 4, the best of rounds N, N-1, N-2 has not
    exceeded the best of rounds 1..N-3.

    Window-based so a single dip does not trigger exit — only a sustained failure
    to beat the previous peak does.
    """
    if round_n < 4:
        return None
    recent_best = max(scores[round_n - 3 : round_n])  # rounds N-2, N-1, N
    prior_best = max(scores[0 : round_n - 3])  # rounds 1 .. N-3
    if recent_best <= prior_best:
        return ExitDecision(
            True,
            STAGNATION_LABEL,
            f"Stagnation detected — best score in last 3 rounds "
            f"({recent_best}/10) has not exceeded prior peak "
            f"({prior_best}/10). Stopping early.",
        )
    return None


def max_rounds_exit(round_n: int, score_n: int) -> ExitDecision | None:
    """v1 max-rounds: round_n == :data:`MAX_ROUNDS` (10)."""
    if round_n >= MAX_ROUNDS:
        return ExitDecision(
            True,
            MAX_ROUNDS_LABEL,
            f"Maximum rounds reached (score: {score_n}/10).",
        )
    return None


def evaluate_exit(round_n: int, scores: Sequence[int]) -> ExitDecision:
    """Compose the exit checks in v1 order: converge, then stagnate, then max.

    ``scores`` holds the parsed integer scores for rounds 1..round_n (index 0 is
    round 1). Returns the first firing :class:`ExitDecision`, or a non-stopping
    decision when no condition is met.
    """
    score_n = _score_at(scores, round_n)
    for decision in (
        convergence_exit(round_n, score_n),
        stagnation_exit(round_n, scores),
        max_rounds_exit(round_n, score_n),
    ):
        if decision is not None:
            return decision
    return ExitDecision(False)


# --------------------------------------------------------------------------- #
# naming — snapshots, slugs, winner resolution
# --------------------------------------------------------------------------- #
# The pre-schema marker form, read as LENIENTLY as the JSON form beside it — and no more
# leniently, which is the harder half: a marker this fails to read takes the warn-and-default
# path, and the default is A, so misreading publishes the losing plan.
#
# Tolerated: case anywhere, ``**`` emphasis, a ``Plan `` prefix, a trailing full stop,
# surrounding horizontal whitespace, and ``\r``.
_PREFERRED_LINE_RE = re.compile(
    r"^[ \t]*(?:\*\*)?[ \t]*PREFERRED[ \t]*:[ \t]*(?:\*\*)?[ \t]*(?P<value>.*?)[ \t\r]*$",
    re.IGNORECASE | re.MULTILINE,
)

# Given the label's VALUE, is a side named? Three accepting shapes, tried in order:
#
#   ALONE  the letter is the whole value (bar ``**`` and a full stop). Case-insensitive,
#          so `preferred: b` resolves exactly as the JSON path's `"preferred": "b"` does.
#   MARK   the letter is followed by punctuation — `B — tighter`, `B, it stages`,
#          `B (rollback)`. Punctuation separates a token from prose, so this is safe in
#          either case.
#   PROSE  the letter is followed by whitespace and an explanation. UPPER CASE ONLY, and
#          that restriction is the discriminator: `A`/`B` are the schema's own tokens,
#          while the indefinite article is lowercase.
#
# **The membership test for `_CONNECTORS` is one question: can this word follow the
# indefinite article "a" in English?** If it can, it is a noun phrase and the letter is an
# article, not a side — `a compromise`, `a merge`, `a hybrid`. If it cannot, the letter is a
# side and what follows is its justification. Extend the list only by that test, never by
# adding whatever a judge happened to write.
#
# Given up deliberately: `PREFERRED: A simpler` is unreadable rather than resolved. An
# unreadable marker is reported to a human who fixes it; a misread one publishes the loser.
_CONNECTORS = r"because|since|as|is|was|wins|won|remains|scores"
_SIDE_ALONE_RE = re.compile(
    r"^(?:PLAN[ \t]+)?([AB])[ \t]*(?:\*\*)?[ \t]*\.?[ \t]*(?:\*\*)?$", re.IGNORECASE
)
_SIDE_THEN_MARK_RE = re.compile(
    r"^(?:PLAN[ \t]+)?([AB])[ \t]*[^\w\s].*$", re.IGNORECASE
)
# NOT IGNORECASE: `A`/`B` are the schema's own tokens and the indefinite article is
# lowercase, so case is the first discriminator and the connector is the second.
_SIDE_THEN_PROSE_RE = re.compile(
    rf"^(?:Plan[ \t]+)?([AB])[ \t]+(?:{_CONNECTORS})\b.*$"
)


@dataclass(frozen=True)
class PreferredReading:
    """What the ``PREFERRED:`` marker contract yielded.

    ``side`` is ``'A'`` / ``'B'`` / ``None``. ``unreadable`` carries the label line when one
    was present but named no side — a DIFFERENT state from no label at all, reported
    differently, because only one of the two is fixable by editing that line.
    """

    side: str | None
    unreadable: str | None


def _side_from_value(value: str) -> str | None:
    """``'A'`` / ``'B'`` if the label's value names a side, else ``None``."""
    for pattern in (_SIDE_ALONE_RE, _SIDE_THEN_MARK_RE, _SIDE_THEN_PROSE_RE):
        match = pattern.match(value)
        if match:
            return match.group(1).upper()
    return None


def read_preferred_marker(text: str) -> PreferredReading:
    """Read the pre-schema ``PREFERRED:`` contract, distinguishing all three outcomes.

    The FIRST label whose value names a side wins, so a prose sentence opening
    ``Preferred:`` earlier in the verdict cannot suppress the real one. If none names a
    side, the last such label is returned as ``unreadable`` so the caller can quote it.
    """
    unreadable = None
    for match in _PREFERRED_LINE_RE.finditer(text):
        side = _side_from_value(match.group("value").strip())
        if side is not None:
            return PreferredReading(side, None)
        unreadable = match.group(0).strip()
    return PreferredReading(None, unreadable)


def plan_snapshot_name(side: str, round_n: int) -> str:
    """Round-snapshot basename, e.g. ``plan-a-round-3.md`` (``side`` is ``a``/``b``)."""
    return f"plan-{side}-round-{round_n}.md"


def slugify_name(name: str) -> str:
    """Lowercase a runtime name into its file slug (v1: ``controller_slug`` etc.).

    Case-folding only, which is why :func:`require_distinct_slugs` exists: two names
    that differ only in case produce ONE slug, and the duel then has one filename for
    two plans.
    """
    return name.lower()


# Characters that cannot appear in a slug, because ``plan-{slug}.md`` is a FILENAME. ``/``
# and ``\`` both, on every platform: Windows accepts either as a separator, and a workdir
# authored on one host is read on another. ``<>:"|?*`` are the names Windows refuses — ``:``
# in particular selects an NTFS alternate data stream.
_UNSAFE_SLUG_CHARS = frozenset('/\\<>:"|?*') | frozenset(chr(c) for c in range(32))


def require_safe_slug(role: str, name: str) -> None:
    """Refuse a runtime name whose slug is not ONE ordinary filename component.

    :func:`slugify_name` lowercases and stops, and the collision guard below reads its
    result as a filename. A runtime named ``x/../../victim`` gives ``plan-x/../../victim.md``
    — not ``a``, not ``b``, not a round snapshot — so it walks past every collision check and
    lands OUTSIDE the workdir. A separator alone is enough; no ``..`` is needed.

    Stated as prohibitions rather than an allowed alphabet, so a name in any script still
    works — ``клод`` and ``モデル`` are fine. Checked at STARTUP, before a workdir exists.

    No Windows device-name check (``CON``, ``NUL``, …): those are reserved as a whole stem,
    and the stem here is always ``plan-<slug>``.
    """
    slug = slugify_name(name)
    if not slug:
        raise PlanDuelError(
            f"{role} name is empty, so its final plan would be written to plan-.md. "
            f"Give the runtime a name."
        )
    bad = sorted(_UNSAFE_SLUG_CHARS & set(slug))
    if bad:
        listed = ", ".join(repr(character) for character in bad)
        raise PlanDuelError(
            f"{role} {name!r} gives the file slug {slug!r}, which contains {listed}. "
            f"The slug becomes the filename plan-{slug}.md, so a separator or a "
            f"reserved character writes the final plan somewhere this engine will not "
            f"look for it — or outside the workdir entirely. Give the runtime a name "
            f"that is a single ordinary filename component."
        )
    if slug in (".", "..") or slug != slug.strip() or slug.endswith("."):
        raise PlanDuelError(
            f"{role} {name!r} gives the file slug {slug!r}, which is not a usable "
            f"filename component: it is a directory reference, or it begins or ends "
            f"with a space or a dot (Windows silently trims both, so plan-{slug}.md "
            f"would not be the file that was written). Give the runtime an ordinary "
            f"name."
        )


# A slug matching this makes ``plan-{slug}.md`` a ROUND SNAPSHOT — the frozen input a
# resume treats as authority for what that round actually contained. Destructive from
# either side, because neither final copy reads a snapshot before writing it.
_SNAPSHOT_SLUG_RE = re.compile(r"^[ab]-round-\d+$")

# The live plan each role's final copy must not land on. :func:`write_summary` does
# ``plan-a.md -> plan-{controller_slug}.md`` then ``plan-b.md -> plan-{participant_slug}.md``,
# so the harm is role-specific and the guard has to be too:
#
#   * controller slug ``b`` clobbers ``plan-b.md`` BEFORE the second copy reads it, so the
#     participant's plan is lost outright and both final files hold plan A;
#   * participant slug ``a`` overwrites the live plan A with plan B's content;
#   * controller ``a`` and participant ``b`` are SELF-copies and destroy nothing, which is
#     why a role-aligned ``A``/``B`` duel is allowed.
_FORBIDDEN_LIVE_SLUG = {"controller": "b", "participant": "a"}


def require_distinct_slugs(controller_name: str, participant_name: str) -> None:
    """Refuse a duel whose runtime names would collide over a ``plan-{slug}.md`` file.

    Two collisions, one consequence — the final plans are written as ``plan-{slug}.md``:

    * **With each other.** One slug for both runtimes means the second write lands on the
      first while ``summary.md`` still reports a winner and a loser. Nothing downstream can
      detect it: the surviving file is a valid plan.
    * **With the engine's own files.** A PARTICIPANT named ``A`` overwrites the live
      ``plan-a.md``; a CONTROLLER named ``B`` clobbers ``plan-b.md`` before the second copy
      reads it, so both final files hold plan A; ``a-round-3`` overwrites a snapshot a later
      resume then scores as work it never saw. Role-ALIGNED names are allowed, since each
      copies its own file onto itself.

    Checked at STARTUP, before a model call is dispatched, because that is the only moment
    the answer is free.
    """
    # SHAPE FIRST. Everything below reads a slug as a filename component, and a slug
    # that is not one slips past all of it — a separator makes `plan-{slug}.md` a path,
    # which is neither `a`, `b`, nor a round snapshot however it is spelled.
    require_safe_slug("controller", controller_name)
    require_safe_slug("participant", participant_name)
    if slugify_name(controller_name) == slugify_name(participant_name):
        raise PlanDuelError(
            f"controller {controller_name!r} and participant {participant_name!r} give "
            f"the same file slug {slugify_name(controller_name)!r}, so both final plans "
            f"would be written to plan-{slugify_name(controller_name)}.md and one would "
            f"overwrite the other. Give the two runtimes names that differ by more than "
            f"case."
        )
    for role, name in (("controller", controller_name),
                       ("participant", participant_name)):
        slug = slugify_name(name)
        if _SNAPSHOT_SLUG_RE.match(slug):
            raise PlanDuelError(
                f"{role} {name!r} gives the file slug {slug!r}, so its final plan would "
                f"be written to plan-{slug}.md — a round snapshot, which is the frozen "
                f"input a resume reads for that round. The write would destroy it. Give "
                f"that runtime a name that is not '<a|b>-round-<n>'."
            )
        if slug == _FORBIDDEN_LIVE_SLUG[role]:
            other = "participant" if role == "controller" else "controller"
            raise PlanDuelError(
                f"{role} {name!r} gives the file slug {slug!r}, so its final plan would "
                f"be written to plan-{slug}.md — the {other}'s live plan, which the "
                f"write would destroy. Naming the {role} "
                f"{_FORBIDDEN_LIVE_SLUG[other]!r} instead is fine (it copies that file "
                f"onto itself); it is only this way round that overwrites."
            )


def parse_preferred(text: str) -> str | None:
    """Return ``'A'`` / ``'B'`` from the JSON verdict, else the ``PREFERRED:`` line.

    ``None`` when neither form names a side; the caller warns and defaults to A. A JSON
    verdict carrying an unusable ``preferred`` still falls through to the marker parser.
    Both branches answer in UPPER CASE. Callers that need to tell "the label named no side"
    apart from "there was no label" use :func:`read_preferred_marker` directly.
    """
    obj = parse_judge_json(text)
    if obj is not None:
        value = obj.get("preferred")
        if isinstance(value, str) and value.strip().upper() in ("A", "B"):
            return value.strip().upper()
    return read_preferred_marker(text).side


def resolve_winner(
    preferred: str, controller_name: str, participant_name: str
) -> tuple[str, str]:
    """Resolve the judge's ``PREFERRED: A|B`` to ``(winner_name, winner_file)``.

    Per v1: A -> the controller runtime, B -> the participant runtime. The winner
    file uses the winner's lowercased slug: ``plan-{slug}.md``.
    """
    if preferred == "A":
        return controller_name, f"plan-{slugify_name(controller_name)}.md"
    if preferred == "B":
        return participant_name, f"plan-{slugify_name(participant_name)}.md"
    raise ValueError(f"invalid PREFERRED value: {preferred!r} (expected 'A' or 'B')")


# --------------------------------------------------------------------------- #
# io — encoding-pinned reads/writes and byte-exact copies
# --------------------------------------------------------------------------- #
# All text I/O is pinned to UTF-8 with EXPLICIT newline handling rather than
# relying on locale/pathlib defaults (the classic Windows bite). Text we *parse*
# is normalized to ``\n`` on read; snapshot/freeze copies are byte-exact so a
# plan authored with CRLF is preserved verbatim.
def _normalize_newlines(text: str) -> str:
    """Collapse CRLF/CR to ``\\n`` (the one newline contract both readers share)."""
    return text.replace("\r\n", "\n").replace("\r", "\n")


def read_text_normalized(path: str | os.PathLike[str]) -> str:
    """Read ``path`` as STRICT UTF-8 and normalize CRLF/CR line endings to ``\\n``.

    For engine-owned inputs only — the adapter config, the prompt templates, the user's
    problem file and anything this engine wrote. An undecodable byte in one of those is a
    real authoring or corruption error: silently substituting U+FFFD into a role's argv
    would run a duel against a config nobody wrote. Files a third-party CLI wrote go through
    :func:`read_text_tolerant`.
    """
    return _normalize_newlines(Path(path).read_bytes().decode("utf-8"))


def read_text_tolerant(path: str | os.PathLike[str]) -> str:
    """Read ``path`` as UTF-8 with U+FFFD replacement; same newline normalization.

    For files a THIRD-PARTY CLI wrote — plans, judge verdicts, last-message captures. A
    model's prose routinely picks up a stray cp1252 byte, and a strict decode would throw
    away a whole paid-for round over a character nothing parses. The engine reads these for a
    ``SCORE:`` line, a word count or a table row; one replacement character costs none of
    that. Deliberately NOT the shared default — see :func:`read_text_normalized`.
    """
    return _normalize_newlines(Path(path).read_bytes().decode("utf-8", "replace"))


def read_text_roundtrip(path: str | os.PathLike[str]) -> str:
    """Read a CLI-written file that will be EDITED AND WRITTEN BACK to the same path.

    Like :func:`read_text_tolerant` it never raises on a bad byte, but it preserves that byte
    instead of replacing it: ``surrogateescape`` parks each undecodable byte in a lone
    surrogate that :func:`write_text_roundtrip` turns back into the original. Tolerance is
    right for *parsing* and wrong for *reproducing a file*, where U+FFFD substitution
    silently alters the copy a reader consumes.

    Only the winner-stamping path needs this. The surrogates must not escape that round trip:
    they would raise on any plain UTF-8 encode.
    """
    return _normalize_newlines(
        Path(path).read_bytes().decode("utf-8", "surrogateescape")
    )


def write_text_roundtrip(path: str | os.PathLike[str], text: str) -> None:
    """The exact inverse of :func:`read_text_roundtrip` — see it for why.

    Through :func:`open_no_follow`, like every other write into the workdir.
    ``Path.write_bytes`` follows a planted link — mostly unreachable while ``copy_bytes`` had
    just ``os.replace``d a fresh regular file over the winner path, and no longer so now that
    a missing live plan is a warned SKIP.
    """
    open_no_follow(
        Path(path), _normalize_newlines(text).encode("utf-8", "surrogateescape")
    )


def write_text_utf8(
    path: str | os.PathLike[str], text: str, *, newline: str = "\n"
) -> None:
    """Write ``text`` as UTF-8 with an EXPLICIT newline convention (default LF).

    Any CR/CRLF already in ``text`` is normalized to ``\\n`` first, then to ``newline`` — so
    the on-disk bytes are deterministic regardless of platform.

    **Encoded with ``surrogateescape``, not strictly.** A filesystem path reaches this text
    through ``str(workdir)``, and under a non-UTF-8 locale Python decodes the OS bytes with
    surrogate escapes, which a strict encode then refuses — a complete duel crashed at the
    last step for that reason. ``surrogateescape`` is the inverse of that decode: the escapes
    ARE the original bytes, so they go back to disk exactly as the OS gave them.
    """
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    if newline != "\n":
        normalized = normalized.replace("\n", newline)
    open_no_follow(Path(path), normalized.encode("utf-8", "surrogateescape"))


def open_no_follow(path: Path, data: bytes, *, append: bool = False) -> None:
    """Write ``data`` to ``path``, REFUSING if the final component is a symlink.

    ``Path.write_bytes`` and a plain ``open`` both follow one. The workdir is writable by
    the agents this engine dispatches, so an agent that plants ``summary.md`` as a link to a
    file outside the workdir gets the engine to overwrite that file, through the very
    boundary the adapters' read-only flags advertise.

    ``O_NOFOLLOW`` settles it in the kernel on every POSIX platform, so there is no
    check-then-write window. Windows has no such flag, so there the ``lstat`` is the whole
    guard and the window is real, though small — refusing on a narrowed window beats
    following the link unconditionally.
    """
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    if not nofollow and path.is_symlink():
        raise PlanDuelError(
            f"refusing to write through a symlink: {path}. A duel artifact must be a "
            f"regular file; a link here would send the write outside the workdir."
        )
    flags = os.O_WRONLY | os.O_CREAT | nofollow
    flags |= os.O_APPEND if append else os.O_TRUNC
    try:
        handle = os.open(path, flags, 0o666)
    except OSError as exc:
        if nofollow and exc.errno in (errno.ELOOP, errno.EMLINK):
            raise PlanDuelError(
                f"refusing to write through a symlink: {path}. A duel artifact must be "
                f"a regular file; a link here would send the write outside the workdir."
            ) from exc
        raise
    with os.fdopen(handle, "wb") as out:
        out.write(data)


def write_text_atomic(path: str | os.PathLike[str], text: str) -> None:
    """:func:`write_text_utf8`, but the bytes arrive via a temp file and ``os.replace``.

    For a file whose EXISTENCE is a decision. ``summary.md`` is the duel's completion
    authority: :func:`compute_resume` answers ``complete=True`` the moment it is there and
    exits 0 without looking inside. A plain write is not atomic, so a crash partway leaves a
    truncated summary that every later resume hands to the user as a finished duel.

    ``os.replace`` also declines to write through a symlink standing at ``path``: it swaps
    the link itself for the file, which is what :func:`open_no_follow` enforces by another
    route.
    """
    path = Path(path)
    # `surrogateescape` for the same reason as `write_text_utf8`: a workdir path embedded
    # in the summary carries surrogate escapes under a non-UTF-8 locale, and a strict
    # encode threw away three completed rounds at the final write.
    data = text.replace("\r\n", "\n").replace("\r", "\n").encode("utf-8", "surrogateescape")
    handle, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(handle, "wb") as out:
            out.write(data)
            out.flush()
            os.fsync(out.fileno())
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            tmp.unlink()


def copy_bytes(src: str | os.PathLike[str], dst: str | os.PathLike[str]) -> None:
    """Copy ``src`` to ``dst`` byte-for-byte (preserves CRLF and any encoding).

    ATOMIC: the bytes land in a sibling temp file which is then ``os.replace``d over ``dst``,
    so an interrupted copy can never leave a half-written file in place. Round snapshots are
    resume authority — a truncated one that still cleared the size gate would be trusted as
    a complete plan.
    """
    dst = Path(dst)
    data = Path(src).read_bytes()
    # A UNIQUE, securely created sibling — never a predictable ``.<name>.tmp``. A
    # guessable path could already exist as a symlink, which ``write_bytes`` would
    # follow (clobbering a file outside the workdir) before ``os.replace`` installed
    # the link itself as the snapshot. Same directory, so the rename stays atomic.
    handle, tmp_name = tempfile.mkstemp(dir=dst.parent, prefix=f".{dst.name}.", suffix=".tmp")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(handle, "wb") as out:
            out.write(data)
        os.replace(tmp, dst)
    finally:
        if tmp.exists():
            tmp.unlink()


def file_size_bytes(path: str | os.PathLike[str]) -> int:
    """Return the size of ``path`` in BYTES (the ≥200 B gate is byte-based)."""
    return Path(path).stat().st_size


# --------------------------------------------------------------------------- #
# progress — optional, non-blocking, append-only per-round log
# --------------------------------------------------------------------------- #
# The run-level activity log. Distinct from the per-round ``participant-progress-N.md``
# files: a single, stable, append-only filename for the WHOLE duel, so any controller
# can ``tail``/poll one path. Deliberately ``.log`` (not ``.md``) so the round-0 Agent-B
# ``.md``-only recovery scan (``recover_agent_b_round0``) can never mistake it for a plan.
PROGRESS_LOG_NAME = "progress.log"


def append_progress(path: str | os.PathLike[str], text: str) -> None:
    """APPEND ``text`` to the per-round progress file, never truncating it.

    The progress file is observation-only: tail-able, read by nothing on the correctness
    path, and a concurrent controller/judge line must not clobber a prior one — hence append
    mode with an explicit UTF-8 encoding and no newline translation. ``text`` is encoded and
    written as bytes, which keeps the "no translation" half true on Windows too.

    Through :func:`open_no_follow`, so a progress file planted as a symlink is REFUSED rather
    than followed. The refusal raises :class:`PlanDuelError`, and every caller swallows it
    beside ``OSError``: a log line nobody reads must not fail a duel that ran correctly.
    """
    open_no_follow(Path(path), text.encode("utf-8"), append=True)


# --------------------------------------------------------------------------- #
# artifacts — filename classification + auditable cleanup
# --------------------------------------------------------------------------- #
# Round-numbered artifacts (capture group 1 = the round N). These are exactly the
# globs v1 lists for the higher-round resume cleanup.
_ROUND_ARTIFACT_RES = (
    re.compile(r"^plan-[ab]-round-(\d+)\.md$"),
    re.compile(r"^rejections-[ab]-round-(\d+)\.md$"),
    re.compile(r"^judge-round-(\d+)\.md$"),
    re.compile(r"^judge-prompt-(\d+)\.txt$"),
    re.compile(r"^controller-prompt-(\d+)\.txt$"),
    re.compile(r"^participant-prompt-(\d+)\.txt$"),
    re.compile(r"^participant-round-(\d+)-status\.md$"),
    re.compile(r"^participant-progress-(\d+)\.md$"),
)

# The broader set v1 deletes on the init-incomplete full reset — INCLUDES the
# mutable live plans (`plan-a.md` / `plan-b.md`) matched by `plan-*.md`.
_FULL_RESET_GLOBS = (
    "plan-*.md",
    "rejections-*.md",
    "judge-*.md",
    "controller-prompt-*.txt",
    "judge-prompt-*.txt",
    "participant-prompt-*.txt",
    "participant-*-status.md",
    "participant-progress-*.md",
    PROGRESS_LOG_NAME,
)

# The subset of the above that ONLY this engine ever writes — the evidence
# ``_looks_like_duel_workdir`` accepts that a directory is a duel rather than someone's
# notes. Deliberately excludes ``plan-*.md`` (a person writes those by hand),
# ``judge-*.md`` and ``rejections-*.md`` are kept because their round-numbered shape is
# ours, and ``progress.log`` is left out as too ordinary a name.
_ENGINE_ONLY_GLOBS = (
    "rejections-*.md",
    "judge-round-*.md",
    "controller-prompt-*.txt",
    "judge-prompt-*.txt",
    "participant-prompt-*.txt",
    "participant-*-status.md",
    "participant-progress-*.md",
)


def artifact_round(name: str) -> int | None:
    """Return the round number embedded in a duel artifact ``name``, else ``None``.

    ``None`` means the name is not a round-numbered duel artifact (e.g.
    ``problem.md``, ``summary.md``, ``state.json``, ``plan-a.md``).
    """
    for pattern in _ROUND_ARTIFACT_RES:
        match = pattern.match(name)
        if match is not None:
            return int(match.group(1))
    return None


def is_full_reset_artifact(name: str) -> bool:
    """True if ``name`` is one of v1's init-incomplete full-reset globs.

    ``problem.md`` / ``summary.md`` / ``state.json`` and any non-duel file are
    never matched, so a full reset preserves them.
    """
    return any(fnmatch.fnmatchcase(name, glob) for glob in _FULL_RESET_GLOBS)


def _direct_child_files(workdir: Path):
    """Yield the direct-child FILES of ``workdir`` in stable name order.

    Subdirectories are skipped — cleanup deletes only direct children and NEVER
    recurses (the v1 contract), so nested artifact-named files are untouched.
    """
    for entry in sorted(Path(workdir).iterdir(), key=lambda item: item.name):
        if entry.is_file():
            yield entry


def _normalized_relative(path: Path, workdir: Path) -> str:
    """Relative name with ``/`` separators (direct children → just the name)."""
    return path.relative_to(workdir).as_posix()


def cleanup_higher_rounds(
    workdir: str | os.PathLike[str], last_completed_round: int
) -> list[str]:
    """Delete round-numbered direct children with round > ``last_completed_round``.

    Returns the normalized relative names of the deleted files (the v1 deletion
    log). Never recurses into subdirectories.
    """
    workdir = Path(workdir)
    deleted: list[str] = []
    for entry in _direct_child_files(workdir):
        round_n = artifact_round(entry.name)
        if round_n is not None and round_n > last_completed_round:
            deleted.append(_normalized_relative(entry, workdir))
            entry.unlink()
    return deleted


def cleanup_all_artifacts(
    workdir: str | os.PathLike[str],
    *,
    keep: Collection[str] = (),
) -> list[str]:
    """Delete every duel artifact (full-reset globs) among direct children.

    Preserves ``problem.md`` / ``summary.md`` / ``state.json`` and any non-duel
    file. ``keep`` spares additional artifact names by exact match — used to carry
    an already-validated Plan A across an init-incomplete resume instead of paying
    to regenerate it. With the default empty ``keep`` the deletion log is unchanged.
    Returns the normalized relative names deleted. Never recurses.
    """
    workdir = Path(workdir)
    spared = frozenset(keep)
    deleted: list[str] = []
    for entry in _direct_child_files(workdir):
        if entry.name in spared:
            continue
        if is_full_reset_artifact(entry.name):
            deleted.append(_normalized_relative(entry, workdir))
            entry.unlink()
    return deleted


# --------------------------------------------------------------------------- #
# state — explicit on-disk markers (state.json) for auditable resume
# --------------------------------------------------------------------------- #
STATE_FILENAME = "state.json"

# Written at claim time so a workdir can be recognised as THIS tool's, rather than
# inferred from a file called problem.md - an ordinary name that an ordinary directory
# may hold. See _looks_like_duel_workdir.
DUEL_MARKER_FILENAME = ".plan-duel"


@dataclass
class RoundState:
    """Per-round completion markers, persisted so resume is auditable.

    ``plans_snapshotted`` records that both ``plan-{a,b}-round-N.md`` were written
    (the v1 completion signal); ``judge_completed`` and ``score`` capture whether
    the judge finished and what it scored — used only to *audit* resume edge cases,
    never to override the on-disk snapshot authority.
    """

    plans_snapshotted: bool = False
    judge_completed: bool = False
    score: int | None = None


@dataclass
class RunState:
    """Serializable run state marker (``state.json``)."""

    controller_name: str = ""
    participant_name: str = ""
    rounds: dict[int, RoundState] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "controller_name": self.controller_name,
            "participant_name": self.participant_name,
            "rounds": {
                str(number): {
                    "plans_snapshotted": rs.plans_snapshotted,
                    "judge_completed": rs.judge_completed,
                    "score": rs.score,
                }
                for number, rs in sorted(self.rounds.items())
            },
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, object]) -> RunState:
        rounds: dict[int, RoundState] = {}
        raw_rounds = data.get("rounds", {})
        if isinstance(raw_rounds, dict):
            for key, value in raw_rounds.items():
                if not isinstance(value, dict):
                    continue
                try:
                    number = int(key)
                except (TypeError, ValueError):
                    continue  # skip a malformed round key; load_state never raises
                rounds[number] = RoundState(
                    plans_snapshotted=bool(value.get("plans_snapshotted", False)),
                    judge_completed=bool(value.get("judge_completed", False)),
                    score=value.get("score"),
                )
        return cls(
            controller_name=str(data.get("controller_name", "")),
            participant_name=str(data.get("participant_name", "")),
            rounds=rounds,
        )


def save_state(workdir: str | os.PathLike[str], state: RunState) -> None:
    """Write ``state`` to ``{workdir}/state.json`` (UTF-8, stable key order).

    Through :func:`write_text_atomic`, for both of the properties that function owns. It
    does not write through a planted symlink — ``os.replace`` swaps the link itself for the
    file, while ``Path.write_text`` follows one.

    And a resume reads this file to decide what already happened. A crash partway through a
    plain write leaves JSON that will not parse, which :func:`load_state` reports as ``None``
    — indistinguishable from a duel that never wrote state, so every round marker is silently
    discarded. ``os.replace`` makes the file either the old one or the new one.
    """
    path = Path(workdir) / STATE_FILENAME
    write_text_atomic(
        path, json.dumps(state.to_dict(), indent=2, sort_keys=True) + "\n"
    )


def load_state(workdir: str | os.PathLike[str]) -> RunState | None:
    """Read ``state.json`` if present and parseable, else ``None`` (never raises)."""
    path = Path(workdir) / STATE_FILENAME
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    return RunState.from_dict(data)


# --------------------------------------------------------------------------- #
# resume — scan on-disk state, decide recovery (reproducing v1's outcomes)
# --------------------------------------------------------------------------- #
RESUME_INIT_INCOMPLETE_MESSAGE = "Init incomplete — restarting from round 0."
RESUME_INIT_REUSE_PLAN_A_MESSAGE = (
    "Init incomplete — reusing the validated round-0 Plan A; re-running Plan B only."
)


@dataclass(frozen=True)
class SnapshotScan:
    """What a workdir scan found (direct children only)."""

    plan_a_rounds: frozenset[int]
    plan_b_rounds: frozenset[int]
    judge_rounds: frozenset[int]
    has_live_a: bool
    has_live_b: bool
    has_summary: bool
    has_problem: bool


_PLAN_A_SNAP_RE = re.compile(r"^plan-a-round-(\d+)\.md$")
_PLAN_B_SNAP_RE = re.compile(r"^plan-b-round-(\d+)\.md$")
_JUDGE_SNAP_RE = re.compile(r"^judge-round-(\d+)\.md$")


def scan_snapshots(workdir: str | os.PathLike[str]) -> SnapshotScan:
    """Scan ``workdir`` (direct children only) for the resume-relevant artifacts."""
    workdir = Path(workdir)
    plan_a: set[int] = set()
    plan_b: set[int] = set()
    judge: set[int] = set()
    for entry in _direct_child_files(workdir):
        name = entry.name
        match = _PLAN_A_SNAP_RE.match(name)
        if match is not None:
            plan_a.add(int(match.group(1)))
            continue
        match = _PLAN_B_SNAP_RE.match(name)
        if match is not None:
            plan_b.add(int(match.group(1)))
            continue
        match = _JUDGE_SNAP_RE.match(name)
        if match is not None:
            judge.add(int(match.group(1)))
    return SnapshotScan(
        plan_a_rounds=frozenset(plan_a),
        plan_b_rounds=frozenset(plan_b),
        judge_rounds=frozenset(judge),
        has_live_a=(workdir / "plan-a.md").is_file(),
        has_live_b=(workdir / "plan-b.md").is_file(),
        has_summary=(workdir / "summary.md").is_file(),
        has_problem=(workdir / "problem.md").is_file(),
    )


def last_completed_round(workdir: str | os.PathLike[str]) -> int | None:
    """Highest round N with BOTH plan snapshots (v1's completion authority)."""
    scan = scan_snapshots(workdir)
    complete = scan.plan_a_rounds & scan.plan_b_rounds
    return max(complete) if complete else None


@dataclass
class ResumePlan:
    """The recovery decision for a workdir — reproduces v1's outcomes, auditably.

    ``complete`` → ``summary.md`` already exists (the duel is done). Otherwise, if
    no round has both plan snapshots, ``init_incomplete`` is set (re-run round 0).
    ``copies`` restore the live plans from the last completed round's snapshots;
    ``audit`` records edge-case observations (never affecting the decision).
    ``reuse_plan_a`` narrows an init-incomplete resume to Plan B alone when a
    validated round-0 Plan A snapshot survived.
    """

    workdir: Path
    complete: bool
    init_incomplete: bool
    last_completed_round: int | None
    start_round: int
    copies: list[tuple[Path, Path]]
    message: str | None
    audit: list[str]
    reuse_plan_a: bool = False


def compute_resume(workdir: str | os.PathLike[str]) -> ResumePlan:
    """Decide how to resume ``workdir`` from explicit on-disk state (no mutation).

    Reproduces the v1 golden outcomes (highest fully-snapshotted round is the
    resume point; no complete round means init was interrupted), but cross-checks
    the ``state.json`` marker and the judge/live-plan files to surface the edge
    cases v1 handled implicitly as ``audit`` notes.
    """
    workdir = Path(workdir).resolve()
    scan = scan_snapshots(workdir)
    audit: list[str] = []
    state = load_state(workdir)

    if scan.has_summary:
        return ResumePlan(
            workdir=workdir,
            complete=True,
            init_incomplete=False,
            last_completed_round=None,
            start_round=0,
            copies=[],
            message=None,
            audit=audit,
        )

    complete_rounds = scan.plan_a_rounds & scan.plan_b_rounds
    lcr = max(complete_rounds) if complete_rounds else None

    if lcr is None:
        # Init was interrupted before the first snapshot pair, so round 0 re-runs and the
        # loop starts at round 1. Plan A is snapshotted the moment the engine VALIDATES it,
        # so a surviving plan-a-round-0.md is engine-vouched work — reuse it and re-run Plan
        # B alone. The size re-check guards the one way that snapshot can be untrustworthy.
        snapshot_a = workdir / plan_snapshot_name("a", 0)
        live_a = workdir / "plan-a.md"
        reuse_plan_a = False
        if 0 in scan.plan_a_rounds and snapshot_a.is_file():
            # PROOF of completeness, not a heuristic. The snapshot is a byte copy of the live
            # plan the engine already validated, so an exact match can only hold if the copy
            # finished. This closes the gap a size gate leaves open: a snapshot interrupted
            # past 200 bytes by an older, non-atomic build still looks big enough.
            reuse_plan_a = (
                file_size_bytes(snapshot_a) >= MIN_AGENT_OUTPUT_BYTES
                and live_a.is_file()
                and live_a.read_bytes() == snapshot_a.read_bytes()
            )
            if not reuse_plan_a:
                audit.append(
                    "A round-0 Plan A snapshot is present but unproven (too small, or "
                    "no intact plan-a.md matching it byte-for-byte); it will be "
                    "discarded and regenerated rather than trusted."
                )
        if scan.has_live_a or scan.has_live_b:
            audit.append(
                "Init incomplete: a stale live plan file is present without any "
                "completed round; it will be deleted before re-running round 0."
            )
        if state is not None and state.rounds:
            audit.append(
                "state.json records prior rounds but no snapshot pair survives on "
                "disk; trusting the on-disk snapshots (v1 authority)."
            )
        return ResumePlan(
            workdir=workdir,
            complete=False,
            init_incomplete=True,
            last_completed_round=None,
            start_round=1,
            copies=[],
            message=(
                RESUME_INIT_REUSE_PLAN_A_MESSAGE
                if reuse_plan_a
                else RESUME_INIT_INCOMPLETE_MESSAGE
            ),
            audit=audit,
            reuse_plan_a=reuse_plan_a,
        )

    copies = [
        (workdir / plan_snapshot_name("a", lcr), workdir / "plan-a.md"),
        (workdir / plan_snapshot_name("b", lcr), workdir / "plan-b.md"),
    ]

    # --- Auditable edge cases (do NOT change the v1 decision) ---
    if lcr not in scan.judge_rounds:
        audit.append(
            f"Round {lcr} is complete by plan snapshots but judge-round-{lcr}.md is "
            f"missing; that round is re-judged on resume rather than scored 0, so the "
            f"trajectory matches an uninterrupted run."
        )
    elif state is not None:
        marker = state.rounds.get(lcr)
        if marker is not None and not marker.judge_completed:
            audit.append(
                f"judge-round-{lcr}.md is present but state.json marks its judge "
                f"incomplete (possibly interrupted); it is parsed leniently."
            )
    for side, snapshot in (("a", copies[0][0]), ("b", copies[1][0])):
        live = workdir / f"plan-{side}.md"
        if live.is_file() and live.read_bytes() != snapshot.read_bytes():
            audit.append(
                f"Stale live plan-{side}.md differs from its round-{lcr} snapshot; "
                f"it will be overwritten by the snapshot (v1 resume behavior)."
            )

    return ResumePlan(
        workdir=workdir,
        complete=False,
        init_incomplete=False,
        last_completed_round=lcr,
        start_round=lcr + 1,
        copies=copies,
        message=f"Resuming in {workdir} from round {lcr + 1}.",
        audit=audit,
    )


def apply_resume(plan: ResumePlan) -> list[str]:
    """Execute a :class:`ResumePlan`'s deletions and copies; return the deletion log.

    ``state.json`` is intentionally NOT deleted (it is engine-internal and rewritten
    by the run loop), keeping the deletion log byte-identical to v1's.
    """
    if plan.complete:
        return []
    if plan.init_incomplete:
        keep = (plan_snapshot_name("a", 0),) if plan.reuse_plan_a else ()
        return cleanup_all_artifacts(plan.workdir, keep=keep)
    log = cleanup_higher_rounds(plan.workdir, plan.last_completed_round)
    for src, dst in plan.copies:
        copy_bytes(src, dst)
    return log


# --------------------------------------------------------------------------- #
# freeze — snapshot per-round agent inputs to immutable files before agents run
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class FrozenInputs:
    """Immutable per-round agent inputs (the round-(N-1) plan snapshots).

    Both agents read these frozen references and write the LIVE ``plan-{a,b}.md``,
    so serialized execution is behaviorally identical to v1's "simultaneous"
    agents: the second agent to run cannot observe the first's revised plan.
    """

    round: int
    plan_a: Path
    plan_b: Path


def freeze_round_inputs(
    workdir: str | os.PathLike[str],
    round_n: int,
    *,
    live_a: str = "plan-a.md",
    live_b: str = "plan-b.md",
) -> FrozenInputs:
    """Freeze round ``round_n``'s agent inputs to the round-(N-1) plan snapshots.

    The frozen inputs are ``plan-{a,b}-round-{round_n-1}.md``. When they already exist (the
    normal loop path) they are treated as immutable; when missing (a hand-constructed or
    partially-recovered workdir) they are created by a byte-exact copy of the live plans.
    This is the seam that lets a future concurrent spawn of both agents be a pure
    accelerator, not a correctness dependency.
    """
    workdir = Path(workdir)
    prior = round_n - 1
    frozen_a = workdir / plan_snapshot_name("a", prior)
    frozen_b = workdir / plan_snapshot_name("b", prior)
    if not frozen_a.exists():
        copy_bytes(workdir / live_a, frozen_a)
    if not frozen_b.exists():
        copy_bytes(workdir / live_b, frozen_b)
    return FrozenInputs(round=round_n, plan_a=frozen_a, plan_b=frozen_b)


# --------------------------------------------------------------------------- #
# exec — argv-list subprocess dispatch (never a shell string)
# --------------------------------------------------------------------------- #
def resolve_executable(argv0: str) -> str:
    """Resolve ``argv0`` to an absolute executable path, or raise CliNotFoundError.

    A value containing a path separator (or an absolute path — e.g.
    ``sys.executable``) must exist on disk; a bare name is resolved via
    :func:`shutil.which` (which honors ``PATHEXT`` on Windows). Resolving to an
    absolute path before spawning makes ``argv[0]`` resolution independent of the
    subprocess ``cwd`` — a cross-platform footgun otherwise.
    """
    has_sep = os.sep in argv0 or bool(os.altsep and os.altsep in argv0)
    candidate = Path(argv0)
    if has_sep or candidate.is_absolute():
        # Existence is not enough: a non-executable regular file would pass here and
        # then raise PermissionError at spawn time — after the preceding agent has
        # already been paid for, which is exactly what preflight exists to prevent.
        # ``X_OK`` is not meaningful on Windows, where this degrades to existence.
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate.resolve())
        if candidate.is_file():
            raise CliNotFoundError(f"not executable: {argv0}")
        raise CliNotFoundError(f"executable not found: {argv0}")
    resolved = shutil.which(argv0)
    if resolved is None:
        raise CliNotFoundError(f"CLI not found on PATH: {argv0}")
    return resolved


def preflight_executables(specs: Mapping[str, RoleSpec]) -> None:
    """Resolve EVERY role's CLI before any billable work starts.

    :func:`run_cli` resolves an executable at spawn time, so without this the participant CLI
    is validated only at its first dispatch — after Plan A has been generated — and a missing
    CLI throws that whole run away. An executable that FAILS is probed once and reported with
    every role that needs it; one that resolves costs a ``shutil.which`` lookup per role.

    This checks only that the CLI RESOLVES; whether it can then write its plan is the
    adapter's permission contract, enforced by each command's own flags.
    """
    missing: dict[str, list[str]] = {}
    for role in REQUIRED_ROLES:
        spec = specs.get(role)
        if spec is None or not spec.command:
            continue
        argv0 = spec.command[0]
        if argv0 in missing:
            missing[argv0].append(role)
            continue
        try:
            resolve_executable(argv0)
        except CliNotFoundError:
            missing[argv0] = [role]
    if missing:
        detail = "; ".join(
            f"{cli} (needed by {', '.join(roles)})" for cli, roles in missing.items()
        )
        raise CliNotFoundError(f"CLI not found on PATH: {detail}")


@dataclass
class CliResult:
    """Outcome of a :func:`run_cli` call."""

    returncode: int
    stdout_path: Path | None
    stdout_bytes: bytes | None
    stderr_bytes: bytes | None = None


# How long a kill signal is given to land before escalating, and how long the pipes
# are then drained. Both are BOUNDED on purpose: see ``_terminate_child``.
TERMINATE_WAIT_SECONDS = 5.0
DRAIN_AFTER_KILL_SECONDS = 5.0


def _wait_quietly(proc: subprocess.Popen, seconds: float) -> None:
    """Reap ``proc`` if it exits within ``seconds``; never raise, never block longer."""
    try:
        proc.wait(timeout=seconds)
    except subprocess.TimeoutExpired:
        pass


def _terminate_child(proc: subprocess.Popen, *, group_leader: bool) -> None:
    """Best-effort kill of ``proc`` — its whole process group on POSIX; never raises.

    **POSIX: both rungs go to the GROUP, and neither is conditional on the leader.**
    ``run_cli`` spawns with ``start_new_session``, so the child's pid *is* the pgid. The
    leader exiting says nothing about descendants that inherited its pipes — the common wedge
    is a CLI that returns promptly while the runtime it spawned keeps stdout open. Gating the
    signal on ``poll()`` is how such a descendant survives. So SIGTERM the group, wait a
    bounded moment, then SIGKILL the group regardless, stopping early only on ``ESRCH``.

    A descendant that calls ``setsid()`` leaves the group and survives; the guarantee is
    group-wide, not absolute. Signalling a pgid after the leader is reaped is safe: the
    kernel reserves the pid while it is still a live group's pgid.

    **Windows: the tree, via ``taskkill``, then the direct child.** ``terminate()`` reaches
    only what we spawned, frequently a ``.cmd`` shim; killing the shim leaves the Node
    process holding the inherited stdout pipe. ``taskkill /F /T`` ends a tree with nothing
    outside the standard library — a job object would be stronger and cannot be created from
    stdlib Python. Once the shim has exited its children are re-parented and ``/T`` cannot
    find them, so the caller's bounded drain stays the backstop.
    """
    if os.name == "nt" and proc.poll() is None:
        try:
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                capture_output=True, timeout=TERMINATE_WAIT_SECONDS,
            )
        except (OSError, subprocess.SubprocessError):
            pass  # fall through to terminate/kill, which is what this always did
        _wait_quietly(proc, TERMINATE_WAIT_SECONDS)
    if group_leader:
        for hard in (False, True):
            try:
                os.killpg(proc.pid, signal.SIGKILL if hard else signal.SIGTERM)
            except (ProcessLookupError, PermissionError, OSError):
                break  # the group is gone, or was never ours to signal
            _wait_quietly(proc, TERMINATE_WAIT_SECONDS)
    else:
        for hard in (False, True):
            if proc.poll() is not None:
                break  # nothing else this branch can reach
            try:
                proc.kill() if hard else proc.terminate()
            except (ProcessLookupError, PermissionError, OSError):
                break
            _wait_quietly(proc, TERMINATE_WAIT_SECONDS)
    _wait_quietly(proc, TERMINATE_WAIT_SECONDS)  # never leave it unreaped


def run_cli(
    argv: Sequence[str],
    *,
    cwd: str | os.PathLike[str] | None = None,
    stdout_to: str | os.PathLike[str] | None = None,
    stdout_append: bool = False,
    timeout: float | None = None,
) -> CliResult:
    """Run ``argv`` as an ARGV LIST (never a shell string) with stdin ``DEVNULL``.

    ``stdout_to`` redirects the child's stdout into a file (binary — no newline translation);
    ``stdout_append`` opens it in append mode. When ``stdout_to`` is ``None`` the stdout bytes
    are captured into the result. A non-zero exit raises :class:`CliExecutionError`; a timeout
    :class:`CliTimeoutError`; an unresolvable executable :class:`CliNotFoundError`.

    **Why a hand-rolled ``Popen`` and not ``subprocess.run(timeout=)``.** That helper's
    timeout path calls ``Popen.kill()``, which signals ONLY the direct child and leaves the
    CLI's runtime holding the inherited stdout pipe; on Windows it then calls
    ``communicate()`` with NO timeout, so that survivor blocks the timeout path itself. Every
    spawn here carries a finite timeout, so this path is reachable on every run: group kill
    where the platform has groups, and a BOUNDED drain either way.
    """
    if not argv:
        raise CliExecutionError("cannot run an empty argv")
    resolved = resolve_executable(argv[0])
    full_argv = [resolved, *argv[1:]]

    stdout_handle = None
    stdout_target: Path | None = None
    popen_kwargs: dict[str, object] = {}
    # Its own process group, so the kill above can reach grandchildren. The same flag
    # is what makes ``proc.pid`` a usable pgid, so the two travel together.
    group_leader = os.name == "posix"
    if group_leader:
        popen_kwargs["start_new_session"] = True
    try:
        if stdout_to is not None:
            stdout_target = Path(stdout_to)
            # Same refusal as open_no_follow, and for the same reason: this path is
            # inside a workdir the dispatched agent can write to, so a planted link
            # would aim the CLI's whole stdout at a file outside it. Opened here rather
            # than through the helper because Popen needs the live handle, not a write.
            _nofollow = getattr(os, "O_NOFOLLOW", 0)
            _refusal = f"refusing to write agent stdout through a symlink: {stdout_target}"
            if _nofollow:
                _flags = os.O_WRONLY | os.O_CREAT | _nofollow
                _flags |= os.O_APPEND if stdout_append else os.O_TRUNC
                try:
                    stdout_handle = os.fdopen(os.open(stdout_target, _flags, 0o666), "wb")
                except OSError as exc:
                    if exc.errno in (errno.ELOOP, errno.EMLINK):
                        raise PlanDuelError(_refusal) from exc
                    raise
            else:
                # Windows. Two reasons this branch exists, and the second is not obvious:
                # there is no O_NOFOLLOW, so the lstat IS the guard — and the open must be the
                # BUILTIN one, not os.open.
                #
                # O_APPEND on Windows is a C-runtime convention: the CRT re-seeks to the end
                # before each write. Popen gives the child a duplicated Win32 HANDLE, not a
                # CRT fd, so an os.open'd append fd starts the child writing at offset ZERO.
                # `open(..., "ab")` positions the pointer at EOF, and a duplicated handle DOES
                # inherit the position. Windows CI caught this: a seeded progress file came
                # back with the seed gone.
                if stdout_target.is_symlink():
                    raise PlanDuelError(_refusal)
                stdout_handle = open(stdout_target, "ab" if stdout_append else "wb")
            stdout_arg = stdout_handle
        else:
            stdout_arg = subprocess.PIPE
        proc = subprocess.Popen(
            full_argv,
            stdin=subprocess.DEVNULL,
            stdout=stdout_arg,
            stderr=subprocess.PIPE,
            cwd=str(cwd) if cwd is not None else None,
            **popen_kwargs,
        )
        drained = terminated = completed = False
        try:
            try:
                stdout_bytes, stderr_bytes = proc.communicate(timeout=timeout)
                drained = True
            except subprocess.TimeoutExpired as exc:
                _terminate_child(proc, group_leader=group_leader)
                terminated = True
                try:
                    proc.communicate(timeout=DRAIN_AFTER_KILL_SECONDS)
                    drained = True
                except subprocess.TimeoutExpired:
                    pass  # a survivor still holds the pipe; abandon it, do not hang
                raise CliTimeoutError(
                    f"CLI timed out after {timeout}s: {argv[0]}"
                ) from exc
            returncode = proc.returncode
            completed = True
        finally:
            # Never leave a running child behind, whatever raised — a KeyboardInterrupt out
            # of ``communicate`` is the live case, and ``start_new_session`` means the child
            # no longer takes the terminal's Ctrl-C for itself.
            #
            # Skipped where signalling is wrong rather than merely redundant. The timeout
            # path already escalated, so repeating the ladder would double this call's
            # worst-case duration. And a CLEAN run must not be signalled at all: pipes at EOF
            # means no survivor holds them, while a group kill would still reach a background
            # helper the CLI deliberately left running.
            if not (terminated or completed):
                _terminate_child(proc, group_leader=group_leader)
            # Closing stays BEHIND the drain guard. On Windows ``communicate`` reads
            # through helper threads; an undrained reader is still live, and closing
            # the handle underneath it turns its next read into an uncaught ValueError
            # in that thread. A drained pipe is already closed by ``communicate``, so
            # this only ever mops up the paths that raised before it got there.
            if drained:
                for pipe in (proc.stdout, proc.stderr):
                    if pipe is not None:
                        try:
                            pipe.close()
                        except OSError:  # pragma: no cover - platform-dependent
                            pass
    finally:
        if stdout_handle is not None:
            stdout_handle.close()

    if returncode != 0:
        stderr_text = (stderr_bytes or b"").decode("utf-8", "replace").strip()
        detail = f" — {stderr_text}" if stderr_text else ""
        raise CliExecutionError(
            f"CLI exited with code {returncode}: {argv[0]}{detail}"
        )

    return CliResult(
        returncode=returncode,
        stdout_path=stdout_target,
        stdout_bytes=stdout_bytes if stdout_to is None else None,
        stderr_bytes=stderr_bytes,
    )


# --------------------------------------------------------------------------- #
# capture — per-adapter output-capture policy + failure handling
# --------------------------------------------------------------------------- #
MIN_AGENT_OUTPUT_BYTES = 200


def _agent_output_is_usable(path: Path) -> bool:
    """True when ``path`` is a REGULAR file (not a link to one) the engine can READ,
    of >= :data:`MIN_AGENT_OUTPUT_BYTES`.

    ``lstat`` + ``S_ISREG``, deliberately not ``is_file()``, which follows a symlink. An agent
    that exits 0 after pointing ``plan-b.md`` at ``plan-a.md`` hands back a plan it did not
    write, and a gate looking through the link snapshots it as this round's work. A directory
    fails the same predicate.

    Then validated by actually READING it, because the caller's next move is a copy: a path
    that stats fine and cannot be read would otherwise surface as a bare ``PermissionError``
    outside the diagnostic path. This narrows the check-to-copy window; nothing here can
    close it.
    """
    try:
        if not stat.S_ISREG(os.lstat(path).st_mode):
            return False
        return len(path.read_bytes()) >= MIN_AGENT_OUTPUT_BYTES
    except OSError:
        return False


def _agent_output_rejection(path: Path) -> str:
    """Why :func:`_agent_output_is_usable` said no, in a few words for the halt line.

    The halt used to carry only the CLI's own tail, and an agent that exits 0 after writing a
    SHORT plan leaves a tail that reads like success — `wrote /…/plan-a.md` — beside a file
    that exists and looks fine. The message then points away from the cause, and the only way
    to learn the real one is to read this source for the 200-byte floor.
    """
    try:
        if not path.exists():
            return f"{path.name} was not written"
        if not stat.S_ISREG(os.lstat(path).st_mode):
            return f"{path.name} is not a regular file"
        size = len(path.read_bytes())
        if size < MIN_AGENT_OUTPUT_BYTES:
            return (f"{path.name} is {size} bytes, under the {MIN_AGENT_OUTPUT_BYTES}-byte "
                    f"floor for a usable plan")
    except OSError as exc:
        return f"{path.name} could not be read: {exc.strerror or exc}"
    return f"{path.name} was rejected"


# Round-0 Agent-B fallback: files that are NEVER candidates for a recovered plan.
_AGENT_B_FALLBACK_EXCLUDE = frozenset(
    {
        "problem.md",
        "plan-a.md",
        "participant-round-0-status.md",
        "participant-progress-0.md",
        # ``progress.log`` is a ``.log`` file so the scan below (``.md``-only) already
        # skips it; listed here too as belt-and-suspenders if the scan is ever widened.
        PROGRESS_LOG_NAME,
    }
)


def agent_failure_message(side: str, round_n: int) -> str:
    """The exact v1 halt line for an agent failure (round 0 vs a critique round)."""
    letter = side.upper()
    if round_n == 0:
        return f"Agent {letter} plan generation failed at round 0."
    return f"Agent {letter} update failed at round {round_n}."


# How much of an agent's captured status stream to quote in a halt's ``cause``.
STATUS_TAIL_CHARS = 400


def _tail_line(text: str, max_chars: int) -> str | None:
    """Collapse ``text`` to one line and keep at most ``max_chars`` of its tail."""
    collapsed = " ".join(text.split())
    if not collapsed:
        return None
    if len(collapsed) > max_chars:
        collapsed = "…" + collapsed[-max_chars:]
    return collapsed


def status_tail(
    status_to: str | os.PathLike[str] | None,
    *,
    max_chars: int = STATUS_TAIL_CHARS,
    stdout_bytes: bytes | None = None,
    stderr_bytes: bytes | None = None,
) -> str | None:
    """The tail of an agent's own output, collapsed to one diagnostic line.

    A CLI that cannot write its plan usually EXITS ZERO and explains why in its final
    message, so the process looks successful and only the missing output file signals
    failure. Without this, that explanation sits unread while the halt line says only "failed
    at round N".

    Three sources, in order of signal quality: the captured status FILE, the captured stdout
    BYTES, then STDERR. Stderr is last because runtimes commonly put the human-readable
    transcript there and the final message on stdout — but it is the only source left when a
    CLI says nothing on stdout. ``None`` when nothing usable is found, keeping the halt line
    byte-identical to the golden.
    """
    for source in (status_to, stdout_bytes, stderr_bytes):
        if source is None:
            continue
        try:
            raw = source if isinstance(source, bytes) else Path(source).read_bytes()
        except OSError:
            continue
        # Decode with replacement, never strictly: a CLI may emit non-UTF-8 bytes, and
        # a diagnostic must neither raise (replacing the halt it exists to explain) nor
        # discard an otherwise-readable explanation over one bad byte.
        text = raw.decode("utf-8", "replace")
        tail = _tail_line(text, max_chars)
        if tail is not None:
            return f"last output: {tail}"
    return None


def run_agent(
    argv: Sequence[str],
    output_file: str | os.PathLike[str],
    *,
    side: str,
    round_n: int,
    cwd: str | os.PathLike[str] | None = None,
    status_to: str | os.PathLike[str] | None = None,
    stdout_append: bool = False,
    timeout: float | None = None,
) -> Path:
    """Run an agent CLI and validate its FILE output (the capture policy for A/B).

    The agent writes its artifact directly; the CLI's stdout is only a status stream
    (``status_to``) and is NEVER the result. On any failure — non-zero exit, timeout,
    unresolvable CLI, or a missing/<200 B output file — the halt mirrors v1 exactly. Returns
    the validated ``output_file`` path.
    """
    output_file = Path(output_file)
    halt = agent_failure_message(side, round_n)
    try:
        result = run_cli(
            argv,
            cwd=cwd,
            stdout_to=status_to,
            stdout_append=stdout_append,
            timeout=timeout,
        )
    except CliTimeoutError as exc:
        raise AgentOutputError(halt, cause="CLI timed out") from exc
    except ProcessError as exc:
        raise AgentOutputError(halt, cause=str(exc)) from exc

    # Regular file, readable, and big enough — see ``_agent_output_is_usable``. A DIRECTORY
    # at this path reports an ``st_size`` of 4096 on Linux, so a size-only check accepts it
    # and ``copy_bytes`` dies with a bare ``IsADirectoryError`` several steps away; an
    # unreadable regular file does the same with ``PermissionError``.
    if not _agent_output_is_usable(output_file):
        # Roles without a status path (Agent A) still have their explanation in the
        # captured bytes, so the diagnostic works for BOTH sides.
        # The rejection reason FIRST, then the CLI's tail. An agent that exits 0 after
        # writing a short plan produces a tail that reads like success, so the tail alone
        # sends the reader to the wrong place.
        tail = status_tail(
            status_to,
            stdout_bytes=result.stdout_bytes,
            stderr_bytes=result.stderr_bytes,
        )
        # Only where there is ALREADY a tail. The bare halt — the exact v1 line, with no
        # cause — is a parity contract pinned by three tests. What the reviewer hit had a
        # tail reading `wrote /…/plan-a.md`, which looks like success; that is the one worth
        # annotating, and it is annotated without changing what a bare halt says.
        reason = _agent_output_rejection(output_file)
        raise AgentOutputError(halt, cause=f"{reason} — last output: {tail}" if tail else None)
    return output_file


def capture_judge_message(
    argv: Sequence[str],
    message_path: str | os.PathLike[str],
    *,
    cwd: str | os.PathLike[str] | None = None,
    redirect_stdout: bool = False,
    status_to: str | os.PathLike[str] | None = None,
    timeout: float | None = None,
    round_n: int | None = None,
) -> str:
    """Capture the judge's CLEAN final message — never a raw transcript.

    Two adapter shapes are supported, both landing the clean message in
    ``message_path`` (the file the score is parsed from):

    * ``redirect_stdout=False`` (``--output-last-message``-style): the CLI writes
      ``message_path`` itself; its raw stdout (possibly a transcript that echoes
      the prompt's ``SCORE:`` template) goes to ``status_to`` and is discarded.
    * ``redirect_stdout=True`` (a clean-stdout runtime): the engine redirects the
      CLI's stdout directly into ``message_path``.

    Either way the returned text is read from ``message_path``, so the first
    ``SCORE:`` line parse can never be poisoned by an echoed prompt. A process
    failure or an empty/missing message raises :class:`JudgeOutputError`.
    """
    message_path = Path(message_path)
    stdout_to = message_path if redirect_stdout else status_to
    try:
        run_cli(argv, cwd=cwd, stdout_to=stdout_to, timeout=timeout)
    except ProcessError as exc:
        raise JudgeOutputError(f"Judge process failed at round {round_n}: {exc}") from exc

    if not message_path.exists() or file_size_bytes(message_path) == 0:
        raise JudgeOutputError(f"Judge produced no output at round {round_n}.")
    return read_text_tolerant(message_path)


def recover_agent_b_round0(
    workdir: str | os.PathLike[str],
    *,
    min_bytes: int = MIN_AGENT_OUTPUT_BYTES,
    max_age_seconds: float = 300,
    now: float | None = None,
) -> str | None:
    """v1's round-0 Agent-B fallback: adopt a recent stray ``.md`` as ``plan-b.md``.

    Scans ``workdir`` (direct children only) for a ``.md`` file — other than
    ``problem.md`` / ``plan-a.md`` / the round-0 status & progress files, any
    engine-written ``plan-{a,b}-round-N.md`` snapshot, and ``plan-b.md`` itself —
    that is ≥ ``min_bytes`` and was written within the last
    ``max_age_seconds``. If found (most-recent wins), copies it to ``plan-b.md``
    and returns the v1 log line; otherwise returns ``None`` (the caller then
    halts with the round-0 Agent-B failure message).
    """
    workdir = Path(workdir)
    reference = now if now is not None else time.time()
    candidates: list[Path] = []
    for entry in _direct_child_files(workdir):
        name = entry.name
        if name == "plan-b.md" or name in _AGENT_B_FALLBACK_EXCLUDE:
            continue
        if not name.endswith(".md"):
            continue
        if _PLAN_A_SNAP_RE.match(name) or _PLAN_B_SNAP_RE.match(name):
            # A round snapshot is engine-written, never a stray participant artifact.
            # Plan A's round-0 snapshot now lands BEFORE Agent B runs, so without this
            # guard the fallback would adopt it and make Plan B a copy of Plan A.
            continue
        # os.lstat, and the local is `info` rather than `stat`, because the name `stat`
        # shadows the module imported at the top of this file.
        #
        # Following the link here is an escape from the sandbox the participant runs in. An
        # agent confined to the workdir can still create a link inside it, and the engine is
        # unconfined: `entry.is_file()`, `entry.stat()` and `copy_bytes`' read all
        # dereference, so an outside file's bytes are published as Plan B and frozen as the
        # round-0 snapshot fed to the judge. This was the one place skipping the lstat check
        # that _agent_output_is_usable, _require_regular_file and open_no_follow all apply.
        #
        # NOT pushed down into _direct_child_files: cleanup_higher_rounds and
        # cleanup_all_artifacts share it, and narrowing it there would change which
        # artifacts a resume deletes.
        info = os.lstat(entry)
        if not stat.S_ISREG(info.st_mode):
            continue
        if info.st_size < min_bytes:
            continue
        if reference - info.st_mtime > max_age_seconds:
            continue
        candidates.append(entry)
    if not candidates:
        return None
    chosen = max(candidates, key=lambda item: os.lstat(item).st_mtime)
    copy_bytes(chosen, workdir / "plan-b.md")
    return f"Fallback: used {chosen.name} as plan-b.md."


# --------------------------------------------------------------------------- #
# summary — judge-field extraction, winner stamping, scoped rewrite, assembly
# --------------------------------------------------------------------------- #
SUITE_ROW_VALUE = "plan-init / plan-phase / plan-run"
STATUS_FORMAT_ROW = "| Format | v2 |"
STATUS_SUITE_ROW = f"| Suite | {SUITE_ROW_VALUE} |"

# Status cells that claim to track something no skill ever comes back to update:
# plan.md is written once, and /plan-phase and /plan-run both leave it alone. A
# value here can therefore only ever be stale, so stamping strips them.
MUTABLE_STATUS_KEYS = frozenset({"Phase", "State", "Blocker", "Last updated"})

# Every row ``stamp_winner_plan`` rewrites: the two it owns the value of, plus the
# stale-by-construction ones above. Dropped from an existing table before the canonical
# pair is prepended, so the stamp corrects a value instead of only noticing the key is
# present. Folded for matching, or ``| format | v1 |`` would survive alongside the fresh
# ``| Format | v2 |`` and leave the plan asserting both.
_STAMPED_STATUS_KEYS = frozenset(
    key.casefold() for key in MUTABLE_STATUS_KEYS | {"Format", "Suite"}
)


def _is_stamped_status_key(key: str | None) -> bool:
    """True when ``key`` names a row :func:`stamp_winner_plan` writes itself."""
    return key is not None and key.casefold() in _STAMPED_STATUS_KEYS

# The verbatim note appended when a duel ran >= 5 rounds (from summary.md). The
# summary.md source soft-wraps it across lines; it renders as one paragraph, so
# it is stored here as a single logical line.
FIVE_ROUND_NOTE = (
    "Note: after {rounds_run} rounds of mutual critique, both plans have heavily "
    "incorporated each other's ideas; the winner reflects structural and clarity "
    "differences more than fundamental approach divergence."
)

_MISSED_NONE = "none"


def _label_re(label: str) -> re.Pattern[str]:
    """Match one judge-field LABEL at the head of a line, decoration and case tolerant.

    The same Markdown a judge wraps ``PREFERRED:`` in it wraps the other two labels in, so
    all three are recognised the same way. ``match.end()`` is where the label's own inline
    value starts, which is what :func:`_block_after` needs.
    """
    return re.compile(
        r"^[ \t]*(?:\*\*)?[ \t]*" + re.escape(label) + r"[ \t]*:[ \t]*(?:\*\*)?[ \t]*",
        re.IGNORECASE,
    )


_DIFFERENCES_RE = _label_re("DIFFERENCES")
_MISSED_RE = _label_re("MISSED REJECTIONS")


@dataclass(frozen=True)
class JudgeFields:
    """The structured fields extracted from a judge round file.

    ``score`` is the verdict's integer score (``None`` if missing/unparseable),
    ``differences`` the differences block, ``missed_rejections`` the missed-rejections value
    (``"none"`` when there are none), ``preferred`` ``'A'`` / ``'B'`` / ``None``, and
    ``justification`` the winner's defence paragraph.

    ``differences`` is a rendered STRING in both parse paths — the JSON verdict's array is
    rendered into the same numbered lines the marker contract used. That keeps one
    downstream path, so ``summary.md`` is byte-shaped identically whichever contract the
    round was judged under.
    """

    score: int | None
    differences: str
    missed_rejections: str
    preferred: str | None
    justification: str


def _find_marker(lines: Sequence[str], pattern: re.Pattern[str]) -> int | None:
    """Index of the first line ``pattern`` matches at its head, else ``None``.

    Was ``line.startswith("DIFFERENCES:")`` — exact case, no decoration — so a judge that
    wrote ``**PREFERRED:** B`` had its winner resolved and its JUSTIFICATION come back
    empty, while the unmatched marker line was swallowed into the block above it. One
    definition of "this line is the label" now serves both readers.
    """
    for index, line in enumerate(lines):
        if pattern.match(line):
            return index
    return None


def _find_preferred_line(lines: Sequence[str]) -> int | None:
    """Index of the ``PREFERRED:`` line the justification follows, else ``None``.

    Prefers the first label that names a SIDE — the same choice
    :func:`read_preferred_marker` makes, so the winner and the justification are read from
    one line rather than two. Falls back to the last unreadable label, so a verdict whose
    preference line cannot be parsed still surrenders its justification paragraph.
    """
    fallback = None
    for index, line in enumerate(lines):
        match = _PREFERRED_LINE_RE.match(line)
        if match is None:
            continue
        if _side_from_value(match.group("value").strip()) is not None:
            return index
        fallback = index
    return fallback


def _block_after(
    lines: Sequence[str], start: int, pattern: re.Pattern[str], end: int | None
) -> str:
    """Text from the label line's inline remainder through ``end`` (exclusive)."""
    match = pattern.match(lines[start])
    inline = lines[start][match.end() :].strip() if match else ""
    body = lines[start + 1 : end] if end is not None else lines[start + 1 :]
    combined = ([inline] if inline else []) + list(body)
    return "\n".join(combined).strip("\n").strip()


def _as_sentence(value: object) -> str:
    """One trimmed clause, terminated — so rendered differences read as prose.

    The schema does not require the model to punctuate each field, so a value arriving as
    ``Uses a queue`` must not render with a missing stop or a doubled one.
    """
    text = " ".join(str(value).split())
    if not text:
        return ""
    return text if text[-1] in ".!?:;" else text + "."


def render_differences(items: Sequence[Mapping[str, object]]) -> str:
    """Render the verdict's ``differences`` array into the marker-contract block.

    Emits the exact line shape the pre-schema judge wrote by hand, so
    :func:`rewrite_differences` and ``summary.md`` need no second code path:

        1. <topic>: Plan A: <plan_a>. Plan B: <plan_b>. **Stronger: A** — <reason>

    An empty array renders as ``none``, the value the summary already understands. Entries
    are read leniently because a degraded verdict should still produce a readable summary.
    """
    if not items:
        return _MISSED_NONE
    lines: list[str] = []
    for number, item in enumerate(items, 1):
        if not isinstance(item, Mapping):
            lines.append(f"{number}. {' '.join(str(item).split())}")
            continue
        topic = " ".join(str(item.get("topic", "")).split())
        plan_a = _as_sentence(item.get("plan_a", ""))
        plan_b = _as_sentence(item.get("plan_b", ""))
        stronger = " ".join(str(item.get("stronger", "")).split())
        reason = " ".join(str(item.get("reason", "")).split())
        head = f"{number}. {topic}: " if topic else f"{number}. "
        # Assembled from the parts that are actually present: a verdict missing one
        # side must not render a dangling "Plan A:" or a doubled separator space.
        segments = [
            f"Plan {side}: {value}"
            for side, value in (("A", plan_a), ("B", plan_b))
            if value
        ]
        line = f"{head}{' '.join(segments)}"
        if stronger:
            line += f" **Stronger: {stronger}**"
            if reason:
                line += f" — {reason}"
        elif reason:
            line += f" — {reason}"
        lines.append(line)
    return "\n".join(lines)


def render_missed_rejections(value: object) -> str:
    """Render the verdict's ``missed_rejections`` into the summary's block text.

    An empty array (or anything empty) becomes ``none``, which is exactly what
    :func:`assemble_summary` keys on to omit the section entirely. A non-empty array
    becomes a markdown bullet list; a bare string is passed through for leniency.
    """
    if isinstance(value, str):
        return value.strip() or _MISSED_NONE
    if isinstance(value, Sequence):
        entries = [" ".join(str(item).split()) for item in value]
        entries = [entry for entry in entries if entry]
        if entries:
            return "\n".join(f"- {entry}" for entry in entries)
    return _MISSED_NONE


def _overlay_json_fields(obj: Mapping[str, object], legacy: JudgeFields) -> JudgeFields:
    """Overlay a decoded verdict onto the marker-parsed fields, FIELD BY FIELD.

    Every field falls through to ``legacy`` unless the object carries a usable value for it
    — the same per-field degrade :func:`parse_score` and :func:`parse_preferred` implement,
    extended to the other three.

    This is what makes adopting an object non-destructive. A legacy marker file whose
    justification quotes a JSON payload could otherwise be read as a verdict, blanking the
    differences and justification the markers really carry. With the overlay, the worst case
    of a false adoption is that nothing changes.
    """
    differences_raw = obj.get("differences")
    if isinstance(differences_raw, str):
        differences = differences_raw.strip() or legacy.differences
    elif isinstance(differences_raw, Sequence):
        differences = render_differences(list(differences_raw))
    else:
        differences = legacy.differences

    missed_raw = obj.get("missed_rejections")
    missed = (
        render_missed_rejections(missed_raw)
        if isinstance(missed_raw, (str, Sequence))
        else legacy.missed_rejections
    )

    justification = obj.get("justification")
    return JudgeFields(
        score=legacy.score,
        differences=differences,
        missed_rejections=missed,
        preferred=legacy.preferred,
        justification=(
            justification.strip() or legacy.justification
            if isinstance(justification, str)
            else legacy.justification
        ),
    )


def extract_judge_fields(text: str) -> JudgeFields:
    """Extract the judge's structured fields (see :class:`JudgeFields`).

    The marker contract is parsed FIRST — line-based, not a fragile whole-file regex — so
    the ``DIFFERENCES:`` block is captured as authored and the ``PREFERRED:`` justification
    kept verbatim, which a resume over an older workdir depends on. A schema-enforced JSON
    verdict is then overlaid field by field on top.

    Ordering it this way rather than "JSON, else markers" makes the JSON path additive, so
    no field can be blanked by adopting an object that turned out not to be the verdict.
    """
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    lines = normalized.split("\n")

    diff_idx = _find_marker(lines, _DIFFERENCES_RE)
    missed_idx = _find_marker(lines, _MISSED_RE)
    # The PREFERRED line is located by the SAME reading that takes the side off it, so
    # the two can never disagree about which line that is — including which of several
    # candidates is the real label.
    pref_idx = _find_preferred_line(lines)

    differences = ""
    if diff_idx is not None:
        candidates = [i for i in (missed_idx, pref_idx) if i is not None and i > diff_idx]
        end = min(candidates) if candidates else None
        differences = _block_after(lines, diff_idx, _DIFFERENCES_RE, end)

    missed = _MISSED_NONE
    if missed_idx is not None:
        end = pref_idx if (pref_idx is not None and pref_idx > missed_idx) else None
        missed = _block_after(lines, missed_idx, _MISSED_RE, end) or _MISSED_NONE

    justification = ""
    if pref_idx is not None:
        justification = "\n".join(lines[pref_idx + 1 :]).strip("\n").strip()

    # score/preferred are already JSON-first with their own marker fall-through.
    legacy = JudgeFields(
        score=parse_score(normalized),
        differences=differences,
        missed_rejections=missed,
        preferred=parse_preferred(normalized),
        justification=justification,
    )
    obj = parse_judge_json(normalized)
    return legacy if obj is None else _overlay_json_fields(obj, legacy)


# Longest-first alternation so ``Stronger: A`` wins over a bare ``Plan A`` overlap;
# a single left-to-right pass means a substituted value is never re-scanned (so a
# concrete name that itself contains a token like ``B`` cannot be double-rewritten).
_DIFF_TOKEN_RE = re.compile(r"Stronger: A|Stronger: B|Plan A|Plan B")


def rewrite_differences(
    differences: str, controller_name: str, participant_name: str
) -> str:
    """Scoped A/B → concrete-name rewrite, confined to the ``differences`` block.

    Applies ONLY to the extracted ``DIFFERENCES:`` field — never a global replace over the
    whole summary — so quoted content and the justification paragraph are left intact. The
    four tokens are rewritten in a SINGLE pass (via a lambda, so a name is inserted literally
    and never re-scanned), so a concrete name overlapping a later token cannot be corrupted.
    """
    mapping = {
        "Stronger: A": f"Stronger: {controller_name}",
        "Stronger: B": f"Stronger: {participant_name}",
        "Plan A": controller_name,
        "Plan B": participant_name,
    }
    return _DIFF_TOKEN_RE.sub(lambda m: mapping[m.group(0)], differences)


# At most THREE spaces of indentation, per CommonMark. A fourth makes the line
# indented-code CONTENT rather than a fence, and the difference is not cosmetic —
# see ``_fenced_lines``.
_FENCE_RE = re.compile(r"^ {0,3}(?P<fence>`{3,}|~{3,})(?P<info>.*)$")


def _fence_marker(line: str) -> tuple[str, str] | None:
    """``(fence, info)`` when ``line`` is a CommonMark fence line, else ``None``.

    Two rules beyond "starts with three of the character", both deciding whether a line is a
    fence at all: at most three spaces of indentation, and no backtick anywhere in a BACKTICK
    fence's info string (a tilde fence may hold one).
    """
    match = _FENCE_RE.match(line)
    if match is None:
        return None
    fence, info = match.group("fence"), match.group("info")
    if fence[0] == "`" and "`" in info:
        return None
    return fence, info


def _fenced_lines(lines: Sequence[str]) -> list[bool]:
    """Flag every line inside a fenced code block, the fence lines themselves included.

    :func:`stamp_winner_plan` must not treat a status table QUOTED IN AN EXAMPLE as the
    plan's own. A duel about planning routinely produces a plan whose prose shows a
    ``## Status`` table in a fence — rewriting that example edits documentation the engine
    does not own, and leaves the real plan unstamped.

    A closing fence must use the opener's character, be at least as long, and carry no info
    string; an unterminated fence runs to the end of the document. **Leniency is not
    symmetric**: reading a doubtful line as an OPENER errs toward leaving text alone, while
    reading one as a CLOSER un-fences everything below it.
    """
    flags = [False] * len(lines)
    opener: str | None = None
    for i, line in enumerate(lines):
        marker = _fence_marker(line)
        if opener is None:
            if marker is not None:
                opener = marker[0]
                flags[i] = True
            continue
        flags[i] = True
        if (
            marker is not None
            and marker[0][0] == opener[0]
            and len(marker[0]) >= len(opener)
            and not marker[1].strip()
        ):
            opener = None
    return flags


def _table_row_key(line: str) -> str | None:
    """First cell of a markdown table row (``| Format | v2 |`` → ``"Format"``)."""
    stripped = line.strip()
    if not stripped.startswith("|"):
        return None
    cells = [cell.strip() for cell in stripped.strip("|").split("|")]
    return cells[0] if cells else None


def _is_separator_row(line: str) -> bool:
    """True for a markdown table separator row (``|---|---|``)."""
    stripped = line.strip()
    if not stripped.startswith("|"):
        return False
    cells = [cell.strip() for cell in stripped.strip("|").split("|")]
    return bool(cells) and all(
        cell and set(cell) <= set("-: ") and "-" in cell for cell in cells
    )


def stamp_winner_plan(text: str) -> str:
    """Stamp the winning plan with the v2 ``Format`` / ``Suite`` markers.

    If the plan already has a ``## Status`` table, the two rows are written at the TOP of it
    with their canonical VALUES, replacing any ``Format`` / ``Suite`` row the agent wrote
    rather than merely noting the key is there — so ``| Format | v1 |`` comes back as ``v2``,
    with no duplicate row. Any ``MUTABLE_STATUS_KEYS`` row is dropped. Otherwise a fresh
    ``## Status`` block is inserted beneath the plan title. Only ever called on the winner.

    **Fenced regions are invisible to all of this** (:func:`_fenced_lines`), and owned keys
    are matched case-insensitively when removing.
    """
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    lines = normalized.split("\n")
    fenced = _fenced_lines(lines)

    status_idx = next(
        (
            i
            for i, line in enumerate(lines)
            if not fenced[i] and line.strip() == "## Status"
        ),
        None,
    )
    if status_idx is not None:
        sep_idx = None
        for i in range(status_idx + 1, len(lines)):
            if fenced[i]:
                continue
            if _is_separator_row(lines[i]):
                sep_idx = i
                break
            if lines[i].strip().startswith("## "):
                break  # next section before any table
        if sep_idx is not None:
            end_idx = sep_idx + 1
            for i in range(sep_idx + 1, len(lines)):
                if fenced[i] or _table_row_key(lines[i]) is None:
                    break
                end_idx = i + 1
            # Drop the agent's own Format/Suite rows along with the stale-by-construction
            # ones, then prepend the canonical pair. Dropping is what CORRECTS a wrong
            # value: keeping the row and skipping the insert leaves `| Format | v1 |`
            # behind, and inserting without dropping duplicates the key. Re-inserting at
            # the top is also why this stays idempotent.
            kept = [
                line
                for line in lines[sep_idx + 1 : end_idx]
                if not _is_stamped_status_key(_table_row_key(line))
            ]
            # Always rewrite the row span: the two markers are prepended and the
            # stale-by-construction rows are gone, so the table never ends up as a
            # header and separator with nothing under them.
            lines[sep_idx + 1 : end_idx] = [STATUS_FORMAT_ROW, STATUS_SUITE_ROW] + kept
            return "\n".join(lines)
        # A `## Status` heading with no table beneath it: give it a table with the
        # two load-bearing rows in place, rather than inserting a SECOND `## Status`
        # block beneath the title (which would duplicate the heading).
        table = [
            "",
            "| Field | Value |",
            "|---|---|",
            STATUS_FORMAT_ROW,
            STATUS_SUITE_ROW,
        ]
        lines[status_idx + 1 : status_idx + 1] = table
        return "\n".join(lines)

    block = [
        "",
        "## Status",
        "",
        "| Field | Value |",
        "|---|---|",
        STATUS_FORMAT_ROW,
        STATUS_SUITE_ROW,
    ]
    # Fenced lines skipped here too: a `# ` heading inside an example must not be
    # mistaken for the plan's title and take the block that belongs under the real one.
    title_idx = next(
        (
            i
            for i, line in enumerate(lines)
            if not fenced[i] and line.startswith("# ")
        ),
        None,
    )
    if title_idx is not None:
        lines[title_idx + 1 : title_idx + 1] = block
    else:
        lines[0:0] = block[1:] + [""]
    return "\n".join(lines)


# What a trajectory cell holds when there is no value to put in it. Round 0's SCORE cell
# has always used this; a word count for an absent snapshot now uses the same mark, so the
# table has one spelling for "nothing to report" rather than two.
MISSING_CELL = "—"


def word_count_file(path: str | os.PathLike[str]) -> int | None:
    """Whitespace-delimited word count of a file (the ``wc -w`` equivalent).

    ``None`` when the file cannot be read at all. Reading tolerantly covers an undecodable
    BYTE; a snapshot that is simply ABSENT raised ``FileNotFoundError`` out of
    :func:`write_summary`, at the last step of a duel already paid for. A workdir with a
    snapshot gap is ordinary, and :func:`compute_resume` is built to survive it.

    A count nobody could take is reported as :data:`MISSING_CELL`, the same way a round with
    no score is.
    """
    try:
        return len(read_text_tolerant(path).split())
    except OSError:
        return None


def word_count_cell(count: int | None) -> str:
    """Render a word count for display: the number, or ``—`` when there was none.

    One function so the trajectory table and the per-round progress lines spell a
    missing count the same way, rather than one of them printing ``None``.
    """
    return MISSING_CELL if count is None else str(count)


def assemble_summary(
    *,
    workdir_display: str,
    rounds_run: int,
    stopped_due_to: str,
    controller_name: str,
    participant_name: str,
    controller_slug: str,
    participant_slug: str,
    winner_name: str,
    winner_file: str,
    trajectory: Sequence[tuple[int, int | None, int | None, int | None]],
    justification: str,
    differences_rewritten: str,
    missed_rejections: str,
) -> str:
    """Render the full ``summary.md`` body from computed pieces (pure, no I/O).

    Emits the v1 sections in order: header block, ``## Score trajectory`` (round 0
    shows ``—``), ``## Why {winner} won`` (+ the ``rounds_run >= 5`` note),
    ``## Remaining differences``, ``## Missed rejections`` (only when
    ``missed_rejections != "none"``), and ``## All files``.
    """
    out: list[str] = []
    out.append("# Plan Duel Summary")
    out.append("")
    out.append(f"**Problem:** {workdir_display}/problem.md")
    out.append(
        f"**Rounds run:** {rounds_run} "
        f"(0 = initial plans, 1–{rounds_run} = critique rounds)"
    )
    out.append(f"**Stopped due to:** {stopped_due_to}")
    out.append(
        f"**Winner:** {winner_name} → {workdir_display}/{winner_file} "
        "(stamped `Format: v2` — feed it to `/plan-phase`)"
    )
    out.append("")
    out.append("## Score trajectory")
    out.append("")
    out.append(
        f"| Round | Score | {controller_name} words | {participant_name} words |"
    )
    out.append("|---|---|---|---|")
    for round_n, score, a_words, b_words in trajectory:
        score_cell = MISSING_CELL if score is None else str(score)
        out.append(
            f"| {round_n} | {score_cell} | {word_count_cell(a_words)} | "
            f"{word_count_cell(b_words)} |"
        )
    out.append("")
    out.append(f"## Why {winner_name} won")
    out.append("")
    out.append(justification)
    if rounds_run >= 5:
        out.append("")
        out.append(FIVE_ROUND_NOTE.format(rounds_run=rounds_run))
    out.append("")
    out.append("## Remaining differences")
    out.append("")
    out.append(differences_rewritten)
    if missed_rejections.strip().lower() != _MISSED_NONE:
        out.append("")
        out.append("## Missed rejections")
        out.append("")
        out.append(missed_rejections)
    out.append("")
    out.append("## All files")
    out.append("")
    out.append(f"- Problem:             {workdir_display}/problem.md")
    out.append(
        f"- {controller_name}'s final plan: "
        f"{workdir_display}/plan-{controller_slug}.md"
    )
    out.append(
        f"- {participant_name}'s final plan: "
        f"{workdir_display}/plan-{participant_slug}.md"
    )
    out.append("- Round snapshots:     plan-a-round-N.md, plan-b-round-N.md")
    out.append("- Rejection notes:     rejections-a-round-N.md, rejections-b-round-N.md")
    out.append("- Judge assessments:   judge-round-N.md (one per round)")
    out.append(f"- This summary:        {workdir_display}/summary.md")
    out.append("")
    return "\n".join(out)


# --------------------------------------------------------------------------- #
# run loop — context, round 0 / critique dispatch, refinement loop, execute
# --------------------------------------------------------------------------- #
def render_argv(spec: RoleSpec, values: Mapping[str, object]) -> list[str]:
    """Render a role's argv template by substituting ``⟪name⟫`` markers per element.

    Reuses :func:`render_template`, so an argv element with an unresolved marker
    fails loud rather than spawning a half-substituted command line.
    """
    return [render_template(part, values) for part in spec.command]


# The critique/init companion templates carry one ``## <heading>`` section per role plus
# shared sections. The engine dispatches each role as its OWN subprocess, so it must hand
# each one ONLY its own section — a combined prompt has no signal telling agent B it is
# agent B. ``select_role_section`` reproduces v1's per-role tailoring.
_ROLE_HEADINGS = {"agent_a": "Agent A", "agent_b": "Agent B", "judge": "Judge"}
_ALL_ROLE_HEADINGS = frozenset(_ROLE_HEADINGS.values())
_SECTION_RE = re.compile(r"^## (.+)$", re.MULTILINE)


def select_role_section(template: str, role_heading: str) -> str:
    """Return the prompt for one role: preamble + shared sections + the role's own.

    The other roles' ``## <heading>`` sections are dropped so a role never sees the
    instructions (or the judge rubric) meant for another. Non-role ``##`` sections are shared
    and kept for everyone, with the kept slices preserved verbatim. Raises
    :class:`PlanDuelError` when the requested role's section is absent.
    """
    matches = list(_SECTION_RE.finditer(template))
    if not matches:
        return template  # no sections — the whole template is the prompt
    out = [template[: matches[0].start()]]  # preamble (incl. its trailing newlines)
    found = False
    for i, match in enumerate(matches):
        heading = match.group(1).strip()
        start = match.start()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(template)
        chunk = template[start:end]
        if heading in _ALL_ROLE_HEADINGS:
            if heading == role_heading:
                out.append(chunk)
                found = True
            # a different role's section — drop it
        else:
            out.append(chunk)  # shared section (kept for every role)
    if not found:
        raise PlanDuelError(
            f"companion template has no '## {role_heading}' section for the role"
        )
    return "".join(out)


# Device names Windows reserves at EVERY directory level, matched case-insensitively and
# regardless of extension. A duel whose problem statement slugifies to one of these would
# fail at `mkdir` with an uncaught OSError before doing anything — on the one platform none
# of us develops on, which is why the guard belongs in the code.
_WINDOWS_RESERVED_NAMES = (
    frozenset({"con", "prn", "aux", "nul"})
    | frozenset(f"com{d}" for d in "123456789")
    | frozenset(f"lpt{d}" for d in "123456789")
)


def problem_slug(text: str, *, max_words: int = 4) -> str:
    """Kebab-case slug from a problem statement (v1's auto-workdir naming).

    A slug landing on a Windows reserved device name is suffixed rather than rejected: the
    statement is the user's and the name is an accident of the platform, so the run continues
    under a name that works. `CON` becomes `con-duel`.
    """
    _stop = {
        "the", "a", "an", "for", "to", "of", "and", "or", "in", "on", "with",
        "support", "using", "via",
    }
    words = re.findall(r"[a-z0-9]+", text.lower())
    meaningful = [w for w in words if w not in _stop] or words
    slug = "-".join(meaningful[:max_words]) or "duel"
    # The stem is what Windows matches, so `con.md` is reserved exactly as `con` is.
    if slug.split(".")[0] in _WINDOWS_RESERVED_NAMES:
        slug = f"{slug}-duel"
    return slug


def _round_context(workdir: Path, round_n: int) -> str:
    """The v1 'round context' sentence for a critique round's agent prompt.

    Three cases, not two. The unreadable-prior-score path fell through to round 1's sentence,
    so an agent at round 5 was told "This is the first critique round" while the rest of its
    prompt said round 5 — inviting it to discard four rounds of critique. A missing score is
    missing information, not a fresh start.
    """
    if round_n <= 1:
        return "This is the first critique round."
    prior = workdir / f"judge-round-{round_n - 1}.md"
    if prior.is_file():
        score = parse_score(read_text_tolerant(prior))
        if score is not None:
            return f"The judge scored convergence at {score}/10 last round."
    return (
        f"This is critique round {round_n}. Last round's convergence score could not be "
        f"read, so treat it as unknown — not as a fresh start."
    )


@dataclass
class DuelContext:
    """Carries the per-run identity + template context for prompt/argv rendering."""

    workdir: Path
    controller_name: str
    participant_name: str
    skill_dir: Path | None = None
    # Set once (post-construction) at run start; the elapsed-time source for
    # progress.log lines. ``init=False`` keeps the positional constructor unchanged.
    started_monotonic: float | None = field(default=None, init=False)
    # Memoized ⟪schema_path⟫ / ⟪schema_json⟫ pair; ``None`` = not yet resolved. The
    # schema is read once per run rather than once per dispatch.
    _schema_values: dict[str, str] | None = field(default=None, init=False)
    # Distinct degradation warnings already printed — see _warn_degraded.
    _degraded_warnings: set[str] = field(default_factory=set, init=False)

    def schema_values(self) -> dict[str, str]:
        """The shipped judge schema's two argv forms (see :func:`schema_placeholder_values`).

        ``{}`` when no schema is available — the placeholders are then simply absent
        from the render values, so an adapter that does not use them is unaffected
        and one that does is caught by :func:`preflight_schema`.
        """
        if self._schema_values is None:
            self._schema_values = schema_placeholder_values(self.skill_dir)
        return self._schema_values

    def base_values(self) -> dict[str, object]:
        values: dict[str, object] = {
            "workdir": str(self.workdir),
            "controller_name": self.controller_name,
            "participant_name": self.participant_name,
            "controller_slug": slugify_name(self.controller_name),
            "participant_slug": slugify_name(self.participant_name),
        }
        values.update(self.schema_values())
        return values

    def values(self, *, round_n: int, prompt: object) -> dict[str, object]:
        prior = max(round_n - 1, 0)
        vals = self.base_values()
        vals.update(
            {
                "round": round_n,
                "round_context": _round_context(self.workdir, round_n),
                "prompt": prompt,
                "frozen_a": str(self.workdir / plan_snapshot_name("a", prior)),
                "frozen_b": str(self.workdir / plan_snapshot_name("b", prior)),
            }
        )
        return vals

    def prompt(self, role: str, round_n: int) -> str:
        """Render this ROLE's prompt from the companion template, or a fallback.

        The companion template (``init.md`` for round 0, ``round.md`` for a critique round)
        carries one ``## <heading>`` section per role, and the engine dispatches each role as
        its own subprocess, so this returns ONLY the requested role's section plus the shared
        preamble. Falls back to a generic prompt when no ``skill_dir`` / template is
        available.
        """
        template_name = "init.md" if round_n == 0 else "round.md"
        fallback = f"[plan-duel] role={role} round={round_n}: produce the required artifact."
        if self.skill_dir is None:
            self._warn_degraded(
                f"no --skill-dir, so {role} round {round_n} is being dispatched with a "
                f"one-line placeholder prompt instead of the {template_name} template")
            return fallback
        path = Path(self.skill_dir) / template_name
        if not path.is_file():
            self._warn_degraded(
                f"{path} is missing, so {role} round {round_n} is being dispatched with "
                f"a one-line placeholder prompt")
            return fallback
        text = read_text_normalized(path)
        role_heading = _ROLE_HEADINGS.get(role)
        if role_heading is not None:
            try:
                text = select_role_section(text, role_heading)
            except PlanDuelError:
                # Falling back to the WHOLE template is the one degradation that must never
                # happen: it carries every role's section, so a competing agent would receive
                # the JUDGE's — the rubric it is about to be scored against. Degrade to the
                # placeholder instead: strictly less information, never more, which is the
                # only safe direction for a degradation to run.
                self._warn_degraded(
                    f"{template_name} has no '{role_heading}' section, so {role} round "
                    f"{round_n} is being dispatched with a one-line placeholder prompt. "
                    f"The whole template is NOT sent: it contains the judge's rubric.")
                return fallback
        try:
            return render_template(text, self.values(round_n=round_n, prompt=""))
        except TemplateError as exc:
            self._warn_degraded(
                f"{template_name} failed to render ({exc}), so {role} round {round_n} is "
                f"being dispatched with a one-line placeholder prompt")
            return fallback

    def _warn_degraded(self, detail: str) -> None:
        """Say it, once per distinct message, on stderr.

        Every one of these paths used to be silent, in a tool whose next action is to spend a
        paid model call: a duel that produced nothing useful because the prompt was twelve
        words looked exactly like one whose agents were unhelpful, and cost the same.
        De-duplicated because these fire per role per round and a repeated line teaches a
        reader to skim.
        """
        message = f"Warning: {detail}"
        if message in self._degraded_warnings:
            return
        self._degraded_warnings.add(message)
        sys.stderr.write(message + "\n")


def _dispatch_agent(
    role: str,
    specs: Mapping[str, RoleSpec],
    values: Mapping[str, object],
    output_file: Path,
    *,
    ctx: DuelContext,
    side: str,
    round_n: int,
    workdir: Path,
    timeout: float | None,
    status_to: Path | None = None,
) -> Path:
    """Render + run one agent role, honoring its cwd anchor and capture policy.

    The blocking child call is wrapped in a heartbeat so a multi-minute spawn keeps
    reporting liveness to ``progress.log``.
    """
    spec = specs[role]
    argv = render_argv(spec, values)
    cwd = workdir if spec.cwd == "workdir" else None
    with _heartbeat(ctx, round_n, f"working on Plan {side.upper()}"):
        return run_agent(
            argv,
            output_file,
            side=side,
            round_n=round_n,
            cwd=cwd,
            status_to=status_to,
            timeout=timeout,
        )


def _clear_agent_output(output_file: Path, *, side: str, round_n: int) -> None:
    """Delete the live plan file so this round's dispatch has to create it anew.

    Without this, ``run_agent``'s validation gate is satisfied by the PREVIOUS round's plan —
    a real, readable, long-enough file that tells the gate nothing. A CLI exiting 0 having
    written nothing (a refusal, a context overflow, a tool-permission denial) leaves last
    round's file untouched, and the engine snapshots it as this round's revision.

    Deleting first turns "the file exists" into "this dispatch created it" WITHOUT comparing
    hashes: an agent that legitimately rewrites a plan byte-identically is converged, not
    failed. Scoped to the LIVE ``plan-{a,b}.md``; the round-(N-1) snapshots are never touched.
    """
    try:
        output_file.unlink()
    except FileNotFoundError:
        return  # nothing to clear — already the state we want
    except OSError as exc:
        # Could not clear it, so a surviving file after the dispatch would prove
        # nothing. Halt with the round's own message rather than run blind.
        raise AgentOutputError(
            agent_failure_message(side, round_n),
            cause=f"could not clear {output_file.name} before the dispatch: {exc}",
        ) from exc


def _snapshot_agent_plan(
    source: Path, destination: Path, *, side: str, round_n: int
) -> None:
    """Copy a validated live plan to its round snapshot, halting the way its agent would.

    The copy is where the check-to-copy race actually lands. ``run_agent`` validates the file
    and returns; between that and this it can be removed, replaced or have its permissions
    changed, and an untranslated ``copy_bytes`` then raises a bare ``FileNotFoundError`` from
    outside the diagnostic path. No amount of pre-checking removes a TOCTOU window, so the
    failure is translated where it happens instead.
    """
    try:
        copy_bytes(source, destination)
    except OSError as exc:
        raise AgentOutputError(
            agent_failure_message(side, round_n),
            cause=f"could not snapshot {source.name}: {exc}",
        ) from exc


def _require_regular_file(output_file: Path, *, side: str, round_n: int) -> None:
    """The other half of "a NEWLY CREATED REGULAR file": reject anything but a file.

    :func:`_clear_agent_output` makes "it is here" mean "this dispatch created it". That is
    not yet "this dispatch wrote it": an agent that exits 0 after pointing ``plan-a.md`` at
    the frozen round-(N-1) snapshot hands back that snapshot, and a gate built on predicates
    that follow a symlink records it as this round's revision.

    ``lstat`` inspects the path itself rather than what it points at, which is the whole
    difference. This stays beside the clearing because it is the second half of its
    guarantee: cleared-then-present proves the file is new, and this proves it is a file.
    """
    try:
        mode = os.lstat(output_file).st_mode
    except OSError as exc:  # vanished between the dispatch and here
        raise AgentOutputError(
            agent_failure_message(side, round_n),
            cause=f"could not inspect {output_file.name}: {exc}",
        ) from exc
    if not stat.S_ISREG(mode):
        raise AgentOutputError(
            agent_failure_message(side, round_n),
            cause=f"{output_file.name} is not a regular file",
        )


def _dispatch_judge(
    specs: Mapping[str, RoleSpec],
    values: Mapping[str, object],
    message_path: Path,
    *,
    ctx: DuelContext,
    workdir: Path,
    round_n: int,
    timeout: float | None,
) -> str:
    """Render + run the judge, capturing its CLEAN final message only.

    ``stdout == "clean-last-message"`` means the engine redirects the CLI's clean stdout into
    ``message_path``; ``stdout == "file"`` means the CLI writes it itself. Either way the
    score is parsed from the file, never a raw transcript. An empty/failed judge process
    raises :class:`JudgeOutputError` — never swallowed.
    """
    spec = specs["judge"]
    argv = render_argv(spec, values)
    cwd = workdir if spec.cwd == "workdir" else None
    redirect = spec.stdout == "clean-last-message"
    with _heartbeat(ctx, round_n, "judging"):
        return capture_judge_message(
            argv,
            message_path,
            cwd=cwd,
            redirect_stdout=redirect,
            round_n=round_n,
            timeout=timeout,
        )


HEARTBEAT_INTERVAL_SECONDS = 15.0
HEARTBEAT_JOIN_TIMEOUT_SECONDS = 2.0


def _elapsed_label(ctx: DuelContext) -> str:
    """Return ``+MM:SS`` since the run started, or ``+00:00`` if unset.

    ``started_monotonic`` is ``None`` for a hand-built ``DuelContext`` (e.g. a direct
    unit test); treat that as zero elapsed rather than doing arithmetic on ``None``.
    """
    started = ctx.started_monotonic
    if started is None:
        return "+00:00"
    seconds = max(0, int(time.monotonic() - started))
    minutes, secs = divmod(seconds, 60)
    return f"+{minutes:02d}:{secs:02d}"


def _append_progress_log_line(ctx: DuelContext, text: str) -> None:
    """Best-effort append one timestamped line to the run-level ``progress.log``.

    Observation-only: a filesystem error is SWALLOWED so a failed activity write can never
    abort a correct duel. The full ``summary.md`` is deliberately never written here.

    ``PlanDuelError`` alongside ``OSError`` because :func:`append_progress` refuses a
    ``progress.log`` planted as a symlink, and that refusal is not an ``OSError``. Refusing
    the write is right; failing the duel over it is not.
    """
    try:
        append_progress(
            ctx.workdir / PROGRESS_LOG_NAME, f"[{_elapsed_label(ctx)}] {text}\n"
        )
    except (OSError, PlanDuelError):
        pass


def _progress(ctx: DuelContext, round_n: int, message: str) -> None:
    """Append one non-blocking progress line at an agent/judge spawn point.

    Writes to BOTH observation channels, each best-effort (never raises):
      * the per-round ``participant-progress-N.md`` file — byte-for-byte the v1 line
        content, kept for resume-cleanup compat;
      * the run-level ``progress.log`` — the same message with an elapsed-time prefix and a
        ``round N`` tag, the canonical tail-able channel for any controller.

    Read by NOTHING on the correctness path, so the duel's outcome is identical whether or
    not anyone watches it. Both writes swallow ``PlanDuelError`` beside ``OSError``.
    """
    try:
        append_progress(
            ctx.workdir / f"participant-progress-{round_n}.md", message + "\n"
        )
    except (OSError, PlanDuelError):
        pass
    # ``message`` already begins with ``round N:`` — pass it through as-is (no extra
    # round tag) so the log line reads ``[+MM:SS] round N: <detail>``, not a doubled
    # ``round N  round N:``. Heartbeat/terminator lines add their own tag below.
    _append_progress_log_line(ctx, message)


@contextlib.contextmanager
def _heartbeat(ctx: DuelContext, round_n: int, label: str):
    """Emit a liveness line to ``progress.log`` every N seconds around a blocking spawn.

    A blocking child call can run for minutes with no output; the heartbeat writes
    ``still <label>`` on an interval so a watcher can tell the duel is alive, not hung. The
    first tick is one full interval in, so a fast child produces no noise.

    Teardown must never hang the duel: ``finally`` signals the thread and then does a BOUNDED
    ``join`` — ``daemon=True`` only protects interpreter exit, and an unbounded join on a
    thread stuck in filesystem I/O would block the main thread forever.
    """
    stop = threading.Event()

    def _beat() -> None:
        while not stop.wait(HEARTBEAT_INTERVAL_SECONDS):
            _append_progress_log_line(ctx, f"round {round_n}: still {label}")

    thread = threading.Thread(target=_beat, name="plan-duel-heartbeat", daemon=True)
    thread.start()
    try:
        yield
    finally:
        stop.set()
        thread.join(timeout=HEARTBEAT_JOIN_TIMEOUT_SECONDS)


def run_init_round(
    *,
    workdir: Path,
    specs: Mapping[str, RoleSpec],
    ctx: DuelContext,
    emit,
    timeout: float | None,
    state: RunState,
    reuse_plan_a: bool = False,
) -> None:
    """Round 0 (init.md): generate both initial plans, snapshot, print status.

    Honors the round-0 Agent-B fallback: a short/missing ``plan-b.md`` triggers
    :func:`recover_agent_b_round0`; only if that finds nothing does the halt propagate.

    Plan A is snapshotted the instant it validates rather than once BOTH plans land, so a
    round 0 that dies at Agent B leaves behind an engine-vouched Plan A. ``reuse_plan_a``
    then restores it and re-runs Agent B alone.
    """
    plan_a = workdir / "plan-a.md"
    plan_b = workdir / "plan-b.md"
    snapshot_a = workdir / plan_snapshot_name("a", 0)
    if reuse_plan_a:
        copy_bytes(snapshot_a, plan_a)
        _progress(ctx, 0, "round 0: reusing validated Plan A; generating Plan B")
    else:
        _progress(ctx, 0, "round 0: generating Plan A")
        _dispatch_agent(
            "agent_a",
            specs,
            ctx.values(round_n=0, prompt=ctx.prompt("agent_a", 0)),
            plan_a,
            ctx=ctx,
            side="a",
            round_n=0,
            workdir=workdir,
            timeout=timeout,
        )
        # Snapshot BEFORE Agent B runs: this is what makes a failed round 0 resumable.
        _snapshot_agent_plan(plan_a, snapshot_a, side="a", round_n=0)
        _progress(ctx, 0, "round 0: Plan A written; generating Plan B")
    try:
        _dispatch_agent(
            "agent_b",
            specs,
            ctx.values(round_n=0, prompt=ctx.prompt("agent_b", 0)),
            plan_b,
            ctx=ctx,
            side="b",
            round_n=0,
            workdir=workdir,
            timeout=timeout,
            status_to=workdir / "participant-round-0-status.md",
        )
    except AgentOutputError:
        recovered = recover_agent_b_round0(workdir)
        if recovered is None:
            raise
        emit(recovered)
    _progress(ctx, 0, "round 0: Plan B written")

    # Plan A was snapshotted at validation time (above); only B remains.
    _snapshot_agent_plan(plan_b, workdir / plan_snapshot_name("b", 0), side="b", round_n=0)
    a_words = word_count_cell(word_count_file(workdir / plan_snapshot_name("a", 0)))
    b_words = word_count_cell(word_count_file(workdir / plan_snapshot_name("b", 0)))
    emit(
        f"Round 0 complete — initial plans written | "
        f"A: {a_words} words, B: {b_words} words"
    )
    state.rounds[0] = RoundState(plans_snapshotted=True)
    save_state(workdir, state)


def run_critique_round(
    *,
    workdir: Path,
    round_n: int,
    specs: Mapping[str, RoleSpec],
    ctx: DuelContext,
    emit,
    timeout: float | None,
    state: RunState,
) -> str:
    """One critique round (round.md): freeze inputs, run both agents + the judge.

    ``freeze_round_inputs`` snapshots the round's inputs (N ≥ 1 only) so the serialized
    agents match v1's simultaneous semantics — the second agent cannot read the first's
    fresh revision. Returns the judge's message text.

    Each live plan is cleared immediately before ITS OWN writing dispatch, never earlier,
    and order matters twice: clearing must follow ``freeze_round_inputs``, which creates a
    missing snapshot by copying the live plan; and Plan B is cleared only after Agent A
    finishes, so a halt on A leaves B's last good revision on disk.
    """
    freeze_round_inputs(workdir, round_n)
    plan_a = workdir / "plan-a.md"
    plan_b = workdir / "plan-b.md"
    _progress(ctx, round_n, f"round {round_n}: critiquing Plan A")
    _clear_agent_output(plan_a, side="a", round_n=round_n)
    _dispatch_agent(
        "agent_a",
        specs,
        ctx.values(round_n=round_n, prompt=ctx.prompt("agent_a", round_n)),
        plan_a,
        ctx=ctx,
        side="a",
        round_n=round_n,
        workdir=workdir,
        timeout=timeout,
    )
    _require_regular_file(plan_a, side="a", round_n=round_n)
    _progress(ctx, round_n, f"round {round_n}: critiquing Plan B")
    _clear_agent_output(plan_b, side="b", round_n=round_n)
    _dispatch_agent(
        "agent_b",
        specs,
        ctx.values(round_n=round_n, prompt=ctx.prompt("agent_b", round_n)),
        plan_b,
        ctx=ctx,
        side="b",
        round_n=round_n,
        workdir=workdir,
        timeout=timeout,
        status_to=workdir / f"participant-round-{round_n}-status.md",
    )
    _require_regular_file(plan_b, side="b", round_n=round_n)
    _snapshot_agent_plan(
        plan_a, workdir / plan_snapshot_name("a", round_n), side="a", round_n=round_n
    )
    _snapshot_agent_plan(
        plan_b, workdir / plan_snapshot_name("b", round_n), side="b", round_n=round_n
    )

    judge_path = workdir / f"judge-round-{round_n}.md"
    _progress(ctx, round_n, f"round {round_n}: judging")
    # Record that this round's judge has STARTED, before it has. judge_needs_rerun
    # documents "the file exists but state.json says that round's judge never completed"
    # as one of its two re-run conditions — and that state was never written anywhere, so
    # the condition could not fire. Every RoundState for a critique round was created
    # after the judge returned, with judge_completed=True.
    #
    # What that cost: a judge killed partway through leaves a FRAGMENT on disk and no
    # marker at all, so the file is present, non-empty and unmarked — which
    # judge_needs_rerun reads as a complete verdict and trusts. Writing the marker first
    # makes the interrupted case observable, which is what the docstring always claimed.
    state.rounds[round_n] = RoundState(plans_snapshotted=True, judge_completed=False)
    save_state(workdir, state)
    judge_text = _dispatch_judge(
        specs,
        ctx.values(round_n=round_n, prompt=ctx.prompt("judge", round_n)),
        judge_path,
        ctx=ctx,
        workdir=workdir,
        round_n=round_n,
        timeout=timeout,
    )
    _progress(ctx, round_n, f"round {round_n}: judged")
    state.rounds[round_n] = RoundState(
        plans_snapshotted=True,
        judge_completed=True,
        score=parse_score(judge_text),
    )
    save_state(workdir, state)
    return judge_text


def run_duel(
    *,
    workdir: Path,
    specs: Mapping[str, RoleSpec],
    ctx: DuelContext,
    start_round: int,
    emit,
    timeout: float | None,
    state: RunState,
) -> tuple[int, str]:
    """Run the refinement loop from ``start_round`` to 10; return (rounds_run, stop).

    Scores for rounds already completed before ``start_round`` (a resume) are
    preloaded from the on-disk judge files so the stagnation window looks back
    correctly. Returns the round the loop stopped at and the stop label.
    """
    scores: dict[int, int] = {}
    for n in range(1, start_round):
        judge_path = workdir / f"judge-round-{n}.md"
        judge_text = read_text_tolerant(judge_path) if judge_path.is_file() else ""
        parsed = parse_score(judge_text) if judge_text else None
        # Only the LAST completed round can be re-judged: `round.md` sends the judge to the
        # LIVE plan-a.md / plan-b.md, which `apply_resume` restores from the last completed
        # round's snapshots. Re-judging round N-1 would score round N's plans and file the
        # verdict under the wrong round.
        #
        # Not `parsed is None and ...`: that asks judge_needs_rerun only when the score
        # fails to parse, so a verdict truncated by a kill is trusted whenever its `SCORE:`
        # line survives, and its missing `PREFERRED:` defaults the winner to A.
        # judge_needs_rerun answers this correctly from state.json and is conservative in
        # the other direction, so asking it first costs nothing.
        if n == start_round - 1 and judge_needs_rerun(workdir, n, state):
            # The round is COMPLETE — both plan snapshots exist, which is what made it one
            # — so its score is a real number that was never written down, not a zero.
            # Scoring it 0 rewrites the trajectory: it can fire stagnation that never
            # happened or suppress a convergence that did, and the duel then reports a
            # different winner than the same duel run start to finish.
            judge_text = _rejudge_round(
                workdir=workdir,
                round_n=n,
                specs=specs,
                ctx=ctx,
                emit=emit,
                timeout=timeout,
                state=state,
            )
            parsed = parse_score(judge_text) if judge_text else None
        # v1's score(N) convention survives for a judge still unparseable after the re-run:
        # it counts as 0 and logs the warning. Every round in 1..start_round-1 MUST land in
        # ``scores`` — the exit-check indexes every round, so a skipped entry would raise
        # KeyError rather than reproduce v1's treat-as-0 behavior.
        if parsed is None:
            emit(score_warning(judge_text, n))
            parsed = 0
        scores[n] = parsed

    # Ask the exit question BEFORE running anything, and of EVERY round already on disk
    # rather than only the last. An uninterrupted duel asks after each round and stops at the
    # first that fires, so replaying round by round is what reproduces that answer; asking
    # only about the newest round walks past the round where the duel was over.
    #
    # Deliberately ABOVE the max-rounds guard below: a duel interrupted after round 10
    # resumes at 11, and its real exit may be Convergence, which `evaluate_exit` reports only
    # because it checks convergence before the cap.
    last_round = min(start_round - 1, MAX_ROUNDS)
    for round_n in range(1, last_round + 1):
        decision = evaluate_exit(round_n, [scores[i] for i in range(1, round_n + 1)])
        if decision.stop:
            emit(decision.message)
            if round_n < start_round - 1:
                # The duel ended here, but `apply_resume` restored the live plans from the
                # workdir's NEWEST complete round, which can only exist because an earlier
                # run kept going past this exit. `write_summary` publishes the LIVE plans,
                # so without this the summary would say "stopped at round N" while handing
                # over a later round's plans.
                for side in ("a", "b"):
                    snapshot = workdir / plan_snapshot_name(side, round_n)
                    if snapshot.is_file():
                        copy_bytes(snapshot, workdir / f"plan-{side}.md")
                emit(
                    f"Rounds after {round_n} are on disk but the duel had already stopped "
                    f"there; publishing round {round_n}'s plans."
                )
            return round_n, decision.stopped_due_to

    if start_round > MAX_ROUNDS:
        return MAX_ROUNDS, MAX_ROUNDS_LABEL

    for round_n in range(start_round, MAX_ROUNDS + 1):
        emit(f"### Round {round_n} of up to 10")
        judge_text = run_critique_round(
            workdir=workdir,
            round_n=round_n,
            specs=specs,
            ctx=ctx,
            emit=emit,
            timeout=timeout,
            state=state,
        )
        parsed = parse_score(judge_text)
        if parsed is None:
            emit(score_warning(judge_text, round_n))
            parsed = 0
        scores[round_n] = parsed
        a_words = word_count_cell(
            word_count_file(workdir / plan_snapshot_name("a", round_n)))
        b_words = word_count_cell(
            word_count_file(workdir / plan_snapshot_name("b", round_n)))
        emit(
            f"Round {round_n} complete — score {parsed}/10 | "
            f"A: {a_words} words, B: {b_words} words"
        )
        ordered = [scores[i] for i in range(1, round_n + 1)]
        decision = evaluate_exit(round_n, ordered)
        if decision.stop:
            emit(decision.message)
            return round_n, decision.stopped_due_to

    return MAX_ROUNDS, MAX_ROUNDS_LABEL


def judge_needs_rerun(
    workdir: Path, round_n: int, state: "RunState | None"
) -> bool:
    """Whether round ``round_n``'s judge verdict has to be produced again.

    Two states qualify, and only two:

    * the file is **missing or empty** — the judge never wrote it;
    * the file exists but ``state.json`` says that round's judge never completed — an
      interrupted write, so whatever is on disk is a fragment.

    A verdict that is present, non-empty and marked complete is **not** re-run, even when its
    score will not parse: that verdict is the real one, and its ``PREFERRED:`` line may still
    name a winner. Without a ``state.json`` to consult a present verdict is likewise left
    alone — the engine cannot tell a truncated write from a genuinely unparseable one.
    """
    path = workdir / f"judge-round-{round_n}.md"
    if not path.is_file() or file_size_bytes(path) == 0:
        return True
    marker = state.rounds.get(round_n) if state is not None else None
    return marker is not None and not marker.judge_completed


def _rejudge_round(
    *,
    workdir: Path,
    round_n: int,
    specs: Mapping[str, RoleSpec],
    ctx: DuelContext,
    emit,
    timeout: float | None,
    state: RunState,
) -> str:
    """Re-run the judge for a completed round whose judge file is missing or unusable.

    **The target is cleared first.** An interruption can leave a half-written
    ``judge-round-N.md`` behind, and for an adapter that writes the file itself nothing else
    removes it, so a judge that then failed to write has its stale bytes read back as this
    round's verdict.

    A re-run that fails does NOT halt the duel: it degrades to a score of 0 and says so. So
    the except below is :class:`PlanDuelError` rather than a list of three — a list naming
    ``JudgeOutputError``, ``ProcessError`` and ``OSError`` misses :class:`TemplateError`,
    which an unresolved ``⟪schema_json⟫`` marker raises on a resume past the round cap.
    """
    judge_path = workdir / f"judge-round-{round_n}.md"
    emit(
        f"Round {round_n} is complete but its judge verdict is missing or unreadable — "
        f"re-judging that round rather than scoring it 0."
    )
    try:
        # `FileNotFoundError` is the EXPECTED case — the file is missing, which is half of
        # why we are here. Any other `OSError` means the stale file is still on disk, and an
        # adapter that writes the file itself would have those bytes read back as this
        # round's verdict, so it degrades below rather than being suppressed.
        with contextlib.suppress(FileNotFoundError):
            judge_path.unlink()
        judge_text = _dispatch_judge(
            specs,
            ctx.values(round_n=round_n, prompt=ctx.prompt("judge", round_n)),
            judge_path,
            ctx=ctx,
            workdir=workdir,
            round_n=round_n,
            timeout=timeout,
        )
    except (PlanDuelError, OSError) as exc:
        # PlanDuelError is the base of JudgeOutputError and ProcessError, so this is the
        # old tuple plus every other deliberate failure — TemplateError above all.
        emit(f"Re-judging round {round_n} failed ({exc}); its score falls back to 0.")
        return ""
    # The round's plans were already snapshotted — that is what made it a completed round
    # — and its judge has now finished, so record both. Without this a later resume reads
    # a marker saying this judge never completed, over a judge file that is now real.
    state.rounds[round_n] = RoundState(
        plans_snapshotted=True,
        judge_completed=True,
        score=parse_score(judge_text),
    )
    # The verdict is already on disk and parsed; the marker is bookkeeping. An unwritable
    # state.json must not throw away a recovery that has already succeeded.
    try:
        save_state(workdir, state)
    except OSError as exc:
        emit(f"Could not record round {round_n}'s recovered judge in state.json ({exc}).")
    return judge_text


def _stamp_winner(
    workdir: Path, winner_file: str, written_finals: set[Path], emit
) -> None:
    """Stamp the winning plan with the v2 markers — but ONLY a file this run wrote.

    Read-modify-write of a file the AGENT wrote, so the decode has to round-trip: the stamp
    adds rows and must not rewrite bytes it never looked at.

    ``written_finals`` is what makes this safe, and the path's existence is not. A missing
    live plan is a warned SKIP rather than a halt, so execution reaches here with whatever
    was already at ``winner_file`` — and a previous run's ``plan-{slug}.md`` is exactly that.
    Stamping it would write ``Format: v2`` into an older plan and have ``summary.md`` present
    it as this duel's winner.

    Every remaining failure is a warning, never a halt: the stamp is a decoration, and losing
    the whole summary over it trades something valuable for something cosmetic.
    """
    winner_path = workdir / winner_file
    # Compared as PATHS. Both sides are built from the same ``workdir`` and the same
    # slug, so they are equal exactly when the copy that wrote this file succeeded — no
    # basename fragment to disagree over.
    if winner_path not in written_finals:
        emit(
            f"Warning: {winner_file} was not written by this run, so it is NOT being "
            f"stamped as the winner. Any file at that path is left exactly as it was "
            f"and is not this duel's output — read the round snapshots instead."
        )
        return

    try:
        stamped = stamp_winner_plan(read_text_roundtrip(winner_path))
    except OSError as exc:
        sys.stderr.write(
            f"Warning: could not stamp the winning plan {winner_path}: {exc}. "
            f"The summary below is complete; the plan file is not marked.\n"
        )
        return
    try:
        write_text_roundtrip(winner_path, stamped)
    # PlanDuelError beside OSError: the write REFUSES a winner path standing as a
    # symlink, and that refusal is not an OSError. Refusing is right; losing the
    # summary over a decoration is not — which is what this whole function says.
    except (OSError, PlanDuelError) as exc:
        sys.stderr.write(
            f"Warning: could not write the stamp back to {winner_path}: {exc}. "
            f"The summary below is complete; the plan file is not marked.\n"
        )


def write_summary(
    *,
    workdir: Path,
    rounds_run: int,
    stopped_due_to: str,
    controller_name: str,
    participant_name: str,
    emit,
) -> Path:
    """Assemble + write ``summary.md`` and print it (the Step 3 orchestrator).

    Extracts the final judge fields, resolves the winner, renames the live plans to their
    slugged names, stamps ONLY the winner with the v2 markers, builds the score-trajectory
    table, and applies the scoped A/B → name rewrite to the differences block.

    **Nothing missing from the workdir stops it.** This runs after the duel has been paid
    for, so every read here degrades and says so: an absent final judge yields empty fields,
    an absent snapshot a ``—`` word count, an absent live plan a warned skip. Each of those
    was once a bare ``FileNotFoundError`` that threw away a completed duel.
    """
    controller_slug = slugify_name(controller_name)
    participant_slug = slugify_name(participant_name)

    # The final judge file is normally written by the loop, but a resume of a round-10 duel
    # interrupted before its judge (snapshots are written first) reaches here with it absent.
    # Guard the read like every other judge read and degrade to empty fields — v1 treats a
    # missing final score as 0 and still emits summary.md.
    judge_path = workdir / f"judge-round-{rounds_run}.md"
    judge_text = ""
    if judge_path.is_file():
        judge_text = read_text_tolerant(judge_path)
    else:
        # No file at all, so there is no number to name — score_warning yields the
        # unparseable form here. An out-of-range score was already warned about by the
        # round that produced it (or by the resume preload).
        emit(score_warning("", rounds_run))
    fields = extract_judge_fields(judge_text)
    if fields.preferred is None:
        # Two different failures, and only one is fixable by editing that line. A single
        # "no parseable PREFERRED line" for both told a user whose judge DID write a
        # preference that it had written none — while the winner quietly became A.
        unreadable = read_preferred_marker(judge_text).unreadable
        if unreadable is not None:
            emit(
                f"Warning: round {rounds_run}'s preference line names no side this "
                f"engine will act on ({unreadable!r}) — it must give a bare A or B, "
                f"optionally followed by an explanation. The winner was NOT read from "
                f"that line; falling back to A ({controller_name}) so the summary is "
                f"still written. If that is wrong, correct the line in "
                f"judge-round-{rounds_run}.md, delete summary.md, and resume this "
                f"workdir."
            )
        else:
            emit(
                f"Warning: no parseable PREFERRED line at round {rounds_run} — "
                f'defaulting the winner to A ({controller_name})'
            )
    winner_name, winner_file = resolve_winner(
        fields.preferred or "A", controller_name, participant_name
    )

    # A live plan that is not there is a WARNED SKIP, not a halt — the same answer the
    # missing judge file above already gets. A bare `FileNotFoundError` here lands at the
    # last step of a duel already paid for: no summary, a raw traceback, every round of
    # model output on disk with nothing pointing at it. The final plan is a renamed copy of
    # a file the AGENT wrote; the summary is the engine's own product.
    # Keyed on the PATH, not on a basename. `resolve_winner` yields `plan-{slug}.md` while
    # this recorded `destination.name`; the two are equal only while a slug is a bare
    # filename component, so a slug carrying a separator copied successfully and then never
    # matched — the winner unstamped while the summary announced a v2 plan. A set keyed on a
    # fragment of a path is wrong on its own terms.
    written_finals: set[Path] = set()
    for side, slug in (("a", controller_slug), ("b", participant_slug)):
        source = workdir / f"plan-{side}.md"
        destination = workdir / f"plan-{slug}.md"
        try:
            copy_bytes(source, destination)
        except OSError as exc:
            emit(
                f"Warning: could not copy {source.name} to {destination.name} ({exc}). "
                f"The summary below is complete; that final plan file was not written — "
                f"the round snapshots for side {side.upper()} are unaffected."
            )
        else:
            written_finals.add(destination)

    _stamp_winner(workdir, winner_file, written_finals, emit)

    # Word counts are Optional: a snapshot that is absent scores `—` rather than
    # raising. See :func:`word_count_file`.
    trajectory: list[tuple[int, int | None, int | None, int | None]] = []
    for n in range(0, rounds_run + 1):
        a_words = word_count_file(workdir / plan_snapshot_name("a", n))
        b_words = word_count_file(workdir / plan_snapshot_name("b", n))
        if n == 0:
            score: int | None = None
        else:
            judge_n = workdir / f"judge-round-{n}.md"
            parsed = (
                parse_score(read_text_tolerant(judge_n)) if judge_n.is_file() else None
            )
            score = 0 if parsed is None else parsed
        trajectory.append((n, score, a_words, b_words))

    summary_text = assemble_summary(
        workdir_display=str(workdir),
        rounds_run=rounds_run,
        stopped_due_to=stopped_due_to,
        controller_name=controller_name,
        participant_name=participant_name,
        controller_slug=controller_slug,
        participant_slug=participant_slug,
        winner_name=winner_name,
        winner_file=winner_file,
        trajectory=trajectory,
        justification=fields.justification,
        differences_rewritten=rewrite_differences(
            fields.differences, controller_name, participant_name
        ),
        missed_rejections=fields.missed_rejections,
    )
    summary_path = workdir / "summary.md"
    # ATOMIC, because this file's existence is what every later resume reads as "the
    # duel finished". A half-written one is indistinguishable from a complete one to
    # that check, and it would be printed as the result.
    write_text_atomic(summary_path, summary_text)
    emit(summary_text)
    return summary_path


def _write_completion_terminator(
    ctx: DuelContext, rounds_run: int, stopped_due_to: str, state: RunState
) -> None:
    """Best-effort final ``progress.log`` line marking the duel complete.

    The final score comes from ``state`` (the value the loop already tracked), applying
    v1's missing/unparseable → 0 convention; the full ``summary.md`` goes to ``emit``
    only, never here. Best-effort like every other activity write.
    """
    round_state = state.rounds.get(rounds_run)
    score = round_state.score if round_state is not None and round_state.score is not None else 0
    _append_progress_log_line(
        ctx, f"duel complete — exit={stopped_due_to} score={score} → summary.md"
    )


def _resolve_problem_statement(argument: str | None) -> str:
    """Resolve the new-run problem statement (file path → contents, else inline)."""
    if not argument:
        raise PlanDuelError(
            "No problem statement provided. Pass inline text or a file path."
        )
    candidate = Path(argument)
    if candidate.is_file():
        return read_text_normalized(candidate)
    return argument


def _looks_like_duel_workdir(path: Path) -> bool:
    """A directory THIS tool created — not merely one holding a file named problem.md.

    A resume DELETES ``plan-*.md``, ``judge-*.md``, ``rejections-*.md``, ``participant-*``
    and ``progress.log`` from the directory it is given. ``problem.md`` is an ordinary
    filename and is no evidence the directory is a duel, so pointing a resume at a notes
    directory holding one alongside its own ``plan-*.md`` drafts destroys them.

    Accepts either the marker written at claim time, or — for a workdir predating it —
    ``problem.md`` plus at least one artifact only a duel produces. ``plan-a.md`` and
    ``plan-b.md`` are NOT such an artifact: both are ordinary names a person writes by hand,
    and both are on the reset's deletion list. Every name below is one the engine alone
    emits, so a legacy workdir still resumes.
    """
    if (path / DUEL_MARKER_FILENAME).is_file():
        return True
    if not (path / "problem.md").is_file():
        return False
    if (path / STATE_FILENAME).is_file():
        return True
    scan = scan_snapshots(path)
    if scan.plan_a_rounds or scan.plan_b_rounds or scan.judge_rounds or scan.has_summary:
        return True
    return any(
        # PROGRESS_LOG_NAME is deliberately absent: a `progress.log` is a plausible
        # name in an ordinary notes directory, and the suite pins one as NOT a duel.
        fnmatch.fnmatchcase(entry.name, glob)
        for entry in _direct_child_files(path)
        for glob in _ENGINE_ONLY_GLOBS
    )


def _claim_problem_md(workdir: Path, problem: str) -> bool:
    """Write ``workdir/problem.md`` EXCLUSIVELY. ``False`` if it was already there.

    This file IS the reservation. A duel workdir is occupied exactly when it holds a
    ``problem.md``, so creating it with ``O_EXCL`` makes "claim the workdir" one atomic step
    the kernel arbitrates, and the loser of a race gets a refusal instead of interleaving its
    plans with the winner's.

    A separate lock file is the wrong alternative: it would outlive a crash, and the state a
    crashed duel leaves behind is precisely the state a resume has to be able to enter.

    Bytes match :func:`write_text_utf8`'s default exactly, because a resume reads this back.
    """
    body = problem if problem.endswith("\n") else problem + "\n"
    body = body.replace("\r\n", "\n").replace("\r", "\n")
    try:
        with open(workdir / "problem.md", "xb") as handle:
            handle.write(body.encode("utf-8"))
    except FileExistsError:
        return False
    # Best-effort: the claim above is what makes the workdir ours, and a read-only or
    # full filesystem must not turn a successful claim into a failure. Without the
    # marker the legacy test in _looks_like_duel_workdir still recognises the workdir
    # as soon as it holds a real artifact.
    try:
        (workdir / DUEL_MARKER_FILENAME).write_bytes(b"")
    except OSError:
        pass
    return True


def _resolve_new_workdir(workdir_arg: str | None, problem: str) -> Path:
    """RESERVE and return the new-run workdir: explicit ``--workdir``, or an auto slug.

    Creates the directory AND claims it by writing ``problem.md`` exclusively, so what comes
    back is a workdir this run owns. Only ever called on the NEW-RUN path.

    **The reservation is the point, not a detail.** Naming a free path and creating it later
    is check-then-create: two duels aimed at one directory both proceed and interleave their
    plans, and a symlink swapped in between redirects every write.

    ``mkdir(exist_ok=False)`` settles a directory that does not exist yet, and the loser takes
    the next suffix; an explicit ``--workdir`` naming an EMPTY existing directory is a
    documented workflow mkdir cannot arbitrate, so :func:`_claim_problem_md` covers both.
    ``mkdir`` also settles the dangling symlink for free: ``Path.exists()`` follows the link
    and reports *absent*, while ``mkdir`` sees the link itself and raises.
    """
    if workdir_arg:
        candidate = Path(workdir_arg)
        try:
            candidate.mkdir(parents=True, exist_ok=True)
        except FileExistsError as exc:
            # ``exist_ok`` forgives an existing DIRECTORY only, so this is a file or a
            # symlink pointing nowhere.
            raise PlanDuelError(
                f"--workdir {workdir_arg} already exists and is not a directory. "
                f"Choose a new path."
            ) from exc
        if any(candidate.iterdir()):
            raise PlanDuelError(
                f"--workdir {workdir_arg} is not empty. Refusing to start a new duel "
                f"over existing files — choose an empty or new directory. To RESUME "
                f"the duel in that directory, pass it as the positional argument "
                f"instead of --workdir."
            )
        if not _claim_problem_md(candidate, problem):
            raise PlanDuelError(
                f"--workdir {workdir_arg} was claimed by another duel between this run "
                f"finding it empty and starting in it. Choose a different directory."
            )
        return candidate
    slug = problem_slug(problem)
    base = Path("plans") / "duels"
    candidate = base / slug
    suffix = 2
    while True:
        try:
            candidate.mkdir(parents=True, exist_ok=False)
        except FileExistsError:
            candidate = base / f"{slug}-{suffix}"
            suffix += 1
            continue
        if _claim_problem_md(candidate, problem):
            return candidate
        # The directory is ours by mkdir but its problem.md is not; step aside rather
        # than share. Cannot happen with the mkdir above holding, and costs one suffix.
        candidate = base / f"{slug}-{suffix}"
        suffix += 1


def execute(
    *,
    argument: str | None = None,
    workdir_arg: str | None = None,
    specs: Mapping[str, RoleSpec],
    controller_name: str,
    participant_name: str,
    skill_dir: str | os.PathLike[str] | None = None,
    emit=print,
    timeout: float | None = None,
) -> int:
    """The end-to-end duel: resolve args, run/resume the loop, then write summary.

    Resume is chosen when ``argument`` — the POSITIONAL one, never ``--workdir`` — names an
    existing directory holding ``problem.md``; everything else is a new run. ``workdir`` is
    always resolved to an ABSOLUTE path before any dispatch so participant CLI
    ``{workdir}/…`` paths are cwd-independent.

    Because that resume test runs FIRST, a new run's workdir is by definition not a resume —
    so :func:`_resolve_new_workdir` refuses an explicit directory that already holds
    anything, rather than adding ``problem.md`` to someone else's files.
    """
    skill_dir_path = Path(skill_dir) if skill_dir else None

    # Resolved once; the pre-flight below is deliberately NOT run here. A resume that
    # spawns nothing — replaying a finished duel's summary.md — must stay CLI-free and
    # schema-free, exactly as preflight_executables already is, so both checks sit
    # together at the two points where a judge will actually be dispatched.
    schema_values = schema_placeholder_values(skill_dir_path)

    # RESUME INTENT COMES FROM THE POSITIONAL ARGUMENT ALONE, which is what SKILL.md has
    # always documented: --workdir only chooses where a NEW run lands. While this scanned
    # both, a --workdir that happened to contain a problem.md was silently taken as a resume
    # — the new problem statement discarded, and apply_resume then DELETING files matching
    # patterns as broad as plan-*.md in a directory the user never meant to continue.
    # Before anything is dispatched or created: two names that slugify alike would send both
    # final plans to one filename, and the summary would still claim two.
    require_distinct_slugs(controller_name, participant_name)

    resume_dir: Path | None = None
    if argument:
        path = Path(argument)
        if path.is_dir() and (path / "problem.md").is_file():
            if not _looks_like_duel_workdir(path):
                raise PlanDuelError(
                    f"{argument} holds a problem.md but none of this tool's artifacts, so "
                    f"it does not look like a duel workdir. Refusing to resume: a resume "
                    f"deletes plan-*.md, judge-*.md, rejections-*.md, participant-* and "
                    f"progress.log from the directory it is given, and those globs are "
                    f"broad enough to match an ordinary working directory. If it really "
                    f"is a duel workdir, create an empty {DUEL_MARKER_FILENAME} file in "
                    f"it and re-run."
                )
            resume_dir = path.resolve()

    if resume_dir is not None:
        workdir = resume_dir
        ctx = DuelContext(workdir, controller_name, participant_name, skill_dir_path)
        ctx.started_monotonic = time.monotonic()
        plan = compute_resume(workdir)
        if plan.complete:
            emit(read_text_normalized(workdir / "summary.md"))
            return 0
        # Before apply_resume deletes anything: a missing CLI must not cost the user their
        # artifacts. Skipped when this resume will spawn nothing — a duel whose rounds are all
        # complete but whose summary.md is missing only needs that summary written, so
        # requiring the CLIs would block recovering it. A resume PAST the cap can dispatch one
        # judge to recover the last round's verdict, and this condition still does not require
        # the CLIs for it: the re-judge degrades to v1's 0 and says so, an announced cost.
        if plan.init_incomplete or plan.start_round <= MAX_ROUNDS:
            preflight_executables(specs)
            preflight_schema(specs, schema_values)
        for name in apply_resume(plan):
            emit(f"Deleted {name}")
        if plan.message:
            emit(plan.message)
        state = load_state(workdir) or RunState(controller_name, participant_name)
        state.controller_name = controller_name
        state.participant_name = participant_name
        if plan.init_incomplete:
            run_init_round(
                workdir=workdir, specs=specs, ctx=ctx, emit=emit,
                timeout=timeout, state=state, reuse_plan_a=plan.reuse_plan_a,
            )
            start_round = 1
        else:
            start_round = plan.start_round
    else:
        # Check every CLI — and the schema an adapter's argv may need — before
        # creating a workdir or spending a plan run on one.
        preflight_executables(specs)
        preflight_schema(specs, schema_values)
        # The other way into the refusal above, and the one whose default message would
        # be unhelpful: `--workdir <a duel>` with NO positional argument used to resume.
        # It now cannot, so say what to type instead of "no problem statement provided".
        if not argument and workdir_arg and (Path(workdir_arg) / "problem.md").is_file():
            raise PlanDuelError(
                f"{workdir_arg} already holds a duel. To resume it, pass it as the "
                f"positional argument rather than --workdir."
            )
        problem_statement = _resolve_problem_statement(argument)
        # Already created AND claimed by the call: it reserves the directory by writing
        # problem.md exclusively, rather than naming a free path for a later write to race.
        # A second write here would defeat the exclusivity that made the reservation atomic.
        workdir = _resolve_new_workdir(workdir_arg, problem_statement).resolve()
        emit(str(workdir))
        ctx = DuelContext(workdir, controller_name, participant_name, skill_dir_path)
        ctx.started_monotonic = time.monotonic()
        state = RunState(controller_name, participant_name)
        run_init_round(
            workdir=workdir, specs=specs, ctx=ctx, emit=emit, timeout=timeout, state=state
        )
        start_round = 1

    rounds_run, stopped_due_to = run_duel(
        workdir=workdir, specs=specs, ctx=ctx, start_round=start_round,
        emit=emit, timeout=timeout, state=state,
    )
    write_summary(
        workdir=workdir,
        rounds_run=rounds_run,
        stopped_due_to=stopped_due_to,
        controller_name=controller_name,
        participant_name=participant_name,
        emit=emit,
    )
    _write_completion_terminator(ctx, rounds_run, stopped_due_to, state)
    return 0


# --------------------------------------------------------------------------- #
# CLI entrypoint
# --------------------------------------------------------------------------- #
# Per-spawn wall-clock ceiling. FINITE by default and deliberately generous: a real agent
# writing a full plan runs for many minutes, so a tight bound would kill honest work, while
# no bound lets one wedged CLI hold the duel open forever with `_heartbeat` cheerfully
# reporting "still working". It applies to EACH spawn, not to the duel, whose own ceiling is
# MAX_ROUNDS.
DEFAULT_SPAWN_TIMEOUT_SECONDS = 1800.0


def _spawn_timeout(value: str) -> float:
    """argparse type for ``--timeout``: a FINITE, strictly positive number of seconds.

    ``type=float`` alone would accept ``nan`` and ``inf``. Both reach
    ``Popen.communicate(timeout=)``, where a NaN comparison is never true and an infinite
    deadline never arrives — either one silently restores the unbounded spawn this flag
    exists to prevent, while looking like a configured bound.
    """
    try:
        seconds = float(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"{value!r} is not a number of seconds")
    if not math.isfinite(seconds) or seconds <= 0:
        raise argparse.ArgumentTypeError(
            f"--timeout must be a finite positive number of seconds, got {value!r}"
        )
    return seconds


def build_parser() -> argparse.ArgumentParser:
    """Build the argument parser for the engine CLI."""
    parser = argparse.ArgumentParser(
        prog="plan_duel",
        description="Run or resume a plan-duel (stdlib-only engine).",
    )
    parser.add_argument(
        "argument",
        nargs="?",
        help="Problem statement (inline text or a file path), or the path to an "
        "existing duel workdir to resume.",
    )
    parser.add_argument(
        "--workdir",
        help="Explicit duel working directory (resolved to an absolute path).",
    )
    parser.add_argument(
        "--adapter-config",
        help="Path to the structured adapter-config JSON (per-role command specs).",
    )
    parser.add_argument(
        "--skill-dir",
        help="Path to the plan-duel skill directory holding the prompt templates.",
    )
    parser.add_argument(
        "--controller-name",
        help="Concrete controller runtime name (Agent A), e.g. resolved by SKILL.md.",
    )
    parser.add_argument(
        "--participant-name",
        help="Concrete participant runtime name (Agent B), e.g. resolved by SKILL.md.",
    )
    parser.add_argument(
        "--timeout",
        type=_spawn_timeout,
        default=DEFAULT_SPAWN_TIMEOUT_SECONDS,
        metavar="SECONDS",
        help="Wall-clock ceiling for EACH agent/judge spawn, in seconds "
        f"(default: {DEFAULT_SPAWN_TIMEOUT_SECONDS:g}). A spawn that exceeds it is "
        "killed and halts the duel; there is no way to disable the bound.",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entrypoint: guard the interpreter, parse args, run the duel."""
    # PIN THE ENCODING FIRST, before anything can print. The engine's narration is full of
    # em dashes and its halts carry ⟪…⟫ markers; on a console whose default encoding cannot
    # represent them the FIRST such line raises UnicodeEncodeError and kills the run — after
    # both plans have been generated and snapshotted. ``errors="replace"`` is the belt to
    # utf-8's braces, mattering only if a caller's stream refuses the encoding change but
    # accepts the handler. ``stderr`` needs the same pin, since that is where the halt goes.
    # Line-buffering keeps stdout streaming live instead of block-buffering on a pipe.
    # Guarded and per-stream: a harness may replace either with an object lacking
    # ``reconfigure`` — degrade, never abort.
    for stream, extra in ((sys.stdout, {"line_buffering": True}), (sys.stderr, {})):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace", **extra)
        except (AttributeError, TypeError, ValueError, OSError):
            pass  # no reconfigure, a different signature, or a stream that refuses
    require_python(3, 10)
    parser = build_parser()
    args = parser.parse_args(argv)

    if not args.adapter_config:
        sys.stderr.write("plan-duel: --adapter-config is required.\n")
        return 2
    if not args.controller_name or not args.participant_name:
        sys.stderr.write(
            "plan-duel: --controller-name and --participant-name are required.\n"
        )
        return 2
    try:
        specs = parse_adapter_config(read_text_normalized(args.adapter_config))
    except (PlanDuelError, OSError) as exc:
        sys.stderr.write(f"plan-duel: {exc}\n")
        return 2

    try:
        return execute(
            argument=args.argument,
            workdir_arg=args.workdir,
            specs=specs,
            controller_name=args.controller_name,
            participant_name=args.participant_name,
            skill_dir=args.skill_dir,
            timeout=args.timeout,
        )
    except PlanDuelError as exc:
        sys.stderr.write(f"{exc}\n")
        return 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        # 130 is what a shell reports for SIGINT and what this always exited with — the
        # traceback is what changes. Eight frames of engine internals told the user nothing
        # they could act on, and the one thing they need is that the duel is resumable: the
        # workdir holds every completed round.
        # Names the POSITIONAL form, because that is the only one that resumes. "The same
        # command with the same --workdir" is refused by design, since `--workdir` only ever
        # chooses where a NEW run lands — so the instruction sent the user to the one command
        # that could not work.
        print("\nInterrupted. The duel is resumable — re-run with the workdir as the "
              "POSITIONAL argument (not --workdir) and it continues from the last "
              "completed round.", file=sys.stderr)
        raise SystemExit(130)
