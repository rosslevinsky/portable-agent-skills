#!/usr/bin/env python3
"""Validate skill files against the cross-platform portability contract.

Checks:
- Banned phrases do not appear as normative instructions (outside adapter notes and
  classification declarations)
- Skills classified as Degraded or Runtime-limited carry a `_Classification:` declaration
  line — the word in prose does not stand in for one, and neither does a declaration
  quoted inside a code block
- Companion-skill references (backtick-quoted skill names like `cyw`) include fallback
  instructions nearby
- PORTABILITY.md exists with required sections
- Private paths and project-specific identifiers do not leak into skill files
- plan-duel companion files (init.md, round.md, summary.md) AND the bundled plan_duel.py
  engine exist when plan-duel/SKILL.md is present
- no bundled engine hardcodes a branded CLI name (claude/codex) as a subprocess
  invocation — branded CLIs arrive as argv data from the adapter config. AST-checked, so
  a branded word in a comment, docstring or error string is not flagged
- skills that dispatch sub-agents declare a `_Progress:` posture (observable or bounded):
  a declaration contract, not a runtime guarantee
- installed skill files are self-contained: no reference to repo-root docs and no relative
  path climbing out of skills/<name>/, since the installer ships skill directories only
- each skill's frontmatter `name:` equals its directory basename, since every other rule
  derives names from paths alone
- routing inside a superseded `plan-*-v1` skill runs both ways: intra-suite references
  (siblings AND self) are `-v1`-qualified, while the forward redirect in the `Format: v2`
  refusal guard stays unqualified
- a `-v1` skill's description opens with its choose-me condition and never shares that
  opening sentence with its unqualified counterpart

Usage:
    python3 scripts/validate_cross_runtime.py [skills/]
    python3 scripts/validate_cross_runtime.py --test-fixtures <fixture-dir>

`--test-fixtures` names no fixed path: the published pack carries no test directory, and a
usage line promising one would be an instruction its reader cannot follow.

The plan tracker check lives in `scripts/check_plan_tracker.py`: it checks one document
format the planning suite invented, while this file checks that skills are portable.
"""

import argparse
import ast
import contextlib
import io
import json
import os
import posixpath
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from urllib.parse import unquote, urlsplit

BANNED_PHRASES = [
    "Glob tool",
    "Grep tool",
    "Read tool",
    "Edit tool",
    "Write tool",
    "Bash tool",
    "Agent tool",
    "subagent_type",
    "run_in_background",
]

# Private paths and project-specific identifiers that must not appear in skills.
#
# Only the SHAPES that are private for everyone live here. Your own project names do not
# belong in this file: it is version-controlled, so a blocklist of internal repo names
# would disclose exactly what it exists to keep unpublished. Put those in the optional
# side file below.
PRIVATE_PATH_PATTERNS = [
    # Home directories on every platform this pack supports, not just Linux. The macOS
    # and the Windows form went uncaught until a review pasted one into an installer and
    # watched it pass — and macOS is in the CI matrix, so it is where a real one comes from.
    re.compile(r"/home/[^/\s]+"),           # hygiene-exempt: this IS the pattern
    re.compile(r"/Users/[^/\s]+"),          # hygiene-exempt: this IS the pattern
    re.compile(r"[A-Za-z]:\\Users\\[^\\\s]+"),
    # Bare 'dotfiles' / '~/projects' match the breadth of the retired CI grep  # hygiene-exempt: names the patterns
    # steps for skill text; the doc scan below narrows 'dotfiles' to the path  # hygiene-exempt: names the patterns
    # form because README legitimately uses the word in prose.
    re.compile(r"\bdotfiles\b"),
    re.compile(r"~/projects"),  # hygiene-exempt: this IS the pattern
    # This repository's own GitHub handle. It is already published in README,
    # SECURITY.md and CODEOWNERS, so listing it discloses nothing, and the doc-scan
    # carve-out below has to name it either way. Replace it with your own if you fork.
    re.compile(r"\brosslevinsky\b"),
]

# Extra identifiers, one regex per line, '#' comments and blank lines ignored. This file
# is optional and absent by default — a clone that declares no private names of its own
# needs none, and one that does should keep the file unpublished.
EXTRA_PRIVATE_IDENTIFIERS = Path(__file__).resolve().parent / "private-identifiers.txt"


def _load_extra_private_patterns(path: Path) -> list[re.Pattern]:
    """Compile the workspace's own private identifiers, if it declared any.

    A missing file is the normal case and returns nothing. A malformed line raises:
    a private-name guard that silently drops a pattern it could not compile is worse
    than no guard, because the operator believes they are covered.
    """
    if not path.is_file():
        return []
    out = []
    for lineno, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        try:
            out.append(re.compile(line))
        except re.error as exc:
            raise ValueError(f"{path}:{lineno}: not a valid regex: {exc}") from exc
    return out


PRIVATE_PATH_PATTERNS += _load_extra_private_patterns(EXTRA_PRIVATE_IDENTIFIERS)

# README only, and CONTRIBUTING.md / install.py are deliberately NOT here. Extending the
# Codex-path rule to them flags exactly two lines, and both are correct — each WARNS
# against the wrong path. The rule cannot tell "uses the wrong path" from "warns against
# it", so extending it buys two exemptions and no coverage. What actually breaks a user is
# the installer's target, asserted directly by `tests/test_install_py.py`.
DOC_ARTIFACTS = ["README.md"]
DOC_PRIVATE_PATH_PATTERNS = [
    pattern
    for pattern in PRIVATE_PATH_PATTERNS
    if pattern.pattern not in (r"\brosslevinsky\b", r"\bdotfiles\b")
] + [re.compile(r"dotfiles/claude/skills")]  # hygiene-exempt: this IS the pattern

HARDCODED_ATTRIBUTION_PATTERNS = [
    re.compile(r"noreply@anthropic\.com", re.IGNORECASE),
    re.compile(r"noreply@openai\.com", re.IGNORECASE),
]

STALE_RUNTIME_CLAIM_PATTERNS = [
    re.compile(r"Codex cannot act as controller because it lacks", re.IGNORECASE),
    re.compile(r"Codex.*lacks sub-agent", re.IGNORECASE),
    re.compile(r"Not supported in single-agent runtimes \(e\.g\., Codex\)", re.IGNORECASE),
]

# --- self-contained-install contract -----------------------------------------
# The installer copies skills/<name>/ directories only; repo-root files (PORTABILITY.md,
# README, scripts/, tests/) are NOT shipped. So an installed skill file must not reference
# a repo-root doc, nor climb out of its own skills/<name>/ directory.
#
# The doc names listed here exist only at the repo root. README.md / CHANGELOG.md /
# CONTRIBUTING.md are EXCLUDED — skills legitimately reference same-named files in the
# user's project — and a path-based reference to those is still caught by the '../' rule.
NON_INSTALLED_ROOT_DOCS = ["PORTABILITY.md"]

# The same defect as the `../` escape, in the spelling the escape rule cannot see. `../` is
# caught by counting how far a path climbs; `skills/plan-duel/x.md` climbs nowhere — it
# descends from a root that exists in this repository and in no install, because each
# `skills/<name>/` is shipped on its own with no `skills/` parent above it.
#
# Requires a path SEPARATOR after the skill name, so the prose phrase "the skills/ directory"
# is not matched: naming the directory is not referencing a file in it. Zero shipped skills
# do this today, so it is a regression guard.
SIBLING_SKILL_PATH_RE = re.compile(r"\bskills[\\/][A-Za-z0-9_.-]+[\\/][^\s`'\"<>|,;)\]]+")

# Codex's documented user skills path is $HOME/.agents/skills (developers.openai.com/
# codex/skills; empirically confirmed on codex-cli 0.142.5). Skill files and the
# README must point users at that canonical path and must not steer them at the
# ~/.codex/skills location.
# The invocation form of a skill reference. The whole backtick span must BE the command,
# which is what keeps `plans/<slug>/plan.md` from reading as a reference to `/plan`.
SLASH_COMMAND_RE = re.compile(r"`/([a-z][a-z0-9-]*)`")
# Backtick-slash tokens that are not this pack's skills. Enumerated rather than pattern-
# matched, and short by measurement: these are the only two in the shipped tree.
NON_SKILL_SLASH_COMMANDS = frozenset({
    "review",  # Codex's own native command, named by diff-review as the rung-2 alternative
    "tmp",     # a filesystem path that happens to be written in backticks
})

CODEX_SKILL_PATH_PATTERNS = [
    re.compile(r"~/\.codex/skills"),
    re.compile(r"\$HOME/\.codex/skills"),
    re.compile(r"\$CODEX_HOME/skills"),
]

# Companion skills that require fallback instructions when referenced
COMPANION_SKILLS = ["cyw", "tdd", "web-verify", "diff-review"]

# Words that indicate a fallback is present near a companion-skill reference
FALLBACK_INDICATORS = [
    "unavailable",
    "if available",
    "if the skill is unavailable",
    "if unavailable",
    "otherwise",
    "fallback",
    "manual",
    "equivalent",
    "if supported",
    "if direct skill invocation is unavailable",
]

# EVERY top-level section of the contract, not a chosen subset. A mapping covering only
# some sections lets the rest be rewritten or deleted with nothing noticing, and it is the
# only form that survives a section being added.
#
# This checks that a section EXISTS, never that a skill obeys it — those rules live in the
# `check_*` functions below, each naming the section it implements. An empty tuple means
# the section is prose with no lexical rule behind it, stated rather than inferred.
#
# `tests/test_skill_content.py` holds this to the module: every name here must be a real
# function.
PORTABILITY_SECTION_CHECKS: dict[str, tuple[str, ...]] = {
    "Allowed": ("check_banned_phrases",),
    "Banned": ("check_banned_phrases",),
    "Companion": ("check_companion_skill_fallbacks",),
    "Agent.*Instruction": ("check_codex_skill_paths",),
    "Parallel": (),
    "Independent Verification": ("check_independence_ladder",),
    "Autonomous": ("check_companion_skill_fallbacks",),
    "Inline.*Adapter": ("check_spawn_permissions",),
    "Progress": ("check_progress_declaration",),
    "Bundled Executables": ("check_engine_portability",),
    "Windows Link Hazards": ("check_refused_symlinks",),
    "Structured Machine-Read": ("check_shipped_json",),
    "Shell Assumptions": (),
    "Genericity": ("check_private_paths", "check_hardcoded_attribution"),
    "Portability Classifications": ("check_classification",),
    "v2 Planning-Workflow": ("check_v1_suite_routing",),
    "Verifying a skill pack": (),
}

REQUIRED_PORTABILITY_SECTIONS = list(PORTABILITY_SECTION_CHECKS)

PLAN_DUEL_COMPANIONS = ["init.md", "round.md", "summary.md"]

# The bundled stdlib-only engine plan-duel ships alongside SKILL.md. It is a
# SEPARATE require from the .md companions (the companion set above is unchanged).
PLAN_DUEL_ENGINE = "plan_duel.py"

# The judge's structured-output schema, also a SEPARATE require. Without it, an adapter
# referencing ⟪schema_path⟫ / ⟪schema_json⟫ cannot render its judge command at all — so a
# pack that ships the engine but not the schema is broken, not merely degraded.
PLAN_DUEL_SCHEMA = "judge-schema.json"

# Branded participant-CLI names that must NEVER be hardcoded as a subprocess
# invocation inside the bundled engine — they arrive only as argv DATA injected
# from the SKILL.md adapter config (the PORTABILITY contract). A branded word in a
# comment, docstring, or error-message string is fine; only a branded string literal
# used inside a subprocess-style spawn call is a violation.
BRANDED_CLI_NAMES = ("claude", "codex")
_BRANDED_CLI_RE = re.compile(
    r"\b(" + "|".join(BRANDED_CLI_NAMES) + r")\b", re.IGNORECASE
)
# Spawn functions whose argv must not carry a hardcoded brand. `getoutput`, the `os.exec*`
# / `os.spawn*` family and asyncio's spawns are all ways to start a process with an argv,
# and a narrower list checked none of them. Nothing in the tree used them — this closes the
# gap between the rule and its own description rather than fixing a live defect.
#
# Only `skills/**/*.py` is scanned, which is what makes generic names like `system` safe:
# the two bundled engines are the whole population.
_SUBPROCESS_SPAWN_FUNCS = frozenset({
    "run", "Popen", "call", "check_call", "check_output",
    "getoutput", "getstatusoutput", "system",
    "execv", "execve", "execvp", "execvpe", "execl", "execle", "execlp", "execlpe",
    "spawnv", "spawnve", "spawnvp", "spawnvpe", "spawnl", "spawnle", "spawnlp", "spawnlpe",
    "create_subprocess_exec", "create_subprocess_shell",
})

# Classification markers that trigger the Classification declaration check
CLASSIFICATION_REQUIRED_MARKERS = ["Degraded", "Runtime-limited"]
# What a declaration actually is: a PROSE line whose first non-whitespace text is this
# prefix. Named once and used everywhere it is parsed — discovery, the exempt-line rule and
# the check itself — so the three cannot drift into disagreeing about what they are
# reading. `prose_lines` below decides the prose half the same way for all three.
CLASSIFICATION_DECL_PREFIX = "_Classification:"
# A CommonMark fence line: three or more backticks or tildes, indented at most three
# spaces. The fourth space makes it indented-code CONTENT instead, which is the same
# boundary `INDENTED_CODE_COLUMNS` draws below and the same one the bundled plan-duel
# engine draws in `_FENCE_RE`.
_MD_FENCE_RE = re.compile(r"^ {0,3}(?P<fence>`{3,}|~{3,})(?P<info>.*)$")
# Four columns of indentation start a code block; three or fewer is an ordinary
# paragraph. That is why an indented declaration is still a declaration, and it is the
# whole reason this boundary is enforced rather than "ignore indented lines".
INDENTED_CODE_COLUMNS = 4
# The COMPLETE list of shipped skills whose classification is Degraded or Runtime-limited.
# It must name every one, because the content scan below finds a skill by reading the very
# line this rule exists to require: a skill known only by its declaration stops being
# checked the moment someone deletes it, so the rule switches itself off exactly when it is
# broken.
#
# A skill NOT listed is still caught while its declaration exists, so a downstream pack's
# own Degraded skills are covered without appearing here — what the list buys is that
# deleting a declaration from one of OURS is an error, not a silent exemption.
CLASSIFICATION_REQUIRED_SKILLS = {
    "demo-video",
    "diff-review",
    "plan-duel",
    "security-review-codebase",
    "web-verify",
}

# --- progress-reporting declaration contract ----------------------------------
# A skill that dispatches sub-agents must declare a `_Progress:` posture so the choice is
# conscious and auditable (PORTABILITY.md, "Progress Reporting"):
#   observable — offers an append-only, non-blocking, off-correctness-path progress file
#   bounded    — request/response dispatch; the job returns its result, nothing to observe
# This enforces the CONTRACT, never runtime behavior: a progress file is read by nothing on
# the correctness path, so no check can prove a prose worker actually wrote to it.
AGENT_DISPATCH_SKILLS = {
    "plan-run",
    "plan-duel",
    "diff-review",
    "security-review-codebase",
}
# **The curated set above is the rule. This marker is a safety net, not a detector.**
# It catches a skill that mentions a sub-agent and was not thought of, while
# `AGENT_DISPATCH_SKILLS` is where a dispatcher is declared.
#
# Deliberately not widened to `spawn … agent`, `delegate … agent`, `parallel agents`. Those
# read as ordinary prose in skills that dispatch nothing, so they would fire on description
# rather than behaviour — and a rule that fires on description is one authors phrase around.
AGENT_DISPATCH_MARKERS = [
    re.compile(r"\bsub-?agents?\b", re.IGNORECASE),
]
PROGRESS_DECL_PREFIX = "_Progress:"
PROGRESS_DECL_POSTURES = ("observable", "bounded")
# The posture is a WHOLE WORD, and trailing prose is allowed after it — real declarations
# read `_Progress: observable via a run-level progress.log`. With `startswith`, that
# allowance swallowed the word boundary too: `boundedish nonsense` and `observableness`
# both declared a valid posture.
PROGRESS_DECL_RE = re.compile(
    r"(?:" + "|".join(PROGRESS_DECL_POSTURES) + r")\b", re.IGNORECASE)

# The superseded planning suite. A skill's directory name IS its installed name on
# both runtimes, so the generation is carried by a `-v1` suffix rather than a
# namespace: Codex reads a flat ~/.agents/skills, and `v1:plan-init` is not a legal
# Windows directory name.
V1_SUFFIX = "-v1"
V1_SUITE_BASES = ("plan-init", "plan-phase", "plan-run")
V1_SUITE_SKILLS = frozenset(f"{base}{V1_SUFFIX}" for base in V1_SUITE_BASES)
# `plan-init-v1` must not read as an unqualified `plan-init`; `plan-runner` must not read
# as `plan-run`; `xplan-run` is a different token. The trailing class guard rejects a
# longer word while allowing the `-` that starts a suffix, and the leading one rejects a
# longer word to the left — placed so it applies to the `/` in `/plan-run` and to `plan`
# in `skills/plan-run/`, both of which must still match.
_V1_UNQUALIFIED_REF_RE = re.compile(
    r"(?<![A-Za-z0-9_])/?plan-(?:init|phase|run)(?!-v1\b)(?![A-Za-z0-9])"
)
# A forward redirect must name the canonical suite EXACTLY — any suffix is wrong, not
# just `-v1`. Any suffix at all is the case that matters: a redirect carrying one names
# a directory that does not exist, and a rule banning only `-v1` would wave it through.
_V1_SUFFIXED_REF_RE = re.compile(
    r"(?<![A-Za-z0-9_])/?plan-(?:init|phase|run)-[A-Za-z0-9][\w-]*"
)
# The v1 skills refuse a v2 plan and point forward at the canonical suite. That guard
# is the one place inside a v1 body where an UNQUALIFIED name is correct, and it is
# identified by the marker it tests for.
V2_FORMAT_MARKER = "Format: v2"
# The generation as a whole token, so none of "v10", "env1", "v1alpha" or "v1_beta"
# reads as "v1".
_V1_TOKEN_RE = re.compile(r"(?<![A-Za-z0-9_])v1(?![A-Za-z0-9_])", re.IGNORECASE)
# A description's opening sentence ends at the first terminator followed by
# whitespace or end of text.
_SENTENCE_BREAK_RE = re.compile(r"(?<=[.!?])(?:\s|$)")


BUILD_RESIDUE_DIRS = frozenset({"__pycache__", ".git", ".pytest_cache"})

# The ledger is exempt from the word budget BY RELATIVE PATH, not by basename. Matched both
# ways, a `references/DECISIONS.md` was counted while the root one was not. One rule now:
# the ledger is the file at the skill root, and anything else called DECISIONS.md is
# ordinary prose, budgeted and ruled like it. No skill has one today, so this moves no
# number; it decides the question before a file forces an answer.
LEDGER_FILENAME = "DECISIONS.md"


def _resolves_elsewhere(entry: Path, expected: str) -> bool:
    """Is this directory entry a link — of any kind — rather than a real directory?

    **Asked as "where does it land", never as "is it a symlink".** ``Path.is_symlink()``
    returns False for a **Windows directory junction**, which ``os.walk`` descends whatever
    ``followlinks`` says, so a junction inside a skill pulls an external tree in to be
    scanned and budgeted as that skill's own content. Comparing the resolved path against
    the one this entry ought to have covers junctions, symlinks and whatever else an
    operating system offers.

    ``expected`` is the parent's *already resolved* path joined with the entry's name, not a
    prefix test against the tree root. Containment fails twice over: a link pointing back
    INSIDE the tree passes it, and a junction resolving to the root itself satisfies it, so
    a loop is descended until the path length gives out. Equality has no such gap.

    An ``OSError`` answers "refuse it". **Do not replace this with ``entry.is_symlink()``**:
    that leaves the suite green and the validator passing, because nothing here can *create*
    a junction on any platform, so no test reaches the branch.
    """
    try:
        return os.path.normcase(os.path.realpath(entry)) != os.path.normcase(expected)
    except OSError:
        return True


def _expected_real_path(target: Path) -> str:
    """Where ``target`` would resolve to if it were an ordinary directory.

    ``abspath`` first, because the comparison is between two spellings of a location and
    only one is canonical. Without it, ``_walk_tree(Path("."))`` compared ``/cwd`` against
    ``/cwd/.`` and refused the current directory as a link. ``normcase`` in
    :func:`_resolves_elsewhere` covers the other half on Windows, where ``realpath`` returns
    the filesystem's canonical casing.
    """
    absolute = os.path.abspath(target)
    return os.path.join(
        os.path.realpath(os.path.dirname(absolute)), os.path.basename(absolute)
    )


class UnreadableTree(Exception):
    """A directory could not be read, so the walk that reported on it is incomplete.

    Raised rather than swallowed. A traversal that silently returns fewer files than the
    tree contains reports success for prose it never looked at: the run is green and the
    evidence is absent.

    Caught once, in :func:`validate_skills`, and turned into an ordinary error line — an
    exception that escapes to the top is a traceback, from a module whose whole job is to
    say which file.
    """


