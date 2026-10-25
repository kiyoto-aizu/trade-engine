from datetime import date, datetime, timezone
from types import SimpleNamespace

import pytest
import requests

from src.api import request_handler
from src.infrastructure.kabu.board_repository import BoardRepository
from src.infrastructure.kabu.get_apisoftlimit import get_api_soft_limit
from src.infrastructure.kabu.get_positions import get_positions
from src.infrastructure.kabu.get_token import get_api_token
from src.infrastructure.kabu.get_wallet import get_wallet_cash
from src.infrastructure.kabu.primaryexchange_repository import PrimaryExchangeRepository
from src.infrastructure.kabu.register import register_symbols
from src.infrastructure.kabu.regulation_repository import RegulationRepository
from src.infrastructure.kabu.send_order import place_market_order
from src.infrastructure.kabu.unregister import unregister_all
from src.infrastructure.market_data.get_daily_closes import get_yahoo_daily_closes
from src.infrastructure.market_data.yahoo_finance_client import YahooFinanceClient
from src.infrastructure.market_data.yahoo_index_client import YahooIndexClient
from src.infrastructure.notification import slack_notify
from src.infrastructure.persistence.storage import read_json, write_json
from src.infrastructure.persistence.filtering_result_repository import FilteringResultRepository
from src.domain.models import FilteringResult
from src.application.filtering_usecase import FilteringUseCase
from src.domain.models import OrderHistoryEntry, PriceLimit, ScreeningResult, TradeSignal
from src.domain.enums import OrderSide
from src.domain.rules import calculate_price_limit, calculate_rsi, is_duplicate_order, is_recent_order
from src.entrypoints.run_monthly_analysis import _load_backtest_summaries, _load_daily_summaries, _month_bounds
from src.infrastructure import execution_lock


class Response:
    def __init__(self, data):
        self.data = data

    def raise_for_status(self):
        return None

    def json(self):
        return self.data


@pytest.mark.parametrize('method_name, request_name', [
    ('send_get', 'get'),
    ('send_post', 'post'),
    ('send_put', 'put'),
])
def test_request_handler_returns_json_for_success(monkeypatch, method_name, request_name):
    monkeypatch.setattr(request_handler, '_wait_for_request_slot', lambda: None)
    monkeypatch.setattr(request_handler.requests, request_name, lambda *args, **kwargs: Response({'ok': True}))

    kwargs = {'data': {'key': 'value'}} if method_name != 'send_get' else {}
    assert getattr(request_handler, method_name)('https://example.test', **kwargs) == {'ok': True}


@pytest.mark.parametrize('method_name, request_name', [
    ('send_get', 'get'),
    ('send_post', 'post'),
    ('send_put', 'put'),
])
def test_request_handler_returns_none_for_request_error(monkeypatch, method_name, request_name):
    monkeypatch.setattr(request_handler, '_wait_for_request_slot', lambda: None)
    monkeypatch.setattr(
        request_handler.requests,
        request_name,
        lambda *args, **kwargs: (_ for _ in ()).throw(requests.Timeout('timeout')),
    )

    assert getattr(request_handler, method_name)('https://example.test') is None


def test_kabu_api_wrappers_build_expected_requests(monkeypatch):
    calls = []

    def send_get(url, **kwargs):
        calls.append((url, kwargs))
        if url.endswith('/token'):
            return {'Token': 'token'}
        if url.endswith('/apisoftlimit'):
            return {'Stock': '12.5'}
        return {'StockAccountWallet': 1000}

    monkeypatch.setattr('src.infrastructure.kabu.get_token.request_handler.send_post', lambda url, **kwargs: {'Token': 'token'})
    monkeypatch.setattr('src.infrastructure.kabu.get_positions.request_handler.send_get', send_get)
    monkeypatch.setattr('src.infrastructure.kabu.get_wallet.request_handler.send_get', send_get)
    monkeypatch.setattr('src.infrastructure.kabu.get_apisoftlimit.request_handler.send_get', send_get)

    assert get_api_token() == 'token'
    assert get_positions('token', symbol='7203', side='1') == {'StockAccountWallet': 1000}
    assert get_wallet_cash('token') == {'StockAccountWallet': 1000}
    assert get_api_soft_limit('token') == 125_000.0
    assert calls[0][1]['params']['symbol'] == '7203'
    assert calls[0][1]['params']['side'] == '1'


