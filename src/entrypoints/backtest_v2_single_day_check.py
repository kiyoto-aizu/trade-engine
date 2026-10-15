"""使い捨て検証スクリプト: TradingUseCaseを1営業日だけヒストリカル実行する。"""
from __future__ import annotations

import argparse
import json
import logging
from collections import Counter
from datetime import date, datetime, time
from pathlib import Path
from time import perf_counter
from unittest.mock import patch
from uuid import uuid4

from src.application import trading_usecase as trading_usecase_module
from src.application.market_regime_usecase import MarketRegimeUseCase
from src.application.trading_usecase import TradingUseCase
from src.config import config
from src.infrastructure.backtest.historical_clients import (
    DatedDailyBar,
    HistoricalBoardClient,
    HistoricalClock,
    HistoricalFilteringResultRepository,
    HistoricalMarketDataClient,
    NoOpDailyAnalyzer,
    NoOpFilterDecisionRepository,
)
from src.infrastructure.calendar.japanese_calendar import is_trading_day, is_trading_session
from src.infrastructure.market_data.yahoo_backtest_history_client import calculate_required_fetch_days
from src.infrastructure.market_data.yahoo_daily_bar_cache import fetch_yahoo_dated_ohlc_cached
from src.infrastructure.paper.paper_order_client import PaperOrderClient
from src.infrastructure.persistence.filter_decision_repository import FilterDecisionRepository
from src.infrastructure.persistence.filtering_result_repository import FilteringResultRepository
from src.infrastructure.persistence.parquet_minute_bar_repository import ParquetMinuteBarRepository

logger = logging.getLogger(__name__)
PROJECT_ROOT = Path(__file__).resolve().parents[2]
EXPECTED_COMPARISON_TICKS = 246_000
MARKET_CLOSE_SENTINEL = time(15, 30, 1)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="TradingUseCaseを単一営業日で疑似実行します")
    parser.add_argument(
        "--date",
        type=date.fromisoformat,
        default=date(2026, 9, 25),
        help="対象営業日 (YYYY-MM-DD、既定: 2026-09-25)",
    )
    parser.add_argument(
        "--use-real-filter-decision-repository",
        action="store_true",
        help="NoOpとSQLite Repositoryを両方実行し、実行時間を比較します",
    )
    return parser


def _validate_target_date(target_date: date) -> None:
    probe = datetime.combine(target_date, time(10, 0))
    if not is_trading_day(target_date) or not is_trading_session(
        probe,
        config.MARKET_OPEN_HOUR,
        config.MARKET_OPEN_MINUTE,
        config.MARKET_CLOSE_HOUR,
        config.MARKET_CLOSE_MINUTE,
    ):
        raise ValueError(f"対象日は日本市場の営業日ではありません: {target_date}")
    if config.EMERGENCY_STOP_FILE.exists():
        raise RuntimeError(f"緊急停止フラグが存在するため実行しません: {config.EMERGENCY_STOP_FILE}")


def _load_filter_result(target_date: date):
    repository = FilteringResultRepository(PROJECT_ROOT / "data" / "filtering")
    result = repository.load_for_date(target_date)
    if result is None or not result.symbols:
        raise FileNotFoundError(f"対象日のフィルタ結果または監視銘柄がありません: {target_date}")
    if result.date != target_date.isoformat():
        raise ValueError(
            f"フィルタ結果の日付がファイル指定と一致しません: {result.date} != {target_date}"
        )
    return result


def _load_minute_bars(target_date: date, symbols: list[str]):
    repository = ParquetMinuteBarRepository(PROJECT_ROOT / "data" / "minute_bars_parquet")
    bars_by_symbol = {}
    missing_symbols = []
    timestamps = set()
    for symbol in symbols:
        bars = repository.load_bars(target_date, symbol)
        bars_by_symbol[symbol] = bars
        if not bars:
            missing_symbols.append(symbol)
            logger.warning("分足データがないため当日の評価対象外になります: %s", symbol)
        timestamps.update(datetime.fromisoformat(bar.time) for bar in bars)

    if not timestamps:
        raise ValueError(f"対象銘柄に分足データがありません: {target_date}")
    timestamps.add(datetime.combine(target_date, MARKET_CLOSE_SENTINEL))
    return bars_by_symbol, sorted(timestamps), missing_symbols


