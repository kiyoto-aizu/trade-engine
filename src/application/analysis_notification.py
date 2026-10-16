"""日次・週次・月次の通知で共有する実績・答え合わせの表示。"""
from __future__ import annotations

from collections import Counter
from datetime import date
import re

from src.application.error_summary import error_section_lines
from src.application.trend_check_report import aggregate
from src.application.price_band_trend_check import (
    price_band_has_reference,
    price_band_missing_bands,
    price_band_activity_has_reference,
    price_band_activity_lines,
    price_band_block_lines,
    price_band_monthly_lines,
    price_band_rate_line,
)
from src.domain.no_trade_reason import describe_no_trade_reason
from src.domain.trend_check import JUDGED_LABELS, LABEL_TREND
from src.infrastructure.analysis.period_review import sanitize_period_review
from src.infrastructure.persistence.trend_check_repository import TrendCheckRepository

WEEKDAYS_JA = ("月", "火", "水", "木", "金", "土", "日")
REFERENCE_NOTE = "※参考値: 標本数10件未満、または対象総数30件未満の価格帯別集計は参考値です。"
SKIP_FIELDS = {
    "注意レジームRSI": "market_regime_caution_rsi_filters",
    "ATR危険度": "atr_danger_skips",
    "市場危険度": "market_regime_danger_skips",
}
STATUS_LABELS = {
    "available": "判定あり",
    "unavailable": "判定不可",
    "not_evaluated": "未判定",
    "not_recorded": "未記録",
}
SKIP_OUTCOME_LABELS = (
    ("損失回避", "損失回避の可能性"),
    ("取り逃し", "利益取り逃しの可能性"),
    ("値動きなし", "値動きなし"),
)


def _percent(trend: int, judged: int) -> str:
    if not judged:
        return "判定可能なし"
    return f"{trend}/{judged}件 ({trend / judged:.1%})"


def _is_reference(judged: int) -> bool:
    return 0 < judged < 10


def load_trend_check(
    repository: TrendCheckRepository,
    start: date,
    end: date,
    *,
    with_progression: bool = False,
) -> dict | None:
    versions = repository.versions_with_rows(start.isoformat(), end.isoformat())
    if not versions:
        return None
    latest = repository.latest_version()
    version = latest["version"] if latest and latest["version"] in versions else versions[-1]
    summaries = repository.load_summaries(version, start.isoformat(), end.isoformat())
    rows = repository.load_rows(version, start.isoformat(), end.isoformat())
    if not summaries:
        return None
    return {
        "version": version,
        "aggregate": aggregate(rows, summaries, with_progression=with_progression),
        "summaries": summaries,
        "rows": rows,
    }


def trend_check_heading(result: dict | None, label: str = "B. 戦略の答え合わせ") -> str:
    return f"■ {label}(判定基準 {result['version']})" if result else f"■ {label}"


def trend_check_lines(result: dict | None, *, include_progression: bool = False) -> list[str]:
    """答え合わせの本文(1項目1行)。見出しはtrend_check_headingで付ける。"""
    if result is None:
        return ["・答え合わせ: なし（期間内の日次判定結果なし）"]

    agg = result["aggregate"]
    bought = agg["bought"]
    bought_undecidable = sum(
        1 for row in result["rows"]
        if row["in_selected"] and row["bought"] and row["label"] not in JUDGED_LABELS
    )

    def undecidable(count: int) -> str:
        return f" (判定不能 {count}件)" if count else ""

    bought_count = f"（購入{bought['bought_count']}件）" if bought["bought_count"] else ""
    lines = [
        f"・買った銘柄: {_percent(bought['stat']['trend'], bought['stat']['n'])}"
        f"{bought_count}{undecidable(bought_undecidable)}",
        "・選定銘柄: "
        f"{_percent(agg['pooled_selected']['trend'], agg['pooled_selected']['n'])}"
        f"{undecidable(agg['undecidable_selected'])}",
        "・候補全体: "
        f"{_percent(agg['pooled_candidate']['trend'], agg['pooled_candidate']['n'])}"
        f"{undecidable(agg['undecidable_candidate'])}",
    ]
    sources = agg["universe_sources"]
    if "daily_cache_all" in sources:
        lines.append("・注記: 候補全体は日足キャッシュ全銘柄による代用を含みます。")
    if "screening_previous_day" in sources:
        lines.append("・注記: 候補全体は前営業日のスクリーニング結果による代用を含みます。")

    if include_progression:
        lines.append("週内推移")
        for item in agg["daily"]:
            lines.append(
                f"・{item['date']}: 選定 {_percent(item['selected_trend'], item['selected_judged'])} / "
                f"候補 {_percent(item.get('candidate_trend', 0), _summary_candidate_judged(result, item['date']))}"
            )
    return lines


