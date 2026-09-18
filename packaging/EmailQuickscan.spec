# PyInstaller spec — Windows onefile 빌드.
#
# 실행 방법(빌드 머신, packaging/BUILD.md 절차 4~5 완료 후):
#   pyinstaller packaging/EmailQuickscan.spec
#
# 이 파일은 이 저장소 안에서 실제로 pyinstaller를 돌려 검증하지 못했다
# (Windows 빌드 머신이 이 세션에 없음) — packaging/BUILD.md의 "검증
# 체크리스트"를 빌드 머신에서 반드시 통과시켜야 한다.

import sys
from pathlib import Path

block_cipher = None

ROOT = Path(SPECPATH).parent  # packaging/ 의 부모 = 저장소 루트
SRC = ROOT / "src"

# 빌드 머신에 readpst.exe를 확보해 뒀다면 여기 두고 동봉한다
# (packaging/BUILD.md 절차 3). 없으면 빈 리스트로 두고, resolver.py가
# readpst 경로를 자동으로 건너뛴다.
bundled_bin = ROOT / "packaging" / "bundled_bin"
binaries = []
readpst_exe = bundled_bin / "readpst.exe"
if readpst_exe.exists():
    binaries.append((str(readpst_exe), "."))

datas = [
    (str(ROOT / "config" / "default.yaml"), "config"),
]

a = Analysis(
    [str(SRC / "pst_engine" / "cli.py")],
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
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

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
    onefile=True,
)
