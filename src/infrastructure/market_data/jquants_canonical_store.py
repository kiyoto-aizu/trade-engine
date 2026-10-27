"""Versioned, date-partitioned J-Quants Parquet, independent of legacy bars."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import shutil
from time import perf_counter
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pyarrow as pa
import pyarrow.parquet as pq

from src.infrastructure.market_data.jquants_raw_store import JQuantsPublishedRaw, JQuantsRawStoreError

SCHEMA_VERSION = "1"
TRANSFORMER_VERSION = "3"
_COMMON = ("O", "H", "L", "C", "Vo", "Va")
_ADJUSTED = ("AdjFactor", "AdjO", "AdjH", "AdjL", "AdjC", "AdjVo")
CONFIRMED_NON_CANONICAL_FIELDS = frozenset({"ExRT", "MktCap"})
PARQUET_FILENAME = "part-00000.parquet"
CURRENT_HISTORY_FILENAME = "CURRENT_HISTORY.jsonl"
_JST = ZoneInfo("Asia/Tokyo")


def canonical_schema(dataset: str) -> pa.Schema:
    if dataset not in {"daily", "minute"}:
        raise ValueError("Invalid dataset")
    fields = [pa.field("Code", pa.string(), nullable=False),
              pa.field("Date", pa.date32(), nullable=False)]
    if dataset == "minute":
        fields.append(pa.field("Time", pa.timestamp("us", tz="Asia/Tokyo"), nullable=False))
    fields.extend(pa.field(name, pa.float64()) for name in _COMMON)
    if dataset == "daily":
        fields.extend(pa.field(name, pa.float64()) for name in _ADJUSTED)
        fields.extend(pa.field(name, pa.int8()) for name in ("UL", "LL"))
    return pa.schema(fields)


def strict_date(value: str) -> date:
    if not isinstance(value, str) or re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value) is None:
        raise ValueError("Date must be YYYY-MM-DD")
    return date.fromisoformat(value)


class JQuantsCanonicalError(Exception):
    category = "CANONICAL_ERROR"


class JQuantsStaleSchemaError(JQuantsCanonicalError):
    """The generation is intact but was built with another schema version."""

    category = "STALE_SCHEMA"

    def __init__(self, message: str, schema_version: str | None = None) -> None:
        super().__init__(message)
        self.schema_version = schema_version


class JQuantsQualityError(JQuantsCanonicalError):
    category = "QUALITY_ERROR"

    def __init__(self, quality: dict[str, Any]) -> None:
        super().__init__("Canonical quality validation failed")
        self.quality = dict(quality)


@dataclass(frozen=True)
class JQuantsCanonicalResult:
    generation_id: str
    manifest: Mapping[str, Any]
    reused: bool
    timings_seconds: Mapping[str, float]


def _json_bytes(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, ensure_ascii=True,
                      separators=(",", ":"), allow_nan=False).encode("ascii")


def _atomic_write(path: Path, content: bytes) -> None:
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("xb") as output:
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _record_current_switch(partition: Path, new_generation: str) -> None:
    """CURRENTを別世代へ付け替えるとき、旧CURRENTを追記専用の履歴に残す。"""
    try:
        previous = (partition / "CURRENT").read_text(encoding="ascii").strip()
    except FileNotFoundError:
        return
    if previous == new_generation:
        return
    entry = _json_bytes({
        "previous_generation_id": previous,
        "new_generation_id": new_generation,
        "switched_at_utc": datetime.now(timezone.utc).isoformat(),
    })
    with (partition / CURRENT_HISTORY_FILENAME).open("ab") as history:
        history.write(entry + b"\n")
        history.flush()
        os.fsync(history.fileno())


class JQuantsCanonicalStore:
    """Call under raw_store.ingestion_lock for this dataset/date."""

    def __init__(self, directory: Path | str, *, transformer_version: str = TRANSFORMER_VERSION):
        if not transformer_version:
            raise ValueError("Transformer version is required")
        self.directory = Path(directory)
        self.transformer_version = transformer_version

    def _partition(self, dataset: str, target_date: str) -> Path:
        canonical_schema(dataset)
        strict_date(target_date)
        return self.directory / dataset / f"date={target_date}"

    def parquet_path(self, dataset: str, target_date: str, generation_id: str) -> Path:
        return self._partition(dataset, target_date) / "generations" / generation_id / PARQUET_FILENAME

    def current_manifest(self, dataset: str, target_date: str) -> dict[str, Any] | None:
        partition = self._partition(dataset, target_date)
        try:
            generation = (partition / "CURRENT").read_text(encoding="ascii").strip()
        except FileNotFoundError:
            return None
        return self._verify(partition, generation, dataset, target_date)

    def read_attempt(self, dataset: str, target_date: str) -> dict[str, Any] | None:
        """当日の ATTEMPT.json（直近の変換試行）を読む。無ければNone。"""
        path = self._partition(dataset, target_date) / "ATTEMPT.json"
        try:
            return json.loads(path.read_text(encoding="ascii"))
        except FileNotFoundError:
            return None

    def _verify(
        self, partition: Path, generation: str, dataset: str, target_date: str
    ) -> dict[str, Any]:
        if not re.fullmatch(r"[a-f0-9]{64}", generation):
            raise JQuantsCanonicalError("Invalid canonical generation")
        folder = partition / "generations" / generation
        manifest = json.loads((folder / "manifest.json").read_text(encoding="ascii"))
        parquet = folder / PARQUET_FILENAME
        identity = {key: manifest[key] for key in (
            "dataset", "date", "raw_generation_id", "raw_content_sha256",
            "schema_version", "transformer_version",
        )}
        if (
            hashlib.sha256(_json_bytes(identity)).hexdigest() != generation
            or manifest["generation_id"] != generation
            or manifest["dataset"] != dataset or manifest["date"] != target_date
            or manifest["parquet_sha256"] != _hash_file(parquet)
            or manifest["size_bytes"] != parquet.stat().st_size
        ):
            raise JQuantsCanonicalError("Canonical provenance or file hash mismatch")
        if manifest["schema_version"] != SCHEMA_VERSION:
            raise JQuantsStaleSchemaError(
                "Canonical generation was built with another schema version; rebuild required",
                manifest["schema_version"],
            )
        reader = pq.ParquetFile(parquet)
        if (
            reader.metadata.num_rows != manifest["row_count"]
            or not reader.schema_arrow.equals(canonical_schema(dataset), check_metadata=False)
            or manifest["quality"]["invalid_rows"]
            or manifest["quality"]["exact_duplicates"]
            or manifest["quality"]["conflicting_duplicates"]
            or manifest["quality"]["input_rows"] != manifest["row_count"]
        ):
            raise JQuantsCanonicalError("Canonical schema or counts mismatch")
        return manifest

    def transform(
        self, raw: JQuantsPublishedRaw, *, dataset: str, target_date: str
    ) -> JQuantsCanonicalResult:
        partition = self._partition(dataset, target_date)
        partition.mkdir(parents=True, exist_ok=True)
        identity = {
            "dataset": dataset, "date": target_date,
            "raw_generation_id": raw.manifest["generation_id"],
            "raw_content_sha256": raw.manifest["content_sha256"],
            "schema_version": SCHEMA_VERSION, "transformer_version": self.transformer_version,
        }
        if raw.manifest["kind"] != dataset or raw.manifest["conditions"] != {"date": target_date}:
            raise JQuantsCanonicalError("Raw dataset identity mismatch")
        generation = hashlib.sha256(_json_bytes(identity)).hexdigest()
        destination = partition / "generations" / generation
        attempt = partition / "ATTEMPT.json"
        _atomic_write(attempt, _json_bytes({**identity, "state": "RUNNING"}))
        staging = partition / "staging" / uuid.uuid4().hex
        conversion_seconds = 0.0
        verification_seconds = 0.0
        try:
            if destination.exists():
                verification_start = perf_counter()
                manifest = self._verify(partition, generation, dataset, target_date)
                verification_seconds += perf_counter() - verification_start
                reused = True
            else:
                staging.mkdir(parents=True)
                conversion_start = perf_counter()
                manifest = self._convert(raw, dataset, target_date, staging, identity, generation)
                conversion_seconds += perf_counter() - conversion_start
                destination.parent.mkdir(exist_ok=True)
                os.replace(staging, destination)
                verification_start = perf_counter()
                manifest = self._verify(partition, generation, dataset, target_date)
                verification_seconds += perf_counter() - verification_start
                reused = False
            result = JQuantsCanonicalResult(
                generation, manifest, reused,
                {"conversion": conversion_seconds, "verification": verification_seconds},
            )
            # All validation precedes the only visibility-changing operation.
            _atomic_write(attempt, _json_bytes({**identity, "state": "VALIDATED",
                                              "generation_id": generation}))
            _record_current_switch(partition, generation)
            _atomic_write(partition / "CURRENT", generation.encode("ascii"))
            return result
        except (
            JQuantsCanonicalError, JQuantsRawStoreError, OSError, ValueError,
            KeyError, TypeError, pa.ArrowException,
        ) as error:
            failure: dict[str, Any] = {**identity, "state": "FAILED",
                                      "category": "CANONICAL_ERROR"}
            if isinstance(error, JQuantsQualityError):
                failure.update(category=error.category, quality=error.quality)
            _atomic_write(attempt, _json_bytes(failure))
            if isinstance(error, (JQuantsCanonicalError, JQuantsRawStoreError)):
                raise
            raise JQuantsCanonicalError("Canonical conversion or publication failed") from None
        finally:
            if staging.exists():
                shutil.rmtree(staging)

    def _convert(
        self, raw: JQuantsPublishedRaw, dataset: str, target_date: str,
        staging: Path, identity: dict[str, str], generation: str,
    ) -> dict[str, Any]:
        schema = canonical_schema(dataset)
        quality: dict[str, Any] = dict(
            input_rows=0, invalid_rows=0, exact_duplicates=0,
            conflicting_duplicates=0, unknown_field_count=0,
            rows_with_unknown_fields=0,
        )
        seen: dict[tuple[str, ...], bytes] = {}
        codes: set[str] = set()
        unsupported: set[str] = set()
        observed_fields: set[str] = set()
        field_observations: dict[str, dict[str, int]] = {}
        parquet = staging / PARQUET_FILENAME
        with pq.ParquetWriter(parquet, schema, compression="zstd") as writer:
            for page in raw.iter_pages():
                batch: list[dict[str, Any]] = []
                for record in page:
                    quality["input_rows"] += 1
                    extras = set(record) - set(schema.names)
                    observed_fields.update(extras)
                    unapproved = extras - CONFIRMED_NON_CANONICAL_FIELDS
                    unsupported.update(unapproved)
                    quality["rows_with_unknown_fields"] += bool(unapproved)
                    for name in extras:
                        counts = field_observations.setdefault(
                            name, {"count": 0, "null_count": 0}
                        )
                        counts["count"] += 1
                        counts["null_count"] += record[name] is None
                    try:
                        row = _convert_row(record, dataset, target_date)
                    except (ValueError, TypeError, OverflowError):
                        quality["invalid_rows"] += 1
                        continue
                    key = (record["Code"], record["Date"])
                    if dataset == "minute":
                        key += (record["Time"],)
                    digest = hashlib.sha256(_json_bytes(record)).digest()
                    if key in seen:
                        counter = ("exact_duplicates" if seen[key] == digest
                                   else "conflicting_duplicates")
                        quality[counter] += 1
                        continue
                    seen[key] = digest
                    codes.add(row["Code"])
                    batch.append(row)
                    if len(batch) == 8192:
                        writer.write_table(pa.Table.from_pylist(batch, schema=schema))
                        batch.clear()
                if batch:
                    writer.write_table(pa.Table.from_pylist(batch, schema=schema))
        quality["unknown_field_count"] = len(unsupported)
        if quality["input_rows"] != raw.manifest["row_count"]:
            quality["invalid_rows"] += 1
        if any(quality[name] for name in (
            "invalid_rows", "exact_duplicates", "conflicting_duplicates"
        )):
            raise JQuantsQualityError(quality)
        with parquet.open("r+b") as durable:
            durable.flush()
            os.fsync(durable.fileno())
        manifest = {
            **identity, "generation_id": generation,
            "created_at_utc": datetime.now(timezone.utc).isoformat(),
            "row_count": quality["input_rows"], "symbol_count": len(codes),
            "unsupported_fields": sorted(unsupported),
            "noncanonical_fields": sorted(observed_fields),
            "field_observations": field_observations,
            "quality": quality, "size_bytes": parquet.stat().st_size,
            "parquet_sha256": _hash_file(parquet),
        }
        _atomic_write(staging / "manifest.json", _json_bytes(manifest))
        return manifest


def _convert_row(record: Mapping[str, Any], dataset: str, target_date: str) -> dict[str, Any]:
    required = {"Code", "Date", *_COMMON}
    required.update((*_ADJUSTED, "UL", "LL") if dataset == "daily" else ("Time",))
    if not required.issubset(record):
        raise ValueError("Missing required fields")
    code = record["Code"]
    if not isinstance(code, str) or re.fullmatch(r"[A-Za-z0-9]{4,5}", code) is None:
        raise ValueError("Invalid Code")
    if record["Date"] != target_date:
        raise ValueError("Record date mismatch")
    row: dict[str, Any] = {"Code": code, "Date": strict_date(record["Date"])}
    if dataset == "minute":
        value = record["Time"]
        if not isinstance(value, str) or re.fullmatch(r"[0-9]{2}:[0-9]{2}", value) is None:
            raise ValueError("Invalid interval start time")
        row["Time"] = datetime.strptime(f"{target_date} {value}", "%Y-%m-%d %H:%M").replace(tzinfo=_JST)
    for name in (*_COMMON, *(_ADJUSTED if dataset == "daily" else ())):
        value = record[name]
        if value is not None:
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValueError("Invalid numeric type")
            value = float(value)
            if not math.isfinite(value) or value < 0 or (name == "AdjFactor" and value == 0):
                raise ValueError("Invalid numeric value")
        row[name] = value
    if dataset == "daily":
        for name in ("UL", "LL"):
            value = record[name]
            if not isinstance(value, str) or value not in ("0", "1"):
                raise ValueError("Invalid price limit flag")
            row[name] = int(value)
    for prefix in ("", "Adj") if dataset == "daily" else ("",):
        high, low = row[prefix + "H"], row[prefix + "L"]
        for name in ("O", "H", "L", "C"):
            value = row[prefix + name]
            if value is not None and (
                (high is not None and value > high) or (low is not None and value < low)
            ):
                raise ValueError("Invalid OHLC relationship")
    return row
