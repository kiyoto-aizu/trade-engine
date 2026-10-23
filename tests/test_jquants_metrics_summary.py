import csv
import json

from src.application.jquants_metrics_summary import (
    COLUMNS,
    summarize_directory,
    write_csv,
)
from src.entrypoints.summarize_jquants_metrics import main


def write_run(root, dataset, date, name, **overrides):
    directory = root / dataset / f"date={date}"
    directory.mkdir(parents=True, exist_ok=True)
    payload = {
        "dataset": dataset,
        "date": date,
        "status": "COMPLETE",
        "request_count": 10,
        "retry_count": 0,
        "rate_limit_429_count": 0,
        "http_status_counts": {"200": 10},
        "durations_seconds": {"acquisition": 4.0, "conversion": 1.0, "total": 5.0},
        "raw_size_bytes": 100,
        "canonical_size_bytes": 50,
        "field_observations": {"ExRT": {"count": 3}},
        "secret_like": "must-not-appear",
    }
    payload.update(overrides)
    (directory / name).write_text(json.dumps(payload), encoding="utf-8")


def test_summary_rows_and_verdict(tmp_path):
    write_run(tmp_path, "daily", "2026-10-22", "a.json")
    write_run(tmp_path, "minute", "2026-10-22", "b.json", status="FAILED")
    rows = summarize_directory(tmp_path)
    assert [(r["dataset"], r["verdict"]) for r in rows] == [
        ("daily", "Go"),
        ("minute", "No-Go"),
    ]
    assert rows[0]["total_seconds"] == 5.0


def test_unreadable_file_is_no_go(tmp_path):
    write_run(tmp_path, "daily", "2026-10-22", "a.json")
    (tmp_path / "daily" / "date=2026-10-22" / "bad.json").write_text("{", encoding="utf-8")
    rows = summarize_directory(tmp_path)
    assert any(r["reasons"] == "UNREADABLE_METRICS" for r in rows)


def test_csv_contains_only_declared_columns(tmp_path):
    write_run(tmp_path / "m", "daily", "2026-10-22", "a.json")
    output = tmp_path / "out.csv"
    write_csv(summarize_directory(tmp_path / "m"), output)
    text = output.read_text(encoding="utf-8")
    assert "must-not-appear" not in text and "ExRT" not in text
    with output.open(encoding="utf-8", newline="") as stream:
        assert list(csv.DictReader(stream).fieldnames) == list(COLUMNS)


def test_entrypoint(tmp_path):
    write_run(tmp_path / "m", "daily", "2026-10-22", "a.json")
    output = tmp_path / "sub" / "out.csv"
    assert main(["--metrics-dir", str(tmp_path / "m"), "--output", str(output)]) == 0
    assert output.exists()
