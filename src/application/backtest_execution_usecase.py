"""CLIから受け取った条件でバックテストを実行するユースケース。"""
import dataclasses
import logging
import re
import subprocess
from datetime import date, datetime, timedelta
from enum import Enum
from pathlib import Path

from src.application.backtest_report_usecase import comparison_summary, market_regime_comparison_summary
from src.application.backtest_usecase import simulate_backtest, simulate_timeseries_backtest
from src.config import config
from src.domain.market_regime import calculate_market_regime_series
from src.infrastructure.market_data.yahoo_backtest_history_client import (
    fetch_yahoo_dated_history,
    fetch_yahoo_dated_ohlc,
    fetch_yahoo_history,
)
from src.infrastructure.market_data.yahoo_index_client import YahooIndexClient
from src.infrastructure.persistence.backtest_input_repository import (
    load_daily_filtering_symbols,
    load_history,
    load_symbols,
)
from src.infrastructure.persistence.backtest_input_snapshot import (
    SnapshotError,
    build_minute_manifest,
    collect_filtering_files,
    find_minute_mismatches,
    minute_bar_grid,
    save_backtest_input_snapshot,
)
from src.infrastructure.persistence.filter_decision_repository import FilterDecisionRepository

logger = logging.getLogger(__name__)


def build_backtest_filter_decision_database_path(
    directory: Path, label: str, now: datetime
) -> Path:
    safe_label = re.sub(r"[^A-Za-z0-9_-]", "_", label) or "backtest"
    return directory / f"{safe_label}_{now:%Y%m%d_%H%M%S_%f}.sqlite3"


def _timeseries_kwargs(args, sizing_kwargs, minute_bar_repository, ohlc_history, market_regime_by_date):
    return {
        "starting_cash": args.cash,
        "qty_per_trade": args.qty,
        **sizing_kwargs,
        "fee_rate": args.fee,
        "market_slippage_bps": args.market_slippage_bps,
        "execution_delay_bars": args.execution_delay_bars,
        "order_type": args.order_type,
        "minute_bar_repository": minute_bar_repository,
        "indicator_source": args.indicator_source,
        "close_at_eod": not args.allow_overnight,
        "ohlc_history_by_symbol_date": ohlc_history,
        "market_regime_by_date": market_regime_by_date,
    }


def _fetch_index_bars(args):
    index_client = YahooIndexClient()
    range_ = f"{args.days + 30}d"
    return (
        index_client.get_daily_ohlc("^N225", range_=range_),
        index_client.get_daily_ohlc("^VIX", range_=range_),
    )


def _market_regime_by_date(args, index_bars_sink=None):
    nikkei_bars, vix_bars = _fetch_index_bars(args)
    if index_bars_sink is not None:
        index_bars_sink["^N225"] = nikkei_bars
        index_bars_sink["^VIX"] = vix_bars
    return {
        target_date.isoformat(): regime
        for target_date, regime in calculate_market_regime_series(
            nikkei_bars,
            vix_bars,
            config.MARKET_REGIME_REALIZED_VOL_WINDOW,
            config.MARKET_REGIME_THRESHOLDS,
        ).items()
    }


_SNAPSHOT_CONFIG_NAMES = (
    "BACKTEST_FEE_RATE", "BACKTEST_MARKET_SLIPPAGE_BPS", "BACKTEST_EXECUTION_DELAY_BARS",
    "BACKTEST_ORDER_TYPE", "TARGET_POSITIONS", "MAX_ORDER_AMOUNT_PER_TRADE", "API_SOFT_LIMIT",
    "ORDER_UNIT", "RSI_PERIOD", "RSI_MINIMUM_CLOSES", "RSI_ENTRY_THRESHOLD",
    "RSI_ENTRY_THRESHOLD_CAUTION", "RSI_EXIT_THRESHOLD", "ATR_PERIOD", "ATR_CAUTION_RATIO",
    "ATR_DANGER_RATIO", "ATR_CAUTION_LOT_RATIO", "ATR_DANGER_ACTION",
    "ATR_STOP_NORMAL_MULTIPLIER", "ATR_STOP_CAUTION_MULTIPLIER", "ATR_STOP_DANGER_MULTIPLIER",
    "ATR_PROFIT_LOCK_NORMAL_MULTIPLIER", "ATR_PROFIT_LOCK_CAUTION_MULTIPLIER",
    "ATR_PROFIT_LOCK_DANGER_MULTIPLIER", "ATR_PROFIT_LOCK_TRIGGER_ATR_MULTIPLE",
    "MARKET_REGIME_THRESHOLDS", "MARKET_REGIME_REALIZED_VOL_WINDOW",
    "MARKET_REGIME_ADX_TREND_THRESHOLD", "MARKET_CLOSE_HOUR", "MARKET_CLOSE_MINUTE",
    "FILTER_DECISION_OBSERVATION_DAYS",
)


