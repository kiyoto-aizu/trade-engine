import argparse
import logging
from calendar import monthrange
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
from src.infrastructure.notification.slack_notify import format_result_notification, notify_analysis, process_notification
from src.infrastructure.persistence.storage import write_json
from src.infrastructure.persistence.filter_decision_repository import FilterDecisionRepository
from src.infrastructure.logging_config import configure_logging
from src.application.analysis_notification import (
    load_trend_check,
    period_conclusion,
    period_notification_lines,
)
from src.application.price_band_trend_check import load_price_band_trends
from src.application.trend_check_usecase import DEFAULT_DATABASE_FILE
from src.infrastructure.persistence.trend_check_repository import TrendCheckRepository
from src.config.task_schedule import is_last_trading_day_of_month
from src.config import config

logger = logging.getLogger(__name__)


def _month_bounds(month_text: str) -> tuple[date, date]:
    try:
        year, month = (int(value) for value in month_text.split("-"))
        start = date(year, month, 1)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"月の形式が不正です: {month_text}（YYYY-MMを指定してください）") from exc
    return start, date(year, month, monthrange(year, month)[1])


def _load_daily_summaries(report_directory: Path, month_text: str) -> list[dict]:
    start, end = _month_bounds(month_text)
    return load_daily_summaries(report_directory, start, end)


def _load_backtest_summaries(backtest_directory: Path, start: date, end: date) -> list[dict]:
    return load_backtest_summaries(backtest_directory, start, end)


def build_monthly_summary(
    month_text: str, report_directory: Path, backtest_directory: Path,
    filter_decision_repository: FilterDecisionRepository | None = None,
    as_of: date | None = None,
    trend_repository: TrendCheckRepository | None = None,
) -> dict:
    start, end = _month_bounds(month_text)
    as_of = as_of or date.today()
    daily = _load_daily_summaries(report_directory, month_text)
    previous_month_end = start - timedelta(days=1)
    previous_month_start = previous_month_end.replace(day=1)
    previous_daily = load_daily_summaries(
        report_directory, previous_month_start, previous_month_end
    )
    backtests = _load_backtest_summaries(backtest_directory, start, end)
    period = build_period_metadata(start, end, as_of)
    daily_summary = summarize_daily_reports(daily)
    previous_daily_summary = summarize_daily_reports(previous_daily)
    return {
        "month": month_text,
        "period": period,
        "daily": daily_summary,
        "comparison": build_daily_period_comparison(
            period,
            daily_summary,
            build_period_metadata(previous_month_start, previous_month_end, previous_month_end),
            previous_daily_summary,
        ),
        "backtest": summarize_backtest_runs(backtests, start, end),
        "filter_decision_events": (
            filter_decision_repository.summarize_finalized_events(start, end)
            if filter_decision_repository else {"count": 0, "by_event_type": {}}
        ),
        "trend_check": (
            load_trend_check(trend_repository, start, end)
            if trend_repository else None
        ),
        "price_band_trend_check": load_price_band_trends(start, end),
    }


def main() -> None:
    configure_logging()
    parser = argparse.ArgumentParser(description="月次のペーパートレード・バックテスト総合分析を実行します")
    parser.add_argument("--month", default=None, help="対象月（YYYY-MM）。省略時は当月")
    parser.add_argument("--reports", type=Path, default=Path("data/reports/daily"))
    parser.add_argument("--backtests", type=Path, default=Path("data/backtest/runs"))
    parser.add_argument("--output", type=Path, default=Path("data/reports/monthly"))
    parser.add_argument("--force", action="store_true", help="月末以外でも実行する")
    args = parser.parse_args()

    today = date.today()
    month_text = args.month or f"{today.year:04d}-{today.month:02d}"
    if not args.force and not is_last_trading_day_of_month(today):
        logger.info("月末営業日ではないため月次分析をスキップします")
        return
    with process_notification("月次総合分析", notify_lifecycle=False, trigger="月末または手動実行"):
        summary = build_monthly_summary(
            month_text, args.reports, args.backtests,
            FilterDecisionRepository(config.FILTER_DECISION_DATABASE_FILE), today,
            TrendCheckRepository(DEFAULT_DATABASE_FILE) if DEFAULT_DATABASE_FILE.exists() else None,
        )
        analyzer = create_daily_analyzer()
        analysis = analyzer.analyze_monthly(summary) if analyzer else None
        result = {**summary, "generated_at": datetime.now().isoformat(timespec="seconds"), "llm_analysis": analysis}
        output_path = args.output / f"{month_text}.json"
        try:
            args.output.mkdir(parents=True, exist_ok=True)
            saved = write_json(output_path, result)
        except OSError:
            saved = False
            logger.critical("月次分析結果を保存できません: %s", output_path, exc_info=True)
        else:
            if not saved:
                logger.critical("月次分析結果を保存できません: %s", output_path)
        lines = [
            f"対象月: {month_text}",
            f"集計状態: {'確定' if summary['period']['is_complete'] else '途中'}",
            *period_notification_lines(summary, summary["trend_check"], "月次", analysis),
            (
                f"詳細: {output_path}"
                if saved
                else f"分析結果の保存に失敗しました({output_path.name})"
            ),
        ]
        message = format_result_notification(
            "分析運用", "月次総合分析", period_conclusion(summary, "月次"), lines
        )
        notify_analysis(message)


if __name__ == "__main__":
    main()