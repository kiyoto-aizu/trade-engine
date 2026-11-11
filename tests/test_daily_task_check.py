from datetime import date
import struct
import zipfile

from src.config.task_schedule import ScheduledTask
from src.entrypoints import run_daily_task_check
from src.entrypoints.run_daily_task_check import build_task_check_message, classify_task_run


def test_classifies_normal_and_abnormal_task_markers(tmp_path):
    task = ScheduledTask("フィルタリング", "09:30", "weekday")
    normal_log = "【フィルタリング】開始: 契機=スクリーニング結果\n【フィルタリング】終了"
    failed_log = normal_log + "\n【フィルタリング】異常終了"

    assert classify_task_run(task, date(2026, 9, 28), normal_log, tmp_path) == ("✅", "")
    assert classify_task_run(task, date(2026, 9, 28), failed_log, tmp_path) == ("❌", "異常終了")


def test_missing_and_incomplete_task_logs_require_attention(tmp_path):
    task = ScheduledTask("バックテスト", "08:00", "saturday")

    assert classify_task_run(task, date(2026, 9, 26), "", tmp_path) == (
        "⚠️", "ログ記録なし・要確認",
    )
    assert classify_task_run(task, date(2026, 9, 26), "【バックテスト】開始", tmp_path) == (
        "⚠️", "開始記録のみ・終了記録なし（要確認）",
    )


def test_trading_requires_daily_report_in_addition_to_log_markers(tmp_path):
    trading = ScheduledTask("取引", "09:35-15:30", "weekday")
    log_text = "【取引】開始（ペーパー）\n【取引】終了"
    target_date = date(2026, 9, 28)

    assert classify_task_run(trading, target_date, log_text, tmp_path) == (
        "⚠️", "終了記録あり・日次レポートなし（要確認）",
    )
    (tmp_path / "2026-09-28.json").write_text("{}", encoding="utf-8")
    assert classify_task_run(trading, target_date, log_text, tmp_path) == ("✅", "")


def test_build_task_check_reads_the_given_log_file_and_flags_missing_runs(tmp_path):
    log_path = tmp_path / "trade_project.log"
    log_path.write_text("", encoding="utf-8")

    message, needs_attention = build_task_check_message(
        date(2026, 9, 28), log_path=log_path, reports_directory=tmp_path / "reports",
    )

    assert message.startswith("【本日のタスク実行結果】2026-09-28(月)")
    assert "⚠️ フィルタリング (09:30) ログ記録なし・要確認" in message
    assert needs_attention is True


def test_build_task_check_ignores_previous_day_markers(tmp_path):
    log_path = tmp_path / "trade_project.log"
    log_path.write_text(
        "2026-09-25 09:30:00 INFO: 【フィルタリング】開始\n"
        "2026-09-25 09:31:00 INFO: 【フィルタリング】終了\n",
        encoding="utf-8",
    )

    message, needs_attention = build_task_check_message(
        date(2026, 9, 28), log_path=log_path, reports_directory=tmp_path / "reports",
    )

    assert "⚠️ フィルタリング (09:30) ログ記録なし・要確認" in message
    assert needs_attention is True


def test_build_task_check_fills_missing_local_logs_from_archive(monkeypatch, tmp_path):
    log_root = tmp_path / "application"
    local_directory = log_root / "202609"
    local_directory.mkdir(parents=True)
    local_log = local_directory / "trade_project.log"
    local_log.write_text(
        "2026-09-28 09:31:00,000 INFO: 【フィルタリング】終了\n",
        encoding="utf-8",
    )
    legacy_log = log_root / "trade_project.log.2026-09-28"
    legacy_log.write_text(
        "2026-09-28 09:30:00,000 INFO: 【フィルタリング】開始\n",
        encoding="utf-8",
    )
    archive_path = tmp_path / "D" / "logs" / "application" / "202609.zip"
    archive_path.parent.mkdir(parents=True)
    with zipfile.ZipFile(archive_path, "w") as archive:
        archive.writestr(
            "trade_project.log.2026-09-28",
            "2026-09-28 09:30:00,000 INFO: 【フィルタリング】異常終了\n",
        )
    monkeypatch.setattr(run_daily_task_check.config, "LOG_DIRECTORY", log_root)
    monkeypatch.setattr(run_daily_task_check.config, "LOG_FILE_PATH", local_log)
    monkeypatch.setattr(
        run_daily_task_check.config,
        "LOG_ARCHIVE_DIRECTORY",
        archive_path.parent.parent,
    )

    message, _ = build_task_check_message(date(2026, 9, 28))

    assert "✅ フィルタリング (09:30)" in message


def test_build_task_check_notifies_when_archive_cannot_be_read(monkeypatch, tmp_path):
    log_root = tmp_path / "application"
    local_directory = log_root / "202609"
    local_directory.mkdir(parents=True)
    (local_directory / "trade_project.log").write_text(
        "2026-09-28 09:31:00,000 INFO: 【フィルタリング】終了\n",
        encoding="utf-8",
    )
    archive_path = tmp_path / "D" / "logs" / "application" / "202609.zip"
    archive_path.parent.mkdir(parents=True)
    archive_path.write_bytes(b"not a zip archive")
    monkeypatch.setattr(run_daily_task_check.config, "LOG_DIRECTORY", log_root)
    monkeypatch.setattr(run_daily_task_check.config, "LOG_FILE_PATH", "trade_project.log")
    monkeypatch.setattr(
        run_daily_task_check.config,
        "LOG_ARCHIVE_DIRECTORY",
        archive_path.parent.parent,
    )

    message, needs_attention = build_task_check_message(date(2026, 9, 28))

    assert "アーカイブログを読み込めず" in message
    assert needs_attention is True


