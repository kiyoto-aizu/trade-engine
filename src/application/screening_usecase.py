"""
================================================================================
スクリーニングユースケース
株式銘柄の初期選別を行うアプリケーションロジック層です。
ランキング情報から規制対象外の銘柄を抽出し、スクリーニング結果として出力します。
================================================================================
"""
from datetime import date, datetime
import logging
from time import sleep

from src.config import config
from src.domain.enums import RankingType
from src.domain.models import Regulation, ScreeningAuditEntry, ScreeningResult
from src.domain.market_tendency import TendencyPeriod
from src.domain.rules import (
    exclude_by_regulation,
    filter_candidates_by_price,
    limit_candidates,
    merge_ranking_candidates,
)
from src.application.market_tendency_notification import build_market_tendency_lines
from src.infrastructure.notification.slack_notify import format_result_notification

logger = logging.getLogger(__name__)


class ScreeningUseCase:
    """
    スクリーニング処理を実行するユースケッククラス。
    
    出来高と値上がり率のランキングを結合し、規制情報を確認した上で
    取引対象銘柄のリストを生成します。
    """
    
    def __init__(
        self,
        ranking_repository,
        regulation_repository,
        exchange_repository,
        result_repository,
        notifier=None,
        *,
        market_regime_usecase=None,
        turnover_client=None,
    ):
        """
        ScreeningUseCaseを初期化します。
        
        Args:
            ranking_repository: ランキング情報を取得するリポジトリ
            regulation_repository: 規制情報を取得するリポジトリ
            exchange_repository: 取引所情報を取得するリポジトリ
            result_repository: スクリーニング結果を保存するリポジトリ
            notifier: 通知機能（オプション）
        """
        self.ranking_repository = ranking_repository
        self.regulation_repository = regulation_repository
        self.exchange_repository = exchange_repository
        self.result_repository = result_repository
        self.notifier = notifier
        self.market_regime_usecase = market_regime_usecase
        self.turnover_client = turnover_client
        self.batch_started = None
        self.batch_finished = None

    def execute(
        self,
        target_date=None,
        price_cap: float | None = None,
        keep_unconfirmed: bool = False,
    ) -> ScreeningResult:
        """
        スクリーニング処理を実行します。
        
        処理フロー：
        1. 出来高と値上がり率のランキングを取得
        2. 両ランキングを統合して候補銘柄を作成
        3. 各銘柄の規制情報と取引所情報を確認
        4. 規制対象外の銘柄に絞り込み
        5. 結果を保存して返却
        
        Returns:
            ScreeningResultオブジェクト
            
        Raises:
            RuntimeError: ランキングが空の場合
        """
        turnover = self._collect_ranking(RankingType.TURNOVER, target_date)
        price_gain = self._collect_ranking(RankingType.PRICE_GAIN, target_date)
        if not turnover or not price_gain:
            if self.notifier:
                self.notifier(format_result_notification(
                    "銘柄選定",
                    "スクリーニング",
                    "ランキングがないため中止しました。",
                    ["採用銘柄数: 0件"],
                ))
            raise RuntimeError("ランキングが空のためスクリーニングを中止しました")
        candidates = merge_ranking_candidates(turnover, price_gain, price_gain_weight=config.PRICE_GAIN_WEIGHT)
        turnover_by_symbol = {entry.symbol: entry for entry in turnover}
        price_gain_by_symbol = {entry.symbol: entry for entry in price_gain}
        price_by_symbol = {
            symbol: (
                turnover_by_symbol[symbol].current_price
                if turnover_by_symbol.get(symbol) and turnover_by_symbol[symbol].current_price is not None
                else price_gain_by_symbol[symbol].current_price
                if price_gain_by_symbol.get(symbol)
                else None
            )
            for symbol in candidates
        }
        price_cap = config.get_screening_price_cap() if price_cap is None else price_cap
        price_filter_result = filter_candidates_by_price(candidates, price_by_symbol, price_cap)
        remaining = price_filter_result.remaining
        logger.info(
            "価格上限（%.1f円）により除外: %d件",
            price_cap,
            price_filter_result.excluded_by_price_count,
        )
        if price_filter_result.excluded_missing_price_count:
            logger.info("価格不明により除外: %d件", price_filter_result.excluded_missing_price_count)

        regulations = {}
        check_status_by_symbol = {}
        unconfirmed_symbols = set()
        batch_size = config.SCREENING_BATCH_SIZE
        for batch_start in range(0, len(remaining), batch_size):
            batch = remaining[batch_start:batch_start + batch_size]
            batch_number = batch_start // batch_size + 1
            logger.info("スクリーニングバッチ開始: 番号=%d | 銘柄数=%d", batch_number, len(batch))
            if self.batch_started and not self.batch_started(batch, batch_number):
                raise RuntimeError(f"スクリーニングバッチ{batch_number}の銘柄登録に失敗しました")
            try:
                for symbol in batch:
                    try:
                        exchange = self.exchange_repository.get_primary_exchange(symbol)
                        sleep(config.API_REQUEST_INTERVAL_SECONDS)
                        if exchange is None:
                            status_for = getattr(self.exchange_repository, "status_for", None)
                            status = status_for(symbol) if callable(status_for) else "unconfirmed_market"
                            check_status_by_symbol[symbol] = status
                            unconfirmed_symbols.add(symbol)
                            regulations[symbol] = Regulation(
                                symbol,
                                False if keep_unconfirmed else True,
                                "市場情報未確認" if keep_unconfirmed else "優先市場情報取得失敗",
                                0,
                            )
                            continue
                        if target_date is None:
                            regulation = self.regulation_repository.get_regulation(symbol, exchange)
                        else:
                            regulation = self.regulation_repository.get_regulation(
                                symbol, exchange, target_date=target_date
                            )
                        sleep(config.API_REQUEST_INTERVAL_SECONDS)
                        status_for = getattr(self.exchange_repository, "status_for", None)
                        status = status_for(symbol) if callable(status_for) else "checked"
                        check_failed = status != "checked" or regulation.reason == "規制情報取得失敗"
                        if check_failed:
                            check_status_by_symbol[symbol] = (
                                status if status != "checked" else "unconfirmed_regulation"
                            )
                            unconfirmed_symbols.add(symbol)
                            if keep_unconfirmed:
                                regulations[symbol] = Regulation(symbol, False, "規制情報未確認", 0)
                            else:
                                regulations[symbol] = Regulation(
                                    symbol,
                                    regulation.is_restricted,
                                    regulation.reason,
                                    exchange,
                                )
                            continue
                        regulations[symbol] = Regulation(
                            symbol=symbol,
                            is_restricted=regulation.is_restricted,
                            reason=regulation.reason,
                            primary_exchange=exchange,
                        )
                        check_status_by_symbol[symbol] = "checked"
                    except Exception:
                        logger.exception("%s の規制情報取得中にエラーが発生しました", symbol)
                        unconfirmed_symbols.add(symbol)
                        check_status_by_symbol[symbol] = "unconfirmed"
                        regulations[symbol] = Regulation(
                            symbol,
                            False if keep_unconfirmed else True,
                            "市場・規制情報未確認" if keep_unconfirmed else "規制情報取得時エラー",
                            0,
                        )
                        continue
            finally:
                if self.batch_finished and not self.batch_finished(batch, batch_number):
                    raise RuntimeError(f"スクリーニングバッチ{batch_number}の銘柄解除に失敗しました")
                logger.info("スクリーニングバッチ終了: 番号=%d | 銘柄数=%d", batch_number, len(batch))
        regulations_for_exclusion = regulations
        if keep_unconfirmed and unconfirmed_symbols:
            regulations_for_exclusion = dict(regulations)
            for symbol in unconfirmed_symbols:
                regulations_for_exclusion[symbol] = Regulation(symbol, False, "規制未確認", 1)
        exclusion_result = exclude_by_regulation(remaining, regulations_for_exclusion)
        symbols = limit_candidates(exclusion_result.remaining)
        selected_symbols = set(symbols)
        default_turnover_rank = len(turnover) + 1
        default_price_gain_rank = len(price_gain) + 1
        audit_entries = []
        price_filtered_symbols = set(candidates) - set(remaining)
        for symbol in candidates:
            turnover_entry = turnover_by_symbol.get(symbol)
            price_gain_entry = price_gain_by_symbol.get(symbol)
            turnover_rank = turnover_entry.rank if turnover_entry else default_turnover_rank
            price_gain_rank = price_gain_entry.rank if price_gain_entry else default_price_gain_rank
            regulation = regulations.get(symbol)
            if symbol in price_filtered_symbols:
                price = price_by_symbol[symbol]
                restriction_reason = (
                    "価格不明"
                    if price is None or price <= 0
                    else f"価格上限超過（{price_cap:.1f}円）"
                )
                primary_exchange = 0
                is_restricted = False
            else:
                restriction_reason = regulation.reason
                primary_exchange = regulation.primary_exchange
                is_restricted = regulation.is_restricted
            audit_entries.append(ScreeningAuditEntry(
                symbol=symbol,
                turnover_rank=turnover_rank,
                turnover_value=turnover_entry.value if turnover_entry else 0.0,
                price_gain_rank=price_gain_rank,
                price_gain_value=price_gain_entry.value if price_gain_entry else 0.0,
                total_rank=turnover_rank + price_gain_rank,
                primary_exchange=primary_exchange,
                is_restricted=is_restricted,
                restriction_reason=restriction_reason,
                selected=symbol in selected_symbols,
                price=price_by_symbol[symbol],
                check_status=(
                    "not_checked_price"
                    if symbol in price_filtered_symbols
                    else check_status_by_symbol.get(symbol, "unconfirmed")
                ),
            ))
        result_date = target_date.isoformat() if target_date else datetime.now().date().isoformat()
        result = ScreeningResult(result_date, symbols, datetime.now().isoformat(), audit_entries)
        self.result_repository.save(result)
        if self.notifier:
            self._notify_completion(
                candidates,
                price_filter_result,
                exclusion_result,
                symbols,
                turnover_by_symbol,
                price_gain_by_symbol,
                date.fromisoformat(result_date),
            )
        return result

    def _collect_ranking(self, ranking_type: RankingType, target_date=None):
        """
        市場区分ごとにランキングを取得して結合します。

        全市場(ALL)一括取得だと上位50件が値がさ株に占められやすいため、
        config.SCREENING_EXCHANGE_DIVISIONSで指定した市場区分ごとに個別取得し、
        母集団を拡大します（銘柄が重複した場合は順位の良い方を採用）。
        """
        divisions = config.SCREENING_EXCHANGE_DIVISIONS or ["ALL"]
        by_symbol = {}
        for division in divisions:
            if target_date is None:
                entries = self.ranking_repository.get_ranking(ranking_type, exchange_division=division)
            else:
                entries = self.ranking_repository.get_ranking(
                    ranking_type, exchange_division=division, target_date=target_date
                )
            for entry in entries:
                existing = by_symbol.get(entry.symbol)
                if existing is None or entry.rank < existing.rank:
                    by_symbol[entry.symbol] = entry
        return list(by_symbol.values())

    def _notify_completion(
        self,
        candidates,
        price_filter_result,
        exclusion_result,
        symbols,
        turnover_by_symbol,
        price_gain_by_symbol,
        target_date,
    ) -> None:
        details = [
            f"採用銘柄数: {len(symbols)}件",
            f"候補数: {len(candidates)}件",
            f"価格上限除外数: {price_filter_result.excluded_by_price_count}件",
            f"価格不明除外数: {price_filter_result.excluded_missing_price_count}件",
            f"規制除外数: {exclusion_result.excluded_by_regulation_count}件",
            f"地方取引所除外数: {exclusion_result.excluded_by_exchange_count}件",
        ]
        activity_ratios = self._selected_turnover_ratios(
            symbols, turnover_by_symbol, target_date
        )
        if self.market_regime_usecase is not None:
            try:
                assessment = self.market_regime_usecase.execute()
            except Exception:
                logger.exception("TENDENCY_MARKET_UNAVAILABLE: MarketRegime取得に失敗しました")
                assessment = None
            details.extend(build_market_tendency_lines(
                assessment,
                activity_ratios,
                period=TendencyPeriod.PREVIOUS_CLOSE,
                activity_label="候補の活発度",
                include_market_line=False,
                include_tendency_line=False,
            ))
        top_entries = []
        for symbol in exclusion_result.remaining[:3]:
            turnover_entry = turnover_by_symbol.get(symbol)
            price_gain_entry = price_gain_by_symbol.get(symbol)
            price_gain = (
                f"+{price_gain_entry.value:,.2f}%"
                if price_gain_entry else "-"
            )
            turnover = (
                f"{turnover_entry.value / 100_000_000:,.2f}億円"
                if turnover_entry else "-"
            )
            top_entries.append(
                f"{symbol}(値上がり率 {price_gain}, 売買代金 {turnover})"
            )
        if top_entries:
            details.extend(["上位銘柄:", *[f"- {entry}" for entry in top_entries]])
        anomaly = self._analyze_anomaly_if_needed(
            candidates, price_filter_result, exclusion_result, symbols
        )
        if anomaly:
            details.extend(["LLM異常検知(参考):", anomaly])
        message = format_result_notification(
            "銘柄選定",
            "スクリーニング",
            "スクリーニングが完了しました。",
            details,
        )
        try:
            self.notifier(message)
        except Exception:
            logger.exception("スクリーニング完了通知に失敗しました。")

    def _selected_turnover_ratios(self, symbols, turnover_by_symbol, target_date):
        if self.turnover_client is None:
            return [None for _ in symbols]
        ratios = []
        for symbol in symbols:
            entry = turnover_by_symbol.get(symbol)
            try:
                average = self.turnover_client.get_average_turnover_before(symbol, target_date, 20)
                ratio = (
                    entry.value / average
                    if entry is not None and average is not None and average > 0
                    else None
                )
            except Exception as exc:
                logger.warning(
                    "TENDENCY_ACTIVITY_UNAVAILABLE: 銘柄=%s | 理由=%s",
                    symbol,
                    type(exc).__name__,
                )
                ratio = None
            ratios.append(ratio)
        return ratios

    def _analyze_anomaly_if_needed(self, candidates, price_filter_result, exclusion_result, symbols) -> str | None:
        """採用件数が閾値を下回るなど普段と異なる可能性がある時だけLLMを呼び出し、クレジットを節約する。"""
        if len(symbols) >= config.SCREENING_ANOMALY_MIN_SYMBOLS:
            return None
        try:
            from src.infrastructure.analysis.anomaly_analyzer import create_anomaly_analyzer

            analyzer = create_anomaly_analyzer()
            if not analyzer:
                return None
            summary = {
                "候補件数": len(candidates),
                "価格上限で除外した件数": price_filter_result.excluded_by_price_count,
                "価格不明で除外した件数": price_filter_result.excluded_missing_price_count,
                "規制で除外した件数": exclusion_result.excluded_by_regulation_count,
                "地方取引所で除外した件数": exclusion_result.excluded_by_exchange_count,
                "採用件数": len(symbols),
            }
            return analyzer.analyze_screening(summary)
        except Exception:
            logger.exception("スクリーニング異常検知のLLM呼び出しに失敗しました。")
            return None