def _walk_tree(root: Path):
    """Yield ``(path, relative, suffix, is_symlink)`` for every entry under ``root``.

    **The mechanics of walking, and nothing about scope.** Which files a rule cares about
    stays with that rule; shared here is what was silently different at every call site —
    how directories are descended, whether an error is noticed, where residue is dropped,
    whether case is folded.

    **:func:`os.walk`, deliberately, rather than ``Path.rglob("*")``**, for two reasons:

    * **Symlinked directories.** ``rglob``'s non-descent is incidental behaviour, and 3.13
      changed the surface around it. A file *inside* a symlinked directory is not itself a
      symlink, so a per-entry ``is_symlink()`` filter lets every one through if the walk
      descends. Locked twice — ``followlinks=False`` and the explicit ``dirnames`` prune —
      each sufficient alone, since the guard's own test can only see both lost at once.
    * **Unreadable directories.** ``rglob`` swallows :class:`OSError`, so a directory at mode
      ``0o000`` yields a silently short list and every check still passes. ``onerror`` turns
      that into :class:`UnreadableTree`.

    ``suffix`` is pre-lowered so no caller can forget to fold case, and ``relative`` is
    yielded because :func:`check_self_contained_skill_refs` derives its ``depth`` from it.

    Symlinks are reported, never followed: validating a link validates whatever it resolves
    to, which defeats self-containment.
    """
    # The ROOT is checked too, and it was not. `is_dir()` dereferences, and
    # ``followlinks=False`` governs descendants only — so a `skills/` that was itself a link
    # was walked in full, and every guarantee this module makes became a claim about a tree
    # the repository does not contain. Raising rather than returning empty: an empty walk of
    # a linked root is indistinguishable from an empty directory.
    if _resolves_elsewhere(root, _expected_real_path(root)):
        raise UnreadableTree(
            f"  {root}: this directory is a link, so it was not walked — validating a link "
            f"validates whatever it resolves to, which is not what ships"
        )
    if not root.is_dir():
        return

    problems: list[OSError] = []
    for dirpath, dirnames, filenames in os.walk(
        root, onerror=problems.append, followlinks=False
    ):
        here = Path(dirpath)
        # Resolved fresh per directory rather than composed from the root, so a link that
        # slipped in above this point cannot make every entry below it look ordinary.
        real_here = os.path.realpath(dirpath)
        keep = []
        for name in sorted(dirnames):
            if name in BUILD_RESIDUE_DIRS:
                continue
            entry = here / name
            if _resolves_elsewhere(entry, os.path.join(real_here, name)):
                yield entry, entry.relative_to(root), "", True
            else:
                keep.append(name)
        dirnames[:] = keep
        for name in sorted(filenames):
            entry = here / name
            relative = entry.relative_to(root)
            if BUILD_RESIDUE_DIRS.intersection(relative.parts):
                continue
            yield entry, relative, entry.suffix.lower(), entry.is_symlink()

    if problems:
        raise UnreadableTree(
            f"  {root}: {len(problems)} director(y/ies) could not be read, so this tree was "
            f"only partially scanned and the result below covers less than it claims: "
            f"{problems[0]}"
        )


def iter_skill_roots(skills_dir: Path, suffix: str = ""):
    """Yield each skill's ``SKILL.md``, refusing a symlinked skill root.

    **"Which skills exist" is a different question from "which files belong to one",** and
    conflating them is how the root guard went missing: six call sites asked this one, all
    spelled ``glob("*/SKILL.md")``, and none refused a link — so the *file* walk could refuse
    every symlink inside a skill and still validate a skill that was itself a link out of
    the tree.

    Refusal here is silent by design and reported by :func:`symlinked_skill_roots`, which
    walks the same predicate.
    """
    for skill_md in sorted(skills_dir.glob(f"*{suffix}/SKILL.md")):
        if skill_md.parent.is_symlink() or skill_md.is_symlink():
            continue
        yield skill_md


def symlinked_skill_roots(skills_dir: Path):
    """Yield the skill roots :func:`iter_skill_roots` refuses, so the refusal is reported.

    Same predicate, deliberately. A skill silently skipped is indistinguishable from a skill
    that passed -- the failure mode this whole phase exists to remove.
    """
    for skill_md in sorted(skills_dir.glob("*/SKILL.md")):
        if skill_md.parent.is_symlink() or skill_md.is_symlink():
            yield skill_md.parent


def walk_tree_files(root: Path):
    """Yield ``(path, relative, suffix)`` for every real file under ``root``.

    One walk, one place case is folded, one place residue is dropped — see :func:`_walk_tree`
    for why each belongs to the walk rather than its callers. What a caller does with a file
    still differs legitimately, and so does **the scope**: ``root`` is one skill for the rules
    that ask about a skill and the whole ``skills/`` directory for the two that ask about
    everything shipped. Passing the root is how a caller states which question it is asking.
    """
    for path, relative, suffix, is_symlink in _walk_tree(root):
        if not is_symlink:
            yield path, relative, suffix


def walk_tree_symlinks(root: Path):
    """Yield ``(path, relative)`` for every symlink under ``root`` -- what the walk refuses.

    Same traversal, deliberately: a symlink refused by one rule and unmentioned by another is
    exactly the drift this pair exists to remove. Every symlink is yielded, not only ``.md``
    and ``.py`` -- a symlinked ``judge-schema.json`` or ``guide.txt`` escapes a skill just as
    a markdown one does.
    """
    for path, relative, _suffix, is_symlink in _walk_tree(root):
        if is_symlink:
            yield path, relative


def check_refused_symlinks(skills_dir: Path) -> list[str]:
    """Report every skill root and every in-skill entry the traversal refused to follow.

    :func:`iter_skill_roots` skips a symlinked skill and :func:`walk_tree_files` skips a
    symlinked file, both correctly and both in silence — so the generators whose only purpose
    is to say so must actually be called by production code, or a real run refuses a skill
    and prints nothing.

    Both refusals are errors, not warnings: the installer copies ``skills/<name>/`` and a
    link does not survive the copy, installing as a broken link or as a path that exists only
    on the machine that made it.
    """
    errors = []
    for root in symlinked_skill_roots(skills_dir):
        errors.append(
            f"  {root}: skill root is a symlink — refused, so NOTHING in this skill was "
            f"validated; the installer ships skills/<name>/ and a link does not survive "
            f"the copy"
        )
    for skill_md in iter_skill_roots(skills_dir):
        for path, relative in walk_tree_symlinks(skill_md.parent):
            # The target is quoted where it can be read and omitted where it cannot. A
            # `readlink()` here raises for a Windows junction and for a link deleted between
            # the walk and this line — and a traceback from the function that REPORTS a
            # refusal would be a poor way to learn something was refused.
            try:
                target = f" (target: '{path.readlink()}')"
            except OSError:
                target = ""
            errors.append(
                f"  {path}: '{relative.as_posix()}' is a link out of the skill — refused, so "
                f"it was neither scanned nor counted; installed skills must be "
                f"self-contained{target}"
            )
    return errors


def discover_skill_artifacts(skills_dir: Path) -> list[str]:
    """Discover all SKILL.md files, their references/*.md docs, plus known companion
    files, relative to skills_dir.

    Any directory under skills_dir containing a SKILL.md is a skill. Markdown docs
    shipped under a skill's references/ are instructional content too, so they are gated
    under the same portability contract as SKILL.md. The plan-duel skill additionally has
    companion files that must be packaged.
    """
    artifacts: list[str] = []
    if not skills_dir.is_dir():
        return artifacts
    for skill_md in iter_skill_roots(skills_dir):
        skill_name = skill_md.parent.name
        artifacts.append(f"{skill_name}/SKILL.md")
        # Every markdown file in the skill, at any depth, via the shared walk — not a glob
        # plus a hardcoded companion list. The RATCHET was widened three times (depth,
        # skill-root companions, case) without discovery following, each widening leaving a
        # file the budget charges for and no rule inspects.
        #
        # ONE walk per skill, classified as it goes: two passes over the same tree is the
        # shape this removes.
        markdown: list[str] = []
        python: list[str] = []
        for _path, relative, suffix in walk_tree_files(skill_md.parent):
            name = f"{skill_name}/{relative.as_posix()}"
            if suffix == ".md" and str(relative) != "SKILL.md":
                # B1.5: the ledger IS discovered, so the prose rules run over it, and is
                # still excluded from the word budget by `measure_skill_words`. One walk, two
                # answers — which is why the scope predicate belongs to the CALLER's question
                # and not to the walk. Before this, `DECISIONS.md` was the only shipped
                # markdown governed by no rule at all.
                markdown.append(name)
            elif suffix == ".py":
                # EVERY .py, at any depth, discovered rather than named. Reachable only
                # through a `plan-duel` branch, the bundled-engine portability rule never
                # scanned `skills/diff-review/review_runner.py` — a mandatory companion whose
                # own header claims no branded CLI is baked into it. The same hardcoded
                # `["claude", "-p", ...]` appended to each engine failed from plan_duel.py and
                # passed from review_runner.py. A helper under `references/` ships and
                # executes exactly as a root engine does, and was likewise scanned by nothing.
                python.append(name)
        artifacts.extend(markdown)
        artifacts.extend(python)
        # The plan-duel companion list is gone: the markdown walk above reaches init.md,
        # round.md and summary.md by walking, so a fourth companion is covered the day it
        # lands rather than the day someone remembers to extend a constant. The schema stays
        # named because it is not found by suffix — and the per-artifact rule loop in
        # validate_skills dispatches on .py / .md only, so a .json artifact is gated by the
        # tree-wide check_shipped_json sweep instead.
        if (skill_md.parent / PLAN_DUEL_SCHEMA).is_file():
            artifacts.append(f"{skill_name}/{PLAN_DUEL_SCHEMA}")
    return artifacts


def _md_fence_marker(line: str) -> tuple[str, str] | None:
    """``(fence, info)`` when ``line`` opens or closes a fenced block, else ``None``.

    Two rules beyond "starts with three of the character", and both decide whether the
    line is a fence at all: at most three spaces of indentation, and no backtick anywhere
    in a BACKTICK fence's info string (a tilde fence may carry one).
    """
    match = _MD_FENCE_RE.match(line)
    if match is None:
        return None
    fence, info = match.group("fence"), match.group("info")
    if fence[0] == "`" and "`" in info:
        return None
    return fence, info


def check_unterminated_fence(filepath: Path) -> list[str]:
    """Report a file that ends inside a code fence.

    Treating an unterminated fence as running to end-of-file is CommonMark. The cost is
    silent: every line after the stray fence becomes code, so a `_Classification:` line below
    it reads as an example and the skill drops out of classification discovery. A rule that
    quietly stops covering a file is worse than one that fails, so the reading stays and the
    condition is reported.

    **The scan RESYNCHRONISES, so the state machine alone only catches the LAST block.** So
    parity is checked as well: a well-formed file has two delimiter lines per fence, and an
    odd count means one is unterminated wherever the machine ended up. The first marker is
    named, because that is where the file went wrong.
    """
    try:
        content = _read_text_lossy(filepath)
    except FileNotFoundError:
        return [f"  File not found: {filepath}"]
    opener: tuple[str, str] | None = None
    opened_at = 0
    markers = 0
    first_at = 0
    for i, line in enumerate(content.splitlines(), 1):
        marker = _md_fence_marker(line)
        if marker is None:
            continue
        markers += 1
        if not first_at:
            first_at = i
        if opener is None:
            opener, opened_at = marker, i
        elif (marker[0][0] == opener[0][0]
              and len(marker[0]) >= len(opener[0])
              and not marker[1].strip()):
            opener = None
    if opener is not None:
        return [
            f"  {filepath}:{opened_at}: code fence '{opener[0]}' is never closed, so every "
            f"line below it reads as code — a declaration there is quoted rather than "
            f"made, and the file leaves classification discovery without any rule "
            f"reporting it"
        ]
    if markers % 2:
        return [
            f"  {filepath}:{first_at}: this file has an odd number of code-fence "
            f"delimiters, so one fence is never closed. The scan re-pairs after a missing "
            f"closer — the next opener is swallowed as code and the closer after it stands "
            f"in — so the fence that is actually unbalanced may be anywhere below this "
            f"line; count the fences from here"
        ]
    return []


def markdown_code_flags(lines: list[str]) -> list[bool]:
    """True for each line Markdown renders as CODE rather than prose.

    Two kinds, both CommonMark, and the same reading the bundled plan-duel engine applies to
    its own fences:

    * a fenced block (``` or ~~~), the fence lines included. A closing fence must use the
      opener's character, be at least as long, and carry no info string.
    * four or more columns of leading indentation, tabs expanded first. Three or fewer is an
      ordinary paragraph — which is why an indented declaration still counts.

    A mandatory declaration must not be satisfiable by a document that only LOOKS like it
    declares.
    """
    flags: list[bool] = []
    opener: str | None = None
    for line in lines:
        marker = _md_fence_marker(line)
        if opener is None:
            if marker is not None:
                opener = marker[0]
                flags.append(True)
                continue
            expanded = line.expandtabs(INDENTED_CODE_COLUMNS)
            indent = len(expanded) - len(expanded.lstrip(" "))
            flags.append(bool(expanded.strip()) and indent >= INDENTED_CODE_COLUMNS)
            continue
        # Inside a fence until a valid closer; the closer itself is code too.
        flags.append(True)
        if (
            marker is not None
            and marker[0][0] == opener[0]
            and len(marker[0]) >= len(opener)
            and not marker[1].strip()
        ):
            opener = None
    return flags


def prose_lines(content: str) -> list[str]:
    """Every line Markdown renders as prose, stripped — the only place a declaration counts.

    The one place "is this a declaration?" is answered, so `check_classification`,
    `discover_degraded_or_limited` and `is_classification_line` cannot disagree. A hole in
    discovery would flag a skill on an example it merely quotes, and the check would then
    report that skill's real declaration missing.
    """
    # HTML comments blanked FIRST. A `_Classification:` or `_Progress:` line wrapped in
    # `<!-- ... -->` renders as nothing at all, and it satisfied the mandatory-declaration
    # check anyway — demo-video's real declaration commented out still gave "All validations
    # passed". The module already knew comments hide text; this was the one place that read
    # them as prose.
    #
    # markdown_code_flags still runs over the blanked lines rather than being replaced by
    # _strip_html_comments' own flags: it supplies the indented-code rule, which those do not.
    lines, _fenced = _strip_html_comments(content.splitlines())
    return [
        line.strip()
        for line, is_code in zip(lines, markdown_code_flags(lines))
        if not is_code
    ]


def discover_degraded_or_limited(skills_dir: Path) -> list[str]:
    """Discover SKILL.md files whose _Classification: line contains Degraded or Runtime-limited."""
    flagged: set[str] = set()
    if not skills_dir.is_dir():
        return []
    for skill_md in iter_skill_roots(skills_dir):
        skill_name = skill_md.parent.name
        if skill_name in CLASSIFICATION_REQUIRED_SKILLS:
            flagged.add(f"{skill_name}/SKILL.md")
        try:
            content = skill_md.read_text(encoding="utf-8")
        except OSError:
            continue
        for stripped in prose_lines(content):
            if stripped.startswith(CLASSIFICATION_DECL_PREFIX):
                if any(marker in stripped for marker in CLASSIFICATION_REQUIRED_MARKERS):
                    flagged.add(f"{skill_name}/SKILL.md")
                break
    return sorted(flagged)


def discover_agent_dispatchers(skills_dir: Path) -> list[str]:
    """Discover SKILL.md files that dispatch sub-agents and so must declare `_Progress:`.

    Mirrors discover_degraded_or_limited: a curated set (AGENT_DISPATCH_SKILLS) UNION a
    content scan (any SKILL.md whose text mentions a sub-agent). The union means a new
    dispatcher that uses sub-agent language is caught automatically, while the curated set
    covers dispatchers that shell out to subprocess participants without the word.
    """
    flagged: set[str] = set()
    if not skills_dir.is_dir():
        return []
    for skill_md in iter_skill_roots(skills_dir):
        skill_name = skill_md.parent.name
        if skill_name in AGENT_DISPATCH_SKILLS:
            flagged.add(f"{skill_name}/SKILL.md")
            continue
        try:
            content = skill_md.read_text(encoding="utf-8")
        except OSError:
            continue
        if any(marker.search(content) for marker in AGENT_DISPATCH_MARKERS):
            flagged.add(f"{skill_name}/SKILL.md")
    return sorted(flagged)


def opens_adapter_block(line: str) -> bool:
    """A blockquote line naming an adapter — the one exemption that spans a BLOCK.

    An adapter note is written as a run of ``>`` lines and legitimately spells the
    runtime-specific tool names the banned-phrase rule exists to keep out of skill prose,
    so the exemption has to continue to the end of the quote.
    """
    stripped = line.strip()
    return stripped.startswith(">") and "adapter" in stripped.lower()


def is_classification_line(line: str, in_code: bool = False) -> bool:
    """A ``_Classification:`` declaration — exempt on ITS OWN LINE and nothing further.

    It shared a branch with :func:`opens_adapter_block`, and the caller opened a block
    exemption for whichever matched. So a declaration followed by a blockquote switched
    banned-phrase checking off for that whole quote — a scope nothing about a one-line
    declaration justifies, and one that reads as deliberate rather than accidental. Nothing
    in the tree does this today; separating the branches keeps it that way.

    ``in_code`` comes from :func:`markdown_code_flags` and is what keeps the third parse
    site honest with the other two: a `_Classification:` line quoted inside a fence is an
    example, not a declaration.
    """
    return not in_code and line.strip().startswith(CLASSIFICATION_DECL_PREFIX)


def check_banned_phrases(filepath: Path) -> list[str]:
    """Check for banned phrases outside exempt contexts.

    Exempt contexts are adapter note blockquotes (> lines following an adapter
    header) and _Classification: declarations. Everything else — including
    documentation prose and comments — is checked.
    """
    errors = []
    try:
        content = filepath.read_text(encoding="utf-8")
    except FileNotFoundError:
        return [f"  File not found: {filepath}"]

    lines = content.splitlines()
    code_flags = markdown_code_flags(lines)
    in_adapter_block = False
    for i, (line, in_code) in enumerate(zip(lines, code_flags), 1):
        stripped = line.strip()

        # Only the adapter note opens a BLOCK. A `_Classification:` declaration exempts the
        # line it is written on, and the next line is judged normally.
        if opens_adapter_block(line):
            in_adapter_block = True
            continue

        if is_classification_line(line, in_code=in_code):
            in_adapter_block = False
            continue

        # Continue adapter block: line starts with > (blockquote continuation)
        if in_adapter_block and stripped.startswith(">"):
            continue

        # End adapter block when we hit a non-blockquote line
        in_adapter_block = False

        for phrase in BANNED_PHRASES:
            if phrase in line:
                errors.append(f"  {filepath}:{i}: banned phrase '{phrase}' found")
    return errors


def _read_text_lossy(filepath: Path) -> str:
    """Read a file as UTF-8, falling back to latin-1 when bytes don't decode.

    latin-1 maps every byte, so a mixed-encoding text file (one stray invalid
    byte) is still scanned line-by-line instead of being skipped — the ASCII
    private-reference patterns match byte-for-byte either way, mirroring what a
    plain ``grep -r`` over the tree would have caught.
    """
    try:
        return filepath.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return filepath.read_bytes().decode("latin-1")


def _parse_frontmatter(filepath: Path) -> dict[str, str]:
    """Parse a SKILL.md's leading YAML frontmatter into flat string values.

    Hand-rolled rather than PyYAML: the validator is stdlib-only so it runs on a bare CI
    image on every host. It covers the two shapes the pack uses — an inline scalar and a
    folded/literal block, joined into one line — and unquotes a quoted scalar, so
    ``name: "demo"`` and ``name: demo`` agree.

    No frontmatter, or an opening ``---`` never closed, yields an empty mapping rather than a
    partial parse: an unterminated header would swallow the body into the last field and let
    a bogus ``name:`` satisfy the directory rule. The search for the terminator **stops at
    the first column-0 line that is not YAML**, or a body's thematic break masquerades as it.
    """
    content = _read_text_lossy(filepath)
    lines = content.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}

    fields: dict[str, str] = {}
    key: str | None = None
    buffer: list[str] = []
    terminated = False
    for line in lines[1:]:
        if line == "---":
            terminated = True
            break
        is_yaml_line = (
            not line.strip()
            or line[:1].isspace()
            or line.startswith("- ")
            or re.match(r"^[A-Za-z][\w-]*:", line)
        )
        if not is_yaml_line:
            break
        match = re.match(r"^([A-Za-z][\w-]*):\s*(.*)$", line)
        if match and not line[:1].isspace():
            if key is not None:
                fields[key] = " ".join(buffer).strip()
            key = match.group(1)
            value = match.group(2).strip()
            # `>`/`|` (with optional chomp indicator) open a block scalar: the value
            # is on the following indented lines, not this one.
            buffer = [] if value in ("", ">", "|", ">-", "|-", ">+", "|+") else [value]
        elif key is not None:
            buffer.append(line.strip())
    if not terminated:
        return {}
    if key is not None:
        fields[key] = " ".join(buffer).strip()
    return {k: _unquote_scalar(v) for k, v in fields.items()}


