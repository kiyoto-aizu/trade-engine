import json
from datetime import date, timedelta

import pytest

from src.application.jquants_backfill_usecase import (
    BackfillOutcome,
    JQuantsBackfillUseCase,
)
from src.application.jquants_single_day_ingestion import JQuantsIngestionSummary
from src.domain.jquants_backfill_plan import BackfillPlan
from src.domain.jquants_backfill_policy import (
    DayObservation,
    StopReason,
    StopScope,
    decide_after_day,
    is_repeated_today_stop,
    next_failed_streak,
)
from src.domain.jquants_contract_range import (
    ClientRequestErrorKind,
    classify_client_request_error,
)
from src.infrastructure.market_data.jquants_backfill_state import (
    BackfillLockedError,
    BackfillStateError,
    JQuantsBackfillStateStore,
)

TODAY = date(2026, 10, 29)
GB = 1000**3
START = date(2026, 10, 1)


def _plan(*days, attention=(), in_progress=()):
    return BackfillPlan(
        targets=tuple(days), holidays=(), completed=(), in_progress=tuple(in_progress),
        attention=tuple(attention), out_of_contract=(), holiday_with_ledger=(),
    )


class FakePlanner:
    def __init__(self, plan):
        self.plan_value = plan

    def plan(self, dataset, start, end, today):
        return self.plan_value


class FakeIngestion:
    def __init__(self, results=None, on_run=None):
        self.results = results or {}
        self.calls = []
        self.on_run = on_run

    def run(self, *, dataset, date):
        self.calls.append(date)
        if self.on_run:
            self.on_run(date)
        spec = self.results.get(date, {})
        summary = JQuantsIngestionSummary(dataset, date, status=spec.get("status", "COMPLETE"))
        summary.error_category = spec.get("category")
        summary.warnings = list(spec.get("warnings", []))
        summary.durations_seconds = {"total": spec.get("elapsed", 1.0)}
        summary.execution_metrics = {
            "rate_limit_429_count": spec.get("r429", 0), "retry_count": spec.get("retry", 0),
        }
        return summary


def _usecase(tmp_path, planner, ingestion, *, free=500 * GB, today=lambda: TODAY, **kwargs):
    state = JQuantsBackfillStateStore(tmp_path / "state")
    usecase = JQuantsBackfillUseCase(
        planner, ingestion, state, raw_directory=tmp_path / "raw",
        canonical_directory=tmp_path / "canonical", today_provider=today,
        volume_probe=lambda path: (free, 1), **kwargs,
    )
    return usecase, state


def days(n):
    return [START + timedelta(days=i) for i in range(n)]


def test_runs_oldest_first_and_completes(tmp_path):
    ingestion = FakeIngestion()
    usecase, _ = _usecase(tmp_path, FakePlanner(_plan(*reversed(days(3)))), ingestion)
    result = usecase.run("daily", START, TODAY)
    assert ingestion.calls == [d.isoformat() for d in days(3)]
    assert result.outcome is BackfillOutcome.COMPLETED and result.exit_code == 0


def test_completed_with_review_when_attention_remains(tmp_path):
    usecase, _ = _usecase(
        tmp_path, FakePlanner(_plan(START, attention=[START + timedelta(days=9)])),
        FakeIngestion(),
    )
    assert usecase.run("daily", START, TODAY).outcome is BackfillOutcome.COMPLETED_WITH_REVIEW


def test_date_rollover_skips_without_request(tmp_path):
    # Planned in range, but by send time the rolling boundary has passed this date.
    boundary_day = date(2021, 10, 29)
    clock = iter([TODAY, TODAY + timedelta(days=1)])
    ingestion = FakeIngestion()
    usecase, _ = _usecase(
        tmp_path, FakePlanner(_plan(boundary_day, date(2021, 10, 30))), ingestion,
        today=lambda: next(clock, TODAY + timedelta(days=1)),
    )
    result = usecase.run("daily", boundary_day, TODAY)
    assert result.skipped_out_of_range == [boundary_day]
    assert ingestion.calls == ["2021-10-30"]
    assert result.outcome is BackfillOutcome.COMPLETED


def test_disk_floor_blocks_with_flag(tmp_path):
    ingestion = FakeIngestion()
    usecase, state = _usecase(tmp_path, FakePlanner(_plan(START)), ingestion, free=10 * GB)
    result = usecase.run("minute", START, TODAY)
    assert ingestion.calls == []
    assert result.reason is StopReason.DISK_FLOOR
    assert state.stop_flag()["scope"] == "BACKFILL_FLAG"


def test_disk_unmeasurable_or_tiny_stops_job(tmp_path):
    usecase, state = _usecase(tmp_path, FakePlanner(_plan(START)), FakeIngestion(), free=1000)
    result = usecase.run("daily", START, TODAY)
    assert result.reason is StopReason.DISK_SPACE_CRITICAL
    assert state.stop_flag()["scope"] == "ALL_FLAG"


