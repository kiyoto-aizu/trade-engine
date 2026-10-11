"""銘柄×日付×理由の判断記録・フィルタ段の件数・日次口座状態をSQLiteへ保存するリポジトリ。

既存のfilter_decision_eventsとは別テーブル。記録専用で、売買判定には使いません。
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Mapping

# 専用列へ展開する入力項目。これ以外はdetail_jsonへ入れる
INPUT_COLUMNS: tuple[tuple[str, str], ...] = (
    ("market_regime", "TEXT"),
    ("atr", "REAL"),
    ("atr_ratio", "REAL"),
    ("atr_level", "TEXT"),
    ("rsi", "REAL"),
    ("rsi_applied_threshold", "REAL"),
    ("rsi_required_closes", "INTEGER"),
    ("rsi_actual_closes", "INTEGER"),
    ("current_price", "REAL"),
    ("allocated_budget", "REAL"),
    ("quantity_before", "INTEGER"),
    ("quantity_after", "INTEGER"),
    ("quantity_zero_reason", "TEXT"),
    ("wallet_amount", "REAL"),
    ("open_position_count", "INTEGER"),
    ("target_positions", "INTEGER"),
    ("remaining_slots", "INTEGER"),
)
_INPUT_NAMES = tuple(name for name, _ in INPUT_COLUMNS)

# ロック待ちの上限。取引ループ内で保存するため長くしない(sqlite3の既定値と同じ)
BUSY_TIMEOUT_SECONDS = 5.0

STAGE_TRADING = "trading"
STAGE_FILTERING = "filtering"
SNAPSHOT_KINDS = frozenset({"start", "end"})

# 取引ループの判断理由コード。小文字は既存ログの理由コードをそのまま使う
TRADING_REASON_CODES = (
    "INSUFFICIENT_RSI_HISTORY",
    "RSI_DATA_UNAVAILABLE",
    "BOARD_UNAVAILABLE",
    "ATR_DATA_UNAVAILABLE",
    "ATR_ENTRY_PRICE_UNAVAILABLE",
    "budget_below_one_lot",
    "buy_quantity_invalid_inputs",
    "atr_danger_skip",
    "caution_rounding_to_zero",
    "atr_quantity_adjustment_zero",
    "ORDER_QUANTITY_ZERO",
    "POSITION_LIMIT_REACHED",
    "WALLET_UNKNOWN",
    "MARKET_REGIME_DANGER_SKIP",
    "ORDER_AMOUNT_LIMIT_EXCEEDED",
    "ORDER_SAFETY_BLOCKED",
    "SELL_SIGNAL_WITHOUT_HOLDINGS",
    "NEW_BUY_HALTED_STATE_SAVE_FAILURE",
    "ORDER_REJECTED_NONE",
    "ORDER_REJECTED_RESULT",
    "ORDER_REJECTED_PAPER_PRICE_MISSING",
    "ORDER_REJECTED_PAPER_PRICE_INVALID",
)

# フィルタ段(評価対象外)の理由コード
FILTERING_REASON_CODES = (
    "FILTER_NO_HISTORICAL_DATA",
    "FILTER_AVERAGE_NON_POSITIVE",
    "FILTER_BOARD_MISSING",
    "FILTER_PRICE_MISSING",
    "FILTER_TURNOVER_MISSING",
    "FILTER_AVERAGE_TURNOVER_MISSING",
)


def _iso(value: datetime) -> str:
    return value.isoformat(timespec="seconds")


class DecisionJournalRepository:
    """記録はメモリに集約し、flush()で1トランザクションにまとめて保存します。"""

    def __init__(self, database_path: Path, execution_mode: str = "paper"):
        self.database_path = Path(database_path)
        self.execution_mode = execution_mode
        self._pending: dict[tuple[str, str, str, str], dict] = {}
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.database_path, timeout=BUSY_TIMEOUT_SECONDS)
        connection.row_factory = sqlite3.Row
        return connection

    def _initialize(self) -> None:
        input_columns_sql = ",\n".join(f"{name} {column_type}" for name, column_type in INPUT_COLUMNS)
        connection = self._connect()
        try:
            with connection:
                connection.executescript(f"""
                    CREATE TABLE IF NOT EXISTS decision_records (
                        id INTEGER PRIMARY KEY,
                        decision_date TEXT NOT NULL,
                        symbol TEXT NOT NULL,
                        reason_code TEXT NOT NULL,
                        stage TEXT NOT NULL,
                        execution_mode TEXT NOT NULL,
                        first_occurred_at TEXT NOT NULL,
                        last_occurred_at TEXT NOT NULL,
                        occurrence_count INTEGER NOT NULL,
                        {input_columns_sql},
                        detail_json TEXT NOT NULL DEFAULT '{{}}',
                        first_inputs_json TEXT NOT NULL DEFAULT '{{}}',
                        UNIQUE (execution_mode, decision_date, symbol, reason_code)
                    );
                    CREATE INDEX IF NOT EXISTS idx_decision_records_date
                        ON decision_records(decision_date, reason_code);
                    CREATE TABLE IF NOT EXISTS filter_stage_summaries (
                        decision_date TEXT NOT NULL,
                        execution_mode TEXT NOT NULL,
                        run_count INTEGER NOT NULL,
                        first_run_at TEXT NOT NULL,
                        last_run_at TEXT NOT NULL,
                        input_count INTEGER NOT NULL,
                        evaluated_count INTEGER NOT NULL,
                        skipped_count INTEGER NOT NULL,
                        selected_count INTEGER NOT NULL,
                        skip_reason_counts_json TEXT NOT NULL,
                        PRIMARY KEY (decision_date, execution_mode)
                    );
                    CREATE TABLE IF NOT EXISTS daily_account_snapshots (
                        snapshot_date TEXT NOT NULL,
                        kind TEXT NOT NULL CHECK (kind IN ('start', 'end')),
                        execution_mode TEXT NOT NULL,
                        captured_at TEXT NOT NULL,
                        wallet_amount REAL,
                        open_position_count INTEGER,
                        target_positions INTEGER,
                        holdings_json TEXT NOT NULL,
                        PRIMARY KEY (snapshot_date, kind, execution_mode)
                    );
                """)
        finally:
            connection.close()

    def record(
        self,
        stage: str,
        symbol: str,
        reason_code: str,
        occurred_at: datetime,
        inputs: Mapping[str, object] | None = None,
    ) -> None:
        """同日・同銘柄・同理由を1行へ集約するため、メモリ上で回数と時刻を更新します。"""
        values = {key: value for key, value in (inputs or {}).items() if value is not None}
        key = (stage, occurred_at.date().isoformat(), str(symbol), reason_code)
        timestamp = _iso(occurred_at)
        entry = self._pending.get(key)
        if entry is None:
            self._pending[key] = {
                "first_at": timestamp, "last_at": timestamp, "count": 1,
                "first_inputs": values, "last_inputs": values,
            }
            return
        entry["last_at"] = timestamp
        entry["count"] += 1
        entry["last_inputs"] = values

    def flush(self) -> None:
        """保留中の記録をまとめて保存します。失敗時は保留を残し、次回flushで再送します。"""
        if not self._pending:
            return
        pending = dict(self._pending)
        columns = ", ".join(_INPUT_NAMES)
        placeholders = ", ".join("?" for _ in _INPUT_NAMES)
        updates = ", ".join(f"{name} = COALESCE(excluded.{name}, {name})" for name in _INPUT_NAMES)
        sql = f"""
            INSERT INTO decision_records (
                decision_date, symbol, reason_code, stage, execution_mode,
                first_occurred_at, last_occurred_at, occurrence_count,
                {columns}, detail_json, first_inputs_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, {placeholders}, ?, ?)
            ON CONFLICT (execution_mode, decision_date, symbol, reason_code) DO UPDATE SET
                last_occurred_at = excluded.last_occurred_at,
                occurrence_count = occurrence_count + excluded.occurrence_count,
                {updates},
                detail_json = excluded.detail_json
        """
        rows = []
        for (stage, decision_date, symbol, reason_code), entry in pending.items():
            last = entry["last_inputs"]
            detail = {k: v for k, v in last.items() if k not in _INPUT_NAMES}
            rows.append((
                decision_date, symbol, reason_code, stage, self.execution_mode,
                entry["first_at"], entry["last_at"], entry["count"],
                *(last.get(name) for name in _INPUT_NAMES),
                json.dumps(detail, ensure_ascii=False, sort_keys=True, default=str),
                json.dumps(entry["first_inputs"], ensure_ascii=False, sort_keys=True, default=str),
            ))
        connection = self._connect()
        try:
            with connection:
                connection.executemany(sql, rows)
        finally:
            connection.close()
        # 保存中に追加された記録を落とさないよう、保存済みの分だけ取り除く
        for key, entry in pending.items():
            if self._pending.get(key) is entry:
                del self._pending[key]

    def record_filter_stage_summary(
        self,
        run_at: datetime,
        input_count: int,
        evaluated_count: int,
        skipped_count: int,
        selected_count: int,
        skip_reason_counts: Mapping[str, int],
    ) -> None:
        """日ごとのフィルタ段の件数を保存します。同日の再実行は件数を最新値で更新します。"""
        timestamp = _iso(run_at)
        connection = self._connect()
        try:
            with connection:
                connection.execute(
                    """
                    INSERT INTO filter_stage_summaries (
                        decision_date, execution_mode, run_count, first_run_at, last_run_at,
                        input_count, evaluated_count, skipped_count, selected_count,
                        skip_reason_counts_json
                    ) VALUES (?, ?, 1, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT (decision_date, execution_mode) DO UPDATE SET
                        run_count = run_count + 1,
                        last_run_at = excluded.last_run_at,
                        input_count = excluded.input_count,
                        evaluated_count = excluded.evaluated_count,
                        skipped_count = excluded.skipped_count,
                        selected_count = excluded.selected_count,
                        skip_reason_counts_json = excluded.skip_reason_counts_json
                    """,
                    (
                        run_at.date().isoformat(), self.execution_mode, timestamp, timestamp,
                        input_count, evaluated_count, skipped_count, selected_count,
                        json.dumps(dict(skip_reason_counts), ensure_ascii=False, sort_keys=True),
                    ),
                )
        finally:
            connection.close()

    def record_account_snapshot(
        self,
        kind: str,
        captured_at: datetime,
        wallet_amount: float | None,
        open_position_count: int | None,
        target_positions: int | None,
        holdings: list[dict],
    ) -> None:
        """startは当日の最初の1件を保持し、endは最新で上書きします。"""
        if kind not in SNAPSHOT_KINDS:
            raise ValueError(f"未対応のスナップショット種別です: {kind}")
        conflict = "DO NOTHING" if kind == "start" else (
            "DO UPDATE SET captured_at = excluded.captured_at, wallet_amount = excluded.wallet_amount, "
            "open_position_count = excluded.open_position_count, "
            "target_positions = excluded.target_positions, holdings_json = excluded.holdings_json"
        )
        connection = self._connect()
        try:
            with connection:
                connection.execute(
                    f"""
                    INSERT INTO daily_account_snapshots (
                        snapshot_date, kind, execution_mode, captured_at, wallet_amount,
                        open_position_count, target_positions, holdings_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT (snapshot_date, kind, execution_mode) {conflict}
                    """,
                    (
                        captured_at.date().isoformat(), kind, self.execution_mode, _iso(captured_at),
                        wallet_amount, open_position_count, target_positions,
                        json.dumps(holdings, ensure_ascii=False, sort_keys=True, default=str),
                    ),
                )
        finally:
            connection.close()

    def load_records(
        self,
        decision_date: str,
        stage: str,
        execution_mode: str | None = None,
    ) -> list[dict]:
        """指定日の判断記録を発生順に返します。"""
        query = (
            "SELECT * FROM decision_records WHERE decision_date = ? AND stage = ?"
        )
        parameters: list[object] = [decision_date, stage]
        if execution_mode is not None:
            query += " AND execution_mode = ?"
            parameters.append(execution_mode)
        query += " ORDER BY first_occurred_at, id"
        with self._connect() as connection:
            return [dict(row) for row in connection.execute(query, parameters)]
