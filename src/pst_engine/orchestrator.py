"""파일 큐 + 워커 풀 + Single Writer 루프.

**핵심 원칙(CLAUDE.md)**: SQLite는 단일 writer만 허용한다. 그래서 파싱은
``multiprocessing``으로 파일 단위 병렬화하되, DB 쓰기와
``indexing_log.json`` 갱신은 이 모듈의 메인 프로세스 하나가 큐를 통해
직렬로 처리한다. 워커는 절대 ``storage.py``나 ``state.py``를 import하지
않는다(각 워커 프로세스는 별도 파이썬 인터프리터이므로, import해도
SQLite 커넥션이 공유되지는 않지만 — 역할 경계를 코드 구조로도
지키기 위해 워커 함수에서 그 두 모듈에 접근하지 않는다).

Windows 참고: ``multiprocessing``은 Windows에서 ``spawn`` 방식을 쓴다.
그래서 워커 함수는 반드시 모듈 최상위에 있어야 하고(중첩 함수/람다는
피클 불가), 진입점(``cli.py``)은 반드시
``multiprocessing.freeze_support()``를 호출해야 한다 — 안 하면
PyInstaller onefile exe가 자식 프로세스를 만들 때마다 전체 프로그램을
처음부터 다시 실행해 무한 재귀에 빠진다.
"""

from __future__ import annotations

import glob
import json
import logging
import multiprocessing as mp
import os
import queue
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from .config import Config
from .indexer import PSTIndexer
from .models import MailRecord
from .resolver import ResolvedParser, install_guidance, resolve_parser
from .state import FileState, StateManager
from .storage import MailStorageEngine

logger = logging.getLogger(__name__)

_PST_EXTENSIONS = (".pst", ".ost")
_MSG_EXTENSION = ".msg"

OnEvent = Callable[[str, str, object], None]


# ---------------------------------------------------------------------------
# 대상 파일 탐색
# ---------------------------------------------------------------------------


def discover_targets(input_paths: list[str], recursive: bool) -> list[str]:
    """CLI가 받은 경로/글로브 목록을 실제 "인덱싱 단위" 경로 목록으로 편다.

    한 "단위"는 PST/OST 파일 하나, 또는 .msg 파일이 담긴 디렉터리(혹은
    .msg 파일 하나) 전체다. 정렬된 순서를 반환해 재실행 시에도 처리
    순서가 안정적이다(디버깅·로그 재현성에 도움).
    """
    targets: list[str] = []
    seen: set[str] = set()

    def add(p: str) -> None:
        ap = os.path.abspath(p)
        if ap not in seen and os.path.exists(ap):
            seen.add(ap)
            targets.append(ap)

    for raw in input_paths:
        if any(ch in raw for ch in "*?["):
            # Windows cmd/PowerShell은 글로브를 셸이 확장해 주지 않으므로
            # 우리가 직접 처리해야 한다.
            for m in glob.glob(raw, recursive=recursive):
                _add_path(m, recursive, add)
        else:
            _add_path(raw, recursive, add)

    return sorted(targets)


def _add_path(path: str, recursive: bool, add: Callable[[str], None]) -> None:
    if os.path.isfile(path):
        ext = os.path.splitext(path)[1].lower()
        if ext in _PST_EXTENSIONS or ext == _MSG_EXTENSION:
            add(path)
        return

    if not os.path.isdir(path):
        return

    if _contains_any_msg(path):
        # .msg가 하나라도 있으면 이 디렉터리 전체를 하나의 인덱싱 단위로
        # 묶는다 — ExtractMsgParser가 어차피 재귀적으로 .msg를 찾는다.
        add(path)

    if recursive:
        for root, dirs, files in os.walk(path):
            dirs.sort()
            for fname in sorted(files):
                if os.path.splitext(fname)[1].lower() in _PST_EXTENSIONS:
                    add(os.path.join(root, fname))
    else:
        for fname in sorted(os.listdir(path)):
            fpath = os.path.join(path, fname)
            if os.path.isfile(fpath) and os.path.splitext(fname)[1].lower() in _PST_EXTENSIONS:
                add(fpath)


def _contains_any_msg(dir_path: str) -> bool:
    for _root, _dirs, files in os.walk(dir_path):
        if any(f.lower().endswith(_MSG_EXTENSION) for f in files):
            return True
    return False


@dataclass(slots=True)
class FilePlan:
    path: str
    size: int
    mtime: float
    resolved: ResolvedParser | None
    skip_reason: str | None = None  # 이미 COMPLETED라 건너뛸 때만 채움


