"""状態ファイル(Paper状態・注文履歴)の破損・保存失敗への対応のテスト。実注文・Slack送信は行わない。"""
import json
import os
from datetime import date, datetime
from types import SimpleNamespace

import pytest

from src.application.trading_usecase import JST, TradingUseCase
from src.config import config
from src.domain.models import PriceLimit
from src.domain.volatility import VolatilityLevel
from src.infrastructure.paper.paper_order_client import PaperOrderClient
from src.infrastructure.persistence import storage
from src.infrastructure.persistence.storage import (
    StateFileCorruptError,
    read_json_strict,
    write_json,
)

VALID_STATE = {
    "cash": 500000.0,
    "holdings": {"7203": 100},
    "average_costs": {"7203": 1000.0},
    "next_order_id": 5,
    "realized_pnl": 0.0,
    "realized_pnl_date": "2026-01-01",
}


def _corrupt_copies(path):
    return sorted(path.parent.glob(f"{path.name}.corrupt-*"))


# --- 読み込み ---

def test_missing_state_file_starts_with_initial_values(tmp_path):
    client = PaperOrderClient(prices={}, cash=123.0, state_path=tmp_path / "state.json")
    assert client.cash == 123.0
    assert client.holdings == {}
    assert _corrupt_copies(tmp_path / "state.json") == []


@pytest.mark.parametrize(
    "content",
    [
        "not-json",
        "[1, 2]",
        json.dumps({"cash": "abc"}),
        json.dumps({"cash": 1.0, "holdings": {"7203": "x"}}),
        json.dumps({"cash": 1.0, "holdings": [1]}),
        json.dumps({"cash": 1.0, "next_order_id": None}),
    ],
)
def test_corrupt_state_raises_keeps_original_and_leaves_copy(tmp_path, content):
    path = tmp_path / "state.json"
    path.write_text(content, encoding="utf-8")

    with pytest.raises(StateFileCorruptError, match="ファイルを削除してください") as excinfo:
        PaperOrderClient(prices={}, state_path=path)

    assert str(path) in str(excinfo.value)
    assert path.read_text(encoding="utf-8") == content
    copies = _corrupt_copies(path)
    assert len(copies) == 1
    assert copies[0].read_text(encoding="utf-8") == content

    # 元のファイルが残るので、再起動しても同じ例外になる
    with pytest.raises(StateFileCorruptError):
        PaperOrderClient(prices={}, state_path=path)


def test_valid_state_is_restored(tmp_path):
    path = tmp_path / "state.json"
    path.write_text(json.dumps(VALID_STATE), encoding="utf-8")
    client = PaperOrderClient(prices={}, state_path=path)
    assert client.cash == 500000.0
    assert client.holdings == {"7203": 100}


def test_read_error_raises_state_file_corrupt(tmp_path, monkeypatch):
    path = tmp_path / "state.json"
    path.write_text("{}", encoding="utf-8")
    real_open = open

    def failing_open(file, mode="r", *args, **kwargs):
        if str(file) == str(path) and "r" in mode:
            raise PermissionError("denied")
        return real_open(file, mode, *args, **kwargs)

    monkeypatch.setattr("builtins.open", failing_open)
    monkeypatch.setattr(storage.shutil, "copy2", lambda *a, **k: (_ for _ in ()).throw(OSError("copy failed")))
    with pytest.raises(StateFileCorruptError) as excinfo:
        read_json_strict(path)
    assert excinfo.value.copy_path is None


def test_state_path_none_is_not_affected():
    client = PaperOrderClient(prices={}, cash=10.0, state_path=None)
    assert client.cash == 10.0
    assert client.retry_save_state() is True
    assert client.consecutive_save_failures == 0


# --- write_json ---

def test_write_json_returns_true_on_success(tmp_path):
    path = tmp_path / "a.json"
    assert write_json(path, {"a": 1}) is True
    assert json.loads(path.read_text(encoding="utf-8")) == {"a": 1}
    assert list(tmp_path.glob("*.tmp")) == []


