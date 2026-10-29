import ast
import json
from datetime import date, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.application import jquants_backfill_job as job_module
from src.application.jquants_backfill_job import JQuantsBackfillJob
from src.domain import jquants_backfill_policy as policy_module
from src.domain.jquants_backfill_plan import BackfillPlan
from src.domain.jquants_backfill_policy import (
    HOLD_FORBIDDEN,
    HOLD_OUT_OF_CONTRACT,
    MIN_FREE_BYTES,
    Action,
    is_budget_exceeded,
    is_disk_insufficient,
    judge_day,
)
from src.entrypoints import run_jquants_backfill as entrypoint
from src.infrastructure.market_data import jquants_backfill_hold_store as store_module
from src.infrastructure.market_data.jquants_backfill_hold_store import (
    BackfillHoldStoreError,
    JQuantsBackfillHoldStore,
)

TODAY = date(2026, 10, 29)
GOOD = MIN_FREE_BYTES * 2


def day(n):
    return date(2026, 10, n)


def summary(status="COMPLETE", category=None):
    return SimpleNamespace(status=status, error_category=category)


def plan_of(*targets):
    return BackfillPlan(tuple(targets), (), (), (), (), (), ())


class FakePlanner:
    def __init__(self, targets):
        self.targets = targets
        self.calls = []

    def plan(self, dataset, start, end, today):
        self.calls.append(today)
        return plan_of(*self.targets)


class FakeIngestion:
    def __init__(self, results=None):
        self.results = results or {}
        self.requested = []

    def run(self, *, dataset, date):
        self.requested.append(date)
        return self.results.get(date, summary())


class FakeHolds:
    def __init__(self, held=None):
        self._held = dict(held or {})
        self.recorded = []

    def held(self, dataset):
        return dict(self._held)

    def record(self, dataset, target_date, kind):
        self.recorded.append((target_date, kind))
        self._held[target_date] = kind


class FakeSpace:
    def __init__(self, *values):
        self.values = list(values) or [GOOD]

    def free_bytes(self):
        return self.values.pop(0) if len(self.values) > 1 else self.values[0]


class FakeClock:
    def __init__(self, todays=None, step=0.0):
        self.todays = list(todays or [TODAY])
        self.step = step
        self.now = 0.0

    def today(self):
        return self.todays.pop(0) if len(self.todays) > 1 else self.todays[0]

    def monotonic(self):
        self.now += self.step
        return self.now


def make_job(targets, results=None, holds=None, space=None, clock=None):
    ingestion = FakeIngestion(results)
    holds = holds or FakeHolds()
    job = JQuantsBackfillJob(
        FakePlanner(targets), ingestion, holds, space or FakeSpace(), clock or FakeClock()
    )
    return job, ingestion, holds


def run(job, **kwargs):
    return job.run("daily", day(1), day(28), **kwargs)


# --- policy ---

@pytest.mark.parametrize("status", ["COMPLETE", "ATTENTION"])
def test_success_continues_and_resets_count(status):
    verdict = judge_day("daily", day(1), TODAY, status, None, 2)
    assert (verdict.action, verdict.consecutive_failed) == (Action.CONTINUE, 0)


@pytest.mark.parametrize("category, action, reason, hold", [
    ("AUTH_ERROR", Action.STOP_BACKFILL, "HTTP_401", None),
    ("PERMISSION_OR_CONTRACT_ERROR", Action.STOP_BACKFILL, "HTTP_403", HOLD_FORBIDDEN),
    ("CLIENT_REQUEST_ERROR", Action.STOP_BACKFILL, "UNEXPECTED_400_IN_RANGE", None),
    ("RATE_LIMIT_ABORTED", Action.STOP_TODAY, "RATE_LIMITED", None),
    ("RATE_LIMITED", Action.STOP_TODAY, "RATE_LIMITED", None),
    ("QUALITY_ERROR", Action.STOP_BACKFILL, "QUALITY_VALIDATION_FAILED", None),
])
def test_failed_categories(category, action, reason, hold):
    verdict = judge_day("daily", day(1), TODAY, "FAILED", category, 0)
    assert (verdict.action, verdict.reason, verdict.hold_kind) == (action, reason, hold)


def test_out_of_range_400_ends_backfill_and_holds():
    verdict = judge_day("daily", date(2000, 1, 1), TODAY, "FAILED", "CLIENT_REQUEST_ERROR", 0)
    assert (verdict.action, verdict.hold_kind) == (Action.END_BACKFILL, HOLD_OUT_OF_CONTRACT)


def test_third_consecutive_failed_stops_today():
    first = judge_day("daily", day(1), TODAY, "FAILED", "SERVER_ERROR", 0)
    second = judge_day("daily", day(2), TODAY, "FAILED", "SERVER_ERROR", first.consecutive_failed)
    third = judge_day("daily", day(3), TODAY, "FAILED", "SERVER_ERROR", second.consecutive_failed)
    assert [first.action, second.action, third.action] == [
        Action.CONTINUE, Action.CONTINUE, Action.STOP_TODAY]
    assert third.reason == "CONSECUTIVE_FAILED"


