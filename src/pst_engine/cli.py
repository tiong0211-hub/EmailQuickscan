"""``argparse`` 진입점: ``index`` / ``search`` / ``show`` / ``attach`` /
``export`` / ``gui`` 서브커맨드.

비즈니스 로직을 직접 포함하지 않는다(역할 경계) — 전부
orchestrator/search/storage/resolver에 위임한다. 표 출력은 ``rich``가
있으면 Rich 표를, 없으면 내장 고정폭 렌더러를 쓴다(``optional_deps``
경유, CLAUDE.md 규칙 8: 없다고 설치를 시도하지 않는다).
"""

from __future__ import annotations

import argparse
import logging
import multiprocessing as mp
import sys
from pathlib import Path

from . import __version__
from .config import Config, load_config
from .optional_deps import get_rich
from .orchestrator import plan_files, run_index
from .resolver import fetch_attachment
from .search import MailSearchEngine, SearchResult
from .storage import MailStorageEngine

logger = logging.getLogger(__name__)


def _configure_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


# ---------------------------------------------------------------------------
# argparse 정의
# ---------------------------------------------------------------------------


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="pst-search", description="사내망 Windows PST 메일 검색 엔진")
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    p.add_argument("--config", default=None, help="설정 yaml 경로 (기본: config/default.yaml)")
    p.add_argument("--db", default=None, help="DB 경로 (기본: 설정의 paths.db)")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="command", required=True)

    idx = sub.add_parser("index", help="PST/MSG 인덱싱")
    idx.add_argument("paths", nargs="+", help="PST/MSG 파일, 디렉터리, 글로브 (여러 개 가능)")
    idx.add_argument("--recursive", action="store_true", help="디렉터리를 재귀적으로 탐색")
    idx.add_argument("--dry-run", action="store_true", help="탐색 결과와 선택된 파서만 출력하고 종료")
    idx.add_argument("--workers", type=int, default=None, help="병렬 워커 수 (기본: 설정값)")
    idx.add_argument("--batch-size", type=int, default=None, help="배치 커밋 크기 (기본: 설정값)")

    se = sub.add_parser("search", help="메일 검색")
    se.add_argument("query", nargs="?", default="", help='검색어 (예: "계약서 from:김 hasattachment:yes")')
    se.add_argument("--from", dest="f_from", default=None)
    se.add_argument("--to", dest="f_to", default=None)
    se.add_argument("--subject", dest="f_subject", default=None)
    se.add_argument("--has-attachment", action="store_true")
    se.add_argument("--after", dest="f_after", default=None)
    se.add_argument("--before", dest="f_before", default=None)
    se.add_argument("--folder", dest="f_folder", default=None)
    se.add_argument("--limit", type=int, default=20)
    se.add_argument("--sort", choices=["date", "relevance"], default="date")
    se.add_argument("--whole-word", action="store_true")
    se.add_argument("--json", action="store_true")
    se.add_argument("--csv", dest="csv_out", default=None, help="결과를 CSV로도 저장")

    sh = sub.add_parser("show", help="메일 본문 전문 보기")
    sh.add_argument("message_id")

    at = sub.add_parser("attach", help="첨부파일을 원본 PST/MSG에서 온디맨드로 추출")
    at.add_argument("message_id")
    at.add_argument("--file", dest="att_index", type=int, default=0, help="첨부 인덱스(0부터, --help 겸 show로 확인)")
    at.add_argument("-o", "--out", default=".", help="저장할 디렉터리")

    ex = sub.add_parser("export", help="검색 결과를 CSV/EML로 내보내기")
    ex.add_argument("query", nargs="?", default="")
    ex.add_argument("--format", choices=["csv", "eml"], default="csv")
    ex.add_argument("--out", required=True, help="CSV면 파일 경로, EML이면 디렉터리")
    ex.add_argument("--all", action="store_true", help="쿼리 없이 전량 내보내기")

    sub.add_parser("gui", help="tkinter 화면 실행")

    return p


