"""
================================================================================
分足バックフィル・エントリーポイント
過去N日分のフィルタリング結果に登場した銘柄について、Yahoo Financeの
1分足を取得し、既存の分足を保持したまま不足する時刻の足を追加します。

想定運用：毎週月曜のバックテスト実行前に1回だけ実行する
（Yahooの1分足は直近7日程度しか遡れないため、週次で回収しておく）。

分足はParquetパーティションへ保存します。
================================================================================
"""
import argparse
import csv
import json
import logging
from datetime import date, datetime, timedelta
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path
from time import perf_counter

from src.infrastructure.persistence.backtest_input_repository import load_daily_filtering_symbols
from src.application.minute_bar_backfill_usecase import MinuteBarBackfillUseCase
from src.config import config
from src.config.budget_summary import log_budget_summary
from src.infrastructure.market_data.get_intraday_bars import get_yahoo_intraday_bars
from src.infrastructure.notification.slack_notify import format_result_notification, notify_analysis, process_notification
from src.infrastructure.persistence.parquet_minute_bar_repository import ParquetMinuteBarRepository

logger = logging.getLogger(__name__)


def collect_recent_symbol_tiers(
    filtering_dir: Path,
    days: int,
    today: date,
    additional_filtering_dirs: tuple[Path, ...] = (),
    midday_diagnostic_dirs: tuple[Path, ...] = (),
) -> tuple[list[str], list[str], list[str], dict[str, set[date]]]:
    """朝結果、昼選定、昼選定外を優先順に重複なく収集します。"""
    cutoff = today - timedelta(days=days)
    morning_dates: dict[str, set[date]] = {}
    selected_dates: dict[str, set[date]] = {}
    input_dates: dict[str, set[date]] = {}
    for directory in (filtering_dir, *additional_filtering_dirs):
        if not directory.exists():
            continue
        for date_text, symbols in load_daily_filtering_symbols(directory).items():
            try:
                result_date = date.fromisoformat(date_text)
            except ValueError:
                logger.warning("朝のフィルタ結果の日付が不正です: %s", date_text)
                continue
            if result_date < cutoff or result_date > today:
                continue
            for symbol in symbols:
                morning_dates.setdefault(symbol, set()).add(result_date)

    for directory in midday_diagnostic_dirs:
        if not directory.exists():
            continue
        for path in sorted(directory.glob("*.json")):
            try:
                diagnostic_date = date.fromisoformat(path.name[:10])
            except ValueError:
                logger.warning("昼推定フィルタ診断の日付を判定できません: %s", path)
                continue
            if diagnostic_date < cutoff or diagnostic_date > today:
                continue
            try:
                diagnostic = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                logger.warning("昼推定フィルタ診断を読み込めません: %s (%s)", path, exc)
                continue
            if not isinstance(diagnostic, dict):
                logger.warning("昼推定フィルタ診断の形式が不正です: %s", path)
                continue
            if diagnostic.get("date") != diagnostic_date.isoformat():
                logger.warning("昼推定フィルタ診断の日付がファイル名と一致しません: %s", path)

            candidates = diagnostic.get("candidates")
            if not isinstance(candidates, list):
                logger.warning("昼推定フィルタ診断にcandidates一覧がありません: %s", path)
                candidate_symbols: set[str] = set()
                selected_from_candidates: set[str] = set()
            else:
                candidate_symbols = set()
                selected_from_candidates = set()
                for candidate in candidates:
                    if not isinstance(candidate, dict):
                        logger.warning("昼推定診断の候補形式が不正です: %s", path)
                        continue
                    symbol = candidate.get("symbol")
                    if not isinstance(symbol, str) or not symbol:
                        logger.warning("昼推定診断の候補銘柄が不正です: %s", path)
                        continue
                    candidate_symbols.add(symbol)
                    if candidate.get("selected") is True:
                        selected_from_candidates.add(symbol)
            for symbol in candidate_symbols:
                input_dates.setdefault(symbol, set()).add(diagnostic_date)

            summary = diagnostic.get("summary")
            adopted = summary.get("adopted_symbols") if isinstance(summary, dict) else None
            if not isinstance(adopted, list) or any(not isinstance(item, str) or not item for item in adopted):
                logger.warning("昼推定診断のadopted_symbolsが不正です。候補のselected属性を使います: %s", path)
                adopted_symbols = selected_from_candidates
            else:
                adopted_symbols = set(adopted)
            for symbol in adopted_symbols:
                selected_dates.setdefault(symbol, set()).add(diagnostic_date)

    morning_symbols = set(morning_dates)
    selected_symbols = set(selected_dates) - morning_symbols
    unselected_symbols = set(input_dates) - morning_symbols - set(selected_dates)
    expected_dates: dict[str, set[date]] = {}
    for source in (morning_dates, selected_dates, input_dates):
        for symbol, dates in source.items():
            expected_dates.setdefault(symbol, set()).update(dates)
    return (
        sorted(morning_symbols),
        sorted(selected_symbols),
        sorted(unselected_symbols),
        expected_dates,
    )


