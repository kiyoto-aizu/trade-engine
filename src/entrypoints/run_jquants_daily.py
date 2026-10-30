"""Run with python -m src.entrypoints.run_jquants_daily [--date YYYY-MM-DD] [--publish-after HH:MM]

営業日のみ、日足 → 分足の順に当日分を取り込む。出力は日付・状態コードのみ。
終了コード: 0=正常、2=要確認（ATTENTION・ロックによるスキップ）、1=失敗・停止、3=429 打ち切り。
"""

import argparse
import json
import sqlite3
import sys
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any

from src.application.jquants_daily_job import DailyJobResult, JQuantsDailyJob
from src.application.jquants_metrics_evaluation import evaluate_metrics
from src.application.jquants_single_day_ingestion import JQuantsSingleDayIngestion
from src.config import config
from src.infrastructure.market_data.jquants_calendar_store import JQuantsCalendarStore
from src.infrastructure.market_data.jquants_canonical_store import JQuantsCanonicalStore
from src.infrastructure.market_data.jquants_metrics_failure_history import (
    JQuantsMetricsFailureHistory,
)
from src.infrastructure.market_data.jquants_raw_store import JQuantsRawStore, JQuantsRawStoreError
from src.infrastructure.notification.slack_notify import notify_critical


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


def _verify(summary: Any) -> tuple[str, tuple[str, ...]]:
    path = summary.metrics_file
    try:
        metrics = json.loads(Path(path).read_text(encoding="utf-8")) if path else None
    except (OSError, ValueError):
        metrics = None
    if not isinstance(metrics, dict):
        return "No-Go", ("METRICS_UNREADABLE",)
    evaluation = evaluate_metrics(metrics)
    return evaluation.verdict, evaluation.reasons


def _ingestion() -> JQuantsSingleDayIngestion:
    return JQuantsSingleDayIngestion(
        JQuantsRawStore(), JQuantsCanonicalStore(config.JQUANTS_CANONICAL_DIRECTORY),
        metrics_directory=config.JQUANTS_EXECUTION_METRICS_DIRECTORY,
        notifier=notify_critical,
    )


def build_job() -> JQuantsDailyJob:
    return JQuantsDailyJob(
        JQuantsCalendarStore(config.JQUANTS_CALENDAR_DIRECTORY), _ingestion,
        JQuantsMetricsFailureHistory(config.JQUANTS_EXECUTION_METRICS_DIRECTORY), _verify,
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
        result: DailyJobResult =         build_job().run(run_date, now, args.publish_after)
    except (JQuantsRawStoreError, OSError, ValueError, sqlite3.Error):
        print(json.dumps({"run_date": run_date.isoformat(), "status": "FAILED",
                          "error_category": "INITIALIZATION_ERROR"}))
        return 1
    print(json.dumps(result.as_dict(), sort_keys=True))
    if result.halt_flag_required:
        print("ALERT: halt required (401/403). Investigate before the next run.", file=sys.stderr)
    return result.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
