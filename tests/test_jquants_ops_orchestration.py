import builtins
from dataclasses import replace
from datetime import date, datetime, time, timedelta, timezone
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
from types import SimpleNamespace
from uuid import uuid4

import pytest

from src.application.jquants_backfill_usecase import BackfillOutcome, BackfillResult, JQuantsBackfillUseCase
from src.application.jquants_calendar_job import CalendarJobResult, JQuantsCalendarJob
from src.application.jquants_daily_disk_check import JQuantsDailyDiskCheck
from src.application.jquants_daily_job import DatasetOutcome, DailyJobResult, JQuantsDailyJob
from src.application.jquants_execution_guard import JQuantsExecutionGuard
from src.application.jquants_metrics_evaluation import JQuantsEvaluation, evaluate_summary
from src.application.jquants_ops_orchestration import JQuantsOpsOrchestration
from src.application.jquants_ops_reporting import (
    JQuantsOpsReporter, JQuantsReportedIngestion, daily_event_records,
)
from src.domain.jquants_backfill_plan import BackfillPlan
from src.domain.jquants_backfill_policy import StopReason
from src.domain.jquants_daily_plan import DailyDecision
from src.domain.jquants_daily_stop_policy import StopAction, repeated_daily_failure_stop
from src.domain.jquants_disk_space import DiskVerdict
from src.domain.jquants_ops_policy import JQuantsOpsEvent, aggregate_exit_codes, daily_ready
from src.infrastructure.market_data.jquants_backfill_state import (
    BackfillStateError,
    JQuantsBackfillStateStore,
)
from src.infrastructure.market_data.jquants_ops_events import JQuantsOpsEventRepository
from src.infrastructure.notification.jquants_notify import JQuantsSafeNotifier

DAY = date(2026, 10, 30)
NOW = datetime.combine(DAY, time(18), timezone(timedelta(hours=9)))
SECRET = "SECRET_key https://hooks.invalid/SECRET C:\\private\\SECRET 99990 response=SECRET"


def ready():
    return DailyJobResult(DAY, DailyDecision.TARGET, [
        DatasetOutcome(dataset, True, "COMPLETE", exit_code=0, verdict="Go",
                       not_evaluated=(), stop_scope="NONE")
        for dataset in ("daily", "minute")
    ])


def ops(tmp_path, *, daily=None, calendar=None, backfill=None, notify=True, now_provider=lambda: NOW):
    calls = []
    state = JQuantsBackfillStateStore(tmp_path / "state")
    guard = JQuantsExecutionGuard(state)
    reporter = JQuantsOpsReporter(
        JQuantsOpsEventRepository(tmp_path / "metrics"),
        lambda event: calls.append(("notify", event)) or notify,
    )

    def factory(stage):
        assert (tmp_path / "state" / "backfill.lock").exists()
        calls.append(("build", stage))

        def run_locked(*args, **kwargs):
            assert (tmp_path / "state" / "backfill.lock").exists()
            calls.append((stage, args, kwargs))
            if stage == "CALENDAR":
                return calendar or CalendarJobResult(0, "COMPLETE", stop_scope="NONE")
            if stage == "DAILY":
                return ready() if daily is None else daily
            return backfill or BackfillResult(args[0], BackfillOutcome.COMPLETED)

        return SimpleNamespace(run_locked=run_locked)

    instance = JQuantsOpsOrchestration(
        guard, lambda: factory("CALENDAR"), lambda: factory("DAILY"),
        lambda: factory("BACKFILL"), reporter, now_provider=now_provider,
    )
    return instance, state, calls


def test_order_single_lock_calendar_contract_and_no_refresh(tmp_path):
    job, state, calls = ops(tmp_path)
    result = job.run(NOW)
    stages = [call for call in calls if call[0] in ("CALENDAR", "DAILY", "BACKFILL")]
    assert [call[0] for call in stages] == ["CALENDAR", "DAILY", "BACKFILL", "BACKFILL"]
    assert stages[0][1] == (date(2021, 10, 30), DAY)
    assert stages[0][2] == {"refresh": False}
    assert [call[1][0] for call in stages[2:]] == ["daily", "minute"]
    assert result["exit_code"] == 0 and result["operational_go"] == "NOT_GRANTED"
    assert not (tmp_path / "state" / "backfill.lock").exists()
    assert (tmp_path / "metrics" / "ops" / "events.jsonl").exists()
    assert list((tmp_path / "metrics").glob("*/date=*/*.json")) == []


def test_ops_event_day_is_jst_execution_day_and_target_is_separate(tmp_path):
    job, _, _ = ops(tmp_path)
    utc_execution = datetime(2026, 10, 30, 16, 30, tzinfo=timezone.utc)
    job.event("CALENDAR", utc_execution, 0, "COMPLETE", target_day="2026-10-30")
    event = job.reporter.events[-1]
    assert event.day == "2026-10-31"
    assert event.target_day == "2026-10-30"


@pytest.mark.parametrize("decision,code", [
    (DailyDecision.HOLIDAY, 0), (DailyDecision.NOT_PUBLISHED, 0),
    (DailyDecision.CALENDAR_MISSING, 1),
])
def test_non_target_daily_no_backfill(tmp_path, decision, code):
    job, _, calls = ops(tmp_path, daily=DailyJobResult(DAY, decision))
    result = job.run(NOW)
    assert result["exit_code"] == code
    assert not any(call[0] == "BACKFILL" for call in calls)
    assert bool([call for call in calls if call[0] == "notify"]) == (decision != DailyDecision.HOLIDAY)
    assert any(event["reason"] == "DAILY_NOT_READY" for event in result["events"])


