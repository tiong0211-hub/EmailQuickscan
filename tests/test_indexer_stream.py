"""indexer.py: 제너레이터 스트리밍(배치 크기 준수), 개별 메일 실패 시
스킵하고 계속 진행하는지 검증한다.
"""

from pst_engine.indexer import PSTIndexer
from tests.conftest import FakeParser


def _open_fake(tmp_path, content: str) -> FakeParser:
    p = tmp_path / "sample.pst"
    p.write_text(content)
    parser = FakeParser(str(p))
    parser.open()
    return parser


def test_batches_respect_configured_batch_size(tmp_path, config):
    config.batch_size = 4
    parser = _open_fake(tmp_path, "FAKE:10")
    indexer = PSTIndexer(config)

    batches = list(indexer.iter_batches(parser, str(tmp_path / "sample.pst")))
    sizes = [len(b) for b, _errors in batches]
    assert sizes == [4, 4, 2]  # 10건을 4개씩 스트리밍, 마지막은 남은 만큼


def test_iter_batches_is_a_generator_not_a_list():
    """규칙 3: 메일 전체를 리스트에 담아 반환하지 않는다 — iter_batches
    자체가 제너레이터 함수여야 한다(전체를 모았다가 한 번에 돌려주는
    일반 함수가 아니라).
    """
    import inspect

    assert inspect.isgeneratorfunction(PSTIndexer.iter_batches)


def test_corrupted_single_message_is_skipped_and_reported(tmp_path, config):
    config.batch_size = 100
    parser = _open_fake(tmp_path, "FAKE:5,BADMSG")  # index 1이 고장난 메시지
    indexer = PSTIndexer(config)

    all_records = []
    all_errors = []
    for batch, errors in indexer.iter_batches(parser, str(tmp_path / "sample.pst")):
        all_records.extend(batch)
        all_errors.extend(errors)

    # 5통 중 1통만 실패 -> 정상 레코드 4개 + 오류 1개
    assert len(all_records) == 4
    assert len(all_errors) == 1
    assert all_errors[0]["error_type"] == "AttributeError"


def test_normalized_record_has_korean_searchable_subject(tmp_path, config):
    parser = _open_fake(tmp_path, "FAKE:1")
    indexer = PSTIndexer(config)
    batch, _errors = next(indexer.iter_batches(parser, str(tmp_path / "sample.pst")))
    assert "계약서" in batch[0].subject


def test_message_id_generated_when_missing(tmp_path, config):
    parser = _open_fake(tmp_path, "FAKE:1")
    indexer = PSTIndexer(config)
    batch, _errors = next(indexer.iter_batches(parser, str(tmp_path / "sample.pst")))
    # FakeParser는 Message-ID 헤더를 주지 않으므로 해시 기반 32자 hex여야 한다.
    mid = batch[0].message_id
    assert len(mid) == 32
    int(mid, 16)  # hex 파싱 가능해야 함


def test_locator_json_round_trips(tmp_path, config):
    parser = _open_fake(tmp_path, "FAKE:1")
    indexer = PSTIndexer(config)
    batch, _errors = next(indexer.iter_batches(parser, str(tmp_path / "sample.pst")))
    import json

    locator = json.loads(batch[0].locator_json)
    assert locator["parser"] == "fake"
