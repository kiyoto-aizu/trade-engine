import json
from datetime import date, timedelta
from types import SimpleNamespace

import pytest

from src.application.midday_filtering_usecase import MiddayBandSpec, MiddayFilteringUseCase
from src.domain.models import MinuteBar
from src.infrastructure.market_data.get_intraday_bars import IntradayFetchResult
from src.infrastructure.persistence.filtering_diagnostics_repository import FilteringDiagnosticsRepository
from src.infrastructure.persistence.filtering_result_repository import FilteringResultRepository
from src.infrastructure.persistence.parquet_minute_bar_repository import ParquetMinuteBarRepository

DAY = date(2026, 10, 8)
TODAY = DAY.isoformat()


class FakeScreening:
    def __init__(self, symbols):
        self.symbols = symbols

    def load_for_date(self, target_date):
        assert target_date == date(2026, 10, 7)
        return SimpleNamespace(symbols=self.symbols)


class FakeVolume:
    def __init__(self, averages):
        self.averages = averages

    def get_average_turnover_before(self, symbol, target_date, days):
        assert target_date == DAY and days == 20
        return self.averages.get(symbol)


def bars(price=100.0, volume=1000.0, minutes=("09:00", "09:10", "09:29", "09:30")):
    return [MinuteBar(f"{TODAY}T{m}", price, None, volume, "yahoo") for m in minutes]


class Clock:
    def __init__(self):
        self.now = 0.0
        self.slept = []

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.slept.append(seconds)
        self.now += seconds


def make_bands(tmp_path, screening):
    bands = []
    for name, cap, only in (("450", 450.0, False), ("900", 900.0, False), ("270", 270.0, True)):
        root = tmp_path / name
        bands.append(MiddayBandSpec(
            name=name, price_cap=cap,
            screening_repository=FakeScreening(screening.get(name, [])),
            result_repository=None if only else FilteringResultRepository(root / "result"),
            diagnostics_repository=FilteringDiagnosticsRepository(root / "diag"),
            comparison_only=only,
        ))
    return bands


def make_use_case(fetch, averages, tmp_path, **kwargs):
    clock = Clock()
    notifications = []
    defaults = dict(
        volume_client=FakeVolume(averages),
        minute_fetcher=fetch,
        notifier=notifications.append,
        fetch_retries=1,
        retry_wait_seconds=2.0,
        skip_dates=(),
        monotonic_clock=clock.monotonic,
        sleep_func=clock.sleep,
    )
    defaults.update(kwargs)
    return MiddayFilteringUseCase(**defaults), clock, notifications


def load_diag(tmp_path, name):
    files = sorted((tmp_path / name / "diag").glob(f"{TODAY}_*.json"))
    assert files
    return json.loads(files[-1].read_text(encoding="utf-8"))


def test_estimates_ratio_without_correction_and_uses_separate_source(tmp_path):
    data = {"A": bars(100.0, 1000.0), "B": bars(200.0, 1000.0)}
    use_case, _, notifications = make_use_case(
        lambda s: IntradayFetchResult(data[s], True), {"A": 1_000_000.0, "B": 1_000_000.0}, tmp_path
    )

    report = use_case.run(make_bands(tmp_path, {"450": ["A", "B"]}), DAY, 1000.0)

    diag = load_diag(tmp_path, "450")
    records = {r["symbol"]: r for r in diag["candidates"]}
    # 09:30の足は含めない: 3本 × 価格 × 1000
    assert records["A"]["numerator"] == 300_000.0
    assert records["A"]["ratio"] == pytest.approx(0.3)
    assert records["B"]["ratio"] == pytest.approx(0.6)
    assert records["A"]["numerator_source"] == "yahoo_minute_estimate"
    assert records["A"]["window_bar_count"] == 3
    assert records["A"]["window_first_bar_time"].endswith("09:00")
    assert records["A"]["window_last_bar_time"].endswith("09:29")
    assert records["B"]["rank"] == 1 and records["B"]["selected"] is True
    assert diag["summary"]["source"] == "yahoo_minute_estimate"
    assert diag["result_saved"] is True
    saved = json.loads(next((tmp_path / "450" / "result").glob("*.json")).read_text(encoding="utf-8"))
    assert saved["symbols"] == ["B", "A"]
    assert report.band_summaries["450"]["evaluated_count"] == 2
    assert len(notifications) == 1
    assert "推定値(Yahoo分足" in notifications[0]


