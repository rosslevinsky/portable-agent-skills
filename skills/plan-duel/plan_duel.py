#!/usr/bin/env python3
"""plan-duel engine — stdlib-only, cross-platform duel state machine.

This module owns the deterministic heart of the plan-duel skill. It launches whatever CLIs
the ``SKILL.md`` adapter note injects for the three LLM judgment points — generate Plan A,
generate/critique Plan B, and judge — each as an argv list handed to the diff-review skill's
supervisor, ``review_runner.py``, which is this skill's one hard dependency. ``v1`` in the
comments below names the artifact contract ``SKILL.md``, ``round.md`` and ``summary.md``
spell out; the scenario fixtures hold this engine to its halt lines, exit order, resume
outcomes and summary layout.

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
names through ``which`` so ``PATHEXT`` is honored), Windows runs it through the shell,
which reinterprets ``%VAR%`` / ``&`` in arguments outside Python's quoting — and the
arguments here are whole generated prompts. Preflight refuses a shim on native Windows;
use a non-shim executable, or run the duel under WSL.

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
    exec       — resolve_executable / resolve_backends / run_supervised (an argv list
                 handed to the supervisor, never a shell)
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
import hashlib
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
from dataclasses import dataclass, field, replace as _replace_fields
from pathlib import Path
from typing import Callable, Collection, Mapping, Sequence


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


class SupervisorNotFoundError(CliNotFoundError):
    """The supervisor every role launches through is not where it was looked for."""


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

    def __init__(self, halt_message: str, *, cause: str | None = None,
                 spawn_failed: bool = False):
        self.halt_message = halt_message
        self.cause = cause
        # True when the CLI itself failed (a timeout or a failed exit), not the output check.
        self.spawn_failed = spawn_failed
        super().__init__(halt_message if cause is None else f"{halt_message} — {cause}")


class JudgeOutputError(PlanDuelError):
    """Raised when the judge process failed or produced no clean final message."""


class BackendChangedError(PlanDuelError):
    """The supervisor would not start a role because its backend's entry changed since the
    duel pinned it. Deliberately not a :class:`ProcessError`: the handlers that turn a failed
    agent or judge into a halt line or a round scored 0 must not catch it, because nothing
    failed; the duel can only continue on the backend it started with."""


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
        "model",  # the role's own `model` field; filled per role by render_argv
        "backend_args",  # the role's backend's `args`, spliced in as whole arguments
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
# "file" and "stdin" are refused at parse time because the dispatch site never reads
# prompt_mode: the prompt goes only into argv, and stdin is DEVNULL, so an adapter that
# relied on either mode would hand its CLI no prompt. Implement one at the dispatch site
# before re-listing it.
PROMPT_MODES = frozenset({"arg"})
DECLARED_BUT_UNIMPLEMENTED_PROMPT_MODES = frozenset({"file", "stdin"})
CWD_ANCHORS = frozenset({"workdir"})

_REQUIRED_ROLE_KEYS = ("command", "stdout")
_KNOWN_ROLE_KEYS = frozenset(
    {"command", "stdout", "prompt_mode", "cwd", "placeholders", "model", "env",
     "env_from_parent", "backend"}
)
MODEL_MARKER = f"{PLACEHOLDER_OPEN}model{PLACEHOLDER_CLOSE}"
BACKEND_ARGS_MARKER = f"{PLACEHOLDER_OPEN}backend_args{PLACEHOLDER_CLOSE}"

# A variable name, and nothing else. Stricter than any platform requires, which refuses a
# key with a hyphen in it — but not one in an underscore format (`gsk_…`, `hf_…`, hex), so
# a name that passes may still be a pasted key. A refused name is never quoted back, and
# `frozen_lineup` stores the launching-side name only as a digest.
_ENV_NAME_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")
_NOT_A_NAME = ("is not a variable name (letters, digits and underscores, not starting with a "
               "digit); it is not quoted here, in case a value was typed in its place")


def _env_key(name: str) -> str:
    """The key a variable is stored under: Windows names are case-insensitive."""
    return name.upper() if os.name == "nt" else name


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
        model: the model this role runs, rendered into ``command`` by ``⟪model⟫``;
            ``None`` leaves the choice to the CLI. Recorded, never interpreted.
        env: literal ``(name, value)`` settings for the role's environment — not secrets.
        env_from_parent: ``(name the child reads, name in this process)`` pairs; names on
            both sides, never a value.
        backend: the name of a backend in the user's backends file, which supplies the
            model, the arguments ``⟪backend_args⟫`` stands for, and settings the supervisor
            applies. ``None`` for a role that names none.
        resolved: that backend's fields, once :func:`resolve_backends` has read them.
    """

    command: tuple[str, ...]
    stdout: str
    prompt_mode: str = "arg"
    cwd: str | None = None
    placeholders: tuple[str, ...] = field(default_factory=tuple)
    model: str | None = None
    env: tuple[tuple[str, str], ...] = field(default_factory=tuple)
    env_from_parent: tuple[tuple[str, str], ...] = field(default_factory=tuple)
    backend: str | None = None
    resolved: ResolvedBackend | None = None


@dataclass(frozen=True)
class ResolvedBackend:
    """A backend's fields as the supervisor reported them, with ``⟪model⟫`` filled in its
    ``args``. The literal ``env`` values stay with the supervisor; only their names are kept,
    to refuse a role that sets the same variable itself."""

    name: str
    harness: str
    model: str
    args: tuple[str, ...]
    env_names: tuple[str, ...]
    env_from_parent: tuple[tuple[str, str], ...]
    # A digest of every field the supervisor reported, literal `env` values included: a
    # resume is held to what the name resolved to, not only the name.
    digest: str = ""


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
    unfilled = sorted(set().union(*(find_placeholders(part) for part in command)) - PLACEHOLDERS)
    if unfilled:
        rendered = ", ".join(f"{PLACEHOLDER_OPEN}{name}{PLACEHOLDER_CLOSE}" for name in unfilled)
        raise AdapterConfigError(
            f"role '{role}' command uses marker(s) this engine never fills: {rendered} "
            f"(it fills: {', '.join(sorted(PLACEHOLDERS))})"
        )

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
                f"honors nowhere: the prompt reaches a CLI only through an argv "
                f"placeholder. Accepting it and ignoring it would run the CLI with no "
                f"prompt at all. Put the prompt placeholder in 'command' and "
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

    model = raw.get("model")
    if model is not None and (not isinstance(model, str) or not model.strip()
                              or "\0" in model):
        raise AdapterConfigError(
            f"role '{role}' 'model' must be a non-empty string with no NUL character")
    backend = raw.get("backend")
    if backend is not None and (not isinstance(backend, str) or not backend.strip()
                                or "\0" in backend):
        raise AdapterConfigError(
            f"role '{role}' 'backend' must be the non-empty name of a backend")
    # The backend's arguments are spliced in as whole arguments, never pasted into one: a
    # provider setting inside another argument would reach the CLI as text it never parses.
    args_marked = [part for part in command if "backend_args" in find_placeholders(part)]
    if any(part != BACKEND_ARGS_MARKER for part in args_marked):
        raise AdapterConfigError(
            f"role '{role}' command uses {BACKEND_ARGS_MARKER} inside another argument; it "
            f"must stand alone, where the backend's arguments go")
    if args_marked and backend is None:
        raise AdapterConfigError(
            f"role '{role}' command uses {BACKEND_ARGS_MARKER} but the role names no "
            f"'backend' to fill it")
    # One statement of the model, in a place the engine can read. The marker with no field
    # would render an empty argument; the field with no marker would be recorded, and
    # reported, as the model while the CLI ran its default. A backend is a statement of it.
    uses_marker = any("model" in find_placeholders(part) for part in command)
    if uses_marker and model is None and backend is None:
        raise AdapterConfigError(
            f"role '{role}' command uses {MODEL_MARKER} but the role has no 'model' to fill it")
    if backend is not None and not uses_marker:
        raise AdapterConfigError(
            f"role '{role}' names backend {backend!r} but its command has no {MODEL_MARKER}, "
            f"so the CLI would never be told to run the backend's model")
    # The engine fills every marker, but the supervisor launches a backend role only when the
    # filled model stands as a whole argument or a setting's whole value somewhere in the argv.
    # Checked here, the same rule stops the duel before any role runs rather than at the
    # judge's launch, after both plans were paid for.
    if backend is not None and not any(
            part == MODEL_MARKER or _setting_value(part) == MODEL_MARKER for part in command):
        raise AdapterConfigError(
            f"role '{role}' names backend {backend!r}, so {MODEL_MARKER} must appear as an "
            f"argument of its own or a setting's whole value, like model={MODEL_MARKER}")
    if model is not None and not uses_marker:
        raise AdapterConfigError(
            f"role '{role}' names a 'model' but its command has no {MODEL_MARKER}, so the "
            f"CLI would never be told to run it")

    env = _parse_role_env(role, raw, "env")
    env_from_parent = _parse_role_env(role, raw, "env_from_parent")
    both = sorted({_env_key(n) for n, _ in env} & {_env_key(n) for n, _ in env_from_parent})
    if both:
        raise AdapterConfigError(
            f"role '{role}': {', '.join(both)} is set by both 'env' and 'env_from_parent'")

    return RoleSpec(
        command=command,
        stdout=stdout,
        prompt_mode=prompt_mode,
        cwd=cwd,
        placeholders=placeholders,
        model=model,
        env=env,
        env_from_parent=env_from_parent,
        backend=backend,
    )


