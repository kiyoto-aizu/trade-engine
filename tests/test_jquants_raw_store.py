import json
import sqlite3
import threading
from datetime import datetime, timezone
import pytest

from src.infrastructure.market_data.jquants_raw_store import (
    JQuantsDatasetAlreadyRunningError,
    JQuantsDatasetState,
    JQuantsRawStore,
    JQuantsRawStoreError,
    JQuantsRawValidationError,
)
from src.infrastructure.market_data import jquants_raw_store as raw_store_module
from src.infrastructure.market_data.jquants_v2_client import (
    JQuantsPage,
    JQuantsPageMetadata,
    JQuantsResponseError,
)


ENDPOINT = "equities/bars/daily"
CONDITIONS = {"date": "2026-10-20"}
API_KEY = "fake-api-key-must-not-persist"
PAGE_KEY = "opaque-pagination-key-must-not-persist"


def _page(page_number, rows, *, next_key=None, raw_rows=None):
    payload = {"data": rows}
    if next_key is not None:
        payload["pagination_key"] = next_key
    raw_body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    return JQuantsPage(
        data=tuple(rows if raw_rows is None else raw_rows),
        raw_body=raw_body,
        metadata=JQuantsPageMetadata(
            endpoint=ENDPOINT,
            page_number=page_number,
            row_count=len(rows),
            status_code=200,
            has_next_page=next_key is not None,
        ),
    )


class FakeClient:
    def __init__(self, pages):
        self.pages = pages
        self.calls = []

    def iter_pages(self, endpoint, params=None):
        self.calls.append((endpoint, dict(params or {})))
        yield from self.pages


def _store(tmp_path, *, clock=None):
    return JQuantsRawStore(
        tmp_path / "raw",
        tmp_path / "ledger.sqlite3",
        clock=clock,
    )


def _capture(store, client, *, conditions=CONDITIONS):
    return store.capture(
        client,
        kind="daily",
        endpoint=ENDPOINT,
        conditions=conditions,
    )


def _read_pages(result):
    lines = (result.committed_path / "pages.jsonl").read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines]


def test_multiple_pages_are_sanitized_validated_committed_and_marked_complete(tmp_path):
    store = _store(tmp_path)
    client = FakeClient(
        [
            _page(1, [{"Code": "72030", "Close": 100}], next_key=PAGE_KEY),
            _page(2, [{"Code": "83060", "Close": 200}]),
        ]
    )

    result = _capture(store, client)

    assert result.state is JQuantsDatasetState.COMPLETE
    assert result.page_count == 2
    assert result.row_count == 2
    assert store.get_state(result.dataset_id) is JQuantsDatasetState.COMPLETE
    assert client.calls == [(ENDPOINT, CONDITIONS)]
    manifest = store.get_current_manifest(kind="daily", endpoint=ENDPOINT, conditions=CONDITIONS)
    assert manifest is not None
    assert manifest["page_count"] == 2
    assert manifest["row_count"] == 2
    assert manifest["content_sha256"] == result.content_sha256

    pages = _read_pages(result)
    assert [item["page_number"] for item in pages] == [1, 2]
    assert [len(item["response"]["data"]) for item in pages] == [1, 1]
    assert all(item["captured_at_utc"].endswith("Z") for item in pages)
    serialized_pages = json.dumps(pages)
    assert PAGE_KEY not in serialized_pages
    assert "pagination_key" not in serialized_pages
    assert API_KEY not in serialized_pages
    assert not list((tmp_path / "raw" / "staging").rglob("pages.jsonl"))

    with sqlite3.connect(tmp_path / "ledger.sqlite3") as connection:
        state, pages_count, rows_count = connection.execute(
            "SELECT state, page_count, row_count FROM datasets WHERE dataset_id = ?",
            (result.dataset_id,),
        ).fetchone()
        page_count, ledger_rows = connection.execute(
            """
            SELECT COUNT(*), SUM(row_count) FROM dataset_pages
            WHERE dataset_id = ?
            """,
            (result.dataset_id,),
        ).fetchone()
        ledger_text = json.dumps(
            connection.execute("SELECT * FROM datasets").fetchall()
            + connection.execute("SELECT * FROM dataset_pages").fetchall()
        )
    assert (state, pages_count, rows_count) == ("COMPLETE", 2, 2)
    assert (page_count, ledger_rows) == (2, 2)
    assert API_KEY not in ledger_text
    assert PAGE_KEY not in ledger_text


