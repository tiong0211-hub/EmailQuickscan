"""readpst(libpst) 기반 2순위 PST 파서.

pypff를 쓸 수 없을 때(번들되지 않았거나 import 실패) resolver가 이
파서로 폴백한다. ``readpst -e -D``로 PST 전체를 임시 디렉터리에 개별
``.eml`` 파일로 풀어낸 뒤, 표준 라이브러리 ``email`` 패키지로 하나씩
읽어 yield하고 **읽은 즉시 삭제**한다 — PST 크기만큼의 임시 디스크가
필요한 대신, 인덱싱 도중 메모리에는 순간에 하나의 메일만 존재한다.

이 파서는 이 개발 환경에서 실제 ``readpst``(pst-utils, libpst 0.6.76)를
설치해 CLI 동작(도움말, 버전, 옵션)을 확인했다. 다만 실제 PST 샘플
파일을 구하지 못해 "PST → .eml 추출 → email 파싱" 전체 경로는 이
세션에서 end-to-end로 실행 검증하지 못했다 — README.md의 검증 현황에
정직하게 남긴다.
"""

from __future__ import annotations

import email
import email.parser
import email.policy
import logging
import os
import shutil
import subprocess
import tempfile
from collections.abc import Iterator

from ..models import RawMessage
from .base import BaseParser

logger = logging.getLogger(__name__)

# readpst -e -D는 원본 PST와 비슷하거나(첨부가 별도 파일로 안 빠지고
# .eml 안에 base64로 인라인되므로 base64 팽창률 약 1.37배) 더 큰 용량을
# 임시 디렉터리에 만든다. 안전 마진을 넉넉히 둔다.
_DISK_SAFETY_FACTOR = 1.6

# readpst 전체 추출이 끝나지 않고 멈추는 손상 PST에 대비한 타임아웃(초).
# 매우 큰 PST(수십 GB)도 고려해 넉넉히 둔다 — 그래도 걸리면 손상으로
# 간주해 그 파일만 FAILED 처리한다.
_READPST_TIMEOUT_SECONDS = 6 * 60 * 60


class ReadpstParser(BaseParser):
    """``readpst`` 서브프로세스로 PST를 풀어 RawMessage 스트림으로 노출한다."""

    def __init__(self, path: str, tmp_dir_base: str | None = None) -> None:
        super().__init__(path)
        self._tmp_dir_base = tmp_dir_base
        self._tmp_dir: str | None = None

    def open(self) -> None:
        if shutil.which("readpst") is None:
            raise RuntimeError("readpst 실행 파일을 찾을 수 없습니다 (번들되지 않음)")

        pst_size = os.path.getsize(self._path)
        base_dir = self._tmp_dir_base or tempfile.gettempdir()
        os.makedirs(base_dir, exist_ok=True)
        free = shutil.disk_usage(base_dir).free
        required = int(pst_size * _DISK_SAFETY_FACTOR)
        if free < required:
            raise RuntimeError(
                f"readpst 임시 추출에 필요한 디스크 공간이 부족합니다: "
                f"필요 약 {required / 1e9:.1f}GB, 여유 {free / 1e9:.1f}GB "
                f"(경로: {base_dir})"
            )

        self._tmp_dir = tempfile.mkdtemp(prefix="pst_readpst_", dir=self._tmp_dir_base)
        self._run_readpst(self._path, self._tmp_dir)

    @staticmethod
    def _run_readpst(pst_path: str, out_dir: str) -> None:
        # -e: 폴더별로 개별 .eml 파일 생성 (하나의 mbox로 합치지 않음 —
        #     그래야 파일 단위로 스트리밍 읽기+즉시삭제가 가능하다)
        # -D: 삭제된(지워진) 항목도 포함해 최대한 복구 — 손상된 PST에서도
        #     가능한 많이 건져내기 위함
        # -o: 출력 디렉터리
        result = subprocess.run(
            ["readpst", "-e", "-D", "-o", out_dir, pst_path],
            capture_output=True,
            timeout=_READPST_TIMEOUT_SECONDS,
            check=False,
        )
        if result.returncode != 0:
            stderr = (result.stderr or b"").decode("utf-8", errors="replace")[:2000]
            raise RuntimeError(f"readpst 실행 실패(exit={result.returncode}): {stderr}")

    def close(self) -> None:
        if self._tmp_dir and os.path.isdir(self._tmp_dir):
            shutil.rmtree(self._tmp_dir, ignore_errors=True)
        self._tmp_dir = None

    def iter_messages(self) -> Iterator[RawMessage]:
        if self._tmp_dir is None:
            raise RuntimeError("open()을 먼저 호출해야 합니다")

        for root, dirs, files in os.walk(self._tmp_dir):
            # os.walk 순서를 결정론적으로 고정 — fetch_attachment가 나중에
            # readpst를 재실행했을 때도 같은 folder_path/filename으로 같은
            # 메일을 다시 찾을 수 있어야 한다(같은 PST 입력이면 readpst의
            # 폴더/파일 이름 부여가 결정론적이라는 전제).
            dirs.sort()
            files.sort()
            rel_folder = os.path.relpath(root, self._tmp_dir)
            folder_path = "" if rel_folder == "." else rel_folder.replace(os.sep, "/")

            for fname in files:
                fpath = os.path.join(root, fname)
                try:
                    with open(fpath, "rb") as fh:
                        raw = fh.read()
                    parsed = email.parser.BytesParser(policy=email.policy.compat32).parsebytes(raw)
                    record = self._build_raw_message(parsed, folder_path, fname, len(raw))
                except Exception:
                    logger.debug("readpst: %s 파싱 실패, 건너뜀", fpath, exc_info=True)
                    _safe_remove(fpath)
                    continue

                try:
                    yield record
                finally:
                    # "읽은 즉시 삭제" — 제너레이터가 재개되는 시점(다음
                    # 메일을 요청받은 시점, 또는 소비자가 중간에 멈춰
                    # GeneratorExit이 발생한 시점)에 실행된다.
                    _safe_remove(fpath)

    def _build_raw_message(
        self, parsed: email.message.Message, folder_path: str, fname: str, raw_size: int
    ) -> RawMessage:
        headers: dict[str, str] = {}
        for key, value in parsed.items():
            headers.setdefault(key.lower(), str(value))

        plain_bytes, html_bytes = _extract_bodies(parsed)
        attachment_names = _extract_attachment_names(parsed)

        return RawMessage(
            headers=headers,
            body_bytes=plain_bytes,
            html_bytes=html_bytes,
            folder_path=folder_path,
            attachment_names=attachment_names,
            native_time=None,  # indexer가 headers['date'] 원문을 직접 해석한다
            size_bytes=raw_size,
            locator={
                "parser": "readpst",
                "source_path": self._path,
                "folder_path": folder_path,
                "filename": fname,
            },
            source_path=self._path,
        )

    def fetch_attachment(self, locator: object, index: int) -> tuple[str, bytes] | None:
        """``attach`` 명령 전용: PST를 다시 풀어 해당 메일의 첨부를 꺼낸다.

        readpst는 전량 추출 방식이라 첨부 하나를 위해 PST 전체를 다시
        풀어야 한다 — 느리지만(README에 명시) 상시 저장 비용은 0이다.
        인덱싱 경로에서는 절대 호출되지 않는다.
        """
        if not isinstance(locator, dict) or locator.get("parser") != "readpst":
            return None
        if shutil.which("readpst") is None:
            raise RuntimeError("readpst 실행 파일을 찾을 수 없습니다")

        pst_path = locator["source_path"]
        tmp_dir = tempfile.mkdtemp(prefix="pst_readpst_attach_")
        try:
            self._run_readpst(pst_path, tmp_dir)
            folder_path = locator.get("folder_path") or ""
            filename = locator["filename"]
            full_path = os.path.join(tmp_dir, folder_path, filename) if folder_path else os.path.join(
                tmp_dir, filename
            )
            with open(full_path, "rb") as fh:
                raw = fh.read()
            parsed = email.parser.BytesParser(policy=email.policy.compat32).parsebytes(raw)
            parts = _attachment_parts(parsed)
            if index < 0 or index >= len(parts):
                return None
            part = parts[index]
            payload = part.get_payload(decode=True)
            data = payload if isinstance(payload, bytes) else b""
            name = str(part.get_filename() or f"attachment_{index}")
            return name, data
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)


