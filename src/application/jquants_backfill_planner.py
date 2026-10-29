"""補填対象の一覧ユースケース。通信せず、台帳とカレンダーを読み取るだけ。"""

from __future__ import annotations

from datetime import date
from typing import Protocol

from src.application.jquants_canonical_input import BusinessCalendarPort
from src.domain.jquants_backfill_plan import BackfillPlan, plan_backfill


class LedgerStatePort(Protocol):
    def state(self, dataset: str, target_date: date) -> str | None:
        """取得台帳の状態名。記録が無ければNone。"""


class JQuantsBackfillPlanner:
    def __init__(self, calendar: BusinessCalendarPort, ledger: LedgerStatePort) -> None:
        self._calendar = calendar
        self._ledger = ledger

    def plan(self, dataset: str, start: date, end: date, today: date) -> BackfillPlan:
        return plan_backfill(
            dataset, start, end, today, self._calendar.categories(start, end),
            lambda day: self._ledger.state(dataset, day),
        )
