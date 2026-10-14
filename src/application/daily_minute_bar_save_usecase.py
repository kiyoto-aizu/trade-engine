from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import date
from statistics import median
from typing import Callable

from src.domain.models import MinuteBar
from src.infrastructure.market_data.get_intraday_bars import IntradayFetchResult
from src.infrastructure.persistence.parquet_minute_bar_repository import ParquetMinuteBarRepository

logger = logging.getLogger(__name__)

# 過去履歴が十分にある銘柄だけ、直近の中央値から極端に少ない場合を記録する。
MIN_HISTORY_DAYS = 3
HISTORY_LOOKBACK_DAYS = 10
FEW_BARS_RATIO = 0.5
DEFAULT_FETCH_INTERVAL_SECONDS = 0.5
DEFAULT_RETRIES = 2
DEFAULT_RETRY_WAIT_SECONDS = 5.0
# 土曜バックフィルと同じ取得条件(range=7d)にそろえる。
FETCH_DAYS = 7

SAVED = "SAVED"
FEW_BARS = "FEW_BARS"
ZERO = "ZERO"
ERROR = "ERROR"


@dataclass(frozen=True)
class SymbolSaveResult:
    symbol: str
    status: str
    bar_count: int = 0
    first_time: str | None = None
    last_time: str | None = None
    reason: str | None = None
    historical_median: float | None = None


@dataclass
class DailyMinuteBarSaveResult:
    trade_date: date
    symbols: list[SymbolSaveResult] = field(default_factory=list)

    def summary_line(self) -> str:
        total = len(self.symbols)
        saved = sum(1 for item in self.symbols if item.status in (SAVED, FEW_BARS))
        if saved == total and not any(item.status == FEW_BARS for item in self.symbols):
            return f"分足 {total}銘柄すべて保存"

        parts = [f"分足 {saved}/{total}銘柄保存"]
        zero_symbols = [item.symbol for item in self.symbols if item.status == ZERO]
        error_symbols = [item.symbol for item in self.symbols if item.status == ERROR]
        few_symbols = [item.symbol for item in self.symbols if item.status == FEW_BARS]
        if zero_symbols:
            parts.append(f"0本: {','.join(zero_symbols)}")
        if error_symbols:
            parts.append(f"保存失敗: {','.join(error_symbols)}")
        if few_symbols:
            parts.append(f"本数少: {','.join(few_symbols)}")
        return "、".join(parts)


class DailyMinuteBarSaveUseCase:
    """当日の対象銘柄の1分足をYahooから取得してParquetへ保存する。

    同じ日を繰り返し実行しても、時刻をキーに統合するため重複しない。
    同時刻はYahooの足で置換し、Yahooに無い時刻の既存足(自前ポーリング)は残す(土曜バックフィルと同じ優先ルール)。
    銘柄ごとの失敗は例外にせず結果へ記録し、次の銘柄へ進む。
    """

    def __init__(
        self,
        fetch_bars: Callable[[str, int], IntradayFetchResult],
        repository: ParquetMinuteBarRepository,
        *,
        interval_seconds: float = DEFAULT_FETCH_INTERVAL_SECONDS,
        retries: int = DEFAULT_RETRIES,
        retry_wait_seconds: float = DEFAULT_RETRY_WAIT_SECONDS,
        sleeper: Callable[[float], None] = time.sleep,
    ):
        self.fetch_bars = fetch_bars
        self.repository = repository
        self.interval_seconds = interval_seconds
        self.retries = max(retries, 0)
        self.retry_wait_seconds = retry_wait_seconds
        self.sleeper = sleeper

    def run(self, trade_date: date, symbols: list[str]) -> DailyMinuteBarSaveResult:
        result = DailyMinuteBarSaveResult(trade_date=trade_date)
        for index, symbol in enumerate(symbols):
            if index and self.interval_seconds > 0:
                self.sleeper(self.interval_seconds)
            item = self._save_symbol(trade_date, symbol)
            result.symbols.append(item)
            self._log(item)
        return result

    def _fetch_day_bars(self, trade_date: date, symbol: str) -> tuple[list[MinuteBar], str | None]:
        """当日分の足と、空だった場合の理由を返す。失敗・空は上限まで再試行する。"""
        bars: list[MinuteBar] = []
        reason: str | None = None
        for attempt in range(self.retries + 1):
            if attempt:
                self.sleeper(self.retry_wait_seconds)
            try:
                fetched = self.fetch_bars(symbol, FETCH_DAYS)
            except Exception as exc:
                reason = f"EXCEPTION:{type(exc).__name__}"
                continue
            if not fetched.ok:
                reason = fetched.error or "FETCH_FAILED"
                continue
            bars = [bar for bar in fetched.bars if bar.time[:10] == trade_date.isoformat()]
            if bars:
                return bars, None
            reason = "NO_BARS_FOR_DATE" if fetched.bars else "EMPTY_RESPONSE"
        return [], reason

    def _save_symbol(self, trade_date: date, symbol: str) -> SymbolSaveResult:
        bars, reason = self._fetch_day_bars(trade_date, symbol)
        if not bars:
            return SymbolSaveResult(symbol, ZERO, reason=reason)
        historical_median = self._historical_median(trade_date, symbol)
        try:
            merged = {item.time: item for item in self.repository.load_bars(trade_date, symbol)}
            merged.update({bar.time: bar for bar in bars})
            self.repository.replace_bars(trade_date, symbol, list(merged.values()))
        except Exception as exc:
            return SymbolSaveResult(symbol, ERROR, reason=f"SAVE_ERROR:{type(exc).__name__}")
        times = sorted(bar.time for bar in bars)
        status = (
            FEW_BARS
            if historical_median is not None and len(bars) < historical_median * FEW_BARS_RATIO
            else SAVED
        )
        return SymbolSaveResult(
            symbol, status, len(bars), times[0], times[-1],
            historical_median=historical_median,
        )

    def _historical_median(self, trade_date: date, symbol: str) -> float | None:
        """保存済みの過去日付から、直近履歴の本数中央値を返す。"""
        symbol_dir = self.repository.directory / f"symbol={symbol}"
        if not symbol_dir.is_dir():
            return None
        dates = sorted(
            (
                path.name.removeprefix("date=")
                for path in symbol_dir.iterdir()
                if path.is_dir() and path.name.startswith("date=")
            ),
            reverse=True,
        )
        counts: list[int] = []
        for date_text in dates:
            try:
                historical_date = date.fromisoformat(date_text)
            except ValueError:
                continue
            if historical_date >= trade_date:
                continue
            try:
                count = len(self.repository.load_bars(historical_date, symbol))
            except Exception:
                logger.warning(
                    "過去分足の本数を読み込めないため本数少判定を行いません: 銘柄=%s 日付=%s",
                    symbol, historical_date, exc_info=True,
                )
                continue
            counts.append(count)
            if len(counts) >= HISTORY_LOOKBACK_DAYS:
                break
        if len(counts) < MIN_HISTORY_DAYS:
            return None
        return float(median(counts))

    @staticmethod
    def _log(item: SymbolSaveResult) -> None:
        if item.status in (SAVED, FEW_BARS):
            logger.info(
                "当日分足を保存しました: 銘柄=%s 本数=%d 範囲=%s〜%s%s",
                item.symbol, item.bar_count, item.first_time, item.last_time,
                " (本数少: 出来高が少ない銘柄では正常な場合あり)" if item.status == FEW_BARS else "",
            )
        else:
            logger.warning("当日分足を保存できませんでした: 銘柄=%s 理由=%s", item.symbol, item.reason)