def to_jsonable(value):
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return to_jsonable(dataclasses.asdict(value))
    if isinstance(value, dict):
        return {str(key): to_jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [to_jsonable(item) for item in value]
    if isinstance(value, Path):
        return str(value.resolve())
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    return value


def serialize_arguments(args) -> dict:
    return {key: to_jsonable(value) for key, value in sorted(vars(args).items())}


def collect_config_values() -> dict:
    """結果に影響するconfig値(旧エンジンが実行中に読むもの)。"""
    return {name: to_jsonable(getattr(config, name, None)) for name in _SNAPSHOT_CONFIG_NAMES}


def _git_state() -> dict:
    root = Path(__file__).resolve().parents[2]
    try:
        commit = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=root, capture_output=True, text=True, timeout=10, check=True,
        ).stdout.strip()
        dirty = bool(subprocess.run(
            ["git", "status", "--porcelain", "--untracked-files=no"],
            cwd=root, capture_output=True, text=True, timeout=10, check=True,
        ).stdout.strip())
        return {"git_commit": commit, "git_dirty": dirty}
    except (OSError, subprocess.SubprocessError):
        return {"git_commit": None, "git_dirty": None}


class _LiveInputs:
    """Yahoo/ローカルファイルから入力を取得する。snapshot_directory指定時は取得物を記録して保存する。"""

    def __init__(self, args, sizing_kwargs, mode, minute_bar_repository, snapshot_directory):
        self.args = args
        self.sizing_kwargs = sizing_kwargs
        self.mode = mode
        self.minute_bar_repository = minute_bar_repository
        self.snapshot_directory = snapshot_directory
        self.recording = snapshot_directory is not None
        self.snapshot_path: str | None = None
        self.filtering_directory = None
        self.symbols = None
        self.fixed_history = None
        self.dated_history_data = None
        self.ohlc_history = None
        self.index_bars: dict | None = None
        self.market_regime = None

    def load_daily_symbols(self, args):
        self.filtering_directory = args.filtering_dir
        return load_daily_filtering_symbols(args.filtering_dir)

    def load_symbols(self, args):
        self.symbols = load_symbols(args.symbols)
        return self.symbols

    def fixed_history_for(self, args, symbols):
        history = load_history(args.history) if args.history.exists() else {}
        if args.live:
            history = fetch_yahoo_history(symbols, days=args.days)
        self.fixed_history = history
        return history

    def dated_history(self, symbols, days):
        self.dated_history_data = fetch_yahoo_dated_history(symbols, days=days)
        return self.dated_history_data

    def dated_ohlc(self, symbols, days):
        self.ohlc_history = fetch_yahoo_dated_ohlc(symbols, days=days)
        return self.ohlc_history

    def market_regime_by_date(self, args):
        if self.recording:
            self.index_bars = {}
            self.market_regime = _market_regime_by_date(args, index_bars_sink=self.index_bars)
        else:
            self.market_regime = _market_regime_by_date(args)
        return self.market_regime

    def checkpoint(self, daily_symbols):
        """全入力がそろった直後(シミュレーション前)に保存する。失敗しても続行する。"""
        if not self.recording:
            return
        try:
            self.snapshot_path = str(self._save(daily_symbols).resolve())
            logger.info("バックテスト入力スナップショット: %s", self.snapshot_path)
        except Exception as exc:
            logger.warning("バックテスト入力スナップショットの保存に失敗しました(バックテストは続行します): %s", exc)

    def _save(self, daily_symbols):
        args = self.args
        now = datetime.now()
        label = Path(args.output).stem if getattr(args, "output", None) else "backtest"
        grid = minute_bar_grid(daily_symbols, self.dated_history_data)
        dates = sorted(daily_symbols)
        symbol_set = set(self.dated_history_data or {}) | {
            symbol for symbols in daily_symbols.values() for symbol in symbols
        } | set(self.symbols or [])
        filtering_files = (
            collect_filtering_files(self.filtering_directory, dates) if self.filtering_directory else []
        )
        return save_backtest_input_snapshot(
            self.snapshot_directory,
            label,
            now,
            run_conditions={
                "mode": self.mode,
                "executed_at": now.isoformat(timespec="seconds"),
                **_git_state(),
                "args": serialize_arguments(args),
                "sizing_kwargs": to_jsonable(self.sizing_kwargs),
                "config": collect_config_values(),
            },
            dated_history=self.dated_history_data,
            fixed_history=self.fixed_history,
            symbols=self.symbols,
            ohlc_history=self.ohlc_history,
            index_bars=self.index_bars,
            market_regime_by_date=self.market_regime if self.index_bars is not None else None,
            filtering_files=filtering_files,
            minute_manifest=build_minute_manifest(self.minute_bar_repository, grid),
            period=(dates[0], dates[-1]) if dates else (None, None),
            symbol_count=len(symbol_set),
        )


