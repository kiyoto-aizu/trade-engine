from datetime import date
import threading
from types import SimpleNamespace

import pytest

from src.application import filtering_usecase as filtering_usecase_module
from src.application.filtering_usecase import BoardRetryPolicy, FilteringDeadlineExceeded, FilteringUseCase
from src.config import config
from src.domain.board_request_context import current_board_request_id
from src.infrastructure.kabu.registration_aware_board_cache import RegistrationAwareBoardCache


class _ScreeningRepo:
    def __init__(self, symbols):
        self.symbols = symbols

    def load_for_date(self, _day):
        return SimpleNamespace(symbols=self.symbols)


class _ResultRepo:
    def __init__(self):
        self.saved = []

    def save(self, result):
        self.saved.append(result)


class _VolumeStub:
    def get_average_turnover(self, _symbol, _days):
        return 100.0


class _Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


class _FakeApi:
    """銘柄ごとの応答列(最後の要素は繰り返す)。取得ごとに時計を1進める。"""

    def __init__(self, scripts, clock=None):
        self.scripts = {symbol: list(items) for symbol, items in scripts.items()}
        self.calls = []
        self.request_ids = {}
        self.clock = clock
        self.lock = threading.Lock()

    def get_current_board_for_diagnostics(self, symbol):
        with self.lock:
            self.calls.append(symbol)
            self.request_ids.setdefault(symbol, []).append(current_board_request_id())
            if self.clock is not None:
                self.clock.now += 1
            items = self.scripts[symbol]
            item = items.pop(0) if len(items) > 1 else items[0]
        if isinstance(item, Exception):
            raise item
        return item


def _board(value=None, volume=None, price=500.0):
    return {
        "symbol_name": "x", "current_price": price, "current_price_status": 1,
        "trading_volume": volume, "trading_value": value, "response_keys": ["CurrentPrice"],
        "trading_volume_time": "t", "vwap": None, "raw_trading_volume": volume, "raw_trading_value": value,
    }


MISSING = _board()


def _run(scripts, symbols, *, rounds=2, wait=10.0, deadline=1000.0, retry=True, cache_batch=50):
    clock = _Clock()
    api = _FakeApi(scripts, clock)
    cache = RegistrationAwareBoardCache(api, lambda: {}, batch_size=cache_batch)
    sleeps = []
    results, diagnostics = _ResultRepo(), []

    def sleeper(seconds):
        sleeps.append(seconds)
        clock.now += seconds

    usecase = FilteringUseCase(
        _ScreeningRepo(symbols), cache, _VolumeStub(), results,
        diagnostics_repository=SimpleNamespace(save=diagnostics.append),
        sleep_func=sleeper, monotonic_clock=clock,
    )
    policy = BoardRetryPolicy(rounds, wait, deadline) if retry else None
    result = usecase.execute(target_date=date(2026, 10, 5), board_retry=policy)
    records = {r["symbol"]: r for r in diagnostics[0]["candidates"]}
    return SimpleNamespace(result=result, records=records, summary=diagnostics[0]["summary"], api=api,
                           cache=cache, sleeps=sleeps, saved=results.saved, usecase=usecase)


def test_retry_recovers_missing_board_and_scores_it():
    out = _run({"A": [MISSING, _board(value=1000.0, volume=10)], "B": [_board(value=500.0, volume=5)]}, ["A", "B"])
    record = out.records["A"]

    assert record["numerator_source"] == "TradingValue_retry"
    assert record["status"] == "evaluated" and record["reason_code"] is None
    assert (record["retry_attempted"], record["retry_succeeded"], record["retry_round_succeeded"]) == (True, True, 1)
    assert record["retry_board_trading_value"] == 1000.0
    assert record["retry_fetch_seq"] == 3
    assert record["retry_preceded_by_registration_clear"] is False
    assert record["retry_seconds_since_first_fetch"] is not None
    assert record["retry_seconds_since_previous_fetch"] is not None
    assert record["retry_elapsed_ms"] >= 0
    assert record["board_fetch_elapsed_ms"] >= 0
    assert "A" in out.result.symbols
    assert out.summary["retry_attempted_count"] == 1
    assert out.summary["retry_succeeded_count"] == 1
    assert out.summary["retry_round_success_counts"] == {"1": 1}
    assert out.summary["retry_stopped_by_cutoff"] is False
    assert out.summary["reason_counts"] == {}
    assert out.summary["board_api_metrics"]["fetch_count"] == 3
    assert out.summary["board_api_metrics"]["retry_fetch_count"] == 1
    assert out.summary["board_api_metrics"]["max_concurrency_configured"] == config.FILTER_BOARD_MAX_CONCURRENCY
    assert out.summary["board_run_api_metrics"]["fetch_count"] == 3
    assert out.sleeps == [10.0]
    assert len(set(out.api.request_ids["A"])) == 1
    assert out.api.request_ids["A"][0] is not None


