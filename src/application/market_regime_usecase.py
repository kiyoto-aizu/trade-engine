"""市場全体の荒れ具合を日次データから判定するユースケース。"""
import logging
from datetime import date, datetime, timedelta, timezone

from src.domain.market_regime import (
    apply_trend_relief,
    MarketRegime,
    MarketRegimeAssessment,
    MarketRegimeThresholds,
    calculate_market_regime,
)
from src.domain.market_trend import calculate_adx
from src.domain.market_volatility import (
    DatedClose,
    MarketDailyBar,
    calculate_previous_day_changes,
    calculate_realized_volatility_series,
)
from src.domain.us_market_calendar import previous_us_equity_trading_day
from src.infrastructure.calendar.japanese_calendar import is_trading_day

logger = logging.getLogger(__name__)
JST = timezone(timedelta(hours=9))
NIKKEI_DAILY_BAR_MAX_ATTEMPTS = 3
VIX_DAILY_BAR_MAX_ATTEMPTS = 3


def _previous_trading_day(today: date) -> date:
    candidate = today - timedelta(days=1)
    while not is_trading_day(candidate):
        candidate -= timedelta(days=1)
    return candidate


def _previous_trading_days(end_date: date, count: int) -> list[date]:
    days: list[date] = []
    candidate = end_date
    while len(days) < count:
        if is_trading_day(candidate):
            days.append(candidate)
        candidate -= timedelta(days=1)
    return list(reversed(days))


