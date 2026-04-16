#!/usr/bin/env bash
# Test suite for install.sh
# Runs against temporary directories — never touches real ~/.claude or ~/.codex
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
INSTALLER="$REPO_ROOT/install.sh"
PASS=0
FAIL=0
ERRORS=""
TEMP_SKILL_DIR=""

# All 11 expected skills
SKILLS=(commit cyw extract-hooks plan-and-do plan-duel plan-init plan-phase plan-run security-review-codebase security-review-codebase-hierarchical tdd)

cleanup() {
    if [ -n "${TEMP_SKILL_DIR:-}" ]; then
        rm -rf "$TEMP_SKILL_DIR"
    fi
    rm -rf "$TMPDIR"
}

setup() {
    TMPDIR="$(mktemp -d)"
    export CLAUDE_SKILLS_DIR="$TMPDIR/claude-skills"
    export CODEX_SKILLS_DIR="$TMPDIR/codex-skills"
    trap cleanup EXIT
}

assert() {
    local desc="$1"
    shift
    if "$@" >/dev/null 2>&1; then
        PASS=$((PASS + 1))
        echo "  PASS: $desc"
    else
        FAIL=$((FAIL + 1))
        ERRORS="$ERRORS\n  FAIL: $desc"
        echo "  FAIL: $desc"
    fi
}

assert_not() {
    local desc="$1"
    shift
    if ! "$@" >/dev/null 2>&1; then
        PASS=$((PASS + 1))
        echo "  PASS: $desc"
    else
        FAIL=$((FAIL + 1))
        ERRORS="$ERRORS\n  FAIL: $desc"
        echo "  FAIL: $desc"
    fi
}

assert_fails() {
    local desc="$1"
    shift
    if ! "$@" >/dev/null 2>&1; then
        PASS=$((PASS + 1))
        echo "  PASS: $desc"
    else
        FAIL=$((FAIL + 1))
        ERRORS="$ERRORS\n  FAIL: $desc"
        echo "  FAIL: $desc"
    fi
}

assert_contains() {
    local desc="$1"
    local haystack="$2"
    local needle="$3"
    if [[ "$haystack" == *"$needle"* ]]; then
        PASS=$((PASS + 1))
        echo "  PASS: $desc"
    else
        FAIL=$((FAIL + 1))
        ERRORS="$ERRORS\n  FAIL: $desc"
        echo "  FAIL: $desc"
    fi
}

# --- Setup ---
setup

echo "=== Test: Fresh install ==="
bash "$INSTALLER"

for skill in "${SKILLS[@]}"; do
    assert "claude: $skill/SKILL.md exists" test -f "$CLAUDE_SKILLS_DIR/$skill/SKILL.md"
    assert "codex: $skill/SKILL.md exists" test -f "$CODEX_SKILLS_DIR/$skill/SKILL.md"
done

echo ""
echo "=== Test: plan-duel companion files ==="
assert "claude: plan-duel/init.md exists" test -f "$CLAUDE_SKILLS_DIR/plan-duel/init.md"
assert "claude: plan-duel/round.md exists" test -f "$CLAUDE_SKILLS_DIR/plan-duel/round.md"
assert "claude: plan-duel/summary.md exists" test -f "$CLAUDE_SKILLS_DIR/plan-duel/summary.md"
assert "codex: plan-duel/init.md exists" test -f "$CODEX_SKILLS_DIR/plan-duel/init.md"
assert "codex: plan-duel/round.md exists" test -f "$CODEX_SKILLS_DIR/plan-duel/round.md"
assert "codex: plan-duel/summary.md exists" test -f "$CODEX_SKILLS_DIR/plan-duel/summary.md"

echo ""
echo "=== Test: Manifest files ==="
assert "claude manifest exists" test -f "$CLAUDE_SKILLS_DIR/.installed-by-portable-agent-skills"
assert "codex manifest exists" test -f "$CODEX_SKILLS_DIR/.installed-by-portable-agent-skills"

# Manifest lists all 11 skills
for skill in "${SKILLS[@]}"; do
    assert "manifest lists $skill" grep -q "^${skill}$" "$CLAUDE_SKILLS_DIR/.installed-by-portable-agent-skills"
done

echo ""
echo "=== Test: Idempotency (update) ==="
# Snapshot before
find "$CLAUDE_SKILLS_DIR" -type f | sort > "$TMPDIR/before.txt"
# Run again (update)
bash "$INSTALLER" --update
find "$CLAUDE_SKILLS_DIR" -type f | sort > "$TMPDIR/after.txt"
assert "update is idempotent (same file list)" diff -q "$TMPDIR/before.txt" "$TMPDIR/after.txt"

