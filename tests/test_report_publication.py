import json
import subprocess
from datetime import date, datetime

from src.infrastructure import report_publication


def _settings(**overrides):
    defaults = {
        "enabled": True,
        "token": "token",
        "repository": "kiyoto-aizu/trade-report",
        "public_url": "https://kiyoto-aizu.github.io/trade-report/",
        "retry_count": 2,
        "retry_wait_seconds": 0,
    }
    defaults.update(overrides)
    return report_publication.PublicationSettings(**defaults)


def test_public_payload_is_an_allowlist_and_excludes_sensitive_report_data():
    payload = report_publication.public_report_payload(
        {
            "date": "2026-10-15",
            "trading_mode": "ペーパートレード",
            "order_count": 2,
            "orders": [{"id": "sensitive-order-id"}],
            "positions": [{"symbol": "1234", "quantity": 100}],
            "realized_profit_loss": 1000,
            "log_errors": {"summaries": ["http://localhost:18080/secret"]},
            "report_text": "internal report",
            "llm_analysis": "internal analysis",
            "market_conditions": {"assessment_status": "available", "regime": "CAUTION", "vix": 23.4},
            "trading_evaluation": {"candidate_count": 4, "evaluated_count": 3, "buy_signal_count": 1},
            "no_trade_reason": {"reason": "その他", "detail": "private internal detail"},
            "atr_danger_skips": [{}, {}],
            "market_regime_danger_skips": [{}],
            "market_regime_caution_rsi_filters": [],
        },
        date(2026, 10, 15),
        now=datetime(2026, 10, 15, 16, 30, tzinfo=report_publication.JST),
    )

    assert payload == {
        "date": "2026-10-15",
        "trading_mode": "ペーパートレード",
        "order_count": 2,
        "market_status": "available",
        "market_regime": "CAUTION",
        "candidate_count": 4,
        "evaluated_count": 3,
        "buy_signal_count": 1,
        "no_trade_reason": "その他",
        "skip_counts": {
            "atr_danger": 2,
            "market_regime_danger": 1,
            "market_regime_caution_rsi": 0,
        },
        "charts": [],
        "published_at": "2026-10-15T16:30:00+09:00",
    }
    serialized = json.dumps(payload, ensure_ascii=False)
    for forbidden in ("sensitive-order-id", "localhost", "private internal", "internal report", "internal analysis", "1000"):
        assert forbidden not in serialized


def test_public_chart_files_only_allow_diagnostic_pngs(tmp_path):
    for name in (
        "intraday_trade_diagnostics.png",
        "intraday_trade_diagnostics_detail_3911.png",
        "intraday_trade_diagnostics.json",
        "intraday_trade_diagnostics.csv",
        "intraday_trade_diagnostics.md",
        "intraday_trade_diagnostics_detail_..png",
        "secret.png",
    ):
        (tmp_path / name).write_bytes(b"x")

    names = [path.name for path in report_publication.public_chart_files(tmp_path)]

    assert names == [
        "intraday_trade_diagnostics.png",
        "intraday_trade_diagnostics_detail_3911.png",
    ]
    assert report_publication.public_chart_files(tmp_path / "missing") == []


