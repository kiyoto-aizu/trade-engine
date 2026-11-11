from __future__ import annotations

import argparse
import hashlib
import os
import re
import shutil
import sys
import tempfile
import zipfile
import zlib
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

LOG_CATEGORIES = ("jobs", "application", "tests")
_LOG_FILE_PATTERN = re.compile(r"\.log(?:\..*)?$", re.IGNORECASE)
_DATE_PATTERN = re.compile(r"(?<!\d)(20\d{2})[-.]?(\d{2})[-.]?(\d{2})(?!\d)")
_MONTH_PATTERN = re.compile(r"^\d{6}$")
_ARCHIVE_CONTENT_ERRORS = (
    zipfile.BadZipFile,
    zlib.error,
    EOFError,
    RuntimeError,
    NotImplementedError,
)
LOG_ARCHIVE_READ_ERRORS = (OSError, KeyError, *_ARCHIVE_CONTENT_ERRORS)


@dataclass(frozen=True)
class LogFile:
    source: Path
    destination: Path


def _month_key(value: date) -> int:
    return value.year * 100 + value.month


def _file_month(path: Path) -> str:
    match = _DATE_PATTERN.search(path.name)
    if match:
        year, month = int(match.group(1)), int(match.group(2))
        if 1 <= month <= 12:
            return f"{year:04d}{month:02d}"
    return datetime.fromtimestamp(path.stat().st_mtime).strftime("%Y%m")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _is_log_file(path: Path) -> bool:
    return path.is_file() and not path.is_symlink() and _LOG_FILE_PATTERN.search(path.name) is not None


def _legacy_files(category_directory: Path) -> list[LogFile]:
    candidates = []
    for path in category_directory.iterdir():
        if not _is_log_file(path):
            continue
        month = _file_month(path)
        candidates.append(LogFile(path, category_directory / month / path.name))
    return candidates


def _month_directories(category_directory: Path) -> list[Path]:
    if not category_directory.exists():
        return []
    directories = []
    for path in category_directory.iterdir():
        if path.is_symlink() and _MONTH_PATTERN.fullmatch(path.name):
            raise OSError(f"Symbolic links are not supported in log storage: {path}")
        if path.is_dir() and _MONTH_PATTERN.fullmatch(path.name):
            month = int(path.name[4:])
            if 1 <= month <= 12:
                directories.append(path)
    return sorted(directories)


def _files_in_month(month_directory: Path) -> list[LogFile]:
    candidates = []
    for path in month_directory.rglob("*"):
        if path.is_symlink():
            raise OSError(f"Symbolic links are not supported in log storage: {path}")
        if path.is_file():
            candidates.append(LogFile(path, path))
    return candidates


def _copy_verified(source: Path, destination: Path) -> None:
    source_hash = _sha256(source)
    destination.parent.mkdir(parents=True, exist_ok=True)

    if destination.exists():
        if _sha256(destination) != source_hash:
            raise FileExistsError(f"Existing file differs from source; source retained: {destination}")
        return

    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            prefix=f".{destination.name}.",
            suffix=".partial",
            dir=destination.parent,
            delete=False,
        ) as temporary_file:
            temporary_path = Path(temporary_file.name)
        shutil.copy2(source, temporary_path)
        if _sha256(temporary_path) != source_hash:
            raise OSError(f"Log copy verification failed: {source}")
        if destination.exists():
            if _sha256(destination) != source_hash:
                raise FileExistsError(f"Existing file differs from source; source retained: {destination}")
            return
        os.replace(temporary_path, destination)
        temporary_path = None
        if _sha256(destination) != source_hash:
            raise OSError(f"Archived log verification failed: {destination}")
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def _transfer_files(files: list[LogFile], *, remove_sources: bool) -> None:
    for item in files:
        _copy_verified(item.source, item.destination)

    if remove_sources:
        for item in files:
            item.source.unlink()
        parents = {item.source.parent for item in files}
        for parent in sorted(parents, key=lambda path: len(path.parts), reverse=True):
            while parent.exists():
                try:
                    parent.rmdir()
                except OSError:
                    break
                if parent.parent not in parents:
                    break
                parent = parent.parent


