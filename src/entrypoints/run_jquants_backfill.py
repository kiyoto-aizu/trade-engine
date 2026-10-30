"""Run with python -m src.entrypoints.run_jquants_backfill --dataset minute [--execute].

Without --execute nothing is fetched: only the plan counts and stop state are printed.
--execute calls the real J-Quants API; use it only with explicit approval.
"""

import argparse
import json
import sqlite3
import sys
from datetime import date, timedelta

from src.application.jquants_backfill_planner import JQuantsBackfillPlanner
from src.application.jquants_backfill_usecase import JQuantsBackfillUseCase, jst_today
from src.application.jquants_single_day_ingestion import JQuantsSingleDayIngestion
from src.config import config
from src.domain.jquants_business_calendar import BusinessCalendarError
from src.domain.jquants_backfill_policy import DAILY_BUDGET_SECONDS
from src.domain.jquants_consumer_contract import ConsumerContractError
from src.domain.jquants_contract_range import calculate_earliest_available_date
from src.infrastructure.market_data.jquants_backfill_state import (
    BackfillStateError,
    JQuantsBackfillStateStore,
)
from src.infrastructure.market_data.jquants_calendar_store import JQuantsCalendarStore
from src.infrastructure.market_data.jquants_canonical_store import JQuantsCanonicalStore
from src.infrastructure.market_data.jquants_raw_ledger_state import RawStoreLedgerState
from src.infrastructure.market_data.jquants_raw_store import JQuantsRawStore, JQuantsRawStoreError
from src.infrastructure.notification.slack_notify import notify_critical


def _date_argument(value: str) -> date:
    try:
        parsed = date.fromisoformat(value)
    except ValueError:
        raise argparse.ArgumentTypeError("Date must be a valid YYYY-MM-DD") from None
    if parsed.isoformat() != value:
        raise argparse.ArgumentTypeError("Date must be a valid YYYY-MM-DD")
    return parsed


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="J-Quants past-data backfill")
    parser.add_argument("--dataset", choices=("daily", "minute"))
    parser.add_argument("--from", dest="start", type=_date_argument)
    parser.add_argument("--to", dest="end", type=_date_argument)
    parser.add_argument("--max-days", type=int)
    parser.add_argument("--budget-seconds", type=float, help="Override the daily time budget")
    parser.add_argument("--execute", action="store_true", help="Fetch from the real API")
    parser.add_argument(
        "--clear-stop-flag", action="store_true",
        help="Human-only: remove the stop flag after the cause is confirmed",
    )
    args = parser.parse_args(argv)
    if args.max_days is not None and args.max_days <= 0:
        parser.error("--max-days must be positive")
    if args.budget_seconds is not None and not 0 < args.budget_seconds <= DAILY_BUDGET_SECONDS:
        parser.error("--budget-seconds must be positive and at most the daily budget")
    state = JQuantsBackfillStateStore(config.JQUANTS_BACKFILL_STATE_DIRECTORY)
    try:
        if args.clear_stop_flag:
            print(json.dumps({"status": "COMPLETE", "cleared": state.clear_stop_flag()}))
            return 0
        if args.dataset is None:
            parser.error("--dataset is required")
        today = jst_today()
        start = args.start or calculate_earliest_available_date(args.dataset, today)
        end = args.end or today - timedelta(days=1)
        raw_store = JQuantsRawStore()
        planner = JQuantsBackfillPlanner(
            JQuantsCalendarStore(config.JQUANTS_CALENDAR_DIRECTORY),
            RawStoreLedgerState(raw_store),
        )
        if not args.execute:
            plan = planner.plan(args.dataset, start, end, today)
            flag = state.stop_flag()
            print(json.dumps({
                "status": "DRY_RUN", "dataset": args.dataset,
                "stop_flag": None if flag is None else flag["reason"],
                "summary": {k: len(v) for k, v in plan.as_dict().items()},
            }, sort_keys=True))
            return 0
        canonical_store = JQuantsCanonicalStore(config.JQUANTS_CANONICAL_DIRECTORY)
        ingestion = JQuantsSingleDayIngestion(
            raw_store, canonical_store,
            metrics_directory=config.JQUANTS_EXECUTION_METRICS_DIRECTORY,
            notifier=notify_critical,
        )
        usecase = JQuantsBackfillUseCase(
            planner, ingestion, state,
            raw_directory=raw_store.raw_directory,
            canonical_directory=canonical_store.directory,
            **({} if args.budget_seconds is None else {"budget_seconds": args.budget_seconds}),
        )
        result = usecase.run(args.dataset, start, end, max_days=args.max_days)
    except (BackfillStateError, BusinessCalendarError, ConsumerContractError,
            JQuantsRawStoreError, OSError, ValueError, sqlite3.Error) as exc:
        print(json.dumps({"status": "FAILED", "error_category": type(exc).__name__}))
        return 1
    print(json.dumps(result.as_dict(), sort_keys=True))
    if result.exit_code:
        print(f"ALERT: backfill {result.outcome.value}", file=sys.stderr)
    return result.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
