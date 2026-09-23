"""``MailSearchEngine`` — 쿼리 파싱, MATCH 실행, 결과 렌더링용 데이터 준비.

쓰기 연결을 열지 않는다(역할 경계) — DB는 항상
``file:...?mode=ro``로 연다. ``storage.py``의 ``bigram_tokens`` /
``verify_candidate`` 등 순수 함수는 그대로 import해서 쓴다(그 모듈이
쓰기 연결을 여는 것과 이 모듈이 읽기 전용으로 그 함수를 호출하는 것은
별개다).

쿼리 문법은 README.md "사용자 입력" 절 표를 따른다: Outlook 즉시검색
접두사(from:/subject:/hasattachment: 등)를 기본으로 하고 Gmail·한국어
별칭을 같은 파서로 흡수한다. 지원 범위(문서화된 한계): OR은 인접한 두
단일 텀만 묶는다(중첩 불리언 트리는 지원하지 않는다) — 실무 쿼리
대부분을 커버하면서 구현을 단순하게 유지하기 위한 의도적 범위다.
"""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .config import Config
from .storage import bigram_tokens

# ---------------------------------------------------------------------------
# 필드 별칭
# ---------------------------------------------------------------------------

_FIELD_ALIASES: dict[str, str] = {
    "from": "from", "보낸사람": "from",
    "to": "to", "받는사람": "to",
    "cc": "cc", "참조": "cc",
    "subject": "subject", "제목": "subject",
    "body": "body", "content": "body", "본문": "body",
    "attachments": "attachments", "filename": "attachments", "첨부": "attachments",
    "hasattachment": "hasattachment",
    "folder": "folder", "in": "folder", "폴더": "folder",
    "received": "date", "date": "date", "날짜": "date",
    "after": "date_after", "newer_than": "date_after",
    "before": "date_before", "older_than": "date_before",
    "messagesize": "size", "크기": "size",
    "file": "file",
}

_FTS_FIELDS = {"subject", "from", "to", "cc", "attachments", "body"}
_FTS_COLUMN_FOR_FIELD = {
    "subject": "bi_subject", "body": "bi_body", "from": "bi_from",
    "to": "bi_to", "cc": "bi_cc", "attachments": "bi_att",
}


# ---------------------------------------------------------------------------
# 쿼리 모델
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class Term:
    field: str | None  # None = 맨 텀(전 필드)
    text: str
    negate: bool = False


@dataclass(slots=True)
class OrPair:
    left: Term
    right: Term


@dataclass(slots=True)
class ParsedQuery:
    clauses: list[Term | OrPair] = field(default_factory=list)
    has_attachment: bool | None = None
    folder: str | None = None
    file_substr: str | None = None
    date_after: int | None = None
    date_before: int | None = None
    size_op: tuple[str, int] | None = None
    warnings: list[str] = field(default_factory=list)

    def positive_terms(self) -> list[Term]:
        return [c for c in self.clauses if isinstance(c, Term) and not c.negate]

    def negative_terms(self) -> list[Term]:
        return [c for c in self.clauses if isinstance(c, Term) and c.negate]

    def or_pairs(self) -> list[OrPair]:
        return [c for c in self.clauses if isinstance(c, OrPair)]


# ---------------------------------------------------------------------------
# 토크나이저 + 파서
# ---------------------------------------------------------------------------

_TOKEN_RE = re.compile(
    r'(?P<field1>[A-Za-z_가-힣]+):"(?P<fval_q>[^"]*)"'
    r'|(?P<field2>[A-Za-z_가-힣]+):(?P<fval>[^\s]+)'
    r'|"(?P<phrase>[^"]*)"'
    r'|(?P<word>[^\s]+)'
)


