"""J-Quants-only transport: no raw exceptions, URLs or response text are logged."""

import json
from src.domain.jquants_ops_policy import JQuantsOpsEvent


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
