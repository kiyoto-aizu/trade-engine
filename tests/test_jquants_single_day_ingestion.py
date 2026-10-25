import json
import os
import threading
from contextlib import contextmanager
from dataclasses import replace
from datetime import date, timedelta
from types import SimpleNamespace

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from src.application.jquants_single_day_ingestion import JQuantsSingleDayIngestion
from src.entrypoints import ingest_jquants_single_day as cli
from src.infrastructure.market_data import jquants_canonical_store as canonical_module
from src.infrastructure.market_data.jquants_canonical_store import (
    JQuantsCanonicalStore, canonical_schema,
)
from src.infrastructure.market_data.jquants_raw_store import JQuantsRawStore
from src.infrastructure.market_data.jquants_raw_store import JQuantsRawValidationError
from src.infrastructure.market_data.jquants_v2_client import (
    JQuantsPage, JQuantsPageMetadata, JQuantsRateLimitAbortError, JQuantsTransportError,
)

DAY = "2026-10-20"


def row(dataset="daily", code="123A0", **changes):
    result = dict(Code=code, Date=DAY, O=10, H=12, L=8, C=11, Vo=0, Va=None)
    if dataset == "daily":
        result.update(AdjFactor=0.5, AdjO=5, AdjH=6, AdjL=4, AdjC=5.5,
                      AdjVo=0, UL="0", LL="1")
    else:
        result["Time"] = "09:00"
    result.update(changes)
    return result


class Client:
    def __init__(self, pages, dataset="daily"):
        self.pages = pages
        self.dataset = dataset
        self.calls = []
        self.closed = False

    def close(self):
        self.closed = True

    def iter_pages(self, endpoint, params=None):
        self.calls.append((endpoint, dict(params or {})))
        for number, records in enumerate(self.pages, 1):
            payload = {"data": records, "pagination_key": "SECRET_PAGE_KEY"}
            yield JQuantsPage(
                tuple(records), json.dumps(payload).encode(),
                JQuantsPageMetadata(f"equities/bars/{self.dataset}", number,
                                    len(records), 200,
                                    number < len(self.pages) if isinstance(self.pages, list) else True),
            )


@pytest.fixture
def stores(tmp_path):
    return (
        JQuantsRawStore(tmp_path / "raw", tmp_path / "ledger.sqlite3"),
        JQuantsCanonicalStore(tmp_path / "canonical"),
    )


def run(stores, client=None, **kwargs):
    def factory():
        if client is None:
            pytest.fail("Network/client construction forbidden when reusing raw")
        return client
    return JQuantsSingleDayIngestion(
        *stores, client_factory=factory
    ).run(dataset=kwargs.pop("dataset", "daily"), date=DAY, **kwargs)


def table(stores, dataset="daily"):
    canonical = stores[1]
    manifest = canonical.current_manifest(dataset, DAY)
    path = (canonical.directory / dataset / f"date={DAY}" / "generations"
            / manifest["generation_id"] / "part-00000.parquet")
    return pq.ParquetFile(path).read()


@pytest.mark.parametrize("dataset", ["daily", "minute"])
def test_all_symbol_pages_fields_schema_and_provenance(stores, dataset):
    records = [row(dataset), row(dataset, code="99990", Vo=None, Va=0)]
    client = Client([[records[0]], [records[1]]], dataset)
    result = run(stores, client, dataset=dataset)
    assert result.status == "COMPLETE"
    assert result.exit_code == 0
    assert client.calls == [(f"equities/bars/{dataset}", {"date": DAY})]
    assert client.closed
    assert result.raw_source == "fetch" and result.raw_updated
    assert (result.page_count, result.row_count, result.symbol_count) == (2, 2, 2)
    assert result.raw_size_bytes > 0 and result.canonical_size_bytes > 0
    output = table(stores, dataset)
    assert output.schema == canonical_schema(dataset)
    rows = output.to_pylist()
    assert rows[0]["Code"] == "123A0" and rows[1]["Code"] == "99990"
    assert rows[0]["Date"] == date(2026, 10, 20)
    for index, source in enumerate(records):
        for name, value in source.items():
            if name not in {"Date", "Time"}:
                expected = int(value) if name in {"UL", "LL"} and value is not None else value
                assert rows[index][name] == expected
    if dataset == "minute":
        assert rows[0]["Time"].utcoffset() == timedelta(hours=9)
        assert rows[0]["Time"].strftime("%H:%M") == "09:00"
    manifest = stores[1].current_manifest(dataset, DAY)
    assert manifest["raw_generation_id"] == result.raw_generation_id
    assert manifest["raw_content_sha256"] == result.raw_generation_id
    assert manifest["quality"]["input_rows"] == 2
    assert set(result.durations_seconds) == {"acquisition", "verification", "conversion", "total"}
    assert all(value >= 0 for value in result.durations_seconds.values())