@pytest.mark.parametrize("field,value", [
    ("ran", False), ("status", "ATTENTION"), ("verdict", "Hold"),
    ("not_evaluated", ("DISK_SPACE",)), ("exit_code", 2),
])
def test_missing_current_readiness_blocks_backfill(tmp_path, field, value):
    daily = ready()
    daily.outcomes[0] = replace(daily.outcomes[0], **{field: value})
    job, _, calls = ops(tmp_path, daily=daily)
    job.run(NOW)
    assert not daily_ready(daily, DAY)
    assert not any(call[0] == "BACKFILL" for call in calls)


def test_old_go_metrics_are_not_readiness(tmp_path):
    daily = ready()
    daily.run_date -= timedelta(days=1)
    job, _, calls = ops(tmp_path, daily=daily)
    job.run(NOW)
    assert not any(call[0] == "BACKFILL" for call in calls)


def test_calendar_failure_stops_every_successor(tmp_path):
    job, _, calls = ops(tmp_path, calendar=CalendarJobResult(1, "FAILED", SECRET))
    result = job.run(NOW)
    assert result["exit_code"] == 1
    assert not any(call[0] in ("DAILY", "BACKFILL") for call in calls)
    assert SECRET not in json.dumps(result)


@pytest.mark.parametrize("outcome,code,notify", [
    (BackfillOutcome.BUDGET_EXHAUSTED, 0, False),
    (BackfillOutcome.COMPLETED_WITH_REVIEW, 0, True),
    (BackfillOutcome.STOPPED, 1, False),
    (BackfillOutcome.STOPPED_TODAY, 2, False),
    (BackfillOutcome.BLOCKED_BY_FLAG, 2, False),
    (BackfillOutcome.JST_DAY_CHANGED, 2, False),
])
def test_daily_backfill_not_complete_does_not_start_minute_or_implement_53(
    tmp_path, outcome, code, notify,
):
    job, _, calls = ops(tmp_path, backfill=BackfillResult("daily", outcome))
    result = job.run(NOW)
    assert len([call for call in calls if call[0] == "BACKFILL"]) == 1
    assert result["exit_code"] == code
    assert bool([call for call in calls if call[0] == "notify"]) == notify


def test_jst_rollover_after_calendar_stops_daily(tmp_path):
    job, _, calls = ops(tmp_path, now_provider=lambda: NOW + timedelta(days=1))
    result = job.run(NOW)
    assert result["exit_code"] == 2
    assert not any(call[0] == "DAILY" for call in calls)


def test_global_flag_and_corrupt_state_stop_before_dependency_construction(tmp_path):
    job, state, calls = ops(tmp_path)
    state.set_stop_flag("ALL_FLAG", "AUTH_ERROR", DAY)
    assert job.run(NOW)["exit_code"] == 2
    assert not any(call[0] == "build" for call in calls)
    (tmp_path / "state" / "backfill_state.json").write_text(SECRET, encoding="utf-8")
    job, _, calls = ops(tmp_path)
    result = job.run(NOW)
    assert result["exit_code"] == 1
    assert SECRET not in json.dumps(result)
    assert not any(call[0] == "build" for call in calls)


def test_backfill_flag_does_not_stop_calendar_or_daily(tmp_path):
    job, state, calls = ops(tmp_path)
    state.set_stop_flag("BACKFILL_FLAG", "DISK_FLOOR", DAY)
    job.run(NOW)
    assert any(call[0] == "CALENDAR" for call in calls)
    assert any(call[0] == "DAILY" for call in calls)


@pytest.mark.parametrize("scope,reason", [
    ("BACKFILL_FLAG", "DISK_FLOOR"),
    ("ALL_FLAG", "AUTH_ERROR"),
])
def test_backfill_stop_flags_notify_reason_scope_and_dataset(tmp_path, scope, reason):
    backfill = BackfillResult(
        "daily", BackfillOutcome.STOPPED, StopReason(reason),
        stop_flag={"scope": scope, "reason": reason, "set_on": DAY.isoformat()},
    )
    job, _, calls = ops(tmp_path, backfill=backfill)

    result = job.run(NOW, scheduled=True)

    event = next(
        event for event in result["events"]
        if event["stage"] == "BACKFILL" and event["dataset"] == "daily"
    )
    notified = [item[1] for item in calls if item[0] == "notify"]
    assert result["exit_code"] == 1
    assert event["reason"] == reason
    assert event["stop_scope"] == scope
    assert any(
        item.stage == "BACKFILL" and item.dataset == "daily"
        and item.reason == reason and item.stop_scope == scope
        for item in notified
    )


@pytest.mark.parametrize("notify", [True, False])
def test_backfill_state_write_failure_notifies_and_fails_safely(tmp_path, notify):
    job, _, calls = ops(tmp_path, notify=notify)

    def fail_write(*args, **kwargs):
        raise BackfillStateError(SECRET, write_failure=True)

    job.backfill_factory = lambda: SimpleNamespace(run_locked=fail_write)
    result = job.run(NOW, scheduled=True)

    event = next(
        event for event in result["events"]
        if event["stage"] == "BACKFILL" and event["event_id"] == "STOP_STATE_SAVE_FAILED"
    )
    notified = [item[1] for item in calls if item[0] == "notify"]
    assert result["exit_code"] == 1
    assert event["scheduled"] is True
    assert SECRET not in json.dumps(result)
    if notify:
        assert any(item.event_id == "STOP_STATE_SAVE_FAILED" for item in notified)
    else:
        assert any(item["event_id"] == "SLACK_NOTIFY_FAILED" for item in result["events"])


