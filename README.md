# EmailQuickscan

Outlook 없이, 사내망 Windows PC에서 추가 패키지 설치 없이 동작하는
PST/OST/MSG 메일 검색 엔진. 수십 GB 규모의 다중 PST를 메모리 상한
안에서 인덱싱하고, 한글·영문을 ms 단위로 검색하며, 찾은 메일의 본문과
첨부를 그 자리에서 연다.

이 문서는 최종 사용자 안내와 함께, **왜 이렇게 설계됐는지**를 실측
수치와 함께 남긴다 — 설계 결정 대부분이 이 저장소를 만드는 과정에서
직접 벤치마크한 결과를 근거로 하기 때문이다.

---

## 빠른 시작 (개발 환경)

```bash
pip install -r requirements.txt   # 선택 가속기. 없어도 동작한다.
python -m pytest tests/ -v        # 전체 테스트
python -m pst_engine.cli index ./sample_pst_dir --recursive
python -m pst_engine.cli search "계약서 from:김 hasattachment:yes"
python -m pst_engine.cli show <message_id>
python -m pst_engine.cli gui      # tkinter 화면
```

사내망 배포용 실행 파일은 별도 빌드 머신에서 만든다 —
`packaging/BUILD.md` 참조. **사용자 PC에는 아무것도 설치하지 않는다.**

---

## 명령

| 명령 | 설명 |
|---|---|
| `index <경로...> [--recursive] [--dry-run] [--workers N] [--batch-size N]` | PST/OST/MSG 인덱싱. 경로는 파일·디렉터리·글로브 다수 가능 |
| `search <쿼리> [--limit 20] [--sort date\|relevance] [--whole-word] [--json] [--csv <경로>]` | 검색. `--from`/`--to`/`--subject`/`--after`/`--before`/`--folder`/`--has-attachment` 플래그도 지원(자유 쿼리와 결합됨) |
| `show <message_id>` | 본문 전문 + 헤더 + 첨부 목록 |
| `attach <message_id> --file <번호> [-o 디렉터리]` | 첨부 본체를 원본 PST/MSG에서 온디맨드로 추출 |
| `export <쿼리> --format csv\|eml --out <경로> [--all]` | 검색 결과 내보내기. `eml`은 본문만(첨부 제외) |
| `gui` | tkinter 화면 실행 |

---

## 검색 문법

Outlook 즉시 검색(AQS)의 `from:`/`subject:`/`hasattachment:` 프리픽스
문법을 그대로 받는다. Gmail 문법과 한국어 별칭도 같은 파서가 흡수한다.

| Outlook 문법 | 별칭 | 비고 |
|---|---|---|
| `from:` `to:` `cc:` `subject:` | `보낸사람:` `받는사람:` `참조:` `제목:` | |
| `body:` | `content:` `본문:` | |
| `attachments:` | `filename:` `첨부:` | 첨부 **이름**만 검색됨(본체는 저장 안 함) |
| `hasattachment:yes\|no` | `has:attachment` | |
| `folder:` | `in:` `폴더:` | PST 내 폴더 경로 부분일치 |
| `received:` `after:` `before:` | `date:` `날짜:` `newer_than:` `older_than:` | 아래 날짜 형식 |
| `messagesize:>1MB` | `크기:>1MB` | `>`/`<`/`>=`/`<=` + B/KB/MB/GB |
| `file:` | | 원본 PST 경로 부분일치 |
| `AND` `OR` `NOT` / `-텀` / `"구절"` | | 기본은 AND. OR은 **인접한 단일 텀 두 개만** 묶는다(중첩 불리언 트리 미지원 — 의도적 범위) |

**날짜 값**: `2024-03-15`, `2024-03`, `2024`, 범위 `2024-01-01..2024-02-01`(끝
날짜 당일까지 포함), 상대 `7d`/`3m`/`1y`, `today`/`yesterday`/`thisweek`/
`lastweek`/`thismonth`/`thisyear` 및 `오늘`/`어제`/`이번주`/`지난주`/
`이번달`/`올해`.

**`--whole-word`**: 영문 텀에만 단어 경계를 적용한다(`con`이
`config`/`contract`를 오탐하지 않게). **한글 텀에는 적용하지 않는다** —
조사가 붙는 교착어라(`계약서를`) 단어 경계 검사를 하면 정상 결과까지
걸러지기 때문이다. 이 비대칭은 의도적 설계다.

