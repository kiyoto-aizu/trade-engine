import json
import os
import sqlite3
import subprocess
import sys
import threading
import time
import builtins
from datetime import datetime, timedelta, timezone
from pathlib import Path
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
    if sys.platform != "win32":
        pytest.skip("raw-store integration tests require Windows msvcrt locking")
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
    published = _capture(store, FakeClient([_page(1, [{"Code": "70070"}])]))
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
        )["generation_id"] == published.content_sha256
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
    second_store = _store(tmp_path, clock=lambda: now + timedelta(days=1))
    second = _capture(second_store, FakeClient([_page(1, rows)]))

    assert first.dataset_id == second.dataset_id
    assert first.content_sha256 == second.content_sha256
    assert first.committed_path == second.committed_path
    assert len(list((tmp_path / "raw" / "committed" / first.dataset_id / "generations").iterdir())) == 1
    assert len(_read_pages(second)[0]["response"]["data"]) == 1


def test_content_hash_tracks_page_order_and_page_boundaries(tmp_path):
    store = _store(tmp_path)
    first = _capture(
        store,
        FakeClient(
            [
                _page(1, [{"Code": "72030"}], next_key=PAGE_KEY),
                _page(2, [{"Code": "83060"}]),
            ]
        ),
    )
    reordered = _capture(
        store,
        FakeClient(
            [
                _page(1, [{"Code": "83060"}], next_key=PAGE_KEY),
                _page(2, [{"Code": "72030"}]),
            ]
        ),
    )
    repartitioned = _capture(
        store,
        FakeClient([_page(1, [{"Code": "72030"}, {"Code": "83060"}])]),
    )

    assert first.content_sha256 != reordered.content_sha256
    assert first.content_sha256 != repartitioned.content_sha256


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
        if Path(destination).name == "CURRENT":
            raise OSError(f"{API_KEY} cannot publish pointer")
        return real_replace(source, destination)

    monkeypatch.setattr(raw_store_module.os, "replace", fail_pointer_replace)
    with pytest.raises(JQuantsRawStoreError) as error:
        _capture(store, FakeClient([_page(1, [{"Code": "72030"}])]))

    assert API_KEY not in str(error.value)
    dataset_id = store._dataset_id("daily", ENDPOINT, CONDITIONS)
    assert store.get_state(dataset_id) is JQuantsDatasetState.RUNNING
    assert store.get_current_manifest(
        kind="daily", endpoint=ENDPOINT, conditions=CONDITIONS
    ) is None
    assert list((tmp_path / "raw" / "staging" / dataset_id).rglob("pages.jsonl"))

    monkeypatch.setattr(raw_store_module.os, "replace", real_replace)
    store.recover_dataset(dataset_id)
    assert store.get_state(dataset_id) is JQuantsDatasetState.COMPLETE
    assert store.get_current_manifest(
        kind="daily", endpoint=ENDPOINT, conditions=CONDITIONS
    ) is not None


@pytest.mark.parametrize("saved_page_count", [0, 1, 2])
def test_reacquisition_preserves_published_generation_during_and_after_failure(
    tmp_path, saved_page_count
):
    store = _store(tmp_path)
    original = _capture(store, FakeClient([_page(1, [{"Code": "72030"}])]))
    page_saved = threading.Event()
    continue_capture = threading.Event()
    errors = []

    def failed_pages():
        for page_number in range(1, saved_page_count + 1):
            yield _page(
                page_number,
                [{"Code": str(83060 + page_number)}],
                next_key=PAGE_KEY,
            )
        page_saved.set()
        assert continue_capture.wait(timeout=10)
        raise JQuantsResponseError("safe client error")

    def run_capture():
        try:
            _capture(store, FakeClient(failed_pages()))
        except BaseException as error:
            errors.append(error)

    thread = threading.Thread(target=run_capture)
    thread.start()
    try:
        assert page_saved.wait(timeout=10)
        manifest = store.get_current_manifest(
            kind="daily", endpoint=ENDPOINT, conditions=CONDITIONS
        )
        assert manifest is not None
        assert manifest["generation_id"] == original.content_sha256
    finally:
        continue_capture.set()
        thread.join(timeout=10)

    assert not thread.is_alive()
    assert len(errors) == 1
    manifest = store.get_current_manifest(
        kind="daily", endpoint=ENDPOINT, conditions=CONDITIONS
    )
    assert manifest is not None
    assert manifest["generation_id"] == original.content_sha256


