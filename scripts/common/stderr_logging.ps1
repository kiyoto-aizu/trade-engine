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
    $dateText = (Get-Date).ToString('yyyy-MM-dd')
    $logDir = Join-Path $logsRoot "jobs\$currentMonth"
    New-Item -ItemType Directory -Path $logDir -Force | Out-Null
    return (Join-Path $logDir "${ScriptName}_stderr_${dateText}.log")
}
