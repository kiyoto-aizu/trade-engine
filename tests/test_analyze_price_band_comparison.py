import csv
import json
from datetime import date, timedelta

from scripts.analysis.analyze_price_band_comparison import (
    analyze_candidate_day,
    analyze_screening_candidate_day,
    analyze_selection_atr_candidate,
    calculate_excursion_metrics,
    compare_selected_sets,
    common_dates,
    daily_midday_rows,
    dates_in_range,
    discover_json_dates,
    load_screening_candidates,
    minutes_from_time,
    next_trading_day,
    percentile,
    price_bucket,
    summarize_atr_population,
    summarize_atr_danger_scales,
    summarize_index_daily_returns,
    summarize_selection_atr_buckets,
    summarize_selection_atr_cutoffs,
    summarize_selection_atr_source_bands,
    summarize_selection_rank_relationship,
    summarize_skip_outcomes,
    summarize_screening_candidates,
    summarize_band,
    _write_csv,
    virtual_close_trade,
)
from src.domain.market_regime import MarketRegime


def test_common_dates_uses_only_days_with_all_price_band_files():
    results = {
        "270円": {"2026-10-05": [], "2026-10-06": []},
        "450円": {"2026-10-05": [], "2026-10-08": []},
        "900円": {"2026-10-05": []},
    }

    assert common_dates(results) == ["2026-10-05"]


def test_dates_in_range_keeps_inclusive_boundaries():
    assert dates_in_range(
        ["2026-10-04", "2026-10-05", "2026-11-01", "2026-11-02"],
        date(2026, 10, 5),
        date(2026, 11, 1),
    ) == ["2026-10-05", "2026-11-01"]


def test_discover_json_dates_supports_timestamp_suffixes(tmp_path):
    diagnostics = tmp_path / "diagnostics"
    diagnostics.mkdir()
    (diagnostics / "2026-10-13_120050680747.json").write_text("{}", encoding="utf-8")
    (diagnostics / "notes.json").write_text("{}", encoding="utf-8")

    assert discover_json_dates([diagnostics]) == ["2026-10-13"]


def test_write_csv_supports_rows_with_new_optional_fields(tmp_path):
    path = tmp_path / "rows.csv"
    _write_csv(path, [{"base": 1}, {"base": 2, "optional": "yes"}])

    with path.open(encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))

    assert rows == [
        {"base": "1", "optional": ""},
        {"base": "2", "optional": "yes"},
    ]


def test_excursion_metrics_use_price_series_and_find_first_touch():
    metrics = calculate_excursion_metrics(
        entry_price=100,
        prices=[100, 103, 98, 105],
        atr=2,
        profit_lock_multiplier=2,
        stop_loss_multiplier=1.5,
    )

    assert metrics == {
        "max_up_atr": 2.5,
        "max_down_atr": -1.0,
        "close_atr": 2.5,
        "profit_reached": True,
        "stop_reached": False,
        "first_touch": "利確",
        "first_touch_index": 3,
    }


def test_excursion_metrics_classify_stop_before_later_profit():
    metrics = calculate_excursion_metrics(
        entry_price=100,
        prices=[100, 96, 105],
        atr=2,
        profit_lock_multiplier=2,
        stop_loss_multiplier=1.5,
    )

    assert metrics["first_touch"] == "損切り"
    assert metrics["profit_reached"] is True
    assert metrics["stop_reached"] is True


def test_percentile_uses_linear_interpolation_and_empty_is_none():
    assert percentile([4, 1, 3, 2], 0.25) == 1.75
    assert percentile([], 0.5) is None


