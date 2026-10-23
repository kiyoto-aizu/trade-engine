[CmdletBinding(SupportsShouldProcess)]
param(
    [string]$TaskName = 'trade-engine-backtest',
    [datetime]$At = [datetime]'08:00',
    [switch]$Remove
)

$ErrorActionPreference = 'Stop'
$runner = Join-Path $PSScriptRoot 'run_backtest.ps1'

if ($Remove) {
    if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
        if ($PSCmdlet.ShouldProcess($TaskName, 'Remove scheduled task')) {
            Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
        }
    }
    exit 0
}

if (-not (Test-Path $runner)) {
    throw "Backtest runner was not found: $runner"
}

$trigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Saturday -At $At
$action = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$runner`""
# -WakeToRun: PCがスリープしても目覚めさせて実行を継続する
$settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -WakeToRun

if ($PSCmdlet.ShouldProcess($TaskName, "Register weekly backtest task on Saturday at $($At.ToString('HH:mm'))")) {
    Register-ScheduledTask `
        -TaskName $TaskName `
        -Action $action `
        -Trigger $trigger `
        -Settings $settings `
        -Description 'Runs the weekly trade-engine time-series backtest from daily filtering results.' `
        -Force | Out-Null
}