def test_write_json_returns_false_on_oserror_without_raising(tmp_path):
    assert write_json(tmp_path / "missing_dir" / "a.json", {"a": 1}) is False


def test_write_json_failure_keeps_original_and_removes_temp_file(tmp_path, monkeypatch):
    path = tmp_path / "a.json"
    path.write_text('{"old": true}', encoding="utf-8")

    def failing_replace(src, dst):
        raise OSError("replace failed")

    monkeypatch.setattr(os, "replace", failing_replace)
    assert write_json(path, {"new": True}) is False
    assert json.loads(path.read_text(encoding="utf-8")) == {"old": True}
    assert list(tmp_path.glob("*.tmp")) == []


def test_write_json_serialization_failure_keeps_original(tmp_path):
    path = tmp_path / "a.json"
    path.write_text('{"old": true}', encoding="utf-8")
    with pytest.raises(TypeError):
        write_json(path, {"bad": object()})
    assert json.loads(path.read_text(encoding="utf-8")) == {"old": True}
    assert list(tmp_path.glob("*.tmp")) == []


# --- 環境変数 ---

def test_threshold_validation():
    name = "STATE_SAVE_CONSECUTIVE_FAILURE_THRESHOLD"
    assert config._positive_int_env(name, "3") == 3
    assert config._positive_int_env(name, " 5 ") == 5
    for bad in ("0", "-1", "abc", "", "1.5"):
        with pytest.raises(ValueError, match=name):
            config._positive_int_env(name, bad)


# --- 保存失敗の通知・新規買い停止 ---

def _use_case(tmp_path, messages, order_sender=None):
    return TradingUseCase(
        token="dummy",
        order_history_path=tmp_path / "order_history.json",
        order_sender=order_sender,
        notifier=messages.append,
        daily_report_directory=tmp_path / "reports",
    )


def test_order_history_save_failure_notifies_once_then_recovers(tmp_path, monkeypatch):
    results = iter([False, False, True, False])
    monkeypatch.setattr("src.application.trading_usecase.write_json", lambda *a, **k: next(results))
    messages = []
    use_case = _use_case(tmp_path, messages)

    assert use_case._save_order_history() is False
    assert len(messages) == 1
    assert "order_history.json" in messages[0] and "連続失敗回数: 1" in messages[0]
    use_case._save_order_history()
    assert len(messages) == 1
    assert use_case._order_history_save_failures == 2
    assert use_case._save_order_history() is True
    assert use_case._order_history_save_failures == 0
    use_case._save_order_history()
    assert len(messages) == 2  # 成功で連続が途切れたため、再び失敗すれば通知する


def test_halt_reaches_threshold_and_releases_after_success(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "STATE_SAVE_CONSECUTIVE_FAILURE_THRESHOLD", 2)
    saves = {"ok": False}
    monkeypatch.setattr("src.application.trading_usecase.write_json", lambda *a, **k: saves["ok"])
    use_case = _use_case(tmp_path, [])
    use_case._save_order_history()
    assert use_case._is_new_buy_halted_by_save_failure() is False  # 1回では止めない
    use_case._order_history_save_failures = 2
    assert use_case._is_new_buy_halted_by_save_failure() is True
    saves["ok"] = True
    assert use_case._is_new_buy_halted_by_save_failure() is False


def test_paper_state_save_failure_is_detected_and_released(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "STATE_SAVE_CONSECUTIVE_FAILURE_THRESHOLD", 2)
    paper_ok = {"ok": False}
    monkeypatch.setattr(
        "src.infrastructure.paper.paper_order_client.write_json", lambda *a, **k: paper_ok["ok"]
    )
    client = PaperOrderClient(prices={"7203": 100.0}, state_path=tmp_path / "state.json")
    messages = []
    use_case = _use_case(tmp_path, messages, order_sender=client)

    client.place_market_order("t", "7203", config.OrderSide.BUY.value, 100)
    use_case._save_order_history()
    assert client.consecutive_save_failures == 1
    assert any("state.json" in m for m in messages)
    assert use_case._is_new_buy_halted_by_save_failure() is False

    client.place_market_order("t", "7203", config.OrderSide.BUY.value, 100)
    assert client.consecutive_save_failures == 2
    assert use_case._is_new_buy_halted_by_save_failure() is True

    paper_ok["ok"] = True
    assert use_case._is_new_buy_halted_by_save_failure() is False
    assert client.consecutive_save_failures == 0


