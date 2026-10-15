from src.application.error_summary import summarize_errors


def _timeout(symbol, label="GET通信エラー"):
    return (
        f"❌ [{label}] URL: http://localhost:18080/kabusapi/board/{symbol}@1 | "
        "理由: HTTPConnectionPool(host='localhost', port=18080): Read timed out. (read timeout=10)"
    )


def test_same_timeout_errors_are_merged():
    assert summarize_errors([_timeout("4265"), _timeout("2764")]) == [
        "GET通信エラー(板情報取得・10秒タイムアウト) 2件 / 4265, 2764"
    ]


def test_six_symbols_are_truncated_to_five_plus_rest():
    lines = summarize_errors([_timeout(str(1000 + i)) for i in range(6)])
    assert lines == ["GET通信エラー(板情報取得・10秒タイムアウト) 6件 / 1000, 1001, 1002, 1003, 1004 他1件"]


def test_different_kinds_make_separate_lines():
    refused = (
        "❌ [GET通信エラー] URL: http://localhost:18080/kabusapi/board/1301@1 | "
        "理由: HTTPConnectionPool(host='localhost', port=18080): Max retries exceeded "
        "(Caused by NewConnectionError: Connection refused)"
    )
    status = "❌ [POST通信エラー] URL: http://localhost:18080/kabusapi/sendorder | 理由: 400 Client Error: Bad Request"
    lines = summarize_errors([_timeout("4265"), _timeout("2764"), refused, status])
    assert lines == [
        "GET通信エラー(板情報取得・10秒タイムアウト) 2件 / 4265, 2764",
        "GET通信エラー(板情報取得・接続拒否) 1件 / 1301",
        "POST通信エラー(sendorder・HTTP 400) 1件",
    ]


def test_endpoint_without_symbol_does_not_crash():
    message = "❌ [GET通信エラー] URL: http://localhost:18080/kabusapi/positions | 理由: Read timed out. (read timeout=5)"
    assert summarize_errors([message, message]) == ["GET通信エラー(positions・5秒タイムアウト) 2件"]


def test_no_errors_returns_empty():
    assert summarize_errors([]) == []


def test_unknown_error_falls_back_to_original_text():
    assert summarize_errors(["予期しないエラー: boom", "予期しないエラー: boom"]) == ["予期しないエラー: boom 2件"]
    odd = "❌ [GET通信エラー] URL: http://localhost:18080/kabusapi/board/1301@1 | 理由: weird"
    assert summarize_errors([odd]) == [f"{odd} 1件"]
