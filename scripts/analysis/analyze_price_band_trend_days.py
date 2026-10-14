"""価格帯ごとに「トレンドっぽい日」の頻度を日足キャッシュで集計する調査スクリプト。

読み取り専用。本体コード・設定・DB・状態ファイルは変更せず、出力は
data/backtest_v2_scratch/diagnostics 配下のみに書く。
"""
from __future__ import annotations

import csv
import json
import statistics
import sys
from datetime import date
from pathlib import Path
from typing import Optional

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.domain.volatility import DailyBar, calculate_atr
from src.infrastructure.persistence.listed_security_repository import ListedSecurityRepository

CACHE_DIR = PROJECT_ROOT / "data" / "cache" / "yahoo_daily"
MASTER_PATH = PROJECT_ROOT / "data" / "universe" / "listed_securities.csv"
BAND_DIRS = (
    PROJECT_ROOT / "data" / "screening_price_bands",
    PROJECT_ROOT / "data" / "filtering_price_bands",
)
OUTPUT_DIR = PROJECT_ROOT / "data" / "backtest_v2_scratch" / "diagnostics"
OUTPUT_STEM = "price_band_trend_days"

ATR_PERIOD = 14
MIN_SYMBOLS_PER_BAND = 20
BANDS = (("270以下", 0.0, 270.0), ("270-450", 270.0, 450.0), ("450-900", 450.0, 900.0))
BAND_LABELS = [label for label, _, _ in BANDS]


def band_of(price: float) -> Optional[str]:
    """前日終値から帯域を返す。下限は含まず上限は含む。900円超は対象外。"""
    for label, low, high in BANDS:
        if (price > low or low == 0.0) and price <= high:
            return label
    return None


def load_cache(cache_dir: Path = CACHE_DIR) -> dict[str, list[tuple[str, dict]]]:
    data = {}
    for path in sorted(cache_dir.glob("*.json")):
        raw = json.loads(path.read_text(encoding="utf-8"))
        rows = [(d, b) for d, b in sorted(raw.items()) if b and b.get("close")]
        if rows:
            data[path.stem] = rows
    return data


def to_daily_bar(bar: dict) -> DailyBar:
    return DailyBar(high=float(bar["high"]), low=float(bar["low"]), close=float(bar["close"]),
                    open=float(bar["open"]), volume=bar.get("volume"))


def is_trend_day_a(open_: float, high: float, low: float, close: float, atr: float) -> Optional[str]:
    """始値→終値がATR以上で、終値が高安レンジの上位/下位25%なら up/down。"""
    span = high - low
    if atr is None or atr <= 0 or span <= 0:
        return None
    if abs(close - open_) < atr:
        return None
    position = (close - low) / span
    if close > open_ and position >= 0.75:
        return "up"
    if close < open_ and position <= 0.25:
        return "down"
    return None


def trend_day_b(closes: list[float], i: int) -> Optional[str]:
    """終値が i 日目までの3日連続で同方向なら up/down（i>=3 が必要）。"""
    if i < 3:
        return None
    last = closes[i - 3:i + 1]
    if last[0] < last[1] < last[2] < last[3]:
        return "up"
    if last[0] > last[1] > last[2] > last[3]:
        return "down"
    return None


def collect_observations(cache: dict[str, list[tuple[str, dict]]]) -> list[dict]:
    """銘柄日ごとの観測値。帯域・ATRは当日を含まない前日までのデータで決める。"""
    observations = []
    for symbol, rows in cache.items():
        bars = [to_daily_bar(b) for _, b in rows]
        closes = [b.close for b in bars]
        for i in range(1, len(rows)):
            prev_close = closes[i - 1]
            band = band_of(prev_close)
            if band is None:
                continue
            bar = bars[i]
            atr = calculate_atr(bars[:i], ATR_PERIOD) if i >= ATR_PERIOD + 1 else None
            a_dir = is_trend_day_a(bar.open, bar.high, bar.low, bar.close, atr) if atr else None
            b_dir = trend_day_b(closes, i)
            volume = bar.volume
            observations.append({
                "symbol": symbol, "date": rows[i][0], "band": band, "prev_close": prev_close,
                "atr": atr, "atr_pct": (atr / prev_close * 100) if atr else None,
                "range_over_atr": ((bar.high - bar.low) / atr) if atr else None,
                "a_valid": atr is not None, "a_dir": a_dir,
                "b_valid": i >= 3, "b_dir": b_dir,
                "volume": volume, "turnover": (volume * bar.close) if volume else None,
            })
    return observations


