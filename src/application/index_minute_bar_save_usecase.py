from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Callable

from src.domain.models import MinuteBar
from src.infrastructure.market_data.get_intraday_bars import IntradayFetchResult
from src.infrastructure.persistence.parquet_minute_bar_repository import ParquetMinuteBarRepository

logger = logging.getLogger(__name__)

INDEX_SYMBOL = "^N225"
# Yahooの1分足は1リクエスト8日以内の制限があるため、余裕を持って7日ずつ取得する。
CHUNK_DAYS = 7


@dataclass
class IndexMinuteBarSaveResult:
    saved_counts: dict[date, int] = field(default_factory=dict)
    failures: list[str] = field(default_factory=list)


class IndexMinuteBarSaveUseCase:
    """日経225など指数の1分足を取得し、日付ごとにParquetへ保存する(記録専用。売買判断には使わない)。"""

    def __init__(
        self,
        fetch_bars: Callable[[str, date, date], IntradayFetchResult],
        repository: ParquetMinuteBarRepository,
        symbol: str = INDEX_SYMBOL,
    ):
        self.fetch_bars = fetch_bars
        self.repository = repository
        self.symbol = symbol

    def run(self, start: date, end: date) -> IndexMinuteBarSaveResult:
        """start〜end(両端含む)を取得・保存する。失敗は例外にせず理由を結果とログへ残す。"""
        result = IndexMinuteBarSaveResult()
        chunk_start = start
        while chunk_start <= end:
            chunk_end = min(chunk_start + timedelta(days=CHUNK_DAYS - 1), end)
            self._save_chunk(chunk_start, chunk_end, result)
            chunk_start = chunk_end + timedelta(days=1)
        return result

    def _save_chunk(self, start: date, end: date, result: IndexMinuteBarSaveResult) -> None:
        try:
            fetched = self.fetch_bars(self.symbol, start, end)
        except Exception as exc:
            reason = f"{start}〜{end}: EXCEPTION:{type(exc).__name__}"
            logger.warning("指数分足の取得に失敗しました (%s): %s", self.symbol, reason)
            result.failures.append(reason)
            return
        if not fetched.ok:
            reason = f"{start}〜{end}: {fetched.error}"
            logger.warning("指数分足の取得に失敗しました (%s): %s", self.symbol, reason)
            result.failures.append(reason)
            return

        by_date: dict[date, list[MinuteBar]] = {}
        for bar in fetched.bars:
            bar_date = date.fromisoformat(bar.time[:10])
            if start <= bar_date <= end:
                by_date.setdefault(bar_date, []).append(bar)
        for bar_date, bars in sorted(by_date.items()):
            try:
                merged = {item.time: item for item in self.repository.load_bars(bar_date, self.symbol)}
                merged.update({bar.time: bar for bar in bars})
                self.repository.replace_bars(bar_date, self.symbol, list(merged.values()))
            except Exception as exc:
                reason = f"{bar_date}: SAVE_ERROR:{type(exc).__name__}"
                logger.warning("指数分足の保存に失敗しました (%s): %s", self.symbol, reason)
                result.failures.append(reason)
                continue
            result.saved_counts[bar_date] = len(bars)
        logger.info("指数分足を保存しました: %s %s〜%s 日数=%d", self.symbol, start, end, len(by_date))
