import importlib.util
import json
from datetime import date
from pathlib import Path

import duckdb
import pytest


PATH = Path(__file__).resolve().parents[1] / "analysis" / "issue74_research_20261123" / "run_comparison.py"
SPEC = importlib.util.spec_from_file_location("issue74_research", PATH)
research = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(research)


def candidate(strategy, code, entry, exit_, price=100):
    return {"strategy": strategy, "code": code, "signal_date": date(2026, 1, 1),
            "entry_date": entry, "exit_date": exit_, "raw_open": price,
            "adjusted_open": price, "merged_rank": 1}


def test_stop_waits_for_observed_open_and_reconciles_daily_pnl():
    days = [date(2026, 1, day) for day in (2, 5, 6, 7)]
    row = candidate("range", "11110", days[0], days[2])
    bars = {
        ("11110", days[0]): (100, 100, 94),
        ("11110", days[1]): (None, None, 90),
        ("11110", days[2]): (80, 80, 81),
    }
    decisions, trades, curve, daily, positions, _ = research.simulate(days, [row], bars, ("range",), 0)
    assert decisions[("range", "filled")] == 1
    assert trades[0]["reason"] == "stop_open"
    assert trades[0]["exit_date"] == days[2]
    assert trades[0]["net_return"] == pytest.approx(-0.2)
    assert curve[-1]["equity"] == pytest.approx(280_000)
    assert sum(day["pnl"]["range"] for day in daily) == pytest.approx(-20_000)
    assert not positions


def test_priority_and_duplicate_use_shared_cash_and_slots():
    days = [date(2026, 1, day) for day in (2, 5, 6)]
    rows = [candidate("range", "11110", days[0], days[2]),
            candidate("pullback", "11110", days[0], days[2])]
    bars = {("11110", day): (100, 100, 100) for day in days}
    decisions, trades, curve, daily, positions, _ = research.simulate(
        days, rows, bars, ("pullback", "range"), .00105
    )
    assert decisions[("pullback", "filled")] == 1
    assert decisions[("range", "duplicate")] == 1
    assert len(trades) == 1
    assert trades[0]["strategy"] == "pullback"
    assert sum(row["pnl"]["pullback"] + row["pnl"]["range"] for row in daily) == pytest.approx(
        curve[-1]["equity"] - research.START_CASH
    )
    assert not positions


def test_missing_closes_delay_exit_and_unclosed_is_not_completed():
    days = [date(2026, 1, day) for day in (2, 5, 6)]
    row = candidate("pullback", "11110", days[0], days[1])
    bars = {("11110", days[0]): (100, 100, 100)}
    simulation = research.simulate(days, [row], bars, ("pullback",), 0)
    decisions, trades, curve, daily, positions, fills = simulation
    assert decisions[("pullback", "filled")] == 1
    assert trades == []
    assert len(positions) == 1
    assert curve[-1]["cash"] == 200_000
    assert curve[-1]["equity"] == 300_000
    result = research.summarize(simulation, ("pullback",), [(day, 0, "NORMAL") for day in days])
    assert result["strategies"]["pullback"]["unclosed_exit_state"] == {"scheduled_close_missing": 1}
    assert result["strategies"]["pullback"]["unclosed_max_mark_age_sessions"] == 2
    assert len(fills) == 1
    bars[("11110", days[2])] = (90, 90, 90)
    _, trades, _, _, positions, _ = research.simulate(days, [row], bars, ("pullback",), 0)
    assert trades[0]["delayed"]
    assert trades[0]["exit_date"] == days[2]
    assert not positions


def test_correlation_undefined_when_either_series_has_zero_variance():
    assert research.correlation([0, 0], [1, 2]) is None
    assert research.correlation([1, 2], [2, 1]) == pytest.approx(-1)


