"""J-Quants raw page capture, durable ledger, and atomic dataset publication."""

import hashlib
import json
import os
import re
import shutil
import sqlite3
import tempfile
import uuid
from collections.abc import Callable, Generator, Iterable, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path
from typing import Any, BinaryIO, Protocol

from src.config import config
from src.infrastructure.market_data.jquants_v2_client import JQuantsAPIError, JQuantsPage

_SAFE_ENDPOINT_SEGMENT = re.compile(r"[A-Za-z0-9_-]+\Z")
_SAFE_CODE = re.compile(r"[A-Za-z0-9._-]{1,20}\Z")
_SAFE_DATE = re.compile(r"\d{4}-\d{2}-\d{2}\Z")
_ALLOWED_CONDITIONS = frozenset({"date", "code", "from", "to"})
_PUBLISHED_STATES = frozenset({"COMPLETE", "EMPTY_UNKNOWN"})


class JQuantsDatasetState(StrEnum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    COMPLETE = "COMPLETE"
    EMPTY_UNKNOWN = "EMPTY_UNKNOWN"
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
        self.raw_directory = Path(
            config.JQUANTS_RAW_DIRECTORY if raw_directory is None else raw_directory
        )
        self.ledger_database_file = Path(
            config.JQUANTS_LEDGER_DATABASE_FILE
            if ledger_database_file is None
            else ledger_database_file
        )
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._initialize_ledger()

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
                shutil.rmtree(staging_directory)
                self._finish_dataset(
                    dataset_id,
                    run_id,
                    state,
                    page_count,
                    row_count,
                    content_hash,
                )
                return JQuantsDatasetResult(
                    dataset_id=dataset_id,
                    state=state,
                    page_count=page_count,
                    row_count=row_count,
                    content_sha256=content_hash,
                    committed_path=committed_path,
                )
            except BaseException as error:
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

    def get_state(self, dataset_id: str) -> JQuantsDatasetState | None:
        self._validate_dataset_id(dataset_id)
        with self._connection() as connection:
            row = connection.execute(
                "SELECT state FROM datasets WHERE dataset_id = ?", (dataset_id,)
            ).fetchone()
        return None if row is None else JQuantsDatasetState(row[0])

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
        with self._connection() as connection:
            row = connection.execute(
                "SELECT state FROM datasets WHERE dataset_id = ?", (dataset_id,)
            ).fetchone()
        if row is None or row[0] not in _PUBLISHED_STATES:
            return None

        pointer_path = self._dataset_committed_directory(dataset_id) / "CURRENT"
        try:
            generation_id = pointer_path.read_text(encoding="ascii").strip()
        except FileNotFoundError:
            return None
        if not re.fullmatch(r"[a-f0-9]{64}", generation_id):
            raise JQuantsRawStoreError("J-Quants committed pointer is invalid")
        manifest_path = (
            self._dataset_committed_directory(dataset_id)
            / "generations"
            / generation_id
            / "manifest.json"
        )
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            raise JQuantsRawStoreError("J-Quants committed manifest is unavailable") from None
        if not isinstance(manifest, dict) or manifest.get("dataset_id") != dataset_id:
            raise JQuantsRawStoreError("J-Quants committed manifest is invalid")
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
                """
            )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.ledger_database_file, timeout=30)
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 30000")
        connection.execute("PRAGMA synchronous = FULL")
        return connection

    def _acquire_dataset_lock(self, dataset_id: str) -> BinaryIO:
        lock_directory = self.raw_directory / "locks"
        lock_directory.mkdir(parents=True, exist_ok=True)
        lock_path = lock_directory / f"{dataset_id}.lock"
        handle = lock_path.open("a+b")
        try:
            if lock_path.stat().st_size == 0:
                handle.write(b"\0")
                handle.flush()
            handle.seek(0)
            import msvcrt

            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            return handle
        except OSError:
            handle.close()
            raise JQuantsDatasetAlreadyRunningError(
                "J-Quants dataset is already running"
            ) from None

    @staticmethod
    def _release_dataset_lock(handle: BinaryIO) -> None:
        try:
            handle.seek(0)
            import msvcrt

            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        finally:
            handle.close()

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
                    error_category = NULL
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
                "DELETE FROM dataset_pages WHERE dataset_id = ?", (dataset_id,)
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
                shutil.rmtree(temporary_directory, ignore_errors=True)
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

        self._publish_pointer(committed_directory, content_hash)
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
        try:
            manifest = json.loads(
                (generation_directory / "manifest.json").read_text(encoding="utf-8")
            )
            rows = (generation_directory / "pages.jsonl").read_text(
                encoding="utf-8"
            ).splitlines()
            if (
                not isinstance(manifest, dict)
                or manifest.get("dataset_id") != dataset_id
                or manifest.get("generation_id") != content_hash
                or manifest.get("kind") != kind
                or manifest.get("endpoint") != endpoint
                or manifest.get("conditions") != dict(conditions)
                or manifest.get("page_count") != page_count
                or manifest.get("row_count") != row_count
                or manifest.get("page_sha256") != page_hashes
                or manifest.get("content_sha256") != content_hash
                or len(rows) != page_count
            ):
                raise JQuantsRawStoreError("J-Quants existing generation is inconsistent")
            existing_rows = 0
            for page_number, line in enumerate(rows, start=1):
                record = json.loads(line)
                response = record.get("response") if isinstance(record, dict) else None
                data = response.get("data") if isinstance(response, dict) else None
                page = record.get("page") if isinstance(record, dict) else None
                if (
                    not isinstance(data, list)
                    or not isinstance(page, dict)
                    or record.get("dataset_id") != dataset_id
                    or record.get("kind") != kind
                    or record.get("endpoint") != endpoint
                    or record.get("conditions") != dict(conditions)
                    or record.get("page_number") != page_number
                    or "pagination_key" in record
                    or page.get("content_sha256") != page_hashes[page_number - 1]
                    or hashlib.sha256(
                        self._json_text({"data": data}).encode("utf-8")
                    ).hexdigest()
                    != page_hashes[page_number - 1]
                ):
                    raise JQuantsRawStoreError("J-Quants existing generation is inconsistent")
                existing_rows += len(data)
            if existing_rows != row_count:
                raise JQuantsRawStoreError("J-Quants existing generation is inconsistent")
        except (OSError, UnicodeError, json.JSONDecodeError):
            raise JQuantsRawStoreError("J-Quants existing generation is unreadable") from None

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
            cursor = connection.execute(
                """
                UPDATE datasets
                SET state = ?, updated_at_utc = ?, completed_at_utc = ?,
                    page_count = ?, row_count = ?, content_sha256 = ?,
                    error_category = NULL
                WHERE dataset_id = ? AND run_id = ? AND state = ?
                """,
                (
                    state.value,
                    self._timestamp(),
                    self._timestamp(),
                    page_count,
                    row_count,
                    content_hash,
                    dataset_id,
                    run_id,
                    JQuantsDatasetState.RUNNING.value,
                ),
            )
            if cursor.rowcount != 1:
                raise JQuantsRawStoreError("J-Quants dataset could not be finalized")

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
            connection.execute(
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
