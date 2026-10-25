"""J-Quants canonical の読み取り専用Repository。

検証済みCURRENTの読み取りと、取得状態（台帳・ATTEMPT.json）の観測だけを行う。
欠損・失敗・版の意味づけは domain/jquants_consumer_contract.py が担う。
"""

from __future__ import annotations

from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq

from src.domain.jquants_consumer_contract import (
    VERIFY_BROKEN,
    VERIFY_STALE_SCHEMA,
    DayObservation,
)
from src.infrastructure.market_data.jquants_canonical_store import (
    JQuantsCanonicalError,
    JQuantsCanonicalStore,
    JQuantsStaleSchemaError,
    canonical_schema,
    strict_date,
)
from src.infrastructure.market_data.jquants_raw_store import JQuantsRawStore


class JQuantsCanonicalReader:
    def __init__(
        self, canonical_store: JQuantsCanonicalStore, raw_store: JQuantsRawStore | None = None
    ) -> None:
        self._store = canonical_store
        self._raw_store = raw_store

    @staticmethod
    def column_names(dataset: str) -> tuple[str, ...]:
        return tuple(canonical_schema(dataset).names)

    def observe(self, dataset: str, target_date: str) -> DayObservation:
        day = strict_date(target_date)
        attempt_state: str | None = None
        ledger_state: str | None = None
        try:
            manifest = self._store.current_manifest(dataset, target_date)
        except JQuantsStaleSchemaError as error:
            return DayObservation(
                day, schema_version=error.schema_version, verify_error=VERIFY_STALE_SCHEMA
            )
        except (JQuantsCanonicalError, OSError, ValueError, KeyError, TypeError, pa.ArrowException):
            return DayObservation(day, verify_error=VERIFY_BROKEN)
        if manifest is not None:
            return DayObservation(
                day,
                generation_id=manifest["generation_id"],
                schema_version=manifest["schema_version"],
                transformer_version=manifest["transformer_version"],
                raw_generation_id=manifest["raw_generation_id"],
            )
        try:
            attempt = self._store.read_attempt(dataset, target_date)
        except (OSError, ValueError):
            return DayObservation(day, verify_error=VERIFY_BROKEN)
        if isinstance(attempt, dict):
            attempt_state = attempt.get("state")
        if self._raw_store is not None:
            state = self._raw_store.get_state_for(
                kind=dataset, endpoint=f"equities/bars/{dataset}",
                conditions={"date": target_date},
            )
            ledger_state = None if state is None else state.value
        return DayObservation(day, ledger_state=ledger_state, attempt_state=attempt_state)

    def read_columns(
        self, dataset: str, target_date: str, generation_id: str, columns: tuple[str, ...]
    ) -> pa.Table:
        """検証済みCURRENTが指定世代のときだけ、指定列を読む。"""
        unknown = [name for name in columns if name not in self.column_names(dataset)]
        if unknown:
            raise JQuantsCanonicalError("Unknown canonical columns: " + ", ".join(unknown))
        manifest: Any = self._store.current_manifest(dataset, target_date)
        if manifest is None or manifest["generation_id"] != generation_id:
            raise JQuantsCanonicalError("CURRENT generation changed during the read")
        path = self._store.parquet_path(dataset, target_date, generation_id)
        return pq.read_table(path, columns=list(columns))
