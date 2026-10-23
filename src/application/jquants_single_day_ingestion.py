"""Shared single-day acquisition and canonical conversion use case."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import asdict, dataclass, field
import sqlite3
from time import perf_counter
from typing import Protocol, Any

from src.infrastructure.market_data.jquants_canonical_store import (
    JQuantsCanonicalError,
    JQuantsCanonicalStore,
    JQuantsQualityError,
    strict_date,
)
from src.infrastructure.market_data.jquants_raw_store import (
    JQuantsDatasetState,
    JQuantsRawStore,
    JQuantsRawStoreError,
)
from src.infrastructure.market_data.jquants_v2_client import (
    JQuantsAPIError, JQuantsPage, JQuantsV2Client,
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
    warnings: list[str] = field(default_factory=list)
    error_category: str | None = None

    @property
    def exit_code(self) -> int:
        return {"COMPLETE": 0, "ATTENTION": 2, "FAILED": 1}[self.status]

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


class JQuantsSingleDayIngestion:
    def __init__(
        self, raw_store: JQuantsRawStore, canonical_store: JQuantsCanonicalStore,
        *, client_factory: Callable[[], _Client] = JQuantsV2Client,
    ) -> None:
        self.raw_store = raw_store
        self.canonical_store = canonical_store
        self.client_factory = client_factory

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
        try:
            with self.raw_store.ingestion_lock(
                kind=dataset, endpoint=endpoint, conditions=conditions
            ) as dataset_id:
                self.raw_store.recover_dataset(dataset_id)
                verification_start = perf_counter()
                try:
                    raw = self.raw_store.resolve_published(
                        kind=dataset, endpoint=endpoint, conditions=conditions
                    )
                    previous = self.canonical_store.current_manifest(dataset, date)
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
                    if captured.warning_category:
                        summary.warnings.append(captured.warning_category)
                    summary.raw_updated = (
                        captured.state == JQuantsDatasetState.COMPLETE
                        and (raw is None or captured.content_sha256 != raw.manifest["generation_id"])
                    )
                    verification_start = perf_counter()
                    try:
                        raw = self.raw_store.resolve_published(
                            kind=dataset, endpoint=endpoint, conditions=conditions
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
                if summary.raw_state in {"EMPTY_HELD", "FAILED", "PARTIAL", "RUNNING"}:
                    summary.using_previous_raw = True
                    summary.warnings.append("PREVIOUS_PUBLISHED_RAW_USED")
                    summary.warnings.append(f"RAW_ATTEMPT_{summary.raw_state}")
                conversion_start = perf_counter()
                try:
                    canonical = self.canonical_store.transform(raw, dataset=dataset, target_date=date)
                except (JQuantsCanonicalError, JQuantsRawStoreError, OSError):
                    # A pointer replacement may have succeeded before an I/O error.
                    current = self.canonical_store.current_manifest(dataset, date)
                    summary.canonical_generation_id = (
                        None if current is None else current["generation_id"]
                    )
                    if previous is not None:
                        summary.warnings.append("PREVIOUS_CANONICAL_GENERATION_RETAINED")
                    raise
                finally:
                    summary.durations_seconds["conversion"] = perf_counter() - conversion_start
                manifest = canonical.manifest
                summary.canonical_generation_id = canonical.generation_id
                summary.canonical_reused = canonical.reused
                summary.row_count = manifest["row_count"]
                summary.symbol_count = manifest["symbol_count"]
                summary.canonical_size_bytes = manifest["size_bytes"]
                summary.quality = dict(manifest["quality"])
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
        except (OSError, ValueError, TypeError, KeyError, sqlite3.Error):
            summary.status = "FAILED"
            summary.error_category = "STORAGE_OR_FORMAT_ERROR"
        finally:
            for stage in ("acquisition", "verification", "conversion"):
                summary.durations_seconds.setdefault(stage, 0.0)
            summary.durations_seconds["total"] = perf_counter() - total_start
        return summary