def test_retry_uses_price_times_volume_when_value_missing():
    out = _run({"A": [MISSING, _board(volume=10, price=200.0)]}, ["A"])
    assert out.records["A"]["numerator_source"] == "current_price_x_cumulative_volume_retry"
    assert out.records["A"]["numerator"] == 2000.0


def test_still_missing_after_retry_stays_missing():
    out = _run({"A": [MISSING]}, ["A"])
    record = out.records["A"]

    assert record["reason_code"] == "FILTER_TURNOVER_MISSING"
    assert (record["retry_attempted"], record["retry_succeeded"], record["retry_round_succeeded"]) == (True, False, None)
    assert out.summary["reason_counts"] == {"FILTER_TURNOVER_MISSING": 1}
    assert out.summary["retry_rounds_run"] == 2
    assert out.api.calls == ["A", "A", "A"]
    assert out.saved


def test_second_round_success_is_recorded():
    out = _run({"A": [MISSING, MISSING, _board(value=1000.0, volume=1)]}, ["A"])
    assert out.records["A"]["retry_round_succeeded"] == 2
    assert out.summary["retry_round_success_counts"] == {"2": 1}
    assert out.sleeps == [10.0, 10.0]


def test_only_still_missing_symbols_are_retried_in_later_rounds():
    out = _run({"A": [MISSING, _board(value=1000.0, volume=1)], "B": [MISSING]}, ["A", "B"])
    assert sorted(out.api.calls) == ["A", "A", "B", "B", "B"]


def test_retry_refetches_even_when_cache_holds_the_missing_result():
    api = _FakeApi({"A": [MISSING, _board(value=1000.0, volume=1)]})
    cache = RegistrationAwareBoardCache(api, lambda: {})

    assert cache.get_current_board("A")["trading_value"] is None
    assert cache.get_current_board("A")["trading_value"] is None
    assert api.calls == ["A"]
    assert cache.refetch_current_board("A")["trading_value"] == 1000.0
    assert api.calls == ["A", "A"]
    assert cache.get_current_board("A")["trading_value"] == 1000.0
    assert api.calls == ["A", "A"]


def test_same_retry_round_is_reused_across_price_bands_but_next_round_fetches_again():
    api = _FakeApi({"A": [MISSING, MISSING, _board(value=1000.0, volume=1)]})
    cache = RegistrationAwareBoardCache(api, lambda: {})
    cache.get_current_board("A")

    assert cache.refetch_current_board("A", retry_round=1)["trading_value"] is None
    assert cache.refetch_current_board("A", retry_round=1)["trading_value"] is None
    assert api.calls == ["A", "A"]

    assert cache.refetch_current_board("A", retry_round=2)["trading_value"] == 1000.0
    assert cache.refetch_current_board("A", retry_round=2)["trading_value"] == 1000.0
    assert api.calls == ["A", "A", "A"]


def test_failed_refetch_keeps_the_cached_missing_result():
    api = _FakeApi({"A": [MISSING, RuntimeError("x")]})
    cache = RegistrationAwareBoardCache(api, lambda: {})
    first = cache.get_current_board("A")

    with pytest.raises(RuntimeError):
        cache.refetch_current_board("A")
    assert cache.get_current_board("A") is first


def test_retry_stops_at_cutoff_and_result_is_still_saved():
    # 初回3件で時計=3。待機後=13。Aの取得後=14でBの手前の確認が締め切りに達する
    out = _run({s: [MISSING] for s in "ABC"}, list("ABC"), deadline=14.0)

    assert set(out.api.calls) == {"A", "B", "C"}
    assert all(out.api.calls.count(symbol) in (1, 2) for symbol in "ABC")
    assert sum(out.api.calls.count(symbol) == 2 for symbol in "ABC") >= 1
    assert out.records["A"]["retry_attempted"] is True
    assert out.records["B"]["retry_attempted"] is False
    assert out.summary["retry_stopped_by_cutoff"] is True
    assert out.summary["timed_out"] is False
    assert out.saved
    assert out.summary["reason_counts"] == {"FILTER_TURNOVER_MISSING": 3}


def test_round_is_not_started_when_wait_would_pass_the_cutoff():
    # 1ラウンド目(待機10秒+3件)後に時計=16。2ラウンド目は16+10>16なので開始しない
    out = _run({s: [MISSING] for s in "ABC"}, list("ABC"), deadline=16.0)

    assert out.summary["retry_rounds_run"] == 1
    assert out.summary["retry_stopped_by_cutoff"] is True
    assert out.sleeps == [10.0]
    assert out.saved


def test_retry_is_not_started_when_first_wait_passes_the_cutoff():
    out = _run({"A": [MISSING]}, ["A"], deadline=5.0)
    assert out.summary["retry_rounds_run"] == 0
    assert out.sleeps == []
    assert out.api.calls == ["A"]
    assert out.saved


