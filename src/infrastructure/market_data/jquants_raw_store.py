"""J-Quants raw page capture, durable ledger, and recoverable publication."""

import hashlib
import json
import logging
import os
import re
import shutil
import sqlite3
import sys
import tempfile
import threading
import uuid
from collections.abc import Callable, Generator, Iterable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path
from time import perf_counter
from typing import Any, BinaryIO, Protocol

from src.config import config
from src.infrastructure.market_data.jquants_v2_client import JQuantsAPIError, JQuantsPage

_SAFE_ENDPOINT_SEGMENT = re.compile(r"[A-Za-z0-9_-]+\Z")
_SAFE_CODE = re.compile(r"[A-Za-z0-9._-]{1,20}\Z")
_SAFE_DATE = re.compile(r"\d{4}-\d{2}-\d{2}\Z")
_ALLOWED_CONDITIONS = frozenset({"date", "code", "from", "to"})
_PUBLISHED_STATES = frozenset({"COMPLETE", "EMPTY_UNKNOWN"})
_PROCESS_LOCKS: dict[str, threading.Lock] = {}
_PROCESS_LOCKS_GUARD = threading.Lock()


class JQuantsDatasetState(StrEnum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    COMPLETE = "COMPLETE"
    EMPTY_UNKNOWN = "EMPTY_UNKNOWN"
    EMPTY_HELD = "EMPTY_HELD"
    PARTIAL = "PARTIAL"
    FAILED = "FAILED"


class JQuantsRawStoreError(Exception):
    """Safe, user-facing error raised by raw capture and publication."""

    category = "RAW_STORE_ERROR"


class JQuantsDatasetAlreadyRunningError(JQuantsRawStoreError):
    """The dataset is already being captured by another invocation."""


class JQuantsRawValidationError(JQuantsRawStoreError):
    """A captured page or request condition failed validation."""

    category = "RESPONSE_SCHEMA_ERROR"


@dataclass(frozen=True)
class JQuantsDatasetResult:
    dataset_id: str
    state: JQuantsDatasetState
    page_count: int
    row_count: int
    content_sha256: str | None
    committed_path: Path | None
    warning_category: str | None = None
    validation_seconds: float = 0.0


@dataclass(frozen=True)
class _DatasetLockHandle:
    file: BinaryIO
    process_lock: Any


@dataclass(frozen=True)
class JQuantsIncompleteDataset:
    dataset_id: str
    kind: str
    endpoint: str
    conditions: Mapping[str, str]
    state: JQuantsDatasetState
    page_count: int
    row_count: int
    error_category: str | None


@dataclass(frozen=True)
class JQuantsPublishedRaw:
    manifest: Mapping[str, Any]
    pages_path: Path

    def iter_pages(self) -> Iterator[list[dict[str, Any]]]:
        """Stream a pinned, previously verified immutable generation."""
        count = 0
        rows = 0
        with self.pages_path.open(encoding="utf-8") as source:
            for count, line in enumerate(source, 1):
                record = json.loads(line)
                data = record["response"]["data"]
                digest = hashlib.sha256(
                    JQuantsRawStore._json_text({"data": data}).encode("utf-8")
                ).hexdigest()
                if (
                    count > self.manifest["page_count"]
                    or digest != self.manifest["page_sha256"][count - 1]
                ):
                    raise JQuantsRawValidationError("Published raw changed during reading")
                rows += len(data)
                yield data
        if count != self.manifest["page_count"] or rows != self.manifest["row_count"]:
            raise JQuantsRawValidationError("Published raw counts changed during reading")


class _PagedClient(Protocol):
    def iter_pages(
        self,
        endpoint: str,
        params: Mapping[str, Any] | None = None,
    ) -> Iterable[JQuantsPage]: ...


class JQuantsRawStore:
    """Capture sanitized raw data pages and publish immutable dataset generations."""

    def __init__(
        self,
        raw_directory: Path | str | None = None,
        ledger_database_file: Path | str | None = None,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if sys.platform != "win32":
            raise JQuantsRawStoreError(
                "J-Quants raw storage currently requires Windows file locking"
            )
        self.raw_directory = Path(
            config.JQUANTS_RAW_DIRECTORY if raw_directory is None else raw_directory
        ).expanduser().resolve()
        self.ledger_database_file = Path(
            config.JQUANTS_LEDGER_DATABASE_FILE
            if ledger_database_file is None
            else ledger_database_file
        ).expanduser().resolve()
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._initialize_ledger()
        self._migrate_published_references()

    def capture(
        self,
        client: _PagedClient,
        *,
        kind: str,
        endpoint: str,
        conditions: Mapping[str, Any] | None = None,
    ) -> JQuantsDatasetResult:
        safe_endpoint = self._validate_endpoint(endpoint)
        safe_conditions = self._validate_conditions(
            {} if conditions is None else conditions
        )
        dataset_id = self._dataset_id(kind, safe_endpoint, safe_conditions)
        run_id = uuid.uuid4().hex
        staging_directory = self.raw_directory / "staging" / dataset_id / run_id

        lock_handle = self._acquire_dataset_lock(dataset_id)
        try:
            self._recover_dataset_locked(dataset_id)
            self._claim_dataset(dataset_id, kind, safe_endpoint, safe_conditions, run_id)
            try:
                staging_directory.mkdir(parents=True, exist_ok=False)
                pages_path = staging_directory / "pages.jsonl"
                page_count, row_count, page_hashes = self._capture_pages(
                    client.iter_pages(safe_endpoint, safe_conditions),
                    pages_path,
                    dataset_id,
                    kind,
                    safe_endpoint,
                    safe_conditions,
                    run_id,
                )
                content_hash = self._content_hash(
                    dataset_id, kind, safe_endpoint, safe_conditions, page_hashes
                )
                state = (
                    JQuantsDatasetState.EMPTY_UNKNOWN
                    if row_count == 0
                    else JQuantsDatasetState.COMPLETE
                )
                validation_start = perf_counter()
                self._verify_capture(
                    pages_path,
                    dataset_id,
                    kind,
                    safe_endpoint,
                    safe_conditions,
                    run_id,
                    page_count,
                    row_count,
                    page_hashes,
                )
                validation_seconds = perf_counter() - validation_start
                published_manifest = self._get_published_manifest(
                    dataset_id, kind, safe_endpoint, safe_conditions
                )
                if (
                    row_count == 0
                    and published_manifest is not None
                    and published_manifest["row_count"] > 0
                ):
                    state = JQuantsDatasetState.EMPTY_HELD
                    self._finish_empty_held(
                        dataset_id, run_id, page_count, content_hash
                    )
                    committed_path = (
                        self._dataset_committed_directory(dataset_id)
                        / "generations"
                        / published_manifest["generation_id"]
                    )
                else:
                    committed_path = self._publish_generation(
                        dataset_id,
                        staging_directory,
                        kind,
                        safe_endpoint,
                        safe_conditions,
                        page_count,
                        row_count,
                        page_hashes,
                        content_hash,
                        state,
                    )
                    self._prepare_publication(
                        dataset_id,
                        run_id,
                        state,
                        page_count,
                        row_count,
                        content_hash,
                    )
                    self._publish_pointer(
                        self._dataset_committed_directory(dataset_id), content_hash
                    )
                    self._finish_dataset(
                        dataset_id,
                        run_id,
                        state,
                        page_count,
                        row_count,
                        content_hash,
                    )
                warning_category = self._cleanup_staging(
                    dataset_id, run_id, staging_directory
                )
                return JQuantsDatasetResult(
                    dataset_id=dataset_id,
                    state=state,
                    page_count=page_count,
                    row_count=row_count,
                    content_sha256=content_hash,
                    committed_path=committed_path,
                    warning_category=warning_category,
                    validation_seconds=validation_seconds,
                )
            except BaseException as error:
                if not self._has_pending_publication(dataset_id, run_id):
                    state = (
                        JQuantsDatasetState.PARTIAL
                        if self._page_count(dataset_id, run_id) > 0
                        else JQuantsDatasetState.FAILED
                    )
                    try:
                        self._fail_dataset(
                            dataset_id, run_id, state, self._safe_error_category(error)
                        )
                    except Exception:
                        raise JQuantsRawStoreError(
                            "J-Quants dataset failed and its ledger state could not be updated"
                        ) from None
                if isinstance(error, (JQuantsRawStoreError, JQuantsAPIError)):
                    raise
                if isinstance(error, Exception):
                    category = self._safe_error_category(error)
                    raise JQuantsRawStoreError(
                        f"J-Quants dataset capture failed ({category})"
                    ) from None
                raise
        finally:
            self._release_dataset_lock(lock_handle)

    def recover_dataset(self, dataset_id: str) -> None:
        """Recover a prepared publication while holding its dataset lock."""
        self._validate_dataset_id(dataset_id)
        lock_handle = self._acquire_dataset_lock(dataset_id)
        try:
            self._recover_dataset_locked(dataset_id)
        finally:
            self._release_dataset_lock(lock_handle)

    def get_state(self, dataset_id: str) -> JQuantsDatasetState | None:
        self._validate_dataset_id(dataset_id)
        with self._connection() as connection:
            row = connection.execute(
                "SELECT state FROM datasets WHERE dataset_id = ?", (dataset_id,)
            ).fetchone()
        return None if row is None else JQuantsDatasetState(row[0])

    def get_state_for(
        self, *, kind: str, endpoint: str, conditions: Mapping[str, Any]
    ) -> JQuantsDatasetState | None:
        """台帳の状態を条件から引く読み取り専用の入口。記録が無ければNone。"""
        dataset_id = self._dataset_id(
            kind, self._validate_endpoint(endpoint), self._validate_conditions(conditions)
        )
        return self.get_state(dataset_id)

    def list_incomplete_datasets(self) -> list[JQuantsIncompleteDataset]:
        states = tuple(
            state.value
            for state in (
                JQuantsDatasetState.PENDING,
                JQuantsDatasetState.RUNNING,
                JQuantsDatasetState.PARTIAL,
                JQuantsDatasetState.FAILED,
            )
        )
        return self._list_datasets(states)

    def list_attention_datasets(self) -> list[JQuantsIncompleteDataset]:
        """Empty results needing review, distinct from incomplete captures."""
        return self._list_datasets(("EMPTY_UNKNOWN", "EMPTY_HELD"))

    def _list_datasets(self, states: tuple[str, ...]) -> list[JQuantsIncompleteDataset]:
        placeholders = ",".join("?" for _ in states)
        with self._connection() as connection:
            rows = connection.execute(
                f"""
                SELECT dataset_id, kind, endpoint, conditions_json, state,
                       page_count, row_count, error_category
                FROM datasets
                WHERE state IN ({placeholders})
                ORDER BY updated_at_utc, dataset_id
                """,
                states,
            ).fetchall()
        return [
            JQuantsIncompleteDataset(
                dataset_id=row[0],
                kind=row[1],
                endpoint=row[2],
                conditions=json.loads(row[3]),
                state=JQuantsDatasetState(row[4]),
                page_count=row[5],
                row_count=row[6],
                error_category=row[7],
            )
            for row in rows
        ]

    @contextmanager
    def ingestion_lock(
        self, *, kind: str, endpoint: str, conditions: Mapping[str, Any]
    ) -> Generator[str, None, None]:
        """Serialize whole ingestion jobs; capture/recovery retain their own lock."""
        safe_endpoint = self._validate_endpoint(endpoint)
        safe_conditions = self._validate_conditions(conditions)
        dataset_id = self._dataset_id(kind, safe_endpoint, safe_conditions)
        operation_id = hashlib.sha256(f"ingestion:{dataset_id}".encode("ascii")).hexdigest()
        handle = self._acquire_dataset_lock(operation_id)
        try:
            yield dataset_id
        finally:
            self._release_dataset_lock(handle)

    def resolve_published(
        self, *, kind: str, endpoint: str, conditions: Mapping[str, Any]
    ) -> JQuantsPublishedRaw | None:
        manifest = self.get_current_manifest(
            kind=kind, endpoint=endpoint, conditions=conditions
        )
        if manifest is None:
            return None
        return JQuantsPublishedRaw(
            manifest=manifest,
            pages_path=(
                self._dataset_committed_directory(manifest["dataset_id"])
                / "generations" / manifest["generation_id"] / "pages.jsonl"
            ),
        )

    def get_current_manifest(
        self,
        *,
        kind: str,
        endpoint: str,
        conditions: Mapping[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        safe_endpoint = self._validate_endpoint(endpoint)
        safe_conditions = self._validate_conditions(
            {} if conditions is None else conditions
        )
        dataset_id = self._dataset_id(kind, safe_endpoint, safe_conditions)
        return self._get_published_manifest(
            dataset_id, kind, safe_endpoint, safe_conditions
        )

    def _get_published_manifest(
        self,
        dataset_id: str,
        kind: str,
        endpoint: str,
        conditions: Mapping[str, str],
    ) -> dict[str, Any] | None:
        for _ in range(3):
            with self._connection() as connection:
                connection.execute("BEGIN")
                row = connection.execute(
                    """
                    SELECT kind, endpoint, conditions_json, published_generation_id
                    FROM datasets WHERE dataset_id = ?
                    """,
                    (dataset_id,),
                ).fetchone()
                pending = connection.execute(
                    """
                    SELECT generation_id FROM pending_publications
                    WHERE dataset_id = ?
                    """,
                    (dataset_id,),
                ).fetchone()
            if row is None:
                return None
            try:
                stored_conditions = json.loads(row[2])
            except (TypeError, json.JSONDecodeError):
                raise JQuantsRawStoreError(
                    "J-Quants ledger dataset identity is invalid"
                ) from None
            if (
                row[0] != kind
                or row[1] != endpoint
                or stored_conditions != dict(conditions)
            ):
                raise JQuantsRawStoreError(
                    "J-Quants ledger dataset identity is inconsistent"
                )
            generation_id = row[3]
            pointer_id = self._read_pointer(dataset_id)
            if generation_id is None:
                if pointer_id is None:
                    return None
                if pending is not None and pointer_id == pending[0]:
                    self._verify_generation(
                        dataset_id,
                        pending[0],
                        expected_kind=kind,
                        expected_endpoint=endpoint,
                        expected_conditions=conditions,
                    )
                    raise JQuantsRawStoreError(
                        "J-Quants has no previously confirmed publication"
                    )
                continue
            if pointer_id == generation_id:
                return self._verify_generation(
                    dataset_id,
                    generation_id,
                    expected_kind=kind,
                    expected_endpoint=endpoint,
                    expected_conditions=conditions,
                )
            if pending is not None and pointer_id == pending[0]:
                manifest = self._verify_generation(
                    dataset_id,
                    generation_id,
                    expected_kind=kind,
                    expected_endpoint=endpoint,
                    expected_conditions=conditions,
                )
                self._verify_generation(
                    dataset_id,
                    pending[0],
                    expected_kind=kind,
                    expected_endpoint=endpoint,
                    expected_conditions=conditions,
                )
                return manifest
        raise JQuantsRawStoreError(
            "J-Quants CURRENT disagrees with the verified published generation"
        )

    def _read_pointer(self, dataset_id: str) -> str | None:
        pointer_path = self._dataset_committed_directory(dataset_id) / "CURRENT"
        try:
            generation_id = pointer_path.read_text(encoding="ascii").strip()
        except FileNotFoundError:
            return None
        except (OSError, UnicodeError):
            raise JQuantsRawStoreError("J-Quants CURRENT pointer is unreadable") from None
        if not re.fullmatch(r"[a-f0-9]{64}", generation_id):
            raise JQuantsRawStoreError("J-Quants CURRENT pointer is invalid")
        return generation_id

    def _verify_generation(
        self,
        dataset_id: str,
        generation_id: str,
        *,
        expected_kind: str | None = None,
        expected_endpoint: str | None = None,
        expected_conditions: Mapping[str, str] | None = None,
        expected_page_count: int | None = None,
        expected_row_count: int | None = None,
        expected_page_hashes: list[str] | None = None,
    ) -> dict[str, Any]:
        if not re.fullmatch(r"[a-f0-9]{64}", generation_id):
            raise JQuantsRawStoreError("J-Quants generation identifier is invalid")
        generation_directory = (
            self._dataset_committed_directory(dataset_id)
            / "generations"
            / generation_id
        )
        try:
            manifest = json.loads(
                (generation_directory / "manifest.json").read_text(encoding="utf-8")
            )
            lines = (generation_directory / "pages.jsonl").read_text(
                encoding="utf-8"
            ).splitlines()
        except (OSError, UnicodeError, json.JSONDecodeError):
            raise JQuantsRawStoreError(
                "J-Quants committed generation is unreadable"
            ) from None
        if (
            not isinstance(manifest, dict)
            or manifest.get("schema_version") != 1
            or manifest.get("dataset_id") != dataset_id
            or manifest.get("generation_id") != generation_id
            or manifest.get("content_sha256") != generation_id
            or manifest.get("pages_file") != "pages.jsonl"
            or manifest.get("state") not in _PUBLISHED_STATES
            or not isinstance(manifest.get("kind"), str)
            or not isinstance(manifest.get("endpoint"), str)
            or not isinstance(manifest.get("conditions"), dict)
            or not isinstance(manifest.get("page_count"), int)
            or isinstance(manifest.get("page_count"), bool)
            or not isinstance(manifest.get("row_count"), int)
            or isinstance(manifest.get("row_count"), bool)
            or not isinstance(manifest.get("page_sha256"), list)
        ):
            raise JQuantsRawStoreError("J-Quants committed manifest is invalid")

        page_count = manifest["page_count"]
        row_count = manifest["row_count"]
        page_hashes = manifest["page_sha256"]
        if (
            page_count <= 0
            or row_count < 0
            or len(lines) != page_count
            or len(page_hashes) != page_count
            or any(not isinstance(value, str) or not re.fullmatch(r"[a-f0-9]{64}", value) for value in page_hashes)
            or (manifest["state"] == JQuantsDatasetState.COMPLETE.value and row_count == 0)
            or (manifest["state"] == JQuantsDatasetState.EMPTY_UNKNOWN.value and row_count != 0)
            or (expected_kind is not None and manifest["kind"] != expected_kind)
            or (expected_endpoint is not None and manifest["endpoint"] != expected_endpoint)
            or (
                expected_conditions is not None
                and manifest["conditions"] != dict(expected_conditions)
            )
            or (expected_page_count is not None and page_count != expected_page_count)
            or (expected_row_count is not None and row_count != expected_row_count)
            or (expected_page_hashes is not None and page_hashes != expected_page_hashes)
        ):
            raise JQuantsRawStoreError("J-Quants committed manifest is inconsistent")

        observed_hashes: list[str] = []
        observed_rows = 0
        seen_hashes: set[str] = set()
        try:
            for page_number, line in enumerate(lines, start=1):
                record = json.loads(line)
                response = record.get("response") if isinstance(record, dict) else None
                data = response.get("data") if isinstance(response, dict) else None
                page = record.get("page") if isinstance(record, dict) else None
                if (
                    not isinstance(record, dict)
                    or record.get("schema_version") != 1
                    or record.get("dataset_id") != dataset_id
                    or record.get("kind") != manifest["kind"]
                    or record.get("endpoint") != manifest["endpoint"]
                    or record.get("conditions") != manifest["conditions"]
                    or record.get("page_number") != page_number
                    or "pagination_key" in record
                    or "api_key" in record
                    or not isinstance(data, list)
                    or any(not isinstance(item, Mapping) for item in data)
                    or not isinstance(page, dict)
                    or page.get("row_count") != len(data)
                    or not isinstance(page.get("status_code"), int)
                    or not 200 <= page["status_code"] < 300
                ):
                    raise JQuantsRawStoreError(
                        "J-Quants committed page metadata is invalid"
                    )
                page_hash = hashlib.sha256(
                    self._json_text({"data": data}).encode("utf-8")
                ).hexdigest()
                if page.get("content_sha256") != page_hash:
                    raise JQuantsRawStoreError("J-Quants committed page hash is invalid")
                if data and page_hash in seen_hashes:
                    raise JQuantsRawStoreError("Duplicate J-Quants committed page detected")
                if data:
                    seen_hashes.add(page_hash)
                observed_hashes.append(page_hash)
                observed_rows += len(data)
        except (TypeError, ValueError, UnicodeError):
            raise JQuantsRawStoreError("J-Quants committed page is invalid") from None
        calculated_hash = self._content_hash(
            dataset_id,
            manifest["kind"],
            manifest["endpoint"],
            manifest["conditions"],
            observed_hashes,
        )
        if (
            observed_hashes != page_hashes
            or observed_rows != row_count
            or calculated_hash != generation_id
        ):
            raise JQuantsRawStoreError("J-Quants committed generation hash is invalid")
        return manifest

    def _initialize_ledger(self) -> None:
        self.ledger_database_file.parent.mkdir(parents=True, exist_ok=True)
        with self._connection() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS datasets (
                    dataset_id TEXT PRIMARY KEY,
                    kind TEXT NOT NULL,
                    endpoint TEXT NOT NULL,
                    conditions_json TEXT NOT NULL,
                    state TEXT NOT NULL,
                    run_id TEXT,
                    started_at_utc TEXT,
                    updated_at_utc TEXT NOT NULL,
                    completed_at_utc TEXT,
                    page_count INTEGER NOT NULL DEFAULT 0,
                    row_count INTEGER NOT NULL DEFAULT 0,
                    content_sha256 TEXT,
                    error_category TEXT
                );
                CREATE TABLE IF NOT EXISTS dataset_pages (
                    dataset_id TEXT NOT NULL,
                    run_id TEXT NOT NULL,
                    page_number INTEGER NOT NULL,
                    row_count INTEGER NOT NULL,
                    status_code INTEGER NOT NULL,
                    content_sha256 TEXT NOT NULL,
                    captured_at_utc TEXT NOT NULL,
                    PRIMARY KEY (dataset_id, run_id, page_number),
                    FOREIGN KEY (dataset_id) REFERENCES datasets(dataset_id)
                );
                CREATE TABLE IF NOT EXISTS dataset_attempts (
                    attempt_id TEXT PRIMARY KEY,
                    dataset_id TEXT NOT NULL,
                    state TEXT NOT NULL,
                    started_at_utc TEXT NOT NULL,
                    updated_at_utc TEXT NOT NULL,
                    completed_at_utc TEXT,
                    page_count INTEGER NOT NULL DEFAULT 0,
                    row_count INTEGER NOT NULL DEFAULT 0,
                    content_sha256 TEXT,
                    error_category TEXT,
                    warning_category TEXT
                );
                CREATE TABLE IF NOT EXISTS pending_publications (
                    dataset_id TEXT PRIMARY KEY,
                    attempt_id TEXT NOT NULL,
                    generation_id TEXT NOT NULL,
                    state TEXT NOT NULL,
                    page_count INTEGER NOT NULL,
                    row_count INTEGER NOT NULL,
                    content_sha256 TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS store_settings (
                    setting_key TEXT PRIMARY KEY,
                    setting_value TEXT NOT NULL
                );
                """
            )
            columns = {
                row[1]
                for row in connection.execute("PRAGMA table_info(datasets)").fetchall()
            }
            for column_name in ("published_generation_id", "post_publish_warning_category"):
                if column_name not in columns:
                    connection.execute(
                        f"ALTER TABLE datasets ADD COLUMN {column_name} TEXT"
                    )
            normalized_root = os.path.normcase(str(self.raw_directory))
            connection.execute(
                """
                INSERT OR IGNORE INTO store_settings (setting_key, setting_value)
                VALUES ('raw_directory', ?)
                """,
                (normalized_root,),
            )
            configured_root = connection.execute(
                "SELECT setting_value FROM store_settings WHERE setting_key = 'raw_directory'"
            ).fetchone()[0]
            if configured_root != normalized_root:
                raise JQuantsRawStoreError(
                    "J-Quants ledger is already associated with a different raw directory"
                )
            connection.execute(
                """
                INSERT OR IGNORE INTO dataset_attempts (
                    attempt_id, dataset_id, state, started_at_utc, updated_at_utc,
                    completed_at_utc, page_count, row_count, content_sha256,
                    error_category
                )
                SELECT run_id, dataset_id, state, COALESCE(started_at_utc, updated_at_utc),
                       updated_at_utc, completed_at_utc, page_count, row_count,
                       content_sha256, error_category
                FROM datasets WHERE run_id IS NOT NULL
                """
            )

    def _migrate_published_references(self) -> None:
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT dataset_id, kind, endpoint, conditions_json
                FROM datasets WHERE published_generation_id IS NULL
                """
            ).fetchall()
        for dataset_id, kind, endpoint, conditions_json in rows:
            pointer_id = self._read_pointer(dataset_id)
            if pointer_id is None:
                continue
            conditions = json.loads(conditions_json)
            try:
                self._verify_generation(
                    dataset_id,
                    pointer_id,
                    expected_kind=kind,
                    expected_endpoint=endpoint,
                    expected_conditions=conditions,
                )
            except JQuantsRawStoreError:
                continue
            with self._connection() as connection:
                connection.execute(
                    """
                    UPDATE datasets SET published_generation_id = ?
                    WHERE dataset_id = ? AND published_generation_id IS NULL
                    """,
                    (pointer_id, dataset_id),
                )

    def _recover_dataset_locked(self, dataset_id: str) -> None:
        with self._connection() as connection:
            dataset = connection.execute(
                """
                SELECT kind, endpoint, conditions_json, published_generation_id
                FROM datasets WHERE dataset_id = ?
                """,
                (dataset_id,),
            ).fetchone()
            pending = connection.execute(
                """
                SELECT attempt_id, generation_id, state, page_count, row_count,
                       content_sha256
                FROM pending_publications WHERE dataset_id = ?
                """,
                (dataset_id,),
            ).fetchone()
        if dataset is None:
            if self._read_pointer(dataset_id) is not None:
                raise JQuantsRawStoreError(
                    "J-Quants CURRENT exists without a ledger dataset"
                )
            return

        kind, endpoint, conditions_json, published_id = dataset
        conditions = json.loads(conditions_json)
        committed_directory = self._dataset_committed_directory(dataset_id)
        if pending is not None:
            attempt_id, generation_id, state_value, page_count, row_count, content_hash = pending
            try:
                manifest = self._verify_generation(
                    dataset_id,
                    generation_id,
                    expected_kind=kind,
                    expected_endpoint=endpoint,
                    expected_conditions=conditions,
                    expected_page_count=page_count,
                    expected_row_count=row_count,
                )
                if (
                    content_hash != generation_id
                    or manifest["state"] != state_value
                ):
                    raise JQuantsRawStoreError(
                        "J-Quants pending publication metadata is inconsistent"
                    )
            except JQuantsRawStoreError:
                if published_id is None:
                    raise JQuantsRawStoreError(
                        "J-Quants prepared generation is invalid and no prior publication is verified"
                    ) from None
                self._verify_generation(
                    dataset_id,
                    published_id,
                    expected_kind=kind,
                    expected_endpoint=endpoint,
                    expected_conditions=conditions,
                )
                self._publish_pointer(committed_directory, published_id)
                self._abort_pending_publication(dataset_id, attempt_id)
                return

            self._publish_pointer(committed_directory, generation_id)
            self._finish_dataset(
                dataset_id,
                attempt_id,
                JQuantsDatasetState(state_value),
                page_count,
                row_count,
                content_hash,
            )
            self._cleanup_staging(
                dataset_id,
                attempt_id,
                self.raw_directory / "staging" / dataset_id / attempt_id,
            )
            return

        if published_id is None:
            if self._read_pointer(dataset_id) is not None:
                raise JQuantsRawStoreError(
                    "J-Quants CURRENT has no verified ledger publication reference"
                )
            return
        self._verify_generation(
            dataset_id,
            published_id,
            expected_kind=kind,
            expected_endpoint=endpoint,
            expected_conditions=conditions,
        )
        try:
            pointer_id = self._read_pointer(dataset_id)
        except JQuantsRawStoreError:
            pointer_id = None
        if pointer_id != published_id:
            self._publish_pointer(committed_directory, published_id)

    def _abort_pending_publication(self, dataset_id: str, attempt_id: str) -> None:
        now = self._timestamp()
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                UPDATE datasets
                SET state = ?, updated_at_utc = ?, completed_at_utc = ?,
                    error_category = 'GENERATION_INTEGRITY_ERROR'
                WHERE dataset_id = ? AND run_id = ?
                """,
                (
                    JQuantsDatasetState.FAILED.value,
                    now,
                    now,
                    dataset_id,
                    attempt_id,
                ),
            )
            connection.execute(
                """
                UPDATE dataset_attempts
                SET state = ?, updated_at_utc = ?, completed_at_utc = ?,
                    error_category = 'GENERATION_INTEGRITY_ERROR'
                WHERE attempt_id = ? AND dataset_id = ?
                """,
                (
                    JQuantsDatasetState.FAILED.value,
                    now,
                    now,
                    attempt_id,
                    dataset_id,
                ),
            )
            connection.execute(
                "DELETE FROM pending_publications WHERE dataset_id = ?",
                (dataset_id,),
            )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.ledger_database_file, timeout=30)
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 30000")
        connection.execute("PRAGMA synchronous = FULL")
        return connection

    def _acquire_dataset_lock(self, dataset_id: str) -> _DatasetLockHandle:
        if sys.platform != "win32":
            raise JQuantsRawStoreError(
                "J-Quants raw storage currently requires Windows file locking"
            )
        try:
            import msvcrt
        except ImportError:
            raise JQuantsRawStoreError(
                "Windows file-locking support is unavailable"
            ) from None

        lock_path = self.raw_directory / "locks" / f"{dataset_id}.lock"
        normalized_lock_path = os.path.normcase(str(lock_path.resolve()))
        with _PROCESS_LOCKS_GUARD:
            process_lock = _PROCESS_LOCKS.setdefault(
                normalized_lock_path, threading.Lock()
            )
        if not process_lock.acquire(blocking=False):
            raise JQuantsDatasetAlreadyRunningError(
                "J-Quants dataset is already running"
            )

        handle: BinaryIO | None = None
        locked = False
        try:
            lock_path.parent.mkdir(parents=True, exist_ok=True)
            handle = lock_path.open("a+b")
            if lock_path.stat().st_size == 0:
                handle.write(b"\0")
                handle.flush()
            handle.seek(0)
            try:
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError:
                raise JQuantsDatasetAlreadyRunningError(
                    "J-Quants dataset is already running"
                ) from None
            locked = True
            return _DatasetLockHandle(handle, process_lock)
        except BaseException as error:
            try:
                if handle is not None:
                    try:
                        if locked:
                            handle.seek(0)
                            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                    except OSError:
                        logging.getLogger(__name__).warning(
                            "J-Quants lock cleanup failed",
                            extra={"error_category": "LOCK_CLEANUP_FAILED"},
                        )
                    finally:
                        handle.close()
            finally:
                process_lock.release()
            if isinstance(error, OSError):
                raise JQuantsRawStoreError(
                    "J-Quants dataset lock could not be opened"
                ) from None
            raise

    @staticmethod
    def _release_dataset_lock(handle: _DatasetLockHandle) -> None:
        release_error: JQuantsRawStoreError | None = None
        try:
            try:
                import msvcrt
            except ImportError:
                release_error = JQuantsRawStoreError(
                    "Windows file-locking support became unavailable"
                )
            else:
                try:
                    handle.file.seek(0)
                    msvcrt.locking(handle.file.fileno(), msvcrt.LK_UNLCK, 1)
                except OSError:
                    release_error = JQuantsRawStoreError(
                        "J-Quants dataset lock could not be released cleanly"
                    )
        finally:
            try:
                handle.file.close()
            finally:
                handle.process_lock.release()
        if release_error is not None:
            raise release_error

    @contextmanager
    def _connection(self) -> Generator[sqlite3.Connection, None, None]:
        connection = self._connect()
        try:
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _claim_dataset(
        self,
        dataset_id: str,
        kind: str,
        endpoint: str,
        conditions: Mapping[str, str],
        run_id: str,
    ) -> None:
        now = self._timestamp()
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                INSERT INTO datasets (
                    dataset_id, kind, endpoint, conditions_json, state, run_id,
                    started_at_utc, updated_at_utc, completed_at_utc, page_count,
                    row_count, content_sha256, error_category
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, 0, 0, NULL, NULL)
                ON CONFLICT(dataset_id) DO UPDATE SET
                    kind = excluded.kind,
                    endpoint = excluded.endpoint,
                    conditions_json = excluded.conditions_json,
                    state = excluded.state,
                    run_id = excluded.run_id,
                    started_at_utc = excluded.started_at_utc,
                    updated_at_utc = excluded.updated_at_utc,
                    completed_at_utc = NULL,
                    page_count = 0,
                    row_count = 0,
                    content_sha256 = NULL,
                    error_category = NULL,
                    post_publish_warning_category = NULL
                """,
                (
                    dataset_id,
                    kind,
                    endpoint,
                    self._json_text(dict(conditions)),
                    JQuantsDatasetState.RUNNING.value,
                    run_id,
                    now,
                    now,
                ),
            )
            connection.execute(
                """
                INSERT INTO dataset_attempts (
                    attempt_id, dataset_id, state, started_at_utc, updated_at_utc
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    dataset_id,
                    JQuantsDatasetState.RUNNING.value,
                    now,
                    now,
                ),
            )
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _capture_pages(
        self,
        pages: Iterable[JQuantsPage],
        pages_path: Path,
        dataset_id: str,
        kind: str,
        endpoint: str,
        conditions: Mapping[str, str],
        run_id: str,
    ) -> tuple[int, int, list[str]]:
        page_count = 0
        row_count = 0
        page_hashes: list[str] = []
        seen_page_hashes: set[str] = set()
        with pages_path.open("xb") as output:
            for expected_page_number, page in enumerate(pages, start=1):
                page_hash, page_rows = self._validate_page(
                    page, expected_page_number, endpoint
                )
                if page_rows and page_hash in seen_page_hashes:
                    raise JQuantsRawValidationError("Duplicate J-Quants page detected")
                if page_rows:
                    seen_page_hashes.add(page_hash)
                captured_at = self._timestamp()
                record = {
                    "schema_version": 1,
                    "dataset_id": dataset_id,
                    "kind": kind,
                    "endpoint": endpoint,
                    "conditions": dict(conditions),
                    "page_number": expected_page_number,
                    "captured_at_utc": captured_at,
                    "page": {
                        "status_code": page.metadata.status_code,
                        "row_count": page_rows,
                        "content_sha256": page_hash,
                    },
                    "response": {"data": list(page.data)},
                }
                line = (self._json_text(record) + "\n").encode("utf-8")
                output.write(line)
                output.flush()
                os.fsync(output.fileno())
                self._record_page(
                    dataset_id,
                    run_id,
                    expected_page_number,
                    page_rows,
                    page.metadata.status_code,
                    page_hash,
                    captured_at,
                )
                page_count += 1
                row_count += page_rows
                page_hashes.append(page_hash)
        if page_count == 0:
            raise JQuantsRawValidationError("J-Quants returned no pages")
        return page_count, row_count, page_hashes

    @staticmethod
    def _validate_page(
        page: JQuantsPage,
        expected_page_number: int,
        endpoint: str,
    ) -> tuple[str, int]:
        metadata = page.metadata
        if (
            not isinstance(page.data, (tuple, list))
            or not isinstance(page.raw_body, bytes)
            or not isinstance(metadata.page_number, int)
            or not isinstance(metadata.row_count, int)
            or not isinstance(metadata.status_code, int)
            or not isinstance(metadata.endpoint, str)
            or not isinstance(metadata.has_next_page, bool)
            or metadata.page_number != expected_page_number
            or metadata.endpoint != endpoint
            or not 200 <= metadata.status_code < 300
            or metadata.row_count != len(page.data)
            or any(not isinstance(row, Mapping) for row in page.data)
        ):
            raise JQuantsRawValidationError("J-Quants page metadata is inconsistent")
        try:
            raw_payload = json.loads(page.raw_body)
        except (TypeError, ValueError, UnicodeDecodeError):
            raise JQuantsRawValidationError("J-Quants raw response is invalid") from None
        if (
            not isinstance(raw_payload, dict)
            or raw_payload.get("data") != list(page.data)
            or not isinstance(raw_payload.get("data"), list)
        ):
            raise JQuantsRawValidationError("J-Quants raw response data is inconsistent")
        safe_body = JQuantsRawStore._json_text({"data": list(page.data)}).encode("utf-8")
        return hashlib.sha256(safe_body).hexdigest(), len(page.data)

    def _record_page(
        self,
        dataset_id: str,
        run_id: str,
        page_number: int,
        row_count: int,
        status_code: int,
        content_sha256: str,
        captured_at: str,
    ) -> None:
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT state, run_id FROM datasets WHERE dataset_id = ?
                """,
                (dataset_id,),
            ).fetchone()
            if (
                row is None
                or row[0] != JQuantsDatasetState.RUNNING.value
                or row[1] != run_id
            ):
                raise JQuantsRawStoreError("J-Quants dataset run ownership changed")
            connection.execute(
                """
                INSERT INTO dataset_pages (
                    dataset_id, run_id, page_number, row_count, status_code,
                    content_sha256, captured_at_utc
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    dataset_id,
                    run_id,
                    page_number,
                    row_count,
                    status_code,
                    content_sha256,
                    captured_at,
                ),
            )
            cursor = connection.execute(
                """
                UPDATE datasets
                SET page_count = page_count + 1,
                    row_count = row_count + ?,
                    updated_at_utc = ?
                WHERE dataset_id = ? AND run_id = ? AND state = ?
                """,
                (
                    row_count,
                    captured_at,
                    dataset_id,
                    run_id,
                    JQuantsDatasetState.RUNNING.value,
                ),
            )
            if cursor.rowcount != 1:
                raise JQuantsRawStoreError("J-Quants dataset progress could not be recorded")
            connection.execute(
                """
                UPDATE dataset_attempts
                SET page_count = page_count + 1,
                    row_count = row_count + ?,
                    updated_at_utc = ?
                WHERE attempt_id = ? AND dataset_id = ?
                """,
                (row_count, captured_at, run_id, dataset_id),
            )

    def _verify_capture(
        self,
        pages_path: Path,
        dataset_id: str,
        kind: str,
        endpoint: str,
        conditions: Mapping[str, str],
        run_id: str,
        expected_page_count: int,
        expected_row_count: int,
        expected_hashes: list[str],
    ) -> None:
        staged_hashes: list[str] = []
        staged_row_counts: list[int] = []
        staged_rows = 0
        seen_hashes: set[str] = set()
        try:
            with pages_path.open("r", encoding="utf-8") as source:
                for expected_number, line in enumerate(source, start=1):
                    record = json.loads(line)
                    if (
                        not isinstance(record, dict)
                        or record.get("dataset_id") != dataset_id
                        or record.get("kind") != kind
                        or record.get("endpoint") != endpoint
                        or record.get("conditions") != dict(conditions)
                        or record.get("page_number") != expected_number
                        or "pagination_key" in record
                        or "api_key" in record
                    ):
                        raise JQuantsRawValidationError(
                            "J-Quants staging page metadata is invalid"
                        )
                    response = record.get("response")
                    data = response.get("data") if isinstance(response, dict) else None
                    metadata = record.get("page")
                    if (
                        not isinstance(data, list)
                        or any(not isinstance(row, Mapping) for row in data)
                        or not isinstance(metadata, dict)
                        or not isinstance(metadata.get("row_count"), int)
                        or metadata.get("row_count") != len(data)
                        or not isinstance(metadata.get("status_code"), int)
                        or not 200 <= metadata["status_code"] < 300
                    ):
                        raise JQuantsRawValidationError(
                            "J-Quants staging page content is invalid"
                        )
                    page_hash = hashlib.sha256(
                        self._json_text({"data": data}).encode("utf-8")
                    ).hexdigest()
                    if metadata.get("content_sha256") != page_hash:
                        raise JQuantsRawValidationError(
                            "J-Quants staging page hash is invalid"
                        )
                    if data and page_hash in seen_hashes:
                        raise JQuantsRawValidationError(
                            "Duplicate J-Quants staging page detected"
                        )
                    if data:
                        seen_hashes.add(page_hash)
                    staged_hashes.append(page_hash)
                    staged_row_counts.append(len(data))
                    staged_rows += len(data)
        except (OSError, UnicodeError, json.JSONDecodeError):
            raise JQuantsRawValidationError("J-Quants staging data is unreadable") from None

        with self._connection() as connection:
            ledger_pages = connection.execute(
                """
                SELECT page_number, row_count, status_code, content_sha256
                FROM dataset_pages
                WHERE dataset_id = ? AND run_id = ?
                ORDER BY page_number
                """,
                (dataset_id, run_id),
            ).fetchall()
        if (
            len(staged_hashes) != expected_page_count
            or staged_hashes != expected_hashes
            or staged_rows != expected_row_count
            or len(ledger_pages) != expected_page_count
            or [row[0] for row in ledger_pages] != list(range(1, expected_page_count + 1))
            or [row[1] for row in ledger_pages] != staged_row_counts
            or [row[3] for row in ledger_pages] != expected_hashes
            or any(not 200 <= row[2] < 300 for row in ledger_pages)
        ):
            raise JQuantsRawValidationError("J-Quants capture and ledger metadata disagree")

    def _publish_generation(
        self,
        dataset_id: str,
        staging_directory: Path,
        kind: str,
        endpoint: str,
        conditions: Mapping[str, str],
        page_count: int,
        row_count: int,
        page_hashes: list[str],
        content_hash: str,
        state: JQuantsDatasetState,
    ) -> Path:
        committed_directory = self._dataset_committed_directory(dataset_id)
        generations_directory = committed_directory / "generations"
        generations_directory.mkdir(parents=True, exist_ok=True)
        generation_directory = generations_directory / content_hash

        if not generation_directory.exists():
            temporary_directory = Path(
                tempfile.mkdtemp(prefix=".generation-", dir=generations_directory)
            )
            try:
                shutil.copyfile(
                    staging_directory / "pages.jsonl",
                    temporary_directory / "pages.jsonl",
                )
                manifest = {
                    "schema_version": 1,
                    "dataset_id": dataset_id,
                    "generation_id": content_hash,
                    "kind": kind,
                    "endpoint": endpoint,
                    "conditions": dict(conditions),
                    "state": state.value,
                    "page_count": page_count,
                    "row_count": row_count,
                    "page_sha256": page_hashes,
                    "content_sha256": content_hash,
                    "committed_at_utc": self._timestamp(),
                    "pages_file": "pages.jsonl",
                }
                self._write_durable(
                    temporary_directory / "manifest.json",
                    (self._json_text(manifest) + "\n").encode("utf-8"),
                )
                os.replace(temporary_directory, generation_directory)
            except BaseException:
                try:
                    shutil.rmtree(temporary_directory)
                except OSError:
                    logging.getLogger(__name__).warning(
                        "J-Quants temporary generation cleanup failed",
                        extra={
                            "dataset_id": dataset_id,
                            "error_category": "GENERATION_CLEANUP_FAILED",
                        },
                    )
                raise
        else:
            self._verify_existing_generation(
                generation_directory,
                dataset_id,
                kind,
                endpoint,
                conditions,
                page_count,
                row_count,
                page_hashes,
                content_hash,
            )

        self._verify_generation(
            dataset_id,
            content_hash,
            expected_kind=kind,
            expected_endpoint=endpoint,
            expected_conditions=conditions,
            expected_page_count=page_count,
            expected_row_count=row_count,
            expected_page_hashes=page_hashes,
        )
        return generation_directory

    def _verify_existing_generation(
        self,
        generation_directory: Path,
        dataset_id: str,
        kind: str,
        endpoint: str,
        conditions: Mapping[str, str],
        page_count: int,
        row_count: int,
        page_hashes: list[str],
        content_hash: str,
    ) -> None:
        self._verify_generation(
            dataset_id,
            content_hash,
            expected_kind=kind,
            expected_endpoint=endpoint,
            expected_conditions=conditions,
            expected_page_count=page_count,
            expected_row_count=row_count,
            expected_page_hashes=page_hashes,
        )

    def _publish_pointer(self, committed_directory: Path, generation_id: str) -> None:
        temporary_path = committed_directory / f".CURRENT-{uuid.uuid4().hex}.tmp"
        try:
            self._write_durable(temporary_path, f"{generation_id}\n".encode("ascii"))
            os.replace(temporary_path, committed_directory / "CURRENT")
        finally:
            temporary_path.unlink(missing_ok=True)

    @staticmethod
    def _write_durable(path: Path, content: bytes) -> None:
        with path.open("xb") as output:
            output.write(content)
            output.flush()
            os.fsync(output.fileno())

    def _prepare_publication(
        self,
        dataset_id: str,
        run_id: str,
        state: JQuantsDatasetState,
        page_count: int,
        row_count: int,
        content_hash: str,
    ) -> None:
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT state, run_id FROM datasets WHERE dataset_id = ?",
                (dataset_id,),
            ).fetchone()
            if (
                row is None
                or row[0] != JQuantsDatasetState.RUNNING.value
                or row[1] != run_id
            ):
                raise JQuantsRawStoreError("J-Quants dataset run ownership changed")
            connection.execute(
                """
                INSERT OR REPLACE INTO pending_publications (
                    dataset_id, attempt_id, generation_id, state, page_count,
                    row_count, content_sha256
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    dataset_id,
                    run_id,
                    content_hash,
                    state.value,
                    page_count,
                    row_count,
                    content_hash,
                ),
            )

    def _finish_dataset(
        self,
        dataset_id: str,
        run_id: str,
        state: JQuantsDatasetState,
        page_count: int,
        row_count: int,
        content_hash: str,
    ) -> None:
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            pending = connection.execute(
                """
                SELECT generation_id, state, page_count, row_count, content_sha256
                FROM pending_publications
                WHERE dataset_id = ? AND attempt_id = ?
                """,
                (dataset_id, run_id),
            ).fetchone()
            if pending != (content_hash, state.value, page_count, row_count, content_hash):
                raise JQuantsRawStoreError("J-Quants publication preparation is missing")
            cursor = connection.execute(
                """
                UPDATE datasets
                SET state = ?, updated_at_utc = ?, completed_at_utc = ?,
                    page_count = ?, row_count = ?, content_sha256 = ?,
                    error_category = NULL, published_generation_id = ?,
                    post_publish_warning_category = NULL
                WHERE dataset_id = ? AND run_id = ? AND state = ?
                """,
                (
                    state.value,
                    self._timestamp(),
                    self._timestamp(),
                    page_count,
                    row_count,
                    content_hash,
                    content_hash,
                    dataset_id,
                    run_id,
                    JQuantsDatasetState.RUNNING.value,
                ),
            )
            if cursor.rowcount != 1:
                raise JQuantsRawStoreError("J-Quants dataset could not be finalized")
            connection.execute(
                """
                UPDATE dataset_attempts
                SET state = ?, updated_at_utc = ?, completed_at_utc = ?,
                    page_count = ?, row_count = ?, content_sha256 = ?,
                    error_category = NULL
                WHERE attempt_id = ? AND dataset_id = ?
                """,
                (
                    state.value,
                    self._timestamp(),
                    self._timestamp(),
                    page_count,
                    row_count,
                    content_hash,
                    run_id,
                    dataset_id,
                ),
            )
            connection.execute(
                "DELETE FROM pending_publications WHERE dataset_id = ?",
                (dataset_id,),
            )

    def _finish_empty_held(
        self,
        dataset_id: str,
        run_id: str,
        page_count: int,
        content_hash: str,
    ) -> None:
        now = self._timestamp()
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                """
                UPDATE datasets
                SET state = ?, updated_at_utc = ?, completed_at_utc = ?,
                    page_count = ?, row_count = 0, content_sha256 = ?,
                    error_category = NULL
                WHERE dataset_id = ? AND run_id = ? AND state = ?
                """,
                (
                    JQuantsDatasetState.EMPTY_HELD.value,
                    now,
                    now,
                    page_count,
                    content_hash,
                    dataset_id,
                    run_id,
                    JQuantsDatasetState.RUNNING.value,
                ),
            )
            if cursor.rowcount != 1:
                raise JQuantsRawStoreError(
                    "J-Quants empty observation could not be finalized"
                )
            connection.execute(
                """
                UPDATE dataset_attempts
                SET state = ?, updated_at_utc = ?, completed_at_utc = ?,
                    page_count = ?, row_count = 0, content_sha256 = ?,
                    error_category = NULL
                WHERE attempt_id = ? AND dataset_id = ?
                """,
                (
                    JQuantsDatasetState.EMPTY_HELD.value,
                    now,
                    now,
                    page_count,
                    content_hash,
                    run_id,
                    dataset_id,
                ),
            )

    def _has_pending_publication(self, dataset_id: str, run_id: str) -> bool:
        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT 1 FROM pending_publications
                WHERE dataset_id = ? AND attempt_id = ?
                """,
                (dataset_id, run_id),
            ).fetchone()
        return row is not None

    def _cleanup_staging(
        self, dataset_id: str, run_id: str, staging_directory: Path
    ) -> str | None:
        try:
            if staging_directory.exists():
                shutil.rmtree(staging_directory)
        except OSError:
            warning_category = "STAGING_CLEANUP_FAILED"
            logging.getLogger(__name__).warning(
                "J-Quants staging cleanup failed",
                extra={"dataset_id": dataset_id, "error_category": warning_category},
            )
            with self._connection() as connection:
                connection.execute(
                    """
                    UPDATE datasets
                    SET post_publish_warning_category = ?
                    WHERE dataset_id = ?
                    """,
                    (warning_category, dataset_id),
                )
                connection.execute(
                    """
                    UPDATE dataset_attempts SET warning_category = ?
                    WHERE attempt_id = ? AND dataset_id = ?
                    """,
                    (warning_category, run_id, dataset_id),
                )
            return warning_category
        return None

    def _fail_dataset(
        self,
        dataset_id: str,
        run_id: str,
        state: JQuantsDatasetState,
        error_category: str,
    ) -> None:
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            progress = connection.execute(
                """
                SELECT COUNT(*), COALESCE(SUM(row_count), 0)
                FROM dataset_pages
                WHERE dataset_id = ? AND run_id = ?
                """,
                (dataset_id, run_id),
            ).fetchone()
            cursor = connection.execute(
                """
                UPDATE datasets
                SET state = ?, updated_at_utc = ?, completed_at_utc = NULL,
                    page_count = ?, row_count = ?, error_category = ?
                WHERE dataset_id = ? AND run_id = ? AND state = ?
                """,
                (
                    state.value,
                    self._timestamp(),
                    progress[0],
                    progress[1],
                    error_category,
                    dataset_id,
                    run_id,
                    JQuantsDatasetState.RUNNING.value,
                ),
            )
            if cursor.rowcount == 1:
                connection.execute(
                    """
                    UPDATE dataset_attempts
                    SET state = ?, updated_at_utc = ?, completed_at_utc = ?,
                        page_count = ?, row_count = ?, error_category = ?
                    WHERE attempt_id = ? AND dataset_id = ?
                    """,
                    (
                        state.value,
                        self._timestamp(),
                        self._timestamp(),
                        progress[0],
                        progress[1],
                        error_category,
                        run_id,
                        dataset_id,
                    ),
                )

    def _page_count(self, dataset_id: str, run_id: str) -> int:
        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT COUNT(*) FROM dataset_pages
                WHERE dataset_id = ? AND run_id = ?
                """,
                (dataset_id, run_id),
            ).fetchone()
        return int(row[0]) if row is not None else 0

    def _timestamp(self) -> str:
        value = self._clock()
        if value.tzinfo is None or value.utcoffset() is None:
            raise JQuantsRawStoreError("J-Quants store clock must return an aware datetime")
        return value.astimezone(timezone.utc).isoformat(timespec="microseconds").replace(
            "+00:00", "Z"
        )

    @staticmethod
    def _validate_endpoint(endpoint: str) -> str:
        if not isinstance(endpoint, str):
            raise JQuantsRawValidationError("J-Quants endpoint is invalid")
        segments = endpoint.strip("/").split("/")
        if (
            not segments
            or any(item in {".", ".."} for item in segments)
            or any(not _SAFE_ENDPOINT_SEGMENT.fullmatch(item) for item in segments)
        ):
            raise JQuantsRawValidationError("J-Quants endpoint is invalid")
        return "/".join(segments)

    @staticmethod
    def _validate_conditions(conditions: Mapping[str, Any]) -> dict[str, str]:
        if not isinstance(conditions, Mapping):
            raise JQuantsRawValidationError("J-Quants request conditions are invalid")
        safe: dict[str, str] = {}
        for key, value in conditions.items():
            if (
                not isinstance(key, str)
                or key not in _ALLOWED_CONDITIONS
                or not isinstance(value, str)
                or not value
                or value != value.strip()
            ):
                raise JQuantsRawValidationError("J-Quants request conditions are invalid")
            if key == "code":
                if not _SAFE_CODE.fullmatch(value):
                    raise JQuantsRawValidationError("J-Quants request conditions are invalid")
            elif not _SAFE_DATE.fullmatch(value):
                raise JQuantsRawValidationError("J-Quants request conditions are invalid")
            safe[key] = value
        return dict(sorted(safe.items()))

    @staticmethod
    def _dataset_id(kind: str, endpoint: str, conditions: Mapping[str, str]) -> str:
        if not isinstance(kind, str) or kind not in {"daily", "minute"}:
            raise JQuantsRawValidationError("J-Quants dataset kind is invalid")
        identity = {
            "kind": kind,
            "endpoint": endpoint,
            "conditions": dict(conditions),
        }
        return hashlib.sha256(JQuantsRawStore._json_text(identity).encode("utf-8")).hexdigest()

    @staticmethod
    def _content_hash(
        dataset_id: str,
        kind: str,
        endpoint: str,
        conditions: Mapping[str, str],
        page_hashes: list[str],
    ) -> str:
        identity = {
            "dataset_id": dataset_id,
            "kind": kind,
            "endpoint": endpoint,
            "conditions": dict(conditions),
            "page_sha256": page_hashes,
        }
        return hashlib.sha256(JQuantsRawStore._json_text(identity).encode("utf-8")).hexdigest()

    @staticmethod
    def _json_text(value: Any) -> str:
        try:
            return json.dumps(
                value,
                ensure_ascii=False,
                allow_nan=False,
                sort_keys=True,
                separators=(",", ":"),
            )
        except (TypeError, ValueError, OverflowError):
            raise JQuantsRawValidationError("J-Quants data is not JSON serializable") from None

    @staticmethod
    def _safe_error_category(error: BaseException) -> str:
        category = getattr(error, "category", "UNEXPECTED_ERROR")
        if not isinstance(category, str) or not re.fullmatch(r"[A-Z_]{1,40}", category):
            return "UNEXPECTED_ERROR"
        return category

    @staticmethod
    def _validate_dataset_id(dataset_id: str) -> None:
        if not isinstance(dataset_id, str) or not re.fullmatch(r"[a-f0-9]{64}", dataset_id):
            raise JQuantsRawStoreError("J-Quants dataset identifier is invalid")

    def _dataset_committed_directory(self, dataset_id: str) -> Path:
        self._validate_dataset_id(dataset_id)
        return self.raw_directory / "committed" / dataset_id