def test_build_task_check_notifies_when_archive_member_is_corrupt(monkeypatch, tmp_path):
    log_root = tmp_path / "application"
    local_directory = log_root / "202609"
    local_directory.mkdir(parents=True)
    (local_directory / "trade_project.log").write_text(
        "2026-09-28 09:31:00,000 INFO: 【フィルタリング】終了\n",
        encoding="utf-8",
    )
    archive_path = tmp_path / "D" / "logs" / "application" / "202609.zip"
    archive_path.parent.mkdir(parents=True)
    with zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(
            "trade_project.log.2026-09-28",
            "2026-09-28 09:30:00,000 INFO: 【フィルタリング】開始\n" + "x" * 512,
        )
    archive_bytes = bytearray(archive_path.read_bytes())
    filename_length, extra_length = struct.unpack_from("<HH", archive_bytes, 26)
    archive_bytes[30 + filename_length + extra_length] = 0xFF
    archive_path.write_bytes(archive_bytes)
    monkeypatch.setattr(run_daily_task_check.config, "LOG_DIRECTORY", log_root)
    monkeypatch.setattr(run_daily_task_check.config, "LOG_FILE_PATH", "trade_project.log")
    monkeypatch.setattr(
        run_daily_task_check.config,
        "LOG_ARCHIVE_DIRECTORY",
        archive_path.parent.parent,
    )

    message, needs_attention = build_task_check_message(date(2026, 9, 28))

    assert "アーカイブログを読み込めず" in message
    assert needs_attention is True


def test_build_task_check_notifies_when_archive_member_is_encrypted(monkeypatch, tmp_path):
    log_root = tmp_path / "application"
    local_directory = log_root / "202609"
    local_directory.mkdir(parents=True)
    (local_directory / "trade_project.log").write_text(
        "2026-09-28 09:31:00,000 INFO: 【フィルタリング】終了\n",
        encoding="utf-8",
    )
    archive_path = tmp_path / "D" / "logs" / "application" / "202609.zip"
    archive_path.parent.mkdir(parents=True)
    with zipfile.ZipFile(archive_path, "w") as archive:
        archive.writestr(
            "trade_project.log.2026-09-28",
            "2026-09-28 09:30:00,000 INFO: 【フィルタリング】開始\n",
        )
    archive_bytes = bytearray(archive_path.read_bytes())
    local_flags = struct.unpack_from("<H", archive_bytes, 6)[0]
    struct.pack_into("<H", archive_bytes, 6, local_flags | 1)
    central_header = archive_bytes.index(b"PK\x01\x02")
    central_flags = struct.unpack_from("<H", archive_bytes, central_header + 8)[0]
    struct.pack_into("<H", archive_bytes, central_header + 8, central_flags | 1)
    archive_path.write_bytes(archive_bytes)
    monkeypatch.setattr(run_daily_task_check.config, "LOG_DIRECTORY", log_root)
    monkeypatch.setattr(run_daily_task_check.config, "LOG_FILE_PATH", "trade_project.log")
    monkeypatch.setattr(
        run_daily_task_check.config,
        "LOG_ARCHIVE_DIRECTORY",
        archive_path.parent.parent,
    )

    message, needs_attention = build_task_check_message(date(2026, 9, 28))

    assert "アーカイブログを読み込めず" in message
    assert needs_attention is True


def test_build_task_check_reports_measured_duration(tmp_path):
    log_path = tmp_path / "trade_project.log"
    log_path.write_text(
        "2026-09-28 09:30:00,000 INFO: 【フィルタリング】開始\n"
        "2026-09-28 09:31:23,456 INFO: 【フィルタリング】終了\n",
        encoding="utf-8",
    )

    message, _ = build_task_check_message(
        date(2026, 9, 28), log_path=log_path, reports_directory=tmp_path / "reports",
    )

    assert "✅ フィルタリング (09:30) 実績所要時間: 1分23秒" in message


def test_main_sends_attention_summary_to_daily_and_critical(monkeypatch, tmp_path):
    daily_messages = []
    critical_messages = []
    call_order = []
    monkeypatch.setattr(run_daily_task_check, "configure_logging", lambda: call_order.append("logging"))

    def fail_archive(*args):
        call_order.append("archive")
        raise OSError("archive unavailable")

    monkeypatch.setattr(
        run_daily_task_check,
        "prepare_monthly_log_storage",
        fail_archive,
    )
    monkeypatch.setattr(run_daily_task_check, "notify_daily", daily_messages.append)
    monkeypatch.setattr(run_daily_task_check, "notify_critical", critical_messages.append)
    log_path = tmp_path / "trade_project.log"
    log_path.write_text("", encoding="utf-8")

    run_daily_task_check.main(
        date(2026, 9, 28), log_path=log_path, reports_directory=tmp_path / "reports",
    )

    assert len(daily_messages) == 1
    assert len(critical_messages) == 1
    assert critical_messages[0].startswith("【本日のタスク実行要確認】")
    assert call_order == ["logging", "archive"]