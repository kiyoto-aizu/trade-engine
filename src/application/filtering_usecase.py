"""
================================================================================
フィルタリングユースケース
スクリーニング結果から出来高急騰銘柄を抽出するアプリケーションロジック層です。
前日のスクリーニング結果に基づいて、本日の出来高変動を分析します。
================================================================================
"""
from dataclasses import dataclass
from datetime import date, datetime, timedelta
import logging
from time import monotonic, sleep

from src.domain.models import FilteringResult, ScoredCandidate
from src.domain.market_tendency import TendencyPeriod
from src.domain.rules import calculate_volume_surge_ratio, select_top_n_by_surge_ratio
from src.application.market_tendency_notification import build_market_tendency_lines
from src.config import config
from src.infrastructure.calendar.japanese_calendar import is_trading_day
from src.infrastructure.persistence.decision_journal_repository import STAGE_FILTERING
from src.infrastructure.notification.slack_notify import format_result_notification

logger = logging.getLogger(__name__)


class FilteringDeadlineExceeded(RuntimeError):
    """価格帯別フィルタが設定された締め切りまでに完了しませんでした。"""


@dataclass(frozen=True)
class BoardRetryPolicy:
    """売買代金欠損の板を結果保存前に再取得するための設定。打ち切りを超えても結果保存は妨げない。"""

    max_rounds: int
    wait_seconds: float
    deadline_monotonic: float
    deadline_at: datetime | None = None


