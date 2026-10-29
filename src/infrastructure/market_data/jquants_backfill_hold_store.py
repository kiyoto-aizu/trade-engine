"""補填の「403要確認」「契約範囲外」を日付単位で保持するJSONストア（Git管理外）。

取得台帳（raw store）のスキーマは変えない。ここに記録された日付は、人間が確認するまで再開対象から外す。
"""

from __future__ import annotations

import json
import os
import uuid
from datetime import date
from pathlib import Path

from src.domain.jquants_backfill_policy import HOLD_FORBIDDEN, HOLD_OUT_OF_CONTRACT

FILE_NAME = "backfill_holds.json"
FORMAT_VERSION = 1
VALID_KINDS = frozenset({HOLD_FORBIDDEN, HOLD_OUT_OF_CONTRACT})


class BackfillHoldStoreError(Exception):
    pass


class JQuantsBackfillHoldStore:
    def __init__(self, directory: Path) -> None:
        self._path = Path(directory) / FILE_NAME

    def _load(self) -> dict[str, dict[str, str]]:
        if not self._path.exists():
            return {}
        try:
            payload = json.loads(self._path.read_text(encoding="utf-8"))
            if payload["format_version"] != FORMAT_VERSION:
                raise BackfillHoldStoreError("hold file has an unsupported format version")
            entries = payload["holds"]
            for days in entries.values():
                for key, kind in days.items():
                    if date.fromisoformat(key).isoformat() != key or kind not in VALID_KINDS:
                        raise BackfillHoldStoreError("hold file has an invalid entry")
        except BackfillHoldStoreError:
            raise
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            raise BackfillHoldStoreError("hold file is unreadable") from None
        return entries

    def held(self, dataset: str) -> dict[date, str]:
        return {date.fromisoformat(k): v for k, v in self._load().get(dataset, {}).items()}

    def record(self, dataset: str, target_date: date, kind: str) -> None:
        if kind not in VALID_KINDS:
            raise ValueError("unknown hold kind")
        entries = self._load()
        entries.setdefault(dataset, {})[target_date.isoformat()] = kind
        payload = {"format_version": FORMAT_VERSION, "holds": entries}
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_name(f"{self._path.name}.{uuid.uuid4().hex}.tmp")
        try:
            tmp.write_text(json.dumps(payload, indent=1, sort_keys=True), encoding="utf-8")
            os.replace(tmp, self._path)
        finally:
            tmp.unlink(missing_ok=True)
