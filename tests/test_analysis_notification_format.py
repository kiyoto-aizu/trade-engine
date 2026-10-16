from datetime import date

from src.application.analysis_notification import (
    daily_conclusion,
    daily_notification_lines,
    period_conclusion,
    period_notification_lines,
)
from src.infrastructure.notification.slack_notify import format_result_notification


def _band(price_band, **overrides):
    item = {
        "price_band": price_band,
        "selected_count": 0,
        "judged_count": 0,
        "rate_denominator": 0,
        "trend_count": 0,
        "trend_rate": None,
        "reference": False,
        "missing_days": 0,
        "missing_reasons": {},
        "empty_days": 0,
        "evaluation_excluded_count": 0,
        "undecidable_reasons": {},
        "average_move_pct": None,
        "slot_count": 1,
        "buyable_count": 0,
        "buyable_rate": None,
        "estimate": None,
        "activity": {
            "signal_count": 0,
            "skip_count": 0,
            "skip_reason_counts": {},
            "candidate_count": 0,
            "valid_days": 0,
            "missing_days": 0,
            "missing_reasons": {},
        },
    }
    item.update(overrides)
    return item


def _price_band_trend():
    return {
        "bands": {
            "270": _band(
                270, selected_count=12, judged_count=12, rate_denominator=12,
                trend_count=5, trend_rate=5 / 12, reference=True,
                activity={
                    "signal_count": 3, "skip_count": 2,
                    "skip_reason_counts": {"FILTER_TURNOVER_MISSING": 2},
                    "candidate_count": 12, "valid_days": 1,
                    "missing_days": 0, "missing_reasons": {},
                },
            ),
            "450": _band(
                450, missing_days=1, missing_reasons={"FILTER_TIME_LIMIT": 1},
                activity={
                    "signal_count": 0, "skip_count": 0, "skip_reason_counts": {},
                    "candidate_count": 0, "valid_days": 0, "missing_days": 1,
                    "missing_reasons": {"FILTER_TIME_LIMIT": 1},
                },
            ),
            "900": _band(
                900, missing_days=1, missing_reasons={"FILTER_TIME_LIMIT": 1},
                activity={
                    "signal_count": 0, "skip_count": 0, "skip_reason_counts": {},
                    "candidate_count": 0, "valid_days": 0, "missing_days": 1,
                    "missing_reasons": {"FILTER_TIME_LIMIT": 1},
                },
            ),
        }
    }


def _stat(trend, n):
    return {"trend": trend, "n": n}


def _trend(undecidable_selected=0, undecidable_candidate=0):
    return {
        "version": "v1",
        "rows": [],
        "summaries": [],
        "aggregate": {
            "bought": {"bought_count": 0, "stat": _stat(0, 0)},
            "pooled_selected": _stat(6, 10),
            "pooled_candidate": _stat(40, 100),
            "undecidable_selected": undecidable_selected,
            "undecidable_candidate": undecidable_candidate,
            "universe_sources": [],
            "daily": [],
        },
    }


def _report_1007(**overrides):
    report = {
        "date": "2026-10-07",
        "order_count": 0,
        "realized_profit_loss": 0,
        "unrealized_profit_loss": 0,
        "positions": [],
        "market_conditions": {"regime": "NORMAL", "nikkei_change_percent": -0.35},
        "log_errors": {"count": 0, "summaries": []},
        "atr_danger_skips": [],
        "market_regime_danger_skips": [],
        "market_regime_caution_rsi_filters": [],
    }
    report.update(overrides)
    return report


def _daily_message(report, trend, price_band_trend, analysis=None, **kwargs):
    lines = daily_notification_lines(
        report, trend, cache_update_ok=True, analysis=analysis,
        price_band_trend=price_band_trend, **kwargs,
    )
    summary = daily_conclusion(report, cache_update_ok=True, price_band_trend=price_band_trend)
    return format_result_notification("分析運用", "日次分析", summary, lines)


