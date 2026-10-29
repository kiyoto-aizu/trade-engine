"""J-Quants 営業日カレンダー（markets/calendar）の検証規則（純関数、I/Oなし）。

docs/detail_design/19 の6章に対応する。区分: 0=非営業日, 1=営業日, 2=東証半日立会日,
3=非営業日（大阪取引所のみ祝日取引。株式は休場扱い）。
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import date, timedelta

VALID_CATEGORIES = frozenset({0, 1, 2, 3})


class BusinessCalendarError(Exception):
    """営業日カレンダーが不正、または期間を覆わない。"""


def parse_rows(rows: Iterable[Mapping[str, object]], start: date, end: date) -> dict[date, int]:
    """API応答の行を検証して日付→区分にする。期間の全暦日が揃っていなければ失敗する。"""
    if start > end:
        raise BusinessCalendarError("start must not be after end")
    result: dict[date, int] = {}
    for row in rows:
        raw_date, raw_div = row.get("Date"), row.get("HolDiv")
        if not isinstance(raw_date, str):
            raise BusinessCalendarError("calendar row has an invalid Date")
        try:
            day = date.fromisoformat(raw_date)
        except ValueError:
            raise BusinessCalendarError("calendar row has an invalid Date") from None
        if day.isoformat() != raw_date:
            raise BusinessCalendarError("calendar row has an invalid Date")
        if isinstance(raw_div, bool) or not isinstance(raw_div, (str, int)):
            raise BusinessCalendarError(f"unknown HolDiv for {raw_date}")
        try:
            category = int(raw_div)
        except ValueError:
            raise BusinessCalendarError(f"unknown HolDiv for {raw_date}") from None
        if category not in VALID_CATEGORIES or str(category) != str(raw_div):
            raise BusinessCalendarError(f"unknown HolDiv for {raw_date}")
        if not start <= day <= end:
            raise BusinessCalendarError(f"calendar row outside the requested period: {raw_date}")
        if day in result:
            raise BusinessCalendarError(f"duplicate calendar date: {raw_date}")
        result[day] = category
    missing = _missing(result, start, end)
    if missing:
        raise BusinessCalendarError(
            f"calendar response does not cover the period ({len(missing)} days missing, "
            f"first {missing[0].isoformat()})"
        )
    return result


def _missing(categories: Mapping[date, int], start: date, end: date) -> list[date]:
    days = (end - start).days + 1
    return [d for d in (start + timedelta(n) for n in range(days)) if d not in categories]


def differing_dates(existing: Mapping[date, int], new: Mapping[date, int]) -> list[date]:
    """両方にある日付のうち、区分が異なるものを返す。"""
    return sorted(d for d in new if d in existing and existing[d] != new[d])
