from datetime import date, datetime

import pytest

from src.domain.us_market_calendar import (
    expected_vix_session_date,
    is_us_equity_trading_day,
    latest_confirmed_vix_date,
)


@pytest.mark.parametrize(
    "holiday",
    [
        date(2026, 7, 3),
        date(2026, 11, 26),
        date(2026, 12, 25),
    ],
)
def test_scheduled_us_equity_holidays_are_not_trading_days(holiday):
    assert is_us_equity_trading_day(holiday) is False


def test_new_year_saturday_does_not_close_previous_friday():
    assert is_us_equity_trading_day(date(2027, 12, 31)) is True


def test_expected_vix_session_skips_weekend_and_us_holiday():
    assert expected_vix_session_date(date(2026, 11, 30)) == date(2026, 11, 27)


def test_latest_confirmed_vix_date_waits_for_regular_us_close():
    assert latest_confirmed_vix_date(datetime(2026, 11, 5, 5, 59)) == date(2026, 11, 3)
    assert latest_confirmed_vix_date(datetime(2026, 11, 5, 6, 1)) == date(2026, 11, 4)


def test_latest_confirmed_vix_date_uses_expected_session_on_holidays():
    assert latest_confirmed_vix_date(datetime(2026, 11, 27, 8, 0)) == date(2026, 11, 25)
