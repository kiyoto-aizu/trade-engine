import pytest

from src.application.jquants_metrics_evaluation import (
    GO,
    HOLD,
    NO_GO,
    JQuantsGoThresholds,
    evaluate_metrics,
    stage_breakdown,
)

LIMITS = JQuantsGoThresholds(elapsed_limit_seconds=100.0)


def metrics(**overrides):
    base = {
        "status": "COMPLETE",
        "request_count": 10,
        "retry_count": 0,
        "rate_limit_429_count": 0,
        "wait_seconds": 0.0,
        "http_status_counts": {"200": 10},
        "durations_seconds": {"total": 10.0},
    }
    base.update(overrides)
    return base


def verdict(m, limits=LIMITS):
    return evaluate_metrics(m, limits).verdict


def test_clean_run_is_go_but_reports_not_evaluated_items():
    result = evaluate_metrics(metrics(), LIMITS)
    assert result.verdict == GO
    assert {"WARNINGS", "UNKNOWN_FIELDS", "SYMBOL_COUNT", "DISK_SPACE"} <= set(result.not_evaluated)


def test_status_mapping():
    assert verdict(metrics(status="ATTENTION")) == HOLD
    assert verdict(metrics(status="FAILED")) == NO_GO
    result = evaluate_metrics(metrics(status={"x": 1}), LIMITS)
    assert result.verdict == NO_GO and result.reasons == ("STATUS_INVALID",)


def test_auth_error_is_no_go_and_zero_count_is_not():
    result = evaluate_metrics(metrics(http_status_counts={"403": 1}), LIMITS)
    assert result.verdict == NO_GO and "AUTH_OR_CONTRACT_ERROR" in result.reasons
    assert verdict(metrics(http_status_counts={"401": 0, "200": 5})) == GO


def test_429_count_boundaries_independent_of_request_count():
    assert verdict(metrics(rate_limit_429_count=0)) == GO
    assert verdict(metrics(request_count=4, rate_limit_429_count=1, retry_count=1)) == HOLD
    assert verdict(metrics(rate_limit_429_count=2, retry_count=2)) == HOLD
    assert verdict(metrics(rate_limit_429_count=3, retry_count=3)) == NO_GO


def test_other_retries_exclude_429_retries():
    assert verdict(metrics(rate_limit_429_count=1, retry_count=1)) == HOLD
    assert verdict(metrics(retry_count=2)) == GO
    assert verdict(metrics(retry_count=3)) == HOLD
    assert verdict(metrics(retry_count=10)) == NO_GO


def test_elapsed_limit_unset_is_hold():
    result = evaluate_metrics(metrics(durations_seconds={"total": 1.0}))
    assert result.verdict == HOLD and "ELAPSED_LIMIT_UNSET" in result.reasons


def test_elapsed_boundaries_use_wall_clock_total_including_waits():
    assert verdict(metrics(durations_seconds={"total": 70.0})) == GO
    assert verdict(metrics(durations_seconds={"total": 70.1})) == HOLD
    assert verdict(metrics(durations_seconds={"total": 100.0})) == HOLD
    assert verdict(metrics(durations_seconds={"total": 100.1})) == NO_GO
    waited = metrics(durations_seconds={"total": 150.0}, wait_seconds=120.0,
                     rate_limit_429_count=1, retry_count=1)
    assert evaluate_metrics(waited, LIMITS).verdict == NO_GO
    assert "ELAPSED_OVER_LIMIT" in evaluate_metrics(waited, LIMITS).reasons


def test_unmeasured_requests_are_hold():
    result = evaluate_metrics(metrics(request_count="未計測"), LIMITS)
    assert result.verdict == HOLD and "REQUEST_METRICS_UNMEASURED" in result.reasons


def test_zero_requests_is_go_but_api_health_not_evaluated():
    result = evaluate_metrics(metrics(request_count=0), LIMITS)
    assert result.verdict == GO
    assert "API_HEALTH" in result.not_evaluated and "NO_REQUESTS_MADE" in result.reasons


@pytest.mark.parametrize("bad", [-1, float("nan"), float("inf"), True, {"a": 1}])
def test_invalid_numbers_are_treated_as_unmeasured(bad):
    assert verdict(metrics(request_count=bad)) == HOLD


def test_stage_breakdown_includes_other():
    shares = stage_breakdown({"durations_seconds": {"total": 10.0, "acquisition": 7.0, "conversion": 2.0}})
    assert shares["acquisition"] == pytest.approx(0.7)
    assert shares["other"] == pytest.approx(0.1)
    assert stage_breakdown({}) == {}
    assert stage_breakdown({"durations_seconds": {"total": 0.0}}) == {}


def test_worst_verdict_wins():
    assert verdict(metrics(status="ATTENTION", rate_limit_429_count=10, retry_count=10)) == NO_GO


def test_new_fields_are_evaluated_and_leave_not_evaluated():
    clean = evaluate_metrics(
        metrics(error_category=None, warnings=[], symbol_count=5, unknown_field_count=0,
                raw_source="fetch"), LIMITS)
    assert clean.verdict == GO
    assert set(clean.not_evaluated) == {"DISK_SPACE"}


def test_warnings_unknown_fields_and_symbol_count_raise_hold():
    base = dict(warnings=[], symbol_count=5, unknown_field_count=0, raw_source="fetch")
    assert verdict(metrics(**{**base, "warnings": ["EMPTY_RESULT_MEANING_UNKNOWN"]})) == HOLD
    assert verdict(metrics(**{**base, "unknown_field_count": 2})) == HOLD
    assert verdict(metrics(**{**base, "symbol_count": 0})) == HOLD
    assert verdict(metrics(**{**base, "warnings": ["bad value 88880"]})) == HOLD


def test_error_category_and_raw_source():
    base = dict(warnings=[], symbol_count=5, unknown_field_count=0)
    failed = evaluate_metrics(
        metrics(**base, status="FAILED", error_category="RATE_LIMITED", raw_source="fetch"), LIMITS)
    assert failed.verdict == NO_GO and "ERROR_CATEGORY_RATE_LIMITED" in failed.reasons
    assert verdict(metrics(**base, error_category="free text 88880", raw_source="fetch")) == HOLD
    assert verdict(metrics(**base, raw_source="existing", request_count=0,
                           retry_count=0)) == GO
    zero = evaluate_metrics(metrics(**base, raw_source="fetch", request_count=0), LIMITS)
    assert zero.verdict == HOLD and "FETCH_WITHOUT_REQUESTS" in zero.reasons
    assert verdict(metrics(**base, raw_source="bogus")) == HOLD


def test_429_is_excluded_from_other_retries():
    m = metrics(retry_count=2, rate_limit_429_count=2)
    result = evaluate_metrics(m, LIMITS)
    assert "OTHER_RETRIES_ELEVATED" not in result.reasons
    assert "RATE_LIMIT_429_SEEN" in result.reasons
