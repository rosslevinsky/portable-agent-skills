#!/usr/bin/env python3
"""Validate skill files against the cross-platform portability contract.

Checks:
- Banned phrases do not appear as normative instructions (outside adapter notes
  and classification declarations)
- Skills classified as Degraded or Runtime-limited have Classification declarations
- Companion-skill references (backtick-quoted skill names like `cyw`) include
  fallback instructions nearby
- PORTABILITY.md exists with required sections
- Private paths and project-specific identifiers do not leak into skill files
- plan-duel companion files (init.md, round.md, summary.md) exist when
  plan-duel/SKILL.md is present

Usage:
    python scripts/validate_cross_runtime.py [skills/]
    python scripts/validate_cross_runtime.py --test-fixtures tests/
"""

import argparse
import re
import sys
import tempfile
from pathlib import Path

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

# Private paths and project-specific identifiers that must not appear in skills
PRIVATE_PATH_PATTERNS = [
    re.compile(r"/home/[^/\s]+"),
    re.compile(r"dotfiles/claude/skills"),
    re.compile(r"~/projects/gh/main"),
    re.compile(r"\blanguage_engine\b"),
    re.compile(r"\bprfastack\b"),
    re.compile(r"\brosslevinsky\b"),
    re.compile(r"\bpixi\.toml\b"),
    re.compile(r"\bnew-app\.py\b"),
    re.compile(r"@prfastack"),
]

DOC_ARTIFACTS = ["README.md"]
DOC_PRIVATE_PATH_PATTERNS = [
    pattern for pattern in PRIVATE_PATH_PATTERNS if pattern.pattern != r"\brosslevinsky\b"
]

HARDCODED_ATTRIBUTION_PATTERNS = [
    re.compile(r"noreply@anthropic\.com", re.IGNORECASE),
    re.compile(r"noreply@openai\.com", re.IGNORECASE),
]

STALE_RUNTIME_CLAIM_PATTERNS = [
    re.compile(r"Codex cannot act as controller because it lacks", re.IGNORECASE),
    re.compile(r"Codex.*lacks sub-agent", re.IGNORECASE),
    re.compile(r"Not supported in single-agent runtimes \(e\.g\., Codex\)", re.IGNORECASE),
]

WORKDIR_RELATIVE_SKILL_REF_PATTERNS = [
    re.compile(r"\.\./plan-init/SKILL\.md"),
]

STALE_CODEX_SKILL_PATH_PATTERNS = [
    re.compile(r"~/\.agents/skills"),
    re.compile(r"\$HOME/\.agents/skills"),
]

# Companion skills that require fallback instructions when referenced
COMPANION_SKILLS = ["cyw", "tdd"]

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

REQUIRED_PORTABILITY_SECTIONS = [
    "Allowed",
    "Banned",
    "Companion",
    "Agent.*Instruction",
    "Parallel",
    "Autonomous",
    "Inline.*Adapter",
]

PLAN_DUEL_COMPANIONS = ["init.md", "round.md", "summary.md"]

# Classification markers that trigger the Classification declaration check
CLASSIFICATION_REQUIRED_MARKERS = ["Degraded", "Runtime-limited"]
CLASSIFICATION_REQUIRED_SKILLS = {
    "plan-duel",
    "security-review-codebase",
    "security-review-codebase-hierarchical",
}


def discover_skill_artifacts(skills_dir: Path) -> list[str]:
    """Discover all SKILL.md files plus known companion files, relative to skills_dir.

    Any directory under skills_dir containing a SKILL.md is a skill. The
    plan-duel skill additionally has companion files that must be packaged.
    """
    artifacts: list[str] = []
    if not skills_dir.is_dir():
        return artifacts
    for skill_md in sorted(skills_dir.glob("*/SKILL.md")):
        skill_name = skill_md.parent.name
        artifacts.append(f"{skill_name}/SKILL.md")
        if skill_name == "plan-duel":
            for companion in PLAN_DUEL_COMPANIONS:
                companion_path = skill_md.parent / companion
                if companion_path.exists():
                    artifacts.append(f"{skill_name}/{companion}")
    return artifacts


