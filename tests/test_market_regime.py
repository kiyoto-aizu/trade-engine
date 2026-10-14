from datetime import date, timedelta

import pytest

from src.application.market_regime_usecase import MarketRegimeUseCase
from src.domain.market_regime import (
    MarketRegime,
    MarketRegimeThresholds,
    apply_trend_relief,
    calculate_market_regime,
    calculate_market_regime_series,
    calculate_market_regime_series_with_details,
    classify_realized_volatility,
    classify_vix,
    resolve_rsi_entry_threshold,
)
from src.domain.market_volatility import DatedClose, MarketDailyBar
from src.infrastructure.calendar.japanese_calendar import is_trading_day


THRESHOLDS = MarketRegimeThresholds()


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (16.99, MarketRegime.NORMAL),
        (17.0, MarketRegime.CAUTION),
        (28.99, MarketRegime.CAUTION),
        (29.0, MarketRegime.DANGER),
    ],
)
def test_classify_realized_volatility_uses_inclusive_boundaries(value, expected):
    assert classify_realized_volatility(value, THRESHOLDS) == expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (16.99, MarketRegime.NORMAL),
        (17.0, MarketRegime.CAUTION),
        (26.99, MarketRegime.CAUTION),
        (27.0, MarketRegime.DANGER),
    ],
)
def test_classify_vix_uses_inclusive_boundaries(value, expected):
    assert classify_vix(value, THRESHOLDS) == expected


def test_market_regime_uses_the_more_severe_level():
    assert calculate_market_regime(30.0, 30.0, 0.5, THRESHOLDS) == MarketRegime.DANGER
    assert calculate_market_regime(30.0, 12.0, 0.5, THRESHOLDS) == MarketRegime.DANGER
    assert calculate_market_regime(18.0, 10.0, 0.5, THRESHOLDS) == MarketRegime.CAUTION
    assert calculate_market_regime(10.0, 28.0, 0.5, THRESHOLDS) == MarketRegime.DANGER


@pytest.mark.parametrize(
    ("base_regime_values", "expected"),
    [
        ((10.0, 10.0), MarketRegime.CAUTION),
        ((18.0, 10.0), MarketRegime.DANGER),
        ((30.0, 10.0), MarketRegime.DANGER),
    ],
)
def test_nikkei_change_upgrades_by_one_level(base_regime_values, expected):
    assert calculate_market_regime(*base_regime_values, -2.0, THRESHOLDS) == expected


def test_positive_nikkei_change_also_upgrades_by_one_level():
    assert calculate_market_regime(10.0, 10.0, 2.0, THRESHOLDS) == MarketRegime.CAUTION


def test_change_below_upgrade_threshold_does_not_upgrade():
    assert calculate_market_regime(10.0, 10.0, 1.99, THRESHOLDS) == MarketRegime.NORMAL


def test_missing_classification_input_is_safe_side_danger():
    assert classify_realized_volatility(None, THRESHOLDS) == MarketRegime.DANGER
    assert classify_vix(None, THRESHOLDS) == MarketRegime.DANGER
    assert calculate_market_regime(10.0, None, None, THRESHOLDS) == MarketRegime.DANGER


@pytest.mark.parametrize(
    ("regime", "adx", "expected"),
    [
        (MarketRegime.DANGER, 27.0, MarketRegime.CAUTION),
        (MarketRegime.DANGER, 26.99, MarketRegime.DANGER),
        (MarketRegime.CAUTION, 27.0, MarketRegime.NORMAL),
        (MarketRegime.CAUTION, 20.0, MarketRegime.CAUTION),
        (MarketRegime.NORMAL, 50.0, MarketRegime.NORMAL),
        (MarketRegime.DANGER, None, MarketRegime.DANGER),
    ],
)
def test_apply_trend_relief_uses_one_step_inclusive_threshold(regime, adx, expected):
    assert apply_trend_relief(regime, adx, 27.0) == expected


def test_thresholds_reject_invalid_order():
    with pytest.raises(ValueError):
        MarketRegimeThresholds(realized_vol_caution=30.0, realized_vol_danger=29.0)
    with pytest.raises(ValueError):
        MarketRegimeThresholds(vix_caution=28.0, vix_danger=27.0)


@pytest.mark.parametrize(
    ("regime", "expected"),
    [
        (MarketRegime.NORMAL, 55.0),
        (MarketRegime.CAUTION, 60.0),
        (MarketRegime.DANGER, 60.0),
    ],
)
def test_resolve_rsi_entry_threshold(regime, expected):
    assert resolve_rsi_entry_threshold(regime, 55.0, 60.0) == expected