def _parse_role_env(role: str, raw: Mapping[str, object], key: str) -> tuple[tuple[str, str], ...]:
    """One role's ``env`` or ``env_from_parent``: an object of variable name to string.

    Checked here, when the config is read, so a mistake is refused naming the role and the
    field rather than surfacing as a ``TypeError`` inside a spawn. No message quotes a value,
    nor the launching-side name of ``env_from_parent``: that is where a pasted key lands.
    """
    mapping = raw.get(key, {})
    if not isinstance(mapping, dict):
        raise AdapterConfigError(
            f"role '{role}' '{key}' must be an object of variable name to string")
    pairs = []
    for child, value in mapping.items():
        if not _ENV_NAME_RE.match(child):
            raise AdapterConfigError(f"role '{role}': an entry in '{key}' {_NOT_A_NAME}")
        if not isinstance(value, str):
            raise AdapterConfigError(f"role '{role}' '{key}' entry {child!r} must be a string")
        if key == "env_from_parent" and not _ENV_NAME_RE.match(value):
            raise AdapterConfigError(
                f"role '{role}' '{key}' entry {child!r}: the variable it reads {_NOT_A_NAME}")
        if "\0" in value:
            raise AdapterConfigError(
                f"role '{role}' '{key}' entry {child!r} holds a NUL character")
        pairs.append((child, value))
    return tuple(pairs)


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
        except (json.JSONDecodeError, RecursionError) as exc:
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
# Signed. Without the `-?`, `SCORE: -10` would parse as 10, which clears convergence_exit's
# `>= 8` and ends the duel at round 3 on the WORST score the rubric can express. The two
# score paths would also disagree: a JSON `-10` is rejected as out-of-range, while the
# string `"-10"` would come back as 10 and converge. With the sign kept, a negative lands
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


def _is_complete_verdict(obj: Mapping[str, object]) -> bool:
    """Whether ``obj`` carries every verdict field. Only such an object outranks a marker the
    file carries: a partial one may be an example quoted in the prose, so it fills only the
    fields the markers leave empty."""
    return JUDGE_JSON_KEYS <= set(obj)


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
    except (ValueError, RecursionError):
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
        except (ValueError, RecursionError):
            continue
        if _is_judge_verdict(candidate):
            found = candidate
    return found


def _digits_to_int(digits: str) -> int | None:
    """``int(digits)``, or ``None`` past the interpreter's digit limit.

    Python 3.11+ refuses to convert more than 4,300 digits, and a judge file is written by a
    model: a number that long is no score, so it takes the unparseable path.
    """
    try:
        return int(digits)
    except ValueError:
        return None


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
    # A judge with no enforced schema writes 8.0 for 8; a fraction is no rubric score.
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, str):
        number = _FIRST_INT_RE.search(value)
        if number is not None:
            return _digits_to_int(number.group(0))
    return None


def _marker_score(text: str) -> int | None:
    """The first integer on the first ``SCORE:`` line (the pre-schema contract)."""
    line = _SCORE_LINE_RE.search(text)
    if line is None:
        return None
    number = _FIRST_INT_RE.search(line.group(1))
    return _digits_to_int(number.group(0)) if number is not None else None


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
    if obj is not None and (_SCORE_LINE_RE.search(text) is None or _is_complete_verdict(obj)):
        value = _json_score(obj)
        if value is not None:
            return value
    return _marker_score(text)


def parse_score(text: str) -> int | None:
    """Return the judge's usable score: the JSON verdict's, else the ``SCORE:`` line.

    ``None`` means neither form carried an integer inside the rubric's range. Callers treat
    ``None`` as 0 and emit :func:`score_warning`. A JSON verdict whose ``score`` is missing
    or unusable still falls through to the marker parser, so the JSON form never narrows
    the degrade path.
    """
    obj = parse_judge_json(text)
    marker = _usable_score(_marker_score(text))
    # A SCORE: line counts as carried even when its value is unusable: an out-of-range score
    # is warned about as written, never replaced by a quoted partial object.
    carries_marker = _SCORE_LINE_RE.search(text) is not None
    if obj is not None and (not carries_marker or _is_complete_verdict(obj)):
        score = _usable_score(_json_score(obj))
        if score is not None:
            return score
    return marker


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
    label = read_preferred_marker(text)
    marker = label.side
    # A PREFERRED: line that names no side still counts as carried, as a SCORE: line does.
    carries_marker = marker is not None or label.unreadable is not None
    if obj is not None and (not carries_marker or _is_complete_verdict(obj)):
        value = obj.get("preferred")
        if isinstance(value, str) and value.strip().upper() in ("A", "B"):
            return value.strip().upper()
    return marker


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
    # utf-8-sig drops a leading byte-order mark, which PowerShell 5.1's -Encoding UTF8 writes.
    return _normalize_newlines(Path(path).read_bytes().decode("utf-8-sig"))


def read_text_tolerant(path: str | os.PathLike[str]) -> str:
    """Read ``path`` as UTF-8 with U+FFFD replacement; same newline normalization.

    For files a THIRD-PARTY CLI wrote — plans, judge verdicts, last-message captures. A
    model's prose routinely picks up a stray cp1252 byte, and a strict decode would throw
    away a whole paid-for round over a character nothing parses. The engine reads these for a
    ``SCORE:`` line, a word count or a table row; one replacement character costs none of
    that. Deliberately NOT the shared default — see :func:`read_text_normalized`.
    """
    return _normalize_newlines(Path(path).read_bytes().decode("utf-8", "replace"))


def _mode_or_absent(path: Path, *, follow: bool = True) -> int | None:
    """``path``'s ``st_mode``, or ``None`` when nothing can be at that name.

    **A read has three outcomes and the existence predicates report two.** Which errors
    they fold into False is not "the file is not there": ``Path.exists()`` and its siblings
    answer False for ``ELOOP`` and ``EBADF`` as readily as for ``ENOENT``, so a symlink
    loop — a name the filesystem cannot resolve at all — reads as a name with nothing at
    it. ``os.path.exists()`` is broader still and swallows every ``OSError``, and the set
    each of them folds has changed between interpreter versions, so the answer to "is this
    unreadable file there" depends on which Python is running.

    This fixes the set: absent is ``ENOENT`` and ``ENOTDIR`` (a parent component is not a
    directory), the two that really do mean nothing can be at this name. ``ENAMETOOLONG``
    is not one of them: it says the path could not be RESOLVED, which establishes nothing
    about what stands at the name — a long alias for a real file answers it too, so reading
    it as absence drops a file that is right there. Every other ``OSError`` propagates, on
    every version.

    ``follow=False`` inspects the link itself, for callers asking whether a name IS a link.
    """
    try:
        return (os.stat if follow else os.lstat)(path).st_mode
    except (FileNotFoundError, NotADirectoryError):
        return None


def _present(path: Path) -> bool:
    """Is anything at ``path``? Absent answers False; unreadable raises.

    Follows the link, like the ``Path.exists()`` each caller used before it: a dangling
    link is a name with nothing at it, and a looping one is a name that cannot be resolved.
    """
    return _mode_or_absent(path) is not None


def _is_file(path: Path) -> bool:
    """Is ``path`` a regular file? Absent answers False; unreadable raises."""
    mode = _mode_or_absent(path)
    return mode is not None and stat.S_ISREG(mode)


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


def write_text_roundtrip(
    path: str | os.PathLike[str], text: str, *, newline: str = "\n"
) -> None:
    """The exact inverse of :func:`read_text_roundtrip` — see it for why. ``newline`` puts back
    the line ending the file was read with.

    A symlink standing at ``path`` is refused, like every other write into the workdir; a
    missing live plan is a warned SKIP, so the stamp can reach a link an agent planted. The
    bytes land through :func:`_write_bytes_atomic`: the winner is rewritten in place, and a
    truncating write that failed part-way would leave it empty.
    """
    path = Path(path)
    if path.is_symlink():
        raise PlanDuelError(
            f"refusing to write through a symlink: {path}. A duel artifact must be a "
            f"regular file; a link here would send the write outside the workdir."
        )
    data = _normalize_newlines(text).replace("\n", newline).encode("utf-8", "surrogateescape")
    _write_bytes_atomic(path, data)


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
    # O_NONBLOCK, and the regular-file check below: a named pipe planted at an agent-writable
    # path would otherwise hold the open, or the write, until something read it.
    flags = os.O_WRONLY | os.O_CREAT | nofollow | getattr(os, "O_NONBLOCK", 0)
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
    if not stat.S_ISREG(os.fstat(handle).st_mode):
        os.close(handle)
        raise PlanDuelError(f"refusing to write to {path}: it is not a regular file")
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
    _write_bytes_atomic(path, data)


def _mode_for(path: Path) -> int:
    """The mode a file written to ``path`` through a temporary should end with. `mkstemp`
    creates 0600 and `os.replace` keeps whatever the temporary had, so a plan every teammate
    could read would become readable only by whoever ran the duel. An existing file keeps its
    own mode; a new one gets what a plain create would have, which is the umask's answer."""
    try:
        return os.stat(path).st_mode & 0o7777
    except FileNotFoundError:
        current = os.umask(0)
        os.umask(current)
        return 0o666 & ~current


def _write_bytes_atomic(path: Path, data: bytes) -> None:
    """``data`` into ``path`` by a temporary file beside it and ``os.replace``: the file is the
    old one or the new one, never a truncated one. ``os.replace`` swaps a link standing at
    ``path`` rather than following it."""
    handle, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(handle, "wb") as out:
            out.write(data)
            out.flush()
            os.fsync(out.fileno())
        os.chmod(tmp, _mode_for(path))   # the mode travels with the content
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
        os.chmod(tmp, _mode_for(dst))   # a snapshot or a named plan is shared like any file
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
    # `_is_file`, not `entry.is_file()`. This listing answers "which rounds completed",
    # "which artifacts a resume deletes" and "is init incomplete"; an entry dropped because
    # its name would not resolve makes a completed round look unrun, and an init-incomplete
    # verdict deletes the plans of the round it could not see.
    for entry in sorted(Path(workdir).iterdir(), key=lambda item: item.name):
        if _is_file(entry):
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
    file. ``keep`` spares additional artifact names by exact match, which is how an
    already-validated Plan A crosses an init-incomplete resume instead of being paid
    for twice. With the default empty ``keep`` the deletion log is unchanged.
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

# Written at claim time so a workdir can be recognized as THIS tool's, rather than
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
    # Who played each role, as :func:`frozen_lineup` records it. Empty in a state file
    # written before the record existed, which then resumes unchecked and starts one.
    lineup: dict[str, dict] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "controller_name": self.controller_name,
            "participant_name": self.participant_name,
            "lineup": self.lineup,
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
            lineup=_lineup_from_dict(data.get("lineup")),
        )


