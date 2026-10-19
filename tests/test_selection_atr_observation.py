import json
from datetime import date, timedelta

import pytest

from src.application.screening_usecase import ScreeningUseCase
from src.application.selection_atr_observer import SelectionAtrObserver
from src.domain.atr_ratio_analysis import DatedDailyBar
from src.domain.enums import RankingType
from src.domain.models import RankingEntry, Regulation, ScoredCandidate, ScreeningResult
from src.domain.selection_atr_observation import (
    REASON_FETCH_FAILED,
    REASON_INSUFFICIENT_BARS,
    REASON_INVALID_BAR,
    REASON_LATEST_BAR_MISMATCH,
    REASON_NO_DAILY_BARS,
    REASON_NOT_OBSERVED_PRICE_FILTERED,
    REASON_ZERO_ATR,
    build_atr_below_comparison,
    observe_selection_atr,
)
from src.domain.volatility import DailyBar
from src.infrastructure.persistence.screening_result_repository import ScreeningResultRepository

AS_OF = date(2026, 10, 14)


def make_bars(ranges, as_of=AS_OF):
    """終値を100で固定し、日ごとの値幅(=True Range)がrangesになる日足を、最後がas_ofになるよう作る。"""
    bars = []
    for offset, width in enumerate(ranges):
        day = as_of - timedelta(days=len(ranges) - 1 - offset)
        bars.append(DatedDailyBar(day, DailyBar(high=100 + width / 2, low=100 - width / 2, close=100.0)))
    return bars


def observe(ranges, **kwargs):
    return observe_selection_atr(make_bars(ranges), AS_OF, period=3, **kwargs)


@pytest.mark.parametrize(
    "latest_range, level",
    [(19.99, "NORMAL"), (20.0, "CAUTION"), (39.99, "CAUTION"), (40.0, "DANGER")],
)
def test_level_boundaries_at_1_5_and_2_0(latest_range, level):
    # 直前2本のTR=10。最新TR=20でATR比=1.5、40で2.0
    observation = observe([10, 10, latest_range])

    assert observation.level == level
    assert observation.unavailable_reason is None
    assert observation.as_of == "2026-10-14"


def test_ratio_value_is_latest_true_range_over_atr():
    observation = observe([10, 10, 20])

    assert observation.ratio == pytest.approx(20 / ((10 + 10 + 20) / 3))


def test_future_bars_are_ignored():
    bars = make_bars([10, 10, 20]) + [
        DatedDailyBar(AS_OF + timedelta(days=1), DailyBar(high=200, low=50, close=100.0))
    ]

    observation = observe_selection_atr(bars, AS_OF, period=3)

    assert observation.level == "CAUTION"


@pytest.mark.parametrize(
    "bars, reason",
    [
        ([], REASON_NO_DAILY_BARS),
        (make_bars([10, 20]), REASON_INSUFFICIENT_BARS),
        (make_bars([10, 10, 20], as_of=AS_OF - timedelta(days=1)), REASON_LATEST_BAR_MISMATCH),
        (make_bars([0, 0, 0]), REASON_ZERO_ATR),
    ],
)
def test_unavailable_reasons(bars, reason):
    observation = observe_selection_atr(bars, AS_OF, period=3)

    assert observation.ratio is None and observation.level is None
    assert observation.unavailable_reason == reason


def test_invalid_bar_is_reported_not_raised():
    bars = make_bars([10, 10, 20])
    bars[0] = DatedDailyBar(bars[0].date, DailyBar(high=90.0, low=110.0, close=100.0))

    assert observe_selection_atr(bars, AS_OF, period=3).unavailable_reason == REASON_INVALID_BAR


