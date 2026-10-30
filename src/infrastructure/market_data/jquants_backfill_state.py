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

LOCK_FILE = "backfill.lock"
STATE_FILE = "backfill_state.json"
REVIEW_KINDS = ("PERMISSION_403", "OUT_OF_CONTRACT_400", "UNEXPECTED_400_IN_RANGE", "UNKNOWN_STATUS")


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
        if not path.exists():
            return _empty_state()
        try:
            state = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(state, dict):
                raise ValueError
            return {**_empty_state(), **state}
        except (OSError, ValueError):
            # A damaged state must stop the run, not silently reset the stop flags.
            raise BackfillStateError("backfill state is unreadable") from None

    def save(self, state: dict[str, Any]) -> None:
        self._directory.mkdir(parents=True, exist_ok=True)
        path = self._directory / STATE_FILE
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(state, sort_keys=True), encoding="utf-8")
        os.replace(temporary, path)

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

    def today_stop_dates(self, reason: str | None = None) -> list[tuple[date, str]]:
        return [
            (date.fromisoformat(item["date"]), item["reason"])
            for item in self.load()["today_stops"]
            if reason is None or item["reason"] == reason
        ]

    def record_today_stop(self, day: date, reason: str) -> None:
        state = self.load()
        if not any(i["date"] == day.isoformat() for i in state["today_stops"]):
            state["today_stops"].append({"date": day.isoformat(), "reason": reason})
            state["today_stops"] = state["today_stops"][-10:]
        self.save(state)

    def reset_today_stops(self) -> None:
        """停止しなかった走行日が挟まったら「連続」を途切れさせる（設計書18 §2 の3営業日連続）。"""
        state = self.load()
        if state["today_stops"]:
            state["today_stops"] = []
            self.save(state)

    def is_stopped_today(self, day: date) -> bool:
        return any(d == day for d, _ in self.today_stop_dates())

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
    return {"stop_flag": None, "today_stops": [], "failed_streak": {}, "review": {}}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()
