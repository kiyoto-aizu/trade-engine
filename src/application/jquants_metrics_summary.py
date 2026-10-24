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
    RAW_SOURCES,
    VALID_STATUSES,
    JQuantsGoThresholds,
    evaluate_metrics,
    safe_code,
    safe_codes,
    safe_number,
    stage_breakdown,
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
    "setup_reasons",
    "not_evaluated",
    "evaluation_complete",
    "is_latest",
    "consecutive_failed_same_date",
    "consecutive_failed_dataset",
    "request_count",
    "retry_count",
    "rate_limit_429_count",
    "wait_seconds",
    "acquisition_seconds",
    "raw_validation_seconds",
    "conversion_seconds",
    "canonical_validation_seconds",
    "total_seconds",
    "active_seconds",
    "raw_size_bytes",
    "canonical_size_bytes",
    "error_category",
    "warnings",
    "symbol_count",
    "unknown_field_count",
    "raw_source",
    "other_share",
)

# Reasons caused by missing configuration, not by the quality of the run.
SETUP_REASONS = frozenset({"ELAPSED_LIMIT_UNSET"})


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
    total = safe_number(durations.get("total"))
    wait = safe_number(metrics.get("wait_seconds"))
    raw_source = metrics.get("raw_source")
    shares = stage_breakdown(metrics)
    return {
        "dataset": dataset if dataset in DATASETS else "",
        "date": date if isinstance(date, str) and _DATE.match(date) else "",
        "file": path.name,
        "status": status if status in VALID_STATUSES else "INVALID",
        "partial_verdict": evaluation.verdict,
        "reasons": ";".join(r for r in evaluation.reasons if r not in SETUP_REASONS),
        "setup_reasons": ";".join(r for r in evaluation.reasons if r in SETUP_REASONS),
        "not_evaluated": ";".join(evaluation.not_evaluated),
        "evaluation_complete": not evaluation.not_evaluated,
        "request_count": _number(metrics.get("request_count")),
        "retry_count": _number(metrics.get("retry_count")),
        "rate_limit_429_count": _number(metrics.get("rate_limit_429_count")),
        "wait_seconds": _number(metrics.get("wait_seconds")),
        "acquisition_seconds": _number(durations.get("acquisition")),
        "raw_validation_seconds": _number(durations.get("raw_validation")),
        "conversion_seconds": _number(durations.get("conversion")),
        "canonical_validation_seconds": _number(durations.get("canonical_validation")),
        "total_seconds": _number(durations.get("total")),
        "active_seconds": "" if total is None or wait is None else round(max(total - wait, 0.0), 3),
        "raw_size_bytes": _number(metrics.get("raw_size_bytes")),
        "canonical_size_bytes": _number(metrics.get("canonical_size_bytes")),
        "error_category": safe_code(metrics.get("error_category")) or "",
        "warnings": ";".join(safe_codes(metrics.get("warnings")) or []),
        "symbol_count": _number(metrics.get("symbol_count")),
        "unknown_field_count": _number(metrics.get("unknown_field_count")),
        "raw_source": raw_source if raw_source in RAW_SOURCES else "",
        "other_share": round(shares["other"], 4) if shares else "",
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


def _mark_latest_and_streaks(entries: list[tuple[tuple[str, str], dict[str, Any]]]) -> None:
    """Entries are in file-name (timestamp) order, so the last one per key is the latest.

    Keys come from the directory names, never from file content.
    """
    groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for key, row in entries:
        groups.setdefault(key, []).append(row)
    for attempts in groups.values():
        streak = 0
        for row in attempts:
            streak = streak + 1 if row["status"] == "FAILED" else 0
            row["consecutive_failed_same_date"] = streak
            row["is_latest"] = False
        attempts[-1]["is_latest"] = True
    # Backfill goes back in dates, so the dataset-wide streak follows the file-name timestamp.
    by_dataset: dict[str, list[dict[str, Any]]] = {}
    for (dataset, _), row in entries:
        by_dataset.setdefault(dataset, []).append(row)
    for runs in by_dataset.values():
        streak = 0
        for row in sorted(runs, key=lambda r: str(r.get("file", ""))):
            streak = streak + 1 if row["status"] == "FAILED" else 0
            row["consecutive_failed_dataset"] = streak


def summarize_directory(
    metrics_directory: Path | str,
    thresholds_by_dataset: Mapping[str, JQuantsGoThresholds] | None = None,
) -> list[dict[str, Any]]:
    limits = thresholds_by_dataset or {}
    entries = []
    for path in sorted(Path(metrics_directory).glob("*/date=*/*.json")):
        key = (path.parent.parent.name, path.parent.name)
        try:
            row = summarize_run(path, limits.get(path.parent.parent.name))
        except (OSError, ValueError):
            row = _unreadable_row(path)
        entries.append((key, row))
    _mark_latest_and_streaks(entries)
    return [row for _, row in entries]


def write_csv(rows: Iterable[dict[str, Any]], output: Path | str) -> None:
    # utf-8-sig keeps the file readable when opened directly in Excel.
    with Path(output).open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=COLUMNS, restval="")
        writer.writeheader()
        writer.writerows(rows)