def test_empty_reacquisition_holds_nonempty_publication_and_records_observation(tmp_path):
    store = _store(tmp_path)
    original = _capture(store, FakeClient([_page(1, [{"Code": "72030"}])]))
    pointer_path = original.committed_path.parent.parent / "CURRENT"
    original_pointer = pointer_path.read_bytes()

    result = _capture(store, FakeClient([_page(1, [])]))

    assert result.row_count == 0
    assert result.state.value == "EMPTY_HELD"
    assert pointer_path.read_bytes() == original_pointer
    manifest = store.get_current_manifest(
        kind="daily", endpoint=ENDPOINT, conditions=CONDITIONS
    )
    assert manifest is not None
    assert manifest["generation_id"] == original.content_sha256
    with sqlite3.connect(tmp_path / "ledger.sqlite3") as connection:
        state, page_count, row_count = connection.execute(
            "SELECT state, page_count, row_count FROM datasets WHERE dataset_id = ?",
            (original.dataset_id,),
        ).fetchone()
        attempts = connection.execute(
            "SELECT state, row_count FROM dataset_attempts WHERE dataset_id = ? "
            "ORDER BY started_at_utc",
            (original.dataset_id,),
        ).fetchall()
    assert (state, page_count, row_count) == ("EMPTY_HELD", 1, 0)
    assert attempts[-1] == ("EMPTY_HELD", 0)


def test_staging_cleanup_failure_does_not_undo_publication(tmp_path, monkeypatch):
    store = _store(tmp_path)
    real_rmtree = raw_store_module.shutil.rmtree

    def fail_staging_cleanup(path, *args, **kwargs):
        if Path(path).parent.name == store._dataset_id("daily", ENDPOINT, CONDITIONS):
            raise OSError("synthetic cleanup failure")
        return real_rmtree(path, *args, **kwargs)

    monkeypatch.setattr(raw_store_module.shutil, "rmtree", fail_staging_cleanup)

    result = _capture(store, FakeClient([_page(1, [{"Code": "72030"}])]))

    assert result.state is JQuantsDatasetState.COMPLETE
    assert result.warning_category == "STAGING_CLEANUP_FAILED"
    assert store.get_state(result.dataset_id) is JQuantsDatasetState.COMPLETE
    assert store.get_current_manifest(
        kind="daily", endpoint=ENDPOINT, conditions=CONDITIONS
    ) is not None


@pytest.mark.parametrize("stop_point, expected_exit", [("before_pointer", 71), ("after_pointer", 72)])
def test_process_stop_during_publication_is_recovered_idempotently(
    tmp_path, stop_point, expected_exit
):
    store = _store(tmp_path)
    original = _capture(store, FakeClient([_page(1, [{"Code": "72030"}])]))
    dataset_id = original.dataset_id
    script = r"""
import os
import sys
from pathlib import Path
from src.infrastructure.market_data.jquants_raw_store import JQuantsRawStore
from src.infrastructure.market_data.jquants_v2_client import JQuantsPage, JQuantsPageMetadata

class Client:
    def iter_pages(self, endpoint, params=None):
        rows = [{"Code": "83060", "Close": 200}]
        body = b'{"data":[{"Code":"83060","Close":200}]}'
        yield JQuantsPage(
            data=tuple(rows),
            raw_body=body,
            metadata=JQuantsPageMetadata(endpoint, 1, 1, 200, False),
        )

store = JQuantsRawStore(Path(sys.argv[1]), Path(sys.argv[2]))
if sys.argv[3] == "before_pointer":
    store._publish_pointer = lambda *args: os._exit(71)
else:
    store._finish_dataset = lambda *args: os._exit(72)
store.capture(Client(), kind="daily", endpoint="equities/bars/daily",
              conditions={"date": "2026-10-20"})
"""
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            script,
            str(tmp_path / "raw"),
            str(tmp_path / "ledger.sqlite3"),
            stop_point,
        ],
        cwd=Path(__file__).resolve().parents[1],
        env={**os.environ, "ALLOW_MISSING_ENV": "1"},
        check=False,
        timeout=20,
    )

    assert completed.returncode == expected_exit
    old_manifest = store.get_current_manifest(
        kind="daily", endpoint=ENDPOINT, conditions=CONDITIONS
    )
    if stop_point == "after_pointer":
        assert old_manifest is not None
        assert old_manifest["generation_id"] == original.content_sha256

    store.recover_dataset(dataset_id)
    recovered = store.get_current_manifest(
        kind="daily", endpoint=ENDPOINT, conditions=CONDITIONS
    )
    assert recovered is not None
    assert recovered["row_count"] == 1
    assert store.get_state(dataset_id) is JQuantsDatasetState.COMPLETE
    store.recover_dataset(dataset_id)
    assert store.get_current_manifest(
        kind="daily", endpoint=ENDPOINT, conditions=CONDITIONS
    ) == recovered


