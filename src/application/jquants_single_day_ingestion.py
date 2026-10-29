"""Shared single-day acquisition and canonical conversion use case."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
import json
import sqlite3
import tracemalloc
from time import perf_counter
from pathlib import Path
from typing import Protocol, Any
import uuid

import pyarrow as pa
import pyarrow.parquet as pq

from src.application.jquants_metrics_evaluation import safe_codes
from src.infrastructure.market_data.jquants_canonical_store import (
    JQuantsCanonicalError,
    JQuantsCanonicalStore,
    JQuantsQualityError,
    JQuantsStaleSchemaError,
    strict_date,
)
from src.infrastructure.market_data.jquants_disk_space import measure_volume
from src.infrastructure.market_data.jquants_raw_store import (
    JQuantsDatasetState,
    JQuantsRawStore,
    JQuantsRawStoreError,
)
from src.infrastructure.market_data.jquants_v2_client import (
    JQuantsAPIError, JQuantsPage, JQuantsRateLimitAbortError, JQuantsV2Client,
)


class _Client(Protocol):
    def iter_pages(
        self, endpoint: str, params: Mapping[str, Any] | None = None
    ) -> Iterable[JQuantsPage]: ...

    def close(self) -> None: ...


@dataclass
class JQuantsIngestionSummary:
    dataset: str
    date: str
    status: str = "FAILED"
    raw_state: str | None = None
    raw_generation_id: str | None = None
    canonical_generation_id: str | None = None
    previous_canonical_generation_id: str | None = None
    raw_source: str = "none"
    canonical_reused: bool = False
    raw_updated: bool = False
    using_previous_raw: bool = False
    page_count: int = 0
    row_count: int = 0
    symbol_count: int = 0
    raw_size_bytes: int = 0
    canonical_size_bytes: int = 0
    quality: dict[str, int] = field(default_factory=dict)
    durations_seconds: dict[str, float] = field(default_factory=dict)
    execution_metrics: dict[str, Any] = field(default_factory=dict)
    metrics_file: str | None = None
    warnings: list[str] = field(default_factory=list)
    error_category: str | None = None

    @property
    def exit_code(self) -> int:
        if self.error_category == "RATE_LIMIT_ABORTED":
            return 3
        return {"COMPLETE": 0, "ATTENTION": 2, "FAILED": 1}[self.status]

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


class JQuantsSingleDayIngestion:
    def __init__(
        self, raw_store: JQuantsRawStore, canonical_store: JQuantsCanonicalStore,
        *, client_factory: Callable[[], _Client] = JQuantsV2Client,
        metrics_directory: Path | str | None = None,
        measure_peak_memory: bool = False,
        notifier: Callable[[str], bool] | None = None,
        volume_probe: Callable[[Path | str], tuple[int, int]] = measure_volume,
    ) -> None:
        self.notifier = notifier
        self.volume_probe = volume_probe
        self.raw_store = raw_store
        self.canonical_store = canonical_store
        self.client_factory = client_factory
        self.metrics_directory = None if metrics_directory is None else Path(metrics_directory)
        self.measure_peak_memory = measure_peak_memory

    def _current_or_none(self, dataset: str, date: str) -> dict[str, Any] | None:
        """旧版のCURRENTは保持対象の現行世代として扱わない（版を上げた後の作り直しを妨げない）。"""
        try:
            return self.canonical_store.current_manifest(dataset, date)
        except JQuantsStaleSchemaError:
            return None

    def run(
        self, *, dataset: str, date: str, refresh: bool = False, canonical_only: bool = False
    ) -> JQuantsIngestionSummary:
        if dataset not in {"daily", "minute"}:
            raise ValueError("Dataset must be daily or minute")
        strict_date(date)
        if refresh and canonical_only:
            raise ValueError("Refresh and canonical-only are mutually exclusive")
        summary = JQuantsIngestionSummary(dataset, date)
        endpoint = f"equities/bars/{dataset}"
        conditions = {"date": date}
        total_start = perf_counter()
        started_tracemalloc = self.measure_peak_memory and not tracemalloc.is_tracing()
        if started_tracemalloc:
            tracemalloc.start()
        telemetry: dict[str, Any] = {
            "request_count": "未計測",
            "request_page_count": "未計測",
            "http_status_counts": "未計測",
            "retry_count": "未計測",
            "rate_limit_429_count": "未計測",
            "rate_limit_aborted": False,
            "abort_reason": None,
            "wait_seconds": "未計測",
            "raw_validation_seconds": 0.0,
            "canonical_conversion_seconds": 0.0,
            "canonical_validation_seconds": 0.0,
            "field_observations": {},
            "canonical_comparison": {"status": "NOT_CHECKED"},
            **self._measure_disk_space(),
        }
        try:
            with self.raw_store.ingestion_lock(
                kind=dataset, endpoint=endpoint, conditions=conditions
            ) as dataset_id:
                raw_validation_start = perf_counter()
                try:
                    self.raw_store.recover_dataset(dataset_id)
                finally:
                    telemetry["raw_validation_seconds"] += (
                        perf_counter() - raw_validation_start
                    )
                verification_start = perf_counter()
                try:
                    raw_validation_start = perf_counter()
                    try:
                        raw = self.raw_store.resolve_published(
                            kind=dataset, endpoint=endpoint, conditions=conditions
                        )
                    finally:
                        telemetry["raw_validation_seconds"] += (
                            perf_counter() - raw_validation_start
                        )
                    previous = self._current_or_none(dataset, date)
                    if previous is not None:
                        summary.canonical_generation_id = previous["generation_id"]
                        summary.previous_canonical_generation_id = previous["generation_id"]
                finally:
                    summary.durations_seconds["verification"] = perf_counter() - verification_start
                if raw is not None:
                    summary.raw_generation_id = raw.manifest["generation_id"]
                    summary.raw_source = "existing"
                if raw is None and canonical_only:
                    raise JQuantsCanonicalError("Canonical-only requires published raw")
                if raw is None or refresh:
                    summary.raw_source = "fetch"
                    acquisition_start = perf_counter()
                    client: _Client | None = None
                    try:
                        client = self.client_factory()
                        try:
                            captured = self.raw_store.capture(
                                client, kind=dataset, endpoint=endpoint, conditions=conditions
                            )
                        finally:
                            client.close()
                    except (JQuantsRawStoreError, JQuantsAPIError, OSError):
                        summary.using_previous_raw = raw is not None
                        if raw is not None:
                            summary.warnings.append("FETCH_FAILED_PREVIOUS_RAW_RETAINED")
                        raise
                    finally:
                        summary.durations_seconds["acquisition"] = perf_counter() - acquisition_start
                        state = self.raw_store.get_state(dataset_id)
                        summary.raw_state = None if state is None else state.value
                        if client is not None:
                            client_metrics = getattr(client, "metrics", None)
                            if client_metrics is not None and hasattr(client_metrics, "as_dict"):
                                request_metrics = client_metrics.as_dict()
                                telemetry.update({
                                    "request_count": request_metrics["request_count"],
                                    "request_page_count": request_metrics["page_count"],
                                    "http_status_counts": request_metrics["status_counts"],
                                    "retry_count": request_metrics["retry_count"],
                                    "rate_limit_429_count": request_metrics["rate_limit_429_count"],
                                    "wait_seconds": request_metrics["wait_seconds"],
                                })
                    if captured.warning_category:
                        summary.warnings.append(captured.warning_category)
                    telemetry["raw_validation_seconds"] += captured.validation_seconds
                    summary.durations_seconds["acquisition"] = max(
                        0.0,
                        summary.durations_seconds["acquisition"] - captured.validation_seconds,
                    )
                    summary.raw_updated = (
                        captured.state == JQuantsDatasetState.COMPLETE
                        and (raw is None or captured.content_sha256 != raw.manifest["generation_id"])
                    )
                    verification_start = perf_counter()
                    try:
                        raw_validation_start = perf_counter()
                        try:
                            raw = self.raw_store.resolve_published(
                                kind=dataset, endpoint=endpoint, conditions=conditions
                            )
                        finally:
                            telemetry["raw_validation_seconds"] += (
                                perf_counter() - raw_validation_start
                            )
                    finally:
                        summary.durations_seconds["verification"] += perf_counter() - verification_start
                else:
                    state = self.raw_store.get_state(dataset_id)
                    summary.raw_state = None if state is None else state.value
                if raw is None:
                    raise JQuantsRawStoreError("No verified raw publication")
                summary.raw_generation_id = raw.manifest["generation_id"]
                summary.page_count = raw.manifest["page_count"]
                summary.raw_size_bytes = raw.pages_path.stat().st_size
                if summary.raw_source == "existing":
                    telemetry["request_count"] = 0
                    telemetry["request_page_count"] = 0
                    telemetry["http_status_counts"] = {}
                    telemetry["retry_count"] = 0
                    telemetry["rate_limit_429_count"] = 0
                    telemetry["wait_seconds"] = 0.0
                if summary.raw_state in {"EMPTY_HELD", "FAILED", "PARTIAL", "RUNNING"}:
                    summary.using_previous_raw = True
                    summary.warnings.append("PREVIOUS_PUBLISHED_RAW_USED")
                    summary.warnings.append(f"RAW_ATTEMPT_{summary.raw_state}")
                conversion_start = perf_counter()
                try:
                    canonical = self.canonical_store.transform(raw, dataset=dataset, target_date=date)
                except (JQuantsCanonicalError, JQuantsRawStoreError, OSError):
                    # A pointer replacement may have succeeded before an I/O error.
                    current = self._current_or_none(dataset, date)
                    summary.canonical_generation_id = (
                        None if current is None else current["generation_id"]
                    )
                    if previous is not None:
                        summary.warnings.append("PREVIOUS_CANONICAL_GENERATION_RETAINED")
                    raise
                finally:
                    summary.durations_seconds["conversion"] = perf_counter() - conversion_start
                manifest = canonical.manifest
                telemetry["canonical_conversion_seconds"] = canonical.timings_seconds["conversion"]
                telemetry["canonical_validation_seconds"] = canonical.timings_seconds["verification"]
                summary.canonical_generation_id = canonical.generation_id
                summary.canonical_reused = canonical.reused
                summary.row_count = manifest["row_count"]
                summary.symbol_count = manifest["symbol_count"]
                summary.canonical_size_bytes = manifest["size_bytes"]
                summary.quality = dict(manifest["quality"])
                telemetry["field_observations"] = manifest.get("field_observations", {})
                if not summary.row_count:
                    summary.warnings.append("EMPTY_RESULT_MEANING_UNKNOWN")
                if summary.quality["unknown_field_count"]:
                    summary.warnings.append("UNSUPPORTED_FIELDS_RETAINED_IN_RAW")
                summary.status = "ATTENTION" if summary.warnings else "COMPLETE"
        except JQuantsQualityError as error:
            summary.status = "FAILED"
            summary.quality = error.quality
            summary.error_category = error.category
        except (JQuantsRawStoreError, JQuantsAPIError, JQuantsCanonicalError) as error:
            summary.status = "FAILED"
            summary.error_category = error.category
            if isinstance(error, JQuantsRateLimitAbortError):
                telemetry["rate_limit_aborted"] = True
                telemetry["abort_reason"] = "RATE_LIMIT_429_CAP"
                self._notify_rate_limit_abort(summary, telemetry)
        except (OSError, ValueError, TypeError, KeyError, sqlite3.Error):
            summary.status = "FAILED"
            summary.error_category = "STORAGE_OR_FORMAT_ERROR"
        finally:
            for stage in ("acquisition", "verification", "conversion"):
                summary.durations_seconds.setdefault(stage, 0.0)
            if telemetry["canonical_comparison"]["status"] == "NOT_CHECKED":
                telemetry["canonical_comparison"] = self._compare_daily_and_minute(date)
            summary.durations_seconds["total"] = perf_counter() - total_start
            if started_tracemalloc:
                _, peak_memory = tracemalloc.get_traced_memory()
                tracemalloc.stop()
                telemetry["peak_memory_bytes"] = peak_memory
            else:
                telemetry["peak_memory_bytes"] = "未計測"
            telemetry["page_count"] = summary.page_count
            telemetry["published_page_count"] = summary.page_count
            telemetry["durations_seconds"] = {
                "acquisition": summary.durations_seconds["acquisition"],
                "raw_validation": telemetry["raw_validation_seconds"],
                "conversion": telemetry["canonical_conversion_seconds"],
                "canonical_validation": telemetry["canonical_validation_seconds"],
                "total": summary.durations_seconds["total"],
            }
            telemetry["raw_size_bytes"] = summary.raw_size_bytes
            telemetry["canonical_size_bytes"] = summary.canonical_size_bytes
            summary.execution_metrics = telemetry
            if self.metrics_directory is not None:
                summary.metrics_file = str(self._write_metrics(summary))
        return summary

    def _measure_disk_space(self) -> dict[str, Any]:
        """Free space before the run (doc 18 §4-2 #5). Numbers only; unmeasured on failure."""
        try:
            raw_free, raw_device = self.volume_probe(self.raw_store.raw_directory)
            canonical_free, canonical_device = self.volume_probe(self.canonical_store.directory)
        except OSError:
            return {}
        return {
            "disk_free_bytes_raw": raw_free,
            "disk_free_bytes_canonical": canonical_free,
            "disk_same_volume": raw_device == canonical_device,
        }

    @staticmethod
    def _enumerated_warnings(warnings: list[str]) -> list[str]:
        codes = safe_codes(warnings) or []
        if len(set(warnings)) != len(codes):
            codes = sorted({*codes, "INVALID_WARNING_DROPPED"})
        return codes

    @staticmethod
    def _unknown_field_count(quality: Mapping[str, Any]) -> int | None:
        value = quality.get("unknown_field_count") if isinstance(quality, Mapping) else None
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            return None
        return value

    def _notify_rate_limit_abort(
        self, summary: JQuantsIngestionSummary, telemetry: Mapping[str, Any]
    ) -> None:
        """通知の失敗で取込結果を変えない。本文はdataset・日付・429回数のみ。"""
        if self.notifier is None:
            return
        message = (
            "[J-Quants] 429 累計上限で取得を打ち切りました "
            f"dataset={summary.dataset} date={summary.date} "
            f"429回数={telemetry.get('rate_limit_429_count')}。"
            "原因(リミッター設定・プラン上限・別プロセスとの競合)を確認してから --refresh で再実行してください。"
        )
        try:
            sent = self.notifier(message)
        except Exception:
            sent = False
        if not sent:
            summary.warnings.append("SLACK_NOTIFY_FAILED")

    def _write_metrics(self, summary: JQuantsIngestionSummary) -> Path:
        assert self.metrics_directory is not None
        directory = self.metrics_directory / summary.dataset / f"date={summary.date}"
        directory.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        path = directory / f"{timestamp}-{uuid.uuid4().hex}.json"
        payload = {
            "dataset": summary.dataset,
            "date": summary.date,
            **summary.execution_metrics,
            "status": summary.status,
            # Enumerated codes and counts only; never symbol codes or record values.
            "error_category": summary.error_category,
            "warnings": self._enumerated_warnings(summary.warnings),
            # None means "not measured" (e.g. the run stopped before canonical conversion).
            "symbol_count": summary.symbol_count if summary.canonical_generation_id else None,
            "unknown_field_count": self._unknown_field_count(summary.quality),
            "raw_source": summary.raw_source,
        }
        temporary = path.with_suffix(".tmp")
        try:
            with temporary.open("x", encoding="utf-8") as output:
                json.dump(payload, output, ensure_ascii=True, sort_keys=True, allow_nan=False)
                output.write("\n")
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)
        return path

    def _compare_daily_and_minute(self, date: str) -> dict[str, Any]:
        try:
            manifests = {
                dataset: self.canonical_store.current_manifest(dataset, date)
                for dataset in ("daily", "minute")
            }
            if any(manifest is None for manifest in manifests.values()):
                return {"status": "NOT_BOTH_PUBLISHED"}
            daily_manifest = manifests["daily"]
            minute_manifest = manifests["minute"]
            assert daily_manifest is not None and minute_manifest is not None
            daily_path = self.canonical_store.parquet_path(
                "daily", date, daily_manifest["generation_id"]
            )
            minute_path = self.canonical_store.parquet_path(
                "minute", date, minute_manifest["generation_id"]
            )
            daily = pq.read_table(daily_path, columns=["Code", "Vo", "O", "H", "L", "C"])
            minute = pq.read_table(minute_path, columns=["Code"])
            daily_codes = daily.column("Code").to_pylist()
            daily_volumes = daily.column("Vo").to_pylist()
            daily_positive_codes = {
                code for code, volume in zip(daily_codes, daily_volumes)
                if volume is not None and volume > 0
            }
            daily_rows = {
                code: values for code, *values in zip(
                    daily_codes,
                    daily.column("O").to_pylist(),
                    daily.column("H").to_pylist(),
                    daily.column("L").to_pylist(),
                    daily.column("C").to_pylist(),
                )
            }
            minute_codes = set(minute.column("Code").to_pylist())
            daily_only_codes = daily_positive_codes - minute_codes
            minute_only_codes = minute_codes - daily_positive_codes

            def ohlc_all_null(code: str) -> bool | None:
                values = daily_rows.get(code)
                return None if values is None else all(value is None for value in values)

            return {
                "status": "AVAILABLE",
                "daily_positive_volume_symbol_count": len(daily_positive_codes),
                "minute_symbol_count": len(minute_codes),
                "daily_only_symbol_count": len(daily_only_codes),
                "minute_only_symbol_count": len(minute_only_codes),
                "daily_only_ohlc_all_null_count": sum(
                    ohlc_all_null(code) is True for code in daily_only_codes
                ),
                "daily_only_ohlc_not_all_null_count": sum(
                    ohlc_all_null(code) is False for code in daily_only_codes
                ),
                "minute_only_daily_ohlc_all_null_count": sum(
                    ohlc_all_null(code) is True for code in minute_only_codes
                ),
                "minute_only_daily_ohlc_not_all_null_count": sum(
                    ohlc_all_null(code) is False for code in minute_only_codes
                ),
                "minute_only_daily_record_missing_count": sum(
                    ohlc_all_null(code) is None for code in minute_only_codes
                ),
                "publication_gate": False,
            }
        except (JQuantsCanonicalError, OSError, ValueError, KeyError, pa.ArrowException):
            return {
                "status": "ERROR",
                "error_category": "CANONICAL_COMPARISON_ERROR",
                "publication_gate": False,
            }
