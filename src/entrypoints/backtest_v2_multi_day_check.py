"""使い捨て検証スクリプト: TradingUseCaseを複数営業日でヒストリカル実行する。"""
from __future__ import annotations

import argparse
import json
import logging
import math
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
from src.infrastructure.persistence.storage import write_json

logger = logging.getLogger(__name__)
PROJECT_ROOT = Path(__file__).resolve().parents[2]
SCRATCH_ROOT = PROJECT_ROOT / "data" / "backtest_v2_scratch"
FILTERING_DIRECTORY = PROJECT_ROOT / "data" / "filtering"
MINUTE_BAR_DIRECTORY = PROJECT_ROOT / "data" / "minute_bars_parquet"
MARKET_CLOSE_SENTINEL = time(15, 30, 1)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="TradingUseCaseを複数営業日で疑似実行します")
    parser.add_argument(
        "--start-date",
        type=date.fromisoformat,
        default=date(2026, 9, 1),
        help="対象期間の開始日 (YYYY-MM-DD)",
    )
    parser.add_argument(
        "--end-date",
        type=date.fromisoformat,
        default=date(2026, 9, 25),
        help="対象期間の終了日 (YYYY-MM-DD)",
    )
    parser.add_argument(
        "--use-real-filter-decision-repository",
        action="store_true",
        help="NoOpとSQLite Repositoryの両方を実行して時間を比較します",
    )
    return parser


def _check_runtime_safety() -> None:
    if config.ALLOW_OVERNIGHT_HOLDING:
        raise RuntimeError("複数日検証は日次強制決済が前提です。ALLOW_OVERNIGHT_HOLDING=falseにしてください")
    if config.EMERGENCY_STOP_FILE.exists():
        raise RuntimeError(f"緊急停止フラグが存在するため実行しません: {config.EMERGENCY_STOP_FILE}")


def _discover_days(start_date: date, end_date: date) -> tuple[dict[date, list[str]], list[dict]]:
    if start_date > end_date:
        raise ValueError("--start-dateは--end-date以前の日付にしてください")

    repository = FilteringResultRepository(FILTERING_DIRECTORY)
    target_days: dict[date, list[str]] = {}
    skipped_days = []
    current = start_date
    while current <= end_date:
        if not is_trading_day(current):
            skipped_days.append({"date": current.isoformat(), "reason": "non_trading_day"})
            current = current.fromordinal(current.toordinal() + 1)
            continue

        result = repository.load_for_date(current)
        if result is None or not result.symbols:
            skipped_days.append({"date": current.isoformat(), "reason": "filtering_result_missing"})
            logger.info("フィルタ結果がないため営業日をスキップします: %s", current)
        elif result.date != current.isoformat():
            raise ValueError(
                f"フィルタ結果の日付が対象日と一致しません: {result.date} != {current}"
            )
        else:
            probe = datetime.combine(current, time(10, 0))
            if not is_trading_session(
                probe,
                config.MARKET_OPEN_HOUR,
                config.MARKET_OPEN_MINUTE,
                config.MARKET_CLOSE_HOUR,
                config.MARKET_CLOSE_MINUTE,
            ):
                skipped_days.append({"date": current.isoformat(), "reason": "outside_trading_session"})
            else:
                target_days[current] = result.symbols
        current = current.fromordinal(current.toordinal() + 1)

    return target_days, skipped_days


def _load_day_minute_bars(
    target_date: date,
    symbols: list[str],
) -> tuple[dict[str, list], list[datetime], list[str]]:
    repository = ParquetMinuteBarRepository(MINUTE_BAR_DIRECTORY)
    minute_bars: dict[str, list] = {}
    timestamps = set()
    missing_symbols = []
    for symbol in symbols:
        bars = repository.load_bars(target_date, symbol)
        minute_bars[symbol] = bars
        if not bars:
            missing_symbols.append(symbol)
            logger.warning("分足がない銘柄を当日の評価対象から除外します: date=%s symbol=%s", target_date, symbol)
        timestamps.update(datetime.fromisoformat(bar.time) for bar in bars)

    if not timestamps:
        return minute_bars, [], missing_symbols
    timestamps.add(datetime.combine(target_date, MARKET_CLOSE_SENTINEL))
    return minute_bars, sorted(timestamps), missing_symbols


