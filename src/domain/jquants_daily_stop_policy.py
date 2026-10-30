"""日次更新の停止条件（純関数、I/Oなし）。docs/detail_design/18 §2 の表（日次の列）に対応する。

入力は単日取込の終了状態（status・error_category）と、直前までの同一 dataset の連続 FAILED 回数。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

CONSECUTIVE_FAILED_LIMIT = 3

LOCKED_CATEGORY = "DATASET_ALREADY_RUNNING"  # raw_store のロック例外のカテゴリと一致させる
RATE_LIMIT_ABORTED = "RATE_LIMIT_ABORTED"
HALT_CATEGORIES = {
    "AUTH_ERROR": "HTTP_401",
    "PERMISSION_OR_CONTRACT_ERROR": "HTTP_403_IN_RANGE",
}
JOB_STOP_CATEGORIES = {
    "CLIENT_REQUEST_ERROR": "UNEXPECTED_400_IN_RANGE",
    "QUALITY_ERROR": "QUALITY_VALIDATION_FAILED",
}


class StopAction(StrEnum):
    CONTINUE = "CONTINUE"  # 再開対象として記録し、次の dataset へ進む
    SKIP_LOCKED = "SKIP_LOCKED"  # ロック取得失敗。待機せず記録して次へ
    STOP_JOB = "STOP_JOB"  # その dataset のジョブのみ停止
    STOP_TODAY = "STOP_TODAY"  # 当日の残りを停止（人間へ通知）
    HALT_FLAG = "HALT_FLAG"  # 全ジョブ停止。停止フラグで保持し人間が解除する


@dataclass(frozen=True)
class StopDecision:
    action: StopAction
    reason: str | None = None


def decide_stop(status: str, error_category: str | None, prior_consecutive_failed: int) -> StopDecision:
    """prior_consecutive_failed は今回を含まない、同一 dataset の直近の連続 FAILED 回数。"""
    if error_category == LOCKED_CATEGORY:
        return StopDecision(StopAction.SKIP_LOCKED, LOCKED_CATEGORY)
    if status != "FAILED":
        return StopDecision(StopAction.CONTINUE)
    if error_category == RATE_LIMIT_ABORTED:
        return StopDecision(StopAction.STOP_TODAY, RATE_LIMIT_ABORTED)
    if error_category in HALT_CATEGORIES:
        return StopDecision(StopAction.HALT_FLAG, HALT_CATEGORIES[error_category])
    if error_category in JOB_STOP_CATEGORIES:
        return StopDecision(StopAction.STOP_JOB, JOB_STOP_CATEGORIES[error_category])
    if prior_consecutive_failed + 1 >= CONSECUTIVE_FAILED_LIMIT:
        return StopDecision(StopAction.STOP_TODAY, "CONSECUTIVE_FAILED_3")
    return StopDecision(StopAction.CONTINUE, "FAILED_RECOVERY_TARGET")
