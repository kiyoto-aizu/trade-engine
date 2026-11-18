"""03: トレンド事前定義の検証と特徴量・市場状態別の集計。

入力: trend_research.duckdb（panel, market_state, market_fwd, universe_master）
出力: data/analysis/trend_research_20261117/03_trend_analysis.json

定義（計算前に固定）:
  - 対象: universe_master に存在する銘柄（東証P/S/G の普通株）かつ o_t1 が存在する行
  - 期間分割: P1=2021-11-04..2023-10-31, P2=..2024-10-31, P3=..2025-10-31, P4=..2026-11-16
  - 将来リターン: fwd_ret{5,10,20} = adj_c(t+h)/adj_o(t+1)-1、超過 = 同一窓の TOPIX 連動ETF比
  - トレンド率: 超過リターンが閾値以上の行の割合（閾値感度 3/5/8/10/15%）
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from common import connect, rows_to_dicts, write_json

PERIOD_SQL = """
CASE WHEN date < DATE '2023-11-01' THEN 'P1'
     WHEN date < DATE '2024-11-01' THEN 'P2'
     WHEN date < DATE '2025-11-01' THEN 'P3'
     ELSE 'P4' END
"""

BASE_VIEW = f"""
CREATE OR REPLACE TEMP VIEW v AS
SELECT p.*,
       {PERIOD_SQL} AS period,
       -- 超過リターンは銘柄側と指数側の窓終端日付が一致する行のみ（出来値なし日による窓ずれを排除）
       CASE WHEN p.date_t5 = mf.idx_date_t5 THEN p.fwd_ret5 - mf.idx_fwd_ret5 END AS ex5,
       CASE WHEN p.date_t10 = mf.idx_date_t10 THEN p.fwd_ret10 - mf.idx_fwd_ret10 END AS ex10,
       CASE WHEN p.date_t20 = mf.idx_date_t20 THEN p.fwd_ret20 - mf.idx_fwd_ret20 END AS ex20,
       CASE WHEN p.date_t1 = mf.idx_date_t1 THEN p.fwd_intraday1 - mf.idx_fwd_intraday1 END AS ex1,
       ms.regime_vol_base, ms.mkt_vol20, ms.mkt_sma25_slope5, ms.mkt_sma25_gap,
       ms.mkt_dd252, ms.mkt_adx14_sma, ms.mkt_ret1, ms.mkt_bigmove_prev,
       CASE WHEN p.price_close <= 270 THEN 'a:<=270'
            WHEN p.price_close <= 450 THEN 'b:270-450'
            WHEN p.price_close <= 900 THEN 'c:450-900'
            WHEN p.price_close <= 3000 THEN 'd:900-3000'
            ELSE 'e:>3000' END AS price_band,
       CASE WHEN p.va_avg20_prev < 1e7 THEN 'a:<10M'
            WHEN p.va_avg20_prev < 5e7 THEN 'b:10-50M'
            WHEN p.va_avg20_prev < 1e8 THEN 'c:50-100M'
            WHEN p.va_avg20_prev < 1e9 THEN 'd:100M-1B'
            ELSE 'e:>1B' END AS liq_band
FROM panel p
JOIN market_fwd mf USING (date)
JOIN market_state ms USING (date)
WHERE substr(p.code, 1, 4) IN (SELECT symbol FROM universe_master)
  AND substr(p.code, 5, 1) = '0'  -- 普通株のみ（優先株等の末尾非0を除外）
  AND p.o_t1 IS NOT NULL AND p.rn > 25 AND p.va_avg20_prev > 0
"""

OUTCOME_COLS = """
       ROUND(quantile_cont(ex20, 0.5), 4) AS ex20_med,
       ROUND(AVG(ex20), 4) AS ex20_mean,
       ROUND(quantile_cont(ex20, 0.25), 4) AS ex20_p25,
       ROUND(quantile_cont(ex20, 0.75), 4) AS ex20_p75,
       ROUND(AVG((ex20 >= 0.10)::INT), 4) AS trend10_rate,
       ROUND(AVG((ex20 <= -0.10)::INT), 4) AS crash10_rate,
       ROUND(quantile_cont(fwd_mfe20, 0.5), 4) AS mfe20_med,
       ROUND(quantile_cont(fwd_mae20, 0.5), 4) AS mae20_med,
       ROUND(quantile_cont(ex1, 0.5), 5) AS ex1_med,
       ROUND(AVG(ex1), 5) AS ex1_mean,
       ROUND(AVG((fwd_intraday1 > 0)::INT), 4) AS intraday_win
