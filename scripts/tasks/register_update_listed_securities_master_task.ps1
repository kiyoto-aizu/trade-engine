[CmdletBinding(SupportsShouldProcess)]
param(
    [string]$TaskName = 'trade-engine-update-listed-securities-master',
    [datetime]$At = [datetime]'09:00',
    [switch]$Remove
)

$ErrorActionPreference = 'Stop'
$runner = Join-Path $PSScriptRoot 'run_update_listed_securities_master.ps1'

if ($Remove) {
    if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
        if ($PSCmdlet.ShouldProcess($TaskName, 'Remove scheduled task')) {
            Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
        }
    }
    exit 0
}

if (-not (Test-Path $runner)) {
    throw "Listed securities master update runner was not found: $runner"
}

# 毎週土曜の朝、JPXの東証上場銘柄一覧(data_j.xlsx)を取得し、
# 上場銘柄マスタ(data/universe/listed_securities.csv)を更新する(ADR-0001)。
# 分足バックフィル(07:30)と同じ曜日だが、時間をずらして競合を避ける。
$trigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Saturday -At $At
$action = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$runner`""
$settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -WakeToRun

if ($PSCmdlet.ShouldProcess($TaskName, "Register weekly Saturday listed-securities-master update task at $($At.ToString('HH:mm'))")) {
    Register-ScheduledTask `
        -TaskName $TaskName `
        -Action $action `
        -Trigger $trigger `
        -Settings $settings `
        -Description 'Updates the listed securities master (data/universe/listed_securities.csv) from JPX data_j.xlsx every Saturday (ADR-0001).' `
        -Force | Out-Null
}
