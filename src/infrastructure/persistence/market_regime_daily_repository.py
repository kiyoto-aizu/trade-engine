"""市場レジームの判定結果を、取引日・実行モードごとに1行で保存するリポジトリ(検証用・記録専用)。"""
from __future__ import annotations

import json
import sqlite3
from datetime import date, datetime
from pathlib import Path
from typing import Mapping

_COLUMNS: tuple[str, ...] = (
    "trade_date", "execution_mode", "decided_at", "final_regime", "regime_before_relief",
    "trend_relief_applied", "realized_volatility_percent", "vix", "nikkei_change_percent", "adx",
    "nikkei_last_bar_date", "nikkei_last_close", "vix_last_bar_date", "vix_last_close",
    "data_available", "failure_reason", "thresholds_json", "recorded_at",
)


class MarketRegimeDailyRepository:
    """同じ取引日・同じ実行モードの再保存は上書きし、重複行を作りません。"""

    def __init__(self, database_path: Path):
        self.database_path = Path(database_path)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.database_path)
        connection.row_factory = sqlite3.Row
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute("""
                CREATE TABLE IF NOT EXISTS market_regime_daily (
                    trade_date TEXT NOT NULL,
                    execution_mode TEXT NOT NULL,
                    decided_at TEXT NOT NULL,
                    final_regime TEXT NOT NULL,
                    regime_before_relief TEXT,
                    trend_relief_applied INTEGER NOT NULL,
                    realized_volatility_percent REAL,
                    vix REAL,
                    nikkei_change_percent REAL,
                    adx REAL,
                    nikkei_last_bar_date TEXT,
                    nikkei_last_close REAL,
                    vix_last_bar_date TEXT,
                    vix_last_close REAL,
                    data_available INTEGER NOT NULL,
                    failure_reason TEXT,
                    thresholds_json TEXT NOT NULL DEFAULT '{}',
                    recorded_at TEXT NOT NULL,
                    PRIMARY KEY (trade_date, execution_mode)
                )
            """)

    @staticmethod
    def _text(value) -> str | None:
        if value is None:
            return None
        return value.isoformat() if isinstance(value, (date, datetime)) else str(getattr(value, "value", value))

    def save(
        self,
        assessment,
        decided_at: datetime,
        execution_mode: str,
        thresholds: Mapping[str, object] | None = None,
    ) -> None:
        """判定結果を保存します。MarketRegimeAssessment互換のオブジェクトを受け取ります。"""
        def read(name: str):
            return getattr(assessment, name, None)

        values = (
            decided_at.date().isoformat(),
            execution_mode,
            decided_at.isoformat(timespec="seconds"),
            self._text(read("regime")),
            self._text(read("regime_before_relief")),
            int(bool(read("trend_relief_applied"))),
            read("realized_volatility_percent"),
            read("vix"),
            read("nikkei_change_percent"),
            read("adx"),
            self._text(read("nikkei_last_bar_date")),
            read("nikkei_last_close"),
            self._text(read("vix_last_bar_date")),
            read("vix_last_close"),
            int(bool(read("data_available"))),
            read("failure_reason"),
            json.dumps(dict(thresholds or {}), ensure_ascii=False, sort_keys=True),
            datetime.now().isoformat(timespec="seconds"),
        )
        placeholders = ", ".join("?" for _ in _COLUMNS)
        with self._connect() as connection:
            connection.execute(
                f"INSERT OR REPLACE INTO market_regime_daily ({', '.join(_COLUMNS)}) VALUES ({placeholders})",
                values,
            )

    def load(self, trade_date: date, execution_mode: str | None = None) -> list[dict]:
        query = "SELECT * FROM market_regime_daily WHERE trade_date = ?"
        parameters: list[object] = [trade_date.isoformat()]
        if execution_mode is not None:
            query += " AND execution_mode = ?"
            parameters.append(execution_mode)
        with self._connect() as connection:
            return [dict(row) for row in connection.execute(query + " ORDER BY execution_mode", parameters)]