class MarketRegimeUseCase:
    def __init__(
        self,
        market_data_client,
        thresholds: MarketRegimeThresholds,
        realized_volatility_window: int = 20,
        data_range: str = "3mo",
        adx_threshold: float = 27.0,
        as_of_date: date | None = None,
    ):
        if realized_volatility_window <= 0:
            raise ValueError("実現ボラティリティ期間は正数で指定してください")
        self.market_data_client = market_data_client
        self.thresholds = thresholds
        self.realized_volatility_window = realized_volatility_window
        self.data_range = data_range
        self.adx_threshold = adx_threshold
        self.as_of_date = as_of_date

    def _get_daily_bars_for_date(
        self,
        symbol: str,
        expected_date: date,
        max_attempts: int,
        required_bar_count: int = 1,
    ) -> tuple[list[MarketDailyBar] | None, date | None, list[date]]:
        actual_date: date | None = None
        bars: list[MarketDailyBar] = []
        missing_dates: list[date] = []
        best_fresh_bars: list[MarketDailyBar] | None = None
        best_missing_dates: list[date] = []
        expected_history_dates = (
            [expected_date]
            if required_bar_count == 1
            else _previous_trading_days(expected_date, required_bar_count)
        )
        for attempt in range(1, max_attempts + 1):
            bars = sorted(
                self.market_data_client.get_daily_ohlc(symbol, range_=self.data_range),
                key=lambda bar: bar.date,
            )
            actual_date = bars[-1].date if bars else None
            available_dates = {bar.date for bar in bars}
            missing_dates = [
                target_date
                for target_date in expected_history_dates
                if target_date not in available_dates
            ]
            if actual_date == expected_date:
                if not missing_dates:
                    return bars, actual_date, []
                if (
                    best_fresh_bars is None
                    or len(missing_dates) < len(best_missing_dates)
                ):
                    best_fresh_bars = bars
                    best_missing_dates = missing_dates
            logger.warning(
                "%s日足の最新日または必要履歴が不足しています: "
                "期待最新日=%s / 取得最新日=%s / 履歴欠損日=%s / 試行=%d/%d",
                symbol,
                expected_date,
                actual_date or "データなし",
                ",".join(map(str, missing_dates)) or "なし",
                attempt,
                max_attempts,
            )
        if best_fresh_bars is not None:
            return best_fresh_bars, expected_date, best_missing_dates
        return None, actual_date, missing_dates

    def _unavailable(self, reason: str) -> MarketRegimeAssessment:
        logger.warning("MarketRegimeを安全側にフォールバックします: %s", reason)
        return MarketRegimeAssessment(
            regime=MarketRegime.DANGER,
            realized_volatility_percent=None,
            vix=None,
            nikkei_change_percent=None,
            data_available=False,
            failure_reason=reason,
        )

    def execute(self) -> MarketRegimeAssessment:
        try:
            reference_date = self.as_of_date or datetime.now(JST).date()
            expected_nikkei_date = _previous_trading_day(reference_date)
            required_nikkei_bar_count = max(
                self.realized_volatility_window + 1,
                14 * 2 + 1,
            )
            nikkei_bars, actual_nikkei_date, missing_nikkei_dates = (
                self._get_daily_bars_for_date(
                    "^N225",
                    expected_nikkei_date,
                    NIKKEI_DAILY_BAR_MAX_ATTEMPTS,
                    required_nikkei_bar_count,
                )
            )
            if nikkei_bars is None:
                return self._unavailable(
                    "日経225日足が前営業日まで更新されていません"
                    f"(期待日={expected_nikkei_date}, "
                    f"取得最新日={actual_nikkei_date or 'データなし'}, "
                    f"試行={NIKKEI_DAILY_BAR_MAX_ATTEMPTS}回)"
                )
            if missing_nikkei_dates:
                logger.warning(
                    "日経225の日足履歴に欠損が残っています。取得済みデータで判定を続行します: %s",
                    ",".join(map(str, missing_nikkei_dates)),
                )

            expected_vix_date = previous_us_equity_trading_day(reference_date)
            vix_bars, actual_vix_date, _ = self._get_daily_bars_for_date(
                "^VIX", expected_vix_date, VIX_DAILY_BAR_MAX_ATTEMPTS
            )
            if vix_bars is None:
                return self._unavailable(
                    "VIX日足が米国市場の前営業日まで更新されていません"
                    f"(期待日={expected_vix_date}, "
                    f"取得最新日={actual_vix_date or 'データなし'}, "
                    f"試行={VIX_DAILY_BAR_MAX_ATTEMPTS}回)"
                )

            nikkei_closes = [
                DatedClose(bar.date, bar.close) for bar in nikkei_bars
            ]
            volatility_points = calculate_realized_volatility_series(
                nikkei_closes,
                self.realized_volatility_window,
            )
            changes = calculate_previous_day_changes(nikkei_closes)
            if not volatility_points or not changes:
                return self._unavailable("MarketRegime判定に必要な日経225データが不足しています")

            realized_volatility = volatility_points[-1].value_percent
            nikkei_change = changes[-1].value_percent
            vix = float(vix_bars[-1].close)
            regime = calculate_market_regime(
                realized_volatility,
                vix,
                nikkei_change,
                self.thresholds,
            )
            adx = calculate_adx(nikkei_bars)
            final_regime = apply_trend_relief(regime, adx, self.adx_threshold)
            relief_applied = final_regime != regime
            if relief_applied:
                logger.info("MarketRegime緩和: %s→%s(ADX=%.1f)", regime.value, final_regime.value, adx)
            return MarketRegimeAssessment(
                regime=final_regime,
                realized_volatility_percent=realized_volatility,
                vix=vix,
                nikkei_change_percent=nikkei_change,
                data_available=True,
                adx=adx,
                trend_relief_applied=relief_applied,
                regime_before_relief=regime,
                nikkei_last_bar_date=nikkei_bars[-1].date,
                nikkei_last_close=float(nikkei_bars[-1].close),
                vix_last_bar_date=vix_bars[-1].date,
                vix_last_close=float(vix_bars[-1].close),
            )
        except Exception as exc:
            logger.exception("MarketRegime判定用データの取得または計算に失敗しました")
            return self._unavailable(f"データ取得または計算エラー: {exc}")
