"""過去補填ジョブの停止判定（純関数、I/Oなし）。docs/detail_design/18 §2 表の「補填」列に対応する。

1日分の取込結果（status・error_category）から、次に何をするかを決める。
停止フラグの永続化と全体ロックは扱わない（停止理由を返すところまで）。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from enum import Enum

from src.domain.jquants_contract_range import classify_client_request_error

MAX_CONSECUTIVE_FAILED = 3
BACKFILL_BUDGET_SECONDS = 2 * 60 * 60  # 1日あたりの補填予算（設計書18 §2。人間承認済み）
MIN_FREE_BYTES = 10 * 1024**3  # 保存先の空きがこれ未満なら補填のみ停止（設計書18 §4-1）

HOLD_FORBIDDEN = "FORBIDDEN_REVIEW"  # 403要確認
HOLD_OUT_OF_CONTRACT = "OUT_OF_CONTRACT"  # 契約範囲外

_RATE_LIMIT_CATEGORIES = frozenset({"RATE_LIMITED", "RATE_LIMIT_ABORTED"})


class Action(str, Enum):
    CONTINUE = "CONTINUE"
    STOP_TODAY = "STOP_TODAY"  # 当日の残りだけ止める。翌営業日は妨げない
    STOP_BACKFILL = "STOP_BACKFILL"  # この補填ジョブの残り全日付を止める
    END_BACKFILL = "END_BACKFILL"  # 契約境界に到達したため補填を終了する


@dataclass(frozen=True)
class DayVerdict:
    action: Action
    reason: str | None = None
    hold_kind: str | None = None  # 保持ストアに記録する種別。記録しないときはNone
    consecutive_failed: int = 0


def judge_day(
    dataset: str, target_date: date, today: date, status: str, error_category: str | None,
    previous_consecutive_failed: int,
) -> DayVerdict:
    """1日分の結果を判定する。連続FAILEDは日付をまたいで数える（呼び出し側が前回値を渡す）。"""
    if status in ("COMPLETE", "ATTENTION"):
        return DayVerdict(Action.CONTINUE)
    if status != "FAILED":
        return DayVerdict(Action.STOP_BACKFILL, "UNKNOWN_RESULT_STATUS")
    if error_category == "AUTH_ERROR":
        return DayVerdict(Action.STOP_BACKFILL, "HTTP_401")
    if error_category == "PERMISSION_OR_CONTRACT_ERROR":
        return DayVerdict(Action.STOP_BACKFILL, "HTTP_403", HOLD_FORBIDDEN)
    if error_category == "CLIENT_REQUEST_ERROR":
        if classify_client_request_error(dataset, target_date, today) == "OUT_OF_CONTRACT_RANGE":
            return DayVerdict(Action.END_BACKFILL, "OUT_OF_CONTRACT_RANGE", HOLD_OUT_OF_CONTRACT)
        return DayVerdict(Action.STOP_BACKFILL, "UNEXPECTED_400_IN_RANGE")
    if error_category in _RATE_LIMIT_CATEGORIES:
        return DayVerdict(Action.STOP_TODAY, "RATE_LIMITED")
    if error_category == "QUALITY_ERROR":
        return DayVerdict(Action.STOP_BACKFILL, "QUALITY_VALIDATION_FAILED")
    count = previous_consecutive_failed + 1
    if count >= MAX_CONSECUTIVE_FAILED:
        return DayVerdict(Action.STOP_TODAY, "CONSECUTIVE_FAILED", consecutive_failed=count)
    return DayVerdict(Action.CONTINUE, consecutive_failed=count)


def is_budget_exceeded(elapsed_seconds: float, budget_seconds: float = BACKFILL_BUDGET_SECONDS) -> bool:
    return elapsed_seconds >= budget_seconds


def is_disk_insufficient(free_bytes: int, minimum_bytes: int = MIN_FREE_BYTES) -> bool:
    return free_bytes < minimum_bytes
