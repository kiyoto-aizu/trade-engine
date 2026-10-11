"""旧バックテストエンジンへ渡した入力の保存・検証・読み込み。

保存先は data/backtest/input_snapshots/<実行時刻>_<ラベル>/ 。
大きいデータはgzip圧縮JSON、全体に manifest.json(各ファイルのsha256など)を付ける。
"""
from __future__ import annotations

import gzip
import hashlib
import json
import re
import shutil
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any, Iterable

from src.domain.market_regime import MarketRegime
from src.domain.market_volatility import MarketDailyBar
from src.domain.volatility import DailyBar

SNAPSHOT_VERSION = 1
MANIFEST_NAME = "manifest.json"
RUN_CONDITIONS_NAME = "run_conditions.json"
HISTORY_NAME = "history.json.gz"
OHLC_NAME = "ohlc_history.json.gz"
INDEX_NAME = "index_daily.json.gz"
MINUTE_MANIFEST_NAME = "minute_manifest.json"
FILTERING_DIRECTORY_NAME = "filtering"


class SnapshotError(ValueError):
    """スナップショットが欠損・破損しており、再実行に使えない。"""


@dataclass
class BacktestInputSnapshot:
    directory: Path
    manifest: dict
    run_conditions: dict
    history: dict
    ohlc_history: dict[str, dict[str, DailyBar]] | None
    index_daily: dict | None
    minute_manifest: dict

    @property
    def mode(self) -> str:
        return self.run_conditions["mode"]

    @property
    def filtering_directory(self) -> Path:
        return self.directory / FILTERING_DIRECTORY_NAME


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write_json(path: Path, payload: Any) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _write_gzip_json(path: Path, payload: Any) -> None:
    # mtime固定で、同じ内容なら同じバイト列になる
    with path.open("wb") as raw:
        with gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0) as compressed:
            compressed.write(json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))


def _read_gzip_json(path: Path) -> Any:
    with gzip.open(path, "rb") as file:
        return json.loads(file.read().decode("utf-8"))


def build_snapshot_directory_name(label: str, now: datetime) -> str:
    safe_label = re.sub(r"[^A-Za-z0-9_-]", "_", label) or "backtest"
    return f"{now:%Y%m%d_%H%M%S}_{safe_label}"


