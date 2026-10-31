import json
from datetime import date, datetime, time
from unittest.mock import Mock

import pytest

from src.application.jquants_backfill_usecase import JQuantsBackfillUseCase
from src.application.jquants_daily_job import JQuantsDailyJob
from src.application.jquants_execution_guard import JQuantsExecutionGuard
from src.application.jquants_single_day_ingestion import JQuantsIngestionSummary
from src.domain.jquants_backfill_plan import BackfillPlan
from src.entrypoints import ingest_jquants_single_day as manual
from src.entrypoints import run_jquants_daily as daily
from src.infrastructure.market_data.jquants_backfill_state import (
    BackfillLockedError,
    BackfillStateError,
    JQuantsBackfillStateStore,
    LOCK_FILE,
    REVIEW_KINDS,
    STATE_FILE,
)

DAY = date(2026, 10, 30)


@pytest.fixture
def state(tmp_path, monkeypatch):
    directory = tmp_path / "state"
    monkeypatch.setattr(daily.config, "JQUANTS_BACKFILL_STATE_DIRECTORY", directory)
    return JQuantsBackfillStateStore(directory)


def forbidden():
    pytest.fail("Ingestion/API/raw recovery/publication must not be initialized")


def make_job(state, factory=forbidden):
    calendar = Mock()
    calendar.categories.return_value = {DAY: 1}
    history = Mock()
    history.consecutive_failed.return_value = 0
    return JQuantsDailyJob(
        calendar, factory, history, guard=JQuantsExecutionGuard(state),
    )


@pytest.mark.parametrize("reason", ["AUTH_ERROR", "DISK_SPACE_CRITICAL"])
@pytest.mark.parametrize("past", [False, True])
def test_daily_and_recovery_all_flag_blocks_every_dataset(state, reason, past):
    state.set_stop_flag("ALL_FLAG", reason, DAY)
    now = datetime(2026, 11, 2, 9) if past else datetime.combine(DAY, time(18))
    result = make_job(state).run(DAY, now)
    assert result.exit_code == 2
    assert all(not o.ran and o.reason == reason for o in result.outcomes)
    assert all(o.error_category == "BLOCKED_BY_FLAG" for o in result.outcomes)
    assert state.stop_flag()["scope"] == "ALL_FLAG"


@pytest.mark.parametrize("reason", ["AUTH_ERROR", "DISK_SPACE_CRITICAL"])
@pytest.mark.parametrize("mode", [[], ["--refresh"], ["--canonical-only"]])
def test_manual_all_modes_block_before_store_creation(state, reason, mode, monkeypatch, capsys):
    state.set_stop_flag("ALL_FLAG", reason, DAY)
    monkeypatch.setattr(manual, "JQuantsRawStore", forbidden)
    monkeypatch.setattr(manual, "JQuantsCanonicalStore", lambda path: forbidden())
    code = manual.main(["--dataset", "daily", "--date", DAY.isoformat(), *mode])
    body = json.loads(capsys.readouterr().out)
    assert code == 2 and body["status"] == "BLOCKED_BY_FLAG"
    assert body["reason"] == reason and body["ran"] is False
    assert body["date"] == DAY.isoformat()


@pytest.mark.parametrize("mode", [[], ["--refresh"], ["--canonical-only"]])
def test_manual_backfill_flag_does_not_block_and_holds_global_lock(
    state, mode, monkeypatch, capsys,
):
    state.set_stop_flag("BACKFILL_FLAG", "PERMISSION_ERROR", DAY)
    state.mark_review("PERMISSION_403", "daily", DAY)
    calls = []

    def ingest(**kwargs):
        with pytest.raises(BackfillLockedError):
            with state.global_lock():
                state.clear_stop_flag()
        calls.append(kwargs)
        return JQuantsIngestionSummary("daily", DAY.isoformat(), status="COMPLETE")

    monkeypatch.setattr(manual, "_ingestion", lambda measure: Mock(run=ingest))
    assert manual.main(["--dataset", "daily", "--date", DAY.isoformat(), *mode]) == 0
    capsys.readouterr()
    assert calls == [{
        "dataset": "daily", "date": DAY.isoformat(),
        "refresh": "--refresh" in mode, "canonical_only": "--canonical-only" in mode,
    }]
    with state.global_lock():
        assert state.stop_flag()["scope"] == "BACKFILL_FLAG"


