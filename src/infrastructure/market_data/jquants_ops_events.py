"""Operational records live below metrics/ops, outside ingestion metrics globbing."""

import json
import os
from pathlib import Path

from src.domain.jquants_ops_policy import JQuantsOpsEvent


class JQuantsOpsEventRepository:
    def __init__(self, metrics_directory):
        self._path = Path(metrics_directory) / "ops" / "events.jsonl"

    def append(self, event: JQuantsOpsEvent) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        data = (json.dumps(event.as_dict(), sort_keys=True) + "\n").encode("utf-8")
        fd = os.open(self._path, os.O_CREAT | os.O_APPEND | os.O_WRONLY, 0o600)
        try:
            if os.write(fd, data) != len(data):
                raise OSError("operational event write failed")
            os.fsync(fd)
        finally:
            os.close(fd)

    def scheduled_lock_streak(self) -> int:
        try:
            rows = self._path.read_text(encoding="utf-8").splitlines()
        except FileNotFoundError:
            return 0
        count = 0
        invocations = set()
        for row in reversed(rows):
            event = JQuantsOpsEvent(**json.loads(row))
            if not event.scheduled or event.stage != "OPS" or event.event_id != "RESULT":
                continue
            if event.invocation_id in invocations:
                continue
            invocations.add(event.invocation_id)
            if event.reason != "GLOBAL_ALREADY_RUNNING":
                break
            count += 1
        return count
