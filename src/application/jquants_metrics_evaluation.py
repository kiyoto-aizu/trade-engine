"""Read-only Go/Hold/No-Go evaluation of J-Quants single-day execution metrics.

Operates on the metrics JSON written by the single-day ingestion. It never
touches the network and emits only counts, never record values or symbols.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

GO = "Go"
HOLD = "Hold"
NO_GO = "No-Go"
_RANK = {GO: 0, HOLD: 1, NO_GO: 2}


@dataclass(frozen=True)
class JQuantsGoThresholds:
    rate_429_hold: float = 0.01
    rate_429_no_go: float = 0.05
    retry_rate_hold: float = 0.05
    retry_rate_no_go: float = 0.20
    elapsed_limit_seconds: float | None = None
    elapsed_hold_ratio: float = 0.70


@dataclass(frozen=True)
class JQuantsEvaluation:
    verdict: str
    reasons: tuple[str, ...] = field(default_factory=tuple)


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def evaluate_metrics(
    metrics: Mapping[str, Any], thresholds: JQuantsGoThresholds | None = None
) -> JQuantsEvaluation:
    limits = thresholds or JQuantsGoThresholds()
    verdict = GO
    reasons: list[str] = []

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
    else:
        raise_to(NO_GO, f"STATUS_{status}")

    statuses = metrics.get("http_status_counts")
    if isinstance(statuses, Mapping):
        if any(str(code) in {"401", "403"} and count for code, count in statuses.items()):
            raise_to(NO_GO, "AUTH_OR_CONTRACT_ERROR")

    requests = _number(metrics.get("request_count"))
    if requests:
        rate_429 = (_number(metrics.get("rate_limit_429_count")) or 0.0) / requests
        retry_rate = (_number(metrics.get("retry_count")) or 0.0) / requests
        if rate_429 > limits.rate_429_no_go:
            raise_to(NO_GO, "RATE_LIMIT_429_HIGH")
        elif rate_429 > limits.rate_429_hold:
            raise_to(HOLD, "RATE_LIMIT_429_ELEVATED")
        if retry_rate > limits.retry_rate_no_go:
            raise_to(NO_GO, "RETRY_RATE_HIGH")
        elif retry_rate > limits.retry_rate_hold:
            raise_to(HOLD, "RETRY_RATE_ELEVATED")
    elif requests is None:
        raise_to(HOLD, "REQUEST_METRICS_UNMEASURED")

    durations = metrics.get("durations_seconds")
    total = _number(durations.get("total")) if isinstance(durations, Mapping) else None
    if limits.elapsed_limit_seconds is not None and total is not None:
        if total > limits.elapsed_limit_seconds:
            raise_to(NO_GO, "ELAPSED_OVER_LIMIT")
        elif total > limits.elapsed_limit_seconds * limits.elapsed_hold_ratio:
            raise_to(HOLD, "ELAPSED_NEAR_LIMIT")

    return JQuantsEvaluation(verdict, tuple(reasons))


def stage_breakdown(metrics: Mapping[str, Any]) -> dict[str, float]:
    """Return each stage's share of total time to expose bottlenecks."""
    durations = metrics.get("durations_seconds")
    if not isinstance(durations, Mapping):
        return {}
    total = _number(durations.get("total"))
    if not total:
        return {}
    return {
        stage: value / total
        for stage, raw in durations.items()
        if stage != "total" and (value := _number(raw)) is not None
    }