def _zip_hashes(path: Path) -> dict[str, str]:
    try:
        with zipfile.ZipFile(path, "r") as archive:
            hashes = {}
            for item in archive.infolist():
                if item.is_dir():
                    continue
                if item.filename in hashes:
                    raise OSError(f"Duplicate file in log archive: {item.filename}")
                with archive.open(item, "r") as stream:
                    digest = hashlib.sha256()
                    for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                        digest.update(chunk)
                hashes[item.filename] = digest.hexdigest()
            return hashes
    except _ARCHIVE_CONTENT_ERRORS as error:
        raise OSError(f"Invalid log archive: {path}") from error


def _write_verified_zip(month_directory: Path, archive_path: Path, files: list[LogFile]) -> None:
    expected_hashes = {
        item.source.relative_to(month_directory).as_posix(): _sha256(item.source)
        for item in files
    }
    archive_path.parent.mkdir(parents=True, exist_ok=True)
    if archive_path.exists():
        existing_hashes = _zip_hashes(archive_path)
        if any(existing_hashes.get(name) != digest for name, digest in expected_hashes.items()):
            raise FileExistsError(f"Existing ZIP differs from local logs; local logs retained: {archive_path}")
        return

    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            prefix=f".{archive_path.stem}.",
            suffix=".partial",
            dir=archive_path.parent,
            delete=False,
        ) as temporary_file:
            temporary_path = Path(temporary_file.name)

        with zipfile.ZipFile(
            temporary_path,
            "w",
            compression=zipfile.ZIP_DEFLATED,
            compresslevel=6,
        ) as archive:
            for item in files:
                archive.write(
                    item.source,
                    arcname=item.source.relative_to(month_directory).as_posix(),
                )

        if _zip_hashes(temporary_path) != expected_hashes:
            raise OSError(f"ZIP verification failed; local logs retained: {archive_path}")
        if archive_path.exists():
            existing_hashes = _zip_hashes(archive_path)
            if any(existing_hashes.get(name) != digest for name, digest in expected_hashes.items()):
                raise FileExistsError(
                    f"Existing ZIP differs from local logs; local logs retained: {archive_path}"
                )
            return
        os.replace(temporary_path, archive_path)
        temporary_path = None
        archived_hashes = _zip_hashes(archive_path)
        if any(archived_hashes.get(name) != digest for name, digest in expected_hashes.items()):
            raise OSError(f"Archived ZIP verification failed; local logs retained: {archive_path}")
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def _archive_month(
    month_directory: Path,
    archive_path: Path,
    *,
    apply: bool,
) -> tuple[int, int]:
    files = _files_in_month(month_directory)
    size = sum(item.source.stat().st_size for item in files)
    if apply:
        if files:
            _write_verified_zip(month_directory, archive_path, files)
            for item in files:
                item.source.unlink()
            directories = sorted(
                (path for path in month_directory.rglob("*") if path.is_dir()),
                key=lambda path: len(path.parts),
                reverse=True,
            )
            for directory in directories:
                directory.rmdir()
        month_directory.rmdir()
    return len(files), size


def _previous_month(value: date) -> date:
    first_of_month = value.replace(day=1)
    if first_of_month.month == 1:
        return date(first_of_month.year - 1, 12, 1)
    return date(first_of_month.year, first_of_month.month - 1, 1)


@contextmanager
def _storage_lock(logs_root: Path):
    logs_root.mkdir(parents=True, exist_ok=True)
    lock_path = logs_root / ".monthly-log-storage.lock"
    with lock_path.open("a+b") as lock_file:
        lock_file.seek(0, os.SEEK_END)
        if lock_file.tell() == 0:
            lock_file.write(b"\0")
            lock_file.flush()
        lock_file.seek(0)
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(lock_file.fileno(), msvcrt.LK_LOCK, 1)
            unlock = lambda: msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            unlock = lambda: fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
        try:
            yield
        finally:
            unlock()