def trend_check_has_reference(result: dict | None, *, include_progression: bool = False) -> bool:
    """trend_check_linesで表示した割合に、件数不足(10件未満)のものがあるか。"""
    if result is None:
        return False
    agg = result["aggregate"]
    judged = [
        agg["bought"]["stat"]["n"],
        agg["pooled_selected"]["n"],
        agg["pooled_candidate"]["n"],
    ]
    if include_progression:
        for item in agg["daily"]:
            judged.append(item["selected_judged"])
            judged.append(_summary_candidate_judged(result, item["date"]))
    return any(_is_reference(int(value)) for value in judged)


def _summary_candidate_judged(result: dict, day: str) -> int:
    summary = next((item for item in result["summaries"] if item["trade_date"] == day), None)
    return int(summary["candidate_judged"]) if summary else 0


def _skip_counts(report: dict) -> dict[str, int]:
    return {
        label: len(report.get(field) or [])
        for label, field in SKIP_FIELDS.items()
        if report.get(field)
    }


def _skip_events(report: dict) -> dict[str, list[dict]]:
    events_by_label = {}
    for label, field in SKIP_FIELDS.items():
        events_by_label[label] = [
            {
                "outcome": ((item.get("evaluations") or {}).get("same_day") or {}).get("outcome"),
                "pnl_before_cost": ((item.get("evaluations") or {}).get("same_day") or {}).get(
                    "hypothetical_pnl_before_cost"
                ),
                "finalized": bool(((item.get("evaluations") or {}).get("same_day") or {}).get("finalized")),
            }
            for item in (report.get(field) or [])
        ]
    return events_by_label


def _format_skip_group(label: str, count: int, events: list[dict]) -> str:
    finalized = [
        item for item in events
        if item.get("finalized") and item.get("pnl_before_cost") is not None
    ]
    pnl_total = sum(float(item["pnl_before_cost"]) for item in finalized)
    outcome_counts = {
        text: sum(item.get("outcome") == outcome for item in finalized)
        for text, outcome in SKIP_OUTCOME_LABELS
    }
    details = "・".join(
        f"{text}{number}件" for text, number in outcome_counts.items() if number
    )
    unverified = max(0, count - len(finalized))
    if finalized:
        if len(finalized) == outcome_counts["損失回避"] and pnl_total < 0:
            result = f"当日基準: 見送って正解{len(finalized)}件"
        else:
            result = "当日基準: " + (details or f"損益集計{len(finalized)}件")
        result += f"・買っていたら{pnl_total:+,.0f}円（手数料前）"
    else:
        result = "当日基準結果なし"
    return f"{label} {count}件（{result}／照合不能{unverified}件）"


def _skip_lines(report: dict) -> list[str]:
    """0件と未記録(古いレポートで項目自体がない)を区別して表示する。"""
    if any(field not in report for field in SKIP_FIELDS.values()):
        return ["・見送り 未記録"]
    skips = _skip_counts(report)
    if not skips:
        return ["・見送り 0件"]
    event_groups = _skip_events(report)
    lines = [f"・見送り {sum(skips.values())}件"]
    lines.extend(
        "  " + _format_skip_group(label, count, event_groups[label])
        for label, field in SKIP_FIELDS.items()
        if (count := len(report.get(field) or []))
    )
    return lines


