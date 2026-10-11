import json
from datetime import date, datetime
from pathlib import Path

import pytest

from scripts.analysis.diagnose_unsold_position import (
    CODE_A, CODE_B, CODE_C, CODE_UNKNOWN, ClassificationInput, LogRecord, Obs, Paths,
    classify, collect_log_facts, diagnose, parse_judgment, render_markdown, simulate_exit_path, write_reports,
)
from src.domain.volatility import VolatilityLevel

SYMBOL = "8944"
DAY = date(2026, 10, 6)
BUY_PRICE = 265.0
ATR = 6.5
NORMAL_STOP_LINE = BUY_PRICE - ATR * 1.5  # 255.25


def _input(**overrides) -> ClassificationInput:
    values = dict(
        buy_found=True, state_available=True, held=True, average_cost=265.3, price_points=50, atr=ATR,
        max_range_atr=1.5,
    )
    values.update(overrides)
    return ClassificationInput(**values)


# --- 分類ロジック ---

def test_classify_a_when_range_is_below_stagnant_threshold():
    result = classify(_input(max_range_atr=0.1))
    assert result.code == CODE_A


def test_classify_b_when_price_moved_but_no_condition_met():
    result = classify(_input(max_range_atr=1.5))
    assert result.code == CODE_B


def test_classify_c_when_condition_met_but_no_sell_order():
    result = classify(_input(max_range_atr=0.1, triggered_without_sell=["ATR損切り"]))
    assert result.code == CODE_C
    assert "売り注文が出ていません" in result.reasons[0]


@pytest.mark.parametrize("overrides", [
    {"failure_signs": ["HTTP 401(認証失敗) が3回"]},
    {"average_cost": None},
    {"average_cost": 0.0},
    {"high_anomaly": "高値更新停止の疑い"},
])
def test_classify_c_takes_priority_over_a_and_b(overrides):
    assert classify(_input(max_range_atr=0.1, **overrides)).code == CODE_C
    assert classify(_input(max_range_atr=2.0, **overrides)).code == CODE_C


def test_missing_average_cost_is_ignored_when_not_held():
    assert classify(_input(held=False, average_cost=None, max_range_atr=2.0)).code == CODE_B


@pytest.mark.parametrize(("overrides", "missing_text"), [
    ({"price_points": 0, "max_range_atr": None}, "値動き"),
    ({"atr": None, "max_range_atr": None}, "ATR"),
    ({"buy_found": False}, "買い注文"),
])
def test_classify_unknown_lists_missing_data(overrides, missing_text):
    result = classify(_input(**overrides))
    assert result.code == CODE_UNKNOWN
    assert any(missing_text in item for item in result.missing)


# --- ログ解析・売却条件の再現 ---

def test_parse_judgment_reads_prices_and_holding_high():
    message = ("売買判定: 銘柄=8944 | 現在値=271.0 | エントリー基準=256.3 | 決済基準=251.3 | RSI=63.6 | "
               "エントリーRSI基準=60.0 | 決済RSI基準=45.0 | 保有中最高値=271.0 | 利確ライン=257.9 | 判定=BUY")
    symbol, obs = parse_judgment(LogRecord(datetime(2026, 10, 6, 9, 37, 25), "INFO", "x", message))
    assert symbol == "8944"
    assert (obs.price, obs.lower_band, obs.logged_high, obs.logged_line, obs.decision) == (271.0, 251.3, 271.0, 257.9, "BUY")
    assert parse_judgment(LogRecord(datetime(2026, 10, 6, 9, 37), "INFO", "x", "ATR数量調整: 銘柄=8944")) is None


