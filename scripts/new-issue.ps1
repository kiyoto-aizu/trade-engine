[CmdletBinding()]
param(
  [Parameter(Mandatory = $true)]
  [string]$Title,
  [Parameter(Mandatory = $true)]
  [string]$BodyFile,
  [string[]]$Labels = @(),
  [string[]]$Assignees = @(),
  [switch]$DryRun
)

$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"

Write-Host "[1/7] start"
$url = "https://dark-lake-0586.merrylife1224.workers.dev/create-issue"

Write-Host "[2/7] read .env"
$envPath = Join-Path (Split-Path -Parent $PSScriptRoot) ".env"
if (-not (Test-Path -LiteralPath $envPath -PathType Leaf)) { throw "Required .env file not found: $envPath" }

$secretLine = Get-Content -LiteralPath $envPath -Encoding UTF8 |
  Where-Object { $_ -match '^\s*(?:export\s+)?ISSUE_CREATION_WEBHOOK_SECRET\s*=' } |
  Select-Object -Last 1
if (-not $secretLine) { throw "ISSUE_CREATION_WEBHOOK_SECRET is not set in .env." }

$secret = ($secretLine -replace '^\s*(?:export\s+)?ISSUE_CREATION_WEBHOOK_SECRET\s*=\s*', '').Trim()
if ($secret.Length -ge 2 -and (($secret.StartsWith('"') -and $secret.EndsWith('"')) -or ($secret.StartsWith("'") -and $secret.EndsWith("'")))) {
  $secret = $secret.Substring(1, $secret.Length - 2)
}
if ([string]::IsNullOrWhiteSpace($secret)) { throw "ISSUE_CREATION_WEBHOOK_SECRET is empty in .env." }

Write-Host "[3/7] read body file"
if (-not (Test-Path -LiteralPath $BodyFile -PathType Leaf)) { throw "Body file not found: $BodyFile" }
[string]$bodyText = Get-Content -LiteralPath $BodyFile -Raw -Encoding UTF8

Write-Host "[4/7] build payload"
$payload = @{ title = $Title; body = $bodyText }
if ($Labels.Count -gt 0) { $payload.labels = $Labels }
if ($Assignees.Count -gt 0) { $payload.assignees = $Assignees }
if ($DryRun.IsPresent) { $payload.dry_run = $true }

$json = $payload | ConvertTo-Json -Depth 20
$bytes = [System.Text.Encoding]::UTF8.GetBytes($json)

Write-Host "[5/7] preflight DNS/TLS"
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
[void][System.Net.Dns]::GetHostAddresses("dark-lake-0586.merrylife1224.workers.dev")

Write-Host "[6/7] POST start (30s timeout)"
Add-Type -AssemblyName System.Net.Http
$handler = [System.Net.Http.HttpClientHandler]::new()
$client  = [System.Net.Http.HttpClient]::new($handler)
$client.Timeout = [TimeSpan]::FromSeconds(30)
$req = [System.Net.Http.HttpRequestMessage]::new([System.Net.Http.HttpMethod]::Post, $url)
$req.Headers.Add("x-webhook-secret", $secret)
$req.Content = [System.Net.Http.ByteArrayContent]::new($bytes)
$req.Content.Headers.ContentType = [System.Net.Http.Headers.MediaTypeHeaderValue]::new("application/json")
$req.Content.Headers.ContentType.CharSet = "utf-8"

$resp = $client.SendAsync($req).GetAwaiter().GetResult()
$respText = $resp.Content.ReadAsStringAsync().GetAwaiter().GetResult()

Write-Host "[7/7] POST done: $([int]$resp.StatusCode)"
if (-not $resp.IsSuccessStatusCode) {
  throw "HTTP $([int]$resp.StatusCode): $respText"
}
$respText