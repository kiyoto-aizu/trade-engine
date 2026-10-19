"""選定時点のATR比(前日確定日足のTR÷ATR)の観測記録。売買判断・選定結果には使わない。"""
from dataclasses import dataclass
from datetime import date
from typing import Sequence

from src.domain.atr_ratio_analysis import DatedDailyBar
from src.domain.models import ScoredCandidate
from src.domain.rules import select_top_n_by_surge_ratio
from src.domain.volatility import assess_volatility

REASON_NO_DAILY_BARS = "NO_DAILY_BARS"
REASON_INSUFFICIENT_BARS = "INSUFFICIENT_BARS"
REASON_LATEST_BAR_MISMATCH = "LATEST_BAR_MISMATCH"
REASON_INVALID_BAR = "INVALID_BAR"
REASON_ZERO_ATR = "ZERO_ATR"
REASON_FETCH_FAILED = "DAILY_FETCH_FAILED"
REASON_TIME_LIMIT = "OBSERVATION_TIME_LIMIT"
REASON_NOT_OBSERVED_PRICE_FILTERED = "NOT_OBSERVED_PRICE_FILTERED"


@dataclass(frozen=True)
class SelectionAtrObservation:
    """as_of日の確定日足までで算出したATR比と区分。算出できない場合は理由を持つ。"""

    as_of: str
    ratio: float | None = None
    level: str | None = None
    unavailable_reason: str | None = None

    def as_record(self) -> dict:
        return {
            "selection_atr_as_of": self.as_of,
            "selection_atr_ratio": self.ratio,
            "selection_atr_level": self.level,
            "selection_atr_unavailable_reason": self.unavailable_reason,
        }


def unavailable(as_of: date, reason: str) -> SelectionAtrObservation:
    return SelectionAtrObservation(as_of.isoformat(), unavailable_reason=reason)


def observe_selection_atr(
    dated_bars: Sequence[DatedDailyBar],
    as_of: date,
    period: int = 14,
    caution_ratio: float = 1.5,
    danger_ratio: float = 2.0,
) -> SelectionAtrObservation:
    """as_of日以前の確定日足だけからATR比(最新TR÷ATR)を算出する。as_of日の日足が最新でなければ算出しない。"""
    usable = sorted((item for item in dated_bars if item.date <= as_of), key=lambda item: item.date)
    if not usable:
        return unavailable(as_of, REASON_NO_DAILY_BARS)
    if usable[-1].date != as_of:
        return unavailable(as_of, REASON_LATEST_BAR_MISMATCH)
    if len(usable) < period:
        return unavailable(as_of, REASON_INSUFFICIENT_BARS)
    try:
        assessment = assess_volatility(
            [item.bar for item in usable], period=period, caution_ratio=caution_ratio, danger_ratio=danger_ratio
        )
    except ValueError:
        return unavailable(as_of, REASON_INVALID_BAR)
    if assessment is None:
        return unavailable(as_of, REASON_INSUFFICIENT_BARS)
    if assessment.atr <= 0:
        return unavailable(as_of, REASON_ZERO_ATR)
    return SelectionAtrObservation(as_of.isoformat(), assessment.ratio, assessment.level.value)


def build_atr_below_comparison(
    scored: Sequence[ScoredCandidate],
    atr_ratio_by_symbol: dict[str, float | None],
    adopted: Sequence[str],
    limit_ratio: float = 2.0,
    n: int = 10,
) -> dict:
    """ATR比がlimit_ratio未満の銘柄だけから倍率上位n件を選んだ場合を、現行の採用と並べて返す(比較用のみ)。"""
    known = [item for item in scored if atr_ratio_by_symbol.get(item.symbol) is not None]
    eligible = [item for item in known if atr_ratio_by_symbol[item.symbol] < limit_ratio]
    symbols = select_top_n_by_surge_ratio(eligible, n)
    return {
        "atr_ratio_below": limit_ratio,
        "symbols": symbols,
        "count": len(symbols),
        "shortfall": max(n - len(symbols), 0),
        "eligible_count": len(eligible),
        "unknown_atr_count": len(scored) - len(known),
        "replaced_out": [symbol for symbol in adopted if symbol not in symbols],
        "replaced_in": [symbol for symbol in symbols if symbol not in adopted],
        "comparison_only": True,
    }