def test_scheduled_stale_skips_exclude_manual_and_never_take_lock(tmp_path):
    job, state, _ = ops(tmp_path)
    with state.global_lock():
        for scheduled in (True, False, True, True):
            job, _, calls = ops(tmp_path)
            result = job.run(NOW, scheduled=scheduled)
            assert result["exit_code"] == 2
            assert not any(call[0] == "build" for call in calls)
            assert any(event["stop_scope"] == "UNKNOWN" for event in result["events"])
            assert (tmp_path / "state" / "backfill.lock").exists()
        assert any(event["reason"] == "STALE_LOCK_CHECK_REQUIRED" for event in result["events"])
        notified = [item[1] for item in calls if item[0] == "notify"]
        assert any(event.reason == "GLOBAL_ALREADY_RUNNING" for event in notified)
        assert any(event.reason == "STALE_LOCK_CHECK_REQUIRED" for event in notified)
    job, _, _ = ops(tmp_path, daily=DailyJobResult(DAY, DailyDecision.HOLIDAY))
    job.run(NOW, scheduled=True)
    assert job.reporter.repository.scheduled_lock_streak() == 0


@pytest.mark.parametrize("body", [0, 1, 2, 3])
def test_notification_failure_retains_nonzero_child_codes(tmp_path, body):
    repository = JQuantsOpsEventRepository(tmp_path)
    reporter = JQuantsOpsReporter(repository, lambda event: False)
    event = JQuantsOpsEvent(uuid4().hex, "DAILY", DAY.isoformat(), body, "FAILED")
    reporter.emit(event, notify=True)
    assert reporter.exit_code == (body or 1)
    assert "SLACK_NOTIFY_FAILED" in (tmp_path / "ops" / "events.jsonl").read_text()
    reporter.emit(event, notify=True)
    assert len(reporter.events) == 2


def test_transport_never_logs_exception_response_or_webhook(tmp_path, capsys, caplog):
    class Response:
        text = SECRET

        def raise_for_status(self):
            raise ValueError(SECRET)

    event = JQuantsOpsEvent(uuid4().hex, "DAILY", DAY.isoformat(), 1, "FAILED")
    notifier = JQuantsSafeNotifier(SECRET, post=lambda *args, **kwargs: Response())
    repository = JQuantsOpsEventRepository(tmp_path)
    reporter = JQuantsOpsReporter(repository, notifier)
    reporter.emit(event, notify=True)
    assert reporter.exit_code == 1
    assert SECRET not in caplog.text + capsys.readouterr().out + (tmp_path / "ops" / "events.jsonl").read_text()
    assert JQuantsSafeNotifier("")(event) is False
    assert notifier(SimpleNamespace(as_dict=lambda: {"secret": SECRET})) is False


def test_record_failure_is_not_success_and_has_safe_stderr_code(tmp_path, monkeypatch, capsys):
    from src.entrypoints import run_jquants_ops as cli
    job, _, _ = ops(tmp_path)
    monkeypatch.setattr(job.reporter.repository, "append", lambda event: (_ for _ in ()).throw(OSError(SECRET)))
    monkeypatch.setattr(cli, "_preflight", lambda: True)
    monkeypatch.setattr(cli, "_build_ops", lambda: job)
    assert cli.main(["--execute"]) == 1
    output = capsys.readouterr()
    assert "JQUANTS_LOCAL_EVENT_FAILED" in output.err and SECRET not in output.out + output.err


def test_plan_only_and_invalid_arguments_do_not_audit_or_build(monkeypatch, capsys):
    from src.entrypoints import run_jquants_ops as cli
    monkeypatch.setattr(cli, "_preflight", lambda: pytest.fail("plan audited"))
    monkeypatch.setattr(cli, "_build_ops", lambda: pytest.fail("plan built"))
    assert cli.main([]) == 0
    assert cli.main(["--scheduled"]) == 2
    assert cli.main(["--secret", SECRET]) == 2
    assert SECRET not in capsys.readouterr().out


def test_plan_import_does_not_import_config_application_or_infrastructure():
    script = """
import builtins
original = builtins.__import__
def guarded(name, *args, **kwargs):
    if name.startswith(('src.config', 'src.application', 'src.infrastructure')):
        raise RuntimeError('unexpected dependency')
    return original(name, *args, **kwargs)
builtins.__import__ = guarded
from src.entrypoints.run_jquants_ops import main
raise SystemExit(main([]))
"""
    result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, check=False)
    assert result.returncode == 0 and result.stderr == ""
    assert json.loads(result.stdout)["status"] == "PLAN_ONLY"


def test_real_config_import_failure_is_caught_without_secret_output():
    script = """
import builtins, sys
from src.entrypoints import run_jquants_ops as cli
cli._preflight = lambda: True
original = builtins.__import__
def guarded(name, *args, **kwargs):
    if name == 'src.config':
        print('SECRET_CONFIG_IMPORT', file=sys.stderr)
        raise RuntimeError('SECRET_CONFIG_IMPORT')
    return original(name, *args, **kwargs)
builtins.__import__ = guarded
raise SystemExit(cli.main(['--execute', '--scheduled']))
"""
    result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, check=False)
    assert result.returncode == 1 and result.stderr.strip() == "JQUANTS_LOCAL_EVENT_UNAVAILABLE"
    assert "SECRET_CONFIG_IMPORT" not in result.stdout
    assert json.loads(result.stdout)["reason"] == "INITIALIZATION_ERROR"


