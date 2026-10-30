"""日次更新の対象日計画（純関数、I/Oなし）。docs/detail_design/18 §1 に対応する。

営業日カレンダー（区分1・2=営業日、0・3=休場）で実行日を分類する。
カレンダーに無い日は営業日扱いにせず、判定不能として区別する。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime, time
from enum import StrEnum

from src.domain.jquants_consumer_contract import BUSINESS_DAY_CATEGORIES, HOLIDAY_CATEGORIES


class DailyDecision(StrEnum):
    TARGET = "TARGET"
    HOLIDAY = "HOLIDAY"
    NOT_PUBLISHED = "NOT_PUBLISHED"  # 営業日だが公開予定時刻前。休場とは区別する
    CALENDAR_MISSING = "CALENDAR_MISSING"


@dataclass(frozen=True)
class DailyPlan:
    run_date: date
    decision: DailyDecision

    @property
    def target_date(self) -> date | None:
        return self.run_date if self.decision is DailyDecision.TARGET else None


def plan_daily(
    run_date: date,
    now: datetime,
    categories: Mapping[date, int | None],
    publish_after: time | None = None,
) -> DailyPlan:
    """実行日が日次更新の対象かを判定する。時刻判定を外すのは過去日だけ。"""
    category = categories.get(run_date)
    if category in HOLIDAY_CATEGORIES:
        return DailyPlan(run_date, DailyDecision.HOLIDAY)
    if category not in BUSINESS_DAY_CATEGORIES:
        return DailyPlan(run_date, DailyDecision.CALENDAR_MISSING)
    if publish_after is not None and (
        run_date > now.date() or (run_date == now.date() and now.time() < publish_after)
    ):
        return DailyPlan(run_date, DailyDecision.NOT_PUBLISHED)
    return DailyPlan(run_date, DailyDecision.TARGET)