def test_corrupt_current_generation_is_not_returned(tmp_path):
    store = _store(tmp_path)
    result = _capture(store, FakeClient([_page(1, [{"Code": "72030"}])]))
    pages_path = result.committed_path / "pages.jsonl"
    pages_path.write_text("corrupted\n", encoding="utf-8")

    with pytest.raises(JQuantsRawStoreError, match="generation|page|hash"):
        store.get_current_manifest(
            kind="daily", endpoint=ENDPOINT, conditions=CONDITIONS
        )


def test_corrupt_current_pointer_fails_read_and_recovery_restores_verified_generation(
    tmp_path,
):
    store = _store(tmp_path)
    result = _capture(store, FakeClient([_page(1, [{"Code": "72030"}])]))
    pointer_path = result.committed_path.parent.parent / "CURRENT"
    pointer_path.write_text("not-a-generation\n", encoding="ascii")

    with pytest.raises(JQuantsRawStoreError, match="pointer is invalid"):
        store.get_current_manifest(
            kind="daily", endpoint=ENDPOINT, conditions=CONDITIONS
        )

    store.recover_dataset(result.dataset_id)

    manifest = store.get_current_manifest(
        kind="daily", endpoint=ENDPOINT, conditions=CONDITIONS
    )
    assert manifest is not None
    assert manifest["generation_id"] == result.content_sha256


def test_missing_confirmed_generation_stops_without_selecting_another(tmp_path):
    store = _store(tmp_path)
    first = _capture(store, FakeClient([_page(1, [{"Code": "72030"}])]))
    second = _capture(store, FakeClient([_page(1, [{"Code": "83060"}])]))
    shutil = raw_store_module.shutil
    shutil.rmtree(first.committed_path)
    shutil.rmtree(second.committed_path)

    with pytest.raises(JQuantsRawStoreError, match="generation is unreadable"):
        store.get_current_manifest(
            kind="daily", endpoint=ENDPOINT, conditions=CONDITIONS
        )
    with pytest.raises(JQuantsRawStoreError, match="generation is unreadable"):
        store.recover_dataset(second.dataset_id)


def test_unsupported_os_fails_before_creating_files(tmp_path, monkeypatch):
    monkeypatch.setattr(raw_store_module.sys, "platform", "linux")
    raw_directory = tmp_path / "raw"
    ledger_file = tmp_path / "ledger.sqlite3"

    with pytest.raises(JQuantsRawStoreError, match="requires Windows"):
        JQuantsRawStore(raw_directory, ledger_file)

    assert not raw_directory.exists()
    assert not ledger_file.exists()


def test_one_ledger_cannot_be_reused_for_a_different_raw_root(tmp_path):
    first_store = _store(tmp_path)

    with pytest.raises(JQuantsRawStoreError, match="different raw directory"):
        JQuantsRawStore(tmp_path / "raw-b", first_store.ledger_database_file)


def test_existing_ledger_migrates_verified_current_generation(tmp_path):
    source_store = _store(tmp_path)
    result = _capture(source_store, FakeClient([_page(1, [{"Code": "72030"}])]))
    legacy_ledger = tmp_path / "legacy.sqlite3"
    with sqlite3.connect(tmp_path / "ledger.sqlite3") as source:
        row = source.execute(
            """
            SELECT dataset_id, kind, endpoint, conditions_json, run_id, started_at_utc,
                   updated_at_utc, completed_at_utc, page_count, row_count,
                   content_sha256, error_category
            FROM datasets WHERE dataset_id = ?
            """,
            (result.dataset_id,),
        ).fetchone()
        pages = source.execute(
            """
            SELECT dataset_id, run_id, page_number, row_count, status_code,
                   content_sha256, captured_at_utc
            FROM dataset_pages WHERE dataset_id = ?
            """,
            (result.dataset_id,),
        ).fetchall()
    with sqlite3.connect(legacy_ledger) as legacy:
        legacy.executescript(
            """
            CREATE TABLE datasets (
                dataset_id TEXT PRIMARY KEY, kind TEXT NOT NULL, endpoint TEXT NOT NULL,
                conditions_json TEXT NOT NULL, state TEXT NOT NULL, run_id TEXT,
                started_at_utc TEXT, updated_at_utc TEXT NOT NULL,
                completed_at_utc TEXT, page_count INTEGER NOT NULL DEFAULT 0,
                row_count INTEGER NOT NULL DEFAULT 0, content_sha256 TEXT,
                error_category TEXT
            );
            CREATE TABLE dataset_pages (
                dataset_id TEXT NOT NULL, run_id TEXT NOT NULL, page_number INTEGER NOT NULL,
                row_count INTEGER NOT NULL, status_code INTEGER NOT NULL,
                content_sha256 TEXT NOT NULL, captured_at_utc TEXT NOT NULL,
                PRIMARY KEY (dataset_id, run_id, page_number)
            );
            """
        )
        legacy.execute(
            """
            INSERT INTO datasets VALUES (
                ?, ?, ?, ?, 'RUNNING', ?, ?, ?, NULL, ?, ?, NULL, NULL
            )
            """,
            row[:4] + row[4:7] + row[8:10],
        )
        legacy.executemany("INSERT INTO dataset_pages VALUES (?, ?, ?, ?, ?, ?, ?)", pages)

    migrated = JQuantsRawStore(tmp_path / "raw", legacy_ledger)

    manifest = migrated.get_current_manifest(
        kind="daily", endpoint=ENDPOINT, conditions=CONDITIONS
    )
    assert manifest is not None
    assert manifest["generation_id"] == result.content_sha256
    assert migrated.get_state(result.dataset_id) is JQuantsDatasetState.RUNNING


