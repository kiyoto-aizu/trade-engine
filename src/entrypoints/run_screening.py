"""
================================================================================
スクリーニング実行エントリーポイント
松元鉮ランキングから取引候補銀柄を技不的に選別します。
================================================================================
"""
import argparse
import logging
from datetime import date
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path

from src.config import config
from src.application.market_regime_usecase import MarketRegimeUseCase
from src.infrastructure.kabu.get_token import get_api_token
from src.infrastructure.kabu.regulation_repository import RegulationRepository
from src.infrastructure.kabu.primaryexchange_repository import PrimaryExchangeRepository
from src.infrastructure.kabu.unregister import unregister_all
from src.infrastructure.kabu.register import register_symbols
from src.infrastructure.kabu.token_provider import get_token_provider
from src.infrastructure.execution_lock import market_workflow_lock
from src.infrastructure.notification.slack_notify import notify_daily, process_notification
from src.infrastructure.persistence.screening_result_repository import ScreeningResultRepository
from src.infrastructure.persistence.listed_security_repository import ListedSecurityRepository
from src.infrastructure.persistence.historical_regulation_repository import HistoricalRegulationRepository
from src.infrastructure.persistence.screening_api_check_repository import ScreeningApiCheckRepository
from src.infrastructure.market_data.historical_ranking_repository import HistoricalRankingRepository
from src.infrastructure.market_data.yahoo_finance_client import YahooFinanceClient
from src.infrastructure.market_data.index_daily_bar_fallback import create_index_daily_bar_fallback
from src.infrastructure.market_data.yahoo_index_client import YahooIndexClient
from src.application.screening_usecase import ScreeningUseCase
from src.infrastructure.calendar.japanese_calendar import is_trading_day

def notify_result(message: str) -> None:
    notify_daily(message)


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


def _run_screening() -> None:
    """
    スクリーニング処理を実行します。
    
    処理フロー：
    1. APIトークンを取得
    2. 各リポジトリを害化
    3. ScreeningUseCaseを実行
    """
    parser = argparse.ArgumentParser(description="スクリーニングを実行します")
    parser.add_argument("--date", dest="target_date", type=date.fromisoformat, help="対象日 (YYYY-MM-DD)。指定時は銘柄マスタと日足から再計算")
    args = parser.parse_args()
    effective_target_date = args.target_date or date.today()

    configure_logging()
    if not is_trading_day(effective_target_date):
        logging.getLogger(__name__).info('休場日のため、スクリーニングを開始しません。')
        return
    with market_workflow_lock() as acquired:
        if not acquired:
            logging.getLogger(__name__).warning("他の市場処理が実行中のため、スクリーニングを中止します。")
            return
        with process_notification(
            'スクリーニング',
            notify_lifecycle=False,
            trigger='スケジュールまたは手動実行',
        ):
            token = get_api_token()
            if not token:
                raise SystemExit('トークン取得に失敗しました。')
            if unregister_all(token) is None:
                raise SystemExit('銘柄登録の全解除に失敗しました。')
            root = Path(__file__).resolve().parents[2]
            # ADR-0001: kabu STATIONの/rankingは市場区分ごと上位50件しか返さず、
            # 価格を意識した絞り込みができない。上場銘柄マスタ+日足データから
            # 全銘柄のランキングを自前計算するHistoricalRankingRepositoryを、
            # 通常運用(過去日付指定なし)でも常用する。
            market_data_client = YahooFinanceClient()
            ranking_repository = HistoricalRankingRepository(
                ListedSecurityRepository(root / 'data' / 'universe' / 'listed_securities.csv'),
                market_data_client,
            )
            if args.target_date:
                # 過去日付を明示指定した場合のみ、規制情報もその時点の履歴で再現する
                # (純粋なバックテスト用途。本番の規制判定には使わない)
                regulation_repository = HistoricalRegulationRepository(
                    root / 'data' / 'regulation' / 'historical_regulations.csv'
                )
                exchange_repository = PrimaryExchangeRepository(token)
            else:
                regulation_repository = RegulationRepository(token)
                exchange_repository = PrimaryExchangeRepository(token)
                regulation_repository = ScreeningApiCheckRepository(
                    config.SCREENING_API_CHECK_DIRECTORY,
                    effective_target_date,
                    exchange_repository,
                    regulation_repository,
                )
                exchange_repository = regulation_repository
            usecase = ScreeningUseCase(
                ranking_repository,
                regulation_repository,
                exchange_repository,
                ScreeningResultRepository(config.SCREENING_RESULT_DIRECTORY),
                notify_result,
                market_regime_usecase=MarketRegimeUseCase(
                    market_data_client=YahooIndexClient(),
                    thresholds=config.MARKET_REGIME_THRESHOLDS,
                    realized_volatility_window=config.MARKET_REGIME_REALIZED_VOL_WINDOW,
                    data_range=config.MARKET_REGIME_DATA_RANGE,
                    fallback_bar_provider=create_index_daily_bar_fallback(),
                    retry_wait_seconds=config.MARKET_REGIME_RETRY_WAIT_SECONDS,
                    adx_threshold=config.MARKET_REGIME_ADX_TREND_THRESHOLD,
                ),
                turnover_client=market_data_client,
            )
            usecase.batch_started = lambda batch, _: register_symbols(token, batch) is not None
            usecase.batch_finished = lambda _, __: unregister_all(token) is not None
            usecase.execute(target_date=effective_target_date)
            if args.target_date is None:
                for price_cap in sorted(config.SCREENING_ALTERNATE_PRICE_CAPS):
                    cap_name = f"{price_cap:g}"
                    alternate_usecase = ScreeningUseCase(
                        ranking_repository,
                        regulation_repository,
                        exchange_repository,
                        ScreeningResultRepository(
                            config.SCREENING_PRICE_BAND_RESULT_ROOT / cap_name
                        ),
                    )
                    alternate_usecase.batch_started = (
                        lambda batch, _: register_symbols(token, batch) is not None
                    )
                    alternate_usecase.batch_finished = lambda _, __: unregister_all(token) is not None
                    try:
                        result = alternate_usecase.execute(
                            target_date=effective_target_date,
                            price_cap=price_cap,
                            keep_unconfirmed=True,
                        )
                        logging.getLogger(__name__).info(
                            "価格帯別スクリーニング完了: 上限=%s円 | 採用=%d件 | 保存先=%s",
                            cap_name,
                            len(result.symbols),
                            config.SCREENING_PRICE_BAND_RESULT_ROOT / cap_name,
                        )
                    except Exception:
                        logging.getLogger(__name__).exception(
                            "価格帯別スクリーニングに失敗しました: 上限=%s円", cap_name
                        )
                    finally:
                        if unregister_all(token) is None:
                            logging.getLogger(__name__).error(
                                "価格帯別スクリーニング後の銘柄登録解除に失敗しました: 上限=%s円",
                                cap_name,
                            )
            if get_token_provider().recovery_failed:
                # 1run1回: トークン再取得後も401が続いた(復旧失敗)場合のみ通知する
                notify_result("kabuステーションAPIの認証が回復しません（トークン再取得後も401が継続しました）。")


def main() -> None:
    _run_screening()


if __name__ == '__main__':
    main()
