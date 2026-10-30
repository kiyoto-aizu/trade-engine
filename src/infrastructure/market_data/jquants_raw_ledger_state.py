"""Read-only adapter exposing the raw-store ledger state per dataset and date."""

from __future__ import annotations

from datetime import date

from src.infrastructure.market_data.jquants_raw_store import JQuantsRawStore


class RawStoreLedgerState:
    def __init__(self, raw_store: JQuantsRawStore) -> None:
        self._raw_store = raw_store

    def state(self, dataset: str, target_date: date) -> str | None:
        state = self._raw_store.get_state_for(
            kind=dataset, endpoint=f"equities/bars/{dataset}",
            conditions={"date": target_date.isoformat()},
        )
        return None if state is None else state.value
