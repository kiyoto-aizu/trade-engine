from pathlib import Path

import pytest

from scripts.analysis import plot_intraday_trade_diagnostics as analysis


def test_trailing_line_starts_after_trigger_and_uses_rising_high():
    lines = analysis.TradeLines(
        entry=100.0, stop=98.0, atr=4.0, stop_multiplier=0.5,
        profit_multiplier=1.0, trigger_multiple=0.5,
    )
    trailing, _, trailing_margin, stop_margin = analysis.calculate_trailing_lines(
        [100.5, 102.0, 103.0], lines
    )
    assert trailing == [None, 98.0, 99.0]
    assert trailing_margin == pytest.approx(4.0)
    assert stop_margin == pytest.approx(2.5)


def test_missing_minute_partition_is_tolerated(tmp_path: Path):
    assert analysis.load_minute_bars(tmp_path / "missing.parquet") == []


def test_compressed_times_are_contiguous_across_lunch_break():
    assert analysis.compressed_times([
        {"time": analysis.datetime(2026, 10, 13, 11, 29), "price": 1},
        {"time": analysis.datetime(2026, 10, 13, 12, 30), "price": 1},
    ]) == [149, 150]


def test_time_axis_uses_real_time_with_lunch_break_compressed():
    assert analysis.compressed_minute_position(analysis.datetime(2026, 10, 13, 9, 0)) == 0
    assert analysis.compressed_minute_position(analysis.datetime(2026, 10, 13, 11, 30)) == 150
    assert analysis.compressed_minute_position(analysis.datetime(2026, 10, 13, 12, 30)) == 150
    assert analysis.compressed_minute_position(analysis.datetime(2026, 10, 13, 15, 30)) == 330


def test_event_marker_uses_previous_bar_position():
    bars = [
        {"time": analysis.datetime(2026, 10, 2, 15, 8), "price": 100.0},
        {"time": analysis.datetime(2026, 10, 2, 15, 9), "price": 101.0},
        {"time": analysis.datetime(2026, 10, 2, 15, 22), "price": 102.0},
    ]
    assert analysis._event_index(
        bars, analysis.datetime(2026, 10, 2, 15, 8, 56)
    ) == 0
    assert analysis._event_marker(
        bars, analysis.datetime(2026, 10, 2, 15, 8, 56)
    ) == (analysis.compressed_minute_position(analysis.datetime(2026, 10, 2, 15, 8, 56)), 100.0)


def test_filled_step_segments_keep_sessions_separate_and_fill_previous_price():
    bars = [
        {"time": analysis.datetime(2026, 10, 2, 11, 29), "price": 100.0},
        {"time": analysis.datetime(2026, 10, 2, 11, 30), "price": 101.0},
        {"time": analysis.datetime(2026, 10, 2, 12, 30), "price": 102.0},
        {"time": analysis.datetime(2026, 10, 2, 12, 32), "price": 104.0},
    ]
    segments = analysis._filled_step_segments(
        bars, analysis.datetime(2026, 10, 2, 11, 29)
    )
    assert len(segments) == 2
    assert segments[0][0] == [149, 150]
    assert segments[0][1] == [100.0, 101.0]
    assert segments[1][0] == [150, 151, 152]
    assert segments[1][1] == [102.0, 102.0, 104.0]


def test_skip_reason_and_reference_use_recorded_same_day_values():
    skip = {
        "event_type": "MARKET_REGIME_CAUTION_RSI_FILTER",
        "rsi": 56.2,
        "rsi_applied_threshold": 60.0,
        "occurred_at": "2026-10-13T09:35:00",
        "reference_price": 100.0,
        "same_day_high": 108.0,
        "same_day_close_price": 104.0,
    }
    reference = analysis._reference_skip({"skips": [skip]})
    assert analysis._skip_reason(skip) == "RSI56<60"
    assert reference["price"] == 100.0
    assert reference["record"]["same_day_high"] == 108.0


def test_invalid_order_price_is_excluded_from_valid_price():
    assert analysis._valid_order_price({"price": 0}) is None
    assert analysis._valid_order_price({"price": None}) is None
    assert analysis._valid_order_price({"price": 101.5}) == pytest.approx(101.5)


def test_index_baseline_reproduces_recorded_event_percent():
    bars = [
        {"time": analysis.datetime(2026, 10, 13, 9, 20), "price": 67572.5234375},
    ]
    baseline, validation = analysis._index_baseline_from_events(
        bars,
        [{
            "nikkei_intraday_change_percent": -2.0609327716657355,
            "nikkei_intraday_as_of": "2026-10-13T09:20:10",
        }],
    )
    assert baseline == pytest.approx(68994.45272432709)
    assert validation["matched"] is True
