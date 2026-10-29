"""営業日カレンダーの取得・検証・保存ユースケース（docs/detail_design/19 の6章）。"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from datetime import date
from typing import Any, Protocol

from src.domain.jquants_business_calendar import (
    BusinessCalendarError,
    differing_dates,
    parse_rows,
)

ENDPOINT = "markets/calendar"


class PagedClientPort(Protocol):
    def iter_pages(self, endpoint: str, params: Mapping[str, Any] | None = None) -> Iterator[Any]: ...


class CalendarStorePort(Protocol):
    def load(self) -> dict[date, int]: ...

    def save(self, categories: Mapping[date, int]) -> None: ...


@dataclass(frozen=True)
class CalendarIngestionSummary:
    start: date
    end: date
    fetched: int
    by_category: Mapping[int, int]
    changed: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "start": self.start.isoformat(), "end": self.end.isoformat(),
            "fetched": self.fetched, "changed": self.changed,
            "by_category": {str(k): v for k, v in sorted(self.by_category.items())},
            "status": "COMPLETE",
        }


class JQuantsCalendarIngestion:
    def __init__(self, client: PagedClientPort, store: CalendarStorePort) -> None:
        self._client = client
        self._store = store

    def run(self, start: date, end: date, *, refresh: bool = False) -> CalendarIngestionSummary:
        if start > end:
            raise BusinessCalendarError("start must not be after end")
        rows: list[Mapping[str, Any]] = []
        for page in self._client.iter_pages(
            ENDPOINT, {"from": start.isoformat(), "to": end.isoformat()}
        ):
            rows.extend(page.data)
        fetched = parse_rows(rows, start, end)
        existing = self._store.load()
        changed = differing_dates(existing, fetched)
        if changed and not refresh:
            raise BusinessCalendarError(
                f"stored calendar differs for {len(changed)} days (first "
                f"{changed[0].isoformat()}); rerun with refresh to overwrite"
            )
        merged = {**existing, **fetched}
        if merged != existing:
            self._store.save(merged)
        by_category: dict[int, int] = {}
        for category in fetched.values():
            by_category[category] = by_category.get(category, 0) + 1
        return CalendarIngestionSummary(start, end, len(fetched), by_category, len(changed))
