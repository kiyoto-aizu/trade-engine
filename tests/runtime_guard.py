"""テストが本番の日次レポートや判定イベントDBを書き換えていないかを検出する番人。"""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, time
from pathlib import Path

import pytest

# 本番の平日定時処理の時間帯(切り分け表示用)。判定ロジックには使わない。
SCHEDULE_WINDOWS = (
    ("フィルタリング", time(9, 30), time(9, 34)),
    ("取引", time(9, 35), time(15, 30)),
    ("スクリーニング", time(15, 35), time(15, 59)),
)


def _basename(path: str) -> str:
    return re.split(r"[\\/]", path)[-1]


def _executable_summary(command_line: str) -> str:
    """コマンド行から実行ファイル名と -m モジュール/スクリプト名だけを残す(他の引数は捨てる)。"""
    match = re.match(r'\s*(?:"([^"]*)"|(\S+))(.*)', command_line, re.DOTALL)
    if not match:
        return "(不明)"
    executable = _basename(match.group(1) or match.group(2))
    rest = match.group(3).split()
    # 直後が -m <module> か <script>.py の場合だけ残す。それ以外(-c のコード、オプション値など)は出さない。
    if len(rest) >= 2 and rest[0] == "-m" and re.fullmatch(r"[\w.]+", rest[1]):
        return f"{executable} -m {rest[1]}"
    if rest and re.fullmatch(r"[^\s\"']+\.py", rest[0]):
        return f"{executable} {_basename(rest[0])}"
    return executable


def collect_python_processes() -> list[str]:
    """実行中pythonプロセスの PID・開始時刻・実行名のみを返す。引数は出力しない。"""
    if os.name == "nt":
        script = (
            "Get-CimInstance Win32_Process | Where-Object { $_.Name -like 'python*' } | "
            "ForEach-Object { [pscustomobject]@{ pid = $_.ProcessId; "
            "start = $_.CreationDate.ToString('yyyy-MM-dd HH:mm:ss'); cmd = $_.CommandLine } } | "
            "ConvertTo-Json -Compress"
        )
        output = subprocess.run(
            ["powershell", "-NoProfile", "-Command", script],
            capture_output=True, text=True, timeout=10, check=True,
        ).stdout.strip()
        rows = json.loads(output) if output else []
        if isinstance(rows, dict):
            rows = [rows]
        return [f"PID {row['pid']} 開始 {row['start']} {_executable_summary(row.get('cmd') or '')}" for row in rows]
    output = subprocess.run(
        ["ps", "-eo", "pid=,lstart=,args="],
        capture_output=True, text=True, timeout=10, check=True,
    ).stdout
    lines = []
    for line in output.splitlines():
        fields = line.split(None, 6)
        if len(fields) < 7 or "python" not in Path(fields[6].split()[0]).name:
            continue
        lines.append(f"PID {fields[0]} 開始 {' '.join(fields[1:6])} {_executable_summary(fields[6])}")
    return lines


def describe_schedule_overlap(detected_at: datetime) -> str:
    if detected_at.weekday() >= 5:
        return "休日のため平日定時処理の時間帯外"
    current = detected_at.time().replace(second=0, microsecond=0)
    hits = [name for name, start, end in SCHEDULE_WINDOWS if start <= current <= end]
    return "重なり: " + "、".join(hits) if hits else "平日定時処理の時間帯外"


def describe_diagnostics(files: list[Path], process_collector=collect_python_processes, now=None) -> list[str]:
    """違反時の切り分け情報。違反の有無の判定には関与しない。"""
    detected_at = (now or datetime.now()).astimezone()
    lines = [f"検出時刻(ローカル): {detected_at.isoformat(timespec='seconds')} ({'月火水木金土日'[detected_at.weekday()]}曜)"]
    lines.append(f"定時処理との関係: {describe_schedule_overlap(detected_at)}")
    for path in files:
        try:
            modified = datetime.fromtimestamp(path.stat().st_mtime).isoformat(timespec="microseconds")
            lines.append(f"更新日時(ローカル): {modified} {path}")
        except OSError:
            lines.append(f"更新日時: 取得不可(存在しない) {path}")
    try:
        processes = process_collector()
        lines.append("実行中pythonプロセス(引数は非表示):")
        lines.extend(f"  {item}" for item in processes) if processes else lines.append("  なし")
    except Exception as error:
        lines.append(f"実行中pythonプロセス: 取得不可({type(error).__name__})")
    return lines


@dataclass(frozen=True)
class Snapshot:
    report_stats: dict[str, tuple[int, int]]
    report_hashes: dict[str, str] = field(default_factory=dict)
    database_mtime_ns: int | None = None
    watched_stats: dict[str, tuple[int, int]] = field(default_factory=dict)
    watched_hashes: dict[str, str] = field(default_factory=dict)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 16), b""):
            digest.update(chunk)
    return digest.hexdigest()


