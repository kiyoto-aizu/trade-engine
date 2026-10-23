"""Read-only per-run summary of J-Quants execution metrics JSON files.

Only counts, timings and sizes are emitted. Record values, symbols and
credentials are never read from or written to the summary.
"""

from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any, Iterable

from src.application.jquants_metrics_evaluation import (
    JQuantsGoThresholds,
    evaluate_metrics,
)

COLUMNS = (
    "dataset",
    "date",
    "file",
    "status",
    "verdict",
    "reasons",
    "request_count",
    "retry_count",
    "rate_limit_429_count",
    "wait_seconds",
    "acquisition_seconds",
    "raw_validation_seconds",
    "conversion_seconds",
    "canonical_validation_seconds",
    "total_seconds",
    "raw_size_bytes",
    "canonical_size_bytes",
)


def summarize_run(
    path: Path, thresholds: JQuantsGoThresholds | None = None
) -> dict[str, Any]:
    metrics = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(metrics, dict):
        raise ValueError("metrics JSON must be an object")
    durations = metrics.get("durations_seconds")
    durations = durations if isinstance(durations, dict) else {}
    evaluation = evaluate_metrics(metrics, thresholds)
    return {
        "dataset": metrics.get("dataset"),
        "date": metrics.get("date"),
        "file": path.name,
        "status": metrics.get("status"),
        "verdict": evaluation.verdict,
        "reasons": ";".join(evaluation.reasons),
        "request_count": metrics.get("request_count"),
        "retry_count": metrics.get("retry_count"),
        "rate_limit_429_count": metrics.get("rate_limit_429_count"),
        "wait_seconds": metrics.get("wait_seconds"),
        "acquisition_seconds": durations.get("acquisition"),
        "raw_validation_seconds": durations.get("raw_validation"),
        "conversion_seconds": durations.get("conversion"),
        "canonical_validation_seconds": durations.get("canonical_validation"),
        "total_seconds": durations.get("total"),
        "raw_size_bytes": metrics.get("raw_size_bytes"),
        "canonical_size_bytes": metrics.get("canonical_size_bytes"),
    }


def summarize_directory(
    metrics_directory: Path | str, thresholds: JQuantsGoThresholds | None = None
) -> list[dict[str, Any]]:
    rows = []
    for path in sorted(Path(metrics_directory).glob("*/date=*/*.json")):
        try:
            rows.append(summarize_run(path, thresholds))
        except (OSError, ValueError):
            rows.append({"dataset": path.parent.parent.name, "file": path.name,
                         "date": path.parent.name.removeprefix("date="),
                         "verdict": "No-Go", "reasons": "UNREADABLE_METRICS"})
    return rows


def write_csv(rows: Iterable[dict[str, Any]], output: Path | str) -> None:
    with Path(output).open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=COLUMNS, restval="")
        writer.writeheader()
        writer.writerows(rows)