def _load_daily_bars(
    target_days: dict[date, list[str]],
) -> tuple[dict[str, list[DatedDailyBar]], int, list[dict], float]:
    symbols = sorted({symbol for day_symbols in target_days.values() for symbol in day_symbols})
    earliest_simulated_date = min(target_days)
    latest_simulated_date = max(target_days)
    history_days = calculate_required_fetch_days(earliest_simulated_date)
    all_symbols = [*symbols, "^N225", "^VIX"]
    daily_history_started_at = perf_counter()
    fetched = fetch_yahoo_dated_ohlc_cached(
        all_symbols,
        days=history_days,
        cache_dir=PROJECT_ROOT / "data" / "cache" / "yahoo_daily",
        earliest_needed_date=earliest_simulated_date,
        latest_needed_date=latest_simulated_date,
    )
    daily_history_elapsed_seconds = perf_counter() - daily_history_started_at
    daily_bars: dict[str, list[DatedDailyBar]] = {}
    first_use_dates = {
        symbol: min(day for day, day_symbols in target_days.items() if symbol in day_symbols)
        for symbol in symbols
    }
    daily_history_shortfalls = []
    for symbol in symbols:
        rows = fetched.get(symbol, {})
        bars = [
            DatedDailyBar(
                date=date.fromisoformat(date_text),
                open=bar.open,
                high=bar.high,
                low=bar.low,
                close=bar.close,
            )
            for date_text, bar in sorted(rows.items())
        ]
        daily_bars[symbol] = bars
        first_use_date = first_use_dates[symbol]
        available_bars = sum(bar.date < first_use_date for bar in bars)
        if available_bars < 5:
            raise RuntimeError(
                f"{symbol}は初回対象日{first_use_date}より前の日足が5本未満です: {available_bars}本"
            )
        if available_bars < config.RSI_MINIMUM_CLOSES:
            shortfall = {
                "symbol": symbol,
                "first_target_date": first_use_date.isoformat(),
                "available_daily_bars": available_bars,
                "rsi_minimum_closes": config.RSI_MINIMUM_CLOSES,
            }
            daily_history_shortfalls.append(shortfall)
            logger.warning(
                "初回対象日時点でRSIに必要な日足本数が不足します。RSI条件は未評価になります: %s",
                shortfall,
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
        index_bars = [
            DatedDailyBar(
                date=bar.date,
                open=bar.open,
                high=bar.high,
                low=bar.low,
                close=bar.close,
            )
            for bar in bars
        ]
        if sum(
            bar.date < earliest_simulated_date and bar.open is not None
            for bar in index_bars
        ) < config.MARKET_REGIME_REALIZED_VOL_WINDOW + 2:
            raise RuntimeError(f"期間開始日以前のMarketRegime指数履歴が不足しています: {symbol}")
        daily_bars[symbol] = index_bars
    return daily_bars, history_days, daily_history_shortfalls, daily_history_elapsed_seconds


def _calculate_realized_pnl_from_orders(orders: list[dict]) -> float:
    """PaperOrderClientの約定記録から手数料込み実現損益を独立再計算する。"""
    holdings: dict[str, int] = {}
    average_costs: dict[str, float] = {}
    realized_pnl = 0.0
    for order in orders:
        symbol = str(order["Symbol"])
        side = str(order["Side"])
        quantity = int(order["Qty"])
        price = float(order["Price"])
        fee = float(order["Fee"])
        held_quantity = holdings.get(symbol, 0)
        if side == config.OrderSide.BUY.value:
            new_quantity = held_quantity + quantity
            total_cost = average_costs.get(symbol, 0.0) * held_quantity + price * quantity + fee
            holdings[symbol] = new_quantity
            average_costs[symbol] = total_cost / new_quantity
        elif side == config.OrderSide.SELL.value:
            if held_quantity < quantity:
                raise AssertionError(f"約定記録に保有数量を超える売りがあります: {symbol}")
            realized_pnl += (price - average_costs[symbol]) * quantity - fee
            remaining = held_quantity - quantity
            if remaining:
                holdings[symbol] = remaining
            else:
                holdings.pop(symbol)
                average_costs.pop(symbol)
        else:
            raise ValueError(f"未対応のPaperOrderClient sideです: {side}")
    if holdings:
        raise AssertionError(f"営業日終了後に約定記録上の保有が残っています: {holdings}")
    return round(realized_pnl, 2)


def _noop_notifier(message: str) -> None:
    del message


def _run_period(
    target_days: dict[date, list[str]],
    day_inputs: dict[date, dict],
    daily_bars: dict[str, list[DatedDailyBar]],
    run_directory: Path,
    order_history_path: Path,
    use_real_filter_repository: bool,
) -> dict:
    run_directory.mkdir(parents=True, exist_ok=True)
    daily_report_directory = run_directory / "reports"
    baseline_path = run_directory / "kill_switch_baseline.json"
    filter_repository = (
        FilterDecisionRepository(run_directory / "filter_decision_events.sqlite3")
        if use_real_filter_repository
        else NoOpFilterDecisionRepository()
    )
    current_day = [min(target_days)]
    order_sender = PaperOrderClient(
        prices={},
        cash=config.OPERATING_CAPITAL,
        state_path=None,
        realized_pnl_date=current_day[0].isoformat(),
        today_provider=lambda: current_day[0],
    )
    day_summaries = []
    cumulative_realized_pnl = 0.0
    independent_cumulative_realized_pnl = 0.0
    started_at = perf_counter()

    for target_date, symbols in target_days.items():
        current_day[0] = target_date
        order_sender.today_provider = lambda day=target_date: day
        if order_sender.get_positions("historical-replay"):
            raise AssertionError(f"営業日開始時に前日の保有が残っています: {target_date}")

        day_input = day_inputs[target_date]
        clock = HistoricalClock(target_date, day_input["timestamps"])
        market_data_client = HistoricalMarketDataClient(daily_bars, clock)
        market_regime_usecase = MarketRegimeUseCase(
            market_data_client=market_data_client,
            thresholds=config.MARKET_REGIME_THRESHOLDS,
            realized_volatility_window=config.MARKET_REGIME_REALIZED_VOL_WINDOW,
            data_range=config.MARKET_REGIME_DATA_RANGE,
            adx_threshold=config.MARKET_REGIME_ADX_TREND_THRESHOLD,
            as_of_date=clock.current_date(),
        )
        usecase = TradingUseCase(
            token="historical-replay",
            order_history_path=order_history_path,
            kill_switch_baseline_path=baseline_path,
            market_data_client=market_data_client,
            board_client=HistoricalBoardClient(day_input["minute_bars"], clock),
            filtering_result_repository=HistoricalFilteringResultRepository(
                FILTERING_DIRECTORY,
                clock,
            ),
            order_sender=order_sender,
            notifier=_noop_notifier,
            daily_analyzer=NoOpDailyAnalyzer(),
            daily_report_directory=daily_report_directory,
            market_regime_usecase=market_regime_usecase,
            filter_decision_repository=filter_repository,
        )

        first_execution_order = len(order_sender.orders)
        cash_before = order_sender.cash
        with patch.object(
            trading_usecase_module,
            "get_api_soft_limit",
            return_value=config.API_SOFT_LIMIT,
        ):
            usecase.run(now_provider=clock.now, sleep=clock.advance)
        execution_orders = order_sender.orders[first_execution_order:]
        daily_realized_pnl = order_sender.get_daily_realized_pnl()
        reconstructed_daily_pnl = _calculate_realized_pnl_from_orders(execution_orders)
        if not math.isclose(daily_realized_pnl, reconstructed_daily_pnl, abs_tol=0.01):
            raise AssertionError(
                f"日次実現損益が約定記録と一致しません: {target_date} "
                f"PaperOrderClient={daily_realized_pnl:.2f}, reconstructed={reconstructed_daily_pnl:.2f}"
            )
        if order_sender.realized_pnl_date != target_date.isoformat():
            raise AssertionError(
                f"realized_pnl_dateが対象日に更新されていません: "
                f"{order_sender.realized_pnl_date} != {target_date}"
            )
        cumulative_realized_pnl += daily_realized_pnl
        independent_cumulative_realized_pnl += reconstructed_daily_pnl

        daily_history_entries = [
            entry for entry in usecase.order_history
            if entry.timestamp.startswith(target_date.isoformat())
        ]
        sell_reasons = Counter(
            entry.decision_reason or "未設定"
            for entry in daily_history_entries
            if entry.side == config.OrderSide.SELL
        )
        positions_after = order_sender.get_positions("historical-replay")
        if positions_after:
            raise AssertionError(f"営業日終了後に保有が残っています: {target_date}: {positions_after}")

        assessment = usecase.market_regime_assessment
        day_summaries.append({
            "date": target_date.isoformat(),
            "symbol_count": len(symbols),
            "symbols_without_minute_bars": day_input["missing_symbols"],
            "minute_bar_count": sum(len(bars) for bars in day_input["minute_bars"].values()),
            "evaluation_cycles": max(1, len(day_input["timestamps"]) - 1),
            "order_event_count": len(daily_history_entries),
            "sell_decision_reason_counts": dict(sell_reasons),
            "daily_realized_pnl": daily_realized_pnl,
            "independently_reconstructed_realized_pnl": reconstructed_daily_pnl,
            "cash_before": round(cash_before, 2),
            "cash_after": round(order_sender.cash, 2),
            "open_positions_after_close": len(positions_after),
            "realized_pnl_date": order_sender.realized_pnl_date,
            "market_regime": usecase.market_regime.value,
            "market_regime_available": bool(getattr(assessment, "data_available", False)),
            "market_regime_failure_reason": getattr(assessment, "failure_reason", None),
        })

    elapsed_seconds = perf_counter() - started_at
    if not math.isclose(
        cumulative_realized_pnl,
        independent_cumulative_realized_pnl,
        abs_tol=0.01,
    ):
        raise AssertionError(
            "累積実現損益が全SELL約定の独立集計と一致しません: "
            f"PaperOrderClient={cumulative_realized_pnl:.2f}, "
            f"reconstructed={independent_cumulative_realized_pnl:.2f}"
        )
    return {
        "filter_decision_repository": "sqlite" if use_real_filter_repository else "noop",
        "elapsed_seconds": round(elapsed_seconds, 3),
        "days_completed": len(day_summaries),
        "order_event_count": sum(day["order_event_count"] for day in day_summaries),
        "cumulative_realized_pnl": round(cumulative_realized_pnl, 2),
        "independently_reconstructed_realized_pnl": round(independent_cumulative_realized_pnl, 2),
        "final_cash": round(order_sender.cash, 2),
        "final_positions": order_sender.get_positions("historical-replay"),
        "regime_counts": dict(Counter(day["market_regime"] for day in day_summaries)),
        "daily_summaries": day_summaries,
        "order_history_path": str(order_history_path),
    }


def main() -> None:
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    total_started_at = perf_counter()
    args = _build_parser().parse_args()
    _check_runtime_safety()

    discovered_days, skipped_days = _discover_days(args.start_date, args.end_date)
    if not discovered_days:
        raise ValueError("対象期間に稼働日カレンダーとフィルタ結果の両方を満たす日がありません")

    day_inputs = {}
    for target_date, symbols in discovered_days.items():
        minute_bars, timestamps, missing_symbols = _load_day_minute_bars(target_date, symbols)
        if not timestamps:
            skipped_days.append({"date": target_date.isoformat(), "reason": "minute_bars_missing"})
            logger.warning("全銘柄の分足がないため日付をスキップします: %s", target_date)
            continue
        day_inputs[target_date] = {
            "minute_bars": minute_bars,
            "timestamps": timestamps,
            "missing_symbols": missing_symbols,
        }
    target_days = {day: discovered_days[day] for day in day_inputs}
    if not target_days:
        raise ValueError("フィルタ結果のある営業日に分足データがありません")

    daily_bars, history_days, daily_history_shortfalls, daily_history_elapsed_seconds = _load_daily_bars(target_days)
    SCRATCH_ROOT.mkdir(parents=True, exist_ok=True)
    run_id = uuid4().hex
    main_order_history_path = SCRATCH_ROOT / "multi_day_order_history.json"
    main_run_directory = SCRATCH_ROOT / "multi_day_runs" / run_id / "noop"
    write_json(main_order_history_path, [])
    main_run_directory.mkdir(parents=True, exist_ok=True)
    modes = [(False, main_order_history_path, main_run_directory)]
    if args.use_real_filter_decision_repository:
        comparison_directory = SCRATCH_ROOT / "multi_day_runs" / run_id / "sqlite"
        comparison_order_history = comparison_directory / "multi_day_order_history.json"
        write_json(comparison_order_history, [])
        modes.append((True, comparison_order_history, comparison_directory))

    results = []
    for use_real_repository, order_history_path, run_directory in modes:
        results.append(_run_period(
            target_days,
            day_inputs,
            daily_bars,
            run_directory,
            order_history_path,
            use_real_repository,
        ))
    total_elapsed_seconds = perf_counter() - total_started_at
    simulation_elapsed_seconds = sum(result["elapsed_seconds"] for result in results)

    repository_comparison = None
    if args.use_real_filter_decision_repository:
        noop_seconds = results[0]["elapsed_seconds"]
        sqlite_seconds = results[1]["elapsed_seconds"]
        repository_comparison = {
            "sqlite_minus_noop_seconds": round(sqlite_seconds - noop_seconds, 3),
            "sqlite_over_noop_ratio": round(sqlite_seconds / noop_seconds, 3) if noop_seconds else None,
        }

    total_symbol_ticks = sum(
        day_summary["symbol_count"] * day_summary["evaluation_cycles"]
        for day_summary in results[0]["daily_summaries"]
    )
    single_day_estimate = total_symbol_ticks * 0.661 / 3_080
    summary = {
        "start_date": args.start_date.isoformat(),
        "end_date": args.end_date.isoformat(),
        "allow_overnight_holding": config.ALLOW_OVERNIGHT_HOLDING,
        "history_days_requested": history_days,
        "daily_history_load_elapsed_seconds": round(daily_history_elapsed_seconds, 3),
        "daily_history_shortfalls": daily_history_shortfalls,
        "days_skipped": skipped_days,
        "days_executed": len(target_days),
        "total_symbol_ticks": total_symbol_ticks,
        "total_elapsed_seconds": round(total_elapsed_seconds, 3),
        "simulation_elapsed_seconds": round(simulation_elapsed_seconds, 3),
        "preparation_and_reporting_elapsed_seconds": round(
            max(0.0, total_elapsed_seconds - simulation_elapsed_seconds),
            3,
        ),
        "single_day_linear_estimate_seconds": round(single_day_estimate, 3),
        "results": results,
        "repository_comparison": repository_comparison,
        "sell_pnl_cross_check_passed": all(
            math.isclose(
                result["cumulative_realized_pnl"],
                result["independently_reconstructed_realized_pnl"],
                abs_tol=0.01,
            )
            for result in results
        ),
        "summary_path": str(SCRATCH_ROOT / "multi_day_summary.json"),
    }
    write_json(SCRATCH_ROOT / "multi_day_summary.json", summary)
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()