@pytest.mark.parametrize("dataset", ["daily", "minute"])
def test_all_null_numeric_columns_have_fixed_types(stores, dataset):
    record = row(dataset)
    for name in canonical_schema(dataset).names:
        if name not in {"Code", "Date", "Time", "UL", "LL"}:
            record[name] = None
    result = run(stores, Client([[record]], dataset), dataset=dataset)
    assert result.status == "COMPLETE"
    output = table(stores, dataset)
    assert output.schema == canonical_schema(dataset)
    assert output.column("O").type == pa.float64()
    assert output.column("O").null_count == 1


def test_minute_hhmm_accepts_close_and_does_not_assume_input_order(stores):
    records = [
        row("minute", Time="15:30"),
        row("minute", Time="09:01"),
        row("minute", Time="09:00"),
    ]
    result = run(stores, Client([records], "minute"), dataset="minute")
    assert result.status == "COMPLETE"
    output = table(stores, "minute").to_pylist()
    assert [item["Time"].strftime("%H:%M") for item in output] == [
        "15:30", "09:01", "09:00"
    ]
    assert all(item["Time"].utcoffset() == timedelta(hours=9) for item in output)


def test_daily_string_flags_are_flags_not_prices(stores):
    result = run(stores, Client([[row(UL="1", LL="0")]]))
    assert result.status == "COMPLETE"
    output = table(stores).to_pylist()[0]
    assert (output["UL"], output["LL"]) == (1, 0)
    assert output["UL"] != output["H"] and output["LL"] != output["L"]


def test_normal_daily_all_null_record_is_not_a_quality_failure(stores):
    record = row(UL="0", LL="1")
    for name in canonical_schema("daily").names:
        if name not in {"Code", "Date", "UL", "LL"}:
            record[name] = None
    result = run(stores, Client([[record]]))
    assert result.status == "COMPLETE"
    output = table(stores).to_pylist()[0]
    assert all(output[name] is None for name in (
        "O", "H", "L", "C", "Vo", "Va",
        "AdjFactor", "AdjO", "AdjH", "AdjL", "AdjC", "AdjVo",
    ))
    assert (output["UL"], output["LL"]) == (0, 1)


def test_daily_without_light_session_fields_and_unknown_code_is_kept(stores):
    record = row(code="99990")
    result = run(stores, Client([[record]]))
    assert result.status == "COMPLETE"
    assert table(stores).column("Code").to_pylist() == ["99990"]


def test_unknown_fields_preserved_and_reported_without_leaking(stores):
    result = run(stores, Client([[row(FutureField="SECRET_VALUE", ExRT=None, MktCap=123.4)]]))
    assert result.status == "ATTENTION"
    assert result.quality["unknown_field_count"] == 1
    assert result.quality["rows_with_unknown_fields"] == 1
    assert "SECRET_VALUE" not in json.dumps(result.as_dict())
    assert result.execution_metrics["field_observations"] == {
        "ExRT": {"count": 1, "null_count": 1},
        "FutureField": {"count": 1, "null_count": 0},
        "MktCap": {"count": 1, "null_count": 0},
    }
    raw = stores[0].resolve_published(
        kind="daily", endpoint="equities/bars/daily", conditions={"date": DAY}
    )
    raw_row = next(raw.iter_pages())[0]
    assert raw_row["FutureField"] == "SECRET_VALUE"
    assert raw_row["ExRT"] is None and raw_row["MktCap"] == 123.4


def test_confirmed_noncanonical_fields_are_recorded_without_hold(stores):
    source = row(ExRT=None, MktCap=100.5)
    result = run(stores, Client([[source]]))

    assert result.status == "COMPLETE"
    assert result.quality["unknown_field_count"] == 0
    assert result.execution_metrics["field_observations"] == {
        "ExRT": {"count": 1, "null_count": 1},
        "MktCap": {"count": 1, "null_count": 0},
    }
    assert "ExRT" not in table(stores).schema.names
    assert "MktCap" not in table(stores).schema.names
    raw = stores[0].resolve_published(
        kind="daily", endpoint="equities/bars/daily", conditions={"date": DAY}
    )
    assert next(raw.iter_pages())[0] == source


def test_execution_metrics_are_saved_without_stock_records_or_secrets(stores, tmp_path):
    metrics_directory = tmp_path / "metrics"
    usecase = JQuantsSingleDayIngestion(
        *stores,
        client_factory=lambda: Client([[row(code="88880", MktCap=None)]]),
        metrics_directory=metrics_directory,
    )
    result = usecase.run(dataset="daily", date=DAY)

    assert result.status == "COMPLETE"
    metrics_path = metrics_directory / "daily" / f"date={DAY}"
    saved_files = list(metrics_path.glob("*.json"))
    assert len(saved_files) == 1
    content = saved_files[0].read_text(encoding="utf-8")
    metrics = json.loads(content)
    assert metrics["request_count"] == "未計測"
    assert metrics["page_count"] == 1
    assert metrics["field_observations"]["MktCap"] == {"count": 1, "null_count": 1}
    assert metrics["peak_memory_bytes"] == "未計測"
    assert "88880" not in content
    assert "SECRET_VALUE" not in content


