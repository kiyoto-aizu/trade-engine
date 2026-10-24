"""Read-only partial Go/Hold/No-Go evaluation of J-Quants execution metrics.

Operates on the metrics JSON written by the single-day ingestion. It never
touches the network and emits only enumerated reason codes. The verdict covers
only the checks listed in the design (status, HTTP errors, 429, other retries,
active elapsed time); everything else is reported in ``not_evaluated``.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Mapping

GO = "Go"
HOLD = "Hold"
NO_GO = "No-Go"
_RANK = {GO: 0, HOLD: 1, NO_GO: 2}
VALID_STATUSES = frozenset({"COMPLETE", "ATTENTION", "FAILED"})
# Items required by the design that metrics JSON cannot yet express.
BASE_NOT_EVALUATED = ("WARNINGS", "UNKNOWN_FIELDS", "SYMBOL_COUNT", "DISK_SPACE")


@dataclass(frozen=True)
class JQuantsGoThresholds:
    """Count-based initial values; small daily request counts make ratios unstable."""

    rate_limit_429_hold: int = 1
    rate_limit_429_no_go: int = 3
    other_retry_hold: int = 3
    other_retry_no_go: int = 10
    elapsed_limit_seconds: float | None = None
    elapsed_hold_ratio: float = 0.70


@dataclass(frozen=True)
class JQuantsEvaluation:
    verdict: str
    reasons: tuple[str, ...] = field(default_factory=tuple)
    not_evaluated: tuple[str, ...] = BASE_NOT_EVALUATED


def safe_number(value: Any) -> float | None:
    """Finite, non-negative numbers only; everything else is treated as unmeasured."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if not math.isfinite(value) or value < 0:
        return None
    return float(value)


def evaluate_metrics(
    metrics: Mapping[str, Any], thresholds: JQuantsGoThresholds | None = None
) -> JQuantsEvaluation:
    limits = thresholds or JQuantsGoThresholds()
    verdict = GO
    reasons: list[str] = []
    not_evaluated = list(BASE_NOT_EVALUATED)

    def raise_to(level: str, reason: str) -> None:
        nonlocal verdict
        reasons.append(reason)
        if _RANK[level] > _RANK[verdict]:
            verdict = level

    status = metrics.get("status")
    if status == "COMPLETE":
        pass
    elif status == "ATTENTION":
        raise_to(HOLD, "STATUS_ATTENTION")
    elif status == "FAILED":
        raise_to(NO_GO, "STATUS_FAILED")
    else:
        raise_to(NO_GO, "STATUS_INVALID")

    statuses = metrics.get("http_status_counts")
    if isinstance(statuses, Mapping):
        if any(str(code) in {"401", "403"} and safe_number(count) for code, count in statuses.items()):
            raise_to(NO_GO, "AUTH_OR_CONTRACT_ERROR")

    requests = safe_number(metrics.get("request_count"))
    count_429 = safe_number(metrics.get("rate_limit_429_count"))
    retries = safe_number(metrics.get("retry_count"))
    if requests is None or count_429 is None or retries is None:
        raise_to(HOLD, "REQUEST_METRICS_UNMEASURED")
    elif requests == 0:
        not_evaluated.append("API_HEALTH")
        reasons.append("NO_REQUESTS_MADE")
    else:
        if count_429 >= limits.rate_limit_429_no_go:
            raise_to(NO_GO, "RATE_LIMIT_429_HIGH")
        elif count_429 >= limits.rate_limit_429_hold:
            raise_to(HOLD, "RATE_LIMIT_429_SEEN")
        other_retries = max(retries - count_429, 0.0)
        if other_retries >= limits.other_retry_no_go:
            raise_to(NO_GO, "OTHER_RETRIES_HIGH")
        elif other_retries >= limits.other_retry_hold:
            raise_to(HOLD, "OTHER_RETRIES_ELEVATED")

    durations = metrics.get("durations_seconds")
    total = safe_number(durations.get("total")) if isinstance(durations, Mapping) else None
    if limits.elapsed_limit_seconds is None:
        raise_to(HOLD, "ELAPSED_LIMIT_UNSET")
    elif total is None:
        raise_to(HOLD, "ELAPSED_UNMEASURED")
    elif total > limits.elapsed_limit_seconds:
        raise_to(NO_GO, "ELAPSED_OVER_LIMIT")
    elif total > limits.elapsed_limit_seconds * limits.elapsed_hold_ratio:
        raise_to(HOLD, "ELAPSED_NEAR_LIMIT")

    return JQuantsEvaluation(verdict, tuple(reasons), tuple(not_evaluated))


def stage_breakdown(metrics: Mapping[str, Any]) -> dict[str, float]:
    """Return each stage's share of total time; untracked time is reported as ``other``."""
    durations = metrics.get("durations_seconds")
    if not isinstance(durations, Mapping):
        return {}
    total = safe_number(durations.get("total"))
    if not total:
        return {}
    shares = {
        stage: value / total
        for stage, raw in durations.items()
        if stage != "total" and (value := safe_number(raw)) is not None
    }
    shares["other"] = max(1.0 - sum(shares.values()), 0.0)
    return shares
