"""通知用にエラーメッセージを同種ごとへ集約する純粋関数群。"""

import re
from collections import OrderedDict
from dataclasses import dataclass

MAX_SYMBOLS = 10
MAX_RAW_LENGTH = 80
_URL_PATTERN = re.compile(r"https?://\S+")
_POOL_PATTERN = re.compile(r"HTTPS?ConnectionPool\([^)]*\):?")
_METHOD_PATTERN = re.compile(r"(GET|POST|PUT|DELETE)")


@dataclass(frozen=True)
class ErrorGroup:
    title: str
    count: int
    symbols: tuple[str, ...] = ()

_API_ERROR_PATTERN = re.compile(
    r"^(?:❌\s*)?\[(?P<label>[^\]]+)\]\s*URL:\s*(?P<url>\S+)\s*\|\s*理由:\s*(?P<reason>.*?)"
    r"(?:\s*\|\s*レスポンス:.*)?$",
    re.DOTALL,
)
_PATH_PATTERN = re.compile(r"^(?:https?://[^/]+)?/kabusapi/(?P<path>[^?#]*)")
_SYMBOL_PATTERN = re.compile(r"^(?P<symbol>[^@/]+)@\d+$")
_TIMEOUT_SECONDS_PATTERN = re.compile(r"read timeout=(\d+(?:\.\d+)?)")
_STATUS_PATTERN = re.compile(r"\b([1-5]\d\d)\s+(?:Client|Server)\s+Error")
_BOARD_TIMEOUT_PATTERN = re.compile(r"/board/(?P<symbol>[^@/]+)@\d+.*Read timed out")
_GROUPED_COUNT_PATTERN = re.compile(r"(?P<count>\d+)件")

_ENDPOINT_LABELS = {"board": "板情報"}


def _split_endpoint(url: str) -> tuple[str, str | None]:
    """URLをエンドポイント種別と銘柄コード(なければNone)に分ける。"""
    match = _PATH_PATTERN.match(url)
    if not match:
        return url.split("?", 1)[0].rstrip("/").rsplit("/", 1)[-1] or "不明", None
    segments = [part for part in match.group("path").split("/") if part]
    if not segments:
        return "不明", None
    symbol = None
    if len(segments) > 1:
        symbol_match = _SYMBOL_PATTERN.match(segments[1])
        if symbol_match:
            symbol = symbol_match.group("symbol")
    return segments[0], symbol


def _classify_reason(reason: str) -> tuple[str, str] | None:
    """理由を(キー, 表示文言)に分類する。分類できなければNone。"""
    if "Read timed out" in reason:
        seconds = _TIMEOUT_SECONDS_PATTERN.search(reason)
        text = f"タイムアウト(read timeout={seconds.group(1)}秒)" if seconds else "タイムアウト"
        return "ReadTimeout", text
    if "Connection refused" in reason or "積極的に拒否" in reason:
        return "ConnectionRefused", "接続拒否"
    status = _STATUS_PATTERN.search(reason)
    if status:
        return f"HTTP{status.group(1)}", f" HTTP {status.group(1)}"
    return None


def _concise(text: str) -> str:
    """URL・接続先情報を除き、1行の短い文にする(詳細はログで確認する)。"""
    text = _URL_PATTERN.sub("", text)
    text = _POOL_PATTERN.sub("", text)
    text = " ".join(text.split())
    return text if len(text) <= MAX_RAW_LENGTH else text[: MAX_RAW_LENGTH - 1] + "…"


def _api_title(label: str, endpoint: str, reason_text: str) -> str:
    method = _METHOD_PATTERN.search(label)
    endpoint_label = _ENDPOINT_LABELS.get(endpoint, f"{endpoint} ")
    return f"{endpoint_label}{method.group(1) if method else label}{reason_text}"


def group_errors(messages: list[str]) -> list[ErrorGroup]:
    """同種のエラーを(エンドポイント種別+エラー種別)で集約し、件数の多い順に返す。"""
    groups: "OrderedDict[tuple, dict]" = OrderedDict()
    for raw in messages:
        message = str(raw).strip()
        if not message:
            continue
        parsed = _API_ERROR_PATTERN.match(message)
        symbol = None
        if parsed:
            endpoint, symbol = _split_endpoint(parsed.group("url"))
            reason = _classify_reason(parsed.group("reason"))
            if reason:
                key = ("api", parsed.group("label"), endpoint, reason[0])
                title = _api_title(parsed.group("label"), endpoint, reason[1])
            else:
                detail = _concise(parsed.group("reason"))
                key = ("api-unknown", parsed.group("label"), endpoint, detail)
                title = f"{parsed.group('label')}: {detail}"
        else:
            title = _concise(message)
            key = ("raw", title)
        group = groups.setdefault(key, {"title": title, "count": 0, "symbols": []})
        group["count"] += 1
        if symbol and symbol not in group["symbols"]:
            group["symbols"].append(symbol)

    ordered = sorted(groups.values(), key=lambda group: -group["count"])
    return [ErrorGroup(g["title"], g["count"], tuple(g["symbols"])) for g in ordered]


