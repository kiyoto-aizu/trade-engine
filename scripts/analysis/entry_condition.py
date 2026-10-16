"""買いなし銘柄の「買い条件」と「仮の売りライン」を計算する。

判定は本番の関数(calculate_price_limit / calculate_rsi / resolve_rsi_entry_threshold /
assess_volatility / resolve_atr_exit_multiplier)を再利用し、同じ計算を二重に持たない。
表示用の読み取り専用処理で、売買判定には使われない。
"""
from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from src.config import config
from src.domain.market_regime import MarketRegime, resolve_rsi_entry_threshold
from src.domain.rules import calculate_price_limit, calculate_rsi
from src.domain.volatility import (
    DailyBar,
    VolatilityLevel,
    assess_volatility,
    resolve_atr_exit_multiplier,
    stop_loss_multiplier,
)

SIGNAL_SKIP_EVENTS = frozenset({"ATR_DANGER_SKIP", "MARKET_REGIME_DANGER_SKIP"})
PATTERN_BAND_SHORT = 1
PATTERN_RSI_SHORT = 2
PATTERN_SKIPPED_SIGNAL = 3


def daily_history(rows: dict, day: str) -> tuple[list[float], list[DailyBar]]:
    """日足キャッシュから、対象日より前の確定終値とOHLCを日付順に取り出す。"""
    closes: list[float] = []
    bars: list[DailyBar] = []
    for key in sorted(rows):
        if key >= day:
            continue
        row = rows[key]
        try:
            high, low, close = float(row["high"]), float(row["low"]), float(row["close"])
        except (KeyError, TypeError, ValueError):
            continue
        closes.append(close)
        bars.append(DailyBar(high=high, low=low, close=close))
    return closes, bars


def _number(value: Any) -> float | None:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def recorded_rsi(events: list[dict]) -> tuple[float, float] | None:
    """判定記録に残っている(RSI, 適用基準)。両方そろった最初の記録を使う。"""
    for event in sorted(events, key=lambda row: str(row.get("occurred_at") or "")):
        rsi, threshold = _number(event.get("rsi")), _number(event.get("rsi_applied_threshold"))
        if rsi is None or threshold is None:
            try:
                snapshot = (json.loads(event.get("input_json") or "{}") or {}).get("rsi_input") or {}
            except (TypeError, ValueError):
                snapshot = {}
            rsi = rsi if rsi is not None else _number(snapshot.get("rsi"))
            threshold = threshold if threshold is not None else _number(snapshot.get("applied_threshold"))
        if rsi is not None and threshold is not None:
            return rsi, threshold
    return None


def unavailable(reason: str) -> dict:
    return {"status": "unavailable", "reason": reason}


def evaluate_entry_condition(
    closes: list[float], regime: str | None, recorded: tuple[float, float] | None
) -> dict:
    """上側バンド・RSI・RSI基準を求める。RSIと基準は記録を優先し、無ければ再計算する。"""
    limit = calculate_price_limit(closes)
    if limit is None:
        return unavailable(f"日足不足(終値{len(closes)}本)")
    sources = {"upper_band": "recomputed"}
    if recorded is not None:
        rsi, threshold = recorded
        sources.update({"rsi": "recorded", "rsi_threshold": "recorded"})
    else:
        rsi = calculate_rsi(closes, config.RSI_PERIOD, config.RSI_MINIMUM_CLOSES)
        if rsi is None:
            return unavailable(f"RSI算出不可(終値{len(closes)}本)")
        try:
            threshold = resolve_rsi_entry_threshold(
                MarketRegime(str(regime or "").upper()),
                config.RSI_ENTRY_THRESHOLD,
                config.RSI_ENTRY_THRESHOLD_CAUTION,
            )
        except ValueError:
            return unavailable("市場状態の記録なし")
        sources.update({"rsi": "recomputed", "rsi_threshold": "recomputed"})
    return {
        "status": "ok",
        "upper_band": limit.upper_band,
        "rsi": rsi,
        "rsi_threshold": threshold,
        "rsi_ok": rsi >= threshold,
        "regime": str(regime).upper() if regime else None,
        "sources": sources,
    }


def closest_approach(
    bars: list[dict], upper_band: float
) -> tuple[float, datetime] | None:
    """上側バンドまでの距離が最小になる値と最も早い時刻。到達済みは0円とする。"""
    if not bars:
        return None
    best = min(range(len(bars)), key=lambda index: (max(upper_band - bars[index]["price"], 0.0), index))
    return max(upper_band - bars[best]["price"], 0.0), bars[best]["time"]


def hypothetical_exit(
    bars: list[dict],
    entry: float,
    daily_bars: list[DailyBar],
    recorded_atr: tuple[float, str] | None = None,
) -> dict:
    """見送り時点で買ったと仮定した、ATRの売りライン(階段状)を本番の計算式で求める。"""
    assessment = assess_volatility(
        daily_bars, config.ATR_PERIOD, config.ATR_CAUTION_RATIO, config.ATR_DANGER_RATIO
    ) if daily_bars else None
    if assessment is not None:
        atr, level = assessment.atr, assessment.level
        atr_source = "recomputed"
    elif recorded_atr is not None:
        atr, level = recorded_atr[0], VolatilityLevel(recorded_atr[1])
        atr_source = "recorded"
    else:
        return unavailable("ATR算出不可(日足不足)")
    if atr <= 0 or not bars:
        return unavailable("ATR算出不可" if atr <= 0 else "見送り後の分足なし")
    stop_multiplier = stop_loss_multiplier(
        level,
        config.ATR_STOP_NORMAL_MULTIPLIER,
        config.ATR_STOP_CAUTION_MULTIPLIER,
        config.ATR_STOP_DANGER_MULTIPLIER,
    )
    held_high = entry
    times: list[datetime] = []
    lines: list[float] = []
    multipliers: list[float] = []
    exit_bar: dict | None = None
    for bar in bars:
        held_high = max(held_high, bar["price"])
        multiplier = resolve_atr_exit_multiplier(
            held_high - entry, atr, level,
            config.ATR_STOP_NORMAL_MULTIPLIER,
            config.ATR_STOP_CAUTION_MULTIPLIER,
            config.ATR_STOP_DANGER_MULTIPLIER,
            config.ATR_PROFIT_LOCK_NORMAL_MULTIPLIER,
            config.ATR_PROFIT_LOCK_CAUTION_MULTIPLIER,
            config.ATR_PROFIT_LOCK_DANGER_MULTIPLIER,
            config.ATR_PROFIT_LOCK_TRIGGER_ATR_MULTIPLE,
        )
        line = held_high - atr * multiplier
        times.append(bar["time"])
        lines.append(line)
        multipliers.append(multiplier)
        if bar["price"] <= line:
            exit_bar = bar
            break
    covered = bars[: len(times)]
    margins = [bar["price"] - line for bar, line in zip(covered, lines)]
    return {
        "status": "ok",
        "atr": atr,
        "atr_level": level.value,
        "atr_source": atr_source,
        "stop": entry - atr * stop_multiplier,
        "stop_multiplier": stop_multiplier,
        "final_multiplier": multipliers[-1],
        "times": times,
        "lines": lines,
        "margins": margins,
        "exit": exit_bar,
    }