def _tokenize(raw: str) -> list[tuple[str, str | None, str]]:
    """``(kind, field, text)`` 목록. kind는 "field" | "phrase" | "word"."""
    out: list[tuple[str, str | None, str]] = []
    for m in _TOKEN_RE.finditer(raw):
        if m.group("fval_q") is not None:
            # field1/fval_q는 같은 정규식 대안 안에서 함께 매치되므로
            # fval_q가 있으면 field1도 항상 있다 — "or \"\"\"는 그 사실을
            # mypy에게도 알려주기 위한 것뿐, 실제로 빈 문자열이 될 일은 없다.
            out.append(("field", m.group("field1") or "", m.group("fval_q")))
        elif m.group("fval") is not None:
            out.append(("field", m.group("field2") or "", m.group("fval")))
        elif m.group("phrase") is not None:
            out.append(("phrase", None, m.group("phrase")))
        elif m.group("word") is not None:
            out.append(("word", None, m.group("word")))
    return out


def parse_query(raw: str) -> ParsedQuery:
    """쿼리 문자열을 :class:`ParsedQuery`로 파싱한다. 잘못된 문법이 섞여
    있어도 예외를 내지 않는다 — 이해 못한 조각은 ``warnings``에 적어두고
    무시하거나 맨 텀으로 취급한다(규칙 5의 정신을 검색에도 적용: 사용자
    입력 때문에 파이프라인이 멎지 않는다).
    """
    q = ParsedQuery()
    pending_negate = False
    pending_or = False

    for kind, tfield, text in _tokenize(raw):
        if kind == "word" and tfield is None:
            low = text.lower()
            if low == "and":
                continue
            if low == "or":
                pending_or = True
                continue
            if low == "not":
                pending_negate = True
                continue
            if text.startswith("-") and len(text) > 1:
                text = text[1:]
                pending_negate = True

        clause: Term | None = None

        if kind == "field":
            assert tfield is not None  # _tokenize가 kind="field"일 때 항상 채워 준다
            canonical = _FIELD_ALIASES.get(tfield.lower())
            if canonical == "hasattachment":
                q.has_attachment = _parse_bool(text)
            elif tfield.lower() == "has" and text.strip().lower() in ("attachment", "attachments", "yes", "true"):
                q.has_attachment = True
            elif canonical == "folder":
                q.folder = text
            elif canonical == "file":
                q.file_substr = text
            elif canonical == "date":
                after, before = parse_date_range(text)
                if after is None and before is None:
                    q.warnings.append(f"날짜 값을 이해하지 못해 무시했습니다: {text!r}")
                else:
                    if after is not None:
                        q.date_after = after
                    if before is not None:
                        q.date_before = before
            elif canonical == "date_after":
                after, _ = parse_date_range(text)
                if after is not None:
                    q.date_after = after
                else:
                    q.warnings.append(f"날짜 값을 이해하지 못해 무시했습니다: {text!r}")
            elif canonical == "date_before":
                before, _ = parse_date_range(text)
                if before is None:
                    before, _ = parse_date_range(text)  # 같은 함수, before 없으면 아래에서 처리
                if before is not None:
                    q.date_before = before
                else:
                    q.warnings.append(f"날짜 값을 이해하지 못해 무시했습니다: {text!r}")
            elif canonical == "size":
                parsed = parse_size_filter(text)
                if parsed is not None:
                    q.size_op = parsed
                else:
                    q.warnings.append(f"용량 값을 이해하지 못해 무시했습니다: {text!r}")
            elif canonical in _FTS_FIELDS:
                clause = Term(field=canonical, text=text, negate=pending_negate)
                pending_negate = False
            else:
                # 모르는 접두사는 "field:value" 문자열 자체를 맨 텀으로.
                clause = Term(field=None, text=f"{tfield}:{text}", negate=pending_negate)
                pending_negate = False
        else:
            clause = Term(field=None, text=text, negate=pending_negate)
            pending_negate = False

        if clause is None:
            continue

        prev_is_or_pairable = (
            pending_or and q.clauses and isinstance(q.clauses[-1], Term) and not q.clauses[-1].negate
        )
        if prev_is_or_pairable and not clause.negate:
            prev = q.clauses.pop()
            assert isinstance(prev, Term)  # prev_is_or_pairable에서 이미 확인됨
            q.clauses.append(OrPair(prev, clause))
            pending_or = False
        else:
            if pending_or:
                q.warnings.append("OR 앞뒤가 비교 가능한 단일 텀이 아니어서 AND로 처리했습니다")
                pending_or = False
            q.clauses.append(clause)

    return q