def _unquote_scalar(value: str) -> str:
    """Strip one layer of matching YAML quotes from an inline scalar."""
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
        return value[1:-1]
    return value


def _opening_sentence(text: str) -> str:
    """Return ``text``'s first sentence, terminator included, whitespace collapsed.

    "First sentence" means everything up to and including the first ``.``, ``!`` or
    ``?`` that is followed by whitespace or the end of the text; a text with no such
    terminator is its own opening sentence. Stated here rather than left implicit so
    the description rules below are reproducible by hand.
    """
    flat = " ".join(text.split())
    match = _SENTENCE_BREAK_RE.search(flat)
    return flat[: match.start()].strip() if match else flat


# A line that must contain a private-looking string because its job is to — a pattern
# definition, or test data proving the check fires. Marked per LINE rather than per file:
# exempting a whole file is how coverage shrinks silently, and `grep -rn "hygiene-exempt"`
# lists every exemption in the repository.
HYGIENE_EXEMPT_MARKER = "hygiene-exempt"
# ANCHORED, and a reason is part of the marker. As a bare substring, any line merely
# containing those characters switched the private-path check off for its whole length — an
# `-ible` suffix, a sentence discussing the marker, a URL carrying it in a slug. Requiring
# the colon and something after it makes turning the guard off a deliberate act.
#
# The exempted span is the whole line, and NOT capped by length: a cap would only bite on a
# file with no line breaks, and every such file here is either under `plans/` — which the
# sweep skips — or a one-line fixture.
HYGIENE_EXEMPT_RE = re.compile(re.escape(HYGIENE_EXEMPT_MARKER) + r":\s*\S")


def check_private_paths(filepath: Path, patterns: list[re.Pattern] | None = None) -> list[str]:
    """Check for private paths and project-specific identifiers."""
    errors = []
    patterns = patterns or PRIVATE_PATH_PATTERNS
    try:
        content = _read_text_lossy(filepath)
    except FileNotFoundError:
        return [f"  File not found: {filepath}"]

    for i, line in enumerate(content.splitlines(), 1):
        if HYGIENE_EXEMPT_RE.search(line):
            continue
        for pattern in patterns:
            if pattern.search(line):
                errors.append(
                    f"  {filepath}:{i}: private/project-specific reference '{pattern.pattern}' found"
                )
    return errors


def check_hardcoded_attribution(filepath: Path) -> list[str]:
    """Check for vendor-specific co-author email defaults. Honors `hygiene-exempt`."""
    errors = []
    try:
        content = _read_text_lossy(filepath)
    except FileNotFoundError:
        return [f"  File not found: {filepath}"]

    for i, line in enumerate(content.splitlines(), 1):
        if HYGIENE_EXEMPT_RE.search(line):
            continue
        for pattern in HARDCODED_ATTRIBUTION_PATTERNS:
            if pattern.search(line):
                errors.append(
                    f"  {filepath}:{i}: hardcoded vendor attribution email '{pattern.pattern}' found"
                )
    return errors


def check_stale_runtime_claims(filepath: Path) -> list[str]:
    """Check for stale runtime capability claims in public skill text."""
    errors = []
    try:
        content = filepath.read_text(encoding="utf-8")
    except FileNotFoundError:
        return [f"  File not found: {filepath}"]

    for i, line in enumerate(content.splitlines(), 1):
        for pattern in STALE_RUNTIME_CLAIM_PATTERNS:
            if pattern.search(line):
                errors.append(
                    f"  {filepath}:{i}: stale runtime capability claim '{pattern.pattern}' found"
                )
    return errors


# A run of characters that could be a path. Deliberately NOT a markdown-link matcher: the
# rule scans whole lines, so it catches `cat ../PORTABILITY.md` in a fenced block as well as
# `[x](../PORTABILITY.md)`. The delimiters excluded here separate the path from the markup.
_PATH_TOKEN_RE = re.compile(r"[^\s()\[\]{}<>`'\"|,;]+")


def _escaping_relative_paths(line: str, depth: int):
    """Yield ``(path, reason)`` for each path written on ``line`` that leaves the skill.

    **Counting ``../`` is not the same as resolving a path, and the difference was wrong in
    both directions.** On a file one level down (``references/``), against a rule looking for
    a literal ``../../``:

    ===========================  ==========  ==========  ==============
    written on the line          resolves to old verdict this verdict
    ===========================  ==========  ==========  ==============
    ``../..``                    OUTSIDE     passed      flagged
    ``.././../x.md``             OUTSIDE     passed      flagged
    ``..\\..\\outside.md``        OUTSIDE     passed      flagged
    ``sub/../../SKILL.md``       inside      flagged      passed
    ===========================  ==========  ==========  ==============

    ``depth`` is how far the file sits below its own skill root, so the path resolves from
    where it is written: 0 for ``SKILL.md``, 1 for ``references/x.md``.

    **The resolving is :mod:`posixpath` and :mod:`urllib.parse`, not ours.** Two composition
    points, both easy to get wrong:

    * :func:`~urllib.parse.urlsplit` must run **before** the ``=`` split. Splitting first
      loses the query context, and the two failing shapes fail in opposite directions:
      ``guide.md?next=../../outside.md`` has its traversal inside a query a server resolves,
      while ``../../outside.md?mirror=https://example.com`` is a genuine escaping path.
    * The escape test is ``== ".."`` or ``startswith("../")``, never a bare
      ``startswith("..")`` — ``"..."`` is an ordinary filename.

    ``=`` is split on because the path can follow a flag. Backslashes are separators as well
    as slashes: a skill is written on one machine and installed on another.
    """
    for match in _PATH_TOKEN_RE.finditer(line):
        parts = urlsplit(match.group())
        if parts.scheme and parts.netloc:
            continue  # a server resolves this, not the filesystem
        for candidate in parts.path.split("="):
            # Percent-decoded, because `%2e%2e/` is `../` to whatever follows the link and a
            # rule avoidable by spelling is not a rule. Safe to decode the whole string here
            # rather than only `%2e`: tokenising already happened, so a `%20` turning into a
            # space cannot split one path into two.
            path = unquote(candidate).replace("\\", "/")
            # Only paths that TRAVERSE are judged. Flagging every anchored path is true of a
            # link target and false of most lines: this rule reads whole lines by design,
            # and the shipped skills carry 251 absolute-looking tokens in 26 files, almost
            # all slash-commands like `/cyw`. Flagging those would fire on nearly every skill
            # and teach authors to ignore the rule. Anchored AND traversing is unambiguous.
            if ".." not in path.split("/"):
                continue
            # An anchored path is reported as what it is rather than counted against the
            # skill root — `/tmp/../../x` never started inside the skill, so "two levels
            # above" would be a true-sounding sentence about the wrong thing. Skipping these
            # on the reasoning that the private-path patterns catch them was a hole:
            # `/tmp/../../outside`, `C:\work\..\outside` and a UNC path match none of them.
            if path.startswith("/") or re.match(r"^[A-Za-z]:", path):
                yield candidate, (
                    "is an absolute path — anchored to a filesystem root or a drive, so it "
                    "is not relative to the skill at all and cannot travel with it"
                )
                continue
            resolved = posixpath.normpath(posixpath.join("d/" * depth, path))
            if resolved != ".." and not resolved.startswith("../"):
                continue
            # Say how far out it goes, not merely that it does. `../..` written under
            # `references/` is not visibly an escape until someone counts the levels, and the
            # reason the old rule was wrong here is that counting is easy to get wrong by eye.
            levels = len([s for s in resolved.split("/") if s == ".."])
            yield candidate, (
                f"resolves {levels} level{'s' if levels > 1 else ''} above the skill "
                f"directory"
            )


def check_self_contained_skill_refs(filepath: Path, depth: int) -> list[str]:
    """Flag references that break at runtime because the target is not installed.

    Installed skills are self-contained: the installer ships ``skills/<name>/`` only, so a
    skill file must not reference a repo-root doc nor climb out of its own skill directory.
    The escape threshold depends on where the file sits: a file at the skill root escapes
    with a single ``../``; one level down (``references/``) reaches its own root with one and
    only escapes with two.

    **A depth, not a boolean.** Two values suffice only while nothing below depth 1 exists:
    at `references/deep/guide.md` a boolean judges the file at depth 1 and reports its
    ordinary `../../SKILL.md` as escaping — a false positive, and the kind that matters,
    because it is a rule an author learns to write around.
    """
    errors = []
    try:
        content = filepath.read_text(encoding="utf-8")
    except FileNotFoundError:
        return [f"  File not found: {filepath}"]
    for i, line in enumerate(content.splitlines(), 1):
        for token, reason in _escaping_relative_paths(line, depth):
            errors.append(
                f"  {filepath}:{i}: path '{token}' {reason}; installed skills must be "
                f"self-contained — a sibling skill or the repo root is not shipped with "
                f"this skill"
            )
        for match in SIBLING_SKILL_PATH_RE.finditer(line):
            errors.append(
                f"  {filepath}:{i}: repo-rooted path '{match.group(0)}' — it descends from "
                f"a 'skills/' directory that exists in this repository and not in an "
                f"install, where each skills/<name>/ is shipped on its own; name the skill "
                f"by its invocation instead of pointing at its files"
            )
        for doc in NON_INSTALLED_ROOT_DOCS:
            if doc in line:
                errors.append(
                    f"  {filepath}:{i}: reference to repo-root doc '{doc}' — not installed "
                    f"with the skill (the installer ships skills/<name>/ only); inline what "
                    f"the skill needs at runtime instead of pointing at it"
                )
    return errors


BUNDLED_REF_RE = re.compile(r"(?<![A-Za-z0-9_./-])references/[A-Za-z0-9_./-]+")


def check_bundled_refs_resolve(filepath: Path, skill_root: Path) -> list[str]:
    """Every ``references/...`` path a skill names must exist in that skill.

    The complement of :func:`check_self_contained_skill_refs`, which asks whether a reference
    ESCAPES the skill root and never whether it RESOLVES. A skill could name
    ``references/anchored-assumptions.md``, ship no such file, and pass every rule here, so
    the agent following the instruction hits a dead end mid-task.

    **Scoped to ``references/`` deliberately.** Checking every path-shaped token flags 240
    references in this pack and essentially all are correct: a planning skill legitimately
    names ``plan.md`` and ``package.json``, which are files in the USER's project. Nothing
    general separates those from a bundled companion. ``references/`` is this pack's
    convention for a file that travels inside ``skills/<name>/``, so naming one that is not
    there is always a defect. A placeholder — ``<...>``, ``⟪...⟫`` or a glob — is skipped.
    """
    errors = []
    try:
        content = filepath.read_text(encoding="utf-8")
    except FileNotFoundError:
        return [f"  File not found: {filepath}"]
    for i, line in enumerate(content.splitlines(), 1):
        for match in BUNDLED_REF_RE.finditer(line):
            token = unquote(match.group())
            if any(c in token for c in "<>*?\u27ea\u27eb"):
                continue
            # Written from the SKILL ROOT wherever it appears, including from inside
            # `references/` itself -- that is the convention every skill already uses.
            if not (skill_root / token).exists():
                errors.append(
                    f"  {filepath}:{i}: '{token}' does not exist in this skill — a "
                    f"runtime told to read it has nothing to read, and the instruction "
                    f"fails mid-task rather than at load"
                )
    return errors


# A dispatched reviewer: prose that sends the judging to a unit other than this context.
# Deliberately narrow — these are the shapes the pack uses, not every way an author could
# phrase it. A vocabulary that guesses produces false positives.
# The pack's own idiom for a stated fallback, which FALLBACK_INDICATORS does not cover:
# `diff-review` writes "falls open to rung 2", not "if available". Kept separate because
# that list governs a different rule and would be weakened by words this one needs.
LADDER_INDICATORS = [
    "rung",
    "falls open",
    "falls back",
    "preserved either way",
    "cannot spawn",
    "cannot reach",
    "no other runtime",
    "where a runtime cannot",
    "always works",
    "sequential",
]

DISPATCHED_REVIEWER_RE = re.compile(
    r"fresh (?:independent )?(?:sub-?agent|reviewer|work unit|context)"
    r"|independent sub-?agent"
    r"|dispatch(?:es|ed)? (?:the |each )?(?:review|component review)"
    r"|(?:sub-?agents?|another runtime) (?:where|when) (?:available|supported)"
    r"|different runtime",
    re.IGNORECASE)


def check_independence_ladder(filepath: Path) -> list[str]:
    """A skill that dispatches the judging elsewhere must say what it does without it.

    `PORTABILITY.md`, *Independent Verification*: state the ladder, never a single rung. No
    runtime is obliged to provide a sub-agent or a second model, so a skill whose review step
    only describes the strong rung stops working — silently, because the step still reads as
    satisfiable — on a host that cannot reach it.

    **Narrow on purpose.** The vocabulary is the phrasings the pack already uses; a rule that
    tried to recognise every way an author might describe delegation would fire on correct
    prose.
    """
    errors = []
    try:
        lines = filepath.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return []
    # The frontmatter description is a summary for a dispatcher, not a step a runtime
    # executes, so a mention there carries no obligation. Skipped by finding the second
    # `---`, which is where the body starts.
    body_starts = 0
    if lines[:1] == ["---"]:
        for i, line in enumerate(lines[1:], 1):
            if line.strip() == "---":
                body_starts = i + 1
                break
    references = [i for i, line in enumerate(lines)
                  if i >= body_starts and DISPATCHED_REVIEWER_RE.search(line)]
    if not references:
        return []
    # ONCE PER DOCUMENT, over the whole file rather than a window — the same rule
    # `check_companion_skill_fallbacks` settled on. With a 6-line window instead, three
    # files were flagged and all three were correct, stating the fallback in a section the
    # window could not reach; requiring a restatement beside each adapter mandates exactly
    # the duplication the budget exists to prevent.
    body = " ".join(lines[body_starts:]).lower()
    if any(indicator in body for indicator in FALLBACK_INDICATORS + LADDER_INDICATORS):
        return []
    first = references[0]
    more = f" (and {len(references) - 1} more)" if len(references) > 1 else ""
    errors.append(
        f"  {filepath}:{first + 1}: sends the judging to a unit other than this "
        f"context{more}, and no fallback rung is stated near any of them — a host with no "
        f"sub-agent and one runtime has nothing to do here, and the step still reads as "
        f"satisfiable. State the ladder: strongest rung, then what it degrades to"
    )
    return errors


def check_v1_suite_routing(filepath: Path, in_v1_suite: bool = False) -> list[str]:
    """Check both directions of suite routing inside a v1 planning-skill body.

    Two opposite requirements share one file, which is why a blanket rewrite in either
    direction is wrong:

    * an **intra-suite** reference — to a sibling *or to the skill itself* — must be
      ``-v1``-qualified, because the unqualified names belong to the v2 suite; while
    * a **forward redirect** to that v2 suite must be **unqualified**, because canonical now
      means v2. These live in the ``Format: v2`` refusal guards.

    The two are told apart by paragraph: a block mentioning ``Format: v2`` in prose is a
    forward-redirect context, every other block is intra-suite. Paragraphs break on blank
    lines **and on fenced-code boundaries**, and the marker only counts outside a fence.
    Fenced lines are still scanned, since a template block emits real routing text.
    Frontmatter is excluded — a suite-wide rename rewrites ``description`` wholesale.

    **Granularity is the paragraph, deliberately — and this is the rule's known limit.** A
    forward redirect and a stale intra-suite reference are textually identical, so a redirect
    paragraph reads EVERY bare name in it as a redirect. Splitting on more than one suite
    member was rejected against the tree: `plan-run-v1` says "run `/plan-run` instead (with
    `/plan-phase` for the work breakdown …)", which names two and is correct.

    ``in_v1_suite`` mirrors ``check_self_contained_skill_refs``'s ``depth``: the caller
    decides, so a flat ``.md`` fixture can exercise the rule, and it defaults to the non-v1
    path.
    """
    if not in_v1_suite:
        return []
    try:
        content = _read_text_lossy(filepath)
    except FileNotFoundError:
        return [f"  File not found: {filepath}"]

    lines = content.splitlines()
    body_start = 0
    if lines and lines[0].strip() == "---":
        # Only a column-0 `---` closes the header, matching _parse_frontmatter: an
        # indented one inside a block scalar is content. An unterminated header means
        # there is no frontmatter, so the whole file is body.
        for index in range(1, len(lines)):
            if lines[index] == "---":
                body_start = index + 1
                break

    # Same single fence model as the rest of the module: a delimiter inside an HTML comment
    # must not open a fence, or a commented-out example suppresses the redirect context
    # around a live reference and this rule false-positives.
    #
    # Scan the BODY only. Handing the whole file to the comment scanner put frontmatter back
    # in, so a stray `<!--` inside a YAML folded scalar blanked the entire body and the rule
    # silently found nothing. Line numbering is preserved by padding.
    body_lines, body_fenced = _strip_html_comments(lines[body_start:])
    lines = lines[:body_start] + body_lines
    fenced = [False] * body_start + body_fenced
    errors: list[str] = []

    def check_paragraph(block: list[tuple[int, str]], in_fence: bool) -> None:
        if not block:
            return
        # The marker only establishes a redirect context from live prose: inside a
        # fence or a blockquoted example it is being quoted, not applied. (The shared
        # fence scanner does not see a fence opened inside a blockquote, so the
        # blockquote test is what covers that shape.)
        is_redirect = not in_fence and any(
            V2_FORMAT_MARKER in text and not text.lstrip().startswith(">")
            for _, text in block
        )
        for lineno, text in block:
            if is_redirect:
                for match in _V1_SUFFIXED_REF_RE.finditer(text):
                    errors.append(
                        f"  {filepath}:{lineno}: qualified forward redirect "
                        f"'{match.group(0)}' inside a '{V2_FORMAT_MARKER}' guard; the "
                        f"canonical suite is v2, so the redirect must name it by its "
                        f"bare name, with no suffix. If this reference is instead an "
                        f"intra-suite one that belongs in the v1 suite, split it out of "
                        f"the guard paragraph — the two cannot be told apart within one"
                    )
            else:
                for match in _V1_UNQUALIFIED_REF_RE.finditer(text):
                    errors.append(
                        f"  {filepath}:{lineno}: unqualified intra-suite reference "
                        f"'{match.group(0)}' in a v1 skill body; references to the v1 "
                        f"suite — including a skill's references to itself — must be "
                        f"'{V1_SUFFIX}'-qualified"
                    )

    paragraph: list[tuple[int, str]] = []
    paragraph_fenced = False
    for index in range(body_start, len(lines)):
        text = lines[index]
        lineno = index + 1
        if not text.strip():
            check_paragraph(paragraph, paragraph_fenced)
            paragraph = []
            continue
        if paragraph and fenced[index] != paragraph_fenced:
            check_paragraph(paragraph, paragraph_fenced)
            paragraph = []
        if not paragraph:
            paragraph_fenced = fenced[index]
        paragraph.append((lineno, text))
    check_paragraph(paragraph, paragraph_fenced)

    return errors


def check_codex_skill_paths(filepath: Path) -> list[str]:
    """Check for a ~/.codex/skills install path in favor of the documented
    $HOME/.agents/skills."""
    errors = []
    try:
        content = filepath.read_text(encoding="utf-8")
    except FileNotFoundError:
        return [f"  File not found: {filepath}"]

    for i, line in enumerate(content.splitlines(), 1):
        for pattern in CODEX_SKILL_PATH_PATTERNS:
            if pattern.search(line):
                errors.append(
                    f"  {filepath}:{i}: Codex skill path '{pattern.pattern}' found; use $HOME/.agents/skills (Codex's documented user skills path)"
                )
    return errors


# A spawned CLI's file permission must be stated by the command, never inherited. Scoped to
# `codex exec` because its default sandbox is DIRECTORY-TRUST DEPENDENT — read-only for an
# untrusted directory, writable for a trusted one — so an unflagged command behaves
# differently on each user's machine. `claude -p` is NOT checked: its default withholds edit
# permission deterministically.
#
# BOTH halves are required, because there are TWO write paths: the sandbox governs the
# model's shell commands, while a built-in patch/edit tool is gated by the approval policy,
# so `-s read-only` alone still wrote a file. The executable may carry a path prefix or a
# Windows suffix, which the engine resolves.
_CODEX_EXEC_SHELL_RE = re.compile(r"(?:^|[\s\"'`(])[\w./\\-]*codex(?:\.exe)?\s+exec\b")
_CODEX_EXEC_JSON_RE = re.compile(r'"[\w./\\-]*codex(?:\.exe)?"\s*,\s*"exec"')
_BYPASS_FLAG = "--dangerously-bypass-approvals-and-sandbox"
# Each flag must carry a REAL value: `-s -c approval_policy=never` names no mode, and
# `approval_policy=nevermore` is not `never`. Anchoring the value (and terminating it)
# keeps a malformed command from passing on a substring match alone.
_SANDBOX_MODES = "read-only|workspace-write|danger-full-access"
_SANDBOX_FLAG_RE = re.compile(
    rf'(?:^|[\s"\[])(?:-s|--sandbox)(?:[\s"=,\]]+)(?:{_SANDBOX_MODES})\b'
    rf'|{re.escape(_BYPASS_FLAG)}'
)
# Must be BOUND to its config flag: the bare text `approval_policy=never` can appear
# inside the prompt string, which pins nothing.
_APPROVAL_PIN_RE = re.compile(
    rf'(?:-c|--config)["\'\s,=\]]+["\']?approval_policy\s*=\s*["\']?never(?![\w-])'
    rf'|{re.escape(_BYPASS_FLAG)}'
)
_SPAWN_SPAN_CHARS = 300


