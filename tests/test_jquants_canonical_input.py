import json
from datetime import date

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from src.application import jquants_canonical_input as input_module
from src.application.jquants_canonical_input import JQuantsCanonicalInputUseCase
from src.application.jquants_single_day_ingestion import JQuantsSingleDayIngestion
from src.domain.jquants_consumer_contract import (
    ConsumerContractError,
    ConsumerRequest,
    DayObservation,
    DayStatus,
    business_days_from_calendar,
    classify_day,
    judge_period,
)
from src.infrastructure.market_data import jquants_canonical_reader as reader_module
from src.infrastructure.market_data import jquants_canonical_store as canonical_module
from src.infrastructure.market_data.jquants_canonical_reader import JQuantsCanonicalReader
from src.infrastructure.market_data.jquants_canonical_store import (
    CURRENT_HISTORY_FILENAME,
    SCHEMA_VERSION,
    JQuantsCanonicalStore,
    JQuantsStaleSchemaError,
    canonical_schema,
)
from src.infrastructure.market_data.jquants_raw_store import JQuantsRawStore
from src.infrastructure.market_data.jquants_v2_client import (
    JQuantsPage, JQuantsPageMetadata, JQuantsTransportError,
)

TODAY = date(2026, 10, 25)
MON, TUE, WED = date(2026, 10, 19), date(2026, 10, 20), date(2026, 10, 21)
SAT = date(2026, 10, 24)


def _record(day: date, code="123A0", **changes):
    record = dict(Code=code, Date=day.isoformat(), O=10, H=12, L=8, C=11, Vo=0, Va=None,
                  AdjFactor=0.5, AdjO=5, AdjH=6, AdjL=4, AdjC=5.5, AdjVo=0, UL="0", LL="1")
    record.update(changes)
    return record


class _Client:
    def __init__(self, records, dataset="daily"):
        self.records = records
        self.dataset = dataset

    def close(self):
        pass

    def iter_pages(self, endpoint, params=None):
        payload = json.dumps({"data": self.records}).encode()
        yield JQuantsPage(
            tuple(self.records), payload,
            JQuantsPageMetadata(f"equities/bars/{self.dataset}", 1, len(self.records), 200, False),
        )


class _Calendar:
    def __init__(self, categories):
        self._categories = categories

    def categories(self, start, end):
        return self._categories


@pytest.fixture
def stores(tmp_path):
    return (
        JQuantsRawStore(tmp_path / "raw", tmp_path / "ledger.sqlite3"),
        JQuantsCanonicalStore(tmp_path / "canonical"),
    )


def _ingest(stores, day, *, records=None, **kwargs):
    records = records if records is not None else [_record(day)]
    return JQuantsSingleDayIngestion(
        *stores, client_factory=lambda: _Client(records)
    ).run(dataset="daily", date=day.isoformat(), **kwargs)


def _use_case(stores, categories):
    return JQuantsCanonicalInputUseCase(
        JQuantsCanonicalReader(stores[1], stores[0]), _Calendar(categories)
    )


def _load(use_case, start=MON, end=WED, **kwargs):
    params = dict(dataset="daily", schema_version=SCHEMA_VERSION, columns=["Code", "C"],
                  start=start, end=end, today=TODAY)
    params.update(kwargs)
    return use_case.load(**params)


WEEK = {MON: 1, TUE: 1, WED: 1}


# ---- domain ---------------------------------------------------------------

def _request(**changes):
    params = dict(dataset="daily", schema_version="1", columns=("C",), start=MON, end=WED,
                  today=TODAY)
    params.update(changes)
    return ConsumerRequest(**params)


@pytest.mark.parametrize("changes", [
    dict(dataset="weekly"), dict(schema_version=""), dict(columns=()),
    dict(columns=("C", "C")), dict(start=WED, end=MON),
    dict(allowed_missing_dates=frozenset({date(2026, 1, 1)})),
])
def test_request_rejects_invalid_input(changes):
    with pytest.raises(ConsumerContractError):
        _request(**changes)


def test_calendar_must_cover_the_period_and_use_known_categories():
    assert business_days_from_calendar({MON: 1, TUE: 2, WED: 0}, MON, WED) == {MON, TUE}
    assert business_days_from_calendar({MON: 3}, MON, MON) == frozenset()
    with pytest.raises(ConsumerContractError, match="2026-10-20"):
        business_days_from_calendar({MON: 1}, MON, TUE)
    with pytest.raises(ConsumerContractError):
        business_days_from_calendar({MON: None}, MON, MON)
    with pytest.raises(ConsumerContractError):
        business_days_from_calendar({MON: 9}, MON, MON)