def _mean(values: list[float]) -> Optional[float]:
    return statistics.mean(values) if values else None


def _median(values: list[float]) -> Optional[float]:
    return statistics.median(values) if values else None


def summarize_bands(observations: list[dict]) -> list[dict]:
    results = []
    for label in BAND_LABELS:
        rows = [o for o in observations if o["band"] == label]
        a_rows = [o for o in rows if o["a_valid"]]
        b_rows = [o for o in rows if o["b_valid"]]
        a_up = sum(1 for o in a_rows if o["a_dir"] == "up")
        a_down = sum(1 for o in a_rows if o["a_dir"] == "down")
        b_up = sum(1 for o in b_rows if o["b_dir"] == "up")
        b_down = sum(1 for o in b_rows if o["b_dir"] == "down")
        results.append({
            "band": label,
            "symbols": len({o["symbol"] for o in rows}),
            "symbol_days": len(rows),
            "a_days": len(a_rows), "a_trend": a_up + a_down, "a_up": a_up, "a_down": a_down,
            "a_ratio": (a_up + a_down) / len(a_rows) if a_rows else None,
            "b_days": len(b_rows), "b_trend": b_up + b_down, "b_up": b_up, "b_down": b_down,
            "b_ratio": (b_up + b_down) / len(b_rows) if b_rows else None,
            "mean_atr_yen": _mean([o["atr"] for o in a_rows]),
            "mean_atr_pct": _mean([o["atr_pct"] for o in a_rows]),
            "mean_range_over_atr": _mean([o["range_over_atr"] for o in a_rows]),
            "median_volume": _median([o["volume"] for o in rows if o["volume"]]),
            "median_turnover_yen": _median([o["turnover"] for o in rows if o["turnover"]]),
        })
    return results


def inventory(cache: dict[str, list[tuple[str, dict]]]) -> dict:
    """棚卸し。帯域は各銘柄の最新終値（＝翌営業日にとっての前日終値）基準。"""
    counts = {label: [] for label in BAND_LABELS}
    over = []
    for symbol, rows in cache.items():
        close = float(rows[-1][1]["close"])
        label = band_of(close)
        (counts[label] if label else over).append(symbol)
    lengths = [len(r) for r in cache.values()]
    dates = [r[0][0] for r in cache.values()] + [r[-1][0] for r in cache.values()]
    return {
        "symbols": len(cache),
        "days_min": min(lengths, default=0), "days_median": _median(lengths), "days_max": max(lengths, default=0),
        "period_start": min(dates, default=None), "period_end": max(dates, default=None),
        "band_symbol_counts": {k: len(v) for k, v in counts.items()},
        "over_900_symbols": len(over),
        "has_volume": any(b.get("volume") for r in cache.values() for _, b in r[:1]),
    }


def candidate_symbols(cache: dict, master_path: Path = MASTER_PATH, band_dirs=BAND_DIRS) -> list[dict]:
    """マスタに価格が無いため、帯域別スクリーニング結果に出た銘柄のうち未キャッシュのものを候補とする。"""
    try:
        listed = {s.symbol for s in ListedSecurityRepository(master_path).load_for_date(date.today())}
    except FileNotFoundError:
        listed = set()
    found: dict[tuple[str, str], set[str]] = {}
    for base in band_dirs:
        for band_name in ("450", "900"):
            for path in sorted((base / band_name).glob("*.json")) if (base / band_name).is_dir() else []:
                payload = json.loads(path.read_text(encoding="utf-8"))
                for symbol in payload.get("symbols", []):
                    found.setdefault((band_name, str(symbol)), set()).add(f"{base.name}/{path.stem}")
    rows = []
    for (band_name, symbol), sources in sorted(found.items()):
        rows.append({"price_band_screening": band_name, "symbol": symbol,
                     "in_cache": symbol in cache, "listed_in_master": symbol in listed,
                     "seen_in": len(sources)})
    return rows


