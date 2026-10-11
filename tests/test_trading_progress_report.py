from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.domain.trading_progress_report import build_trading_progress_report
from src.application.trading_usecase import TradingUseCase
from src.infrastructure.market_data.yahoo_index_client import YahooIndexClient


JST = timezone(timedelta(hours=9))


def test_progress_report_formats_snapshot_and_reports_no_activity():
    report = build_trading_progress_report(
        reported_at=datetime(2026, 10, 5, 11, 30, tzinfo=JST),
        scheduled_time="11:30",
        order_count=0,
        order_count_label="約定件数",
        realized_pnl=0,
        holdings=[],
        market_regime="CAUTION",
        nikkei_change_percent=-1.25,
        events=[],
    )

    assert "約定件数: 0件" in report
    assert "確定損益: +0円" in report
    assert "保有銘柄: なし" in report
    assert "朝のMarketRegime: CAUTION" in report
    assert "日経225当日値動き（前日終値比）: -1.25%" in report
    assert "動きなし" in report


def test_progress_report_does_not_invent_unavailable_pnl():
    report = build_trading_progress_report(
        reported_at=datetime(2026, 10, 5, 14, 0, tzinfo=JST),
        scheduled_time="14:00",
        order_count=1,
        order_count_label="約定件数",
        realized_pnl=None,
        holdings=[{"symbol": "7203", "quantity": 100, "profit_loss": None}],
        market_regime="NORMAL",
        nikkei_change_percent=None,
        events=["新規買い見送り: 7203"],
    )

    assert "確定損益: 取得不可" in report
    assert "含み損益: 取得不可" in report
    assert "7203 100株: 取得不可" in report
    assert "新規買い見送り: 7203" in report


def test_progress_report_separates_unheld_sell_observation_from_safety_blocks(tmp_path):
    class FilterEvents:
        def load_summaries(self, **kwargs):
            return []

    class DecisionJournal:
        def load_records(self, *args):
            return [
                {
                    "reason_code": "SELL_SIGNAL_WITHOUT_HOLDINGS",
                    "symbol": "4563",
                    "occurrence_count": 2,
                    "last_occurred_at": "2026-10-09T10:00:00",
                },
                {
                    "reason_code": "ORDER_SAFETY_BLOCKED",
                    "symbol": "7203",
                    "occurrence_count": 5,
                    "last_occurred_at": "2026-10-09T10:00:00",
                },
            ]

    use_case = TradingUseCase(
        token="test",
        order_history_path=Path(tmp_path / "orders.json"),
        filter_decision_repository=FilterEvents(),
        decision_journal_repository=DecisionJournal(),
    )

    events = use_case._progress_report_events(datetime(2026, 10, 9, 11, 30))

    assert "売り条件該当・未保有: 4563 (2回)" in events
    assert "安全条件による注文見送り: 7203 (5回)" in events


def test_yahoo_index_client_fetches_intraday_change_with_one_request(monkeypatch):
    previous_time = datetime(2026, 10, 2, 15, 29, tzinfo=JST).timestamp()
    current_time = datetime(2026, 10, 5, 11, 29, tzinfo=JST).timestamp()
    response = {
        "chart": {
            "result": [{
                "timestamp": [previous_time, current_time],
                "indicators": {"quote": [{"close": [45_000.0, 45_450.0]}]},
            }],
        },
    }
    calls = []

    def send_get(url, **kwargs):
        calls.append((url, kwargs))
        return response

    monkeypatch.setattr(
        "src.infrastructure.market_data.yahoo_index_client.request_handler.send_get",
        send_get,
    )
    change = YahooIndexClient().get_intraday_change_percent(
        "^N225", datetime(2026, 10, 5, 11, 30, tzinfo=JST)
    )

    assert change == pytest.approx(1.0)
    assert len(calls) == 1
    assert calls[0][1]["params"] == {"interval": "1m", "range": "5d"}


def test_due_progress_report_is_sent_once_and_reads_index_once(tmp_path):
    notifications = []
    index_calls = []
    position_calls = []

    class IndexClient:
        def get_intraday_change_percent(self, symbol, as_of):
            index_calls.append((symbol, as_of))
            return 0.5

    class PositionsClient:
        def get_positions(self, token):
            position_calls.append(token)
            return []

    class OrderSender:
        def get_daily_realized_pnl(self):
            return 12.0

    class FilterEvents:
        def load_summaries(self, **kwargs):
            return []

    use_case = TradingUseCase(
        token="test",
        order_history_path=Path(tmp_path / "orders.json"),
        positions_client=PositionsClient(),
        order_sender=OrderSender(),
        market_regime_usecase=SimpleNamespace(market_data_client=IndexClient()),
        filter_decision_repository=FilterEvents(),
        notifier=notifications.append,
    )
    use_case._progress_report_start_time = datetime(2026, 10, 5, 9, 35, tzinfo=JST)
    previous = datetime(2026, 10, 5, 11, 29, tzinfo=JST)
    current = datetime(2026, 10, 5, 11, 30, tzinfo=JST)

    use_case._send_due_progress_reports(previous, current)
    use_case._send_due_progress_reports(previous, current)

    assert len(notifications) == 1
    assert "中間報告（11:30）" in notifications[0]
    assert "約定件数: 0件" in notifications[0]
    assert "確定損益: +12円" in notifications[0]
    assert len(index_calls) == 1
    assert len(position_calls) == 1


def test_progress_report_does_not_send_a_missed_schedule(tmp_path):
    notifications = []
    use_case = TradingUseCase(
        token="test",
        order_history_path=Path(tmp_path / "orders.json"),
        notifier=notifications.append,
    )
    use_case._progress_report_start_time = datetime(2026, 10, 5, 11, 31)

    use_case._send_due_progress_reports(
        datetime(2026, 10, 5, 11, 59), datetime(2026, 10, 5, 12, 0)
    )

    assert notifications == []