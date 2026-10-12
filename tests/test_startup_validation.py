import logging
import os
import subprocess
import sys
from contextlib import nullcontext
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.config import config
from src.config.startup_validation import (
    ConfigValidationError,
    find_config_violations,
    validate_startup_config,
)
from src.domain.market_regime import MarketRegimeThresholds
from src.entrypoints import run_trading
from src.infrastructure.persistence.storage import StateFileCorruptError


def make_settings(**overrides):
    thresholds = overrides.pop("thresholds", {})
    values = dict(
        OPERATING_CAPITAL=100000.0,
        MAX_ORDER_AMOUNT_PER_TRADE=30000.0,
        TARGET_POSITIONS=3,
        MAX_ORDER_COUNT_PER_DAY=10,
        DAILY_LOSS_LIMIT_RATIO=0.02,
        API_SOFT_LIMIT=1000000.0,
        RSI_PERIOD=14,
        RSI_MINIMUM_CLOSES=30,
        RSI_ENTRY_THRESHOLD=55.0,
        RSI_EXIT_THRESHOLD=45.0,
        RSI_ENTRY_THRESHOLD_CAUTION=60.0,
        MARKET_LIQUIDATION_HOUR=15,
        MARKET_LIQUIDATION_MINUTE=20,
        MARKET_OPEN_HOUR=9,
        MARKET_OPEN_MINUTE=0,
        MARKET_CLOSE_HOUR=15,
        MARKET_CLOSE_MINUTE=30,
        TRADING_PROGRESS_REPORT_1_HOUR=11,
        TRADING_PROGRESS_REPORT_1_MINUTE=30,
        TRADING_PROGRESS_REPORT_2_HOUR=14,
        TRADING_PROGRESS_REPORT_2_MINUTE=0,
        BOARD_FETCH_CONSECUTIVE_FAILURE_THRESHOLD=3,
        STATE_SAVE_CONSECUTIVE_FAILURE_THRESHOLD=3,
        LIQUIDATION_POSITIONS_FETCH_RETRIES=3,
        KABU_TOKEN_REFRESH_MIN_INTERVAL_SECONDS=60.0,
        KABU_TOKEN_REFRESH_FAILURE_BACKOFF_SECONDS=300.0,
        API_REQUEST_INTERVAL_SECONDS=0.12,
        LIQUIDATION_POSITIONS_FETCH_RETRY_BACKOFF_SECONDS=5.0,
    )
    t = dict(
        realized_vol_caution=17.0,
        realized_vol_danger=29.0,
        vix_caution=17.0,
        vix_danger=27.0,
        nikkei_change_upgrade=2.0,
    )
    t.update(thresholds)
    values["MARKET_REGIME_THRESHOLDS"] = SimpleNamespace(**t)
    values.update(overrides)
    return SimpleNamespace(**values)


def names(settings):
    return {v.name for v in find_config_violations(settings)}


def test_attribute_names_exist_in_config():
    defaults = make_settings()
    for field in vars(defaults):
        assert hasattr(config, field), field
    for attr in vars(defaults.MARKET_REGIME_THRESHOLDS):
        assert hasattr(config.MARKET_REGIME_THRESHOLDS, attr), attr
    assert validate_startup_config(defaults) == []


