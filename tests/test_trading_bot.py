import json
from datetime import date, datetime, time, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.config import config
from src.domain.models import OrderHistoryEntry, PriceLimit, TradeSignal
from src.domain.market_regime import MarketRegime
from src.infrastructure.persistence.filter_decision_repository import FilterDecisionRepository
from src.domain.volatility import VolatilityLevel
from src.domain.rules import is_duplicate_order, is_market_closed, is_recent_order, is_safe_to_order
from src.application.trading_usecase import TradingUseCase
from src.infrastructure.paper.paper_order_client import PaperOrderClient
from src.entrypoints.run_trading import create_trading_use_case


@pytest.fixture(autouse=True)
def set_env(monkeypatch):
    monkeypatch.setenv('API_PASSWORD_DEV', 'dummy')
    monkeypatch.setenv('IS_DEMO', 'true')
    monkeypatch.setattr(config, "RSI_PERIOD", 2)
    monkeypatch.setattr(config, "RSI_MINIMUM_CLOSES", 5)
    monkeypatch.setattr(config, "RSI_ENTRY_THRESHOLD", 50.0)
    monkeypatch.setattr(config, "RSI_EXIT_THRESHOLD", 50.0)
    return monkeypatch


def test_required_env_helper_raises_when_missing():
    with pytest.raises(ValueError, match='Missing required environment variable: MISSING_ENV_FOR_TEST'):
        from src.config import config
        config._load_required_env('MISSING_ENV_FOR_TEST', allow_missing=False)


def test_pytest_logs_are_stored_under_data_logs_tests():
    assert Path(config.LOG_FILE_PATH) == config.LOG_DIRECTORY / 'pytest.log'
    assert config.LOG_DIRECTORY.name == 'tests'
    assert config.LOG_DIRECTORY.parent.name == 'logs'
    assert config.LOG_DIRECTORY.parent.parent.name == 'data'


def test_is_market_closed_boundary():
    assert not is_market_closed(time(15, 29), 15, 30)
    assert is_market_closed(time(15, 30), 15, 30)
    assert is_market_closed(time(16, 0), 15, 30)


def test_trade_signal_follows_trend_and_exits_when_it_weakens():
    limit = PriceLimit(lower_band=99.0, upper_band=101.0)

    entry = TradeSignal.evaluate('7203', 102.0, limit, 60.0, 55.0, 45.0)
    exit_signal = TradeSignal.evaluate('7203', 98.0, limit, 40.0, 55.0, 45.0)

    assert entry is not None
    assert entry.side == config.OrderSide.BUY
    assert exit_signal is not None
    assert exit_signal.side == config.OrderSide.SELL


def test_trading_use_case_prefers_atr_exit_over_buy_signal_for_existing_holding(monkeypatch, tmp_path):
    symbols_path = tmp_path / 'top_symbols.json'
    symbols_path.write_text(json.dumps(['7203']), encoding='utf-8')
    orders = []

    class MarketDataClient:
        def get_yahoo_daily_closes(self, symbol):
            return [100.0] * 5

        def get_yahoo_daily_bars(self, symbol):
            return [object()]

    class BoardClient:
        def get_current_board(self, token, symbol):
            return {'current_price': 90.0}

    class WalletClient:
        def get_wallet_cash(self, token):
            return {'StockAccountWallet': 100_000.0}

    class PositionsClient:
        def get_positions(self, token):
            return [{
                'Symbol': '7203',
                'Side': config.OrderSide.SELL.value,
                'HoldQty': '100',
                'AveragePrice': 100.0,
            }]

    class OrderSender:
        def set_price(self, symbol, price):
            pass

        def place_market_order(self, token, symbol, side, quantity):
            orders.append((symbol, side, quantity))
            return {'Result': 0, 'OrderId': 'sell-1'}

    monkeypatch.setattr(config, 'IS_DEMO', True)
    monkeypatch.setattr(config, 'ALLOW_OVERNIGHT_HOLDING', True)
    monkeypatch.setattr(config, 'API_SOFT_LIMIT', 100_000.0)
    monkeypatch.setattr(config, 'MAX_ORDER_AMOUNT_PER_TRADE', 100_000.0)
    monkeypatch.setattr('src.application.trading_usecase.calculate_price_limit', lambda closes: PriceLimit(70.0, 80.0))
    monkeypatch.setattr('src.application.trading_usecase.calculate_rsi', lambda *args, **kwargs: 60.0)
    monkeypatch.setattr(
        'src.application.trading_usecase.assess_volatility',
        lambda *args, **kwargs: SimpleNamespace(atr=2.0, ratio=1.0, level=VolatilityLevel.NORMAL, latest_true_range=2.0),
    )
    current_times = iter([datetime(2026, 9, 4, 10, 0), datetime(2026, 9, 4, 15, 30)])
    use_case = TradingUseCase(
        token='dummy', order_history_path=tmp_path / 'order_history.json',
        market_data_client=MarketDataClient(), board_client=BoardClient(),
        wallet_client=WalletClient(), positions_client=PositionsClient(),
        order_sender=OrderSender(), notifier=lambda message: None,
        daily_report_directory=tmp_path / 'reports',
    )

    use_case.run(
        top_symbols_path=symbols_path,
        now_provider=lambda: next(current_times),
        sleep=lambda seconds: None,
    )

    assert orders == [('7203', config.OrderSide.SELL.value, 100)]


@pytest.mark.parametrize('atr_level', [VolatilityLevel.NORMAL, VolatilityLevel.CAUTION])
def test_trading_use_case_executes_atr_exit_with_paper_order_client(
    monkeypatch, tmp_path, atr_level
):
    trade_date = date(2026, 9, 25)
    times = [
        datetime(2026, 9, 25, 10, 0),
        datetime(2026, 9, 25, 10, 1),
        datetime(2026, 9, 25, 10, 2),
        datetime(2026, 9, 25, 10, 3),
    ]
    prices = iter([100.0, 110.0, 104.0])
    tick_index = [0]

    class MarketDataClient:
        def get_yahoo_daily_closes(self, symbol):
            return [100.0] * 5

        def get_yahoo_daily_bars(self, symbol):
            return [object()]

    class BoardClient:
        def get_current_board(self, token, symbol):
            return {'current_price': next(prices)}

    monkeypatch.setattr(config, 'TRADING_MODE', 'paper')
    monkeypatch.setattr(config, 'IS_DEMO', True)
    monkeypatch.setattr(config, 'API_SOFT_LIMIT', 100_000.0)
    monkeypatch.setattr(config, 'MAX_ORDER_AMOUNT_PER_TRADE', 30_000.0)
    monkeypatch.setattr(config, 'MARKET_CLOSE_HOUR', 10)
    monkeypatch.setattr(config, 'MARKET_CLOSE_MINUTE', 3)
    monkeypatch.setattr(config, 'ALLOW_OVERNIGHT_HOLDING', False)
    monkeypatch.setattr(config, 'ATR_PROFIT_LOCK_TRIGGER_ATR_MULTIPLE', 0.5)
    monkeypatch.setattr(config, 'ATR_PROFIT_LOCK_NORMAL_MULTIPLIER', 2.5)
    monkeypatch.setattr(config, 'ATR_PROFIT_LOCK_CAUTION_MULTIPLIER', 2.0)
    monkeypatch.setattr(
        'src.application.trading_usecase.calculate_price_limit',
        lambda closes: PriceLimit(80.0, 99.0),
    )
    monkeypatch.setattr('src.application.trading_usecase.calculate_rsi', lambda *args, **kwargs: 60.0)
    monkeypatch.setattr(
        'src.application.trading_usecase.assess_volatility',
        lambda *args, **kwargs: SimpleNamespace(
            atr=2.0, ratio=1.0, level=atr_level, latest_true_range=2.0
        ),
    )
    monkeypatch.setattr(config, 'EMERGENCY_STOP_FILE', tmp_path / 'emergency_stop.flag')
    monkeypatch.setattr(config, 'LOG_DIRECTORY', tmp_path / 'logs')

    order_sender = PaperOrderClient(
        prices={},
        cash=100_000.0,
        fee_rate=0.0,
        market_slippage_bps=0.0,
        state_path=None,
        realized_pnl_date=trade_date.isoformat(),
        today_provider=lambda: trade_date,
    )
    filter_repository = FilterDecisionRepository(tmp_path / 'filter_decisions.sqlite3')
    use_case = TradingUseCase(
        token='dummy',
        order_history_path=tmp_path / 'order_history.json',
        market_data_client=MarketDataClient(),
        board_client=BoardClient(),
        order_sender=order_sender,
        notifier=lambda message: None,
        daily_analyzer=SimpleNamespace(analyze=lambda summary: None),
        daily_report_directory=tmp_path / 'reports',
        filter_decision_repository=filter_repository,
        kill_switch_baseline_path=tmp_path / 'kill_switch_baseline.json',
    )
    symbols_path = tmp_path / 'top_symbols.json'
    symbols_path.write_text(json.dumps(['7203']), encoding='utf-8')

    use_case.run(
        top_symbols_path=symbols_path,
        now_provider=lambda: times[tick_index[0]],
        sleep=lambda seconds: tick_index.__setitem__(0, tick_index[0] + 1),
    )

    assert [order['Side'] for order in order_sender.orders] == [
        config.OrderSide.BUY.value,
        config.OrderSide.SELL.value,
    ]
    assert order_sender.orders[1]['SignalPrice'] == pytest.approx(104.0)
    assert use_case.order_history[-1].decision_reason == 'ATR損切り基準到達'
    assert use_case.order_history[-1].price == pytest.approx(104.0)
    assert order_sender.get_positions('dummy') == []
    atr_exit_events = [
        event for event in filter_repository.load_summaries()
        if event['event_type'] == 'ATR_STOP_EXIT'
    ]
    assert len(atr_exit_events) == 1
    assert atr_exit_events[0]['symbol'] == '7203'


