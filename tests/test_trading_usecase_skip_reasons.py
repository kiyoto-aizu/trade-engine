import json
import logging
from datetime import date, datetime
from types import SimpleNamespace

import pytest

from src.application import trading_usecase as trading_usecase_module
from src.application.trading_usecase import TradingUseCase
from src.config import config
from src.domain.enums import OrderSide
from src.domain.models import PriceLimit
from src.domain.volatility import VolatilityLevel
from src.infrastructure.market_data import get_daily_closes
from src.infrastructure.paper.paper_order_client import PaperOrderClient
from src.infrastructure.persistence.filter_decision_repository import FilterDecisionRepository

_CALCULATE_PRICE_LIMIT = trading_usecase_module.calculate_price_limit
_CALCULATE_RSI = trading_usecase_module.calculate_rsi
_REAL_PRICE_LIMIT = object()
_REAL_RSI = object()


class _OrderSender:
    def __init__(self, response=None):
        self.response = response
        self.orders = []

    def set_price(self, symbol, price):
        pass

    def place_market_order(self, token, symbol, side, quantity):
        self.orders.append((symbol, side, quantity))
        return self.response


def test_trade_judgment_logs_info_only_on_first_observation_or_state_change(tmp_path):
    use_case = TradingUseCase(token='dummy', order_history_path=tmp_path / 'unused_history.json')

    assert use_case._trade_judgment_log_level('7203', 'SELL') == logging.INFO
    assert use_case._trade_judgment_log_level('7203', 'SELL') == logging.DEBUG
    assert use_case._trade_judgment_log_level('7203', 'なし') == logging.INFO
    assert use_case._trade_judgment_log_level('7203', 'なし') == logging.DEBUG
    assert use_case._trade_judgment_log_level('7203', 'BUY') == logging.INFO
    assert use_case._trade_judgment_log_level('8306', 'BUY') == logging.INFO


