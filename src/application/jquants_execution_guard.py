"""Shared execution boundary for daily jobs and manual single-day ingestion."""

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from typing import Any

from src.application.jquants_single_day_ingestion import (
    JQuantsIngestionSummary,
    JQuantsSingleDayIngestion,
)
from src.infrastructure.market_data.jquants_backfill_state import (
    BackfillLockedError,
    JQuantsBackfillStateStore,
)


class JQuantsExecutionBlockedError(Exception):
    def __init__(self, reason: str) -> None:
        super().__init__("execution blocked by ALL_FLAG")
        self.reason = reason


@dataclass(frozen=True)
class SkippedIngestionSummary:
    dataset: str
    date: str
    status: str
    reason: str
    ran: bool = False
    error_category: str | None = None
    exit_code: int = 2

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


class JQuantsExecutionGuard:
    def __init__(self, state: JQuantsBackfillStateStore) -> None:
        self._state = state

    @contextmanager
    def locked(self) -> Iterator[JQuantsBackfillStateStore]:
        with self._state.global_lock():
            flag = self._state.stop_flag()
            if flag is not None and flag["scope"] == "ALL_FLAG":
                raise JQuantsExecutionBlockedError(flag["reason"])
            yield self._state

    def run_single_day(
        self,
        factory: Callable[[], JQuantsSingleDayIngestion],
        *,
        dataset: str,
        date: str,
        refresh: bool = False,
        canonical_only: bool = False,
    ) -> JQuantsIngestionSummary | SkippedIngestionSummary:
        try:
            with self.locked():
                return factory().run(
                    dataset=dataset, date=date, refresh=refresh, canonical_only=canonical_only,
                )
        except JQuantsExecutionBlockedError as exc:
            return SkippedIngestionSummary(
                dataset, date, "BLOCKED_BY_FLAG", exc.reason, error_category="BLOCKED_BY_FLAG",
            )
        except BackfillLockedError:
            return SkippedIngestionSummary(
                dataset, date, "LOCK_SKIPPED", "GLOBAL_ALREADY_RUNNING",
                error_category="GLOBAL_ALREADY_RUNNING",
            )
