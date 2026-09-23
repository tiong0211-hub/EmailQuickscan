"""extract_msg 기반 ``.msg`` 파서 (3순위 폴백).

pypff도 readpst도 쓸 수 없을 때, 확장자가 ``.msg``인 파일(또는 ``.msg``
파일이 담긴 디렉터리)에 한해 이 파서를 쓴다. PST 자체는 열지 못하므로
"PST 전체 폴백"이 아니라 이미 낱개 ``.msg``로 내보내진 메일에만 쓰이는
마지막 경로다(resolver.py 4단계 규칙 참조).

여기서 쓰는 extract_msg API는 PyPI extract-msg 0.56.1의 실제 소스
(``msg_classes/message_base.py``, ``attachments/attachment_base.py``)를
직접 읽어 확인했다: ``Message.header``(email.message.Message로 복원된
원본 헤더), ``.body``(str), ``.htmlBody``(bytes), ``.date``(datetime),
``.attachments``(리스트, 각 원소의 ``.name``/``.data``).
"""

from __future__ import annotations

import logging
import os
from collections.abc import Iterator
from typing import Any

from ..models import RawMessage
from ..optional_deps import get_extract_msg
from .base import BaseParser

logger = logging.getLogger(__name__)


class ExtractMsgParser(BaseParser):
    """``.msg`` 단일 파일 또는 ``.msg``가 담긴 디렉터리를 순회한다."""

    def __init__(self, path: str) -> None:
        super().__init__(path)
        self._extract_msg = get_extract_msg()

    def open(self) -> None:
        if self._extract_msg is None:
            raise RuntimeError("extract_msg 모듈을 사용할 수 없습니다 (번들되지 않음)")
        if not os.path.exists(self._path):
            raise RuntimeError(f"경로가 존재하지 않습니다: {self._path}")

    def close(self) -> None:
        # 파일별로 열고 닫으므로(iter_messages 내부에서 with문 사용)
        # 여기서 정리할 장수명 리소스가 없다.
        pass

    def _iter_msg_paths(self) -> Iterator[str]:
        if os.path.isfile(self._path):
            yield self._path
            return
        for root, dirs, files in os.walk(self._path):
            dirs.sort()
            for fname in sorted(files):
                if fname.lower().endswith(".msg"):
                    yield os.path.join(root, fname)

    def iter_messages(self) -> Iterator[RawMessage]:
        if self._extract_msg is None:
            raise RuntimeError("open()을 먼저 호출해야 합니다")

        base_dir = self._path if os.path.isdir(self._path) else os.path.dirname(self._path)

        for msg_path in self._iter_msg_paths():
            try:
                with self._extract_msg.openMsg(msg_path) as msg:
                    yield self._build_raw_message(msg, msg_path, base_dir)
            except Exception:
                logger.debug("extract_msg: %s 파싱 실패, 건너뜀", msg_path, exc_info=True)
                continue

    def _build_raw_message(self, msg: Any, msg_path: str, base_dir: str) -> RawMessage:
        headers: dict[str, str] = {}
        try:
            header_msg = msg.header  # email.message.Message 또는 None
            if header_msg is not None:
                for key, value in header_msg.items():
                    headers.setdefault(key.lower(), str(value))
        except Exception:
            logger.debug("extract_msg: 헤더 복원 실패: %s", msg_path, exc_info=True)

        # 이 파서가 채워 넣는 값들은 "원본 필드 보강"이지 정규화가
        # 아니다 — pypff_parser와 동일한 논리(헤더 스트림이 없거나
        # 불완전한 .msg가 흔하다).
        if "subject" not in headers and getattr(msg, "subject", None):
            headers["subject"] = str(msg.subject)
        if "from" not in headers and getattr(msg, "sender", None):
            headers["from"] = str(msg.sender)
        if "to" not in headers and getattr(msg, "to", None):
            headers["to"] = str(msg.to)
        if "cc" not in headers and getattr(msg, "cc", None):
            headers["cc"] = str(msg.cc)

        body_text = getattr(msg, "body", None)
        # MSG 포맷은 본문을 유니코드 속성으로 저장하므로 extract_msg가
        # 이미 올바른 str을 돌려준다 — 여기서 utf-8로 인코딩해 두면
        # indexer.safe_decode()가 항상 "ok"로 되돌려준다(재-디코딩이
        # 아니라 우리가 만든 bytes를 다시 읽는 것이므로 손실이 없다).
        body_bytes = body_text.encode("utf-8") if isinstance(body_text, str) else b""

        html_raw = getattr(msg, "htmlBody", None)
        html_bytes = bytes(html_raw) if isinstance(html_raw, (bytes, bytearray)) else None

        attachment_names: list[str] = []
        try:
            for att in msg.attachments:
                name = getattr(att, "name", None)
                attachment_names.append(str(name) if name else "attachment")
        except Exception:
            logger.debug("extract_msg: 첨부 목록 조회 실패: %s", msg_path, exc_info=True)

        native_time = getattr(msg, "date", None)

        try:
            size = os.path.getsize(msg_path)
        except OSError:
            size = len(body_bytes) + (len(html_bytes) if html_bytes else 0)

        rel_folder = os.path.relpath(os.path.dirname(msg_path), base_dir)
        folder_path = "" if rel_folder == "." else rel_folder.replace(os.sep, "/")

        return RawMessage(
            headers=headers,
            body_bytes=body_bytes,
            html_bytes=html_bytes,
            folder_path=folder_path,
            attachment_names=attachment_names,
            native_time=native_time,
            size_bytes=size,
            locator={"parser": "extractmsg", "msg_path": msg_path},
            source_path=msg_path,
        )

    def fetch_attachment(self, locator: object, index: int) -> tuple[str, bytes] | None:
        """``attach`` 명령 전용: 같은 .msg 파일을 다시 열어 첨부를 꺼낸다.

        .msg는 파일 자체가 메일 하나이므로 readpst처럼 전체를 다시 풀
        필요가 없다 — 대상 파일만 다시 열면 된다. 인덱싱 경로에서는
        절대 호출되지 않는다.
        """
        if self._extract_msg is None:
            raise RuntimeError("extract_msg 모듈을 사용할 수 없습니다")
        if not isinstance(locator, dict) or locator.get("parser") != "extractmsg":
            return None

        msg_path = locator["msg_path"]
        with self._extract_msg.openMsg(msg_path) as msg:
            attachments = list(msg.attachments)
            if index < 0 or index >= len(attachments):
                return None
            att = attachments[index]
            name = str(getattr(att, "name", None) or f"attachment_{index}")
            data = getattr(att, "data", None)
            if not isinstance(data, (bytes, bytearray)):
                # 임베디드 .msg 첨부 등 bytes가 아닌 경우는 이 경로에서
                # 지원하지 않는다 — 이름만이라도 돌려준다.
                return name, b""
            return name, bytes(data)
