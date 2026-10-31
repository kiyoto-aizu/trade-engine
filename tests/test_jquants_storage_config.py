import os
import json
import ast
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

from src.jquants_storage import (
    default_jquants_storage_settings,
    inspect_jquants_storage,
)
from src.entrypoints import check_jquants_storage_config
from src import jquants_storage


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


def explicit_environment(settings):
    return {key: os.fspath(value) for key, value in vars(settings).items()}


def test_storage_audit_accepts_normalized_d_drive_paths():
    settings = make_settings("D:/trade-engine-data/jquants")

    audit = inspect_jquants_storage(settings, explicit_environment(settings))

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

    audit = inspect_jquants_storage(settings, explicit_environment(settings))

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

    with monkeypatch.context() as patch:
        patch.setattr(Path, "mkdir", fail_if_accessed)
        patch.setattr(Path, "open", fail_if_accessed)
        patch.setattr(os, "makedirs", fail_if_accessed)
        audit = inspect_jquants_storage(settings, explicit_environment(settings))

    assert audit.is_valid


def test_entrypoint_prints_audit_without_reading_api_key(monkeypatch, capsys):
    for key, value in explicit_environment(
        make_settings("D:/trade-engine-data/jquants")
    ).items():
        monkeypatch.setenv(key, value)
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
    for key, value in explicit_environment(settings).items():
        monkeypatch.setenv(key, value)
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

    audit = inspect_jquants_storage(settings, explicit_environment(settings))

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
    monkeypatch.setattr(
        jquants_storage, "os",
        SimpleNamespace(
            name=os.name, fspath=os.fspath,
            path=SimpleNamespace(
                isabs=os.path.isabs,
                abspath=lambda _: r"D:\runtime\data\jquants\raw",
            ),
        ),
    )

    audit = inspect_jquants_storage(settings, {})

    raw = next(item for item in audit.destinations if item.name == "raw")
    assert raw.drive == "D:"
    assert raw.path == r"D:\runtime\data\jquants\raw"
    assert not raw.is_absolute
    assert not raw.matches_operational_drive


def test_storage_audit_rejects_root_relative_windows_path(monkeypatch):
    settings = make_settings("D:/trade-engine-data/jquants")
    settings.JQUANTS_RAW_DIRECTORY = Path(r"\runtime\data\jquants\raw")
    monkeypatch.setattr(
        jquants_storage, "os",
        SimpleNamespace(name="nt", fspath=os.fspath, path=os.path),
    )

    audit = inspect_jquants_storage(settings, {})

    raw = next(item for item in audit.destinations if item.name == "raw")
    assert raw.drive == ""
    assert not raw.is_absolute
    assert not raw.matches_operational_drive


def test_storage_audit_normalizes_windows_paths_without_filesystem_resolution(
    monkeypatch,
):
    settings = make_settings("d:/trade-engine-data/jquants")
    def unexpected_abspath(*args):
        raise AssertionError("absolute Windows paths must not need abspath")

    monkeypatch.setattr(
        jquants_storage, "os",
        SimpleNamespace(
            name="nt", fspath=os.fspath,
            path=SimpleNamespace(abspath=unexpected_abspath),
        ),
    )

    audit = inspect_jquants_storage(settings, explicit_environment(settings))

    assert audit.is_valid
    assert all(item.path.startswith("d:\\") for item in audit.destinations)


def test_windows_simulation_leaves_process_state_unchanged(monkeypatch, tmp_path):
    original_name = os.name
    original_abspath = os.path.abspath
    settings = make_settings("D:/trade-engine-data/jquants")
    with monkeypatch.context() as patch:
        patch.setattr(
            jquants_storage, "os",
            SimpleNamespace(name="nt", fspath=os.fspath, path=os.path),
        )
        assert inspect_jquants_storage(settings, explicit_environment(settings)).is_valid
        assert os.name == original_name
        assert os.path.abspath is original_abspath
        assert Path(tmp_path).is_dir()
    assert jquants_storage.os is os
    assert os.name == original_name
    assert os.path.abspath is original_abspath


def test_defaults_on_d_drive_never_pass_without_all_explicit_settings():
    settings = make_settings("D:/repository/data/jquants")
    environment = explicit_environment(settings)
    for key in environment:
        incomplete = {name: value for name, value in environment.items() if name != key}
        audit = inspect_jquants_storage(settings, incomplete)
        assert not audit.is_valid
        assert next(item for item in audit.destinations if item.setting == key).source == "default"
    assert not inspect_jquants_storage(settings, {}).is_valid
    assert inspect_jquants_storage(settings, environment).is_valid


def test_audit_defaults_match_config_definitions_without_importing_config():
    root = Path(__file__).resolve().parents[1]
    tree = ast.parse((root / "src" / "config" / "config.py").read_text(encoding="utf-8"))
    defaults = default_jquants_storage_settings()
    definitions = {
        node.targets[0].id: node.value
        for node in tree.body
        if isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Name)
    }
    for key in vars(defaults):
        definition = definitions[key]
        getenv_call = definition.args[0]
        relative = getattr(defaults, key).relative_to(jquants_storage._REPOSITORY_ROOT)
        expression = "_repo_root" + "".join(f" / {part!r}" for part in relative.parts)
        expected = ast.parse(f"str({expression})", mode="eval").body
        assert ast.dump(getenv_call.args[1]) == ast.dump(expected)


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
try:
    runpy.run_module("src.entrypoints.check_jquants_storage_config", run_name="__main__")
except SystemExit as result:
    assert result.code == 0
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
