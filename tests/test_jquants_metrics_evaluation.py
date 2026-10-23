from src.application.jquants_metrics_evaluation import (
    GO,
    HOLD,
    NO_GO,
    JQuantsGoThresholds,
    evaluate_metrics,
    stage_breakdown,
)


def metrics(**overrides):
    base = {
        "status": "COMPLETE",
        "request_count": 100,
        "retry_count": 0,
        "rate_limit_429_count": 0,
        "http_status_counts": {"200": 100},
        "durations_seconds": {"total": 10.0},
    }
    base.update(overrides)
    return base


def test_clean_run_is_go():
    assert evaluate_metrics(metrics()).verdict == GO


def test_status_mapping():
    assert evaluate_metrics(metrics(status="ATTENTION")).verdict == HOLD
    assert evaluate_metrics(metrics(status="FAILED")).verdict == NO_GO


def test_auth_error_is_no_go():
    result = evaluate_metrics(metrics(http_status_counts={"403": 1}))
    assert result.verdict == NO_GO
    assert "AUTH_OR_CONTRACT_ERROR" in result.reasons


def test_429_boundaries():
    assert evaluate_metrics(metrics(rate_limit_429_count=1)).verdict == GO
    assert evaluate_metrics(metrics(rate_limit_429_count=2)).verdict == HOLD
    assert evaluate_metrics(metrics(rate_limit_429_count=5)).verdict == HOLD
    assert evaluate_metrics(metrics(rate_limit_429_count=6)).verdict == NO_GO


def test_retry_boundaries():
    assert evaluate_metrics(metrics(retry_count=5)).verdict == GO
    assert evaluate_metrics(metrics(retry_count=6)).verdict == HOLD
    assert evaluate_metrics(metrics(retry_count=21)).verdict == NO_GO


def test_elapsed_limit_applies_only_when_configured():
    assert evaluate_metrics(metrics(durations_seconds={"total": 999.0})).verdict == GO
    limits = JQuantsGoThresholds(elapsed_limit_seconds=100.0)
    assert evaluate_metrics(metrics(durations_seconds={"total": 70.0}), limits).verdict == GO
    assert evaluate_metrics(metrics(durations_seconds={"total": 80.0}), limits).verdict == HOLD
    assert evaluate_metrics(metrics(durations_seconds={"total": 101.0}), limits).verdict == NO_GO


def test_unmeasured_requests_are_hold():
    result = evaluate_metrics(metrics(request_count="未計測"))
    assert result.verdict == HOLD
    assert "REQUEST_METRICS_UNMEASURED" in result.reasons


def test_stage_breakdown_shares():
    shares = stage_breakdown({"durations_seconds": {"total": 10.0, "acquisition": 8.0, "conversion": 2.0}})
    assert shares == {"acquisition": 0.8, "conversion": 0.2}
    assert stage_breakdown({}) == {}
    assert stage_breakdown({"durations_seconds": {"total": 0.0}}) == {}


def test_worst_verdict_wins():
    result = evaluate_metrics(metrics(status="ATTENTION", rate_limit_429_count=10))
    assert result.verdict == NO_GO
