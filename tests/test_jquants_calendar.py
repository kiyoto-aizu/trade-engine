from datetime import date
from types import SimpleNamespace

import pytest

from src.application.jquants_calendar_ingestion import JQuantsCalendarIngestion
from src.application.jquants_canonical_input import BusinessCalendarPort
from src.domain.jquants_business_calendar import BusinessCalendarError, parse_rows
from src.domain.jquants_consumer_contract import business_days_from_calendar
from src.infrastructure.market_data.jquants_calendar_store import JQuantsCalendarStore


class FakeClient:
    def __init__(self, rows):
        self.rows = rows
        self.calls = []

    def iter_pages(self, endpoint, params=None):
        self.calls.append((endpoint, dict(params)))
        yield SimpleNamespace(data=tuple(self.rows[:2]))
        yield SimpleNamespace(data=tuple(self.rows[2:]))


def rows(*pairs):
    return [{"Date": d, "HolDiv": h} for d, h in pairs]


JAN = rows(("2026-01-01", "0"), ("2026-01-02", "0"), ("2026-01-03", "0"), ("2026-01-04", "3"),
           ("2026-01-05", "1"), ("2026-01-06", "2"))
START, END = date(2026, 1, 1), date(2026, 1, 6)


def test_ingest_saves_and_serves_port(tmp_path):
    store = JQuantsCalendarStore(tmp_path)
    client = FakeClient(JAN)
    summary = JQuantsCalendarIngestion(client, store).run(START, END)
    assert client.calls == [("markets/calendar", {"from": "2026-01-01", "to": "2026-01-06"})]
    assert summary.fetched == 6 and summary.by_category == {0: 3, 3: 1, 1: 1, 2: 1}
    cats = store.categories(START, END)
    assert business_days_from_calendar(cats, START, END) == {date(2026, 1, 5), date(2026, 1, 6)}


def _use_port(port: BusinessCalendarPort) -> dict:
    return dict(port.categories(START, END))


def test_store_satisfies_port(tmp_path):
    store = JQuantsCalendarStore(tmp_path)
    JQuantsCalendarIngestion(FakeClient(JAN), store).run(START, END)
    assert _use_port(store)[date(2026, 1, 5)] == 1


def test_reversed_period_fails_before_network(tmp_path):
    client = FakeClient(JAN)
    with pytest.raises(BusinessCalendarError):
        JQuantsCalendarIngestion(client, JQuantsCalendarStore(tmp_path)).run(END, START)
    assert client.calls == []


def test_same_content_does_not_rewrite(tmp_path):
    store = JQuantsCalendarStore(tmp_path)
    JQuantsCalendarIngestion(FakeClient(JAN), store).run(START, END)
    path = tmp_path / "calendar.json"
    path.touch()
    old = path.stat().st_mtime_ns
    JQuantsCalendarIngestion(FakeClient(JAN), store).run(START, END)
    assert path.stat().st_mtime_ns == old
    assert [p.name for p in tmp_path.iterdir()] == ["calendar.json"]


def test_load_rejects_non_standard_values(tmp_path):
    from src.infrastructure.market_data import jquants_calendar_store as m

    for cats in ({"20260105": 1}, {"2026-01-05": 1.0}):
        path = tmp_path / "calendar.json"
        path.write_text(
            __import__("json").dumps(
                {"format_version": 1, "sha256": m._digest(cats), "categories": cats}),
            encoding="utf-8")
        with pytest.raises(BusinessCalendarError):
            JQuantsCalendarStore(tmp_path).load()


def test_unsaved_period_is_not_covered(tmp_path):
    store = JQuantsCalendarStore(tmp_path)
    with pytest.raises(Exception, match="does not cover"):
        business_days_from_calendar(store.categories(START, END), START, END)


@pytest.mark.parametrize("bad", [
    rows(("2026-01-01", "9")), rows(("2026-01-01", "x")), rows(("2026-1-1", "0")),
    rows(("2026-01-01", None)), rows(("2026-01-01", True)),
])
def test_invalid_rows_fail(bad):
    with pytest.raises(BusinessCalendarError):
        parse_rows(bad, date(2026, 1, 1), date(2026, 1, 1))


