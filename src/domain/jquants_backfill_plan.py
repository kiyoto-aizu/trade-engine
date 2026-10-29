"""補填対象の日付計画（純関数、I/Oなし）。docs/detail_design/18 §3-6 に対応する。

営業日カレンダーで休場（区分0・3）と判定した日は、補填対象から外して「休場」として区別する。
台帳には書かない（台帳は取得試行の記録で、取得していない日の記録を混ぜない）。
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import date, timedelta

from src.domain.jquants_consumer_contract import HOLIDAY_CATEGORIES, business_days_from_calendar
from src.domain.jquants_contract_range import is_within_contract

IN_PROGRESS_STATES = frozenset({"RUNNING"})
ATTENTION_STATES = frozenset({"EMPTY_UNKNOWN", "EMPTY_HELD"})
COMPLETED_STATES = frozenset({"COMPLETE"})


@dataclass(frozen=True)
class BackfillPlan:
    targets: tuple[date, ...]
    holidays: tuple[date, ...]
    completed: tuple[date, ...]
    in_progress: tuple[date, ...]
    attention: tuple[date, ...]
    out_of_contract: tuple[date, ...]
    holiday_with_ledger: tuple[date, ...]  # 休場なのに台帳に記録がある日（変更せず警告のみ）

    def as_dict(self) -> dict[str, list[str]]:
        return {
            name: [d.isoformat() for d in getattr(self, name)]
            for name in (
                "targets", "holidays", "completed", "in_progress", "attention",
                "out_of_contract", "holiday_with_ledger",
            )
        }


def plan_backfill(
    dataset: str,
    start: date,
    end: date,
    today: date,
    categories: Mapping[date, int | None],
    ledger_state: Callable[[date], str | None],
) -> BackfillPlan:
    """期間内の各日を分類する。カレンダーが期間を覆わなければ失敗する（ConsumerContractError）。"""
    if start > end:
        raise ValueError("start must not be after end")
    business = business_days_from_calendar(categories, start, end)
    buckets: dict[str, list[date]] = {
        "targets": [], "holidays": [], "completed": [], "in_progress": [],
        "attention": [], "out_of_contract": [], "holiday_with_ledger": [],
    }
    for offset in range((end - start).days + 1):
        day = start + timedelta(days=offset)
        if not is_within_contract(dataset, day, today):
            buckets["out_of_contract"].append(day)
            continue
        state = ledger_state(day)
        if day not in business:
            if categories.get(day) in HOLIDAY_CATEGORIES:
                buckets["holidays"].append(day)
                if state is not None:
                    buckets["holiday_with_ledger"].append(day)
                continue
            raise ValueError("unclassifiable calendar category")
        if state in COMPLETED_STATES:
            buckets["completed"].append(day)
        elif state in IN_PROGRESS_STATES:
            buckets["in_progress"].append(day)
        elif state in ATTENTION_STATES:
            buckets["attention"].append(day)
        else:
            buckets["targets"].append(day)
    return BackfillPlan(**{k: tuple(v) for k, v in buckets.items()})
