"""Run with python -m src.entrypoints.list_jquants_backfill --dataset daily --from ... --to ...

通信しない。カレンダーと台帳を読むだけで、日付のみを出力する。
"""

import argparse
import json
from datetime import date

from src.application.jquants_backfill_planner import JQuantsBackfillPlanner
from src.config import config
from src.domain.jquants_business_calendar import BusinessCalendarError
from src.domain.jquants_consumer_contract import ConsumerContractError
from src.infrastructure.market_data.jquants_calendar_store import JQuantsCalendarStore
from src.infrastructure.market_data.jquants_raw_ledger_state import RawStoreLedgerState
from src.infrastructure.market_data.jquants_raw_store import JQuantsRawStore, JQuantsRawStoreError


def _date_argument(value: str) -> date:
    try:
        parsed = date.fromisoformat(value)
    except ValueError:
        raise argparse.ArgumentTypeError("Date must be a valid YYYY-MM-DD") from None
    if parsed.isoformat() != value:
        raise argparse.ArgumentTypeError("Date must be a valid YYYY-MM-DD")
    return parsed



def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="List J-Quants backfill targets (no network)")
    parser.add_argument("--dataset", choices=("daily", "minute"), required=True)
    parser.add_argument("--from", dest="start", type=_date_argument, required=True)
    parser.add_argument("--to", dest="end", type=_date_argument, required=True)
    args = parser.parse_args(argv)
    try:
        planner = JQuantsBackfillPlanner(
            JQuantsCalendarStore(config.JQUANTS_CALENDAR_DIRECTORY),
            RawStoreLedgerState(JQuantsRawStore()),
        )
        plan = planner.plan(args.dataset, args.start, args.end, date.today())
    except (BusinessCalendarError, ConsumerContractError, JQuantsRawStoreError, OSError,
            ValueError) as exc:
        print(json.dumps({"status": "FAILED", "error_category": type(exc).__name__}))
        return 1
    body = plan.as_dict()
    output = {
        "status": "COMPLETE", "dataset": args.dataset, **body,
        "summary": {name: len(days) for name, days in body.items()},
        "warnings": ["holiday_with_ledger"] if body["holiday_with_ledger"] else [],
    }
    print(json.dumps(output, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
