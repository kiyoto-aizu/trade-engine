from src.domain.no_trade_reason import (
    CAPACITY_OR_FUNDS,
    CONDITION_NOT_MET,
    MARKET_ASSESSMENT_UNAVAILABLE,
    MARKET_STATE_SKIP,
    NO_CANDIDATE,
    OTHER,
    TRADING_HALTED,
    classify_no_trade_reason,
    describe_no_trade_reason,
    summarize_market_failure,
)


def _classify(**overrides):
    values = {
        "order_count": 0,
        "kill_switch_triggered": False,
        "emergency_stop_triggered": False,
        "candidate_count": 10,
        "evaluated_count": 10,
        "buy_signal_count": 0,
        "journal_reason_codes": [],
        "market_regime_skip_count": 0,
        "atr_danger_skip_count": 0,
    }
    values.update(overrides)
    return classify_no_trade_reason(**values)


def test_no_reason_when_orders_were_filled():
    assert _classify(order_count=1) is None


def test_each_reason_is_selected_from_existing_records():
    assert _classify()["reason"] == CONDITION_NOT_MET
    assert _classify(candidate_count=0)["reason"] == NO_CANDIDATE
    assert _classify(kill_switch_triggered=True)["reason"] == TRADING_HALTED
    assert _classify(emergency_stop_triggered=True)["detail"] == "手動緊急停止"
    assert _classify(journal_reason_codes=["NEW_BUY_HALTED_STATE_SAVE_FAILURE"])["reason"] == TRADING_HALTED
    assert _classify(market_regime_skip_count=2, buy_signal_count=2)["reason"] == MARKET_STATE_SKIP
    assert _classify(journal_reason_codes=["MARKET_REGIME_DANGER_SKIP"])["reason"] == MARKET_STATE_SKIP
    assert _classify(journal_reason_codes=["POSITION_LIMIT_REACHED"])["reason"] == CAPACITY_OR_FUNDS
    assert _classify(journal_reason_codes=["WALLET_UNKNOWN"])["reason"] == CAPACITY_OR_FUNDS
    assert _classify(atr_danger_skip_count=1, buy_signal_count=1)["reason"] == OTHER


def test_priority_halt_then_candidates_then_market_then_capacity():
    codes = ["POSITION_LIMIT_REACHED", "MARKET_REGIME_DANGER_SKIP"]
    assert _classify(journal_reason_codes=codes)["reason"] == MARKET_STATE_SKIP
    assert _classify(journal_reason_codes=codes, kill_switch_triggered=True)["reason"] == TRADING_HALTED
    assert _classify(journal_reason_codes=codes, candidate_count=0)["reason"] == NO_CANDIDATE


def test_data_unavailable_does_not_hide_condition_not_met_but_is_other_without_evaluation():
    assert _classify(journal_reason_codes=["BOARD_UNAVAILABLE"])["reason"] == CONDITION_NOT_MET
    unevaluated = _classify(evaluated_count=0, journal_reason_codes=["BOARD_UNAVAILABLE"])
    assert unevaluated == {"reason": OTHER, "detail": "BOARD_UNAVAILABLE"}
    assert _classify(buy_signal_count=1)["reason"] == OTHER


def test_describe_marks_missing_record_as_unrecorded():
    assert describe_no_trade_reason(None) == "未記録"
    assert describe_no_trade_reason({"reason": CONDITION_NOT_MET, "detail": "x"}) == CONDITION_NOT_MET


def test_describe_appends_japanese_detail_and_hides_codes():
    assert describe_no_trade_reason(
        {"reason": CONDITION_NOT_MET, "detail": "評価10銘柄で買いシグナルなし"}
    ) == CONDITION_NOT_MET
    assert describe_no_trade_reason(
        {"reason": CONDITION_NOT_MET, "detail": "売りシグナルだが未保有あり"}
    ) == "買い条件に届かず(売りシグナルだが未保有あり)"
    assert describe_no_trade_reason(
        {"reason": MARKET_ASSESSMENT_UNAVAILABLE, "detail": "日経225日足の更新不備"}
    ) == "市場判定不可(日経225日足の更新不備)"
    # 保存済みの過去分(コード名のdetail)は、分類はそのまま・表示だけ日本語化する
    assert describe_no_trade_reason(
        {"reason": "その他", "detail": "SELL_SIGNAL_WITHOUT_HOLDINGS"}
    ) == "その他(売りシグナルだが未保有あり)"
    assert describe_no_trade_reason(
        {"reason": "その他", "detail": "NEW_UNKNOWN_CODE"}
    ) == "その他"
    assert describe_no_trade_reason({"reason": "SOME_CODE", "detail": "x"}) == "その他"


def test_sell_signal_without_holdings_is_info_and_keeps_condition_not_met():
    result = _classify(journal_reason_codes=["SELL_SIGNAL_WITHOUT_HOLDINGS"] * 3)

    assert result == {"reason": CONDITION_NOT_MET, "detail": "売りシグナルだが未保有あり"}


def test_unknown_code_is_still_other_and_not_swallowed_by_info_code():
    result = _classify(journal_reason_codes=["SELL_SIGNAL_WITHOUT_HOLDINGS", "NEW_UNKNOWN_CODE"])

    assert result == {"reason": OTHER, "detail": "NEW_UNKNOWN_CODE"}


def test_market_assessment_unavailable_with_market_skip_is_dedicated_reason():
    failure = "日経225日足が前営業日まで更新されていません(期待日=2026-10-14, 取得最新日=2026-10-13, 試行=3回)"

    for kwargs in (
        {"market_regime_skip_count": 1, "buy_signal_count": 1},
        {"journal_reason_codes": ["MARKET_REGIME_DANGER_SKIP"], "buy_signal_count": 1},
    ):
        result = _classify(
            market_assessment_status="unavailable", market_failure_reason=failure, **kwargs
        )
        assert result == {"reason": MARKET_ASSESSMENT_UNAVAILABLE, "detail": "日経225日足の更新不備"}

    available = _classify(
        market_assessment_status="available", market_regime_skip_count=1, buy_signal_count=1
    )
    assert available["reason"] == MARKET_STATE_SKIP


def test_market_assessment_unavailable_without_skip_is_not_blamed():
    result = _classify(
        market_assessment_status="unavailable",
        market_failure_reason="日経225日足が前営業日まで更新されていません",
        journal_reason_codes=["SELL_SIGNAL_WITHOUT_HOLDINGS"],
    )

    assert result["reason"] == CONDITION_NOT_MET


def test_market_failure_summary_hides_raw_exception_text():
    assert summarize_market_failure("データ取得または計算エラー: HTTPConnectionPool(host='x')") == (
        "市場判定データの取得・計算エラー"
    )
    assert summarize_market_failure("よく分からない") == "市場判定データの取得不可"