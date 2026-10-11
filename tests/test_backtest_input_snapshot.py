import json
import shutil
import sys
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

from src.application import backtest_execution_usecase
from src.config import config
from src.domain.market_volatility import MarketDailyBar
from src.domain.models import MinuteBar
from src.domain.volatility import DailyBar
from src.entrypoints import run_backtest
from src.infrastructure.market_data.yahoo_index_client import YahooIndexClient
from src.infrastructure.persistence.backtest_input_snapshot import (
    SnapshotError,
    load_backtest_input_snapshot,
)
from src.infrastructure.persistence.parquet_minute_bar_repository import ParquetMinuteBarRepository

HISTORY = {
    "7203": {
        (date(2026, 9, 1) + timedelta(days=offset)).isoformat(): float(close)
        for offset, close in enumerate([100, 100, 100, 100, 100, 101, 103, 106, 104, 101, 99, 98, 100, 103])
    },
}
OHLC = {
    "7203": {
        date_text: DailyBar(high=close + 2, low=close - 2, close=close, open=close - 1, volume=1000.0)
        for date_text, close in HISTORY["7203"].items()
    },
}


@pytest.fixture(autouse=True)
def backtest_environment(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "RSI_PERIOD", 2)
    monkeypatch.setattr(config, "RSI_MINIMUM_CLOSES", 5)
    monkeypatch.setattr(config, "RSI_ENTRY_THRESHOLD", 50.0)
    monkeypatch.setattr(config, "RSI_EXIT_THRESHOLD", 50.0)
    monkeypatch.setattr(config, "BACKTEST_FEE_RATE", 0.0)
    monkeypatch.setattr(config, "BACKTEST_MARKET_SLIPPAGE_BPS", 0.0)
    monkeypatch.setattr(config, "BACKTEST_EXECUTION_DELAY_BARS", 0)
    monkeypatch.setattr(config, "BACKTEST_FILTER_DECISION_DIRECTORY", tmp_path / "events")
    monkeypatch.setattr(config, "FILTER_DECISION_DATABASE_FILE", tmp_path / "production.sqlite3")
    monkeypatch.setattr(config, "BACKTEST_INPUT_SNAPSHOT_DIRECTORY", tmp_path / "snapshots")
    monkeypatch.setattr(run_backtest, "configure_logging", lambda *a, **k: None)
    monkeypatch.setattr(run_backtest, "notify_analysis", lambda message: True)
    monkeypatch.setattr(run_backtest, "create_daily_analyzer", lambda: None)

    @contextmanager
    def no_notification(*args, **kwargs):
        yield

    monkeypatch.setattr(run_backtest, "process_notification", no_notification)


@pytest.fixture
def filtering_dir(tmp_path):
    directory = tmp_path / "filtering"
    directory.mkdir()
    for offset in range(8):
        date_text = (date(2026, 9, 7) + timedelta(days=offset)).isoformat()
        (directory / f"{date_text}.json").write_text(
            json.dumps({"date": date_text, "symbols": ["7203"]}), encoding="utf-8"
        )
    return directory


@pytest.fixture
def minute_dir(tmp_path):
    repository = ParquetMinuteBarRepository(tmp_path / "minute")
    repository.replace_bars(date(2026, 9, 7), "7203", [
        MinuteBar(time="2026-09-07T09:00:00", price=92.0, cumulative_volume=None, volume=10, source="yahoo"),
        MinuteBar(time="2026-09-07T09:01:00", price=92.0, cumulative_volume=None, volume=12, source="yahoo"),
    ])
    return repository.directory


def _index_bars(symbol):
    start = date(2026, 7, 1)
    if symbol == "^VIX":
        return [
            MarketDailyBar(date=start + timedelta(days=offset), open=14.0, high=15.0, low=13.0,
                           close=14.0 + (offset % 3) * 0.1)
            for offset in range(80)
        ]
    return [
        MarketDailyBar(
            date=start + timedelta(days=offset),
            open=40000.0 + offset * 10, high=40100.0 + offset * 10,
            low=39900.0 + offset * 10, close=40000.0 + offset * 10 + (offset % 5) * 5,
        )
        for offset in range(80)
    ]


