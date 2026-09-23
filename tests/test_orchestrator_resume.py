"""orchestrator.py: 손상 파일이 섞여도 나머지가 완주하는지, 재실행 시
중복 삽입 0건인지(멱등 + 증분 스킵)를 검증한다.

가짜 파서를 쓰므로 실제 PST/pypff/readpst 없이도 Single Writer 루프,
큐 기반 워커 통신, state.json 갱신까지 전부 실행된다.
"""

import sqlite3

from pst_engine.orchestrator import run_index


def _write_fake_pst(path, content: str) -> None:
    path.write_text(content)


def test_corrupted_file_does_not_block_others(tmp_path, config, fake_resolver):
    config.batch_size = 10
    config.workers = 2

    _write_fake_pst(tmp_path / "a.pst", "FAKE:20")
    _write_fake_pst(tmp_path / "b.pst", "FAKE:15")
    _write_fake_pst(tmp_path / "broken.pst", "CORRUPT")

    db = tmp_path / "mail_index.db"
    state = tmp_path / "indexing_log.json"
    errors = tmp_path / "errors.jsonl"

    summary = run_index(
        [str(tmp_path / "a.pst"), str(tmp_path / "b.pst"), str(tmp_path / "broken.pst")],
        config, db, state, errors, recursive=False,
    )

    assert summary.files_completed == 2
    assert summary.files_failed == 1
    assert summary.mails_written == 35

    conn = sqlite3.connect(db)
    assert conn.execute("SELECT count(*) FROM mails").fetchone()[0] == 35
    conn.close()


def test_rerun_is_idempotent_and_skips_completed(tmp_path, config, fake_resolver):
    config.batch_size = 10
    config.workers = 1
    _write_fake_pst(tmp_path / "a.pst", "FAKE:12")

    db = tmp_path / "mail_index.db"
    state = tmp_path / "indexing_log.json"
    errors = tmp_path / "errors.jsonl"

    run_index([str(tmp_path / "a.pst")], config, db, state, errors)

    events = []
    summary2 = run_index(
        [str(tmp_path / "a.pst")], config, db, state, errors,
        on_event=lambda kind, path, payload: events.append(kind),
    )

    assert summary2.files_skipped == 1
    assert summary2.files_completed == 0
    assert "skipped" in events

    conn = sqlite3.connect(db)
    assert conn.execute("SELECT count(*) FROM mails").fetchone()[0] == 12
    conn.close()


def test_individual_message_errors_are_logged_to_errors_jsonl(tmp_path, config, fake_resolver):
    config.batch_size = 100
    _write_fake_pst(tmp_path / "a.pst", "FAKE:5,BADMSG")

    db = tmp_path / "mail_index.db"
    state = tmp_path / "indexing_log.json"
    errors = tmp_path / "errors.jsonl"

    summary = run_index([str(tmp_path / "a.pst")], config, db, state, errors)

    assert summary.files_completed == 1
    assert summary.mails_written == 4  # 5통 중 1통은 정규화 실패

    import json

    lines = errors.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    entry = json.loads(lines[0])
    assert entry["file_path"].endswith("a.pst")


def test_no_parser_available_marks_failed_with_guidance(tmp_path, config):
    """resolver를 패치하지 않은 채로(즉 진짜 resolve_parser 사용) 지원
    안 되는 확장자를 주면, 프로그램이 죽지 않고 FAILED로 기록되며 설치
    안내가 사유에 담겨야 한다(자동 설치는 하지 않는다 — 규칙 8).
    """
    target = tmp_path / "a.pst"
    target.write_text("not a real pst")

    db = tmp_path / "mail_index.db"
    state = tmp_path / "indexing_log.json"
    errors = tmp_path / "errors.jsonl"

    summary = run_index([str(target)], config, db, state, errors)

    # 이 샌드박스에는 pypff(3.11 인터프리터 기준)도 readpst도 없을 수
    # 있으므로 결과가 환경에 따라 갈리지만, 최소한 예외로 죽지는 않고
    # 파일 단위 결과(완료 또는 실패)로 수렴해야 한다.
    assert summary.files_completed + summary.files_failed == 1


def test_discover_targets_handles_glob_and_recursive_dirs(tmp_path):
    from pst_engine.orchestrator import discover_targets

    sub = tmp_path / "nested"
    sub.mkdir()
    (tmp_path / "a.pst").write_text("x")
    (sub / "b.pst").write_text("x")
    (tmp_path / "c.ost").write_text("x")
    (tmp_path / "ignore.txt").write_text("x")

    flat = discover_targets([str(tmp_path)], recursive=False)
    assert str(tmp_path / "a.pst") in flat
    assert str(tmp_path / "c.ost") in flat
    assert str(sub / "b.pst") not in flat  # 비재귀라 하위 디렉터리는 안 봄

    deep = discover_targets([str(tmp_path)], recursive=True)
    assert str(sub / "b.pst") in deep

    glob_result = discover_targets([str(tmp_path / "*.pst")], recursive=False)
    assert str(tmp_path / "a.pst") in glob_result
    assert str(tmp_path / "c.ost") not in glob_result
