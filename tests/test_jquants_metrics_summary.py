import csv
import json

from src.application.jquants_metrics_evaluation import JQuantsGoThresholds
from src.application.jquants_metrics_summary import (
    COLUMNS,
    summarize_directory,
    write_csv,
)
from src.entrypoints.summarize_jquants_metrics import main

LIMITS = {
    "daily": JQuantsGoThresholds(elapsed_limit_seconds=100.0),
    "minute": JQuantsGoThresholds(elapsed_limit_seconds=100.0),
}


def write_run(root, folder, day, name, **overrides):
    directory = root / folder / f"date={day}"
    directory.mkdir(parents=True, exist_ok=True)
    payload = {
        "dataset": folder,
        "date": day,
        "status": "COMPLETE",
        "request_count": 10,
        "retry_count": 0,
        "rate_limit_429_count": 0,
        "wait_seconds": 0.0,
        "http_status_counts": {"200": 10},
        "durations_seconds": {"acquisition": 4.0, "conversion": 1.0, "total": 5.0},
        "raw_size_bytes": 100,
        "canonical_size_bytes": 50,
        "field_observations": {"ExRT": {"count": 3}},
        "secret_like": "must-not-appear",
    }
    payload.update(overrides)
    (directory / name).write_text(json.dumps(payload), encoding="utf-8")


def test_summary_rows_and_partial_verdict(tmp_path):
    write_run(tmp_path, "daily", "2026-10-22", "a.json")
    write_run(tmp_path, "minute", "2026-10-22", "b.json", status="FAILED")
    rows = summarize_directory(tmp_path, LIMITS)
    assert [(r["dataset"], r["partial_verdict"]) for r in rows] == [
        ("daily", "Go"),
        ("minute", "No-Go"),
    ]
    assert rows[0]["total_seconds"] == 5
    assert "WARNINGS" in rows[0]["not_evaluated"]


def test_unreadable_and_non_object_files_are_no_go(tmp_path):
    write_run(tmp_path, "daily", "2026-10-22", "a.json")
    directory = tmp_path / "daily" / "date=2026-10-22"
    (directory / "bad.json").write_text("{", encoding="utf-8")
    (directory / "list.json").write_text("[]", encoding="utf-8")
    rows = summarize_directory(tmp_path, LIMITS)
    assert sum(r["reasons"] == "UNREADABLE_METRICS" for r in rows) == 2


def test_values_are_validated_not_copied(tmp_path):
    write_run(
        tmp_path, "daily", "2026-10-22", "a.json",
        status="LEAKTOKEN", dataset="LEAKTOKEN", date="88880",
        request_count={"code": "88880"}, retry_count=-1,
        total_seconds=float("nan"),
    )
    output = tmp_path / "out.csv"
    write_csv(summarize_directory(tmp_path, LIMITS), output)
    text = output.read_text(encoding="utf-8-sig")
    assert "88880" not in text and "LEAKTOKEN" not in text and "must-not-appear" not in text
    assert "ExRT" not in text


def test_latest_attempt_and_consecutive_failures(tmp_path):
    write_run(tmp_path, "daily", "2026-10-22", "1.json", status="FAILED")
    write_run(tmp_path, "daily", "2026-10-22", "2.json", status="FAILED")
    write_run(tmp_path, "daily", "2026-10-22", "3.json", status="COMPLETE")
    write_run(tmp_path, "daily", "2026-10-22", "4.json", status="FAILED")
    rows = summarize_directory(tmp_path, LIMITS)
    assert [r["consecutive_failed"] for r in rows] == [1, 2, 0, 1]
    assert [r["is_latest"] for r in rows] == [False, False, False, True]


def test_csv_columns_and_excel_friendly_encoding(tmp_path):
    write_run(tmp_path / "m", "daily", "2026-10-22", "a.json")
    output = tmp_path / "out.csv"
    write_csv(summarize_directory(tmp_path / "m", LIMITS), output)
    assert output.read_bytes().startswith(b"\xef\xbb\xbf")
    with output.open(encoding="utf-8-sig", newline="") as stream:
        assert list(csv.DictReader(stream).fieldnames) == list(COLUMNS)


def test_entrypoint_exit_codes(tmp_path):
    import pytest
    write_run(tmp_path / "m", "daily", "2026-10-22", "a.json", status="FAILED")
    output = tmp_path / "sub" / "out.csv"
    args = ["--metrics-dir", str(tmp_path / "m"), "--output", str(output)]
    assert main(args) == 0
    assert output.exists()
    assert main(args + ["--fail-on-no-go"]) == 3
    write_run(tmp_path / "m", "daily", "2026-10-22", "z.json", status="COMPLETE")
    assert main(args + ["--fail-on-no-go", "--daily-elapsed-limit", "100"]) == 0
    assert main(["--metrics-dir", str(tmp_path / "none"), "--output", str(output)]) == 4
    with pytest.raises(SystemExit):
        main(args + ["--daily-elapsed-limit", "nan"])
