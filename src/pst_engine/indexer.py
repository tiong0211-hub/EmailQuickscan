"""``PSTIndexer`` — 파서가 내놓은 ``RawMessage``를 정규화해 ``MailRecord``
배치로 스트리밍한다.

DB 커밋과 로그 파일 쓰기는 하지 않는다(역할 경계). 개별 메일 정규화
실패는 스킵하고 구조화된 오류 dict로 만들어 **호출자에게 그대로
넘긴다** — 그 오류를 어디(``errors.jsonl``)에 어떻게 쓸지는
orchestrator.py가 정한다.
"""

from __future__ import annotations

import datetime as dt_module
import email.utils
import hashlib
import json
import logging
import time
import zlib
from collections.abc import Iterator

from .config import Config
from .encoding import encode_raw_preview, safe_decode
from .models import MailRecord, RawMessage
from .parsers.base import ParserProtocol

logger = logging.getLogger(__name__)


class PSTIndexer:
    def __init__(self, config: Config) -> None:
        self.config = config

    def iter_batches(
        self, parser: ParserProtocol, file_path: str
    ) -> Iterator[tuple[list[MailRecord], list[dict]]]:
        """``(정규화된 배치, 이번 배치에서 난 오류 목록)`` 튜플을 스트리밍한다.

        ``batch_size``마다(또는 파서가 끝났을 때 남은 만큼) yield하고
        즉시 새 리스트로 갈아치운다 — 절대 전체를 누적하지 않는다(규칙 3).
        """
        batch: list[MailRecord] = []
        errors: list[dict] = []

        for raw in parser.iter_messages():
            try:
                record = self._normalize(raw, file_path)
                batch.append(record)
            except Exception as exc:
                errors.append(
                    {
                        "file_path": file_path,
                        "folder_path": getattr(raw, "folder_path", ""),
                        "error_type": type(exc).__name__,
                        "error": str(exc),
                        "ts": int(time.time()),
                    }
                )
                logger.debug("indexer: 메시지 정규화 실패 (file=%s)", file_path, exc_info=True)

            if len(batch) >= self.config.batch_size:
                yield batch, errors
                batch, errors = [], []

        if batch or errors:
            yield batch, errors

    def _normalize(self, raw: RawMessage, file_path: str) -> MailRecord:
        headers = raw.headers or {}

        subject = (headers.get("subject") or "").strip()
        from_addr = _normalize_addr_field(headers.get("from", ""))
        to_addr = _normalize_addr_field(headers.get("to", ""))
        cc_addr = _normalize_addr_field(headers.get("cc", ""))

        date_utc = _resolve_date_epoch(raw.native_time, headers.get("date"))

        message_id = _resolve_message_id(headers.get("message-id"), subject, date_utc, from_addr)

        body_text, decode_status = safe_decode(raw.body_bytes)
        snippet = body_text[: self.config.snippet_len]

        raw_b64 = None
        if decode_status == "failed":
            raw_b64, truncated = encode_raw_preview(raw.body_bytes, self.config.max_raw_bytes)
            if truncated:
                logger.debug(
                    "indexer: raw_body_b64가 %dB 상한으로 잘림 (message_id=%s)",
                    self.config.max_raw_bytes,
                    message_id,
                )

        attachment_names_joined = "; ".join(name for name in raw.attachment_names if name)
        has_attachment = 1 if raw.attachment_names else 0

        body_z = zlib.compress(body_text.encode("utf-8"), level=6)

        locator_json = ""
        if raw.locator is not None:
            try:
                locator_json = json.dumps(raw.locator, ensure_ascii=False)
            except TypeError:
                # locator에 JSON으로 못 담는 값이 섞여 있어도(있어선 안
                # 되지만) attach 기능 하나만 못 쓰게 될 뿐, 인덱싱
                # 자체를 실패시키지 않는다(규칙 5).
                logger.debug("indexer: locator를 JSON으로 직렬화하지 못함 (message_id=%s)", message_id, exc_info=True)

        return MailRecord(
            message_id=message_id,
            file_path=raw.source_path or file_path,
            folder_path=raw.folder_path or "",
            from_addr=from_addr,
            to_addr=to_addr,
            cc_addr=cc_addr,
            date_utc=date_utc,
            subject=subject,
            snippet=snippet,
            has_attachment=has_attachment,
            attachment_names=attachment_names_joined,
            size_bytes=raw.size_bytes,
            body_z=body_z,
            raw_body_b64=raw_b64,
            decode_status=decode_status,
            indexed_at=int(time.time()),
            locator_json=locator_json,
        )


