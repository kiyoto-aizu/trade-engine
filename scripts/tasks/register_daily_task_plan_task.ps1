[CmdletBinding(SupportsShouldProcess)]
param(
    [string]$TaskName = 'trade-engine-daily-task-plan',
    [datetime]$At = [datetime]'07:00',
    [switch]$Remove
)

$ErrorActionPreference = 'Stop'
$runner = Join-Path $PSScriptRoot 'run_daily_task_plan.ps1'

if ($Remove) {
    if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
        if ($PSCmdlet.ShouldProcess($TaskName, 'Remove scheduled task')) {
            Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
        }
    }
    exit 0
}

if (-not (Test-Path $runner)) {
    throw "Daily task plan runner was not found: $runner"
}

$trigger = New-ScheduledTaskTrigger -Daily -At $At
$action = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$runner`""
$settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -WakeToRun

if ($PSCmdlet.ShouldProcess($TaskName, "Register daily task plan notification at $($At.ToString('HH:mm'))")) {
    Register-ScheduledTask `
        -TaskName $TaskName `
        -Action $action `
        -Trigger $trigger `
        -Settings $settings `
        -Description 'Sends the daily scheduled-task plan to Slack.' `
        -Force | Out-Null
}