def test_unknown_status_fails_closed():
    assert judge_day("daily", day(1), TODAY, "???", None, 0).action is Action.STOP_BACKFILL


def test_budget_and_disk_thresholds():
    assert not is_budget_exceeded(7199, 7200) and is_budget_exceeded(7200, 7200)
    assert is_disk_insufficient(MIN_FREE_BYTES - 1) and not is_disk_insufficient(MIN_FREE_BYTES)


# --- job ---

def test_runs_oldest_first_and_never_sends_out_of_range_dates():
    old = date(2000, 1, 1)
    job, ingestion, _ = make_job([day(5), old, day(3)])
    result = run(job)
    assert ingestion.requested == ["2026-10-03", "2026-10-05"]
    assert result.exit_code == 0 and result.stop_action is None


def test_held_dates_are_not_retried():
    job, ingestion, _ = make_job([day(3), day(4)], holds=FakeHolds({day(3): HOLD_FORBIDDEN}))
    run(job)
    assert ingestion.requested == ["2026-10-04"]


def test_403_records_hold_and_stops():
    job, ingestion, holds = make_job(
        [day(3), day(4)], {"2026-10-03": summary("FAILED", "PERMISSION_OR_CONTRACT_ERROR")}
    )
    result = run(job)
    assert ingestion.requested == ["2026-10-03"]
    assert holds.recorded == [(day(3), HOLD_FORBIDDEN)]
    assert (result.stop_reason, result.exit_code) == ("HTTP_403", 4)


def test_end_backfill_verdict_records_hold(monkeypatch):
    forced = policy_module.DayVerdict(Action.END_BACKFILL, "OUT_OF_CONTRACT_RANGE", HOLD_OUT_OF_CONTRACT)
    monkeypatch.setattr(job_module, "judge_day", lambda *args: forced)
    job, ingestion, holds = make_job([day(3), day(4)])
    result = run(job)
    assert ingestion.requested == ["2026-10-03"]
    assert holds.recorded == [(day(3), HOLD_OUT_OF_CONTRACT)]
    assert (result.stop_action, result.exit_code) == (Action.END_BACKFILL, 4)


def test_in_range_400_stops_without_retry():
    job, ingestion, holds = make_job(
        [day(3), day(4)], {"2026-10-03": summary("FAILED", "CLIENT_REQUEST_ERROR")}
    )
    result = run(job)
    assert ingestion.requested == ["2026-10-03"]
    assert holds.recorded == [] and result.stop_action is Action.STOP_BACKFILL


def test_401_stops_backfill():
    job, ingestion, _ = make_job(
        [day(3), day(4)], {"2026-10-03": summary("FAILED", "AUTH_ERROR")}
    )
    result = run(job)
    assert ingestion.requested == ["2026-10-03"] and result.stop_reason == "HTTP_401"


def test_429_stops_today_with_exit_3():
    job, ingestion, _ = make_job(
        [day(3), day(4)], {"2026-10-03": summary("FAILED", "RATE_LIMIT_ABORTED")}
    )
    result = run(job)
    assert ingestion.requested == ["2026-10-03"]
    assert (result.stop_action, result.exit_code) == (Action.STOP_TODAY, 3)


def test_three_consecutive_failed_stop_and_success_resets():
    failed = summary("FAILED", "SERVER_ERROR")
    results = {f"2026-10-{n:02d}": failed for n in (1, 2, 4, 5, 7, 8, 9)}
    job, ingestion, _ = make_job([day(n) for n in range(1, 12)], results)
    result = run(job)
    assert ingestion.requested[-1] == "2026-10-09"
    assert (result.stop_reason, result.exit_code) == ("CONSECUTIVE_FAILED", 1)


def test_quality_failure_stops_remaining_days():
    job, ingestion, _ = make_job(
        [day(3), day(4)], {"2026-10-03": summary("FAILED", "QUALITY_ERROR")}
    )
    result = run(job)
    assert ingestion.requested == ["2026-10-03"] and result.exit_code == 4


def test_attention_continues_and_exit_code_2():
    job, ingestion, _ = make_job([day(3), day(4)], {"2026-10-03": summary("ATTENTION")})
    result = run(job)
    assert ingestion.requested == ["2026-10-03", "2026-10-04"] and result.exit_code == 2


def test_low_disk_stops_before_request():
    job, ingestion, _ = make_job([day(3), day(4)], space=FakeSpace(GOOD, MIN_FREE_BYTES - 1))
    result = run(job)
    assert ingestion.requested == ["2026-10-03"]
    assert (result.stop_reason, result.exit_code) == ("DISK_SPACE_LOW", 4)


def test_budget_exceeded_stops_today():
    job, ingestion, _ = make_job([day(3), day(4), day(5)], clock=FakeClock(step=10))
    result = run(job, budget_seconds=25)
    assert ingestion.requested == ["2026-10-03", "2026-10-04"]
    assert (result.stop_reason, result.exit_code) == ("BUDGET_EXCEEDED", 0)


