import json
from datetime import date

from src.domain.volatility import DailyBar
from src.infrastructure.market_data import yahoo_backtest_history_client
from src.infrastructure.market_data import yahoo_daily_bar_cache
from src.infrastructure.market_data.yahoo_backtest_history_client import calculate_required_fetch_days


def test_calculate_required_fetch_days_anchors_range_to_earliest_simulation_date():
    assert calculate_required_fetch_days(
        date(2026, 9, 7),
        today=date(2026, 9, 28),
    ) == 81
    assert calculate_required_fetch_days(
        date(2026, 10, 1),
        today=date(2026, 9, 28),
    ) == 60


def test_cached_fetch_saves_ohlc_and_reuses_sufficient_cache(tmp_path, monkeypatch):
    calls = []

    def fake_fetch(symbols, days):
        calls.append((symbols, days))
        return {
            symbol: {
                "2026-09-24": DailyBar(open=99.0, high=102.0, low=98.0, close=101.0),
                "2026-09-25": DailyBar(open=101.0, high=103.0, low=100.0, close=102.0),
            }
            for symbol in symbols
        }

    monkeypatch.setattr(yahoo_daily_bar_cache, "fetch_yahoo_dated_ohlc", fake_fetch)
    first = yahoo_daily_bar_cache.fetch_yahoo_dated_ohlc_cached(
        ["3133", "^N225"],
        days=81,
        cache_dir=tmp_path,
        earliest_needed_date=date(2026, 9, 24),
        latest_needed_date=date(2026, 9, 25),
    )

    assert calls == [(["3133", "^N225"], 81)]
    assert first["^N225"]["2026-09-25"].open == 101.0
    saved = json.loads((tmp_path / "^N225.json").read_text(encoding="utf-8"))
    assert saved["2026-09-25"] == {
        "open": 101.0,
        "high": 103.0,
        "low": 100.0,
        "close": 102.0,
        "volume": None,
    }

    second = yahoo_daily_bar_cache.fetch_yahoo_dated_ohlc_cached(
        ["3133", "^N225"],
        days=81,
        cache_dir=tmp_path,
        earliest_needed_date=date(2026, 9, 24),
        latest_needed_date=date(2026, 9, 25),
    )
    assert calls == [(["3133", "^N225"], 81)]
    assert second["3133"]["2026-09-24"].close == 101.0


def test_cached_fetch_refreshes_when_latest_required_date_is_not_cached(tmp_path, monkeypatch):
    yahoo_daily_bar_cache._save_cache(
        tmp_path,
        "3133",
        {"2026-09-24": DailyBar(open=99.0, high=102.0, low=98.0, close=101.0)},
    )
    calls = []

    def fake_fetch(symbols, days):
        calls.append((symbols, days))
        return {
            "3133": {
                "2026-09-25": DailyBar(open=101.0, high=103.0, low=100.0, close=102.0),
            }
        }

    monkeypatch.setattr(yahoo_daily_bar_cache, "fetch_yahoo_dated_ohlc", fake_fetch)
    history = yahoo_daily_bar_cache.fetch_yahoo_dated_ohlc_cached(
        ["3133"],
        days=81,
        cache_dir=tmp_path,
        earliest_needed_date=date(2026, 9, 24),
        latest_needed_date=date(2026, 9, 25),
    )

    assert calls == [(["3133"], 81)]
    assert sorted(history["3133"]) == ["2026-09-24", "2026-09-25"]


def test_dated_ohlc_fetcher_preserves_open_for_index_market_regime(monkeypatch):
    timestamp = 1790294400

    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {
                "chart": {
                    "result": [{
                        "timestamp": [timestamp],
                        "indicators": {"quote": [{
                            "open": [100.0],
                            "high": [110.0],
                            "low": [95.0],
                            "close": [105.0],
                        }]},
                    }]
                }
            }

    monkeypatch.setattr(yahoo_backtest_history_client.requests, "get", lambda *args, **kwargs: Response())
    history = yahoo_backtest_history_client.fetch_yahoo_dated_ohlc(["^N225"], days=81)

    assert list(history["^N225"].values())[0].open == 100.0


def test_index_cache_without_open_is_refetched(tmp_path, monkeypatch):
    yahoo_daily_bar_cache._save_cache(
        tmp_path,
        "^N225",
        {"2026-09-25": DailyBar(high=110.0, low=95.0, close=105.0)},
    )
    calls = []

    def fake_fetch(symbols, days):
        calls.append(symbols)
        return {
            "^N225": {
                "2026-09-25": DailyBar(open=100.0, high=110.0, low=95.0, close=105.0),
            }
        }

    monkeypatch.setattr(yahoo_daily_bar_cache, "fetch_yahoo_dated_ohlc", fake_fetch)
    history = yahoo_daily_bar_cache.fetch_yahoo_dated_ohlc_cached(
        ["^N225"],
        days=81,
        cache_dir=tmp_path,
        earliest_needed_date=date(2026, 9, 25),
        latest_needed_date=date(2026, 9, 25),
    )

    assert calls == [["^N225"]]
    assert history["^N225"]["2026-09-25"].open == 100.0


