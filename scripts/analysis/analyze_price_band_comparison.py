"""3価格帯の実運用エントリー条件と成立後の値動きを比較する読み取り専用分析。"""
from __future__ import annotations

import argparse
import csv
import json
import math
import statistics
import sys
from calendar import monthrange
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.application.market_regime_usecase import MarketRegimeUseCase
from src.config import config
from src.domain.enums import OrderSide
from src.domain.market_regime import MarketRegime, resolve_rsi_entry_threshold
from src.domain.market_volatility import MarketDailyBar
from src.domain.models import TradeSignal
from src.domain.volatility import DailyBar, VolatilityLevel, assess_volatility
from src.domain.rules import calculate_price_limit, calculate_rsi
from src.infrastructure.calendar.japanese_calendar import is_trading_day
from src.entrypoints.update_daily_bar_cache import latest_confirmed_trading_day


BAND_INPUTS = {
    "270円": "data/filtering",
    "450円": "data/filtering_price_bands/450",
    "900円": "data/filtering_price_bands/900",
}
BAND_ORDER = tuple(BAND_INPUTS)
SCREENING_INPUTS = {
    "270円": "data/screening",
    "450円": "data/screening_price_bands/450",
    "900円": "data/screening_price_bands/900",
}
OUTPUT_DIR = PROJECT_ROOT / "data" / "analysis_output" / "price_band_comparison_extended_2026-10-18"
MIN_REFERENCE_SAMPLE = 30
DAILY_CLOSE_LOOKBACK_DAYS = 60
A_COMPARISON_DATES = (
    "2026-10-05", "2026-10-06", "2026-10-07", "2026-10-08", "2026-10-09",
    "2026-10-13", "2026-10-14", "2026-10-15", "2026-10-16",
)
B_COMPARISON_DATES = ("2026-10-13", "2026-10-14", "2026-10-15", "2026-10-16")
VIRTUAL_ORDER_CAPS = (30_000, 50_000, 100_000)
ATR_DANGER_SCALE_RATIOS = (0.75, 0.5, 0.25)
PROFIT_LOCK_MULTIPLIERS = {
    VolatilityLevel.NORMAL: config.ATR_PROFIT_LOCK_NORMAL_MULTIPLIER,
    VolatilityLevel.CAUTION: config.ATR_PROFIT_LOCK_CAUTION_MULTIPLIER,
    VolatilityLevel.DANGER: config.ATR_PROFIT_LOCK_DANGER_MULTIPLIER,
}


def _subtract_months(target: date, months: int) -> date:
    month_index = target.year * 12 + target.month - 1 - months
    year, month_zero_based = divmod(month_index, 12)
    month = month_zero_based + 1
    return date(year, month, min(target.day, monthrange(year, month)[1]))


def load_filter_results(
    project_root: Path = PROJECT_ROOT,
) -> tuple[dict[str, dict[str, list[str]]], list[str]]:
    """日別フィルタ結果を読み込み、形式不整合を明示して返す。"""
    results: dict[str, dict[str, list[str]]] = {}
    issues: list[str] = []
    for band, relative_dir in BAND_INPUTS.items():
        directory = project_root / relative_dir
        by_date: dict[str, list[str]] = {}
        if not directory.is_dir():
            issues.append(f"{band}: フィルタ結果ディレクトリがありません ({directory})")
            results[band] = by_date
            continue
        for path in sorted(directory.glob("*.json")):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                issues.append(f"{band}: {path.name} を読めません ({type(exc).__name__})")
                continue
            if not isinstance(payload, dict):
                issues.append(f"{band}: {path.name} のJSONがオブジェクトではありません")
                continue
            trade_date = payload.get("date")
            symbols = payload.get("symbols")
            if not isinstance(trade_date, str) or not isinstance(symbols, list):
                issues.append(f"{band}: {path.name} にdateまたはsymbolsがありません")
                continue
            if trade_date != path.stem:
                issues.append(
                    f"{band}: {path.name} のdate={trade_date}がファイル名と一致しません"
                )
                continue
            if any(not isinstance(symbol, (str, int)) for symbol in symbols):
                issues.append(f"{band}: {path.name} のsymbolsに不正な値があります")
                continue
            if trade_date in by_date:
                issues.append(f"{band}: {trade_date} の結果が重複しています")
                continue
            by_date[trade_date] = list(dict.fromkeys(str(symbol) for symbol in symbols))
        results[band] = by_date
    return results, issues


def common_dates(results: dict[str, dict[str, list[str]]]) -> list[str]:
    """全価格帯に結果ファイルが存在する日付のみ返す。"""
    date_sets = [set(results.get(band, {})) for band in BAND_ORDER]
    return sorted(set.intersection(*date_sets)) if date_sets else []


def discover_json_dates(directories: list[Path]) -> list[str]:
    """指定ディレクトリ群に存在するISO日付JSONのファイル名を統合する。"""
    return sorted({
        path.name[:10]
        for directory in directories
        if directory.is_dir()
        for path in directory.glob("*.json")
        if len(path.name) >= 10 and _is_iso_date(path.name[:10])
    })


def _is_iso_date(value: str) -> bool:
    try:
        return date.fromisoformat(value).isoformat() == value
    except ValueError:
        return False


def dates_in_range(
    dates: list[str], start_date: date | None, end_date: date | None
) -> list[str]:
    """日付一覧を任意の両端を含む期間で絞り込む。"""
    return [
        day for day in dates
        if (start_date is None or day >= start_date.isoformat())
        and (end_date is None or day <= end_date.isoformat())
    ]


def load_daily_caches(
    symbols: set[str], cache_dir: Path
) -> tuple[dict[str, dict[str, dict[str, Any]]], list[str]]:
    """候補銘柄の日足キャッシュを読む。ファイル欠損とJSON不正を記録する。"""
    caches: dict[str, dict[str, dict[str, Any]]] = {}
    issues: list[str] = []
    for symbol in sorted(symbols):
        path = cache_dir / f"{symbol}.json"
        if not path.is_file():
            issues.append(f"{symbol}: 日足キャッシュなし")
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            issues.append(f"{symbol}: 日足キャッシュを読めません ({type(exc).__name__})")
            continue
        if not isinstance(payload, dict):
            issues.append(f"{symbol}: 日足キャッシュの形式が不正です")
            continue
        caches[symbol] = payload
    return caches, issues


def price_bucket(price: float | None) -> str:
    """監査価格を比較Aの価格区分に割り当てる。"""
    if price is None or not math.isfinite(price) or price <= 0:
        return "価格不明"
    if price <= 270:
        return "〜270円"
    if price <= 450:
        return "270超〜450円"
    if price <= 900:
        return "450超〜900円"
    return "900円超（区分外）"


def next_trading_day(trade_date: str) -> str:
    """対象日の次の日本市場営業日を返す。"""
    candidate = date.fromisoformat(trade_date) + timedelta(days=1)
    while not is_trading_day(candidate):
        candidate += timedelta(days=1)
    return candidate.isoformat()


def load_screening_candidates(
    project_root: Path,
    dates: tuple[str, ...] = A_COMPARISON_DATES,
) -> tuple[list[dict[str, Any]], list[str]]:
    """3帯のスクリーニング候補を銘柄×日で統合し、監査価格を保持する。"""
    merged: dict[tuple[str, str], dict[str, Any]] = {}
    issues: list[str] = []
    for band, relative_dir in SCREENING_INPUTS.items():
        directory = project_root / relative_dir
        for trade_date in dates:
            path = directory / f"{trade_date}.json"
            if not path.is_file():
                issues.append(f"比較A: {band} {trade_date}のスクリーニング結果なし")
                continue
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                issues.append(f"比較A: {path}を読めません ({type(exc).__name__})")
                continue
            if not isinstance(payload, dict) or payload.get("date") != trade_date:
                issues.append(f"比較A: {path.name}のJSON形式またはdateが不正です")
                continue
            symbols = payload.get("symbols")
            audit_entries = payload.get("audit_entries")
            if not isinstance(symbols, list) or not isinstance(audit_entries, list):
                issues.append(f"比較A: {path.name}にsymbolsまたはaudit_entriesがありません")
                continue
            audit_by_symbol: dict[str, dict[str, Any]] = {}
            for entry in audit_entries:
                if isinstance(entry, dict) and isinstance(entry.get("symbol"), (str, int)):
                    audit_by_symbol[str(entry["symbol"])] = entry
            unique_symbols = list(dict.fromkeys(str(symbol) for symbol in symbols))
            if len(unique_symbols) != len(symbols):
                issues.append(f"比較A: {path.name}のsymbolsに重複があります")
            if len(unique_symbols) != 50:
                issues.append(
                    f"比較A: {path.name}の候補数が50件ではありません ({len(unique_symbols)})"
                )
            for symbol in unique_symbols:
                record = audit_by_symbol.get(symbol)
                price_value = record.get("price") if record else None
                try:
                    price = float(price_value) if price_value is not None else None
                except (TypeError, ValueError):
                    price = None
                key = (trade_date, symbol)
                candidate = merged.setdefault(key, {
                    "date": trade_date,
                    "symbol": symbol,
                    "audit_prices": {},
                    "audit_rankings": {},
                    "source_bands": set(),
                })
                candidate["source_bands"].add(band)
                if record is not None:
                    candidate["audit_rankings"][band] = {
                        key: record.get(key)
                        for key in (
                            "turnover_rank", "price_gain_rank", "total_rank",
                            "turnover_value", "price_gain_value",
                        )
                    }
                if price is None or not math.isfinite(price) or price <= 0:
                    issues.append(
                        f"比較A: {band} {trade_date} {symbol}の監査価格がありません/不正です"
                    )
                else:
                    candidate["audit_prices"][band] = price

    rows: list[dict[str, Any]] = []
    for candidate in merged.values():
        audit_prices = candidate["audit_prices"]
        audit_rankings = candidate["audit_rankings"]
        ordered_prices = [
            audit_prices[band] for band in SCREENING_INPUTS if band in audit_prices
        ]
        ordered_rankings = [
            audit_rankings[band]
            for band in SCREENING_INPUTS
            if band in audit_rankings
        ]
        price = ordered_prices[0] if ordered_prices else None
        if any(value != price for value in ordered_prices[1:]):
            issues.append(
                f"比較A: {candidate['date']} {candidate['symbol']}の帯別監査価格が不一致 "
                f"({audit_prices})。270円→450円→900円の順で採用"
            )
        if ordered_rankings and any(
            ranking != ordered_rankings[0] for ranking in ordered_rankings[1:]
        ):
            issues.append(
                f"比較A: {candidate['date']} {candidate['symbol']}の帯別順位監査が不一致 "
                f"({audit_rankings})。270円→450円→900円の順で採用"
            )
        rows.append({
            "date": candidate["date"],
            "symbol": candidate["symbol"],
            "audit_price": price,
            "price_group": price_bucket(price),
            "source_bands": ",".join(sorted(candidate["source_bands"])),
            "source_band_count": len(candidate["source_bands"]),
            **(ordered_rankings[0] if ordered_rankings else {}),
        })
    return sorted(rows, key=lambda row: (row["date"], row["symbol"])), issues


def analyze_screening_candidate_day(
    candidate: dict[str, Any],
    daily_cache: dict[str, Any] | None,
) -> dict[str, Any]:
    """スクリーニング翌営業日の値幅を、評価日より前のATRで正規化する。"""
    row = dict(candidate)
    evaluation_date = next_trading_day(candidate["date"])
    row.update({
        "evaluation_date": evaluation_date,
        "daily_cache_found": daily_cache is not None,
        "daily_ohlc_available": False,
        "atr": None,
        "daily_range_atr": None,
        "open_max_up_atr": None,
        "open_max_down_atr": None,
        "close_change_atr": None,
        "unavailable_reason": None,
    })
    if daily_cache is None:
        row["unavailable_reason"] = "日足キャッシュなし"
        return row
    prior_bars = _daily_bars_before(daily_cache, evaluation_date)
    assessment = assess_volatility(
        [bar for _, bar in prior_bars],
        config.ATR_PERIOD,
        config.ATR_CAUTION_RATIO,
        config.ATR_DANGER_RATIO,
    )
    raw_today = daily_cache.get(evaluation_date)
    if not isinstance(raw_today, dict):
        row["unavailable_reason"] = f"翌営業日({evaluation_date})のOHLC日足なし"
        return row
    try:
        open_price, high, low, close = (
            float(raw_today["open"]),
            float(raw_today["high"]),
            float(raw_today["low"]),
            float(raw_today["close"]),
        )
    except (KeyError, TypeError, ValueError):
        row["unavailable_reason"] = "対象日のOHLCが不足/不正"
        return row
    if min(open_price, high, low, close) <= 0 or high < low:
        row["unavailable_reason"] = "対象日のOHLCが不正"
        return row
    row["daily_ohlc_available"] = True
    if assessment is None or assessment.atr <= 0:
        row["unavailable_reason"] = f"対象日前のATR({config.ATR_PERIOD})算出に必要な日足不足"
        return row
    atr = assessment.atr
    row.update({
        "atr": atr,
        "daily_range_atr": (high - low) / atr,
        "open_max_up_atr": (high - open_price) / atr,
        "open_max_down_atr": (low - open_price) / atr,
        "close_change_atr": (close - open_price) / atr,
    })
    return row


def _atr_level_for_ratio(ratio: float) -> str:
    if ratio >= config.ATR_DANGER_RATIO:
        return VolatilityLevel.DANGER.value
    if ratio >= config.ATR_CAUTION_RATIO:
        return VolatilityLevel.CAUTION.value
    return VolatilityLevel.NORMAL.value


def _daily_bars_through(
    cache: dict[str, Any], trade_date: str
) -> list[tuple[str, DailyBar]]:
    """指定日を含む確定日足を返す。"""
    day_after = (date.fromisoformat(trade_date) + timedelta(days=1)).isoformat()
    return _daily_bars_before(cache, day_after)


def analyze_selection_atr_candidate(
    candidate: dict[str, Any],
    daily_cache: dict[str, Any] | None,
    evaluation_row: dict[str, Any],
) -> dict[str, Any]:
    """選定日までの確定日足ATR比と翌営業日の正規化値幅を求める。"""
    row = dict(candidate)
    selection_bars = (
        _daily_bars_through(daily_cache, candidate["date"])
        if daily_cache is not None else []
    )
    assessment = assess_volatility(
        [bar for _, bar in selection_bars],
        config.ATR_PERIOD,
        config.ATR_CAUTION_RATIO,
        config.ATR_DANGER_RATIO,
    )
    row.update({
        "selection_reference_date": selection_bars[-1][0] if selection_bars else None,
        "selection_atr": assessment.atr if assessment else None,
        "selection_latest_true_range": (
            assessment.latest_true_range if assessment else None
        ),
        "selection_atr_ratio": assessment.ratio if assessment else None,
        "selection_atr_level": (
            assessment.level.value if assessment else None
        ),
        "selection_atr_unavailable_reason": None,
        "evaluation_date": evaluation_row["evaluation_date"],
        "evaluation_ohlc_available": evaluation_row["daily_ohlc_available"],
        "daily_range_atr": None,
        "open_max_up_atr": None,
        "open_max_down_atr": None,
        "close_change_atr": None,
    })
    if assessment is None or assessment.atr <= 0:
        row["selection_atr_unavailable"] = True
        row["selection_atr_unavailable_reason"] = (
            f"選定日までの日足ATR({config.ATR_PERIOD})算出に必要な履歴不足"
            if assessment is None else "選定時点ATRが0以下"
        )
        return row
    row["selection_atr_unavailable"] = False
    row["selection_atr_level"] = _atr_level_for_ratio(assessment.ratio)
    if not evaluation_row["daily_ohlc_available"] or daily_cache is None:
        return row
    raw = daily_cache.get(evaluation_row["evaluation_date"])
    if not isinstance(raw, dict):
        return row
    try:
        open_price, high, low, close = (
            float(raw["open"]), float(raw["high"]),
            float(raw["low"]), float(raw["close"]),
        )
    except (KeyError, TypeError, ValueError):
        return row
    if min(open_price, high, low, close) <= 0 or high < low:
        return row
    row.update({
        "daily_range_atr": (high - low) / assessment.atr,
        "open_max_up_atr": (high - open_price) / assessment.atr,
        "open_max_down_atr": (low - open_price) / assessment.atr,
        "close_change_atr": (close - open_price) / assessment.atr,
    })
    return row


