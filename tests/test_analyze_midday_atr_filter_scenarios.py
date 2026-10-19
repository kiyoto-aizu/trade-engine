from datetime import date, timedelta

from scripts.analysis.analyze_midday_atr_filter_scenarios import (
    calculate_preceding_atr_ratios,
    select_scenario_symbols,
    summarize_scenario_day,
    summarize_scenario_totals,
)


def test_atr_filter_scenarios_select_highest_multiplier_rank_with_cap():
    candidates = {
        str(index): {"rank": index + 1, "ratio": 3.0 - index / 10}
        for index in range(12)
    }
    atr_ratios = {
        "0": 2.0,
        "1": 1.9,
        "2": 1.4,
        "3": 1.5,
        "4": 1.2,
        "5": 1.7,
        "6": None,
        "7": 1.3,
        "8": 1.8,
        "9": 1.1,
        "10": 1.6,
        "11": 2.1,
    }
    adopted = [str(index) for index in range(10)]

    current = select_scenario_symbols(candidates, adopted, atr_ratios, "A")
    below_danger = select_scenario_symbols(candidates, adopted, atr_ratios, "B")
    normal_only = select_scenario_symbols(candidates, adopted, atr_ratios, "C")

    assert current["symbols"] == adopted
    assert below_danger["symbols"] == [
        "1", "2", "3", "4", "5", "7", "8", "9", "10",
    ]
    assert below_danger["eligible_count"] == 9
    assert below_danger["shortfall_count"] == 1
    assert normal_only["symbols"] == ["2", "4", "7", "9"]
    assert normal_only["eligible_count"] == 4
    assert normal_only["shortfall_count"] == 6


def test_selection_atr_ratio_uses_prior_completed_daily_bars_only():
    trade_date = date(2026, 10, 13)
    cache = {
        (trade_date - timedelta(days=offset)).isoformat(): {
            "open": 100,
            "high": 102,
            "low": 98,
            "close": 100,
        }
        for offset in range(1, 15)
    }
    cache[trade_date.isoformat()] = {
        "open": 100,
        "high": 1_000,
        "low": 1,
        "close": 900,
    }

    ratios = calculate_preceding_atr_ratios(
        {"1111": {"rank": 1}},
        trade_date.isoformat(),
        {"1111": cache},
    )

    assert ratios == {"1111": 1.0}


def test_scenario_summary_counts_net_pnl_only_for_post_filter_signals():
    rows = [
        {
            "scenario": "B",
            "band": "450円",
            "date": "2026-10-13",
            "input_count": 50,
            "input_atr_evaluable": 48,
            "input_atr_below_2_count": 35,
            "input_normal_count": 24,
            "input_no_minute_count": 2,
            "eligible_count": 35,
            "replaced_from_A_count": 3,
            "replaced_from_A_symbols": ["a", "b", "c"],
            "added_vs_A_symbols": ["x", "y", "z"],
            "selection_shortfall_count": 0,
            "minute_available": True,
            "signal_evaluated": True,
            "pre_signal": True,
            "post_signal": True,
            "rejection_category": None,
            "virtual_net_pnl_100": 230.5,
            "close_atr": 0.4,
        },
        {
            "scenario": "B",
            "band": "450円",
            "date": "2026-10-13",
            "input_count": 50,
            "input_atr_evaluable": 48,
            "input_atr_below_2_count": 35,
            "input_normal_count": 24,
            "input_no_minute_count": 2,
            "eligible_count": 35,
            "replaced_from_A_count": 3,
            "replaced_from_A_symbols": ["a", "b", "c"],
            "added_vs_A_symbols": ["x", "y", "z"],
            "selection_shortfall_count": 0,
            "minute_available": True,
            "signal_evaluated": True,
            "pre_signal": True,
            "post_signal": False,
            "rejection_category": "atr_danger",
            "virtual_net_pnl_100": -500,
            "close_atr": -1.2,
        },
    ]

    summary = summarize_scenario_day(rows)

    assert summary["selected_count"] == 2
    assert summary["pre_signal_count"] == 2
    assert summary["post_signal_count"] == 1
    assert summary["skip_atr_danger"] == 1
    assert summary["post_virtual_net_pnl_100_sum"] == 230.5
    assert summary["post_virtual_net_pnl_100_n"] == 1
    assert summary["post_close_atr_median"] == 0.4


def test_scenario_summary_can_represent_zero_eligible_selection():
    context = {
        "scenario": "C",
        "band": "900円",
        "date": "2026-10-13",
        "input_count": 50,
        "input_atr_evaluable": 0,
        "input_atr_below_2_count": 0,
        "input_normal_count": 0,
        "input_no_minute_count": 0,
        "eligible_count": 0,
        "replaced_from_A_count": 10,
        "replaced_from_A_symbols": [str(index) for index in range(10)],
        "added_vs_A_symbols": [],
        "selection_shortfall_count": 10,
    }

    summary = summarize_scenario_day([], context)

    assert summary["selected_count"] == 0
    assert summary["pre_signal_count"] == 0
    assert summary["post_signal_count"] == 0
    assert summary["selection_shortfall_count"] == 10


def test_scenario_totals_sum_daily_signal_and_net_results():
    rows = [
        {
            "scenario": "A",
            "band": "270円",
            "pre_signal_count": 2,
            "post_signal_count": 1,
            "post_signal_unavailable_count": 0,
            "skip_caution_rsi": 0,
            "skip_atr_danger": 1,
            "skip_other": 0,
            "post_virtual_net_pnl_100_sum": 100,
            "post_virtual_net_pnl_100_n": 1,
            "replaced_from_A_count": 0,
            "selection_shortfall_count": 0,
        },
        {
            "scenario": "A",
            "band": "270円",
            "pre_signal_count": 3,
            "post_signal_count": 2,
            "post_signal_unavailable_count": 1,
            "skip_caution_rsi": 1,
            "skip_atr_danger": 0,
            "skip_other": 0,
            "post_virtual_net_pnl_100_sum": -50,
            "post_virtual_net_pnl_100_n": 2,
            "replaced_from_A_count": 0,
            "selection_shortfall_count": 0,
        },
    ]

    summary = summarize_scenario_totals(rows)[0]

    assert summary["day_count"] == 2
    assert summary["pre_signal_count"] == 5
    assert summary["post_signal_count"] == 3
    assert summary["post_signal_unavailable_count"] == 1
    assert summary["post_virtual_net_pnl_100_sum"] == 50
    assert summary["post_virtual_net_pnl_100_n"] == 3