def test_reason_codes_are_separated(tmp_path):
    def fetch(symbol):
        if symbol == "FAIL":
            return IntradayFetchResult([], False, "NO_RESPONSE")
        if symbol == "NOBARS":
            return IntradayFetchResult([], True)
        if symbol == "ZERO":
            return IntradayFetchResult(bars(volume=0.0), True)
        if symbol == "NOAVG":
            return IntradayFetchResult(bars(), True)
        return IntradayFetchResult(bars(), True)

    use_case, _, _ = make_use_case(fetch, {"OK": 1_000_000.0, "ZERO": 1.0}, tmp_path)
    use_case.run(make_bands(tmp_path, {"450": ["FAIL", "NOBARS", "ZERO", "NOAVG", "OK"]}), DAY, 1000.0)

    diag = load_diag(tmp_path, "450")
    codes = {r["symbol"]: r["reason_code"] for r in diag["candidates"]}
    assert codes == {
        "FAIL": "MIDDAY_FETCH_FAILED",
        "NOBARS": "MIDDAY_NO_BARS",
        "ZERO": "MIDDAY_ZERO_VOLUME",
        "NOAVG": "FILTER_AVERAGE_MISSING",
        "OK": None,
    }
    assert diag["summary"]["evaluated_count"] == 1
    assert diag["summary"]["reason_counts"]["MIDDAY_FETCH_FAILED"] == 1


def test_few_bars_are_not_excluded_by_count(tmp_path):
    use_case, _, _ = make_use_case(
        lambda s: IntradayFetchResult(bars(minutes=("09:12",)), True), {"A": 1_000_000.0}, tmp_path
    )
    use_case.run(make_bands(tmp_path, {"450": ["A"]}), DAY, 1000.0)

    record = load_diag(tmp_path, "450")["candidates"][0]
    assert record["status"] == "evaluated"
    assert record["window_bar_count"] == 1


def test_fetch_retries_then_succeeds_and_records_attempts(tmp_path):
    calls = []

    def fetch(symbol):
        calls.append(symbol)
        return IntradayFetchResult([], False, "NO_RESPONSE") if len(calls) == 1 else IntradayFetchResult(bars(), True)

    use_case, clock, _ = make_use_case(fetch, {"A": 1_000_000.0}, tmp_path)
    use_case.run(make_bands(tmp_path, {"450": ["A"]}), DAY, 1000.0)

    record = load_diag(tmp_path, "450")["candidates"][0]
    assert record["status"] == "evaluated"
    assert record["minute_fetch_attempts"] == 2
    assert clock.slept == [2.0]


def test_deadline_marks_remaining_as_time_limit_and_skips_result_file(tmp_path):
    clock_holder = {}

    def fetch(symbol):
        clock_holder["clock"].now += 10.0
        return IntradayFetchResult(bars(), True)

    use_case, clock, _ = make_use_case(fetch, {s: 1_000_000.0 for s in "ABC"}, tmp_path)
    clock_holder["clock"] = clock
    use_case.run(make_bands(tmp_path, {"450": ["A", "B", "C"]}), DAY, 15.0)

    diag = load_diag(tmp_path, "450")
    statuses = {r["symbol"]: (r["status"], r["reason_code"]) for r in diag["candidates"]}
    assert statuses["A"][0] == "evaluated" and statuses["B"][0] == "evaluated"
    assert statuses["C"] == ("not_evaluated", "FILTER_TIME_LIMIT")
    assert diag["summary"]["timed_out"] is True
    assert diag["result_saved"] is False
    assert not list((tmp_path / "450" / "result").glob("*.json"))


