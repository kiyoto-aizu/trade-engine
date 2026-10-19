"""
================================================================================
昼フィルタリング実行エントリーポイント
450円・900円帯を、昼休みにYahoo 1分足(09:00〜09:30)から推定してフィルタリングします。
270円帯は売買判断に使わず、朝の板の実測倍率との比較用に推定だけを記録します。
================================================================================
"""
import argparse
import logging
from datetime import date, datetime, time as dt_time
from functools import partial
from time import monotonic

from src.application.midday_filtering_usecase import MiddayBandSpec, MiddayFilteringUseCase
from src.application.selection_atr_observer import SelectionAtrObserver
from src.config import config
from src.infrastructure.calendar.japanese_calendar import is_trading_day
from src.application.midday_verification_summary import format_verification_summary, summarize_verification
from src.infrastructure.execution_lock import midday_filtering_lock, midday_filtering_verify_lock
from src.infrastructure.logging_config import configure_logging
from src.infrastructure.market_data.cached_volume_client import CachedVolumeClient
from src.infrastructure.market_data.get_intraday_bars import fetch_yahoo_intraday_bars
from src.infrastructure.market_data.yahoo_finance_client import YahooFinanceClient
from src.infrastructure.notification.slack_notify import format_result_notification, notify_daily, process_notification
from src.infrastructure.persistence.filtering_diagnostics_repository import FilteringDiagnosticsRepository
from src.infrastructure.persistence.filtering_result_repository import FilteringResultRepository
from src.infrastructure.persistence.parquet_minute_bar_repository import ParquetMinuteBarRepository
from src.infrastructure.persistence.screening_result_repository import ScreeningResultRepository

# 朝のフィルタ・板の登録解除が終わっている目安の時刻。これより前の当日実行は行わない
EARLIEST_RUN_TIME = dt_time(9, 40)

logger = logging.getLogger(__name__)


def _skip(reason: str, notify: bool = True) -> None:
    """昼フィルタを実行しなかった理由をログと通知に残す。通知の失敗は握りつぶす。検証用実行では通知しない。"""
    logger.info(reason)
    if not notify:
        print(f"昼フィルタを実行しませんでした: {reason}")
        return
    try:
        notify_daily(format_result_notification(
            "銘柄選定", "フィルタリング(昼・推定値)", "昼のフィルタリングを実行しませんでした。", [f"理由: {reason}"]
        ))
    except Exception:
        logger.exception("昼フィルタのスキップ通知に失敗しました。")


def build_bands(root=None) -> list[MiddayBandSpec]:
    """450円・900円(売買判断に使わない研究用)と、比較用の270円の評価対象を組み立てる。

    rootは結果・診断JSONの保存先(省略時は本番の保存先)。候補一覧は読み取りのみ。
    """
    root = config.MIDDAY_FILTERING_RESULT_ROOT if root is None else root
    bands = []
    for price_cap in sorted(config.SCREENING_ALTERNATE_PRICE_CAPS):
        name = f"{price_cap:g}"
        bands.append(MiddayBandSpec(
            name=name,
            price_cap=price_cap,
            screening_repository=ScreeningResultRepository(config.SCREENING_PRICE_BAND_RESULT_ROOT / name),
            result_repository=FilteringResultRepository(root / name),
            diagnostics_repository=FilteringDiagnosticsRepository(root / name / "diagnostics"),
        ))
    primary_cap = config.get_screening_price_cap()
    bands.append(MiddayBandSpec(
        name=f"{primary_cap:g}",
        price_cap=primary_cap,
        screening_repository=ScreeningResultRepository(config.SCREENING_RESULT_DIRECTORY),
        diagnostics_repository=FilteringDiagnosticsRepository(root / f"{primary_cap:g}" / "diagnostics"),
        comparison_only=True,
    ))
    return bands


def _budget_seconds(target_date: date, now: datetime) -> float:
    """全体の締切までの残り秒数。当日は本日の締切時刻まで、過去日の手動実行は(締切-開始)の長さ。"""
    if target_date == now.date():
        deadline = datetime.combine(now.date(), config.MIDDAY_FILTERING_DEADLINE_TIME)
        return (deadline - now).total_seconds()
    start = datetime.combine(target_date, config.MIDDAY_FILTERING_START_TIME)
    end = datetime.combine(target_date, config.MIDDAY_FILTERING_DEADLINE_TIME)
    return (end - start).total_seconds()


# 検証用実行の既定の予算。約150銘柄(1銘柄30秒タイムアウト×リトライ)が終わるのに十分な長さ
DEFAULT_VERIFY_BUDGET_SECONDS = 3 * 60 * 60


def parse_args(argv=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="昼のフィルタリング(Yahoo分足による推定)を実行します")
    parser.add_argument("--date", dest="target_date", type=date.fromisoformat, help="対象日 (YYYY-MM-DD)。省略時は本日")
    parser.add_argument(
        "--verify", action="store_true",
        help="検証用実行。開始時刻・締切のチェックを無視し、結果は検証用ディレクトリに保存、通知はせず要約を表示する(--date必須)",
    )
    parser.add_argument("--budget-seconds", type=float, help="検証用実行の予算(秒)。--verify のときだけ指定できる")
    parser.add_argument(
        "--verify-save-bars", action="store_true",
        help="検証用の分足Parquet保存先に09:00〜09:30の足を保存する。--verify のときだけ指定できる",
    )
    args = parser.parse_args(argv)
    if args.verify and args.target_date is None:
        parser.error("--verify を使うときは --date が必要です。")
    if not args.verify and (args.budget_seconds is not None or args.verify_save_bars):
        parser.error("--budget-seconds と --verify-save-bars は --verify と一緒にだけ指定できます。")
    if args.budget_seconds is not None and args.budget_seconds <= 0:
        parser.error("--budget-seconds は正の数にしてください。")
    return args