def test_peak_memory_is_measured_only_when_opted_in(stores):
    usecase = JQuantsSingleDayIngestion(
        *stores, client_factory=lambda: Client([[row()]]), measure_peak_memory=True,
    )
    result = usecase.run(dataset="daily", date=DAY)
    assert isinstance(result.execution_metrics["peak_memory_bytes"], int)


def test_execution_metrics_include_http_client_counters(stores):
    client = Client([[row()]])
    client.metrics = type("Metrics", (), {
        "as_dict": lambda self: {
            "request_count": 2,
            "page_count": 1,
            "status_counts": {"200": 1, "429": 1},
            "retry_count": 1,
            "rate_limit_429_count": 1,
            "wait_seconds": 120.2,
        }
    })()
    result = run(stores, client)

    assert result.execution_metrics["request_count"] == 2
    assert result.execution_metrics["request_page_count"] == 1
    assert result.execution_metrics["http_status_counts"] == {"200": 1, "429": 1}
    assert result.execution_metrics["retry_count"] == 1
    assert result.execution_metrics["rate_limit_429_count"] == 1
    assert result.execution_metrics["wait_seconds"] == 120.2


def test_real_metrics_json_is_summarized_with_actual_field_names(stores, tmp_path):
    from src.application.jquants_metrics_summary import summarize_directory

    client = Client([[row(code="88880")]])
    client.metrics = type("Metrics", (), {
        "as_dict": lambda self: {
            "request_count": 2, "page_count": 1, "status_counts": {"200": 1, "429": 1},
            "retry_count": 1, "rate_limit_429_count": 1, "wait_seconds": 1.5,
        }
    })()
    metrics_directory = tmp_path / "metrics"
    JQuantsSingleDayIngestion(
        *stores, client_factory=lambda: client, metrics_directory=metrics_directory
    ).run(dataset="daily", date=DAY)

    (summary,) = summarize_directory(metrics_directory)
    assert summary["dataset"] == "daily" and summary["date"] == DAY
    assert summary["status"] == "COMPLETE"
    assert summary["request_count"] == 2 and summary["rate_limit_429_count"] == 1
    assert summary["partial_verdict"] == "Hold"
    assert "RATE_LIMIT_429_SEEN" in summary["reasons"]
    assert "88880" not in json.dumps(summary)


def test_daily_minute_canonical_comparison_is_informational(stores):
    daily_rows = [
        row(code="123A0", Vo=10),
        row(code="22220", Vo=1),
        row(code="99990", Vo=0, O=None, H=None, L=None, C=None),
    ]
    run(stores, Client([daily_rows]))
    minute_rows = [
        row("minute", code="22220", Time="09:00"),
        row("minute", code="99990", Time="09:00"),
        row("minute", code="55550", Time="09:00"),
    ]
    result = run(stores, Client([minute_rows], "minute"), dataset="minute")

    comparison = result.execution_metrics["canonical_comparison"]
    assert comparison == {
        "status": "AVAILABLE",
        "daily_positive_volume_symbol_count": 2,
        "minute_symbol_count": 3,
        "daily_only_symbol_count": 1,
        "minute_only_symbol_count": 2,
        "daily_only_ohlc_all_null_count": 0,
        "daily_only_ohlc_not_all_null_count": 1,
        "minute_only_daily_ohlc_all_null_count": 1,
        "minute_only_daily_ohlc_not_all_null_count": 0,
        "minute_only_daily_record_missing_count": 1,
        "publication_gate": False,
    }


def test_empty_result_and_empty_held_are_distinct_from_failure(stores):
    empty = run(stores, Client([[]]))
    assert empty.status == "ATTENTION" and empty.exit_code == 2
    assert empty.raw_state == "EMPTY_UNKNOWN" and empty.row_count == 0
    assert table(stores).schema == canonical_schema("daily")
    complete = run(stores, Client([[row()]]), refresh=True)
    held = run(stores, Client([[]]), refresh=True)
    assert held.status == "ATTENTION" and held.raw_state == "EMPTY_HELD"
    assert held.using_previous_raw and not held.raw_updated
    assert held.raw_generation_id == complete.raw_generation_id
    assert held.canonical_generation_id == complete.canonical_generation_id
    assert held.row_count == 1 and held.canonical_reused
    assert [item.state.value for item in stores[0].list_attention_datasets()] == ["EMPTY_HELD"]
    assert stores[0].list_incomplete_datasets() == []
    reused = run(stores, canonical_only=True)
    assert reused.raw_state == "EMPTY_HELD" and reused.status == "ATTENTION"


