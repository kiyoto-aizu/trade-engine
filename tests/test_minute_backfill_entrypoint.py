import json
from contextlib import contextmanager
from datetime import date, datetime
from unittest.mock import patch

from src.domain.models import MinuteBar
from src.entrypoints.run_minute_backfill import main
from src.entrypoints import run_minute_backfill
from src.config import config
from src.infrastructure.persistence.parquet_minute_bar_repository import ParquetMinuteBarRepository


def test_run_minute_backfill_main_flow(tmp_path):
    data_dir = tmp_path / "data"
    filtering_dir = data_dir / "filtering"
    minute_bars_dir = data_dir / "minute_bars_parquet"
    filtering_dir.mkdir(parents=True)

    # フィルタリング結果ファイル作成
    (filtering_dir / "2026-09-10.json").write_text(json.dumps({
        "date": "2026-09-10",
        "symbols": ["7203"],
        "generated_at": "2026-09-10T09:30:00",
    }), encoding="utf-8")

    fake_bars = [
        MinuteBar(time="2026-09-10T09:31:00", price=100.0, cumulative_volume=None, volume=500, source="yahoo"),
    ]

    with patch("src.entrypoints.run_minute_backfill.get_yahoo_intraday_bars", return_value=fake_bars), \
            patch("src.entrypoints.run_minute_backfill.notify_analysis") as mock_notify, \
         patch("sys.argv", ["run_minute_backfill", "--days", "7"]):
        
        main(
            now_provider=lambda: datetime(2026, 9, 10, 10, 0, 0),
            filtering_dir=filtering_dir,
            output_dir=minute_bars_dir,
            quality_report_dir=tmp_path / "analysis",
        )

        mock_notify.assert_called_once()
        assert "【業務】市場データ管理\n【機能】分足バックフィル\n" in mock_notify.call_args[0][0]
        assert "対象銘柄数(朝/昼選定/昼選定外): 1/0/0件" in mock_notify.call_args[0][0]
        assert "新規追加本数: 1本" in mock_notify.call_args[0][0]

    repository = ParquetMinuteBarRepository(minute_bars_dir)
    saved_bars = repository.load_bars(date(2026, 9, 10), "7203")
    assert len(saved_bars) == 1
    assert saved_bars[0].source == "yahoo"
    assert (minute_bars_dir / "symbol=7203" / "date=2026-09-10" / "data.parquet").exists()
    assert not list((data_dir / "minute_bars").glob("**/*.json"))


