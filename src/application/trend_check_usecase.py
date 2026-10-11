"""トレンド答え合わせ(日次): 選定銘柄・候補全体の当日の動きを版付き基準で判定して保存する。

判定・集計のみ。売買ロジックや既存の保存データの意味には触れない(入力は読み取りのみ)。
"""
from __future__ import annotations

import json
import logging
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Callable, Optional

from src.domain.rules import calculate_rsi
from src.domain.trend_check import (
    BUILTIN_CRITERIA,
    JUDGED_LABELS,
    LABEL_TREND,
    calculate_atr_before,
    calculate_minute_auxiliary,
    judge_day,
)
from src.domain.volatility import DailyBar
from src.infrastructure.persistence.trend_check_repository import TrendCheckRepository

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DATABASE_FILE = PROJECT_ROOT / "data" / "analysis" / "trend_check.sqlite3"
DEFAULT_REPORT_DIRECTORY = PROJECT_ROOT / "data" / "reports" / "trend_check"

UNIVERSE_DIAGNOSTICS = "filtering_diagnostics"
UNIVERSE_SCREENING = "screening_previous_day"
UNIVERSE_DAILY_CACHE = "daily_cache_all"
UNIVERSE_DESCRIPTIONS = {
    UNIVERSE_DIAGNOSTICS: "当日のフィルタリング診断に保存された入力候補(スクリーニング通過銘柄)",
    UNIVERSE_SCREENING: "直前営業日のスクリーニング通過銘柄(診断ファイルが無い日の代用。当日フィルタの入力と同一)",
    UNIVERSE_DAILY_CACHE: "【代用】日足キャッシュの全銘柄(スクリーニング保存が無い日)。母集団として質が異なる",
}


@dataclass(frozen=True)
class TrendCheckPaths:
    filtering_dir: Path
    screening_dir: Path
    diagnostics_dir: Path
    daily_cache_dir: Path
    minute_bar_dir: Path
    daily_report_dir: Path
    order_history_file: Path
    decision_database: Path
    midday_filtering_dir: Path | None = None

    @classmethod
    def default(cls) -> "TrendCheckPaths":
        from src.config import config

        return cls(
            filtering_dir=Path(config.FILTERING_RESULT_DIRECTORY),
            screening_dir=Path(config.SCREENING_RESULT_DIRECTORY),
            diagnostics_dir=Path(config.FILTERING_DIAGNOSTICS_DIRECTORY),
            daily_cache_dir=PROJECT_ROOT / "data" / "cache" / "yahoo_daily",
            minute_bar_dir=Path(config.MINUTE_BAR_PARQUET_DIR),
            daily_report_dir=Path(config.DAILY_REPORT_DIRECTORY),
            order_history_file=PROJECT_ROOT / "data" / "trading" / "order_history.json",
            decision_database=Path(config.FILTER_DECISION_DATABASE_FILE),
            midday_filtering_dir=Path(config.MIDDAY_FILTERING_RESULT_ROOT),
        )


class DailyBarStore:
    """日足キャッシュ(data/cache/yahoo_daily)の読み取り専用ビュー。"""

    def __init__(self, cache_dir: Path):
        self.cache_dir = Path(cache_dir)
        self._bars: dict[str, dict[str, DailyBar]] = {}

    def clear(self) -> None:
        self._bars.clear()

    def symbols(self) -> list[str]:
        return sorted(path.stem for path in self.cache_dir.glob("*.json") if not path.stem.startswith("^"))

    def get(self, symbol: str) -> dict[str, DailyBar]:
        if symbol not in self._bars:
            path = self.cache_dir / f"{symbol}.json"
            bars: dict[str, DailyBar] = {}
            if path.exists():
                for day, values in json.loads(path.read_text(encoding="utf-8")).items():
                    try:
                        bars[day] = DailyBar(
                            high=float(values["high"]), low=float(values["low"]), close=float(values["close"]),
                            open=float(values["open"]) if values.get("open") is not None else None,
                            volume=float(values["volume"]) if values.get("volume") is not None else None,
                        )
                    except (KeyError, TypeError, ValueError):
                        continue
            self._bars[symbol] = bars
        return self._bars[symbol]


