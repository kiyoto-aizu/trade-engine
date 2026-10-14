import json

import pytest

from scripts.analysis.analyze_price_band_trend_days import (
    band_of,
    candidate_symbols,
    collect_observations,
    inventory,
    is_trend_day_a,
    load_cache,
    summarize_bands,
    trend_day_b,
)


def test_band_of_boundaries():
    assert band_of(270) == "270以下"
    assert band_of(270.5) == "270-450"
    assert band_of(450) == "270-450"
    assert band_of(900) == "450-900"
    assert band_of(900.1) is None


def test_trend_day_a_requires_move_and_close_position():
    assert is_trend_day_a(100, 111, 99, 110, atr=10) == "up"
    assert is_trend_day_a(110, 111, 99, 100, atr=10) == "down"
    assert is_trend_day_a(100, 111, 99, 109, atr=10) is None  # 動きがATR未満
    assert is_trend_day_a(100, 130, 99, 110, atr=10) is None  # 上位25%に引けていない
    assert is_trend_day_a(100, 100, 100, 100, atr=10) is None


def test_trend_day_b_three_consecutive_moves():
    assert trend_day_b([1, 2, 3, 4], 3) == "up"
    assert trend_day_b([4, 3, 2, 1], 3) == "down"
    assert trend_day_b([1, 2, 2, 3], 3) is None
    assert trend_day_b([1, 2, 3, 4], 2) is None


def _write_cache(tmp_path, symbol, closes):
    data = {f"2026-01-{i + 1:02d}": {"open": c, "high": c + 1, "low": c - 1, "close": c, "volume": 1000.0}
            for i, c in enumerate(closes)}
    (tmp_path / f"{symbol}.json").write_text(json.dumps(data), encoding="utf-8")


def test_inventory_and_summary(tmp_path):
    _write_cache(tmp_path, "1111", [100 + i for i in range(20)])
    _write_cache(tmp_path, "2222", [500 + i for i in range(20)])
    cache = load_cache(tmp_path)
    inv = inventory(cache)
    assert inv["symbols"] == 2
    assert inv["band_symbol_counts"] == {"270以下": 1, "270-450": 0, "450-900": 1}
    summary = {s["band"]: s for s in summarize_bands(collect_observations(cache))}
    low, high = summary["270以下"], summary["450-900"]
    assert low["symbols"] == 1 and high["symbols"] == 1
    assert low["b_up"] == low["b_trend"] == low["b_days"] == 17
    assert low["a_days"] == 5  # 15本目以降のみATRが使える
    assert summary["270-450"]["a_ratio"] is None
    assert high["mean_atr_pct"] == pytest.approx(high["mean_atr_yen"] / 510 * 100, rel=0.05)


def test_candidate_symbols_excludes_nothing_but_flags_cache(tmp_path):
    band = tmp_path / "bands" / "450"
    band.mkdir(parents=True)
    (band / "2026-10-05.json").write_text(json.dumps({"date": "2026-10-05", "symbols": ["1111", "3333"]}))
    master = tmp_path / "master.csv"
    master.write_text("symbol,exchange_division,listed_from,listed_to\n3333,TP,1900-01-01,\n", encoding="utf-8")
    rows = candidate_symbols({"1111": []}, master, (tmp_path / "bands",))
    flags = {r["symbol"]: (r["in_cache"], r["listed_in_master"]) for r in rows}
    assert flags == {"1111": (True, False), "3333": (False, True)}
