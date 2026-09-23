"""search.py: 쿼리 파싱(Outlook/Gmail/한국어 별칭), 날짜 값, 이스케이프,
--whole-word, 검증 단계의 오탐 제거, 0건 진단을 검증한다.
"""

import time
import zlib
from datetime import datetime, timezone

import pytest

from pst_engine.models import MailRecord
from pst_engine.search import (
    MailSearchEngine,
    parse_date_range,
    parse_query,
    parse_size_filter,
)
from pst_engine.storage import MailStorageEngine

# ---------------------------------------------------------------------------
# 쿼리 파서 단위 테스트
# ---------------------------------------------------------------------------


def test_bare_term_has_no_field():
    q = parse_query("계약서")
    assert len(q.clauses) == 1
    assert q.clauses[0].field is None
    assert q.clauses[0].text == "계약서"


@pytest.mark.parametrize("prefix", ["from", "보낸사람"])
def test_from_field_and_korean_alias(prefix):
    q = parse_query(f"{prefix}:김영수")
    assert len(q.clauses) == 1
    assert q.clauses[0].field == "from"
    assert q.clauses[0].text == "김영수"


@pytest.mark.parametrize("prefix", ["subject", "제목"])
def test_subject_field_alias(prefix):
    q = parse_query(f"{prefix}:계약")
    assert q.clauses[0].field == "subject"


@pytest.mark.parametrize("prefix", ["attachments", "filename", "첨부"])
def test_attachments_field_aliases(prefix):
    q = parse_query(f'{prefix}:"계약서.pdf"')
    assert q.clauses[0].field == "attachments"
    assert q.clauses[0].text == "계약서.pdf"


def test_hasattachment_yes_no():
    assert parse_query("hasattachment:yes").has_attachment is True
    assert parse_query("hasattachment:no").has_attachment is False


def test_gmail_style_has_attachment_alias():
    assert parse_query("has:attachment").has_attachment is True


def test_folder_field_and_alias():
    assert parse_query("folder:보낸").folder == ["보낸"]
    assert parse_query("in:보낸").folder == ["보낸"]
    assert parse_query("폴더:보낸").folder == ["보낸"]


def test_folder_repeated_accumulates_instead_of_overwriting():
    # 회귀 방지: 예전엔 두 번째 folder:가 첫 번째를 경고 없이 지워버렸다
    # (실사용자가 "folder:A OR folder:B"를 치면 A가 사라진다고 보고).
    q = parse_query("folder:법무 OR folder:영업")
    assert q.folder == ["법무", "영업"]


def test_folder_between_terms_does_not_leak_pending_or():
    # folder:는 Term을 만들지 않으므로, "OR" 다음에 folder:가 오면
    # pending_or가 리셋되지 않고 다음 무관한 텀까지 잘못 OR로 묶이는
    # 버그가 있었다 — 회귀 방지.
    q = parse_query("계약 OR folder:법무 회의록")
    positives = [t.text for t in q.positive_terms()]
    assert q.or_pairs() == []
    assert positives == ["계약", "회의록"]
    assert any("AND로 처리" in w for w in q.warnings)


def test_negation_dash_prefix():
    q = parse_query("계약 -해지")
    positives = [t.text for t in q.positive_terms()]
    negatives = [t.text for t in q.negative_terms()]
    assert positives == ["계약"]
    assert negatives == ["해지"]


def test_negation_not_keyword():
    q = parse_query("계약 NOT 해지")
    assert [t.text for t in q.negative_terms()] == ["해지"]


def test_or_pairs_adjacent_single_terms():
    q = parse_query("계약 OR 회의록")
    assert len(q.or_pairs()) == 1
    pair = q.or_pairs()[0]
    assert pair.left.text == "계약"
    assert pair.right.text == "회의록"


def test_quoted_phrase_kept_as_one_term():
    q = parse_query('"계약 관련 문서"')
    assert q.clauses[0].text == "계약 관련 문서"


