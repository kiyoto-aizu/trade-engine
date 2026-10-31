import pytest
import os
from pathlib import Path

from src.config import config
from tests.runtime_guard import RuntimeStateGuard, watch

# モンキーパッチ前の本番パス。番人は常にこちらを監視する。
PRODUCTION_REPORTS_DIRECTORY = config.DAILY_REPORT_DIRECTORY
PRODUCTION_FILTER_DECISION_DATABASE = config.FILTER_DECISION_DATABASE_FILE
PRODUCTION_SCREENING_DIRECTORY = config.SCREENING_RESULT_DIRECTORY
PRODUCTION_FILTERING_DIRECTORY = config.FILTERING_RESULT_DIRECTORY
PRODUCTION_FILTERING_DIAGNOSTICS_DIRECTORY = config.FILTERING_DIAGNOSTICS_DIRECTORY
PRODUCTION_SCREENING_API_CHECK_DIRECTORY = config.SCREENING_API_CHECK_DIRECTORY
PRODUCTION_SCREENING_PRICE_BAND_DIRECTORY = config.SCREENING_PRICE_BAND_RESULT_ROOT
PRODUCTION_FILTERING_PRICE_BAND_DIRECTORY = config.FILTERING_PRICE_BAND_RESULT_ROOT
JQUANTS_STORAGE_KEYS = (
    "JQUANTS_RAW_DIRECTORY", "JQUANTS_CANONICAL_DIRECTORY", "JQUANTS_LEDGER_DATABASE_FILE",
    "JQUANTS_EXECUTION_METRICS_DIRECTORY", "JQUANTS_BACKFILL_STATE_DIRECTORY",
    "JQUANTS_CALENDAR_DIRECTORY",
)
PRODUCTION_JQUANTS_DESTINATIONS = tuple(
    Path(getattr(config, key)).absolute() for key in JQUANTS_STORAGE_KEYS
)


@pytest.fixture(autouse=True)
def _jquants_execution_io_guard(request, monkeypatch, tmp_path):
    if request.path.name not in {
        "test_jquants_ops_orchestration.py", "test_jquants_daily_job.py",
        "test_jquants_calendar.py", "test_jquants_execution_guard.py",
        "test_jquants_single_day_ingestion.py", "test_jquants_backfill_job.py",
    }:
        return
    for key in JQUANTS_STORAGE_KEYS:
        monkeypatch.setattr(config, key, tmp_path / key)

    def check(path):
        candidate = Path(path).absolute()
        if any(candidate == root or root in candidate.parents for root in PRODUCTION_JQUANTS_DESTINATIONS):
            pytest.fail("JQUANTS_DEFAULT_DESTINATION_WRITE_BLOCKED", pytrace=False)

    original_mkdir = Path.mkdir
    original_open = Path.open
    original_os_open = os.open

    def mkdir(path, *args, **kwargs):
        check(path)
        return original_mkdir(path, *args, **kwargs)

    def opened(path, mode="r", *args, **kwargs):
        if any(flag in mode for flag in "wax+"):
            check(path)
        return original_open(path, mode, *args, **kwargs)

    def os_open(path, flags, *args, **kwargs):
        if flags & (os.O_CREAT | os.O_WRONLY | os.O_RDWR | os.O_TRUNC | os.O_APPEND):
            check(path)
        return original_os_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(Path, "mkdir", mkdir)
    monkeypatch.setattr(Path, "open", opened)
    monkeypatch.setattr(os, "open", os_open)

    def forbidden_post(*args, **kwargs):
        pytest.fail("JQUANTS_NETWORK_NOT_MOCKED", pytrace=False)

    monkeypatch.setattr("requests.post", forbidden_post)
    return PRODUCTION_JQUANTS_DESTINATIONS


@pytest.fixture(autouse=True)
def _isolate_runtime_paths(monkeypatch, tmp_path_factory):
    """既定の出力先(日次レポート・判定イベントDB)をテスト用の一時フォルダへ差し替える。"""
    root = tmp_path_factory.mktemp("isolated_runtime")
    monkeypatch.setattr(config, "DAILY_REPORT_DIRECTORY", root / "reports" / "daily")
    monkeypatch.setattr(config, "FILTER_DECISION_DATABASE_FILE", root / "state" / "filter_decision_events.sqlite3")
    monkeypatch.setattr(config, "SCREENING_RESULT_DIRECTORY", root / "screening")
    monkeypatch.setattr(config, "FILTERING_RESULT_DIRECTORY", root / "filtering")
    monkeypatch.setattr(config, "FILTERING_DIAGNOSTICS_DIRECTORY", root / "filtering_diagnostics")
    monkeypatch.setattr(config, "SCREENING_API_CHECK_DIRECTORY", root / "screening_api_checks")
    monkeypatch.setattr(config, "SCREENING_PRICE_BAND_RESULT_ROOT", root / "screening_price_bands")
    monkeypatch.setattr(config, "FILTERING_PRICE_BAND_RESULT_ROOT", root / "filtering_price_bands")


@pytest.fixture(autouse=True)
def _disable_optional_anomaly_llm(monkeypatch):
    """通常のテストから外部LLMを呼ばない。LLM専用テストは個別に有効化する。"""
    monkeypatch.setattr(config, "LLM_ANOMALY_ANALYSIS_ENABLED", False)


@pytest.fixture(autouse=True)
def _production_state_guard(request):
    guard = RuntimeStateGuard(
        PRODUCTION_REPORTS_DIRECTORY,
        PRODUCTION_FILTER_DECISION_DATABASE,
        watched_directories=(
            PRODUCTION_SCREENING_DIRECTORY,
            PRODUCTION_FILTERING_DIRECTORY,
            PRODUCTION_FILTERING_DIAGNOSTICS_DIRECTORY,
            PRODUCTION_SCREENING_API_CHECK_DIRECTORY,
            PRODUCTION_SCREENING_PRICE_BAND_DIRECTORY,
            PRODUCTION_FILTERING_PRICE_BAND_DIRECTORY,
        ),
    )
    with watch(guard, request.node.nodeid):
        yield


@pytest.fixture(autouse=True, scope="session")
def _production_state_hash_guard():
    guard = RuntimeStateGuard(
        PRODUCTION_REPORTS_DIRECTORY,
        PRODUCTION_FILTER_DECISION_DATABASE,
        with_hash=True,
        watched_directories=(
            PRODUCTION_SCREENING_DIRECTORY,
            PRODUCTION_FILTERING_DIRECTORY,
            PRODUCTION_FILTERING_DIAGNOSTICS_DIRECTORY,
            PRODUCTION_SCREENING_API_CHECK_DIRECTORY,
            PRODUCTION_SCREENING_PRICE_BAND_DIRECTORY,
            PRODUCTION_FILTERING_PRICE_BAND_DIRECTORY,
        ),
    )
    with watch(guard, "テストセッション全体"):
        yield
