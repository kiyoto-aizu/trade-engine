"""
================================================================================
ビジネスルール定義モジュール
取引に関する検証・計算ロジックを集中管理しています。
================================================================================
"""
import logging
from datetime import date, datetime, time, timedelta
from typing import Callable, List, Optional

from src.domain.enums import OrderSide
from src.domain.models import ExclusionResult, OrderHistoryEntry, PriceFilterResult, PriceLimit, ScoredCandidate, TradeSignal

logger = logging.getLogger(__name__)


# ================================================================================
# 価格基準値の計算
# ================================================================================

def calculate_price_limit(closes: List[float], period: int = 5) -> Optional[PriceLimit]:
    """
    過去の終値から移動平均を計算し、上下バンドを決定します。
    
    Args:
        closes: 過去の終値のリスト
        
    Returns:
        下側バンド（移動平均の99%）と上側バンド（移動平均の101%）を含むPriceLimitオブジェクト
        データ不足の場合はNone
    """
    if not closes or len(closes) < period:
        return None

    moving_average = sum(closes[-period:]) / period
    return PriceLimit(
        lower_band=round(moving_average * 0.99, 1),
        upper_band=round(moving_average * 1.01, 1),
    )


def calculate_rsi(closes: List[float], period: int = 14, minimum_closes: int = 30) -> Optional[float]:
    """確定終値からWilder方式のRSIを計算します。"""
    if len(closes) < minimum_closes or len(closes) < period + 1:
        return None
    if any(price <= 0 for price in closes):
        return None

    changes = [current - previous for previous, current in zip(closes, closes[1:])]
    gains = [max(change, 0.0) for change in changes]
    losses = [max(-change, 0.0) for change in changes]
    average_gain = sum(gains[:period]) / period
    average_loss = sum(losses[:period]) / period
    for gain, loss in zip(gains[period:], losses[period:]):
        average_gain = (average_gain * (period - 1) + gain) / period
        average_loss = (average_loss * (period - 1) + loss) / period

    if average_loss == 0:
        return 50.0 if average_gain == 0 else 100.0
    if average_gain == 0:
        return 0.0
    relative_strength = average_gain / average_loss
    return 100.0 - (100.0 / (1.0 + relative_strength))


# ================================================================================
# 市場終了時刻チェック
# ================================================================================

def is_market_closed(now: time, close_hour: int, close_minute: int) -> bool:
    """
    現在時刻が市場終了時刻を過ぎているかどうかを判定します。

    Args:
        now: 現在時刻
        close_hour: 市場クローズ時刻（時）
        close_minute: 市場クローズ時刻（分）

    Returns:
        市場終了時刻に達している場合True
    """
    return (now.hour, now.minute) >= (close_hour, close_minute)


def is_trading_day(
    target_date: date,
    holiday_checker: Callable[[date], bool] | None = None,
) -> bool:
    """
    指定日が株式市場の稼働日かどうかを判定します。
    土曜・日曜・祝日・年末年始(12/31, 1/2, 1/3)を非稼働日とします。

    Args:
        target_date: 判定対象の日付

    Returns:
        稼働日の場合True
    """
    if target_date.weekday() >= 5:
        return False
    if holiday_checker is not None and holiday_checker(target_date):
        return False
    if target_date.month == 12 and target_date.day == 31:
        return False
    if target_date.month == 1 and target_date.day in (2, 3):
        return False
    return True


def is_trading_session(
    now: datetime,
    open_hour: int,
    open_minute: int,
    close_hour: int,
    close_minute: int,
    holiday_checker: Callable[[date], bool] | None = None,
) -> bool:
    """稼働日かつ市場時間内かを判定します。"""
    if not is_trading_day(now.date(), holiday_checker=holiday_checker):
        return False
    session_start = time(open_hour, open_minute)
    session_end = time(close_hour, close_minute)
    return session_start <= now.time() < session_end


# ================================================================================
# 注文重複チェック
# ================================================================================

def is_duplicate_order(
    signal: TradeSignal,
    order_history: List[OrderHistoryEntry],
    now: datetime | None = None,
) -> bool:
    """
    同一銘柄・同一方向の注文が当日中に既に存在するかを確認します。
    
    Args:
        signal: チェック対象の取引シグナル
        order_history: 注文履歴のリスト
        
    Returns:
        重複が存在する場合True、存在しない場合False
    """
    today = (now if now is not None else datetime.now()).date()
    return any(
        entry.symbol == signal.symbol and entry.side == signal.side
        and _entry_date(entry) == today
        for entry in order_history
    )


