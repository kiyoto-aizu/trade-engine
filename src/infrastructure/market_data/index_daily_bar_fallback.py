"""指数の日足が取得できないときに、1分足から日足を組み立てて補完します。"""
from __future__ import annotations

import logging
from datetime import date
from typing import Callable

from src.config import config
from src.domain.market_volatility import MarketDailyBar
from src.domain.models import MinuteBar
from src.infrastructure.market_data.get_intraday_bars import (
    IntradayFetchResult,
    fetch_yahoo_index_intraday_bars,
)
from src.infrastructure.persistence.parquet_minute_bar_repository import ParquetMinuteBarRepository

logger = logging.getLogger(__name__)

# 大引け間際まで足が揃っている日だけを確定扱いにする
MIN_BAR_COUNT = 300
LAST_BAR_NOT_BEFORE = "15:25"


def build_daily_bar(target_date: date, bars: list[MinuteBar]) -> MarketDailyBar | None:
    """1分足(終値のみ)から日足を作ります。場中の欠けた日は作りません。"""
    day_bars = sorted(
        (bar for bar in bars if bar.time[:10] == target_date.isoformat() and bar.price > 0),
        key=lambda bar: bar.time,
    )
    if len(day_bars) < MIN_BAR_COUNT or day_bars[-1].time[11:16] < LAST_BAR_NOT_BEFORE:
        return None
    prices = [bar.price for bar in day_bars]
    return MarketDailyBar(
        date=target_date,
        open=prices[0],
        high=max(prices),
        low=min(prices),
        close=prices[-1],
    )


class MinuteBarDailyFallback:
    """保存済みの1分足を優先し、無ければYahooから取得して日足を組み立てる。"""

    def __init__(
        self,
        repository: ParquetMinuteBarRepository,
        fetch_bars: Callable[[str, date, date], IntradayFetchResult] = fetch_yahoo_index_intraday_bars,
    ):
        self.repository = repository
        self.fetch_bars = fetch_bars

    def __call__(self, symbol: str, target_date: date) -> MarketDailyBar | None:
        try:
            saved = build_daily_bar(target_date, self.repository.load_bars(target_date, symbol))
        except Exception:
            logger.exception("保存済み分足の読み込みに失敗しました: %s %s", symbol, target_date)
            saved = None
        if saved is not None:
            return saved
        fetched = self.fetch_bars(symbol, target_date, target_date)
        if not fetched.ok:
            logger.warning("分足による日足補完の取得に失敗しました: %s %s %s", symbol, target_date, fetched.error)
            return None
        return build_daily_bar(target_date, fetched.bars)


def create_index_daily_bar_fallback() -> MinuteBarDailyFallback:
    return MinuteBarDailyFallback(ParquetMinuteBarRepository(config.INDEX_MINUTE_BAR_PARQUET_DIR))