def _parse_bool(text: str) -> bool | None:
    low = text.strip().lower()
    if low in ("yes", "true", "1"):
        return True
    if low in ("no", "false", "0"):
        return False
    return None


# ---------------------------------------------------------------------------
# 날짜/용량 값 파서
# ---------------------------------------------------------------------------

_RELATIVE_RE = re.compile(r"^(\d+)([dmy])$", re.IGNORECASE)
_NAMED_SINGLE = {
    "today": 0, "오늘": 0,
    "yesterday": 1, "어제": 1,
}
_YEAR_RE = re.compile(r"^\d{4}$")
_YEAR_MONTH_RE = re.compile(r"^\d{4}-\d{2}$")
_FULL_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _day_start(dt: datetime) -> int:
    return int(dt.replace(hour=0, minute=0, second=0, microsecond=0).timestamp())


def parse_date_range(value: str, now: datetime | None = None) -> tuple[int | None, int | None]:
    """날짜 표현을 ``(after_epoch, before_epoch)`` 반 개방구간으로.

    지원: ``YYYY``, ``YYYY-MM``, ``YYYY-MM-DD``, 범위
    ``YYYY-MM-DD..YYYY-MM-DD``, 상대 ``7d``/``3m``/``1y``, 그리고
    today/yesterday/thisweek/lastweek/thismonth/thisyear와 한국어
    별칭(오늘/어제/이번주/지난주/이번달/올해). 이해 못하면
    ``(None, None)``을 돌려준다 — 호출자가 경고만 남기고 필터 없이
    진행한다.
    """
    now = now or datetime.now(timezone.utc)
    v = value.strip().lower()

    if ".." in v:
        a_str, _, b_str = v.partition("..")
        a, _ = parse_date_range(a_str, now)
        b, b_end = parse_date_range(b_str, now)
        # 범위의 끝은 b_str이 가리키는 구간의 "끝"(exclusive)을 쓴다.
        return a, (b_end if b_end is not None else b)

    m = _RELATIVE_RE.match(v)
    if m:
        n, unit = int(m.group(1)), m.group(2)
        days = {"d": n, "m": n * 30, "y": n * 365}[unit]
        return int((now - timedelta(days=days)).timestamp()), None

    if v in ("thisweek", "이번주"):
        start = _day_start(now - timedelta(days=now.weekday()))
        return start, start + 7 * 86400
    if v in ("lastweek", "지난주"):
        this_week = _day_start(now - timedelta(days=now.weekday()))
        return this_week - 7 * 86400, this_week
    if v in ("thismonth", "이번달"):
        start = _day_start(now.replace(day=1))
        nxt = (
            now.replace(year=now.year + 1, month=1, day=1)
            if now.month == 12
            else now.replace(month=now.month + 1, day=1)
        )
        return start, _day_start(nxt)
    if v in ("thisyear", "올해"):
        start = _day_start(now.replace(month=1, day=1))
        end = _day_start(now.replace(year=now.year + 1, month=1, day=1))
        return start, end
    if v in _NAMED_SINGLE:
        base = now - timedelta(days=_NAMED_SINGLE[v])
        start = _day_start(base)
        return start, start + 86400

    if _YEAR_RE.match(v):
        y = int(v)
        start = _day_start(datetime(y, 1, 1, tzinfo=timezone.utc))
        end = _day_start(datetime(y + 1, 1, 1, tzinfo=timezone.utc))
        return start, end
    if _YEAR_MONTH_RE.match(v):
        y, mo = (int(x) for x in v.split("-"))
        start = _day_start(datetime(y, mo, 1, tzinfo=timezone.utc))
        end_y, end_mo = (y + 1, 1) if mo == 12 else (y, mo + 1)
        end = _day_start(datetime(end_y, end_mo, 1, tzinfo=timezone.utc))
        return start, end
    if _FULL_DATE_RE.match(v):
        y, mo, d = (int(x) for x in v.split("-"))
        start = _day_start(datetime(y, mo, d, tzinfo=timezone.utc))
        return start, start + 86400

    return None, None


