"""``StateManager`` — ``indexing_log.json``의 원자적 읽기/쓰기.

**워커 프로세스에서 호출하지 않는다**(역할 경계, CLAUDE.md). 파일 단위
상태와 증분 판별(재실행 시 무엇을 건너뛸지)은 orchestrator.py의 메인
프로세스만 결정한다 — 여러 워커가 동시에 이 JSON을 건드리면 Single
Writer 원칙이 깨진다.

CLAUDE.md의 상태 머신 중 ``RETRY``는 별도의 영속 상태로 두지 않았다.
DB busy/lock 재시도는 배치 하나를 커밋하는 짧은 순간에 일어나는 일이라
``storage.upsert_batch()`` 내부의 지수 백오프 루프로 이미 처리되며,
최종 실패해야만 예외가 올라와 그 파일이 ``FAILED``로 남는다. 파일
단위로 "RETRY 중"이라는 상태를 영속화해 봐야 재실행 판단에 쓸 정보가
늘지 않으므로(항상 WRITING에서 이어서 재순회) 의도적으로 생략했다 —
README.md "구현 중 조정한 것" 절에 기록.
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import asdict, dataclass, fields
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_VALID_STATUSES = {"DISCOVERED", "PARSING", "WRITING", "COMPLETED", "FAILED"}


@dataclass(slots=True)
class FileState:
    status: str = "DISCOVERED"
    last_batch_index: int = 0
    mails_written: int = 0
    updated_at: int = 0
    error: str | None = None
    size: int = 0
    mtime: float = 0.0


_FIELD_NAMES = tuple(f.name for f in fields(FileState))


class StateManager:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._data: dict[str, dict[str, Any]] = {}
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            self._data = {}
            return
        try:
            with self.path.open("r", encoding="utf-8") as fh:
                raw = json.load(fh)
            self._data = raw if isinstance(raw, dict) else {}
        except (json.JSONDecodeError, OSError):
            # 손상된 상태 파일도 프로그램을 멈추지 않는다 — 빈 상태로
            # 시작하면 최악의 경우 전 파일을 다시 순회할 뿐, 업서트가
            # 멱등이라 데이터가 깨지지는 않는다.
            logger.warning("state: %s 손상되어 빈 상태로 시작합니다", self.path, exc_info=True)
            self._data = {}

    def save(self) -> None:
        """tmp 파일에 쓴 뒤 ``os.replace``로 원자적 교체.

        같은 파일시스템 안에서의 ``os.replace``는 POSIX/Windows 모두
        원자적이므로, 쓰는 도중 프로세스가 죽어도(Ctrl+C, 강제종료)
        기존 파일이 반쯤 쓰인 내용으로 깨지는 일이 없다.
        """
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = self.path.with_name(f"{self.path.name}.tmp{os.getpid()}")
        with tmp_path.open("w", encoding="utf-8") as fh:
            json.dump(self._data, fh, ensure_ascii=False, indent=2, sort_keys=True)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_path, self.path)

    def get(self, file_path: str) -> FileState:
        raw = self._data.get(file_path, {})
        kwargs = {name: raw[name] for name in _FIELD_NAMES if name in raw}
        return FileState(**kwargs)

    def set(self, file_path: str, state: FileState) -> None:
        if state.status not in _VALID_STATUSES:
            raise ValueError(f"알 수 없는 상태: {state.status!r}")
        state.updated_at = int(time.time())
        self._data[file_path] = asdict(state)

    def should_skip(self, file_path: str, size: int, mtime: float) -> bool:
        """이미 COMPLETED이고 파일 크기/수정시각이 그대로면 건너뛴다.

        ``mtime``은 부동소수 epoch라 파일시스템/OS에 따라 미세한 오차가
        생길 수 있어 완전 일치 대신 작은 허용 오차로 비교한다.
        """
        st = self.get(file_path)
        return st.status == "COMPLETED" and st.size == size and abs(st.mtime - mtime) < 1.0

    def all_states(self) -> dict[str, FileState]:
        return {path: self.get(path) for path in self._data}
