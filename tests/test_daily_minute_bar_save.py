from datetime import date, datetime

import pytest

from src.application.daily_minute_bar_save_usecase import (
    DailyMinuteBarSaveUseCase,
    FEW_BARS,
    SAVED,
    ZERO,
)
from src.domain.models import MinuteBar
from src.entrypoints import run_daily_analysis
from src.entrypoints.save_daily_minute_bars import collect_daily_symbols, save_daily_minute_bars
from src.infrastructure.market_data.get_intraday_bars import IntradayFetchResult
from src.infrastructure.persistence.parquet_minute_bar_repository import ParquetMinuteBarRepository

DAY = date(2026, 10, 13)
AFTER_CLOSE = datetime(2026, 10, 13, 16, 30)


def _bars(day="2026-10-13", count=3, price=100.0, source="yahoo"):
    return [
        MinuteBar(time=f"{day}T09:{minute:02d}:00", price=price + minute, cumulative_volume=None, volume=1.0, source=source)
        for minute in range(count)
    ]


def _write_filtering(tmp_path, symbols, name="filtering"):
    directory = tmp_path / name
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "2026-10-13.json").write_text(
        '{"date": "2026-10-13", "symbols": %s}' % str(symbols).replace("'", '"'), encoding="utf-8"
    )
    return directory


def _save(tmp_path, fetch, **kwargs):
    return save_daily_minute_bars(
        DAY,
        now=kwargs.pop("now", AFTER_CLOSE),
        filtering_dir=_write_filtering(tmp_path, ["1111", "2222"]),
        output_dir=tmp_path / "out",
        include_price_bands=False,
        fetch_bars=fetch,
        interval_seconds=0,
        retries=kwargs.pop("retries", 1),
        retry_wait_seconds=0,
        sleeper=lambda _: None,
        **kwargs,
    )


def test_saves_each_symbol_and_records_counts(tmp_path):
    result = _save(tmp_path, lambda symbol, days: IntradayFetchResult(bars=_bars(count=5)))

    assert [item.status for item in result.symbols] == [SAVED, SAVED]
    assert result.symbols[0].bar_count == 5
    assert result.symbols[0].first_time == "2026-10-13T09:00:00"
    repository = ParquetMinuteBarRepository(tmp_path / "out")
    assert len(repository.load_bars(DAY, "1111")) == 5
    assert result.summary_line() == "分足 2銘柄すべて保存"


def test_full_day_is_saved_without_few_bars_flag(tmp_path):
    full = [
        MinuteBar(time=f"2026-10-13T{9 + i // 60:02d}:{i % 60:02d}:00", price=1.0, cumulative_volume=None, volume=1.0, source="yahoo")
        for i in range(330)
    ]
    result = _save(tmp_path, lambda symbol, days: IntradayFetchResult(bars=full))

    assert [item.status for item in result.symbols] == [SAVED, SAVED]


def test_only_target_date_is_saved(tmp_path):
    mixed = _bars("2026-10-09", 2) + _bars("2026-10-13", 3)
    _save(tmp_path, lambda symbol, days: IntradayFetchResult(bars=mixed))

    repository = ParquetMinuteBarRepository(tmp_path / "out")
    assert len(repository.load_bars(DAY, "1111")) == 3
    assert repository.load_bars(date(2026, 10, 9), "1111") == []


def test_rerun_is_idempotent_and_yahoo_replaces_same_time_but_keeps_poll_only_times(tmp_path):
    repository = ParquetMinuteBarRepository(tmp_path / "out")
    repository.replace_bars(DAY, "1111", [
        MinuteBar(time="2026-10-13T09:00:00", price=1.0, cumulative_volume=None, volume=None, source="poll"),
        MinuteBar(time="2026-10-13T09:10:00", price=7.0, cumulative_volume=None, volume=None, source="poll"),
    ])
    fetch = lambda symbol, days: IntradayFetchResult(bars=_bars(count=3, price=100.0))

    _save(tmp_path, fetch)
    first = repository.load_bars(DAY, "1111")
    _save(tmp_path, fetch)
    second = repository.load_bars(DAY, "1111")

    assert first == second
    assert [bar.time[-8:] for bar in second] == ["09:00:00", "09:01:00", "09:02:00", "09:10:00"]
    assert second[0].source == "yahoo" and second[0].price == 100.0
    assert second[-1].source == "poll"


