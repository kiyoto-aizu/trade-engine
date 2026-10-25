"""J-Quants canonical を利用側が読むときの入力契約の規則（純関数、I/Oなし）。

docs/detail_design/19 の3章に対応する。欠損日の判定・版の混在検出・結果の記録を担う。
"""

from __future__ import annotations

from collections.abc import Collection, Mapping
from dataclasses import dataclass
from datetime import date
from enum import StrEnum
from typing import Any

from src.domain.jquants_contract_range import CONTRACT_YEARS, is_within_contract

DATA_SOURCE = "jquants"
BUSINESS_DAY_CATEGORIES = frozenset({1, 2})
HOLIDAY_CATEGORIES = frozenset({0, 3})
LEDGER_FAILED_STATES = frozenset({"FAILED", "PARTIAL"})
LEDGER_IN_PROGRESS_STATES = frozenset({"PENDING", "RUNNING"})
ATTEMPT_FAILED_STATE = "FAILED"
ATTEMPT_IN_PROGRESS_STATE = "RUNNING"

VERIFY_STALE_SCHEMA = "STALE_SCHEMA"
VERIFY_BROKEN = "BROKEN"


class ConsumerContractError(Exception):
    """入力契約に反するため、利用側が処理を続けられない。"""


class DayStatus(StrEnum):
    AVAILABLE = "AVAILABLE"
    HOLIDAY = "HOLIDAY"
    MISSING = "MISSING"  # 営業日で世代が無く、取得失敗の記録も無い（未公開または未取得）
    FETCH_FAILED = "FETCH_FAILED"
    IN_PROGRESS = "IN_PROGRESS"
    OUT_OF_CONTRACT = "OUT_OF_CONTRACT"
    STALE_SCHEMA = "STALE_SCHEMA"
    BROKEN = "BROKEN"


@dataclass(frozen=True)
class ConsumerRequest:
    dataset: str
    schema_version: str
    columns: tuple[str, ...]
    start: date
    end: date
    today: date
    allowed_missing_dates: frozenset[date] = frozenset()

    def __post_init__(self) -> None:
        if self.dataset not in CONTRACT_YEARS:
            raise ConsumerContractError("dataset must be daily or minute")
        if not isinstance(self.schema_version, str) or not self.schema_version:
            raise ConsumerContractError("schema_version is required")
        if not self.columns or len(set(self.columns)) != len(self.columns):
            raise ConsumerContractError("columns must be a non-empty list without duplicates")
        if self.start > self.end:
            raise ConsumerContractError("start must not be after end")
        outside = sorted(d for d in self.allowed_missing_dates if not self.start <= d <= self.end)
        if outside:
            raise ConsumerContractError(
                "allowed missing dates are outside the period: "
                + ", ".join(d.isoformat() for d in outside)
            )


@dataclass(frozen=True)
class DayObservation:
    """読み取り層が観測した事実。意味づけはこのモジュールが行う。"""

    date: date
    generation_id: str | None = None
    schema_version: str | None = None
    transformer_version: str | None = None
    raw_generation_id: str | None = None
    verify_error: str | None = None  # None / VERIFY_STALE_SCHEMA / VERIFY_BROKEN
    ledger_state: str | None = None
    attempt_state: str | None = None


@dataclass(frozen=True)
class GenerationRef:
    date: date
    generation_id: str
    schema_version: str
    transformer_version: str
    raw_generation_id: str


@dataclass(frozen=True)
class ConsumerRecord:
    dataset: str
    schema_version: str
    columns: tuple[str, ...]
    start: date
    end: date
    missing_dates: tuple[date, ...]
    generations: tuple[GenerationRef, ...]
    data_source: str = DATA_SOURCE

    def as_dict(self) -> dict[str, Any]:
        return {
            "dataset": self.dataset,
            "schema_version": self.schema_version,
            "columns": list(self.columns),
            "start": self.start.isoformat(),
            "end": self.end.isoformat(),
            "missing_dates": [d.isoformat() for d in self.missing_dates],
            "generations": [
                {
                    "date": ref.date.isoformat(),
                    "generation_id": ref.generation_id,
                    "schema_version": ref.schema_version,
                    "transformer_version": ref.transformer_version,
                    "raw_generation_id": ref.raw_generation_id,
                }
                for ref in self.generations
            ],
            "data_source": self.data_source,
        }


def _format_dates(dates: Collection[date]) -> str:
    return ", ".join(d.isoformat() for d in sorted(dates))


