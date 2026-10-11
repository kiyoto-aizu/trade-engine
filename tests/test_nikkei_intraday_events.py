import sqlite3
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.api import request_handler
from src.application.trading_usecase import NIKKEI_INTRADAY_EVENT_TYPES, TradingUseCase
from src.infrastructure.market_data.yahoo_index_client import JST, YahooIndexClient
from src.infrastructure.persistence.filter_decision_repository import FilterDecisionRepository


class Getter:
    def __init__(self, result=(-1.25, datetime(2026, 10, 9, 10, 4, tzinfo=JST)), error=None):
        self.result, self.error, self.calls = result, error, []

    def get_intraday_snapshot(self, symbol, as_of, timeout=30):
        self.calls.append((symbol, as_of, timeout))
        if self.error:
            raise self.error
        return self.result


def _use_case(tmp_path, client):
    repository = FilterDecisionRepository(tmp_path / "events.sqlite3")
    use_case = TradingUseCase(
        token="t",
        order_history_path=Path(tmp_path / "orders.json"),
        market_regime_usecase=SimpleNamespace(market_data_client=client),
        filter_decision_repository=repository,
    )
    return use_case, repository


def _rows(tmp_path):
    connection = sqlite3.connect(tmp_path / "events.sqlite3")
    connection.row_factory = sqlite3.Row
    return connection.execute("SELECT * FROM filter_decision_events ORDER BY id").fetchall()


def test_existing_database_gets_nullable_columns_without_data_loss(tmp_path):
    path = tmp_path / "events.sqlite3"
    FilterDecisionRepository(path)
    connection = sqlite3.connect(path)
    connection.execute("ALTER TABLE filter_decision_events DROP COLUMN nikkei_intraday_change_percent")
    connection.execute("ALTER TABLE filter_decision_events DROP COLUMN nikkei_intraday_as_of")
    connection.execute(
        "INSERT INTO filter_decision_events (event_type, symbol, execution_mode, occurred_at, reference_price, "
        "quantity, lowest_price, highest_price, last_price, last_observed_at, observation_count, created_at) "
        "VALUES ('ATR_DANGER_SKIP', '1111', 'paper', '2026-10-08T10:00:00', 100, 1, 100, 100, 100, "
        "'2026-10-08T10:00:00', 1, '2026-10-08T10:00:00')"
    )
    connection.commit()
    connection.close()

    FilterDecisionRepository(path)
    FilterDecisionRepository(path)

    connection = sqlite3.connect(path)
    row = connection.execute(
        "SELECT symbol, nikkei_intraday_change_percent, nikkei_intraday_as_of FROM filter_decision_events"
    ).fetchone()
    assert row == ("1111", None, None)


@pytest.mark.parametrize("event_type", sorted(NIKKEI_INTRADAY_EVENT_TYPES))
def test_target_events_store_intraday_values(tmp_path, event_type):
    getter = Getter()
    use_case, _ = _use_case(tmp_path, getter)

    use_case._record_filter_decision_safely(event_type, "1111", datetime(2026, 10, 9, 10, 5), 100.0, 1, {})

    row = _rows(tmp_path)[0]
    assert row["nikkei_intraday_change_percent"] == -1.25
    assert row["nikkei_intraday_as_of"] == "2026-10-09T10:04:00"
    assert getter.calls[0][0] == "^N225"
    assert getter.calls[0][2] == 5


def test_non_target_event_does_not_fetch(tmp_path):
    getter = Getter()
    use_case, _ = _use_case(tmp_path, getter)

    use_case._record_filter_decision_safely("ATR_STOP_EXIT", "1111", datetime(2026, 10, 9, 10, 5), 100.0, 1, {})

    row = _rows(tmp_path)[0]
    assert row["nikkei_intraday_change_percent"] is None
    assert getter.calls == []


def test_fetch_is_cached_for_60_seconds(tmp_path):
    getter = Getter()
    use_case, _ = _use_case(tmp_path, getter)
    for minute, second, symbol in ((5, 0, "1111"), (5, 30, "2222"), (6, 5, "3333")):
        use_case._record_filter_decision_safely(
            "ATR_DANGER_SKIP", symbol, datetime(2026, 10, 9, 10, minute, second), 100.0, 1, {}
        )

    assert len(getter.calls) == 2
    assert len(_rows(tmp_path)) == 3


@pytest.mark.parametrize("client", [Getter(error=RuntimeError("timeout")), Getter(result=None), object()])
def test_failures_are_recorded_as_null_and_do_not_block_event(tmp_path, client):
    use_case, _ = _use_case(tmp_path, client)

    event_id = use_case._record_filter_decision_safely(
        "MARKET_REGIME_DANGER_SKIP", "1111", datetime(2026, 10, 9, 10, 5), 100.0, 1, {}
    )

    assert event_id is not None
    row = _rows(tmp_path)[0]
    assert row["nikkei_intraday_change_percent"] is None
    assert row["nikkei_intraday_as_of"] is None


def test_failure_is_cached_so_outage_does_not_refetch_every_call(tmp_path):
    getter = Getter(error=RuntimeError("down"))
    use_case, _ = _use_case(tmp_path, getter)
    for symbol in ("1111", "2222"):
        use_case._record_filter_decision_safely(
            "ATR_DANGER_SKIP", symbol, datetime(2026, 10, 9, 10, 5, 10), 100.0, 1, {}
        )

    assert len(getter.calls) == 1


def test_snapshot_returns_percent_and_last_bar_time(monkeypatch):
    base = int(datetime(2026, 10, 9, 9, 0, tzinfo=JST).timestamp())
    previous = int(datetime(2026, 10, 8, 15, 0, tzinfo=JST).timestamp())
    response = {"chart": {"result": [{
        "timestamp": [previous, base, base + 60, base + 120],
        "indicators": {"quote": [{"close": [40000.0, 39900.0, 39800.0, 39700.0]}]},
        "meta": {},
    }]}}
    monkeypatch.setattr(request_handler, "send_get", lambda *a, **k: response)
    client = YahooIndexClient()

    as_of = datetime(2026, 10, 9, 9, 1, 30)
    percent, bar_time = client.get_intraday_snapshot("^N225", as_of)

    assert percent == pytest.approx((39800.0 / 40000.0 - 1) * 100)
    assert bar_time == datetime(2026, 10, 9, 9, 1, tzinfo=JST)
    assert client.get_intraday_change_percent("^N225", as_of) == pytest.approx(percent)


def test_snapshot_returns_none_without_response(monkeypatch):
    monkeypatch.setattr(request_handler, "send_get", lambda *a, **k: None)

    assert YahooIndexClient().get_intraday_snapshot("^N225", datetime(2026, 10, 9, 9, 1)) is None
