"""Summarize J-Quants execution metrics into a CSV (read-only, no network).

Exit codes: 0 = written, 1 = unexpected error, 2 = invalid arguments,
3 = a latest run is No-Go (only with --fail-on-no-go), 4 = metrics directory missing.
"""

from __future__ import annotations

import argparse
import math
from pathlib import Path

from src.application.jquants_metrics_evaluation import JQuantsGoThresholds
from src.application.jquants_metrics_summary import summarize_directory, write_csv
from src.config import config

EXIT_NO_GO = 3
EXIT_MISSING_METRICS = 4


def _positive_finite(text: str) -> float:
    value = float(text)
    if not math.isfinite(value) or value <= 0:
        raise argparse.ArgumentTypeError("must be a finite number greater than 0")
    return value


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--metrics-dir", default=None,
                        help="Default: config.JQUANTS_EXECUTION_METRICS_DIRECTORY")
    parser.add_argument("--output", default=None,
                        help="Default: metrics_summary.csv next to the metrics directory")
    parser.add_argument("--daily-elapsed-limit", type=_positive_finite, default=None,
                        help="Wall-clock seconds allowed for a daily run")
    parser.add_argument("--minute-elapsed-limit", type=_positive_finite, default=None,
                        help="Wall-clock seconds allowed for a minute run")
    parser.add_argument("--fail-on-no-go", action="store_true",
                        help="Exit with 3 when the latest run of any dataset/date is No-Go")
    args = parser.parse_args(argv)
    if args.metrics_dir is None:
        args.metrics_dir = str(config.JQUANTS_EXECUTION_METRICS_DIRECTORY)
    if args.output is None:
        args.output = str(config.JQUANTS_EXECUTION_METRICS_DIRECTORY.parent / "metrics_summary.csv")
    if not Path(args.metrics_dir).is_dir():
        print(f"metrics directory not found: {args.metrics_dir}")
        return EXIT_MISSING_METRICS
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
        return EXIT_NO_GO
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
