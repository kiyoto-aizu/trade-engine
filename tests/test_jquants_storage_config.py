import os
import json
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

from src.jquants_storage import (
    default_jquants_storage_settings,
    inspect_jquants_storage,
)
from src.entrypoints import check_jquants_storage_config


def make_settings(root: str) -> SimpleNamespace:
    directory = Path(root)
    return SimpleNamespace(
        JQUANTS_RAW_DIRECTORY=directory / "raw",
        JQUANTS_CANONICAL_DIRECTORY=directory / "canonical",
        JQUANTS_LEDGER_DATABASE_FILE=directory / "ledger.sqlite3",
        JQUANTS_EXECUTION_METRICS_DIRECTORY=directory / "metrics",
        JQUANTS_BACKFILL_STATE_DIRECTORY=directory / "backfill_state",
        JQUANTS_CALENDAR_DIRECTORY=directory / "calendar",
    )


def test_storage_audit_accepts_normalized_d_drive_paths():
    settings = make_settings("D:/trade-engine-data/jquants")

    audit = inspect_jquants_storage(settings, {})

    assert audit.is_valid
    assert audit.as_dict()["status"] == "PASS"
    assert all(item.drive == "D:" for item in audit.destinations)
    assert next(item for item in audit.destinations if item.name == "raw").path.endswith(
        r"\raw"
    )


def test_storage_audit_rejects_any_non_d_destination():
    settings = make_settings("D:/trade-engine-data/jquants")
    settings.JQUANTS_BACKFILL_STATE_DIRECTORY = Path(
        "C:/trade-engine-data/jquants/backfill_state"
    )

    audit = inspect_jquants_storage(settings, {})

    assert not audit.is_valid
    assert audit.as_dict()["status"] == "FAILED"
    backfill = next(
        item for item in audit.destinations if item.name == "backfill_state"
    )
    assert not backfill.matches_operational_drive


def test_storage_audit_reports_override_presence_without_values():
    settings = make_settings("D:/trade-engine-data/jquants")
    environ = {
        "JQUANTS_RAW_DIRECTORY": "D:/trade-engine-data/jquants/raw",
        "JQUANTS_API_KEY": "secret-sentinel-must-not-be-printed",
    }

    audit = inspect_jquants_storage(settings, environ)
    output = json.dumps(audit.as_dict())

    assert audit.destinations[0].source == "environment"
    assert audit.destinations[1].source == "default"
    assert "secret-sentinel-must-not-be-printed" not in output


def test_storage_audit_does_not_access_or_create_storage_paths(monkeypatch):
    settings = make_settings("D:/missing-jquants-audit-path")

    def fail_if_accessed(*args, **kwargs):
        raise AssertionError("storage audit must not access the filesystem")

    monkeypatch.setattr(Path, "mkdir", fail_if_accessed)
    monkeypatch.setattr(Path, "open", fail_if_accessed)
    monkeypatch.setattr(os, "makedirs", fail_if_accessed)

    audit = inspect_jquants_storage(settings, {})

    assert audit.is_valid


def test_entrypoint_prints_audit_without_reading_api_key(monkeypatch, capsys):
    monkeypatch.setattr(
        check_jquants_storage_config,
        "default_jquants_storage_settings",
        lambda: make_settings("D:/trade-engine-data/jquants"),
    )
    monkeypatch.setenv("JQUANTS_RAW_DIRECTORY", "D:/trade-engine-data/jquants/raw")
    monkeypatch.setenv("JQUANTS_API_KEY", "secret-sentinel-must-not-be-printed")

    result = check_jquants_storage_config.main()
    output = capsys.readouterr().out

    assert result == 0
    audit = json.loads(output)
    raw = next(item for item in audit["destinations"] if item["name"] == "raw")
    assert raw["source"] == "environment"
    assert "secret-sentinel-must-not-be-printed" not in output


def test_entrypoint_fails_when_a_destination_is_not_on_d_drive(monkeypatch, capsys):
    settings = make_settings("D:/trade-engine-data/jquants")
    settings.JQUANTS_LEDGER_DATABASE_FILE = Path(
        "C:/trade-engine-data/jquants/ledger.sqlite3"
    )
    monkeypatch.setattr(
        check_jquants_storage_config,
        "default_jquants_storage_settings",
        lambda: settings,
    )

    result = check_jquants_storage_config.main()
    output = capsys.readouterr().out

    assert result == 1
    assert '"status": "FAILED"' in output


def test_storage_audit_rejects_environment_override_to_c_drive():
    settings = make_settings("D:/trade-engine-data/jquants")

    audit = inspect_jquants_storage(
        settings,
        {"JQUANTS_RAW_DIRECTORY": "C:/trade-engine-data/jquants/raw"},
    )

    raw = next(item for item in audit.destinations if item.name == "raw")
    assert not audit.is_valid
    assert raw.source == "environment"
    assert raw.drive == "C:"


