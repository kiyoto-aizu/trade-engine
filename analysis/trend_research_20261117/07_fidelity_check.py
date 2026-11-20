"""07: 近似パイプラインと実運用結果の忠実度チェック（レビュー指摘9対応）。

入力: data/screening/YYYY-MM-DD.json（上位50。日付=スクリーニング実行日=前営業日）
      data/filtering/YYYY-MM-DD.json（上位10。日付=取引日）
      trend_research.duckdb（panel, bars_daily, minute_day_summary, universe_master）
出力: data/analysis/trend_research_20261117/07_fidelity_check.json

本調査の近似再現（前日ランキング順位和上位50 → 朝サージ上位10）が、実運用の
スクリーニング・フィルタリング結果とどの程度一致するかを測る。
既知の差分: 実運用はYahoo日足・板・規制/市場チェックを使い、本再現はJ-Quants日足・分足Vaのみ。
"""
from __future__ import annotations

import json
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from common import (
    ANALYSIS_DATA_CUTOFF,
    REPO_ROOT,
    connect,
    current_parquet_paths,
    rows_to_dicts,
    write_json,
)

ROUND_TRIP_COST = 0.0021
ACTUAL_DATA_CUTOFF = "2026-11-18"


def load_symbol_files(
    directory: Path, through_date: str | None = None
) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for f in sorted(directory.glob("????-??-??.json")):
        if through_date is not None and f.stem > through_date:
            continue
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            continue
        if isinstance(d, dict) and isinstance(d.get("symbols"), list):
            out[f.stem] = [str(s) for s in d["symbols"]]
    return out


def load_screening_audit(
    directory: Path, through_date: str | None = None
) -> list[tuple]:
    rows = []
    for f in sorted(directory.glob("????-??-??.json")):
        if through_date is not None and f.stem > through_date:
            continue
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            continue
        if not isinstance(data, dict):
            continue
        for item in data.get("audit_entries", []):
            if not isinstance(item, dict) or item.get("symbol") is None:
                continue
            rows.append((
                f.stem,
                str(item["symbol"]),
                item.get("total_rank"),
                item.get("turnover_rank"),
                item.get("price_gain_rank"),
                item.get("is_restricted"),
                item.get("selected"),
            ))
    return rows


def percentile(values: list[float], probability: float) -> float:
    ordered = sorted(values)
    if not ordered:
        raise ValueError("percentile requires at least one value")
    position = (len(ordered) - 1) * probability
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    weight = position - lower
    return ordered[lower] * (1 - weight) + ordered[upper] * weight


def bootstrap_daily_cluster_ci(
    con,
    strict_fill: bool = False,
    excluded: bool | None = None,
    round_trip_cost: float = 0.0,
) -> tuple[list[float] | None, int]:
    fill_filter = "AND strict_fill" if strict_fill else ""
    exclusion_filter = ""
    if excluded is not None:
        exclusion_filter = f"AND excluded_by_known_conditions IS {str(excluded).upper()}"
    daily_returns = rows_to_dicts(con.sql(f"""
        SELECT date,
               SUM(return_0935_1525 - {round_trip_cost}) AS return_sum,
               COUNT(*) AS observations
        FROM actual_filtering_outcomes
        WHERE return_0935_1525 IS NOT NULL
          {fill_filter}
          {exclusion_filter}
        GROUP BY date ORDER BY date
    """))
    if not daily_returns:
        return None, 0

    rng = random.Random(71)
    bootstrap_means = []
    for _ in range(10_000):
        sample = [daily_returns[rng.randrange(len(daily_returns))] for _ in daily_returns]
        bootstrap_means.append(
            sum(row["return_sum"] for row in sample)
            / sum(row["observations"] for row in sample)
        )
    return [
        round(percentile(bootstrap_means, 0.025), 5),
        round(percentile(bootstrap_means, 0.975), 5),
    ], len(daily_returns)


