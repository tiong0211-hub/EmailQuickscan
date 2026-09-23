"""PyInstaller 전용 진입점.

``src/pst_engine/cli.py``를 PyInstaller Analysis의 최상위 스크립트로
직접 지정하면 안 된다 — ``cli.py``는 ``pst_engine`` 패키지의 일부로서
상대 import(``from . import __version__`` 등)를 쓰는데, PyInstaller가
최상위 스크립트를 패키지 컨텍스트 없이 ``__main__``으로 바로 실행하면
그 상대 import가 즉시 ``ImportError: attempted relative import with no
known parent package``로 죽는다.

**이건 추측이 아니라 이 세션에서 실제로 재현해 확인한 문제다**: Linux용
PyInstaller로 같은 spec을 직접 빌드·실행해 보니 정확히 이 에러로 즉시
종료됐다(사내망 Windows에서도 동일하게 재현될 구조적 문제이므로 빌드
결과를 기다릴 필요 없이 지금 고친다).

이 얇은 진입점 스크립트는 ``pst_engine`` 패키지 밖에 있으므로 절대
경로로 import한다 — 그러면 상대 import 문제가 아예 생기지 않는다.
"""

import multiprocessing
import sys

if __name__ == "__main__":
    # Windows spawn 멀티프로세싱 대응: 자식 프로세스를 만들기 전(사실상
    # 프로그램 시작 시점)에 가장 먼저 호출해야 한다 — cli.main()도 다시
    # 호출하지만(이중 안전장치), 공식 권장대로 진입점 스크립트에서 한 번
    # 더 명시한다. 두 번 호출해도 안전하다(멱등).
    multiprocessing.freeze_support()

    from pst_engine.cli import main

    sys.exit(main())
