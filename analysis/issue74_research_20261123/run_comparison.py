"""Exploratory Issue 74 replay; reads the existing research DB and writes aggregates only."""
from __future__ import annotations

import argparse
import hashlib
import json
import math
from collections import Counter, defaultdict
from datetime import date
from pathlib import Path
from statistics import mean, median

import duckdb

ROOT = Path(__file__).resolve().parents[2]
DB = ROOT / "data" / "analysis" / "trend_research_20261117" / "trend_research.duckdb"
REFERENCE = ROOT / "data" / "analysis" / "trend_research_20261117" / "issue73" / "freeze_v1" / "report.json"
OUTPUT = ROOT / "data" / "analysis" / "issue74_research_20261123" / "trial_review_followup_v1"
END = date(2026, 11, 17)
START_CASH = 300_000.0
SLOTS = 3
MAX_ORDER = 200_000
LOT = 100
STRATEGIES = ("pullback", "range", "rebound")
FEES = (0.00105, 0.00205, 0.00305)


def digest(path):
    with path.open("rb") as stream:
        hasher = hashlib.sha256()
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            hasher.update(chunk)
        return hasher.hexdigest()


def load_inputs(con):
    market = con.execute(
        "SELECT date, topix_ret1, regime_vol_base FROM market_state "
        "WHERE date <= ? ORDER BY date", [END]
    ).fetchall()
    sessions = [row[0] for row in market]
    if not sessions or sessions[-1] != END or len(set(sessions)) != len(sessions):
        raise ValueError("Invalid market session series")
    relation = con.execute("""
        WITH timing AS (
            SELECT date, lead(date, 1) OVER w AS entry_date,
                   lead(date, 3) OVER w AS exit_date
            FROM market_state WHERE date <= $cutoff WINDOW w AS (ORDER BY date)
        ), affordable AS (
            SELECT p.code, p.date AS signal_date, p.ret1, p.ret5, p.ret20,
                   p.off_high60, p.sma25_gap, p.va, t.entry_date, t.exit_date,
                   b.o AS raw_open, b.adj_o AS adjusted_open,
                   rank() OVER (PARTITION BY p.date ORDER BY p.va DESC) AS turnover_rank,
                   rank() OVER (PARTITION BY p.date ORDER BY p.ret1 DESC NULLS LAST) AS return_rank
            FROM panel p JOIN timing t ON t.date = p.date
            JOIN universe_master u ON substr(p.code, 1, 4) = u.symbol
                AND p.date >= u.listed_from AND (u.listed_to IS NULL OR p.date <= u.listed_to)
            LEFT JOIN bars_daily b ON b.code = p.code AND b.date = t.entry_date
            WHERE substr(p.code, 5, 1) = '0' AND p.price_close <= 1000
                AND p.va_avg20 >= 1e8 AND p.date <= $cutoff
        ), ranked AS (
            SELECT *, rank() OVER (PARTITION BY signal_date ORDER BY turnover_rank + return_rank)
                AS merged_rank
            FROM affordable
        )
        SELECT * FROM ranked WHERE merged_rank <= 50
        ORDER BY entry_date, merged_rank, code
    """, {"cutoff": END})
    columns = [column[0] for column in relation.description]
    candidates = []
    required = {
        "pullback": ("ret1", "ret5", "off_high60"),
        "range": ("ret20", "sma25_gap"),
        "rebound": ("ret20", "ret5", "ret1"),
    }
    missing = {strategy: Counter({"any": 0, **{field: 0 for field in fields}})
               for strategy, fields in required.items()}
    for values in relation.fetchall():
        row = dict(zip(columns, values))
        for strategy, fields in required.items():
            missing_fields = [field for field in fields if row[field] is None]
            if missing_fields:
                missing[strategy]["any"] += 1
                missing[strategy].update(missing_fields)
        if (row["ret1"] is not None and -.02 <= row["ret1"] <= 0
                and row["ret5"] is not None and row["ret5"] > 0
                and row["off_high60"] is not None and row["off_high60"] >= -.05):
            candidates.append({**row, "strategy": "pullback"})
        if (row["ret20"] is not None and abs(row["ret20"]) <= .03
                and row["sma25_gap"] is not None and row["sma25_gap"] <= -.02):
            candidates.append({**row, "strategy": "range"})
        if (row["ret20"] is not None and row["ret20"] <= -.10
                and row["ret5"] is not None and row["ret5"] <= -.08
                and row["ret1"] is not None and row["ret1"] > 0):
            candidates.append({**row, "strategy": "rebound"})
    codes = sorted({row["code"] for row in candidates})
    bars = {
        (code, day): (raw_open, adjusted_open, adjusted_close)
        for code, day, raw_open, adjusted_open, adjusted_close in con.execute("""
            SELECT code, date, o, adj_o, adj_c FROM bars_daily
            WHERE date <= ? AND code IN (SELECT unnest(?))
        """, [END, codes]).fetchall()
    }
    return sessions, market, candidates, bars, {key: dict(value) for key, value in missing.items()}


