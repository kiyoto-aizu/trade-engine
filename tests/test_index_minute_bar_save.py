from datetime import date

import src.entrypoints.run_daily_analysis as daily_entry
from src.api import request_handler
from src.application.index_minute_bar_save_usecase import IndexMinuteBarSaveUseCase
from src.domain.models import MinuteBar
from src.infrastructure.market_data import get_intraday_bars
from src.infrastructure.market_data.get_intraday_bars import IntradayFetchResult
from src.infrastructure.persistence.parquet_minute_bar_repository import ParquetMinuteBarRepository


def _bar(time, price=100.0):
    return MinuteBar(time=time, price=price, cumulative_volume=None, volume=0.0, source="yahoo")


def test_fetch_index_uses_period_params_and_parses(monkeypatch):
    captured = {}

    def fake_get(url, params=None, headers=None, timeout=10):
        captured.update(url=url, params=params)
        return {"chart": {"result": [{
            "timestamp": [1791590400, 1791590460],
            "indicators": {"quote": [{"close": [39000.5, None], "volume": [0, 0]}]},
        }]}}

    monkeypatch.setattr(request_handler, "send_get", fake_get)

    result = get_intraday_bars.fetch_yahoo_index_intraday_bars("^N225", date(2026, 10, 5), date(2026, 10, 9))

    assert result.ok
    assert "%5EN225" in captured["url"]
    assert captured["params"]["interval"] == "1m"
    assert captured["params"]["period2"] - captured["params"]["period1"] == 5 * 86400
    assert [bar.price for bar in result.bars] == [39000.5]
    assert result.bars[0].time.startswith("2026-10-")


def test_fetch_index_reports_no_response(monkeypatch):
    monkeypatch.setattr(request_handler, "send_get", lambda *a, **k: None)

    result = get_intraday_bars.fetch_yahoo_index_intraday_bars("^N225", date(2026, 10, 9), date(2026, 10, 9))

    assert not result.ok
    assert result.error == "NO_RESPONSE"


def test_usecase_saves_by_date_chunks_and_upserts(tmp_path):
    calls = []

    def fetch(symbol, start, end):
        calls.append((start, end))
        return IntradayFetchResult(bars=[
            _bar(f"{start.isoformat()}T09:00:00", 1.0),
            _bar(f"{start.isoformat()}T09:01:00", 2.0),
            _bar("2020-01-01T09:00:00", 9.0),
        ])

    repository = ParquetMinuteBarRepository(tmp_path)
    use_case = IndexMinuteBarSaveUseCase(fetch, repository)

    result = use_case.run(date(2026, 10, 1), date(2026, 10, 9))
    use_case.run(date(2026, 10, 1), date(2026, 10, 1))

    assert calls[:2] == [(date(2026, 10, 1), date(2026, 10, 7)), (date(2026, 10, 8), date(2026, 10, 9))]
    assert result.saved_counts == {date(2026, 10, 1): 2, date(2026, 10, 8): 2}
    assert result.failures == []
    assert len(repository.load_bars(date(2026, 10, 1), "^N225")) == 2


def test_usecase_records_failure_reason_without_raising(tmp_path):
    def failing(symbol, start, end):
        return IntradayFetchResult(ok=False, error="NO_RESPONSE")

    def raising(symbol, start, end):
        raise RuntimeError("boom")

    repository = ParquetMinuteBarRepository(tmp_path)
    first = IndexMinuteBarSaveUseCase(failing, repository).run(date(2026, 10, 9), date(2026, 10, 9))
    second = IndexMinuteBarSaveUseCase(raising, repository).run(date(2026, 10, 9), date(2026, 10, 9))

    assert first.failures == ["2026-10-09〜2026-10-09: NO_RESPONSE"]
    assert second.failures == ["2026-10-09〜2026-10-09: EXCEPTION:RuntimeError"]
    assert first.saved_counts == {}


def test_daily_analysis_main_saves_index_bars_and_survives_failures(monkeypatch):
    saved = []
    monkeypatch.setattr(daily_entry, "configure_logging", lambda: None)
    monkeypatch.setattr(daily_entry, "run_daily_analysis", lambda target: True)
    monkeypatch.setattr(daily_entry, "save_index_minute_bars", lambda start, end: saved.append((start, end)))

    assert daily_entry.main(["--date", "2026-10-09"]) == 0
    assert saved == [(date(2026, 10, 9), date(2026, 10, 9))]
