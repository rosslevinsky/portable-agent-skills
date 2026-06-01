<#
    Pester tests for install.ps1 (the Windows / PowerShell installer).

    Mirrors the coverage of tests/test_installer.sh. Runs against throwaway
    temp directories - never touches a real ~/.claude or ~/.codex.

    Run:  Invoke-Pester tests/install.Tests.ps1
    Requires: Pester 5.x. Works under Windows PowerShell 5.1 and PowerShell 7+.
#>

# Defined at top level (not in BeforeAll) so the value is available during
# Pester's discovery phase, which is when -ForEach test cases are expanded.
$ExpectedSkills = @(
    'commit', 'cyw', 'extract-hooks', 'plan-and-do', 'plan-duel', 'plan-init',
    'plan-phase', 'plan-run', 'security-review-codebase',
    'security-review-codebase-hierarchical', 'tdd'
)

BeforeAll {
    $script:RepoRoot  = Split-Path -Parent $PSScriptRoot
    $script:Installer = Join-Path $RepoRoot 'install.ps1'
    # The executable hosting this test run (powershell.exe on 5.1, pwsh on 7+).
    $script:PwshExe   = [System.Diagnostics.Process]::GetCurrentProcess().MainModule.FileName
    $script:ManifestName = '.installed-by-portable-agent-skills'

    # Re-declared here for the run phase: the top-level $ExpectedSkills above is
    # only in scope during discovery, so run-time bodies need their own binding.
    $script:ExpectedSkills = @(
        'commit', 'cyw', 'extract-hooks', 'plan-and-do', 'plan-duel', 'plan-init',
        'plan-phase', 'plan-run', 'security-review-codebase',
        'security-review-codebase-hierarchical', 'tdd'
    )

    $script:Sandboxes = New-Object System.Collections.ArrayList

    function New-Sandbox {
        $base = Join-Path ([System.IO.Path]::GetTempPath()) ('pas-' + [System.Guid]::NewGuid().ToString('N'))
        [void]$script:Sandboxes.Add($base)
        [pscustomobject]@{
            Base     = $base
            Claude   = Join-Path $base 'claude-skills'
            Codex    = Join-Path $base 'codex-skills'
            Manifest = { param($dir) Join-Path $dir $script:ManifestName }
        }
    }

    # Run install.ps1 in a fresh child process so its `exit` calls don't kill
    # the test session, and so $LASTEXITCODE reflects the script's real code.
    function Invoke-Installer {
        param([string[]]$Arguments = @(), [string]$ClaudeDir, [string]$CodexDir)
        # Windows PowerShell 5.1 turns a child process's stderr (captured via
        # 2>&1) into a terminating NativeCommandError when ErrorActionPreference
        # is 'Stop'. The installer writes its refusal message to stderr, so relax
        # the preference here to capture that text instead of throwing.
        $ErrorActionPreference = 'Continue'
        $env:CLAUDE_SKILLS_DIR = $ClaudeDir
        $env:CODEX_SKILLS_DIR  = $CodexDir
        try {
            $output = & $script:PwshExe -NoProfile -File $script:Installer @Arguments 2>&1
            $code = $LASTEXITCODE
        } finally {
            Remove-Item Env:CLAUDE_SKILLS_DIR, Env:CODEX_SKILLS_DIR -ErrorAction SilentlyContinue
        }
        [pscustomobject]@{ ExitCode = $code; Output = ($output | Out-String) }
    }
}

AfterAll {
    foreach ($base in $script:Sandboxes) {
        Remove-Item -LiteralPath $base -Recurse -Force -ErrorAction SilentlyContinue
    }
}

