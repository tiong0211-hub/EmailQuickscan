# Windows 배포용 빌드 절차

**이 절차는 아직 실행되지 않았다.** 사용자 검수 완료 후 진행하기로
합의된 순서를 문서화만 해 둔 것이다(계획 문서 T11 참조). 이 문서와
`EmailQuickscan.spec`, `build_windows.ps1`은 개발용 Linux 세션에서
작성했고, 실제 빌드는 **Windows 빌드 머신**에서 사람이 실행해야 한다.

## 빌드 머신 요구사항

- Windows 10/11, 사내망이 아니라 **인터넷이 되는** 별도의 빌드 전용 PC
  (배포 대상 PC와 다르다 — 배포 대상은 pip 설치가 불가능한 사내망 PC).
- Python 3.10 이상 (개발에 쓴 버전과 맞추는 걸 권장: 3.10~3.12).
- Visual Studio Build Tools (MSVC, C++ 워크로드) — `libpff-python`이
  PyPI에 Windows 휠을 배포하지 않아 소스에서 직접 컴파일해야 한다.
- Git (libpff 소스 클론용).

## 절차

1. **가상환경 준비**
   ```powershell
   python -m venv build_venv
   build_venv\Scripts\Activate.ps1
   pip install --upgrade pip
   ```

2. **libpff를 소스에서 빌드해 pypff.pyd 얻기**

   PyPI의 `libpff-python` sdist(`pip download libpff-python --no-binary :all:`)
   안에 `pypff/`(Python 바인딩)와 `libpff/`(C 라이브러리) 소스가 함께
   들어있다. MSVC로 빌드하는 방법은 sdist 안의 `msvscpp/` 솔루션 파일
   또는 `setup.py build_ext`를 MSVC 환경에서 실행하는 방법 두 가지가
   있다 — 이 프로젝트에서는 실제로 시도해 보지 못했으므로(Windows
   빌드 머신이 없음), libpff 프로젝트의 공식 Windows 빌드 안내를
   따르는 것을 권장한다.

   실패해도 치명적이지 않다: `pypff.pyd`를 못 만들면 실행 파일은
   readpst(아래 3번) 또는 `.msg` 경로로 자동 폴백한다
   (CLAUDE.md의 4단계 파서 선택 규칙).

3. **readpst.exe 확보**
   ```powershell
   pip download libpst  # 또는 libpst 공식 Windows 바이너리 배포본 확인
   ```
   libpst(readpst의 원 프로젝트)의 Windows 빌드는 배포판마다 상황이
   다르다. 확보한 `readpst.exe`와 그 의존 DLL을
   `packaging/bundled_bin/readpst.exe`에 둔다.

4. **순수 Python 가속 패키지 설치** (빌드 venv에만 — 배포 대상 PC에는
   설치하지 않는다)
   ```powershell
   pip install extract-msg PyYAML chardet rich pyinstaller
   ```

5. **PyInstaller 빌드**
   ```powershell
   pyinstaller packaging\EmailQuickscan.spec
   ```
   결과물: `packaging\dist\EmailQuickscan\EmailQuickscan.exe`
   (또는 spec의 onefile 설정에 따라 단일 exe).

6. **번들 검증 체크리스트** (배포 전 반드시 확인)
   - [ ] 패키지가 전혀 설치되지 않은 깨끗한 Windows PC에서 exe가 실행되는가
   - [ ] `EmailQuickscan.exe index --dry-run <샘플 PST>`가 파서 선택 결과를
         출력하는가 (pypff/readpst 중 무엇이 잡혔는지 확인)
   - [ ] 실제 PST 1개로 인덱싱이 끝까지 도는가
   - [ ] `계약`(2자) 검색이 결과를 반환하는가
   - [ ] GUI(`EmailQuickscan.exe gui`)가 정상적으로 뜨는가
   - [ ] 첨부 다운로드(attach)가 동작하는가
   - [ ] 작업 관리자에서 자식 프로세스(워커)가 무한 재생성되지 않는가
         (freeze_support 누락 시 생기는 증상 — orchestrator.py의
         `_worker_main`이 spawn으로 뜨는지 확인)

## 이 세션에서 검증하지 못한 것 (정직하게 남김)

- `libpff` 소스의 실제 MSVC 빌드 성공 여부
- PyInstaller onefile exe의 실제 생성·실행
- Windows에서 `multiprocessing`(spawn)이 orchestrator.py와 함께
  실제로 동작하는지 (`freeze_support()`는 코드에 넣어뒀지만
  Windows에서 실행해 확인하지는 못했다)

이 셋은 Windows 빌드 머신에서 사람이 직접 확인해야 한다.
