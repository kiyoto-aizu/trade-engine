from types import SimpleNamespace

import requests

from src.api import request_handler
from src.domain.board_request_context import board_request_context
from src.infrastructure.notification import slack_notify


def test_slack_notification_is_suppressed_during_tests(monkeypatch):
    calls = []
    monkeypatch.setattr(slack_notify.requests, 'post', lambda *args, **kwargs: calls.append(args))

    assert not slack_notify.notify_daily('test message')
    assert calls == []


def test_request_handler_applies_interval_between_get_and_post(monkeypatch):
    monotonic_values = iter([0.0, 0.0, 0.1, 0.1])
    sleeps = []
    responses = [SimpleNamespace(raise_for_status=lambda: None, json=lambda: {}),
                 SimpleNamespace(raise_for_status=lambda: None, json=lambda: {})]

    monkeypatch.setattr(request_handler.time, 'monotonic', lambda: next(monotonic_values))
    monkeypatch.setattr(request_handler.time, 'sleep', sleeps.append)
    monkeypatch.setattr(request_handler.config, 'API_REQUEST_INTERVAL_SECONDS', 0.5)
    monkeypatch.setattr(request_handler.requests, 'get', lambda *args, **kwargs: responses[0])
    monkeypatch.setattr(request_handler.requests, 'post', lambda *args, **kwargs: responses[1])
    request_handler._last_request_at = None

    request_handler.send_get('https://example.test')
    request_handler.send_post('https://example.test')

    assert sleeps == [0.4]


def test_board_429_is_retried_and_success_is_recorded(monkeypatch):
    responses = iter((429, 200))
    calls = []
    sleeps = []

    def fake_get(url, **_kwargs):
        calls.append(url)
        status = next(responses)
        if status == 429:
            response = SimpleNamespace(status_code=429)
            raise requests.HTTPError(response=response)
        return SimpleNamespace(raise_for_status=lambda: None, json=lambda: {"CurrentPrice": 100})

    monkeypatch.setattr(request_handler, "_wait_for_request_slot", lambda: None)
    monkeypatch.setattr(request_handler.requests, "get", fake_get)
    monkeypatch.setattr(request_handler.time, "sleep", sleeps.append)
    monkeypatch.setattr(request_handler.config, "FILTER_BOARD_429_MAX_RETRIES", 1)
    monkeypatch.setattr(request_handler.config, "FILTER_BOARD_429_RETRY_WAIT_SECONDS", 0.25)
    before = request_handler.get_rate_limit_stats("/board/")

    result = request_handler.send_get("http://localhost/kabusapi/board/7203@1")

    after = request_handler.get_rate_limit_stats("/board/")
    assert result == {"CurrentPrice": 100}
    assert len(calls) == 2
    assert sleeps == [0.25]
    assert after["responses"] - before["responses"] == 1
    assert after["retries"] - before["retries"] == 1
    assert after["succeeded"] - before["succeeded"] == 1
    assert after["failed"] - before["failed"] == 0


def test_board_429_exhaustion_returns_none_and_non_board_429_is_not_retried(monkeypatch):
    calls = []

    def always_limited(url, **_kwargs):
        calls.append(url)
        raise requests.HTTPError(response=SimpleNamespace(status_code=429))

    monkeypatch.setattr(request_handler, "_wait_for_request_slot", lambda: None)
    monkeypatch.setattr(request_handler.requests, "get", always_limited)
    monkeypatch.setattr(request_handler.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(request_handler.config, "FILTER_BOARD_429_MAX_RETRIES", 1)
    before = request_handler.get_rate_limit_stats("/board/")

    assert request_handler.send_get("http://localhost/kabusapi/board/7203@1") is None
    board_after = request_handler.get_rate_limit_stats("/board/")
    assert len(calls) == 2
    assert board_after["responses"] - before["responses"] == 2
    assert board_after["retries"] - before["retries"] == 1
    assert board_after["failed"] - before["failed"] == 1

    calls.clear()
    assert request_handler.send_get("http://localhost/kabusapi/wallet/cash") is None
    assert len(calls) == 1


def test_read_timeout_log_keeps_board_request_correlation_id(monkeypatch, caplog):
    def timed_out(_url, **_kwargs):
        raise requests.ReadTimeout("read timeout=10")

    monkeypatch.setattr(request_handler, "_wait_for_request_slot", lambda: None)
    monkeypatch.setattr(request_handler.requests, "get", timed_out)

    with board_request_context("abc123"):
        assert request_handler.send_get("http://localhost/kabusapi/board/7203@1") is None

    assert "read timeout=10" in caplog.text
    assert "request_id=abc123" in caplog.text