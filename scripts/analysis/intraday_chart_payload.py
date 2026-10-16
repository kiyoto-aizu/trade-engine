"""分足トレード診断の描画用JSONを作る。

集計は plot_intraday_trade_diagnostics.collect の結果を再利用し、計算を二重に持たない。
公開リポジトリへ載せるため、銘柄コード・価格・時刻・損益・理由ラベルだけを出力する。
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from scripts.analysis import plot_intraday_trade_diagnostics as analysis

PAYLOAD_VERSION = 1
_UNSAFE_LABEL_CHARACTERS = frozenset("\\/:@")
_MAX_LABEL_LENGTH = 40


def earliest_minimum(times: list[datetime], values: list[float]) -> tuple[float, datetime] | None:
    """最小値とその時刻を返す。同値が複数ある場合は最も早い時刻を採る。"""
    if not values or len(times) != len(values):
        return None
    index = min(range(len(values)), key=lambda position: (values[position], position))
    return values[index], times[index]


def _label(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return "理由記録なし"
    if len(text) > _MAX_LABEL_LENGTH or any(char in _UNSAFE_LABEL_CHARACTERS for char in text):
        return "理由記録あり"
    return text


def _hhmm(moment: datetime) -> str:
    return moment.strftime("%H:%M")


def _hhmmss(moment: datetime) -> str:
    return moment.strftime("%H:%M:%S")


def _round(value: float) -> float:
    return round(float(value), 4)


def _margin(found: tuple[float, datetime] | None) -> dict | None:
    if found is None:
        return None
    return {"value": _round(found[0]), "time": _hhmm(found[1])}


def _step_points(times: list[datetime], values: list[float | None]) -> list[list]:
    """値が変わる点だけを残す。最後の足も残し、階段線を終端まで伸ばせるようにする。"""
    points: list[list] = []
    previous: float | None = None
    last_index = len(times) - 1
    for index, (moment, value) in enumerate(zip(times, values)):
        if value is None:
            continue
        if value != previous or index == last_index:
            points.append([_hhmm(moment), _round(value)])
            previous = value
    return points


def _symbol_orders(report: dict, symbol: str, side: str) -> list[dict]:
    rows = [
        row for row in report.get("orders", [])
        if str(row.get("symbol")) == symbol and str(row.get("side")) == side
    ]
    return sorted(rows, key=lambda row: str(row.get("timestamp") or ""))


def _order_payload(order: dict) -> dict | None:
    moment = analysis._parse_time(order.get("timestamp"))
    if moment is None:
        return None
    price = analysis._valid_order_price(order)
    return {
        "time": _hhmmss(moment),
        "price": _round(price) if price is not None else None,
        "price_missing": price is None,
        "reason": _label(order.get("decision_reason") or order.get("result_code")),
    }


def _symbol_payload(report: dict, item: dict) -> dict:
    symbol = str(item["symbol"])
    bars = item["bars"]
    bought = item["buy"] is not None and item["entry"] is not None
    prices = [bar["price"] for bar in bars]
    payload: dict[str, Any] = {
        "symbol": symbol,
        "result": "buy" if bought else ("skip" if item["skips"] else "none"),
        "low": _round(min(prices)) if prices else None,
        "high": _round(max(prices)) if prices else None,
        "bars": [[_hhmm(bar["time"]), _round(bar["price"])] for bar in bars],
        "buy": None,
        "sells": [],
        "skips": [],
        "entry": None,
        "stop": None,
        "stop_from": None,
        "trailing": [],
        "min_margin": None,
        "pnl": None,
        "price_missing": bool(item.get("invalid_orders")),
    }
    for skip in sorted(item["skips"], key=lambda row: str(row.get("occurred_at") or "")):
        moment = analysis._parse_time(skip.get("occurred_at"))
        if moment is None:
            continue
        reference = skip.get("reference_price")
        payload["skips"].append({
            "time": _hhmmss(moment),
            "price": _round(reference) if isinstance(reference, (int, float)) and reference > 0 else None,
            "reason": _label(analysis._skip_reason(skip)),
        })
    if not bought:
        return payload

    buy_orders = _symbol_orders(report, symbol, "2")
    buy_source = buy_orders[0] if buy_orders else {
        "timestamp": item["buy"].get("occurred_at"),
        "price": item["buy"].get("reference_price"),
        "decision_reason": item["buy"].get("event_type"),
    }
    buy = _order_payload(buy_source)
    buy_time = analysis._parse_time(buy_source.get("timestamp"))
    sells = [sell for sell in map(_order_payload, _symbol_orders(report, symbol, "1")) if sell]
    sell_times = [analysis._parse_time(row.get("timestamp")) for row in _symbol_orders(report, symbol, "1")]
    sell_times = [moment for moment in sell_times if moment is not None]
    hold_end = min(sell_times) if sell_times else None
    entry = float(item["entry"])
    payload.update({"buy": buy, "sells": sells, "entry": _round(entry)})

    holding = [
        bar for bar in bars
        if buy_time is not None and bar["time"] >= buy_time and (hold_end is None or bar["time"] <= hold_end)
    ]
    atr = item.get("atr")
    if holding and atr:
        lines = analysis.build_lines(entry, atr, item.get("atr_level") or "NORMAL")
        hold_prices = [bar["price"] for bar in holding]
        hold_times = [bar["time"] for bar in holding]
        trailing, stop_margins, _, _ = analysis.calculate_trailing_lines(hold_prices, lines)
        trailing_pairs = [
            (moment, price - line)
            for moment, price, line in zip(hold_times, hold_prices, trailing)
            if line is not None
        ]
        payload.update({
            "stop": _round(lines.stop),
            "stop_from": _hhmm(buy_time),
            "trailing": _step_points(hold_times, trailing),
            "min_margin": {
                "trailing": _margin(earliest_minimum(
                    [pair[0] for pair in trailing_pairs], [pair[1] for pair in trailing_pairs]
                )),
                "stop": _margin(earliest_minimum(hold_times, stop_margins)),
            },
        })
    valid_sells = [sell for sell in sells if sell["price"] is not None]
    if valid_sells:
        quantity = next(
            (row.get("qty") for row in _symbol_orders(report, symbol, "1") if isinstance(row.get("qty"), (int, float))),
            None,
        ) or next(
            (row.get("qty") for row in buy_orders if isinstance(row.get("qty"), (int, float))),
            None,
        )
        if quantity:
            payload["pnl"] = round((valid_sells[0]["price"] - entry) * quantity)
    if (buy and buy["price_missing"]) or any(sell["price_missing"] for sell in sells):
        payload["price_missing"] = True
    return payload


def _index_payload(report: dict) -> dict | None:
    bars = report.get("index_bars") or []
    previous_close = report.get("index_previous_close")
    if not bars or not previous_close:
        return None
    return {
        "points": [
            [_hhmm(bar["time"]), round((bar["price"] - previous_close) / previous_close * 100, 3)]
            for bar in bars
        ]
    }


def build_chart_payload(report: dict) -> dict:
    """collect()の結果から、買いあり→見送りあり→買いなしの順に並べた描画用データを作る。"""
    order = {"buy": 0, "skip": 1, "none": 2}
    symbols = [_symbol_payload(report, item) for item in report["symbols"]]
    symbols.sort(key=lambda row: order[row["result"]])
    return {
        "version": PAYLOAD_VERSION,
        "date": report["date"],
        "market_regime": report.get("market_regime") or None,
        "index": _index_payload(report),
        "symbols": symbols,
    }


def write_chart_payload(report: dict, path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(build_chart_payload(report), ensure_ascii=False, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    return path
