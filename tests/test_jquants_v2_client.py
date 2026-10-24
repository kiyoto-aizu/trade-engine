import logging
import traceback
from collections import deque

import pytest
import requests

from src.infrastructure.market_data.jquants_v2_client import (
    JQuantsAuthenticationError,
    JQuantsClientRequestError,
    JQuantsConfigurationError,
    JQuantsContractAccessError,
    JQuantsHTTPError,
    JQuantsPaginationError,
    JQuantsRateLimitError,
    JQuantsRateLimitAbortError,
    JQuantsRateLimiter,
    JQuantsResponseError,
    JQuantsServerError,
    JQuantsTransportError,
    JQuantsV2Client,
)
from src.config import config


class FakeClock:
    def __init__(self):
        self.now = 0.0
        self.sleeps = []

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


class FakeResponse:
    def __init__(self, status_code=200, payload=None, raw_body=b'{"data":[]}', headers=None):
        self.status_code = status_code
        self.payload = {"data": []} if payload is None else payload
        self.content = raw_body
        self.headers = {} if headers is None else headers

    def json(self):
        if isinstance(self.payload, BaseException):
            raise self.payload
        return self.payload


class FakeTransport:
    def __init__(self, *results):
        self.results = deque(results)
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        result = self.results.popleft()
        if isinstance(result, BaseException):
            raise result
        return result


def _client(transport, *, clock=None, max_attempts=3, **kwargs):
    clock = clock or FakeClock()
    limiter = JQuantsRateLimiter(1.2, clock=clock.monotonic, sleep=clock.sleep)
    client = JQuantsV2Client(
        "test-api-key",
        max_attempts=max_attempts,
        rate_limiter=limiter,
        sleep=clock.sleep,
        transport=transport,
        **kwargs,
    )
    return client, clock


def _response(payload=None, *, status_code=200, raw_body=b'{"data":[]}'):
    return FakeResponse(status_code=status_code, payload=payload, raw_body=raw_body)


def test_missing_key_fails_safely_without_transport():
    transport = FakeTransport()

    with pytest.raises(JQuantsConfigurationError) as error:
        JQuantsV2Client(api_key=" ")

    assert "test-api-key" not in str(error.value)
    assert not transport.calls


def test_fetches_one_page_with_api_key_and_returns_raw_body_and_metadata():
    raw_body = b'{"data":[{"Code":"72030"}]}'
    transport = FakeTransport(_response({"data": [{"Code": "72030"}]}, raw_body=raw_body))
    client, _ = _client(transport)

    page = next(client.iter_pages("/equities/bars/daily", {"date": "2026-10-20"}))

    url, request = transport.calls[0]
    assert url == "https://api.jquants.com/v2/equities/bars/daily"
    assert request["headers"] == {"x-api-key": "test-api-key"}
    assert request["allow_redirects"] is False
    assert request["timeout"] == 30
    assert page.data == ({"Code": "72030"},)
    assert page.raw_body == raw_body
    assert page.metadata.endpoint == "equities/bars/daily"
    assert page.metadata.page_number == 1
    assert page.metadata.row_count == 1
    assert page.metadata.has_next_page is False


def test_multiple_pages_pass_pagination_key_only_on_following_request():
    transport = FakeTransport(
        _response({"data": [{"n": 1}], "pagination_key": "opaque-token"}),
        _response({"data": [{"n": 2}]}),
    )
    client, _ = _client(transport)
    params = {"code": "72030"}

    pages = list(client.iter_pages("equities/bars/minute", params))

    assert [page.data for page in pages] == [({"n": 1},), ({"n": 2},)]
    assert pages[0].metadata.has_next_page is True
    assert pages[1].metadata.page_number == 2
    assert transport.calls[0][1]["params"] == {"code": "72030"}
    assert transport.calls[1][1]["params"] == {
        "code": "72030",
        "pagination_key": "opaque-token",
    }
    assert "opaque-token" not in repr(pages[0].metadata)


