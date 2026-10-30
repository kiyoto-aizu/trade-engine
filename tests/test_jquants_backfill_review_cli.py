import json
from datetime import date

from src.entrypoints import manage_jquants_backfill_review as entry
from src.infrastructure.market_data.jquants_backfill_state import (
    JQuantsBackfillStateStore,
)

TARGET_DATE = date(2026, 10, 28)
OPERATION_DATE = date(2026, 10, 30)
REASON_CODE = "UNKNOWN_STATUS"


def _entry_args(*args):
    return ["clear", "--dataset", "daily", "--date", TARGET_DATE.isoformat(),
            "--reason-code", REASON_CODE, *args]


def test_list_is_read_only_and_works_while_backfill_lock_is_held(
    monkeypatch, tmp_path, capsys
):
    monkeypatch.setattr(entry.config, "JQUANTS_BACKFILL_STATE_DIRECTORY", tmp_path)
    state = JQuantsBackfillStateStore(tmp_path)
    state.mark_review(REASON_CODE, "daily", TARGET_DATE)

    with state.global_lock():
        assert entry.main(["list"]) == 0

    output = json.loads(capsys.readouterr().out)
    assert output == {
        "status": "COMPLETE",
        "review": [{
            "dataset": "daily", "target_date": TARGET_DATE.isoformat(),
            "reason_code": REASON_CODE,
        }],
    }
    assert TARGET_DATE in state.review_dates("daily")


def test_clear_removes_one_item_and_records_dates_and_reason_code(
    monkeypatch, tmp_path, capsys
):
    monkeypatch.setattr(entry.config, "JQUANTS_BACKFILL_STATE_DIRECTORY", tmp_path)
    monkeypatch.setattr(entry, "jst_today", lambda: OPERATION_DATE)
    state = JQuantsBackfillStateStore(tmp_path)
    state.mark_review(REASON_CODE, "daily", TARGET_DATE)
    state.mark_review(REASON_CODE, "minute", TARGET_DATE)

    assert entry.main(_entry_args()) == 0

    output = json.loads(capsys.readouterr().out)
    assert output == {
        "status": "COMPLETE", "cleared": True, "dataset": "daily",
        "target_date": TARGET_DATE.isoformat(),
        "operation_date": OPERATION_DATE.isoformat(), "reason_code": REASON_CODE,
    }
    assert state.review_entries() == [{
        "dataset": "minute", "target_date": TARGET_DATE.isoformat(),
        "reason_code": REASON_CODE,
    }]
    assert state.load()["review_clear_audit"] == {
        "daily": [{
            "target_date": TARGET_DATE.isoformat(),
            "operation_date": OPERATION_DATE.isoformat(),
            "reason_code": REASON_CODE,
        }],
        "minute": [],
    }


def test_clear_refuses_to_change_state_when_lock_is_held(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(entry.config, "JQUANTS_BACKFILL_STATE_DIRECTORY", tmp_path)
    state = JQuantsBackfillStateStore(tmp_path)
    state.mark_review(REASON_CODE, "daily", TARGET_DATE)

    with state.global_lock():
        assert entry.main(_entry_args()) == 1

    output = json.loads(capsys.readouterr().out)
    assert output == {"status": "FAILED", "error_category": "BackfillLockedError"}
    assert TARGET_DATE in state.review_dates("daily")
    assert state.load()["review_clear_audit"]["daily"] == []


def test_missing_review_entry_is_explicit_and_not_audited(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(entry.config, "JQUANTS_BACKFILL_STATE_DIRECTORY", tmp_path)
    monkeypatch.setattr(entry, "jst_today", lambda: OPERATION_DATE)
    state = JQuantsBackfillStateStore(tmp_path)

    assert entry.main(_entry_args()) == 1

    assert json.loads(capsys.readouterr().out) == {
        "status": "FAILED", "error_category": "REVIEW_ENTRY_NOT_FOUND",
    }
    assert state.load()["review_clear_audit"]["daily"] == []


def test_failed_state_save_keeps_review_and_audit_unchanged(
    monkeypatch, tmp_path, capsys
):
    monkeypatch.setattr(entry.config, "JQUANTS_BACKFILL_STATE_DIRECTORY", tmp_path)
    monkeypatch.setattr(entry, "jst_today", lambda: OPERATION_DATE)
    state = JQuantsBackfillStateStore(tmp_path)
    state.mark_review(REASON_CODE, "daily", TARGET_DATE)
    monkeypatch.setattr(
        "os.replace", lambda *args, **kwargs: (_ for _ in ()).throw(OSError("disk")),
    )

    assert entry.main(_entry_args()) == 1

    assert json.loads(capsys.readouterr().out) == {
        "status": "FAILED", "error_category": "BackfillStateError",
    }
    assert TARGET_DATE in state.review_dates("daily")
    assert state.load()["review_clear_audit"]["daily"] == []