def test_collect_log_facts_counts_current_and_legacy_missing_holding_messages():
    records = [
        LogRecord(
            datetime(2026, 10, 6, 9, 37),
            "INFO",
            "src.application.trading_usecase",
            "売買判定: 銘柄=8944 | 現在値=265.0 | エントリー基準=256.0 | 決済基準=251.0 | "
            "RSI=40.0 | エントリーRSI基準=60.0 | 決済RSI基準=45.0 | "
            "保有中最高値=- | 利確ライン=- | 判定=SELL",
        ),
        LogRecord(
            datetime(2026, 10, 6, 9, 37, 1),
            "INFO",
            "src.application.trading_usecase",
            "売り条件該当・未保有: 銘柄=8944 | 状態=開始",
        ),
        LogRecord(
            datetime(2026, 10, 6, 9, 37, 2),
            "WARNING",
            "src.domain.rules",
            "保有株が確認できないため、売り注文を見送ります。",
        ),
    ]

    facts = collect_log_facts(records, SYMBOL, None, None)

    assert len(facts["guards"]["no_holding"][("8944", "SELL")]) == 2


def _path(prices):
    return [Obs(datetime(2026, 10, 6, 9, 36 + index), price, "bar") for index, price in enumerate(prices)]


def _simulate(prices):
    return simulate_exit_path(
        SYMBOL, _path(prices), entry_price=265.3, initial_high=BUY_PRICE, atr=ATR,
        level=VolatilityLevel.NORMAL, fallback_context=(63.0, 251.0, 256.0),
    )


def test_simulation_detects_atr_stop_without_profit():
    result = _simulate([265.0, 260.0, NORMAL_STOP_LINE - 0.1])
    assert result["first_trigger"]["kind"] == "ATR損切り"


def test_simulation_switches_to_profit_lock_after_gain_and_trails_from_high():
    # 高値276(含み益>=ATR×0.5)で利確倍率(NORMAL 2.5)に切り替わり、276-6.5×2.5=259.75で利確
    result = _simulate([265.0, 276.0, 259.0])
    assert result["first_trigger"]["kind"] == "ATRトレーリング利確"
    assert result["first_trigger"]["line"] == pytest.approx(276.0 - ATR * 2.5)
    assert _simulate([265.0, 276.0, 262.0])["first_trigger"] is None


# --- 結合(一時ファイルのみ使用) ---

def _build_paths(tmp_path: Path, *, prices, held=True, average_costs=None, extra_log_lines=(), sell=None) -> Paths:
    trading = tmp_path / "trading"
    trading.mkdir()
    orders = [{
        "symbol": SYMBOL, "side": "2", "price": BUY_PRICE, "qty": 100, "timestamp": "2026-10-06T09:35:17.259896",
        "result_code": 0, "order_id": "paper-1", "atr": ATR, "atr_level": "NORMAL", "market_regime": "NORMAL",
        "rsi": 63.0, "basis_lower_band": 251.0, "basis_upper_band": 256.0, "decision_reason": "上側バンド突破・RSI条件成立",
    }]
    if sell:
        orders.append({"symbol": SYMBOL, "side": "1", "price": sell[1], "qty": 100, "timestamp": sell[0],
                       "result_code": 0, "order_id": "paper-2", "decision_reason": "持ち越し防止"})
    (trading / "order_history.json").write_text(json.dumps(orders), encoding="utf-8")
    state = {"cash": 70000.0, "holdings": {SYMBOL: 100} if held else {},
             "average_costs": ({SYMBOL: 265.3} if average_costs is None else average_costs) if held else {},
             "next_order_id": 2, "realized_pnl": 0.0, "realized_pnl_date": "2026-10-06"}
    (trading / "paper_account_state.json").write_text(json.dumps(state), encoding="utf-8")
    logs = tmp_path / "logs"
    logs.mkdir()
    lines = []
    for index, price in enumerate(prices):
        at = f"2026-10-06 09:{36 + index:02d}:20,100"
        lines.append(
            f"{at} INFO src.application.trading_usecase: 売買判定: 銘柄={SYMBOL} | 現在値={price} | エントリー基準=256.0 | "
            f"決済基準=251.0 | RSI=63.0 | エントリーRSI基準=55.0 | 決済RSI基準=45.0 | 保有中最高値=- | 利確ライン=- | 判定=なし")
    lines.extend(extra_log_lines)
    (logs / "trade_project.log").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return Paths(
        order_history=trading / "order_history.json", state=trading / "paper_account_state.json", logs=logs,
        minute_bars=tmp_path / "no_minute_bars", daily_cache=tmp_path / "no_daily_cache",
        kill_switch_baseline=tmp_path / "no_baseline.json", decision_db=tmp_path / "no_db.sqlite3",
        emergency_stop=tmp_path / "no_emergency_stop",
    )