def test_malformed_query_does_not_raise():
    # 괄호/콜론이 이상하게 섞여도 예외 없이 뭔가는 돌려줘야 한다.
    q = parse_query('from: : : "unterminated quote 계약')
    assert isinstance(q.clauses, list)


# ---------------------------------------------------------------------------
# 날짜/용량 값 파서
# ---------------------------------------------------------------------------


def test_date_full_form():
    after, before = parse_date_range("2024-03-15")
    assert before - after == 86400


def test_date_year_month_form():
    after, before = parse_date_range("2024-03")
    d_after = datetime.fromtimestamp(after, tz=timezone.utc)
    d_before = datetime.fromtimestamp(before, tz=timezone.utc)
    assert (d_after.year, d_after.month, d_after.day) == (2024, 3, 1)
    assert (d_before.year, d_before.month, d_before.day) == (2024, 4, 1)


def test_date_year_only_form():
    after, before = parse_date_range("2024")
    d_after = datetime.fromtimestamp(after, tz=timezone.utc)
    d_before = datetime.fromtimestamp(before, tz=timezone.utc)
    assert d_after.year == 2024 and d_before.year == 2025


def test_date_relative_days():
    now = datetime(2024, 6, 15, tzinfo=timezone.utc)
    after, before = parse_date_range("7d", now=now)
    assert before is None
    assert (now.timestamp() - after) == pytest.approx(7 * 86400, abs=1)


def test_date_named_today_and_korean_alias():
    now = datetime(2024, 6, 15, 14, 30, tzinfo=timezone.utc)
    a1, b1 = parse_date_range("today", now=now)
    a2, b2 = parse_date_range("오늘", now=now)
    assert (a1, b1) == (a2, b2)


def test_date_range_with_dotdot_is_inclusive_of_end_day():
    # "A..B"는 B 당일까지 포함한다(사용자가 범위를 적을 때의 직관에
    # 맞춤) — before는 B 다음날 자정(배타적 상한)이 된다.
    after, before = parse_date_range("2024-01-01..2024-02-01")
    d_after = datetime.fromtimestamp(after, tz=timezone.utc)
    d_before = datetime.fromtimestamp(before, tz=timezone.utc)
    assert (d_after.year, d_after.month, d_after.day) == (2024, 1, 1)
    assert (d_before.year, d_before.month, d_before.day) == (2024, 2, 2)


def test_date_unrecognized_returns_none_none():
    assert parse_date_range("완전히 이상한 값") == (None, None)


def test_size_filter_parsing():
    assert parse_size_filter(">1MB") == (">", 1024 * 1024)
    assert parse_size_filter("<500KB") == ("<", 500 * 1024)
    assert parse_size_filter("2GB") == (">=", 2 * 1024**3)
    assert parse_size_filter("not a size") is None


# ---------------------------------------------------------------------------
# 실행 레벨: 검증/오탐 제거/whole-word/0건 진단/혼합 길이
# ---------------------------------------------------------------------------


def _mk(mid, subj, body, frm="a@corp.com", to="b@corp.com", date=1700000000, att="", has_att=0, folder="Inbox"):
    return MailRecord(
        message_id=mid, file_path="a.pst", folder_path=folder, from_addr=frm, to_addr=to, cc_addr="",
        date_utc=date, subject=subj, snippet=body[:300], has_attachment=has_att, attachment_names=att,
        size_bytes=len(body), body_z=zlib.compress(body.encode("utf-8")), raw_body_b64=None,
        decode_status="ok", indexed_at=int(time.time()), locator_json="",
    )


