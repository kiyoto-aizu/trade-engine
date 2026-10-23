$url = "https://dark-lake-0586.merrylife1224.workers.dev/create-issue"
$envPath = Join-Path (Split-Path -Parent $PSScriptRoot) ".env"
if (-not (Test-Path -LiteralPath $envPath -PathType Leaf)) {
  throw "Required .env file not found: $envPath"
}

$secretLine = Get-Content -LiteralPath $envPath |
  Where-Object { $_ -match '^\s*(?:export\s+)?ISSUE_CREATION_WEBHOOK_SECRET\s*=' } |
  Select-Object -Last 1
if (-not $secretLine) {
  throw "ISSUE_CREATION_WEBHOOK_SECRET is not set in .env."
}

$secret = ($secretLine -replace '^\s*(?:export\s+)?ISSUE_CREATION_WEBHOOK_SECRET\s*=\s*', '').Trim()
if ($secret.Length -ge 2 -and
    (($secret.StartsWith('"') -and $secret.EndsWith('"')) -or
     ($secret.StartsWith("'") -and $secret.EndsWith("'")))) {
  $secret = $secret.Substring(1, $secret.Length - 2)
}
if ([string]::IsNullOrWhiteSpace($secret)) {
  throw "ISSUE_CREATION_WEBHOOK_SECRET is empty in .env."
}

$body = @{
  title = "TRD-PR3.1: 実APIパイロット反映（未知項目許可リスト・実行計測・日足/分足突合・tzdata）"
  body  = @"
## Implementation Request
- Task ID: TRD-PR3-1
- Base Branch: main
- Target Branch: feat/TRD-PR3-1
- Label Transition: `wf:plan-ready` -> `wf:impl-running`
"@
  labels    = @("wf:plan-ready","jquants","data-pipeline","quality","windows")
  assignees = @("kiyoto-aizu")
} | ConvertTo-Json -Depth 10

$response = Invoke-RestMethod -Method Post -Uri $url `
  -Headers @{ "x-webhook-secret" = $secret } `
  -ContentType "application/json" `
  -Body $body

$response | Format-List