def _fmt(value, digits=2, pct=False) -> str:
    if value is None:
        return "-"
    return f"{value * 100:.{digits}f}%" if pct else f"{value:,.{digits}f}"


def build_markdown(inv: dict, summary: list[dict], candidates: list[dict]) -> str:
    by = {s["band"]: s for s in summary}
    best_a = max((s for s in summary if s["a_ratio"] is not None), key=lambda s: s["a_ratio"], default=None)
    small = [s["band"] for s in summary if s["symbols"] < MIN_SYMBOLS_PER_BAND]
    lines = ["# 価格帯別トレンド日の頻度（日足キャッシュ）", "", "## 要は何がわかったか"]
    if best_a:
        lines.append(f"- 定義Aのトレンド日が最も多かった帯域は「{best_a['band']}」({_fmt(best_a['a_ratio'], 1, True)})。")
    else:
        lines.append("- 集計できる帯域がありません。")
    if small:
        lines.append(f"- ただし銘柄数が{MIN_SYMBOLS_PER_BAND}未満の帯域({', '.join(small)})があり、帯域の優劣は結論にできません。")
    else:
        lines.append("- 各帯域とも銘柄数は確保できていますが、期間が短く傾向の目安にとどまります。")
    lines.append(f"- 現在上場の銘柄のみで期間も短い（中央値{_fmt(inv['days_median'], 0)}日分）ため、あくまで参考値です。")
    lines += ["", "## 棚卸し", "",
              f"- 銘柄数: {inv['symbols']}、日数: 最小{inv['days_min']} / 中央{_fmt(inv['days_median'], 0)} / 最大{inv['days_max']}、"
              f"期間: {inv['period_start']} 〜 {inv['period_end']}",
              "- 最新終値基準の帯域別銘柄数: " + ", ".join(f"{k} {v}" for k, v in inv["band_symbol_counts"].items())
              + f"（900円超 {inv['over_900_symbols']}は対象外）", ""]
    if candidates:
        lines += [f"- 450円帯・900円帯の候補（帯域別スクリーニング結果に出た銘柄）: {len(candidates)}件、"
                  f"うち未キャッシュ {sum(1 for c in candidates if not c['in_cache'])}件。"
                  f"一覧はCSV `{OUTPUT_STEM}_candidates.csv`。株価の取得は未実施。", ""]
    lines += ["## 帯域別集計（帯域・ATRは前日までのデータ基準）", "",
              "| 帯域 | 銘柄数 | 銘柄日数 | A日数 | Aトレンド割合 | A上昇/下落 | B日数 | Bトレンド割合 | B上昇/下落 | 平均ATR(円) | 平均ATR(%) | 値幅÷ATR |",
              "|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for label in BAND_LABELS:
        s = by[label]
        lines.append(f"| {label} | {s['symbols']} | {s['symbol_days']} | {s['a_days']} | {_fmt(s['a_ratio'], 1, True)} | "
                     f"{s['a_up']}/{s['a_down']} | {s['b_days']} | {_fmt(s['b_ratio'], 1, True)} | {s['b_up']}/{s['b_down']} | "
                     f"{_fmt(s['mean_atr_yen'])} | {_fmt(s['mean_atr_pct'])} | {_fmt(s['mean_range_over_atr'])} |")
    lines += ["", "## 出来高・売買代金（キャッシュに出来高あり。売買代金は終値×出来高の概算）", "",
              "| 帯域 | 出来高中央値 | 売買代金中央値(円) |", "|---|---|---|"]
    for label in BAND_LABELS:
        s = by[label]
        lines.append(f"| {label} | {_fmt(s['median_volume'], 0)} | {_fmt(s['median_turnover_yen'], 0)} |")
    lines += ["", "## 定義", "",
              "- A: 当日の|終値-始値|がATR(14)以上、かつ終値が高安レンジの上位25%(上昇)/下位25%(下落)。ATRは本体の`calculate_atr`を前日までの日足で呼ぶ（当日を含めない）。",
              "- B: 終値が3営業日連続で同方向（厳密な大小）に動いた日（3日目を数える）。",
              "- 帯域は前日終値で判定。270以下 / 270超〜450 / 450超〜900。900円超は対象外。",
              "- 本体の`calculate_atr`は直近period本のTrue Range平均で、先頭は前日終値が無いため本数が1少ない窓になり得る。そのため必ずバー15本以上を要求している。",
              "", "## 注意点", "",
              f"- サンプル: 銘柄数{inv['symbols']}、帯域別は上表。{MIN_SYMBOLS_PER_BAND}銘柄未満や数百銘柄日未満の帯域は結論を出せない。",
              "- 現在上場している銘柄のみが対象（生き残りバイアス）。上場廃止・急落銘柄が含まれず、トレンド日の頻度は偏り得る。",
              f"- 期間が短い（キャッシュは1銘柄あたり最小{inv['days_min']}〜最大{inv['days_max']}日。依頼時の想定約84日より実際は長いが、いずれにせよ短く相場環境の影響が大きい）。",
              "- 同一日の銘柄間は独立ではなく、割合の差が偶然の範囲かは検定していない。"]
    return "\n".join(lines) + "\n"


def write_chart(summary: list[dict], path: Path) -> None:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    matplotlib.rcParams["font.family"] = ["Yu Gothic", "Meiryo", "MS Gothic", "sans-serif"]
    labels = [f"{s['band']}\n({s['symbols']}銘柄)" for s in summary]
    panels = (("トレンド日の割合(%)", [("A", "a_ratio", 100), ("B", "b_ratio", 100)]),
              ("平均ATR(価格比%)", [("ATR%", "mean_atr_pct", 1)]),
              ("日中値幅÷ATR", [("値幅/ATR", "mean_range_over_atr", 1)]))
    fig, axes = plt.subplots(1, 3, figsize=(14, 4.5))
    for ax, (title, series) in zip(axes, panels):
        width = 0.8 / len(series)
        for k, (name, key, scale) in enumerate(series):
            values = [(s[key] or 0) * scale for s in summary]
            xs = [i + (k - (len(series) - 1) / 2) * width for i in range(len(summary))]
            ax.bar(xs, values, width=width, label=name)
        ax.set_xticks(range(len(summary)))
        ax.set_xticklabels(labels)
        ax.set_title(title)
        if len(series) > 1:
            ax.legend()
    fig.tight_layout()
    fig.savefig(path, dpi=120)
    plt.close(fig)


def main() -> None:
    cache = load_cache()
    if not cache:
        raise SystemExit(f"日足キャッシュが空です: {CACHE_DIR}")
    inv = inventory(cache)
    observations = collect_observations(cache)
    summary = summarize_bands(observations)
    candidates = candidate_symbols(cache)

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    markdown = build_markdown(inv, summary, candidates)
    (OUTPUT_DIR / f"{OUTPUT_STEM}_report.md").write_text(markdown, encoding="utf-8")
    (OUTPUT_DIR / f"{OUTPUT_STEM}.json").write_text(
        json.dumps({"inventory": inv, "summary": summary, "candidates": candidates},
                   ensure_ascii=False, indent=2), encoding="utf-8")
    with (OUTPUT_DIR / f"{OUTPUT_STEM}.csv").open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(summary[0]))
        writer.writeheader()
        writer.writerows(summary)
    if candidates:
        with (OUTPUT_DIR / f"{OUTPUT_STEM}_candidates.csv").open("w", encoding="utf-8-sig", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=list(candidates[0]))
            writer.writeheader()
            writer.writerows(candidates)
    write_chart(summary, OUTPUT_DIR / f"{OUTPUT_STEM}.png")
    print(markdown)


if __name__ == "__main__":
    main()