def test_push_copies_only_given_charts(monkeypatch, tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    chart = source / "intraday_trade_diagnostics.png"
    chart.write_bytes(b"png")
    work = tmp_path / "work"
    work.mkdir()

    def fake_git(arguments, environment, *, check=True):
        if arguments[0] == "clone":
            (work / "trade-report").mkdir()
        elif arguments[-3:] == ["diff", "--cached", "--quiet"]:
            return subprocess.CompletedProcess(arguments, 0)
        return subprocess.CompletedProcess(arguments, 0)

    class TemporaryDirectory:
        def __init__(self, *_args, **_kwargs):
            pass

        def __enter__(self):
            return str(work)

        def __exit__(self, *_):
            return False

    monkeypatch.setattr(report_publication, "_run_git", fake_git)
    monkeypatch.setattr(report_publication, "_git_auth_environment", lambda token: {})
    monkeypatch.setattr(report_publication.tempfile, "TemporaryDirectory", TemporaryDirectory)
    report_publication._push_payload({"date": "2026-10-15"}, _settings(), [chart])

    copied = work / "trade-report" / "charts" / "2026-10-15" / chart.name
    assert copied.read_bytes() == b"png"


def test_public_chart_data_accepts_only_valid_data_for_the_date(tmp_path):
    day = date(2026, 10, 6)
    good = {
        "version": 1, "data_source": "self_collected", "date": "2026-10-06",
        "market_regime": None, "index": None, "symbols": [],
    }
    path = tmp_path / report_publication.CHART_DATA_NAME

    assert report_publication.public_chart_data(None, day) is None
    assert report_publication.public_chart_data(tmp_path, day) is None

    path.write_text(json.dumps(good), encoding="utf-8")
    assert json.loads(report_publication.public_chart_data(tmp_path, day)) == good

    for bad in (
        {**good, "date": "2026-10-07"},
        {**good, "token": "secret"},
        {**good, "symbols": "x"},
    ):
        path.write_text(json.dumps(bad), encoding="utf-8")
        assert report_publication.public_chart_data(tmp_path, day) is None

    path.write_text("not json", encoding="utf-8")
    assert report_publication.public_chart_data(tmp_path, day) is None


def test_public_chart_data_allows_only_listed_data_sources(tmp_path):
    day = date(2026, 10, 6)
    good = {
        "version": 2, "data_source": "self_collected", "date": "2026-10-06",
        "market_regime": None, "index": None, "symbols": [],
    }
    path = tmp_path / report_publication.CHART_DATA_NAME

    assert report_publication.ALLOWED_CHART_DATA_SOURCES == frozenset({"self_collected"})
    for source in ("jquants", "yahoo", "unknown", "", None, ["self_collected"], 1):
        path.write_text(json.dumps({**good, "data_source": source}), encoding="utf-8")
        assert report_publication.public_chart_data(tmp_path, day) is None

    unrecorded = {key: value for key, value in good.items() if key != "data_source"}
    path.write_text(json.dumps(unrecorded), encoding="utf-8")
    assert report_publication.public_chart_data(tmp_path, day) is None

    path.write_text(json.dumps(good), encoding="utf-8")
    assert report_publication.public_chart_data(tmp_path, day) is not None


def test_push_writes_chart_data_under_data_charts(monkeypatch, tmp_path):
    work = tmp_path / "work"
    work.mkdir()

    def fake_git(arguments, environment, *, check=True):
        if arguments[0] == "clone":
            (work / "trade-report").mkdir()
        return subprocess.CompletedProcess(arguments, 0)

    class TemporaryDirectory:
        def __init__(self, *_args, **_kwargs):
            pass

        def __enter__(self):
            return str(work)

        def __exit__(self, *_):
            return False

    monkeypatch.setattr(report_publication, "_run_git", fake_git)
    monkeypatch.setattr(report_publication, "_git_auth_environment", lambda token: {})
    monkeypatch.setattr(report_publication.tempfile, "TemporaryDirectory", TemporaryDirectory)
    report_publication._push_payload({"date": "2026-10-06"}, _settings(), None, '{"symbols": []}')

    written = work / "trade-report" / "data" / "charts" / "2026-10-06.json"
    assert written.read_text(encoding="utf-8") == '{"symbols": []}'
    index = json.loads((work / "trade-report" / "data" / "index.json").read_text(encoding="utf-8"))
    assert index == {"dates": ["2026-10-06"]}


def test_payload_rejects_mismatched_report_date():
    try:
        report_publication.public_report_payload({"date": "2026-10-14"}, date(2026, 10, 15))
    except ValueError as exc:
        assert "一致" in str(exc)
    else:
        raise AssertionError("日付不一致を拒否すべきです")


def test_publish_failure_does_not_raise_when_token_is_missing(caplog):
    result = report_publication.publish_daily_report(
        {"date": "2026-10-15"},
        date(2026, 10, 15),
        settings=_settings(token=""),
    )

    assert result.attempted is True
    assert result.published is False
    assert result.message.startswith("公開: 失敗")
    assert "トークンが未設定" in caplog.text


def test_publish_rejects_any_repository_other_than_the_allowlisted_target():
    result = report_publication.publish_daily_report(
        {"date": "2026-10-15"},
        date(2026, 10, 15),
        settings=_settings(repository="kiyoto-aizu/another-repository"),
    )

    assert result.published is False
    assert "公開先設定が不正" in result.message


def test_push_writes_only_site_and_allowlisted_data(monkeypatch, tmp_path):
    pushed = []

    def fake_git(arguments, environment, *, check=True):
        if arguments[0] == "clone":
            root = tmp_path / "trade-report"
            root.mkdir()
            (root / "README.md").write_text("existing", encoding="utf-8")
        elif arguments[-3:] == ["diff", "--cached", "--quiet"]:
            return subprocess.CompletedProcess(arguments, 1)
        elif arguments[-3:] == ["push", "origin", "main"]:
            pushed.append(arguments)
        return subprocess.CompletedProcess(arguments, 0)

    def fake_auth(token):
        return {}

    class TemporaryDirectory:
        def __init__(self, *_args, **_kwargs):
            pass

        def __enter__(self):
            return str(tmp_path)

        def __exit__(self, *_):
            return False

    monkeypatch.setattr(report_publication, "_run_git", fake_git)
    monkeypatch.setattr(report_publication, "_git_auth_environment", fake_auth)
    monkeypatch.setattr(report_publication.tempfile, "TemporaryDirectory", TemporaryDirectory)
    report_publication._push_payload(
        {"date": "2026-10-15", "order_count": 1},
        _settings(),
    )

    root = tmp_path / "trade-report"
    assert (root / "index.html").exists()
    assert json.loads((root / "data" / "2026-10-15.json").read_text(encoding="utf-8")) == {
        "date": "2026-10-15",
        "order_count": 1,
    }
    assert json.loads((root / "data" / "index.json").read_text(encoding="utf-8")) == {
        "dates": ["2026-10-15"]
    }
    assert pushed


def test_wait_for_public_url_retries_until_the_site_is_ready(monkeypatch):
    attempts = []

    class Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

    def fake_open(_url, timeout):
        attempts.append(timeout)
        if len(attempts) == 1:
            raise report_publication.URLError("not deployed")
        return Response()

    monkeypatch.setattr(report_publication, "urlopen", fake_open)

    assert report_publication._wait_for_public_url(_settings(), sleeper=lambda _: None) is True
    assert attempts == [10, 10]
