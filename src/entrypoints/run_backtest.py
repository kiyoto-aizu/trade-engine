import argparse
import json
import logging
from datetime import date, datetime, timedelta
from pathlib import Path

from src.application.backtest_execution_usecase import execute_backtest, to_jsonable
from src.application.backtest_report_usecase import save_backtest_result
from src.config import config
from src.infrastructure.persistence.backtest_input_snapshot import load_backtest_input_snapshot
from src.infrastructure.analysis.daily_analyzer import create_daily_analyzer
from src.infrastructure.notification.slack_notify import (
    format_result_notification,
    notify_analysis,
    process_notification,
)
from src.infrastructure.logging_config import configure_logging
from src.infrastructure.persistence.parquet_minute_bar_repository import ParquetMinuteBarRepository

logger = logging.getLogger(__name__)


def _validate_backtest_arguments(args: argparse.Namespace) -> None:
    if (args.start_date is None) != (args.end_date is None):
        raise ValueError("--start-date と --end-date は両方指定してください")
    if args.fixed_qty and (
        args.target_positions is not None or args.max_order_amount is not None
    ):
        raise ValueError(
            "--fixed-qty と --target-positions/--max-order-amount は同時指定できません"
        )


def _build_sizing_kwargs(args: argparse.Namespace) -> dict[str, object]:
    _validate_backtest_arguments(args)
    if args.production_sizing:
        logger.warning("--production-sizing は非推奨です。指定しても挙動は変わりません。")

    if args.fixed_qty:
        target_positions = None
        max_order_amount_per_trade = None
    else:
        target_positions = (
            args.target_positions
            if args.target_positions is not None
            else config.TARGET_POSITIONS
        )
        max_order_amount_per_trade = (
            args.max_order_amount
            if args.max_order_amount is not None
            else config.MAX_ORDER_AMOUNT_PER_TRADE
        )
    return {
        "target_positions": target_positions,
        "max_order_amount_per_trade": max_order_amount_per_trade,
        "api_soft_limit": config.API_SOFT_LIMIT if target_positions is not None else None,
    }


def _filter_daily_symbols(
    daily_symbols: dict[str, list[str]],
    start_date: date | None,
    end_date: date | None,
    days: int,
) -> dict[str, list[str]]:
    if start_date is not None and end_date is not None:
        logger.warning("--start-date/--end-date を優先し、--days は無視します。")
        return {
            date_text: symbols_on_day
            for date_text, symbols_on_day in daily_symbols.items()
            if start_date <= date.fromisoformat(date_text) <= end_date
        }
    if daily_symbols:
        latest_date = max(date.fromisoformat(date_text) for date_text in daily_symbols)
        cutoff = latest_date - timedelta(days=days)
        return {
            date_text: symbols_on_day
            for date_text, symbols_on_day in daily_symbols.items()
            if date.fromisoformat(date_text) >= cutoff
        }
    return daily_symbols