def test_daily_notification_sample_for_2026_10_07():
    message = _daily_message(
        _report_1007(), _trend(), _price_band_trend(),
        analysis="所見: 売買なしの日。欠測帯の原因確認が優先。",
        no_trade_reason={"reason": "買い条件に届かず", "detail": "評価10銘柄で買いシグナルなし"},
    )

    assert message == "\n".join([
        "【業務】分析運用",
        "【機能】日次分析",
        "【概要】",
        "日足更新・答え合わせ・日次レビューが完了しました。今日は売買なし。450円・900円帯は欠測です。",
        "【詳細】",
        "■ A. ペーパートレード実績",
        "・損益: 実現 +0円 / 評価 +0円",
        "・約定 0件 / 保有 0銘柄",
        "・市場状態: NORMAL(日経 -0.35%)",
        "・見送り 0件",
        "・売買0件の理由: 買い条件に届かず",
        "",
        "■ エラー なし",
        "",
        "■ B. 戦略の答え合わせ(判定基準 v1)",
        "・買った銘柄: 判定可能なし",
        "・選定銘柄: 6/10件 (60.0%)",
        "・候補全体: 40/100件 (40.0%)",
        "",
        "価格帯別（選定10銘柄）",
        "・270円 : 5/12件 (41.7%) / シグナル3・見送り2(売買代金データなし×2)",
        "・450円 : 欠測(フィルタ時間切れ1日) / 有効0日",
        "・900円 : 欠測(フィルタ時間切れ1日) / 有効0日",
        "",
        "■ D. 次回確認",
        "・450円帯の欠測: フィルタ時間切れ",
        "・900円帯の欠測: フィルタ時間切れ",
        "",
        "■ LLM日次評価(参考)",
        "【所見】売買なしの日。欠測帯の原因確認が優先。",
        "※ロジック変更・認証修正・投資判断・売買指示は行わない参考意見です。",
        "",
        "※参考値: 標本数10件未満、または対象総数30件未満の価格帯別集計は参考値です。",
    ])


def _timeout_message(symbol):
    return (
        f"❌ [GET通信エラー] URL: http://localhost:18080/kabusapi/board/{symbol}@1 | "
        "理由: HTTPConnectionPool(host='localhost', port=18080): Read timed out. (read timeout=10)"
    )


def test_daily_errors_are_aggregated_without_url_and_keep_total_count():
    report = _report_1007(
        log_errors={
            "count": 8,
            "summaries": [_timeout_message("4265"), _timeout_message("2764")],
        }
    )

    lines = daily_notification_lines(report, None, cache_update_ok=True, analysis=None)
    section = lines[lines.index("■ エラー 8件"):][:3]

    assert section == [
        "■ エラー 8件",
        "・板情報GETタイムアウト(read timeout=10秒)",
        "  対象: 4265, 2764",
    ]
    assert not any("localhost" in line or "http" in line for line in lines)


def test_price_band_source_is_marked_once_with_footnote():
    bands = _price_band_trend()
    for band in ("450", "900"):
        bands["bands"][band].update(
            missing_days=0, missing_reasons={}, selected_count=10, judged_count=10,
            rate_denominator=10, trend_count=0, trend_rate=0.0, reference=True,
            by_source={"yahoo_minute_estimate": {}},
            activity={
                "signal_count": 10, "skip_count": 1,
                "skip_reason_counts": {"FILTER_TURNOVER_MISSING": 1},
                "candidate_count": 10, "valid_days": 1, "missing_days": 0,
                "missing_reasons": {}, "source_days": {"yahoo_minute_estimate": 1},
            },
        )

    lines = daily_notification_lines(
        _report_1007(), _trend(), cache_update_ok=True, analysis=None, price_band_trend=bands
    )
    block = lines[lines.index("価格帯別（選定10銘柄）"):][:5]

    assert block[2].startswith("・450円※: ") and block[3].startswith("・900円※: ")
    assert "シグナル10・見送り1(売買代金データなし×1)" in block[2]
    assert block[4] == "※昼推定 / 有効1日"
    assert "[昼推定]" not in "\n".join(lines)


def test_data_save_section_puts_path_on_next_line():
    lines = daily_notification_lines(
        _report_1007(), None, cache_update_ok=True, analysis=None,
        minute_bar_line="分足 8/10銘柄保存、0本: 1111,2222",
        chart_line="グラフ 全体図1枚・詳細図10枚を保存: data\\reports\\daily_charts\\2026-10-15\\",
    )
    start = lines.index("■ データ保存")

    assert lines[start:start + 4] == [
        "■ データ保存",
        "・分足: 8/10銘柄保存、0本: 1111,2222",
        "・グラフ: 全体図1枚・詳細図10枚",
        "  保存先: data\\reports\\daily_charts\\2026-10-15\\",
    ]


def test_llm_review_gets_headings_one_line_each_and_single_caveat():
    from src.application.analysis_notification import format_daily_review

    text = "\n".join([
        "本日の事実", "- 売買なしです。",
        "所見：", "- 一点目です。", "- 二点目です。ロジック変更は行わない参考意見です。",
        "【次回確認】欠測の原因。",
        "ロジック変更や投資判断は行わず、検証期間中の参考意見です。",
    ])

    assert format_daily_review(text) == [
        "【事実】売買なしです。",
        "【所見】",
        "・一点目です。",
        "・二点目です。",
        "【次回確認】欠測の原因。",
        "※ロジック変更・認証修正・投資判断・売買指示は行わない参考意見です。",
    ]


