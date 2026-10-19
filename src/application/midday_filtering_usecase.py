"""
================================================================================
昼フィルタリングユースケース
450円・900円帯(売買判断に使わない研究用の帯)を、朝の板取得ではなく昼休みにYahoo 1分足から評価します。
09:00〜09:30の「終値×出来高」合計を当日を除く20日平均売買代金で割った推定倍率で順位づけします。
採用ロジック(倍率の計算・上位10件選定)は朝のフィルタリングと同じ domain.rules を使い、値の補正はしません。
270円帯は売買判断に使わず、朝の板の実測倍率との比較(推定/実測)を記録するためだけに同じ方法で推定します。
================================================================================
"""
import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from time import monotonic, sleep

from src.config import config
from src.domain.minute_turnover_estimation import (
    WINDOW_END,
    WINDOW_START,
    aggregate_window,
    compare_with_board,
    is_in_window,
    judge,
)
from src.domain.models import FilteringResult, MinuteBar, ScoredCandidate
from src.domain.rules import calculate_volume_surge_ratio, select_top_n_by_surge_ratio
from src.domain.selection_atr_observation import (
    REASON_TIME_LIMIT as REASON_ATR_TIME_LIMIT,
    build_atr_below_comparison,
    unavailable,
)
from src.infrastructure.calendar.japanese_calendar import is_trading_day
from src.infrastructure.market_data.get_intraday_bars import IntradayFetchResult
from src.infrastructure.notification.slack_notify import format_result_notification

logger = logging.getLogger(__name__)

NUMERATOR_SOURCE = "yahoo_minute_estimate"
REASON_FETCH_FAILED = "MIDDAY_FETCH_FAILED"
REASON_NO_BARS = "MIDDAY_NO_BARS"
REASON_ZERO_VOLUME = "MIDDAY_ZERO_VOLUME"
REASON_TIME_LIMIT = "FILTER_TIME_LIMIT"
REASON_AVERAGE_MISSING = "FILTER_AVERAGE_MISSING"
REASON_AVERAGE_NON_POSITIVE = "FILTER_AVERAGE_NON_POSITIVE"
REASON_LABELS = {
    REASON_FETCH_FAILED: "取得失敗",
    REASON_NO_BARS: "当日の足なし",
    REASON_ZERO_VOLUME: "足はあるが出来高0",
    REASON_TIME_LIMIT: "時間切れ",
    REASON_AVERAGE_MISSING: "20日平均なし",
    REASON_AVERAGE_NON_POSITIVE: "20日平均が0以下",
}


@dataclass(frozen=True)
class MiddayBandSpec:
    """昼に評価する1つの価格帯。comparison_only=Trueの帯は結果ファイルを保存せず、売買判断にも使わない。"""

    name: str
    price_cap: float
    screening_repository: object
    diagnostics_repository: object
    result_repository: object | None = None
    comparison_only: bool = False


@dataclass
class SymbolOutcome:
    symbol: str
    reason_code: str | None = None
    error_type: str | None = None
    window: dict | None = None
    average: float | None = None
    attempts: int = 0
    fetch_ms: float | None = None
    bars: list[MinuteBar] = field(default_factory=list)


@dataclass
class MiddayRunReport:
    skipped_reason: str | None = None
    band_summaries: dict[str, dict] = field(default_factory=dict)
    comparison: dict | None = None
    fetched_count: int = 0
    unique_symbol_count: int = 0
    elapsed_seconds: float = 0.0
    outcomes: dict[str, "SymbolOutcome"] = field(default_factory=dict)