def _build_loop(
    monkeypatch,
    tmp_path,
    *,
    closes=None,
    daily_bars=None,
    boards=None,
    positions=None,
    wallet_amount=100_000.0,
    order_sender=None,
    price_limit=PriceLimit(80.0, 99.0),
    rsi=60.0,
    assessment=None,
    close_minute=3,
):
    monkeypatch.setattr(config, 'TRADING_MODE', 'paper')
    monkeypatch.setattr(config, 'IS_DEMO', True)
    monkeypatch.setattr(config, 'API_SOFT_LIMIT', 100_000.0)
    monkeypatch.setattr(config, 'MAX_ORDER_AMOUNT_PER_TRADE', 30_000.0)
    monkeypatch.setattr(config, 'TARGET_POSITIONS', 3)
    monkeypatch.setattr(config, 'MAX_ORDER_COUNT_PER_DAY', 10)
    monkeypatch.setattr(config, 'DAILY_LOSS_LIMIT_RATIO', 0.02)
    monkeypatch.setattr(config, 'ATR_PERIOD', 14)
    monkeypatch.setattr(config, 'ATR_DANGER_ACTION', 'skip')
    monkeypatch.setattr(config, 'ATR_CAUTION_LOT_RATIO', 0.5)
    monkeypatch.setattr(config, 'RSI_PERIOD', 2)
    monkeypatch.setattr(config, 'RSI_MINIMUM_CLOSES', 5)
    monkeypatch.setattr(config, 'ALLOW_OVERNIGHT_HOLDING', True)
    monkeypatch.setattr(config, 'MARKET_CLOSE_HOUR', 10)
    monkeypatch.setattr(config, 'MARKET_CLOSE_MINUTE', close_minute)
    monkeypatch.setattr(config, 'EMERGENCY_STOP_FILE', tmp_path / 'emergency_stop.flag')
    monkeypatch.setattr(config, 'LOG_DIRECTORY', tmp_path / 'logs')
    if price_limit is _REAL_PRICE_LIMIT:
        monkeypatch.setattr(trading_usecase_module, 'calculate_price_limit', _CALCULATE_PRICE_LIMIT)
    else:
        monkeypatch.setattr(
            trading_usecase_module, 'calculate_price_limit', lambda values: price_limit
        )
    if rsi is _REAL_RSI:
        monkeypatch.setattr(trading_usecase_module, 'calculate_rsi', _CALCULATE_RSI)
    else:
        monkeypatch.setattr(trading_usecase_module, 'calculate_rsi', lambda *args, **kwargs: rsi)
    monkeypatch.setattr(
        trading_usecase_module,
        'assess_volatility',
        lambda *args, **kwargs: assessment,
    )

    class MarketDataClient:
        def get_yahoo_daily_closes(self, symbol):
            return [100.0] * 30 if closes is None else closes

        def get_yahoo_daily_bars(self, symbol):
            return [object()] * 14 if daily_bars is None else daily_bars

    class BoardClient:
        def __init__(self):
            self._boards = iter(boards or [{'current_price': 100.0}])
            self._last_board = boards[-1] if boards else {'current_price': 100.0}

        def get_current_board(self, token, symbol):
            return next(self._boards, self._last_board)

    class WalletClient:
        def get_wallet_cash(self, token):
            return {'StockAccountWallet': wallet_amount}

    class PositionsClient:
        def get_positions(self, token):
            return positions or []

    sender = order_sender or _OrderSender()
    repository = FilterDecisionRepository(tmp_path / 'filter_decisions.sqlite3')
    use_case = TradingUseCase(
        token='test',
        order_history_path=tmp_path / 'order_history.json',
        market_data_client=MarketDataClient(),
        board_client=BoardClient(),
        wallet_client=WalletClient(),
        positions_client=PositionsClient(),
        order_sender=sender,
        notifier=lambda message: None,
        daily_analyzer=SimpleNamespace(analyze=lambda summary: None),
        daily_report_directory=tmp_path / 'reports',
        filter_decision_repository=repository,
        kill_switch_baseline_path=tmp_path / 'kill_switch_baseline.json',
    )
    symbols_path = tmp_path / 'top_symbols.json'
    symbols_path.write_text(json.dumps(['7203']), encoding='utf-8')
    tick = [0]
    times = [datetime(2026, 9, 25, 10, minute) for minute in range(close_minute + 1)]

    def now_provider():
        return times[min(tick[0], len(times) - 1)]

    def sleep(_seconds):
        tick[0] += 1

    return use_case, sender, repository, symbols_path, now_provider, sleep


@pytest.mark.parametrize(
    ('response', 'reason_code'),
    [
        (None, 'ORDER_REJECTED_NONE'),
        ({'Result': 1, 'Message': 'rejected'}, 'ORDER_REJECTED_RESULT'),
    ],
)
def test_order_rejection_reason_is_logged_for_every_attempt(
    monkeypatch, tmp_path, caplog, response, reason_code
):
    sender = _OrderSender(response)
    use_case, _, _, symbols_path, now_provider, sleep = _build_loop(
        monkeypatch, tmp_path, order_sender=sender, close_minute=3
    )

    with caplog.at_level(logging.ERROR, logger='src.application.trading_usecase'):
        use_case.run(symbols_path, now_provider=now_provider, sleep=sleep)

    matching = [record for record in caplog.records if reason_code in record.message]
    assert len(matching) == len(sender.orders) == 3
    assert '7203' in matching[0].message
    assert 'BUY' in matching[0].message
    assert '300' in matching[0].message
    if response is None:
        assert '応答=None' in matching[0].message
    else:
        assert 'rejected' in matching[0].message
    assert len(sender.orders) > 1


