"""
================================================================================
ドメインモデル定義モジュール
取引システムで使用される主要なデータクラスを定義しています。
================================================================================
"""
from __future__ import annotations
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, Optional

from src.domain.enums import OrderSide, RankingType


# ================================================================================
# 注文履歴
# ================================================================================

@dataclass
class OrderHistoryEntry:
    """
    実行した注文の履歴を記録するデータクラス。
    
    Attributes:
        symbol: 株式シンボル（銘柄コード）
        side: 注文種別（買い/売り）
        price: 約定価格
        qty: 注文数量
        timestamp: 注文時刻（ISO形式）
        result_code: kabu APIの発注応答コード（Result。0が成功）
        order_id: kabu APIが発行した注文受付番号（OrderId）
        basis_lower_band: 発注根拠となった下側バンド
        basis_upper_band: 発注根拠となった上側バンド
    """
    symbol: str
    side: OrderSide
    price: float
    qty: int
    timestamp: str
    result_code: Optional[int] = None
    order_id: Optional[str] = None
    basis_lower_band: Optional[float] = None
    basis_upper_band: Optional[float] = None
    atr: Optional[float] = None
    atr_true_range: Optional[float] = None
    atr_ratio: Optional[float] = None
    atr_level: Optional[str] = None
    atr_stop_multiplier: Optional[float] = None
    market_regime: Optional[str] = None
    decision_reason: Optional[str] = None
    rsi: Optional[float] = None
    rsi_entry_threshold: Optional[float] = None
    rsi_exit_threshold: Optional[float] = None
    current_price: Optional[float] = None
    rsi_input: Optional[Dict] = None
    order_qty_before_atr: Optional[int] = None
    allocated_budget: Optional[float] = None
    price_source: Optional[str] = None
    price_quality: Optional[str] = None
    price_observed_at: Optional[str] = None
    price_age_seconds: Optional[float] = None

    @classmethod
    def from_dict(cls, data: Dict) -> 'OrderHistoryEntry':
        """
        辞書からOrderHistoryEntryインスタンスを生成します。
        
        Args:
            data: 注文履歴データを含む辞書
            
        Returns:
            OrderHistoryEntryインスタンス
        """
        return cls(
            symbol=data['symbol'],
            side=OrderSide(data['side']),
            price=data['price'],
            qty=data['qty'],
            timestamp=data['timestamp'],
            result_code=data.get('result_code'),
            order_id=data.get('order_id'),
            basis_lower_band=data.get('basis_lower_band', data.get('basis_buy_limit')),
            basis_upper_band=data.get('basis_upper_band', data.get('basis_sell_limit')),
            atr=data.get('atr'),
            atr_true_range=data.get('atr_true_range'),
            atr_ratio=data.get('atr_ratio'),
            atr_level=data.get('atr_level'),
            atr_stop_multiplier=data.get('atr_stop_multiplier'),
            market_regime=data.get('market_regime'),
            decision_reason=data.get('decision_reason'),
            rsi=data.get('rsi'),
            rsi_entry_threshold=data.get('rsi_entry_threshold'),
            rsi_exit_threshold=data.get('rsi_exit_threshold'),
            current_price=data.get('current_price'),
            rsi_input=data.get('rsi_input'),
            order_qty_before_atr=data.get('order_qty_before_atr'),
            allocated_budget=data.get('allocated_budget'),
            price_source=data.get('price_source'),
            price_quality=data.get('price_quality'),
            price_observed_at=data.get('price_observed_at'),
            price_age_seconds=data.get('price_age_seconds'),
        )

    def to_dict(self) -> Dict:
        """
        OrderHistoryEntryを辞書形式に変換します。
        
        Returns:
            辞書形式の注文履歴データ
        """
        return {
            'symbol': self.symbol,
            'side': self.side.value,
            'price': self.price,
            'qty': self.qty,
            'timestamp': self.timestamp,
            'result_code': self.result_code,
            'order_id': self.order_id,
            'basis_lower_band': self.basis_lower_band,
            'basis_upper_band': self.basis_upper_band,
            'atr': self.atr,
            'atr_true_range': self.atr_true_range,
            'atr_ratio': self.atr_ratio,
            'atr_level': self.atr_level,
            'atr_stop_multiplier': self.atr_stop_multiplier,
            'market_regime': self.market_regime,
            'decision_reason': self.decision_reason,
            'rsi': self.rsi,
            'rsi_entry_threshold': self.rsi_entry_threshold,
            'rsi_exit_threshold': self.rsi_exit_threshold,
            'current_price': self.current_price,
            'rsi_input': self.rsi_input,
            'order_qty_before_atr': self.order_qty_before_atr,
            'allocated_budget': self.allocated_budget,
            'price_source': self.price_source,
            'price_quality': self.price_quality,
            'price_observed_at': self.price_observed_at,
            'price_age_seconds': self.price_age_seconds,
        }