class RuntimeStateGuard:
    """data/reports配下の全ファイルと判定イベントDBの更新日時を記録して比べる。"""

    def __init__(
        self,
        reports_directory: Path,
        database_file: Path,
        with_hash: bool = False,
        watched_directories: tuple[Path, ...] = (),
    ):
        self.reports_directory = Path(reports_directory)
        self.database_file = Path(database_file)
        self.with_hash = with_hash
        self.watched_directories = tuple(Path(path) for path in watched_directories)

    def snapshot(self) -> Snapshot:
        stats: dict[str, tuple[int, int]] = {}
        hashes: dict[str, str] = {}
        watched_stats: dict[str, tuple[int, int]] = {}
        watched_hashes: dict[str, str] = {}
        if self.reports_directory.exists():
            for path in sorted(self.reports_directory.rglob("*")):
                if not path.is_file():
                    continue
                key = path.relative_to(self.reports_directory).as_posix()
                info = path.stat()
                stats[key] = (info.st_size, info.st_mtime_ns)
                if self.with_hash:
                    hashes[key] = _sha256(path)
        database_mtime = self.database_file.stat().st_mtime_ns if self.database_file.exists() else None
        for directory in self.watched_directories:
            if not directory.exists():
                continue
            for path in sorted(directory.rglob("*")):
                if not path.is_file():
                    continue
                key = f"{directory.resolve()}::{path.relative_to(directory).as_posix()}"
                info = path.stat()
                watched_stats[key] = (info.st_size, info.st_mtime_ns)
                if self.with_hash:
                    watched_hashes[key] = _sha256(path)
        return Snapshot(stats, hashes, database_mtime, watched_stats, watched_hashes)

    def changes_since(self, before: Snapshot) -> list[str]:
        after = self.snapshot()
        changes = []
        for key in sorted(set(before.report_stats) | set(after.report_stats)):
            if key not in before.report_stats:
                changes.append(f"追加: {self.reports_directory / key}")
            elif key not in after.report_stats:
                changes.append(f"削除: {self.reports_directory / key}")
            elif before.report_stats[key] != after.report_stats[key]:
                changes.append(f"更新(サイズまたは更新日時): {self.reports_directory / key}")
            elif self.with_hash and before.report_hashes.get(key) != after.report_hashes.get(key):
                changes.append(f"内容変更(ハッシュ不一致): {self.reports_directory / key}")
        if before.database_mtime_ns != after.database_mtime_ns:
            changes.append(f"DBの更新日時が変化: {self.database_file}")
        for key in sorted(set(before.watched_stats) | set(after.watched_stats)):
            if key not in before.watched_stats:
                changes.append(f"監視対象に追加: {key}")
            elif key not in after.watched_stats:
                changes.append(f"監視対象から削除: {key}")
            elif before.watched_stats[key] != after.watched_stats[key]:
                changes.append(f"監視対象が更新: {key}")
            elif self.with_hash and before.watched_hashes.get(key) != after.watched_hashes.get(key):
                changes.append(f"監視対象の内容が変更: {key}")
        return changes

    def existing_changed_paths(self, before: Snapshot) -> list[Path]:
        """追加・更新されたファイルの実パス(診断表示用。違反判定には使わない)。"""
        after = self.snapshot()
        paths = [
            self.reports_directory / key
            for key, value in after.report_stats.items()
            if before.report_stats.get(key) != value
            or (self.with_hash and before.report_hashes.get(key) != after.report_hashes.get(key))
        ]
        if before.database_mtime_ns != after.database_mtime_ns:
            paths.append(self.database_file)
        for key, value in after.watched_stats.items():
            if before.watched_stats.get(key) != value or (
                self.with_hash and before.watched_hashes.get(key) != after.watched_hashes.get(key)
            ):
                directory, relative = key.split("::", 1)
                paths.append(Path(directory) / relative)
        return paths


@contextmanager
def watch(guard: RuntimeStateGuard, label: str, process_collector=collect_python_processes):
    """ブロック内で本番の状態が変わったら、終了時にテストを失敗させる。"""
    before = guard.snapshot()
    yield
    changes = guard.changes_since(before)
    if changes:
        pytest.fail(
            f"{label}: テストが本番のデータを書き換えました。tmp_pathへ隔離してください。\n" + "\n".join(changes)
            + "\n--- 切り分け情報 ---\n"
            + "\n".join(describe_diagnostics(guard.existing_changed_paths(before), process_collector)),
            pytrace=False,
        )
