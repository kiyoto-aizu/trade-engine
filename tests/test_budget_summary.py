import json
import logging
from types import SimpleNamespace

from src.config import config as real_config
from src.config.budget_summary import build_budget_summary, log_budget_summary


def make_cfg(**overrides):
    values = dict(
        OPERATING_CAPITAL=100_000,
        MAX_ORDER_AMOUNT_PER_TRADE=30_000,
        TARGET_POSITIONS=3,
        SCREENING_PRICE_MARGIN=0.9,
        API_SOFT_LIMIT=1_000_000,
        ORDER_UNIT=100,
        SCREENING_ALTERNATE_PRICE_CAPS=(450.0, 900.0),
        IS_DEMO=False,
        PAPER_ACCOUNT_STATE_FILE="paper.json",
        KILL_SWITCH_BASELINE_FILE="baseline.json",
    )
    values.update(overrides)
    return SimpleNamespace(**values)


def test_normal_case_lines_and_no_warnings(tmp_path):
    paper = tmp_path / "paper.json"
    paper.write_text(json.dumps({"cash": 101_277.4}), encoding="utf-8")
    base = tmp_path / "base.json"
    base.write_text(json.dumps({"date": "2026-10-19", "capital": 101_277.4}), encoding="utf-8")
    info, warnings = build_budget_summary(
        make_cfg(), effective_api_limit=2_000_000, paper_state_path=paper, baseline_path=base
    )
    assert warnings == []
    assert "資金=100,000" in info[0] and "API上限(実効値)=2,000,000" in info[0]
    assert "1枠予算=30,000" in info[1] and "主帯の価格上限=270円" in info[1]
    assert "270円→100株必要額27,000(1枠予算の0.90倍)" in info[2]
    assert "450円→100株必要額45,000(1枠予算の1.50倍)" in info[2]
    assert "900円→100株必要額90,000(1枠予算の3.00倍)" in info[2]
    assert "ペーパー現金=101,277" in info[3] and "保存日=2026-10-19" in info[3]


def test_effective_api_limit_unknown_uses_setting_with_note():
    info, _ = build_budget_summary(make_cfg())
    assert "API上限(実効値)=未取得" in info[0]
    assert "API取得前" in info[1]


def test_api_limit_lowers_budget_and_warns_when_below_order_cap():
    info, warnings = build_budget_summary(make_cfg(), effective_api_limit=20_000)
    assert "1枠予算=20,000" in info[1]
    assert any("注文上限" in w and "API上限" in w for w in warnings)
    assert any("100株必要額" in w for w in warnings)


def test_state_deviation_boundary(tmp_path):
    paper = tmp_path / "p.json"
    base = tmp_path / "b.json"
    paper.write_text(json.dumps({"cash": 120_000}), encoding="utf-8")
    base.write_text(json.dumps({"date": "2026-10-19", "capital": 120_001}), encoding="utf-8")
    _, warnings = build_budget_summary(make_cfg(), paper_state_path=paper, baseline_path=base)
    assert not any("ペーパー現金" in w for w in warnings)
    assert any("当日開始資産" in w for w in warnings)
    paper.write_text(json.dumps({"cash": 79_000}), encoding="utf-8")
    _, warnings = build_budget_summary(make_cfg(), paper_state_path=paper, baseline_path=base)
    assert any("ペーパー現金" in w for w in warnings)


def test_main_cap_vs_alternate_boundary():
    cfg = make_cfg(SCREENING_ALTERNATE_PRICE_CAPS=(270.0, 900.0))
    _, warnings = build_budget_summary(cfg)
    assert any("代替帯の最小値" in w for w in warnings)
    cfg = make_cfg(SCREENING_ALTERNATE_PRICE_CAPS=(271.0, 900.0))
    _, warnings = build_budget_summary(cfg)
    assert not any("代替帯の最小値" in w for w in warnings)


def test_margin_boundaries():
    for margin, warned in ((0.5, False), (1.0, False), (0.49, True), (1.01, True)):
        _, warnings = build_budget_summary(make_cfg(SCREENING_PRICE_MARGIN=margin))
        assert any("SCREENING_PRICE_MARGIN" in w for w in warnings) is warned


def test_budget_below_main_required_boundary():
    # 資金÷枠数=30,000 → 価格上限270円(27,000)で通常は不足しない
    _, warnings = build_budget_summary(make_cfg(SCREENING_PRICE_MARGIN=1.0))
    assert not any("100株必要額" in w for w in warnings)
    _, warnings = build_budget_summary(make_cfg(SCREENING_PRICE_MARGIN=1.0), effective_api_limit=29_999)
    assert any("100株必要額" in w for w in warnings)


def test_alternate_bands_not_warned_when_unaffordable():
    _, warnings = build_budget_summary(make_cfg())
    assert warnings == []


def test_missing_and_corrupt_state_files_do_not_raise(tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text("{broken", encoding="utf-8")
    listy = tmp_path / "list.json"
    listy.write_text("[1]", encoding="utf-8")
    info, warnings = build_budget_summary(
        make_cfg(), paper_state_path=tmp_path / "none.json", baseline_path=bad
    )
    assert "ペーパー現金=なし(ファイルなし)" in info[3]
    assert "当日開始資産=なし(読込失敗" in info[3]
    assert warnings == []
    info, _ = build_budget_summary(make_cfg(), paper_state_path=listy)
    assert "形式不正" in info[3]


def test_log_function_swallows_exceptions(caplog):
    broken = SimpleNamespace()
    with caplog.at_level(logging.WARNING):
        log_budget_summary(logging.getLogger("t"), cfg=broken)
    assert "予算サマリーの作成に失敗" in caplog.text


def test_log_function_fetcher_failure_is_not_fatal(caplog):
    def boom(token):
        raise RuntimeError("x")

    with caplog.at_level(logging.INFO):
        log_budget_summary(logging.getLogger("t"), cfg=make_cfg(), token="tk", api_limit_fetcher=boom)
    assert "実効値を取得できませんでした" in caplog.text
    assert "予算設定:" in caplog.text


def test_demo_uses_setting_as_effective(caplog):
    with caplog.at_level(logging.INFO):
        log_budget_summary(logging.getLogger("t"), cfg=make_cfg(IS_DEMO=True))
    assert "API上限(実効値)=1,000,000" in caplog.text


def test_main_cap_matches_real_config_formula():
    info, _ = build_budget_summary(real_config)
    assert f"主帯の価格上限={real_config.get_screening_price_cap():g}円" in info[1]