def _load_daily_bars(target_date: date, symbols: list[str]) -> dict[str, list[DatedDailyBar]]:
    required_days = calculate_required_fetch_days(target_date)
    all_symbols = [*symbols, "^N225", "^VIX"]
    fetched = fetch_yahoo_dated_ohlc_cached(
        all_symbols,
        days=required_days,
        cache_dir=PROJECT_ROOT / "data" / "cache" / "yahoo_daily",
        earliest_needed_date=target_date,
        latest_needed_date=target_date,
    )
    daily_bars: dict[str, list[DatedDailyBar]] = {}
    minimum_closes = config.RSI_MINIMUM_CLOSES
    missing_symbols = []
    for symbol in symbols:
        rows = fetched.get(symbol, {})
        bars = [
            DatedDailyBar(
                date=date.fromisoformat(date_text),
                high=bar.high,
                low=bar.low,
                close=bar.close,
                open=bar.open,
            )
            for date_text, bar in sorted(rows.items())
        ]
        daily_bars[symbol] = bars
        if sum(bar.date < target_date for bar in bars) < minimum_closes:
            missing_symbols.append(symbol)

    if missing_symbols:
        raise RuntimeError(
            "対象日以前の確定日足が不足しています "
            f"(必要={minimum_closes}本以上): {', '.join(missing_symbols)}"
        )

    for symbol in ("^N225", "^VIX"):
        bars = [
            DatedDailyBar(
                date=date.fromisoformat(date_text),
                open=bar.open,
                high=bar.high,
                low=bar.low,
                close=bar.close,
            )
            for date_text, bar in sorted(fetched.get(symbol, {}).items())
        ]
        if not any(bar.date < target_date and bar.open is not None for bar in bars):
            raise RuntimeError(f"対象日以前の指数日足がありません: {symbol}")
        daily_bars[symbol] = bars
    return daily_bars


def _noop_notifier(message: str) -> None:
    del message


def _build_usecase(
    target_date: date,
    minute_bars: dict,
    timestamps: list[datetime],
    daily_bars: dict[str, list[DatedDailyBar]],
    run_directory: Path,
    use_real_filter_repository: bool,
) -> tuple[TradingUseCase, HistoricalClock]:
    clock = HistoricalClock(target_date, timestamps)
    market_data_client = HistoricalMarketDataClient(daily_bars, clock)
    filter_repository = (
        FilterDecisionRepository(run_directory / "filter_decision_events.sqlite3")
        if use_real_filter_repository
        else NoOpFilterDecisionRepository()
    )
    market_regime_usecase = MarketRegimeUseCase(
        market_data_client=market_data_client,
        thresholds=config.MARKET_REGIME_THRESHOLDS,
        realized_volatility_window=config.MARKET_REGIME_REALIZED_VOL_WINDOW,
        data_range=config.MARKET_REGIME_DATA_RANGE,
        adx_threshold=config.MARKET_REGIME_ADX_TREND_THRESHOLD,
        as_of_date=clock.current_date(),
    )
    order_sender = PaperOrderClient(
        prices={},
        cash=config.OPERATING_CAPITAL,
        state_path=None,
        realized_pnl_date=target_date.isoformat(),
        today_provider=clock.current_date,
    )
    usecase = TradingUseCase(
        token="historical-replay",
        order_history_path=run_directory / f"{target_date.isoformat()}_order_history.json",
        kill_switch_baseline_path=run_directory / "kill_switch_baseline.json",
        market_data_client=market_data_client,
        board_client=HistoricalBoardClient(minute_bars, clock),
        filtering_result_repository=HistoricalFilteringResultRepository(
            PROJECT_ROOT / "data" / "filtering",
            clock,
        ),
        order_sender=order_sender,
        notifier=_noop_notifier,
        daily_analyzer=NoOpDailyAnalyzer(),
        daily_report_directory=run_directory / "reports",
        market_regime_usecase=market_regime_usecase,
        filter_decision_repository=filter_repository,
    )
    return usecase, clock


