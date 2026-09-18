"""readpst_parser.py의 순수 MIME 파싱 헬퍼(_extract_bodies /
_extract_attachment_names)를 실제 readpst 실행 없이 단위 테스트한다.
표준 라이브러리 email 패키지만으로 만든 합성 .eml을 쓴다.
"""

import email
import email.message
import email.parser
import email.policy

from pst_engine.parsers.readpst_parser import _extract_attachment_names, _extract_bodies


def _make_multipart_eml() -> email.message.Message:
    """readpst가 실제로 만들어내는 것과 같은 표준 규격 MIME 메일을
    ``EmailMessage``로 올바르게 구성(헤더 인코딩 포함)한 뒤, 실제
    운영 경로와 동일하게 ``compat32`` 정책의 ``BytesParser``로 되읽는다.
    """
    msg = email.message.EmailMessage()
    msg["From"] = "김영수 <ys.kim@corp.co.kr>"
    msg["To"] = "rcpt@corp.co.kr"
    msg["Subject"] = "테스트 메일"
    msg.set_content("본문 내용입니다.")
    msg.add_alternative("<p>본문 HTML</p>", subtype="html")
    msg.add_attachment(b"%PDF-1.4 fake", maintype="application", subtype="pdf", filename="계약서.pdf")
    return email.parser.BytesParser(policy=email.policy.compat32).parsebytes(bytes(msg))


def test_extract_bodies_returns_plain_and_html_as_raw_bytes():
    msg = _make_multipart_eml()
    plain, html = _extract_bodies(msg)
    assert "본문 내용입니다." in plain.decode("utf-8")
    assert html is not None
    assert "본문 HTML" in html.decode("utf-8")


def test_extract_bodies_skips_attachment_parts():
    msg = _make_multipart_eml()
    plain, _html = _extract_bodies(msg)
    assert b"JVBER" not in plain  # base64 첨부 내용이 본문으로 섞이면 안 된다


def test_extract_attachment_names_finds_filename():
    msg = _make_multipart_eml()
    names = _extract_attachment_names(msg)
    assert names == ["계약서.pdf"]


def test_extract_bodies_non_multipart_plain_text():
    raw = (
        "From: a@corp.com\r\nTo: b@corp.com\r\nSubject: 단순 메일\r\n"
        'Content-Type: text/plain; charset="utf-8"\r\n\r\n'
        "그냥 평문입니다.\r\n"
    )
    msg = email.parser.BytesParser(policy=email.policy.compat32).parsebytes(raw.encode("utf-8"))
    plain, html = _extract_bodies(msg)
    assert plain.decode("utf-8") == "그냥 평문입니다.\r\n"
    assert html is None


def test_extract_attachment_names_empty_for_plain_email():
    raw = "From: a@corp.com\r\nTo: b@corp.com\r\nSubject: x\r\n\r\n본문\r\n"
    msg = email.parser.BytesParser(policy=email.policy.compat32).parsebytes(raw.encode("utf-8"))
    assert _extract_attachment_names(msg) == []
