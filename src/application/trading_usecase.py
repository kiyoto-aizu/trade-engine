"""
================================================================================
取引ユースケース
フィルタリング対象銘柄の売買シグナル生成・注文実行を行うアプリケーションロジック層です。
リアルタイムの価格監視、資金チェック、キルスイッチ判定などの取引制御を実装しています。
================================================================================
"""
import logging
import json
import hashlib
import inspect
import math
import time
from collections import Counter
from datetime import datetime, timedelta, timezone
from datetime import date
from datetime import time as clock_time
from pathlib import Path
from typing import List, Optional

from src.config import config
from src.domain.models import OrderHistoryEntry, PriceLimit, TradeSignal
from src.domain.rules import calculate_buy_quantity, calculate_price_limit, calculate_rsi, check_kill_switch, is_buy_order_amount_allowed, is_market_closed, is_safe_to_order
from src.domain.volatility import DailyBar, VolatilityLevel, adjust_quantity_for_volatility, assess_volatility, resolve_atr_exit_multiplier, stop_loss_multiplier
from src.domain.market_regime import MarketRegime, resolve_rsi_entry_threshold
from src.domain.trading_progress_report import build_trading_progress_report
from src.infrastructure.kabu.get_board import (
    get_current_board,
    get_current_board_with_freshness,
)
from src.infrastructure.kabu.get_positions import get_positions
from src.infrastructure.kabu.get_wallet import get_wallet_cash
from src.infrastructure.kabu.get_apisoftlimit import get_api_soft_limit
from src.infrastructure.kabu.send_order import place_market_order
from src.infrastructure.market_data.get_daily_closes import (
    get_yahoo_daily_bars,
    get_yahoo_daily_closes,
    reset_empty_daily_warning_dedupe,
)
from src.infrastructure.notification.slack_notify import (
    format_result_notification,
    notify_critical,
    notify_daily,
)
from src.infrastructure.calendar.japanese_calendar import is_trading_session
from src.infrastructure.persistence.storage import (
    StateFileCorruptError,
    preserve_corrupt_copy,
    read_json,
    read_json_strict,
    write_json,
)
from src.infrastructure.persistence.filter_decision_repository import FilterDecisionRepository
from src.domain.no_trade_reason import classify_no_trade_reason
from src.infrastructure.persistence.decision_journal_repository import STAGE_TRADING
from src.infrastructure.analysis.daily_analyzer import create_daily_analyzer
from src.application.shadow_position_tracker import ShadowPosition, resolve_reference

logger = logging.getLogger(__name__)
JST = timezone(timedelta(hours=9))
# 見送りイベントへ日経225の日中値を追記する対象(検証用の記録のみ)
NIKKEI_INTRADAY_EVENT_TYPES = frozenset({
    "MARKET_REGIME_DANGER_SKIP",
    "MARKET_REGIME_CAUTION_RSI_FILTER",
    "ATR_DANGER_SKIP",
    "ADX_TREND_RELIEF",
})
NIKKEI_INTRADAY_CACHE_SECONDS = 60
NIKKEI_INTRADAY_TIMEOUT_SECONDS = 5