@pytest.fixture
def populated_db(tmp_path, config):
    db_path = tmp_path / "search.db"
    eng = MailStorageEngine(db_path, config)
    eng.upsert_batch([
        _mk(
            "m1", "Q3 계약서 검토 요청", "올해 계약 관련 문서입니다", "김영수 <ys.kim@corp.co.kr>",
            date=1700000000, att="계약서.pdf", has_att=1,
        ),
        _mk("m2", "회의록", "내일 회의 일정 공지", "carol@corp.com", date=1701000000),
        _mk(
            "m3", "AI QA report", "naive cafe report. contract review needed.", "eve@corp.com",
            date=1702000000, att="report.docx", has_att=1,
        ),
        _mk("m4", "personal note", "config file update", "ivy@corp.com", date=1703000000),
    ])
    eng.close()
    return db_path


def test_mixed_length_and_query(populated_db, config):
    with MailSearchEngine(populated_db, config) as se:
        result = se.search("계약 검토")  # 2자 + 2자, 둘 다 bigram 경로
        assert {h.message_id for h in result.hits} == {"m1"}


def test_field_scoped_search(populated_db, config):
    with MailSearchEngine(populated_db, config) as se:
        result = se.search("subject:계약")
        assert {h.message_id for h in result.hits} == {"m1"}


def test_whole_word_filters_partial_substring_match(populated_db, config):
    with MailSearchEngine(populated_db, config) as se:
        without = se.search("con")
        with_ww = se.search("con", whole_word=True)
    # "config"/"contract" 둘 다 "con"의 부분일치로 잡히지만, 독립된
    # 단어 "con"은 어디에도 없다.
    assert {h.message_id for h in without.hits} >= {"m3", "m4"}
    assert with_ww.hits == []
    assert any("whole-word" in w for w in with_ww.warnings)


def test_zero_hit_diagnosis_identifies_culprit(populated_db, config):
    with MailSearchEngine(populated_db, config) as se:
        result = se.search("계약 존재하지않는단어")
    assert result.hits == []
    assert result.diagnosis is not None
    culprits = [d.label for d in result.diagnosis if d.is_culprit]
    assert "존재하지않는단어" in culprits
    assert "계약" not in culprits


def test_has_attachment_filter(populated_db, config):
    with MailSearchEngine(populated_db, config) as se:
        result = se.search("hasattachment:yes")
    assert {h.message_id for h in result.hits} == {"m1", "m3"}


def test_date_after_filter(populated_db, config):
    # date_utc가 실제로 그 이후인 것만 나와야 한다 — 정확한 경계는
    # populated_db의 고정 epoch 값에 의존하지 않고 "필터가 적용됐는가"만
    # 확인한다: 아주 먼 미래로 필터하면 0건이어야 한다.
    with MailSearchEngine(populated_db, config) as se:
        empty = se.search("after:2999-01-01")
    assert empty.hits == []


def test_get_full_returns_body_text_and_all_columns(populated_db, config):
    """search.get_full()은 sqlite3.Row를 dict로 펼치는데, Row는 일반
    dict가 아니라 __iter__가 키가 아닌 값을 내놓는 타입이다 — 그 차이를
    무시하고 ``for k in row``로 순회하면 조용히 IndexError가 난다(실제
    회귀였다). 모든 컬럼이 제대로 채워지는지 직접 검증한다.
    """
    with MailSearchEngine(populated_db, config) as se:
        full = se.get_full("m1")
    assert full is not None
    assert full["subject"] == "Q3 계약서 검토 요청"
    assert full["body_text"] == "올해 계약 관련 문서입니다"
    assert full["attachment_names"] == ["계약서.pdf"]
    assert full["message_id"] == "m1"


def test_get_full_missing_message_returns_none(populated_db, config):
    with MailSearchEngine(populated_db, config) as se:
        assert se.get_full("no-such-id") is None


def test_or_query_matches_either_side(populated_db, config):
    with MailSearchEngine(populated_db, config) as se:
        result = se.search("계약 OR 회의록")
    assert {h.message_id for h in result.hits} == {"m1", "m2"}


def test_not_query_excludes_term(populated_db, config):
    with MailSearchEngine(populated_db, config) as se:
        result = se.search("report NOT contract")
    ids = {h.message_id for h in result.hits}
    assert "m3" not in ids  # m3 본문에 contract가 있으므로 제외돼야 한다


