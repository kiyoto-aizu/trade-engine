"""metrics JSON から同一 dataset の連続 FAILED 回数を数える（読み取り専用）。"""

from __future__ import annotations

import json
from pathlib import Path

from src.domain.jquants_daily_stop_policy import LOCKED_CATEGORY


class JQuantsMetricsFailureHistory:
    def __init__(self, metrics_directory: Path | str) -> None:
        self._directory = Path(metrics_directory)

    def consecutive_failed(self, dataset: str) -> int:
        """ファイル名（時刻）順の末尾から連続する FAILED の数。ロック失敗は無視し、読めないものは FAILED とみなす。"""
        streak = 0
        paths = sorted(
            self._directory.glob(f"{dataset}/date=*/*.json"), key=lambda p: p.name, reverse=True
        )
        for path in paths:
            status, category = self._read(path)
            if category == LOCKED_CATEGORY:
                continue
            if status != "FAILED":
                break
            streak += 1
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
