"""Backfill stop/slow-down rules (doc 18 §2). Pure functions; no I/O."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from enum import StrEnum

from src.domain.jquants_contract_range import ClientRequestErrorKind, classify_client_request_error

CONSECUTIVE_FAILED_LIMIT = 3
CONSECUTIVE_STOP_DAYS_LIMIT = 3
RETRY_HOLD = 3
RETRY_NO_GO = 10
HOLD_BUDGET_FACTOR = 0.5
HOLD_ELAPSED_RATIO = 0.7
# Fixed with human approval on 2026-10-25 (doc 18 §4).
ELAPSED_LIMIT_SECONDS = {"daily": 60.0, "minute": 600.0}
DAILY_BUDGET_SECONDS = 2 * 60 * 60


class StopScope(StrEnum):
    NONE = "NONE"
    TODAY = "TODAY"  # rest of today only
    END_JOB = "END_JOB"  # this backfill job only; next run may start
    BACKFILL_FLAG = "BACKFILL_FLAG"  # backfill stays stopped until a human clears it
    ALL_FLAG = "ALL_FLAG"  # every job stays stopped until a human clears it


class StopReason(StrEnum):
    AUTH_ERROR = "AUTH_ERROR"
    PERMISSION_ERROR = "PERMISSION_ERROR"
    RATE_LIMITED = "RATE_LIMITED"
    RETRIES_EXCESSIVE = "RETRIES_EXCESSIVE"
    ELAPSED_OVER_LIMIT = "ELAPSED_OVER_LIMIT"
    CONSECUTIVE_FAILED = "CONSECUTIVE_FAILED"
    SERVER_ERROR = "SERVER_ERROR"
    OUT_OF_CONTRACT_400 = "OUT_OF_CONTRACT_400"
    UNEXPECTED_400_IN_RANGE = "UNEXPECTED_400_IN_RANGE"
    QUALITY_ERROR = "QUALITY_ERROR"
    UNKNOWN_FIELDS = "UNKNOWN_FIELDS"
    DISK_SPACE_CRITICAL = "DISK_SPACE_CRITICAL"
    DISK_FLOOR = "DISK_FLOOR"
    REPEATED_TODAY_STOP = "REPEATED_TODAY_STOP"
    REPEATED_REVIEW_STOP = "REPEATED_REVIEW_STOP"
    UNKNOWN_STATUS = "UNKNOWN_STATUS"


@dataclass(frozen=True)
class DayObservation:
    dataset: str
    target_date: date
    today: date
    status: str
    error_category: str | None
    warnings: tuple[str, ...]
    rate_limit_429_count: int
    retry_count: int
    elapsed_seconds: float
    failed_streak: int  # consecutive FAILED including this day


@dataclass(frozen=True)
class DayDecision:
    scope: StopScope
    reason: StopReason | None = None
    hold: bool = False


_KNOWN_STATUSES = frozenset({"COMPLETE", "ATTENTION", "FAILED"})

_CATEGORY_RULES: dict[str, tuple[StopScope, StopReason]] = {
    "AUTH_ERROR": (StopScope.ALL_FLAG, StopReason.AUTH_ERROR),
    "PERMISSION_OR_CONTRACT_ERROR": (StopScope.BACKFILL_FLAG, StopReason.PERMISSION_ERROR),
    "RATE_LIMIT_ABORTED": (StopScope.TODAY, StopReason.RATE_LIMITED),
    "RATE_LIMITED": (StopScope.TODAY, StopReason.RATE_LIMITED),
    "SERVER_ERROR": (StopScope.TODAY, StopReason.SERVER_ERROR),
    "QUALITY_ERROR": (StopScope.END_JOB, StopReason.QUALITY_ERROR),
}


def decide_after_day(obs: DayObservation) -> DayDecision:
    """Apply the doc 18 §2 table to one finished day. The first matching rule wins."""
    if obs.status not in _KNOWN_STATUSES:
        return DayDecision(StopScope.END_JOB, StopReason.UNKNOWN_STATUS)
    rule = _CATEGORY_RULES.get(obs.error_category or "")
    if rule is not None:
        return DayDecision(*rule)
    if obs.error_category == "CLIENT_REQUEST_ERROR":
        kind = classify_client_request_error(obs.dataset, obs.target_date, obs.today)
        if kind is ClientRequestErrorKind.OUT_OF_CONTRACT_RANGE:
            return DayDecision(StopScope.END_JOB, StopReason.OUT_OF_CONTRACT_400)
        return DayDecision(StopScope.END_JOB, StopReason.UNEXPECTED_400_IN_RANGE)
    if obs.rate_limit_429_count >= 1:
        return DayDecision(StopScope.TODAY, StopReason.RATE_LIMITED)
    other_retries = max(0, obs.retry_count - obs.rate_limit_429_count)
    if other_retries >= RETRY_NO_GO:
        return DayDecision(StopScope.TODAY, StopReason.RETRIES_EXCESSIVE)
    limit = ELAPSED_LIMIT_SECONDS[obs.dataset]
    if obs.elapsed_seconds > limit:
        return DayDecision(StopScope.TODAY, StopReason.ELAPSED_OVER_LIMIT)
    if obs.status == "FAILED" and obs.failed_streak >= CONSECUTIVE_FAILED_LIMIT:
        return DayDecision(StopScope.TODAY, StopReason.CONSECUTIVE_FAILED)
    if "UNSUPPORTED_FIELDS_RETAINED_IN_RAW" in obs.warnings:
        return DayDecision(StopScope.END_JOB, StopReason.UNKNOWN_FIELDS)
    hold = other_retries >= RETRY_HOLD or obs.elapsed_seconds > limit * HOLD_ELAPSED_RATIO
    return DayDecision(StopScope.NONE, None, hold)


def next_failed_streak(current: int, status: str) -> int:
    """FAILED counts across dates for one dataset; any non-FAILED result resets it."""
    return current + 1 if status == "FAILED" else 0


def is_repeated_today_stop(history: list[tuple[date, StopReason]], reason: StopReason) -> bool:
    """True when the latest N stop days (including the new one) all share ``reason``."""
    recent = history[-CONSECUTIVE_STOP_DAYS_LIMIT:]
    return (
        len(recent) == CONSECUTIVE_STOP_DAYS_LIMIT
        and len({day for day, _ in recent}) == CONSECUTIVE_STOP_DAYS_LIMIT
        and all(item_reason is reason for _, item_reason in recent)
    )


def effective_budget_seconds(base_seconds: float, held: bool) -> float:
    return base_seconds * HOLD_BUDGET_FACTOR if held else base_seconds