def test_halt_stops_new_buys_but_not_sells_or_eod_liquidation(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "STATE_SAVE_CONSECUTIVE_FAILURE_THRESHOLD", 2)
    monkeypatch.setattr(
        "src.application.trading_usecase.write_json",
        lambda path, *a, **k: path.name == "kill_switch_baseline.json",  # 基準保存は成功させ、履歴保存だけ失敗させる
    )
    monkeypatch.setattr("src.infrastructure.paper.paper_order_client.write_json", lambda *a, **k: False)
    monkeypatch.setattr(config, "TRADING_MODE", "paper")
    monkeypatch.setattr(config, "IS_DEMO", True)
    monkeypatch.setattr(config, "API_SOFT_LIMIT", 100_000.0)
    monkeypatch.setattr(config, "MAX_ORDER_AMOUNT_PER_TRADE", 30_000.0)
    monkeypatch.setattr(config, "TARGET_POSITIONS", 4)
    monkeypatch.setattr(config, "MARKET_CLOSE_HOUR", 10)
    monkeypatch.setattr(config, "MARKET_CLOSE_MINUTE", 2)
    monkeypatch.setattr(config, "ALLOW_OVERNIGHT_HOLDING", False)
    monkeypatch.setattr(config, "EMERGENCY_STOP_FILE", tmp_path / "emergency_stop.flag")
    monkeypatch.setattr(config, "LOG_DIRECTORY", tmp_path / "logs")
    monkeypatch.setattr(
        "src.application.trading_usecase.calculate_price_limit", lambda closes: PriceLimit(80.0, 99.0)
    )
    monkeypatch.setattr("src.application.trading_usecase.calculate_rsi", lambda *a, **k: 60.0)
    monkeypatch.setattr(
        "src.application.trading_usecase.assess_volatility",
        lambda *a, **k: SimpleNamespace(
            atr=2.0, ratio=1.0, level=VolatilityLevel.NORMAL, latest_true_range=2.0
        ),
    )

    class MarketDataClient:
        def get_yahoo_daily_closes(self, symbol):
            return [100.0] * 5

        def get_yahoo_daily_bars(self, symbol):
            return [object()]

    class BoardClient:
        def get_current_board(self, token, symbol):
            return {"current_price": 100.0}

        def get_current_board_with_freshness(self, token, symbol):
            return {
                "current_price": 100.0,
                "current_price_status": 1,
                "current_price_time": datetime(2026, 9, 25, 10, 2, tzinfo=JST).isoformat(),
            }
    trade_date = date(2026, 9, 25)
    order_sender = PaperOrderClient(
        prices={},
        cash=1_000_000.0,
        fee_rate=0.0,
        market_slippage_bps=0.0,
        state_path=tmp_path / "state.json",
        realized_pnl_date=trade_date.isoformat(),
        today_provider=lambda: trade_date,
    )
    messages = []
    use_case = TradingUseCase(
        token="dummy",
        order_history_path=tmp_path / "order_history.json",
        market_data_client=MarketDataClient(),
        board_client=BoardClient(),
        order_sender=order_sender,
        notifier=messages.append,
        daily_analyzer=SimpleNamespace(analyze=lambda summary: None),
        daily_report_directory=tmp_path / "reports",
        kill_switch_baseline_path=tmp_path / "kill_switch_baseline.json",
    )
    symbols_path = tmp_path / "top_symbols.json"
    symbols_path.write_text(json.dumps(["1111", "2222", "3333"]), encoding="utf-8")
    times = [datetime(2026, 9, 25, 10, 0), datetime(2026, 9, 25, 10, 1), datetime(2026, 9, 25, 10, 2)]
    tick = [0]

    use_case.run(
        top_symbols_path=symbols_path,
        now_provider=lambda: times[tick[0]],
        sleep=lambda seconds: tick.__setitem__(0, tick[0] + 1),
    )

    sides = [(o["Symbol"], o["Side"]) for o in order_sender.orders]
    buy, sell = config.OrderSide.BUY.value, config.OrderSide.SELL.value
    assert sides == [("1111", buy), ("2222", buy), ("1111", sell), ("2222", sell)]
    assert order_sender.get_positions("dummy") == []
    assert sum("NEW_BUY_HALTED_STATE_SAVE_FAILURE" in m for m in messages) == 1
    assert any("連続失敗回数: 1" in m for m in messages)


