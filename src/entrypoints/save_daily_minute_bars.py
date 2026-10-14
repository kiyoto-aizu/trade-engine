"""当日の対象銘柄の1分足をYahooから取得してParquetへ保存する(日次分析の引け後に相乗りして実行)。

手動実行: python -m src.entrypoints.save_daily_minute_bars [--date YYYY-MM-DD] [--include-price-bands]
"""
from __future__ import annotations

import argparse
import json
import logging
from datetime import date, datetime
from pathlib import Path
from time import perf_counter

from src.application.daily_minute_bar_save_usecase import DailyMinuteBarSaveResult, DailyMinuteBarSaveUseCase
from src.config import config
from src.entrypoints.update_daily_bar_cache import latest_confirmed_trading_day
from src.infrastructure.calendar.japanese_calendar import is_trading_day
from src.infrastructure.logging_config import configure_logging
from src.infrastructure.market_data.get_intraday_bars import fetch_yahoo_intraday_bars
from src.infrastructure.persistence.parquet_minute_bar_repository import ParquetMinuteBarRepository

logger = logging.getLogger(__name__)


def _symbols_in(path: Path) -> list[str]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    symbols = data.get("symbols", []) if isinstance(data, dict) else data
    return [str(symbol) for symbol in symbols] if isinstance(symbols, list) else []


def collect_daily_symbols(
    trade_date: date,
    *,
    filtering_dir: Path | None = None,
    include_price_bands: bool | None = None,
) -> list[str]:
    """当日の通常フィルタ結果の銘柄。オプションで価格帯別フィルタ結果の銘柄も含める。"""
    file_name = f"{trade_date.isoformat()}.json"
    directories = [Path(filtering_dir or config.FILTERING_RESULT_DIRECTORY)]
    if config.DAILY_MINUTE_BAR_INCLUDE_PRICE_BANDS if include_price_bands is None else include_price_bands:
        directories.extend(
            Path(config.FILTERING_PRICE_BAND_RESULT_ROOT) / f"{cap:g}"
            for cap in config.SCREENING_ALTERNATE_PRICE_CAPS
        )
    symbols: set[str] = set()
    for directory in directories:
        symbols.update(_symbols_in(directory / file_name))
    return sorted(symbols)


def save_daily_minute_bars(
    trade_date: date,
    *,
    now: datetime | None = None,
    filtering_dir: Path | None = None,
    output_dir: Path | None = None,
    include_price_bands: bool | None = None,
    fetch_bars=fetch_yahoo_intraday_bars,
    **usecase_options,
) -> DailyMinuteBarSaveResult | None:
    """失敗しても例外を投げず、理由をログへ残す。日次分析など他の処理を止めないために使う。

    取引日の引け後(確定日)だけ動かし、休場日・引け前・対象銘柄なしは何もせずNoneを返す。
    """
    try:
        current = now or datetime.now()
        if not is_trading_day(trade_date):
            logger.info("休場日のため当日分足の保存をスキップします: %s", trade_date)
            return None
        if latest_confirmed_trading_day(current) < trade_date:
            logger.info("引け前のため当日分足の保存をスキップします: %s", trade_date)
            return None
        symbols = collect_daily_symbols(
            trade_date, filtering_dir=filtering_dir, include_price_bands=include_price_bands
        )
        if not symbols:
            logger.info("当日のフィルタ結果がないため分足の保存を行いません: %s", trade_date)
            return None
        use_case = DailyMinuteBarSaveUseCase(
            fetch_bars,
            ParquetMinuteBarRepository(output_dir or config.MINUTE_BAR_PARQUET_DIR),
            **usecase_options,
        )
        started = perf_counter()
        result = use_case.run(trade_date, symbols)
        logger.info(
            "当日分足の保存結果: %s | 所要時間=%.1f秒", result.summary_line(), perf_counter() - started
        )
        return result
    except Exception:
        logger.exception("当日分足の保存処理で例外が発生しました")
        return None


def main(argv: list[str] | None = None) -> int:
    configure_logging()
    parser = argparse.ArgumentParser(description="当日の対象銘柄の1分足を保存します")
    parser.add_argument("--date", type=date.fromisoformat, default=date.today())
    parser.add_argument("--include-price-bands", action="store_true", help="価格帯別フィルタ結果の銘柄も含める")
    args = parser.parse_args(argv)
    result = save_daily_minute_bars(args.date, include_price_bands=args.include_price_bands or None)
    if result is None:
        return 0
    for item in result.symbols:
        print(f"{item.symbol}: {item.status} {item.bar_count}本 {item.first_time}〜{item.last_time} {item.reason or ''}")
    print(result.summary_line())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