**결과가 안 나올 때**: 접두사 없는 맨 텀은 제목·본문·From·To·Cc·첨부명
6개 컬럼 전체에서 찾는다. 0건이면 각 조건을 단독으로 다시 세어 어느
조건이 원인인지 보여준다(`search.py`의 진단 기능).

---

## 설계 근거

계획 단계에서 실측으로 초기 설계의 문제를 여러 번 찾아 구조를 바꿨다.
아래 수치는 전부 이 저장소를 만드는 동안 실제로 측정한 값이다(합성
데이터, 이 개발 환경: Python 3.11, SQLite 3.45.1).

### 1. FTS5 `trigram`은 2글자를 못 찾는다

`MATCH '계약'`(2자), `MATCH 'AI'`(2자 영문) 모두 **0건**. 3자 이상만
매치된다. `LIKE` 폴백은 정확하지만 느리다(희귀어 최악 234ms@300k행 →
100만 건 약 780ms 추정).

### 2. Python이 만든 bigram 토큰 스트림이 trigram을 완전히 대체한다

bigram(인접 2글자) 토큰을 `unicode61` 토크나이저로 색인하면 trigram과
**결과가 완전히 일치**한다(보고서·세금계산서·contract·proposal 각 2000건
비교, 누락 0·오차 0). 그러면서 색인이 더 작고 더 빠르다.

**→ `CLAUDE.md` 규칙 4를 갱신했다.** trigram을 쓰지 않고, `bigram_tokens()`
(storage.py)가 만든 토큰 스트림을 `unicode61`로 색인한다.

### 3. `INTERSECT`·정렬·bm25가 병목이었다 (50만 건 기준)

| 경로 | 초기 방식 | 수정 후 | 기법 |
|---|---|---|---|
| 2텀 AND | 501ms | **0.2ms** | `INTERSECT` 대신 단일 `MATCH 'a AND b'` 식 |
| 날짜정렬 top20 | 667ms | **0.4ms** | rowid를 시간순으로 미리 배치 → `ORDER BY rowid DESC` |
| 날짜범위 + 검색 | 761ms | **1.5ms** | 날짜 조건을 rowid 범위로 변환 |
| 혼합 2자+3자 AND | 394ms | **0.5ms** | 단일 bigram 인덱스로 통합(2번 항목과 같은 이유) |
| bm25 관련도 정렬 | 794ms | **38ms** | 최신 5000건(`relevance_candidates`)만 재정렬. 근사치임을 출력에 표시 |

`rowid = (date_utc << 20) | (sha256(message_id)[:3] & 0xFFFFF)`로 직접
계산해 배치한다(`storage.compute_rowid`) — 이 덕에 `ORDER BY rowid DESC`가
곧 최신순 정렬이라 별도 정렬 비용이 없다.

### 4. 색인 용량의 주범은 인용 답장 체인이었다

실무형 메일 본문 평균 912자 중 **603자(66%)가
`-----Original Message-----` 아래 재인용된 과거 내용**이었다(합성
벤치마크). 원본 메일이 이미 따로 색인돼 있으므로 중복이다.
`strip_quoted_reply()`(storage.py)가 색인 직전에만 제거하고, `body_z`
(압축 저장 원문)에는 그대로 남긴다 — `show` 명령은 항상 원문을 보여준다.

### 5. 본문 키워드 추출은 이득이 없었다

"본문에서 키워드만 골라 색인하면 더 빠르지 않을까"를 20만 건으로
직접 재봤다(정답: 본문에 "세금계산서" 포함 146,045건).

| 색인 범위 | 100만 통 색인 용량 | `계약` 검색 | `contract` 검색 | 재현율 |
|---|---|---|---|---|
| 제목+From+To+첨부명만 | 0.31GB | 0.10ms | 0.18ms | 10% |
| + 본문 키워드 상위10개 | 0.31GB | 0.15ms | 1.26ms | 26% |
| **+ 본문 전체** | 2.09GB | 0.23ms | 2.17ms | **100%** |

속도 차이는 0.08~0.9ms(200ms 예산의 0.5% 수준)뿐이었다 — FTS5 지연은
색인 크기가 아니라 **매치 건수**에 좌우되기 때문에, 색인을 줄여도
검색은 빨라지지 않는다. 반면 재현율은 26%로 떨어진다. **→ 본문 전체를
색인한다**(인용문 제거 후). 키워드 선별 로직은 만들지 않았다.

### 6. `detail=none`은 필드 한정 검색을 깬다