def test_empty_completed_data_is_committed_as_empty_unknown(tmp_path):
    store = _store(tmp_path)
    client = FakeClient([_page(1, [])])

    result = _capture(store, client)

    assert result.state is JQuantsDatasetState.EMPTY_UNKNOWN
    assert result.page_count == 1
    assert result.row_count == 0
    assert store.get_state(result.dataset_id) is JQuantsDatasetState.EMPTY_UNKNOWN
    manifest = store.get_current_manifest(kind="daily", endpoint=ENDPOINT, conditions=CONDITIONS)
    assert manifest is not None
    assert manifest["state"] == "EMPTY_UNKNOWN"
    assert _read_pages(result)[0]["response"] == {"data": []}


def test_later_page_failure_is_partial_and_never_published(tmp_path):
    store = _store(tmp_path)

    def failed_pages():
        yield _page(1, [{"Code": "72030"}], next_key=PAGE_KEY)
        raise JQuantsResponseError("safe client error")

    with pytest.raises(JQuantsResponseError):
        _capture(store, FakeClient(failed_pages()))

    dataset_id = store._dataset_id("daily", ENDPOINT, CONDITIONS)
    assert store.get_state(dataset_id) is JQuantsDatasetState.PARTIAL
    incomplete = store.list_incomplete_datasets()
    assert len(incomplete) == 1
    assert (incomplete[0].page_count, incomplete[0].row_count) == (1, 1)
    assert store.get_current_manifest(
        kind="daily", endpoint=ENDPOINT, conditions=CONDITIONS
    ) is None
    assert list((tmp_path / "raw" / "staging" / dataset_id).rglob("pages.jsonl"))


def test_partial_run_can_be_retried_and_completed(tmp_path):
    store = _store(tmp_path)

    def first_attempt():
        yield _page(1, [{"Code": "72030"}])
        raise JQuantsResponseError("safe")

    with pytest.raises(JQuantsResponseError):
        _capture(store, FakeClient(first_attempt()))
    dataset_id = store._dataset_id("daily", ENDPOINT, CONDITIONS)
    assert store.get_state(dataset_id) is JQuantsDatasetState.PARTIAL

    result = _capture(store, FakeClient([_page(1, [{"Code": "72030"}])]))

    assert result.state is JQuantsDatasetState.COMPLETE
    assert store.get_state(dataset_id) is JQuantsDatasetState.COMPLETE
    assert store.get_current_manifest(
        kind="daily", endpoint=ENDPOINT, conditions=CONDITIONS
    ) is not None


def test_simultaneous_capture_is_rejected_and_running_data_is_not_visible(tmp_path):
    store = _store(tmp_path)
    page_saved = threading.Event()
    continue_capture = threading.Event()
    results = []
    errors = []

    def blocking_pages():
        yield _page(1, [{"Code": "72030"}])
        page_saved.set()
        if not continue_capture.wait(timeout=10):
            raise TimeoutError("test synchronization timed out")

    def run_first():
        try:
            results.append(_capture(store, FakeClient(blocking_pages())))
        except BaseException as error:
            errors.append(error)

    thread = threading.Thread(target=run_first)
    thread.start()
    try:
        assert page_saved.wait(timeout=10)
        dataset_id = store._dataset_id("daily", ENDPOINT, CONDITIONS)
        assert store.get_state(dataset_id) is JQuantsDatasetState.RUNNING
        assert store.list_incomplete_datasets()[0].page_count == 1
        assert store.get_current_manifest(
            kind="daily", endpoint=ENDPOINT, conditions=CONDITIONS
        ) is None
        with pytest.raises(JQuantsDatasetAlreadyRunningError):
            _capture(store, FakeClient([_page(1, [{"Code": "72030"}])]))
    finally:
        continue_capture.set()
        thread.join(timeout=10)

    assert not thread.is_alive()
    assert errors == []
    assert len(results) == 1
    assert store.get_state(results[0].dataset_id) is JQuantsDatasetState.COMPLETE


def test_repeating_identical_capture_reuses_generation_without_duplicate_data(tmp_path):
    now = datetime(2026, 10, 21, tzinfo=timezone.utc)
    store = _store(tmp_path, clock=lambda: now)
    rows = [{"Code": "72030", "Close": 100}]
    first = _capture(store, FakeClient([_page(1, rows)]))
    second = _capture(store, FakeClient([_page(1, rows)]))

    assert first.dataset_id == second.dataset_id
    assert first.content_sha256 == second.content_sha256
    assert first.committed_path == second.committed_path
    assert len(list((tmp_path / "raw" / "committed" / first.dataset_id / "generations").iterdir())) == 1
    assert len(_read_pages(second)[0]["response"]["data"]) == 1