def test_kabu_repositories_parse_success_and_failure_responses(monkeypatch):
    response = {
        'CurrentPrice': '123.4',
        'CurrentPriceTime': '2026-10-02T15:00:00+09:00',
        'CurrentPriceStatus': 8,
        'TradingVolume': 10,
        'TradingValue': 20,
    }
    monkeypatch.setattr(
        'src.infrastructure.kabu.board_repository.request_handler.send_get',
        lambda *args, **kwargs: response,
    )
    board = BoardRepository('token')
    assert board.get_current_price('7203') == 123.4
    assert board.get_current_board('7203')['symbol_name'] == '銘柄:7203'
    assert board.get_current_board('7203').keys() == {
        'symbol_name', 'current_price', 'trading_volume', 'trading_value', 'response_keys'
    }
    quote = board.get_current_board_with_freshness('7203')
    assert quote['current_price_time'] == response['CurrentPriceTime']
    assert quote['current_price_status'] == 8

    monkeypatch.setattr(
        'src.infrastructure.kabu.primaryexchange_repository.request_handler.send_get',
        lambda *args, **kwargs: {'PrimaryExchange': '1'},
    )
    assert PrimaryExchangeRepository('token').get_primary_exchange('7203') == 1

    monkeypatch.setattr(
        'src.infrastructure.kabu.regulation_repository.request_handler.send_get',
        lambda *args, **kwargs: {'RegulationsInfo': [{'Reason': '注意'}]},
    )
    regulation = RegulationRepository('token').get_regulation('7203', 1)
    assert (regulation.is_restricted, regulation.reason) == (True, '注意')

    monkeypatch.setattr('src.infrastructure.kabu.regulation_repository.request_handler.send_get', lambda *args, **kwargs: None)
    assert RegulationRepository('token').get_regulation('7203', 1).is_restricted is True


def test_order_registration_and_unregistration_handle_responses(monkeypatch):
    calls = []
    monkeypatch.setattr('src.infrastructure.kabu.send_order.request_handler.send_post', lambda url, **kwargs: calls.append(kwargs['data']) or {'Result': 0, 'OrderId': 'order-1'})
    monkeypatch.setattr('src.infrastructure.kabu.register.request_handler.send_put', lambda url, **kwargs: {'RegistList': [{'Symbol': '7203'}]} if url.endswith('/register') else {'RegistList': []})

    assert place_market_order('token', '7203', '2', 200)['OrderId'] == 'order-1'
    assert calls[0]['Qty'] == 200
    assert calls[0]['DelivType'] == 2
    assert register_symbols('token', ['7203'])['RegistList'][0]['Symbol'] == '7203'
    assert unregister_all('token') == {'RegistList': []}

    monkeypatch.setattr('src.infrastructure.kabu.send_order.request_handler.send_post', lambda *args, **kwargs: {'Result': 1})
    monkeypatch.setattr('src.infrastructure.kabu.register.request_handler.send_put', lambda *args, **kwargs: None)
    assert place_market_order('token', '7203', '1') is None
    assert register_symbols('token', ['7203']) is None
    assert unregister_all('token') is None


def test_yahoo_clients_parse_valid_and_invalid_responses(monkeypatch):
    response = {
        'chart': {
            'result': [{
                'timestamp': [1_725_824_000, 1_725_910_400],
                'indicators': {'quote': [{'close': [100, 110], 'volume': [10, 20]}]},
            }],
        },
    }
    first_date = datetime.fromtimestamp(1_725_824_000, tz=timezone.utc).date()
    second_date = datetime.fromtimestamp(1_725_910_400, tz=timezone.utc).date()
    monkeypatch.setattr('src.infrastructure.market_data.get_daily_closes.request_handler.send_get', lambda *args, **kwargs: response)
    assert get_yahoo_daily_closes('7203') == [100.0, 110.0]

    client = YahooFinanceClient()
    monkeypatch.setattr('src.infrastructure.market_data.yahoo_finance_client.request_handler.send_get', lambda *args, **kwargs: response)
    assert client.get_daily_market_data('7203', second_date) == {'close': 110.0, 'volume': 20.0, 'previous_close': 100.0}
    assert client.get_turnover_for_date('7203', second_date) == 2200.0
    assert client.get_average_turnover_before('7203', date.fromordinal(second_date.toordinal() + 1), days=2) == 1600.0
    assert client.get_average_volume('7203', days=2) == 15.0
    assert client.get_average_turnover('7203', days=2) == 1600.0
    details = client.get_average_turnover_details('7203', days=2, target_date=second_date)
    assert details == {
        'average_turnover': 1600.0,
        'average_days': 2,
        'average_includes_target_date': True,
    }

    monkeypatch.setattr('src.infrastructure.market_data.yahoo_finance_client.request_handler.send_get', lambda *args, **kwargs: None)
    assert client.get_daily_market_data('7203', date.today()) is None
    assert client.get_turnover_for_date('7203', date.today()) is None
    assert client.get_average_volume('7203') is None
    assert client.get_average_turnover_details('7203')['average_includes_target_date'] is None


