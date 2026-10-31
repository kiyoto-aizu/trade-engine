"""日次更新ジョブ: 対象日の決定 → 日足 → 分足の取得 → 検証。docs/detail_design/18 §1・§2。

取得そのものは単日取込（ingest_jquants_single_day の usecase）へ委ね、
ここでは対象日の計画と終了状態に応じた停止条件だけを扱う。他のデータ源へは切り替えない。
既存の全体ロックと停止フラグを再利用し、日次固有の停止・失敗を同じstateへ分離して保存する。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import date, datetime, time, timedelta
from typing import Any, Protocol

from src.application.jquants_canonical_input import BusinessCalendarPort
from src.application.jquants_execution_guard import (
    JQuantsExecutionBlockedError,
    JQuantsExecutionGuard,
)
from src.domain.jquants_daily_plan import DailyDecision, plan_daily
from src.domain.jquants_daily_stop_policy import StopAction, decide_stop, repeated_daily_failure_stop
from src.domain.jquants_disk_space import DiskVerdict
from src.infrastructure.market_data.jquants_backfill_state import (
    BackfillLockedError,
    JQuantsBackfillStateStore,
    DAILY_REASONS,
    BackfillStateError,
)

DATASET_ORDER = ("daily", "minute")  # 日足 → 分足（同時に走らせない）
_EXIT_SEVERITY = {0: 0, 2: 1, 1: 2, 3: 3}


class IngestionSummaryLike(Protocol):
    status: str
    error_category: str | None
    exit_code: int


class SingleDayIngestionPort(Protocol):
    def run(self, *, dataset: str, date: str) -> IngestionSummaryLike: ...


class FailureHistoryPort(Protocol):
    def consecutive_failed(self, dataset: str) -> int:
        """実行前時点の同一 dataset の連続 FAILED 回数（日付をまたいで数える）（ロック失敗は数えず、途切れさせない）。"""


Verifier = Callable[[IngestionSummaryLike], tuple[str, tuple[str, ...]]]
EVALUATION_REASONS = frozenset({
    "METRICS_UNREADABLE", "ERROR_CATEGORY_INVALID", "WARNINGS_PRESENT", "UNKNOWN_FIELDS_SEEN",
    "SYMBOL_COUNT_ZERO", "RAW_SOURCE_INVALID", "DISK_SPACE_CRITICAL", "DISK_SPACE_LOW",
    "STATUS_ATTENTION", "STATUS_FAILED", "STATUS_INVALID", "RATE_LIMIT_ABORTED",
    "AUTH_OR_CONTRACT_ERROR", "HTTP_400_SEEN", "REQUEST_METRICS_UNMEASURED",
    "FETCH_WITHOUT_REQUESTS", "RATE_LIMIT_429_HIGH", "RATE_LIMIT_429_SEEN",
    "OTHER_RETRIES_HIGH", "OTHER_RETRIES_ELEVATED", "ELAPSED_LIMIT_UNSET",
    "ELAPSED_UNMEASURED", "ELAPSED_OVER_LIMIT", "ELAPSED_NEAR_LIMIT",
}) | frozenset(f"ERROR_CATEGORY_{reason}" for reason in DAILY_REASONS)
EVALUATION_ITEMS = frozenset({
    "WARNINGS", "UNKNOWN_FIELDS", "SYMBOL_COUNT", "DISK_SPACE", "API_HEALTH",
    "EVALUATION_UNAVAILABLE", "METRICS",
})


@dataclass(frozen=True)
class DatasetOutcome:
    dataset: str
    ran: bool
    status: str | None = None
    error_category: str | None = None
    exit_code: int | None = None
    verdict: str | None = None
    verdict_reasons: tuple[str, ...] = ()
    stop_action: StopAction = StopAction.CONTINUE
    reason: str | None = None
    not_evaluated: tuple[str, ...] = ("EVALUATION_UNAVAILABLE",)
    stop_scope: str = "UNKNOWN"
    notification_failed: bool = False
    metrics_persistence_failed: bool = False


@dataclass
class DailyJobResult:
    run_date: date
    decision: DailyDecision
    outcomes: list[DatasetOutcome] = field(default_factory=list)

    @property
    def halt_flag_required(self) -> bool:
        return any(o.stop_action is StopAction.HALT_FLAG for o in self.outcomes)

    @property
    def exit_code(self) -> int:
        if self.decision is DailyDecision.CALENDAR_MISSING:
            return 1
        codes = [o.exit_code for o in self.outcomes if o.exit_code is not None]
        if any(o.stop_action is StopAction.SKIP_LOCKED for o in self.outcomes):
            codes.append(2)
        return max(codes, key=_EXIT_SEVERITY.__getitem__, default=0)

    def as_dict(self) -> dict[str, Any]:
        return {
            "run_date": self.run_date.isoformat(),
            "decision": self.decision.value,
            "halt_flag_required": self.halt_flag_required,
            "exit_code": self.exit_code,
            "outcomes": [
                {
                    "dataset": o.dataset, "ran": o.ran, "status": o.status,
                    "error_category": o.error_category, "exit_code": o.exit_code,
                    "verdict": o.verdict, "verdict_reasons": list(o.verdict_reasons),
                    "stop_action": o.stop_action.value, "reason": o.reason,
                    "not_evaluated": list(o.not_evaluated),
                    "stop_scope": o.stop_scope,
                    "notification_failed": o.notification_failed,
                    "metrics_persistence_failed": o.metrics_persistence_failed,
                }
                for o in self.outcomes
            ],
        }


class JQuantsDailyJob:
    def __init__(
        self,
        calendar: BusinessCalendarPort,
        ingestion_factory: Callable[[], SingleDayIngestionPort],
        history: FailureHistoryPort | None = None,
        verify: Verifier | None = None,
        *,
        guard: JQuantsExecutionGuard,
        disk_check: Callable[[str], DiskVerdict | None] | None = None,
    ) -> None:
        self._calendar = calendar
        self._ingestion_factory = ingestion_factory
        self._ingestion: SingleDayIngestionPort | None = None
        self._verify = verify
        self._guard = guard
        self._disk_check = disk_check

    def run(
        self, run_date: date, now: datetime, publish_after: time | None = None
    ) -> DailyJobResult:
        try:
            with self._guard.locked() as state:
                return self.run_locked(run_date, now, state, publish_after)
        except JQuantsExecutionBlockedError as exc:
            reason = exc.reason
            category = "BLOCKED_BY_FLAG"
        except BackfillLockedError:
            reason = "GLOBAL_ALREADY_RUNNING"
            category = "GLOBAL_ALREADY_RUNNING"
        result = DailyJobResult(run_date, DailyDecision.TARGET)
        result.outcomes = [
            DatasetOutcome(
                dataset, ran=False, exit_code=2, error_category=category, reason=reason,
                stop_scope="ALL_FLAG" if category == "BLOCKED_BY_FLAG" else "UNKNOWN",
            )
            for dataset in DATASET_ORDER
        ]
        return result

    def run_locked(
        self, run_date: date, now: datetime, state: JQuantsBackfillStateStore,
        publish_after: time | None = None,
    ) -> DailyJobResult:
        categories = self._calendar.categories(run_date, run_date)
        plan = plan_daily(run_date, now, categories, publish_after)
        result = DailyJobResult(run_date, plan.decision)
        if plan.target_date is None:
            return result
        return self._run_locked(result, plan.target_date, state, now.date())

    def _run_locked(
        self, result: DailyJobResult, target: date, state: JQuantsBackfillStateStore,
        execution_day: date,
    ) -> DailyJobResult:
        blocked: StopAction | None = None
        for dataset in DATASET_ORDER:
            if blocked is not None:
                result.outcomes.append(
                    DatasetOutcome(dataset, ran=False, stop_action=blocked, reason="STOPPED_EARLIER")
                )
                continue
            daily_stop = state.daily_stop(dataset, execution_day)
            if daily_stop is not None:
                result.outcomes.append(DatasetOutcome(
                    dataset, ran=False, exit_code=2, error_category="DAILY_STOPPED",
                    reason=daily_stop,
                ))
                continue
            if target in state.review_dates(dataset):
                result.outcomes.append(DatasetOutcome(
                    dataset, ran=False, exit_code=2, error_category="REVIEW_EXCLUDED",
                    reason="REVIEW_EXCLUDED",
                ))
                continue
            history = state.daily_history(dataset)
            prior_failed = 0
            for event in reversed(history):
                if event["status"] != "FAILED":
                    break
                prior_failed += 1
            outcome = self._run_dataset(dataset, target, prior_failed)
            result.outcomes.append(outcome)
            if outcome.stop_action is not StopAction.SKIP_LOCKED:
                try:
                    self._persist_outcome(state, dataset, execution_day, outcome)
                except BackfillStateError:
                    result.outcomes.append(DatasetOutcome(
                        dataset, ran=False, status="FAILED", exit_code=1,
                        error_category="STOP_STATE_SAVE_FAILED", reason="STOP_STATE_SAVE_FAILED",
                    ))
                    break
            result.outcomes[-1] = replace(
                outcome, stop_scope=(state.stop_flag() or {}).get("scope", "NONE"),
            )
            if outcome.metrics_persistence_failed:
                result.outcomes.append(DatasetOutcome(
                    dataset, ran=False, status="FAILED", exit_code=1,
                    error_category="METRICS_SAVE_FAILED", reason="METRICS_SAVE_FAILED",
                    stop_scope=(state.stop_flag() or {}).get("scope", "NONE"),
                ))
            if outcome.stop_action in (StopAction.STOP_TODAY, StopAction.HALT_FLAG):
                blocked = outcome.stop_action
        return result

    def _persist_outcome(self, state, dataset, execution_day, outcome) -> None:
        reason = outcome.error_category or "NONE"
        if reason == "PERMISSION_OR_CONTRACT_ERROR":
            reason = "PERMISSION_ERROR"
        elif reason not in DAILY_REASONS | {"NONE"}:
            reason = "UNKNOWN_ERROR"
        scope = outcome.stop_action.value if outcome.stop_action in (
            StopAction.STOP_JOB, StopAction.STOP_TODAY,
        ) else None
        stop_reason = "CONSECUTIVE_FAILED" if outcome.reason == "CONSECUTIVE_FAILED_3" else reason
        state.record_daily_result(
            dataset, execution_day, outcome.status or "FAILED", reason,
            stop_scope=scope, stop_reason=stop_reason,
            halt_reason=reason if outcome.stop_action is StopAction.HALT_FLAG else None,
        )
        if stop_reason == "CONSECUTIVE_FAILED" and scope == "STOP_TODAY":
            history = state.daily_history(dataset)
            stops = state.daily_stop_history(dataset)
            earliest = min(date.fromisoformat(item["date"]) for item in stops)
            categories = self._calendar.categories(earliest - timedelta(days=7), execution_day)
            if repeated_daily_failure_stop(stops, history, categories, execution_day):
                state.set_stop_flag("ALL_FLAG", "REPEATED_TODAY_STOP", execution_day)

    def _ingest(self) -> SingleDayIngestionPort:
        # 休場日・未公開では RawStore 等を作らない（初期化失敗を避ける）
        if self._ingestion is None:
            self._ingestion = self._ingestion_factory()
        return self._ingestion

    def _run_dataset(self, dataset: str, target: date, prior_failed: int) -> DatasetOutcome:
        try:
            if self._disk_check is not None:
                try:
                    disk = self._disk_check(dataset)
                except Exception:
                    disk = None
                if disk is None or disk is DiskVerdict.STOP:
                    return DatasetOutcome(
                        dataset, ran=False, status="FAILED", exit_code=1,
                        error_category="DISK_UNMEASURABLE" if disk is None else "DISK_SPACE_CRITICAL",
                        stop_action=StopAction.STOP_TODAY if disk is None else StopAction.HALT_FLAG,
                        reason="DISK_UNMEASURABLE" if disk is None else "DISK_SPACE_CRITICAL",
                    )
            summary = self._ingest().run(dataset=dataset, date=target.isoformat())
        except Exception:
            decision = decide_stop("FAILED", "STORAGE_OR_FORMAT_ERROR", prior_failed)
            return DatasetOutcome(
                dataset, ran=True, status="FAILED", error_category="STORAGE_OR_FORMAT_ERROR",
                exit_code=1, stop_action=decision.action, reason=decision.reason,
            )
        status = summary.status if summary.status in ("COMPLETE", "ATTENTION", "FAILED") else "FAILED"
        category = summary.error_category
        if category is not None and category not in DAILY_REASONS | {
            "PERMISSION_OR_CONTRACT_ERROR", "DATASET_ALREADY_RUNNING",
        }:
            category = "UNKNOWN_ERROR"
        decision = decide_stop(status, category, prior_failed)
        verdict, reasons, not_evaluated = (None, (), ("EVALUATION_UNAVAILABLE",))
        if self._verify is not None and decision.action is not StopAction.SKIP_LOCKED:
            try:
                evaluation = self._verify(summary)
            except Exception:
                evaluation = ("No-Go", ("METRICS_UNREADABLE",))
            if hasattr(evaluation, "verdict"):
                verdict, reasons = evaluation.verdict, evaluation.reasons
                not_evaluated = evaluation.not_evaluated
            else:
                verdict, reasons = evaluation
            if verdict not in ("Go", "Hold", "No-Go") or any(
                reason not in EVALUATION_REASONS for reason in reasons
            ) or any(item not in EVALUATION_ITEMS for item in not_evaluated):
                verdict, reasons, not_evaluated = "No-Go", ("METRICS_UNREADABLE",), ("METRICS",)
        code = summary.exit_code
        if type(code) is not int or code not in _EXIT_SEVERITY:
            code = 1
        return DatasetOutcome(
            dataset, ran=decision.action is not StopAction.SKIP_LOCKED,
            status=status, error_category=category,
            exit_code=None if decision.action is StopAction.SKIP_LOCKED else code,
            verdict=verdict, verdict_reasons=reasons,
            stop_action=decision.action, reason=decision.reason,
            not_evaluated=not_evaluated,
            notification_failed="SLACK_NOTIFY_FAILED" in getattr(summary, "warnings", ()),
            metrics_persistence_failed=getattr(summary, "metrics_persistence_failed", False),
        )
