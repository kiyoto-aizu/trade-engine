import json
from datetime import date, datetime

import pytest

from src.application.analysis_notification import daily_notification_lines, period_notification_lines
from src.application.trend_check_usecase import TrendCheckPaths
from src.entrypoints import run_daily_analysis


def _paths(tmp_path):
    return TrendCheckPaths(
        filtering_dir=tmp_path / "filtering",
        screening_dir=tmp_path / "screening",
        diagnostics_dir=tmp_path / "diagnostics",
        daily_cache_dir=tmp_path / "cache",
        minute_bar_dir=tmp_path / "minute_bars",
        daily_report_dir=tmp_path / "daily_reports",
        order_history_file=tmp_path / "orders.json",
        decision_database=tmp_path / "decisions.sqlite3",
        midday_filtering_dir=tmp_path / "filtering_midday",
    )


def test_daily_cache_refresh_retries_only_unupdated_symbols(monkeypatch, tmp_path):
    paths = _paths(tmp_path)
    monkeypatch.setattr(run_daily_analysis, "symbols_for_date", lambda *_: ["AAA", "BBB"])
    monkeypatch.setattr(
        run_daily_analysis,
        "price_band_symbols_for_update",
        lambda *_: ["BBB", "CCC"],
    )
    calls = []
    sleeps = []

    def update(cache_dir, symbols, days, now):
        calls.append((list(symbols), days, now))
        if len(calls) == 1:
            return {"AAA": "2026-10-06"}
        return {symbol: "2026-10-06" for symbol in symbols}

    success, missing = run_daily_analysis.refresh_cache_with_retries(
        date(2026, 10, 6),
        paths,
        retries=3,
        wait_seconds=60,
        now=datetime(2026, 10, 6, 16, 0),
        update=update,
        sleeper=sleeps.append,
    )

    assert success is True
    assert missing == []
    assert [call[0] for call in calls] == [
        ["AAA", "BBB", "CCC"],
        ["BBB", "CCC"],
    ]
    assert sleeps == [60]


def test_daily_notification_marks_failed_daily_refresh_as_undecidable():
    lines = daily_notification_lines(
        {"order_count": 1, "realized_profit_loss": 100},
        {"aggregate": {"pooled_selected": {"n": 10}}},
        cache_update_ok=False,
        analysis=None,
    )

    assert "判定不能(日足更新失敗)" in lines
    assert not any("候補全体:" in line for line in lines)


@pytest.mark.parametrize("content", ["{broken-json", "[]"])
def test_corrupt_daily_report_is_treated_as_missing(
    tmp_path, caplog, monkeypatch, content
):
    paths = _paths(tmp_path)
    paths.daily_report_dir.mkdir(parents=True)
    (paths.daily_report_dir / "2026-10-06.json").write_text(
        content, encoding="utf-8"
    )

    with caplog.at_level("WARNING"):
        report = run_daily_analysis._load_daily_report(paths, date(2026, 10, 6))

    assert report is None
    assert "日次レポートを読み込めません" in caplog.text or "形式が不正です" in caplog.text
    monkeypatch.setattr(
        run_daily_analysis, "load_selected_symbols", lambda *_: ["AAA"]
    )
    assert run_daily_analysis.resolve_no_trade_reason(
        report, paths, date(2026, 10, 6)
    ) == {"reason": run_daily_analysis.OTHER, "detail": "日次レポートなし"}


def test_valid_daily_report_is_loaded_unchanged(tmp_path):
    paths = _paths(tmp_path)
    paths.daily_report_dir.mkdir(parents=True)
    report = {"date": "2026-10-06", "order_count": 2}
    (paths.daily_report_dir / "2026-10-06.json").write_text(
        json.dumps(report), encoding="utf-8"
    )

    assert run_daily_analysis._load_daily_report(paths, date(2026, 10, 6)) == report


def test_daily_analysis_notifies_even_when_daily_refresh_fails(monkeypatch, tmp_path):
    paths = _paths(tmp_path)
    notifications = []
    trend_check_calls = []
    monkeypatch.setattr(
        run_daily_analysis,
        "refresh_cache_with_retries",
        lambda *args, **kwargs: (False, ["AAA"]),
    )
    monkeypatch.setattr(
        run_daily_analysis,
        "run_daily_safely",
        lambda *args, **kwargs: trend_check_calls.append((args, kwargs)) or True,
    )
    monkeypatch.setattr(run_daily_analysis, "_load_daily_report", lambda *args: None)
    monkeypatch.setattr(run_daily_analysis, "create_daily_analyzer", lambda: None)
    monkeypatch.setattr(run_daily_analysis, "notify_analysis", notifications.append)

    success = run_daily_analysis.run_daily_analysis(
        date(2026, 10, 6),
        paths=paths,
        database_file=tmp_path / "trend.sqlite3",
        now=datetime(2026, 10, 6, 17, 0),
        sleeper=lambda _: None,
    )

    assert success is False
    assert len(notifications) == 1
    assert trend_check_calls == []
    assert "【機能】日次分析" in notifications[0]
    assert "判定不能(日足更新失敗)" in notifications[0]


