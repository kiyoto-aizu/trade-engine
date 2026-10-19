import json
from datetime import date

from src.infrastructure.analysis.summary_loader import (
    build_daily_period_comparison,
    build_period_metadata,
    load_backtest_summaries,
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
    assert summaries[0]["log_errors"] == {
        "recorded": True,
        "count": 2,
        "board_timeout": {
            "count": 0, "recovered": 0, "final_failure": 0, "unrecorded": 0,
            "symbols": {"recovered": [], "final_failure": [], "unrecorded": []},
        },
        "other_errors": {
            "count": 2, "groups": [{"title": "詳細未記録", "count": 2}],
        },
    }
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
        "log_error_unrecorded_days": 0,
        "emergency_stop_days": 0,
        "market_assessment_status_counts": {"unavailable": 1},
        "market_status_counts": {"判定不可": 1},
        "board_timeout": {
            "count": 0, "recovered": 0, "final_failure": 0, "unrecorded": 0,
            "symbols": {"recovered": [], "final_failure": [], "unrecorded": []},
        },
        "other_errors": {
            "count": 2, "groups": [{"title": "詳細未記録", "count": 2}],
        },
    }


def test_load_daily_summaries_uses_recorded_nikkei_fallback_without_raw_errors(tmp_path):
    (tmp_path / "2026-10-16.json").write_text(
        json.dumps({
            "date": "2026-10-16",
            "order_count": 0,
            "total_profit_loss": 0,
            "kill_switch_triggered": False,
            "emergency_stop_triggered": False,
            "positions": [],
            "market_conditions": {
                "assessment_status": "available",
                "nikkei_fallback_used": True,
            },
            "log_errors": {
                "count": 2,
                "summaries": ["❌ [GET通信エラー] URL: http://localhost/board/AAA@1 | 理由: Read timed out"],
            },
        }),
        encoding="utf-8",
    )

    summaries = load_daily_summaries(tmp_path, date(2026, 10, 16), date(2026, 10, 16))

    assert summaries[0]["market_assessment_status"] == "available_fallback"
    assert summaries[0]["log_errors"]["board_timeout"]["unrecorded"] == 2
    assert summaries[0]["log_errors"]["board_timeout"]["symbols"]["unrecorded"] == ["AAA"]
    assert "log_error_summaries" not in summaries[0]
    assert "localhost" not in json.dumps(summaries[0], ensure_ascii=False)


def test_summarize_daily_operations_keeps_fallback_state_separate():
    operations = summarize_daily_operations([
        {"market_assessment_status": "available"},
        {"market_assessment_status": "available_fallback"},
        {"market_assessment_status": "unavailable"},
    ])

    assert operations["market_status_counts"] == {
        "判定あり": 1, "判定あり(補完)": 1, "判定不可": 1,
    }


def test_summarize_backtest_runs_uses_latest_of_multiple_exact_period_runs():
    backtests = [
        {
            "generated_timestamp": "2026-09-19T17:09:39",
            "period_start": "2026-09-14",
            "period_end": "2026-09-18",
            "total_pnl": 200,
            "total_trades": 3,
        },
        {
            "generated_timestamp": "2026-09-19T08:06:50",
            "period_start": "2026-09-14",
            "period_end": "2026-09-18",
            "total_pnl": 100,
            "total_trades": 2,
        },
    ]

    summary = summarize_backtest_runs(backtests, date(2026, 9, 14), date(2026, 9, 18))

    assert summary["summary_status"] == "latest_of_multiple_exact_period_runs"
    assert summary["matching_period_run_count"] == 2
    assert summary["exact_period_run_available"] is True
    assert summary["total_pnl"] == 200
    assert summary["total_trades"] == 3


def test_summarize_backtest_runs_matches_requested_period_when_evaluated_period_is_shorter():
    backtests = [
        {
            "period_start": "2026-10-13",
            "period_end": "2026-10-16",
            "requested_period_start": "2026-10-12",
            "requested_period_end": "2026-10-16",
            "total_pnl": 100,
            "total_trades": 2,
        },
        {
            "period_start": "2026-09-01",
            "period_end": "2026-10-16",
            "total_pnl": -5,
            "total_trades": 26,
        },
    ]

    summary = summarize_backtest_runs(backtests, date(2026, 10, 12), date(2026, 10, 16))

    assert summary["summary_status"] == "single_exact_period_run"
    assert summary["exact_period_run_available"] is True
    assert summary["total_pnl"] == 100


def test_load_backtest_summaries_reads_weekly_runs_with_requested_period(tmp_path):
    (tmp_path / "latest_weekly_20261017_080650_731604.json").write_text(
        json.dumps({
            "generated_at": "2026-10-17T08:06:50",
            "period_start": "2026-10-13",
            "period_end": "2026-10-16",
            "requested_period_start": "2026-10-12",
            "requested_period_end": "2026-10-16",
            "total_pnl": 0.0,
            "total_trades": 0,
        }),
        encoding="utf-8",
    )

    backtests = load_backtest_summaries(tmp_path, date(2026, 10, 12), date(2026, 10, 16))
    summary = summarize_backtest_runs(backtests, date(2026, 10, 12), date(2026, 10, 16))

    assert summary["exact_period_run_available"] is True
    assert summary["total_trades"] == 0


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