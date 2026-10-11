"""フィルタ・見送り・緩和の判定イベントをSQLiteへ永続化するリポジトリ。"""
from __future__ import annotations

import json
import math
import sqlite3
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Mapping

# 当日基準列。既存DBには ALTER TABLE ADD COLUMN で追加する(既存行はNULLのまま)
SAME_DAY_COLUMNS: tuple[tuple[str, str], ...] = (
    ("same_day_close_price", "REAL"),
    ("same_day_high", "REAL"),
    ("same_day_low", "REAL"),
    ("same_day_last_observed_at", "TEXT"),
    ("same_day_observation_count", "INTEGER"),
    ("same_day_price_change_percent", "REAL"),
    ("same_day_hypothetical_pnl_before_cost", "REAL"),
    ("same_day_outcome", "TEXT"),
    ("same_day_finalized_at", "TEXT"),
    ("same_day_data_quality", "TEXT"),
)

# 見送り時刻の日経225の日中値(検証用・nullable)
NIKKEI_INTRADAY_COLUMNS = (
    ("nikkei_intraday_change_percent", "REAL"),
    ("nikkei_intraday_as_of", "TEXT"),
)
# data_quality判定の仮置き閾値
STALE_LAST_OBSERVATION_MINUTES = 30
FEW_OBSERVATIONS_MIN_EXPECTED = 5
FEW_OBSERVATIONS_RATIO = 0.2
DEFAULT_SESSION_END = time(15, 30)
DEFAULT_LOOP_INTERVAL_SECONDS = 60
# 日足のみの再生で作られたイベント。当日基準の集計から除外する
DAILY_REPLAY_FLAG = "DAILY_REPLAY_NO_INTRADAY"


EVENT_TYPES = frozenset({
    "ATR_DANGER_SKIP",
    "ATR_STOP_EXIT",
    "MARKET_REGIME_DANGER_SKIP",
    "MARKET_REGIME_CAUTION_RSI_FILTER",
    "ADX_TREND_RELIEF",
})