def test_budget_exhausted_ends_normally(tmp_path):
    usecase, _ = _usecase(
        tmp_path, FakePlanner(_plan(*days(3))), FakeIngestion({START.isoformat(): {"elapsed": 5}}),
        budget_seconds=4,
    )
    result = usecase.run("daily", START, TODAY)
    assert result.outcome is BackfillOutcome.BUDGET_EXHAUSTED
    assert result.remaining_targets == 2


def test_retry_hold_shrinks_budget(tmp_path):
    ingestion = FakeIngestion({START.isoformat(): {"retry": 3, "elapsed": 10}})
    usecase, _ = _usecase(
        tmp_path, FakePlanner(_plan(*days(3))), ingestion, budget_seconds=15,
    )
    result = usecase.run("daily", START, TODAY)
    # Held: budget 7.5s, already spent 10s.
    assert result.outcome is BackfillOutcome.BUDGET_EXHAUSTED
    assert len(ingestion.calls) == 1


def test_max_days(tmp_path):
    ingestion = FakeIngestion()
    usecase, _ = _usecase(tmp_path, FakePlanner(_plan(*days(5))), ingestion)
    result = usecase.run("daily", START, TODAY, max_days=2)
    assert len(ingestion.calls) == 2 and result.outcome is BackfillOutcome.BUDGET_EXHAUSTED


@pytest.mark.parametrize("category,scope,flag", [
    ("AUTH_ERROR", "ALL_FLAG", True),
    ("PERMISSION_OR_CONTRACT_ERROR", "BACKFILL_FLAG", True),
])
def test_flag_stops(tmp_path, category, scope, flag):
    ingestion = FakeIngestion({START.isoformat(): {"status": "FAILED", "category": category}})
    usecase, state = _usecase(tmp_path, FakePlanner(_plan(*days(3))), ingestion)
    result = usecase.run("daily", START, TODAY)
    assert len(ingestion.calls) == 1 and result.outcome is BackfillOutcome.STOPPED
    assert state.stop_flag()["scope"] == scope
    # Next run is blocked until a human clears the flag.
    assert usecase.run("daily", START, TODAY).outcome is BackfillOutcome.BLOCKED_BY_FLAG
    state.clear_stop_flag()


def test_403_date_excluded_from_next_run(tmp_path):
    ingestion = FakeIngestion({
        START.isoformat(): {"status": "FAILED", "category": "PERMISSION_OR_CONTRACT_ERROR"},
    })
    usecase, state = _usecase(tmp_path, FakePlanner(_plan(*days(2))), ingestion)
    usecase.run("daily", START, TODAY)
    state.clear_stop_flag()
    ingestion.calls.clear()
    result = usecase.run("daily", START, TODAY)
    assert ingestion.calls == [days(2)[1].isoformat()]
    assert result.review_excluded == [START]
    assert result.outcome is BackfillOutcome.COMPLETED_WITH_REVIEW


def test_429_stops_today_only(tmp_path):
    ingestion = FakeIngestion({START.isoformat(): {"status": "FAILED", "category": "RATE_LIMIT_ABORTED"}})
    usecase, state = _usecase(tmp_path, FakePlanner(_plan(*days(3))), ingestion)
    result = usecase.run("minute", START, TODAY)
    assert result.reason is StopReason.RATE_LIMITED and state.stop_flag() is None
    assert usecase.run("minute", START, TODAY).outcome is BackfillOutcome.STOPPED_TODAY
    tomorrow = JQuantsBackfillUseCase(
        FakePlanner(_plan(START)), FakeIngestion(), state, raw_directory=tmp_path,
        canonical_directory=tmp_path, today_provider=lambda: TODAY + timedelta(days=1),
        volume_probe=lambda p: (500 * GB, 1),
    )
    assert tomorrow.run("minute", START, TODAY).outcome is BackfillOutcome.COMPLETED


def test_three_consecutive_failed_stops_today_across_runs(tmp_path):
    results = {d.isoformat(): {"status": "FAILED", "category": "STORAGE_OR_FORMAT_ERROR"} for d in days(5)}
    ingestion = FakeIngestion(results)
    usecase, state = _usecase(tmp_path, FakePlanner(_plan(*days(5))), ingestion)
    result = usecase.run("daily", START, TODAY)
    assert len(ingestion.calls) == 3
    assert result.reason is StopReason.CONSECUTIVE_FAILED
    assert state.failed_streak("daily") == 3


def test_success_resets_failed_streak(tmp_path):
    ingestion = FakeIngestion({days(2)[0].isoformat(): {"status": "FAILED", "category": "X"}})
    usecase, state = _usecase(tmp_path, FakePlanner(_plan(*days(2))), ingestion)
    usecase.run("daily", START, TODAY)
    assert state.failed_streak("daily") == 0


