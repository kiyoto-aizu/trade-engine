import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import datetime
import logging
from pathlib import Path
from types import SimpleNamespace

from src.entrypoints import run_filtering
from src.infrastructure.kabu.registration_aware_board_cache import RegistrationAwareBoardCache
from src.infrastructure.market_data.cached_volume_client import CachedVolumeClient
from src.config import config


def test_main_does_not_request_token_on_non_trading_day(monkeypatch):
    monkeypatch.setattr(sys, 'argv', ['run_filtering.py'])
    monkeypatch.setattr(run_filtering, 'configure_logging', lambda: None)
    monkeypatch.setattr(run_filtering, 'is_trading_day', lambda _: False)
    monkeypatch.setattr(
        run_filtering,
        'get_api_token',
        lambda: (_ for _ in ()).throw(AssertionError('token was requested')),
    )
    monkeypatch.setattr(
        run_filtering,
        'market_workflow_lock',
        lambda: (_ for _ in ()).throw(AssertionError('market_workflow_lock was entered')),
    )

    # 例外が発生しなければ、休場日ガードで早期returnできている
    run_filtering.main()


def test_registration_aware_board_cache_deduplicates_and_clears_at_batch_limit():
    requested = []
    unregisters = []

    class BoardStub:
        def get_current_board(self, symbol):
            requested.append(symbol)
            return {"symbol": symbol}

    cache = RegistrationAwareBoardCache(
        BoardStub(),
        lambda: unregisters.append("clear") or {},
        batch_size=2,
    )

    assert cache.get_current_board("7203") == {"symbol": "7203"}
    assert cache.get_current_board("7203") == {"symbol": "7203"}
    assert cache.get_current_board("8306") == {"symbol": "8306"}
    assert cache.get_current_board("9432") == {"symbol": "9432"}

    assert requested == ["7203", "8306", "9432"]
    assert unregisters == ["clear"]
    assert cache.fetch_count == 3
    assert cache.cache_hit_count == 1


def test_parallel_cache_coalesces_duplicates_and_waits_for_active_fetches_before_clear():
    lock = threading.Lock()
    both_started = threading.Event()
    release_fetches = threading.Event()
    clear_called = threading.Event()
    active_requests = 0
    requested = []
    active_at_clear = []

    class SlowBoardClient:
        def get_current_board(self, symbol):
            nonlocal active_requests
            with lock:
                requested.append(symbol)
                active_requests += 1
                if active_requests == 2:
                    both_started.set()
            assert release_fetches.wait(timeout=2)
            with lock:
                active_requests -= 1
            return {"symbol": symbol}

    def unregister():
        with lock:
            active_at_clear.append(active_requests)
        clear_called.set()
        return {}

    cache = RegistrationAwareBoardCache(
        SlowBoardClient(), unregister, batch_size=2, max_workers=2
    )
    with ThreadPoolExecutor(max_workers=2) as executor:
        batch = executor.submit(cache.fetch_current_boards, ["A", "A", "B"])
        assert both_started.wait(timeout=2)
        next_symbol = executor.submit(cache.get_current_board, "C")
        assert not clear_called.wait(timeout=0.05)
        release_fetches.set()
        assert batch.result(timeout=2) == {"A": {"symbol": "A"}, "B": {"symbol": "B"}}
        assert next_symbol.result(timeout=2) == {"symbol": "C"}

    assert requested.count("A") == 1
    assert requested.count("B") == 1
    assert requested.count("C") == 1
    assert active_at_clear == [0]
    assert cache.max_concurrency_observed == 2


def test_cached_volume_client_reuses_results_without_sharing_mutable_dicts():
    calls = []

    class VolumeStub:
        def get_average_turnover_details(self, symbol, days, target_date):
            calls.append((symbol, days, target_date))
            return {"average_turnover": 100.0}

    client = CachedVolumeClient(VolumeStub())
    first = client.get_average_turnover_details("7203", target_date=datetime(2026, 10, 5).date())
    first["average_turnover"] = 0.0
    second = client.get_average_turnover_details("7203", target_date=datetime(2026, 10, 5).date())

    assert second == {"average_turnover": 100.0}
    assert calls == [("7203", 20, datetime(2026, 10, 5).date())]