def test_python_audit_boundary_uses_all_six_environment_settings(monkeypatch, tmp_path):
    from src.entrypoints import run_jquants_ops as cli
    from src import jquants_storage
    settings = {
        name: str(tmp_path / name)
        for name in (
            "JQUANTS_RAW_DIRECTORY", "JQUANTS_CANONICAL_DIRECTORY",
            "JQUANTS_LEDGER_DATABASE_FILE", "JQUANTS_EXECUTION_METRICS_DIRECTORY",
            "JQUANTS_BACKFILL_STATE_DIRECTORY", "JQUANTS_CALENDAR_DIRECTORY",
        )
    }
    calls = []
    monkeypatch.setattr(cli.os, "environ", settings)
    monkeypatch.setattr(
        jquants_storage, "inspect_jquants_storage",
        lambda defaults, environ: calls.append(environ) or SimpleNamespace(is_valid=True),
    )
    assert cli._preflight() is True and calls == [settings]


@pytest.mark.parametrize("audit", [True, False])
def test_audit_precedes_any_dependency_or_config_import(monkeypatch, capsys, audit):
    from src.entrypoints import run_jquants_ops as cli
    order = []
    monkeypatch.setattr(cli, "_preflight", lambda: order.append("audit") or audit)

    def config_failure():
        order.append("build")
        print(SECRET)
        raise RuntimeError(SECRET)

    monkeypatch.setattr(cli, "_build_ops", config_failure)
    assert cli.main(["--execute", "--scheduled"]) == 1
    assert order == (["audit", "build"] if audit else ["audit"])
    output = capsys.readouterr()
    assert SECRET not in output.out + output.err


def daily_job(tmp_path, *, category="STORAGE_OR_FORMAT_ERROR", boom=False, disk=None):
    state = JQuantsBackfillStateStore(tmp_path / "state")
    calls = []
    categories = {
        DAY + timedelta(days=offset): 1 if (DAY + timedelta(days=offset)).weekday() < 5 else 0
        for offset in range(-14, 15)
    }

    def run(*, dataset, date):
        calls.append((dataset, date))
        if boom:
            raise RuntimeError(SECRET)
        return SimpleNamespace(status="FAILED", error_category=category,
                               exit_code=3 if category == "RATE_LIMIT_ABORTED" else 1)

    job = JQuantsDailyJob(
        SimpleNamespace(categories=lambda start, end: categories),
        lambda: SimpleNamespace(run=run),
        SimpleNamespace(consecutive_failed=lambda dataset: 100), guard=JQuantsExecutionGuard(state),
        disk_check=disk,
    )
    return job, state, calls


def test_unexpected_storage_failure_recorded_exactly_once_not_metrics_double_counted(tmp_path):
    job, state, _ = daily_job(tmp_path, boom=True)
    first = job.run(DAY, NOW)
    assert first.outcomes[0].stop_action is StopAction.CONTINUE
    assert len(state.daily_history("daily")) == 1
    job.run(DAY, NOW)
    third = job.run(DAY, NOW)
    assert third.outcomes[0].stop_action is StopAction.STOP_TODAY
    assert len(state.daily_history("daily")) == 3
    assert state.daily_stop("minute", DAY) == "CONSECUTIVE_FAILED"
    assert SECRET not in (tmp_path / "state" / "backfill_state.json").read_text()


def test_daily_three_adjacent_business_stops_promote_non429_only(tmp_path):
    job, state, _ = daily_job(tmp_path)
    for prior in (DAY - timedelta(days=3), DAY - timedelta(days=2)):
        state.record_daily_result("daily", prior, "FAILED", "STORAGE_OR_FORMAT_ERROR")
    for day in (DAY - timedelta(days=1), DAY, DAY + timedelta(days=3)):
        job.run(day, datetime.combine(day, time(18), NOW.tzinfo))
    assert state.stop_flag()["reason"] == "REPEATED_TODAY_STOP"


def test_daily_429_never_promotes_even_with_large_failure_streak(tmp_path):
    job, state, _ = daily_job(tmp_path, category="RATE_LIMIT_ABORTED")
    for day in (DAY - timedelta(days=1), DAY, DAY + timedelta(days=3)):
        assert job.run(day, datetime.combine(day, time(18), NOW.tzinfo)).exit_code == 3
    assert state.stop_flag() is None
    assert state.daily_stop("minute", DAY) == "RATE_LIMIT_ABORTED"
    assert all(event["reason"] == "RATE_LIMIT_ABORTED" for event in state.daily_history("daily"))


@pytest.mark.parametrize("interruption", ["missing", "success", "different_cause"])
def test_promotion_business_gaps_success_and_different_causes_break_streak(interruption):
    dates = [DAY - timedelta(days=2), DAY - timedelta(days=1), DAY]
    stops = [{"date": day.isoformat(), "reason": "CONSECUTIVE_FAILED"} for day in dates]
    history = [{"date": day.isoformat(), "status": "FAILED", "reason": "SERVER_ERROR"} for day in dates]
    if interruption == "missing":
        history.pop(1)
    elif interruption == "success":
        history[1]["status"] = "COMPLETE"
    else:
        history[1]["reason"] = "STORAGE_OR_FORMAT_ERROR"
    assert not repeated_daily_failure_stop(stops, history, {day: 1 for day in dates}, DAY)