def test_repeated_today_stop_sets_flag(tmp_path):
    state = JQuantsBackfillStateStore(tmp_path / "state")
    state.record_today_stop(TODAY - timedelta(days=2), "RATE_LIMITED")
    state.record_today_stop(TODAY - timedelta(days=1), "RATE_LIMITED")
    ingestion = FakeIngestion({START.isoformat(): {"status": "FAILED", "category": "RATE_LIMIT_ABORTED"}})
    usecase, state = _usecase(tmp_path, FakePlanner(_plan(START)), ingestion)
    usecase.run("daily", START, TODAY)
    assert state.stop_flag()["reason"] == "REPEATED_TODAY_STOP"


def test_out_of_range_400_ends_job_and_excludes_date(tmp_path):
    ingestion = FakeIngestion({START.isoformat(): {"status": "FAILED", "category": "CLIENT_REQUEST_ERROR"}})
    usecase, state = _usecase(tmp_path, FakePlanner(_plan(START)), ingestion)
    # START is in range for daily, so this is the unexpected-in-range case.
    result = usecase.run("daily", START, TODAY)
    assert result.reason is StopReason.UNEXPECTED_400_IN_RANGE and state.stop_flag() is None


def test_lock_held_skips_without_waiting(tmp_path):
    ingestion = FakeIngestion()
    usecase, state = _usecase(tmp_path, FakePlanner(_plan(START)), ingestion)
    with state.global_lock():
        result = usecase.run("daily", START, TODAY)
    assert result.outcome is BackfillOutcome.LOCK_SKIPPED and ingestion.calls == []
    assert not (tmp_path / "state" / "backfill.lock").exists()


def test_lock_released_after_error(tmp_path):
    def boom(_):
        raise ValueError("x")
    usecase, _ = _usecase(tmp_path, FakePlanner(_plan(START)), FakeIngestion(on_run=boom))
    with pytest.raises(ValueError):
        usecase.run("daily", START, TODAY)
    assert not (tmp_path / "state" / "backfill.lock").exists()


def test_flag_set_while_waiting_is_seen_after_lock(tmp_path):
    usecase, state = _usecase(tmp_path, FakePlanner(_plan(START)), FakeIngestion())
    original = state.global_lock

    def lock_then_flag():
        manager = original()
        state.set_stop_flag("ALL_FLAG", "AUTH_ERROR", TODAY)
        return manager

    state.global_lock = lock_then_flag
    assert usecase.run("daily", START, TODAY).outcome is BackfillOutcome.BLOCKED_BY_FLAG


def test_lock_is_exclusive(tmp_path):
    state = JQuantsBackfillStateStore(tmp_path)
    with state.global_lock():
        with pytest.raises(BackfillLockedError):
            with state.global_lock():
                pass


def test_corrupt_state_is_not_reset(tmp_path):
    (tmp_path / "backfill_state.json").write_text("{broken", encoding="utf-8")
    with pytest.raises(BackfillStateError):
        JQuantsBackfillStateStore(tmp_path).stop_flag()


def test_output_has_dates_and_codes_only(tmp_path):
    usecase, state = _usecase(tmp_path, FakePlanner(_plan(*days(2))), FakeIngestion())
    usecase.run("daily", START, TODAY)
    text = json.dumps(usecase.run("daily", START, TODAY).as_dict())
    text += (tmp_path / "state" / "backfill_state.json").read_text(encoding="utf-8")
    assert str(tmp_path) not in text


def obs(**over):
    base = dict(
        dataset="minute", target_date=date(2026, 10, 1), today=TODAY, status="COMPLETE",
        error_category=None, warnings=(), rate_limit_429_count=0, retry_count=0,
        elapsed_seconds=100.0, failed_streak=0,
    )
    base.update(over)
    return DayObservation(**base)


