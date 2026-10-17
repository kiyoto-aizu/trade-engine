"""板取得のリトライを初回の失敗と機械的に結び付ける実行コンテキスト。"""
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Iterator

_request_id: ContextVar[str | None] = ContextVar("board_request_id", default=None)


@contextmanager
def board_request_context(request_id: str) -> Iterator[None]:
    token = _request_id.set(request_id)
    try:
        yield
    finally:
        _request_id.reset(token)


def current_board_request_id() -> str | None:
    return _request_id.get()