def test_candidate_selection_does_not_require_a_future_bar():
    con = duckdb.connect(":memory:")
    try:
        con.execute("CREATE TABLE market_state(date DATE, topix_ret1 DOUBLE, regime_vol_base VARCHAR)")
        con.execute("INSERT INTO market_state VALUES ('2026-11-13', 0, 'NORMAL'), "
                    "('2026-11-16', 0, 'NORMAL'), ('2026-11-17', 0, 'NORMAL')")
        con.execute("CREATE TABLE universe_master(symbol VARCHAR, listed_from DATE, listed_to DATE)")
        con.execute("INSERT INTO universe_master VALUES ('1111', '2020-01-01', NULL)")
        con.execute("""CREATE TABLE panel(
            code VARCHAR, date DATE, ret1 DOUBLE, ret5 DOUBLE, ret20 DOUBLE,
            off_high60 DOUBLE, sma25_gap DOUBLE, va DOUBLE, price_close DOUBLE, va_avg20 DOUBLE
        )""")
        con.execute("INSERT INTO panel VALUES "
                    "('11110', '2026-11-13', -0.01, 0.01, 0.01, -0.02, -0.03, 1e9, 500, 1e9)")
        con.execute("CREATE TABLE bars_daily(code VARCHAR, date DATE, o DOUBLE, adj_o DOUBLE, adj_c DOUBLE)")
        con.execute("INSERT INTO universe_master VALUES ('2222', '2020-01-01', NULL)")
        con.execute("INSERT INTO panel VALUES "
                    "('22220', '2026-11-13', NULL, NULL, NULL, NULL, NULL, 1e9, 500, 1e9)")
        _, _, candidates, _, missing = research.load_inputs(con)
        assert {r["strategy"] for r in candidates} == {"pullback", "range"}
        assert all(r["entry_date"] == date(2026, 11, 16) and r["raw_open"] is None for r in candidates)
        assert missing["pullback"]["any"] == 1
        assert missing["range"]["any"] == 1
        assert missing["rebound"]["any"] == 1
    finally:
        con.close()


def test_filled_overlap_includes_unclosed_positions_and_distribution():
    days = [date(2026, 1, day) for day in (2, 5, 6)]
    market = [(day, 0, "NORMAL") for day in days]
    left = candidate("pullback", "11110", days[0], days[1])
    right = candidate("range", "22220", days[0], days[1])
    bars = {(code, days[0]): (100, 100, 100) for code in ("11110", "22220")}
    a = research.simulate(days, [left], bars, ("pullback",), 0)
    b = research.simulate(days, [right], bars, ("range",), 0)
    overlap = research.compare_daily(a, b, market)
    assert overlap["filled_entry_day_overlap"] == 1
    assert overlap["completed_entry_day_overlap"] == 0
    assert research.quantile([-.1, 0, .1], .05) == pytest.approx(-.09)
    assert research.quantile([], .05) is None


def test_stop_scenarios_apply_only_to_selected_strategies():
    days = [date(2026, 1, day) for day in (2, 5, 6)]
    row = candidate("pullback", "11110", days[0], days[2])
    bars = {("11110", days[0]): (100, 100, 94), ("11110", days[1]): (90, 90, 90),
            ("11110", days[2]): (100, 100, 105)}
    original = research.simulate(days, [row], bars, ("pullback",), 0)
    all_stops = research.simulate(days, [row], bars, ("pullback",), 0, research.STRATEGIES)
    assert original[1][0]["reason"] == "scheduled_close"
    assert original[1][0]["net_return"] == pytest.approx(.05)
    assert all_stops[1][0]["reason"] == "stop_open"
    assert all_stops[1][0]["net_return"] == pytest.approx(-.10)


def test_unconstrained_candidates_include_overlap_and_exclude_missing_exits():
    days = [date(2026, 1, day) for day in (2, 5, 6)]
    rows = [candidate("range", "11110", days[0], days[2]),
            candidate("range", "22220", days[0], days[2]),
            candidate("range", "33330", days[0], days[2])]
    bars = {("11110", days[2]): (100, 100, 110), ("22220", days[2]): (100, 100, 90)}
    result = research.candidate_returns(days, rows, bars, .00105, ())
    item = result["range"]
    assert item["counts"]["completed"] == 2
    assert item["counts"]["unclosed"] == 1
    assert item["mean_net_return"] == pytest.approx((1 - .00105) / (1 + .00105) - 1)


