from datetime import date, time
from types import SimpleNamespace

import pytest

from src.application.jquants_daily_job import JQuantsDailyJob
from src.domain.jquants_daily_plan import DailyDecision, plan_daily
from src.domain.jquants_daily_stop_policy import StopAction, decide_stop
from src.infrastructure.market_data.jquants_metrics_failure_history import (
    JQuantsMetricsFailureHistory,
)
from src.infrastructure.market_data.jquants_raw_store import JQuantsDatasetAlreadyRunningError

DAY = date(2026, 10, 30)


class Calendar:
    def __init__(self, categories):
        self._categories = categories

    def categories(self, start, end):
        return self._categories


class Ingestion:
    def __init__(self, results):
        self.results = results
        self.calls = []

    def run(self, *, dataset, date):
        self.calls.append((dataset, date))
        status, category = self.results.get(dataset, ("COMPLETE", None))
        code = {"COMPLETE": 0, "ATTENTION": 2, "FAILED": 1}[status]
        if category == "RATE_LIMIT_ABORTED":
            code = 3
        return SimpleNamespace(
            status=status, error_category=category, exit_code=code, metrics_file=None
        )


class History:
    def __init__(self, counts=None):
        self.counts = counts or {}

    def consecutive_failed(self, dataset):
        return self.counts.get(dataset, 0)


def job(results=None, categories=None, counts=None, verify=None):
    ingestion = Ingestion(results or {})
    cats = {DAY: 1} if categories is None else categories
    return JQuantsDailyJob(Calendar(cats), ingestion, History(counts), verify), ingestion


def run(j, now=time(18, 0), publish_after=None):
    return j.run(DAY, now, publish_after)


@pytest.mark.parametrize(
    ("category", "decision"),
    [(1, DailyDecision.TARGET), (2, DailyDecision.TARGET), (0, DailyDecision.HOLIDAY),
     (3, DailyDecision.HOLIDAY), (None, DailyDecision.CALENDAR_MISSING)],
)
def test_plan_distinguishes_business_day_holiday_and_missing(category, decision):
    categories = {} if category is None else {DAY: category}
    assert plan_daily(DAY, time(18), categories).decision is decision


def test_not_published_is_distinct_from_holiday():
    before = plan_daily(DAY, time(9), {DAY: 1}, time(17))
    after = plan_daily(DAY, time(17), {DAY: 1}, time(17))
    assert before.decision is DailyDecision.NOT_PUBLISHED and before.target_date is None
    assert after.decision is DailyDecision.TARGET and after.target_date == DAY
    assert plan_daily(DAY, time(9), {DAY: 0}, time(17)).decision is DailyDecision.HOLIDAY


@pytest.mark.parametrize("categories", [{DAY: 0}, {DAY: 3}, {}])
def test_non_target_days_do_not_ingest(categories):
    j, ingestion = job(categories=categories)
    result = run(j)
    assert ingestion.calls == []
    assert result.exit_code == (1 if not categories else 0)


def test_not_published_does_not_ingest():
    j, ingestion = job()
    result = run(j, now=time(9), publish_after=time(17))
    assert ingestion.calls == [] and result.decision is DailyDecision.NOT_PUBLISHED
    assert result.exit_code == 0


def test_business_day_runs_daily_then_minute():
    j, ingestion = job()
    result = run(j)
    assert ingestion.calls == [("daily", "2026-10-30"), ("minute", "2026-10-30")]
    assert result.exit_code == 0


def test_verification_is_recorded_per_dataset():
    j, _ = job(verify=lambda summary: ("Go", ()))
    assert [o.verdict for o in run(j).outcomes] == ["Go", "Go"]


@pytest.mark.parametrize("category", ["AUTH_ERROR", "PERMISSION_OR_CONTRACT_ERROR"])
def test_401_403_halt_everything_and_hold_flag(category):
    j, ingestion = job({"daily": ("FAILED", category)})
    result = run(j)
    assert [c[0] for c in ingestion.calls] == ["daily"]
    assert result.halt_flag_required and result.exit_code == 1
    assert result.outcomes[1].ran is False


