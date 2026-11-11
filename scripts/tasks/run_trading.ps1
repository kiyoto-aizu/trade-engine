[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$python = Join-Path $projectRoot '.venv\Scripts\python.exe'

if (-not (Test-Path -LiteralPath $python)) {
    Write-Error "Python virtual environment was not found: $python"
    exit 1
}

$env:TRADING_MODE = 'paper'
$env:ENABLE_LIVE_ORDERING = 'false'

$scriptsRoot = Split-Path -Parent $PSScriptRoot
. (Join-Path $scriptsRoot 'common\stderr_logging.ps1')
$stderrLog = Initialize-StderrLogging -ProjectRoot $projectRoot -ScriptName 'run_trading'
$dateText = (Get-Date).ToString('yyyy-MM-dd')

$ErrorActionPreference = 'Continue'
Push-Location $projectRoot
try {
    & $python -m src.entrypoints.run_trading 2>> $stderrLog
    exit $LASTEXITCODE
}
finally {
    Pop-Location
}
