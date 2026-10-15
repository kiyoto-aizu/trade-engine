"""
================================================================================
取引実行エントリーポイント
フィルタ、リング結果を基に自動取引を実行します。
================================================================================
"""
import logging
from contextlib import contextmanager
from logging.handlers import TimedRotatingFileHandler
from datetime import datetime
from pathlib import Path

from src.config import config
from src.config.startup_validation import ConfigValidationError, validate_startup_config
from src.application.market_regime_usecase import MarketRegimeUseCase
from src.application.market_tendency_notification import build_market_tendency_lines
from src.application.trading_usecase import TradingUseCase
from src.domain.market_tendency import TendencyPeriod
from src.domain.rules import is_market_closed
from src.infrastructure.kabu.get_token import get_api_token
from src.infrastructure.persistence.filtering_result_repository import FilteringResultRepository
from src.infrastructure.persistence.filtering_diagnostics_repository import FilteringDiagnosticsRepository
from src.infrastructure.persistence.filter_decision_repository import FilterDecisionRepository
from src.infrastructure.persistence.decision_journal_repository import DecisionJournalRepository
from src.infrastructure.persistence.market_regime_daily_repository import MarketRegimeDailyRepository
from src.infrastructure.execution_lock import market_workflow_lock
from src.infrastructure.notification.slack_notify import notify_critical, notify_daily, process_notification
from src.infrastructure.paper.paper_order_client import PaperOrderClient
from src.infrastructure.persistence.storage import StateFileCorruptError
from src.infrastructure.market_data.index_daily_bar_fallback import create_index_daily_bar_fallback
from src.infrastructure.market_data.yahoo_index_client import YahooIndexClient
from src.infrastructure.calendar.japanese_calendar import is_trading_session


@contextmanager
def _notify_corrupt_state_file():
    try:
        yield
    except StateFileCorruptError as exc:
        logger = logging.getLogger(__name__)
        logger.critical("状態ファイルの破損により取引の起動を中止します: %s", exc)
        try:
            notify_critical(f"【取引】起動を中止しました(状態ファイルの破損)\n{exc}")
        except Exception:
            logger.exception("状態ファイル破損の通知に失敗しました。")
        raise


def _create_decision_journal_repository():
    """判断記録DBを作成する。失敗しても取引は開始する(記録なし)。"""
    try:
        return DecisionJournalRepository(
            config.FILTER_DECISION_DATABASE_FILE, execution_mode=config.TRADING_MODE
        )
    except Exception:
        logging.getLogger(__name__).exception('判断記録DBの初期化に失敗しました。記録なしで続行します。')
        return None


def _create_market_regime_daily_repository():
    """市場レジームの日次記録DBを作成する。失敗しても取引は開始する(記録なし)。"""
    try:
        return MarketRegimeDailyRepository(config.MARKET_REGIME_DAILY_DATABASE_FILE)
    except Exception:
        logging.getLogger(__name__).exception('市場レジーム日次記録DBの初期化に失敗しました。記録なしで続行します。')
        return None


def _load_filtering_activity_ratios(target_date, symbols):
    try:
        diagnostics = FilteringDiagnosticsRepository(
            config.FILTERING_DIAGNOSTICS_DIRECTORY
        ).load_latest_for_date(target_date.isoformat())
    except Exception as exc:
        logging.getLogger(__name__).warning(
            "TENDENCY_ACTIVITY_UNAVAILABLE: 診断ファイル読込失敗 | 理由=%s",
            type(exc).__name__,
        )
        diagnostics = None

    ratios_by_symbol = {}
    if diagnostics is not None:
        ratios_by_symbol = {
            str(candidate.get("symbol")): candidate.get("ratio")
            for candidate in diagnostics.get("candidates", [])
            if candidate.get("selected")
        }
    ratios = [ratios_by_symbol.get(str(symbol)) for symbol in symbols]
    missing = sum(ratio is None for ratio in ratios)
    if missing:
        logging.getLogger(__name__).warning(
            "TENDENCY_ACTIVITY_UNAVAILABLE: 対象銘柄=%d | 欠損=%d",
            len(symbols),
            missing,
        )
    return ratios


