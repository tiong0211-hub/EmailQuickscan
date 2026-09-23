# EmailQuickscan 사용 안내서

이 문서는 **사내망(인터넷·pip 설치 불가) Windows PC**에 EmailQuickscan을
가져가서 실제로 PST 메일을 검색하기까지의 전 과정을 다룬다. 개발/설계
배경은 `README.md`, 빌드 절차 상세는 `packaging/BUILD.md`를 참조.

---

## 0. 전체 그림

```
① 인터넷 되는 빌드 PC          ② 파일 반출(보안 절차)      ③ 사내망 PC
   (관리자, 한 번만)               (USB 등, 파일 1개)          (일반 사용자)
   ─────────────────────    →    ──────────────────    →    ──────────────
   EmailQuickscan.exe 빌드         무결성 확인(해시 대조)        폴더에 두고
                                                                바로 실행
```

- **한 번만, 관리자가**: 인터넷 되는 별도 PC에서 exe 파일 하나를 만든다.
- **매번, 보안 절차대로**: 그 exe 파일 하나만 사내망으로 반출한다.
- **사내망 PC에서**: 설치·pip·인터넷 전혀 없이 그 파일을 실행하면 끝이다.

exe 하나에 모든 게 들어있다(Python, SQLite, PST 파서, GUI). 사내망 PC에
아무것도 설치하지 않는다.

---

## 1. 관리자: exe 만들고 반입하기

### 1-1. 빌드 (인터넷 되는 PC, 사내망과 분리된 별도 PC)

`build_windows.ps1`은 **PowerShell 전용 스크립트**다. 아래 순서대로
**PowerShell 창**(명령 프롬프트/cmd.exe 아님 — 프롬프트가 `PS C:\...>`
로 시작하는지 확인)에서 실행한다.

1. 시작 메뉴에서 `PowerShell` 검색 → **Windows PowerShell** 실행
   (관리자 권한 불필요)
2. 아래 명령을 순서대로 입력:

```powershell
git clone <이 저장소 주소>
cd EmailQuickscan
.\packaging\build_windows.ps1
```

만약 `.\packaging\build_windows.ps1` 실행 시 "이 시스템에서 스크립트를
실행할 수 없으므로..." 같은 빨간 오류가 뜨면, 조직 정책이 PowerShell
스크립트 실행을 기본 차단하고 있는 것이다. 같은 창에서 아래를 먼저
실행한 뒤 다시 시도한다(이 PowerShell 창에만 적용되며 시스템 전체
설정은 바꾸지 않는다):

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
```

**명령 프롬프트(cmd.exe)만 열려 있고 PowerShell 창을 새로 띄우기
어렵다면**, cmd.exe 안에서도 아래처럼 실행할 수 있다:

```
powershell -ExecutionPolicy Bypass -File packaging\build_windows.ps1
```

Visual Studio나 별도 컴파일러 설치가 필요 없다(자세한 이유는
`packaging/BUILD.md` 참조 — pypff는 컴파일 없는 사전 빌드 패키지를
쓴다). 결과물:

```
packaging\dist\EmailQuickscan.exe
```

(하위 폴더 없이 `dist` 바로 아래에 파일 하나만 생긴다 — onefile 빌드.)

빌드 후 `packaging/BUILD.md`의 "번들 검증 체크리스트"를 **반드시** 통과
시킨다(파서가 잡히는지, 실제 PST 1개로 끝까지 도는지, GUI가 뜨는지 등).

### 1-2. 반출 전 무결성 기록 (권장)

반출 중 파일이 변조·손상되지 않았는지 나중에 대조할 수 있도록, 빌드
직후 체크섬을 남겨 둔다.

```powershell
Get-FileHash packaging\dist\EmailQuickscan.exe -Algorithm SHA256
```

이 값을 반출 승인 문서 등에 같이 기록해 둔다.

### 1-3. 반출

회사의 파일 반출 보안 절차(USB 반입·반출 승인, 매체 검사 등)를 따라
`EmailQuickscan.exe` **파일 하나만** 사내망으로 옮긴다. 소스 코드나
빌드 산출물의 나머지 파일은 필요 없다.

사내망에 들어온 뒤, 위에서 기록한 해시와 대조해 본다:

```powershell
Get-FileHash EmailQuickscan.exe -Algorithm SHA256
```
두 값이 같으면 반출 중 손상이 없었다는 뜻이다.

---

## 2. 사내망 PC: 준비

### 2-1. 전용 폴더에 두기

```
C:\EmailQuickscan\
└── EmailQuickscan.exe
```

**중요**: 이 프로그램은 실행되는 폴더(exe가 있는 폴더) 바로 밑에
`data\` 폴더를 만들어 색인 결과(DB)와 로그를 저장한다. 바탕화면
바로가기로 실행하든, 다른 폴더에서 명령창으로 실행하든 **항상 exe가
있는 폴더의 data 폴더**를 쓰도록 만들어져 있으므로, 실행 위치를
신경 쓰지 않아도 색인 결과가 흩어지지 않는다. 다만 **exe를 옮기면
그 밑의 data 폴더도 같이 옮겨야** 색인 결과가 유지된다(4-4절 참조).

### 2-2. (선택) 설정 바꾸기

기본 설정(워커 수 4개, 배치 크기 1000건 등)은 대부분의 PC에서 그대로
쓰면 된다. 사양이 낮은 PC에서 워커 수를 줄이고 싶다면:

```
C:\EmailQuickscan\
├── EmailQuickscan.exe
└── config\
    └── default.yaml     ← 관리자가 이 폴더/파일을 직접 만들어 넣는다