_SIZE_RE = re.compile(r"^([<>]=?)?\s*([\d.]+)\s*(b|kb|mb|gb)?$", re.IGNORECASE)
_SIZE_UNITS = {"b": 1, "kb": 1024, "mb": 1024**2, "gb": 1024**3}


def parse_size_filter(value: str) -> tuple[str, int] | None:
    m = _SIZE_RE.match(value.strip())
    if not m:
        return None
    op = m.group(1) or ">="
    num = float(m.group(2))
    unit = (m.group(3) or "b").lower()
    return op, int(num * _SIZE_UNITS[unit])


_SIZE_OPS = {
    ">": lambda col, val: (f"{col} > ?", val),
    ">=": lambda col, val: (f"{col} >= ?", val),
    "<": lambda col, val: (f"{col} < ?", val),
    "<=": lambda col, val: (f"{col} <= ?", val),
}


# ---------------------------------------------------------------------------
# FTS5 MATCH 식 빌드
# ---------------------------------------------------------------------------


def _fts_quote(tok: str) -> str:
    return '"' + tok.replace('"', '""') + '"'


def _term_fts_expr(term: Term) -> str | None:
    """텀 하나를 FTS5 MATCH 조각으로. bigram이 하나도 안 나오면(1글자
    텀 등) ``None`` — 그 텀은 MATCH에 못 넣고 검증 단계에서만 걸러진다.
    """
    tokens = bigram_tokens(term.text).split()
    if not tokens:
        return None
    inner = " AND ".join(_fts_quote(t) for t in tokens)
    if term.field and term.field in _FTS_COLUMN_FOR_FIELD:
        return f"{_FTS_COLUMN_FOR_FIELD[term.field]}:({inner})"
    return inner


def build_match_expression(query: ParsedQuery) -> tuple[str | None, list[str]]:
    """쿼리의 텀들로 FTS5 MATCH 식을 만든다.

    반환: ``(match_expr 또는 None, "이 식으로는 못 담아 검증에만 쓸 텀 목록")``.
    ``match_expr``이 None이면 FTS 후보 좁히기를 쓸 수 없다는 뜻 — 호출자
    (search 실행부)가 전체 스캔 폴백으로 넘어간다.
    """
    positive_parts: list[str] = []
    skipped_short: list[str] = []

    for t in query.positive_terms():
        expr = _term_fts_expr(t)
        if expr is None:
            skipped_short.append(t.text)
        else:
            positive_parts.append(expr)

    for pair in query.or_pairs():
        left = _term_fts_expr(pair.left)
        right = _term_fts_expr(pair.right)
        if left and right:
            positive_parts.append(f"({left} OR {right})")
        elif left or right:
            positive_parts.append(left or right)  # type: ignore[arg-type]
        # 둘 다 너무 짧으면 이 OR 쌍은 FTS에 기여하지 못한다 — verify에서
        # or_groups로 여전히 걸러진다(search_engine이 처리).

    negative_parts: list[str] = []
    for t in query.negative_terms():
        expr = _term_fts_expr(t)
        if expr is not None:
            negative_parts.append(expr)
        # 너무 짧은 NOT 텀은 FTS로 못 거르고 검증 단계에서만 걸러진다.

    if not positive_parts:
        return None, skipped_short

    expr = " AND ".join(positive_parts)
    if negative_parts:
        expr = f"{expr} NOT ({' OR '.join(negative_parts)})"
    return expr, skipped_short


