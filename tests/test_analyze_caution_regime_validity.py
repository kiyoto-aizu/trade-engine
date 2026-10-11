from scripts.analysis import analyze_caution_regime_validity as study


def test_trend_groups_separate_caution_other_and_unknown_regimes():
    groups = study._trend_groups([
        {
            "regime": "CAUTION",
            "selected_trend": 2,
            "selected_judged": 10,
            "candidate_trend": 3,
            "candidate_judged": 20,
        },
        {
            "regime": "NORMAL",
            "selected_trend": 1,
            "selected_judged": 5,
            "candidate_trend": 4,
            "candidate_judged": 15,
        },
        {
            "regime": None,
            "selected_trend": 0,
            "selected_judged": 0,
            "candidate_trend": 0,
            "candidate_judged": 0,
        },
    ])

    assert groups["CAUTION"] == {
        "days": 1, "selected": [2, 10], "candidate": [3, 20],
    }
    assert groups["他"]["days"] == 1
    assert groups["状態不明"]["days"] == 1
    assert study._is_reference(groups["他"], "selected")
    assert study._is_reference(groups["CAUTION"], "selected")
    assert not study._is_reference(
        {"days": 10, "selected": [3, 30], "candidate": [8, 50]}, "selected"
    )


def test_skip_outcomes_include_parquet_unmatched_events(monkeypatch):
    monkeypatch.setattr(study, "_event_pnl", lambda event: event.get("pnl"))
    events = [
        {"event_type": study.EVENT_TYPES[0], "pnl": -100},
        {"event_type": study.EVENT_TYPES[0], "pnl": 250},
        {"event_type": study.EVENT_TYPES[0], "pnl": None},
        {"event_type": study.EVENT_TYPES[1], "pnl": 0},
    ]

    result = study._skip_outcomes(events)

    rsi = result["MARKET_REGIME_CAUTION_RSI_FILTER"]
    assert (rsi["events"], rsi["matched"], rsi["unmatched"]) == (3, 2, 1)
    assert rsi["outcomes"] == {"損失回避": 1, "取り逃し": 1}
    assert rsi["pnl"] == 150
    atr = result["ATR_DANGER_SKIP"]
    assert atr["outcomes"] == {"値動きなし": 1}
    assert atr["pnl"] == 0


def test_previous_close_does_not_use_same_day_or_stale_cache_value():
    assert study._previous_daily_close(
        {
            "2026-10-08": {"close": 100},
            "2026-10-09": {"close": 120},
        },
        "2026-10-09",
    ) == 100
    assert study._previous_daily_close(
        {"2026-10-02": {"close": 100}},
        "2026-10-06",
    ) is None
