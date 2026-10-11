from __future__ import annotations

import logging
import time
from datetime import date
from pathlib import Path
from uuid import uuid4

from src.domain.models import MinuteBar

logger = logging.getLogger(__name__)


class ParquetMinuteBarRepository:
    """銘柄・日付パーティションへ分足を保存するParquetリポジトリ。"""

    _COLUMNS = ("time", "price", "cumulative_volume", "volume", "source")
    _REPLACE_ATTEMPTS = 5
    _REPLACE_RETRY_SECONDS = 0.2

    def __init__(self, directory: Path):
        self.directory = Path(directory)

    def _partition_path(self, target_date: date, symbol: str) -> Path:
        return (
            self.directory
            / f"symbol={symbol}"
            / f"date={target_date.isoformat()}"
            / "data.parquet"
        )

    def partition_path(self, target_date: date, symbol: str) -> Path:
        return self._partition_path(target_date, symbol)

    @staticmethod
    def _require_pyarrow():
        try:
            import pyarrow as pa
            import pyarrow.parquet as pq
        except ImportError as exc:
            raise RuntimeError("Parquet保存にはpyarrowが必要です。requirements.txtをインストールしてください。") from exc
        return pa, pq

    def load_bars(self, target_date: date, symbol: str) -> list[MinuteBar]:
        path = self._partition_path(target_date, symbol)
        if not path.exists():
            return []
        _, parquet = self._require_pyarrow()
        table = parquet.read_table(path, columns=list(self._COLUMNS))
        return [MinuteBar(**row) for row in table.to_pylist()]

    def append_bar(self, target_date: date, symbol: str, bar: MinuteBar) -> None:
        path = self._partition_path(target_date, symbol)
        path.parent.mkdir(parents=True, exist_ok=True)
        bars = self.load_bars(target_date, symbol)
        existing = next((item for item in bars if item.time == bar.time), None)
        if existing is not None and existing.source == "yahoo" and bar.source == "poll":
            logger.debug(
                "既にYahoo由来の分足があるため、簡易ポーリングでの上書きをスキップしました: 銘柄=%s 時刻=%s",
                symbol, bar.time,
            )
            return

        bars = [item for item in bars if item.time != bar.time]
        bars.append(bar)
        bars.sort(key=lambda item: item.time)
        self._write_bars(path, bars)
        logger.debug(
            "分足をParquet保存しました: 銘柄=%s 時刻=%s 件数=%d 取得元=%s",
            symbol, bar.time, len(bars), bar.source,
        )

    def replace_bars(self, target_date: date, symbol: str, bars: list[MinuteBar]) -> None:
        """移行やバッチ処理向けに、1パーティションを一括置換します。"""
        path = self._partition_path(target_date, symbol)
        path.parent.mkdir(parents=True, exist_ok=True)
        self._write_bars(path, sorted(bars, key=lambda item: item.time))

    def _write_bars(self, path: Path, bars: list[MinuteBar]) -> None:
        pa, parquet = self._require_pyarrow()
        table = pa.Table.from_pydict(
            {
                "time": [bar.time for bar in bars],
                "price": [bar.price for bar in bars],
                "cumulative_volume": [bar.cumulative_volume for bar in bars],
                "volume": [bar.volume for bar in bars],
                "source": [bar.source for bar in bars],
            },
            schema=pa.schema([
                pa.field("time", pa.string()),
                pa.field("price", pa.float64()),
                pa.field("cumulative_volume", pa.float64()),
                pa.field("volume", pa.float64()),
                pa.field("source", pa.string()),
            ]),
        )
        temporary_path = path.with_name(f"{path.name}.{uuid4().hex}.tmp")
        try:
            parquet.write_table(table, temporary_path)
            self._replace_with_retry(temporary_path, path)
        except Exception:
            try:
                temporary_path.unlink(missing_ok=True)
            except OSError:
                logger.warning("置換に失敗した一時Parquetファイルを削除できませんでした: %s", temporary_path)
            raise

    def _replace_with_retry(self, temporary_path: Path, path: Path) -> None:
        for attempt in range(1, self._REPLACE_ATTEMPTS + 1):
            try:
                temporary_path.replace(path)
                return
            except PermissionError:
                if attempt == self._REPLACE_ATTEMPTS:
                    raise
                delay = self._REPLACE_RETRY_SECONDS * (2 ** (attempt - 1))
                logger.warning(
                    "Parquetファイルがロックされているため置換を再試行します: 対象=%s 試行=%d/%d 待機=%.1f秒",
                    path,
                    attempt,
                    self._REPLACE_ATTEMPTS,
                    delay,
                )
                time.sleep(delay)