def test_initial_empty_is_in_attention_list_only(stores):
    run(stores, Client([[]]))
    assert len(stores[0].list_attention_datasets()) == 1
    assert stores[0].list_incomplete_datasets() == []


def failing_pages(first_page=False):
    if first_page:
        yield [row(code="22220")]
    raise JQuantsTransportError("SECRET_EXCEPTION_BODY")


def aborting_pages():
    yield [row(code="22220")]
    raise JQuantsRateLimitAbortError("cap reached")


def test_429_abort_is_failed_partial_keeps_previous_and_recovers_with_refresh(stores, tmp_path):
    old = run(stores, Client([[row()]]))
    metrics_dir = tmp_path / "metrics"
    aborted = JQuantsSingleDayIngestion(
        *stores, client_factory=lambda: Client(aborting_pages()), metrics_directory=metrics_dir
    ).run(dataset="daily", date=DAY, refresh=True)
    assert aborted.status == "FAILED" and aborted.exit_code == 3
    assert aborted.raw_state == "PARTIAL"
    assert aborted.error_category == "RATE_LIMIT_ABORTED"
    assert aborted.execution_metrics["rate_limit_aborted"] is True
    assert aborted.execution_metrics["abort_reason"] == "RATE_LIMIT_429_CAP"
    assert stores[1].current_manifest("daily", DAY)["generation_id"] == old.canonical_generation_id
    assert aborted.raw_generation_id == old.raw_generation_id
    assert len(stores[0].list_incomplete_datasets()) == 1

    saved = json.loads(next(metrics_dir.rglob("*.json")).read_text(encoding="utf-8"))
    assert saved["rate_limit_aborted"] is True and saved["status"] == "FAILED"
    from src.application.jquants_metrics_evaluation import evaluate_metrics
    evaluation = evaluate_metrics(saved)
    assert evaluation.verdict == "No-Go" and "RATE_LIMIT_ABORTED" in evaluation.reasons

    recovered = run(stores, Client([[row(code="22220")]]), refresh=True)
    assert recovered.status == "COMPLETE" and recovered.exit_code == 0
    assert stores[0].list_incomplete_datasets() == []


def test_429_abort_sends_safe_slack_critical_notification(stores):
    sent = []
    client = Client(aborting_pages())
    client.metrics = SimpleNamespace(as_dict=lambda: {
        "request_count": 3, "page_count": 1, "status_counts": {"200": 1, "429": 3},
        "retry_count": 2, "rate_limit_429_count": 3, "wait_seconds": 240.0,
    })
    result = JQuantsSingleDayIngestion(
        *stores, client_factory=lambda: client, notifier=sent.append
    ).run(dataset="daily", date=DAY, refresh=True)
    assert result.exit_code == 3 and len(sent) == 1
    assert DAY in sent[0] and "daily" in sent[0]
    assert "22220" not in sent[0] and "SECRET" not in sent[0]
    assert "429回数=3" in sent[0]


def test_slack_false_return_adds_warning_without_changing_result(stores):
    result = JQuantsSingleDayIngestion(
        *stores, client_factory=lambda: Client(aborting_pages()), notifier=lambda _m: False
    ).run(dataset="daily", date=DAY, refresh=True)
    assert result.status == "FAILED" and result.exit_code == 3
    assert "SLACK_NOTIFY_FAILED" in result.warnings


def test_slack_failure_does_not_change_abort_result(stores):
    def broken(_message):
        raise RuntimeError("SECRET_WEBHOOK")
    result = JQuantsSingleDayIngestion(
        *stores, client_factory=lambda: Client(aborting_pages()), notifier=broken
    ).run(dataset="daily", date=DAY, refresh=True)
    assert result.status == "FAILED" and result.exit_code == 3
    assert "SLACK_NOTIFY_FAILED" in result.warnings
    assert "SECRET_WEBHOOK" not in json.dumps(result.as_dict())


def test_non_429_failure_does_not_notify(stores):
    sent = []
    JQuantsSingleDayIngestion(
        *stores, client_factory=lambda: Client(failing_pages()), notifier=sent.append
    ).run(dataset="daily", date=DAY, refresh=True)
    assert sent == []