def test_yahoo_index_client_uses_plain_symbol_and_parses_ohlc(monkeypatch):
    response = {
        'chart': {
            'result': [{
                'timestamp': [1_725_824_000, 1_725_910_400],
                'indicators': {'quote': [{
                    'open': [99, 109],
                    'high': [101, 111],
                    'low': [98, 108],
                    'close': [100, 110],
                }]},
            }],
        },
    }
    calls = []

    def send_get(url, **kwargs):
        calls.append((url, kwargs))
        return response

    monkeypatch.setattr('src.infrastructure.market_data.yahoo_index_client.request_handler.send_get', send_get)

    bars = YahooIndexClient().get_daily_ohlc('^N225', range_='max')

    assert len(bars) == 2
    assert bars[0].open == 99.0
    assert bars[1].high == 111.0
    assert '^N225.T' not in calls[0][0]
    assert calls[0][1]['params']['interval'] == '1d'
    assert calls[0][1]['params']['period1'] == 0
    assert calls[0][1]['params']['period2'] > 0

    with pytest.raises(ValueError):
        YahooIndexClient().get_daily_ohlc('7203')


def test_slack_notification_handles_missing_settings_and_request_errors(monkeypatch):
    monkeypatch.setattr(slack_notify.config, '_is_test_runtime', lambda: False)
    monkeypatch.setattr(slack_notify.config, 'SLACK_WEBHOOK_DAILY', '')
    assert slack_notify.notify_daily('message') is False

    monkeypatch.setattr(slack_notify.config, 'SLACK_WEBHOOK_DAILY', 'https://example.test/webhook')
    monkeypatch.setattr(
        slack_notify.requests,
        'post',
        lambda *args, **kwargs: (_ for _ in ()).throw(requests.ConnectionError('down')),
    )
    assert slack_notify.notify_daily('message') is False

    monkeypatch.setattr(slack_notify.requests, 'post', lambda *args, **kwargs: Response({}))
    assert slack_notify.notify_daily('message') is True


def test_process_notification_sends_start_and_end(monkeypatch):
    messages = []
    monkeypatch.setattr(slack_notify, 'notify_daily', messages.append)

    with slack_notify.process_notification('テスト', trigger='手動'):
        pass

    assert messages == ['【テスト】開始', '【テスト】終了']


def test_process_notification_reraises_and_notifies_analyzed_error(monkeypatch):
    messages = []
    monkeypatch.setattr(slack_notify, 'notify_daily', messages.append)
    monkeypatch.setattr(slack_notify, 'notify_critical', messages.append)
    monkeypatch.setattr(
        slack_notify,
        '_analyze_exception_safely',
        lambda process_name, exc: SimpleNamespace(occurrence_count=2, summary='原因\n詳細'),
    )

    with pytest.raises(ValueError, match='boom'):
        with slack_notify.process_notification('テスト'):
            raise ValueError('boom')

    assert messages[0] == '【テスト】開始'
    assert '異常終了' in messages[1]
    assert '原因' in messages[1]


def test_process_notification_ignores_lifecycle_notification_failure(monkeypatch):
    monkeypatch.setattr(slack_notify, 'notify_daily', lambda message: (_ for _ in ()).throw(RuntimeError('down')))
    monkeypatch.setattr(slack_notify, 'notify_critical', lambda message: (_ for _ in ()).throw(RuntimeError('down')))
    slack_notify.notify_process_start('テスト')
    slack_notify.notify_process_end('テスト', success=False, detail='失敗')