Describe 'install (default copy mode)' {
    BeforeAll {
        $script:sb = New-Sandbox
        $script:result = Invoke-Installer -ClaudeDir $sb.Claude -CodexDir $sb.Codex
    }

    It 'exits 0' {
        $result.ExitCode | Should -Be 0
    }

    It 'reports the installed skill count' {
        $result.Output | Should -Match "Installed $($ExpectedSkills.Count) skills"
    }

    It 'installs <_> into the Claude target' -ForEach $ExpectedSkills {
        Test-Path -LiteralPath (Join-Path $sb.Claude (Join-Path $_ 'SKILL.md')) | Should -BeTrue
    }

    It 'installs <_> into the Codex target' -ForEach $ExpectedSkills {
        Test-Path -LiteralPath (Join-Path $sb.Codex (Join-Path $_ 'SKILL.md')) | Should -BeTrue
    }

    It 'writes a manifest to both targets' {
        Test-Path -LiteralPath (Join-Path $sb.Claude $ManifestName) | Should -BeTrue
        Test-Path -LiteralPath (Join-Path $sb.Codex  $ManifestName) | Should -BeTrue
    }

    It 'lists every skill in the manifest' {
        $listed = Get-Content -LiteralPath (Join-Path $sb.Claude $ManifestName) |
            Where-Object { $_ -notmatch '^\s*#' -and $_ -notmatch '^\s*$' }
        $listed | Should -Be $ExpectedSkills
    }

    It 'includes required manifest headers' {
        $text = Get-Content -LiteralPath (Join-Path $sb.Claude $ManifestName) -Raw
        $text | Should -Match '# Portable Agent Skills manifest'
        $text | Should -Match '# installed-at: \d{4}-\d{2}-\d{2}T'
        $text | Should -Match '# source-commit:'
        $text | Should -Match '# source-version:'
    }

    It 'writes the manifest with LF line endings (no CRLF)' {
        $bytes = [System.IO.File]::ReadAllBytes((Join-Path $sb.Claude $ManifestName))
        # 13 = CR. install.sh manifests are LF-only; the two must stay byte-compatible.
        ($bytes -contains [byte]13) | Should -BeFalse
    }
}

Describe 'install is idempotent (-Update)' {
    It 're-running leaves exactly one copy of each skill and exits 0' {
        $sb = New-Sandbox
        (Invoke-Installer -ClaudeDir $sb.Claude -CodexDir $sb.Codex).ExitCode | Should -Be 0
        $second = Invoke-Installer -Arguments @('-Update') -ClaudeDir $sb.Claude -CodexDir $sb.Codex
        $second.ExitCode | Should -Be 0
        (Get-ChildItem -LiteralPath $sb.Claude -Directory).Count | Should -Be $ExpectedSkills.Count
    }
}

Describe 'verify' {
    It 'exits 0 on a clean install and reports source-commit' {
        $sb = New-Sandbox
        Invoke-Installer -ClaudeDir $sb.Claude -CodexDir $sb.Codex | Out-Null
        $v = Invoke-Installer -Arguments @('-Verify') -ClaudeDir $sb.Claude -CodexDir $sb.Codex
        $v.ExitCode | Should -Be 0
        $v.Output | Should -Match 'source-commit'
    }

    It 'exits 1 when no manifest is present' {
        $sb = New-Sandbox
        $v = Invoke-Installer -Arguments @('-Verify') -ClaudeDir $sb.Claude -CodexDir $sb.Codex
        $v.ExitCode | Should -Be 1
        $v.Output | Should -Match 'not installed by this pack'
    }

    It 'exits 1 and flags a NEW SKILL when the manifest is missing one' {
        $sb = New-Sandbox
        Invoke-Installer -ClaudeDir $sb.Claude -CodexDir $sb.Codex | Out-Null
        $manifest = Join-Path $sb.Claude $ManifestName
        (Get-Content -LiteralPath $manifest | Where-Object { $_ -ne 'cyw' }) |
            Set-Content -LiteralPath $manifest
        $v = Invoke-Installer -Arguments @('-Verify') -ClaudeDir $sb.Claude -CodexDir $sb.Codex
        $v.ExitCode | Should -Be 1
        $v.Output | Should -Match 'NEW SKILL AVAILABLE: cyw'
    }

    It 'exits 1 and flags MISSING when an installed skill dir is gone' {
        $sb = New-Sandbox
        Invoke-Installer -ClaudeDir $sb.Claude -CodexDir $sb.Codex | Out-Null
        Remove-Item -LiteralPath (Join-Path $sb.Claude 'cyw') -Recurse -Force
        $v = Invoke-Installer -Arguments @('-Verify') -ClaudeDir $sb.Claude -CodexDir $sb.Codex
        $v.ExitCode | Should -Be 1
        $v.Output | Should -Match 'MISSING: cyw'
    }
}

