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
        )

        mock_notify.assert_called_once()
        assert "【業務】市場データ管理\n【機能】分足バックフィル\n" in mock_notify.call_args[0][0]
        assert "対象銘柄数: 1件" in mock_notify.call_args[0][0]
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
        ("270", ["7203", "MID270"]),
        ("450", ["9984", "MID450"]),
        ("900", ["MID900", "9432"]),
    ):
        diagnostics = midday_root / band / "diagnostics"
        diagnostics.mkdir(parents=True)
        (diagnostics / "2026-09-10_120000000000.json").write_text(json.dumps({
            "date": "2026-09-10",
            "summary": {"input_count": 50, "adopted_symbols": symbols},
        }), encoding="utf-8")

    captured = {}

    class FakeBackfillUseCase:
        def __init__(self, fetch_intraday_bars, repository):
            self.fetch_intraday_bars = fetch_intraday_bars
            self.repository = repository

        def run(self, symbols, days):
            captured["symbols"] = symbols
            captured["days"] = days
            return {symbol: 0 for symbol in symbols}

    @contextmanager
    def context(*_args, **_kwargs):
        yield

    monkeypatch.setattr(config, "FILTERING_RESULT_DIRECTORY", primary)
    monkeypatch.setattr(config, "FILTERING_PRICE_BAND_RESULT_ROOT", tmp_path / "filtering_price_bands")
    monkeypatch.setattr(config, "MIDDAY_FILTERING_RESULT_ROOT", midday_root)
    monkeypatch.setattr(config, "SCREENING_ALTERNATE_PRICE_CAPS", (450.0, 900.0))
    monkeypatch.setattr(config, "get_screening_price_cap", lambda: 270.0)
    monkeypatch.setattr(run_minute_backfill, "configure_logging", lambda: None)
    monkeypatch.setattr(run_minute_backfill, "process_notification", context)
    monkeypatch.setattr(run_minute_backfill, "MinuteBarBackfillUseCase", FakeBackfillUseCase)
    monkeypatch.setattr(run_minute_backfill, "format_result_notification", lambda *_args, **_kwargs: "summary")
    monkeypatch.setattr(run_minute_backfill, "notify_analysis", lambda _message: None)

    with patch("sys.argv", ["run_minute_backfill", "--days", "7"]):
        main(now_provider=lambda: datetime(2026, 9, 10, 10, 0), output_dir=tmp_path / "minute")

    assert captured == {
        "symbols": ["7203", "8306", "9432", "9984", "MID270", "MID450", "MID900"],
        "days": 7,
    }
