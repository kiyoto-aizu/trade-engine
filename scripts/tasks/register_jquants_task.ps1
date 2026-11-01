[CmdletBinding(SupportsShouldProcess)]
param(
    [ValidatePattern('^trade-engine-jquants(-[A-Za-z0-9_-]+)?$')]
    [string]$TaskName = 'trade-engine-jquants',
    [string]$Account,
    [switch]$Replace,
    [switch]$Remove
)

$ErrorActionPreference = 'Stop'
$taskPath = '\'
$runner = Join-Path $PSScriptRoot 'run_jquants.ps1'
$projectRoot = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)

if ($Remove) {
    if ($Replace) {
        throw 'JQUANTS_TASK_INVALID_OPTIONS'
    }
    if ($PSCmdlet.ShouldProcess($TaskName, 'Remove J-Quants task definition only')) {
        $existing = Get-ScheduledTask -TaskPath $taskPath -ErrorAction Stop |
            Where-Object { $_.TaskName -eq $TaskName }
        if ($existing) {
            Unregister-ScheduledTask -TaskName $TaskName -TaskPath $taskPath -Confirm:$false
        }
    }
    return
}

if ([string]::IsNullOrWhiteSpace($Account)) {
    throw 'JQUANTS_TASK_ACCOUNT_REQUIRED'
}
if (-not (Test-Path -LiteralPath $runner -PathType Leaf)) {
    throw 'JQUANTS_TASK_RUNNER_MISSING'
}
if ((Get-TimeZone).Id -ne 'Tokyo Standard Time') {
    throw 'JQUANTS_TASK_JST_REQUIRED'
}

if ($PSCmdlet.ShouldProcess($TaskName, 'Register DISABLED J-Quants task at 17:00 JST')) {
    $existing = Get-ScheduledTask -TaskPath $taskPath -ErrorAction Stop |
        Where-Object { $_.TaskName -eq $TaskName }
    if ($existing -and -not $Replace) {
        throw 'JQUANTS_TASK_EXISTS_USE_REPLACE'
    }
    $trigger = New-ScheduledTaskTrigger -Daily -At ([datetime]'17:00')
    $action = New-ScheduledTaskAction `
        -Execute 'powershell.exe' `
        -Argument "-NoProfile -NonInteractive -ExecutionPolicy Bypass -File `"$runner`" -Execute -Scheduled" `
        -WorkingDirectory $projectRoot
    $principal = New-ScheduledTaskPrincipal -UserId $Account -LogonType Interactive -RunLevel Limited
    $settings = New-ScheduledTaskSettingsSet `
        -MultipleInstances IgnoreNew `
        -ExecutionTimeLimit ([TimeSpan]::Zero) `
        -Disable
    $task = New-ScheduledTask -Action $action -Trigger $trigger -Principal $principal `
        -Settings $settings -Description 'J-Quants calendar, daily and gated backfill. Disabled until separate operational approval.'
    Register-ScheduledTask -TaskName $TaskName -TaskPath $taskPath -InputObject $task -Force:$Replace | Out-Null
}
