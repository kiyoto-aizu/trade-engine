"""Past-data backfill job (doc 18 §1, §2, §4-2). One day at a time, oldest first."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol

from src.application.jquants_single_day_ingestion import JQuantsIngestionSummary
from src.domain.jquants_backfill_plan import BackfillPlan
from src.domain.jquants_backfill_policy import (
    DAILY_BUDGET_SECONDS,
    DayObservation,
    StopReason,
    StopScope,
    decide_after_day,
    effective_budget_seconds,
    is_repeated_today_stop,
    next_failed_streak,
)
from src.domain.jquants_contract_range import is_within_contract
from src.domain.jquants_disk_space import (
    DEFAULT_RUN_BYTES,
    DiskVerdict,
    VolumeNeed,
    backfill_may_start,
    judge_volumes,
)
from src.infrastructure.market_data.jquants_backfill_state import (
    BackfillLockedError,
    JQuantsBackfillStateStore,
)
from src.infrastructure.market_data.jquants_disk_space import measure_volume

JST = timezone(timedelta(hours=9))


def jst_today() -> date:
    return datetime.now(JST).date()


class BackfillOutcome(StrEnum):
    COMPLETED = "COMPLETED"
    COMPLETED_WITH_REVIEW = "COMPLETED_WITH_REVIEW"  # done, but dates await a human check
    BUDGET_EXHAUSTED = "BUDGET_EXHAUSTED"  # normal end; the next run continues
    BLOCKED_BY_FLAG = "BLOCKED_BY_FLAG"
    STOPPED_TODAY = "STOPPED_TODAY"
    LOCK_SKIPPED = "LOCK_SKIPPED"
    STOPPED = "STOPPED"


_EXIT_CODES = {
    BackfillOutcome.COMPLETED: 0,
    BackfillOutcome.COMPLETED_WITH_REVIEW: 0,
    BackfillOutcome.BUDGET_EXHAUSTED: 0,
    BackfillOutcome.BLOCKED_BY_FLAG: 2,
    BackfillOutcome.STOPPED_TODAY: 2,
    BackfillOutcome.LOCK_SKIPPED: 2,
    BackfillOutcome.STOPPED: 1,
}


class _Planner(Protocol):
    def plan(self, dataset: str, start: date, end: date, today: date) -> BackfillPlan: ...


class _Ingestion(Protocol):
    def run(self, *, dataset: str, date: str) -> JQuantsIngestionSummary: ...


@dataclass
class BackfillResult:
    dataset: str
    outcome: BackfillOutcome
    reason: StopReason | None = None
    processed: list[date] = field(default_factory=list)
    skipped_out_of_range: list[date] = field(default_factory=list)
    attention: list[date] = field(default_factory=list)
    review_excluded: list[date] = field(default_factory=list)
    remaining_targets: int = 0
    spent_seconds: float = 0.0

    @property
    def exit_code(self) -> int:
        return _EXIT_CODES[self.outcome]

    def as_dict(self) -> dict[str, Any]:
        return {
            "dataset": self.dataset,
            "outcome": self.outcome.value,
            "reason": None if self.reason is None else self.reason.value,
            "processed": [d.isoformat() for d in self.processed],
            "skipped_out_of_range": [d.isoformat() for d in self.skipped_out_of_range],
            "attention": [d.isoformat() for d in self.attention],
            "review_excluded": [d.isoformat() for d in self.review_excluded],
            "remaining_targets": self.remaining_targets,
            "spent_seconds": round(self.spent_seconds, 1),
        }


class JQuantsBackfillUseCase:
    def __init__(
        self,
        planner: _Planner,
        ingestion: _Ingestion,
        state: JQuantsBackfillStateStore,
        *,
        raw_directory: Path | str,
        canonical_directory: Path | str,
        today_provider: Callable[[], date] = jst_today,
        volume_probe: Callable[[Path | str], tuple[int, int]] = measure_volume,
        budget_seconds: float = DAILY_BUDGET_SECONDS,
    ) -> None:
        self._planner = planner
        self._ingestion = ingestion
        self._state = state
        self._raw_directory = raw_directory
        self._canonical_directory = canonical_directory
        self._today_provider = today_provider
        self._volume_probe = volume_probe
        self._budget_seconds = budget_seconds
        self._max_days: int | None = None
    def run(
        self, dataset: str, start: date, end: date, *, max_days: int | None = None
    ) -> BackfillResult:
        self._max_days = max_days
        today = self._today_provider()
        result = BackfillResult(dataset, BackfillOutcome.COMPLETED)
        try:
            with self._state.global_lock():
                blocked = self._blocked_outcome(today)
                if blocked is not None:
                    result.outcome = blocked
                    return result
                return self._run_locked(dataset, start, end, today, result)
        except BackfillLockedError:
            result.outcome = BackfillOutcome.LOCK_SKIPPED
            return result

    def _blocked_outcome(self, today: date) -> BackfillOutcome | None:
        if self._state.stop_flag() is not None:
            return BackfillOutcome.BLOCKED_BY_FLAG
        if self._state.is_stopped_today(today):
            return BackfillOutcome.STOPPED_TODAY
        return None

    def _run_locked(
        self, dataset: str, start: date, end: date, today: date, result: BackfillResult
    ) -> BackfillResult:
        plan = self._planner.plan(dataset, start, end, today)
        reviewed = self._state.review_dates(dataset)
        result.review_excluded = sorted(d for d in reviewed if start <= d <= end)
        targets = sorted(d for d in plan.targets if d not in reviewed)
        result.attention = list(plan.attention)
        held = False
        sizes = dict(DEFAULT_RUN_BYTES[dataset])
        for index, day in enumerate(targets):
            result.remaining_targets = len(targets) - index
            now = self._today_provider()
            if not is_within_contract(dataset, day, now):
                # The range rolled over after planning; skip without sending a request.
                result.skipped_out_of_range.append(day)
                continue
            if result.spent_seconds >= effective_budget_seconds(self._budget_seconds, held) or (
                self._max_days is not None and len(result.processed) >= self._max_days
            ):
                result.outcome = BackfillOutcome.BUDGET_EXHAUSTED
                if result.processed:
                    self._state.reset_today_stops()
                return result
            disk = self._check_disk(sizes)
            if disk is None:
                self._stop(result, now, StopScope.END_JOB, StopReason.DISK_FLOOR)
                return result
            verdict, floor_ok = disk
            if verdict is DiskVerdict.STOP:
                self._stop(result, now, StopScope.ALL_FLAG, StopReason.DISK_SPACE_CRITICAL)
                return result
            if not floor_ok:
                self._stop(result, now, StopScope.BACKFILL_FLAG, StopReason.DISK_FLOOR)
                return result
            held = held or verdict is DiskVerdict.HOLD
            summary = self._ingestion.run(dataset=dataset, date=day.isoformat())
            result.processed.append(day)
            elapsed = float(summary.durations_seconds.get("total", 0.0))
            result.spent_seconds += elapsed
            if summary.raw_size_bytes > 0:
                sizes["raw"] = summary.raw_size_bytes
            if summary.canonical_size_bytes > 0:
                sizes["canonical"] = summary.canonical_size_bytes
            streak = next_failed_streak(self._state.failed_streak(dataset), summary.status)
            self._state.set_failed_streak(dataset, streak)
            metrics = summary.execution_metrics
            decision = decide_after_day(DayObservation(
                dataset=dataset, target_date=day, today=now, status=summary.status,
                error_category=summary.error_category, warnings=tuple(summary.warnings),
                rate_limit_429_count=_count(metrics.get("rate_limit_429_count")),
                retry_count=_count(metrics.get("retry_count")),
                elapsed_seconds=elapsed, failed_streak=streak,
            ))
            if summary.status == "ATTENTION" and day not in result.attention:
                result.attention.append(day)
            if decision.scope is not StopScope.NONE:
                assert decision.reason is not None
                self._record_review(dataset, day, decision.reason)
                result.remaining_targets = len(targets) - index - 1
                self._stop(result, now, decision.scope, decision.reason)
                return result
            held = held or decision.hold
        result.remaining_targets = 0
        if result.processed:
            self._state.reset_today_stops()
        if result.attention or plan.in_progress or result.review_excluded:
            result.outcome = BackfillOutcome.COMPLETED_WITH_REVIEW
        return result

    def _record_review(self, dataset: str, day: date, reason: StopReason) -> None:
        if reason is StopReason.PERMISSION_ERROR:
            self._state.mark_review("PERMISSION_403", dataset, day)
        elif reason in (
            StopReason.OUT_OF_CONTRACT_400, StopReason.UNEXPECTED_400_IN_RANGE,
            StopReason.UNKNOWN_STATUS,
        ):
            # 同じ日を次回も再送しないよう、400・未知statusも要確認に入れる。
            self._state.mark_review(reason.value, dataset, day)

    def _stop(
        self, result: BackfillResult, day: date, scope: StopScope, reason: StopReason
    ) -> None:
        result.outcome = BackfillOutcome.STOPPED
        result.reason = reason
        if scope is StopScope.TODAY:
            self._state.record_today_stop(day, reason.value)
            history = [(d, StopReason(r)) for d, r in self._state.today_stop_dates()]
            if is_repeated_today_stop(history, reason):
                self._state.set_stop_flag(
                    StopScope.BACKFILL_FLAG.value, StopReason.REPEATED_TODAY_STOP.value, day
                )
        elif scope in (StopScope.BACKFILL_FLAG, StopScope.ALL_FLAG):
            self._state.set_stop_flag(scope.value, reason.value, day)

    def _check_disk(self, sizes: dict[str, int]) -> tuple[DiskVerdict, bool] | None:
        """Return (verdict, floor_ok) before one day's fetch, or None when unmeasurable."""
        try:
            raw_free, raw_device = self._volume_probe(self._raw_directory)
            canonical_free, canonical_device = self._volume_probe(self._canonical_directory)
        except OSError:
            return None
        if raw_device == canonical_device:
            volumes = [(raw_free, sizes["raw"] + sizes["canonical"])]
        else:
            volumes = [(raw_free, sizes["raw"]), (canonical_free, sizes["canonical"])]
        needs = [VolumeNeed(free, needed) for free, needed in volumes]
        floor_ok = all(backfill_may_start(free, needed) for free, needed in volumes)
        return judge_volumes(needs), floor_ok


def _count(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0
