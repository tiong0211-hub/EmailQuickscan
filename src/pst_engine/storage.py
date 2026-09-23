"""``MailStorageEngine`` — 스키마 생성, Upsert, CSV/EML export.

이 모듈이 소유한 세 가지 핵심 유틸리티(``bigram_tokens``,
``strip_quoted_reply``, ``verify_candidates``)는 색인 시점(여기)과 검색
시점(search.py)에서 **반드시 동일한 결과**를 내야 한다 — 둘이 어긋나면
색인엔 있는데 검색에선 조용히 0건이 되는 버그가 생긴다. search.py는
이 모듈의 순수 함수를 그대로 import해서 쓴다(쓰기 연결을 열지 않으므로
"검색은 쓰기 연결 금지"라는 역할 경계를 어기지 않는다).

설계 근거(왜 trigram이 아니라 이 방식인지, 왜 detail=column인지, 왜
rowid를 직접 계산하는지)는 README.md의 "설계 근거" 절 및 CLAUDE.md
규칙 4를 참조.
"""

from __future__ import annotations

import csv
import hashlib
import logging
import re
import sqlite3
import time
import unicodedata
import zlib
from collections.abc import Iterable
from pathlib import Path

from .config import Config
from .models import MailRecord

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 스키마
# ---------------------------------------------------------------------------

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS mails (
    message_id        TEXT PRIMARY KEY,
    file_path         TEXT NOT NULL,
    folder_path       TEXT NOT NULL DEFAULT '',
    from_addr         TEXT NOT NULL DEFAULT '',
    to_addr           TEXT NOT NULL DEFAULT '',
    cc_addr           TEXT NOT NULL DEFAULT '',
    date_utc          INTEGER NOT NULL DEFAULT 0,
    subject           TEXT NOT NULL DEFAULT '',
    snippet           TEXT NOT NULL DEFAULT '',
    has_attachment    INTEGER NOT NULL DEFAULT 0,
    attachment_names  TEXT NOT NULL DEFAULT '',
    size_bytes        INTEGER NOT NULL DEFAULT 0,
    body_z            BLOB,
    raw_body_b64      TEXT,
    decode_status     TEXT NOT NULL DEFAULT 'ok',
    indexed_at        INTEGER NOT NULL DEFAULT 0,
    locator_json      TEXT NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_date ON mails(date_utc);
CREATE INDEX IF NOT EXISTS idx_from ON mails(from_addr);
CREATE INDEX IF NOT EXISTS idx_folder ON mails(folder_path);
"""

# mails_fts는 detail/tokenize를 config로부터 받으므로 별도 문자열
# 포매팅으로 만든다(schema_sql 상수 하나로 고정할 수 없음).
_FTS_TABLE_TEMPLATE = """
CREATE VIRTUAL TABLE IF NOT EXISTS mails_fts USING fts5(
    bi_subject, bi_body, bi_from, bi_to, bi_cc, bi_att,
    tokenize = "{tokenize}",
    detail = {detail}
);
"""

_FTS_COLUMNS = ("bi_subject", "bi_body", "bi_from", "bi_to", "bi_cc", "bi_att")


def build_fts_ddl(config: Config) -> str:
    return _FTS_TABLE_TEMPLATE.format(tokenize=config.fts_tokenize, detail=config.fts_detail)


# ---------------------------------------------------------------------------
# bigram 토큰화 — 색인/검색 공용, 반드시 동일해야 한다
# ---------------------------------------------------------------------------

# 한글 완성형(가-힣) + 자모(ㄱ-ㆎ) + 영숫자만 토큰 문자로 인정하고 나머지는
# 전부 구분자로 취급한다. 이메일 주소의 '@', '.'도 구분자가 되므로
# "user@corp.com"은 "user", "corp", "com"으로 쪼개진다 — 3자 이상 부분
# 문자열은 trigram이 담당하던 시절과 동일하게 이런 조각 단위로 잡힌다.
_TOKEN_SPLIT_RE = re.compile(r"[^0-9a-z가-힣ㄱ-ㆎ]+")


def bigram_tokens(text: str | None) -> str:
    """NFKC 정규화 → casefold → 토큰 분할 → 각 토큰의 인접 2-gram을 공백으로
    이어붙인다.

    이 함수의 출력을 FTS5 컬럼에 그대로 저장하고, 검색 쿼리도 같은
    함수로 변환한 뒤 MATCH한다. 1글자 토큰은 2-gram을 만들 수 없어
    누락된다 — 그래서 검색 쪽에서 1글자 텀은 이 함수를 쓰지 않고 별도
    LIKE 폴백을 쓴다(search.py).
    """
    if not text:
        return ""
    normalized = unicodedata.normalize("NFKC", text).casefold()
    grams: list[str] = []
    for token in _TOKEN_SPLIT_RE.split(normalized):
        if len(token) < 2:
            continue
        for i in range(len(token) - 1):
            grams.append(token[i : i + 2])
    return " ".join(grams)


# ---------------------------------------------------------------------------
# 인용 답장 체인 제거 — 색인 전용(저장 원문 body_z에는 적용하지 않는다)
# ---------------------------------------------------------------------------

# "-----Original Message-----" 류의 구분선. 영어/한국어 Outlook, 그리고
# 흔한 변형(대시 개수, 앞뒤 공백)을 넓게 잡는다. 이 줄을 찾으면 그 줄부터
# 메시지 끝까지를 통째로 버린다 — 그 아래는 전부 이전에 오간 메일의
# 재인용이라고 보기 때문이다(실측: 평균 본문의 66%가 이 아래에 있다).
_ORIGINAL_MESSAGE_RE = re.compile(
    r"^[ \t]*-{2,}[ \t]*(Original Message|원본 메시지)[ \t]*-{2,}.*\Z",
    re.MULTILINE | re.DOTALL | re.IGNORECASE,
)

# ">" 또는 "|"로 시작하는 인용 줄(이메일 클라이언트의 표준 인용 표기).
_QUOTE_LINE_RE = re.compile(r"^[ \t]*[>|]", re.MULTILINE)


def strip_quoted_reply(body: str) -> str:
    """색인용 본문에서 인용 답장 체인을 제거한다.

    ``body_z``(원문 압축 저장)에는 이 함수를 적용하지 않는다 — 사용자가
    ``show``로 볼 때는 원본 그대로 보여야 하기 때문이다. 색인 대상
    텍스트에만 적용해 중복 색인을 막는다(README "설계 근거" 발견 4).
    """
    if not body:
        return ""
    truncated = _ORIGINAL_MESSAGE_RE.sub("", body)
    lines = [line for line in truncated.split("\n") if not _QUOTE_LINE_RE.match(line)]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# rowid — 전역 시간순 배치 (ORDER BY rowid DESC == 최신순)
# ---------------------------------------------------------------------------

_ROWID_HASH_BITS = 20
_ROWID_HASH_MASK = (1 << _ROWID_HASH_BITS) - 1


def compute_rowid(message_id: str, date_utc: int) -> int:
    """``(date_utc << 20) | (sha256(message_id)의 앞 3바이트 & 0xFFFFF)``.

    실측(README 발견 2): rowid를 시간순으로 미리 배치해 두면 날짜 정렬이
    인덱스 스캔이 되어 667ms → 0.4ms로 줄어든다. 해시 부분은 같은
    date_utc를 가진 메일들 사이에서 rowid 충돌 가능성을 낮추기 위한
    것뿐이고(멱등성의 최종 기준은 message_id), 충돌 시엔
    :func:`resolve_rowid`가 선형 탐침으로 해결한다.
    """
    digest = hashlib.sha256(message_id.encode("utf-8")).digest()
    hash_part = int.from_bytes(digest[:3], "big") & _ROWID_HASH_MASK
    # SQLite INTEGER PRIMARY KEY(rowid)는 부호 있는 64비트다. date_utc가
    # 아주 먼 미래가 아닌 한(epoch 초 기준 약 8700만 년 뒤에나 오버플로)
    # 시프트해도 64비트 안에 넉넉히 들어온다.
    return (date_utc << _ROWID_HASH_BITS) | hash_part


# ---------------------------------------------------------------------------
# MailStorageEngine
# ---------------------------------------------------------------------------


class UpsertStats:
    __slots__ = ("inserted", "retries", "updated")

    def __init__(self) -> None:
        self.inserted = 0
        self.updated = 0
        self.retries = 0

    def __repr__(self) -> str:  # pragma: no cover - 디버그 편의
        return f"UpsertStats(inserted={self.inserted}, updated={self.updated}, retries={self.retries})"


class MailStorageEngine:
    """쓰기 연결 하나를 소유한다 (Single Writer 원칙 — orchestrator.py에서만
    인스턴스화한다).
    """

    def __init__(self, db_path: str | Path, config: Config) -> None:
        self.db_path = Path(db_path)
        self.config = config
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.db_path), timeout=30.0, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self._ensure_schema()

    def _ensure_schema(self) -> None:
        self.conn.executescript(SCHEMA_SQL)
        self.conn.executescript(build_fts_ddl(self.config))

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> MailStorageEngine:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- upsert ------------------------------------------------------------

    def upsert_batch(self, records: list[MailRecord]) -> UpsertStats:
        """레코드 배치를 하나의 트랜잭션으로 upsert한다.

        멱등성의 기준은 ``message_id``다: 이미 있으면 UPDATE, 없으면
        INSERT하며, 두 경우 모두 같은 rowid를 쓰도록 먼저 조회한다(rowid는
        message_id로부터 결정론적으로 계산되지만, 해시 충돌 시 아래
        :meth:`_resolve_rowid`가 기존에 배정된 rowid를 우선한다).

        DB busy/lock은 설정된 backoff로 재시도한다(CLAUDE.md 재시도 정책).
        """
        stats = UpsertStats()
        if not records:
            return stats

        attempt = 0
        backoffs = self.config.retry_backoff_seconds
        while True:
            try:
                self._upsert_batch_once(records, stats)
                return stats
            except sqlite3.OperationalError as exc:
                if "locked" not in str(exc).lower() and "busy" not in str(exc).lower():
                    raise
                if attempt >= self.config.retry_max_attempts:
                    raise
                delay = backoffs[min(attempt, len(backoffs) - 1)]
                logger.warning(
                    "storage: DB busy/locked, %.1fs 뒤 재시도 (%d/%d)",
                    delay, attempt + 1, self.config.retry_max_attempts,
                )
                stats.retries += 1
                time.sleep(delay)
                attempt += 1

    def _upsert_batch_once(self, records: list[MailRecord], stats: UpsertStats) -> None:
        cur = self.conn.cursor()
        cur.execute("BEGIN IMMEDIATE")
        try:
            for record in records:
                rowid = self._resolve_rowid(cur, record.message_id, record.date_utc)
                existed = self._row_exists(cur, rowid)

                cur.execute(
                    """
                    INSERT INTO mails (
                        rowid, message_id, file_path, folder_path, from_addr, to_addr, cc_addr,
                        date_utc, subject, snippet, has_attachment, attachment_names, size_bytes,
                        body_z, raw_body_b64, decode_status, indexed_at, locator_json
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(message_id) DO UPDATE SET
                        file_path=excluded.file_path,
                        folder_path=excluded.folder_path,
                        from_addr=excluded.from_addr,
                        to_addr=excluded.to_addr,
                        cc_addr=excluded.cc_addr,
                        date_utc=excluded.date_utc,
                        subject=excluded.subject,
                        snippet=excluded.snippet,
                        has_attachment=excluded.has_attachment,
                        attachment_names=excluded.attachment_names,
                        size_bytes=excluded.size_bytes,
                        body_z=excluded.body_z,
                        raw_body_b64=excluded.raw_body_b64,
                        decode_status=excluded.decode_status,
                        indexed_at=excluded.indexed_at,
                        locator_json=excluded.locator_json
                    """,
                    (
                        rowid,
                        record.message_id,
                        record.file_path,
                        record.folder_path,
                        record.from_addr,
                        record.to_addr,
                        record.cc_addr,
                        record.date_utc,
                        record.subject,
                        record.snippet,
                        record.has_attachment,
                        record.attachment_names,
                        record.size_bytes,
                        record.body_z,
                        record.raw_body_b64,
                        record.decode_status,
                        record.indexed_at,
                        record.locator_json,
                    ),
                )

                # FTS는 외부콘텐츠 방식이 아니므로(토큰 스트림 자체가
                # 저장 데이터) 수동으로 지우고 다시 넣는다 — rowid
                # 삭제는 실측 0.1ms로 저렴하다.
                cur.execute("DELETE FROM mails_fts WHERE rowid=?", (rowid,))
                body_text = _decompress_body(record.body_z)
                indexed_body = strip_quoted_reply(body_text) if self.config.strip_quoted_replies else body_text
                cur.execute(
                    "INSERT INTO mails_fts (rowid, bi_subject, bi_body, bi_from, bi_to, bi_cc, bi_att) "
                    "VALUES (?,?,?,?,?,?,?)",
                    (
                        rowid,
                        bigram_tokens(record.subject),
                        bigram_tokens(indexed_body),
                        bigram_tokens(record.from_addr),
                        bigram_tokens(record.to_addr),
                        bigram_tokens(record.cc_addr),
                        bigram_tokens(record.attachment_names),
                    ),
                )

                if existed:
                    stats.updated += 1
                else:
                    stats.inserted += 1

            cur.execute("COMMIT")
        except Exception:
            cur.execute("ROLLBACK")
            raise

    def _row_exists(self, cur: sqlite3.Cursor, rowid: int) -> bool:
        cur.execute("SELECT 1 FROM mails WHERE rowid=?", (rowid,))
        return cur.fetchone() is not None

    def _resolve_rowid(self, cur: sqlite3.Cursor, message_id: str, date_utc: int) -> int:
        """message_id가 이미 배정받은 rowid가 있으면 그걸 재사용하고,
        없으면 새로 계산하되 다른 message_id가 이미 차지하고 있으면
        선형 탐침으로 다음 빈 자리를 찾는다.
        """
        cur.execute("SELECT rowid FROM mails WHERE message_id=?", (message_id,))
        row = cur.fetchone()
        if row is not None:
            return row[0]

        candidate = compute_rowid(message_id, date_utc)
        while True:
            cur.execute("SELECT message_id FROM mails WHERE rowid=?", (candidate,))
            occupant = cur.fetchone()
            if occupant is None or occupant[0] == message_id:
                return candidate
            candidate += 1  # 드문 해시 충돌 — 같은 날짜 구간 안에서 다음 슬롯

    # -- 검증(오탐 제거) -----------------------------------------------------

    #: 쿼리의 필드 이름 -> mails 테이블에서 가져올 실제 텍스트로의 매핑.
    #: ``None``(필드 지정 없는 "맨 텀")은 전 필드를 합친 텍스트를 본다.
    _VERIFY_FIELDS = ("subject", "body", "from", "to", "cc", "attachments")

    def _row_field_texts(self, cur: sqlite3.Cursor, rowid: int) -> dict[str | None, str] | None:
        cur.execute(
            "SELECT subject, from_addr, to_addr, cc_addr, attachment_names, body_z FROM mails WHERE rowid=?",
            (rowid,),
        )
        row = cur.fetchone()
        if row is None:
            return None
        subject, from_addr, to_addr, cc_addr, attachment_names, body_z = row
        body_text = _decompress_body(body_z)
        indexed_body = strip_quoted_reply(body_text) if self.config.strip_quoted_replies else body_text
        texts: dict[str | None, str] = {
            "subject": subject or "",
            "body": indexed_body,
            "from": from_addr or "",
            "to": to_addr or "",
            "cc": cc_addr or "",
            "attachments": attachment_names or "",
        }
        texts[None] = " \n ".join(texts[f] for f in self._VERIFY_FIELDS)
        return {k: unicodedata.normalize("NFKC", v).casefold() for k, v in texts.items()}

    def verify_candidate(
        self,
        cur: sqlite3.Cursor,
        rowid: int,
        *,
        and_terms: list[tuple[str | None, str]],
        or_groups: list[tuple[tuple[str | None, str], tuple[str | None, str]]] | None = None,
        not_terms: list[tuple[str | None, str]] | None = None,
    ) -> bool:
        """bigram AND 매치로 나온 후보 하나가 실제로 쿼리를 만족하는지
        저장된 컬럼(subject/본문/주소/첨부명)에서 부분 문자열로
        재확인한다.

        ``detail=column``은 구문(phrase) 검색을 지원하지 않으므로 bigram
        AND는 "상위집합"일 뿐이다 — 이 재검증이 없으면 실측 2.8% 오탐이
        생긴다(README 발견 7). 읽기 전용 커서로도 호출 가능하다(SELECT뿐,
        쓰기 연결을 요구하지 않는다 — search.py가 재사용하는 이유).

        각 텀은 ``(field, text)`` 쌍이다. ``field``가 ``None``이면 전
        필드를 합친 텍스트에서 찾는다.
        """
        texts = self._row_field_texts(cur, rowid)
        if texts is None:
            return False

        def contains(field: str | None, term: str) -> bool:
            needle = unicodedata.normalize("NFKC", term).casefold()
            if not needle:
                return True
            haystack = texts.get(field, texts[None])
            return needle in haystack

        for field, term in and_terms:
            if not contains(field, term):
                return False

        for (lf, lt), (rf, rt) in or_groups or []:
            if not (contains(lf, lt) or contains(rf, rt)):
                return False

        for field, term in not_terms or []:
            if contains(field, term):
                return False

        return True

    # -- export --------------------------------------------------------

    def export_csv(self, rows: Iterable[sqlite3.Row], out_path: str | Path, columns: list[str]) -> int:
        """검색 결과(또는 임의 쿼리 결과)를 CSV로 스트리밍 저장한다.

        커서를 한 행씩 그대로 흘려보낸다 — 결과 전체를 리스트로 모으지
        않는다(규칙 3). Excel에서 한글이 깨지지 않도록 utf-8-sig로 쓴다.
        """
        out = Path(out_path)
        out.parent.mkdir(parents=True, exist_ok=True)
        count = 0
        with out.open("w", encoding="utf-8-sig", newline="") as fh:
            writer = csv.writer(fh)
            writer.writerow(columns)
            for row in rows:
                writer.writerow([row[c] for c in columns])
                count += 1
        return count

    def export_eml(self, rows: Iterable[sqlite3.Row], out_dir: str | Path) -> int:
        """검색 결과를 ``.eml``로 저장한다. 본문만 담고 첨부는 제외한다
        (사용자 승인 사항) — 첨부 본체는 애초에 DB에 없으므로, 필요하면
        ``attach`` 명령으로 원본 PST에서 따로 꺼내야 한다.
        """
        out = Path(out_dir)
        out.mkdir(parents=True, exist_ok=True)
        count = 0
        for row in rows:
            body_text = _decompress_body(row["body_z"])
            eml = _build_eml(
                subject=row["subject"] or "",
                from_addr=row["from_addr"] or "",
                to_addr=row["to_addr"] or "",
                cc_addr=row["cc_addr"] or "",
                date_utc=row["date_utc"] or 0,
                body_text=body_text,
            )
            safe_name = _safe_filename(row["message_id"])
            (out / f"{safe_name}.eml").write_bytes(eml)
            count += 1
        return count


def _decompress_body(body_z: bytes | None) -> str:
    if not body_z:
        return ""
    try:
        return zlib.decompress(body_z).decode("utf-8", errors="replace")
    except zlib.error:
        logger.warning("storage: body_z 압축 해제 실패", exc_info=True)
        return ""


def _safe_filename(message_id: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]", "_", message_id)[:80] or "mail"


def _build_eml(*, subject: str, from_addr: str, to_addr: str, cc_addr: str, date_utc: int, body_text: str) -> bytes:
    import email.message
    import email.utils

    msg = email.message.EmailMessage()
    msg["Subject"] = subject
    msg["From"] = from_addr
    msg["To"] = to_addr
    if cc_addr:
        msg["Cc"] = cc_addr
    msg["Date"] = email.utils.formatdate(date_utc, usegmt=True)
    msg.set_content(body_text)
    return bytes(msg)