"""


def q(con, sql: str) -> list[dict]:
    return rows_to_dicts(con.sql(sql))


def main() -> None:
    con = connect()
    con.execute(BASE_VIEW)
    out: dict = {}

    out["universe"] = q(con, "SELECT period, COUNT(*) AS rows, COUNT(DISTINCT code) AS codes, COUNT(DISTINCT date) AS days FROM v GROUP BY 1 ORDER BY 1")

    # --- オーバーナイト/日中ドリフトの分解（提案の根拠。分布・損失側も併記） ---
    out["overnight_vs_intraday"] = q(con, """
        SELECT period, COUNT(*) AS n,
               ROUND(AVG(gap_t1), 5) AS overnight_mean,
               ROUND(quantile_cont(gap_t1, 0.5), 5) AS overnight_med,
               ROUND(quantile_cont(gap_t1, 0.01), 5) AS overnight_p1,
               ROUND(quantile_cont(gap_t1, 0.05), 5) AS overnight_p5,
               ROUND(AVG((gap_t1 <= -0.10)::INT), 5) AS overnight_le_m10pct,
               ROUND(AVG(fwd_intraday1), 5) AS intraday_mean,
               ROUND(quantile_cont(fwd_intraday1, 0.5), 5) AS intraday_med,
               ROUND(quantile_cont(fwd_intraday1, 0.05), 5) AS intraday_p5
        FROM v WHERE gap_t1 IS NOT NULL AND fwd_intraday1 IS NOT NULL
        GROUP BY 1 ORDER BY 1
    """)

    # --- 生存者バイアスの定量化（マスタ絞りの影響） ---
    # マスタ外コードのうち、データ末尾より10営業日以上前に消滅したコード（上場廃止の近似）
    out["survivorship"] = {
        "daily_codes_not_in_master": con.sql(
            "SELECT COUNT(DISTINCT code) FROM bars_daily WHERE substr(code,1,4) NOT IN (SELECT symbol FROM universe_master)"
        ).fetchone()[0],
        "master_not_in_daily": con.sql(
            "SELECT COUNT(DISTINCT symbol) FROM universe_master WHERE symbol NOT IN (SELECT substr(code,1,4) FROM bars_daily)"
        ).fetchone()[0],
        "disappeared_codes_total": con.sql(
            """
            SELECT COUNT(*) FROM (
              SELECT code, MAX(date) AS last_d FROM bars_daily WHERE substr(code,5,1)='0' GROUP BY 1
            ) WHERE last_d < DATE '2026-10-31'
            """
        ).fetchone()[0],
        "disappeared_codes_excluded_by_master": con.sql(
            """
            SELECT COUNT(*) FROM (
              SELECT code, MAX(date) AS last_d FROM bars_daily WHERE substr(code,5,1)='0' GROUP BY 1
            ) WHERE last_d < DATE '2026-10-31'
              AND substr(code,1,4) NOT IN (SELECT symbol FROM universe_master)
            """
        ).fetchone()[0],
    }
    # マスタ絞りなし（末尾0の全コード。ETF・REIT等を含む点に注意）での主要集計の再現
    con.execute(BASE_VIEW.replace(
        "substr(p.code, 1, 4) IN (SELECT symbol FROM universe_master)\n  AND substr(p.code, 5, 1) = '0'",
        "substr(p.code, 5, 1) = '0'",
    ).replace("TEMP VIEW v AS", "TEMP VIEW v_all AS"))
    out["survivorship::fwd_distribution_all_codes"] = q(con, """
        SELECT period, COUNT(*) AS n,
               ROUND(quantile_cont(ex20, 0.5), 4) AS ex20_med,
               ROUND(AVG((ex20 >= 0.10)::INT), 4) AS trend10_rate,
               ROUND(AVG((ex20 <= -0.10)::INT), 4) AS crash10_rate
        FROM v_all WHERE ex20 IS NOT NULL GROUP BY 1 ORDER BY 1
    """)
    out["survivorship::price_band_all_codes"] = q(con, """
        SELECT period,
               CASE WHEN price_close <= 270 THEN 'a:<=270' WHEN price_close <= 450 THEN 'b:270-450'
                    WHEN price_close <= 900 THEN 'c:450-900' WHEN price_close <= 3000 THEN 'd:900-3000'
                    ELSE 'e:>3000' END AS bin,
               COUNT(*) AS n,
               ROUND(quantile_cont(ex20, 0.5), 4) AS ex20_med,
               ROUND(AVG((ex20 >= 0.10)::INT), 4) AS trend10_rate,
               ROUND(AVG((ex20 <= -0.10)::INT), 4) AS crash10_rate,
               ROUND(AVG(fwd_intraday1), 5) AS intraday_mean
        FROM v_all WHERE ex20 IS NOT NULL GROUP BY 1, 2 ORDER BY 1, 2
    """)

    out["fwd_distribution"] = q(con, """
        SELECT period,
               COUNT(*) AS n,
               ROUND(quantile_cont(ex5, 0.5), 4) AS ex5_med,
               ROUND(quantile_cont(ex20, 0.25), 4) AS ex20_p25,
               ROUND(quantile_cont(ex20, 0.5), 4) AS ex20_med,
               ROUND(quantile_cont(ex20, 0.75), 4) AS ex20_p75,
               ROUND(quantile_cont(ex20, 0.9), 4) AS ex20_p90,
               ROUND(AVG(ex20), 4) AS ex20_mean,
               ROUND(quantile_cont(fwd_mfe20, 0.5), 4) AS mfe20_med,
               ROUND(quantile_cont(fwd_mae20, 0.5), 4) AS mae20_med
        FROM v WHERE ex20 IS NOT NULL GROUP BY 1 ORDER BY 1
    """)
    out["trend_rate_sensitivity"] = q(con, """
        SELECT period, h, threshold, n, ROUND(rate, 4) AS rate
        FROM (
          SELECT period, h.h AS h, t.threshold,
                 COUNT(*) AS n,
                 AVG(CASE h.h WHEN 5 THEN (ex5 >= t.threshold)::INT
                              WHEN 10 THEN (ex10 >= t.threshold)::INT
                              ELSE (ex20 >= t.threshold)::INT END) AS rate
          FROM v, (VALUES (5),(10),(20)) h(h), (VALUES (0.03),(0.05),(0.08),(0.10),(0.15)) t(threshold)
          WHERE ex20 IS NOT NULL AND ex10 IS NOT NULL AND ex5 IS NOT NULL
          GROUP BY 1, 2, 3
        ) ORDER BY period, h, threshold
    """)
    out["trend_rate_with_mae"] = q(con, """
        SELECT period, COUNT(*) AS n,
               ROUND(AVG((ex20 >= 0.10)::INT), 4) AS trend10,
               ROUND(AVG((ex20 >= 0.10 AND fwd_mae20 >= -0.05)::INT), 4) AS trend10_mae5,
               ROUND(AVG((ex20 >= 0.10 AND fwd_mae20 >= -0.10)::INT), 4) AS trend10_mae10,
               ROUND(AVG((ex20 <= -0.10)::INT), 4) AS crash10
        FROM v WHERE ex20 IS NOT NULL GROUP BY 1 ORDER BY 1
    """)

    feature_bins = {
        "va_surge": "CASE WHEN va_surge < 0.5 THEN 'a:<0.5' WHEN va_surge < 1 THEN 'b:0.5-1' WHEN va_surge < 2 THEN 'c:1-2' WHEN va_surge < 4 THEN 'd:2-4' WHEN va_surge < 8 THEN 'e:4-8' ELSE 'f:>=8' END",
        "rsi14_sma": "CASE WHEN rsi14_sma < 30 THEN 'a:<30' WHEN rsi14_sma < 45 THEN 'b:30-45' WHEN rsi14_sma < 55 THEN 'c:45-55' WHEN rsi14_sma < 60 THEN 'd:55-60' WHEN rsi14_sma < 70 THEN 'e:60-70' ELSE 'f:>=70' END",
        "ret5": "CASE WHEN ret5 < -0.05 THEN 'a:<-5%' WHEN ret5 < 0 THEN 'b:-5-0%' WHEN ret5 < 0.05 THEN 'c:0-5%' WHEN ret5 < 0.10 THEN 'd:5-10%' WHEN ret5 < 0.20 THEN 'e:10-20%' ELSE 'f:>=20%' END",
        "ret20": "CASE WHEN ret20 < -0.10 THEN 'a:<-10%' WHEN ret20 < 0 THEN 'b:-10-0%' WHEN ret20 < 0.10 THEN 'c:0-10%' WHEN ret20 < 0.25 THEN 'd:10-25%' WHEN ret20 < 0.5 THEN 'e:25-50%' ELSE 'f:>=50%' END",
        "atr14_pct": "CASE WHEN atr14_pct < 0.02 THEN 'a:<2%' WHEN atr14_pct < 0.03 THEN 'b:2-3%' WHEN atr14_pct < 0.05 THEN 'c:3-5%' WHEN atr14_pct < 0.08 THEN 'd:5-8%' ELSE 'e:>=8%' END",
        "sma5_gap": "CASE WHEN sma5_gap < -0.01 THEN 'a:<-1%' WHEN sma5_gap < 0.01 THEN 'b:-1-1%' WHEN sma5_gap < 0.03 THEN 'c:1-3%' WHEN sma5_gap < 0.07 THEN 'd:3-7%' ELSE 'e:>=7%' END",
        "off_high252": "CASE WHEN off_high252 >= -0.01 THEN 'a:新高値圏' WHEN off_high252 >= -0.05 THEN 'b:-5--1%' WHEN off_high252 >= -0.15 THEN 'c:-15--5%' WHEN off_high252 >= -0.3 THEN 'd:-30--15%' ELSE 'e:<-30%' END",
        "price_band": "price_band",
        "liq_band": "liq_band",
        "up_days_in_5": "'d' || up_days_in_5::VARCHAR",
        "gap_t1": "CASE WHEN gap_t1 < -0.02 THEN 'a:<-2%' WHEN gap_t1 < 0 THEN 'b:-2-0%' WHEN gap_t1 < 0.02 THEN 'c:0-2%' WHEN gap_t1 < 0.05 THEN 'd:2-5%' WHEN gap_t1 < 0.10 THEN 'e:5-10%' ELSE 'f:>=10%' END",
        "ul_prev": "CASE WHEN ul = 1 THEN 'ストップ高' ELSE '通常' END",
    }
    guard = {
        "va_surge": "va_surge IS NOT NULL",
        "rsi14_sma": "rsi14_sma IS NOT NULL",
        "ret5": "ret5 IS NOT NULL",
        "ret20": "ret20 IS NOT NULL",
        "atr14_pct": "atr14_pct IS NOT NULL",
        "sma5_gap": "sma5_gap IS NOT NULL",
        "off_high252": "off_high252 IS NOT NULL",
        "gap_t1": "gap_t1 IS NOT NULL",
    }
    for name, bin_sql in feature_bins.items():
        where = guard.get(name, "TRUE")
        out[f"feature::{name}"] = q(con, f"""
            SELECT period, {bin_sql} AS bin, COUNT(*) AS n,
                   {OUTCOME_COLS}
            FROM v WHERE ex20 IS NOT NULL AND {where}
            GROUP BY 1, 2 ORDER BY 1, 2
        """)

    for name, bin_sql in {
        "regime_vol_base": "regime_vol_base",
        "mkt_trend": "CASE WHEN mkt_sma25_slope5 > 0.005 THEN 'a:上昇' WHEN mkt_sma25_slope5 > -0.005 THEN 'b:横ばい' ELSE 'c:下落' END",
        "mkt_dd": "CASE WHEN mkt_dd252 >= -0.03 THEN 'a:高値圏' WHEN mkt_dd252 >= -0.10 THEN 'b:-3--10%' ELSE 'c:<-10%' END",
        "mkt_adx": "CASE WHEN mkt_adx14_sma >= 27 THEN 'a:ADX>=27' ELSE 'b:ADX<27' END",
        "mkt_bigmove_prev": "CASE WHEN mkt_bigmove_prev = 1 THEN '前日±2%以上' ELSE '通常' END",
    }.items():
        out[f"market::{name}"] = q(con, f"""
            SELECT period, {bin_sql} AS bin, COUNT(*) AS n, COUNT(DISTINCT date) AS days,
                   ROUND(quantile_cont(ex20, 0.5), 4) AS ex20_med,
                   ROUND(AVG((ex20 >= 0.10)::INT), 4) AS trend10_rate,
                   ROUND(AVG((ex20 <= -0.10)::INT), 4) AS crash10_rate,
                   ROUND(AVG(fwd_intraday1), 5) AS intraday_mean,
                   ROUND(quantile_cont(fwd_intraday1, 0.5), 5) AS intraday_med,
                   ROUND(AVG((fwd_intraday1 > 0)::INT), 4) AS intraday_win
            FROM v WHERE ex20 IS NOT NULL
            GROUP BY 1, 2 ORDER BY 1, 2
        """)

    out["interact::surge_x_regime"] = q(con, """
        SELECT regime_vol_base AS regime,
               CASE WHEN va_surge >= 4 THEN 'surge>=4' WHEN va_surge >= 2 THEN 'surge2-4' ELSE 'surge<2' END AS surge,
               COUNT(*) AS n,
               ROUND(quantile_cont(ex20, 0.5), 4) AS ex20_med,
               ROUND(AVG((ex20 >= 0.10)::INT), 4) AS trend10_rate,
               ROUND(AVG((ex20 <= -0.10)::INT), 4) AS crash10_rate,
               ROUND(AVG(fwd_intraday1), 5) AS intraday_mean,
               ROUND(AVG((fwd_intraday1 > 0)::INT), 4) AS intraday_win
        FROM v WHERE ex20 IS NOT NULL AND va_surge IS NOT NULL GROUP BY 1, 2 ORDER BY 1, 2
    """)
    out["interact::surge_x_price"] = q(con, """
        SELECT price_band,
               CASE WHEN va_surge >= 4 THEN 'surge>=4' WHEN va_surge >= 2 THEN 'surge2-4' ELSE 'surge<2' END AS surge,
               COUNT(*) AS n,
               ROUND(quantile_cont(ex20, 0.5), 4) AS ex20_med,
               ROUND(AVG((ex20 >= 0.10)::INT), 4) AS trend10_rate,
               ROUND(AVG((ex20 <= -0.10)::INT), 4) AS crash10_rate,
               ROUND(AVG(fwd_intraday1), 5) AS intraday_mean
        FROM v WHERE ex20 IS NOT NULL AND va_surge IS NOT NULL GROUP BY 1, 2 ORDER BY 1, 2
    """)
    out["interact::momentum_x_surge"] = q(con, """
        SELECT CASE WHEN ret20 >= 0.10 THEN 'mom20>=10%' WHEN ret20 >= 0 THEN 'mom20 0-10%' ELSE 'mom20<0' END AS momentum,
               CASE WHEN va_surge >= 4 THEN 'surge>=4' WHEN va_surge >= 2 THEN 'surge2-4' ELSE 'surge<2' END AS surge,
               COUNT(*) AS n,
               ROUND(quantile_cont(ex20, 0.5), 4) AS ex20_med,
               ROUND(AVG((ex20 >= 0.10)::INT), 4) AS trend10_rate,
               ROUND(AVG((ex20 <= -0.10)::INT), 4) AS crash10_rate,
               ROUND(AVG(fwd_intraday1), 5) AS intraday_mean,
               ROUND(quantile_cont(fwd_mae20, 0.5), 4) AS mae20_med
        FROM v WHERE ex20 IS NOT NULL AND ret20 IS NOT NULL AND va_surge IS NOT NULL GROUP BY 1, 2 ORDER BY 1, 2
    """)

    out["daily_breadth_by_market"] = q(con, """
        WITH breadth AS (
          SELECT date, regime_vol_base, mkt_sma25_slope5, COUNT(*) AS n,
                 AVG((ex20 >= 0.10)::INT) AS trend_share
          FROM v WHERE ex20 IS NOT NULL GROUP BY 1, 2, 3
        )
        SELECT regime_vol_base,
               CASE WHEN mkt_sma25_slope5 > 0.005 THEN 'a:上昇' WHEN mkt_sma25_slope5 > -0.005 THEN 'b:横ばい' ELSE 'c:下落' END AS mkt_trend,
               COUNT(*) AS days,
               ROUND(AVG(trend_share), 4) AS mean_share,
               ROUND(quantile_cont(trend_share, 0.25), 4) AS p25,
               ROUND(quantile_cont(trend_share, 0.5), 4) AS median,
               ROUND(quantile_cont(trend_share, 0.75), 4) AS p75
        FROM breadth GROUP BY 1, 2 ORDER BY 1, 2
    """)

    write_json("03_trend_analysis.json", out)
    con.close()


if __name__ == "__main__":
    main()
