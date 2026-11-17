"""05: エントリーゲート（SMA5+1%ブレイクアウト・RSI）の分足検証と、コスト込みウォークフォワード。

入力: trend_research.duckdb、J-Quants canonical 分足 parquet
出力: data/analysis/trend_research_20261117/05_walkforward.json

前提:
  - 対象候補は 04 と同じ近似パイプライン（前日スクリーニング上位50 → 朝サージ上位10）
  - ブレイクアウト: 前日までの直近5本の生終値SMA×1.01 を0.1円丸めした上限帯に、
    09:35以降の分足高値が最初に到達したバーの終値でエントリー（本番は板価格・成行のため近似）
  - RSIゲートは Cutler 近似 RSI(t-1) >= 55 を使用（本番は Wilder。値がずれるため傾向比較用）
  - イグジット: 15:25 までの最後の分足終値（EOD決済近似）。ATRトレーリングは含まない（限界として報告）
  - コスト: 往復 手数料0.055%×2 + スリッページ5bps×2 = 約0.21%（現行バックテスト既定と同一）
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from common import connect, current_parquet_paths, rows_to_dicts, write_json

ROUND_TRIP_COST = 0.0021  # 0.055%*2 + 5bps*2


def q(con, sql: str) -> list[dict]:
    return rows_to_dicts(con.sql(sql))


def main() -> None:
    con = connect()
    con.execute("SET TimeZone='Asia/Tokyo'")
    minute_paths = [p for _, p in current_parquet_paths("minute")]
    out: dict = {}

    # --- 候補テーブル（04と同一ロジック）を一時テーブル化 ---
    con.execute("""
        CREATE OR REPLACE TEMP TABLE cand AS
        WITH prev AS (
          SELECT p.code, p.date AS prev_date, p.date_t1 AS date,
                 p.price_close AS prev_close, p.va AS prev_va, p.ret1 AS prev_ret1,
                 p.ret20, p.rsi14_sma, p.va_avg20, p.ul AS prev_ul
          FROM panel p
          JOIN bars_daily b1 ON b1.code = p.code AND b1.date = p.date_t1
          WHERE substr(p.code, 1, 4) IN (SELECT symbol FROM universe_master)
            AND p.price_close <= 900 AND p.va_avg20 > 0 AND p.date_t1 IS NOT NULL
            AND COALESCE(b1.adj_factor, 1) = 1  -- 分割・併合当日は生値のバンド比較が破綻するため除外
        ), ranked AS (
          SELECT *,
                 rank() OVER (PARTITION BY prev_date ORDER BY prev_va DESC) AS r_va,
                 rank() OVER (PARTITION BY prev_date ORDER BY prev_ret1 DESC NULLS LAST) AS r_up
          FROM prev
        ), screened AS (
          SELECT *, rank() OVER (PARTITION BY prev_date ORDER BY (r_va + r_up)) AS r_merge
          FROM ranked
        ), sma AS (
          SELECT code, date AS prev_date,
                 AVG(c) OVER (PARTITION BY code ORDER BY date ROWS BETWEEN 4 PRECEDING AND CURRENT ROW) AS sma5_raw,
                 COUNT(c) OVER (PARTITION BY code ORDER BY date ROWS BETWEEN 4 PRECEDING AND CURRENT ROW) AS n5
          FROM bars_daily WHERE c IS NOT NULL
        )
        SELECT s.*, m.va_morning / s.va_avg20 AS morning_surge,
               m.entry_0935_close, m.close_1525, m.open_day,
               m.entry_0935_close / s.prev_close - 1 AS morning_move,
               ROUND(sma.sma5_raw * 1.01, 1) AS upper_band,
               sma.n5
        FROM screened s
        JOIN minute_day_summary m ON m.code = s.code AND m.date = s.date
        JOIN sma ON sma.code = s.code AND sma.prev_date = s.prev_date
        WHERE s.r_merge <= 50 AND m.va_morning IS NOT NULL
          AND m.entry_0935_close IS NOT NULL AND m.close_1525 IS NOT NULL AND sma.n5 = 5
    """)
    con.execute("""
        CREATE OR REPLACE TEMP TABLE top10 AS
        SELECT * FROM (
          SELECT *, rank() OVER (PARTITION BY date ORDER BY morning_surge DESC NULLS LAST) AS r_surge
          FROM cand
        ) WHERE r_surge <= 10
    """)

    # --- 分足からブレイクアウト到達バーを抽出（候補のみ・1スキャン） ---
    con.execute(
        """
        CREATE OR REPLACE TEMP TABLE breakout AS
        SELECT b.Code AS code, b.Date AS date,
               min((b.Time AT TIME ZONE 'Asia/Tokyo')::TIME) AS cross_time,
               arg_min(b.C, b.Time) AS cross_close
        FROM read_parquet(?) b
        JOIN top10 t ON t.code = b.Code AND t.date = b.Date
        WHERE (b.Time AT TIME ZONE 'Asia/Tokyo')::TIME >= TIME '09:35'
          AND (b.Time AT TIME ZONE 'Asia/Tokyo')::TIME <= TIME '15:19'
          AND b.H >= t.upper_band
        GROUP BY 1, 2
        """,
        [minute_paths],
    )

    con.execute(f"""
        CREATE OR REPLACE TEMP VIEW sim AS
        SELECT t.*, br.cross_time, br.cross_close,
               CASE WHEN t.rsi14_sma >= 55 THEN 1 ELSE 0 END AS rsi_ok,
               CASE WHEN br.cross_time IS NOT NULL THEN t.close_1525 / br.cross_close - 1 END AS ret_breakout,
               CASE WHEN br.cross_time IS NOT NULL THEN t.close_1525 / br.cross_close - 1 - {ROUND_TRIP_COST} END AS ret_breakout_net,
               t.close_1525 / t.entry_0935_close - 1 AS ret_0935,
               t.close_1525 / t.entry_0935_close - 1 - {ROUND_TRIP_COST} AS ret_0935_net,
               CASE WHEN t.date < DATE '2025-11-16' THEN 'M1' ELSE 'M2' END AS mperiod,
               year(t.date)::VARCHAR || 'Q' || quarter(t.date)::VARCHAR AS yq
        FROM top10 t
        LEFT JOIN breakout br ON br.code = t.code AND br.date = t.date
    """)

    out["gate_trigger_rates"] = q(con, """
        SELECT mperiod, COUNT(*) AS cand,
               ROUND(AVG((cross_time IS NOT NULL)::INT), 4) AS breakout_rate,
               ROUND(AVG(rsi_ok), 4) AS rsi55_rate,
               ROUND(AVG((cross_time IS NOT NULL AND rsi_ok=1)::INT), 4) AS both_rate
        FROM sim GROUP BY 1 ORDER BY 1
    """)

    def outcome(col: str) -> str:
        return f"""
          COUNT({col}) AS n,
          ROUND(AVG({col}), 5) AS mean,
          ROUND(quantile_cont({col}, 0.5), 5) AS med,
          ROUND(quantile_cont({col}, 0.1), 5) AS p10,
          ROUND(quantile_cont({col}, 0.9), 5) AS p90,
          ROUND(AVG(({col} > 0)::INT), 4) AS win
        """

    # ゲート別比較（コスト後）
    out["gate_comparison_net"] = []
    for label, cond, col in [
        ("A_全候補_0935エントリー", "TRUE", "ret_0935_net"),
        ("B_ブレイクアウトのみ", "cross_time IS NOT NULL", "ret_breakout_net"),
        ("C_ブレイクアウト+RSI55", "cross_time IS NOT NULL AND rsi_ok=1", "ret_breakout_net"),
        ("D_ブレイクアウト+RSI55未満", "cross_time IS NOT NULL AND rsi_ok=0", "ret_breakout_net"),
        ("E_非ブレイクアウト(0935)", "cross_time IS NULL", "ret_0935_net"),
        ("F_ブレイクアウト+朝2%以下", "cross_time IS NOT NULL AND morning_move <= 0.02", "ret_breakout_net"),
        ("G_ブレイクアウト+RSI55+朝2%以下+UL/mom除外",
         "cross_time IS NOT NULL AND rsi_ok=1 AND morning_move <= 0.02 AND prev_ul = 0 AND (ret20 IS NULL OR ret20 < 0.25)",
         "ret_breakout_net"),
    ]:
        rows = q(con, f"SELECT mperiod, {outcome(col)} FROM sim WHERE {cond} GROUP BY 1 ORDER BY 1")
        out["gate_comparison_net"].append({"variant": label, "rows": rows})

    # ブレイクアウト到達時刻別
    out["breakout_time_buckets"] = q(con, f"""
        SELECT CASE WHEN cross_time < TIME '10:00' THEN 'a:9:35-10時'
                    WHEN cross_time < TIME '11:00' THEN 'b:10-11時'
                    WHEN cross_time < TIME '13:00' THEN 'c:11-13時'
                    ELSE 'd:13時以降' END AS bucket,
               {outcome('ret_breakout_net')}
        FROM sim WHERE cross_time IS NOT NULL GROUP BY 1 ORDER BY 1
    """)

    # 四半期別の安定性（主要バリアント）
    out["quarterly_stability"] = []
    for label, cond, col in [
        ("A_全候補_0935", "TRUE", "ret_0935_net"),
        ("C_ブレイクアウト+RSI55", "cross_time IS NOT NULL AND rsi_ok=1", "ret_breakout_net"),
        ("G_除外付きブレイクアウト", "cross_time IS NOT NULL AND rsi_ok=1 AND morning_move <= 0.02 AND prev_ul = 0 AND (ret20 IS NULL OR ret20 < 0.25)", "ret_breakout_net"),
    ]:
        rows = q(con, f"SELECT yq, {outcome(col)} FROM sim WHERE {cond} GROUP BY 1 ORDER BY 1")
        out["quarterly_stability"].append({"variant": label, "rows": rows})

    # --- 日足5年プロキシ: スクリーニング段階の選択ルール比較（P1..P4） ---
    con.execute("""
        CREATE OR REPLACE TEMP VIEW daily_proxy AS
        WITH prev AS (
          SELECT p.*, ms.regime_vol_base,
                 mf.idx_fwd_intraday1,
                 CASE WHEN p.date < DATE '2023-11-01' THEN 'P1'
                      WHEN p.date < DATE '2024-11-01' THEN 'P2'
                      WHEN p.date < DATE '2025-11-01' THEN 'P3'
                      ELSE 'P4' END AS period
          FROM panel p
          JOIN market_state ms USING (date)
          JOIN market_fwd mf USING (date)
          WHERE substr(p.code, 1, 4) IN (SELECT symbol FROM universe_master)
            AND p.va_avg20 > 0 AND p.fwd_intraday1 IS NOT NULL
        ), ranked AS (
          SELECT *,
                 rank() OVER (PARTITION BY date ORDER BY va DESC) AS r_va,
                 rank() OVER (PARTITION BY date ORDER BY ret1 DESC NULLS LAST) AS r_up
          FROM prev WHERE price_close <= 900
        )
        SELECT *, rank() OVER (PARTITION BY date ORDER BY (r_va + r_up)) AS r_merge
        FROM ranked
    """)
    out["daily_proxy_screening"] = []
    for label, cond in [
        ("S0_上位50全体", "r_merge <= 50"),
        ("S1_上位50+前日サージ>=4除外", "r_merge <= 50 AND (va_surge IS NULL OR va_surge < 4)"),
        ("S2_上位50+UL/mom25除外", "r_merge <= 50 AND ul = 0 AND (ret20 IS NULL OR ret20 < 0.25)"),
        ("S3_上位50+ギャップ2%超除外", "r_merge <= 50 AND gap_t1 <= 0.02"),
        ("S4_S2+S3複合", "r_merge <= 50 AND ul = 0 AND (ret20 IS NULL OR ret20 < 0.25) AND gap_t1 <= 0.02"),
        ("S5_価格帯450-900のみ上位50", "r_merge <= 50 AND price_close > 450"),
        ("S6_価格帯<=270のみ上位50", "r_merge <= 50 AND price_close <= 270"),
    ]:
        rows = q(con, f"""
            SELECT period, COUNT(*) AS n, COUNT(DISTINCT date) AS days,
                   ROUND(AVG(fwd_intraday1 - 0.0021), 5) AS net_mean,
                   ROUND(quantile_cont(fwd_intraday1 - 0.0021, 0.5), 5) AS net_med,
                   ROUND(AVG((fwd_intraday1 > 0.0021)::INT), 4) AS net_win,
                   ROUND(AVG(fwd_intraday1 - idx_fwd_intraday1), 5) AS ex1_mean
            FROM daily_proxy WHERE {cond} GROUP BY 1 ORDER BY 1
        """)
        out["daily_proxy_screening"].append({"variant": label, "rows": rows})

    # レジーム別（日足プロキシ・上位50）
    out["daily_proxy_by_regime"] = q(con, """
        SELECT period, regime_vol_base AS regime, COUNT(*) AS n, COUNT(DISTINCT date) AS days,
               ROUND(AVG(fwd_intraday1 - 0.0021), 5) AS net_mean,
               ROUND(AVG((fwd_intraday1 > 0.0021)::INT), 4) AS net_win
        FROM daily_proxy WHERE r_merge <= 50 GROUP BY 1, 2 ORDER BY 1, 2
    """)

    write_json("05_walkforward.json", out)
    con.close()


if __name__ == "__main__":
    main()