def test_empty_data_is_a_successful_page():
    transport = FakeTransport(_response({"data": []}))
    client, _ = _client(transport)

    page = next(client.iter_pages("equities/bars/daily"))

    assert page.data == ()
    assert page.metadata.row_count == 0


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"data": None},
        {"data": {}},
        {"data": ["not-an-object"]},
        {"data": [], "pagination_key": ""},
        {"data": [], "pagination_key": 42},
    ],
)
def test_malformed_data_and_pagination_are_not_treated_as_empty(payload):
    client, _ = _client(FakeTransport(_response(payload)))

    with pytest.raises(JQuantsResponseError) as error:
        next(client.iter_pages("equities/bars/daily"))

    assert error.value.category == "RESPONSE_SCHEMA_ERROR"


def test_invalid_json_is_not_treated_as_an_empty_result():
    client, _ = _client(FakeTransport(_response(ValueError("private response body"))))

    with pytest.raises(JQuantsResponseError, match="invalid JSON"):
        next(client.iter_pages("equities/bars/daily"))


def test_failure_on_a_later_page_propagates_without_success_shaped_fallback():
    transport = FakeTransport(
        _response({"data": [{"n": 1}], "pagination_key": "next"}),
        _response(status_code=500),
        _response(status_code=500),
        _response(status_code=500),
    )
    client, _ = _client(transport, max_attempts=3)
    pages = client.iter_pages("equities/bars/daily")

    assert next(pages).data == ({"n": 1},)
    with pytest.raises(JQuantsServerError) as error:
        next(pages)
    assert error.value.category == "SERVER_ERROR"


def test_repeated_pagination_key_raises_cycle_error():
    transport = FakeTransport(
        _response({"data": [{"n": 1}], "pagination_key": "loop"}),
        _response({"data": [{"n": 2}], "pagination_key": "loop"}),
    )
    client, _ = _client(transport)

    with pytest.raises(JQuantsPaginationError):
        list(client.iter_pages("equities/bars/daily"))

    assert len(transport.calls) == 2


def test_caller_parameters_are_not_mutated():
    params = {"code": "72030", "date": "2026-10-20"}
    original = dict(params)
    transport = FakeTransport(
        _response({"data": [], "pagination_key": "next"}),
        _response({"data": []}),
    )
    client, _ = _client(transport)

    list(client.iter_pages("equities/bars/minute", params))

    assert params == original


@pytest.mark.parametrize(
    ("status_code", "error_type"),
    [
        (400, JQuantsClientRequestError),
        (401, JQuantsAuthenticationError),
        (403, JQuantsContractAccessError),
    ],
)
def test_client_does_not_retry_non_retryable_client_errors(status_code, error_type):
    transport = FakeTransport(_response({}, status_code=status_code))
    client, _ = _client(transport)

    with pytest.raises(error_type) as error:
        next(client.iter_pages("equities/bars/daily"))

    assert len(transport.calls) == 1
    expected_category = {
        400: "CLIENT_REQUEST_ERROR",
        401: "AUTH_ERROR",
        403: "PERMISSION_OR_CONTRACT_ERROR",
    }[status_code]
    assert error.value.category == expected_category


def test_429_waits_between_bounded_attempts_without_real_waiting():
    transport = FakeTransport(
        _response({}, status_code=429),
        _response({}, status_code=429),
        _response({}, status_code=429),
    )
    client, clock = _client(transport, max_attempts=3, max_429_per_run=10)

    with pytest.raises(JQuantsRateLimitError) as error:
        next(client.iter_pages("equities/bars/daily"))

    assert len(transport.calls) == 3
    assert clock.sleeps.count(120) == 2
    assert client.metrics.request_count == 3
    assert client.metrics.page_count == 0
    assert client.metrics.status_counts == {"429": 3}
    assert client.metrics.retry_count == 2
    assert client.metrics.rate_limit_429_count == 3
    assert client.metrics.wait_seconds == 240
    assert error.value.category == "RATE_LIMITED"


def test_cumulative_429_cap_aborts_run_across_pages_without_extra_wait():
    page1 = _response({"data": [], "pagination_key": "k1"})
    transport = FakeTransport(
        page1,
        _response({}, status_code=429),
        _response({}, status_code=429),
        _response({}, status_code=429),
    )
    client, clock = _client(transport, max_attempts=4, max_429_per_run=3)
    pages = client.iter_pages("equities/bars/daily")

    assert next(pages).metadata.page_number == 1
    with pytest.raises(JQuantsRateLimitAbortError) as error:
        next(pages)

    assert error.value.category == "RATE_LIMIT_ABORTED"
    assert len(transport.calls) == 4
    assert clock.sleeps.count(120) == 2
    assert client.metrics.rate_limit_429_count == 3
    assert client.metrics.wait_seconds < 3 * 120


