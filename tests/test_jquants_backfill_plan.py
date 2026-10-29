import json
from datetime import date

import pytest

from src.application.jquants_backfill_planner import JQuantsBackfillPlanner
from src.domain.jquants_backfill_plan import plan_backfill
from src.domain.jquants_consumer_contract import ConsumerContractError

TODAY = date(2026, 10, 29)
START, END = date(2026, 1, 1), date(2026, 1, 7)
CATS = {
    date(2026, 1, 1): 0, date(2026, 1, 2): 0, date(2026, 1, 3): 0, date(2026, 1, 4): 3,
    date(2026, 1, 5): 1, date(2026, 1, 6): 2, date(2026, 1, 7): 1,
}


def d(n):
    return date(2026, 1, n)


def test_holidays_are_excluded_from_targets():
    plan = plan_backfill("daily", START, END, TODAY, CATS, lambda day: None)
    assert plan.holidays == (d(1), d(2), d(3), d(4))
    assert plan.targets == (d(5), d(6), d(7))
    assert plan.holiday_with_ledger == ()


def test_states_are_classified():
    states = {d(5): "COMPLETE", d(6): "RUNNING", d(7): "EMPTY_UNKNOWN"}
    plan = plan_backfill("daily", START, END, TODAY, CATS, states.get)
    assert (plan.completed, plan.in_progress, plan.attention, plan.targets) == (
        (d(5),), (d(6),), (d(7),), ())
    for state in ("FAILED", "PARTIAL", "PENDING", None):
        again = plan_backfill("daily", START, END, TODAY, CATS, {d(5): state}.get)
        assert d(5) in again.targets


def test_holiday_with_ledger_record_warns_without_change():
    plan = plan_backfill("daily", START, END, TODAY, CATS, {d(2): "COMPLETE"}.get)
    assert plan.holiday_with_ledger == (d(2),) and d(2) in plan.holidays


def test_uncovered_calendar_fails_instead_of_assuming():
    partial = {k: v for k, v in CATS.items() if k != d(5)}
    with pytest.raises(ConsumerContractError, match="2026-01-05"):
        plan_backfill("daily", START, END, TODAY, partial, lambda day: None)
    with pytest.raises(ConsumerContractError):
        plan_backfill("daily", START, END, TODAY, {}, lambda day: None)


def test_out_of_contract_dates_are_separated():
    start, end = date(2021, 1, 4), date(2021, 1, 4)
    plan = plan_backfill("daily", start, end, TODAY, {start: 1}, lambda day: None)
    assert plan.out_of_contract == (start,) and plan.targets == ()


def test_category_3_is_holiday_and_idempotent():
    first = plan_backfill("daily", START, END, TODAY, CATS, lambda day: None)
    second = plan_backfill("daily", START, END, TODAY, CATS, lambda day: None)
    assert d(4) in first.holidays and first == second


def test_reversed_period_fails():
    with pytest.raises(ValueError):
        plan_backfill("daily", END, START, TODAY, CATS, lambda day: None)


class _Calendar:
    def categories(self, start, end):
        return {k: v for k, v in CATS.items() if start <= k <= end}


class _Ledger:
    def state(self, dataset, target_date):
        return "COMPLETE" if target_date == d(5) else None


def test_planner_uses_ports_and_output_has_dates_only():
    plan = JQuantsBackfillPlanner(_Calendar(), _Ledger()).plan("daily", START, END, TODAY)
    assert plan.completed == (d(5),) and plan.targets == (d(6), d(7))
    text = json.dumps(plan.as_dict())
    assert set(plan.as_dict()) == {
        "targets", "holidays", "completed", "in_progress", "attention",
        "out_of_contract", "holiday_with_ledger"}
    assert all(len(v) == 10 for values in plan.as_dict().values() for v in values)
    assert text.count("-") == 2 * sum(len(v) for v in plan.as_dict().values())


def test_every_day_lands_in_exactly_one_bucket():
    states = {d(5): "COMPLETE", d(6): "EMPTY_HELD"}
    plan = plan_backfill("daily", START, END, TODAY, CATS, states.get)
    buckets = [v for k, v in plan.as_dict().items() if k != "holiday_with_ledger"]
    assert sum(len(v) for v in buckets) == (END - START).days + 1


def test_unknown_category_is_not_dropped_silently(monkeypatch):
    import src.domain.jquants_backfill_plan as module
    monkeypatch.setattr(module, "business_days_from_calendar", lambda c, s, e: set())
    with pytest.raises(ValueError):
        plan_backfill("daily", START, END, TODAY, {**CATS, d(5): 9}, lambda day: None)


def test_minute_contract_boundary():
    old, recent = date(2024, 1, 4), date(2026, 10, 28)
    cats = {old: 1, recent: 1}
    plan = plan_backfill("minute", old, old, TODAY, cats, lambda day: None)
    assert plan.out_of_contract == (old,)
    plan = plan_backfill("minute", recent, recent, TODAY, cats, lambda day: None)
    assert plan.targets == (recent,)


def test_entrypoint_outputs_plan_and_failure(tmp_path, monkeypatch, capsys):
    from src.entrypoints import list_jquants_backfill as ep
    from src.infrastructure.market_data.jquants_calendar_store import JQuantsCalendarStore

    class FakeRaw:
        def get_state_for(self, **kwargs):
            return None

    monkeypatch.setattr(ep.config, "JQUANTS_CALENDAR_DIRECTORY", tmp_path)
    monkeypatch.setattr(ep, "JQuantsRawStore", FakeRaw)
    args = ["--dataset", "daily", "--from", "2026-01-05", "--to", "2026-01-07"]
    assert ep.main(args) == 1
    assert '"status": "FAILED"' in capsys.readouterr().out
    JQuantsCalendarStore(tmp_path).save({d(5): 1, d(6): 0, d(7): 2})
    assert ep.main(args) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["targets"] == ["2026-01-05", "2026-01-07"] and out["holidays"] == ["2026-01-06"]
