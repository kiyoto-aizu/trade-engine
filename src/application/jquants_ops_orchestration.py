"""Audit is upstream; one shared lock covers calendar, daily and conditional backfill."""

from datetime import datetime, timedelta, time, timezone
from uuid import uuid4

from src.application.jquants_execution_guard import JQuantsExecutionBlockedError
from src.domain.jquants_contract_range import calculate_earliest_available_date
from src.domain.jquants_ops_policy import JQuantsOpsEvent, REASONS, STATUSES, daily_ready
from src.application.jquants_ops_reporting import build_ops_summary, daily_event_records
from src.infrastructure.market_data.jquants_backfill_state import (
    BackfillLockedError,
    BackfillStateError,
)

JST = timezone(timedelta(hours=9))


class JQuantsOpsOrchestration:
    def __init__(
        self, guard, calendar_factory, daily_factory, backfill_factory, reporter,
        *, now_provider=lambda: datetime.now(timezone(timedelta(hours=9))),
        summary_notifier=None,
    ):
        self.guard = guard
        self.calendar_factory = calendar_factory
        self.daily_factory = daily_factory
        self.backfill_factory = backfill_factory
        self.reporter = reporter
        self.invocation_id = uuid4().hex
        self.current_stage = "OPS"
        self.now_provider = now_provider
        self.scheduled = False
        self.summary_notifier = summary_notifier
        self.backfill_processed_counts = {"daily": 0, "minute": 0}

    def event(self, stage, now, code, status, reason="NONE", dataset=None, scope="UNKNOWN",
              scheduled=False, event_id="RESULT", notify=False, target_day=None):
        execution_day = now.astimezone(JST).date() if now.tzinfo else now.date()
        event = JQuantsOpsEvent(
            self.invocation_id, stage, execution_day.isoformat(), code,
            status if status in STATUSES else "FAILED",
            reason if reason in REASONS else "STAGE_FAILED", dataset, scope, scheduled, event_id,
            target_day,
        )
        self.reporter.emit(event, notify=notify)

    def run(self, now, *, scheduled=False):
        now = now.astimezone(JST) if now.tzinfo else now.replace(tzinfo=JST)
        self.scheduled = scheduled
        self.backfill_processed_counts = {"daily": 0, "minute": 0}
        try:
            with self.guard.locked() as state:
                self._run_locked(now, state, scheduled)
        except BackfillLockedError:
            self.event("OPS", now, 2, "LOCK_SKIPPED", "GLOBAL_ALREADY_RUNNING",
                       scheduled=scheduled, notify=True)
            self._skip_after("CALENDAR", now, "STOP_STATE_UNKNOWN", scheduled)
            self.event("CALENDAR", now, 2, "LOCK_SKIPPED", "GLOBAL_ALREADY_RUNNING",
                       scheduled=scheduled)
            if scheduled:
                try:
                    if self.reporter.repository.scheduled_lock_streak() >= 3:
                        self.event(
                            "OPS", now, 2, "LOCK_SKIPPED", "STALE_LOCK_CHECK_REQUIRED",
                            scheduled=True, event_id="STALE_LOCK_CHECK_REQUIRED", notify=True,
                        )
                except Exception:
                    self.event("OPS", now, 1, "FAILED", "LOCAL_EVENT_FAILED",
                               event_id="LOCAL_EVENT_FAILED", scheduled=scheduled)
        except JQuantsExecutionBlockedError:
            self.event("OPS", now, 2, "BLOCKED_BY_FLAG", "ALL_FLAG", scope="ALL_FLAG",
                       scheduled=scheduled, notify=True)
            self._skip_after("CALENDAR", now, "ALL_FLAG", scheduled)
        except BackfillStateError as exc:
            is_write_failure = exc.write_failure
            reason = (
                exc.cause_reason
                if is_write_failure and exc.cause_reason in REASONS
                else "STOP_STATE_SAVE_FAILED" if is_write_failure else "STATE_ERROR"
            )
            event_id = "STOP_STATE_SAVE_FAILED" if is_write_failure else "RESULT"
            self.event(
                self.current_stage, now, 1, "FAILED", reason,
                dataset=exc.dataset, scheduled=scheduled, event_id=event_id,
                notify=is_write_failure or self.current_stage != "BACKFILL",
            )
            self._skip_after(self.current_stage, now, reason, scheduled)
        except Exception:
            self.event(self.current_stage, now, 1, "FAILED", "STAGE_FAILED",
                       scheduled=scheduled, notify=True)
            self._skip_after(self.current_stage, now, "STAGE_FAILED", scheduled)
        if not any(event.stage == "OPS" and event.event_id == "RESULT" for event in self.reporter.events):
            self.event("OPS", now, self.reporter.exit_code, self._status(), scheduled=scheduled)
        result_status = self._status()
        result_exit_code = self.reporter.exit_code
        summary_notification_failed = self._send_summary(
            now, result_status, result_exit_code,
        )
        return {
            "invocation_id": self.invocation_id, "exit_code": result_exit_code,
            "status": result_status,
            "events": [event.as_dict() for event in self.reporter.events],
            "operational_go": "NOT_GRANTED",
            "summary_notification_failed": summary_notification_failed,
        }

    def _send_summary(self, now, status, exit_code):
        if self.summary_notifier is None:
            return False
        summary = build_ops_summary(
            self.reporter.events, now.date().isoformat(), status, exit_code,
            self.backfill_processed_counts,
        )
        try:
            sent = self.summary_notifier(summary)
        except Exception:
            sent = False
        if sent:
            return False
        event = next((
            item for item in reversed(self.reporter.events)
            if item.stage == "OPS" and item.event_id == "RESULT"
        ), None)
        if event is not None:
            self.reporter.record_nonfatal_notification_failure(event)
        return True

    def _status(self):
        events = self.reporter.events
        if any(event.status == "LOCK_SKIPPED" for event in events):
            return "LOCK_SKIPPED"
        if self.reporter.exit_code:
            return "FAILED"
        if any(event.status == "BUDGET_EXHAUSTED" for event in events):
            return "BUDGET_EXHAUSTED"
        if any(event.reason in (
            "COMPLETED_WITH_REVIEW", "NOT_PUBLISHED", "NO_GO", "EVALUATION_INCOMPLETE",
        ) for event in events):
            return "ATTENTION"
        if any(event.status == "SKIPPED" for event in events):
            return "SKIPPED"
        return "COMPLETE"

    def _skip_after(self, stage, now, reason, scheduled):
        stages = ("CALENDAR", "DAILY", "BACKFILL")
        start = stages.index(stage) if stage in stages else -1
        for following in stages[start + 1:]:
            self.event(following, now, 0, "SKIPPED", reason, scheduled=scheduled)

    def _run_locked(self, now, state, scheduled):
        self.current_stage = "CALENDAR"
        today = now.date()
        calendar = self.calendar_factory().run_locked(
            calculate_earliest_available_date("daily", today), today, refresh=False,
        )
        self.event("CALENDAR", now, calendar.exit_code, calendar.status, calendar.reason,
                   scope=(state.stop_flag() or {}).get("scope", "NONE"),
                   scheduled=scheduled, notify=calendar.exit_code in (1, 2),
                   target_day=today.isoformat())
        if calendar.exit_code != 0 or calendar.status != "COMPLETE":
            self._skip_after("CALENDAR", now, "CALENDAR_NOT_READY", scheduled)
            return
        if self.reporter.exit_code:
            self._skip_after("CALENDAR", now, "LOCAL_EVENT_FAILED", scheduled)
            return
        self.current_stage = "DAILY"
        current_now = self.now_provider()
        if current_now.date() != today:
            self.event("DAILY", now, 2, "JST_DAY_CHANGED", "JST_DAY_CHANGED", scheduled=scheduled)
            self._skip_after("DAILY", now, "JST_DAY_CHANGED", scheduled)
            return
        daily = self.daily_factory().run_locked(today, current_now, state, time(17))
        for daily_event, notify, notification_failed in daily_event_records(
            daily, self.invocation_id, now.date(),
            (state.stop_flag() or {}).get("scope", "NONE"),
        ):
            daily_event = daily_event.__class__(
                **{**daily_event.as_dict(), "scheduled": scheduled}
            )
            self.reporter.emit(daily_event, notify=notify)
            if notification_failed:
                self.reporter._failure(daily_event, "SLACK_NOTIFY_FAILED")
        if not daily_ready(daily, today) or self.reporter.exit_code:
            self._skip_after("DAILY", now, "DAILY_NOT_READY", scheduled)
            return
        self.current_stage = "BACKFILL"
        if self.now_provider().date() != today:
            self.event("BACKFILL", now, 2, "JST_DAY_CHANGED", "JST_DAY_CHANGED", scheduled=scheduled)
            return
        flag = state.stop_flag()
        if flag is not None:
            self.event("BACKFILL", now, 2, "BLOCKED_BY_FLAG", flag["scope"], "daily",
                       scope=flag["scope"], scheduled=scheduled)
            self.event("BACKFILL", now, 0, "SKIPPED", "BACKFILL_NOT_COMPLETE", "minute",
                       scope=flag["scope"], scheduled=scheduled)
            return
        backfill = self.backfill_factory()
        for dataset in ("daily", "minute"):
            result = backfill.run_locked(
                dataset, calculate_earliest_available_date(dataset, today),
                today - timedelta(days=1), today=today,
            )
            self.backfill_processed_counts[dataset] = len(result.processed)
            stop_flag = result.stop_flag or {}
            stop_scope = stop_flag.get("scope", "NONE")
            stop_reason = stop_flag.get("reason")
            is_flagged = stop_scope in ("BACKFILL_FLAG", "ALL_FLAG")
            self.event(
                "BACKFILL", now, result.exit_code, result.outcome.value,
                stop_reason if is_flagged and stop_reason else result.outcome.value,
                dataset, scope=stop_scope,
                scheduled=scheduled,
                notify=result.outcome.value == "COMPLETED_WITH_REVIEW" or is_flagged,
            )
            if result.outcome.value != "COMPLETED" or result.remaining_targets or self.reporter.exit_code:
                if dataset == "daily":
                    self.event("BACKFILL", now, 0, "SKIPPED", "BACKFILL_NOT_COMPLETE",
                               "minute", scheduled=scheduled)
                break
        self.event("OPS", now, self.reporter.exit_code, self._status(),
                   scope=(state.stop_flag() or {}).get("scope", "NONE"),
                   scheduled=scheduled)