def collect_filtering_files(directory: Path, date_texts: Iterable[str]) -> list[Path]:
    """load_daily_filtering_symbolsが対象日として読んだJSONファイルを返す。"""
    wanted = set(date_texts)
    files = []
    for path in sorted(Path(directory).glob("*.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        date_text = str(data.get("date") or path.stem) if isinstance(data, dict) else path.stem
        if date_text in wanted:
            files.append(path)
    return files


def minute_bar_grid(daily_symbols: dict[str, list[str]], dated_history: dict | None) -> list[tuple[str, str]]:
    """シミュレーションが分足を読みに行く (日付, 銘柄) の一覧。"""
    all_symbols = set(dated_history or {}) | {s for symbols in daily_symbols.values() for s in symbols}
    return [(date_text, symbol) for date_text in sorted(daily_symbols) for symbol in sorted(all_symbols)]


def _minute_entry(repository, date_text: str, symbol: str) -> dict | None:
    path = repository.partition_path(date.fromisoformat(date_text), symbol)
    if not path.exists():
        return None
    return {
        "bars": len(repository.load_bars(date.fromisoformat(date_text), symbol)),
        "sha256": _sha256_file(path),
    }


def build_minute_manifest(repository, grid: list[tuple[str, str]]) -> dict:
    """分足はコピーせず、対象日・銘柄ごとの本数とsha256だけを残す。"""
    if repository is None:
        return {"enabled": False, "entries": {}}
    entries: dict[str, dict[str, dict]] = {}
    for date_text, symbol in grid:
        entry = _minute_entry(repository, date_text, symbol)
        if entry is not None:
            entries.setdefault(date_text, {})[symbol] = entry
    return {"enabled": True, "directory": str(repository.directory), "entries": entries}


def find_minute_mismatches(manifest: dict, repository, grid: list[tuple[str, str]]) -> dict[str, list[str]]:
    """保存時のマニフェストと現在の分足Parquetを比較する。"""
    mismatches: dict[str, list[str]] = {"changed": [], "missing": [], "added": []}
    if not manifest.get("enabled") or repository is None:
        return mismatches
    entries = manifest.get("entries", {})
    for date_text, symbol in grid:
        expected = entries.get(date_text, {}).get(symbol)
        actual = _minute_entry(repository, date_text, symbol)
        key = f"{date_text}/{symbol}"
        if expected is None and actual is not None:
            mismatches["added"].append(key)
        elif expected is not None and actual is None:
            mismatches["missing"].append(key)
        elif expected is not None and actual != expected:
            mismatches["changed"].append(key)
    return mismatches


def _bars_to_json(bars: list[MarketDailyBar]) -> list[dict]:
    return [
        {"date": bar.date.isoformat(), "open": bar.open, "high": bar.high, "low": bar.low, "close": bar.close}
        for bar in bars
    ]


def _bars_from_json(rows: list[dict]) -> list[MarketDailyBar]:
    return [
        MarketDailyBar(
            date=date.fromisoformat(row["date"]),
            open=row["open"], high=row["high"], low=row["low"], close=row["close"],
        )
        for row in rows
    ]


def save_backtest_input_snapshot(
    root: Path,
    label: str,
    now: datetime,
    *,
    run_conditions: dict,
    dated_history: dict | None,
    fixed_history: dict | None,
    symbols: list[str] | None,
    ohlc_history: dict[str, dict[str, DailyBar]] | None,
    index_bars: dict[str, list[MarketDailyBar]] | None,
    market_regime_by_date: dict[str, MarketRegime] | None,
    filtering_files: list[Path],
    minute_manifest: dict,
    period: tuple[str | None, str | None],
    symbol_count: int,
) -> Path:
    """一時ディレクトリへ書いてから改名し、書きかけのスナップショットを残さない。"""
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    name = build_snapshot_directory_name(label, now)
    final = root / name
    suffix = 1
    while final.exists():
        suffix += 1
        final = root / f"{name}_{suffix}"
    temporary = root / f"{final.name}.tmp"
    if temporary.exists():
        shutil.rmtree(temporary)
    temporary.mkdir()
    try:
        _write_json(temporary / RUN_CONDITIONS_NAME, run_conditions)
        _write_gzip_json(temporary / HISTORY_NAME, {
            "dated_history": dated_history,
            "fixed_history": fixed_history,
            "symbols": symbols,
        })
        if ohlc_history is not None:
            _write_gzip_json(temporary / OHLC_NAME, {
                symbol: {
                    date_text: {
                        "high": bar.high, "low": bar.low, "close": bar.close,
                        "open": bar.open, "volume": bar.volume,
                    }
                    for date_text, bar in by_date.items()
                }
                for symbol, by_date in ohlc_history.items()
            })
        if index_bars is not None or market_regime_by_date is not None:
            _write_gzip_json(temporary / INDEX_NAME, {
                "bars": {symbol: _bars_to_json(bars) for symbol, bars in (index_bars or {}).items()},
                "market_regime_by_date": {
                    date_text: regime.value for date_text, regime in (market_regime_by_date or {}).items()
                },
            })
        _write_json(temporary / MINUTE_MANIFEST_NAME, minute_manifest)
        if filtering_files:
            (temporary / FILTERING_DIRECTORY_NAME).mkdir()
            for path in filtering_files:
                shutil.copy2(path, temporary / FILTERING_DIRECTORY_NAME / path.name)

        files = {
            path.relative_to(temporary).as_posix(): {"sha256": _sha256_file(path), "bytes": path.stat().st_size}
            for path in sorted(temporary.rglob("*")) if path.is_file()
        }
        _write_json(temporary / MANIFEST_NAME, {
            "snapshot_version": SNAPSHOT_VERSION,
            "created_at": now.isoformat(timespec="seconds"),
            "mode": run_conditions["mode"],
            "period_start": period[0],
            "period_end": period[1],
            "symbol_count": symbol_count,
            "total_bytes": sum(entry["bytes"] for entry in files.values()),
            "files": files,
        })
        temporary.replace(final)
    except BaseException:
        shutil.rmtree(temporary, ignore_errors=True)
        raise
    return final


def load_backtest_input_snapshot(directory: Path) -> BacktestInputSnapshot:
    """全ファイルのsha256を検証してから読み込む。欠損・不一致はSnapshotError。"""
    directory = Path(directory)
    manifest_path = directory / MANIFEST_NAME
    if not manifest_path.is_file():
        raise SnapshotError(f"スナップショットのmanifest.jsonがありません: {directory}")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        files = manifest["files"]
    except (json.JSONDecodeError, KeyError, TypeError) as exc:
        raise SnapshotError(f"manifest.jsonを読み込めません: {manifest_path}") from exc
    if manifest.get("snapshot_version") != SNAPSHOT_VERSION:
        raise SnapshotError(f"未対応のスナップショット形式です: {manifest.get('snapshot_version')}")

    missing = [name for name in files if not (directory / name).is_file()]
    if missing:
        raise SnapshotError(f"スナップショットのファイルが欠けています: {', '.join(missing)}")
    corrupted = [name for name, entry in files.items() if _sha256_file(directory / name) != entry["sha256"]]
    if corrupted:
        raise SnapshotError(f"スナップショットのsha256が一致しません: {', '.join(corrupted)}")

    required = {RUN_CONDITIONS_NAME, HISTORY_NAME, MINUTE_MANIFEST_NAME}
    absent = sorted(required - set(files))
    if absent:
        raise SnapshotError(f"スナップショットの必須ファイルがmanifestにありません: {', '.join(absent)}")

    try:
        run_conditions = json.loads((directory / RUN_CONDITIONS_NAME).read_text(encoding="utf-8"))
        history = _read_gzip_json(directory / HISTORY_NAME)
        minute_manifest = json.loads((directory / MINUTE_MANIFEST_NAME).read_text(encoding="utf-8"))
        ohlc_history = None
        if OHLC_NAME in files:
            ohlc_history = {
                symbol: {date_text: DailyBar(**bar) for date_text, bar in by_date.items()}
                for symbol, by_date in _read_gzip_json(directory / OHLC_NAME).items()
            }
        index_daily = None
        if INDEX_NAME in files:
            raw = _read_gzip_json(directory / INDEX_NAME)
            index_daily = {
                "bars": {symbol: _bars_from_json(rows) for symbol, rows in raw["bars"].items()},
                "market_regime_by_date": {
                    date_text: MarketRegime(value) for date_text, value in raw["market_regime_by_date"].items()
                },
            }
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise SnapshotError(f"スナップショットの内容を読み込めません: {directory} ({exc})") from exc
    return BacktestInputSnapshot(
        directory=directory,
        manifest=manifest,
        run_conditions=run_conditions,
        history=history,
        ohlc_history=ohlc_history,
        index_daily=index_daily,
        minute_manifest=minute_manifest,
    )