def _market_status_text(status_counts: dict, report_count: int) -> str:
    if not status_counts:
        return "市場状態: 記録なし"
    parts = [
        f"{STATUS_LABELS.get(status, status)} {count}日"
        for status, count in sorted(status_counts.items())
    ]
    if (
        len(status_counts) == 1
        and "available" in status_counts
        and status_counts["available"] == report_count
        and report_count > 0
    ):
        return f"市場状態: {report_count}日とも判定あり"
    return "市場状態: " + "・".join(parts)


def _nikkei_text(market: dict) -> str:
    change = market.get("nikkei_change_percent")
    return f"日経 {change:+.2f}%" if isinstance(change, (int, float)) else "日経 不明"


def daily_paper_lines(report: dict | None) -> list[str]:
    if report is None:
        return ["・日次実績: 日次レポートなし"]
    market = report.get("market_conditions") or {}
    positions = report.get("positions") or []
    regime = market.get("regime") or market.get("assessment_status") or "不明"
    return [
        f"・損益: 実現 {float(report.get('realized_profit_loss') or 0):+.0f}円 / "
        f"評価 {float(report.get('unrealized_profit_loss') or 0):+.0f}円",
        f"・約定 {int(report.get('order_count') or 0)}件 / 保有 {len(positions)}銘柄",
        f"・市場状態: {regime}({_nikkei_text(market)})",
        *_skip_lines(report),
    ]


def daily_error_lines(report: dict | None) -> list[str]:
    """日次レポートのエラーを「■ エラー N件」へ集約する。レポートなしなら何も出さない。"""
    if report is None:
        return []
    errors = report.get("log_errors") or {}
    messages = errors.get("unique_messages") or errors.get("summaries") or []
    return error_section_lines([str(item) for item in messages], int(errors.get("count") or 0))


_MINUTE_BAR_PREFIX = "分足 "
_CHART_SAVED = re.compile(r"^グラフ (?P<what>.+?)を保存: (?P<path>.+)$")
_CHART_FAILED = re.compile(r"^グラフ生成失敗（保存先: (?P<path>.+?)。(?P<note>[^。]+)）$")


def data_save_lines(minute_bar_line: str | None, chart_line: str | None) -> list[str]:
    """分足とグラフの保存結果を「■ データ保存」にまとめ、保存先パスは次の行に出す。"""
    lines: list[str] = []
    if minute_bar_line:
        body = minute_bar_line.removeprefix(_MINUTE_BAR_PREFIX)
        lines.append(f"・分足: {body}")
    if chart_line:
        if saved := _CHART_SAVED.match(chart_line):
            lines.extend([f"・グラフ: {saved['what']}", f"  保存先: {saved['path']}"])
        elif failed := _CHART_FAILED.match(chart_line):
            lines.extend([f"・グラフ: 生成失敗({failed['note']})", f"  保存先: {failed['path']}"])
        else:
            lines.append(f"・グラフ: {chart_line.removeprefix('グラフ')}")
    return ["■ データ保存", *lines] if lines else []


