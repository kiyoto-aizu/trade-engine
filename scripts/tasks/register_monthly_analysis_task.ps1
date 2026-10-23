[CmdletBinding(SupportsShouldProcess)]
param(
    [string]$TaskName = 'trade-engine-monthly-analysis',
    [datetime]$At = [datetime]'17:00',
    [switch]$Remove
)

$ErrorActionPreference = 'Stop'
$runner = Join-Path $PSScriptRoot 'run_monthly_analysis.ps1'

if ($Remove) {
    if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
        if ($PSCmdlet.ShouldProcess($TaskName, 'Remove scheduled task')) {
            Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
        }
    }
    exit 0
}

if (-not (Test-Path $runner)) {
    throw "Monthly analysis runner was not found: $runner"
}

$trigger = New-ScheduledTaskTrigger -Daily -At $At
$action = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$runner`""
# -WakeToRun: PCがスリープしても目覚めさせて実行を継続する
$settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -WakeToRun

if ($PSCmdlet.ShouldProcess($TaskName, "Register monthly analysis task at $($At.ToString('HH:mm'))")) {
    Register-ScheduledTask `
        -TaskName $TaskName `
        -Action $action `
        -Trigger $trigger `
        -Settings $settings `
        -Description 'Runs monthly analysis at month end; the Python runner skips non-month-end days.' `
        -Force | Out-Null
}