def business_days_from_calendar(
    categories: Mapping[date, int | None], start: date, end: date
) -> frozenset[date]:
    """営業日カレンダーから期間内の営業日を返す。期間を覆わない・未知の区分なら失敗する。"""
    uncovered: list[date] = []
    business: set[date] = set()
    current = start
    while current <= end:
        category = categories.get(current)
        if category in BUSINESS_DAY_CATEGORIES:
            business.add(current)
        elif category not in HOLIDAY_CATEGORIES:
            uncovered.append(current)
        current = date.fromordinal(current.toordinal() + 1)
    if uncovered:
        raise ConsumerContractError(
            "business calendar does not cover the period (cannot judge missing days): "
            + _format_dates(uncovered)
        )
    return frozenset(business)


def classify_day(
    observation: DayObservation, *, is_business_day: bool, dataset: str, today: date
) -> DayStatus:
    if not is_business_day:
        return DayStatus.HOLIDAY
    if observation.verify_error == VERIFY_STALE_SCHEMA:
        return DayStatus.STALE_SCHEMA
    if observation.verify_error is not None:
        return DayStatus.BROKEN
    if observation.generation_id is not None:
        return DayStatus.AVAILABLE
    if not is_within_contract(dataset, observation.date, today):
        return DayStatus.OUT_OF_CONTRACT
    if (
        observation.ledger_state in LEDGER_FAILED_STATES
        or observation.attempt_state == ATTEMPT_FAILED_STATE
    ):
        return DayStatus.FETCH_FAILED
    if (
        observation.ledger_state in LEDGER_IN_PROGRESS_STATES
        or observation.attempt_state == ATTEMPT_IN_PROGRESS_STATE
    ):
        return DayStatus.IN_PROGRESS
    return DayStatus.MISSING


def judge_period(
    request: ConsumerRequest,
    observations: Mapping[date, DayObservation],
    business_days: Collection[date],
) -> ConsumerRecord:
    """期間全体を判定し、続行できなければ失敗させる。続行できるときは記録を返す。"""
    statuses = {
        day: classify_day(
            observation, is_business_day=day in business_days,
            dataset=request.dataset, today=request.today,
        )
        for day, observation in observations.items()
    }

    versions = {
        observation.schema_version
        for day, observation in observations.items()
        if statuses[day] in (DayStatus.AVAILABLE, DayStatus.STALE_SCHEMA)
        and observation.schema_version is not None
    }
    if len(versions) > 1:
        raise ConsumerContractError(
            "schema versions are mixed in the period ("
            + ", ".join(sorted(versions)) + "); rebuild the whole period to one version"
        )

    def days_with(status: DayStatus) -> list[date]:
        return [day for day, value in statuses.items() if value == status]

    if stale := days_with(DayStatus.STALE_SCHEMA):
        raise ConsumerContractError(
            "old schema version, rebuild required: " + _format_dates(stale)
        )
    if broken := days_with(DayStatus.BROKEN):
        raise ConsumerContractError("canonical data failed verification: " + _format_dates(broken))
    if failed := days_with(DayStatus.FETCH_FAILED):
        raise ConsumerContractError("acquisition failed: " + _format_dates(failed))
    if running := days_with(DayStatus.IN_PROGRESS):
        raise ConsumerContractError("acquisition in progress: " + _format_dates(running))
    if outside := days_with(DayStatus.OUT_OF_CONTRACT):
        raise ConsumerContractError(
            "outside the contract plan range (not a missing day): " + _format_dates(outside)
        )

    missing = days_with(DayStatus.MISSING)
    not_allowed = [day for day in missing if day not in request.allowed_missing_dates]
    if not_allowed:
        raise ConsumerContractError("missing days: " + _format_dates(not_allowed))

    generations = tuple(
        GenerationRef(
            day, observation.generation_id or "", observation.schema_version or "",
            observation.transformer_version or "", observation.raw_generation_id or "",
        )
        for day, observation in sorted(observations.items())
        if statuses[day] == DayStatus.AVAILABLE
    )
    if any(ref.schema_version != request.schema_version for ref in generations):
        raise ConsumerContractError("generation schema version differs from the requested one")
    return ConsumerRecord(
        dataset=request.dataset,
        schema_version=request.schema_version,
        columns=request.columns,
        start=request.start,
        end=request.end,
        missing_dates=tuple(sorted(missing)),
        generations=generations,
    )
