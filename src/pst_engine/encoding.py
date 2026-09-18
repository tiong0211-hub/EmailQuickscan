"""인코딩 폴백 유틸리티.

CLAUDE.md 규칙: ``safe_decode()``는 어떤 입력을 줘도 예외를 raise하지 않고
항상 ``(text, status)`` 튜플을 반환한다. 폴백 순서는

    utf-8 → (chardet 추정, 있으면 1회) → cp949 → euc-kr → latin-1

이다. latin-1은 1바이트=1코드포인트라 어떤 바이트열도 디코딩에 "성공"하므로
체인의 최종 방어선 역할을 한다 — 즉 ``failed``는 "예외가 났다"는 뜻이
아니라 "utf-8/cp949/euc-kr 중 어느 것도 맞지 않아 원본을 latin-1로만
보존했다"는 뜻이다. 이 경우 원본 바이트를 base64로 별도 보존해야
사람이 나중에 재해독을 시도할 수 있다(storage.py가 raw_body_b64에 담당).

chardet은 명세의 고정 폴백 체인을 "대체"하지 않는다. chardet이 높은
신뢰도로 특정 인코딩을 지목하면 그 인코딩을 체인 맨 앞에 한 번 끼워
시도할 뿐이며, 실패하면 원래 체인(utf-8→cp949→euc-kr→latin-1)으로
되돌아간다.
"""

from __future__ import annotations

import base64

from . import optional_deps

# 명세가 못박은 고정 폴백 순서. chardet 유무와 무관하게 항상 이 순서를
# 최종적으로 다 시도한다.
_FALLBACK_CHAIN: tuple[str, ...] = ("utf-8", "cp949", "euc-kr", "latin-1")

# chardet 추정을 신뢰할 최소 신뢰도. 너무 낮은 신뢰도의 추정을 앞세우면
# 오히려 잘못된 인코딩으로 "성공"해버려(예: cp949 바이트를 latin-1로 억지
# 디코딩) 나중에 깨진 문자열이 나올 위험이 있다.
_CHARDET_MIN_CONFIDENCE = 0.6

# 표준 인코딩 이름 → codecs가 인식하는 이름으로 정규화(주로 chardet이
# "EUC-KR", "CP949" 등 대소문자 섞어 반환하는 것을 흡수).
_NAME_ALIASES = {
    "euc-kr": "euc-kr",
    "euckr": "euc-kr",
    "cp949": "cp949",
    "uhc": "cp949",
    "utf-8": "utf-8",
    "utf8": "utf-8",
    "ascii": "utf-8",  # ascii는 utf-8의 부분집합이므로 utf-8로 시도해도 무방
}


def _chardet_guess(data: bytes) -> str | None:
    """chardet이 번들돼 있으면 신뢰도 높은 추정 인코딩 이름을 반환한다.

    chardet이 없거나, 추정에 실패하거나, 신뢰도가 낮으면 None을 반환한다.
    이 함수 자체도 절대 예외를 밖으로 내보내지 않는다 — chardet 내부
    구현이 던지는 예외까지 safe_decode의 "무조건 성공" 계약을 깨게 둘 수
    없기 때문이다.
    """
    chardet = optional_deps.get_chardet()
    if chardet is None:
        return None
    try:
        result = chardet.detect(data)
    except Exception:
        return None
    encoding = result.get("encoding")
    confidence = result.get("confidence") or 0.0
    if not encoding or confidence < _CHARDET_MIN_CONFIDENCE:
        return None
    normalized = _NAME_ALIASES.get(encoding.lower())
    return normalized


def safe_decode(data: bytes | None) -> tuple[str, str]:
    """바이트열을 텍스트로 디코딩한다. 절대 raise하지 않는다.

    반환값: ``(text, status)``
      - ``status == "ok"``       : utf-8로 성공
      - ``status == "fallback"`` : cp949 또는 euc-kr로 성공
                                    (chardet 추정이 이 둘 중 하나로 맞아도
                                    fallback으로 분류한다 — utf-8이 아닌
                                    이상 "완전한 정석"은 아니기 때문)
      - ``status == "failed"``   : latin-1까지 내려갔거나, 애초에 bytes가
                                    아닌 입력이 들어왔다. latin-1 자체는
                                    항상 "디코딩 성공"하므로 예외적 상황이
                                    아니라 "원본 인코딩을 확신할 수 없다"는
                                    뜻이다.
    """
    if not isinstance(data, (bytes, bytearray)):
        # bytes가 아닌 입력(None, 잘못된 타입 등)도 절대 raise하지 않고
        # failed로 분류해 빈 문자열을 돌려준다 — 파이프라인이 멈추지 않게.
        return "", "failed"

    raw = bytes(data)
    if not raw:
        return "", "ok"

    # 1) utf-8 정석 경로
    try:
        return raw.decode("utf-8", errors="strict"), "ok"
    except UnicodeDecodeError:
        pass

    # 2) chardet 추정(있으면) — 신뢰도가 높을 때만, 한 번만 시도
    guess = _chardet_guess(raw)
    if guess is not None:
        try:
            return raw.decode(guess, errors="strict"), "fallback"
        except (UnicodeDecodeError, LookupError):
            pass

    # 3) 명세 고정 체인 (utf-8은 이미 시도했으니 건너뛴다)
    for name in _FALLBACK_CHAIN[1:-1]:  # cp949, euc-kr만
        try:
            return raw.decode(name, errors="strict"), "fallback"
        except (UnicodeDecodeError, LookupError):
            continue

    # 4) latin-1 최종 방어선 — 항상 성공한다
    return raw.decode("latin-1", errors="strict"), "failed"


def encode_raw_preview(data: bytes | None, max_bytes: int) -> tuple[str | None, bool]:
    """디코딩 실패(``failed``) 시 원본을 base64로 보존하기 위한 헬퍼.

    ``max_bytes``를 넘기면 앞부분만 잘라 보존하고 두 번째 반환값으로
    ``True``(잘림)를 알린다 — storage.py가 이를 errors.jsonl에 기록할지
    판단하는 데 쓴다. 무제한 보존은 10GB PST에서 메모리 상한을 깨므로
    상한을 반드시 둔다.
    """
    if not isinstance(data, (bytes, bytearray)) or not data:
        return None, False
    raw = bytes(data)
    truncated = len(raw) > max_bytes
    if truncated:
        raw = raw[:max_bytes]
    return base64.b64encode(raw).decode("ascii"), truncated