def main() -> None:
    con = connect()
    out: dict = {}

    screening_dir = REPO_ROOT / "data" / "screening"
    filtering_dir = REPO_ROOT / "data" / "filtering"
    screening = load_symbol_files(screening_dir, ACTUAL_DATA_CUTOFF)
    filtering = load_symbol_files(filtering_dir, ACTUAL_DATA_CUTOFF)
    out["actual_files_cutoff"] = ACTUAL_DATA_CUTOFF
    out["actual_files"] = {
        "screening_days": len(screening),
        "screening_range": [min(screening), max(screening)] if screening else None,
        "filtering_days": len(filtering),
        "filtering_range": [min(filtering), max(filtering)] if filtering else None,
        "screening_symbols": sum(map(len, screening.values())),
        "filtering_symbols": sum(map(len, filtering.values())),
        "screening_files_after_cutoff": sorted(
            f.stem for f in screening_dir.glob("????-??-??.json")
            if f.stem > ACTUAL_DATA_CUTOFF
        ),
        "filtering_files_after_cutoff": sorted(
            f.stem for f in filtering_dir.glob("????-??-??.json")
            if f.stem > ACTUAL_DATA_CUTOFF
        ),
    }

    # 実運用結果を一時テーブル化
    con.execute("CREATE OR REPLACE TEMP TABLE actual_screening (prev_date DATE, symbol VARCHAR)")
    con.executemany(
        "INSERT INTO actual_screening VALUES (?, ?)",
        [(d, s) for d, syms in screening.items() for s in syms],
    )
    con.execute("CREATE OR REPLACE TEMP TABLE actual_filtering (date DATE, symbol VARCHAR)")
    con.executemany(
        "INSERT INTO actual_filtering VALUES (?, ?)",
        [(d, s) for d, syms in filtering.items() for s in syms],
    )
    con.execute("CREATE OR REPLACE TEMP TABLE actual_filtering_days (date DATE)")
    con.executemany(
        "INSERT INTO actual_filtering_days VALUES (?)",
        [(d,) for d in filtering],
    )
    filtering_dates = set(filtering)
    minute_paths = [
        path for date, path in current_parquet_paths(
            "minute", ANALYSIS_DATA_CUTOFF
        )
        if date in filtering_dates
    ]
    con.execute("""
        CREATE OR REPLACE TEMP TABLE actual_exit_times AS
        SELECT b.Code AS code, b.Date AS date,
               arg_max(
                 (b.Time AT TIME ZONE 'Asia/Tokyo')::TIME, b.Time
               ) FILTER (
                 (b.Time AT TIME ZONE 'Asia/Tokyo')::TIME <= TIME '15:25'
               ) AS close_1525_time
        FROM read_parquet(?) b
        JOIN actual_filtering a
          ON b.Date = a.date AND substr(b.Code, 1, 4) = a.symbol
         AND substr(b.Code, 5, 1) = '0'
        GROUP BY 1, 2
    """, [minute_paths])
    con.execute("""
        CREATE OR REPLACE TEMP TABLE actual_screening_audit (
          prev_date DATE, symbol VARCHAR, total_rank DOUBLE, turnover_rank DOUBLE,
          price_gain_rank DOUBLE, is_restricted BOOLEAN, selected BOOLEAN
        )
    """)
    con.executemany(
        "INSERT INTO actual_screening_audit VALUES (?, ?, ?, ?, ?, ?, ?)",
        load_screening_audit(screening_dir, ACTUAL_DATA_CUTOFF),
    )
    con.execute("""
        CREATE OR REPLACE TEMP VIEW actual_filtering_outcomes AS
        SELECT a.date, a.symbol,
               p.code IS NOT NULL AS has_previous_daily_data,
               p.price_close AS previous_close,
               p.ul AS prior_limit_up, p.ret20 AS prior_20d_return,
               m.open_day / NULLIF(p.price_close, 0) - 1 AS opening_gap,
               m.close_pre0930 / NULLIF(p.price_close, 0) - 1 AS move_at_0930,
               m.entry_0935_close, m.close_1525,
               m.entry_0935_close / NULLIF(p.price_close, 0) - 1 AS move_at_0935,
               m.close_1525 / NULLIF(m.entry_0935_close, 0) - 1 AS return_0935_1525,
               m.entry_time, x.close_1525_time,
               (m.entry_0935_close IS NOT NULL AND m.close_1525 IS NOT NULL
                 AND m.entry_time <= TIME '09:45'
                 AND x.close_1525_time >= TIME '15:00') AS strict_fill,
               (p.ul = 1 OR p.ret20 >= 0.25
                 OR m.close_pre0930 / NULLIF(p.price_close, 0) - 1 > 0.02)
                 AS excluded_by_known_conditions
        FROM actual_filtering a
        LEFT JOIN panel p
          ON substr(p.code, 1, 4) = a.symbol AND substr(p.code, 5, 1) = '0'
         AND p.date_t1 = a.date
        LEFT JOIN minute_day_summary m ON m.code = p.code AND m.date = a.date
        LEFT JOIN actual_exit_times x ON x.code = m.code AND x.date = m.date
    """)

    out["filtering_screening_date_alignment"] = rows_to_dicts(con.sql(f"""
        WITH cal AS (
          SELECT date, lag(date) OVER (ORDER BY date) AS prev_market_date
          FROM (SELECT DISTINCT date FROM market_state)
        ), mapped AS (
          SELECT f.date, c.prev_market_date, s.prev_date AS matched_screening_date
          FROM actual_filtering_days f
          LEFT JOIN cal c USING (date)
          LEFT JOIN (SELECT DISTINCT prev_date FROM actual_screening) s
            ON s.prev_date = c.prev_market_date
          WHERE f.date <= DATE '{ANALYSIS_DATA_CUTOFF}'
        )
        SELECT COUNT(*) AS filtering_dates,
               COUNT(*) FILTER (prev_market_date IS NOT NULL) AS dates_with_market_calendar,
               COUNT(*) FILTER (matched_screening_date = prev_market_date) AS dates_with_prior_session_screening,
               COUNT(*) FILTER (matched_screening_date IS NULL) AS dates_without_match
        FROM mapped
    """))[0]
    out["filtering_screening_date_alignment"]["analysis_data_cutoff"] = (
        ANALYSIS_DATA_CUTOFF
    )
    alignment_details = rows_to_dicts(con.sql(f"""
        WITH cal AS (
          SELECT date, lag(date) OVER (ORDER BY date) AS prev_market_date
          FROM (SELECT DISTINCT date FROM market_state)
        )
        SELECT f.date::VARCHAR AS date,
               CASE
                 WHEN f.date > DATE '{ANALYSIS_DATA_CUTOFF}'
                   THEN 'excluded_by_analysis_cutoff'
                 WHEN c.prev_market_date IS NULL
                   THEN 'no_market_calendar_record'
                 ELSE 'missing_prior_session_screening_file'
               END AS reason
        FROM actual_filtering_days f
        LEFT JOIN cal c USING (date)
        LEFT JOIN (SELECT DISTINCT prev_date FROM actual_screening) s
          ON s.prev_date = c.prev_market_date
        WHERE s.prev_date IS NULL
        ORDER BY f.date
    """))
    out["filtering_screening_date_alignment"]["dates_without_match_list"] = [
        row["date"] for row in alignment_details
        if row["reason"] != "excluded_by_analysis_cutoff"
    ]
    out["filtering_screening_date_alignment"]["dates_without_match_reasons"] = [
        row for row in alignment_details
        if row["reason"] != "excluded_by_analysis_cutoff"
    ]
    out["filtering_screening_date_alignment"]["dates_excluded_by_analysis_cutoff_list"] = [
        row for row in alignment_details
        if row["reason"] == "excluded_by_analysis_cutoff"
    ]
    out["filtering_screening_date_alignment"]["dates_excluded_by_analysis_cutoff"] = (
        len(out["filtering_screening_date_alignment"]["dates_excluded_by_analysis_cutoff_list"])
    )
    out["filtering_files_excluded_by_analysis_cutoff"] = []
    available_daily_dates = {
        date for date, _ in current_parquet_paths("daily")
    }
    available_minute_dates = {
        date for date, _ in current_parquet_paths("minute")
    }
    for date, symbols in filtering.items():
        if date <= ANALYSIS_DATA_CUTOFF:
            continue
        out["filtering_files_excluded_by_analysis_cutoff"].append({
            "date": date,
            "symbols_in_filter_file": len(symbols),
            "daily_data_available": date in available_daily_dates,
            "minute_data_available": date in available_minute_dates,
            "analysis_data_cutoff": ANALYSIS_DATA_CUTOFF,
            "interpretation": "excluded to preserve the fixed analysis window; not a non-market day",
        })

    non_market_filter_dates = rows_to_dicts(con.sql(f"""
        SELECT f.date::VARCHAR AS date
        FROM actual_filtering_days f
        LEFT JOIN market_state ms USING (date)
        WHERE ms.date IS NULL
          AND f.date <= DATE '{ANALYSIS_DATA_CUTOFF}'
        ORDER BY f.date
    """))
    out["filtering_files_without_market_session"] = []
    for row in non_market_filter_dates:
        date = row["date"]
        filtering_path = REPO_ROOT / "data" / "filtering" / f"{date}.json"
        screening_path = REPO_ROOT / "data" / "screening" / f"{date}.json"
        payload = json.loads(filtering_path.read_text(encoding="utf-8"))
        daily_rows = con.execute(
            "SELECT COUNT(*) FROM bars_daily WHERE date = ?", [date]
        ).fetchone()[0]
        minute_symbol_days = con.execute(
            "SELECT COUNT(*) FROM minute_day_summary WHERE date = ?", [date]
        ).fetchone()[0]
        out["filtering_files_without_market_session"].append({
            "date": date,
            "symbols_in_filter_file": len(payload.get("symbols", [])),
            "generated_at": payload.get("generated_at"),
            "same_date_screening_file_exists": screening_path.is_file(),
            "daily_rows": daily_rows,
            "minute_symbol_days": minute_symbol_days,
            "interpretation": "file presence alone does not establish that the live filter or orders ran",
        })

    # 近似スクリーニング（04/05と同一。価格上限900円・順位和上位50）
    con.execute("""
        CREATE OR REPLACE TEMP VIEW approx AS
        WITH prev AS (
          SELECT p.code, p.date AS prev_date, p.date_t1 AS date,
                 p.va AS prev_va, p.ret1 AS prev_ret1, p.va_avg20
          FROM panel p
          WHERE substr(p.code, 1, 4) IN (SELECT symbol FROM universe_master)
            AND substr(p.code, 5, 1) = '0'
            AND p.price_close <= 900 AND p.va_avg20 > 0 AND p.date_t1 IS NOT NULL
        ), ranked AS (
          SELECT *,
                 rank() OVER (PARTITION BY prev_date ORDER BY prev_va DESC) AS r_va,
                 rank() OVER (PARTITION BY prev_date ORDER BY prev_ret1 DESC NULLS LAST) AS r_up
          FROM prev
        ), screened AS (
          SELECT *, rank() OVER (PARTITION BY prev_date ORDER BY (r_va + r_up)) AS r_merge
          FROM ranked
        )
        SELECT s.*, substr(s.code, 1, 4) AS symbol4,
               m.va_morning / s.va_avg20 AS morning_surge
        FROM screened s
        LEFT JOIN minute_day_summary m ON m.code = s.code AND m.date = s.date
        WHERE s.r_merge <= 50
    """)
    con.execute("""
        CREATE OR REPLACE TEMP VIEW approx_top10 AS
        SELECT date, symbol4
        FROM (
          SELECT date, symbol4,
                 rank() OVER (PARTITION BY date ORDER BY morning_surge DESC NULLS LAST) AS r_surge
          FROM approx WHERE morning_surge IS NOT NULL
        )
        WHERE r_surge <= 10
    """)

    # スクリーニング一致率: 実運用の各日の50銘柄のうち、近似上位50に含まれた割合
    out["screening_overlap"] = rows_to_dicts(con.sql("""
        WITH comparable_actual AS (
          SELECT * FROM actual_screening
          WHERE prev_date IN (SELECT DISTINCT prev_date FROM approx)
        ), per_day AS (
          SELECT a.prev_date,
                 COUNT(*) AS actual_n,
                 COUNT(ap.symbol4) AS matched
          FROM comparable_actual a
          LEFT JOIN approx ap ON ap.prev_date = a.prev_date AND ap.symbol4 = a.symbol
          GROUP BY 1
        )
        SELECT COUNT(*) AS days,
               ROUND(AVG(matched * 1.0 / actual_n), 4) AS mean_overlap,
               ROUND(quantile_cont(matched * 1.0 / actual_n, 0.25), 4) AS p25,
               ROUND(quantile_cont(matched * 1.0 / actual_n, 0.5), 4) AS median,
               ROUND(quantile_cont(matched * 1.0 / actual_n, 0.75), 4) AS p75
        FROM per_day WHERE actual_n > 0
    """))[0]
    out["actual_screening_rank_distribution"] = rows_to_dicts(con.sql("""
        SELECT COUNT(*) AS selected_symbols,
               COUNT(DISTINCT s.prev_date) AS days,
               COUNT(a.total_rank) AS symbols_with_rank_audit,
               ROUND(AVG(a.total_rank), 2) AS mean_total_rank,
               ROUND(quantile_cont(a.total_rank, 0.5), 2) AS median_total_rank,
               ROUND(quantile_cont(a.total_rank, 0.25), 2) AS p25_total_rank,
               ROUND(quantile_cont(a.total_rank, 0.75), 2) AS p75_total_rank,
               COUNT(*) FILTER (a.is_restricted IS TRUE) AS restricted_in_selected,
               COUNT(*) FILTER (a.selected IS TRUE) AS audit_selected_matches
        FROM actual_screening s
        LEFT JOIN actual_screening_audit a USING (prev_date, symbol)
        WHERE s.prev_date IN (SELECT DISTINCT prev_date FROM approx)
    """))[0]
    out["actual_screening_rank_distribution"]["total_rank_definition"] = (
        "audit total_rank is turnover_rank + price_gain_rank; it is not the final selected-list position. "
        "The selected flag is recorded separately after candidate filtering and limiting."
    )

    # フィルタリング一致率: 実運用の各日の10銘柄のうち、近似の朝サージ上位10に含まれた割合
    out["filtering_overlap"] = rows_to_dicts(con.sql("""
        WITH per_day AS (
          SELECT a.date, COUNT(*) AS actual_n, COUNT(t.symbol4) AS matched
          FROM actual_filtering a
          LEFT JOIN approx_top10 t ON t.date = a.date AND t.symbol4 = a.symbol
          GROUP BY 1
        )
        SELECT COUNT(*) AS days,
               ROUND(AVG(matched * 1.0 / actual_n), 4) AS mean_overlap,
               ROUND(quantile_cont(matched * 1.0 / actual_n, 0.5), 4) AS median
        FROM per_day WHERE actual_n > 0
    """))[0]
    out["filtering_overlap_by_session_shift"] = rows_to_dicts(con.sql("""
        WITH cal AS (
          SELECT date, lag(date) OVER (ORDER BY date) AS prev_date,
                 lead(date) OVER (ORDER BY date) AS next_date
          FROM (SELECT DISTINCT date FROM market_state)
        ), targets AS (
          SELECT f.date AS actual_date, c.prev_date, c.date AS same_date, c.next_date
          FROM actual_filtering_days f LEFT JOIN cal c USING (date)
          WHERE c.prev_date IS NOT NULL AND c.next_date IS NOT NULL
        ), shifts AS (
          SELECT actual_date, '-1' AS session_shift, prev_date AS approx_date FROM targets
          UNION ALL
          SELECT actual_date, '0', same_date FROM targets
          UNION ALL
          SELECT actual_date, '+1', next_date FROM targets
        ), per_day AS (
          SELECT s.session_shift, s.actual_date,
                 COUNT(*) AS actual_n, COUNT(t.symbol4) AS matched
          FROM shifts s
          JOIN actual_filtering a ON a.date = s.actual_date
          LEFT JOIN approx_top10 t
            ON t.date = s.approx_date AND t.symbol4 = a.symbol
          WHERE s.approx_date IS NOT NULL
          GROUP BY 1, 2
        )
        SELECT session_shift, COUNT(*) AS days,
               ROUND(AVG(matched * 1.0 / actual_n), 4) AS mean_overlap,
               ROUND(quantile_cont(matched * 1.0 / actual_n, 0.5), 4) AS median_overlap
        FROM per_day GROUP BY 1 ORDER BY 1
    """))

    # 実運用フィルタ10銘柄が、近似スクリーニング上位50に入っていた割合（段階別の切り分け）
    out["filtering_in_approx_top50"] = rows_to_dicts(con.sql("""
        WITH per_day AS (
          SELECT a.date, COUNT(*) AS actual_n, COUNT(ap.symbol4) AS matched
          FROM actual_filtering a
          LEFT JOIN approx ap ON ap.date = a.date AND ap.symbol4 = a.symbol
          GROUP BY 1
        )
        SELECT COUNT(*) AS days,
               ROUND(AVG(matched * 1.0 / actual_n), 4) AS mean_overlap,
               ROUND(quantile_cont(matched * 1.0 / actual_n, 0.5), 4) AS median
        FROM per_day WHERE actual_n > 0
    """))[0]

    # 実運用選択の価格系列成績（実約定記録ではなく、9:35→15:25のバー価格）
    out["actual_filtering_performance"] = rows_to_dicts(con.sql("""
        SELECT COUNT(*) AS n, COUNT(DISTINCT date) AS days,
               ROUND(AVG(return_0935_1525), 5) AS ret_mean,
               ROUND(quantile_cont(return_0935_1525, 0.5), 5) AS ret_med,
               ROUND(quantile_cont(return_0935_1525, 0.1), 5) AS ret_p10,
               ROUND(quantile_cont(return_0935_1525, 0.9), 5) AS ret_p90,
               ROUND(AVG((return_0935_1525 > 0)::INT), 4) AS win,
               'per selected-symbol observation, before costs' AS return_basis
        FROM actual_filtering_outcomes
        WHERE return_0935_1525 IS NOT NULL
    """))[0]
    ci, ci_days = bootstrap_daily_cluster_ci(con)
    if ci is not None:
        performance = out["actual_filtering_performance"]
        performance["daily_cluster_bootstrap_95ci"] = ci
        performance["bootstrap_days"] = ci_days
        performance["bootstrap_method"] = (
            "10,000 resamples of observed dates with replacement; "
            "pooled per-symbol mean; percentile CI; seed=71"
        )
    performance["after_round_trip_cost"] = rows_to_dicts(con.sql(f"""
        SELECT ROUND(AVG(return_0935_1525 - {ROUND_TRIP_COST}), 5) AS ret_mean,
               ROUND(quantile_cont(return_0935_1525 - {ROUND_TRIP_COST}, 0.5), 5) AS ret_med,
               ROUND(AVG((return_0935_1525 > {ROUND_TRIP_COST})::INT), 4) AS win
        FROM actual_filtering_outcomes WHERE return_0935_1525 IS NOT NULL
    """))[0]
    performance["after_round_trip_cost"]["round_trip_cost"] = ROUND_TRIP_COST
    performance["after_round_trip_cost"]["daily_cluster_bootstrap_95ci"] = (
        bootstrap_daily_cluster_ci(con, round_trip_cost=ROUND_TRIP_COST)[0]
    )

    out["actual_filtering_strict_fill_performance"] = rows_to_dicts(con.sql("""
        SELECT COUNT(*) AS n, COUNT(DISTINCT date) AS days,
               ROUND(AVG(return_0935_1525), 5) AS ret_mean,
               ROUND(quantile_cont(return_0935_1525, 0.5), 5) AS ret_med,
               ROUND(AVG((return_0935_1525 > 0)::INT), 4) AS win,
               'per selected-symbol observation, before costs' AS return_basis,
               'entry by 09:45 and bar used for 15:25 exit price at/after 15:00' AS fill_definition
        FROM actual_filtering_outcomes
        WHERE return_0935_1525 IS NOT NULL AND strict_fill
    """))[0]
    strict_ci, strict_days = bootstrap_daily_cluster_ci(con, strict_fill=True)
    if strict_ci is not None:
        strict_performance = out["actual_filtering_strict_fill_performance"]
        strict_performance["daily_cluster_bootstrap_95ci"] = strict_ci
        strict_performance["bootstrap_days"] = strict_days
        strict_performance["bootstrap_method"] = (
            "10,000 resamples of observed dates with replacement; "
            "pooled per-symbol mean; percentile CI; seed=71"
        )
    strict_performance["after_round_trip_cost"] = rows_to_dicts(con.sql(f"""
        SELECT ROUND(AVG(return_0935_1525 - {ROUND_TRIP_COST}), 5) AS ret_mean,
               ROUND(quantile_cont(return_0935_1525 - {ROUND_TRIP_COST}, 0.5), 5) AS ret_med,
               ROUND(AVG((return_0935_1525 > {ROUND_TRIP_COST})::INT), 4) AS win
        FROM actual_filtering_outcomes
        WHERE return_0935_1525 IS NOT NULL AND strict_fill
    """))[0]
    strict_performance["after_round_trip_cost"]["round_trip_cost"] = ROUND_TRIP_COST
    strict_performance["after_round_trip_cost"]["daily_cluster_bootstrap_95ci"] = (
        bootstrap_daily_cluster_ci(
            con, strict_fill=True, round_trip_cost=ROUND_TRIP_COST
        )[0]
    )

    out["actual_filtering_exclusion_performance"] = rows_to_dicts(con.sql(f"""
        SELECT excluded_by_known_conditions,
               COUNT(*) AS n, COUNT(DISTINCT date) AS days,
               ROUND(AVG(return_0935_1525), 5) AS ret_mean_before_cost,
               ROUND(quantile_cont(return_0935_1525, 0.5), 5) AS ret_median_before_cost,
               ROUND(quantile_cont(return_0935_1525, 0.1), 5) AS ret_p10_before_cost,
               ROUND(AVG((return_0935_1525 > 0)::INT), 4) AS win_before_cost,
               ROUND(AVG(return_0935_1525 - {ROUND_TRIP_COST}), 5) AS ret_mean_after_cost,
               ROUND(AVG((return_0935_1525 > {ROUND_TRIP_COST})::INT), 4) AS win_after_cost
        FROM actual_filtering_outcomes
        WHERE return_0935_1525 IS NOT NULL
          AND excluded_by_known_conditions IS NOT NULL
        GROUP BY 1 ORDER BY 1
    """))
    for group in out["actual_filtering_exclusion_performance"]:
        is_excluded = group["excluded_by_known_conditions"]
        group["round_trip_cost"] = ROUND_TRIP_COST
        group["daily_cluster_bootstrap_95ci_before_cost"] = (
            bootstrap_daily_cluster_ci(con, excluded=is_excluded)[0]
        )
        group["daily_cluster_bootstrap_95ci_after_cost"] = (
            bootstrap_daily_cluster_ci(
                con, excluded=is_excluded, round_trip_cost=ROUND_TRIP_COST
            )[0]
        )
        group["bootstrap_method"] = (
            "10,000 resamples of observed dates with replacement; "
            "pooled per-symbol mean; percentile CI; seed=71"
        )

    out["actual_filtering_exclusion_exposure"] = rows_to_dicts(con.sql("""
        SELECT COUNT(*) AS selected_symbol_rows,
               COUNT(*) FILTER (has_previous_daily_data) AS rows_with_previous_daily_data,
               COUNT(*) FILTER (
                 entry_0935_close IS NOT NULL AND close_1525 IS NOT NULL
               ) AS rows_with_entry_exit_prices,
               COUNT(*) FILTER (strict_fill) AS rows_meeting_strict_fill_time,
               COUNT(*) FILTER (prior_limit_up = 1) AS prior_limit_up_rows,
               COUNT(*) FILTER (prior_20d_return >= 0.25) AS prior_20d_return_ge25_rows,
               COUNT(*) FILTER (opening_gap > 0.02) AS opening_gap_gt2_rows,
               COUNT(*) FILTER (excluded_by_known_conditions IS TRUE) AS any_known_by_0930_exclusion_rows,
               COUNT(*) FILTER (move_at_0930 > 0.02) AS move_gt2_at_0930_rows,
               COUNT(*) FILTER (move_at_0935 > 0.02) AS move_gt2_at_0935_rows,
               COUNT(DISTINCT date) FILTER (
                 entry_0935_close IS NOT NULL AND close_1525 IS NOT NULL
               ) AS outcome_days
        FROM actual_filtering_outcomes
    """))[0]

    write_json("07_fidelity_check.json", out)
    con.close()


if __name__ == "__main__":
    main()
