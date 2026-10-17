import json
import logging
from datetime import date, datetime
from pathlib import Path

from src.application.error_summary import normalize_daily_errors
from src.infrastructure.calendar.japanese_calendar import is_trading_day

logger = logging.getLogger(__name__)


def build_period_metadata(start: date, end: date, as_of: date) -> dict:
    if as_of < start:
        status = "not_started"
    elif as_of < end:
        status = "in_progress"
    else:
        status = "complete"
    return {
        "start": start.isoformat(),
        "end": end.isoformat(),
        "as_of": as_of.isoformat(),
        "status": status,
        "is_complete": status == "complete",
    }


def summarize_daily_operations(summaries: list[dict]) -> dict:
    status_counts: dict[str, int] = {}
    log_error_days = 0
    log_error_count = 0
    log_error_unrecorded_days = 0
    emergency_stop_days = 0
    timeout = {
        "count": 0,
        "recovered": 0,
        "final_failure": 0,
        "unrecorded": 0,
        "symbols": {"recovered": set(), "final_failure": set(), "unrecorded": set()},
    }
    other_count = 0
    other_groups: dict[str, int] = {}
    for item in summaries:
        status = str(item.get("market_assessment_status") or "not_recorded")
        status_counts[status] = status_counts.get(status, 0) + 1
        current_error_count = int(item.get("log_error_count") or 0)
        errors = normalize_daily_errors(item.get("log_errors"))
        if not errors["recorded"]:
            log_error_unrecorded_days += 1
        if current_error_count > 0:
            log_error_days += 1
            log_error_count += current_error_count
        daily_timeout = errors["board_timeout"]
        for name in ("count", "recovered", "final_failure", "unrecorded"):
            timeout[name] += daily_timeout[name]
        for name, symbols in daily_timeout["symbols"].items():
            timeout["symbols"][name].update(symbols)
        daily_other = errors["other_errors"]
        other_count += daily_other["count"]
        for group in daily_other["groups"]:
            title = group["title"]
            other_groups[title] = other_groups.get(title, 0) + group["count"]
        if item.get("emergency_stop_triggered"):
            emergency_stop_days += 1
    market_status_counts = {
        {
            "available": "判定あり",
            "available_fallback": "判定あり(補完)",
            "unavailable": "判定不可",
            "not_evaluated": "未判定",
            "not_recorded": "未記録",
        }.get(status, "未記録"): count
        for status, count in status_counts.items()
    }
    return {
        "log_error_days": log_error_days,
        "log_error_count": log_error_count,
        "log_error_unrecorded_days": log_error_unrecorded_days,
        "emergency_stop_days": emergency_stop_days,
        "market_assessment_status_counts": status_counts,
        "market_status_counts": market_status_counts,
        "board_timeout": {
            **{key: value for key, value in timeout.items() if key != "symbols"},
            "symbols": {
                name: sorted(symbols) for name, symbols in timeout["symbols"].items()
            },
        },
        "other_errors": {
            "count": other_count,
            "groups": [
                {"title": title, "count": count}
                for title, count in sorted(other_groups.items())
            ],
        },
    }


def summarize_daily_reports(summaries: list[dict]) -> dict:
    return {
        "report_count": len(summaries),
        "order_count": sum(int(item["order_count"] or 0) for item in summaries),
        "total_profit_loss": round(
            sum(float(item["total_profit_loss"] or 0) for item in summaries), 2
        ),
        "kill_switch_days": sum(1 for item in summaries if item["kill_switch_triggered"]),
        "emergency_stop_days": sum(1 for item in summaries if item["emergency_stop_triggered"]),
        "trading_modes": sorted(
            {str(item["trading_mode"]) for item in summaries if item.get("trading_mode")}
        ),
        "operational_summary": summarize_daily_operations(summaries),
        "reports": summaries,
    }


def build_daily_period_comparison(
    current_period: dict,
    current_summary: dict,
    previous_period: dict,
    previous_summary: dict,
) -> dict:
    current_modes = current_summary["trading_modes"]
    previous_modes = previous_summary["trading_modes"]
    same_single_mode = (
        len(current_modes) == 1
        and current_modes == previous_modes
    )
    if not current_period["is_complete"]:
        available = False
        reason = "対象期間が未完了です"
    elif not current_summary["report_count"] or not previous_summary["report_count"]:
        available = False
        reason = "比較対象の日次レポートが不足しています"
    elif not same_single_mode:
        available = False
        reason = "両期間の取引モードが一致しません"
    else:
        available = True
        reason = "同一取引モードの日次レポート集計です"

    def values(period: dict, summary: dict) -> dict:
        return {
            "period": period,
            "report_count": summary["report_count"],
            "order_count": summary["order_count"],
            "total_profit_loss": summary["total_profit_loss"],
            "trading_modes": summary["trading_modes"],
            "operational_summary": summary["operational_summary"],
        }

    return {
        "available": available,
        "basis": "同一取引モードの日次レポート集計" if available else None,
        "reason": reason,
        "current": values(current_period, current_summary),
        "previous": values(previous_period, previous_summary),
    }


