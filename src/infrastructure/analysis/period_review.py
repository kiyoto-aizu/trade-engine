"""週次・月次レビューの入力整形と出力検査。"""
from __future__ import annotations

import ast
import copy
import logging
import re
from typing import Any

logger = logging.getLogger(__name__)

_STATUS_LABELS = {
    "available": "判定あり",
    "unavailable": "判定不可",
    "not_evaluated": "未判定",
    "not_recorded": "未記録",
}
_STATUS_PATTERN = re.compile(
    r"(?<![A-Za-z0-9_])(?:not_evaluated|not_recorded|unavailable|available)(?![A-Za-z0-9_])",
    re.IGNORECASE,
)
_CONTAINER_PATTERN = re.compile(r"\{[^{}\n]*\}|\[[^\[\]\n]*\]")
_SNAKE_CASE_PATTERN = re.compile(
    r"(?<![A-Za-z0-9_])[a-z][a-z0-9]*(?:_[a-z0-9]+)+(?![A-Za-z0-9_])"
    r"|(?<![A-Za-z0-9_])[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+(?![A-Za-z0-9_])"
)


def _without_internal_period_data(value: Any) -> Any:
    if isinstance(value, dict):
        result = {}
        for key, item in value.items():
            key_text = str(key)
            normalized_key = key_text.lower()
            if normalized_key in {
                "market_assessment_status",
                "market_assessment_status_counts",
            }:
                continue
            if any(token in normalized_key for token in ("multi_day", "multi-day", "multiday")):
                continue
            if key_text == "filter_decision_events":
                same_day = item.get("same_day") if isinstance(item, dict) else None
                result[key_text] = {
                    "same_day": _without_internal_period_data(same_day)
                } if isinstance(same_day, dict) else {}
                continue
            if key_text == "basis" and isinstance(item, str) and "複数営業日" in item:
                continue
            if isinstance(item, str) and "複数営業日" in item:
                continue
            result[key_text] = _without_internal_period_data(item)
        return result
    if isinstance(value, list):
        return [
            _without_internal_period_data(item)
            for item in value
            if not (isinstance(item, str) and "複数営業日" in item)
        ]
    return value


def prepare_period_review_summary(period_summary: dict) -> dict:
    review_summary = copy.deepcopy(period_summary)
    backtest = review_summary.get("backtest")
    period = review_summary.get("period")
    if isinstance(backtest, dict) and not (
        backtest.get("exact_period_run_available")
        and isinstance(period, dict)
        and period.get("status") == "complete"
    ):
        review_summary["backtest"] = {"exact_period_run_available": False}
    return _without_internal_period_data(review_summary)


def _has_python_container(line: str) -> bool:
    for match in _CONTAINER_PATTERN.finditer(line):
        try:
            value = ast.literal_eval(match.group())
        except (SyntaxError, ValueError):
            continue
        if isinstance(value, (dict, list)):
            return True
    return False


def _container_depth(line: str) -> int:
    depth = 0
    quote = None
    escaped = False
    for character in line:
        if quote:
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == quote:
                quote = None
        elif character in {"'", '"'}:
            quote = character
        elif character in "{[":
            depth += 1
        elif character in "}]":
            depth -= 1
    return depth


def sanitize_period_review(text: str) -> str | None:
    kept_lines = []
    container_depth = 0
    for line in text.splitlines():
        if container_depth:
            container_depth += _container_depth(line)
            container_depth = max(0, container_depth)
            logger.warning("週次・月次LLM評価から内部表現を含む行を除外しました")
            continue
        localized_line = _STATUS_PATTERN.sub(
            lambda match: _STATUS_LABELS[match.group().lower()], line
        )
        if (
            "複数営業日" in line
            or _has_python_container(line)
            or _SNAKE_CASE_PATTERN.search(localized_line)
        ):
            logger.warning("週次・月次LLM評価から内部表現を含む行を除外しました")
            continue
        if _container_depth(line):
            container_depth = _container_depth(line)
            logger.warning("週次・月次LLM評価から内部表現を含む行を除外しました")
            continue
        kept_lines.append(localized_line)
    sanitized = "\n".join(kept_lines).strip()
    return sanitized or None
