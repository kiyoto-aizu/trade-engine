"""List or clear J-Quants backfill review exclusions without editing state by hand."""

import argparse
from datetime import date
import json

from src.application.jquants_backfill_usecase import jst_today
from src.config import config
from src.infrastructure.market_data.jquants_backfill_state import (
    BackfillStateError,
    JQuantsBackfillStateStore,
    REVIEW_DATASETS,
    REVIEW_KINDS,
)


def _date_argument(value: str) -> date:
    try:
        parsed = date.fromisoformat(value)
    except ValueError:
        raise argparse.ArgumentTypeError("Date must be a valid YYYY-MM-DD") from None
    if parsed.isoformat() != value:
        raise argparse.ArgumentTypeError("Date must be a valid YYYY-MM-DD")
    return parsed


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Manage J-Quants backfill review exclusions")
    commands = parser.add_subparsers(dest="command", required=True)
    list_parser = commands.add_parser("list", help="List review exclusions without changing state")
    list_parser.add_argument("--dataset", choices=REVIEW_DATASETS)
    clear_parser = commands.add_parser("clear", help="Clear one review exclusion")
    clear_parser.add_argument("--dataset", choices=REVIEW_DATASETS, required=True)
    clear_parser.add_argument("--date", type=_date_argument, required=True)
    clear_parser.add_argument("--reason-code", choices=REVIEW_KINDS, required=True)
    args = parser.parse_args(argv)

    state = JQuantsBackfillStateStore(config.JQUANTS_BACKFILL_STATE_DIRECTORY)
    try:
        if args.command == "list":
            with state.global_lock():
                review = state.review_entries(args.dataset)
            print(json.dumps({
                "status": "COMPLETE",
                "review": review,
            }, sort_keys=True))
            return 0

        with state.global_lock():
            operation_date = jst_today()
            cleared = state.clear_review(
                args.dataset, args.date, args.reason_code, operation_date=operation_date
            )
        if not cleared:
            print(json.dumps({
                "status": "FAILED", "error_category": "REVIEW_ENTRY_NOT_FOUND",
            }, sort_keys=True))
            return 1
        print(json.dumps({
            "status": "COMPLETE", "cleared": True, "dataset": args.dataset,
            "target_date": args.date.isoformat(), "operation_date": operation_date.isoformat(),
            "reason_code": args.reason_code,
        }, sort_keys=True))
        return 0
    except (BackfillStateError, OSError, ValueError) as exc:
        print(json.dumps({
            "status": "FAILED", "error_category": type(exc).__name__,
        }, sort_keys=True))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
