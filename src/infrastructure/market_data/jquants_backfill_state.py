"""File-based global lock and stop state for the J-Quants backfill (doc 18 §1-1, §2-1).

Holds only enumerated codes, dates and counters. No paths, symbols or free text.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import date, datetime, timezone
import json
import os
from pathlib import Path
from typing import Any

from src.domain.jquants_backfill_policy import StopReason

LOCK_FILE = "backfill.lock"
STATE_FILE = "backfill_state.json"
REVIEW_KINDS = ("PERMISSION_403", "OUT_OF_CONTRACT_400", "UNEXPECTED_400_IN_RANGE", "UNKNOWN_STATUS")
_STOP_REASONS = frozenset(reason.value for reason in StopReason)


class BackfillStateError(Exception):
    pass


class BackfillLockedError(BackfillStateError):
    """Another run holds the global lock. Never wait for it or take it over."""


class JQuantsBackfillStateStore:
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
        self._directory.mkdir(parents=True, exist_ok=True)
        path = self._directory / STATE_FILE
        temporary = path.with_suffix(".tmp")
        try:
            temporary.write_text(json.dumps(state, sort_keys=True), encoding="utf-8")
            os.replace(temporary, path)
        except OSError:
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


def _empty_state() -> dict[str, Any]:
    return {
        "stop_flag": None, "today_stops": {}, "cause_stops": {}, "failed_streak": {}, "review": {},
    }


def _validate_date(value: Any) -> None:
    if not isinstance(value, str) or date.fromisoformat(value).isoformat() != value:
        raise ValueError


def _validate_execution_state(state: dict[str, Any]) -> None:
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
            if dataset not in ("daily", "minute") or not isinstance(days, list):
                raise ValueError
            for day in days:
                _validate_date(day)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()
