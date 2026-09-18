# PST Search Engine — 프로젝트 컨텍스트

## 목적

수십 GB 규모의 다중 PST 파일을 메모리 초과 없이 인덱싱하여, SQLite FTS5 기반 ms 단위 한글 검색을 제공하는 CLI/GUI 엔진을 구축한다. Outlook 설치 여부와 무관한 독립형 Python 환경에서 동작해야 하며, 최종 배포는 사내망 Windows PC에서 추가 패키지 설치 없이 실행되는 단일 실행 파일이다.

---

## 절대 규칙

1. LLM / 외부 Cloud API 호출 금지. 순수 Python + 표준 SQLite만 사용한다.
2. `# TODO: 구현하세요` 형태의 미완성 스텁 금지. 모든 메서드는 실제 동작하는 로직을 포함한다.
3. 메일 전체를 리스트에 담아 반환 금지. 반드시 generator / `yield` 스트리밍으로 처리한다.
4. **FTS5 인덱스는 Python이 생성한 2-gram 토큰 스트림을 `unicode61`로 색인한다(`detail=column`). 원문에 단어 토크나이저(`trigram` 포함)를 직접 적용하는 것은 금지한다 — 한글 검색 실패의 직접 원인이기 때문이다.**
   - 변경 이력: 최초안은 FTS5 `trigram` 토크나이저를 기본으로 지정했었다. 실측 결과 `trigram`은 2글자 쿼리(`"계약"`, `"AI"`)를 전혀 매치하지 못했고(0건), Python이 생성한 bigram 토큰 스트림은 동일 데이터셋에서 trigram과 결과가 완전히 일치하면서(누락 0·오차 0, 2000건 비교) 색인 용량이 더 작고 검색도 더 빨랐다. 이에 따라 규칙을 갱신했다. 측정값은 `README.md`의 "설계 근거" 절 참조.
5. 예외는 파이프라인을 중단시키지 않는다. 스킵 + 구조화 로그 기록 후 계속 진행한다.
6. 모듈 역할 경계를 침범하지 않는다 (아래 "역할 경계" 참조).
7. 복잡한 메서드에는 상세한 한글 주석을 단다.
8. 런타임에 자동으로 `pip install`을 실행하지 않는다. 설치 안내 메시지만 출력한다. 가속 파서(`pypff`, `extract_msg` 등)와 선택 라이브러리(`PyYAML`, `chardet`, `rich`)는 빌드 시점에 실행 파일에 번들하며, 런타임에는 있으면 쓰고 없으면 폴백한다 — 사내망 배포 환경은 pip 설치 자체가 불가능하기 때문이다.

---

## 역할 경계

| 모듈 | 하는 일 | 하지 않는 일 |
|---|---|---|
| `cli.py` | 명령 파싱, 위임 | 비즈니스 로직 직접 포함 ❌ |
| `gui.py` | tkinter 화면, orchestrator/search 호출 | 파싱/SQL 직접 수행 ❌ |
| `orchestrator.py` | 파일 큐, 워커 풀, Single Writer 루프 | 파싱/SQL 직접 수행 ❌ |
| `resolver.py` | 사용 가능 파서 탐지 및 선택 | 필드 추출 ❌, 자동 설치 ❌ |
| `indexer.py` | PST 순회, 필드 추출, 배치 yield | DB 커밋 ❌, 로그 파일 쓰기 ❌ |
| `storage.py` | 스키마 생성, Upsert, CSV/EML export | PST 파일 접근 ❌, 렌더링 ❌ |
| `search.py` | 쿼리 빌드, MATCH 실행, 결과 렌더링용 데이터 준비 | 쓰기 연결 ❌, 스키마 변경 ❌ |
| `state.py` | `indexing_log.json` 원자적 읽기/쓰기 | 워커 프로세스에서 호출 ❌ |
| `config.py` | `default.yaml` 로드 | 비즈니스 로직 ❌ |
| `optional_deps.py` | 선택 패키지 import 중계 | 폴백 로직 자체를 포함 ❌ (호출부가 결정) |

핵심 원칙: **쓰기 경로는 Single Writer 하나로 수렴한다.** SQLite는 단일 writer만 허용하므로, 파싱은 병렬로 수행하되 DB 쓰기와 상태 로그 갱신은 메인 프로세스가 큐를 통해 직렬 처리한다.

---

## 기술 규약

- Python 3.10+, type hint 필수, `dataclass` 사용
- SQLite: WAL 모드, `PRAGMA synchronous=NORMAL`
- 배치 커밋 단위: 기본 1000건 (`config/default.yaml`로 조정)
- 워커 수: 기본 `min(4, cpu_count)` — **파일 단위** 병렬 (파일 내부 병렬화 금지)
- 재시도: 지수 백오프 최대 3회 (0.5s → 1s → 2s)
- 로깅: 표준 `logging` 모듈 사용, 에러는 JSON Lines 포맷으로 `data/errors.jsonl`에 기록
- 검색 연결은 반드시 읽기 전용(`file:...?mode=ro`)으로 연다
- Windows `spawn` 멀티프로세싱을 전제로 워커 함수는 모듈 최상위에 두고, 진입점은 `multiprocessing.freeze_support()`를 호출한다