def test_market_regime_series_uses_latest_vix_without_looking_ahead():
    nikkei = _bars([100.0 + index for index in range(22)])
    vix = [
        MarketDailyBar(date(2026, 1, 20), 10.0, 10.0, 10.0, 10.0),
        MarketDailyBar(date(2026, 1, 25), 30.0, 30.0, 30.0, 30.0),
    ]

    result = calculate_market_regime_series(nikkei, vix, 20, THRESHOLDS)

    assert result[date(2026, 1, 21)] == MarketRegime.NORMAL


def test_market_regime_series_marks_stale_vix_as_danger():
    nikkei = _bars([100.0 + index for index in range(40)])
    vix = [MarketDailyBar(date(2026, 1, 2), 10.0, 10.0, 10.0, 10.0)]

    result = calculate_market_regime_series(nikkei, vix, 20, THRESHOLDS)

    assert result[date(2026, 1, 21)] == MarketRegime.DANGER


def test_market_regime_series_records_adx_relief_without_looking_ahead():
    nikkei = _bars([100.0 + index for index in range(40)])
    vix = _bars([30.0] * 40)

    result = calculate_market_regime_series_with_details(
        nikkei, vix, 20, THRESHOLDS, adx_threshold=27.0
    )

    assert result.trend_relief_dates
    assert all(target_date <= nikkei[-1].date for target_date in result.trend_relief_dates)
    assert all(result.regimes[target_date] == MarketRegime.CAUTION for target_date in result.trend_relief_dates)


class FakeMarketDataClient:
    def __init__(self, nikkei_bars=None, vix_bars=None):
        self.nikkei_bars = nikkei_bars or []
        self.vix_bars = vix_bars or []

    def get_daily_ohlc(self, symbol, range_="3mo"):
        if symbol == "^N225":
            return self.nikkei_bars
        if symbol == "^VIX":
            return self.vix_bars
        raise AssertionError(f"unexpected symbol: {symbol}")


def _bars(closes):
    start = date(2026, 1, 1)
    return [
        MarketDailyBar(start + timedelta(days=index), close, close, close, close)
        for index, close in enumerate(closes)
    ]


def _trading_bars(closes, last_date):
    dates = []
    candidate = last_date
    while len(dates) < len(closes):
        if is_trading_day(candidate):
            dates.append(candidate)
        candidate -= timedelta(days=1)
    dates.reverse()
    return [
        MarketDailyBar(target_date, close, close, close, close)
        for target_date, close in zip(dates, closes)
    ]


def test_previous_trading_day_skips_weekends_and_japanese_holidays():
    from src.application.market_regime_usecase import _previous_trading_day
    from src.domain.us_market_calendar import previous_us_equity_trading_day

    assert _previous_trading_day(date(2026, 10, 14)) == date(2026, 10, 13)
    assert _previous_trading_day(date(2026, 10, 13)) == date(2026, 10, 9)
    assert previous_us_equity_trading_day(date(2026, 10, 14)) == date(2026, 10, 13)
    assert previous_us_equity_trading_day(date(2026, 10, 13)) == date(2026, 10, 12)
    assert previous_us_equity_trading_day(date(2026, 1, 20)) == date(2026, 1, 16)


def test_use_case_uses_explicit_as_of_date_for_expected_nikkei_date(monkeypatch):
    from src.application import market_regime_usecase

    reference_date = date(2026, 10, 14)
    expected_nikkei_date = date(2026, 10, 13)
    observed_dates = []
    monkeypatch.setattr(
        market_regime_usecase,
        "_previous_trading_day",
        lambda target: observed_dates.append(target) or expected_nikkei_date,
    )
    nikkei = _trading_bars(
        [100.0 + index for index in range(29)], expected_nikkei_date
    )
    vix = _bars([20.0])
    monkeypatch.setattr(
        market_regime_usecase,
        "previous_us_equity_trading_day",
        lambda target: vix[-1].date,
    )

    result = MarketRegimeUseCase(
        FakeMarketDataClient(nikkei, vix),
        THRESHOLDS,
        realized_volatility_window=20,
        as_of_date=reference_date,
    ).execute()

    assert observed_dates == [reference_date]
    assert result.data_available is True


def test_use_case_returns_latest_market_regime_values(monkeypatch):
    nikkei = _trading_bars(
        [100.0 + index for index in range(27)] + [120.0, 121.0],
        date(2026, 1, 22),
    )
    vix = _bars([20.0])
    monkeypatch.setattr(
        "src.application.market_regime_usecase.previous_us_equity_trading_day",
        lambda today: vix[-1].date,
    )
    monkeypatch.setattr(
        "src.application.market_regime_usecase._previous_trading_day",
        lambda today: nikkei[-1].date,
    )
    result = MarketRegimeUseCase(
        FakeMarketDataClient(nikkei, vix),
        THRESHOLDS,
        realized_volatility_window=20,
        adx_threshold=100.0,
    ).execute()

    assert result.data_available is True
    assert result.regime == MarketRegime.CAUTION
    assert result.vix == 20.0
    assert result.nikkei_change_percent == pytest.approx(
        (nikkei[-1].close / nikkei[-2].close - 1) * 100
    )
    assert result.failure_reason is None


