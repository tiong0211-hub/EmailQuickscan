"""인코딩 폴백 체인 테스트. safe_decode()는 어떤 입력에도 raise하지
않고 (text, status) 튜플을 반환해야 한다.
"""

from pst_engine.encoding import encode_raw_preview, safe_decode


def test_utf8_ok():
    text, status = safe_decode("안녕하세요".encode())
    assert text == "안녕하세요"
    assert status == "ok"


def test_cp949_fallback():
    text, status = safe_decode("안녕하세요".encode("cp949"))
    assert text == "안녕하세요"
    assert status == "fallback"


def test_euckr_fallback():
    text, status = safe_decode("안녕하세요".encode("euc-kr"))
    assert text == "안녕하세요"
    assert status == "fallback"


def test_undecodable_falls_back_to_latin1_and_never_raises():
    # utf-8/cp949/euc-kr 어느 것으로도 깔끔히 안 풀리는 바이트열.
    garbage = bytes([0xFF, 0xFE, 0x00, 0x01, 0x80, 0x81])
    text, status = safe_decode(garbage)
    assert status == "failed"
    # latin-1은 항상 성공하므로 빈 문자열이 아니라 뭔가는 돌아온다.
    assert len(text) == len(garbage)


def test_none_input_never_raises():
    text, status = safe_decode(None)
    assert text == ""
    assert status == "failed"


def test_wrong_type_input_never_raises():
    text, status = safe_decode(12345)  # type: ignore[arg-type]
    assert text == ""
    assert status == "failed"


def test_empty_bytes_is_ok():
    text, status = safe_decode(b"")
    assert text == ""
    assert status == "ok"


def test_raw_body_b64_only_makes_sense_after_failed_status():
    garbage = bytes([0xFF, 0xFE, 0x00, 0x01])
    _text, status = safe_decode(garbage)
    assert status == "failed"
    b64, truncated = encode_raw_preview(garbage, max_bytes=1024)
    assert b64 is not None
    assert truncated is False


def test_raw_body_b64_truncates_over_limit():
    data = b"x" * 1000
    b64, truncated = encode_raw_preview(data, max_bytes=10)
    assert truncated is True
    import base64

    assert len(base64.b64decode(b64)) == 10


def test_raw_body_b64_never_raises_on_bad_input():
    b64, truncated = encode_raw_preview(None, max_bytes=10)
    assert b64 is None
    assert truncated is False
