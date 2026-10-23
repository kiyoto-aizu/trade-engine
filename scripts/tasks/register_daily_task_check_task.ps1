[CmdletBinding(SupportsShouldProcess)]
param(
    [string]$TaskName = 'trade-engine-daily-task-check',
    [datetime]$At = [datetime]'18:00',
    [switch]$Remove
)

$ErrorActionPreference = 'Stop'
$runner = Join-Path $PSScriptRoot 'run_daily_task_check.ps1'

if ($Remove) {
    if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
        if ($PSCmdlet.ShouldProcess($TaskName, 'Remove scheduled task')) {
            Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
        }
    }
    exit 0
}

if (-not (Test-Path $runner)) {
    throw "Daily task check runner was not found: $runner"
}

$trigger = New-ScheduledTaskTrigger -Daily -At $At
$action = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$runner`""
$settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -WakeToRun

if ($PSCmdlet.ShouldProcess($TaskName, "Register daily task check notification at $($At.ToString('HH:mm'))")) {
    Register-ScheduledTask `
        -TaskName $TaskName `
        -Action $action `
        -Trigger $trigger `
        -Settings $settings `
        -Description 'Checks and reports the daily scheduled-task results to Slack.' `
        -Force | Out-Null
}