def _normalize_addr_field(raw_value: str) -> str:
    """헤더의 주소 목록(콤마 구분, 표시이름 포함 가능)을 ``'; '`` 구분
    "Name <addr>" 형태로 통일한다.

    ``email.utils.getaddresses``는 표시이름에 콤마가 있어도(따옴표로
    감싸져 있으면) 올바르게 쪼개는 표준 라이브러리 함수다. 세미콜론으로
    구분된 값(pypff의 DISPLAY_TO처럼 이름만 있고 주소가 없는 경우)도
    같은 함수가 통째로 하나의 "표시이름"으로 처리해 버리므로, 미리
    세미콜론을 콤마로 바꿔 넣어 각 항목이 개별적으로 파싱되게 한다.
    """
    if not raw_value:
        return ""
    normalized_input = raw_value.replace(";", ",")
    pairs = email.utils.getaddresses([normalized_input])
    parts: list[str] = []
    for name, addr in pairs:
        name = name.strip()
        addr = addr.strip()
        if not name and not addr:
            continue
        if name and addr:
            parts.append(f"{name} <{addr}>")
        else:
            parts.append(addr or name)
    return "; ".join(parts)


def _resolve_date_epoch(native_time: object, date_header: str | None) -> int:
    """파서가 이미 datetime을 줬으면 그걸 쓰고, 아니면 ``Date:`` 헤더
    원문을 해석한다. 둘 다 실패하면 0(1970-01-01 UTC)을 쓴다 — 예외를
    내지 않는다(규칙 5).
    """
    # isinstance로 좁혀야 datetime의 tzinfo/replace/timestamp에 안전하게
    # 접근할 수 있다 — native_time은 파서마다 다른 타입을 줄 수 있어
    # object로 선언돼 있다(hasattr만으로는 정적 타입이 좁혀지지 않는다).
    if isinstance(native_time, dt_module.datetime):
        try:
            dt = native_time
            if dt.tzinfo is None:
                # PST 내부 시각은 보통 이미 UTC로 정규화돼 있다(libpff가
                # 그렇게 반환한다). tz 정보가 없는 경우만 UTC로 가정한다.
                dt = dt.replace(tzinfo=dt_module.timezone.utc)
            return int(dt.timestamp())
        except (OverflowError, OSError, ValueError):
            pass

    if date_header:
        try:
            parsed = email.utils.parsedate_to_datetime(date_header)
            if parsed is not None:
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=dt_module.timezone.utc)
                return int(parsed.timestamp())
        except (TypeError, ValueError, OverflowError):
            pass

    return 0


def _resolve_message_id(header_value: str | None, subject: str, date_utc: int, from_addr: str) -> str:
    """헤더에 Message-ID가 있으면 그대로, 없으면 명세대로 해시 생성.

    ``sha256(subject + "|" + iso_date + "|" + sender).hexdigest()[:32]``
    """
    if header_value:
        cleaned = header_value.strip().strip("<>")
        if cleaned:
            return cleaned

    iso_date = _epoch_to_iso(date_utc)
    basis = f"{subject}|{iso_date}|{from_addr}"
    return hashlib.sha256(basis.encode("utf-8")).hexdigest()[:32]


def _epoch_to_iso(epoch: int) -> str:
    import datetime as _dt

    return _dt.datetime.fromtimestamp(epoch, tz=_dt.timezone.utc).isoformat()
