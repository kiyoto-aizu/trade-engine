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
SITE_INDEX = """<!doctype html>
<html lang="ja">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Trade Report</title>
  <style>
    :root { color-scheme: light dark; font-family: system-ui, sans-serif; }
    body { max-width: 58rem; margin: 2rem auto; padding: 0 1rem; }
    #calendar { display: grid; grid-template-columns: repeat(auto-fit, minmax(16rem, 1fr)); gap: 1rem; }
    .month { border: 1px solid #888; border-radius: .5rem; padding: .75rem; }
    .month h2 { margin-top: 0; font-size: 1.1rem; }
    .dates { display: grid; grid-template-columns: repeat(7, 1fr); gap: .35rem; padding: 0; list-style: none; }
    .dates button { width: 100%; padding: .5rem .1rem; cursor: pointer; }
    #report { border: 1px solid #888; border-radius: .5rem; padding: 1rem; min-height: 10rem; }
    h3 { margin: 1.25rem 0 .5rem; font-size: 1rem; }
    .rows { display: grid; grid-template-columns: 12rem 1fr; gap: .4rem 1rem; margin: 0; }
    .rows dt { font-weight: 700; } .rows dd { margin: 0; }
    .bar-row { display: grid; grid-template-columns: 12rem 1fr 3rem; gap: .75rem; align-items: center; margin: .35rem 0; }
    .bar-track { background: rgba(128,128,128,.2); border-radius: .25rem; height: 1rem; }
    .bar { background: #3b82f6; height: 100%; border-radius: .25rem; min-width: 2px; }
    .bar.zero { background: transparent; }
    .trend { display: flex; align-items: flex-end; gap: .75rem; overflow-x: auto; padding-bottom: .25rem; }
    .trend-day { display: flex; flex-direction: column; align-items: center; font-size: .8rem; }
    .trend-bars { display: flex; align-items: flex-end; gap: 2px; height: 8rem; }
    .trend-bars span { width: .8rem; border-radius: .2rem .2rem 0 0; min-height: 2px; }
    .legend { display: flex; gap: 1rem; font-size: .85rem; margin-top: .5rem; }
    .legend i { display: inline-block; width: .8rem; height: .8rem; margin-right: .3rem; border-radius: .2rem; }
    .error { color: #b00020; }
    .charts { display: grid; gap: 1rem; }
    .charts img { width: 100%; height: auto; border: 1px solid #ddd; border-radius: .4rem; }
    @media (max-width: 40rem) { .rows, .bar-row { grid-template-columns: 1fr; } }
  </style>
</head>
<body>
  <h1>Trade Report</h1>
  <p>カレンダーの日付を選択すると、その日の公開用集計データを表示します。</p>
  <div id="calendar" aria-label="公開日カレンダー"></div>
  <section id="report" aria-live="polite">読み込み中です。</section>
  <script>
    const calendar = document.querySelector("#calendar");
    const report = document.querySelector("#report");
    const marketStatus = { available: "判定あり", unavailable: "判定不可", not_evaluated: "未判定", not_recorded: "未記録" };
    const regimes = { NORMAL: "通常", CAUTION: "注意", DANGER: "危険" };
    const skipLabels = { atr_danger: "ATR危険度見送り", market_regime_danger: "市場危険度見送り", market_regime_caution_rsi: "注意レジームRSI見送り" };
    const cache = new Map();
    const el = (tag, text, className) => {
      const node = document.createElement(tag);
      if (text !== undefined) node.textContent = text;
      if (className) node.className = className;
      return node;
    };
    function rows(pairs) {
      const dl = el("dl", undefined, "rows");
      for (const [term, value] of pairs) dl.append(el("dt", term), el("dd", String(value ?? "未記録")));
      return dl;
    }
    function bars(pairs) {
      const box = el("div");
      const max = Math.max(1, ...pairs.map(([, value]) => value));
      for (const [label, value] of pairs) {
        const row = el("div", undefined, "bar-row");
        const track = el("div", undefined, "bar-track");
        const bar = el("div", undefined, value ? "bar" : "bar zero");
        bar.style.width = `${(value / max) * 100}%`;
        track.append(bar);
        row.append(el("span", label), track, el("span", String(value)));
        box.append(row);
      }
      return box;
    }
    function trend(days) {
      const series = [["候補数", "candidate_count", "#94a3b8"], ["評価数", "evaluated_count", "#3b82f6"], ["買いシグナル数", "buy_signal_count", "#16a34a"], ["約定件数", "order_count", "#f59e0b"]];
      const max = Math.max(1, ...days.flatMap((d) => series.map(([, key]) => d[key] || 0)));
      const wrap = el("div", undefined, "trend");
      for (const day of days) {
        const column = el("div", undefined, "trend-day");
        const group = el("div", undefined, "trend-bars");
        for (const [name, key, color] of series) {
          const bar = el("span");
          bar.style.height = `${((day[key] || 0) / max) * 100}%`;
          bar.style.background = color;
          bar.title = `${name}: ${day[key] || 0}`;
          group.append(bar);
        }
        column.append(group, el("span", day.date.slice(5)));
        wrap.append(column);
      }
      const legend = el("div", undefined, "legend");
      for (const [name, , color] of series) {
        const item = el("span");
        const swatch = el("i");
        swatch.style.background = color;
        item.append(swatch, name);
        legend.append(item);
      }
      const box = el("div");
      box.append(wrap, legend);
      return box;
    }
    const chartName = /^intraday_trade_diagnostics(_detail_[0-9A-Z]{4,5})?[.]png$/;
    function chartSection(data) {
      const names = (data.charts || []).filter((name) => chartName.test(name));
      if (!names.length) return [];
      const box = el("div", undefined, "charts");
      for (const name of names) {
        const link = el("a");
        link.href = `charts/${data.date}/${name}`;
        link.target = "_blank";
        link.rel = "noopener";
        const image = el("img");
        image.src = link.href;
        image.alt = name.includes("_detail_") ? "銘柄別の分足診断図" : "分足診断図（全体）";
        image.loading = "lazy";
        link.append(image);
        box.append(link);
      }
      return [el("h3", "分足トレード診断"), box];
    }
    function render(data, history) {
      report.className = "";
      report.replaceChildren(
        el("h3", "概要"),
        rows([
          ["対象日", data.date], ["取引モード", data.trading_mode], ["約定件数", `${data.order_count}件`],
          ["市場データ状態", marketStatus[data.market_status] || data.market_status],
          ["市場レジーム", regimes[data.market_regime] || data.market_regime],
          ["売買なし理由", data.no_trade_reason || "売買あり"], ["公開生成日時", data.published_at],
        ]),
        el("h3", "当日の件数"),
        bars([["候補数", data.candidate_count], ["評価数", data.evaluated_count], ["買いシグナル数", data.buy_signal_count], ["約定件数", data.order_count]]),
        el("h3", "見送り件数"),
        bars(Object.entries(data.skip_counts || {}).map(([key, value]) => [skipLabels[key] || key, value])),
        el("h3", "推移（直近30日）"),
        trend(history),
        ...chartSection(data),
      );
    }
    async function loadDay(day) {
      if (!cache.has(day)) {
        const response = await fetch(`data/${day}.json`, { cache: "no-store" });
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        cache.set(day, await response.json());
      }
      return cache.get(day);
    }
    let allDates = [];
    async function selectDay(day) {
      report.className = "";
      report.textContent = "読み込み中です。";
      try {
        const recent = allDates.slice(0, 30).reverse();
        const history = await Promise.all(recent.map(loadDay));
        render(await loadDay(day), history);
      } catch (error) {
        report.className = "error";
        report.textContent = `データを表示できませんでした: ${error.message}`;
      }
    }
    async function main() {
      try {
        const response = await fetch("data/index.json", { cache: "no-store" });
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        const { dates } = await response.json();
        allDates = dates;
        const months = new Map();
        for (const day of dates) {
          const month = day.slice(0, 7);
          if (!months.has(month)) months.set(month, []);
          months.get(month).push(day);
        }
        for (const [month, monthDates] of months) {
          const section = document.createElement("section");
          section.className = "month";
          const title = document.createElement("h2");
          title.textContent = month;
          const list = document.createElement("ul");
          list.className = "dates";
          for (const day of monthDates) {
            const item = document.createElement("li");
            const button = document.createElement("button");
            button.type = "button"; button.textContent = day.slice(8);
            button.setAttribute("aria-label", day);
            button.addEventListener("click", () => selectDay(day));
            item.append(button); list.append(item);
          }
          section.append(title, list); calendar.append(section);
        }
        if (dates.length) await selectDay(dates[0]);
        else report.textContent = "公開済みデータはまだありません。";
      } catch (error) {
        report.className = "error";
        report.textContent = `公開日一覧を読み込めませんでした: ${error.message}`;
      }
    }
    main();
  </script>
</body>
</html>
"""


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
        payload = public_report_payload(report, trade_date, now=now, charts=charts)
        _push_payload(payload, settings, charts)
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


def _push_payload(payload: dict, settings: PublicationSettings, charts: list[Path] | None = None) -> None:
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