Describe 'uninstall' {
    It 'removes tracked skills and the manifest, and exits 0' {
        $sb = New-Sandbox
        Invoke-Installer -ClaudeDir $sb.Claude -CodexDir $sb.Codex | Out-Null
        $u = Invoke-Installer -Arguments @('-Uninstall') -ClaudeDir $sb.Claude -CodexDir $sb.Codex
        $u.ExitCode | Should -Be 0
        Test-Path -LiteralPath (Join-Path $sb.Claude $ManifestName) | Should -BeFalse
        Test-Path -LiteralPath (Join-Path $sb.Claude 'commit')      | Should -BeFalse
    }

    It 'leaves an unowned same-name directory untouched' {
        $sb = New-Sandbox
        Invoke-Installer -ClaudeDir $sb.Claude -CodexDir $sb.Codex | Out-Null
        # Drop an unowned skill not present in the manifest.
        $mine = Join-Path $sb.Claude 'my-own-skill'
        New-Item -ItemType Directory -Path $mine -Force | Out-Null
        Set-Content -LiteralPath (Join-Path $mine 'SKILL.md') -Value 'mine'
        Invoke-Installer -Arguments @('-Uninstall') -ClaudeDir $sb.Claude -CodexDir $sb.Codex | Out-Null
        Test-Path -LiteralPath (Join-Path $mine 'SKILL.md') | Should -BeTrue
    }

    It 'is a no-op when there is no manifest' {
        $sb = New-Sandbox
        $u = Invoke-Installer -Arguments @('-Uninstall') -ClaudeDir $sb.Claude -CodexDir $sb.Codex
        $u.ExitCode | Should -Be 0
        $u.Output | Should -Match 'nothing to uninstall'
    }
}

Describe 'ownership preflight' {
    It 'refuses to overwrite an unowned existing skill (exit 1)' {
        $sb = New-Sandbox
        $clash = Join-Path $sb.Claude 'commit'
        New-Item -ItemType Directory -Path $clash -Force | Out-Null
        Set-Content -LiteralPath (Join-Path $clash 'SKILL.md') -Value 'mine'
        $r = Invoke-Installer -ClaudeDir $sb.Claude -CodexDir $sb.Codex
        $r.ExitCode | Should -Be 1
        $r.Output | Should -Match 'Refusing to replace existing unowned skill'
    }

    It '-Force replaces an unowned existing skill (exit 0)' {
        $sb = New-Sandbox
        $clash = Join-Path $sb.Claude 'commit'
        New-Item -ItemType Directory -Path $clash -Force | Out-Null
        Set-Content -LiteralPath (Join-Path $clash 'SKILL.md') -Value 'mine'
        $r = Invoke-Installer -Arguments @('-Force') -ClaudeDir $sb.Claude -CodexDir $sb.Codex
        $r.ExitCode | Should -Be 0
        # The pack's real commit skill should now be in place (not the 'mine' stub).
        (Get-Content -LiteralPath (Join-Path $clash 'SKILL.md') -Raw) | Should -Not -Match '^mine'
    }
}

Describe 'dry-run' {
    It 'makes no changes and exits 0' {
        $sb = New-Sandbox
        $r = Invoke-Installer -Arguments @('-DryRun') -ClaudeDir $sb.Claude -CodexDir $sb.Codex
        $r.ExitCode | Should -Be 0
        $r.Output | Should -Match '\[dry-run\]'
        Test-Path -LiteralPath $sb.Claude | Should -BeFalse
        Test-Path -LiteralPath $sb.Codex  | Should -BeFalse
    }
}

Describe 'help' {
    It '-Help prints usage and exits 0' {
        $sb = New-Sandbox
        $r = Invoke-Installer -Arguments @('-Help') -ClaudeDir $sb.Claude -CodexDir $sb.Codex
        $r.ExitCode | Should -Be 0
        $r.Output | Should -Match '-Verify'
        $r.Output | Should -Match '-Uninstall'
    }
}
