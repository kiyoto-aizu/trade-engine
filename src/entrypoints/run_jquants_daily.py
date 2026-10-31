"""Run with python -m src.entrypoints.run_jquants_daily [--date YYYY-MM-DD] [--publish-after HH:MM]

営業日のみ、日足 → 分足の順に当日分を取り込む。出力は日付・状態コードのみ。
終了コード: 0=正常、2=要確認（ATTENTION・ロックによるスキップ）、1=失敗・停止、3=429 打ち切り。
"""

import argparse
import json
import sys
from datetime import date, datetime, time, timedelta, timezone
from typing import Any
from uuid import uuid4

from src.application.jquants_daily_job import DailyJobResult, JQuantsDailyJob
from src.application.jquants_execution_guard import JQuantsExecutionGuard
from src.application.jquants_metrics_evaluation import evaluate_summary
from src.application.jquants_daily_disk_check import JQuantsDailyDiskCheck
from src.application.jquants_single_day_ingestion import JQuantsSingleDayIngestion
from src.config import config
from src.infrastructure.market_data.jquants_calendar_store import JQuantsCalendarStore
from src.infrastructure.market_data.jquants_backfill_state import JQuantsBackfillStateStore
from src.infrastructure.market_data.jquants_canonical_store import JQuantsCanonicalStore
from src.infrastructure.market_data.jquants_raw_store import JQuantsRawStore
from src.infrastructure.notification.jquants_notify import JQuantsSafeNotifier, notify_critical
from src.infrastructure.market_data.jquants_ops_events import JQuantsOpsEventRepository
from src.application.jquants_ops_reporting import JQuantsOpsReporter
from src.domain.jquants_ops_policy import JQuantsOpsEvent


DEFAULT_PUBLISH_AFTER = time(17, 0)
JST = timezone(timedelta(hours=9))


def _date_argument(value: str) -> date:
    try:
        parsed = date.fromisoformat(value)
    except ValueError:
        raise argparse.ArgumentTypeError("Date must be a valid YYYY-MM-DD") from None
    if parsed.isoformat() != value:
        raise argparse.ArgumentTypeError("Date must be a valid YYYY-MM-DD")
    return parsed


def _time_argument(value: str) -> time:
    try:
        return time.fromisoformat(value)
    except ValueError:
        raise argparse.ArgumentTypeError("Time must be a valid HH:MM") from None


def _verify(summary: Any):
    return evaluate_summary(summary)


def _ingestion() -> JQuantsSingleDayIngestion:
    return JQuantsSingleDayIngestion(
        JQuantsRawStore(), JQuantsCanonicalStore(config.JQUANTS_CANONICAL_DIRECTORY),
        metrics_directory=config.JQUANTS_EXECUTION_METRICS_DIRECTORY,
        notifier=notify_critical,
    )


def build_job() -> JQuantsDailyJob:
    return JQuantsDailyJob(
        JQuantsCalendarStore(config.JQUANTS_CALENDAR_DIRECTORY), _ingestion,
        None, _verify,
        guard=JQuantsExecutionGuard(
            JQuantsBackfillStateStore(config.JQUANTS_BACKFILL_STATE_DIRECTORY),
        ),
        disk_check=JQuantsDailyDiskCheck(
            config.JQUANTS_RAW_DIRECTORY, config.JQUANTS_CANONICAL_DIRECTORY,
        ),
    )


def _build_reporter():
    return JQuantsOpsReporter(
        JQuantsOpsEventRepository(config.JQUANTS_EXECUTION_METRICS_DIRECTORY),
        JQuantsSafeNotifier(config.SLACK_WEBHOOK_CRITICAL),
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="J-Quants daily update job")
    parser.add_argument("--date", type=_date_argument, default=None, help="Run date (default: today)")
    parser.add_argument(
        "--publish-after", type=_time_argument, default=DEFAULT_PUBLISH_AFTER,
        help="Business day before this time is reported as NOT_PUBLISHED and skipped",
    )
    args = parser.parse_args(argv)
    now = datetime.now(JST)
    run_date = args.date or now.date()
    try:
        result: DailyJobResult = build_job().run(run_date, now, args.publish_after)
    except Exception:
        print(json.dumps({"run_date": run_date.isoformat(), "status": "FAILED",
                          "error_category": "INITIALIZATION_ERROR"}))
        try:
            reporter = _build_reporter()
            reporter.emit(JQuantsOpsEvent(
                uuid4().hex, "DAILY", now.date().isoformat(), 1, "FAILED", "INITIALIZATION_ERROR",
            ), notify=True)
            if any(item.reason == "LOCAL_EVENT_FAILED" for item in reporter.events):
                print("JQUANTS_LOCAL_EVENT_FAILED", file=sys.stderr)
        except Exception:
            print("JQUANTS_LOCAL_EVENT_FAILED", file=sys.stderr)
        return 1
    print(json.dumps(result.as_dict(), sort_keys=True))
    try:
        reporter = _build_reporter()
        reporter.daily(result, uuid4().hex, now.date())
    except Exception:
        print("JQUANTS_LOCAL_EVENT_FAILED", file=sys.stderr)
        return result.exit_code or 1
    if any(item.reason == "LOCAL_EVENT_FAILED" for item in reporter.events):
        print("JQUANTS_LOCAL_EVENT_FAILED", file=sys.stderr)
    if result.halt_flag_required:
        print("ALERT: halt required (401/403). Investigate before the next run.", file=sys.stderr)
    return reporter.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
