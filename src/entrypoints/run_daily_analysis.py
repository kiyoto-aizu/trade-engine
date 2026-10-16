"""日足更新、トレンド答え合わせ、日次分析通知を順番に実行する。"""
from __future__ import annotations

import argparse
import json
import logging
import time
from datetime import date, datetime
from pathlib import Path
from typing import Callable

from src.application.analysis_notification import (
    daily_conclusion,
    daily_notification_lines,
    load_trend_check,
)
from src.application.price_band_trend_check import (
    price_band_symbols_for_update,
    run_price_band_checks,
)
from src.infrastructure.analysis.daily_analyzer import create_daily_analyzer
from src.infrastructure.notification.slack_notify import (
    format_result_notification,
    notify_analysis,
    process_notification,
)
from src.infrastructure.report_publication import CHART_DATA_NAME, publish_daily_report
from src.infrastructure.persistence.trend_check_repository import TrendCheckRepository
from src.application.trend_check_usecase import (
    DEFAULT_DATABASE_FILE,
    TrendCheckPaths,
    load_selected_symbols,
    symbols_for_date,
)
from src.domain.no_trade_reason import NO_CANDIDATE, OTHER
from src.infrastructure.logging_config import configure_logging
from src.infrastructure.calendar.japanese_calendar import is_trading_day
from src.entrypoints.run_trend_check import run_daily_safely
from src.entrypoints.run_index_minute_backfill import save_index_minute_bars
from src.entrypoints.save_daily_minute_bars import save_daily_minute_bars
from src.entrypoints.update_daily_bar_cache import latest_confirmed_trading_day, update_cache

logger = logging.getLogger(__name__)
DEFAULT_RETRY_COUNT = 3
DEFAULT_RETRY_WAIT_SECONDS = 60
DAILY_CHARTS_ROOT = Path(__file__).resolve().parents[2] / "data" / "reports" / "daily_charts"


def refresh_cache_with_retries(
    trade_date: date,
    paths: TrendCheckPaths,
    *,
    retries: int = DEFAULT_RETRY_COUNT,
    wait_seconds: float = DEFAULT_RETRY_WAIT_SECONDS,
    now: datetime | None = None,
    update: Callable = update_cache,
    sleeper: Callable[[float], None] = time.sleep,
) -> tuple[bool, list[str]]:
    """対象日まで全対象銘柄の日足が揃うまで、最大3回更新する。"""
    symbols = sorted(
        set(symbols_for_date(paths, trade_date.isoformat()))
        | set(price_band_symbols_for_update(paths, trade_date))
    )
    if not symbols:
        logger.info("日足更新対象なし: %s", trade_date)
        return True, []

    current = now or datetime.now()
    expected = min(trade_date, latest_confirmed_trading_day(current)).isoformat()
    latest_by_symbol: dict[str, str] = {}
    remaining = symbols
    if retries < 1:
        raise ValueError("日足更新リトライ回数は1回以上を指定してください")
    for attempt in range(1, retries + 1):
        try:
            latest_by_symbol.update(update(paths.daily_cache_dir, remaining, 120, current))
        except Exception:
            logger.exception("日足キャッシュ更新に失敗しました: 試行=%d/%d", attempt, retries)
            latest_by_symbol = {}
        remaining = [
            symbol for symbol in symbols
            if latest_by_symbol.get(symbol, "") < expected
        ]
        if not remaining:
            logger.info("日足キャッシュ更新完了: 対象=%d銘柄 / 最新日=%s", len(symbols), expected)
            return True, []
        logger.warning(
            "日足が未更新の銘柄: 試行=%d/%d / 対象=%d銘柄",
            attempt, retries, len(remaining),
        )
        if attempt < retries:
            sleeper(wait_seconds)
    return False, remaining


def _load_daily_report(paths: TrendCheckPaths, trade_date: date) -> dict | None:
    path = paths.daily_report_dir / f"{trade_date.isoformat()}.json"
    if not path.exists():
        logger.warning("日次取引レポートがありません: %s", path)
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("日次レポートを読み込めません: %s (%s)", path, exc)
        return None
    if not isinstance(data, dict):
        logger.warning("日次取引レポートの形式が不正です: %s", path)
        return None
    return data


def resolve_no_trade_reason(report: dict | None, paths: TrendCheckPaths, trade_date: date) -> dict | None:
    """売買0件の理由。レポートがない日は、当日のフィルタ結果の有無から候補なしを判別する。"""
    if report is not None:
        return report.get("no_trade_reason")
    if not load_selected_symbols(paths, trade_date.isoformat()):
        return {"reason": NO_CANDIDATE, "detail": "当日のフィルタ結果なし"}
    return {"reason": OTHER, "detail": "日次レポートなし"}


def write_chart_data(report: dict, path: Path) -> None:
    """HTML版の分足診断用JSONを書き出す。失敗してもPNG生成と日次分析は止めない。"""
    try:
        from scripts.analysis.intraday_chart_payload import write_chart_payload

        write_chart_payload(report, path)
    except Exception:
        logger.exception("分足診断JSONの生成に失敗しました: %s", path)


def generate_intraday_charts(
    trade_date: date,
    *,
    collect_report: Callable | None = None,
    write_report: Callable | None = None,
) -> str:
    """分足診断図を生成し、失敗しても日次分析を継続する。"""
    output_dir = DAILY_CHARTS_ROOT / trade_date.isoformat()
    output_base = output_dir / "intraday_trade_diagnostics"
    try:
        import matplotlib

        matplotlib.use("Agg", force=True)
        if collect_report is None or write_report is None:
            from scripts.analysis.plot_intraday_trade_diagnostics import collect, write_outputs
            collect_report = collect
            write_report = write_outputs

        root = Path(__file__).resolve().parents[2]
        report = collect_report(root, trade_date.isoformat())
        write_report(report, output_base)
        write_chart_data(report, output_dir / CHART_DATA_NAME)
        detail_count = len(report.get("symbols", []))
        relative_dir = output_dir.relative_to(root)
        line = (
            f"グラフ 全体図1枚・詳細図{detail_count}枚を保存: "
            f"{relative_dir}\\"
        )
        logger.info("%s", line)
        return line
    except Exception:
        logger.exception("分足診断グラフの生成に失敗しました: %s", output_dir)
        return f"グラフ生成失敗（保存先: {output_dir}。詳細はログを確認）"