def test_daily_backfill_history_does_not_block_and_one_lock_covers_both_datasets(state):
    state.set_stop_flag("BACKFILL_FLAG", "DISK_FLOOR", DAY)
    state.record_today_stop("daily", DAY, "RATE_LIMITED")
    calls = []

    def ingest(**kwargs):
        with pytest.raises(BackfillLockedError):
            with state.global_lock():
                pass
        calls.append(kwargs["dataset"])
        return JQuantsIngestionSummary(**kwargs, status="COMPLETE")

    result = make_job(state, lambda: Mock(run=ingest)).run(DAY, datetime.combine(DAY, time(18)))
    assert result.exit_code == 0 and calls == ["daily", "minute"]
    with state.global_lock():
        pass


@pytest.mark.parametrize("kind", REVIEW_KINDS)
@pytest.mark.parametrize("dataset", ["daily", "minute"])
def test_review_excludes_only_matching_dataset_date(state, kind, dataset):
    state.mark_review(kind, dataset, DAY)
    calls = []

    def ingest(**kwargs):
        calls.append(kwargs["dataset"])
        return JQuantsIngestionSummary(**kwargs, status="COMPLETE")

    job = make_job(state, lambda: Mock(run=ingest))
    result = job.run(DAY, datetime.combine(DAY, time(18)))
    assert result.exit_code == 2
    assert calls == ["minute" if dataset == "daily" else "daily"]
    excluded = next(o for o in result.outcomes if o.dataset == dataset)
    assert not excluded.ran and excluded.reason == "REVIEW_EXCLUDED"
    # An adjacent review date must not exclude the current date.
    other = date(2026, 10, 29)
    other_state = state.load()
    other_state["review"] = {kind: {dataset: [other.isoformat()]}}
    state.save(other_state)
    calls.clear()
    assert job.run(DAY, datetime.combine(DAY, time(18))).exit_code == 0
    assert calls == ["daily", "minute"]


def test_both_reviewed_dates_are_reported_not_success(state):
    for dataset in ("daily", "minute"):
        state.mark_review("UNKNOWN_STATUS", dataset, DAY)
    result = make_job(state).run(DAY, datetime.combine(DAY, time(18)))
    assert result.exit_code == 2
    assert result.as_dict()["run_date"] == DAY.isoformat()
    assert all(not o.ran and o.reason == "REVIEW_EXCLUDED" for o in result.outcomes)


def test_daily_build_job_wires_guard_before_ingestion(state, monkeypatch, capsys):
    state.set_stop_flag("ALL_FLAG", "AUTH_ERROR", DAY)
    monkeypatch.setattr(daily, "JQuantsCalendarStore", lambda directory: Mock(
        categories=lambda start, end: {DAY: 1},
    ))
    monkeypatch.setattr(daily, "_ingestion", forbidden)
    monkeypatch.setattr(daily, "JQuantsMetricsFailureHistory", lambda directory: Mock())
    code = daily.main(["--date", DAY.isoformat(), "--publish-after", "00:00"])
    body = json.loads(capsys.readouterr().out)
    assert code == 2 and all(o["ran"] is False for o in body["outcomes"])


@pytest.mark.parametrize("entry", ["manual", "daily"])
def test_stale_global_lock_is_not_stolen(state, entry, monkeypatch, tmp_path, capsys):
    directory = tmp_path / "state"
    directory.mkdir()
    lock = directory / LOCK_FILE
    lock.write_text('{"pid": 0, "acquired_at": "STALE"}', encoding="utf-8")
    monkeypatch.setattr(manual, "_ingestion", lambda measure: forbidden())
    monkeypatch.setattr(daily, "build_job", lambda: make_job(state))
    args = ["--dataset", "daily", "--date", DAY.isoformat()] if entry == "manual" else [
        "--date", DAY.isoformat(), "--publish-after", "00:00",
    ]
    assert (manual if entry == "manual" else daily).main(args) == 2
    assert lock.exists() and "STALE" in lock.read_text(encoding="utf-8")
    capsys.readouterr()