def test_lock_failure_closes_handle_and_releases_same_process_guard(
    tmp_path, monkeypatch
):
    import msvcrt

    store = _store(tmp_path)
    dataset_id = store._dataset_id("daily", ENDPOINT, CONDITIONS)
    opened_handles = []
    real_open = Path.open
    real_locking = msvcrt.locking

    def tracking_open(path, *args, **kwargs):
        handle = real_open(path, *args, **kwargs)
        if Path(path).name == f"{dataset_id}.lock":
            opened_handles.append(handle)
        return handle

    def fail_locking(*args):
        raise OSError("synthetic lock failure")

    monkeypatch.setattr(Path, "open", tracking_open)
    monkeypatch.setattr(msvcrt, "locking", fail_locking)
    with pytest.raises(JQuantsDatasetAlreadyRunningError):
        store._acquire_dataset_lock(dataset_id)
    assert len(opened_handles) == 1
    assert opened_handles[0].closed

    monkeypatch.setattr(msvcrt, "locking", real_locking)
    handle = store._acquire_dataset_lock(dataset_id)
    store._release_dataset_lock(handle)


def test_lock_module_import_failure_has_no_file_side_effects(tmp_path, monkeypatch):
    store = _store(tmp_path)
    dataset_id = store._dataset_id("daily", ENDPOINT, CONDITIONS)
    real_import = builtins.__import__

    def fail_msvcrt_import(name, *args, **kwargs):
        if name == "msvcrt":
            raise ImportError("synthetic import failure")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fail_msvcrt_import)
    with pytest.raises(JQuantsRawStoreError, match="locking support is unavailable"):
        store._acquire_dataset_lock(dataset_id)

    assert not (store.raw_directory / "locks").exists()
    monkeypatch.setattr(builtins, "__import__", real_import)
    handle = store._acquire_dataset_lock(dataset_id)
    monkeypatch.setattr(builtins, "__import__", fail_msvcrt_import)
    with pytest.raises(JQuantsRawStoreError, match="became unavailable"):
        store._release_dataset_lock(handle)
    assert handle.file.closed
    monkeypatch.setattr(builtins, "__import__", real_import)
    handle = store._acquire_dataset_lock(dataset_id)
    store._release_dataset_lock(handle)


def test_another_process_holding_dataset_lock_prevents_capture(tmp_path):
    store = _store(tmp_path)
    dataset_id = store._dataset_id("daily", ENDPOINT, CONDITIONS)
    ready_file = tmp_path / "lock-ready"
    script = r"""
import sys
import time
from pathlib import Path
from src.infrastructure.market_data.jquants_raw_store import JQuantsRawStore

store = JQuantsRawStore(Path(sys.argv[1]), Path(sys.argv[2]))
handle = store._acquire_dataset_lock(sys.argv[3])
Path(sys.argv[4]).write_text("ready", encoding="ascii")
time.sleep(2)
store._release_dataset_lock(handle)
"""
    process = subprocess.Popen(
        [
            sys.executable,
            "-c",
            script,
            str(store.raw_directory),
            str(store.ledger_database_file),
            dataset_id,
            str(ready_file),
        ],
        cwd=Path(__file__).resolve().parents[1],
        env={**os.environ, "ALLOW_MISSING_ENV": "1"},
    )
    try:
        deadline = time.monotonic() + 10
        while not ready_file.exists() and time.monotonic() < deadline:
            if process.poll() is not None:
                pytest.fail("lock-holder process exited before acquiring its lock")
            time.sleep(0.05)
        assert ready_file.exists()
        with pytest.raises(JQuantsDatasetAlreadyRunningError):
            _capture(store, FakeClient([_page(1, [{"Code": "72030"}])]))
        with pytest.raises(JQuantsDatasetAlreadyRunningError):
            store.recover_dataset(dataset_id)
    finally:
        process.wait(timeout=10)

    result = _capture(store, FakeClient([_page(1, [{"Code": "72030"}])]))
    assert result.state is JQuantsDatasetState.COMPLETE