class _SnapshotInputs:
    """保存済みスナップショットから入力を返す。ネットワークは使わない。"""

    def __init__(self, snapshot, minute_bar_repository):
        self.snapshot = snapshot
        self.mode = snapshot.mode
        self.minute_bar_repository = minute_bar_repository
        self.snapshot_path = str(snapshot.directory.resolve())
        self._warn_about_changed_conditions()

    def _warn_about_changed_conditions(self):
        saved = self.snapshot.run_conditions
        changed = sorted(
            name for name, value in saved.get("config", {}).items()
            if collect_config_values().get(name) != value
        )
        if changed:
            logger.warning("保存時とconfig値が異なります(現在の値で実行します): %s", ", ".join(changed))
        current_commit = _git_state()["git_commit"]
        if saved.get("git_commit") and current_commit != saved["git_commit"]:
            logger.warning("保存時のgitコミットと異なります: 保存時=%s 現在=%s", saved["git_commit"], current_commit)

    def _required(self, value, name):
        if value is None:
            raise SnapshotError(f"スナップショットに{name}がありません: {self.snapshot.directory}")
        return value

    def load_daily_symbols(self, args):
        directory = self.snapshot.filtering_directory
        return load_daily_filtering_symbols(directory) if directory.exists() else {}

    def load_symbols(self, args):
        return list(self._required(self.snapshot.history.get("symbols"), "symbols"))

    def fixed_history_for(self, args, symbols):
        return self._required(self.snapshot.history.get("fixed_history"), "fixed_history")

    def dated_history(self, symbols, days):
        stored = self._required(self.snapshot.history.get("dated_history"), "dated_history")
        wanted = set(symbols)
        return {symbol: history for symbol, history in stored.items() if symbol in wanted}

    def dated_ohlc(self, symbols, days):
        stored = self._required(self.snapshot.ohlc_history, "ohlc_history(--live保存分)")
        wanted = set(symbols)
        return {symbol: bars for symbol, bars in stored.items() if symbol in wanted}

    def market_regime_by_date(self, args):
        index_daily = self._required(self.snapshot.index_daily, "指数日足/market_regime_by_date")
        return dict(index_daily["market_regime_by_date"])

    def checkpoint(self, daily_symbols):
        grid = minute_bar_grid(daily_symbols, self.snapshot.history.get("dated_history"))
        mismatches = find_minute_mismatches(self.snapshot.minute_manifest, self.minute_bar_repository, grid)
        if any(mismatches.values()):
            logger.warning(
                "分足がスナップショット保存時から変化しています: 変更=%d 欠落=%d 追加=%d (例: %s)",
                len(mismatches["changed"]), len(mismatches["missing"]), len(mismatches["added"]),
                ", ".join((mismatches["changed"] + mismatches["missing"] + mismatches["added"])[:5]),
            )