def prepare_monthly_log_storage(
    logs_root: Path,
    archive_root: Path,
    *,
    current_date: date | None = None,
    categories: tuple[str, ...] = LOG_CATEGORIES,
) -> list[str]:
    """Create monthly folders and archive local logs older than the previous month."""
    logs_root = logs_root.resolve()
    archive_root = archive_root.resolve()
    if archive_root == logs_root or logs_root in archive_root.parents:
        raise ValueError("The log archive directory must be outside the local log directory.")
    if any(category not in LOG_CATEGORIES for category in categories):
        raise ValueError("Unknown log category.")

    current_date = current_date or date.today()
    oldest_kept_month = _month_key(_previous_month(current_date))
    current_month = current_date.strftime("%Y%m")
    summaries = []

    with _storage_lock(logs_root):
        for category in categories:
            category_directory = logs_root / category
            category_directory.mkdir(parents=True, exist_ok=True)
            (category_directory / current_month).mkdir(exist_ok=True)

            legacy = _legacy_files(category_directory)
            if legacy:
                _transfer_files(legacy, remove_sources=True)
                summaries.append(f"{category}: moved {len(legacy)} legacy log files into monthly folders.")

            for month_directory in _month_directories(category_directory):
                if int(month_directory.name) >= oldest_kept_month:
                    continue
                archive_path = archive_root / category / f"{month_directory.name}.zip"
                count, size = _archive_month(month_directory, archive_path, apply=True)
                summaries.append(
                    f"{category}/{month_directory.name}: archived {count} files "
                    f"({size} bytes) to {archive_path}."
                )

    return summaries


def preview_monthly_log_storage(
    logs_root: Path,
    *,
    current_date: date | None = None,
    categories: tuple[str, ...] = LOG_CATEGORIES,
) -> list[tuple[str, str, str, int, int]]:
    """Return expired monthly folders and root-level legacy logs without changing files."""
    current_date = current_date or date.today()
    oldest_kept_month = _month_key(_previous_month(current_date))
    grouped: dict[tuple[str, str, str], tuple[int, int]] = {}

    for category in categories:
        category_directory = logs_root / category
        for legacy in _legacy_files(category_directory) if category_directory.exists() else []:
            month = legacy.destination.parent.name
            action = (
                "archive and remove local month"
                if int(month) < oldest_kept_month
                else "move into monthly folder"
            )
            key = (category, month, action)
            count, size = grouped.get(key, (0, 0))
            grouped[key] = (count + 1, size + legacy.source.stat().st_size)
        for month_directory in _month_directories(category_directory):
            if int(month_directory.name) >= oldest_kept_month:
                continue
            files = _files_in_month(month_directory)
            key = (category, month_directory.name, "archive and remove local month")
            count, size = grouped.get(key, (0, 0))
            grouped[key] = (
                count + len(files),
                size + sum(item.source.stat().st_size for item in files),
            )
    return [
        (category, month, action, count, size)
        for (category, month, action), (count, size) in sorted(grouped.items())
    ]


def _default_paths() -> tuple[Path, Path]:
    repository_root = Path(__file__).resolve().parents[2]
    return repository_root / "data" / "logs", Path(r"D:\logs")


def main(argv: list[str] | None = None) -> int:
    default_logs_root, default_archive_root = _default_paths()
    parser = argparse.ArgumentParser(description="Maintain monthly log folders and archive old months.")
    parser.add_argument("--logs-root", type=Path, default=default_logs_root)
    parser.add_argument("--archive-root", type=Path, default=default_archive_root)
    parser.add_argument("--current-month", default=date.today().strftime("%Y%m"))
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--preview", action="store_true", help="List planned moves and archives without changes.")
    mode.add_argument("--apply", action="store_true", help="Apply monthly migration and ZIP archival.")
    args = parser.parse_args(argv)

    if not _MONTH_PATTERN.fullmatch(args.current_month):
        parser.error("--current-month must use YYYYMM.")
    try:
        current_date = date(int(args.current_month[:4]), int(args.current_month[4:]), 1)
    except ValueError:
        parser.error("--current-month must be a valid YYYYMM month.")
    if args.preview or not args.apply:
        entries = preview_monthly_log_storage(args.logs_root, current_date=current_date)
        if not entries:
            print("No legacy logs or expired monthly folders found.")
        for category, month, action, count, size in entries:
            target = (
                f"{args.archive_root / category / f'{month}.zip'}"
                if action == "archive and remove local month"
                else f"{args.logs_root / category / month}"
            )
            print(f"{category}/{month}: {action}; {count} file(s), {size} bytes -> {target}")
        print("Preview only: no files were moved or deleted.")
        return 0

    try:
        for summary in prepare_monthly_log_storage(
            args.logs_root, args.archive_root, current_date=current_date,
        ):
            print(summary)
    except (OSError, ValueError) as error:
        print(f"Log archive/rotation failed; source logs were retained where not yet transferred: {error}",
              file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
