"""Run with python -m src.entrypoints.ingest_jquants_single_day."""

import argparse
import json
import sqlite3
import sys

from src.config import config
from src.application.jquants_single_day_ingestion import JQuantsSingleDayIngestion
from src.infrastructure.market_data.jquants_canonical_store import (
    JQuantsCanonicalStore,
    strict_date,
)
from src.infrastructure.market_data.jquants_raw_store import JQuantsRawStore, JQuantsRawStoreError


def _date_argument(value: str) -> str:
    try:
        strict_date(value)
    except ValueError:
        raise argparse.ArgumentTypeError("Date must be a valid YYYY-MM-DD") from None
    return value


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="J-Quants single-day all-symbol ingestion")
    parser.add_argument("--dataset", choices=("daily", "minute"), required=True)
    parser.add_argument("--date", type=_date_argument, required=True)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--refresh", action="store_true")
    mode.add_argument("--canonical-only", action="store_true")
    parser.add_argument(
        "--measure-peak-memory", action="store_true",
        help="Opt in to tracemalloc peak measurement (slows large runs)",
    )
    args = parser.parse_args(argv)
    try:
        usecase = JQuantsSingleDayIngestion(
            JQuantsRawStore(), JQuantsCanonicalStore(config.JQUANTS_CANONICAL_DIRECTORY),
            metrics_directory=config.JQUANTS_EXECUTION_METRICS_DIRECTORY,
            measure_peak_memory=args.measure_peak_memory,
        )
        summary = usecase.run(
            dataset=args.dataset, date=args.date,
            refresh=args.refresh, canonical_only=args.canonical_only,
        )
    except (JQuantsRawStoreError, OSError, ValueError, sqlite3.Error):
        print(json.dumps({"dataset": args.dataset, "date": args.date, "status": "FAILED",
                          "error_category": "INITIALIZATION_ERROR"}))
        return 1
    print(json.dumps(summary.as_dict(), sort_keys=True))
    if summary.error_category == "RATE_LIMIT_ABORTED":
        print(
            "ALERT: 429 cumulative limit reached; run aborted. "
            "Investigate rate-limit settings/plan before --refresh.",
            file=sys.stderr,
        )
    return summary.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
