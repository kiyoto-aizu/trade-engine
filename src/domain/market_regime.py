"""市場全体の荒れ具合を判定するドメインロジック。"""
from dataclasses import dataclass
from enum import Enum
from math import isfinite
from datetime import date
from typing import Sequence

from src.domain.market_volatility import (
    DatedClose,
    MarketDailyBar,
    calculate_previous_day_changes,
    calculate_realized_volatility_series,
)
from src.domain.market_trend import calculate_adx_series
from src.domain.us_market_calendar import previous_us_equity_trading_day


class MarketRegime(str, Enum):
    NORMAL = "NORMAL"
    CAUTION = "CAUTION"
    DANGER = "DANGER"


@dataclass(frozen=True)
class MarketRegimeThresholds:
    realized_vol_caution: float = 17.0
    realized_vol_danger: float = 29.0
    vix_caution: float = 17.0
    vix_danger: float = 27.0
    nikkei_change_upgrade: float = 2.0

    def __post_init__(self) -> None:
        threshold_values = (
            self.realized_vol_caution,
            self.realized_vol_danger,
            self.vix_caution,
            self.vix_danger,
            self.nikkei_change_upgrade,
        )
        if any(not isfinite(value) or value < 0 for value in threshold_values):
            raise ValueError("MarketRegimeの閾値は0以上の有限値で指定してください")
        if self.realized_vol_danger <= self.realized_vol_caution:
            raise ValueError("実現ボラのDANGER閾値はCAUTION閾値より大きくしてください")
        if self.vix_danger <= self.vix_caution:
            raise ValueError("VIXのDANGER閾値はCAUTION閾値より大きくしてください")


@dataclass(frozen=True)
class MarketRegimeAssessment:
    regime: MarketRegime
    realized_volatility_percent: float | None
    vix: float | None
    nikkei_change_percent: float | None
    data_available: bool
    failure_reason: str | None = None
    adx: float | None = None
    trend_relief_applied: bool = False
    # 検証用の記録項目。判定には使わない
    regime_before_relief: MarketRegime | None = None
    nikkei_last_bar_date: date | None = None
    nikkei_last_close: float | None = None
    vix_last_bar_date: date | None = None
    vix_last_close: float | None = None


@dataclass(frozen=True)
class MarketRegimeSeriesResult:
    regimes: dict[date, MarketRegime]
    trend_relief_dates: frozenset[date]


def _validate_value(value: float, name: str) -> None:
    if not isfinite(value) or value < 0:
        raise ValueError(f"{name}は0以上の有限値で指定してください")


def _more_severe(first: MarketRegime, second: MarketRegime) -> MarketRegime:
    return max(first, second, key=lambda regime: (MarketRegime.NORMAL, MarketRegime.CAUTION, MarketRegime.DANGER).index(regime))


def _upgrade_one_level(regime: MarketRegime) -> MarketRegime:
    if regime == MarketRegime.NORMAL:
        return MarketRegime.CAUTION
    if regime == MarketRegime.CAUTION:
        return MarketRegime.DANGER
    return MarketRegime.DANGER


def classify_realized_volatility(
    value_percent: float | None,
    thresholds: MarketRegimeThresholds,
) -> MarketRegime:
    """日経225の年率実現ボラティリティを判定します。"""
    if value_percent is None:
        return MarketRegime.DANGER
    _validate_value(value_percent, "実現ボラティリティ")
    if value_percent >= thresholds.realized_vol_danger:
        return MarketRegime.DANGER
    if value_percent >= thresholds.realized_vol_caution:
        return MarketRegime.CAUTION
    return MarketRegime.NORMAL


def classify_vix(value: float | None, thresholds: MarketRegimeThresholds) -> MarketRegime:
    """VIX終値を判定します。"""
    if value is None:
        return MarketRegime.DANGER
    _validate_value(value, "VIX")
    if value >= thresholds.vix_danger:
        return MarketRegime.DANGER
    if value >= thresholds.vix_caution:
        return MarketRegime.CAUTION
    return MarketRegime.NORMAL


