"""파서 공통 인터페이스.

모든 파서(pypff/readpst/extract_msg)는 이 프로토콜을 만족한다:

- ``open()``            : 생성자에 넘긴 경로를 연다. 실패하면 예외를
                           raise해도 된다 — resolver.py가 선택 단계에서
                           그 다음 파서로 넘어가는 판단에 쓴다.
- ``iter_messages()``   : ``RawMessage``를 하나씩 yield하는 제너레이터.
                           절대 리스트로 전체를 모으지 않는다(규칙 3).
- ``close()``           : 리소스 정리. 여러 번 호출해도 안전해야 한다.
- ``fetch_attachment(locator, index)`` : 인덱싱이 끝난 뒤 ``attach``
                           명령이 첨부 "본체"를 온디맨드로 꺼낼 때만
                           쓴다. 인덱싱 경로(iter_messages)에서는 절대
                           호출되지 않는다 — 이게 첨부 본체를 저장하지
                           않고도 상시 용량 0을 유지하는 핵심이다.

파서는 필드 정규화를 하지 않는다(역할 경계, CLAUDE.md). ``RawMessage``는
최대한 원본 그대로이며, 주소 분리·날짜 epoch 변환·본문 인코딩 폴백은
전부 ``indexer.py``의 몫이다.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Protocol, TypeVar, runtime_checkable

from ..models import RawMessage

_ParserT = TypeVar("_ParserT", bound="BaseParser")


@runtime_checkable
class ParserProtocol(Protocol):
    def open(self) -> None: ...

    def iter_messages(self) -> Iterator[RawMessage]: ...

    def close(self) -> None: ...

    def fetch_attachment(self, locator: object, index: int) -> tuple[str, bytes] | None: ...

    def __enter__(self) -> ParserProtocol: ...

    def __exit__(self, *exc: object) -> None: ...


class BaseParser:
    """``open``/``close``를 컨텍스트 매니저로 감싸주는 공통 보일러플레이트.

    각 파서 구현은 이 클래스를 상속해 ``open()``/``close()``/
    ``iter_messages()``/``fetch_attachment()``만 채우면 된다.
    """

    def __init__(self, path: str) -> None:
        self._path = path

    def open(self) -> None:  # pragma: no cover - 하위 클래스가 구현
        raise NotImplementedError

    def close(self) -> None:  # pragma: no cover - 하위 클래스가 구현
        raise NotImplementedError

    def __enter__(self: _ParserT) -> _ParserT:
        # typing.Self는 3.11+ 전용이라 3.10 호환을 위해 TypeVar로 흉내낸다
        # — 그래야 PypffParser().open()처럼 하위 클래스 타입 그대로
        # 돌아온다는 걸 mypy가 알고, resolver.py의 ParserProtocol 구조적
        # 타이핑 검사도 통과한다(하위 클래스가 실제로 프로토콜 전체를
        # 구현하는지는 각 서브클래스에서 확인된다).
        self.open()
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