def test_max_days_limits_count_from_oldest():
    job, ingestion, _ = make_job([day(5), day(3), day(4)])
    run(job, max_days=2)
    assert ingestion.requested == ["2026-10-03", "2026-10-04"]


def test_date_rollover_rechecks_range_before_request():
    start = date(2021, 10, 29)  # TODAY時点では範囲内、翌日には範囲外
    next_day = TODAY + timedelta(days=1)
    clock = FakeClock([TODAY, next_day])
    job, ingestion, holds = make_job([start, start + timedelta(days=1)], clock=clock)
    result = run(job)
    assert ingestion.requested == ["2021-10-30"]
    assert result.skipped_out_of_contract == [start]
    assert holds.recorded == [] and result.exit_code == 0


def test_result_output_has_only_dates_and_categories():
    job, _, _ = make_job([day(3)], {"2026-10-03": summary("FAILED", "SERVER_ERROR")})
    body = run(job).as_dict()
    assert body["days"] == [
        {"date": "2026-10-03", "status": "FAILED", "error_category": "SERVER_ERROR"}]
    assert set(body) == {"dataset", "days", "skipped_out_of_contract", "holds", "stop_action",
                         "stop_reason", "exit_code"}


def test_invalid_max_days_rejected():
    job, _, _ = make_job([day(3)])
    with pytest.raises(ValueError):
        run(job, max_days=0)


# --- hold store ---

def test_hold_store_roundtrip(tmp_path):
    store = JQuantsBackfillHoldStore(tmp_path / "nested")
    assert store.held("daily") == {}
    store.record("daily", day(3), HOLD_FORBIDDEN)
    store.record("daily", day(4), HOLD_OUT_OF_CONTRACT)
    store.record("minute", day(3), HOLD_FORBIDDEN)
    again = JQuantsBackfillHoldStore(tmp_path / "nested")
    assert again.held("daily") == {day(3): HOLD_FORBIDDEN, day(4): HOLD_OUT_OF_CONTRACT}
    assert again.held("minute") == {day(3): HOLD_FORBIDDEN}


def test_hold_store_rejects_bad_kind_and_corrupt_file(tmp_path):
    store = JQuantsBackfillHoldStore(tmp_path)
    with pytest.raises(ValueError):
        store.record("daily", day(3), "OTHER")
    (tmp_path / store_module.FILE_NAME).write_text("{broken", encoding="utf-8")
    with pytest.raises(BackfillHoldStoreError):
        store.held("daily")
    (tmp_path / store_module.FILE_NAME).write_text(
        json.dumps({"format_version": 1, "holds": {"daily": {"2026-10-03": "X"}}}), encoding="utf-8")
    with pytest.raises(BackfillHoldStoreError):
        store.held("daily")


# --- entrypoint ---

def test_entrypoint_requires_allow_network(capsys):
    code = entrypoint.main(["--dataset", "daily", "--from", "2026-10-01", "--to", "2026-10-02"])
    assert code == 2
    assert json.loads(capsys.readouterr().out)["status"] == "SKIPPED"


def test_entrypoint_rejects_reversed_range(capsys):
    args = ["--dataset", "daily", "--from", "2026-10-02", "--to", "2026-10-01", "--allow-network"]
    assert entrypoint.main(args) == 1
    assert json.loads(capsys.readouterr().out)["error_category"] == "INVALID_RANGE"


def test_entrypoint_runs_job_and_returns_exit_code(monkeypatch, capsys):
    captured = {}

    class StubJob:
        def __init__(self, *args):
            pass

        def run(self, dataset, start, end, **kwargs):
            captured.update(dataset=dataset, **kwargs)
            return job_module.BackfillJobResult(dataset, stop_action=Action.STOP_BACKFILL,
                                                stop_reason="HTTP_401")

    monkeypatch.setattr(entrypoint, "JQuantsRawStore", lambda: object())
    monkeypatch.setattr(entrypoint, "JQuantsSingleDayIngestion", lambda *a, **k: object())
    monkeypatch.setattr(entrypoint, "JQuantsCanonicalStore", lambda *a, **k: object())
    monkeypatch.setattr(entrypoint, "JQuantsBackfillJob", StubJob)
    args = ["--dataset", "minute", "--from", "2026-10-01", "--to", "2026-10-02",
            "--max-days", "3", "--allow-network"]
    assert entrypoint.main(args) == 4
    assert captured["max_days"] == 3
    assert json.loads(capsys.readouterr().out)["stop_reason"] == "HTTP_401"


# --- import guard ---

@pytest.mark.parametrize("module, forbidden", [
    (policy_module, ("src.application", "src.infrastructure", "requests", "socket", "os", "pathlib")),
    (job_module, ("src.infrastructure", "requests", "socket", "urllib")),
])
def test_layers_do_not_import_outer_or_network_modules(module, forbidden):
    tree = ast.parse(Path(module.__file__).read_text(encoding="utf-8"))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.add(node.module or "")
    assert not [m for m in imported if any(m == f or m.startswith(f + ".") for f in forbidden)]