def _read_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def list_filtering_dates(paths: TrendCheckPaths) -> list[str]:
    return sorted(path.stem for path in paths.filtering_dir.glob("????-??-??.json"))


def load_selected_symbols(paths: TrendCheckPaths, trade_date: str) -> Optional[list[str]]:
    data = _read_json(paths.filtering_dir / f"{trade_date}.json")
    if not data:
        return None
    return [str(symbol) for symbol in data.get("symbols", [])]


def load_candidates(
    paths: TrendCheckPaths, trade_date: str, store: DailyBarStore
) -> tuple[list[str], str, dict[str, dict]]:
    """候補全体(スクリーニング通過銘柄)と、その出所・診断情報を返す。"""
    diagnostics = sorted(paths.diagnostics_dir.glob(f"{trade_date}_*.json"))
    for path in reversed(diagnostics):
        data = _read_json(path)
        candidates = (data or {}).get("candidates") or []
        if candidates:
            by_symbol = {str(item["symbol"]): item for item in candidates if "symbol" in item}
            return list(by_symbol), UNIVERSE_DIAGNOSTICS, by_symbol

    previous = sorted(path for path in paths.screening_dir.glob("????-??-??.json") if path.stem < trade_date)
    if previous:
        data = _read_json(previous[-1])
        symbols = [str(symbol) for symbol in (data or {}).get("symbols", [])]
        if symbols:
            return symbols, UNIVERSE_SCREENING, {}
    return store.symbols(), UNIVERSE_DAILY_CACHE, {}


def load_regime(paths: TrendCheckPaths, trade_date: str) -> Optional[str]:
    report = _read_json(paths.daily_report_dir / f"{trade_date}.json")
    regime = ((report or {}).get("market_conditions") or {}).get("regime")
    if regime:
        return str(regime)
    if not paths.decision_database.exists():
        return None
    try:
        with closing(sqlite3.connect(f"file:{paths.decision_database}?mode=ro", uri=True)) as connection:
            for table, column in (("filter_decision_events", "occurred_at"), ("decision_records", "first_occurred_at")):
                row = connection.execute(
                    f"SELECT market_regime FROM {table} WHERE {column} LIKE ? AND market_regime IS NOT NULL LIMIT 1",
                    (f"{trade_date}%",),
                ).fetchone()
                if row:
                    return str(row[0])
    except sqlite3.Error:
        logger.warning("レジームの補完読み取りに失敗しました: %s", trade_date, exc_info=True)
    return None


def load_trades(paths: TrendCheckPaths, trade_date: str) -> Optional[dict[str, dict]]:
    """銘柄ごとの{bought, pnl}。その日の注文記録が確認できない場合はNone(買った/買わないは不明)。"""
    orders: Optional[list[dict]] = None
    report = _read_json(paths.daily_report_dir / f"{trade_date}.json")
    if isinstance(report, dict) and isinstance(report.get("orders"), list):
        orders = report["orders"]
    else:
        history = _read_json(paths.order_history_file)
        if isinstance(history, list):
            todays = [item for item in history if str(item.get("timestamp", "")).startswith(trade_date)]
            if todays:
                orders = todays
    if orders is None:
        return None

    legs: dict[str, dict[str, list[tuple[float, float]]]] = {}
    for order in orders:
        if order.get("result_code") not in (0, None):
            continue
        symbol = str(order.get("symbol"))
        side = "buy" if str(order.get("side")) == "2" else "sell"
        try:
            leg = (float(order["price"]), float(order["qty"]))
        except (KeyError, TypeError, ValueError):
            continue
        legs.setdefault(symbol, {"buy": [], "sell": []})[side].append(leg)

    result: dict[str, dict] = {}
    for symbol, sides in legs.items():
        buy_qty = sum(qty for _, qty in sides["buy"])
        sell_qty = sum(qty for _, qty in sides["sell"])
        pnl = None
        if buy_qty > 0 and sell_qty > 0:
            average_buy = sum(price * qty for price, qty in sides["buy"]) / buy_qty
            average_sell = sum(price * qty for price, qty in sides["sell"]) / sell_qty
            pnl = round(min(buy_qty, sell_qty) * (average_sell - average_buy), 2)
        result[symbol] = {"bought": buy_qty > 0, "pnl": pnl}
    return result


