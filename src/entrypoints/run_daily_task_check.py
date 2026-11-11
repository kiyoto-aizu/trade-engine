import logging
import zipfile
from datetime import date, datetime, timedelta
from pathlib import Path

from src.config import config
from src.config.task_schedule import ScheduledTask, tasks_for_date
from src.infrastructure.logging_config import configure_logging
from src.infrastructure.notification.slack_notify import notify_critical, notify_daily

logger = logging.getLogger(__name__)
PROJECT_ROOT = Path(__file__).resolve().parents[2]
LOG_TIMESTAMP_FORMAT = "%Y-%m-%d %H:%M:%S,%f"


def task_run_duration(log_text: str, task_name: str) -> timedelta | None:
    start_marker = f"【{task_name}】開始"
    end_markers = (f"【{task_name}】終了", f"【{task_name}】異常終了")
    started_at: datetime | None = None
    duration: timedelta | None = None

    for line in log_text.splitlines():
        try:
            timestamp = datetime.strptime(line[:23], LOG_TIMESTAMP_FORMAT)
        except ValueError:
            timestamp = None

        if start_marker in line:
            started_at = timestamp
        elif any(marker in line for marker in end_markers):
            duration = (
                timestamp - started_at
                if timestamp is not None and started_at is not None and timestamp >= started_at
                else None
            )
            started_at = None

    return duration


def format_duration(duration: timedelta) -> str:
    total_seconds = int(duration.total_seconds())
    minutes, seconds = divmod(total_seconds, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}時間{minutes}分{seconds}秒"
    if minutes:
        return f"{minutes}分{seconds}秒"
    return f"{seconds}秒"


def classify_task_run(
    task: ScheduledTask,
    target_date: date,
    log_text: str,
    reports_directory: Path,
) -> tuple[str, str]:
    start_marker = f"【{task.name}】開始"
    end_marker = f"【{task.name}】終了"
    error_marker = f"【{task.name}】異常終了"
    is_trading = task.name == "取引"
    has_report = (reports_directory / f"{target_date.isoformat()}.json").is_file()

    if error_marker in log_text:
        return "❌", "異常終了"
    if start_marker not in log_text:
        detail = "ログ記録なし・要確認"
        if is_trading:
            detail += "・" + ("日次レポートあり" if has_report else "日次レポートなし")
        return "⚠️", detail
    if end_marker not in log_text:
        return "⚠️", "開始記録のみ・終了記録なし（要確認）"
    if is_trading and not has_report:
        return "⚠️", "終了記録あり・日次レポートなし（要確認）"
    return "✅", ""


def build_task_check_message(
    target_date: date,
    log_path: Path | None = None,
    reports_directory: Path | None = None,
) -> tuple[str, bool]:
    if log_path is not None:
        try:
            log_text = log_path.read_text(encoding="utf-8", errors="replace")
        except FileNotFoundError:
            log_text = ""
    else:
        month = target_date.strftime("%Y%m")
        name = Path(config.LOG_FILE_PATH).name
        local_directory = Path(config.LOG_DIRECTORY) / month
        log_names = (name, f"{name}.{target_date.isoformat()}")
        archived_logs = Path(config.LOG_ARCHIVE_DIRECTORY) / "application" / f"{month}.zip"
        local_logs = {}
        for log_name in log_names:
            for path in (local_directory / log_name, Path(config.LOG_DIRECTORY) / log_name):
                if path.exists():
                    local_logs[log_name] = path
                    break
        missing_names = set(log_names) - local_logs.keys()
        archived_text = {}
        if missing_names and archived_logs.exists():
            try:
                with zipfile.ZipFile(archived_logs) as archive:
                    names = set(archive.namelist())
                    archived_text = {
                        log_name: archive.read(log_name).decode("utf-8", errors="replace")
                        for log_name in missing_names
                        if log_name in names
                    }
            except (OSError, zipfile.BadZipFile, KeyError) as error:
                raise OSError(f"Could not read archived application log {archived_logs}: {error}") from error
        log_text = "\n".join(
            local_logs[log_name].read_text(encoding="utf-8", errors="replace")
            if log_name in local_logs else archived_text.get(log_name, "")
            for log_name in log_names
        )

    report_dir = reports_directory or PROJECT_ROOT / "data" / "reports" / "daily"
    log_text = "\n".join(
        line for line in log_text.splitlines()
        if line[:10] == target_date.isoformat()
    )

    tasks = tasks_for_date(target_date)
    heading = f"【本日のタスク実行結果】{target_date:%Y-%m-%d}({"月火水木金土日"[target_date.weekday()]})"
    if not tasks:
        return f"{heading}\n本日の実行予定タスクはありません。", False

    lines = [heading]
    needs_attention = False
    for task in tasks:
        status, detail = classify_task_run(task, target_date, log_text, report_dir)
        needs_attention = needs_attention or status != "✅"
        line = f"{status} {task.name} ({task.display_time})"
        if detail:
            line += f" {detail}"
        duration = task_run_duration(log_text, task.name)
        if duration is not None:
            line += f" 実績所要時間: {format_duration(duration)}"
        lines.append(line)
    return "\n".join(lines), needs_attention


def main(
    target_date: date | None = None,
    log_path: Path | None = None,
    reports_directory: Path | None = None,
) -> None:
    configure_logging()
    effective_date = target_date or date.today()
    message, needs_attention = build_task_check_message(
        effective_date, log_path=log_path, reports_directory=reports_directory,
    )
    logger.info("本日のタスク実行結果をSlackへ通知します。")
    notify_daily(message)
    if needs_attention:
        notify_critical(f"【本日のタスク実行要確認】\n{message}")


if __name__ == "__main__":
    main()