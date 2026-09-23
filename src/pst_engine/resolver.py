"""파서 선택기 (LibraryResolver).

CLAUDE.md의 4단계 규칙을 결정론적으로 구현한다.

    1. import pypff 성공?          → PypffParser
    2. shutil.which("readpst")?    → ReadpstParser
    3. 확장자가 .msg 인가?          → ExtractMsgParser
    4. 전부 실패                   → None (호출자가 FAILED 처리)

파일을 실제로 열어보고 판단하지 않는다 — "이 파일 유형에 이 파서를 쓸
수 있는가"만 저렴하게 확인한다(모듈 import 여부 + 실행 파일 존재 여부 +
확장자). 실제로 여는 중에 나는 런타임 오류(손상된 PST 등)는
orchestrator.py가 파일 단위로 잡아 FAILED 처리한다 — 그것은 "파서
확보"가 아니라 "파싱 실패"이므로 역할이 다르다.

필드 추출은 절대 하지 않는다(역할 경계) — 이 모듈은 어떤 파서 *클래스*를
쓸지와 그 사유만 돌려준다.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
from collections.abc import Callable
from dataclasses import dataclass

from . import optional_deps
from .config import Config
from .parsers.base import ParserProtocol
from .parsers.extractmsg_parser import ExtractMsgParser
from .parsers.pypff_parser import PypffParser
from .parsers.readpst_parser import ReadpstParser

_PST_EXTENSIONS = {".pst", ".ost"}


@dataclass(slots=True)
class ResolvedParser:
    """resolver가 내린 결정. 실제 인스턴스는 :meth:`instantiate`로 만든다
    (선택 시점에는 아무것도 열지 않는다 — 생성 비용을 미룬다).
    """

    name: str
    reason: str
    factory: Callable[[], ParserProtocol]

    def instantiate(self) -> ParserProtocol:
        return self.factory()


def _looks_like_msg_source(path: str) -> bool:
    """``.msg`` 단일 파일이거나, ``.msg``가 하나라도 있는 디렉터리인가."""
    if os.path.isfile(path):
        return path.lower().endswith(".msg")
    if os.path.isdir(path):
        for _root, _dirs, files in os.walk(path):
            if any(f.lower().endswith(".msg") for f in files):
                return True
    return False


def _looks_like_pst_container(path: str) -> bool:
    """PST/OST로 볼 수 있는가. 확장자가 없거나 낯설어도(사내 아카이브에서
    흔함) ``.msg``만 아니면 PST 컨테이너 가능성을 열어 둔다 — pypff/readpst
    가 실제로 열어보고 아니면 알아서 실패하므로, 여기서는 명백히 아닌
    경우(.msg, 디렉터리)만 걸러낸다.
    """
    if not os.path.isfile(path):
        return False
    ext = os.path.splitext(path)[1].lower()
    if ext in _PST_EXTENSIONS:
        return True
    return ext != ".msg"


def _find_readpst_binary() -> str | None:
    """PATH뿐 아니라, PyInstaller onefile 배포 시 실행 파일 옆에 동봉된
    readpst(.exe)도 탐색한다 — 사내망은 PATH 등록조차 못 할 수 있다.
    """
    found = shutil.which("readpst")
    if found:
        return found
    exe_dir = os.path.dirname(os.path.abspath(sys.executable))
    candidate_name = "readpst.exe" if os.name == "nt" else "readpst"
    candidate = os.path.join(exe_dir, candidate_name)
    if os.path.isfile(candidate):
        return candidate
    return None


def resolve_parser(path: str, config: Config) -> ResolvedParser | None:
    """경로 하나에 대해 4단계 규칙을 적용해 파서를 고른다.

    아무것도 쓸 수 없으면 ``None``을 반환한다 — 호출자(orchestrator)는
    이를 해당 파일의 FAILED 상태와 :func:`install_guidance` 안내로
    이어간다.
    """
    pst_like = _looks_like_pst_container(path)
    msg_like = _looks_like_msg_source(path)

    # 1) pypff
    if pst_like and optional_deps.get_pypff() is not None:
        return ResolvedParser(
            name="pypff",
            reason="pypff 모듈 사용 가능 (1순위)",
            factory=lambda: PypffParser(path),
        )

    # 2) readpst
    if pst_like:
        readpst_bin = _find_readpst_binary()
        if readpst_bin is not None:
            return ResolvedParser(
                name="readpst",
                reason=f"readpst 실행 파일 감지({readpst_bin}), pypff 미사용 (2순위)",
                factory=lambda: ReadpstParser(path, tmp_dir_base=config.readpst_tmp_dir),
            )

    # 3) extract_msg (.msg 전용)
    if msg_like and optional_deps.get_extract_msg() is not None:
        return ResolvedParser(
            name="extract_msg",
            reason=".msg 소스 + extract_msg 모듈 사용 가능 (3순위)",
            factory=lambda: ExtractMsgParser(path),
        )

    # 4) 전부 실패
    return None


# locator dict의 "parser" 키(각 파서가 스스로 적어 넣는다) -> 그 파서를
# 다시 인스턴스화하는 방법. 색인 시점에 어떤 파서가 이 메일을 만들어
# 냈는지가 곧 attach 시점에 첨부를 다시 꺼낼 방법을 정한다 — 파일
# 확장자로 다시 추측하지 않고 locator에 박아둔 사실을 그대로 믿는다.
_LOCATOR_PARSER_FACTORIES: dict[str, Callable[[str, Config], ParserProtocol]] = {
    "pypff": lambda file_path, config: PypffParser(file_path),
    "readpst": lambda file_path, config: ReadpstParser(file_path, tmp_dir_base=config.readpst_tmp_dir),
    "extractmsg": lambda file_path, config: ExtractMsgParser(file_path),
}


def fetch_attachment(file_path: str, locator_json: str, index: int, config: Config) -> tuple[str, bytes] | None:
    """``attach`` 명령 전용: 색인 시 저장해 둔 locator로 원본을 다시 열어
    첨부 "본체" 하나를 꺼낸다. 인덱싱 경로에서는 절대 쓰이지 않는다 —
    이게 첨부 본체를 DB에 저장하지 않고도 상시 용량 0을 유지하는 대가로,
    사용자가 실제로 첨부를 원할 때만 원본 PST/MSG를 다시 연다.
    """
    if not locator_json:
        return None
    try:
        locator = json.loads(locator_json)
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(locator, dict):
        return None

    parser_name = locator.get("parser")
    if not isinstance(parser_name, str):
        return None
    factory = _LOCATOR_PARSER_FACTORIES.get(parser_name)
    if factory is None:
        return None

    parser = factory(file_path, config)
    with parser:
        return parser.fetch_attachment(locator, index)


def install_guidance() -> str:
    """파서를 하나도 못 찾았을 때 보여줄 안내 문자열.

    CLAUDE.md 규칙 8: 자동으로 pip install을 실행하지 않는다. 안내
    메시지만 출력한다.
    """
    summary = optional_deps.available_summary()
    lines = [
        "PST/MSG를 처리할 파서를 찾지 못했습니다. 다음 중 하나가 필요합니다:",
        "  1) pypff (libpff) 번들 또는 설치",
        "  2) readpst 실행 파일 (pst-utils / libpst)",
        "  3) extract_msg 번들 또는 설치 (.msg 파일에 한함)",
        "",
        f"현재 이 프로세스에서 감지된 선택 패키지: {summary}",
        "",
        "이 프로그램은 자동으로 패키지를 설치하지 않습니다. 배포용",
        "실행 파일에는 이들이 미리 번들되어 있어야 하며(packaging/BUILD.md",
        "참조), 사내망 환경에서는 pip install 자체가 불가능한 것을",
        "전제로 설계되었습니다.",
    ]
    return "\n".join(lines)
