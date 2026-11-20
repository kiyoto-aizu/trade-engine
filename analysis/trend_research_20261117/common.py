"""共通ユーティリティ: canonical 世代解決と DuckDB 接続。

入力: D:\\jquants\\canonical（読み取りのみ）
出力: なし（他スクリプトから import される）
"""
from __future__ import annotations

import os
from pathlib import Path

import duckdb

REPO_ROOT = Path(__file__).resolve().parents[2]
OUTPUT_DIR = REPO_ROOT / "data" / "analysis" / "trend_research_20261117"
DB_PATH = OUTPUT_DIR / "trend_research.duckdb"

CANONICAL_ROOT = Path(os.getenv("JQUANTS_CANONICAL_DIRECTORY", r"D:\jquants\canonical"))
ANALYSIS_DATA_CUTOFF = "2026-11-17"


def current_parquet_paths(
    kind: str, through_date: str | None = None
) -> list[tuple[str, str]]:
    """kind ('daily'|'minute') の date=ディレクトリごとに CURRENT 世代の parquet パスを返す。

    戻り値: [(date_str, posix_path), ...]（日付昇順）。CURRENT が無い日付はスキップして警告。
    """
    base = CANONICAL_ROOT / kind
    out: list[tuple[str, str]] = []
    skipped: list[str] = []
    for d in sorted(base.iterdir()):
        if not d.name.startswith("date="):
            continue
        date_str = d.name[5:]
        if through_date is not None and date_str > through_date:
            continue
        cur = d / "CURRENT"
        if not cur.is_file():
            skipped.append(date_str)
            continue
        gen = cur.read_text(encoding="utf-8").strip()
        part = d / "generations" / gen / "part-00000.parquet"
        if not part.is_file():
            skipped.append(date_str)
            continue
        out.append((date_str, part.as_posix()))
    if skipped:
        print(f"[warn] {kind}: CURRENT/parquet 欠落でスキップ: {skipped}")
    return out


def connect(read_only: bool = False) -> duckdb.DuckDBPyConnection:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    return duckdb.connect(str(DB_PATH), read_only=read_only)


def write_json(name: str, payload) -> Path:
    import json

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    path = OUTPUT_DIR / name
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(f"[out] {path}")
    return path


def rows_to_dicts(rel) -> list[dict]:
    cols = [c[0] for c in rel.description]
    return [dict(zip(cols, row)) for row in rel.fetchall()]