def test_config_module_defaults_have_no_violations():
    """.envを読まず、環境変数も空にした子プロセスで、configの既定値を検証する。"""
    code = (
        "import dotenv; dotenv.load_dotenv = lambda *a, **k: False\n"
        "from src.config import config\n"
        "from src.config.startup_validation import find_config_violations\n"
        "v = find_config_violations(config)\n"
        "print('\\n'.join(x.format() for x in v)); raise SystemExit(1 if v else 0)\n"
    )
    root = Path(__file__).resolve().parents[1]
    env = {k: os.environ[k] for k in ("SYSTEMROOT", "PATH", "TEMP", "TMP") if k in os.environ}
    env["ALLOW_MISSING_ENV"] = "true"
    env["TRADING_RUNTIME_ISOLATED"] = "1"
    result = subprocess.run([sys.executable, "-c", code], cwd=root, env=env, capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr

@pytest.mark.parametrize(
    "name,ok,bad",
    [
        ("OPERATING_CAPITAL", [30000], [0, -1]),
        ("API_SOFT_LIMIT", [0.01], [0, -1]),
        ("TARGET_POSITIONS", [1], [0, 1.5]),
        ("MAX_ORDER_COUNT_PER_DAY", [1], [0, 2.5]),
        ("DAILY_LOSS_LIMIT_RATIO", [0.0001, 1], [0, 1.0001]),
        ("RSI_PERIOD", [2], [1, 2.5]),
        ("RSI_ENTRY_THRESHOLD", [46, 55], [-1, 101]),
        ("MARKET_LIQUIDATION_HOUR", [0, 23], [-1, 24]),
        ("MARKET_LIQUIDATION_MINUTE", [0, 59], [-1, 60]),
        ("TRADING_PROGRESS_REPORT_1_HOUR", [9, 11], [-1, 24]),
        ("TRADING_PROGRESS_REPORT_1_MINUTE", [0, 59], [-1, 60]),
        ("TRADING_PROGRESS_REPORT_2_HOUR", [12, 15], [-1, 24]),
        ("TRADING_PROGRESS_REPORT_2_MINUTE", [0, 59], [-1, 60]),
        ("BOARD_FETCH_CONSECUTIVE_FAILURE_THRESHOLD", [1], [0, 1.5]),
        ("STATE_SAVE_CONSECUTIVE_FAILURE_THRESHOLD", [1], [0, 1.5]),
        ("LIQUIDATION_POSITIONS_FETCH_RETRIES", [1], [0, 1.5]),
        ("KABU_TOKEN_REFRESH_MIN_INTERVAL_SECONDS", [0], [-0.1]),
        ("KABU_TOKEN_REFRESH_FAILURE_BACKOFF_SECONDS", [0], [-0.1]),
        ("API_REQUEST_INTERVAL_SECONDS", [0], [-0.1]),
        ("LIQUIDATION_POSITIONS_FETCH_RETRY_BACKOFF_SECONDS", [0], [-0.1]),
    ],
)
def test_boundaries(name, ok, bad):
    for value in ok:
        assert find_config_violations(make_settings(**{name: value})) == [], (name, value)
    for value in bad:
        assert name in names(make_settings(**{name: value})), (name, value)


def test_order_amount_boundary_against_capital():
    assert not find_config_violations(make_settings(MAX_ORDER_AMOUNT_PER_TRADE=100000.0))
    assert "MAX_ORDER_AMOUNT_PER_TRADE" in names(make_settings(MAX_ORDER_AMOUNT_PER_TRADE=100000.01))
    assert "MAX_ORDER_AMOUNT_PER_TRADE" in names(make_settings(MAX_ORDER_AMOUNT_PER_TRADE=0))


def test_rsi_relations():
    assert not find_config_violations(make_settings(RSI_MINIMUM_CLOSES=15))
    assert "RSI_MINIMUM_CLOSES" in names(make_settings(RSI_MINIMUM_CLOSES=14))
    assert not find_config_violations(make_settings(RSI_ENTRY_THRESHOLD_CAUTION=55.0))
    assert "RSI_ENTRY_THRESHOLD_CAUTION" in names(make_settings(RSI_ENTRY_THRESHOLD_CAUTION=54.9))
    assert not find_config_violations(make_settings(RSI_EXIT_THRESHOLD=54.9))
    assert "RSI_EXIT_THRESHOLD" in names(make_settings(RSI_EXIT_THRESHOLD=55.0))
    assert "RSI_EXIT_THRESHOLD" in names(make_settings(RSI_EXIT_THRESHOLD=70.0))


def test_regime_relations():
    assert "MARKET_REGIME_REALIZED_VOL_CAUTION/_DANGER" in names(
        make_settings(thresholds=dict(realized_vol_danger=17.0))
    )
    assert "MARKET_REGIME_VIX_CAUTION/_DANGER" in names(make_settings(thresholds=dict(vix_danger=16.0)))
    assert not find_config_violations(make_settings(thresholds=dict(vix_danger=17.01)))
    assert "MARKET_REGIME_NIKKEI_CHANGE_UPGRADE" in names(make_settings(thresholds=dict(nikkei_change_upgrade=0)))
    assert not find_config_violations(make_settings(thresholds=dict(nikkei_change_upgrade=0.01)))


def test_progress_report_times_must_be_in_session_and_ordered():
    assert "TRADING_PROGRESS_REPORT_1_HOUR" in names(
        make_settings(TRADING_PROGRESS_REPORT_1_HOUR=8)
    )
    assert "TRADING_PROGRESS_REPORT_2_HOUR/MINUTE" in names(
        make_settings(TRADING_PROGRESS_REPORT_2_HOUR=11, TRADING_PROGRESS_REPORT_2_MINUTE=30)
    )
    assert not find_config_violations(make_settings())


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf"), True, False, "10", None])
def test_nan_inf_bool_and_non_numbers_are_rejected(bad):
    for name in ("OPERATING_CAPITAL", "TARGET_POSITIONS", "DAILY_LOSS_LIMIT_RATIO",
                 "API_REQUEST_INTERVAL_SECONDS", "RSI_ENTRY_THRESHOLD", "MARKET_LIQUIDATION_HOUR"):
        assert name in names(make_settings(**{name: bad})), (name, bad)
    assert "MARKET_REGIME_VIX_CAUTION/_DANGER" in names(make_settings(thresholds=dict(vix_caution=bad)))
    assert "MARKET_REGIME_NIKKEI_CHANGE_UPGRADE" in names(make_settings(thresholds=dict(nikkei_change_upgrade=bad)))


def test_missing_attribute_is_a_violation():
    settings = make_settings()
    del settings.API_SOFT_LIMIT
    assert "API_SOFT_LIMIT" in names(settings)


def test_all_violations_are_collected_in_one_exception():
    settings = make_settings(
        OPERATING_CAPITAL=0, TARGET_POSITIONS=0, RSI_PERIOD=1, API_REQUEST_INTERVAL_SECONDS=-1,
        STATE_SAVE_CONSECUTIVE_FAILURE_THRESHOLD=0,
    )
    with pytest.raises(ConfigValidationError) as info:
        validate_startup_config(settings)
    collected = {v.name for v in info.value.violations}
    assert {"OPERATING_CAPITAL", "TARGET_POSITIONS", "RSI_PERIOD", "API_REQUEST_INTERVAL_SECONDS",
            "STATE_SAVE_CONSECUTIVE_FAILURE_THRESHOLD"} <= collected
    for name in collected:
        assert name in str(info.value)


class _NoOp:
    def __enter__(self):
        return True

    def __exit__(self, *args):
        return False


def _patch_entrypoint(monkeypatch, notify):
    monkeypatch.setattr(run_trading, "configure_logging", lambda: None)
    monkeypatch.setattr(run_trading, "notify_critical", notify)
    monkeypatch.setattr(run_trading, "config", make_settings(OPERATING_CAPITAL=0, TARGET_POSITIONS=0))
    reached = []
    monkeypatch.setattr(run_trading, "market_workflow_lock", lambda: reached.append("lock") or _NoOp())
    monkeypatch.setattr(run_trading, "create_trading_use_case", lambda token: reached.append("usecase"))
    monkeypatch.setattr(run_trading, "get_api_token", lambda: reached.append("token") or "t")
    return reached


def test_main_stops_logs_and_notifies_on_violation(monkeypatch, caplog):
    sent = []
    reached = _patch_entrypoint(monkeypatch, lambda message: sent.append(message) or True)
    with caplog.at_level(logging.ERROR):
        with pytest.raises(SystemExit) as info:
            run_trading.main(now_provider=lambda: datetime(2026, 10, 5, 10, 0))
    assert info.value.code == 1
    assert reached == []
    assert len(sent) == 1 and "OPERATING_CAPITAL" in sent[0] and "TARGET_POSITIONS" in sent[0]
    assert "OPERATING_CAPITAL" in caplog.text and "TARGET_POSITIONS" in caplog.text


def test_main_stops_even_if_notification_raises(monkeypatch):
    def failing(message):
        raise RuntimeError("slack down")

    reached = _patch_entrypoint(monkeypatch, failing)
    with pytest.raises(SystemExit) as info:
        run_trading.main(now_provider=lambda: datetime(2026, 10, 5, 10, 0))
    assert info.value.code == 1
    assert reached == []


def test_main_notifies_and_stops_on_corrupt_state_file(monkeypatch, tmp_path):
    sent = []
    monkeypatch.setattr(run_trading, "configure_logging", lambda: None)
    monkeypatch.setattr(run_trading, "_validate_config_or_exit", lambda: None)
    monkeypatch.setattr(run_trading, "market_workflow_lock", lambda: _NoOp())
    monkeypatch.setattr(run_trading, "process_notification", lambda *args, **kwargs: nullcontext())
    monkeypatch.setattr(run_trading, "is_trading_session", lambda *args, **kwargs: True)
    monkeypatch.setattr(
        run_trading,
        "FilteringResultRepository",
        lambda *_: SimpleNamespace(
            load_for_date=lambda _: SimpleNamespace(symbols=["AAA"])
        ),
    )
    monkeypatch.setattr(run_trading, "get_api_token", lambda: "token")
    monkeypatch.setattr(run_trading, "notify_critical", sent.append)

    def corrupt_state(*args, **kwargs):
        path = tmp_path / "corrupt.json"
        raise StateFileCorruptError(path, "JSONDecodeError", path)

    monkeypatch.setattr(run_trading, "create_trading_use_case", corrupt_state)

    with pytest.raises(StateFileCorruptError):
        run_trading.main(now_provider=lambda: datetime(2026, 10, 5, 10, 0))

    assert sent == [
        "【取引】起動を中止しました(状態ファイルの破損)\n"
        f"状態ファイルが破損しているか読み込めません: {tmp_path / 'corrupt.json'} "
        "(原因: JSONDecodeError)。"
        f" 破損ファイルのコピー: {tmp_path / 'corrupt.json'}。"
        "原因を確認し、ファイルを修正するか、意図的に初期化する場合はファイルを削除してください。"
    ]