def test_storage_handles_success_and_io_errors(tmp_path, monkeypatch):
    path = tmp_path / 'data.json'
    write_json(path, {'value': 1})
    assert read_json(path) == {'value': 1}

    invalid = tmp_path / 'invalid.json'
    invalid.write_text('{', encoding='utf-8')
    assert read_json(invalid) is None
    assert read_json(tmp_path / 'missing.json') is None

    monkeypatch.setattr('builtins.open', lambda *args, **kwargs: (_ for _ in ()).throw(OSError('disk full')))
    write_json(path, {'value': 2})
    assert read_json(path) is None


def test_filtering_usecase_skips_missing_market_inputs_and_notifies_failure(monkeypatch, tmp_path):
    class ScreeningRepository:
        def load_for_date(self, target_date):
            return ScreeningResult(target_date.isoformat(), ['missing', 'no-price', 'no-value', 'no-average', 'zero'], 'now')

    class BoardClient:
        def get_current_board(self, symbol):
            return {
                'missing': None,
                'no-price': {},
                'no-value': {'current_price': 100},
                'no-average': {'current_price': 100, 'trading_volume': 10},
                'zero': {'current_price': 100, 'trading_value': 100},
            }[symbol]

    class VolumeClient:
        def get_average_turnover(self, symbol, days):
            return {'no-average': None, 'zero': 0}[symbol]

    class ResultRepository:
        def save(self, result):
            self.result = result

    notifications = []
    result_repository = ResultRepository()
    use_case = FilteringUseCase(
        ScreeningRepository(),
        BoardClient(),
        VolumeClient(),
        result_repository,
        notifier=lambda message: notifications.append(message) or (_ for _ in ()).throw(RuntimeError('notify failed')),
    )

    result = use_case.execute(target_date=date(2026, 9, 10))

    assert result.symbols == []
    assert result_repository.result is result
    assert '評価対象外数: 5件' in notifications[0]


def test_filtering_usecase_handles_missing_screening_and_historical_data(tmp_path):
    class EmptyScreeningRepository:
        def load_for_date(self, target_date):
            return None

    class ResultRepository:
        def save(self, result):
            self.result = result

    result_repository = ResultRepository()
    use_case = FilteringUseCase(
        EmptyScreeningRepository(),
        object(),
        object(),
        result_repository,
        notifier=lambda message: None,
    )
    result = use_case.execute(target_date=date(2026, 9, 10))
    assert result.symbols == []


def test_filtering_usecase_handles_historical_missing_values(tmp_path):
    class ScreeningRepository:
        def load_for_date(self, target_date):
            return ScreeningResult(target_date.isoformat(), ['7203', '8306'], 'now')

    class HistoricalVolumeClient:
        def get_turnover_for_date(self, symbol, target_date):
            return None if symbol == '7203' else 100

        def get_average_turnover_before(self, symbol, target_date, days):
            return None if symbol == '8306' else 100

    class ResultRepository:
        def save(self, result):
            self.result = result

    result = FilteringUseCase(
        ScreeningRepository(), object(), HistoricalVolumeClient(), ResultRepository()
    ).execute(target_date=date(2026, 9, 10))
    assert result.symbols == []


def test_monthly_analysis_helpers_skip_corrupt_files_and_parse_legacy_values(tmp_path):
    with pytest.raises(ValueError):
        _month_bounds('invalid')

    reports = tmp_path / 'reports'
    reports.mkdir()
    (reports / '2026-09-01.json').write_text('{', encoding='utf-8')
    (reports / '2026-09-02.json').write_text('[]', encoding='utf-8')
    (reports / '2026-09-03.json').write_text('{"order_count": 2}', encoding='utf-8')
    daily = _load_daily_summaries(reports, '2026-09')
    assert daily == [{
        'date': '2026-09-03',
        'trading_mode': None,
        'order_count': 2,
        'total_profit_loss': 0,
        'kill_switch_triggered': False,
        'emergency_stop_triggered': False,
        'market_assessment_status': 'not_recorded',
        'market_regime': None,
        'market_failure_reason': None,
        'log_error_count': 0,
        'log_error_recorded': False,
        'log_errors': {
            'recorded': False,
            'count': 0,
            'board_timeout': {
                'count': 0,
                'recovered': 0,
                'final_failure': 0,
                'unrecorded': 0,
                'symbols': {'recovered': [], 'final_failure': [], 'unrecorded': []},
            },
            'other_errors': {'count': 0, 'groups': []},
        },
        'position_count': None,
        'skip_counts': {},
        'skip_evaluations': {
            'ATR危険度': [],
            '市場危険度': [],
            '注意レジームRSI': [],
        },
    }]

    backtests = tmp_path / 'backtests'
    backtests.mkdir()
    (backtests / 'latest_timeseries_bad.json').write_text('{', encoding='utf-8')
    (backtests / 'latest_timeseries_legacy.json').write_text(
        '{"generated_at":"2026-09-03T10:00:00", "総損益": 10, "総取引数": 2, "勝率": 0.5, "最大ドローダウン": 3, "最終保有数": 0}',
        encoding='utf-8',
    )
    result = _load_backtest_summaries(backtests, date(2026, 9, 1), date(2026, 9, 30))
    assert result[0]['total_pnl'] == 10
    assert result[0]['total_trades'] == 2