@pytest.mark.parametrize("scope,dataset,blocked", [
    ("STOP_TODAY", "minute", True), ("STOP_JOB", "daily", True), ("STOP_JOB", "minute", False),
])
@pytest.mark.parametrize("mode", [{}, {"refresh": True}, {"canonical_only": True}])
def test_daily_execution_day_stops_respected_by_manual_all_modes(tmp_path, scope, dataset, blocked, mode):
    state = JQuantsBackfillStateStore(tmp_path)
    state.record_daily_result("daily", DAY, "FAILED", "QUALITY_ERROR", stop_scope=scope)
    guard = JQuantsExecutionGuard(state, today_provider=lambda: DAY)
    called = []
    result = guard.run_single_day(
        lambda: SimpleNamespace(run=lambda **kwargs: called.append(kwargs) or SimpleNamespace(exit_code=0)),
        dataset=dataset, date=(DAY - timedelta(days=7)).isoformat(), **mode,
    )
    assert bool(called) == (not blocked)
    assert result.exit_code == (2 if blocked else 0)


def test_backfill_today_stop_does_not_block_manual_and_daily(tmp_path):
    job, state, calls = daily_job(tmp_path)
    state.record_today_stop("daily", DAY, "RATE_LIMITED")
    job.run(DAY, NOW)
    assert calls


@pytest.mark.parametrize("category,reason", [
    ("AUTH_ERROR", "AUTH_ERROR"), ("PERMISSION_OR_CONTRACT_ERROR", "PERMISSION_ERROR"),
])
def test_daily_auth_flags_use_existing_reason_codes(tmp_path, category, reason):
    job, state, calls = daily_job(tmp_path, category=category)
    assert job.run(DAY, NOW).exit_code == 1
    assert state.stop_flag() == {"scope": "ALL_FLAG", "reason": reason, "set_on": DAY.isoformat()}
    assert len(calls) == 1


@pytest.mark.parametrize("verdict", [None, DiskVerdict.STOP])
def test_daily_disk_unmeasured_and_critical_stop_before_api(tmp_path, verdict):
    job, state, calls = daily_job(tmp_path, disk=lambda dataset: verdict)
    assert job.run(DAY, NOW).exit_code == 1 and calls == []
    assert (state.stop_flag() is not None) == (verdict is DiskVerdict.STOP)


def test_daily_disk_probe_error_never_treated_as_sufficient():
    check = JQuantsDailyDiskCheck("raw", "canonical", volume_probe=lambda path: (_ for _ in ()).throw(OSError(SECRET)))
    assert check("daily") is None


def test_stop_state_write_failure_retains_429_and_emits_distinct_error(tmp_path, monkeypatch):
    job, state, _ = daily_job(tmp_path, category="RATE_LIMIT_ABORTED")
    monkeypatch.setattr(state, "save", lambda value: (_ for _ in ()).throw(BackfillStateError(SECRET)))
    result = job.run(DAY, NOW)
    assert result.exit_code == 3
    assert result.outcomes[-1].reason == "STOP_STATE_SAVE_FAILED"
    assert len(result.outcomes) == 2
    assert SECRET not in json.dumps(result.as_dict())


@pytest.mark.parametrize("save_failure", [False, True])
def test_429_safe_boundary_not_duplicated_and_state_failure_is_distinct(tmp_path, monkeypatch, save_failure):
    job, state, calls = ops(tmp_path)

    class Ingestion:
        def run(self, *, dataset, date):
            self.notifier(SECRET)
            return SimpleNamespace(status="FAILED", error_category="RATE_LIMIT_ABORTED", exit_code=3)

    job.daily_factory = lambda: JQuantsDailyJob(
        SimpleNamespace(categories=lambda start, end: {DAY: 1}),
        lambda: JQuantsReportedIngestion(Ingestion(), job.reporter, lambda: job),
        None, guard=job.guard,
    )
    if save_failure:
        monkeypatch.setattr(state, "save", lambda value: (_ for _ in ()).throw(BackfillStateError(SECRET)))
    result = job.run(NOW, scheduled=True)
    notified = [call[1] for call in calls if call[0] == "notify"]
    assert result["exit_code"] == 3 and len(notified) == (2 if save_failure else 1)
    assert notified[0].event_id == "RATE_LIMIT_ABORTED" and notified[0].scheduled
    if save_failure:
        assert notified[1].event_id == "STOP_STATE_SAVE_FAILED"
    assert SECRET not in json.dumps(result)


def test_429_notification_result_is_scoped_to_this_emission(tmp_path):
    notifier_results = iter([False, True])
    reporter = JQuantsOpsReporter(
        JQuantsOpsEventRepository(tmp_path / "metrics"),
        lambda event: next(notifier_results),
    )
    reporter.emit(JQuantsOpsEvent(uuid4().hex, "CALENDAR", DAY.isoformat(), 1, "FAILED"), notify=True)
    context = SimpleNamespace(
        invocation_id=uuid4().hex, current_stage="BACKFILL", scheduled=False,
        now_provider=lambda: NOW + timedelta(days=1),
    )

    class Ingestion:
        def run(self, **kwargs):
            assert self.notifier("ignored")
            return SimpleNamespace(status="FAILED")

    ingestion = Ingestion()
    result = JQuantsReportedIngestion(ingestion, reporter, lambda: context).run(
        dataset="daily", date="2021-11-08",
    )
    rate_event = next(event for event in reporter.events if event.event_id == "RATE_LIMIT_ABORTED")
    assert result.status == "FAILED"
    assert rate_event.day == (NOW + timedelta(days=1)).date().isoformat()
    assert rate_event.target_day == "2021-11-08"
    assert sum(event.event_id == "SLACK_NOTIFY_FAILED" for event in reporter.events) == 1


