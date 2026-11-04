"""J-Quants-only transport: no raw exceptions, URLs or response text are logged."""

import json
from src.domain.jquants_ops_policy import JQuantsOpsEvent, JQuantsOpsSummary


class JQuantsSafeNotifier:
    def __init__(self, webhook_url: str, *, post=None):
        self._webhook_url = webhook_url
        self._post = post

    def __call__(self, event) -> bool:
        try:
            if not isinstance(event, JQuantsOpsEvent) or not self._webhook_url:
                return False
            post = self._post
            if post is None:
                from src.config import config
                if config._is_test_runtime():
                    return False
                import requests
                post = requests.post
            payload = json.dumps(event.as_dict(), sort_keys=True)
            response = post(self._webhook_url, json={"text": payload}, timeout=10)
            response.raise_for_status()
            return True
        except Exception:
            return False


class JQuantsOpsSummaryNotifier:
    def __init__(self, webhook_url: str, *, post=None):
        self._webhook_url = webhook_url
        self._post = post

    def __call__(self, summary) -> bool:
        try:
            if not isinstance(summary, JQuantsOpsSummary) or not self._webhook_url:
                return False
            post = self._post
            if post is None:
                from src.config import config
                if config._is_test_runtime():
                    return False
                import requests
                post = requests.post
            from src.infrastructure.notification.slack_notify import format_result_notification
            message = format_result_notification(
                "J-Quants",
                "定期データ運用",
                f"実行日: {summary.day} / 全体状態: {summary.overall_status}",
                [
                    f"カレンダー: {summary.calendar_status}",
                    "日次: "
                    f"daily={summary.daily_daily_status}, "
                    f"minute={summary.daily_minute_status}",
                    "補填: "
                    f"daily={summary.backfill_daily_status} "
                    f"({summary.backfill_daily_processed}件), "
                    f"minute={summary.backfill_minute_status} "
                    f"({summary.backfill_minute_processed}件)",
                    f"予算到達: {'あり' if summary.budget_exhausted else 'なし'}",
                    f"終了コード: {summary.exit_code}",
                ],
            )
            response = post(self._webhook_url, json={"text": message}, timeout=10)
            response.raise_for_status()
            return True
        except Exception:
            return False


def notify_critical(message: str) -> bool:
    """Compatibility callback for existing 429 ingestion, never forwards free text."""
    try:
        from datetime import datetime, timedelta, timezone
        from uuid import uuid4
        from src.config import config
        from src.domain.jquants_ops_policy import JQuantsOpsEvent
        event = JQuantsOpsEvent(
            uuid4().hex, "INGESTION",
            datetime.now(timezone(timedelta(hours=9))).date().isoformat(),
            3, "FAILED", "RATE_LIMIT_ABORTED", event_id="RATE_LIMIT_ABORTED",
        )
        return JQuantsSafeNotifier(config.SLACK_WEBHOOK_CRITICAL)(event)
    except Exception:
        return False
