[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$python = Join-Path $projectRoot '.venv\Scripts\python.exe'
$archiveRoot = 'D:\logs'
$currentMonth = (Get-Date).ToString('yyyyMM')

if (-not (Test-Path -LiteralPath $python -PathType Leaf)) {
    throw "Python virtual environment was not found: $python"
}

Push-Location $projectRoot
try {
    & $python -m src.infrastructure.monthly_log_storage `
        --archive-root $archiveRoot `
        --current-month $currentMonth `
        --preview
    if ($LASTEXITCODE -ne 0) {
        throw "Monthly log archive preview failed with exit code $LASTEXITCODE."
    }
}
finally {
    Pop-Location
}