---

## 인코딩 폴백 순서

```
utf-8 → cp949 → euc-kr → latin-1(무손실)
```

전부 실패해도 **해당 메일을 스킵하지 않는다.** 원본 바이트를 `raw_body_b64`에 보존하고 `decode_status`를 기록한다.

- `decode_status` ∈ `{ok, fallback, failed}`
- `safe_decode()`는 예외를 raise 하지 않고 항상 `(text, status)` 튜플을 반환한다
- `chardet`가 번들되어 있으면 신뢰도가 높은 추정 인코딩을 폴백 체인 **맨 앞에 한 번** 끼워 넣을 뿐, 체인 자체를 대체하지 않는다

---

## Message-ID 생성 규칙

헤더에 `Message-ID`가 있으면 그대로 사용한다. 없으면:

```
sha256(subject + "|" + iso_date + "|" + sender).hexdigest()[:32]
```

이 값이 `mails` 테이블의 PRIMARY KEY이며, Upsert 멱등성의 기준이 된다.

---

## DB 스키마 (확정)

**`mails`** (일반 테이블)

| 컬럼 | 타입 | 비고 |
|---|---|---|
| `message_id` | TEXT | PRIMARY KEY |
| `file_path` | TEXT | 원본 PST 경로 |
| `folder_path` | TEXT | PST 내 원본 폴더 |
| `from_addr` | TEXT | |
| `to_addr` | TEXT | 복수 수신자는 `; ` 구분 |
| `cc_addr` | TEXT | 복수는 `; ` 구분 |
| `date_utc` | INTEGER | epoch seconds |
| `subject` | TEXT | |
| `snippet` | TEXT | 본문 초반 300자 |
| `has_attachment` | INTEGER | 0/1 |
| `attachment_names` | TEXT | `; ` 구분, 이름만 (본체는 저장하지 않음) |
| `size_bytes` | INTEGER | |
| `body_z` | BLOB | zlib 압축된 본문 원문(인용문 포함) |
| `raw_body_b64` | TEXT | 디코딩 실패 시 원본 보존 |
| `decode_status` | TEXT | ok / fallback / failed |
| `indexed_at` | INTEGER | epoch seconds |

**rowid**: 자동 증가가 아니라 `(date_utc << 20) | (sha256(message_id)[:3] & 0xFFFFF)`로 명시 계산한다 — 전역 시간순이라 `ORDER BY rowid`가 곧 최신순 정렬이 된다(회귀 방지: 이 설계가 없으면 날짜순 정렬에 별도 정렬 비용이 든다).

**`mails_fts`** (FTS5)

- 대상 컬럼: `bi_subject`, `bi_body`, `bi_from`, `bi_to`, `bi_cc`, `bi_att` — 각각 원본 텍스트의 Python 생성 2-gram 토큰 스트림
- `tokenize = "unicode61 remove_diacritics 0"`, `detail = column`
- 외부 콘텐츠 방식이 아니다(토큰 스트림 자체가 저장 데이터이므로 `content=` 미지정). `storage.upsert_batch()`가 rowid를 맞춰 수동 동기화한다.

**보조 인덱스**

- `idx_date` on `mails(date_utc)`
- `idx_from` on `mails(from_addr)`
- `idx_folder` on `mails(folder_path)`

---

## 파서 선택 로직 (규칙 기반, 결정론적)

```
1. import pypff 성공?          → PypffParser
2. shutil.which("readpst")?    → ReadpstParser (subprocess + 임시 EML 파싱)
3. 확장자가 .msg 인가?          → ExtractMsgParser
4. 전부 실패                   → 파일 상태 FAILED, 사유 로그, 다음 파일로 진행
```

LLM 판단이나 사용자 확인 없이 런타임에 자동 결정한다. `resolver.py`는 실행 파일과 같은 디렉터리에 번들된 `pypff.pyd` / `readpst.exe`도 탐지 대상에 포함한다.

---

## 태스크 상태 머신

```
DISCOVERED ─┬─ (로그상 COMPLETED) ─→ SKIPPED
            └─ PARSING ─┬─ (파서 확보 불가) ─→ FAILED
                        └─ WRITING ─┬─ (다음 배치) ─→ WRITING
                                    ├─ (쓰기 실패) ─→ RETRY ─┬─ (≤3회) ─→ WRITING
                                    │                        └─ (초과) ─→ FAILED
                                    └─ (파일 끝) ─→ COMPLETED
```

---

## 재시도 정책