def test_band_summary_counts_zero_signal_days_and_post_filter_denominators():
    rows = [
        {
            "band": "270円",
            "date": "2026-10-05",
            "symbol": "1111",
            "signal_evaluated": True,
            "pre_signal": True,
            "post_signal": False,
            "primary_skip_reason": "atr_danger",
            "skip_reasons": ["atr_danger"],
            "max_up_atr": 1.0,
            "max_down_atr": -0.5,
            "close_atr": 0.25,
            "profit_reached": False,
            "stop_reached": False,
            "cost_atr_ratio": None,
            "tick_percent": None,
            "first_touch": "引け",
            "zero_shares_1_slots": None,
            "zero_shares_2_slots": None,
            "zero_shares_3_slots": None,
        },
        {
            "band": "270円",
            "date": "2026-10-06",
            "symbol": "2222",
            "signal_evaluated": True,
            "pre_signal": False,
            "post_signal": False,
            "primary_skip_reason": None,
            "skip_reasons": [],
            "max_up_atr": None,
            "max_down_atr": None,
            "close_atr": None,
            "profit_reached": None,
            "stop_reached": None,
            "cost_atr_ratio": None,
            "tick_percent": None,
            "first_touch": None,
            "zero_shares_1_slots": None,
            "zero_shares_2_slots": None,
            "zero_shares_3_slots": None,
        },
    ]

    summary = summarize_band("270円", rows, ["2026-10-05", "2026-10-06"])

    assert summary["pre_signal_count"] == 1
    assert summary["post_signal_count"] == 0
    assert summary["zero_signal_days"] == 1
    assert summary["zero_signal_day_rate"] == 0.5
    assert summary["primary_skip_atr_danger"] == 1
    assert summary["max_up_atr_n"] == 1
    assert summary["first_引け_n"] == 1
    assert summary["first_引け_rate"] == 1.0
    assert summary["zero_shares_1_slots_n"] == 0


def test_candidate_analysis_uses_only_prior_daily_bars_and_first_minute_crossing():
    trade_date = date(2026, 10, 5)
    daily_cache = {}
    for offset in range(40, 0, -1):
        close = 100 + (40 - offset)
        day = (trade_date - timedelta(days=offset)).isoformat()
        daily_cache[day] = {
            "open": close,
            "high": close + 1,
            "low": close - 1,
            "close": close,
        }
    base = analyze_candidate_day(
        "270円",
        "1111",
        trade_date.isoformat(),
        [("09:00", 130), ("09:01", 150), ("09:02", 200)],
        daily_cache,
        MarketRegime.CAUTION,
        True,
        None,
    )
    with_future = dict(daily_cache)
    with_future[trade_date.isoformat()] = {
        "open": 100_000,
        "high": 100_001,
        "low": 99_999,
        "close": 100_000,
    }
    with_future[(trade_date + timedelta(days=1)).isoformat()] = {
        "open": 200_000,
        "high": 200_001,
        "low": 199_999,
        "close": 200_000,
    }
    future_row = analyze_candidate_day(
        "270円",
        "1111",
        trade_date.isoformat(),
        [("09:00", 130), ("09:01", 150), ("09:02", 200)],
        with_future,
        MarketRegime.CAUTION,
        True,
        None,
    )

    assert base["pre_signal"] and base["post_signal"]
    assert base["signal_time"] == "09:01"
    assert base["entry_price"] == 150
    assert base["profit_reached"] is True
    assert future_row["rsi"] == base["rsi"]
    assert future_row["atr"] == base["atr"]


def test_candidate_post_filter_is_unavailable_when_market_regime_data_is_missing():
    trade_date = date(2026, 10, 5)
    daily_cache = {}
    for offset in range(40, 0, -1):
        close = 100 + (40 - offset)
        daily_cache[(trade_date - timedelta(days=offset)).isoformat()] = {
            "open": close,
            "high": close + 1,
            "low": close - 1,
            "close": close,
        }

    row = analyze_candidate_day(
        "270円",
        "1111",
        trade_date.isoformat(),
        [("09:35", 130), ("09:36", 150)],
        daily_cache,
        MarketRegime.DANGER,
        False,
        "index cache is stale",
    )

    assert row["pre_signal"] is True
    assert row["post_signal"] is None
    assert row["primary_skip_reason"] == "market_regime_unavailable"
    assert row["rsi_threshold"] is None


def test_price_bucket_uses_inclusive_upper_limits():
    assert price_bucket(270) == "〜270円"
    assert price_bucket(270.01) == "270超〜450円"
    assert price_bucket(450) == "270超〜450円"
    assert price_bucket(450.01) == "450超〜900円"
    assert price_bucket(900) == "450超〜900円"
    assert price_bucket(900.01) == "900円超（区分外）"
    assert price_bucket(None) == "価格不明"


