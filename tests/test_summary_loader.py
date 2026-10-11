import json
from datetime import date

from src.infrastructure.analysis.summary_loader import (
    build_daily_period_comparison,
    build_period_metadata,
    load_daily_summaries,
    summarize_backtest_runs,
    summarize_daily_operations,
    summarize_daily_reports,
)


def test_build_period_metadata_distinguishes_incomplete_periods():
    assert build_period_metadata(date(2026, 9, 14), date(2026, 9, 18), date(2026, 9, 16)) == {
        "start": "2026-09-14",
        "end": "2026-09-18",
        "as_of": "2026-09-16",
        "status": "in_progress",
        "is_complete": False,
    }


def test_load_daily_summaries_keeps_operational_context(tmp_path):
    (tmp_path / "2026-09-16.json").write_text(
        json.dumps({
            "date": "2026-09-16",
            "trading_mode": "ペーパートレード",
            "order_count": 0,
            "total_profit_loss": 0,
            "kill_switch_triggered": False,
            "emergency_stop_triggered": False,
            "positions": [],
            "market_conditions": {
                "assessment_status": "unavailable",
                "failure_reason": "日足データが空です",
            },
            "log_errors": {"count": 2, "summaries": ["認証エラー"]},
            "market_regime_caution_rsi_filters": [
                {
                    "evaluations": {
                        "same_day": {
                            "outcome": "損失回避の可能性",
                            "hypothetical_pnl_before_cost": -5600,
                            "finalized": True,
                        }
                    }
                },
                {"evaluations": {"same_day": {"finalized": False}}},
            ],
        }),
        encoding="utf-8",
    )

    summaries = load_daily_summaries(tmp_path, date(2026, 9, 16), date(2026, 9, 16))

    assert summaries[0]["market_assessment_status"] == "unavailable"
    assert summaries[0]["log_error_summaries"] == ["認証エラー"]
    assert summaries[0]["position_count"] == 0
    assert summaries[0]["skip_counts"] == {"注意レジームRSI": 2}
    assert summaries[0]["skip_evaluations"]["注意レジームRSI"] == [
        {
            "outcome": "損失回避の可能性",
            "pnl_before_cost": -5600,
            "finalized": True,
        },
        {"outcome": None, "pnl_before_cost": None, "finalized": False},
    ]
    assert summarize_daily_operations(summaries) == {
        "log_error_days": 1,
        "log_error_count": 2,
        "emergency_stop_days": 0,
        "market_assessment_status_counts": {"unavailable": 1},
    }


def test_summarize_backtest_runs_does_not_sum_multiple_exact_period_runs():
    backtests = [
        {
            "period_start": "2026-09-14",
            "period_end": "2026-09-18",
            "total_pnl": 100,
            "total_trades": 2,
        },
        {
            "period_start": "2026-09-14",
            "period_end": "2026-09-18",
            "total_pnl": 200,
            "total_trades": 3,
        },
    ]

    summary = summarize_backtest_runs(backtests, date(2026, 9, 14), date(2026, 9, 18))

    assert summary["summary_status"] == "multiple_exact_period_runs"
    assert summary["exact_period_run_available"] is False
    assert summary["total_pnl"] is None
    assert summary["total_trades"] is None


def test_build_daily_period_comparison_requires_complete_matching_modes():
    current_summary = summarize_daily_reports([
        {
            "trading_mode": "ペーパートレード",
            "order_count": 2,
            "total_profit_loss": 100,
            "kill_switch_triggered": False,
            "emergency_stop_triggered": False,
            "market_assessment_status": "available",
            "log_error_count": 0,
        }
    ])
    previous_summary = summarize_daily_reports([
        {
            "trading_mode": "ペーパートレード",
            "order_count": 1,
            "total_profit_loss": 50,
            "kill_switch_triggered": False,
            "emergency_stop_triggered": False,
            "market_assessment_status": "available",
            "log_error_count": 0,
        }
    ])
    complete_period = build_period_metadata(
        date(2026, 9, 14), date(2026, 9, 18), date(2026, 9, 18)
    )
    previous_period = build_period_metadata(
        date(2026, 9, 7), date(2026, 9, 11), date(2026, 9, 11)
    )

    comparison = build_daily_period_comparison(
        complete_period, current_summary, previous_period, previous_summary
    )

    assert comparison["available"] is True
    assert comparison["basis"] == "同一取引モードの日次レポート集計"
    assert comparison["current"]["order_count"] == 2
    assert comparison["previous"]["order_count"] == 1

    incomplete_period = build_period_metadata(
        date(2026, 9, 14), date(2026, 9, 18), date(2026, 9, 16)
    )
    unavailable = build_daily_period_comparison(
        incomplete_period, current_summary, previous_period, previous_summary
    )

    assert unavailable["available"] is False
    assert unavailable["reason"] == "対象期間が未完了です"