def test_cache_update_excludes_newer_bars_and_refreshes_latest_date(tmp_path, monkeypatch):
    yahoo_daily_bar_cache._save_cache(
        tmp_path,
        "^VIX",
        {
            "2026-11-02": DailyBar(open=18.0, high=20.0, low=17.0, close=19.0, volume=0.0),
            "2026-11-03": DailyBar(open=19.0, high=22.0, low=18.0, close=20.0, volume=0.0),
            "2026-11-04": DailyBar(open=20.0, high=22.0, low=19.0, close=21.0, volume=0.0),
        },
    )
    fetched_latest = DailyBar(open=19.0, high=21.0, low=18.0, close=20.5, volume=0.0)
    calls = []

    def fake_fetch(symbols, days):
        calls.append(symbols)
        return {
            "^VIX": {
                "2026-11-03": fetched_latest,
                "2026-11-04": DailyBar(open=20.0, high=23.0, low=19.0, close=22.0, volume=0.0),
            }
        }

    monkeypatch.setattr(yahoo_daily_bar_cache, "fetch_yahoo_dated_ohlc", fake_fetch)
    history = yahoo_daily_bar_cache.fetch_yahoo_dated_ohlc_cached(
        ["^VIX"],
        days=120,
        cache_dir=tmp_path,
        earliest_needed_date=date(2026, 7, 7),
        latest_needed_date=date(2026, 11, 3),
        latest_allowed_date=date(2026, 11, 3),
        refresh_latest_date=True,
    )

    assert calls == [["^VIX"]]
    assert max(history["^VIX"]) == "2026-11-03"
    assert history["^VIX"]["2026-11-03"].close == 20.5
    saved = json.loads((tmp_path / "^VIX.json").read_text(encoding="utf-8"))
    assert max(saved) == "2026-11-03"
    assert saved["2026-11-03"]["close"] == 20.5


def test_forced_latest_refresh_does_not_accept_cached_bar_on_fetch_failure(tmp_path, monkeypatch):
    yahoo_daily_bar_cache._save_cache(
        tmp_path,
        "^VIX",
        {
            "2026-11-02": DailyBar(open=18.0, high=20.0, low=17.0, close=19.0, volume=0.0),
            "2026-11-03": DailyBar(open=19.0, high=22.0, low=18.0, close=20.0, volume=0.0),
        },
    )
    monkeypatch.setattr(
        yahoo_daily_bar_cache,
        "fetch_yahoo_dated_ohlc",
        lambda symbols, days: {},
    )

    history = yahoo_daily_bar_cache.fetch_yahoo_dated_ohlc_cached(
        ["^VIX"],
        days=120,
        cache_dir=tmp_path,
        earliest_needed_date=date(2026, 7, 7),
        latest_needed_date=date(2026, 11, 3),
        latest_allowed_date=date(2026, 11, 3),
        refresh_latest_date=True,
    )

    assert max(history["^VIX"]) == "2026-11-02"
    saved = json.loads((tmp_path / "^VIX.json").read_text(encoding="utf-8"))
    assert "2026-11-03" not in saved


def test_cache_reuse_persists_removal_of_bars_after_allowed_date(tmp_path, monkeypatch):
    yahoo_daily_bar_cache._save_cache(
        tmp_path,
        "3133",
        {
            "2026-11-02": DailyBar(open=18.0, high=20.0, low=17.0, close=19.0, volume=100.0),
            "2026-11-03": DailyBar(open=19.0, high=22.0, low=18.0, close=20.0, volume=100.0),
            "2026-11-04": DailyBar(open=20.0, high=22.0, low=19.0, close=21.0, volume=100.0),
        },
    )
    calls = []

    def fake_fetch(symbols, days):
        calls.append(symbols)
        return {}

    monkeypatch.setattr(
        yahoo_daily_bar_cache,
        "fetch_yahoo_dated_ohlc",
        fake_fetch,
    )

    history = yahoo_daily_bar_cache.fetch_yahoo_dated_ohlc_cached(
        ["3133"],
        days=120,
        cache_dir=tmp_path,
        earliest_needed_date=date(2026, 11, 2),
        latest_needed_date=date(2026, 11, 3),
        latest_allowed_date=date(2026, 11, 3),
    )

    assert max(history["3133"]) == "2026-11-03"
    assert calls == []
    saved = json.loads((tmp_path / "3133.json").read_text(encoding="utf-8"))
    assert max(saved) == "2026-11-03"


