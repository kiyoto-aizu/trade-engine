import logging
import sys
from contextlib import nullcontext

import pytest

from src.entrypoints import run_monthly_analysis, run_weekly_analysis


@pytest.mark.parametrize(
    "module, arguments, filename, summary_builder",
    [
        (
            run_weekly_analysis,
            ["--force", "--week-start", "2026-10-05"],
            "2026-10-05_2026-10-09.json",
            "build_weekly_summary",
        ),
        (
            run_monthly_analysis,
            ["--force", "--month", "2026-10"],
            "2026-10.json",
            "build_monthly_summary",
        ),
    ],
)
@pytest.mark.parametrize("failure_stage", ["mkdir", "write_json"])
def test_period_analysis_save_failure_still_notifies_findings(
    monkeypatch,
    tmp_path,
    caplog,
    module,
    arguments,
    filename,
    summary_builder,
    failure_stage,
):
    notifications = []
    summary = {"period": {"is_complete": True}, "trend_check": None}
    output = tmp_path / "output"
    if failure_stage == "mkdir":
        output.write_text("not a directory", encoding="utf-8")
    else:
        monkeypatch.setattr(module, "write_json", lambda *_: False)

    monkeypatch.setattr(module, "create_daily_analyzer", lambda: None)
    monkeypatch.setattr(module, "configure_logging", lambda: None)
    monkeypatch.setattr(module, "notify_analysis", notifications.append)
    monkeypatch.setattr(module, "process_notification", lambda *args, **kwargs: nullcontext())
    monkeypatch.setattr(
        module, summary_builder, lambda *args, **kwargs: summary
    )
    monkeypatch.setattr(module, "FilterDecisionRepository", lambda *_: None)
    monkeypatch.setattr(module, "period_notification_lines", lambda *args: ["集計所見", "LLM所見"])
    monkeypatch.setattr(module, "period_conclusion", lambda *args: "期間所見")
    monkeypatch.setattr(
        sys,
        "argv",
        ["analysis", *arguments, "--output", str(output)],
    )

    with caplog.at_level(logging.CRITICAL):
        module.main()

    assert len(notifications) == 1
    assert "集計所見" in notifications[0]
    assert "LLM所見" in notifications[0]
    assert f"分析結果の保存に失敗しました({filename})" in notifications[0]
    assert "詳細:" not in notifications[0]
    assert str(output) not in notifications[0]
    assert "分析結果を保存できません" in caplog.text


@pytest.mark.parametrize(
    "module, arguments, filename, summary_builder",
    [
        (
            run_weekly_analysis,
            ["--force", "--week-start", "2026-10-05"],
            "2026-10-05_2026-10-09.json",
            "build_weekly_summary",
        ),
        (
            run_monthly_analysis,
            ["--force", "--month", "2026-10"],
            "2026-10.json",
            "build_monthly_summary",
        ),
    ],
)
def test_period_analysis_save_success_keeps_detail_path(
    monkeypatch, tmp_path, module, arguments, filename, summary_builder
):
    notifications = []
    summary = {"period": {"is_complete": True}, "trend_check": None}
    output = tmp_path / "output"
    monkeypatch.setattr(module, "create_daily_analyzer", lambda: None)
    monkeypatch.setattr(module, "configure_logging", lambda: None)
    monkeypatch.setattr(module, "notify_analysis", notifications.append)
    monkeypatch.setattr(module, "process_notification", lambda *args, **kwargs: nullcontext())
    monkeypatch.setattr(
        module, summary_builder, lambda *args, **kwargs: summary
    )
    monkeypatch.setattr(module, "FilterDecisionRepository", lambda *_: None)
    monkeypatch.setattr(module, "period_notification_lines", lambda *args: ["集計所見"])
    monkeypatch.setattr(module, "period_conclusion", lambda *args: "期間所見")
    monkeypatch.setattr(
        sys,
        "argv",
        ["analysis", *arguments, "--output", str(output)],
    )

    module.main()

    output_path = output / filename
    assert output_path.exists()
    assert len(notifications) == 1
    assert f"詳細: {output_path}" in notifications[0]
    assert "保存に失敗しました" not in notifications[0]
