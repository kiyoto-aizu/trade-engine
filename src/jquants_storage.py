"""Read-only validation of effective J-Quants storage destinations.

Kept outside src.config because that package imports credential/dotenv settings.
"""

from __future__ import annotations

import ntpath
import os
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath
from typing import Mapping, Protocol

EXPECTED_OPERATIONAL_DRIVE = "D:"
_REPOSITORY_ROOT = Path(os.path.abspath(__file__)).parent.parent


class JQuantsStorageSettings(Protocol):
    JQUANTS_RAW_DIRECTORY: Path | str
    JQUANTS_CANONICAL_DIRECTORY: Path | str
    JQUANTS_LEDGER_DATABASE_FILE: Path | str
    JQUANTS_EXECUTION_METRICS_DIRECTORY: Path | str
    JQUANTS_BACKFILL_STATE_DIRECTORY: Path | str
    JQUANTS_CALENDAR_DIRECTORY: Path | str


@dataclass(frozen=True)
class StorageDestination:
    name: str
    setting: str
    kind: str
    source: str
    path: str
    drive: str
    is_absolute: bool
    matches_operational_drive: bool

    def as_dict(self) -> dict[str, str | bool]:
        return {
            "name": self.name,
            "setting": self.setting,
            "kind": self.kind,
            "source": self.source,
            "path": self.path,
            "drive": self.drive,
            "is_absolute": self.is_absolute,
            "matches_operational_drive": self.matches_operational_drive,
        }


@dataclass(frozen=True)
class JQuantsStorageAudit:
    destinations: tuple[StorageDestination, ...]

    @property
    def is_valid(self) -> bool:
        return all(
            item.source == "environment" and item.matches_operational_drive
            for item in self.destinations
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "status": "PASS" if self.is_valid else "FAILED",
            "expected_drive": EXPECTED_OPERATIONAL_DRIVE,
            "destinations": [item.as_dict() for item in self.destinations],
        }


def _normalize_destination(value: Path | str) -> tuple[str, str, bool]:
    raw_path = os.fspath(value)
    windows_path = PureWindowsPath(raw_path)
    if windows_path.drive:
        normalized = ntpath.normpath(raw_path)
        normalized_windows_path = PureWindowsPath(normalized)
        return (
            normalized,
            normalized_windows_path.drive.upper(),
            normalized_windows_path.is_absolute(),
        )

    if windows_path.root and os.name == "nt":
        return ntpath.normpath(raw_path), "", False

    is_absolute = os.path.isabs(raw_path)
    normalized = os.path.abspath(raw_path)
    return normalized, PureWindowsPath(normalized).drive.upper(), is_absolute


def default_jquants_storage_settings() -> JQuantsStorageSettings:
    root = _REPOSITORY_ROOT / "data" / "jquants"
    return _StorageSettings(
        JQUANTS_RAW_DIRECTORY=root / "raw",
        JQUANTS_CANONICAL_DIRECTORY=root / "canonical",
        JQUANTS_LEDGER_DATABASE_FILE=root / "ledger.sqlite3",
        JQUANTS_EXECUTION_METRICS_DIRECTORY=root / "metrics",
        JQUANTS_BACKFILL_STATE_DIRECTORY=root / "backfill_state",
        JQUANTS_CALENDAR_DIRECTORY=root / "calendar",
    )


@dataclass(frozen=True)
class _StorageSettings:
    JQUANTS_RAW_DIRECTORY: Path
    JQUANTS_CANONICAL_DIRECTORY: Path
    JQUANTS_LEDGER_DATABASE_FILE: Path
    JQUANTS_EXECUTION_METRICS_DIRECTORY: Path
    JQUANTS_BACKFILL_STATE_DIRECTORY: Path
    JQUANTS_CALENDAR_DIRECTORY: Path


def inspect_jquants_storage(
    settings: JQuantsStorageSettings,
    environ: Mapping[str, str],
) -> JQuantsStorageAudit:
    configured_targets = (
        ("raw", "JQUANTS_RAW_DIRECTORY", settings.JQUANTS_RAW_DIRECTORY, "directory"),
        (
            "canonical",
            "JQUANTS_CANONICAL_DIRECTORY",
            settings.JQUANTS_CANONICAL_DIRECTORY,
            "directory",
        ),
        (
            "ledger",
            "JQUANTS_LEDGER_DATABASE_FILE",
            settings.JQUANTS_LEDGER_DATABASE_FILE,
            "file",
        ),
        (
            "metrics",
            "JQUANTS_EXECUTION_METRICS_DIRECTORY",
            settings.JQUANTS_EXECUTION_METRICS_DIRECTORY,
            "directory",
        ),
        (
            "backfill_state",
            "JQUANTS_BACKFILL_STATE_DIRECTORY",
            settings.JQUANTS_BACKFILL_STATE_DIRECTORY,
            "directory",
        ),
        (
            "calendar",
            "JQUANTS_CALENDAR_DIRECTORY",
            settings.JQUANTS_CALENDAR_DIRECTORY,
            "directory",
        ),
    )
    destinations = []
    for name, setting, default_value, kind in configured_targets:
        source = "environment" if setting in environ else "default"
        value = environ.get(setting, os.fspath(default_value))
        normalized_path, drive, is_absolute = _normalize_destination(value)
        destinations.append(
            StorageDestination(
                name=name,
                setting=setting,
                kind=kind,
                source=source,
                path=normalized_path,
                drive=drive,
                is_absolute=is_absolute,
                matches_operational_drive=(
                    drive.casefold() == EXPECTED_OPERATIONAL_DRIVE.casefold()
                    and is_absolute
                ),
            )
        )
    return JQuantsStorageAudit(tuple(destinations))