@pytest.mark.parametrize("verdict,not_evaluated,reason", [
    ("No-Go", (), "NO_GO"), ("Go", ("METRICS",), "EVALUATION_INCOMPLETE"),
])
def test_daily_reporting_notifies_no_go_and_incomplete_in_both_paths(
    tmp_path, verdict, not_evaluated, reason,
):
    result = DailyJobResult(DAY, DailyDecision.TARGET, [
        DatasetOutcome("daily", True, "COMPLETE", exit_code=0, verdict=verdict,
                       not_evaluated=not_evaluated, stop_scope="NONE"),
    ])
    standalone = JQuantsOpsReporter(JQuantsOpsEventRepository(tmp_path / "standalone"), lambda _: True)
    standalone.daily(result, uuid4().hex, DAY)
    emitted, notify, _ = daily_event_records(result, uuid4().hex, DAY, "NONE")[0]
    assert standalone.events[0].reason == reason
    assert standalone.events[0].reason == emitted.reason and notify is True
    assert any(event.reason == reason for event in standalone.events)


def test_daily_verification_preserves_partial_evaluation_and_approved_limits(tmp_path):
    metrics = {
        "dataset": "daily", "status": "COMPLETE", "error_category": None,
        "warnings": [], "unknown_field_count": 0, "symbol_count": 10, "raw_source": "fetch",
        "disk_free_bytes_raw": 500_000_000_000, "disk_free_bytes_canonical": 500_000_000_000,
        "disk_same_volume": True, "raw_size_bytes": 10, "canonical_size_bytes": 10,
        "request_count": 1, "response_status_counts": {"200": 1},
        "rate_limit_429_count": 0, "retry_count": 0,
        "durations_seconds": {"acquisition": 1, "conversion": 1, "total": 61},
    }
    path = tmp_path / "metrics.json"
    path.write_text(json.dumps(metrics), encoding="utf-8")
    evaluation = evaluate_summary(SimpleNamespace(dataset="daily", metrics_file=path))
    assert "ELAPSED_LIMIT_UNSET" not in evaluation.reasons
    assert evaluation.verdict == "No-Go" and "ELAPSED_OVER_LIMIT" in evaluation.reasons
    assert evaluate_summary(SimpleNamespace(dataset="daily", metrics_file=None)).not_evaluated == ("METRICS",)


def budget_usecase(tmp_path, *, elapsed=1, limit=7200, retry=0, crash=False, now=lambda: DAY):
    state = JQuantsBackfillStateStore(tmp_path / "state")
    ticks = [0.0]
    calls = []
    targets = (DAY - timedelta(days=2), DAY - timedelta(days=1))
    plan = BackfillPlan(targets, (), (), (), (), (), ())

    def run(*, dataset, date):
        calls.append((dataset, date))
        ticks[0] += elapsed
        if crash:
            raise RuntimeError(SECRET)
        return SimpleNamespace(
            status="COMPLETE", error_category=None, warnings=[], durations_seconds={"total": 0},
            raw_size_bytes=1, canonical_size_bytes=1,
            execution_metrics={"rate_limit_429_count": 0, "retry_count": retry},
        )

    usecase = JQuantsBackfillUseCase(
        SimpleNamespace(plan=lambda *args: plan), SimpleNamespace(run=run), state,
        raw_directory=tmp_path / "raw", canonical_directory=tmp_path / "canonical",
        today_provider=now, volume_probe=lambda path: (500_000_000_000, 1),
        budget_seconds=limit, clock=lambda: ticks[0],
    )
    return usecase, state, calls


def test_budget_monotonic_not_summary_shared_across_restart_and_dataset(tmp_path):
    first, state, calls = budget_usecase(tmp_path, elapsed=10, limit=15)
    assert first.run("daily", DAY - timedelta(days=2), DAY).outcome is BackfillOutcome.COMPLETED
    assert len(calls) == 2 and state.load()["shared_budget"]["spent_seconds"] == 20
    next_run, _, calls = budget_usecase(tmp_path, elapsed=0, limit=7200)
    result = next_run.run("minute", DAY - timedelta(days=2), DAY)
    assert result.outcome is BackfillOutcome.BUDGET_EXHAUSTED and calls == []
    assert state.load()["shared_budget"]["limit_seconds"] == 15


def test_7200_soft_boundary_overshoot_finishes_started_day(tmp_path):
    usecase, state, calls = budget_usecase(tmp_path, elapsed=7300)
    result = usecase.run("daily", DAY - timedelta(days=2), DAY)
    assert result.outcome is BackfillOutcome.BUDGET_EXHAUSTED and len(calls) == 1
    assert state.load()["shared_budget"]["spent_seconds"] == 7300
    assert state.load()["shared_budget"]["in_progress"] is None