# ================================================================================
# 価格設定
# ================================================================================

@dataclass
class PriceLimit:
    """
    銘柄ごとの上下バンドを保持するデータクラス。
    
    Attributes:
        lower_band: 移動平均より下側のバンド。弱含み時の決済判定に使う
        upper_band: 移動平均より上側のバンド。上抜け時の新規買い判定に使う
    """
    lower_band: float
    upper_band: float


# ================================================================================
# 取引シグナル
# ================================================================================

@dataclass
class TradeSignal:
    """
    買い/売りシグナルを表すデータクラス。
    
    Attributes:
        symbol: 株式シンボル（銘柄コード）
        side: 注文種別（買い/売り）
        price: シグナル発生時の価格
        qty: 推奨注文数量
    """
    symbol: str
    side: OrderSide
    price: float
    qty: int

    @classmethod
    def evaluate(
        cls,
        symbol: str,
        current_price: float,
        limit: PriceLimit,
        rsi: Optional[float],
        rsi_entry_threshold: float = 55.0,
        rsi_exit_threshold: float = 45.0,
        entry_price: Optional[float] = None,
        atr: Optional[float] = None,
        stop_loss_multiplier: Optional[float] = None,
    ) -> Optional['TradeSignal']:
        """
        上昇トレンドへの追随と、その失速時の決済シグナルを生成します。
        
        Args:
            symbol: 株式シンボル
            current_price: 現在価格
            limit: トレンド追随判定に使う上下バンド
            
        Returns:
            TradeSignalインスタンス（シグナルがない場合はNone）
        """
        regular_exit = rsi is not None and current_price <= limit.lower_band and rsi <= rsi_exit_threshold
        atr_exit = (
            entry_price is not None
            and atr is not None
            and stop_loss_multiplier is not None
            and current_price <= entry_price - atr * stop_loss_multiplier
        )
        if regular_exit or atr_exit:
            return cls(symbol=symbol, side=OrderSide.SELL, price=current_price, qty=0)
        if rsi is not None and current_price >= limit.upper_band and rsi >= rsi_entry_threshold:
            return cls(symbol=symbol, side=OrderSide.BUY, price=current_price, qty=0)
        return None

    def to_order_history_entry(
        self,
        limit: 'PriceLimit',
        order_response: Optional[Dict] = None,
        diagnostics: Optional[Dict] = None,
        timestamp: Optional[datetime] = None,
    ) -> 'OrderHistoryEntry':
        """
        TradeSignalを注文履歴エントリに変換します。
        
        Args:
            limit: シグナル生成の根拠となった買い/売り基準値
            order_response: kabu APIの発注応答（Result, OrderIdを含む）
            
        Returns:
            OrderHistoryEntryインスタンス
        """
        order_response = order_response or {}
        diagnostics = diagnostics or {}
        return OrderHistoryEntry(
            symbol=self.symbol,
            side=self.side,
            price=self.price,
            qty=self.qty,
            timestamp=(timestamp if timestamp is not None else datetime.now()).isoformat(),
            result_code=order_response.get('Result'),
            order_id=order_response.get('OrderId'),
            basis_lower_band=limit.lower_band,
            basis_upper_band=limit.upper_band,
            atr=diagnostics.get('atr'),
            atr_true_range=diagnostics.get('atr_true_range'),
            atr_ratio=diagnostics.get('atr_ratio'),
            atr_level=diagnostics.get('atr_level'),
            atr_stop_multiplier=diagnostics.get('atr_stop_multiplier'),
            market_regime=diagnostics.get('market_regime'),
            decision_reason=diagnostics.get('decision_reason'),
            rsi=diagnostics.get('rsi'),
            rsi_entry_threshold=diagnostics.get('rsi_entry_threshold'),
            rsi_exit_threshold=diagnostics.get('rsi_exit_threshold'),
            current_price=diagnostics.get('current_price'),
            rsi_input=diagnostics.get('rsi_input'),
            order_qty_before_atr=diagnostics.get('order_qty_before_atr'),
            allocated_budget=diagnostics.get('allocated_budget'),
            price_source=diagnostics.get('price_source'),
            price_quality=diagnostics.get('price_quality'),
            price_observed_at=diagnostics.get('price_observed_at'),
            price_age_seconds=diagnostics.get('price_age_seconds'),
        )


# ================================================================================
# ランキング情報
# ================================================================================

@dataclass
class RankingEntry:
    """
    銘柄のランキング情報を保持するデータクラス。
    
    Attributes:
        symbol: 株式シンボル（銘柄コード）
        rank: ランキング順位
        value: ランキング値（出来高、値上がり率など）
        ranking_type: ランキング種別
    """
    symbol: str
    rank: int
    value: float
    ranking_type: RankingType
    current_price: float | None = None