| FTS5 구성 | 100만 건 색인 | `subject:` 필드 한정 | 맨텀 검색 |
|---|---|---|---|
| 단일 테이블 `detail=full` | 2.55GB | ✅ 0.07ms | 0.25ms |
| **단일 테이블 `detail=column`** | **2.09GB** | ✅ 0.11ms | **0.34ms** |
| meta(`full`)+body(`none`) 분리 | 1.89GB | ✅ 0.05ms | ❌ **91ms** |

`detail=column`을 택했다 — 용량은 `detail=full`보다 작고, `subject:`
필드 한정과 맨텀 검색을 모두 빠르게 지원한다.

### 7. 검증(verify) 단계가 없으면 오탐이 난다

`detail=column`은 구문(phrase) 검색을 지원하지 않아 여러 bigram 토큰을
AND로만 묶는다 — 이는 "상위집합" 매치일 뿐이라, 조사가 붙는 실제
한국어에서 **오탐 2.8%**가 측정됐다(150,103건 vs 정답 146,045건).

**→** `storage.verify_candidate()`가 후보 각각을 저장된 텍스트에서
부분 문자열로 재확인한다(`search.py`가 매 검색마다 호출). 비용은
0.005ms×20(기본 limit) 수준으로 무시할 만하다.

---

## 최종 구조 (100만 통 기준 추정)

| 구성요소 | 용량 | 역할 |
|---|---|---|
| bigram FTS5 색인 (제목·본문·From·To·Cc·첨부명) | 2.09GB | **찾기** — 0.1~2.2ms |
| 본문 zlib 압축 저장(`body_z`) | 0.64GB | **보기** — 전문 열람, 검증, 재색인 |
| 메타데이터 테이블 + 보조 인덱스 | ~0.2GB | 날짜·폴더·크기 |
| 첨부 본체 | **0GB** | 저장 안 함. `attach` 명령이 원본에서 온디맨드 추출 |
| **합계** | **≈2.9GB** | |

`body_z`를 압축 보관하므로 **색인 정책(어떤 필드를 색인할지, 인용문을
지울지)을 나중에 바꿔도 PST를 다시 읽지 않고 재색인**할 수 있다.

---

## 아키텍처

### 모듈 역할 경계

`CLAUDE.md`에 확정된 경계를 그대로 따른다 — 요약하면:

- **쓰기 경로는 단일 writer로 수렴한다.** 파싱은 파일 단위로
  병렬화하되(`orchestrator.py`가 워커 프로세스 풀 관리), DB 쓰기와
  `indexing_log.json` 갱신은 메인 프로세스 하나가 큐를 통해 직렬
  처리한다. 워커는 `storage.py`/`state.py`를 import하지 않는다.
- **파서는 정규화하지 않는다.** `parsers/*.py`는 `RawMessage`(원본
  그대로)만 만든다. 주소 분리, 날짜 epoch 변환, 인코딩 폴백은 전부
  `indexer.py`가 담당한다.
- **검색은 쓰기 연결을 열지 않는다.** `search.py`는 항상
  `file:...?mode=ro`로 연다. `storage.py`의 순수 함수(`bigram_tokens`,
  `verify_candidate` 등)는 import해서 재사용한다 — 그래야 색인 시점과
  검색 시점의 토큰화 로직이 항상 일치한다(하나라도 어긋나면 있는데도
  0건이 되는 조용한 버그가 생긴다).

### 스키마

```sql
CREATE TABLE mails (
  message_id        TEXT PRIMARY KEY,  -- 헤더 Message-ID, 없으면 해시
  file_path, folder_path, from_addr, to_addr, cc_addr,
  date_utc INTEGER, subject, snippet,
  has_attachment INTEGER, attachment_names, size_bytes,
  body_z BLOB,               -- zlib 압축 원문(인용문 포함)
  raw_body_b64, decode_status, indexed_at,
  locator_json TEXT          -- attach 명령이 첨부 본체를 다시 찾는 단서
);

CREATE VIRTUAL TABLE mails_fts USING fts5(
  bi_subject, bi_body, bi_from, bi_to, bi_cc, bi_att,
  tokenize = "unicode61 remove_diacritics 0",
  detail = column
);
```

`message_id`가 없으면
`sha256(subject + "|" + iso_date + "|" + sender).hexdigest()[:32]`로
생성한다(명세 그대로). 이 값이 Upsert 멱등성의 기준이다.

