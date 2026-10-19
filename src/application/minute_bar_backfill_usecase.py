from __future__ import annotations

import logging
from datetime import date
from typing import Callable

from src.domain.models import MinuteBar
from src.infrastructure.persistence.parquet_minute_bar_repository import ParquetMinuteBarRepository

logger = logging.getLogger(__name__)


class MinuteBarBackfillUseCase:
    """
    Yahoo Financeの分足で、既存データにない時刻の足だけを追加するユースケース。

    同じ時刻の分足が既に保存されている場合は、取得元に関係なく既存値を保持する。
    """

    def __init__(self, fetch_intraday_bars: Callable[[str, int], list[MinuteBar]], repository: ParquetMinuteBarRepository):
        self.fetch_intraday_bars = fetch_intraday_bars
        self.repository = repository

    def run(self, symbols: list[str], days: int = 7) -> dict[str, int]:
        """
        対象銘柄それぞれについてYahoo分足を取得し、日付ごとに振り分けて保存します。

        Returns:
            銘柄ごとに新規追加した足の本数（呼び出し元でのログ・通知用）
        """
        imported_counts: dict[str, int] = {}
        for symbol in symbols:
            bars = self.fetch_intraday_bars(symbol, days)
            if not bars:
                logger.warning("Yahoo分足が取得できなかったためスキップします: 銘柄=%s", symbol)
                imported_counts[symbol] = 0
                continue

            existing_times_by_date: dict[date, set[str]] = {}
            imported_count = 0
            for bar in bars:
                bar_date = date.fromisoformat(bar.time[:10])
                if bar_date not in existing_times_by_date:
                    existing_times_by_date[bar_date] = {
                        existing_bar.time
                        for existing_bar in self.repository.load_bars(bar_date, symbol)
                    }
                if bar.time in existing_times_by_date[bar_date]:
                    continue
                self.repository.append_bar(bar_date, symbol, bar)
                existing_times_by_date[bar_date].add(bar.time)
                imported_count += 1
            imported_counts[symbol] = imported_count
            logger.info("Yahoo分足を取り込みました: 銘柄=%s 件数=%d", symbol, imported_count)

        return imported_counts