def _run_timeseries_comparisons(
    args, daily_symbols, dated_history, sizing_kwargs, minute_bar_repository, ohlc_history, result,
):
    atr_comparison = None
    market_regime_comparison = None
    common = _timeseries_kwargs(
        args, sizing_kwargs, minute_bar_repository, ohlc_history, None,
    )
    if args.compare_market_regime:
        baseline = simulate_timeseries_backtest(
            daily_symbols, dated_history, **common, market_regime_enabled=False,
        )
        market_regime_comparison = market_regime_comparison_summary(baseline, result)
    if args.compare_atr:
        baseline = simulate_timeseries_backtest(
            daily_symbols, dated_history, **common, enable_volatility_adjustment=False,
        )
        lot_only = simulate_timeseries_backtest(
            daily_symbols, dated_history, **common,
            enable_volatility_sizing=True, enable_atr_stop_loss=False,
        )
        atr_comparison = {
            "without_atr": comparison_summary(baseline),
            "lot_adjustment_only": comparison_summary(lot_only),
            "lot_adjustment_and_stop": comparison_summary(result),
        }
    return atr_comparison, market_regime_comparison


def _run_filtering_backtest(args, sizing_kwargs, minute_bar_repository, inputs):
    daily_symbols = inputs.load_daily_symbols(args)
    if args.start_date is not None and args.end_date is not None:
        logger.warning("--start-date/--end-date を優先し、--days は無視します。")
        daily_symbols = {
            date_text: symbols_on_day
            for date_text, symbols_on_day in daily_symbols.items()
            if args.start_date <= date.fromisoformat(date_text) <= args.end_date
        }
    elif daily_symbols:
        latest_date = max(date.fromisoformat(date_text) for date_text in daily_symbols)
        cutoff = latest_date - timedelta(days=args.days)
        daily_symbols = {
            date_text: symbols_on_day
            for date_text, symbols_on_day in daily_symbols.items()
            if date.fromisoformat(date_text) >= cutoff
        }
    symbols = sorted({symbol for symbols_on_day in daily_symbols.values() for symbol in symbols_on_day})
    history = inputs.dated_history(symbols, args.days + 5)
    ohlc_history = inputs.dated_ohlc(symbols, args.days + 5) if args.live else None
    market_regime_by_date = inputs.market_regime_by_date(args) if args.compare_market_regime else None
    output_path = getattr(args, "output", None)
    label = Path(output_path).stem if output_path else "backtest"
    filter_decision_database_path = build_backtest_filter_decision_database_path(
        config.BACKTEST_FILTER_DECISION_DIRECTORY, label, datetime.now()
    )
    logger.info("バックテスト判定イベントDB: %s", filter_decision_database_path)
    inputs.checkpoint(daily_symbols)
    result = simulate_timeseries_backtest(
        daily_symbols,
        history,
        **_timeseries_kwargs(
            args,
            sizing_kwargs,
            minute_bar_repository,
            ohlc_history,
            market_regime_by_date,
        ),
        filter_decision_repository=FilterDecisionRepository(
            filter_decision_database_path,
            allowed_execution_modes=frozenset({"backtest"}),
        ),
    )
    atr_comparison, market_regime_comparison = _run_timeseries_comparisons(
        args, daily_symbols, history, sizing_kwargs, minute_bar_repository, ohlc_history, result,
    )
    return result, atr_comparison, market_regime_comparison


def _run_minute_backtest(args, sizing_kwargs, minute_bar_repository, symbols, inputs):
    dated_history = inputs.dated_history(symbols, args.days + 5)
    ohlc_history = inputs.dated_ohlc(symbols, args.days + 5) if args.live else None
    daily_symbols = {
        date_text: symbols
        for date_text in sorted({
            date_text for symbol_history in dated_history.values() for date_text in symbol_history
        })
    }
    market_regime_by_date = inputs.market_regime_by_date(args) if args.compare_market_regime else None
    inputs.checkpoint(daily_symbols)
    result = simulate_timeseries_backtest(
        daily_symbols,
        dated_history,
        **_timeseries_kwargs(
            args,
            sizing_kwargs,
            minute_bar_repository,
            ohlc_history,
            market_regime_by_date,
        ),
    )
    atr_comparison, market_regime_comparison = _run_timeseries_comparisons(
        args, daily_symbols, dated_history, sizing_kwargs, minute_bar_repository, ohlc_history, result,
    )
    return result, atr_comparison, market_regime_comparison


