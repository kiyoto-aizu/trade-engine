"""Windows task definitions are exercised with mocks, never registered."""

import json
import os
from pathlib import Path
import shutil
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[1]
REGISTER = ROOT / "scripts" / "tasks" / "register_jquants_task.ps1"
RUNNER = ROOT / "scripts" / "tasks" / "run_jquants.ps1"
POWERSHELL = shutil.which("powershell.exe")
pytestmark = pytest.mark.skipif(
    os.name != "nt" or POWERSHELL is None, reason="Windows PowerShell required"
)


def quote(path):
    return "'" + str(path).replace("'", "''") + "'"


def run_ps(script):
    return subprocess.run(
        [POWERSHELL, "-NoProfile", "-NonInteractive", "-Command", script],
        capture_output=True, text=True, timeout=30, cwd=ROOT,
    )


MOCKS = r"""
$ErrorActionPreference = 'Stop'
$global:Calls = @()
$global:Existing = $false
function Get-TimeZone { return @{ Id = 'Tokyo Standard Time' } }
function Get-ScheduledTask {
    param($TaskName, $TaskPath, $ErrorAction)
    $global:Calls += 'Get'
    if ($global:Existing) { return @{ TaskName = 'trade-engine-jquants' } }
}
function New-ScheduledTaskTrigger {
    param([switch]$Daily, $At)
    $global:Trigger = @{ Daily = [bool]$Daily; At = $At.ToString('HH:mm') }
    return 'trigger'
}
function New-ScheduledTaskAction {
    param($Execute, $Argument, $WorkingDirectory)
    $global:Action = @{ Execute = $Execute; Argument = $Argument; WorkingDirectory = $WorkingDirectory }
    return 'action'
}
function New-ScheduledTaskPrincipal {
    param($UserId, $LogonType, $RunLevel)
    $global:Principal = @{ UserId = $UserId; LogonType = $LogonType; RunLevel = $RunLevel }
    return 'principal'
}
function New-ScheduledTaskSettingsSet {
    param($MultipleInstances, $ExecutionTimeLimit, [switch]$Disable,
          [switch]$WakeToRun, [switch]$StartWhenAvailable, $RestartCount)
    $global:Settings = @{
        MultipleInstances = $MultipleInstances; ExecutionTimeLimit = $ExecutionTimeLimit.TotalSeconds
        Disable = [bool]$Disable; WakeToRun = [bool]$WakeToRun
        StartWhenAvailable = [bool]$StartWhenAvailable; RestartCount = $RestartCount
    }
    return 'settings'
}
function New-ScheduledTask {
    param($Action, $Trigger, $Principal, $Settings, $Description)
    return 'task'
}
function Register-ScheduledTask {
    param($TaskName, $TaskPath, $InputObject, [switch]$Force)
    $global:Calls += 'Register'
    $global:Registered = @{ TaskName = $TaskName; TaskPath = $TaskPath; Force = [bool]$Force }
}
function Unregister-ScheduledTask {
    param($TaskName, $TaskPath, [switch]$Confirm)
    $global:Calls += 'Remove'
    $global:Removed = @{ TaskName = $TaskName; TaskPath = $TaskPath }
}
"""


def registration(arguments="", setup=""):
    result = run_ps(
        MOCKS + setup + f"\n& {quote(REGISTER)} {arguments}\n"
        + """@{ Calls = @($global:Calls); Trigger = $global:Trigger;
            Action = $global:Action; Principal = $global:Principal;
            Settings = $global:Settings; Registered = $global:Registered;
            Removed = $global:Removed } | ConvertTo-Json -Depth 5 -Compress"""
    )
    return result


def test_scripts_parse_without_executing():
    for path in (REGISTER, RUNNER):
        result = run_ps(
            "$tokens=$null; $errors=$null; "
            f"[System.Management.Automation.Language.Parser]::ParseFile({quote(path)},"
            " [ref]$tokens, [ref]$errors) | Out-Null; "
            "if ($errors.Count) { exit 1 }; exit 0"
        )
        assert result.returncode == 0, result.stderr


def test_registration_is_disabled_interactive_and_jst():
    result = registration("-Account 'test-account'")
    assert result.returncode == 0, result.stderr
    body = json.loads(result.stdout)
    assert body["Calls"] == ["Get", "Register"]
    assert body["Trigger"] == {"Daily": True, "At": "17:00"}
    assert body["Principal"] == {
        "UserId": "test-account", "LogonType": "Interactive", "RunLevel": "Limited",
    }
    assert body["Settings"] == {
        "MultipleInstances": "IgnoreNew", "ExecutionTimeLimit": 0,
        "Disable": True, "WakeToRun": False, "StartWhenAvailable": False,
        "RestartCount": None,
    }
    assert body["Registered"]["TaskPath"] == "\\"
    assert body["Registered"]["Force"] is False
    assert body["Action"]["WorkingDirectory"] == str(ROOT)
    assert f'-File "{RUNNER}" -Execute -Scheduled' in body["Action"]["Argument"]


@pytest.mark.parametrize("arguments", [
    "-Account 'test-account' -WhatIf",
    "-Remove -WhatIf",
])
def test_whatif_does_not_call_task_cmdlets(arguments):
    result = registration(arguments)
    assert result.returncode == 0, result.stderr
    body = json.loads(result.stdout.strip().splitlines()[-1])
    assert body["Calls"] == []


