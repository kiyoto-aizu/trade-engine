import pytest
from datetime import date

from src.config import config
from src.application.trading_usecase import TradingUseCase
from src.infrastructure.paper.paper_order_client import PaperOrderClient


def test_paper_order_client_updates_virtual_cash_holdings_and_order_log():
    executor = PaperOrderClient(
        prices={'7203': 90.0}, cash=10_000.0, order_qty=100,
        fee_rate=0.0, market_slippage_bps=0.0,
    )

    buy_result = executor.place_market_order('unused', '7203', config.OrderSide.BUY.value)
    sell_result = executor.place_market_order('unused', '7203', config.OrderSide.SELL.value)

    assert buy_result['Result'] == 0
    assert sell_result['OrderId'] == 'paper-2'
    assert executor.cash == 10_000.0
    assert executor.holdings == {}
    assert len(executor.orders) == 2


def test_paper_order_client_applies_market_slippage_and_round_trip_fees():
    executor = PaperOrderClient(
        prices={'7203': 100.0},
        cash=20_000.0,
        order_qty=100,
        fee_rate=0.001,
        market_slippage_bps=5.0,
    )

    buy_result = executor.place_market_order('unused', '7203', config.OrderSide.BUY.value)
    sell_result = executor.place_market_order('unused', '7203', config.OrderSide.SELL.value)

    assert buy_result['Price'] == 100.05
    assert buy_result['Fee'] == 10.005
    assert sell_result['Price'] == 99.95
    assert sell_result['Fee'] == pytest.approx(9.995)
    assert executor.cash == pytest.approx(19_970.0)
    assert executor.holdings == {}
    assert executor.get_daily_realized_pnl() == pytest.approx(-30.0)


def test_paper_order_client_uses_injected_date_for_daily_realized_pnl():
    simulated_date = [date(2026, 9, 25)]
    executor = PaperOrderClient(
        prices={'7203': 100.0},
        cash=20_000.0,
        order_qty=100,
        fee_rate=0.0,
        market_slippage_bps=0.0,
        realized_pnl_date=simulated_date[0].isoformat(),
        today_provider=lambda: simulated_date[0],
    )
    executor.place_market_order('unused', '7203', config.OrderSide.BUY.value)
    executor.set_price('7203', 90.0)
    executor.place_market_order('unused', '7203', config.OrderSide.SELL.value)

    assert executor.get_daily_realized_pnl() == -1_000.0

    simulated_date[0] = date(2026, 9, 28)
    assert executor.get_daily_realized_pnl() == 0.0
    assert executor.realized_pnl_date == '2026-09-28'


def test_paper_order_client_persists_fee_inclusive_average_cost(tmp_path):
    state_path = tmp_path / 'paper_account_state.json'
    executor = PaperOrderClient(
        prices={'7203': 100.0}, cash=20_000.0, order_qty=100,
        fee_rate=0.001, market_slippage_bps=5.0, state_path=state_path,
    )
    executor.place_market_order('unused', '7203', config.OrderSide.BUY.value)

    restarted = PaperOrderClient(prices={'7203': 100.0}, state_path=state_path)

    assert restarted.average_costs['7203'] == pytest.approx(100.15005)
    position = restarted.get_positions('unused')[0]
    assert position['AveragePrice'] == pytest.approx(100.15005)
    assert TradingUseCase._get_position_entry_price(position) == pytest.approx(100.15005)
    assert restarted.get_positions('unused')[0]['ProfitLoss'] == -15.0


def test_paper_order_client_persists_daily_realized_pnl(tmp_path):
    state_path = tmp_path / 'paper_account_state.json'
    executor = PaperOrderClient(
        prices={'7203': 100.0}, cash=20_000.0, order_qty=100,
        fee_rate=0.0, market_slippage_bps=0.0, state_path=state_path,
    )
    executor.place_market_order('unused', '7203', config.OrderSide.BUY.value)
    executor.prices['7203'] = 90.0
    executor.place_market_order('unused', '7203', config.OrderSide.SELL.value)

    restarted = PaperOrderClient(prices={'7203': 90.0}, state_path=state_path)

    assert restarted.get_daily_realized_pnl() == -1000.0


def test_paper_order_client_rejects_orders_without_changing_state():
    executor = PaperOrderClient(prices={'7203': 90.0}, cash=1_000.0, order_qty=100)

    buy_result = executor.place_market_order('unused', '7203', config.OrderSide.BUY.value)
    sell_result = executor.place_market_order('unused', '7203', config.OrderSide.SELL.value)

    assert buy_result is None
    assert sell_result is None
    assert executor.cash == 1_000.0
    assert executor.holdings == {}
    assert executor.orders == []