echo ""
echo "=== Test: Existing unowned skills are protected ==="
PROTECT_ROOT="$TMPDIR/protect"
mkdir -p "$PROTECT_ROOT/claude-skills/commit" "$PROTECT_ROOT/codex-skills/commit"
echo "custom claude" > "$PROTECT_ROOT/claude-skills/commit/SKILL.md"
echo "custom codex" > "$PROTECT_ROOT/codex-skills/commit/SKILL.md"
assert_fails "install refuses to replace unowned same-name skills" env \
    CLAUDE_SKILLS_DIR="$PROTECT_ROOT/claude-skills" \
    CODEX_SKILLS_DIR="$PROTECT_ROOT/codex-skills" \
    bash "$INSTALLER"
assert "unowned claude skill preserved" grep -q "custom claude" "$PROTECT_ROOT/claude-skills/commit/SKILL.md"
assert "unowned codex skill preserved" grep -q "custom codex" "$PROTECT_ROOT/codex-skills/commit/SKILL.md"
assert "force can replace unowned same-name skills" env \
    CLAUDE_SKILLS_DIR="$PROTECT_ROOT/claude-skills" \
    CODEX_SKILLS_DIR="$PROTECT_ROOT/codex-skills" \
    bash "$INSTALLER" --force

PARTIAL_ROOT="$TMPDIR/partial"
mkdir -p "$PARTIAL_ROOT/codex-skills/commit"
echo "custom codex" > "$PARTIAL_ROOT/codex-skills/commit/SKILL.md"
assert_fails "install preflight prevents partial install when codex conflicts" env \
    CLAUDE_SKILLS_DIR="$PARTIAL_ROOT/claude-skills" \
    CODEX_SKILLS_DIR="$PARTIAL_ROOT/codex-skills" \
    bash "$INSTALLER"
assert_not "claude target untouched after codex preflight failure" test -d "$PARTIAL_ROOT/claude-skills"
assert "codex conflicting skill preserved after preflight failure" grep -q "custom codex" "$PARTIAL_ROOT/codex-skills/commit/SKILL.md"

echo ""
echo "=== Test: Uninstall removes only manifest-tracked dirs ==="
# Create a non-package skill directory that should survive
mkdir -p "$CLAUDE_SKILLS_DIR/my-custom-skill"
echo "custom" > "$CLAUDE_SKILLS_DIR/my-custom-skill/SKILL.md"

bash "$INSTALLER" --uninstall

for skill in "${SKILLS[@]}"; do
    assert_not "claude: $skill removed after uninstall" test -d "$CLAUDE_SKILLS_DIR/$skill"
    assert_not "codex: $skill removed after uninstall" test -d "$CODEX_SKILLS_DIR/$skill"
done

assert "custom skill survives uninstall" test -f "$CLAUDE_SKILLS_DIR/my-custom-skill/SKILL.md"
assert_not "claude manifest removed" test -f "$CLAUDE_SKILLS_DIR/.installed-by-portable-agent-skills"
assert_not "codex manifest removed" test -f "$CODEX_SKILLS_DIR/.installed-by-portable-agent-skills"

echo ""
echo "=== Test: Dry-run ==="
# Clean state
rm -rf "$CLAUDE_SKILLS_DIR" "$CODEX_SKILLS_DIR"
OUTPUT="$(bash "$INSTALLER" --dry-run 2>&1)"
assert_not "dry-run does not create claude dir" test -d "$CLAUDE_SKILLS_DIR"
assert_not "dry-run does not create codex dir" test -d "$CODEX_SKILLS_DIR"
assert_contains "dry-run mentions commit skill" "$OUTPUT" "commit"
assert_contains "dry-run mentions plan-duel skill" "$OUTPUT" "plan-duel"

DEFAULT_OUTPUT="$(env -u CLAUDE_SKILLS_DIR -u CODEX_SKILLS_DIR HOME="$TMPDIR/default-home" bash "$INSTALLER" --dry-run 2>&1)"
assert_contains "default codex target is ~/.codex/skills" "$DEFAULT_OUTPUT" "$TMPDIR/default-home/.codex/skills"

echo ""
echo "=== Test: --link creates symlinks ==="
rm -rf "$CLAUDE_SKILLS_DIR" "$CODEX_SKILLS_DIR"
bash "$INSTALLER" --link

assert "claude: commit/ is a dir-level symlink" test -L "$CLAUDE_SKILLS_DIR/commit"
assert "codex: commit/ is a dir-level symlink" test -L "$CODEX_SKILLS_DIR/commit"
assert "claude: plan-duel/init.md resolves through symlink" test -f "$CLAUDE_SKILLS_DIR/plan-duel/init.md"
assert "codex: plan-duel/init.md resolves through symlink" test -f "$CODEX_SKILLS_DIR/plan-duel/init.md"