def _compose_query(args: argparse.Namespace) -> str:
    """``--from``/``--to`` 등 플래그를 자유 쿼리 문자열에 합친다.

    자유 텍스트 쿼리와 플래그는 동일한 내부 쿼리 모델로 파싱되므로
    (search.py), 여기서는 문자열을 이어붙이기만 하면 된다.
    """
    parts = [args.query] if getattr(args, "query", "") else []
    mapping = [
        ("f_from", "from"), ("f_to", "to"), ("f_subject", "subject"),
        ("f_after", "after"), ("f_before", "before"), ("f_folder", "folder"),
    ]
    for attr, field in mapping:
        val = getattr(args, attr, None)
        if val:
            quoted = f'"{val}"' if " " in val else val
            parts.append(f"{field}:{quoted}")
    if getattr(args, "has_attachment", False):
        parts.append("hasattachment:yes")
    return " ".join(p for p in parts if p)


def _resolve_paths(args: argparse.Namespace, config: Config) -> tuple[str, str, str]:
    db_path = args.db or config.db_path
    return db_path, config.state_path, config.errors_path


# ---------------------------------------------------------------------------
# 서브커맨드 구현
# ---------------------------------------------------------------------------


def cmd_index(args: argparse.Namespace, config: Config) -> int:
    if args.workers:
        config.workers = args.workers
    if args.batch_size:
        config.batch_size = args.batch_size

    db_path, state_path, errors_path = _resolve_paths(args, config)

    if args.dry_run:
        from .state import StateManager

        state = StateManager(state_path)
        plans = plan_files(args.paths, config, recursive=args.recursive, state=state)
        if not plans:
            print("탐색된 파일이 없습니다.")
            return 0
        rows = []
        for p in plans:
            if p.skip_reason:
                status = f"SKIP ({p.skip_reason})"
            elif p.resolved is None:
                status = "FAILED (파서 없음)"
            else:
                status = f"{p.resolved.name} — {p.resolved.reason}"
            rows.append({"경로": p.path, "크기": _fmt_bytes(p.size), "선택": status})
        _print_table(rows, ["경로", "크기", "선택"])
        return 0

    def on_event(kind: str, path: str, payload: object) -> None:
        name = Path(path).name
        if kind == "resolved" and isinstance(payload, dict):
            print(f"[{name}] 파서 선택: {payload['name']} ({payload['reason']})")
        elif kind == "batch" and isinstance(payload, dict):
            print(f"[{name}] +{payload['count']}통 (누적 {payload['total']})")
        elif kind == "done":
            print(f"[{name}] 완료: {payload}통")
        elif kind == "failed":
            print(f"[{name}] 실패: {payload}")
        elif kind == "skipped":
            print(f"[{name}] 건너뜀: {payload}")
        elif kind == "errors" and isinstance(payload, list):
            print(f"[{name}] 개별 메일 오류 {len(payload)}건 (data/errors.jsonl에 기록)")

    summary = run_index(
        args.paths, config, db_path, state_path, errors_path,
        recursive=args.recursive, on_event=on_event,
    )

    print()
    print(
        f"완료 {summary.files_completed} / 실패 {summary.files_failed} / "
        f"건너뜀 {summary.files_skipped} / 메일 {summary.mails_written}통"
        + (" (중단됨)" if summary.interrupted else "")
    )
    for path, reason in summary.failures.items():
        print(f"  실패: {path}\n    사유: {reason}")
    return 1 if summary.files_failed and not summary.files_completed else 0


def cmd_search(args: argparse.Namespace, config: Config) -> int:
    db_path, _, _ = _resolve_paths(args, config)
    query = _compose_query(args)
    with MailSearchEngine(db_path, config) as engine:
        result = engine.search(query, limit=args.limit, sort=args.sort, whole_word=args.whole_word)

    if args.json:
        import json as _json

        print(_json.dumps(_result_to_json(result), ensure_ascii=False, indent=2))
    else:
        _print_search_result(result)

    if args.csv_out:
        rows = [_hit_to_row(h) for h in result.hits]
        _write_simple_csv(args.csv_out, rows, ["message_id", "date", "from", "to", "subject", "attachments"])
        print(f"\nCSV 저장: {args.csv_out} ({len(rows)}행)")

    return 0