@pytest.mark.parametrize("payload", [
    "{SECRET", "null", '{"stop_flag": []}', '{"stop_flag": {"scope": "UNKNOWN"}}',
    '{"stop_flag": {"scope":"ALL_FLAG","reason":"SECRET value","set_on":"2026-10-30"}}',
    '{"stop_flag": {"scope":"ALL_FLAG","reason":"SECRET_VALUE","set_on":"2026-10-30"}}',
    '{"stop_flag": {"scope":"BACKFILL_FLAG","reason":"SECRET_VALUE","set_on":"2026-10-30"}}',
    '{"stop_flag": {"scope":"ALL_FLAG","reason":12345,"set_on":"2026-10-30"}}',
    '{"stop_flag": {"scope":"ALL_FLAG","reason":"AUTH_ERROR","set_on":"2026-02-30"}}',
    '{"review": null}', '{"review": {"UNKNOWN": {}}}',
    '{"review": {"PERMISSION_403": []}}',
    '{"review": {"PERMISSION_403": {"daily": "2026-10-30"}}}',
    '{"review": {"PERMISSION_403": {"daily": ["2026-02-30"]}}}',
    '{"review": {"PERMISSION_403": {"daily": ["20261030"]}}}',
    '{"review": {"PERMISSION_403": {"daily": [null]}}}',
    '{"review": {"PERMISSION_403": {"unknown": []}}}',
])
@pytest.mark.parametrize("entry", ["manual", "daily"])
def test_invalid_state_fails_without_fetch_or_leaking_payload(
    state, payload, entry, tmp_path, monkeypatch, capsys,
):
    directory = tmp_path / "state"
    directory.mkdir()
    path = directory / STATE_FILE
    path.write_text(payload, encoding="utf-8")
    monkeypatch.setattr(manual, "_ingestion", lambda measure: forbidden())
    monkeypatch.setattr(daily, "build_job", lambda: make_job(state))
    args = ["--dataset", "daily", "--date", DAY.isoformat()] if entry == "manual" else [
        "--date", DAY.isoformat(), "--publish-after", "00:00",
    ]
    assert (manual if entry == "manual" else daily).main(args) == 1
    output = capsys.readouterr()
    assert "SECRET" not in output.out + output.err
    assert path.read_text(encoding="utf-8") == payload
    assert not (directory / LOCK_FILE).exists()


def test_unreadable_state_and_lock_creation_failure_stop_before_ingestion(
    state, monkeypatch, tmp_path,
):
    directory = tmp_path / "state"
    directory.mkdir()
    (directory / STATE_FILE).mkdir()
    with pytest.raises(BackfillStateError):
        JQuantsExecutionGuard(state).run_single_day(forbidden, dataset="daily", date=DAY.isoformat())
    monkeypatch.setattr("src.infrastructure.market_data.jquants_backfill_state.os.open",
                        Mock(side_effect=PermissionError("SECRET")))
    with pytest.raises(PermissionError):
        JQuantsExecutionGuard(state).run_single_day(forbidden, dataset="daily", date=DAY.isoformat())


def test_exception_releases_shared_lock(state):
    def factory():
        raise ValueError("synthetic failure")

    with pytest.raises(ValueError):
        JQuantsExecutionGuard(state).run_single_day(factory, dataset="daily", date=DAY.isoformat())
    with state.global_lock():
        pass


def test_backfill_inner_ingestion_does_not_acquire_second_global_lock(state, tmp_path):
    plan = BackfillPlan(
        targets=(DAY,), holidays=(), completed=(), in_progress=(), attention=(),
        out_of_contract=(), holiday_with_ledger=(),
    )
    planner = Mock()
    planner.plan.return_value = plan
    calls = []

    def ingest(**kwargs):
        with pytest.raises(BackfillLockedError):
            with state.global_lock():
                pass
        calls.append(kwargs)
        return JQuantsIngestionSummary(**kwargs, status="COMPLETE")

    job = JQuantsBackfillUseCase(
        planner, Mock(run=ingest), state, raw_directory=tmp_path / "raw",
        canonical_directory=tmp_path / "canonical", today_provider=lambda: DAY,
        volume_probe=lambda directory: (500 * 1000**3, 1),
    )
    assert job.run("daily", DAY, DAY).exit_code == 0 and len(calls) == 1
    state.set_stop_flag("BACKFILL_FLAG", "DISK_FLOOR", DAY)
    assert job.run("daily", DAY, DAY).outcome.value == "BLOCKED_BY_FLAG"
    assert len(calls) == 1