def test_market_workflow_lock_yields_false_when_lock_is_unavailable(monkeypatch):
    monkeypatch.setattr(execution_lock, 'LOCK_FILE', execution_lock.Path('test-market-workflow.lock'))
    monkeypatch.setattr(execution_lock, '_lock', lambda handle: (_ for _ in ()).throw(OSError('busy')))
    try:
        with execution_lock.market_workflow_lock() as acquired:
            assert acquired is False
    finally:
        execution_lock.LOCK_FILE.unlink(missing_ok=True)


def test_domain_rules_reject_invalid_data_and_detect_duplicate_orders():
    assert calculate_price_limit([]) is None
    assert calculate_price_limit([100.0, 101.0], period=5) is None
    assert calculate_rsi([100.0] * 4, period=2, minimum_closes=5) is None
    assert calculate_rsi([100.0, 0.0, 100.0, 100.0, 100.0], period=2, minimum_closes=5) is None
    assert calculate_rsi([100.0] * 5, period=2, minimum_closes=5) == 50.0

    signal = TradeSignal('7203', OrderSide.BUY, 100.0, 100)
    entry = OrderHistoryEntry('7203', OrderSide.BUY, 100.0, 100, datetime.now().isoformat())
    assert is_duplicate_order(signal, [entry])
    assert is_recent_order(signal, [entry], lock_seconds=60)
    assert not is_recent_order(signal, [], lock_seconds=60)


def test_yahoo_daily_closes_returns_empty_for_malformed_response(monkeypatch):
    monkeypatch.setattr(
        'src.infrastructure.market_data.get_daily_closes.request_handler.send_get',
        lambda *args, **kwargs: {'chart': {'result': [{'timestamp': ['bad'], 'indicators': {}}]}},
    )
    assert get_yahoo_daily_closes('7203') == []


def test_external_empty_and_invalid_api_values_are_rejected(monkeypatch):
    monkeypatch.setattr('src.infrastructure.kabu.get_apisoftlimit.request_handler.send_get', lambda *args, **kwargs: None)
    assert get_api_soft_limit('token') is None
    monkeypatch.setattr('src.infrastructure.kabu.get_apisoftlimit.request_handler.send_get', lambda *args, **kwargs: {'Stock': 'invalid'})
    assert get_api_soft_limit('token') is None

    monkeypatch.setattr('src.infrastructure.market_data.get_daily_closes.request_handler.send_get', lambda *args, **kwargs: {'chart': {'result': []}})
    assert get_yahoo_daily_closes('7203') == []

    monkeypatch.setattr('src.infrastructure.kabu.get_token.request_handler.send_post', lambda *args, **kwargs: None)
    assert get_api_token() is None

    monkeypatch.setattr('src.infrastructure.kabu.board_repository.request_handler.send_get', lambda *args, **kwargs: None)
    board = BoardRepository('token')
    assert board.get_current_board('7203') is None
    assert board.get_current_price('7203') is None

    monkeypatch.setattr(
        'src.infrastructure.market_data.get_daily_closes.request_handler.send_get',
        lambda *args, **kwargs: {'chart': {'result': [{'timestamp': ['invalid'], 'indicators': {'quote': [{'close': [1]}]}}]}},
    )
    assert get_yahoo_daily_closes('7203') == []


def test_filtering_result_repository_handles_initial_and_date_loads(tmp_path):
    repository = FilteringResultRepository(tmp_path)
    assert repository.load_latest() is None
    result = FilteringResult('2026-09-12', ['7203'], 'now')
    repository.save(result)
    assert repository.load_latest().symbols == ['7203']
    assert repository.load_for_date(date(2026, 9, 12)).symbols == ['7203']
    assert repository.load_for_date(date(2026, 9, 11)) is None