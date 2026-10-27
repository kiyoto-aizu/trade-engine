"""安全な日次サマリーを別のGitHub Pagesリポジトリへ公開する。"""
from __future__ import annotations

import base64
import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Callable
from urllib.error import HTTPError, URLError
from urllib.request import urlopen

from src.domain.no_trade_reason import NO_TRADE_REASONS

logger = logging.getLogger(__name__)

JST = timezone(timedelta(hours=9))
_TRADING_MODES = frozenset({"ペーパートレード", "本番取引"})
_MARKET_STATUSES = frozenset({"available", "unavailable", "not_evaluated", "not_recorded"})
_MARKET_REGIMES = frozenset({"NORMAL", "CAUTION", "DANGER"})
SITE_INDEX = (Path(__file__).with_name("report_site") / "index.html").read_text(encoding="utf-8")


@dataclass(frozen=True)
class PublicationSettings:
    enabled: bool
    token: str
    repository: str
    public_url: str
    retry_count: int
    retry_wait_seconds: float


@dataclass(frozen=True)
class PublicationResult:
    attempted: bool
    published: bool
    message: str


_CHART_NAME = re.compile(r"intraday_trade_diagnostics(_detail_[0-9A-Z]{4,5})?\.png")
CHART_DATA_NAME = "intraday_chart_data.json"
_CHART_DATA_KEYS = frozenset(
    {"version", "data_source", "date", "market_regime", "index", "symbols"}
)
# 公開してよい出どころの許可一覧。J-Quants由来は所有者の承認なしに加えない。
# 出どころの記録が無いデータは、一覧に無いものと同じく公開しない。
ALLOWED_CHART_DATA_SOURCES = frozenset({"self_collected"})
_CHART_DATA_MAX_BYTES = 500_000


def public_chart_data(chart_dir: Path | None, trade_date: date) -> str | None:
    """HTML版の分足診断JSONを検証して返す。不正・巨大・日付違いなら公開しない。"""
    if chart_dir is None:
        return None
    path = chart_dir / CHART_DATA_NAME
    if not path.is_file() or path.stat().st_size > _CHART_DATA_MAX_BYTES:
        return None
    try:
        text = path.read_text(encoding="utf-8")
        data = json.loads(text)
    except (OSError, ValueError):
        return None
    if isinstance(data, dict):
        source = data.get("data_source")
        if source is None:
            logger.warning("分足診断JSONを公開から除外: data_source なし")
            return None
        if not isinstance(source, str) or source not in ALLOWED_CHART_DATA_SOURCES:
            logger.warning("分足診断JSONを公開から除外: data_source が許可リスト外")
            return None
    if (
        not isinstance(data, dict)
        or set(data) - _CHART_DATA_KEYS
        or data.get("date") != trade_date.isoformat()
        or not isinstance(data.get("symbols"), list)
    ):
        return None
    return text


def public_chart_files(chart_dir: Path | None) -> list[Path]:
    """公開許可済みの分足診断画像(pngのみ)を、固定のファイル名形式で絞り込む。"""
    if chart_dir is None or not chart_dir.is_dir():
        return []
    return sorted(
        path for path in chart_dir.iterdir() if path.is_file() and _CHART_NAME.fullmatch(path.name)
    )


def public_report_payload(
    report: dict,
    trade_date: date,
    *,
    now: datetime | None = None,
    charts: list[Path] | None = None,
) -> dict:
    """公開許可済みの集計項目だけを元日報から再構成する。"""
    report_date = report.get("date")
    if report_date and report_date != trade_date.isoformat():
        raise ValueError("日次レポートの日付が公開対象日と一致しません")
    market = report.get("market_conditions")
    evaluation = report.get("trading_evaluation")
    if not isinstance(market, dict):
        market = {}
    if not isinstance(evaluation, dict):
        evaluation = {}
    no_trade = report.get("no_trade_reason")
    no_trade_reason = (
        no_trade.get("reason")
        if isinstance(no_trade, dict) and no_trade.get("reason") in NO_TRADE_REASONS
        else None
    )
    timestamp = (now or datetime.now(JST)).astimezone(JST).isoformat(timespec="seconds")
    return {
        "date": trade_date.isoformat(),
        "trading_mode": _allowed_text(report.get("trading_mode"), _TRADING_MODES),
        "order_count": _non_negative_int(report.get("order_count")),
        "market_status": _allowed_text(market.get("assessment_status"), _MARKET_STATUSES),
        "market_regime": _allowed_text(market.get("regime"), _MARKET_REGIMES),
        "candidate_count": _non_negative_int(evaluation.get("candidate_count")),
        "evaluated_count": _non_negative_int(evaluation.get("evaluated_count")),
        "buy_signal_count": _non_negative_int(evaluation.get("buy_signal_count")),
        "no_trade_reason": no_trade_reason,
        "skip_counts": {
            "atr_danger": _list_count(report.get("atr_danger_skips")),
            "market_regime_danger": _list_count(report.get("market_regime_danger_skips")),
            "market_regime_caution_rsi": _list_count(report.get("market_regime_caution_rsi_filters")),
        },
        "charts": [path.name for path in charts or [] if _CHART_NAME.fullmatch(path.name)],
        "published_at": timestamp,
    }