@pytest.mark.parametrize("category", ["CLIENT_REQUEST_ERROR", "QUALITY_ERROR"])
def test_400_and_quality_failure_stop_only_that_job(category):
    j, ingestion = job({"daily": ("FAILED", category)})
    result = run(j)
    assert [c[0] for c in ingestion.calls] == ["daily", "minute"]
    assert result.outcomes[0].stop_action is StopAction.STOP_JOB
    assert not result.halt_flag_required and result.exit_code == 1


def test_single_failure_is_recovery_target_and_continues():
    j, ingestion = job({"daily": ("FAILED", "SERVER_ERROR")}, counts={"daily": 1})
    result = run(j)
    assert len(ingestion.calls) == 2
    assert result.outcomes[0].stop_action is StopAction.CONTINUE


def test_third_consecutive_failure_stops_the_rest_of_the_day():
    j, ingestion = job({"daily": ("FAILED", "SERVER_ERROR")}, counts={"daily": 2})
    result = run(j)
    assert [c[0] for c in ingestion.calls] == ["daily"]
    assert result.outcomes[0].stop_action is StopAction.STOP_TODAY
    assert result.outcomes[0].reason == "CONSECUTIVE_FAILED_3"
    assert not result.halt_flag_required


def test_429_abort_is_failed_and_exit_code_3_without_halting():
    j, ingestion = job({"daily": ("FAILED", "RATE_LIMIT_ABORTED")})
    result = run(j)
    assert len(ingestion.calls) == 2 and result.exit_code == 3


def test_attention_continues_and_exits_2():
    j, ingestion = job({"daily": ("ATTENTION", None)})
    result = run(j)
    assert len(ingestion.calls) == 2 and result.exit_code == 2


def test_lock_failure_is_skipped_and_recorded_without_counting_as_failure():
    j, ingestion = job({"daily": ("FAILED", "DATASET_ALREADY_RUNNING")})
    result = run(j)
    assert len(ingestion.calls) == 2
    first = result.outcomes[0]
    assert first.stop_action is StopAction.SKIP_LOCKED and first.exit_code is None
    assert result.exit_code == 2


def test_lock_error_has_its_own_category():
    assert JQuantsDatasetAlreadyRunningError.category == "DATASET_ALREADY_RUNNING"


def test_stop_policy_non_failed_never_stops():
    assert decide_stop("ATTENTION", "AUTH_ERROR", 5).action is StopAction.CONTINUE


def write_metrics(directory, dataset, day, name, status, category=None):
    folder = directory / dataset / f"date={day}"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f"{name}.json").write_text(
        '{"status": "%s", "error_category": %s}'
        % (status, "null" if category is None else f'"{category}"'),
        encoding="utf-8",
    )


def test_failure_history_counts_across_dates_and_ignores_lock(tmp_path):
    write_metrics(tmp_path, "daily", "2026-10-27", "20261027T0", "COMPLETE")
    write_metrics(tmp_path, "daily", "2026-10-28", "20261028T0", "FAILED", "SERVER_ERROR")
    write_metrics(tmp_path, "daily", "2026-10-29", "20261029T0", "FAILED", "SERVER_ERROR")
    write_metrics(tmp_path, "daily", "2026-10-29", "20261029T1", "FAILED", "DATASET_ALREADY_RUNNING")
    write_metrics(tmp_path, "minute", "2026-10-29", "20261029T0", "COMPLETE")
    history = JQuantsMetricsFailureHistory(tmp_path)
    assert history.consecutive_failed("daily") == 2
    assert history.consecutive_failed("minute") == 0
    write_metrics(tmp_path, "daily", "2026-10-30", "20261030T0", "ATTENTION")
    assert history.consecutive_failed("daily") == 0