def _run(paths: Paths, minutes: int):
    return diagnose(SYMBOL, DAY, paths, now=datetime(2026, 10, 6, 9, 36 + minutes, 40))


def test_diagnose_a_when_price_is_flat(tmp_path):
    prices = [265.0, 265.5, 265.0, 265.5, 265.0, 265.5]
    result = _run(_build_paths(tmp_path, prices=prices), len(prices))
    assert result["conclusion"]["code"] == CODE_A


def test_diagnose_b_when_price_moves_but_stays_above_lines(tmp_path):
    prices = [265.0, 270.0, 272.0, 268.0, 264.0, 266.0]
    result = _run(_build_paths(tmp_path, prices=prices), len(prices))
    assert result["conclusion"]["code"] == CODE_B
    assert all(item["met"] is not True for item in result["conditions"])


def test_diagnose_c_when_stop_line_touched_but_no_sell_order(tmp_path):
    prices = [265.0, 262.0, 255.0, 256.0, 257.0, 258.0]
    result = _run(_build_paths(tmp_path, prices=prices), len(prices))
    assert result["conclusion"]["code"] == CODE_C
    assert "ATR損切り" in result["conclusion"]["reasons"][0]


def test_diagnose_c_when_log_has_order_rejection(tmp_path):
    prices = [265.0, 266.0, 265.0, 266.0]
    rejected = "2026-10-06 09:38:30,100 ERROR src.application.trading_usecase: ORDER_REJECTED_NONE: 銘柄=8944 | 方向=SELL | 数量=100 | 応答=None"
    result = _run(_build_paths(tmp_path, prices=prices, extra_log_lines=[rejected]), len(prices))
    assert result["conclusion"]["code"] == CODE_C
    assert any("ORDER_REJECTED" in reason for reason in result["conclusion"]["reasons"])


def test_diagnose_c_when_average_cost_is_missing(tmp_path):
    prices = [265.0, 270.0, 268.0, 266.0]
    result = _run(_build_paths(tmp_path, prices=prices, average_costs={}), len(prices))
    assert result["conclusion"]["code"] == CODE_C
    assert "平均取得価格" in result["conclusion"]["reasons"][0]


def test_diagnose_unknown_without_logs_and_bars(tmp_path):
    paths = _build_paths(tmp_path, prices=[])
    result = _run(paths, 5)
    assert result["conclusion"]["code"] == CODE_UNKNOWN
    assert any("値動き" in item for item in result["conclusion"]["missing"])


def test_diagnose_sold_by_eod_is_not_a_failure(tmp_path):
    prices = [265.0, 270.0, 268.0]
    paths = _build_paths(tmp_path, prices=prices, held=False, sell=("2026-10-06T15:20:22.000000", 268.0))
    result = diagnose(SYMBOL, DAY, paths, now=datetime(2026, 10, 6, 18, 0))
    assert result["conclusion"]["code"] == CODE_B
    assert "売却済み" in result["status_line"]


def test_reports_are_written_only_to_output_dir(tmp_path):
    prices = [265.0, 270.0, 268.0, 266.0]
    paths = _build_paths(tmp_path, prices=prices)
    result = _run(paths, len(prices))
    markdown_path, json_path = write_reports(result, tmp_path / "out")

    assert markdown_path.name == "diagnose_unsold_8944_2026-10-06.md"
    assert json.loads(json_path.read_text(encoding="utf-8"))["conclusion"]["code"] == CODE_B
    text = render_markdown(result)
    assert text.index("## 結論") < text.index("## 売却条件の実装") < text.index("## 売却条件の照合")
    assert "仮置き" in text
    assert sorted(path.name for path in (tmp_path / "out").iterdir()) == sorted([markdown_path.name, json_path.name])
