from datetime import date
import zipfile

import pytest

from src.infrastructure.monthly_log_storage import (
    main,
    prepare_monthly_log_storage,
    preview_monthly_log_storage,
)


def test_new_month_migrates_flat_logs_and_archives_two_months_old_logs(tmp_path):
    logs_root = tmp_path / "data" / "logs"
    archive_root = tmp_path / "D" / "logs"
    jobs = logs_root / "jobs"
    jobs.mkdir(parents=True)
    (jobs / "run_trading_stderr_2026-10-14.log").write_bytes(b"job log")
    (jobs / "run_filtering_stderr_2026-11-06.log").write_bytes(b"recent job log")

    summaries = prepare_monthly_log_storage(
        logs_root,
        archive_root,
        current_date=date(2026, 12, 1),
    )

    archive_path = archive_root / "jobs" / "202610.zip"
    with zipfile.ZipFile(archive_path) as archive:
        assert archive.read("run_trading_stderr_2026-10-14.log") == b"job log"
    assert not (jobs / "202610").exists()
    assert not (jobs / "run_trading_stderr_2026-10-14.log").exists()
    assert (jobs / "202611" / "run_filtering_stderr_2026-11-06.log").read_bytes() == b"recent job log"
    assert (jobs / "202612").is_dir()
    assert any("202610.zip" in summary for summary in summaries)


def test_existing_archive_mismatch_keeps_local_logs(tmp_path):
    logs_root = tmp_path / "data" / "logs"
    archive_root = tmp_path / "D" / "logs"
    month_directory = logs_root / "application" / "202610"
    month_directory.mkdir(parents=True)
    local_log = month_directory / "trade_project.log"
    local_log.write_bytes(b"original log")
    archive_path = archive_root / "application" / "202610.zip"
    archive_path.parent.mkdir(parents=True)
    with zipfile.ZipFile(archive_path, "w") as archive:
        archive.writestr("trade_project.log", b"different log")

    with pytest.raises(FileExistsError, match="local logs retained"):
        prepare_monthly_log_storage(
            logs_root,
            archive_root,
            current_date=date(2026, 12, 1),
            categories=("application",),
        )

    assert local_log.read_bytes() == b"original log"


def test_preview_lists_months_to_archive_without_changing_files(tmp_path):
    logs_root = tmp_path / "data" / "logs"
    old_month = logs_root / "tests" / "202610"
    old_month.mkdir(parents=True)
    old_log = old_month / "pytest.log.2026-10-20"
    old_log.write_bytes(b"test log")

    entries = preview_monthly_log_storage(logs_root, current_date=date(2026, 12, 1))

    assert entries == [
        ("tests", "202610", "archive and remove local month", 1, len(b"test log")),
    ]
    assert old_log.read_bytes() == b"test log"
    assert old_month.is_dir()


def test_preview_groups_old_legacy_logs_as_archive_candidates(tmp_path):
    logs_root = tmp_path / "data" / "logs"
    jobs = logs_root / "jobs"
    old_month = jobs / "202610"
    old_month.mkdir(parents=True)
    (old_month / "existing.log").write_bytes(b"existing")
    (jobs / "run_trading_stderr_2026-10-14.log").write_bytes(b"legacy")

    entries = preview_monthly_log_storage(logs_root, current_date=date(2026, 12, 1))

    assert entries == [
        ("jobs", "202610", "archive and remove local month", 2, len(b"existing") + len(b"legacy")),
    ]


def test_cli_defaults_to_preview_without_modifying_logs(tmp_path, capsys):
    logs_root = tmp_path / "data" / "logs"
    old_month = logs_root / "jobs" / "202610"
    old_month.mkdir(parents=True)
    old_log = old_month / "run.log"
    old_log.write_bytes(b"keep until apply")
    archive_root = tmp_path / "D" / "logs"

    result = main([
        "--logs-root", str(logs_root),
        "--archive-root", str(archive_root),
        "--current-month", "202612",
    ])

    assert result == 0
    assert "Preview only" in capsys.readouterr().out
    assert old_log.read_bytes() == b"keep until apply"
    assert not archive_root.exists()


def test_cli_applies_archive_only_when_requested(tmp_path):
    logs_root = tmp_path / "data" / "logs"
    old_month = logs_root / "jobs" / "202610"
    old_month.mkdir(parents=True)
    old_log = old_month / "run.log"
    old_log.write_bytes(b"archive this")
    archive_root = tmp_path / "D" / "logs"

    result = main([
        "--logs-root", str(logs_root),
        "--archive-root", str(archive_root),
        "--current-month", "202612",
        "--apply",
    ])

    assert result == 0
    with zipfile.ZipFile(archive_root / "jobs" / "202610.zip") as archive:
        assert archive.read("run.log") == b"archive this"
    assert not old_month.exists()


def test_january_rotation_keeps_december_and_archives_november(tmp_path):
    logs_root = tmp_path / "data" / "logs"
    archive_root = tmp_path / "D" / "logs"
    old_month = logs_root / "jobs" / "202611"
    previous_month = logs_root / "jobs" / "202612"
    old_month.mkdir(parents=True)
    previous_month.mkdir()
    (old_month / "run_backtest_stderr_2026-11-28.log").write_bytes(b"November")
    (previous_month / "run_backtest_stderr_2026-12-28.log").write_bytes(b"December")

    prepare_monthly_log_storage(
        logs_root,
        archive_root,
        current_date=date(2027, 1, 1),
        categories=("jobs",),
    )

    assert (archive_root / "jobs" / "202611.zip").is_file()
    assert not old_month.exists()
    assert (previous_month / "run_backtest_stderr_2026-12-28.log").is_file()
    assert (logs_root / "jobs" / "202701").is_dir()