def test_run_minute_backfill_default_merges_price_band_filter_directories(tmp_path, monkeypatch):
    primary = tmp_path / "filtering"
    band_450 = tmp_path / "filtering_price_bands" / "450"
    band_900 = tmp_path / "filtering_price_bands" / "900"
    midday_root = tmp_path / "filtering_midday"
    for directory, symbols in (
        (primary, ["7203", "8306"]),
        (band_450, ["8306", "9984"]),
        (band_900, ["9984", "9432"]),
    ):
        directory.mkdir(parents=True)
        (directory / "2026-09-10.json").write_text(json.dumps({
            "date": "2026-09-10",
            "symbols": symbols,
            "generated_at": "2026-09-10T09:30:00",
        }), encoding="utf-8")
    for band, symbols in (
        ("270", ["7203", "MID270", "C1"]),
        ("450", ["9984", "MID450", "C1", "C2"]),
        ("900", ["MID900", "9432", "C2", "C3"]),
    ):
        diagnostics = midday_root / band / "diagnostics"
        diagnostics.mkdir(parents=True)
        (diagnostics / "2026-09-10_120000000000.json").write_text(json.dumps({
            "date": "2026-09-10",
            "summary": {
                "input_count": 50,
                "adopted_symbols": [symbol for symbol in symbols if not symbol.startswith("C")],
            },
            "candidates": [
                {"symbol": symbol, "selected": not symbol.startswith("C")}
                for symbol in symbols
            ],
        }), encoding="utf-8")

    captured = {}

    class FakeBackfillUseCase:
        def __init__(self, fetch_intraday_bars, repository):
            self.fetch_intraday_bars = fetch_intraday_bars
            self.repository = repository
            self.last_bars_by_date = {}
            self.last_failures = {}
            self.last_unprocessed_symbols = []

        def run(self, symbols, days, max_elapsed_seconds=None):
            captured.setdefault("calls", []).append((symbols, days, max_elapsed_seconds))
            return {symbol: 0 for symbol in symbols}

    @contextmanager
    def context(*_args, **_kwargs):
        yield

    monkeypatch.setattr(config, "FILTERING_RESULT_DIRECTORY", primary)
    monkeypatch.setattr(config, "FILTERING_PRICE_BAND_RESULT_ROOT", tmp_path / "filtering_price_bands")
    monkeypatch.setattr(config, "MIDDAY_FILTERING_RESULT_ROOT", midday_root)
    monkeypatch.setattr(config, "SCREENING_ALTERNATE_PRICE_CAPS", (450.0, 900.0))
    monkeypatch.setattr(config, "get_screening_price_cap", lambda: 270.0)
    monkeypatch.setattr(config, "MINUTE_BACKFILL_UNSELECTED_TIMEOUT_SECONDS", 1800)
    monkeypatch.setattr(run_minute_backfill, "configure_logging", lambda: None)
    monkeypatch.setattr(run_minute_backfill, "process_notification", context)
    monkeypatch.setattr(run_minute_backfill, "MinuteBarBackfillUseCase", FakeBackfillUseCase)
    monkeypatch.setattr(run_minute_backfill, "format_result_notification", lambda *_args, **_kwargs: "summary")
    monkeypatch.setattr(run_minute_backfill, "notify_analysis", lambda _message: None)
    monkeypatch.setattr(
        run_minute_backfill,
        "__file__",
        str(tmp_path / "src" / "entrypoints" / "run_minute_backfill.py"),
    )

    with patch("sys.argv", ["run_minute_backfill", "--days", "7"]):
        main(
            now_provider=lambda: datetime(2026, 9, 10, 10, 0),
            output_dir=tmp_path / "minute",
        )

    assert list((tmp_path / "data" / "analysis" / "minute_bar_backfill_quality").glob("minute_backfill_*.csv"))
    assert captured["calls"] == [
        (["7203", "8306", "9432", "9984"], 7, None),
        (["MID270", "MID450", "MID900"], 7, None),
        (["C1", "C2", "C3"], 7, 1800),
    ]


def test_collect_recent_symbol_tiers_uses_partial_diagnostic_and_skips_corrupt_file(tmp_path, caplog):
    diagnostics = tmp_path / "diagnostics"
    diagnostics.mkdir()
    (diagnostics / "2026-09-10_120000.json").write_text("{broken", encoding="utf-8")
    (diagnostics / "2026-09-11_120000.json").write_text(json.dumps({
        "date": "2026-09-11",
        "summary": {"adopted_symbols": None},
        "candidates": [
            {"symbol": "SELECTED", "selected": True},
            {"symbol": "UNSELECTED", "selected": False},
            {"bad": "candidate"},
        ],
    }), encoding="utf-8")

    morning, selected, unselected, expected_dates = run_minute_backfill.collect_recent_symbol_tiers(
        tmp_path / "no-morning-results",
        days=7,
        today=date(2026, 9, 11),
        midday_diagnostic_dirs=(diagnostics,),
    )

    assert morning == []
    assert selected == ["SELECTED"]
    assert unselected == ["UNSELECTED"]
    assert expected_dates == {
        "SELECTED": {date(2026, 9, 11)},
        "UNSELECTED": {date(2026, 9, 11)},
    }
    assert "昼推定フィルタ診断を読み込めません" in caplog.text
    assert "adopted_symbols" in caplog.text