def _classify(observation, business=True, day=MON):
    return classify_day(observation, is_business_day=business, dataset="daily", today=TODAY)


@pytest.mark.parametrize("observation,business,expected", [
    (DayObservation(MON), False, DayStatus.HOLIDAY),
    (DayObservation(MON, generation_id="g", schema_version="1"), True, DayStatus.AVAILABLE),
    (DayObservation(MON), True, DayStatus.MISSING),
    (DayObservation(MON, ledger_state="EMPTY_UNKNOWN"), True, DayStatus.MISSING),
    (DayObservation(MON, ledger_state="EMPTY_HELD"), True, DayStatus.MISSING),
    (DayObservation(MON, ledger_state="COMPLETE"), True, DayStatus.MISSING),
    (DayObservation(MON, ledger_state="FAILED"), True, DayStatus.FETCH_FAILED),
    (DayObservation(MON, ledger_state="PARTIAL"), True, DayStatus.FETCH_FAILED),
    (DayObservation(MON, attempt_state="FAILED"), True, DayStatus.FETCH_FAILED),
    (DayObservation(MON, ledger_state="RUNNING"), True, DayStatus.IN_PROGRESS),
    (DayObservation(MON, attempt_state="RUNNING"), True, DayStatus.IN_PROGRESS),
    (DayObservation(MON, verify_error="STALE_SCHEMA"), True, DayStatus.STALE_SCHEMA),
    (DayObservation(MON, verify_error="BROKEN"), True, DayStatus.BROKEN),
])
def test_classify_day(observation, business, expected):
    assert _classify(observation, business) == expected


def test_out_of_contract_is_distinct_from_missing_and_fetch_failed():
    old = date(2020, 1, 6)
    failed = DayObservation(old, ledger_state="FAILED")
    status = classify_day(failed, is_business_day=True, dataset="daily", today=TODAY)
    assert status == DayStatus.OUT_OF_CONTRACT
    assert classify_day(DayObservation(old), is_business_day=True, dataset="daily",
                        today=TODAY) == DayStatus.OUT_OF_CONTRACT


def _generation(day, version="1"):
    return DayObservation(day, generation_id=f"g{day.day}", schema_version=version,
                          transformer_version="3", raw_generation_id=f"r{day.day}")


def test_judge_period_records_generations_and_allowed_missing():
    observations = {MON: _generation(MON), TUE: DayObservation(TUE), WED: _generation(WED)}
    with pytest.raises(ConsumerContractError, match="missing days: 2026-10-20"):
        judge_period(_request(), observations, {MON, TUE, WED})
    request = _request(allowed_missing_dates=frozenset({TUE}))
    record = judge_period(request, observations, {MON, TUE, WED}).as_dict()
    assert record["missing_dates"] == ["2026-10-20"]
    assert record["data_source"] == "jquants"
    assert [g["generation_id"] for g in record["generations"]] == ["g19", "g21"]
    assert record["generations"][0] == {
        "date": "2026-10-19", "generation_id": "g19", "schema_version": "1",
        "transformer_version": "3", "raw_generation_id": "r19",
    }


@pytest.mark.parametrize("status_observation,message", [
    (DayObservation(TUE, ledger_state="FAILED"), "acquisition failed"),
    (DayObservation(TUE, attempt_state="FAILED"), "acquisition failed"),
    (DayObservation(TUE, ledger_state="RUNNING"), "in progress"),
    (DayObservation(TUE, verify_error="BROKEN"), "failed verification"),
    (DayObservation(date(2020, 1, 6), ledger_state="FAILED"), "contract plan range"),
])
def test_failures_are_not_skippable_by_allowed_missing_dates(status_observation, message):
    day = status_observation.date
    observations = {MON: _generation(MON), day: status_observation}
    request = _request(allowed_missing_dates=frozenset({day}) & {MON, TUE, WED})
    with pytest.raises(ConsumerContractError, match=message):
        judge_period(request, observations, set(observations))


def test_stale_and_mixed_versions_have_separate_messages():
    stale = {MON: _generation(MON, "2"), TUE: DayObservation(TUE, verify_error="STALE_SCHEMA",
                                                            schema_version="1")}
    with pytest.raises(ConsumerContractError, match="mixed"):
        judge_period(_request(schema_version="2"), stale, {MON, TUE})
    only_stale = {TUE: DayObservation(TUE, verify_error="STALE_SCHEMA", schema_version="1")}
    with pytest.raises(ConsumerContractError, match="rebuild required"):
        judge_period(_request(schema_version="2"), only_stale, {TUE})
    mixed_available = {MON: _generation(MON, "1"), TUE: _generation(TUE, "2")}
    with pytest.raises(ConsumerContractError, match="mixed"):
        judge_period(_request(), mixed_available, {MON, TUE})