def valid(value):
    return value is not None and math.isfinite(value) and value > 0


def simulate(sessions, candidates, bars, strategies, fee, stop_strategies=("range", "rebound")):
    by_day = defaultdict(list)
    decisions = Counter()
    for row in candidates:
        if row["strategy"] not in strategies:
            continue
        if row["entry_date"] is None or row["exit_date"] is None:
            decisions[(row["strategy"], "right_censored")] += 1
        elif not valid(row["raw_open"]) or not valid(row["adjusted_open"]):
            decisions[(row["strategy"], "missing_entry")] += 1
        else:
            by_day[row["entry_date"]].append(row)
    for rows in by_day.values():
        rows.sort(key=lambda r: (strategies.index(r["strategy"]), r["merged_rank"], r["code"]))
    cash = START_CASH
    positions = {}
    trades = []
    fills = []
    curve = []
    daily = []
    before_strategy = {}
    for day in sessions:
        before = {
            code: pos["notional"] * pos["last"] / pos["row"]["adjusted_open"]
            for code, pos in positions.items()
        }
        equity_before_open = cash + sum(before.values())
        flow = Counter()
        active = {pos["row"]["strategy"] for pos in positions.values()}

        def sell(code, adjusted_price, reason):
            nonlocal cash
            pos = positions.pop(code)
            row = pos["row"]
            proceeds = pos["notional"] * adjusted_price / row["adjusted_open"] * (1 - fee)
            cost = pos["notional"] * (1 + fee)
            cash += proceeds
            flow[row["strategy"]] += proceeds
            trades.append({
                "strategy": row["strategy"], "code": code, "signal_date": row["signal_date"],
                "entry_date": row["entry_date"], "exit_date": day,
                "net_pnl": proceeds - cost, "net_return": proceeds / cost - 1,
                "reason": reason, "delayed": day > row["exit_date"],
            })

        for code, pos in list(positions.items()):
            if pos["stop_pending"]:
                bar = bars.get((code, day))
                if bar is not None and valid(bar[0]) and valid(bar[1]):
                    sell(code, bar[1], "stop_open")
        for row in by_day[day]:
            strategy, code = row["strategy"], row["code"]
            if code in positions:
                decisions[(strategy, "duplicate")] += 1
                continue
            if len(positions) >= SLOTS:
                decisions[(strategy, "full_slots")] += 1
                continue
            budget = min(MAX_ORDER, equity_before_open / SLOTS, cash / (1 + fee))
            quantity = math.floor(budget / row["raw_open"] / LOT) * LOT
            if quantity < LOT:
                decisions[(strategy, "below_one_lot")] += 1
                continue
            notional = quantity * row["raw_open"]
            cash -= notional * (1 + fee)
            flow[strategy] -= notional * (1 + fee)
            positions[code] = {"row": row, "notional": notional,
                               "last": row["adjusted_open"], "last_day": day,
                               "last_kind": "entry_open", "stop_pending": False}
            decisions[(strategy, "filled")] += 1
            fills.append({"strategy": strategy, "code": code, "entry_date": day})
            active.add(strategy)
        slots = len(positions)
        for code, pos in list(positions.items()):
            row = pos["row"]
            bar = bars.get((code, day))
            close = bar[2] if bar is not None else None
            if valid(close):
                pos["last"] = close
                pos["last_day"] = day
                pos["last_kind"] = "close"
            if not pos["stop_pending"] and day >= row["exit_date"] and valid(close):
                sell(code, close, "scheduled_close")
            elif (not pos["stop_pending"] and row["strategy"] in stop_strategies
                  and day < row["exit_date"] and valid(close)
                  and close / row["adjusted_open"] <= .95):
                pos["stop_pending"] = True
        after = {
            code: pos["notional"] * pos["last"] / pos["row"]["adjusted_open"]
            for code, pos in positions.items()
        }
        pnl = {strategy: float(flow[strategy]) for strategy in strategies}
        for code, value in before.items():
            pnl[before_strategy[code]] -= value
        for code, value in after.items():
            pnl[positions[code]["row"]["strategy"]] += value
        equity = cash + sum(after.values())
        if not math.isclose(sum(pnl.values()), equity - equity_before_open, abs_tol=1e-6):
            raise ValueError("Daily strategy attribution does not reconcile")
        curve.append({"date": day, "equity": equity, "cash": cash, "slots": slots})
        daily.append({"date": day, "pnl": pnl, "active": active, "prior_equity": equity_before_open})
        before_strategy = {code: pos["row"]["strategy"] for code, pos in positions.items()}
    return decisions, trades, curve, daily, positions, fills