def discover_degraded_or_limited(skills_dir: Path) -> list[str]:
    """Discover SKILL.md files whose _Classification: line contains Degraded or Runtime-limited."""
    flagged: set[str] = set()
    if not skills_dir.is_dir():
        return []
    for skill_md in sorted(skills_dir.glob("*/SKILL.md")):
        skill_name = skill_md.parent.name
        if skill_name in CLASSIFICATION_REQUIRED_SKILLS:
            flagged.add(f"{skill_name}/SKILL.md")
        try:
            content = skill_md.read_text()
        except OSError:
            continue
        for line in content.splitlines():
            stripped = line.strip()
            if stripped.startswith("_Classification:"):
                if any(marker in stripped for marker in CLASSIFICATION_REQUIRED_MARKERS):
                    flagged.add(f"{skill_name}/SKILL.md")
                break
    return sorted(flagged)


def is_exempt_line(line: str) -> bool:
    """Check if a line starts an exempt block.

    Exempt contexts (where banned phrases are allowed):
    - Adapter notes: blockquote lines containing 'adapter' (case-insensitive)
    - Classification declarations: lines starting with _Classification:
    """
    stripped = line.strip()
    if stripped.startswith(">") and "adapter" in stripped.lower():
        return True
    if stripped.startswith("_Classification:"):
        return True
    return False


def check_banned_phrases(filepath: Path) -> list[str]:
    """Check for banned phrases outside exempt contexts.

    Exempt contexts are adapter note blockquotes (> lines following an adapter
    header) and _Classification: declarations. Everything else — including
    documentation prose and comments — is checked.
    """
    errors = []
    try:
        content = filepath.read_text()
    except FileNotFoundError:
        return [f"  File not found: {filepath}"]

    in_adapter_block = False
    for i, line in enumerate(content.splitlines(), 1):
        stripped = line.strip()

        if is_exempt_line(line):
            in_adapter_block = True
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


def check_private_paths(filepath: Path, patterns: list[re.Pattern] | None = None) -> list[str]:
    """Check for private paths and project-specific identifiers in skill files."""
    errors = []
    patterns = patterns or PRIVATE_PATH_PATTERNS
    try:
        content = filepath.read_text()
    except FileNotFoundError:
        return [f"  File not found: {filepath}"]

    for i, line in enumerate(content.splitlines(), 1):
        for pattern in patterns:
            if pattern.search(line):
                errors.append(
                    f"  {filepath}:{i}: private/project-specific reference '{pattern.pattern}' found"
                )
    return errors


def check_hardcoded_attribution(filepath: Path) -> list[str]:
    """Check for vendor-specific co-author email defaults in skill files."""
    errors = []
    try:
        content = filepath.read_text()
    except FileNotFoundError:
        return [f"  File not found: {filepath}"]

    for i, line in enumerate(content.splitlines(), 1):
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
        content = filepath.read_text()
    except FileNotFoundError:
        return [f"  File not found: {filepath}"]

    for i, line in enumerate(content.splitlines(), 1):
        for pattern in STALE_RUNTIME_CLAIM_PATTERNS:
            if pattern.search(line):
                errors.append(
                    f"  {filepath}:{i}: stale runtime capability claim '{pattern.pattern}' found"
                )
    return errors


def check_workdir_relative_skill_refs(filepath: Path) -> list[str]:
    """Check for skill paths that break when participant CLIs run from a workdir."""
    errors = []
    try:
        content = filepath.read_text()
    except FileNotFoundError:
        return [f"  File not found: {filepath}"]

    for i, line in enumerate(content.splitlines(), 1):
        for pattern in WORKDIR_RELATIVE_SKILL_REF_PATTERNS:
            if pattern.search(line):
                errors.append(
                    f"  {filepath}:{i}: workdir-relative companion skill path '{pattern.pattern}' found"
                )
    return errors


def check_stale_codex_skill_paths(filepath: Path) -> list[str]:
    """Check for obsolete Codex user-skill install paths."""
    errors = []
    try:
        content = filepath.read_text()
    except FileNotFoundError:
        return [f"  File not found: {filepath}"]

    for i, line in enumerate(content.splitlines(), 1):
        for pattern in STALE_CODEX_SKILL_PATH_PATTERNS:
            if pattern.search(line):
                errors.append(
                    f"  {filepath}:{i}: stale Codex skill path '{pattern.pattern}' found; use $HOME/.codex/skills (or $CODEX_HOME/skills)"
                )
    return errors


def check_cross_skill_references(filepath: Path, known_skills: list[str]) -> list[str]:
    """Check that every backtick-quoted skill-name reference exists in the pack.

    A "skill reference" is `<name>` immediately followed by the word "skill"
    (with optional whitespace/quoting), e.g., ``the `cyw` skill``. This avoids
    flagging every backtick-quoted term while still catching stale companion
    references when a skill is renamed or removed.
    """
    errors: list[str] = []
    try:
        content = filepath.read_text()
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
    return errors