def calculate_market_regime(
    realized_volatility_percent: float | None,
    vix: float | None,
    nikkei_change_percent: float | None,
    thresholds: MarketRegimeThresholds,
) -> MarketRegime:
    """実現ボラとVIXを統合し、必要なら前日比で1段階格上げします。"""
    regime = _more_severe(
        classify_realized_volatility(realized_volatility_percent, thresholds),
        classify_vix(vix, thresholds),
    )
    if nikkei_change_percent is not None:
        if not isfinite(nikkei_change_percent):
            raise ValueError("日経平均前日比は有限値で指定してください")
        if abs(nikkei_change_percent) >= thresholds.nikkei_change_upgrade:
            regime = _upgrade_one_level(regime)
    return regime


def apply_trend_relief(
    market_regime: MarketRegime,
    adx: float | None,
    adx_threshold: float = 27.0,
) -> MarketRegime:
    """ADXが閾値以上の明確なトレンド時だけ市場レジームを1段階緩和します。"""
    if adx_threshold < 0 or not isfinite(adx_threshold):
        raise ValueError("ADX閾値は0以上の有限値で指定してください")
    if adx is None:
        return market_regime
    if adx < 0 or not isfinite(adx):
        raise ValueError("ADXは0以上の有限値で指定してください")
    if adx < adx_threshold:
        return market_regime
    if market_regime == MarketRegime.DANGER:
        return MarketRegime.CAUTION
    if market_regime == MarketRegime.CAUTION:
        return MarketRegime.NORMAL
    return MarketRegime.NORMAL


def resolve_rsi_entry_threshold(
    market_regime: MarketRegime,
    normal_threshold: float,
    caution_threshold: float,
) -> float:
    """市場レジームに応じたBUY用RSI閾値を返します。"""
    if not isfinite(normal_threshold) or not isfinite(caution_threshold):
        raise ValueError("RSIエントリー閾値は有限値で指定してください")
    if normal_threshold < 0 or caution_threshold < 0:
        raise ValueError("RSIエントリー閾値は0以上で指定してください")
    if market_regime == MarketRegime.NORMAL:
        return normal_threshold
    return caution_threshold


def calculate_market_regime_series(
    nikkei_bars: Sequence[MarketDailyBar],
    vix_bars: Sequence[MarketDailyBar],
    window: int,
    thresholds: MarketRegimeThresholds,
) -> dict[date, MarketRegime]:
    """未来データを使わず、対象日ごとのMarketRegimeを計算します。"""
    return calculate_market_regime_series_with_details(
        nikkei_bars, vix_bars, window, thresholds
    ).regimes


def calculate_market_regime_series_with_details(
    nikkei_bars: Sequence[MarketDailyBar],
    vix_bars: Sequence[MarketDailyBar],
    window: int,
    thresholds: MarketRegimeThresholds,
    adx_threshold: float = 27.0,
) -> MarketRegimeSeriesResult:
    """ADX緩和情報を含む、未来データを使わないレジーム系列を返します。"""
    if window <= 0:
        raise ValueError("実現ボラティリティ期間は正数で指定してください")
    nikkei_closes = [DatedClose(bar.date, bar.close) for bar in nikkei_bars]
    vix_closes = sorted(
        (DatedClose(bar.date, bar.close) for bar in vix_bars),
        key=lambda item: item.date,
    )
    volatility_points = {
        point.date: point.value_percent
        for point in calculate_realized_volatility_series(nikkei_closes, window)
    }
    changes = {
        point.date: point.value_percent
        for point in calculate_previous_day_changes(nikkei_closes)
    }
    adx_points = {
        point.date: point.value
        for point in calculate_adx_series(nikkei_bars)
    }
    result: dict[date, MarketRegime] = {}
    trend_relief_dates: set[date] = set()
    vix_index = 0
    latest_vix: float | None = None
    latest_vix_date: date | None = None
    for target_date in sorted(volatility_points):
        while vix_index < len(vix_closes) and vix_closes[vix_index].date < target_date:
            latest_vix = vix_closes[vix_index].close
            latest_vix_date = vix_closes[vix_index].date
            vix_index += 1
        expected_vix_date = previous_us_equity_trading_day(target_date)
        if latest_vix_date != expected_vix_date:
            result[target_date] = MarketRegime.DANGER
            continue
        base_regime = calculate_market_regime(
            volatility_points[target_date],
            latest_vix,
            changes.get(target_date),
            thresholds,
        )
        final_regime = apply_trend_relief(
            base_regime, adx_points.get(target_date), adx_threshold
        )
        result[target_date] = final_regime
        if final_regime != base_regime:
            trend_relief_dates.add(target_date)
    return MarketRegimeSeriesResult(result, frozenset(trend_relief_dates))