def test_screening_candidates_are_deduplicated_by_symbol_day_and_bucketed_by_audit_price(
    tmp_path,
):
    symbols = [str(1000 + index) for index in range(50)]
    for relative_dir in (
        "data/screening",
        "data/screening_price_bands/450",
        "data/screening_price_bands/900",
    ):
        path = tmp_path / relative_dir / "2026-10-05.json"
        path.parent.mkdir(parents=True)
        (path).write_text(json.dumps({
            "date": "2026-10-05",
            "symbols": symbols,
            "audit_entries": [
                {"symbol": symbol, "price": 270 if symbol == "1000" else 451}
                for symbol in symbols
            ],
        }), encoding="utf-8")

    rows, issues = load_screening_candidates(tmp_path, ("2026-10-05",))

    assert len(rows) == 50
    assert not issues
    assert rows[0]["price_group"] == "〜270円"
    assert rows[0]["source_band_count"] == 3
    assert rows[1]["price_group"] == "450超〜900円"


def test_daily_candidate_excursions_use_prior_only_atr_and_summarize_samples():
    trade_date = date(2026, 10, 5)
    evaluation_date = date(2026, 10, 6)
    daily_cache = {
        (evaluation_date - timedelta(days=offset)).isoformat(): {
            "open": 100, "high": 102, "low": 98, "close": 100,
        }
        for offset in range(1, 15)
    }
    daily_cache[evaluation_date.isoformat()] = {
        "open": 100, "high": 110, "low": 90, "close": 105,
    }
    daily_cache["2026-10-07"] = {
        "open": 10_000, "high": 20_000, "low": 1, "close": 19_000,
    }
    candidate = {
        "date": trade_date.isoformat(),
        "symbol": "1111",
        "audit_price": 270.0,
        "price_group": "〜270円",
    }

    row = analyze_screening_candidate_day(candidate, daily_cache)
    summaries = summarize_screening_candidates([row])

    assert row["evaluation_date"] == "2026-10-06"
    assert row["atr"] == 4
    assert row["daily_range_atr"] == 5
    assert row["open_max_up_atr"] == 2.5
    assert row["open_max_down_atr"] == -2.5
    assert row["close_change_atr"] == 1.25
    assert summaries[0]["daily_range_atr_n"] == 1
    assert summaries[0]["daily_range_atr_median"] == 5
    assert summaries[0]["reference_value"] is True


def test_selection_atr_uses_only_bars_before_screening_day():
    screening_date = date(2026, 10, 5)
    evaluation_date = date(2026, 10, 6)
    daily_cache = {
        (screening_date - timedelta(days=offset)).isoformat(): {
            "open": 100, "high": 102, "low": 98, "close": 100,
        }
        for offset in range(1, 15)
    }
    daily_cache[screening_date.isoformat()] = {
        "open": 100, "high": 106, "low": 98, "close": 104,
    }
    daily_cache[evaluation_date.isoformat()] = {
        "open": 100, "high": 108, "low": 96, "close": 104,
    }
    daily_cache["2026-10-07"] = {
        "open": 100, "high": 10_000, "low": 1, "close": 9_000,
    }
    candidate = {
        "date": screening_date.isoformat(),
        "symbol": "1111",
        "price_group": "〜270円",
    }
    evaluation_row = {
        "evaluation_date": evaluation_date.isoformat(),
        "daily_ohlc_available": True,
    }

    row = analyze_selection_atr_candidate(candidate, daily_cache, evaluation_row)

    assert row["selection_reference_date"] == "2026-10-05"
    assert round(row["selection_atr"], 8) == round(60 / 14, 8)
    assert round(row["selection_atr_ratio"], 8) == round(8 / (60 / 14), 8)
    assert row["selection_atr_level"] == "CAUTION"
    assert round(row["daily_range_atr"], 8) == round(12 / (60 / 14), 8)
    assert round(row["open_max_up_atr"], 8) == round(8 / (60 / 14), 8)
    assert round(row["open_max_down_atr"], 8) == round(-4 / (60 / 14), 8)
    assert round(row["close_change_atr"], 8) == round(4 / (60 / 14), 8)


def test_selection_atr_unavailability_has_a_clear_reason():
    screening_date = date(2026, 10, 5)
    daily_cache = {
        (screening_date - timedelta(days=offset)).isoformat(): {
            "open": 100, "high": 102, "low": 98, "close": 100,
        }
        for offset in range(1, 13)
    }

    row = analyze_selection_atr_candidate(
        {"date": screening_date.isoformat(), "symbol": "646A"},
        daily_cache,
        {
            "evaluation_date": "2026-10-06",
            "daily_ohlc_available": True,
        },
    )

    assert row["selection_atr_unavailable"] is True
    assert "ATR(14)" in row["selection_atr_unavailable_reason"]


