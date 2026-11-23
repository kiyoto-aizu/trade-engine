import importlib.util
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
