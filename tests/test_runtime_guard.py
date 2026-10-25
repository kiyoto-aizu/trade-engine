import json
import os
import subprocess
from datetime import datetime

import pytest

from tests import runtime_guard
from tests.runtime_guard import (
    RuntimeStateGuard,
    _executable_summary,
    collect_python_processes,
    describe_schedule_overlap,
    watch,
)

pytest_plugins = ("pytester",)


def _make_guard(tmp_path, with_hash=False):
    reports = tmp_path / "production" / "reports" / "daily"
    reports.mkdir(parents=True)
    (reports / "2026-09-04.json").write_text('{"order_count": 0}', encoding="utf-8")
    database = tmp_path / "production" / "state" / "events.sqlite3"
    database.parent.mkdir(parents=True)
    database.write_bytes(b"db")
    return RuntimeStateGuard(reports, database, with_hash=with_hash), reports, database


def _bump_mtime(path, nanoseconds=5_000_000):
    info = path.stat()
    os.utime(path, ns=(info.st_atime_ns, info.st_mtime_ns + nanoseconds))


def test_guard_reports_nothing_when_nothing_changes(tmp_path):
    guard, _, _ = _make_guard(tmp_path)

    assert guard.changes_since(guard.snapshot()) == []


def test_guard_detects_added_removed_and_rewritten_report_files(tmp_path):
    guard, reports, _ = _make_guard(tmp_path)
    before = guard.snapshot()

    (reports / "2026-09-16.json").write_text("{}", encoding="utf-8")
    (reports / "2026-09-04.json").unlink()
    changes = guard.changes_since(before)

    assert any(item.startswith("追加") and "2026-09-16.json" in item for item in changes)
    assert any(item.startswith("削除") and "2026-09-04.json" in item for item in changes)


def test_guard_detects_rewrite_with_identical_content_through_mtime(tmp_path):
    guard, reports, _ = _make_guard(tmp_path)
    before = guard.snapshot()

    target = reports / "2026-09-04.json"
    target.write_text(target.read_text(encoding="utf-8"), encoding="utf-8")
    _bump_mtime(target)

    assert any("更新" in item for item in guard.changes_since(before))


def test_hash_guard_detects_content_change_even_when_mtime_is_restored(tmp_path):
    stat_guard, reports, database = _make_guard(tmp_path)
    hash_guard = RuntimeStateGuard(reports, database, with_hash=True)
    stat_before = stat_guard.snapshot()
    hash_before = hash_guard.snapshot()

    target = reports / "2026-09-04.json"
    original_ns = target.stat().st_mtime_ns
    target.write_text('{"order_count": 9}', encoding="utf-8")
    os.utime(target, ns=(original_ns, original_ns))

    assert hash_guard.changes_since(hash_before) != []
    assert not any("ハッシュ" in item for item in stat_guard.changes_since(stat_before))


def test_guard_detects_database_creation_and_update(tmp_path):
    guard, _, database = _make_guard(tmp_path)
    before = guard.snapshot()

    _bump_mtime(database)

    assert any("DB" in item for item in guard.changes_since(before))
    database.unlink()
    assert any("DB" in item for item in guard.changes_since(before))


def test_watch_fails_the_test_when_production_state_changes(tmp_path):
    guard, reports, _ = _make_guard(tmp_path)

    with pytest.raises(pytest.fail.Exception, match="本番のデータを書き換えました"):
        with watch(guard, "dummy"):
            (reports / "2026-09-16.json").write_text("{}", encoding="utf-8")


def _fail_message(tmp_path, collector):
    guard, reports, _ = _make_guard(tmp_path)
    with pytest.raises(pytest.fail.Exception) as raised:
        with watch(guard, "dummy", process_collector=collector):
            (reports / "2026-09-16.json").write_text("{}", encoding="utf-8")
    return str(raised.value)


def test_violation_message_has_detection_time_mtime_and_processes(tmp_path):
    message = _fail_message(tmp_path, lambda: ["PID 123 開始 2026-10-25 09:31:00 python -m src.entrypoints.run_filtering"])

    assert "検出時刻(ローカル)" in message
    assert "更新日時(ローカル)" in message and "2026-09-16.json" in message
    assert "定時処理との関係" in message
    assert "PID 123" in message and "run_filtering" in message


