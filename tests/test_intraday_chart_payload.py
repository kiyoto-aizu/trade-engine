import json
from datetime import datetime

from scripts.analysis import intraday_chart_payload as payload_module


def _bars(prices, start_minute=0):
    return [
        {"time": datetime(2026, 10, 6, 9 + (start_minute + index) // 60, (start_minute + index) % 60), "price": price}
        for index, price in enumerate(prices)
    ]


def _item(symbol, *, bars=None, buy=None, entry=None, skips=None, atr=None, level="NORMAL", invalid=None):
    return {
        "symbol": symbol, "bars": bars or [], "buy": buy, "entry": entry,
        "skips": skips or [], "atr": atr, "atr_level": level,
        "invalid_orders": invalid or [],
    }


def _report(items, orders=None, **extra):
    return {
        "date": "2026-10-06", "orders": orders or [], "symbols": items,
        "index_bars": [], "index_previous_close": None, "market_regime": None, **extra,
    }


def _order(symbol, side, minute, price, reason="テスト理由", qty=100):
    return {
        "symbol": symbol, "side": side, "price": price, "qty": qty,
        "timestamp": f"2026-10-06T09:{minute:02d}:30", "decision_reason": reason,
        "order_id": "paper-1", "atr": 2.0,
    }


def test_bought_symbol_has_lines_markers_margins_and_pnl():
    bars = _bars([100, 101, 104, 110, 108, 107, 105])
    buy = _order("1111", "2", 1, 101.0)
    sell = _order("1111", "1", 5, 107.0)
    report = _report([_item("1111", bars=bars, buy=buy, entry=101.0, atr=2.0)], [buy, sell])

    data = payload_module.build_chart_payload(report)
    symbol = data["symbols"][0]

    assert symbol["result"] == "buy"
    assert symbol["buy"] == {"time": "09:01:30", "price": 101.0, "price_missing": False, "reason": "テスト理由"}
    assert symbol["sells"][0]["price"] == 107.0
    assert symbol["entry"] == 101.0
    assert symbol["stop"] == 101.0 - 2.0 * payload_module.analysis.select_multiplier("NORMAL", stop=True)
    assert symbol["stop_from"] == "09:01"
    assert symbol["pnl"] == 600
    assert symbol["min_margin"]["stop"]["time"] == "09:02"
    assert symbol["min_margin"]["trailing"] is not None
    assert symbol["trailing"][-1][0] == "09:05"
    assert [row[0] for row in symbol["bars"]][0] == "09:00"
    assert symbol["price_missing"] is False


def test_skip_only_symbol_has_skip_markers_and_no_trade_lines():
    skip = {"event_type": "ATR_DANGER_SKIP", "occurred_at": "2026-10-06T09:02:10", "reference_price": 100.0}
    report = _report([_item("2222", bars=_bars([100, 99, 98]), skips=[skip])])

    symbol = payload_module.build_chart_payload(report)["symbols"][0]

    assert symbol["result"] == "skip"
    assert symbol["skips"] == [{"time": "09:02:10", "price": 100.0, "reason": "ATR危険"}]
    assert symbol["buy"] is None and symbol["stop"] is None and symbol["min_margin"] is None
    assert symbol["trailing"] == []


def test_symbol_without_trade_or_skip_has_no_trade_data():
    report = _report([_item("3333", bars=_bars([50, 51]))])

    symbol = payload_module.build_chart_payload(report)["symbols"][0]

    assert symbol["result"] == "none"
    assert symbol["buy"] is None and symbol["min_margin"] is None and symbol["pnl"] is None


def test_symbols_are_ordered_bought_then_skipped_then_none():
    skip = {"event_type": "ATR_DANGER_SKIP", "occurred_at": "2026-10-06T09:02:10", "reference_price": 10.0}
    buy = _order("5555", "2", 1, 100.0)
    report = _report(
        [
            _item("3333", bars=_bars([50, 51])),
            _item("4444", bars=_bars([10, 11]), skips=[skip]),
            _item("5555", bars=_bars([100, 101]), buy=buy, entry=100.0, atr=2.0),
        ],
        [buy],
    )

    symbols = payload_module.build_chart_payload(report)["symbols"]

    assert [row["symbol"] for row in symbols] == ["5555", "4444", "3333"]


def test_zero_yen_sell_has_no_price_and_is_flagged_as_missing():
    bars = _bars([100, 101, 102, 103])
    buy = _order("6666", "2", 0, 100.0)
    sell = _order("6666", "1", 3, 0.0)
    report = _report(
        [_item("6666", bars=bars, buy=buy, entry=100.0, atr=2.0, invalid=[{**sell, "price_valid": False}])],
        [buy, sell],
    )

    symbol = payload_module.build_chart_payload(report)["symbols"][0]

    assert symbol["sells"] == [
        {"time": "09:03:30", "price": None, "price_missing": True, "reason": "テスト理由"}
    ]
    assert symbol["price_missing"] is True
    assert symbol["pnl"] is None


def test_missing_market_regime_is_null_and_index_is_optional():
    report = _report([_item("3333", bars=_bars([50]))])

    data = payload_module.build_chart_payload(report)

    assert data["market_regime"] is None
    assert data["index"] is None


def test_index_and_regime_are_exported_when_recorded():
    report = _report(
        [], index_bars=_bars([39000, 39390]), index_previous_close=39000.0, market_regime="DANGER"
    )

    data = payload_module.build_chart_payload(report)

    assert data["market_regime"] == "DANGER"
    assert data["index"]["points"] == [["09:00", 0.0], ["09:01", 1.0]]


def test_earliest_minimum_prefers_the_earliest_time_on_ties():
    times = _bars([0, 0, 0, 0])
    times = [bar["time"] for bar in times]

    assert payload_module.earliest_minimum(times, [3.0, 1.0, 2.0, 1.0]) == (1.0, times[1])
    assert payload_module.earliest_minimum([], []) is None


def test_unsafe_reason_labels_are_replaced():
    assert payload_module._label("C:\\Users\\kai\\secret.log") == "理由記録あり"
    assert payload_module._label("x" * 80) == "理由記録あり"
    assert payload_module._label(None) == "理由記録なし"
    assert payload_module._label("上側バンド突破") == "上側バンド突破"


def test_payload_is_json_serializable_and_has_only_allowed_keys(tmp_path):
    buy = _order("1111", "2", 1, 101.0)
    report = _report([_item("1111", bars=_bars([100, 101, 102]), buy=buy, entry=101.0, atr=2.0)], [buy])

    path = payload_module.write_chart_payload(report, tmp_path / "out" / "2026-10-06.json")
    text = path.read_text(encoding="utf-8")
    data = json.loads(text)

    assert set(data) == {"version", "date", "market_regime", "index", "symbols"}
    assert "paper-1" not in text and "order_id" not in text


# --- 買いなし銘柄の買い条件・仮の売りライン ---------------------------------------------

from src.domain.volatility import DailyBar  # noqa: E402


def _daily(closes, spread=2.0):
    rows = {}
    for index, close in enumerate(closes):
        rows[f"2026-09-{index + 1:02d}"] = {"high": close + spread / 2, "low": close - spread / 2, "close": close}
    return payload_module.entry_condition.daily_history(rows, "2026-10-06")


def _checked_item(symbol, prices, closes, skips=None, spread=2.0, minute=0):
    closes_list, bars = _daily(closes, spread)
    item = _item(symbol, bars=_bars(prices, minute), skips=skips)
    item["daily_closes"], item["daily_bars"] = closes_list, bars
    return item


def _check(item, **extra):
    data = payload_module.build_chart_payload(_report([item], condition_regime="CAUTION", **extra), details=True)
    return data["symbols"][0]["entry_check"]


RISING = [100 + index for index in range(30)]
FALLING = [130 - index for index in range(30)]


def test_pattern1_rsi_ok_reports_closest_approach_to_upper_band():
    band = payload_module.entry_condition.calculate_price_limit(RISING).upper_band
    prices = [band - 5, band - 1.5, band - 3]
    check = _check(_checked_item("1111", prices, RISING))

    assert check["pattern"] == 1
    assert check["upper_band"] == band
    assert check["gap"] == {"value": 1.5, "time": "09:01", "reached": False}
    assert check["rsi_ok"] is True
    assert check["details"]["rsi_threshold"] == 60.0
    assert check["details"]["source"] == {"upper_band": "recomputed", "rsi": "recomputed", "rsi_threshold": "recomputed"}


def test_pattern2_rsi_short_has_no_gap():
    check = _check(_checked_item("2222", [100, 101], FALLING))

    assert check["pattern"] == 2
    assert check["rsi_ok"] is False and check["gap"] is None
    assert check["details"]["rsi_short"] == 60.0 - check["details"]["rsi"]


def test_recorded_rsi_is_preferred_and_marked_as_recorded():
    skip = {"event_type": "MARKET_REGIME_CAUTION_RSI_FILTER", "occurred_at": "2026-10-06T09:01:00",
            "reference_price": 100.0, "rsi": 58.0, "rsi_applied_threshold": 60.0}
    check = _check(_checked_item("3333", [100, 101], RISING, skips=[skip]))

    assert check["pattern"] == 2
    assert check["details"]["rsi"] == 58.0
    assert check["details"]["source"]["rsi"] == "recorded"
    assert check["details"]["source"]["upper_band"] == "recomputed"


def test_unavailable_when_history_or_regime_is_missing():
    short = _check(_checked_item("4444", [100], [100, 101, 102]))
    assert short["pattern"] is None and "日足不足" in short["unavailable"]

    item = _checked_item("4444", [100], RISING)
    data = payload_module.build_chart_payload(_report([item]), details=True)
    assert data["symbols"][0]["entry_check"] == {"pattern": None, "unavailable": "市場状態の記録なし"}


def test_pattern3_hypothetical_line_matches_production_atr_calculation_including_step_down():
    closes = [100.0] * 20
    skips = [
        {"event_type": "ATR_DANGER_SKIP", "occurred_at": "2026-10-06T09:00:10", "reference_price": 100.0},
        {"event_type": "MARKET_REGIME_DANGER_SKIP", "occurred_at": "2026-10-06T09:03:10", "reference_price": 102.0},
    ]
    check = _check(_checked_item("5555", [100, 101, 103, 102, 98], closes, skips=skips))

    # ATR=2(NORMAL)。損切り1.5倍→含み益がATR×0.5以上で利確2.5倍に切り替わり、線が一度下がる
    assert check["pattern"] == 3
    assert check["skip"] == {"time": "09:00:10", "price": 100.0}
    assert check["reason"] == "ATR危険"
    assert check["stop"] == 100.0 - 2.0 * 1.5
    assert check["line"] == [["09:00", 97.0], ["09:01", 96.0], ["09:02", 98.0], ["09:04", 98.0]]
    assert check["min_margin"] == {"value": 0.0, "time": "09:04"}
    assert check["exit"] == {"time": "09:04", "price": 98.0}
    assert check["details"]["atr"] == 2.0 and check["details"]["final_multiplier"] == 2.5


def test_pattern3_uses_recorded_atr_when_daily_history_is_short():
    skip = {"event_type": "ATR_DANGER_SKIP", "occurred_at": "2026-10-06T09:00:00",
            "reference_price": 100.0, "atr": 4.0, "atr_level": "DANGER"}
    item = _checked_item("6666", [100, 101], [100, 101], skips=[skip])

    check = _check(item)

    assert check["details"]["atr_source"] == "recorded"
    assert check["stop"] == 100.0 - 4.0 * 0.7


def test_pattern3_unavailable_without_atr():
    skip = {"event_type": "ATR_DANGER_SKIP", "occurred_at": "2026-10-06T09:00:00", "reference_price": 100.0}

    check = _check(_checked_item("7777", [100, 101], [100, 101], skips=[skip]))

    assert check == {"pattern": None, "unavailable": "ATR算出不可(日足不足)"}


def test_bought_symbol_has_no_entry_check():
    buy = _order("1111", "2", 1, 101.0)
    report = _report([_item("1111", bars=_bars([100, 101]), buy=buy, entry=101.0, atr=2.0)], [buy])

    assert payload_module.build_chart_payload(report)["symbols"][0]["entry_check"] is None


def test_private_mode_removes_threshold_and_multiplier_numbers():
    closes = [100.0] * 20
    skip = {"event_type": "ATR_DANGER_SKIP", "occurred_at": "2026-10-06T09:00:10", "reference_price": 100.0}
    items = [
        _checked_item("5555", [100, 101, 103], closes, skips=[skip]),
        _checked_item("2222", [100, 101], FALLING),
    ]
    report = _report(items, condition_regime="CAUTION")

    data = payload_module.build_chart_payload(report, details=False)
    text = json.dumps(data, ensure_ascii=False)

    for row in data["symbols"]:
        assert "details" not in row["entry_check"]
    for key in ("rsi_threshold", "rsi_short", "atr", "stop_multiplier", "final_multiplier", "atr_level"):
        assert f'"{key}"' not in text
    assert "line" in next(row for row in data["symbols"] if row["symbol"] == "5555")["entry_check"]


def test_details_setting_defaults_to_public_and_can_be_turned_off(monkeypatch):
    monkeypatch.delenv(payload_module.CONDITION_DETAILS_ENV, raising=False)
    assert payload_module.condition_details_enabled() is True
    monkeypatch.setenv(payload_module.CONDITION_DETAILS_ENV, "false")
    assert payload_module.condition_details_enabled() is False


def test_payload_with_entry_checks_stays_within_publication_validation(tmp_path):
    from src.infrastructure import report_publication as publication

    closes = [100.0] * 20
    skip = {"event_type": "ATR_DANGER_SKIP", "occurred_at": "2026-10-06T09:00:10", "reference_price": 100.0}
    report = _report([_checked_item("5555", [100, 101, 103], closes, skips=[skip])], condition_regime="CAUTION")
    payload_module.write_chart_payload(report, tmp_path / publication.CHART_DATA_NAME)

    assert publication.public_chart_data(tmp_path, datetime(2026, 10, 6).date()) is not None