def _build_parser(repo_root: Path, suppress_defaults: bool = False) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="バックテストを実行します")
    parser.add_argument("--symbols", type=Path, default=repo_root / "data" / "filtering" / "2026-09-01.json", help="銘柄一覧のJSONファイル")
    parser.add_argument("--filtering-dir", type=Path, default=None, help="日付別フィルタリング結果のディレクトリ")
    parser.add_argument("--history", type=Path, default=repo_root / "data" / "backtest" / "fixtures" / "sample_history.json", help="銘柄ごとの終値履歴JSONファイル")
    parser.add_argument("--cash", type=float, default=100000.0, help="開始現金")
    parser.add_argument("--qty", type=int, default=100, help="1回の売買数量（--fixed-qty指定時のみ使用）")
    parser.add_argument("--production-sizing", action="store_true", help="非推奨。指定しても挙動は変わらず、本番相当サイジングがデフォルトで適用される")
    parser.add_argument("--fixed-qty", action="store_true", help="固定数量モードを使用する（--qtyを使用）")
    parser.add_argument("--target-positions", type=int, default=None, help="同時保有銘柄数の上限（既定: config.TARGET_POSITIONS）")
    parser.add_argument("--max-order-amount", type=float, default=None, help="1回あたりの発注上限額（既定: config.MAX_ORDER_AMOUNT_PER_TRADE）")
    parser.add_argument("--fee", type=float, default=config.BACKTEST_FEE_RATE, help="片道手数料率 (例: 0.001 = 0.1%%)")
    parser.add_argument("--market-slippage-bps", type=float, default=config.BACKTEST_MARKET_SLIPPAGE_BPS, help="成行の片道スリッページ（bps、買いは加算・売りは減算）")
    parser.add_argument("--execution-delay-bars", type=int, default=config.BACKTEST_EXECUTION_DELAY_BARS, help="シグナルから想定約定までの遅延バー数")
    parser.add_argument("--order-type", choices=("market", "limit"), default=config.BACKTEST_ORDER_TYPE, help="想定注文種別（market: 成行、limit: シグナル価格の指値）")
    parser.add_argument("--live", action="store_true", help="Yahoo Finance から実データを取得してバックテストを実行")
    parser.add_argument("--days", type=int, default=730, help="Yahoo Finance から取得する日数（既定: 約2年）")
    parser.add_argument("--start-date", type=date.fromisoformat, default=None, help="評価開始日（YYYY-MM-DD。--end-dateと同時指定）")
    parser.add_argument("--end-date", type=date.fromisoformat, default=None, help="評価終了日（YYYY-MM-DD。--start-dateと同時指定）")
    parser.add_argument("--allow-overnight", action="store_true", help="持ち越しを許可し、当日終値での強制決済を無効にする")
    parser.add_argument("--minute-bars-dir", type=Path, default=None, help="分足データディレクトリ。指定時は分足ごとに判定・約定を再生します")
    parser.add_argument("--indicator-source", choices=("daily", "minute"), default="daily", help="SMA5/RSIの算出元（既定: daily）")
    parser.add_argument("--output", type=Path, default=None, help="結果JSONの保存先")
    parser.add_argument("--archive-directory", type=Path, default=None, help="日時付き結果の保存先")
    parser.add_argument("--compare-atr", action="store_true", help="ATRなし・ロット調整のみ・ATRありの比較を追加する（--liveが必要。ATRはライブ実行時に標準適用）")
    parser.add_argument("--compare-market-regime", action="store_true", help="MarketRegime導入前後を比較する（--liveの日付付きバックテストが必要）")
    parser.add_argument("--input-snapshot", type=Path, default=None, help="保存済みの入力スナップショットで再実行する（Yahooを使わない。引数は保存値が既定で、明示した引数だけ上書き）")
    if suppress_defaults:
        for action in parser._actions:
            action.default = argparse.SUPPRESS
    return parser


_SNAPSHOT_NOT_RESTORED = frozenset({"input_snapshot", "output", "archive_directory"})


def _apply_input_snapshot(
    repo_root: Path, args: argparse.Namespace, snapshot, argv: list[str] | None = None,
) -> tuple[argparse.Namespace, dict]:
    """保存した引数を既定にし、コマンドラインで明示した引数だけを上書きする。"""
    explicit = vars(_build_parser(repo_root, suppress_defaults=True).parse_args(argv))
    actions = {action.dest: action for action in _build_parser(repo_root)._actions}
    saved = snapshot.run_conditions["args"]
    merged = vars(args).copy()
    for dest, value in saved.items():
        if dest in _SNAPSHOT_NOT_RESTORED or dest in explicit or dest not in actions:
            continue
        action_type = actions[dest].type
        merged[dest] = action_type(value) if action_type is not None and value is not None else value
    overrides = {}
    for dest, value in explicit.items():
        if dest in _SNAPSHOT_NOT_RESTORED:
            continue
        override = to_jsonable(value)
        if override != saved.get(dest):
            overrides[dest] = {"snapshot": saved.get(dest), "override": override}
    return argparse.Namespace(**merged), overrides


def _display_result(result: dict, atr_comparison: dict | None, market_regime_comparison: dict | None) -> dict:
    display_result = {
        "総損益": result.get("総損益", result.get("total_pnl", 0.0)),
        "勝率": result.get("勝率", result.get("win_rate", 0.0)),
        "利益因子": result.get("利益因子", result.get("profit_factor", 0.0)),
        "最大ドローダウン": result.get("最大ドローダウン", result.get("max_drawdown", 0.0)),
        "総取引数": result.get("総取引数", result.get("total_trades", 0)),
        "現金残高": result.get("現金残高", result.get("cash", 0.0)),
        "最終保有数": result.get("最終保有数", result.get("final_position", 0)),
        "取引履歴": result.get("取引履歴", result.get("trade_history", [])),
        "銘柄別要約": result.get("銘柄別要約", result.get("summary_by_symbol", [])),
        "日別要約": result.get("日別要約", result.get("daily_summary", [])),
        "保有期間別要約": result.get("保有期間別要約", result.get("holding_bucket_summary", [])),
        "対象期間": f"{result['period_start']} - {result['period_end']}" if result.get("period_start") else None,
    }
    if atr_comparison:
        display_result["ATR比較"] = atr_comparison
    if market_regime_comparison:
        display_result["MarketRegime比較"] = market_regime_comparison
    return display_result


