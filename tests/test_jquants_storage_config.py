import json
from pathlib import Path
from types import SimpleNamespace

from src.config.jquants_storage import inspect_jquants_storage
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
    assert audit.destinations[0].path.endswith(r"\raw")


def test_storage_audit_rejects_any_non_d_destination():
    settings = make_settings("D:/trade-engine-data/jquants")
    settings.JQUANTS_BACKFILL_STATE_DIRECTORY = Path(
        "C:/trade-engine-data/jquants/backfill_state"
    )

    audit = inspect_jquants_storage(settings, {})

    assert not audit.is_valid
    assert audit.as_dict()["status"] == "FAILED"
    assert not audit.destinations[4].matches_operational_drive


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


def test_storage_audit_does_not_create_missing_directories(tmp_path):
    missing_root = tmp_path / "not-created"
    settings = make_settings(str(missing_root))

    audit = inspect_jquants_storage(settings, {})

    assert not audit.is_valid
    assert not missing_root.exists()


def test_entrypoint_prints_audit_without_reading_api_key(monkeypatch, capsys):
    settings = make_settings("D:/trade-engine-data/jquants")
    monkeypatch.setattr(check_jquants_storage_config, "config", settings, raising=False)
    monkeypatch.setattr(
        check_jquants_storage_config,
        "inspect_jquants_storage",
        lambda settings, environ: inspect_jquants_storage(settings, environ),
    )
    monkeypatch.setenv("JQUANTS_API_KEY", "secret-sentinel-must-not-be-printed")
    monkeypatch.setattr(check_jquants_storage_config, "os", SimpleNamespace(environ={}))

    result = check_jquants_storage_config.main()
    output = capsys.readouterr().out

    assert result == 0
    assert "secret-sentinel-must-not-be-printed" not in output


def test_entrypoint_fails_when_a_destination_is_not_on_d_drive(monkeypatch, capsys):
    settings = make_settings("D:/trade-engine-data/jquants")
    settings.JQUANTS_LEDGER_DATABASE_FILE = Path(
        "C:/trade-engine-data/jquants/ledger.sqlite3"
    )
    monkeypatch.setattr(check_jquants_storage_config, "config", settings, raising=False)
    monkeypatch.setattr(check_jquants_storage_config, "os", SimpleNamespace(environ={}))

    result = check_jquants_storage_config.main()
    output = capsys.readouterr().out

    assert result == 1
    assert '"status": "FAILED"' in output