class FilteringUseCase:
    """
    フィルタリング処理を実行するユースケッククラス。
    
    前日のスクリーニング結果に対して、本日の出来高を確認し、
    出来高急騰銘柄をトップNに絞り込みます。
    """
    
    def __init__(
        self, screening_repository, board_client, volume_client, result_repository, notifier=None,
        decision_journal_repository=None,
        diagnostics_repository=None,
        market_regime_usecase=None,
        sleep_func=None,
        monotonic_clock=None,
    ):
        """
        FilteringUseCaseを初期化します。
        
        Args:
            screening_repository: スクリーニング結果を取得するリポジトリ
            board_client: リアルタイム板情報を取得するクライアント
            volume_client: 過去の出来高データを取得するクライアント
            result_repository: フィルタリング結果を保存するリポジトリ
            notifier: 通知機能（オプション）
        """
        self.screening_repository = screening_repository
        self.board_client = board_client
        self.volume_client = volume_client
        self.result_repository = result_repository
        self.notifier = notifier
        self.decision_journal_repository = decision_journal_repository
        self.diagnostics_repository = diagnostics_repository
        self.market_regime_usecase = market_regime_usecase
        self._sleep = sleep_func
        self._monotonic_clock = monotonic_clock

    def execute(
        self,
        target_date: date | None = None,
        price_cap: float | None = None,
        deadline_monotonic: float | None = None,
        price_band: str | None = None,
        board_retry: BoardRetryPolicy | None = None,
        board_metrics_before: dict | None = None,
        board_run_metrics_before: dict | None = None,
        board_prefetch_summary: dict | None = None,
    ) -> FilteringResult:
        """
        フィルタリング処理を実行します。
        
        処理フロー：
        1. 前営業日のスクリーニング結果を取得
        2. 各銘柄の本日出来高と過去20営業日平均を比較
        3. 出来高急騰率でスコアリング
        4. 上位10銘柄に絞り込み
        5. 結果を保存して返却
        
        Returns:
            FilteringResultオブジェクト
        """
        run_started_at = datetime.now()
        metrics_getter = getattr(self.board_client, "get_metrics_snapshot", None)
        metrics_before = board_metrics_before
        if metrics_before is None and callable(metrics_getter):
            metrics_before = metrics_getter()
        today = target_date or run_started_at.date()
        try:
            price_cap = config.get_screening_price_cap() if price_cap is None else price_cap
        except Exception:
            price_cap = None
        previous_business_day = today - timedelta(days=1)
        # 土日・祝日・年末年始を跨ぐ場合は前営業日に遡る
        while not is_trading_day(previous_business_day):
            previous_business_day -= timedelta(days=1)
        screening = self.screening_repository.load_for_date(previous_business_day)
        scored = []
        candidates = []
        skips: list[tuple[str, str]] = []
        diagnostics = []
        reason_counts: dict[str, int] = {}
        timed_out = False
        next_unprocessed_index = 0

        def mark_skipped(record, diagnostic_reason, journal_reason, already_listed=False):
            record["status"] = "skipped"
            record["reason_code"] = diagnostic_reason
            if not already_listed:
                diagnostics.append(record)
            reason_counts[diagnostic_reason] = reason_counts.get(diagnostic_reason, 0) + 1
            skips.append((record["symbol"], journal_reason))

        retry_active = board_retry is not None and callable(getattr(self.board_client, "refetch_current_board", None))
        retry_pending: list[tuple[dict, str]] = []
        retry_stats = {
            "attempted": 0, "succeeded": 0, "rounds_run": 0, "stopped_by_cutoff": False, "round_success": {},
        }

        def run_board_retry():
            clock = self._monotonic_clock or monotonic
            sleeper = self._sleep or sleep
            handled: set[int] = set()

            def finalize_missing(record):
                handled.add(id(record))
                mark_skipped(
                    record, "FILTER_TURNOVER_MISSING", "FILTER_TURNOVER_MISSING", already_listed=True
                )
                logger.warning(
                    "フィルタリング評価対象外: 銘柄=%s 理由=当日売買代金を計算できません", record["symbol"]
                )

            def attempt(record, symbol, round_no, board_result) -> bool:
                if not record["retry_attempted"]:
                    retry_stats["attempted"] += 1
                record["retry_attempted"] = True
                if isinstance(board_result, Exception):
                    record["retry_error_type"] = type(board_result).__name__
                    board = None
                else:
                    board = board_result
                meta = getattr(self.board_client, "get_retry_meta", lambda _s: None)(symbol)
                if isinstance(meta, dict):
                    record.update(meta)
                if board:
                    record["retry_board_trading_volume"] = board.get("raw_trading_volume", board.get("trading_volume"))
                    record["retry_board_trading_value"] = board.get("raw_trading_value", board.get("trading_value"))
                    record["retry_board_current_price_status"] = board.get("current_price_status")
                    record["retry_board_trading_volume_time"] = board.get("trading_volume_time")
                value = board.get("trading_value") if board else None
                volume = board.get("trading_volume") if board else None
                source = "TradingValue_retry"
                if value is None and board and volume is not None and board.get("current_price") is not None:
                    value = float(board["current_price"]) * float(volume)
                    source = "current_price_x_cumulative_volume_retry"
                if value is None:
                    logger.info("板リトライ失敗: 銘柄=%s ラウンド=%d", symbol, round_no)
                    return False
                logger.info("板リトライ成功: 銘柄=%s ラウンド=%d", symbol, round_no)
                handled.add(id(record))
                retry_stats["succeeded"] += 1
                key = str(round_no)
                retry_stats["round_success"][key] = retry_stats["round_success"].get(key, 0) + 1
                record["retry_succeeded"] = True
                record["retry_round_succeeded"] = round_no
                record["status"] = "evaluated"
                record["reason_code"] = None
                record["numerator_source"] = source
                record["board_current_price"] = board.get("current_price")
                record["numerator"] = float(value)
                try:
                    average = self._load_average(symbol, today, board, record)
                except Exception as exc:
                    record["error_type"] = type(exc).__name__
                    mark_skipped(
                        record, "FILTER_AVERAGE_MISSING", "FILTER_AVERAGE_TURNOVER_MISSING", already_listed=True
                    )
                    logger.exception("平均売買代金の取得に失敗しました: 銘柄=%s", symbol)
                    return True
                if average is None:
                    mark_skipped(
                        record, "FILTER_AVERAGE_MISSING", "FILTER_AVERAGE_TURNOVER_MISSING", already_listed=True
                    )
                    logger.warning("フィルタリング評価対象外: 銘柄=%s 理由=平均売買代金なし", symbol)
                    return True
                record["average_turnover"] = float(average)
                try:
                    surge_ratio = calculate_volume_surge_ratio(float(value), average)
                except ValueError:
                    mark_skipped(
                        record, "FILTER_AVERAGE_NON_POSITIVE", "FILTER_AVERAGE_NON_POSITIVE", already_listed=True
                    )
                    logger.warning("フィルタリング評価対象外: 銘柄=%s 理由=平均出来高が0以下", symbol)
                    return True
                scored.append(ScoredCandidate(symbol, float(value), average, surge_ratio))
                record["ratio"] = surge_ratio
                if value == 0 and volume == 0:
                    record["reason_code"] = "FILTER_NO_TRADES"
                    reason_counts["FILTER_NO_TRADES"] = reason_counts.get("FILTER_NO_TRADES", 0) + 1
                return True

            try:
                unresolved = list(retry_pending)
                logger.info(
                    "板取得リトライ開始: 対象%d件 打ち切り時刻=%s 最大%dラウンド 待機%s秒",
                    len(unresolved), board_retry.deadline_at.isoformat() if board_retry.deadline_at else "-",
                    board_retry.max_rounds, board_retry.wait_seconds,
                )
                for round_no in range(1, board_retry.max_rounds + 1):
                    if not unresolved:
                        break
                    if clock() + board_retry.wait_seconds > board_retry.deadline_monotonic:
                        retry_stats["stopped_by_cutoff"] = True
                        break
                    sleeper(board_retry.wait_seconds)
                    parallel_refetch = getattr(self.board_client, "refetch_current_boards", None)
                    if callable(parallel_refetch):
                        results = parallel_refetch(
                            [symbol for _record, symbol in unresolved],
                            max_workers=config.FILTER_BOARD_MAX_CONCURRENCY,
                            should_start=lambda: clock() < board_retry.deadline_monotonic,
                            retry_round=round_no,
                        )
                    else:
                        results = {}
                        for _record, symbol in unresolved:
                            if clock() >= board_retry.deadline_monotonic:
                                break
                            try:
                                results[symbol] = self.board_client.refetch_current_board(symbol)
                            except Exception as exc:
                                results[symbol] = exc
                    if not results:
                        retry_stats["stopped_by_cutoff"] = True
                        break
                    retry_stats["rounds_run"] = round_no
                    still_missing = []
                    for position, (record, symbol) in enumerate(unresolved):
                        if symbol not in results:
                            retry_stats["stopped_by_cutoff"] = True
                            still_missing.extend(unresolved[position:])
                            break
                        if not attempt(record, symbol, round_no, results[symbol]):
                            still_missing.append((record, symbol))
                    unresolved = still_missing
                    if retry_stats["stopped_by_cutoff"]:
                        break
            except Exception:
                logger.exception("板取得リトライ中に想定外の例外が発生しました。残りは欠損として続行します。")
            for record, _symbol in retry_pending:
                if id(record) not in handled:
                    finalize_missing(record)

        if screening:
            for index, symbol in enumerate(screening.symbols):
                if deadline_monotonic is not None and monotonic() >= deadline_monotonic:
                    timed_out = True
                    next_unprocessed_index = index
                    break
                next_unprocessed_index = index + 1
                record = {
                    "symbol": symbol,
                    "price_band": price_band,
                    "price_cap": price_cap,
                    "numerator": None,
                    "numerator_source": None,
                    "board_current_price": None,
                    "board_retrieved_at": None,
                    "board_fetch_duration_ms": None,
                    "average_turnover": None,
                    "average_days": None,
                    "average_includes_target_date": None,
                    "ratio": None,
                    "rank": None,
                    "selected": False,
                    "reason_code": None,
                    "status": "evaluated",
                }
                if target_date and hasattr(self.volume_client, "get_turnover_for_date"):
                    try:
                        today_value = self.volume_client.get_turnover_for_date(symbol, today)
                        average = self.volume_client.get_average_turnover_before(symbol, today, 20)
                    except Exception as exc:
                        record["error_type"] = type(exc).__name__
                        mark_skipped(record, "FILTER_AVERAGE_MISSING", "FILTER_NO_HISTORICAL_DATA")
                        logger.warning("過去日フィルタデータ取得失敗: 銘柄=%s 理由=%s", symbol, type(exc).__name__)
                        continue
                    if today_value is None or average is None:
                        mark_skipped(record, "FILTER_AVERAGE_MISSING", "FILTER_NO_HISTORICAL_DATA")
                        logger.warning("フィルタリング評価対象外: 銘柄=%s 理由=過去データなし | 日付=%s", symbol, today)
                        continue
                    record["numerator"] = float(today_value)
                    record["numerator_source"] = "yahoo_daily_close_x_volume"
                    record["average_turnover"] = float(average)
                    record["average_includes_target_date"] = False
                    try:
                        surge_ratio = calculate_volume_surge_ratio(float(today_value), average)
                    except ValueError:
                        mark_skipped(record, "FILTER_AVERAGE_NON_POSITIVE", "FILTER_AVERAGE_NON_POSITIVE")
                        logger.warning("フィルタリング評価対象外: 銘柄=%s 理由=平均出来高が0以下", symbol)
                        continue
                    scored.append(ScoredCandidate(symbol, float(today_value), average, surge_ratio))
                    record["ratio"] = surge_ratio
                    diagnostics.append(record)
                    continue

                board_started_at = datetime.now()
                try:
                    board = self.board_client.get_current_board(symbol)
                except Exception as exc:
                    record["board_retrieved_at"] = datetime.now().isoformat()
                    record["board_fetch_duration_ms"] = round(
                        (datetime.now() - board_started_at).total_seconds() * 1000, 3
                    )
                    record["error_type"] = type(exc).__name__
                    self._attach_fetch_meta(record, symbol)
                    mark_skipped(record, "FILTER_BOARD_FETCH_FAILED", "FILTER_BOARD_MISSING")
                    logger.exception("板情報の取得に失敗しました: 銘柄=%s", symbol)
                    continue
                record["board_retrieved_at"] = datetime.now().isoformat()
                record["board_fetch_duration_ms"] = round(
                    (datetime.now() - board_started_at).total_seconds() * 1000, 3
                )
                if not board:
                    self._attach_fetch_meta(record, symbol)
                    self._attach_board_diagnostics(record, {})
                    mark_skipped(record, "FILTER_BOARD_FETCH_FAILED", "FILTER_BOARD_MISSING")
                    logger.warning("フィルタリング評価対象外: 銘柄=%s 理由=板情報なし", symbol)
                    continue
                record["board_current_price"] = board.get("current_price")
                self._attach_fetch_meta(record, symbol)
                today_value = board.get("trading_value")
                volume = board.get("trading_volume")
                if today_value is not None:
                    record["numerator_source"] = "TradingValue"
                elif volume is not None and board.get("current_price") is not None:
                    today_value = float(board["current_price"]) * float(volume)
                    record["numerator_source"] = "current_price_x_cumulative_volume"
                if today_value is None:
                    missing_reason = (
                        "FILTER_CURRENT_PRICE_MISSING"
                        if volume is not None and board.get("current_price") is None
                        else "FILTER_TURNOVER_MISSING"
                    )
                    self._attach_board_diagnostics(record, board)
                    if retry_active and missing_reason == "FILTER_TURNOVER_MISSING":
                        # 全銘柄を一巡した後にまとめて再取得する。欠損の確定とログはその後に行う
                        record.update(retry_attempted=False, retry_succeeded=False, retry_round_succeeded=None)
                        record["status"] = "skipped"
                        record["reason_code"] = missing_reason
                        diagnostics.append(record)
                        retry_pending.append((record, symbol))
                        continue
                    mark_skipped(record, missing_reason, "FILTER_TURNOVER_MISSING")
                    logger.warning("フィルタリング評価対象外: 銘柄=%s 理由=当日売買代金を計算できません", symbol)
                    continue
                record["numerator"] = float(today_value)
                if deadline_monotonic is not None and monotonic() >= deadline_monotonic:
                    record["status"] = "partial"
                    record["reason_code"] = "FILTER_TIME_LIMIT"
                    diagnostics.append(record)
                    reason_counts["FILTER_TIME_LIMIT"] = reason_counts.get("FILTER_TIME_LIMIT", 0) + 1
                    timed_out = True
                    break
                try:
                    average = self._load_average(symbol, today, board, record)
                except Exception as exc:
                    record["error_type"] = type(exc).__name__
                    mark_skipped(record, "FILTER_AVERAGE_MISSING", "FILTER_AVERAGE_TURNOVER_MISSING")
                    logger.exception("平均売買代金の取得に失敗しました: 銘柄=%s", symbol)
                    continue
                if average is None:
                    mark_skipped(record, "FILTER_AVERAGE_MISSING", "FILTER_AVERAGE_TURNOVER_MISSING")
                    logger.warning("フィルタリング評価対象外: 銘柄=%s 理由=平均売買代金なし", symbol)
                    continue
                record["average_turnover"] = float(average)
                try:
                    surge_ratio = calculate_volume_surge_ratio(float(today_value), average)
                except ValueError:
                    mark_skipped(record, "FILTER_AVERAGE_NON_POSITIVE", "FILTER_AVERAGE_NON_POSITIVE")
                    logger.warning("フィルタリング評価対象外: 銘柄=%s 理由=平均出来高が0以下", symbol)
                    continue
                scored.append(ScoredCandidate(symbol, float(today_value), average, surge_ratio))
                record["ratio"] = surge_ratio
                if today_value == 0 and volume == 0:
                    record["reason_code"] = "FILTER_NO_TRADES"
                    reason_counts["FILTER_NO_TRADES"] = reason_counts.get("FILTER_NO_TRADES", 0) + 1
                diagnostics.append(record)
                if deadline_monotonic is not None and monotonic() >= deadline_monotonic:
                    timed_out = True
                    break
            if retry_pending and not timed_out:
                run_board_retry()
            # 同時刻帯の過去分足が取得できないため、絶対倍率の足切りは行わず相対順位で選ぶ。
            candidates = scored
        if deadline_monotonic is not None and monotonic() >= deadline_monotonic:
            timed_out = True
        if timed_out and screening:
            for symbol in screening.symbols[next_unprocessed_index:]:
                diagnostics.append({
                    "symbol": symbol,
                    "price_band": price_band,
                    "price_cap": price_cap,
                    "numerator": None,
                    "numerator_source": None,
                    "board_current_price": None,
                    "board_retrieved_at": None,
                    "board_fetch_duration_ms": None,
                    "average_turnover": None,
                    "average_days": None,
                    "average_includes_target_date": None,
                    "ratio": None,
                    "rank": None,
                    "selected": False,
                    "reason_code": "FILTER_TIME_LIMIT",
                    "status": "not_evaluated",
                })
                reason_counts["FILTER_TIME_LIMIT"] = reason_counts.get("FILTER_TIME_LIMIT", 0) + 1
        symbols = select_top_n_by_surge_ratio(candidates, 10)
        result = FilteringResult(today.isoformat(), symbols, datetime.now().isoformat())
        if not timed_out:
            self.result_repository.save(result)
        ranked = sorted(candidates, key=lambda item: (-item.surge_ratio, item.symbol))
        rank_by_symbol = {candidate.symbol: rank for rank, candidate in enumerate(ranked, start=1)}
        selected_symbols = set(symbols)
        for record in diagnostics:
            if record["status"] == "evaluated":
                record["rank"] = rank_by_symbol.get(record["symbol"])
                record["selected"] = record["symbol"] in selected_symbols
        skipped_count = len(skips)
        summary = {
            "input_count": len(screening.symbols) if screening else 0,
            "evaluated_count": len(scored),
            "skipped_count": skipped_count,
            "selected_count": len(symbols),
            "reason_counts": reason_counts,
            "elapsed_ms": round((datetime.now() - run_started_at).total_seconds() * 1000, 3),
            "board_missing_fetch_seq_distribution": self._missing_seq_distribution(diagnostics),
            "timed_out": timed_out,
            "stop_reason": "FILTER_TIME_LIMIT" if timed_out else None,
            "unprocessed_count": sum(record["status"] == "not_evaluated" for record in diagnostics),
        }
        if callable(metrics_getter):
            metrics_after = metrics_getter()
            def calculate_metrics_delta(before):
                delta = {}
                for key, value in metrics_after.items():
                    before_value = (before or {}).get(key)
                    if isinstance(value, dict):
                        delta[key] = {
                            metric: count - (before_value or {}).get(metric, 0)
                            for metric, count in value.items()
                        }
                    elif key in {"max_concurrency_configured", "max_concurrency_observed"}:
                        delta[key] = value
                    elif isinstance(value, (int, float)):
                        delta[key] = value - (before_value or 0)
                    else:
                        delta[key] = value
                return delta

            summary["board_api_metrics"] = calculate_metrics_delta(metrics_before)
            run_metrics_before = board_run_metrics_before or metrics_before
            if run_metrics_before is not None:
                summary["board_run_api_metrics"] = calculate_metrics_delta(run_metrics_before)
        if board_prefetch_summary is not None:
            summary["board_prefetch"] = board_prefetch_summary
        band_label = price_band or (board_prefetch_summary or {}).get("band")
        fetch_log_getter = getattr(self.board_client, "get_fetch_log", None)
        board_fetches = fetch_log_getter(band_label) if callable(fetch_log_getter) and band_label else []
        if board_fetches:
            summary["board_fetch_stats"] = self._board_fetch_stats(board_fetches)
        if retry_active:
            summary.update({
                "retry_attempted_count": retry_stats["attempted"],
                "retry_succeeded_count": retry_stats["succeeded"],
                "retry_rounds_run": retry_stats["rounds_run"],
                "retry_stopped_by_cutoff": retry_stats["stopped_by_cutoff"],
                "retry_round_success_counts": retry_stats["round_success"],
            })
        logger.info(
            "フィルタリングサマリー: date=%s input=%d evaluated=%d skipped=%d selected=%d reasons=%s elapsed_ms=%.3f",
            today.isoformat(), summary["input_count"], summary["evaluated_count"], skipped_count,
            len(symbols), reason_counts, summary["elapsed_ms"],
        )
        if self.diagnostics_repository is not None:
            try:
                self.diagnostics_repository.save({
                    "date": today.isoformat(),
                    "generated_at": result.generated_at,
                    "price_band": price_band,
                    "price_cap": price_cap,
                    "result_saved": not timed_out,
                    "summary": summary,
                    "board_fetches": board_fetches,
                    "candidates": diagnostics,
                })
            except Exception:
                logger.exception("フィルタリング診断記録の保存に失敗しました。選定結果は保存済みです。")
        if timed_out:
            partial_count = sum(record["status"] == "partial" for record in diagnostics)
            logger.warning(
                "価格帯別フィルタを時間切れで打ち切りました: band=%s evaluated=%d skipped=%d "
                "partial=%d unprocessed=%d time_limit=%d",
                price_band,
                len(scored),
                skipped_count,
                partial_count,
                summary["unprocessed_count"],
                reason_counts.get("FILTER_TIME_LIMIT", 0),
            )
            raise FilteringDeadlineExceeded(f"{price_band or price_cap}円フィルタが時間切れで打ち切られました")
        self._journal_filter_stage(screening, len(scored), skips, symbols, today)
        if self.notifier:
            self._notify_completion(screening, symbols, scored, skipped_count, reason_counts)
        return result

    @staticmethod
    def _board_fetch_stats(fetches: list[dict]) -> dict:
        """帯ごとの板取得所要時間(初回取得のみ)の件数・合計・中央値・最大。"""
        durations = sorted(item["elapsed_ms"] for item in fetches if item.get("kind") == "initial")
        retry_durations = [item["elapsed_ms"] for item in fetches if item.get("kind") == "retry"]
        count = len(durations)
        if count:
            middle = count // 2
            median = durations[middle] if count % 2 else (durations[middle - 1] + durations[middle]) / 2
        else:
            median = None
        return {
            "fetch_count": count,
            "total_ms": round(sum(durations), 3),
            "median_ms": None if median is None else round(median, 3),
            "max_ms": durations[-1] if durations else None,
            "retry_fetch_count": len(retry_durations),
            "retry_total_ms": round(sum(retry_durations), 3),
        }

    def _load_average(self, symbol, today, board, record):
        if hasattr(self.volume_client, "get_average_turnover_details"):
            average_details = self.volume_client.get_average_turnover_details(symbol, 20, today)
            record["average_days"] = average_details["average_days"]
            record["average_includes_target_date"] = average_details["average_includes_target_date"]
            return average_details["average_turnover"]
        if hasattr(self.volume_client, "get_average_turnover"):
            return self.volume_client.get_average_turnover(symbol, 20)
        average_volume = self.volume_client.get_average_volume(symbol, 20)
        return average_volume * float(board["current_price"]) if average_volume is not None else None

    def _attach_fetch_meta(self, record: dict, symbol: str) -> None:
        """実取得順・登録解除からの取得回数を診断に付ける(キャッシュ非対応クライアントでは何もしない)。"""
        getter = getattr(self.board_client, "get_fetch_meta", None)
        if not callable(getter):
            return
        meta = getter(symbol)
        if isinstance(meta, dict):
            record.update(meta)

    @staticmethod
    def _attach_board_diagnostics(record: dict, board: dict) -> None:
        """売買代金が欠損した時だけ、板API返答の生の項目を診断に残す。"""
        keys = board.get("response_keys")
        record["board_response_keys"] = sorted(keys) if keys is not None else None
        record["board_trading_volume"] = board.get("raw_trading_volume", board.get("trading_volume"))
        record["board_trading_value"] = board.get("raw_trading_value", board.get("trading_value"))
        record["board_trading_volume_time"] = board.get("trading_volume_time")
        record["board_current_price_time"] = board.get("current_price_time")
        record["board_current_price_status"] = board.get("current_price_status")
        record["board_vwap"] = board.get("vwap")

    @staticmethod
    def _missing_seq_distribution(diagnostics: list[dict]) -> dict[str, int]:
        """板欠損の実取得順を10件刻みで集計する。"""
        missing_reasons = {"FILTER_TURNOVER_MISSING", "FILTER_CURRENT_PRICE_MISSING", "FILTER_BOARD_FETCH_FAILED"}
        buckets: dict[int, int] = {}
        for record in diagnostics:
            seq = record.get("board_fetch_seq")
            if seq is None or record.get("reason_code") not in missing_reasons:
                continue
            start = (seq - 1) // 10 * 10 + 1
            buckets[start] = buckets.get(start, 0) + 1
        return {f"{start}-{start + 9}": count for start, count in sorted(buckets.items())}

    def _journal_filter_stage(self, screening, evaluated_count, skips, symbols, today) -> None:
        """日ごとの件数と、評価対象外の銘柄×日付×理由を記録する。失敗しても選定は継続する。"""
        if self.decision_journal_repository is None:
            return
        try:
            now = datetime.now()
            reason_counts: dict[str, int] = {}
            for symbol, code in skips:
                reason_counts[code] = reason_counts.get(code, 0) + 1
                self.decision_journal_repository.record(
                    STAGE_FILTERING, symbol, code, now, {"target_date": today.isoformat()}
                )
            self.decision_journal_repository.flush()
            self.decision_journal_repository.record_filter_stage_summary(
                now,
                len(screening.symbols) if screening else 0,
                evaluated_count,
                len(skips),
                len(symbols),
                reason_counts,
            )
        except Exception:
            logger.exception("フィルタリング段の判断記録の保存に失敗しました。")

    def _notify_completion(
        self, screening, symbols, scored, skipped_count: int = 0, reason_counts: dict[str, int] | None = None
    ) -> None:
        selected_symbols = set(symbols)
        activity_ratios = [
            candidate.surge_ratio
            for candidate in scored
            if candidate.symbol in selected_symbols
        ]
        tendency_lines: list[str] = []
        if self.market_regime_usecase is not None:
            try:
                assessment = self.market_regime_usecase.execute()
            except Exception:
                logger.exception("TENDENCY_MARKET_UNAVAILABLE: MarketRegime取得に失敗しました")
                assessment = None
            tendency_lines = build_market_tendency_lines(
                assessment,
                activity_ratios,
                period=TendencyPeriod.PREVIOUS_CLOSE,
                activity_label="通過銘柄の活発度",
                include_market_line=False,
                include_tendency_line=False,
            )
        if not screening:
            message = format_result_notification(
                "銘柄選定",
                "フィルタリング",
                "前日の結果がないため完了しました。",
                ["採用銘柄数: 0件", *tendency_lines],
            )
        else:
            ratios_by_symbol = {candidate.symbol: candidate.surge_ratio for candidate in scored}
            details = [
                f"採用銘柄数: {len(symbols)}件",
                f"入力銘柄数: {len(screening.symbols)}件",
                f"評価完了数: {len(scored)}件",
                f"評価対象外数: {skipped_count}件",
                *tendency_lines,
            ]
            if reason_counts:
                details.append(
                    "評価理由コード件数: "
                    + ", ".join(f"{reason}={count}" for reason, count in sorted(reason_counts.items()))
                )
            if symbols:
                details.extend([
                    "上位銘柄:",
                    *[
                        f"- {symbol}(20日平均売買代金の{ratios_by_symbol[symbol]:.1f}倍)"
                        for symbol in symbols[:5]
                    ],
                ])
            anomaly = self._analyze_anomaly_if_needed(screening, symbols, scored, skipped_count)
            if anomaly:
                details.extend(["LLM異常検知(参考):", anomaly])
            message = format_result_notification(
                "銘柄選定",
                "フィルタリング",
                "フィルタリングが完了しました。",
                details,
            )
        try:
            self.notifier(message)
        except Exception:
            logger.exception("フィルタリング完了通知に失敗しました。")

    def _analyze_anomaly_if_needed(self, screening, symbols, scored, skipped_count: int) -> str | None:
        """採用件数が閾値を下回るなど普段と異なる可能性がある時だけLLMを呼び出し、クレジットを節約する。"""
        candidate_count = len(screening.symbols)
        if candidate_count > 0 and len(symbols) >= config.FILTERING_ANOMALY_MIN_SYMBOLS:
            return None
        try:
            from src.infrastructure.analysis.anomaly_analyzer import create_anomaly_analyzer

            analyzer = create_anomaly_analyzer()
            if not analyzer:
                return None
            summary = {
                "スクリーニング対象件数": candidate_count,
                "出来高条件クリア件数": len(scored),
                "採用件数": len(symbols),
                "評価対象外件数": skipped_count,
            }
            return analyzer.analyze_filtering(summary)
        except Exception:
            logger.exception("フィルタリング異常検知のLLM呼び出しに失敗しました。")
            return None