def period_paper_lines(summary: dict, kind: str) -> list[str]:
    daily = summary["daily"]
    ops = daily["operational_summary"]
    reports = daily["reports"]
    lines = [
        f"・損益: {daily['total_profit_loss']:+.0f}円",
        f"・約定 {daily['order_count']}件 / 日次レポート {daily['report_count']}日",
        "・" + _market_status_text(
            ops["market_assessment_status_counts"], daily["report_count"]
        ),
        f"・緊急停止: {ops['emergency_stop_days']}日",
    ]
    skip_totals: dict[str, int] = {}
    skip_events: dict[str, list[dict]] = {}
    for report in reports:
        for label, count in report.get("skip_counts", {}).items():
            skip_totals[label] = skip_totals.get(label, 0) + count
        for label, events in report.get("skip_evaluations", {}).items():
            skip_events.setdefault(label, []).extend(events)
    if skip_totals:
        lines.append(f"・見送り {sum(skip_totals.values())}件")
        lines.extend(
            "  " + _format_skip_group(label, count, skip_events.get(label, []))
            for label, count in sorted(skip_totals.items())
        )
    else:
        lines.append("・見送り 記録なし")

    if kind == "週次":
        by_weekday: dict[int, list[float]] = {}
        for report in reports:
            day = date.fromisoformat(str(report["date"])[:10])
            by_weekday.setdefault(day.weekday(), []).append(float(report.get("total_profit_loss") or 0))
        lines.append(
            "・曜日別損益: "
            + (", ".join(
                f"{WEEKDAYS_JA[weekday]} {sum(values):+.0f}円"
                for weekday, values in sorted(by_weekday.items())
            ) if by_weekday else "記録なし")
        )
        lines.append("・市場状態の推移: " + " → ".join(
            f"{str(item['date'])[-5:]} {item.get('market_regime') or item.get('market_assessment_status') or '不明'}"
            for item in reports
        ) if reports else "・市場状態の推移: 記録なし")
    return lines


def period_error_lines(summary: dict) -> list[str]:
    """期間内の日次エラーを重複なく集め、日次と同じ「■ エラー N件」へ集約する。"""
    daily = summary["daily"]
    messages: list[str] = []
    for report in daily["reports"]:
        for message in report.get("log_error_summaries") or []:
            if str(message) not in messages:
                messages.append(str(message))
    total = int(daily["operational_summary"].get("log_error_count") or 0)
    return error_section_lines(messages, total)


def period_notification_lines(summary: dict, trend: dict | None, kind: str, analysis: str | None) -> list[str]:
    backtest = summary["backtest"]
    backtest_lines = (
        [f"・対象期間一致: 損益 {backtest['total_pnl']}円 / 約定 {backtest['total_trades']}件"]
        if backtest["exact_period_run_available"]
        else ["なし（対象期間に一致するバックテスト結果なし）"]
    )
    price_band_trend = summary.get("price_band_trend_check")
    lines = [
        f"■ A. ペーパートレード実績({kind})",
        *period_paper_lines(summary, kind),
        "",
        *period_error_lines(summary),
        "",
        trend_check_heading(trend),
        *trend_check_lines(trend, include_progression=kind == "週次"),
    ]
    if kind == "月次":
        lines.extend(["", *price_band_monthly_lines(price_band_trend)])
        activity = price_band_block_lines(price_band_trend, with_rate=False)
        lines.extend(["", "価格帯別の取引機会（選定10銘柄）", *activity[1:]])
    else:
        lines.extend(["", *price_band_block_lines(price_band_trend)])
    if kind == "月次" and trend is None:
        lines.append("・判定できなかった日: 日次判定結果なし（原因を特定できる保存データなし）")
    elif kind == "月次" and trend is not None:
        undecidable_days = [
            item for item in trend["summaries"] if item["selected_undecidable"] or item["candidate_undecidable"]
        ]
        if undecidable_days:
            reason_counts = Counter(
                row.get("undecidable_reason") or "理由記録なし"
                for row in trend["rows"]
                if row["label"] not in JUDGED_LABELS
            )
            lines.append("・判定できなかった日: " + ", ".join(
                f"{item['trade_date']} (選定{item['selected_undecidable']}件/候補{item['candidate_undecidable']}件)"
                for item in undecidable_days
            ))
            if reason_counts:
                lines.append("・判定不能の原因: " + ", ".join(
                    f"{reason} {count}件" for reason, count in sorted(reason_counts.items())
                ))
        else:
            lines.append("・判定できなかった日: なし")
    lines.extend([
        "",
        "■ C. バックテスト（別枠）",
        *backtest_lines,
        "",
        *_next_check_section(next_check_lines(trend, price_band_trend)),
    ])
    if analysis:
        sanitized_analysis = sanitize_period_review(analysis)
        if sanitized_analysis:
            lines.extend(["", "■ LLM評価(参考)", sanitized_analysis])
    if (
        trend_check_has_reference(trend, include_progression=kind == "週次")
        or price_band_has_reference(price_band_trend)
        or price_band_activity_has_reference(price_band_trend)
    ):
        lines.extend(["", REFERENCE_NOTE])
    return lines


