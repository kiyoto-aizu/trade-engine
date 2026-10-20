"""
================================================================================
フィルタリング実行エントリーポイント
スクリーニング結果から出来高急騰銘柄を抽出し、次の売買対象を決定します。
================================================================================
"""
import argparse
import logging
from datetime import date, datetime, timedelta
from time import monotonic, perf_counter

from src.config import config
from src.config.budget_summary import log_budget_summary
from src.application.filtering_usecase import BoardRetryPolicy, FilteringUseCase
from src.application.price_band_filtering_usecase import PriceBandFilteringUseCase
from src.infrastructure.persistence.decision_journal_repository import DecisionJournalRepository
from src.infrastructure.kabu.board_repository import BoardRepository
from src.infrastructure.kabu.get_token import get_api_token
from src.infrastructure.kabu.unregister import unregister_all
from src.infrastructure.kabu.token_provider import get_token_provider
from src.infrastructure.execution_lock import market_workflow_lock
from src.infrastructure.logging_config import configure_logging
from src.infrastructure.kabu.registration_aware_board_cache import RegistrationAwareBoardCache
from src.infrastructure.market_data.cached_volume_client import CachedVolumeClient
from src.infrastructure.market_data.yahoo_finance_client import YahooFinanceClient
from src.infrastructure.notification.slack_notify import notify_daily, process_notification
from src.infrastructure.persistence.filtering_result_repository import FilteringResultRepository
from src.infrastructure.persistence.filtering_diagnostics_repository import FilteringDiagnosticsRepository
from src.infrastructure.persistence.screening_result_repository import ScreeningResultRepository
from src.infrastructure.calendar.japanese_calendar import is_trading_day

def _build_board_retry_policy(board_cache) -> BoardRetryPolicy | None:
    """270円(通常結果)の板欠損リトライ設定。打ち切りは価格帯の締め切りの手前に置く。"""
    if board_cache is None or not config.FILTER_BOARD_RETRY_ENABLED:
        return None
    now = datetime.now()
    deadline_at = datetime.combine(now.date(), config.FILTERING_PRICE_BAND_DEADLINE_TIME) - timedelta(
        seconds=config.FILTER_BOARD_RETRY_MARGIN_SECONDS
    )
    return BoardRetryPolicy(
        max_rounds=config.FILTER_BOARD_RETRY_MAX_ROUNDS,
        wait_seconds=config.FILTER_BOARD_RETRY_WAIT_SECONDS,
        deadline_monotonic=monotonic() + (deadline_at - now).total_seconds(),
        deadline_at=deadline_at,
    )


def main() -> None:
    """
    フィルタリング処理を実行します。
    
    処理フロー：
    1. API トークンを取得
    2. スクリーニング結果リポジトリを初期化
    3. 各銘柄の本日出来高と過去平均を比較
    4. 出来高急騰率でトップ10に絞り込み
    5. 結果を保存・通知
    """
    parser = argparse.ArgumentParser(description="フィルタリングを実行します")
    parser.add_argument("--date", dest="target_date", type=date.fromisoformat, help="対象日 (YYYY-MM-DD)。指定時は日足から過去結果を再計算")
    args = parser.parse_args()

    configure_logging()
    log_budget_summary(logging.getLogger(__name__))
    if not is_trading_day(args.target_date or date.today()):
        logging.getLogger(__name__).info('休場日のため、フィルタリングを開始しません。')
        return
    with market_workflow_lock() as acquired:
        if not acquired:
            logging.getLogger(__name__).warning("他の市場処理が実行中のため、フィルタリングを中止します。")
            return
        with process_notification(
            'フィルタリング',
            notify_lifecycle=False,
            trigger='スクリーニング結果',
        ):
            token = None
            board_client = None
            board_cache = None
            notifier = None
            if args.target_date is None:
                token = get_api_token()
                if not token:
                    raise SystemExit('トークン取得に失敗しました。')
                if unregister_all(token) is None:
                    raise SystemExit('銘柄登録の全解除に失敗しました。')
                board_cache = RegistrationAwareBoardCache(
                    BoardRepository(token),
                    lambda: unregister_all(token),
                    batch_size=config.SCREENING_BATCH_SIZE,
                    max_workers=config.FILTER_BOARD_MAX_CONCURRENCY,
                )
                board_client = board_cache
                notifier = notify_daily
            journal_repository = None
            if args.target_date is None:
                # 過去日指定の再実行で本番の記録を汚さないよう、当日実行時のみ記録する
                try:
                    journal_repository = DecisionJournalRepository(
                        config.FILTER_DECISION_DATABASE_FILE, execution_mode=config.TRADING_MODE
                    )
                except Exception:
                    logging.getLogger(__name__).exception('判断記録DBの初期化に失敗しました。記録なしで続行します。')
            volume_client = CachedVolumeClient(YahooFinanceClient())
            usecase = FilteringUseCase(
                ScreeningResultRepository(config.SCREENING_RESULT_DIRECTORY),
                board_client,
                volume_client,
                FilteringResultRepository(config.FILTERING_RESULT_DIRECTORY),
                notifier,
                decision_journal_repository=journal_repository,
                diagnostics_repository=(
                    FilteringDiagnosticsRepository(config.FILTERING_DIAGNOSTICS_DIRECTORY)
                    if args.target_date is None else None
                ),
            )
            try:
                if board_cache is None:
                    usecase.execute(target_date=args.target_date)
                else:
                    PriceBandFilteringUseCase(
                        board_cache,
                        volume_client,
                        now=datetime.now,
                        monotonic_clock=monotonic,
                        perf_counter_clock=perf_counter,
                        filtering_use_case_factory=FilteringUseCase,
                        screening_repository_factory=ScreeningResultRepository,
                        filtering_repository_factory=FilteringResultRepository,
                        diagnostics_repository_factory=FilteringDiagnosticsRepository,
                        logger=logging.getLogger(__name__),
                    ).run(
                        usecase,
                        target_date=args.target_date,
                        board_retry=_build_board_retry_policy(board_cache),
                    )
            except Exception:
                if board_cache is not None and not board_cache.clear_registrations():
                    logging.getLogger(__name__).error("フィルタ失敗後の銘柄登録解除にも失敗しました。")
                raise
            if get_token_provider().recovery_failed:
                # 1run1回: トークン再取得後も401が続いた(復旧失敗)場合のみ通知する
                notify_daily("kabuステーションAPIの認証が回復しません（トークン再取得後も401が継続しました）。")


if __name__ == '__main__':
    main()