def test_cli_429_abort_exit_code_and_alert(stores, monkeypatch, capsys, tmp_path):
    sent = []
    monkeypatch.setattr(cli, "notify_critical", lambda message: sent.append(message) or True)
    monkeypatch.setattr(cli, "JQuantsRawStore", lambda: stores[0])
    monkeypatch.setattr(cli, "JQuantsCanonicalStore", lambda path: stores[1])
    monkeypatch.setattr(cli.config, "JQUANTS_EXECUTION_METRICS_DIRECTORY", tmp_path / "metrics")
    original = cli.JQuantsSingleDayIngestion
    monkeypatch.setattr(cli, "JQuantsSingleDayIngestion",
                        lambda raw, canonical, **kwargs: original(
                            raw, canonical, client_factory=lambda: Client(aborting_pages()), **kwargs
                        ))
    code = cli.main(["--dataset", "daily", "--date", DAY, "--refresh"])
    output = capsys.readouterr()
    assert code == 3
    assert "ALERT" in output.err and "22220" not in output.out + output.err
    assert len(sent) == 1
    assert json.loads(output.out)["error_category"] == "RATE_LIMIT_ABORTED"


@pytest.mark.parametrize("first_page", [False, True])
@pytest.mark.parametrize("previous", [False, True])
def test_transport_failure_never_publishes_canonical_and_retains_previous(stores, first_page, previous):
    old = run(stores, Client([[row()]])) if previous else None
    failure = run(stores, Client(failing_pages(first_page)), refresh=True)
    assert failure.status == "FAILED" and failure.exit_code == 1
    assert failure.raw_state == ("PARTIAL" if first_page else "FAILED")
    assert failure.error_category == "TRANSPORT_ERROR"
    assert failure.using_previous_raw == previous
    assert "SECRET_EXCEPTION_BODY" not in json.dumps(failure.as_dict())
    manifest = stores[1].current_manifest("daily", DAY)
    if old:
        assert manifest["generation_id"] == old.canonical_generation_id
        assert failure.raw_generation_id == old.raw_generation_id
        assert "FETCH_FAILED_PREVIOUS_RAW_RETAINED" in failure.warnings
        reused = run(stores, canonical_only=True)
        assert reused.status == "ATTENTION" and reused.using_previous_raw
    else:
        assert manifest is None
    assert len(stores[0].list_incomplete_datasets()) == 1
    assert not stores[0].list_attention_datasets()


def test_transform_failure_retries_without_network(stores, monkeypatch):
    original = stores[1]._convert
    def fail(*args, **kwargs):
        raise OSError("SECRET_STORAGE_ERROR")
    monkeypatch.setattr(stores[1], "_convert", fail)
    failed = run(stores, Client([[row()]]))
    assert failed.status == "FAILED" and failed.raw_state == "COMPLETE"
    assert stores[1].current_manifest("daily", DAY) is None
    attempt_path = stores[1].directory / "daily" / f"date={DAY}" / "ATTEMPT.json"
    assert json.loads(attempt_path.read_text())["state"] == "FAILED"
    monkeypatch.setattr(stores[1], "_convert", original)
    retried = run(stores, canonical_only=True)
    assert retried.status == "COMPLETE"
    assert retried.raw_source == "existing" and not retried.raw_updated
    assert retried.raw_generation_id == failed.raw_generation_id


def test_idempotency_and_transformer_version_change(stores):
    first = run(stores, Client([[row()]]))
    manifest = stores[1].current_manifest("daily", DAY)
    second = run(stores)
    assert second.canonical_reused and second.raw_source == "existing"
    assert second.canonical_generation_id == first.canonical_generation_id
    assert stores[1].current_manifest("daily", DAY) == manifest
    refreshed = run(stores, Client([[row()]]), refresh=True)
    assert not refreshed.raw_updated and refreshed.canonical_reused
    updated_store = JQuantsCanonicalStore(stores[1].directory, transformer_version="4")
    third = run((stores[0], updated_store), canonical_only=True)
    assert not third.canonical_reused
    assert third.raw_generation_id == first.raw_generation_id
    assert third.canonical_generation_id != first.canonical_generation_id
    old = (stores[1].directory / "daily" / f"date={DAY}" / "generations"
           / first.canonical_generation_id / "part-00000.parquet")
    assert old.exists()


@pytest.mark.parametrize("conflict", [False, True])
def test_cross_page_exact_and_conflicting_duplicates_fail_without_dedup(stores, conflict):
    first = row()
    repeated = row(C=10) if conflict else row()
    # Distinct page hashes pass raw validation and exercise canonical key validation.
    result = run(stores, Client([[first, row(code="22220")], [repeated]]))
    assert result.status == "FAILED" and result.error_category == "QUALITY_ERROR"
    assert result.quality["conflicting_duplicates"] == int(conflict)
    assert result.quality["exact_duplicates"] == int(not conflict)
    assert stores[1].current_manifest("daily", DAY) is None


