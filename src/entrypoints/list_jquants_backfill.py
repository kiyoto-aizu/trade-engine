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
from src.infrastructure.market_data.jquants_raw_store import JQuantsRawStore, JQuantsRawStoreError


def _date_argument(value: str) -> date:
    try:
        parsed = date.fromisoformat(value)
    except ValueError:
        raise argparse.ArgumentTypeError("Date must be a valid YYYY-MM-DD") from None
    if parsed.isoformat() != value:
        raise argparse.ArgumentTypeError("Date must be a valid YYYY-MM-DD")
    return parsed


class _RawStoreLedger:
    def __init__(self, raw_store: JQuantsRawStore) -> None:
        self._raw_store = raw_store

    def state(self, dataset: str, target_date: date) -> str | None:
        state = self._raw_store.get_state_for(
            kind=dataset, endpoint=f"equities/bars/{dataset}",
            conditions={"date": target_date.isoformat()},
        )
        return None if state is None else state.value


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="List J-Quants backfill targets (no network)")
    parser.add_argument("--dataset", choices=("daily", "minute"), required=True)
    parser.add_argument("--from", dest="start", type=_date_argument, required=True)
    parser.add_argument("--to", dest="end", type=_date_argument, required=True)
    args = parser.parse_args(argv)
    try:
        planner = JQuantsBackfillPlanner(
            JQuantsCalendarStore(config.JQUANTS_CALENDAR_DIRECTORY),
            _RawStoreLedger(JQuantsRawStore()),
        )
        plan = planner.plan(args.dataset, args.start, args.end, date.today())
    except (BusinessCalendarError, ConsumerContractError, JQuantsRawStoreError, OSError,
            ValueError) as exc:
        print(json.dumps({"status": "FAILED", "error_category": type(exc).__name__}))
        return 1
    print(json.dumps({"status": "COMPLETE", "dataset": args.dataset, **plan.as_dict()},
                     sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