def test_429_below_cap_still_recovers():
    transport = FakeTransport(_response({}, status_code=429), _response({"data": []}))
    client, clock = _client(transport, max_attempts=4, max_429_per_run=3)

    assert next(client.iter_pages("equities/bars/daily")).metadata.row_count == 0
    assert clock.sleeps.count(120) == 1


def test_non_positive_429_cap_is_rejected():
    with pytest.raises(JQuantsConfigurationError):
        _client(FakeTransport(), max_429_per_run=0)


@pytest.mark.parametrize(
    "failure",
    [
            requests.ConnectionError("test-api-key and private body"),
            requests.Timeout("test-api-key and private body"),
    ],
)
def test_transient_transport_failures_retry_with_bounded_backoff(failure):
    transport = FakeTransport(failure, _response({"data": []}))
    client, clock = _client(transport, max_attempts=2)

    assert next(client.iter_pages("equities/bars/daily")).data == ()
    assert len(transport.calls) == 2
    assert clock.sleeps.count(1) == 1
    assert client.metrics.request_count == 2
    assert client.metrics.page_count == 1
    assert client.metrics.status_counts == {"200": 1}
    assert client.metrics.retry_count == 1
    assert client.metrics.wait_seconds == 1.2
    assert JQuantsTransportError.category == "TRANSPORT_ERROR"


def test_server_errors_retry_with_exponential_backoff():
    transport = FakeTransport(
        _response({}, status_code=503),
        _response({"data": []}),
    )
    client, clock = _client(transport, max_attempts=2)

    assert next(client.iter_pages("equities/bars/daily")).data == ()
    assert len(transport.calls) == 2
    assert clock.sleeps.count(1) == 1


def test_retry_backoff_is_capped_by_configuration(monkeypatch):
    monkeypatch.setattr(config, "JQUANTS_RETRY_BASE_SECONDS", 10)
    monkeypatch.setattr(config, "JQUANTS_RETRY_MAX_SECONDS", 12)
    transport = FakeTransport(
        _response(status_code=503),
        _response(status_code=503),
        _response(status_code=503),
        _response({"data": []}),
    )
    client, clock = _client(transport, max_attempts=4)

    assert next(client.iter_pages("equities/bars/daily")).data == ()
    assert clock.sleeps == [10, 12, 12]


def test_keyboard_interrupt_is_not_swallowed():
    interrupt = KeyboardInterrupt()
    client, _ = _client(FakeTransport(interrupt))

    with pytest.raises(KeyboardInterrupt) as error:
        next(client.iter_pages("equities/bars/daily"))

    assert error.value is interrupt


def test_every_retry_attempt_passes_through_the_shared_rate_limiter():
    clock = FakeClock()
    limiter = JQuantsRateLimiter(1.2, clock=clock.monotonic, sleep=clock.sleep)
    first_transport = FakeTransport(
        _response({}, status_code=503),
        _response({"data": []}),
    )
    second_transport = FakeTransport(_response({"data": []}))
    first = JQuantsV2Client(
        "dummy", rate_limiter=limiter, sleep=clock.sleep, max_attempts=2, transport=first_transport
    )
    second = JQuantsV2Client(
        "dummy", rate_limiter=limiter, sleep=clock.sleep, transport=second_transport
    )

    next(first.iter_pages("equities/bars/daily"))
    next(second.iter_pages("equities/bars/daily"))

    assert clock.sleeps == pytest.approx([1, 0.2, 1.2])


def test_clients_share_rate_limiter_when_given_the_same_instance():
    clock = FakeClock()
    limiter = JQuantsRateLimiter(1.2, clock=clock.monotonic, sleep=clock.sleep)
    first = JQuantsV2Client(
        "dummy", rate_limiter=limiter, sleep=clock.sleep, transport=FakeTransport(_response({"data": []}))
    )
    second = JQuantsV2Client(
        "dummy", rate_limiter=limiter, sleep=clock.sleep, transport=FakeTransport(_response({"data": []}))
    )

    next(first.iter_pages("equities/bars/daily"))
    next(second.iter_pages("equities/bars/daily"))

    assert clock.sleeps == [1.2]