def test_single_char_term_falls_back_without_crashing(populated_db, config):
    with MailSearchEngine(populated_db, config) as se:
        result = se.search("a")  # 1글자 — bigram 불가, 경고만 내고 죽지 않아야 함
    assert isinstance(result.hits, list)
    assert any("1글자" in w for w in result.warnings)


def test_user_input_with_quotes_is_escaped_safely(populated_db, config):
    with MailSearchEngine(populated_db, config) as se:
        # FTS5 구문 오류를 유발할 수 있는 입력이어도 예외 없이 처리돼야 한다.
        result = se.search('계약" OR 1=1 --')
    assert isinstance(result.hits, list)


# ---------------------------------------------------------------------------
# folder: 부분일치 검색 + list_folders() (GUI 폴더 드롭다운의 근거)
# ---------------------------------------------------------------------------


@pytest.fixture
def foldered_db(tmp_path, config):
    db_path = tmp_path / "foldered.db"
    eng = MailStorageEngine(db_path, config)
    eng.upsert_batch([
        _mk("f1", "받은편지함 최상위 메일", "본문1", folder="받은편지함"),
        _mk("f2", "법무팀 계약서", "본문2", folder="받은편지함/법무"),
        _mk("f3", "법무팀 2024", "본문3", folder="받은편지함/법무/2024"),
        _mk("f4", "보낸 메일", "본문4", folder="보낸편지함"),
    ])
    eng.close()
    return db_path


def test_folder_query_matches_substring_across_subfolders(foldered_db, config):
    # "법무"는 f2("받은편지함/법무")와 f3("받은편지함/법무/2024") 양쪽 경로에
    # 부분 문자열로 포함되므로 둘 다 걸려야 한다(정확 일치/접두사 전용이
    # 아니라 README에 문서화된 부분일치 동작).
    with MailSearchEngine(foldered_db, config) as se:
        result = se.search("folder:법무")
    assert {h.message_id for h in result.hits} == {"f2", "f3"}


def test_folder_query_prefix_also_matches_descendants(foldered_db, config):
    # 상위 폴더 이름으로 검색해도(접두사) 그 아래 하위 폴더까지 부분
    # 일치로 함께 걸린다 — f1(정확히 그 폴더)뿐 아니라 f2/f3도 포함.
    with MailSearchEngine(foldered_db, config) as se:
        result = se.search("folder:받은편지함")
    assert {h.message_id for h in result.hits} == {"f1", "f2", "f3"}


def test_folder_query_excludes_unrelated_folder(foldered_db, config):
    with MailSearchEngine(foldered_db, config) as se:
        result = se.search("folder:법무")
    assert "f4" not in {h.message_id for h in result.hits}


def test_folder_or_query_returns_union_of_both_folders(foldered_db, config):
    # 회귀 방지: 예전엔 "folder:A OR folder:B"를 치면 A가 조용히
    # 사라지고 B만 남았다(실사용자 발견 버그) — 이제는 합집합이어야 한다.
    with MailSearchEngine(foldered_db, config) as se:
        result = se.search("folder:법무 OR folder:보낸편지함")
    ids = {h.message_id for h in result.hits}
    assert ids == {"f2", "f3", "f4"}  # f2/f3=법무 계열, f4=보낸편지함


def test_list_folders_returns_sorted_distinct_paths(foldered_db, config):
    with MailSearchEngine(foldered_db, config) as se:
        folders = se.list_folders()
    assert folders == sorted(folders)
    assert set(folders) == {"받은편지함", "받은편지함/법무", "받은편지함/법무/2024", "보낸편지함"}


def test_list_folders_empty_db_returns_empty_list(tmp_path, config):
    db_path = tmp_path / "empty.db"
    MailStorageEngine(db_path, config).close()
    with MailSearchEngine(db_path, config) as se:
        assert se.list_folders() == []