def test_rsi_input_insufficient_history_is_logged_once_with_counts(monkeypatch, tmp_path, caplog):
    use_case, _, _, symbols_path, now_provider, sleep = _build_loop(
        monkeypatch,
        tmp_path,
        closes=[100.0, 101.0, 102.0],
        price_limit=_REAL_PRICE_LIMIT,
        rsi=_REAL_RSI,
    )

    with caplog.at_level(logging.WARNING, logger='src.application.trading_usecase'):
        use_case.run(symbols_path, now_provider=now_provider, sleep=sleep)

    matching = [record for record in caplog.records if 'INSUFFICIENT_RSI_HISTORY' in record.message]
    assert len(matching) == 1
    assert '必要本数=5' in matching[0].message
    assert '実績本数=3' in matching[0].message
    rsi_input = json.loads(matching[0].message.split('RSI_INPUT=', 1)[1])
    assert rsi_input['decision_at'] == '2026-09-25T10:00:00'
    assert rsi_input['symbol'] == '7203'
    assert rsi_input['market_regime'] == 'NORMAL'
    assert rsi_input['rsi'] is None
    assert rsi_input['applied_threshold'] == config.RSI_ENTRY_THRESHOLD
    assert rsi_input['close_count'] == 3
    assert rsi_input['required_close_count'] == 5
    assert rsi_input['last_close'] == 102.0
    assert rsi_input['current_price'] is None
    assert rsi_input['unavailable_reason'] == 'insufficient_close_history'
    assert rsi_input['outcome'] == 'unavailable'


def test_rsi_input_invalid_history_reason_is_logged(monkeypatch, tmp_path, caplog):
    use_case, sender, _, symbols_path, now_provider, sleep = _build_loop(
        monkeypatch,
        tmp_path,
        closes=[100.0, 101.0, 0.0, 102.0, 103.0],
        rsi=_REAL_RSI,
        price_limit=PriceLimit(99.0, 101.0),
    )

    with caplog.at_level(logging.WARNING, logger='src.application.trading_usecase'):
        use_case.run(symbols_path, now_provider=now_provider, sleep=sleep)

    matching = [record for record in caplog.records if 'RSI_DATA_UNAVAILABLE' in record.message]
    assert len(matching) == 1
    assert '必要本数=5' in matching[0].message
    assert '実績本数=5' in matching[0].message
    rsi_input = json.loads(matching[0].message.split('RSI_INPUT=', 1)[1])
    assert rsi_input['close_count'] == 5
    assert rsi_input['last_close'] == 103.0
    assert rsi_input['current_price'] == 100.0
    assert rsi_input['unavailable_reason'] == 'non_positive_close'
    assert rsi_input['outcome'] == 'unavailable'
    assert sender.orders == []


def test_board_unavailable_and_recovery_are_logged_once(monkeypatch, tmp_path, caplog):
    use_case, _, _, symbols_path, now_provider, sleep = _build_loop(
        monkeypatch,
        tmp_path,
        boards=[None, {'current_price': None}, {'current_price': 100.0}, None],
        price_limit=PriceLimit(99.0, 101.0),
        rsi=50.0,
        close_minute=4,
    )

    with caplog.at_level(logging.WARNING, logger='src.application.trading_usecase'):
        use_case.run(symbols_path, now_provider=now_provider, sleep=sleep)

    unavailable = [record for record in caplog.records if 'BOARD_UNAVAILABLE' in record.message]
    recovered = [record for record in caplog.records if 'BOARD_RECOVERED' in record.message]
    assert len(unavailable) == 1
    assert len(recovered) == 1
    assert '7203' in unavailable[0].message
    assert '7203' in recovered[0].message


