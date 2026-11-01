"""Run with python -m src.entrypoints.ingest_jquants_calendar --from ... --to ... --allow-network."""

import argparse
import json
import sys
from datetime import date, datetime, timedelta, timezone
from uuid import uuid4

from src.config import config
from src.infrastructure.market_data.jquants_calendar_store import JQuantsCalendarStore
from src.infrastructure.market_data.jquants_v2_client import JQuantsV2Client
from src.application.jquants_calendar_job import JQuantsCalendarJob, CalendarJobResult
from src.application.jquants_execution_guard import JQuantsExecutionGuard
from src.infrastructure.market_data.jquants_backfill_state import JQuantsBackfillStateStore
from src.infrastructure.market_data.jquants_ops_events import JQuantsOpsEventRepository
from src.infrastructure.notification.jquants_notify import JQuantsSafeNotifier
from src.application.jquants_ops_reporting import JQuantsOpsReporter
from src.domain.jquants_ops_policy import JQuantsOpsEvent, REASONS

JST = timezone(timedelta(hours=9))


def _date_argument(value: str) -> date:
    try:
        parsed = date.fromisoformat(value)
    except ValueError:
        raise argparse.ArgumentTypeError("Date must be a valid YYYY-MM-DD") from None
    if parsed.isoformat() != value:
        raise argparse.ArgumentTypeError("Date must be a valid YYYY-MM-DD")
    return parsed


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="J-Quants business calendar ingestion")
    parser.add_argument("--from", dest="start", type=_date_argument, required=True)
    parser.add_argument("--to", dest="end", type=_date_argument, required=True)
    parser.add_argument("--refresh", action="store_true", help="Overwrite differing stored days")
    parser.add_argument("--allow-network", action="store_true",
                        help="Required to call the real API")
    args = parser.parse_args(argv)
    if not args.allow_network:
        print(json.dumps({"status": "SKIPPED", "reason": "--allow-network is required"}))
        return 2
    try:
        state = JQuantsBackfillStateStore(config.JQUANTS_BACKFILL_STATE_DIRECTORY)
        result = JQuantsCalendarJob(
            JQuantsExecutionGuard(state), JQuantsV2Client,
            lambda: JQuantsCalendarStore(config.JQUANTS_CALENDAR_DIRECTORY),
        ).run(args.start, args.end, refresh=args.refresh)
    except Exception:
        result = CalendarJobResult(1, "FAILED", "STATE_ERROR")
    print(json.dumps(result.as_dict(), sort_keys=True))
    try:
        reporter = _build_reporter()
        reporter.emit(JQuantsOpsEvent(
            uuid4().hex, "CALENDAR", datetime.now(JST).date().isoformat(),
            result.exit_code, result.status,
            result.reason if result.reason in REASONS else "STAGE_FAILED",
            stop_scope=result.stop_scope, target_day=args.end.isoformat(),
        ), notify=result.exit_code in (1, 2))
    except Exception:
        print("JQUANTS_LOCAL_EVENT_FAILED", file=sys.stderr)
        return result.exit_code or 1
    if any(item.reason == "LOCAL_EVENT_FAILED" for item in reporter.events):
        print("JQUANTS_LOCAL_EVENT_FAILED", file=sys.stderr)
    return reporter.exit_code


def _build_reporter():
    return JQuantsOpsReporter(
        JQuantsOpsEventRepository(config.JQUANTS_EXECUTION_METRICS_DIRECTORY),
        JQuantsSafeNotifier(config.SLACK_WEBHOOK_CRITICAL),
    )


if __name__ == "__main__":
    raise SystemExit(main())
