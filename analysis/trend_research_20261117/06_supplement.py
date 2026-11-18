"""06: 補完分析 — スイング型トレンド候補の多日保有プロキシと、本番レジーム記録との突合。

入力: trend_research.duckdb、data/state/market_regime_daily.sqlite3（読み取りのみ）
出力: data/analysis/trend_research_20261117/06_supplement.json

スイング条件（03の探索結果に基づき設計）:
  - 注意: 条件は全期間の探索結果（03）から作られているため、本集計はインサンプルの確認であり
    独立検証ではない。期間別の符号一貫性の確認と、無条件ベースラインとの差分の把握が目的。
  - 高値圏: off_high252 >= -0.05
  - 中程度モメンタム: ret20 が 0.10-0.25
  - 流動性: va_avg20 >= 1e8
  - ボラ: atr14_pct 0.02-0.05
  - エントリー: 翌営業日始値、保有: 20営業日（コスト往復0.21%を控除）
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from common import REPO_ROOT, connect, rows_to_dicts, write_json


def q(con, sql: str) -> list[dict]:
    return rows_to_dicts(con.sql(sql))


def main() -> None:
    con = connect()
    out: dict = {}

    con.execute("""
        CREATE OR REPLACE TEMP VIEW swing AS
        SELECT p.*, mf.idx_fwd_ret20,
               CASE WHEN p.date < DATE '2023-11-01' THEN 'P1'
                    WHEN p.date < DATE '2024-11-01' THEN 'P2'
                    WHEN p.date < DATE '2025-11-01' THEN 'P3'
                    ELSE 'P4' END AS period
        FROM panel p
        JOIN market_fwd mf USING (date)
        WHERE substr(p.code, 1, 4) IN (SELECT symbol FROM universe_master)
          AND substr(p.code, 5, 1) = '0'
          AND p.fwd_ret20 IS NOT NULL AND p.date_t20 = mf.idx_date_t20  -- 銘柄と指数の窓終端を一致させる
          AND p.off_high252 >= -0.05
          AND p.ret20 BETWEEN 0.10 AND 0.25
          AND p.va_avg20 >= 1e8
          AND p.atr14_pct BETWEEN 0.02 AND 0.05
    """)
    out["swing_trend_candidates"] = q(con, """
        SELECT period, COUNT(*) AS n, COUNT(DISTINCT date) AS days,
               ROUND(COUNT(*) * 1.0 / COUNT(DISTINCT date), 1) AS cand_per_day,
               ROUND(AVG(fwd_ret20 - 0.0021), 5) AS net_mean,
               ROUND(quantile_cont(fwd_ret20 - 0.0021, 0.5), 5) AS net_med,
               ROUND(quantile_cont(fwd_ret20, 0.1), 5) AS p10,
               ROUND(quantile_cont(fwd_ret20, 0.9), 5) AS p90,
               ROUND(AVG((fwd_ret20 > 0.0021)::INT), 4) AS net_win,
               ROUND(AVG(fwd_ret20 - idx_fwd_ret20), 5) AS excess_mean,
               ROUND(AVG((fwd_ret20 - idx_fwd_ret20 >= 0.10)::INT), 4) AS trend10_rate,
               ROUND(quantile_cont(fwd_mae20, 0.5), 4) AS mae_med
        FROM swing GROUP BY 1 ORDER BY 1
    """)
    # 価格帯900円以下に絞った場合（現行の資金制約との両立確認）
    out["swing_trend_le900"] = q(con, """
        SELECT period, COUNT(*) AS n, COUNT(DISTINCT date) AS days,
               ROUND(AVG(fwd_ret20 - 0.0021), 5) AS net_mean,
               ROUND(quantile_cont(fwd_ret20 - 0.0021, 0.5), 5) AS net_med,
               ROUND(AVG((fwd_ret20 > 0.0021)::INT), 4) AS net_win
        FROM swing WHERE price_close <= 900 GROUP BY 1 ORDER BY 1
    """)
    # 比較: 同じ流動性・ボラ帯でモメンタム条件なし（条件の寄与確認）
    out["swing_baseline_same_liq_vol"] = q(con, """
        SELECT CASE WHEN p.date < DATE '2023-11-01' THEN 'P1'
                    WHEN p.date < DATE '2024-11-01' THEN 'P2'
                    WHEN p.date < DATE '2025-11-01' THEN 'P3'
                    ELSE 'P4' END AS period,
               COUNT(*) AS n,
               ROUND(AVG(p.fwd_ret20 - 0.0021), 5) AS net_mean,
               ROUND(AVG((p.fwd_ret20 > 0.0021)::INT), 4) AS net_win
        FROM panel p
        JOIN market_fwd mf USING (date)
        WHERE substr(p.code, 1, 4) IN (SELECT symbol FROM universe_master)
          AND substr(p.code, 5, 1) = '0'
          AND p.fwd_ret20 IS NOT NULL AND p.date_t20 = mf.idx_date_t20
          AND p.va_avg20 >= 1e8 AND p.atr14_pct BETWEEN 0.02 AND 0.05
        GROUP BY 1 ORDER BY 1
    """)

    # --- 本番レジーム記録（検証用DB）と実現ボラ代理レジームの突合 ---
    # 本番は trade_date 当日の朝に「前営業日までのデータ」で判定するため、
    # 代理側は trade_date の前営業日の market_state と結合する（同日結合は1日先読みになる）
    db = REPO_ROOT / "data" / "state" / "market_regime_daily.sqlite3"
    regime_rows: list[tuple] = []
    if db.exists():
        sq = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
        try:
            tables = [r[0] for r in sq.execute("SELECT name FROM sqlite_master WHERE type='table'")]
            out["regime_db_tables"] = tables
            if "market_regime_daily" in tables:
                regime_rows = list(sq.execute(
                    "SELECT trade_date, final_regime, regime_before_relief, execution_mode"
                    " FROM market_regime_daily WHERE data_available = 1"
                ))
        finally:
            sq.close()
    out["regime_db_row_count"] = len(regime_rows)
    if regime_rows:
        con.execute(
            "CREATE OR REPLACE TEMP TABLE prod_regime (trade_date DATE, final_regime VARCHAR,"
            " regime_before_relief VARCHAR, execution_mode VARCHAR)"
        )
        con.executemany("INSERT INTO prod_regime VALUES (?, ?, ?, ?)", regime_rows)
        out["regime_proxy_agreement"] = q(con, """
            WITH proxy_lag AS (
              SELECT date,
                     lead(date) OVER (ORDER BY date) AS next_trading_date,
                     regime_vol_base
              FROM market_state
            )
            SELECT pr.final_regime AS prod_final, pr.regime_before_relief AS prod_before_relief,
                   pl.regime_vol_base AS proxy_prev_day, COUNT(*) AS n
            FROM prod_regime pr
            JOIN proxy_lag pl ON pl.next_trading_date = pr.trade_date
            GROUP BY 1, 2, 3 ORDER BY 1, 2, 3
        """)

    write_json("06_supplement.json", out)
    con.close()


if __name__ == "__main__":
    main()