def collect_recent_symbols(
    filtering_dir: Path,
    days: int,
    today: date,
    additional_filtering_dirs: tuple[Path, ...] = (),
    midday_diagnostic_dirs: tuple[Path, ...] = (),
) -> list[str]:
    """朝フィルタと昼診断の銘柄を重複なく集めます。"""
    morning, selected, unselected, _ = collect_recent_symbol_tiers(
        filtering_dir, days, today, additional_filtering_dirs, midday_diagnostic_dirs
    )
    return sorted(set(morning) | set(selected) | set(unselected))


def write_quality_csv(
    report_dir: Path,
    timestamp: datetime,
    expected_dates: dict[str, set[date]],
    bars_by_date: dict[tuple[str, date], int],
    failures: dict[str, str],
    unprocessed_symbols: list[str],
) -> Path:
    report_dir.mkdir(parents=True, exist_ok=True)
    path = report_dir / f"minute_backfill_{timestamp:%Y%m%d_%H%M%S}.csv"
    records = [
        (symbol, target_date)
        for symbol, dates in expected_dates.items()
        for target_date in dates
    ]
    record_set = set(records)
    records.extend(key for key in bars_by_date if key not in record_set)
    with path.open("w", newline="", encoding="utf-8-sig") as stream:
        writer = csv.writer(stream)
        writer.writerow(["symbol", "date", "yahoo_bars_received", "status"])
        for symbol, target_date in sorted(records, key=lambda item: (item[1], item[0])):
            count = bars_by_date.get((symbol, target_date), 0)
            status = "received" if count else failures.get(symbol, "NO_BARS")
            if symbol in unprocessed_symbols:
                status = "NOT_ATTEMPTED_TIMEOUT"
            writer.writerow([symbol, target_date.isoformat(), count, status])
    return path


def configure_logging() -> None:
    """標準出力と日次ローテーションするファイルへログを出力します。"""
    logging.root.handlers.clear()
    log_level = getattr(logging, config.LOG_LEVEL, logging.INFO)
    logging.root.setLevel(log_level)

    formatter = logging.Formatter('%(asctime)s %(levelname)s %(name)s: %(message)s')

    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(formatter)
    logging.root.addHandler(stream_handler)

    file_handler = TimedRotatingFileHandler(
        config.LOG_FILE_PATH,
        when='midnight',
        interval=1,
        backupCount=config.LOG_BACKUP_COUNT,
        encoding='utf-8',
    )
    file_handler.setFormatter(formatter)
    logging.root.addHandler(file_handler)


