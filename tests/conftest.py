"""공용 pytest fixture. 네트워크 의존 없음 — 전부 가짜 파서/합성 데이터로
동작한다(실제 PST 샘플이 없어도 파이프라인 전체를 검증할 수 있어야
한다는 게 이 테스트 스위트의 목적이다).
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import pytest

from pst_engine.config import Config, load_config
from pst_engine.models import RawMessage
from pst_engine.parsers.base import BaseParser


@pytest.fixture
def config() -> Config:
    c = load_config()
    c.batch_size = 5
    return c


class FakeParser(BaseParser):
    """실제 PST 없이 indexer/orchestrator를 검증하기 위한 가짜 파서.

    파일 내용이 ``"FAKE:<n>"``이면 n통의 합성 메일을 만들어낸다.
    ``"CORRUPT"``가 들어있으면 ``open()``에서 예외를 던져 손상 PST를
    흉내낸다. 개별 메시지 하나를 일부러 깨뜨리려면(정규화 실패 유도)
    내용에 ``"BADMSG"``를 포함시킨다 — 그 몸통이 None이 되어 indexer의
    정규화 단계에서 예외가 나도록 만든다.
    """

    def open(self) -> None:
        with open(self._path, "rb") as f:
            content = f.read().decode()
        if "CORRUPT" in content:
            raise RuntimeError("손상된 PST (테스트 시뮬레이션)")
        self._n = int(content.split(":")[1].split(",")[0])
        self._bad_index = 1 if "BADMSG" in content else None

    def close(self) -> None:
        pass

    def iter_messages(self):
        import os

        base = os.path.basename(self._path)
        for i in range(self._n):
            body = f"본문입니다 계약 관련 내용 {i}. AI QA report included."
            headers = {
                "subject": f"{base} 계약서 검토 {i}",
                "from": f"user{i % 3}@corp.co.kr",
                "to": f"rcpt{i % 2}@corp.co.kr",
                "date": "Mon, 01 Jan 2024 09:00:00 +0900",
            }
            if self._bad_index is not None and i == self._bad_index:
                # subject를 문자열이 아닌 값으로 넣어 indexer._normalize의
                # ``.strip()`` 호출이 AttributeError를 내도록 유도한다 —
                # "개별 메일 하나가 정규화 중 실제로 예외를 내도 나머지는
                # 계속 처리된다"를 검증하기 위한 현실적인 결함 시뮬레이션
                # (파서가 이상한 타입의 헤더값을 주는 경우는 실제로도
                # 생길 수 있다).
                headers = {**headers, "subject": 12345}
            yield RawMessage(
                headers=headers,
                body_bytes=body.encode("utf-8"),
                folder_path="Inbox",
                attachment_names=["계약서.pdf"] if i % 4 == 0 else [],
                native_time=None,
                size_bytes=100,
                locator={"parser": "fake", "index": i},
                source_path=self._path,
            )

    def fetch_attachment(self, locator, index):
        if isinstance(locator, dict) and locator.get("parser") == "fake" and index == 0:
            return "계약서.pdf", b"%PDF-fake"
        return None


@pytest.fixture
def fake_resolver(monkeypatch):
    """resolver.resolve_parser를 몽키패치해 ``.pst``/``.fakepst`` 파일에
    항상 :class:`FakeParser`를 쓰도록 강제한다. orchestrator.py가 이미
    import해 간 이름(``orchestrator.resolve_parser``)도 함께 바꿔야
    실제로 적용된다.
    """
    from pst_engine import orchestrator, resolver
    from pst_engine.resolver import ResolvedParser

    def fake_resolve(path, config):
        if path.endswith((".pst", ".fakepst")):
            return ResolvedParser(name="fake", reason="테스트용", factory=lambda: FakeParser(path))
        return None

    monkeypatch.setattr(resolver, "resolve_parser", fake_resolve)
    monkeypatch.setattr(orchestrator, "resolve_parser", fake_resolve)
    return fake_resolve
