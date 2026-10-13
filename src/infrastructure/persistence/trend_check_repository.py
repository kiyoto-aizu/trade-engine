"""トレンド答え合わせ結果の専用SQLite(既存の判定イベントDBとは別ファイル)。"""
from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from datetime import datetime
from pathlib import Path
from typing import Iterable, Optional

from src.domain.trend_check import definition_to_json, version_sort_key

RESULT_COLUMNS = (
    "trade_date", "symbol", "version", "in_selected", "label", "undecidable_reason",
    "open", "high", "low", "close", "atr", "open_to_close_pct", "close_position", "range_to_atr",
    "turnover_ratio", "price", "price_source", "rsi", "regime",
    "bought", "pnl", "direction_consistency", "vwap_above_ratio",
)
SUMMARY_COLUMNS = (
    "trade_date", "version", "universe_source", "regime",
    "selected_count", "selected_judged", "selected_trend", "selected_rate",
    "candidate_count", "candidate_judged", "candidate_trend", "candidate_rate",
    "selected_undecidable", "candidate_undecidable",
    "bought_count", "bought_trend", "pnl_total", "selection_source",
)


class TrendCheckRepository:
    def __init__(self, database_file: Path):
        self.database_file = Path(database_file)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        self.database_file.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.database_file)
        connection.row_factory = sqlite3.Row
        return connection

    def _initialize(self) -> None:
        with closing(self._connect()) as connection, connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS trend_criteria_versions (
                    version TEXT PRIMARY KEY,
                    definition_json TEXT NOT NULL,
                    start_date TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS trend_daily_results (
                    trade_date TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    version TEXT NOT NULL,
                    in_selected INTEGER NOT NULL,
                    label TEXT NOT NULL,
                    undecidable_reason TEXT,
                    open REAL, high REAL, low REAL, close REAL, atr REAL,
                    open_to_close_pct REAL, close_position REAL, range_to_atr REAL,
                    turnover_ratio REAL, price REAL, price_source TEXT, rsi REAL, regime TEXT,
                    bought INTEGER, pnl REAL,
                    direction_consistency REAL, vwap_above_ratio REAL,
                    PRIMARY KEY (trade_date, symbol, version)
                );
                CREATE INDEX IF NOT EXISTS idx_trend_daily_results_version_date
                    ON trend_daily_results(version, trade_date);
                CREATE TABLE IF NOT EXISTS trend_daily_summaries (
                    trade_date TEXT NOT NULL,
                    version TEXT NOT NULL,
                    universe_source TEXT NOT NULL,
                    regime TEXT,
                    selected_count INTEGER NOT NULL,
                    selected_judged INTEGER NOT NULL,
                    selected_trend INTEGER NOT NULL,
                    selected_rate REAL,
                    candidate_count INTEGER NOT NULL,
                    candidate_judged INTEGER NOT NULL,
                    candidate_trend INTEGER NOT NULL,
                    candidate_rate REAL,
                    selected_undecidable INTEGER NOT NULL,
                    candidate_undecidable INTEGER NOT NULL,
                    bought_count INTEGER NOT NULL,
                    bought_trend INTEGER NOT NULL,
                    pnl_total REAL,
                    selection_source TEXT,
                    PRIMARY KEY (trade_date, version)
                );
                """
            )
            columns = {
                row["name"]
                for row in connection.execute("PRAGMA table_info(trend_daily_summaries)")
            }
            if "selection_source" not in columns:
                connection.execute(
                    "ALTER TABLE trend_daily_summaries ADD COLUMN selection_source TEXT"
                )

    # --- 版 ---
    def register_version(
        self, version: str, definition: dict, start_date: str, reason: str, overwrite: bool = False
    ) -> bool:
        """版を登録する。既存の版は上書きしない(overwrite指定時のみ)。登録したらTrue。"""
        with closing(self._connect()) as connection, connection:
            exists = connection.execute(
                "SELECT 1 FROM trend_criteria_versions WHERE version = ?", (version,)
            ).fetchone()
            if exists and not overwrite:
                return False
            connection.execute(
                "INSERT OR REPLACE INTO trend_criteria_versions VALUES (?, ?, ?, ?, ?)",
                (version, definition_to_json(definition), start_date, reason,
                 datetime.now().isoformat(timespec="seconds")),
            )
            return True

    def list_versions(self) -> list[dict]:
        with closing(self._connect()) as connection:
            rows = connection.execute("SELECT * FROM trend_criteria_versions").fetchall()
        versions = [
            {**dict(row), "definition": json.loads(row["definition_json"])} for row in rows
        ]
        return sorted(versions, key=lambda item: version_sort_key(item["version"]))

    def get_version(self, version: str) -> Optional[dict]:
        return next((item for item in self.list_versions() if item["version"] == version), None)

    def latest_version(self) -> Optional[dict]:
        versions = self.list_versions()
        return versions[-1] if versions else None

    # --- 日次結果 ---
    def replace_day(self, trade_date: str, version: str, rows: Iterable[dict], summary: dict) -> None:
        """同じ日・同じ版の結果を1トランザクションで置き換える(冪等)。他の版には触れない。"""
        rows = list(rows)
        with closing(self._connect()) as connection, connection:
            connection.execute(
                "DELETE FROM trend_daily_results WHERE trade_date = ? AND version = ?", (trade_date, version)
            )
            connection.execute(
                "DELETE FROM trend_daily_summaries WHERE trade_date = ? AND version = ?", (trade_date, version)
            )
            connection.executemany(
                f"INSERT INTO trend_daily_results ({', '.join(RESULT_COLUMNS)}) "
                f"VALUES ({', '.join('?' for _ in RESULT_COLUMNS)})",
                [tuple(row.get(column) for column in RESULT_COLUMNS) for row in rows],
            )
            connection.execute(
                f"INSERT INTO trend_daily_summaries ({', '.join(SUMMARY_COLUMNS)}) "
                f"VALUES ({', '.join('?' for _ in SUMMARY_COLUMNS)})",
                tuple(summary.get(column) for column in SUMMARY_COLUMNS),
            )

    def load_rows(self, version: str, start: str, end: str) -> list[dict]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                "SELECT * FROM trend_daily_results WHERE version = ? AND trade_date BETWEEN ? AND ? "
                "ORDER BY trade_date, in_selected DESC, symbol",
                (version, start, end),
            ).fetchall()
        return [dict(row) for row in rows]

    def load_summaries(self, version: str, start: str, end: str) -> list[dict]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                "SELECT * FROM trend_daily_summaries WHERE version = ? AND trade_date BETWEEN ? AND ? "
                "ORDER BY trade_date",
                (version, start, end),
            ).fetchall()
        return [dict(row) for row in rows]

    def versions_with_rows(self, start: str, end: str) -> list[str]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                "SELECT DISTINCT version FROM trend_daily_summaries WHERE trade_date BETWEEN ? AND ?",
                (start, end),
            ).fetchall()
        return sorted((row["version"] for row in rows), key=version_sort_key)
