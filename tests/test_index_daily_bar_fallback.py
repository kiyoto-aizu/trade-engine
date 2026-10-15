from datetime import date

from src.domain.models import MinuteBar
from src.infrastructure.market_data.get_intraday_bars import IntradayFetchResult
from src.infrastructure.market_data.index_daily_bar_fallback import (
    MinuteBarDailyFallback,
    build_daily_bar,
)

TARGET = date(2026, 10, 14)


def _minute_bars(count=333):
    bars = []
    for index in range(count):
        total = 9 * 60 + index + (59 if index >= 151 else 0)
        bars.append(MinuteBar(
            time=f"2026-10-14T{total // 60:02d}:{total % 60:02d}:00",
            price=100.0 + index,
            cumulative_volume=None,
            volume=0.0,
            source="yahoo",
        ))
    return bars


class FakeRepository:
    def __init__(self, bars):
        self.bars = bars

    def load_bars(self, target_date, symbol):
        return self.bars


def test_build_daily_bar_aggregates_minute_prices():
    bar = build_daily_bar(TARGET, _minute_bars())

    assert (bar.open, bar.high, bar.low, bar.close) == (100.0, 432.0, 100.0, 432.0)
    assert bar.date == TARGET


def test_build_daily_bar_rejects_partial_day():
    assert build_daily_bar(TARGET, _minute_bars(count=200)) is None


def test_fallback_prefers_saved_minute_bars_over_yahoo():
    def fail_fetch(symbol, start, end):
        raise AssertionError("Yahooは呼ばれない想定")

    bar = MinuteBarDailyFallback(FakeRepository(_minute_bars()), fail_fetch)("^N225", TARGET)

    assert bar.close == 432.0


def test_fallback_uses_yahoo_when_nothing_is_saved():
    fetched = IntradayFetchResult(bars=_minute_bars())

    bar = MinuteBarDailyFallback(FakeRepository([]), lambda *_: fetched)("^N225", TARGET)

    assert bar.close == 432.0


def test_fallback_returns_none_when_yahoo_fails():
    failed = IntradayFetchResult(ok=False, error="NO_RESPONSE")

    assert MinuteBarDailyFallback(FakeRepository([]), lambda *_: failed)("^N225", TARGET) is None