def _run_once(
    target_date: date,
    symbols: list[str],
    minute_bars: dict,
    timestamps: list[datetime],
    daily_bars: dict[str, list[DatedDailyBar]],
    run_directory: Path,
    use_real_filter_repository: bool,
) -> dict:
    run_directory.mkdir(parents=True, exist_ok=False)
    usecase, clock = _build_usecase(
        target_date,
        minute_bars,
        timestamps,
        daily_bars,
        run_directory,
        use_real_filter_repository,
    )
    started_at = perf_counter()
    with patch.object(
        trading_usecase_module,
        "get_api_soft_limit",
        return_value=config.API_SOFT_LIMIT,
    ):
        usecase.run(now_provider=clock.now, sleep=clock.advance)
    elapsed_seconds = perf_counter() - started_at

    sell_reasons = Counter(
        entry.decision_reason or "未設定"
        for entry in usecase.order_history
        if entry.side == config.OrderSide.SELL
    )
    manual_emergency_sells = sum(
        entry.side == config.OrderSide.SELL
        and entry.decision_reason == "手動緊急停止"
        for entry in daily_history_entries
    )
    evaluation_cycles = max(1, len(timestamps) - 1)
    evaluated_symbol_ticks = evaluation_cycles * len(symbols)
    projected_seconds = elapsed_seconds * EXPECTED_COMPARISON_TICKS / evaluated_symbol_ticks
    return {
        "filter_decision_repository": "sqlite" if use_real_filter_repository else "noop",
        "elapsed_seconds": round(elapsed_seconds, 3),
        "evaluation_cycles": evaluation_cycles,
        "evaluated_symbol_ticks": evaluated_symbol_ticks,
        "projected_seconds_for_246k_ticks": round(projected_seconds, 1),
        "order_event_count": len(usecase.order_history),
        "sell_decision_reason_counts": dict(sell_reasons),
        "manual_emergency_liquidation_count": manual_emergency_sells,
        "order_history_path": str(usecase.order_history_path),
        "report_directory": str(usecase.daily_report_directory),
    }


def main() -> None:
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    args = _build_parser().parse_args()
    target_date = args.date
    _validate_target_date(target_date)
    filtering_result = _load_filter_result(target_date)
    symbols = filtering_result.symbols
    minute_bars, timestamps, missing_symbols = _load_minute_bars(target_date, symbols)
    daily_history_started_at = perf_counter()
    daily_bars = _load_daily_bars(target_date, symbols)
    daily_history_elapsed_seconds = perf_counter() - daily_history_started_at

    run_id = uuid4().hex
    scratch_root = PROJECT_ROOT / "data" / "backtest_v2_scratch" / target_date.isoformat() / run_id
    run_modes = [False, True] if args.use_real_filter_decision_repository else [False]
    results = []
    for use_real_filter_repository in run_modes:
        mode = "sqlite" if use_real_filter_repository else "noop"
        results.append(_run_once(
            target_date,
            symbols,
            minute_bars,
            timestamps,
            daily_bars,
            scratch_root / mode,
            use_real_filter_repository,
        ))

    comparison = None
    if args.use_real_filter_decision_repository:
        noop_seconds = results[0]["elapsed_seconds"]
        sqlite_seconds = results[1]["elapsed_seconds"]
        comparison = {
            "sqlite_minus_noop_seconds": round(sqlite_seconds - noop_seconds, 3),
            "sqlite_over_noop_ratio": round(sqlite_seconds / noop_seconds, 3) if noop_seconds else None,
        }

    print(json.dumps({
        "target_date": target_date.isoformat(),
        "is_trading_day": True,
        "symbol_count": len(symbols),
        "symbols_without_minute_bars": missing_symbols,
        "minute_bar_count": sum(len(bars) for bars in minute_bars.values()),
        "daily_history_load_elapsed_seconds": round(daily_history_elapsed_seconds, 3),
        "clock_timestamp_count": len(timestamps),
        "daily_analyzer": "noop",
        "notifier": "noop",
        "results": results,
        "repository_comparison": comparison,
        "scratch_directory": str(scratch_root),
        "sell_reason_field_note": "OrderHistoryEntryにnote列がないためdecision_reason別に集計",
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()