def frozen_lineup(specs: Mapping[str, RoleSpec]) -> dict[str, dict]:
    """Who plays each role: what a resume may not change.

    argv0 as the adapter writes it — never the path :func:`resolve_executable` finds, which
    moves when a CLI is upgraded — the ``model``, and the ``env_from_parent`` mapping, never
    a value. The name the child reads is kept; the name read from the launching side is kept
    only as :func:`_name_digest`, because a key pasted there can pass the name check, and a
    record that holds it verbatim writes the key to disk. Both go through :func:`_env_key`,
    so a case-only change is no change where names ignore case. Named things only: freezing the whole command would
    refuse a resume that adds ``--verbose`` or a longer timeout, and a duel with one bad flag
    could then never be resumed.

    A role naming a backend also records the backend's name, and its ``model`` is the one the
    backend supplied — so ``specs`` must have been through :func:`resolve_backends`, or, on a
    resume that launches nothing, :func:`_models_from_record`. The same
    model reached through another provider is another player. The key is absent for a role
    with no backend, which is also how a record written before backends reads.
    """
    lineup: dict[str, dict] = {}
    for role, spec in specs.items():
        if role not in REQUIRED_ROLES:
            continue
        record = {
            "argv0": spec.command[0],
            "model": spec.model,
            "env_from_parent": {_env_key(child): _name_digest(_env_key(parent))
                                for child, parent in spec.env_from_parent},
        }
        # The role's own literal settings, and what its backend resolved to, as digests: an
        # `env` value can be the address that picks the provider, and the agent running the
        # skill rewrites the config on every run, so a changed value is a changed player.
        # No value is written down. A record written before these existed has neither, and a
        # resume that launches nothing reads no backend, so each is compared only where both
        # sides carry it.
        record["env"] = _name_digest(json.dumps(
            sorted((_env_key(name), value) for name, value in spec.env)))
        if spec.backend is not None:
            record["backend"] = spec.backend
        if spec.resolved is not None:
            record["backend_digest"] = spec.resolved.digest
        lineup[role] = record
    return lineup


def _name_digest(name: str) -> str:
    """Enough of a SHA-256 of ``name`` to tell two names apart, and not enough to be one."""
    return hashlib.sha256(name.encode("utf-8")).hexdigest()[:16]


def _lineup_from_dict(raw: object) -> dict[str, dict]:
    """The recorded lineup, keeping only well-formed entries; :func:`load_state` never raises."""
    if not isinstance(raw, dict):
        return {}
    lineup: dict[str, dict] = {}
    for role, record in raw.items():
        if role not in REQUIRED_ROLES or not isinstance(record, dict):
            continue
        argv0, model = record.get("argv0"), record.get("model")
        mapping = record.get("env_from_parent", {})
        backend = record.get("backend")
        if (not isinstance(argv0, str) or not (model is None or isinstance(model, str))
                or not (backend is None or isinstance(backend, str))
                or not isinstance(mapping, dict)
                or not all(isinstance(k, str) and isinstance(v, str)
                           for k, v in mapping.items())):
            continue
        lineup[role] = {"argv0": argv0, "model": model, "env_from_parent": dict(mapping)}
        if backend is not None:
            lineup[role]["backend"] = backend
        for key in ("env", "backend_digest"):
            if isinstance(record.get(key), str):
                lineup[role][key] = record[key]
    return lineup


def lineup_changes(saved: Mapping[str, dict], current: Mapping[str, dict]) -> list[str]:
    """Each way ``current`` plays a role differently from ``saved``, one phrase per change.

    A forwarded credential is named by the variable the child reads, never by the one it
    is read from: that side is where a key gets pasted by mistake.
    """
    changes: list[str] = []
    for role in REQUIRED_ROLES:
        before, after = saved.get(role), current.get(role)
        if before is None or after is None:
            continue
        if before["argv0"] != after["argv0"]:
            changes.append(f"{role} ran {before['argv0']!r} and now names {after['argv0']!r}")
        if before["model"] != after["model"]:
            changes.append(f"{role} ran {_model_phrase(before['model'])} and now names "
                           f"{_model_phrase(after['model'])}")
        if before.get("backend") != after.get("backend"):
            changes.append(f"{role} ran on {_backend_phrase(before.get('backend'))} and now "
                           f"names {_backend_phrase(after.get('backend'))}")
        old, new = before["env_from_parent"], after["env_from_parent"]
        moved = sorted(name for name in set(old) | set(new) if old.get(name) != new.get(name))
        if moved:
            changes.append(f"{role} forwards a different credential as {', '.join(moved)}")
        if "env" in before and "env" in after and before["env"] != after["env"]:
            changes.append(f"{role} sets different values in its own env")
        if ("backend_digest" in before and "backend_digest" in after
                and before.get("backend") == after.get("backend")
                and before["backend_digest"] != after["backend_digest"]):
            changes.append(f"{role}'s backend {after.get('backend')!r} now resolves to "
                           f"different settings")
    return changes


def _kept_players(plan: "ResumePlan", workdir: Path) -> frozenset[str]:
    """The roles whose output ``plan`` carries forward: only they are held to the lineup.

    A role whose every output the resume discards is played afresh, so a first spawn that
    failed on a mistyped model can be retried with the corrected one.
    """
    if plan.init_incomplete:
        return frozenset({"agent_a"}) if plan.reuse_plan_a else frozenset()
    if scan_snapshots(workdir).judge_rounds:
        return frozenset(REQUIRED_ROLES)
    return frozenset({"agent_a", "agent_b"})


def _model_phrase(model: str | None) -> str:
    return f"model {model!r}" if model is not None else "no stated model"


def _backend_phrase(backend: str | None) -> str:
    return f"backend {backend!r}" if backend is not None else "no backend"


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
    """Read ``state.json`` if absent or unparseable, else ``None``.

    ``None`` means the file is NOT THERE or does not parse. It does not mean the file
    could not be read: an unreadable ``state.json`` raises, because ``None`` is taken by
    every caller as "this duel wrote no state", which discards every round marker, makes a
    judged round look unjudged, and skips the guard that refuses a resume whose controller
    and participant names differ from the ones the duel started with.
    """
    path = Path(workdir) / STATE_FILENAME
    if not _present(path):
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None  # removed between the check and the read
    except (json.JSONDecodeError, ValueError, RecursionError):
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
        # `_is_file` throughout: `has_summary` False ends a finished duel by re-running
        # it, and `has_live_*` False loses the audit line that says a stale plan is about
        # to be deleted. Neither may be derived from a name that would not resolve.
        has_live_a=_is_file(workdir / "plan-a.md"),
        has_live_b=_is_file(workdir / "plan-b.md"),
        has_summary=_is_file(workdir / "summary.md"),
        has_problem=_is_file(workdir / "problem.md"),
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
        if 0 in scan.plan_a_rounds and _is_file(snapshot_a):
            # PROOF of completeness, not a heuristic. The snapshot is a byte copy of the live
            # plan the engine already validated, so an exact match can only hold if the copy
            # finished. This closes the gap a size gate leaves open: a snapshot interrupted
            # past 200 bytes by an older, non-atomic build still looks big enough.
            reuse_plan_a = (
                file_size_bytes(snapshot_a) >= MIN_AGENT_OUTPUT_BYTES
                and _is_file(live_a)
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
        if _is_file(live) and live.read_bytes() != snapshot.read_bytes():
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
        # plan-a.md stays with its snapshot: compute_resume trusts the snapshot only while the
        # two match byte for byte, so deleting it here makes a resume killed before
        # run_init_round restores it pay for Plan A again.
        keep = (plan_snapshot_name("a", 0), "plan-a.md") if plan.reuse_plan_a else ()
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
    # `_present`, not `.exists()`. A frozen input whose name will not resolve is not a
    # frozen input that is missing, and the difference is destructive: `.exists()` says no,
    # the copy below overwrites the round's immutable snapshot with whatever the live plan
    # currently holds, and the judge scores a round against inputs it was never given.
    if not _present(frozen_a):
        copy_bytes(workdir / live_a, frozen_a)
    if not _present(frozen_b):
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
        # `os.access` answers a question the filesystem may refuse to answer, and both
        # branches below are refusals, so its two-valued answer authorizes nothing: an
        # unknown here becomes `CliNotFoundError` before any agent is paid for, which is
        # what preflight is for.
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate.resolve())
        if candidate.is_file():
            raise CliNotFoundError(f"not executable: {argv0}")
        raise CliNotFoundError(f"executable not found: {argv0}")
    resolved = shutil.which(argv0)
    if resolved is None:
        raise CliNotFoundError(f"CLI not found on PATH: {argv0}")
    # Absolute, so a relative PATH entry names one file whatever directory a role runs in.
    resolved = os.path.abspath(resolved)
    # Windows searches the current directory before PATH, and that directory is normally the
    # repository being planned: a program planted there would run with the adapters' flags.
    cwd = os.path.normcase(os.getcwd())
    listed = {
        os.path.normcase(os.path.abspath(entry))
        for entry in os.environ.get("PATH", os.defpath).split(os.pathsep)
    }
    if os.path.normcase(os.path.dirname(resolved)) == cwd and cwd not in listed:
        raise CliNotFoundError(
            f"{argv0} resolves to {resolved}, in the current directory rather than on PATH; "
            f"refusing to run a program from the directory the duel was started in. Start "
            f"it from another directory, or name the CLI by absolute path."
        )
    return resolved


def _refuse_batch_shim(resolved: str, argv0: str) -> None:
    """On Windows, refuse a CLI that resolves to a ``.cmd`` or ``.bat`` wrapper.

    Windows runs one through ``cmd.exe``, which ends an argument at a newline and reinterprets
    ``%`` and ``&``, and the arguments that matter here are multi-line prompts: the duel would
    fail at its first spawn, or run a prompt nobody wrote. String-only, with no ``Path``.
    """
    if os.name == "nt" and os.path.splitext(resolved)[1].lower() in (".cmd", ".bat"):
        raise CliNotFoundError(
            f"{argv0} resolves to {resolved}, a batch wrapper that Windows runs through "
            f"cmd.exe, which mangles the multi-line prompts this engine passes. Point the "
            f"adapter at the program the wrapper launches, or run the duel under WSL."
        )