# --- キルスイッチ基準資本 ---

def _baseline_use_case(tmp_path, messages):
    return TradingUseCase(
        token="dummy",
        order_history_path=tmp_path / "order_history.json",
        notifier=messages.append,
        daily_report_directory=tmp_path / "reports",
        kill_switch_baseline_path=tmp_path / "kill_switch_baseline.json",
    )


def test_kill_switch_baseline_failure_keeps_memory_value_notifies_once_and_recovers(tmp_path, monkeypatch):
    saves = {"ok": False}
    monkeypatch.setattr("src.application.trading_usecase.write_json", lambda *a, **k: saves["ok"])
    messages = []
    use_case = _baseline_use_case(tmp_path, messages)

    use_case._initialize_daily_starting_capital("2026-09-25", 100_000.0, [])
    assert use_case.daily_starting_capital == 100_000.0
    assert len(messages) == 1 and "kill_switch_baseline.json" in messages[0]
    use_case._retry_kill_switch_baseline_save()
    assert len(messages) == 1
    assert use_case._kill_switch_baseline_save_failures == 2

    saves["ok"] = True
    use_case._retry_kill_switch_baseline_save()
    assert use_case._kill_switch_baseline_save_failures == 0
    assert use_case._kill_switch_baseline_unsaved is None
    assert use_case.daily_starting_capital == 100_000.0


def test_kill_switch_baseline_consecutive_failures_halt_new_buys_until_saved(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "STATE_SAVE_CONSECUTIVE_FAILURE_THRESHOLD", 2)
    saves = {"ok": False}
    monkeypatch.setattr("src.application.trading_usecase.write_json", lambda *a, **k: saves["ok"])
    use_case = _baseline_use_case(tmp_path, [])

    use_case._initialize_daily_starting_capital("2026-09-25", 100_000.0, [])
    assert use_case._is_new_buy_halted_by_save_failure() is False  # 1回では止めない
    use_case._retry_kill_switch_baseline_save()  # ループごとの再保存
    assert use_case._is_new_buy_halted_by_save_failure() is True
    saves["ok"] = True
    assert use_case._is_new_buy_halted_by_save_failure() is False


def test_kill_switch_baseline_success_is_unchanged(tmp_path):
    messages = []
    use_case = _baseline_use_case(tmp_path, messages)
    use_case._initialize_daily_starting_capital("2026-09-25", 100_000.0, [])
    saved = json.loads((tmp_path / "kill_switch_baseline.json").read_text(encoding="utf-8"))
    assert saved == {"date": "2026-09-25", "capital": 100_000.0}
    assert messages == []


def test_corrupt_kill_switch_baseline_stops_and_preserves_copy(tmp_path):
    path = tmp_path / "kill_switch_baseline.json"
    content = "{not-json"
    path.write_text(content, encoding="utf-8")
    use_case = _baseline_use_case(tmp_path, [])

    with pytest.raises(StateFileCorruptError):
        use_case._initialize_daily_starting_capital("2026-09-25", 50_000.0, [])

    assert path.read_text(encoding="utf-8") == content
    copies = _corrupt_copies(path)
    assert len(copies) == 1
    assert copies[0].read_text(encoding="utf-8") == content


