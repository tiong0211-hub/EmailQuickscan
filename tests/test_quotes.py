"""strip_quoted_reply()가 인용 답장 체인만 제거하고 본문(새로 쓴 내용)은
보존하는지 검증한다. README 발견 4: 실무 메일 본문의 평균 66%가 이
아래에 있다.
"""

from pst_engine.storage import strip_quoted_reply


def test_removes_original_message_block_english():
    body = (
        "새로 쓴 답장 내용입니다.\n\n"
        "-----Original Message-----\n"
        "From: alice@corp.com\n"
        "이전에 보낸 긴 내용...\n"
        "여러 줄에 걸친 인용문\n"
    )
    result = strip_quoted_reply(body)
    assert "새로 쓴 답장 내용입니다." in result
    assert "이전에 보낸 긴 내용" not in result
    assert "From: alice@corp.com" not in result


def test_removes_original_message_block_korean():
    body = "감사합니다.\n\n-----원본 메시지-----\n보낸 사람: 김철수\n예전 내용입니다.\n"
    result = strip_quoted_reply(body)
    assert "감사합니다." in result
    assert "예전 내용입니다" not in result


def test_removes_gt_prefixed_quote_lines():
    body = "새 내용\n> 인용된 이전 줄 1\n> 인용된 이전 줄 2\n새 내용 이어짐"
    result = strip_quoted_reply(body)
    assert "인용된 이전 줄" not in result
    assert "새 내용" in result
    assert "새 내용 이어짐" in result


def test_removes_pipe_prefixed_quote_lines():
    body = "본문\n| 인용줄\n계속되는 본문"
    result = strip_quoted_reply(body)
    assert "인용줄" not in result


def test_body_without_quotes_is_unchanged_content():
    body = "그냥 평범한 메일 본문입니다. 특별한 인용문이 없습니다."
    result = strip_quoted_reply(body)
    assert result == body


def test_empty_body_returns_empty():
    assert strip_quoted_reply("") == ""
    assert strip_quoted_reply(None) == ""  # type: ignore[arg-type]


def test_does_not_over_strip_lines_that_merely_contain_gt_not_at_start():
    body = "1 > 2 는 참입니다.\n이 줄은 인용이 아닙니다."
    result = strip_quoted_reply(body)
    # 줄 맨 앞이 아니라 중간에 있는 '>' 는 인용 표시로 보지 않는다.
    assert "1 > 2 는 참입니다." in result