@pytest.mark.parametrize("over,scope,reason", [
    ({}, StopScope.NONE, None),
    ({"error_category": "AUTH_ERROR"}, StopScope.ALL_FLAG, StopReason.AUTH_ERROR),
    ({"error_category": "PERMISSION_OR_CONTRACT_ERROR"}, StopScope.BACKFILL_FLAG, StopReason.PERMISSION_ERROR),
    ({"error_category": "SERVER_ERROR"}, StopScope.TODAY, StopReason.SERVER_ERROR),
    ({"error_category": "QUALITY_ERROR"}, StopScope.END_JOB, StopReason.QUALITY_ERROR),
    ({"rate_limit_429_count": 1}, StopScope.TODAY, StopReason.RATE_LIMITED),
    ({"retry_count": 10}, StopScope.TODAY, StopReason.RETRIES_EXCESSIVE),
    ({"retry_count": 10, "rate_limit_429_count": 0, "elapsed_seconds": 601}, StopScope.TODAY, StopReason.RETRIES_EXCESSIVE),
    ({"elapsed_seconds": 601}, StopScope.TODAY, StopReason.ELAPSED_OVER_LIMIT),
    ({"status": "FAILED", "failed_streak": 3}, StopScope.TODAY, StopReason.CONSECUTIVE_FAILED),
    ({"status": "FAILED", "failed_streak": 2}, StopScope.NONE, None),
    ({"warnings": ("UNSUPPORTED_FIELDS_RETAINED_IN_RAW",)}, StopScope.END_JOB, StopReason.UNKNOWN_FIELDS),
    ({"error_category": "CLIENT_REQUEST_ERROR"}, StopScope.END_JOB, StopReason.UNEXPECTED_400_IN_RANGE),
    ({"error_category": "CLIENT_REQUEST_ERROR", "target_date": date(2020, 1, 1)}, StopScope.END_JOB, StopReason.OUT_OF_CONTRACT_400),
])
def test_decision_table(over, scope, reason):
    decision = decide_after_day(obs(**over))
    assert (decision.scope, decision.reason) == (scope, reason)


def test_hold_thresholds():
    assert decide_after_day(obs(retry_count=3)).hold
    assert decide_after_day(obs(elapsed_seconds=421)).hold
    assert not decide_after_day(obs(retry_count=2, elapsed_seconds=420)).hold
    assert not decide_after_day(obs(retry_count=12, rate_limit_429_count=10)).hold


def test_helpers():
    assert next_failed_streak(2, "FAILED") == 3 and next_failed_streak(2, "ATTENTION") == 0
    days3 = [(TODAY - timedelta(days=i), StopReason.RATE_LIMITED) for i in (2, 1, 0)]
    assert is_repeated_today_stop(days3, StopReason.RATE_LIMITED)
    assert not is_repeated_today_stop(days3, StopReason.SERVER_ERROR)
    assert not is_repeated_today_stop(days3[1:], StopReason.RATE_LIMITED)


def test_classify_returns_enum():
    assert classify_client_request_error("daily", date(2020, 1, 1), TODAY) is (
        ClientRequestErrorKind.OUT_OF_CONTRACT_RANGE
    )


def test_unknown_status_fails_closed():
    decision = decide_after_day(obs(status="???"))
    assert (decision.scope, decision.reason) == (StopScope.END_JOB, StopReason.UNKNOWN_STATUS)


def test_policy_and_usecase_do_not_import_network_modules():
    import ast
    from pathlib import Path

    from src.application import jquants_backfill_usecase as usecase
    from src.domain import jquants_backfill_policy as policy

    forbidden = ("requests", "socket", "urllib", "httpx", "jquants_v2_client")
    for module in (policy, usecase):
        tree = ast.parse(Path(module.__file__).read_text(encoding="utf-8"))
        names = {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
        names |= {n.module or "" for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
        assert not [m for m in names if any(f in m for f in forbidden)], module.__name__
    assert "src.application" not in {
        n.module for n in ast.walk(ast.parse(Path(policy.__file__).read_text(encoding="utf-8")))
        if isinstance(n, ast.ImportFrom)}


def test_entrypoint_dry_run_does_not_build_ingestion(monkeypatch, tmp_path, capsys):
    from src.entrypoints import run_jquants_backfill as entry

    class Planner:
        def __init__(self, *args):
            pass

        def plan(self, dataset, start, end, today):
            return BackfillPlan((date(2026, 10, 1),), (), (), (), (), (), ())

    def forbidden(*args, **kwargs):
        raise AssertionError("must not be constructed in dry-run")

    monkeypatch.setattr(entry.config, "JQUANTS_BACKFILL_STATE_DIRECTORY", tmp_path)
    monkeypatch.setattr(entry, "JQuantsRawStore", lambda: object())
    monkeypatch.setattr(entry, "RawStoreLedgerState", lambda store: object())
    monkeypatch.setattr(entry, "JQuantsBackfillPlanner", Planner)
    monkeypatch.setattr(entry, "JQuantsSingleDayIngestion", forbidden)
    monkeypatch.setattr(entry, "JQuantsBackfillUseCase", forbidden)
    code = entry.main(["--dataset", "daily", "--from", "2026-10-01", "--to", "2026-10-02"])
    assert code == 0
    assert json.loads(capsys.readouterr().out)["status"] == "DRY_RUN"


@pytest.mark.parametrize("flag, value", [("--budget-seconds", "0"), ("--max-days", "0")])
def test_entrypoint_rejects_non_positive_limits(flag, value):
    from src.entrypoints import run_jquants_backfill as entry

    with pytest.raises(SystemExit):
        entry.main(["--dataset", "daily", flag, value])
