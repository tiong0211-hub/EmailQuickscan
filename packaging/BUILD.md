# Windows 배포용 빌드 절차

**이 절차는 아직 실행되지 않았다.** 사용자 검수 완료 후 진행하기로
합의된 순서를 문서화만 해 둔 것이다(계획 문서 T11 참조). 이 문서와
`EmailQuickscan.spec`, `build_windows.ps1`은 개발용 Linux 세션에서
작성했고, 실제 빌드는 **Windows 빌드 머신**에서 사람이 실행해야 한다.

## 컴파일러가 필요 없다 — 왜 그런지

최초 계획은 `libpff-python`을 소스에서 MSVC로 직접 컴파일하는 것이었다
(PyPI에 Windows 휠이 없어서). Visual Studio Build Tools는 수 GB에
달해 빌드 머신 준비 부담이 컸다.

재조사 결과, **`libpff-python-windows`**(PyPI)라는 사전 빌드 패키지가
있다는 걸 확인했다: libpff 동일 버전(20231205)을 Windows용으로 미리
컴파일해 `win_amd64` wheel(cp310~cp313)로 배포한다. 이 wheel을 실제로
받아 내부를 열어 확인한 결과:

- `pypff.cp3xx-win_amd64.pyd` **단일 파일**뿐이고 별도 DLL 의존성이 없다
  (정적 링크) — `pip install`만으로 끝난다.
- 설치되는 모듈명이 `libpff-python`(비-Windows)과 똑같이 `pypff`다 —
  `resolver.py`/`pypff_parser.py`/`optional_deps.py`는 **코드 변경이
  전혀 필요 없다**. `requirements.txt`의 환경 마커
  (`sys_platform == "win32"`)가 자동으로 이 wheel을 고른다.

**주의할 점 하나**: 이 wheel은 libyal 프로젝트 원저자(Joachim Metz)가
아니라 제3자(PyPI 사용자 BenoitChevrier)가 올린 비공식 재배포판이다.
이메일 내용을 다루는 도구이므로 신뢰성을 따졌고, 다음 근거로 사용하기로
결정했다(사용자 승인):
- `libpff-python`(공식)과 **버전 번호가 정확히 동일**(20231205) —
  같은 시점의 공식 소스를 빌드한 것으로 보인다.
- wheel 안에 libpff 공식 소스(`pypff/*.c` 전체)가 그대로 포함돼 있어
  공식 GitHub 저장소(`github.com/libyal/libpff`)와 직접 대조 가능하다.
- 정확한 버전(`==20231205`)으로 고정해 설치한다 — 이후 다른 버전이
  올라와도 자동으로 바뀌지 않는다.

이 판단이 바뀌면(예: 공식 wheel이 나오거나, 더 신뢰할 수 있는 빌드
경로가 생기면) `requirements.txt`/`pyproject.toml`의 해당 줄만 바꾸면
된다 — 나머지 코드는 영향받지 않는다.

## readpst(2순위 폴백 파서)는 Windows 배포판에서 뺐다

readpst는 공식 Windows 바이너리가 없다(Cygwin/WSL/직접 빌드뿐 — 이
세션에서 조사했지만 신뢰할 만한 사전 빌드 exe를 찾지 못했다). pypff가
컴파일 없이 1순위로 확보되므로, Windows 배포판에서는 readpst 없이도
대부분의 PST가 열린다. `resolver.py`의 4단계 규칙은 그대로 두었으므로
(readpst.exe가 없으면 자동으로 건너뜀), 나중에 필요하면
`packaging/bundled_bin/readpst.exe`에 직접 구한 바이너리를 넣기만 하면
`EmailQuickscan.spec`이 자동으로 동봉한다 — 코드 변경 없이 복구 가능한
선택 사항으로 남겨뒀다.

## 빌드 머신 요구사항

- Windows 10/11, 사내망이 아니라 **인터넷이 되는** 별도의 빌드 전용 PC
  (배포 대상 PC와 다르다 — 배포 대상은 pip 설치가 불가능한 사내망 PC).
- Python 3.10~3.13 (`libpff-python-windows`가 지원하는 범위).
- **그게 전부다.** Visual Studio Build Tools도, MSYS2도, Git도 필요
  없다(libpff 소스를 받아 컴파일하지 않으므로).

## 절차

1. **가상환경 준비**
   ```powershell
   python -m venv build_venv
   build_venv\Scripts\Activate.ps1
   pip install --upgrade pip
   ```

2. **가속 패키지 설치** (빌드 venv에만 — 배포 대상 PC에는 설치하지 않는다)
   ```powershell
   pip install -r requirements.txt
   ```
   `requirements.txt`의 환경 마커가 이 머신이 Windows임을 보고
   `libpff-python-windows`를 고른다. 컴파일이 없으므로 몇 초~몇십 초
   안에 끝난다. 설치 후 `python -c "import pypff"`가 에러 없이
   끝나는지 확인한다(`build_windows.ps1`이 자동으로 확인해 준다).

3. **PyInstaller 빌드**
   ```powershell
   pyinstaller packaging\EmailQuickscan.spec
   ```
   또는 위 2~3번을 한 번에: `.\packaging\build_windows.ps1`

   결과물: `packaging\dist\EmailQuickscan.exe` (단일 파일, 하위 폴더 없음).
   spec이 `COLLECT()`를 쓰지 않고 `a.binaries`/`a.zipfiles`/`a.datas`를
   `EXE()`에 직접 넘기는 onefile 구성이라 `dist` 바로 아래에 exe 하나만
   생긴다 — 이 저장소에서 실제로 빌드해 확인한 결과다.

4. **번들 검증 체크리스트** (배포 전 반드시 확인)
   - [ ] 패키지가 전혀 설치되지 않은 깨끗한 Windows PC에서 exe가 실행되는가
   - [ ] `EmailQuickscan.exe index --dry-run <샘플 PST>`가 파서 선택 결과를
         출력하는가 — **pypff가 잡혀야 정상**(readpst는 이제 기본적으로
         번들되지 않는다)
   - [ ] readpst 없이 pypff만으로 실제 PST 1개가 끝까지 인덱싱되는가
   - [ ] `계약`(2자) 검색이 결과를 반환하는가
   - [ ] GUI(`EmailQuickscan.exe gui`)가 정상적으로 뜨는가
   - [ ] 첨부 다운로드(attach)가 동작하는가
   - [ ] 작업 관리자에서 자식 프로세스(워커)가 무한 재생성되지 않는가
         (freeze_support 누락 시 생기는 증상 — orchestrator.py의
         `_worker_main`이 spawn으로 뜨는지 확인)

## 이 세션에서 검증하지 못한 것 (정직하게 남김)

- 실제 Windows 머신에서 `pip install libpff-python-windows` 후
  `import pypff`가 정말로 되는지 (wheel 내부 구조는 이 세션에서 직접
  받아 확인했지만, Windows 실행 환경 자체는 이 세션에 없다)
- PyInstaller onefile exe의 실제 생성·실행
- Windows에서 `multiprocessing`(spawn)이 orchestrator.py와 함께
  실제로 동작하는지 (`freeze_support()`는 코드에 넣어뒀지만
  Windows에서 실행해 확인하지는 못했다)

이 셋은 Windows 빌드 머신에서 사람이 직접 확인해야 한다.