def _write_raw(tmp_path, symbol, payload):
    (tmp_path / f"{symbol}.json").write_text(json.dumps(payload), encoding="utf-8")


def _fake_fetch_factory(calls, volume):
    def fake_fetch(symbols, days):
        calls.append(list(symbols))
        return {
            symbol: {"2026-09-25": DailyBar(open=1.0, high=3.0, low=1.0, close=2.0, volume=volume)}
            for symbol in symbols
        }
    return fake_fetch


def _fetch(tmp_path, symbols):
    return yahoo_daily_bar_cache.fetch_yahoo_dated_ohlc_cached(
        symbols, days=81, cache_dir=tmp_path,
        earliest_needed_date=date(2026, 9, 25), latest_needed_date=date(2026, 9, 25),
    )


def test_daily_bar_volume_is_optional_for_backward_compatibility():
    bar = DailyBar(high=2.0, low=1.0, close=1.5)
    assert bar.volume is None
    assert DailyBar(2.0, 1.0, 1.5, 1.2).open == 1.2


def test_legacy_cache_without_volume_key_is_refetched_and_volume_saved(tmp_path, monkeypatch):
    _write_raw(tmp_path, "3133", {"2026-09-25": {"open": 1.0, "high": 3.0, "low": 1.0, "close": 2.0}})
    calls = []
    monkeypatch.setattr(yahoo_daily_bar_cache, "fetch_yahoo_dated_ohlc", _fake_fetch_factory(calls, 1500.0))

    history = _fetch(tmp_path, ["3133"])

    assert calls == [["3133"]]
    assert history["3133"]["2026-09-25"].volume == 1500.0
    saved = json.loads((tmp_path / "3133.json").read_text(encoding="utf-8"))
    assert saved["2026-09-25"]["volume"] == 1500.0
    _fetch(tmp_path, ["3133"])
    assert calls == [["3133"]]


def test_volume_round_trips_through_cache(tmp_path):
    yahoo_daily_bar_cache._save_cache(
        tmp_path, "3133", {"2026-09-25": DailyBar(high=3.0, low=1.0, close=2.0, open=1.0, volume=1234.0)}
    )
    assert yahoo_daily_bar_cache._load_cache(tmp_path, "3133")["2026-09-25"].volume == 1234.0


def test_index_with_null_or_zero_volume_key_is_not_refetched(tmp_path, monkeypatch):
    _write_raw(tmp_path, "^N225", {"2026-09-25": {"open": 1.0, "high": 3.0, "low": 1.0, "close": 2.0, "volume": 0}})
    _write_raw(tmp_path, "^VIX", {"2026-09-25": {"open": 1.0, "high": 3.0, "low": 1.0, "close": 2.0, "volume": None}})
    calls = []
    monkeypatch.setattr(yahoo_daily_bar_cache, "fetch_yahoo_dated_ohlc", _fake_fetch_factory(calls, 9.0))

    history = _fetch(tmp_path, ["^N225", "^VIX"])

    assert calls == []
    assert history["^N225"]["2026-09-25"].volume == 0.0
    assert history["^VIX"]["2026-09-25"].volume is None


def test_legacy_index_cache_without_volume_key_is_refetched(tmp_path, monkeypatch):
    _write_raw(tmp_path, "^N225", {"2026-09-25": {"open": 1.0, "high": 3.0, "low": 1.0, "close": 2.0}})
    calls = []
    monkeypatch.setattr(yahoo_daily_bar_cache, "fetch_yahoo_dated_ohlc", _fake_fetch_factory(calls, 0.0))

    _fetch(tmp_path, ["^N225"])

    assert calls == [["^N225"]]


def test_dated_ohlc_fetcher_returns_volume(monkeypatch):
    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {"chart": {"result": [{
                "timestamp": [1790294400, 1790380800],
                "indicators": {"quote": [{
                    "open": [1.0, 1.0], "high": [2.0, 2.0], "low": [1.0, 1.0], "close": [2.0, 2.0],
                    "volume": [500, None],
                }]},
            }]}}

    monkeypatch.setattr(yahoo_backtest_history_client.requests, "get", lambda *a, **k: Response())
    bars = list(yahoo_backtest_history_client.fetch_yahoo_dated_ohlc(["3133"], days=81)["3133"].values())
    assert [bar.volume for bar in bars] == [500.0, None]