def _blockquote_joined(content: str) -> tuple[str, list[int]]:
    """Join lines into one scannable string, mapping each char back to its line.

    A single invocation is frequently wrapped across lines inside a blockquote, so scanning
    line-by-line would miss the flags that follow the wrap. Leading ``>`` markers are
    stripped so the reassembled command reads as it would on one line.
    """
    parts: list[str] = []
    line_of: list[int] = []
    for lineno, line in enumerate(content.splitlines(), 1):
        text = re.sub(r"^\s*>+\s?", "", line)
        for _ in range(len(text) + 1):  # +1 for the joining space
            line_of.append(lineno)
        parts.append(text)
    return " ".join(parts), line_of


def _spawn_span(joined: str, start: int, terminator: str, spawn_re) -> str:
    """The text belonging to ONE spawn: from ``start`` to its terminator.

    A command ends at its own terminator (the code span's closing backtick, or the argv
    array's ``]``) whenever one appears before the next spawn, so the character cap cannot
    cut a long-but-valid command short. The next-spawn bound is always enforced, so an
    unflagged command can never borrow a later one's flags.
    """
    nxt = spawn_re.search(joined, start)
    hard_end = nxt.start() if nxt is not None else len(joined)
    found = joined.find(terminator, start)
    if found != -1 and found < hard_end:
        return joined[start:found]
    return joined[start : min(hard_end, start + _SPAWN_SPAN_CHARS)]


def check_spawn_permissions(filepath: Path) -> list[str]:
    """Every `codex exec` invocation in a skill must state its sandbox explicitly.

    Bare mentions (an inline ``codex exec`` with no arguments, used in prose) are not
    invocations and are skipped; anything carrying arguments must name a sandbox mode.
    """
    errors = []
    try:
        content = filepath.read_text(encoding="utf-8")
    except FileNotFoundError:
        return [f"  File not found: {filepath}"]

    # Shell form: `codex exec …` — the span runs to the closing backtick of the code
    # span, else a bounded look-ahead.
    joined, line_of = _blockquote_joined(content)
    for match in _CODEX_EXEC_SHELL_RE.finditer(joined):
        span = _spawn_span(joined, match.end(), "`", _CODEX_EXEC_SHELL_RE)
        if not span.strip():
            continue  # a bare `codex exec` mention, not an invocation
        lineno = line_of[match.start()] if match.start() < len(line_of) else 0
        if not _SANDBOX_FLAG_RE.search(span):
            errors.append(
                f"  {filepath}:{lineno}: `codex exec` invocation without an explicit "
                f"-s/--sandbox mode; the default is directory-trust dependent, so an "
                f"agent that must write silently writes nothing (exit 0) in an "
                f"untrusted directory"
            )
        elif not _APPROVAL_PIN_RE.search(span):
            errors.append(
                f"  {filepath}:{lineno}: `codex exec` states a sandbox but does not pin "
                f"approval_policy=never; the model can escalate past the sandbox and a "
                f"machine set to auto-approve will grant it, so the stated mode is not "
                f"a bound"
            )

    # JSON argv form: ["codex", "exec", …] — one command per line by convention.
    # JSON argv form: ["codex", "exec", …]. Scanned on the JOINED text, not per line —
    # a pretty-printed array puts each element on its own line and would otherwise
    # bypass the check entirely. The span ends at the array's closing bracket.
    for match in _CODEX_EXEC_JSON_RE.finditer(joined):
        span = _spawn_span(joined, match.end(), "]", _CODEX_EXEC_JSON_RE)
        lineno = line_of[match.start()] if match.start() < len(line_of) else 0
        if not _SANDBOX_FLAG_RE.search(span):
            errors.append(
                f"  {filepath}:{lineno}: adapter command spawns `codex exec` without an "
                f"explicit \"-s\" sandbox mode; state the permission the role's "
                f"contract needs (write-scoped to its workdir, or read-only)"
            )
        elif not _APPROVAL_PIN_RE.search(span):
            errors.append(
                f"  {filepath}:{lineno}: adapter command states a sandbox but does not pin "
                f"\"approval_policy=never\"; without it the model can escalate past the "
                f"sandbox, so the stated mode is not a bound"
            )
    return errors


def _strip_html_comments(lines: list[str]) -> tuple[list[str], list[bool]]:
    """Resolve HTML comments and code fences in ONE ordered pass.

    Returns (lines with comment spans blanked, per-line fenced flags), preserving line
    numbering so diagnostics keep pointing at the right place. The two constructs mask each
    other, so separate passes are wrong in whichever direction loses. The rule:

    - inside a fence, HTML delimiters are literal text and do not open or close a comment;
    - inside a comment, fence delimiters are commented-out text and do not open a fence.
    """
    out: list[str] = []
    fenced: list[bool] = []
    in_comment = False
    fence_char: str | None = None
    fence_len = 0

    for raw in lines:
        stripped = raw.lstrip()

        if fence_char is not None:
            # Inside a fence: literal. Only a proper closing delimiter ends it.
            fenced.append(True)
            out.append(raw)
            run = len(stripped) - len(stripped.lstrip(fence_char))
            if run >= fence_len and not stripped[run:].strip():
                fence_char, fence_len = None, 0
            continue

        if (not in_comment
                and (stripped.startswith("```") or stripped.startswith("~~~"))):
            ch = stripped[0]
            run = len(stripped) - len(stripped.lstrip(ch))
            if run >= 3:
                fence_char, fence_len = ch, run
                fenced.append(True)
                out.append(raw)
                continue

        # Ordinary text, or text inside a comment. Delimiters are walked IN ORDER within the
        # line, so `<!-- a --> b <!-- c` leaves the comment open and keeps the visible `b`.
        # Comment state and inline-code state are tracked TOGETHER, because separate passes
        # are wrong in both directions.
        #
        # The rules, all three mutually exclusive by construction:
        #   inside a comment  — only `-->` acts; backticks are ordinary text
        #   inside a code span — only its own closing run acts; `<!--` is ordinary text
        #   otherwise          — a backtick run opens a span, an UNESCAPED `<!--` a comment
        kept: list[str] = []
        pos = 0
        while pos < len(raw):
            if in_comment:
                close = raw.find("-->", pos)
                if close == -1:
                    break
                in_comment, pos = False, close + 3
                continue
            ch = raw[pos]
            if ch == "`":
                run = len(raw[pos:]) - len(raw[pos:].lstrip("`"))
                closer = raw.find("`" * run, pos + run)
                # A span closes on a run of the SAME length; an unmatched run is literal.
                while closer != -1 and raw[closer + run: closer + run + 1] == "`":
                    closer = raw.find("`" * run, closer + run)
                end = closer + run if closer != -1 else pos + run
                kept.append(raw[pos:end])
                pos = end
                continue
            if raw.startswith("<!--", pos):
                # `\<!--` is literal text, not an opener. Count the backslash run: an odd
                # number escapes, an even number is itself escaped backslashes.
                back = len(raw[:pos]) - len(raw[:pos].rstrip("\\"))
                if back % 2 == 1:
                    kept.append(raw[pos])
                    pos += 1
                    continue
                in_comment, pos = True, pos + 4
                continue
            kept.append(ch)
            pos += 1
        fenced.append(False)
        out.append("".join(kept))

    return out, fenced


def check_cross_skill_references(filepath: Path, known_skills: list[str]) -> list[str]:
    """Check that every backtick-quoted skill-name reference exists in the pack.

    Two spellings, because both send a reader somewhere:

    * ``the `cyw` skill`` — narrow on purpose, so not every backtick-quoted term counts.
    * ``` `/cyw` ``` — the INVOCATION form, and the likelier one.

    The invocation form requires the backtick span to be *exactly* the slash command. That
    strictness is what makes the rule usable: a looser `/name` match reads
    `plans/<slug>/plan.md` as a reference to `/plan`. Two references that do not resolve are
    named in `NON_SKILL_SLASH_COMMANDS`.
    """
    errors: list[str] = []
    try:
        content = filepath.read_text(encoding="utf-8")
    except FileNotFoundError:
        return [f"  File not found: {filepath}"]

    known_set = set(known_skills)
    pattern = re.compile(r"`([a-z][a-z0-9-]+)`\s+skill", re.IGNORECASE)
    for i, line in enumerate(content.splitlines(), 1):
        for match in pattern.finditer(line):
            name = match.group(1)
            if name not in known_set:
                errors.append(
                    f"  {filepath}:{i}: references unknown skill '`{name}` skill' (not found in skills/)"
                )
        for match in SLASH_COMMAND_RE.finditer(line):
            name = match.group(1)
            if name in known_set or name in NON_SKILL_SLASH_COMMANDS:
                continue
            errors.append(
                f"  {filepath}:{i}: references unknown slash command '/{name}' — no skill "
                f"of that name ships, so a reader told to run it has nothing to run"
            )
    return errors


def check_companion_skill_fallbacks(filepath: Path) -> list[str]:
    """Check that every companion skill a document names has a stated degraded path.

    Looks for backtick-quoted skill names and verifies that a fallback indicator appears
    within a 6-line window — the reference line, the two above and the three below — of **at
    least one** of that companion's references. Skips files that ARE the companion itself.

    **Once per document, not once per mention.** The window is how the fallback is found,
    not what is required: requiring the restatement at every site mandates the same paragraph
    three times in a file whose budget is the reader's attention. A document naming a
    companion and stating no fallback anywhere is still rejected; the error names the first
    reference and counts the rest, because the fix is one insertion.
    """
    errors = []
    try:
        content = filepath.read_text(encoding="utf-8")
    except FileNotFoundError:
        return []

    lines = content.splitlines()

    for skill_name in COMPANION_SKILLS:
        # Don't check the skill's own file
        if filepath.parent.name == skill_name:
            continue

        # Find lines that reference this companion skill as a skill to invoke
        pattern = re.compile(rf"`{skill_name}`\s+skill|the\s+`{skill_name}`\s+skill", re.IGNORECASE)
        references = [i for i, line in enumerate(lines) if pattern.search(line)]
        if not references:
            continue

        # A 6-line window around a reference (two lines above, three below).
        def has_fallback(i: int) -> bool:
            window = " ".join(lines[max(0, i - 2):min(len(lines), i + 4)]).lower()
            return any(indicator in window for indicator in FALLBACK_INDICATORS)

        if not any(has_fallback(i) for i in references):
            first = references[0]
            elsewhere = (
                f" (and {len(references) - 1} more reference(s) in this file)"
                if len(references) > 1 else ""
            )
            errors.append(
                f"  {filepath}:{first+1}: companion skill `{skill_name}` referenced "
                f"without a fallback instruction anywhere in this file{elsewhere}"
            )
    return errors


def check_classification(filepath: Path) -> list[str]:
    """Check that a Degraded/Runtime-limited skill declares its classification.

    What satisfies this is a DECLARATION LINE — a line Markdown renders as prose, whose
    first non-whitespace text is ``_Classification:``, with a value after the colon. A bare
    substring does not, and neither does a declaration quoted inside a fenced or indented
    code block. The word turns up in headings, prose and comments, so anything looser lets
    someone delete the real declaration, leave something resembling one, and pass.

    The value is checked only for being non-empty: this runs over whatever pack it is
    pointed at, and a skill may carry a classification this file has no business vetoing.
    """
    # Lossy, because a single non-UTF-8 byte used to raise UnicodeDecodeError out of here
    # and abort the entire validation run — not an OSError, so the catch below never saw it.
    # A file this cannot decode cleanly is one to REPORT on, not one to die on.
    # check_shipped_files_decode asks that question once, up front; these two readers stay
    # lossy so a file that IS reported as undecodable is still scanned rather than skipped.
    try:
        content = _read_text_lossy(filepath)
    except FileNotFoundError:
        return [f"  File not found: {filepath}"]

    for stripped in prose_lines(content):
        if stripped.startswith(CLASSIFICATION_DECL_PREFIX):
            # Emphasis markers are stripped off the value so a declaration whose whole line
            # is italicised — which every real one is — is not read as having said something
            # when all it carries is its own closing underscore.
            if stripped[len(CLASSIFICATION_DECL_PREFIX):].strip().strip("_*` "):
                return []
            return [
                f"  {filepath}: empty {CLASSIFICATION_DECL_PREFIX} declaration "
                f"(state what degrades and why)"
            ]
    return [
        f"  {filepath}: missing {CLASSIFICATION_DECL_PREFIX} declaration line (required for "
        f"Degraded/Runtime-limited skills; neither the word 'Classification:' in prose nor a "
        f"declaration quoted inside a code block counts as one)"
    ]


def check_progress_declaration(filepath: Path) -> list[str]:
    """Check that an agent-dispatching skill declares a valid `_Progress:` posture.

    Valid: a line beginning ``_Progress:`` whose value starts with 'observable' or
    'bounded'. A DECLARATION check — it asserts the skill has consciously chosen a posture,
    not that any progress file is written. A progress file is off the correctness path by
    design and read by nothing, so runtime emission is unverifiable. See PORTABILITY.md
    "Progress Reporting".
    """
    # Lossy, because a single non-UTF-8 byte used to raise UnicodeDecodeError out of here
    # and abort the entire validation run — not an OSError, so the catch below never saw it.
    # A file this cannot decode cleanly is one to REPORT on, not one to die on.
    # check_shipped_files_decode asks that question once, up front; these two readers stay
    # lossy so a file that IS reported as undecodable is still scanned rather than skipped.
    try:
        content = _read_text_lossy(filepath)
    except FileNotFoundError:
        return [f"  File not found: {filepath}"]

    # prose_lines, not splitlines. A `_Progress:` line quoted inside a fenced block used to
    # satisfy this check, while `check_classification` has carved fences out all along — and
    # its docstring's claim that the two parse the same way is what kept the gap invisible.
    # Anyone could delete the real declaration, leave an example behind, and pass.
    for stripped in prose_lines(content):
        if stripped.startswith(PROGRESS_DECL_PREFIX):
            # Markdown emphasis stripped off the ends BEFORE the word-boundary test. The
            # declaration is written italic — `_Progress: bounded_` — and `_` is a word
            # character, so without this the boundary never fires on a bare posture and the
            # minimal, wholly valid form would be rejected.
            value = stripped[len(PROGRESS_DECL_PREFIX):].strip().strip("_*").strip().lower()
            if PROGRESS_DECL_RE.match(value):
                return []
            return [
                f"  {filepath}: invalid _Progress: declaration "
                f"(expected 'observable' or 'bounded', got {stripped!r})"
            ]
    return [
        f"  {filepath}: missing _Progress: declaration (required for sub-agent-dispatching "
        f"skills; use 'observable' to offer an append-only progress file, or 'bounded' for "
        f"request/response dispatch — see PORTABILITY.md 'Progress Reporting')"
    ]


def _is_subprocess_spawn_call(node: ast.Call) -> bool:
    """True if ``node`` invokes a subprocess-style spawn (``subprocess.run``, ``Popen``…).

    Matches both attribute calls (``subprocess.run`` / ``sp.run``) and bare-name
    calls (``run`` after ``from subprocess import run``) whose final name is one of
    :data:`_SUBPROCESS_SPAWN_FUNCS`. This is intentionally lenient on the module
    alias so an aliased import cannot smuggle a hardcoded brand past the check.
    """
    func = node.func
    if isinstance(func, ast.Attribute):
        return func.attr in _SUBPROCESS_SPAWN_FUNCS
    if isinstance(func, ast.Name):
        return func.id in _SUBPROCESS_SPAWN_FUNCS
    return False


def check_engine_portability(filepath: Path) -> list[str]:
    """Fail if the bundled engine hardcodes a branded CLI name AS AN INVOCATION.

    The participant CLIs (``claude`` / ``codex``) must arrive as argv DATA from the SKILL.md
    adapter config. This parses the engine's AST and flags a branded name appearing as a
    string literal inside a subprocess-style spawn call. The scan is confined to those
    calls' descendants, so a branded word in a comment, docstring or error message is never
    flagged; a call spawning an injected argv variable carries no string literals.

    Deliberately conservative: it inspects ALL descendant string literals (argv AND kwargs).
    """
    try:
        source = filepath.read_text(encoding="utf-8")
    except FileNotFoundError:
        return [f"  File not found: {filepath}"]

    try:
        tree = ast.parse(source, filename=str(filepath))
    except SyntaxError as exc:
        return [f"  {filepath}: could not parse engine for portability check: {exc}"]

    errors: list[str] = []
    seen: set[tuple[int, str]] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not _is_subprocess_spawn_call(node):
            continue
        for sub in ast.walk(node):
            if isinstance(sub, ast.Constant) and isinstance(sub.value, str):
                match = _BRANDED_CLI_RE.search(sub.value)
                if match is None:
                    continue
                line = getattr(sub, "lineno", getattr(node, "lineno", 0))
                key = (line, match.group(0).lower())
                if key in seen:
                    continue
                seen.add(key)
                errors.append(
                    f"  {filepath}:{line}: hardcoded branded CLI name "
                    f"'{match.group(0)}' used as a subprocess invocation "
                    f"(branded CLIs must be injected as argv data from the adapter "
                    f"config, not hardcoded in the engine)"
                )
    return errors


# Files a skill MUST ship beside its SKILL.md, keyed by skill name. A skill whose adapter
# argv or prose points at a companion that is not packaging-enforced is the defect this
# table prevents: plan-run's accelerator doc told users to pass `--output-schema <file>`
# while no such file existed in the skill. Structured-output schemas belong here for the
# same reason — an adapter referencing ⟪schema_path⟫ cannot render its command without one.
REQUIRED_COMPANIONS = {
    "plan-duel": [*PLAN_DUEL_COMPANIONS, PLAN_DUEL_ENGINE, PLAN_DUEL_SCHEMA],
    "diff-review": ["review_runner.py", "review-schema.json"],
    "plan-run": [
        "references/phase-worker-contract.md",
        "references/phase-worker-schema.json",
    ],
}


SKILL_BUDGETS_FILENAME = "skill-budgets.json"


def measure_skill_words(skill_dir: Path) -> int:
    """Prose a runtime reads while working: every skill-root ``*.md`` plus
    ``references/**/*.md``.

    Skill-root markdown is counted WHOLESALE rather than just ``SKILL.md``, because a skill
    may keep companion prose beside it that the engine feeds to models every round.
    Budgeting only ``SKILL.md`` makes the ratchet trivially avoidable: move prose into a
    sibling and the count falls while the model still reads every word.

    Three exclusions, all the same argument — the ratchet governs prose that accretes, not
    artifacts whose size is set by what they do: a ``DECISIONS.md``, because counting it
    would score a relocation out of ``SKILL.md`` as zero reduction; **a bundled engine** at
    the skill root, which is executed rather than read; and **non-markdown under
    ``references/``**, because budget-locking a schema would make adding a field need a raise.
    """
    total = 0
    for path, relative, suffix in walk_tree_files(skill_dir):
        if suffix != ".md" or str(relative) == LEDGER_FILENAME:
            continue
        total += len(_read_text_lossy(path).split())
    return total


