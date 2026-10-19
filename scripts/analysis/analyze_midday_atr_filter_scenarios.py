"""昼推定候補への選定時ATRフィルタ適用を仮想比較する読み取り専用分析。"""
from __future__ import annotations

import argparse
import csv
import json
import math
import statistics
import sys
from datetime import date
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.analysis.analyze_price_band_comparison import (
    BAND_ORDER,
    MIN_REFERENCE_SAMPLE,
    _daily_bars_before,
    _market_regime_for_day,
    _read_minute_prices,
    _write_csv,
    analyze_candidate_day,
    analyze_screening_candidate_day,
    analyze_selection_atr_candidate,
    dates_in_range,
    discover_json_dates,
    load_daily_caches,
    load_midday_selections,
    load_screening_candidates,
    minutes_from_time,
    summarize_selection_atr_source_bands,
)
from src.config import config
from src.domain.market_regime import MarketRegime
from src.domain.volatility import VolatilityLevel, assess_volatility

SCENARIOS = {
    "A": {"label": "現行倍率上位10", "threshold": None},
    "B": {"label": "ATR比2.0未満から倍率上位10", "threshold": 2.0},
    "C": {"label": "ATR比1.5未満から倍率上位10", "threshold": 1.5},
}
DEFAULT_START = date(2026, 10, 13)
DEFAULT_END = date(2026, 10, 16)


def select_scenario_symbols(
    candidates: dict[str, dict[str, Any]],
    adopted_symbols: list[str],
    atr_ratios: dict[str, float | None],
    scenario: str,
) -> dict[str, Any]:
    """診断順位と選定時ATR比からシナリオの最大10銘柄を選ぶ。"""
    if scenario not in SCENARIOS:
        raise ValueError(f"未知のシナリオです: {scenario}")
    input_count = len(candidates)
    atr_evaluable = sum(atr_ratios.get(symbol) is not None for symbol in candidates)
    multiplier_ranked = sum(
        isinstance(item.get("rank"), (int, float))
        and math.isfinite(float(item["rank"]))
        and isinstance(item.get("ratio"), (int, float))
        and math.isfinite(float(item["ratio"]))
        for item in candidates.values()
    )
    threshold = SCENARIOS[scenario]["threshold"]
    if threshold is None:
        symbols = list(dict.fromkeys(
            symbol for symbol in adopted_symbols if symbol in candidates
        ))[:10]
        eligible_count = input_count
    else:
        eligible = [
            (symbol, item)
            for symbol, item in candidates.items()
            if atr_ratios.get(symbol) is not None
            and atr_ratios[symbol] < threshold
            and isinstance(item.get("rank"), (int, float))
            and math.isfinite(float(item["rank"]))
        ]
        eligible.sort(key=lambda pair: (float(pair[1]["rank"]), pair[0]))
        eligible_count = len(eligible)
        symbols = [symbol for symbol, _ in eligible[:10]]
    return {
        "symbols": symbols,
        "input_count": input_count,
        "input_atr_evaluable": atr_evaluable,
        "input_multiplier_ranked": multiplier_ranked,
        "eligible_count": eligible_count,
        "shortfall_count": max(0, 10 - len(symbols)),
    }


def calculate_preceding_atr_ratios(
    candidates: dict[str, dict[str, Any]],
    trade_date: str,
    daily_caches: dict[str, dict[str, Any]],
) -> dict[str, float | None]:
    """昼判定日より前に確定した日足のみでATR比を算出する。"""
    result: dict[str, float | None] = {}
    for symbol in candidates:
        bars = _daily_bars_before(daily_caches.get(symbol, {}), trade_date)
        assessment = assess_volatility(
            [bar for _, bar in bars],
            config.ATR_PERIOD,
            config.ATR_CAUTION_RATIO,
            config.ATR_DANGER_RATIO,
        )
        result[symbol] = assessment.ratio if assessment else None
    return result


