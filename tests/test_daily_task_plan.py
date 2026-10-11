from datetime import date

from src.config.task_schedule import is_last_trading_day_of_month, tasks_for_date
from src.entrypoints import run_daily_task_plan


def test_weekday_schedule_excludes_japanese_holidays():
    assert [task.name for task in tasks_for_date(date(2026, 9, 28))] == [
        "フィルタリング",
        "取引",
        "フィルタリング(昼)",
        "スクリーニング",
        "日次分析",
    ]
    assert tasks_for_date(date(2026, 9, 22)) == []


def test_saturday_schedule_is_ordered_and_includes_listed_master_update():
    assert [(task.display_time, task.name) for task in tasks_for_date(date(2026, 9, 26))] == [
        ("07:30", "分足バックフィル"),
        ("08:00", "バックテスト"),
        ("09:00", "上場銘柄マスタ更新"),
        ("10:00", "週次分析"),
    ]


def test_monthly_task_uses_the_last_trading_day_not_calendar_month_end():
    assert is_last_trading_day_of_month(date(2026, 5, 29))
    assert not is_last_trading_day_of_month(date(2026, 5, 31))
    assert [task.name for task in tasks_for_date(date(2026, 5, 29))][-1] == "月次総合分析"


def test_plan_message_lists_tasks_and_marks_screening_for_next_business_day():
    message = run_daily_task_plan.build_task_plan_message(date(2026, 9, 28))

    assert message == (
        "【本日の実行予定タスク】2026-09-28(月)\n"
        "09:30 フィルタリング\n"
        "09:35-15:30 取引\n"
        "12:00 フィルタリング(昼)\n"
        "15:35 スクリーニング(翌営業日向け)\n"
        "16:30 日次分析"
    )


def test_plan_message_reports_weekday_market_holiday():
    assert run_daily_task_plan.build_task_plan_message(date(2026, 9, 22)) == (
        "【本日の実行予定タスク】2026-09-22(火)\n"
        "本日は休場日のため、フィルタリング/取引/スクリーニングはスキップされます。"
    )


def test_main_sends_plan_to_daily_channel(monkeypatch):
    messages = []
    monkeypatch.setattr(run_daily_task_plan, "configure_logging", lambda: None)
    monkeypatch.setattr(run_daily_task_plan, "notify_daily", messages.append)

    run_daily_task_plan.main(date(2026, 9, 28))

    assert len(messages) == 1
    assert messages[0].startswith("【本日の実行予定タスク】")