"""J-Quants canonical を契約どおりに読むユースケース（読み取り専用）。

営業日カレンダー・読み取りRepositoryはポート経由で受け取り、規則は domain に委ねる。
他のデータ源（Yahoo・自前Parquet）へは切り替えない。
"""

from __future__ import annotations

from collections.abc import Collection, Mapping
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any, Protocol

from src.domain.jquants_consumer_contract import (
    ConsumerContractError,
    ConsumerRecord,
    ConsumerRequest,
    DayObservation,
    business_days_from_calendar,
    judge_period,
)
from src.infrastructure.market_data.jquants_canonical_store import SCHEMA_VERSION


class BusinessCalendarPort(Protocol):
    def categories(self, start: date, end: date) -> Mapping[date, int | None]:
        """日付ごとの営業日区分（0〜3）。判定できない日は含めないかNone。"""


class CanonicalReaderPort(Protocol):
    def column_names(self, dataset: str) -> tuple[str, ...]: ...

    def observe(self, dataset: str, target_date: str) -> DayObservation: ...

    def read_columns(
        self, dataset: str, target_date: str, generation_id: str, columns: tuple[str, ...]
    ) -> Any: ...


@dataclass(frozen=True)
class JQuantsCanonicalInput:
    record: ConsumerRecord
    tables: Mapping[date, Any]


class JQuantsCanonicalInputUseCase:
    def __init__(self, reader: CanonicalReaderPort, calendar: BusinessCalendarPort) -> None:
        self._reader = reader
        self._calendar = calendar

    def load(
        self,
        *,
        dataset: str,
        schema_version: str,
        columns: Collection[str],
        start: date,
        end: date,
        today: date,
        allowed_missing_dates: Collection[date] = (),
    ) -> JQuantsCanonicalInput:
        request = ConsumerRequest(
            dataset=dataset,
            schema_version=schema_version,
            columns=tuple(columns),
            start=start,
            end=end,
            today=today,
            allowed_missing_dates=frozenset(allowed_missing_dates),
        )
        if request.schema_version != SCHEMA_VERSION:
            raise ConsumerContractError(
                f"requested schema_version {request.schema_version} does not match "
                f"the implementation ({SCHEMA_VERSION})"
            )
        known = self._reader.column_names(dataset)
        unknown = [name for name in request.columns if name not in known]
        if unknown:
            raise ConsumerContractError("unknown columns: " + ", ".join(unknown))

        business_days = business_days_from_calendar(
            self._calendar.categories(start, end), start, end
        )
        observations: dict[date, DayObservation] = {}
        for offset in range((end - start).days + 1):
            day = start + timedelta(days=offset)
            if day in business_days:
                observations[day] = self._reader.observe(dataset, day.isoformat())
        record = judge_period(request, observations, business_days)
        tables = {
            ref.date: self._reader.read_columns(
                dataset, ref.date.isoformat(), ref.generation_id, request.columns
            )
            for ref in record.generations
        }
        return JQuantsCanonicalInput(record, tables)