@pytest.mark.parametrize(
    ('daily_bars', 'available_bars'),
    [([], 0), ([object()] * 13, 13)],
)
def test_atr_data_unavailable_for_holding_is_logged_once(
    monkeypatch, tmp_path, caplog, daily_bars, available_bars
):
    use_case, _, _, symbols_path, now_provider, sleep = _build_loop(
        monkeypatch,
        tmp_path,
        daily_bars=daily_bars,
        positions=[{
            'Symbol': '7203',
            'Side': OrderSide.SELL.value,
            'HoldQty': 100,
            'AveragePrice': 100.0,
        }],
        price_limit=PriceLimit(80.0, 120.0),
        rsi=50.0,
        assessment=None,
    )

    with caplog.at_level(logging.WARNING, logger='src.application.trading_usecase'):
        use_case.run(symbols_path, now_provider=now_provider, sleep=sleep)

    matching = [record for record in caplog.records if 'ATR_DATA_UNAVAILABLE' in record.message]
    assert len(matching) == 1
    assert '必要本数=14' in matching[0].message
    assert f'実績本数={available_bars}' in matching[0].message


def test_empty_yahoo_daily_response_is_warned_once(monkeypatch, caplog):
    monkeypatch.setattr(get_daily_closes, '_EMPTY_DAILY_WARNINGS', set(), raising=False)
    monkeypatch.setattr(get_daily_closes.request_handler, 'send_get', lambda *args, **kwargs: None)

    with caplog.at_level(logging.WARNING, logger='src.infrastructure.market_data.get_daily_closes'):
        assert get_daily_closes.get_yahoo_daily_closes('7203') == []
        assert get_daily_closes.get_yahoo_daily_closes('7203') == []

    matching = [record for record in caplog.records if 'DAILY_DATA_UNAVAILABLE' in record.message]
    assert len(matching) == 1
    assert '7203' in matching[0].message


def test_eod_missing_price_is_warned_without_changing_attempt(monkeypatch, tmp_path, caplog):
    use_case, sender, _, _, _, _ = _build_loop(
        monkeypatch,
        tmp_path,
        boards=[None],
        positions=[{
            'Symbol': '7203',
            'Side': OrderSide.SELL.value,
            'HoldQty': 100,
            'AveragePrice': 100.0,
        }],
    )

    with caplog.at_level(logging.WARNING, logger='src.application.trading_usecase'):
        use_case._liquidate_all_positions()

    matching = [record for record in caplog.records if 'EOD_PRICE_UNAVAILABLE' in record.message]
    assert len(matching) == 1
    assert sender.orders == [('7203', OrderSide.SELL.value, 100)]
    assert use_case._liquidation_results[0]['status'] == '売却注文失敗'


@pytest.mark.parametrize(
    ('wallet_amount', 'assessment', 'quantity_override', 'reason_code'),
    [
        (10_000.0, None, None, 'budget_below_one_lot'),
        (
            100_000.0,
            SimpleNamespace(atr=10.0, ratio=3.0, level=VolatilityLevel.DANGER, latest_true_range=30.0),
            None,
            'atr_danger_skip',
        ),
        (
            100_000.0,
            SimpleNamespace(atr=1.0, ratio=1.7, level=VolatilityLevel.CAUTION, latest_true_range=2.0),
            50,
            'caution_rounding_to_zero',
        ),
    ],
)
def test_zero_quantity_reason_is_logged_once(
    monkeypatch, tmp_path, caplog, wallet_amount, assessment, quantity_override, reason_code
):
    if quantity_override is not None:
        monkeypatch.setattr(
            trading_usecase_module,
            'calculate_buy_quantity',
            lambda *args, **kwargs: quantity_override,
        )
    use_case, sender, repository, symbols_path, now_provider, sleep = _build_loop(
        monkeypatch,
        tmp_path,
        wallet_amount=wallet_amount,
        assessment=assessment,
        price_limit=PriceLimit(80.0, 99.0),
        rsi=60.0,
    )

    with caplog.at_level(logging.WARNING, logger='src.application.trading_usecase'):
        use_case.run(symbols_path, now_provider=now_provider, sleep=sleep)

    matching = [record for record in caplog.records if reason_code in record.message]
    assert len(matching) == 1
    assert '7203' in matching[0].message
    assert sender.orders == []
    if reason_code == 'atr_danger_skip':
        events = repository.load_summaries()
        assert any(event['event_type'] == 'ATR_DANGER_SKIP' for event in events)


