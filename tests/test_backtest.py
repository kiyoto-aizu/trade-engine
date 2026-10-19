import json
from datetime import date, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.application import backtest_usecase
from src.application.backtest_usecase import (
    compare_market_regime_backtest,
    compare_trend_relief_backtest,
    simulate_backtest,
    simulate_timeseries_backtest,
)
from src.domain.enums import OrderSide
from src.domain.market_regime import MarketRegime
from src.config import config
from src.domain.rules import calculate_rsi
from src.application.backtest_report_usecase import save_backtest_result
from src.application import backtest_execution_usecase
from src.infrastructure.market_data.yahoo_backtest_history_client import fetch_yahoo_history
from src.infrastructure.persistence.backtest_input_repository import load_history
from src.entrypoints.run_backtest import (
    _build_sizing_kwargs,
    _filter_daily_symbols,
    _validate_backtest_arguments,
)
from src.infrastructure.analysis.daily_analyzer import OpenAIDailyAnalyzer
from src.domain.models import MinuteBar, TradeSignal
from src.domain.volatility import DailyBar, is_atr_stop_loss_triggered
from src.entrypoints.run_monthly_analysis import build_monthly_summary
from src.infrastructure.persistence.parquet_minute_bar_repository import ParquetMinuteBarRepository
from src.infrastructure.persistence.filter_decision_repository import FilterDecisionRepository


@pytest.fixture(autouse=True)
def legacy_backtest_signal_parameters(monkeypatch):
    monkeypatch.setattr(config, "RSI_PERIOD", 2)
    monkeypatch.setattr(config, "RSI_MINIMUM_CLOSES", 5)
    monkeypatch.setattr(config, "RSI_ENTRY_THRESHOLD", 50.0)
    monkeypatch.setattr(config, "RSI_EXIT_THRESHOLD", 50.0)
    monkeypatch.setattr(config, "BACKTEST_FEE_RATE", 0.0)
    monkeypatch.setattr(config, "BACKTEST_MARKET_SLIPPAGE_BPS", 0.0)
    monkeypatch.setattr(config, "BACKTEST_EXECUTION_DELAY_BARS", 0)


def test_calculate_rsi_uses_wilder_smoothing():
    closes = [100.0] * 16 + [101.0, 100.0, 102.0, 101.0, 103.0, 102.0, 104.0, 103.0, 105.0, 104.0, 106.0, 105.0, 107.0, 106.0]

    rsi = calculate_rsi(closes, period=14, minimum_closes=30)

    assert rsi is not None
    assert 50.0 < rsi < 70.0


def test_fetch_yahoo_history_reads_live_response(monkeypatch):
    class DummyResponse:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self):
            return b'{"chart":{"result":[{"indicators":{"quote":[{"close":[100.0, 101.0, null, 103.0]}]}}]}}'

    monkeypatch.setattr("requests.get", lambda *args, **kwargs: DummyResponse())

    history = fetch_yahoo_history(["7203"], days=4)

    assert history["7203"] == [100.0, 101.0, 103.0]


def test_load_history_from_csv(tmp_path):
    csv_path = tmp_path / "history.csv"
    csv_path.write_text(
        "symbol,close\n7203,100\n7203,101\n7203,102\n",
        encoding="utf-8",
    )

    history = load_history(csv_path)

    assert history["7203"] == [100.0, 101.0, 102.0]


def test_save_backtest_result_keeps_latest_and_timestamped_archive(tmp_path):
    output_path = tmp_path / "latest_timeseries.json"
    result = {"total_pnl": 123.45, "trade_history": []}

    archive_path = save_backtest_result(output_path, result)

    assert output_path.exists()
    assert archive_path.exists()
    assert archive_path != output_path
    assert archive_path.stem.startswith("latest_timeseries_")
    assert output_path.read_text(encoding="utf-8") == archive_path.read_text(encoding="utf-8")

def test_save_backtest_result_can_archive_separately_from_latest(tmp_path):
    output_path = tmp_path / "latest" / "latest_timeseries.json"
    archive_directory = tmp_path / "runs"

    archive_path = save_backtest_result(
        output_path, {"total_pnl": 10.0}, archive_directory
    )

    assert output_path.exists()
    assert archive_path.parent == archive_directory
    assert archive_path.name.startswith("latest_timeseries_")


