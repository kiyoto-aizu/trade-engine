import json
from datetime import date

from src.entrypoints import run_monthly_analysis
from src.infrastructure.analysis.daily_analyzer import OpenAIDailyAnalyzer


def test_build_monthly_summary_marks_in_progress_and_keeps_operational_context(tmp_path):
    reports = tmp_path / "reports"
    backtests = tmp_path / "backtests"
    reports.mkdir()
    backtests.mkdir()
    (reports / "2026-09-16.json").write_text(
        json.dumps({
            "date": "2026-09-16",
            "order_count": 0,
            "total_profit_loss": 0,
            "kill_switch_triggered": False,
            "emergency_stop_triggered": False,
            "market_conditions": {"assessment_status": "not_evaluated"},
            "log_errors": {"count": 1, "summaries": ["認証エラー"]},
        }),
        encoding="utf-8",
    )

    summary = run_monthly_analysis.build_monthly_summary(
        "2026-09", reports, backtests, as_of=date(2026, 9, 16)
    )

    assert summary["period"]["status"] == "in_progress"
    assert summary["daily"]["operational_summary"]["log_error_count"] == 1
    assert summary["daily"]["operational_summary"]["market_assessment_status_counts"] == {
        "not_evaluated": 1
    }
    assert summary["daily"]["operational_summary"]["market_status_counts"] == {"未判定": 1}
    assert summary["daily"]["operational_summary"]["log_error_unrecorded_days"] == 0
    assert summary["backtest"]["summary_status"] == "no_runs"
    assert summary["backtest"]["total_pnl"] is None
    assert summary["comparison"]["available"] is False


def test_monthly_analyzer_uses_period_aware_review_prompt(monkeypatch):
    captured = {}

    class DummyResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {"choices": [{"message": {"content": "観測事実\n- 参考評価"}}]}

    def post(url, **kwargs):
        captured["payload"] = kwargs["json"]
        return DummyResponse()

    monkeypatch.setattr("src.infrastructure.analysis.daily_analyzer.requests.post", post)
    result = OpenAIDailyAnalyzer("key", "model", "https://example.test").analyze_monthly(
        {"period": {"status": "in_progress"}}
    )

    prompt = captured["payload"]["messages"][1]["content"]
    assert result.startswith("観測事実")
    assert "月次集計" in prompt
    assert "period.statusがcomplete以外" in prompt
    assert "exact_period_run_available" in prompt
    assert "comparison.availableがtrue" in prompt
    assert "same_dayの当日基準だけ" in prompt
    assert "複数営業日基準は使わない" in prompt
    assert "board_timeout.recoveredは問題なし" in prompt
    assert "final_failureとunrecordedだけ" in prompt
    assert "ログ本文は入力に含まれていません" in prompt