def check_companion_skill_fallbacks(filepath: Path) -> list[str]:
    """Check that companion-skill references include nearby fallback instructions.

    Looks for backtick-quoted skill names (e.g., `cyw` skill, `tdd` skill) and
    verifies that within a 5-line window there is a fallback indicator word.
    Skips files that ARE the companion skill itself (cyw doesn't need a fallback
    for referencing itself).
    """
    errors = []
    try:
        content = filepath.read_text()
    except FileNotFoundError:
        return []

    lines = content.splitlines()

    for skill_name in COMPANION_SKILLS:
        # Don't check the skill's own file
        if filepath.parent.name == skill_name:
            continue

        # Find lines that reference this companion skill as a skill to invoke
        pattern = re.compile(rf"`{skill_name}`\s+skill|the\s+`{skill_name}`\s+skill", re.IGNORECASE)
        for i, line in enumerate(lines):
            if pattern.search(line):
                # Check a window of 5 lines around the reference for fallback language
                window_start = max(0, i - 2)
                window_end = min(len(lines), i + 4)
                window_text = " ".join(lines[window_start:window_end]).lower()

                has_fallback = any(indicator in window_text for indicator in FALLBACK_INDICATORS)
                if not has_fallback:
                    errors.append(
                        f"  {filepath}:{i+1}: companion skill `{skill_name}` referenced "
                        f"without a nearby fallback instruction"
                    )
    return errors


def check_classification(filepath: Path) -> list[str]:
    """Check that Degraded/Runtime-limited skills have a Classification declaration."""
    try:
        content = filepath.read_text()
    except FileNotFoundError:
        return [f"  File not found: {filepath}"]

    if "Classification:" not in content:
        return [f"  {filepath}: missing Classification declaration (required for Degraded/Runtime-limited skills)"]
    return []


def check_companion_files(skills_dir: Path) -> list[str]:
    """Check that plan-duel companion files exist when plan-duel/SKILL.md is present."""
    errors = []
    plan_duel_dir = skills_dir / "plan-duel"
    skill_md = plan_duel_dir / "SKILL.md"

    if not skill_md.exists():
        return []

    for companion in PLAN_DUEL_COMPANIONS:
        companion_path = plan_duel_dir / companion
        if not companion_path.exists():
            errors.append(f"  {companion_path}: missing plan-duel companion file (required when SKILL.md is present)")

    return errors


def check_portability_md(repo_root: Path) -> list[str]:
    """Check that PORTABILITY.md exists with required sections.

    Looks at the repository root, not inside the skills directory.
    """
    portability = repo_root / "PORTABILITY.md"
    errors = []

    if not portability.exists():
        return ["  PORTABILITY.md not found in repository root"]

    content = portability.read_text()
    for section_pattern in REQUIRED_PORTABILITY_SECTIONS:
        pattern = rf"## {section_pattern}"
        if not re.search(pattern, content):
            errors.append(f"  PORTABILITY.md: missing required section matching '## {section_pattern}'")

    return errors


def validate_skills(skills_dir: Path, repo_root: Path) -> list[str]:
    """Run all validations against the skills directory."""
    all_errors = []

    # Check PORTABILITY.md at repo root
    all_errors.extend(check_portability_md(repo_root))

    # Auto-discover skills and classification-required skills
    skill_artifacts = discover_skill_artifacts(skills_dir)
    degraded_or_limited = discover_degraded_or_limited(skills_dir)

    if not skill_artifacts:
        all_errors.append(f"  No skills discovered under {skills_dir}")
        return all_errors

    # Collect skill names (the directories containing a SKILL.md)
    skill_names = sorted({a.split("/")[0] for a in skill_artifacts})

    # Check each skill artifact
    for artifact in skill_artifacts:
        filepath = skills_dir / artifact
        all_errors.extend(check_banned_phrases(filepath))
        all_errors.extend(check_companion_skill_fallbacks(filepath))
        all_errors.extend(check_private_paths(filepath))
        all_errors.extend(check_hardcoded_attribution(filepath))
        all_errors.extend(check_stale_runtime_claims(filepath))
        all_errors.extend(check_workdir_relative_skill_refs(filepath))
        all_errors.extend(check_stale_codex_skill_paths(filepath))
        all_errors.extend(check_cross_skill_references(filepath, skill_names))

    for artifact in DOC_ARTIFACTS:
        filepath = repo_root / artifact
        all_errors.extend(check_private_paths(filepath, DOC_PRIVATE_PATH_PATTERNS))
        all_errors.extend(check_stale_codex_skill_paths(filepath))

    # Check Classification declarations for Degraded/Limited skills
    for artifact in degraded_or_limited:
        filepath = skills_dir / artifact
        all_errors.extend(check_classification(filepath))

    # Check plan-duel companion files
    all_errors.extend(check_companion_files(skills_dir))

    return all_errors