def test_atr_below_comparison_keeps_top_by_ratio_and_records_shortfall():
    scored = [
        ScoredCandidate("A", 1.0, 1.0, 9.0),
        ScoredCandidate("B", 1.0, 1.0, 8.0),
        ScoredCandidate("C", 1.0, 1.0, 7.0),
        ScoredCandidate("D", 1.0, 1.0, 6.0),
    ]
    atr = {"A": 2.0, "B": 1.99, "C": None, "D": 0.5}

    result = build_atr_below_comparison(scored, atr, ["A", "B", "C"], limit_ratio=2.0, n=3)

    assert result["symbols"] == ["B", "D"]
    assert result["shortfall"] == 1
    assert result["unknown_atr_count"] == 1
    assert result["replaced_out"] == ["A", "C"]
    assert result["replaced_in"] == ["D"]
    assert result["comparison_only"] is True


class RankingStub:
    def get_ranking(self, ranking_type, exchange_division="ALL"):
        return [
            RankingEntry("7203", 1, 100.0, ranking_type, 100.0),
            RankingEntry("9999", 2, 90.0, ranking_type, 500.0),
        ]


class RegulationStub:
    def get_regulation(self, symbol, market_code):
        return Regulation(symbol, False)


class ExchangeStub:
    def get_primary_exchange(self, symbol):
        return 1


def run_screening(tmp_path, observer):
    repository = ScreeningResultRepository(tmp_path)
    result = ScreeningUseCase(
        RankingStub(), RegulationStub(), ExchangeStub(), repository, selection_atr_observer=observer
    ).execute(price_cap=270.0)
    return result, repository.load_latest()


def test_screening_audit_records_atr_ratio_without_changing_selection(tmp_path):
    today = date.today()
    observer = SelectionAtrObserver(
        lambda symbol, until: make_bars([10, 10, 40], as_of=until), period=3
    )

    result, loaded = run_screening(tmp_path, observer)

    assert result.symbols == ["7203"]
    entries = {entry.symbol: entry for entry in loaded.audit_entries}
    assert entries["7203"].selection_atr_level == "DANGER"
    assert entries["7203"].selection_atr_as_of == today.isoformat()
    assert entries["7203"].selection_atr_ratio == pytest.approx(40 / 20)
    assert entries["9999"].selection_atr_unavailable_reason == REASON_NOT_OBSERVED_PRICE_FILTERED


def test_screening_continues_when_daily_fetch_fails(tmp_path):
    def failing(symbol, until):
        raise RuntimeError("boom")

    result, loaded = run_screening(tmp_path, SelectionAtrObserver(failing, period=3))

    assert result.symbols == ["7203"]
    entry = next(entry for entry in loaded.audit_entries if entry.symbol == "7203")
    assert entry.selection_atr_unavailable_reason == REASON_FETCH_FAILED


def test_screening_without_observer_leaves_fields_empty(tmp_path):
    _, loaded = run_screening(tmp_path, None)

    assert all(entry.selection_atr_level is None for entry in loaded.audit_entries)
    assert all(entry.selection_atr_unavailable_reason is None for entry in loaded.audit_entries)


def test_old_audit_files_without_atr_fields_still_load():
    result = ScreeningResult.from_dict({
        "date": "2026-10-05",
        "symbols": ["7203"],
        "generated_at": "2026-10-05T15:35:00",
        "audit_entries": [{
            "symbol": "7203", "turnover_rank": 1, "turnover_value": 1.0, "price_gain_rank": 1,
            "price_gain_value": 1.0, "total_rank": 2, "primary_exchange": 1, "is_restricted": False,
            "restriction_reason": "", "selected": True, "price": 100.0, "check_status": "checked",
        }],
    })

    entry = result.audit_entries[0]
    assert entry.selection_atr_ratio is None and entry.selection_atr_level is None
    assert json.loads(json.dumps(result.to_dict()))["audit_entries"][0]["selection_atr_ratio"] is None


def test_observer_caches_daily_history_per_symbol_and_date():
    calls = []

    def fetch(symbol, until):
        calls.append((symbol, until))
        return make_bars([10, 10, 20], as_of=until)

    observer = SelectionAtrObserver(fetch, period=3)
    observer.observe("A", AS_OF)
    observer.observe("A", AS_OF)

    assert calls == [("A", AS_OF)]