def test_failures_are_recorded_with_reason_and_do_not_raise(tmp_path):
    def fetch(symbol, days):
        if symbol == "1111":
            raise RuntimeError("boom")
        return IntradayFetchResult(ok=False, error="NO_RESPONSE")

    result = _save(tmp_path, fetch)

    assert [item.status for item in result.symbols] == [ZERO, ZERO]
    assert result.symbols[0].reason == "EXCEPTION:RuntimeError"
    assert result.symbols[1].reason == "NO_RESPONSE"
    assert result.summary_line() == "分足 0/2銘柄保存、0本: 1111,2222"


def test_empty_response_is_retried_then_zero(tmp_path):
    calls = []

    def fetch(symbol, days):
        calls.append(symbol)
        return IntradayFetchResult(bars=[])

    result = _save(tmp_path, fetch, retries=2)

    assert calls.count("1111") == 3
    assert result.symbols[0].reason == "EMPTY_RESPONSE"


def test_retry_recovers_after_transient_failure(tmp_path):
    attempts = {"n": 0}

    def fetch(symbol, days):
        attempts["n"] += 1
        if attempts["n"] == 1:
            return IntradayFetchResult(ok=False, error="NO_RESPONSE")
        return IntradayFetchResult(bars=_bars(count=4))

    result = _save(tmp_path, fetch)

    assert result.symbols[0].bar_count == 4


def test_save_error_is_recorded_and_next_symbol_continues(tmp_path, monkeypatch):
    original = ParquetMinuteBarRepository.replace_bars

    def flaky(self, target_date, symbol, bars):
        if symbol == "1111":
            raise PermissionError
        return original(self, target_date, symbol, bars)

    monkeypatch.setattr(ParquetMinuteBarRepository, "replace_bars", flaky)

    result = _save(tmp_path, lambda symbol, days: IntradayFetchResult(bars=_bars()))

    assert result.symbols[0].reason == "SAVE_ERROR:PermissionError"
    assert result.symbols[1].bar_count == 3


@pytest.mark.parametrize(
    "trade_date, now",
    [
        (date(2026, 10, 10), datetime(2026, 10, 10, 16, 30)),  # 土曜
        (date(2026, 10, 12), datetime(2026, 10, 12, 16, 30)),  # スポーツの日
        (DAY, datetime(2026, 10, 13, 14, 0)),  # 引け前
    ],
)
def test_skips_closed_days_and_before_close(tmp_path, trade_date, now):
    calls = []
    result = save_daily_minute_bars(
        trade_date,
        now=now,
        filtering_dir=_write_filtering(tmp_path, ["1111"]),
        output_dir=tmp_path / "out",
        fetch_bars=lambda *args: calls.append(args),
    )

    assert result is None
    assert calls == []
    assert not (tmp_path / "out").exists()


def test_unexpected_exception_returns_none(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "src.entrypoints.save_daily_minute_bars.collect_daily_symbols",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x")),
    )

    assert save_daily_minute_bars(DAY, now=AFTER_CLOSE) is None


def test_collect_symbols_price_bands_are_optional(tmp_path, monkeypatch):
    from src.config import config

    normal = _write_filtering(tmp_path, ["1111", "2222"])
    root = tmp_path / "bands"
    _write_filtering(root, ["2222", "3333"], name="450")
    monkeypatch.setattr(config, "FILTERING_PRICE_BAND_RESULT_ROOT", root)
    monkeypatch.setattr(config, "SCREENING_ALTERNATE_PRICE_CAPS", (450.0, 900.0))

    assert collect_daily_symbols(DAY, filtering_dir=normal, include_price_bands=False) == ["1111", "2222"]
    assert collect_daily_symbols(DAY, filtering_dir=normal, include_price_bands=True) == ["1111", "2222", "3333"]