class TradingUseCase:
    """
    取引処理を実行するユースケッククラス。
    
    機能：
    - 株価監視と売買シグナル生成
    - 資金確認と保有株確認
    - キルスイッチによるリスク制御
    - 注文実行と履歴管理
    - 市場終了時のレポート送信
    """
    
    def __init__(
        self,
        token: str,
        order_history_path: Path,
        market_data_client=None,
        board_client=None,
        wallet_client=None,
        positions_client=None,
        order_sender=None,
        filtering_result_repository=None,
        notifier=None,
        daily_analyzer=None,
        daily_report_directory: Optional[Path] = None,
        market_regime_usecase=None,
        filter_decision_repository: FilterDecisionRepository | None = None,
        kill_switch_baseline_path: Optional[Path] = None,
        decision_journal_repository=None,
        enable_shadow_position_tracking: bool = False,
        market_regime_daily_repository=None,
    ):
        """
        TradingUseCaseを初期化します。
        
        Args:
            token: Kabu.com Station API認証トークン
            order_history_path: 注文履歴を保存するファイルパス
            market_data_client: Yahoo FinanceなどのMarketDataクライアント（オプション、テスト用）
            board_client: リアルタイム板情報クライアント（オプション、テスト用）
            wallet_client: 口座資金情報クライアント（オプション、テスト用）
            positions_client: 保有株情報クライアント（オプション、テスト用）
            order_sender: 注文送信クライアント（オプション、テスト用）
            filtering_result_repository: フィルタリング結果リポジトリ（オプション）
            notifier: 通知機能（テスト用オプション）
        """
        self.token = token
        self.order_history_path = order_history_path
        self.order_history: List[OrderHistoryEntry] = []
        # 依存性注入（テスト時は別実装を注入可能）
        self.market_data_client = market_data_client
        self.board_client = board_client
        self.wallet_client = wallet_client
        self.positions_client = positions_client
        self.order_sender = order_sender
        self.filtering_result_repository = filtering_result_repository
        self.notifier = notifier
        self._now_provider = datetime.now
        self._current_now: datetime | None = None
        self.daily_analyzer = daily_analyzer if daily_analyzer is not None else create_daily_analyzer()
        self.daily_report_directory = daily_report_directory or config.DAILY_REPORT_DIRECTORY
        self.filter_decision_repository = filter_decision_repository or FilterDecisionRepository(
            order_history_path.parent / "filter_decision_events.sqlite3"
        )
        self.kill_switch_baseline_path = kill_switch_baseline_path or (
            order_history_path.parent / Path(config.KILL_SWITCH_BASELINE_FILE).name
        )
        # 判断記録は注入された場合のみ有効(記録専用で売買判定には使わない)
        self.decision_journal_repository = decision_journal_repository
        self.enable_shadow_position_tracking = enable_shadow_position_tracking
        self._shadow_positions: dict[str, ShadowPosition] = {}
        self._shadow_pending_symbols: set[str] = set()
        self._account_snapshot: dict = {}
        self.market_regime_usecase = market_regime_usecase
        self.market_regime_daily_repository = market_regime_daily_repository
        self.market_regime = MarketRegime.NORMAL
        self.market_regime_assessment = None
        self.last_positions = []
        self.kill_switch_triggered = False
        self.emergency_stop_triggered = False
        self.daily_starting_capital = config.OPERATING_CAPITAL
        self.api_soft_limit: Optional[float] = None
        self._missing_holding_sell_signals: dict[str, bool] = {}
        self._logged_trade_reasons: set[tuple[str, str]] = set()
        self._board_unavailable_symbols: set[str] = set()
        # 売買0件の理由を分類するための記録(売買判定には使わない)
        self._monitored_symbol_count = 0
        self._buy_evaluated_symbols: set[str] = set()
        self._buy_signal_symbols: set[str] = set()
        self._sell_condition_observations: dict[str, str] = {}
        self._liquidation_results: list[dict] = []
        # ADR-0002: ATRトレーリングストップ用の保有中最高値(銘柄ごと、メモリ保持・当日限り)
        self._holding_high_prices: dict[str, float] = {}
        # ADR-0002: 銘柄ごとの直前の判定。変化時だけINFOにする
        self._last_logged_judgments: dict[str, str] = {}
        # kabu API認証の復旧失敗・板取得連続失敗の通知は1runにつき1回に抑える
        self._auth_recovery_notified = False
        self._board_fetch_all_failed_streak = 0
        self._board_fetch_failure_notified = False
        # 状態ファイル保存失敗: 注文履歴の連続失敗回数と、通知済みの連続失敗ストリーク
        self._order_history_save_failures = 0
        self._kill_switch_baseline_save_failures = 0
        self._kill_switch_baseline_unsaved: Optional[dict] = None
        self._save_failure_notified_sources: set[str] = set()
        self._new_buy_halt_notified = False
        self._progress_reports_sent: set[str] = set()
        self._progress_report_start_time: datetime | None = None
        self._nikkei_intraday_cache: tuple[datetime, dict] | None = None

    @property
    def _execution_mode(self) -> str:
        return config.TRADING_MODE

    def _is_paper_mode(self) -> bool:
        return self._execution_mode == "paper"

    def _record_filter_decision_safely(
        self,
        event_type: str,
        symbol: str,
        occurred_at: datetime,
        reference_price: float,
        quantity: int,
        inputs: dict,
    ) -> int | None:
        try:
            if event_type in NIKKEI_INTRADAY_EVENT_TYPES:
                inputs = {**inputs, **self._nikkei_intraday_inputs(occurred_at)}
            return self.filter_decision_repository.record_event(
                event_type, symbol, occurred_at, reference_price, quantity, inputs, self._execution_mode
            )
        except Exception:
            logger.exception("判定イベントの保存に失敗しました: 種別=%s 銘柄=%s", event_type, symbol)

    def _nikkei_intraday_inputs(self, now: datetime) -> dict:
        """見送り時刻の日経225日中値(検証用の記録のみ)。失敗・遅延しても判断や注文には影響させない。"""
        empty = {"nikkei_intraday_change_percent": None, "nikkei_intraday_as_of": None}
        cached = self._nikkei_intraday_cache
        if cached is not None and 0 <= (now - cached[0]).total_seconds() < NIKKEI_INTRADAY_CACHE_SECONDS:
            return cached[1]
        values = empty
        try:
            getter = getattr(
                getattr(self.market_regime_usecase, "market_data_client", None), "get_intraday_snapshot", None
            )
            snapshot = getter("^N225", now, NIKKEI_INTRADAY_TIMEOUT_SECONDS) if getter else None
            if snapshot is not None:
                percent, bar_time = snapshot
                values = {
                    "nikkei_intraday_change_percent": percent,
                    "nikkei_intraday_as_of": (
                        bar_time.astimezone(JST).replace(tzinfo=None) if bar_time.tzinfo else bar_time
                    ).isoformat(timespec="seconds"),
                }
        except Exception as exc:
            logger.warning("日経225の日中値の取得に失敗しました(記録は空で続行): %s", exc)
        # 失敗も同じ秒数だけ保持し、障害中に毎分取得し直して注文ループを遅らせない
        self._nikkei_intraday_cache = (now, values)
        return values

    @staticmethod
    def _shadow_position_key(symbol: str, order_id: str | None, timestamp: str, quantity: int) -> str:
        identity = f"{symbol}|{order_id or ''}|{timestamp}|{quantity}"
        return hashlib.sha256(identity.encode("utf-8")).hexdigest()[:20]

    @staticmethod
    def _shadow_execution_time(order_response: dict | None, fallback: datetime) -> tuple[datetime, bool]:
        raw = (order_response or {}).get("ExecutionTime")
        if raw:
            try:
                value = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
                if value.tzinfo is not None:
                    value = value.astimezone(JST).replace(tzinfo=None)
                return value, True
            except (TypeError, ValueError, OverflowError):
                pass
        return fallback, False

    def _record_shadow_event_safely(self, symbol: str, reason_code: str, occurred_at: datetime, values: dict) -> None:
        if not self.enable_shadow_position_tracking:
            return
        repository = self.decision_journal_repository
        if repository is None:
            self._log_trade_reason_once(
                logging.WARNING, "SHADOW_RECORD_UNAVAILABLE", symbol,
                "判断記録リポジトリが未設定です",
            )
            return
        try:
            repository_mode = getattr(repository, "execution_mode", self._execution_mode)
            if repository_mode != self._execution_mode:
                raise ValueError(f"execution_mode mismatch: {repository_mode}")
            repository.record(STAGE_TRADING, symbol, reason_code, occurred_at, values)
            self._shadow_pending_symbols.add(symbol)
        except Exception as exc:
            self._log_trade_reason_once(
                logging.WARNING, "SHADOW_RECORD_WRITE_FAILED", symbol,
                f"理由={reason_code} | 例外={type(exc).__name__}: {exc}",
            )

    def _start_shadow_position(
        self,
        entry: OrderHistoryEntry,
        order_response: dict | None,
        volatility_assessment,
        previous_close: float | None,
    ) -> None:
        if not self.enable_shadow_position_tracking:
            return
        timestamp = entry.timestamp
        position_key = self._shadow_position_key(entry.symbol, entry.order_id, timestamp, entry.qty)
        if position_key in self._shadow_positions:
            return
        response_price = (order_response or {}).get("Price")
        try:
            fill_price = float(response_price)
            has_fill_price = math.isfinite(fill_price) and fill_price > 0
        except (TypeError, ValueError, OverflowError):
            fill_price = float(entry.price)
            has_fill_price = False
        if not has_fill_price:
            fill_price = float(entry.price)
        fill_time, has_fill_time = self._shadow_execution_time(
            order_response, self._current_now or datetime.fromisoformat(timestamp)
        )
        reference, reference_kind = resolve_reference(
            getattr(volatility_assessment, "atr", None), previous_close
        )
        position = ShadowPosition(
            position_key=position_key,
            symbol=entry.symbol,
            buy_at=fill_time,
            buy_price=fill_price,
            buy_quantity=entry.qty,
            reference_value=reference,
            reference_kind=reference_kind,
        )
        if not has_fill_price:
            position.data_quality.add("buy_price_is_order_reference")
        if not has_fill_time:
            position.data_quality.add("buy_time_is_order_acceptance_time")
        if self._execution_mode == "live":
            position.data_quality.add("live_buy_fill_not_confirmed")
        self._shadow_positions[position_key] = position

    def _observe_shadow_positions(
        self,
        symbol: str,
        observed_at: datetime,
        price: float | None,
        volatility_assessment=None,
        previous_close: float | None = None,
        quality: str | None = None,
    ) -> None:
        if not self.enable_shadow_position_tracking:
            return
        try:
            for position in list(self._shadow_positions.values()):
                if position.symbol != symbol or position.settled_quantity >= position.buy_quantity:
                    continue
                if position.reference_value is None:
                    position.reference_value, position.reference_kind = resolve_reference(
                        getattr(volatility_assessment, "atr", None), previous_close
                    )
                    if position.reference_value is not None:
                        position.data_quality.discard("reference_unavailable")
                for row in position.observe(observed_at, price, quality):
                    reason_code = f"SHADOW_POSITION_{position.position_key}_N{row['checkpoint_minutes']}"
                    self._record_shadow_event_safely(symbol, reason_code, observed_at, row)
        except Exception as exc:
            self._log_trade_reason_once(
                logging.WARNING, "SHADOW_TRACKING_FAILED", symbol,
                f"観測記録をスキップ: 例外={type(exc).__name__}: {exc}",
            )

    def _settle_shadow_positions(
        self,
        symbol: str,
        settled_at: datetime,
        requested_price: float,
        quantity: int,
        settlement_reason: str,
        order_response: dict | None,
    ) -> None:
        if not self.enable_shadow_position_tracking:
            return
        remaining = max(int(quantity), 0)
        settled_at, has_fill_time = self._shadow_execution_time(order_response, settled_at)
        response_price = (order_response or {}).get("Price")
        try:
            settled_price = float(response_price)
            has_fill_price = math.isfinite(settled_price) and settled_price > 0
        except (TypeError, ValueError, OverflowError):
            settled_price = float(requested_price)
            has_fill_price = False
        if not has_fill_price:
            settled_price = float(requested_price)
        if not has_fill_time:
            for position in self._shadow_positions.values():
                if position.symbol == symbol and position.settled_quantity < position.buy_quantity:
                    position.data_quality.add("settlement_time_is_order_acceptance_time")
        if not has_fill_price:
            for position in self._shadow_positions.values():
                if position.symbol == symbol and position.settled_quantity < position.buy_quantity:
                    position.data_quality.add("settlement_price_is_order_reference")
        if self._execution_mode == "live":
            for position in self._shadow_positions.values():
                if position.symbol == symbol and position.settled_quantity < position.buy_quantity:
                    position.data_quality.add("live_settlement_fill_not_confirmed")
        for key, position in sorted(
            list(self._shadow_positions.items()), key=lambda item: item[1].buy_at
        ):
            if position.symbol != symbol or remaining <= 0:
                continue
            open_quantity = position.buy_quantity - position.settled_quantity
            if open_quantity <= 0:
                continue
            settled_quantity = min(remaining, open_quantity)
            row = position.settle(
                settled_at, settled_price, settled_quantity, settlement_reason
            )
            row["settlement_price_source"] = (
                "order_response" if has_fill_price else "order_reference_price"
            )
            row["settlement_time_source"] = (
                "order_response" if has_fill_time else "order_acceptance_time"
            )
            row["settlement_order_id"] = (order_response or {}).get("OrderId")
            reason_code = f"SHADOW_POSITION_{position.position_key}_SETTLEMENT"
            self._record_shadow_event_safely(symbol, reason_code, settled_at, row)
            remaining -= settled_quantity
            if position.settled_quantity >= position.buy_quantity:
                self._shadow_positions.pop(key, None)

    def _note_shadow_capacity_skip(self, symbol: str, occurred_at: datetime) -> None:
        if not self.enable_shadow_position_tracking:
            return
        for position in self._shadow_positions.values():
            if position.settled_quantity < position.buy_quantity:
                try:
                    position.add_blocked_candidate(symbol, occurred_at)
                except Exception as exc:
                    self._log_trade_reason_once(
                        logging.WARNING, "SHADOW_TRACKING_FAILED", position.symbol,
                        f"枠上限候補の紐づけをスキップ: 例外={type(exc).__name__}: {exc}",
                    )

    def _restore_shadow_positions(self, positions: list[dict], now: datetime) -> None:
        if not self.enable_shadow_position_tracking:
            return
        lots_by_symbol: dict[str, list[dict]] = {}
        ordered_history = sorted(self.order_history, key=lambda item: item.timestamp)
        for entry in ordered_history:
            if entry.result_code != 0 or entry.qty <= 0:
                continue
            symbol = str(entry.symbol)
            if entry.side == config.OrderSide.BUY:
                lots_by_symbol.setdefault(symbol, []).append({"entry": entry, "remaining": entry.qty})
            elif entry.side == config.OrderSide.SELL:
                remaining = entry.qty
                for lot in lots_by_symbol.get(symbol, []):
                    matched = min(remaining, lot["remaining"])
                    lot["remaining"] -= matched
                    remaining -= matched
                    if remaining <= 0:
                        break

        open_by_symbol: dict[str, int] = {}
        for position in positions:
            if self._is_open_position(position):
                symbol = str(position.get("Symbol", ""))
                open_by_symbol[symbol] = open_by_symbol.get(symbol, 0) + int(position.get("HoldQty", 0) or 0)

        existing_records = []
        if self.decision_journal_repository is not None:
            try:
                existing_records = self.decision_journal_repository.load_records(
                    now.date().isoformat(), STAGE_TRADING, self._execution_mode
                )
            except Exception as exc:
                for symbol in open_by_symbol:
                    self._log_trade_reason_once(
                        logging.WARNING, "SHADOW_HISTORY_READ_FAILED", symbol,
                        f"既存シャドウ記録の読込失敗: {type(exc).__name__}",
                    )

        details_by_key: dict[str, list[dict]] = {}
        for record in existing_records:
            try:
                detail = json.loads(record.get("detail_json") or "{}")
            except (TypeError, json.JSONDecodeError):
                continue
            if detail.get("position_key"):
                details_by_key.setdefault(detail["position_key"], []).append(detail)

        for symbol, hold_quantity in open_by_symbol.items():
            remaining_holding = hold_quantity
            for lot in lots_by_symbol.get(symbol, []):
                if remaining_holding <= 0 or lot["remaining"] <= 0:
                    continue
                entry = lot["entry"]
                matched = min(remaining_holding, lot["remaining"])
                key = self._shadow_position_key(symbol, entry.order_id, entry.timestamp, entry.qty)
                reference, reference_kind = resolve_reference(entry.atr, None)
                tracker = ShadowPosition(
                    position_key=key, symbol=symbol,
                    buy_at=datetime.fromisoformat(entry.timestamp),
                    buy_price=float(entry.price), buy_quantity=entry.qty,
                    reference_value=reference, reference_kind=reference_kind,
                    history_incomplete=True,
                    settled_quantity=entry.qty - matched,
                )
                tracker.data_quality.add("buy_price_from_order_history")
                for detail in details_by_key.get(key, []):
                    if detail.get("event_type") == "shadow_position_checkpoint":
                        checkpoint = detail.get("checkpoint_minutes")
                        if isinstance(checkpoint, int):
                            tracker.completed_checkpoints.add(checkpoint)
                        tracker.observation_count = max(
                            tracker.observation_count, int(detail.get("observation_count") or 0)
                        )
                for record in existing_records:
                    if record.get("reason_code") != "POSITION_LIMIT_REACHED":
                        continue
                    first_at = str(record.get("first_occurred_at", ""))
                    last_at = str(record.get("last_occurred_at", ""))
                    if not first_at or not last_at or last_at > now.isoformat(timespec="seconds"):
                        continue
                    if last_at < entry.timestamp:
                        continue
                    candidate = str(record.get("symbol", ""))
                    tracker.capacity_blocked_candidates[candidate] = {
                        "count": int(record.get("occurrence_count", 1)),
                        "first_at": first_at,
                        "last_at": last_at,
                        "aggregated": True,
                    }
                    tracker.data_quality.add("capacity_skip_history_aggregated")
                    if first_at < entry.timestamp:
                        tracker.data_quality.add("capacity_skip_period_overlaps_buy_time")
                self._shadow_positions[key] = tracker
                remaining_holding -= matched
            if remaining_holding > 0:
                self._log_trade_reason_once(
                    logging.WARNING, "SHADOW_POSITION_ORIGIN_UNKNOWN", symbol,
                    f"保有数量={hold_quantity}のうち購入履歴と対応できない数量={remaining_holding}",
                )
            return None

    def _log_trade_reason_once(
        self,
        level: int,
        reason_code: str,
        symbol: str,
        detail: str,
    ) -> None:
        key = (symbol, reason_code)
        if key in self._logged_trade_reasons:
            return
        self._logged_trade_reasons.add(key)
        logger.log(level, "%s: 銘柄=%s | %s", reason_code, symbol, detail)

    def _trade_judgment_log_level(self, symbol: str, judgment: str) -> int:
        previous_judgment = self._last_logged_judgments.get(symbol)
        self._last_logged_judgments[symbol] = judgment
        if previous_judgment is None or previous_judgment != judgment:
            return logging.INFO
        return logging.DEBUG

    def _observe_missing_holding_sell_signal(
        self,
        symbol: str,
        occurred_at: datetime,
        missing_holdings: bool,
        cleared_by: str = "売り条件が解消",
    ) -> None:
        """売り条件該当・未保有を状態遷移時だけ記録する。"""
        if missing_holdings:
            if symbol in self._missing_holding_sell_signals:
                return
            same_day_net_buy_quantity = sum(
                entry.qty
                if entry.side == config.OrderSide.BUY
                else -entry.qty
                for entry in self.order_history
                if entry.symbol == symbol
                and entry.timestamp[:10] == occurred_at.date().isoformat()
                and entry.side in (config.OrderSide.BUY, config.OrderSide.SELL)
            )
            has_same_day_net_position = same_day_net_buy_quantity > 0
            if has_same_day_net_position:
                warning_detail = (
                    f" | 当日純買付数量={same_day_net_buy_quantity}株"
                )
            else:
                warning_detail = ""
            self._missing_holding_sell_signals[symbol] = has_same_day_net_position
            logger.log(
                logging.WARNING if has_same_day_net_position else logging.INFO,
                "売り条件該当・未保有: 銘柄=%s | 状態=開始%s",
                symbol,
                warning_detail,
            )
            self._journal_decision(
                symbol,
                "SELL_SIGNAL_WITHOUT_HOLDINGS",
                occurred_at,
                state="started",
                same_day_net_buy_quantity=same_day_net_buy_quantity,
            )
            return

        had_same_day_buy = self._missing_holding_sell_signals.pop(symbol, None)
        if had_same_day_buy is None:
            return
        logger.log(
            logging.WARNING if had_same_day_buy else logging.INFO,
            "売り条件該当・未保有: 銘柄=%s | 状態=解消 | 理由=%s",
            symbol,
            cleared_by,
        )

    def _journal_decision(self, symbol: str, reason_code: str, occurred_at: datetime, **inputs) -> None:
        """銘柄×日付×理由の判断記録を残す。失敗しても売買は止めない。"""
        if self.decision_journal_repository is None:
            return
        try:
            values = {"market_regime": self.market_regime.value, **self._account_snapshot, **inputs}
            self.decision_journal_repository.record(STAGE_TRADING, symbol, reason_code, occurred_at, values)
        except Exception:
            logger.exception("判断記録の保存に失敗しました: 理由=%s 銘柄=%s", reason_code, symbol)
        if reason_code == "POSITION_LIMIT_REACHED":
            self._note_shadow_capacity_skip(symbol, occurred_at)

    def _flush_decision_journal_safely(self) -> None:
        if self.decision_journal_repository is None:
            return
        try:
            self.decision_journal_repository.flush()
        except Exception:
            logger.exception("判断記録の保存に失敗しました")
            for symbol in self._shadow_pending_symbols:
                self._log_trade_reason_once(
                    logging.WARNING, "SHADOW_RECORD_FLUSH_FAILED", symbol,
                    "シャドウ記録を含む判断記録のflushに失敗しました",
                )
        else:
            self._shadow_pending_symbols.clear()

    def _remember_account_state(self, wallet_amount, positions) -> None:
        """判断記録に添える口座状態を、取得済みの値から控える(追加のAPI呼び出しはしない)。"""
        try:
            open_count = self._count_open_positions(positions)
            self._account_snapshot = {
                "wallet_amount": wallet_amount,
                "open_position_count": open_count,
                "target_positions": config.TARGET_POSITIONS,
                "remaining_slots": max(config.TARGET_POSITIONS - open_count, 0),
                "account_observed_at": (self._current_now or datetime.now()).isoformat(timespec="seconds"),
            }
        except Exception:
            self._account_snapshot = {}

    @staticmethod
    def _volatility_inputs(assessment) -> dict:
        return {
            "atr": getattr(assessment, "atr", None),
            "atr_ratio": getattr(assessment, "ratio", None),
            "atr_level": getattr(getattr(assessment, "level", None), "value", None),
        }

    def _record_account_snapshot_safely(self, kind: str, at: datetime, positions: list[dict]) -> None:
        if self.decision_journal_repository is None:
            return
        try:
            holdings = [
                {"symbol": position.get("Symbol"), "quantity": int(position.get("HoldQty", 0) or 0)}
                for position in positions
                if self._is_open_position(position)
            ]
            self.decision_journal_repository.record_account_snapshot(
                kind, at,
                self._account_snapshot.get("wallet_amount"),
                len(holdings), config.TARGET_POSITIONS, holdings,
            )
        except Exception:
            logger.exception("日次口座状態の保存に失敗しました: 種別=%s", kind)

    def _update_filter_decision_observations_safely(
        self, observed_at: datetime, prices_by_symbol: dict[str, float]
    ) -> None:
        try:
            self.filter_decision_repository.update_open_event_observations(
                observed_at, prices_by_symbol, self._execution_mode
            )
        except Exception:
            logger.exception("判定イベントの観測更新に失敗しました")

    def _finalize_filter_decisions_safely(self, as_of: datetime) -> None:
        try:
            self.filter_decision_repository.finalize_due_events(
                as_of, config.FILTER_DECISION_OBSERVATION_DAYS, self._execution_mode
            )
        except Exception:
            logger.exception("判定イベントの確定に失敗しました")

    def _finalize_same_day_filter_decisions_safely(self, as_of: datetime, include_today: bool) -> None:
        """当日基準を確定する。include_today=Falseなら再起動で取り残された前日以前の分だけ。"""
        if config.ALLOW_OVERNIGHT_HOLDING:
            session_end = clock_time(config.MARKET_CLOSE_HOUR, config.MARKET_CLOSE_MINUTE)
        else:
            session_end = clock_time(config.MARKET_LIQUIDATION_HOUR, config.MARKET_LIQUIDATION_MINUTE)
        try:
            self.filter_decision_repository.finalize_same_day_events(
                as_of, self._execution_mode, include_today, session_end, config.LOOP_INTERVAL
            )
        except Exception:
            logger.exception("判定イベントの当日確定に失敗しました")

    def _mark_board_unavailable_safely(self, symbol: str, observed_at: datetime) -> None:
        try:
            self.filter_decision_repository.mark_same_day_board_unavailable(
                symbol, observed_at, self._execution_mode
            )
        except Exception:
            logger.exception("判定イベントの板取得不能記録に失敗しました: 銘柄=%s", symbol)

    def _rsi_input_snapshot(
        self,
        symbol: str,
        occurred_at: datetime,
        closes: list[float] | None,
        rsi: float | None,
        applied_threshold: float,
        current_price: float | None,
        outcome: str | None = None,
        threshold_kind: str = "entry",
    ) -> dict:
        close_count = len(closes) if closes is not None else 0
        required_closes = max(5, config.RSI_MINIMUM_CLOSES, config.RSI_PERIOD + 1)
        if rsi is not None:
            unavailable_reason = None
            calculation_status = "available"
            passed = rsi <= applied_threshold if threshold_kind == "exit" else rsi >= applied_threshold
            inferred_outcome = "passed" if passed else "below_threshold"
        else:
            calculation_status = "unavailable"
            inferred_outcome = "unavailable"
            if close_count < required_closes:
                unavailable_reason = "insufficient_close_history"
            elif closes and any(value <= 0 for value in closes):
                unavailable_reason = "non_positive_close"
            else:
                unavailable_reason = "rsi_calculation_returned_none"
        return {
            "decision_at": occurred_at.isoformat(),
            "symbol": symbol,
            "market_regime": self.market_regime.value,
            "rsi": rsi,
            "applied_threshold": applied_threshold,
            "threshold_kind": threshold_kind,
            "calculation_status": calculation_status,
            "unavailable_reason": unavailable_reason,
            "close_count": close_count,
            "required_close_count": required_closes,
            "last_close": closes[-1] if closes else None,
            "current_price": current_price,
            "outcome": outcome or inferred_outcome,
        }

    def _log_rsi_unavailable(
        self,
        symbol: str,
        occurred_at: datetime,
        closes: list[float] | None,
        rsi: float | None,
        applied_threshold: float,
        current_price: float | None,
    ) -> None:
        required_closes = max(5, config.RSI_MINIMUM_CLOSES, config.RSI_PERIOD + 1)
        actual_closes = len(closes) if closes is not None else 0
        if actual_closes < required_closes:
            reason_code = "INSUFFICIENT_RSI_HISTORY"
            reason = "insufficient_close_history"
        elif rsi is None:
            reason_code = "RSI_DATA_UNAVAILABLE"
            reason = (
                "non_positive_close"
                if closes and any(value <= 0 for value in closes)
                else "invalid_or_unavailable_rsi_input"
            )
        else:
            return
        rsi_input = self._rsi_input_snapshot(
            symbol, occurred_at, closes, rsi, applied_threshold, current_price, "unavailable"
        )
        rsi_input["unavailable_reason"] = reason
        self._journal_decision(
            symbol, reason_code, occurred_at,
            rsi=rsi, rsi_applied_threshold=applied_threshold,
            rsi_required_closes=required_closes, rsi_actual_closes=actual_closes,
            current_price=current_price, unavailable_reason=reason,
        )
        detail = (
            f"必要本数={required_closes} | 実績本数={actual_closes} | "
            f"RSI_INPUT={json.dumps(rsi_input, ensure_ascii=False, sort_keys=True)}"
        )
        self._log_trade_reason_once(logging.WARNING, reason_code, symbol, detail)

    def _market_regime_event_inputs(
        self, rsi, entry_threshold: float, assessment=None, rsi_input: dict | None = None
    ) -> dict:
        market_assessment = self.market_regime_assessment
        inputs = {
            "atr": getattr(assessment, "atr", None),
            "true_range": getattr(assessment, "latest_true_range", None),
            "atr_ratio": getattr(assessment, "ratio", None),
            "atr_level": getattr(getattr(assessment, "level", None), "value", None),
            "market_regime": self.market_regime.value,
            "realized_volatility_percent": getattr(market_assessment, "realized_volatility_percent", None),
            "vix": getattr(market_assessment, "vix", None),
            "nikkei_change_percent": getattr(market_assessment, "nikkei_change_percent", None),
            "adx": getattr(market_assessment, "adx", None),
            "rsi": rsi,
            "rsi_normal_threshold": config.RSI_ENTRY_THRESHOLD,
            "rsi_applied_threshold": entry_threshold,
        }
        if rsi_input is not None:
            inputs["rsi_input"] = rsi_input
        return inputs

    def _filter_decision_summaries(self, event_type: str, occurred_on: date | None = None) -> list[dict]:
        try:
            return [
                item for item in self.filter_decision_repository.load_summaries(
                    execution_mode=self._execution_mode
                )
                if item["event_type"] == event_type
                and (occurred_on is None or item["occurred_at"][:10] == occurred_on.isoformat())
            ]
        except Exception:
            logger.exception("判定イベントのサマリー取得に失敗しました: 種別=%s", event_type)
            return []

    def _notify_safely(self, message: str) -> None:
        """通知失敗で取引制御自体を妨げないように通知します。"""
        if self.notifier:
            try:
                self.notifier(message)
            except Exception:
                logger.exception("緊急通知の送信に失敗しました")
            return
        notify_critical(message)

    def _trigger_kill_switch(self, reason: str) -> None:
        """キルスイッチを一度だけ発動し、即時通知します。"""
        if self.kill_switch_triggered:
            return
        self.kill_switch_triggered = True
        logger.warning("キルスイッチを発動しました: %s", reason)
        self._notify_safely(f"【緊急停止】キルスイッチを発動しました: {reason}")

    def _is_emergency_stop_requested(self) -> bool:
        return config.EMERGENCY_STOP_FILE.exists()

    def _holdings_lines(self) -> list[str]:
        """通知に添える保有銘柄一覧（決済待ちの建玉のみ）。"""
        held = [p for p in self.last_positions if self._is_open_position(p)]
        if not held:
            return ["保有銘柄: なし"]
        return [
            f"保有銘柄: {p.get('Symbol', '')} {int(p.get('HoldQty', 0) or 0)}株"
            for p in held
        ]

    def _refresh_holdings_for_notification(self) -> None:
        """通知用に保有銘柄を極力最新化する。取得できない場合は既存のlast_positionsを使う。"""
        try:
            _, positions = self._load_account_state()
            if positions:
                self.last_positions = positions
        except Exception:
            logger.exception("通知用の保有銘柄取得に失敗しました")

    def _load_positions_for_progress_report(self) -> list[dict] | None:
        try:
            if self.positions_client:
                positions = self.positions_client.get_positions(self.token)
            elif self.order_sender and hasattr(self.order_sender, "get_positions"):
                positions = self.order_sender.get_positions(self.token)
            else:
                positions = get_positions(self.token)
            if positions is None:
                return None
            self.last_positions = positions
            return [
                {
                    "symbol": position.get("Symbol", ""),
                    "quantity": int(position.get("HoldQty", 0) or 0),
                    "profit_loss": self._optional_number(position.get("ProfitLoss")),
                }
                for position in positions
                if self._is_open_position(position)
            ]
        except Exception:
            logger.exception("中間報告用の保有株取得に失敗しました")
            return None

    @staticmethod
    def _optional_number(value) -> float | None:
        try:
            number = float(value)
            return number if math.isfinite(number) else None
        except (TypeError, ValueError, OverflowError):
            return None

    def _progress_report_events(self, as_of: datetime) -> list[str]:
        events = []
        cutoff = as_of.replace(tzinfo=None).isoformat(timespec="seconds")
        event_labels = {
            "ATR_DANGER_SKIP": "ATR DANGERによる新規買い見送り",
            "ATR_STOP_EXIT": "ATR損切り",
            "MARKET_REGIME_DANGER_SKIP": "MarketRegime DANGERによる新規買い見送り",
            "MARKET_REGIME_CAUTION_RSI_FILTER": "CAUTION時のRSI条件による新規買い見送り",
        }
        try:
            summaries = self.filter_decision_repository.load_summaries(
                start=as_of.date(), end=as_of.date(), execution_mode=self._execution_mode
            )
            for item in summaries:
                occurred_at = str(item.get("occurred_at", ""))[:19]
                label = event_labels.get(item.get("event_type"))
                if label and occurred_at <= cutoff:
                    events.append(f"{label}: {item.get('symbol', '')}")
        except Exception:
            logger.exception("中間報告用の判定イベント取得に失敗しました")

        if self.decision_journal_repository is not None:
            reason_labels = {
                "NEW_BUY_HALTED_STATE_SAVE_FAILURE": "状態保存失敗による新規買い停止",
                "POSITION_LIMIT_REACHED": "保有上限による新規買い見送り",
                "WALLET_UNKNOWN": "買付余力不明による新規買い見送り",
                "ORDER_AMOUNT_LIMIT_EXCEEDED": "注文金額上限による新規買い見送り",
                "ORDER_SAFETY_BLOCKED": "安全条件による注文見送り",
                "SELL_SIGNAL_WITHOUT_HOLDINGS": "売り条件該当・未保有",
                "caution_rounding_to_zero": "CAUTION数量調整による新規買い見送り",
                "atr_quantity_adjustment_zero": "ATR数量調整による新規買い見送り",
            }
            try:
                records = self.decision_journal_repository.load_records(
                    as_of.date().isoformat(), STAGE_TRADING, self._execution_mode
                )
                for record in records:
                    occurred_at = str(record.get("last_occurred_at", ""))[:19]
                    label = reason_labels.get(record.get("reason_code"))
                    if label and occurred_at <= cutoff:
                        count = int(record.get("occurrence_count", 1))
                        events.append(
                            f"{label}: {record.get('symbol', '')} ({count}回)"
                        )
            except Exception:
                logger.exception("中間報告用の判断記録取得に失敗しました")
        return events

    def _send_trading_progress_report(self, as_of: datetime, scheduled_time: str) -> None:
        today = as_of.date().isoformat()
        orders = [
            entry for entry in self.order_history
            if entry.timestamp[:10] == today
            and entry.timestamp[:19] <= as_of.replace(tzinfo=None).isoformat(timespec="seconds")
        ]
        realized_pnl = None
        if self.order_sender and hasattr(self.order_sender, "get_daily_realized_pnl"):
            try:
                realized_pnl = self._optional_number(self.order_sender.get_daily_realized_pnl())
            except Exception:
                logger.exception("中間報告用の確定損益取得に失敗しました")
        holdings = self._load_positions_for_progress_report()

        nikkei_change = None
        market_client = getattr(self.market_regime_usecase, "market_data_client", None)
        getter = getattr(market_client, "get_intraday_change_percent", None)
        if callable(getter):
            try:
                nikkei_change = getter("^N225", as_of)
            except Exception:
                logger.exception("中間報告用の日経225値取得に失敗しました")

        report = build_trading_progress_report(
            reported_at=as_of,
            scheduled_time=scheduled_time,
            order_count=len(orders),
            order_count_label=(
                "約定件数" if self._is_paper_mode()
                else "注文受付件数（実約定未照会）"
            ),
            realized_pnl=realized_pnl,
            holdings=holdings,
            market_regime=self.market_regime.value,
            nikkei_change_percent=nikkei_change,
            events=self._progress_report_events(as_of),
            kill_switch_triggered=self.kill_switch_triggered,
            emergency_stop_triggered=self.emergency_stop_triggered,
        )
        try:
            if self.notifier:
                self.notifier(report)
            else:
                notify_daily(report)
        except Exception:
            logger.exception("中間報告の通知に失敗しました")

    def _send_due_progress_reports(self, previous: datetime, current: datetime) -> None:
        if self._progress_report_start_time is None:
            return
        schedules = (
            (config.TRADING_PROGRESS_REPORT_1_HOUR, config.TRADING_PROGRESS_REPORT_1_MINUTE),
            (config.TRADING_PROGRESS_REPORT_2_HOUR, config.TRADING_PROGRESS_REPORT_2_MINUTE),
        )
        for hour, minute in schedules:
            label = f"{hour:02d}:{minute:02d}"
            if label in self._progress_reports_sent:
                continue
            scheduled = current.replace(hour=hour, minute=minute, second=0, microsecond=0)
            started_after_schedule = self._progress_report_start_time.replace(tzinfo=None) > scheduled.replace(tzinfo=None)
            crossed_schedule = previous.replace(tzinfo=None) < scheduled.replace(tzinfo=None) <= current.replace(tzinfo=None)
            started_at_schedule = (
                previous.replace(tzinfo=None) == scheduled.replace(tzinfo=None)
                and current.replace(tzinfo=None) == scheduled.replace(tzinfo=None)
            )
            if not started_after_schedule and (crossed_schedule or started_at_schedule):
                self._progress_reports_sent.add(label)
                self._send_trading_progress_report(current, label)

    def _check_auth_recovery_notification(self) -> None:
        """トークン再取得後も401が続いた(復旧失敗)場合、1runにつき1回だけ通知する。"""
        from src.infrastructure.kabu.token_provider import get_token_provider

        if self._auth_recovery_notified or not get_token_provider().recovery_failed:
            return
        self._auth_recovery_notified = True
        self._refresh_holdings_for_notification()
        message = "\n".join([
            "【業務】取引運用",
            "【機能】API認証",
            "【概要】",
            "kabuステーションAPIの認証が回復しません（トークン再取得後も401が継続しています）。",
            "【詳細】",
            *self._holdings_lines(),
        ])
        self._notify_safely(message)

    def _check_board_fetch_failure_notification(self, attempted: int, failed: int) -> None:
        """対象銘柄全件の板取得が連続N回失敗した場合、1runにつき1回だけ通知する。"""
        if attempted == 0:
            return
        if failed < attempted:
            self._board_fetch_all_failed_streak = 0
            return
        self._board_fetch_all_failed_streak += 1
        if (
            self._board_fetch_failure_notified
            or self._board_fetch_all_failed_streak < config.BOARD_FETCH_CONSECUTIVE_FAILURE_THRESHOLD
        ):
            return
        self._board_fetch_failure_notified = True
        self._refresh_holdings_for_notification()
        message = "\n".join([
            "【業務】取引運用",
            "【機能】板情報取得",
            "【概要】",
            f"対象銘柄全件の板取得が{self._board_fetch_all_failed_streak}回連続で失敗しました。",
            "【詳細】",
            *self._holdings_lines(),
        ])
        self._notify_safely(message)

    # ================================================================================
    # 注文履歴の管理
    # ================================================================================

    def _load_order_history(self) -> None:
        """注文履歴ファイルから過去の注文を読み込みます。"""
        if not self.order_history_path.exists():
            data = []
        else:
            try:
                data = json.loads(self.order_history_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise ValueError(f"注文履歴ファイルの読み込みに失敗しました: {exc}") from exc
        if not isinstance(data, list):
            message = f"注文履歴の検証に失敗しました: 行データは配列ではありません: {data!r}"
            logger.critical(message)
            self._notify_safely(message)
            raise ValueError(message)

        loaded_entries = []
        invalid_rows = []
        for row_index, item in enumerate(data):
            symbol = item.get("symbol", "<欠落>") if isinstance(item, dict) else "<判定不能>"
            problems = []
            if not isinstance(item, dict):
                problems.append(f"行データがobjectではありません: {item!r}")
            else:
                for field_name in ("symbol", "side", "price", "qty", "timestamp"):
                    if field_name not in item:
                        problems.append(f"{field_name}=<欠落>")
                if "symbol" in item and not isinstance(item["symbol"], str):
                    problems.append(f"symbolの型が不正: {item['symbol']!r}")
                if "side" in item:
                    try:
                        config.OrderSide(item["side"])
                    except (TypeError, ValueError):
                        problems.append(f"sideが不正: {item['side']!r}")
                if "price" in item and (
                    isinstance(item["price"], bool) or not isinstance(item["price"], (int, float))
                ):
                    problems.append(f"priceの型が不正: {item['price']!r}")
                if "qty" in item and (isinstance(item["qty"], bool) or not isinstance(item["qty"], int)):
                    problems.append(f"qtyの型が不正: {item['qty']!r}")
                if "timestamp" in item:
                    timestamp = item["timestamp"]
                    if not isinstance(timestamp, str):
                        problems.append(f"timestampの型が不正: {timestamp!r}")
                    else:
                        try:
                            datetime.fromisoformat(timestamp)
                        except ValueError:
                            problems.append(f"timestampを解釈できません: {timestamp!r}")

            if problems:
                invalid_rows.append(
                    f"行={row_index} 銘柄={symbol!r} | " + "; ".join(problems)
                )
                continue
            try:
                loaded_entries.append(OrderHistoryEntry.from_dict(item))
            except (KeyError, TypeError, ValueError) as exc:
                invalid_rows.append(
                    f"行={row_index} 銘柄={symbol!r} | 行データが不正: {exc}"
                )

        if invalid_rows:
            shown_rows = invalid_rows[:10]
            suffix = f"\nほか{len(invalid_rows) - len(shown_rows)}行" if len(invalid_rows) > len(shown_rows) else ""
            message = (
                "注文履歴の検証に失敗しました（行番号は0始まり）: "
                f"不正行数={len(invalid_rows)}\n" + "\n".join(shown_rows) + suffix
            )
            logger.critical(message)
            self._notify_safely(message)
            raise ValueError(message)

        self.order_history = loaded_entries

    def _save_order_history(self) -> bool:
        """現在の注文履歴をファイルに保存します。失敗を記録し、メモリ上の履歴は維持します。"""
        saved = write_json(self.order_history_path, [entry.to_dict() for entry in self.order_history])
        self._order_history_save_failures = 0 if saved else self._order_history_save_failures + 1
        self._check_state_save_failure_notification()
        return saved

    def _state_save_failure_counts(self) -> dict[str, tuple[str, int]]:
        """保存対象ごとの(ファイル名, 連続失敗回数)。"""
        counts = {
            "order_history": (self.order_history_path.name, self._order_history_save_failures),
            "kill_switch_baseline": (
                self.kill_switch_baseline_path.name, self._kill_switch_baseline_save_failures,
            ),
        }
        state_path = getattr(self.order_sender, "state_path", None)
        if state_path is not None:
            counts["paper_state"] = (
                Path(state_path).name,
                int(getattr(self.order_sender, "consecutive_save_failures", 0) or 0),
            )
        return counts

    def _check_state_save_failure_notification(self) -> None:
        """保存失敗の連続を初回失敗時にcritical通知する。成功でストリークをリセットする。"""
        for source, (filename, failures) in self._state_save_failure_counts().items():
            if failures == 0:
                self._save_failure_notified_sources.discard(source)
                continue
            logger.error(
                "STATE_SAVE_FAILURE: ファイル=%s | 連続失敗=%d回", filename, failures
            )
            if source in self._save_failure_notified_sources:
                continue
            self._save_failure_notified_sources.add(source)
            self._notify_safely("\n".join([
                "【業務】取引運用",
                "【機能】状態ファイル保存",
                "【概要】",
                "状態ファイルの保存に失敗しました。処理は続行し、次の保存機会に再試行します。",
                "【詳細】",
                f"ファイル: {filename}",
                f"連続失敗回数: {failures}",
                "例外: OSError(詳細はログを確認してください)",
            ]))

    def _is_new_buy_halted_by_save_failure(self) -> bool:
        """保存失敗が連続して閾値に達していれば、再保存を試してなお失敗中かを返す。"""
        threshold = config.STATE_SAVE_CONSECUTIVE_FAILURE_THRESHOLD
        if not any(failures >= threshold for _, failures in self._state_save_failure_counts().values()):
            self._new_buy_halt_notified = False
            return False
        # 買いが止まっている間は注文がなく保存機会もないため、ここで再保存して解除を可能にする
        if self._order_history_save_failures >= threshold:
            self._save_order_history()
        if self._kill_switch_baseline_save_failures >= threshold:
            self._retry_kill_switch_baseline_save()
        retry = getattr(self.order_sender, "retry_save_state", None)
        if callable(retry) and getattr(self.order_sender, "consecutive_save_failures", 0) >= threshold:
            retry()
            self._check_state_save_failure_notification()
        halted = any(failures >= threshold for _, failures in self._state_save_failure_counts().values())
        if not halted:
            self._new_buy_halt_notified = False
        return halted

    def prepare_market_regime(self):
        """取引開始前に市場レジームを取得し、通知・レポートで再利用します。"""
        if self.market_regime_usecase is not None and self.market_regime_assessment is None:
            self.market_regime_assessment = self.market_regime_usecase.execute()
            self.market_regime = self.market_regime_assessment.regime
            self._record_market_regime_daily_safely()
        return self.market_regime_assessment

    def _record_market_regime_daily_safely(self) -> None:
        """検証用に判定結果を日次で保存します。失敗しても売買は止めません。"""
        repository = self.market_regime_daily_repository
        if repository is None or self.market_regime_assessment is None:
            return
        try:
            regime_usecase = self.market_regime_usecase
            thresholds = getattr(regime_usecase, "thresholds", None)
            threshold_values = dict(vars(thresholds)) if thresholds is not None else {}
            threshold_values.update({
                "adx_trend_threshold": getattr(regime_usecase, "adx_threshold", None),
                "realized_volatility_window": getattr(regime_usecase, "realized_volatility_window", None),
                "data_range": getattr(regime_usecase, "data_range", None),
            })
            repository.save(
                self.market_regime_assessment,
                self._now_provider(),
                self._execution_mode,
                threshold_values,
            )
        except Exception:
            logger.exception("市場レジームの日次記録に失敗しました")

    def market_conditions_summary(self) -> str:
        """通知向けに市場状況を短く整形します。"""
        assessment = self.market_regime_assessment
        if assessment is None or not getattr(assessment, "data_available", False):
            reason = getattr(assessment, "failure_reason", None) or "データ未取得"
            return f"MarketRegime={self.market_regime.value} | 判定データなし: {reason}"

        def format_metric(value, suffix=""):
            return f"{value:.2f}{suffix}" if value is not None else "-"

        return (
            f"MarketRegime={self.market_regime.value} | "
            f"日経前日比={format_metric(getattr(assessment, 'nikkei_change_percent', None), '%')} | "
            f"実現ボラ={format_metric(getattr(assessment, 'realized_volatility_percent', None), '%')} | "
            f"VIX={format_metric(getattr(assessment, 'vix', None))} | "
            f"ADX={format_metric(getattr(assessment, 'adx', None))}"
        )

    def market_conditions_detail(self) -> list[str]:
        """市場指標の値と既存状態ラベルを通知用に整形します。"""
        assessment = self.market_regime_assessment
        if assessment is None or not getattr(assessment, "data_available", False):
            reason = getattr(assessment, "failure_reason", None) or "データ未取得"
            return [f"MarketRegime: {self.market_regime.value}", f"現在の状態: 判定不能（{reason}）"]

        realized_volatility = getattr(assessment, "realized_volatility_percent", None)
        vix = getattr(assessment, "vix", None)
        nikkei_change = getattr(assessment, "nikkei_change_percent", None)
        adx = getattr(assessment, "adx", None)
        if any(value is None for value in (realized_volatility, vix, nikkei_change, adx)):
            return [self.market_conditions_summary(), "補足: 一部の市場指標を取得できませんでした。"]
        thresholds = config.MARKET_REGIME_THRESHOLDS
        lines = [
            "MarketRegime: " + self.market_regime.value + "（" + {
                MarketRegime.NORMAL: "通常",
                MarketRegime.CAUTION: "やや警戒",
                MarketRegime.DANGER: "危険・新規買い停止",
            }[self.market_regime] + "）",
            f"日経前日比: {nikkei_change:.2f}%（状態: {'上昇' if nikkei_change >= 0 else '下落'}）",
            f"実現ボラティリティ: {realized_volatility:.2f}%（状態: {'危険' if realized_volatility >= thresholds.realized_vol_danger else '注意' if realized_volatility >= thresholds.realized_vol_caution else '通常'}）",
            f"VIX: {vix:.2f}（状態: {'危険' if vix >= thresholds.vix_danger else '注意' if vix >= thresholds.vix_caution else '通常'}）",
            f"ADX: {adx:.2f}（状態: {'強いトレンド' if adx >= config.MARKET_REGIME_ADX_TREND_THRESHOLD else '強いトレンドなし'}）",
        ]
        return lines

    @staticmethod
    def _order_detail_lines(entry: OrderHistoryEntry, include_market_regime: bool = True) -> list[str]:
        """注文履歴を判断理由と用語補足付きの通知行へ変換します。"""
        lines = [
            f"銘柄: {entry.symbol}",
            f"売買: {'買い' if entry.side == config.OrderSide.BUY else '売り'}",
            f"約定価格: {entry.price:.1f}円",
            f"数量: {entry.qty}株",
            f"判断理由: {entry.decision_reason or '注文条件成立'}",
        ]
        if entry.side == config.OrderSide.BUY and entry.allocated_budget is not None:
            lines.append(f"割当予算: {entry.allocated_budget:,.0f}円")
        if entry.rsi is not None:
            if entry.side == config.OrderSide.BUY and entry.rsi_entry_threshold is not None:
                state = "買い基準以上" if entry.rsi >= entry.rsi_entry_threshold else "買い基準未満"
            elif entry.side == config.OrderSide.SELL and entry.rsi_exit_threshold is not None:
                state = "決済基準以下" if entry.rsi <= entry.rsi_exit_threshold else "決済基準超"
            else:
                state = "判定材料"
            lines.append(f"RSI: {entry.rsi:.2f}（意味: 値動きの勢い / 状態: {state}）")
        else:
            lines.append("RSI: データなし")
        if entry.rsi_entry_threshold is not None:
            lines.append(f"RSI買い基準: {entry.rsi_entry_threshold:.2f}")
        if entry.rsi_exit_threshold is not None:
            lines.append(f"RSI決済基準: {entry.rsi_exit_threshold:.2f}")
        if entry.current_price is not None:
            lines.append(f"現在価格: {entry.current_price:.1f}円")
        if entry.basis_lower_band is not None and entry.basis_upper_band is not None:
            lines.append(
                f"価格基準: 下側バンド={entry.basis_lower_band:.1f}円 / "
                f"上側バンド={entry.basis_upper_band:.1f}円"
            )
        if include_market_regime and entry.market_regime:
            regime_state = {
                "NORMAL": "通常",
                "CAUTION": "やや警戒",
                "DANGER": "危険",
            }.get(entry.market_regime, "不明")
            lines.append(f"MarketRegime: {entry.market_regime}（意味: 市場全体の警戒度 / 状態: {regime_state}）")
        if entry.atr is not None:
            atr_ratio = entry.atr_ratio or 0.0
            atr_state = "危険" if atr_ratio >= config.ATR_DANGER_RATIO else "注意" if atr_ratio >= config.ATR_CAUTION_RATIO else "通常"
            lines.extend([
                f"ATR: {entry.atr:.3f}円（意味: 通常の値動き幅 / 状態: {atr_state}）",
                f"TR: {entry.atr_true_range:.3f}円（意味: 直近の実際の値幅）",
                f"ATR比率: {atr_ratio:.2f}（意味: 直近値幅÷ATR / 状態: {'通常より大きい' if atr_ratio >= 1 else '通常より小さい'}）",
                f"ATRレベル: {entry.atr_level}",
            ])
            if entry.atr_stop_multiplier is not None:
                lines.append(f"ATR損切り倍率: {entry.atr_stop_multiplier:.2f}倍")
        else:
            lines.append("ATR: データなし")
        if entry.order_qty_before_atr is not None:
            lines.append(f"ATR数量調整: {entry.order_qty_before_atr}株 → {entry.qty}株")
        return lines

    def _register_order(
        self,
        signal: TradeSignal,
        limit: PriceLimit,
        order_response: Optional[dict],
        volatility_assessment=None,
        diagnostics: Optional[dict] = None,
        shadow_previous_close: float | None = None,
    ) -> None:
        """
        実行した注文を履歴に記録します。
        
        Args:
            signal: 実行した取引シグナル
            limit: 発注根拠となった価格基準値
            order_response: kabu APIの発注応答（監査ログ用）
        """
        order_diagnostics = {
            "market_regime": self.market_regime.value,
        }
        order_diagnostics.update(diagnostics or {})
        if volatility_assessment is not None:
            order_diagnostics.update({
                "atr": volatility_assessment.atr,
                "atr_true_range": volatility_assessment.latest_true_range,
                "atr_ratio": volatility_assessment.ratio,
                "atr_level": volatility_assessment.level.value,
                "atr_stop_multiplier": stop_loss_multiplier(
                    volatility_assessment.level,
                    config.ATR_STOP_NORMAL_MULTIPLIER,
                    config.ATR_STOP_CAUTION_MULTIPLIER,
                    config.ATR_STOP_DANGER_MULTIPLIER,
                ),
            })
        self.order_history.append(
            signal.to_order_history_entry(
                limit,
                order_response,
                order_diagnostics,
                timestamp=self._current_now or self._now_provider(),
            )
        )
        self._save_order_history()
        entry = self.order_history[-1]
        # ADR-0002: 約定成立時に保有中最高値を初期化(買い)・クリア(売り)する。
        # 通常決済・ATR損切り・大引けの強制決済(_liquidate_all_positions)は
        # いずれもこのメソッドを経由するため、ここ1箇所で両方をカバーできる。
        if signal.side == config.OrderSide.BUY:
            self._holding_high_prices[signal.symbol] = signal.price
            try:
                self._start_shadow_position(
                    entry, order_response, volatility_assessment, shadow_previous_close
                )
            except Exception as exc:
                self._log_trade_reason_once(
                    logging.WARNING, "SHADOW_TRACKING_FAILED", signal.symbol,
                    f"BUY追跡開始をスキップ: 例外={type(exc).__name__}: {exc}",
                )
        elif signal.side == config.OrderSide.SELL:
            self._holding_high_prices.pop(signal.symbol, None)
            try:
                self._settle_shadow_positions(
                    signal.symbol,
                    self._current_now or datetime.fromisoformat(entry.timestamp),
                    signal.price,
                    signal.qty,
                    (diagnostics or {}).get("decision_reason") or "sell_order_accepted",
                    order_response,
                )
            except Exception as exc:
                self._log_trade_reason_once(
                    logging.WARNING, "SHADOW_TRACKING_FAILED", signal.symbol,
                    f"SELL追跡完了をスキップ: 例外={type(exc).__name__}: {exc}",
                )
        side_label = "買い" if signal.side == config.OrderSide.BUY else "売り"
        message_lines = [
            "【業務】取引運用",
            "【機能】注文執行",
            "【概要】",
            f"{side_label}注文が成立しました。",
            "【詳細】",
            *self._order_detail_lines(entry),
        ]
        message = "\n".join(message_lines)
        if self.notifier:
            try:
                self.notifier(message)
            except Exception:
                logger.exception("注文約定通知の送信に失敗しました: 銘柄=%s", signal.symbol)
            return
        notify_critical(message)

    def _calculate_daily_pnl(self, positions: List[dict]) -> float:
        """実現損益と保有中の評価損益を合算します。"""
        unrealized_pnl = sum(float(position.get('ProfitLoss', 0) or 0) for position in positions)
        realized_pnl = 0.0
        if self.order_sender and hasattr(self.order_sender, 'get_daily_realized_pnl'):
            realized_pnl = float(self.order_sender.get_daily_realized_pnl() or 0)
        else:
            realized_pnl = sum(
                float(
                    position.get(
                        'RealizedProfitLoss',
                        position.get('RealizedPnL', position.get('RealizedProfitLossAmount', 0)),
                    )
                    or 0
                )
                for position in positions
            )
        return unrealized_pnl + realized_pnl

    def _calculate_total_equity(self, wallet_amount: float, positions: List[dict]) -> float:
        """現金(買付余力)と保有ポジション評価額を合算した総資産を計算します。"""
        position_value = sum(
            float(position.get('HoldQty', 0) or 0) * float(position.get('CurrentPrice', 0) or 0)
            for position in positions
        )
        return float(wallet_amount or 0) + position_value

    def _initialize_paper_prices(self, preflight_market_data=None) -> None:
        """ペーパートレードの保有銘柄に再起動直後の現在値を設定します。"""
        if not self.order_sender or not hasattr(self.order_sender, "holdings"):
            return
        holdings = getattr(self.order_sender, "holdings", {})
        set_price = getattr(self.order_sender, "set_price", None)
        if not holdings or not callable(set_price):
            return

        for symbol in holdings:
            snapshot = preflight_market_data.get(symbol) if preflight_market_data else None
            board = snapshot.get("board") if snapshot else (
                self.board_client.get_current_board(self.token, symbol)
                if self.board_client else get_current_board(self.token, symbol)
            )
            current_price = board.get("current_price") if board else None
            if current_price is not None:
                set_price(symbol, float(current_price))

    def _initialize_daily_starting_capital(self, today, initial_wallet_amount, initial_positions) -> None:
        """当日の初回起動時だけキルスイッチ基準資本を保存します。"""
        baseline = read_json_strict(self.kill_switch_baseline_path)
        if baseline is not None:
            try:
                baseline_date = date.fromisoformat(baseline["date"])
                baseline_capital = float(baseline["capital"])
                if not math.isfinite(baseline_capital) or baseline_capital <= 0:
                    raise ValueError("capital must be a finite positive number")
            except (KeyError, TypeError, ValueError, OverflowError) as exc:
                raise StateFileCorruptError(
                    self.kill_switch_baseline_path,
                    f"値が不正です({type(exc).__name__}: {exc})",
                    preserve_corrupt_copy(self.kill_switch_baseline_path),
                ) from exc

            if baseline_date.isoformat() == today:
                self.daily_starting_capital = baseline_capital
                return

        computed_capital = self._calculate_total_equity(initial_wallet_amount, initial_positions)
        self.daily_starting_capital = (
            computed_capital if computed_capital > 0 else config.OPERATING_CAPITAL
        )
        self._kill_switch_baseline_unsaved = {"date": today, "capital": self.daily_starting_capital}
        self._retry_kill_switch_baseline_save()

    def _retry_kill_switch_baseline_save(self) -> None:
        """未保存のキルスイッチ基準資本を保存する。失敗してもメモリ上の基準を使い続ける。"""
        payload = self._kill_switch_baseline_unsaved
        if payload is None:
            return
        if write_json(self.kill_switch_baseline_path, payload):
            self._kill_switch_baseline_unsaved = None
            self._kill_switch_baseline_save_failures = 0
        else:
            self._kill_switch_baseline_save_failures += 1
        self._check_state_save_failure_notification()

    # ================================================================================
    # 口座状態の取得
    # ================================================================================

    def _load_account_state(self) -> tuple[Optional[float], List[dict]]:
        """
        現在の口座状態（資金・保有株）を取得します。
        
        Returns:
            タプル: (買付可能額, 保有株リスト)
        """
        # テスト用の注入クライアント、またはデフォルトのインフラストラクチャ実装を使用
        if self.wallet_client:
            wallet = self.wallet_client.get_wallet_cash(self.token)
        elif self.order_sender and hasattr(self.order_sender, 'get_wallet_cash'):
            wallet = self.order_sender.get_wallet_cash(self.token)
        else:
            wallet = get_wallet_cash(self.token)

        if self.positions_client:
            positions = self.positions_client.get_positions(self.token) or []
        elif self.order_sender and hasattr(self.order_sender, 'get_positions'):
            positions = self.order_sender.get_positions(self.token) or []
        else:
            positions = get_positions(self.token) or []
        wallet_amount = None
        if wallet is not None:
            wallet_amount = wallet.get('StockAccountWallet')
        self._remember_account_state(wallet_amount, positions)
        return wallet_amount, positions

    @staticmethod
    def _is_open_position(position: dict) -> bool:
        """信用売り建て決済待ちのポジションかどうかを判定する。"""
        return (
            position.get('Side') == config.OrderSide.SELL.value
            and int(position.get('HoldQty', 0) or 0) > 0
        )

    def _count_open_positions(self, positions: List[dict]) -> int:
        """信用売り建て決済待ち＝保有中とみなすポジション数を数える。"""
        return sum(self._is_open_position(position) for position in positions)

    def _has_holdings(self, symbol: str, positions: List[dict]) -> bool:
        """
        指定銘柄の保有株があるかどうかを確認します。
        
        Args:
            symbol: 銘柄シンボル
            positions: 保有株リスト
            
        Returns:
            保有している場合True、していない場合False
        """
        return any(
            pos.get('Symbol') == symbol and self._is_open_position(pos)
            for pos in positions
        )

    def _get_daily_bars(self, symbol: str) -> list[DailyBar] | None:
        if self.market_data_client:
            getter = getattr(self.market_data_client, 'get_yahoo_daily_bars', None)
            return getter(symbol) if getter else None
        return get_yahoo_daily_bars(symbol)

    @staticmethod
    def _get_position_entry_price(position: dict) -> float | None:
        """証券会社・ペーパー実装の平均取得価格フィールドを吸収します。"""
        for key in ('AveragePrice', 'AvgPrice', 'EntryPrice'):
            value = position.get(key)
            if value is not None and float(value) > 0:
                return float(value)
        return None

    def collect_preflight_market_data(self, symbols: list[str]) -> dict[str, dict] | None:
        """初回の売買判断に必要な確定終値と板価格を取得します。"""
        self._logged_trade_reasons.clear()
        self._board_unavailable_symbols.clear()
        reset_empty_daily_warning_dedupe()
        market_data = {}
        for symbol in symbols:
            daily_bars = self._get_daily_bars(symbol)
            closes = [bar.close for bar in daily_bars] if daily_bars is not None else (
                self.market_data_client.get_yahoo_daily_closes(symbol)
                if self.market_data_client else get_yahoo_daily_closes(symbol)
            )
            if not closes or len(closes) < config.RSI_MINIMUM_CLOSES:
                required_closes = max(
                    5,
                    config.RSI_MINIMUM_CLOSES,
                    config.RSI_PERIOD + 1,
                )
                self._log_trade_reason_once(
                    logging.WARNING,
                    "INSUFFICIENT_RSI_HISTORY",
                    symbol,
                    f"必要本数={required_closes} | 実績本数={len(closes) if closes else 0}",
                )
                return None
            board = (
                self.board_client.get_current_board(self.token, symbol)
                if self.board_client else get_current_board(self.token, symbol)
            )
            if not board or board.get('current_price') is None:
                self._board_unavailable_symbols.add(symbol)
                self._log_trade_reason_once(
                    logging.WARNING,
                    "BOARD_UNAVAILABLE",
                    symbol,
                    "preflightの板情報またはcurrent_priceがありません",
                )
                return None
            if symbol in self._board_unavailable_symbols:
                self._log_trade_reason_once(
                    logging.WARNING,
                    "BOARD_RECOVERED",
                    symbol,
                    "preflightの板価格取得が復旧しました",
                )
                self._board_unavailable_symbols.discard(symbol)
            snapshot = {'closes': closes, 'board': board}
            if daily_bars is not None:
                snapshot['daily_bars'] = daily_bars
            market_data[symbol] = snapshot
        return market_data

    # ================================================================================
    # レポート送信
    # ================================================================================

    def _daily_log_error_summary(self, today: str) -> dict:
        """当日のログからエラーの有無と代表的な概要だけを集計します。"""
        patterns = (" ERROR ", "異常終了", "予期しないエラー")
        messages = []
        for path in Path(config.LOG_DIRECTORY).glob("trade_project.log*"):
            try:
                for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
                    if not line.startswith(today) or not any(pattern in line for pattern in patterns):
                        continue
                    try:
                        logged_at = datetime.strptime(line[:19], "%Y-%m-%d %H:%M:%S")
                    except ValueError:
                        continue
                    if is_trading_session(
                        logged_at,
                        config.MARKET_OPEN_HOUR,
                        config.MARKET_OPEN_MINUTE,
                        config.MARKET_CLOSE_HOUR,
                        config.MARKET_CLOSE_MINUTE,
                    ):
                        messages.append(line.split(": ", 1)[-1].strip())
            except OSError:
                logger.warning("日次ログを読み込めません: %s", path)
        counts = Counter(messages)
        return {
            "has_errors": bool(messages),
            "count": len(messages),
            "summaries": [message for message, _ in counts.most_common(5)],
        }

    def _record_atr_danger_skip(self, symbol: str, observed_at: datetime, price: float, assessment, quantity: int) -> None:
        """ATR DANGERで見送った買いシグナルと、その後の観測価格を記録します。"""
        self._record_filter_decision_safely("ATR_DANGER_SKIP", symbol, observed_at, price, quantity, {
            "atr": assessment.atr,
            "true_range": assessment.latest_true_range,
            "atr_ratio": assessment.ratio,
            "atr_level": getattr(getattr(assessment, "level", None), "value", None),
        })

    def _update_atr_danger_skip_observation(self, symbol: str, observed_at: datetime, price: float) -> None:
        self._update_filter_decision_observations_safely(observed_at, {symbol: price})

    def _atr_danger_skip_summary(self, occurred_on: date | None = None) -> list[dict]:
        summaries = self._filter_decision_summaries("ATR_DANGER_SKIP", occurred_on)
        for item in summaries:
            item["skipped_at"] = item["occurred_at"]
            item["entry_price"] = item["reference_price"]
        return summaries

    def _record_atr_stop_exit(
        self,
        symbol: str,
        sold_at: datetime,
        entry_price: float,
        exit_price: float,
        quantity: int,
        assessment,
        stop_multiplier: float,
    ) -> None:
        """ATR損切りで約定した売却と、その後の価格推移を記録します。"""
        self._record_filter_decision_safely("ATR_STOP_EXIT", symbol, sold_at, exit_price, quantity, {
            "entry_price": entry_price,
            "atr": assessment.atr,
            "atr_ratio": assessment.ratio,
            "atr_level": assessment.level.value,
            "stop_multiplier": stop_multiplier,
            "stop_price": entry_price - assessment.atr * stop_multiplier,
            "realized_pnl_before_cost": (exit_price - entry_price) * quantity,
        })

    def _update_atr_stop_exit_observation(self, symbol: str, observed_at: datetime, price: float) -> None:
        self._update_filter_decision_observations_safely(observed_at, {symbol: price})

    def _atr_stop_exit_summary(self, occurred_on: date | None = None) -> list[dict]:
        summaries = self._filter_decision_summaries("ATR_STOP_EXIT", occurred_on)
        for item in summaries:
            item["sold_at"] = item["occurred_at"]
            item["exit_price"] = item["reference_price"]
            item["entry_price"] = item["inputs"].get("entry_price")
            item["stop_price"] = item["inputs"].get("stop_price")
            item["realized_pnl_before_cost"] = item["inputs"].get("realized_pnl_before_cost")
            item["lowest_price_after_exit"] = item["lowest_price"]
            item["highest_price_after_exit"] = item["highest_price"]
            item["last_price_after_exit"] = item["last_price"]
            item["post_exit_change_percent"] = item["price_change_percent"]
            item["avoided_pnl_before_cost"] = item["hypothetical_pnl_before_cost"]
        return summaries

    @staticmethod
    def _event_report_line(item: dict) -> str:
        """当日基準を主に表示する。当日列が無い旧行だけ複数営業日基準。"""
        same_day = (item.get("evaluations") or {}).get("same_day")
        if same_day:
            outcome = same_day["outcome"]
            pnl = same_day["hypothetical_pnl_before_cost"]
            label = (
                f"当日基準{'' if same_day['finalized'] else '(暫定)'}, "
                f"品質={same_day['data_quality'] or '未確定'}"
            )
        else:
            outcome, pnl, label = item["outcome"], item["hypothetical_pnl_before_cost"], "複数営業日基準"
        return f"{item['symbol']}: {outcome} ({pnl:+.0f}円概算) [{label}]"

    def _classify_no_trade_reason(
        self,
        today: str,
        order_count: int,
        atr_danger_skips: list,
        market_regime_danger_skips: list,
        market_regime_caution_rsi_filters: list,
    ) -> dict | None:
        """約定0件の日の理由を1つ選ぶ。記録専用で、失敗しても日次レポートは出す。"""
        if order_count > 0:
            return None
        codes: list[str] = []
        if self.decision_journal_repository is not None:
            try:
                records = self.decision_journal_repository.load_records(
                    today, STAGE_TRADING, self._execution_mode
                )
                codes = [str(record.get("reason_code")) for record in records]
            except Exception:
                logger.exception("売買0件の理由判別用の判断記録を読めませんでした")
        return classify_no_trade_reason(
            order_count=order_count,
            kill_switch_triggered=self.kill_switch_triggered,
            emergency_stop_triggered=self.emergency_stop_triggered,
            candidate_count=self._monitored_symbol_count,
            evaluated_count=len(self._buy_evaluated_symbols),
            buy_signal_count=len(self._buy_signal_symbols),
            journal_reason_codes=codes,
            market_regime_skip_count=(
                len(market_regime_danger_skips) + len(market_regime_caution_rsi_filters)
            ),
            atr_danger_skip_count=len(atr_danger_skips),
        )

    def _send_end_of_day_report(self) -> None:
        """市場終了時に本日の取引レポートを送信します。"""
        self._load_order_history()
        self._progress_reports_sent.clear()
        report_now = self._current_now or self._now_provider()
        today = report_now.date().isoformat()
        log_error_summary = self._daily_log_error_summary(today)
        daily_orders = [entry for entry in self.order_history if entry.timestamp.startswith(today)]
        self._finalize_filter_decisions_safely(report_now)
        atr_danger_skips = self._atr_danger_skip_summary(report_now.date())
        atr_stop_exits = self._atr_stop_exit_summary(report_now.date())
        market_regime_danger_skips = self._filter_decision_summaries("MARKET_REGIME_DANGER_SKIP", report_now.date())
        market_regime_caution_rsi_filters = self._filter_decision_summaries(
            "MARKET_REGIME_CAUTION_RSI_FILTER", report_now.date()
        )
        adx_trend_reliefs = self._filter_decision_summaries("ADX_TREND_RELIEF", report_now.date())
        lines = [
            "【業務】取引運用",
            "【機能】取引終了",
            "【概要】",
            f"{config.TRADING_MODE_LABEL}の本日の取引を終了しました。",
            "【詳細】",
            f"約定件数: {len(daily_orders)}件（買い: {sum(entry.side == config.OrderSide.BUY for entry in daily_orders)}件 / 売り: {sum(entry.side == config.OrderSide.SELL for entry in daily_orders)}件）",
        ]
        if self.kill_switch_triggered:
            lines.append("キルスイッチ: 発動")
        if self.emergency_stop_triggered:
            lines.append("手動緊急停止: 発動")
        if not daily_orders:
            lines.append("本日の約定はありませんでした。")
        daily_realized_pnl = 0.0
        if self.order_sender and hasattr(self.order_sender, 'get_daily_realized_pnl'):
            daily_realized_pnl = float(self.order_sender.get_daily_realized_pnl() or 0)
        total_unrealized_pnl = sum(
            float(position.get('ProfitLoss', 0) or 0) for position in self.last_positions
        )
        lines.append("--- 損益 ---")
        lines.append(f"実現損益: {daily_realized_pnl}円")
        if self.last_positions:
            lines.append("--- 評価損益 ---")
            for position in self.last_positions:
                lines.append(
                    f"{position.get('Symbol', '')}: "
                    f"{position.get('ProfitLoss', 0)}円 "
                    f"({position.get('ProfitLossRate', 0)}%)"
                )
        lines.append(f"評価損益: {total_unrealized_pnl}円")
        lines.append(f"合計損益: {daily_realized_pnl + total_unrealized_pnl}円")
        lines.append("--- エラー概要 ---")
        if log_error_summary["has_errors"]:
            lines.append(f"エラーあり: {log_error_summary['count']}件")
            lines.extend(f"- {summary}" for summary in log_error_summary["summaries"])
        else:
            lines.append("エラーなし")
        if atr_danger_skips:
            lines.append("ATR DANGER見送り:")
            for item in atr_danger_skips:
                lines.append(self._event_report_line(item))
        if atr_stop_exits:
            lines.append("ATR損切り売却:")
            for item in atr_stop_exits:
                lines.append(self._event_report_line(item))
        if self._liquidation_results:
            lines.append("--- 強制売却確認 ---")
            lines.extend(
                f"{item['symbol']}: {item['status']}"
                + (f" ({item['reason']})" if item.get("reason") else "")
                for item in self._liquidation_results
            )

        market_assessment = self.market_regime_assessment
        market_conditions = {
            "assessment_status": (
                "not_evaluated"
                if market_assessment is None
                else "available" if getattr(market_assessment, "data_available", False) else "unavailable"
            ),
            "regime": self.market_regime.value if market_assessment is not None else None,
            "realized_volatility_percent": getattr(market_assessment, "realized_volatility_percent", None),
            "vix": getattr(market_assessment, "vix", None),
            "nikkei_change_percent": getattr(market_assessment, "nikkei_change_percent", None),
            "adx": getattr(market_assessment, "adx", None),
            "data_available": (
                getattr(market_assessment, "data_available", False)
                if market_assessment is not None
                else None
            ),
            "failure_reason": getattr(market_assessment, "failure_reason", None),
        }
        no_trade_reason = self._classify_no_trade_reason(
            today, len(daily_orders), atr_danger_skips,
            market_regime_danger_skips, market_regime_caution_rsi_filters,
        )
        daily_summary = {
            "date": today,
            "trading_mode": config.TRADING_MODE_LABEL,
            "order_count": len(daily_orders),
            "orders": [
                {
                    "symbol": entry.symbol,
                    "side": entry.side.value,
                    "price": entry.price,
                    "qty": entry.qty,
                    "result_code": entry.result_code,
                    "atr": entry.atr,
                    "atr_true_range": entry.atr_true_range,
                    "atr_ratio": entry.atr_ratio,
                    "atr_level": entry.atr_level,
                    "atr_stop_multiplier": entry.atr_stop_multiplier,
                    "market_regime": entry.market_regime,
                }
                for entry in daily_orders
            ],
            "market_conditions": market_conditions,
            "positions": [
                {
                    "symbol": position.get("Symbol", ""),
                    "profit_loss": float(position.get("ProfitLoss", 0) or 0),
                    "profit_loss_rate": float(position.get("ProfitLossRate", 0) or 0),
                }
                for position in self.last_positions
            ],
            "realized_profit_loss": daily_realized_pnl,
            "unrealized_profit_loss": total_unrealized_pnl,
            "total_profit_loss": daily_realized_pnl + total_unrealized_pnl,
            "kill_switch_triggered": self.kill_switch_triggered,
            "emergency_stop_triggered": self.emergency_stop_triggered,
            "log_errors": log_error_summary,
            "atr_danger_skips": atr_danger_skips,
            "atr_stop_exits": atr_stop_exits,
            "market_regime_danger_skips": market_regime_danger_skips,
            "market_regime_caution_rsi_filters": market_regime_caution_rsi_filters,
            "adx_trend_reliefs": adx_trend_reliefs,
            "liquidation_results": self._liquidation_results,
            "trading_evaluation": {
                "candidate_count": self._monitored_symbol_count,
                "evaluated_count": len(self._buy_evaluated_symbols),
                "buy_signal_count": len(self._buy_signal_symbols),
            },
            "no_trade_reason": no_trade_reason,
        }
        report_text = "\n".join(lines)
        report_data = {
            **daily_summary,
            "generated_at": report_now.isoformat(timespec="seconds"),
            "report_text": report_text,
            "llm_analysis": None,
        }
        report_saved = self._save_daily_report(today, report_data, report_text)
        positions = [
            f"{item.get('Symbol', '')}({float(item.get('ProfitLoss', 0) or 0):+.0f}円)"
            for item in self.last_positions
        ]
        error_summaries = log_error_summary.get("summaries") or []
        error_detail = " / ".join(str(item) for item in error_summaries[:2]) or "なし"
        liquidation_results = self._liquidation_results
        liquidation_detail = "、".join(
            f"{item['symbol']}:{item['status']}" for item in liquidation_results
        ) or "対象なし"
        notification = format_result_notification(
            "取引",
            "トレード終了",
            "本日の取引処理が終了しました。",
            [
                f"約定件数: {len(daily_orders)}件",
                f"実現損益: {daily_realized_pnl:+.0f}円",
                f"評価損益: {total_unrealized_pnl:+.0f}円",
                f"保有: {', '.join(positions) if positions else 'なし'}",
                f"エラー概要: {error_detail}",
                f"強制売却確認: {liquidation_detail}",
                *([] if report_saved else ["日次レポート保存失敗"]),
            ],
        )
        if self.notifier:
            self.notifier(notification)
        else:
            notify_daily(notification)

    DAILY_REPORT_SAVE_RETRY_SECONDS = 1.0

    def _save_daily_report(self, today, report_data: dict, report_text: str) -> bool:
        """日次レポートを保存する。失敗時は1回再試行し、なお失敗なら通知して False を返す。"""
        report_path = self.daily_report_directory / f"{today}.json"
        error = "write_json が False を返しました(詳細はログを確認してください)"
        for attempt in range(2):
            if attempt:
                time.sleep(self.DAILY_REPORT_SAVE_RETRY_SECONDS)
            try:
                self.daily_report_directory.mkdir(parents=True, exist_ok=True)
                if write_json(report_path, report_data):
                    return True
            except OSError as exc:
                error = f"{type(exc).__name__}: {exc}"
                logger.exception("日次レポートの保存準備に失敗: %s", report_path)
        logger.critical(
            "日次レポートの保存に失敗しました: ファイル=%s | エラー=%s\nreport_text:\n%s",
            report_path.name, error, report_text,
        )
        self._notify_safely(
            f"日次レポートの保存に失敗しました。ファイル: {report_path.name} / エラー: {error}"
        )
        return False

    def _fresh_liquidation_quote(
        self, symbol: str
    ) -> tuple[float | None, str | None, dict | None]:
        if self.board_client is not None:
            getter = getattr(self.board_client, "get_current_board_with_freshness", None)
            board = getter(self.token, symbol) if callable(getter) else None
        else:
            board = get_current_board_with_freshness(self.token, symbol)
        if not board:
            return None, "LIQUIDATION_BOARD_UNAVAILABLE", board

        raw_time = board.get("current_price_time")
        try:
            quote_time = datetime.fromisoformat(raw_time) if isinstance(raw_time, str) else raw_time
        except (TypeError, ValueError):
            return None, "LIQUIDATION_PRICE_TIME_INVALID", board
        if not isinstance(quote_time, datetime) or quote_time.tzinfo is None:
            return None, "LIQUIDATION_PRICE_TIME_INVALID", board

        evaluation_time = self._current_now or datetime.now(JST)
        if evaluation_time.tzinfo is None:
            evaluation_time = evaluation_time.replace(tzinfo=JST)
        if quote_time.astimezone(JST).date() != evaluation_time.astimezone(JST).date():
            return None, "LIQUIDATION_PRICE_NOT_TODAY", board

        status = board.get("current_price_status")
        if isinstance(status, bool) or status not in (1, 8, "1", "8"):
            return None, "LIQUIDATION_PRICE_STATUS_INVALID", board

        raw_price = board.get("current_price")
        try:
            price = float(raw_price)
        except (TypeError, ValueError, OverflowError):
            return None, "LIQUIDATION_PRICE_INVALID", board
        if not math.isfinite(price) or price <= 0:
            return None, "LIQUIDATION_PRICE_INVALID", board
        return price, None, board

    def _fetch_positions_for_liquidation(self) -> List[dict] | None:
        """
        清算処理専用の保有株一覧取得。

        取得失敗(None)と保有ゼロ([])を区別するため、`_load_account_state`の
        `or []`フォールバックは使わない。失敗時は既定回数までバックオフを挟んで
        再試行する（401で失敗した場合も、再試行のたびにrequest_handler層の
        トークン自動再取得が働く）。
        """
        attempts = max(1, config.LIQUIDATION_POSITIONS_FETCH_RETRIES)
        for attempt in range(attempts):
            if self.positions_client:
                positions = self.positions_client.get_positions(self.token)
            elif self.order_sender and hasattr(self.order_sender, 'get_positions'):
                positions = self.order_sender.get_positions(self.token)
            else:
                positions = get_positions(self.token)
            if positions is not None:
                return positions
            if attempt < attempts - 1:
                logger.warning(
                    "保有株一覧の取得に失敗しました。再試行します(%d/%d)。",
                    attempt + 1, attempts,
                )
                time.sleep(config.LIQUIDATION_POSITIONS_FETCH_RETRY_BACKOFF_SECONDS)
        return None

    def _liquidate_all_positions(
        self,
        *,
        require_fresh_price: bool = False,
        late: bool = False,
    ) -> None:
        """持ち越しを防ぐため、現物の全保有を成行で売却します。"""
        self._liquidation_results = []
        unresolved: list[dict[str, object]] = []
        positions = self._fetch_positions_for_liquidation()
        if positions is None:
            logger.error(
                "EOD_LIQUIDATION_POSITIONS_UNAVAILABLE: 保有株一覧を取得できず、強制決済を実行できませんでした。"
            )
            self._notify_safely(
                "\n".join([
                    "【業務】取引運用",
                    "【機能】強制決済",
                    "【概要】",
                    "保有株一覧の取得に失敗したため、強制決済を実行できませんでした。",
                    "【詳細】",
                    "kabuステーションの起動・認証状態を確認し、必要なら手動で保有状況を確認してください。",
                ])
            )
            return
        self.last_positions = positions
        holdings: dict[str, int] = {}
        for position in positions:
            if position.get('Side') != config.OrderSide.SELL.value:
                continue
            quantity = int(position.get('HoldQty', 0) or 0)
            if quantity > 0:
                symbol = str(position.get('Symbol', ''))
                if symbol:
                    holdings[symbol] = holdings.get(symbol, 0) + quantity

        for symbol, quantity in holdings.items():
            try:
                if require_fresh_price:
                    price, quote_failure, board = self._fresh_liquidation_quote(symbol)
                    if quote_failure is not None or price is None:
                        reason = quote_failure or "LIQUIDATION_PRICE_INVALID"
                        result = {
                            "symbol": symbol,
                            "quantity": quantity,
                            "status": "未決済",
                            "reason": reason,
                        }
                        self._liquidation_results.append(result)
                        unresolved.append(result)
                        logger.error(
                            "EOD_LIQUIDATION_UNRESOLVED: 銘柄=%s | 数量=%s | 理由=%s | 板=%r",
                            symbol,
                            quantity,
                            reason,
                            board,
                        )
                        continue
                else:
                    board = (
                        self.board_client.get_current_board(self.token, symbol)
                        if self.board_client else get_current_board(self.token, symbol)
                    )
                    price = float((board or {}).get('current_price') or 0)
                    if price <= 0:
                        self._log_trade_reason_once(
                            logging.WARNING,
                            "EOD_PRICE_UNAVAILABLE",
                            symbol,
                            f"持ち越し防止売り価格={price} | 数量={quantity}",
                        )
                decision_reason = "EOD_LATE_LIQUIDATION" if late else "持ち越し防止"
                signal = TradeSignal(symbol, config.OrderSide.SELL, price, quantity)
                if self.order_sender and hasattr(self.order_sender, 'set_price') and price > 0:
                    self.order_sender.set_price(symbol, price)
                order_method = self.order_sender.place_market_order if self.order_sender else place_market_order
                order_args = (self.token, symbol, config.OrderSide.SELL.value, quantity)
                try:
                    inspect.signature(order_method).bind(*order_args)
                except TypeError:
                    order_args = order_args[:3]
                order_result = order_method(*order_args)
                if order_result and order_result.get('Result') == 0:
                    self._register_order(
                        signal,
                        PriceLimit(price, price),
                        order_result,
                        diagnostics={"decision_reason": decision_reason},
                    )
                    self._liquidation_results.append({
                        "symbol": symbol,
                        "quantity": quantity,
                        "status": "売却注文受付",
                        "reason": self._sell_condition_observations.get(
                            symbol, "通常SELL条件の観測なし（15:20以降の起動など）"
                        ),
                    })
                    if late:
                        logger.info(
                            "EOD_LATE_LIQUIDATION: 銘柄=%s | 数量=%s | 約定価格=%s",
                            symbol,
                            quantity,
                            price,
                        )
                    elif self._is_paper_mode():
                        logger.info("持ち越し防止売りが成立しました: 銘柄=%s | 数量=%s", symbol, quantity)
                    else:
                        logger.info("持ち越し防止売りを発注しました: 銘柄=%s | 数量=%s", symbol, quantity)
                else:
                    reason_code = (
                        "ORDER_REJECTED_NONE" if order_result is None else "ORDER_REJECTED_RESULT"
                    )
                    self._liquidation_results.append({
                        "symbol": symbol,
                        "quantity": quantity,
                        "status": "売却注文失敗",
                        "reason": reason_code,
                    })
                    logger.error(
                        "%s: 銘柄=%s | EOD方向=SELL | 数量=%s | 応答=%r",
                        reason_code,
                        symbol,
                        quantity,
                        order_result,
                    )
                    result = {
                        "symbol": symbol,
                        "quantity": quantity,
                        "status": "売却注文失敗",
                        "reason": reason_code,
                    }
                    unresolved.append(result)
                    logger.error(
                        "EOD_LIQUIDATION_UNRESOLVED: 銘柄=%s | 数量=%s | 理由=%s",
                        symbol,
                        quantity,
                        reason_code,
                    )
            except Exception:
                result = {
                    "symbol": symbol,
                    "quantity": quantity,
                    "status": "売却処理エラー",
                    "reason": "EOD_LIQUIDATION_EXCEPTION",
                }
                self._liquidation_results.append(result)
                unresolved.append(result)
                logger.exception("持ち越し防止売り中にエラーが発生しました: 銘柄=%s", symbol)

        if require_fresh_price and unresolved:
            summary = "【緊急】EOD決済未完了\n" + "\n".join(
                f"{item['symbol']}: 数量={item['quantity']} 理由={item['reason']}"
                for item in unresolved
            )
            self._notify_safely(summary)

    def warn_on_overnight_positions(self) -> None:
        """持ち越し禁止設定で起動時に残っている保有だけを警告します。"""
        if config.ALLOW_OVERNIGHT_HOLDING:
            return
        _, positions = self._load_account_state()
        for position in positions:
            if position.get('Side') != config.OrderSide.SELL.value:
                continue
            quantity = int(position.get('HoldQty', 0) or 0)
            if quantity <= 0:
                continue
            self._log_trade_reason_once(
                logging.WARNING,
                "OVERNIGHT_POSITION_AT_START",
                str(position.get('Symbol', '')),
                f"保有数量={quantity}",
            )

    # ================================================================================
    # メイン取引ループ
    # ================================================================================

    def run(self, top_symbols_path: Path | None = None, now_provider=None, sleep=None, preflight_market_data=None) -> None:
        """
        取引ボットを起動します。市場終了まで銘柄を監視し、売買シグナルで自動注文を実行します。
        
        処理フロー：
        1. 注文履歴を読み込み
        2. フィルタ結果または指定ファイルから監視銘柄を取得
        3. 市場終了まで以下を繰り返す：
           - 各銘柄の株価を確認
           - 売買シグナルを生成
           - 資金確認とキルスイッチ判定を実施
           - 安全が確認できた場合のみ注文実行
        4. 最終的な評価損益を確認
        5. レポートを送信
        
        Args:
            top_symbols_path: 監視銘柄リストファイルのパス（オプション）
            now_provider: 現在時刻を取得する関数（テスト用、デフォルト：datetime.now）
            sleep: スリープ関数（テスト用、デフォルト：time.sleep）
        """
        self._load_order_history()
        self._missing_holding_sell_signals.clear()
        self._logged_trade_reasons.clear()
        self._board_unavailable_symbols.clear()
        self._buy_evaluated_symbols.clear()
        self._buy_signal_symbols.clear()
        self._holding_high_prices.clear()
        self._last_logged_judgments.clear()
        self._shadow_positions.clear()
        self._shadow_pending_symbols.clear()
        reset_empty_daily_warning_dedupe()
        now_provider = now_provider or datetime.now
        self._now_provider = now_provider
        self._current_now = None
        sleep = sleep or time.sleep
        initial_now = None
        if self.filtering_result_repository:
            result = self.filtering_result_repository.load_latest()
            initial_now = now_provider()
            self._current_now = initial_now
            today = initial_now.date().isoformat()
            if not result or result.date != today or not result.symbols:
                message = "当日のフィルタ結果がないため、取引を開始しません"
                if self.notifier:
                    self.notifier(message)
                else:
                    notify_daily(message)
                return
            symbols = result.symbols
        else:
            symbols = read_json(top_symbols_path) if top_symbols_path else []
            symbols = symbols or []
        if not symbols:
            logger.info("上位銘柄リストが空です。取引を行いません。")
            return

        self._monitored_symbol_count = len(symbols)
        if initial_now is None:
            initial_now = now_provider()
            self._current_now = initial_now
        self._progress_report_start_time = initial_now
        today = initial_now.date().isoformat()

        if self.market_regime_usecase is not None:
            self.prepare_market_regime()
            logger.info(
                "MarketRegimeを取得しました: レジーム=%s | データ取得=%s | 理由=%s",
                self.market_regime.value,
                self.market_regime_assessment.data_available,
                self.market_regime_assessment.failure_reason or "なし",
            )

        self._initialize_paper_prices(preflight_market_data)
        initial_wallet_amount, initial_positions = self._load_account_state()
        self._record_account_snapshot_safely("start", initial_now, initial_positions)
        try:
            self._restore_shadow_positions(initial_positions, initial_now)
        except Exception as exc:
            for position in initial_positions:
                if self._is_open_position(position):
                    symbol = str(position.get("Symbol", ""))
                    self._log_trade_reason_once(
                        logging.WARNING, "SHADOW_HISTORY_RESTORE_FAILED", symbol,
                        f"再起動時の履歴復元をスキップ: 例外={type(exc).__name__}: {exc}",
                    )
        self._initialize_daily_starting_capital(
            today, initial_wallet_amount, initial_positions
        )

        kill_switch_triggered = False
        filter_decisions_initialized = False
        first_loop = True
        previous_loop_now = initial_now
        # 市場終了時刻まで取引ループを実行
        use_preflight_market_data = bool(preflight_market_data)
        while not kill_switch_triggered:
            if self._is_emergency_stop_requested():
                self.emergency_stop_triggered = True
                self._trigger_kill_switch("手動緊急停止フラグが検知されました")
                self._liquidate_all_positions(require_fresh_price=self._is_paper_mode())
                break
            now = initial_now if first_loop else now_provider()
            self._current_now = now
            first_loop = False
            self._retry_kill_switch_baseline_save()
            if not filter_decisions_initialized:
                self._finalize_filter_decisions_safely(now)
                self._finalize_same_day_filter_decisions_safely(now, include_today=False)
                filter_decisions_initialized = True
            if is_market_closed(now.time(), config.MARKET_CLOSE_HOUR, config.MARKET_CLOSE_MINUTE):
                if not config.ALLOW_OVERNIGHT_HOLDING:
                    self._liquidate_all_positions(require_fresh_price=True, late=True)
                self._finalize_same_day_filter_decisions_safely(now, include_today=True)
                break
            if not config.ALLOW_OVERNIGHT_HOLDING and is_market_closed(
                now.time(),
                config.MARKET_LIQUIDATION_HOUR,
                config.MARKET_LIQUIDATION_MINUTE,
            ):
                self._liquidate_all_positions(require_fresh_price=True)
                self._finalize_same_day_filter_decisions_safely(now, include_today=True)
                break
            self._send_due_progress_reports(previous_loop_now, now)
            board_fetch_attempted = 0
            board_fetch_failed = 0
            for symbol in symbols:
                try:
                    # RSI計算用の確定日足終値を取得
                    snapshot = preflight_market_data.get(symbol) if use_preflight_market_data else None
                    if snapshot:
                        closes = snapshot['closes']
                    elif self.market_data_client:
                        closes = self.market_data_client.get_yahoo_daily_closes(symbol)
                    else:
                        closes = get_yahoo_daily_closes(symbol)
                    limit = calculate_price_limit(closes)
                    rsi = calculate_rsi(closes, config.RSI_PERIOD, config.RSI_MINIMUM_CLOSES)
                    required_closes = max(
                        5,
                        config.RSI_MINIMUM_CLOSES,
                        config.RSI_PERIOD + 1,
                    )
                    actual_closes = len(closes) if closes is not None else 0
                    entry_threshold = resolve_rsi_entry_threshold(
                        self.market_regime,
                        config.RSI_ENTRY_THRESHOLD,
                        config.RSI_ENTRY_THRESHOLD_CAUTION,
                    )
                    if limit is None:
                        self._log_rsi_unavailable(
                            symbol, now, closes, rsi, entry_threshold, None
                        )
                        continue
                    # リアルタイム株価を取得
                    if not snapshot:
                        board_fetch_attempted += 1
                    board = snapshot['board'] if snapshot else (
                        self.board_client.get_current_board(self.token, symbol)
                        if self.board_client else get_current_board(self.token, symbol)
                    )
                    if not board or board.get('current_price') is None:
                        if not snapshot:
                            board_fetch_failed += 1
                        self._board_unavailable_symbols.add(symbol)
                        self._observe_shadow_positions(
                            symbol, now, None, quality="board_unavailable"
                        )
                        self._mark_board_unavailable_safely(symbol, now)
                        self._log_rsi_unavailable(
                            symbol, now, closes, rsi, entry_threshold, None
                        )
                        self._journal_decision(
                            symbol, "BOARD_UNAVAILABLE", now,
                            rsi=rsi, rsi_applied_threshold=entry_threshold,
                            rsi_required_closes=required_closes, rsi_actual_closes=actual_closes,
                        )
                        self._log_trade_reason_once(
                            logging.WARNING,
                            "BOARD_UNAVAILABLE",
                            symbol,
                            "板情報またはcurrent_priceがありません",
                        )
                        continue
                    if symbol in self._board_unavailable_symbols:
                        self._log_trade_reason_once(
                            logging.WARNING,
                            "BOARD_RECOVERED",
                            symbol,
                            "板価格取得が復旧しました",
                        )
                        self._board_unavailable_symbols.discard(symbol)
                    current_price = float(board['current_price'])
                    self._log_rsi_unavailable(
                        symbol, now, closes, rsi, entry_threshold, current_price
                    )
                    self._update_filter_decision_observations_safely(
                        now, {symbol: current_price}
                    )
                    daily_bars = snapshot.get('daily_bars') if snapshot else self._get_daily_bars(symbol)
                    assessment = assess_volatility(
                        daily_bars,
                        config.ATR_PERIOD,
                        config.ATR_CAUTION_RATIO,
                        config.ATR_DANGER_RATIO,
                    ) if daily_bars else None
                    previous_close = (
                        float(daily_bars[-1].close)
                        if daily_bars and getattr(daily_bars[-1], "close", None) is not None
                        else None
                    )
                    self._observe_shadow_positions(
                        symbol, now, current_price, assessment, previous_close
                    )
                    entry_price = None
                    held_high = None
                    trailing_stop_line = None
                    atr_stop_multiplier = None
                    decision_reason = None
                    # 売買シグナルを生成
                    signal = TradeSignal.evaluate(
                        symbol,
                        board['current_price'],
                        limit,
                        rsi,
                        entry_threshold,
                        config.RSI_EXIT_THRESHOLD,
                    )
                    self._buy_evaluated_symbols.add(symbol)
                    if signal is not None and signal.side == config.OrderSide.BUY:
                        self._buy_signal_symbols.add(symbol)
                    held_position = None
                    if signal is not None and signal.side == config.OrderSide.BUY:
                        _, current_positions = self._load_account_state()
                        held_position = next(
                            (
                                position for position in current_positions
                                if position.get('Symbol') == symbol
                                and position.get('Side') == config.OrderSide.SELL.value
                                and int(position.get('HoldQty', 0) or 0) > 0
                            ),
                            None,
                        )
                    if signal is not None:
                        decision_reason = (
                            "上側バンド突破・RSI条件成立"
                            if signal.side == config.OrderSide.BUY
                            else "下側バンド到達・RSI条件成立"
                        )
                    if (
                        signal is None or signal.side == config.OrderSide.BUY
                    ) and assessment is not None:
                        if held_position is None:
                            _, current_positions = self._load_account_state()
                            held_position = next(
                                (
                                    position for position in current_positions
                                    if position.get('Symbol') == symbol
                                    and position.get('Side') == config.OrderSide.SELL.value
                                    and int(position.get('HoldQty', 0) or 0) > 0
                                ),
                                None,
                            )
                        entry_price = self._get_position_entry_price(held_position) if held_position else None
                        if held_position is not None and entry_price is None:
                            quantity = int(held_position.get('HoldQty', 0) or 0)
                            if quantity > 0:
                                self._journal_decision(
                                    symbol, "ATR_ENTRY_PRICE_UNAVAILABLE", now,
                                    current_price=current_price, holding_quantity=quantity,
                                    **self._volatility_inputs(assessment),
                                )
                                self._log_trade_reason_once(
                                    logging.WARNING,
                                    "ATR_ENTRY_PRICE_UNAVAILABLE",
                                    symbol,
                                    f"保有数量={quantity} | 平均取得価格がありません",
                                )
                        if entry_price is not None:
                            # ADR-0002: ATR損切りの基準点を、エントリー価格ではなく
                            # 「保有開始後の最高値」に置き換える(シャンデリア・イグジット)。
                            # 保有中最高値がまだ記録されていない場合(再起動直後など)は
                            # エントリー価格を初期値として扱う(=含み益ゼロなら従来と同じ挙動)。
                            held_high = max(
                                self._holding_high_prices.get(symbol, entry_price),
                                float(board['current_price']),
                            )
                            self._holding_high_prices[symbol] = held_high
                            # ADR-0006: 含み益がATR基準で一定以上乗っている場合のみ、
                            # 利確専用の(より広い)倍率を使う。乗っていなければ従来の損切り倍率のまま。
                            atr_stop_multiplier = resolve_atr_exit_multiplier(
                                held_high - entry_price,
                                assessment.atr,
                                assessment.level,
                                config.ATR_STOP_NORMAL_MULTIPLIER,
                                config.ATR_STOP_CAUTION_MULTIPLIER,
                                config.ATR_STOP_DANGER_MULTIPLIER,
                                config.ATR_PROFIT_LOCK_NORMAL_MULTIPLIER,
                                config.ATR_PROFIT_LOCK_CAUTION_MULTIPLIER,
                                config.ATR_PROFIT_LOCK_DANGER_MULTIPLIER,
                                config.ATR_PROFIT_LOCK_TRIGGER_ATR_MULTIPLE,
                            )
                            trailing_stop_line = held_high - assessment.atr * atr_stop_multiplier
                            atr_signal = TradeSignal.evaluate(
                                symbol,
                                board['current_price'],
                                limit,
                                rsi,
                                entry_threshold,
                                config.RSI_EXIT_THRESHOLD,
                                held_high,
                                assessment.atr,
                                atr_stop_multiplier,
                            )
                            if atr_signal is not None:
                                signal = atr_signal
                                decision_reason = "ATR損切り基準到達"
                    if signal is None:
                        _, observed_positions = self._load_account_state()
                        held_position = next(
                            (
                                position for position in observed_positions
                                if position.get("Symbol") == symbol
                                and position.get("Side") == config.OrderSide.SELL.value
                                and int(position.get("HoldQty", 0) or 0) > 0
                            ),
                            None,
                        )
                        if held_position:
                            price_reason = (
                                f"現在値={board['current_price']:.1f} > 決済基準={limit.lower_band:.1f}"
                                if board['current_price'] > limit.lower_band
                                else "現在値条件は成立したがRSI条件未成立"
                            )
                            rsi_reason = (
                                f"RSI={rsi:.1f} > 決済RSI基準={config.RSI_EXIT_THRESHOLD:.1f}"
                                if rsi is not None and rsi > config.RSI_EXIT_THRESHOLD
                                else "RSIが取得できない"
                            )
                            self._sell_condition_observations[symbol] = (
                                f"通常SELL条件未成立: {price_reason}; {rsi_reason}"
                            )
                        if self.market_regime == MarketRegime.CAUTION:
                            normal_signal = TradeSignal.evaluate(
                                symbol, board['current_price'], limit, rsi,
                                config.RSI_ENTRY_THRESHOLD, config.RSI_EXIT_THRESHOLD,
                            )
                            if normal_signal is not None and normal_signal.side == config.OrderSide.BUY:
                                wallet_amount, positions = self._load_account_state()
                                if wallet_amount is not None:
                                    open_position_count = self._count_open_positions(positions)
                                    remaining_slots = max(config.TARGET_POSITIONS - open_position_count, 1)
                                    estimated_budget = min(
                                        wallet_amount / remaining_slots,
                                        config.MAX_ORDER_AMOUNT_PER_TRADE,
                                    )
                                    estimated_quantity = calculate_buy_quantity(
                                        normal_signal.price, estimated_budget, config.ORDER_UNIT
                                    )
                                    if estimated_quantity > 0:
                                        self._record_filter_decision_safely(
                                            "MARKET_REGIME_CAUTION_RSI_FILTER", symbol, now,
                                            float(board['current_price']), estimated_quantity,
                                            self._market_regime_event_inputs(
                                                rsi,
                                                entry_threshold,
                                                assessment,
                                                rsi_input=self._rsi_input_snapshot(
                                                    symbol, now, closes, rsi, entry_threshold,
                                                    current_price, "excluded_by_caution_threshold",
                                                ),
                                            ),
                                        )
                    available_daily_bars = len(daily_bars) if daily_bars is not None else 0
                    if (
                        (signal is None or signal.side == config.OrderSide.BUY)
                        and held_position is not None
                        and assessment is None
                        and available_daily_bars < config.ATR_PERIOD
                    ):
                        self._journal_decision(
                            symbol, "ATR_DATA_UNAVAILABLE", now,
                            current_price=current_price,
                            atr_required_bars=config.ATR_PERIOD, atr_actual_bars=available_daily_bars,
                        )
                        self._log_trade_reason_once(
                            logging.WARNING,
                            "ATR_DATA_UNAVAILABLE",
                            symbol,
                            f"必要本数={config.ATR_PERIOD} | 実績本数={available_daily_bars}",
                        )
                    # ADR-0002: 初回または判定状態の変化だけINFO、同じ判定の繰り返しはDEBUG。
                    judgment = signal.side.name if signal else "なし"
                    log_level = self._trade_judgment_log_level(symbol, judgment)
                    logger.log(
                        log_level,
                        "売買判定: 銘柄=%s | 現在値=%.1f | エントリー基準=%.1f | 決済基準=%.1f | "
                        "RSI=%.1f | エントリーRSI基準=%.1f | 決済RSI基準=%.1f | "
                        "保有中最高値=%s | 利確ライン=%s | 判定=%s",
                        symbol,
                        board['current_price'],
                        limit.upper_band,
                        limit.lower_band,
                        rsi,
                        entry_threshold,
                        config.RSI_EXIT_THRESHOLD,
                        f"{held_high:.1f}" if held_high is not None else "-",
                        f"{trailing_stop_line:.1f}" if trailing_stop_line is not None else "-",
                        judgment,
                    )
                    if signal is None or signal.side == config.OrderSide.BUY:
                        self._observe_missing_holding_sell_signal(
                            symbol, now, missing_holdings=False
                        )
                    if not signal:
                        continue
                    # 口座状態を確認
                    wallet_amount, positions = self._load_account_state()
                    self.last_positions = positions
                    has_holdings = self._has_holdings(symbol, positions)
                    if signal.side == config.OrderSide.SELL:
                        self._observe_missing_holding_sell_signal(
                            symbol,
                            now,
                            missing_holdings=not has_holdings,
                            cleared_by="保有を確認",
                        )
                    api_limit = config.API_SOFT_LIMIT if config.IS_DEMO else get_api_soft_limit(self.token)
                    if api_limit is None:
                        logger.warning("API発注上限を取得できないため、発注を停止しました。")
                        self._trigger_kill_switch("API発注上限を取得できません")
                        kill_switch_triggered = True
                        break
                    self.api_soft_limit = api_limit
                    if signal.side == config.OrderSide.BUY:
                        has_holdings = self._has_holdings(symbol, positions)
                        open_position_count = self._count_open_positions(positions)
                        if not has_holdings and open_position_count >= config.TARGET_POSITIONS:
                            self._journal_decision(
                                symbol, "POSITION_LIMIT_REACHED", now, current_price=current_price,
                                **self._volatility_inputs(assessment),
                            )
                            logger.info(
                                "保有上限のため新規買いを見送ります: 銘柄=%s | 保有数=%s/%s",
                                symbol,
                                open_position_count,
                                config.TARGET_POSITIONS,
                            )
                            continue
                        if wallet_amount is None:
                            self._journal_decision(
                                symbol, "WALLET_UNKNOWN", now, current_price=current_price,
                            )
                            logger.warning("現物買付可能額が不明なため、買い注文を見送ります。")
                            continue
                        original_qty = 0
                        remaining_slots = max(config.TARGET_POSITIONS - open_position_count, 1)
                        budget_per_position = min(
                            wallet_amount / remaining_slots,
                            config.MAX_ORDER_AMOUNT_PER_TRADE,
                            self.api_soft_limit,
                        )
                        signal.qty = calculate_buy_quantity(
                            signal.price,
                            budget_per_position,
                            config.ORDER_UNIT,
                        )
                        if signal.qty == 0:
                            reason_code = (
                                "budget_below_one_lot"
                                if signal.price > 0
                                and budget_per_position < signal.price * config.ORDER_UNIT
                                else "buy_quantity_invalid_inputs"
                            )
                            self._journal_decision(
                                symbol, reason_code, now, current_price=current_price,
                                allocated_budget=budget_per_position, quantity_before=0,
                                quantity_after=0, quantity_zero_reason=reason_code,
                                unit_price=signal.price, order_unit=config.ORDER_UNIT,
                            )
                            self._log_trade_reason_once(
                                logging.WARNING,
                                reason_code,
                                symbol,
                                f"予算={budget_per_position} | 価格={signal.price} | 1単位={config.ORDER_UNIT}",
                            )
                        if daily_bars:
                            if assessment:
                                original_qty = signal.qty
                                signal.qty = adjust_quantity_for_volatility(
                                    signal.qty,
                                    assessment.level,
                                    config.ORDER_UNIT,
                                    config.ATR_CAUTION_LOT_RATIO,
                                    config.ATR_DANGER_ACTION,
                                )
                                if original_qty > 0 and signal.qty == 0:
                                    if (
                                        assessment.level == VolatilityLevel.DANGER
                                        and config.ATR_DANGER_ACTION == "skip"
                                    ):
                                        reason_code = "atr_danger_skip"
                                    elif assessment.level == VolatilityLevel.CAUTION:
                                        reason_code = "caution_rounding_to_zero"
                                    else:
                                        reason_code = "atr_quantity_adjustment_zero"
                                    self._journal_decision(
                                        symbol, reason_code, now, current_price=current_price,
                                        allocated_budget=budget_per_position,
                                        quantity_before=original_qty, quantity_after=0,
                                        quantity_zero_reason=reason_code,
                                        **self._volatility_inputs(assessment),
                                    )
                                    self._log_trade_reason_once(
                                        logging.WARNING,
                                        reason_code,
                                        symbol,
                                        f"数量={original_qty}->0 | ATRレベル={assessment.level.value}",
                                    )
                                logger.info(
                                    "ATR数量調整: 銘柄=%s | ATR=%.3f | TR=%.3f | 倍率=%.2f | レベル=%s | 数量=%s->%s",
                                    symbol,
                                    assessment.atr,
                                    assessment.latest_true_range,
                                    assessment.ratio,
                                    assessment.level.value,
                                    original_qty,
                                    signal.qty,
                                )
                                if (
                                    assessment.level == VolatilityLevel.DANGER
                                    and config.ATR_DANGER_ACTION == "skip"
                                    and signal.qty == 0
                                    and original_qty > 0
                                ):
                                    self._record_atr_danger_skip(
                                        symbol,
                                        now,
                                        float(board['current_price']),
                                        assessment,
                                        original_qty,
                                    )
                        if self.market_regime == MarketRegime.DANGER:
                            self._journal_decision(
                                symbol, "MARKET_REGIME_DANGER_SKIP", now, current_price=current_price,
                                rsi=rsi, rsi_applied_threshold=entry_threshold,
                                allocated_budget=budget_per_position, quantity_after=signal.qty,
                                **self._volatility_inputs(assessment),
                            )
                            if signal.qty > 0:
                                self._record_filter_decision_safely(
                                    "MARKET_REGIME_DANGER_SKIP", symbol, now,
                                    float(board['current_price']), signal.qty,
                                    self._market_regime_event_inputs(rsi, entry_threshold, assessment),
                                )
                            logger.info(
                                "MarketRegimeにより新規買いを見送ります: 銘柄=%s | レジーム=%s",
                                symbol,
                                self.market_regime.value,
                            )
                            continue
                        elif self.market_regime == MarketRegime.CAUTION:
                            logger.info(
                                "MarketRegimeによりエントリーRSI基準を引き上げました: 銘柄=%s | レジーム=%s | RSI基準=%.1f",
                                symbol,
                                self.market_regime.value,
                                entry_threshold,
                            )
                    else:
                        held_quantity = next(
                            (
                                int(position.get('HoldQty', 0) or 0)
                                for position in positions
                                if position.get('Symbol') == symbol
                                and position.get('Side') == config.OrderSide.SELL.value
                            ),
                            0,
                        )
                        signal.qty = held_quantity if held_quantity > 0 else config.ORDER_UNIT
                        original_qty = signal.qty
                    if signal.qty <= 0:
                        self._journal_decision(
                            symbol, "ORDER_QUANTITY_ZERO", now, current_price=current_price,
                            quantity_before=original_qty, quantity_after=signal.qty,
                            allocated_budget=budget_per_position if signal.side == config.OrderSide.BUY else None,
                            side=signal.side.name, **self._volatility_inputs(assessment),
                        )
                        logger.info("注文数量が0のため見送ります: 銘柄=%s", symbol)
                        continue
                    if signal.side == config.OrderSide.BUY:
                        daily_pnl = self._calculate_daily_pnl(positions)
                        # 発注回数上限は新規買いだけを対象とし、決済売りは除外する。
                        daily_orders = sum(
                            1 for entry in self.order_history
                            if entry.side == config.OrderSide.BUY
                            and entry.timestamp.startswith(now.date().isoformat())
                        )
                        if not check_kill_switch(
                            daily_orders, daily_pnl, self.daily_starting_capital, config
                        ):
                            logger.warning("キルスイッチにより新規買い注文を停止しました。")
                            self._trigger_kill_switch(
                                f"日次損益または発注回数の上限超過（損益={daily_pnl:.1f}円、発注件数={daily_orders}件）"
                            )
                            kill_switch_triggered = True
                            break
                    if (
                        signal.side == config.OrderSide.BUY
                        and not is_buy_order_amount_allowed(
                            signal.price * signal.qty,
                            config,
                            api_soft_limit=self.api_soft_limit,
                        )
                    ):
                        self._journal_decision(
                            symbol, "ORDER_AMOUNT_LIMIT_EXCEEDED", now, current_price=current_price,
                            quantity_after=signal.qty, allocated_budget=budget_per_position,
                            order_amount=signal.price * signal.qty,
                        )
                        logger.warning(
                            "買い注文を見送ります: 銘柄=%s | 注文金額=%.1f円 | 上限=%.1f円",
                            symbol,
                            signal.price * signal.qty,
                            min(config.MAX_ORDER_AMOUNT_PER_TRADE, self.api_soft_limit),
                        )
                        continue
                    # 注文の安全性を確認
                    if signal.side == config.OrderSide.BUY and self._is_new_buy_halted_by_save_failure():
                        self._journal_decision(
                            symbol, "NEW_BUY_HALTED_STATE_SAVE_FAILURE", now, current_price=current_price,
                            quantity_after=signal.qty, side=signal.side.name,
                        )
                        self._log_trade_reason_once(
                            logging.ERROR,
                            "NEW_BUY_HALTED_STATE_SAVE_FAILURE",
                            symbol,
                            "状態ファイルの保存失敗が連続したため新規買いを停止中",
                        )
                        if not self._new_buy_halt_notified:
                            self._new_buy_halt_notified = True
                            self._notify_safely("\n".join([
                                "【業務】取引運用",
                                "【機能】新規買い停止",
                                "【概要】",
                                "NEW_BUY_HALTED_STATE_SAVE_FAILURE: 状態ファイルの保存失敗が"
                                f"{config.STATE_SAVE_CONSECUTIVE_FAILURE_THRESHOLD}回以上連続したため、新規の買いを停止しました。",
                                "売り・損切り・決済は継続します。",
                            ]))
                        continue
                    if not is_safe_to_order(
                        signal,
                        wallet_amount,
                        has_holdings,
                        self.order_history,
                        config.ORDER_LOCK_SECONDS,
                        warn_on_missing_holdings=False,
                        now=now,
                    ):
                        if signal.side != config.OrderSide.SELL or has_holdings:
                            self._journal_decision(
                                symbol, "ORDER_SAFETY_BLOCKED", now, current_price=current_price,
                                quantity_after=signal.qty, side=signal.side.name,
                            )
                        continue
                    # 注文を実行（成否はResultコードで判定。失敗時はNoneが返る）
                    if self.order_sender and hasattr(self.order_sender, 'set_price'):
                        self.order_sender.set_price(symbol, signal.price)
                    order_method = (
                        self.order_sender.place_market_order
                        if self.order_sender
                        else place_market_order
                    )
                    order_args = (self.token, symbol, signal.side.value, signal.qty)
                    try:
                        inspect.signature(order_method).bind(*order_args)
                    except TypeError:
                        order_args = order_args[:3]
                    order_result = order_method(*order_args)
                    if order_result and order_result.get('Result') == 0:
                        self._register_order(
                            signal,
                            limit,
                            order_result,
                            assessment,
                            diagnostics={
                                "decision_reason": decision_reason,
                                "rsi": rsi,
                                "rsi_entry_threshold": entry_threshold,
                                "rsi_exit_threshold": config.RSI_EXIT_THRESHOLD,
                                "current_price": board['current_price'],
                                "rsi_input": self._rsi_input_snapshot(
                                    symbol,
                                    now,
                                    closes,
                                    rsi,
                                    entry_threshold if signal.side == config.OrderSide.BUY else config.RSI_EXIT_THRESHOLD,
                                    current_price,
                                    threshold_kind="entry" if signal.side == config.OrderSide.BUY else "exit",
                                ),
                                "order_qty_before_atr": original_qty,
                                "allocated_budget": budget_per_position if signal.side == config.OrderSide.BUY else None,
                            },
                            shadow_previous_close=previous_close,
                        )
                        if decision_reason == "ATR損切り基準到達":
                            self._record_atr_stop_exit(
                                symbol,
                                now,
                                entry_price,
                                float(signal.price),
                                signal.qty,
                                assessment,
                                atr_stop_multiplier,
                            )
                        if (
                            signal.side == config.OrderSide.BUY
                            and getattr(self.market_regime_assessment, "trend_relief_applied", False)
                        ):
                            self._record_filter_decision_safely(
                                "ADX_TREND_RELIEF", symbol, now, float(signal.price), signal.qty,
                                self._market_regime_event_inputs(rsi, entry_threshold, assessment),
                            )
                        logger.info(
                            "%s成立: 銘柄=%s | 約定価格=%.1f | 数量=%s | 注文受付番号=%s",
                            "買い" if signal.side == config.OrderSide.BUY else "売り",
                            symbol,
                            signal.price,
                            signal.qty,
                            order_result.get('OrderId'),
                        )
                    elif order_result is None:
                        rejection_reason = getattr(self.order_sender, "last_rejection_reason", None)
                        if rejection_reason not in (
                            "ORDER_REJECTED_PAPER_PRICE_MISSING",
                            "ORDER_REJECTED_PAPER_PRICE_INVALID",
                        ):
                            rejection_reason = "ORDER_REJECTED_NONE"
                        self._journal_decision(
                            symbol, rejection_reason, now, current_price=current_price,
                            quantity_after=signal.qty, side=signal.side.name,
                        )
                        logger.error(
                            "%s: 銘柄=%s | 方向=%s | 数量=%s | 応答=None",
                            rejection_reason,
                            symbol,
                            signal.side.name,
                            signal.qty,
                        )
                    else:
                        self._journal_decision(
                            symbol, "ORDER_REJECTED_RESULT", now, current_price=current_price,
                            quantity_after=signal.qty, side=signal.side.name,
                            order_response=repr(order_result),
                        )
                        logger.error(
                            "%s: 銘柄=%s | 方向=%s | 数量=%s | 応答=%r",
                            "ORDER_REJECTED_RESULT",
                            symbol,
                            signal.side.name,
                            signal.qty,
                            order_result,
                        )
                except Exception:
                    # 想定外の例外は当該銘柄のみスキップし、ループ全体を止めない
                    logger.exception("%s の評価中に予期しないエラーが発生しました", symbol)
                    continue
            self._check_auth_recovery_notification()
            self._check_board_fetch_failure_notification(board_fetch_attempted, board_fetch_failed)
            self._flush_decision_journal_safely()
            use_preflight_market_data = False
            previous_loop_now = now
            sleep(config.LOOP_INTERVAL)

        # 最終的な評価損益を取得して報告
        if self.positions_client:
            self.last_positions = self.positions_client.get_positions(self.token) or []
        elif self.order_sender and hasattr(self.order_sender, 'get_positions'):
            self.last_positions = self.order_sender.get_positions(self.token) or []
        else:
            self.last_positions = get_positions(self.token) or []
        self._flush_decision_journal_safely()
        self._record_account_snapshot_safely("end", self._current_now or initial_now, self.last_positions)
        self._send_end_of_day_report()
