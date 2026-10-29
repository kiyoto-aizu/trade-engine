"""J-Quants 営業日カレンダーの保存と読み出し（入出力のみ）。BusinessCalendarPort を満たす。

保存するのは日付と区分だけで、API応答の生データは保存しない。
"""

from __future__ import annotations

import hashlib
import json
import os
import uuid
from collections.abc import Mapping
from datetime import date
from pathlib import Path

from src.domain.jquants_business_calendar import VALID_CATEGORIES, BusinessCalendarError

FORMAT_VERSION = 1
FILE_NAME = "calendar.json"


def _digest(categories: Mapping[str, int]) -> str:
    body = json.dumps(dict(sorted(categories.items())), separators=(",", ":"))
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


class JQuantsCalendarStore:
    def __init__(self, directory: Path) -> None:
        self._path = Path(directory) / FILE_NAME

    def load(self) -> dict[date, int]:
        """保存済みの全日付を返す。未保存は空。ファイルが壊れていれば失敗する。"""
        if not self._path.exists():
            return {}
        try:
            payload = json.loads(self._path.read_text(encoding="utf-8"))
            raw = payload["categories"]
            if payload["format_version"] != FORMAT_VERSION:
                raise BusinessCalendarError("calendar file has an unsupported format version")
            if payload["sha256"] != _digest(raw):
                raise BusinessCalendarError("calendar file hash mismatch")
            result = {date.fromisoformat(k): v for k, v in raw.items()}
            if any(d.isoformat() != k for d, k in zip(result, raw)):
                raise BusinessCalendarError("calendar file has a non-standard date")
        except BusinessCalendarError:
            raise
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            raise BusinessCalendarError("calendar file is unreadable") from None
        if any(type(v) is not int or v not in VALID_CATEGORIES for v in result.values()):
            raise BusinessCalendarError("calendar file has an unknown category")
        return result

    def save(self, categories: Mapping[date, int]) -> None:
        raw = {d.isoformat(): c for d, c in sorted(categories.items())}
        payload = {
            "format_version": FORMAT_VERSION,
            "sha256": _digest(raw),
            "categories": raw,
        }
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_name(f"{self._path.name}.{uuid.uuid4().hex}.tmp")
        try:
            tmp.write_text(json.dumps(payload, indent=1, sort_keys=True), encoding="utf-8")
            os.replace(tmp, self._path)
        finally:
            tmp.unlink(missing_ok=True)

    def categories(self, start: date, end: date) -> Mapping[date, int | None]:
        """保存済みの日付だけを返す。未保存の日は含めない（読み取り側が失敗させる）。"""
        return {d: c for d, c in self.load().items() if start <= d <= end}
