#!/usr/bin/env bash
# Portable Agent Skills — Installer
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")" && pwd)"
SKILLS_SRC="$REPO_ROOT/skills"

CLAUDE_TARGET="${CLAUDE_SKILLS_DIR:-$HOME/.claude/skills}"
CODEX_TARGET="${CODEX_SKILLS_DIR:-$HOME/.codex/skills}"

MANIFEST_NAME=".installed-by-portable-agent-skills"

usage() {
    cat <<'EOF'
Portable Agent Skills — Installer

Usage:
  ./install.sh              Install (copy) skills to Claude Code and Codex directories
  ./install.sh --update     Update existing installation (same as install, idempotent)
  ./install.sh --uninstall  Remove only skills installed by this pack
  ./install.sh --verify     Check installed skills against source and manifest
  ./install.sh --dry-run    Print what would happen without making changes
  ./install.sh --link       Symlink instead of copy (for development workflows)
  ./install.sh --force      Replace existing same-name skills even if unowned
  ./install.sh --help       Show this message and exit

Environment variables:
  CLAUDE_SKILLS_DIR  Override Claude Code skills directory (default: ~/.claude/skills)
  CODEX_SKILLS_DIR   Override Codex skills directory (default: ~/.codex/skills)
EOF
}

# --- Discover skills from source tree ---
# Any directory under skills/ that contains a SKILL.md is a skill.
discover_skills() {
    local skills=()
    local entry
    for entry in "$SKILLS_SRC"/*/SKILL.md; do
        [ -f "$entry" ] || continue
        skills+=("$(basename "$(dirname "$entry")")")
    done
    printf '%s\n' "${skills[@]}" | sort
}

SKILLS=()
while IFS= read -r line; do
    [ -n "$line" ] && SKILLS+=("$line")
done < <(discover_skills)

if [ "${#SKILLS[@]}" -eq 0 ]; then
    echo "Error: no skills discovered under $SKILLS_SRC" >&2
    exit 1
fi

# --- Version info ---
source_commit() {
    if command -v git >/dev/null 2>&1 && [ -d "$REPO_ROOT/.git" ]; then
        git -C "$REPO_ROOT" rev-parse HEAD 2>/dev/null || echo "unknown"
    else
        echo "unknown"
    fi
}

source_version() {
    if command -v git >/dev/null 2>&1 && [ -d "$REPO_ROOT/.git" ]; then
        git -C "$REPO_ROOT" describe --tags --always --dirty 2>/dev/null || echo "unknown"
    else
        echo "unknown"
    fi
}

write_manifest() {
    local target="$1"
    {
        echo "# Portable Agent Skills manifest"
        echo "# installed-at: $(date -u +%Y-%m-%dT%H:%M:%SZ)"
        echo "# source-commit: $(source_commit)"
        echo "# source-version: $(source_version)"
        printf '%s\n' "${SKILLS[@]}"
    } > "$target/$MANIFEST_NAME"
}

# Read manifest skill names (strips comment lines)
read_manifest_skills() {
    local manifest="$1"
    [ -f "$manifest" ] || return
    grep -v '^#' "$manifest" | grep -v '^[[:space:]]*$'
}

MODE="install"
DRY_RUN=false
USE_LINK=false
FORCE=false

# Parse arguments
for arg in "$@"; do
    case "$arg" in
        --help|-h)   usage; exit 0 ;;
        --update)    MODE="install" ;;
        --uninstall) MODE="uninstall" ;;
        --verify)    MODE="verify" ;;
        --dry-run)   DRY_RUN=true ;;
        --link)      USE_LINK=true ;;
        --force)     FORCE=true ;;
        *)
            echo "Unknown option: $arg" >&2
            usage >&2
            exit 1
            ;;
    esac
done

manifest_owns_skill() {
    local target="$1"
    local skill="$2"
    local manifest="$target/$MANIFEST_NAME"

    [ -f "$manifest" ] && read_manifest_skills "$manifest" | grep -Fxq "$skill"
}

preflight_target() {
    local target="$1"
    local target_name="$2"

    if $DRY_RUN || $FORCE; then
        return
    fi

    for skill in "${SKILLS[@]}"; do
        local dst="$target/$skill"
        if [ -e "$dst" ] || [ -L "$dst" ]; then
            if ! manifest_owns_skill "$target" "$skill"; then
                echo "Refusing to replace existing unowned skill: $dst ($target_name)" >&2
                echo "Move it aside, uninstall it manually, or rerun with --force to replace it." >&2
                exit 1
            fi
        fi
    done
}

install_to_target() {
    local target="$1"
    local target_name="$2"

    if $DRY_RUN; then
        echo "[dry-run] Would install to $target:"
        for skill in "${SKILLS[@]}"; do
            if $USE_LINK; then
                echo "  symlink $skill/"
            else
                echo "  copy $skill/"
            fi
        done
        echo "  write $MANIFEST_NAME"
        return
    fi

    mkdir -p "$target"

    for skill in "${SKILLS[@]}"; do
        local src="$SKILLS_SRC/$skill"
        local dst="$target/$skill"

        if [ -e "$dst" ] || [ -L "$dst" ]; then
            rm -rf "$dst"
        fi

        if $USE_LINK; then
            ln -sfn "$src" "$dst"
        else
            cp -r "$src" "$dst"
        fi
    done

    write_manifest "$target"

    echo "Installed ${#SKILLS[@]} skills to $target ($target_name)"
}

uninstall_from_target() {
    local target="$1"
    local target_name="$2"
    local manifest="$target/$MANIFEST_NAME"

    if [ ! -f "$manifest" ]; then
        if $DRY_RUN; then
            echo "[dry-run] No manifest at $target — nothing to uninstall ($target_name)"
        else
            echo "No manifest at $target — nothing to uninstall ($target_name)"
        fi
        return
    fi

    if $DRY_RUN; then
        echo "[dry-run] Would uninstall from $target ($target_name):"
        while IFS= read -r skill; do
            echo "  remove $skill/"
        done < <(read_manifest_skills "$manifest")
        echo "  remove $MANIFEST_NAME"
        return
    fi

    while IFS= read -r skill; do
        local dst="$target/$skill"
        if [ -e "$dst" ] || [ -L "$dst" ]; then
            rm -rf "$dst"
        fi
    done < <(read_manifest_skills "$manifest")

    rm -f "$manifest"
    echo "Uninstalled skills from $target ($target_name)"
}

verify_target() {
    local target="$1"
    local target_name="$2"
    local manifest="$target/$MANIFEST_NAME"
    local issues=0

    if [ ! -f "$manifest" ]; then
        echo "[$target_name] No manifest at $target — not installed by this pack."
        return 1
    fi

    echo "[$target_name] $target"
    # Print manifest metadata
    grep '^#' "$manifest" | sed 's/^/  /'

    local installed_commit
    installed_commit="$(grep '^# source-commit:' "$manifest" | awk '{print $3}' || echo "")"
    local current_commit
    current_commit="$(source_commit)"
    if [ -n "$installed_commit" ] && [ "$installed_commit" != "unknown" ] && \
       [ "$current_commit" != "unknown" ] && [ "$installed_commit" != "$current_commit" ]; then
        echo "  NOTE: installed commit ($installed_commit) differs from source ($current_commit) — run ./install.sh --update to refresh."
    fi

    # Check each manifest-listed skill exists at target
    while IFS= read -r skill; do
        local dst="$target/$skill"
        if [ ! -e "$dst" ] && [ ! -L "$dst" ]; then
            echo "  MISSING: $skill (listed in manifest, not present at target)"
            issues=$((issues + 1))
            continue
        fi
        # Check for broken symlinks (common in --link mode when source moves)
        if [ -L "$dst" ] && [ ! -e "$dst" ]; then
            echo "  BROKEN SYMLINK: $skill -> $(readlink "$dst")"
            issues=$((issues + 1))
            continue
        fi
        if [ -d "$dst" ] && [ ! -f "$dst/SKILL.md" ]; then
            echo "  INCOMPLETE: $skill has no SKILL.md"
            issues=$((issues + 1))
        fi
    done < <(read_manifest_skills "$manifest")

    # Check for skills in source that are not in manifest (stale install, new skills available)
    for skill in "${SKILLS[@]}"; do
        if ! read_manifest_skills "$manifest" | grep -Fxq "$skill"; then
            echo "  NEW SKILL AVAILABLE: $skill (in source, not yet installed — run ./install.sh --update)"
            issues=$((issues + 1))
        fi
    done

    if [ "$issues" -eq 0 ]; then
        echo "  OK: ${#SKILLS[@]} skills verified."
    else
        echo "  $issues issue(s) found."
        return 1
    fi
}

case "$MODE" in
    install)
        preflight_target "$CLAUDE_TARGET" "Claude Code"
        preflight_target "$CODEX_TARGET" "Codex"
        install_to_target "$CLAUDE_TARGET" "Claude Code"
        install_to_target "$CODEX_TARGET" "Codex"
        ;;
    uninstall)
        uninstall_from_target "$CLAUDE_TARGET" "Claude Code"
        uninstall_from_target "$CODEX_TARGET" "Codex"
        ;;
    verify)
        verify_ok=true
        verify_target "$CLAUDE_TARGET" "Claude Code" || verify_ok=false
        verify_target "$CODEX_TARGET" "Codex" || verify_ok=false
        $verify_ok || exit 1
        ;;
esac