def test_usecase_summary_marks_zero_and_few(tmp_path):
    repository = ParquetMinuteBarRepository(tmp_path)
    for symbol in ("1", "2"):
        for day in (date(2026, 10, 8), date(2026, 10, 9), date(2026, 10, 12)):
            repository.replace_bars(day, symbol, _bars(day.isoformat(), count=10))
    results = {"1": IntradayFetchResult(bars=_bars(count=2)), "2": IntradayFetchResult(bars=[])}
    use_case = DailyMinuteBarSaveUseCase(
        lambda symbol, days: results[symbol], repository, interval_seconds=0, retries=0, sleeper=lambda _: None
    )

    result = use_case.run(DAY, ["1", "2"])
    assert result.summary_line() == "分足 1/2銘柄保存、0本: 2、本数少: 1"
    assert result.symbols[0].historical_median == 10


def test_few_bars_is_not_judged_without_enough_history(tmp_path):
    repository = ParquetMinuteBarRepository(tmp_path)
    repository.replace_bars(date(2026, 10, 12), "1", _bars("2026-10-12", count=10))
    use_case = DailyMinuteBarSaveUseCase(
        lambda symbol, days: IntradayFetchResult(bars=_bars(count=2)),
        repository,
        interval_seconds=0,
        retries=0,
        sleeper=lambda _: None,
    )

    result = use_case.run(DAY, ["1"])

    assert result.symbols[0].status == SAVED
    assert result.symbols[0].historical_median is None


def test_summary_lists_only_anomalous_symbols(tmp_path):
    repository = ParquetMinuteBarRepository(tmp_path)
    use_case = DailyMinuteBarSaveUseCase(
        lambda symbol, days: IntradayFetchResult(bars=_bars(count=5)),
        repository,
        interval_seconds=0,
        retries=0,
        sleeper=lambda _: None,
    )

    result = use_case.run(DAY, ["1234", "5678"])

    assert result.summary_line() == "分足 2銘柄すべて保存"


def test_daily_analysis_continues_and_notifies_when_minute_save_fails(monkeypatch, tmp_path):
    from tests.test_daily_analysis import _paths

    paths = _paths(tmp_path)
    notifications = []
    monkeypatch.setattr(run_daily_analysis, "refresh_cache_with_retries", lambda *a, **k: (True, []))
    monkeypatch.setattr(run_daily_analysis, "run_daily_safely", lambda *a, **k: True)
    monkeypatch.setattr(run_daily_analysis, "_load_daily_report", lambda *a: None)
    monkeypatch.setattr(run_daily_analysis, "create_daily_analyzer", lambda: None)
    monkeypatch.setattr(run_daily_analysis, "notify_analysis", notifications.append)
    monkeypatch.setattr(run_daily_analysis, "run_price_band_checks", lambda *a, **k: None)
    monkeypatch.setattr(run_daily_analysis, "load_trend_check", lambda *a, **k: None)
    monkeypatch.setattr(run_daily_analysis, "resolve_no_trade_reason", lambda *a: None)
    monkeypatch.setattr(run_daily_analysis, "TrendCheckRepository", lambda *_: None)
    _write_filtering(tmp_path, ["1111"])

    def fetch_fails(symbol, days):
        raise RuntimeError("yahoo down")

    monkeypatch.setattr(
        run_daily_analysis,
        "save_daily_minute_bars",
        lambda trade_date, **kw: save_daily_minute_bars(
            trade_date, fetch_bars=fetch_fails, interval_seconds=0, retries=0, sleeper=lambda _: None, **kw
        ),
    )

    success = run_daily_analysis.run_daily_analysis(
        DAY, paths=paths, database_file=tmp_path / "t.sqlite3", now=AFTER_CLOSE, sleeper=lambda _: None
    )

    assert success is True
    assert len(notifications) == 1
    assert "・分足: 0/1銘柄保存、0本: 1111" in notifications[0]
