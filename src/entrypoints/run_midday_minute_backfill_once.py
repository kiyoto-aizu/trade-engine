"""昼推定診断に記録された入力銘柄の指定日分足を一回限りで補完します。"""
import argparse
import csv
import json
import logging
from datetime import date, datetime
from pathlib import Path
from time import perf_counter

from src.config import config
from src.entrypoints.run_minute_backfill import configure_logging
from src.infrastructure.market_data.get_intraday_bars import fetch_yahoo_intraday_bars_for_dates
from src.infrastructure.persistence.parquet_minute_bar_repository import ParquetMinuteBarRepository

logger = logging.getLogger(__name__)


def load_diagnostic_inputs(
    diagnostic_dirs: tuple[Path, ...],
    start_date: date,
    end_date: date,
) -> dict[date, set[str]]:
    """期間内の昼推定診断から、全入力候補を日付ごとに集めます。"""
    inputs: dict[date, set[str]] = {}
    for directory in diagnostic_dirs:
        if not directory.exists():
            continue
        for path in sorted(directory.glob("*.json")):
            try:
                target_date = date.fromisoformat(path.name[:10])
            except ValueError:
                logger.warning("昼推定診断の日付を判定できません: %s", path)
                continue
            if not start_date <= target_date <= end_date:
                continue
            try:
                diagnostic = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                logger.warning("昼推定診断を読み込めません: %s (%s)", path, exc)
                continue
            if not isinstance(diagnostic, dict) or not isinstance(diagnostic.get("candidates"), list):
                logger.warning("昼推定診断のcandidates形式が不正です: %s", path)
                continue
            if diagnostic.get("date") != target_date.isoformat():
                logger.warning("昼推定診断の日付がファイル名と一致しません: %s", path)
            symbols = inputs.setdefault(target_date, set())
            for candidate in diagnostic["candidates"]:
                if not isinstance(candidate, dict) or not isinstance(candidate.get("symbol"), str):
                    logger.warning("昼推定診断の候補形式が不正です: %s", path)
                    continue
                symbol = candidate["symbol"]
                if symbol:
                    symbols.add(symbol)
    return {target_date: symbols for target_date, symbols in inputs.items() if symbols}


def run(start_date: date, end_date: date, output_dir: Path, report_dir: Path) -> Path:
    if end_date < start_date:
        raise ValueError("終了日は開始日以降を指定してください。")
    diagnostic_dirs = tuple(
        config.MIDDAY_FILTERING_RESULT_ROOT / f"{price_cap:g}" / "diagnostics"
        for price_cap in (config.get_screening_price_cap(), *config.SCREENING_ALTERNATE_PRICE_CAPS)
    )
    inputs = load_diagnostic_inputs(diagnostic_dirs, start_date, end_date)
    timestamp = datetime.now()
    report_dir.mkdir(parents=True, exist_ok=True)
    report_path = report_dir / f"manual_midday_backfill_{timestamp:%Y%m%d_%H%M%S}.csv"
    if not inputs:
        with report_path.open("w", newline="", encoding="utf-8-sig") as stream:
            csv.writer(stream).writerow([
                "symbol", "date", "yahoo_bars_received", "bars_added", "status", "error", "elapsed_seconds",
            ])
        logger.warning(
            "指定期間に昼推定診断の入力銘柄がありません: %s〜%s 品質CSV=%s",
            start_date,
            end_date,
            report_path,
        )
        return report_path

    repository = ParquetMinuteBarRepository(output_dir)
    started = perf_counter()
    total_received = 0
    total_added = 0
    failures: list[str] = []
    received_dates: list[date] = []
    with report_path.open("w", newline="", encoding="utf-8-sig") as stream:
        writer = csv.writer(stream)
        writer.writerow(["symbol", "date", "yahoo_bars_received", "bars_added", "status", "error", "elapsed_seconds"])
        for target_date, symbols in sorted(inputs.items()):
            day_received = 0
            for symbol in sorted(symbols):
                request_started = perf_counter()
                error = ""
                try:
                    result = fetch_yahoo_intraday_bars_for_dates(symbol, target_date, target_date)
                    bars = [bar for bar in result.bars if bar.time[:10] == target_date.isoformat()]
                    if not result.ok:
                        error = result.error or "FETCH_FAILED"
                        bars = []
                        logger.warning(
                            "Yahoo分足の取得に失敗しました。次の銘柄・日へ進みます: "
                            "銘柄=%s 日付=%s エラー=%s",
                            symbol,
                            target_date,
                            error,
                        )
                except Exception as exc:
                    logger.warning(
                        "昼推定分足の取得に失敗しました。次の銘柄・日へ進みます: 銘柄=%s 日付=%s エラー=%s",
                        symbol,
                        target_date,
                        exc,
                    )
                    error = f"FETCH_ERROR:{type(exc).__name__}"
                    bars = []

                received_count = len(bars)
                added_count = 0
                if not error and bars:
                    existing_bars = repository.load_bars(target_date, symbol)
                    existing_times = {bar.time for bar in existing_bars}
                    new_bars = []
                    for bar in bars:
                        if bar.time in existing_times:
                            continue
                        existing_times.add(bar.time)
                        new_bars.append(bar)
                    if new_bars:
                        repository.replace_bars(target_date, symbol, [*existing_bars, *new_bars])
                    added_count = len(new_bars)
                status = "FETCH_FAILED" if error else "RECEIVED" if received_count else "NO_BARS"
                elapsed = perf_counter() - request_started
                writer.writerow([
                    symbol,
                    target_date.isoformat(),
                    received_count,
                    added_count,
                    status,
                    error,
                    f"{elapsed:.3f}",
                ])
                total_received += received_count
                total_added += added_count
                day_received += received_count
                if error or not received_count:
                    failures.append(f"{symbol}@{target_date.isoformat()}")
            if day_received:
                received_dates.append(target_date)

    elapsed_seconds = perf_counter() - started
    earliest = min(received_dates) if received_dates else None
    logger.info(
        "一回限りの昼推定分足バックフィル完了: 対象銘柄日=%d | 取得本数=%d | 新規追加=%d本 | "
        "未取得銘柄・日=%d件%s | Yahoo最古取得日=%s | 所要時間=%.3f秒 | 品質CSV=%s",
        sum(len(symbols) for symbols in inputs.values()),
        total_received,
        total_added,
        len(failures),
        f" ({', '.join(failures)})" if failures else "",
        earliest.isoformat() if earliest else "なし",
        elapsed_seconds,
        report_path,
    )
    for target_date, symbols in sorted(inputs.items()):
        logger.info("手動バックフィル対象: 日付=%s 銘柄数=%d", target_date, len(symbols))
    return report_path


def main() -> None:
    parser = argparse.ArgumentParser(description="昼推定診断の銘柄を指定日ごとに一度だけ補完します")
    parser.add_argument("--start-date", type=date.fromisoformat, required=True)
    parser.add_argument("--end-date", type=date.fromisoformat, required=True)
    parser.add_argument("--output-dir", type=Path, default=config.MINUTE_BAR_PARQUET_DIR)
    parser.add_argument(
        "--report-dir",
        type=Path,
        default=Path(__file__).resolve().parents[2] / "analysis" / "minute_bar_backfill_quality",
    )
    configure_logging()
    args = parser.parse_args()
    run(args.start_date, args.end_date, args.output_dir, args.report_dir)


if __name__ == "__main__":
    main()
