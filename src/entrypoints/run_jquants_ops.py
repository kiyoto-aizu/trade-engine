"""Explicit operator execution; the default only prints a side-effect-free plan."""

import argparse
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone
import io
import json
import logging
import os


class _SafeParser(argparse.ArgumentParser):
    def error(self, message):
        raise ValueError("INVALID_ARGUMENTS")


def _preflight():
    from src.jquants_storage import default_jquants_storage_settings, inspect_jquants_storage
    return inspect_jquants_storage(default_jquants_storage_settings(), os.environ).is_valid


def _build_ops():
    environment = dict(os.environ)
    from src.config import config
    from src.jquants_storage import default_jquants_storage_settings, inspect_jquants_storage
    expected = inspect_jquants_storage(default_jquants_storage_settings(), environment)
    effective = inspect_jquants_storage(config, {})
    if [item.path for item in expected.destinations] != [item.path for item in effective.destinations]:
        raise ValueError("effective storage differs from preflight")
    from src.application.jquants_backfill_planner import JQuantsBackfillPlanner
    from src.application.jquants_backfill_usecase import JQuantsBackfillUseCase
    from src.application.jquants_calendar_job import JQuantsCalendarJob
    from src.application.jquants_daily_job import JQuantsDailyJob
    from src.application.jquants_execution_guard import JQuantsExecutionGuard
    from src.application.jquants_metrics_evaluation import evaluate_summary
    from src.application.jquants_ops_orchestration import JQuantsOpsOrchestration
    from src.application.jquants_ops_reporting import JQuantsOpsReporter, JQuantsReportedIngestion
    from src.application.jquants_single_day_ingestion import JQuantsSingleDayIngestion
    from src.application.jquants_daily_disk_check import JQuantsDailyDiskCheck
    from src.infrastructure.market_data.jquants_backfill_state import JQuantsBackfillStateStore
    from src.infrastructure.market_data.jquants_calendar_store import JQuantsCalendarStore
    from src.infrastructure.market_data.jquants_canonical_store import JQuantsCanonicalStore
    from src.infrastructure.market_data.jquants_ops_events import JQuantsOpsEventRepository
    from src.infrastructure.market_data.jquants_raw_ledger_state import RawStoreLedgerState
    from src.infrastructure.market_data.jquants_raw_store import JQuantsRawStore
    from src.infrastructure.market_data.jquants_v2_client import JQuantsV2Client
    from src.infrastructure.notification.jquants_notify import (
        JQuantsOpsSummaryNotifier, JQuantsSafeNotifier,
    )

    state = JQuantsBackfillStateStore(config.JQUANTS_BACKFILL_STATE_DIRECTORY)
    guard = JQuantsExecutionGuard(state)
    reporter = JQuantsOpsReporter(
        JQuantsOpsEventRepository(config.JQUANTS_EXECUTION_METRICS_DIRECTORY),
        JQuantsSafeNotifier(config.SLACK_WEBHOOK_CRITICAL),
    )
    holder = {}

    def ingestion():
        return JQuantsReportedIngestion(JQuantsSingleDayIngestion(
            JQuantsRawStore(), JQuantsCanonicalStore(config.JQUANTS_CANONICAL_DIRECTORY),
            metrics_directory=config.JQUANTS_EXECUTION_METRICS_DIRECTORY,
        ), reporter, lambda: holder["ops"])

    def daily():
        return JQuantsDailyJob(
            JQuantsCalendarStore(config.JQUANTS_CALENDAR_DIRECTORY), ingestion,
            None, evaluate_summary, guard=guard,
            disk_check=JQuantsDailyDiskCheck(
                config.JQUANTS_RAW_DIRECTORY, config.JQUANTS_CANONICAL_DIRECTORY,
            ),
        )

    def backfill():
        scoped = ingestion()
        raw = scoped.ingestion.raw_store
        canonical = scoped.ingestion.canonical_store
        return JQuantsBackfillUseCase(
            JQuantsBackfillPlanner(
                JQuantsCalendarStore(config.JQUANTS_CALENDAR_DIRECTORY), RawStoreLedgerState(raw),
            ), scoped, state, raw_directory=raw.raw_directory,
            canonical_directory=canonical.directory,
        )

    ops = JQuantsOpsOrchestration(
        guard,
        lambda: JQuantsCalendarJob(
            guard, JQuantsV2Client,
            lambda: JQuantsCalendarStore(config.JQUANTS_CALENDAR_DIRECTORY),
        ),
        daily, backfill, reporter,
        summary_notifier=JQuantsOpsSummaryNotifier(config.SLACK_WEBHOOK_DAILY),
    )
    holder["ops"] = ops
    return ops


def main(argv=None):
    parser = _SafeParser(description="J-Quants audited operations (registration stays disabled)")
    parser.add_argument("--execute", action="store_true", help="Explicitly authorize real acquisition")
    parser.add_argument("--scheduled", action="store_true", help="Track scheduled lock skips")
    try:
        args = parser.parse_args(argv)
    except ValueError:
        print(json.dumps({"stage": "OPS", "status": "FAILED", "reason": "INVALID_ARGUMENTS", "exit_code": 2}))
        return 2
    if args.scheduled and not args.execute:
        print(json.dumps({"stage": "OPS", "status": "SKIPPED", "reason": "EXECUTE_REQUIRED", "exit_code": 2}))
        return 2
    if not args.execute:
        print(json.dumps({
            "stage": "OPS", "status": "PLAN_ONLY", "exit_code": 0,
            "stages": ["AUDIT", "CALENDAR", "DAILY", "BACKFILL"],
            "operational_go": "NOT_GRANTED",
        }, sort_keys=True))
        return 0
    disabled = logging.root.manager.disable
    logging.disable(logging.CRITICAL)
    try:
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            if not _preflight():
                result = {"stage": "AUDIT", "status": "FAILED", "reason": "AUDIT_FAILED", "exit_code": 1}
            else:
                ops = _build_ops()
                now = datetime.now(timezone(timedelta(hours=9)))
                ops.event("AUDIT", now, 0, "COMPLETE", "PASS", scheduled=args.scheduled)
                result = ops.run(now, scheduled=args.scheduled)
    except (Exception, SystemExit):
        result = {"stage": "OPS", "status": "FAILED", "reason": "INITIALIZATION_ERROR", "exit_code": 1}
    finally:
        logging.disable(disabled)
    print(json.dumps(result, sort_keys=True))
    if result.get("reason") in ("AUDIT_FAILED", "INITIALIZATION_ERROR"):
        import sys
        print("JQUANTS_LOCAL_EVENT_UNAVAILABLE", file=sys.stderr)
    if any(event.get("reason") == "LOCAL_EVENT_FAILED" for event in result.get("events", [])):
        import sys
        print("JQUANTS_LOCAL_EVENT_FAILED", file=sys.stderr)
    if result.get("summary_notification_failed"):
        import sys
        print("JQUANTS_DAILY_SUMMARY_NOTIFY_FAILED", file=sys.stderr)
    return result["exit_code"]


if __name__ == "__main__":
    raise SystemExit(main())