def test_storage_audit_accepts_lowercase_d_drive():
    settings = make_settings("d:/trade-engine-data/jquants")

    audit = inspect_jquants_storage(settings, {})

    assert audit.is_valid
    assert all(item.drive == "D:" for item in audit.destinations)


def test_storage_audit_rejects_unc_and_relative_paths():
    settings = make_settings("D:/trade-engine-data/jquants")
    settings.JQUANTS_RAW_DIRECTORY = Path(r"\\server\share\jquants\raw")
    settings.JQUANTS_CALENDAR_DIRECTORY = Path("data/jquants/calendar")

    audit = inspect_jquants_storage(settings, {})

    raw = next(item for item in audit.destinations if item.name == "raw")
    calendar = next(item for item in audit.destinations if item.name == "calendar")
    assert not raw.matches_operational_drive
    assert raw.drive == r"\\SERVER\SHARE"
    assert not calendar.matches_operational_drive


def test_storage_audit_rejects_drive_relative_paths():
    settings = make_settings("D:/trade-engine-data/jquants")
    settings.JQUANTS_RAW_DIRECTORY = Path("D:relative/raw")

    audit = inspect_jquants_storage(settings, {})

    raw = next(item for item in audit.destinations if item.name == "raw")
    assert raw.drive == "D:"
    assert not raw.is_absolute
    assert not raw.matches_operational_drive


def test_storage_audit_rejects_relative_path_even_if_cwd_is_on_d_drive(monkeypatch):
    settings = make_settings("D:/trade-engine-data/jquants")
    settings.JQUANTS_RAW_DIRECTORY = Path("data/jquants/raw")
    monkeypatch.setattr(os.path, "abspath", lambda _: r"D:\runtime\data\jquants\raw")

    audit = inspect_jquants_storage(settings, {})

    raw = next(item for item in audit.destinations if item.name == "raw")
    assert raw.drive == "D:"
    assert raw.path == r"D:\runtime\data\jquants\raw"
    assert not raw.is_absolute
    assert not raw.matches_operational_drive


def test_storage_audit_rejects_root_relative_windows_path(monkeypatch):
    settings = make_settings("D:/trade-engine-data/jquants")
    settings.JQUANTS_RAW_DIRECTORY = Path(r"\runtime\data\jquants\raw")
    monkeypatch.setattr(os, "name", "nt")
    monkeypatch.setattr(os.path, "abspath", lambda _: r"D:\runtime\data\jquants\raw")

    audit = inspect_jquants_storage(settings, {})

    raw = next(item for item in audit.destinations if item.name == "raw")
    assert raw.drive == ""
    assert not raw.is_absolute
    assert not raw.matches_operational_drive


def test_storage_audit_normalizes_windows_paths_without_filesystem_resolution(
    monkeypatch,
):
    settings = make_settings("d:/trade-engine-data/jquants")
    monkeypatch.setattr(os, "name", "nt")
    monkeypatch.setattr(os.path, "abspath", lambda *_: (_ for _ in ()).throw(AssertionError()))

    audit = inspect_jquants_storage(settings, {})

    assert audit.is_valid
    assert all(item.path.startswith("d:\\") for item in audit.destinations)


def test_cli_import_does_not_load_config_or_read_unrelated_environment():
    root = Path(__file__).resolve().parents[1]
    script = r'''
import os
import runpy
import sys
import types

allowed = {
    "JQUANTS_RAW_DIRECTORY",
    "JQUANTS_CANONICAL_DIRECTORY",
    "JQUANTS_LEDGER_DATABASE_FILE",
    "JQUANTS_EXECUTION_METRICS_DIRECTORY",
    "JQUANTS_BACKFILL_STATE_DIRECTORY",
    "JQUANTS_CALENDAR_DIRECTORY",
}

class GuardedEnvironment(dict):
    def _check(self, key):
        if key not in allowed:
            raise AssertionError("unexpected environment access")
    def __contains__(self, key):
        self._check(key)
        return super().__contains__(key)
    def get(self, key, default=None):
        self._check(key)
        return super().get(key, default)

os.environ = GuardedEnvironment({
    name: "D:/trade-engine-data/jquants/" + name.removeprefix("JQUANTS_").lower()
    for name in allowed
})
dotenv = types.ModuleType("dotenv")
def fail_if_dotenv_loaded(*args, **kwargs):
    raise AssertionError(".env must not be loaded")
dotenv.load_dotenv = fail_if_dotenv_loaded
sys.modules["dotenv"] = dotenv
runpy.run_module("src.entrypoints.check_jquants_storage_config", run_name="__main__")
assert "src.config.config" not in sys.modules
'''
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=root,
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "JQUANTS_API_KEY": "sentinel-must-not-be-read-or-printed",
            "PYTHON_DOTENV_DISABLED": "1",
        },
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert "sentinel-must-not-be-read-or-printed" not in result.stdout
    assert "sentinel-must-not-be-read-or-printed" not in result.stderr
    assert "src.config.config" not in result.stdout
