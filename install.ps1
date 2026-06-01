<#
.SYNOPSIS
    Portable Agent Skills - Installer (Windows / PowerShell)

.DESCRIPTION
    Native PowerShell port of install.sh for Windows users running Claude Code
    and/or Codex CLI outside WSL. Copies each skill under skills/ into the
    Claude Code and Codex user skills directories and writes an ownership
    manifest compatible with install.sh.

    Copy-only: this installer intentionally does not implement a --link mode.
    For an editable dev workflow, use ./install.sh --link under WSL or Git Bash.

.PARAMETER Update
    Update an existing installation (same as install; idempotent).

.PARAMETER Uninstall
    Remove only skills installed by this pack (tracked via the manifest).

.PARAMETER Verify
    Check installed skills against source and manifest.

.PARAMETER DryRun
    Print what would happen without making changes.

.PARAMETER Force
    Replace existing same-name skills even if not owned by this pack.

.PARAMETER Help
    Show usage and exit.

.NOTES
    Environment variables:
      CLAUDE_SKILLS_DIR  Override Claude Code skills directory (default: $HOME\.claude\skills)
      CODEX_SKILLS_DIR   Override Codex skills directory (default: $HOME\.codex\skills)

.EXAMPLE
    .\install.ps1
    .\install.ps1 -Update
    .\install.ps1 -Uninstall
    .\install.ps1 -Verify
    .\install.ps1 -DryRun
#>