def preflight_executables(specs: Mapping[str, RoleSpec]) -> None:
    """Resolve EVERY role's CLI before any billable work starts.

    :func:`run_supervised` resolves an executable at spawn time, so without this the participant CLI
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
            resolved = resolve_executable(argv0)
        except CliNotFoundError:
            missing[argv0] = [role]
            continue
        _refuse_batch_shim(resolved, argv0)
    if missing:
        detail = "; ".join(
            f"{cli} (needed by {', '.join(roles)})" for cli, roles in missing.items()
        )
        raise CliNotFoundError(f"CLI not found on PATH: {detail}")


# --------------------------------------------------------------------------- #
# supervisor — every role launches through diff-review's review_runner.py
# --------------------------------------------------------------------------- #
SUPERVISOR_SKILL = "diff-review"
SUPERVISOR_NAME = "review_runner.py"
# How long past a spawn's own limit the supervisor may take to stop that spawn and report. Its
# kill ladder and drain waits are bounded well inside this; it bounds a supervisor that hangs.
SUPERVISOR_GRACE_SECONDS = 120.0
# How long a supervisor told to stop is given to stop its CLI, before it is killed itself.
SUPERVISOR_STOP_SECONDS = 30.0
# How long an interrupted supervisor is let finish on its own before it is signaled. Its
# cleanup is a terminate, a kill and a wait of 5 seconds each; a signal during it ends it.
SUPERVISOR_INTERRUPT_WAIT_SECONDS = 20.0
# The supervisor's reason when a helper still held the CLI's stdout after it exited.
_SUPERVISOR_UNDRAINED = "reader did not drain child output"
# The supervisor's refusal of a backend whose entry changed since the duel pinned it,
# verbatim from review_runner.py.
_BACKEND_CHANGED = "changed since the run pinned it"
# The supervisor's refusals of a backends file that went missing, stopped parsing or lost
# the role's entry, verbatim from review_runner.py. For a role pinned to a backend these are
# the same event as a changed entry: the backend it started with can no longer be read.
_BACKENDS_FILE_REFUSALS = ("there is no backends file at", "the backends file ",
                           "no backend named ")


def _backends_file_text() -> str:
    """The backends file the supervisor reads, spelled as it spells it in a refusal about an
    entry: through `Path`, as the supervisor builds it."""
    override = os.environ.get("PORTABLE_AGENT_SKILLS_BACKENDS")
    return str(Path(override) if override
               else Path.home() / ".portable-agent-skills" / "backends.json")


def _is_pinned_backend_refusal(reason: str, spec: "RoleSpec | None") -> bool:
    """Whether a launch refusal means the role's pinned backend is no longer the one the
    duel started with: a changed entry, or, for a role on a backend, a backends file that
    cannot give that entry back at all."""
    if _BACKEND_CHANGED in reason:
        return True
    if spec is None or spec.backend is None:
        return False
    return (any(mark in reason for mark in _BACKENDS_FILE_REFUSALS)
            or _backends_file_text() in reason)
# The prefix of a name that carries one literal setting to the supervisor in its environment.
_LITERAL_ENV_PREFIX = "PLAN_DUEL_LITERAL_"
# The supervisor's own start and end lines in the output log, which are not the CLI's words.
_SUPERVISOR_MARKER_RE = re.compile(
    rb"\[review_runner\] (?:start|end status=\S* exit=\S* drained=\S*)\r?\n")
_LAUNCHER_SUFFIXES = (".exe", ".cmd", ".bat", ".com")


def default_supervisor() -> Path:
    """``review_runner.py`` in the diff-review skill, installed beside this one."""
    return Path(__file__).resolve().parent.parent / SUPERVISOR_SKILL / SUPERVISOR_NAME


def require_supervisor(path: str | os.PathLike[str]) -> Path:
    """``path`` when it is a file, else a refusal naming the skill that provides it.

    Asked before anything is dispatched, and again at every spawn, where it reaches a caller
    as a :class:`ProcessError` like any other launch that could not happen — so a resumed
    re-judge scores zero rather than halting.
    """
    path = Path(path)
    try:
        info = os.stat(path)
    except OSError as exc:
        absent = isinstance(exc, (FileNotFoundError, NotADirectoryError))
        raise SupervisorNotFoundError(
            f"plan-duel launches every role through the {SUPERVISOR_SKILL} skill's "
            f"{SUPERVISOR_NAME}, and "
            + (f"there is none at {path}" if absent
               else f"{path} could not be read ({exc.strerror or exc})")
            + f". Install the {SUPERVISOR_SKILL} skill beside this one, or pass --supervisor "
              f"with the path to its {SUPERVISOR_NAME}.") from None
    if not stat.S_ISREG(info.st_mode):
        raise SupervisorNotFoundError(
            f"{path} is not a file, so it is not the {SUPERVISOR_SKILL} skill's "
            f"{SUPERVISOR_NAME} that plan-duel launches every role through; pass --supervisor.")
    return path


def _supervisor_status(stdout: bytes) -> dict | None:
    """The one JSON status line the supervisor prints last, or ``None`` without one."""
    for line in reversed(stdout.decode("utf-8", "replace").splitlines()):
        if not line.strip():
            continue
        try:
            status = json.loads(line)
        except (ValueError, RecursionError):
            return None
        return status if isinstance(status, dict) and isinstance(status.get("status"), str) \
            else None
    return None


def _harness_of(program: str) -> str:
    """The harness a program name launches, read the way the supervisor reads it: the last
    path part, without a launcher suffix, case-folded."""
    base = program.replace("\\", "/").rsplit("/", 1)[-1]
    stem, suffix = os.path.splitext(base)
    if suffix.lower() in _LAUNCHER_SUFFIXES:
        base = stem
    return base.casefold()


def _setting_value(part: str) -> str | None:
    """The value of a ``key=value`` argument, unquoted, or ``None`` for anything else."""
    key, sep, value = part.partition("=")
    if not sep or not key or any(ch.isspace() for ch in key):
        return None
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        return value[1:-1]
    return value


def _args_with_model(args: Sequence[str], model: str) -> tuple[str, ...]:
    """A backend's ``args`` with ``⟪model⟫`` filled where it is a whole argument or a
    setting's value.

    The supervisor fills them by the same rule and then looks for the result in the argv it
    is handed; an argument filled any other way here is not found there, and the launch is
    refused rather than run without the backend's settings.
    """
    filled = []
    for part in args:
        if part == MODEL_MARKER:
            filled.append(model)
        elif _setting_value(part) == MODEL_MARKER:
            key, sep, value = part.partition("=")
            filled.append(key + sep + value.replace(MODEL_MARKER, model))
        else:
            filled.append(part)
    return tuple(filled)


def _resolve_backend(supervisor: Path, name: str) -> ResolvedBackend:
    """One backend, read by the supervisor: the backends file has one reader, and it is not
    this engine."""
    try:
        done = subprocess.run([sys.executable, str(supervisor), "--resolve-backend", name],
                              stdin=subprocess.DEVNULL, capture_output=True, timeout=60)
    except (OSError, subprocess.SubprocessError) as exc:
        raise AdapterConfigError(
            f"backend {name!r} could not be read: the supervisor did not run ({exc})") from exc
    status = _supervisor_status(done.stdout)
    if status is None:
        raise AdapterConfigError(
            f"backend {name!r} could not be read: the supervisor exited {done.returncode} "
            f"without a status line")
    if status["status"] != "ok":
        raise AdapterConfigError(str(status.get("reason") or f"backend {name!r} was refused"))
    fields = status.get("backend")
    try:
        harness, model, args = fields["harness"], fields["model"], fields["args"]
        env, env_from_parent = fields["env"], fields["env_from_parent"]
        well_formed = (isinstance(harness, str) and isinstance(model, str)
                       and all(isinstance(part, str) for part in args)
                       and all(isinstance(key, str) for key in env)
                       and all(isinstance(key, str) and isinstance(value, str)
                               for key, value in env_from_parent.items()))
    except (KeyError, TypeError, AttributeError):
        well_formed = False
    if not well_formed:
        raise AdapterConfigError(
            f"backend {name!r}: the supervisor reported it in a shape this engine does not read")
    # The same digest review_runner.py's `backend_digest` takes of these fields and checks
    # on every launch: if the two formulas ever differ, every launch is refused.
    return ResolvedBackend(name=name, harness=harness, model=model,
                           args=_args_with_model(args, model), env_names=tuple(env),
                           env_from_parent=tuple(env_from_parent.items()),
                           digest=_name_digest(json.dumps(fields, sort_keys=True)))


def resolve_backends(specs: Mapping[str, RoleSpec],
                     supervisor: str | os.PathLike[str]) -> dict[str, RoleSpec]:
    """``specs`` with every named backend read, and its model taken as the role's.

    Before anything else uses a role's model — the lineup a resume is held to, the summary,
    the argv — because a backend is where that model is stated. A role that also states its
    own must state the same one: a role runs one model. A backend with ``args`` needs the
    command's ``⟪backend_args⟫``, or its provider settings would be dropped and the CLI would
    reach its default provider under this backend's name. A config naming no backend is
    returned as it is, and needs no supervisor for this.
    """
    names = sorted({spec.backend for spec in specs.values() if spec.backend is not None})
    if not names:
        return dict(specs)
    supervisor = require_supervisor(supervisor)
    found = {name: _resolve_backend(supervisor, name) for name in names}
    resolved: dict[str, RoleSpec] = {}
    for role, spec in specs.items():
        backend = found.get(spec.backend) if spec.backend is not None else None
        if backend is None:
            resolved[role] = spec
            continue
        if spec.model is not None and spec.model != backend.model:
            raise AdapterConfigError(
                f"role '{role}' states model {spec.model!r}, but its backend {backend.name!r} "
                f"runs {backend.model!r}. A role runs one model: drop the role's 'model', or "
                f"make the two agree")
        if backend.args and BACKEND_ARGS_MARKER not in spec.command:
            raise AdapterConfigError(
                f"role '{role}': backend {backend.name!r} has 'args', but the role's command "
                f"has no {BACKEND_ARGS_MARKER} to put them in, so its provider settings "
                f"would be dropped")
        own = {_env_key(name) for name, _ in spec.env + spec.env_from_parent}
        theirs = ({_env_key(name) for name in backend.env_names}
                  | {_env_key(child) for child, _ in backend.env_from_parent})
        both = sorted(own & theirs)
        if both:
            raise AdapterConfigError(
                f"role '{role}': {', '.join(both)} is set both by the role and by its backend "
                f"{backend.name!r}; set it once")
        resolved[role] = _replace_fields(spec, model=backend.model, resolved=backend)
    return resolved


def _models_from_record(specs: Mapping[str, RoleSpec],
                        lineup: Mapping[str, dict]) -> dict[str, RoleSpec]:
    """``specs`` for a resume that launches nothing: each role naming a backend and no model
    of its own takes the model the record says that same backend supplied.

    The backend itself is not read, so replaying a finished duel needs neither the backends
    file nor the supervisor that reads it. A role whose backend differs from the record's is
    left without one, and the lineup check reports the change of backend.
    """
    replayed: dict[str, RoleSpec] = {}
    for role, spec in specs.items():
        record = lineup.get(role)
        if (spec.backend is not None and spec.model is None and record is not None
                and record.get("backend") == spec.backend):
            spec = _replace_fields(spec, model=record["model"])
        replayed[role] = spec
    return replayed


def preflight_launch(specs: Mapping[str, RoleSpec]) -> None:
    """Refuse, before the first launch, what the supervisor would refuse at a later one.

    The supervisor checks a backend's harness and every forwarded variable as it launches each
    role, so a mistake on the judge would otherwise surface after both plans were paid for. A
    forwarded variable is checked on the launching side only — the child's side is the
    harness's own name, normally unset by design — and empty counts as unset. An entry is
    named by the variable the agent reads, never by the one it is read from, which is where a
    key typed in the wrong place lands.
    """
    problems = []
    for role in REQUIRED_ROLES:
        spec = specs.get(role)
        if spec is None:
            continue
        backend = spec.resolved
        if backend is not None:
            launched, expected = _harness_of(spec.command[0]), _harness_of(backend.harness)
            if launched != expected:
                problems.append(
                    f"{role}'s backend {backend.name!r} is written for {expected!r}, but its "
                    f"command launches {launched!r}")
        forwarded = spec.env_from_parent + (backend.env_from_parent if backend else ())
        unset = sorted({child for child, parent in forwarded if not os.environ.get(parent)})
        if unset:
            problems.append(
                f"{role} forwards {', '.join(unset)} from a variable that is not set, or is "
                f"empty, in this process's environment")
    if problems:
        raise PlanDuelError(
            f"Refusing to start, before any role launches: {'; '.join(problems)}.")


@dataclass
class SpawnResult:
    """What one supervised spawn left: whether the supervisor has the reply in hand, why not
    where it has not, and what the CLI printed on either stream, for a halt to quote."""

    ok: bool
    reason: str | None
    output: bytes


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

    **POSIX: both rungs go to the GROUP, and neither is conditional on the leader.** A
    caller passing ``group_leader`` spawned with ``start_new_session``, so the pid *is* the
    pgid. The
    leader exiting says nothing about descendants that inherited its pipes — the common wedge
    is a CLI that returns promptly while the runtime it spawned keeps stdout open. Gating the
    signal on ``poll()`` is how such a descendant survives. So SIGTERM the group, wait a
    bounded moment, then SIGKILL the group regardless, stopping early only on ``ESRCH``.

    A descendant that calls ``setsid()`` leaves the group and survives; the guarantee is
    group-wide, not absolute. Signaling a pgid after the leader is reaped is safe: the
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
            pass  # fall through to terminate/kill
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


def _program(argv0: str) -> str:
    """argv0 as the supervisor is given it.

    A bare name stays bare, for the supervisor to find on PATH as the adapter wrote it. A path
    is made absolute but never resolved through a link: the supervisor reads the harness from
    the name, and an installed CLI is often a link to a script named nothing like it.
    """
    has_sep = os.sep in argv0 or bool(os.altsep and os.altsep in argv0)
    return os.path.abspath(argv0) if has_sep or Path(argv0).is_absolute() else argv0


def _clear_for_supervisor(path: Path) -> None:
    """Make way for a file the supervisor creates at ``path``.

    The supervisor writes only files it creates, so a regular file left from an earlier spawn
    is removed here — the same fresh start a truncating write gave. A link is refused, never
    removed: the path is inside a workdir an agent can write to, and one planted there is
    where a write would have left the workdir.
    """
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return
    if stat.S_ISLNK(info.st_mode):
        raise PlanDuelError(f"refusing to write agent output through a symlink: {path}")
    if stat.S_ISREG(info.st_mode):
        path.unlink()


def _read_output(path: Path) -> bytes:
    """What the CLI printed, from the supervisor's output log, without its own lines.

    Only a regular file is read: a pipe put in its place would hold the read open. A log that
    cannot be read costs a halt its diagnostic, never the halt.
    """
    try:
        if not stat.S_ISREG(os.lstat(path).st_mode):
            return b""
        data = path.read_bytes()
    except OSError:
        return b""
    return _SUPERVISOR_MARKER_RE.sub(b"", data)


def _stop_supervisor(proc: subprocess.Popen) -> None:
    """Stop a supervisor and the CLI under it; never raises.

    On POSIX the supervisor is asked first: its own handler ends the CLI's whole process group,
    which is not this process's to reach. On Windows a terminate ends the supervisor alone, so
    the tree goes at once. Either way it is bounded, and the last resort is the kill.
    """
    if os.name == "posix":
        with contextlib.suppress(OSError):
            proc.terminate()
        try:
            proc.communicate(timeout=SUPERVISOR_STOP_SECONDS)
            return
        except (subprocess.TimeoutExpired, OSError, ValueError):
            pass
    _terminate_child(proc, group_leader=False)
    with contextlib.suppress(subprocess.TimeoutExpired, OSError, ValueError):
        proc.communicate(timeout=DRAIN_AFTER_KILL_SECONDS)


def _let_supervisor_finish(proc: subprocess.Popen) -> None:
    """Give a supervisor that may already be stopping time to finish; never raises.

    A terminal Ctrl-C reaches the supervisor as well as this process, and its handler is then
    ending the CLI, which runs in a session of its own and is reached by nothing else. A
    second signal in that window ends the supervisor mid-cleanup and leaves the CLI running,
    so it is sent only once this wait is over. A second Ctrl-C ends the wait.
    """
    with contextlib.suppress(subprocess.TimeoutExpired, KeyboardInterrupt, OSError):
        proc.wait(timeout=SUPERVISOR_INTERRUPT_WAIT_SECONDS)


def run_supervised(
    argv: Sequence[str],
    *,
    findings: str | os.PathLike[str],
    mode: str,
    display: str | os.PathLike[str] | None = None,
    cwd: str | os.PathLike[str] | None = None,
    timeout: float | None = None,
    spec: RoleSpec | None = None,
    supervisor: str | os.PathLike[str] | None = None,
) -> SpawnResult:
    """Launch ``argv`` through the supervisor and report what it decided.

    ``mode`` is the supervisor's result mode: ``external-file`` for a CLI that writes
    ``findings`` itself, ``raw-stdout`` for one whose reply is its stdout, which the
    supervisor writes to ``findings`` byte for byte. ``display`` receives the CLI's stdout and
    stderr as they arrive; without one they go to a private file, read back and removed. The
    role's ``env``, ``env_from_parent`` and ``backend`` are handed to the supervisor, which
    applies them to the CLI alone, so no forwarded value passes through this process's hands.

    The CLI gets stdin at end-of-file and a process group of its own, and is stopped at
    ``timeout``. The supervisor's silence limit is set to the same value: a CLI that answers
    only at the end is silent until then, and a lower one would stop it for thinking.

    Raises :class:`CliTimeoutError` for a stopped spawn, :class:`CliNotFoundError` for a CLI or
    supervisor that is not there, and :class:`CliExecutionError` for a CLI that exited non-zero
    or was not started. A CLI that exited 0 without the reply its mode expects returns
    ``ok=False``, and the caller's own check of its output decides what that means.
    """
    if not argv:
        raise CliExecutionError("cannot run an empty argv")
    # Here as well as in preflight: a resume past the round cap skips preflight and still
    # dispatches a judge.
    _refuse_batch_shim(resolve_executable(argv[0]), argv[0])
    supervisor = require_supervisor(supervisor if supervisor is not None
                                    else default_supervisor())
    limit = float(timeout) if timeout is not None else DEFAULT_SPAWN_TIMEOUT_SECONDS
    findings = Path(os.path.abspath(findings))
    scratch = Path(tempfile.mkdtemp(prefix="plan-duel-output-")) if display is None else None
    try:
        display = scratch / "output.log" if scratch is not None \
            else Path(os.path.abspath(display))
        for path in (findings, display):
            try:
                _clear_for_supervisor(path)
            except OSError as exc:
                raise CliExecutionError(
                    f"could not clear {path.name} before launching {argv[0]}: {exc}") from exc
        command = [sys.executable, str(supervisor), "--result-mode", mode,
                   "--findings", str(findings), "--display", str(display),
                   "--deadline", repr(limit), "--idle", repr(limit)]
        if cwd is not None:
            command += ["--cwd", os.path.abspath(cwd)]
        supervisor_env = None
        if spec is not None:
            if spec.backend is not None:
                command += ["--backend", spec.backend]
                # The supervisor reads the backends file on every launch, so the digest this
                # run pinned goes with the name: an entry edited mid-run is refused, not run.
                if spec.resolved is not None and spec.resolved.digest:
                    command += ["--backend-digest", spec.resolved.digest]
            # A literal value travels in the supervisor's environment, which only its owner
            # can read, and never on its argv, which every local user can. An empty one blanks
            # a key, is no secret, and is refused as a forwarded value, so it stays a flag.
            carried = {}
            for name, value in spec.env:
                if value:
                    private = f"{_LITERAL_ENV_PREFIX}{len(carried)}"
                    carried[private] = value
                    command += ["--env-from-parent", f"{name}={private}"]
                else:
                    command += ["--env", f"{name}="]
            if carried:
                supervisor_env = {**os.environ, **carried}
            for child, parent in spec.env_from_parent:
                command += ["--env-from-parent", f"{child}={parent}"]
        command += ["--", _program(argv[0]), *argv[1:]]
        try:
            proc = subprocess.Popen(command, stdin=subprocess.DEVNULL, env=supervisor_env,
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        except OSError as exc:
            raise CliExecutionError(
                f"the supervisor could not be started for {argv[0]}: {exc}") from exc
        except ValueError:
            # Never quote the error: it names a character of a setting's value.
            raise CliExecutionError(
                f"the supervisor could not be started for {argv[0]}: a setting could not be "
                f"passed in this platform's encoding") from None
        try:
            stdout, stderr = proc.communicate(timeout=limit + SUPERVISOR_GRACE_SECONDS)
        except subprocess.TimeoutExpired as exc:
            _stop_supervisor(proc)
            raise CliTimeoutError(f"CLI timed out after {limit:g}s: {argv[0]}") from exc
        except KeyboardInterrupt:
            _let_supervisor_finish(proc)
            _stop_supervisor(proc)
            raise
        except BaseException:
            # Never leave a supervisor behind, whatever raised: a KeyboardInterrupt out of the
            # wait is the live case.
            _stop_supervisor(proc)
            raise
        output = _read_output(display)
    finally:
        if scratch is not None:
            shutil.rmtree(scratch, ignore_errors=True)

    status = _supervisor_status(stdout)
    if status is None:
        detail = _tail_line(stderr.decode("utf-8", "replace"), STATUS_TAIL_CHARS)
        raise CliExecutionError(
            f"the supervisor exited {proc.returncode} without reporting on {argv[0]}"
            + (f" — {detail}" if detail else ""))
    if status["status"] == "ok":
        return SpawnResult(ok=True, reason=None, output=output)
    if status["status"] in ("deadline", "idle_timeout"):
        raise CliTimeoutError(f"CLI timed out after {limit:g}s: {argv[0]}")
    reason = str(status.get("reason") or status["status"])
    exit_code = status.get("exit_code")
    if reason == _SUPERVISOR_UNDRAINED and exit_code == 0:
        # A helper outlived the CLI holding its stdout, and the supervisor released the file
        # the CLI wrote: a spawn that was stopped, not an agent that wrote nothing. A CLI that
        # failed is reported by its exit code below, whatever its helpers did.
        raise CliTimeoutError(
            f"{argv[0]} exited, but a process it started still held its output and was not "
            f"stopped")
    if exit_code is None:
        # Never launched: a refusal, in the supervisor's own words, which name no value.
        if reason.startswith("reviewer CLI not found"):
            raise CliNotFoundError(f"CLI not found on PATH: {argv[0]}")
        if _is_pinned_backend_refusal(reason, spec):
            raise BackendChangedError(f"{argv[0]} was not started: {reason}")
        raise CliExecutionError(f"{argv[0]} was not started: {reason}")
    if exit_code != 0:
        tail = _tail_line(output.decode("utf-8", "replace"), STATUS_TAIL_CHARS)
        raise CliExecutionError(
            f"CLI exited with code {exit_code}: {argv[0]}" + (f" — {tail}" if tail else ""))
    return SpawnResult(ok=False, reason=reason, output=output)


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

    The halt needs its own reason rather than the CLI's tail alone. An agent that exits 0
    after writing a SHORT plan leaves a tail that reads like success — `wrote /…/plan-a.md`
    — beside a file that exists and looks fine, so a message built from the tail points away
    from the cause and the 200-byte floor is findable only by reading this source.
    """
    try:
        # `_present`: "was not written" is a claim about the agent, and a path whose name
        # merely would not resolve has not earned it. `_present` raises there, and the
        # handler below reports the read failure by name.
        if not _present(path):
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
            # Tolerated here and nowhere else: this builds the TAIL of a halt message that
            # is already being raised for its own reason. Skipping an unreadable capture
            # costs a sentence of explanation, never a decision — the halt happens either
            # way, and no caller derives a fact from the result.
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
    timeout: float | None = None,
    spec: RoleSpec | None = None,
    supervisor: str | os.PathLike[str] | None = None,
) -> Path:
    """Run an agent CLI and validate its FILE output (the capture policy for A/B).

    The agent writes its artifact directly; the CLI's output is only a status stream
    (``status_to``) and is NEVER the result — unless ``status_to`` IS ``output_file``, the
    role whose reply is its stdout, which the supervisor then writes there byte for byte. On
    any failure — non-zero exit, timeout, unresolvable CLI, or a missing/<200 B output file —
    the halt mirrors v1 exactly. Returns the validated ``output_file`` path.
    """
    output_file = Path(output_file)
    halt = agent_failure_message(side, round_n)
    reply_is_stdout = status_to is not None and Path(status_to) == output_file
    try:
        result = run_supervised(
            argv,
            findings=output_file,
            mode="raw-stdout" if reply_is_stdout else "external-file",
            display=None if reply_is_stdout else status_to,
            cwd=cwd,
            timeout=timeout,
            spec=spec,
            supervisor=supervisor,
        )
    except CliTimeoutError as exc:
        raise AgentOutputError(halt, cause="CLI timed out", spawn_failed=True) from exc
    except ProcessError as exc:
        raise AgentOutputError(halt, cause=str(exc), spawn_failed=True) from exc

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
        tail = status_tail(None, stdout_bytes=result.output)
        # Only where there is ALREADY a tail. The bare halt — the exact v1 line, with no
        # cause — is a parity contract pinned by three tests. A tail reading
        # `wrote /…/plan-a.md` looks like success; that is the one worth annotating, and it
        # is annotated without changing what a bare halt says.
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
    spec: RoleSpec | None = None,
    supervisor: str | os.PathLike[str] | None = None,
) -> str:
    """Capture the judge's CLEAN final message — never a raw transcript.

    Two adapter shapes are supported, both landing the clean message in
    ``message_path`` (the file the score is parsed from):

    * ``redirect_stdout=False`` (``--output-last-message``-style): the CLI writes
      ``message_path`` itself; its raw stdout (possibly a transcript that echoes
      the prompt's ``SCORE:`` template) goes to ``status_to`` and is discarded.
    * ``redirect_stdout=True`` (a clean-stdout runtime): the supervisor writes the CLI's
      stdout, and nothing else, into ``message_path``.

    Either way the returned text is read from ``message_path``, so the first
    ``SCORE:`` line parse can never be poisoned by an echoed prompt. A process
    failure or an empty/missing message raises :class:`JudgeOutputError`.
    """
    message_path = Path(message_path)
    try:
        run_supervised(argv, findings=message_path,
                       mode="raw-stdout" if redirect_stdout else "external-file",
                       display=status_to, cwd=cwd, timeout=timeout, spec=spec,
                       supervisor=supervisor)
    except ProcessError as exc:
        raise JudgeOutputError(f"Judge process failed at round {round_n}: {exc}") from exc

    # `_present`, so an unreadable capture is not charged to the judge as "produced no
    # output". It raises instead, and `main` reports the OSError with the path in it.
    if not _present(message_path) or file_size_bytes(message_path) == 0:
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
            # Plan A's round-0 snapshot lands BEFORE Agent B runs, so without this
            # guard the fallback would adopt it and make Plan B a copy of Plan A.
            continue
        # os.lstat, and the local is `info` rather than `stat`, because the name `stat`
        # shadows the module imported at the top of this file.
        #
        # Following the link here is an escape from the sandbox the participant runs in. An
        # agent confined to the workdir can still create a link inside it, and the engine is
        # unconfined: `entry.is_file()`, `entry.stat()` and `copy_bytes`' read all
        # dereference, so an outside file's bytes are published as Plan B and frozen as the
        # round-0 snapshot fed to the judge. This is the same lstat check that
        # _agent_output_is_usable and _require_regular_file apply, and the same refusal
        # of a link that open_no_follow makes on a write.
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
    all three are recognized the same way. ``match.end()`` is where the label's own inline
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
    ``justification`` the winner's defense paragraph.

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

    Decoration and case tolerant, through the patterns :func:`_label_re` builds. An
    exact-case ``line.startswith("DIFFERENCES:")`` would miss a decorated marker such as
    ``**DIFFERENCES:**`` and swallow the unmatched line into the block above it. One
    definition of "this line is the label" serves both readers.
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


