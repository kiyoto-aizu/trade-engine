"""Shared guarded calendar entry; orchestration reuses the lock-held operation."""

from dataclasses import dataclass, replace

from src.application.jquants_calendar_ingestion import JQuantsCalendarIngestion
from src.application.jquants_execution_guard import JQuantsExecutionBlockedError
from src.domain.jquants_business_calendar import BusinessCalendarError
from src.infrastructure.market_data.jquants_backfill_state import BackfillLockedError


@dataclass(frozen=True)
class CalendarJobResult:
    exit_code: int
    status: str
    reason: str = "NONE"
    summary: object = None
    stop_scope: str = "UNKNOWN"

    def as_dict(self):
        if self.summary is not None:
            return self.summary.as_dict()
        return {"status": self.status, "error_category": self.reason,
                "stop_scope": self.stop_scope, "exit_code": self.exit_code}


class JQuantsCalendarJob:
    def __init__(self, guard, client_factory, store_factory):
        self.guard = guard
        self.client_factory = client_factory
        self.store_factory = store_factory

    def run(self, start, end, *, refresh=False):
        try:
            with self.guard.locked() as state:
                result = self.run_locked(start, end, refresh=refresh)
                return replace(result, stop_scope=(state.stop_flag() or {}).get("scope", "NONE"))
        except JQuantsExecutionBlockedError:
            return CalendarJobResult(2, "BLOCKED_BY_FLAG", "ALL_FLAG", stop_scope="ALL_FLAG")
        except BackfillLockedError:
            return CalendarJobResult(2, "LOCK_SKIPPED", "GLOBAL_ALREADY_RUNNING")

    def run_locked(self, start, end, *, refresh=False):
        client = None
        try:
            client = self.client_factory()
            summary = JQuantsCalendarIngestion(client, self.store_factory()).run(
                start, end, refresh=refresh,
            )
            result = CalendarJobResult(0, "COMPLETE", summary=summary, stop_scope="NONE")
        except BusinessCalendarError as exc:
            result = CalendarJobResult(1, "FAILED", exc.reason.value)
        except Exception as exc:
            category = getattr(exc, "category", None)
            result = CalendarJobResult(1, "FAILED", category if isinstance(category, str) and category in {
                "API_ERROR", "AUTH_ERROR", "PERMISSION_OR_CONTRACT_ERROR", "RATE_LIMITED",
                "RATE_LIMIT_ABORTED", "TRANSPORT_ERROR", "SERVER_ERROR", "HTTP_ERROR",
                "CLIENT_REQUEST_ERROR", "RESPONSE_SCHEMA_ERROR",
            } else "STAGE_FAILED")
        finally:
            if client is not None:
                try:
                    client.close()
                except Exception:
                    result = CalendarJobResult(1, "FAILED", "STAGE_FAILED")
        return result