def create_trading_use_case(token: str) -> TradingUseCase:
    """実行モードに応じた取引ユースケースを組み立てます。"""
    root = Path(__file__).resolve().parents[2]
    runtime_state_dir = root / 'data' / 'trading'
    runtime_state_dir.mkdir(parents=True, exist_ok=True)
    order_sender = None
    if config.TRADING_MODE == 'paper':
        order_sender = PaperOrderClient(
            prices={},
            cash=config.OPERATING_CAPITAL,
            state_path=root / config.PAPER_ACCOUNT_STATE_FILE,
        )
    elif config.TRADING_MODE != 'live' or config.IS_DEMO or not config.ENABLE_LIVE_ORDERING:
        raise ValueError(
            'ライブ注文にはTRADING_MODE=live、IS_DEMO=false、ENABLE_LIVE_ORDERING=trueが必要です。'
        )
    return TradingUseCase(
        token=token,
        order_history_path=root / config.ORDER_HISTORY_FILE,
        kill_switch_baseline_path=root / config.KILL_SWITCH_BASELINE_FILE,
        order_sender=order_sender,
        filtering_result_repository=FilteringResultRepository(config.FILTERING_RESULT_DIRECTORY),
        filter_decision_repository=FilterDecisionRepository(
            config.FILTER_DECISION_DATABASE_FILE,
            allowed_execution_modes=frozenset({"paper", "live"}),
        ),
        decision_journal_repository=_create_decision_journal_repository(),
        enable_shadow_position_tracking=True,
        market_regime_daily_repository=_create_market_regime_daily_repository(),
        market_regime_usecase=MarketRegimeUseCase(
            market_data_client=YahooIndexClient(),
            thresholds=config.MARKET_REGIME_THRESHOLDS,
            realized_volatility_window=config.MARKET_REGIME_REALIZED_VOL_WINDOW,
            data_range=config.MARKET_REGIME_DATA_RANGE,
            fallback_bar_provider=create_index_daily_bar_fallback(),
            retry_wait_seconds=config.MARKET_REGIME_RETRY_WAIT_SECONDS,
            adx_threshold=config.MARKET_REGIME_ADX_TREND_THRESHOLD,
        ),
    )


def configure_logging() -> None:
    """ロギングを設定します。"""
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


def _validate_config_or_exit() -> None:
    """設定値の違反を全件ログ・通知し、起動を止める(通知失敗は握りつぶす)。"""
    try:
        validate_startup_config(config)
    except ConfigValidationError as exc:
        logging.getLogger(__name__).error('起動時の設定値検証に失敗しました。\n%s', exc)
        try:
            notify_critical(f'【取引】起動を中止しました(設定値の違反)\n{exc}')
        except Exception:
            logging.getLogger(__name__).exception('設定値違反の通知に失敗しました。')
        raise SystemExit(1) from exc


def main(now_provider=None) -> None:
    """
    取引ボットを起動します。
    
    処理フロー：
    1. ロギングを設定
    2. APIトークンを取得
    3. TradingUseCaseを組み立てて実行
    """
    configure_logging()
    _validate_config_or_exit()
    logging.getLogger(__name__).info('取引モード: %s', config.TRADING_MODE_LABEL)
    with market_workflow_lock() as acquired, _notify_corrupt_state_file():
        if not acquired:
            logging.getLogger(__name__).warning("他の市場処理が実行中のため、トレーディングを中止します。")
            return
        with process_notification(
            '取引',
            notify_lifecycle=False,
            trigger='フィルタリング結果',
            start_detail=f'開始（{config.TRADING_MODE_LABEL}）',
        ):
            now = (now_provider or datetime.now)()
            if not is_trading_session(
                now,
                config.MARKET_OPEN_HOUR,
                config.MARKET_OPEN_MINUTE,
                config.MARKET_CLOSE_HOUR,
                config.MARKET_CLOSE_MINUTE,
            ):
                logging.getLogger(__name__).info('市場時間外または休場日のため、取引を開始しません。')
                return

            filtering_repository = FilteringResultRepository(config.FILTERING_RESULT_DIRECTORY)
            filtering_result = filtering_repository.load_for_date(now.date())
            if not filtering_result or not filtering_result.symbols:
                logging.getLogger(__name__).info('当日のフィルタ結果がないため、取引を開始しません。')
                return

            token = get_api_token()
            if not token:
                raise SystemExit('トークン取得に失敗しました。')

            use_case = create_trading_use_case(token)
            use_case.warn_on_overnight_positions()
            if hasattr(use_case, 'prepare_market_regime'):
                use_case.prepare_market_regime()
                tendency_lines = build_market_tendency_lines(
                    getattr(use_case, "market_regime_assessment", None),
                    _load_filtering_activity_ratios(now.date(), filtering_result.symbols),
                    period=TendencyPeriod.CURRENT_DAY,
                    activity_label="対象銘柄の活発度",
                    include_market_line=False,
                    include_activity_line=False,
                )
                message = (
                    "【業務】取引運用\n"
                    "【機能】取引開始\n"
                    "【概要】\n"
                    f"{config.TRADING_MODE_LABEL}を開始しました。\n"
                    "【詳細】\n"
                    f"対象銘柄数: {len(filtering_result.symbols)}\n"
                    + "\n".join([*use_case.market_conditions_detail(), *tendency_lines])
                )
                notify_daily(message)
            if not config.ALLOW_OVERNIGHT_HOLDING and is_market_closed(
                now.time(),
                config.MARKET_LIQUIDATION_HOUR,
                config.MARKET_LIQUIDATION_MINUTE,
            ):
                use_case.run()
            else:
                market_data = use_case.collect_preflight_market_data(filtering_result.symbols)
                if market_data is None:
                    logging.getLogger(__name__).error('市場データまたは板情報を取得できないため、取引を開始しません。')
                    return
                use_case.run(preflight_market_data=market_data)


if __name__ == '__main__':
    main()