def _overlay_json_fields(
    obj: Mapping[str, object], legacy: JudgeFields, carried: Collection[str] = ()
) -> JudgeFields:
    """Overlay a decoded verdict onto the marker-parsed fields, FIELD BY FIELD.

    Every field falls through to ``legacy`` unless the object carries a usable value for it
    — the same per-field degrade :func:`parse_score` and :func:`parse_preferred` implement,
    extended to the other three.

    This is what makes adopting an object non-destructive. A legacy marker file whose
    justification quotes a JSON payload could otherwise be read as a verdict, blanking the
    differences and justification the markers really carry. With the overlay, the worst case
    of a false adoption is that nothing changes.
    """
    # A partial object may be an example quoted in the prose: it fills only the fields no
    # marker line carries (``carried``), and only a complete verdict replaces what one does.
    complete = _is_complete_verdict(obj)
    differences_raw = obj.get("differences")
    if not complete and "differences" in carried:
        differences = legacy.differences
    elif isinstance(differences_raw, str):
        differences = differences_raw.strip() or legacy.differences
    elif isinstance(differences_raw, Sequence):
        differences = render_differences(list(differences_raw))
    else:
        differences = legacy.differences

    missed_raw = obj.get("missed_rejections")
    if not complete and "missed_rejections" in carried:
        missed = legacy.missed_rejections
    elif isinstance(missed_raw, (str, Sequence)):
        missed = render_missed_rejections(missed_raw)
    else:
        missed = legacy.missed_rejections

    justification = obj.get("justification")
    if not complete and "justification" in carried:
        justification = None
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
    carried = {
        name
        for name, index in (("differences", diff_idx), ("missed_rejections", missed_idx),
                            ("justification", pref_idx))
        if index is not None
    }
    return legacy if obj is None else _overlay_json_fields(obj, legacy, carried)