def test_gap_duplicate_and_out_of_range_fail():
    with pytest.raises(BusinessCalendarError, match="cover"):
        parse_rows(JAN[:-1], START, END)
    with pytest.raises(BusinessCalendarError, match="duplicate"):
        parse_rows(JAN + rows(("2026-01-01", "0")), START, END)
    with pytest.raises(BusinessCalendarError, match="outside"):
        parse_rows(JAN + rows(("2026-01-07", "1")), START, END)


def test_failed_validation_keeps_store(tmp_path):
    store = JQuantsCalendarStore(tmp_path)
    JQuantsCalendarIngestion(FakeClient(JAN), store).run(START, END)
    before = store.load()
    with pytest.raises(BusinessCalendarError):
        JQuantsCalendarIngestion(FakeClient(JAN[:-1]), store).run(START, END)
    assert store.load() == before


def test_difference_requires_refresh_and_rerun_is_idempotent(tmp_path):
    store = JQuantsCalendarStore(tmp_path)
    JQuantsCalendarIngestion(FakeClient(JAN), store).run(START, END)
    assert JQuantsCalendarIngestion(FakeClient(JAN), store).run(START, END).changed == 0
    changed = [dict(r) for r in JAN]
    changed[4]["HolDiv"] = "0"
    with pytest.raises(BusinessCalendarError, match="refresh"):
        JQuantsCalendarIngestion(FakeClient(changed), store).run(START, END)
    assert store.load()[date(2026, 1, 5)] == 1
    summary = JQuantsCalendarIngestion(FakeClient(changed), store).run(START, END, refresh=True)
    assert summary.changed == 1 and store.load()[date(2026, 1, 5)] == 0


def test_other_period_is_merged(tmp_path):
    store = JQuantsCalendarStore(tmp_path)
    JQuantsCalendarIngestion(FakeClient(JAN), store).run(START, END)
    JQuantsCalendarIngestion(FakeClient(rows(("2026-02-02", "1"))), store).run(
        date(2026, 2, 2), date(2026, 2, 2))
    assert len(store.load()) == 7


def test_corrupted_file_fails(tmp_path):
    store = JQuantsCalendarStore(tmp_path)
    JQuantsCalendarIngestion(FakeClient(JAN), store).run(START, END)
    path = tmp_path / "calendar.json"
    path.write_text(path.read_text(encoding="utf-8").replace('"2026-01-05": 1', '"2026-01-05": 0'),
                    encoding="utf-8")
    with pytest.raises(BusinessCalendarError, match="hash"):
        store.load()
    path.write_text("{", encoding="utf-8")
    with pytest.raises(BusinessCalendarError, match="unreadable"):
        store.load()


def test_entrypoint_requires_allow_network(capsys):
    from src.entrypoints.ingest_jquants_calendar import main
    assert main(["--from", "2026-01-01", "--to", "2026-01-31"]) == 2
    assert "SKIPPED" in capsys.readouterr().out


def test_entrypoint_success_and_failure_output(tmp_path, monkeypatch, capsys):
    from src.entrypoints import ingest_jquants_calendar as ep

    monkeypatch.setattr(ep.config, "JQUANTS_CALENDAR_DIRECTORY", tmp_path)
    holder = {}

    class Client(FakeClient):
        def __init__(self):
            super().__init__(holder["rows"])

        def close(self):
            pass

    monkeypatch.setattr(ep, "JQuantsV2Client", Client)
    args = ["--from", "2026-01-01", "--to", "2026-01-06", "--allow-network"]
    holder["rows"] = JAN
    assert ep.main(args) == 0
    out = capsys.readouterr().out
    assert '"status": "COMPLETE"' in out and "HolDiv" not in out and "Date" not in out
    holder["rows"] = JAN[:-1]
    assert ep.main(args) == 1
    assert '"error_category": "BusinessCalendarError"' in capsys.readouterr().out
