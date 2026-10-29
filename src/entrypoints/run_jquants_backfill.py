"""Run with python -m src.entrypoints.run_jquants_backfill --dataset daily --from ... --to ... --allow-network

過去補填を古い日から1日ずつ実行する（1実行1dataset）。出力は日付とカテゴリのみ。
終了コード: 0=完了、1=失敗あり、2=要確認あり（--allow-network 未指定のスキップも2）、3=429打ち切り、4=補填停止。
"""

import argparse
import json
import shutil
import sqlite3
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from src.application.jquants_backfill_job import JQuantsBackfillJob
from src.application.jquants_backfill_planner import JQuantsBackfillPlanner
from src.application.jquants_single_day_ingestion import JQuantsSingleDayIngestion
from src.config import config
from src.domain.jquants_backfill_policy import BACKFILL_BUDGET_SECONDS
from src.domain.jquants_business_calendar import BusinessCalendarError
from src.domain.jquants_consumer_contract import ConsumerContractError
from src.entrypoints.list_jquants_backfill import _date_argument, _RawStoreLedger
from src.infrastructure.market_data.jquants_backfill_hold_store import (
    BackfillHoldStoreError,
    JQuantsBackfillHoldStore,
)
from src.infrastructure.market_data.jquants_calendar_store import JQuantsCalendarStore
from src.infrastructure.market_data.jquants_canonical_store import JQuantsCanonicalStore
from src.infrastructure.market_data.jquants_raw_store import JQuantsRawStore, JQuantsRawStoreError

_JST = timezone(timedelta(hours=9))


def _positive_int(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError("Must be a positive integer") from None
    if parsed <= 0:
        raise argparse.ArgumentTypeError("Must be a positive integer")
    return parsed


def _positive_float(value: str) -> float:
    try:
        parsed = float(value)
    except ValueError:
        raise argparse.ArgumentTypeError("Must be a positive number") from None
    if not parsed > 0:
        raise argparse.ArgumentTypeError("Must be a positive number")
    return parsed


class _Clock:
    def today(self) -> date:
        return datetime.now(_JST).date()

    def monotonic(self) -> float:
        return time.monotonic()


class _StorageFreeSpace:
    """保存先（raw・canonical）の空き容量の小さい方を返す。"""

    def __init__(self, *directories: Path) -> None:
        self._directories = directories

    def free_bytes(self) -> int:
        values = []
        for directory in self._directories:
            probe = Path(directory)
            while not probe.exists() and probe != probe.parent:
                probe = probe.parent
            values.append(shutil.disk_usage(probe).free)
        return min(values)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="J-Quants past backfill job")
    parser.add_argument("--dataset", choices=("daily", "minute"), required=True)
    parser.add_argument("--from", dest="start", type=_date_argument, required=True)
    parser.add_argument("--to", dest="end", type=_date_argument, required=True)
    parser.add_argument("--max-days", type=_positive_int)
    parser.add_argument("--budget-seconds", type=_positive_float, default=BACKFILL_BUDGET_SECONDS)
    parser.add_argument("--allow-network", action="store_true",
                        help="Required to call the real API")
    args = parser.parse_args(argv)
    if not args.allow_network:
        print(json.dumps({"status": "SKIPPED", "reason": "--allow-network is required"}))
        return 2
    if args.start > args.end:
        print(json.dumps({"status": "FAILED", "error_category": "INVALID_RANGE"}))
        return 1
    try:
        raw_store = JQuantsRawStore()
        job = JQuantsBackfillJob(
            JQuantsBackfillPlanner(
                JQuantsCalendarStore(config.JQUANTS_CALENDAR_DIRECTORY), _RawStoreLedger(raw_store)
            ),
            JQuantsSingleDayIngestion(
                raw_store, JQuantsCanonicalStore(config.JQUANTS_CANONICAL_DIRECTORY),
                metrics_directory=config.JQUANTS_EXECUTION_METRICS_DIRECTORY,
            ),
            JQuantsBackfillHoldStore(config.JQUANTS_BACKFILL_HOLD_DIRECTORY),
            _StorageFreeSpace(config.JQUANTS_RAW_DIRECTORY, config.JQUANTS_CANONICAL_DIRECTORY),
            _Clock(),
        )
        result = job.run(
            args.dataset, args.start, args.end,
            max_days=args.max_days, budget_seconds=args.budget_seconds,
        )
    except (BusinessCalendarError, BackfillHoldStoreError, ConsumerContractError,
            JQuantsRawStoreError, OSError, ValueError, sqlite3.Error) as exc:
        print(json.dumps({"status": "FAILED", "error_category": type(exc).__name__}))
        return 1
    print(json.dumps(result.as_dict(), sort_keys=True))
    return result.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
