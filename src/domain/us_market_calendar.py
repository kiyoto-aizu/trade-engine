"""Scheduled US equity-market trading dates used for VIX freshness checks."""
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

JAPAN_TIMEZONE = ZoneInfo("Asia/Tokyo")
US_EASTERN_TIMEZONE = ZoneInfo("America/New_York")
US_EQUITY_CLOSE = time(16, 0)


def _nth_weekday(year: int, month: int, weekday: int, occurrence: int) -> date:
    first = date(year, month, 1)
    offset = (weekday - first.weekday()) % 7
    return first + timedelta(days=offset + 7 * (occurrence - 1))


def _last_weekday(year: int, month: int, weekday: int) -> date:
    last = date(year, month + 1, 1) - timedelta(days=1) if month < 12 else date(year, 12, 31)
    return last - timedelta(days=(last.weekday() - weekday) % 7)


def _easter_sunday(year: int) -> date:
    a = year % 19
    b, c = divmod(year, 100)
    d, e = divmod(b, 4)
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month, day = divmod(h + l - 7 * m + 114, 31)
    return date(year, month, day + 1)


def _observed_fixed_holiday(day: date, saturday_previous_day: bool = True) -> date:
    if day.weekday() == 5 and saturday_previous_day:
        return day - timedelta(days=1)
    if day.weekday() == 6:
        return day + timedelta(days=1)
    return day


def _us_equity_holidays(year: int) -> frozenset[date]:
    new_year = date(year, 1, 1)
    holidays = {
        _observed_fixed_holiday(new_year, saturday_previous_day=False),
        _nth_weekday(year, 2, 0, 3),
        _easter_sunday(year) - timedelta(days=2),
        _last_weekday(year, 5, 0),
        _observed_fixed_holiday(date(year, 7, 4)),
        _nth_weekday(year, 9, 0, 1),
        _nth_weekday(year, 11, 3, 4),
        _observed_fixed_holiday(date(year, 12, 25)),
    }
    if year >= 1998:
        holidays.add(_nth_weekday(year, 1, 0, 3))
    if year >= 2022:
        holidays.add(_observed_fixed_holiday(date(year, 6, 19)))
    return frozenset(day for day in holidays if day.year == year and day.weekday() < 5)


def is_us_equity_trading_day(day: date) -> bool:
    """Return whether this is a weekday outside scheduled NYSE holidays."""
    return day.weekday() < 5 and day not in _us_equity_holidays(day.year)


def previous_us_equity_trading_day(day: date) -> date:
    """Return the latest NYSE session strictly before the supplied date."""
    candidate = day - timedelta(days=1)
    while not is_us_equity_trading_day(candidate):
        candidate -= timedelta(days=1)
    return candidate


def expected_vix_session_date(reference_date: date) -> date:
    """Return the latest scheduled US session strictly before a reference date."""
    return previous_us_equity_trading_day(reference_date)


def latest_confirmed_vix_date(now: datetime) -> date:
    """Return the latest US session whose regular close has passed.

    Naive datetimes are interpreted as Japan local time, matching the runtime's
    local timestamps. Aware datetimes are converted by their represented instant.
    """
    local_now = (
        now.replace(tzinfo=JAPAN_TIMEZONE)
        if now.tzinfo is None
        else now
    )
    us_now = local_now.astimezone(US_EASTERN_TIMEZONE)
    if (
        is_us_equity_trading_day(us_now.date())
        and us_now.time().replace(tzinfo=None) >= US_EQUITY_CLOSE
    ):
        return us_now.date()
    return expected_vix_session_date(us_now.date())
