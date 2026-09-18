"""선택 패키지(가속 라이브러리) import를 한 곳에 모아 감싸는 모듈.

역할 경계: 이 모듈은 "패키지가 있는지 없는지"만 판단해 모듈 객체(또는 None)를
반환한다. 없을 때 무엇을 할지(폴백 로직 자체)는 호출부의 몫이다 — 그래야
storage.py/search.py/cli.py 등 각 모듈이 자신의 폴백을 스스로 책임지는
역할 경계가 유지된다.

사내망 배포 환경은 pip 설치가 불가능하므로, 아래 패키지들은 전부
"빌드 시점에 PyInstaller가 번들하거나, 번들되지 않았다면 조용히 폴백한다"는
전제로 다룬다. 런타임에 pip install을 시도하는 코드는 절대 넣지 않는다
(CLAUDE.md 규칙 8).
"""

from __future__ import annotations

import importlib
from functools import cache
from types import ModuleType


@cache
def _try_import(name: str) -> ModuleType | None:
    """모듈을 import 시도하고, 실패하면 예외 없이 None을 반환한다.

    lru_cache로 감싸 프로세스당 한 번만 import를 시도한다 — 없는 모듈을
    매 호출마다 다시 찾아보는 비용(파일시스템 스캔 등)을 피하기 위함이다.
    """
    try:
        return importlib.import_module(name)
    except ImportError:
        return None


def get_yaml() -> ModuleType | None:
    """PyYAML. 없으면 config.py가 표준 라이브러리 기반 서브셋 파서로 폴백한다."""
    return _try_import("yaml")


def get_chardet() -> ModuleType | None:
    """chardet. 없으면 encoding.py는 명세의 고정 폴백 체인만 사용한다."""
    return _try_import("chardet")


def get_rich() -> ModuleType | None:
    """rich. 없으면 cli.py가 내장 고정폭 표 렌더러로 폴백한다."""
    return _try_import("rich")


def get_pypff() -> ModuleType | None:
    """libpff-python. 없으면 resolver가 readpst → .msg 순으로 폴백한다."""
    return _try_import("pypff")


def get_extract_msg() -> ModuleType | None:
    """extract-msg. 없으면 .msg 파싱 경로가 비활성화된다."""
    return _try_import("extract_msg")


def available_summary() -> dict[str, bool]:
    """진단·로그용: 이 프로세스에서 어떤 선택 패키지가 실제로 잡혔는지 요약.

    `index --dry-run`이나 시작 로그에서 "이번 실행은 무엇으로 동작하는지"를
    사용자에게 보여주는 데 쓴다.
    """
    return {
        "yaml": get_yaml() is not None,
        "chardet": get_chardet() is not None,
        "rich": get_rich() is not None,
        "pypff": get_pypff() is not None,
        "extract_msg": get_extract_msg() is not None,
    }