def _minute_auxiliary(paths: TrendCheckPaths, trade_date: str, symbol: str) -> dict[str, Optional[float]]:
    empty = {"direction_consistency": None, "vwap_above_ratio": None}
    try:
        from src.infrastructure.persistence.parquet_minute_bar_repository import ParquetMinuteBarRepository

        bars = ParquetMinuteBarRepository(paths.minute_bar_dir).load_bars(date.fromisoformat(trade_date), symbol)
    except Exception:
        return empty
    return calculate_minute_auxiliary(bars) if bars else empty


def _rate(trend: int, judged: int) -> Optional[float]:
    return trend / judged if judged else None


def evaluate_day(
    trade_date: str,
    definition: dict,
    paths: TrendCheckPaths,
    store: Optional[DailyBarStore] = None,
    atr_period: Optional[int] = None,
    rsi_period: Optional[int] = None,
    rsi_minimum_closes: Optional[int] = None,
    selected_only: bool = False,
) -> Optional[tuple[list[dict], dict]]:
    """1日分を判定して(行, 日次サマリ)を返す。その日のフィルタ結果が無ければNone。"""
    from src.config import config

    atr_period = atr_period or config.ATR_PERIOD
    rsi_period = rsi_period or config.RSI_PERIOD
    rsi_minimum_closes = rsi_minimum_closes or config.RSI_MINIMUM_CLOSES
    store = store or DailyBarStore(paths.daily_cache_dir)

    selected = load_selected_symbols(paths, trade_date)
    if selected is None:
        return None
    if selected_only:
        candidates, universe_source, diagnostics = [], "selected_price_band", {}
    else:
        candidates, universe_source, diagnostics = load_candidates(paths, trade_date, store)
    regime = load_regime(paths, trade_date)
    trades = load_trades(paths, trade_date)
    version = definition["version"]

    rows: list[dict] = []
    for symbol in list(dict.fromkeys([*selected, *candidates])):
        bars = store.get(symbol)
        today_bar = bars.get(trade_date)
        atr = calculate_atr_before(bars, trade_date, atr_period)
        judgement = judge_day(today_bar, atr, definition)
        closes = [bars[key].close for key in sorted(bars) if key < trade_date]
        rsi = calculate_rsi(closes, rsi_period, rsi_minimum_closes)

        diagnostic = diagnostics.get(symbol) or {}
        price = diagnostic.get("board_current_price")
        price_source = "selection_board" if price else None
        if not price and closes:
            price, price_source = closes[-1], "previous_close"

        trade = (trades or {}).get(symbol)
        bought = None if trades is None else int(bool(trade and trade["bought"]))
        auxiliary = _minute_auxiliary(paths, trade_date, symbol) if symbol in selected else {}
        rows.append({
            "trade_date": trade_date, "symbol": symbol, "version": version,
            "in_selected": int(symbol in selected),
            "label": judgement.label, "undecidable_reason": judgement.reason,
            "open": today_bar.open if today_bar else None,
            "high": today_bar.high if today_bar else None,
            "low": today_bar.low if today_bar else None,
            "close": today_bar.close if today_bar else None,
            "atr": judgement.atr,
            "open_to_close_pct": judgement.open_to_close_pct,
            "close_position": judgement.close_position,
            "range_to_atr": judgement.range_to_atr,
            "turnover_ratio": diagnostic.get("ratio"),
            "price": price, "price_source": price_source, "rsi": rsi, "regime": regime,
            "bought": bought, "pnl": trade["pnl"] if trade else None,
            "direction_consistency": auxiliary.get("direction_consistency"),
            "vwap_above_ratio": auxiliary.get("vwap_above_ratio"),
        })

    return rows, summarize_rows(trade_date, version, universe_source, regime, rows)