def test_selection_atr_summary_groups_price_and_volatility_levels():
    rows = [
        {
            "price_group": "〜270円",
            "selection_atr_level": "NORMAL",
            "evaluation_ohlc_available": True,
            "daily_range_atr": 2.0,
            "open_max_up_atr": 1.0,
            "open_max_down_atr": -1.0,
            "close_change_atr": 0.5,
        },
        {
            "price_group": "〜270円",
            "selection_atr_level": "NORMAL",
            "evaluation_ohlc_available": True,
            "daily_range_atr": 4.0,
            "open_max_up_atr": 2.0,
            "open_max_down_atr": -2.0,
            "close_change_atr": 1.5,
        },
        {
            "price_group": "〜270円",
            "selection_atr_level": "DANGER",
            "evaluation_ohlc_available": False,
            "daily_range_atr": None,
            "open_max_up_atr": None,
            "open_max_down_atr": None,
            "close_change_atr": None,
        },
    ]

    summaries = summarize_selection_atr_buckets(rows)
    normal = next(
        item for item in summaries
        if item["price_group"] == "〜270円"
        and item["selection_atr_level"] == "NORMAL"
    )

    assert normal["candidate_count"] == 2
    assert normal["daily_range_atr_median"] == 3
    assert normal["daily_range_atr_p25"] == 2.5
    assert normal["daily_range_atr_p75"] == 3.5


def test_selection_atr_source_band_summary_keeps_overlapping_memberships_in_band():
    rows = [
        {
            "date": "2026-10-13",
            "symbol": "1111",
            "source_bands": "270円,450円",
            "selection_atr_level": "NORMAL",
            "evaluation_ohlc_available": True,
            "daily_range_atr": 1.0,
            "open_max_up_atr": 0.5,
            "open_max_down_atr": -0.5,
            "close_change_atr": 0.25,
        },
        {
            "date": "2026-10-13",
            "symbol": "2222",
            "source_bands": "270円",
            "selection_atr_level": "DANGER",
            "evaluation_ohlc_available": True,
            "daily_range_atr": 2.0,
            "open_max_up_atr": 1.0,
            "open_max_down_atr": -1.0,
            "close_change_atr": -0.5,
        },
    ]

    summaries = summarize_selection_atr_source_bands(rows)
    low_normal = next(
        item for item in summaries
        if item["screening_band"] == "270円"
        and item["selection_atr_level"] == "NORMAL"
    )
    mid_normal = next(
        item for item in summaries
        if item["screening_band"] == "450円"
        and item["selection_atr_level"] == "NORMAL"
    )
    low_danger = next(
        item for item in summaries
        if item["screening_band"] == "270円"
        and item["selection_atr_level"] == "DANGER"
    )

    assert low_normal["candidate_count"] == 1
    assert mid_normal["candidate_count"] == 1
    assert low_danger["candidate_count"] == 1


def test_rank_relationship_reports_spearman_and_top_quartile_danger_rate():
    rows = [
        {
            "date": f"2026-10-0{index + 1}",
            "symbol": str(1000 + index),
            "turnover_rank": index + 1,
            "price_gain_rank": index + 1,
            "selection_atr_ratio": 2.5 - index * 0.5,
            "selection_atr_level": "DANGER" if index == 0 else "NORMAL",
            "evaluation_ohlc_available": True,
        }
        for index in range(4)
    ]

    summaries = summarize_selection_rank_relationship(rows)
    turnover_all = next(
        item for item in summaries
        if item["factor"] == "売買代金順位" and item["segment"] == "全体"
    )
    turnover_top = next(
        item for item in summaries
        if item["factor"] == "売買代金順位"
        and item["segment"] == "順位1四分位"
    )
    gain_all = next(
        item for item in summaries
        if item["factor"] == "値上がり率順位" and item["segment"] == "全体"
    )

    assert turnover_all["spearman_rho"] == -1
    assert gain_all["spearman_rho"] == -1
    assert turnover_top["danger_count"] == 1
    assert turnover_top["danger_rate"] == 1


