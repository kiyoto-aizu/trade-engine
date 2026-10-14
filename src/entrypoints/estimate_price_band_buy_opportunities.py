"""保存済み選定結果へ現行の売買判断を適用する読み取り専用試算。"""
from __future__ import annotations

import argparse
import json
import logging
import tempfile
from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime, time
from pathlib import Path
from unittest.mock import patch

from src.application import trading_usecase as trading_usecase_module
from src.application.market_regime_usecase import MarketRegimeUseCase
from src.application.trading_usecase import TradingUseCase
from src.config import config
from src.domain.market_regime import MarketRegime, MarketRegimeAssessment
from src.domain.models import FilteringResult
from src.infrastructure.backtest.historical_clients import (
    DatedDailyBar,
    HistoricalBoardClient,
    HistoricalClock,
    HistoricalFilteringResultRepository,
    HistoricalMarketDataClient,
    NoOpDailyAnalyzer,
    NoOpFilterDecisionRepository,
)
from src.infrastructure.calendar.japanese_calendar import is_trading_day
from src.infrastructure.paper.paper_order_client import PaperOrderClient
from src.infrastructure.persistence.filtering_result_repository import FilteringResultRepository
from src.infrastructure.persistence.parquet_minute_bar_repository import ParquetMinuteBarRepository

logger = logging.getLogger(__name__)
PROJECT_ROOT = Path(__file__).resolve().parents[2]
FILTERING_DIRECTORIES = {
    270: PROJECT_ROOT / "data" / "filtering",
    450: PROJECT_ROOT / "data" / "filtering_price_bands" / "450",
    900: PROJECT_ROOT / "data" / "filtering_price_bands" / "900",
}
MINUTE_BAR_DIRECTORY = PROJECT_ROOT / "data" / "minute_bars_parquet"
DAILY_CACHE_DIRECTORY = PROJECT_ROOT / "data" / "cache" / "yahoo_daily"
DAILY_REPORT_DIRECTORY = PROJECT_ROOT / "data" / "reports" / "daily"
MARKET_CLOSE_SENTINEL = time(15, 30, 1)
EXPANDED_ORDER_CAPS = {270: None, 450: 50_000.0, 900: 100_000.0}


@dataclass(frozen=True)
class SizingScenario:
    name: str
    order_cap: float
    target_positions: int


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="保存済み選定結果へ現行の買い判断を適用して価格帯別に試算します"
    )
    parser.add_argument("--start-date", type=date.fromisoformat, default=date(2026, 9, 1))
    parser.add_argument("--end-date", type=date.fromisoformat, default=None)
    return parser


def _check_runtime_safety() -> None:
    if config.ALLOW_OVERNIGHT_HOLDING:
        raise RuntimeError("当日中の決済が前提です。ALLOW_OVERNIGHT_HOLDING=falseにしてください")
    if config.EMERGENCY_STOP_FILE.exists():
        raise RuntimeError(f"緊急停止フラグが存在するため試算しません: {config.EMERGENCY_STOP_FILE}")


def _load_filtering_result(directory: Path, target_date: date) -> FilteringResult | None:
    return FilteringResultRepository(directory).load_for_date(target_date)


def _discover_comparison_days(start_date: date, end_date: date | None) -> list[date]:
    if end_date is not None and start_date > end_date:
        raise ValueError("--start-dateは--end-date以前の日付にしてください")

    selected_days = []
    reference_directory = FILTERING_DIRECTORIES[450]
    comparison_directory = FILTERING_DIRECTORIES[270]
    for path in sorted(reference_directory.glob("*.json")):
        target_date = date.fromisoformat(path.stem)
        if target_date < start_date or (end_date is not None and target_date > end_date):
            continue
        if not is_trading_day(target_date):
            continue
        band_450 = _load_filtering_result(reference_directory, target_date)
        band_270 = _load_filtering_result(comparison_directory, target_date)
        if band_450 and band_450.symbols and band_270 and band_270.symbols:
            selected_days.append(target_date)
    return selected_days


