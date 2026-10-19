[CmdletBinding(SupportsShouldProcess)]
param(
    [string]$TaskName = 'trade-pjoject-minute-backfill',
    [datetime]$At = [datetime]'07:30',
    [switch]$Remove
)

$ErrorActionPreference = 'Stop'
$runner = Join-Path $PSScriptRoot 'run_minute_backfill.ps1'

if ($Remove) {
    if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
        if ($PSCmdlet.ShouldProcess($TaskName, 'Remove scheduled task')) {
            Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
        }
    }
    exit 0
}

if (-not (Test-Path $runner)) {
    throw "Minute backfill runner was not found: $runner"
}

# 毎週土曜の朝、過去7日分の分足データを取得し不足時刻の足を追加する
$trigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Saturday -At $At
$action = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$runner`""
$settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -WakeToRun

if ($PSCmdlet.ShouldProcess($TaskName, "Register weekly Saturday minute-bar backfill task at $($At.ToString('HH:mm'))")) {
    Register-ScheduledTask `
        -TaskName $TaskName `
        -Action $action `
        -Trigger $trigger `
        -Settings $settings `
        -Description 'Backfills self-polled minute bars with Yahoo Finance intraday data every Saturday for the past 7 days.' `
        -Force | Out-Null
}