def _entry_date(entry: OrderHistoryEntry):
    """
    注文履歴エントリからタイムスタンプを抽出して日付に変換します。
    
    Args:
        entry: 注文履歴エントリ
        
    Returns:
        日付オブジェクト、パースエラーの場合はNone
    """
    try:
        return datetime.fromisoformat(entry.timestamp).date()
    except (TypeError, ValueError) as exc:
        logger.warning(
            "ORDER_HISTORY_TIMESTAMP_INVALID: 銘柄=%s | 方向=%s | timestamp=%r | 理由=%s",
            entry.symbol,
            getattr(entry.side, "value", entry.side),
            entry.timestamp,
            type(exc).__name__,
        )
        return None


# ================================================================================
# 最近の注文チェック
# ================================================================================

def is_recent_order(
    signal: TradeSignal,
    order_history: List[OrderHistoryEntry],
    lock_seconds: int,
    now: datetime | None = None,
) -> bool:
    """
    指定秒数以内に同一銘柄・同一方向の注文が存在するかを確認します。
    二重発注防止のため、ロック期間内の注文をチェックします。
    
    Args:
        signal: チェック対象の取引シグナル
        order_history: 注文履歴のリスト
        lock_seconds: ロック期間（秒）
        
    Returns:
        ロック期間内に重複が存在する場合True、存在しない場合False
    """
    current_time = now if now is not None else datetime.now()
    cutoff = current_time - timedelta(seconds=lock_seconds)
    for entry in reversed(order_history):
        if entry.symbol == signal.symbol and entry.side == signal.side:
            try:
                ts = datetime.fromisoformat(entry.timestamp)
                if ts >= cutoff:
                    return True
            except (TypeError, ValueError) as exc:
                logger.warning(
                    "ORDER_HISTORY_TIMESTAMP_INVALID: 銘柄=%s | 方向=%s | timestamp=%r | 理由=%s",
                    entry.symbol,
                    getattr(entry.side, "value", entry.side),
                    entry.timestamp,
                    type(exc).__name__,
                )
                continue
    return False


# ================================================================================
# 注文実行可否判定
# ================================================================================

def is_safe_to_order(
    signal: TradeSignal,
    wallet_amount: Optional[float],
    has_holdings: bool,
    order_history: List[OrderHistoryEntry],
    lock_seconds: int,
    warn_on_missing_holdings: bool = True,
    now: datetime | None = None,
) -> bool:
    """
    取引シグナルに基づいて注文を実行しても安全かどうかを総合的に判定します。
    
    チェック項目：
    - 買い注文の場合、十分な資金があるか
    - 売り注文の場合、保有株があるか
    - 当日同一銘柄の重複注文がないか
    - ロック期間内の重複注文がないか
    
    Args:
        signal: 実行予定の取引シグナル
        wallet_amount: 現物買付可能額
        has_holdings: 保有株の有無
        order_history: 注文履歴のリスト
        lock_seconds: ロック期間（秒）
        
    Returns:
        注文を実行しても安全な場合True、安全でない場合False
    """
    if signal.side == OrderSide.BUY:
        if wallet_amount is None:
            logger.warning("現物買付可能額が不明なため、買い注文を見送ります。")
            return False

        required = signal.price * signal.qty
        if required > wallet_amount:
            logger.warning("予算不足: 注文必要額 %s 円 > 買付可能額 %s 円", required, wallet_amount)
            return False

    if signal.side == OrderSide.SELL and not has_holdings:
        if warn_on_missing_holdings:
            logger.info(
                "売り条件該当・未保有: 銘柄=%s | 売り注文を見送ります。",
                signal.symbol,
            )
        return False

    if is_duplicate_order(signal, order_history, now):
        logger.warning("当日同一シンボル・同一方向の注文が既にあります。")
        return False

    if is_recent_order(signal, order_history, lock_seconds, now):
        logger.warning("同一注文が %s 秒以内に発生しています。二重発注を防止します。", lock_seconds)
        return False

    return True


# ================================================================================
# ランキング候補の統合・並べ替え
# ================================================================================

