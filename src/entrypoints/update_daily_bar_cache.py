"""日足キャッシュ(data/cache/yahoo_daily)を最新日まで手動更新するCLI。

使い方: python -m src.entrypoints.update_daily_bar_cache [--days 120] [--symbols 3133 ^N225] [--cache-dir PATH]
銘柄を省略すると、キャッシュディレクトリにある全銘柄(指数を含む)を更新します。
volumeキーが無い旧形式のキャッシュは、日付が足りていても再取得されます。
VIXは米国東部時間16時の通常引け後に確定日を進め、確定前は前の米国営業日を使います。
"""
from __future__ import annotations

import argparse
import logging
import sys
from datetime import date, datetime, time, timedelta
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.config import config
from src.domain.us_market_calendar import latest_confirmed_vix_date
from src.infrastructure.calendar.japanese_calendar import is_trading_day
from src.infrastructure.market_data.yahoo_daily_bar_cache import fetch_yahoo_dated_ohlc_cached

logger = logging.getLogger(__name__)

DEFAULT_CACHE_DIR = PROJECT_ROOT / "data" / "cache" / "yahoo_daily"
# 古い側の判定に持たせる余裕。Yahooの範囲指定は暦日で、先頭の営業日がずれるため
EARLIEST_MARGIN_DAYS = 10


def latest_confirmed_trading_day(now: datetime) -> date:
    """確定済みの最新営業日。当日は引け後のみ含める(場中の未確定足をキャッシュしない)。"""
    candidate = now.date()
    if not (is_trading_day(candidate) and now.time() >= time(config.MARKET_CLOSE_HOUR, config.MARKET_CLOSE_MINUTE)):
        candidate -= timedelta(days=1)
        while not is_trading_day(candidate):
            candidate -= timedelta(days=1)
    return candidate


def discover_symbols(cache_dir: Path) -> list[str]:
    return sorted(path.stem for path in cache_dir.glob("*.json"))


def update_cache(cache_dir: Path, symbols: list[str], days: int, now: datetime) -> dict[str, str]:
    """キャッシュを更新し、銘柄ごとの最新日を返す。"""
    latest = latest_confirmed_trading_day(now)
    earliest = now.date() - timedelta(days=max(days - EARLIEST_MARGIN_DAYS, 1))
    vix_symbols = [symbol for symbol in symbols if symbol == "^VIX"]
    other_symbols = [symbol for symbol in symbols if symbol != "^VIX"]
    history = {}
    if other_symbols:
        history.update(
            fetch_yahoo_dated_ohlc_cached(
                other_symbols,
                days=days,
                cache_dir=cache_dir,
                earliest_needed_date=earliest,
                latest_needed_date=latest,
                latest_allowed_date=latest,
            )
        )
    if vix_symbols:
        latest_vix_date = latest_confirmed_vix_date(now)
        history.update(
            fetch_yahoo_dated_ohlc_cached(
                vix_symbols,
                days=days,
                cache_dir=cache_dir,
                earliest_needed_date=earliest,
                latest_needed_date=latest_vix_date,
                latest_allowed_date=latest_vix_date,
                refresh_latest_date=True,
            )
        )
    return {symbol: max(bars) for symbol, bars in history.items() if bars}


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    parser = argparse.ArgumentParser(description="Yahoo日足キャッシュを最新日まで更新します")
    parser.add_argument("--days", type=int, default=120, help="取得する暦日数(既定: 120)")
    parser.add_argument("--symbols", nargs="*", default=None, help="対象銘柄(省略時はキャッシュ内の全銘柄)")
    parser.add_argument("--cache-dir", type=Path, default=DEFAULT_CACHE_DIR)
    args = parser.parse_args(argv)

    symbols = args.symbols or discover_symbols(args.cache_dir)
    if not symbols:
        logger.error("対象銘柄がありません。--symbols を指定してください。")
        return 1
    now = datetime.now()
    latest_by_symbol = update_cache(args.cache_dir, symbols, args.days, now)
    missing = [symbol for symbol in symbols if symbol not in latest_by_symbol]
    expected_by_symbol = {
        symbol: latest_confirmed_trading_day(now).isoformat()
        for symbol in symbols
    }
    if "^VIX" in expected_by_symbol:
        expected_by_symbol["^VIX"] = latest_confirmed_vix_date(now).isoformat()
    behind = sorted(
        symbol
        for symbol, latest in latest_by_symbol.items()
        if latest < expected_by_symbol[symbol]
    )
    logger.info(
        "更新完了: 対象=%s銘柄 / 国内日足目標日=%s / VIX目標日=%s "
        "/ 最新日が目標に満たない=%s銘柄 / 取得できず=%s銘柄",
        len(symbols),
        latest_confirmed_trading_day(now),
        expected_by_symbol.get("^VIX", "対象外"),
        len(behind),
        len(missing),
    )
    if latest_by_symbol:
        logger.info("キャッシュの最新日: 最小=%s 最大=%s", min(latest_by_symbol.values()), max(latest_by_symbol.values()))
    if missing:
        logger.warning("取得できなかった銘柄: %s", ", ".join(missing))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