@dataclass(frozen=True)
class ListedSecurity:
    """日付付き上場銘柄マスタの1レコード。"""
    symbol: str
    exchange_division: str
    listed_from: str
    listed_to: Optional[str] = None

    def is_listed_on(self, target_date: str) -> bool:
        return self.listed_from <= target_date and (
            self.listed_to is None or target_date <= self.listed_to
        )


# ================================================================================
# 規制情報
# ================================================================================

@dataclass
class Regulation:
    """
    銘柄の規制・制限情報を保持するデータクラス。
    
    Attributes:
        symbol: 株式シンボル（銘柄コード）
        is_restricted: 取引が制限されているかどうか
        reason: 制限理由
        primary_exchange: プライマリ取引所コード
    """
    symbol: str
    is_restricted: bool
    reason: str = ""
    primary_exchange: int = 1


@dataclass
class ExclusionResult:
    """候補除外の結果。"""
    remaining: list[str]
    excluded_by_regulation_count: int = 0
    excluded_by_exchange_count: int = 0


@dataclass
class PriceFilterResult:
    """株価上限による候補除外の結果。"""
    remaining: list[str]
    excluded_by_price_count: int = 0
    excluded_missing_price_count: int = 0


# ================================================================================
# スクリーニング結果
# ================================================================================

@dataclass
class ScreeningAuditEntry:
    """スクリーニング候補の採用判定を再現するための監査情報。"""
    symbol: str
    turnover_rank: int
    turnover_value: float
    price_gain_rank: int
    price_gain_value: float
    total_rank: int
    primary_exchange: int
    is_restricted: bool
    restriction_reason: str
    selected: bool
    price: float | None = None
    check_status: str | None = None


@dataclass
class ScreeningResult:
    """
    スクリーニング処理の結果を保持するデータクラス。
    
    Attributes:
        date: スクリーニング実行日（ISO形式）
        symbols: スクリーニング対象銘柄のリスト
        generated_at: 結果生成時刻（ISO形式）
        audit_entries: 全候補のランキング・規制・採用判定情報
    """
    date: str
    symbols: list[str]
    generated_at: str
    audit_entries: list[ScreeningAuditEntry] = field(default_factory=list)

    def to_dict(self) -> Dict:
        """JSON永続化用の辞書へ変換します。"""
        return {
            "date": self.date,
            "symbols": self.symbols,
            "generated_at": self.generated_at,
            "audit_entries": [entry.__dict__ for entry in self.audit_entries],
        }

    @classmethod
    def from_dict(cls, data: Dict) -> "ScreeningResult":
        """旧形式のJSONも含め、辞書から結果を復元します。"""
        return cls(
            date=data["date"],
            symbols=data["symbols"],
            generated_at=data["generated_at"],
            audit_entries=[ScreeningAuditEntry(**entry) for entry in data.get("audit_entries", [])],
        )


# ================================================================================
# スコア付き候補
# ================================================================================

@dataclass
class ScoredCandidate:
    """
    出来高などの指標でスコアリングされた候補銘柄を保持するデータクラス。
    
    Attributes:
        symbol: 株式シンボル（銘柄コード）
        today_volume: 今日の出来高
        average_volume: 平均出来高
        surge_ratio: 急騰率
    """
    symbol: str
    today_volume: float
    average_volume: float
    surge_ratio: float


@dataclass(frozen=True)
class Candidate:
    """順位付きの発注候補。"""
    symbol: str
    current_price: float
    rank: int


@dataclass(frozen=True)
class Allocation:
    """候補銘柄に割り当てる予算と購入予定数量。"""
    symbol: str
    budget: float
    quantity: int


# ================================================================================
# フィルタリング結果
# ================================================================================

@dataclass
class FilteringResult:
    """
    フィルタリング処理の結果を保持するデータクラス。
    
    Attributes:
        date: フィルタリング実行日（ISO形式）
        symbols: フィルタリング後の銘柄リスト
        generated_at: 結果生成時刻（ISO形式）
    """
    date: str
    symbols: list[str]
    generated_at: str


# ================================================================================
# 分足データ（簡易収集）
# ================================================================================

@dataclass
class MinuteBar:
    """
    分足1本分のデータ。

    2種類の取得元を想定する:
    - "yahoo": Yahoo Financeのintraday chart APIから取得した本物のOHLC由来の値（高精度）
    - "poll": kabuステーション/boardの1分間隔ポーリングをそのまま1本の足とした値（簡易・低精度）

    Attributes:
        time: 記録時刻（ISO形式、分単位に丸め済み）
        price: その分の価格（Yahoo由来なら終値、ポーリング由来ならCurrentPrice）
        cumulative_volume: ポーリング時点の当日累積出来高（TradingVolume）。Yahoo由来はNone
        volume: その分の出来高（Yahoo由来はAPIの値そのまま、ポーリング由来は累積出来高の差分）
        source: データの取得元（"yahoo" または "poll"）
    """
    time: str
    price: float
    cumulative_volume: Optional[float]
    volume: Optional[float]
    source: str = "poll"
