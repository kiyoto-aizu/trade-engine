"""01: データ棚卸しと品質実測。

入力: J-Quants canonical 日足・分足 parquet、data/universe/listed_securities.csv
出力: data/analysis/trend_research_20261117/
  - trend_research.duckdb（bars_daily, minute_day_summary ほか）
  - 01_data_quality.json（品質集計）
"""
from __future__ import annotations

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


def main() -> None:
    con = connect()
    con.execute("SET TimeZone='Asia/Tokyo'")
    report: dict = {}

    available_daily = current_parquet_paths("daily")
    available_minute = current_parquet_paths("minute")
    daily = current_parquet_paths("daily", ANALYSIS_DATA_CUTOFF)
    minute = current_parquet_paths("minute", ANALYSIS_DATA_CUTOFF)
    report["analysis_data_cutoff"] = ANALYSIS_DATA_CUTOFF
    report["available_daily_dates"] = {
        "count": len(available_daily),
        "first": available_daily[0][0],
        "last": available_daily[-1][0],
    }
    report["available_minute_dates"] = {
        "count": len(available_minute),
        "first": available_minute[0][0],
        "last": available_minute[-1][0],
    }
    report["daily_dates"] = {"count": len(daily), "first": daily[0][0], "last": daily[-1][0]}
    report["minute_dates"] = {"count": len(minute), "first": minute[0][0], "last": minute[-1][0]}

    daily_paths = [p for _, p in daily]
    minute_paths = [p for _, p in minute]

    # --- 日足を materialize ---
    con.execute("DROP TABLE IF EXISTS bars_daily")
    con.execute(
        """
        CREATE TABLE bars_daily AS
        SELECT Code AS code, Date AS date, O AS o, H AS h, L AS l, C AS c,
               Vo AS vo, Va AS va, AdjFactor AS adj_factor,
               AdjO AS adj_o, AdjH AS adj_h, AdjL AS adj_l, AdjC AS adj_c, AdjVo AS adj_vo,
               UL AS ul, LL AS ll
        FROM read_parquet(?)
        """,
        [daily_paths],
    )
    report["bars_daily"] = rows_to_dicts(
        con.sql(
            """
            SELECT COUNT(*) AS rows, COUNT(DISTINCT code) AS codes,
                   MIN(date) AS min_date, MAX(date) AS max_date,
                   COUNT(*) FILTER (c IS NULL) AS null_close,
                   COUNT(*) FILTER (o IS NULL) AS null_open,
                   COUNT(*) FILTER (o IS NULL OR h IS NULL OR l IS NULL OR c IS NULL) AS null_ohlc,
                   COUNT(*) FILTER (va IS NULL) AS null_va,
                   COUNT(*) FILTER (vo = 0) AS zero_volume,
                   COUNT(*) FILTER (
                     h < GREATEST(o, c) OR l > LEAST(o, c) OR h < l
                   ) AS invalid_ohlc,
                   COUNT(*) FILTER (o <= 0 OR h <= 0 OR l <= 0 OR c <= 0) AS nonpositive_ohlc,
                   COUNT(*) FILTER (vo < 0 OR va < 0) AS negative_volume_or_value,
                   COUNT(*) FILTER (ul = 1) AS limit_up,
                   COUNT(*) FILTER (ll = 1) AS limit_down
            FROM bars_daily
            """
        )
    )[0]
    report["daily_duplicates"] = con.sql(
        "SELECT COUNT(*) FROM (SELECT code, date FROM bars_daily GROUP BY 1,2 HAVING COUNT(*)>1)"
    ).fetchone()[0]
    report["daily_codes_per_date"] = rows_to_dicts(
        con.sql(
            """
            WITH per_date AS (SELECT date, COUNT(DISTINCT code) AS codes FROM bars_daily GROUP BY 1)
            SELECT MIN(codes) AS min, quantile_cont(codes, 0.5) AS median, MAX(codes) AS max FROM per_date
            """
        )
    )[0]
    report["daily_codes_by_year"] = rows_to_dicts(
        con.sql(
            """
            SELECT year(date) AS year, COUNT(DISTINCT date) AS trading_days,
                   ROUND(AVG(codes),0) AS avg_codes
            FROM (SELECT date, COUNT(DISTINCT code) AS codes FROM bars_daily GROUP BY 1)
            GROUP BY 1 ORDER BY 1
            """
        )
    )
    # 平日でデータが無い日（祝日を含むため参考値）
    report["weekday_gaps"] = con.sql(
        """
        WITH cal AS (
          SELECT unnest(generate_series(DATE '2021-11-04', DATE '2026-11-16', INTERVAL 1 DAY))::DATE AS d
        ), days AS (SELECT DISTINCT date FROM bars_daily)
        SELECT COUNT(*) FROM cal
        WHERE dayofweek(d) BETWEEN 1 AND 5 AND d NOT IN (SELECT date FROM days)
        """
    ).fetchone()[0]

    # --- 上場マスタとの突合（canonical Code は5桁、マスタは4桁） ---
    uni = (REPO_ROOT / "data" / "universe" / "listed_securities.csv").as_posix()
    con.execute("DROP TABLE IF EXISTS universe_master")
    con.execute(
        f"""
        CREATE TABLE universe_master AS
        SELECT symbol, exchange_division, listed_from::DATE AS listed_from,
               NULLIF(listed_to, '')::DATE AS listed_to
        FROM read_csv('{uni}', header=true, all_varchar=true)
        """
    )
    report["universe_master"] = rows_to_dicts(
        con.sql(
            """
            SELECT COUNT(*) AS rows, COUNT(DISTINCT symbol) AS symbols,
                   COUNT(*) FILTER (listed_to IS NOT NULL) AS delisted_rows,
                   COUNT(DISTINCT exchange_division) AS divisions
            FROM universe_master
            """
        )
    )[0]
    report["exchange_divisions"] = rows_to_dicts(
        con.sql("SELECT exchange_division, COUNT(*) AS n FROM universe_master GROUP BY 1 ORDER BY 2 DESC")
    )
    report["daily_codes_not_in_master"] = con.sql(
        """
        SELECT COUNT(DISTINCT code) FROM bars_daily
        WHERE substr(code,1,4) NOT IN (SELECT symbol FROM universe_master)
        """
    ).fetchone()[0]
    report["master_not_in_daily"] = con.sql(
        """
        SELECT COUNT(DISTINCT symbol) FROM universe_master
        WHERE symbol NOT IN (SELECT substr(code,1,4) FROM bars_daily)
        """
    ).fetchone()[0]

    # --- 分足: 日×銘柄サマリを materialize（後段の分析でも使用） ---
    con.execute("DROP TABLE IF EXISTS minute_day_summary")
    con.execute(
        """
        CREATE TABLE minute_day_summary AS
        SELECT
          Code AS code,
          Date AS date,
          COUNT(*) AS bars,
          MIN(Time AT TIME ZONE 'Asia/Tokyo') AS first_time,
          MAX(Time AT TIME ZONE 'Asia/Tokyo') AS last_time,
          SUM(Va) AS va_total,
          SUM(Vo) AS vo_total,
          SUM(Va) FILTER ((Time AT TIME ZONE 'Asia/Tokyo')::TIME < TIME '09:30') AS va_morning,
          SUM(Vo) FILTER ((Time AT TIME ZONE 'Asia/Tokyo')::TIME < TIME '09:30') AS vo_morning,
          arg_max(C, Time) FILTER ((Time AT TIME ZONE 'Asia/Tokyo')::TIME < TIME '09:30') AS close_pre0930,
          arg_max((Time AT TIME ZONE 'Asia/Tokyo')::TIME, Time)
            FILTER ((Time AT TIME ZONE 'Asia/Tokyo')::TIME < TIME '09:30') AS close_pre0930_time,
          arg_min(O, Time) AS open_day,
          arg_min(C, Time) FILTER ((Time AT TIME ZONE 'Asia/Tokyo')::TIME >= TIME '09:35') AS entry_0935_close,
          min((Time AT TIME ZONE 'Asia/Tokyo')::TIME) FILTER ((Time AT TIME ZONE 'Asia/Tokyo')::TIME >= TIME '09:35') AS entry_time,
          arg_max(C, Time) AS close_last,
          arg_max(C, Time) FILTER ((Time AT TIME ZONE 'Asia/Tokyo')::TIME <= TIME '15:25') AS close_1525,
          max(H) FILTER ((Time AT TIME ZONE 'Asia/Tokyo')::TIME >= TIME '09:35') AS high_after_0935,
          min(L) FILTER ((Time AT TIME ZONE 'Asia/Tokyo')::TIME >= TIME '09:35') AS low_after_0935,
          arg_max((Time AT TIME ZONE 'Asia/Tokyo')::TIME, H) AS time_of_high,
          arg_min((Time AT TIME ZONE 'Asia/Tokyo')::TIME, L) AS time_of_low,
          arg_max((Time AT TIME ZONE 'Asia/Tokyo')::TIME, H) FILTER ((Time AT TIME ZONE 'Asia/Tokyo')::TIME >= TIME '09:35') AS time_of_high_after_0935,
          arg_min((Time AT TIME ZONE 'Asia/Tokyo')::TIME, L) FILTER ((Time AT TIME ZONE 'Asia/Tokyo')::TIME >= TIME '09:35') AS time_of_low_after_0935,
          max(H) AS high_day,
          min(L) AS low_day
        FROM read_parquet(?)
        GROUP BY 1, 2
        """,
        [minute_paths],
    )
    report["minute_day_summary"] = rows_to_dicts(
        con.sql(
            """
            SELECT COUNT(*) AS code_days, COUNT(DISTINCT code) AS codes, COUNT(DISTINCT date) AS dates,
                   quantile_cont(bars, 0.5) AS median_bars,
                   quantile_cont(bars, 0.1) AS p10_bars,
                   quantile_cont(bars, 0.9) AS p90_bars,
                   MIN(first_time)::VARCHAR AS min_first_time,
                   MAX(last_time)::VARCHAR AS max_last_time
            FROM minute_day_summary
            """
        )
    )[0]
    report["minute_bar_quality"] = rows_to_dicts(
        con.execute(
            """
            SELECT COUNT(*) AS rows,
                   COUNT(DISTINCT (Code, Date, Time)) AS unique_bar_keys,
                   COUNT(*) FILTER (Code IS NULL OR Date IS NULL OR Time IS NULL) AS null_keys,
                   COUNT(*) FILTER (O IS NULL OR H IS NULL OR L IS NULL OR C IS NULL) AS null_ohlc,
                   COUNT(*) FILTER (Vo IS NULL OR Va IS NULL) AS null_volume_or_value,
                   COUNT(*) FILTER (
                     H < GREATEST(O, C) OR L > LEAST(O, C) OR H < L
                   ) AS invalid_ohlc,
                   COUNT(*) FILTER (O <= 0 OR H <= 0 OR L <= 0 OR C <= 0) AS nonpositive_price,
                   COUNT(*) FILTER (Vo < 0 OR Va < 0) AS negative_volume_or_value
            FROM read_parquet(?)
            """,
            [minute_paths],
        )
    )[0]
    report["minute_bar_quality"]["duplicate_key_excess_rows"] = (
        report["minute_bar_quality"]["rows"]
        - report["minute_bar_quality"]["unique_bar_keys"]
    )
    report["minute_bars_per_day_by_halfyear"] = rows_to_dicts(
        con.sql(
            """
            SELECT strftime(date, '%Y-H') || CASE WHEN month(date)<=6 THEN '1' ELSE '2' END AS half,
                   COUNT(DISTINCT date) AS dates, COUNT(*) AS code_days,
                   ROUND(AVG(bars),1) AS avg_bars, quantile_cont(bars,0.5) AS median_bars
            FROM minute_day_summary GROUP BY 1 ORDER BY 1
            """
        )
    )
    # 分足と日足の日付整合（分足期間中に日足のみ存在する営業日）
    report["minute_missing_vs_daily"] = rows_to_dicts(
        con.sql(
            """
            SELECT date::VARCHAR AS date FROM (SELECT DISTINCT date FROM bars_daily WHERE date >= DATE '2024-11-05')
            WHERE date NOT IN (SELECT DISTINCT date FROM minute_day_summary)
            ORDER BY 1
            """
        )
    )
    # 分足銘柄数と日足銘柄数の日次比較（偏り検知）
    report["minute_vs_daily_code_coverage"] = rows_to_dicts(
        con.sql(
            """
            WITH m AS (SELECT date, COUNT(DISTINCT code) AS m_codes FROM minute_day_summary GROUP BY 1),
                 d AS (SELECT date, COUNT(DISTINCT code) AS d_codes FROM bars_daily WHERE date >= DATE '2024-11-05' GROUP BY 1)
            SELECT MIN(m_codes/d_codes) AS min_ratio, quantile_cont(m_codes/d_codes, 0.5) AS median_ratio,
                   MAX(m_codes/d_codes) AS max_ratio
            FROM m JOIN d USING (date)
            """
        )
    )[0]

    # 調整後価格の連続性チェック。イベント日に raw/adjusted return の差も集計し、
    # 分割比率が小さくても調整が効いているか確認する。
    report["adj_continuity_check"] = rows_to_dicts(
        con.sql(
            """
            WITH r AS (
              SELECT code, date, adj_factor,
                     adj_c / NULLIF(lag(adj_c) OVER (PARTITION BY code ORDER BY date), 0) - 1 AS ret1_adj,
                     c / NULLIF(lag(c) OVER (PARTITION BY code ORDER BY date), 0) - 1 AS ret1_raw
              FROM bars_daily WHERE c IS NOT NULL AND adj_c IS NOT NULL
            )
            SELECT COUNT(*) FILTER (abs(ret1_adj) > 0.5) AS adj_ret_gt50pct,
                   COUNT(*) FILTER (abs(ret1_adj) > 0.5 AND COALESCE(adj_factor,1) <> 1) AS adj_ret_gt50pct_on_event_day,
                   COUNT(*) FILTER (abs(ret1_adj) > 0.5 AND COALESCE(adj_factor,1) = 1) AS adj_ret_gt50pct_off_event_day,
                   COUNT(*) FILTER (abs(ret1_raw) > 0.5) AS raw_ret_gt50pct,
                   COUNT(*) FILTER (COALESCE(adj_factor,1) <> 1) AS event_day_rows,
                   COUNT(*) FILTER (
                     COALESCE(adj_factor,1) <> 1 AND abs(ret1_raw) > 0.5
                   ) AS event_raw_ret_gt50pct,
                   COUNT(*) FILTER (
                     COALESCE(adj_factor,1) <> 1 AND abs(ret1_adj) > 0.5
                   ) AS event_adj_ret_gt50pct,
                   COUNT(*) FILTER (
                     COALESCE(adj_factor,1) <> 1
                     AND abs(ret1_raw - ret1_adj) > 0.05
                   ) AS event_raw_adj_gap_gt5pp,
                   ROUND(quantile_cont(abs(ret1_raw - ret1_adj), 0.5)
                     FILTER (COALESCE(adj_factor,1) <> 1), 5) AS event_raw_adj_gap_median,
                   ROUND(quantile_cont(abs(ret1_raw - ret1_adj), 0.9)
                     FILTER (COALESCE(adj_factor,1) <> 1), 5) AS event_raw_adj_gap_p90
            FROM r
            """
        )
    )[0]
    report["adj_non_event_extremes_by_year"] = rows_to_dicts(
        con.sql(
            """
            WITH r AS (
              SELECT date, COALESCE(adj_factor,1) AS adj_factor,
                     adj_c / NULLIF(lag(adj_c) OVER (PARTITION BY code ORDER BY date), 0) - 1 AS ret1_adj
              FROM bars_daily WHERE c IS NOT NULL AND adj_c IS NOT NULL
            )
            SELECT year(date) AS year, COUNT(*) AS rows,
                   COUNT(*) FILTER (abs(ret1_adj) > 0.5) AS adj_ret_gt50pct_off_event_day
            FROM r
            WHERE adj_factor = 1
            GROUP BY 1 ORDER BY 1
            """
        )
    )

    write_json("01_data_quality.json", report)
    con.close()


if __name__ == "__main__":
    main()