def test_violation_message_states_process_info_unavailable_and_still_fails(tmp_path):
    def broken():
        raise OSError("denied")

    message = _fail_message(tmp_path, broken)

    assert "取得不可(OSError)" in message
    assert "本番のデータを書き換えました" in message


def test_process_summary_drops_arguments():
    assert _executable_summary('"C:\\Python\\python.exe" -m src.app --token=SECRET123 KEY=abc') == "python.exe -m src.app"
    assert _executable_summary("python.exe run.py --token=SECRET123") == "python.exe run.py"
    assert _executable_summary('"C:\\Program Files\\Python\\python.exe" run.py') == "python.exe run.py"


@pytest.mark.parametrize(
    "command",
    [
        'python -c "API_KEY=abc123; import os"',
        "python --opt SECRET123 run.py",
        "python -X dev SECRET123",
        "python SECRET123",
    ],
)
def test_process_summary_never_leaks_option_values_or_inline_code(command):
    summary = _executable_summary(command)

    assert "SECRET123" not in summary and "abc123" not in summary
    assert summary == "python"


def test_schedule_overlap_uses_minute_granularity():
    assert "取引" in describe_schedule_overlap(datetime(2026, 10, 26, 15, 30, 45))
    assert "フィルタリング" in describe_schedule_overlap(datetime(2026, 10, 26, 9, 34, 30))


def test_schedule_overlap_labels():
    assert "取引" in describe_schedule_overlap(datetime(2026, 10, 26, 10, 0))
    assert "時間帯外" in describe_schedule_overlap(datetime(2026, 10, 26, 8, 0))
    assert "休日" in describe_schedule_overlap(datetime(2026, 10, 25, 10, 0))


def test_collect_python_processes_parses_windows_output_without_arguments(monkeypatch):
    payload = json.dumps(
        [{"pid": 11, "start": "2026-10-25 09:31:00", "cmd": '"C:\\Python\\python.exe" -m src.app --token=SECRET123'}]
    )
    monkeypatch.setattr(runtime_guard.os, "name", "nt")
    monkeypatch.setattr(
        runtime_guard.subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, 0, stdout=payload)
    )

    assert collect_python_processes() == ["PID 11 開始 2026-10-25 09:31:00 python.exe -m src.app"]


def test_collect_python_processes_parses_ps_output_without_arguments(monkeypatch):
    output = "  22 Sun Oct 25 09:31:00 2026 /usr/bin/python3 run.py --token=SECRET123\n  33 Sun Oct 25 09:32:00 2026 /bin/bash -c x\n"
    monkeypatch.setattr(runtime_guard.os, "name", "posix")
    monkeypatch.setattr(
        runtime_guard.subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, 0, stdout=output)
    )

    assert collect_python_processes() == ["PID 22 開始 Sun Oct 25 09:31:00 2026 python3 run.py"]


def test_watch_passes_when_production_state_is_untouched(tmp_path):
    guard, _, _ = _make_guard(tmp_path)

    with watch(guard, "dummy"):
        pass


def test_autouse_fixture_makes_a_test_that_writes_to_the_production_path_fail(pytester):
    pytester.makeconftest(
        """
        import pytest
        from pathlib import Path
        from tests.runtime_guard import RuntimeStateGuard, watch

        PRODUCTION = Path(__file__).parent / "fake_production"
        PRODUCTION.mkdir()
        (PRODUCTION / "existing.json").write_text("{}", encoding="utf-8")

        @pytest.fixture(autouse=True)
        def _guard(request):
            guard = RuntimeStateGuard(PRODUCTION, PRODUCTION / "events.sqlite3")
            with watch(guard, request.node.nodeid):
                yield
        """
    )
    pytester.makepyfile(
        """
        from pathlib import Path

        PRODUCTION = Path(__file__).parent / "fake_production"

        def test_writes_to_production():
            (PRODUCTION / "existing.json").write_text('{"changed": true}', encoding="utf-8")

        def test_leaves_production_alone(tmp_path):
            (tmp_path / "report.json").write_text("{}", encoding="utf-8")
        """
    )

    result = pytester.runpytest()

    # 番人の失敗はテスト終了時(teardown)に出るため、pytestはerrorとして数える。
    result.assert_outcomes(passed=2, errors=1)
    result.stdout.fnmatch_lines(["*本番のデータを書き換えました*"])
