"""
================================================================================
FILTER_TURNOVER_MISSING の銘柄が 09:00〜09:30 に実際に売買されていたかをYahoo 1分足で確認する診断(読み取り専用)
- 本体コード・DB・設定は変更しません(結果JSONをanalysis配下へ保存するのみ)
- 判定: TRADED(出来高累計>0) / NO_VOLUME(足はあるが累計0) / NO_BARS(その日の足なし)
- 日付全体で足が1本も無い場合は UNAVAILABLE(Yahooが遡れない)、銘柄の取得失敗は FETCH_FAILED

使い方:
    python -m src.entrypoints.diagnose_missing_turnover_by_minute_bars
================================================================================
"""
import argparse
import json
import re
import zipfile
from datetime import datetime
from pathlib import Path

from src.config import config
from src.domain.minute_turnover_estimation import aggregate_window, calibrate, judge, stats as _stats
from src.infrastructure.market_data.get_intraday_bars import get_yahoo_intraday_bars

DEFAULT_DATES = ["2026-09-29", "2026-09-30", "2026-10-01", "2026-10-02", "2026-10-05"]
WINDOW_START = "09:00"
WINDOW_END = "09:30"  # 含まない
LAST_FIVE_START = "09:25"
_LINE = re.compile(r"評価対象外:\s*銘柄=(\S+)\s+理由=(.*)")

LOG_DIRECTORY = Path(config.SCREENING_RESULT_DIRECTORY).parent / "logs" / "jobs"
OUTPUT_DIRECTORY = Path(config.SCREENING_RESULT_DIRECTORY).parent / "analysis"


def parse_log_missing(path: Path, archive_member: str | None = None) -> dict[str, list[str]]:
    """UTF-16ログから評価対象外の銘柄を理由区分ごと(turnover / current_price / other)に返す。"""
    result: dict[str, list[str]] = {"turnover": [], "current_price": [], "other": []}
    if not path.exists():
        return result
    if archive_member is None:
        text = path.read_bytes().decode("utf-16", errors="replace")
    else:
        with zipfile.ZipFile(path, "r") as archive:
            if archive_member not in archive.namelist():
                return result
            text = archive.read(archive_member).decode("utf-16", errors="replace")
    for line in text.splitlines():
        match = _LINE.search(line)
        if not match:
            continue
        symbol, reason = match.group(1), match.group(2)
        if "当日売買代金" not in reason and "現在値" not in reason:
            continue
        key = "current_price" if "現在値" in reason else "turnover"
        if symbol not in result[key]:
            result[key].append(symbol)
    return result