def _non_negative_int(value: object) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0


def _allowed_text(value: object, allowed: frozenset[str]) -> str:
    return value if isinstance(value, str) and value in allowed else "未記録"


def _list_count(value: object) -> int:
    return len(value) if isinstance(value, list) else 0


def publication_settings_from_environment() -> PublicationSettings:
    return PublicationSettings(
        enabled=os.getenv("TRADE_REPORT_PUBLISH_ENABLED", "false").strip().lower() in {"1", "true", "yes"},
        token=os.getenv("TRADE_REPORT_GITHUB_TOKEN", "").strip(),
        repository=os.getenv("TRADE_REPORT_REPOSITORY", "kiyoto-aizu/trade-report").strip(),
        public_url=os.getenv("TRADE_REPORT_PUBLIC_URL", "https://kiyoto-aizu.github.io/trade-report/").strip(),
        retry_count=int(os.getenv("TRADE_REPORT_PUBLISH_RETRY_COUNT", "12")),
        retry_wait_seconds=float(os.getenv("TRADE_REPORT_PUBLISH_RETRY_WAIT_SECONDS", "10")),
    )


def publish_daily_report(
    report: dict | None,
    trade_date: date,
    *,
    settings: PublicationSettings | None = None,
    now: datetime | None = None,
    sleeper: Callable[[float], None] = time.sleep,
    chart_dir: Path | None = None,
) -> PublicationResult:
    """日次分析を止めずに、公開用JSONのpushとPagesの到達確認を行う。"""
    if settings is None:
        try:
            settings = publication_settings_from_environment()
        except ValueError as exc:
            logger.error("公開設定が不正です: %s", exc)
            return PublicationResult(True, False, "公開: 失敗（公開設定が不正。詳細は内部ログを確認）")
    if not settings.enabled:
        return PublicationResult(False, False, "公開: 未設定（TRADE_REPORT_PUBLISH_ENABLED=false）")
    if report is None:
        return PublicationResult(True, False, "公開: 失敗（日次レポートなし。詳細は内部ログを確認）")
    if not settings.token:
        logger.error("公開用GitHubトークンが未設定です")
        return PublicationResult(True, False, "公開: 失敗（トークン未設定。詳細は内部ログを確認）")
    if settings.repository != "kiyoto-aizu/trade-report":
        logger.error("許可されていない公開先です: %s", settings.repository)
        return PublicationResult(True, False, "公開: 失敗（公開先設定が不正。詳細は内部ログを確認）")
    try:
        charts = public_chart_files(chart_dir)
        chart_data = public_chart_data(chart_dir, trade_date)
        payload = public_report_payload(report, trade_date, now=now, charts=charts)
        _push_payload(payload, settings, charts, chart_data)
        ready = _wait_for_public_url(settings, sleeper=sleeper)
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        stderr = getattr(exc, "stderr", "") or ""
        logger.exception(
            "日次レポートの公開に失敗しました: %s %s",
            exc,
            stderr.replace(settings.token, "***").strip(),
        )
        return PublicationResult(True, False, "公開: 失敗（詳細は内部ログを確認）")

    if ready:
        return PublicationResult(True, True, f"公開: {settings.public_url}")
    return PublicationResult(True, False, "公開: 失敗（Pagesの到達確認がタイムアウト。詳細は内部ログを確認）")