def test_atr_cutoff_counts_screening_and_midday_members_separately():
    rows = [
        {"date": "2026-10-13", "symbol": "1111", "selection_atr_ratio": 1.4},
        {"date": "2026-10-13", "symbol": "2222", "selection_atr_ratio": 2.1},
    ]
    screening = [
        {
            "date": "2026-10-13",
            "symbol": "1111",
            "source_bands": "270円,450円",
        },
        {
            "date": "2026-10-13",
            "symbol": "2222",
            "source_bands": "270円",
        },
    ]
    selections = {
        "270円": {
            "2026-10-13": {"symbols": ["1111", "2222", "3333"]},
        },
        "450円": {"2026-10-13": {"symbols": ["1111"]}},
        "900円": {},
    }

    summaries = summarize_selection_atr_cutoffs(
        rows, screening, selections, ("2026-10-12", "2026-10-13")
    )
    low_price = next(
        item for item in summaries
        if item["band"] == "270円" and item["date"] == "2026-10-13"
    )
    mid_price = next(
        item for item in summaries
        if item["band"] == "450円" and item["date"] == "2026-10-13"
    )
    no_selection = next(
        item for item in summaries
        if item["band"] == "270円" and item["date"] == "2026-10-12"
    )

    assert low_price["screening_candidate_count"] == 2
    assert low_price["screening_below_danger_count"] == 1
    assert low_price["screening_below_danger_rate"] == 0.5
    assert low_price["midday_adopted_count"] == 3
    assert low_price["midday_atr_evaluable"] == 2
    assert low_price["midday_below_danger_count"] == 1
    assert low_price["midday_below_danger_rate"] == 0.5
    assert mid_price["screening_candidate_count"] == 1
    assert mid_price["screening_below_danger_count"] == 1
    assert no_selection["midday_adopted_count"] is None


def test_index_daily_return_uses_last_available_close_before_target_date():
    rows = summarize_index_daily_returns(
        {
            "2026-10-09": {"close": 100},
            "2026-10-13": {"close": 110},
            "2026-10-14": {"close": 99},
        },
        ("2026-10-13", "2026-10-14"),
    )

    assert [
        (
            row["date"],
            row["previous_close_date"],
            row["previous_close"],
            row["close"],
            round(row["change_percent"], 8),
        )
        for row in rows
    ] == [
        ("2026-10-13", "2026-10-09", 100, 110, 10),
        ("2026-10-14", "2026-10-13", 110, 99, -10),
    ]


def test_next_trading_day_skips_weekend_and_japanese_holiday():
    assert next_trading_day("2026-10-09") == "2026-10-13"


def test_daily_midday_rows_break_down_post_signals_by_primary_skip_reason():
    selections = {
        "270円": {
            "2026-10-13": {"symbols": ["1111", "2222", "3333"]},
        },
    }
    rows = [
        {
            "band": "270円", "date": "2026-10-13", "symbol": "1111",
            "pre_signal": True, "post_signal": True, "primary_skip_reason": None,
        },
        {
            "band": "270円", "date": "2026-10-13", "symbol": "2222",
            "pre_signal": True, "post_signal": False, "primary_skip_reason": "atr_danger",
            "rejection_category": "atr_danger",
        },
    ]
    missing = [{
        "band": "270円", "date": "2026-10-13", "symbol": "3333",
    }]

    summary = daily_midday_rows(selections, rows, missing)[0]

    assert summary["minute_available"] == 2
    assert summary["minute_missing"] == 1
    assert summary["pre_signals"] == 2
    assert summary["post_signals"] == 1
    assert summary["post_signal_unavailable"] == 0
    assert summary["skip_atr_danger"] == 1


def test_virtual_close_trade_checks_order_caps_and_deducts_costs():
    result = virtual_close_trade({
        "entry_price": 301.0,
        "close_price": 306.0,
    })

    assert result["buy_100_with_30000_cap"] is False
    assert result["buy_100_with_50000_cap"] is True
    assert result["buy_100_with_100000_cap"] is True
    assert result["virtual_gross_pnl_100"] == 500.0
    assert result["virtual_net_pnl_100"] < result["virtual_gross_pnl_100"]