def test_return_correlation_uses_prior_equity_and_handles_invalid_denominator():
    days = [date(2026, 1, d) for d in (2, 5, 6)]
    a = [{"date": d, "pnl": {"pullback": pnl}, "prior_equity": eq, "active": {"pullback"}}
         for d, pnl, eq in zip(days, (10, 20, 10), (100, 100, 0))]
    b = [{"date": d, "pnl": {"range": pnl}, "prior_equity": eq, "active": {"range"}}
         for d, pnl, eq in zip(days, (10, 20, 20), (100, 200, 100))]
    left = ({}, [], [], a, {}, [])
    right = ({}, [], [], b, {}, [])
    result = research.compare_daily(left, right, [(d, 0, "NORMAL") for d in days])
    assert result["all_sessions"]["nonpositive_equity_days"] == 1
    assert result["all_sessions"]["normalized_days"] == 2
    assert result["all_sessions"]["equity_return_correlation"] is None
    assert result["by_year_all_sessions"]["2026"]["days"] == 3


def test_existing_output_fails_before_accessing_inputs(tmp_path):
    with pytest.raises(FileExistsError):
        research.run(tmp_path / "missing-db", tmp_path, tmp_path / "missing-reference")


def test_future_prices_do_not_affect_ranks_or_candidate_membership():
    con = duckdb.connect(":memory:")
    try:
        con.execute("CREATE TABLE market_state(date DATE, topix_ret1 DOUBLE, regime_vol_base VARCHAR)")
        con.execute("INSERT INTO market_state VALUES ('2026-11-13', 0, 'NORMAL'), "
                    "('2026-11-16', 0, 'NORMAL'), ('2026-11-17', 0, 'NORMAL')")
        con.execute("CREATE TABLE universe_master(symbol VARCHAR, listed_from DATE, listed_to DATE)")
        con.execute("""CREATE TABLE panel(
            code VARCHAR, date DATE, ret1 DOUBLE, ret5 DOUBLE, ret20 DOUBLE,
            off_high60 DOUBLE, sma25_gap DOUBLE, va DOUBLE, price_close DOUBLE, va_avg20 DOUBLE
        )""")
        con.execute("CREATE TABLE bars_daily(code VARCHAR, date DATE, o DOUBLE, adj_o DOUBLE, adj_c DOUBLE)")
        for i in range(60):
            symbol = f"{1000 + i}"
            con.execute("INSERT INTO universe_master VALUES (?, '2020-01-01', NULL)", [symbol])
            ret1 = -.019 + (i * 17 % 60) * .0003
            con.execute("INSERT INTO panel VALUES (?, '2026-11-13', ?, .01, .01, -.02, -.03, ?, 500, 1e9)",
                        [symbol + "0", ret1, 1e9 - i * 1e6])
        def selection():
            return {(r["strategy"], r["code"], r["merged_rank"])
                    for r in research.load_inputs(con)[2]}
        before = selection()
        scores = [i + 1 + 60 - (i * 17 % 60) for i in range(60)]
        expected = {
            (strategy, f"{1000 + i}0", rank)
            for i, score in enumerate(scores)
            for rank in [1 + sum(other < score for other in scores)]
            if rank <= 50
            for strategy in ("pullback", "range")
        }
        assert before == expected
        con.execute("INSERT INTO bars_daily SELECT code, '2026-11-16', 1, 1, 1 FROM panel")
        assert selection() == before
        con.execute("UPDATE bars_daily SET o=1000000, adj_o=1000000, adj_c=1000000")
        assert selection() == before
    finally:
        con.close()


def test_full_slot_and_one_lot_cash_constraints_are_reported():
    days = [date(2026, 1, day) for day in (2, 5, 6)]
    rows = [candidate("pullback", str(11110 + i * 10), days[0], days[2], price=100)
            for i in range(4)]
    bars = {(row["code"], day): (100, 100, 100) for row in rows for day in days}
    decisions, _, _, _, _, _ = research.simulate(days, rows, bars, ("pullback",), 0)
    assert decisions[("pullback", "filled")] == 3
    assert decisions[("pullback", "full_slots")] == 1
    expensive = candidate("pullback", "99990", days[0], days[2], price=1001)
    decisions, _, _, _, _, _ = research.simulate(days, [expensive], {}, ("pullback",), .00105)
    assert decisions[("pullback", "below_one_lot")] == 1


