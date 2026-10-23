"""Summarize J-Quants execution metrics into a CSV (read-only, no network)."""

from __future__ import annotations

import argparse
from pathlib import Path

from src.application.jquants_metrics_summary import summarize_directory, write_csv


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--metrics-dir", default="data/jquants/metrics")
    parser.add_argument("--output", default="data/jquants/metrics_summary.csv")
    args = parser.parse_args(argv)
    rows = summarize_directory(args.metrics_dir)
    Path(args.output).parent.mkdir(parents=True, exist_ok=True)
    write_csv(rows, args.output)
    print(f"runs={len(rows)} output={args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