def merge_ranking_candidates(turnover_ranking, price_gain_ranking, price_gain_weight: float = 1.0):
    """
    出来高ランキングと値上がり率ランキングを統合し、総合スコアで順位付けします。
    両ランキングで上位の銘柄が高スコアになります。

    Args:
        turnover_ranking: 出来高ランキングのリスト
        price_gain_ranking: 値上がり率ランキングのリスト
        price_gain_weight: 値上がり率側の順位に掛ける重み係数(ADR-0001)。
            デフォルト1.0は従来通りの等重み合算。1.0未満にすると、
            既に値上がりして予算上限に近づいている銘柄の影響を弱められる。

    Returns:
        統合スコアでソートされた銘柄シンボルのリスト
    """
    rankings = (turnover_ranking, price_gain_ranking)
    weights = (1.0, price_gain_weight)
    defaults = [len(ranking) + 1 for ranking in rankings]
    scores = {}
    for index, ranking in enumerate(rankings):
        for entry in ranking:
            scores.setdefault(entry.symbol, [defaults[0], defaults[1]])[index] = entry.rank
    return sorted(
        scores,
        key=lambda symbol: (
            sum(rank * weight for rank, weight in zip(scores[symbol], weights)),
            symbol,
        ),
    )


# ================================================================================
# 規制・制限チェック
# ================================================================================

def exclude_by_regulation(candidates, regulations):
    """
    規制対象外の銘柄のみをフィルタリングして返します。
    
    除外条件：
    - 取引が制限されている銘柄
    - プライマリ取引所が3, 5, 6（指定市場など）である銘柄
    
    Args:
        candidates: フィルタリング対象の銘柄シンボルのリスト
        regulations: 銘柄ごとの規制情報を含む辞書
        
    Returns:
        残った銘柄と理由別の除外件数
    """
    remaining = []
    excluded_by_regulation_count = 0
    excluded_by_exchange_count = 0
    for symbol in candidates:
        regulation = regulations.get(symbol)
        if regulation is None or regulation.is_restricted:
            excluded_by_regulation_count += 1
        elif regulation.primary_exchange in {3, 5, 6}:
            excluded_by_exchange_count += 1
        else:
            remaining.append(symbol)
    return ExclusionResult(
        remaining=remaining,
        excluded_by_regulation_count=excluded_by_regulation_count,
        excluded_by_exchange_count=excluded_by_exchange_count,
    )


def filter_candidates_by_price(candidates, price_by_symbol, price_cap):
    """株価上限を超える、または価格不明の候補を除外する。"""
    remaining = []
    excluded_by_price_count = 0
    excluded_missing_price_count = 0
    for symbol in candidates:
        price = price_by_symbol.get(symbol)
        if price is None or price <= 0:
            excluded_missing_price_count += 1
        elif price > price_cap:
            excluded_by_price_count += 1
        else:
            remaining.append(symbol)
    return PriceFilterResult(
        remaining=remaining,
        excluded_by_price_count=excluded_by_price_count,
        excluded_missing_price_count=excluded_missing_price_count,
    )


def limit_candidates(candidates, min_count=30, max_count=50):
    if len(candidates) < min_count:
        logger.warning("候補銘柄が%d件で、最小件数%d件を下回っています。", len(candidates), min_count)
    return candidates[:max_count]


def calculate_volume_surge_ratio(today_volume: float, average_volume: float) -> float:
    if average_volume <= 0:
        raise ValueError("average_volume must be positive")
    return today_volume / average_volume


def select_top_n_by_surge_ratio(scored: List[ScoredCandidate], n: int = 10):
    return [candidate.symbol for candidate in sorted(scored, key=lambda item: (-item.surge_ratio, item.symbol))[:n]]


def check_kill_switch(daily_orders, daily_pnl, capital, settings):
    if daily_orders >= settings.MAX_ORDER_COUNT_PER_DAY:
        return False
    if capital > 0 and daily_pnl <= -(capital * settings.DAILY_LOSS_LIMIT_RATIO):
        return False
    return True


def is_buy_order_amount_allowed(order_amount, settings, api_soft_limit=None):
    """買い注文の金額上限を判定します。売り注文には適用しません。"""
    effective_soft_limit = api_soft_limit if api_soft_limit is not None else settings.API_SOFT_LIMIT
    max_amount = min(settings.MAX_ORDER_AMOUNT_PER_TRADE, effective_soft_limit)
    return order_amount <= max_amount


def calculate_buy_quantity(price, max_amount, order_unit):
    """上限額内で購入できる最大の売買単位数を返します。"""
    if price <= 0 or max_amount <= 0 or order_unit <= 0:
        return 0
    return int(max_amount // (price * order_unit)) * order_unit
