[CmdletBinding()]
param(
    [switch]$Execute,
    [switch]$Scheduled
)

$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
$python = Join-Path $projectRoot '.venv\Scripts\python.exe'
$code = 1

try {
    if ($Scheduled -and -not $Execute) {
        throw 'JQUANTS_RUNNER_INVALID_OPTIONS'
    }
    if ($Execute -and (Get-TimeZone).Id -ne 'Tokyo Standard Time') {
        throw 'JQUANTS_RUNNER_JST_REQUIRED'
    }
    if (-not (Test-Path -LiteralPath $python -PathType Leaf)) {
        throw 'JQUANTS_RUNNER_PYTHON_MISSING'
    }
    $arguments = @('-m', 'src.entrypoints.run_jquants_ops')
    if ($Execute) { $arguments += '--execute' }
    if ($Scheduled) { $arguments += '--scheduled' }
    Push-Location $projectRoot
    try {
        # Python owns sanitized operational records; never forward native exception text.
        $ErrorActionPreference = 'Continue'
        & $python @arguments 2>&1 | Out-Null
        $code = $LASTEXITCODE
        if ($null -eq $code -or $code -notin @(0, 1, 2, 3)) { $code = 1 }
    }
    finally {
        Pop-Location
        $ErrorActionPreference = 'Stop'
    }
}
catch {
    $code = 1
}

Write-Output ('{{"stage":"RUNNER","exit_code":{0}}}' -f $code)
if ($code -ne 0) {
    [Console]::Error.WriteLine('JQUANTS_RUNNER_NON_SUCCESS')
}
exit $code