def next_check_lines(
    trend: dict | None,
    price_band_trend: dict | None,
    *,
    cache_update_ok: bool = True,
    check_ok: bool = True,
) -> list[str]:
    """実際に確認が必要な点だけを返す。何もなければ「なし」。"""
    items: list[str] = []
    if not cache_update_ok:
        items.append("日足更新が完了していません（答え合わせは判定不能）")
    elif not check_ok:
        items.append("答え合わせ処理でエラーが発生しました（ログを確認）")
    elif trend is not None:
        agg = trend["aggregate"]
        selected, candidate = agg["undecidable_selected"], agg["undecidable_candidate"]
        if selected or candidate:
            items.append(f"判定不能 選定{selected}件/候補{candidate}件（原因を確認）")
    for band, reason in price_band_missing_bands(price_band_trend):
        items.append(f"{band}円帯の欠測: {reason}")
    return items or ["なし"]


def _missing_band_sentence(price_band_trend: dict | None) -> str:
    bands = [band for band, _ in price_band_missing_bands(price_band_trend)]
    return "・".join(f"{band}円" for band in bands) + "帯は欠測です。" if bands else ""


def daily_conclusion(
    report: dict | None,
    *,
    cache_update_ok: bool,
    price_band_trend: dict | None = None,
) -> str:
    """日次分析の【概要】。結論を1〜2行で示す。"""
    head = (
        "日足更新・答え合わせ・日次レビューが完了しました。"
        if cache_update_ok
        else "日足更新が完了せず、答え合わせは判定不能です。"
    )
    if report is None:
        trade = "日次レポートなし。"
    elif not int(report.get("order_count") or 0):
        trade = "今日は売買なし。"
    else:
        trade = (
            f"今日は約定{int(report.get('order_count') or 0)}件、"
            f"損益 実現{float(report.get('realized_profit_loss') or 0):+.0f}円・"
            f"評価{float(report.get('unrealized_profit_loss') or 0):+.0f}円。"
        )
    return head + trade + _missing_band_sentence(price_band_trend)


def period_conclusion(summary: dict, kind: str) -> str:
    """週次・月次分析の【概要】。固有の集計内容は変えず、結論だけを先頭に示す。"""
    daily = summary["daily"]
    span = "今週" if kind == "週次" else "今月"
    if not daily["report_count"]:
        trade = f"{span}は日次レポートなし。"
    else:
        trade = f"{span}は約定{daily['order_count']}件、損益{daily['total_profit_loss']:+.0f}円。"
    return (
        f"{kind}分析が完了しました。" + trade
        + _missing_band_sentence(summary.get("price_band_trend_check"))
    )


REVIEW_CAVEAT = "※ロジック変更・認証修正・投資判断・売買指示は行わない参考意見です。"
_REVIEW_HEADINGS = {
    "本日の事実": "事実",
    "事実": "事実",
    "運用・データ状態": "運用・データ状態",
    "所見": "所見",
    "次回確認": "次回確認",
    "仮説": "仮説",
}
_REVIEW_BRACKET_LINE = re.compile(r"^[#■●\s*]*【(?P<name>[^】]{1,14})】\s*(?P<rest>.*)$")
_REVIEW_HEADING_LINE = re.compile(
    r"^[#■●\s*]*(?P<name>[^【】:：]{1,14}?)\s*[*]*\s*(?:[:：]\s*(?P<rest>.*))?$"
)
_BULLET_PREFIX = re.compile(r"^\s*(?:[-*・●]|\d+[.)])\s*")
_CAVEAT_SENTENCE = re.compile(r"参考意見|ロジック変更|認証情報の修正")


