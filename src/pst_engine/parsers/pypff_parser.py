"""pypff(libpff) 기반 1순위 PST 파서.

resolver.py가 ``import pypff``에 성공했을 때만 이 파서를 선택한다.
실행 파일에 pypff가 번들되어 있지 않으면 자동으로 설치하지 않고
다음 파서(readpst)로 넘어간다(CLAUDE.md 규칙 8).

여기서 쓰는 pypff API(속성·메서드 이름)는 이 구현 중 apt의
python3-pypff(libpff 20180714)로 실제 로드해 ``dir()``로 확인했고,
동작이 버전에 따라 다를 수 있는 부분(첨부파일 이름)은 PyPI
libpff-python 20231205 소스(pypff_attachment.c, pypff_record_entry.c,
libpff/libpff_mapi.h)를 직접 읽어 교차 확인했다.

핵심 발견: ``pypff.attachment`` 객체에는 이름을 직접 주는 속성이 없다
(``get_size``/``read_buffer``/``seek_offset``/레코드셋 접근자뿐). 첨부
파일명은 MAPI 레코드 엔트리 중 PidTagAttachLongFilename(0x3707) /
PidTagAttachFilename(0x3704)를 순회해 읽어야 한다.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from typing import Any

from ..models import RawMessage
from ..optional_deps import get_pypff
from .base import BaseParser

logger = logging.getLogger(__name__)

# MAPI 속성 태그 (libpff/libpff_mapi.h 확인값). 긴 이름을 우선한다.
_ENTRY_TYPE_ATTACH_FILENAME_LONG = 0x3707
_ENTRY_TYPE_ATTACH_FILENAME_SHORT = 0x3704

# 메시지 레벨 MAPI 속성 태그. transport_headers가 없는 PST(사내 Exchange가
# 순수 내부망으로 주고받아 SMTP 헤더가 아예 안 실리는 경우가 흔하다)에서
# from/to/cc를 최대한 복구하기 위해 쓴다. DISPLAY_TO는
# libpff/libpff_mapi.h에 정의돼 있어 그 값(0x0e04)을 그대로 썼고,
# DISPLAY_CC/SENDER_EMAIL_ADDRESS/SENT_REPRESENTING_EMAIL_ADDRESS는
# libpff가 이름을 안 붙였을 뿐 [MS-OXPROPS]가 정의하는 표준 MAPI 속성
# 태그이므로(0x0c1a 바로 옆 0x0c1f처럼 libpff_mapi.h에 있는 값들과 같은
# 체계) 원시 태그 값을 직접 썼다 — 없는 PST에서는 그냥 못 찾을 뿐이다.
_ENTRY_TYPE_DISPLAY_TO = 0x0E04
_ENTRY_TYPE_DISPLAY_CC = 0x0E03
_ENTRY_TYPE_SENDER_EMAIL_ADDRESS = 0x0C1F
_ENTRY_TYPE_SENT_REPRESENTING_EMAIL_ADDRESS = 0x0065


class PypffParser(BaseParser):
    """``pypff.file``을 감싸 RawMessage 스트림으로 노출한다."""

    def __init__(self, path: str) -> None:
        super().__init__(path)
        self._pypff = get_pypff()
        # pypff.file 인스턴스의 실제 타입은 스텁이 없어 알 수 없으므로
        # Any로 둔다 — None인 "닫힌 상태"와 "열린 pypff.file"을 함께
        # 표현해야 한다.
        self._file: Any = None

    def open(self) -> None:
        pypff_module = self._pypff
        if pypff_module is None:
            raise RuntimeError("pypff 모듈을 사용할 수 없습니다 (번들되지 않음)")
        self._file = pypff_module.file()
        self._file.open(self._path)

    def close(self) -> None:
        if self._file is not None:
            try:
                self._file.close()
            except Exception:
                logger.debug("pypff: close 중 예외 무시", exc_info=True)
            self._file = None

    def iter_messages(self) -> Iterator[RawMessage]:
        """루트 폴더부터 재귀적으로 순회하며 메시지를 하나씩 yield한다.

        재귀 자체가 제너레이터 체인이므로 폴더/메시지 전체를 리스트로
        모으는 일이 없다(규칙 3) — 어느 시점에도 "현재 메시지 하나"만
        메모리에 존재한다.
        """
        if self._file is None:
            raise RuntimeError("open()을 먼저 호출해야 합니다")
        root = self._file.root_folder
        yield from self._walk_folder(root, [], [])

    def _walk_folder(
        self, folder: Any, path_names: list[str], path_indices: list[int]
    ) -> Iterator[RawMessage]:
        folder_path = "/".join(path_names)

        for i in range(folder.number_of_sub_messages):
            try:
                message = folder.get_sub_message(i)
                yield self._build_raw_message(message, folder_path, path_indices, i)
            except Exception:
                # pypff 자체 접근 실패(드묾)만 여기서 삼킨다. RawMessage를
                # 정규화하다 나는 실패는 indexer.py가 담당한다(규칙 5:
                # 예외가 파이프라인을 멈추지 않게).
                logger.debug("pypff: 메시지 index=%d 접근 실패, 건너뜀", i, exc_info=True)
                continue

        for i in range(folder.number_of_sub_folders):
            try:
                sub = folder.get_sub_folder(i)
            except Exception:
                logger.debug("pypff: 하위 폴더 index=%d 접근 실패, 건너뜀", i, exc_info=True)
                continue
            name = sub.name or f"folder_{i}"
            yield from self._walk_folder(sub, [*path_names, name], [*path_indices, i])

    def _build_raw_message(
        self, message: Any, folder_path: str, path_indices: list[int], msg_index: int
    ) -> RawMessage:
        headers = _parse_header_block(message.transport_headers or "")

        body_text = message.plain_text_body
        body_bytes = body_text.encode("utf-8") if isinstance(body_text, str) else (body_text or b"")

        html_raw = message.html_body
        if isinstance(html_raw, (bytes, bytearray)):
            html_bytes: bytes | None = bytes(html_raw)
        elif isinstance(html_raw, str):
            html_bytes = html_raw.encode("utf-8")
        else:
            html_bytes = None

        attachment_names: list[str] = []
        for ai in range(message.number_of_attachments):
            try:
                att = message.get_attachment(ai)
                name = _attachment_name(att)
            except Exception:
                logger.debug("pypff: 첨부 index=%d 이름 조회 실패", ai, exc_info=True)
                name = None
            attachment_names.append(name or f"attachment_{ai}")

        native_time = message.client_submit_time or message.delivery_time

        size = len(body_bytes) + (len(html_bytes) if html_bytes else 0)

        # transport_headers 자체가 없는 PST가 흔하다(순수 사내 Exchange
        # 발신 메일은 SMTP 헤더가 실리지 않는다). 그럴 때 MAPI 레코드
        # 엔트리에서 직접 보강한다 — 이것도 "정규화"가 아니라 "원본 필드
        # 보강"이다(주소 형식을 다듬는 건 indexer의 몫).
        if "subject" not in headers and message.subject:
            headers["subject"] = message.subject
        if "from" not in headers:
            sender_email = _message_entry_string(message, _ENTRY_TYPE_SENDER_EMAIL_ADDRESS) or _message_entry_string(
                message, _ENTRY_TYPE_SENT_REPRESENTING_EMAIL_ADDRESS
            )
            if message.sender_name and sender_email:
                headers["from"] = f"{message.sender_name} <{sender_email}>"
            elif sender_email:
                headers["from"] = sender_email
            elif message.sender_name:
                headers["from"] = message.sender_name
        if "to" not in headers:
            display_to = _message_entry_string(message, _ENTRY_TYPE_DISPLAY_TO)
            if display_to:
                headers["to"] = display_to
        if "cc" not in headers:
            display_cc = _message_entry_string(message, _ENTRY_TYPE_DISPLAY_CC)
            if display_cc:
                headers["cc"] = display_cc

        return RawMessage(
            headers=headers,
            body_bytes=body_bytes,
            html_bytes=html_bytes,
            folder_path=folder_path,
            attachment_names=attachment_names,
            native_time=native_time,
            size_bytes=size,
            locator={
                "parser": "pypff",
                "folder_indices": list(path_indices),
                "message_index": msg_index,
            },
            source_path=self._path,
        )

    def fetch_attachment(self, locator: object, index: int) -> tuple[str, bytes] | None:
        """``attach`` 명령 전용: locator로 메시지를 재탐색해 첨부 본체를 꺼낸다.

        인덱싱 중에는 절대 호출되지 않는다 — 첨부 본체를 읽는 유일한
        경로이며, DB에는 이름만 저장하므로 상시 용량이 들지 않는다.
        """
        if self._file is None:
            raise RuntimeError("open()을 먼저 호출해야 합니다")
        if not isinstance(locator, dict) or locator.get("parser") != "pypff":
            return None

        folder = self._file.root_folder
        for idx in locator.get("folder_indices", []):
            folder = folder.get_sub_folder(idx)
        message = folder.get_sub_message(locator["message_index"])

        if index < 0 or index >= message.number_of_attachments:
            return None
        att = message.get_attachment(index)
        name = _attachment_name(att) or f"attachment_{index}"
        data = att.read_buffer(att.size)
        return name, bytes(data) if data is not None else b""


def _record_entry_strings(item: Any, *entry_types: int) -> dict[int, str]:
    """item(메시지/첨부 등)의 모든 레코드셋을 훑어 원하는 MAPI 속성
    태그들의 문자열 값을 한 번에 모아 온다. attachment든 message든 같은
    ``get_record_set``/``number_of_record_sets`` 접근자를 상속하므로
    (둘 다 libpff의 공통 item 베이스) 이 함수 하나로 재사용한다.
    """
    wanted = set(entry_types)
    found: dict[int, str] = {}
    for rs_index in range(item.number_of_record_sets):
        record_set = item.get_record_set(rs_index)
        for e_index in range(record_set.number_of_entries):
            entry = record_set.get_entry(e_index)
            try:
                entry_type = entry.entry_type
            except Exception:
                # libpff 바인딩(C 확장)이 던지는 예외 타입이 버전마다
                # 달라 넓게 잡는다(pyproject.toml에서 BLE001을 프로젝트
                # 전역으로 완화한 이유와 동일) — 항목 하나 접근 실패로
                # 전체 인덱싱이 멈추면 안 된다(규칙 5).
                logger.debug("pypff: entry_type 조회 실패, 이 엔트리는 건너뜀", exc_info=True)
                continue
            if entry_type not in wanted or entry_type in found:
                continue
            try:
                value = entry.get_data_as_string()
            except Exception:
                logger.debug("pypff: entry_type=%s 데이터 조회 실패, 건너뜀", entry_type, exc_info=True)
                continue
            if value:
                found[entry_type] = value
    return found


def _message_entry_string(message: Any, entry_type: int) -> str | None:
    return _record_entry_strings(message, entry_type).get(entry_type)


def _attachment_name(att: Any) -> str | None:
    """MAPI 레코드 엔트리에서 첨부파일 이름을 찾는다 (긴 이름 우선)."""
    values = _record_entry_strings(att, _ENTRY_TYPE_ATTACH_FILENAME_LONG, _ENTRY_TYPE_ATTACH_FILENAME_SHORT)
    return values.get(_ENTRY_TYPE_ATTACH_FILENAME_LONG) or values.get(_ENTRY_TYPE_ATTACH_FILENAME_SHORT)


def _parse_header_block(text: str) -> dict[str, str]:
    """``Key: Value`` 줄로 이뤄진 원시 헤더 블록을 dict로 변환.

    pypff의 ``transport_headers``는 RFC822 헤더 전체를 문자열로 그대로
    준다(있을 때). 여기서는 폴딩(다음 줄이 공백으로 시작하는 연속 줄)
    해제만 하고, 그 이상의 정규화(주소 분리 등)는 하지 않는다 — 그건
    indexer.py의 몫이다.
    """
    headers: dict[str, str] = {}
    if not text:
        return headers
    current_key: str | None = None
    for line in text.splitlines():
        if not line.strip():
            continue
        if line[0] in " \t" and current_key is not None:
            headers[current_key] += " " + line.strip()
            continue
        if ":" in line:
            key, _, value = line.partition(":")
            current_key = key.strip().lower()
            headers.setdefault(current_key, value.strip())
    return headers