def _load_market_assessment(target_date: date) -> MarketRegimeAssessment | None:
    path = DAILY_REPORT_DIRECTORY / f"{target_date.isoformat()}.json"
    if not path.exists():
        return None
    report = json.loads(path.read_text(encoding="utf-8"))
    if report.get("date") != target_date.isoformat():
        raise ValueError(f"日次レポートの日付が一致しません: {path}")
    conditions = report.get("market_conditions")
    if not isinstance(conditions, dict) or conditions.get("assessment_status") != "available":
        return None
    try:
        regime = MarketRegime(conditions["regime"])
        assessment = MarketRegimeAssessment(
            regime=regime,
            realized_volatility_percent=conditions["realized_volatility_percent"],
            vix=conditions["vix"],
            nikkei_change_percent=conditions["nikkei_change_percent"],
            data_available=bool(conditions["data_available"]),
            failure_reason=conditions.get("failure_reason"),
            adx=conditions.get("adx"),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError(f"保存済み市場状態の形式が不正です: {path}") from exc
    return assessment if assessment.data_available else None


def _load_daily_bars(symbol: str, target_date: date) -> list[DatedDailyBar]:
    path = DAILY_CACHE_DIRECTORY / f"{symbol}.json"
    if not path.exists():
        return []
    raw = json.loads(path.read_text(encoding="utf-8"))
    bars = []
    for date_text, values in sorted(raw.items()):
        bar_date = date.fromisoformat(date_text)
        if bar_date >= target_date:
            continue
        bars.append(DatedDailyBar(
            date=bar_date,
            high=float(values["high"]),
            low=float(values["low"]),
            close=float(values["close"]),
            open=float(values["open"]) if values.get("open") is not None else None,
        ))
    return bars


def _load_day_inputs(
    target_date: date,
    symbols: list[str],
) -> tuple[dict, dict[str, list[DatedDailyBar]], list[dict], list[datetime]]:
    minute_repository = ParquetMinuteBarRepository(MINUTE_BAR_DIRECTORY)
    minute_bars = {}
    daily_bars = {}
    exclusions = []
    timestamps: set[datetime] = set()
    required_daily_bars = max(5, config.RSI_MINIMUM_CLOSES, config.RSI_PERIOD + 1)
    for symbol in symbols:
        bars = minute_repository.load_bars(target_date, symbol)
        minute_bars[symbol] = bars
        if not bars:
            exclusions.append({"symbol": symbol, "reason": "minute_bars_missing"})
        else:
            timestamps.update(datetime.fromisoformat(bar.time) for bar in bars)

        history = _load_daily_bars(symbol, target_date)
        daily_bars[symbol] = history
        if len(history) < required_daily_bars:
            exclusions.append({
                "symbol": symbol,
                "reason": "daily_history_shortfall",
                "available_daily_bars": len(history),
                "required_daily_bars": required_daily_bars,
            })

    if timestamps:
        timestamps.add(datetime.combine(target_date, MARKET_CLOSE_SENTINEL))
    return minute_bars, daily_bars, exclusions, sorted(timestamps)


def _build_scenarios(band: int) -> tuple[SizingScenario, SizingScenario]:
    current = SizingScenario(
        name="current",
        order_cap=config.MAX_ORDER_AMOUNT_PER_TRADE,
        target_positions=config.TARGET_POSITIONS,
    )
    expanded_cap = EXPANDED_ORDER_CAPS[band] or config.MAX_ORDER_AMOUNT_PER_TRADE
    expanded_positions = (
        max(1, int(config.OPERATING_CAPITAL // expanded_cap))
        if EXPANDED_ORDER_CAPS[band] is not None
        else config.TARGET_POSITIONS
    )
    expanded = SizingScenario(
        name="band_adjusted",
        order_cap=expanded_cap,
        target_positions=expanded_positions,
    )
    return current, expanded


def _noop_notifier(_message: str) -> None:
    return None


def _realized_pnl_by_symbol(orders: list[dict]) -> dict[str, float]:
    holdings: dict[str, int] = {}
    costs: dict[str, float] = {}
    realized: dict[str, float] = Counter()
    for order in orders:
        symbol = str(order["Symbol"])
        quantity = int(order["Qty"])
        price = float(order["Price"])
        if order["Side"] == config.OrderSide.BUY.value:
            previous_quantity = holdings.get(symbol, 0)
            holdings[symbol] = previous_quantity + quantity
            costs[symbol] = costs.get(symbol, 0.0) + price * quantity
        elif order["Side"] == config.OrderSide.SELL.value:
            held_quantity = holdings.get(symbol, 0)
            if held_quantity < quantity:
                raise AssertionError(f"仮想約定に保有数量を超える売りがあります: {symbol}")
            allocated_cost = costs[symbol] * quantity / held_quantity
            realized[symbol] += price * quantity - allocated_cost
            remaining = held_quantity - quantity
            if remaining:
                holdings[symbol] = remaining
                costs[symbol] -= allocated_cost
            else:
                holdings.pop(symbol)
                costs.pop(symbol)
        else:
            raise ValueError(f"未対応のPaperOrderClient sideです: {order['Side']}")
    if holdings:
        raise AssertionError(f"試算終了後に保有が残っています: {holdings}")
    return {symbol: round(pnl, 2) for symbol, pnl in realized.items()}


def _run_day(
    target_date: date,
    band: int,
    scenario: SizingScenario,
    filtering_result: FilteringResult,
    assessment: MarketRegimeAssessment,
) -> dict:
    minute_bars, daily_bars, exclusions, timestamps = _load_day_inputs(
        target_date, filtering_result.symbols
    )
    if not timestamps:
        return {
            "date": target_date.isoformat(),
            "band": band,
            "scenario": scenario.name,
            "status": "no_minute_bars",
            "selected_count": len(filtering_result.symbols),
            "minute_bar_symbol_count": 0,
            "evaluable_symbol_count": 0,
            "cannot_simulate": exclusions,
        }

    eligible_symbols = [
        symbol for symbol in filtering_result.symbols
        if minute_bars[symbol]
        and len(daily_bars[symbol]) >= max(
            5, config.RSI_MINIMUM_CLOSES, config.RSI_PERIOD + 1
        )
    ]
    if not eligible_symbols:
        return {
            "date": target_date.isoformat(),
            "band": band,
            "scenario": scenario.name,
            "status": "no_evaluable_symbols",
            "selected_count": len(filtering_result.symbols),
            "minute_bar_symbol_count": sum(bool(bars) for bars in minute_bars.values()),
            "evaluable_symbol_count": 0,
            "cannot_simulate": exclusions,
        }

    with tempfile.TemporaryDirectory(prefix="price-band-estimate-") as temp_text:
        run_directory = Path(temp_text)
        filter_directory = run_directory / "filtering"
        replay_result = FilteringResult(
            date=filtering_result.date,
            symbols=eligible_symbols,
            generated_at=filtering_result.generated_at,
        )
        FilteringResultRepository(filter_directory).save(replay_result)
        clock = HistoricalClock(target_date, timestamps)
        market_data_client = HistoricalMarketDataClient(daily_bars, clock)
        market_regime_usecase = MarketRegimeUseCase(
            market_data_client=market_data_client,
            thresholds=config.MARKET_REGIME_THRESHOLDS,
            realized_volatility_window=config.MARKET_REGIME_REALIZED_VOL_WINDOW,
            data_range=config.MARKET_REGIME_DATA_RANGE,
            adx_threshold=config.MARKET_REGIME_ADX_TREND_THRESHOLD,
            as_of_date=clock.current_date(),
        )
        market_regime_usecase.execute = lambda: assessment
        order_sender = PaperOrderClient(
            prices={},
            cash=config.OPERATING_CAPITAL,
            fee_rate=0.0,
            market_slippage_bps=0.0,
            state_path=None,
            realized_pnl_date=target_date.isoformat(),
            today_provider=clock.current_date,
        )
        usecase = TradingUseCase(
            token="read-only-price-band-estimate",
            order_history_path=run_directory / "order_history.json",
            kill_switch_baseline_path=run_directory / "kill_switch_baseline.json",
            market_data_client=market_data_client,
            board_client=HistoricalBoardClient(minute_bars, clock),
            filtering_result_repository=HistoricalFilteringResultRepository(
                filter_directory, clock
            ),
            order_sender=order_sender,
            notifier=_noop_notifier,
            daily_analyzer=NoOpDailyAnalyzer(),
            daily_report_directory=run_directory / "reports",
            market_regime_usecase=market_regime_usecase,
            filter_decision_repository=NoOpFilterDecisionRepository(),
        )
        decision_reasons: dict[str, set[str]] = {}

        def capture_decision(
            symbol: str,
            reason_code: str,
            _occurred_at: datetime,
            **_inputs,
        ) -> None:
            decision_reasons.setdefault(symbol, set()).add(reason_code)

        usecase._journal_decision = capture_decision
        with (
            patch.object(config, "MAX_ORDER_AMOUNT_PER_TRADE", scenario.order_cap),
            patch.object(config, "TARGET_POSITIONS", scenario.target_positions),
            patch.object(config, "API_SOFT_LIMIT", max(config.API_SOFT_LIMIT, scenario.order_cap)),
            patch.object(
                trading_usecase_module,
                "get_api_soft_limit",
                return_value=max(config.API_SOFT_LIMIT, scenario.order_cap),
            ),
        ):
            usecase.run(now_provider=clock.now, sleep=clock.advance)

        pnl_by_symbol = _realized_pnl_by_symbol(order_sender.orders)
        buy_entries = [
            entry for entry in usecase.order_history
            if entry.timestamp.startswith(target_date.isoformat())
            and entry.side == config.OrderSide.BUY
        ]
        purchased_symbols = {entry.symbol for entry in buy_entries}
        money_block_reasons = {"budget_below_one_lot", "ORDER_AMOUNT_LIMIT_EXCEEDED"}
        money_blocked_symbols = {
            symbol for symbol, reasons in decision_reasons.items()
            if reasons & money_block_reasons
        } - purchased_symbols
        position_limited_symbols = {
            symbol for symbol, reasons in decision_reasons.items()
            if "POSITION_LIMIT_REACHED" in reasons
        } - purchased_symbols
        buy_signals = set(usecase._buy_signal_symbols)
        return {
            "date": target_date.isoformat(),
            "band": band,
            "scenario": scenario.name,
            "status": "simulated",
            "market_regime": assessment.regime.value,
            "selected_count": len(filtering_result.symbols),
            "minute_bar_symbol_count": sum(bool(bars) for bars in minute_bars.values()),
            "evaluable_symbol_count": len(eligible_symbols),
            "cannot_simulate": exclusions,
            "buy_signal_count": len(buy_signals),
            "purchased_symbol_count": len(purchased_symbols),
            "money_constrained_symbol_count": len(money_blocked_symbols),
            "money_constrained_symbols": sorted(money_blocked_symbols),
            "position_limit_symbol_count": len(position_limited_symbols),
            "position_limit_symbols": sorted(position_limited_symbols),
            "hypothetical_pnl_before_costs": round(sum(pnl_by_symbol.values()), 2),
            "wins": sum(pnl > 0 for pnl in pnl_by_symbol.values()),
            "losses": sum(pnl < 0 for pnl in pnl_by_symbol.values()),
            "flat": sum(pnl == 0 for pnl in pnl_by_symbol.values()),
            "pnl_by_symbol": pnl_by_symbol,
            "buy_signal_market_regime": {
                "CAUTION": len(buy_signals) if assessment.regime == MarketRegime.CAUTION else 0,
                "other": len(buy_signals) if assessment.regime != MarketRegime.CAUTION else 0,
            },
            "purchased_market_regime": {
                "CAUTION": len(purchased_symbols) if assessment.regime == MarketRegime.CAUTION else 0,
                "other": len(purchased_symbols) if assessment.regime != MarketRegime.CAUTION else 0,
            },
        }


def _aggregate(rows: list[dict], exclusions: list[dict] | None = None) -> dict:
    completed = [row for row in rows if row["status"] == "simulated"]
    exclusion_reasons = {item["reason"] for item in exclusions or []}
    if completed:
        analysis_status = "simulated"
    elif exclusion_reasons and exclusion_reasons <= {
        "filtering_result_missing",
        "empty_selection",
    }:
        analysis_status = "no_saved_selections"
    elif rows or exclusion_reasons:
        analysis_status = "not_simulatable"
    else:
        analysis_status = "no_comparable_dates"
    signal_by_regime: Counter[str] = Counter()
    purchased_by_regime: Counter[str] = Counter()
    for row in completed:
        regime_bucket = "CAUTION" if row["market_regime"] == "CAUTION" else "other"
        signal_by_regime[regime_bucket] += row["buy_signal_count"]
        purchased_by_regime[regime_bucket] += row["purchased_symbol_count"]
    buy_signal_count = sum(row.get("buy_signal_count", 0) for row in rows)
    selected_count = sum(row["selected_count"] for row in rows)
    return {
        "analysis_status": analysis_status,
        "sample_assessment": (
            "reference"
            if completed and (
                len(completed) < 10 or selected_count < 30 or buy_signal_count < 30
            )
            else "not_assessed" if not completed else "adequate_for_summary"
        ),
        "selected_count": selected_count,
        "minute_bar_symbol_count": sum(row.get("minute_bar_symbol_count", 0) for row in rows),
        "evaluable_symbol_count": sum(row.get("evaluable_symbol_count", 0) for row in rows),
        "buy_signal_count": buy_signal_count,
        "purchased_symbol_count": sum(row.get("purchased_symbol_count", 0) for row in rows),
        "money_constrained_symbol_count": sum(row.get("money_constrained_symbol_count", 0) for row in rows),
        "position_limit_symbol_count": sum(row.get("position_limit_symbol_count", 0) for row in rows),
        "hypothetical_pnl_before_costs": round(
            sum(row.get("hypothetical_pnl_before_costs", 0.0) for row in rows), 2
        ),
        "wins": sum(row.get("wins", 0) for row in rows),
        "losses": sum(row.get("losses", 0) for row in rows),
        "flat": sum(row.get("flat", 0) for row in rows),
        "buy_signals_by_market_regime": dict(signal_by_regime),
        "purchased_by_market_regime": dict(purchased_by_regime),
        "completed_days": len(completed),
        "days_with_partial_input": sum(
            bool(row.get("cannot_simulate")) for row in completed
        ),
    }


def estimate(
    start_date: date = date(2026, 9, 1),
    end_date: date | None = None,
) -> dict:
    _check_runtime_safety()
    comparison_days = _discover_comparison_days(start_date, end_date)
    results = []
    exclusions = []
    for target_date in comparison_days:
        assessment = _load_market_assessment(target_date)
        for band in (270, 450, 900):
            filtering_result = _load_filtering_result(
                FILTERING_DIRECTORIES[band], target_date
            )
            scenarios = _build_scenarios(band)
            if filtering_result is None or not filtering_result.symbols:
                reason = "filtering_result_missing" if filtering_result is None else "empty_selection"
                for scenario in scenarios:
                    exclusions.append({
                        "date": target_date.isoformat(),
                        "band": band,
                        "scenario": scenario.name,
                        "reason": reason,
                    })
                continue
            if assessment is None:
                for scenario in scenarios:
                    exclusions.append({
                        "date": target_date.isoformat(),
                        "band": band,
                        "scenario": scenario.name,
                        "reason": "market_regime_unavailable",
                        "selected_count": len(filtering_result.symbols),
                    })
                continue
            for scenario in scenarios:
                results.append(_run_day(
                    target_date,
                    band,
                    scenario,
                    filtering_result,
                    assessment,
                ))

    summaries = {}
    for band in (270, 450, 900):
        summaries[str(band)] = {}
        for scenario_name in ("current", "band_adjusted"):
            rows = [
                row for row in results
                if row["band"] == band and row["scenario"] == scenario_name
            ]
            scenario_exclusions = [
                item for item in exclusions
                if item["band"] == band and item["scenario"] == scenario_name
            ]
            summaries[str(band)][scenario_name] = _aggregate(rows, scenario_exclusions)

    return {
        "period": {
            "start_date": start_date.isoformat(),
            "end_date": (end_date or max(comparison_days, default=start_date)).isoformat(),
            "comparison_dates": [target_date.isoformat() for target_date in comparison_days],
        },
        "sizing_scenarios": {
            str(band): [
                {
                    "name": scenario.name,
                    "order_cap": scenario.order_cap,
                    "target_positions": scenario.target_positions,
                }
                for scenario in _build_scenarios(band)
            ]
            for band in (270, 450, 900)
        },
        "daily_results": results,
        "summary_by_band": summaries,
        "excluded": exclusions,
        "pnl_note": "現行の売買判断による仮想約定。手数料・スリッページ前。",
        "data_note": "保存済み選定結果・分足・日足キャッシュ・日次レポートのみ使用。入力データは変更しない。",
    }


def main() -> None:
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    args = _build_parser().parse_args()
    print(json.dumps(
        estimate(args.start_date, args.end_date),
        ensure_ascii=False,
        indent=2,
    ))


if __name__ == "__main__":
    main()
