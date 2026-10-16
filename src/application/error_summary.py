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