def test_zero_price_board_is_rejected_before_signal_evaluation(monkeypatch, tmp_path, caplog):
    monkeypatch.setattr(
        trading_usecase_module.TradeSignal,
        'evaluate',
        classmethod(lambda cls, symbol, current_price, *args, **kwargs: SimpleNamespace(
            symbol=symbol, side=OrderSide.BUY, price=0.0, qty=0
        )),
    )
    use_case, sender, _, symbols_path, now_provider, sleep = _build_loop(
        monkeypatch,
        tmp_path,
        boards=[{'current_price': 0.0}],
    )

    with caplog.at_level(logging.WARNING, logger='src.application.trading_usecase'):
        use_case.run(symbols_path, now_provider=now_provider, sleep=sleep)

    matching = [
        record for record in caplog.records
        if 'ORDER_REJECTED_PAPER_PRICE_INVALID' in record.message
    ]
    assert len(matching) == 1
    assert sender.orders == []


def test_danger_minimum_adjustment_to_zero_has_reason(monkeypatch, tmp_path, caplog):
    monkeypatch.setattr(config, 'ATR_DANGER_ACTION', 'minimum')
    monkeypatch.setattr(
        trading_usecase_module,
        'calculate_buy_quantity',
        lambda *args, **kwargs: 50,
    )
    assessment = SimpleNamespace(
        atr=10.0,
        ratio=3.0,
        level=VolatilityLevel.DANGER,
        latest_true_range=30.0,
    )
    use_case, sender, _, symbols_path, now_provider, sleep = _build_loop(
        monkeypatch,
        tmp_path,
        assessment=assessment,
    )
    monkeypatch.setattr(config, 'ATR_DANGER_ACTION', 'minimum')

    with caplog.at_level(logging.WARNING, logger='src.application.trading_usecase'):
        use_case.run(symbols_path, now_provider=now_provider, sleep=sleep)

    matching = [
        record for record in caplog.records
        if 'atr_quantity_adjustment_zero' in record.message
    ]
    assert len(matching) == 1
    assert sender.orders == []