def _build_use_case(*, verify: bool, save_bars: bool, target_date: date) -> MiddayFilteringUseCase:
    if verify:
        comparison_root = config.MIDDAY_FILTERING_VERIFY_RESULT_ROOT / target_date.isoformat() / "comparison"
        bar_repository = ParquetMinuteBarRepository(config.MINUTE_BAR_PARQUET_VERIFY_DIR) if save_bars else None
    else:
        comparison_root = config.MIDDAY_FILTERING_RESULT_ROOT / "comparison"
        bar_repository = ParquetMinuteBarRepository(config.MINUTE_BAR_PARQUET_DIR)
    return MiddayFilteringUseCase(
        volume_client=CachedVolumeClient(YahooFinanceClient()),
        minute_fetcher=partial(fetch_yahoo_intraday_bars, timeout=config.MIDDAY_FILTERING_FETCH_TIMEOUT_SECONDS),
        bar_repository=bar_repository,
        # 朝の板の診断JSONは270円帯の比較のために読み取るだけ
        board_diagnostics_repository=FilteringDiagnosticsRepository(config.FILTERING_DIAGNOSTICS_DIRECTORY),
        comparison_repository=FilteringDiagnosticsRepository(comparison_root),
        notifier=None if verify else notify_daily,
        skip_dates=() if verify else None,
        # 選定時点のATR比の観測記録のみ(採用銘柄・売買判断には使わない)
        selection_atr_observer=SelectionAtrObserver(
            lambda symbol, until: YahooFinanceClient().get_daily_ohlc_history(symbol, "3mo", until=until)
        ),
    )


def run_verification(target_date: date, budget_seconds: float, save_bars: bool) -> None:
    """検証用実行。時刻チェックなし・本番と別の保存先・通知なし。本番の昼フィルタとは別のロックを使う。"""
    if not is_trading_day(target_date):
        _skip("休場日のため", notify=False)
        return
    with midday_filtering_verify_lock() as acquired:
        if not acquired:
            _skip("検証用の昼フィルタが既に実行中のため", notify=False)
            return
        root = config.MIDDAY_FILTERING_VERIFY_RESULT_ROOT / target_date.isoformat()
        use_case = _build_use_case(verify=True, save_bars=save_bars, target_date=target_date)
        report = use_case.run(build_bands(root), target_date, monotonic() + budget_seconds)
        if report.skipped_reason:
            print(f"昼フィルタを実行しませんでした: {report.skipped_reason}")
            return
        production_budget = (
            datetime.combine(target_date, config.MIDDAY_FILTERING_DEADLINE_TIME)
            - datetime.combine(target_date, config.MIDDAY_FILTERING_START_TIME)
        ).total_seconds()
        for line in format_verification_summary(summarize_verification(report, production_budget)):
            logger.info(line)
            print(line)
        print(f"結果の保存先: {root}" + (f" / 分足: {config.MINUTE_BAR_PARQUET_VERIFY_DIR}" if save_bars else " / 分足は保存していません"))


def main(argv=None) -> None:
    args = parse_args(argv)
    configure_logging()
    if args.verify:
        run_verification(
            args.target_date,
            args.budget_seconds if args.budget_seconds is not None else DEFAULT_VERIFY_BUDGET_SECONDS,
            args.verify_save_bars,
        )
        return

    now = datetime.now()
    target_date = args.target_date or now.date()
    if not is_trading_day(target_date):
        _skip("休場日のため")
        return
    if target_date.isoformat() in config.MIDDAY_FILTERING_SKIP_DATES:
        _skip(f"設定(MIDDAY_FILTERING_SKIP_DATES)により{target_date}はスキップ(午前のみの取引日など)")
        return
    if target_date == now.date() and now.time() < EARLIEST_RUN_TIME:
        _skip(f"朝の処理が終わる前({EARLIEST_RUN_TIME:%H:%M}前)のため")
        return
    budget = _budget_seconds(target_date, now)
    if budget <= 0:
        _skip(f"全体の締切({config.MIDDAY_FILTERING_DEADLINE_TIME:%H:%M})を過ぎているため")
        return

    # 取引ループが保持する market_workflow_lock とは別の専用ロック。昼フィルタの二重起動だけを防ぐ
    with midday_filtering_lock() as acquired:
        if not acquired:
            _skip("昼フィルタが既に実行中のため(専用ロックを取得できませんでした)")
            return
        with process_notification("フィルタリング(昼)", notify_lifecycle=False, trigger="スクリーニング結果"):
            use_case = _build_use_case(verify=False, save_bars=True, target_date=target_date)
            use_case.run(build_bands(), target_date, monotonic() + budget)


if __name__ == "__main__":
    main()