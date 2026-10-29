"""metrics JSON から同一 dataset の連続 FAILED 回数を数える（読み取り専用）。"""

from __future__ import annotations

import json
from pathlib import Path

LOCKED_CATEGORY = "DATASET_ALREADY_RUNNING"


class JQuantsMetricsFailureHistory:
    def __init__(self, metrics_directory: Path | str) -> None:
        self._directory = Path(metrics_directory)

    def consecutive_failed(self, dataset: str) -> int:
        """ファイル名（時刻）順の末尾から連続する FAILED の数。ロック失敗は無視し、読めないものは FAILED とみなす。"""
        streak = 0
        for path in sorted(self._directory.glob(f"{dataset}/date=*/*.json"), key=lambda p: p.name):
            status, category = self._read(path)
            if category == LOCKED_CATEGORY:
                continue
            streak = streak + 1 if status == "FAILED" else 0
        return streak

    @staticmethod
    def _read(path: Path) -> tuple[str | None, str | None]:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return "FAILED", None
        if not isinstance(data, dict):
            return "FAILED", None
        status, category = data.get("status"), data.get("error_category")
        return (status if isinstance(status, str) else "FAILED",
                category if isinstance(category, str) else None)
