"""storage.py: 스키마/FTS 색인/멱등 upsert/rowid 시간순 배치를 검증한다.

핵심 회귀 방지 대상: "계약"(2자)/"계약서"(3자)/"AI"(2자 영문) 모두 결과를
반환해야 한다 — CLAUDE.md 검증 기준의 핵심이자, 이 프로젝트가 trigram을
버리고 bigram+detail=column을 택한 이유 그 자체다.
"""

import time
import zlib

import pytest

from pst_engine.models import MailRecord
from pst_engine.storage import MailStorageEngine, bigram_tokens


def _mk(mid, subj, body, frm="a@corp.com", to="b@corp.com", cc="", date=1700000000, att="", has_att=0):
    return MailRecord(
        message_id=mid, file_path="a.pst", folder_path="Inbox", from_addr=frm, to_addr=to, cc_addr=cc,
        date_utc=date, subject=subj, snippet=body[:300], has_attachment=has_att, attachment_names=att,
        size_bytes=len(body), body_z=zlib.compress(body.encode("utf-8")), raw_body_b64=None,
        decode_status="ok", indexed_at=int(time.time()), locator_json="",
    )


@pytest.fixture
def engine(tmp_path, config):
    eng = MailStorageEngine(tmp_path / "test.db", config)
    yield eng
    eng.close()


def _match(cur, expr):
    cur.execute(
        "SELECT m.message_id FROM mails m JOIN mails_fts f ON f.rowid=m.rowid WHERE f.mails_fts MATCH ?",
        (expr,),
    )
    return {r[0] for r in cur.fetchall()}


def _and_expr(term):
    return " AND ".join(f'"{t}"' for t in bigram_tokens(term).split())


def test_two_char_korean_search_returns_results(engine):
    engine.upsert_batch([_mk("m1", "Q3 계약서 검토 요청", "올해 계약 관련 문서입니다")])
    cur = engine.conn.cursor()
    assert _match(cur, _and_expr("계약")) == {"m1"}


def test_three_char_korean_search_returns_results(engine):
    engine.upsert_batch([_mk("m1", "Q3 계약서 검토 요청", "본문")])
    cur = engine.conn.cursor()
    assert _match(cur, _and_expr("계약서")) == {"m1"}


def test_two_char_english_search_returns_results(engine):
    engine.upsert_batch([_mk("m1", "AI QA report", "본문")])
    cur = engine.conn.cursor()
    assert _match(cur, _and_expr("AI")) == {"m1"}


def test_attachment_name_is_searchable(engine):
    engine.upsert_batch([_mk("m1", "제목", "본문", att="계약서_최종.pdf")])
    cur = engine.conn.cursor()
    assert _match(cur, "bi_att:(" + _and_expr("계약서") + ")") == {"m1"}


def test_cc_is_searchable(engine):
    engine.upsert_batch([_mk("m1", "제목", "본문", cc="carol@corp.com")])
    cur = engine.conn.cursor()
    assert _match(cur, "bi_cc:(" + _and_expr("carol") + ")") == {"m1"}


def test_upsert_is_idempotent(engine):
    rec = _mk("m1", "제목", "본문")
    engine.upsert_batch([rec])
    engine.upsert_batch([rec])
    engine.upsert_batch([rec])
    cur = engine.conn.cursor()
    cur.execute("SELECT count(*) FROM mails")
    assert cur.fetchone()[0] == 1


def test_upsert_updates_existing_row(engine):
    engine.upsert_batch([_mk("m1", "원래 제목", "본문")])
    engine.upsert_batch([_mk("m1", "바뀐 제목", "본문")])
    cur = engine.conn.cursor()
    cur.execute("SELECT subject FROM mails WHERE message_id='m1'")
    assert cur.fetchone()[0] == "바뀐 제목"
    cur.execute("SELECT count(*) FROM mails")
    assert cur.fetchone()[0] == 1


def test_rowid_orders_by_date_descending(engine):
    engine.upsert_batch([
        _mk("old", "제목", "본문", date=1000),
        _mk("new", "제목", "본문", date=2000),
        _mk("mid", "제목", "본문", date=1500),
    ])
    cur = engine.conn.cursor()
    cur.execute("SELECT message_id FROM mails ORDER BY rowid DESC")
    assert [r[0] for r in cur.fetchall()] == ["new", "mid", "old"]


def test_compute_rowid_collision_is_resolved_by_linear_probe(engine, monkeypatch):
    """서로 다른 message_id가 같은 rowid를 계산해내는 드문 충돌 상황을
    강제로 재현해, upsert가 그래도 안전하게(둘 다 살아남고 중복 없이)
    처리하는지 확인한다.
    """
    import pst_engine.storage as storage_mod

    monkeypatch.setattr(storage_mod, "compute_rowid", lambda message_id, date_utc: 999)

    engine.upsert_batch([_mk("m1", "첫번째", "본문", date=1700000000)])
    engine.upsert_batch([_mk("m2", "두번째", "본문", date=1700000000)])

    cur = engine.conn.cursor()
    cur.execute("SELECT message_id, rowid FROM mails ORDER BY message_id")
    rows = cur.fetchall()
    assert {r[0] for r in rows} == {"m1", "m2"}
    assert rows[0][1] != rows[1][1]  # 서로 다른 rowid를 배정받았어야 한다


def test_verify_candidate_rejects_partial_bigram_false_positive(engine):
    """detail=column의 bigram AND는 상위집합일 뿐이므로, "검토"와 "요청"이
    각각 다른 곳에만 있고 실제로 그 조합 문자열이 없어도 MATCH는 통과할
    수 있다 — verify_candidate가 그런 경우를 걸러내야 한다.
    """
    engine.upsert_batch([_mk("m1", "제목 없음", "요청과 전혀 다른 검토용 문서")])
    cur = engine.conn.cursor()
    rowid = cur.execute("SELECT rowid FROM mails WHERE message_id='m1'").fetchone()[0]
    # 실제로 존재하는 부분 문자열은 통과해야 한다.
    assert engine.verify_candidate(cur, rowid, and_terms=[(None, "검토")])
    # 존재하지 않는 문자열은 걸러야 한다.
    assert not engine.verify_candidate(cur, rowid, and_terms=[(None, "없는문자열조합")])


def test_export_csv_streams_rows(engine, tmp_path):
    engine.upsert_batch([_mk("m1", "제목1", "본문1"), _mk("m2", "제목2", "본문2")])
    cur = engine.conn.cursor()
    cur.execute("SELECT * FROM mails ORDER BY rowid")
    rows = cur.fetchall()
    out = tmp_path / "out.csv"
    count = engine.export_csv(rows, out, ["message_id", "subject"])
    assert count == 2
    content = out.read_text(encoding="utf-8-sig")
    assert "제목1" in content and "제목2" in content


def test_export_eml_excludes_attachments_body_only(engine, tmp_path):
    engine.upsert_batch([_mk("m1", "제목", "본문 내용입니다", att="secret.pdf")])
    cur = engine.conn.cursor()
    cur.execute("SELECT * FROM mails")
    rows = cur.fetchall()
    out_dir = tmp_path / "eml_out"
    count = engine.export_eml(rows, out_dir)
    assert count == 1
    eml_bytes = (out_dir / "m1.eml").read_bytes()
    assert b"secret.pdf" not in eml_bytes  # 첨부는 절대 포함되지 않는다

    import email
    import email.policy

    parsed = email.message_from_bytes(eml_bytes, policy=email.policy.default)
    assert "본문 내용입니다" in parsed.get_content()