# ---------------------------------------------------------------------------
# 검색 결과
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class SearchHit:
    rowid: int
    message_id: str
    subject: str
    from_addr: str
    to_addr: str
    date_utc: int
    snippet: str
    has_attachment: bool
    attachment_names: list[str]
    folder_path: str
    file_path: str


@dataclass(slots=True)
class DiagnosisEntry:
    label: str
    count_display: str  # "1000+건" | "37건" | "0건"
    is_culprit: bool


@dataclass(slots=True)
class SearchResult:
    hits: list[SearchHit]
    more_available: bool
    elapsed_ms: float
    warnings: list[str]
    used_relevance_sort: bool
    diagnosis: list[DiagnosisEntry] | None = None


class MailSearchEngine:
    """읽기 전용 연결만 연다. 쓰기 연결을 여는 메서드는 없다(역할 경계)."""

    def __init__(self, db_path: str | Path, config: Config) -> None:
        self.db_path = Path(db_path)
        self.config = config
        uri = f"file:{self.db_path.as_posix()}?mode=ro"
        self.conn = sqlite3.connect(uri, uri=True)
        self.conn.execute("PRAGMA query_only=ON")
        self.conn.row_factory = sqlite3.Row

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> MailSearchEngine:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- 공개 API ------------------------------------------------------

    def list_folders(self) -> list[str]:
        """인덱싱된 메일들의 고유 ``folder_path`` 목록(정렬됨, 빈 문자열 제외).

        ``idx_folder`` 인덱스를 타는 단순 DISTINCT 스캔이라 100만 건
        규모에서도 빠르다. GUI 폴더 드롭다운을 채우는 용도로만 쓰이며,
        검색 경로 자체(부분일치 LIKE)에는 관여하지 않는다.
        """
        cur = self.conn.cursor()
        cur.execute("SELECT DISTINCT folder_path FROM mails WHERE folder_path != '' ORDER BY folder_path")
        return [row[0] for row in cur.fetchall()]

    def search(
        self,
        raw_query: str,
        *,
        limit: int = 20,
        sort: str = "date",
        whole_word: bool = False,
        offset_rowid: int | None = None,
    ) -> SearchResult:
        import time

        t0 = time.time()
        query = parse_query(raw_query)
        match_expr, skipped_short = build_match_expression(query)

        if skipped_short:
            query.warnings.append(
                f"1글자 검색어는 느릴 수 있습니다: {', '.join(skipped_short)!r}"
            )

        and_terms = [(t.field, t.text) for t in query.positive_terms()]
        not_terms = [(t.field, t.text) for t in query.negative_terms()]
        or_groups = [((p.left.field, p.left.text), (p.right.field, p.right.text)) for p in query.or_pairs()]

        cur = self.conn.cursor()

        use_relevance = sort == "relevance" and match_expr is not None
        if sort == "relevance" and match_expr is None:
            query.warnings.append("관련도 정렬은 검색어가 없어 날짜순으로 대체합니다")

        scan_limit = self.config.relevance_candidates if use_relevance else max(limit * 20, 500)

        rows = self._scan_candidates(cur, match_expr, query, scan_limit, offset_rowid)

        # verify_candidate는 인스턴스 메서드이지만 읽기 전용 커서로도
        # 안전하다(SELECT만 함). 쓰기 연결을 새로 열지 않기 위해
        # MailStorageEngine을 생성하지 않고, 설정만 담은 껍데기로 같은
        # 메서드를 바인딩해 재사용한다(아래 _make_verifier/_make_text_fetcher).
        verify_fn = _make_verifier(self.config)
        text_fetcher = _make_text_fetcher(self.config) if whole_word else None

        verified: list[sqlite3.Row] = []
        removed_by_whole_word = 0
        for row in rows:
            if not verify_fn(cur, row["rowid"], and_terms, or_groups, not_terms):
                continue
            if whole_word and text_fetcher is not None and not _whole_word_ok(cur, row["rowid"], query, text_fetcher):
                removed_by_whole_word += 1
                continue
            verified.append(row)

        more_available = len(verified) > limit
        page = verified[:limit]

        if use_relevance:
            assert match_expr is not None  # use_relevance 계산식에서 이미 보장됨
            page = self._bm25_sort(cur, match_expr, [r["rowid"] for r in page])

        hits = [self._row_to_hit(r) for r in page]

        diagnosis = None
        if not hits:
            if removed_by_whole_word > 0:
                query.warnings.append(
                    f"--whole-word 옵션이 {removed_by_whole_word}건을 제외해 결과가 0건이 됐습니다 "
                    f"(단어 일부만 일치). 옵션을 끄면 다시 나타날 수 있습니다."
                )
            diagnosis = self._diagnose(cur, query)

        elapsed_ms = (time.time() - t0) * 1000
        return SearchResult(
            hits=hits,
            more_available=more_available,
            elapsed_ms=elapsed_ms,
            warnings=query.warnings,
            used_relevance_sort=use_relevance,
            diagnosis=diagnosis,
        )

    # -- 내부 구현 -------------------------------------------------------

    def _scan_candidates(
        self,
        cur: sqlite3.Cursor,
        match_expr: str | None,
        query: ParsedQuery,
        scan_limit: int,
        offset_rowid: int | None,
    ) -> list[sqlite3.Row]:
        where: list[str] = []
        params: list[object] = []

        if match_expr is not None:
            where.append("f.mails_fts MATCH ?")
            params.append(match_expr)
        if offset_rowid is not None:
            where.append("m.rowid < ?")
            params.append(offset_rowid)
        if query.has_attachment is not None:
            where.append("m.has_attachment = ?")
            params.append(1 if query.has_attachment else 0)
        if query.folder:
            where.append("m.folder_path LIKE ?")
            params.append(f"%{query.folder}%")
        if query.file_substr:
            where.append("m.file_path LIKE ?")
            params.append(f"%{query.file_substr}%")
        if query.date_after is not None:
            where.append("m.date_utc >= ?")
            params.append(query.date_after)
        if query.date_before is not None:
            where.append("m.date_utc < ?")
            params.append(query.date_before)
        if query.size_op is not None:
            op, val = query.size_op
            clause, bound = _SIZE_OPS.get(op, _SIZE_OPS[">="])("m.size_bytes", val)
            where.append(clause)
            params.append(bound)

        join = "JOIN mails_fts f ON f.rowid = m.rowid" if match_expr is not None else ""
        where_sql = f"WHERE {' AND '.join(where)}" if where else ""
        sql = (
            f"SELECT m.rowid, m.message_id, m.subject, m.from_addr, m.to_addr, m.date_utc, "
            f"m.snippet, m.has_attachment, m.attachment_names, m.folder_path, m.file_path "
            f"FROM mails m {join} {where_sql} ORDER BY m.rowid DESC LIMIT ?"
        )
        params.append(scan_limit)
        cur.execute(sql, params)
        return cur.fetchall()

    def _bm25_sort(self, cur: sqlite3.Cursor, match_expr: str, rowids: list[int]) -> list[sqlite3.Row]:
        if not rowids:
            return []
        placeholders = ",".join("?" for _ in rowids)
        cur.execute(
            f"SELECT m.rowid, m.message_id, m.subject, m.from_addr, m.to_addr, m.date_utc, "
            f"m.snippet, m.has_attachment, m.attachment_names, m.folder_path, m.file_path, "
            f"bm25(mails_fts) AS score "
            f"FROM mails m JOIN mails_fts f ON f.rowid = m.rowid "
            f"WHERE f.mails_fts MATCH ? AND m.rowid IN ({placeholders}) ORDER BY score ASC",
            [match_expr, *rowids],
        )
        return cur.fetchall()

    def _row_to_hit(self, row: sqlite3.Row) -> SearchHit:
        names = [n for n in (row["attachment_names"] or "").split("; ") if n]
        return SearchHit(
            rowid=row["rowid"],
            message_id=row["message_id"],
            subject=row["subject"],
            from_addr=row["from_addr"],
            to_addr=row["to_addr"],
            date_utc=row["date_utc"],
            snippet=row["snippet"],
            has_attachment=bool(row["has_attachment"]),
            attachment_names=names,
            folder_path=row["folder_path"],
            file_path=row["file_path"],
        )

    def get_full(self, message_id: str) -> dict | None:
        """``show`` 명령용: 본문 전문을 포함한 전체 레코드를 돌려준다."""
        cur = self.conn.cursor()
        cur.execute("SELECT * FROM mails WHERE message_id=?", (message_id,))
        row = cur.fetchone()
        if row is None:
            return None
        from .storage import _decompress_body  # 순수 함수 재사용

        body_text = _decompress_body(row["body_z"])
        # sqlite3.Row는 dict가 아니다 — __iter__는 키가 아니라 값을
        # 내놓으므로(튜플처럼) row.keys()를 명시적으로 써야 한다.
        # ruff의 SIM118("dict처럼 in dict.keys() 대신 in dict")은 실제
        # dict에만 맞는 제안이라 여기 적용하면 안 된다.
        result = {k: row[k] for k in row.keys()}  # noqa: SIM118
        result["body_text"] = body_text
        result["attachment_names"] = [n for n in (row["attachment_names"] or "").split("; ") if n]
        return result

    # -- 0건 진단 ---------------------------------------------------------

    def _diagnose(self, cur: sqlite3.Cursor, query: ParsedQuery) -> list[DiagnosisEntry]:
        """결과가 0건일 때, 조건을 하나씩 단독으로 다시 세어 어느 조건이
        원인인지 보여준다. 각 프로브는 ``LIMIT 1000``으로 상한을 둬
        희귀어라도 느려지지 않게 한다(실측: 2자 count(*)가 300k행에서
        105ms — 상한 없는 count는 이 규모에서도 느리다).
        """
        entries: list[DiagnosisEntry] = []
        probe_limit = 1000

        for t in query.positive_terms():
            expr = _term_fts_expr(t)
            label = f"{t.field + ':' if t.field else ''}{t.text}"
            if expr is None:
                entries.append(DiagnosisEntry(label, "1글자(정밀 세기 불가)", False))
                continue
            n = self._probe_count(cur, expr, probe_limit)
            entries.append(DiagnosisEntry(label, _fmt_count(n, probe_limit), n == 0))

        if query.folder:
            n = self._probe_count_sql(
                cur, "SELECT rowid FROM mails WHERE folder_path LIKE ? LIMIT ?", [f"%{query.folder}%", probe_limit]
            )
            entries.append(DiagnosisEntry(f"folder:{query.folder}", _fmt_count(n, probe_limit), n == 0))
        if query.has_attachment is not None:
            n = self._probe_count_sql(
                cur,
                "SELECT rowid FROM mails WHERE has_attachment=? LIMIT ?",
                [1 if query.has_attachment else 0, probe_limit],
            )
            has_label = f"hasattachment:{'yes' if query.has_attachment else 'no'}"
            entries.append(DiagnosisEntry(has_label, _fmt_count(n, probe_limit), n == 0))
        if query.date_after is not None or query.date_before is not None:
            conds, params = [], []
            if query.date_after is not None:
                conds.append("date_utc >= ?")
                params.append(query.date_after)
            if query.date_before is not None:
                conds.append("date_utc < ?")
                params.append(query.date_before)
            params.append(probe_limit)
            n = self._probe_count_sql(cur, f"SELECT rowid FROM mails WHERE {' AND '.join(conds)} LIMIT ?", params)
            entries.append(DiagnosisEntry("날짜 조건", _fmt_count(n, probe_limit), n == 0))

        return entries

    def _probe_count(self, cur: sqlite3.Cursor, match_expr: str, limit: int) -> int:
        cur.execute(
            "SELECT count(*) FROM (SELECT rowid FROM mails_fts WHERE mails_fts MATCH ? LIMIT ?)",
            (match_expr, limit),
        )
        return cur.fetchone()[0]

    def _probe_count_sql(self, cur: sqlite3.Cursor, sql: str, params: Sequence[object]) -> int:
        cur.execute(f"SELECT count(*) FROM ({sql})", params)
        return cur.fetchone()[0]