def summarize(simulation, strategies, market):
    decisions, trades, curve, _, positions, _ = simulation
    peak, max_dd = START_CASH, 0.0
    for row in curve:
        peak = max(peak, row["equity"])
        max_dd = max(max_dd, 1 - row["equity"] / peak)
    results = {}
    regimes = {row[0]: row[2] for row in market}
    for strategy in strategies:
        subset = [row for row in trades if row["strategy"] == strategy]
        returns = [row["net_return"] for row in subset]
        open_positions = [p for p in positions.values() if p["row"]["strategy"] == strategy]
        session_indices = {row["date"]: index for index, row in enumerate(curve)}
        open_reasons = Counter(
            "stop_open_pending" if p["stop_pending"] else
            "scheduled_close_missing" if curve[-1]["date"] >= p["row"]["exit_date"] else
            "before_scheduled_exit"
            for p in open_positions
        )
        results[strategy] = {
            "decisions": {reason: count for (s, reason), count in decisions.items() if s == strategy},
            "completed": len(subset), "win_rate": sum(x > 0 for x in returns) / len(returns) if returns else None,
            "mean_net_return": mean(returns) if returns else None,
            "median_net_return": median(returns) if returns else None,
            "net_return_p05": quantile(returns, .05),
            "net_return_p95": quantile(returns, .95),
            "net_realized_pnl": sum(row["net_pnl"] for row in subset),
            "completed_per_week": len(subset) / len({r["date"].isocalendar()[:2] for r in curve}),
            "completed_per_month": len(subset) / len({(r["date"].year, r["date"].month) for r in curve}),
            "stop_exits": sum(row["reason"] == "stop_open" for row in subset),
            "delayed_exits": sum(row["delayed"] for row in subset),
            "unclosed": len(open_positions),
            "unclosed_exit_state": dict(open_reasons),
            "unclosed_last_mark_kind": dict(Counter(p["last_kind"] for p in open_positions)),
            "unclosed_max_mark_age_sessions": max(
                (len(curve) - 1 - session_indices[p["last_day"]] for p in open_positions), default=None
            ),
            "by_entry_year": breakdown(subset, lambda row: str(row["entry_date"].year)),
            "by_signal_regime": breakdown(subset, lambda row: regimes.get(row["signal_date"]) or "unknown"),
        }
    mark = curve[-1]["equity"] - curve[-1]["cash"]
    return {
        "strategies": results, "final_equity": curve[-1]["equity"], "cash": curve[-1]["cash"],
        "unclosed_mark": mark, "zero_mark_stress_equity": curve[-1]["cash"], "max_dd": max_dd,
        "slot_utilization": sum(r["slots"] for r in curve) / (SLOTS * len(curve)),
    }


def quantile(values, fraction):
    if not values:
        return None
    ordered = sorted(values)
    index = (len(ordered) - 1) * fraction
    low, high = math.floor(index), math.ceil(index)
    return ordered[low] + (ordered[high] - ordered[low]) * (index - low)


def breakdown(trades, key):
    groups = defaultdict(list)
    for row in trades:
        groups[key(row)].append(row)
    return {
        group: {"completed": len(rows),
                "win_rate": sum(row["net_return"] > 0 for row in rows) / len(rows),
                "mean_net_return": mean(row["net_return"] for row in rows),
                "net_realized_pnl": sum(row["net_pnl"] for row in rows)}
        for group, rows in groups.items()
    }


def correlation(xs, ys):
    if len(xs) < 2:
        return None
    mx, my = mean(xs), mean(ys)
    dx, dy = [x - mx for x in xs], [y - my for y in ys]
    denom = math.sqrt(sum(x * x for x in dx) * sum(y * y for y in dy))
    return sum(x * y for x, y in zip(dx, dy)) / denom if denom else None


