import csv
import json
import sys
from datetime import date

from src.config import config
from src.domain.models import MinuteBar
from src.entrypoints import run_midday_minute_backfill_once as manual_backfill
from src.infrastructure.market_data.get_intraday_bars import IntradayFetchResult
from src.infrastructure.persistence.parquet_minute_bar_repository import ParquetMinuteBarRepository


def test_main_defaults_report_directory_to_data_analysis(tmp_path, monkeypatch):
    monkeypatch.setattr(
        manual_backfill,
        "__file__",
        str(tmp_path / "src" / "entrypoints" / "run_midday_minute_backfill_once.py"),
    )
    monkeypatch.setattr(manual_backfill, "configure_logging", lambda: None)
    captured = {}
    monkeypatch.setattr(manual_backfill, "run", lambda *args: captured.setdefault("args", args))
    monkeypatch.setattr(sys, "argv", [
        "run_midday_minute_backfill_once",
        "--start-date", "2026-10-13",
        "--end-date", "2026-10-13",
    ])

    manual_backfill.main()

    assert captured["args"][3] == tmp_path / "data" / "analysis" / "minute_bar_backfill_quality"


def test_manual_backfill_imports_all_diagnostic_candidates_without_overwriting(tmp_path, monkeypatch):
    diagnostics_root = tmp_path / "filtering_midday"
    for band, candidates in (
        ("270", [{"symbol": "A", "selected": True}, {"symbol": "B", "selected": False}]),
        ("450", [{"symbol": "A", "selected": True}, {"symbol": "C", "selected": False}]),
        ("900", [{"symbol": "D", "selected": False}]),
    ):
        diagnostic_dir = diagnostics_root / band / "diagnostics"
        diagnostic_dir.mkdir(parents=True)
        (diagnostic_dir / "2026-10-13_120000.json").write_text(json.dumps({
            "date": "2026-10-13",
            "candidates": candidates,
        }), encoding="utf-8")
    monkeypatch.setattr(config, "MIDDAY_FILTERING_RESULT_ROOT", diagnostics_root)
    monkeypatch.setattr(config, "SCREENING_ALTERNATE_PRICE_CAPS", (450.0, 900.0))
    monkeypatch.setattr(config, "get_screening_price_cap", lambda: 270.0)

    output_dir = tmp_path / "parquet"
    repository = ParquetMinuteBarRepository(output_dir)
    repository.append_bar(date(2026, 10, 13), "A", MinuteBar(
        time="2026-10-13T09:31:00",
        price=100.0,
        cumulative_volume=None,
        volume=5,
        source="poll",
    ))
    requested = []

    def fetch(symbol, start, end):
        requested.append((symbol, start, end))
        return IntradayFetchResult(bars=[
            MinuteBar(
                time=f"{start.isoformat()}T09:31:00",
                price=999.0 if symbol == "A" else 101.0,
                cumulative_volume=None,
                volume=10,
                source="yahoo",
            ),
            MinuteBar(
                time=f"{start.isoformat()}T09:32:00",
                price=102.0,
                cumulative_volume=None,
                volume=20,
                source="yahoo",
            ),
        ])

    monkeypatch.setattr(manual_backfill, "fetch_yahoo_intraday_bars_for_dates", fetch)

    report = manual_backfill.run(
        date(2026, 10, 13),
        date(2026, 10, 13),
        output_dir,
        tmp_path / "analysis",
    )

    assert requested == [
        (symbol, date(2026, 10, 13), date(2026, 10, 13))
        for symbol in ["A", "B", "C", "D"]
    ]
    saved = repository.load_bars(date(2026, 10, 13), "A")
    assert [(bar.time, bar.price, bar.source) for bar in saved] == [
        ("2026-10-13T09:31:00", 100.0, "poll"),
        ("2026-10-13T09:32:00", 102.0, "yahoo"),
    ]
    with report.open(encoding="utf-8-sig", newline="") as stream:
        records = list(csv.DictReader(stream))
    assert len(records) == 4
    assert all(row["yahoo_bars_received"] == "2" for row in records)
    assert all(row["bars_added"] == "2" for row in records if row["symbol"] != "A")