def _fmt_count(n: int, cap: int) -> str:
    return f"{cap}+건" if n >= cap else f"{n}건"


def _make_verifier(config: Config):
    """MailStorageEngine을 새로 만들지 않고(쓰기 연결을 열지 않기 위해)
    같은 검증 로직을 읽기 전용 커서에 적용하는 얇은 래퍼. storage.py의
    ``verify_candidate`` 구현은 인스턴스 메서드라 ``self.config``(
    strip_quoted_replies 설정)에 접근해야 하므로, 여기서는 그 설정만
    담은 껍데기 객체를 만들어 같은 메서드를 바인딩해 쓴다 — DB 연결은
    전혀 만들지 않는다.
    """
    from .storage import MailStorageEngine

    shim = object.__new__(MailStorageEngine)
    shim.config = config  # type: ignore[attr-defined]

    def verify(cur: sqlite3.Cursor, rowid: int, and_terms, or_groups, not_terms) -> bool:
        return MailStorageEngine.verify_candidate(
            shim, cur, rowid, and_terms=and_terms, or_groups=or_groups, not_terms=not_terms
        )

    return verify


def _make_text_fetcher(config: Config):
    """``--whole-word`` 검증용: rowid의 필드별 원문(색인과 동일하게
    인용문 제거 적용, NFKC+casefold까지 끝난 상태)을 읽어온다.

    verify_candidate와 같은 ``_row_field_texts``를 재사용한다 — 표시용
    ``snippet``(300자)만 보면 본문 뒷부분의 단어 경계를 놓치므로, 색인에
    실제로 쓰인 전체 텍스트를 봐야 한다.
    """
    from .storage import MailStorageEngine

    shim = object.__new__(MailStorageEngine)
    shim.config = config  # type: ignore[attr-defined]

    def fetch(cur: sqlite3.Cursor, rowid: int) -> dict[str | None, str] | None:
        return MailStorageEngine._row_field_texts(shim, cur, rowid)

    return fetch


_WORD_BOUNDARY_TERM_RE = re.compile(r"[0-9A-Za-z]+")


def _whole_word_ok(cur: sqlite3.Cursor, rowid: int, query: ParsedQuery, text_fetcher) -> bool:
    """``--whole-word``: 영문 텀에 한해 단어 경계 일치를 요구한다.

    한글 텀에는 적용하지 않는다 — 조사가 붙는 교착어라(예: "계약서를")
    단어 경계 검사를 하면 정상적인 결과까지 걸러진다(README에 명기한
    의도적 비대칭).
    """
    texts = text_fetcher(cur, rowid)
    if texts is None:
        return False
    for t in query.positive_terms():
        if not _WORD_BOUNDARY_TERM_RE.fullmatch(t.text):
            continue  # 한글 등 비-ASCII 텀은 검사하지 않는다
        haystack = texts.get(t.field, texts[None])  # 이미 NFKC+casefold 완료
        needle = t.text.casefold()
        pattern = re.compile(rf"(?<![0-9a-z_]){re.escape(needle)}(?![0-9a-z_])")
        if not pattern.search(haystack):
            return False
    return True
