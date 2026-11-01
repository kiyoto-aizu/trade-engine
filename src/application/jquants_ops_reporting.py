"""Safe local/critical reporting, shared with future backfill cause notifications."""

from dataclasses import replace
from datetime import datetime, timedelta, timezone

from src.domain.jquants_ops_policy import JQuantsOpsEvent, REASONS, STATUSES, aggregate_exit_codes

JST = timezone(timedelta(hours=9))


def daily_event_records(result, invocation_id, execution_day, stop_scope="UNKNOWN"):
    """Build the same safe daily event and notification decisions for every entrypoint."""
    records = []
    if not result.outcomes:
        decision = result.decision.value
        records.append((
            JQuantsOpsEvent(
                invocation_id, "DAILY", execution_day.isoformat(), result.exit_code,
                decision if decision in STATUSES else "FAILED",
                decision if decision in REASONS else "STAGE_FAILED",
                stop_scope=stop_scope,
            ),
            decision != "HOLIDAY", False,
        ))
        return records
    for item in result.outcomes:
        code = item.exit_code if item.exit_code is not None else result.exit_code
        reason = item.reason or item.error_category or "NONE"
        if getattr(item, "verdict", None) != "Go" and code == 0:
            reason = "NO_GO"
        elif getattr(item, "not_evaluated", False) and code == 0:
            reason = "EVALUATION_INCOMPLETE"
        event_id = reason if reason in ("STOP_STATE_SAVE_FAILED", "METRICS_SAVE_FAILED") else "RESULT"
        event = JQuantsOpsEvent(
            invocation_id, "DAILY", execution_day.isoformat(), code,
            item.status if item.status in STATUSES else "SKIPPED",
            reason if reason in REASONS else "STAGE_FAILED", item.dataset,
            item.stop_scope, event_id=event_id,
        )
        records.append((
            event, code in (1, 2) or (code == 0 and reason != "NONE"),
            bool(getattr(item, "notification_failed", False)),
        ))
    return records


class JQuantsOpsReporter:
    def __init__(self, repository, notifier):
        self.repository = repository
        self.notifier = notifier
        self.events = []
        self.codes = []
        self._seen = set()

    def emit(self, event, *, notify=False):
        key = (event.invocation_id, event.stage, event.dataset, event.day, event.event_id)
        if key in self._seen:
            return
        self._seen.add(key)
        self.events.append(event)
        self.codes.append(event.exit_code)
        try:
            self.repository.append(event)
        except Exception:
            self._failure(event, "LOCAL_EVENT_FAILED", persist=False)
        if notify:
            try:
                sent = self.notifier(event)
            except Exception:
                sent = False
            if not sent:
                self._failure(event, "SLACK_NOTIFY_FAILED")

    def _failure(self, event, reason, *, persist=True):
        failure = replace(event, status="FAILED", reason=reason, event_id=reason, exit_code=1)
        self.events.append(failure)
        self.codes.append(1)
        if persist:
            try:
                self.repository.append(failure)
            except Exception:
                self._failure(event, "LOCAL_EVENT_FAILED", persist=False)

    @property
    def exit_code(self):
        # Notification failure only upgrades an otherwise successful body.
        originals = [e.exit_code for e in self.events if e.event_id not in (
            "SLACK_NOTIFY_FAILED", "LOCAL_EVENT_FAILED",
        )]
        body = aggregate_exit_codes(originals)
        return body if body else (1 if any(self.codes) else 0)

    def daily(self, result, invocation_id, execution_day, *, stop_scope="UNKNOWN"):
        for event, notify, notification_failed in daily_event_records(
            result, invocation_id, execution_day, stop_scope,
        ):
            self.emit(event, notify=notify)
            if notification_failed:
                self._failure(event, "SLACK_NOTIFY_FAILED")


class JQuantsReportedIngestion:
    def __init__(self, ingestion, reporter, context_provider):
        self.ingestion = ingestion
        self.reporter = reporter
        self.context_provider = context_provider

    def run(self, *, dataset, date):
        context = self.context_provider()

        def aborted(message):
            now_provider = getattr(context, "now_provider", None)
            now = now_provider() if callable(now_provider) else datetime.now(JST)
            execution_day = now.astimezone(JST).date() if now.tzinfo else now.date()
            failures_before = sum(
                item.event_id == "SLACK_NOTIFY_FAILED" for item in self.reporter.events
            )
            event = JQuantsOpsEvent(
                context.invocation_id, context.current_stage, execution_day.isoformat(), 3, "FAILED",
                "RATE_LIMIT_ABORTED", dataset, scheduled=context.scheduled,
                event_id="RATE_LIMIT_ABORTED", target_day=date,
            )
            self.reporter.emit(event, notify=True)
            failures_after = sum(
                item.event_id == "SLACK_NOTIFY_FAILED" for item in self.reporter.events
            )
            return failures_after == failures_before

        self.ingestion.notifier = aborted
        return self.ingestion.run(dataset=dataset, date=date)