def test_daily_analysis_main_runs_on_trading_day(monkeypatch):
    analysis_calls = []
    minute_bar_calls = []
    monkeypatch.setattr(run_daily_analysis, "configure_logging", lambda: None)
    monkeypatch.setattr(
        run_daily_analysis,
        "run_daily_analysis",
        lambda target_date: analysis_calls.append(target_date) or True,
    )
    monkeypatch.setattr(
        run_daily_analysis,
        "save_index_minute_bars",
        lambda start, end: minute_bar_calls.append((start, end)),
    )

    result = run_daily_analysis.main(["--date", "2026-10-06"])

    assert result == 0
    assert analysis_calls == [date(2026, 10, 6)]
    assert minute_bar_calls == [(date(2026, 10, 6), date(2026, 10, 6))]


@pytest.mark.parametrize(
    "target_date",
    [
        "2026-10-10",  # 土曜
        "2026-10-11",  # 日曜
        "2026-09-21",  # 敬老の日
        "2026-12-31",
        "2026-01-02",
        "2026-01-03",
    ],
)
def test_daily_analysis_main_skips_closed_days_without_side_effects(
    monkeypatch, caplog, target_date
):
    analysis_calls = []
    minute_bar_calls = []
    notification_calls = []
    monkeypatch.setattr(run_daily_analysis, "configure_logging", lambda: None)
    monkeypatch.setattr(
        run_daily_analysis,
        "run_daily_analysis",
        lambda target: analysis_calls.append(target),
    )
    monkeypatch.setattr(
        run_daily_analysis,
        "save_index_minute_bars",
        lambda *args: minute_bar_calls.append(args),
    )
    monkeypatch.setattr(
        run_daily_analysis,
        "notify_analysis",
        notification_calls.append,
    )

    with caplog.at_level("INFO", logger=run_daily_analysis.__name__):
        result = run_daily_analysis.main(["--date", target_date])

    assert result == 0
    assert "休場日のため日次分析をスキップします" in caplog.text
    assert analysis_calls == []
    assert minute_bar_calls == []
    assert notification_calls == []


def test_daily_analysis_main_force_runs_on_closed_day(monkeypatch):
    analysis_calls = []
    minute_bar_calls = []
    monkeypatch.setattr(run_daily_analysis, "configure_logging", lambda: None)
    monkeypatch.setattr(
        run_daily_analysis,
        "run_daily_analysis",
        lambda target_date: analysis_calls.append(target_date) or True,
    )
    monkeypatch.setattr(
        run_daily_analysis,
        "save_index_minute_bars",
        lambda start, end: minute_bar_calls.append((start, end)),
    )

    result = run_daily_analysis.main(["--date", "2026-10-11", "--force"])

    assert result == 0
    assert analysis_calls == [date(2026, 10, 11)]
    assert minute_bar_calls == [(date(2026, 10, 11), date(2026, 10, 11))]


def test_period_notice_separates_backtest_and_states_missing_exact_period():
    summary = {
        "daily": {
            "report_count": 0,
            "total_profit_loss": 0,
            "order_count": 0,
            "operational_summary": {
                "market_assessment_status_counts": {},
                "log_error_count": 0,
                "emergency_stop_days": 0,
            },
            "reports": [],
        },
        "backtest": {
            "exact_period_run_available": False,
            "total_pnl": None,
            "total_trades": None,
        },
    }

    lines = period_notification_lines(summary, None, "月次", None)

    assert lines.index("A. ペーパートレード実績 (月次)") < lines.index("B. 戦略の答え合わせ")
    assert lines.index("B. 戦略の答え合わせ") < lines.index("C. バックテスト（別枠）")
    assert lines.index("C. バックテスト（別枠）") < lines.index("D. 次回確認")
    assert "なし（対象期間に一致するバックテスト結果なし）" in lines
    assert any("判定できなかった日:" in line for line in lines)