def cmd_show(args: argparse.Namespace, config: Config) -> int:
    db_path, _, _ = _resolve_paths(args, config)
    with MailSearchEngine(db_path, config) as engine:
        full = engine.get_full(args.message_id)
    if full is None:
        print(f"메일을 찾지 못했습니다: {args.message_id}")
        return 1

    import datetime as _dt

    dt = _dt.datetime.fromtimestamp(full["date_utc"], tz=_dt.timezone.utc)
    print(f"제목: {full['subject']}")
    print(f"보낸사람: {full['from_addr']}")
    print(f"받는사람: {full['to_addr']}")
    if full["cc_addr"]:
        print(f"참조: {full['cc_addr']}")
    print(f"날짜: {dt.isoformat()}")
    print(f"폴더: {full['folder_path']}")
    print(f"원본: {full['file_path']}")
    if full["decode_status"] != "ok":
        print(f"[주의] 인코딩 상태: {full['decode_status']}")
    print()
    print(full["body_text"])
    if full["attachment_names"]:
        print()
        print("첨부:")
        for i, name in enumerate(full["attachment_names"]):
            print(f"  [{i}] {name}")
        print('  (attach 명령으로 꺼내려면: pst-search attach <message_id> --file <번호>)')
    return 0


def cmd_attach(args: argparse.Namespace, config: Config) -> int:
    db_path, _, _ = _resolve_paths(args, config)
    with MailSearchEngine(db_path, config) as engine:
        full = engine.get_full(args.message_id)
    if full is None:
        print(f"메일을 찾지 못했습니다: {args.message_id}")
        return 1
    if not full["locator_json"]:
        print("이 메일에는 첨부 위치 정보가 없습니다(오래된 인덱스이거나 첨부가 없습니다).")
        return 1

    result = fetch_attachment(full["file_path"], full["locator_json"], args.att_index, config)
    if result is None:
        print(f"첨부 index={args.att_index}를 원본에서 찾지 못했습니다. 원본 파일이 이동/삭제되지 않았는지 확인하세요.")
        return 1

    name, data = result
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / name
    out_path.write_bytes(data)
    print(f"저장됨: {out_path} ({_fmt_bytes(len(data))})")
    return 0


def cmd_export(args: argparse.Namespace, config: Config) -> int:
    db_path, _, _ = _resolve_paths(args, config)
    query = "" if args.all else args.query

    with MailSearchEngine(db_path, config) as engine:
        cur = engine.conn.cursor()
        if query:
            result = engine.search(query, limit=1_000_000, sort="date")
            rowids = [h.rowid for h in result.hits]
            if not rowids:
                print("내보낼 결과가 없습니다.")
                return 0
            placeholders = ",".join("?" for _ in rowids)
            cur.execute(f"SELECT * FROM mails WHERE rowid IN ({placeholders}) ORDER BY rowid DESC", rowids)
        else:
            cur.execute("SELECT * FROM mails ORDER BY rowid DESC")
        rows = cur.fetchall()

    # export만을 위해 새 쓰기 연결을 열지 않는다 — MailStorageEngine의
    # export 메서드는 순수하게 파일 쓰기만 하므로, 이미 있는 DB를 다시
    # 쓰기 모드로 여는 대신 읽기 전용 연결에서 얻은 행을 그대로 넘긴다.
    # (export_csv/export_eml은 self.config만 쓰고 self.conn은 쓰지 않는다.)
    shim_config = config
    exporter = object.__new__(MailStorageEngine)
    exporter.config = shim_config  # type: ignore[attr-defined]

    if args.format == "csv":
        columns = [
            "message_id", "file_path", "folder_path", "from_addr", "to_addr", "cc_addr",
            "date_utc", "subject", "snippet", "has_attachment", "attachment_names", "size_bytes",
        ]
        count = MailStorageEngine.export_csv(exporter, rows, args.out, columns)
        print(f"CSV 저장: {args.out} ({count}행)")
    else:
        count = MailStorageEngine.export_eml(exporter, rows, args.out)
        print(f"EML 저장: {args.out} ({count}개, 본문만 — 첨부 제외)")
    return 0


def cmd_gui(args: argparse.Namespace, config: Config) -> int:
    from .gui import run_gui

    db_path, state_path, errors_path = _resolve_paths(args, config)
    run_gui(config, db_path, state_path, errors_path)
    return 0