def compare_daily(left, right, market):
    ld, rd = left[3], right[3]
    market_by_day = {row[0]: row[1] for row in market}
    pairs = list(zip(ld, rd))
    if any(a["date"] != b["date"] for a, b in pairs) or len(ld) != len(rd):
        raise ValueError("Daily series do not align")
    a_name = next(iter(ld[0]["pnl"]))
    b_name = next(iter(rd[0]["pnl"]))
    active = [(a, b) for a, b in pairs if a_name in a["active"] and b_name in b["active"]]
    crashes = [(a, b) for a, b in active
               if market_by_day[a["date"]] is not None and market_by_day[a["date"]] <= -.02]
    left_trades, right_trades = left[1], right[1]
    left_fills, right_fills = left[5], right[5]
    left_days = {r["entry_date"] for r in left_trades}
    right_days = {r["entry_date"] for r in right_trades}
    left_pairs = {(r["entry_date"], r["code"]) for r in left_trades}
    right_pairs = {(r["entry_date"], r["code"]) for r in right_trades}
    left_filled_days = {r["entry_date"] for r in left_fills}
    right_filled_days = {r["entry_date"] for r in right_fills}
    left_filled_pairs = {(r["entry_date"], r["code"]) for r in left_fills}
    right_filled_pairs = {(r["entry_date"], r["code"]) for r in right_fills}
    def metric(rows):
        xs = [a["pnl"][a_name] for a, _ in rows]
        ys = [b["pnl"][b_name] for _, b in rows]
        normalized = [(a["pnl"][a_name] / a["prior_equity"], b["pnl"][b_name] / b["prior_equity"])
                      for a, b in rows if a["prior_equity"] > 0 and b["prior_equity"] > 0]
        return {"days": len(rows), "correlation": correlation(xs, ys),
                "normalized_days": len(normalized), "nonpositive_equity_days": len(rows) - len(normalized),
                "equity_return_correlation": correlation(
                    [x for x, _ in normalized], [y for _, y in normalized]),
                "simultaneous_loss_days": sum(x < 0 and y < 0 for x, y in zip(xs, ys)),
                "simultaneous_loss_rate": sum(x < 0 and y < 0 for x, y in zip(xs, ys)) / len(rows) if rows else None}
    return {"all_sessions": metric(pairs), "both_active": metric(active),
            "both_active_topix_down_2pct": metric(crashes),
            "by_year_all_sessions": {
                str(year): metric([(a, b) for a, b in pairs if a["date"].year == year])
                for year in sorted({a["date"].year for a, _ in pairs})
            },
            "filled_entry_day_overlap": len(left_filled_days & right_filled_days),
            "filled_entry_day_code_overlap": len(left_filled_pairs & right_filled_pairs),
            "completed_entry_day_overlap": len(left_days & right_days),
            "completed_entry_day_code_overlap": len(left_pairs & right_pairs)}


def candidate_returns(sessions, candidates, bars, fee, stop_strategies):
    indices = {day: index for index, day in enumerate(sessions)}
    stats = {strategy: Counter() for strategy in STRATEGIES}
    values = {strategy: [] for strategy in STRATEGIES}
    for row in candidates:
        strategy = row["strategy"]
        counts = stats[strategy]
        if row["entry_date"] is None or row["exit_date"] is None:
            counts["right_censored"] += 1
            continue
        if not valid(row["raw_open"]) or not valid(row["adjusted_open"]):
            counts["missing_entry"] += 1
            continue
        pending = False
        exit_price = None
        for day in sessions[indices[row["entry_date"]]:]:
            bar = bars.get((row["code"], day))
            if pending and bar is not None and valid(bar[0]) and valid(bar[1]):
                exit_price = bar[1]
                counts["stop_exit"] += 1
                break
            close = bar[2] if bar is not None else None
            if not pending and day >= row["exit_date"] and valid(close):
                exit_price = close
                break
            if (not pending and strategy in stop_strategies and day < row["exit_date"]
                    and valid(close) and close / row["adjusted_open"] <= .95):
                pending = True
        if exit_price is None:
            counts["unclosed"] += 1
            continue
        counts["completed"] += 1
        values[strategy].append(exit_price * (1 - fee) / (row["adjusted_open"] * (1 + fee)) - 1)
    return {
        strategy: {
            "counts": dict(stats[strategy]),
            "mean_net_return": mean(values[strategy]) if values[strategy] else None,
            "median_net_return": median(values[strategy]) if values[strategy] else None,
            "win_rate": (sum(value > 0 for value in values[strategy]) / len(values[strategy])
                         if values[strategy] else None),
        }
        for strategy in STRATEGIES
    }