@pytest.mark.parametrize("dataset,changes", [
    ("daily", {"Date": "2026-10-19"}),
    ("daily", {"Date": "2026-02-30"}),
    ("daily", {"Code": 12340}),
    ("daily", {"Code": None}),
    ("daily", {"O": "10"}),
    ("daily", {"O": True}),
    ("daily", {"H": 9}),
    ("daily", {"L": 13}),
    ("daily", {"AdjH": 4}),
    ("daily", {"AdjFactor": 0}),
    ("daily", {"Vo": -1}),
    ("daily", {"Va": -1}),
    ("daily", {"UL": 1}),
    ("daily", {"UL": None}),
    ("daily", {"LL": "2"}),
    ("daily", {"LL": None}),
    ("minute", {"Time": "24:00"}),
    ("minute", {"Time": "09:60"}),
    ("minute", {"Time": "9:00"}),
    ("minute", {"Time": "09:00:01"}),
    ("minute", {"Time": None}),
    ("minute", {"C": 13}),
    ("minute", {"Vo": -1}),
])
def test_bad_rows_fail_quality(stores, dataset, changes):
    result = run(stores, Client([[row(dataset, **changes)]], dataset), dataset=dataset)
    assert result.status == "FAILED" and result.error_category == "QUALITY_ERROR"
    assert result.quality["invalid_rows"] == 1
    assert stores[1].current_manifest(dataset, DAY) is None


@pytest.mark.parametrize("dataset,missing", [
    ("daily", "AdjC"), ("minute", "Va"), ("daily", "O"), ("daily", "UL"), ("daily", "LL"),
])
def test_missing_required_field_is_not_silently_null(stores, dataset, missing):
    record = row(dataset)
    del record[missing]
    result = run(stores, Client([[record]], dataset), dataset=dataset)
    assert result.error_category == "QUALITY_ERROR"


def test_quality_failure_preserves_old_canonical(stores):
    old = run(stores, Client([[row()]]))
    failed = run(stores, Client([[row(Vo=-1)]]), refresh=True)
    assert failed.error_category == "QUALITY_ERROR"
    assert failed.raw_generation_id != old.raw_generation_id
    assert failed.canonical_generation_id == old.canonical_generation_id
    assert stores[1].current_manifest("daily", DAY)["generation_id"] == old.canonical_generation_id


def test_raw_prepared_publication_recovered_before_read(stores, monkeypatch):
    old = run(stores, Client([[row()]]))
    original = stores[0]._finish_dataset
    def fail(*args, **kwargs):
        raise OSError("synthetic interrupted ledger finalization")
    monkeypatch.setattr(stores[0], "_finish_dataset", fail)
    interrupted = run(stores, Client([[row(code="22220")]]), refresh=True)
    assert interrupted.status == "FAILED"
    monkeypatch.setattr(stores[0], "_finish_dataset", original)
    recovered = run(stores, canonical_only=True)
    assert recovered.status == "COMPLETE"
    assert recovered.raw_generation_id != old.raw_generation_id
    assert table(stores).column("Code").to_pylist() == ["22220"]


@pytest.mark.parametrize("after_replace", [False, True])
def test_canonical_pointer_failure_retains_valid_generations(stores, monkeypatch, after_replace):
    first = run(stores, Client([[row()]]))
    original = os.replace
    pointer = stores[1].directory / "daily" / f"date={DAY}" / "CURRENT"
    def fail(source, destination):
        if destination == pointer:
            if after_replace:
                original(source, destination)
            raise OSError("SECRET_POINTER_FAILURE")
        return original(source, destination)
    monkeypatch.setattr(canonical_module.os, "replace", fail)
    failed = run(stores, Client([[row(code="22220")]]), refresh=True)
    assert failed.status == "FAILED"
    current = stores[1].current_manifest("daily", DAY)
    assert failed.canonical_generation_id == current["generation_id"]
    assert failed.previous_canonical_generation_id == first.canonical_generation_id
    assert (current["generation_id"] == first.canonical_generation_id) == (not after_replace)
    old = pointer.parent / "generations" / first.canonical_generation_id
    assert pq.ParquetFile(old / "part-00000.parquet").metadata.num_rows == 1
    monkeypatch.setattr(canonical_module.os, "replace", original)
    retried = run(stores, canonical_only=True)
    assert retried.status == "COMPLETE" and retried.canonical_reused


def test_active_job_not_recovered_or_fetched_twice(stores):
    entered = threading.Event()
    release = threading.Event()
    results = []
    def pages():
        entered.set()
        assert release.wait(10)
        yield [row()]
    first_client = Client(pages())
    second_client = Client([[row(code="22220")]])
    worker = threading.Thread(target=lambda: results.append(run(stores, first_client)))
    worker.start()
    try:
        assert entered.wait(10)
        second = run(stores, second_client)
        assert second.status == "FAILED" and second_client.calls == []
        assert stores[0].list_incomplete_datasets()[0].state.value == "RUNNING"
    finally:
        release.set()
        worker.join(10)
    assert not worker.is_alive()
    assert results[0].status == "COMPLETE"


