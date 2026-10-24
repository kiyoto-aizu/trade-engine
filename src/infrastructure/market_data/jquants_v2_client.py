"""安全なJ-Quants API V2取得クライアント。"""

import math
import re
import threading
import time
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol
from urllib.parse import urlsplit

import requests
from requests.adapters import HTTPAdapter

from src.config import config

_ENDPOINT_SEGMENT = re.compile(r"[A-Za-z0-9_-]+\Z")


class JQuantsAPIError(Exception):
    """本文・URL・認証情報を含めないJ-Quants APIエラー。"""

    category = "API_ERROR"


class JQuantsConfigurationError(JQuantsAPIError):
    """クライアント設定が不正または不足している。"""


class JQuantsAuthenticationError(JQuantsAPIError):
    """APIキーが認証されなかった。"""

    category = "AUTH_ERROR"


class JQuantsContractAccessError(JQuantsAPIError):
    """アクセスまたは契約上の制限が疑われるが、最終判定は呼び出し側に委ねる。"""

    category = "PERMISSION_OR_CONTRACT_ERROR"


class JQuantsRateLimitError(JQuantsAPIError):
    """レート制限が試行上限まで継続した。"""

    category = "RATE_LIMITED"


class JQuantsRateLimitAbortError(JQuantsRateLimitError):
    """1実行内の429累計が上限に達し、残りの取得を打ち切った。"""

    category = "RATE_LIMIT_ABORTED"


class JQuantsTransportError(JQuantsAPIError):
    """一時的な通信失敗が試行上限まで継続した。"""

    category = "TRANSPORT_ERROR"


class JQuantsServerError(JQuantsAPIError):
    """サーバーエラーが試行上限まで継続した。"""

    category = "SERVER_ERROR"


class JQuantsHTTPError(JQuantsAPIError):
    """再試行しないHTTPエラー。"""

    category = "HTTP_ERROR"

    def __init__(self, status_code: int) -> None:
        self.status_code = status_code
        super().__init__(f"J-Quants API returned HTTP {status_code}")


class JQuantsClientRequestError(JQuantsHTTPError):
    """再試行しない4xx要求エラー。"""

    category = "CLIENT_REQUEST_ERROR"

class JQuantsResponseError(JQuantsAPIError):
    """JSON応答またはページング項目の形式が不正。"""

    category = "RESPONSE_SCHEMA_ERROR"


class JQuantsPaginationError(JQuantsAPIError):
    """ページキーが循環している。"""

    category = "RESPONSE_SCHEMA_ERROR"


@dataclass(frozen=True)
class JQuantsPageMetadata:
    endpoint: str
    page_number: int
    row_count: int
    status_code: int
    has_next_page: bool


@dataclass(frozen=True)
class JQuantsPage:
    data: tuple[Mapping[str, Any], ...] = field(repr=False)
    raw_body: bytes = field(repr=False)
    metadata: JQuantsPageMetadata


@dataclass
class JQuantsRequestMetrics:
    request_count: int = 0
    page_count: int = 0
    status_counts: dict[str, int] = field(default_factory=dict)
    retry_count: int = 0
    rate_limit_429_count: int = 0
    wait_seconds: float = 0.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "request_count": self.request_count,
            "page_count": self.page_count,
            "status_counts": dict(sorted(self.status_counts.items())),
            "retry_count": self.retry_count,
            "rate_limit_429_count": self.rate_limit_429_count,
            "wait_seconds": self.wait_seconds,
        }


class _Response(Protocol):
    status_code: int
    content: bytes

    def json(self) -> Any: ...


class _Transport(Protocol):
    def get(
        self,
        url: str,
        *,
        params: Mapping[str, Any],
        headers: Mapping[str, str],
        timeout: float,
        allow_redirects: bool,
    ) -> _Response: ...