def run_daily_analysis(
    trade_date: date,
    *,
    paths: TrendCheckPaths | None = None,
    database_file: Path | None = None,
    retry_count: int = DEFAULT_RETRY_COUNT,
    retry_wait_seconds: float = DEFAULT_RETRY_WAIT_SECONDS,
    now: datetime | None = None,
    sleeper: Callable[[float], None] = time.sleep,
) -> bool:
    paths = paths or TrendCheckPaths.default()
    database_file = database_file or DEFAULT_DATABASE_FILE
    with process_notification("日次分析", notify_lifecycle=False, trigger="取引日引け後"):
        cache_update_ok, missing_symbols = refresh_cache_with_retries(
            trade_date,
            paths,
            retries=retry_count,
            wait_seconds=retry_wait_seconds,
            now=now,
            sleeper=sleeper,
        )
        if not cache_update_ok:
            logger.error("日足更新が完了しませんでした。答え合わせは判定不能として通知します: %s", missing_symbols)

        # 日足更新の後・グラフ生成の前に、当日の対象銘柄の分足を保存する。失敗しても後続を止めない。
        minute_result = save_daily_minute_bars(
            trade_date,
            now=now,
            filtering_dir=paths.filtering_dir,
            output_dir=paths.minute_bar_dir,
        )
        minute_bar_line = minute_result.summary_line() if minute_result else None
        chart_line = generate_intraday_charts(trade_date)

        check_ok = False
        if cache_update_ok:
            check_ok = run_daily_safely(
                trade_date,
                refresh_cache=False,
                database_file=database_file,
                paths=paths,
            )
        report = _load_daily_report(paths, trade_date)
        repository = TrendCheckRepository(database_file)
        trend = (
            load_trend_check(repository, trade_date, trade_date)
            if cache_update_ok
            else None
        )
        price_band_trend = run_price_band_checks(
            trade_date,
            paths,
            cache_update_ok=cache_update_ok,
        )
        publication = publish_daily_report(
            report, trade_date, chart_dir=DAILY_CHARTS_ROOT / trade_date.isoformat()
        )
        no_trade_reason = resolve_no_trade_reason(report, paths, trade_date)
        analyzer = create_daily_analyzer()
        analysis_input = {
            "date": (report or {}).get("date", trade_date.isoformat()),
            "trading_mode": (report or {}).get("trading_mode"),
            "order_count": (report or {}).get("order_count", 0),
            "realized_profit_loss": (report or {}).get("realized_profit_loss", 0),
            "unrealized_profit_loss": (report or {}).get("unrealized_profit_loss", 0),
            "positions": (report or {}).get("positions", []),
            "market_conditions": (report or {}).get("market_conditions", {}),
            "log_errors": (report or {}).get("log_errors", {}),
            "no_trade_reason": (
                None
                if (report or {}).get("order_count")
                else no_trade_reason or {"reason": "未記録"}
            ),
            "trading_evaluation": (report or {}).get("trading_evaluation"),
            "skip_counts": {
                "ATR危険度見送り": len((report or {}).get("atr_danger_skips") or []),
                "市場危険度見送り": len((report or {}).get("market_regime_danger_skips") or []),
                "注意レジームRSI見送り": len((report or {}).get("market_regime_caution_rsi_filters") or []),
            },
            "trend_check": (
            {"available": False, "reason": "日足更新失敗"} if not cache_update_ok else
            trend["aggregate"] if trend else {"available": False, "reason": "日次判定結果なし"}
            ),
        }
        analysis = analyzer.analyze(analysis_input) if analyzer else None
        lines = daily_notification_lines(
            report,
            trend,
            cache_update_ok=cache_update_ok,
            analysis=analysis,
            price_band_trend=price_band_trend,
            check_ok=check_ok,
            no_trade_reason=no_trade_reason,
            minute_bar_line=minute_bar_line,
            chart_line=chart_line,
        )
        lines.append(publication.message)
        message = format_result_notification(
            "分析運用",
            "日次分析",
            daily_conclusion(
                report,
                cache_update_ok=cache_update_ok,
                price_band_trend=price_band_trend,
            ),
            lines,
        )
        notify_analysis(message)
        success = cache_update_ok and check_ok
        if not success:
            logger.error("【日次分析】異常終了: 日足更新=%s 答え合わせ=%s", cache_update_ok, check_ok)
        return success


def main(argv: list[str] | None = None) -> int:
    configure_logging()
    parser = argparse.ArgumentParser(description="日足更新・答え合わせ・日次分析を実行します")
    parser.add_argument("--date", type=date.fromisoformat, default=date.today())
    parser.add_argument("--force", action="store_true", help="休場日でも実行する")
    args = parser.parse_args(argv)
    if not args.force and not is_trading_day(args.date):
        logger.info("休場日のため日次分析をスキップします")
        return 0
    success = run_daily_analysis(args.date)
    # 引け後の日経225分足の保存。記録専用で、失敗しても日次分析の結果には影響させない。
    save_index_minute_bars(args.date, args.date)
    return 0 if success else 1


if __name__ == "__main__":
    raise SystemExit(main())
