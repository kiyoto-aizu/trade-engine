import json
import sys
from datetime import date

import pytest

from src.entrypoints import run_weekly_analysis
from src.infrastructure.analysis.daily_analyzer import OpenAIDailyAnalyzer


def test_week_bounds_returns_monday_to_friday_across_month_boundary():
    assert run_weekly_analysis._week_bounds(date(2026, 9, 1)) == (
        date(2026, 8, 31),
        date(2026, 9, 4),
    )
    assert run_weekly_analysis._week_bounds(date(2026, 9, 4)) == (
        date(2026, 8, 31),
        date(2026, 9, 4),
    )


def test_build_weekly_summary_filters_both_sources_by_inclusive_date_range(tmp_path):
    reports = tmp_path / "reports"
    backtests = tmp_path / "backtests"
    reports.mkdir()
    backtests.mkdir()
    for report_date, orders in (("2026-09-07", 2), ("2026-09-11", 1), ("2026-09-12", 99)):
        (reports / f"{report_date}.json").write_text(
            json.dumps({"date": report_date, "order_count": orders, "total_profit_loss": orders * 10}),
            encoding="utf-8",
        )
    for name, generated_at, pnl in (
        ("inside.json", "2026-09-12T09:00:00", 100),
        ("outside.json", "2026-09-12T09:00:00", 999),
    ):
        (backtests / f"latest_timeseries_{name}").write_text(
            json.dumps({
                "generated_at": generated_at,
                "period_start": "2026-09-07" if name == "inside.json" else None,
                "period_end": "2026-09-11" if name == "inside.json" else None,
                "total_pnl": pnl,
                "total_trades": 2,
            }),
            encoding="utf-8",
        )

    summary = run_weekly_analysis.build_weekly_summary(
        date(2026, 9, 7), date(2026, 9, 11), reports, backtests, as_of=date(2026, 9, 12)
    )

    assert summary["period"]["status"] == "complete"
    assert summary["daily"]["report_count"] == 2
    assert summary["daily"]["order_count"] == 3
    assert summary["backtest"]["run_count"] == 1
    assert summary["backtest"]["exact_period_run_available"] is True
    assert summary["backtest"]["total_pnl"] == 100
    assert summary["comparison"]["available"] is False


def test_main_skips_non_saturday_without_force(monkeypatch, tmp_path):
    class Wednesday:
        @staticmethod
        def today():
            return date(2026, 9, 9)

    monkeypatch.setattr(run_weekly_analysis, "date", Wednesday)
    monkeypatch.setattr(sys, "argv", ["run_weekly_analysis", "--output", str(tmp_path)])

    run_weekly_analysis.main()

    assert not list(tmp_path.glob("*.json"))


def test_main_rejects_non_monday_week_start(monkeypatch):
    monkeypatch.setattr(
        sys, "argv", ["run_weekly_analysis", "--force", "--week-start", "2026-09-13"]
    )

    with pytest.raises(SystemExit) as exc_info:
        run_weekly_analysis.main()

    assert exc_info.value.code == 2


def test_main_force_writes_result_and_notifies(monkeypatch, tmp_path):
    notifications = []
    monkeypatch.setattr(run_weekly_analysis, "create_daily_analyzer", lambda: None)
    monkeypatch.setattr(run_weekly_analysis, "notify_analysis", notifications.append)
    monkeypatch.setattr(
        sys,
        "argv",
        ["run_weekly_analysis", "--force", "--week-start", "2026-09-07", "--output", str(tmp_path)],
    )

    run_weekly_analysis.main()

    output = tmp_path / "2026-09-07_2026-09-11.json"
    assert output.exists()
    assert notifications[0].startswith("【業務】分析運用\n【機能】週次分析\n【概要】")
    assert "対象週: 2026-09-07～2026-09-11" in notifications[0]


def test_weekly_analyzer_uses_required_review_prompt(monkeypatch):
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
    result = OpenAIDailyAnalyzer("key", "model", "https://example.test").analyze_weekly({"week_start": "2026-09-07"})

    prompt = captured["payload"]["messages"][1]["content"]
    assert result.startswith("観測事実")
    assert "週次集計" in prompt
    assert "観測事実" in prompt
    assert "期間・稼働状態" in prompt
    assert "exact_period_run_available" in prompt
    assert "comparison.availableがtrue" in prompt
    assert "same_dayの当日基準だけ" in prompt
    assert "複数営業日基準は使わない" in prompt
    assert "投資判断、売買指示は行わず" in prompt
    assert "参考意見" in prompt


def test_strategy_review_summary_reuses_existing_openai_auth_and_limits_scope(monkeypatch):
    captured = {}

    class DummyResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {"choices": [{"message": {"content": "横断所見\n- 次の確認案"}}]}

    def post(url, **kwargs):
        captured["url"] = url
        captured.update(kwargs)
        return DummyResponse()

    monkeypatch.setattr("src.infrastructure.analysis.daily_analyzer.requests.post", post)
    analyzer = OpenAIDailyAnalyzer("existing-key", "existing-model", "https://example.test")
    result = analyzer.analyze_strategy_review({"counts": {"supported": 1}, "hypotheses": []})

    assert result == "横断所見\n- 次の確認案"
    assert captured["headers"]["Authorization"] == "Bearer existing-key"
    assert captured["json"]["model"] == "existing-model"
    assert len(captured["json"]["messages"]) == 2
    assert "結論や優先度を上書きせず" in captured["json"]["messages"][0]["content"]


def test_daily_analyzer_prompt_uses_event_agnostic_review_contract(monkeypatch):
    captured = {}

    class DummyResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {"choices": [{"message": {"content": "今日の評価\n- 参考評価"}}]}

    def post(url, **kwargs):
        captured["payload"] = kwargs["json"]
        return DummyResponse()

    monkeypatch.setattr("src.infrastructure.analysis.daily_analyzer.requests.post", post)
    OpenAIDailyAnalyzer("key", "model", "https://example.test").analyze(
        {"order_count": 0, "skip_counts": {}, "trend_check": {"available": False}}
    )

    prompt = captured["payload"]["messages"][1]["content"]
    assert "【事実】" in prompt
    assert "運用・データ状態" in prompt
    assert "skip_countsが空でない場合" in prompt
    assert "assessment_statusがnot_evaluated" in prompt
    assert "複数営業日基準の結果は扱わない" in prompt
    assert "trend_check.availableがfalse" in prompt
    assert "future_control_skips" not in prompt