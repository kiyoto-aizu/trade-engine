"""Yahoo Financeから市場指数の日足OHLCを取得します。"""
import logging
import math
from datetime import date, datetime, timedelta, timezone

from src.api import request_handler
from src.domain.market_volatility import MarketDailyBar

logger = logging.getLogger(__name__)
JST = timezone(timedelta(hours=9))


class YahooIndexClient:
    """.Tを付けずにYahoo Financeの指数シンボルを取得するクライアント。"""

    SUPPORTED_SYMBOLS = frozenset({"^N225", "^VIX", "^DJI", "^GSPC"})

    def get_daily_ohlc(self, symbol: str, range_: str = "max") -> list[MarketDailyBar]:
        """指定指数の確定日足OHLCを返します。"""
        if symbol not in self.SUPPORTED_SYMBOLS:
            raise ValueError(f"未対応の指数シンボルです: {symbol}")

        response = request_handler.send_get(
            f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}",
            params=(
                {"interval": "1d", "period1": 0, "period2": int(datetime.now(timezone.utc).timestamp())}
                if range_ == "max"
                else {"interval": "1d", "range": range_}
            ),
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=30,
        )
        if not response:
            return []

        try:
            result = response["chart"]["result"][0]
            timestamps = result.get("timestamp", [])
            quote = result["indicators"]["quote"][0]
            rows = []
            today = datetime.now(JST).date()
            for timestamp, open_, high, low, close in zip(
                timestamps,
                quote.get("open", []),
                quote.get("high", []),
                quote.get("low", []),
                quote.get("close", []),
            ):
                target_date = datetime.fromtimestamp(timestamp, tz=JST).date()
                if target_date >= today or any(value is None for value in (open_, high, low, close)):
                    continue
                rows.append(MarketDailyBar(
                    date=target_date,
                    open=float(open_),
                    high=float(high),
                    low=float(low),
                    close=float(close),
                ))
            return rows
        except (KeyError, IndexError, TypeError, ValueError, OverflowError):
            logger.warning("Yahoo Financeの指数日足取得に失敗しました: %s", symbol)
            return []

    def get_intraday_change_percent(self, symbol: str, as_of: datetime) -> float | None:
        """日経225の当日値を1回の取得で読み、直近営業日の終値比を返します。"""
        snapshot = self.get_intraday_snapshot(symbol, as_of)
        return None if snapshot is None else snapshot[0]

    def get_intraday_snapshot(
        self, symbol: str, as_of: datetime, timeout: float = 30
    ) -> tuple[float, datetime] | None:
        """日中の前日比(%)と、その算出に使った最終1分足の時刻(JST)を返します。"""
        if symbol != "^N225":
            raise ValueError("日中の前日比取得に対応する指数は^N225のみです")

        try:
            response = request_handler.send_get(
                f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}",
                params={"interval": "1m", "range": "5d"},
                headers={"User-Agent": "Mozilla/5.0"},
                timeout=timeout,
            )
            result = response["chart"]["result"][0] if response else None
            if result is None:
                return None
            if as_of.tzinfo is None:
                as_of = as_of.replace(tzinfo=JST)
            target_date = as_of.astimezone(JST).date()
            timestamps = result.get("timestamp", [])
            closes = result["indicators"]["quote"][0].get("close", [])
            previous_close = result.get("meta", {}).get("chartPreviousClose")
            if previous_close is not None:
                previous_close = float(previous_close)
                if not math.isfinite(previous_close) or previous_close <= 0:
                    previous_close = None
            current_price = None
            current_time: datetime | None = None
            previous_date: date | None = None
            for timestamp, raw_close in zip(timestamps, closes):
                if raw_close is None:
                    continue
                close = float(raw_close)
                if not math.isfinite(close) or close <= 0:
                    continue
                bar_time = datetime.fromtimestamp(timestamp, tz=JST)
                if bar_time > as_of.astimezone(JST):
                    continue
                if bar_time.date() < target_date:
                    if previous_date is None or bar_time.date() >= previous_date:
                        previous_date = bar_time.date()
                        previous_close = close
                elif bar_time.date() == target_date:
                    current_price = close
                    current_time = bar_time
            if previous_close is None or current_price is None or current_time is None:
                return None
            return (current_price / previous_close - 1.0) * 100.0, current_time
        except (KeyError, IndexError, TypeError, ValueError, OverflowError):
            logger.warning("Yahoo Financeの日経225日中値取得に失敗しました")
            return None
