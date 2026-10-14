import sqlite3
from datetime import date, datetime, timedelta
from pathlib import Path

from src.application import market_regime_usecase
from src.application.market_regime_usecase import MarketRegimeUseCase
from src.application.trading_usecase import TradingUseCase
from src.domain.market_regime import MarketRegime, MarketRegimeAssessment, MarketRegimeThresholds
from src.domain.market_volatility import MarketDailyBar
from src.infrastructure.calendar.japanese_calendar import is_trading_day
from src.infrastructure.persistence.market_regime_daily_repository import MarketRegimeDailyRepository


def _bars(closes, start=date(2026, 1, 1)):
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


class FakeClient:
    def __init__(self, nikkei, vix):
        self.nikkei, self.vix = nikkei, vix

    def get_daily_ohlc(self, symbol, range_="3mo"):
        return self.nikkei if symbol == "^N225" else self.vix


def test_use_case_records_pre_relief_regime_and_last_bar_inputs(monkeypatch):
    nikkei = _trading_bars(
        [100.0 + index for index in range(40)], date(2026, 2, 9)
    )
    vix = _bars([30.0, 31.0])
    monkeypatch.setattr(
        market_regime_usecase, "_previous_trading_day", lambda today: nikkei[-1].date
    )
    monkeypatch.setattr(
        market_regime_usecase,
        "previous_us_equity_trading_day",
        lambda today: vix[-1].date,
    )

    result = MarketRegimeUseCase(
        FakeClient(nikkei, vix), MarketRegimeThresholds(), realized_volatility_window=20
    ).execute()

    assert result.regime == MarketRegime.CAUTION
    assert result.regime_before_relief == MarketRegime.DANGER
    assert result.trend_relief_applied is True
    assert result.nikkei_last_bar_date == nikkei[-1].date
    assert result.nikkei_last_close == nikkei[-1].close
    assert result.vix_last_bar_date == vix[-1].date
    assert result.vix_last_close == 31.0


def test_repository_overwrites_same_day_and_mode_without_duplicates(tmp_path):
    repository = MarketRegimeDailyRepository(tmp_path / "regime.sqlite3")
    first = MarketRegimeAssessment(
        regime=MarketRegime.DANGER, realized_volatility_percent=None, vix=None,
        nikkei_change_percent=None, data_available=False, failure_reason="取得失敗",
    )
    second = MarketRegimeAssessment(
        regime=MarketRegime.CAUTION, realized_volatility_percent=18.0, vix=20.0,
        nikkei_change_percent=-0.5, data_available=True, adx=30.0, trend_relief_applied=True,
        regime_before_relief=MarketRegime.DANGER, nikkei_last_bar_date=date(2026, 10, 8),
        nikkei_last_close=39000.0, vix_last_bar_date=date(2026, 10, 8), vix_last_close=20.0,
    )

    repository.save(first, datetime(2026, 10, 9, 9, 36), "paper", {"vix_caution": 17.0})
    repository.save(second, datetime(2026, 10, 9, 9, 40), "paper", {"vix_caution": 17.0})
    repository.save(first, datetime(2026, 10, 9, 9, 40), "live")

    rows = repository.load(date(2026, 10, 9))
    assert [row["execution_mode"] for row in rows] == ["live", "paper"]
    paper = repository.load(date(2026, 10, 9), "paper")
    assert len(paper) == 1
    assert paper[0]["final_regime"] == "CAUTION"
    assert paper[0]["regime_before_relief"] == "DANGER"
    assert paper[0]["trend_relief_applied"] == 1
    assert paper[0]["nikkei_last_bar_date"] == "2026-10-08"
    assert paper[0]["decided_at"] == "2026-10-09T09:40:00"
    assert '"vix_caution": 17.0' in paper[0]["thresholds_json"]


class StubRegimeUseCase:
    thresholds = MarketRegimeThresholds()
    adx_threshold = 27.0
    realized_volatility_window = 20
    data_range = "3mo"

    def __init__(self, assessment):
        self.assessment = assessment
        self.calls = 0

    def execute(self):
        self.calls += 1
        return self.assessment


def _assessment():
    return MarketRegimeAssessment(
        regime=MarketRegime.CAUTION, realized_volatility_percent=18.0, vix=20.0,
        nikkei_change_percent=0.1, data_available=True, adx=20.0,
        regime_before_relief=MarketRegime.CAUTION,
    )


def test_prepare_market_regime_saves_one_row_and_keeps_regime(tmp_path):
    repository = MarketRegimeDailyRepository(tmp_path / "regime.sqlite3")
    regime_usecase = StubRegimeUseCase(_assessment())
    use_case = TradingUseCase(
        token="t", order_history_path=Path(tmp_path / "orders.json"),
        market_regime_usecase=regime_usecase, market_regime_daily_repository=repository,
    )
    use_case._now_provider = lambda: datetime(2026, 10, 9, 9, 36)

    use_case.prepare_market_regime()
    use_case.prepare_market_regime()

    assert regime_usecase.calls == 1
    assert use_case.market_regime == MarketRegime.CAUTION
    rows = repository.load(date(2026, 10, 9))
    assert len(rows) == 1
    assert rows[0]["execution_mode"] == use_case._execution_mode


def test_prepare_market_regime_ignores_record_failures(tmp_path):
    class BrokenRepository:
        def save(self, *args, **kwargs):
            raise sqlite3.OperationalError("disk I/O error")

    use_case = TradingUseCase(
        token="t", order_history_path=Path(tmp_path / "orders.json"),
        market_regime_usecase=StubRegimeUseCase(_assessment()),
        market_regime_daily_repository=BrokenRepository(),
    )

    assessment = use_case.prepare_market_regime()

    assert assessment.regime == MarketRegime.CAUTION
    assert use_case.market_regime == MarketRegime.CAUTION