def normalize_daily_errors(errors: dict | None) -> dict:
    """新旧日次レポートのエラー情報を通知・期間集計用の形に揃える。"""
    errors = errors if isinstance(errors, dict) else {}
    total_count = int(errors.get("count") or 0)
    timeout = errors.get("board_timeout")
    other = errors.get("other_errors")
    if (
        isinstance(timeout, dict)
        and "count" in timeout
        and isinstance(other, dict)
        and "count" in other
    ):
        return {
            "recorded": "count" in errors,
            "count": total_count,
            "board_timeout": {
                "count": int(timeout.get("count") or 0),
                "recovered": int(timeout.get("recovered") or 0),
                "final_failure": int(timeout.get("final_failure") or 0),
                "unrecorded": int(timeout.get("unrecorded") or 0),
                "symbols": {
                    name: list((timeout.get("symbols") or {}).get(name) or [])
                    for name in ("recovered", "final_failure", "unrecorded")
                },
            },
            "other_errors": {
                "count": int(other.get("count") or 0),
                "groups": [
                    {
                        "title": str(item.get("title") or "その他エラー"),
                        "count": int(item.get("count") or 0),
                    }
                    for item in other.get("groups") or []
                    if isinstance(item, dict)
                ],
            },
        }

    messages = errors.get("unique_messages") or errors.get("summaries") or []
    messages = [str(item) for item in messages]
    grouped_summaries = [str(item) for item in errors.get("grouped_summaries") or []]
    timeout_messages = [message for message in messages if _BOARD_TIMEOUT_PATTERN.search(message)]
    symbols = list(dict.fromkeys(
        match.group("symbol")
        for message in timeout_messages
        if (match := _BOARD_TIMEOUT_PATTERN.search(message))
    ))
    timeout_count = 0
    other_count = 0
    groups = []
    if grouped_summaries:
        for summary in grouped_summaries:
            match = _GROUPED_COUNT_PATTERN.search(summary)
            count = int(match.group("count")) if match else 1
            if "タイムアウト" in summary and ("板情報" in summary or "板取得" in summary):
                timeout_count += count
            else:
                other_count += count
                groups.append({
                    "title": "日次レポート公開失敗" if "日次レポートの公開に失敗" in summary else "その他エラー",
                    "count": count,
                })
    elif timeout_messages and len(timeout_messages) == len(messages):
        timeout_count = total_count
    elif timeout_messages:
        timeout_count = len(timeout_messages)
        other_count = max(0, total_count - timeout_count)
        if other_count:
            groups.append({"title": "詳細未記録", "count": other_count})
    else:
        other_count = total_count
        if other_count:
            groups.append({"title": "詳細未記録", "count": other_count})

    timeout_count = min(timeout_count, total_count)
    other_count = max(0, total_count - timeout_count)
    if other_count and not groups:
        groups.append({"title": "詳細未記録", "count": other_count})
    return {
        "recorded": "count" in errors,
        "count": total_count,
        "board_timeout": {
            "count": timeout_count,
            "recovered": 0,
            "final_failure": 0,
            "unrecorded": timeout_count,
            "symbols": {"recovered": [], "final_failure": [], "unrecorded": symbols},
        },
        "other_errors": {"count": other_count, "groups": groups},
    }


def daily_error_notification_lines(errors: dict | None) -> list[str]:
    normalized = normalize_daily_errors(errors)
    if not normalized["recorded"]:
        return ["■ エラー 未記録"]
    timeout = normalized["board_timeout"]
    other = normalized["other_errors"]
    if not normalized["count"]:
        return ["■ エラー なし"]
    if timeout["count"]:
        lines = [
            f"■ エラー 発生{timeout['count']}件"
            f"(回復{timeout['recovered']}・最終失敗{timeout['final_failure']}・記録なし{timeout['unrecorded']})"
        ]
        for outcome, label in (("final_failure", "最終失敗"), ("unrecorded", "記録なし")):
            outcome_symbols = timeout["symbols"][outcome]
            if outcome_symbols:
                lines.append(f"  {label}対象: {', '.join(outcome_symbols)}")
    else:
        lines = [f"■ エラー {other['count']}件"]
    if other["count"]:
        lines.append(f"・その他エラー {other['count']}件")
        lines.extend(
            f"  {group['title']}: {group['count']}件"
            for group in other["groups"]
        )
    return lines


def error_section_lines(messages: list[str], total: int | None = None) -> list[str]:
    """通知用の「■ エラー N件」セクション。totalは元の総数(省略時はmessages件数)。"""
    groups = group_errors(messages)
    count = total if total is not None else len(messages)
    if not count and not groups:
        return ["■ エラー なし"]
    lines = [f"■ エラー {count}件"]
    for group in groups:
        suffix = f" ×{group.count}" if not group.symbols and group.count > 1 else ""
        lines.append(f"・{group.title}{suffix}")
        if group.symbols:
            shown = ", ".join(group.symbols[:MAX_SYMBOLS])
            if len(group.symbols) > MAX_SYMBOLS:
                shown += f" 他{len(group.symbols) - MAX_SYMBOLS}件"
            lines.append(f"  対象: {shown}")
    return lines