@pytest.mark.parametrize(
    "baseline",
    [
        {"date": "2026-09-25", "capital": "not-a-number"},
        {"date": "2026-09-25", "capital": 0},
        {"date": "not-a-date", "capital": 50_000},
    ],
)
def test_invalid_kill_switch_baseline_values_stop_and_preserve_copy(
    tmp_path, baseline
):
    path = tmp_path / "kill_switch_baseline.json"
    content = json.dumps(baseline)
    path.write_text(content, encoding="utf-8")
    use_case = _baseline_use_case(tmp_path, [])

    with pytest.raises(StateFileCorruptError):
        use_case._initialize_daily_starting_capital("2026-09-25", 50_000.0, [])

    assert path.read_text(encoding="utf-8") == content
    copies = _corrupt_copies(path)
    assert len(copies) == 1
    assert copies[0].read_text(encoding="utf-8") == content


def test_same_day_kill_switch_baseline_is_loaded_without_recalculation(tmp_path):
    path = tmp_path / "kill_switch_baseline.json"
    path.write_text(
        json.dumps({"date": "2026-09-25", "capital": 75_000.0}),
        encoding="utf-8",
    )
    use_case = _baseline_use_case(tmp_path, [])

    use_case._initialize_daily_starting_capital("2026-09-25", 50_000.0, [])

    assert use_case.daily_starting_capital == 75_000.0
    assert json.loads(path.read_text(encoding="utf-8")) == {
        "date": "2026-09-25",
        "capital": 75_000.0,
    }


def test_previous_day_kill_switch_baseline_is_replaced_normally(tmp_path):
    path = tmp_path / "kill_switch_baseline.json"
    path.write_text(
        json.dumps({"date": "2026-09-24", "capital": 75_000.0}),
        encoding="utf-8",
    )
    use_case = _baseline_use_case(tmp_path, [])

    use_case._initialize_daily_starting_capital("2026-09-25", 50_000.0, [])

    assert use_case.daily_starting_capital == 50_000.0
    assert json.loads(path.read_text(encoding="utf-8")) == {
        "date": "2026-09-25",
        "capital": 50_000.0,
    }


# --- 日次レポート ---

def _report_case(tmp_path, monkeypatch):
    monkeypatch.setattr("src.application.trading_usecase.time.sleep", lambda s: None)
    messages = []
    return _baseline_use_case(tmp_path, messages), messages


def test_daily_report_write_failure_retries_then_still_sends_result(tmp_path, monkeypatch):
    use_case, messages = _report_case(tmp_path, monkeypatch)
    calls = []
    monkeypatch.setattr(
        "src.application.trading_usecase.write_json", lambda *a, **k: calls.append(a) or False
    )
    use_case._send_end_of_day_report()

    assert len(calls) == 2
    assert any("日次レポートの保存に失敗しました" in m and ".json" in m for m in messages)
    result = [m for m in messages if "トレード終了" in m]
    assert len(result) == 1 and "日次レポート保存失敗" in result[0]


def test_daily_report_mkdir_failure_still_sends_result(tmp_path, monkeypatch):
    use_case, messages = _report_case(tmp_path, monkeypatch)
    (tmp_path / "reports").write_text("file", encoding="utf-8")  # mkdir が失敗する
    use_case._send_end_of_day_report()

    assert any("日次レポートの保存に失敗しました" in m for m in messages)
    assert any("トレード終了" in m and "日次レポート保存失敗" in m for m in messages)


def test_daily_report_retry_success_is_silent(tmp_path, monkeypatch):
    use_case, messages = _report_case(tmp_path, monkeypatch)
    real = storage.write_json
    results = iter([False])
    monkeypatch.setattr(
        "src.application.trading_usecase.write_json",
        lambda p, d: False if next(results, True) is False else real(p, d),
    )
    use_case._send_end_of_day_report()
    assert len(messages) == 1 and "日次レポート保存失敗" not in messages[0]


def test_daily_report_success_is_unchanged(tmp_path, monkeypatch):
    use_case, messages = _report_case(tmp_path, monkeypatch)
    use_case._send_end_of_day_report()
    assert len(messages) == 1 and "日次レポート保存失敗" not in messages[0]
    assert list((tmp_path / "reports").glob("*.json"))