# ---- reader + use case with real stores -----------------------------------

def test_loads_current_generation_with_record_and_selected_columns(stores):
    for day in (MON, TUE, WED):
        assert _ingest(stores, day).status == "COMPLETE"
    result = _load(_use_case(stores, WEEK))
    assert result.record.missing_dates == ()
    assert result.record.data_source == "jquants"
    assert result.record.columns == ("Code", "C")
    assert [ref.date for ref in result.record.generations] == [MON, TUE, WED]
    manifest = stores[1].current_manifest("daily", MON.isoformat())
    first = result.record.generations[0]
    assert (first.generation_id, first.raw_generation_id) == (
        manifest["generation_id"], manifest["raw_generation_id"])
    assert (first.schema_version, first.transformer_version) == (
        manifest["schema_version"], manifest["transformer_version"])
    assert result.tables[MON].column_names == ["Code", "C"]
    assert result.tables[MON].to_pylist() == [{"Code": "123A0", "C": 11.0}]


def test_holiday_is_not_missing_but_business_day_without_data_fails(stores):
    _ingest(stores, MON)
    _ingest(stores, WED)
    use_case = _use_case(stores, {MON: 1, TUE: 0, WED: 1})
    assert _load(use_case).record.missing_dates == ()
    with pytest.raises(ConsumerContractError, match="missing days: 2026-10-20"):
        _load(_use_case(stores, WEEK))
    kept = _load(_use_case(stores, WEEK), allowed_missing_dates=[TUE])
    assert kept.record.missing_dates == (TUE,)
    assert TUE not in kept.tables


def test_calendar_gap_and_wrong_version_fail(stores):
    _ingest(stores, MON)
    with pytest.raises(ConsumerContractError, match="does not cover"):
        _load(_use_case(stores, {MON: 1}), end=TUE)
    with pytest.raises(ConsumerContractError, match="schema_version"):
        _load(_use_case(stores, {MON: 1}), end=MON, schema_version="999")
    with pytest.raises(ConsumerContractError, match="unknown columns"):
        _load(_use_case(stores, {MON: 1}), end=MON, columns=["Nope"])


def test_failed_acquisition_is_not_a_missing_day_even_with_allowed_dates(stores):
    _ingest(stores, MON)
    class FailingClient(_Client):
        def iter_pages(self, endpoint, params=None):
            raise JQuantsTransportError("offline")
            yield

    failing = JQuantsSingleDayIngestion(*stores, client_factory=lambda: FailingClient([]))
    assert failing.run(dataset="daily", date=TUE.isoformat()).status == "FAILED"
    observation = JQuantsCanonicalReader(stores[1], stores[0]).observe("daily", TUE.isoformat())
    assert observation.generation_id is None
    assert "FAILED" in (observation.ledger_state, observation.attempt_state)
    with pytest.raises(ConsumerContractError, match="acquisition failed"):
        _load(_use_case(stores, {MON: 1, TUE: 1}), end=TUE, allowed_missing_dates=[TUE])


def test_unpublished_attempt_state_is_not_read_as_data(stores):
    partition = stores[1].directory / "daily" / f"date={TUE.isoformat()}"
    partition.mkdir(parents=True)
    (partition / "ATTEMPT.json").write_text(json.dumps({"state": "RUNNING"}), encoding="ascii")
    (partition / "staging" / "x").mkdir(parents=True)
    observation = JQuantsCanonicalReader(stores[1]).observe("daily", TUE.isoformat())
    assert observation.generation_id is None and observation.attempt_state == "RUNNING"
    with pytest.raises(ConsumerContractError, match="in progress"):
        _load(_use_case(stores, {TUE: 1}), start=TUE, end=TUE)


def test_broken_data_fails_instead_of_becoming_a_missing_day(stores):
    _ingest(stores, MON)
    generation = stores[1].current_manifest("daily", MON.isoformat())["generation_id"]
    parquet = stores[1].parquet_path("daily", MON.isoformat(), generation)
    parquet.write_bytes(parquet.read_bytes() + b"x")
    with pytest.raises(ConsumerContractError, match="failed verification"):
        _load(_use_case(stores, {MON: 1}), end=MON, allowed_missing_dates=[MON])