def main() -> None:
    configure_logging()
    with process_notification("バックテスト", notify_lifecycle=False, trigger="手動実行"):
        repo_root = Path(__file__).resolve().parents[2]
        args = _build_parser(repo_root).parse_args()
        input_snapshot = None
        input_overrides: dict = {}
        if args.input_snapshot is not None:
            input_snapshot = load_backtest_input_snapshot(args.input_snapshot)
            args, input_overrides = _apply_input_snapshot(repo_root, args, input_snapshot)
            if input_overrides:
                logger.warning("スナップショットの値を上書きして実行します: %s", input_overrides)
        _validate_backtest_arguments(args)
        sizing_kwargs = _build_sizing_kwargs(args)
        if (sizing_kwargs["target_positions"] is not None or sizing_kwargs["max_order_amount_per_trade"] is not None) and not args.filtering_dir:
            logger.warning("--fixed-qty/--target-positions/--max-order-amount は --filtering-dirでのみ有効です。固定銘柄モードでは無視されます。")
        if args.compare_atr and not args.live:
            raise ValueError("--compare-atrを使う場合は--liveを指定してください")
        if args.compare_market_regime and not args.live:
            raise ValueError("--compare-market-regimeを使う場合は--liveを指定してください")
        minute_bar_repository = ParquetMinuteBarRepository(args.minute_bars_dir) if args.minute_bars_dir else None
        if args.indicator_source == "minute" and minute_bar_repository is None:
            raise ValueError("--indicator-source minuteを使う場合は--minute-bars-dirを指定してください")
        result, atr_comparison, market_regime_comparison = execute_backtest(
            args, sizing_kwargs, minute_bar_repository,
            snapshot_directory=None if input_snapshot else config.BACKTEST_INPUT_SNAPSHOT_DIRECTORY,
            input_snapshot=input_snapshot,
        )
        if input_overrides:
            result["input_snapshot_overrides"] = input_overrides

        display_result = _display_result(result, atr_comparison, market_regime_comparison)
        analyzer = create_daily_analyzer()
        llm_analysis = analyzer.analyze_backtest(display_result) if analyzer else None
        result["generated_at"] = datetime.now().isoformat(timespec="seconds")
        if atr_comparison:
            result["atr_comparison"] = atr_comparison
        if market_regime_comparison:
            result["market_regime_comparison"] = market_regime_comparison
        if llm_analysis:
            result["llm_analysis"] = llm_analysis

        archive_path = (
            save_backtest_result(args.output, result, args.archive_directory)
            if args.output else None
        )
        print(json.dumps(display_result, ensure_ascii=False, indent=2))
        report_lines = [
            f"対象期間: {display_result['対象期間'] or '指定なし'}",
            f"総損益: {display_result['総損益']}",
            f"勝率: {display_result['勝率']}",
            f"総取引数: {display_result['総取引数']}",
            f"最大ドローダウン: {display_result['最大ドローダウン']}",
            f"最終保有数: {display_result['最終保有数']}",
        ]
        daily_summary = display_result["日別要約"]
        if daily_summary:
            profitable_days = sum(1 for entry in daily_summary if entry.get("total_realized_pnl", 0) > 0)
            losing_days = sum(1 for entry in daily_summary if entry.get("total_realized_pnl", 0) < 0)
            best_day = max(daily_summary, key=lambda entry: entry.get("total_realized_pnl", 0))
            worst_day = min(daily_summary, key=lambda entry: entry.get("total_realized_pnl", 0))
            report_lines.extend([
                f"日別決済損益: 利益日 {profitable_days}日 / 損失日 {losing_days}日",
                f"最大利益日: {best_day.get('date', '-')} ({best_day.get('total_realized_pnl', 0)})",
                f"最大損失日: {worst_day.get('date', '-')} ({worst_day.get('total_realized_pnl', 0)})",
            ])
        if args.output:
            report_lines.append(f"詳細: {args.output}")
        if archive_path:
            report_lines.append(f"履歴: {archive_path}")
        if llm_analysis:
            report_lines.extend(["LLMバックテスト評価(参考):", llm_analysis])
        notify_analysis(format_result_notification("分析運用", "バックテスト", "バックテストが完了しました。", report_lines))


if __name__ == "__main__":
    main()
