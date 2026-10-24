"""Summarize J-Quants execution metrics into a CSV (read-only, no network)."""

from __future__ import annotations

import argparse
from pathlib import Path

from src.application.jquants_metrics_evaluation import JQuantsGoThresholds
from src.application.jquants_metrics_summary import summarize_directory, write_csv


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--metrics-dir", default="data/jquants/metrics")
    parser.add_argument("--output", default="data/jquants/metrics_summary.csv")
    parser.add_argument("--daily-elapsed-limit", type=float, default=None,
                        help="Seconds of active time allowed for a daily run")
    parser.add_argument("--minute-elapsed-limit", type=float, default=None,
                        help="Seconds of active time allowed for a minute run")
    parser.add_argument("--fail-on-no-go", action="store_true",
                        help="Exit with 1 when the latest run of any dataset/date is No-Go")
    args = parser.parse_args(argv)
    rows = summarize_directory(
        args.metrics_dir,
        {
            "daily": JQuantsGoThresholds(elapsed_limit_seconds=args.daily_elapsed_limit),
            "minute": JQuantsGoThresholds(elapsed_limit_seconds=args.minute_elapsed_limit),
        },
    )
    Path(args.output).parent.mkdir(parents=True, exist_ok=True)
    write_csv(rows, args.output)
    print(f"runs={len(rows)} output={args.output}")
    if args.fail_on_no_go and any(
        row["is_latest"] and row["partial_verdict"] == "No-Go" for row in rows
    ):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