def summarize_scenario_day(
    rows: list[dict[str, Any]],
    context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """選定・分足欠損・シグナル・見送り・仮想損益を日別に集計する。"""
    first = rows[0] if rows else context
    if first is None:
        raise ValueError("空集計にはシナリオ・日付・選定メタデータが必要です")
    post_signals = [row for row in rows if row.get("post_signal") is True]
    close_atr_values = [
        float(row["close_atr"])
        for row in post_signals
        if row.get("close_atr") is not None
    ]
    reasons = {
        "caution_rsi": 0,
        "atr_danger": 0,
        "other": 0,
        "unavailable": 0,
    }
    for row in rows:
        if not row.get("pre_signal"):
            continue
        reason = row.get("rejection_category")
        if reason in reasons:
            reasons[reason] += 1
    return {
        "scenario": first["scenario"],
        "scenario_label": SCENARIOS[first["scenario"]]["label"],
        "band": first["band"],
        "date": first["date"],
        "input_count": first["input_count"],
        "input_atr_evaluable": first["input_atr_evaluable"],
        "input_multiplier_ranked": first.get("input_multiplier_ranked", 0),
        "input_atr_below_2_count": first["input_atr_below_2_count"],
        "input_normal_count": first["input_normal_count"],
        "input_no_minute_count": first["input_no_minute_count"],
        "eligible_count": first["eligible_count"],
        "selected_count": len(rows),
        "selected_with_minute_count": sum(row["minute_available"] for row in rows),
        "selected_missing_minute_count": sum(not row["minute_available"] for row in rows),
        "signal_evaluated_count": sum(row.get("signal_evaluated", False) for row in rows),
        "pre_signal_count": sum(row.get("pre_signal", False) for row in rows),
        "post_signal_count": len(post_signals),
        "post_signal_unavailable_count": reasons["unavailable"],
        "skip_caution_rsi": reasons["caution_rsi"],
        "skip_atr_danger": reasons["atr_danger"],
        "skip_other": reasons["other"],
        "post_virtual_net_pnl_100_sum": sum(
            float(row.get("virtual_net_pnl_100") or 0.0)
            for row in post_signals
        ),
        "post_virtual_net_pnl_100_n": sum(
            row.get("virtual_net_pnl_100") is not None for row in post_signals
        ),
        "post_close_atr_n": len(close_atr_values),
        "post_close_atr_median": (
            statistics.median(close_atr_values) if close_atr_values else None
        ),
        "post_close_atr_p25": percentile(close_atr_values, 0.25),
        "post_close_atr_p75": percentile(close_atr_values, 0.75),
        "replaced_from_A_count": first["replaced_from_A_count"],
        "replaced_from_A_symbols": ",".join(first["replaced_from_A_symbols"]),
        "added_vs_A_symbols": ",".join(first["added_vs_A_symbols"]),
        "selection_shortfall_count": first["selection_shortfall_count"],
    }


def summarize_scenario_totals(
    daily_rows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """対象期間を案・帯ごとに合算する。"""
    summaries = []
    for scenario in SCENARIOS:
        for band in BAND_ORDER:
            members = [
                row for row in daily_rows
                if row["scenario"] == scenario and row["band"] == band
            ]
            if not members:
                continue
            summaries.append({
                "scenario": scenario,
                "scenario_label": SCENARIOS[scenario]["label"],
                "band": band,
                "day_count": len(members),
                "pre_signal_count": sum(row["pre_signal_count"] for row in members),
                "post_signal_count": sum(row["post_signal_count"] for row in members),
                "post_signal_unavailable_count": sum(
                    row["post_signal_unavailable_count"] for row in members
                ),
                "skip_caution_rsi": sum(row["skip_caution_rsi"] for row in members),
                "skip_atr_danger": sum(row["skip_atr_danger"] for row in members),
                "skip_other": sum(row["skip_other"] for row in members),
                "post_virtual_net_pnl_100_sum": sum(
                    row["post_virtual_net_pnl_100_sum"] for row in members
                ),
                "post_virtual_net_pnl_100_n": sum(
                    row["post_virtual_net_pnl_100_n"] for row in members
                ),
                "replaced_from_A_count": sum(
                    row["replaced_from_A_count"] for row in members
                ),
                "selection_shortfall_count": sum(
                    row["selection_shortfall_count"] for row in members
                ),
            })
    return summaries


def percentile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    if len(values) == 1:
        return values[0]
    ordered = sorted(values)
    position = (len(ordered) - 1) * fraction
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    weight = position - lower
    return ordered[lower] * (1 - weight) + ordered[upper] * weight


def summarize_normal_danger_benchmark(
    screening_rows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """候補翌営業日のNORMAL/DANGER値幅を価格帯別に比較する。"""
    summaries = summarize_selection_atr_source_bands(screening_rows)
    return [
        {
            "screening_band": item["screening_band"],
            "atr_level": item["selection_atr_level"],
            "sample_count": item["candidate_count"],
            **{
                key: item[key]
                for key in (
                    "daily_range_atr_median", "daily_range_atr_p25",
                    "daily_range_atr_p75", "open_max_up_atr_median",
                    "open_max_up_atr_p25", "open_max_up_atr_p75",
                    "open_max_down_atr_median", "open_max_down_atr_p25",
                    "open_max_down_atr_p75", "close_change_atr_median",
                    "close_change_atr_p25", "close_change_atr_p75",
                )
            },
            "reference_value": item["candidate_count"] < MIN_REFERENCE_SAMPLE,
        }
        for item in summaries
        if item["selection_atr_level"] in (
            VolatilityLevel.NORMAL.value, VolatilityLevel.DANGER.value
        )
    ]


def _read_index_caches(daily_cache_dir: Path, issues: list[str]) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for symbol in ("^N225", "^VIX"):
        path = daily_cache_dir / f"{symbol}.json"
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(payload, dict):
                raise ValueError("JSONルートがオブジェクトではありません")
        except (OSError, json.JSONDecodeError, ValueError) as exc:
            issues.append(f"{symbol}: 指数キャッシュを読めません ({type(exc).__name__})")
            result[symbol] = {}
        else:
            result[symbol] = payload
    return result


def _build_report(
    start_date: date,
    end_date: date,
    daily_rows: list[dict[str, Any]],
    total_rows: list[dict[str, Any]],
    benchmark_rows: list[dict[str, Any]],
    missing_rows: list[dict[str, Any]],
    issues: list[str],
) -> str:
    input_days = {
        (row["band"], row["date"]): row["input_count"]
        for row in daily_rows
        if row["scenario"] == "A"
    }
    total_inputs = sum(input_days.values())
    input_missing_count = sum(row["scenario"] == "INPUT" for row in missing_rows)
    lines = [
        f"# 昼推定選定へのATR比フィルタ仮想比較（{start_date}〜{end_date}）",
        "",
        "## この分析で言えること / 言えないこと",
        f"- 対象期間の昼推定診断から入力銘柄日 **{total_inputs}件**を読み、診断の倍率順位と選定日前に確定した日足ATR比でA/B/Cを比較しました。",
        "- ATR比は直近確定日足TR÷同日までの直近ATR(14)。判定日以降の日足は選定条件に使っていません。値動きとシグナルは09:35以降の分足だけで評価しています。",
        "- 診断入力50銘柄のうち倍率と順位を取得できない銘柄は50件の入力数には含みますが、B/Cの再選定候補には含めず、日別に倍率順位あり件数を記録します。倍率順位は診断順位と照合し、rank順がratio降順と一致することを確認しました。",
        "- B/CはATR条件を満たす候補のうち診断順位（倍率上位）から最大10銘柄を採用します。条件を満たす候補が10未満なら全候補を採り、不足数を記録します。",
        "- 9:35以降の分足がない選定銘柄はシグナル集計から除外し、入力50銘柄全体と実選定銘柄の欠損をCSVに記録しています。",
        "- これは同じ入力銘柄群の仮想比較で、戦略設定を変えた実績・将来の収益性・実約定を示すものではありません。全てのA/B/C日別サンプルは30件未満の参考値です。",
        "",
        "### 期間合計（シグナル数の増減）",
        "",
        "| 案 | 帯 | 比較日数 | 見送り前 | 見送り後 | RSI見送り | ATR DANGER | その他 | 判定不可 | 見送り後損益(円,n) | Aからの交代銘柄日 | 選定不足合計 |",
        "|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for row in total_rows:
        pnl_text = (
            f"{row['post_virtual_net_pnl_100_sum']:.0f} "
            f"(n={row['post_virtual_net_pnl_100_n']})"
        )
        if row["post_virtual_net_pnl_100_n"] < MIN_REFERENCE_SAMPLE:
            pnl_text += "・参考値"
        lines.append(
            f"| {row['scenario']} {row['scenario_label']} | {row['band']} "
            f"| {row['day_count']} | {row['pre_signal_count']} "
            f"| {row['post_signal_count']} | {row['skip_caution_rsi']} "
            f"| {row['skip_atr_danger']} | {row['skip_other']} "
            f"| {row['post_signal_unavailable_count']} | {pnl_text} "
            f"| {row['replaced_from_A_count']} | {row['selection_shortfall_count']} |"
        )
    lines.extend([
        "",
        "## 帯別・日別シナリオ比較",
        "",
        "| 案 | 帯 | 日付 | 入力数 | ATR評価可 | 倍率順位あり | ATR<2 | NORMAL | 入力分足欠損 | 条件通過数 | 選定数 | 選定分足あり/欠損 | 前シグナル | 見送り後 | RSI見送り | ATR DANGER | その他 | 判定不可 | 見送り後損益(円,n) | 引け騰落/ATR 中央値[25%,75%] | Aから交代(流出/流入) | 選定不足 |",
        "|---|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|---:|",
    ])
    for row in daily_rows:
        pnl = (
            f"{row['post_virtual_net_pnl_100_sum']:.0f} "
            f"(n={row['post_virtual_net_pnl_100_n']})"
        )
        if row["post_virtual_net_pnl_100_n"] < MIN_REFERENCE_SAMPLE:
            pnl += "・参考値"
        if row["post_close_atr_median"] is None:
            close_text = f"— (n={row['post_close_atr_n']})"
        else:
            close_text = (
                f"{row['post_close_atr_median']:.2f} "
                f"[{row['post_close_atr_p25']:.2f}, {row['post_close_atr_p75']:.2f}] "
                f"(n={row['post_close_atr_n']}・参考値)"
            )
        lines.append(
            f"| {row['scenario']} {row['scenario_label']} | {row['band']} | {row['date']} "
            f"| {row['input_count']} | {row['input_atr_evaluable']} "
            f"| {row['input_multiplier_ranked']} "
            f"| {row['input_atr_below_2_count']} | {row['input_normal_count']} "
            f"| {row['input_no_minute_count']} | {row['eligible_count']} "
            f"| {row['selected_count']} "
            f"| {row['selected_with_minute_count']}/{row['selected_missing_minute_count']} "
            f"| {row['pre_signal_count']} | {row['post_signal_count']} "
            f"| {row['skip_caution_rsi']} | {row['skip_atr_danger']} "
            f"| {row['skip_other']} | {row['post_signal_unavailable_count']} "
            f"| {pnl} | {close_text} "
            f"| {row['replaced_from_A_count']} "
            f"({row['replaced_from_A_symbols'] or '—'}/{row['added_vs_A_symbols'] or '—'}) "
            f"| {row['selection_shortfall_count']} |"
        )
    lines.extend([
        "",
        "## NORMAL/DANGERの翌営業日値幅（比較用）",
        "",
        "3帯のスクリーニング候補を母集団として、選定日翌営業日のOHLC値幅を選定日ATRで正規化した参考比較です。昼入力50銘柄のシナリオ結果とは母集団が異なります。",
        "",
        "| スクリーニング帯 | ATR区分 | n | 日中値幅/ATR 中央値[25%,75%] | 始値から最大上昇/ATR | 始値から最大下落/ATR | 引け騰落/ATR |",
        "|---|---|---:|---:|---:|---:|---:|",
    ])
    for row in benchmark_rows:
        marker = "・参考値" if row["sample_count"] < MIN_REFERENCE_SAMPLE else ""

        def metric_text(prefix: str) -> str:
            median = row[f"{prefix}_median"]
            if median is None:
                return f"— (n=0{marker})"
            return (
                f"{median:.2f} [{row[f'{prefix}_p25']:.2f}, "
                f"{row[f'{prefix}_p75']:.2f}] (n={row['sample_count']}{marker})"
            )

        lines.append(
            f"| {row['screening_band']} | {row['atr_level']} "
            f"| {row['sample_count']}{marker} "
            f"| {metric_text('daily_range_atr')} "
            f"| {metric_text('open_max_up_atr')} "
            f"| {metric_text('open_max_down_atr')} "
            f"| {metric_text('close_change_atr')} |"
        )
    lines.extend([
        "",
        "## 欠損・不整合",
        "",
        f"- 分足(09:35以降)がない入力銘柄日: **{input_missing_count}件**。シナリオ別の選定銘柄欠損を含む詳細は `missing_minute_inputs.csv` と `scenario_symbols.csv` を参照してください。",
        "- 各A/B/Cの選定銘柄から分足欠損を除外しました。欠損を0リターンまたは0シグナルとして扱っていません。",
        f"- 読み込み・データ品質の注意: **{len(issues)}件**。",
    ])
    lines.extend(f"  - {issue}" for issue in issues)
    lines.extend([
        "",
        "## 前回の「分子/ATR評価可能数」の定義",
        "",
        "前回の残存数表の分子は、当該日・当該帯で選定時ATR比が算出でき、かつ閾値（ATR比<2.0）を満たした銘柄数です。分母はATR比を評価できた銘柄数で、昼採用銘柄は選定10銘柄のうちATR比を評価できた数です。ATR比が不明な銘柄は分子にも分母にも含めません。",
        "",
        "## 10/13〜10/23の再実行",
        "",
        "10/19〜10/23の診断・分足・日足キャッシュが揃った後に、次を実行すると同じ分析を累積できます（出力先は期間別の新規フォルダ）。",
        "",
        "```powershell",
        "& .\\.venv\\Scripts\\python.exe scripts\\analysis\\analyze_midday_atr_filter_scenarios.py --start-date 2026-10-13 --end-date 2026-10-23",
        "```",
    ])
    return "\n".join(lines) + "\n"


def analyze(
    project_root: Path,
    output_dir: Path,
    start_date: date,
    end_date: date,
) -> dict[str, Any]:
    if start_date > end_date:
        raise ValueError("開始日は終了日以前にしてください")
    diagnostics_dirs = [
        project_root / "data" / "filtering_midday" / folder / "diagnostics"
        for folder in ("270", "450", "900")
    ]
    dates = tuple(dates_in_range(
        discover_json_dates(diagnostics_dirs), start_date, end_date
    ))
    selections, issues = load_midday_selections(project_root, dates)
    input_symbols = {
        symbol
        for by_date in selections.values()
        for selection in by_date.values()
        for symbol in selection["candidates"]
    }
    daily_cache_dir = project_root / "data" / "cache" / "yahoo_daily"
    daily_caches, cache_issues = load_daily_caches(input_symbols, daily_cache_dir)
    issues.extend(cache_issues)
    minute_prices = _read_minute_prices(
        project_root / "data" / "minute_bars_parquet",
        list(dates),
        input_symbols,
    )
    index_caches = _read_index_caches(daily_cache_dir, issues)
    regimes = {
        day: _market_regime_for_day(day, index_caches)
        for day in dates
    }

    daily_rows: list[dict[str, Any]] = []
    symbol_rows: list[dict[str, Any]] = []
    missing_rows: list[dict[str, Any]] = []
    for band in BAND_ORDER:
        for trade_date in dates:
            selection = selections.get(band, {}).get(trade_date)
            if selection is None:
                continue
            candidates = selection["candidates"]
            adopted = selection["symbols"]
            atr_ratios = calculate_preceding_atr_ratios(
                candidates, trade_date, daily_caches
            )
            after_935_by_symbol = {
                symbol: minutes_from_time(
                    minute_prices.get((symbol, trade_date), []), "09:35"
                )
                for symbol in candidates
            }
            input_no_minute = [
                symbol for symbol, bars in after_935_by_symbol.items() if not bars
            ]
            input_atr_below_2_count = sum(
                ratio is not None and ratio < 2.0
                for ratio in atr_ratios.values()
            )
            input_normal_count = sum(
                ratio is not None and ratio < config.ATR_CAUTION_RATIO
                for ratio in atr_ratios.values()
            )
            selections_by_scenario = {
                scenario: select_scenario_symbols(
                    candidates, adopted, atr_ratios, scenario
                )
                for scenario in SCENARIOS
            }
            baseline = set(selections_by_scenario["A"]["symbols"])
            for scenario, chosen in selections_by_scenario.items():
                selected_symbols = chosen["symbols"]
                removed = sorted(baseline - set(selected_symbols))
                added = sorted(set(selected_symbols) - baseline)
                regimes_for_day = regimes.get(
                    trade_date, (MarketRegime.NORMAL, False, "判定不可")
                )
                regime, regime_available, regime_reason = regimes_for_day
                input_stats = {
                    "input_count": len(candidates),
                    "input_atr_evaluable": chosen["input_atr_evaluable"],
                    "input_multiplier_ranked": chosen["input_multiplier_ranked"],
                    "input_atr_below_2_count": input_atr_below_2_count,
                    "input_normal_count": input_normal_count,
                    "input_no_minute_count": len(input_no_minute),
                    "eligible_count": chosen["eligible_count"],
                    "replaced_from_A_count": len(removed),
                    "replaced_from_A_symbols": removed,
                    "added_vs_A_symbols": added,
                    "selection_shortfall_count": chosen["shortfall_count"],
                }
                scenario_rows: list[dict[str, Any]] = []
                for symbol in selected_symbols:
                    candidate = candidates[symbol]
                    ratio = atr_ratios.get(symbol)
                    rank = candidate.get("rank")
                    minute_bars = after_935_by_symbol.get(symbol, [])
                    minute_available = bool(minute_bars)
                    row = {
                        "scenario": scenario,
                        "band": band,
                        "date": trade_date,
                        "symbol": symbol,
                        "diagnostic_rank": rank,
                        "multiplier": candidate.get("ratio"),
                        "selection_atr_ratio": ratio,
                        "selection_atr_level": (
                            "DANGER" if ratio is not None and ratio >= 2.0
                            else "CAUTION" if ratio is not None and ratio >= 1.5
                            else "NORMAL" if ratio is not None else None
                        ),
                        "minute_available": minute_available,
                        "minute_bar_count_after_935": len(minute_bars),
                        "input_no_minute": symbol in input_no_minute,
                        **input_stats,
                    }
                    if minute_available:
                        analyzed = analyze_candidate_day(
                            band,
                            symbol,
                            trade_date,
                            minute_bars,
                            daily_caches.get(symbol),
                            regime,
                            regime_available,
                            regime_reason,
                        )
                        row.update(analyzed)
                        row["scenario"] = scenario
                        row["band"] = band
                        row["date"] = trade_date
                        row["symbol"] = symbol
                        row["multiplier"] = candidate.get("ratio")
                        row["selection_atr_ratio"] = ratio
                        row["selection_atr_level"] = (
                            "DANGER" if ratio is not None and ratio >= 2.0
                            else "CAUTION" if ratio is not None and ratio >= 1.5
                            else "NORMAL" if ratio is not None else None
                        )
                        row["minute_available"] = True
                        row["minute_bar_count_after_935"] = len(minute_bars)
                        row["input_no_minute"] = False
                        row.update(input_stats)
                    else:
                        missing_rows.append({
                            "scenario": scenario,
                            "band": band,
                            "date": trade_date,
                            "symbol": symbol,
                            "diagnostic_rank": rank,
                            "multiplier": candidate.get("ratio"),
                            "selection_atr_ratio": ratio,
                            "input_no_minute": True,
                            "scenario_selected": True,
                        })
                    scenario_rows.append(row)
                    symbol_rows.append(row)
                daily_rows.append(summarize_scenario_day(
                    scenario_rows,
                    {
                        "scenario": scenario,
                        "band": band,
                        "date": trade_date,
                        **input_stats,
                    },
                ))
            for symbol in input_no_minute:
                missing_rows.append({
                    "scenario": "INPUT",
                    "band": band,
                    "date": trade_date,
                    "symbol": symbol,
                    "diagnostic_rank": candidates[symbol].get("rank"),
                    "multiplier": candidates[symbol].get("ratio"),
                    "selection_atr_ratio": atr_ratios.get(symbol),
                    "input_no_minute": True,
                    "scenario_selected": False,
                })

    screening_candidates, screening_issues = load_screening_candidates(
        project_root, dates
    )
    issues.extend(screening_issues)
    screening_rows = [
        analyze_selection_atr_candidate(
            candidate,
            daily_caches.get(candidate["symbol"]),
            analyze_screening_candidate_day(
                candidate, daily_caches.get(candidate["symbol"])
            ),
        )
        for candidate in screening_candidates
    ]
    benchmark_rows = summarize_normal_danger_benchmark(screening_rows)
    total_rows = summarize_scenario_totals(daily_rows)

    output_dir.mkdir(parents=True, exist_ok=True)
    _write_csv(output_dir / "scenario_daily.csv", daily_rows)
    _write_csv(output_dir / "scenario_totals.csv", total_rows)
    _write_csv(output_dir / "scenario_symbols.csv", symbol_rows)
    _write_csv(output_dir / "missing_minute_inputs.csv", missing_rows)
    _write_csv(output_dir / "normal_danger_next_day.csv", benchmark_rows)
    report = _build_report(
        start_date, end_date, daily_rows, total_rows, benchmark_rows,
        missing_rows, issues
    )
    (output_dir / "report.md").write_text(report, encoding="utf-8")
    return {
        "dates": list(dates),
        "daily_rows": daily_rows,
        "total_rows": total_rows,
        "symbol_rows": symbol_rows,
        "missing_rows": missing_rows,
        "benchmark_rows": benchmark_rows,
        "issues": issues,
        "output_dir": output_dir,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start-date", type=date.fromisoformat, default=DEFAULT_START)
    parser.add_argument("--end-date", type=date.fromisoformat, default=DEFAULT_END)
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=None,
        help="出力先。省略時は期間名に応じたanalysis_output配下の新規フォルダ",
    )
    args = parser.parse_args()
    output_dir = args.output_dir or (
        PROJECT_ROOT / "data" / "analysis_output"
        / f"midday_atr_filter_scenarios_{args.start_date}_{args.end_date}"
    )
    result = analyze(PROJECT_ROOT, output_dir, args.start_date, args.end_date)
    print(f"診断対象日: {', '.join(result['dates']) or 'なし'}")
    print(f"日別比較: {len(result['daily_rows'])}件")
    print(
        "分足欠損入力: "
        f"{sum(row['scenario'] == 'INPUT' for row in result['missing_rows'])}件"
    )
    print(f"注意事項: {len(result['issues'])}件")
    print(f"出力先: {output_dir}")


if __name__ == "__main__":
    main()