def validate_pullback_baseline(ref):
    pull = ref["strategies"]["pullback"]
    if (sum(pull["decisions"].values()), pull["completed"], round(ref["final_equity"]),
        round(ref["max_dd"], 4)) != (1108, 542, 381950, .1395):
        raise ValueError("Pullback replay does not match the recorded signal-only baseline")


def run(db, output, reference=REFERENCE):
    if output.exists():
        raise FileExistsError(f"Output already exists: {output}")
    with reference.open(encoding="utf-8") as stream:
        expected_hash = json.load(stream)["input_sha256"]
    db_hash = digest(db)
    if db_hash != expected_hash:
        raise ValueError("Input DB differs from the frozen Issue 73 baseline")
    with duckdb.connect(str(db), read_only=True) as con:
        sessions, market, candidates, bars, missing = load_inputs(con)
    if digest(db) != db_hash:
        raise ValueError("Input DB changed during read")
    runs = {}
    correlations = {}
    strategy_sets = {
        "pullback": ("pullback",), "range": ("range",), "rebound": ("rebound",),
        "pullback_range": ("pullback", "range"),
        "pullback_rebound": ("pullback", "rebound"),
    }
    for fee in FEES:
        simulations = {
            name: simulate(sessions, candidates, bars, strategies, fee)
            for name, strategies in strategy_sets.items()
        }
        runs[str(fee)] = {
            name: summarize(sim, tuple(sim[3][0]["pnl"]), market) for name, sim in simulations.items()
        }
        correlations[str(fee)] = {
            name: compare_daily(simulations["pullback"], simulations[name], market)
            for name in ("range", "rebound")
        }
        if fee == FEES[0]:
            validate_pullback_baseline(runs[str(fee)]["pullback"])
    overlap = {}
    pullback = {(r["signal_date"], r["code"]) for r in candidates if r["strategy"] == "pullback"}
    for strategy in ("range", "rebound"):
        other = {(r["signal_date"], r["code"]) for r in candidates if r["strategy"] == strategy}
        overlap[strategy] = {
            "signal_date_code": len(pullback & other),
            "signal_days": len({d for d, _ in pullback} & {d for d, _ in other}),
        }
    followup = {}
    for name, stops in (("original", ("range", "rebound")), ("no_stops", ()),
                        ("all_stops", STRATEGIES)):
        portfolio = {
            key: simulate(sessions, candidates, bars, strategies, FEES[0], stops)
            for key, strategies in strategy_sets.items()
        }
        followup[name] = {
            "unconstrained_candidates": candidate_returns(sessions, candidates, bars, FEES[0], stops),
            "portfolios": {key: summarize(sim, strategy_sets[key], market) for key, sim in portfolio.items()},
        }
    ranking = {
        strategy: {
            "candidates": len(rows),
            "first_signal_date": str(min(row["signal_date"] for row in rows)) if rows else None,
            "median_rank": median(row["merged_rank"] for row in rows) if rows else None,
            "rank_at_most_10": sum(row["merged_rank"] <= 10 for row in rows),
        }
        for strategy in STRATEGIES for rows in [[r for r in candidates if r["strategy"] == strategy]]
    }
    report = {
        "trial": "review_followup_v1", "input_sha256": db_hash, "script_sha256": digest(Path(__file__)),
        "period": [str(sessions[0]), str(sessions[-1])], "sessions": len(sessions),
        "candidate_counts": dict(Counter(r["strategy"] for r in candidates)),
        "candidate_overlap": overlap, "missing_indicators_among_ranked": missing,
        "runs": runs, "daily_standalone_correlations": correlations, "followup": followup,
        "candidate_ranking": ranking,
    }
    output.mkdir(parents=True, exist_ok=False)
    with (output / "report.json").open("x", encoding="utf-8") as stream:
        json.dump(report, stream, indent=2, allow_nan=False)
    print(f"Saved aggregate report: {output / 'report.json'}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DB)
    parser.add_argument("--output", type=Path, default=OUTPUT)
    parser.add_argument("--reference", type=Path, default=REFERENCE)
    args = parser.parse_args()
    run(args.db, args.output, args.reference)
