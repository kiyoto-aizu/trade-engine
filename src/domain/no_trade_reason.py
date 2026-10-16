"""売買0件の日の理由を1つに分類する純粋関数。記録専用で、売買判定には使わない。"""
from __future__ import annotations

import re
from collections import Counter
from typing import Iterable, Mapping

NO_CANDIDATE = "候補なし"
CONDITION_NOT_MET = "買い条件に届かず"
MARKET_STATE_SKIP = "市場状態による見送り"
CAPACITY_OR_FUNDS = "枠・資金不足"
TRADING_HALTED = "取引停止"
MARKET_ASSESSMENT_UNAVAILABLE = "市場判定不可"
OTHER = "その他"

NO_TRADE_REASONS = (
    NO_CANDIDATE,
    CONDITION_NOT_MET,
    MARKET_STATE_SKIP,
    MARKET_ASSESSMENT_UNAVAILABLE,
    CAPACITY_OR_FUNDS,
    TRADING_HALTED,
    OTHER,
)

_HALT_CODES = frozenset({"NEW_BUY_HALTED_STATE_SAVE_FAILURE"})
_MARKET_CODES = frozenset({"MARKET_REGIME_DANGER_SKIP"})
_DATA_CODES = frozenset({
    "INSUFFICIENT_RSI_HISTORY",
    "RSI_DATA_UNAVAILABLE",
    "BOARD_UNAVAILABLE",
    "ATR_DATA_UNAVAILABLE",
    "ATR_ENTRY_PRICE_UNAVAILABLE",
})
_CAPACITY_CODES = frozenset({
    "POSITION_LIMIT_REACHED",
    "WALLET_UNKNOWN",
    "ORDER_AMOUNT_LIMIT_EXCEEDED",
    "budget_below_one_lot",
    "buy_quantity_invalid_inputs",
    "caution_rounding_to_zero",
    "atr_quantity_adjustment_zero",
    "ORDER_QUANTITY_ZERO",
})
# 買い判定に影響しない情報コード。理由の分類では「想定外」として扱わない
_INFO_CODES = frozenset({"SELL_SIGNAL_WITHOUT_HOLDINGS"})

# 通知に出す日本語ラベル。ここに無いコードは通知へ出さない
CODE_LABELS = {
    "SELL_SIGNAL_WITHOUT_HOLDINGS": "売りシグナルだが未保有あり",
    "MARKET_REGIME_DANGER_SKIP": "市場状態の危険判定による見送り",
    "NEW_BUY_HALTED_STATE_SAVE_FAILURE": "状態保存失敗による新規買い停止",
    "POSITION_LIMIT_REACHED": "保有枠の上限",
    "WALLET_UNKNOWN": "買付余力が不明",
    "ORDER_AMOUNT_LIMIT_EXCEEDED": "注文金額の上限超過",
    "budget_below_one_lot": "1単元の予算に満たない",
    "buy_quantity_invalid_inputs": "買付数量の計算入力が不正",
    "caution_rounding_to_zero": "注意レジームの数量調整で0株",
    "atr_quantity_adjustment_zero": "ATR数量調整で0株",
    "ORDER_QUANTITY_ZERO": "注文数量が0",
    "INSUFFICIENT_RSI_HISTORY": "RSI算出の履歴不足",
    "RSI_DATA_UNAVAILABLE": "RSIデータなし",
    "BOARD_UNAVAILABLE": "板情報なし",
    "ATR_DATA_UNAVAILABLE": "ATRデータなし",
    "ATR_ENTRY_PRICE_UNAVAILABLE": "ATR用の価格なし",
}
_CODE_TOKEN = re.compile(r"^[A-Za-z0-9_]+$")
_DETAIL_SEPARATOR = "・"


def summarize_market_failure(failure_reason: str | None) -> str:
    """市場判定の失敗理由を短い日本語にする。例外文言などの生の内容は出さない。"""
    text = str(failure_reason or "")
    if "日経225日足" in text:
        return "日経225日足の更新不備"
    if "VIX日足" in text:
        return "VIX日足の更新不備"
    if "日経225データが不足" in text:
        return "日経225データ不足"
    if "データ取得または計算エラー" in text:
        return "市場判定データの取得・計算エラー"
    return "市場判定データの取得不可"


def classify_no_trade_reason(
    *,
    order_count: int,
    kill_switch_triggered: bool,
    emergency_stop_triggered: bool,
    candidate_count: int,
    evaluated_count: int,
    buy_signal_count: int,
    journal_reason_codes: Iterable[str],
    market_regime_skip_count: int,
    atr_danger_skip_count: int,
    market_assessment_status: str | None = None,
    market_failure_reason: str | None = None,
) -> dict | None:
    """約定が1件でもあればNone。なければ {"reason": 候補名, "detail": 補足} を返す。"""
    if order_count > 0:
        return None
    codes = Counter(journal_reason_codes)

    if kill_switch_triggered or emergency_stop_triggered or codes.keys() & _HALT_CODES:
        detail = (
            "手動緊急停止" if emergency_stop_triggered
            else "キルスイッチ" if kill_switch_triggered
            else "状態保存失敗による新規買い停止"
        )
        return {"reason": TRADING_HALTED, "detail": detail}
    if candidate_count <= 0:
        return {"reason": NO_CANDIDATE, "detail": "監視銘柄なし"}
    if market_regime_skip_count > 0 or codes.keys() & _MARKET_CODES:
        count = max(market_regime_skip_count, sum(codes[code] for code in _MARKET_CODES))
        if market_assessment_status == "unavailable":
            return {
                "reason": MARKET_ASSESSMENT_UNAVAILABLE,
                "detail": summarize_market_failure(market_failure_reason),
            }
        return {"reason": MARKET_STATE_SKIP, "detail": f"市場状態による見送りの記録 {count}件"}
    capacity = sorted(codes.keys() & _CAPACITY_CODES)
    if capacity:
        return {"reason": CAPACITY_OR_FUNDS, "detail": "・".join(capacity)}
    if atr_danger_skip_count > 0:
        return {"reason": OTHER, "detail": f"ATR危険度見送り {atr_danger_skip_count}件"}
    other_codes = sorted(codes.keys() - _DATA_CODES - _INFO_CODES)
    if other_codes:
        return {"reason": OTHER, "detail": "・".join(other_codes)}
    if evaluated_count > 0 and buy_signal_count == 0:
        info = [CODE_LABELS[code] for code in sorted(codes.keys() & _INFO_CODES)]
        return {
            "reason": CONDITION_NOT_MET,
            "detail": _DETAIL_SEPARATOR.join(info) or f"評価{evaluated_count}銘柄で買いシグナルなし",
        }
    if evaluated_count <= 0:
        detail = "・".join(sorted(codes)) or "買い条件の評価記録なし"
        return {"reason": OTHER, "detail": detail}
    return {"reason": OTHER, "detail": "買いシグナルありだが見送り・注文の記録なし"}


def describe_no_trade_reason(value: Mapping | None) -> str:
    """通知用の短い表記。記録がない(古いレポートなど)場合は「未記録」。"""
    if not value or not value.get("reason"):
        return "未記録"
    return str(value["reason"])
