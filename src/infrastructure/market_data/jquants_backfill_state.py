"""File-based global lock and stop state for the J-Quants backfill (doc 18 §1-1, §2-1).

Holds only enumerated codes, dates and counters. No paths, symbols or free text.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import date, datetime, timezone
import json
import math
import os
from pathlib import Path
from typing import Any

from src.domain.jquants_backfill_policy import StopReason

LOCK_FILE = "backfill.lock"
STATE_FILE = "backfill_state.json"
REVIEW_KINDS = ("PERMISSION_403", "OUT_OF_CONTRACT_400", "UNEXPECTED_400_IN_RANGE", "UNKNOWN_STATUS")
REVIEW_DATASETS = ("daily", "minute")
_STOP_REASONS = frozenset(reason.value for reason in StopReason)
DAILY_REASONS = frozenset({
    "AUTH_ERROR", "PERMISSION_ERROR", "DISK_SPACE_CRITICAL", "DISK_UNMEASURABLE",
    "RATE_LIMIT_ABORTED", "CONSECUTIVE_FAILED", "STORAGE_OR_FORMAT_ERROR",
    "SERVER_ERROR", "CLIENT_REQUEST_ERROR", "QUALITY_ERROR", "UNKNOWN_ERROR",
    "REPEATED_TODAY_STOP", "RESPONSE_SCHEMA_ERROR", "RAW_STORE_ERROR",
})
_OPTIONAL_FIELDS = {
    "review_clear_audit", "daily_history", "daily_stops", "shared_budget",
}


class BackfillStateError(Exception):
    pass


class BackfillLockedError(BackfillStateError):
    """Another run holds the global lock. Never wait for it or take it over."""


class JQuantsBackfillStateStore:
    """Persistent state operations; callers serialize cross-process access with global_lock()."""

    def __init__(self, directory: Path | str) -> None:
        self._directory = Path(directory)

    @contextmanager
    def global_lock(self) -> Iterator[None]:
        self._directory.mkdir(parents=True, exist_ok=True)
        path = self._directory / LOCK_FILE
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            raise BackfillLockedError("global lock is held") from None
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump({"pid": os.getpid(), "acquired_at": _now()}, handle)
            yield
        finally:
            path.unlink(missing_ok=True)

    def load(self) -> dict[str, Any]:
        path = self._directory / STATE_FILE
        try:
            state = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(state, dict):
                raise ValueError
            if not set(state).issubset(_empty_state()):
                raise ValueError
            required_fields = set(_empty_state()) - _OPTIONAL_FIELDS
            if not required_fields.issubset(state):
                raise ValueError
            merged = {**_empty_state(), **state}
            if not isinstance(merged["today_stops"], dict) or not isinstance(
                merged["cause_stops"], dict
            ):
                raise ValueError
            _validate_execution_state(merged)
            return merged
        except FileNotFoundError:
            return _empty_state()
        except (OSError, ValueError, TypeError):
            # A damaged state must stop the run, not silently reset the stop flags.
            raise BackfillStateError("backfill state is unreadable") from None

    def save(self, state: dict[str, Any]) -> None:
        path = self._directory / STATE_FILE
        temporary = path.with_suffix(".tmp")
        try:
            _validate_execution_state(state)
            self._directory.mkdir(parents=True, exist_ok=True)
            temporary.write_text(json.dumps(state, sort_keys=True), encoding="utf-8")
            os.replace(temporary, path)
        except (OSError, ValueError, TypeError, KeyError):
            # 停止が永続化されないまま続行しない。呼び出し側は失敗として終了する。
            raise BackfillStateError("backfill state is not writable") from None

    def stop_flag(self) -> dict[str, str] | None:
        return self.load()["stop_flag"]

    def set_stop_flag(self, scope: str, reason: str, set_on: date) -> None:
        state = self.load()
        state["stop_flag"] = {"scope": scope, "reason": reason, "set_on": set_on.isoformat()}
        self.save(state)

    def clear_stop_flag(self) -> bool:
        state = self.load()
        had_flag = state["stop_flag"] is not None
        state["stop_flag"] = None
        self.save(state)
        return had_flag

    def today_stop_dates(
        self, dataset: str, reason: str | None = None
    ) -> list[tuple[date, str]]:
        return [
            (date.fromisoformat(item["date"]), item["reason"])
            for item in self.load()["today_stops"].get(dataset, [])
            if reason is None or item["reason"] == reason
        ]

    def record_today_stop(self, dataset: str, day: date, reason: str) -> None:
        state = self.load()
        stops = state["today_stops"].setdefault(dataset, [])
        if not any(i["date"] == day.isoformat() for i in stops):
            stops.append({"date": day.isoformat(), "reason": reason})
            state["today_stops"][dataset] = stops[-10:]
        self.save(state)

    def record_cause_stop(self, dataset: str, day: date, reason: str) -> None:
        state = self.load()
        stops = state["cause_stops"].setdefault(dataset, [])
        if not any(i["date"] == day.isoformat() for i in stops):
            stops.append({"date": day.isoformat(), "reason": reason})
            state["cause_stops"][dataset] = stops[-10:]
        self.save(state)

    def cause_stop_dates(self, dataset: str) -> list[tuple[date, str]]:
        return [
            (date.fromisoformat(item["date"]), item["reason"])
            for item in self.load()["cause_stops"].get(dataset, [])
        ]

    def reset_today_stops(self, dataset: str) -> None:
        """成功した走行日が挟まったら「連続」を途切れさせる（設計書18 §2-1、dataset別）。"""
        state = self.load()
        if state["today_stops"].get(dataset) or state["cause_stops"].get(dataset):
            state["today_stops"][dataset] = []
            state["cause_stops"][dataset] = []
            self.save(state)

    def is_stopped_today(self, dataset: str, day: date) -> bool:
        return any(d == day for d, _ in self.today_stop_dates(dataset))

    def failed_streak(self, dataset: str) -> int:
        return int(self.load()["failed_streak"].get(dataset, 0))

    def set_failed_streak(self, dataset: str, value: int) -> None:
        state = self.load()
        state["failed_streak"][dataset] = value
        self.save(state)

    def mark_review(self, kind: str, dataset: str, target_date: date) -> None:
        if kind not in REVIEW_KINDS:
            raise ValueError("unknown review kind")
        state = self.load()
        days = state["review"].setdefault(kind, {}).setdefault(dataset, [])
        if target_date.isoformat() not in days:
            days.append(target_date.isoformat())
        self.save(state)

    def review_dates(self, dataset: str) -> set[date]:
        found: set[date] = set()
        for per_dataset in self.load()["review"].values():
            found.update(date.fromisoformat(d) for d in per_dataset.get(dataset, []))
        return found

    def review_entries(self, dataset: str | None = None) -> list[dict[str, str]]:
        if dataset is not None and dataset not in REVIEW_DATASETS:
            raise ValueError("unknown review dataset")
        entries = [
            {"dataset": entry_dataset, "target_date": day, "reason_code": reason_code}
            for reason_code, per_dataset in self.load()["review"].items()
            for entry_dataset, days in per_dataset.items()
            if dataset is None or entry_dataset == dataset
            for day in days
        ]
        return sorted(entries, key=lambda item: (
            item["target_date"], item["dataset"], item["reason_code"]
        ))

    def clear_review(
        self, dataset: str, target_date: date, reason_code: str, operation_date: date
    ) -> bool:
        if dataset not in REVIEW_DATASETS:
            raise ValueError("unknown review dataset")
        if reason_code not in REVIEW_KINDS:
            raise ValueError("unknown review reason code")
        state = self.load()
        days = state["review"].get(reason_code, {}).get(dataset, [])
        target = target_date.isoformat()
        if target not in days:
            return False

        days.remove(target)
        per_dataset = state["review"][reason_code]
        if not days:
            per_dataset.pop(dataset)
        if not per_dataset:
            state["review"].pop(reason_code)
        state["review_clear_audit"][dataset].append({
            "target_date": target,
            "operation_date": operation_date.isoformat(),
            "reason_code": reason_code,
        })
        self.save(state)
        return True

    def daily_stop(self, dataset: str, execution_day: date) -> str | None:
        for item in self.load()["daily_stops"]:
            if item["date"] == execution_day.isoformat() and (
                item["scope"] == "STOP_TODAY" or item["dataset"] == dataset
            ):
                return item["reason"]
        return None

    def daily_history(self, dataset: str) -> list[dict[str, Any]]:
        return self.load()["daily_history"].get(dataset, [])

    def record_daily_result(
        self, dataset: str, execution_day: date, status: str, reason: str,
        *, stop_scope: str | None = None, stop_reason: str | None = None,
        halt_reason: str | None = None,
    ) -> None:
        state = self.load()
        state["daily_history"].setdefault(dataset, []).append({
            "date": execution_day.isoformat(), "status": status, "reason": reason,
        })
        if stop_scope is not None:
            state["daily_stops"].append({
                "date": execution_day.isoformat(), "dataset": dataset,
                "scope": stop_scope, "reason": stop_reason or reason,
            })
        if halt_reason is not None:
            state["stop_flag"] = {
                "scope": "ALL_FLAG", "reason": halt_reason, "set_on": execution_day.isoformat(),
            }
        self.save(state)

    def daily_stop_history(self, dataset: str) -> list[dict[str, Any]]:
        return [item for item in self.load()["daily_stops"] if item["dataset"] == dataset]

    def budget(self, day: date, limit: float = 7200.0) -> dict[str, Any]:
        state = self.load()
        budget = state["shared_budget"]
        if budget is not None and budget["in_progress"] is not None:
            raise BackfillStateError("backfill budget is unresolved")
        if budget is None or budget["date"] != day.isoformat():
            budget = {
                "date": day.isoformat(), "spent_seconds": 0.0, "held": False,
                "limit_seconds": min(limit, 7200.0), "in_progress": None,
            }
        else:
            budget["limit_seconds"] = min(budget["limit_seconds"], limit, 7200.0)
        state["shared_budget"] = budget
        self.save(state)
        return budget

    def begin_budget_day(self, day: date, dataset: str, target: date, held: bool) -> None:
        state = self.load()
        budget = state["shared_budget"]
        if budget is None or budget["date"] != day.isoformat() or budget["in_progress"] is not None:
            raise BackfillStateError("backfill budget is unresolved")
        budget["held"] = budget["held"] or held
        budget["in_progress"] = {"dataset": dataset, "target_date": target.isoformat()}
        self.save(state)

    def finish_budget_day(self, seconds: float, held: bool) -> float:
        state = self.load()
        budget = state["shared_budget"]
        if budget is None or budget["in_progress"] is None:
            raise BackfillStateError("backfill budget has no active day")
        budget["spent_seconds"] += seconds
        budget["held"] = budget["held"] or held
        budget["in_progress"] = None
        self.save(state)
        return budget["spent_seconds"]


def _empty_state() -> dict[str, Any]:
    return {
        "stop_flag": None, "today_stops": {}, "cause_stops": {}, "failed_streak": {},
        "review": {}, "review_clear_audit": {dataset: [] for dataset in REVIEW_DATASETS},
        "daily_history": {}, "daily_stops": [], "shared_budget": None,
    }


def _validate_date(value: Any) -> None:
    if not isinstance(value, str) or date.fromisoformat(value).isoformat() != value:
        raise ValueError


def _validate_execution_state(state: dict[str, Any]) -> None:
    if set(state) != set(_empty_state()):
        raise ValueError
    flag = state["stop_flag"]
    if flag is not None:
        if not isinstance(flag, dict) or set(flag) != {"scope", "reason", "set_on"}:
            raise ValueError
        if flag["scope"] not in ("ALL_FLAG", "BACKFILL_FLAG"):
            raise ValueError
        if not isinstance(flag["reason"], str) or flag["reason"] not in _STOP_REASONS:
            raise ValueError
        _validate_date(flag["set_on"])
    review = state["review"]
    if not isinstance(review, dict):
        raise ValueError
    for kind, datasets in review.items():
        if kind not in REVIEW_KINDS or not isinstance(datasets, dict):
            raise ValueError
        for dataset, days in datasets.items():
            if dataset not in REVIEW_DATASETS or not isinstance(days, list):
                raise ValueError
            for value in days:
                _validate_date(value)
            if len(days) != len(set(days)):
                raise ValueError
    audit = state["review_clear_audit"]
    if not isinstance(audit, dict) or set(audit) != set(REVIEW_DATASETS):
        raise ValueError
    expected_fields = {"target_date", "operation_date", "reason_code"}
    for events in audit.values():
        if not isinstance(events, list):
            raise ValueError
        for event in events:
            if not isinstance(event, dict) or set(event) != expected_fields:
                raise ValueError
            if not isinstance(event["reason_code"], str) or event["reason_code"] not in REVIEW_KINDS:
                raise ValueError
            _validate_date(event["target_date"])
            _validate_date(event["operation_date"])
    for key in ("today_stops", "cause_stops"):
        if not isinstance(state[key], dict):
            raise ValueError
        for dataset, events in state[key].items():
            if dataset not in REVIEW_DATASETS or not isinstance(events, list):
                raise ValueError
            for event in events:
                if not isinstance(event, dict) or set(event) != {"date", "reason"}:
                    raise ValueError
                _validate_date(event["date"])
                if event["reason"] not in _STOP_REASONS:
                    raise ValueError
    if not isinstance(state["failed_streak"], dict):
        raise ValueError
    for dataset, count in state["failed_streak"].items():
        if dataset not in REVIEW_DATASETS or type(count) is not int or count < 0:
            raise ValueError
    if not isinstance(state["daily_history"], dict):
        raise ValueError
    for dataset, events in state["daily_history"].items():
        if dataset not in REVIEW_DATASETS or not isinstance(events, list):
            raise ValueError
        for event in events:
            if not isinstance(event, dict) or set(event) != {"date", "status", "reason"}:
                raise ValueError
            _validate_date(event["date"])
            if event["status"] not in ("COMPLETE", "ATTENTION", "FAILED"):
                raise ValueError
            if event["reason"] not in DAILY_REASONS | {"NONE"}:
                raise ValueError
    if not isinstance(state["daily_stops"], list):
        raise ValueError
    for event in state["daily_stops"]:
        if not isinstance(event, dict) or set(event) != {"date", "dataset", "scope", "reason"}:
            raise ValueError
        _validate_date(event["date"])
        if event["dataset"] not in REVIEW_DATASETS or event["scope"] not in ("STOP_JOB", "STOP_TODAY"):
            raise ValueError
        if event["reason"] not in DAILY_REASONS:
            raise ValueError
    budget = state["shared_budget"]
    if budget is not None:
        if not isinstance(budget, dict) or set(budget) != {
            "date", "spent_seconds", "limit_seconds", "held", "in_progress",
        }:
            raise ValueError
        _validate_date(budget["date"])
        for key in ("spent_seconds", "limit_seconds"):
            value = budget[key]
            if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
                raise ValueError
        if not 0 < budget["limit_seconds"] <= 7200 or type(budget["held"]) is not bool:
            raise ValueError
        active = budget["in_progress"]
        if active is not None:
            if not isinstance(active, dict) or set(active) != {"dataset", "target_date"}:
                raise ValueError
            if active["dataset"] not in REVIEW_DATASETS:
                raise ValueError
            _validate_date(active["target_date"])


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()