def _push_payload(
    payload: dict,
    settings: PublicationSettings,
    charts: list[Path] | None = None,
    chart_data: str | None = None,
) -> None:
    with tempfile.TemporaryDirectory(prefix="trade-report-") as temporary:
        root = Path(temporary) / "trade-report"
        environment = _git_auth_environment(settings.token)
        _run_git(["clone", f"https://github.com/{settings.repository}.git", str(root)], environment)
        _ensure_site_files(root)
        data_dir = root / "data"
        data_dir.mkdir(exist_ok=True)
        (data_dir / f"{payload['date']}.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        _write_index(data_dir, payload["date"])
        if chart_data:
            (data_dir / "charts").mkdir(exist_ok=True)
            (data_dir / "charts" / f"{payload['date']}.json").write_text(chart_data, encoding="utf-8")
        if charts:
            chart_target = root / "charts" / payload["date"]
            chart_target.mkdir(parents=True, exist_ok=True)
            for chart in charts:
                shutil.copyfile(chart, chart_target / chart.name)
        _run_git(
            ["-C", str(root), "add", "index.html", "data", *(["charts"] if (root / "charts").exists() else [])],
            environment,
        )
        changed = _run_git(["-C", str(root), "diff", "--cached", "--quiet"], environment, check=False)
        if changed.returncode == 0:
            logger.info("公開用データは変更なしです: %s", payload["date"])
            return
        _run_git(["-C", str(root), "config", "user.name", "Trade Report Publisher"], environment)
        _run_git(
            ["-C", str(root), "config", "user.email", "trade-report-publisher@users.noreply.github.com"],
            environment,
        )
        _run_git(["-C", str(root), "commit", "-m", f"Publish report for {payload['date']}"], environment)
        _run_git(["-C", str(root), "-c", "credential.helper=", "push", "origin", "main"], environment)


def _ensure_site_files(root: Path) -> None:
    """画面の改善が公開先へ反映されるよう、index.htmlを常に最新の内容へ揃える。"""
    (root / "index.html").write_text(SITE_INDEX, encoding="utf-8")


def _write_index(data_dir: Path, report_date: str) -> None:
    index_path = data_dir / "index.json"
    dates: list[str] = []
    if index_path.exists():
        loaded = json.loads(index_path.read_text(encoding="utf-8"))
        if isinstance(loaded, dict) and isinstance(loaded.get("dates"), list):
            dates = [item for item in loaded["dates"] if isinstance(item, str)]
    dates = sorted(set(dates) | {report_date}, reverse=True)
    index_path.write_text(json.dumps({"dates": dates}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _git_auth_environment(token: str) -> dict[str, str]:
    """トークンをファイルやコマンドラインに出さず、環境変数の認証ヘッダーでgitへ渡す。"""
    credentials = base64.b64encode(f"x-access-token:{token}".encode("ascii")).decode("ascii")
    environment = os.environ.copy()
    environment.update(
        {
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": "http.https://github.com/.extraheader",
            "GIT_CONFIG_VALUE_0": f"AUTHORIZATION: basic {credentials}",
        }
    )
    return environment


def _run_git(
    arguments: list[str],
    environment: dict[str, str],
    *,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *arguments],
        check=check,
        env=environment,
        text=True,
        capture_output=True,
    )


def _wait_for_public_url(
    settings: PublicationSettings,
    *,
    sleeper: Callable[[float], None],
) -> bool:
    if settings.retry_count < 1:
        raise ValueError("TRADE_REPORT_PUBLISH_RETRY_COUNTは1以上にしてください")
    if settings.retry_wait_seconds < 0:
        raise ValueError("TRADE_REPORT_PUBLISH_RETRY_WAIT_SECONDSは0以上にしてください")
    for attempt in range(1, settings.retry_count + 1):
        try:
            with urlopen(settings.public_url, timeout=10) as response:
                if 200 <= response.status < 400:
                    logger.info("GitHub Pagesの公開を確認しました: 試行=%d", attempt)
                    return True
                logger.warning("GitHub Pagesの応答が成功ではありません: 試行=%d / status=%s", attempt, response.status)
        except (HTTPError, URLError, OSError) as exc:
            logger.info("GitHub Pagesの公開待機中: 試行=%d / %s", attempt, exc)
        if attempt < settings.retry_count:
            sleeper(settings.retry_wait_seconds)
    return False