def test_budget_hold_persists_across_invocations(tmp_path):
    first, state, calls = budget_usecase(tmp_path, elapsed=10, limit=15, retry=3)
    first.run("daily", DAY - timedelta(days=2), DAY)
    assert len(calls) == 1 and state.load()["shared_budget"]["held"]
    second, _, calls = budget_usecase(tmp_path, limit=15)
    assert second.run("minute", DAY - timedelta(days=2), DAY).outcome is BackfillOutcome.BUDGET_EXHAUSTED
    assert calls == []


def test_new_known_jst_day_can_start_fresh_budget(tmp_path):
    first, state, _ = budget_usecase(tmp_path, elapsed=10, limit=15)
    first.run("daily", DAY - timedelta(days=2), DAY)
    second, _, calls = budget_usecase(tmp_path, now=lambda: DAY + timedelta(days=1), elapsed=2)
    assert second.run("minute", DAY - timedelta(days=2), DAY).outcome is BackfillOutcome.COMPLETED
    assert len(calls) == 2
    assert state.load()["shared_budget"]["spent_seconds"] == 4
    assert state.load()["shared_budget"]["date"] == (DAY + timedelta(days=1)).isoformat()


def test_exit_severity_preserves_existing_daily_order():
    assert aggregate_exit_codes([0, 2, 1, 3]) == 3
    assert aggregate_exit_codes([0, 2, 1]) == 1
    assert aggregate_exit_codes([0, 2]) == 2


@pytest.mark.parametrize("new_day", [False, True])
def test_crash_keeps_unknown_budget_and_blocks_even_next_day(tmp_path, new_day):
    broken, state, _ = budget_usecase(tmp_path, crash=True)
    with pytest.raises(RuntimeError):
        broken.run("daily", DAY - timedelta(days=2), DAY)
    assert state.load()["shared_budget"]["in_progress"] is not None
    next_run, _, calls = budget_usecase(
        tmp_path, now=lambda: DAY + timedelta(days=int(new_day)),
    )
    with pytest.raises(BackfillStateError):
        next_run.run("minute", DAY - timedelta(days=2), DAY)
    assert calls == []
    assert SECRET not in (tmp_path / "state" / "backfill_state.json").read_text()


def test_budget_jst_rollover_stops_without_reattributing_consumption(tmp_path):
    days = iter([DAY, DAY, DAY + timedelta(days=1)])
    usecase, state, calls = budget_usecase(tmp_path, elapsed=10, now=lambda: next(days, DAY + timedelta(days=1)))
    result = usecase.run("daily", DAY - timedelta(days=2), DAY)
    assert result.outcome is BackfillOutcome.JST_DAY_CHANGED and len(calls) == 1
    assert state.load()["shared_budget"]["date"] == DAY.isoformat()
    assert state.load()["shared_budget"]["spent_seconds"] == 10


@pytest.mark.parametrize("field,value", [
    ("daily_history", []), ("daily_stops", {}), ("shared_budget", {"date": SECRET}),
    ("failed_streak", {"daily": -1}), ("today_stops", {"daily": [{"date": DAY.isoformat(), "reason": SECRET}]}),
])
def test_state_invalid_old_and_new_values_fail_closed(tmp_path, field, value):
    state = JQuantsBackfillStateStore(tmp_path)
    content = state.load()
    content[field] = value
    (tmp_path / "backfill_state.json").write_text(json.dumps(content), encoding="utf-8")
    with pytest.raises(BackfillStateError):
        state.load()


def test_legacy_state_gets_safe_new_fields_without_losing_flags_or_reviews(tmp_path):
    state = JQuantsBackfillStateStore(tmp_path)
    state.set_stop_flag("BACKFILL_FLAG", "DISK_FLOOR", DAY)
    state.mark_review("PERMISSION_403", "minute", DAY)
    content = state.load()
    for key in ("daily_history", "daily_stops", "shared_budget"):
        content.pop(key)
    (tmp_path / "backfill_state.json").write_text(json.dumps(content), encoding="utf-8")
    assert state.load()["daily_stops"] == [] and state.load()["shared_budget"] is None
    assert state.stop_flag()["scope"] == "BACKFILL_FLAG"
    assert state.review_dates("minute") == {DAY}


@pytest.mark.parametrize("scope", ["ALL_FLAG", "BACKFILL_FLAG"])
def test_calendar_guard_flags_lock_before_any_client_store(tmp_path, scope):
    state = JQuantsBackfillStateStore(tmp_path)
    state.set_stop_flag(scope, "AUTH_ERROR", DAY)
    calls = []

    def client():
        calls.append("client")
        assert (tmp_path / "backfill.lock").exists()
        raise ValueError(SECRET)

    job = JQuantsCalendarJob(
        JQuantsExecutionGuard(state), client, lambda: pytest.fail("unexpected store"),
    )
    result = job.run(DAY, DAY)
    assert calls == ([] if scope == "ALL_FLAG" else ["client"])
    assert result.exit_code == (2 if scope == "ALL_FLAG" else 1)
    assert SECRET not in json.dumps(result.as_dict())


@pytest.mark.parametrize("category", ["AUTH_ERROR", "PERMISSION_OR_CONTRACT_ERROR"])
def test_daily_required_halt_and_history_commit_in_one_atomic_save(tmp_path, monkeypatch, category):
    job, state, calls = daily_job(tmp_path, category=category)
    original = state.save
    saves = []

    def save_once(value):
        saves.append(value)
        if len(saves) > 1:
            raise BackfillStateError("second write must not happen")
        original(value)

    monkeypatch.setattr(state, "save", save_once)
    result = job.run(DAY, NOW)
    assert result.exit_code == 1 and len(calls) == 1 and len(saves) == 1
    assert saves[0]["stop_flag"]["scope"] == "ALL_FLAG"
    assert len(state.daily_history("daily")) == 1
    assert state.stop_flag()["scope"] == "ALL_FLAG"