def test_daily_next_check_is_none_when_nothing_to_check():
    bands = {"bands": {"270": _band(270), "450": _band(450), "900": _band(900)}}
    lines = daily_notification_lines(
        _report_1007(), _trend(), cache_update_ok=True, analysis=None, price_band_trend=bands
    )

    assert lines[lines.index("■ D. 次回確認") + 1:] == ["なし"]
    assert "参考値" not in "\n".join(lines)
    assert any(line.startswith("・270円") and "欠測(" in line for line in lines)


def test_daily_skip_summary_distinguishes_zero_from_unrecorded():
    zero = daily_notification_lines(_report_1007(), None, cache_update_ok=True, analysis=None)
    legacy = _report_1007()
    del legacy["market_regime_danger_skips"]
    unrecorded = daily_notification_lines(legacy, None, cache_update_ok=True, analysis=None)
    with_skips = daily_notification_lines(
        _report_1007(atr_danger_skips=[{}, {}], market_regime_danger_skips=[{}]),
        None, cache_update_ok=True, analysis=None,
    )

    assert "・見送り 0件" in zero
    assert "・見送り 未記録" in unrecorded
    assert "・見送り 3件" in with_skips
    assert "  ATR危険度 2件（当日基準結果なし／照合不能2件）" in with_skips
    assert "  市場危険度 1件（当日基準結果なし／照合不能1件）" in with_skips


def test_daily_skip_summary_shows_finalized_same_day_outcome():
    report = _report_1007(
        market_regime_caution_rsi_filters=[
            {
                "evaluations": {
                    "same_day": {
                        "finalized": True,
                        "outcome": "損失回避の可能性",
                        "hypothetical_pnl_before_cost": -5600,
                    }
                }
            }
        ]
    )

    lines = daily_notification_lines(report, None, cache_update_ok=True, analysis=None)

    assert "・見送り 1件" in lines
    assert (
        "  注意レジームRSI 1件（当日基準: 見送って正解1件・"
        "買っていたら-5,600円（手数料前）"
        "／照合不能0件）"
    ) in lines


def test_daily_conclusion_with_trades_shows_profit_and_count():
    report = _report_1007(order_count=2, realized_profit_loss=1200, unrealized_profit_loss=-300)

    assert daily_conclusion(report, cache_update_ok=True) == (
        "日足更新・答え合わせ・日次レビューが完了しました。"
        "今日は約定2件、損益 実現+1200円・評価-300円。"
    )


def test_daily_next_check_reports_undecidable_only_when_present():
    lines = daily_notification_lines(
        _report_1007(), _trend(undecidable_selected=1, undecidable_candidate=3),
        cache_update_ok=True, analysis=None,
    )

    assert "・判定不能 選定1件/候補3件（原因を確認）" in lines


def _period_summary():
    return {
        "daily": {
            "report_count": 5,
            "total_profit_loss": 300,
            "order_count": 2,
            "operational_summary": {
                "market_assessment_status_counts": {},
                "log_error_count": 0,
                "emergency_stop_days": 0,
            },
            "reports": [],
        },
        "backtest": {"exact_period_run_available": False, "total_pnl": None, "total_trades": None},
        "price_band_trend_check": _price_band_trend(),
    }


def test_period_notification_uses_same_rules_as_daily():
    summary = _period_summary()

    lines = period_notification_lines(summary, _trend(), "週次", None)

    assert period_conclusion(summary, "週次") == (
        "週次分析が完了しました。今週は約定2件、損益+300円。450円・900円帯は欠測です。"
    )
    assert sum(line.startswith("※参考値") for line in lines) == 1
    assert "参考値" not in "".join(line for line in lines if not line.startswith("※参考値"))
    assert lines.index("C. バックテスト（別枠）") < lines.index("D. 次回確認")
    assert lines[lines.index("D. 次回確認") + 1:-1] == [
        "450円帯の欠測: フィルタ時間切れ",
        "900円帯の欠測: フィルタ時間切れ",
    ]
    assert lines[-1].startswith("※参考値:")


def test_period_status_and_skip_summary_are_human_readable():
    summary = _period_summary()
    summary["daily"]["operational_summary"]["market_assessment_status_counts"] = {"available": 5}
    summary["daily"]["reports"] = [
        {
            "date": "2026-10-05",
            "total_profit_loss": 0,
            "skip_counts": {"注意レジームRSI": 1},
            "skip_evaluations": {
                "注意レジームRSI": [
                    {
                        "outcome": "損失回避の可能性",
                        "pnl_before_cost": -5600,
                        "finalized": True,
                    }
                ]
            },
        }
    ]

    lines = period_notification_lines(summary, _trend(), "週次", None)

    assert "市場状態: 5日とも判定あり" in lines
    assert (
        "見送り内訳: 注意レジームRSI 1件（当日基準: 見送って正解1件・"
        "買っていたら-5,600円（手数料前）"
        "／照合不能0件）"
    ) in lines