def test_main_saves_270_filter_before_isolated_price_band_failures(monkeypatch, caplog):
    caplog.set_level(logging.INFO)
    events = []
    board_requests = []
    saved_results = {}
    token_requests = []
    unregister_calls = []

    @contextmanager
    def context(*_args, **_kwargs):
        yield

    @contextmanager
    def acquired_lock():
        yield True

    class FakeBoardClient:
        def __init__(self, _token):
            pass

        def get_current_board(self, symbol):
            board_requests.append(symbol)
            return {"current_price": 100.0, "trading_value": 1000.0}

    class FakeRepository:
        def __init__(self, directory):
            self.directory = Path(directory)

        def load_for_date(self, _day):
            if self.directory == config.SCREENING_RESULT_DIRECTORY:
                return SimpleNamespace(symbols=["common"])
            if self.directory.name == "450":
                return SimpleNamespace(symbols=["common", "450-a", "450-b"])
            if self.directory.name == "900":
                return SimpleNamespace(symbols=["common", "900-a"])
            return SimpleNamespace(symbols=[])

        def save(self, result):
            saved_results[self.directory] = result.symbols
            events.append(("saved", self.directory.name, result.symbols))

    class FakeFilteringUseCase:
        def __init__(self, screening_repository, board_client, _volume, result_repository, *_args, **_kwargs):
            self.screening_repository = screening_repository
            self.board_client = board_client
            self.result_repository = result_repository

        def execute(self, target_date=None, price_cap=None, **_kwargs):
            if price_cap is None:
                self.board_client.get_current_board("common")
                self.result_repository.save(SimpleNamespace(symbols=["common"]))
                events.append(("primary_complete",))
                return SimpleNamespace(symbols=["common"])
            if price_cap == 450.0:
                self.board_client.get_current_board("common")
                self.board_client.get_current_board("450-a")
                self.board_client.get_current_board("450-b")
                events.append(("alternate_failed", price_cap))
                raise RuntimeError("injected alternative failure")
            self.board_client.get_current_board("common")
            self.board_client.get_current_board("450-a")
            self.board_client.get_current_board("450-b")
            self.board_client.get_current_board("900-a")
            self.result_repository.save(SimpleNamespace(symbols=["common", "900-a"]))
            events.append(("alternate_complete", price_cap))
            return SimpleNamespace(symbols=["common", "900-a"])

    class FakeDiagnosticsRepository:
        def __init__(self, _directory):
            pass

    original_datetime = run_filtering.datetime

    class MorningDateTime(original_datetime):
        # 締め切り(09:34)前に固定し、実行時刻で板の先読み有無が変わらないようにする
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 10, 5, 9, 0)

    monkeypatch.setattr(run_filtering, "datetime", MorningDateTime)
    monkeypatch.setattr(config, "FILTERING_PRICE_BAND_DEADLINE_TIME", original_datetime.strptime("09:34", "%H:%M").time())
    monkeypatch.setattr(sys, "argv", ["run_filtering.py"])
    monkeypatch.setattr(run_filtering, "configure_logging", lambda: None)
    monkeypatch.setattr(run_filtering, "is_trading_day", lambda _day: True)
    monkeypatch.setattr(run_filtering, "market_workflow_lock", acquired_lock)
    monkeypatch.setattr(run_filtering, "process_notification", context)
    monkeypatch.setattr(run_filtering, "get_api_token", lambda: token_requests.append("token") or "token")
    monkeypatch.setattr(run_filtering, "get_token_provider", lambda: SimpleNamespace(recovery_failed=False))
    monkeypatch.setattr(
        run_filtering,
        "unregister_all",
        lambda _token: unregister_calls.append("clear") or events.append(("unregister",)) or {},
    )
    monkeypatch.setattr(run_filtering, "BoardRepository", FakeBoardClient)
    monkeypatch.setattr(run_filtering, "FilteringUseCase", FakeFilteringUseCase)
    monkeypatch.setattr(run_filtering, "ScreeningResultRepository", FakeRepository)
    monkeypatch.setattr(run_filtering, "FilteringResultRepository", FakeRepository)
    monkeypatch.setattr(run_filtering, "FilteringDiagnosticsRepository", FakeDiagnosticsRepository)
    monkeypatch.setattr(run_filtering, "DecisionJournalRepository", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(run_filtering, "YahooFinanceClient", lambda: object())
    monkeypatch.setattr(config, "SCREENING_ALTERNATE_PRICE_CAPS", (450.0, 900.0))
    monkeypatch.setattr(config, "FILTERING_MORNING_ALTERNATE_BANDS_ENABLED", True)
    monkeypatch.setattr(config, "SCREENING_BATCH_SIZE", 2)

    run_filtering.main()

    assert saved_results[config.FILTERING_RESULT_DIRECTORY] == ["common"]
    assert saved_results[config.FILTERING_PRICE_BAND_RESULT_ROOT / "900"] == ["common", "900-a"]
    assert len(board_requests) == len(set(board_requests)) == 4
    assert set(board_requests) == {"common", "450-a", "450-b", "900-a"}
    assert events.index(("primary_complete",)) < events.index(("alternate_failed", 450.0))
    assert events.index(("alternate_failed", 450.0)) < events.index(("alternate_complete", 900.0))
    assert token_requests == ["token"]
    assert len(unregister_calls) == 4
    messages = [record.getMessage() for record in caplog.records]
    assert "価格帯別フィルタに失敗しました: 上限=450円" in messages
    band_900_message = next(
        message for message in messages if message.startswith("価格帯別フィルタ完了: 上限=900円")
    )
    assert "採用=2件 | 所要=" in band_900_message
    assert f"保存先={config.FILTERING_PRICE_BAND_RESULT_ROOT / '900'}" in band_900_message
    summary = next(record for record in caplog.records if record.msg.startswith("全価格帯板取得サマリー:"))
    assert summary.args[0:2] == (4, 8)
    assert summary.args[4] == 2


def test_main_stops_price_bands_when_unregister_after_primary_fails(monkeypatch, caplog):
    executions = []
    unregister_count = 0

    @contextmanager
    def context(*_args, **_kwargs):
        yield

    @contextmanager
    def acquired_lock():
        yield True

    class FakeFilteringUseCase:
        def __init__(self, screening_repository, *_args, **_kwargs):
            self.screening_repository = screening_repository

        def execute(self, target_date=None, price_cap=None, **_kwargs):
            executions.append(price_cap)
            return SimpleNamespace(symbols=[])

    def unregister(_token):
        nonlocal unregister_count
        unregister_count += 1
        return None if unregister_count == 2 else {}

    monkeypatch.setattr(sys, "argv", ["run_filtering.py"])
    monkeypatch.setattr(run_filtering, "configure_logging", lambda: None)
    monkeypatch.setattr(run_filtering, "is_trading_day", lambda _day: True)
    monkeypatch.setattr(run_filtering, "market_workflow_lock", acquired_lock)
    monkeypatch.setattr(run_filtering, "process_notification", context)
    monkeypatch.setattr(run_filtering, "get_api_token", lambda: "token")
    monkeypatch.setattr(run_filtering, "get_token_provider", lambda: SimpleNamespace(recovery_failed=False))
    monkeypatch.setattr(run_filtering, "unregister_all", unregister)
    monkeypatch.setattr(run_filtering, "BoardRepository", lambda _token: object())
    monkeypatch.setattr(run_filtering, "FilteringUseCase", FakeFilteringUseCase)
    monkeypatch.setattr(
        run_filtering,
        "ScreeningResultRepository",
        lambda *_args: SimpleNamespace(load_for_date=lambda _day: SimpleNamespace(symbols=[])),
    )
    monkeypatch.setattr(run_filtering, "FilteringResultRepository", lambda *_args: object())
    monkeypatch.setattr(run_filtering, "FilteringDiagnosticsRepository", lambda *_args: object())
    monkeypatch.setattr(run_filtering, "DecisionJournalRepository", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(run_filtering, "YahooFinanceClient", lambda: object())
    monkeypatch.setattr(config, "SCREENING_ALTERNATE_PRICE_CAPS", (450.0, 900.0))
    monkeypatch.setattr(config, "FILTERING_MORNING_ALTERNATE_BANDS_ENABLED", True)

    run_filtering.main()

    assert executions == [None]
    assert "通常フィルタ後の登録解除に失敗したため、追加価格帯フィルタを中止します。" in [
        record.getMessage() for record in caplog.records
    ]


def test_main_skips_price_bands_for_historical_date_without_board_cache(monkeypatch):
    executions = []

    @contextmanager
    def context(*_args, **_kwargs):
        yield

    @contextmanager
    def acquired_lock():
        yield True

    class FakeFilteringUseCase:
        def __init__(self, _screening, board_client, _volume, _result, *_args, **_kwargs):
            assert board_client is None

        def execute(self, target_date=None, price_cap=None, **_kwargs):
            executions.append((target_date, price_cap))
            return SimpleNamespace(symbols=[])

    monkeypatch.setattr(sys, "argv", ["run_filtering.py", "--date", "2026-10-02"])
    monkeypatch.setattr(run_filtering, "configure_logging", lambda: None)
    monkeypatch.setattr(run_filtering, "is_trading_day", lambda _day: True)
    monkeypatch.setattr(run_filtering, "market_workflow_lock", acquired_lock)
    monkeypatch.setattr(run_filtering, "process_notification", context)
    monkeypatch.setattr(run_filtering, "get_api_token", lambda: (_ for _ in ()).throw(AssertionError()))
    monkeypatch.setattr(run_filtering, "FilteringUseCase", FakeFilteringUseCase)
    monkeypatch.setattr(run_filtering, "ScreeningResultRepository", lambda *_args: object())
    monkeypatch.setattr(run_filtering, "FilteringResultRepository", lambda *_args: object())
    monkeypatch.setattr(run_filtering, "FilteringDiagnosticsRepository", lambda *_args: object())
    monkeypatch.setattr(run_filtering, "YahooFinanceClient", lambda: object())
    monkeypatch.setattr(config, "SCREENING_ALTERNATE_PRICE_CAPS", (450.0,))
    monkeypatch.setattr(config, "FILTERING_MORNING_ALTERNATE_BANDS_ENABLED", True)

    run_filtering.main()

    assert executions == [(datetime(2026, 10, 2).date(), None)]


def test_price_band_deadline_preserves_primary_and_releases_lock_after_unregister(monkeypatch, tmp_path):
    from src.application.filtering_usecase import FilteringDeadlineExceeded
    from src.infrastructure.execution_lock import market_workflow_lock as actual_market_workflow_lock
    from src.infrastructure import execution_lock

    events = []
    saved_results = {}
    saved_diagnostics = []
    unregister_calls = []
    original_datetime = run_filtering.datetime

    class FixedDateTime(original_datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 10, 5, 9, 34)

    @contextmanager
    def process_context(*_args, **_kwargs):
        yield

    @contextmanager
    def tracked_lock():
        with actual_market_workflow_lock() as acquired:
            yield acquired
        events.append(("lock_released",))

    class FakeRepository:
        def __init__(self, directory):
            self.directory = Path(directory)

        def load_for_date(self, _day):
            if self.directory == config.SCREENING_RESULT_DIRECTORY:
                return SimpleNamespace(symbols=["primary-270"])
            return SimpleNamespace(symbols=[f"band-{self.directory.name}"])

        def save(self, result):
            saved_results[self.directory] = result.symbols
            events.append(("result_saved", self.directory))

    class FakeDiagnosticsRepository:
        def __init__(self, directory):
            self.directory = Path(directory)

        def save(self, diagnostic):
            saved_diagnostics.append((self.directory, diagnostic))
            events.append(("diagnostics_saved", self.directory))

    class FakeFilteringUseCase:
        def __init__(self, screening_repository, _board, _volume, result_repository, *_args, **kwargs):
            self.screening_repository = screening_repository
            self.result_repository = result_repository
            self.diagnostics_repository = kwargs.get("diagnostics_repository")

        def execute(self, target_date=None, price_cap=None, **kwargs):
            if price_cap is None:
                self.result_repository.save(SimpleNamespace(symbols=["primary-270"]))
                self.diagnostics_repository.save({"baseline": True})
                return SimpleNamespace(symbols=["primary-270"])
            assert kwargs["deadline_monotonic"] == 100.0
            diagnostic = {
                "price_band": kwargs["price_band"],
                "result_saved": False,
                "summary": {"timed_out": True, "stop_reason": "FILTER_TIME_LIMIT"},
                "candidates": [],
            }
            self.diagnostics_repository.save(diagnostic)
            raise FilteringDeadlineExceeded("injected deadline")

    monkeypatch.setattr(execution_lock, "LOCK_FILE", tmp_path / ".market_workflow.lock")
    monkeypatch.setattr(sys, "argv", ["run_filtering.py"])
    monkeypatch.setattr(run_filtering, "datetime", FixedDateTime)
    monkeypatch.setattr(run_filtering, "monotonic", lambda: 100.0)
    monkeypatch.setattr(run_filtering, "configure_logging", lambda: None)
    monkeypatch.setattr(run_filtering, "is_trading_day", lambda _day: True)
    monkeypatch.setattr(run_filtering, "market_workflow_lock", tracked_lock)
    monkeypatch.setattr(run_filtering, "process_notification", process_context)
    monkeypatch.setattr(run_filtering, "get_api_token", lambda: "token")
    monkeypatch.setattr(run_filtering, "get_token_provider", lambda: SimpleNamespace(recovery_failed=False))
    monkeypatch.setattr(
        run_filtering,
        "BoardRepository",
        lambda _token: SimpleNamespace(get_current_board=lambda symbol: {"symbol": symbol}),
    )
    monkeypatch.setattr(
        run_filtering,
        "unregister_all",
        lambda _token: unregister_calls.append("clear") or events.append(("unregistered",)) or {},
    )
    monkeypatch.setattr(run_filtering, "FilteringUseCase", FakeFilteringUseCase)
    monkeypatch.setattr(run_filtering, "ScreeningResultRepository", FakeRepository)
    monkeypatch.setattr(run_filtering, "FilteringResultRepository", FakeRepository)
    monkeypatch.setattr(run_filtering, "FilteringDiagnosticsRepository", FakeDiagnosticsRepository)
    monkeypatch.setattr(run_filtering, "DecisionJournalRepository", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(run_filtering, "YahooFinanceClient", lambda: object())
    monkeypatch.setattr(config, "SCREENING_ALTERNATE_PRICE_CAPS", (450.0, 900.0))
    monkeypatch.setattr(config, "FILTERING_MORNING_ALTERNATE_BANDS_ENABLED", True)
    monkeypatch.setattr(config, "FILTERING_PRICE_BAND_DEADLINE_TIME", datetime.strptime("09:33", "%H:%M").time())

    run_filtering.main()

    assert saved_results[config.FILTERING_RESULT_DIRECTORY] == ["primary-270"]
    assert len(saved_diagnostics) == 3
    assert [item[1]["price_band"] for item in saved_diagnostics[1:]] == ["450", "900"]
    assert all(item[1]["result_saved"] is False for item in saved_diagnostics[1:])
    assert events.index(("unregistered",)) < events.index(("lock_released",))
    assert len(unregister_calls) == 3
    with actual_market_workflow_lock() as acquired:
        assert acquired is True