def summarize_selection_atr_buckets(
    rows: list[dict[str, Any]],
    group_values: tuple[str, ...] = ("〜270円", "270超〜450円", "450超〜900円"),
) -> list[dict[str, Any]]:
    """選定時ATRレベル×指定区分の翌営業日値幅を集計する。"""
    levels = (
        VolatilityLevel.NORMAL.value,
        VolatilityLevel.CAUTION.value,
        VolatilityLevel.DANGER.value,
    )
    summaries: list[dict[str, Any]] = []
    for group in group_values:
        for level in levels:
            members = [
                row for row in rows
                if row.get("price_group") == group
                and row.get("selection_atr_level") == level
                and row.get("evaluation_ohlc_available")
            ]
            summary: dict[str, Any] = {
                "price_group": group,
                "selection_atr_level": level,
                "candidate_count": len(members),
                "reference_value": len(members) < MIN_REFERENCE_SAMPLE,
            }
            for metric in (
                "daily_range_atr", "open_max_up_atr",
                "open_max_down_atr", "close_change_atr",
            ):
                values = [
                    float(row[metric]) for row in members
                    if row.get(metric) is not None
                ]
                summary[f"{metric}_n"] = len(values)
                summary[f"{metric}_median"] = (
                    statistics.median(values) if values else None
                )
                summary[f"{metric}_p25"] = percentile(values, 0.25)
                summary[f"{metric}_p75"] = percentile(values, 0.75)
            summaries.append(summary)
    return summaries