def test_trading_loop_gap_past_market_close_does_not_leave_a_position(
    monkeypatch, tmp_path, caplog
):
    trade_date = date(2026, 9, 25)
    times = [
        datetime(2026, 9, 25, 15, 19),
        datetime(2026, 9, 25, 15, 31),
    ]
    tick_index = [0]

    monkeypatch.setattr(config, 'TRADING_MODE', 'paper')
    monkeypatch.setattr(config, 'IS_DEMO', True)
    monkeypatch.setattr(config, 'API_SOFT_LIMIT', 100_000.0)
    monkeypatch.setattr(config, 'MAX_ORDER_AMOUNT_PER_TRADE', 30_000.0)
    monkeypatch.setattr(config, 'TARGET_POSITIONS', 3)
    monkeypatch.setattr(config, 'ALLOW_OVERNIGHT_HOLDING', False)
    monkeypatch.setattr(config, 'MARKET_LIQUIDATION_HOUR', 15)
    monkeypatch.setattr(config, 'MARKET_LIQUIDATION_MINUTE', 20)
    monkeypatch.setattr(config, 'MARKET_CLOSE_HOUR', 15)
    monkeypatch.setattr(config, 'MARKET_CLOSE_MINUTE', 30)
    monkeypatch.setattr(config, 'EMERGENCY_STOP_FILE', tmp_path / 'emergency_stop.flag')
    monkeypatch.setattr(config, 'LOG_DIRECTORY', tmp_path / 'logs')
    monkeypatch.setattr(
        trading_usecase_module,
        'calculate_price_limit',
        lambda closes: PriceLimit(80.0, 99.0),
    )
    monkeypatch.setattr(
        trading_usecase_module,
        'calculate_rsi',
        lambda *args, **kwargs: 60.0,
    )
    monkeypatch.setattr(trading_usecase_module, 'assess_volatility', lambda *args, **kwargs: None)

    class MarketDataClient:
        def get_yahoo_daily_closes(self, symbol):
            return [100.0] * 30

        def get_yahoo_daily_bars(self, symbol):
            return []

    class BoardClient:
        def get_current_board(self, token, symbol):
            return {'current_price': 100.0}

        def get_current_board_with_freshness(self, token, symbol):
            return {
                'current_price': 100.0,
                'current_price_time': '2026-09-25T15:30:00+09:00',
                'current_price_status': 8,
            }

    order_sender = PaperOrderClient(
        prices={},
        cash=100_000.0,
        fee_rate=0.0,
        market_slippage_bps=0.0,
        state_path=None,
        realized_pnl_date=trade_date.isoformat(),
        today_provider=lambda: trade_date,
    )
    use_case = TradingUseCase(
        token='test',
        order_history_path=tmp_path / 'order_history.json',
        market_data_client=MarketDataClient(),
        board_client=BoardClient(),
        order_sender=order_sender,
        notifier=lambda message: None,
        daily_analyzer=SimpleNamespace(analyze=lambda summary: None),
        daily_report_directory=tmp_path / 'reports',
        filter_decision_repository=FilterDecisionRepository(
            tmp_path / 'filter_decisions.sqlite3'
        ),
        kill_switch_baseline_path=tmp_path / 'kill_switch_baseline.json',
    )
    symbols_path = tmp_path / 'top_symbols.json'
    symbols_path.write_text(json.dumps(['7203']), encoding='utf-8')

    def now_provider():
        return times[min(tick_index[0], len(times) - 1)]

    def sleep(_seconds):
        tick_index[0] += 1

    with caplog.at_level(logging.INFO, logger='src.application.trading_usecase'):
        use_case.run(
            top_symbols_path=symbols_path,
            now_provider=now_provider,
            sleep=sleep,
        )

    assert order_sender.orders[0]['Side'] == OrderSide.BUY.value
    assert order_sender.orders[1]['Side'] == OrderSide.SELL.value
    assert order_sender.get_positions('test') == []
    assert use_case.order_history[-1].decision_reason == 'EOD_LATE_LIQUIDATION'
    assert any('EOD_LATE_LIQUIDATION' in record.message for record in caplog.records)

def _run_and_load_report(use_case, symbols_path, now_provider, sleep, tmp_path):
    use_case.run(symbols_path, now_provider=now_provider, sleep=sleep)
    return json.loads((tmp_path / 'reports' / '2026-09-25.json').read_text(encoding='utf-8'))


def test_daily_report_records_condition_not_met_when_no_buy_signal(monkeypatch, tmp_path):
    use_case, sender, _, symbols_path, now_provider, sleep = _build_loop(
        monkeypatch, tmp_path, price_limit=PriceLimit(80.0, 110.0)
    )

    report = _run_and_load_report(use_case, symbols_path, now_provider, sleep, tmp_path)

    assert sender.orders == []
    assert report['no_trade_reason']['reason'] == '買い条件に届かず'
    assert report['trading_evaluation'] == {
        'candidate_count': 1, 'evaluated_count': 1, 'buy_signal_count': 0,
    }


def test_daily_report_has_no_reason_when_orders_were_filled(monkeypatch, tmp_path):
    use_case, sender, _, symbols_path, now_provider, sleep = _build_loop(
        monkeypatch, tmp_path, order_sender=_OrderSender({'Result': 0, 'OrderId': 'x'})
    )

    report = _run_and_load_report(use_case, symbols_path, now_provider, sleep, tmp_path)

    assert sender.orders
    assert report['order_count'] >= 1
    assert report['no_trade_reason'] is None
    assert report['trading_evaluation']['buy_signal_count'] == 1