class MiddayFilteringUseCase:
    def __init__(
        self,
        volume_client,
        minute_fetcher,
        *,
        bar_repository=None,
        board_diagnostics_repository=None,
        comparison_repository=None,
        notifier=None,
        fetch_retries: int | None = None,
        retry_wait_seconds: float | None = None,
        skip_dates: tuple[str, ...] | None = None,
        monotonic_clock=None,
        sleep_func=None,
        now=None,
        selection_atr_observer=None,
    ):
        """
        Args:
            volume_client: get_average_turnover_before(symbol, date, days)を持つ日足クライアント
            minute_fetcher: symbol -> IntradayFetchResult (取得失敗と足なしを区別する)
            bar_repository: 09:00〜09:30の分足を保存するParquetリポジトリ(任意)
            board_diagnostics_repository: 朝の板の診断JSON(270円の比較用、任意)
            comparison_repository: 日ごとの比較結果の保存先(任意)
        """
        self.volume_client = volume_client
        self.minute_fetcher = minute_fetcher
        self.bar_repository = bar_repository
        self.board_diagnostics_repository = board_diagnostics_repository
        self.comparison_repository = comparison_repository
        self.notifier = notifier
        self.fetch_retries = config.MIDDAY_FILTERING_FETCH_RETRIES if fetch_retries is None else fetch_retries
        self.retry_wait_seconds = (
            config.MIDDAY_FILTERING_RETRY_WAIT_SECONDS if retry_wait_seconds is None else retry_wait_seconds
        )
        self.skip_dates = config.MIDDAY_FILTERING_SKIP_DATES if skip_dates is None else skip_dates
        self._monotonic = monotonic_clock or monotonic
        self._sleep = sleep_func or sleep
        self._now = now or datetime.now
        self.selection_atr_observer = selection_atr_observer
        self._observation_as_of: date | None = None
        self._observation_deadline = float("inf")

    def run(self, bands: list[MiddayBandSpec], target_date: date, deadline_monotonic: float) -> MiddayRunReport:
        started = self._monotonic()
        today = target_date.isoformat()
        if not is_trading_day(target_date):
            return self._skipped("休場日のため昼フィルタを実行しません。")
        if today in self.skip_dates:
            return self._skipped(f"設定(MIDDAY_FILTERING_SKIP_DATES)により{today}は昼フィルタをスキップします。")

        previous_business_day = target_date - timedelta(days=1)
        while not is_trading_day(previous_business_day):
            previous_business_day -= timedelta(days=1)
        self._observation_as_of = previous_business_day
        self._observation_deadline = deadline_monotonic
        screenings = {
            band.name: band.screening_repository.load_for_date(previous_business_day) for band in bands
        }
        # 売買判断に使う帯を先に取得し、締切に間に合わない場合は比較用の帯が先に切れるようにする
        ordered_bands = sorted(bands, key=lambda band: band.comparison_only)
        symbols: list[str] = []
        for band in ordered_bands:
            screening = screenings[band.name]
            for symbol in (screening.symbols if screening else []):
                if str(symbol) not in symbols:
                    symbols.append(str(symbol))

        outcomes: dict[str, SymbolOutcome] = {}
        for symbol in symbols:
            if self._monotonic() >= deadline_monotonic:
                break
            outcomes[symbol] = self._evaluate_symbol(symbol, today, deadline_monotonic)
        logger.info(
            "昼フィルタの取得完了: 対象%d銘柄 取得済み%d銘柄 未着手%d銘柄",
            len(symbols), len(outcomes), len(symbols) - len(outcomes),
        )

        report = MiddayRunReport(unique_symbol_count=len(symbols), fetched_count=len(outcomes), outcomes=outcomes)
        for band in ordered_bands:
            screening = screenings[band.name]
            report.band_summaries[band.name] = self._evaluate_band(
                band, screening, outcomes, today, started, bool(screening)
            )
            if band.comparison_only:
                report.comparison = report.band_summaries[band.name].get("board_comparison")
        if report.comparison is not None and self.comparison_repository is not None:
            self._save_safely(
                self.comparison_repository, {"date": today, "generated_at": self._now().isoformat(),
                                             "comparison": report.comparison}, "270円比較結果"
            )
        report.elapsed_seconds = self._monotonic() - started
        self._notify(report, ordered_bands, today)
        # 分足の保存は評価・通知の後に行い、失敗や所要時間が結果・締切に影響しないようにする
        self._save_bars(outcomes, target_date)
        return report

    def _skipped(self, reason: str) -> MiddayRunReport:
        logger.info(reason)
        return MiddayRunReport(skipped_reason=reason)

    def _evaluate_symbol(self, symbol: str, today: str, deadline_monotonic: float) -> SymbolOutcome:
        outcome = SymbolOutcome(symbol)
        started = self._monotonic()
        result = None
        while True:
            outcome.attempts += 1
            try:
                result = self.minute_fetcher(symbol)
            except Exception as exc:
                result = IntradayFetchResult(ok=False, error=type(exc).__name__)
            if result.ok or outcome.attempts > self.fetch_retries:
                break
            if self._monotonic() + self.retry_wait_seconds >= deadline_monotonic:
                break
            self._sleep(self.retry_wait_seconds)
        outcome.fetch_ms = round((self._monotonic() - started) * 1000, 3)
        if not result.ok:
            outcome.reason_code = REASON_FETCH_FAILED
            outcome.error_type = result.error
            logger.warning("昼フィルタ評価対象外: 銘柄=%s 理由=分足取得失敗(%s)", symbol, result.error)
            return outcome
        outcome.bars = result.bars
        outcome.window = aggregate_window(result.bars, today)
        verdict = judge(outcome.window)
        if verdict == "NO_BARS":
            outcome.reason_code = REASON_NO_BARS
            return outcome
        if verdict == "NO_VOLUME":
            outcome.reason_code = REASON_ZERO_VOLUME
            return outcome
        try:
            outcome.average = self.volume_client.get_average_turnover_before(symbol, date.fromisoformat(today), 20)
        except Exception as exc:
            outcome.error_type = type(exc).__name__
            outcome.average = None
        if outcome.average is None:
            outcome.reason_code = REASON_AVERAGE_MISSING
        elif outcome.average <= 0:
            outcome.reason_code = REASON_AVERAGE_NON_POSITIVE
        return outcome

    def _evaluate_band(self, band, screening, outcomes, today, started, screening_found) -> dict:
        records: list[dict] = []
        scored: list[ScoredCandidate] = []
        reason_counts: dict[str, int] = {}
        for symbol in (str(s) for s in (screening.symbols if screening else [])):
            record = {
                "symbol": symbol,
                "price_band": band.name,
                "price_cap": band.price_cap,
                "numerator": None,
                "numerator_source": None,
                "board_current_price": None,
                "board_retrieved_at": None,
                "board_fetch_duration_ms": None,
                "average_turnover": None,
                "average_days": None,
                "average_includes_target_date": False,
                "ratio": None,
                "rank": None,
                "selected": False,
                "reason_code": None,
                "status": "evaluated",
                "window_bar_count": None,
                "window_first_bar_time": None,
                "window_last_bar_time": None,
                "window_volume": None,
                "minute_fetch_attempts": None,
                "minute_fetch_duration_ms": None,
            }
            outcome = outcomes.get(symbol)
            if outcome is None:
                record["status"] = "not_evaluated"
                record["reason_code"] = REASON_TIME_LIMIT
            else:
                record["minute_fetch_attempts"] = outcome.attempts
                record["minute_fetch_duration_ms"] = outcome.fetch_ms
                if outcome.error_type:
                    record["error_type"] = outcome.error_type
                if outcome.window:
                    record["window_bar_count"] = outcome.window["bar_count"]
                    record["window_first_bar_time"] = outcome.window["first_bar_time"]
                    record["window_last_bar_time"] = outcome.window["last_bar_time"]
                    record["window_volume"] = outcome.window["volume"]
                    record["numerator"] = outcome.window["value_estimate"]
                    record["numerator_source"] = NUMERATOR_SOURCE
                record["average_turnover"] = outcome.average
                if outcome.reason_code:
                    record["status"] = "skipped"
                    record["reason_code"] = outcome.reason_code
                else:
                    ratio = calculate_volume_surge_ratio(outcome.window["value_estimate"], outcome.average)
                    record["ratio"] = ratio
                    scored.append(ScoredCandidate(
                        symbol, outcome.window["value_estimate"], outcome.average, ratio
                    ))
            if record["reason_code"]:
                reason_counts[record["reason_code"]] = reason_counts.get(record["reason_code"], 0) + 1
            records.append(record)

        selected = select_top_n_by_surge_ratio(scored, 10)
        ranked = sorted(scored, key=lambda item: (-item.surge_ratio, item.symbol))
        rank_by_symbol = {candidate.symbol: rank for rank, candidate in enumerate(ranked, start=1)}
        for record in records:
            if record["status"] == "evaluated":
                record["rank"] = rank_by_symbol.get(record["symbol"])
                record["selected"] = record["symbol"] in set(selected)
        unprocessed = sum(record["status"] == "not_evaluated" for record in records)
        atr_comparison = self._observe_selection_atr(records, scored, selected)
        timed_out = unprocessed > 0
        generated_at = self._now().isoformat()
        result_saved = False
        if band.result_repository is not None and not band.comparison_only and screening_found and not timed_out:
            try:
                band.result_repository.save(FilteringResult(today, selected, generated_at))
                result_saved = True
            except Exception:
                logger.exception("昼フィルタ結果の保存に失敗しました: 帯=%s", band.name)
        summary = {
            "input_count": len(records),
            "evaluated_count": len(scored),
            "skipped_count": sum(record["status"] == "skipped" for record in records) + unprocessed,
            "selected_count": len(selected),
            "reason_counts": reason_counts,
            "elapsed_ms": round((self._monotonic() - started) * 1000, 3),
            "timed_out": timed_out,
            "stop_reason": REASON_TIME_LIMIT if timed_out else None,
            "unprocessed_count": unprocessed,
            "source": NUMERATOR_SOURCE,
            "window": f"{WINDOW_START}-{WINDOW_END}",
            "screening_found": screening_found,
            "comparison_only": band.comparison_only,
            "adopted_symbols": list(selected),
        }
        if band.comparison_only:
            summary["board_comparison"] = self._compare_with_board(today, scored, outcomes)
        if atr_comparison is not None:
            summary["selection_atr_below_comparison"] = atr_comparison
        self._save_safely(band.diagnostics_repository, {
            "date": today,
            "generated_at": generated_at,
            "price_band": band.name,
            "price_cap": band.price_cap,
            "source": NUMERATOR_SOURCE,
            "result_saved": result_saved,
            "summary": summary,
            "candidates": records,
        }, f"昼フィルタ診断(帯={band.name})")
        summary["top"] = [
            (symbol, next(item.surge_ratio for item in scored if item.symbol == symbol)) for symbol in selected[:5]
        ]
        logger.info(
            "昼フィルタ完了: 帯=%s円 入力=%d 評価=%d 採用=%d 理由=%s",
            band.name, len(records), len(scored), len(selected), reason_counts,
        )
        return summary

    def _observe_selection_atr(self, records, scored, selected) -> dict | None:
        """各候補に選定時点(前営業日の確定日足)のATR比を記録し、ATR比2.0未満のみで選んだ場合の比較用銘柄を返す。

        観測のみで、採用銘柄・保存する結果には使わない。失敗・締切超過は理由を記録して続行する。
        """
        if self.selection_atr_observer is None or self._observation_as_of is None:
            return None
        as_of = self._observation_as_of
        ratio_by_symbol: dict[str, float | None] = {}
        for record in records:
            if self._monotonic() >= self._observation_deadline:
                observation = unavailable(as_of, REASON_ATR_TIME_LIMIT)
            else:
                observation = self.selection_atr_observer.observe_safely(record["symbol"], as_of)
            record.update(observation.as_record())
            ratio_by_symbol[record["symbol"]] = observation.ratio
        comparison = build_atr_below_comparison(
            scored, ratio_by_symbol, selected, limit_ratio=config.ATR_DANGER_RATIO
        )
        in_comparison = set(comparison["symbols"])
        for record in records:
            record["comparison_selected_atr_below_danger"] = record["symbol"] in in_comparison
        return comparison

    def _compare_with_board(self, today, scored, outcomes) -> dict:
        board_ratios = self._load_board_ratios(today)
        if not board_ratios:
            return {"available": False, "note": "朝の板の診断JSONが見つからないため比較できません。"}
        estimated = {item.symbol: item.surge_ratio for item in scored}
        yahoo_values = {item.symbol: item.today_volume for item in scored}
        comparison = compare_with_board(estimated, board_ratios, yahoo_values)
        comparison["available"] = True
        return comparison

    def _load_board_ratios(self, today: str) -> dict[str, float]:
        if self.board_diagnostics_repository is None:
            return {}
        try:
            data = self.board_diagnostics_repository.load_latest_for_date(today)
        except Exception:
            logger.exception("朝の板の診断JSONの読み込みに失敗しました。")
            return {}
        if not data:
            return {}
        return {
            str(record["symbol"]): float(record["ratio"])
            for record in data.get("candidates", [])
            if record.get("status") == "evaluated" and record.get("ratio") is not None
        }

    @staticmethod
    def _save_safely(repository, payload: dict, label: str) -> None:
        try:
            repository.save(payload)
        except Exception:
            logger.exception("%sの保存に失敗しました。", label)

    def _save_bars(self, outcomes: dict[str, SymbolOutcome], target_date: date) -> None:
        """09:00〜09:30の足だけをYahooの値で保存する。範囲外の既存の足はそのまま残す。"""
        if self.bar_repository is None:
            return
        day = target_date.isoformat()
        saved = failed = 0
        for symbol, outcome in outcomes.items():
            window_bars = [
                bar for bar in outcome.bars if bar.time[:10] == day and is_in_window(bar.time[11:16])
            ]
            if not window_bars:
                continue
            try:
                existing = [
                    bar for bar in self.bar_repository.load_bars(target_date, symbol)
                    if not (bar.time[:10] == day and is_in_window(bar.time[11:16]))
                ]
                self.bar_repository.replace_bars(target_date, symbol, existing + window_bars)
                saved += 1
            except Exception:
                failed += 1
                logger.exception("昼フィルタの分足保存に失敗しました: 銘柄=%s", symbol)
        logger.info("昼フィルタの分足Parquet保存: 保存=%d銘柄 失敗=%d銘柄", saved, failed)

    def _notify(self, report: MiddayRunReport, bands: list[MiddayBandSpec], today: str) -> None:
        if self.notifier is None:
            return
        details = [
            "値の出所: 推定値(Yahoo分足の09:00〜09:30売買代金 ÷ 当日を除く20日平均)。板の実測値とは異なります。",
            "推定値は板の実測より低めに出る傾向があります(補正はしていません)。",
            f"取得銘柄: {report.fetched_count}/{report.unique_symbol_count}件 経過: {report.elapsed_seconds:.0f}秒",
        ]
        for band in bands:
            summary = report.band_summaries.get(band.name)
            if summary is None:
                continue
            title = f"[{band.name}円帯{'・比較用(売買判断には使いません)' if band.comparison_only else ''}]"
            details.append(title)
            details.append(
                f"入力{summary['input_count']}件 / 評価完了{summary['evaluated_count']}件 / "
                f"評価対象外{summary['skipped_count']}件 / 採用{summary['selected_count']}件"
            )
            if summary["reason_counts"]:
                details.append(
                    "評価対象外の理由: "
                    + ", ".join(
                        f"{REASON_LABELS.get(code, code)}={count}"
                        for code, count in sorted(summary["reason_counts"].items())
                    )
                )
            if not band.comparison_only and summary["top"]:
                details.append("上位銘柄:")
                details.extend(f"- {symbol}(20日平均売買代金の{ratio:.1f}倍・推定)" for symbol, ratio in summary["top"])
        comparison = report.comparison
        if comparison is not None:
            if comparison.get("available") and comparison.get("stats"):
                stats = comparison["stats"]
                details.append(
                    f"[270円帯の推定と朝の実測の比較] 推定/実測の中央値{stats['median']:.2f}"
                    f"(最小{stats['min']:.2f}〜最大{stats['max']:.2f}、{stats['count']}件) "
                    f"上位{comparison['top_n']}の一致{comparison['top_overlap_count']}件"
                )
            else:
                details.append("[270円帯の推定と朝の実測の比較] 比較できる銘柄がありませんでした。")
        message = format_result_notification(
            "銘柄選定", "フィルタリング(昼・推定値)", "昼のフィルタリング(Yahoo分足による推定)が完了しました。", details
        )
        try:
            self.notifier(message)
        except Exception:
            logger.exception("昼フィルタ完了通知に失敗しました。")