@pytest.mark.parametrize('average_price_mode', ['missing', 'none'])
def test_trading_use_case_warns_once_when_held_position_has_no_average_price(
    monkeypatch, tmp_path, caplog, average_price_mode
):
    trade_date = date(2026, 9, 25)
    times = [
        datetime(2026, 9, 25, 10, 0),
        datetime(2026, 9, 25, 10, 0, 30),
        datetime(2026, 9, 25, 10, 1),
    ]
    tick_index = [0]

    class MarketDataClient:
        def get_yahoo_daily_closes(self, symbol):
            return [100.0] * 5

        def get_yahoo_daily_bars(self, symbol):
            return [object()]

    class BoardClient:
        def get_current_board(self, token, symbol):
            return {'current_price': 90.0}

    class PositionsClient:
        def get_positions(self, token):
            position = {
                'Symbol': '7203',
                'Side': config.OrderSide.SELL.value,
                'HoldQty': 100,
            }
            if average_price_mode == 'none':
                position['AveragePrice'] = None
            return [position]

    monkeypatch.setattr(config, 'TRADING_MODE', 'paper')
    monkeypatch.setattr(config, 'IS_DEMO', True)
    monkeypatch.setattr(config, 'MARKET_CLOSE_HOUR', 10)
    monkeypatch.setattr(config, 'MARKET_CLOSE_MINUTE', 1)
    monkeypatch.setattr(config, 'EMERGENCY_STOP_FILE', tmp_path / 'emergency_stop.flag')
    monkeypatch.setattr(config, 'LOG_DIRECTORY', tmp_path / 'logs')
    monkeypatch.setattr(
        'src.application.trading_usecase.calculate_price_limit',
        lambda closes: PriceLimit(80.0, 100.0),
    )
    monkeypatch.setattr('src.application.trading_usecase.calculate_rsi', lambda *args, **kwargs: 60.0)
    monkeypatch.setattr(
        'src.application.trading_usecase.assess_volatility',
        lambda *args, **kwargs: SimpleNamespace(
            atr=2.0, ratio=1.0, level=VolatilityLevel.NORMAL, latest_true_range=2.0
        ),
    )
    order_sender = PaperOrderClient(
        prices={},
        cash=100_000.0,
        fee_rate=0.0,
        market_slippage_bps=0.0,
        state_path=None,
        realized_pnl_date=trade_date.isoformat(),
        today_provider=lambda: trade_date,
    )
    order_sender.place_market_order('dummy', '7203', config.OrderSide.BUY.value, 100)
    use_case = TradingUseCase(
        token='dummy',
        order_history_path=tmp_path / 'order_history.json',
        market_data_client=MarketDataClient(),
        board_client=BoardClient(),
        positions_client=PositionsClient(),
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

    with caplog.at_level('WARNING', logger='src.application.trading_usecase'):
        use_case.run(
            top_symbols_path=symbols_path,
            now_provider=lambda: times[tick_index[0]],
            sleep=lambda seconds: tick_index.__setitem__(0, tick_index[0] + 1),
        )

    warnings = [
        record for record in caplog.records
        if 'ATR_ENTRY_PRICE_UNAVAILABLE' in record.message
    ]
    assert len(warnings) == 1
    assert '7203' in warnings[0].message
    assert '100' in warnings[0].message


def test_order_history_load_corrupt(tmp_path):
    history_file = tmp_path / 'order_history.json'
    history_file.write_text('not-json', encoding='utf-8')

    use_case = TradingUseCase(token='dummy', order_history_path=history_file)
    with pytest.raises(ValueError):
        use_case._load_order_history()


@pytest.mark.parametrize('timestamp', ['not-a-timestamp', None, 12345, ''])
def test_order_history_load_rejects_invalid_timestamp_and_notifies(
    monkeypatch, tmp_path, timestamp
):
    history_file = tmp_path / 'order_history.json'
    history_file.write_text(json.dumps([{
        'symbol': '7203',
        'side': '2',
        'price': 100.0,
        'qty': 100,
        'timestamp': timestamp,
    }]), encoding='utf-8')
    critical_messages = []
    monkeypatch.setattr(
        'src.application.trading_usecase.notify_critical', critical_messages.append
    )
    use_case = TradingUseCase(token='dummy', order_history_path=history_file)

    with pytest.raises(ValueError) as exc_info:
        use_case.run()

    assert '行番号は0始まり' in str(exc_info.value)
    assert '行=0' in str(exc_info.value)
    assert "銘柄='7203'" in str(exc_info.value)
    assert repr(timestamp) in str(exc_info.value)
    assert len(critical_messages) == 1
    assert '行=0' in critical_messages[0]
    assert repr(timestamp) in critical_messages[0]


def test_order_history_load_aggregates_invalid_rows(tmp_path):
    history_file = tmp_path / 'order_history.json'
    history_file.write_text(json.dumps([
        {'symbol': '7203', 'side': '2', 'price': 100.0, 'qty': 100, 'timestamp': 'bad'},
        {'symbol': '6758', 'side': '1', 'price': 200.0, 'timestamp': '2026-09-04T10:00:00'},
    ]), encoding='utf-8')
    use_case = TradingUseCase(token='dummy', order_history_path=history_file)

    with pytest.raises(ValueError) as exc_info:
        use_case._load_order_history()

    message = str(exc_info.value)
    assert '不正行数=2' in message
    assert "行=0 銘柄='7203'" in message
    assert "timestampを解釈できません: 'bad'" in message
    assert "行=1 銘柄='6758'" in message
    assert 'qty=<欠落>' in message


@pytest.mark.parametrize(
    ('field', 'value', 'expected'),
    [
        ('symbol', 7203, 'symbolの型が不正'),
        ('side', 'BUY', 'sideが不正'),
        ('price', '100', 'priceの型が不正'),
        ('qty', 100.0, 'qtyの型が不正'),
        ('timestamp', '2026-09-04T10:00:00', 'timestamp=<欠落>'),
    ],
)
def test_order_history_load_rejects_invalid_required_fields(
    tmp_path, field, value, expected
):
    history_file = tmp_path / 'order_history.json'
    row = {
        'symbol': '7203',
        'side': '2',
        'price': 100.0,
        'qty': 100,
        'timestamp': '2026-09-04T10:00:00',
    }
    if field == 'timestamp' and value == '2026-09-04T10:00:00':
        row.pop(field)
    else:
        row[field] = value
    history_file.write_text(json.dumps([row]), encoding='utf-8')
    use_case = TradingUseCase(token='dummy', order_history_path=history_file)

    with pytest.raises(ValueError) as exc_info:
        use_case._load_order_history()

    assert '行=0' in str(exc_info.value)
    assert expected in str(exc_info.value)


@pytest.mark.parametrize('field', ['symbol', 'side', 'price', 'qty', 'timestamp'])
def test_order_history_load_rejects_missing_required_fields(tmp_path, field):
    history_file = tmp_path / 'order_history.json'
    row = {
        'symbol': '7203',
        'side': '2',
        'price': 100.0,
        'qty': 100,
        'timestamp': '2026-09-04T10:00:00',
    }
    row.pop(field)
    history_file.write_text(json.dumps([row]), encoding='utf-8')
    use_case = TradingUseCase(token='dummy', order_history_path=history_file)

    with pytest.raises(ValueError, match=f'{field}=<欠落>'):
        use_case._load_order_history()


def test_order_history_load_accepts_naive_and_future_iso_timestamps(tmp_path):
    history_file = tmp_path / 'order_history.json'
    history_file.write_text(json.dumps([{
        'symbol': '7203',
        'side': '2',
        'price': 100.0,
        'qty': 100,
        'timestamp': '2099-09-04T10:00:00',
    }]), encoding='utf-8')
    use_case = TradingUseCase(token='dummy', order_history_path=history_file)

    use_case._load_order_history()

    assert len(use_case.order_history) == 1
    assert use_case.order_history[0].timestamp == '2099-09-04T10:00:00'


@pytest.mark.parametrize('timestamp', [None, 12345])
def test_order_history_timestamp_type_errors_warn_and_do_not_escape(
    caplog, timestamp
):
    signal = TradeSignal('7203', config.OrderSide.BUY, 100.0, 100)
    entry = OrderHistoryEntry(
        symbol='7203', side=config.OrderSide.BUY, price=100.0, qty=100,
        timestamp=timestamp,
    )

    with caplog.at_level('WARNING', logger='src.domain.rules'):
        assert not is_duplicate_order(signal, [entry], now=datetime(2026, 9, 4, 10))
        assert not is_recent_order(
            signal, [entry], lock_seconds=60, now=datetime(2026, 9, 4, 10)
        )

    warnings = [record for record in caplog.records if 'ORDER_HISTORY_TIMESTAMP_INVALID' in record.message]
    assert len(warnings) == 2
    assert all('銘柄=7203' in record.message for record in warnings)
    assert all('方向=2' in record.message for record in warnings)
    assert all(f'timestamp={timestamp!r}' in record.message for record in warnings)


def test_recent_order_timezone_mismatch_warns_without_raising(caplog):
    signal = TradeSignal('7203', config.OrderSide.BUY, 100.0, 100)
    entry = OrderHistoryEntry(
        symbol='7203', side=config.OrderSide.BUY, price=100.0, qty=100,
        timestamp='2026-09-04T10:00:00+09:00',
    )

    with caplog.at_level('WARNING', logger='src.domain.rules'):
        assert not is_recent_order(
            signal, [entry], lock_seconds=60, now=datetime(2026, 9, 4, 10, 0, 30)
        )

    assert any('ORDER_HISTORY_TIMESTAMP_INVALID' in record.message for record in caplog.records)


def test_order_history_rsi_input_roundtrip_and_legacy_compatibility(tmp_path):
    history_file = tmp_path / 'order_history.json'
    rsi_input = {
        'decision_at': '2026-09-04T10:00:00',
        'symbol': '7203',
        'market_regime': 'NORMAL',
        'rsi': 61.25,
        'applied_threshold': 55.0,
        'threshold_kind': 'entry',
        'calculation_status': 'available',
        'unavailable_reason': None,
        'close_count': 30,
        'required_close_count': 30,
        'last_close': 101.0,
        'current_price': 102.0,
        'outcome': 'passed',
    }
    entry = OrderHistoryEntry(
        symbol='7203',
        side=config.OrderSide.BUY,
        price=102.0,
        qty=100,
        timestamp='2026-09-04T10:00:00',
        rsi_input=rsi_input,
    )
    history_file.write_text(json.dumps([entry.to_dict()]), encoding='utf-8')

    reloaded = TradingUseCase(token='dummy', order_history_path=history_file)
    reloaded._load_order_history()

    assert reloaded.order_history[0].rsi_input == rsi_input

    legacy_record = entry.to_dict()
    legacy_record.pop('rsi_input')
    history_file.write_text(json.dumps([legacy_record]), encoding='utf-8')
    legacy_reloaded = TradingUseCase(token='dummy', order_history_path=history_file)
    legacy_reloaded._load_order_history()

    assert legacy_reloaded.order_history[0].rsi_input is None


def test_order_history_register_records_audit_fields(tmp_path):
    history_file = tmp_path / 'order_history.json'
    use_case = TradingUseCase(token='dummy', order_history_path=history_file)
    use_case._load_order_history()

    signal = TradeSignal(symbol='1475', side=config.OrderSide.BUY, price=1000.0, qty=100)
    limit = PriceLimit(lower_band=990.0, upper_band=1010.0)
    messages = []
    use_case.notifier = messages.append
    use_case._register_order(
        signal,
        limit,
        {'Result': 0, 'OrderId': 'abc123'},
        diagnostics={'allocated_budget': 30_000.0},
    )

    assert len(use_case.order_history) == 1
    entry = use_case.order_history[0]
    assert entry.symbol == '1475'
    assert entry.result_code == 0
    assert entry.order_id == 'abc123'
    assert entry.basis_lower_band == 990.0
    assert entry.basis_upper_band == 1010.0
    assert messages[0].startswith('【業務】取引運用\n【機能】注文執行')
    assert '買い注文が成立しました。' in messages[0]
    assert '銘柄: 1475' in messages[0]
    assert '割当予算: 30,000円' in messages[0]

    # 再読み込みしても永続化されていること
    reloaded = TradingUseCase(token='dummy', order_history_path=history_file)
    reloaded._load_order_history()
    assert len(reloaded.order_history) == 1
    assert reloaded.order_history[0].order_id == 'abc123'
    assert reloaded.order_history[0].allocated_budget == 30_000.0


def test_order_history_and_end_of_day_report_use_injected_clock(tmp_path):
    simulated_now = datetime(2026, 9, 25, 15, 30, 1)
    messages = []
    use_case = TradingUseCase(
        token='dummy',
        order_history_path=tmp_path / 'order_history.json',
        notifier=messages.append,
        daily_analyzer=SimpleNamespace(analyze=lambda daily_summary: None),
        daily_report_directory=tmp_path / 'reports',
    )
    use_case._now_provider = lambda: simulated_now
    use_case._load_order_history()
    use_case._register_order(
        TradeSignal('7203', config.OrderSide.BUY, 100.0, 100),
        PriceLimit(99.0, 101.0),
        {'Result': 0, 'OrderId': 'historical-order'},
    )

    use_case._send_end_of_day_report()

    report = json.loads(
        (tmp_path / 'reports' / '2026-09-25.json').read_text(encoding='utf-8')
    )
    assert use_case.order_history[0].timestamp == simulated_now.isoformat()
    assert report['date'] == '2026-09-25'
    assert report['generated_at'] == simulated_now.isoformat(timespec='seconds')
    assert report['order_count'] == 1
    assert report['orders'][0]['symbol'] == '7203'
    assert '約定件数: 1件' in messages[-1]
    assert '約定価格:' not in messages[-1]
    assert '判断理由:' not in messages[-1]


def test_order_guards_compare_history_against_injected_simulation_time():
    simulated_now = datetime(2026, 9, 25, 10, 0)
    signal = TradeSignal('7203', config.OrderSide.BUY, 100.0, 100)
    previous_order = OrderHistoryEntry(
        symbol='7203',
        side=config.OrderSide.BUY,
        price=100.0,
        qty=100,
        timestamp=(simulated_now - timedelta(minutes=1)).isoformat(),
    )

    assert is_duplicate_order(signal, [previous_order], now=simulated_now)
    assert is_recent_order(signal, [previous_order], 60, now=simulated_now)
    assert not is_recent_order(
        signal,
        [previous_order],
        30,
        now=simulated_now,
    )
    assert not is_safe_to_order(
        signal,
        wallet_amount=20_000.0,
        has_holdings=False,
        order_history=[previous_order],
        lock_seconds=60,
        now=simulated_now,
    )


def test_has_holdings_checks_sell_side_positions(tmp_path):
    use_case = TradingUseCase(token='dummy', order_history_path=tmp_path / 'unused_history.json')
    positions = [
        {'Symbol': '1475', 'Side': config.OrderSide.SELL.value, 'HoldQty': '100'},
        {'Symbol': '7203', 'Side': '1', 'HoldQty': '0'},
    ]

    assert use_case._has_holdings('1475', positions)
    assert not use_case._has_holdings('7203', positions)
    assert not use_case._has_holdings('9999', positions)


def test_count_open_positions_counts_only_open_sell_side_positions(tmp_path):
    use_case = TradingUseCase(token='dummy', order_history_path=tmp_path / 'unused_history.json')
    positions = [
        {'Symbol': '1475', 'Side': config.OrderSide.SELL.value, 'HoldQty': '100'},
        {'Symbol': '7203', 'Side': config.OrderSide.SELL.value, 'HoldQty': '0'},
        {'Symbol': '8306', 'Side': config.OrderSide.BUY.value, 'HoldQty': '100'},
    ]

    assert use_case._count_open_positions(positions) == 1


def test_trading_use_case_uses_daily_starting_total_equity_for_kill_switch(monkeypatch, tmp_path):
    symbols_path = tmp_path / 'top_symbols.json'
    symbols_path.write_text(json.dumps(['7203']), encoding='utf-8')
    orders = []

    class MarketDataClient:
        def get_yahoo_daily_closes(self, symbol):
            return [90.0] * config.RSI_MINIMUM_CLOSES

    class BoardClient:
        def get_current_board(self, token, symbol):
            return {'current_price': 92.0}

    class WalletClient:
        def get_wallet_cash(self, token):
            return {'StockAccountWallet': 200_000.0}

    class PositionsClient:
        def get_positions(self, token):
            return [
                {
                    'Symbol': '1301',
                    'Side': config.OrderSide.BUY.value,
                    'HoldQty': '100',
                    'CurrentPrice': '1000',
                    'ProfitLoss': '-3000',
                }
            ]

    class OrderSender:
        def place_market_order(self, token, symbol, side, qty):
            orders.append((symbol, side, qty))
            return {'Result': 0, 'OrderId': 'order-1'}

    monkeypatch.setattr(config, 'IS_DEMO', True)
    monkeypatch.setattr(config, 'OPERATING_CAPITAL', 100_000.0)
    monkeypatch.setattr(config, 'DAILY_LOSS_LIMIT_RATIO', 0.02)
    monkeypatch.setattr(config, 'API_SOFT_LIMIT', 100_000.0)
    current_times = iter([datetime(2026, 9, 4, 10, 0), datetime(2026, 9, 4, 15, 30)])
    use_case = TradingUseCase(
        token='dummy', order_history_path=tmp_path / 'order_history.json',
        market_data_client=MarketDataClient(), board_client=BoardClient(),
        wallet_client=WalletClient(), positions_client=PositionsClient(),
        order_sender=OrderSender(), notifier=lambda message: None,
        daily_report_directory=tmp_path / 'reports',
    )

    use_case.run(top_symbols_path=symbols_path, now_provider=lambda: next(current_times), sleep=lambda seconds: None)

    assert use_case.daily_starting_capital == 300_000.0
    assert [(symbol, side) for symbol, side, _ in orders] == [
        ('7203', config.OrderSide.BUY.value)
    ]
    assert use_case.kill_switch_triggered is False


def test_daily_starting_capital_is_restored_for_same_day_restart(tmp_path):
    baseline_path = tmp_path / 'kill_switch_baseline.json'
    use_case = TradingUseCase(
        token='dummy',
        order_history_path=tmp_path / 'order_history.json',
        kill_switch_baseline_path=baseline_path,
    )

    use_case._initialize_daily_starting_capital('2026-09-25', 100_000.0, [])
    use_case._initialize_daily_starting_capital('2026-09-25', 1.0, [])

    assert use_case.daily_starting_capital == 100_000.0
    assert json.loads(baseline_path.read_text(encoding='utf-8')) == {
        'date': '2026-09-25',
        'capital': 100_000.0,
    }


def test_daily_starting_capital_is_recalculated_when_date_changes(tmp_path):
    baseline_path = tmp_path / 'kill_switch_baseline.json'
    baseline_path.write_text(
        json.dumps({'date': '2026-09-24', 'capital': 100_000.0}),
        encoding='utf-8',
    )
    use_case = TradingUseCase(
        token='dummy',
        order_history_path=tmp_path / 'order_history.json',
        kill_switch_baseline_path=baseline_path,
    )

    use_case._initialize_daily_starting_capital(
        '2026-09-25',
        80_000.0,
        [{'HoldQty': 100, 'CurrentPrice': 250.0}],
    )

    assert use_case.daily_starting_capital == 105_000.0
    assert json.loads(baseline_path.read_text(encoding='utf-8'))['date'] == '2026-09-25'


def test_paper_prices_are_warmed_for_existing_holdings_only(tmp_path):
    class BoardClient:
        def __init__(self):
            self.calls = []

        def get_current_board(self, token, symbol):
            self.calls.append(symbol)
            return {'current_price': 110.0}

    executor = PaperOrderClient(
        prices={}, cash=20_000.0, order_qty=100,
        fee_rate=0.0, market_slippage_bps=0.0,
    )
    executor.holdings = {'7203': 100}
    executor.average_costs = {'7203': 100.0}
    board_client = BoardClient()
    use_case = TradingUseCase(
        token='dummy',
        order_history_path=tmp_path / 'order_history.json',
        board_client=board_client,
        order_sender=executor,
    )

    use_case._initialize_paper_prices()

    assert board_client.calls == ['7203']
    assert executor.get_positions('unused')[0]['ProfitLoss'] == 1_000.0

    empty_board_client = BoardClient()
    empty_executor = PaperOrderClient(prices={})
    empty_use_case = TradingUseCase(
        token='dummy',
        order_history_path=tmp_path / 'empty-order-history.json',
        board_client=empty_board_client,
        order_sender=empty_executor,
    )
    empty_use_case._initialize_paper_prices()
    assert empty_board_client.calls == []


def test_trading_use_case_falls_back_to_operating_capital_when_starting_equity_is_zero(monkeypatch, tmp_path):
    symbols_path = tmp_path / 'top_symbols.json'
    symbols_path.write_text(json.dumps(['7203']), encoding='utf-8')

    class WalletClient:
        def get_wallet_cash(self, token):
            return None

    class PositionsClient:
        def get_positions(self, token):
            return []

    monkeypatch.setattr(config, 'OPERATING_CAPITAL', 123_456.0)
    use_case = TradingUseCase(
        token='dummy', order_history_path=tmp_path / 'order_history.json',
        wallet_client=WalletClient(), positions_client=PositionsClient(),
        notifier=lambda message: None,
        daily_report_directory=tmp_path / 'reports',
    )

    use_case.run(
        top_symbols_path=symbols_path,
        now_provider=lambda: datetime(2026, 9, 4, 15, 30),
        sleep=lambda seconds: None,
    )

    assert use_case.daily_starting_capital == 123_456.0


def test_trading_use_case_sizes_new_buy_from_current_wallet(monkeypatch, tmp_path):
    symbols_path = tmp_path / 'top_symbols.json'
    symbols_path.write_text(json.dumps(['7203']), encoding='utf-8')
    orders = []

    class MarketDataClient:
        def get_yahoo_daily_closes(self, symbol):
            return [90.0] * config.RSI_MINIMUM_CLOSES

    class BoardClient:
        def get_current_board(self, token, symbol):
            return {'current_price': 92.0}

    class WalletClient:
        def get_wallet_cash(self, token):
            return {'StockAccountWallet': 100_000.0}

    class PositionsClient:
        def get_positions(self, token):
            return []

    class OrderSender:
        def place_market_order(self, token, symbol, side, qty):
            orders.append((symbol, side, qty))
            return {'Result': 0, 'OrderId': 'order-1'}

    monkeypatch.setattr(config, 'IS_DEMO', True)
    monkeypatch.setattr(config, 'TARGET_POSITIONS', 3)
    monkeypatch.setattr(config, 'MAX_ORDER_AMOUNT_PER_TRADE', 30_000.0)
    monkeypatch.setattr(config, 'API_SOFT_LIMIT', 100_000.0)
    clock_calls = 0

    def now_provider():
        nonlocal clock_calls
        clock_calls += 1
        return datetime(2026, 9, 4, 10, 0) if clock_calls <= 2 else datetime(2026, 9, 4, 15, 30)

    use_case = TradingUseCase(
        token='dummy', order_history_path=tmp_path / 'order_history.json',
        market_data_client=MarketDataClient(), board_client=BoardClient(),
        wallet_client=WalletClient(), positions_client=PositionsClient(),
        order_sender=OrderSender(), notifier=lambda message: None,
        daily_report_directory=tmp_path / 'reports',
    )

    use_case.run(top_symbols_path=symbols_path, now_provider=now_provider, sleep=lambda seconds: None)

    assert orders == [('7203', config.OrderSide.BUY.value, 300)]


def test_trading_use_case_sizes_new_buy_by_remaining_position_slots(monkeypatch, tmp_path):
    symbols_path = tmp_path / 'top_symbols.json'
    symbols_path.write_text(json.dumps(['7203']), encoding='utf-8')
    orders = []

    class MarketDataClient:
        def get_yahoo_daily_closes(self, symbol):
            return [2_000.0] * config.RSI_MINIMUM_CLOSES

    class BoardClient:
        def get_current_board(self, token, symbol):
            return {'current_price': 2_200.0}

    class WalletClient:
        def get_wallet_cash(self, token):
            return {'StockAccountWallet': 500_000.0}

    class PositionsClient:
        def get_positions(self, token):
            return [
                {'Symbol': symbol, 'Side': config.OrderSide.SELL.value, 'HoldQty': '100'}
                for symbol in ('1301', '1332', '1605')
            ]

    class OrderSender:
        def place_market_order(self, token, symbol, side, qty):
            orders.append((symbol, side, qty))
            return {'Result': 0, 'OrderId': 'order-1'}

    monkeypatch.setattr(config, 'IS_DEMO', True)
    monkeypatch.setattr(config, 'ALLOW_OVERNIGHT_HOLDING', True)
    monkeypatch.setattr(config, 'TARGET_POSITIONS', 5)
    monkeypatch.setattr(config, 'MAX_ORDER_AMOUNT_PER_TRADE', 1_000_000.0)
    monkeypatch.setattr(config, 'API_SOFT_LIMIT', 1_000_000.0)
    current_times = iter([datetime(2026, 9, 4, 10, 0), datetime(2026, 9, 4, 15, 30)])
    use_case = TradingUseCase(
        token='dummy', order_history_path=tmp_path / 'order_history.json',
        market_data_client=MarketDataClient(), board_client=BoardClient(),
        wallet_client=WalletClient(), positions_client=PositionsClient(),
        order_sender=OrderSender(), notifier=lambda message: None,
        daily_report_directory=tmp_path / 'reports',
    )

    use_case.run(top_symbols_path=symbols_path, now_provider=lambda: next(current_times), sleep=lambda seconds: None)

    # Fixed five-slot allocation would allow only 100,000 yen, below one 220,000-yen lot.
    assert orders == [('7203', config.OrderSide.BUY.value, 100)]


def test_trading_use_case_uses_one_remaining_slot_for_existing_holding_at_position_limit(monkeypatch, tmp_path):
    symbols_path = tmp_path / 'top_symbols.json'
    symbols_path.write_text(json.dumps(['7203']), encoding='utf-8')
    orders = []

    class MarketDataClient:
        def get_yahoo_daily_closes(self, symbol):
            return [2_000.0] * config.RSI_MINIMUM_CLOSES

    class BoardClient:
        def get_current_board(self, token, symbol):
            return {'current_price': 2_200.0}

    class WalletClient:
        def get_wallet_cash(self, token):
            return {'StockAccountWallet': 500_000.0}

    class PositionsClient:
        def get_positions(self, token):
            return [
                {'Symbol': symbol, 'Side': config.OrderSide.SELL.value, 'HoldQty': '100'}
                for symbol in ('7203', '1301', '1332', '1605', '1801')
            ]

    class OrderSender:
        def place_market_order(self, token, symbol, side, qty):
            orders.append((symbol, side, qty))
            return {'Result': 0, 'OrderId': 'order-1'}

    monkeypatch.setattr(config, 'IS_DEMO', True)
    monkeypatch.setattr(config, 'ALLOW_OVERNIGHT_HOLDING', True)
    monkeypatch.setattr(config, 'TARGET_POSITIONS', 5)
    monkeypatch.setattr(config, 'MAX_ORDER_AMOUNT_PER_TRADE', 1_000_000.0)
    monkeypatch.setattr(config, 'API_SOFT_LIMIT', 1_000_000.0)
    current_times = iter([datetime(2026, 9, 4, 10, 0), datetime(2026, 9, 4, 15, 30)])
    use_case = TradingUseCase(
        token='dummy', order_history_path=tmp_path / 'order_history.json',
        market_data_client=MarketDataClient(), board_client=BoardClient(),
        wallet_client=WalletClient(), positions_client=PositionsClient(),
        order_sender=OrderSender(), notifier=lambda message: None,
        daily_report_directory=tmp_path / 'reports',
    )

    use_case.run(top_symbols_path=symbols_path, now_provider=lambda: next(current_times), sleep=lambda seconds: None)

    assert orders == [('7203', config.OrderSide.BUY.value, 200)]


def test_trading_use_case_keeps_buy_budget_caps_with_remaining_position_slots(monkeypatch, tmp_path):
    symbols_path = tmp_path / 'top_symbols.json'
    symbols_path.write_text(json.dumps(['7203']), encoding='utf-8')
    orders = []

    class MarketDataClient:
        def get_yahoo_daily_closes(self, symbol):
            return [900.0] * config.RSI_MINIMUM_CLOSES

    class BoardClient:
        def get_current_board(self, token, symbol):
            return {'current_price': 1_000.0}

    class WalletClient:
        def get_wallet_cash(self, token):
            return {'StockAccountWallet': 500_000.0}

    class PositionsClient:
        def get_positions(self, token):
            return [
                {'Symbol': symbol, 'Side': config.OrderSide.SELL.value, 'HoldQty': '100'}
                for symbol in ('1301', '1332', '1605')
            ]

    class OrderSender:
        def place_market_order(self, token, symbol, side, qty):
            orders.append((symbol, side, qty))
            return {'Result': 0, 'OrderId': 'order-1'}

    monkeypatch.setattr(config, 'IS_DEMO', True)
    monkeypatch.setattr(config, 'ALLOW_OVERNIGHT_HOLDING', True)
    monkeypatch.setattr(config, 'TARGET_POSITIONS', 5)
    monkeypatch.setattr(config, 'MAX_ORDER_AMOUNT_PER_TRADE', 300_000.0)
    monkeypatch.setattr(config, 'API_SOFT_LIMIT', 150_000.0)
    current_times = iter([datetime(2026, 9, 4, 10, 0), datetime(2026, 9, 4, 15, 30)])
    use_case = TradingUseCase(
        token='dummy', order_history_path=tmp_path / 'order_history.json',
        market_data_client=MarketDataClient(), board_client=BoardClient(),
        wallet_client=WalletClient(), positions_client=PositionsClient(),
        order_sender=OrderSender(), notifier=lambda message: None,
        daily_report_directory=tmp_path / 'reports',
    )

    use_case.run(top_symbols_path=symbols_path, now_provider=lambda: next(current_times), sleep=lambda seconds: None)

    assert orders == [('7203', config.OrderSide.BUY.value, 100)]


def test_trading_use_case_monitors_all_candidates_when_position_limit_is_reached(monkeypatch, tmp_path, caplog):
    symbols = [str(1000 + index) for index in range(10)]
    symbols_path = tmp_path / 'top_symbols.json'
    symbols_path.write_text(json.dumps(symbols), encoding='utf-8')
    observed_symbols = []
    caplog.set_level('INFO', logger='src.application.trading_usecase')

    class MarketDataClient:
        def get_yahoo_daily_closes(self, symbol):
            return [90.0] * config.RSI_MINIMUM_CLOSES

    class BoardClient:
        def get_current_board(self, token, symbol):
            observed_symbols.append(symbol)
            return {'current_price': 92.0}

    class WalletClient:
        def get_wallet_cash(self, token):
            return {'StockAccountWallet': 100_000.0}

    class PositionsClient:
        def get_positions(self, token):
            return [
                {'Symbol': str(index), 'Side': config.OrderSide.SELL.value, 'HoldQty': '100'}
                for index in range(3)
            ]

    class OrderSender:
        def place_market_order(self, *args):
            raise AssertionError('保有上限時の新規注文は実行されない')

    monkeypatch.setattr(config, 'IS_DEMO', True)
    monkeypatch.setattr(config, 'TARGET_POSITIONS', 3)
    monkeypatch.setattr(config, 'API_SOFT_LIMIT', 100_000.0)
    clock_calls = 0

    def now_provider():
        nonlocal clock_calls
        clock_calls += 1
        return datetime(2026, 9, 4, 10, 0) if clock_calls == 1 else datetime(2026, 9, 4, 15, 30)

    use_case = TradingUseCase(
        token='dummy', order_history_path=tmp_path / 'order_history.json',
        market_data_client=MarketDataClient(), board_client=BoardClient(),
        wallet_client=WalletClient(), positions_client=PositionsClient(),
        order_sender=OrderSender(), notifier=lambda message: None,
        daily_report_directory=tmp_path / 'reports',
    )

    use_case.run(top_symbols_path=symbols_path, now_provider=now_provider, sleep=lambda seconds: None)

    assert observed_symbols == symbols
    assert '保有上限のため新規買いを見送ります' in caplog.text


def test_trading_use_case_factory_uses_paper_executor_by_default(monkeypatch, tmp_path):
    monkeypatch.setattr(config, 'FILTER_DECISION_DATABASE_FILE', tmp_path / 'filter_decisions.sqlite3')
    monkeypatch.setattr(config, 'TRADING_MODE', 'paper')
    monkeypatch.setattr(config, 'IS_DEMO', True)
    monkeypatch.setattr(config, 'ENABLE_LIVE_ORDERING', False)

    bot = create_trading_use_case(token='dummy')

    assert isinstance(bot.order_sender, PaperOrderClient)


def test_trading_use_case_factory_rejects_live_mode_without_explicit_production_settings(monkeypatch):
    monkeypatch.setattr(config, 'TRADING_MODE', 'live')
    monkeypatch.setattr(config, 'IS_DEMO', True)
    monkeypatch.setattr(config, 'ENABLE_LIVE_ORDERING', True)

    with pytest.raises(ValueError, match='ライブ注文には'):
        create_trading_use_case(token='dummy')


def test_end_of_day_report_identifies_paper_trading(monkeypatch, tmp_path):
    monkeypatch.setattr(config, 'TRADING_MODE_LABEL', 'ペーパートレード')
    messages = []
    use_case = TradingUseCase(
        token='dummy',
        order_history_path=tmp_path / 'order_history.json',
        notifier=messages.append,
        daily_analyzer=SimpleNamespace(analyze=lambda daily_summary: None),
        daily_report_directory=tmp_path / 'reports',
    )

    use_case._send_end_of_day_report()

    assert messages[0].startswith('【業務】取引\n【機能】トレード終了')
    report = json.loads((tmp_path / 'reports' / f'{datetime.now().date().isoformat()}.json').read_text(encoding='utf-8'))
    assert report['order_count'] == 0
    assert 'LLM日次評価' not in report['report_text']
    assert '実現損益:' in messages[0]
    assert '評価損益:' in messages[0]
    assert '強制売却確認:' in messages[0]


def test_end_of_day_report_keeps_market_conditions_in_data_but_not_notification(tmp_path):
    messages = []
    use_case = TradingUseCase(
        token='dummy',
        order_history_path=tmp_path / 'order_history.json',
        notifier=messages.append,
        daily_report_directory=tmp_path / 'reports',
    )
    use_case.market_regime = MarketRegime.CAUTION
    use_case.market_regime_assessment = SimpleNamespace(
        realized_volatility_percent=21.5,
        vix=19.2,
        nikkei_change_percent=-1.1,
        adx=24.0,
        data_available=True,
        failure_reason=None,
    )
    use_case._register_order(
        TradeSignal('7203', config.OrderSide.BUY, 100.0, 100),
        PriceLimit(95.0, 105.0),
        {'Result': 0, 'OrderId': 'order-1'},
        SimpleNamespace(
            atr=3.2,
            latest_true_range=4.8,
            ratio=1.5,
            level=VolatilityLevel.CAUTION,
        ),
    )

    use_case._send_end_of_day_report()

    report = json.loads((tmp_path / 'reports' / f'{datetime.now().date().isoformat()}.json').read_text(encoding='utf-8'))
    assert report['market_conditions'] == {
        'assessment_status': 'available',
        'regime': 'CAUTION',
        'realized_volatility_percent': 21.5,
        'vix': 19.2,
        'nikkei_change_percent': -1.1,
        'adx': 24.0,
        'data_available': True,
        'failure_reason': None,
        'nikkei_fallback_used': False,
    }
    assert report['orders'][0]['atr'] == 3.2
    assert report['orders'][0]['atr_level'] == 'CAUTION'
    assert 'MarketRegime: CAUTION' not in messages[-1]
    assert 'ATR: 3.200円' not in messages[-1]
    assert '約定件数: 1件' in messages[-1]
    assert '銘柄: 7203' not in messages[-1]


def test_end_of_day_report_marks_market_conditions_not_evaluated(tmp_path):
    use_case = TradingUseCase(
        token='dummy',
        order_history_path=tmp_path / 'order_history.json',
        notifier=lambda message: None,
        daily_analyzer=SimpleNamespace(analyze=lambda daily_summary: None),
        daily_report_directory=tmp_path / 'reports',
    )

    use_case._send_end_of_day_report()

    report = json.loads((tmp_path / 'reports' / f'{datetime.now().date().isoformat()}.json').read_text(encoding='utf-8'))
    assert report['market_conditions'] == {
        'assessment_status': 'not_evaluated',
        'regime': None,
        'realized_volatility_percent': None,
        'vix': None,
        'nikkei_change_percent': None,
        'adx': None,
        'data_available': None,
        'failure_reason': None,
        'nikkei_fallback_used': False,
    }


def test_end_of_day_report_marks_market_conditions_unavailable(tmp_path):
    use_case = TradingUseCase(
        token='dummy',
        order_history_path=tmp_path / 'order_history.json',
        notifier=lambda message: None,
        daily_analyzer=SimpleNamespace(analyze=lambda daily_summary: None),
        daily_report_directory=tmp_path / 'reports',
    )
    use_case.market_regime = MarketRegime.DANGER
    use_case.market_regime_assessment = SimpleNamespace(
        realized_volatility_percent=None,
        vix=None,
        nikkei_change_percent=None,
        adx=None,
        data_available=False,
        failure_reason='日経225またはVIXの日足データが空です',
    )

    use_case._send_end_of_day_report()

    report = json.loads((tmp_path / 'reports' / f'{datetime.now().date().isoformat()}.json').read_text(encoding='utf-8'))
    assert report['market_conditions']['assessment_status'] == 'unavailable'
    assert report['market_conditions']['regime'] == 'DANGER'
    assert report['market_conditions']['data_available'] is False
    assert report['market_conditions']['failure_reason'] == '日経225またはVIXの日足データが空です'


def test_end_of_day_report_counts_only_errors_during_trading_session(monkeypatch, tmp_path):
    monkeypatch.setattr(config, 'LOG_DIRECTORY', tmp_path)
    today = '2026-09-25'
    (tmp_path / 'trade_project.log').write_text(
        f"{today} 08:59:59 ERROR test: before market\n"
        f"{today} 09:00:00 ERROR test: opening error\n"
        f"{today} 15:29:59 ERROR test: closing error\n"
        f"{today} 15:30:00 ERROR test: after market\n",
        encoding='utf-8',
    )
    use_case = TradingUseCase(token='dummy', order_history_path=tmp_path / 'order_history.json')

    summary = use_case._daily_log_error_summary(today)

    assert summary['count'] == 2
    assert summary['summaries'] == ['opening error', 'closing error']


def test_end_of_day_report_correlates_timeout_outcomes_and_separates_other_errors(
    monkeypatch, tmp_path
):
    monkeypatch.setattr(config, 'LOG_DIRECTORY', tmp_path)
    today = '2026-09-25'
    (tmp_path / 'trade_project.log').write_text(
        f'{today} 09:10:00,000 ERROR src.api: GET /board/AAA@1 Read timed out request_id=aaa111\n'
        f'{today} 09:10:01,000 INFO src.filter: 板取得リトライ結果: request_id=aaa111 outcome=recovered symbol=AAA\n'
        f'{today} 09:11:00,000 ERROR src.api: GET /board/BBB@1 Read timed out request_id=bbb222\n'
        f'{today} 09:11:01,000 INFO src.filter: 板取得リトライ結果: request_id=bbb222 outcome=final_failure symbol=BBB\n'
        f'{today} 09:12:00,000 ERROR src.api: GET /board/CCC@1 Read timed out\n'
        f'{today} 09:13:00,000 ERROR src.report: 日次レポートの公開に失敗しました\n',
        encoding='utf-8',
    )
    use_case = TradingUseCase(token='dummy', order_history_path=tmp_path / 'order_history.json')

    summary = use_case._daily_log_error_summary(today)

    assert summary['count'] == 4
    assert summary['board_timeout'] == {
        'count': 3,
        'recovered': 1,
        'final_failure': 1,
        'unrecorded': 1,
        'symbols': {
            'recovered': ['AAA'],
            'final_failure': ['BBB'],
            'unrecorded': ['CCC'],
        },
    }
    assert summary['other_errors'] == {
        'count': 1,
        'groups': [{'title': '日次レポート公開失敗', 'count': 1}],
    }


def test_end_of_day_notification_shows_timeout_outcomes_without_recovered_symbols(tmp_path):
    messages = []
    use_case = TradingUseCase(
        token='dummy',
        order_history_path=tmp_path / 'order_history.json',
        notifier=messages.append,
        daily_report_directory=tmp_path / 'reports',
    )
    use_case._daily_log_error_summary = lambda _today: {
        'has_errors': True,
        'count': 4,
        'board_timeout': {
            'count': 3, 'recovered': 1, 'final_failure': 1, 'unrecorded': 1,
            'symbols': {
                'recovered': ['AAA'],
                'final_failure': ['BBB'],
                'unrecorded': ['CCC'],
            },
        },
        'other_errors': {
            'count': 1, 'groups': [{'title': '日次レポート公開失敗', 'count': 1}],
        },
    }

    use_case._send_end_of_day_report()

    assert '■ エラー 発生3件(回復1・最終失敗1・記録なし1)' in messages[0]
    assert '  最終失敗対象: BBB' in messages[0]
    assert '  記録なし対象: CCC' in messages[0]
    assert 'AAA' not in messages[0]
    assert '・その他エラー 1件' in messages[0]


def test_end_of_day_report_includes_atr_danger_skip_outcome(tmp_path):
    messages = []
    use_case = TradingUseCase(
        token='dummy',
        order_history_path=tmp_path / 'order_history.json',
        notifier=messages.append,
        daily_report_directory=tmp_path / 'reports',
    )
    assessment = SimpleNamespace(atr=5.0, latest_true_range=12.0, ratio=2.4)
    use_case._record_atr_danger_skip('3624', datetime.now().replace(hour=14, minute=55, second=0, microsecond=0), 95.0, assessment, 300)
    use_case._update_atr_danger_skip_observation('3624', datetime.now().replace(hour=15, minute=19, second=0, microsecond=0), 97.0)

    use_case._send_end_of_day_report()

    report = json.loads((tmp_path / 'reports' / f'{datetime.now().date().isoformat()}.json').read_text(encoding='utf-8'))
    skip = report['atr_danger_skips'][0]
    assert skip['hypothetical_pnl_before_cost'] == 600.0
    assert skip['outcome'] == '利益取り逃しの可能性'
    assert '3624: 利益取り逃しの可能性 (+600円概算)' in report['report_text']


def test_end_of_day_report_includes_atr_stop_exit_outcome(tmp_path):
    messages = []
    use_case = TradingUseCase(
        token='dummy',
        order_history_path=tmp_path / 'order_history.json',
        notifier=messages.append,
        daily_report_directory=tmp_path / 'reports',
    )
    assessment = SimpleNamespace(atr=5.0, ratio=2.4, level=VolatilityLevel.DANGER)
    use_case._record_atr_stop_exit(
        '3624', datetime.now().replace(hour=14, minute=55, second=0, microsecond=0), 100.0, 96.5, 300, assessment, 0.7
    )
    use_case._update_atr_stop_exit_observation('3624', datetime.now().replace(hour=15, minute=19, second=0, microsecond=0), 94.5)

    use_case._send_end_of_day_report()

    report = json.loads((tmp_path / 'reports' / f'{datetime.now().date().isoformat()}.json').read_text(encoding='utf-8'))
    exit_summary = report['atr_stop_exits'][0]
    assert exit_summary['avoided_pnl_before_cost'] == 600.0
    assert exit_summary['outcome'] == '下落回避の可能性'
    assert '3624: 下落回避の可能性 (+600円概算)' in report['report_text']


def test_end_of_day_report_does_not_run_or_notify_daily_llm_analysis(tmp_path):
    messages = []
    summaries = []

    class DailyAnalyzer:
        def analyze(self, summary):
            summaries.append(summary)
            return '今日の評価\n- 参考評価です。'

    use_case = TradingUseCase(
        token='dummy',
        order_history_path=tmp_path / 'order_history.json',
        notifier=messages.append,
        daily_analyzer=DailyAnalyzer(),
        daily_report_directory=tmp_path / 'reports',
    )
    use_case._send_end_of_day_report()

    assert summaries == []
    assert messages[0].startswith('【業務】取引\n【機能】トレード終了')
    assert 'LLM日次評価' not in messages[0]
    assert '参考評価です。' not in messages[0]
    report = json.loads((tmp_path / 'reports' / f'{datetime.now().date().isoformat()}.json').read_text(encoding='utf-8'))
    assert report['llm_analysis'] is None


@pytest.mark.parametrize(
    ("regime", "expected_order_count"),
    [
        (MarketRegime.NORMAL, 1),
        (MarketRegime.CAUTION, 0),
    ],
)
def test_rsi_input_is_recorded_when_buy_passes_or_caution_excludes(
    monkeypatch,
    tmp_path,
    regime,
    expected_order_count,
):
    monkeypatch.setattr(config, 'IS_DEMO', True)
    symbols_path = tmp_path / 'top_symbols.json'
    symbols_path.write_text(json.dumps(['7203']), encoding='utf-8')
    history_path = tmp_path / 'order_history.json'
    calls = []

    class MarketDataClient:
        def get_yahoo_daily_closes(self, symbol):
            return [90.0] * 5

    class BoardClient:
        def get_current_board(self, token, symbol):
            return {'current_price': 92.0}

    class WalletClient:
        def get_wallet_cash(self, token):
            return {'StockAccountWallet': 100_000.0}

    class PositionsClient:
        def get_positions(self, token):
            return []

    class NoOpAnalyzer:
        def analyze(self, summary):
            return None

    class OrderSender:
        def place_market_order(self, token, symbol, side):
            calls.append((token, symbol, side))
            return {'Result': 0, 'OrderId': 'paper-order-1'}

    class MarketRegimeProvider:
        def __init__(self):
            self.calls = 0

        def execute(self):
            self.calls += 1
            return SimpleNamespace(
                regime=regime,
                data_available=True,
                failure_reason=None,
            )

    market_regime_provider = MarketRegimeProvider()
    filter_decision_repository = FilterDecisionRepository(tmp_path / "filter_decisions.sqlite3")

    current_times = iter([datetime(2026, 9, 4, 10, 0), datetime(2026, 9, 4, 15, 30)])
    monkeypatch.setattr(config, 'API_SOFT_LIMIT', 100_000.0)
    monkeypatch.setattr(
        'src.application.trading_usecase.get_api_soft_limit',
        lambda token: (_ for _ in ()).throw(AssertionError('live soft-limit API was called')),
    )

    use_case = TradingUseCase(
        token='dummy',
        order_history_path=history_path,
        market_data_client=MarketDataClient(),
        board_client=BoardClient(),
        wallet_client=WalletClient(),
        positions_client=PositionsClient(),
        order_sender=OrderSender(),
        notifier=lambda message: None,
        market_regime_usecase=market_regime_provider,
        filter_decision_repository=filter_decision_repository,
        daily_report_directory=tmp_path / 'reports',
    )
    use_case.run(
        top_symbols_path=symbols_path,
        now_provider=lambda: next(current_times),
        sleep=lambda seconds: None,
    )

    assert len(calls) == expected_order_count
    assert len(use_case.order_history) == expected_order_count
    if expected_order_count:
        assert calls == [('dummy', '7203', config.OrderSide.BUY.value)]
        assert use_case.order_history[0].result_code == 0
        assert use_case.order_history[0].order_id == 'paper-order-1'
        rsi_input = use_case.order_history[0].rsi_input
        assert rsi_input == {
            'decision_at': '2026-09-04T10:00:00',
            'symbol': '7203',
            'market_regime': 'NORMAL',
            'rsi': 50.0,
            'applied_threshold': config.RSI_ENTRY_THRESHOLD,
            'threshold_kind': 'entry',
            'calculation_status': 'available',
            'unavailable_reason': None,
            'close_count': 5,
            'required_close_count': 5,
            'last_close': 90.0,
            'current_price': 92.0,
            'outcome': 'passed',
        }
    event_types = [
        item["event_type"]
        for item in filter_decision_repository.load_summaries(execution_mode="paper")
    ]
    if regime == MarketRegime.CAUTION:
        assert event_types == ["MARKET_REGIME_CAUTION_RSI_FILTER"]
        event = filter_decision_repository.load_summaries(execution_mode="paper")[0]
        rsi_input = event["inputs"]["rsi_input"]
        assert rsi_input == {
            'decision_at': '2026-09-04T10:00:00',
            'symbol': '7203',
            'market_regime': 'CAUTION',
            'rsi': 50.0,
            'applied_threshold': config.RSI_ENTRY_THRESHOLD_CAUTION,
            'threshold_kind': 'entry',
            'calculation_status': 'available',
            'unavailable_reason': None,
            'close_count': 5,
            'required_close_count': 5,
            'last_close': 90.0,
            'current_price': 92.0,
            'outcome': 'excluded_by_caution_threshold',
        }
    else:
        assert event_types == []
    assert market_regime_provider.calls == 1


def test_trading_use_case_records_adx_relief_after_successful_buy(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "IS_DEMO", True)
    symbols_path = tmp_path / "top_symbols.json"
    symbols_path.write_text(json.dumps(["7203"]), encoding="utf-8")
    filter_decision_repository = FilterDecisionRepository(tmp_path / "filter_decisions.sqlite3")

    class MarketDataClient:
        def get_yahoo_daily_closes(self, symbol):
            return [90.0] * 5

    class BoardClient:
        def get_current_board(self, token, symbol):
            return {"current_price": 92.0}

    class WalletClient:
        def get_wallet_cash(self, token):
            return {"StockAccountWallet": 100_000.0}

    class PositionsClient:
        def get_positions(self, token):
            return []

    class OrderSender:
        def place_market_order(self, token, symbol, side):
            return {"Result": 0, "OrderId": "paper-order-1"}

    class MarketRegimeProvider:
        def execute(self):
            return SimpleNamespace(
                regime=MarketRegime.NORMAL, data_available=True, failure_reason=None,
                trend_relief_applied=True, adx=30.0,
            )

    current_times = iter([datetime(2026, 9, 4, 10, 0), datetime(2026, 9, 4, 15, 30)])
    monkeypatch.setattr(config, "API_SOFT_LIMIT", 100_000.0)
    use_case = TradingUseCase(
        token="dummy", order_history_path=tmp_path / "order_history.json",
        market_data_client=MarketDataClient(), board_client=BoardClient(), wallet_client=WalletClient(),
        positions_client=PositionsClient(), order_sender=OrderSender(), notifier=lambda message: None,
        market_regime_usecase=MarketRegimeProvider(), filter_decision_repository=filter_decision_repository,
        daily_report_directory=tmp_path / "reports",
    )

    use_case.run(top_symbols_path=symbols_path, now_provider=lambda: next(current_times), sleep=lambda seconds: None)

    events = filter_decision_repository.load_summaries(execution_mode="paper")
    assert [(item["event_type"], item["symbol"], item["adx"]) for item in events] == [
        ("ADX_TREND_RELIEF", "7203", 30.0)
    ]


def test_trading_use_case_continues_when_filter_decision_storage_fails(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "IS_DEMO", True)
    symbols_path = tmp_path / "top_symbols.json"
    symbols_path.write_text(json.dumps(["7203"]), encoding="utf-8")
    orders = []

    class FailingRepository:
        def load_open_events(self, execution_mode):
            raise OSError("storage unavailable")

        def finalize_due_events(self, as_of, observation_days, execution_mode=None):
            raise OSError("storage unavailable")

        def update_open_event_observations(self, observed_at, prices_by_symbol, execution_mode):
            raise OSError("storage unavailable")

        def record_event(self, *args, **kwargs):
            raise OSError("storage unavailable")

        def load_summaries(self, **kwargs):
            raise OSError("storage unavailable")

    class MarketDataClient:
        def get_yahoo_daily_closes(self, symbol):
            return [90.0] * 5

    class BoardClient:
        def get_current_board(self, token, symbol):
            return {"current_price": 92.0}

    class WalletClient:
        def get_wallet_cash(self, token):
            return {"StockAccountWallet": 100_000.0}

    class PositionsClient:
        def get_positions(self, token):
            return []

    class OrderSender:
        def place_market_order(self, token, symbol, side):
            orders.append((symbol, side))
            return {"Result": 0, "OrderId": "paper-order-1"}

    current_times = iter([datetime(2026, 9, 16, 10, 0), datetime(2026, 9, 16, 15, 30)])
    monkeypatch.setattr(config, "API_SOFT_LIMIT", 100_000.0)
    use_case = TradingUseCase(
        token="dummy", order_history_path=tmp_path / "order_history.json",
        market_data_client=MarketDataClient(), board_client=BoardClient(), wallet_client=WalletClient(),
        positions_client=PositionsClient(), order_sender=OrderSender(), notifier=lambda message: None,
        filter_decision_repository=FailingRepository(),
        daily_report_directory=tmp_path / "reports",
    )

    use_case.run(top_symbols_path=symbols_path, now_provider=lambda: next(current_times), sleep=lambda seconds: None)

    assert orders == [("7203", config.OrderSide.BUY.value)]


def test_trading_use_case_collects_complete_preflight_market_data(tmp_path):
    class MarketDataClient:
        def get_yahoo_daily_closes(self, symbol):
            return [100.0] * config.RSI_MINIMUM_CLOSES

    class BoardClient:
        def get_current_board(self, token, symbol):
            return {'current_price': 101.0}

    use_case = TradingUseCase(
        token='dummy',
        order_history_path=tmp_path / 'order_history.json',
        market_data_client=MarketDataClient(),
        board_client=BoardClient(),
    )

    assert use_case.collect_preflight_market_data(['7203', '8306']) == {
        '7203': {'closes': [100.0] * config.RSI_MINIMUM_CLOSES, 'board': {'current_price': 101.0}},
        '8306': {'closes': [100.0] * config.RSI_MINIMUM_CLOSES, 'board': {'current_price': 101.0}},
    }


def test_trading_use_case_rejects_incomplete_preflight_market_data(tmp_path):
    class MarketDataClient:
        def get_yahoo_daily_closes(self, symbol):
            return [100.0] * (config.RSI_MINIMUM_CLOSES - 1)

    class BoardClient:
        def get_current_board(self, token, symbol):
            raise AssertionError('board should not be called')

    use_case = TradingUseCase(
        token='dummy',
        order_history_path=tmp_path / 'order_history.json',
        market_data_client=MarketDataClient(),
        board_client=BoardClient(),
    )

    assert use_case.collect_preflight_market_data(['7203']) is None


def test_trading_use_case_does_not_record_rejected_order(monkeypatch, tmp_path):
    symbols_path = tmp_path / 'top_symbols.json'
    symbols_path.write_text(json.dumps(['7203']), encoding='utf-8')
    history_path = tmp_path / 'order_history.json'
    order_calls = []
    current_times = iter([datetime(2026, 9, 4, 10, 0), datetime(2026, 9, 4, 15, 30)])

    monkeypatch.setattr('src.application.trading_usecase.get_yahoo_daily_closes', lambda symbol: [90.0] * 5)
    monkeypatch.setattr(
        'src.application.trading_usecase.get_current_board',
        lambda token, symbol: {'current_price': 92.0},
    )
    monkeypatch.setattr(
        'src.application.trading_usecase.get_wallet_cash',
        lambda token: {'StockAccountWallet': 100_000.0},
    )
    monkeypatch.setattr('src.application.trading_usecase.get_positions', lambda token: [])
    monkeypatch.setattr('src.application.trading_usecase.get_api_soft_limit', lambda token: 100_000.0)
    monkeypatch.setattr(
        'src.application.trading_usecase.place_market_order',
        lambda token, symbol, side: order_calls.append((symbol, side)) or {'Result': 1, 'OrderId': 'rejected'},
    )

    use_case = TradingUseCase(
        token='dummy',
        order_history_path=history_path,
        notifier=lambda message: None,
        daily_report_directory=tmp_path / 'reports',
    )
    use_case.run(
        top_symbols_path=symbols_path,
        now_provider=lambda: next(current_times),
        sleep=lambda seconds: None,
    )

    assert order_calls == [('7203', config.OrderSide.BUY.value)]
    assert use_case.order_history == []


def test_trading_use_case_liquidates_all_holdings_before_market_close(monkeypatch, tmp_path):
    symbols_path = tmp_path / 'top_symbols.json'
    symbols_path.write_text(json.dumps(['7203']), encoding='utf-8')
    executor = PaperOrderClient(prices={'7203': 100.0}, cash=20_000.0, order_qty=100)
    executor.place_market_order('unused', '7203', config.OrderSide.BUY.value)

    class BoardClient:
        def get_current_board(self, token, symbol):
            return {'current_price': 101.0}

        def get_current_board_with_freshness(self, token, symbol):
            return {
                'current_price': 101.0,
                'current_price_time': '2026-09-04T15:14:00+09:00',
                'current_price_status': 1,
            }

    monkeypatch.setattr(config, 'MARKET_LIQUIDATION_HOUR', 15)
    monkeypatch.setattr(config, 'MARKET_LIQUIDATION_MINUTE', 20)
    use_case = TradingUseCase(
        token='unused',
        order_history_path=tmp_path / 'order_history.json',
        board_client=BoardClient(),
        order_sender=executor,
        notifier=lambda message: None,
        daily_report_directory=tmp_path / 'reports',
    )

    use_case.run(
        top_symbols_path=symbols_path,
        now_provider=lambda: datetime(2026, 9, 4, 15, 20),
        sleep=lambda seconds: None,
    )

    assert executor.holdings == {}
    assert executor.orders[-1]['Side'] == config.OrderSide.SELL.value
    assert executor.orders[-1]['Qty'] == 100
    assert use_case.order_history[-1].side == config.OrderSide.SELL
    assert use_case.order_history[-1].qty == 100


def test_trading_use_case_records_kill_switch_in_daily_report(monkeypatch, tmp_path):
    symbols_path = tmp_path / 'top_symbols.json'
    symbols_path.write_text(json.dumps(['7203']), encoding='utf-8')
    messages = []
    current_times = iter([datetime(2026, 9, 4, 10, 0), datetime(2026, 9, 4, 10, 0)])

    class MarketDataClient:
        def get_yahoo_daily_closes(self, symbol):
            return [90.0] * 5

    class BoardClient:
        def get_current_board(self, token, symbol):
            return {'current_price': 92.0}

    class WalletClient:
        def get_wallet_cash(self, token):
            return {'StockAccountWallet': 100_000.0}

    class PositionsClient:
        def get_positions(self, token):
            return []

    class NoOpAnalyzer:
        def analyze(self, summary):
            return None

    monkeypatch.setattr(config, 'IS_DEMO', True)
    monkeypatch.setattr(config, 'API_SOFT_LIMIT', 100_000.0)
    monkeypatch.setattr('src.application.trading_usecase.check_kill_switch', lambda *args: False)
    use_case = TradingUseCase(
        token='dummy',
        order_history_path=tmp_path / 'order_history.json',
        market_data_client=MarketDataClient(),
        board_client=BoardClient(),
        wallet_client=WalletClient(),
        positions_client=PositionsClient(),
        notifier=messages.append,
        daily_analyzer=NoOpAnalyzer(),
        daily_report_directory=tmp_path / 'reports',
    )

    use_case.run(
        top_symbols_path=symbols_path,
        now_provider=lambda: next(current_times),
        sleep=lambda seconds: None,
    )

    report = json.loads((tmp_path / 'reports' / '2026-09-04.json').read_text(encoding='utf-8'))
    assert use_case.kill_switch_triggered is True
    assert report['date'] == '2026-09-04'
    assert report['kill_switch_triggered'] is True
    assert '【緊急停止】キルスイッチを発動しました' in messages[0]
    assert all('キルスイッチ: 発動' not in message for message in messages)


def test_kill_switch_notifies_only_once(monkeypatch, tmp_path):
    messages = []
    use_case = TradingUseCase(
        token='dummy',
        order_history_path=tmp_path / 'order_history.json',
        notifier=messages.append,
    )

    use_case._trigger_kill_switch('損失上限超過')
    use_case._trigger_kill_switch('重複通知')

    assert messages == ['【緊急停止】キルスイッチを発動しました: 損失上限超過']


def test_emergency_stop_liquidates_positions_and_stops_loop(monkeypatch, tmp_path):
    symbols_path = tmp_path / 'top_symbols.json'
    symbols_path.write_text(json.dumps(['7203']), encoding='utf-8')
    stop_file = tmp_path / 'emergency_stop'
    stop_file.write_text('requested', encoding='utf-8')
    messages = []
    orders = []

    class PositionsClient:
        def get_positions(self, token):
            return [{'Symbol': '7203', 'Side': config.OrderSide.SELL.value, 'HoldQty': 100}]

    class WalletClient:
        def get_wallet_cash(self, token):
            return {'StockAccountWallet': 100_000.0}

    class BoardClient:
        def get_current_board(self, token, symbol):
            return {'current_price': 92.0}

    class OrderSender:
        def set_price(self, symbol, price):
            pass

        def place_market_order(self, token, symbol, side, quantity):
            orders.append((symbol, side, quantity))
            return {'Result': 0, 'OrderId': 'emergency-1'}

    monkeypatch.setattr(config, 'EMERGENCY_STOP_FILE', stop_file)
    monkeypatch.setattr(config, 'TRADING_MODE', 'live')
    use_case = TradingUseCase(
        token='dummy',
        order_history_path=tmp_path / 'order_history.json',
        positions_client=PositionsClient(),
        wallet_client=WalletClient(),
        board_client=BoardClient(),
        order_sender=OrderSender(),
        notifier=messages.append,
        daily_report_directory=tmp_path / 'reports',
    )

    use_case.run(top_symbols_path=symbols_path, sleep=lambda seconds: None)

    assert use_case.emergency_stop_triggered is True
    assert orders == [('7203', config.OrderSide.SELL.value, 100)]
    assert any('手動緊急停止フラグ' in message for message in messages)


def test_trading_use_case_logs_repeated_sell_signal_without_holdings_once(monkeypatch, tmp_path, caplog):
    symbols_path = tmp_path / 'top_symbols.json'
    symbols_path.write_text(json.dumps(['7203']), encoding='utf-8')
    current_times = iter([
        datetime(2026, 9, 4, 10, 0),
        datetime(2026, 9, 4, 10, 0),
        datetime(2026, 9, 4, 10, 1),
        datetime(2026, 9, 4, 10, 1),
        datetime(2026, 9, 4, 15, 30),
    ])

    class MarketDataClient:
        def get_yahoo_daily_closes(self, symbol):
            return [90.0] * 5

    class BoardClient:
        def get_current_board(self, token, symbol):
            return {'current_price': 89.0}

    class WalletClient:
        def get_wallet_cash(self, token):
            return {'StockAccountWallet': 100_000.0}

    class PositionsClient:
        def get_positions(self, token):
            return []

    monkeypatch.setattr(config, 'IS_DEMO', True)
    monkeypatch.setattr(config, 'API_SOFT_LIMIT', 100_000.0)
    monkeypatch.setattr(config, 'MAX_ORDER_AMOUNT_PER_TRADE', 100_000.0)
    use_case = TradingUseCase(
        token='dummy',
        order_history_path=tmp_path / 'order_history.json',
        market_data_client=MarketDataClient(),
        board_client=BoardClient(),
        wallet_client=WalletClient(),
        positions_client=PositionsClient(),
        notifier=lambda message: None,
        daily_report_directory=tmp_path / 'reports',
    )

    with caplog.at_level('INFO', logger='src.application.trading_usecase'):
        use_case.run(
            top_symbols_path=symbols_path,
            now_provider=lambda: next(current_times),
            sleep=lambda seconds: None,
        )

    observations = [
        record for record in caplog.records
        if record.message.startswith('売り条件該当・未保有:')
    ]
    assert len(observations) == 1
    assert observations[0].levelname == 'INFO'
    assert '銘柄=7203' in observations[0].message
    assert '状態=開始' in observations[0].message


def test_missing_holding_sell_signal_logs_only_state_transitions(tmp_path, caplog):
    class DecisionJournal:
        def __init__(self):
            self.records = []

        def record(self, stage, symbol, reason_code, occurred_at, inputs):
            self.records.append((stage, symbol, reason_code, occurred_at, inputs))

    journal = DecisionJournal()
    use_case = TradingUseCase(
        token='dummy',
        order_history_path=tmp_path / 'unused_history.json',
        decision_journal_repository=journal,
    )
    now = datetime(2026, 10, 9, 10, 0)

    with caplog.at_level('INFO', logger='src.application.trading_usecase'):
        use_case._observe_missing_holding_sell_signal('7203', now, missing_holdings=True)
        use_case._observe_missing_holding_sell_signal('7203', now, missing_holdings=True)
        use_case._observe_missing_holding_sell_signal(
            '7203', now, missing_holdings=False, cleared_by='売り条件が解消'
        )
        use_case._observe_missing_holding_sell_signal('7203', now, missing_holdings=True)

    observations = [
        record for record in caplog.records
        if record.message.startswith('売り条件該当・未保有:')
    ]
    assert [record.levelname for record in observations] == ['INFO', 'INFO', 'INFO']
    assert all('銘柄=7203' in record.message for record in observations)
    assert '状態=開始' in observations[0].message
    assert '状態=解消' in observations[1].message
    assert '状態=開始' in observations[2].message
    assert [
        (stage, symbol, reason_code, inputs['state'])
        for stage, symbol, reason_code, _, inputs in journal.records
    ] == [
        ('trading', '7203', 'SELL_SIGNAL_WITHOUT_HOLDINGS', 'started'),
        ('trading', '7203', 'SELL_SIGNAL_WITHOUT_HOLDINGS', 'started'),
    ]


def test_missing_holding_after_same_day_buy_is_warning_on_each_transition(tmp_path, caplog):
    use_case = TradingUseCase(token='dummy', order_history_path=tmp_path / 'unused_history.json')
    now = datetime(2026, 10, 9, 10, 0)
    use_case.order_history = [
        OrderHistoryEntry(
            symbol='7203',
            side=config.OrderSide.BUY,
            price=100.0,
            qty=100,
            timestamp='2026-10-09T09:45:00',
            result_code=0,
        )
    ]

    with caplog.at_level('INFO', logger='src.application.trading_usecase'):
        use_case._observe_missing_holding_sell_signal('7203', now, missing_holdings=True)
        use_case._observe_missing_holding_sell_signal(
            '7203', now, missing_holdings=False, cleared_by='保有を確認'
        )

    observations = [
        record for record in caplog.records
        if record.message.startswith('売り条件該当・未保有:')
    ]
    assert [record.levelname for record in observations] == ['WARNING', 'WARNING']
    assert all('銘柄=7203' in record.message for record in observations)
    assert '当日純買付数量=100株' in observations[0].message


def test_missing_holding_after_same_day_buy_and_full_sell_is_info(tmp_path, caplog):
    use_case = TradingUseCase(token='dummy', order_history_path=tmp_path / 'unused_history.json')
    now = datetime(2026, 10, 9, 10, 0)
    use_case.order_history = [
        OrderHistoryEntry(
            symbol='7203',
            side=config.OrderSide.BUY,
            price=100.0,
            qty=100,
            timestamp='2026-10-09T09:35:00',
            result_code=0,
        ),
        OrderHistoryEntry(
            symbol='7203',
            side=config.OrderSide.SELL,
            price=105.0,
            qty=100,
            timestamp='2026-10-09T09:50:00',
            result_code=0,
        ),
    ]

    with caplog.at_level('INFO', logger='src.application.trading_usecase'):
        use_case._observe_missing_holding_sell_signal('7203', now, missing_holdings=True)

    observations = [
        record for record in caplog.records
        if record.message.startswith('売り条件該当・未保有:')
    ]
    assert len(observations) == 1
    assert observations[0].levelname == 'INFO'
    assert '銘柄=7203' in observations[0].message
    assert '当日純買付数量=' not in observations[0].message


def test_missing_holding_after_partial_same_day_sell_remains_warning(tmp_path, caplog):
    use_case = TradingUseCase(token='dummy', order_history_path=tmp_path / 'unused_history.json')
    now = datetime(2026, 10, 9, 10, 0)
    use_case.order_history = [
        OrderHistoryEntry(
            symbol='7203',
            side=config.OrderSide.BUY,
            price=100.0,
            qty=200,
            timestamp='2026-10-09T09:35:00',
            result_code=0,
        ),
        OrderHistoryEntry(
            symbol='7203',
            side=config.OrderSide.SELL,
            price=105.0,
            qty=100,
            timestamp='2026-10-09T09:50:00',
            result_code=0,
        ),
    ]

    with caplog.at_level('INFO', logger='src.application.trading_usecase'):
        use_case._observe_missing_holding_sell_signal('7203', now, missing_holdings=True)

    observations = [
        record for record in caplog.records
        if record.message.startswith('売り条件該当・未保有:')
    ]
    assert len(observations) == 1
    assert observations[0].levelname == 'WARNING'
    assert '銘柄=7203' in observations[0].message
    assert '当日純買付数量=100株' in observations[0].message


def test_buy_safety_checks_still_reject_insufficient_budget(caplog):
    signal = TradeSignal('7203', config.OrderSide.BUY, 1_000.0, 100)

    with caplog.at_level('WARNING', logger='src.domain.rules'):
        assert not is_safe_to_order(
            signal,
            wallet_amount=50_000.0,
            has_holdings=False,
            order_history=[],
            lock_seconds=60,
        )

    assert any('予算不足:' in record.message for record in caplog.records)