class JQuantsRateLimiter:
    """同一プロセス内のクライアント間で共有できるリクエスト間隔制御。"""

    def __init__(
        self,
        minimum_interval_seconds: float,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if not math.isfinite(minimum_interval_seconds) or minimum_interval_seconds < 0:
            raise JQuantsConfigurationError("J-Quants request interval must be non-negative")
        self._minimum_interval_seconds = minimum_interval_seconds
        self._clock = clock
        self._sleep = sleep
        self._last_request_at: float | None = None
        self._lock = threading.Lock()

    def wait_for_slot(self) -> float:
        with self._lock:
            now = self._clock()
            waited = 0.0
            if self._last_request_at is not None:
                remaining = self._minimum_interval_seconds - (now - self._last_request_at)
                if remaining > 0:
                    self._sleep(remaining)
                    waited = remaining
            self._last_request_at = self._clock()
            return waited


_DEFAULT_RATE_LIMITER = JQuantsRateLimiter(config.JQUANTS_REQUEST_INTERVAL_SECONDS)
_FIXED_BASE_URL = "https://api.jquants.com/v2"


def _safe_endpoint(endpoint: str) -> str:
    if not isinstance(endpoint, str):
        raise JQuantsConfigurationError("J-Quants endpoint must be a relative API path")
    parsed = urlsplit(endpoint)
    path = parsed.path.strip("/")
    segments = path.split("/") if path else []
    if (
        parsed.scheme
        or parsed.netloc
        or parsed.query
        or parsed.fragment
        or not segments
        or any(segment in {".", ".."} or not _ENDPOINT_SEGMENT.fullmatch(segment) for segment in segments)
    ):
        raise JQuantsConfigurationError("J-Quants endpoint must be a safe relative API path")
    return "/".join(segments)


def _safe_base_url() -> str:
    if config.JQUANTS_API_BASE_URL != _FIXED_BASE_URL:
        raise JQuantsConfigurationError("J-Quants API base URL must remain the official V2 host")
    return _FIXED_BASE_URL


def _new_session() -> requests.Session:
    session = requests.Session()
    adapter = HTTPAdapter(max_retries=0)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


class JQuantsV2Client:
    """J-Quants V2のページ単位取得クライアント。"""

    def __init__(
        self,
        api_key: str | None = None,
        *,
        timeout_seconds: float | None = None,
        max_attempts: int | None = None,
        max_429_per_run: int | None = None,
        rate_limiter: JQuantsRateLimiter | None = None,
        sleep: Callable[[float], None] = time.sleep,
        transport: _Transport | None = None,
    ) -> None:
        resolved_key = config.JQUANTS_API_KEY if api_key is None else api_key
        if not isinstance(resolved_key, str) or not resolved_key.strip():
            raise JQuantsConfigurationError("JQUANTS_API_KEY is required")
        self._api_key = resolved_key.strip()

        self._timeout_seconds = (
            config.JQUANTS_TIMEOUT_SECONDS if timeout_seconds is None else timeout_seconds
        )
        self._max_attempts = config.JQUANTS_MAX_ATTEMPTS if max_attempts is None else max_attempts
        if (
            not isinstance(self._timeout_seconds, (int, float))
            or not math.isfinite(self._timeout_seconds)
            or self._timeout_seconds <= 0
        ):
            raise JQuantsConfigurationError("J-Quants timeout must be positive")
        if not isinstance(self._max_attempts, int) or self._max_attempts <= 0:
            raise JQuantsConfigurationError("J-Quants max attempts must be positive")
        self._max_429_per_run = (
            config.JQUANTS_MAX_429_PER_RUN if max_429_per_run is None else max_429_per_run
        )
        if not isinstance(self._max_429_per_run, int) or self._max_429_per_run <= 0:
            raise JQuantsConfigurationError("J-Quants max 429 per run must be positive")

        self._rate_limiter = rate_limiter if rate_limiter is not None else _DEFAULT_RATE_LIMITER
        self._sleep = sleep
        self.metrics = JQuantsRequestMetrics()
        self._base_url = _safe_base_url()
        if transport is None:
            self._session = _new_session()
            self._transport: _Transport = self._session
        else:
            self._session = None
            self._transport = transport

    def close(self) -> None:
        if self._session is not None:
            self._session.close()

    def iter_pages(
        self,
        endpoint: str,
        params: Mapping[str, Any] | None = None,
    ) -> Iterator[JQuantsPage]:
        """取得結果をページごとに返す。最後まで反復しない限り全件取得ではない。"""
        safe_endpoint = _safe_endpoint(endpoint)
        original_params = dict(params or {})
        if "pagination_key" in original_params:
            raise JQuantsConfigurationError("pagination_key is managed by the client")

        next_key: str | None = None
        used_keys: set[str] = set()
        page_number = 0
        while True:
            request_params = dict(original_params)
            if next_key is not None:
                if next_key in used_keys:
                    raise JQuantsPaginationError("J-Quants pagination key cycle detected")
                used_keys.add(next_key)
                request_params["pagination_key"] = next_key

            page, returned_key = self._get_page(safe_endpoint, request_params, page_number + 1)
            if returned_key is not None and returned_key in used_keys:
                raise JQuantsPaginationError("J-Quants pagination key cycle detected")

            yield page
            page_number += 1
            if returned_key is None:
                return
            next_key = returned_key

    def _get_page(
        self,
        endpoint: str,
        params: Mapping[str, Any],
        page_number: int,
    ) -> tuple[JQuantsPage, str | None]:
        url = f"{self._base_url}/{endpoint}"
        headers = {"x-api-key": self._api_key}
        response: _Response | None = None

        for attempt in range(1, self._max_attempts + 1):
            self.metrics.wait_seconds += self._rate_limiter.wait_for_slot()
            request_failed = False
            retryable_transport_failure = False
            self.metrics.request_count += 1
            try:
                response = self._transport.get(
                    url,
                    params=dict(params),
                    headers=headers,
                    timeout=self._timeout_seconds,
                    allow_redirects=False,
                )
            except (requests.ConnectionError, requests.Timeout):
                request_failed = True
                retryable_transport_failure = True
            except requests.RequestException:
                request_failed = True
            if request_failed:
                if retryable_transport_failure and attempt < self._max_attempts:
                    self._wait(self._backoff_seconds(attempt))
                    self.metrics.retry_count += 1
                    continue
                raise JQuantsTransportError(
                    f"J-Quants request failed after {attempt} attempt(s)"
                ) from None

            assert response is not None
            status_code = response.status_code
            if not isinstance(status_code, int) or not 100 <= status_code <= 599:
                raise JQuantsResponseError("J-Quants returned an invalid HTTP status") from None
            status_key = str(status_code)
            self.metrics.status_counts[status_key] = (
                self.metrics.status_counts.get(status_key, 0) + 1
            )
            if 200 <= status_code < 300:
                break
            if status_code == 429:
                self.metrics.rate_limit_429_count += 1
                if self.metrics.rate_limit_429_count >= self._max_429_per_run:
                    raise JQuantsRateLimitAbortError(
                        "J-Quants run aborted: cumulative 429 limit reached "
                        f"({self.metrics.rate_limit_429_count})"
                    ) from None
                if attempt < self._max_attempts:
                    self._wait(config.JQUANTS_429_WAIT_SECONDS)
                    self.metrics.retry_count += 1
                    continue
                raise JQuantsRateLimitError(
                    f"J-Quants rate limit persisted after {attempt} attempt(s)"
                ) from None
            if 500 <= status_code < 600:
                if attempt < self._max_attempts:
                    self._wait(self._backoff_seconds(attempt))
                    self.metrics.retry_count += 1
                    continue
                raise JQuantsServerError(
                    f"J-Quants server error persisted after {attempt} attempt(s)"
                ) from None
            if status_code == 401:
                raise JQuantsAuthenticationError("J-Quants authentication failed") from None
            if status_code == 403:
                raise JQuantsContractAccessError("J-Quants access is not permitted") from None
            if 400 <= status_code < 500:
                raise JQuantsClientRequestError(status_code) from None
            raise JQuantsHTTPError(status_code) from None

        if response is None:
            raise JQuantsAPIError("J-Quants request did not produce a response") from None

        json_failed = False
        try:
            payload = response.json()
        except (ValueError, TypeError, AttributeError):
            payload = None
            json_failed = True
        if json_failed or not isinstance(payload, dict):
            raise JQuantsResponseError("J-Quants returned invalid JSON") from None

        data = payload.get("data")
        if not isinstance(data, list) or any(not isinstance(row, Mapping) for row in data):
            raise JQuantsResponseError("J-Quants response data must be a list of objects") from None

        returned_key = payload.get("pagination_key")
        if returned_key is not None and (
            not isinstance(returned_key, str) or not returned_key
        ):
            raise JQuantsResponseError("J-Quants pagination_key is invalid") from None
        raw_body = response.content
        if not isinstance(raw_body, bytes):
            raise JQuantsResponseError("J-Quants response body is invalid") from None

        page = JQuantsPage(
            data=tuple(data),
            raw_body=raw_body,
            metadata=JQuantsPageMetadata(
                endpoint=endpoint,
                page_number=page_number,
                row_count=len(data),
                status_code=response.status_code,
                has_next_page=returned_key is not None,
            ),
        )
        self.metrics.page_count += 1
        return page, returned_key

    def _wait(self, seconds: float) -> None:
        self._sleep(seconds)
        self.metrics.wait_seconds += seconds

    @staticmethod
    def _backoff_seconds(attempt: int) -> float:
        base = config.JQUANTS_RETRY_BASE_SECONDS
        maximum = config.JQUANTS_RETRY_MAX_SECONDS
        return min(base * (2 ** (attempt - 1)), maximum)