def _backtest_args(**overrides):
    values = {
        "start_date": None,
        "end_date": None,
        "fixed_qty": False,
        "target_positions": None,
        "max_order_amount": None,
        "production_sizing": False,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_backtest_default_sizing_uses_config_values(monkeypatch):
    monkeypatch.setattr(config, "TARGET_POSITIONS", 7)
    monkeypatch.setattr(config, "MAX_ORDER_AMOUNT_PER_TRADE", 12345.0)
    monkeypatch.setattr(config, "API_SOFT_LIMIT", 9999.0)

    assert _build_sizing_kwargs(_backtest_args()) == {
        "target_positions": 7,
        "max_order_amount_per_trade": 12345.0,
        "api_soft_limit": 9999.0,
    }


def test_backtest_fixed_qty_disables_production_sizing():
    assert _build_sizing_kwargs(_backtest_args(fixed_qty=True)) == {
        "target_positions": None,
        "max_order_amount_per_trade": None,
        "api_soft_limit": None,
    }


def test_backtest_production_sizing_is_deprecated_noop(caplog):
    default = _build_sizing_kwargs(_backtest_args())
    with caplog.at_level("WARNING"):
        deprecated = _build_sizing_kwargs(_backtest_args(production_sizing=True))

    assert deprecated == default
    assert "非推奨" in caplog.text


def test_filtering_backtest_records_events_only_in_backtest_database(monkeypatch, tmp_path):
    backtest_database = tmp_path / "backtest" / "filter_decisions.sqlite3"
    production_database = tmp_path / "state" / "filter_decisions.sqlite3"
    monkeypatch.setattr(config, "BACKTEST_FILTER_DECISION_DIRECTORY", backtest_database.parent)
    monkeypatch.setattr(config, "FILTER_DECISION_DATABASE_FILE", production_database)
    monkeypatch.setattr(
        backtest_execution_usecase,
        "load_daily_filtering_symbols",
        lambda directory: {"2026-09-01": ["7203"]},
    )
    monkeypatch.setattr(
        backtest_execution_usecase,
        "fetch_yahoo_dated_history",
        lambda symbols, days: {"7203": {"2026-09-01": 100.0}},
    )

    def record_backtest_event(*args, filter_decision_repository, **kwargs):
        filter_decision_repository.record_event(
            "ATR_DANGER_SKIP", "7203", datetime(2026, 9, 1, 10, 0), 100.0, 100,
            execution_mode="backtest",
        )
        return {"total_trades": 0}

    monkeypatch.setattr(
        backtest_execution_usecase, "simulate_timeseries_backtest", record_backtest_event
    )
    args = SimpleNamespace(
        filtering_dir=tmp_path / "filtering",
        start_date=None,
        end_date=None,
        days=30,
        live=True,
        compare_market_regime=False,
        cash=100_000,
        qty=100,
        fee=0.0,
        market_slippage_bps=0.0,
        execution_delay_bars=0,
        order_type="market",
        allow_overnight=False,
        indicator_source="daily",
        compare_atr=False,
    )

    backtest_execution_usecase.execute_backtest(args, {}, None)

    event_databases = list(backtest_database.parent.glob("*.sqlite3"))
    assert len(event_databases) == 1
    backtest_events = FilterDecisionRepository(event_databases[0]).load_summaries()
    production_events = FilterDecisionRepository(production_database).load_summaries()
    assert [event["execution_mode"] for event in backtest_events] == ["backtest"]
    assert production_events == []


def test_backtest_filter_decision_path_sanitizes_label_and_uses_timestamp(tmp_path):
    first = backtest_execution_usecase.build_backtest_filter_decision_database_path(
        tmp_path, "latest weekly.json", datetime(2026, 10, 1, 8, 0, 0, 1)
    )
    second = backtest_execution_usecase.build_backtest_filter_decision_database_path(
        tmp_path, "latest weekly.json", datetime(2026, 10, 1, 8, 0, 0, 2)
    )
    fallback = backtest_execution_usecase.build_backtest_filter_decision_database_path(
        tmp_path, "", datetime(2026, 10, 1, 8, 0, 0, 1)
    )

    assert first.name == "latest_weekly_json_20261001_080000_000001.sqlite3"
    assert second != first
    assert fallback.name.startswith("backtest_")


def _prepare_executable_filter_backtest(monkeypatch, tmp_path):
    daily_symbols = {"2026-09-06": ["7203"], "2026-09-07": ["7203"]}
    history = {
        "7203": {
            "2026-09-01": 90.0,
            "2026-09-02": 90.0,
            "2026-09-03": 90.0,
            "2026-09-04": 90.0,
            "2026-09-05": 90.0,
            "2026-09-06": 92.0,
            "2026-09-07": 120.0,
            "2026-09-08": 98.0,
        },
    }
    monkeypatch.setattr(
        backtest_execution_usecase, "load_daily_filtering_symbols", lambda directory: daily_symbols
    )
    monkeypatch.setattr(
        backtest_execution_usecase, "fetch_yahoo_dated_history", lambda symbols, days: history
    )
    monkeypatch.setattr(
        backtest_execution_usecase,
        "_market_regime_by_date",
        lambda args: {date_text: MarketRegime.DANGER for date_text in history["7203"]},
    )
    monkeypatch.setattr(config, "FILTER_DECISION_DATABASE_FILE", tmp_path / "production.sqlite3")


def _execute_filtering_run(output, start_date=None, end_date=None):
    args = SimpleNamespace(
        filtering_dir=Path("filtering"),
        start_date=start_date,
        end_date=end_date,
        days=730,
        live=True,
        compare_market_regime=True,
        cash=10_000.0,
        qty=100,
        fee=0.0,
        market_slippage_bps=0.0,
        execution_delay_bars=0,
        order_type="market",
        allow_overnight=True,
        indicator_source="daily",
        compare_atr=False,
        output=output,
    )
    return backtest_execution_usecase.execute_backtest(args, {}, None)


def _assert_no_duplicate_backtest_events(database_path):
    import sqlite3

    with sqlite3.connect(database_path) as connection:
        duplicates = connection.execute(
            "SELECT event_type, symbol, substr(occurred_at, 1, 10), COUNT(*) "
            "FROM filter_decision_events WHERE execution_mode='backtest' "
            "GROUP BY event_type, symbol, substr(occurred_at, 1, 10) HAVING COUNT(*) > 1"
        ).fetchall()
    assert duplicates == []


def test_cumulative_and_weekly_backtests_write_independent_event_databases(monkeypatch, tmp_path):
    _prepare_executable_filter_backtest(monkeypatch, tmp_path)
    monkeypatch.setattr(config, "BACKTEST_FILTER_DECISION_DIRECTORY", tmp_path / "filter_events")

    _execute_filtering_run(tmp_path / "latest_timeseries.json")
    weekly_result, _, _ = _execute_filtering_run(
        tmp_path / "latest_weekly.json", date(2026, 9, 6), date(2026, 9, 7)
    )
    assert weekly_result["requested_period_start"] == "2026-09-06"
    assert weekly_result["requested_period_end"] == "2026-09-07"

    databases = sorted((tmp_path / "filter_events").glob("*.sqlite3"))
    assert len(databases) == 2
    assert {path.name.split("_")[0] for path in databases} == {"latest"}
    assert any(path.name.startswith("latest_timeseries_") for path in databases)
    assert any(path.name.startswith("latest_weekly_") for path in databases)
    for database_path in databases:
        events = FilterDecisionRepository(database_path).load_summaries()
        assert events
        assert {event["execution_mode"] for event in events} == {"backtest"}
        _assert_no_duplicate_backtest_events(database_path)
    assert not config.FILTER_DECISION_DATABASE_FILE.exists()


def test_repeated_backtest_does_not_accumulate_or_modify_previous_database(monkeypatch, tmp_path):
    _prepare_executable_filter_backtest(monkeypatch, tmp_path)
    monkeypatch.setattr(config, "BACKTEST_FILTER_DECISION_DIRECTORY", tmp_path / "filter_events")
    output = tmp_path / "latest_timeseries.json"

    _execute_filtering_run(output)
    first_database = next((tmp_path / "filter_events").glob("*.sqlite3"))
    first_contents = first_database.read_bytes()
    first_count = len(FilterDecisionRepository(first_database).load_summaries())
    _execute_filtering_run(output)

    databases = list((tmp_path / "filter_events").glob("*.sqlite3"))
    assert len(databases) == 2
    assert first_database.read_bytes() == first_contents
    assert all(len(FilterDecisionRepository(path).load_summaries()) == first_count for path in databases)
    assert not config.FILTER_DECISION_DATABASE_FILE.exists()


@pytest.mark.parametrize("option", ["target_positions", "max_order_amount"])
def test_backtest_fixed_qty_rejects_sizing_overrides(option):
    with pytest.raises(ValueError, match="同時指定できません"):
        _validate_backtest_arguments(_backtest_args(fixed_qty=True, **{option: 1}))


@pytest.mark.parametrize("missing", ["start_date", "end_date"])
def test_backtest_date_range_requires_both_dates(missing):
    values = {"start_date": date(2026, 9, 21), "end_date": date(2026, 9, 25)}
    values[missing] = None

    with pytest.raises(ValueError, match="両方指定"):
        _validate_backtest_arguments(_backtest_args(**values))


def test_backtest_explicit_date_range_filters_daily_symbols_and_period():
    daily_symbols = {
        "2026-09-18": ["7203"],
        "2026-09-21": ["7203"],
        "2026-09-22": ["9984"],
        "2026-09-25": ["7203"],
        "2026-09-28": ["9984"],
    }

    filtered = _filter_daily_symbols(
        daily_symbols, date(2026, 9, 21), date(2026, 9, 25), days=1
    )
    result = simulate_timeseries_backtest(
        filtered,
        {
            "7203": {date_text: 100.0 for date_text in filtered},
            "9984": {date_text: 100.0 for date_text in filtered},
        },
    )

    assert list(filtered) == ["2026-09-21", "2026-09-22", "2026-09-25"]
    assert result["period_start"] == "2026-09-21"
    assert result["period_end"] == "2026-09-25"


def test_backtest_analyzer_uses_backtest_specific_review_prompt(monkeypatch):
    captured = {}

    class DummyResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {"choices": [{"message": {"content": "結果の評価\n- 参考評価です。"}}]}

    def post(url, **kwargs):
        captured["url"] = url
        captured["payload"] = kwargs["json"]
        return DummyResponse()

    monkeypatch.setattr("src.infrastructure.analysis.daily_analyzer.requests.post", post)
    analyzer = OpenAIDailyAnalyzer("key", "model", "https://example.test")

    result = analyzer.analyze_backtest({"総損益": 100.0, "最大ドローダウン": 50.0})

    assert result.startswith("結果の評価")
    assert "バックテスト集計" in captured["payload"]["messages"][1]["content"]


def test_openai_analyzer_daily_and_monthly_success(monkeypatch):
    class DummyResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {"choices": [{"message": {"content": "参考評価です。"}}]}

    monkeypatch.setattr(
        "src.infrastructure.analysis.daily_analyzer.requests.post",
        lambda *args, **kwargs: DummyResponse(),
    )
    analyzer = OpenAIDailyAnalyzer("key", "model", "https://example.test")

    assert analyzer.analyze({"order_count": 1}) == "参考評価です。"
    assert analyzer.analyze_monthly({"months": 1}) == "参考評価です。"


def test_build_monthly_summary_aggregates_daily_reports_and_backtests(tmp_path):
    reports = tmp_path / "reports"
    backtests = tmp_path / "backtests"
    reports.mkdir()
    backtests.mkdir()
    (reports / "2026-09-01.json").write_text(
        json.dumps({"date": "2026-09-01", "trading_mode": "ペーパートレード", "order_count": 2, "total_profit_loss": 100, "kill_switch_triggered": False}),
        encoding="utf-8",
    )
    (reports / "2026-09-02.json").write_text(
        json.dumps({"date": "2026-09-02", "trading_mode": "ペーパートレード", "order_count": 1, "total_profit_loss": -20, "kill_switch_triggered": True}),
        encoding="utf-8",
    )
    (backtests / "latest_timeseries_20260902.json").write_text(
        json.dumps({
            "generated_at": "2026-09-30T17:00:00",
            "period_start": "2026-09-01",
            "period_end": "2026-09-30",
            "total_pnl": 250,
            "total_trades": 4,
        }),
        encoding="utf-8",
    )

    summary = build_monthly_summary("2026-09", reports, backtests)

    assert summary["daily"]["order_count"] == 3
    assert summary["daily"]["total_profit_loss"] == 80
    assert summary["daily"]["kill_switch_days"] == 1
    assert summary["backtest"]["exact_period_run_available"] is True
    assert summary["backtest"]["total_pnl"] == 250
    assert summary["backtest"]["total_trades"] == 4


def test_simulate_backtest_buys_then_sells_on_signal():
    history = {
        "7203": [90.0, 90.0, 90.0, 90.0, 90.0, 92.0, 120.0, 98.0, 96.0],
    }

    result = simulate_backtest(["7203"], history, starting_cash=10_000.0, qty_per_trade=100, close_at_eod=False)

    assert result["total_trades"] == 2
    assert result["cash"] == 10_400.0
    assert result["final_position"] == 0
    assert result["total_pnl"] == 400.0


def test_simulate_backtest_exits_on_atr_stop_loss(monkeypatch):
    monkeypatch.setattr(config, "RSI_PERIOD", 2)
    monkeypatch.setattr(config, "RSI_MINIMUM_CLOSES", 5)
    monkeypatch.setattr(config, "RSI_ENTRY_THRESHOLD", 50.0)
    monkeypatch.setattr(config, "RSI_EXIT_THRESHOLD", 0.0)
    monkeypatch.setattr(config, "ATR_PERIOD", 3)
    monkeypatch.setattr(config, "ATR_CAUTION_RATIO", 1.5)
    monkeypatch.setattr(config, "ATR_DANGER_RATIO", 2.0)
    monkeypatch.setattr(config, "ATR_STOP_NORMAL_MULTIPLIER", 1.5)
    monkeypatch.setattr(config, "ATR_STOP_CAUTION_MULTIPLIER", 1.0)
    monkeypatch.setattr(config, "ATR_STOP_DANGER_MULTIPLIER", 0.7)

    history = {"7203": [100.0, 100.0, 100.0, 100.0, 100.0, 102.0, 90.0]}
    ohlc = {
        "7203": [DailyBar(101.0, 99.0, 100.0)] * 6
        + [DailyBar(91.0, 89.0, 90.0)],
    }

    result = simulate_backtest(
        ["7203"],
        history,
        starting_cash=20_000.0,
        qty_per_trade=100,
        ohlc_history_by_symbol=ohlc,
        close_at_eod=False,
    )

    assert any(signal.get("note") == "atr_stop_loss" for signal in result["signals"])
    assert result["final_position"] == 0


def test_atr_stop_uses_high_water_mark_not_entry_price():
    holding_high = 150.0
    atr = 10.0

    assert not is_atr_stop_loss_triggered(140.0, holding_high, atr, 1.5)
    assert is_atr_stop_loss_triggered(130.0, holding_high, atr, 1.5)


def test_simulate_timeseries_backtest_uses_daily_symbol_sets():
    dated_history = {
        "7203": {
            "2026-09-01": 90.0,
            "2026-09-02": 90.0,
            "2026-09-03": 90.0,
            "2026-09-04": 90.0,
            "2026-09-05": 90.0,
            "2026-09-06": 92.0,
            "2026-09-07": 120.0,
            "2026-09-08": 98.0,
            "2026-09-09": 96.0,
        },
    }
    daily_symbols = {
        "2026-09-06": ["7203"],
        "2026-09-07": ["7203"],
        "2026-09-08": ["7203"],
        "2026-09-09": ["7203"],
    }

    result = simulate_timeseries_backtest(
        daily_symbols,
        dated_history,
        starting_cash=10_000.0,
        qty_per_trade=100,
        close_at_eod=False,
    )

    assert result["total_trades"] == 2
    assert result["total_pnl"] == 400.0
    assert result["period_start"] == "2026-09-06"
    assert result["period_end"] == "2026-09-09"
    assert result["daily_summary"] == [{
        "date": "2026-09-09",
        "trade_count": 1,
        "total_realized_pnl": 400.0,
        "win_count": 1,
        "avg_realized_pnl": 400.0,
    }]


def _two_symbol_daily_history_and_symbols():
    price_series = {
        "2026-09-01": 90.0, "2026-09-02": 90.0, "2026-09-03": 90.0,
        "2026-09-04": 90.0, "2026-09-05": 90.0, "2026-09-06": 92.0,
        "2026-09-07": 120.0, "2026-09-08": 98.0, "2026-09-09": 96.0,
    }
    dated_history = {"7203": dict(price_series), "9984": dict(price_series)}
    daily_symbols = {
        "2026-09-06": ["7203", "9984"],
        "2026-09-07": ["7203", "9984"],
        "2026-09-08": ["7203", "9984"],
        "2026-09-09": ["7203", "9984"],
    }
    return daily_symbols, dated_history


def test_timeseries_backtest_target_positions_allocates_budget_dynamically():
    # Issue 3: target_positions未指定時はqty_per_trade固定だが、指定時は
    # 本番(trading_usecase.py)と同じ「残り建玉枠で現金按分」で数量を決める。
    daily_symbols, dated_history = _two_symbol_daily_history_and_symbols()

    fixed = simulate_timeseries_backtest(
        daily_symbols, dated_history, starting_cash=40_000.0, qty_per_trade=100,
        close_at_eod=False, enable_volatility_adjustment=False, market_regime_enabled=False,
    )
    sized = simulate_timeseries_backtest(
        daily_symbols, dated_history, starting_cash=40_000.0, qty_per_trade=100,
        close_at_eod=False, enable_volatility_adjustment=False, market_regime_enabled=False,
        target_positions=2,
    )

    # 固定モード: qty_per_trade(100)がそのまま使われる
    assert [s["qty"] for s in fixed["signals"] if s["side"] == OrderSide.BUY.value] == [100, 100]
    # 予算配分モード: 40,000円を2枠に按分(20,000円/枠)して単元(100株)で丸めた数量になる
    assert [s["qty"] for s in sized["signals"] if s["side"] == OrderSide.BUY.value] == [200, 200]


def test_timeseries_backtest_target_positions_caps_max_order_amount():
    daily_symbols, dated_history = _two_symbol_daily_history_and_symbols()

    capped = simulate_timeseries_backtest(
        daily_symbols, dated_history, starting_cash=40_000.0, qty_per_trade=100,
        close_at_eod=False, enable_volatility_adjustment=False, market_regime_enabled=False,
        target_positions=2, max_order_amount_per_trade=10_000.0,
    )

    # 按分すると20,000円/枠だが、上限額10,000円でキャップされるため数量が減る
    assert [s["qty"] for s in capped["signals"] if s["side"] == OrderSide.BUY.value] == [100, 100]


def test_timeseries_backtest_target_positions_caps_api_soft_limit():
    daily_symbols, dated_history = _two_symbol_daily_history_and_symbols()

    capped = simulate_timeseries_backtest(
        daily_symbols, dated_history, starting_cash=40_000.0, qty_per_trade=100,
        close_at_eod=False, enable_volatility_adjustment=False, market_regime_enabled=False,
        target_positions=2, api_soft_limit=5_000.0,
    )

    assert [s for s in capped["signals"] if s["side"] == OrderSide.BUY.value] == []


def test_timeseries_backtest_target_positions_limit_skips_new_buys():
    daily_symbols, dated_history = _two_symbol_daily_history_and_symbols()

    limited = simulate_timeseries_backtest(
        daily_symbols, dated_history, starting_cash=40_000.0, qty_per_trade=100,
        close_at_eod=False, enable_volatility_adjustment=False, market_regime_enabled=False,
        target_positions=1,
    )

    # 同時保有上限(1)に達しているため、2銘柄目(9984)の新規買いは一切発生しない
    buy_symbols = {s["symbol"] for s in limited["signals"] if s["side"] == OrderSide.BUY.value}
    assert buy_symbols == {"7203"}
    # 先に買えた7203は、余った現金枠(40,000円/1枠)をすべて使った数量になる
    assert [s["qty"] for s in limited["signals"] if s["side"] == OrderSide.BUY.value] == [400]


def test_timeseries_backtest_atr_uses_only_completed_daily_bars(monkeypatch):
    captured_bars = []

    def capture_assessment(bars, *args, **kwargs):
        captured_bars.append(list(bars))
        return None

    monkeypatch.setattr("src.application.backtest_usecase.assess_volatility", capture_assessment)
    dated_history = {
        "7203": {
            "2026-09-01": 90.0,
            "2026-09-02": 90.0,
            "2026-09-03": 90.0,
            "2026-09-04": 90.0,
            "2026-09-05": 90.0,
            "2026-09-06": 92.0,
            "2026-09-07": 120.0,
        },
    }
    ohlc = {
        "7203": {
            date_text: DailyBar(91.0, 89.0, 90.0)
            for date_text in dated_history["7203"]
        },
    }
    ohlc["7203"]["2026-09-07"] = DailyBar(999.0, 1.0, 120.0)

    simulate_timeseries_backtest(
        {"2026-09-06": ["7203"], "2026-09-07": ["7203"]},
        dated_history,
        starting_cash=20_000.0,
        qty_per_trade=100,
        ohlc_history_by_symbol_date=ohlc,
        close_at_eod=False,
        enable_volatility_adjustment=False,
        market_regime_enabled=False,
    )

    assert captured_bars
    assert all(all(bar.high != 999.0 for bar in bars) for bars in captured_bars)


def test_timeseries_backtest_danger_skips_buy_but_does_not_change_sell_logic(tmp_path):
    dated_history = {
        "7203": {
            "2026-09-01": 90.0,
            "2026-09-02": 90.0,
            "2026-09-03": 90.0,
            "2026-09-04": 90.0,
            "2026-09-05": 90.0,
            "2026-09-06": 92.0,
            "2026-09-07": 120.0,
            "2026-09-08": 98.0,
        },
    }
    regimes = {date_text: MarketRegime.DANGER for date_text in dated_history["7203"]}

    repository = FilterDecisionRepository(tmp_path / "filter_decisions.sqlite3")
    result = simulate_timeseries_backtest(
        {"2026-09-06": ["7203"], "2026-09-07": ["7203"]},
        dated_history,
        starting_cash=10_000.0,
        qty_per_trade=100,
        close_at_eod=False,
        market_regime_by_date=regimes,
        filter_decision_repository=repository,
    )

    assert result["total_trades"] == 0
    assert result["market_regime_adjustment"]["danger_skipped"] >= 1
    assert [item["event_type"] for item in repository.load_open_events("backtest")] == [
        "MARKET_REGIME_DANGER_SKIP"
    ]


def test_timeseries_backtest_caution_filters_buy_by_rsi_threshold(monkeypatch):
    dated_history = {
        "7203": {
            "2026-09-01": 90.0,
            "2026-09-02": 90.0,
            "2026-09-03": 90.0,
            "2026-09-04": 90.0,
            "2026-09-05": 90.0,
            "2026-09-06": 92.0,
        },
    }
    monkeypatch.setattr(config, "RSI_ENTRY_THRESHOLD_CAUTION", 100.0)
    result = simulate_timeseries_backtest(
        {"2026-09-06": ["7203"]},
        dated_history,
        starting_cash=100_000.0,
        qty_per_trade=300,
        close_at_eod=False,
        market_regime_by_date={"2026-09-06": MarketRegime.CAUTION},
    )

    assert result["total_trades"] == 0
    assert result["market_regime_adjustment"]["caution_rsi_filtered"] == 1


def test_compare_market_regime_backtest_reports_before_and_after_metrics():
    dated_history = {
        "7203": {
            "2026-09-01": 90.0,
            "2026-09-02": 90.0,
            "2026-09-03": 90.0,
            "2026-09-04": 90.0,
            "2026-09-05": 90.0,
            "2026-09-06": 92.0,
        },
    }
    comparison = compare_market_regime_backtest(
        daily_symbols={"2026-09-06": ["7203"]},
        dated_history_by_symbol=dated_history,
        starting_cash=100_000.0,
        qty_per_trade=300,
        close_at_eod=False,
        market_regime_by_date={"2026-09-06": MarketRegime.DANGER},
    )

    assert set(comparison) == {"baseline", "with_market_regime", "delta", "market_regime_adjustment"}
    assert comparison["baseline"]["total_trades"] >= comparison["with_market_regime"]["total_trades"]


def test_compare_trend_relief_backtest_reports_relief_metrics():
    dated_history = {
        "7203": {
            "2026-09-01": 90.0,
            "2026-09-02": 90.0,
            "2026-09-03": 90.0,
            "2026-09-04": 90.0,
            "2026-09-05": 90.0,
            "2026-09-06": 92.0,
        },
    }
    comparison = compare_trend_relief_backtest(
        daily_symbols={"2026-09-06": ["7203"]},
        dated_history_by_symbol=dated_history,
        starting_cash=100_000.0,
        qty_per_trade=300,
        close_at_eod=False,
        market_regime_by_date={"2026-09-06": MarketRegime.DANGER},
    )

    assert set(comparison) == {
        "without_trend_relief",
        "with_trend_relief",
        "delta",
        "trend_relief_days",
    }
    assert comparison["trend_relief_days"] == 0


def test_timeseries_backtest_evaluates_each_minute_with_daily_indicators(tmp_path):
    dated_history = {
        "7203": {
            "2026-09-01": 90.0,
            "2026-09-02": 90.0,
            "2026-09-03": 90.0,
            "2026-09-04": 90.0,
            "2026-09-05": 90.0,
            "2026-09-06": 90.0,
        },
    }
    repository = ParquetMinuteBarRepository(tmp_path)
    repository.append_bar(date(2026, 9, 6), "7203", MinuteBar(
        time="2026-09-06T09:00:00", price=92.0, cumulative_volume=None, volume=100, source="yahoo",
    ))
    repository.append_bar(date(2026, 9, 6), "7203", MinuteBar(
        time="2026-09-06T09:01:00", price=89.0, cumulative_volume=None, volume=100, source="yahoo",
    ))

    result = simulate_timeseries_backtest(
        {"2026-09-06": ["7203"]},
        dated_history,
        starting_cash=10_000.0,
        qty_per_trade=100,
        minute_bar_repository=repository,
        indicator_source="daily",
        close_at_eod=False,
    )

    assert [signal["time"] for signal in result["signals"]] == [
        "2026-09-06T09:00:00",
        "2026-09-06T09:01:00",
    ]
    assert result["total_pnl"] == -300.0
    assert result["indicator_source"] == "daily"
    assert result["minute_bar_mode"] is True
    assert result["minute_signal_evaluations"] == 2
    assert result["daily_close_signal_evaluations"] == 0


def test_timeseries_backtest_minute_indicator_does_not_use_current_bar(tmp_path):
    repository = ParquetMinuteBarRepository(tmp_path)
    for minute, price in enumerate([90.0, 90.0, 90.0, 90.0, 90.0, 92.0, 120.0, 98.0, 96.0]):
        repository.append_bar(date(2026, 9, 6), "7203", MinuteBar(
            time=f"2026-09-06T09:{minute:02d}:00", price=price,
            cumulative_volume=None, volume=100, source="yahoo",
        ))

    result = simulate_timeseries_backtest(
        {"2026-09-06": ["7203"]},
        {"7203": {"2026-09-06": 96.0}},
        starting_cash=10_000.0,
        qty_per_trade=100,
        minute_bar_repository=repository,
        indicator_source="minute",
        close_at_eod=False,
    )

    assert [signal["time"] for signal in result["signals"]] == [
        "2026-09-06T09:05:00",
        "2026-09-06T09:08:00",
    ]
    assert result["total_pnl"] == 400.0


def test_timeseries_backtest_merges_symbols_in_minute_order(tmp_path):
    repository = ParquetMinuteBarRepository(tmp_path)
    dated_history = {
        symbol: {
            "2026-09-01": 90.0,
            "2026-09-02": 90.0,
            "2026-09-03": 90.0,
            "2026-09-04": 90.0,
            "2026-09-05": 90.0,
        }
        for symbol in ("1111", "2222")
    }
    for symbol, bars in {
        "1111": [("09:00", 92.0), ("10:00", 89.0)],
        "2222": [("09:30", 92.0)],
    }.items():
        for clock, price in bars:
            repository.append_bar(date(2026, 9, 6), symbol, MinuteBar(
                time=f"2026-09-06T{clock}:00", price=price,
                cumulative_volume=None, volume=100, source="yahoo",
            ))

    result = simulate_timeseries_backtest(
        {"2026-09-06": ["1111", "2222"]},
        dated_history,
        starting_cash=10_000.0,
        qty_per_trade=100,
        minute_bar_repository=repository,
        indicator_source="daily",
        close_at_eod=False,
    )

    assert [signal["symbol"] for signal in result["signals"]] == ["1111", "1111"]
    assert result["final_position"] == 0
    assert result["cash"] == 9_700.0


def test_simulate_backtest_does_not_use_current_price_for_signal_baseline():
    history = {
        "7203": [90.0, 90.0, 90.0, 90.0, 90.0, 92.0, 120.0, 98.0, 96.0],
    }

    result = simulate_backtest(["7203"], history, starting_cash=10_000.0, qty_per_trade=100, close_at_eod=False)

    assert result["total_trades"] == 2
    assert result["final_position"] == 0


def test_simulate_backtest_fees_and_position_state():
    history = {
        "7203": [90.0, 90.0, 90.0, 90.0, 90.0, 92.0, 120.0, 98.0, 96.0],
    }

    result = simulate_backtest(
        ["7203"],
        history,
        starting_cash=10_000.0,
        qty_per_trade=100,
        fee_rate=0.001,
        close_at_eod=False,
    )

    assert result["total_trades"] == 2
    assert result["final_position"] == 0
    assert 10_000.0 < result["cash"] < 10_400.0
    assert result["total_pnl"] < 400.0


def test_simulate_backtest_applies_delayed_market_slippage_and_round_trip_fees():
    history = {"7203": [90.0, 90.0, 90.0, 90.0, 90.0, 92.0, 120.0, 98.0, 96.0]}

    result = simulate_backtest(
        ["7203"],
        history,
        starting_cash=20_000.0,
        qty_per_trade=100,
        fee_rate=0.001,
        market_slippage_bps=5.0,
        execution_delay_bars=1,
        close_at_eod=False,
    )

    trade = result["trade_history"][0]
    assert trade["buy_price"] == pytest.approx(120.06)
    assert trade["sell_price"] == pytest.approx(95.952)
    assert trade["fee"] == pytest.approx(21.6012)
    assert trade["realized_pnl"] == pytest.approx(-2432.4012)
    assert result["execution_assumptions"]["execution_delay_bars"] == 1


def test_timeseries_backtest_applies_delayed_market_slippage_and_round_trip_fees(tmp_path):
    repository = ParquetMinuteBarRepository(tmp_path)
    for minute, price in enumerate([90.0, 90.0, 90.0, 90.0, 90.0, 92.0, 120.0, 98.0, 96.0]):
        repository.append_bar(date(2026, 9, 6), "7203", MinuteBar(
            time=f"2026-09-06T09:{minute:02d}:00", price=price,
            cumulative_volume=None, volume=100, source="yahoo",
        ))

    result = simulate_timeseries_backtest(
        {"2026-09-06": ["7203"]},
        {"7203": {"2026-09-06": 96.0}},
        starting_cash=20_000.0,
        qty_per_trade=100,
        fee_rate=0.001,
        market_slippage_bps=5.0,
        execution_delay_bars=1,
        minute_bar_repository=repository,
        indicator_source="minute",
        close_at_eod=False,
    )

    trade = result["trade_history"][0]
    assert trade["buy_price"] == pytest.approx(120.06)
    assert trade["sell_price"] == pytest.approx(95.952)
    assert trade["fee"] == pytest.approx(21.6012)


def test_simulate_backtest_reports_summary_metrics():
    history = {
        "7203": [90.0, 90.0, 90.0, 90.0, 90.0, 92.0, 120.0, 98.0, 96.0],
    }

    result = simulate_backtest(
        ["7203"],
        history,
        starting_cash=10_000.0,
        qty_per_trade=100,
        fee_rate=0.001,
    )

    assert "win_rate" in result
    assert "max_drawdown" in result
    assert "profit_factor" in result
    assert 0.0 <= result["win_rate"] <= 1.0
    assert result["max_drawdown"] >= 0.0
    assert result["profit_factor"] >= 0.0


def test_simulate_backtest_tracks_trade_history_with_holding_days():
    history = {
        "7203": [90.0, 90.0, 90.0, 90.0, 90.0, 92.0, 120.0, 98.0, 96.0],
    }

    result = simulate_backtest(["7203"], history, starting_cash=10_000.0, qty_per_trade=100, close_at_eod=False)

    assert "trade_history" in result
    assert len(result["trade_history"]) == 1
    assert result["trade_history"][0]["symbol"] == "7203"
    assert result["trade_history"][0]["realized_pnl"] == 400.0
    assert result["trade_history"][0]["holding_days"] == 3


def test_simulate_backtest_summarizes_symbol_and_holding_period_results():
    history = {
        "7203": [90.0, 90.0, 90.0, 90.0, 90.0, 92.0, 120.0, 98.0, 96.0],
        "7204": [90.0, 90.0, 90.0, 90.0, 90.0, 92.0, 120.0, 98.0, 96.0],
    }

    result = simulate_backtest(["7203", "7204"], history, starting_cash=10_000.0, qty_per_trade=100)

    assert "summary_by_symbol" in result
    assert "holding_bucket_summary" in result
    assert any(entry["symbol"] == "7203" for entry in result["summary_by_symbol"])
    assert any(entry["bucket"] == "1-3" for entry in result["holding_bucket_summary"])
    assert sum(entry["total_realized_pnl"] for entry in result["summary_by_symbol"]) > 0


def test_simulate_backtest_tracks_daily_summary_for_review():
    history = {
        "7203": [90.0, 90.0, 90.0, 90.0, 90.0, 92.0, 96.0],
    }

    result = simulate_backtest(
        ["7203"],
        history,
        starting_cash=10_000.0,
        qty_per_trade=100,
        close_at_eod=True,
    )

    assert "daily_summary" in result
    assert len(result["daily_summary"]) >= 1
    assert all("day_index" in entry for entry in result["daily_summary"])
    assert all("total_realized_pnl" in entry for entry in result["daily_summary"])


def test_simulate_backtest_supports_tunable_signal_thresholds():
    history = {
        "7203": [90.0, 90.0, 90.0, 90.0, 90.0, 92.0, 120.0, 98.0, 96.0],
    }

    result = simulate_backtest(
        ["7203"],
        history,
        starting_cash=10_000.0,
        qty_per_trade=100,
        buy_threshold_ratio=0.98,
        sell_threshold_ratio=1.03,
        noise_band_ratio=0.02,
    )

    assert result["total_trades"] == 0
    assert result["cash"] == 10_000.0
    assert result["final_position"] == 0


def test_simulate_backtest_applies_stop_loss_to_limit_downside():
    history = {
        "7203": [90.0, 90.0, 90.0, 90.0, 90.0, 92.0, 89.0],
    }

    result = simulate_backtest(
        ["7203"],
        history,
        starting_cash=10_000.0,
        qty_per_trade=100,
        stop_loss_ratio=0.02,
    )

    assert result["total_trades"] >= 1
    assert result["final_position"] == 0
    assert min(entry["realized_pnl"] for entry in result["trade_history"]) < 0


def test_simulate_backtest_requires_directional_momentum_for_signal():
    history = {
        "7203": [90.0, 90.0, 90.0, 90.0, 90.0, 92.0, 120.0, 98.0, 96.0],
    }

    result = simulate_backtest(
        ["7203"],
        history,
        starting_cash=10_000.0,
        qty_per_trade=100,
        trend_strength_ratio=0.05,
    )

    assert result["total_trades"] == 0
    assert result["final_position"] == 0


def test_simulate_backtest_closes_positions_at_end_of_day_for_day_trade_mode():
    history = {
        "7203": [90.0, 90.0, 90.0, 90.0, 90.0, 92.0, 89.0],
    }

    result = simulate_backtest(
        ["7203"],
        history,
        starting_cash=10_000.0,
        qty_per_trade=100,
    )

    assert result["final_position"] == 0
    assert len(result["trade_history"]) >= 1
    assert all(entry["holding_days"] <= 1 for entry in result["trade_history"])