def format_daily_review(text: str) -> list[str]:
    """LLM日次評価を【見出し】+1項目1行に整える。注意書きは末尾に1回だけ付ける。"""
    sections: list[tuple[str, list[str]]] = []

    def add(heading: str, item: str) -> None:
        if not sections or sections[-1][0] != heading:
            sections.append((heading, []))
        if item:
            sections[-1][1].append(item)

    current = "所見"
    for raw in text.splitlines():
        line = raw.strip().replace("**", "")
        if not line:
            continue
        match = _REVIEW_BRACKET_LINE.match(line)
        marked = bool(match)
        if not match:
            match = _REVIEW_HEADING_LINE.match(line)
            marked = bool(match) and (line[0] in "#■" or "：" in line or ":" in line)
        name = match["name"].strip() if match else ""
        if match and name in _REVIEW_HEADINGS and (marked or not match["rest"]):
            current = _REVIEW_HEADINGS[name]
            add(current, "")
            line = (match["rest"] or "").strip()
            if not line:
                continue
        line = _BULLET_PREFIX.sub("", line)
        sentences = [
            sentence for sentence in re.split(r"(?<=。)", line)
            if sentence.strip() and not _CAVEAT_SENTENCE.search(sentence)
        ]
        item = "".join(sentences).strip()
        if item:
            add(current, item)

    lines: list[str] = []
    for heading, items in sections:
        if len(items) == 1:
            lines.append(f"【{heading}】{items[0]}")
        else:
            lines.append(f"【{heading}】")
            lines.extend(f"・{item}" for item in items)
    return [*lines, REVIEW_CAVEAT]


def _next_check_section(items: list[str]) -> list[str]:
    return ["■ D. 次回確認", *(items if items == ["なし"] else [f"・{item}" for item in items])]


def daily_notification_lines(
    report: dict | None,
    trend: dict | None,
    *,
    cache_update_ok: bool,
    analysis: str | None,
    price_band_trend: dict | None = None,
    check_ok: bool = True,
    no_trade_reason: dict | None = None,
    minute_bar_line: str | None = None,
    chart_line: str | None = None,
) -> list[str]:
    lines = ["■ A. ペーパートレード実績", *daily_paper_lines(report)]
    if report is None or not int(report.get("order_count") or 0):
        lines.append(f"・売買0件の理由: {describe_no_trade_reason(no_trade_reason)}")
    if error_lines := daily_error_lines(report):
        lines.extend(["", *error_lines])
    if not cache_update_ok:
        lines.extend(["", trend_check_heading(None), "・判定不能(日足更新失敗)"])
    else:
        lines.extend(["", trend_check_heading(trend), *trend_check_lines(trend)])
    lines.extend(["", *price_band_block_lines(price_band_trend)])
    if save_lines := data_save_lines(minute_bar_line, chart_line):
        lines.extend(["", *save_lines])
    lines.append("")
    lines.extend(
        _next_check_section(
            next_check_lines(trend, price_band_trend, cache_update_ok=cache_update_ok, check_ok=check_ok)
        )
    )
    if analysis:
        lines.extend(["", "■ LLM日次評価(参考)", *format_daily_review(analysis)])
    if (
        (cache_update_ok and trend_check_has_reference(trend))
        or price_band_has_reference(price_band_trend)
        or price_band_activity_has_reference(price_band_trend)
    ):
        lines.extend(["", REFERENCE_NOTE])
    return lines