@pytest.mark.parametrize(
    ("price", "reason"),
    [
        (None, "ORDER_REJECTED_PAPER_PRICE_MISSING"),
        (0, "ORDER_REJECTED_PAPER_PRICE_INVALID"),
        (-1, "ORDER_REJECTED_PAPER_PRICE_INVALID"),
        (float("nan"), "ORDER_REJECTED_PAPER_PRICE_INVALID"),
        (float("inf"), "ORDER_REJECTED_PAPER_PRICE_INVALID"),
        (float("-inf"), "ORDER_REJECTED_PAPER_PRICE_INVALID"),
        ("100", "ORDER_REJECTED_PAPER_PRICE_INVALID"),
    ],
)
@pytest.mark.parametrize("side", [config.OrderSide.BUY.value, config.OrderSide.SELL.value])
def test_paper_order_client_rejects_invalid_prices_without_mutating_state(
    tmp_path, caplog, price, reason, side
):
    state_path = tmp_path / "paper_state.json"
    executor = PaperOrderClient(
        prices={"7203": price},
        cash=10_000.0,
        order_qty=100,
        holdings={"7203": 100},
        average_costs={"7203": 80.0},
        orders=[{"OrderId": "paper-6"}],
        realized_pnl=123.0,
        state_path=state_path,
        _next_order_id=7,
    )
    assert executor._save_state()
    state_before = state_path.read_bytes()

    result = executor.place_market_order("unused", "7203", side)

    assert result is None
    assert executor.last_rejection_reason == reason
    assert executor.cash == 10_000.0
    assert executor.holdings == {"7203": 100}
    assert executor.average_costs == {"7203": 80.0}
    assert executor.orders == [{"OrderId": "paper-6"}]
    assert executor._next_order_id == 7
    assert executor.realized_pnl == 123.0
    assert state_path.read_bytes() == state_before
    assert reason in caplog.text
    assert "7203" in caplog.text


def test_paper_order_client_keeps_price_quality_metadata_on_order():
    executor = PaperOrderClient(
        prices={}, cash=10_000.0, order_qty=100, fee_rate=0.0, market_slippage_bps=0.0,
    )
    executor.set_price(
        "7203",
        90.0,
        {
            "price_source": "last_valid_board",
            "price_quality": "stale",
            "price_observed_at": "2026-10-02T15:08:00",
            "price_age_seconds": 56.0,
        },
    )

    result = executor.place_market_order("unused", "7203", config.OrderSide.BUY.value)

    assert result["price_quality"] == "stale"
    assert result["price_source"] == "last_valid_board"
    assert result["price_age_seconds"] == 56.0


def test_paper_order_client_restores_account_state_after_restart(tmp_path):
    state_path = tmp_path / 'paper_account_state.json'
    executor = PaperOrderClient(
        prices={'7203': 90.0},
        cash=10_000.0,
        order_qty=100,
        state_path=state_path,
    )
    executor.place_market_order('unused', '7203', config.OrderSide.BUY.value)

    restarted = PaperOrderClient(
        prices={'7203': 90.0},
        cash=1.0,
        order_qty=100,
        state_path=state_path,
    )

    assert restarted.cash == pytest.approx(990.547525)
    assert restarted.holdings == {'7203': 100}
    assert restarted.get_wallet_cash('unused')['StockAccountWallet'] == pytest.approx(990.547525)
    assert restarted.get_positions('unused')[0]['HoldQty'] == 100


def test_trading_use_case_uses_paper_account_instead_of_live_account_clients(monkeypatch, tmp_path):
    executor = PaperOrderClient(prices={'7203': 90.0}, cash=10_000.0, order_qty=100)
    executor.place_market_order('unused', '7203', config.OrderSide.BUY.value)
    use_case = TradingUseCase(
        token='unused',
        order_history_path=tmp_path / 'order_history.json',
        order_sender=executor,
    )

    monkeypatch.setattr(
        'src.application.trading_usecase.get_wallet_cash',
        lambda token: (_ for _ in ()).throw(AssertionError('live wallet API was called')),
    )
    monkeypatch.setattr(
        'src.application.trading_usecase.get_positions',
        lambda token: (_ for _ in ()).throw(AssertionError('live positions API was called')),
    )

    wallet, positions = use_case._load_account_state()

    assert wallet == pytest.approx(990.547525)
    assert positions[0]['Symbol'] == '7203'
