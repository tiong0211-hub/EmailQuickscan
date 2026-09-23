"""state.py: 원자적 저장, 중단 후 재개, 증분 SKIPPED 판정."""

import json

from pst_engine.state import FileState, StateManager


def test_atomic_save_and_reload(tmp_path):
    p = tmp_path / "indexing_log.json"
    sm = StateManager(p)
    sm.set("a.pst", FileState(status="WRITING", last_batch_index=3, mails_written=3000, size=1000, mtime=123.0))
    sm.save()

    sm2 = StateManager(p)
    st = sm2.get("a.pst")
    assert st.status == "WRITING"
    assert st.last_batch_index == 3
    assert st.mails_written == 3000


def test_no_tmp_file_left_behind(tmp_path):
    p = tmp_path / "indexing_log.json"
    sm = StateManager(p)
    sm.set("a.pst", FileState(status="COMPLETED"))
    sm.save()
    leftovers = list(tmp_path.glob("*.tmp*"))
    assert leftovers == []


def test_should_skip_only_when_completed_and_unchanged(tmp_path):
    p = tmp_path / "indexing_log.json"
    sm = StateManager(p)
    assert sm.should_skip("a.pst", 1000, 123.0) is False  # 기록 없음

    sm.set("a.pst", FileState(status="WRITING", size=1000, mtime=123.0))
    assert sm.should_skip("a.pst", 1000, 123.0) is False  # 아직 완료 아님

    sm.set("a.pst", FileState(status="COMPLETED", size=1000, mtime=123.0))
    assert sm.should_skip("a.pst", 1000, 123.0) is True
    assert sm.should_skip("a.pst", 2000, 123.0) is False  # 크기 변경
    assert sm.should_skip("a.pst", 1000, 999.0) is False  # mtime 변경


def test_resume_after_interruption_simulated(tmp_path):
    """WRITING 상태로 중단된 파일은 재실행 시 skip 대상이 아니어야 한다
    (COMPLETED만 skip 대상) — orchestrator가 처음부터 재순회하게 하는
    전제 조건.
    """
    p = tmp_path / "indexing_log.json"
    sm = StateManager(p)
    sm.set("a.pst", FileState(status="WRITING", last_batch_index=2, mails_written=2000, size=5000, mtime=1.0))
    sm.save()

    sm2 = StateManager(p)
    assert sm2.should_skip("a.pst", 5000, 1.0) is False


def test_corrupted_state_file_recovers_to_empty(tmp_path):
    p = tmp_path / "indexing_log.json"
    p.write_text("{not valid json", encoding="utf-8")
    sm = StateManager(p)  # 예외 없이 로드돼야 한다
    assert sm.all_states() == {}


def test_invalid_status_rejected():
    sm = StateManager("/tmp/unused_state_path.json")
    import pytest

    with pytest.raises(ValueError):
        sm.set("a.pst", FileState(status="NOT_A_REAL_STATUS"))


def test_saved_json_is_human_readable(tmp_path):
    p = tmp_path / "indexing_log.json"
    sm = StateManager(p)
    sm.set("a.pst", FileState(status="COMPLETED", size=10, mtime=1.0))
    sm.save()
    raw = json.loads(p.read_text(encoding="utf-8"))
    assert raw["a.pst"]["status"] == "COMPLETED"