| 실패 유형 | 처리 | 재시도 |
|---|---|---|
| 개별 메일 파싱 오류 | 스킵 + `errors.jsonl` 기록 | 없음 |
| 인코딩 실패 | `decode_status=failed`로 저장 | 없음 |
| DB lock / busy | 지수 백오프 0.5 → 1 → 2초 | 최대 3회 |
| 파서 확보 불가 | 파일 FAILED 처리 | 없음 |
| 프로세스 중단(Ctrl+C) | 현재 배치까지 커밋 후 상태 저장 | 재실행 시 이어서 |

**중단 복구**: `indexing_log.json`은 파일 단위 상태와 마지막 성공 배치 정보를 기록한다. 재실행 시 `COMPLETED`는 스킵하고, `WRITING` 상태로 중단된 파일은 처음부터 다시 순회하되 Upsert가 멱등이므로 중복이 생기지 않는다.

---

## 검색 알고리즘 (설계 근거는 README.md 참조)

1. 쿼리를 텀으로 분해하고 각 텀을 `bigram_tokens()`로 변환한다.
2. 2자 이상 텀은 bigram AND 결합 단일 `MATCH` 식으로 질의한다. 1자 텀은 bigram이 불가능하므로 저장 텍스트 `LIKE` 폴백을 쓴다.
3. `mails_fts`가 담는 것은 토큰의 **상위집합 일치**이므로 (구문 검색 불가), 상위 N건(기본 20)만 `mails.body_z`를 해제해 substring으로 재검증한다. 검증에서 탈락하면 다음 후보로 넘어간다.
4. 정렬 기본값은 `ORDER BY rowid DESC`(최신순). 관련도 정렬은 최신 N건(기본 5000)만 `bm25()`로 재정렬하는 근사치이며, 근사임을 출력에 표시한다.
5. 결과가 0건이면 각 조건을 단독으로 재질의해 어느 조건이 원인인지 표시한다.

---

## 디렉토리 구조

```
EmailQuickscan/
├── CLAUDE.md
├── README.md
├── pyproject.toml
├── requirements.txt
├── config/
│   └── default.yaml
├── src/
│   └── pst_engine/
│       ├── __init__.py
│       ├── cli.py
│       ├── gui.py
│       ├── orchestrator.py
│       ├── resolver.py
│       ├── indexer.py
│       ├── parsers/
│       │   ├── base.py
│       │   ├── pypff_parser.py
│       │   ├── readpst_parser.py
│       │   └── extractmsg_parser.py
│       ├── storage.py
│       ├── search.py
│       ├── state.py
│       ├── encoding.py
│       ├── config.py
│       ├── optional_deps.py
│       └── models.py
├── packaging/
│   ├── EmailQuickscan.spec
│   ├── build_windows.ps1
│   └── BUILD.md
├── data/
│   ├── mail_index.db      (git-ignored, 런타임 생성)
│   ├── indexing_log.json  (git-ignored, 런타임 생성)
│   └── errors.jsonl       (git-ignored, 런타임 생성)
└── tests/
    ├── test_encoding.py
    ├── test_config.py
    ├── test_optional_deps.py
    ├── test_state.py
    ├── test_storage_fts.py
    ├── test_search_query.py
    ├── test_quotes.py
    ├── test_indexer_stream.py
    ├── test_orchestrator_resume.py
    └── fixtures/
```

---

## 검증 기준 (완료 판정)

- [ ] 10GB PST 처리 시 RSS 메모리 1GB 미만 유지
- [ ] 한글 2글자 키워드("계약") 검색이 결과를 정상 반환
- [ ] 손상된 PST 1개가 섞여 있어도 나머지 파일 인덱싱이 완주
- [ ] 동일 명령 재실행 시 중복 삽입 0건 (증분 동작)
- [ ] 100만 건 기준 단일 검색 200ms 이내
- [ ] 인덱싱 진행 중에도 검색 명령이 정상 동작 (WAL 확인)
- [ ] `pypff` 미번들 환경에서도 폴백 파서로 인덱싱 성공
- [ ] Windows에서 추가 패키지 설치 없이 실행 파일 하나로 동작 (사용자 환경에서 검수)

---

## 설치 참고 (개발 환경)

- **Linux**: `libpff` 시스템 라이브러리 선행 설치 후 `pip install libpff-python`. `readpst`는 `pst-utils` 패키지.
- **Windows 최종 배포**: pip 설치가 불가능한 사내망을 전제로 하므로, 가속 파서는 빌드 머신에서 미리 컴파일·수집해 PyInstaller 실행 파일에 번들한다(`packaging/BUILD.md` 참조). `pypff`는 Windows용 PyPI 휠이 없어 빌드 머신에서 MSVC로 직접 컴파일해야 한다. 번들이 실패해도 `readpst`(있으면) 또는 `.msg` 경로로 자동 폴백하도록 설계돼 있다.
- 설치/번들 실패 시 프로그램은 중단되지 않고 안내 메시지를 출력한 뒤 사용 가능한 파서로 계속 진행한다.