def test_use_case_applies_adx_relief_to_danger_regime(monkeypatch):
    nikkei = _trading_bars(
        [100.0 + index for index in range(40)], date(2026, 1, 22)
    )
    vix = _bars([30.0])
    monkeypatch.setattr(
        "src.application.market_regime_usecase.previous_us_equity_trading_day",
        lambda today: vix[-1].date,
    )
    monkeypatch.setattr(
        "src.application.market_regime_usecase._previous_trading_day",
        lambda today: nikkei[-1].date,
    )

    result = MarketRegimeUseCase(
        FakeMarketDataClient(nikkei, vix), THRESHOLDS, realized_volatility_window=20
    ).execute()

    assert result.regime == MarketRegime.CAUTION
    assert result.adx is not None
    assert result.adx >= 27.0
    assert result.trend_relief_applied is True


def test_use_case_retries_until_nikkei_reaches_previous_trading_day(monkeypatch):
    previous_day = date(2026, 10, 13)
    monkeypatch.setattr(
        "src.application.market_regime_usecase._previous_trading_day",
        lambda today: previous_day,
    )
    current_bars = _trading_bars(
        [100.0 + index for index in range(29)], previous_day
    )
    stale_bars = current_bars[:-1]
    vix = _bars([20.0])
    monkeypatch.setattr(
        "src.application.market_regime_usecase.previous_us_equity_trading_day",
        lambda today: vix[-1].date,
    )

    class RetryingClient:
        def __init__(self):
            self.nikkei_calls = 0

        def get_daily_ohlc(self, symbol, range_="3mo"):
            if symbol == "^N225":
                self.nikkei_calls += 1
                return stale_bars if self.nikkei_calls == 1 else current_bars
            return vix

    client = RetryingClient()
    result = MarketRegimeUseCase(
        client, THRESHOLDS, realized_volatility_window=20
    ).execute()

    assert client.nikkei_calls == 2
    assert result.data_available is True
    assert result.nikkei_last_bar_date == previous_day


def test_use_case_retries_when_nikkei_history_has_a_missing_trading_day(monkeypatch):
    expected_date = date(2026, 10, 13)
    monkeypatch.setattr(
        "src.application.market_regime_usecase.previous_us_equity_trading_day",
        lambda today: expected_date,
    )
    complete_bars = _trading_bars(
        [100.0 + index for index in range(29)], expected_date
    )
    missing_bar = complete_bars[10]
    incomplete_bars = complete_bars[:10] + complete_bars[11:]
    vix = _trading_bars([20.0], expected_date)

    class RetryingClient:
        def __init__(self):
            self.nikkei_calls = 0

        def get_daily_ohlc(self, symbol, range_="3mo"):
            if symbol == "^N225":
                self.nikkei_calls += 1
                return incomplete_bars if self.nikkei_calls == 1 else complete_bars
            return vix

    client = RetryingClient()
    result = MarketRegimeUseCase(
        client,
        THRESHOLDS,
        realized_volatility_window=20,
        as_of_date=date(2026, 10, 14),
    ).execute()

    assert client.nikkei_calls == 2
    assert result.data_available is True
    assert result.nikkei_last_bar_date == expected_date
    assert missing_bar.date not in [bar.date for bar in incomplete_bars]


def test_use_case_continues_when_nikkei_history_gap_persists(monkeypatch):
    expected_date = date(2026, 10, 13)
    monkeypatch.setattr(
        "src.application.market_regime_usecase.previous_us_equity_trading_day",
        lambda today: expected_date,
    )
    complete_bars = _trading_bars(
        [100.0 + index for index in range(29)], expected_date
    )
    missing_bar = complete_bars[10]
    incomplete_bars = complete_bars[:10] + complete_bars[11:]
    vix = _trading_bars([20.0], expected_date)

    class IncompleteHistoryClient:
        def __init__(self):
            self.nikkei_calls = 0
            self.vix_calls = 0

        def get_daily_ohlc(self, symbol, range_="3mo"):
            if symbol == "^N225":
                self.nikkei_calls += 1
                return incomplete_bars
            self.vix_calls += 1
            return vix

    client = IncompleteHistoryClient()
    result = MarketRegimeUseCase(
        client,
        THRESHOLDS,
        realized_volatility_window=20,
        as_of_date=date(2026, 10, 14),
    ).execute()

    assert client.nikkei_calls == 3
    assert client.vix_calls == 1
    assert result.data_available is True
    assert result.regime == MarketRegime.CAUTION
    assert result.failure_reason is None
    assert missing_bar.date not in [bar.date for bar in incomplete_bars]