def summarize_backtest_runs(backtests: list[dict], start: date, end: date) -> dict:
    matching_runs = [
        item
        for item in backtests
        if _parse_date(item.get("period_start")) == start and _parse_date(item.get("period_end")) == end
    ]
    if not backtests:
        status = "no_runs"
    elif len(matching_runs) == 1:
        status = "single_exact_period_run"
    elif matching_runs:
        status = "multiple_exact_period_runs"
    else:
        status = "no_exact_period_run"

    matching_run = matching_runs[0] if len(matching_runs) == 1 else None
    return {
        "run_count": len(backtests),
        "matching_period_run_count": len(matching_runs),
        "summary_status": status,
        "exact_period_run_available": matching_run is not None,
        "total_pnl": matching_run["total_pnl"] if matching_run else None,
        "total_trades": matching_run["total_trades"] if matching_run else None,
        "runs": backtests,
    }


def load_daily_summaries(report_directory: Path, start: date, end: date) -> list[dict]:
    summaries = []
    for path in sorted(report_directory.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("日次レポートを読み込めません: %s (%s)", path, exc)
            continue
        if not isinstance(data, dict):
            continue
        report_date_text = data.get("date", path.stem)
        try:
            report_date = date.fromisoformat(str(report_date_text)[:10])
        except ValueError:
            continue
        if not start <= report_date <= end:
            continue
        market_conditions = data.get("market_conditions")
        if not isinstance(market_conditions, dict):
            market_conditions = {}
        log_errors = data.get("log_errors")
        if not isinstance(log_errors, dict):
            log_errors = {}
        positions = data.get("positions")
        skip_fields = {
            "ATR危険度": "atr_danger_skips",
            "市場危険度": "market_regime_danger_skips",
            "注意レジームRSI": "market_regime_caution_rsi_filters",
        }
        normalized_errors = normalize_daily_errors(log_errors)
        normalized_errors["board_timeout"]["symbols"]["recovered"] = []
        fallback_used = bool(market_conditions.get("nikkei_fallback_used", False))
        assessment_status = market_conditions.get("assessment_status") or "not_recorded"
        if assessment_status == "available" and fallback_used:
            assessment_status = "available_fallback"
        skip_events = {
            label: [
                {
                    "outcome": ((event.get("evaluations") or {}).get("same_day") or {}).get("outcome"),
                    "pnl_before_cost": ((event.get("evaluations") or {}).get("same_day") or {}).get(
                        "hypothetical_pnl_before_cost"
                    ),
                    "finalized": bool(
                        ((event.get("evaluations") or {}).get("same_day") or {}).get("finalized")
                    ),
                }
                for event in (data.get(field) or [])
            ]
            for label, field in skip_fields.items()
        }
        summaries.append({
            "date": data.get("date", path.stem),
            "trading_mode": data.get("trading_mode"),
            "order_count": data.get("order_count", 0),
            "total_profit_loss": data.get("total_profit_loss", 0),
            "kill_switch_triggered": data.get("kill_switch_triggered", False),
            "emergency_stop_triggered": data.get("emergency_stop_triggered", False),
            "market_assessment_status": assessment_status,
            "market_regime": market_conditions.get("regime"),
            "market_failure_reason": market_conditions.get("failure_reason"),
            "log_error_count": normalized_errors["count"],
            "log_error_recorded": normalized_errors["recorded"],
            "log_errors": normalized_errors,
            "position_count": len(positions) if isinstance(positions, list) else None,
            "skip_counts": {
                label: len(data.get(field) or [])
                for label, field in skip_fields.items()
                if data.get(field)
            },
            "skip_evaluations": skip_events,
        })
    return summaries


def load_backtest_summaries(backtest_directory: Path, start: date, end: date) -> list[dict]:
    summaries = []
    for path in sorted(backtest_directory.glob("latest_timeseries_*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            generated_at = data.get("generated_at")
            generated_date = (
                datetime.fromisoformat(generated_at).date()
                if generated_at
                else date.fromtimestamp(path.stat().st_mtime)
            )
        except (OSError, json.JSONDecodeError, TypeError, ValueError) as exc:
            logger.warning("バックテスト結果を読み込めません: %s (%s)", path, exc)
            continue
        period_start = _parse_date(data.get("period_start")) if isinstance(data, dict) else None
        period_end = _parse_date(data.get("period_end")) if isinstance(data, dict) else None
        has_overlapping_period = (
            period_start is not None
            and period_end is not None
            and period_start <= end
            and start <= period_end
        )
        if (start <= generated_date <= end or has_overlapping_period) and isinstance(data, dict):
            summaries.append({
                "generated_at": generated_date.isoformat(),
                "period_start": data.get("period_start"),
                "period_end": data.get("period_end"),
                "total_pnl": data.get("total_pnl", data.get("総損益", 0)),
                "total_trades": data.get("total_trades", data.get("総取引数", 0)),
                "win_rate": data.get("win_rate", data.get("勝率", 0)),
                "max_drawdown": data.get("max_drawdown", data.get("最大ドローダウン", 0)),
                "final_position": data.get("final_position", data.get("最終保有数", 0)),
            })
    return summaries


def _parse_date(value: object) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None