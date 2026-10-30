"""J-Quants 契約で取得できる日付範囲の判定（純関数）。

実測(2026-10-25, Issue #12)で、開始日（today の N 年前の同月同日）は取得可、
その前日は HTTP 400 だった。範囲は日々ローリングする。
"""

from __future__ import annotations

from datetime import date
from enum import StrEnum

CONTRACT_YEARS: dict[str, int] = {
    "daily": 5,
    "minute": 2,
}


def _years_before(today: date, years: int) -> date:
    try:
        return today.replace(year=today.year - years)
    except ValueError:
        # 2/29 の N 年前が存在しない場合は翌日(3/1)に寄せ、範囲外を投げない側に倒す。
        return date(today.year - years, 3, 1)


def calculate_earliest_available_date(dataset: str, today: date) -> date:
    """取得可能な最古の日付（この日を含む）。"""
    if dataset not in CONTRACT_YEARS:
        raise ValueError(f"unknown dataset: {dataset}")
    return _years_before(today, CONTRACT_YEARS[dataset])


def is_within_contract(dataset: str, target_date: date, today: date) -> bool:
    """target_date が契約範囲内（開始日〜today）か。"""
    earliest = calculate_earliest_available_date(dataset, today)
    return earliest <= target_date <= today


def sort_backfill_dates(dataset: str, dates: list[date], today: date) -> list[date]:
    """補填対象を範囲内に絞り、古い日から並べる（ローリングで先に期限切れになるため）。"""
    return sorted(d for d in dates if is_within_contract(dataset, d, today))


class ClientRequestErrorKind(StrEnum):
    UNEXPECTED_400_IN_RANGE = "UNEXPECTED_400_IN_RANGE"
    OUT_OF_CONTRACT_RANGE = "OUT_OF_CONTRACT_RANGE"


def classify_client_request_error(
    dataset: str, target_date: date, today: date
) -> ClientRequestErrorKind:
    """HTTP 400 の原因分類。

    範囲内の 400 は実装不具合・想定外、範囲外の 400 は判定漏れ（境界到達）。
    未来日の 400 も範囲外に分類されるため、日付ずれが原因のときは区別して調べる。
    """
    if is_within_contract(dataset, target_date, today):
        return ClientRequestErrorKind.UNEXPECTED_400_IN_RANGE
    return ClientRequestErrorKind.OUT_OF_CONTRACT_RANGE