def plan_files(
    input_paths: list[str], config: Config, *, recursive: bool, state: StateManager | None = None
) -> list[FilePlan]:
    """탐색 + 파서 선택 + (있으면) 증분 스킵 판정까지 미리 계산한다.

    ``index --dry-run``과 실제 인덱싱 실행이 같은 계획을 공유하도록
    분리했다 — dry-run은 이 결과를 출력만 하고, 실제 실행은 이 결과로
    워커에 작업을 분배한다.
    """
    plans: list[FilePlan] = []
    for path in discover_targets(input_paths, recursive):
        try:
            size = _path_size(path)
            mtime = os.path.getmtime(path)
        except OSError:
            size, mtime = 0, 0.0

        skip_reason = None
        if state is not None and state.should_skip(path, size, mtime):
            skip_reason = "이미 COMPLETED (크기/수정시각 동일)"

        resolved = None if skip_reason else resolve_parser(path, config)
        plans.append(FilePlan(path=path, size=size, mtime=mtime, resolved=resolved, skip_reason=skip_reason))
    return plans


def _path_size(path: str) -> int:
    if os.path.isfile(path):
        return os.path.getsize(path)
    total = 0
    for root, _dirs, files in os.walk(path):
        for fname in files:
            try:
                total += os.path.getsize(os.path.join(root, fname))
            except OSError:
                pass
    return total


# ---------------------------------------------------------------------------
# 워커 프로세스 — 반드시 모듈 최상위 함수 (Windows spawn 피클 제약)
# ---------------------------------------------------------------------------


def _worker_main(
    file_queue: mp.Queue[str | None],
    results_queue: mp.Queue[tuple],
    config: Config,
) -> None:
    """워커 프로세스 루프. ``storage.py``/``state.py``를 import하지 않는다
    — 파싱과 정규화만 하고, 결과는 전부 큐로 메인에 넘긴다.
    """
    indexer = PSTIndexer(config)
    while True:
        path = file_queue.get()
        if path is None:  # 종료 신호
            return

        resolved = resolve_parser(path, config)
        if resolved is None:
            results_queue.put(("failed", path, install_guidance()))
            continue

        results_queue.put(("resolved", path, {"name": resolved.name, "reason": resolved.reason}))

        total_written = 0
        batch_index = 0
        try:
            parser = resolved.instantiate()
            with parser:
                for batch, errors in indexer.iter_batches(parser, path):
                    if batch:
                        results_queue.put(("batch", path, batch))
                        total_written += len(batch)
                        batch_index += 1
                    if errors:
                        results_queue.put(("errors", path, errors))
            results_queue.put(("done", path, total_written))
        except Exception as exc:
            logger.exception("worker: %s 처리 중 예외", path)
            results_queue.put(("failed", path, f"{type(exc).__name__}: {exc}"))


# ---------------------------------------------------------------------------
# 메인 프로세스: Single Writer 루프
# ---------------------------------------------------------------------------


