"""市場全体の荒れ具合を日次データから判定するユースケース。"""
import logging

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
    calculate_previous_day_changes,
    calculate_realized_volatility_series,
)

logger = logging.getLogger(__name__)


class MarketRegimeUseCase:
    def __init__(
        self,
        market_data_client,
        thresholds: MarketRegimeThresholds,
        realized_volatility_window: int = 20,
        data_range: str = "3mo",
        adx_threshold: float = 27.0,
    ):
        if realized_volatility_window <= 0:
            raise ValueError("実現ボラティリティ期間は正数で指定してください")
        self.market_data_client = market_data_client
        self.thresholds = thresholds
        self.realized_volatility_window = realized_volatility_window
        self.data_range = data_range
        self.adx_threshold = adx_threshold

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
            nikkei_bars = self.market_data_client.get_daily_ohlc(
                "^N225", range_=self.data_range
            )
            vix_bars = self.market_data_client.get_daily_ohlc(
                "^VIX", range_=self.data_range
            )
            if not nikkei_bars or not vix_bars:
                return self._unavailable("日経225またはVIXの日足データが空です")

            nikkei_closes = [DatedClose(bar.date, bar.close) for bar in nikkei_bars]
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