@pytest.fixture
def mocked_network(monkeypatch):
    calls = {"count": 0}

    def history(symbols, days):
        calls["count"] += 1
        return {symbol: dict(values) for symbol, values in HISTORY.items()}

    def ohlc(symbols, days):
        calls["count"] += 1
        return {symbol: dict(values) for symbol, values in OHLC.items()}

    def index_bars(self, symbol, range_="max"):
        calls["count"] += 1
        return _index_bars(symbol)

    monkeypatch.setattr(backtest_execution_usecase, "fetch_yahoo_dated_history", history)
    monkeypatch.setattr(backtest_execution_usecase, "fetch_yahoo_dated_ohlc", ohlc)
    monkeypatch.setattr(YahooIndexClient, "get_daily_ohlc", index_bars)
    return calls


def _forbid_network(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("ネットワークを使ってはいけません")

    monkeypatch.setattr(backtest_execution_usecase, "fetch_yahoo_dated_history", forbidden)
    monkeypatch.setattr(backtest_execution_usecase, "fetch_yahoo_dated_ohlc", forbidden)
    monkeypatch.setattr(backtest_execution_usecase, "fetch_yahoo_history", forbidden)
    monkeypatch.setattr(YahooIndexClient, "get_daily_ohlc", forbidden)


def _run(monkeypatch, *arguments):
    monkeypatch.setattr(sys, "argv", ["run_backtest.py", *arguments])
    run_backtest.main()


def _live_arguments(filtering_dir, output, minute_dir=None):
    arguments = [
        "--filtering-dir", str(filtering_dir), "--live", "--compare-market-regime",
        "--compare-atr", "--cash", "1000000", "--qty", "100", "--fixed-qty", "--allow-overnight",
        "--fee", "0", "--market-slippage-bps", "0", "--execution-delay-bars", "0",
        "--output", str(output),
    ]
    if minute_dir is not None:
        arguments += ["--minute-bars-dir", str(minute_dir)]
    return arguments


def _read(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _single_snapshot(tmp_path):
    snapshots = [path for path in (tmp_path / "snapshots").iterdir() if path.is_dir()]
    assert len(snapshots) == 1
    return snapshots[0]


def _without_save_time(result):
    return {key: value for key, value in result.items() if key != "generated_at"}


def test_saved_snapshot_replays_to_identical_result_without_network(
    monkeypatch, tmp_path, filtering_dir, minute_dir, mocked_network
):
    first_path = tmp_path / "first.json"
    _run(monkeypatch, *_live_arguments(filtering_dir, first_path))
    first = _read(first_path)
    snapshot_dir = _single_snapshot(tmp_path)

    assert first["total_trades"] > 0
    assert first["snapshot_path"] == str(snapshot_dir.resolve())
    manifest = _read(snapshot_dir / "manifest.json")
    assert manifest["period_start"] == "2026-09-07" and manifest["period_end"] == "2026-09-14"
    assert manifest["symbol_count"] == 1 and manifest["total_bytes"] > 0
    assert {"history.json.gz", "ohlc_history.json.gz", "index_daily.json.gz", "run_conditions.json",
            "minute_manifest.json", "filtering/2026-09-07.json"} <= set(manifest["files"])
    assert not list((tmp_path / "minute").rglob("*.json.gz"))

    shutil.rmtree(filtering_dir)
    _forbid_network(monkeypatch)
    second_path = tmp_path / "second.json"
    _run(monkeypatch, "--input-snapshot", str(snapshot_dir), "--output", str(second_path))
    second = _read(second_path)

    assert _without_save_time(second) == _without_save_time(first)
    assert len([p for p in (tmp_path / "snapshots").iterdir()]) == 1


def test_replay_overrides_only_explicit_arguments_and_records_them(
    monkeypatch, tmp_path, filtering_dir, mocked_network
):
    first_path = tmp_path / "first.json"
    _run(monkeypatch, *_live_arguments(filtering_dir, first_path))
    snapshot_dir = _single_snapshot(tmp_path)
    first = _read(first_path)
    _forbid_network(monkeypatch)

    unchanged_path = tmp_path / "unchanged.json"
    _run(monkeypatch, "--input-snapshot", str(snapshot_dir), "--cash", "1000000", "--output", str(unchanged_path))
    assert "input_snapshot_overrides" not in _read(unchanged_path)

    changed_path = tmp_path / "changed.json"
    _run(monkeypatch, "--input-snapshot", str(snapshot_dir), "--cash", "20000", "--output", str(changed_path))
    changed = _read(changed_path)

    assert changed["input_snapshot_overrides"] == {"cash": {"snapshot": 1000000.0, "override": 20000.0}}
    assert changed["cash"] != first["cash"] or changed["total_pnl"] != first["total_pnl"]


def test_replay_warns_when_minute_bars_changed(
    monkeypatch, tmp_path, filtering_dir, minute_dir, mocked_network, caplog
):
    _run(monkeypatch, *_live_arguments(filtering_dir, tmp_path / "first.json", minute_dir))
    snapshot_dir = _single_snapshot(tmp_path)
    ParquetMinuteBarRepository(minute_dir).replace_bars(date(2026, 9, 7), "7203", [
        MinuteBar(time="2026-09-07T09:00:00", price=999.0, cumulative_volume=None, volume=1, source="yahoo"),
    ])
    _forbid_network(monkeypatch)
    caplog.clear()

    _run(monkeypatch, "--input-snapshot", str(snapshot_dir), "--output", str(tmp_path / "second.json"))

    assert "分足がスナップショット保存時から変化しています" in caplog.text
    assert (tmp_path / "second.json").exists()


@pytest.fixture
def saved_snapshot(monkeypatch, tmp_path, filtering_dir, mocked_network):
    _run(monkeypatch, *_live_arguments(filtering_dir, tmp_path / "first.json"))
    return _single_snapshot(tmp_path)


def test_snapshot_with_missing_file_is_rejected(monkeypatch, tmp_path, saved_snapshot):
    (saved_snapshot / "ohlc_history.json.gz").unlink()
    with pytest.raises(SnapshotError, match="欠けています.*ohlc_history"):
        load_backtest_input_snapshot(saved_snapshot)

    _forbid_network(monkeypatch)
    with pytest.raises(SnapshotError):
        _run(monkeypatch, "--input-snapshot", str(saved_snapshot), "--output", str(tmp_path / "x.json"))
    assert not (tmp_path / "x.json").exists()


def test_snapshot_with_wrong_sha256_is_rejected(saved_snapshot):
    target = saved_snapshot / "filtering" / "2026-09-07.json"
    target.write_text(json.dumps({"date": "2026-09-07", "symbols": ["9999"]}), encoding="utf-8")

    with pytest.raises(SnapshotError, match="sha256.*2026-09-07"):
        load_backtest_input_snapshot(saved_snapshot)


def test_snapshot_without_manifest_is_rejected(tmp_path):
    with pytest.raises(SnapshotError, match="manifest.json"):
        load_backtest_input_snapshot(tmp_path)


def test_backtest_continues_when_snapshot_save_fails(
    monkeypatch, tmp_path, filtering_dir, mocked_network, caplog
):
    def failing_save(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(backtest_execution_usecase, "save_backtest_input_snapshot", failing_save)
    output = tmp_path / "result.json"

    _run(monkeypatch, *_live_arguments(filtering_dir, output))

    result = _read(output)
    assert result["total_trades"] > 0
    assert result["snapshot_path"] is None
    assert "スナップショットの保存に失敗しました" in caplog.text
    assert not list((tmp_path / "snapshots").glob("*.tmp"))


def test_result_is_unchanged_when_snapshot_is_not_requested(
    monkeypatch, tmp_path, filtering_dir, mocked_network
):
    from types import SimpleNamespace

    args = SimpleNamespace(
        filtering_dir=filtering_dir, start_date=None, end_date=None, days=730, live=True,
        compare_market_regime=False, cash=10_000.0, qty=100, fee=0.0, market_slippage_bps=0.0,
        execution_delay_bars=0, order_type="market", allow_overnight=True,
        indicator_source="daily", compare_atr=False,
    )

    result, _, _ = backtest_execution_usecase.execute_backtest(args, {}, None)

    assert "snapshot_path" not in result
    assert not (tmp_path / "snapshots").exists()
