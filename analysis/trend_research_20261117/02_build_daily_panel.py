"""02: 日足パネル構築（特徴量 + 将来リターン + 市場状態）。

入力: trend_research.duckdb の bars_daily（01で作成）
出力: 同 duckdb に以下テーブルを作成
  - market_state: 日付ごとの市場状態（TOPIX/N225 連動ETFを指数代理に使用）
  - panel: 銘柄×日付の特徴量（t時点まで）と将来リターン（t+1以降）

指標の注意:
  - リターン・テクニカルは調整後価格(adj_*)、価格帯・売買代金は生値を使用。
  - RSI/ADX は SMA 平滑の近似（Cutler 方式）。本番の Wilder 方式とは別物。
  - 出来値のない日（c IS NULL）は除外してから窓計算するため、停止期間をまたぐ窓は実日数より長い期間を参照する。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from common import connect

TOPIX_ETF = "13060"  # TOPIX連動型上場投資信託
N225_ETF = "13210"   # 日経225連動型上場投資信託


def main() -> None:
    con = connect()
    con.execute("SET TimeZone='Asia/Tokyo'")

    # --- 市場状態（指数代理: ETF日足） ---
    con.execute("DROP TABLE IF EXISTS market_state")
    con.execute(
        f"""
        CREATE TABLE market_state AS
        WITH n225 AS (
          SELECT date, adj_o AS o, adj_h AS h, adj_l AS l, adj_c AS c
          FROM bars_daily WHERE code = '{N225_ETF}' AND c IS NOT NULL
        ), topix AS (
          SELECT date, adj_o AS o, adj_c AS c FROM bars_daily WHERE code = '{TOPIX_ETF}' AND c IS NOT NULL
        ), n1 AS (
          SELECT date, o, h, l, c,
                 c / lag(c) OVER w - 1 AS ret1,
                 lag(c) OVER w AS c_prev,
                 GREATEST(h - l, abs(h - lag(c) OVER w), abs(l - lag(c) OVER w)) AS tr,
                 GREATEST(h - lag(h) OVER w, 0) AS up_move,
                 GREATEST(lag(l) OVER w - l, 0) AS down_move
          FROM n225 WINDOW w AS (ORDER BY date)
        ), n2 AS (
          SELECT date, o, h, l, c, ret1,
                 stddev_samp(ret1) OVER (ORDER BY date ROWS BETWEEN 19 PRECEDING AND CURRENT ROW)
                   * sqrt(252) * 100 AS realized_vol20,
                 AVG(c) OVER (ORDER BY date ROWS BETWEEN 24 PRECEDING AND CURRENT ROW) AS sma25,
                 AVG(tr) OVER (ORDER BY date ROWS BETWEEN 13 PRECEDING AND CURRENT ROW) AS atr14,
                 AVG(CASE WHEN up_move > down_move THEN up_move ELSE 0 END)
                   OVER (ORDER BY date ROWS BETWEEN 13 PRECEDING AND CURRENT ROW) AS plus_dm14,
                 AVG(CASE WHEN down_move > up_move THEN down_move ELSE 0 END)
                   OVER (ORDER BY date ROWS BETWEEN 13 PRECEDING AND CURRENT ROW) AS minus_dm14,
                 AVG(tr) OVER (ORDER BY date ROWS BETWEEN 13 PRECEDING AND CURRENT ROW) AS tr14,
                 MAX(h) OVER (ORDER BY date ROWS BETWEEN 251 PRECEDING AND CURRENT ROW) AS high252,
                 ROW_NUMBER() OVER (ORDER BY date) AS rn
          FROM n1
        ), n3 AS (
          SELECT *,
                 100.0 * abs(plus_dm14/tr14 - minus_dm14/tr14) / NULLIF(plus_dm14/tr14 + minus_dm14/tr14, 0) AS dx
          FROM n2
        ), n4 AS (
          SELECT *, AVG(dx) OVER (ORDER BY date ROWS BETWEEN 13 PRECEDING AND CURRENT ROW) AS adx14_sma
          FROM n3
        )
        SELECT n4.date,
               n4.c AS n225_close, n4.ret1 AS mkt_ret1,
               n4.realized_vol20 AS mkt_vol20,
               n4.sma25 AS mkt_sma25,
               n4.c / n4.sma25 - 1 AS mkt_sma25_gap,
               n4.sma25 / lag(n4.sma25, 5) OVER (ORDER BY n4.date) - 1 AS mkt_sma25_slope5,
               CASE WHEN n4.rn > 252 THEN n4.c / n4.high252 - 1 END AS mkt_dd252,
               n4.adx14_sma AS mkt_adx14_sma,
               topix.c AS topix_close,
               topix.o AS topix_open,
               topix.c / lag(topix.c) OVER (ORDER BY n4.date) - 1 AS topix_ret1,
               -- 現行の実現ボラ閾値 17/29% を代理指標に適用したレジーム近似（VIXなし）
               CASE
                 WHEN n4.realized_vol20 IS NULL THEN NULL
                 WHEN n4.realized_vol20 >= 29.0 THEN 'DANGER'
                 WHEN n4.realized_vol20 >= 17.0 THEN 'CAUTION'
                 ELSE 'NORMAL'
               END AS regime_vol_base,
               CASE WHEN abs(n4.ret1) >= 0.02 THEN 1 ELSE 0 END AS mkt_bigmove_prev
        FROM n4 JOIN topix USING (date)
        ORDER BY date
        """
    )

    # --- 銘柄×日付パネル ---
    con.execute("DROP TABLE IF EXISTS panel")
    con.execute(
        """
        CREATE TABLE panel AS
        WITH base AS (
          SELECT code, date, o, h, l, c, vo, va, ul, ll,
                 adj_o, adj_h, adj_l, adj_c
          FROM bars_daily
          WHERE c IS NOT NULL AND adj_c IS NOT NULL AND adj_c > 0
        ), f1 AS (
          SELECT *,
                 adj_c / lag(adj_c) OVER w - 1 AS ret1,
                 adj_c / lag(adj_c, 5) OVER w - 1 AS ret5,
                 adj_c / lag(adj_c, 20) OVER w - 1 AS ret20,
                 AVG(adj_c) OVER (w ROWS BETWEEN 4 PRECEDING AND CURRENT ROW) AS sma5,
                 AVG(adj_c) OVER (w ROWS BETWEEN 24 PRECEDING AND CURRENT ROW) AS sma25,
                 GREATEST(adj_h - adj_l, abs(adj_h - lag(adj_c) OVER w), abs(adj_l - lag(adj_c) OVER w)) AS tr,
                 (h - l) / NULLIF(c, 0) AS range_pct,
                 ROW_NUMBER() OVER w AS rn
          FROM base WINDOW w AS (PARTITION BY code ORDER BY date)
        ), f2 AS (
          SELECT *,
                 stddev_samp(ret1) OVER (w ROWS BETWEEN 19 PRECEDING AND CURRENT ROW) * sqrt(252) * 100 AS vol20,
                 AVG(tr) OVER (w ROWS BETWEEN 13 PRECEDING AND CURRENT ROW) / NULLIF(adj_c, 0) AS atr14_pct,
                 AVG(GREATEST(ret1, 0)) OVER (w ROWS BETWEEN 13 PRECEDING AND CURRENT ROW) AS avg_gain14,
                 AVG(GREATEST(-ret1, 0)) OVER (w ROWS BETWEEN 13 PRECEDING AND CURRENT ROW) AS avg_loss14,
                 AVG(va) OVER (w ROWS BETWEEN 20 PRECEDING AND 1 PRECEDING) AS va_avg20_prev,
                 AVG(va) OVER (w ROWS BETWEEN 19 PRECEDING AND CURRENT ROW) AS va_avg20,
                 MAX(adj_h) OVER (w ROWS BETWEEN 251 PRECEDING AND CURRENT ROW) AS high252,
                 MAX(adj_h) OVER (w ROWS BETWEEN 59 PRECEDING AND CURRENT ROW) AS high60,
                 SUM(CASE WHEN ret1 > 0 THEN 0 ELSE 1 END)
                   OVER (w ROWS BETWEEN 4 PRECEDING AND CURRENT ROW) AS down_in_5,
                 lag(va) OVER w AS va_prev,
                 lead(adj_o, 1) OVER w AS o_t1,
                 lead(adj_c, 1) OVER w AS c_t1,
                 lead(adj_h, 1) OVER w AS h_t1,
                 lead(adj_l, 1) OVER w AS l_t1,
                 lead(adj_c, 5) OVER w AS c_t5,
                 lead(adj_c, 10) OVER w AS c_t10,
                 lead(adj_c, 20) OVER w AS c_t20,
                 MAX(adj_h) OVER (w ROWS BETWEEN 1 FOLLOWING AND 5 FOLLOWING) AS hmax_5,
                 MIN(adj_l) OVER (w ROWS BETWEEN 1 FOLLOWING AND 5 FOLLOWING) AS lmin_5,
                 MAX(adj_h) OVER (w ROWS BETWEEN 1 FOLLOWING AND 10 FOLLOWING) AS hmax_10,
                 MIN(adj_l) OVER (w ROWS BETWEEN 1 FOLLOWING AND 10 FOLLOWING) AS lmin_10,
                 MAX(adj_h) OVER (w ROWS BETWEEN 1 FOLLOWING AND 20 FOLLOWING) AS hmax_20,
                 MIN(adj_l) OVER (w ROWS BETWEEN 1 FOLLOWING AND 20 FOLLOWING) AS lmin_20,
                 COUNT(*) OVER (w ROWS BETWEEN 1 FOLLOWING AND 20 FOLLOWING) AS fwd_days_avail,
                 lead(date, 1) OVER w AS date_t1,
                 lead(date, 20) OVER w AS date_t20
          FROM f1 WINDOW w AS (PARTITION BY code ORDER BY date)
        )
        SELECT
          code, date, c AS price_close, va, ul, ll, rn,
          ret1, ret5, ret20, range_pct, vol20, atr14_pct,
          adj_c / NULLIF(sma5, 0) - 1 AS sma5_gap,
          adj_c / NULLIF(sma25, 0) - 1 AS sma25_gap,
          CASE WHEN avg_gain14 + avg_loss14 > 0
               THEN 100.0 * avg_gain14 / (avg_gain14 + avg_loss14) END AS rsi14_sma,
          va_avg20_prev, va_avg20, va_prev,
          va / NULLIF(va_avg20_prev, 0) AS va_surge,
          CASE WHEN rn > 252 THEN adj_c / NULLIF(high252, 0) - 1 END AS off_high252,
          CASE WHEN rn > 60 THEN adj_c / NULLIF(high60, 0) - 1 END AS off_high60,
          (5 - down_in_5) AS up_days_in_5,
          -- 将来リターン（entry = t+1 の調整後始値）
          o_t1, date_t1, date_t20, fwd_days_avail,
          o_t1 / NULLIF(c, 0) / (adj_c / c) - 1 AS gap_t1,  -- 調整比率を打ち消し生値ベースのギャップ
          c_t1 / NULLIF(o_t1, 0) - 1 AS fwd_intraday1,
          h_t1 / NULLIF(o_t1, 0) - 1 AS fwd_mfe1,
          l_t1 / NULLIF(o_t1, 0) - 1 AS fwd_mae1,
          c_t5 / NULLIF(o_t1, 0) - 1 AS fwd_ret5,
          c_t10 / NULLIF(o_t1, 0) - 1 AS fwd_ret10,
          c_t20 / NULLIF(o_t1, 0) - 1 AS fwd_ret20,
          hmax_5 / NULLIF(o_t1, 0) - 1 AS fwd_mfe5,
          lmin_5 / NULLIF(o_t1, 0) - 1 AS fwd_mae5,
          hmax_10 / NULLIF(o_t1, 0) - 1 AS fwd_mfe10,
          lmin_10 / NULLIF(o_t1, 0) - 1 AS fwd_mae10,
          hmax_20 / NULLIF(o_t1, 0) - 1 AS fwd_mfe20,
          lmin_20 / NULLIF(o_t1, 0) - 1 AS fwd_mae20
        FROM f2
        """
    )

    # 指数の同一ウィンドウリターン（超過リターン計算用）を market_state 側へ付与
    con.execute("DROP TABLE IF EXISTS market_fwd")
    con.execute(
        """
        CREATE TABLE market_fwd AS
        SELECT date,
               lead(topix_open, 1) OVER w AS idx_o_t1,
               lead(topix_close, 1) OVER w / NULLIF(lead(topix_open, 1) OVER w, 0) - 1 AS idx_fwd_intraday1,
               lead(topix_close, 5) OVER w / NULLIF(lead(topix_open, 1) OVER w, 0) - 1 AS idx_fwd_ret5,
               lead(topix_close, 10) OVER w / NULLIF(lead(topix_open, 1) OVER w, 0) - 1 AS idx_fwd_ret10,
               lead(topix_close, 20) OVER w / NULLIF(lead(topix_open, 1) OVER w, 0) - 1 AS idx_fwd_ret20
        FROM market_state WINDOW w AS (ORDER BY date)
        """
    )

    n = con.sql("SELECT COUNT(*) FROM panel").fetchone()[0]
    m = con.sql("SELECT COUNT(*) FROM market_state").fetchone()[0]
    print(f"panel rows: {n:,}, market_state rows: {m}")
    print(con.sql(
        """
        SELECT MIN(date) AS min_d, MAX(date) AS max_d,
               COUNT(*) FILTER (rsi14_sma IS NOT NULL) AS with_rsi,
               COUNT(*) FILTER (fwd_ret20 IS NOT NULL) AS with_fwd20
        FROM panel
        """
    ))
    con.close()


if __name__ == "__main__":
    main()
