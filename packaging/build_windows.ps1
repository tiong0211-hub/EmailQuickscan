# EmailQuickscan Windows 빌드 스크립트
#
# 이 스크립트는 packaging/BUILD.md의 절차를 자동화한 것이다. 실제
# Windows 빌드 머신이 없는 이 개발 세션에서는 실행/검증하지 못했다 —
# 실행 전 BUILD.md를 먼저 읽고, 특히 libpff MSVC 빌드 단계는 수동 개입이
# 필요할 수 있다.
#
# 사용법 (PowerShell, 빌드 전용 PC에서):
#   .\packaging\build_windows.ps1

$ErrorActionPreference = "Stop"

$RepoRoot = Split-Path -Parent $PSScriptRoot
$VenvDir = Join-Path $RepoRoot "build_venv"
$BundledBin = Join-Path $PSScriptRoot "bundled_bin"

Write-Host "== 1. 빌드 가상환경 준비 ==" -ForegroundColor Cyan
if (-not (Test-Path $VenvDir)) {
    python -m venv $VenvDir
}
& "$VenvDir\Scripts\Activate.ps1"
python -m pip install --upgrade pip

Write-Host "== 2. 가속 패키지 설치 (빌드 venv 전용, 배포 대상엔 설치 안 함) ==" -ForegroundColor Cyan
# libpff-python은 Windows 휠이 없어 여기서 설치를 시도하면 소스 빌드가
# 자동으로 걸린다 — MSVC Build Tools가 없으면 여기서 실패한다. 실패해도
# 스크립트를 멈추지 않고 넘어간다(readpst/.msg 폴백으로 계속 동작).
pip install extract-msg PyYAML chardet rich pyinstaller
try {
    pip install libpff-python
} catch {
    Write-Warning "libpff-python 설치/빌드 실패 — pypff.pyd 없이 진행합니다. readpst 또는 .msg 경로로 폴백됩니다."
}

Write-Host "== 3. readpst.exe 확인 ==" -ForegroundColor Cyan
if (-not (Test-Path (Join-Path $BundledBin "readpst.exe"))) {
    Write-Warning "packaging\bundled_bin\readpst.exe가 없습니다. BUILD.md 절차 3을 참고해 직접 준비해 주세요."
    Write-Warning "readpst 없이도 빌드는 계속되지만, pypff도 없다면 PST 파서가 하나도 없는 exe가 됩니다."
}

Write-Host "== 4. PyInstaller 빌드 ==" -ForegroundColor Cyan
Push-Location $RepoRoot
pyinstaller packaging\EmailQuickscan.spec --distpath packaging\dist --workpath packaging\build --noconfirm
Pop-Location

Write-Host "== 완료: packaging\dist\EmailQuickscan\ ==" -ForegroundColor Green
Write-Host "배포 전 BUILD.md의 '번들 검증 체크리스트'를 반드시 확인하세요." -ForegroundColor Yellow
