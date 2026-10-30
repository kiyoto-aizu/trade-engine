"""Read-only validation of effective J-Quants storage destinations."""

from __future__ import annotations

import ntpath
import os
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath
from typing import Mapping, Protocol

EXPECTED_OPERATIONAL_DRIVE = "D:"


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
    matches_operational_drive: bool

    def as_dict(self) -> dict[str, str | bool]:
        return {
            "name": self.name,
            "setting": self.setting,
            "kind": self.kind,
            "source": self.source,
            "path": self.path,
            "drive": self.drive,
            "matches_operational_drive": self.matches_operational_drive,
        }


@dataclass(frozen=True)
class JQuantsStorageAudit:
    destinations: tuple[StorageDestination, ...]

    @property
    def is_valid(self) -> bool:
        return all(item.matches_operational_drive for item in self.destinations)

    def as_dict(self) -> dict[str, object]:
        return {
            "status": "PASS" if self.is_valid else "FAILED",
            "expected_drive": EXPECTED_OPERATIONAL_DRIVE,
            "destinations": [item.as_dict() for item in self.destinations],
        }


def _normalize_destination(value: Path | str) -> tuple[str, str]:
    raw_path = os.fspath(value)
    windows_path = PureWindowsPath(raw_path)
    if windows_path.drive and windows_path.is_absolute():
        if os.name == "nt":
            resolved = Path(raw_path).expanduser().resolve()
            return str(resolved), resolved.drive.upper()
        normalized = ntpath.normpath(raw_path)
        return normalized, PureWindowsPath(normalized).drive.upper()

    resolved = Path(raw_path).expanduser().resolve()
    return str(resolved), resolved.drive.upper()


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
    for name, setting, value, kind in configured_targets:
        normalized_path, drive = _normalize_destination(value)
        destinations.append(
            StorageDestination(
                name=name,
                setting=setting,
                kind=kind,
                source="environment" if setting in environ else "default",
                path=normalized_path,
                drive=drive,
                matches_operational_drive=(
                    drive.casefold() == EXPECTED_OPERATIONAL_DRIVE.casefold()
                ),
            )
        )
    return JQuantsStorageAudit(tuple(destinations))