```

exe 옆에 이 파일을 두면 **재빌드 없이** 즉시 반영된다(파일 안 예시는
저장소의 `config/default.yaml` 참조). 없으면 exe에 내장된 기본값이
쓰인다.

### 2-3. 명령창 열기

`C:\EmailQuickscan` 폴더에서 주소창에 `cmd`를 입력하고 Enter(탐색기
주소표시줄에 직접 입력하면 그 폴더가 작업 폴더인 명령창이 뜬다). 이후
모든 명령은 이 창에서 실행한다.

---

## 3. 첫 실행: 파서 확인

PST를 실제로 색인하기 전에, 이 exe가 PST를 열 수 있는 상태로 잘
만들어졌는지 먼저 확인한다.

```powershell
EmailQuickscan.exe index --dry-run "D:\메일보관"
```

출력 예시:
```
경로                        크기      선택
D:\메일보관\2023.pst         1.2GB    pypff — pypff 모듈 사용 가능 (1순위)
D:\메일보관\2024.pst         890MB    pypff — pypff 모듈 사용 가능 (1순위)
```

`선택` 칸에 `pypff`가 떠야 정상이다. `FAILED (파서 없음)`이 뜨면 exe가
잘못 빌드된 것이니 관리자에게 알린다(`packaging/BUILD.md` 참조).

---

## 4. 색인하기 (index)

### 4-1. PST 폴더 통째로

```powershell
EmailQuickscan.exe index "D:\메일보관" --recursive
```

`D:\메일보관` 아래(하위 폴더 포함)의 모든 `.pst`/`.ost` 파일을 찾아
색인한다. 처음엔 용량에 비례해 시간이 걸린다(수십 GB면 몇십 분 단위).
콘솔에 파일별 진행 상황이 실시간으로 출력된다.

### 4-2. 특정 파일 몇 개만

```powershell
EmailQuickscan.exe index "D:\메일보관\영업1팀.pst" "D:\메일보관\영업2팀.pst"
```

### 4-3. 중단해도 된다

`Ctrl+C`로 멈춰도 그때까지 처리한 내용은 저장돼 있다. 나중에 **같은
명령을 다시 실행**하면 이미 끝난 파일은 건너뛰고 나머지만 이어서
한다 — 중복 저장도 되지 않는다.

### 4-4. 새 PST가 생기면

새로 받은 PST가 있으면, 그냥 다시 색인 명령을 실행한다:

```powershell
EmailQuickscan.exe index "D:\메일보관" --recursive
```

이미 색인된 파일(크기·수정시각이 그대로)은 자동으로 건너뛰고, 새
파일이나 바뀐 파일만 처리한다.

### 4-5. 색인 결과(DB) 백업·이동

`C:\EmailQuickscan\data\mail_index.db` 파일 하나가 색인 결과 전부다.
이 파일만 복사하면:
- 백업이 된다.
- 다른 PC에 `EmailQuickscan.exe` + 이 `data\mail_index.db`만 가져다
  두면 **원본 PST 없이도 검색·본문 열람**이 그대로 된다(본문은 DB
  안에 압축 저장돼 있다). 단, 첨부 파일을 꺼내는 기능(`attach`)만은
  원본 PST가 그 자리에 있어야 동작한다.

---

## 5. 검색하기

### 5-1. GUI (마우스로)

```powershell
EmailQuickscan.exe gui
```

검색창에 타이핑하면 바로 결과가 갱신된다. 왼쪽이 목록, 오른쪽이 본문
미리보기다. 첨부파일은 미리보기 아래 버튼을 눌러 저장한다.

바로가기를 만들어 두면 더블클릭만으로 켤 수 있다:
1. `EmailQuickscan.exe`에 마우스 오른쪽 클릭 → 바로 가기 만들기
2. 바로가기 속성 → **대상**란 끝에 한 칸 띄고 `gui` 추가
   (예: `"C:\EmailQuickscan\EmailQuickscan.exe" gui`)
3. **시작 위치**가 `C:\EmailQuickscan`인지 확인(비어 있으면 자동으로
   맞는 경우가 많지만, 다르면 직접 채운다)

> 참고: 이 exe는 명령줄 도구와 GUI를 겸하도록 만들어져 있어, GUI를
> 켜면 뒤에 검은 명령창이 하나 같이 뜬다. 정상 동작이며, 그 창을
> 닫으면 GUI도 같이 꺼진다.

### 5-2. 명령줄로

```powershell
EmailQuickscan.exe search "계약서"
EmailQuickscan.exe search "계약서 from:김영수 hasattachment:yes"
EmailQuickscan.exe search --from 김영수 --has-attachment --after 2024-01-01
EmailQuickscan.exe search "계약" --limit 50 --sort relevance
```

자세한 검색 문법(필드 지정, 날짜 형식, AND/OR/NOT 등)은
`README.md`의 "검색 문법" 절 표를 참조. 자주 쓰는 것만 추리면:

| 하고 싶은 것 | 입력 예시 |
|---|---|
| 제목/본문/보낸사람 등 어디든 이 단어 포함 | `계약서` |
| 제목에만 | `subject:계약서` |
| 특정인이 보낸 것만 | `from:김영수` |
| 첨부파일 있는 것만 | `hasattachment:yes` |
| 첨부파일명으로 | `attachments:견적서.pdf` |
| 최근 일주일 | `received:7d` |
| 특정 기간 | `received:2024-01-01..2024-03-31` |
| 여러 조건 동시에(공백=AND) | `계약서 from:김영수 hasattachment:yes` |
| 둘 중 하나 | `계약 OR 견적` |
| 이 단어는 빼고 | `계약 -해지` |

결과가 하나도 안 나오면, 어느 조건 때문인지 화면에 같이 알려준다.

### 5-3. 본문 전체 보기

```powershell
EmailQuickscan.exe show <메일ID>
```
`<메일ID>`는 `search` 결과 표의 `message_id` 칸 값을 그대로 쓴다.

### 5-4. 첨부파일 꺼내기

```powershell
EmailQuickscan.exe attach <메일ID> --file 0 -o C:\Downloads
```
`--file 0`은 첫 번째 첨부(0번부터 시작). `show` 명령으로 먼저 몇 번인지
확인한다. **원본 PST 파일이 색인 당시 그 경로에 그대로 있어야** 동작한다
(DB에는 첨부 이름만 있고 내용은 없다 — 검색은 되지만 꺼낼 땐 원본이
필요하다).

### 5-5. 검색 결과 내보내기 (감사·제출용)

```powershell
EmailQuickscan.exe export "계약서 from:김영수" --format csv --out 결과.csv
EmailQuickscan.exe export "계약서" --format eml --out C:\내보내기폴더
```
`csv`는 엑셀에서 바로 열리는 메일 목록(제목·보낸사람·날짜 등),
`eml`은 메일 본문 파일들(첨부는 포함 안 됨, 첨부는 5-4절로 따로).

---

## 6. 문제 해결

| 증상 | 원인·해결 |
|---|---|
| `index --dry-run`에서 모든 파일이 `FAILED (파서 없음)` | exe가 잘못 빌드됨. 관리자에게 `packaging/BUILD.md` 검증 체크리스트 확인 요청 |
| 검색 결과가 0건 | 화면에 "이 조건 때문일 수 있습니다"로 원인 조건이 같이 표시된다. 조건을 하나씩 빼며 확인 |
| 첨부가 안 꺼내진다 | 원본 PST가 이동/삭제됐을 가능성. 색인 당시 경로에 파일이 그대로 있는지 확인 |
| 색인이 아주 오래 걸린다 | PST 용량과 개수에 비례한다. 중단해도 이어서 할 수 있으니(4-3절) 업무 시간 외에 돌려도 된다 |
| 새로 산 PC로 옮겼는데 검색 결과가 비어 있다 | `data\mail_index.db`를 같이 옮기지 않은 것. 4-5절 참조 |
| GUI 뒤에 검은 창이 계속 떠 있다 | 정상이다(5-1절 참고). 닫으면 GUI도 같이 꺼진다 |

---

## 7. 명령어 한눈에 보기

| 명령 | 용도 |
|---|---|
| `index <경로...> [--recursive] [--dry-run]` | PST 색인(처음 1회 + 새 PST 생길 때마다) |
| `search <검색어> [--limit N] [--sort date\|relevance]` | 검색 |
| `show <메일ID>` | 본문 전문 + 첨부 목록 |
| `attach <메일ID> --file <번호> -o <폴더>` | 첨부 꺼내기 |
| `export <검색어> --format csv\|eml --out <경로>` | 내보내기 |
| `gui` | 마우스로 쓰는 화면 |

모든 명령 앞에 `EmailQuickscan.exe `를 붙여서 실행한다(예:
`EmailQuickscan.exe search 계약서`).