def run_test_fixtures(fixtures_dir: Path) -> list[str]:
    """Validate test fixtures produce expected results."""
    errors = []

    # Test: file with banned phrases should fail
    f = fixtures_dir / "test_validate_banned_phrases.md"
    if f.exists():
        if not check_banned_phrases(f):
            errors.append("  FIXTURE FAIL: test_validate_banned_phrases.md should have been rejected but passed")
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
        if not check_classification(f):
            errors.append("  FIXTURE FAIL: test_validate_missing_classification.md should have been rejected but passed")
    else:
        errors.append(f"  Fixture not found: {f}")

    # Test: known Runtime-limited/Degraded skills still require classification
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_skills = Path(tmpdir)
        plan_duel = tmp_skills / "plan-duel"
        plan_duel.mkdir()
        (plan_duel / "SKILL.md").write_text("# Plan Duel\n")
        flagged = discover_degraded_or_limited(tmp_skills)
        if "plan-duel/SKILL.md" not in flagged:
            errors.append(
                "  FIXTURE FAIL: plan-duel without classification should still be classification-required"
            )

    # Test: companion skill without fallback should fail
    f = fixtures_dir / "test_validate_no_fallback.md"
    if f.exists():
        if not check_companion_skill_fallbacks(f):
            errors.append("  FIXTURE FAIL: test_validate_no_fallback.md should have been rejected but passed")
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
        if not check_private_paths(f):
            errors.append("  FIXTURE FAIL: test_validate_private_paths.md should have been rejected but passed")
    else:
        errors.append(f"  Fixture not found: {f}")

    # Test: hardcoded vendor attribution should fail
    f = fixtures_dir / "test_validate_hardcoded_attribution.md"
    if f.exists():
        if not check_hardcoded_attribution(f):
            errors.append("  FIXTURE FAIL: test_validate_hardcoded_attribution.md should have been rejected but passed")
    else:
        errors.append(f"  Fixture not found: {f}")

    # Test: stale runtime capability claims should fail
    f = fixtures_dir / "test_validate_stale_runtime_claim.md"
    if f.exists():
        if not check_stale_runtime_claims(f):
            errors.append("  FIXTURE FAIL: test_validate_stale_runtime_claim.md should have been rejected but passed")
    else:
        errors.append(f"  Fixture not found: {f}")

    # Test: stale Codex skill path guidance should fail
    f = fixtures_dir / "test_validate_stale_codex_skill_path.md"
    if f.exists():
        if not check_stale_codex_skill_paths(f):
            errors.append("  FIXTURE FAIL: test_validate_stale_codex_skill_path.md should have been rejected but passed")
    else:
        errors.append(f"  Fixture not found: {f}")

    # Test: missing plan-duel companion files should fail
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_skills = Path(tmpdir)
        plan_duel = tmp_skills / "plan-duel"
        plan_duel.mkdir()
        (plan_duel / "SKILL.md").write_text("# Plan Duel\n")
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

    # Test: workdir-relative companion skill reference should fail
    f = fixtures_dir / "test_validate_plan_duel_relative_prompt.md"
    if f.exists():
        if not check_workdir_relative_skill_refs(f):
            errors.append("  FIXTURE FAIL: test_validate_plan_duel_relative_prompt.md should have been rejected but passed")
    else:
        errors.append(f"  Fixture not found: {f}")

    return errors


def main():
    parser = argparse.ArgumentParser(description="Validate cross-platform skill portability")
    parser.add_argument("skills_dir", nargs="?", type=Path, default=None,
                        help="Path to skills directory (default: skills/ relative to repo root)")
    parser.add_argument("--test-fixtures", type=Path, help="Run fixture-based smoke tests")
    args = parser.parse_args()

    repo_root = Path(__file__).parent.parent
    skills_dir = args.skills_dir if args.skills_dir else repo_root / "skills"

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

    errors = validate_skills(skills_dir, repo_root)
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
