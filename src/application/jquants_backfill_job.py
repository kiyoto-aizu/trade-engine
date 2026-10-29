"""過去補填ジョブ（古い日から1日ずつ取り込み、停止条件で止める）。docs/detail_design/18 §2・§3・§4-1。

日付の計画は #45 の JQuantsBackfillPlanner、1日分の取込は JQuantsSingleDayIngestion を使う。
通信・時刻・容量・保持ストアはポートで受け取るため、このモジュールは実APIを呼ばない。
出力に含めるのは日付とカテゴリのみ（メッセージ・URL・認証情報は含めない）。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date
from typing import Protocol

from src.domain.jquants_backfill_plan import BackfillPlan
from src.domain.jquants_backfill_policy import (
    BACKFILL_BUDGET_SECONDS,
    Action,
    is_budget_exceeded,
    is_disk_insufficient,
    judge_day,
)
from src.domain.jquants_contract_range import is_within_contract, sort_backfill_dates


class DaySummary(Protocol):
    status: str
    error_category: str | None


class PlannerPort(Protocol):
    def plan(self, dataset: str, start: date, end: date, today: date) -> BackfillPlan: ...


class IngestionPort(Protocol):
    def run(self, *, dataset: str, date: str) -> DaySummary: ...


class HoldStorePort(Protocol):
    def held(self, dataset: str) -> Mapping[date, str]:
        """403要確認・契約範囲外として再開対象から外している日付と種別。"""

    def record(self, dataset: str, target_date: date, kind: str) -> None: ...


class FreeSpacePort(Protocol):
    def free_bytes(self) -> int: ...


class ClockPort(Protocol):
    def today(self) -> date:
        """JSTの実行日。"""

    def monotonic(self) -> float: ...


@dataclass
class BackfillJobResult:
    dataset: str
    days: list[tuple[date, str, str | None]] = field(default_factory=list)
    skipped_out_of_contract: list[date] = field(default_factory=list)
    holds: list[tuple[date, str]] = field(default_factory=list)
    stop_action: Action | None = None
    stop_reason: str | None = None

    @property
    def exit_code(self) -> int:
        """0=完了、1=失敗あり、2=要確認あり、3=429打ち切り、4=補填停止。"""
        if self.stop_reason == "RATE_LIMITED":
            return 3
        if self.stop_action in (Action.STOP_BACKFILL, Action.END_BACKFILL):
            return 4
        if self.stop_reason == "CONSECUTIVE_FAILED" or any(s == "FAILED" for _, s, _ in self.days):
            return 1
        if self.holds or any(s == "ATTENTION" for _, s, _ in self.days):
            return 2
        return 0

    def as_dict(self) -> dict[str, object]:
        return {
            "dataset": self.dataset,
            "days": [
                {"date": day.isoformat(), "status": status, "error_category": category}
                for day, status, category in self.days
            ],
            "skipped_out_of_contract": [d.isoformat() for d in self.skipped_out_of_contract],
            "holds": [{"date": d.isoformat(), "kind": kind} for d, kind in self.holds],
            "stop_action": None if self.stop_action is None else self.stop_action.value,
            "stop_reason": self.stop_reason,
            "exit_code": self.exit_code,
        }


class JQuantsBackfillJob:
    def __init__(
        self, planner: PlannerPort, ingestion: IngestionPort, holds: HoldStorePort,
        free_space: FreeSpacePort, clock: ClockPort,
    ) -> None:
        self._planner = planner
        self._ingestion = ingestion
        self._holds = holds
        self._free_space = free_space
        self._clock = clock

    def run(
        self, dataset: str, start: date, end: date, *, max_days: int | None = None,
        budget_seconds: float = BACKFILL_BUDGET_SECONDS,
    ) -> BackfillJobResult:
        if max_days is not None and max_days <= 0:
            raise ValueError("max_days must be positive")
        started = self._clock.monotonic()
        today = self._clock.today()
        plan = self._planner.plan(dataset, start, end, today)
        held = self._holds.held(dataset)
        targets = [d for d in sort_backfill_dates(dataset, list(plan.targets), today)
                   if d not in held]
        if max_days is not None:
            targets = targets[:max_days]
        result = BackfillJobResult(dataset)
        consecutive_failed = 0
        for day in targets:
            if is_budget_exceeded(self._clock.monotonic() - started, budget_seconds):
                result.stop_action, result.stop_reason = Action.STOP_TODAY, "BUDGET_EXCEEDED"
                break
            if is_disk_insufficient(self._free_space.free_bytes()):
                result.stop_action, result.stop_reason = Action.STOP_BACKFILL, "DISK_SPACE_LOW"
                break
            # 日付またぎで範囲が動くため、送信直前に今日の日付で再判定する（設計書18 §2-2）。
            today = self._clock.today()
            if not is_within_contract(dataset, day, today):
                result.skipped_out_of_contract.append(day)
                continue
            summary = self._ingestion.run(dataset=dataset, date=day.isoformat())
            verdict = judge_day(
                dataset, day, today, summary.status, summary.error_category, consecutive_failed
            )
            consecutive_failed = verdict.consecutive_failed
            result.days.append((day, summary.status, summary.error_category))
            if verdict.hold_kind is not None:
                self._holds.record(dataset, day, verdict.hold_kind)
                result.holds.append((day, verdict.hold_kind))
            if verdict.action is not Action.CONTINUE:
                result.stop_action, result.stop_reason = verdict.action, verdict.reason
                break
        return result