### 첨부 처리

첨부 **이름**만 색인·저장한다(`"계약서.pdf"`로 검색은 되지만 본체는
DB에 없다). `RawMessage.locator`(파서별 위치 정보: pypff는 폴더
인덱스+메시지 인덱스, readpst는 폴더/파일명, extract_msg는 msg 경로)를
JSON으로 `locator_json` 컬럼에 저장해 두고, `attach` 명령이
`resolver.fetch_attachment()`를 통해 원본을 다시 열어 그 자리에서
꺼낸다. 상시 저장 비용은 0이지만, readpst 경로는 PST 전체를 다시
풀어야 해서 느리다(README "설치 참고" 및 `packaging/BUILD.md` 참조) —
설계상 감수한 트레이드오프다.

### 파서 선택 (`resolver.py`)

```
1. import pypff 성공?          → PypffParser
2. shutil.which("readpst")?    → ReadpstParser
3. 확장자가 .msg 인가?          → ExtractMsgParser
4. 전부 실패                   → 파일 FAILED, 설치 안내만 출력(자동 설치 안 함)
```

파일을 실제로 열어보지 않고(비용이 크므로) "이 파일 유형에 이 파서를
쓸 수 있는가"만 저렴하게 판단한다. 실제로 여는 중 나는 오류(손상된
PST 등)는 `orchestrator.py`가 파일 단위로 잡아 FAILED 처리하고 다음
파일로 넘어간다.

`pypff`/`extract_msg` API는 이 개발 환경에서 실제로 설치해(apt의
`python3-pypff`, PyPI의 `extract-msg`) `dir()`로 확인하거나 소스를 직접
읽어(libpff-python 20231205 sdist, extract-msg 0.56.1 wheel) 검증했다 —
특히 pypff의 첨부 파일명은 고수준 API에 노출돼 있지 않아 MAPI 레코드
엔트리(`PidTagAttachLongFilename` 0x3707)를 직접 순회해야 한다는 것,
그리고 `readpst_parser.py`가 실제로는 `email.parser`를 import하지 않아
런타임에 죽는 잠재 버그가 있었다는 것도 이 과정에서 발견해 고쳤다
(직접 단위 테스트로 잡음 — `tests/test_readpst_helpers.py`).

---

## 의존성 정책

사내망은 pip 설치가 불가능하다는 전제로 설계했다(CLAUDE.md 규칙 8).

| 패키지 | 용도 | 없으면 |
|---|---|---|
| `libpff-python`(비-Windows) / `libpff-python-windows`(Windows) | PST 1순위 파서 | readpst → .msg 순으로 폴백 |
| `extract-msg` | `.msg` 파서 | 그 경로만 비활성 |
| `PyYAML` | `config/default.yaml` 읽기 | 표준 라이브러리 서브셋 파서로 폴백(`config.parse_yaml_subset`) |
| `chardet` | 인코딩 1차 추정 | 명세 고정 폴백 체인(`utf-8→cp949→euc-kr→latin-1`)만 사용 |
| `rich` | CLI 표 출력 | 내장 고정폭 렌더러 |

런타임 코드는 이 패키지들의 유무를 `optional_deps.py` 한 곳에서만
판단한다(`try/except ImportError`가 코드 전반에 흩어지지 않게). 배포용
실행 파일에는 빌드 머신에서 전부 번들한다(`packaging/BUILD.md`) —
런타임에 `pip install`을 시도하는 코드는 없다.

`libpff-python`(공식)은 PyPI에 **Windows 휠이 없다**(macOS 휠 + 소스
tarball뿐, 이 세션에서 직접 확인). 대신 같은 libpff를 Windows용으로
미리 컴파일해 올린 제3자 wheel `libpff-python-windows`를 쓴다 — 실제로
다운로드해 내부를 열어 보니 외부 DLL 의존성 없는 단일 `.pyd`이고,
설치되는 모듈명도 `pypff`로 동일해 **컴파일도 코드 변경도 필요 없다**.
비공식 배포판을 쓰는 신뢰 판단 근거(버전 일치, 소스 대조 가능,
버전 고정)는 `packaging/BUILD.md`에 기록했다. 이 덕분에 Visual Studio
Build Tools 없이 Python + pip만으로 빌드 머신을 준비할 수 있다.

---

## 검증 현황 (정직하게 남김)

### 이 세션에서 실제로 확인한 것