echo ""
echo "=== Test: Custom env var targets ==="
rm -rf "$CLAUDE_SKILLS_DIR" "$CODEX_SKILLS_DIR"
CUSTOM_CLAUDE="$TMPDIR/custom-claude"
CUSTOM_CODEX="$TMPDIR/custom-codex"
CLAUDE_SKILLS_DIR="$CUSTOM_CLAUDE" CODEX_SKILLS_DIR="$CUSTOM_CODEX" bash "$INSTALLER"
assert "custom claude target used" test -f "$CUSTOM_CLAUDE/commit/SKILL.md"
assert "custom codex target used" test -f "$CUSTOM_CODEX/commit/SKILL.md"

echo ""
echo "=== Test: Manifest version metadata ==="
assert "manifest has source-commit header" grep -q "^# source-commit:" "$CUSTOM_CLAUDE/.installed-by-portable-agent-skills"
assert "manifest has installed-at header" grep -q "^# installed-at:" "$CUSTOM_CLAUDE/.installed-by-portable-agent-skills"

echo ""
echo "=== Test: --verify reports clean install ==="
VERIFY_OUT="$(CLAUDE_SKILLS_DIR="$CUSTOM_CLAUDE" CODEX_SKILLS_DIR="$CUSTOM_CODEX" bash "$INSTALLER" --verify 2>&1)"
assert_contains "verify reports OK for claude" "$VERIFY_OUT" "OK:"
assert_contains "verify reports source-commit" "$VERIFY_OUT" "source-commit"

echo ""
echo "=== Test: --verify flags missing skill ==="
rm -rf "$CUSTOM_CLAUDE/commit"
assert_fails "verify exits nonzero for missing skill" env \
    CLAUDE_SKILLS_DIR="$CUSTOM_CLAUDE" \
    CODEX_SKILLS_DIR="$CUSTOM_CODEX" \
    bash "$INSTALLER" --verify
VERIFY_MISS="$(CLAUDE_SKILLS_DIR="$CUSTOM_CLAUDE" CODEX_SKILLS_DIR="$CUSTOM_CODEX" bash "$INSTALLER" --verify 2>&1 || true)"
assert_contains "verify detects missing skill" "$VERIFY_MISS" "MISSING: commit"

echo ""
echo "=== Test: --verify requires installed manifests ==="
NOINSTALL_ROOT="$TMPDIR/noinstall"
assert_fails "verify exits nonzero when not installed" env \
    CLAUDE_SKILLS_DIR="$NOINSTALL_ROOT/claude" \
    CODEX_SKILLS_DIR="$NOINSTALL_ROOT/codex" \
    bash "$INSTALLER" --verify

echo ""
echo "=== Test: --verify fails on new source skill ==="
rm -rf "$CUSTOM_CLAUDE" "$CUSTOM_CODEX"
CLAUDE_SKILLS_DIR="$CUSTOM_CLAUDE" CODEX_SKILLS_DIR="$CUSTOM_CODEX" bash "$INSTALLER"
TEMP_SKILL_DIR="$REPO_ROOT/skills/zz-temp-installer-test"
mkdir -p "$TEMP_SKILL_DIR"
printf '%s\n' '---' 'name: zz-temp-installer-test' 'description: Temporary installer test skill.' '---' > "$TEMP_SKILL_DIR/SKILL.md"
assert_fails "verify exits nonzero for source skill missing from manifest" env \
    CLAUDE_SKILLS_DIR="$CUSTOM_CLAUDE" \
    CODEX_SKILLS_DIR="$CUSTOM_CODEX" \
    bash "$INSTALLER" --verify
VERIFY_NEW="$(CLAUDE_SKILLS_DIR="$CUSTOM_CLAUDE" CODEX_SKILLS_DIR="$CUSTOM_CODEX" bash "$INSTALLER" --verify 2>&1 || true)"
assert_contains "verify detects new source skill" "$VERIFY_NEW" "NEW SKILL AVAILABLE: zz-temp-installer-test"
rm -rf "$TEMP_SKILL_DIR"
TEMP_SKILL_DIR=""

echo ""
echo "=== Test: --help prints usage ==="
HELP_OUT="$(bash "$INSTALLER" --help 2>&1)"
assert_contains "help mentions --verify" "$HELP_OUT" "--verify"
assert_contains "help mentions --uninstall" "$HELP_OUT" "--uninstall"

echo ""
echo "=== Test: Auto-discovery picks up skills dynamically ==="
# Skills array was populated from skills/*/SKILL.md
assert "auto-discovery found 11 skills" test "${#SKILLS[@]}" -eq 11

echo ""
echo "========================================="
echo "Results: $PASS passed, $FAIL failed"
if [ "$FAIL" -gt 0 ]; then
    echo -e "Failures:$ERRORS"
    exit 1
else
    echo "All installer tests passed."
    exit 0
fi
