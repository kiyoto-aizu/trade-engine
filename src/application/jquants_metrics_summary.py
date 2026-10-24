"""Read-only per-run summary of J-Quants execution metrics JSON files.

Only validated numbers and enumerated codes are emitted. Record values,
symbols, credentials and free-form text are never copied into the summary.
"""

from __future__ import annotations

import csv
import json
import re
from pathlib import Path
from typing import Any, Iterable, Mapping

from src.application.jquants_metrics_evaluation import (
    NO_GO,
    VALID_STATUSES,
    JQuantsGoThresholds,
    evaluate_metrics,
    safe_number,
)

DATASETS = frozenset({"daily", "minute"})
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

COLUMNS = (
    "dataset",
    "date",
    "file",
    "status",
    "partial_verdict",
    "reasons",
    "not_evaluated",
    "is_latest",
    "consecutive_failed",
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


def _number(value: Any) -> float | int | str:
    number = safe_number(value)
    if number is None:
        return ""
    return int(number) if number.is_integer() else number


def summarize_run(
    path: Path, thresholds: JQuantsGoThresholds | None = None
) -> dict[str, Any]:
    metrics = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(metrics, dict):
        raise ValueError("metrics JSON must be an object")
    durations = metrics.get("durations_seconds")
    durations = durations if isinstance(durations, dict) else {}
    evaluation = evaluate_metrics(metrics, thresholds)
    dataset = metrics.get("dataset")
    date = metrics.get("date")
    status = metrics.get("status")
    return {
        "dataset": dataset if dataset in DATASETS else "",
        "date": date if isinstance(date, str) and _DATE.match(date) else "",
        "file": path.name,
        "status": status if status in VALID_STATUSES else "INVALID",
        "partial_verdict": evaluation.verdict,
        "reasons": ";".join(evaluation.reasons),
        "not_evaluated": ";".join(evaluation.not_evaluated),
        "request_count": _number(metrics.get("request_count")),
        "retry_count": _number(metrics.get("retry_count")),
        "rate_limit_429_count": _number(metrics.get("rate_limit_429_count")),
        "wait_seconds": _number(metrics.get("wait_seconds")),
        "acquisition_seconds": _number(durations.get("acquisition")),
        "raw_validation_seconds": _number(durations.get("raw_validation")),
        "conversion_seconds": _number(durations.get("conversion")),
        "canonical_validation_seconds": _number(durations.get("canonical_validation")),
        "total_seconds": _number(durations.get("total")),
        "raw_size_bytes": _number(metrics.get("raw_size_bytes")),
        "canonical_size_bytes": _number(metrics.get("canonical_size_bytes")),
    }


def _unreadable_row(path: Path) -> dict[str, Any]:
    dataset = path.parent.parent.name
    date = path.parent.name.removeprefix("date=")
    return {
        "dataset": dataset if dataset in DATASETS else "",
        "date": date if _DATE.match(date) else "",
        "file": path.name,
        "status": "INVALID",
        "partial_verdict": NO_GO,
        "reasons": "UNREADABLE_METRICS",
    }


def _mark_latest_and_streaks(rows: list[dict[str, Any]]) -> None:
    """Rows are in file-name (timestamp) order, so the last row per key is the latest."""
    groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for row in rows:
        groups.setdefault((row["dataset"], row["date"]), []).append(row)
    for attempts in groups.values():
        streak = 0
        for row in attempts:
            streak = streak + 1 if row["status"] == "FAILED" else 0
            row["consecutive_failed"] = streak
            row["is_latest"] = False
        attempts[-1]["is_latest"] = True


def summarize_directory(
    metrics_directory: Path | str,
    thresholds_by_dataset: Mapping[str, JQuantsGoThresholds] | None = None,
) -> list[dict[str, Any]]:
    limits = thresholds_by_dataset or {}
    rows = []
    for path in sorted(Path(metrics_directory).glob("*/date=*/*.json")):
        try:
            rows.append(summarize_run(path, limits.get(path.parent.parent.name)))
        except (OSError, ValueError):
            rows.append(_unreadable_row(path))
    _mark_latest_and_streaks(rows)
    return rows


def write_csv(rows: Iterable[dict[str, Any]], output: Path | str) -> None:
    # utf-8-sig keeps the file readable when opened directly in Excel.
    with Path(output).open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=COLUMNS, restval="")
        writer.writeheader()
        writer.writerows(rows)
