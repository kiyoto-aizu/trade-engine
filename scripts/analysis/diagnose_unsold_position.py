"""保有銘柄が売却されない原因を、ローカルファイルだけで切り分ける読み取り専用の診断スクリプト。

実行方法:
    python scripts/analysis/diagnose_unsold_position.py --symbol 8944 --date 2026-10-06

結論は次の4つのいずれか(優先順位 C -> A -> B)。
    A: 実際に値動きがない / B: 値動きはあるが売却条件に未達(正常な保留)
    C: バグ・障害の兆候あり / 判断不能: データ不足

読み取り専用の約束:
    - 状態ファイル・注文履歴・ログ・分足・日足キャッシュは読み込むだけ。判断記録DBは mode=ro で開く
    - kabuステーションAPI(/token・板)は呼ばない(取引プロセスのトークンを無効化しないため)
    - 書き込むのは --output-dir(既定 data/reports/diagnose_unsold/)の diagnose_unsold_{symbol}_{date}.md / .json のみ
売却条件の判定は src.domain の関数・定数を再利用する(二重実装しない)。
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from bisect import bisect_right
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, time, timedelta
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.config import config  # noqa: E402
from src.domain.models import OrderHistoryEntry, PriceLimit, TradeSignal  # noqa: E402
from src.domain.rules import (  # noqa: E402
    check_kill_switch, is_duplicate_order, is_market_closed, is_recent_order,
)
from src.domain.volatility import (  # noqa: E402
    DailyBar, VolatilityLevel, assess_volatility, resolve_atr_exit_multiplier, stop_loss_multiplier,
)
from src.infrastructure.paper.paper_order_client import PaperOrderClient  # noqa: E402
from src.infrastructure.persistence.parquet_minute_bar_repository import ParquetMinuteBarRepository  # noqa: E402

# 「ほぼ動いていない」の目安(仮置き)。買い後の値幅(高値-安値)がATRのこの倍率未満なら値動きなしとみなす
STAGNANT_ATR_FRACTION = 0.3
# 売却条件の成立から実際の売りまでの許容遅れ(ループ周期の倍数)
SELL_DELAY_TOLERANCE_LOOPS = 3
# 取引ループの停止とみなす無音時間(ループ周期の倍数)
LOOP_STALL_LOOPS = 5

CODE_A, CODE_B, CODE_C, CODE_UNKNOWN = "A", "B", "C", "判断不能"

_RECORD_RE = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}),\d+ (\w+) (\S+): (.*)$")
_REGIME_RE = re.compile(r"MarketRegimeを取得しました: レジーム=(\w+)")
_GUARD_DUPLICATE = "当日同一シンボル・同一方向の注文が既にあります"
_GUARD_LOCK = "秒以内に発生しています。二重発注を防止します"
_GUARD_NO_HOLDING = (
    "保有株が確認できないため、売り注文を見送ります",
    "売り条件該当・未保有",
)
_TRADING_LOGGER_PREFIXES = (
    "src.application.trading_usecase", "src.infrastructure.kabu", "src.api",
    "src.infrastructure.paper", "src.infrastructure.market_data",
)

# 銘柄名(銘柄=8944)を含む行だけを数える兆候
_SYMBOL_SIGNS: dict[str, re.Pattern] = {
    "BOARD_UNAVAILABLE(板取得失敗)": re.compile(r"BOARD_UNAVAILABLE"),
    "DAILY_DATA_UNAVAILABLE(日足なし)": re.compile(r"DAILY_DATA_UNAVAILABLE"),
    "ATR_DATA_UNAVAILABLE(ATR日足不足)": re.compile(r"ATR_DATA_UNAVAILABLE"),
    "ATR_ENTRY_PRICE_UNAVAILABLE(平均取得価格欠落)": re.compile(r"ATR_ENTRY_PRICE_UNAVAILABLE|平均取得価格がありません"),
    "RSI履歴不足(売り判定が飛ばされる)": re.compile(r"INSUFFICIENT_RSI_HISTORY|RSI_DATA_UNAVAILABLE"),
    "ORDER_REJECTED(注文拒否)": re.compile(r"ORDER_REJECTED"),
    "EOD決済の未完了": re.compile(r"EOD_LIQUIDATION_UNRESOLVED|EOD_PRICE_UNAVAILABLE|持ち越し防止売り中にエラー"),
}
# 銘柄に関係なく、買った後に出ていたら取引全体の異常として数える兆候(WARNING以上のみ)
_GLOBAL_SIGNS: dict[str, re.Pattern] = {
    "HTTP 401(認証失敗)": re.compile(r"(?<![\w.])401(?![\w.])"),
    "4001009(kabu APIエラー)": re.compile(r"4001009"),
    "連続失敗": re.compile(r"連続.*失敗|失敗.*連続"),
    "キルスイッチ": re.compile(r"キルスイッチ(を発動|により)"),
    "緊急停止": re.compile(r"手動緊急停止"),
    "EOD決済の保有取得失敗": re.compile(r"EOD_LIQUIDATION_POSITIONS_UNAVAILABLE"),
}
_JOURNAL_FAILURE_REASONS = (
    "BOARD_UNAVAILABLE", "ATR_DATA_UNAVAILABLE", "ATR_ENTRY_PRICE_UNAVAILABLE",
    "INSUFFICIENT_RSI_HISTORY", "RSI_DATA_UNAVAILABLE", "ORDER_REJECTED_NONE", "ORDER_REJECTED_RESULT",
    "ORDER_REJECTED_PAPER_PRICE_MISSING", "ORDER_REJECTED_PAPER_PRICE_INVALID", "ORDER_QUANTITY_ZERO",
)


# ================================================================================
# 判定ロジック(A/B/C分類)。入出力を持たない純粋関数
# ================================================================================

@dataclass
class ClassificationInput:
    buy_found: bool
    state_available: bool
    held: bool
    average_cost: float | None
    price_points: int
    atr: float | None
    max_range_atr: float | None
    triggered_without_sell: list[str] = field(default_factory=list)
    failure_signs: list[str] = field(default_factory=list)
    high_anomaly: str | None = None
    state_inconsistency: str | None = None
    stagnant_threshold: float = STAGNANT_ATR_FRACTION


@dataclass
class Conclusion:
    code: str
    reasons: list[str]
    missing: list[str] = field(default_factory=list)


def classify(inp: ClassificationInput) -> Conclusion:
    """優先順位 C -> A -> B。材料が足りなければ判断不能。"""
    c_reasons: list[str] = []
    if inp.held and inp.state_available and not (inp.average_cost and inp.average_cost > 0):
        c_reasons.append("保有中なのに平均取得価格が欠落(None/0)しており、ATR決済が無効になります")
    if inp.high_anomaly:
        c_reasons.append(inp.high_anomaly)
    if inp.state_inconsistency:
        c_reasons.append(inp.state_inconsistency)
    c_reasons.extend(f"売却条件が成立したのに売り注文が出ていません: {item}" for item in inp.triggered_without_sell)
    c_reasons.extend(f"障害の兆候: {item}" for item in inp.failure_signs)
    if c_reasons:
        return Conclusion(CODE_C, c_reasons)

    missing = []
    if not inp.buy_found:
        missing.append("指定日の買い注文の記録")
    if inp.price_points == 0:
        missing.append("買い後の値動きデータ(分足もログの売買判定も無い)")
    if inp.atr is None or inp.atr <= 0:
        missing.append("ATR(注文記録にも日足キャッシュにも無い)")
    if missing or inp.max_range_atr is None:
        return Conclusion(CODE_UNKNOWN, ["判断に必要なデータが足りません"], missing)

    if inp.max_range_atr < inp.stagnant_threshold:
        return Conclusion(CODE_A, [
            f"買い後の値幅がATRの{inp.max_range_atr:.2f}倍で、目安({inp.stagnant_threshold}倍・仮置き)未満です",
            "障害の兆候と、成立済みの売却条件は見つかりませんでした",
        ])
    return Conclusion(CODE_B, [
        f"買い後の値幅はATRの{inp.max_range_atr:.2f}倍あり、値動きはあります",
        "障害の兆候は無く、どの売却条件にも届いていません(正常な保留)",
    ])


def friendly_summary(conclusion: Conclusion, status_line: str) -> list[str]:
    """結論を、やさしい言葉の2〜3行にする。"""
    if conclusion.code == CODE_C:
        lines = ["バグや障害の可能性があります。"] + conclusion.reasons[:2]
    elif conclusion.code == CODE_A:
        lines = ["買ってから値がほとんど動いていません。売る条件に届かず、そのままになっているだけと見られます。",
                 conclusion.reasons[0]]
    elif conclusion.code == CODE_B:
        lines = ["値は動いていますが、売る条件にはまだ届いていません。仕様どおりの保留と見られます。",
                 conclusion.reasons[1]]
    else:
        lines = ["材料が足りず、原因を判断できません。", "足りないもの: " + "、".join(conclusion.missing)]
    return [*lines, status_line][:4]


# ================================================================================
# 売却条件の経路シミュレーション(本番と同じ domain 関数を、観測価格の順に当てる)
# ================================================================================

@dataclass
class Obs:
    at: datetime
    price: float
    source: str  # "bar"(分足) / "log"(売買判定ログ)
    rsi: float | None = None
    lower_band: float | None = None
    upper_band: float | None = None
    logged_high: float | None = None
    logged_line: float | None = None
    decision: str | None = None


def exit_multipliers(level: VolatilityLevel) -> tuple[float, float]:
    """(損切り倍率, 利確倍率)。ATRレベル(直近TR/ATR比)別で、MarketRegime別ではない。"""
    stop = stop_loss_multiplier(
        level, config.ATR_STOP_NORMAL_MULTIPLIER, config.ATR_STOP_CAUTION_MULTIPLIER,
        config.ATR_STOP_DANGER_MULTIPLIER,
    )
    lock = stop_loss_multiplier(
        level, config.ATR_PROFIT_LOCK_NORMAL_MULTIPLIER, config.ATR_PROFIT_LOCK_CAUTION_MULTIPLIER,
        config.ATR_PROFIT_LOCK_DANGER_MULTIPLIER,
    )
    return stop, lock


def select_multiplier(gain: float, atr: float, level: VolatilityLevel) -> float:
    return resolve_atr_exit_multiplier(
        gain, atr, level,
        config.ATR_STOP_NORMAL_MULTIPLIER, config.ATR_STOP_CAUTION_MULTIPLIER, config.ATR_STOP_DANGER_MULTIPLIER,
        config.ATR_PROFIT_LOCK_NORMAL_MULTIPLIER, config.ATR_PROFIT_LOCK_CAUTION_MULTIPLIER,
        config.ATR_PROFIT_LOCK_DANGER_MULTIPLIER, config.ATR_PROFIT_LOCK_TRIGGER_ATR_MULTIPLE,
    )


def simulate_exit_path(
    symbol: str,
    path: list[Obs],
    *,
    entry_price: float,
    initial_high: float,
    atr: float,
    level: VolatilityLevel,
    fallback_context: tuple[float | None, float | None, float | None],
    context_obs: list[Obs] | None = None,
) -> dict:
    """trading_usecase の売り判定(高値更新 -> 倍率選択 -> TradeSignal.evaluate)を観測順に再現する。

    RSI・下側バンドは売買判定ログ(context_obs)の直近値を使い、無ければ fallback_context(買い注文の記録)を使う。
    """
    context_source = path if context_obs is None else context_obs
    log_context = [(ob.at, (ob.rsi, ob.lower_band, ob.upper_band)) for ob in context_source if ob.source == "log" and ob.rsi is not None]
    log_times = [item[0] for item in log_context]

    def context_at(at: datetime):
        index = bisect_right(log_times, at) - 1
        return log_context[index][1] if index >= 0 else fallback_context

    held_high = initial_high
    first_trigger: dict | None = None
    closest: dict | None = None
    final: dict = {}
    # 含み益なし扱いの倍率と違えば、利確倍率へ切り替わっている
    base_multiplier = select_multiplier(float("-inf"), atr, level)
    for ob in path:
        held_high = max(held_high, ob.price)
        multiplier = select_multiplier(held_high - entry_price, atr, level)
        locking = multiplier != base_multiplier
        line = held_high - atr * multiplier
        rsi, lower, upper = context_at(ob.at)
        limit = PriceLimit(lower, upper) if lower is not None and upper is not None else PriceLimit(0.0, float("inf"))
        atr_signal = TradeSignal.evaluate(
            symbol, ob.price, limit, None, config.RSI_ENTRY_THRESHOLD, config.RSI_EXIT_THRESHOLD,
            held_high, atr, multiplier,
        )
        full_signal = TradeSignal.evaluate(
            symbol, ob.price, limit, rsi, config.RSI_ENTRY_THRESHOLD, config.RSI_EXIT_THRESHOLD,
            held_high, atr, multiplier,
        )
        kind = None
        if atr_signal is not None and atr_signal.side == config.OrderSide.SELL:
            kind = "ATRトレーリング利確" if locking else "ATR損切り"
        elif full_signal is not None and full_signal.side == config.OrderSide.SELL:
            kind = "通常SELL(下側バンド+RSI)"
        if kind and first_trigger is None:
            first_trigger = {"kind": kind, "at": ob.at, "price": ob.price, "line": line}
        margin = ob.price - line
        if closest is None or margin < closest["margin"]:
            closest = {"at": ob.at, "price": ob.price, "line": line, "margin": margin}
        final = {
            "at": ob.at, "price": ob.price, "held_high": held_high, "multiplier": multiplier, "locking": locking,
            "line": line, "rsi": rsi, "lower_band": lower, "upper_band": upper,
        }
    return {"first_trigger": first_trigger, "closest": closest, "final": final}


def eod_trigger(end_ref: datetime, sold_at: datetime | None) -> dict | None:
    """持ち越し防止決済(15:20)の成立と、実際の売りまでの遅れ。"""
    if config.ALLOW_OVERNIGHT_HOLDING:
        return None
    if not is_market_closed(end_ref.time(), config.MARKET_LIQUIDATION_HOUR, config.MARKET_LIQUIDATION_MINUTE):
        return None
    deadline = datetime.combine(end_ref.date(), time(config.MARKET_LIQUIDATION_HOUR, config.MARKET_LIQUIDATION_MINUTE))
    return {"kind": "持ち越し防止決済(15:20)", "at": deadline, "delay_seconds": (end_ref - deadline).total_seconds(),
            "sold": sold_at is not None}


# ================================================================================
# ファイル読み込み(すべて読み取り専用)
# ================================================================================

@dataclass
class Paths:
    order_history: Path
    state: Path
    logs: Path
    minute_bars: Path
    daily_cache: Path
    kill_switch_baseline: Path
    decision_db: Path
    emergency_stop: Path


def default_paths() -> Paths:
    return Paths(
        order_history=PROJECT_ROOT / config.ORDER_HISTORY_FILE,
        state=PROJECT_ROOT / config.PAPER_ACCOUNT_STATE_FILE,
        logs=Path(config.LOG_DIRECTORY),
        minute_bars=Path(config.MINUTE_BAR_PARQUET_DIR),
        daily_cache=PROJECT_ROOT / "data" / "cache" / "yahoo_daily",
        kill_switch_baseline=PROJECT_ROOT / "data" / "trading" / "kill_switch_baseline.json",
        decision_db=Path(config.FILTER_DECISION_DATABASE_FILE),
        emergency_stop=Path(config.EMERGENCY_STOP_FILE),
    )


def _read_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8-sig")), None
    except FileNotFoundError:
        return None, "ファイルがありません"
    except (OSError, ValueError) as exc:
        return None, f"読み込めません({type(exc).__name__})"


def _to_float(value) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number == number and abs(number) != float("inf") else None


def load_order_entries(path: Path) -> tuple[list[OrderHistoryEntry], list[str]]:
    data, error = _read_json(path)
    if error:
        return [], [f"注文履歴: {error}"]
    entries, problems = [], []
    for index, item in enumerate(data if isinstance(data, list) else []):
        try:
            entries.append(OrderHistoryEntry.from_dict(item))
        except (KeyError, TypeError, ValueError):
            problems.append(f"注文履歴の{index}行目を解釈できません")
    return entries, problems


def read_position_state(path: Path, symbol: str) -> dict:
    data, error = _read_json(path)
    if error or not isinstance(data, dict):
        return {"available": False, "note": error or "形式が不正です"}
    holdings = data.get("holdings") or {}
    costs = data.get("average_costs") or {}
    highs = data.get("holding_high_prices")
    quantity = int(holdings.get(symbol, 0) or 0)
    return {
        "available": True, "held": quantity > 0, "quantity": quantity,
        "average_cost": _to_float(costs.get(symbol)), "average_cost_key_present": symbol in costs,
        "state_holding_high": _to_float(highs.get(symbol)) if isinstance(highs, dict) else None,
        "holding_high_in_state_file": isinstance(highs, dict),
        "realized_pnl": _to_float(data.get("realized_pnl")), "realized_pnl_date": data.get("realized_pnl_date"),
        "cash": _to_float(data.get("cash")), "file_modified": datetime.fromtimestamp(path.stat().st_mtime).isoformat(timespec="seconds"),
    }


@dataclass
class LogRecord:
    at: datetime
    level: str
    logger: str
    message: str


def read_log_records(log_dir: Path, day: str) -> tuple[list[LogRecord], list[str]]:
    """当日の行だけを返す。当日分は trade_project.log、過去日は日付付きの回転ファイルにある。"""
    records, files = [], []
    for name in ("trade_project.log", f"trade_project.log.{day}"):
        path = log_dir / name
        if not path.exists():
            continue
        files.append(name)
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            matched = _RECORD_RE.match(line)
            if matched and matched.group(1).startswith(day):
                records.append(LogRecord(datetime.strptime(matched.group(1), "%Y-%m-%d %H:%M:%S"),
                                         matched.group(2), matched.group(3), matched.group(4)))
    records.sort(key=lambda record: record.at)
    return records, files


def parse_judgment(record: LogRecord) -> tuple[str, Obs] | None:
    """「売買判定: 銘柄=.. | 現在値=.. | ...」行を観測値へ変換する。"""
    if not record.message.startswith("売買判定:"):
        return None
    fields = {}
    for part in record.message.split(":", 1)[1].split(" | "):
        if "=" in part:
            key, value = part.strip().split("=", 1)
            fields[key] = value
    price = _to_float(fields.get("現在値"))
    symbol = fields.get("銘柄")
    if price is None or not symbol:
        return None
    return symbol, Obs(
        at=record.at, price=price, source="log", rsi=_to_float(fields.get("RSI")),
        lower_band=_to_float(fields.get("決済基準")), upper_band=_to_float(fields.get("エントリー基準")),
        logged_high=_to_float(fields.get("保有中最高値")), logged_line=_to_float(fields.get("利確ライン")),
        decision=fields.get("判定"),
    )


def collect_log_facts(records: list[LogRecord], symbol: str, start: datetime | None, end: datetime | None) -> dict:
    """売買判定の観測・ガード発動・障害の兆候・ループの動きをログから集める。"""
    observations: list[Obs] = []
    all_loop_times: list[datetime] = []
    guards = {"duplicate": {}, "lock": {}, "no_holding": {}}
    last_judgment: tuple[str, str | None] | None = None
    signs: dict[str, dict] = {}
    regime = None

    def add_sign(code: str, record: LogRecord) -> None:
        item = signs.setdefault(code, {"code": code, "count": 0, "first": record.at, "last": record.at, "samples": []})
        item["count"] += 1
        item["last"] = record.at
        if len(item["samples"]) < 3:
            item["samples"].append(f"{record.at:%H:%M:%S} {record.level} {record.message[:160]}")

    for record in records:
        found = _REGIME_RE.search(record.message)
        if found and regime is None:
            regime = found.group(1)
        parsed = parse_judgment(record)
        if parsed:
            judged_symbol, obs = parsed
            last_judgment = (judged_symbol, obs.decision)
            if start is None or record.at >= start:
                if end is None or record.at <= end:
                    all_loop_times.append(record.at)
            if judged_symbol == symbol and (start is None or record.at >= start) and (end is None or record.at <= end):
                observations.append(obs)
            continue
        for key, markers in (
            ("duplicate", (_GUARD_DUPLICATE,)),
            ("lock", (_GUARD_LOCK,)),
            ("no_holding", _GUARD_NO_HOLDING),
        ):
            if any(marker in record.message for marker in markers) and last_judgment:
                bucket = guards[key].setdefault(last_judgment, [])
                bucket.append(record.at)
        if start is not None and record.at < start:
            continue
        if end is not None and record.at > end + timedelta(minutes=1):
            continue
        mentions_symbol = f"銘柄={symbol}" in record.message
        if mentions_symbol:
            for code, pattern in _SYMBOL_SIGNS.items():
                if pattern.search(record.message):
                    add_sign(code, record)
        if record.level in ("WARNING", "ERROR", "CRITICAL"):
            for code, pattern in _GLOBAL_SIGNS.items():
                if pattern.search(record.message):
                    add_sign(code, record)
            if record.level in ("ERROR", "CRITICAL") and record.logger.startswith(_TRADING_LOGGER_PREFIXES) and not mentions_symbol:
                add_sign("取引ループ関連のERROR", record)
    return {
        "observations": observations, "loop_times": all_loop_times, "guards": guards,
        "signs": list(signs.values()), "regime": regime,
    }


def read_decision_db(path: Path, symbol: str, day: str, mode: str) -> dict:
    """判断記録DBを mode=ro で開く。開けない・表が無い場合は理由だけ返す。"""
    if not path.exists():
        return {"available": False, "note": "DBファイルがありません", "records": [], "events": []}
    try:
        connection = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True, timeout=2.0)
        connection.row_factory = sqlite3.Row
        try:
            records = [dict(row) for row in connection.execute(
                "SELECT reason_code, first_occurred_at, last_occurred_at, occurrence_count, detail_json "
                "FROM decision_records WHERE decision_date=? AND symbol=? AND execution_mode=? ORDER BY first_occurred_at",
                (day, symbol, mode))]
            events = [dict(row) for row in connection.execute(
                "SELECT event_type, occurred_at, reference_price FROM filter_decision_events "
                "WHERE symbol=? AND substr(occurred_at,1,10)=? AND execution_mode=? ORDER BY occurred_at",
                (symbol, day, mode))]
        finally:
            connection.close()
        return {"available": True, "note": "", "records": records, "events": events}
    except sqlite3.Error as exc:
        return {"available": False, "note": f"読み取りに失敗({type(exc).__name__}: {exc})", "records": [], "events": []}


def load_daily_bars(cache_dir: Path, symbol: str, day: str) -> tuple[list[DailyBar], str | None, str | None]:
    """指定日より前の日足を古い順に返す(最新日, 注記)。"""
    data, error = _read_json(cache_dir / f"{symbol}.json")
    if error or not isinstance(data, dict):
        return [], None, f"日足キャッシュ: {error or '形式が不正です'}"
    bars, latest = [], None
    for key in sorted(data):
        row = data[key]
        if key >= day or not isinstance(row, dict):
            continue
        high, low, close = (_to_float(row.get(name)) for name in ("high", "low", "close"))
        if None in (high, low, close) or min(high, low, close) <= 0 or high < low:
            continue
        bars.append(DailyBar(high=high, low=low, close=close, open=_to_float(row.get("open")), volume=_to_float(row.get("volume"))))
        latest = key
    return bars, latest, None


def estimate_paper_average_cost(symbol: str, price: float, quantity: int) -> float | None:
    """ペーパー約定式(スリッページ・手数料込み)を既存クラスのメモリ上インスタンスで再現する。ファイルは触らない。"""
    client = PaperOrderClient(prices={symbol: price}, cash=price * quantity * 10 + 1_000_000.0, state_path=None)
    client.place_market_order("", symbol, config.OrderSide.BUY.value, quantity)
    return client.average_costs.get(symbol)


# ================================================================================
# 診断本体
# ================================================================================

def _fmt(value, digits: int = 1, unit: str = "") -> str:
    if value is None:
        return "不明"
    return f"{value:,.{digits}f}{unit}" if isinstance(value, (int, float)) else str(value)


def _entry_at(entry: OrderHistoryEntry) -> datetime:
    return datetime.fromisoformat(entry.timestamp)


def diagnose(symbol: str, day: date, paths: Paths, now: datetime | None = None) -> dict:
    now = now or datetime.now()
    day_text = day.isoformat()
    is_today = day == now.date()
    notes: list[str] = []

    # --- 2. 注文履歴 ---
    entries, problems = load_order_entries(paths.order_history)
    notes.extend(problems)
    same_day = [e for e in entries if e.symbol == symbol and e.timestamp.startswith(day_text)]
    buys = [e for e in same_day if e.side == config.OrderSide.BUY]
    buy = buys[-1] if buys else None
    buy_at = _entry_at(buy) if buy else None
    sells = [e for e in same_day if e.side == config.OrderSide.SELL and buy_at and _entry_at(e) >= buy_at]
    sell = sells[0] if sells else None
    sold_at = _entry_at(sell) if sell else None

    # --- 1. 保有状態 ---
    state = read_position_state(paths.state, symbol)
    if not state["available"]:
        notes.append(f"状態ファイル: {state['note']}")
    elif not is_today:
        notes.append("状態ファイルは現在の状態で、指定日当時の状態とは限りません")
    held = bool(state.get("held"))
    estimated_cost = estimate_paper_average_cost(symbol, buy.price, buy.qty) if buy else None
    state_cost = state.get("average_cost") if held else None
    entry_price = state_cost if (state_cost and state_cost > 0) else estimated_cost
    entry_source = "状態ファイルの平均取得価格" if (state_cost and state_cost > 0) else "買い記録からの推定(ペーパー約定式を再利用)"

    # --- 5. ログ ---
    records, log_files = read_log_records(paths.logs, day_text)
    if not log_files:
        notes.append("当日のログファイルがありません")
    end_ref = sold_at or (now if is_today else datetime.combine(day, time(config.MARKET_CLOSE_HOUR, config.MARKET_CLOSE_MINUTE)))
    # ログの時刻は秒精度なので、買い注文と同じ秒のログも対象にする
    log_start = buy_at.replace(microsecond=0) if buy_at else None
    facts = collect_log_facts(records, symbol, log_start, sold_at)

    # --- 3. 値動き ---
    bars = []
    if buy_at:
        try:
            bars = ParquetMinuteBarRepository(paths.minute_bars).load_bars(day, symbol)
        except (RuntimeError, OSError, ValueError) as exc:
            notes.append(f"分足を読み込めません({type(exc).__name__})")
    bar_obs = [Obs(datetime.fromisoformat(bar.time), float(bar.price), "bar") for bar in bars
               if buy_at and datetime.fromisoformat(bar.time) >= buy_at and (sold_at is None or datetime.fromisoformat(bar.time) <= sold_at)]
    log_obs = facts["observations"]
    path = bar_obs or log_obs
    path_source = "分足" if bar_obs else ("売買判定ログ(INFO出力分のみ)" if log_obs else "なし")
    if not bars:
        notes.append("分足なし(指定日のParquetがありません)" if buy_at else "分足は未確認(買い記録なし)")

    # ATR: 本番が注文時に記録した値を優先し、日足キャッシュの再計算を照合に使う
    daily_bars, latest_daily, daily_note = load_daily_bars(paths.daily_cache, symbol, day_text)
    if daily_note:
        notes.append(daily_note)
    cache_assessment = assess_volatility(daily_bars, config.ATR_PERIOD, config.ATR_CAUTION_RATIO, config.ATR_DANGER_RATIO) if daily_bars else None
    recorded_atr = buy.atr if buy and buy.atr and buy.atr > 0 else None
    atr = recorded_atr or (cache_assessment.atr if cache_assessment else None)
    atr_source = "買い注文の記録(本番の使用値)" if recorded_atr else ("日足キャッシュから再計算" if cache_assessment else "なし")
    level_text = buy.atr_level if buy and buy.atr_level else (cache_assessment.level.value if cache_assessment else None)
    try:
        level = VolatilityLevel(level_text) if level_text else None
    except ValueError:
        level, level_text = None, None
        notes.append("ATRレベルの記録値を解釈できません")
    if recorded_atr and cache_assessment and abs(recorded_atr - cache_assessment.atr) > recorded_atr * 0.05:
        notes.append(
            f"ATRの記録値({recorded_atr:.3f})とキャッシュ再計算値({cache_assessment.atr:.3f})が5%超ずれています"
            f"(日足キャッシュの最新日={latest_daily}。本番は取得時点のYahoo日足を使うため差が出ます)")

    buy_price = buy.price if buy else None
    prices = [ob.price for ob in path]
    price_summary = None
    if prices and buy_price:
        high, low = max(prices), min(prices)
        price_summary = {
            "open": prices[0], "high": high, "low": low, "last": prices[-1], "first_at": path[0].at, "last_at": path[-1].at,
            "points": len(prices), "max_rise": max(high - buy_price, 0.0), "max_fall": max(buy_price - low, 0.0),
            "range": max(high, buy_price) - min(low, buy_price),
        }
        if atr:
            price_summary.update(
                max_rise_atr=price_summary["max_rise"] / atr, max_fall_atr=price_summary["max_fall"] / atr,
                range_atr=price_summary["range"] / atr)

    # --- 4. 売却条件 ---
    sim = None
    if path and atr and level and entry_price and buy_price:
        sim = simulate_exit_path(
            symbol, path, entry_price=entry_price, initial_high=buy_price, atr=atr, level=level,
            fallback_context=(buy.rsi, buy.basis_lower_band, buy.basis_upper_band), context_obs=log_obs)
    eod = eod_trigger(end_ref, sold_at) if buy else None
    stop_multiplier, lock_multiplier = exit_multipliers(level) if level else (None, None)
    final = sim["final"] if sim else {}
    held_high = final.get("held_high")
    stop_line = held_high - atr * stop_multiplier if (held_high is not None and atr and stop_multiplier) else None
    lock_line = held_high - atr * lock_multiplier if (held_high is not None and atr and lock_multiplier) else None
    last_price = final.get("price")
    locking = final.get("locking")
    conditions = [
        {"name": "ATR損切り", "formula": "現在値 <= 保有中高値 - ATR × 損切り倍率(含み益がATR×%.1f未満のとき適用)" % config.ATR_PROFIT_LOCK_TRIGGER_ATR_MULTIPLE,
         "threshold": stop_line, "current": last_price, "applies": None if locking is None else not locking,
         "met": None if stop_line is None else last_price <= stop_line and not locking,
         "extra": f"最安値={_fmt(price_summary['low']) if price_summary else '不明'} / 倍率={_fmt(stop_multiplier, 2)}"},
        {"name": "ATRトレーリング利確", "formula": "現在値 <= 保有中高値 - ATR × 利確倍率(高値-平均取得価格 >= ATR×%.1f のとき適用)" % config.ATR_PROFIT_LOCK_TRIGGER_ATR_MULTIPLE,
         "threshold": lock_line, "current": last_price, "applies": locking,
         "met": None if lock_line is None else last_price <= lock_line and bool(locking),
         "extra": f"保有中高値={_fmt(held_high)} / 倍率={_fmt(lock_multiplier, 2)} / 含み益(高値-取得価格)={_fmt(held_high - entry_price) if (held_high is not None and entry_price) else '不明'}"},
        {"name": "通常SELL(下側バンド+RSI)", "formula": "現在値 <= 決済基準(下側バンド) かつ RSI <= %.1f" % config.RSI_EXIT_THRESHOLD,
         "threshold": final.get("lower_band"), "current": last_price, "applies": None,
         "met": None if final.get("lower_band") is None else bool(last_price <= final["lower_band"] and final.get("rsi") is not None and final["rsi"] <= config.RSI_EXIT_THRESHOLD),
         "extra": f"RSI={_fmt(final.get('rsi'))}"},
        {"name": "持ち越し防止決済", "formula": "時刻 >= %02d:%02d(ALLOW_OVERNIGHT_HOLDING=%s)" % (config.MARKET_LIQUIDATION_HOUR, config.MARKET_LIQUIDATION_MINUTE, config.ALLOW_OVERNIGHT_HOLDING),
         "threshold": f"{config.MARKET_LIQUIDATION_HOUR:02d}:{config.MARKET_LIQUIDATION_MINUTE:02d}", "current": f"{end_ref:%H:%M:%S}", "applies": not config.ALLOW_OVERNIGHT_HOLDING,
         "met": eod is not None, "extra": _eod_remaining_text(end_ref)},
    ]

    # ガード(売りを止める要因)
    history_before = [e for e in entries if sold_at is None or _entry_at(e) < sold_at]
    sell_signal = TradeSignal(symbol, config.OrderSide.SELL, last_price or 0.0, buy.qty if buy else 0)
    guard_ref = sold_at or end_ref
    duplicate_sell = is_duplicate_order(sell_signal, history_before, guard_ref) if buy else None
    recent_sell = is_recent_order(sell_signal, history_before, config.ORDER_LOCK_SECONDS, guard_ref) if buy else None
    guard_hits = {key: {f"{s}/{side}": len(times) for (s, side), times in value.items() if s == symbol} for key, value in facts["guards"].items()}
    sell_guard_hits = sum(len(times) for key in ("duplicate", "lock", "no_holding") for (s, side), times in facts["guards"][key].items() if s == symbol and side == "SELL")
    baseline, _ = _read_json(paths.kill_switch_baseline)
    capital = _to_float(baseline.get("capital")) if isinstance(baseline, dict) and baseline.get("date") == day_text else None
    realized = state.get("realized_pnl") if state.get("realized_pnl_date") == day_text else None
    buy_count = sum(1 for e in entries if e.side == config.OrderSide.BUY and e.timestamp.startswith(day_text))
    kill_ok = check_kill_switch(buy_count, realized or 0.0, capital, config) if capital else None
    emergency_file = paths.emergency_stop.exists()
    guards = {
        "duplicate_sell_today": duplicate_sell, "recent_sell_lock": recent_sell, "order_lock_seconds": config.ORDER_LOCK_SECONDS,
        "log_guard_hits_for_symbol": guard_hits, "log_sell_guard_hits": sell_guard_hits,
        "kill_switch_check_ok": kill_ok, "kill_switch_buy_count": buy_count, "kill_switch_capital": capital,
        "kill_switch_note": "実現損益(状態ファイル)のみで計算した参考値。含み損益は含まない",
        "emergency_stop_file_exists": emergency_file,
    }

    # --- DB ---
    db = read_decision_db(paths.decision_db, symbol, day_text, config.TRADING_MODE)
    if not db["available"]:
        notes.append(f"判断記録DB: {db['note']}")

    # --- 障害の兆候(C) ---
    failure_signs = [f"{item['code']} が{item['count']}回(初回 {item['first']:%H:%M:%S})" for item in facts["signs"]]
    for record in db["records"]:
        if record["reason_code"] in _JOURNAL_FAILURE_REASONS and buy_at and record["last_occurred_at"] >= buy_at.isoformat(timespec="seconds"):
            failure_signs.append(f"判断記録DBに {record['reason_code']} が{record['occurrence_count']}回")
        if record["reason_code"] == "ORDER_SAFETY_BLOCKED" and '"SELL"' in (record["detail_json"] or ""):
            failure_signs.append(f"判断記録DBに売りの ORDER_SAFETY_BLOCKED が{record['occurrence_count']}回")
    if sell_guard_hits and not sell:
        failure_signs.append(f"売り判定がガードで止められたログが{sell_guard_hits}回")
    if any(event["event_type"] == "ATR_STOP_EXIT" for event in db["events"]):
        notes.append("判断記録DBに ATR_STOP_EXIT(ATR損切り売却)の記録があります")

    # ループの動き
    loop_times = facts["loop_times"]
    last_loop_at = loop_times[-1] if loop_times else None
    if buy and not sell and not held_is_closed(held, state) and last_loop_at and (end_ref - last_loop_at).total_seconds() > LOOP_STALL_LOOPS * config.LOOP_INTERVAL \
            and end_ref.time() < time(config.MARKET_CLOSE_HOUR, config.MARKET_CLOSE_MINUTE):
        failure_signs.append(f"売り判定ループが止まった形跡(最後の売買判定ログ {last_loop_at:%H:%M:%S})")

    # 高値の異常: 記録された保有中最高値が観測価格の最高値に追いついていない
    high_anomaly = None
    logged_highs = [ob.logged_high for ob in log_obs if ob.logged_high is not None]
    if logged_highs and prices and max(prices) > max(logged_highs) + 1e-9 and path_source == "分足":
        high_anomaly = f"保有中最高値のログ({max(logged_highs):.1f})が観測した最高値({max(prices):.1f})に追いついていません(高値更新停止の疑い)"
    if state.get("state_holding_high") is not None and prices and max(prices) > state["state_holding_high"] + 1e-9:
        high_anomaly = f"状態ファイルの高値({state['state_holding_high']:.1f})が観測した最高値({max(prices):.1f})より低い"

    # 売却条件の成立と売り注文のずれ
    triggered_without_sell: list[str] = []
    tolerance = SELL_DELAY_TOLERANCE_LOOPS * config.LOOP_INTERVAL
    triggers = [t for t in ((sim or {}).get("first_trigger"),) if t]
    for trigger in triggers:
        if not sold_at or (sold_at - trigger["at"]).total_seconds() > tolerance:
            triggered_without_sell.append(f"{trigger['kind']}(成立 {trigger['at']:%H:%M:%S} 価格{trigger['price']:.1f} / ライン{trigger['line']:.1f})")
    if eod and eod["delay_seconds"] > tolerance and (not sold_at or (sold_at - eod["at"]).total_seconds() > tolerance):
        triggered_without_sell.append(f"{eod['kind']}(15:20から{eod['delay_seconds']:.0f}秒経過)")

    state_inconsistency = None
    if is_today and buy and state["available"] and not state.get("held") and not sell:
        state_inconsistency = "状態ファイルに保有が無いのに、当日の売り注文が履歴にありません"
    if is_today and sell and state["available"] and state.get("held") and state.get("quantity", 0) >= buy.qty:
        state_inconsistency = "売り注文が履歴にあるのに、状態ファイルでは保有が残っています"

    conclusion = classify(ClassificationInput(
        buy_found=buy is not None, state_available=state["available"] and is_today and not sold_at, held=held and not sold_at,
        average_cost=state_cost, price_points=len(prices), atr=atr,
        max_range_atr=price_summary.get("range_atr") if price_summary else None,
        triggered_without_sell=triggered_without_sell, failure_signs=failure_signs,
        high_anomaly=high_anomaly, state_inconsistency=state_inconsistency))

    status_line = _status_line(buy, sell, held, state)
    last_board = log_obs[-1].at if log_obs else None
    board_gap = (end_ref - last_board).total_seconds() if last_board else None
    return {
        "symbol": symbol, "date": day_text, "generated_at": now.isoformat(timespec="seconds"),
        "conclusion": asdict(conclusion), "summary_lines": friendly_summary(conclusion, status_line), "status_line": status_line,
        "notes": notes, "state": state,
        "entry": {"price": entry_price, "source": entry_source, "state_average_cost": state_cost, "estimated": estimated_cost, "initial_high": buy_price},
        "orders": {"buys": [e.to_dict() for e in buys], "sells": [e.to_dict() for e in sells]},
        "atr": {"value": atr, "source": atr_source, "level": level_text,
                "recorded": recorded_atr, "cache_recomputed": cache_assessment.atr if cache_assessment else None, "cache_latest_day": latest_daily},
        "regime": (buy.market_regime if buy and buy.market_regime else None) or facts["regime"] or "不明",
        "price": {"source": path_source, "summary": price_summary, "minute_bar_count": len(bars), "log_observation_count": len(log_obs)},
        "conditions": conditions, "guards": guards, "simulation": sim,
        "board": {"last_observed_at": last_board, "gap_seconds_to_end": board_gap, "last_loop_at": last_loop_at,
                  "loop_log_count": len(loop_times), "end_reference": end_ref},
        "log": {"files": log_files, "record_count": len(records), "signs": facts["signs"]},
        "decision_db": db, "stagnant_threshold": STAGNANT_ATR_FRACTION,
        "log_excerpts": _log_excerpts(records, symbol, buy_at, sold_at),
    }


def held_is_closed(held: bool, state: dict) -> bool:
    return state.get("available") and not held


def _eod_remaining_text(end_ref: datetime) -> str:
    deadline = datetime.combine(end_ref.date(), time(config.MARKET_LIQUIDATION_HOUR, config.MARKET_LIQUIDATION_MINUTE))
    seconds = (deadline - end_ref).total_seconds()
    return f"15:20まで残り{seconds / 60:.1f}分" if seconds > 0 else f"15:20を{-seconds / 60:.1f}分過ぎている"


def _status_line(buy, sell, held: bool, state: dict) -> str:
    if buy is None:
        return "状態: 指定日の買い注文が履歴にありません"
    if sell is not None:
        reason = sell.decision_reason or "理由の記録なし"
        return f"状態: 売却済み({sell.timestamp[11:19]} {sell.price:.1f}円 / 理由={reason})"
    if state.get("available") and held:
        return f"状態: 保有中({state['quantity']}株)で、売り注文は履歴にありません"
    return "状態: 売り注文は履歴にありません(保有状態は不明)"


def _log_excerpts(records: list[LogRecord], symbol: str, buy_at, sold_at, limit: int = 12) -> list[str]:
    picked = [r for r in records if f"銘柄={symbol}" in r.message and (buy_at is None or r.at >= buy_at - timedelta(minutes=1))]
    if not picked:
        return []
    selected = picked if len(picked) <= limit else picked[:4] + picked[-(limit - 4):]
    return [f"{r.at:%H:%M:%S} {r.level} {r.message[:200]}" for r in selected]


# ================================================================================
# レポート出力
# ================================================================================

def _yes(value: bool | None) -> str:
    return "不明" if value is None else ("成立" if value else "未成立")


def render_markdown(result: dict) -> str:
    conclusion = result["conclusion"]
    label = {CODE_A: "A: 実際に値動きがない", CODE_B: "B: 値動きはあるが売却条件に未達(正常な保留)",
             CODE_C: "C: バグ・障害の兆候あり", CODE_UNKNOWN: "判断不能: データ不足"}[conclusion["code"]]
    lines = [f"# 未売却の診断: 銘柄{result['symbol']} / {result['date']}", "",
             f"生成: {result['generated_at']}(読み取り専用の診断。API呼び出しなし)", "",
             f"## 結論: {label}", ""]
    lines += [f"- {text}" for text in result["summary_lines"]]
    lines += ["", "## 売却条件の実装(条件式と参照する値)", "",
              "売り判定は取引ループ(`src/application/trading_usecase.py` の `run()`)が約60秒ごとに銘柄ごとに行う。",
              "", "| 条件 | 条件式 | 参照する値・定数 | 実装 |", "|---|---|---|---|",
              f"| ATR損切り | 現在値 <= 保有中高値 - ATR × 損切り倍率 | 損切り倍率(ATRレベル別) NORMAL={config.ATR_STOP_NORMAL_MULTIPLIER} / CAUTION={config.ATR_STOP_CAUTION_MULTIPLIER} / DANGER={config.ATR_STOP_DANGER_MULTIPLIER}。ATR期間={config.ATR_PERIOD}(TRの単純平均) | `TradeSignal.evaluate`(models.py)、`resolve_atr_exit_multiplier`(volatility.py) |",
              f"| ATRトレーリング利確 | 同じ式。ただし(保有中高値 - 平均取得価格) >= ATR × {config.ATR_PROFIT_LOCK_TRIGGER_ATR_MULTIPLE} のとき利確倍率に切り替わる | 利確倍率 NORMAL={config.ATR_PROFIT_LOCK_NORMAL_MULTIPLIER} / CAUTION={config.ATR_PROFIT_LOCK_CAUTION_MULTIPLIER} / DANGER={config.ATR_PROFIT_LOCK_DANGER_MULTIPLIER}。ATRレベル(直近TR/ATR比: 警戒>={config.ATR_CAUTION_RATIO}, 危険>={config.ATR_DANGER_RATIO})別で、MarketRegime別ではない | 同上 |",
              f"| 通常SELL | 現在値 <= 下側バンド(決済基準) かつ RSI <= {config.RSI_EXIT_THRESHOLD} | 下側バンド=SMA5×0.99、RSI期間={config.RSI_PERIOD} | `TradeSignal.evaluate` |",
              f"| 持ち越し防止決済 | 時刻 >= {config.MARKET_LIQUIDATION_HOUR:02d}:{config.MARKET_LIQUIDATION_MINUTE:02d} で全保有を成行売り(15:30以降は遅延清算) | ALLOW_OVERNIGHT_HOLDING={config.ALLOW_OVERNIGHT_HOLDING} | `is_market_closed`(rules.py)、`_liquidate_all_positions` |",
              f"| 時間ロック | 同一銘柄・同一方向の注文が{config.ORDER_LOCK_SECONDS}秒以内にあれば発注しない | ORDER_LOCK_SECONDS | `is_recent_order` / `is_safe_to_order` |",
              "| 同日重複ガード | 同一銘柄・同一方向の注文が当日すでにあれば発注しない(2回目の売りも止まる) | 注文履歴 | `is_duplicate_order` |",
              f"| キルスイッチ | 当日の買い件数>={config.MAX_ORDER_COUNT_PER_DAY} または 日次損益<=-資本×{config.DAILY_LOSS_LIMIT_RATIO} で、ループ全体を停止 | 資本=kill_switch_baseline.json | `check_kill_switch` |",
              "", "- ATRの前提: 保有の平均取得価格が取れないとATR決済は丸ごと無効になる(`ATR_ENTRY_PRICE_UNAVAILABLE`)。",
              "- 保有中高値は取引プロセスのメモリ上にだけあり、状態ファイルには保存されない(再起動すると買値に戻る)。",
              "- 売り判定のログ(売買判定)は、シグナルがある時と各銘柄の初回だけINFO。シグナルなしはDEBUGなのでログに残らない。", ""]

    summary = result["price"]["summary"]
    lines += ["## 売却条件の照合", ""]
    lines += ["| 条件 | しきい値 | 現在値 | 差(現在値-しきい値) | 適用中 | 判定 | 補足 |", "|---|---|---|---|---|---|---|"]
    for item in result["conditions"]:
        threshold, current = item["threshold"], item["current"]
        gap = current - threshold if isinstance(threshold, (int, float)) and isinstance(current, (int, float)) else None
        applies = "-" if item["applies"] is None else ("はい" if item["applies"] else "いいえ")
        lines.append(f"| {item['name']} | {_fmt(threshold)} | {_fmt(current)} | {_fmt(gap)} | {applies} | {_yes(item['met'])} | {item['extra']} |")
    guards = result["guards"]
    lines += ["", "売りを止める要因:", "",
              f"- 同日重複ガード(同日にSELL済みか): {_yes_blocked(guards['duplicate_sell_today'])}",
              f"- 時間ロック({guards['order_lock_seconds']}秒以内にSELL): {_yes_blocked(guards['recent_sell_lock'])}",
              f"- ログ上の売りガード発動(この銘柄・SELL): {guards['log_sell_guard_hits']}回 / 全ガード内訳: {guards['log_guard_hits_for_symbol']}",
              f"- キルスイッチ(参考): {'上限内' if guards['kill_switch_check_ok'] else ('発動水準' if guards['kill_switch_check_ok'] is False else '不明')} / 当日買い件数={guards['kill_switch_buy_count']} / {guards['kill_switch_note']}",
              f"- 緊急停止ファイル: {'あり' if guards['emergency_stop_file_exists'] else 'なし'}", ""]
    sim = result["simulation"]
    if sim and sim["closest"]:
        closest = sim["closest"]
        lines.append(f"- 損切り/利確ラインに最も近づいた時点: {closest['at']:%H:%M:%S} 価格{closest['price']:.1f} / ライン{closest['line']:.1f}(余裕{closest['margin']:.1f}円)")
        lines.append(f"- 観測期間中に売却条件が成立した時点: {_trigger_text(sim['first_trigger'])}")
    lines.append("")

    atr = result["atr"]
    lines += ["## 根拠データ", "", "### 1. 保有状態(状態ファイル)", ""]
    state = result["state"]
    if state["available"]:
        lines += [f"- 保有数量: {state['quantity']}株(保有={'あり' if state['held'] else 'なし'}) / ファイル更新: {state['file_modified']}",
                  f"- 平均取得価格(状態ファイル): {_fmt(state['average_cost'], 2) if state['held'] else '保有なしのため対象外'}"
                  + ("" if not state["held"] or (state["average_cost"] and state["average_cost"] > 0) else "  **欠落・0・None**"),
                  f"- 保有中高値: 状態ファイルに{'あり' if state['holding_high_in_state_file'] else '保存されない(メモリのみ)'}。ログ上の最大={_fmt(_logged_high(result))}",
                  f"- 評価に使った取得価格: {_fmt(result['entry']['price'], 2)}({result['entry']['source']})",
                  f"- 実現損益: {_fmt(state['realized_pnl'], 1)}円({state['realized_pnl_date']})"]
    else:
        lines.append(f"- 不明(状態ファイル: {state['note']})")

    lines += ["", "### 2. 注文履歴", ""]
    for order in result["orders"]["buys"] + result["orders"]["sells"]:
        lines.append(f"- {order['timestamp']} {'BUY' if order['side'] == config.OrderSide.BUY.value else 'SELL'} {order['qty']}株 {order['price']}円 "
                     f"注文ID={order['order_id']} 結果={order['result_code']} 理由={order['decision_reason']}")
    if not result["orders"]["sells"]:
        lines.append("- 同日のSELL注文: 履歴に記録なし(注文履歴には受付成功分だけが載る。拒否はログ・判断記録DBで確認)")
    records = result["decision_db"]["records"]
    lines.append("- 判断記録DB(読み取り専用): " + ("、".join(f"{r['reason_code']}×{r['occurrence_count']}" for r in records) if records else ("記録なし" if result["decision_db"]["available"] else f"読めません({result['decision_db']['note']})")))
    events = result["decision_db"]["events"]
    if events:
        lines.append("- 判定イベント: " + "、".join(f"{e['event_type']}@{str(e['occurred_at'])[11:19]}" for e in events))

    lines += ["", "### 3. 値動き", "", f"- 値動きの出典: {result['price']['source']}(分足{result['price']['minute_bar_count']}本 / 売買判定ログ{result['price']['log_observation_count']}件)"]
    if summary:
        lines += [f"- 買い後の始値={_fmt(summary['open'])} 高値={_fmt(summary['high'])} 安値={_fmt(summary['low'])} 最新値={_fmt(summary['last'])}"
                  f"({summary['first_at']:%H:%M:%S}〜{summary['last_at']:%H:%M:%S}, {summary['points']}点)"]
        if "range_atr" in summary:
            lines += [f"- 買値{_fmt(result['entry']['initial_high'])}からの最大上昇={_fmt(summary['max_rise'])}円(ATRの{summary['max_rise_atr']:.2f}倍) / 最大下落={_fmt(summary['max_fall'])}円(ATRの{summary['max_fall_atr']:.2f}倍)",
                      f"- 値幅(高値-安値)=ATRの{summary['range_atr']:.2f}倍。「ほぼ動いていない」の目安=ATRの{result['stagnant_threshold']}倍未満(**仮置き**)"]
    else:
        lines.append("- 値動きデータなし(分足なし、売買判定ログもなし)")
    lines.append(f"- ATR={_fmt(atr['value'], 3)}({atr['source']}) / ATRレベル={atr['level'] or '不明'} / 日足キャッシュ再計算={_fmt(atr['cache_recomputed'], 3)}(最新日={atr['cache_latest_day']}) / MarketRegime={result['regime']}")

    board = result["board"]
    lines += ["", "### 5. 障害・バグの兆候(ログ)", "",
              f"- ログ: {', '.join(result['log']['files']) or 'なし'} / 当日{result['log']['record_count']}行 / 売買判定ログ(全銘柄)={board['loop_log_count']}件",
              f"- {result['symbol']}の板の最終観測(売買判定ログ): {_fmt(board['last_observed_at'])} / 比較時刻{board['end_reference']:%H:%M:%S}までの空白={_fmt(board['gap_seconds_to_end'], 0, '秒')}"
              "(シグナルなしのループはログに出ないため、空白は参考値)",
              f"- 売り判定ループの形跡: 最後の売買判定ログ={_fmt(board['last_loop_at'])}"]
    if result["log"]["signs"]:
        for item in result["log"]["signs"]:
            lines.append(f"- 兆候 **{item['code']}**: {item['count']}回(初回 {item['first']:%H:%M:%S} / 最終 {item['last']:%H:%M:%S})")
            lines += [f"    - `{sample}`" for sample in item["samples"]]
    else:
        lines.append("- ログに障害の兆候(401, 4001009, 連続失敗, DAILY_DATA_UNAVAILABLE, ATR不足, ORDER_REJECTED, 平均取得価格欠落など)は見つかりませんでした")
    if result["log_excerpts"]:
        lines += ["", "ログ抜粋:", "", "```"] + result["log_excerpts"] + ["```"]
    if result["notes"]:
        lines += ["", "### 注意・不明点", ""] + [f"- {note}" for note in result["notes"]]
    lines += ["", "### 事実と推測の区別", "",
              "- 事実: 状態ファイル・注文履歴・ログ・分足・日足キャッシュの値。",
              "- 推測: 取得価格(状態ファイルが無い場合)、分足が無い場合の値動き(売買判定ログから補完)、ガード発動の銘柄・方向(直前の売買判定ログから推定)。", ""]
    return "\n".join(lines)


def _yes_blocked(value: bool | None) -> str:
    return "不明" if value is None else ("止まる状況" if value else "止まらない")


def _trigger_text(trigger: dict | None) -> str:
    return "なし" if not trigger else f"{trigger['kind']}({trigger['at']:%H:%M:%S} 価格{trigger['price']:.1f} / ライン{trigger['line']:.1f})"


def _logged_high(result: dict) -> float | None:
    final = (result["simulation"] or {}).get("final") or {}
    return final.get("held_high")


def write_reports(result: dict, output_dir: Path) -> tuple[Path, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    stem = f"diagnose_unsold_{result['symbol']}_{result['date']}"
    markdown_path, json_path = output_dir / f"{stem}.md", output_dir / f"{stem}.json"
    markdown_path.write_text(render_markdown(result), encoding="utf-8")
    json_path.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    return markdown_path, json_path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="保有銘柄が売却されない原因を切り分ける(読み取り専用)")
    parser.add_argument("--symbol", required=True, help="銘柄コード(例: 8944)")
    parser.add_argument("--date", type=date.fromisoformat, default=None, help="対象日 YYYY-MM-DD(省略時は今日)")
    parser.add_argument(
        "--output-dir", type=Path, default=PROJECT_ROOT / "data" / "reports" / "diagnose_unsold",
        help="レポートの出力先(既定: data/reports/diagnose_unsold/)",
    )
    args = parser.parse_args(argv)
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    result = diagnose(args.symbol, args.date or date.today(), default_paths())
    print(render_markdown(result))
    markdown_path, json_path = write_reports(result, args.output_dir)
    print(f"\n出力: {markdown_path}\n出力: {json_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
