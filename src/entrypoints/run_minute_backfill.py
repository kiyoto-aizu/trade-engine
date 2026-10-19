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
import json
import logging
from datetime import date, datetime, timedelta
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path
from time import perf_counter

from src.infrastructure.persistence.backtest_input_repository import load_daily_filtering_symbols
from src.application.minute_bar_backfill_usecase import MinuteBarBackfillUseCase
from src.config import config
from src.infrastructure.market_data.get_intraday_bars import get_yahoo_intraday_bars
from src.infrastructure.notification.slack_notify import format_result_notification, notify_analysis, process_notification
from src.infrastructure.persistence.parquet_minute_bar_repository import ParquetMinuteBarRepository

logger = logging.getLogger(__name__)


def collect_recent_symbols(
    filtering_dir: Path,
    days: int,
    today: date,
    additional_filtering_dirs: tuple[Path, ...] = (),
    midday_diagnostic_dirs: tuple[Path, ...] = (),
) -> list[str]:
    """朝のフィルタ結果と昼推定診断から直近days日分の銘柄を重複なく集めます。"""
    daily_symbols: dict[str, set[str]] = {}
    for directory in (filtering_dir, *additional_filtering_dirs):
        if not directory.exists():
            continue
        for date_text, symbols in load_daily_filtering_symbols(directory).items():
            daily_symbols.setdefault(date_text, set()).update(symbols)
    cutoff = today - timedelta(days=days)
    for directory in midday_diagnostic_dirs:
        if not directory.exists():
            continue
        diagnostics_by_date: dict[date, Path] = {}
        for path in sorted(directory.glob("*.json")):
            try:
                diagnostic_date = date.fromisoformat(path.name[:10])
            except ValueError:
                logger.warning(
                    "昼推定フィルタ診断の日付を判定できません。朝の結果だけで続行します: %s",
                    path,
                )
                continue
            if diagnostic_date >= cutoff:
                diagnostics_by_date[diagnostic_date] = path

        for diagnostic_date, path in diagnostics_by_date.items():
            try:
                diagnostic = json.loads(path.read_text(encoding="utf-8"))
                if not isinstance(diagnostic, dict) or diagnostic.get("date") != diagnostic_date.isoformat():
                    raise ValueError("診断JSONのdateがファイル名と一致しません")
                summary = diagnostic.get("summary")
                if not isinstance(summary, dict):
                    raise ValueError("診断JSONのsummaryがありません")
                adopted_symbols = summary.get("adopted_symbols")
                if not isinstance(adopted_symbols, list) or any(
                    not isinstance(symbol, str) or not symbol for symbol in adopted_symbols
                ):
                    raise ValueError("診断JSONのsummary.adopted_symbolsが不正です")
            except (OSError, json.JSONDecodeError, ValueError) as exc:
                logger.warning(
                    "昼推定フィルタ診断を読み込めません。朝の結果だけで続行します: %s (%s)",
                    path,
                    exc,
                )
                continue
            daily_symbols.setdefault(diagnostic_date.isoformat(), set()).update(adopted_symbols)

    recent_symbols = {
        symbol
        for date_text, symbols_on_day in daily_symbols.items()
        if date.fromisoformat(date_text) >= cutoff
        for symbol in symbols_on_day
    }
    return sorted(recent_symbols)


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


def main(now_provider=None, filtering_dir: Path | None = None, output_dir: Path | None = None) -> None:
    configure_logging()
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

    primary_symbols = set(collect_recent_symbols(target_filtering_dir, args.days, now.date()))
    symbols = collect_recent_symbols(
        target_filtering_dir,
        args.days,
        now.date(),
        additional_filtering_dirs=additional_filtering_dirs,
        midday_diagnostic_dirs=midday_diagnostic_dirs,
    )
    additional_symbols = set(symbols) - primary_symbols
    if not symbols:
        logger.info("直近%d日分のフィルタリング結果がないため、バックフィルを行いません。", args.days)
        return
    logger.info(
        "分足バックフィル対象: 通常結果=%d件 | 通常結果にない追加銘柄=%d件 | 合計=%d件",
        len(primary_symbols), len(additional_symbols), len(symbols),
    )

    with process_notification("分足バックフィル", notify_lifecycle=False, trigger="フィルタリング結果"):
        usecase = MinuteBarBackfillUseCase(
            fetch_intraday_bars=get_yahoo_intraday_bars,
            repository=ParquetMinuteBarRepository(target_output_dir),
        )
        started = perf_counter()
        imported_counts = usecase.run(symbols, days=args.days)
        elapsed_seconds = perf_counter() - started

        total = sum(imported_counts.values())
        logger.info(
            "分足バックフィル実績: 対象=%d件 | 通常結果にない追加銘柄=%d件 | 新規追加=%d本 | 所要時間=%.3f秒",
            len(symbols), len(additional_symbols), total, elapsed_seconds,
        )
        message = format_result_notification(
            "市場データ管理",
            "分足バックフィル",
            "分足データの取込が完了しました。",
            [
                f"対象銘柄数: {len(symbols)}件",
                f"通常結果にない追加銘柄: {len(additional_symbols)}件",
                f"新規追加本数: {total}本",
                f"所要時間: {elapsed_seconds:.3f}秒",
            ],
        )
        notify_analysis(message)


if __name__ == "__main__":
    main()
