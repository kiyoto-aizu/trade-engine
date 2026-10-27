"""Free-space measurement for storage volumes."""

from __future__ import annotations

import os
import shutil
from pathlib import Path


def measure_volume(path: Path | str) -> tuple[int, int]:
    """Return ``(free_bytes, device_id)`` of the volume that holds or will hold ``path``."""
    target = Path(path).absolute()
    while not target.exists() and target.parent != target:
        target = target.parent
    return shutil.disk_usage(target).free, os.stat(target).st_dev
