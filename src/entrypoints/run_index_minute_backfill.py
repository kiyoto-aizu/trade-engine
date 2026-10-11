"""日経225の1分足をYahooから取得してParquetへ保存する(CAUTION検証用の記録)。

引け後に日次分析から呼ばれるほか、取得可能な期間(約30日)を手動で遡って保存できる。
"""
from __future__ import annotations

import argparse
import logging
from datetime import date, timedelta

from src.application.index_minute_bar_save_usecase import IndexMinuteBarSaveResult, IndexMinuteBarSaveUseCase
from src.config import config
from src.infrastructure.logging_config import configure_logging
from src.infrastructure.market_data.get_intraday_bars import fetch_yahoo_index_intraday_bars
from src.infrastructure.persistence.parquet_minute_bar_repository import ParquetMinuteBarRepository

logger = logging.getLogger(__name__)


def save_index_minute_bars(start: date, end: date) -> IndexMinuteBarSaveResult | None:
    """失敗しても例外を投げず、理由をログへ残す。日次分析など他の処理を止めないために使う。"""
    try:
        use_case = IndexMinuteBarSaveUseCase(
            fetch_yahoo_index_intraday_bars,
            ParquetMinuteBarRepository(config.INDEX_MINUTE_BAR_PARQUET_DIR),
        )
        result = use_case.run(start, end)
    except Exception:
        logger.exception("指数分足の保存処理で例外が発生しました")
        return None
    if result.failures:
        logger.warning("指数分足の保存で失敗がありました: %s", result.failures)
    return result


def main(argv: list[str] | None = None) -> int:
    configure_logging()
    parser = argparse.ArgumentParser(description="日経225の1分足を保存します")
    parser.add_argument("--date", type=date.fromisoformat, default=date.today(), help="終了日(既定: 今日)")
    parser.add_argument("--days", type=int, default=1, help="終了日から遡る日数(カレンダー日。Yahooは約30日まで)")
    args = parser.parse_args(argv)
    result = save_index_minute_bars(args.date - timedelta(days=max(args.days, 1) - 1), args.date)
    if result is None:
        return 1
    for saved_date, count in sorted(result.saved_counts.items()):
        print(f"{saved_date}: {count}本")
    for failure in result.failures:
        print(f"失敗: {failure}")
    return 1 if result.failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
