"""Yahoo Financeのintraday chart APIから分足（デフォルト1分足）を取得します。"""
import logging
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from urllib.parse import quote

from src.api import request_handler
from src.domain.models import MinuteBar

logger = logging.getLogger(__name__)
JST = timezone(timedelta(hours=9))


@dataclass(frozen=True)
class IntradayFetchResult:
    """分足取得の結果。取得失敗(ok=False)と、取得できたが足が無い(ok=True, bars=[])を区別する。"""

    bars: list[MinuteBar] = field(default_factory=list)
    ok: bool = True
    error: str | None = None


def get_yahoo_intraday_bars(symbol: str, days: int = 7, interval: str = "1m") -> list[MinuteBar]:
    """
    指定銘柄の分足をYahoo Financeから取得し、MinuteBar(source="yahoo")のリストで返します。

    Yahoo Financeの制約により、1分足(interval="1m")は直近7日程度までしか遡れません。
    銘柄がどの日付の足かはMinuteBar単体ではなく、呼び出し側でtimeの日付部分から判定してください。
    失敗時は空リストを返します(失敗と足なしを区別したい場合はfetch_yahoo_intraday_barsを使う)。
    """
    return fetch_yahoo_intraday_bars(symbol, days=days, interval=interval).bars


def fetch_yahoo_intraday_bars(
    symbol: str, days: int = 7, interval: str = "1m", timeout: float = 30
) -> IntradayFetchResult:
    """get_yahoo_intraday_barsと同じ取得を行い、失敗の有無と理由も返します。"""
    yf_symbol = f"{symbol}.T"
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{yf_symbol}"
    params = {"interval": interval, "range": f"{days}d"}
    response = request_handler.send_get(
        url,
        params=params,
        headers={"User-Agent": "Mozilla/5.0"},
        timeout=timeout,
    )
    if not response:
        return IntradayFetchResult(ok=False, error="NO_RESPONSE")
    return _parse_intraday_response(symbol, response)


def fetch_yahoo_index_intraday_bars(
    index_symbol: str, start: date, end: date, interval: str = "1m", timeout: float = 30
) -> IntradayFetchResult:
    """指数(例: ^N225)の分足を、start〜end(両端含む・JST)の期間で取得します。

    Yahooの1分足は1回のリクエストが8日以内に制限されるため、呼び出し側で分割してください。
    """
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{quote(index_symbol, safe='')}"
    period1 = int(datetime.combine(start, time.min, tzinfo=JST).timestamp())
    period2 = int(datetime.combine(end + timedelta(days=1), time.min, tzinfo=JST).timestamp())
    response = request_handler.send_get(
        url,
        params={"interval": interval, "period1": period1, "period2": period2},
        headers={"User-Agent": "Mozilla/5.0"},
        timeout=timeout,
    )
    if not response:
        return IntradayFetchResult(ok=False, error="NO_RESPONSE")
    return _parse_intraday_response(index_symbol, response)


def _parse_intraday_response(symbol: str, response: dict) -> IntradayFetchResult:
    try:
        chart = response.get("chart", {})
        result = chart.get("result") or []
        if not result:
            error = chart.get("error")
            if error:
                return IntradayFetchResult(ok=False, error=f"YAHOO_ERROR:{error.get('code', 'unknown')}")
            return IntradayFetchResult()
        chart_result = result[0]
        timestamps = chart_result.get("timestamp") or []
        quote = chart_result.get("indicators", {}).get("quote", [{}])[0]
        closes = quote.get("close", [])
        volumes = quote.get("volume", [])
    except (KeyError, TypeError, IndexError, AttributeError) as exc:
        logger.exception("Yahoo分足データの解析に失敗しました (%s): %s", symbol, exc)
        return IntradayFetchResult(ok=False, error=f"PARSE_ERROR:{type(exc).__name__}")

    bars: list[MinuteBar] = []
    for index, timestamp in enumerate(timestamps):
        close = closes[index] if index < len(closes) else None
        if close is None:
            continue
        volume = volumes[index] if index < len(volumes) else None
        minute_time = datetime.fromtimestamp(timestamp, tz=JST).strftime("%Y-%m-%dT%H:%M:00")
        bars.append(MinuteBar(
            time=minute_time,
            price=float(close),
            cumulative_volume=None,
            volume=float(volume) if volume is not None else None,
            source="yahoo",
        ))
    return IntradayFetchResult(bars=bars)