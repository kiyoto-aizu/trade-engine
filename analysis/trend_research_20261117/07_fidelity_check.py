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
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from common import REPO_ROOT, connect, rows_to_dicts, write_json


def load_symbol_files(directory: Path) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for f in sorted(directory.glob("????-??-??.json")):
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            continue
        if isinstance(d, dict) and isinstance(d.get("symbols"), list):
            out[f.stem] = [str(s) for s in d["symbols"]]
    return out


def main() -> None:
    con = connect()
    out: dict = {}

    screening = load_symbol_files(REPO_ROOT / "data" / "screening")
    filtering = load_symbol_files(REPO_ROOT / "data" / "filtering")
    out["actual_files"] = {
        "screening_days": len(screening),
        "screening_range": [min(screening), max(screening)] if screening else None,
        "filtering_days": len(filtering),
        "filtering_range": [min(filtering), max(filtering)] if filtering else None,
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

    # スクリーニング一致率: 実運用の各日の50銘柄のうち、近似上位50に含まれた割合
    out["screening_overlap"] = rows_to_dicts(con.sql("""
        WITH per_day AS (
          SELECT a.prev_date,
                 COUNT(*) AS actual_n,
                 COUNT(ap.symbol4) AS matched
          FROM actual_screening a
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

    # フィルタリング一致率: 実運用の各日の10銘柄のうち、近似の朝サージ上位10に含まれた割合
    out["filtering_overlap"] = rows_to_dicts(con.sql("""
        WITH approx_top10 AS (
          SELECT date, symbol4 FROM (
            SELECT date, symbol4,
                   rank() OVER (PARTITION BY date ORDER BY morning_surge DESC NULLS LAST) AS r_surge
            FROM approx WHERE morning_surge IS NOT NULL
          ) WHERE r_surge <= 10
        ), per_day AS (
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

    # 実運用フィルタ10銘柄の実測成績（minute_day_summaryベース、9:35→15:25、コスト前）
    out["actual_filtering_performance"] = rows_to_dicts(con.sql("""
        SELECT COUNT(*) AS n, COUNT(DISTINCT a.date) AS days,
               ROUND(AVG(m.close_1525 / m.entry_0935_close - 1), 5) AS ret_mean,
               ROUND(quantile_cont(m.close_1525 / m.entry_0935_close - 1, 0.5), 5) AS ret_med,
               ROUND(AVG((m.close_1525 / m.entry_0935_close - 1 > 0)::INT), 4) AS win
        FROM actual_filtering a
        JOIN minute_day_summary m
          ON substr(m.code, 1, 4) = a.symbol AND substr(m.code, 5, 1) = '0' AND m.date = a.date
        WHERE m.entry_0935_close IS NOT NULL AND m.close_1525 IS NOT NULL
    """))[0]

    write_json("07_fidelity_check.json", out)
    con.close()


if __name__ == "__main__":
    main()