def test_use_case_retries_until_vix_reaches_previous_us_trading_day(monkeypatch):
    expected_vix_date = date(2026, 10, 13)
    monkeypatch.setattr(
        "src.application.market_regime_usecase._previous_trading_day",
        lambda today: date(2026, 10, 13),
    )
    monkeypatch.setattr(
        "src.application.market_regime_usecase.previous_us_equity_trading_day",
        lambda today: expected_vix_date,
    )
    nikkei = _trading_bars(
        [100.0 + index for index in range(29)], date(2026, 10, 13)
    )
    stale_vix = _bars([20.0])
    fresh_vix = _bars([20.0, 21.0])
    fresh_vix[-1] = MarketDailyBar(expected_vix_date, 21.0, 21.0, 21.0, 21.0)

    class RetryingClient:
        def __init__(self):
            self.vix_calls = 0

        def get_daily_ohlc(self, symbol, range_="3mo"):
            if symbol == "^N225":
                return nikkei
            self.vix_calls += 1
            return stale_vix if self.vix_calls == 1 else fresh_vix

    client = RetryingClient()
    result = MarketRegimeUseCase(
        client,
        THRESHOLDS,
        realized_volatility_window=20,
        as_of_date=date(2026, 10, 14),
    ).execute()

    assert client.vix_calls == 2
    assert result.data_available is True
    assert result.vix_last_bar_date == expected_vix_date


def test_use_case_returns_danger_when_vix_never_reaches_previous_us_trading_day(monkeypatch):
    expected_nikkei_date = date(2026, 10, 13)
    expected_vix_date = date(2026, 10, 13)
    monkeypatch.setattr(
        "src.application.market_regime_usecase._previous_trading_day",
        lambda today: expected_nikkei_date,
    )
    monkeypatch.setattr(
        "src.application.market_regime_usecase.previous_us_equity_trading_day",
        lambda today: expected_vix_date,
    )
    nikkei = _trading_bars(
        [100.0 + index for index in range(29)], expected_nikkei_date
    )
    stale_vix = _bars([20.0])

    class StaleVixClient:
        def __init__(self):
            self.vix_calls = 0

        def get_daily_ohlc(self, symbol, range_="3mo"):
            if symbol == "^N225":
                return nikkei
            self.vix_calls += 1
            return stale_vix

    client = StaleVixClient()
    result = MarketRegimeUseCase(
        client,
        THRESHOLDS,
        realized_volatility_window=20,
        as_of_date=date(2026, 10, 14),
    ).execute()

    assert client.vix_calls == 3
    assert result.regime == MarketRegime.DANGER
    assert result.data_available is False
    assert "VIX日足が米国市場の前営業日まで更新されていません" in result.failure_reason


def test_use_case_returns_danger_when_nikkei_never_reaches_previous_trading_day(monkeypatch):
    previous_day = date(2026, 10, 13)
    monkeypatch.setattr(
        "src.application.market_regime_usecase._previous_trading_day",
        lambda today: previous_day,
    )

    class StaleClient:
        def __init__(self):
            self.nikkei_calls = 0
            self.vix_calls = 0

        def get_daily_ohlc(self, symbol, range_="3mo"):
            if symbol == "^N225":
                self.nikkei_calls += 1
                return _bars([100.0 + index for index in range(22)])
            self.vix_calls += 1
            return _bars([20.0])

    client = StaleClient()
    result = MarketRegimeUseCase(
        client, THRESHOLDS, realized_volatility_window=20
    ).execute()

    assert client.nikkei_calls == 3
    assert client.vix_calls == 0
    assert result.regime == MarketRegime.DANGER
    assert result.data_available is False
    assert "前営業日まで更新されていません" in result.failure_reason


def test_use_case_falls_back_to_danger_when_data_is_unavailable():
    result = MarketRegimeUseCase(
        FakeMarketDataClient(), THRESHOLDS, realized_volatility_window=20
    ).execute()

    assert result.regime == MarketRegime.DANGER
    assert result.data_available is False
    assert result.failure_reason


def test_use_case_falls_back_to_danger_when_client_raises():
    class FailingClient:
        def get_daily_ohlc(self, symbol, range_="3mo"):
            raise TimeoutError("Yahoo unavailable")

    result = MarketRegimeUseCase(
        FailingClient(), THRESHOLDS, realized_volatility_window=20,
        as_of_date=date(2026, 10, 14),
    ).execute()

    assert result.regime == MarketRegime.DANGER
    assert result.data_available is False
    assert "Yahoo unavailable" in result.failure_reason