def test_existing_task_requires_explicit_replace():
    result = registration("-Account 'test-account'", "$global:Existing = $true")
    assert result.returncode != 0
    assert "JQUANTS_TASK_EXISTS_USE_REPLACE" in result.stderr
    result = registration("-Account 'test-account' -Replace", "$global:Existing = $true")
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["Registered"]["Force"] is True
    assert json.loads(result.stdout)["Settings"]["Disable"] is True


def test_remove_only_targets_jquants_definition():
    result = registration("-Remove", "$global:Existing = $true")
    assert result.returncode == 0, result.stderr
    body = json.loads(result.stdout)
    assert body["Calls"] == ["Get", "Remove"]
    assert body["Removed"] == {
        "TaskName": "trade-engine-jquants", "TaskPath": "\\",
    }
    assert body["Registered"] is None


@pytest.mark.parametrize("arguments,setup,code", [
    ("", "", "JQUANTS_TASK_ACCOUNT_REQUIRED"),
    ("-Remove -Replace", "", "JQUANTS_TASK_INVALID_OPTIONS"),
    ("-Account 'test-account'", "function Get-TimeZone { @{ Id = 'UTC' } }",
     "JQUANTS_TASK_JST_REQUIRED"),
    ("-Remove -TaskName trade-engine-trading", "", "ParameterArgumentValidationError"),
])
def test_registration_rejects_unsafe_inputs(arguments, setup, code):
    result = registration(arguments, setup)
    assert result.returncode != 0
    assert code in result.stderr


@pytest.mark.parametrize("arguments", ["", "-Scheduled", "-Execute"])
def test_runner_missing_python_is_sanitized_non_success(arguments):
    result = run_ps(
        "function Test-Path { return $false }; "
        "function Get-TimeZone { return @{ Id = 'Tokyo Standard Time' } }; "
        f"& {quote(RUNNER)} {arguments}"
    )
    assert result.returncode == 1
    assert json.loads(result.stdout) == {"stage": "RUNNER", "exit_code": 1}
    assert result.stderr.strip() == "JQUANTS_RUNNER_NON_SUCCESS"
    assert str(ROOT) not in result.stdout + result.stderr


@pytest.mark.parametrize("code", [0, 1, 2, 3, 99])
def test_runner_propagates_allowed_codes_without_forwarding_native_output(code):
    python = ROOT / ".venv" / "Scripts" / "python.exe"
    result = run_ps(
        "function Test-Path { return $true }; "
        f"Set-Item -LiteralPath ('Function:\\' + {quote(python)}) -Value "
        "{ Write-Output 'SECRET_RESPONSE'; "
        f"$global:LASTEXITCODE = {code} }}; "
        f"& {quote(RUNNER)}; exit $LASTEXITCODE"
    )
    expected = code if code in (0, 1, 2, 3) else 1
    assert result.returncode == expected, result.stderr
    assert json.loads(result.stdout) == {"stage": "RUNNER", "exit_code": expected}
    assert "SECRET_RESPONSE" not in result.stdout + result.stderr


def test_runner_does_not_contain_operational_cleanup_or_task_calls():
    source = RUNNER.read_text()
    for forbidden in ("Remove-Item", "Stop-Process", "ScheduledTask", ".env", "stderr_logging"):
        assert forbidden not in source
    assert "--execute" in source and "--scheduled" in source


@pytest.mark.parametrize("arguments,expected", [
    ("", ["-m", "src.entrypoints.run_jquants_ops"]),
    ("-Execute", ["-m", "src.entrypoints.run_jquants_ops", "--execute"]),
    ("-Execute -Scheduled",
     ["-m", "src.entrypoints.run_jquants_ops", "--execute", "--scheduled"]),
])
def test_runner_has_explicit_execution_and_scheduled_arguments(arguments, expected):
    python = ROOT / ".venv" / "Scripts" / "python.exe"
    result = run_ps(
        "function Test-Path { return $true }; "
        "function Get-TimeZone { return @{ Id = 'Tokyo Standard Time' } }; "
        f"Set-Item -LiteralPath ('Function:\\' + {quote(python)}) -Value "
        "{ $global:NativeArgs = @($args); Write-Error 'SECRET_WEBHOOK_RESPONSE'; "
        "$global:LASTEXITCODE = 0 }; "
        f"& {quote(RUNNER)} {arguments}; "
        "ConvertTo-Json -InputObject @($global:NativeArgs) -Compress; exit $LASTEXITCODE"
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout.strip().splitlines()[-1]) == expected
    assert "SECRET_WEBHOOK_RESPONSE" not in result.stdout + result.stderr


def test_execute_requires_jst_before_python_invocation():
    result = run_ps(
        "function Get-TimeZone { return @{ Id = 'UTC' } }; "
        "function Test-Path { throw 'SHOULD_NOT_PROBE_PYTHON' }; "
        f"& {quote(RUNNER)} -Execute; exit $LASTEXITCODE"
    )
    assert result.returncode == 1
    assert result.stderr.strip() == "JQUANTS_RUNNER_NON_SUCCESS"


@pytest.mark.parametrize("arguments", ["-Account 'test-account'", "-Remove"])
def test_task_query_errors_are_not_treated_as_missing(arguments):
    result = registration(
        arguments, "function Get-ScheduledTask { throw 'MOCK_TASK_QUERY_DENIED' }"
    )
    assert result.returncode != 0
    assert "MOCK_TASK_QUERY_DENIED" in result.stderr
