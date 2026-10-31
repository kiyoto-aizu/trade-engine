"""Safe local/critical reporting, shared with future backfill cause notifications."""

from dataclasses import replace

from src.domain.jquants_ops_policy import JQuantsOpsEvent, REASONS, STATUSES, aggregate_exit_codes


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

    def daily(self, result, invocation_id, execution_day):
        if not result.outcomes:
            decision = result.decision.value
            self.emit(JQuantsOpsEvent(
                invocation_id, "DAILY", execution_day.isoformat(), result.exit_code,
                decision if decision in STATUSES else "FAILED", decision,
            ), notify=decision != "HOLIDAY")
        for item in result.outcomes:
            code = item.exit_code if item.exit_code is not None else result.exit_code
            reason = item.reason or item.error_category or "NONE"
            event_id = reason if reason in ("STOP_STATE_SAVE_FAILED", "METRICS_SAVE_FAILED") else "RESULT"
            event = JQuantsOpsEvent(
                invocation_id, "DAILY", execution_day.isoformat(), code,
                item.status if item.status in STATUSES else "SKIPPED",
                reason if reason in REASONS else "STAGE_FAILED", item.dataset,
                item.stop_scope, event_id=event_id,
            )
            self.emit(event, notify=code in (1, 2))
            if item.notification_failed:
                self._failure(event, "SLACK_NOTIFY_FAILED")


class JQuantsReportedIngestion:
    def __init__(self, ingestion, reporter, context_provider):
        self.ingestion = ingestion
        self.reporter = reporter
        self.context_provider = context_provider

    def run(self, *, dataset, date):
        context = self.context_provider()

        def aborted(message):
            event = JQuantsOpsEvent(
                context.invocation_id, context.current_stage, date, 3, "FAILED",
                "RATE_LIMIT_ABORTED", dataset, scheduled=context.scheduled,
                event_id="RATE_LIMIT_ABORTED",
            )
            self.reporter.emit(event, notify=True)
            return not any(
                item.event_id == "SLACK_NOTIFY_FAILED" for item in self.reporter.events
            )

        self.ingestion.notifier = aborted
        return self.ingestion.run(dataset=dataset, date=date)