def test_direct_active_capture_lock_is_not_stolen_by_recovery(stores):
    with stores[0].ingestion_lock(
        kind="daily", endpoint="equities/bars/daily", conditions={"date": DAY}
    ) as dataset_id:
        handle = stores[0]._acquire_dataset_lock(dataset_id)
    try:
        client = Client([[row()]])
        result = run(stores, client)
        assert result.status == "FAILED" and client.calls == []
    finally:
        stores[0]._release_dataset_lock(handle)


def test_raw_corruption_stops_instead_of_refetching(stores):
    complete = run(stores, Client([[row()]]))
    published = stores[0].resolve_published(
        kind="daily", endpoint="equities/bars/daily", conditions={"date": DAY}
    )
    published.pages_path.write_text("{}\n")
    result = run(stores, refresh=True)
    assert result.status == "FAILED"
    assert stores[1].current_manifest("daily", DAY)["generation_id"] == complete.canonical_generation_id


def test_pinned_reader_detects_changes_after_resolution(stores):
    run(stores, Client([[row()]]))
    raw = stores[0].resolve_published(
        kind="daily", endpoint="equities/bars/daily", conditions={"date": DAY}
    )
    record = json.loads(raw.pages_path.read_text())
    record["response"]["data"][0]["C"] = 10
    raw.pages_path.write_text(json.dumps(record) + "\n")
    with pytest.raises(JQuantsRawValidationError, match="changed during reading"):
        list(raw.iter_pages())


def test_raw_row_count_mismatch_prevents_canonical_publication(stores):
    run(stores, Client([[row()]]))
    raw = stores[0].resolve_published(
        kind="daily", endpoint="equities/bars/daily", conditions={"date": DAY}
    )
    modified = replace(raw, manifest={**raw.manifest, "row_count": 2})
    changed = JQuantsCanonicalStore(stores[1].directory, transformer_version="4")
    with pytest.raises(JQuantsRawValidationError):
        changed.transform(modified, dataset="daily", target_date=DAY)
    assert stores[1].current_manifest("daily", DAY)["transformer_version"] == "3"


def test_batches_larger_than_writer_batch_size(stores):
    client = Client([[row(code=f"{index:05d}") for index in range(9000)],
                     [row(code=f"{index:05d}") for index in range(9000, 10000)]])
    result = run(stores, client)
    assert result.status == "COMPLETE"
    assert result.row_count == result.symbol_count == 10000
    assert table(stores).num_rows == 10000


def test_canonical_only_without_raw_fails_without_client(stores):
    result = run(stores, canonical_only=True)
    assert result.status == "FAILED" and result.error_category == "CANONICAL_ERROR"
    assert result.raw_source == "none"


def test_lock_release_failure_cannot_report_success(stores, monkeypatch):
    run(stores, Client([[row()]]))
    original = stores[0].ingestion_lock

    @contextmanager
    def fail_release(**kwargs):
        with original(**kwargs) as dataset_id:
            yield dataset_id
        raise OSError("SECRET_RELEASE_ERROR")

    monkeypatch.setattr(stores[0], "ingestion_lock", fail_release)
    result = run(stores, canonical_only=True)
    assert result.status == "FAILED" and result.exit_code == 1
    assert result.error_category == "STORAGE_OR_FORMAT_ERROR"
    assert "SECRET_RELEASE_ERROR" not in json.dumps(result.as_dict())


def test_canonical_hash_corruption_is_not_silently_reused(stores):
    result = run(stores, Client([[row()]]))
    parquet = (stores[1].directory / "daily" / f"date={DAY}" / "generations"
               / result.canonical_generation_id / "part-00000.parquet")
    with parquet.open("ab") as output:
        output.write(b"corrupt")
    failed = run(stores, canonical_only=True)
    assert failed.status == "FAILED"
    assert failed.error_category == "CANONICAL_ERROR"


def test_generation_verification_not_per_record(stores, monkeypatch):
    original = stores[0]._verify_generation
    calls = []

    def counted(*args, **kwargs):
        calls.append(args)
        return original(*args, **kwargs)

    monkeypatch.setattr(stores[0], "_verify_generation", counted)
    result = run(stores, Client([[row(code=f"{index:05d}") for index in range(100)]]))
    assert result.status == "COMPLETE"
    # Capture and resolution can each validate, but never once for each row.
    assert len(calls) < 10
    calls.clear()
    reused = run(stores, canonical_only=True)
    assert reused.status == "COMPLETE"
    # Recovery validates the published generation, then resolution validates it again.
    assert len(calls) == 2


def test_invalid_arguments_rejected_by_usecase_before_storage(stores, monkeypatch):
    monkeypatch.setattr(stores[0], "ingestion_lock", lambda **kwargs: pytest.fail("no storage"))
    usecase = JQuantsSingleDayIngestion(*stores)
    for kwargs in (
        {"dataset": "weekly", "date": DAY},
        {"dataset": "daily", "date": "2026-02-30"},
        {"dataset": "daily", "date": DAY, "refresh": True, "canonical_only": True},
    ):
        with pytest.raises(ValueError):
            usecase.run(**kwargs)


