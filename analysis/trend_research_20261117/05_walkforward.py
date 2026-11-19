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
  - 注意: 本スクリプトの「四半期別安定性」は学習/検証の分割ではなく、全期間で設計した条件の
    期間別一貫性の確認である（インサンプル）。採用判断には未使用期間での再確認が必要。
    主要バリアントには日単位クラスタのブートストラップ95%CIを併記する。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from common import (
    ANALYSIS_DATA_CUTOFF,
    connect,
    current_parquet_paths,
    rows_to_dicts,
    write_json,
)

ROUND_TRIP_COST = 0.0021  # 0.055%*2 + 5bps*2


def q(con, sql: str) -> list[dict]:
    return rows_to_dicts(con.sql(sql))


def main() -> None:
    con = connect()
    con.execute("SET TimeZone='Asia/Tokyo'")
    minute_paths = [
        p for _, p in current_parquet_paths("minute", ANALYSIS_DATA_CUTOFF)
    ]
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
            AND substr(p.code, 5, 1) = '0'  -- 普通株のみ
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
          -- SMA5は調整後終値で計算し、t-1時点の生値スケールへ換算する（窓内に分割があっても破綻しない）
          SELECT code, date AS prev_date,
                 AVG(adj_c) OVER (PARTITION BY code ORDER BY date ROWS BETWEEN 4 PRECEDING AND CURRENT ROW)
                   * (c / adj_c) AS sma5_scaled,
                 COUNT(c) OVER (PARTITION BY code ORDER BY date ROWS BETWEEN 4 PRECEDING AND CURRENT ROW) AS n5
          FROM bars_daily WHERE c IS NOT NULL AND adj_c IS NOT NULL AND adj_c > 0
        )
        SELECT s.*, m.va_morning / s.va_avg20 AS morning_surge,
               m.close_pre0930, m.close_pre0930_time,
               m.entry_0935_close, m.close_1525, m.open_day, m.entry_time, m.last_time,
               m.entry_0935_close / s.prev_close - 1 AS morning_move,
               m.close_pre0930 / s.prev_close - 1 AS move_at_0930,
               ROUND(sma.sma5_scaled * 1.01, 1) AS upper_band,
               sma.n5,
               CASE WHEN m.entry_0935_close IS NOT NULL AND m.close_1525 IS NOT NULL
                         AND m.entry_time <= TIME '09:45' AND m.last_time::TIME >= TIME '15:00'
                    THEN 1 ELSE 0 END AS filled
        FROM screened s
        JOIN minute_day_summary m ON m.code = s.code AND m.date = s.date
        JOIN sma ON sma.code = s.code AND sma.prev_date = s.prev_date
        WHERE s.r_merge <= 50 AND m.va_morning IS NOT NULL AND sma.n5 = 5
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
        SELECT t.*, br.cross_time,
               -- 約定価格はバンド価格と到達バー終値の高い方（高値だけ触れて下げたバーでの有利約定を防ぐ保守化）
               GREATEST(t.upper_band, br.cross_close) AS entry_breakout,
               CASE WHEN t.rsi14_sma >= 55 THEN 1 ELSE 0 END AS rsi_ok,
               CASE WHEN br.cross_time IS NOT NULL AND t.filled = 1
                    THEN t.close_1525 / GREATEST(t.upper_band, br.cross_close) - 1 END AS ret_breakout,
               CASE WHEN br.cross_time IS NOT NULL AND t.filled = 1
                    THEN t.close_1525 / GREATEST(t.upper_band, br.cross_close) - 1 - {ROUND_TRIP_COST} END AS ret_breakout_net,
               CASE WHEN t.filled = 1 THEN t.close_1525 / t.entry_0935_close - 1 END AS ret_0935,
               CASE WHEN t.filled = 1 THEN t.close_1525 / t.entry_0935_close - 1 - {ROUND_TRIP_COST} END AS ret_0935_net,
               CASE WHEN t.date < DATE '2025-11-16' THEN 'M1' ELSE 'M2' END AS mperiod,
               year(t.date)::VARCHAR || 'Q' || quarter(t.date)::VARCHAR AS yq
        FROM top10 t
        LEFT JOIN breakout br ON br.code = t.code AND br.date = t.date
    """)

    out["fill_rates"] = q(con, """
        SELECT mperiod, COUNT(*) AS top10_rows, ROUND(AVG(filled), 4) AS fill_rate
        FROM sim GROUP BY 1 ORDER BY 1
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
        ("F_ブレイクアウト+朝2%以下", "cross_time IS NOT NULL AND move_at_0930 <= 0.02", "ret_breakout_net"),
        ("G_ブレイクアウト+RSI55+朝2%以下+UL/mom除外",
         "cross_time IS NOT NULL AND rsi_ok=1 AND move_at_0930 <= 0.02 AND prev_ul = 0 AND (ret20 IS NULL OR ret20 < 0.25)",
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
        ("G_除外付きブレイクアウト", "cross_time IS NOT NULL AND rsi_ok=1 AND move_at_0930 <= 0.02 AND prev_ul = 0 AND (ret20 IS NULL OR ret20 < 0.25)", "ret_breakout_net"),
    ]:
        rows = q(con, f"SELECT yq, {outcome(col)} FROM sim WHERE {cond} GROUP BY 1 ORDER BY 1")
        out["quarterly_stability"].append({"variant": label, "rows": rows})

    # --- 日足5年プロキシ: スクリーニング段階の選択ルール比較（P1..P4） ---
    # 順位付けは前日時点情報のみで行い、将来情報（fwd_intraday1 の有無）は集計時に扱う
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
            AND substr(p.code, 5, 1) = '0'
            AND p.va_avg20 > 0
        ), ranked AS (
          SELECT *,
                 rank() OVER (PARTITION BY date ORDER BY va DESC) AS r_va,
                 rank() OVER (PARTITION BY date ORDER BY ret1 DESC NULLS LAST) AS r_up,
                 rank() OVER (PARTITION BY date, (price_close > 450) ORDER BY va DESC) AS r_va_band,
                 rank() OVER (PARTITION BY date, (price_close > 450) ORDER BY ret1 DESC NULLS LAST) AS r_up_band
          FROM prev WHERE price_close <= 900
        )
        SELECT *,
               rank() OVER (PARTITION BY date ORDER BY (r_va + r_up)) AS r_merge,
               rank() OVER (PARTITION BY date, (price_close > 450) ORDER BY (r_va_band + r_up_band)) AS r_merge_band
        FROM ranked
    """)
    out["daily_proxy_unfilled"] = q(con, """
        SELECT period, COUNT(*) FILTER (r_merge <= 50) AS top50_rows,
               ROUND(AVG((fwd_intraday1 IS NULL)::INT) FILTER (r_merge <= 50), 4) AS top50_no_fwd_rate
        FROM daily_proxy GROUP BY 1 ORDER BY 1
    """)
    out["daily_proxy_screening"] = []
    for label, cond in [
        ("S0_上位50全体", "r_merge <= 50"),
        ("S1_上位50+前日サージ>=4除外", "r_merge <= 50 AND (va_surge IS NULL OR va_surge < 4)"),
        ("S2_上位50+UL/mom25除外", "r_merge <= 50 AND ul = 0 AND (ret20 IS NULL OR ret20 < 0.25)"),
        ("S3_上位50+ギャップ2%超除外", "r_merge <= 50 AND gap_t1 <= 0.02"),
        ("S4_S2+S3複合", "r_merge <= 50 AND ul = 0 AND (ret20 IS NULL OR ret20 < 0.25) AND gap_t1 <= 0.02"),
        ("S5_価格帯450-900のみ上位50(帯内で再ランキング)", "price_close > 450 AND r_merge_band <= 50"),
        ("S6_価格帯<=270のみ上位50(帯内で再ランキング)", "price_close <= 270 AND r_merge_band <= 50"),
    ]:
        rows = q(con, f"""
            SELECT period, COUNT(fwd_intraday1) AS n, COUNT(DISTINCT date) AS days,
                   ROUND(AVG(fwd_intraday1 - 0.0021), 5) AS net_mean,
                   ROUND(quantile_cont(fwd_intraday1 - 0.0021, 0.5), 5) AS net_med,
                   ROUND(AVG((fwd_intraday1 > 0.0021)::INT), 4) AS net_win,
                   ROUND(AVG(fwd_intraday1 - idx_fwd_intraday1), 5) AS ex1_mean
            FROM daily_proxy WHERE ({cond}) AND fwd_intraday1 IS NOT NULL GROUP BY 1 ORDER BY 1
        """)
        out["daily_proxy_screening"].append({"variant": label, "rows": rows})

    # レジーム別（日足プロキシ・上位50）
    out["daily_proxy_by_regime"] = q(con, """
        SELECT period, regime_vol_base AS regime, COUNT(fwd_intraday1) AS n, COUNT(DISTINCT date) AS days,
               ROUND(AVG(fwd_intraday1 - 0.0021), 5) AS net_mean,
               ROUND(AVG((fwd_intraday1 > 0.0021)::INT), 4) AS net_win
        FROM daily_proxy WHERE r_merge <= 50 AND fwd_intraday1 IS NOT NULL GROUP BY 1, 2 ORDER BY 1, 2
    """)

    # --- 日単位クラスタのブートストラップCI（同日の銘柄間相関を考慮） ---
    # 1トレード平均（コスト後）の日次平均を単位に、日を復元抽出して平均の95%CIを推定
    import random

    random.seed(20261118)

    def bootstrap_ci(day_means: list[float], n_boot: int = 2000) -> dict:
        if not day_means:
            return {"days": 0}
        n = len(day_means)
        stats = []
        for _ in range(n_boot):
            s = [day_means[random.randrange(n)] for _ in range(n)]
            stats.append(sum(s) / n)
        stats.sort()
        return {
            "days": n,
            "mean": round(sum(day_means) / n, 5),
            "ci_lo": round(stats[int(0.025 * n_boot)], 5),
            "ci_hi": round(stats[int(0.975 * n_boot)], 5),
        }

    out["bootstrap_ci_daily_mean"] = []
    for label, cond, col in [
        ("A_全候補_0935", "ret_0935_net IS NOT NULL", "ret_0935_net"),
        ("C_ブレイクアウト+RSI55", "cross_time IS NOT NULL AND rsi_ok=1 AND ret_breakout_net IS NOT NULL", "ret_breakout_net"),
        ("G_除外付きブレイクアウト",
         "cross_time IS NOT NULL AND rsi_ok=1 AND move_at_0930 <= 0.02 AND prev_ul = 0 AND (ret20 IS NULL OR ret20 < 0.25) AND ret_breakout_net IS NOT NULL",
         "ret_breakout_net"),
    ]:
        day_means = [r[0] for r in con.sql(
            f"SELECT AVG({col}) FROM sim WHERE {cond} GROUP BY date"
        ).fetchall() if r[0] is not None]
        out["bootstrap_ci_daily_mean"].append({"variant": label, **bootstrap_ci(day_means)})

    # 未約定行を評価対象外とする既定集計に加え、全額損失(-100%)と仮定した極端な下方ストレスを示す。
    out["fill_failure_stress"] = q(con, """
        WITH selected AS (
          SELECT 'A_全候補_0935' AS variant, mperiod, filled, ret_0935_net AS ret
          FROM sim
          UNION ALL
          SELECT 'C_ブレイクアウト+RSI55', mperiod, filled, ret_breakout_net
          FROM sim WHERE cross_time IS NOT NULL AND rsi_ok = 1
          UNION ALL
          SELECT 'G_除外付きブレイクアウト', mperiod, filled, ret_breakout_net
          FROM sim
          WHERE cross_time IS NOT NULL AND rsi_ok = 1 AND move_at_0930 <= 0.02
            AND prev_ul = 0 AND (ret20 IS NULL OR ret20 < 0.25)
        )
        SELECT variant, mperiod, COUNT(*) AS selected_n,
               COUNT(*) FILTER (filled = 1) AS filled_n,
               COUNT(*) FILTER (filled = 0) AS unfilled_n,
               ROUND(AVG(ret) FILTER (filled = 1), 5) AS filled_only_net_mean,
               ROUND(
                 (COALESCE(SUM(ret) FILTER (filled = 1), 0)
                   - COUNT(*) FILTER (filled = 0)) / COUNT(*),
                 5
               ) AS net_mean_if_unfilled_loses_100pct
        FROM selected GROUP BY 1, 2 ORDER BY 1, 2
    """)

    write_json("05_walkforward.json", out)
    con.close()


if __name__ == "__main__":
    main()
