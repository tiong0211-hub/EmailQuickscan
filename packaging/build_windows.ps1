# EmailQuickscan Windows 빌드 스크립트
#
# 이 스크립트는 packaging/BUILD.md의 절차를 자동화한 것이다. 실제
# Windows 빌드 머신이 없는 이 개발 세션에서는 실행/검증하지 못했다.
#
# 컴파일러(Visual Studio Build Tools 등)가 전혀 필요 없다 — pypff는
# libpff-python-windows(PyPI)의 사전 빌드 wheel을 그대로 설치하고,
# 나머지 가속 패키지도 전부 순수 Python이거나 자체 wheel을 제공한다.
# 자세한 근거는 BUILD.md 참조.
#
# 사용법 (PowerShell, 빌드 전용 PC에서):
#   .\packaging\build_windows.ps1

$ErrorActionPreference = "Stop"

$RepoRoot = Split-Path -Parent $PSScriptRoot
$VenvDir = Join-Path $RepoRoot "build_venv"

Write-Host "== 1. 빌드 가상환경 준비 ==" -ForegroundColor Cyan
if (-not (Test-Path $VenvDir)) {
    python -m venv $VenvDir
}
& "$VenvDir\Scripts\Activate.ps1"
python -m pip install --upgrade pip

Write-Host "== 2. 가속 패키지 설치 (빌드 venv 전용, 배포 대상엔 설치 안 함) ==" -ForegroundColor Cyan
# requirements.txt의 환경 마커가 이 PC가 Windows임을 보고 libpff-python이
# 아니라 libpff-python-windows(사전 빌드 wheel)를 골라 설치한다 — 컴파일
# 없이 pypff.pyd 하나가 그대로 설치된다.
pip install -r (Join-Path $RepoRoot "requirements.txt")

Write-Host "== 3. pypff 설치 확인 ==" -ForegroundColor Cyan
$pypffOk = python -c "import pypff; print('ok')" 2>$null
if ($pypffOk -eq "ok") {
    Write-Host "  pypff 정상 설치됨 (1순위 PST 파서)" -ForegroundColor Green
} else {
    Write-Warning "pypff import 실패. 이 exe는 PST를 열 파서가 없을 수 있습니다."
    Write-Warning "requirements.txt의 libpff-python-windows 버전이 이 Python 버전(3.10~3.13)을 지원하는지 확인하세요."
}

Write-Host "== 4. PyInstaller 빌드 ==" -ForegroundColor Cyan
Push-Location $RepoRoot
pyinstaller packaging\EmailQuickscan.spec --distpath packaging\dist --workpath packaging\build --noconfirm
Pop-Location

Write-Host "== 완료: packaging\dist\EmailQuickscan\ ==" -ForegroundColor Green
Write-Host "배포 전 BUILD.md의 '번들 검증 체크리스트'를 반드시 확인하세요." -ForegroundColor Yellow