def _safe_remove(path: str) -> None:
    try:
        os.remove(path)
    except OSError:
        logger.debug("readpst: 임시 파일 삭제 실패: %s", path, exc_info=True)


def _extract_bodies(msg: email.message.Message) -> tuple[bytes, bytes | None]:
    """plain/html 본문을 "디코딩 전 원본 바이트"로 꺼낸다.

    ``get_payload(decode=True)``는 Content-Transfer-Encoding(base64,
    quoted-printable 등)만 해제할 뿐 문자셋 디코딩은 하지 않는다 — 문자셋
    판별·폴백은 이 프로젝트에서 항상 indexer.py의 safe_decode()가
    전담해야 하므로(헤더가 잘못 명시한 문자셋을 그대로 믿지 않기 위해),
    여기서는 절대 str로 바꾸지 않는다.
    """
    plain_bytes = b""
    html_bytes: bytes | None = None

    if msg.is_multipart():
        for part in msg.walk():
            if part.is_multipart():
                continue
            disp = str(part.get("Content-Disposition") or "").lower()
            if "attachment" in disp:
                continue
            ctype = part.get_content_type()
            if ctype == "text/plain" and not plain_bytes:
                payload = part.get_payload(decode=True)
                # get_payload(decode=True)는 보통 bytes를 주지만, 파트가
                # message/rfc822 같은 중첩 메시지면 이론상 Message
                # 객체가 나올 수도 있다 — 그런 비정상 케이스는 본문으로
                # 취급하지 않고 조용히 건너뛴다(규칙 5).
                if isinstance(payload, bytes):
                    plain_bytes = payload
            elif ctype == "text/html" and html_bytes is None:
                payload = part.get_payload(decode=True)
                if isinstance(payload, bytes):
                    html_bytes = payload
    else:
        payload = msg.get_payload(decode=True)
        resolved_payload = payload if isinstance(payload, bytes) else b""
        if msg.get_content_type() == "text/html":
            html_bytes = resolved_payload
        else:
            plain_bytes = resolved_payload

    return plain_bytes, html_bytes


def _attachment_parts(msg: email.message.Message) -> list[email.message.Message]:
    """파일명이 있는(=첨부로 볼 수 있는) leaf 파트를 순서대로 반환."""
    if not msg.is_multipart():
        return []
    return [part for part in msg.walk() if not part.is_multipart() and part.get_filename()]


def _extract_attachment_names(msg: email.message.Message) -> list[str]:
    return [str(part.get_filename()) for part in _attachment_parts(msg)]
