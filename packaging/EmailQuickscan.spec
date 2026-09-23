# PyInstaller spec — Windows onefile 빌드.
#
# 실행 방법(빌드 머신, packaging/BUILD.md 절차 4~5 완료 후):
#   pyinstaller packaging/EmailQuickscan.spec
#
# 이 spec 자체는 이 세션에서 Linux용 PyInstaller로 실제로 빌드·실행해
# 검증했다(Windows exe 자체는 만들 수 없지만, Analysis/EXE 구성이
# 유효한지와 진입점이 실제로 도는지는 플랫폼 무관하게 확인 가능하다).
# 그 과정에서 최상위 스크립트를 src/pst_engine/cli.py로 직접 지정하면
# 상대 import 때문에 실행 즉시 죽는 버그를 발견해 packaging/entrypoint.py
# 를 따로 두는 방식으로 고쳤다 — 아래 Analysis()의 스크립트 인자 참조.
# Windows 고유 동작(콘솔 창, 아이콘 등)은 그래도 실제 Windows에서
# 검증해야 한다 — packaging/BUILD.md의 "검증 체크리스트" 참조.

from pathlib import Path

ROOT = Path(SPECPATH).parent  # packaging/ 의 부모 = 저장소 루트
SRC = ROOT / "src"

# readpst는 표준 빌드 절차에서 더 이상 준비하지 않는다 — pypff가
# libpff-python-windows(사전 빌드 wheel, 컴파일 불필요)로 1순위 파서를
# 확보하므로 readpst 없이도 PST를 연다(BUILD.md 참조). 그래도 나중에
# 사용자가 readpst.exe를 직접 구해 packaging/bundled_bin/에 넣으면 이
# 코드가 그대로 집어 동봉한다 — 선택적 2차 안전망으로만 남겨 둔다.
bundled_bin = ROOT / "packaging" / "bundled_bin"
binaries = []
readpst_exe = bundled_bin / "readpst.exe"
if readpst_exe.exists():
    binaries.append((str(readpst_exe), "."))

datas = [
    (str(ROOT / "config" / "default.yaml"), "config"),
]

a = Analysis(
    [str(ROOT / "packaging" / "entrypoint.py")],
    pathex=[str(SRC)],
    binaries=binaries,
    datas=datas,
    hiddenimports=[
        # PyInstaller의 정적 분석이 optional_deps.py의 동적 import를
        # 못 찾을 수 있어 명시한다. 빌드 venv에 없으면 자동으로
        # 제외되니, 있는 것만 실제로 번들된다.
        "pypff",
        "extract_msg",
        "yaml",
        "chardet",
        "rich",
    ],
    hookspath=[],
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name="EmailQuickscan",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,  # CLI로도 쓰이므로 콘솔 유지. GUI 전용 exe가 필요하면
                   # 별도 spec에서 console=False + entry를 gui.run_gui로.
    # onefile 여부는 별도 플래그가 아니라 COLLECT()를 호출하지 않고
    # a.binaries/a.zipfiles/a.datas를 EXE()에 바로 넘기는 것으로
    # 결정된다(PyInstaller의 실제 동작 — 이 spec을 직접 빌드해 확인함).
    # 결과물은 packaging/dist/ 바로 아래의 단일 파일
    # EmailQuickscan(.exe)이며 하위 폴더가 생기지 않는다.
)
