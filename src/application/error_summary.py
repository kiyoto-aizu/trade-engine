"""通知用にエラーメッセージを同種ごとへ集約する純粋関数群。"""

import re
from collections import OrderedDict

MAX_SYMBOLS = 5

_API_ERROR_PATTERN = re.compile(
    r"^(?:❌\s*)?\[(?P<label>[^\]]+)\]\s*URL:\s*(?P<url>\S+)\s*\|\s*理由:\s*(?P<reason>.*?)"
    r"(?:\s*\|\s*レスポンス:.*)?$",
    re.DOTALL,
)
_PATH_PATTERN = re.compile(r"^(?:https?://[^/]+)?/kabusapi/(?P<path>[^?#]*)")
_SYMBOL_PATTERN = re.compile(r"^(?P<symbol>[^@/]+)@\d+$")
_TIMEOUT_SECONDS_PATTERN = re.compile(r"read timeout=(\d+(?:\.\d+)?)")
_STATUS_PATTERN = re.compile(r"\b([1-5]\d\d)\s+(?:Client|Server)\s+Error")

_ENDPOINT_LABELS = {"board": "板情報取得"}


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
        return "ReadTimeout", f"{seconds.group(1)}秒タイムアウト" if seconds else "タイムアウト"
    if "Connection refused" in reason or "積極的に拒否" in reason:
        return "ConnectionRefused", "接続拒否"
    status = _STATUS_PATTERN.search(reason)
    if status:
        return f"HTTP{status.group(1)}", f"HTTP {status.group(1)}"
    return None


def summarize_errors(messages: list[str]) -> list[str]:
    """同種のエラーを1行にまとめた概要行を、件数の多い順に返す。"""
    groups: "OrderedDict[tuple, dict]" = OrderedDict()
    for raw in messages:
        message = str(raw).strip()
        parsed = _API_ERROR_PATTERN.match(message)
        reason = _classify_reason(parsed.group("reason")) if parsed else None
        if parsed and reason:
            endpoint, symbol = _split_endpoint(parsed.group("url"))
            key = ("api", parsed.group("label"), endpoint, reason[0])
            title = (
                f"{parsed.group('label')}"
                f"({_ENDPOINT_LABELS.get(endpoint, endpoint)}・{reason[1]})"
            )
        else:
            key = ("raw", message)
            title, symbol = message, None
        group = groups.setdefault(key, {"title": title, "count": 0, "symbols": []})
        group["count"] += 1
        if symbol and symbol not in group["symbols"]:
            group["symbols"].append(symbol)

    ordered = sorted(groups.values(), key=lambda group: -group["count"])
    lines = []
    for group in ordered:
        line = f"{group['title']} {group['count']}件"
        symbols = group["symbols"]
        if symbols:
            shown = ", ".join(symbols[:MAX_SYMBOLS])
            if len(symbols) > MAX_SYMBOLS:
                shown += f" 他{len(symbols) - MAX_SYMBOLS}件"
            line += f" / {shown}"
        lines.append(line)
    return lines
