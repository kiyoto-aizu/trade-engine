"""実注文を発生させないペーパートレード用の注文実行実装。"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from datetime import date
from numbers import Real
from typing import Callable, Dict, List, Optional
from pathlib import Path

from src.config import config
from src.infrastructure.persistence.storage import (
    StateFileCorruptError,
    preserve_corrupt_copy,
    read_json_strict,
    write_json,
)

logger = logging.getLogger(__name__)


@dataclass
class PaperOrderClient:
    """現在価格を使って仮想残高と保有株を更新する注文実行器。"""

    prices: Dict[str, float]
    cash: float = 1_000_000.0
    order_qty: int = field(default_factory=lambda: config.ORDER_UNIT)
    fee_rate: float = field(default_factory=lambda: config.PAPER_FEE_RATE)
    market_slippage_bps: float = field(default_factory=lambda: config.PAPER_MARKET_SLIPPAGE_BPS)
    holdings: Dict[str, int] = field(default_factory=dict)
    average_costs: Dict[str, float] = field(default_factory=dict)
    orders: List[dict] = field(default_factory=list)
    realized_pnl: float = 0.0
    realized_pnl_date: str = field(default_factory=lambda: date.today().isoformat())
    today_provider: Callable[[], date] = field(default=date.today, repr=False, compare=False)
    state_path: Optional[Path] = None
    _next_order_id: int = 1
    last_rejection_reason: Optional[str] = field(default=None, init=False)
    _price_metadata: Dict[str, dict] = field(default_factory=dict, init=False, repr=False)
    # 状態ファイルの保存が連続して失敗した回数(成功で0に戻る)。TradingUseCaseが参照する
    consecutive_save_failures: int = field(default=0, init=False)

    def __post_init__(self) -> None:
        if self.state_path is None:
            return
        state = read_json_strict(self.state_path)
        if state is None:
            return
        try:
            self._apply_state(state)
        except (TypeError, ValueError, AttributeError, OverflowError) as exc:
            raise StateFileCorruptError(
                self.state_path,
                f"値が不正です({type(exc).__name__}: {exc})",
                preserve_corrupt_copy(self.state_path),
            ) from exc

    def _apply_state(self, state: dict) -> None:
        cash = float(state.get('cash', self.cash))
        holdings = {
            str(symbol): int(quantity)
            for symbol, quantity in state.get('holdings', {}).items()
            if int(quantity) > 0
        }
        average_costs = {
            str(symbol): float(cost)
            for symbol, cost in state.get('average_costs', {}).items()
            if symbol in holdings
        }
        next_order_id = max(1, int(state.get('next_order_id', self._next_order_id)))
        realized_pnl_date = state.get('realized_pnl_date', self.realized_pnl_date)
        realized_pnl = (
            float(state.get('realized_pnl', 0.0))
            if realized_pnl_date == self.today_provider().isoformat()
            else 0.0
        )
        self.cash = cash
        self.holdings = holdings
        self.average_costs = average_costs
        self._next_order_id = next_order_id
        self.realized_pnl_date = realized_pnl_date
        self.realized_pnl = realized_pnl

    def _save_state(self) -> bool:
        if self.state_path is None:
            return True
        saved = write_json(self.state_path, {
            'cash': self.cash,
            'holdings': self.holdings,
            'average_costs': self.average_costs,
            'next_order_id': self._next_order_id,
            'realized_pnl': self.realized_pnl,
            'realized_pnl_date': self.realized_pnl_date,
        })
        self.consecutive_save_failures = 0 if saved else self.consecutive_save_failures + 1
        return saved

    def retry_save_state(self) -> bool:
        """現在のメモリ状態を保存し直す。"""
        return self._save_state()

    def set_price(self, symbol: str, price: float, metadata: Optional[dict] = None) -> None:
        self.prices[symbol] = price
        self._price_metadata[symbol] = dict(metadata or {})

    def place_market_order(self, token: str, symbol: str, side: str, quantity: Optional[int] = None) -> Optional[dict]:
        """成行注文を不利方向のスリッページ・手数料込みで仮想約定する。"""
        del token
        self.last_rejection_reason = None
        price = self.prices.get(symbol)
        if price is None:
            self.last_rejection_reason = "ORDER_REJECTED_PAPER_PRICE_MISSING"
            logger.error(
                "%s: 銘柄=%s | 方向=%s | 価格=%r",
                self.last_rejection_reason,
                symbol,
                side,
                price,
            )
            return None
        try:
            if isinstance(price, bool) or not isinstance(price, Real):
                raise TypeError("価格が数値ではありません")
            price = float(price)
        except (TypeError, ValueError, OverflowError):
            price = None
        if price is None or not math.isfinite(price) or price <= 0:
            self.last_rejection_reason = "ORDER_REJECTED_PAPER_PRICE_INVALID"
            logger.error(
                "%s: 銘柄=%s | 方向=%s | 価格=%r",
                self.last_rejection_reason,
                symbol,
                side,
                self.prices.get(symbol),
            )
            return None

        quantity = quantity if quantity is not None else self.order_qty
        held_quantity = self.holdings.get(symbol, 0)
        slippage_rate = self.market_slippage_bps / 10_000.0
        if side == config.OrderSide.BUY.value:
            execution_price = price * (1.0 + slippage_rate)
            fee = execution_price * quantity * self.fee_rate
            required_cash = execution_price * quantity + fee
            if self.cash < required_cash:
                return None
            self.cash -= required_cash
            self.holdings[symbol] = held_quantity + quantity
            previous_cost = self.average_costs.get(symbol, 0.0) * held_quantity
            self.average_costs[symbol] = (previous_cost + required_cash) / self.holdings[symbol]
        elif side == config.OrderSide.SELL.value:
            if held_quantity < quantity:
                return None
            execution_price = price * (1.0 - slippage_rate)
            fee = execution_price * quantity * self.fee_rate
            self._reset_daily_realized_pnl_if_needed()
            self.realized_pnl += (execution_price - self.average_costs.get(symbol, 0.0)) * quantity - fee
            self.cash += execution_price * quantity - fee
            remaining_quantity = held_quantity - quantity
            if remaining_quantity:
                self.holdings[symbol] = remaining_quantity
            else:
                self.holdings.pop(symbol, None)
                self.average_costs.pop(symbol, None)
        else:
            return None

        order_id = f"paper-{self._next_order_id}"
        self._next_order_id += 1
        order = {
            "Result": 0,
            "OrderId": order_id,
            "Symbol": symbol,
            "Side": side,
            "Qty": quantity,
            "Price": execution_price,
            "SignalPrice": price,
            "Fee": fee,
            "SlippageBps": self.market_slippage_bps,
        }
        order.update(self._price_metadata.get(symbol, {}))
        self.orders.append(order)
        self._save_state()
        return order

    def get_wallet_cash(self, token: str) -> dict:
        del token
        return {'StockAccountWallet': self.cash}

    def _reset_daily_realized_pnl_if_needed(self) -> None:
        today = self.today_provider().isoformat()
        if self.realized_pnl_date != today:
            self.realized_pnl_date = today
            self.realized_pnl = 0.0

    def get_daily_realized_pnl(self) -> float:
        self._reset_daily_realized_pnl_if_needed()
        return round(self.realized_pnl, 2)

    def get_positions(self, token: str) -> list[dict]:
        del token
        return [
            {
                'Symbol': symbol,
                'Side': config.OrderSide.SELL.value,
                'HoldQty': quantity,
                'AveragePrice': self.average_costs.get(symbol, 0.0),
                'CurrentPrice': self.prices.get(symbol, 0.0),
                'ProfitLoss': round((self.prices.get(symbol, 0.0) - self.average_costs.get(symbol, 0.0)) * quantity, 2),
            }
            for symbol, quantity in self.holdings.items()
        ]
