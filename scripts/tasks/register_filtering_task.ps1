[CmdletBinding(SupportsShouldProcess)]
param(
    [string]$TaskName = 'trade-engine-filtering',
    [datetime]$At = [datetime]'09:30',
    [switch]$Remove
)

$ErrorActionPreference = 'Stop'
$scriptsRoot = Split-Path -Parent $PSScriptRoot
$projectRoot = Split-Path -Parent $scriptsRoot
$runner = Join-Path $PSScriptRoot 'run_filtering.ps1'

if ($Remove) {
    if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
        if ($PSCmdlet.ShouldProcess($TaskName, 'Remove scheduled task')) {
            Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
        }
    }
    exit 0
}

if (-not (Test-Path $runner)) {
    throw "Filtering runner was not found: $runner"
}

$trigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Monday, Tuesday, Wednesday, Thursday, Friday -At $At
$action = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$runner`""
# -WakeToRun: PCがスリープしても目覚めさせて実行を継続する
$settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -WakeToRun
$description = "Runs trade-engine filtering from $projectRoot on business weekdays."

if ($PSCmdlet.ShouldProcess($TaskName, "Register weekday filtering task at $($At.ToString('HH:mm'))")) {
    Register-ScheduledTask `
        -TaskName $TaskName `
        -Action $action `
        -Trigger $trigger `
        -Settings $settings `
        -Description $description `
        -Force | Out-Null
}