def _run_fixed_backtest(args, symbols, history, inputs):
    ohlc_history = inputs.dated_ohlc(symbols, args.days) if args.live else None
    inputs.checkpoint({})
    result = simulate_backtest(
        symbols,
        history,
        starting_cash=args.cash,
        qty_per_trade=args.qty,
        fee_rate=args.fee,
        market_slippage_bps=args.market_slippage_bps,
        execution_delay_bars=args.execution_delay_bars,
        order_type=args.order_type,
        close_at_eod=not args.allow_overnight,
        ohlc_history_by_symbol={
            symbol: [bar for _, bar in sorted((ohlc_history or {}).get(symbol, {}).items())]
            for symbol in symbols
        } if ohlc_history else None,
    )
    atr_comparison = None
    if args.compare_atr:
        common = {
            "starting_cash": args.cash,
            "qty_per_trade": args.qty,
            "fee_rate": args.fee,
            "market_slippage_bps": args.market_slippage_bps,
            "execution_delay_bars": args.execution_delay_bars,
            "order_type": args.order_type,
            "close_at_eod": not args.allow_overnight,
            "ohlc_history_by_symbol": {
                symbol: [bar for _, bar in sorted((ohlc_history or {}).get(symbol, {}).items())]
                for symbol in symbols
            } if ohlc_history else None,
        }
        baseline = simulate_backtest(symbols, history, **common, enable_volatility_adjustment=False)
        lot_only = simulate_backtest(
            symbols, history, **common,
            enable_volatility_sizing=True, enable_atr_stop_loss=False,
        )
        atr_comparison = {
            "without_atr": comparison_summary(baseline),
            "lot_adjustment_only": comparison_summary(lot_only),
            "lot_adjustment_and_stop": comparison_summary(result),
        }
    return result, atr_comparison, None


def _mode_of(args, minute_bar_repository):
    if args.filtering_dir:
        return "filtering"
    return "minute" if minute_bar_repository is not None else "fixed"


def execute_backtest(
    args, sizing_kwargs, minute_bar_repository, snapshot_directory=None, input_snapshot=None,
):
    """snapshot_directory指定時は入力を保存し、input_snapshot指定時は保存済み入力で再実行する。"""
    if input_snapshot is not None:
        inputs = _SnapshotInputs(input_snapshot, minute_bar_repository)
    else:
        inputs = _LiveInputs(
            args, sizing_kwargs, _mode_of(args, minute_bar_repository),
            minute_bar_repository, snapshot_directory,
        )
    result, atr_comparison, market_regime_comparison = _dispatch_backtest(
        args, sizing_kwargs, minute_bar_repository, inputs,
    )
    if input_snapshot is not None or snapshot_directory is not None:
        result["snapshot_path"] = inputs.snapshot_path
    return result, atr_comparison, market_regime_comparison


def _dispatch_backtest(args, sizing_kwargs, minute_bar_repository, inputs):
    if inputs.mode == "filtering":
        if not args.live:
            raise ValueError("--filtering-dir を使う場合は --live も指定してください")
        return _run_filtering_backtest(args, sizing_kwargs, minute_bar_repository, inputs)

    symbols = inputs.load_symbols(args)
    history = inputs.fixed_history_for(args, symbols) if inputs.mode == "fixed" or args.live else {}
    if inputs.mode == "minute":
        if not args.live:
            raise ValueError("--minute-bars-dir を使う固定銘柄モードでは --live を指定してください")
        return _run_minute_backtest(args, sizing_kwargs, minute_bar_repository, symbols, inputs)
    return _run_fixed_backtest(args, symbols, history, inputs)

