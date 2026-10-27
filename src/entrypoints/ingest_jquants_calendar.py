"""Run with python -m src.entrypoints.ingest_jquants_calendar --from ... --to ... --allow-network."""

import argparse
import json
from datetime import date

from src.application.jquants_calendar_ingestion import JQuantsCalendarIngestion
from src.config import config
from src.domain.jquants_business_calendar import BusinessCalendarError
from src.infrastructure.market_data.jquants_calendar_store import JQuantsCalendarStore
from src.infrastructure.market_data.jquants_v2_client import JQuantsAPIError, JQuantsV2Client


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
    client = None
    try:
        client = JQuantsV2Client()
        summary = JQuantsCalendarIngestion(
            client, JQuantsCalendarStore(config.JQUANTS_CALENDAR_DIRECTORY)
        ).run(args.start, args.end, refresh=args.refresh)
    except (JQuantsAPIError, BusinessCalendarError, OSError, ValueError) as exc:
        category = getattr(exc, "category", type(exc).__name__)
        print(json.dumps({"status": "FAILED", "error_category": category}))
        return 1
    finally:
        if client is not None:
            client.close()
    print(json.dumps(summary.as_dict(), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