@pytest.mark.parametrize("value", ["2026-2-01", "2026-02-30", "20261020", "2026-10-20x"])
def test_invalid_cli_dates_rejected_without_initializing_store(monkeypatch, value, capsys):
    monkeypatch.setattr(cli, "JQuantsRawStore", lambda: pytest.fail("must validate first"))
    with pytest.raises(SystemExit) as error:
        cli.main(["--dataset", "daily", "--date", value])
    assert error.value.code == 2
    assert value not in capsys.readouterr().err


def test_conflicting_cli_flags_rejected(monkeypatch):
    monkeypatch.setattr(cli, "JQuantsRawStore", lambda: pytest.fail("must validate first"))
    with pytest.raises(SystemExit) as error:
        cli.main(["--dataset", "daily", "--date", DAY, "--refresh", "--canonical-only"])
    assert error.value.code == 2


@pytest.mark.parametrize("mode", ["complete", "empty", "held", "failure", "quality"])
def test_cli_exit_codes_and_safe_json(stores, monkeypatch, capsys, tmp_path, mode):
    if mode == "held":
        run(stores, Client([[row()]]))
    pages = {
        "complete": [[row()]], "empty": [[]], "held": [[]],
        "failure": failing_pages(), "quality": [[row(Vo=-1)]],
    }[mode]
    client = Client(pages)
    monkeypatch.setattr(cli, "JQuantsRawStore", lambda: stores[0])
    monkeypatch.setattr(cli, "JQuantsCanonicalStore", lambda path: stores[1])
    monkeypatch.setattr(cli.config, "JQUANTS_EXECUTION_METRICS_DIRECTORY", tmp_path / "metrics")
    original = cli.JQuantsSingleDayIngestion
    monkeypatch.setattr(cli, "JQuantsSingleDayIngestion",
                        lambda raw, canonical, **kwargs: original(
                            raw, canonical, client_factory=lambda: client, **kwargs
                        ))
    code = cli.main(["--dataset", "daily", "--date", DAY, "--refresh"])
    output = capsys.readouterr()
    summary = json.loads(output.out)
    assert code == (0 if mode == "complete" else 2 if mode in {"empty", "held"} else 1)
    for secret in ("SECRET_PAGE_KEY", "SECRET_EXCEPTION_BODY", "123A0", '"O"', '"AdjC"'):
        assert secret not in output.out + output.err
    assert summary["dataset"] == "daily" and summary["date"] == DAY


def test_written_metrics_json_has_judgement_fields_and_evaluates(stores, tmp_path):
    from src.application.jquants_metrics_evaluation import evaluate_metrics

    client = Client([[row(code="88880")]])
    client.metrics = type("Metrics", (), {
        "as_dict": lambda self: {
            "request_count": 3, "page_count": 1, "status_counts": {"200": 1, "429": 2},
            "retry_count": 2, "rate_limit_429_count": 2, "wait_seconds": 1.5,
        }
    })()
    metrics_directory = tmp_path / "metrics"
    JQuantsSingleDayIngestion(
        *stores, client_factory=lambda: client, metrics_directory=metrics_directory
    ).run(dataset="daily", date=DAY)

    (path,) = (metrics_directory / "daily" / f"date={DAY}").glob("*.json")
    text = path.read_text(encoding="utf-8")
    saved = json.loads(text)
    assert "88880" not in text
    assert saved["raw_source"] == "fetch" and saved["symbol_count"] == 1
    assert saved["error_category"] is None and saved["warnings"] == []
    assert saved["http_status_counts"] == {"200": 1, "429": 2}
    assert set(saved["durations_seconds"]) == {
        "acquisition", "raw_validation", "conversion", "canonical_validation", "total"
    }
    result = evaluate_metrics(saved)
    assert "OTHER_RETRIES_ELEVATED" not in result.reasons
    assert {"WARNINGS", "UNKNOWN_FIELDS", "SYMBOL_COUNT"}.isdisjoint(result.not_evaluated)


def test_writer_drops_non_code_warnings_and_marks_unmeasured_counts(stores, tmp_path):
    ingestion = JQuantsSingleDayIngestion(*stores, metrics_directory=tmp_path / "m")
    assert ingestion._enumerated_warnings(["B_WARN", "8944", "free text", "B_WARN"]) == [
        "B_WARN", "INVALID_WARNING_DROPPED"]
    assert ingestion._enumerated_warnings(["B_WARN", "A_WARN"]) == ["A_WARN", "B_WARN"]
    assert ingestion._unknown_field_count({}) is None
    assert ingestion._unknown_field_count({"unknown_field_count": "x"}) is None
    assert ingestion._unknown_field_count({"unknown_field_count": 2}) == 2