- `python -m pytest tests/ -v` — **97개 전부 통과**
- `ruff check src/ tests/` — 통과 (실질 버그를 잡는 규칙 위주 설정,
  근거는 `pyproject.toml` 주석 참조)
- `mypy src/pst_engine/` — 통과
- 가짜 파서로 orchestrator 전체 파이프라인 end-to-end 실행: 손상 파일
  1개가 섞여도 나머지 완주, 재실행 시 중복 0건, `errors.jsonl` 기록 확인
- 2자/3자/영문 2자 한글·영문 검색, 필드 한정, AND/OR/NOT, 구문 검색,
  날짜 전 형식, `--whole-word`, 0건 진단, CSV/EML export **실제 실행**
- tkinter GUI를 **Xvfb 가상 디스플레이에서 실제로 띄워** 검색·미리보기·
  하이라이트·필드 검색까지 기능 동작 확인(이 프로젝트의 원래 예상보다
  나은 결과 — 계획 당시엔 "헤더리스라 GUI 검증 불가"로 예상했었다)
- `pypff`(apt `python3-pypff`)와 `readpst`(apt `pst-utils`)를 실제
  설치해 API를 확인(단, 실제 PST 파일을 구하지 못해 "PST 원본 →
  RawMessage" 경로 자체는 end-to-end로 돌려보지 못함 — 아래 참조)
- Windows용 사전 빌드 wheel `libpff-python-windows`를 실제로 다운로드해
  내부를 열어 확인: `pypff.cp3xx-win_amd64.pyd` 단일 파일, 외부 DLL
  의존성 없음, 모듈명이 `libpff-python`(비-Windows)과 동일한 `pypff` —
  MSVC 컴파일 없이 빌드 머신에서 `pip install`만으로 설치됨을 근거와
  함께 확인(실제 Windows 실행 자체는 미검증, 아래 참조)

### 이 세션에서 확인하지 못한 것

- **실제 PST 파일로 인덱싱.** 공개 샘플 PST를 구하지 못해 `pypff_parser.py`/
  `readpst_parser.py`는 실제 라이브러리 API를 소스 레벨에서 검증했을
  뿐, "진짜 PST → RawMessage" 경로 자체는 미검증이다.
- **Windows 환경 전반.** `spawn` 멀티프로세싱 실동작, `libpff-python-windows`
  wheel이 실제 Windows에서 `pip install`로 깔끔히 설치되는지,
  PyInstaller onefile exe 생성·실행, GUI의 실제 Windows 렌더링.
  이 넷은 `packaging/BUILD.md`의 체크리스트로 남겨뒀다 — Windows 빌드
  머신에서 사람이 확인해야 한다.
- **10GB급 실제 PST에서의 메모리 상한(1GB) 검증.** 스트리밍 설계(배치
  yield, 큐 백프레셔 `maxsize=workers*2`)는 코드 리뷰로는 타당하지만,
  대용량 실측은 하지 못했다.

이 셋을 "통과"로 보고하지 않는다 — 사용자 환경에서 검수가 필요하다.

---

## 개발

```bash
python -m pytest tests/ -v      # 테스트
python -m ruff check src/ tests/   # lint
python -m mypy src/pst_engine/     # 타입 체크
```

`tests/conftest.py`의 `FakeParser`가 실제 PST 없이 indexer/orchestrator
전체 파이프라인을 검증한다(손상 파일 시뮬레이션 포함). `readpst_parser.py`
의 순수 MIME 헬퍼는 `tests/test_readpst_helpers.py`가 실제 `email`
패키지로 직접 단위 테스트한다 — 네트워크나 외부 바이너리 없이도 파서
로직의 상당 부분을 검증할 수 있다.

## 구현 중 명세에서 조정한 것

- **상태 머신의 `RETRY`를 별도 영속 상태로 두지 않았다.** DB busy/lock
  재시도는 `storage.upsert_batch()` 내부의 지수 백오프 루프로 처리되고,
  최종 실패해야 예외가 올라와 그 파일이 `FAILED`로 남는다. 파일 단위로
  "RETRY 중"을 영속화해도 재실행 판단(멱등 재순회)에 추가 정보가 되지
  않아 생략했다.
- **`--sort relevance`는 근사치다.** 최신 5000건(`config.relevance_candidates`)
  만 bm25로 재정렬한다 — 전체 정렬은 100만 건 기준 약 700ms가 걸려
  200ms 예산을 못 맞춘다. 근사임을 CLI/JSON 출력에 표시한다.