def test_270_is_comparison_only_and_compared_with_board(tmp_path):
    board_dir = tmp_path / "board"
    board_repo = FilteringDiagnosticsRepository(board_dir)
    board_repo.save({"date": TODAY, "candidates": [
        {"symbol": "A", "status": "evaluated", "ratio": 1.0},
        {"symbol": "B", "status": "evaluated", "ratio": 2.0},
    ]})
    data = {"A": bars(100.0, 1000.0), "B": bars(200.0, 1000.0)}
    comparison_repo = FilteringDiagnosticsRepository(tmp_path / "comparison")
    use_case, _, notifications = make_use_case(
        lambda s: IntradayFetchResult(data[s], True), {"A": 300_000.0, "B": 750_000.0}, tmp_path,
        board_diagnostics_repository=board_repo, comparison_repository=comparison_repo,
    )

    report = use_case.run(make_bands(tmp_path, {"270": ["A", "B"]}), DAY, 1000.0)

    assert not (tmp_path / "270" / "result").exists()
    assert report.comparison["available"] is True
    assert report.comparison["stats"]["median"] == pytest.approx(0.7)
    assert report.comparison["top_overlap_count"] == 2
    saved = json.loads(next((tmp_path / "comparison").glob("*.json")).read_text(encoding="utf-8"))
    assert saved["comparison"]["top_overlap_count"] == 2
    assert "270" in notifications[0]


def test_comparison_band_is_cut_first_by_deadline(tmp_path):
    clock_holder = {}

    def fetch(symbol):
        clock_holder["clock"].now += 10.0
        return IntradayFetchResult(bars(), True)

    use_case, clock, _ = make_use_case(fetch, {s: 1.0e6 for s in ("A", "B")}, tmp_path)
    clock_holder["clock"] = clock
    use_case.run(make_bands(tmp_path, {"270": ["B"], "450": ["A"]}), DAY, 5.0)

    # 期限内に着手できるのは先頭の1銘柄だけ。売買判断に使う帯が優先される
    assert load_diag(tmp_path, "450")["candidates"][0]["status"] == "evaluated"
    assert load_diag(tmp_path, "270")["candidates"][0]["reason_code"] == "FILTER_TIME_LIMIT"


def test_parquet_saves_only_window_after_notification_and_keeps_outside(tmp_path):
    order = []
    repo = ParquetMinuteBarRepository(tmp_path / "parquet")
    outside = MinuteBar(f"{TODAY}T10:00", 50.0, 10.0, 5.0, "poll")
    stale_inside = MinuteBar(f"{TODAY}T09:05", 1.0, 1.0, 1.0, "poll")
    repo.replace_bars(DAY, "A", [stale_inside, outside])
    use_case, _, _ = make_use_case(
        lambda s: IntradayFetchResult(bars(), True), {"A": 1.0e6}, tmp_path,
        bar_repository=repo, notifier=lambda message: order.append("notify"),
    )
    original = repo.replace_bars
    repo.replace_bars = lambda *a, **k: (order.append("save"), original(*a, **k))[1]

    use_case.run(make_bands(tmp_path, {"450": ["A"]}), DAY, 1000.0)

    assert order == ["notify", "save"]
    saved = repo.load_bars(DAY, "A")
    times = [bar.time[11:16] for bar in saved]
    assert "10:00" in times and "09:05" not in times
    assert "09:30" not in times
    assert {"09:00", "09:10", "09:29"} <= set(times)


def test_parquet_failure_does_not_break_result(tmp_path):
    class BrokenRepo:
        def load_bars(self, *args):
            raise OSError("disk")

        def replace_bars(self, *args):
            raise OSError("disk")

    use_case, _, notifications = make_use_case(
        lambda s: IntradayFetchResult(bars(), True), {"A": 1.0e6}, tmp_path, bar_repository=BrokenRepo()
    )
    report = use_case.run(make_bands(tmp_path, {"450": ["A"]}), DAY, 1000.0)

    assert report.band_summaries["450"]["evaluated_count"] == 1
    assert len(notifications) == 1