def summarize_rows(trade_date: str, version: str, universe_source: str, regime: Optional[str], rows: list[dict]) -> dict:
    selected = [row for row in rows if row["in_selected"]]

    def counts(items: list[dict]) -> tuple[int, int, int]:
        judged = [row for row in items if row["label"] in JUDGED_LABELS]
        return len(items), len(judged), sum(1 for row in judged if row["label"] == LABEL_TREND)

    selected_count, selected_judged, selected_trend = counts(selected)
    candidate_count, candidate_judged, candidate_trend = counts(rows)
    bought = [row for row in selected if row["bought"]]
    pnls = [row["pnl"] for row in bought if row["pnl"] is not None]
    return {
        "trade_date": trade_date, "version": version, "universe_source": universe_source, "regime": regime,
        "selected_count": selected_count, "selected_judged": selected_judged, "selected_trend": selected_trend,
        "selected_rate": _rate(selected_trend, selected_judged),
        "candidate_count": candidate_count, "candidate_judged": candidate_judged,
        "candidate_trend": candidate_trend, "candidate_rate": _rate(candidate_trend, candidate_judged),
        "selected_undecidable": selected_count - selected_judged,
        "candidate_undecidable": candidate_count - candidate_judged,
        "bought_count": len(bought),
        "bought_trend": sum(1 for row in bought if row["label"] == LABEL_TREND),
        "pnl_total": round(sum(pnls), 2) if pnls else None,
    }


def ensure_builtin_versions(repository: TrendCheckRepository, first_date: str) -> None:
    for version, definition in BUILTIN_CRITERIA.items():
        repository.register_version(version, definition, first_date, "初期版(仮置き)")


def run_day(
    repository: TrendCheckRepository, trade_date: str, version: str, paths: TrendCheckPaths,
    store: Optional[DailyBarStore] = None,
) -> Optional[dict]:
    """判定して保存する(同じ日・同じ版は上書き)。保存したサマリを返す。"""
    registered = repository.get_version(version)
    if registered is None:
        raise ValueError(f"未登録の判定基準版です: {version}")
    result = evaluate_day(trade_date, registered["definition"], paths, store)
    if result is None:
        return None
    rows, summary = result
    repository.replace_day(trade_date, version, rows, summary)
    return summary


def recompute_all(
    repository: TrendCheckRepository, version: str, paths: TrendCheckPaths,
    progress: Optional[Callable[[str], None]] = None,
) -> list[dict]:
    """保存済みの全フィルタ結果を、元の日足・分足から指定版で再計算する(他の版は残す)。"""
    store = DailyBarStore(paths.daily_cache_dir)
    summaries = []
    for trade_date in list_filtering_dates(paths):
        summary = run_day(repository, trade_date, version, paths, store)
        if summary:
            summaries.append(summary)
            if progress:
                progress(trade_date)
    return summaries


def refresh_daily_cache(symbols: list[str], paths: TrendCheckPaths, now: Optional[datetime] = None) -> None:
    """既存の日足キャッシュ更新処理を対象銘柄だけに使う(確定済みの日足のみ追記される)。"""
    from src.entrypoints.update_daily_bar_cache import update_cache

    update_cache(paths.daily_cache_dir, sorted(set(symbols)), 120, now or datetime.now())


def symbols_for_date(paths: TrendCheckPaths, trade_date: str) -> list[str]:
    """キャッシュ更新の対象(選定+候補)。日足キャッシュ全銘柄の代用時は選定銘柄のみ。"""
    selected = load_selected_symbols(paths, trade_date) or []
    candidates, source, _ = load_candidates(paths, trade_date, DailyBarStore(paths.daily_cache_dir))
    return list(dict.fromkeys([*selected, *(candidates if source != UNIVERSE_DAILY_CACHE else [])]))
