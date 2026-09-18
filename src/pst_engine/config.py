"""``config/default.yaml`` 로더.

PyYAML이 번들되어 있으면 그것으로 파싱한다. 없으면(주로 이 저장소를
직접 체크아웃해 pip 없이 개발/테스트할 때) 표준 라이브러리만으로 동작하는
최소 YAML 서브셋 파서로 폴백한다 — 주석, ``key: value``, 임의 깊이의
들여쓰기 중첩, ``- item`` 리스트, 따옴표 문자열/숫자/불리언/null 정도만
지원하면 ``default.yaml``을 읽기에 충분하다.

두 경로 모두 최종적으로 같은 중첩 dict를 만들어내므로, 이후 단계(설정값
평탄화)는 파서 종류와 무관하게 동일하게 동작한다. test_config.py가 두
경로의 결과가 같은지 검증한다.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import optional_deps

_DEFAULT_YAML_PATH = Path(__file__).resolve().parent.parent.parent / "config" / "default.yaml"


# ---------------------------------------------------------------------------
# 표준 라이브러리 전용 최소 YAML 서브셋 파서 (PyYAML 미번들 시 폴백)
# ---------------------------------------------------------------------------

_SCALAR_TRUE = {"true", "yes", "on"}
_SCALAR_FALSE = {"false", "no", "off"}
_SCALAR_NULL = {"null", "~", "none", ""}


def _parse_scalar(raw: str) -> Any:
    """따옴표/숫자/불리언/null을 판별해 파이썬 값으로 변환한다."""
    s = raw.strip()
    if not s:
        return None
    if s[0] == s[-1] and s[0] in "\"'" and len(s) >= 2:
        return s[1:-1]
    # 인라인 주석 제거 (따옴표 안이 아닌 '#'만) — 이미 위에서 따옴표 케이스는
    # 처리했으므로 여기서는 단순히 '#' 이후를 자른다.
    if "#" in s:
        s = s.split("#", 1)[0].strip()
    low = s.lower()
    if low in _SCALAR_NULL:
        return None
    if low in _SCALAR_TRUE:
        return True
    if low in _SCALAR_FALSE:
        return False
    if re.fullmatch(r"-?\d+", s):
        return int(s)
    if re.fullmatch(r"-?\d+\.\d+", s):
        return float(s)
    if s.startswith("[") and s.endswith("]"):
        # 인라인 리스트: [0.5, 1, 2]
        inner = s[1:-1].strip()
        if not inner:
            return []
        return [_parse_scalar(part) for part in inner.split(",")]
    return s


def _strip_comment_line(line: str) -> str:
    """줄 전체가 주석이거나 빈 줄이면 빈 문자열을 반환(스킵 대상 표시)."""
    stripped = line.strip()
    if not stripped or stripped.startswith("#"):
        return ""
    return line


def parse_yaml_subset(text: str) -> dict[str, Any]:
    """들여쓰기 스택 기반으로 중첩 dict/list를 만드는 최소 YAML 파서.

    지원: 주석(#), ``key: value``, ``key:``(다음 줄부터 중첩 또는 리스트),
    ``- item`` 리스트, 따옴표 문자열, 숫자, 불리언, null. 그 외(anchor,
    flow mapping, multiline block 등)는 지원하지 않는다 — default.yaml이
    이 부분을 쓰지 않도록 설계했다.

    ``key:``처럼 값이 없는 줄은 그 아래가 dict로 이어질지 list로
    이어질지 그 줄만 봐서는 알 수 없다 — 그래서 먼저 주석/빈 줄을 걷어낸
    ``(들여쓰기, 내용)`` 목록을 통째로 만든 뒤, 한 줄 앞을 미리보기
    (lookahead)해서 다음 줄이 더 깊은 들여쓰기의 ``"- "`` 항목이면
    list를, 아니면 dict를 만든다.
    """
    lines: list[tuple[int, str]] = []
    for raw_line in text.splitlines():
        stripped_line = _strip_comment_line(raw_line)
        if not stripped_line:
            continue
        indent = len(stripped_line) - len(stripped_line.lstrip(" "))
        lines.append((indent, stripped_line.strip()))

    root: dict[str, Any] = {}
    # 스택 원소: (들여쓰기 폭, 컨테이너(dict 또는 list))
    stack: list[tuple[int, Any]] = [(-1, root)]

    i = 0
    n = len(lines)
    while i < n:
        indent, content = lines[i]

        # 현재 들여쓰기보다 깊은 스택 프레임을 모두 pop
        while stack and indent <= stack[-1][0]:
            stack.pop()
        _parent_indent, parent = stack[-1]

        if content.startswith("- "):
            item_raw = content[2:]
            if not isinstance(parent, list):
                raise ValueError(f"YAML 파싱 오류: 리스트가 아닌 곳에 '-' 항목: {content!r}")
            if ":" in item_raw and not item_raw.strip().startswith(("\"", "'")):
                # "- key: value" 형태의 리스트 안 맵 (default.yaml에서는
                # 쓰지 않지만 견고성을 위해 최소 지원)
                key, _, val = item_raw.partition(":")
                new_map: dict[str, Any] = {key.strip(): _parse_scalar(val)}
                parent.append(new_map)
                stack.append((indent, new_map))
            else:
                parent.append(_parse_scalar(item_raw))
            i += 1
            continue

        if ":" not in content:
            raise ValueError(f"YAML 파싱 오류: 콜론 없는 줄: {content!r}")

        key, _, value = content.partition(":")
        key = key.strip().strip("\"'")
        value = value.strip()

        if not isinstance(parent, dict):
            # 함수 인자 타입 검증이 아니라 "파싱 중인 문서 구조"에 대한
            # 검증이므로 TypeError가 아니라 ValueError가 맞다.
            raise ValueError(f"YAML 파싱 오류: dict가 아닌 곳에 key: value: {content!r}")  # noqa: TRY004

        if value == "":
            next_is_list_item = (
                i + 1 < n and lines[i + 1][0] > indent and lines[i + 1][1].startswith("- ")
            )
            container: Any = [] if next_is_list_item else {}
            parent[key] = container
            stack.append((indent, container))
        else:
            parent[key] = _parse_scalar(value)
        i += 1

    return root


def _load_raw_yaml(path: Path) -> dict[str, Any]:
    """PyYAML이 있으면 그것으로, 없으면 서브셋 파서로 YAML 파일을 읽는다.

    파일이 없으면 빈 dict를 반환한다(설정 파일이 없어도 내장 기본값으로
    동작해야 한다).
    """
    if not path.exists():
        return {}
    text = path.read_text(encoding="utf-8")
    yaml_mod = optional_deps.get_yaml()
    if yaml_mod is not None:
        data = yaml_mod.safe_load(text)
        return data or {}
    return parse_yaml_subset(text)


# ---------------------------------------------------------------------------
# 평탄화된 설정 객체
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class Config:
    """엔진 전역 설정. 모든 필드는 합리적인 기본값을 가지므로 YAML이
    없거나 일부 키가 빠져 있어도 항상 동작한다.
    """

    batch_size: int = 1000
    workers: int = 0  # 0 => min(4, os.cpu_count())
    retry_max_attempts: int = 3
    retry_backoff_seconds: tuple[float, ...] = (0.5, 1.0, 2.0)
    readpst_tmp_dir: str | None = None

    fts_tokenize: str = "unicode61 remove_diacritics 0"
    fts_detail: str = "column"
    relevance_candidates: int = 5000
    snippet_len: int = 300
    strip_quoted_replies: bool = True

    max_raw_bytes: int = 256 * 1024

    db_path: str = "data/mail_index.db"
    state_path: str = "data/indexing_log.json"
    errors_path: str = "data/errors.jsonl"

    raw: dict[str, Any] = field(default_factory=dict, repr=False)


def _get(d: dict[str, Any], *keys: str, default: Any = None) -> Any:
    node: Any = d
    for k in keys:
        if not isinstance(node, dict) or k not in node:
            return default
        node = node[k]
    return node if node is not None else default


def load_config(path: str | Path | None = None) -> Config:
    """설정 파일을 로드해 :class:`Config`로 평탄화한다.

    ``path``를 생략하면 ``config/default.yaml``을 찾는다. 파일이 없거나
    일부 키가 비어 있어도 예외를 내지 않고 기본값으로 채운다 — 설정
    파일 문제로 인덱싱/검색이 죽는 일은 없어야 한다.
    """
    target = Path(path) if path is not None else _DEFAULT_YAML_PATH
    try:
        raw = _load_raw_yaml(target)
    except Exception:
        # 설정 파일이 손상돼 있어도 프로그램 전체가 죽지 않는다 — 내장
        # 기본값으로 계속 진행한다(CLAUDE.md 규칙 5의 정신을 설정 로딩에도
        # 적용).
        raw = {}

    backoff = _get(raw, "indexing", "retry", "backoff_seconds")
    backoff_tuple = tuple(float(x) for x in backoff) if isinstance(backoff, list) else (0.5, 1.0, 2.0)

    return Config(
        batch_size=int(_get(raw, "indexing", "batch_size", default=1000)),
        workers=int(_get(raw, "indexing", "workers", default=0)),
        retry_max_attempts=int(_get(raw, "indexing", "retry", "max_attempts", default=3)),
        retry_backoff_seconds=backoff_tuple,
        readpst_tmp_dir=_get(raw, "indexing", "readpst_tmp_dir"),
        fts_tokenize=str(_get(raw, "search", "fts", "tokenize", default="unicode61 remove_diacritics 0")),
        fts_detail=str(_get(raw, "search", "fts", "detail", default="column")),
        relevance_candidates=int(_get(raw, "search", "relevance_candidates", default=5000)),
        snippet_len=int(_get(raw, "search", "snippet_len", default=300)),
        strip_quoted_replies=bool(_get(raw, "search", "strip_quoted_replies", default=True)),
        max_raw_bytes=int(_get(raw, "encoding", "max_raw_bytes", default=256 * 1024)),
        db_path=str(_get(raw, "paths", "db", default="data/mail_index.db")),
        state_path=str(_get(raw, "paths", "state", default="data/indexing_log.json")),
        errors_path=str(_get(raw, "paths", "errors", default="data/errors.jsonl")),
        raw=raw,
    )