def summarize_selection_atr_source_bands(
    rows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """スクリーニング元の3帯別にATRレベルと値幅を集計する。"""
    summaries: list[dict[str, Any]] = []
    for band in BAND_ORDER:
        source_rows = [
            {**row, "price_group": band}
            for row in rows
            if band in row.get("source_bands", "").split(",")
        ]
        for summary in summarize_selection_atr_buckets(source_rows, (band,)):
            if summary["price_group"] == band:
                summaries.append({
                    "screening_band": band,
                    **{
                        key: value for key, value in summary.items()
                        if key != "price_group"
                    },
                })
    return summaries


def _average_ranks(values: list[float]) -> list[float]:
    ordered_indices = sorted(range(len(values)), key=lambda index: values[index])
    ranks = [0.0] * len(values)
    cursor = 0
    while cursor < len(ordered_indices):
        end = cursor + 1
        while (
            end < len(ordered_indices)
            and values[ordered_indices[end]] == values[ordered_indices[cursor]]
        ):
            end += 1
        average = (cursor + 1 + end) / 2
        for index in ordered_indices[cursor:end]:
            ranks[index] = average
        cursor = end
    return ranks


def _spearman_correlation(x_values: list[float], y_values: list[float]) -> float | None:
    if len(x_values) != len(y_values) or len(x_values) < 2:
        return None
    x_ranks = _average_ranks(x_values)
    y_ranks = _average_ranks(y_values)
    x_mean = statistics.mean(x_ranks)
    y_mean = statistics.mean(y_ranks)
    numerator = sum(
        (x - x_mean) * (y - y_mean)
        for x, y in zip(x_ranks, y_ranks)
    )
    denominator = math.sqrt(
        sum((x - x_mean) ** 2 for x in x_ranks)
        * sum((y - y_mean) ** 2 for y in y_ranks)
    )
    return numerator / denominator if denominator else None


def summarize_selection_rank_relationship(
    rows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """監査順位と選定時ATR比のSpearman相関・順位四分位DANGER率を出す。"""
    eligible = [
        row for row in rows
        if row.get("selection_atr_ratio") is not None
        and row.get("evaluation_ohlc_available")
    ]
    summaries: list[dict[str, Any]] = []
    for rank_field, factor in (
        ("turnover_rank", "売買代金順位"),
        ("price_gain_rank", "値上がり率順位"),
    ):
        ranked = [
            row for row in eligible
            if row.get(rank_field) is not None
        ]
        rank_values = [float(row[rank_field]) for row in ranked]
        ratio_values = [float(row["selection_atr_ratio"]) for row in ranked]
        summaries.append({
            "factor": factor,
            "rank_field": rank_field,
            "segment": "全体",
            "sample_count": len(ranked),
            "spearman_rho": _spearman_correlation(rank_values, ratio_values),
            "atr_ratio_median": (
                statistics.median(ratio_values) if ratio_values else None
            ),
            "danger_count": sum(
                row["selection_atr_level"] == VolatilityLevel.DANGER.value
                for row in ranked
            ),
            "danger_rate": (
                sum(
                    row["selection_atr_level"] == VolatilityLevel.DANGER.value
                    for row in ranked
                ) / len(ranked)
                if ranked else None
            ),
        })
        ordered = sorted(
            ranked, key=lambda row: (float(row[rank_field]), row["date"], row["symbol"])
        )
        quartiles: dict[str, list[dict[str, Any]]] = {
            f"順位{index + 1}四分位": [] for index in range(4)
        }
        for index, row in enumerate(ordered):
            quartiles[f"順位{min(4, index * 4 // len(ordered) + 1)}四分位"].append(row)
        for label, members in quartiles.items():
            count = len(members)
            danger_count = sum(
                row["selection_atr_level"] == VolatilityLevel.DANGER.value
                for row in members
            )
            ratios = [float(row["selection_atr_ratio"]) for row in members]
            summaries.append({
                "factor": factor,
                "rank_field": rank_field,
                "segment": label,
                "sample_count": count,
                "spearman_rho": None,
                "atr_ratio_median": statistics.median(ratios) if ratios else None,
                "danger_count": danger_count,
                "danger_rate": danger_count / count if count else None,
            })
    return summaries


def summarize_selection_atr_cutoffs(
    candidate_rows: list[dict[str, Any]],
    screening_candidates: list[dict[str, Any]],
    midday_selections: dict[str, dict[str, dict[str, Any]]],
    dates: tuple[str, ...],
) -> list[dict[str, Any]]:
    """スクリーニング50銘柄・昼採用10銘柄のATR比2未満残存数を出す。"""
    atr_by_key = {
        (row["date"], row["symbol"]): row.get("selection_atr_ratio")
        for row in candidate_rows
    }
    groups: list[dict[str, Any]] = []
    for band in BAND_ORDER:
        for day in dates:
            source_candidates = [
                row for row in screening_candidates
                if row["date"] == day
                and band in row["source_bands"].split(",")
            ]
            screening_evaluable = sum(
                atr_by_key.get((day, row["symbol"])) is not None
                for row in source_candidates
            )
            kept = [
                row for row in source_candidates
                if atr_by_key.get((day, row["symbol"])) is not None
                and atr_by_key[(day, row["symbol"])] < config.ATR_DANGER_RATIO
            ]
            selection = midday_selections.get(band, {}).get(day)
            adopted = selection["symbols"] if selection else []
            midday_evaluable = (
                sum(atr_by_key.get((day, symbol)) is not None for symbol in adopted)
                if selection else None
            )
            adopted_kept = [
                symbol for symbol in adopted
                if atr_by_key.get((day, symbol)) is not None
                and atr_by_key[(day, symbol)] < config.ATR_DANGER_RATIO
            ]
            groups.append({
                "band": band,
                "date": day,
                "screening_candidate_count": len(source_candidates),
                "screening_atr_evaluable": screening_evaluable,
                "screening_below_danger_count": len(kept),
                "screening_below_danger_rate": (
                    len(kept) / screening_evaluable if screening_evaluable else None
                ),
                "midday_adopted_count": len(adopted) if selection else None,
                "midday_atr_evaluable": midday_evaluable,
                "midday_below_danger_count": len(adopted_kept) if selection else None,
                "midday_below_danger_rate": (
                    len(adopted_kept) / midday_evaluable
                    if midday_evaluable else None
                ),
            })
    return groups


def summarize_index_daily_returns(
    cache: dict[str, Any],
    dates: tuple[str, ...],
) -> list[dict[str, Any]]:
    """指定日の終値と直前利用可能日の終値から日経騰落率を求める。"""
    valid_closes: dict[str, float] = {}
    for day, raw in cache.items():
        if not isinstance(raw, dict):
            continue
        try:
            close = float(raw["close"])
        except (KeyError, TypeError, ValueError):
            continue
        if close > 0:
            valid_closes[day] = close
    rows = []
    for day in dates:
        previous_days = [candidate for candidate in valid_closes if candidate < day]
        previous_day = max(previous_days) if previous_days else None
        current_close = valid_closes.get(day)
        previous_close = valid_closes.get(previous_day) if previous_day else None
        rows.append({
            "date": day,
            "previous_close_date": previous_day,
            "previous_close": previous_close,
            "close": current_close,
            "change_percent": (
                (current_close / previous_close - 1) * 100
                if current_close is not None and previous_close is not None else None
            ),
        })
    return rows


def summarize_screening_candidates(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """価格区分ごとの候補数と日足ATR比の分布を集計する。"""
    groups = ("〜270円", "270超〜450円", "450超〜900円", "900円超（区分外）", "価格不明")
    summaries = []
    for group in groups:
        group_rows = [row for row in rows if row["price_group"] == group]
        summary: dict[str, Any] = {
            "price_group": group,
            "candidate_symbol_days": len(group_rows),
            "candidate_days": len({row["date"] for row in group_rows}),
            "unique_symbols": len({row["symbol"] for row in group_rows}),
            "daily_ohlc_n": sum(row["daily_ohlc_available"] for row in group_rows),
            "daily_ohlc_days": len({
                row["date"] for row in group_rows if row["daily_ohlc_available"]
            }),
        }
        for metric in (
            "daily_range_atr", "open_max_up_atr", "open_max_down_atr", "close_change_atr"
        ):
            values = [float(row[metric]) for row in group_rows if row.get(metric) is not None]
            summary[f"{metric}_n"] = len(values)
            summary[f"{metric}_days"] = len({
                row["date"] for row in group_rows if row.get(metric) is not None
            })
            summary[f"{metric}_median"] = statistics.median(values) if values else None
            summary[f"{metric}_p25"] = percentile(values, 0.25)
            summary[f"{metric}_p75"] = percentile(values, 0.75)
        summary["reference_value"] = summary["candidate_symbol_days"] < MIN_REFERENCE_SAMPLE
        summaries.append(summary)
    return summaries


def _load_diagnostic(
    directory: Path, trade_date: str
) -> tuple[dict[str, Any] | None, str | None]:
    paths = sorted(directory.glob(f"{trade_date}_*.json"))
    if not paths:
        return None, f"{directory}: {trade_date}の診断JSONなし"
    path = paths[-1]
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return None, f"{path}: 診断JSONを読めません ({type(exc).__name__})"
    if not isinstance(payload, dict) or payload.get("date") != trade_date:
        return None, f"{path}: 診断JSONの形式/dateが不正です"
    if not isinstance(payload.get("summary"), dict) or not isinstance(payload.get("candidates"), list):
        return None, f"{path}: 診断JSONにsummary/candidatesがありません"
    return payload, None


def load_midday_selections(
    project_root: Path,
    dates: tuple[str, ...] = B_COMPARISON_DATES,
) -> tuple[dict[str, dict[str, dict[str, Any]]], list[str]]:
    """3帯・対象日の昼推定診断を読み、選定銘柄と窓情報を返す。"""
    by_band: dict[str, dict[str, dict[str, Any]]] = {band: {} for band in BAND_ORDER}
    issues: list[str] = []
    for band in BAND_ORDER:
        folder = "270" if band == "270円" else band.removesuffix("円")
        directory = project_root / "data" / "filtering_midday" / folder / "diagnostics"
        for trade_date in dates:
            payload, issue = _load_diagnostic(directory, trade_date)
            if issue:
                issues.append(f"比較B/D: {issue}")
                continue
            summary = payload["summary"]
            adopted = summary.get("adopted_symbols")
            if not isinstance(adopted, list) or any(
                not isinstance(symbol, (str, int)) for symbol in adopted
            ):
                issues.append(f"比較B/D: {band} {trade_date}のadopted_symbolsが不正です")
                continue
            symbols = list(dict.fromkeys(str(symbol) for symbol in adopted))
            if len(symbols) != 10:
                issues.append(
                    f"比較B: {band} {trade_date}の選定数が10件ではありません ({len(symbols)})"
                )
            candidates = {
                str(item["symbol"]): item
                for item in payload["candidates"]
                if isinstance(item, dict) and isinstance(item.get("symbol"), (str, int))
            }
            first_values = {
                str(item.get("window_first_bar_time")).split("T")[-1]
                for item in payload["candidates"]
                if isinstance(item, dict) and item.get("window_first_bar_time")
            }
            last_values = {
                str(item.get("window_last_bar_time")).split("T")[-1]
                for item in payload["candidates"]
                if isinstance(item, dict) and item.get("window_last_bar_time")
            }
            by_band[band][trade_date] = {
                "symbols": symbols,
                "candidates": candidates,
                "window": summary.get("window"),
                "first_bars": [min(first_values)] if first_values else [],
                "last_bars": [max(last_values)] if last_values else [],
            }
    return by_band, issues


def minutes_from_time(
    minute_bars: list[tuple[str, float]], start_time: str = "09:35"
) -> list[tuple[str, float]]:
    """分足の時刻がstart_time以降の価格だけを返す。"""
    return [
        (time_text, price)
        for time_text, price in minute_bars
        if str(time_text).split("T")[-1][:5] >= start_time
    ]


def compare_selected_sets(
    trade_date: str,
    morning_symbols: list[str],
    midday_symbols: list[str],
    morning_details: dict[str, dict[str, Any]],
    midday_details: dict[str, dict[str, Any]],
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """朝・昼選定の重なりを集計し、差分銘柄の順位と倍率を返す。"""
    morning = set(morning_symbols)
    midday = set(midday_symbols)
    common_symbols = morning & midday
    union = morning | midday
    summary = {
        "date": trade_date,
        "morning_count": len(morning),
        "midday_count": len(midday),
        "overlap_count": len(common_symbols),
        "morning_overlap_rate": len(common_symbols) / len(morning) if morning else None,
        "midday_overlap_rate": len(common_symbols) / len(midday) if midday else None,
        "jaccard_rate": len(common_symbols) / len(union) if union else None,
    }
    differences = []
    for symbol in sorted(union - common_symbols):
        morning_item = morning_details.get(symbol, {})
        midday_item = midday_details.get(symbol, {})
        differences.append({
            "date": trade_date,
            "symbol": symbol,
            "morning_selected": symbol in morning,
            "morning_rank": morning_item.get("rank"),
            "morning_ratio": morning_item.get("ratio"),
            "midday_selected": symbol in midday,
            "midday_rank": midday_item.get("rank"),
            "midday_ratio": midday_item.get("ratio"),
        })
    return summary, differences


def calculate_excursion_metrics(
    entry_price: float,
    prices: list[float],
    atr: float | None,
    profit_lock_multiplier: float,
    stop_loss_multiplier: float = 1.5,
) -> dict[str, float | bool | str | None]:
    """成立価格以降の価格系列だけからATR比の値幅と先着ラインを計算する。"""
    if not prices or atr is None or atr <= 0:
        return {
            "max_up_atr": None,
            "max_down_atr": None,
            "close_atr": None,
            "profit_reached": None,
            "stop_reached": None,
            "first_touch": None,
            "first_touch_index": None,
        }
    max_price = max(prices)
    min_price = min(prices)
    profit_line = entry_price + atr * profit_lock_multiplier
    stop_line = entry_price - atr * stop_loss_multiplier
    first_touch = None
    first_touch_index = None
    for index, price in enumerate(prices):
        if price >= profit_line:
            first_touch = "利確"
            first_touch_index = index
            break
        if price <= stop_line:
            first_touch = "損切り"
            first_touch_index = index
            break
    return {
        "max_up_atr": (max_price - entry_price) / atr,
        "max_down_atr": (min_price - entry_price) / atr,
        "close_atr": (prices[-1] - entry_price) / atr,
        "profit_reached": max_price >= profit_line,
        "stop_reached": min_price <= stop_line,
        "first_touch": first_touch or "引け",
        "first_touch_index": first_touch_index,
    }


def virtual_close_trade(row: dict[str, Any]) -> dict[str, Any]:
    """100株を仮想購入し、引けで手仕舞った場合の手取り損益を計算する。"""
    entry = row.get("entry_price")
    close = row.get("close_price")
    if entry is None or close is None:
        return {"virtual_gross_pnl_100": None, "virtual_net_pnl_100": None}
    entry_price = float(entry)
    close_price = float(close)
    quantity = config.ORDER_UNIT
    tick = _tick_size(entry_price)
    gross = (close_price - entry_price) * quantity
    fees = (entry_price + close_price) * quantity * config.PAPER_FEE_RATE
    slippage = (
        (entry_price + close_price)
        * quantity
        * config.PAPER_MARKET_SLIPPAGE_BPS
        / 10_000
    )
    spread = tick * quantity
    result: dict[str, Any] = {
        "virtual_gross_pnl_100": gross,
        "virtual_net_pnl_100": gross - fees - slippage - spread,
        "virtual_close_return_percent": (close_price / entry_price - 1) * 100,
    }
    for cap in VIRTUAL_ORDER_CAPS:
        result[f"buy_100_with_{cap}_cap"] = (
            entry_price * quantity <= cap
        )
    return result


def summarize_skip_outcomes(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """見送り理由ごとに100株仮想引け損益を帯別集計する。"""
    reason_labels = {
        "atr_danger": "ATR DANGER",
        "caution_rsi": "CAUTION RSI",
        "other": "その他",
    }
    summaries: list[dict[str, Any]] = []
    for band in BAND_ORDER:
        band_rows = [
            row for row in rows
            if row.get("band") == band and row.get("pre_signal")
        ]
        for category, label in (
            ("atr_danger", reason_labels["atr_danger"]),
            ("caution_rsi", reason_labels["caution_rsi"]),
            ("other", reason_labels["other"]),
            ("accepted", "見送り後（対照）"),
        ):
            group = [
                row for row in band_rows
                if (
                    row.get("post_signal") is True
                    if category == "accepted"
                    else row.get("rejection_category") == category
                )
            ]
            evaluable = [
                row for row in group
                if row.get("virtual_net_pnl_100") is not None
                and row.get("atr") is not None
                and row["atr"] > 0
            ]
            net_pnls = [float(row["virtual_net_pnl_100"]) for row in evaluable]
            normalized_pnls = [
                float(row["virtual_net_pnl_100"])
                / (float(row["atr"]) * config.ORDER_UNIT)
                for row in evaluable
            ]
            summaries.append({
                "band": band,
                "reason": label,
                "reason_category": category,
                "signal_count": len(group),
                "evaluable_count": len(evaluable),
                "loss_count": sum(pnl < 0 for pnl in net_pnls),
                "profit_count": sum(pnl > 0 for pnl in net_pnls),
                "breakeven_count": sum(pnl == 0 for pnl in net_pnls),
                "net_pnl_atr_sum": sum(normalized_pnls),
                "net_pnl_atr_median": (
                    statistics.median(normalized_pnls) if normalized_pnls else None
                ),
                "net_pnl_yen_sum": sum(net_pnls),
                "net_pnl_yen_median": (
                    statistics.median(net_pnls) if net_pnls else None
                ),
                "gross_pnl_yen_sum": sum(
                    float(row["virtual_gross_pnl_100"])
                    for row in evaluable
                    if row.get("virtual_gross_pnl_100") is not None
                ),
                "reference_value": len(evaluable) < MIN_REFERENCE_SAMPLE,
            })
    return summaries


def summarize_atr_danger_scales(
    rows: list[dict[str, Any]],
    ratios: tuple[float, ...] = ATR_DANGER_SCALE_RATIOS,
) -> list[dict[str, Any]]:
    """ATR DANGER見送りシグナルの縮小数量仮想損益を帯別集計する。"""
    summaries: list[dict[str, Any]] = []
    for band in ("450円", "900円"):
        danger_rows = [
            row for row in rows
            if row.get("band") == band
            and row.get("pre_signal")
            and row.get("rejection_category") == "atr_danger"
            and row.get("virtual_net_pnl_100") is not None
            and row.get("atr") is not None
            and row["atr"] > 0
        ]
        for ratio in ratios:
            scaled_pnls = [
                float(row["virtual_net_pnl_100"]) * ratio
                for row in danger_rows
            ]
            normalized_pnls = [
                float(row["virtual_net_pnl_100"]) / (float(row["atr"]) * config.ORDER_UNIT)
                for row in danger_rows
            ]
            summaries.append({
                "band": band,
                "scale_ratio": ratio,
                "equivalent_shares": config.ORDER_UNIT * ratio,
                "signal_count": len(danger_rows),
                "loss_count": sum(pnl < 0 for pnl in scaled_pnls),
                "profit_count": sum(pnl > 0 for pnl in scaled_pnls),
                "net_pnl_yen_sum": sum(scaled_pnls),
                "net_pnl_yen_median": (
                    statistics.median(scaled_pnls) if scaled_pnls else None
                ),
                "net_pnl_atr_median": (
                    statistics.median(normalized_pnls) if normalized_pnls else None
                ),
                "reference_value": len(danger_rows) < MIN_REFERENCE_SAMPLE,
            })
    return summaries


def summarize_atr_population(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """昼推定選定銘柄のATR危険判定を帯・実価格区分ごとに集計する。"""
    groups = [
        ("価格帯", band, [row for row in rows if row["band"] == band])
        for band in BAND_ORDER
    ]
    price_groups = sorted({
        row["price_group"] for row in rows if row.get("price_group")
    })
    groups.extend(
        (
            "実価格",
            group,
            [row for row in rows if row.get("price_group") == group],
        )
        for group in price_groups
    )
    summaries = []
    for group_type, group_name, group_rows in groups:
        evaluated = [row for row in group_rows if row.get("atr_level") is not None]
        danger = [row for row in evaluated if row["atr_level"] == VolatilityLevel.DANGER.value]
        ratios = [float(row["atr_ratio"]) for row in evaluated if row.get("atr_ratio") is not None]
        summaries.append({
            "group_type": group_type,
            "group": group_name,
            "selected_symbol_days": len(group_rows),
            "atr_evaluable": len(evaluated),
            "atr_danger_count": len(danger),
            "atr_danger_rate": len(danger) / len(evaluated) if evaluated else None,
            "atr_caution_count": sum(
                row["atr_level"] == VolatilityLevel.CAUTION.value for row in evaluated
            ),
            "atr_ratio_median": statistics.median(ratios) if ratios else None,
            "price_median": statistics.median([
                float(row["reference_price"])
                for row in group_rows if row.get("reference_price") is not None
            ]) if any(row.get("reference_price") is not None for row in group_rows) else None,
        })
    return summaries


def percentile(values: list[float], fraction: float) -> float | None:
    """線形補間による分位点。"""
    if not values:
        return None
    if not 0 <= fraction <= 1:
        raise ValueError("fractionは0から1の範囲で指定してください")
    ordered = sorted(values)
    position = (len(ordered) - 1) * fraction
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


def summarize_band(
    band: str, rows: list[dict[str, Any]], dates: list[str]
) -> dict[str, Any]:
    """価格帯のシグナル数・値幅・コスト・到達率を集計する。"""
    band_rows = [row for row in rows if row["band"] == band]
    pre_signals = [row for row in band_rows if row["pre_signal"]]
    post_signals = [row for row in band_rows if row["post_signal"]]
    post_assessed_signals = [
        row for row in pre_signals if row.get("post_signal") is not None
    ]
    per_day = {
        day: sum(row["pre_signal"] for row in band_rows if row["date"] == day)
        for day in dates
    }
    evaluable_days = set()
    for day in dates:
        day_rows = [row for row in band_rows if row["date"] == day]
        has_signal = per_day[day] > 0
        complete = bool(day_rows) and all(row["signal_evaluated"] for row in day_rows)
        if has_signal or complete:
            evaluable_days.add(day)
    zero_days = sum(per_day[day] == 0 for day in evaluable_days)
    result: dict[str, Any] = {
        "band": band,
        "comparison_mode": "朝の板ベース（比較C）",
        "comparison_days": len(dates),
        "candidate_symbol_days": len(band_rows),
        "candidate_symbols": len({row["symbol"] for row in band_rows}),
        "pre_signal_count": len(pre_signals),
        "pre_signal_per_day": len(pre_signals) / len(dates) if dates else None,
        "zero_signal_days": zero_days,
        "signal_evaluable_days": len(evaluable_days),
        "zero_signal_day_rate": zero_days / len(evaluable_days) if evaluable_days else None,
        "post_signal_count": len(post_signals),
        "post_signal_evaluable_count": len(post_assessed_signals),
        "post_signal_unavailable_count": len(pre_signals) - len(post_assessed_signals),
        "pre_signal_days": len({row["date"] for row in pre_signals}),
        "post_signal_days": len({row["date"] for row in post_signals}),
        "primary_skip_regime_rsi": sum(
            row["primary_skip_reason"] == "market_regime_rsi" for row in band_rows
        ),
        "primary_skip_atr_danger": sum(
            row["primary_skip_reason"] == "atr_danger" for row in band_rows
        ),
        "primary_skip_market_danger": sum(
            row["primary_skip_reason"] == "market_regime_danger" for row in band_rows
        ),
        "primary_skip_market_regime_unavailable": sum(
            row["primary_skip_reason"] == "market_regime_unavailable" for row in band_rows
        ),
        "skip_caution_rsi": sum(
            row.get("rejection_category") == "caution_rsi" for row in band_rows
        ),
        "skip_atr_danger": sum(
            row.get("rejection_category") == "atr_danger" for row in band_rows
        ),
        "skip_other": sum(
            row.get("rejection_category") == "other" for row in band_rows
        ),
        "overlap_atr_danger": sum(
            "atr_danger" in row["skip_reasons"] for row in band_rows
        ),
        "overlap_market_danger": sum(
            "market_regime_danger" in row["skip_reasons"] for row in band_rows
        ),
    }
    metric_sources: dict[str, list[dict[str, Any]]] = {
        "max_up_atr": pre_signals,
        "max_down_atr": pre_signals,
        "close_atr": pre_signals,
        "profit_reached": pre_signals,
        "stop_reached": pre_signals,
        "cost_atr_ratio": pre_signals,
        "tick_percent": pre_signals,
        "first_touch": pre_signals,
    }
    for metric, source_rows in metric_sources.items():
        values = [row[metric] for row in source_rows if row.get(metric) is not None]
        result[f"{metric}_n"] = len(values)
        result[f"{metric}_days"] = len({
            row["date"] for row in source_rows if row.get(metric) is not None
        })
        if metric in {"profit_reached", "stop_reached"}:
            result[metric + "_count"] = sum(bool(value) for value in values)
            result[metric + "_rate"] = (
                sum(bool(value) for value in values) / len(values) if values else None
            )
        elif metric == "first_touch":
            for outcome in ("利確", "損切り", "引け"):
                count = sum(value == outcome for value in values)
                result[f"first_{outcome}_n"] = len(values)
                result[f"first_{outcome}_count"] = count
                result[f"first_{outcome}_rate"] = count / len(values) if values else None
                result[f"first_{outcome}_days"] = len({
                    row["date"] for row in source_rows if row.get("first_touch") is not None
                })
        else:
            numeric = [float(value) for value in values]
            result[f"{metric}_median"] = statistics.median(numeric) if numeric else None
            result[f"{metric}_p25"] = percentile(numeric, 0.25)
            result[f"{metric}_p75"] = percentile(numeric, 0.75)
    for slots in (1, 2, 3):
        name = f"zero_shares_{slots}_slots"
        values = [row[name] for row in pre_signals if row.get(name) is not None]
        result[f"{name}_n"] = len(values)
        result[f"{name}_days"] = len({
            row["date"] for row in pre_signals if row.get(name) is not None
        })
        result[f"{name}_count"] = sum(bool(value) for value in values)
        result[f"{name}_rate"] = (
            sum(bool(value) for value in values) / len(values) if values else None
        )
    for key, value in list(result.items()):
        if key.endswith("_n"):
            result[key.removesuffix("_n") + "_reference"] = value < MIN_REFERENCE_SAMPLE
    return result


def _daily_bars_before(
    cache: dict[str, Any], trade_date: str
) -> list[tuple[str, DailyBar]]:
    bars: list[tuple[str, DailyBar]] = []
    for day, raw in sorted(cache.items()):
        if day >= trade_date or not isinstance(raw, dict):
            continue
        try:
            high = float(raw["high"])
            low = float(raw["low"])
            close = float(raw["close"])
            if min(high, low, close) <= 0 or high < low:
                continue
            bars.append((day, DailyBar(high=high, low=low, close=close)))
        except (KeyError, TypeError, ValueError):
            continue
    return bars


def _tick_size(price: float) -> float:
    """東証普通株の価格帯別呼値。比較対象の3価格帯では通常1円。"""
    if price <= 3_000:
        return 1.0
    if price <= 5_000:
        return 5.0
    if price <= 30_000:
        return 10.0
    if price <= 50_000:
        return 50.0
    if price <= 300_000:
        return 100.0
    if price <= 500_000:
        return 500.0
    if price <= 3_000_000:
        return 1_000.0
    if price <= 5_000_000:
        return 5_000.0
    if price <= 30_000_000:
        return 10_000.0
    if price <= 50_000_000:
        return 50_000.0
    return 100_000.0


def _market_regime_for_day(
    trade_date: str,
    index_cache: dict[str, dict[str, Any]],
) -> tuple[MarketRegime, bool, str | None]:
    """本番のレジームUseCaseを、当日より前のキャッシュだけで実行する。"""
    as_of = date.fromisoformat(trade_date)

    class CachedIndexClient:
        def get_daily_ohlc(self, symbol: str, range_: str = "max") -> list[MarketDailyBar]:
            raw_rows = index_cache.get(symbol, {})
            start = _subtract_months(as_of, 3) if range_ == "3mo" else None
            result = []
            for day, raw in sorted(raw_rows.items()):
                if day >= trade_date or (start is not None and day < start.isoformat()):
                    continue
                try:
                    result.append(
                        MarketDailyBar(
                            date=date.fromisoformat(day),
                            open=float(raw["open"]),
                            high=float(raw["high"]),
                            low=float(raw["low"]),
                            close=float(raw["close"]),
                        )
                    )
                except (KeyError, TypeError, ValueError):
                    continue
            return result

    assessment = MarketRegimeUseCase(
        CachedIndexClient(),
        config.MARKET_REGIME_THRESHOLDS,
        realized_volatility_window=config.MARKET_REGIME_REALIZED_VOL_WINDOW,
        data_range=config.MARKET_REGIME_DATA_RANGE,
        adx_threshold=config.MARKET_REGIME_ADX_TREND_THRESHOLD,
        as_of_date=as_of,
        retry_wait_seconds=0,
    ).execute()
    return assessment.regime, assessment.data_available, assessment.failure_reason


def analyze_candidate_day(
    band: str,
    symbol: str,
    trade_date: str,
    minute_bars: list[tuple[str, float]],
    daily_cache: dict[str, Any] | None,
    market_regime: MarketRegime,
    market_regime_available: bool,
    market_regime_reason: str | None,
) -> dict[str, Any]:
    """本番の価格バンド・RSI判定で当日の最初のBUYシグナルを探す。"""
    row: dict[str, Any] = {
        "band": band,
        "date": trade_date,
        "symbol": symbol,
        "daily_cache_found": daily_cache is not None,
        "daily_history_rows": 0,
        "minute_bar_count": len(minute_bars),
        "market_regime": market_regime.value,
        "market_regime_available": market_regime_available,
        "market_regime_reason": market_regime_reason,
        "signal_evaluated": False,
        "pre_signal": False,
        "post_signal": False,
        "primary_skip_reason": None,
        "skip_reasons": [],
        "rsi": None,
        "rsi_threshold": None,
        "upper_band": None,
        "atr": None,
        "atr_ratio": None,
        "atr_level": None,
        "signal_time": None,
        "entry_price": None,
        "max_up_atr": None,
        "max_down_atr": None,
        "close_atr": None,
        "profit_reached": None,
        "stop_reached": None,
        "first_touch": None,
        "first_touch_time": None,
        "first_touch_price": None,
        "close_price": None,
        "cost_atr_ratio": None,
        "tick_percent": None,
        "zero_shares_1_slots": None,
        "zero_shares_2_slots": None,
        "zero_shares_3_slots": None,
        "unavailable_reason": None,
    }
    if daily_cache is None:
        row["unavailable_reason"] = "日足キャッシュなし"
        return row
    daily_bars = _daily_bars_before(daily_cache, trade_date)
    row["daily_history_rows"] = len(daily_bars)
    if not daily_bars:
        row["unavailable_reason"] = "対象日より前の有効日足なし"
        return row

    cutoff = (date.fromisoformat(trade_date) - timedelta(days=DAILY_CLOSE_LOOKBACK_DAYS)).isoformat()
    closes = [
        bar.close for day, bar in daily_bars
        if day >= cutoff
    ]
    limit = calculate_price_limit(closes)
    rsi = calculate_rsi(closes, config.RSI_PERIOD, config.RSI_MINIMUM_CLOSES)
    assessment = assess_volatility(
        [bar for _, bar in daily_bars],
        config.ATR_PERIOD,
        config.ATR_CAUTION_RATIO,
        config.ATR_DANGER_RATIO,
    )
    if assessment is not None:
        row["atr"] = assessment.atr
        row["atr_ratio"] = assessment.ratio
        row["atr_level"] = assessment.level.value
    if limit is None or rsi is None:
        row["unavailable_reason"] = (
            f"エントリー指標不足(終値{len(closes)}本)"
        )
        return row
    row["rsi"] = rsi
    row["upper_band"] = limit.upper_band
    if not minute_bars:
        row["unavailable_reason"] = "当日の分足Parquetなし"
        return row
    row["signal_evaluated"] = True

    normal_threshold = config.RSI_ENTRY_THRESHOLD
    actual_threshold = resolve_rsi_entry_threshold(
        market_regime,
        config.RSI_ENTRY_THRESHOLD,
        config.RSI_ENTRY_THRESHOLD_CAUTION,
    )
    signal_index = None
    signal_price = None
    for index, (time_text, price) in enumerate(minute_bars):
        signal = TradeSignal.evaluate(
            symbol, price, limit, rsi, normal_threshold, config.RSI_EXIT_THRESHOLD
        )
        if signal is not None and signal.side == OrderSide.BUY:
            signal_index = index
            signal_price = price
            row["signal_time"] = time_text
            break
    if signal_index is None or signal_price is None:
        return row

    row["pre_signal"] = True
    row["entry_price"] = signal_price
    row["rsi_threshold"] = actual_threshold
    actual_signal = TradeSignal.evaluate(
        symbol,
        signal_price,
        limit,
        rsi,
        actual_threshold,
        config.RSI_EXIT_THRESHOLD,
    )
    skip_reasons: list[str] = []
    caution_rsi_veto = (
        market_regime == MarketRegime.CAUTION
        and (actual_signal is None or actual_signal.side != OrderSide.BUY)
    )
    atr_danger_veto = (
        actual_signal is not None
        and actual_signal.side == OrderSide.BUY
        and assessment is not None
        and assessment.level == VolatilityLevel.DANGER
        and config.ATR_DANGER_ACTION == "skip"
    )
    if actual_signal is None or actual_signal.side != OrderSide.BUY:
        skip_reasons.append("market_regime_rsi")
    elif assessment is not None and (
        assessment.level == VolatilityLevel.DANGER
        and config.ATR_DANGER_ACTION == "skip"
    ):
        skip_reasons.append("atr_danger")
    if actual_signal is not None and actual_signal.side == OrderSide.BUY:
        if market_regime == MarketRegime.DANGER:
            skip_reasons.append("market_regime_danger")
    row["skip_reasons"] = skip_reasons
    row["primary_skip_reason"] = skip_reasons[0] if skip_reasons else None
    row["post_signal"] = not skip_reasons
    if not market_regime_available and row["pre_signal"]:
        row["rejection_category"] = "unavailable"
    elif caution_rsi_veto:
        row["rejection_category"] = "caution_rsi"
    elif atr_danger_veto:
        row["rejection_category"] = "atr_danger"
    elif skip_reasons:
        row["rejection_category"] = "other"
    else:
        row["rejection_category"] = None
    if row["pre_signal"] and not market_regime_available:
        row["rsi_threshold"] = None
        row["skip_reasons"] = ["market_regime_unavailable"]
        row["primary_skip_reason"] = "market_regime_unavailable"
        row["post_signal"] = None

    if assessment is not None:
        signal_prices = [price for _, price in minute_bars[signal_index:]]
        excursion = calculate_excursion_metrics(
            signal_price,
            signal_prices,
            assessment.atr,
            PROFIT_LOCK_MULTIPLIERS[assessment.level],
            config.ATR_STOP_NORMAL_MULTIPLIER,
        )
        row.update(excursion)
        row["close_price"] = signal_prices[-1] if signal_prices else None
        touch_index = excursion.get("first_touch_index")
        if touch_index is not None:
            row["first_touch_time"] = minute_bars[signal_index + int(touch_index)][0]
            row["first_touch_price"] = signal_prices[int(touch_index)]

    if row["pre_signal"]:
        tick = _tick_size(signal_price)
        row["tick_percent"] = tick / signal_price * 100.0
        if assessment is not None and assessment.atr > 0:
            total_cost = (
                signal_price * 2 * config.PAPER_FEE_RATE
                + signal_price * 2 * config.PAPER_MARKET_SLIPPAGE_BPS / 10_000
                + tick
            )
            row["cost_atr_ratio"] = total_cost / assessment.atr
        for slots in (1, 2, 3):
            budget = min(
                config.OPERATING_CAPITAL / slots,
                config.MAX_ORDER_AMOUNT_PER_TRADE,
            )
            row[f"zero_shares_{slots}_slots"] = (
                signal_price * config.ORDER_UNIT > budget
            )
        row.update(virtual_close_trade(row))
    return row


def _coverage_summary(
    results: dict[str, dict[str, list[str]]],
    common: list[str],
    project_root: Path,
    daily_caches: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    coverage = []
    for band in BAND_ORDER:
        by_date = results.get(band, {})
        pairs = [(day, symbol) for day, symbols in by_date.items() for symbol in symbols]
        symbols = {symbol for _, symbol in pairs}
        minute_count = sum(
            (project_root / "data" / "minute_bars_parquet"
             / f"symbol={symbol}" / f"date={day}" / "data.parquet").is_file()
            for day, symbol in pairs
        )
        cache_count = sum(
            (project_root / "data" / "cache" / "yahoo_daily" / f"{symbol}.json").is_file()
            for symbol in symbols
        )
        prior_daily_count = sum(
            any(day_key < day for day_key in daily_caches.get(symbol, {}))
            for day, symbol in pairs
        )
        common_pairs = [
            (day, symbol) for day in common for symbol in by_date.get(day, [])
        ]
        common_minute_count = sum(
            (project_root / "data" / "minute_bars_parquet"
             / f"symbol={symbol}" / f"date={day}" / "data.parquet").is_file()
            for day, symbol in common_pairs
        )
        common_cache_count = sum(
            (project_root / "data" / "cache" / "yahoo_daily" / f"{symbol}.json").is_file()
            for _, symbol in common_pairs
        )
        common_prior_daily_count = sum(
            any(day_key < day for day_key in daily_caches.get(symbol, {}))
            for day, symbol in common_pairs
        )
        dates = sorted(by_date)
        coverage.append({
            "band": band,
            "result_days": len(by_date),
            "date_start": dates[0] if dates else None,
            "date_end": dates[-1] if dates else None,
            "candidate_symbol_days": len(pairs),
            "unique_symbols": len(symbols),
            "minute_parquet_pairs": minute_count,
            "minute_parquet_rate": minute_count / len(pairs) if pairs else None,
            "daily_cache_symbols": cache_count,
            "daily_cache_rate": cache_count / len(symbols) if symbols else None,
            "prior_daily_pairs": prior_daily_count,
            "prior_daily_rate": prior_daily_count / len(pairs) if pairs else None,
            "common_candidate_symbol_days": len(common_pairs),
            "common_minute_parquet_pairs": common_minute_count,
            "common_daily_cache_pairs": common_cache_count,
            "common_prior_daily_pairs": common_prior_daily_count,
        })
    return coverage


def _read_minute_prices(
    minute_dir: Path, dates: list[str], symbols: set[str]
) -> dict[tuple[str, str], list[tuple[str, float]]]:
    if not dates or not symbols:
        return {}
    files = list(minute_dir.glob("symbol=*/date=*/data.parquet"))
    if not files:
        return {}
    import duckdb

    symbol_list = sorted(symbols)
    placeholders = ", ".join("?" for _ in symbol_list)
    date_placeholders = ", ".join("?" for _ in dates)
    query = f"""
        SELECT symbol, CAST(date AS VARCHAR), time, price
        FROM read_parquet(?, hive_partitioning = true)
        WHERE symbol IN ({placeholders})
          AND CAST(date AS VARCHAR) IN ({date_placeholders})
          AND price IS NOT NULL
        ORDER BY date, symbol, time
    """
    pattern = str(minute_dir / "symbol=*" / "date=*" / "data.parquet")
    connection = duckdb.connect(":memory:")
    try:
        rows = connection.execute(
            query, [pattern, *symbol_list, *dates]
        ).fetchall()
    finally:
        connection.close()
    bars: dict[tuple[str, str], list[tuple[str, float]]] = defaultdict(list)
    for symbol, trade_date, time_text, price in rows:
        value = float(price)
        if value > 0:
            bars[(str(symbol), str(trade_date))].append((str(time_text), value))
    return dict(bars)


def _write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        fieldnames = list(dict.fromkeys(
            key for row in rows for key in row
        ))
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def _format_distribution(
    summary: dict[str, Any], metric: str, days: int, percentage: bool = False
) -> str:
    n = summary[f"{metric}_n"]
    days = summary.get(f"{metric}_days", days)
    median = summary.get(f"{metric}_median")
    p25 = summary.get(f"{metric}_p25")
    p75 = summary.get(f"{metric}_p75")
    if median is None:
        return f"— (n={n}, d={days}・参考値)"
    reference = "・参考値" if n < MIN_REFERENCE_SAMPLE else ""
    if percentage:
        return (
            f"{median:.2f}% [{p25:.2f}%, {p75:.2f}%] "
            f"(n={n}, d={days}{reference})"
        )
    return f"{median:.2f} [{p25:.2f}, {p75:.2f}] (n={n}, d={days}{reference})"


def _format_rate(summary: dict[str, Any], metric: str, days: int) -> str:
    n = summary[f"{metric}_n"]
    days = summary.get(f"{metric}_days", days)
    count = summary.get(f"{metric}_count", 0)
    rate = summary.get(f"{metric}_rate")
    if rate is None:
        return f"— ({count}/{n}, d={days}・参考値)"
    reference = "・参考値" if n < MIN_REFERENCE_SAMPLE else ""
    return f"{rate * 100:.1f}% ({count}/{n}, d={days}{reference})"


def build_markdown(
    results: dict[str, dict[str, list[str]]],
    common: list[str],
    coverage: list[dict[str, Any]],
    summaries: list[dict[str, Any]],
    rows: list[dict[str, Any]],
    issues: list[str],
) -> str:
    baseline_dates = sorted(results.get("270円", {}))
    missing_dates = {
        band: [day for day in baseline_dates if day not in results.get(band, {})]
        for band in ("450円", "900円")
    }
    comparison_pairs = sum(
        item["common_candidate_symbol_days"] for item in coverage
    )
    common_minute = sum(item["common_minute_parquet_pairs"] for item in coverage)
    common_daily = sum(item["common_daily_cache_pairs"] for item in coverage)
    regimes = {
        (row["date"], row["market_regime"], row["market_regime_available"])
        for row in rows
    }
    regime_labels = []
    for day, regime, available in sorted(regimes):
        label = f"{day}={regime}"
        if not available:
            label += "(入力不足)"
        regime_labels.append(label)
    unavailable_regimes = sorted({
        row["date"]
        for row in rows
        if not row["market_regime_available"]
    })
    lines = [
        "# 価格帯別比較",
        "",
        "## この表から言えること / 言えないこと",
        f"- 比較C（朝の板ベース）で3帯の結果ファイルがそろった日は **{len(common)}日**（{', '.join(common) if common else 'なし'}）、候補銘柄日数は合計 **{comparison_pairs}件**です。A（日足候補）、B（昼分足）、D（朝昼重なり）は方式を分けて集計しています。",
        f"- 3帯共通日の分足Parquetは **{common_minute}/{comparison_pairs}件**、日足キャッシュは **{common_daily}/{comparison_pairs}件**あります。欠損行はシグナル成立を判定できません。",
        f"- 270円を基準に結果ファイルが欠ける日数は450円帯 **{len(missing_dates['450円'])}日**、900円帯 **{len(missing_dates['900円'])}日**で、比較期間は全データ期間より短くなっています。",
        f"- 市場レジーム（当日より前の日経/VIX日足で再計算）: {', '.join(regime_labels) or '評価日なし'}。入力不足で見送り判定を確定できない日は {', '.join(unavailable_regimes) or 'なし'} です。",
        "- 共通期間またはシグナルのサンプルが少ないため、勝率・収益性・11月以降の再現性は推定できません。少数サンプルの値は「参考値」です。",
        "",
        "## 比較C：前回の朝の板ベース分足",
        "",
        "| 価格帯 | 候補銘柄日数 | シグナル前 (件/日) | 0シグナル日割合 | 見送り後 | 主な見送り理由 (レジームRSI / ATR DANGER / Market DANGER) | 最大上昇 ATR (中央値 [25,75]) | 最大下落 ATR (中央値 [25,75]) | 引け騰落 ATR (中央値 [25,75]) | 利確到達率 | 損切り到達率 | コスト/ATR | 1ティック/株価 | 0株率 (1/2/3枠) | 先利確 / 先損切 / 引け |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for summary in summaries:
        day_count = summary["comparison_days"]
        zero_rate = summary["zero_signal_day_rate"]
        zero_text = (
            f"{summary['zero_signal_days']}/{day_count} "
            f"({zero_rate * 100:.1f}%, d={summary['signal_evaluable_days']}"
            f"{'・参考値' if summary['signal_evaluable_days'] < MIN_REFERENCE_SAMPLE else ''})"
            if zero_rate is not None else f"— (d=0/{day_count}評価可能日)"
        )
        average_text = (
            f"{summary['pre_signal_per_day']:.2f}"
            if summary["pre_signal_per_day"] is not None else "—"
        )
        reason_text = (
            f"{summary['primary_skip_regime_rsi']}/{summary['pre_signal_count']} "
            f"(d={day_count}) / "
            f"{summary['primary_skip_atr_danger']}/{summary['pre_signal_count']} "
            f"(d={day_count}) / "
            f"{summary['primary_skip_market_danger']}/{summary['pre_signal_count']} "
            f"(d={day_count})"
        )
        outcome_text = " / ".join(
            _format_rate(summary, f"first_{name}", day_count)
            for name in ("利確", "損切り", "引け")
        )
        zero_share_text = " / ".join(
            _format_rate(summary, f"zero_shares_{slots}_slots", day_count)
            for slots in (1, 2, 3)
        )
        lines.append(
            f"| {summary['band']} | {summary['candidate_symbol_days']} "
            f"(n={summary['candidate_symbol_days']}, d={day_count}) "
            f"| {summary['pre_signal_count']} / {day_count} = "
            f"{average_text} "
            f"(n={summary['pre_signal_count']}, d={day_count}"
            f"{'・参考値' if summary['pre_signal_count'] < MIN_REFERENCE_SAMPLE else ''}) "
            f"| {zero_text} | {summary['post_signal_count']} "
            f"(n={summary['post_signal_count']}, d={day_count}"
            f"{'・参考値' if summary['post_signal_count'] < MIN_REFERENCE_SAMPLE else ''}) "
            f"| {reason_text} | {_format_distribution(summary, 'max_up_atr', day_count)} "
            f"| {_format_distribution(summary, 'max_down_atr', day_count)} "
            f"| {_format_distribution(summary, 'close_atr', day_count)} "
            f"| {_format_rate(summary, 'profit_reached', day_count)} "
            f"| {_format_rate(summary, 'stop_reached', day_count)} "
            f"| {_format_distribution(summary, 'cost_atr_ratio', day_count)} "
            f"| {_format_distribution(summary, 'tick_percent', day_count, percentage=True)} "
            f"| {zero_share_text} | {outcome_text} |"
        )
    lines.extend([
        "",
        "## データ範囲・欠損",
        "",
        "| 価格帯 | 結果ファイル日数 / 期間 | 候補銘柄日数 | 銘柄数 | 分足Parquet | 日足キャッシュ | 共通期間の候補日数 | 共通期間 分足 / 日足 |",
        "|---|---:|---:|---:|---:|---:|---:|---:|",
    ])
    for item in coverage:
        period = (
            f"{item['date_start']}〜{item['date_end']}"
            if item["date_start"] else "—"
        )
        minute = (
            f"{item['minute_parquet_pairs']}/{item['candidate_symbol_days']} "
            f"({item['minute_parquet_rate'] * 100:.1f}%)"
            if item["minute_parquet_rate"] is not None else "対象なし"
        )
        cache = (
            f"{item['daily_cache_symbols']}/{item['unique_symbols']} "
            f"({item['daily_cache_rate'] * 100:.1f}%)"
            if item["daily_cache_rate"] is not None else "対象なし"
        )
        prior_daily = (
            f"{item['prior_daily_pairs']}/{item['candidate_symbol_days']} "
            f"({item['prior_daily_rate'] * 100:.1f}%)"
            if item["prior_daily_rate"] is not None else "対象なし"
        )
        lines.append(
            f"| {item['band']} | {item['result_days']} / {period} "
            f"| {item['candidate_symbol_days']} | {item['unique_symbols']} "
            f"| {minute} | {cache} / 前日足 {prior_daily} | "
            f"{item['common_candidate_symbol_days']} | "
            f"{item['common_minute_parquet_pairs']} / {item['common_prior_daily_pairs']} |"
        )
    lines.extend([
        "",
        f"- 3帯共通日: {', '.join(common) if common else 'なし'}",
        f"- 450円帯で270円の結果日から欠ける日: {', '.join(missing_dates['450円']) or 'なし'}",
        f"- 900円帯で270円の結果日から欠ける日: {', '.join(missing_dates['900円']) or 'なし'}",
        "",
        "## 定義・注意点",
        "",
        f"- シグナル前は通常レジームの本番RSI基準 ({config.RSI_ENTRY_THRESHOLD:g}) を使った最初のBUY成立、見送り後は本番のCAUTION/DANGER時RSI基準・ATR DANGER・Market DANGERを反映。理由別件数は同時成立する理由があり得るため、表は優先理由で排他的に集計しています。",
        f"- RSI/価格バンドは対象日より前の60暦日の日足終値（本番Yahoo取得の60日範囲）、ATRは対象日前の日足のみで本番のATR({config.ATR_PERIOD}) / DANGER判定を使用。市場レジームは対象日より前の指数日足のみを本番UseCaseに渡して再計算します。",
        "- 従来の比較表の値幅・コスト・到達割合は見送り前シグナルが母数です。このレポートでは別節に、見送りシグナルと見送り後シグナルの当日引け仮想損益を追加しています。",
        f"- 値幅・到達判定はシグナル成立時刻以降の分足価格系列のみ。分足に高値・安値がないため、足内の到達順や実際の約定を示しません。利確到達は成立価格+ATR×ボラティリティ別ATR_PROFIT_LOCK倍率、損切りは成立価格−ATR×{config.ATR_STOP_NORMAL_MULTIPLIER:g}で固定計算しています（本番の追従ストップ約定再現ではありません）。",
        f"- コストはシグナル前の成立価格で計算し、ペーパー設定の往復手数料 ({config.PAPER_FEE_RATE:.5%}×2)、往復スリッページ ({config.PAPER_MARKET_SLIPPAGE_BPS:g}bps×2)、往復のスプレッド目安1ティックを含めます。呼値は東証普通株の株価帯別標準単位を適用し、特殊銘柄区分による別呼値は識別していません。0株率はシグナル前の成立価格で100株を、各枠の予算 min(運用資本/枠数, 1注文上限) で買えない割合です。",
        "- 枠数・保有銘柄数・注文回数の制約はシグナル数に適用していません。取引停止、売買停止、注文拒否、歩み値、実スプレッドも再現していません。",
    ])
    if issues:
        lines.extend(["", "## 入力データの不整合・欠損メモ", ""])
        lines.extend(f"- {issue}" for issue in issues)
    return "\n".join(lines) + "\n"


def daily_midday_rows(
    midday_selections: dict[str, dict[str, dict[str, Any]]],
    midday_rows: list[dict[str, Any]],
    midday_missing: list[dict[str, Any]],
    dates: tuple[str, ...] = B_COMPARISON_DATES,
) -> list[dict[str, Any]]:
    """昼推定のシグナル・見送り理由を価格帯×日別に集計する。"""
    missing_keys = {
        (item["band"], item["date"], item["symbol"])
        for item in midday_missing
    }
    reason_columns = {
        "market_regime_rsi": "skip_market_regime_rsi",
        "atr_danger": "skip_atr_danger",
        "market_regime_danger": "skip_market_danger",
        "market_regime_unavailable": "skip_regime_unavailable",
    }
    summaries = []
    for band in BAND_ORDER:
        for day in dates:
            selection = midday_selections.get(band, {}).get(day)
            if selection is None:
                continue
            selected_symbols = selection["symbols"]
            day_rows = [
                row for row in midday_rows
                if row["band"] == band and row["date"] == day
            ]
            pre_signals = [row for row in day_rows if row.get("pre_signal")]
            summary = {
                "comparison_mode": "昼推定選定（09:35以降）",
                "band": band,
                "date": day,
                "selected_symbols": len(selected_symbols),
                "minute_available": sum(
                    (band, day, symbol) not in missing_keys
                    for symbol in selected_symbols
                ),
                "minute_missing": sum(
                    (band, day, symbol) in missing_keys
                    for symbol in selected_symbols
                ),
                "pre_signals": len(pre_signals),
                "post_signals": sum(row.get("post_signal") is True for row in pre_signals),
                "post_signal_evaluable": sum(
                    row.get("post_signal") is not None for row in pre_signals
                ),
                "post_signal_unavailable": sum(
                    row.get("post_signal") is None for row in pre_signals
                ),
            }
            summary.update({
                column: sum(row.get("primary_skip_reason") == reason for row in pre_signals)
                for reason, column in reason_columns.items()
            })
            summary.update({
                "skip_caution_rsi": sum(
                    row.get("rejection_category") == "caution_rsi" for row in pre_signals
                ),
                "skip_atr_danger": sum(
                    row.get("rejection_category") == "atr_danger" for row in pre_signals
                ),
                "skip_other": sum(
                    row.get("rejection_category") == "other" for row in pre_signals
                ),
            })
            summaries.append(summary)
    return summaries


def _daily_distribution_text(summary: dict[str, Any], metric: str) -> str:
    count = summary[f"{metric}_n"]
    days = summary[f"{metric}_days"]
    median = summary[f"{metric}_median"]
    if median is None:
        return f"— (n={count}, d={days}・参考値)"
    reference = "・参考値" if count < MIN_REFERENCE_SAMPLE else ""
    return (
        f"{median:.2f} [{summary[f'{metric}_p25']:.2f}, "
        f"{summary[f'{metric}_p75']:.2f}] (n={count}, d={days}{reference})"
    )


def build_extended_markdown(
    prior_report: str,
    screening_summaries: list[dict[str, Any]],
    screening_rows: list[dict[str, Any]],
    midday_summaries: list[dict[str, Any]],
    midday_selections: dict[str, dict[str, dict[str, Any]]],
    midday_rows: list[dict[str, Any]],
    midday_missing: list[dict[str, Any]],
    atr_population_summaries: list[dict[str, Any]],
    virtual_trade_rows: list[dict[str, Any]],
    outcome_summaries: list[dict[str, Any]],
    danger_scale_summaries: list[dict[str, Any]],
    windows: list[dict[str, Any]],
    overlaps: list[dict[str, Any]],
    mismatch_rows: list[dict[str, Any]],
    screening_dates: tuple[str, ...] = A_COMPARISON_DATES,
    midday_dates: tuple[str, ...] = B_COMPARISON_DATES,
    selection_atr_rows: list[dict[str, Any]] | None = None,
    selection_atr_summaries: list[dict[str, Any]] | None = None,
    source_band_summaries: list[dict[str, Any]] | None = None,
    rank_relationships: list[dict[str, Any]] | None = None,
    atr_cutoff_rows: list[dict[str, Any]] | None = None,
    index_return_rows: list[dict[str, Any]] | None = None,
) -> str:
    """比較A/B/Dを追加し、前回の比較Cレポートをそのまま保持する。"""
    report_title, _, report_body = prior_report.rstrip().partition("\n")
    lines = [
        report_title,
        "",
        "## ATR DANGER判定の定義",
        "",
        f"- ATRは{config.ATR_PERIOD}本の日足True Rangeの単純平均です。各日のTrue Rangeは max(高値−安値, |高値−前日終値|, |安値−前日終値|) です。",
        f"- ATR比 = 最新確定日足のTrue Range ÷ 同日までの直近{config.ATR_PERIOD}本のATR。したがって比率は値幅（円）をATR（円）で割った無次元値です。昼推定日の判定では当日の日足を使わず、対象日より前に確定した日足のみで評価します。",
        f"- 設定閾値はCAUTIONがATR比≥{config.ATR_CAUTION_RATIO:g}、DANGERがATR比≥{config.ATR_DANGER_RATIO:g}です。ATR DANGER時の現行動作は `ATR_DANGER_ACTION={config.ATR_DANGER_ACTION}`（数量を0にして新規買いを見送り）です。",
        "",
        report_body,
        "",
        "## 比較A：スクリーニング候補ベース（日足近似）",
        "",
    ]
    valid_candidates = sum(row["daily_ohlc_available"] for row in screening_rows)
    confirmed_through = latest_confirmed_trading_day(datetime.now())
    future_rows = sum(
        date.fromisoformat(row["evaluation_date"]) > confirmed_through
        for row in screening_rows
    )
    matured_candidates = len(screening_rows) - future_rows
    matured_rows = [
        row for row in screening_rows
        if date.fromisoformat(row["evaluation_date"]) <= confirmed_through
    ]
    matured_available = sum(row["daily_ohlc_available"] for row in matured_rows)
    matured_missing = len(matured_rows) - matured_available
    matured_atr_available = sum(row.get("atr") is not None for row in matured_rows)
    matured_atr_missing = len(matured_rows) - matured_atr_available
    mapping = "、".join(
        f"{date.fromisoformat(trade_date).month}/{date.fromisoformat(trade_date).day}"
        f"→{date.fromisoformat(next_trading_day(trade_date)).month}/"
        f"{date.fromisoformat(next_trading_day(trade_date)).day}"
        for trade_date in screening_dates
    )
    screening_period = (
        f"{screening_dates[0]}〜{screening_dates[-1]}"
        if screening_dates else "対象日なし"
    )
    lines.extend([
        "### この表から言えること / 言えないこと",
        f"- {screening_period}の3帯スクリーニング結果を銘柄×日で統合し、候補 **{len(screening_rows)}件**（翌営業日OHLCあり **{valid_candidates}件**）を価格監査値で区分しました。",
        f"- 評価日はスクリーニング日の翌営業日（{mapping}）です。未到来の評価日分 **{future_rows}件**を除く候補は **{matured_candidates}件**で、OHLC確認済み **{matured_available}件**、ATR算出可能 **{matured_atr_available}件**でした。",
        f"- 日足キャッシュを参照し、集計中の追加取得はしていません。到来済み評価日のOHLC未確認 **{matured_missing}件**、ATR算出不可 **{matured_atr_missing}件**は指標から除外し、CSVの候補明細に理由を記録しています。",
        "- 価格区分はスクリーニング監査日の価格で決め、同じ銘柄×日の重複は1件です。ATRは評価日の前日までの日足だけで計算します。",
        "- 日足高値・安値・始値・終値からの近似であり、エントリーシグナルの件数、分足の値動き、到達順、約定可能性を示しません。",
        "",
        "| 価格区分 | 候補銘柄日 (n, d) | 翌営業日OHLCあり (n, d) | 日中値幅/ATR | 始値から最大上昇/ATR | 始値から最大下落/ATR | 引け騰落/ATR |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ])
    for summary in screening_summaries:
        candidate_days = len({
            row["date"] for row in screening_rows if row["price_group"] == summary["price_group"]
        })
        lines.append(
            f"| {summary['price_group']} | {summary['candidate_symbol_days']} "
            f"(n={summary['candidate_symbol_days']}, d={candidate_days}) "
            f"| {summary['daily_ohlc_n']}/{summary['candidate_symbol_days']} "
            f"(n={summary['daily_ohlc_n']}, d={summary['daily_ohlc_days']}) "
            f"| {_daily_distribution_text(summary, 'daily_range_atr')} "
            f"| {_daily_distribution_text(summary, 'open_max_up_atr')} "
            f"| {_daily_distribution_text(summary, 'open_max_down_atr')} "
            f"| {_daily_distribution_text(summary, 'close_change_atr')} |"
        )

    selected_total = sum(
        len(selection["symbols"])
        for by_date in midday_selections.values()
        for selection in by_date.values()
    )
    strategy_evaluable = sum(row["signal_evaluated"] for row in midday_rows)
    post_signal_unavailable = sum(
        row.get("pre_signal") and not row.get("market_regime_available")
        for row in midday_rows
    )
    lines.extend([
        "",
        "## 比較B：昼推定選定銘柄の分足（09:35以降）",
        "",
        "### この表から言えること / 言えないこと",
        f"- 昼推定診断から対象期間内の選定銘柄 **{selected_total}件**を読み、実際の窓・足時刻は下の窓一覧に記録しました。値動き・シグナル評価は09:35以降に限定しています。",
        f"- 09:35以降の分足がない選定銘柄日は **{len(midday_missing)}件**です。欠損はゼロリターンにせず除外し、対象数と欠損内訳を別掲します。",
        f"- 分足があり、日足指標を含む戦略判定まで評価できた銘柄日は **{strategy_evaluable}/{selected_total - len(midday_missing)}件**です。レジームデータ不足で見送り後を判定できないシグナルは **{post_signal_unavailable}件**です。",
        "- 朝の板ベース選定は混ぜていません。昼の選定10銘柄/帯/日は候補抽出の条件付き標本であり、30件未満の指標は「参考値」です。",
        "",
        "| 比較方式 | 価格帯 | 選定銘柄日数 | 分足あり(09:35以降) | 分足欠損 | 評価可能銘柄日 | シグナル前(観測数 / 完全日平均) | 0シグナル日 | 見送り後 | 見送り理由(CAUTION RSI/ATR DANGER/その他/不明) | 最大上昇 ATR (中央値 [25,75]) | 最大下落 ATR (中央値 [25,75]) | 引け騰落 ATR (中央値 [25,75]) | 利確到達 | 損切り到達 | コスト/ATR | 1ティック/株価 | 0株率 (1/2/3枠) | 先利確/先損切/引け |",
        "|---|---|---:|---:|---:|---:|---:|---:|---:|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ])
    for summary in midday_summaries:
        day_count = len(midday_dates)
        selected = summary["selected_symbol_days"]
        missing = summary["missing_minute_symbol_days"]
        metrics = [
            _format_distribution(summary, key, day_count)
            for key in ("max_up_atr", "max_down_atr", "close_atr")
        ]
        rates = [
            _format_rate(summary, key, day_count)
            for key in ("profit_reached", "stop_reached")
        ]
        cost = _format_distribution(summary, "cost_atr_ratio", day_count)
        tick = _format_distribution(summary, "tick_percent", day_count, percentage=True)
        shares = " / ".join(
            _format_rate(summary, f"zero_shares_{slots}_slots", day_count)
            for slots in (1, 2, 3)
        )
        outcomes = " / ".join(
            _format_rate(summary, f"first_{name}", day_count)
            for name in ("利確", "損切り", "引け")
        )
        zero_rate = summary["zero_signal_day_rate"]
        zero_text = (
            f"{summary['zero_signal_days']}/{summary['signal_evaluable_days']} "
            f"({zero_rate * 100:.1f}%, d={summary['signal_evaluable_days']}"
            f"{'・参考値' if summary['signal_evaluable_days'] < MIN_REFERENCE_SAMPLE else ''})"
            if zero_rate is not None else f"— (d=0/{len(midday_dates)}評価可能日)"
        )
        average = (
            f"{summary['pre_signal_count']}観測 / "
            f"{summary['pre_signal_per_day']:.2f}件/完全日 "
            f"(n={summary['complete_day_pre_signal_count']}, "
            f"d={summary['signal_evaluable_days']}"
            f"{'・参考値' if summary['complete_day_pre_signal_count'] < MIN_REFERENCE_SAMPLE else ''})"
            if summary["pre_signal_per_day"] is not None else
            f"{summary['pre_signal_count']}観測 / —件/完全日 (d=0)"
        )
        reasons = (
            f"{summary['skip_caution_rsi']}/"
            f"{summary['skip_atr_danger']}/"
            f"{summary['skip_other']}/"
            f"{summary['primary_skip_market_regime_unavailable']} "
            f"(n={summary['pre_signal_count']}, d={summary['pre_signal_days']})"
        )
        post_text = (
            f"{summary['post_signal_count']}/{summary['post_signal_evaluable_count']} "
            f"(n={summary['post_signal_evaluable_count']}, "
            f"d={summary['post_signal_days']}"
            f"{'・参考値' if summary['post_signal_evaluable_count'] < MIN_REFERENCE_SAMPLE else ''})"
            if summary["post_signal_evaluable_count"] else
            f"評価不可 (対象シグナル n={summary['post_signal_unavailable_count']})"
            if summary["post_signal_unavailable_count"] else "— (対象シグナルなし)"
        )
        lines.append(
            f"| 昼推定09:35以降 | {summary['band']} "
            f"| {selected} (n={selected}, d={day_count}"
            f"{'・参考値' if selected < MIN_REFERENCE_SAMPLE else ''}) "
            f"| {summary['minute_after_935_n']} "
            f"(n={summary['minute_after_935_n']}, d={day_count}) "
            f"| {missing} (n={missing}, d={day_count}) "
            f"| {summary['strategy_evaluable_symbol_days']} "
            f"(n={summary['strategy_evaluable_symbol_days']}, d={day_count}) "
            f"| {average} | {zero_text} "
            f"| {post_text} "
            f"| {reasons} | {' | '.join(metrics)} | {' | '.join(rates)} | {cost} | {tick} "
            f"| {shares} | {outcomes} |"
        )
    lines.extend([
        "",
        "### 見送り後シグナルの日別内訳",
        "",
        "見送り理由は排他的に分類しています。CAUTION時RSI見送りがATR DANGERと同時に成立する場合はCAUTION RSIを優先し、ATR DANGER件数には重ねて数えません。「その他」にはMarket DANGER等を含みます。レジーム入力不足の日は見送り後を判定不可とし、買える件数には含めていません。",
        "",
        "| 価格帯 | 日付 | 選定数 | 分足あり/欠損 | シグナル前 | 見送り後 | 判定不可 | CAUTION RSI | ATR DANGER | その他 | レジーム不明 |",
        "|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ])
    for item in daily_midday_rows(
        midday_selections, midday_rows, midday_missing, midday_dates
    ):
        lines.append(
            f"| {item['band']} | {item['date']} | {item['selected_symbols']} "
            f"| {item['minute_available']}/{item['minute_missing']} "
            f"| {item['pre_signals']} | {item['post_signals']} "
            f"| {item['post_signal_unavailable']} | {item['skip_caution_rsi']} "
            f"| {item['skip_atr_danger']} | {item['skip_other']} "
            f"| {item['skip_regime_unavailable']} |"
        )
    lines.extend([
        "",
        "### ATR DANGERの価格帯・ATR比との関係",
        "",
        "ATR DANGER率は各昼推定選定銘柄日の、判定時点より前の日足ATR評価を母数にしています。実価格区分は09:35以降の最初の分足価格です。",
        "",
        "| 区分種別 | 区分 | 選定銘柄日 | ATR評価可 | ATR DANGER | DANGER率 | CAUTION | ATR比中央値 | 09:35価格中央値 |",
        "|---|---|---:|---:|---:|---:|---:|---:|---:|",
    ])
    for item in atr_population_summaries:
        danger_rate = (
            f"{item['atr_danger_rate'] * 100:.1f}%"
            if item["atr_danger_rate"] is not None else "—"
        )
        atr_median = (
            f"{item['atr_ratio_median']:.2f}"
            if item["atr_ratio_median"] is not None else "—"
        )
        price_median = (
            f"{item['price_median']:.2f}"
            if item["price_median"] is not None else "—"
        )
        lines.append(
            f"| {item['group_type']} | {item['group']} | {item['selected_symbol_days']} "
            f"| {item['atr_evaluable']} | {item['atr_danger_count']} | {danger_rate} "
            f"| {item['atr_caution_count']} | {atr_median} | {price_median} |"
        )
    lines.extend([
        "",
        "ATR DANGER率と価格の差はこの選定標本内の記述統計です。価格帯と日足値幅・ATR比は交絡しうるため、因果や一般化は示しません。",
        "",
        f"### 見送り後シグナル{len(virtual_trade_rows)}件：100株仮想注文と引け損益",
        "",
        "注文上限は注文金額のみで判定し、買付余力・保有枠・売買停止は考慮していません。100株を買える場合、09:35以降の最後の分足価格で引け決済した損益を表示します（手数料・往復スリッページ・1ティック相当を控除）。利確/損切り到達は分足系列で閾値を観測したかを示し、実約定価格ではありません。",
        "",
        "| 帯 | 日付 | 銘柄 | 成立時刻/価格 | 30,000円上限 | 50,000円上限 | 100,000円上限 | 利確到達 | 損切り到達 | 初回到達 | 引け騰落 ATR | 引け損益(粗利/コスト控除後,100株) |",
        "|---|---|---|---:|---|---|---|---|---|---|---:|---:|",
    ])
    for item in virtual_trade_rows:
        def capacity_text(cap: int) -> str:
            possible = item.get(f"buy_100_with_{cap}_cap")
            return "可" if possible else "不可"

        gross = item.get("virtual_gross_pnl_100")
        net = item.get("virtual_net_pnl_100")
        pnl = f"{gross:+.0f}円 / {net:+.0f}円" if gross is not None and net is not None else "—"
        lines.append(
            f"| {item['band']} | {item['date']} | {item['symbol']} "
            f"| {item['signal_time']} / {item['entry_price']:.2f}円 "
            f"| {capacity_text(30_000)} | {capacity_text(50_000)} "
            f"| {capacity_text(100_000)} "
            f"| {'はい' if item['profit_reached'] else 'いいえ'} "
            f"| {'はい' if item['stop_reached'] else 'いいえ'} "
            f"| {item['first_touch']} ({item['first_touch_time'] or '未到達'}) "
            f"| {item['close_atr']:+.2f} "
            f"| {pnl} |"
        )
    lines.extend([
        "",
        "### 見送り理由別の当日損益（100株仮想・コスト控除後）",
        "",
        "成立シグナル価格で100株買い、大引けで決済したと仮定します。見送り群で純損益<0は「損失回避」、>0は「取り逃し」、=0は損益なしです。損益ATR比は純損益÷(ATR円×100株)、円損益は往復手数料・想定スリッページ・1ティック相当控除後です。見送り後群は比較用対照です。30件未満は参考値です。",
        "",
        "| 帯 | 理由/対照 | シグナル数 | 評価可能 | 損失回避/損失件数 | 取り逃し/利益件数 | 損益なし | 純損益ATR比合計 | 純損益ATR比中央値 | 純損益合計 | 純損益中央値 |",
        "|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ])
    for item in outcome_summaries:
        reference = "・参考値" if item["reference_value"] else ""
        atr_sum = f"{item['net_pnl_atr_sum']:+.2f}" if item["evaluable_count"] else "—"
        atr_median = (
            f"{item['net_pnl_atr_median']:+.2f}"
            if item["net_pnl_atr_median"] is not None else "—"
        )
        yen_sum = f"{item['net_pnl_yen_sum']:+.0f}円" if item["evaluable_count"] else "—"
        yen_median = (
            f"{item['net_pnl_yen_median']:+.0f}円"
            if item["net_pnl_yen_median"] is not None else "—"
        )
        lines.append(
            f"| {item['band']} | {item['reason']} | {item['signal_count']} "
            f"| {item['evaluable_count']}{reference} | {item['loss_count']} "
            f"| {item['profit_count']} | {item['breakeven_count']} "
            f"| {atr_sum} | {atr_median} | {yen_sum} | {yen_median} |"
        )
    lines.extend([
        "",
        "### ATR DANGER時の数量縮小シナリオ（450円・900円帯、参考値）",
        "",
        "現行の見送りを仮に数量縮小へ置き換え、各ATR DANGER見送りシグナルを100株基準の75%・50%・25%（比例按分した仮想株数）で引け決済した損益です。損益は価格経路が同じなら株数に比例すると仮定しています。日本株の通常の売買単位は100株のため、75/50/25株は実際には発注できず、発注可否・価格インパクトも評価しない参考値です。30件未満は参考値です。",
        "",
        "| 帯 | 縮小率 | 仮想株数/件 | シグナル数 | 損失件数 | 利益件数 | 純損益合計 | 純損益中央値 | 1件あたり純損益ATR比中央値 |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|",
    ])
    for item in danger_scale_summaries:
        reference = "・参考値" if item["reference_value"] else ""
        net_sum = f"{item['net_pnl_yen_sum']:+.0f}円" if item["signal_count"] else "—"
        net_median = (
            f"{item['net_pnl_yen_median']:+.0f}円"
            if item["net_pnl_yen_median"] is not None else "—"
        )
        atr_median = (
            f"{item['net_pnl_atr_median']:+.2f}"
            if item["net_pnl_atr_median"] is not None else "—"
        )
        lines.append(
            f"| {item['band']} | {item['scale_ratio']:.0%} | "
            f"{item['equivalent_shares']:g}株 | {item['signal_count']}{reference} "
            f"| {item['loss_count']} | {item['profit_count']} | {net_sum} "
            f"| {net_median} | {atr_median} |"
        )
    lines.extend([
        "",
        "### 昼推定診断の窓",
        "",
        "| 価格帯 | 日付 | 診断の窓 | 入力足の最初 | 入力足の最後 | 入力銘柄数 | 選定数 |",
        "|---|---|---|---|---|---:|---:|",
    ])
    for item in windows:
        lines.append(
            f"| {item['band']} | {item['date']} | {item['window'] or '記録なし'} "
            f"| {', '.join(item['first_bars']) or '記録なし'} "
            f"| {', '.join(item['last_bars']) or '記録なし'} "
            f"| {item['input_count']} | {item['adopted_count']} |"
        )
    lines.extend([
        "",
        "### 09:35以降の分足欠損",
        "",
        "| 価格帯 | 日付 | 銘柄 | 分足件数（全日/09:35以降） |",
        "|---|---|---|---:|",
    ])
    if midday_missing:
        for item in midday_missing:
            lines.append(
                f"| {item['band']} | {item['date']} | {item['symbol']} "
                f"| {item['all_minute_bars']}/{item['after_935_bars']} |"
            )
    else:
        lines.append("| — | — | 欠損なし | 0 |")

    overlap_total = sum(item["overlap_count"] for item in overlaps)
    morning_total = sum(item["morning_count"] for item in overlaps)
    midday_total = sum(item["midday_count"] for item in overlaps)
    lines.extend([
        "",
        "## 比較D：270円帯の朝・昼選定の重なり",
        "",
        "### この表から言えること / 言えないこと",
        f"- {len(overlaps)}日で朝選定 **{morning_total}件**・昼選定 **{midday_total}件**に対し、同日共通は **{overlap_total}件**でした（朝母数比 {overlap_total / morning_total * 100:.1f}%）。" if morning_total else "- 朝の選定銘柄がないため一致率を計算できません。",
        "- 一致は選定銘柄集合の重なりだけを示し、昼推定方式の予測精度や将来の再現性は示しません。",
        "- 日別の差分銘柄は、朝/昼の順位・倍率を診断JSONから付記しています。倍率の母数や入力期間が異なるため倍率同士は直接同一尺度ではありません。",
        "",
        "| 日付 | 朝選定数 | 昼選定数 | 一致件数 | 朝に対する一致率 | 昼に対する一致率 | Jaccard |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ])
    for item in overlaps:
        morning_rate = item["morning_overlap_rate"]
        midday_rate = item["midday_overlap_rate"]
        jaccard = item["jaccard_rate"]
        lines.append(
            f"| {item['date']} | {item['morning_count']} | {item['midday_count']} "
            f"| {item['overlap_count']} "
            f"| {morning_rate * 100:.1f}% ({item['overlap_count']}/{item['morning_count']}"
            f"{'・参考値' if item['morning_count'] < MIN_REFERENCE_SAMPLE else ''})"
            if morning_rate is not None else
            f"| {item['date']} | {item['morning_count']} | {item['midday_count']} | {item['overlap_count']} | — "
        )
        lines[-1] += (
            f" | {midday_rate * 100:.1f}% ({item['overlap_count']}/{item['midday_count']}"
            f"{'・参考値' if item['midday_count'] < MIN_REFERENCE_SAMPLE else ''})"
            f" | {jaccard * 100:.1f}% (n={item['morning_count'] + item['midday_count'] - item['overlap_count']}"
            f"{'・参考値' if item['morning_count'] + item['midday_count'] - item['overlap_count'] < MIN_REFERENCE_SAMPLE else ''})"
            if midday_rate is not None and jaccard is not None else " | — | —"
        ) + " |"
    lines.extend([
        "",
        "### 一致しなかった銘柄（順位・倍率）",
        "",
        "| 日付 | 銘柄 | 朝選定 | 朝順位 | 朝倍率 | 昼選定 | 昼順位 | 昼倍率 |",
        "|---|---|---:|---:|---:|---:|---:|---:|",
    ])
    if mismatch_rows:
        for item in mismatch_rows:
            morning_ratio = item["morning_ratio"]
            midday_ratio = item["midday_ratio"]
            lines.append(
                f"| {item['date']} | {item['symbol']} | "
                f"{'はい' if item['morning_selected'] else 'いいえ'} "
                f"| {item['morning_rank'] if item['morning_rank'] is not None else '—'} "
                f"| {f'{morning_ratio:.3f}' if morning_ratio is not None else '—'} "
                f"| {'はい' if item['midday_selected'] else 'いいえ'} "
                f"| {item['midday_rank'] if item['midday_rank'] is not None else '—'} "
                f"| {f'{midday_ratio:.3f}' if midday_ratio is not None else '—'} |"
            )
    else:
        lines.append("| — | 差分なし | — | — | — | — | — | — |")
    selection_atr_rows = selection_atr_rows or []
    selection_atr_summaries = selection_atr_summaries or []
    source_band_summaries = source_band_summaries or []
    rank_relationships = rank_relationships or []
    atr_cutoff_rows = atr_cutoff_rows or []
    index_return_rows = index_return_rows or []
    evaluation_candidates = [
        row for row in selection_atr_rows
        if row.get("evaluation_ohlc_available")
    ]
    selection_atr_available = sum(
        row.get("selection_atr_ratio") is not None
        for row in evaluation_candidates
    )
    lines.extend([
        "",
        "## 選定時点ATR比と翌営業日の値動き",
        "",
        "### この表から言えること / 言えないこと",
        f"- 3帯スクリーニング候補は銘柄×日で重複排除し、翌営業日OHLCを確認できた **{len(evaluation_candidates)}件**を評価しました。選定時点ATR比を算出できたのは **{selection_atr_available}件**です。",
        f"- ATR比はスクリーニング日の確定日足TR÷その日までの直近{config.ATR_PERIOD}本ATRです。NORMALは<{config.ATR_CAUTION_RATIO:g}、CAUTIONは≥{config.ATR_CAUTION_RATIO:g}かつ<{config.ATR_DANGER_RATIO:g}、DANGERは≥{config.ATR_DANGER_RATIO:g}です。翌営業日の値幅も同じ選定時点ATRで正規化しています。",
        "- 価格区分は選定日の監査価格に基づき、未来の日足を選定時点ATRには使っていません。各セルn<30は参考値です。これは候補抽出後の条件付き標本で、ATR比による因果効果や将来の収益性を示しません。",
        "",
        "| 監査価格区分 | 選定時ATRレベル | 翌営業日OHLCあり | 日中値幅/ATR 中央値 [25%,75%] | 始値から最大上昇/ATR | 始値から最大下落/ATR | 引け騰落/ATR |",
        "|---|---|---:|---:|---:|---:|---:|",
    ])
    for item in selection_atr_summaries:
        def selection_distribution(metric: str) -> str:
            count = item[f"{metric}_n"]
            if not count:
                return "— (n=0)"
            marker = "・参考値" if count < MIN_REFERENCE_SAMPLE else ""
            return (
                f"{item[f'{metric}_median']:.2f} "
                f"[{item[f'{metric}_p25']:.2f}, {item[f'{metric}_p75']:.2f}] "
                f"(n={count}{marker})"
            )

        lines.append(
            f"| {item['price_group']} | {item['selection_atr_level']} "
            f"| {item['candidate_count']} (n={item['candidate_count']}) "
            f"| {selection_distribution('daily_range_atr')} "
            f"| {selection_distribution('open_max_up_atr')} "
            f"| {selection_distribution('open_max_down_atr')} "
            f"| {selection_distribution('close_change_atr')} |"
        )
    lines.extend([
        "",
        "### スクリーニング元価格帯別の内訳",
        "",
        "この表は各帯のスクリーニング50銘柄を母集団にした帯別内訳です。同一銘柄×日が複数帯に含まれる場合、帯ごとに計上されるため、上の重複排除済み801件とは合計が一致しません。",
        "",
        "| スクリーニング帯 | 選定時ATRレベル | 翌営業日OHLCあり | 日中値幅/ATR 中央値 [25%,75%] | 始値から最大上昇/ATR | 始値から最大下落/ATR | 引け騰落/ATR |",
        "|---|---|---:|---:|---:|---:|---:|",
    ])
    for item in source_band_summaries:
        def source_distribution(metric: str) -> str:
            count = item[f"{metric}_n"]
            if not count:
                return "— (n=0)"
            marker = "・参考値" if count < MIN_REFERENCE_SAMPLE else ""
            return (
                f"{item[f'{metric}_median']:.2f} "
                f"[{item[f'{metric}_p25']:.2f}, {item[f'{metric}_p75']:.2f}] "
                f"(n={count}{marker})"
            )

        lines.append(
            f"| {item['screening_band']} | {item['selection_atr_level']} "
            f"| {item['candidate_count']} (n={item['candidate_count']}) "
            f"| {source_distribution('daily_range_atr')} "
            f"| {source_distribution('open_max_up_atr')} "
            f"| {source_distribution('open_max_down_atr')} "
            f"| {source_distribution('close_change_atr')} |"
        )
    lines.extend([
        "",
        "### 順位要素と選定時ATR比",
        "",
        "順位1が最上位です。全体のSpearman相関は順位数値とATR比の単調関係、順位四分位のDANGER率は順位の高低ごとの構成差を表します。四分位1が上位25%です。",
        "",
        "| 順位要素 | 範囲 | n | Spearman ρ | ATR比中央値 | DANGER件数 | DANGER率 |",
        "|---|---|---:|---:|---:|---:|---:|",
    ])
    for item in rank_relationships:
        rho = item["spearman_rho"]
        median = item["atr_ratio_median"]
        danger_rate = item["danger_rate"]
        marker = "・参考値" if item["sample_count"] < MIN_REFERENCE_SAMPLE else ""
        lines.append(
            f"| {item['factor']} | {item['segment']} | {item['sample_count']}{marker} "
            f"| {f'{rho:.3f}' if rho is not None else '—'} "
            f"| {f'{median:.2f}' if median is not None else '—'} "
            f"| {item['danger_count']} "
            f"| {f'{danger_rate * 100:.1f}%' if danger_rate is not None else '—'} |"
        )
    lines.extend([
        "",
        "### ATR比2.0未満に絞った場合の残存数",
        "",
        "候補50銘柄はスクリーニング元の各帯・各日で計数し、銘柄×日で帯内重複は除外済みです。昼上位10は昼診断がある日のみ、診断JSONの採用銘柄を再順位付けせず、その10銘柄内でATR比<2.0の数を計数しています。",
        "",
        "| スクリーニング帯 | 日付 | 候補数 | ATR評価可 | ATR比<2.0 | 昼採用数 | 昼ATR評価可 | 昼採用中ATR比<2.0 |",
        "|---|---|---:|---:|---:|---:|---:|---:|",
    ])
    for item in atr_cutoff_rows:
        day_marker = (
            "・参考値" if item["screening_atr_evaluable"] < MIN_REFERENCE_SAMPLE
            else ""
        )
        lines.append(
            f"| {item['band']} | {item['date']} | {item['screening_candidate_count']} "
            f"| {item['screening_atr_evaluable']} "
            f"| {item['screening_below_danger_count']}{day_marker} "
            f"| {item['midday_adopted_count'] if item['midday_adopted_count'] is not None else '—'} "
            f"| {item['midday_atr_evaluable'] if item['midday_atr_evaluable'] is not None else '—'} "
            f"| {item['midday_below_danger_count'] if item['midday_below_danger_count'] is not None else '—'}"
            f"{'・参考値' if item['midday_atr_evaluable'] is not None and item['midday_atr_evaluable'] < MIN_REFERENCE_SAMPLE else ''} |"
        )
    lines.extend([
        "",
        "### 日経225の日次騰落",
        "",
        "| 日付 | 比較終値日 | 前日終値 | 当日終値 | 騰落率 |",
        "|---|---|---:|---:|---:|",
    ])
    for item in index_return_rows:
        change = item["change_percent"]
        lines.append(
            f"| {item['date']} | {item['previous_close_date'] or '—'} "
            f"| {item['previous_close'] if item['previous_close'] is not None else '—'} "
            f"| {item['close'] if item['close'] is not None else '—'} "
            f"| {f'{change:+.2f}%' if change is not None else '— (指数日足不足)'} |"
        )
    return "\n".join(lines) + "\n"


def analyze(
    project_root: Path = PROJECT_ROOT,
    output_dir: Path = OUTPUT_DIR,
    start_date: date | None = None,
    end_date: date | None = None,
) -> dict[str, Any]:
    if (start_date is None) != (end_date is None):
        raise ValueError("start_dateとend_dateは両方指定してください")
    if start_date is not None and end_date is not None and start_date > end_date:
        raise ValueError("start_dateはend_date以前の日付にしてください")
    results, issues = load_filter_results(project_root)
    if start_date is None:
        screening_dates = list(A_COMPARISON_DATES)
        midday_dates = list(B_COMPARISON_DATES)
    else:
        screening_dirs = [
            project_root / relative_dir for relative_dir in SCREENING_INPUTS.values()
        ]
        screening_dates = dates_in_range(
            discover_json_dates(screening_dirs), start_date, end_date
        )
        midday_dirs = [
            project_root / "data" / "filtering_midday" / folder / "diagnostics"
            for folder in ("270", "450", "900")
        ]
        midday_dates = dates_in_range(
            discover_json_dates(midday_dirs), start_date, end_date
        )
    screening_dates_tuple = tuple(screening_dates)
    midday_dates_tuple = tuple(midday_dates)
    if start_date is not None and end_date is not None:
        results = {
            band: {
                day: symbols
                for day, symbols in by_date.items()
                if start_date.isoformat() <= day <= end_date.isoformat()
            }
            for band, by_date in results.items()
        }
    common = common_dates(results)
    screening_candidates, screening_issues = load_screening_candidates(
        project_root, screening_dates_tuple
    )
    issues.extend(screening_issues)
    midday_selections, midday_issues = load_midday_selections(
        project_root, midday_dates_tuple
    )
    issues.extend(midday_issues)
    all_symbols = {
        symbol
        for by_date in results.values()
        for symbols in by_date.values()
        for symbol in symbols
    }
    all_symbols.update(row["symbol"] for row in screening_candidates)
    all_symbols.update(
        symbol
        for by_date in midday_selections.values()
        for selection in by_date.values()
        for symbol in selection["symbols"]
    )
    daily_cache_dir = project_root / "data" / "cache" / "yahoo_daily"
    daily_caches, cache_issues = load_daily_caches(all_symbols, daily_cache_dir)
    issues.extend(cache_issues)
    coverage = _coverage_summary(results, common, project_root, daily_caches)
    index_cache: dict[str, dict[str, Any]] = {}
    for symbol in ("^N225", "^VIX"):
        path = daily_cache_dir / f"{symbol}.json"
        if not path.is_file():
            issues.append(f"{symbol}: 市場指数の日足キャッシュなし")
            index_cache[symbol] = {}
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(payload, dict):
                raise ValueError("JSONルートがオブジェクトではありません")
            index_cache[symbol] = payload
        except (OSError, json.JSONDecodeError, ValueError) as exc:
            issues.append(f"{symbol}: 市場指数キャッシュを読めません ({type(exc).__name__})")
            index_cache[symbol] = {}

    common_symbols = {
        symbol
        for band in BAND_ORDER
        for day in common
        for symbol in results.get(band, {}).get(day, [])
    }
    midday_symbols = {
        symbol
        for by_date in midday_selections.values()
        for selection in by_date.values()
        for symbol in selection["symbols"]
    }
    minute_dates = sorted(set(common) | set(midday_dates_tuple))
    minute_prices = _read_minute_prices(
        project_root / "data" / "minute_bars_parquet",
        minute_dates,
        common_symbols | midday_symbols,
    )
    regime_by_day: dict[str, tuple[MarketRegime, bool, str | None]] = {
        day: _market_regime_for_day(day, index_cache)
        for day in sorted(set(common) | set(midday_dates_tuple))
    }
    for day in midday_dates_tuple:
        regime, available, reason = regime_by_day[day]
        if not available:
            issues.append(
                f"比較B: {day}のMarketRegime入力不足のため見送り後判定不可 "
                f"(fallback={regime.value}, {reason})"
            )
    rows: list[dict[str, Any]] = []
    for band in BAND_ORDER:
        for day in common:
            regime, regime_available, regime_reason = regime_by_day[day]
            for symbol in results[band][day]:
                rows.append(analyze_candidate_day(
                    band,
                    symbol,
                    day,
                    minute_prices.get((symbol, day), []),
                    daily_caches.get(symbol),
                    regime,
                    regime_available,
                    regime_reason,
                ))
    summaries = [summarize_band(band, rows, common) for band in BAND_ORDER]

    screening_rows = [
        analyze_screening_candidate_day(candidate, daily_caches.get(candidate["symbol"]))
        for candidate in screening_candidates
    ]
    screening_summaries = summarize_screening_candidates(screening_rows)
    selection_atr_rows = [
        analyze_selection_atr_candidate(
            candidate,
            daily_caches.get(candidate["symbol"]),
            evaluation_row,
        )
        for candidate, evaluation_row in zip(screening_candidates, screening_rows)
    ]
    selection_atr_summaries = summarize_selection_atr_buckets(selection_atr_rows)
    source_band_summaries = summarize_selection_atr_source_bands(selection_atr_rows)
    rank_relationships = summarize_selection_rank_relationship(selection_atr_rows)
    atr_cutoff_rows = summarize_selection_atr_cutoffs(
        selection_atr_rows,
        screening_candidates,
        midday_selections,
        screening_dates_tuple,
    )
    index_return_rows = summarize_index_daily_returns(
        index_cache.get("^N225", {}),
        tuple(day for day in midday_dates_tuple if day in B_COMPARISON_DATES),
    )

    midday_rows: list[dict[str, Any]] = []
    midday_missing: list[dict[str, Any]] = []
    atr_population_rows: list[dict[str, Any]] = []
    windows: list[dict[str, Any]] = []
    midday_summaries: list[dict[str, Any]] = []
    for band in BAND_ORDER:
        band_rows: list[dict[str, Any]] = []
        band_selected_count = 0
        band_missing_count = 0
        for trade_date in midday_dates_tuple:
            selection = midday_selections.get(band, {}).get(trade_date)
            if selection is None:
                continue
            selected_symbols = selection["symbols"]
            band_selected_count += len(selected_symbols)
            windows.append({
                "band": band,
                "date": trade_date,
                "window": selection["window"],
                "first_bars": selection["first_bars"],
                "last_bars": selection["last_bars"],
                "input_count": selection["candidates"].__len__(),
                "adopted_count": len(selected_symbols),
            })
            for symbol in selected_symbols:
                full_bars = minute_prices.get((symbol, trade_date), [])
                after_935 = minutes_from_time(full_bars, "09:35")
                price_at_935 = after_935[0][1] if after_935 else None
                prior_daily = _daily_bars_before(
                    daily_caches.get(symbol, {}), trade_date
                )
                atr_assessment = assess_volatility(
                    [bar for _, bar in prior_daily],
                    config.ATR_PERIOD,
                    config.ATR_CAUTION_RATIO,
                    config.ATR_DANGER_RATIO,
                )
                atr_population_rows.append({
                    "band": band,
                    "date": trade_date,
                    "symbol": symbol,
                    "reference_price": price_at_935,
                    "price_group": price_bucket(price_at_935),
                    "atr": atr_assessment.atr if atr_assessment else None,
                    "atr_ratio": atr_assessment.ratio if atr_assessment else None,
                    "atr_level": atr_assessment.level.value if atr_assessment else None,
                })
                if not after_935:
                    band_missing_count += 1
                    midday_missing.append({
                        "band": band,
                        "date": trade_date,
                        "symbol": symbol,
                        "all_minute_bars": len(full_bars),
                        "after_935_bars": 0,
                    })
                    continue
                regime, regime_available, regime_reason = regime_by_day.get(
                    trade_date, (MarketRegime.NORMAL, False, "レジーム評価なし")
                )
                candidate_row = analyze_candidate_day(
                    band,
                    symbol,
                    trade_date,
                    after_935,
                    daily_caches.get(symbol),
                    regime,
                    regime_available,
                    regime_reason,
                )
                candidate_row["comparison_mode"] = "昼推定選定（09:35以降）"
                candidate_row["selected_rank"] = selection["candidates"].get(
                    symbol, {}
                ).get("rank")
                candidate_row["selected_ratio"] = selection["candidates"].get(
                    symbol, {}
                ).get("ratio")
                band_rows.append(candidate_row)
        band_summary = summarize_band(band, band_rows, list(midday_dates_tuple))
        complete_days = []
        for trade_date in midday_dates_tuple:
            selected_count = len(
                midday_selections.get(band, {}).get(trade_date, {}).get("symbols", [])
            )
            evaluated_rows = [
                row for row in band_rows
                if row["date"] == trade_date and row["signal_evaluated"]
            ]
            if selected_count and len(evaluated_rows) == selected_count:
                complete_days.append(trade_date)
        complete_day_rows = [
            row for row in band_rows if row["date"] in complete_days
        ]
        complete_signal_count = sum(row["pre_signal"] for row in complete_day_rows)
        complete_zero_days = sum(
            not any(
                row["pre_signal"] for row in complete_day_rows
                if row["date"] == trade_date
            )
            for trade_date in complete_days
        )
        band_summary["signal_evaluable_days"] = len(complete_days)
        band_summary["zero_signal_days"] = complete_zero_days
        band_summary["zero_signal_day_rate"] = (
            complete_zero_days / len(complete_days) if complete_days else None
        )
        band_summary["complete_day_pre_signal_count"] = complete_signal_count
        band_summary.update({
            "comparison_mode": "昼推定選定（09:35以降）",
            "selected_symbol_days": band_selected_count,
            "minute_after_935_n": len(band_rows),
            "missing_minute_symbol_days": band_missing_count,
            "strategy_evaluable_symbol_days": sum(
                row["signal_evaluated"] for row in band_rows
            ),
            "strategy_unavailable_symbol_days": sum(
                not row["signal_evaluated"] for row in band_rows
            ),
        })
        band_summary["pre_signal_per_day"] = (
            complete_signal_count / band_summary["signal_evaluable_days"]
            if band_summary["signal_evaluable_days"] else None
        )
        midday_rows.extend(band_rows)
        midday_summaries.append(band_summary)

    midday_daily_summaries = daily_midday_rows(
        midday_selections, midday_rows, midday_missing, midday_dates_tuple
    )
    atr_population_summaries = summarize_atr_population(atr_population_rows)
    virtual_trade_rows = [
        {
            key: row.get(key)
            for key in (
                "band", "date", "symbol", "signal_time", "entry_price",
                "close_price", "atr", "atr_ratio", "atr_level", "rsi",
                "market_regime", "max_up_atr", "max_down_atr", "close_atr",
                "profit_reached", "stop_reached", "first_touch",
                "first_touch_time", "first_touch_price", "cost_atr_ratio",
                "virtual_close_return_percent", "virtual_gross_pnl_100",
                "virtual_net_pnl_100",
            )
        } | {
            f"buy_100_with_{cap}_cap": row.get(f"buy_100_with_{cap}_cap")
            for cap in VIRTUAL_ORDER_CAPS
        }
        for row in midday_rows
        if row.get("post_signal") is True
    ]
    outcome_summaries = summarize_skip_outcomes(midday_rows)
    danger_scale_summaries = summarize_atr_danger_scales(midday_rows)
    overlaps: list[dict[str, Any]] = []
    mismatch_rows: list[dict[str, Any]] = []
    for trade_date in midday_dates_tuple:
        morning_symbols = results.get("270円", {}).get(trade_date, [])
        if trade_date not in results.get("270円", {}):
            issues.append(f"比較D: 朝270円帯のフィルタ結果なし ({trade_date})")
        morning_payload, morning_issue = _load_diagnostic(
            project_root / "data" / "filtering_diagnostics", trade_date
        )
        if morning_issue:
            issues.append(f"比較D: {morning_issue}")
        midday_selection = midday_selections.get("270円", {}).get(trade_date)
        if midday_selection is None:
            continue
        morning_details = {
            str(item["symbol"]): item
            for item in (morning_payload or {}).get("candidates", [])
            if isinstance(item, dict) and isinstance(item.get("symbol"), (str, int))
        }
        midday_details = midday_selection["candidates"]
        overlap, differences = compare_selected_sets(
            trade_date,
            morning_symbols,
            midday_selection["symbols"],
            morning_details,
            midday_details,
        )
        overlap["low_overlap"] = (
            overlap["morning_overlap_rate"] is not None
            and overlap["morning_overlap_rate"] < 0.5
        )
        overlaps.append(overlap)
        mismatch_rows.extend(differences)

    output_dir.mkdir(parents=True, exist_ok=True)
    _write_csv(output_dir / "comparison.csv", summaries)
    _write_csv(output_dir / "daily.csv", [
        {
            "band": band,
            "date": day,
            "candidate_symbols": len(results[band][day]),
            "pre_signals": sum(
                row["pre_signal"] for row in rows
                if row["band"] == band and row["date"] == day
            ),
            "post_signals": sum(
                row["post_signal"] is True for row in rows
                if row["band"] == band and row["date"] == day
            ),
            "market_regime": regime_by_day[day][0].value,
            "market_regime_available": regime_by_day[day][1],
            "signal_evaluable": all(
                row["signal_evaluated"] for row in rows
                if row["band"] == band and row["date"] == day
            ),
            "zero_signal_day": not any(
                row["pre_signal"] for row in rows
                if row["band"] == band and row["date"] == day
            ),
        }
        for band in BAND_ORDER
        for day in common
    ])
    _write_csv(output_dir / "symbols.csv", rows)
    _write_csv(output_dir / "data_availability.csv", coverage)
    _write_csv(output_dir / "comparison_a.csv", screening_summaries)
    _write_csv(output_dir / "candidates_a.csv", screening_rows)
    _write_csv(
        output_dir / "selection_atr_candidates.csv",
        [row for row in selection_atr_rows if row.get("evaluation_ohlc_available")],
    )
    _write_csv(output_dir / "selection_atr_buckets.csv", selection_atr_summaries)
    _write_csv(
        output_dir / "selection_atr_source_band_buckets.csv",
        source_band_summaries,
    )
    _write_csv(output_dir / "selection_atr_rank_relationship.csv", rank_relationships)
    _write_csv(output_dir / "selection_atr_cutoff_counts.csv", atr_cutoff_rows)
    _write_csv(output_dir / "nikkei225_daily_returns.csv", index_return_rows)
    _write_csv(output_dir / "comparison_b.csv", midday_summaries)
    _write_csv(output_dir / "daily_b.csv", midday_daily_summaries)
    _write_csv(output_dir / "symbols_b.csv", midday_rows)
    _write_csv(output_dir / "atr_danger_b.csv", atr_population_rows)
    _write_csv(output_dir / "atr_danger_summary_b.csv", atr_population_summaries)
    _write_csv(output_dir / "virtual_post_trades_b.csv", virtual_trade_rows)
    _write_csv(output_dir / "skip_outcomes_b.csv", outcome_summaries)
    _write_csv(output_dir / "atr_danger_scales_b.csv", danger_scale_summaries)
    _write_csv(output_dir / "missing_b.csv", midday_missing)
    _write_csv(output_dir / "windows_b.csv", windows)
    _write_csv(output_dir / "overlap_d.csv", overlaps)
    _write_csv(output_dir / "mismatches_d.csv", mismatch_rows)
    report = build_extended_markdown(
        build_markdown(results, common, coverage, summaries, rows, issues),
        screening_summaries,
        screening_rows,
        midday_summaries,
        midday_selections,
        midday_rows,
        midday_missing,
        atr_population_summaries,
        virtual_trade_rows,
        outcome_summaries,
        danger_scale_summaries,
        windows,
        overlaps,
        mismatch_rows,
        screening_dates_tuple,
        midday_dates_tuple,
        selection_atr_rows,
        selection_atr_summaries,
        source_band_summaries,
        rank_relationships,
        atr_cutoff_rows,
        index_return_rows,
    )
    (output_dir / "report.md").write_text(report, encoding="utf-8")
    return {
        "common_dates": common,
        "screening_dates": screening_dates,
        "midday_dates": midday_dates,
        "coverage": coverage,
        "summaries": summaries,
        "rows": rows,
        "screening_summaries": screening_summaries,
        "screening_rows": screening_rows,
        "selection_atr_rows": selection_atr_rows,
        "selection_atr_summaries": selection_atr_summaries,
        "source_band_summaries": source_band_summaries,
        "rank_relationships": rank_relationships,
        "atr_cutoff_rows": atr_cutoff_rows,
        "index_return_rows": index_return_rows,
        "midday_summaries": midday_summaries,
        "midday_rows": midday_rows,
        "atr_population_rows": atr_population_rows,
        "atr_population_summaries": atr_population_summaries,
        "virtual_trade_rows": virtual_trade_rows,
        "outcome_summaries": outcome_summaries,
        "danger_scale_summaries": danger_scale_summaries,
        "midday_missing": midday_missing,
        "windows": windows,
        "overlaps": overlaps,
        "mismatches": mismatch_rows,
        "issues": issues,
        "output_dir": output_dir,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=None,
        help="Markdown/CSVを書き出すフォルダ（省略時は既定先。期間指定時は期間名の新規フォルダ）",
    )
    parser.add_argument("--start-date", type=date.fromisoformat, help="対象期間の開始日 (YYYY-MM-DD)")
    parser.add_argument("--end-date", type=date.fromisoformat, help="対象期間の終了日 (YYYY-MM-DD)")
    args = parser.parse_args()
    if (args.start_date is None) != (args.end_date is None):
        parser.error("--start-dateと--end-dateは両方指定してください")
    if args.start_date is not None and args.end_date is not None:
        if args.start_date > args.end_date:
            parser.error("--start-dateは--end-date以前にしてください")
        output_dir = args.output_dir or (
            PROJECT_ROOT / "data" / "analysis_output"
            / f"price_band_comparison_{args.start_date.isoformat()}_{args.end_date.isoformat()}"
        )
    else:
        output_dir = args.output_dir or OUTPUT_DIR
    result = analyze(
        output_dir=output_dir,
        start_date=args.start_date,
        end_date=args.end_date,
    )
    print(
        f"比較A対象日: {', '.join(result['screening_dates']) or 'なし'}; "
        f"比較B/D対象日: {', '.join(result['midday_dates']) or 'なし'}"
    )
    print(f"3帯共通日: {', '.join(result['common_dates']) or 'なし'}")
    for item in result["coverage"]:
        print(
            f"{item['band']}: 結果={item['result_days']}日 / "
            f"銘柄日={item['candidate_symbol_days']} / "
            f"分足={item['minute_parquet_pairs']}/{item['candidate_symbol_days']} / "
            f"日足={item['daily_cache_symbols']}/{item['unique_symbols']}"
        )
    print(f"出力先: {result['output_dir']}")


if __name__ == "__main__":
    main()
