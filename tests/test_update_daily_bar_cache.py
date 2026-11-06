from datetime import date, datetime

from src.entrypoints import update_daily_bar_cache as cli


def test_latest_confirmed_trading_day_excludes_unfinished_today():
    assert cli.latest_confirmed_trading_day(datetime(2026, 10, 2, 10, 0)) == datetime(2026, 10, 1).date()
    assert cli.latest_confirmed_trading_day(datetime(2026, 10, 2, 16, 0)) == datetime(2026, 10, 2).date()
    assert cli.latest_confirmed_trading_day(datetime(2026, 10, 3, 12, 0)) == datetime(2026, 10, 2).date()


def test_update_cache_passes_latest_and_returns_latest_dates(tmp_path, monkeypatch):
    captured = {}

    def fake_cached(symbols, days, cache_dir, earliest_needed_date, latest_needed_date):
        captured.update(symbols=symbols, latest=latest_needed_date)
        return {"3133": {"2026-10-01": object(), "2026-09-30": object()}}

    monkeypatch.setattr(cli, "fetch_yahoo_dated_ohlc_cached", fake_cached)
    result = cli.update_cache(tmp_path, ["3133"], 120, datetime(2026, 10, 2, 10, 0))

    assert captured["latest"] == datetime(2026, 10, 1).date()
    assert result == {"3133": "2026-10-01"}


def test_update_cache_uses_us_trading_day_for_vix(tmp_path, monkeypatch):
    calls = []

    def fake_cached(symbols, days, cache_dir, earliest_needed_date, latest_needed_date):
        calls.append((symbols, latest_needed_date))
        return {symbol: {"2026-11-03": object()} for symbol in symbols}

    monkeypatch.setattr(cli, "fetch_yahoo_dated_ohlc_cached", fake_cached)
    cli.update_cache(
        tmp_path,
        ["AAA", "^N225", "^VIX"],
        120,
        datetime(2026, 11, 4, 16, 32),
    )

    assert calls == [
        (["AAA", "^N225"], date(2026, 11, 4)),
        (["^VIX"], date(2026, 11, 3)),
    ]
