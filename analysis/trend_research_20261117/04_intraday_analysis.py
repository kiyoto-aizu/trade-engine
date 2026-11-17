"""04: 分足による朝フィルタ再現と日中フォロースルー検証（2024-11-05〜2026-11-16）。

入力: trend_research.duckdb（minute_day_summary, panel, universe_master, market_state）
出力: data/analysis/trend_research_20261117/04_intraday_analysis.json

再現の前提:
  - スクリーニング近似: 前営業日 t-1 時点で 終値<=900円・マスタ在籍・va_avg20>0 の銘柄から、
    売買代金順位と値上がり率順位の順位和上位50銘柄（現行 merge_ranking_candidates の近似）
  - フィルタ近似: 当日 t の 09:00-09:29 売買代金合計 ÷ 20日平均売買代金（t-1まで）で上位10銘柄
    （現行は板の当日累計 9:30 時点。分足合計は近似であり、現行昼フィルタ検証では推定/実測の中央値0.82）
  - エントリー: 09:35 以降最初の分足終値。イグジット: 15:25 までの最後の分足終値（現行EOD 15:20-15:25 決済の近似）
  - コストは本集計では未控除（05 で控除後を扱う）
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from common import connect, rows_to_dicts, write_json

SETUP = """
CREATE OR REPLACE TEMP VIEW cand AS
WITH prev AS (
  SELECT p.code, p.date AS prev_date, p.date_t1 AS date,
         p.price_close AS prev_close, p.va AS prev_va, p.ret1 AS prev_ret1,
         p.ret5, p.ret20, p.rsi14_sma, p.sma5_gap, p.atr14_pct, p.vol20,
         p.va_avg20, p.va_surge AS prev_va_surge, p.ul AS prev_ul,
         p.off_high252, p.up_days_in_5
  FROM panel p
  WHERE substr(p.code, 1, 4) IN (SELECT symbol FROM universe_master)
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
SELECT s.*, m.va_morning, m.vo_morning, m.entry_0935_close, m.close_1525, m.close_last,
       m.high_after_0935, m.low_after_0935, m.time_of_high, m.time_of_low,
       m.open_day, m.bars,
       m.va_morning / s.va_avg20 AS morning_surge,
       m.entry_0935_close / s.prev_close - 1 AS morning_move,
       m.open_day / s.prev_close - 1 AS gap_open,
       m.close_1525 / m.entry_0935_close - 1 AS ret_0935_1525,
       m.high_after_0935 / m.entry_0935_close - 1 AS mfe_0935,
       m.low_after_0935 / m.entry_0935_close - 1 AS mae_0935,
       m.close_1525 / m.open_day - 1 AS ret_0900_1525,
       ms.regime_vol_base, ms.mkt_sma25_slope5, ms.mkt_dd252
FROM screened s
JOIN minute_day_summary m ON m.code = s.code AND m.date = s.date
JOIN market_state ms ON ms.date = s.date
WHERE s.r_merge <= 50;

CREATE OR REPLACE TEMP VIEW sel AS
SELECT *,
       rank() OVER (PARTITION BY date ORDER BY morning_surge DESC NULLS LAST) AS r_surge,
       rank() OVER (PARTITION BY date ORDER BY va_morning DESC NULLS LAST) AS r_va_morning,
       rank() OVER (PARTITION BY date ORDER BY prev_ret1 DESC NULLS LAST) AS r_prev_up,
       CASE WHEN date < DATE '2025-11-16' THEN 'M1(24/11-25/11)' ELSE 'M2(25/11-26/11)' END AS mperiod
FROM cand
WHERE va_morning IS NOT NULL AND entry_0935_close IS NOT NULL AND close_1525 IS NOT NULL;
"""

OUTCOME = """
  COUNT(*) AS n, COUNT(DISTINCT date) AS days,
  ROUND(AVG(ret_0935_1525), 5) AS ret_mean,
  ROUND(quantile_cont(ret_0935_1525, 0.5), 5) AS ret_med,
  ROUND(quantile_cont(ret_0935_1525, 0.1), 5) AS ret_p10,
  ROUND(quantile_cont(ret_0935_1525, 0.9), 5) AS ret_p90,
  ROUND(AVG((ret_0935_1525 > 0)::INT), 4) AS win,
  ROUND(AVG(mfe_0935), 5) AS mfe_mean,
  ROUND(quantile_cont(mfe_0935, 0.5), 5) AS mfe_med,
  ROUND(AVG(mae_0935), 5) AS mae_mean,
  ROUND(quantile_cont(mae_0935, 0.5), 5) AS mae_med
"""


def q(con, sql: str) -> list[dict]:
    return rows_to_dicts(con.sql(sql))


def main() -> None:
    con = connect()
    con.execute("SET TimeZone='Asia/Tokyo'")
    for stmt in SETUP.split(";"):
        if stmt.strip():
            con.execute(stmt)
    out: dict = {}

    out["candidate_counts"] = q(con, """
        SELECT mperiod, COUNT(DISTINCT date) AS days, COUNT(*) AS cand_rows,
               ROUND(AVG(morning_surge), 3) AS surge_mean,
               ROUND(quantile_cont(morning_surge, 0.5), 3) AS surge_med
        FROM sel GROUP BY 1 ORDER BY 1
    """)

    # --- 現行相当: 朝サージ上位10 vs 代替選択 ---
    variants = {
        "V0_top10_surge": "r_surge <= 10",
        "V1_rank11_30_surge": "r_surge BETWEEN 11 AND 30",
        "V2_top10_prev_gainers": "r_prev_up <= 10",
        "V3_top10_va_morning": "r_va_morning <= 10",
        "V4_top10_surge_nogap2": "r_surge <= 10 AND morning_move <= 0.02",
        "V5_top10_surge_no_ul_mom25": "r_surge <= 10 AND prev_ul = 0 AND (ret20 IS NULL OR ret20 < 0.25)",
        "V6_top10_surge_liq50M": "r_surge <= 10 AND va_avg20 >= 5e7",
        "V7_all_candidates": "TRUE",
    }
    for name, cond in variants.items():
        out[f"variant::{name}"] = q(con, f"""
            SELECT mperiod, {OUTCOME}
            FROM sel WHERE {cond} GROUP BY 1 ORDER BY 1
        """)

    # --- サージ順位と成績の関係 ---
    out["surge_rank_buckets"] = q(con, f"""
        SELECT mperiod,
               CASE WHEN r_surge <= 5 THEN 'a:1-5' WHEN r_surge <= 10 THEN 'b:6-10'
                    WHEN r_surge <= 20 THEN 'c:11-20' WHEN r_surge <= 35 THEN 'd:21-35'
                    ELSE 'e:36-50' END AS bucket,
               {OUTCOME}
        FROM sel GROUP BY 1, 2 ORDER BY 1, 2
    """)
    out["surge_level_buckets"] = q(con, f"""
        SELECT CASE WHEN morning_surge < 0.1 THEN 'a:<0.1' WHEN morning_surge < 0.2 THEN 'b:0.1-0.2'
                    WHEN morning_surge < 0.4 THEN 'c:0.2-0.4' WHEN morning_surge < 0.8 THEN 'd:0.4-0.8'
                    ELSE 'e:>=0.8' END AS bucket,
               {OUTCOME}
        FROM sel GROUP BY 1 ORDER BY 1
    """)

    # --- 朝の値動き（ギャップ・9:35までの上昇率）との交互作用 ---
    out["morning_move_buckets"] = q(con, f"""
        SELECT CASE WHEN morning_move < -0.02 THEN 'a:<-2%' WHEN morning_move < 0 THEN 'b:-2-0%'
                    WHEN morning_move < 0.02 THEN 'c:0-2%' WHEN morning_move < 0.05 THEN 'd:2-5%'
                    WHEN morning_move < 0.10 THEN 'e:5-10%' ELSE 'f:>=10%' END AS bucket,
               {OUTCOME}
        FROM sel WHERE r_surge <= 10 GROUP BY 1 ORDER BY 1
    """)

    # --- 市場状態別（選択後） ---
    out["regime_effect_top10"] = q(con, f"""
        SELECT regime_vol_base AS regime, {OUTCOME}
        FROM sel WHERE r_surge <= 10 GROUP BY 1 ORDER BY 1
    """)
    out["mkt_trend_effect_top10"] = q(con, f"""
        SELECT CASE WHEN mkt_sma25_slope5 > 0.005 THEN 'a:上昇' WHEN mkt_sma25_slope5 > -0.005 THEN 'b:横ばい' ELSE 'c:下落' END AS mkt,
               {OUTCOME}
        FROM sel WHERE r_surge <= 10 GROUP BY 1 ORDER BY 1
    """)

    # --- 高値・安値の時刻分布（上位10） ---
    out["time_of_high_top10"] = q(con, """
        SELECT CASE WHEN time_of_high < TIME '10:00' THEN 'a:9:35-10:00未満'
                    WHEN time_of_high < TIME '11:00' THEN 'b:10-11時'
                    WHEN time_of_high < TIME '12:45' THEN 'c:11-12:45'
                    WHEN time_of_high < TIME '14:00' THEN 'd:12:45-14時'
                    ELSE 'e:14時以降' END AS bucket,
               COUNT(*) AS n,
               ROUND(AVG(ret_0935_1525), 5) AS ret_mean,
               ROUND(AVG((ret_0935_1525 > 0)::INT), 4) AS win
        FROM sel WHERE r_surge <= 10 GROUP BY 1 ORDER BY 1
    """)

    # --- エントリー時刻の比較（9:00寄付き vs 9:35） ---
    out["entry_timing_top10"] = q(con, """
        SELECT mperiod,
               ROUND(AVG(ret_0900_1525), 5) AS ret_0900_mean,
               ROUND(AVG(ret_0935_1525), 5) AS ret_0935_mean,
               ROUND(quantile_cont(ret_0900_1525, 0.5), 5) AS ret_0900_med,
               ROUND(quantile_cont(ret_0935_1525, 0.5), 5) AS ret_0935_med,
               ROUND(AVG(entry_0935_close / open_day - 1), 5) AS drift_0900_0935
        FROM sel WHERE r_surge <= 10 GROUP BY 1 ORDER BY 1
    """)

    # --- 翌日以降への持ち越し効果（現行はEOD決済）: 上位10のt+1以降 ---
    out["swing_vs_daytrade_top10"] = q(con, """
        SELECT s.mperiod,
               COUNT(*) AS n,
               ROUND(AVG(s.ret_0935_1525), 5) AS daytrade_mean,
               ROUND(AVG(p.fwd_ret5), 5) AS hold5_mean,
               ROUND(quantile_cont(p.fwd_ret5, 0.5), 5) AS hold5_med,
               ROUND(AVG(p.fwd_ret20), 5) AS hold20_mean,
               ROUND(quantile_cont(p.fwd_ret20, 0.5), 5) AS hold20_med
        FROM sel s
        JOIN panel p ON p.code = s.code AND p.date = s.prev_date
        WHERE s.r_surge <= 10
        GROUP BY 1 ORDER BY 1
    """)

    write_json("04_intraday_analysis.json", out)
    con.close()


if __name__ == "__main__":
    main()
