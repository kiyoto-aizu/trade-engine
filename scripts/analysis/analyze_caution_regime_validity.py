"""市場状態CAUTIONと関連する保存データを読み取り専用で調査します。"""
from __future__ import annotations

import json
import sqlite3
from collections import Counter
from contextlib import closing
from datetime import date, datetime, time
from pathlib import Path

import pyarrow.parquet as parquet

ROOT = Path(__file__).resolve().parents[2]
TREND_DATABASE = ROOT / "data" / "analysis" / "trend_check.sqlite3"
EVENT_DATABASE = ROOT / "data" / "state" / "filter_decision_events.sqlite3"
PARQUET_ROOT = ROOT / "data" / "minute_bars_parquet"
INDEX_CACHE = ROOT / "data" / "cache" / "yahoo_daily" / "^N225.json"
START_DATE = "2026-09-01"
EVENT_TYPES = ("MARKET_REGIME_CAUTION_RSI_FILTER", "ATR_DANGER_SKIP")
EXIT_CUTOFF = time(15, 20)
OUTCOME_LABELS = {1: "取り逃し", -1: "損失回避", 0: "値動きなし"}


def _connect_read_only(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    return connection


def _version_key(version: str) -> tuple:
    digits = "".join(character for character in version if character.isdigit())
    return (int(digits) if digits else -1, version)


def _load_trend_days() -> list[dict]:
    with closing(_connect_read_only(TREND_DATABASE)) as connection:
        rows = connection.execute(
            """
            SELECT trade_date, version, regime,
                   selected_count, selected_judged, selected_trend,
                   candidate_count, candidate_judged, candidate_trend
            FROM trend_daily_summaries
            WHERE trade_date >= ?
            ORDER BY trade_date, version
            """,
            (START_DATE,),
        ).fetchall()
    latest_by_day: dict[str, dict] = {}
    for row in rows:
        item = dict(row)
        current = latest_by_day.get(item["trade_date"])
        if current is None or _version_key(item["version"]) > _version_key(current["version"]):
            latest_by_day[item["trade_date"]] = item
    return list(latest_by_day.values())


def _trend_groups(days: list[dict]) -> dict[str, dict]:
    groups = {
        "CAUTION": {"days": 0, "selected": [0, 0], "candidate": [0, 0]},
        "他": {"days": 0, "selected": [0, 0], "candidate": [0, 0]},
        "状態不明": {"days": 0, "selected": [0, 0], "candidate": [0, 0]},
    }
    for day in days:
        regime = day.get("regime")
        key = "状態不明" if not regime else "CAUTION" if regime == "CAUTION" else "他"
        group = groups[key]
        group["days"] += 1
        group["selected"][0] += int(day.get("selected_trend") or 0)
        group["selected"][1] += int(day.get("selected_judged") or 0)
        group["candidate"][0] += int(day.get("candidate_trend") or 0)
        group["candidate"][1] += int(day.get("candidate_judged") or 0)
    return groups


def _rate_text(group: dict, field: str) -> str:
    trend, judged = group[field]
    if not judged:
        return "判定可能件数なし"
    return f"{trend}/{judged}件 ({trend / judged:.1%})"


def _is_reference(group: dict, field: str) -> bool:
    return group["days"] < 10 or group[field][1] < 30


def _load_events() -> list[dict]:
    with closing(_connect_read_only(EVENT_DATABASE)) as connection:
        rows = connection.execute(
            """
            SELECT event_type, execution_mode, occurred_at, symbol, reference_price, quantity
            FROM filter_decision_events
            WHERE event_type IN (?, ?)
            ORDER BY occurred_at, symbol, id
            """,
            EVENT_TYPES,
        ).fetchall()
    return [dict(row) for row in rows]


def _parse_time(value: object) -> datetime | None:
    if value is None:
        return None
    parsed = value if isinstance(value, datetime) else datetime.fromisoformat(str(value))
    return parsed.replace(tzinfo=None) if parsed.tzinfo is not None else parsed


def _load_intraday_prices(symbol: str, day: str) -> list[dict]:
    path = PARQUET_ROOT / f"symbol={symbol}" / f"date={day}" / "data.parquet"
    if not path.is_file():
        return []
    rows = parquet.read_table(path, columns=["time", "price"]).to_pylist()
    prices = [
        {"time": _parse_time(row["time"]), "price": float(row["price"])}
        for row in rows
        if row["time"] is not None and row["price"] is not None
    ]
    return sorted(prices, key=lambda row: row["time"])


def _event_pnl(event: dict) -> float | None:
    occurred = _parse_time(event.get("occurred_at"))
    if occurred is None or event.get("reference_price") is None or event.get("quantity") is None:
        return None
    cutoff = datetime.combine(occurred.date(), EXIT_CUTOFF)
    after_event = [
        row for row in _load_intraday_prices(str(event["symbol"]), occurred.date().isoformat())
        if occurred < row["time"] <= cutoff
    ]
    if not after_event:
        return None
    return (
        float(after_event[-1]["price"]) - float(event["reference_price"])
    ) * int(event["quantity"])


def _skip_outcomes(events: list[dict]) -> dict[str, dict]:
    result = {
        event_type: {
            "events": 0,
            "matched": 0,
            "unmatched": 0,
            "outcomes": Counter(),
            "pnl": 0.0,
        }
        for event_type in EVENT_TYPES
    }
    for event in events:
        summary = result[event["event_type"]]
        summary["events"] += 1
        pnl = _event_pnl(event)
        if pnl is None:
            summary["unmatched"] += 1
            continue
        outcome = OUTCOME_LABELS[(pnl > 0) - (pnl < 0)]
        summary["matched"] += 1
        summary["outcomes"][outcome] += 1
        summary["pnl"] += pnl
    return result


def _load_nikkei_cache() -> dict:
    if not INDEX_CACHE.is_file():
        return {}
    data = json.loads(INDEX_CACHE.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        return {}
    return data


def _previous_daily_close(cache: dict, day: str) -> float | None:
    previous_dates = [cached_day for cached_day in cache if cached_day < day]
    if not previous_dates:
        return None
    previous_day = max(previous_dates)
    if (date.fromisoformat(day) - date.fromisoformat(previous_day)).days > 3:
        return None
    item = cache[previous_day]
    if isinstance(item, dict):
        for key in ("close", "Close", "adjclose", "Adj Close"):
            value = item.get(key)
            if value is not None:
                return float(value)
    return None


def _caution_intraday(days: list[dict], cache: dict) -> tuple[list[tuple[str, str]], int]:
    caution_days = [item["trade_date"] for item in days if item.get("regime") == "CAUTION"]
    available = []
    missing = 0
    for day in caution_days:
        bars = _load_intraday_prices("^N225", day)
        if not bars:
            missing += 1
            continue
        previous_close = _previous_daily_close(cache, day)
        if previous_close is None or previous_close <= 0:
            available.append((day, "分足あり・比較基準終値なし"))
            continue
        changes = [(row["price"] / previous_close - 1) * 100 for row in bars]
        available.append((day, f"{min(changes):+.2f}%〜{max(changes):+.2f}%"))
    return available, missing


def build_report() -> str:
    days = _load_trend_days()
    groups = _trend_groups(days)
    known_regimes = {str(day["regime"]) for day in days if day.get("regime")}
    events = _load_events()
    outcomes = _skip_outcomes(events)
    cache = _load_nikkei_cache()
    caution_moves, caution_without_bars = _caution_intraday(days, cache)
    modes = Counter(str(event.get("execution_mode") or "不明") for event in events)

    if days:
        dates = [item["trade_date"] for item in days]
        span = f"{min(dates)}〜{max(dates)}"
    else:
        span = "記録なし"
    lines = [
        "# CAUTION市場状態の妥当性調査",
        "",
        "## 結論",
        "CAUTIONの厳しさは判断保留。トレンド適合率と見送り損益は参考材料にとどまり、場中の市場状態を再現できる指数等の時点データも不足している。",
        "",
        f"対象: trend_check.sqlite3の保存全期間（{span}、{len(days)}日、{START_DATE}以降）。",
        "DBはSQLite URI mode=roで読み取り、Parquet/JSONも読み取りのみ。",
        "",
        "## (a) CAUTION日と他の日のトレンド適合率",
    ]
    if len(known_regimes) <= 1:
        regime_text = "、".join(sorted(known_regimes)) if known_regimes else "市場状態記録なし"
        lines.append(f"保存された市場状態は{regime_text}の{len(known_regimes)}種類のみ。")
    lines.append("| 市場状態 | 日数 | 選定10銘柄の適合 | 候補50銘柄の適合 |")
    lines.append("|---|---:|---:|---:|")
    for label, group in groups.items():
        if not group["days"]:
            continue
        selected = _rate_text(group, "selected")
        candidates = _rate_text(group, "candidate")
        if _is_reference(group, "selected") or _is_reference(group, "candidate"):
            selected += "（参考値）" if _is_reference(group, "selected") else ""
            candidates += "（参考値）" if _is_reference(group, "candidate") else ""
        lines.append(f"| {label} | {group['days']} | {selected} | {candidates} |")
    lines.extend([
        "",
        "参考値の基準: 当該状態の日数が10日未満、または判定可能銘柄数が30件未満。",
        "各銘柄を独立標本とはみなせず、日数が少ない群の比較は特に参考値。",
        "",
        "## (b) 見送りイベントの当日値動き",
        "損益は見送り時の基準価格から15:20以前の分足最終価格まで、記録quantityで算出。手数料・滑りを含まない仮想損益。",
        "価格の後続分足がないイベントは照合不能。ATR_DANGER_SKIPは比較用。",
        "対象実行モード: " + ("、".join(f"{mode} {count}件" for mode, count in sorted(modes.items())) or "記録なし"),
        "| イベント | 全件 | 取り逃し | 損失回避 | 値動きなし | 照合不能 | 買っていた場合の損益合計 |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ])
    display_names = {
        "MARKET_REGIME_CAUTION_RSI_FILTER": "注意レジームRSI",
        "ATR_DANGER_SKIP": "ATR_DANGER_SKIP",
    }
    for event_type in EVENT_TYPES:
        item = outcomes[event_type]
        lines.append(
            f"| {display_names[event_type]} | {item['events']} | "
            f"{item['outcomes']['取り逃し']} | {item['outcomes']['損失回避']} | "
            f"{item['outcomes']['値動きなし']} | {item['unmatched']} | "
            f"{item['pnl']:+,.0f}円 |"
        )
    lines.extend([
        "",
        "## (c) 朝一CAUTION固定判定の影響",
        f"trend_check上のCAUTION日は{groups['CAUTION']['days']}日。日経225分足が見つからない日は{caution_without_bars}日。",
    ])
    if caution_moves:
        lines.append("日経225当日変化（前営業日終値比）を算出できた日:")
        lines.extend(f"- {day}: {movement}" for day, movement in caution_moves)
        lines.append(
            "指数の値動きだけでは、当日実現ボラティリティ・VIX・ADXの再評価を含む市場状態全体の判定を再現できないため、"
            "CAUTION以外へ変化したかは断定しない。"
        )
    else:
        lines.append("保存された日経225分足から場中再判定を比較できる日は確認できなかった。")
    lines.extend([
        "判定に必要だが不足している保存項目:",
        "- 各判定時刻の日経225価格、直近確定終値、元データ時刻、取得時刻（同時点の前日比を再現するため）",
        "- 各判定時刻のVIX値と取得時刻",
        "- 当時の実現ボラティリティ算出に使った日次価格系列・窓・算出値",
        "- ADXによる緩和を再現する場合のADX値・算出時刻・緩和適用結果",
    ])
    return "\n".join(lines) + "\n"


def main() -> int:
    print(build_report(), end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