def load_diagnostics(directory: Path, day: str) -> list[dict]:
    records: list[dict] = []
    for path in sorted(Path(directory).glob(f"{day}_*.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        if data.get("date") == day:
            records.extend(data.get("candidates", []))
    return records


def fetch_all(symbols, fetcher) -> tuple[dict[str, list], dict[str, str]]:
    """同一銘柄は1回だけ取得する。失敗(例外・空応答)は記録して続行する。"""
    bars_by_symbol: dict[str, list] = {}
    errors: dict[str, str] = {}
    for symbol in symbols:
        try:
            bars = fetcher(symbol)
        except Exception as exc:
            errors[symbol] = type(exc).__name__
            continue
        if not bars:
            errors[symbol] = "EMPTY_RESPONSE"
            continue
        bars_by_symbol[symbol] = bars
    return bars_by_symbol, errors


def build_report(targets: dict[str, dict], bars_by_symbol, errors, kabu_values: dict) -> dict:
    """targets: {日付: {"missing": [...], "current_price_missing": [...], "evaluated": [...]}}"""
    report = {}
    for day, groups in targets.items():
        day_available = any(
            any(b.time[:10] == day for b in bars) for bars in bars_by_symbol.values()
        )
        rows = []
        for group, symbols in groups.items():
            for symbol in symbols:
                row = {"symbol": symbol, "group": group}
                if symbol in errors:
                    row["verdict"] = "FETCH_FAILED"
                    row["error"] = errors[symbol]
                elif not day_available:
                    row["verdict"] = "UNAVAILABLE"
                else:
                    window = aggregate_window(bars_by_symbol[symbol], day)
                    row["verdict"] = judge(window)
                    row.update(window or {"bar_count": 0, "volume": None, "value_estimate": None,
                                          "last_five_volume": None})
                    kabu = kabu_values.get((day, symbol))
                    if kabu is not None and window and kabu:
                        row["kabu_numerator"] = kabu
                        row["yahoo_to_kabu_value_ratio"] = round(window["value_estimate"] / kabu, 3)
                rows.append(row)
        counts = {}
        for row in rows:
            if row["group"] == "evaluated":
                continue
            counts[row["verdict"]] = counts.get(row["verdict"], 0) + 1
        report[day] = {"day_available": day_available, "rows": rows, "counts": counts}
    return report


def format_report(report: dict) -> str:
    lines = []
    for day, data in report.items():
        lines.append(f"=== {day} ===" + ("" if data["day_available"] else "  [取得不可: Yahoo分足が遡れません]"))
        lines.append(f"{'symbol':<8} {'group':<22} {'verdict':<13} {'vol 9:00-9:30':>14} {'vol 9:25-9:30':>14} {'yahoo/kabu':>10}")
        for r in data["rows"]:
            lines.append(
                f"{r['symbol']:<8} {r['group']:<22} {r['verdict']:<13} {str(r.get('volume')):>14} "
                f"{str(r.get('last_five_volume')):>14} {str(r.get('yahoo_to_kabu_value_ratio', '')):>10}"
            )
        lines.append(f"集計(欠損のみ): {data['counts'] or 'なし'}")
        lines.append("")
    return "\n".join(lines)


def collect_targets(dates, log_directory, diagnostics_directory):
    targets: dict[str, dict] = {}
    kabu_values: dict = {}
    notes: list[str] = []
    for day in dates:
        log_name = f"run_filtering_stderr_{day}.log"
        month = day[:4] + day[5:7]
        local_month_log = Path(log_directory) / month / log_name
        legacy_log = Path(log_directory) / log_name
        archived_month = Path(config.LOG_ARCHIVE_DIRECTORY) / "jobs" / f"{month}.zip"
        if local_month_log.exists():
            log = parse_log_missing(local_month_log)
        elif legacy_log.exists():
            log = parse_log_missing(legacy_log)
        else:
            log = parse_log_missing(archived_month, archive_member=log_name)
        missing, price_missing = list(log["turnover"]), list(log["current_price"])
        evaluated: list[str] = []
        records = load_diagnostics(diagnostics_directory, day)
        if records:
            diag_missing = [r["symbol"] for r in records
                            if r.get("status") == "skipped" and r.get("reason_code") == "FILTER_TURNOVER_MISSING"]
            diag_price = [r["symbol"] for r in records
                          if r.get("status") == "skipped" and r.get("reason_code") == "FILTER_CURRENT_PRICE_MISSING"]
            # ログは両理由を同一文言で出すため、診断JSONがあればその区分を優先する
            log_all = set(missing) | set(price_missing)
            diag_all = set(diag_missing) | set(diag_price)
            notes.append(f"{day}: ログ{len(log_all)}件 / 診断JSON{len(diag_all)}件 / 一致={log_all == diag_all}")
            missing, price_missing = diag_missing, diag_price
            for r in records:
                if r.get("status") == "evaluated" and r.get("numerator") is not None:
                    evaluated.append(r["symbol"])
                    kabu_values[(day, r["symbol"])] = float(r["numerator"])
        targets[day] = {"missing": missing, "current_price_missing": price_missing, "evaluated": evaluated}
    return targets, kabu_values, notes


SIMULATION_NOTE = (
    "推定値です。Yahoo分足の売買代金概算は板の売買代金より中央値で約9%小さく、"
    "実際の判定(板の値)とは一致しません。"
)
DAILY_CACHE_DIRECTORY = Path(config.SCREENING_RESULT_DIRECTORY).parent / "cache" / "yahoo_daily"


def _previous_business_day(day: str) -> str:
    from datetime import date, timedelta
    from src.infrastructure.calendar.japanese_calendar import is_trading_day

    current = date.fromisoformat(day) - timedelta(days=1)
    while not is_trading_day(current):
        current -= timedelta(days=1)
    return current.isoformat()


def _read_symbols(path: Path) -> list[str] | None:
    if not path.exists():
        return None
    return [str(s) for s in json.loads(path.read_text(encoding="utf-8")).get("symbols", [])]


def load_population(day, screening_directory, filtering_directory, diagnostics_directory) -> dict | None:
    """その日のフィルタ入力(前営業日のスクリーニング結果)と実際の採用10件を返す。無ければ診断JSONへフォールバック。"""
    population = _read_symbols(Path(screening_directory) / f"{_previous_business_day(day)}.json")
    adopted = _read_symbols(Path(filtering_directory) / f"{day}.json")
    if population is not None and adopted is not None:
        return {"population": population, "adopted": adopted, "source": "data/screening + data/filtering"}
    records = load_diagnostics(diagnostics_directory, day)
    if records:
        return {
            "population": [str(r["symbol"]) for r in records],
            "adopted": [str(r["symbol"]) for r in records if r.get("selected")],
            "source": "FALLBACK: 診断JSON(スクリーニング/フィルタ結果ファイルなし)",
        }
    return None


def load_kabu_ratios(diagnostics_directory, day) -> dict[str, float]:
    return {
        str(r["symbol"]): float(r["ratio"])
        for r in load_diagnostics(diagnostics_directory, day)
        if r.get("status") == "evaluated" and r.get("ratio") is not None
    }


def _cached_daily_turnover(symbol: str, cache_directory: Path) -> dict[str, float]:
    path = Path(cache_directory) / f"{symbol}.json"
    if not path.exists():
        return {}
    raw = json.loads(path.read_text(encoding="utf-8"))
    return {
        day: float(v["close"]) * float(v["volume"])
        for day, v in raw.items()
        if v.get("close") is not None and v.get("volume") is not None
    }


def default_daily_turnover(symbol: str, cache_directory: Path = DAILY_CACHE_DIRECTORY) -> dict[str, float]:
    """Yahoo日足から日付別売買代金を取得する。失敗時は日足キャッシュ(読み取りのみ)から算出する。"""
    from src.infrastructure.market_data.yahoo_finance_client import YahooFinanceClient

    try:
        data = YahooFinanceClient()._get_daily_turnover(symbol, days=90)
        if data:
            return data
    except Exception:
        pass
    return _cached_daily_turnover(symbol, cache_directory)


def average_turnover_before(turnover_by_day: dict[str, float], day: str, days: int = 20) -> float | None:
    values = [v for d, v in sorted(turnover_by_day.items()) if d < day][-days:]
    average = sum(values) / len(values) if values else 0.0
    return average if average > 0 else None


def estimate_ratios(day, symbols, bars_by_symbol, turnover_by_symbol) -> tuple[dict[str, float], dict[str, str]]:
    """推定倍率(09:00〜09:30売買代金概算 / 20日平均売買代金)。推定不能な銘柄は理由つきで別に返す。"""
    ratios: dict[str, float] = {}
    unestimable: dict[str, str] = {}
    for symbol in symbols:
        bars = bars_by_symbol.get(symbol)
        if not bars:
            unestimable[symbol] = "分足なし"
            continue
        window = aggregate_window(bars, day)
        if window is None:
            unestimable[symbol] = "当日の分足なし"
            continue
        average = average_turnover_before(turnover_by_symbol.get(symbol) or {}, day)
        if average is None:
            unestimable[symbol] = "平均売買代金なし"
            continue
        ratios[symbol] = window["value_estimate"] / average
    return ratios, unestimable


def simulate_day(ratios: dict[str, float], unestimable: dict[str, str], adopted: list[str],
                 missing: set[str], top_n: int = 10) -> dict:
    """推定倍率で順位づけし、欠損銘柄が取れていた場合の上位との差を求める。"""
    ranked = sorted(ratios.items(), key=lambda item: (-item[1], item[0]))
    top = [symbol for symbol, _ in ranked[:top_n]]
    evaluated_ratios = {s: v for s, v in ratios.items() if s not in missing}
    missing_ratios = {s: v for s, v in ratios.items() if s in missing}
    adopted_ratios = [ratios[s] for s in adopted if s in ratios]
    return {
        "estimated_top": [{"symbol": s, "ratio": round(ratios[s], 3), "is_missing": s in missing,
                           "is_adopted": s in adopted} for s in top],
        "top_missing_count": sum(s in missing for s in top),
        "top_missing_symbols": [s for s in top if s in missing],
        "overlap_with_adopted": len(set(top) & set(adopted)),
        "adopted_count": len(adopted),
        "adopted_unestimable": [s for s in adopted if s not in ratios],
        "adopted_ratio_stats": _stats(adopted_ratios),
        "missing_ratio_stats": _stats(list(missing_ratios.values())),
        "evaluated_ratio_stats": _stats(list(evaluated_ratios.values())),
        "over_one_evaluated": sum(v >= 1.0 for v in evaluated_ratios.values()),
        "over_one_missing": sum(v >= 1.0 for v in missing_ratios.values()),
        "estimable_count": len(ratios),
        "unestimable_count": len(unestimable),
        "unestimable": unestimable,
    }


def run_simulation(dates, targets, fetch, daily_turnover, screening_directory, filtering_directory,
                   diagnostics_directory, bars_by_symbol=None) -> dict:
    populations = {}
    for day in dates:
        loaded = load_population(day, screening_directory, filtering_directory, diagnostics_directory)
        if loaded is not None:
            populations[day] = loaded
    needed = sorted({s for p in populations.values() for s in p["population"]})
    bars_by_symbol = dict(bars_by_symbol or {})
    to_fetch = [s for s in needed if s not in bars_by_symbol]
    fetched, errors = fetch_all(to_fetch, fetch)
    bars_by_symbol.update(fetched)
    turnover_by_symbol = {s: daily_turnover(s) for s in needed}

    result: dict = {"note": SIMULATION_NOTE, "fetch_errors": errors, "days": {}}
    for day in dates:
        loaded = populations.get(day)
        if loaded is None:
            result["days"][day] = {"skipped": "スクリーニング/フィルタ結果も診断JSONも無いため再現不可"}
            continue
        if not any(b.time[:10] == day for bars in bars_by_symbol.values() for b in bars):
            result["days"][day] = {"skipped": "取得不可: Yahoo分足が遡れません", "source": loaded["source"]}
            continue
        population = loaded["population"]
        groups = targets.get(day, {})
        missing = (set(groups.get("missing", [])) | set(groups.get("current_price_missing", []))) & set(population)
        ratios, unestimable = estimate_ratios(day, population, bars_by_symbol, turnover_by_symbol)
        day_result = simulate_day(ratios, unestimable, loaded["adopted"], missing)
        day_result.update({"source": loaded["source"], "population_count": len(population),
                           "missing_count": len(missing)})
        kabu_ratios = load_kabu_ratios(diagnostics_directory, day)
        if kabu_ratios:
            yahoo_values = {}
            for s in kabu_ratios:
                window = aggregate_window(bars_by_symbol.get(s, []), day)
                yahoo_values[s] = window["value_estimate"] if window else 0
            day_result["calibration"] = calibrate(ratios, kabu_ratios, yahoo_values)
        result["days"][day] = day_result
    return result


def format_simulation(sim: dict) -> str:
    lines = [f"[注意] {sim['note']}", ""]
    for day, d in sim["days"].items():
        lines.append(f"=== 上位10件の再現 {day} ===")
        if "skipped" in d:
            lines.extend([d["skipped"], ""])
            continue
        lines.append(f"母集団ソース: {d['source']} / 母集団{d['population_count']}件 / 欠損{d['missing_count']}件 / "
                     f"推定不能{d['unestimable_count']}件(除外)")
        lines.append(f"{'rank':>4} {'symbol':<8} {'est_ratio':>10} 欠損 採用")
        for rank, row in enumerate(d["estimated_top"], start=1):
            lines.append(f"{rank:>4} {row['symbol']:<8} {row['ratio']:>10.3f} {'*' if row['is_missing'] else ' ':>3} "
                         f"{'*' if row['is_adopted'] else ' ':>3}")
        lines.append(f"推定上位10件のうち欠損銘柄: {d['top_missing_count']}件 {d['top_missing_symbols']}")
        lines.append(f"推定上位10件と実際の採用{d['adopted_count']}件の重なり: {d['overlap_with_adopted']}件")
        lines.append(f"採用銘柄の推定倍率: {d['adopted_ratio_stats']} (推定不能の採用: {d['adopted_unestimable']})")
        lines.append(f"欠損銘柄の推定倍率: {d['missing_ratio_stats']}")
        lines.append(f"評価できた銘柄の推定倍率: {d['evaluated_ratio_stats']}")
        lines.append(f"推定倍率>=1.0: 評価できた{d['over_one_evaluated']}件 / 欠損{d['over_one_missing']}件")
        cal = d.get("calibration")
        if cal:
            lines.append(f"校正(推定/板の実倍率): {cal['stats']} / Yahoo売買代金0で別枠: {cal['zero_yahoo_value_symbols']}")
        lines.append("")
    return "\n".join(lines)


def main(argv=None, fetcher=None, log_directory=None, diagnostics_directory=None, output=None,
         screening_directory=None, filtering_directory=None, daily_turnover=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dates", default=",".join(DEFAULT_DATES), help="カンマ区切りの対象日")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--no-simulation", action="store_true", help="上位10件の再現を行わない")
    args = parser.parse_args(argv)
    dates = [d.strip() for d in args.dates.split(",") if d.strip()]
    diagnostics_directory = diagnostics_directory or config.FILTERING_DIAGNOSTICS_DIRECTORY

    targets, kabu_values, notes = collect_targets(dates, log_directory or LOG_DIRECTORY, diagnostics_directory)
    symbols = sorted({s for groups in targets.values() for symbols in groups.values() for s in symbols})
    print(f"[情報] 対象銘柄 {len(symbols)}件をYahoo 1分足(7日分)で取得します")
    fetch = fetcher or (lambda symbol: get_yahoo_intraday_bars(symbol, days=7, interval="1m"))
    bars_by_symbol, errors = fetch_all(symbols, fetch)

    report = build_report(targets, bars_by_symbol, errors, kabu_values)
    for note in notes:
        print(f"[情報] {note}")
    print(format_report(report))
    if errors:
        print(f"[警告] 取得失敗/空応答: {errors}")

    payload = {"notes": notes, "fetch_errors": errors, "report": report}
    if not args.no_simulation:
        simulation = run_simulation(
            dates, targets, fetch, daily_turnover or default_daily_turnover,
            screening_directory or config.SCREENING_RESULT_DIRECTORY,
            filtering_directory or config.FILTERING_RESULT_DIRECTORY,
            diagnostics_directory, bars_by_symbol,
        )
        print(format_simulation(simulation))
        payload["simulation"] = simulation

    out = output or args.output or OUTPUT_DIRECTORY / f"missing_turnover_minute_bars_{datetime.now():%Y%m%d_%H%M%S}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[情報] JSON保存: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
