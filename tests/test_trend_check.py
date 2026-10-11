import json
from datetime import date, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.application import trend_check_report as report
from src.application.trend_check_usecase import (
    DailyBarStore,
    TrendCheckPaths,
    ensure_builtin_versions,
    recompute_all,
    run_day,
)
from src.domain.trend_check import (
    CRITERIA_V1,
    LABEL_CHOPPY,
    LABEL_FLAT,
    LABEL_REVERSE,
    LABEL_TREND,
    LABEL_UNDECIDABLE,
    calculate_atr_before,
    calculate_minute_auxiliary,
    judge_day,
)
from src.domain.volatility import DailyBar
from src.entrypoints import run_trend_check
from src.infrastructure.persistence.trend_check_repository import TrendCheckRepository

TARGET = "2026-09-10"


def _bar(open_, high, low, close):
    return DailyBar(high=high, low=low, close=close, open=open_)


def _history(days=40, end="2026-09-09"):
    last = date.fromisoformat(end)
    bars = {}
    for index in range(days):
        day = (last - timedelta(days=days - 1 - index)).isoformat()
        bars[day] = _bar(100, 102, 98, 100)  # ATR = 4
    return bars


def _write_cache(cache_dir: Path, symbol: str, bars: dict):
    cache_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        day: {"open": b.open, "high": b.high, "low": b.low, "close": b.close, "volume": 1000}
        for day, b in bars.items()
    }
    (cache_dir / f"{symbol}.json").write_text(json.dumps(payload), encoding="utf-8")


@pytest.fixture
def paths(tmp_path):
    base = tmp_path
    result = TrendCheckPaths(
        filtering_dir=base / "filtering",
        screening_dir=base / "screening",
        diagnostics_dir=base / "diagnostics",
        daily_cache_dir=base / "cache",
        minute_bar_dir=base / "minute",
        daily_report_dir=base / "daily_reports",
        order_history_file=base / "order_history.json",
        decision_database=base / "decision.sqlite3",
        midday_filtering_dir=base / "filtering_midday",
    )
    result.filtering_dir.mkdir()
    result.screening_dir.mkdir()
    return result


def _setup_day(paths, selected, extra_cache=(), day=TARGET):
    """selected: {symbol: today's bar or None}"""
    (paths.filtering_dir / f"{day}.json").write_text(json.dumps({"symbols": list(selected)}), encoding="utf-8")
    for symbol, today in selected.items():
        bars = _history()
        if today is not None:
            bars[day] = today
        _write_cache(paths.daily_cache_dir, symbol, bars)
    for symbol, today in extra_cache:
        bars = _history()
        bars[day] = today
        _write_cache(paths.daily_cache_dir, symbol, bars)


def _repository(tmp_path):
    repository = TrendCheckRepository(tmp_path / "trend.sqlite3")
    ensure_builtin_versions(repository, "2026-09-01")
    return repository


def test_judge_labels_and_undecidable():
    atr = 4.0
    assert judge_day(_bar(100, 104, 100, 103), atr, CRITERIA_V1).label == LABEL_TREND
    assert judge_day(_bar(100, 101, 95, 97), atr, CRITERIA_V1).label == LABEL_REVERSE
    assert judge_day(_bar(100, 101, 100, 100), atr, CRITERIA_V1).label == LABEL_FLAT
    assert judge_day(_bar(100, 106, 98, 100.5), atr, CRITERIA_V1).label == LABEL_CHOPPY
    assert judge_day(None, atr, CRITERIA_V1).label == LABEL_UNDECIDABLE
    assert judge_day(_bar(100, 104, 100, 103), None, CRITERIA_V1).label == LABEL_UNDECIDABLE


def test_atr_uses_only_days_before_target():
    bars = _history()
    base = calculate_atr_before(bars, TARGET, 14)
    bars[TARGET] = _bar(100, 500, 1, 300)
    bars["2026-09-11"] = _bar(100, 900, 1, 300)
    assert calculate_atr_before(bars, TARGET, 14) == base


def test_minute_auxiliary_is_recorded():
    minutes = [SimpleNamespace(time=f"09:0{i}", price=p, volume=100) for i, p in enumerate([100, 101, 102, 101, 103])]
    aux = calculate_minute_auxiliary(minutes)
    assert 0 <= aux["direction_consistency"] <= 1
    assert 0 <= aux["vwap_above_ratio"] <= 1
    assert calculate_minute_auxiliary([])["vwap_above_ratio"] is None