def main(
    now_provider=None,
    filtering_dir: Path | None = None,
    output_dir: Path | None = None,
    quality_report_dir: Path | None = None,
) -> None:
    configure_logging()
    log_budget_summary(logger)
    now = (now_provider or datetime.now)()

    parser = argparse.ArgumentParser(description="Yahoo分足で自前収集データをバックフィルします")
    parser.add_argument("--days", type=int, default=7, help="遡って取得する日数（Yahoo 1分足の実用上限は7日程度）")
    parser.add_argument("--filtering-dir", type=Path, default=None, help="フィルタリング結果ディレクトリ")
    parser.add_argument("--output-dir", type=Path, default=None, help="分足保存先ディレクトリ")
    args = parser.parse_args()

    custom_filtering_dir = filtering_dir is not None or args.filtering_dir is not None
    target_filtering_dir = filtering_dir or args.filtering_dir or config.FILTERING_RESULT_DIRECTORY
    target_output_dir = output_dir or args.output_dir or config.MINUTE_BAR_PARQUET_DIR
    additional_filtering_dirs = () if custom_filtering_dir else tuple(
        config.FILTERING_PRICE_BAND_RESULT_ROOT / f"{price_cap:g}"
        for price_cap in config.SCREENING_ALTERNATE_PRICE_CAPS
    )
    midday_diagnostic_dirs = () if custom_filtering_dir else tuple(
        config.MIDDAY_FILTERING_RESULT_ROOT / f"{price_cap:g}" / "diagnostics"
        for price_cap in (config.get_screening_price_cap(), *config.SCREENING_ALTERNATE_PRICE_CAPS)
    )

    morning_symbols, selected_symbols, unselected_symbols, expected_dates = collect_recent_symbol_tiers(
        target_filtering_dir,
        args.days,
        now.date(),
        additional_filtering_dirs=additional_filtering_dirs,
        midday_diagnostic_dirs=midday_diagnostic_dirs,
    )
    symbol_count = len(morning_symbols) + len(selected_symbols) + len(unselected_symbols)
    symbols = [*morning_symbols, *selected_symbols, *unselected_symbols]
    if not symbols:
        logger.info("直近%d日分のフィルタリング結果がないため、バックフィルを行いません。", args.days)
        return
    logger.info(
        "分足バックフィル対象: 朝=%d件 | 昼選定=%d件 | 昼選定外=%d件 | 合計=%d件",
        len(morning_symbols), len(selected_symbols), len(unselected_symbols), symbol_count,
    )

    with process_notification("分足バックフィル", notify_lifecycle=False, trigger="フィルタリング結果"):
        usecase = MinuteBarBackfillUseCase(
            fetch_intraday_bars=get_yahoo_intraday_bars,
            repository=ParquetMinuteBarRepository(target_output_dir),
        )
        started = perf_counter()
        imported_counts: dict[str, int] = {}
        bars_by_date: dict[tuple[str, date], int] = {}
        failures: dict[str, str] = {}
        unprocessed_symbols: list[str] = []
        for tier_symbols in (morning_symbols, selected_symbols):
            if tier_symbols:
                imported_counts.update(usecase.run(tier_symbols, days=args.days))
                bars_by_date.update(usecase.last_bars_by_date)
                failures.update(usecase.last_failures)
        if unselected_symbols:
            imported_counts.update(usecase.run(
                unselected_symbols,
                days=args.days,
                max_elapsed_seconds=config.MINUTE_BACKFILL_UNSELECTED_TIMEOUT_SECONDS,
            ))
            bars_by_date.update(usecase.last_bars_by_date)
            failures.update(usecase.last_failures)
            unprocessed_symbols = usecase.last_unprocessed_symbols
        elapsed_seconds = perf_counter() - started

        total = sum(imported_counts.values())
        missed = sorted(
            f"{symbol}@{target_date.isoformat()}"
            for symbol, dates in expected_dates.items()
            for target_date in dates
            if bars_by_date.get((symbol, target_date), 0) == 0
        )
        quality_report = write_quality_csv(
            quality_report_dir or (
                Path(__file__).resolve().parents[2] / "data" / "analysis" / "minute_bar_backfill_quality"
            ),
            now,
            expected_dates,
            bars_by_date,
            failures,
            unprocessed_symbols,
        )
        logger.info(
            "分足バックフィル実績: 朝=%d件 | 昼選定=%d件 | 昼選定外=%d件 | "
            "新規追加=%d本 | 未取得銘柄・日=%d件%s | 所要時間=%.3f秒 | 品質CSV=%s",
            len(morning_symbols),
            len(selected_symbols),
            len(unselected_symbols),
            total,
            len(missed),
            f" ({', '.join(missed)})" if missed else "",
            elapsed_seconds,
            quality_report,
        )
        message = format_result_notification(
            "市場データ管理",
            "分足バックフィル",
            "分足データの取込が完了しました。",
            [
                f"対象銘柄数(朝/昼選定/昼選定外): {len(morning_symbols)}/{len(selected_symbols)}/{len(unselected_symbols)}件",
                f"新規追加本数: {total}本",
                f"未取得銘柄・日: {len(missed)}件",
                f"所要時間: {elapsed_seconds:.3f}秒",
            ],
        )
        notify_analysis(message)


if __name__ == "__main__":
    main()
