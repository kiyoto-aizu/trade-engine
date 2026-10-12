import argparse
import logging
from datetime import date, datetime, timedelta
from pathlib import Path

from src.infrastructure.analysis.daily_analyzer import create_daily_analyzer
from src.infrastructure.analysis.summary_loader import (
    build_daily_period_comparison,
    build_period_metadata,
    load_backtest_summaries,
    load_daily_summaries,
    summarize_backtest_runs,
    summarize_daily_reports,
)
from src.infrastructure.persistence.filter_decision_repository import FilterDecisionRepository
from src.application.analysis_notification import (
    load_trend_check,
    period_conclusion,
    period_notification_lines,
)
from src.application.price_band_trend_check import load_price_band_trends
from src.application.trend_check_usecase import DEFAULT_DATABASE_FILE
from src.infrastructure.persistence.trend_check_repository import TrendCheckRepository
from src.config import config
from src.infrastructure.notification.slack_notify import format_result_notification, notify_analysis, process_notification
from src.infrastructure.persistence.storage import write_json
from src.infrastructure.logging_config import configure_logging

logger = logging.getLogger(__name__)

def _week_bounds(reference_date: date) -> tuple[date, date]:
    week_start = reference_date - timedelta(days=reference_date.weekday())
    return week_start, week_start + timedelta(days=4)


def build_weekly_summary(
    week_start: date, week_end: date, report_directory: Path, backtest_directory: Path,
    filter_decision_repository: FilterDecisionRepository | None = None,
    as_of: date | None = None,
    trend_repository: TrendCheckRepository | None = None,
) -> dict:
    as_of = as_of or date.today()
    daily = load_daily_summaries(report_directory, week_start, week_end)
    previous_week_start = week_start - timedelta(days=7)
    previous_week_end = week_end - timedelta(days=7)
    previous_daily = load_daily_summaries(
        report_directory, previous_week_start, previous_week_end
    )
    backtests = load_backtest_summaries(backtest_directory, week_start, week_end)
    period = build_period_metadata(week_start, week_end, as_of)
    daily_summary = summarize_daily_reports(daily)
    previous_daily_summary = summarize_daily_reports(previous_daily)
    return {
        "week_start": week_start.isoformat(),
        "week_end": week_end.isoformat(),
        "period": period,
        "daily": daily_summary,
        "comparison": build_daily_period_comparison(
            period,
            daily_summary,
            build_period_metadata(previous_week_start, previous_week_end, previous_week_end),
            previous_daily_summary,
        ),
        "backtest": summarize_backtest_runs(backtests, week_start, week_end),
        "filter_decision_events": (
            filter_decision_repository.summarize_finalized_events(week_start, week_end)
            if filter_decision_repository else {"count": 0, "by_event_type": {}}
        ),
        "trend_check": (
            load_trend_check(trend_repository, week_start, week_end, with_progression=True)
            if trend_repository else None
        ),
        "price_band_trend_check": load_price_band_trends(week_start, week_end),
    }


def main() -> None:
    configure_logging()
    parser = argparse.ArgumentParser(description="週次のペーパートレード・バックテスト総合分析を実行します")
    parser.add_argument("--week-start", default=None, help="対象週の月曜（YYYY-MM-DD）。省略時は実行日が属する週")
    parser.add_argument("--reports", type=Path, default=Path("data/reports/daily"))
    parser.add_argument("--backtests", type=Path, default=Path("data/backtest/runs"))
    parser.add_argument("--output", type=Path, default=Path("data/reports/weekly"))
    parser.add_argument("--force", action="store_true", help="土曜以外でも実行する")
    args = parser.parse_args()

    if args.week_start:
        week_start = date.fromisoformat(args.week_start)
        if week_start.weekday() != 0:
            parser.error("--week-startは月曜日（YYYY-MM-DD）を指定してください")
    else:
        week_start = None
    today = date.today()
    if not args.force and today.weekday() != 5:
        return
    if week_start is None:
        week_start, _ = _week_bounds(today)
    week_end = week_start + timedelta(days=4)
    with process_notification("週次分析", notify_lifecycle=False, trigger="土曜または手動実行"):
        summary = build_weekly_summary(
            week_start, week_end, args.reports, args.backtests,
            FilterDecisionRepository(config.FILTER_DECISION_DATABASE_FILE), today,
            TrendCheckRepository(DEFAULT_DATABASE_FILE) if DEFAULT_DATABASE_FILE.exists() else None,
        )
        analyzer = create_daily_analyzer()
        analysis = analyzer.analyze_weekly(summary) if analyzer else None
        result = {**summary, "generated_at": datetime.now().isoformat(timespec="seconds"), "llm_analysis": analysis}
        output_path = args.output / f"{week_start:%Y-%m-%d}_{week_end:%Y-%m-%d}.json"
        try:
            args.output.mkdir(parents=True, exist_ok=True)
            saved = write_json(output_path, result)
        except OSError:
            saved = False
            logger.critical("週次分析結果を保存できません: %s", output_path, exc_info=True)
        else:
            if not saved:
                logger.critical("週次分析結果を保存できません: %s", output_path)
        lines = [
            f"対象週: {week_start}～{week_end}",
            f"集計状態: {'確定' if summary['period']['is_complete'] else '途中'}",
            *period_notification_lines(summary, summary["trend_check"], "週次", analysis),
            (
                f"詳細: {output_path}"
                if saved
                else f"分析結果の保存に失敗しました({output_path.name})"
            ),
        ]
        message = format_result_notification(
            "分析運用", "週次分析", period_conclusion(summary, "週次"), lines
        )
        notify_analysis(message)


if __name__ == "__main__":
    main()