@pytest.mark.parametrize(
    "endpoint",
    [
        "https://attacker.example/data",
        "//attacker.example/data",
        "../data",
        "equities/bars/daily?redirect=https://attacker.example",
        "equities/bars/daily#fragment",
    ],
)
def test_unsafe_or_non_official_endpoint_is_rejected_before_sending_key(endpoint):
    transport = FakeTransport()
    client, _ = _client(transport)

    with pytest.raises(JQuantsConfigurationError):
        next(client.iter_pages(endpoint))

    assert transport.calls == []


@pytest.mark.parametrize(
    "location",
    [
        "http://api.jquants.com/v2/redirected",
        "https://attacker.example/collect",
    ],
)
def test_redirect_response_is_not_followed_or_sent_to_another_host(location):
    response = FakeResponse(status_code=302, headers={"Location": location})
    transport = FakeTransport(response)
    client, _ = _client(transport)

    with pytest.raises(JQuantsHTTPError):
        next(client.iter_pages("equities/bars/daily"))

    assert len(transport.calls) == 1
    request_url, request = transport.calls[0]
    assert request["allow_redirects"] is False
    assert request_url.startswith("https://api.jquants.com/")
    assert "x-api-key" in request["headers"]


def test_modified_api_base_url_is_rejected_before_sending_key(monkeypatch):
    transport = FakeTransport()
    monkeypatch.setattr(config, "JQUANTS_API_BASE_URL", "https://attacker.example/v2")

    with pytest.raises(JQuantsConfigurationError):
        _client(transport)

    assert transport.calls == []


def test_page_repr_does_not_include_raw_market_data_or_pagination_key():
    transport = FakeTransport(
        _response(
            {"data": [{"close": 123.45}], "pagination_key": "opaque-pagination-key"},
            raw_body=b'{"data":[{"close":123.45}],"pagination_key":"opaque-pagination-key"}',
        )
    )
    client, _ = _client(transport)

    page = next(client.iter_pages("equities/bars/daily"))

    assert "123.45" not in repr(page)
    assert "opaque-pagination-key" not in repr(page)


def test_secret_and_response_body_are_not_exposed_by_logs_or_traceback(caplog):
    secret = "test-api-key"
    private_body = "private-quote-data-never-log"
    transport = FakeTransport(
        requests.ConnectionError(f"{secret} {private_body}"),
        requests.ConnectionError(f"{secret} {private_body}"),
    )
    client, _ = _client(transport, max_attempts=2)

    with caplog.at_level(logging.DEBUG):
        with pytest.raises(JQuantsTransportError) as error:
            next(client.iter_pages("equities/bars/daily"))

    rendered = "".join(traceback.format_exception(error.type, error.value, error.tb))
    assert secret not in rendered
    assert private_body not in rendered
    assert secret not in caplog.text
    assert private_body not in caplog.text
    assert error.value.__context__ is None


def test_http_error_and_pagination_cycle_do_not_expose_body_or_key(caplog):
    private_body = '{"error":"outside contract","pagination_key":"private-page-token"}'
    client, _ = _client(
        FakeTransport(
            _response({}, status_code=400, raw_body=private_body.encode())
        )
    )

    with caplog.at_level(logging.DEBUG):
        with pytest.raises(JQuantsClientRequestError) as http_error:
            next(client.iter_pages("equities/bars/daily"))

    rendered = "".join(
        traceback.format_exception(http_error.type, http_error.value, http_error.tb)
    )
    assert "private-page-token" not in rendered
    assert private_body not in rendered
    assert private_body not in caplog.text

    cycle_transport = FakeTransport(
        _response({"data": [], "pagination_key": "private-page-token"}),
        _response({"data": [], "pagination_key": "private-page-token"}),
    )
    cycle_client, _ = _client(cycle_transport)
    with pytest.raises(JQuantsPaginationError) as cycle_error:
        list(cycle_client.iter_pages("equities/bars/daily"))

    assert "private-page-token" not in str(cycle_error.value)
