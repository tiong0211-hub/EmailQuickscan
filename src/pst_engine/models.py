"""공용 데이터 타입.

두 개의 dataclass만 둔다:

- ``RawMessage``: 파서(``parsers/*.py``)가 내보내는 **정규화 전** 원본.
  파서는 필드 정규화를 하지 않는다는 역할 경계(CLAUDE.md) 때문에, 파서와
  indexer 사이를 잇는 운반용 타입이 별도로 필요하다.
- ``MailRecord``: indexer가 정규화를 끝낸 뒤 storage로 넘기는 최종 레코드.
  DB 스키마(확정 10컬럼 + 승인된 6컬럼)와 1:1로 대응한다.

두 타입 모두 ``slots=True``를 써서 100만 건 단위 스트리밍 시 인스턴스당
메모리를 줄인다(일반 dataclass는 인스턴스마다 ``__dict__``를 갖는다).
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(slots=True)
class RawMessage:
    """파서가 PST/EML/MSG에서 그대로 꺼낸 원본 메시지.

    필드는 최대한 가공 없이 담는다 — 예를 들어 ``to_raw``는 아직
    '; '로 정규화되지 않은, 파서가 반환한 그대로의 문자열(또는 리스트)이다.
    실제 정규화는 indexer.py의 몫이다.
    """

    # 헤더 이름(소문자로 정규화) -> 원본 값. 동일 이름이 여러 번 나오면
    # 첫 값만 남긴다. 비어 있으면 파서가 헤더를 못 얻은 것 — indexer가
    # subject/sender_name 등 파서가 별도로 채운 다른 필드로 최대한
    # 복구한다.
    headers: dict[str, str] = field(default_factory=dict)

    # 본문. bytes로 두는 이유: 인코딩 판별·폴백(safe_decode)을 indexer가
    # 전담하기 때문이다(파서는 디코딩하지 않는다).
    body_bytes: bytes = b""
    html_bytes: bytes | None = None

    # PST 내 원본 폴더 경로(예: "받은 편지함/2024"). 파서가 순회하며 알 수
    # 있는 유일한 곳이므로 여기서 채운다.
    folder_path: str = ""

    # 첨부 "이름"만 (본체는 절대 읽지 않는다 — 용량 정책).
    attachment_names: list[str] = field(default_factory=list)

    # 파서별 원본 발신 시각(이미 aware datetime이거나 None). indexer가
    # epoch로 변환한다.
    native_time: object | None = None

    size_bytes: int = 0

    # attach 명령이 나중에 이 메일의 첨부 "본체"를 원본 PST/EML/MSG에서
    # 다시 열어 꺼낼 때 쓰는 위치 정보. 파서마다 형태가 다르므로 불투명한
    # 값으로 둔다(pypff면 폴더 경로+메시지 인덱스, readpst/msg면 파일 경로).
    locator: object | None = None

    # 원본 PST/EML/MSG 파일의 절대 경로. file_path 컬럼으로 그대로 간다.
    source_path: str = ""


@dataclass(slots=True)
class MailRecord:
    """정규화가 끝난, DB에 그대로 upsert되는 레코드.

    컬럼 이름과 타입은 CLAUDE.md의 확정 스키마와 1:1로 대응한다.
    """

    message_id: str
    file_path: str
    folder_path: str
    from_addr: str
    to_addr: str
    cc_addr: str
    date_utc: int
    subject: str
    snippet: str
    has_attachment: int
    attachment_names: str
    size_bytes: int
    body_z: bytes  # zlib 압축된 본문 원문(인용문 포함, 검증·열람용)
    raw_body_b64: str | None
    decode_status: str  # "ok" | "fallback" | "failed"
    indexed_at: int
    # RawMessage.locator를 JSON으로 직렬화한 것(없으면 ""). attach 명령이
    # 색인이 끝난 뒤에도 첨부 "본체"를 원본 PST에서 다시 찾아 꺼낼 수
    # 있는 유일한 단서다 — 저장해 두지 않으면 인덱싱 시점의 위치 정보가
    # 영영 사라진다.
    locator_json: str = ""
