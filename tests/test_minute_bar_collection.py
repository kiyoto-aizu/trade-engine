import json
from datetime import date
import logging

from src.entrypoints.run_minute_backfill import collect_recent_symbols


def test_collect_recent_symbols(tmp_path):
    filtering_dir = tmp_path / "filtering"
    filtering_dir.mkdir(parents=True)

    # 2026-09-08のフィルタリング結果
    file_1 = filtering_dir / "2026-09-08.json"
    file_1.write_text(json.dumps({
        "date": "2026-09-08",
        "symbols": ["7203", "8306"],
        "generated_at": "2026-09-08T09:30:00",
    }), encoding="utf-8")

    # 2026-09-10のフィルタリング結果
    file_2 = filtering_dir / "2026-09-10.json"
    file_2.write_text(json.dumps({
        "date": "2026-09-10",
        "symbols": ["8306", "9984"],
        "generated_at": "2026-09-10T09:30:00",
    }), encoding="utf-8")

    today = date(2026, 9, 10)
    # 直近3日間 (2026-09-07以降)
    symbols = collect_recent_symbols(filtering_dir, days=3, today=today)
    assert symbols == ["7203", "8306", "9984"]

    # 直近1日間 (2026-09-09以降)
    symbols_1d = collect_recent_symbols(filtering_dir, days=1, today=today)
    assert symbols_1d == ["8306", "9984"]


def test_collect_recent_symbols_merges_additional_price_band_results(tmp_path):
    primary = tmp_path / "filtering"
    band_450 = tmp_path / "filtering_price_bands" / "450"
    band_900 = tmp_path / "filtering_price_bands" / "900"
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

    symbols = collect_recent_symbols(
        primary,
        days=3,
        today=date(2026, 9, 10),
        additional_filtering_dirs=(band_450, band_900),
    )

    assert symbols == ["7203", "8306", "9432", "9984"]


def test_collect_recent_symbols_merges_latest_midday_diagnostic_selections(tmp_path):
    primary = tmp_path / "filtering"
    primary.mkdir()
    (primary / "2026-09-10.json").write_text(json.dumps({
        "date": "2026-09-10",
        "symbols": ["7203", "8306"],
    }), encoding="utf-8")

    diagnostic_dirs = [tmp_path / "midday" / band / "diagnostics" for band in ("270", "450", "900")]
    for directory in diagnostic_dirs:
        directory.mkdir(parents=True)
    for directory, symbols in zip(diagnostic_dirs, (
        ["8306", "3996"],
        ["3996", "9984"],
        ["9432"],
    )):
        (directory / "2026-09-10_120000000000.json").write_text(json.dumps({
            "date": "2026-09-10",
            "summary": {"input_count": 50, "adopted_symbols": symbols},
            "candidates": [{"symbol": f"{index:04d}"} for index in range(50)],
        }), encoding="utf-8")

    symbols = collect_recent_symbols(
        primary,
        days=3,
        today=date(2026, 9, 10),
        midday_diagnostic_dirs=tuple(diagnostic_dirs),
    )

    expected = sorted({"3996", "7203", "8306", "9432", "9984"} | {f"{i:04d}" for i in range(50)})
    assert symbols == expected


def test_collect_recent_symbols_merges_same_day_diagnostics_and_ignores_old_dates(tmp_path):
    diagnostic_dir = tmp_path / "midday" / "450" / "diagnostics"
    diagnostic_dir.mkdir(parents=True)
    for filename, symbols in (
        ("2026-09-10_120000000000.json", ["OLD"]),
        ("2026-09-10_120100000000.json", ["NEW"]),
        ("2026-09-06_120000000000.json", ["OUTSIDE"]),
    ):
        (diagnostic_dir / filename).write_text(json.dumps({
            "date": filename[:10],
            "summary": {"adopted_symbols": symbols},
        }), encoding="utf-8")

    symbols = collect_recent_symbols(
        tmp_path / "missing-morning-results",
        days=3,
        today=date(2026, 9, 10),
        midday_diagnostic_dirs=(diagnostic_dir,),
    )

    assert symbols == ["NEW", "OLD"]


def test_collect_recent_symbols_applies_inclusive_seven_day_cutoff_to_diagnostics(tmp_path):
    diagnostic_dir = tmp_path / "midday" / "900" / "diagnostics"
    diagnostic_dir.mkdir(parents=True)
    for day, symbol in (("2026-10-16", "TOO_OLD"), ("2026-10-17", "CUTOFF")):
        (diagnostic_dir / f"{day}_120000000000.json").write_text(json.dumps({
            "date": day,
            "summary": {"adopted_symbols": [symbol]},
        }), encoding="utf-8")

    symbols = collect_recent_symbols(
        tmp_path / "missing-morning-results",
        days=7,
        today=date(2026, 10, 24),
        midday_diagnostic_dirs=(diagnostic_dir,),
    )

    assert symbols == ["CUTOFF"]


def test_collect_recent_symbols_skips_invalid_or_missing_midday_diagnostics(
    tmp_path, caplog
):
    diagnostic_dir = tmp_path / "midday" / "450" / "diagnostics"
    diagnostic_dir.mkdir(parents=True)
    (diagnostic_dir / "2026-09-10_120000000000.json").write_text("{bad json", encoding="utf-8")
    (diagnostic_dir / "2026-09-10_120100000000.json").write_text(json.dumps({
        "date": "2026-09-10",
        "summary": {"adopted_symbols": "not-a-list"},
    }), encoding="utf-8")
    missing_dir = tmp_path / "missing-diagnostics"

    with caplog.at_level(logging.WARNING):
        symbols = collect_recent_symbols(
            tmp_path / "missing-morning-results",
            days=3,
            today=date(2026, 9, 10),
            midday_diagnostic_dirs=(diagnostic_dir, missing_dir),
        )

    assert symbols == []
    assert "昼推定フィルタ診断を読み込めません" in caplog.text
