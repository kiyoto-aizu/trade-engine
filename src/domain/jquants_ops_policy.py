"""Current-invocation readiness, safe operational events and exit severity."""

from dataclasses import asdict, dataclass
from datetime import date
import re

EXIT_SEVERITY = {0: 0, 2: 1, 1: 2, 3: 3}
STAGES = frozenset({"AUDIT", "CALENDAR", "DAILY", "BACKFILL", "OPS", "INGESTION"})
REASONS = frozenset({
    "NONE", "PASS", "AUDIT_FAILED", "INITIALIZATION_ERROR", "STAGE_FAILED",
    "STATE_ERROR", "STOP_STATE_SAVE_FAILED", "LOCAL_EVENT_FAILED", "SLACK_NOTIFY_FAILED",
    "ALL_FLAG", "BACKFILL_FLAG", "UNKNOWN", "GLOBAL_ALREADY_RUNNING",
    "STALE_LOCK_CHECK_REQUIRED", "DAILY_NOT_READY", "CALENDAR_NOT_READY",
    "HOLIDAY", "NOT_PUBLISHED", "CALENDAR_MISSING", "REVIEW_EXCLUDED",
    "DAILY_STOPPED", "STOPPED_EARLIER", "EVALUATION_INCOMPLETE",
    "AUTH_ERROR", "PERMISSION_ERROR", "DISK_SPACE_CRITICAL", "DISK_UNMEASURABLE",
    "RATE_LIMIT_ABORTED", "CONSECUTIVE_FAILED", "CONSECUTIVE_FAILED_3",
    "STORAGE_OR_FORMAT_ERROR", "SERVER_ERROR", "CLIENT_REQUEST_ERROR", "QUALITY_ERROR",
    "UNKNOWN_ERROR", "HTTP_401", "HTTP_403_IN_RANGE", "UNEXPECTED_400_IN_RANGE",
    "QUALITY_VALIDATION_FAILED", "FAILED_RECOVERY_TARGET", "RESPONSE_SCHEMA_ERROR",
    "RAW_STORE_ERROR", "DATASET_ALREADY_RUNNING", "BLOCKED_BY_FLAG",
    "COMPLETED", "COMPLETED_WITH_REVIEW", "BUDGET_EXHAUSTED", "STOPPED",
    "STOPPED_TODAY", "LOCK_SKIPPED", "JST_DAY_CHANGED", "BACKFILL_NOT_COMPLETE",
    "STOP_STATE_UNKNOWN", "REPEATED_TODAY_STOP", "NO_GO", "ATTENTION",
    "METRICS_SAVE_FAILED",
})
STATUSES = frozenset({
    "COMPLETE", "FAILED", "SKIPPED", "ATTENTION", "NOT_PUBLISHED", "HOLIDAY",
    "COMPLETED", "COMPLETED_WITH_REVIEW", "BUDGET_EXHAUSTED", "BLOCKED_BY_FLAG",
    "STOPPED_TODAY", "LOCK_SKIPPED", "STOPPED", "JST_DAY_CHANGED", "DAILY_STOPPED",
})


def aggregate_exit_codes(codes) -> int:
    return max(codes, key=EXIT_SEVERITY.__getitem__, default=0)


def daily_ready(result, today: date) -> bool:
    return (
        result.run_date == today and result.decision.value == "TARGET"
        and {item.dataset for item in result.outcomes} == {"daily", "minute"}
        and len(result.outcomes) == 2
        and all(
            item.ran and item.status == "COMPLETE" and item.exit_code == 0
            and item.verdict == "Go" and not item.not_evaluated
            for item in result.outcomes
        )
    )


@dataclass(frozen=True)
class JQuantsOpsEvent:
    invocation_id: str
    stage: str
    day: str
    exit_code: int
    status: str
    reason: str = "NONE"
    dataset: str | None = None
    stop_scope: str = "UNKNOWN"
    scheduled: bool = False
    event_id: str = "RESULT"

    def __post_init__(self):
        if not re.fullmatch(r"[0-9a-f]{32}", self.invocation_id):
            raise ValueError("invalid invocation id")
        if date.fromisoformat(self.day).isoformat() != self.day:
            raise ValueError("invalid date")
        if self.stage not in STAGES or type(self.exit_code) is not int or self.exit_code not in EXIT_SEVERITY:
            raise ValueError("invalid operational event")
        if self.status not in STATUSES or self.reason not in REASONS:
            raise ValueError("invalid operational code")
        if self.dataset not in (None, "daily", "minute") or self.stop_scope not in (
            "UNKNOWN", "NONE", "ALL_FLAG", "BACKFILL_FLAG",
        ):
            raise ValueError("invalid operational scope")
        if type(self.scheduled) is not bool or self.event_id not in (
            "RESULT", "RATE_LIMIT_ABORTED", "SLACK_NOTIFY_FAILED", "LOCAL_EVENT_FAILED",
            "STOP_STATE_SAVE_FAILED", "STALE_LOCK_CHECK_REQUIRED",
            "METRICS_SAVE_FAILED",
        ):
            raise ValueError("invalid event id")

    def as_dict(self):
        return asdict(self)
