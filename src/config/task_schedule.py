from calendar import monthrange
from dataclasses import dataclass
from datetime import date
from typing import Literal

from src.config import config
from src.infrastructure.calendar.japanese_calendar import is_trading_day


@dataclass(frozen=True)
class ScheduledTask:
    name: str
    display_time: str
    days: Literal["weekday", "saturday", "daily"]
    month_end_only: bool = False


TASKS: list[ScheduledTask] = [
    ScheduledTask("フィルタリング", "09:30", "weekday"),
    ScheduledTask("フィルタリング(昼)", config.MIDDAY_FILTERING_START_TIME.strftime("%H:%M"), "weekday"),
    ScheduledTask("取引", "09:35-15:30", "weekday"),
    ScheduledTask("スクリーニング", "15:35", "weekday"),
    ScheduledTask("日次分析", config.DAILY_ANALYSIS_START_TIME.strftime("%H:%M"), "weekday"),
    ScheduledTask("分足バックフィル", "07:30", "saturday"),
    ScheduledTask("バックテスト", "08:00", "saturday"),
    ScheduledTask("上場銘柄マスタ更新", "09:00", "saturday"),
    ScheduledTask("週次分析", "10:00", "saturday"),
    ScheduledTask("月次総合分析", "17:00", "daily", month_end_only=True),
]


def is_last_trading_day_of_month(target_date: date) -> bool:
    if not is_trading_day(target_date):
        return False

    last_day = monthrange(target_date.year, target_date.month)[1]
    return all(
        not is_trading_day(date(target_date.year, target_date.month, day))
        for day in range(target_date.day + 1, last_day + 1)
    )


def tasks_for_date(target_date: date) -> list[ScheduledTask]:
    if target_date.weekday() == 5:
        active_days = {"saturday"}
    elif is_trading_day(target_date):
        active_days = {"weekday"}
    else:
        active_days = set()

    tasks = [
        task for task in TASKS
        if task.days in active_days
        or (task.days == "daily" and is_last_trading_day_of_month(target_date))
    ]
    return sorted(tasks, key=lambda task: task.display_time)