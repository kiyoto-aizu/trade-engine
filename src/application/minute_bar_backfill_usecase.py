from __future__ import annotations

import logging
from datetime import date
from time import perf_counter
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
        self.last_bars_by_date: dict[tuple[str, date], int] = {}
        self.last_failures: dict[str, str] = {}
        self.last_unprocessed_symbols: list[str] = []

    def run(
        self,
        symbols: list[str],
        days: int = 7,
        max_elapsed_seconds: float | None = None,
    ) -> dict[str, int]:
        """
        対象銘柄それぞれについてYahoo分足を取得し、日付ごとに振り分けて保存します。

        Returns:
            銘柄ごとに新規追加した足の本数（呼び出し元でのログ・通知用）
        """
        started = perf_counter()
        self.last_bars_by_date = {}
        self.last_failures = {}
        self.last_unprocessed_symbols = []
        imported_counts: dict[str, int] = {}
        for index, symbol in enumerate(symbols):
            if max_elapsed_seconds is not None and perf_counter() - started >= max_elapsed_seconds:
                self.last_unprocessed_symbols = symbols[index:]
                logger.warning(
                    "選定外銘柄のバックフィル時間上限に達したため残りを打ち切ります: "
                    "上限=%.1f秒 未取得銘柄数=%d",
                    max_elapsed_seconds,
                    len(self.last_unprocessed_symbols),
                )
                break
            try:
                bars = self.fetch_intraday_bars(symbol, days)
            except Exception as exc:
                self.last_failures[symbol] = f"FETCH_ERROR:{type(exc).__name__}"
                imported_counts[symbol] = 0
                logger.warning(
                    "Yahoo分足の取得に失敗したため次の銘柄へ進みます: 銘柄=%s エラー=%s",
                    symbol,
                    exc,
                )
                continue
            if not bars:
                logger.warning("Yahoo分足が取得できなかったためスキップします: 銘柄=%s", symbol)
                imported_counts[symbol] = 0
                self.last_failures[symbol] = "NO_BARS"
                continue

            existing_bars_by_date: dict[date, list[MinuteBar]] = {}
            existing_times_by_date: dict[date, set[str]] = {}
            new_bars_by_date: dict[date, list[MinuteBar]] = {}
            for bar in bars:
                bar_date = date.fromisoformat(bar.time[:10])
                key = (symbol, bar_date)
                self.last_bars_by_date[key] = self.last_bars_by_date.get(key, 0) + 1
                if bar_date not in existing_times_by_date:
                    existing_bars = self.repository.load_bars(bar_date, symbol)
                    existing_bars_by_date[bar_date] = existing_bars
                    existing_times_by_date[bar_date] = {existing_bar.time for existing_bar in existing_bars}
                if bar.time in existing_times_by_date[bar_date]:
                    continue
                new_bars_by_date.setdefault(bar_date, []).append(bar)
                existing_times_by_date[bar_date].add(bar.time)
            imported_count = 0
            for bar_date, new_bars in new_bars_by_date.items():
                self.repository.replace_bars(
                    bar_date,
                    symbol,
                    [*existing_bars_by_date[bar_date], *new_bars],
                )
                imported_count += len(new_bars)
            imported_counts[symbol] = imported_count
            logger.info("Yahoo分足を取り込みました: 銘柄=%s 件数=%d", symbol, imported_count)

        return imported_counts