# ---------------------------------------------------------------------------
# 출력 헬퍼 (rich 있으면 사용, 없으면 고정폭 폴백)
# ---------------------------------------------------------------------------


def _print_table(rows: list[dict], columns: list[str]) -> None:
    rich = get_rich()
    if rich is not None:
        from rich.console import Console
        from rich.table import Table

        table = Table(show_lines=False)
        for c in columns:
            table.add_column(c)
        for row in rows:
            table.add_row(*[str(row.get(c, "")) for c in columns])
        Console().print(table)
        return

    widths = [max(len(c), *(len(str(r.get(c, ""))) for r in rows)) if rows else len(c) for c in columns]
    print("  ".join(c.ljust(w) for c, w in zip(columns, widths, strict=True)))
    print("  ".join("-" * w for w in widths))
    for row in rows:
        print("  ".join(str(row.get(c, "")).ljust(w) for c, w in zip(columns, widths, strict=True)))


def _print_search_result(result: SearchResult) -> None:
    if result.warnings:
        for w in result.warnings:
            print(f"[안내] {w}")

    if not result.hits:
        print(f"결과 없음 ({result.elapsed_ms:.1f}ms)")
        if result.diagnosis:
            print("조건별 단독 검색 결과:")
            for d in result.diagnosis:
                mark = " <- 이 조건 때문일 수 있습니다" if d.is_culprit else ""
                print(f"  {d.label}: {d.count_display}{mark}")
        return

    rows = [_hit_to_row(h) for h in result.hits]
    _print_table(rows, ["message_id", "date", "from", "subject", "attachments"])
    more = " (더 있음)" if result.more_available else ""
    sort_note = " [관련도순 근사]" if result.used_relevance_sort else ""
    print(f"\n{len(result.hits)}건 표시{more} · {result.elapsed_ms:.1f}ms{sort_note}")


def _hit_to_row(h) -> dict:
    import datetime as _dt

    dt = _dt.datetime.fromtimestamp(h.date_utc, tz=_dt.timezone.utc)
    return {
        "message_id": h.message_id,
        "date": dt.strftime("%Y-%m-%d"),
        "from": h.from_addr,
        "to": h.to_addr,
        "subject": h.subject,
        "attachments": "; ".join(h.attachment_names),
    }


def _result_to_json(result: SearchResult) -> dict:
    return {
        "hits": [_hit_to_row(h) for h in result.hits],
        "more_available": result.more_available,
        "elapsed_ms": result.elapsed_ms,
        "warnings": result.warnings,
        "used_relevance_sort": result.used_relevance_sort,
        "diagnosis": (
            [{"label": d.label, "count": d.count_display, "is_culprit": d.is_culprit} for d in result.diagnosis]
            if result.diagnosis
            else None
        ),
    }


def _write_simple_csv(path: str, rows: list[dict], columns: list[str]) -> None:
    import csv

    with open(path, "w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(columns)
        for row in rows:
            writer.writerow([row.get(c, "") for c in columns])


def _fmt_bytes(n: int) -> str:
    size = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024:
            return f"{size:.0f}{unit}" if unit == "B" else f"{size:.1f}{unit}"
        size /= 1024
    return f"{size:.1f}PB"


# ---------------------------------------------------------------------------
# 진입점
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    # Windows(spawn) onefile 배포에서 자식 프로세스가 프로그램을 처음부터
    # 다시 실행하며 무한 재귀에 빠지는 것을 막는다 — 반드시 다른 어떤
    # 코드보다도 먼저 호출돼야 한다.
    mp.freeze_support()

    parser = build_arg_parser()
    args = parser.parse_args(argv)
    _configure_logging(args.verbose)

    config = load_config(args.config)

    handlers = {
        "index": cmd_index,
        "search": cmd_search,
        "show": cmd_show,
        "attach": cmd_attach,
        "export": cmd_export,
        "gui": cmd_gui,
    }
    handler = handlers[args.command]
    try:
        return handler(args, config)
    except KeyboardInterrupt:
        print("\n중단되었습니다.")
        return 130


if __name__ == "__main__":
    sys.exit(main())