def test_holiday_and_skip_date_do_not_fetch(tmp_path):
    calls = []
    use_case, _, _ = make_use_case(lambda s: calls.append(s), {}, tmp_path, skip_dates=(TODAY,))

    skipped = use_case.run(make_bands(tmp_path, {"450": ["A"]}), DAY, 1000.0)
    holiday = use_case.run(make_bands(tmp_path, {"450": ["A"]}), date(2026, 10, 10), 1000.0)

    assert skipped.skipped_reason and holiday.skipped_reason
    assert calls == []


def _atr_observer(latest_ranges):
    from src.application.selection_atr_observer import SelectionAtrObserver
    from src.domain.atr_ratio_analysis import DatedDailyBar
    from src.domain.volatility import DailyBar

    def fetch(symbol, until):
        if symbol not in latest_ranges:
            raise RuntimeError("fetch failed")
        ranges = [10.0, 10.0, latest_ranges[symbol]]
        return [
            DatedDailyBar(until - timedelta(days=2 - i), DailyBar(100 + w / 2, 100 - w / 2, 100.0))
            for i, w in enumerate(ranges)
        ]

    return SelectionAtrObserver(fetch, period=3)


def test_diagnostics_record_selection_atr_and_atr_below_comparison_without_changing_adoption(tmp_path):
    data = {s: bars(price, 1000.0) for s, price in (("A", 300.0), ("B", 200.0), ("C", 100.0))}
    averages = {s: 1_000_000.0 for s in data}
    use_case, _, _ = make_use_case(
        lambda s: IntradayFetchResult(data[s], True), averages, tmp_path,
        # A=DANGER(ATR比2.0)、B=NORMAL、Cは日足取得失敗
        selection_atr_observer=_atr_observer({"A": 40.0, "B": 10.0}),
    )

    use_case.run(make_bands(tmp_path, {"450": ["A", "B", "C"]}), DAY, 1000.0)

    diag = load_diag(tmp_path, "450")
    records = {r["symbol"]: r for r in diag["candidates"]}
    assert records["A"]["selection_atr_level"] == "DANGER"
    assert records["A"]["selection_atr_as_of"] == "2026-10-07"
    assert records["B"]["selection_atr_level"] == "NORMAL"
    assert records["C"]["selection_atr_ratio"] is None
    assert records["C"]["selection_atr_unavailable_reason"] == "DAILY_FETCH_FAILED"
    assert diag["summary"]["adopted_symbols"] == ["A", "B", "C"]
    comparison = diag["summary"]["selection_atr_below_comparison"]
    assert comparison["symbols"] == ["B"]
    assert comparison["replaced_out"] == ["A", "C"]
    assert comparison["unknown_atr_count"] == 1
    assert records["B"]["comparison_selected_atr_below_danger"] is True
    assert records["A"]["comparison_selected_atr_below_danger"] is False
    assert records["A"]["selected"] is True


def test_diagnostics_without_observer_have_no_atr_fields(tmp_path):
    use_case, _, _ = make_use_case(
        lambda s: IntradayFetchResult(bars(), True), {"A": 1_000_000.0}, tmp_path
    )

    use_case.run(make_bands(tmp_path, {"450": ["A"]}), DAY, 1000.0)

    diag = load_diag(tmp_path, "450")
    assert "selection_atr_ratio" not in diag["candidates"][0]
    assert "selection_atr_below_comparison" not in diag["summary"]


def test_selection_atr_observation_stops_after_deadline(tmp_path):
    use_case, clock, _ = make_use_case(
        lambda s: IntradayFetchResult(bars(), True), {"A": 1_000_000.0}, tmp_path,
        selection_atr_observer=_atr_observer({"A": 10.0}),
    )
    clock.now = 0.0

    use_case.run(make_bands(tmp_path, {"450": ["A"]}), DAY, 0.0)

    record = load_diag(tmp_path, "450")["candidates"][0]
    assert record["status"] == "not_evaluated"
    assert record["selection_atr_unavailable_reason"] == "OBSERVATION_TIME_LIMIT"
