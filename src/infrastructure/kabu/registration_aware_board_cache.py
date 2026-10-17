from concurrent.futures import Future, ThreadPoolExecutor
from datetime import datetime
from threading import Condition
from time import perf_counter
from uuid import uuid4

from src.domain.board_request_context import board_request_context


class _FetchNotStarted(Exception):
    pass


class RegistrationAwareBoardCache:
    """板取得を共有し、並列取得中の登録解除と同一銘柄の重複取得を防ぎます。"""

    def __init__(self, board_client, unregister_callback, batch_size: int = 50, max_workers: int = 3):
        self.board_client = board_client
        self.unregister_callback = unregister_callback
        self.batch_size = batch_size
        self.max_workers = max_workers
        self._cache: dict[str, tuple[str, object]] = {}
        self._requests_since_clear = 0
        self._blocked_error: Exception | None = None
        self._condition = Condition()
        self._inflight: dict[str, Future] = {}
        self._retry_round_results: dict[str, dict[int, object | Exception]] = {}
        self.fetch_count = 0
        self.retry_fetch_count = 0
        self.cache_hit_count = 0
        self.fetch_duration_ms = 0.0
        self.retry_fetch_duration_ms = 0.0
        self.unregister_count = 0
        self.max_concurrency_observed = 0
        self._active_requests = 0
        self._started_at = perf_counter()
        self._last_clear_at: float | None = None
        self._fetch_meta: dict[str, dict] = {}
        self._retry_meta: dict[str, dict] = {}
        self._first_fetch_at: dict[str, float] = {}
        self._request_ids: dict[str, str] = {}
        self._last_fetch_at: float | None = None
        # 遅さの原因調査用。板取得1件ごとの時刻・所要時間・登録数を、取得中の価格帯ラベル付きで残す
        self.current_band: str | None = None
        self._fetch_log: list[dict] = []

    def get_fetch_log(self, band: str | None = None) -> list[dict]:
        with self._condition:
            return [dict(entry) for entry in self._fetch_log if band is None or entry["band"] == band]

    def _append_fetch_log(self, key, *, force, started_wall, elapsed_ms, registered_count, ok) -> None:
        self._fetch_log.append({
            "symbol": key,
            "band": self.current_band,
            "kind": "retry" if force else "initial",
            "started_at": started_wall.isoformat(timespec="milliseconds"),
            "completed_at": datetime.now().isoformat(timespec="milliseconds"),
            "elapsed_ms": round(elapsed_ms, 3),
            # このリクエストを含む、直近の登録解除以降の登録数
            "registered_count": registered_count,
            "ok": ok,
        })

    @property
    def requests_since_clear(self) -> int:
        with self._condition:
            return self._requests_since_clear

    @property
    def seconds_since_last_clear(self) -> float:
        with self._condition:
            base = self._last_clear_at if self._last_clear_at is not None else self._started_at
        return perf_counter() - base

    def get_fetch_meta(self, symbol) -> dict | None:
        with self._condition:
            return self._fetch_meta.get(str(symbol))

    def get_retry_meta(self, symbol) -> dict | None:
        with self._condition:
            return self._retry_meta.get(str(symbol))

    def get_request_id(self, symbol) -> str | None:
        with self._condition:
            return self._request_ids.get(str(symbol))

    def get_metrics_snapshot(self) -> dict:
        with self._condition:
            metrics = {
                "fetch_count": self.fetch_count,
                "retry_fetch_count": self.retry_fetch_count,
                "cache_hit_count": self.cache_hit_count,
                "fetch_duration_ms": round(self.fetch_duration_ms, 3),
                "retry_fetch_duration_ms": round(self.retry_fetch_duration_ms, 3),
                "unregister_count": self.unregister_count,
                "max_concurrency_configured": self.max_workers,
                "max_concurrency_observed": self.max_concurrency_observed,
            }
        stats_getter = getattr(self.board_client, "get_rate_limit_stats", None)
        metrics["rate_limit"] = stats_getter() if callable(stats_getter) else {
            "responses": 0, "retries": 0, "succeeded": 0, "failed": 0,
        }
        return metrics

    def _clear_registrations_locked(self) -> bool:
        self.unregister_count += 1
        try:
            result = self.unregister_callback()
        except Exception:
            self._blocked_error = RuntimeError("銘柄登録解除に失敗しました")
            return False
        if result is None:
            self._blocked_error = RuntimeError("銘柄登録解除に失敗しました")
            return False
        self._requests_since_clear = 0
        self._last_clear_at = perf_counter()
        self._blocked_error = None
        return True

    def clear_registrations(self) -> bool:
        with self._condition:
            self._condition.wait_for(lambda: not self._inflight)
            return self._clear_registrations_locked()

    def get_current_board(self, symbol):
        return self._fetch_current_board(symbol, force=False)

    def refetch_current_board(self, symbol, should_start=None, retry_round=None):
        return self._fetch_current_board(
            symbol, force=True, should_start=should_start, retry_round=retry_round
        )

    def _fetch_current_board(self, symbol, *, force: bool, should_start=None, retry_round=None):
        key = str(symbol)
        leader = False
        cleared_for_request = False
        with self._condition:
            while True:
                if force and retry_round is not None:
                    saved_rounds = self._retry_round_results.get(key, {})
                    if retry_round in saved_rounds:
                        saved_result = saved_rounds[retry_round]
                        if isinstance(saved_result, Exception):
                            raise saved_result
                        return saved_result
                saved = self._cache.get(key)
                if not force and saved is not None:
                    self.cache_hit_count += 1
                    if saved[0] == "error":
                        raise saved[1]
                    return saved[1]
                future = self._inflight.get(key)
                if future is not None:
                    break
                if should_start is not None and not should_start():
                    raise _FetchNotStarted()
                if self._blocked_error is not None:
                    raise self._blocked_error
                if self._requests_since_clear >= self.batch_size:
                    self._condition.wait_for(lambda: not self._inflight)
                    if should_start is not None and not should_start():
                        raise _FetchNotStarted()
                    if not self._clear_registrations_locked():
                        raise self._blocked_error
                    cleared_for_request = True
                    continue

                future = Future()
                self._inflight[key] = future
                request_id = self._request_ids.setdefault(key, uuid4().hex)
                request_count_before = self._requests_since_clear
                self._requests_since_clear += 1
                self.fetch_count += 1
                if force:
                    self.retry_fetch_count += 1
                started = perf_counter()
                started_wall = datetime.now()
                self._active_requests += 1
                self.max_concurrency_observed = max(self.max_concurrency_observed, self._active_requests)
                fetch_sequence = self.fetch_count
                previous_fetch_at = self._last_fetch_at
                self._last_fetch_at = started
                self._first_fetch_at.setdefault(key, started)
                if force:
                    first = self._first_fetch_at.get(key)
                    self._retry_meta[key] = {
                        "request_id": request_id,
                        "retry_fetch_seq": fetch_sequence,
                        "retry_requests_since_clear": request_count_before,
                        "retry_preceded_by_registration_clear": cleared_for_request,
                        "retry_seconds_since_first_fetch": round(started - first, 3) if first is not None else None,
                        "retry_seconds_since_previous_fetch": (
                            round(started - previous_fetch_at, 3) if previous_fetch_at is not None else None
                        ),
                    }
                else:
                    self._fetch_meta[key] = {
                        "request_id": request_id,
                        "board_fetch_seq": fetch_sequence,
                        "requests_since_clear": request_count_before,
                        "seconds_since_last_clear": round(self.seconds_since_last_clear, 3),
                        "seconds_since_previous_fetch": (
                            round(started - previous_fetch_at, 3) if previous_fetch_at is not None else None
                        ),
                    }
                leader = True
                break

        if not leader:
            return future.result()

        diagnostic_getter = getattr(self.board_client, "get_current_board_for_diagnostics", None)
        fetch = diagnostic_getter or self.board_client.get_current_board
        try:
            with board_request_context(request_id):
                board = fetch(symbol)
        except Exception as exc:
            with self._condition:
                elapsed_ms = (perf_counter() - started) * 1000
                if force:
                    if retry_round is not None:
                        self._retry_round_results.setdefault(key, {})[retry_round] = exc
                    self._retry_meta[key]["retry_elapsed_ms"] = round(elapsed_ms, 3)
                    self.retry_fetch_duration_ms += elapsed_ms
                else:
                    self._cache[key] = ("error", exc)
                    self._fetch_meta[key]["board_fetch_elapsed_ms"] = round(elapsed_ms, 3)
                self.fetch_duration_ms += elapsed_ms
                self._append_fetch_log(
                    key, force=force, started_wall=started_wall, elapsed_ms=elapsed_ms,
                    registered_count=request_count_before + 1, ok=False,
                )
                self._finish_request(key, future, exception=exc)
            raise

        with self._condition:
            elapsed_ms = (perf_counter() - started) * 1000
            if force:
                if retry_round is not None:
                    self._retry_round_results.setdefault(key, {})[retry_round] = board
                self._retry_meta[key]["retry_elapsed_ms"] = round(elapsed_ms, 3)
                self.retry_fetch_duration_ms += elapsed_ms
                if board:
                    self._cache[key] = ("result", board)
            else:
                self._cache[key] = ("result", board)
                self._fetch_meta[key]["board_fetch_elapsed_ms"] = round(elapsed_ms, 3)
            self.fetch_duration_ms += elapsed_ms
            self._append_fetch_log(
                key, force=force, started_wall=started_wall, elapsed_ms=elapsed_ms,
                registered_count=request_count_before + 1, ok=bool(board),
            )
            self._finish_request(key, future, result=board)
        return board

    def _finish_request(self, key, future, *, result=None, exception=None) -> None:
        self._active_requests -= 1
        self._inflight.pop(key, None)
        if exception is not None:
            future.set_exception(exception)
        else:
            future.set_result(result)
        self._condition.notify_all()

    def fetch_current_boards(
        self, symbols, max_workers: int | None = None, should_start=None
    ) -> dict[str, object]:
        return self._fetch_many(symbols, force=False, max_workers=max_workers, should_start=should_start)

    def refetch_current_boards(
        self, symbols, max_workers: int | None = None, should_start=None, retry_round=None
    ) -> dict[str, object]:
        return self._fetch_many(
            symbols, force=True, max_workers=max_workers, should_start=should_start, retry_round=retry_round
        )

    def _fetch_many(
        self, symbols, *, force: bool, max_workers: int | None, should_start=None, retry_round=None
    ) -> dict[str, object]:
        unique_symbols = list(dict.fromkeys(str(symbol) for symbol in symbols))

        def fetch_one(symbol):
            try:
                result = self._fetch_current_board(
                    symbol, force=force, should_start=should_start, retry_round=retry_round
                )
            except _FetchNotStarted:
                return symbol, False, None
            except Exception as exc:
                return symbol, True, exc
            return symbol, True, result

        with ThreadPoolExecutor(max_workers=max_workers or self.max_workers) as executor:
            futures = [executor.submit(fetch_one, symbol) for symbol in unique_symbols]
            return {
                symbol: result
                for future in futures
                for symbol, started, result in (future.result(),)
                if started
            }