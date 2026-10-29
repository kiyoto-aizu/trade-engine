"""Disk-space judgment for J-Quants runs (doc 18 §4-1, §4-2).

Pure functions only: callers measure free bytes and pass numbers in.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

HOLD_MULTIPLIER = 3.0
STOP_MULTIPLIER = 1.5
# Past-backfill only: stop when free space falls below this (daily updates continue).
BACKFILL_MIN_FREE_BYTES = 10 * 1000**3

MB = 1000**2
# Used when no measured size exists yet (Issue #12 measurements, per business day).
DEFAULT_RUN_BYTES = {
    "daily": {"raw": 1 * MB, "canonical": MB // 5},
    "minute": {"raw": 65 * MB, "canonical": 8 * MB},
}


class DiskVerdict(StrEnum):
    OK = "OK"
    HOLD = "HOLD"
    STOP = "STOP"


@dataclass(frozen=True)
class VolumeNeed:
    """Free space measured for one volume and the bytes the run places on it."""

    free_bytes: int
    needed_bytes: int


def judge_volume(volume: VolumeNeed) -> DiskVerdict:
    if volume.free_bytes < volume.needed_bytes * STOP_MULTIPLIER:
        return DiskVerdict.STOP
    if volume.free_bytes < volume.needed_bytes * HOLD_MULTIPLIER:
        return DiskVerdict.HOLD
    return DiskVerdict.OK


_ORDER = {DiskVerdict.OK: 0, DiskVerdict.HOLD: 1, DiskVerdict.STOP: 2}


def judge_volumes(volumes: list[VolumeNeed]) -> DiskVerdict:
    """The worst verdict across volumes wins."""
    return max((judge_volume(v) for v in volumes), key=_ORDER.__getitem__, default=DiskVerdict.OK)


def backfill_may_start(free_bytes: int, estimated_bytes: int) -> bool:
    """Backfill needs the estimate to fit within free space minus the 10GB floor."""
    return estimated_bytes <= free_bytes - BACKFILL_MIN_FREE_BYTES
