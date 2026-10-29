"""日次更新ジョブ: 対象日の決定 → 日足 → 分足の取得 → 検証。docs/detail_design/18 §1・§2。

取得そのものは単日取込（ingest_jquants_single_day の usecase）へ委ね、
ここでは対象日の計画と終了状態に応じた停止条件だけを扱う。他のデータ源へは切り替えない。
停止フラグの永続化・全体ロック・スケジューラ登録は対象外（結果で必要性を返すだけ）。
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import date, time
from typing import Any, Protocol

from src.application.jquants_canonical_input import BusinessCalendarPort
from src.domain.jquants_daily_plan import DailyDecision, plan_daily
from src.domain.jquants_daily_stop_policy import StopAction, decide_stop

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
                }
                for o in self.outcomes
            ],
        }


class JQuantsDailyJob:
    def __init__(
        self,
        calendar: BusinessCalendarPort,
        ingestion: SingleDayIngestionPort,
        history: FailureHistoryPort,
        verify: Verifier | None = None,
    ) -> None:
        self._calendar = calendar
        self._ingestion = ingestion
        self._history = history
        self._verify = verify

    def run(
        self, run_date: date, now: time, publish_after: time | None = None
    ) -> DailyJobResult:
        categories: Mapping[date, int | None] = self._calendar.categories(run_date, run_date)
        plan = plan_daily(run_date, now, categories, publish_after)
        result = DailyJobResult(run_date, plan.decision)
        if plan.target_date is None:
            return result
        blocked: StopAction | None = None
        for dataset in DATASET_ORDER:
            if blocked is not None:
                result.outcomes.append(
                    DatasetOutcome(dataset, ran=False, stop_action=blocked, reason="STOPPED_EARLIER")
                )
                continue
            outcome = self._run_dataset(dataset, plan.target_date)
            result.outcomes.append(outcome)
            if outcome.stop_action in (StopAction.STOP_TODAY, StopAction.HALT_FLAG):
                blocked = outcome.stop_action
        return result

    def _run_dataset(self, dataset: str, target: date) -> DatasetOutcome:
        prior_failed = self._history.consecutive_failed(dataset)
        summary = self._ingestion.run(dataset=dataset, date=target.isoformat())
        decision = decide_stop(summary.status, summary.error_category, prior_failed)
        verdict, reasons = (None, ())
        if self._verify is not None and decision.action is not StopAction.SKIP_LOCKED:
            verdict, reasons = self._verify(summary)
        return DatasetOutcome(
            dataset, ran=True, status=summary.status, error_category=summary.error_category,
            exit_code=None if decision.action is StopAction.SKIP_LOCKED else summary.exit_code,
            verdict=verdict, verdict_reasons=reasons,
            stop_action=decision.action, reason=decision.reason,
        )