# Longest-first alternation so ``Stronger: A`` wins over a bare ``Plan A`` overlap;
# a single left-to-right pass means a substituted value is never re-scanned (so a
# concrete name that itself contains a token like ``B`` cannot be double-rewritten).
# Whole labels only: "Plan API" is not "Plan A" followed by "PI".
_DIFF_TOKEN_RE = re.compile(r"\b(?:Stronger: A|Stronger: B|Plan A|Plan B)\b")


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


# A markdown heading: up to three spaces of indent, since four make an indented code block.
_ATX_HEADING_RE = re.compile(r"^ {0,3}#{1,6}(?:\s|$)")


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
            if not fenced[i] and _ATX_HEADING_RE.match(line) and line.strip() == "## Status"
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
            if _ATX_HEADING_RE.match(lines[i]):
                break  # any heading, a subsection included, ends the Status section's content
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
# and a word count for an absent snapshot both use this mark, so the table has one spelling
# for "nothing to report" rather than two.
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
    winner_stamped: bool = True,
    models: Mapping[str, str | None] | None = None,
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
    # Claimed only when the stamp landed: a replay prints this line as the duel's result.
    out.append(
        f"**Winner:** {winner_name} → {workdir_display}/{winner_file} "
        + ("(stamped `Format: v2` — feed it to `/plan-phase`)" if winner_stamped
           else "(NOT stamped — see the warning above; the round snapshots hold every plan)")
    )
    # From each role's `model` field, never from a runtime's name, which is free text. A
    # duel whose roles state no model prints no Models line.
    if models and any(models.get(role) for role in REQUIRED_ROLES):
        def ran(model: str | None) -> str:
            return f"ran `{model}`" if model else "ran its CLI's default model"
        out.append(
            f"**Models:** {controller_name} {ran(models.get('agent_a'))}; "
            f"{participant_name} {ran(models.get('agent_b'))}; "
            f"the judge {ran(models.get('judge'))}."
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
    fails loud rather than spawning a half-substituted command line. ``⟪model⟫`` is filled
    from the role's own field and nothing else. ``⟪backend_args⟫`` stands alone and becomes
    the backend's arguments, one argv element each — none, for a backend with no ``args``.
    """
    if spec.backend is not None and spec.resolved is None:
        raise PlanDuelError(
            f"backend {spec.backend!r} has not been read; resolve_backends reads it")
    if spec.model is not None:
        values = {**values, "model": spec.model}
    argv: list[str] = []
    for part in spec.command:
        if part == BACKEND_ARGS_MARKER:
            argv.extend(spec.resolved.args)
        else:
            argv.append(render_template(part, values))
    return argv


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
    # Capped by length as well as by word count: one long token (a hash, an identifier)
    # otherwise makes a directory name the file system refuses.
    slug = "-".join(meaningful[:max_words])[:64].strip("-") or "duel"
    # The stem is what Windows matches, so `con.md` is reserved exactly as `con` is.
    if slug.split(".")[0] in _WINDOWS_RESERVED_NAMES:
        slug = f"{slug}-duel"
    return slug


def _round_context(workdir: Path, round_n: int) -> str:
    """The v1 'round context' sentence for a critique round's agent prompt.

    Three cases, not two: round 1, a readable prior score, and an unreadable one. The
    unreadable case gets its own sentence, naming the round, rather than round 1's: "This is
    the first critique round" at round 5 contradicts the rest of the prompt and invites the
    agent to discard four rounds of critique. A missing score is missing information, not a
    fresh start.
    """
    if round_n <= 1:
        return "This is the first critique round."
    prior = workdir / f"judge-round-{round_n - 1}.md"
    if _is_file(prior):
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
    # The supervisor every role launches through; None looks beside this skill.
    supervisor: Path | None = None
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
        # `_is_file`: the degraded branch below says the template "is missing" and
        # dispatches a one-line placeholder in its place. An unreadable template is a
        # different fact and must not be reported, or acted on, as an absent one.
        if not _is_file(path):
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

        A silent degrade is the wrong default in a tool whose next action is to spend a
        paid model call: a duel that produced nothing useful because the prompt was twelve
        words looks exactly like one whose agents were unhelpful, and costs the same.
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
            # A role whose stdout is its result has that stdout land in the plan file, as a
            # judge's lands in its message file.
            status_to=output_file if spec.stdout == "clean-last-message" else status_to,
            timeout=timeout,
            spec=spec,
            supervisor=ctx.supervisor,
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
            spec=spec,
            supervisor=ctx.supervisor,
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
    except AgentOutputError as exc:
        # Only a CLI that exited cleanly without a usable plan-b.md gets the fallback: after
        # a timeout or a failed exit, a recent stray .md is a killed agent's draft.
        if exc.spawn_failed:
            raise
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
    # re-runs a present verdict only when state.json marks that round's judge as not
    # completed, and this write (with the matching one before a re-judge) is what puts
    # that marker there; the RoundState written after the judge returns says
    # judge_completed=True.
    #
    # judge_needs_rerun treats a present, non-empty file with no marker for its round as a
    # complete verdict. Writing the marker first means a judge killed partway through
    # leaves its FRAGMENT beside judge_completed=False, so a resume re-runs it.
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
        # Read as _replay_stops_before_spawning and write_summary read it: an unreadable
        # verdict degrades like a missing one, so the replay predicted is the replay run.
        judge_text = _judge_text_or_none(judge_path) or ""
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
                    live = workdir / f"plan-{side}.md"
                    # `_is_file`: the else branch DELETES the live plan and reports the
                    # side as unwritten, so a snapshot whose name merely would not resolve
                    # would cost the user a plan and put a false line in the summary.
                    if _is_file(snapshot):
                        copy_bytes(snapshot, live)
                    else:
                        # The live plan is a later round's, and published it would carry this
                        # round's score and winner; the summary reports the side as unwritten.
                        with contextlib.suppress(FileNotFoundError):
                            live.unlink()
                        emit(
                            f"Warning: round {round_n}'s Plan {side.upper()} snapshot is "
                            f"missing, so no final plan is published for that side."
                        )
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
    # `_is_file`: a verdict whose name will not resolve is not a verdict that is missing.
    # Read as missing it is re-judged — a paid dispatch spent on a question already
    # answered, and the answer on disk overwritten by it.
    if not _is_file(path) or file_size_bytes(path) == 0:
        return True
    marker = state.rounds.get(round_n) if state is not None else None
    return marker is not None and not marker.judge_completed


def _replay_stops_before_spawning(
    workdir: Path, start_round: int, state: "RunState | None"
) -> bool:
    """Whether :func:`run_duel`'s replay of the rounds before ``start_round`` ends the duel
    without a spawn: the last of them needs no re-judge, and the exit checks already fire on
    the verdicts on disk. Such a resume only writes the summary, so it needs no CLI. Mirrors
    run_duel's preload and exit checks; a change to either changes this.
    """
    last_round = start_round - 1
    if last_round < 1 or judge_needs_rerun(workdir, last_round, state):
        return False
    scores: list[int] = []
    for round_n in range(1, last_round + 1):
        text = _judge_text_or_none(workdir / f"judge-round-{round_n}.md")
        parsed = parse_score(text) if text else None
        scores.append(0 if parsed is None else parsed)
        if evaluate_exit(round_n, scores).stop:
            return True
    return False


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
        # Marked unfinished before the dispatch, as a critique round's judge is: a re-judge
        # killed mid-write leaves a fragment that a marker still saying complete would trust.
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
    except BackendChangedError:
        raise
    except (PlanDuelError, OSError) as exc:
        # PlanDuelError is the base of JudgeOutputError and ProcessError, so this catches
        # both plus every other deliberate failure — TemplateError above all.
        # A failure after a partial write leaves bytes the summary would read back as this
        # round's score and preferred side, so they go too.
        with contextlib.suppress(OSError):
            judge_path.unlink()
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
) -> bool:
    """Stamp the winning plan with the v2 markers — but ONLY a file this run wrote. Returns
    whether the stamp was written, which the summary's Winner line reports.

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
        return False

    try:
        original = winner_path.read_bytes()
        stamped = stamp_winner_plan(read_text_roundtrip(winner_path))
    except OSError as exc:
        sys.stderr.write(
            f"Warning: could not stamp the winning plan {winner_path}: {exc}. "
            f"The summary below is complete; the plan file is not marked.\n"
        )
        return False
    try:
        # A CRLF plan stays CRLF: the stamp adds rows and must not rewrite the other lines.
        write_text_roundtrip(
            winner_path, stamped, newline="\r\n" if b"\r\n" in original else "\n"
        )
    # PlanDuelError beside OSError: the write REFUSES a winner path standing as a
    # symlink, and that refusal is not an OSError. Refusing is right; losing the
    # summary over a decoration is not — which is what this whole function says.
    except (OSError, PlanDuelError) as exc:
        sys.stderr.write(
            f"Warning: could not write the stamp back to {winner_path}: {exc}. "
            f"The summary below is complete; the plan file is not marked.\n"
        )
        return False
    return True


def _judge_text_or_none(path: Path) -> str | None:
    """A judge file's text, or ``None`` when it is absent, not a regular file, or unreadable.

    The summary is written after the duel is paid for, so an unreadable verdict degrades
    exactly as a missing one does.
    """
    try:
        # A regular file only: a named pipe at a judge path would hold the read open, outside
        # any spawn's timeout.
        if not stat.S_ISREG(os.stat(path).st_mode):
            return None
        return read_text_tolerant(path)
    except OSError:
        return None


def write_summary(
    *,
    workdir: Path,
    rounds_run: int,
    stopped_due_to: str,
    controller_name: str,
    participant_name: str,
    emit,
    models: Mapping[str, str | None] | None = None,
) -> Path:
    """Assemble + write ``summary.md`` and print it (the Step 3 orchestrator).

    Extracts the final judge fields, resolves the winner, copies the live plans to their
    slugged names, stamps ONLY the winner with the v2 markers, builds the score-trajectory
    table, and applies the scoped A/B → name rewrite to the differences block.

    **Nothing missing from the workdir stops it.** This runs after the duel has been paid
    for, so every read here degrades and says so: an absent final judge yields empty fields,
    an absent snapshot a ``—`` word count, an absent live plan a warned skip. Letting any of
    them raise would throw away a duel that has already finished and been paid for.
    """
    controller_slug = slugify_name(controller_name)
    participant_slug = slugify_name(participant_name)

    # The final judge file is normally written by the loop, but a resume of a round-10 duel
    # interrupted before its judge (snapshots are written first) reaches here with it absent.
    # Guard the read like every other judge read and degrade to empty fields — v1 treats a
    # missing final score as 0 and still emits summary.md.
    judge_path = workdir / f"judge-round-{rounds_run}.md"
    judge_text = _judge_text_or_none(judge_path)
    if judge_text is None:
        # No readable file, so there is no number to name — score_warning yields the
        # unparseable form here. An out-of-range score was already warned about by the
        # round that produced it (or by the resume preload).
        judge_text = ""
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

    stamped = _stamp_winner(workdir, winner_file, written_finals, emit)

    # Word counts are Optional: a snapshot that is absent scores `—` rather than
    # raising. See :func:`word_count_file`.
    trajectory: list[tuple[int, int | None, int | None, int | None]] = []
    for n in range(0, rounds_run + 1):
        a_words = word_count_file(workdir / plan_snapshot_name("a", n))
        b_words = word_count_file(workdir / plan_snapshot_name("b", n))
        if n == 0:
            score: int | None = None
        else:
            judge_n_text = _judge_text_or_none(workdir / f"judge-round-{n}.md")
            parsed = parse_score(judge_n_text) if judge_n_text is not None else None
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
        winner_stamped=stamped,
        models=models,
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


def _path_test(test: Callable[[Path], bool], path: Path) -> bool:
    """``test(path)``, answering False for a name too long to be a path.

    The positional argument is probed as a path before it is taken as inline text, and an
    ordinary paragraph is longer than one path component may be. Before Python 3.13 pathlib
    raises that ENAMETOOLONG from ``is_dir`` and ``is_file`` instead of answering False.
    """
    try:
        return test(path)
    except OSError as exc:
        if exc.errno == errno.ENAMETOOLONG:
            return False
        raise


def _resolve_problem_statement(argument: str | None) -> str:
    """Resolve the new-run problem statement (file path → contents, else inline)."""
    if not argument:
        raise PlanDuelError(
            "No problem statement provided. Pass inline text or a file path."
        )
    candidate = Path(argument)
    if _path_test(Path.is_file, candidate):
        try:
            return read_text_normalized(candidate)
        except UnicodeDecodeError as exc:
            raise PlanDuelError(
                f"{argument} is not UTF-8 text ({exc.reason} at byte {exc.start}); "
                f"save it as UTF-8 and re-run."
            ) from exc
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
    # `_is_file` on all three. False here refuses the resume, which is the safe direction
    # — but it refuses it with a message telling the operator the directory holds none of
    # this tool's artifacts, which is a claim about the tree, not about an unresolvable
    # name.
    if _is_file(path / DUEL_MARKER_FILENAME):
        return True
    if not _is_file(path / "problem.md"):
        return False
    if _is_file(path / STATE_FILENAME):
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
    # marker the legacy test in _looks_like_duel_workdir still recognizes the workdir
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
    supervisor: str | os.PathLike[str] | None = None,
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
    supervisor_path = (Path(os.path.abspath(supervisor)) if supervisor
                       else default_supervisor())

    # Resolved once; the pre-flight below is deliberately NOT run here. A resume that
    # spawns nothing — replaying a finished duel's summary.md — must stay CLI-free and
    # schema-free, exactly as preflight_executables already is, so both checks sit
    # together at the two points where a judge will actually be dispatched.
    schema_values = schema_placeholder_values(skill_dir_path)

    # RESUME INTENT COMES FROM THE POSITIONAL ARGUMENT ALONE, which is what SKILL.md
    # documents: --workdir only chooses where a NEW run lands. A --workdir holding a
    # problem.md is never read as a resume. With no positional argument the new-run branch
    # refuses it and names the positional form; with one, _resolve_new_workdir refuses it as
    # not empty. Either way the new problem statement is not discarded, and apply_resume,
    # which deletes files matching patterns as broad as plan-*.md, never runs in a directory
    # the user did not ask to continue.
    # Before anything is dispatched or created: two names that slugify alike would send both
    # final plans to one filename, and the summary would still claim two.
    require_distinct_slugs(controller_name, participant_name)

    resume_dir: Path | None = None
    if argument:
        path = Path(argument)
        if _path_test(Path.is_dir, path) and _is_file(path / "problem.md"):
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
        elif _path_test(Path.is_dir, path):
            raise PlanDuelError(
                f"{argument} is a directory with no problem.md, so it is neither a duel to "
                f"resume nor a problem statement. Pass a duel workdir to resume one, or a "
                f"problem file or the problem as text to start one."
            )

    if resume_dir is not None:
        workdir = resume_dir
        ctx = DuelContext(workdir, controller_name, participant_name, skill_dir_path,
                          supervisor_path)
        ctx.started_monotonic = time.monotonic()
        plan = compute_resume(workdir)
        if plan.complete:
            # Tolerant: write_text_atomic keeps a non-UTF-8 byte of the workdir path with
            # surrogateescape, which a strict read of the same file refuses.
            emit(read_text_tolerant(workdir / "summary.md"))
            return 0
        # Before apply_resume deletes anything: a missing CLI must not cost the user their
        # artifacts. Skipped when this resume will spawn nothing — a duel whose rounds are all
        # complete but whose summary.md is missing only needs that summary written, so
        # requiring the CLIs would block recovering it. A resume PAST the cap can dispatch one
        # judge to recover the last round's verdict, and this condition still does not require
        # the CLIs for it: the re-judge degrades to v1's 0 and says so, an announced cost.
        saved_state = load_state(workdir)
        if saved_state is not None and saved_state.controller_name and (
            (saved_state.controller_name, saved_state.participant_name)
            != (controller_name, participant_name)
        ):
            raise PlanDuelError(
                f"{workdir} was started with {saved_state.controller_name} as controller and "
                f"{saved_state.participant_name} as participant, but this resume names "
                f"{controller_name} and {participant_name}. Plan A and Plan B belong to the "
                f"runtimes that wrote them, so resume with the names the duel started with."
            )
        # A backend is where a role's model is stated, so it is read before any model is
        # compared or reported — but only by a resume that launches something. A replay that
        # only rebuilds the summary needs neither the supervisor nor the backend, and takes a
        # backend role's model from the record of the duel it replays.
        launches = plan.init_incomplete or not _replay_stops_before_spawning(
            workdir, plan.start_round, saved_state)
        specs = (resolve_backends(specs, supervisor_path) if launches
                 else _models_from_record(specs, saved_state.lineup if saved_state else {}))
        current_lineup = frozen_lineup(specs)
        if saved_state is not None:
            # A resume that reads no backend has no digest of its own; the one recorded for
            # the same backend is carried forward, or saving this lineup would erase it and
            # the next resume could not tell an edited backend from the one that played.
            for role, record in current_lineup.items():
                before = saved_state.lineup.get(role, {})
                if ("backend_digest" not in record and "backend_digest" in before
                        and before.get("backend") == record.get("backend")):
                    record["backend_digest"] = before["backend_digest"]
            kept = _kept_players(plan, workdir)
            changes = lineup_changes(
                {role: record for role, record in saved_state.lineup.items() if role in kept},
                current_lineup,
            )
            if changes:
                raise PlanDuelError(
                    f"{workdir} cannot resume with this adapter config, because a resume "
                    f"cannot change who plays: {'; '.join(changes)}. The work this resume "
                    f"keeps came from those players, so resume with the CLI, model, backend "
                    f"and credential names the duel started with, or start a new duel. Other "
                    f"changes to a command, such as an added flag, do resume."
                )
        # Nor when the verdicts on disk already end the duel: replaying them spawns nothing.
        if plan.init_incomplete or (plan.start_round <= MAX_ROUNDS and launches):
            require_supervisor(supervisor_path)
            preflight_executables(specs)
            preflight_launch(specs)
            preflight_schema(specs, schema_values)
        for name in apply_resume(plan):
            emit(f"Deleted {name}")
        if plan.message:
            emit(plan.message)
        state = load_state(workdir) or RunState(controller_name, participant_name)
        state.controller_name = controller_name
        state.participant_name = participant_name
        state.lineup = current_lineup
        save_state(workdir, state)
        if plan.init_incomplete:
            run_init_round(
                workdir=workdir, specs=specs, ctx=ctx, emit=emit,
                timeout=timeout, state=state, reuse_plan_a=plan.reuse_plan_a,
            )
            start_round = 1
        else:
            start_round = plan.start_round
    else:
        # Check the supervisor, every CLI and what each launch needs — and the schema an
        # adapter's argv may need — before creating a workdir or spending a plan run on one.
        specs = resolve_backends(specs, supervisor_path)
        require_supervisor(supervisor_path)
        preflight_executables(specs)
        preflight_launch(specs)
        preflight_schema(specs, schema_values)
        # The other way into the refusal above, and the one whose default message would
        # be unhelpful: `--workdir <a duel>` with NO positional argument does not resume.
        # Say what to type, rather than "no problem statement provided".
        if not argument and workdir_arg and _is_file(Path(workdir_arg) / "problem.md"):
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
        ctx = DuelContext(workdir, controller_name, participant_name, skill_dir_path,
                          supervisor_path)
        ctx.started_monotonic = time.monotonic()
        state = RunState(controller_name, participant_name, lineup=frozen_lineup(specs))
        # Before round 0 spawns anything: a validated Plan A is kept for reuse the moment it
        # exists, and a resume may reuse it only against the lineup that wrote it.
        save_state(workdir, state)
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
        models={role: spec.model for role, spec in specs.items()},
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
        "killed and halts the duel, except a resume's judge re-run, which scores that "
        "round 0; there is no way to disable the bound.",
    )
    parser.add_argument(
        "--supervisor",
        help="Path to the diff-review skill's review_runner.py, which launches every role "
        "(default: the one in the diff-review skill installed beside this one).",
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
    except UnicodeDecodeError as exc:
        sys.stderr.write(
            f"plan-duel: {args.adapter_config} is not UTF-8 text ({exc.reason} at byte "
            f"{exc.start}); save it as UTF-8 and re-run.\n"
        )
        return 2
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
            supervisor=args.supervisor,
        )
    except PlanDuelError as exc:
        sys.stderr.write(f"{exc}\n")
        return 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        # 130 is what a shell reports for SIGINT. No traceback: frames of engine internals
        # tell the user nothing they can act on, and the one thing they need is that the
        # duel is resumable: the workdir holds every completed round.
        # Names the POSITIONAL form, because that is the only one that resumes. `--workdir`
        # only ever chooses where a NEW run lands, and _resolve_new_workdir refuses a
        # directory that is not empty, so the same command with the same --workdir fails.
        print("\nInterrupted. The duel is resumable — re-run with the workdir as the "
              "POSITIONAL argument (not --workdir) and it continues from the last "
              "completed round.", file=sys.stderr)
        raise SystemExit(130)