def test_period_review_prompt_uses_only_same_day_results_and_avoids_repeated_numbers():
    from src.infrastructure.analysis.daily_analyzer import _build_period_review_prompt

    summary = {
        "filter_decision_events": {
            "count": 3,
            "by_event_type": {"MULTI_DAY_ONLY": {"count": 3}},
            "basis": "複数営業日基準",
            "same_day": {"basis": "当日基準", "count": 1},
        },
        "daily": {
            "operational_summary": {
                "market_assessment_status_counts": {"available": 5},
            },
            "reports": [
                {
                    "market_assessment_status": "available",
                    "skip_evaluations": {
                        "注意レジームRSI": [{
                            "evaluations": {
                                "same_day": {"outcome": "当日結果"},
                                "multi_day": {"outcome": "複数営業日結果"},
                            }
                        }]
                    },
                }
            ],
        },
        "backtest": {"exact_period_run_available": False, "runs": [{"total_pnl": 12345}]},
    }

    prompt = _build_period_review_prompt("週次", summary)

    assert "MULTI_DAY_ONLY" not in prompt
    assert '"count": 3' not in prompt
    assert "12345" not in prompt
    assert '"available": 5' not in prompt
    assert '"market_assessment_status"' not in prompt
    assert '"multi_day"' not in prompt
    assert "複数営業日結果" not in prompt
    assert '"outcome": "当日結果"' in prompt
    assert "比較対象なし" in prompt
    assert "通知本文のA・Bに表示済みの数値は再掲せず" in prompt
    assert "割合は%表記" in prompt


def test_period_notification_sanitizes_llm_output_and_localizes_status():
    summary = _period_summary()
    summary["daily"]["operational_summary"]["market_assessment_status_counts"] = {
        "available": 5,
    }
    analysis = "\n".join([
        "所見",
        "- 市場評価は available でした。",
        "- 生データ {'available': 5} を参照しました。",
        "{'available': 5,",
        " 'unavailable': 2",
        "}",
        "- 複数営業日基準では12件です。",
        "- market_assessment_status_counts を確認しました。",
    ])

    for kind in ("週次", "月次"):
        lines = period_notification_lines(summary, _trend(), kind, analysis)
        message = "\n".join(lines)
        llm_section = message.split("LLM評価（参考）:\n", 1)[1]

        assert "市場状態: 5日とも判定あり" in lines
        assert "市場評価は 判定あり でした。" in llm_section
        assert "{'available': 5}" not in message
        assert "複数営業日" not in message
        assert "market_assessment_status_counts" not in message
        assert "available" not in message
        assert lines[-1].startswith("※参考値:")


def test_monthly_next_check_is_none_without_problems():
    summary = _period_summary()
    summary["price_band_trend_check"] = None

    lines = period_notification_lines(summary, _trend(), "月次", None)

    assert period_conclusion(summary, "月次") == "月次分析が完了しました。今月は約定2件、損益+300円。"
    assert lines[lines.index("D. 次回確認") + 1:] == ["なし"]


def test_no_trade_reason_line_only_on_zero_trade_days_and_marks_unrecorded():
    unrecorded = daily_notification_lines(_report_1007(), None, cache_update_ok=True, analysis=None)
    traded = daily_notification_lines(
        _report_1007(order_count=1), None, cache_update_ok=True, analysis=None,
        no_trade_reason={"reason": "その他"},
    )
    missing_report = daily_notification_lines(
        None, None, cache_update_ok=True, analysis=None,
        no_trade_reason={"reason": "候補なし", "detail": "当日のフィルタ結果なし"},
    )

    assert "・売買0件の理由: 未記録" in unrecorded
    assert not any(line.startswith("・売買0件の理由") for line in traded)
    assert "・売買0件の理由: 候補なし" in missing_report


def test_resolve_no_trade_reason_uses_report_or_filtering_result(monkeypatch):
    from src.entrypoints import run_daily_analysis

    paths = object()
    recorded = {"reason": "枠・資金不足", "detail": "POSITION_LIMIT_REACHED"}
    assert run_daily_analysis.resolve_no_trade_reason(
        _report_1007(no_trade_reason=recorded), paths, date(2026, 10, 7)
    ) == recorded
    assert run_daily_analysis.resolve_no_trade_reason(
        _report_1007(order_count=1), paths, date(2026, 10, 7)
    ) is None

    monkeypatch.setattr(run_daily_analysis, "load_selected_symbols", lambda *_: [])
    assert run_daily_analysis.resolve_no_trade_reason(None, paths, date(2026, 10, 7))["reason"] == "候補なし"
    monkeypatch.setattr(run_daily_analysis, "load_selected_symbols", lambda *_: ["7203"])
    assert run_daily_analysis.resolve_no_trade_reason(None, paths, date(2026, 10, 7))["reason"] == "その他"
