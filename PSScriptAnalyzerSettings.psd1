@{
    # PSScriptAnalyzer policy for this repo's PowerShell (install.ps1 + Pester tests).
    # Run:  Invoke-ScriptAnalyzer -Path install.ps1 -Settings PSScriptAnalyzerSettings.psd1
    Severity    = @('Error', 'Warning', 'Information')

    ExcludeRules = @(
        # install.ps1 is an interactive CLI installer: its output is purely
        # informational console UI that the script never captures itself. Routing
        # it through Write-Host (rather than Write-Output) is deliberate -- it is
        # what keeps function return values clean (Write-Output inside a function
        # leaks into the return value, which previously broke -Verify's exit code).
        'PSAvoidUsingWriteHost',

        # False positives on -DryRun / -Force: both switches are used, but only
        # inside nested functions that close over them, which the analyzer's
        # parameter-usage heuristic does not trace across function boundaries.
        'PSReviewUnusedParameter',

        # New-Sandbox in the Pester suite is a test helper, not a public cmdlet,
        # and does not mutate external state worth gating behind -WhatIf/-Confirm.
        'PSUseShouldProcessForStateChangingFunctions'
    )
}