[CmdletBinding()]
param(
    [switch]$Update,
    [switch]$Uninstall,
    [switch]$Verify,
    [switch]$DryRun,
    [switch]$Force,
    [switch]$Help
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$RepoRoot   = $PSScriptRoot
$SkillsSrc  = Join-Path $RepoRoot 'skills'
$ManifestName = '.installed-by-portable-agent-skills'

# Match install.sh exactly: honor CLAUDE_SKILLS_DIR / CODEX_SKILLS_DIR overrides,
# otherwise default to the per-user dirs under $HOME.
$ClaudeTarget = if ($env:CLAUDE_SKILLS_DIR) { $env:CLAUDE_SKILLS_DIR } else { Join-Path $HOME '.claude\skills' }
$CodexTarget  = if ($env:CODEX_SKILLS_DIR)  { $env:CODEX_SKILLS_DIR }  else { Join-Path $HOME '.codex\skills' }

function Show-Usage {
    @'
Portable Agent Skills - Installer (Windows / PowerShell)

Usage:
  .\install.ps1              Install (copy) skills to Claude Code and Codex directories
  .\install.ps1 -Update     Update existing installation (same as install, idempotent)
  .\install.ps1 -Uninstall  Remove only skills installed by this pack
  .\install.ps1 -Verify     Check installed skills against source and manifest
  .\install.ps1 -DryRun     Print what would happen without making changes
  .\install.ps1 -Force      Replace existing same-name skills even if unowned
  .\install.ps1 -Help       Show this message and exit

Environment variables:
  CLAUDE_SKILLS_DIR  Override Claude Code skills directory (default: $HOME\.claude\skills)
  CODEX_SKILLS_DIR   Override Codex skills directory (default: $HOME\.codex\skills)

Note: this installer is copy-only. For an editable (symlink) dev workflow,
use ./install.sh --link under WSL or Git Bash.
'@ | Write-Host
}

if ($Help) { Show-Usage; exit 0 }

# --- Discover skills from source tree ---
# Any directory under skills/ that contains a SKILL.md is a skill.
function Get-Skill {
    if (-not (Test-Path -LiteralPath $SkillsSrc)) { return @() }
    Get-ChildItem -LiteralPath $SkillsSrc -Directory |
        Where-Object { Test-Path -LiteralPath (Join-Path $_.FullName 'SKILL.md') } |
        Select-Object -ExpandProperty Name |
        Sort-Object
}

$Skills = @(Get-Skill)
if ($Skills.Count -eq 0) {
    [Console]::Error.WriteLine("Error: no skills discovered under $SkillsSrc")
    exit 1
}

# --- Version info ---
function Get-SourceCommit {
    if ((Get-Command git -ErrorAction SilentlyContinue) -and (Test-Path -LiteralPath (Join-Path $RepoRoot '.git'))) {
        $c = git -C $RepoRoot rev-parse HEAD 2>$null
        if ($LASTEXITCODE -eq 0 -and $c) { return $c.Trim() }
    }
    return 'unknown'
}

function Get-SourceVersion {
    if ((Get-Command git -ErrorAction SilentlyContinue) -and (Test-Path -LiteralPath (Join-Path $RepoRoot '.git'))) {
        $v = git -C $RepoRoot describe --tags --always --dirty 2>$null
        if ($LASTEXITCODE -eq 0 -and $v) { return $v.Trim() }
    }
    return 'unknown'
}

function Write-Manifest {
    param([string]$Target)
    $installedAt = [DateTime]::UtcNow.ToString('yyyy-MM-ddTHH:mm:ssZ')
    $lines = @(
        '# Portable Agent Skills manifest'
        "# installed-at: $installedAt"
        "# source-commit: $(Get-SourceCommit)"
        "# source-version: $(Get-SourceVersion)"
    ) + $Skills
    # Write LF-terminated lines to stay byte-compatible with install.sh manifests.
    $content = ($lines -join "`n") + "`n"
    [System.IO.File]::WriteAllText((Join-Path $Target $ManifestName), $content, [System.Text.UTF8Encoding]::new($false))
}

# Read manifest skill names (strips comment and blank lines)
function Read-ManifestSkill {
    param([string]$Manifest)
    if (-not (Test-Path -LiteralPath $Manifest)) { return @() }
    Get-Content -LiteralPath $Manifest |
        Where-Object { $_ -notmatch '^\s*#' -and $_ -notmatch '^\s*$' } |
        ForEach-Object { $_.Trim() }
}

function Test-ManifestOwnsSkill {
    param([string]$Target, [string]$Skill)
    $manifest = Join-Path $Target $ManifestName
    (Read-ManifestSkill $manifest) -contains $Skill
}

# Determine mode (mutually exclusive; install/update is the default).
# -Update is an alias for install (the copy is idempotent), so it needs no
# distinct mode - it just makes intent explicit on the command line.
$Mode = 'install'
if ($Uninstall) { $Mode = 'uninstall' }
elseif ($Verify) { $Mode = 'verify' }
elseif ($Update) { Write-Verbose 'Update requested: reinstalling skills (idempotent).' }

function Invoke-Preflight {
    param([string]$Target, [string]$TargetName)
    if ($DryRun -or $Force) { return }
    foreach ($skill in $Skills) {
        $dst = Join-Path $Target $skill
        if (Test-Path -LiteralPath $dst) {
            if (-not (Test-ManifestOwnsSkill $Target $skill)) {
                [Console]::Error.WriteLine("Refusing to replace existing unowned skill: $dst ($TargetName)")
                [Console]::Error.WriteLine("Move it aside, uninstall it manually, or rerun with -Force to replace it.")
                exit 1
            }
        }
    }
}

function Install-ToTarget {
    param([string]$Target, [string]$TargetName)

    if ($DryRun) {
        Write-Host "[dry-run] Would install to ${Target}:"
        foreach ($skill in $Skills) { Write-Host "  copy $skill/" }
        Write-Host "  write $ManifestName"
        return
    }

    if (-not (Test-Path -LiteralPath $Target)) {
        New-Item -ItemType Directory -Path $Target -Force | Out-Null
    }

    foreach ($skill in $Skills) {
        $src = Join-Path $SkillsSrc $skill
        $dst = Join-Path $Target $skill
        if (Test-Path -LiteralPath $dst) {
            Remove-Item -LiteralPath $dst -Recurse -Force
        }
        Copy-Item -LiteralPath $src -Destination $dst -Recurse -Force
    }

    Write-Manifest $Target
    Write-Host "Installed $($Skills.Count) skills to $Target ($TargetName)"
}

function Uninstall-FromTarget {
    param([string]$Target, [string]$TargetName)
    $manifest = Join-Path $Target $ManifestName

    if (-not (Test-Path -LiteralPath $manifest)) {
        if ($DryRun) {
            Write-Host "[dry-run] No manifest at $Target - nothing to uninstall ($TargetName)"
        } else {
            Write-Host "No manifest at $Target - nothing to uninstall ($TargetName)"
        }
        return
    }

    if ($DryRun) {
        Write-Host "[dry-run] Would uninstall from $Target ($TargetName):"
        foreach ($skill in (Read-ManifestSkill $manifest)) { Write-Host "  remove $skill/" }
        Write-Host "  remove $ManifestName"
        return
    }

    foreach ($skill in (Read-ManifestSkill $manifest)) {
        $dst = Join-Path $Target $skill
        if (Test-Path -LiteralPath $dst) {
            Remove-Item -LiteralPath $dst -Recurse -Force
        }
    }

    Remove-Item -LiteralPath $manifest -Force
    Write-Host "Uninstalled skills from $Target ($TargetName)"
}

function Test-Target {
    param([string]$Target, [string]$TargetName)
    $manifest = Join-Path $Target $ManifestName
    $issues = 0

    if (-not (Test-Path -LiteralPath $manifest)) {
        Write-Host "[$TargetName] No manifest at $Target - not installed by this pack."
        return $false
    }

    Write-Host "[$TargetName] $Target"
    # Print manifest metadata (comment lines)
    Get-Content -LiteralPath $manifest | Where-Object { $_ -match '^\s*#' } | ForEach-Object { Write-Host "  $_" }

    $installedCommit = (Get-Content -LiteralPath $manifest |
        Where-Object { $_ -match '^# source-commit:' } |
        ForEach-Object { ($_ -split '\s+')[2] }) | Select-Object -First 1
    $currentCommit = Get-SourceCommit
    if ($installedCommit -and $installedCommit -ne 'unknown' -and
        $currentCommit -ne 'unknown' -and $installedCommit -ne $currentCommit) {
        Write-Host "  NOTE: installed commit ($installedCommit) differs from source ($currentCommit) - run .\install.ps1 -Update to refresh."
    }

    $manifestSkills = @(Read-ManifestSkill $manifest)

    # Check each manifest-listed skill exists at target
    foreach ($skill in $manifestSkills) {
        $dst = Join-Path $Target $skill
        if (-not (Test-Path -LiteralPath $dst)) {
            Write-Host "  MISSING: $skill (listed in manifest, not present at target)"
            $issues++
            continue
        }
        if ((Test-Path -LiteralPath $dst -PathType Container) -and
            -not (Test-Path -LiteralPath (Join-Path $dst 'SKILL.md'))) {
            Write-Host "  INCOMPLETE: $skill has no SKILL.md"
            $issues++
        }
    }

    # Check for skills in source that are not in manifest
    foreach ($skill in $Skills) {
        if ($manifestSkills -notcontains $skill) {
            Write-Host "  NEW SKILL AVAILABLE: $skill (in source, not yet installed - run .\install.ps1 -Update)"
            $issues++
        }
    }

    if ($issues -eq 0) {
        Write-Host "  OK: $($Skills.Count) skills verified."
        return $true
    } else {
        Write-Host "  $issues issue(s) found."
        return $false
    }
}

switch ($Mode) {
    'install' {
        Invoke-Preflight $ClaudeTarget 'Claude Code'
        Invoke-Preflight $CodexTarget  'Codex'
        Install-ToTarget $ClaudeTarget 'Claude Code'
        Install-ToTarget $CodexTarget  'Codex'
    }
    'uninstall' {
        Uninstall-FromTarget $ClaudeTarget 'Claude Code'
        Uninstall-FromTarget $CodexTarget  'Codex'
    }
    'verify' {
        $ok = $true
        if (-not (Test-Target $ClaudeTarget 'Claude Code')) { $ok = $false }
        if (-not (Test-Target $CodexTarget  'Codex'))       { $ok = $false }
        if (-not $ok) { exit 1 }
    }
}

# Explicit success exit so $LASTEXITCODE is reliable for callers (PowerShell does
# not reset it when a script falls off the end).
exit 0