def test_reading_does_not_modify_the_canonical_store(stores):
    _ingest(stores, MON)

    def snapshot():
        return {
            str(path): path.stat().st_mtime_ns
            for path in stores[1].directory.rglob("*") if path.is_file()
        }

    before = snapshot()
    _load(_use_case(stores, {MON: 1}), end=MON)
    assert snapshot() == before


# ---- rebuild with a new schema version ------------------------------------

def _use_new_schema(monkeypatch):
    def extended(dataset):
        schema = original(dataset)
        return schema.append(pa.field("NewColumn", pa.float64()))

    original = canonical_schema
    for module in (canonical_module, reader_module):
        monkeypatch.setattr(module, "canonical_schema", extended)
    monkeypatch.setattr(canonical_module, "SCHEMA_VERSION", "2")
    monkeypatch.setattr(input_module, "SCHEMA_VERSION", "2")


def test_old_version_is_reported_as_rebuild_required_not_as_broken(stores, monkeypatch):
    _ingest(stores, MON)
    _use_new_schema(monkeypatch)
    with pytest.raises(JQuantsStaleSchemaError):
        stores[1].current_manifest("daily", MON.isoformat())
    observation = JQuantsCanonicalReader(stores[1]).observe("daily", MON.isoformat())
    assert observation.verify_error == "STALE_SCHEMA" and observation.schema_version == "1"
    with pytest.raises(ConsumerContractError, match="rebuild required"):
        _load(_use_case(stores, {MON: 1}), end=MON, schema_version="2")


def test_rebuilt_generation_keeps_existing_columns_identical(stores, monkeypatch):
    records = [_record(MON), _record(MON, code="99990", Vo=None, Va=0)]
    _ingest(stores, MON, records=records)
    old_manifest = stores[1].current_manifest("daily", MON.isoformat())
    old_path = stores[1].parquet_path("daily", MON.isoformat(), old_manifest["generation_id"])
    old_table = pq.read_table(old_path)
    old_bytes = old_path.read_bytes()

    _use_new_schema(monkeypatch)
    summary = _ingest(stores, MON, records=records, canonical_only=True)
    assert summary.status == "COMPLETE"
    new_manifest = stores[1].current_manifest("daily", MON.isoformat())
    assert new_manifest["generation_id"] != old_manifest["generation_id"]
    assert new_manifest["schema_version"] == "2"
    assert new_manifest["raw_generation_id"] == old_manifest["raw_generation_id"]
    new_table = pq.read_table(
        stores[1].parquet_path("daily", MON.isoformat(), new_manifest["generation_id"])
    )

    assert new_table.column_names == old_table.column_names + ["NewColumn"]
    for name in old_table.column_names:
        assert new_table.schema.field(name) == old_table.schema.field(name)
        assert new_table.column(name).equals(old_table.column(name))
    assert new_table.column("NewColumn").null_count == new_table.num_rows

    # 旧世代は不変のまま残り、旧CURRENTは履歴に残る
    assert old_path.read_bytes() == old_bytes
    history = (stores[1].directory / "daily" / f"date={MON.isoformat()}"
               / CURRENT_HISTORY_FILENAME).read_text(encoding="ascii").splitlines()
    assert [json.loads(line)["previous_generation_id"] for line in history] == [
        old_manifest["generation_id"]]
    assert json.loads(history[0])["new_generation_id"] == new_manifest["generation_id"]

    loaded = _load(_use_case(stores, {MON: 1}), end=MON, schema_version="2",
                   columns=["Code", "C", "NewColumn"])
    assert loaded.record.generations[0].schema_version == "2"


def test_current_history_is_written_only_when_current_changes(stores):
    _ingest(stores, MON)
    history = stores[1].directory / "daily" / f"date={MON.isoformat()}" / CURRENT_HISTORY_FILENAME
    assert not history.exists()
    _ingest(stores, MON, canonical_only=True)
    assert not history.exists()


def test_failed_rebuild_keeps_the_previous_current(stores, monkeypatch):
    _ingest(stores, MON)
    old = stores[1].current_manifest("daily", MON.isoformat())["generation_id"]
    _use_new_schema(monkeypatch)
    def boom(*args, **kwargs):
        raise canonical_module.JQuantsCanonicalError("conversion failed")

    monkeypatch.setattr(stores[1], "transform", boom)
    assert _ingest(stores, MON, canonical_only=True).status == "FAILED"
    current = (stores[1].directory / "daily" / f"date={MON.isoformat()}" / "CURRENT").read_text()
    assert current.strip() == old
