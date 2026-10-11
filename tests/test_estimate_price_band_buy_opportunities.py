import json
from datetime import date

from src.config import config
from src.domain.models import FilteringResult
from src.entrypoints import estimate_price_band_buy_opportunities as estimator
from src.infrastructure.persistence.filtering_result_repository import FilteringResultRepository


def test_build_scenarios_uses_requested_band_caps_and_recalculates_positions(monkeypatch):
    monkeypatch.setattr(config, "OPERATING_CAPITAL", 100_000.0)
    monkeypatch.setattr(config, "MAX_ORDER_AMOUNT_PER_TRADE", 30_000.0)
    monkeypatch.setattr(config, "TARGET_POSITIONS", 3)

    current_450, expanded_450 = estimator._build_scenarios(450)
    current_900, expanded_900 = estimator._build_scenarios(900)
    current_270, expanded_270 = estimator._build_scenarios(270)

    assert (current_450.order_cap, current_450.target_positions) == (30_000.0, 3)
    assert (expanded_450.order_cap, expanded_450.target_positions) == (50_000.0, 2)
    assert (current_900.order_cap, current_900.target_positions) == (30_000.0, 3)
    assert (expanded_900.order_cap, expanded_900.target_positions) == (100_000.0, 1)
    assert (current_270.order_cap, current_270.target_positions) == (30_000.0, 3)
    assert (expanded_270.order_cap, expanded_270.target_positions) == (30_000.0, 3)


def test_discover_comparison_days_requires_nonempty_270_and_450_results(tmp_path, monkeypatch):
    band_270 = tmp_path / "270"
    band_450 = tmp_path / "450"
    FilteringResultRepository(band_270).save(
        FilteringResult(date="2026-10-06", symbols=["1111"], generated_at="")
    )
    FilteringResultRepository(band_270).save(
        FilteringResult(date="2026-10-07", symbols=["2222"], generated_at="")
    )
    FilteringResultRepository(band_450).save(
        FilteringResult(date="2026-10-06", symbols=["3333"], generated_at="")
    )
    FilteringResultRepository(band_450).save(
        FilteringResult(date="2026-10-07", symbols=[], generated_at="")
    )
    monkeypatch.setitem(estimator.FILTERING_DIRECTORIES, 270, band_270)
    monkeypatch.setitem(estimator.FILTERING_DIRECTORIES, 450, band_450)
    monkeypatch.setattr(estimator, "is_trading_day", lambda _: True)

    assert estimator._discover_comparison_days(date(2026, 10, 1), None) == [
        date(2026, 10, 6)
    ]


def test_load_market_assessment_uses_only_available_saved_report(tmp_path, monkeypatch):
    monkeypatch.setattr(estimator, "DAILY_REPORT_DIRECTORY", tmp_path)
    (tmp_path / "2026-10-06.json").write_text(json.dumps({
        "date": "2026-10-06",
        "market_conditions": {
            "assessment_status": "available",
            "regime": "CAUTION",
            "realized_volatility_percent": 20.0,
            "vix": 16.0,
            "nikkei_change_percent": -0.5,
            "adx": 19.0,
            "data_available": True,
            "failure_reason": None,
        },
    }), encoding="utf-8")
    (tmp_path / "2026-10-07.json").write_text(json.dumps({
        "date": "2026-10-07",
        "market_conditions": {"assessment_status": "not_evaluated"},
    }), encoding="utf-8")

    assessment = estimator._load_market_assessment(date(2026, 10, 6))

    assert assessment is not None
    assert assessment.regime.value == "CAUTION"
    assert estimator._load_market_assessment(date(2026, 10, 7)) is None


def test_realized_pnl_by_symbol_calculates_before_costs_and_requires_flat_positions():
    orders = [
        {"Symbol": "1111", "Side": config.OrderSide.BUY.value, "Qty": 100, "Price": 100.0},
        {"Symbol": "1111", "Side": config.OrderSide.SELL.value, "Qty": 100, "Price": 110.0},
        {"Symbol": "2222", "Side": config.OrderSide.BUY.value, "Qty": 100, "Price": 100.0},
        {"Symbol": "2222", "Side": config.OrderSide.SELL.value, "Qty": 100, "Price": 90.0},
        {"Symbol": "3333", "Side": config.OrderSide.BUY.value, "Qty": 100, "Price": 100.0},
        {"Symbol": "3333", "Side": config.OrderSide.SELL.value, "Qty": 100, "Price": 100.0},
    ]

    assert estimator._realized_pnl_by_symbol(orders) == {
        "1111": 1_000.0,
        "2222": -1_000.0,
        "3333": 0.0,
    }


def test_aggregate_marks_missing_selection_as_not_simulated():
    summary = estimator._aggregate([], [{
        "reason": "filtering_result_missing",
    }])

    assert summary["analysis_status"] == "no_saved_selections"
    assert summary["completed_days"] == 0
    assert summary["buy_signal_count"] == 0


def test_aggregate_does_not_report_input_failures_as_zero_activity():
    summary = estimator._aggregate([{
        "status": "no_minute_bars",
        "selected_count": 10,
    }])

    assert summary["analysis_status"] == "not_simulatable"


def test_aggregate_marks_small_sample_as_reference_and_splits_regime_counts():
    summary = estimator._aggregate([{
        "status": "simulated",
        "market_regime": "CAUTION",
        "selected_count": 30,
        "buy_signal_count": 3,
        "purchased_symbol_count": 1,
    }])

    assert summary["sample_assessment"] == "reference"
    assert summary["buy_signals_by_market_regime"] == {"CAUTION": 3}
    assert summary["purchased_by_market_regime"] == {"CAUTION": 1}
