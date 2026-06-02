#Requires -Version 5.1
<#
    CI harness for the Windows PowerShell installer.

    Installs the test dependencies (Pester 5.x, PSScriptAnalyzer), runs static
    analysis, then runs the Pester suite. Throws (nonzero exit) on any failure.

    Invoked once per interpreter by the installer-windows CI job, so the same
    checks run under both Windows PowerShell 5.1 (powershell.exe) and
    PowerShell 7+ (pwsh). Run it locally the same way, e.g.:
        pwsh -NoProfile -ExecutionPolicy Bypass -File tests/ci-windows.ps1
#>

$ErrorActionPreference = 'Stop'
$RepoRoot = Split-Path -Parent $PSScriptRoot
Set-Location $RepoRoot

Write-Host "== PowerShell $($PSVersionTable.PSEdition) $($PSVersionTable.PSVersion) =="

# --- Install test dependencies ---
# TLS 1.2 is required to reach PSGallery from Windows PowerShell 5.1.
[Net.ServicePointManager]::SecurityProtocol =
    [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12
if (-not (Get-PackageProvider -Name NuGet -ErrorAction SilentlyContinue)) {
    Install-PackageProvider -Name NuGet -Force -Scope CurrentUser | Out-Null
}
Set-PSRepository -Name PSGallery -InstallationPolicy Trusted
# 5.1 ships Pester 3.4; force a 5.x side-by-side install.
Install-Module Pester -MinimumVersion 5.5.0 -Force -SkipPublisherCheck -Scope CurrentUser
Install-Module PSScriptAnalyzer -Force -Scope CurrentUser

# --- Static analysis ---
Write-Host '== PSScriptAnalyzer =='
Import-Module PSScriptAnalyzer
$findings = @()
foreach ($f in 'install.ps1', 'tests/install.Tests.ps1') {
    $findings += Invoke-ScriptAnalyzer -Path $f -Settings ./PSScriptAnalyzerSettings.psd1
}
if ($findings) {
    $findings | Format-Table Severity, RuleName, ScriptName, Line, Message -AutoSize -Wrap
    throw "PSScriptAnalyzer reported $($findings.Count) finding(s)."
}
Write-Host 'PSScriptAnalyzer: clean.'

# --- Pester suite ---
Write-Host '== Pester =='
Import-Module Pester -MinimumVersion 5.0
$cfg = New-PesterConfiguration
$cfg.Run.Path = 'tests/install.Tests.ps1'
$cfg.Run.PassThru = $true
$cfg.Output.Verbosity = 'Detailed'
$res = Invoke-Pester -Configuration $cfg
if ($res.FailedCount -gt 0) {
    throw "$($res.FailedCount) Pester test(s) failed."
}
Write-Host 'All Windows installer checks passed.'
