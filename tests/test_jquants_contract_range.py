from datetime import date

import pytest

from src.domain.jquants_contract_range import (
    calculate_earliest_available_date,
    classify_client_request_error,
    is_within_contract,
    sort_backfill_dates,
)

TODAY = date(2026, 10, 25)


def test_measured_boundaries_daily():
    assert is_within_contract("daily", date(2021, 10, 25), TODAY)
    assert not is_within_contract("daily", date(2021, 10, 24), TODAY)
    assert not is_within_contract("daily", date(2021, 10, 22), TODAY)


def test_measured_boundaries_minute():
    assert is_within_contract("minute", date(2024, 10, 25), TODAY)
    assert not is_within_contract("minute", date(2024, 10, 24), TODAY)


def test_future_date_is_out_of_range():
    assert not is_within_contract("daily", date(2026, 10, 26), TODAY)


def test_leap_day_fallback_when_start_year_not_leap():
    assert calculate_earliest_available_date("minute", date(2028, 2, 29)) == date(2026, 3, 1)


def test_unknown_dataset_raises():
    with pytest.raises(ValueError):
        calculate_earliest_available_date("weekly", TODAY)


def test_sort_backfill_dates_oldest_first_and_drops_out_of_range():
    dates = [date(2026, 10, 20), date(2021, 10, 22), date(2021, 10, 25), date(2023, 1, 4)]
    assert sort_backfill_dates("daily", dates, TODAY) == [
        date(2021, 10, 25),
        date(2023, 1, 4),
        date(2026, 10, 20),
    ]


def test_classify_400():
    assert classify_client_request_error("daily", date(2021, 10, 22), TODAY) == "OUT_OF_CONTRACT_RANGE"
    assert classify_client_request_error("daily", date(2022, 1, 4), TODAY) == "UNEXPECTED_400_IN_RANGE"