def test_skip_outcome_summary_classifies_avoided_losses_and_missed_profits():
    rows = [
        {
            "band": "450円", "pre_signal": True, "post_signal": False,
            "rejection_category": "atr_danger", "atr": 2,
            "virtual_net_pnl_100": -250, "virtual_gross_pnl_100": -200,
        },
        {
            "band": "450円", "pre_signal": True, "post_signal": False,
            "rejection_category": "atr_danger", "atr": 3,
            "virtual_net_pnl_100": 150, "virtual_gross_pnl_100": 200,
        },
        {
            "band": "450円", "pre_signal": True, "post_signal": False,
            "rejection_category": "caution_rsi", "atr": 2,
            "virtual_net_pnl_100": -50, "virtual_gross_pnl_100": 100,
        },
        {
            "band": "900円", "pre_signal": True, "post_signal": True,
            "rejection_category": None, "atr": 2,
            "virtual_net_pnl_100": 200, "virtual_gross_pnl_100": 300,
        },
    ]

    summaries = summarize_skip_outcomes(rows)
    by_key = {(row["band"], row["reason_category"]): row for row in summaries}

    danger = by_key[("450円", "atr_danger")]
    assert danger["signal_count"] == danger["evaluable_count"] == 2
    assert danger["loss_count"] == danger["profit_count"] == 1
    assert danger["net_pnl_yen_sum"] == -100
    assert danger["net_pnl_atr_sum"] == -0.75
    assert by_key[("450円", "caution_rsi")]["loss_count"] == 1
    accepted = by_key[("900円", "accepted")]
    assert accepted["profit_count"] == 1
    assert accepted["net_pnl_yen_sum"] == 200


def test_atr_danger_scale_summary_pro_rates_virtual_pnl_and_keeps_sample_count():
    rows = [
        {
            "band": "450円", "pre_signal": True, "rejection_category": "atr_danger",
            "atr": 2, "virtual_net_pnl_100": -200,
        },
        {
            "band": "450円", "pre_signal": True, "rejection_category": "atr_danger",
            "atr": 4, "virtual_net_pnl_100": 400,
        },
        {
            "band": "900円", "pre_signal": True, "rejection_category": "other",
            "atr": 3, "virtual_net_pnl_100": 300,
        },
    ]

    summaries = summarize_atr_danger_scales(rows, ratios=(0.5,))
    by_band = {row["band"]: row for row in summaries}

    assert by_band["450円"]["signal_count"] == 2
    assert by_band["450円"]["equivalent_shares"] == 50
    assert by_band["450円"]["net_pnl_yen_sum"] == 100
    assert by_band["450円"]["loss_count"] == 1
    assert by_band["450円"]["profit_count"] == 1
    assert by_band["900円"]["signal_count"] == 0


def test_atr_population_summary_compares_price_bands_and_atr_ratio():
    rows = [
        {
            "band": "270円", "price_group": "〜270円",
            "reference_price": 200, "atr_level": "NORMAL", "atr_ratio": 1.0,
        },
        {
            "band": "900円", "price_group": "450超〜900円",
            "reference_price": 800, "atr_level": "DANGER", "atr_ratio": 2.2,
        },
    ]

    summaries = summarize_atr_population(rows)

    band_summaries = {row["group"]: row for row in summaries if row["group_type"] == "価格帯"}
    assert band_summaries["270円"]["atr_danger_rate"] == 0
    assert band_summaries["900円"]["atr_danger_rate"] == 1
    price_summaries = {row["group"]: row for row in summaries if row["group_type"] == "実価格"}
    assert price_summaries["450超〜900円"]["atr_ratio_median"] == 2.2


def test_minutes_from_time_excludes_every_bar_before_0935():
    bars = [
        ("2026-10-13T09:34:00", 10),
        ("2026-10-13T09:35:00", 11),
        ("09:36:00", 12),
    ]

    assert minutes_from_time(bars) == bars[1:]


def test_compare_selected_sets_reports_overlap_and_mismatch_ranks_and_ratios():
    summary, differences = compare_selected_sets(
        "2026-10-13",
        ["1111", "2222"],
        ["2222", "3333"],
        {
            "1111": {"rank": 3, "ratio": 1.2},
            "2222": {"rank": 1, "ratio": 2.0},
        },
        {
            "2222": {"rank": 2, "ratio": 1.8},
            "3333": {"rank": 1, "ratio": 3.0},
        },
    )

    assert summary["overlap_count"] == 1
    assert summary["morning_overlap_rate"] == 0.5
    assert summary["midday_overlap_rate"] == 0.5
    assert [row["symbol"] for row in differences] == ["1111", "3333"]
    assert differences[0]["morning_ratio"] == 1.2
    assert differences[1]["midday_rank"] == 1
