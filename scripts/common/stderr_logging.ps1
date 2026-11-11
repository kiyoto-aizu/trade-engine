function Initialize-StderrLogging {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)]
        [string]$ProjectRoot,
        [Parameter(Mandatory)]
        [string]$ScriptName
    )

    $currentMonth = (Get-Date).ToString('yyyyMM')
    $logsRoot = Join-Path $ProjectRoot 'data\logs'
    $archiveRoot = 'D:\logs'
    $python = Join-Path $ProjectRoot '.venv\Scripts\python.exe'

    if (-not (Test-Path -LiteralPath $python -PathType Leaf)) {
        throw "Python virtual environment was not found: $python"
    }

    Push-Location $ProjectRoot
    try {
        & $python -m src.infrastructure.monthly_log_storage `
            --logs-root $logsRoot `
            --archive-root $archiveRoot `
            --current-month $currentMonth `
            --apply
        if ($LASTEXITCODE -ne 0) {
            Write-Warning 'Monthly log archival failed. Existing logs were retained where transfer was incomplete; task logging will continue.'
        }
    }
    finally {
        Pop-Location
    }

    $dateText = (Get-Date).ToString('yyyy-MM-dd')
    $logDir = Join-Path $logsRoot "jobs\$currentMonth"
    New-Item -ItemType Directory -Path $logDir -Force | Out-Null
    return (Join-Path $logDir "${ScriptName}_stderr_${dateText}.log")
}