class FilterDecisionRepository:
    """判定イベントの観測値をイベント行へ集約して保存します。"""

    def __init__(
        self,
        database_path: Path,
        allowed_execution_modes: frozenset[str] | None = None,
    ):
        self.database_path = database_path
        self.allowed_execution_modes = allowed_execution_modes
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.database_path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS filter_decision_events (
                    id INTEGER PRIMARY KEY,
                    event_type TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    execution_mode TEXT NOT NULL,
                    occurred_at TEXT NOT NULL,
                    reference_price REAL NOT NULL,
                    quantity INTEGER NOT NULL,
                    atr REAL,
                    true_range REAL,
                    atr_ratio REAL,
                    atr_level TEXT,
                    stop_multiplier REAL,
                    market_regime TEXT,
                    realized_volatility_percent REAL,
                    vix REAL,
                    nikkei_change_percent REAL,
                    adx REAL,
                    rsi REAL,
                    rsi_normal_threshold REAL,
                    rsi_applied_threshold REAL,
                    input_json TEXT NOT NULL DEFAULT '{}',
                    lowest_price REAL NOT NULL,
                    highest_price REAL NOT NULL,
                    last_price REAL NOT NULL,
                    last_observed_at TEXT NOT NULL,
                    observation_count INTEGER NOT NULL,
                    status TEXT NOT NULL DEFAULT 'observing',
                    finalized_at TEXT,
                    price_change_percent REAL,
                    hypothetical_pnl_before_cost REAL,
                    outcome TEXT,
                    created_at TEXT NOT NULL,
                    CHECK (event_type IN (
                        'ATR_DANGER_SKIP', 'ATR_STOP_EXIT', 'MARKET_REGIME_DANGER_SKIP',
                        'MARKET_REGIME_CAUTION_RSI_FILTER', 'ADX_TREND_RELIEF'
                    )),
                    CHECK (status IN ('observing', 'finalized'))
                );
                CREATE INDEX IF NOT EXISTS idx_filter_decision_open
                    ON filter_decision_events(status, symbol, execution_mode);
                CREATE INDEX IF NOT EXISTS idx_filter_decision_finalized
                    ON filter_decision_events(status, finalized_at, execution_mode);
            """)
            existing_columns = {
                row["name"] for row in connection.execute("PRAGMA table_info(filter_decision_events)")
            }
            for name, column_type in (*SAME_DAY_COLUMNS, *NIKKEI_INTRADAY_COLUMNS):
                if name in existing_columns:
                    continue
                try:
                    connection.execute(f"ALTER TABLE filter_decision_events ADD COLUMN {name} {column_type}")
                except sqlite3.OperationalError as error:
                    if "duplicate column" not in str(error).lower():
                        raise

    @staticmethod
    def _iso_datetime(value: datetime) -> str:
        return value.isoformat(timespec="seconds")

    @staticmethod
    def _business_days_elapsed(start: date, end: date) -> int:
        """発生日の翌日から終了日までの平日数を返します。"""
        if end <= start:
            return 0
        elapsed = 0
        current = start + timedelta(days=1)
        while current <= end:
            if current.weekday() < 5:
                elapsed += 1
            current += timedelta(days=1)
        return elapsed

    def record_event(
        self,
        event_type: str,
        symbol: str,
        occurred_at: datetime,
        reference_price: float,
        quantity: int,
        inputs: Mapping[str, object] | None = None,
        execution_mode: str = "paper",
        same_day_data_quality: str | None = None,
    ) -> int:
        """イベントを記録し、同日・同種別・同銘柄の未確定イベントは再利用します。"""
        if (
            self.allowed_execution_modes is not None
            and execution_mode not in self.allowed_execution_modes
        ):
            raise ValueError(f"execution_modeは許可されていません: {execution_mode}")
        if event_type not in EVENT_TYPES:
            raise ValueError(f"未対応の判定イベント種別です: {event_type}")
        if reference_price <= 0:
            raise ValueError("reference_priceは正数を指定してください")
        if quantity <= 0:
            raise ValueError("quantityは正数を指定してください")

        values = dict(inputs or {})
        occurred_text = self._iso_datetime(occurred_at)
        with self._connect() as connection:
            existing = connection.execute(
                """
                SELECT id FROM filter_decision_events
                WHERE event_type = ? AND symbol = ? AND execution_mode = ?
                  AND status = 'observing' AND substr(occurred_at, 1, 10) = ?
                """,
                (event_type, symbol, execution_mode, occurred_at.date().isoformat()),
            ).fetchone()
            if existing is not None:
                return int(existing["id"])
            cursor = connection.execute(
                """
                INSERT INTO filter_decision_events (
                    event_type, symbol, execution_mode, occurred_at, reference_price, quantity,
                    atr, true_range, atr_ratio, atr_level, stop_multiplier, market_regime,
                    realized_volatility_percent, vix, nikkei_change_percent, adx, rsi,
                    rsi_normal_threshold, rsi_applied_threshold, input_json,
                    lowest_price, highest_price, last_price, last_observed_at,
                    observation_count, created_at,
                    same_day_close_price, same_day_high, same_day_low,
                    same_day_last_observed_at, same_day_observation_count, same_day_data_quality,
                    nikkei_intraday_change_percent, nikkei_intraday_as_of
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?, ?, 1, ?, ?, ?)
                """,
                (
                    event_type, symbol, execution_mode, occurred_text, reference_price, quantity,
                    values.get("atr"), values.get("true_range"), values.get("atr_ratio"),
                    values.get("atr_level"), values.get("stop_multiplier"), values.get("market_regime"),
                    values.get("realized_volatility_percent"), values.get("vix"),
                    values.get("nikkei_change_percent"), values.get("adx"), values.get("rsi"),
                    values.get("rsi_normal_threshold"), values.get("rsi_applied_threshold"),
                    json.dumps(values, ensure_ascii=False, sort_keys=True),
                    reference_price, reference_price, reference_price, occurred_text, occurred_text,
                    reference_price, reference_price, reference_price, occurred_text,
                    same_day_data_quality,
                    values.get("nikkei_intraday_change_percent"), values.get("nikkei_intraday_as_of"),
                ),
            )
            return int(cursor.lastrowid)

    def load_open_events(self, execution_mode: str | None = None) -> list[dict]:
        query = "SELECT * FROM filter_decision_events WHERE status = 'observing'"
        parameters: tuple[object, ...] = ()
        if execution_mode is not None:
            query += " AND execution_mode = ?"
            parameters = (execution_mode,)
        query += " ORDER BY occurred_at, id"
        with self._connect() as connection:
            return [self._row_to_dict(row) for row in connection.execute(query, parameters)]

    def update_observation(self, event_id: int, observed_at: datetime, price: float) -> bool:
        """未確定イベントの観測集約値を更新し、対象があればTrueを返します。"""
        if price <= 0:
            raise ValueError("観測価格は正数を指定してください")
        observed_text = self._iso_datetime(observed_at)
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE filter_decision_events
                SET lowest_price = MIN(lowest_price, ?), highest_price = MAX(highest_price, ?),
                    last_price = ?, last_observed_at = ?, observation_count = observation_count + 1
                WHERE id = ? AND status = 'observing'
                """,
                (price, price, price, observed_text, event_id),
            )
            if cursor.rowcount != 1:
                return False
            # 当日列は、観測日が発生日と同じで当日未確定の行(列がNULLの旧行は除く)だけ更新する
            connection.execute(
                """
                UPDATE filter_decision_events
                SET same_day_high = MAX(same_day_high, ?), same_day_low = MIN(same_day_low, ?),
                    same_day_close_price = ?, same_day_last_observed_at = ?,
                    same_day_observation_count = COALESCE(same_day_observation_count, 0) + 1
                WHERE id = ? AND same_day_close_price IS NOT NULL AND same_day_finalized_at IS NULL
                  AND substr(occurred_at, 1, 10) = ?
                """,
                (price, price, price, observed_text, event_id, observed_text[:10]),
            )
            return True

    def mark_same_day_board_unavailable(
        self, symbol: str, observed_at: datetime, execution_mode: str | None = None
    ) -> int:
        """当日に板取得不能だった銘柄の、当日未確定イベントへBOARD_UNAVAILABLEを記録します。"""
        query = (
            "SELECT id, same_day_data_quality FROM filter_decision_events "
            "WHERE symbol = ? AND same_day_close_price IS NOT NULL AND same_day_finalized_at IS NULL "
            "AND substr(occurred_at, 1, 10) = ?"
        )
        parameters: list[object] = [symbol, observed_at.date().isoformat()]
        if execution_mode is not None:
            query += " AND execution_mode = ?"
            parameters.append(execution_mode)
        updated = 0
        with self._connect() as connection:
            for row in connection.execute(query, parameters).fetchall():
                flags = self._split_flags(row["same_day_data_quality"])
                if "BOARD_UNAVAILABLE" in flags:
                    continue
                flags.append("BOARD_UNAVAILABLE")
                connection.execute(
                    "UPDATE filter_decision_events SET same_day_data_quality = ? WHERE id = ?",
                    (",".join(flags), row["id"]),
                )
                updated += 1
        return updated

    @staticmethod
    def _split_flags(value: object) -> list[str]:
        return [item for item in str(value or "").split(",") if item and item != "OK"]

    @staticmethod
    def _parse_naive(value: str) -> datetime:
        parsed = datetime.fromisoformat(value)
        return parsed.replace(tzinfo=None) if parsed.tzinfo is not None else parsed

    def finalize_same_day_events(
        self,
        as_of: datetime | date,
        execution_mode: str | None = None,
        include_today: bool = False,
        session_end: time = DEFAULT_SESSION_END,
        loop_interval_seconds: int = DEFAULT_LOOP_INTERVAL_SECONDS,
    ) -> list[dict]:
        """当日列が未確定のイベントを確定します。

        発生日がas_of日より前のもの(再起動後の取り残し)は常に対象で、
        include_today=Trueのときだけ発生日=as_of日のものも対象にします。
        """
        as_of_date = as_of.date() if isinstance(as_of, datetime) else as_of
        finalized_at = (
            self._iso_datetime(as_of) if isinstance(as_of, datetime)
            else datetime.combine(as_of, time.min).isoformat(timespec="seconds")
        )
        query = (
            "SELECT * FROM filter_decision_events "
            "WHERE same_day_finalized_at IS NULL AND same_day_close_price IS NOT NULL"
        )
        parameters: list[object] = []
        if execution_mode is not None:
            query += " AND execution_mode = ?"
            parameters.append(execution_mode)
        query += " ORDER BY occurred_at, id"
        finalized: list[dict] = []
        with self._connect() as connection:
            rows = [self._row_to_dict(row) for row in connection.execute(query, parameters)]
            for event in rows:
                occurred = self._parse_naive(event["occurred_at"])
                if occurred.date() > as_of_date or (occurred.date() == as_of_date and not include_today):
                    continue
                evaluation = self._evaluate(
                    str(event["event_type"]), float(event["reference_price"]),
                    float(event["same_day_close_price"]), int(event["quantity"]),
                )
                data_quality = self._same_day_data_quality(
                    event, occurred, session_end, loop_interval_seconds
                )
                cursor = connection.execute(
                    """
                    UPDATE filter_decision_events
                    SET same_day_finalized_at = ?, same_day_price_change_percent = ?,
                        same_day_hypothetical_pnl_before_cost = ?, same_day_outcome = ?,
                        same_day_data_quality = ?
                    WHERE id = ? AND same_day_finalized_at IS NULL
                    """,
                    (
                        finalized_at, evaluation["price_change_percent"],
                        evaluation["hypothetical_pnl_before_cost"], evaluation["outcome"],
                        data_quality, event["id"],
                    ),
                )
                if cursor.rowcount == 1:
                    finalized.append({
                        **event,
                        "same_day_finalized_at": finalized_at,
                        "same_day_price_change_percent": evaluation["price_change_percent"],
                        "same_day_hypothetical_pnl_before_cost": evaluation["hypothetical_pnl_before_cost"],
                        "same_day_outcome": evaluation["outcome"],
                        "same_day_data_quality": data_quality,
                    })
        return finalized

    def _same_day_data_quality(
        self, event: Mapping[str, object], occurred: datetime,
        session_end: time, loop_interval_seconds: int,
    ) -> str:
        flags = self._split_flags(event.get("same_day_data_quality"))
        end = datetime.combine(occurred.date(), session_end)
        last_observed = self._parse_naive(str(event["same_day_last_observed_at"]))
        gap_minutes = (end - last_observed).total_seconds() / 60
        if gap_minutes > STALE_LAST_OBSERVATION_MINUTES:
            flags.append(f"STALE_LAST_OBSERVATION({gap_minutes:.0f}min)")
        interval = max(1, loop_interval_seconds)
        expected = int(max(0.0, (end - occurred).total_seconds()) // interval) + 1
        count = int(event.get("same_day_observation_count") or 0)
        if expected >= FEW_OBSERVATIONS_MIN_EXPECTED and count < math.ceil(expected * FEW_OBSERVATIONS_RATIO):
            flags.append(f"FEW_OBSERVATIONS({count}/{expected})")
        return ",".join(flags) if flags else "OK"

    def update_open_event_observations(
        self,
        observed_at: datetime,
        prices_by_symbol: Mapping[str, float],
        execution_mode: str | None = None,
    ) -> int:
        """取得できた板価格で、同じ銘柄の未確定イベントをまとめて更新します。"""
        open_events = self.load_open_events(execution_mode)
        updated = 0
        for event in open_events:
            price = prices_by_symbol.get(event["symbol"])
            if price is not None and self.update_observation(event["id"], observed_at, float(price)):
                updated += 1
        return updated

    def finalize_due_events(
        self,
        as_of: datetime | date,
        observation_days: int,
        execution_mode: str | None = None,
    ) -> list[dict]:
        """指定営業日数を経過したイベントを確定し、確定済みデータを返します。"""
        if observation_days <= 0:
            raise ValueError("observation_daysは正数を指定してください")
        as_of_date = as_of.date() if isinstance(as_of, datetime) else as_of
        finalized_at = self._iso_datetime(as_of) if isinstance(as_of, datetime) else datetime.combine(as_of, datetime.min.time()).isoformat(timespec="seconds")
        finalized: list[dict] = []
        for event in self.load_open_events(execution_mode):
            occurred_date = datetime.fromisoformat(event["occurred_at"]).date()
            if self._business_days_elapsed(occurred_date, as_of_date) < observation_days:
                continue
            summary = self._summarize(event)
            with self._connect() as connection:
                cursor = connection.execute(
                    """
                    UPDATE filter_decision_events
                    SET status = 'finalized', finalized_at = ?, price_change_percent = ?,
                        hypothetical_pnl_before_cost = ?, outcome = ?
                    WHERE id = ? AND status = 'observing'
                    """,
                    (
                        finalized_at, summary["price_change_percent"],
                        summary["hypothetical_pnl_before_cost"], summary["outcome"], event["id"],
                    ),
                )
                if cursor.rowcount == 1:
                    finalized.append({**event, **summary, "status": "finalized", "finalized_at": finalized_at})
        return finalized

    def load_summaries(
        self,
        start: date | None = None,
        end: date | None = None,
        execution_mode: str | None = None,
        finalized_only: bool = False,
    ) -> list[dict]:
        clauses: list[str] = []
        parameters: list[object] = []
        if start is not None:
            clauses.append("substr(COALESCE(finalized_at, occurred_at), 1, 10) >= ?")
            parameters.append(start.isoformat())
        if end is not None:
            clauses.append("substr(COALESCE(finalized_at, occurred_at), 1, 10) <= ?")
            parameters.append(end.isoformat())
        if execution_mode is not None:
            clauses.append("execution_mode = ?")
            parameters.append(execution_mode)
        if finalized_only:
            clauses.append("status = 'finalized'")
        query = "SELECT * FROM filter_decision_events"
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY occurred_at, id"
        with self._connect() as connection:
            return [self._summary_from_row(row) for row in connection.execute(query, parameters)]

    def summarize_finalized_events(
        self, start: date, end: date, execution_mode: str | None = None
    ) -> dict:
        """期間内に確定したイベントを種別・結果分類ごとに集計します。"""
        summaries = self.load_summaries(start, end, execution_mode, finalized_only=True)
        by_type: dict[str, dict] = {}
        for item in summaries:
            summary = by_type.setdefault(item["event_type"], {"count": 0, "outcomes": {}})
            summary["count"] += 1
            outcome = item["outcome"]
            summary["outcomes"][outcome] = summary["outcomes"].get(outcome, 0) + 1
        return {
            "count": len(summaries),
            "by_event_type": by_type,
            "basis": "複数営業日基準(FILTER_DECISION_OBSERVATION_DAYS営業日経過で確定。期間は確定日)。count/by_event_typeはこの基準",
            "same_day": self._summarize_same_day(start, end, execution_mode),
        }

    def load_same_day_summaries(
        self, start: date, end: date, execution_mode: str | None = None
    ) -> list[dict]:
        """期間内に当日基準が確定したイベントを返します。"""
        query = (
            "SELECT * FROM filter_decision_events WHERE same_day_finalized_at IS NOT NULL "
            "AND substr(same_day_finalized_at, 1, 10) >= ? AND substr(same_day_finalized_at, 1, 10) <= ?"
        )
        parameters: list[object] = [start.isoformat(), end.isoformat()]
        if execution_mode is not None:
            query += " AND execution_mode = ?"
            parameters.append(execution_mode)
        query += " ORDER BY occurred_at, id"
        with self._connect() as connection:
            return [self._summary_from_row(row) for row in connection.execute(query, parameters)]

    def _summarize_same_day(self, start: date, end: date, execution_mode: str | None) -> dict:
        by_type: dict[str, dict] = {}
        data_quality: dict[str, int] = {}
        excluded_daily_replay = 0
        items = self.load_same_day_summaries(start, end, execution_mode)
        for item in items:
            if DAILY_REPLAY_FLAG in self._split_flags(item["same_day_data_quality"]):
                excluded_daily_replay += 1
                continue
            summary = by_type.setdefault(
                item["event_type"], {"count": 0, "outcomes": {}, "pnl_before_cost_total": 0.0}
            )
            summary["count"] += 1
            outcome = item["same_day_outcome"]
            summary["outcomes"][outcome] = summary["outcomes"].get(outcome, 0) + 1
            summary["pnl_before_cost_total"] += float(item["same_day_hypothetical_pnl_before_cost"])
            for flag in str(item["same_day_data_quality"] or "OK").split(","):
                name = flag.split("(")[0]
                data_quality[name] = data_quality.get(name, 0) + 1
        return {
            "basis": "当日基準(発生日の取引終了時に確定。期間は当日確定日)",
            "count": len(items) - excluded_daily_replay,
            "excluded_daily_replay_count": excluded_daily_replay,
            "by_event_type": by_type,
            "data_quality": data_quality,
        }

    @staticmethod
    def _row_to_dict(row: sqlite3.Row) -> dict:
        result = dict(row)
        result["inputs"] = json.loads(result.pop("input_json"))
        return result

    @classmethod
    def _summary_from_row(cls, row: sqlite3.Row) -> dict:
        event = cls._row_to_dict(row)
        multi_day = cls._summarize(event)
        return {
            **event,
            **multi_day,
            "evaluations": {
                "multi_day": {
                    "basis": "複数営業日基準",
                    "finalized": event["status"] == "finalized",
                    "last_price": event["last_price"],
                    **multi_day,
                },
                "same_day": cls._same_day_evaluation(event),
            },
        }

    @classmethod
    def _same_day_evaluation(cls, event: Mapping[str, object]) -> dict | None:
        if event.get("same_day_close_price") is None:
            return None
        finalized = event.get("same_day_finalized_at") is not None
        if finalized:
            result = {
                "price_change_percent": event["same_day_price_change_percent"],
                "hypothetical_pnl_before_cost": event["same_day_hypothetical_pnl_before_cost"],
                "outcome": event["same_day_outcome"],
            }
        else:
            result = cls._evaluate(
                str(event["event_type"]), float(event["reference_price"]),
                float(event["same_day_close_price"]), int(event["quantity"]),
            )
        return {
            "basis": "当日基準",
            "finalized": finalized,
            "close_price": event["same_day_close_price"],
            "high_price": event["same_day_high"],
            "low_price": event["same_day_low"],
            "last_observed_at": event["same_day_last_observed_at"],
            "observation_count": event["same_day_observation_count"],
            "data_quality": event["same_day_data_quality"],
            **result,
        }

    @classmethod
    def _summarize(cls, event: Mapping[str, object]) -> dict:
        return cls._evaluate(
            str(event["event_type"]), float(event["reference_price"]),
            float(event["last_price"]), int(event["quantity"]),
        )

    @staticmethod
    def _evaluate(event_type: str, reference_price: float, last_price: float, quantity: int) -> dict:
        if event_type == "ATR_STOP_EXIT":
            hypothetical_pnl = (reference_price - last_price) * quantity
            outcome = (
                "下落回避の可能性" if hypothetical_pnl > 0 else
                "早すぎる決済の可能性" if hypothetical_pnl < 0 else "売却後の値動きなし"
            )
        elif event_type == "ADX_TREND_RELIEF":
            hypothetical_pnl = (last_price - reference_price) * quantity
            outcome = (
                "緩和取引が有利だった可能性" if hypothetical_pnl > 0 else
                "緩和取引が不利だった可能性" if hypothetical_pnl < 0 else "値動きなし"
            )
        else:
            hypothetical_pnl = (last_price - reference_price) * quantity
            outcome = (
                "損失回避の可能性" if hypothetical_pnl < 0 else
                "利益取り逃しの可能性" if hypothetical_pnl > 0 else "値動きなし"
            )
        return {
            "price_change_percent": (last_price - reference_price) / reference_price * 100,
            "hypothetical_pnl_before_cost": hypothetical_pnl,
            "outcome": outcome,
        }