def test_run_day_is_idempotent_and_keeps_old_version(paths, tmp_path):
    _setup_day(
        paths,
        {"1111": _bar(100, 104, 100, 103), "2222": _bar(100, 101, 95, 97), "3333": None},
        extra_cache=[("4444", _bar(100, 104, 100, 103))],
    )
    repository = _repository(tmp_path)
    first = run_day(repository, TARGET, "v1", paths)
    assert first["selected_trend"] == 1
    assert first["selected_undecidable"] == 1
    run_day(repository, TARGET, "v1", paths)
    rows = repository.load_rows("v1", TARGET, TARGET)
    assert len(rows) == 4 and {row["version"] for row in rows} == {"v1"}

    definition = json.loads(json.dumps(CRITERIA_V1))
    definition["version"] = "v2"
    definition["thresholds"]["open_to_close_min_pct"] = 5.0
    repository.register_version("v2", definition, TARGET, "テスト")
    run_day(repository, TARGET, "v2", paths)
    assert repository.load_summaries("v1", TARGET, TARGET)[0]["selected_trend"] == 1
    assert repository.load_summaries("v2", TARGET, TARGET)[0]["selected_trend"] == 0
    assert len(repository.load_rows("v1", TARGET, TARGET)) == 4


def test_period_report_never_mixes_versions(paths, tmp_path):
    _setup_day(paths, {"1111": _bar(100, 104, 100, 103)})
    repository = _repository(tmp_path)
    run_day(repository, TARGET, "v1", paths)
    definition = json.loads(json.dumps(CRITERIA_V1))
    definition["version"] = "v2"
    definition["thresholds"]["open_to_close_min_pct"] = 5.0
    repository.register_version("v2", definition, TARGET, "テスト")
    run_day(repository, TARGET, "v2", paths)

    period = report.build_period_report(repository, "weekly", date(2026, 9, 7), date(2026, 9, 11), "w")
    assert period["version"] == "v2"
    assert period["aggregate"]["pooled_selected"]["trend"] == 0
    v1_stats = repository.load_rows("v1", "2026-09-07", "2026-09-11")
    assert all(row["version"] == "v1" for row in v1_stats)


def test_small_counts_are_marked_reference():
    rows = [{"in_selected": 1, "label": LABEL_TREND, "regime": "NORMAL", "trade_date": TARGET, "symbol": "1",
             "bought": 0, "pnl": None, "atr": None, "price": None, "rsi": None, "turnover_ratio": None}]
    summaries = [{
        "trade_date": TARGET, "selected_judged": 1, "selected_trend": 1, "selected_rate": 1.0,
        "candidate_rate": 1.0, "regime": "NORMAL", "universe_source": "x", "selected_undecidable": 0,
    }]
    agg = report.aggregate(rows, summaries)
    assert "参考" in "\n".join(report.conclusion_lines(agg))
    assert "※参考値" in report._stat_text(agg["pooled_selected"])

def test_recompute_all_and_reports(paths, tmp_path):
    _setup_day(paths, {"1111": _bar(100, 104, 100, 103)})
    _setup_day(paths, {"1111": None}, day="2026-10-05")
    repository = _repository(tmp_path)
    summaries = recompute_all(repository, "v1", paths)
    assert [s["trade_date"] for s in summaries] == [TARGET, "2026-10-05"]
    assert summaries[1]["selected_undecidable"] == 1

    output = tmp_path / "out"
    assert report.write_daily_report(repository, TARGET, output, "v1").exists()
    assert (output / "daily" / f"{TARGET}.csv").exists()
    assert report.write_recompute_report(repository, "v1", summaries, output).exists()
    monthly = report.write_period_report(
        repository, "monthly", date(2026, 9, 1), date(2026, 9, 30), "2026-09", output
    )
    assert "判定基準 v1" in monthly.read_text(encoding="utf-8")


def test_register_new_version_command(tmp_path):
    repository = _repository(tmp_path)
    definition = run_trend_check.register_new_version(
        repository, "v2", "しきい値見直し", "2026-10-07", {"open_to_close_min_pct": 2.0}
    )
    assert definition["thresholds"]["open_to_close_min_pct"] == 2.0
    assert repository.get_version("v1")["definition"]["thresholds"]["open_to_close_min_pct"] == 1.0
    with pytest.raises(ValueError):
        run_trend_check.register_new_version(repository, "v2", "重複", "2026-10-07", {})
    with pytest.raises(ValueError):
        run_trend_check.register_new_version(repository, "v3", "未知", "2026-10-07", {"unknown": 1})


def test_run_daily_safely_swallows_errors_and_notifies(paths, tmp_path):
    (paths.filtering_dir / f"{TARGET}.json").write_text("{", encoding="utf-8")
    messages = []
    ok = run_trend_check.run_daily_safely(
        date.fromisoformat(TARGET),
        refresh_cache=False,
        database_file=tmp_path / "t.sqlite3",
        output_dir=tmp_path / "out",
        paths=paths,
        notify_failure=messages.append,
    )
    assert ok is False
    assert len(messages) == 1


def test_run_daily_safely_skips_without_filtering_result(paths, tmp_path):
    messages = []
    assert run_trend_check.run_daily_safely(
        date.fromisoformat(TARGET), False, tmp_path / "t.sqlite3", tmp_path / "out", paths, messages.append
    )
    assert messages == []
    assert not (tmp_path / "t.sqlite3").exists()