def test_invalid_page_data_fails_validation_without_complete_state(tmp_path):
    store = _store(tmp_path)
    invalid = _page(1, [{"Code": "72030"}], raw_rows=["not-an-object"])

    with pytest.raises(JQuantsRawValidationError):
        _capture(store, FakeClient([invalid]))

    dataset_id = store._dataset_id("daily", ENDPOINT, CONDITIONS)
    assert store.get_state(dataset_id) is JQuantsDatasetState.FAILED
    assert store.get_current_manifest(
        kind="daily", endpoint=ENDPOINT, conditions=CONDITIONS
    ) is None


def test_generic_capture_failure_is_sanitized_in_exception_ledger_and_logs(tmp_path, caplog):
    store = _store(tmp_path)

    def failed_pages():
        yield _page(1, [{"Code": "72030"}])
        raise RuntimeError(f"{API_KEY} {PAGE_KEY} raw response body")

    with pytest.raises(JQuantsRawStoreError) as error:
        _capture(store, FakeClient(failed_pages()))

    assert API_KEY not in str(error.value)
    assert PAGE_KEY not in str(error.value)
    assert API_KEY not in caplog.text
    assert PAGE_KEY not in caplog.text
    dataset_id = store._dataset_id("daily", ENDPOINT, CONDITIONS)
    with sqlite3.connect(tmp_path / "ledger.sqlite3") as connection:
        state, category = connection.execute(
            "SELECT state, error_category FROM datasets WHERE dataset_id = ?",
            (dataset_id,),
        ).fetchone()
    assert state == "PARTIAL"
    assert category == "UNEXPECTED_ERROR"


def test_unsafe_request_conditions_are_rejected_before_dataset_creation(tmp_path):
    store = _store(tmp_path)

    with pytest.raises(JQuantsRawStoreError):
        store.capture(
            FakeClient([]),
            kind="daily",
            endpoint=ENDPOINT,
            conditions={"pagination_key": PAGE_KEY},
        )

    with pytest.raises(JQuantsRawStoreError):
        store.capture(
            FakeClient([]),
            kind="daily",
            endpoint="../daily",
            conditions=CONDITIONS,
        )


def test_interrupted_running_dataset_is_listed_and_can_be_reclaimed(tmp_path):
    store = _store(tmp_path)
    first = _capture(store, FakeClient([_page(1, [{"Code": "72030"}])]))

    with sqlite3.connect(tmp_path / "ledger.sqlite3") as connection:
        connection.execute(
            "UPDATE datasets SET state = 'RUNNING' WHERE dataset_id = ?",
            (first.dataset_id,),
        )
    incomplete = store.list_incomplete_datasets()

    assert len(incomplete) == 1
    assert incomplete[0].dataset_id == first.dataset_id
    assert incomplete[0].state is JQuantsDatasetState.RUNNING
    resumed = _capture(store, FakeClient([_page(1, [{"Code": "72030"}])]))
    assert resumed.state is JQuantsDatasetState.COMPLETE
    assert store.list_incomplete_datasets() == []


def test_pointer_failure_keeps_staging_and_is_recoverable(tmp_path, monkeypatch):
    store = _store(tmp_path)
    real_replace = raw_store_module.os.replace

    def fail_pointer_replace(source, destination):
        if str(destination).endswith("\\CURRENT"):
            raise OSError(f"{API_KEY} cannot publish pointer")
        return real_replace(source, destination)

    monkeypatch.setattr(raw_store_module.os, "replace", fail_pointer_replace)
    with pytest.raises(JQuantsRawStoreError) as error:
        _capture(store, FakeClient([_page(1, [{"Code": "72030"}])]))

    assert API_KEY not in str(error.value)
    dataset_id = store._dataset_id("daily", ENDPOINT, CONDITIONS)
    assert store.get_state(dataset_id) is JQuantsDatasetState.PARTIAL
    assert store.get_current_manifest(
        kind="daily", endpoint=ENDPOINT, conditions=CONDITIONS
    ) is None
    assert list((tmp_path / "raw" / "staging" / dataset_id).rglob("pages.jsonl"))

    monkeypatch.setattr(raw_store_module.os, "replace", real_replace)
    result = _capture(store, FakeClient([_page(1, [{"Code": "72030"}])]))
    assert result.state is JQuantsDatasetState.COMPLETE
    assert store.get_current_manifest(
        kind="daily", endpoint=ENDPOINT, conditions=CONDITIONS
    ) is not None
