import logging
from datetime import date, datetime, timedelta, timezone

from src.api import request_handler
from src.domain.atr_ratio_analysis import DatedDailyBar
from src.domain.volatility import DailyBar

logger = logging.getLogger(__name__)


class YahooFinanceClient:
    def get_daily_ohlc_history(
        self, symbol: str, range_: str = "max", until: date | None = None
    ) -> list[DatedDailyBar]:
        """指定銘柄の確定日足OHLC履歴を返します。

        untilを指定すると、その日(含む)までの日足を返します。省略時は当日より前の日足です。
        """
        try:
            response = request_handler.send_get(
                f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}.T",
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
            result = response["chart"]["result"][0]
            quote = result["indicators"]["quote"][0]
            today = datetime.now(timezone.utc).date() if until is None else until + timedelta(days=1)
            return [
                DatedDailyBar(
                    date=datetime.fromtimestamp(timestamp, tz=timezone.utc).date(),
                    bar=DailyBar(high=float(high), low=float(low), close=float(close)),
                )
                for timestamp, high, low, close in zip(
                    result.get("timestamp", []),
                    quote.get("high", []),
                    quote.get("low", []),
                    quote.get("close", []),
                )
                if datetime.fromtimestamp(timestamp, tz=timezone.utc).date() < today
                and high is not None
                and low is not None
                and close is not None
            ]
        except (KeyError, IndexError, TypeError, ValueError, OverflowError):
            logger.warning("Yahoo Financeの日足OHLC取得に失敗しました: %s", symbol)
            return []

    def get_daily_market_data(self, symbol: str, target_date: date) -> dict[str, float] | None:
        """指定日と直前営業日の終値、および指定日の出来高を返します。"""
        try:
            response = request_handler.send_get(
                f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}.T",
                params={"interval": "1d", "range": "3mo"},
                headers={"User-Agent": "Mozilla/5.0"},
                timeout=30,
            )
            result = response["chart"]["result"][0]
            timestamps = result["timestamp"]
            quote = result["indicators"]["quote"][0]
            rows = [
                (datetime.fromtimestamp(timestamp, tz=timezone.utc).date().isoformat(), close, volume)
                for timestamp, close, volume in zip(timestamps, quote["close"], quote["volume"])
                if close is not None and volume is not None
            ]
            target_index = next((index for index, row in enumerate(rows) if row[0] == target_date.isoformat()), None)
            if target_index is None or target_index == 0:
                return None
            _, close, volume = rows[target_index]
            previous_close = rows[target_index - 1][1]
            return {"close": float(close), "volume": float(volume), "previous_close": float(previous_close)}
        except (KeyError, IndexError, TypeError, ValueError):
            logger.warning("Yahoo Financeの日足取得に失敗しました: %s %s", symbol, target_date)
            return None

    def _get_daily_turnover(self, symbol: str, days: int = 90) -> dict[str, float]:
        """Yahoo Financeの日足から日付別の売買代金を取得します。"""
        response = request_handler.send_get(
            f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}.T",
            params={"interval": "1d", "range": f"{days}d"},
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=30,
        )
        result = response["chart"]["result"][0]
        timestamps = result.get("timestamp", [])
        quote = result["indicators"]["quote"][0]
        return {
            datetime.fromtimestamp(timestamp, tz=timezone.utc).date().isoformat(): float(close) * float(volume)
            for timestamp, close, volume in zip(timestamps, quote["close"], quote["volume"])
            if close is not None and volume is not None
        }

    def get_turnover_for_date(self, symbol: str, target_date: date) -> float | None:
        """指定日の終値×出来高を返します。"""
        try:
            return self._get_daily_turnover(symbol).get(target_date.isoformat())
        except (KeyError, IndexError, TypeError, ValueError):
            logger.warning("Yahoo Financeの対象日売買代金取得に失敗しました: %s %s", symbol, target_date)
            return None

    def get_average_turnover_before(self, symbol: str, target_date: date, days: int = 20) -> float | None:
        """指定日より前の日足売買代金の平均を返します。"""
        try:
            values = [
                value for day, value in self._get_daily_turnover(symbol, days=days + 30).items()
                if day < target_date.isoformat()
            ][-days:]
            return sum(values) / len(values) if values else None
        except (KeyError, IndexError, TypeError, ValueError, ZeroDivisionError):
            logger.warning("Yahoo Financeの対象日前平均売買代金取得に失敗しました: %s %s", symbol, target_date)
            return None

    def get_average_volume(self, symbol: str, days: int = 20) -> float | None:
        """過去の日足出来高平均を返します（後方互換用）。"""
        response = request_handler.send_get(
            f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}.T",
            params={"interval": "1d", "range": "1mo"},
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=30,
        )
        try:
            volumes = response["chart"]["result"][0]["indicators"]["quote"][0]["volume"]
            values = [float(volume) for volume in volumes if volume is not None][-days:]
            return sum(values) / len(values) if values else None
        except (KeyError, IndexError, TypeError, ValueError, ZeroDivisionError):
            logger.warning("Yahoo Financeの出来高取得に失敗しました: %s", symbol)
            return None

    def get_average_turnover(self, symbol: str, days: int = 20) -> float | None:
        """過去の日足終値と出来高から売買代金平均を返します。"""
        response = request_handler.send_get(
            f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}.T",
            params={"interval": "1d", "range": "1mo"},
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=30,
        )
        try:
            quote = response["chart"]["result"][0]["indicators"]["quote"][0]
            values = [
                float(close) * float(volume)
                for close, volume in zip(quote["close"], quote["volume"])
                if close is not None and volume is not None
            ][-days:]
            return sum(values) / len(values) if values else None
        except (KeyError, IndexError, TypeError, ValueError, ZeroDivisionError):
            logger.warning("Yahoo Financeの売買代金取得に失敗しました: %s", symbol)
            return None

    def get_average_turnover_details(
        self,
        symbol: str,
        days: int = 20,
        target_date: date | None = None,
    ) -> dict[str, float | int | bool | None]:
        """現在の平均値を変えず、使用件数と対象日行の有無も返します。"""
        try:
            response = request_handler.send_get(
                f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}.T",
                params={"interval": "1d", "range": "1mo"},
                headers={"User-Agent": "Mozilla/5.0"},
                timeout=30,
            )
            result = response["chart"]["result"][0]
            quote = result["indicators"]["quote"][0]
            closes = quote["close"]
            volumes = quote["volume"]
            values = [
                float(close) * float(volume)
                for close, volume in zip(closes, volumes)
                if close is not None and volume is not None
            ][-days:]
            timestamps = result.get("timestamp", [])
            aligned = len(timestamps) == len(closes) == len(volumes)
            includes_target_date = None
            if target_date is not None and aligned:
                dated_values = [
                    datetime.fromtimestamp(timestamp, tz=timezone.utc).date()
                    for timestamp, close, volume in zip(timestamps, closes, volumes)
                    if close is not None and volume is not None
                ][-days:]
                includes_target_date = target_date in dated_values
            return {
                "average_turnover": sum(values) / len(values) if values else None,
                "average_days": len(values),
                "average_includes_target_date": includes_target_date,
            }
        except (KeyError, IndexError, TypeError, ValueError, ZeroDivisionError, OverflowError):
            logger.warning("Yahoo Financeの売買代金詳細取得に失敗しました: %s", symbol)
            return {
                "average_turnover": None,
                "average_days": 0,
                "average_includes_target_date": None,
            }