def test_hash_mismatch_stops_before_reading_database(tmp_path):
    database = tmp_path / "input.db"
    database.write_bytes(b"test input")
    reference = tmp_path / "reference.json"
    reference.write_text(json.dumps({"input_sha256": "wrong"}), encoding="utf-8")
    with pytest.raises(ValueError, match="differs"):
        research.run(database, tmp_path / "output", reference)
    assert not (tmp_path / "output").exists()


def test_aggregate_output_excludes_trade_codes_and_prices():
    days = [date(2026, 1, d) for d in (2, 5, 6)]
    row = candidate("pullback", "SENSITIVE_CODE", days[0], days[2], price=123.456)
    bars = {(row["code"], d): (123.456, 123.456, 123.456) for d in days}
    market = [(d, 0, "NORMAL") for d in days]
    sim = research.simulate(days, [row], bars, ("pullback",), .00105)
    aggregate = research.summarize(sim, ("pullback",), market)
    text = json.dumps(aggregate)
    assert "SENSITIVE_CODE" not in text
    assert "123.456" not in text


def test_run_writes_only_aggregates_for_synthetic_database(tmp_path, monkeypatch):
    db = tmp_path / "input.duckdb"
    with duckdb.connect(str(db)) as con:
        con.execute("CREATE TABLE market_state(date DATE, topix_ret1 DOUBLE, regime_vol_base VARCHAR)")
        con.execute("INSERT INTO market_state VALUES ('2026-11-12', 0, 'NORMAL'), "
                    "('2026-11-13', 0, 'NORMAL'), ('2026-11-16', 0, 'NORMAL'), "
                    "('2026-11-17', 0, 'NORMAL')")
        con.execute("CREATE TABLE universe_master(symbol VARCHAR, listed_from DATE, listed_to DATE)")
        con.execute("INSERT INTO universe_master VALUES ('9876', '2020-01-01', NULL)")
        con.execute("""CREATE TABLE panel(
            code VARCHAR, date DATE, ret1 DOUBLE, ret5 DOUBLE, ret20 DOUBLE,
            off_high60 DOUBLE, sma25_gap DOUBLE, va DOUBLE, price_close DOUBLE, va_avg20 DOUBLE
        )""")
        con.execute("INSERT INTO panel VALUES "
                    "('98760', '2026-11-12', -.01, .01, .01, -.02, -.03, 1e9, 500, 1e9)")
        con.execute("CREATE TABLE bars_daily(code VARCHAR, date DATE, o DOUBLE, adj_o DOUBLE, adj_c DOUBLE)")
        con.execute("INSERT INTO bars_daily VALUES "
                    "('98760', '2026-11-13', 123.456789, 123.456789, 123.456789), "
                    "('98760', '2026-11-16', 123.456789, 123.456789, 123.456789), "
                    "('98760', '2026-11-17', 123.456789, 123.456789, 123.456789)")
    reference = tmp_path / "reference.json"
    reference.write_text(json.dumps({"input_sha256": research.digest(db)}), encoding="utf-8")
    output = tmp_path / "output"
    with pytest.raises(ValueError, match="recorded signal-only baseline"):
        research.run(db, output, reference)
    assert not output.exists()

    monkeypatch.setattr(research, "validate_pullback_baseline", lambda ref: None)
    research.run(db, output, reference)
    assert {path.name for path in output.iterdir()} == {"report.json"}
    text = (output / "report.json").read_text(encoding="utf-8")
    report = json.loads(text)
    assert report["candidate_counts"] == {"pullback": 1, "range": 1}
    assert set(report["followup"]) == {"original", "no_stops", "all_stops"}
    assert report["candidate_ranking"]["pullback"]["candidates"] == 1
    assert "98760" not in text
    assert "123.456789" not in text