class ErrorLogger:
    """``data/errors.jsonl``에 JSON Lines로 추가 기록한다. 메인 프로세스만
    연다(storage.py와 마찬가지로 단일 writer).
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = self.path.open("a", encoding="utf-8")

    def log(self, entry: dict) -> None:
        self._fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
        self._fh.flush()

    def close(self) -> None:
        self._fh.close()


@dataclass(slots=True)
class IndexSummary:
    files_completed: int = 0
    files_failed: int = 0
    files_skipped: int = 0
    mails_written: int = 0
    interrupted: bool = False
    failures: dict[str, str] = field(default_factory=dict)


def run_index(
    input_paths: list[str],
    config: Config,
    db_path: str | Path,
    state_path: str | Path,
    errors_path: str | Path,
    *,
    recursive: bool = False,
    on_event: OnEvent | None = None,
    stop_flag: Callable[[], bool] | None = None,
) -> IndexSummary:
    """전체 인덱싱을 실행한다. 이미 처리된(COMPLETED) 파일은 건너뛴다.

    ``on_event(kind, file_path, payload)``는 모든 사건(resolved/batch/
    errors/done/failed/skipped)마다 메인 프로세스에서 동기 호출된다 —
    CLI는 진행 표시줄에, GUI는 스레드-세이프 큐에 넣는 데 쓴다.

    ``stop_flag()``가 True를 반환하거나 Ctrl+C(KeyboardInterrupt)가
    들어오면, 현재까지 커밋된 배치와 상태는 그대로 둔 채 워커를 정리하고
    반환한다 — 다음 실행이 ``WRITING`` 상태였던 파일을 처음부터
    재순회하되 업서트가 멱등이라 중복이 생기지 않는다.
    """
    state = StateManager(state_path)
    plans = plan_files(input_paths, config, recursive=recursive, state=state)

    summary = IndexSummary()
    to_process: list[FilePlan] = []
    for plan in plans:
        if plan.skip_reason:
            summary.files_skipped += 1
            if on_event:
                on_event("skipped", plan.path, plan.skip_reason)
            continue
        if plan.resolved is None:
            summary.files_failed += 1
            summary.failures[plan.path] = install_guidance()
            state.set(plan.path, FileState(status="FAILED", error="파서를 찾지 못함", size=plan.size, mtime=plan.mtime))
            if on_event:
                on_event("failed", plan.path, "파서를 찾지 못함")
            continue
        state.set(plan.path, FileState(status="PARSING", size=plan.size, mtime=plan.mtime))
        to_process.append(plan)
    state.save()

    if not to_process:
        return summary

    worker_count = config.workers if config.workers > 0 else min(4, os.cpu_count() or 1)
    worker_count = max(1, min(worker_count, len(to_process)))

    # maxsize = workers * 2 — 이 백프레셔가 메모리 상한을 지키는 핵심
    # 장치다(README 발견: 워커가 배치를 만들어도 메인이 못 따라가면
    # put()이 블록되어 워커 자체가 잠시 멈춘다).
    results_queue: mp.Queue[tuple] = mp.Queue(maxsize=worker_count * 2)
    file_queue: mp.Queue[str | None] = mp.Queue()
    for plan in to_process:
        file_queue.put(plan.path)
    for _ in range(worker_count):
        file_queue.put(None)

    workers = [
        mp.Process(target=_worker_main, args=(file_queue, results_queue, config), daemon=True)
        for _ in range(worker_count)
    ]
    for w in workers:
        w.start()

    storage = MailStorageEngine(db_path, config)
    error_log = ErrorLogger(errors_path)

    remaining = {plan.path for plan in to_process}
    written_per_file: dict[str, int] = {p: 0 for p in remaining}
    batch_count_per_file: dict[str, int] = {p: 0 for p in remaining}

    try:
        while remaining:
            if stop_flag is not None and stop_flag():
                summary.interrupted = True
                break
            try:
                kind, path, payload = results_queue.get(timeout=0.5)
            except queue.Empty:
                if not any(w.is_alive() for w in workers) and remaining:
                    # 모든 워커가 죽었는데 남은 파일이 있다 — 비정상
                    # 종료. 남은 파일은 상태를 건드리지 않고 둬서(대부분
                    # PARSING) 다음 실행이 처음부터 다시 시도하게 한다.
                    logger.error("orchestrator: 모든 워커가 종료됐지만 %d개 파일이 남았습니다", len(remaining))
                    break
                continue

            if kind == "resolved":
                if on_event:
                    on_event("resolved", path, payload)
                continue

            if kind == "batch":
                batch: list[MailRecord] = payload
                storage.upsert_batch(batch)
                written_per_file[path] = written_per_file.get(path, 0) + len(batch)
                batch_count_per_file[path] = batch_count_per_file.get(path, 0) + 1
                state.set(
                    path,
                    FileState(
                        status="WRITING",
                        last_batch_index=batch_count_per_file[path],
                        mails_written=written_per_file[path],
                        size=next(p.size for p in to_process if p.path == path),
                        mtime=next(p.mtime for p in to_process if p.path == path),
                    ),
                )
                state.save()
                if on_event:
                    on_event("batch", path, {"count": len(batch), "total": written_per_file[path]})
                continue

            if kind == "errors":
                for err in payload:
                    error_log.log(err)
                if on_event:
                    on_event("errors", path, payload)
                continue

            if kind == "done":
                plan = next(p for p in to_process if p.path == path)
                state.set(
                    path,
                    FileState(
                        status="COMPLETED",
                        last_batch_index=batch_count_per_file.get(path, 0),
                        mails_written=written_per_file.get(path, 0),
                        size=plan.size,
                        mtime=plan.mtime,
                    ),
                )
                state.save()
                summary.files_completed += 1
                summary.mails_written += written_per_file.get(path, 0)
                remaining.discard(path)
                if on_event:
                    on_event("done", path, written_per_file.get(path, 0))
                continue

            if kind == "failed":
                failed_plan = next((p for p in to_process if p.path == path), None)
                state.set(
                    path,
                    FileState(
                        status="FAILED",
                        error=str(payload),
                        size=failed_plan.size if failed_plan else 0,
                        mtime=failed_plan.mtime if failed_plan else 0.0,
                    ),
                )
                state.save()
                summary.files_failed += 1
                summary.failures[path] = str(payload)
                remaining.discard(path)
                if on_event:
                    on_event("failed", path, payload)
                continue
    except KeyboardInterrupt:
        summary.interrupted = True
        logger.info("orchestrator: Ctrl+C 감지, 현재까지 커밋된 배치를 보존하고 종료합니다")
    finally:
        for w in workers:
            if w.is_alive():
                w.terminate()
        for w in workers:
            w.join(timeout=5)
        error_log.close()
        storage.close()

    return summary