def check_skill_budgets(skills_dir: Path, budgets_path: Path) -> list[str]:
    """Fail when a skill's runtime-read word count exceeds its recorded budget.

    One direction only — this function is the CEILING. Shrinking always passes; growing
    requires raising the recorded number in the same change, where a reviewer sees the prose
    and the new ceiling together.

    It is bounded from below elsewhere, because slack satisfies a ceiling:
    `tests/test_skill_budgets.py` holds every recorded number EQUAL to its measured count.
    Coverage is asserted in both directions — a skill with no budget, and a budget with no
    skill — because a ratchet that silently skips a file is indistinguishable from one that
    passes it. The v1 suite is excluded and may hold no budget.
    """
    errors: list[str] = []
    try:
        # STRICT utf-8, deliberately not `_read_text_lossy`. That reader falls back to
        # latin-1 so one stray byte cannot abort a prose SCAN; here the file is the datum
        # the whole check reads from, and silently reinterpreting a corrupt one as valid
        # would fail open on the ratchet itself. A BOM surfaces below as a JSON error.
        recorded = json.loads(budgets_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return [f"  {budgets_path}: missing skill budget file (the size ratchet cannot run)"]
    except UnicodeDecodeError as exc:
        return [f"  {budgets_path}: not valid UTF-8 ({exc}); the ratchet refuses to guess"]
    except json.JSONDecodeError as exc:
        return [f"  {budgets_path}: not parseable JSON ({exc})"]
    if not isinstance(recorded, dict):
        return [f"  {budgets_path}: expected a JSON object of skill -> word budget"]

    on_disk = {p.parent.name for p in iter_skill_roots(skills_dir)}
    for name in sorted(recorded):
        if name in V1_SUITE_SKILLS:
            errors.append(
                f"  {budgets_path}: '{name}' is in the superseded v1 suite and must not "
                f"carry a budget — the ratchet does not apply to it"
            )
        elif name not in on_disk:
            errors.append(
                f"  {budgets_path}: '{name}' has a recorded budget but no skill on disk; "
                f"remove the stale entry"
            )
    for name in sorted(on_disk - V1_SUITE_SKILLS):
        if name not in recorded:
            errors.append(
                f"  {budgets_path}: '{name}' has no recorded budget; add one recording "
                f"its current skill-root *.md + references/**/*.md word count"
            )
            continue
        budget = recorded[name]
        if not isinstance(budget, int) or isinstance(budget, bool):
            errors.append(f"  {budgets_path}: '{name}' budget must be an integer, got {budget!r}")
            continue
        actual = measure_skill_words(skills_dir / name)
        if actual > budget:
            errors.append(
                f"  skills/{name}: {actual} words exceeds its recorded budget of {budget} "
                f"(+{actual - budget}). Cut it back, or raise the number in "
                f"scripts/{SKILL_BUDGETS_FILENAME} in this same change so a reviewer sees both."
            )
    return errors


def check_companion_files(skills_dir: Path) -> list[str]:
    """Check each skill ships the companions its SKILL.md depends on.

    A skill's companions ship as a unit alongside its ``SKILL.md`` (the installer
    copies the whole directory); each missing file is a separate error. Skills absent
    from the tree are skipped, so this is safe to run over a partial pack.
    """
    errors = []
    for skill_name, companions in sorted(REQUIRED_COMPANIONS.items()):
        skill_dir = skills_dir / skill_name
        if not (skill_dir / "SKILL.md").exists():
            continue
        for companion in companions:
            companion_path = skill_dir / companion
            if not companion_path.exists():
                errors.append(
                    f"  {companion_path}: missing {skill_name} companion file "
                    f"'{companion}' (required when SKILL.md is present)"
                )
    return errors


def check_shipped_json(skills_dir: Path) -> list[str]:
    """Every ``.json`` shipped under skills/ must be a parseable JSON object.

    Structured-output schemas are handed straight to a CLI's flag parser, so a stray comma
    turns into a failed spawn deep inside a paid run — and the inline form re-serializes the
    document, which cannot happen if it does not decode. Non-object JSON is rejected too: a
    schema is always an object.
    """
    errors: list[str] = []
    if not skills_dir.is_dir():
        return errors
    # The shared walk, not `rglob("*.json")` plus a private `__pycache__` test. That test was
    # one of the three separate residue filters this phase collapsed, and the glob folded no
    # case, so a `SCHEMA.JSON` shipped unparsed. Symlinks are refused here for the same reason
    # they are everywhere else: parsing a link parses whatever it resolves to.
    for filepath, _relative, suffix in walk_tree_files(skills_dir):
        if suffix != ".json":
            continue
        try:
            document = json.loads(filepath.read_text(encoding="utf-8"))
        except OSError as exc:
            errors.append(f"  {filepath}: shipped JSON could not be read: {exc}")
            continue
        except ValueError as exc:
            errors.append(f"  {filepath}: shipped JSON is not valid JSON: {exc}")
            continue
        if not isinstance(document, dict):
            errors.append(
                f"  {filepath}: shipped JSON must be an object at the top level "
                f"(got {type(document).__name__})"
            )
    return errors


def check_portability_md(repo_root: Path) -> list[str]:
    """Check that PORTABILITY.md exists with required sections.

    Looks at the repository root, not inside the skills directory.

    **Anchored to the start of a PROSE line, and that is three separate fixes.** An
    unanchored ``re.search(rf"## {section}")`` let a heading satisfy it three ways without
    existing:

    * demoted to ``### Allowed``, which CONTAINS ``## Allowed`` as a substring;
    * written mid-sentence, since nothing required it to begin a line;
    * quoted inside a code fence, an example of the contract rather than an instance.
    """
    portability = repo_root / "PORTABILITY.md"
    errors = []

    if not portability.exists():
        return ["  PORTABILITY.md not found in repository root"]

    content = portability.read_text(encoding="utf-8")
    lines = content.splitlines()
    prose = "\n".join(
        line for line, is_code in zip(lines, markdown_code_flags(lines)) if not is_code
    )
    for section_pattern in REQUIRED_PORTABILITY_SECTIONS:
        # `## ` exactly: `re.MULTILINE` anchors `^` to each line, and the space after the
        # two hashes is what rejects `###`.
        if not re.search(rf"^## {section_pattern}", prose, re.MULTILINE):
            errors.append(f"  PORTABILITY.md: missing required section matching '## {section_pattern}'")

    return errors


def check_readme_inventory(readme_path: Path, skill_names: list[str],
                           skills_dir: Path | None = None) -> list[str]:
    """Check that README's skill inventory stays in sync with the skills listing.

    Contract: the backticked skill names in the rows of the table under the
    ``## Skill Inventory`` heading must equal, as a set (order-insensitive), the
    directory names in ``skill_names``; and every literal ``<N> skills`` figure in
    the README prose must equal ``len(skill_names)``.
    """
    try:
        content = readme_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return [f"  File not found: {readme_path}"]

    errors = []

    section = re.search(
        r"^## Skill Inventory$(.*?)(?=^## |\Z)", content, re.MULTILINE | re.DOTALL
    )
    if not section:
        return [f"  {readme_path}: no '## Skill Inventory' section found"]

    listed = set()
    declared_in_readme: dict[str, str] = {}
    for line in section.group(1).splitlines():
        row = re.match(r"\|\s*`([^`]+)`\s*\|", line.strip())
        if row:
            listed.add(row.group(1))
        # The Classification column, captured separately so a row that is missing it does
        # not silently drop out of the inventory check above.
        full_row = re.match(r"\|\s*`([^`]+)`\s*\|[^|]*\|\s*([^|]+?)\s*\|\s*$", line.strip())
        if full_row:
            declared_in_readme[full_row.group(1)] = full_row.group(2)

    expected = set(skill_names)
    for missing in sorted(expected - listed):
        errors.append(
            f"  {readme_path}: skill inventory table is missing a row for '{missing}' (present under skills/)"
        )
    for extra in sorted(listed - expected):
        errors.append(
            f"  {readme_path}: skill inventory table lists '{extra}', which is not under skills/"
        )

    for count in re.findall(r"\b(\d+) skills\b", content):
        if int(count) != len(expected):
            errors.append(
                f"  {readme_path}: prose says '{count} skills' but skills/ contains {len(expected)}"
            )

    # The Classification column must agree with what the skill declares. The README is where
    # someone decides whether a skill will work for them, and it was free to say `Full`
    # about a skill whose own text says `Degraded` — the two were checked separately and
    # compared never. All sixteen agree today, so this is a regression guard.
    if skills_dir is not None:
        for name in sorted(expected & set(declared_in_readme)):
            actual = declared_classification(skills_dir / name / "SKILL.md")
            if actual.lower() != declared_in_readme[name].strip().lower():
                errors.append(
                    f"  {readme_path}: inventory says '{name}' is "
                    f"'{declared_in_readme[name].strip()}' but the skill declares "
                    f"'{actual}' — a reader chooses a skill on this column"
                )

    return errors


def declared_classification(skill_md: Path) -> str:
    """A skill's declared classification, defaulting to ``Full``.

    Absence IS the declaration for a Full skill: only ``Degraded`` and ``Runtime-limited``
    are required to say so, per ``CLASSIFICATION_REQUIRED_MARKERS``. Read through
    ``prose_lines`` so a declaration quoted inside a fence is an example rather than an
    instance — the same reading the other three parse sites use.
    """
    try:
        content = skill_md.read_text(encoding="utf-8")
    except OSError:
        return "Full"
    for line in prose_lines(content):
        if line.startswith(CLASSIFICATION_DECL_PREFIX):
            value = line[len(CLASSIFICATION_DECL_PREFIX):].strip().strip("_*").strip()
            # The first word is the classification; everything after it is the explanation
            # every real declaration carries ("Degraded — assertions run in any runtime …").
            return re.split(r"[\s—–-]", value, 1)[0].strip() or "Full"
    return "Full"


def check_skill_name_frontmatter(skills_dir: Path) -> list[str]:
    """Check every skill's frontmatter ``name:`` equals its directory basename.

    CONTRIBUTING.md has always required the two to agree, and nothing enforced it: every
    other rule derives skill names from directory paths alone, so a rename that leaves
    ``name:`` behind produces a skill that installs under one name and declares another.
    During a suite-wide rename that stale ``name:`` collides with the skill taking the
    vacated directory.
    """
    errors = []
    for skill_md in iter_skill_roots(skills_dir):
        expected = skill_md.parent.name
        declared = _parse_frontmatter(skill_md).get("name", "")
        if not declared:
            errors.append(
                f"  {skill_md}: frontmatter has no 'name:' field; it must equal the "
                f"directory basename '{expected}'"
            )
        elif declared != expected:
            errors.append(
                f"  {skill_md}: frontmatter name '{declared}' does not match directory "
                f"basename '{expected}' (the directory name is the installed name)"
            )
    return errors


def check_v1_description_disjointness(skills_dir: Path) -> list[str]:
    """Check each ``-v1`` skill's description can be told apart from its counterpart.

    A superseded skill ships alongside the canonical one, so both descriptions are in front
    of the dispatcher at once. Two requirements, both on the description's OPENING SENTENCE
    (see ``_opening_sentence`` for the exact definition):

    1. it must state the **choose-me condition**, which for a superseded skill means naming
       its generation (``v1``); and
    2. it must not be the counterpart's opening sentence, since two skills whose descriptions
       open identically are indistinguishable at dispatch time.

    A ``-v1`` skill with no unqualified counterpart is still held to (1).
    """
    errors = []
    for skill_md in iter_skill_roots(skills_dir, V1_SUFFIX):
        v1_name = skill_md.parent.name
        opening = _opening_sentence(_parse_frontmatter(skill_md).get("description", ""))
        # As a whole token: "Use v10 plans." names a different generation, and
        # "env1 compatibility" names none at all.
        if not _V1_TOKEN_RE.search(opening):
            errors.append(
                f"  {skill_md}: description does not state its choose-me condition — "
                f"the opening sentence must name the generation it serves "
                f"('{V1_SUFFIX.lstrip('-')}'), but opens with: {opening!r}"
            )

        counterpart = skills_dir / v1_name[: -len(V1_SUFFIX)] / "SKILL.md"
        if not counterpart.exists():
            continue
        counterpart_opening = _opening_sentence(
            _parse_frontmatter(counterpart).get("description", "")
        )
        if opening.lower() == counterpart_opening.lower():
            errors.append(
                f"  {skill_md}: description shares its opening sentence with "
                f"'{counterpart.parent.name}' ({opening!r}); the two are dispatched "
                f"against each other, so their openings must be disjoint"
            )
    return errors


# Files that legitimately contain private-looking strings, because their job is to. Two
# entries, both fixtures, both named rather than pattern-matched — an allowlist that grows
# by glob stops being an allowlist.
# The only files that may name the maintainer: repository identity and its own docs.
# Everything else gets the strict set, because a handle in any of those is a paste.
OWNER_NAMING_FILES = frozenset({
    "LICENSE", "NOTICE", "SECURITY.md", "CODE_OF_CONDUCT.md", "README.md",
    "CHANGELOG.md", "CONTRIBUTING.md", "PORTABILITY.md",
    ".github/CODEOWNERS", ".github/ISSUE_TEMPLATE/config.yml",
    # An agent-instruction file names the repository it belongs to, the same way the
    # README does. Listed rather than skipped, so its home paths are still caught.
    "CLAUDE.md",
    # The Agent Plugins manifest. Its `homepage` and `repository` fields ARE the
    # repository's identity, and the URL it carries is the published one the README badge
    # already links. Relaxed, never skipped: a home path or a workspace-private name in it
    # is still caught.
    "plugin.json",
})

HYGIENE_ALLOWLIST = frozenset({
    "tests/test_validate_private_paths.md",          # negative fixture for this check
    "tests/test_validate_hardcoded_attribution.md",  # negative fixture for the other one
    "scripts/private-identifiers.txt",               # the workspace's list; never shipped
})

# Directories with nothing shipped in them, or nothing readable as source.
#
# A top-level `plans/` holds working notes. Those name whatever repositories the work
# touches, constantly, because that is what they are for; they are notes about the work,
# not part of the product. Named explicitly rather than pattern-matched: a skip that grows
# by glob stops being reviewable. Skipped only at the repository ROOT — a nested directory
# of the same name is content.
HYGIENE_SKIP_ROOTS = frozenset({"plans", "node_modules", ".venv"})
# `.git` and `__pycache__` are NOT listed here. Both are build residue and both are pruned
# by the walk itself (`BUILD_RESIDUE_DIRS`); a second list naming them would be a second
# copy of a rule that already has one, and that is how residue filters get out of step.


def tracked_paths(root: Path) -> frozenset[str] | None:
    """Repo-relative paths git tracks under ``root``, or ``None`` when git cannot answer.

    **Used to SUBTRACT noise, never to enumerate.** The walk remains the single answer to
    "which files exist"; this only removes files that are not part of the project. With no
    git, nothing is subtracted and the behaviour is unchanged.

    It has to be optional, because **this validator ships** and a user runs it against an
    installed pack that is nobody's git repository. Branching the *scope* on git would give
    their tree a different answer than ours.

    An untracked file is scratch work, build residue or a stale checkout, and nothing carries
    it to anyone else — and on a working copy with the usual residue it produces hundreds of
    violations, enough to make the sweep useless as a signal.

    **Fails closed two ways**, because a wrong answer here switches the guard off silently:

    * a non-zero exit means git could not answer — including a stray empty ``/tmp/.git``,
      which makes ``git`` in a temporary directory exit 128 rather than report an empty
      repository;
    * an empty set is treated as no answer, since a repository that tracks nothing tells us
      nothing.

    **The paths line up because ``ls-files`` is relative to the directory it is asked in.**
    At a repository root it prints ``nested/inner.txt``; at ``nested/`` it prints
    ``inner.txt``. So a temporary directory inside an unrelated repository gets an empty
    answer rather than that repository's file list.
    """
    try:
        result = subprocess.run(
            ["git", "-C", str(root), "ls-files", "-z"],
            capture_output=True, timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    # `os.fsdecode`, not `text=True`. git writes raw path bytes and `-z` disables the
    # C-quoting that would hide them; text mode then decodes with the LOCALE's encoding.
    # Under `LC_ALL=C` a file named `café.md` raises UnicodeDecodeError — a ValueError the
    # `except` above misses. Worse on Windows: cp1252 decodes those bytes silently to a name
    # matching no walked path, so the file reads as untracked and is SUBTRACTED — the guard
    # off, scanning nothing, reporting clean. `os.fsdecode` uses the filesystem's own codec
    # with `surrogateescape`, so the value compares equal to what `os.walk` produced.
    paths = frozenset(os.fsdecode(p) for p in result.stdout.split(b"\0") if p)
    return paths or None


def iter_hygiene_targets(root: Path):
    """Yield ``(path, rel, patterns, is_symlink)`` for every file the hygiene sweep judges.

    **Separated from the sweep so its scope has one home.** ``tests/test_skill_content.py``
    audits the same file set for dead exemption markers, and re-deriving that scope from the
    constants goes stale the moment either moves. ``patterns`` is part of the answer, not a
    detail left to the caller: choosing the relaxed set for the identity documents is a
    scope decision.
    """
    # Walked FIRST, then filtered — the walk is still the only thing that enumerates. `git`
    # is asked once, and only to subtract files that are not part of the project; see
    # `tracked_paths` for why subtracting is not the same as branching the scope.
    candidates = [
        (p, "/".join(r.parts), r.parts, s, link)
        for p, r, s, link in _walk_tree(root)
    ]
    tracked = tracked_paths(root)

    for filepath, rel, rel_parts, suffix, is_symlink in candidates:
        # Not tracked, so it is not part of the project, and a file nothing carries onward
        # is not a file this sweep protects. Skipping it is not a narrowing of coverage but
        # a removal of noise — measured on a working copy carrying the usual residue, 645
        # violations, every one of them untracked.
        if tracked is not None and rel not in tracked:
            continue
        # Anchored to the repository ROOT. `parts` intersection matched at any depth, so
        # `skills/cyw/plans/leak.md` was skipped — inside the one directory whose text is
        # the product. A top-level `plans/` holds working notes; a nested one is a skill's
        # own content and gets scanned like everything else.
        if rel_parts and rel_parts[0] in HYGIENE_SKIP_ROOTS:
            continue
        # Build residue is pruned by the walk now (`BUILD_RESIDUE_DIRS`), which is why the
        # old `HYGIENE_SKIP_ANYWHERE` set is gone: it was the third of three residue filters.
        # A stray `.pyc` OUTSIDE `__pycache__` is still skipped by suffix — it is compiled
        # output wherever it sits, and legitimately embeds the path it was built from.
        if suffix == ".pyc":
            continue
        if rel in HYGIENE_ALLOWLIST:
            continue
        # Two pattern sets. Skill text is the PRODUCT: it must be generic, so it may not
        # name the maintainer or carry a private path shape at all. The identity documents
        # in OWNER_NAMING_FILES — LICENSE, SECURITY.md, CODEOWNERS, the README badge —
        # exist to name their owner, so they get the relaxed set. Nothing else does.
        #
        # Chosen ONCE, here, above both branches. When it meant "anything outside skills/"
        # the relaxation covered both installers, the CI workflow and the whole test corpus;
        # and while the symlink branch kept an older rule the file branch had dropped, an
        # identical target was caught under skills/ and missed at the root. A rule with two
        # implementations has two behaviours.
        patterns = (DOC_PRIVATE_PATH_PATTERNS if rel in OWNER_NAMING_FILES
                    else PRIVATE_PATH_PATTERNS)
        yield filepath, rel, patterns, is_symlink


def sweep_content_hygiene(root: Path) -> list[str]:
    """Apply the universal content-hygiene rules to EVERY file in the repository.

    **This walks the whole tree, and that is the point.** Scoping the sweep to `skills/`
    shrinks coverage silently: a fake private path in the root docs, the CI workflow or
    `scripts/private-identifiers.txt.example` passes clean, and path-handling code outside
    `skills/` is where a real home path gets pasted. That example file *invites* a user to
    type their internal repo names, and `.gitignore` protects the copy, not the template.

    Mixed-encoding files are scanned lossily rather than skipped (see ``_read_text_lossy``).
    Compiled-cache noise is excluded: a locally generated ``.pyc`` legitimately embeds the
    absolute build path.
    """
    errors = []
    # The shared traversal, via `iter_hygiene_targets`, which holds this sweep's own scope.
    # Sharing the walk is not sharing the question: "every file in the repository" differs
    # from "which files belong to this skill". What the walk contributes is the part that
    # was quietly different at every call site — `rglob` swallowed the OSError from an
    # unreadable directory, so a repository could sweep clean because a directory was
    # unreadable rather than because it held nothing.
    for filepath, _rel, patterns, is_symlink in iter_hygiene_targets(root):
        # A symlink's TARGET is content: git stores the target string in the object, so a
        # link pointing at an absolute home path publishes that path even though the link
        # may be dangling and `is_file()` therefore false. Read the target, not the file.
        if is_symlink:
            # `readlink` raises on a Windows junction, which the walk labels a link for the
            # same reason it labels a symlink one. Unguarded, the sweep died there and the
            # top-level OSError catch replaced every finding accumulated so far with one
            # generic line.
            #
            # Guarded, but NOT skipped: an unreadable target is reported. Skipping fails
            # open — this sweep's job is to say whether a private path is present, and "I
            # could not look" is not "there is none".
            try:
                target = str(filepath.readlink())
            except OSError as exc:
                errors.append(
                    f"  {filepath}: this is a link whose target could not be read, so it "
                    f"was NOT scanned for private paths ({exc})"
                )
                continue
            for pattern in patterns:
                if pattern.search(target):
                    errors.append(
                        f"  {filepath}: symlink target '{target}' contains a "
                        f"private/project-specific reference '{pattern.pattern}'"
                    )
            continue
        if not filepath.is_file():
            continue
        errors.extend(check_private_paths(filepath, patterns))
        errors.extend(check_hardcoded_attribution(filepath))
    return errors


def check_shipped_files_decode(skills_dir: Path) -> list[str]:
    """Every shipped text file under ``skills_dir`` decodes as strict UTF-8.

    A precondition, not a style rule: the rest of the module reads these files with
    ``read_text(encoding="utf-8")`` at eight separate sites, and one undecodable byte took
    the whole validation down with a traceback that did not name the file.

    ``.md`` / ``.py`` / ``.json``, because those are what the shipped skills carry. A binary
    asset under ``references/`` is nobody's text.

    It walks with the shared traversal, so it reaches exactly the files the scans it protects
    reach. A private ``rglob`` differs three ways — case-sensitive suffixes while discovery
    folds case, reading through a symlink, and swallowing :class:`OSError` so an unreadable
    directory passes for files never opened.
    """
    errors: list[str] = []
    for path, _relative, suffix in walk_tree_files(skills_dir):
        if suffix not in (".md", ".py", ".json"):
            continue
        try:
            path.read_text(encoding="utf-8")
        except UnicodeDecodeError as exc:
            errors.append(f"  {path}: not valid UTF-8 ({exc})")
        except OSError as exc:
            errors.append(f"  {path}: could not be read ({exc})")
    return errors


def validate_skills(skills_dir: Path, repo_root: Path) -> list[str]:
    """Run all validations against the skills directory."""
    all_errors = []

    # Check PORTABILITY.md at repo root
    all_errors.extend(check_portability_md(repo_root))

    # Before anything reads a shipped file: is every one of them decodable?
    #
    # Discovery reads strict UTF-8 and catches only OSError, so a single bad byte in any
    # SKILL.md aborted the run with a raw UnicodeDecodeError — the offending path nowhere in
    # the traceback — before the private-path sweep, the banned phrases or the budget ratchet
    # had run. Routing the two reading sites through _read_text_lossy is not sufficient: the
    # run then dies identically at the next of eight strict reads. So the question is asked
    # once, up front, and reported the way a bad skill-budgets.json is: name the file and say
    # what is wrong with it.
    undecodable = check_shipped_files_decode(skills_dir)
    if undecodable:
        return all_errors + undecodable

    # What the walk refused, said out loud. Both generators exist so a refusal is REPORTED,
    # and a refusal nobody reports is the same as no refusal: a skill skipped for being a
    # symlink is indistinguishable from a skill that passed every rule.
    all_errors.extend(check_refused_symlinks(skills_dir))

    # Auto-discover skills and classification-required skills
    skill_artifacts = discover_skill_artifacts(skills_dir)
    degraded_or_limited = discover_degraded_or_limited(skills_dir)

    if not skill_artifacts:
        all_errors.append(f"  No skills discovered under {skills_dir}")
        return all_errors

    # Collect skill names (the directories containing a SKILL.md)
    skill_names = sorted({a.split("/")[0] for a in skill_artifacts})

    # Rules are applied by INTENT, not blanket over every file: a bundled engine is a
    # discovered artifact so the portability rule can scan it, but the markdown-prose rules
    # below are substring/structure scans written for human-facing skill text and would
    # false-positive on an innocent comment or docstring. So: universal content-hygiene
    # rules apply to any shipped file; the AST portability rule to the engine; the
    # skill-doc-prose rules to markdown only.
    # Universal content hygiene — EVERY file in the repository, not just skills/.
    all_errors.extend(sweep_content_hygiene(repo_root))

    for artifact in skill_artifacts:
        filepath = skills_dir / artifact

        # Case is folded HERE as well as in the walk, and that is not belt-and-braces: the
        # walk lowers the suffix it yields, but this loop re-derives one from the artifact
        # path. Widening discovery to reach `GUIDE.MD` without folding here would discover
        # the file and then match neither branch, so it would receive no rule at all.
        suffix = filepath.suffix.lower()

        # Bundled-engine portability (AST, precise) — Python source only.
        if suffix == ".py":
            all_errors.extend(check_engine_portability(filepath))

        # Skill-doc prose / structure — markdown only (meaningless or false-positive
        # prone on code).
        if suffix == ".md":
            all_errors.extend(check_unterminated_fence(filepath))
            all_errors.extend(check_banned_phrases(filepath))
            all_errors.extend(check_companion_skill_fallbacks(filepath))
            all_errors.extend(check_stale_runtime_claims(filepath))
            # How far below its skill root the file sits, counted from the artifact name:
            # `<skill>/<file>` is depth 0, `<skill>/references/<file>` is 1,
            # `<skill>/references/deep/<file>` is 2. Counting separators rather than
            # testing `== 1` is the whole fix for the depth-2 false positive — the artifact
            # name already carried the real depth, and the caller was throwing it away.
            all_errors.extend(
                check_self_contained_skill_refs(filepath, depth=artifact.count("/") - 1)
            )
            # The complement of the rule above: that one asks whether a reference leaves
            # the skill, this one whether it lands on anything.
            all_errors.extend(
                check_bundled_refs_resolve(
                    filepath, skills_dir / artifact.split("/")[0]
                )
            )
            # Suite routing inside the superseded planning skills. Every other file
            # is inert here — the flag, not the file, is what turns the rule on.
            all_errors.extend(
                check_v1_suite_routing(
                    filepath, in_v1_suite=artifact.split("/")[0] in V1_SUITE_SKILLS
                )
            )
            all_errors.extend(check_codex_skill_paths(filepath))
            all_errors.extend(check_spawn_permissions(filepath))
            all_errors.extend(check_independence_ladder(filepath))
            all_errors.extend(check_cross_skill_references(filepath, skill_names))

    for artifact in DOC_ARTIFACTS:
        filepath = repo_root / artifact
        # No private-path call here. The whole-tree hygiene sweep already scans README with
        # the same relaxed pattern set (it is in `OWNER_NAMING_FILES`), so this call only
        # ever reported the identical finding a second time. Two reports of one defect is
        # not twice the coverage; it is a reader deciding whether they are the same defect.
        all_errors.extend(check_codex_skill_paths(filepath))

    # Check Classification declarations for Degraded/Limited skills
    for artifact in degraded_or_limited:
        filepath = skills_dir / artifact
        all_errors.extend(check_classification(filepath))

    # Check progress-posture declarations for sub-agent-dispatching skills
    for artifact in discover_agent_dispatchers(skills_dir):
        filepath = skills_dir / artifact
        all_errors.extend(check_progress_declaration(filepath))

    # A skill's directory name is its installed name; the declared name must agree
    all_errors.extend(check_skill_name_frontmatter(skills_dir))

    # A superseded '-v1' skill must be dispatch-distinguishable from its counterpart
    all_errors.extend(check_v1_description_disjointness(skills_dir))

    # Check plan-duel companion files
    all_errors.extend(check_companion_files(skills_dir))

    # Every shipped .json (structured-output schemas) must actually parse
    all_errors.extend(check_shipped_json(skills_dir))

    # Check README's skill inventory stays in sync with the skills listing
    all_errors.extend(check_readme_inventory(repo_root / "README.md", skill_names, skills_dir))

    # The size ratchet's CEILING: prose a runtime reads may not grow past its recorded budget
    # without that number moving in the same change. Shrinking passes here, but not overall —
    # `tests/test_skill_budgets.py` holds the number equal to the measured count, so a shrink
    # lowers it too.
    all_errors.extend(
        check_skill_budgets(skills_dir, repo_root / "scripts" / SKILL_BUDGETS_FILENAME)
    )

    return all_errors


def _rejected_for(label: str, findings: list[str], because: str) -> list[str]:
    """No finding, or no finding SAYING ``because``, is a fixture failure.

    **Rejection alone is not evidence the intended rule fired.** Several checks report more
    than one kind of defect, so a fixture written for rule A that trips rule B satisfies a
    bare ``if not check(f)`` and proves nothing about A. ``because`` is a distinguishing
    fragment of the intended finding's text, so the assertion names the rule instead of
    counting the findings.
    """
    if any(because in finding for finding in findings):
        return []
    return [
        f"  FIXTURE FAIL: {label} should have been rejected by a finding saying "
        f"{because!r}; got {findings or 'no findings at all'}"
    ]


def run_test_fixtures(fixtures_dir: Path) -> list[str]:
    """Validate test fixtures produce expected results."""
    errors = []

    # Test: file with banned phrases should fail
    f = fixtures_dir / "test_validate_banned_phrases.md"
    if f.exists():
        errors += _rejected_for("test_validate_banned_phrases.md",
                                check_banned_phrases(f), "banned phrase")
    else:
        errors.append(f"  Fixture not found: {f}")

    # Test: clean file should pass
    f = fixtures_dir / "test_validate_clean.md"
    if f.exists():
        result = check_banned_phrases(f)
        if result:
            errors.append(f"  FIXTURE FAIL: test_validate_clean.md should have passed but was rejected: {result}")
    else:
        errors.append(f"  Fixture not found: {f}")

    # Test: missing classification should fail
    f = fixtures_dir / "test_validate_missing_classification.md"
    if f.exists():
        errors += _rejected_for("test_validate_missing_classification.md",
                                check_classification(f), "missing _Classification: declaration")
    else:
        errors.append(f"  Fixture not found: {f}")

    # Test: EVERY classification-required skill stays required once its declaration is gone.
    # Discovery finds a skill either by name or by reading the very line this rule requires,
    # so a skill missing from CLASSIFICATION_REQUIRED_SKILLS goes unchecked the moment
    # someone deletes its `_Classification:` line.
    #
    # The five names are written out rather than read from the constant: a loop over the set
    # would shrink with it. Each case runs through to a real ERROR, not just to discovery.
    for skill_name in ("demo-video", "diff-review", "plan-duel", "security-review-codebase",
                       "web-verify"):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_skills = Path(tmpdir)
            skill_dir = tmp_skills / skill_name
            skill_dir.mkdir()
            (skill_dir / "SKILL.md").write_text(
                f"# {skill_name}\n\nNo classification declaration here.\n", encoding="utf-8"
            )
            artifact = f"{skill_name}/SKILL.md"
            if artifact not in discover_degraded_or_limited(tmp_skills):
                errors.append(
                    f"  FIXTURE FAIL: {skill_name} without classification should still be "
                    "classification-required"
                )
            elif not check_classification(tmp_skills / artifact):
                errors.append(
                    f"  FIXTURE FAIL: {skill_name} without classification was flagged but "
                    "produced no error"
                )

    # Test: only a DECLARATION LINE satisfies the classification rule. The word in a
    # heading, in prose or inside a comment is not one; neither is a prefix missing its
    # underscore or its value; and neither is a declaration Markdown renders as CODE.
    #
    # The indentation cases are a pair on purpose. "Ignore indented lines" would be wrong —
    # a declaration inside a list item is indented and real — so the boundary is
    # CommonMark's: three spaces is a paragraph, the fourth makes it code. Accepted shapes
    # sit alongside the rejected ones so a rule that refused everything could not pass.
    classification_shapes = [
        ("prose", "# Skill\n\nSee the Classification: table in the README.\n", True),
        ("heading", "# Skill\n\n## Classification: Degraded\n", True),
        # Three comment cases, and only the LAST one actually exercises comments. The first
        # has `Classification:` with no leading underscore; the second is a one-liner, so the
        # `<!--` prefix keeps the stripped line from starting with the marker. Both were
        # green before prose_lines blanked comments. The multi-line form — what a person
        # writes when commenting a line out — is the case that fails without the fix.
        ("comment", "# Skill\n\n<!-- Classification: Degraded -->\n", True),
        ("commented-out-declaration",
         "# Skill\n\n<!-- _Classification: Degraded — an old example._ -->\n", True),
        ("multiline-commented-out-declaration",
         "# Skill\n\n<!--\n_Classification: Degraded — an old example._\n-->\n", True),
        ("no-underscore", "# Skill\n\nClassification: Degraded — needs a CLI.\n", True),
        ("no-colon", "# Skill\n\n_Classification Degraded_ — needs a CLI.\n", True),
        ("empty-value", "# Skill\n\n_Classification:\n", True),
        ("empty-italics", "# Skill\n\n_Classification:_\n", True),
        ("fenced", "# Skill\n\n```\n_Classification: Degraded — an example._\n```\n", True),
        ("fenced-with-info",
         "# Skill\n\n```markdown\n_Classification: Degraded — an example._\n```\n", True),
        ("tilde-fenced", "# Skill\n\n~~~\n_Classification: Degraded — an example._\n~~~\n", True),
        ("four-space-code", "# Skill\n\n    _Classification: Degraded — an example._\n", True),
        ("tab-code", "# Skill\n\n\t_Classification: Degraded — an example._\n", True),
        ("declaration", "# Skill\n\n_Classification: Degraded — needs a CLI on PATH._\n", False),
        ("indented-declaration", "# Skill\n\n  _Classification: Degraded — needs ffmpeg._\n", False),
        ("three-space-declaration",
         "# Skill\n\n   _Classification: Degraded — one under the code boundary._\n", False),
        ("after-a-closed-fence",
         "# Skill\n\n```\nnot a declaration\n```\n\n_Classification: Degraded — the real one._\n",
         False),
    ]
    with tempfile.TemporaryDirectory() as tmpdir:
        for label, body, should_fail in classification_shapes:
            probe = Path(tmpdir) / f"classification-{label}.md"
            probe.write_text(body, encoding="utf-8")
            rejected = bool(check_classification(probe))
            if should_fail and not rejected:
                errors.append(
                    f"  FIXTURE FAIL: a '{label}' mention is not a _Classification: "
                    "declaration and should have been rejected"
                )
            elif not should_fail and rejected:
                errors.append(
                    f"  FIXTURE FAIL: a real _Classification: declaration ('{label}') was "
                    "rejected"
                )

    # Test: an undecodable shipped file is REPORTED by name, not raised out of the run.
    #
    # One 0x97 in any SKILL.md used to abort everything with a UnicodeDecodeError from
    # pathlib — the offending path nowhere in the traceback, no finding of any kind
    # printed, the private-path sweep and the budget ratchet never reached.
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_skills = Path(tmpdir) / "skills"
        broken = tmp_skills / "mojibake-skill"
        broken.mkdir(parents=True)
        (broken / "SKILL.md").write_bytes(b"# Skill\n\nAn em dash written as cp1252: \x97\n")
        decode_errors = check_shipped_files_decode(tmp_skills)
        if not decode_errors:
            errors.append(
                "  FIXTURE FAIL: a SKILL.md holding a non-UTF-8 byte should have been "
                "reported as undecodable"
            )
        elif "mojibake-skill/SKILL.md" not in decode_errors[0].replace("\\", "/"):
            errors.append(
                "  FIXTURE FAIL: the undecodable-file report must name the file; got "
                f"{decode_errors[0]!r}"
            )
        # And the whole run degrades rather than dying: this is the call that used to
        # raise instead of returning a list.
        try:
            run_errors = validate_skills(tmp_skills, Path(tmpdir))
        except UnicodeDecodeError:
            errors.append(
                "  FIXTURE FAIL: validate_skills raised UnicodeDecodeError instead of "
                "reporting the file"
            )
        else:
            if not any("not valid UTF-8" in e for e in run_errors):
                errors.append(
                    "  FIXTURE FAIL: a full validation run over a tree holding an "
                    "undecodable file did not report it"
                )

    # Test: discovery reads the same rule as the check. A skill that merely QUOTES a
    # declaration in a fence has not declared one, and if only the check knew that the two
    # sites would disagree in the worst direction: discovery flags the skill on the example,
    # and the check then reports its declaration missing.
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_skills = Path(tmpdir)
        quoting = tmp_skills / "contract-quoting-skill"
        quoting.mkdir()
        (quoting / "SKILL.md").write_text(
            "# Contract-quoting skill\n\nDeclare it like this:\n\n```\n"
            "_Classification: Degraded — needs a CLI on PATH._\n```\n",
            encoding="utf-8",
        )
        if "contract-quoting-skill/SKILL.md" in discover_degraded_or_limited(tmp_skills):
            errors.append(
                "  FIXTURE FAIL: a fenced example is not a declaration, so a skill quoting "
                "one must not be discovered as classification-required"
            )

    # The same pairing for an HTML comment, which is where the two sites actually did
    # disagree: discovery returned the skill on the strength of a commented-out line while
    # the check accepted that same line as satisfying the requirement. Both directions are
    # asserted, because fixing only one would reopen the hole from the other side.
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_skills = Path(tmpdir)
        commented = tmp_skills / "commented-out-skill"
        commented.mkdir()
        (commented / "SKILL.md").write_text(
            "# Commented-out skill\n\n<!--\n"
            "_Classification: Degraded — an old example._\n-->\n",
            encoding="utf-8",
        )
        if "commented-out-skill/SKILL.md" in discover_degraded_or_limited(tmp_skills):
            errors.append(
                "  FIXTURE FAIL: a declaration inside an HTML comment renders as nothing, "
                "so a skill carrying only that must not be discovered as "
                "classification-required"
            )

    # Test: companion skill without fallback should fail
    f = fixtures_dir / "test_validate_no_fallback.md"
    if f.exists():
        errors += _rejected_for("test_validate_no_fallback.md",
                                check_companion_skill_fallbacks(f),
                                "companion skill `cyw` referenced without a fallback")
    else:
        errors.append(f"  Fixture not found: {f}")

    # Test: gate companion skills (web-verify / diff-review) without fallback should fail
    f = fixtures_dir / "test_validate_gate_skill_no_fallback.md"
    if f.exists():
        errors += _rejected_for("test_validate_gate_skill_no_fallback.md",
                                check_companion_skill_fallbacks(f),
                                "companion skill `diff-review` referenced without a fallback")
    else:
        errors.append(f"  Fixture not found: {f}")

    # Test: companion skill with fallback should pass
    f = fixtures_dir / "test_validate_with_fallback.md"
    if f.exists():
        result = check_companion_skill_fallbacks(f)
        if result:
            errors.append(f"  FIXTURE FAIL: test_validate_with_fallback.md should have passed but was rejected: {result}")
    else:
        errors.append(f"  Fixture not found: {f}")

    # Test: a fallback stated ONCE covers every later reference to the same companion.
    # The rule is about the document, not the sentence — restating the degraded path at
    # every mention is the duplication the pack spent a plan removing.
    f = fixtures_dir / "test_validate_repeated_reference_fallback.md"
    if f.exists():
        result = check_companion_skill_fallbacks(f)
        if result:
            errors.append(
                "  FIXTURE FAIL: test_validate_repeated_reference_fallback.md states the "
                f"fallback once and should have passed, but was rejected: {result}"
            )
    else:
        errors.append(f"  Fixture not found: {f}")

    # Test: multi-line adapter block should pass
    f = fixtures_dir / "test_validate_multiline_adapter.md"
    if f.exists():
        result = check_banned_phrases(f)
        if result:
            errors.append(f"  FIXTURE FAIL: test_validate_multiline_adapter.md should have passed but was rejected: {result}")
    else:
        errors.append(f"  Fixture not found: {f}")

    # Test: private paths should fail
    f = fixtures_dir / "test_validate_private_paths.md"
    if f.exists():
        errors += _rejected_for("test_validate_private_paths.md",
                                check_private_paths(f), "private/project-specific reference")
    else:
        errors.append(f"  Fixture not found: {f}")

    # Test: a clone's own private identifiers load from the side file, and a missing
    # file is the normal case rather than an error. Driven over a temp file rather than
    # the real one, which most clones do not have — this check has to pass there too.
    with tempfile.TemporaryDirectory() as tmp:
        side = Path(tmp) / "private-identifiers.txt"
        if _load_extra_private_patterns(side):
            errors.append("  FIXTURE FAIL: a missing private-identifiers.txt should yield no patterns")
        side.write_text("# a comment\n\n\\bacme_internal\\b\n", encoding="utf-8")
        loaded = _load_extra_private_patterns(side)
        if [p.pattern for p in loaded] != [r"\bacme_internal\b"]:
            errors.append(f"  FIXTURE FAIL: private-identifiers.txt should load one pattern, got {loaded}")
        probe = Path(tmp) / "probe.md"
        probe.write_text("The acme_internal service is referenced here.\n", encoding="utf-8")
        errors += _rejected_for("a loaded private identifier in a skill file",
                                check_private_paths(probe, loaded), "acme_internal")
        side.write_text("[unclosed\n", encoding="utf-8")
        try:
            _load_extra_private_patterns(side)
        except ValueError:
            pass
        else:
            errors.append("  FIXTURE FAIL: an uncompilable private-identifier line should raise, not be skipped")

    # Test: hardcoded vendor attribution should fail
    f = fixtures_dir / "test_validate_hardcoded_attribution.md"
    if f.exists():
        errors += _rejected_for("test_validate_hardcoded_attribution.md",
                                check_hardcoded_attribution(f),
                                "hardcoded vendor attribution email")
    else:
        errors.append(f"  Fixture not found: {f}")

    # Test: stale runtime capability claims should fail
    f = fixtures_dir / "test_validate_stale_runtime_claim.md"
    if f.exists():
        # Per PATTERN, not per file. One line used to trip patterns 1 and 2 together while
        # pattern 3 had no line at all, so a pattern that stopped matching left the fixture
        # still rejecting and the loss invisible. Asserting each pattern's own reported
        # string makes the fixture's coverage equal to the rule's.
        stale_findings = check_stale_runtime_claims(f)
        for pattern in STALE_RUNTIME_CLAIM_PATTERNS:
            errors += _rejected_for("test_validate_stale_runtime_claim.md",
                                    stale_findings, f"claim '{pattern.pattern}' found")
    else:
        errors.append(f"  Fixture not found: {f}")

    # Test: ~/.codex/skills path guidance should fail
    f = fixtures_dir / "test_validate_codex_skill_path.md"
    if f.exists():
        errors += _rejected_for("test_validate_codex_skill_path.md",
                                check_codex_skill_paths(f), "Codex skill path")
    else:
        errors.append(f"  Fixture not found: {f}")

    # Test: a spawned `codex exec` with no sandbox flag should fail (both the
    # line-wrapped shell form and the JSON argv form).
    f = fixtures_dir / "test_validate_spawn_permission.md"
    if f.exists():
        result = check_spawn_permissions(f)
        if len(result) != 3:
            errors.append(
                f"  FIXTURE FAIL: test_validate_spawn_permission.md should produce 3 errors "
                f"(shell + JSON missing sandbox, plus sandbox without approval pin) but got "
                f"{len(result)}: {result}"
            )
    else:
        errors.append(f"  Fixture not found: {f}")

    # Test: explicit sandbox modes (and a bare prose mention) should pass
    f = fixtures_dir / "test_validate_spawn_permission_clean.md"
    if f.exists():
        result = check_spawn_permissions(f)
        if result:
            errors.append(
                f"  FIXTURE FAIL: test_validate_spawn_permission_clean.md should have passed "
                f"but was rejected: {result}"
            )
    else:
        errors.append(f"  Fixture not found: {f}")

    # Test: missing plan-duel companion files should fail
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_skills = Path(tmpdir)
        plan_duel = tmp_skills / "plan-duel"
        plan_duel.mkdir()
        (plan_duel / "SKILL.md").write_text("# Plan Duel\n", encoding="utf-8")
        # Engine and schema present so only the 3 missing .md companions are counted
        # here (each has its own missing-file fixture below).
        (plan_duel / "plan_duel.py").write_text("import subprocess\n", encoding="utf-8")
        (plan_duel / PLAN_DUEL_SCHEMA).write_text('{"type": "object"}\n', encoding="utf-8")
        # Deliberately omit init.md, round.md, summary.md
        result = check_companion_files(tmp_skills)
        if len(result) != 3:
            errors.append(
                f"  FIXTURE FAIL: missing companion files should produce 3 errors but got {len(result)}: {result}"
            )

    # Test: unknown skill reference should fail
    f = fixtures_dir / "test_validate_unknown_skill_reference.md"
    if f.exists():
        known = {"cyw", "tdd", "plan-init"}
        result = check_cross_skill_references(f, sorted(known))
        if not result:
            errors.append(
                "  FIXTURE FAIL: test_validate_unknown_skill_reference.md should have been rejected but passed"
            )
    else:
        errors.append(f"  Fixture not found: {f}")

    # Test: a '../' path that escapes the skill directory should fail (skill-root file).
    f = fixtures_dir / "test_validate_plan_duel_relative_prompt.md"
    if f.exists():
        errors += _rejected_for("test_validate_plan_duel_relative_prompt.md",
                                check_self_contained_skill_refs(f, depth=0),
                                "resolves 1 level above the skill directory")
    else:
        errors.append(f"  Fixture not found: {f}")

    # Test: a reference to a repo-root doc (PORTABILITY.md) should fail — it is not
    # installed with the skill.
    f = fixtures_dir / "test_validate_skill_root_doc_ref.md"
    if f.exists():
        errors += _rejected_for("test_validate_skill_root_doc_ref.md",
                                check_self_contained_skill_refs(f, depth=0),
                                "reference to repo-root doc 'PORTABILITY.md'")
    else:
        errors.append(f"  Fixture not found: {f}")

    # Test: the `references/`-level escape THRESHOLD, at the depth those files are judged at.
    # The clean fixture below proves one `../` is allowed there; nothing proved two is not.
    # Asserted on the reported level count, not on non-emptiness, because at depth 0 the same
    # file is rejected for a different reason and a truthiness check could not tell them apart.
    f = fixtures_dir / "test_validate_skill_references_escape.md"
    if f.exists():
        result = check_self_contained_skill_refs(f, depth=1)
        if not any("1 level above" in r for r in result):
            errors.append(
                "  FIXTURE FAIL: test_validate_skill_references_escape.md should report a "
                f"'../../' as escaping 1 level above a references/ file; got {result}"
            )
        if not any("2 levels above" in r for r in result):
            errors.append(
                "  FIXTURE FAIL: test_validate_skill_references_escape.md should report a "
                f"'../../../' as escaping 2 levels above a references/ file; got {result}"
            )
    else:
        errors.append(f"  Fixture not found: {f}")

    # Test: a file that ends inside a code fence should be reported in its own right.
    # Mid-file, where the scan re-pairs and the state machine ends clean. Its own case rather
    # than folded into the one below: they are caught by different halves of the rule, and a
    # fixture that trips either would satisfy a single assertion while leaving the other half
    # untested.
    f = fixtures_dir / "test_validate_unterminated_fence_midfile.md"
    if f.exists():
        errors += _rejected_for("test_validate_unterminated_fence_midfile.md",
                                check_unterminated_fence(f), "odd number of code-fence")
    else:
        errors.append(f"  Fixture not found: {f}")

    f = fixtures_dir / "test_validate_unterminated_fence.md"
    if f.exists():
        errors += _rejected_for("test_validate_unterminated_fence.md",
                                check_unterminated_fence(f), "is never closed")
    else:
        errors.append(f"  Fixture not found: {f}")

    # Test: a repo-rooted `skills/<other>/...` path should fail. It is the same defect as
    # the `../` escape in a spelling the escape rule cannot see, because it climbs out of
    # nothing — it starts from a `skills/` root that an installed skill does not have.
    f = fixtures_dir / "test_validate_skill_sibling_path.md"
    if f.exists():
        errors += _rejected_for("test_validate_skill_sibling_path.md",
                                check_self_contained_skill_refs(f, depth=0),
                                "repo-rooted path 'skills/diff-review/review_runner.py'")
    else:
        errors.append(f"  Fixture not found: {f}")

    # Test: a references/ doc that stays inside its own skill (a single '../' to the skill
    # root, an in-project plans/README.md) must PASS — the single-'../' allowance and the
    # README/CHANGELOG exclusion are what keep this rule false-positive-free.
    f = fixtures_dir / "test_validate_skill_selfcontained_clean.md"
    if f.exists():
        result = check_self_contained_skill_refs(f, depth=1)
        if result:
            errors.append(
                f"  FIXTURE FAIL: test_validate_skill_selfcontained_clean.md should have passed but was rejected: {result}"
            )
    else:
        errors.append(f"  Fixture not found: {f}")

    # Test: `check_portability_md` had no negative fixture at all, so its unanchored search
    # could only ever be observed passing. All three evasions are built from the REAL
    # document rather than a stub, so a mutation is one substitution away from the positive
    # control and the difference between them is exactly the property under test.
    real_portability = fixtures_dir.parent / "PORTABILITY.md"
    if real_portability.exists():
        source = real_portability.read_text(encoding="utf-8")
        evasions = {
            "a required section demoted to '###'":
                source.replace("\n## Allowed", "\n### Allowed", 1),
            "a required section quoted inside a code fence":
                source.replace("\n## Allowed", "\n```\n## Allowed\n```\n", 1),
            "a required section removed outright":
                source.replace("\n## Allowed", "\n## Removed-Entirely", 1),
        }
        with tempfile.TemporaryDirectory() as tmp:
            probe_root = Path(tmp)
            for label, mutated in evasions.items():
                if mutated == source:
                    errors.append(
                        f"  FIXTURE FAIL: the PORTABILITY.md probe for {label} changed "
                        f"nothing — the heading it edits has been renamed, so this case is "
                        f"no longer being tested"
                    )
                    continue
                (probe_root / "PORTABILITY.md").write_text(mutated, encoding="utf-8")
                errors += _rejected_for(
                    f"PORTABILITY.md with {label}",
                    check_portability_md(probe_root),
                    "missing required section matching '## Allowed'",
                )
            # The control: unmodified, it must pass. Without this the three above are
            # satisfied by a rule that rejects everything.
            (probe_root / "PORTABILITY.md").write_text(source, encoding="utf-8")
            control = check_portability_md(probe_root)
            if control:
                errors.append(
                    f"  FIXTURE FAIL: the shipped PORTABILITY.md must satisfy its own "
                    f"section check; got {control}"
                )
    else:
        errors.append(f"  Fixture not found: {real_portability}")

    # Test: the size ratchet and the counter under it. Neither had `--test-fixtures`
    # coverage, so a contributor running the two documented gate commands and not `unittest`
    # never exercised the ceiling that governs every skill's prose.
    with tempfile.TemporaryDirectory() as tmp:
        probe_skills = Path(tmp) / "skills"
        (probe_skills / "demo" / "references").mkdir(parents=True)
        (probe_skills / "demo" / "SKILL.md").write_text(
            "---\nname: demo\ndescription: d\n---\n\none two three four five\n",
            encoding="utf-8")
        # Counted too — `references/**` is inside the ratchet's scope, and a counter that
        # silently stopped descending would make every budget look satisfiable.
        (probe_skills / "demo" / "references" / "extra.md").write_text(
            "six seven eight\n", encoding="utf-8")
        measured = measure_skill_words(probe_skills / "demo")
        if measured <= 5:
            errors.append(
                f"  FIXTURE FAIL: measure_skill_words counted {measured} words for a skill "
                f"whose references/ holds three of them — the counter is not descending"
            )
        budgets = Path(tmp) / "budgets.json"
        budgets.write_text(json.dumps({"demo": measured - 1}), encoding="utf-8")
        errors += _rejected_for("a skill one word over its recorded budget",
                                check_skill_budgets(probe_skills, budgets),
                                "exceeds its recorded budget")
        budgets.write_text(json.dumps({"demo": measured}), encoding="utf-8")
        at_budget = check_skill_budgets(probe_skills, budgets)
        if at_budget:
            errors.append(
                f"  FIXTURE FAIL: a skill exactly AT its budget must pass the ceiling; "
                f"got {at_budget}"
            )
        # A skill with no recorded budget at all must fail — the ratchet has no default,
        # which is what stops a new skill shipping unmeasured.
        budgets.write_text(json.dumps({}), encoding="utf-8")
        errors += _rejected_for("a skill with no recorded budget",
                                check_skill_budgets(probe_skills, budgets), "demo")

    # Test: README inventory drifted from the skills listing should fail — for
    # ALL three drift kinds (missing row, extra row, stale prose count)
    f = fixtures_dir / "test_validate_readme_inventory.md"
    if f.exists():
        drifted = "\n".join(
            check_readme_inventory(f, ["alpha-skill", "beta-skill", "gamma-skill"])
        )
        if (
            "gamma-skill" not in drifted
            or "retired-skill" not in drifted
            or "4 skills" not in drifted
        ):
            errors.append(
                "  FIXTURE FAIL: test_validate_readme_inventory.md should be rejected "
                f"for missing row + extra row + stale count, but got: {drifted!r}"
            )
    else:
        errors.append(f"  Fixture not found: {f}")

    # Test: the content-hygiene sweep covers non-artifact assets (e.g. a
    # references/ shell helper) that artifact discovery does not list
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_skills = Path(tmpdir)
        helper_skill = tmp_skills / "demo-skill"
        (helper_skill / "references").mkdir(parents=True)
        (helper_skill / "SKILL.md").write_text("# Demo\n", encoding="utf-8")
        (helper_skill / "references" / "helper.sh").write_text(
            "cp ~/projects/x .\n", encoding="utf-8")  # hygiene-exempt: fixture data
        result = sweep_content_hygiene(tmp_skills)
        if len(result) != 1:
            errors.append(
                "  FIXTURE FAIL: content-hygiene sweep should flag the non-artifact "
                f"helper.sh exactly once but got {len(result)}: {result}"
            )

    # Test: a mixed-encoding text file (one invalid UTF-8 byte) must still be
    # scanned — the sweep must not fail open on a single bad byte, and compiled
    # __pycache__ noise must not be swept at all
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_skills = Path(tmpdir)
        mixed_skill = tmp_skills / "demo-skill"
        (mixed_skill / "__pycache__").mkdir(parents=True)
        (mixed_skill / "SKILL.md").write_text("# Demo\n", encoding="utf-8")
        (mixed_skill / "notes.txt").write_bytes(
            b"see ~/projects/x\n\xff\n")  # hygiene-exempt: fixture data
        (mixed_skill / "__pycache__" / "demo.cpython-312.pyc").write_bytes(
            b"\x00/home/somebody/private\x00"  # hygiene-exempt: fixture data
        )
        result = sweep_content_hygiene(tmp_skills)
        joined = "\n".join(result)
        if len(result) != 1 or "notes.txt" not in joined or "~/projects" not in joined:  # hygiene-exempt: asserts on fixture data
            errors.append(
                "  FIXTURE FAIL: mixed-encoding notes.txt should yield exactly its one "
                f"finding (and __pycache__ should be excluded) but got {len(result)}: {result}"
            )

    # Test: README inventory in sync with the skills listing should pass
    f = fixtures_dir / "test_validate_readme_inventory_clean.md"
    if f.exists():
        result = check_readme_inventory(f, ["alpha-skill", "beta-skill", "gamma-skill"])
        if result:
            errors.append(
                f"  FIXTURE FAIL: test_validate_readme_inventory_clean.md should have passed but was rejected: {result}"
            )
    else:
        errors.append(f"  Fixture not found: {f}")

    # Test: reference docs under a skill's references/ are discovered as gated artifacts
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_skills = Path(tmpdir)
        ref_skill = tmp_skills / "demo-skill"
        (ref_skill / "references").mkdir(parents=True)
        (ref_skill / "SKILL.md").write_text("# Demo\n", encoding="utf-8")
        (ref_skill / "references" / "guide.md").write_text("# Guide\n", encoding="utf-8")
        discovered = discover_skill_artifacts(tmp_skills)
        if "demo-skill/references/guide.md" not in discovered:
            errors.append(
                "  FIXTURE FAIL: reference docs under references/ should be discovered as gated artifacts"
            )

    # Test: the plan-duel engine is discovered as a gated artifact when present
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_skills = Path(tmpdir)
        plan_duel = tmp_skills / "plan-duel"
        plan_duel.mkdir()
        (plan_duel / "SKILL.md").write_text("# Plan Duel\n", encoding="utf-8")
        (plan_duel / "plan_duel.py").write_text("import subprocess\n", encoding="utf-8")
        discovered = discover_skill_artifacts(tmp_skills)
        if "plan-duel/plan_duel.py" not in discovered:
            errors.append(
                "  FIXTURE FAIL: plan-duel/plan_duel.py should be discovered as a gated artifact"
            )

    # Test: missing plan-duel engine should fail (engine required when SKILL.md present)
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_skills = Path(tmpdir)
        plan_duel = tmp_skills / "plan-duel"
        plan_duel.mkdir()
        (plan_duel / "SKILL.md").write_text("# Plan Duel\n", encoding="utf-8")
        for companion in PLAN_DUEL_COMPANIONS:
            (plan_duel / companion).write_text("# companion\n", encoding="utf-8")
        (plan_duel / PLAN_DUEL_SCHEMA).write_text('{"type": "object"}\n', encoding="utf-8")
        # Deliberately omit plan_duel.py — the engine require must flag it.
        result = check_companion_files(tmp_skills)
        if len(result) != 1:
            errors.append(
                f"  FIXTURE FAIL: missing plan-duel engine should produce exactly 1 error but got {len(result)}: {result}"
            )

    # Test: missing plan-duel judge schema should fail. Without it a judge adapter
    # referencing ⟪schema_path⟫/⟪schema_json⟫ cannot render its command at all.
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_skills = Path(tmpdir)
        plan_duel = tmp_skills / "plan-duel"
        plan_duel.mkdir()
        (plan_duel / "SKILL.md").write_text("# Plan Duel\n", encoding="utf-8")
        for companion in PLAN_DUEL_COMPANIONS:
            (plan_duel / companion).write_text("# companion\n", encoding="utf-8")
        (plan_duel / "plan_duel.py").write_text("import subprocess\n", encoding="utf-8")
        # Deliberately omit judge-schema.json — the schema require must flag it.
        result = check_companion_files(tmp_skills)
        if len(result) != 1 or PLAN_DUEL_SCHEMA not in result[0]:
            errors.append(
                f"  FIXTURE FAIL: missing plan-duel judge schema should produce exactly "
                f"1 error naming {PLAN_DUEL_SCHEMA} but got {len(result)}: {result}"
            )

    # Test: the companion require is not plan-duel-only — a skill whose adapter argv
    # references a schema must have that schema packaging-enforced too.
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_skills = Path(tmpdir)
        diff_review = tmp_skills / "diff-review"
        diff_review.mkdir()
        (diff_review / "SKILL.md").write_text("# Diff Review\n", encoding="utf-8")
        (diff_review / "review_runner.py").write_text("import subprocess\n", encoding="utf-8")
        # Deliberately omit review-schema.json.
        result = check_companion_files(tmp_skills)
        if len(result) != 1 or "review-schema.json" not in result[0]:
            errors.append(
                f"  FIXTURE FAIL: a missing diff-review schema companion should produce "
                f"exactly 1 error naming review-schema.json but got {len(result)}: {result}"
            )

    # Test: the judge schema is discovered as a gated artifact when present
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_skills = Path(tmpdir)
        plan_duel = tmp_skills / "plan-duel"
        plan_duel.mkdir()
        (plan_duel / "SKILL.md").write_text("# Plan Duel\n", encoding="utf-8")
        (plan_duel / PLAN_DUEL_SCHEMA).write_text('{"type": "object"}\n', encoding="utf-8")
        discovered = discover_skill_artifacts(tmp_skills)
        if f"plan-duel/{PLAN_DUEL_SCHEMA}" not in discovered:
            errors.append(
                f"  FIXTURE FAIL: plan-duel/{PLAN_DUEL_SCHEMA} should be discovered as a gated artifact"
            )

    # Test: shipped-JSON validity. A malformed schema (or a non-object one) must fail
    # at validate time rather than inside a spawned CLI's flag parser mid-run; a
    # well-formed one must pass.
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_skills = Path(tmpdir)
        json_skill = tmp_skills / "demo-skill"
        json_skill.mkdir(parents=True)
        (json_skill / "SKILL.md").write_text("# Demo\n", encoding="utf-8")
        (json_skill / "good.json").write_text('{"type": "object"}\n', encoding="utf-8")
        if check_shipped_json(tmp_skills):
            errors.append(
                "  FIXTURE FAIL: a well-formed shipped .json should pass the validity check"
            )
        (json_skill / "broken.json").write_text('{"type": "object",}\n', encoding="utf-8")
        (json_skill / "not-an-object.json").write_text("[1, 2, 3]\n", encoding="utf-8")
        result = check_shipped_json(tmp_skills)
        joined = "\n".join(result)
        if len(result) != 2 or "broken.json" not in joined or "not-an-object.json" not in joined:
            errors.append(
                f"  FIXTURE FAIL: shipped-JSON check should flag exactly the malformed "
                f"and non-object files but got {len(result)}: {result}"
            )

    # Test: engine-portability rule — the bundled engine must not hardcode a branded
    # CLI name as a subprocess invocation, while a branded word in a docstring,
    # comment, or error-message string must NOT be flagged. Built inside a
    # TemporaryDirectory so the synthetic bad-engine .py is never linted as real
    # project source.
    bad_engine = (
        "import subprocess\n"
        "\n"
        "\n"
        "def run_agent_b(prompt):\n"
        '    return subprocess.run(["claude", "-p", prompt])\n'
    )
    clean_engine = (
        '"""Duel engine. The adapter injects the claude/codex CLI as argv data."""\n'
        "import subprocess\n"
        "\n"
        "\n"
        "def run_cli(argv):\n"
        "    # argv[0] may be claude or codex, injected from the adapter config.\n"
        "    if not argv:\n"
        '        raise ValueError("no claude/codex executable was injected")\n'
        "    return subprocess.run(argv, stdin=subprocess.DEVNULL)\n"
    )
    with tempfile.TemporaryDirectory() as tmpdir:
        bad = Path(tmpdir) / "plan-duel" / "plan_duel.py"
        bad.parent.mkdir(parents=True)
        bad.write_text(bad_engine, encoding="utf-8")
        if not any("branded CLI" in e or "hardcode" in e.lower()
                   for e in check_engine_portability(bad)):
            errors.append(
                '  FIXTURE FAIL: engine hardcoding subprocess.run(["claude", ...]) '
                "should have been flagged but passed"
            )

        clean = Path(tmpdir) / "plan-duel" / "clean_engine.py"
        clean.write_text(clean_engine, encoding="utf-8")
        result = check_engine_portability(clean)
        if result:
            errors.append(
                "  FIXTURE FAIL: clean engine (injected argv; brand only in "
                f"docstring/comment/error) should have passed but was flagged: {result}"
            )

    # Test: the rule reaches a bundled engine in ANY skill, not just plan-duel. Wired to a
    # `skill_name == "plan-duel"` branch in discovery, it never scanned
    # skills/diff-review/review_runner.py — a mandatory companion whose own header claims no
    # branded CLI is baked into it. The rule worked; nothing invoked it. Asserted through
    # DISCOVERY rather than by calling check_engine_portability directly, because calling it
    # directly is exactly what passed while the hole was open.
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_skills = Path(tmpdir) / "skills"
        other = tmp_skills / "some-other-skill"
        other.mkdir(parents=True)
        (other / "SKILL.md").write_text("# Some other skill\n\nBody.\n", encoding="utf-8")
        (other / "its_engine.py").write_text(bad_engine, encoding="utf-8")
        if "some-other-skill/its_engine.py" not in discover_skill_artifacts(tmp_skills):
            errors.append(
                "  FIXTURE FAIL: a skill-root .py outside plan-duel was not discovered, "
                "so the bundled-engine portability rule never runs on it"
            )

    # Test: file-kind rule gating. The markdown-prose "banned phrases" scan is a substring
    # scan for skill TEXT; it must apply to .md skill docs but NOT to the bundled .py engine,
    # where the same word can legitimately appear in a code comment. Same phrase => flagged
    # in a .md companion (control), not flagged in the engine .py.
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_skills = Path(tmpdir) / "skills"
        pd = tmp_skills / "plan-duel"
        pd.mkdir(parents=True)
        (pd / "SKILL.md").write_text(
            "# Plan Duel\n\n_Classification: Degraded — participant CLI on PATH; "
            "needs Python 3.10+._\n",
            encoding="utf-8",
        )
        (pd / "init.md").write_text("# Init\n", encoding="utf-8")
        (pd / "summary.md").write_text("# Summary\n", encoding="utf-8")
        # Engine comment mentions a banned prose phrase — must NOT be flagged (it is
        # code, not skill prose) and is brand-free so the portability rule passes too.
        (pd / "plan_duel.py").write_text(
            "import subprocess\n"
            "# This engine blocks on each subprocess; it never uses run_in_background\n"
            "# or a subagent_type — those are Claude-tool concepts the engine avoids.\n"
            "def run_cli(argv):\n"
            "    return subprocess.run(argv, stdin=subprocess.DEVNULL)\n",
            encoding="utf-8",
        )
        # Control: the SAME banned phrase in a .md companion MUST still be flagged.
        (pd / "round.md").write_text(
            "# Round\n\nUse run_in_background to stream progress.\n", encoding="utf-8"
        )
        result = validate_skills(tmp_skills, Path(tmpdir))
        py_flagged = any(
            "plan_duel.py" in e and "run_in_background" in e for e in result
        )
        md_flagged = any("round.md" in e and "run_in_background" in e for e in result)
        if py_flagged:
            errors.append(
                "  FIXTURE FAIL: a banned prose phrase in an engine .py code comment "
                "should NOT be flagged by the markdown-prose scan"
            )
        if not md_flagged:
            errors.append(
                "  FIXTURE FAIL: a banned phrase in a .md companion should still be "
                "flagged (control — the .md rules must remain active)"
            )

    # Test: _Progress: declaration contract. A valid observable/bounded posture passes;
    # a missing line or an unknown posture fails. (Declaration check only — it does not,
    # and cannot, verify a progress file is actually written.)
    f = fixtures_dir / "test_validate_progress_observable.md"
    if f.exists():
        result = check_progress_declaration(f)
        if result:
            errors.append(
                f"  FIXTURE FAIL: test_validate_progress_observable.md should have passed but was rejected: {result}"
            )
    else:
        errors.append(f"  Fixture not found: {f}")

    f = fixtures_dir / "test_validate_progress_bounded.md"
    if f.exists():
        result = check_progress_declaration(f)
        if result:
            errors.append(
                f"  FIXTURE FAIL: test_validate_progress_bounded.md should have passed but was rejected: {result}"
            )
    else:
        errors.append(f"  Fixture not found: {f}")

    f = fixtures_dir / "test_validate_progress_missing.md"
    if f.exists():
        if not check_progress_declaration(f):
            errors.append(
                "  FIXTURE FAIL: test_validate_progress_missing.md (no _Progress: line) should have been rejected but passed"
            )
    else:
        errors.append(f"  Fixture not found: {f}")

    f = fixtures_dir / "test_validate_progress_invalid.md"
    if f.exists():
        if not check_progress_declaration(f):
            errors.append(
                "  FIXTURE FAIL: test_validate_progress_invalid.md (unknown posture) should have been rejected but passed"
            )
    else:
        errors.append(f"  Fixture not found: {f}")

    # Test: dispatcher discovery — curated set AND content scan flag; a plain skill with
    # no sub-agent language and not in the set is left alone.
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_skills = Path(tmpdir)
        # curated-set member (needs no sub-agent language to be flagged)
        (tmp_skills / "plan-duel").mkdir()
        (tmp_skills / "plan-duel" / "SKILL.md").write_text("# Plan Duel\n", encoding="utf-8")
        # content-scan member (mentions a sub-agent)
        (tmp_skills / "content-dispatch").mkdir()
        (tmp_skills / "content-dispatch" / "SKILL.md").write_text(
            "# X\n\nHand each phase to a fresh sub-agent that returns a status.\n"
        , encoding="utf-8")
        # plain skill — neither in the set nor mentioning a sub-agent
        (tmp_skills / "plain").mkdir()
        (tmp_skills / "plain" / "SKILL.md").write_text("# Plain\n\nRead the file and update it.\n", encoding="utf-8")
        flagged = discover_agent_dispatchers(tmp_skills)
        if "plan-duel/SKILL.md" not in flagged:
            errors.append(
                "  FIXTURE FAIL: a curated-set skill should be flagged as an agent dispatcher"
            )
        if "content-dispatch/SKILL.md" not in flagged:
            errors.append(
                "  FIXTURE FAIL: a skill mentioning a sub-agent should be flagged as an agent dispatcher"
            )
        if "plain/SKILL.md" in flagged:
            errors.append(
                "  FIXTURE FAIL: a skill with no dispatch language should NOT be flagged as a dispatcher"
            )

    # Test: v1 routing, direction 1 — an UNQUALIFIED intra-suite reference in a v1
    # body is rejected, and a self-reference counts (the shape at plan-init's "the
    # user's argument (everything after `/plan-init`)"). Asserted on the exact
    # diagnostic, so this cannot pass for the same reason as the redirect fixture
    # below — the two edge cases must be independently proven.
    f = fixtures_dir / "test_validate_v1_routing_unqualified_self.md"
    if f.exists():
        result = "\n".join(check_v1_suite_routing(f, in_v1_suite=True))
        if "unqualified intra-suite reference" not in result or "plan-init" not in result:
            errors.append(
                "  FIXTURE FAIL: test_validate_v1_routing_unqualified_self.md should be "
                f"rejected for an unqualified self-reference, but got: {result!r}"
            )
        if check_v1_suite_routing(f, in_v1_suite=False):
            errors.append(
                "  FIXTURE FAIL: the v1-routing rule must be inert when in_v1_suite=False"
            )
    else:
        errors.append(f"  Fixture not found: {f}")

    # Test: v1 routing, direction 2 — a '-v1'-QUALIFIED forward redirect inside the
    # `Format: v2` refusal guard is rejected. This is the opposite direction of the
    # rule above: canonical now means v2, so the guard must point at the bare name.
    f = fixtures_dir / "test_validate_v1_routing_qualified_redirect.md"
    if f.exists():
        result = "\n".join(check_v1_suite_routing(f, in_v1_suite=True))
        if "qualified forward redirect" not in result or "plan-phase-v1" not in result:
            errors.append(
                "  FIXTURE FAIL: test_validate_v1_routing_qualified_redirect.md should be "
                f"rejected for a '-v1'-qualified forward redirect, but got: {result!r}"
            )
    else:
        errors.append(f"  Fixture not found: {f}")

    # Test: the redirect rule bans EVERY suffix, not just '-v1'. A guard still pointing at a
    # suffixed name names a directory that does not exist, so it must be rejected too. Also
    # confirms a fenced block butted against a guard does NOT inherit its redirect context:
    # the '-v1' reference inside the fence is correct and must survive.
    with tempfile.TemporaryDirectory() as tmpdir:
        # The suffixed name is ASSEMBLED, not spelled out. A tree-wide grep for a
        # leftover suffixed reference is how a rename gets audited, and this
        # fixture's entire job is to contain one — written contiguously, the linter's
        # own test data would be that grep's only hit.
        stale_name = "plan-run-" + "next"
        stale = Path(tmpdir) / "stale_redirect.md"
        stale.write_text(
            "# Execute Plan (v1)\n\n"
            f"If `plan.md` carries a `Format: v2` marker, stop: run `/{stale_name}`.\n"
            "```\n"
            "Next step: run `/plan-phase-v1 <path>`.\n"
            "```\n"
        , encoding="utf-8")
        result = "\n".join(check_v1_suite_routing(stale, in_v1_suite=True))
        if f"/{stale_name}" not in result:
            errors.append(
                "  FIXTURE FAIL: a forward redirect still carrying a stale "
                f"suffix should be rejected, but got: {result!r}"
            )
        if "plan-phase-v1" in result:
            errors.append(
                "  FIXTURE FAIL: a fenced block adjacent to a guard must not inherit "
                f"the redirect context — its '-v1' reference is correct: {result!r}"
            )

    # Test: token boundaries on both sides. A longer identifier that merely CONTAINS a
    # suite name is a different token and must not be diagnosed; a quoted marker (in a
    # blockquoted example) must not establish a redirect context for live prose.
    with tempfile.TemporaryDirectory() as tmpdir:
        # Assembled for the same reason as the fixture above.
        longer_name = "xplan-run-" + "next"
        edges = Path(tmpdir) / "edges.md"
        edges.write_text(
            "# Notes\n\n"
            f"The `/{longer_name}` helper and the plan-runner daemon are unrelated.\n\n"
            "> An example guard: `Format: v2` sends you to `/plan-run`.\n\n"
            "Then hand off to `/plan-phase` as usual.\n"
        , encoding="utf-8")
        result = "\n".join(check_v1_suite_routing(edges, in_v1_suite=True))
        if longer_name in result or "plan-runner" in result:
            errors.append(
                "  FIXTURE FAIL: a longer identifier containing a suite name is a "
                f"different token and must not be diagnosed: {result!r}"
            )
        if "/plan-phase" not in result:
            errors.append(
                "  FIXTURE FAIL: a blockquoted example mentioning the marker must not "
                f"turn live prose after it into a redirect context: {result!r}"
            )

    # Test: both directions correct in one file — '-v1'-qualified intra-suite
    # references AND an unqualified forward redirect — must pass.
    f = fixtures_dir / "test_validate_v1_routing_clean.md"
    if f.exists():
        result = check_v1_suite_routing(f, in_v1_suite=True)
        if result:
            errors.append(
                "  FIXTURE FAIL: test_validate_v1_routing_clean.md should have passed "
                f"but was rejected: {result}"
            )
    else:
        errors.append(f"  Fixture not found: {f}")

    # Test: frontmatter `name:` must equal the directory basename. A `git mv` that
    # renames the directory and leaves `name:` behind is invisible to every other
    # rule, because skill names are derived from paths alone.
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_skills = Path(tmpdir)
        (tmp_skills / "renamed-skill").mkdir()
        (tmp_skills / "renamed-skill" / "SKILL.md").write_text(
            "---\nname: old-skill\ndescription: >\n  Does a thing.\n---\n\n# X\n"
        , encoding="utf-8")
        (tmp_skills / "matching-skill").mkdir()
        (tmp_skills / "matching-skill" / "SKILL.md").write_text(
            "---\nname: matching-skill\ndescription: >\n  Does a thing.\n---\n\n# X\n"
        , encoding="utf-8")
        # An UNTERMINATED header must not read as well-formed just because the body
        # contains a thematic break further down. This is a real defect a bulk edit
        # produced: it dropped the closing `---`, every skill still "parsed"
        # against the `---` under the overview, and nothing fired.
        (tmp_skills / "unterminated").mkdir()
        (tmp_skills / "unterminated" / "SKILL.md").write_text(
            "---\nname: unterminated\ndescription: >\n  Does a thing.\n\n"
            "# Title\n\n## Overview\n\nProse.\n\n---\n\n## Step 1\n"
        , encoding="utf-8")
        result = "\n".join(check_skill_name_frontmatter(tmp_skills))
        if "unterminated" not in result or "no 'name:' field" not in result:
            errors.append(
                "  FIXTURE FAIL: a SKILL.md whose frontmatter is never closed must be "
                f"rejected, not parsed against a body thematic break: {result!r}"
            )
        if "frontmatter name 'old-skill' does not match" not in result:
            errors.append(
                "  FIXTURE FAIL: a SKILL.md whose name: differs from its directory "
                f"basename should be rejected, but got: {result!r}"
            )
        if "matching-skill" in result:
            errors.append(
                "  FIXTURE FAIL: a SKILL.md whose name: matches its directory basename "
                f"should pass, but got: {result!r}"
            )

    # Test: '-v1' description disjointness. Two independent failures, exercised
    # separately so neither can mask the other, plus a clean pair.
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_skills = Path(tmpdir)

        def _write_skill(name: str, description: str) -> None:
            (tmp_skills / name).mkdir(exist_ok=True)
            (tmp_skills / name / "SKILL.md").write_text(
                f"---\nname: {name}\ndescription: >\n  {description}\n---\n\n# X\n"
            , encoding="utf-8")

        # (a) shared opening sentence — the opening states v1, so only the
        # disjointness half may fire.
        _write_skill("alpha", "Run a v1 or v2 plan to completion. Canonical suite.")
        _write_skill("alpha-v1", "Run a v1 or v2 plan to completion. Superseded suite.")
        result = "\n".join(check_v1_description_disjointness(tmp_skills))
        if "shares its opening sentence" not in result:
            errors.append(
                "  FIXTURE FAIL: a '-v1' description sharing its opening sentence with "
                f"its counterpart should be rejected, but got: {result!r}"
            )
        if "choose-me condition" in result:
            errors.append(
                "  FIXTURE FAIL: the shared-opening case states 'v1' in its opening "
                f"sentence, so the choose-me half must not fire: {result!r}"
            )

        # (b) opening sentence states no choose-me condition — the openings differ,
        # so only the choose-me half may fire.
        _write_skill("beta", "Create a structured plan document. Canonical suite.")
        _write_skill("beta-v1", "Break a plan into ordered phases. Superseded suite.")
        result = "\n".join(
            e for e in check_v1_description_disjointness(tmp_skills) if "beta-v1" in e
        )
        if "choose-me condition" not in result:
            errors.append(
                "  FIXTURE FAIL: a '-v1' description whose opening sentence never names "
                f"the generation should be rejected, but got: {result!r}"
            )
        if "shares its opening sentence" in result:
            errors.append(
                "  FIXTURE FAIL: the no-choose-me case has a distinct opening sentence, "
                f"so the disjointness half must not fire: {result!r}"
            )

        # (c) clean pair — distinct openings, and the '-v1' one names its generation.
        _write_skill("gamma", "Create a structured plan document. Canonical suite.")
        _write_skill("gamma-v1", "Use for a v1 plan with no Format marker. Superseded.")
        result = [e for e in check_v1_description_disjointness(tmp_skills) if "gamma" in e]
        if result:
            errors.append(
                "  FIXTURE FAIL: a disjoint, generation-naming '-v1' description should "
                f"pass, but was rejected: {result}"
            )

        # (d) the generation must appear as a whole token — "v10"/"env1" are not "v1".
        _write_skill("delta", "Create a structured plan document. Canonical suite.")
        _write_skill(
            "delta-v1", "Use for v10, env1, v1alpha and v1_beta plans. Superseded."
        )
        result = "\n".join(
            e for e in check_v1_description_disjointness(tmp_skills) if "delta-v1" in e
        )
        if "choose-me condition" not in result:
            errors.append(
                "  FIXTURE FAIL: 'v10'/'env1' must not satisfy the generation check, "
                f"but got: {result!r}"
            )

    # Test: the three new rules are WIRED INTO validate_skills(), not merely defined.
    # Without this, deleting any of the three calls leaves every fixture above green
    # and the live tree green too (the rules are vacuous on a tree with no v1 suite), so
    # the wiring
    # would be entirely untested.
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_root = Path(tmpdir)
        tmp_skills = tmp_root / "skills"
        # Rule A trip: directory and declared name disagree.
        (tmp_skills / "renamed-skill").mkdir(parents=True)
        (tmp_skills / "renamed-skill" / "SKILL.md").write_text(
            "---\nname: old-skill\ndescription: >\n  Does a thing.\n---\n\n# X\n"
        , encoding="utf-8")
        # Rule B trip (unqualified intra-suite ref) and Rule C trip (opening sentence
        # names no generation) in one v1-suite skill.
        (tmp_skills / "plan-init-v1").mkdir(parents=True)
        (tmp_skills / "plan-init-v1" / "SKILL.md").write_text(
            "---\nname: plan-init-v1\ndescription: >\n  Break a plan into phases.\n---\n\n"
            "# Create Plan\n\nThe user's argument (everything after `/plan-init`).\n"
        , encoding="utf-8")
        wired = "\n".join(validate_skills(tmp_skills, tmp_root))
        if "does not match directory basename" not in wired:
            errors.append(
                "  FIXTURE FAIL: check_skill_name_frontmatter is not wired into "
                f"validate_skills(): {wired!r}"
            )
        if "unqualified intra-suite reference" not in wired:
            errors.append(
                "  FIXTURE FAIL: check_v1_suite_routing is not wired into "
                f"validate_skills() for a plan-*-v1 skill: {wired!r}"
            )
        if "choose-me condition" not in wired:
            errors.append(
                "  FIXTURE FAIL: check_v1_description_disjointness is not wired into "
                f"validate_skills(): {wired!r}"
            )


    return errors

def main():
    # Diagnostics quote the files they check, and those files are full of em dashes. On a
    # console whose encoding cannot represent one, printing a finding would raise
    # UnicodeEncodeError and take down the run *reporting* the problem rather than the
    # problem itself. Reads are pinned to utf-8 at every call site; this pins the other end.
    # Guarded because a caller may have replaced sys.stdout with an object lacking reconfigure.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):  # pragma: no cover - depends on host stream
            pass

    parser = argparse.ArgumentParser(description="Validate cross-platform skill portability")
    parser.add_argument("skills_dir", nargs="?", type=Path, default=None,
                        help="Path to skills directory (default: skills/ relative to repo root)")
    parser.add_argument("--test-fixtures", type=Path, help="Run fixture-based smoke tests")
    args = parser.parse_args()

    # `repo_root` follows the skills directory it was GIVEN, rather than always naming the
    # tree this file lives in. As an unconditional `Path(__file__).parent.parent`, the
    # documented `[skills/]` argument worked against exactly one tree: pointed elsewhere,
    # `check_portability_md`, `sweep_content_hygiene`, `check_readme_inventory` and
    # `check_skill_budgets` kept reading the pack while the skill rules read the caller's
    # tree, so the README inventory and the budget file were compared against skills they
    # had never heard of.
    #
    # Resolved before taking the parent, so a bare relative `skills/` yields the directory
    # containing it rather than `.`.
    if args.skills_dir:
        skills_dir = args.skills_dir
        repo_root = args.skills_dir.resolve().parent
    else:
        repo_root = Path(__file__).parent.parent
        skills_dir = repo_root / "skills"


    if args.test_fixtures:
        errors = run_test_fixtures(args.test_fixtures)
        if errors:
            print("Fixture tests FAILED:")
            for e in errors:
                print(e)
            sys.exit(1)
        else:
            print("All fixture tests passed.")
            sys.exit(0)

    # An unreadable directory is a finding, not a crash. `_walk_tree` raises rather than
    # returning a short list, because a short list validates clean and says nothing; but an
    # exception reaching the top is a traceback naming a pathlib frame instead of the
    # directory. Caught here, once, for every walk in the run.
    try:
        errors = validate_skills(skills_dir, repo_root)
    except UnreadableTree as exc:
        errors = [str(exc)]
    except OSError as exc:
        # `UnreadableTree` covers only what `os.walk` hands to `onerror`. The stat calls the
        # walk makes itself, and every `read`/`readlink` downstream, raise plain OSError — an
        # unreadable parent, a file deleted between the walk and the rule that opens it, a
        # permission that changes mid-run. Those reached the top as a traceback naming a
        # pathlib frame. One catch, one line, same exit code.
        errors = [f"  the validation run could not complete: {exc}"]
    if errors:
        print(f"Validation FAILED ({len(errors)} issues):")
        for e in errors:
            print(e)
        sys.exit(1)
    else:
        print("All validations passed.")
        sys.exit(0)


if __name__ == "__main__":
    main()
