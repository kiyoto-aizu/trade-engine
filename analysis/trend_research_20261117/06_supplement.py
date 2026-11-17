"""06: 補完分析 — スイング型トレンド候補の多日保有プロキシと、本番レジーム記録との突合。

入力: trend_research.duckdb、data/state/market_regime_daily.sqlite3（読み取りのみ）
出力: data/analysis/trend_research_20261117/06_supplement.json

スイング条件（03の結果から設計。多重検定の注意: 本テーブルは探索結果の確認であり、
P1-P2で条件を固定し P3-P4 を検証期間として読む）:
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
          AND p.fwd_ret20 IS NOT NULL AND p.fwd_days_avail = 20
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
        WHERE substr(p.code, 1, 4) IN (SELECT symbol FROM universe_master)
          AND p.fwd_ret20 IS NOT NULL AND p.fwd_days_avail = 20
          AND p.va_avg20 >= 1e8 AND p.atr14_pct BETWEEN 0.02 AND 0.05
        GROUP BY 1 ORDER BY 1
    """)

    # --- 本番レジーム記録（検証用DB）と実現ボラ代理レジームの突合 ---
    db = REPO_ROOT / "data" / "state" / "market_regime_daily.sqlite3"
    regime_rows: list[dict] = []
    if db.exists():
        sq = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
        try:
            tables = [r[0] for r in sq.execute("SELECT name FROM sqlite_master WHERE type='table'")]
            out["regime_db_tables"] = tables
            for t in tables:
                cols = [r[1] for r in sq.execute(f"PRAGMA table_info({t})")]
                out[f"regime_db_schema::{t}"] = cols
            main_table = tables[0] if tables else None
            if main_table:
                regime_rows = [
                    dict(zip([c[0] for c in sq.execute(f"SELECT * FROM {main_table} LIMIT 1").description],
                             row))
                    for row in sq.execute(f"SELECT * FROM {main_table}")
                ]
        finally:
            sq.close()
    out["regime_db_row_count"] = len(regime_rows)
    if regime_rows:
        # 代理レジームと本番記録の一致率
        con.execute("CREATE OR REPLACE TEMP TABLE prod_regime (date DATE, regime VARCHAR)")
        date_key = next((k for k in regime_rows[0] if "date" in k.lower()), None)
        regime_key = next((k for k in regime_rows[0] if "regime" in k.lower() or "state" in k.lower()), None)
        out["regime_db_keys"] = {"date": date_key, "regime": regime_key, "all": list(regime_rows[0].keys())}
        if date_key and regime_key:
            vals = [(r[date_key], str(r[regime_key])) for r in regime_rows if r.get(date_key)]
            con.executemany("INSERT INTO prod_regime VALUES (?, ?)", vals)
            out["regime_proxy_agreement"] = q(con, """
                SELECT pr.regime AS prod, ms.regime_vol_base AS proxy, COUNT(*) AS n
                FROM prod_regime pr JOIN market_state ms ON ms.date = pr.date
                GROUP BY 1, 2 ORDER BY 1, 2
            """)

    write_json("06_supplement.json", out)
    con.close()


if __name__ == "__main__":
    main()
