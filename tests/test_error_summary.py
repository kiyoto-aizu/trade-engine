from src.application.error_summary import error_section_lines, group_errors


def _timeout(symbol, label="GET通信エラー"):
    return (
        f"❌ [{label}] URL: http://localhost:18080/kabusapi/board/{symbol}@1 | "
        "理由: HTTPConnectionPool(host='localhost', port=18080): Read timed out. (read timeout=10)"
    )


def test_same_timeout_errors_are_merged_into_one_line():
    assert error_section_lines([_timeout("4265"), _timeout("2764")]) == [
        "■ エラー 2件",
        "・板情報GETタイムアウト(read timeout=10秒)",
        "  対象: 4265, 2764",
    ]


def test_total_count_is_shown_as_given_and_same_symbol_is_listed_once():
    lines = error_section_lines([_timeout("4265"), _timeout("2764")], total=8)

    assert lines[0] == "■ エラー 8件"
    assert lines[2] == "  対象: 4265, 2764"
    assert group_errors([_timeout("4265")] * 3)[0].count == 3


def test_mixed_kinds_make_separate_lines_ordered_by_count():
    refused = (
        "❌ [GET通信エラー] URL: http://localhost:18080/kabusapi/board/1301@1 | "
        "理由: HTTPConnectionPool(host='localhost', port=18080): Max retries exceeded "
        "(Caused by NewConnectionError: Connection refused)"
    )
    status = "❌ [POST通信エラー] URL: http://localhost:18080/kabusapi/sendorder | 理由: 400 Client Error: Bad Request"

    lines = error_section_lines([refused, _timeout("4265"), _timeout("2764"), status])

    assert lines == [
        "■ エラー 4件",
        "・板情報GETタイムアウト(read timeout=10秒)",
        "  対象: 4265, 2764",
        "・板情報GET接続拒否",
        "  対象: 1301",
        "・sendorder POST HTTP 400",
    ]


def test_notification_never_contains_url_host_port_or_exception_class():
    text = "\n".join(error_section_lines([_timeout("4265")]))

    for forbidden in ("http", "localhost", "18080", "HTTPConnectionPool", "kabusapi"):
        assert forbidden not in text


def test_unknown_errors_are_kept_one_by_one_without_urls():
    odd = "❌ [GET通信エラー] URL: http://localhost:18080/kabusapi/board/1301@1 | 理由: weird"

    lines = error_section_lines(["予期しないエラー: boom", "予期しないエラー: boom", odd])

    assert lines == [
        "■ エラー 3件",
        "・予期しないエラー: boom ×2",
        "・GET通信エラー: weird",
        "  対象: 1301",
    ]


def test_long_unknown_error_is_shortened():
    line = error_section_lines(["x" * 300])[1]

    assert len(line) < 90 and line.endswith("…")


def test_no_errors_shows_none():
    assert error_section_lines([]) == ["■ エラー なし"]
    assert group_errors([]) == []


def test_symbols_are_truncated_with_rest_count():
    lines = error_section_lines([_timeout(str(1000 + i)) for i in range(12)])

    assert lines[2].endswith("1009 他2件")
