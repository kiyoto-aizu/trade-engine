"""指定営業日の監視銘柄について、分足と売買ラインを可視化する読み取り専用診断。"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from dataclasses import dataclass
from datetime import datetime, time
from pathlib import Path
from typing import Any

import pyarrow.parquet as parquet

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from src.config import config
from scripts.analysis.entry_condition import daily_history

DEFAULT_OUTPUT = ROOT / "data" / "backtest_v2_scratch" / "diagnostics" / "intraday_trade_diagnostics"
MARKET_BREAK_START = time(11, 30)
MARKET_BREAK_END = time(12, 30)
SESSION_START = time(9, 0)
SESSION_END = time(15, 30)
TIME_TICKS = [(0, "9:00"), (60, "10:00"), (120, "11:00"), (180, "13:00"), (240, "14:00"), (300, "15:00")]
REGIME_COLORS = {"NORMAL": "#d9ead3", "CAUTION": "#fff2cc", "DANGER": "#f4cccc"}


@dataclass(frozen=True)
class TradeLines:
    entry: float
    stop: float
    atr: float
    stop_multiplier: float
    profit_multiplier: float
    trigger_multiple: float


def _json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def _parse_time(value: Any) -> datetime | None:
    if value is None:
        return None
    try:
        parsed = value if isinstance(value, datetime) else datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    return parsed.replace(tzinfo=None) if parsed.tzinfo else parsed


def partition_path(root: Path, day: str, symbol: str) -> Path:
    return root / f"symbol={symbol}" / f"date={day}" / "data.parquet"


def atr_from_daily_cache(rows: dict, day: str) -> float | None:
    prior = [(date, rows[date]) for date in sorted(rows) if date < day]
    if len(prior) < config.ATR_PERIOD:
        return None
    recent = prior[-config.ATR_PERIOD:]
    true_ranges = []
    for index, (_, bar) in enumerate(recent):
        high, low = float(bar["high"]), float(bar["low"])
        previous_close = float(recent[index - 1][1]["close"]) if index else float(bar["close"])
        true_ranges.append(max(high - low, abs(high - previous_close), abs(low - previous_close)))
    return sum(true_ranges) / config.ATR_PERIOD


def load_minute_bars(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    rows = parquet.read_table(path, columns=["time", "price"]).to_pylist()
    result = []
    for row in rows:
        moment = _parse_time(row.get("time"))
        price = row.get("price")
        if moment is not None and price is not None:
            result.append({"time": moment, "price": float(price)})
    return sorted(result, key=lambda row: row["time"])


def compressed_times(bars: list[dict[str, Any]]) -> list[int]:
    """実時刻を、昼休みだけ詰めた分単位の横軸へ変換する。"""
    return [compressed_minute_position(bar["time"]) for bar in bars]


def compressed_minute_position(moment: datetime) -> int:
    minutes = (moment.hour - 9) * 60 + moment.minute
    if moment.time() >= MARKET_BREAK_END:
        minutes -= 60
    return minutes


def _time_ticks(axis) -> None:
    axis.set_xticks([position for position, _ in TIME_TICKS])
    axis.set_xticklabels([label for _, label in TIME_TICKS])
    axis.set_xlim(0, compressed_minute_position(datetime.combine(datetime.today(), SESSION_END)))


def _step_prices(bars: list[dict[str, Any]]) -> tuple[list[int], list[float]]:
    return compressed_times(bars), [float(bar["price"]) for bar in bars]


def _clock(moment: datetime | None) -> str:
    return moment.strftime("%H:%M") if moment else "時刻不明"


def _valid_order_price(order: dict[str, Any] | None) -> float | None:
    if not order:
        return None
    try:
        price = float(order.get("price"))
    except (TypeError, ValueError):
        return None
    return price if price > 0 else None


def _order_reason(order: dict[str, Any]) -> str:
    return str(order.get("decision_reason") or order.get("result_code") or "理由記録なし")


def _plot_index_segments(axis, bars: list[dict[str, Any]], values: list[float], **kwargs) -> None:
    current_x: list[int] = []
    current_y: list[float] = []
    afternoon = False
    for bar, value in zip(bars, values):
        is_afternoon = bar["time"].time() >= MARKET_BREAK_END
        if is_afternoon and not afternoon and current_x:
            axis.plot(current_x, current_y, **kwargs)
            current_x, current_y = [], []
        afternoon = is_afternoon
        current_x.append(compressed_minute_position(bar["time"]))
        current_y.append(value)
    if current_x:
        axis.plot(current_x, current_y, **kwargs)


def _event_index(bars: list[dict[str, Any]], moment: datetime | None) -> int | None:
    if not bars or moment is None:
        return None
    previous = [index for index, bar in enumerate(bars) if bar["time"] <= moment]
    return previous[-1] if previous else 0


def _event_marker(
    bars: list[dict[str, Any]], moment: datetime | None
) -> tuple[int, float] | None:
    index = _event_index(bars, moment)
    if index is None or moment is None:
        return None
    return compressed_minute_position(moment), float(bars[index]["price"])


def _filled_step_segments(
    bars: list[dict[str, Any]], start_time: datetime
) -> list[tuple[list[int], list[float]]]:
    """各セッションを1分刻みに展開し、欠落時刻は直前の足で埋める。"""
    selected = [bar for bar in bars if bar["time"] >= start_time]
    segments: list[list[dict[str, Any]]] = [[], []]
    for bar in selected:
        segments[1 if bar["time"].time() >= MARKET_BREAK_END else 0].append(bar)
    result = []
    for session_bars in segments:
        if not session_bars:
            continue
        positions = compressed_times(session_bars)
        prices_by_position = {position: bar["price"] for position, bar in zip(positions, session_bars)}
        values: list[float] = []
        current = float(session_bars[0]["price"])
        for position in range(positions[0], positions[-1] + 1):
            if position in prices_by_position:
                current = float(prices_by_position[position])
            values.append(current)
        result.append((list(range(positions[0], positions[-1] + 1)), values))
    return result


def _add_panel_annotations(axis, lines: list[str]) -> None:
    for index, line in enumerate(lines):
        axis.text(
            0.02,
            0.96 - index * 0.085,
            line,
            transform=axis.transAxes,
            fontsize=8,
            va="top",
            color="#d62728" if "価格が" in line else "black",
            bbox={"facecolor": "white", "edgecolor": "none", "alpha": 0.9, "pad": 1.5},
            zorder=6,
        )


def select_multiplier(level: str, *, stop: bool) -> float:
    values = {
        "NORMAL": (
            config.ATR_STOP_NORMAL_MULTIPLIER
            if stop else config.ATR_PROFIT_LOCK_NORMAL_MULTIPLIER
        ),
        "CAUTION": (
            config.ATR_STOP_CAUTION_MULTIPLIER
            if stop else config.ATR_PROFIT_LOCK_CAUTION_MULTIPLIER
        ),
        "DANGER": (
            config.ATR_STOP_DANGER_MULTIPLIER
            if stop else config.ATR_PROFIT_LOCK_DANGER_MULTIPLIER
        ),
    }
    return values.get(level, values["NORMAL"])


def build_lines(entry: float, atr: float, level: str) -> TradeLines:
    if entry <= 0 or atr <= 0:
        raise ValueError("買値とATRは正数で指定してください")
    return TradeLines(
        entry=entry,
        stop=entry - atr * select_multiplier(level, stop=True),
        atr=atr,
        stop_multiplier=select_multiplier(level, stop=True),
        profit_multiplier=select_multiplier(level, stop=False),
        trigger_multiple=config.ATR_PROFIT_LOCK_TRIGGER_ATR_MULTIPLE,
    )


def calculate_trailing_lines(
    prices: list[float], lines: TradeLines
) -> tuple[list[float | None], list[float], float | None, float | None]:
    """買い後高値を再計算し、トレーリング線と最小余裕を返す。"""
    high = lines.entry
    trailing: list[float | None] = []
    stop_margins: list[float] = []
    trailing_margins: list[float] = []
    for price in prices:
        high = max(high, price)
        stop_margins.append(price - lines.stop)
        if high - lines.entry >= lines.atr * lines.trigger_multiple:
            line = high - lines.atr * lines.profit_multiplier
            trailing.append(line)
            trailing_margins.append(price - line)
        else:
            trailing.append(None)
    return (
        trailing,
        stop_margins,
        min(trailing_margins) if trailing_margins else None,
        min(stop_margins) if stop_margins else None,
    )


def _read_events(path: Path, day: str) -> tuple[list[dict], list[dict]]:
    if not path.is_file():
        return [], []
    buys_sells: list[dict] = []
    skips: list[dict] = []
    with sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True) as connection:
        connection.row_factory = sqlite3.Row
        rows = connection.execute(
            """
            SELECT event_type, occurred_at, symbol, reference_price, quantity,
                   atr, atr_level, stop_multiplier, input_json, rsi,
                   rsi_applied_threshold, same_day_high, same_day_close_price,
                   same_day_last_observed_at, nikkei_intraday_change_percent,
                   nikkei_intraday_as_of
            FROM filter_decision_events
            WHERE execution_mode = 'paper' AND substr(occurred_at, 1, 10) = ?
            ORDER BY occurred_at, id
            """,
            (day,),
        ).fetchall()
    for row in rows:
        item = dict(row)
        if item["event_type"] in {"ATR_DANGER_SKIP", "MARKET_REGIME_DANGER_SKIP",
                                  "MARKET_REGIME_CAUTION_RSI_FILTER"}:
            skips.append(item)
        else:
            buys_sells.append(item)
    return buys_sells, skips


def _read_market_regime(path: Path, day: str) -> str | None:
    if not path.is_file():
        return None
    with sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True) as connection:
        row = connection.execute(
            "SELECT final_regime FROM market_regime_daily "
            "WHERE trade_date = ? AND execution_mode = 'paper'",
            (day,),
        ).fetchone()
    return str(row[0]).upper() if row and row[0] else None


def _report_regime(root: Path, day: str) -> str | None:
    """日別DBに無い日は、日次レポートに保存された市場状態を使う。"""
    report = _json(root / "data" / "reports" / "daily" / f"{day}.json", {})
    regime = (report.get("market_conditions") or {}).get("regime") if isinstance(report, dict) else None
    return str(regime).upper() if regime else None


def _index_bars(root: Path, day: str) -> tuple[list[dict[str, Any]], float | None]:
    path = partition_path(root / "data" / "index_minute_bars_parquet", day, "^N225")
    bars = load_minute_bars(path)
    daily = _json(root / "data" / "cache" / "yahoo_daily" / "^N225.json", {})
    previous = None
    prior = [key for key in daily if key < day]
    if prior:
        try:
            previous = float(daily[max(prior)]["close"])
        except (KeyError, TypeError, ValueError):
            previous = None
    return bars, previous


def _index_baseline_from_events(
    bars: list[dict[str, Any]], events: list[dict[str, Any]]
) -> tuple[float | None, dict[str, Any]]:
    for event in events:
        change = event.get("nikkei_intraday_change_percent")
        as_of = _parse_time(event.get("nikkei_intraday_as_of"))
        if change is None or as_of is None:
            continue
        matching = [bar for bar in bars if bar["time"] <= as_of]
        if not matching:
            continue
        current = matching[-1]["price"]
        baseline = current / (1.0 + float(change) / 100.0)
        actual = (current / baseline - 1.0) * 100.0
        return baseline, {
            "expected": float(change),
            "actual": actual,
            "as_of": as_of.isoformat(timespec="seconds"),
            "matched_bar": matching[-1]["time"].isoformat(timespec="seconds"),
            "matched": abs(actual - float(change)) < 1e-9,
        }
    return None, {"matched": None}


def _skip_reason(skip: dict[str, Any]) -> str:
    event_type = skip.get("event_type")
    if event_type == "MARKET_REGIME_CAUTION_RSI_FILTER":
        rsi = skip.get("rsi")
        threshold = skip.get("rsi_applied_threshold")
        if rsi is not None and threshold is not None:
            return f"RSI{float(rsi):.0f}<{float(threshold):.0f}"
        return "RSIフィルタ"
    if event_type == "ATR_DANGER_SKIP":
        return "ATR危険"
    if event_type == "MARKET_REGIME_DANGER_SKIP":
        return "市場危険"
    return str(event_type or "見送り")


def _reference_skip(item: dict[str, Any]) -> dict[str, Any] | None:
    skips = item["skips"]
    if not skips:
        return None
    skip = min(skips, key=lambda row: row.get("occurred_at") or "")
    reference = skip.get("reference_price")
    moment = _parse_time(skip.get("occurred_at"))
    if reference is None or moment is None:
        return None
    return {"time": moment, "price": float(reference), "reason": _skip_reason(skip), "record": skip}


def _orders(path: Path, day: str) -> list[dict]:
    rows = _json(path, [])
    if not isinstance(rows, list):
        return []
    return [row for row in rows if isinstance(row, dict) and str(row.get("timestamp", "")).startswith(day)]


def _default_day(root: Path) -> str:
    days = {path.stem for path in (root / "data" / "filtering").glob("????-??-??.json")}
    return max(days) if days else datetime.now().date().isoformat()


def _symbols(root: Path, day: str) -> list[str]:
    data = _json(root / "data" / "filtering" / f"{day}.json", {})
    return [str(symbol) for symbol in data.get("symbols", [])][:10]


def minute_inventory(root: Path) -> dict:
    rows = []
    for path in sorted((root / "data" / "minute_bars_parquet").glob("symbol=*/date=*/data.parquet")):
        symbol = path.parts[-3].split("=", 1)[1]
        day = path.parts[-2].split("=", 1)[1]
        rows.append({"symbol": symbol, "date": day, "bar_count": len(load_minute_bars(path))})
    by_symbol: dict[str, dict] = {}
    for row in rows:
        item = by_symbol.setdefault(row["symbol"], {"symbol": row["symbol"], "date_count": 0, "bar_count": 0, "dates": []})
        item["date_count"] += 1
        item["bar_count"] += row["bar_count"]
        item["dates"].append(row["date"])
    return {
        "date_range": [min((row["date"] for row in rows), default=None), max((row["date"] for row in rows), default=None)],
        "partition_count": len(rows),
        "by_symbol": sorted(by_symbol.values(), key=lambda item: item["symbol"]),
    }


def collect(root: Path, day: str) -> dict:
    minute_root = root / "data" / "minute_bars_parquet"
    daily_root = root / "data" / "cache" / "yahoo_daily"
    orders = _orders(root / "data" / "trading" / "order_history.json", day)
    event_orders, skips = _read_events(root / "data" / "state" / "filter_decision_events.sqlite3", day)
    index_bars, index_previous_close = _index_bars(root, day)
    index_baseline, index_validation = _index_baseline_from_events(index_bars, event_orders + skips)
    if index_baseline is not None:
        index_previous_close = index_baseline
    market_regime = _read_market_regime(root / "data" / "state" / "market_regime_daily.sqlite3", day)
    invalid_orders = [
        {
            **row,
            "price_valid": _valid_order_price(row) is not None,
            "order_reason": _order_reason(row),
        }
        for row in orders
        if _valid_order_price(row) is None and str(row.get("side")) in {"1", "2"}
    ]
    event_by_symbol = {str(row["symbol"]): row for row in event_orders}
    result = []
    for symbol in _symbols(root, day):
        bars = load_minute_bars(partition_path(minute_root, day, symbol))
        buy_order = next((row for row in orders if str(row.get("symbol")) == symbol and str(row.get("side")) == "2"), None)
        buy = buy_order or event_by_symbol.get(symbol)
        buy_time = _parse_time((buy or {}).get("timestamp") or (buy or {}).get("occurred_at"))
        entry = _valid_order_price(buy)
        if entry is None and buy is not None:
            entry = _valid_order_price({"price": buy.get("reference_price")})
        recorded_atr = (buy or {}).get("atr")
        atr = float(recorded_atr) if isinstance(recorded_atr, (int, float)) and recorded_atr > 0 else None
        atr_source = "注文記録ATR" if atr else None
        if atr is None:
            daily = _json(daily_root / f"{symbol}.json", {})
            atr = atr_from_daily_cache(daily, day)
            if atr:
                atr_source = "日足キャッシュ再計算ATR"
        level = str((buy or {}).get("atr_level") or "NORMAL").upper()
        after_buy = [bar for bar in bars if buy_time is not None and bar["time"] >= buy_time]
        metric = {"trailing_min_margin": None, "stop_min_margin": None,
                  "trailing_min_margin_atr": None, "stop_min_margin_atr": None}
        line_values: list[float | None] = []
        stop_value: float | None = None
        trailing_time: datetime | None = None
        stop_time: datetime | None = None
        if buy and entry > 0 and atr and after_buy:
            lines = build_lines(entry, atr, level)
            line_values, stop_margins, trailing_margin, stop_margin = calculate_trailing_lines(
                [bar["price"] for bar in after_buy], lines
            )
            stop_value = lines.stop
            if stop_margins:
                stop_time = after_buy[stop_margins.index(stop_margin)]["time"]
            trailing_margins = [
                price - line for price, line in zip(
                    [bar["price"] for bar in after_buy], line_values
                ) if line is not None
            ]
            if trailing_margins:
                target = min(trailing_margins)
                trailing_time = after_buy[
                    next(
                        index for index, (price, line) in enumerate(
                            zip([bar["price"] for bar in after_buy], line_values)
                        ) if line is not None and price - line == target
                    )
                ]["time"]
            metric.update({
                "trailing_min_margin": trailing_margin,
                "stop_min_margin": stop_margin,
                "trailing_min_margin_atr": trailing_margin / atr if trailing_margin is not None else None,
                "stop_min_margin_atr": stop_margin / atr if stop_margin is not None else None,
                "trailing_min_time": trailing_time.isoformat(timespec="minutes") if trailing_time else None,
                "stop_min_time": stop_time.isoformat(timespec="minutes") if stop_time else None,
            })
        skip_reference = _reference_skip({"skips": [row for row in skips if str(row["symbol"]) == symbol]})
        daily_closes, daily_bars = daily_history(_json(daily_root / f"{symbol}.json", {}), day)
        skip_after = (
            [bar for bar in bars if skip_reference and bar["time"] >= skip_reference["time"]]
            if skip_reference else []
        )
        skip_record = skip_reference["record"] if skip_reference else {}
        if skip_reference and skip_after:
            same_day_high = skip_record.get("same_day_high")
            same_day_close = skip_record.get("same_day_close_price")
            same_day_close_time = _parse_time(skip_record.get("same_day_last_observed_at"))
            high = float(same_day_high) if same_day_high is not None else max(bar["price"] for bar in skip_after)
            close = (
                float(same_day_close)
                if same_day_close is not None
                else skip_after[-1]["price"]
            )
            skip_high_delta = high - skip_reference["price"]
            skip_close_delta = close - skip_reference["price"]
            skip_close_time = same_day_close_time or skip_after[-1]["time"]
        else:
            skip_high_delta = None
            skip_close_delta = None
            skip_close_time = None
        result.append({
            "symbol": symbol, "bar_count": len(bars), "bars": bars, "after_buy": after_buy,
            "buy": buy, "skips": [row for row in skips if str(row["symbol"]) == symbol],
            "skip_reference": skip_reference,
            "skip_high_delta": skip_high_delta,
            "skip_close_delta": skip_close_delta,
            "skip_close_time": skip_close_time.isoformat(timespec="minutes") if skip_close_time else None,
            "atr": atr, "atr_source": atr_source, "atr_level": level,
            "daily_closes": daily_closes, "daily_bars": daily_bars,
            "entry": entry or None, "stop": stop_value, "trailing": line_values, **metric,
            "invalid_orders": [row for row in invalid_orders if str(row.get("symbol")) == symbol],
        })
    result.sort(
        key=lambda item: (
            not (item["buy"] is not None and item["entry"] is not None),
            -(item["skip_high_delta"] or float("-inf")),
        )
    )
    return {
        "date": day, "orders": orders, "symbols": result, "minute_inventory": minute_inventory(root),
        "index_bars": index_bars, "index_previous_close": index_previous_close,
        "index_validation": index_validation, "invalid_orders": invalid_orders,
        "market_regime": market_regime,
        "condition_regime": market_regime or _report_regime(root, day),
    }


def _set_font() -> None:
    import matplotlib.pyplot as plt

    plt.rcParams["font.family"] = ["Yu Gothic", "Meiryo", "sans-serif"]


def render(report: dict, output: Path) -> None:
    import matplotlib.pyplot as plt
    from matplotlib.gridspec import GridSpec

    _set_font()
    figure = plt.figure(figsize=(16, 21))
    grid = GridSpec(6, 2, figure=figure, height_ratios=[1, 1, 1, 1, 1, 0.8], hspace=0.65)
    axes = [figure.add_subplot(grid[row, column]) for row in range(5) for column in range(2)]
    for axis, item in zip(axes, report["symbols"]):
        bars = item["bars"]
        bought = item["buy"] is not None and item["entry"] is not None
        low_price = bool(bars) and min(bar["price"] for bar in bars) < 50
        if not bars:
            axis.text(0.5, 0.5, "分足なし", ha="center", va="center", fontsize=14)
            axis.set_title(f"{item['symbol']}  {'買い' if bought else '見送り'}", loc="left")
            axis.axis("off")
            continue
        x, prices = _step_prices(bars)
        line_width = 0.8 if low_price else 1.3
        axis.plot(x, prices, color="#246bce", linewidth=line_width, drawstyle="steps-post", label="株価")
        axis.scatter(x, prices, color="#246bce", s=5, alpha=0.7, label="_nolegend_")
        if bought and item["entry"] is not None:
            start = len(bars) - len(item["after_buy"])
            trade_x = x[start:]
            axis.plot(trade_x, [item["entry"]] * len(trade_x), color="gray", linewidth=0.8, label="買値")
            if item["stop"] is not None:
                axis.plot(trade_x, [item["stop"]] * len(trade_x), color="red", linestyle=":", linewidth=0.9, label="損切り")
            trailing = [None] * start + item["trailing"]
            axis.plot(x, trailing, color="#d5a400", linestyle=":", linewidth=1.0, label="トレーリング")
            buy_time = _parse_time((item["buy"] or {}).get("timestamp") or (item["buy"] or {}).get("occurred_at"))
            buy_marker = _event_marker(bars, buy_time)
            if buy_marker is not None:
                axis.scatter([buy_marker[0]], [buy_marker[1]], color="green", s=18,
                             zorder=3, label="買い")
            sell = next((row for row in report["orders"]
                         if str(row.get("symbol")) == item["symbol"] and str(row.get("side")) == "1"), None)
            if sell:
                sell_time = _parse_time(sell.get("timestamp"))
                sell_marker = _event_marker(bars, sell_time)
                if sell_marker is not None:
                    sell_price = _valid_order_price(sell)
                    if sell_price is not None:
                        axis.scatter([sell_marker[0]], [sell_marker[1]], color="#e67e22", s=18, zorder=3, label="売り")
        reference = item["skip_reference"]
        if reference:
            ref_x = compressed_minute_position(reference["time"])
            after = [(position, price) for position, price, bar in zip(x, prices, bars)
                     if bar["time"] >= reference["time"]]
            if after:
                axis.plot(
                    [ref_x, after[-1][0]], [reference["price"], reference["price"]],
                    color="#777777", linestyle="--", linewidth=0.8, label="参考価格",
                )
                after_x = [position for position, _ in after]
                after_prices = [price for _, price in after]
                for filled_x, filled_prices in _filled_step_segments(bars, reference["time"]):
                    axis.fill_between(
                        filled_x, filled_prices, reference["price"],
                        where=[price >= reference["price"] for price in filled_prices],
                        color="#f39c12", alpha=0.16, step="post",
                        linewidth=0, edgecolor="none", antialiased=False,
                    )
                    axis.fill_between(
                        filled_x, filled_prices, reference["price"],
                        where=[price < reference["price"] for price in filled_prices],
                        color="#2ca25f", alpha=0.14, step="post",
                        linewidth=0, edgecolor="none", antialiased=False,
                    )
                skip_marker = _event_marker(bars, reference["time"])
                if skip_marker is not None:
                    axis.scatter([skip_marker[0]], [skip_marker[1]], color="#777777", s=18, marker="x", label="見送り")
        trailing_text = (
            f"T最小余裕 {item['trailing_min_margin']:.2f}円/{item['trailing_min_margin_atr']:.2f}ATR "
            f"@{_clock(_parse_time(item.get('trailing_min_time')))}"
            if bought and item["trailing_min_margin"] is not None else ""
        )
        stop_text = (
            f"S最小余裕 {item['stop_min_margin']:.2f}円/{item['stop_min_margin_atr']:.2f}ATR "
            f"@{_clock(_parse_time(item.get('stop_min_time')))}"
            if bought and item["stop_min_margin"] is not None else ""
        )
        reason = f" ({reference['reason']})" if reference else ""
        metrics = "  ".join(text for text in (trailing_text, stop_text) if text)
        suffix = f"  {metrics}" if metrics else ""
        axis.set_title(
            f"{item['symbol']}  {'買い' if bought else '見送り'}{reason}  本数:{item['bar_count']}{suffix}",
            loc="left", fontsize=9,
        )
        annotation_lines = []
        if reference:
            annotation_lines.append(
                f"見送り後高値 {item['skip_high_delta']:+.2f}円 / "
                f"引け値 {item['skip_close_delta']:+.2f}円 @{_clock(_parse_time(item.get('skip_close_time')))}"
            )
        invalid_notes = []
        for order in item["invalid_orders"]:
            order_time = _parse_time(order.get("timestamp"))
            marker = _event_marker(bars, order_time)
            if marker is not None:
                axis.scatter(
                    [marker[0]], [marker[1]], color="#d62728", marker="X", s=32,
                    zorder=5, label="注文価格不正",
                )
            side = "売り" if str(order.get("side")) == "1" else "買い"
            raw_price = order.get("price")
            invalid_notes.append(f"{side}価格が{raw_price if raw_price is not None else '欠落'}円で記録")
        if invalid_notes:
            annotation_lines.extend(invalid_notes)
        if low_price:
            annotation_lines.append("1円刻み")
        _add_panel_annotations(axis, annotation_lines)
        _time_ticks(axis)
        axis.grid(alpha=0.2)
    for axis in axes[len(report["symbols"]):]:
        axis.axis("off")
    index_axis = figure.add_subplot(grid[5, :])
    index_bars = report["index_bars"]
    previous_close = report["index_previous_close"]
    if index_bars and previous_close:
        index_x = compressed_times(index_bars)
        index_change = [(bar["price"] - previous_close) / previous_close * 100 for bar in index_bars]
        _plot_index_segments(
            index_axis, index_bars, index_change,
            color="#333333", linewidth=1.0, label="日経225前日比",
        )
        index_axis.axhline(0, color="#888888", linewidth=0.6)
        _time_ticks(index_axis)
        index_axis.set_ylabel("%")
    else:
        index_axis.text(0.5, 0.5, "日経225分足なし", ha="center", va="center")
    regime = report.get("market_regime")
    if regime:
        index_axis.axvspan(0, 330, color=REGIME_COLORS.get(regime, "#eeeeee"), alpha=0.35,
                           label=f"市場状態:{regime}")
    else:
        index_axis.text(0.99, 0.82, "市場状態の記録なし", transform=index_axis.transAxes,
                        ha="right", fontsize=9)
    index_axis.set_title("共通: 日経225前日比 / 朝決定の市場状態", loc="left", fontsize=10)
    index_axis.grid(alpha=0.2)
    handles = []
    labels = []
    for axis in axes + [index_axis]:
        axis_handles, axis_labels = axis.get_legend_handles_labels()
        for handle, label in zip(axis_handles, axis_labels):
            if label != "_nolegend_" and label not in labels:
                handles.append(handle)
                labels.append(label)
    if handles:
        figure.legend(handles, labels, loc="upper center", bbox_to_anchor=(0.5, 0.995),
                     ncol=6, fontsize=8)
    figure.suptitle(f"分足トレード診断 {report['date']}", fontsize=16, y=0.94)
    figure.subplots_adjust(top=0.88, bottom=0.07, left=0.05, right=0.98)
    output.parent.mkdir(parents=True, exist_ok=True)
    figure.savefig(output, dpi=150)
    plt.close(figure)


def _render_detail(report: dict, item: dict, output: Path) -> None:
    import matplotlib.pyplot as plt

    _set_font()
    figure, axis = plt.subplots(figsize=(16, 8))
    bars = item["bars"]
    x, prices = _step_prices(bars)
    low_price = min(prices) < 50
    axis.plot(x, prices, color="#246bce", linewidth=0.8 if low_price else 1.5,
              drawstyle="steps-post", label="株価")
    axis.scatter(x, prices, color="#246bce", s=8, alpha=0.7, label="_nolegend_")
    bought = item["buy"] is not None and item["entry"] is not None
    if bought:
        start = len(bars) - len(item["after_buy"])
        trade_x = x[start:]
        axis.plot(trade_x, [item["entry"]] * len(trade_x), color="gray", linewidth=1, label="買値")
        if item["stop"] is not None:
            axis.plot(trade_x, [item["stop"]] * len(trade_x), color="red", linestyle=":", label="損切り")
        axis.plot(x, [None] * start + item["trailing"], color="#d5a400",
                  linestyle=":", linewidth=1.2, label="トレーリング")
        buy_time = _parse_time((item["buy"] or {}).get("timestamp") or (item["buy"] or {}).get("occurred_at"))
        buy_marker = _event_marker(bars, buy_time)
        if buy_marker is not None:
            axis.scatter([buy_marker[0]], [buy_marker[1]], color="green", s=35, label="買い", zorder=3)
        sell = next((row for row in report["orders"]
                     if str(row.get("symbol")) == item["symbol"] and str(row.get("side")) == "1"), None)
        if sell:
            sell_time = _parse_time(sell.get("timestamp"))
            sell_marker = _event_marker(bars, sell_time)
            sell_price = _valid_order_price(sell)
            if sell_marker is not None and sell_price is not None:
                axis.scatter([sell_marker[0]], [sell_marker[1]], color="#e67e22", s=35, label="売り", zorder=3)
    reference = item["skip_reference"]
    if reference:
        after = [(position, price) for position, price, bar in zip(x, prices, bars)
                 if bar["time"] >= reference["time"]]
        after_x = [position for position, _ in after]
        after_prices = [price for _, price in after]
        if after:
            axis.plot(
                [compressed_minute_position(reference["time"]), after_x[-1]],
                [reference["price"], reference["price"]],
                color="#777777", linestyle="--", label="参考価格",
            )
        for filled_x, filled_prices in _filled_step_segments(bars, reference["time"]):
            axis.fill_between(
                filled_x, filled_prices, reference["price"],
                where=[price >= reference["price"] for price in filled_prices],
                color="#f39c12", alpha=0.16, step="post",
                linewidth=0, edgecolor="none", antialiased=False,
            )
            axis.fill_between(
                filled_x, filled_prices, reference["price"],
                where=[price < reference["price"] for price in filled_prices],
                color="#2ca25f", alpha=0.14, step="post",
                linewidth=0, edgecolor="none", antialiased=False,
            )
        if after:
            skip_marker = _event_marker(bars, reference["time"])
            if skip_marker is not None:
                axis.scatter([skip_marker[0]], [skip_marker[1]], color="#777777", s=35, marker="x", label="見送り")
    metrics = []
    if bought and item["trailing_min_margin"] is not None:
        metrics.append(
            f"T最小余裕 {item['trailing_min_margin']:.2f}円/{item['trailing_min_margin_atr']:.2f}ATR "
            f"@{_clock(_parse_time(item.get('trailing_min_time')))}"
        )
    if bought and item["stop_min_margin"] is not None:
        metrics.append(
            f"S最小余裕 {item['stop_min_margin']:.2f}円/{item['stop_min_margin_atr']:.2f}ATR "
            f"@{_clock(_parse_time(item.get('stop_min_time')))}"
        )
    reason = f" ({reference['reason']})" if reference else ""
    axis.set_title(
        f"{item['symbol']} {'買い' if bought else '見送り'}{reason} 本数:{item['bar_count']}  "
        + "  ".join(metrics),
        loc="left",
    )
    _time_ticks(axis)
    annotation_lines = []
    high_text = (
        f"見送り後高値 {item['skip_high_delta']:+.2f}円 / "
        f"引け値 {item['skip_close_delta']:+.2f}円 @{_clock(_parse_time(item.get('skip_close_time')))}"
        if reference and item["skip_high_delta"] is not None else ""
    )
    if high_text:
        annotation_lines.append(high_text)
    invalid_notes = []
    for order in item["invalid_orders"]:
        order_time = _parse_time(order.get("timestamp"))
        marker = _event_marker(bars, order_time)
        if marker is not None:
            axis.scatter([marker[0]], [marker[1]], color="#d62728", marker="X", s=45,
                         zorder=5, label="注文価格不正")
        side = "売り" if str(order.get("side")) == "1" else "買い"
        raw_price = order.get("price")
        invalid_notes.append(f"{side}価格が{raw_price if raw_price is not None else '欠落'}円で記録")
    if invalid_notes:
        annotation_lines.extend(invalid_notes)
    if low_price:
        annotation_lines.append("1円刻み")
    _add_panel_annotations(axis, annotation_lines)
    axis.grid(alpha=0.2)
    handles, labels = axis.get_legend_handles_labels()
    if handles:
        figure.legend(handles, labels, loc="upper center", bbox_to_anchor=(0.5, 0.94),
                     ncol=6, fontsize=9)
    figure.suptitle(f"分足トレード診断 詳細 {report['date']}", y=0.98)
    figure.text(0.01, 0.01, "買い後高値は分足から再計算した代用値です。", fontsize=9)
    figure.tight_layout(rect=(0.01, 0.04, 0.99, 0.87))
    output.parent.mkdir(parents=True, exist_ok=True)
    figure.savefig(output, dpi=160)
    plt.close(figure)


def render_detail_pages(report: dict, output_base: Path) -> list[Path]:
    bought = [item for item in report["symbols"] if item["buy"] is not None and item["bars"]]
    skipped = sorted(
        (item for item in report["symbols"] if item["skip_high_delta"] is not None and item["skip_high_delta"] > 0),
        key=lambda item: item["skip_high_delta"],
        reverse=True,
    )[:3]
    selected = []
    seen: set[str] = set()
    for item in bought + skipped:
        if item["symbol"] not in seen:
            selected.append(item)
            seen.add(item["symbol"])
    paths = []
    for item in selected:
        path = output_base.with_name(f"{output_base.name}_detail_{item['symbol']}").with_suffix(".png")
        _render_detail(report, item, path)
        paths.append(path)
    return paths


def write_outputs(report: dict, output_base: Path) -> None:
    output_base.parent.mkdir(parents=True, exist_ok=True)
    serializable = {
        "date": report["date"],
        "orders": report["orders"],
        "minute_inventory": report["minute_inventory"],
        "market_regime": report.get("market_regime"),
        "index_validation": report.get("index_validation"),
        "invalid_orders": report.get("invalid_orders", []),
        "symbols": [
            {
                key: value for key, value in item.items()
                if key not in {"bars", "after_buy", "trailing", "skip_reference", "daily_closes", "daily_bars"}
            }
            for item in report["symbols"]
        ],
    }
    output_base.with_suffix(".json").write_text(json.dumps(serializable, ensure_ascii=False, indent=2), encoding="utf-8")
    import csv

    fields = ["symbol", "bar_count", "entry", "atr", "atr_source", "stop", "trailing_min_margin",
              "trailing_min_margin_atr", "stop_min_margin", "stop_min_margin_atr"]
    with output_base.with_suffix(".csv").open("w", newline="", encoding="utf-8-sig") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        writer.writerows({field: item.get(field) for field in fields} for item in report["symbols"])
    render(report, output_base.with_suffix(".png"))
    detail_paths = render_detail_pages(report, output_base)
    missing = [item["symbol"] for item in report["symbols"] if not item["bars"]]
    sparse: list[str] = []
    report_lines = [
        "要は何がわかったか:",
        f"- {report['date']} の監視10銘柄について、買い/見送りとラインへの最小余裕を比較できる。",
        f"- 分足なし {len(missing)}銘柄、極端に少ない分足 {len(sparse)}銘柄。",
        "- 買い後高値は分足から再計算した代用値であり、本番の実際の高値との差があり得る。",
        f"- 市場状態: {report.get('market_regime') or '記録なし'}。",
        f"- 日経前日比のイベント記録照合: {report.get('index_validation')}.",
        f"- 詳細図: {', '.join(path.name for path in detail_paths) or '該当なし'}。",
        "",
        "## データ棚卸し",
        f"- 分足Parquetの日付範囲: {report['minute_inventory']['date_range'][0]} ～ {report['minute_inventory']['date_range'][1]}、"
        f"パーティション {report['minute_inventory']['partition_count']}件。",
        "- 銘柄ごとの分足本数は付属JSONの `minute_inventory.by_symbol` に記載。",
        f"- 指定日の注文履歴: BUY {sum(str(row.get('side')) == '2' for row in report['orders'])}件、"
        f"SELL {sum(str(row.get('side')) == '1' for row in report['orders'])}件（時刻・価格・ATRをJSONに保存）。",
        "",
        "## 注意点",
        f"- 高値を再計算で代用した銘柄: {', '.join(item['symbol'] for item in report['symbols'] if item['after_buy']) or 'なし'}",
        f"- ATRを再計算で代用した銘柄: {', '.join(item['symbol'] for item in report['symbols'] if item['atr_source'] == '日足キャッシュ再計算ATR') or 'なし'}",
        f"- 分足の本数が極端に少ない銘柄: {', '.join(sparse) or 'なし'}",
    ]
    invalid_4766_4597 = [
        order for order in report.get("invalid_orders", [])
        if str(order.get("symbol")) in {"4766", "4597"}
    ]
    if invalid_4766_4597:
        report_lines.extend([
            "",
            "## 2026-10-02 売り価格異常の調査記録",
            *[
                f"- {order['symbol']} 売り価格={order.get('price')!r}円、時刻={order.get('timestamp')!r}、理由={order.get('order_reason')}"
                for order in invalid_4766_4597
            ],
        ])
    output_base.with_suffix(".md").write_text("\n".join(report_lines) + "\n", encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-root", type=Path, default=ROOT)
    parser.add_argument("--date", default=None)
    parser.add_argument("--output-base", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args(argv)
    root = args.project_root.resolve()
    output = args.output_base if args.output_base.is_absolute() else root / args.output_base
    report = collect(root, args.date or _default_day(root))
    write_outputs(report, output)
    print(f"Outputs: {output.with_suffix('.png')}, {output.with_suffix('.csv')}, {output.with_suffix('.json')}, {output.with_suffix('.md')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