def test_current_price_missing_board_none_and_exceptions_are_not_retried():
    scripts = {
        "P": [_board(volume=10, price=None)],
        "N": [None],
        "E": [RuntimeError("boom")],
    }
    out = _run(scripts, ["P", "N", "E"])

    assert out.api.calls == ["P", "N", "E"]
    assert out.records["P"]["reason_code"] == "FILTER_CURRENT_PRICE_MISSING"
    assert out.records["N"]["reason_code"] == "FILTER_BOARD_FETCH_FAILED"
    assert out.records["E"]["reason_code"] == "FILTER_BOARD_FETCH_FAILED"
    assert "retry_attempted" not in out.records["P"]
    assert out.summary["retry_attempted_count"] == 0
    assert out.sleeps == []


def test_disabled_retry_matches_previous_behavior():
    scripts = {"A": [MISSING, _board(value=1000.0, volume=1)], "B": [_board(value=500.0, volume=5)]}
    out = _run(scripts, ["A", "B"], retry=False)

    assert out.api.calls == ["A", "B"]
    assert out.sleeps == []
    assert out.records["A"]["reason_code"] == "FILTER_TURNOVER_MISSING"
    assert not any(key.startswith("retry_") for key in out.records["A"])
    assert not any(key.startswith("retry_") for key in out.summary)
    assert out.summary["reason_counts"] == {"FILTER_TURNOVER_MISSING": 1}
    assert out.result.symbols == ["B"]


def test_recovered_board_is_reused_from_cache_by_other_price_bands():
    out = _run({"A": [MISSING, _board(value=1000.0, volume=1)]}, ["A"])
    calls_before = list(out.api.calls)
    other_results, other_diag = _ResultRepo(), []

    band = FilteringUseCase(
        _ScreeningRepo(["A"]), out.cache, _VolumeStub(), other_results,
        diagnostics_repository=SimpleNamespace(save=other_diag.append),
    )
    result = band.execute(target_date=date(2026, 10, 5), price_cap=450.0, price_band="450")

    assert out.api.calls == calls_before
    assert result.symbols == ["A"]
    assert other_diag[0]["candidates"][0]["numerator_source"] == "TradingValue"


def test_retry_counts_toward_registration_clear_and_records_it():
    clears = []
    api = _FakeApi({s: [MISSING, _board(value=1.0, volume=1)] for s in ("A", "B", "C")})
    cache = RegistrationAwareBoardCache(api, lambda: clears.append(1) or {}, batch_size=3)
    for symbol in "ABC":
        cache.get_current_board(symbol)
    assert cache.requests_since_clear == 3

    cache.refetch_current_board("A")

    assert clears == [1]
    assert cache.get_retry_meta("A")["retry_preceded_by_registration_clear"] is True
    assert cache.requests_since_clear == 1
    cache.refetch_current_board("B")
    assert cache.get_retry_meta("B")["retry_preceded_by_registration_clear"] is False
    assert cache.requests_since_clear == 2


@pytest.mark.parametrize("raw", ["-1", "abc"])
def test_retry_settings_reject_invalid_values(raw, monkeypatch):
    monkeypatch.setenv("FILTER_BOARD_RETRY_WAIT_SECONDS", raw)
    with pytest.raises(ValueError):
        config._non_negative_env("FILTER_BOARD_RETRY_WAIT_SECONDS", "10", float)


def test_retry_setting_defaults():
    assert config.FILTER_BOARD_MAX_CONCURRENCY >= 1
    assert config.FILTER_BOARD_RETRY_MAX_ROUNDS >= 0
    assert config.FILTER_BOARD_RETRY_WAIT_SECONDS >= 0
    assert config.FILTER_BOARD_RETRY_MARGIN_SECONDS >= 0
    assert config.FILTER_BOARD_429_MAX_RETRIES >= 0
    assert config.FILTER_BOARD_429_RETRY_WAIT_SECONDS >= 0


def test_timeout_warning_reports_evaluated_partial_and_unprocessed_counts(monkeypatch, caplog):
    times = iter((0.0, 0.6, 0.6))
    monkeypatch.setattr(filtering_usecase_module, "monotonic", lambda: next(times))
    diagnostics = []
    usecase = FilteringUseCase(
        _ScreeningRepo(["A", "B"]),
        RegistrationAwareBoardCache(
            _FakeApi({"A": [_board(value=1000.0, volume=1)], "B": [_board(value=500.0, volume=1)]}),
            lambda: {},
        ),
        _VolumeStub(),
        _ResultRepo(),
        diagnostics_repository=SimpleNamespace(save=diagnostics.append),
    )

    with pytest.raises(FilteringDeadlineExceeded):
        usecase.execute(target_date=date(2026, 10, 5), deadline_monotonic=0.5)

    summary = diagnostics[0]["summary"]
    assert summary["evaluated_count"] == 0
    assert summary["unprocessed_count"] == 1
    warning = next(
        record.getMessage()
        for record in caplog.records
        if record.getMessage().startswith("価格帯別フィルタを時間切れで打ち切りました:")
    )
    assert "evaluated=0 skipped=0 partial=1 unprocessed=1 time_limit=2" in warning