def test_failed_atomic_halt_commit_does_not_leave_unrestricted_auth_history(tmp_path, monkeypatch):
    job, state, _ = daily_job(tmp_path, category="AUTH_ERROR")
    monkeypatch.setattr(state, "save", lambda value: (_ for _ in ()).throw(BackfillStateError(SECRET)))
    assert job.run(DAY, NOW).outcomes[-1].reason == "STOP_STATE_SAVE_FAILED"
    assert state.daily_history("daily") == [] and state.stop_flag() is None


@pytest.mark.parametrize("interruption", ["COMPLETE", "ATTENTION", "different_cause", "RATE_LIMIT_ABORTED"])
def test_success_then_failure_same_business_day_breaks_promotion(interruption):
    dates = [DAY - timedelta(days=2), DAY - timedelta(days=1), DAY]
    stops = [{"date": day.isoformat(), "reason": "CONSECUTIVE_FAILED"} for day in dates]
    failures = [{"date": day.isoformat(), "status": "FAILED", "reason": "SERVER_ERROR"} for day in dates]
    reset = {
        "date": dates[1].isoformat(), "status": interruption if interruption in ("COMPLETE", "ATTENTION") else "FAILED",
        "reason": "NONE" if interruption in ("COMPLETE", "ATTENTION") else (
            "STORAGE_OR_FORMAT_ERROR" if interruption == "different_cause" else interruption
        ),
    }
    history = [failures[0], reset, failures[1], failures[2]]
    assert not repeated_daily_failure_stop(stops, history, {day: 1 for day in dates}, DAY)


def test_daily_metrics_write_failure_preserves_429_and_separate_persistence_event(tmp_path):
    from src.application.jquants_single_day_ingestion import JQuantsIngestionSummary
    job, state, calls = daily_job(tmp_path)
    summary = JQuantsIngestionSummary("daily", DAY.isoformat(), error_category="RATE_LIMIT_ABORTED",
                                    metrics_persistence_failed=True)
    job._ingestion_factory = lambda: SimpleNamespace(run=lambda **kwargs: calls.append(kwargs) or summary)
    result = job.run(DAY, NOW)
    assert result.exit_code == 3 and len(calls) == 1
    assert result.outcomes[0].error_category == "RATE_LIMIT_ABORTED"
    assert result.outcomes[0].stop_action is StopAction.STOP_TODAY
    assert result.outcomes[1].reason == "METRICS_SAVE_FAILED"
    assert state.daily_history("daily") == [{
        "date": DAY.isoformat(), "status": "FAILED", "reason": "RATE_LIMIT_ABORTED",
    }]
    assert state.stop_flag() is None
    reporter = JQuantsOpsReporter(JQuantsOpsEventRepository(tmp_path / "metrics"), lambda event: True)
    reporter.daily(result, uuid4().hex, DAY)
    assert any(event.event_id == "METRICS_SAVE_FAILED" for event in reporter.events)
    assert reporter.exit_code == 3


def test_attempted_original_default_event_write_fails_before_any_io(_jquants_execution_io_guard):
    default_metrics = _jquants_execution_io_guard[3]
    event = JQuantsOpsEvent(uuid4().hex, "DAILY", DAY.isoformat(), 1, "FAILED")
    with pytest.raises(pytest.fail.Exception, match="JQUANTS_DEFAULT_DESTINATION_WRITE_BLOCKED"):
        JQuantsOpsEventRepository(default_metrics).append(event)


def test_all_jquants_storage_paths_are_isolated_and_write_holes_are_blocked(
    _jquants_execution_io_guard, tmp_path,
):
    from tests.conftest import JQUANTS_STORAGE_KEYS
    from src.config import config

    production = _jquants_execution_io_guard
    assert len(production) == len(JQUANTS_STORAGE_KEYS) == 6
    assert all(Path(getattr(config, key)) != root for key, root in zip(JQUANTS_STORAGE_KEYS, production))
    source = tmp_path / "safe-source"
    source.write_text("test-only", encoding="utf-8")
    source_dir = tmp_path / "safe-source-dir"
    source_dir.mkdir()
    (source_dir / "file").write_text("test-only", encoding="utf-8")
    ledger = production[2]
    with pytest.raises(pytest.fail.Exception, match="JQUANTS_DEFAULT_DESTINATION_WRITE_BLOCKED"):
        sqlite3.connect(ledger)
    with pytest.raises(pytest.fail.Exception, match="JQUANTS_DEFAULT_DESTINATION_WRITE_BLOCKED"):
        os.replace(source, production[0] / "guard-probe")
    with pytest.raises(pytest.fail.Exception, match="JQUANTS_DEFAULT_DESTINATION_WRITE_BLOCKED"):
        shutil.copyfile(source, production[3] / "guard-probe")
    with pytest.raises(pytest.fail.Exception, match="JQUANTS_DEFAULT_DESTINATION_WRITE_BLOCKED"):
        shutil.copytree(source_dir, production[4] / "guard-probe")
    with pytest.raises(pytest.fail.Exception, match="JQUANTS_DEFAULT_DESTINATION_WRITE_BLOCKED"):
        shutil.rmtree(production[5])
    assert source.read_text(encoding="utf-8") == "test-only"
