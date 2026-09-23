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

Write-Host "== 0. Python 확인 ==" -ForegroundColor Cyan
# python -m venv 등 외부 명령의 실패는 $ErrorActionPreference="Stop"으로
# 잡히지 않는다(터미네이팅 오류가 아니라 종료 코드일 뿐이므로) — Python이
# 없으면 조용히 다음 단계로 넘어가 훨씬 헷갈리는 2차 오류(Activate.ps1을
# 못 찾음)로 이어졌다. 여기서 먼저 명확하게 확인하고 즉시 안내한다.
#
# 상한(3.13)이 있는 이유: 1순위 파서 libpff-python-windows(PyPI)가
# 이 시점 기준 cp310~cp313 wheel만 제공한다(cp314 없음 — 실제 사용자가
# 막 나온 Python 3.14를 설치했다가 "No matching distribution" 오류로
# 이 사실을 이 세션에서 발견했다). "python" 명령이 이 범위 밖이면,
# Python Launcher(py.exe)로 3.13→3.10 순서로 맞는 버전을 자동으로 찾는다
# — 여러 Python 버전이 공존해도(예: 3.14 기본 + 3.12 추가 설치) 동작한다.
function Test-VersionInRange($exe, $arg) {
    if ($arg) { & $exe $arg -c "import sys; exit(0 if (3,10) <= sys.version_info[:2] <= (3,13) else 1)" 2>$null }
    else { & $exe -c "import sys; exit(0 if (3,10) <= sys.version_info[:2] <= (3,13) else 1)" 2>$null }
    return ($LASTEXITCODE -eq 0)
}

$PyExe = $null
$PyArg = $null

# PowerShell의 -and/-or는 단축 평가(short-circuit)를 하지 않는다 — 양쪽을
# 항상 평가하므로, "python이 없으면 & python ... 호출 자체를 건너뛴다"를
# 하나의 -and 식으로 쓰면 python이 정말 없을 때 & 호출이 그대로 실행돼
# CommandNotFoundException으로 스크립트가 죽는다. 그래서 중첩 if로
# 각 검사를 실제로 필요할 때만 실행한다.
if (Get-Command python -ErrorAction SilentlyContinue) {
    if (Test-VersionInRange "python" $null) {
        $PyExe = "python"
    }
}
if (-not $PyExe) {
    if (Get-Command py -ErrorAction SilentlyContinue) {
        foreach ($ver in @("3.13", "3.12", "3.11", "3.10")) {
            if (Test-VersionInRange "py" "-$ver") {
                $PyExe = "py"
                $PyArg = "-$ver"
                break
            }
        }
    }
}

if (-not $PyExe) {
    Write-Host "Python 3.10~3.13 범위의 인터프리터를 찾지 못했습니다." -ForegroundColor Red
    Write-Host "(libpff-python-windows가 현재 이 범위의 wheel만 제공합니다 —" -ForegroundColor Red
    Write-Host " Python이 아예 없거나, 3.14 이상만 설치돼 있으면 여기서 멈춥니다.)" -ForegroundColor Red
    Write-Host "https://www.python.org/downloads/ 에서 Python 3.12를 설치하세요." -ForegroundColor Red
    Write-Host "(이미 다른 버전이 있어도 함께 설치 가능합니다. 설치 후 'py -3.12'로" -ForegroundColor Red
    Write-Host " 이 스크립트가 자동으로 찾아 쓰므로 PATH 체크는 필수가 아닙니다.)" -ForegroundColor Red
    Write-Host "설치 후 이 PowerShell 창을 닫고 새로 열어 다시 실행하세요." -ForegroundColor Red
    exit 1
}
$foundLabel = if ($PyArg) { "$PyExe $PyArg" } else { $PyExe }
$foundVersion = if ($PyArg) { & $PyExe $PyArg --version } else { & $PyExe --version }
Write-Host "  $foundLabel 사용 ($foundVersion)" -ForegroundColor Green

Write-Host "== 1. 빌드 가상환경 준비 ==" -ForegroundColor Cyan
if (-not (Test-Path $VenvDir)) {
    if ($PyArg) { & $PyExe $PyArg -m venv $VenvDir } else { & $PyExe -m venv $VenvDir }
    if ($LASTEXITCODE -ne 0) {
        Write-Host "가상환경 생성 실패 (python -m venv). 위 오류 메시지를 확인하세요." -ForegroundColor Red
        exit 1
    }
}
& "$VenvDir\Scripts\Activate.ps1"
# 가상환경이 활성화된 뒤로는 "python"/"pip"이 항상 위에서 고른 올바른
# 버전을 가리키므로, 아래부터는 더 이상 $PyExe/$PyArg를 쓸 필요가 없다.
python -m pip install --upgrade pip

Write-Host "== 2. 가속 패키지 설치 (빌드 venv 전용, 배포 대상엔 설치 안 함) ==" -ForegroundColor Cyan
# requirements.txt의 환경 마커가 이 PC가 Windows임을 보고 libpff-python이
# 아니라 libpff-python-windows(사전 빌드 wheel)를 골라 설치한다 — 컴파일
# 없이 pypff.pyd 하나가 그대로 설치된다.
pip install -r (Join-Path $RepoRoot "requirements.txt")
if ($LASTEXITCODE -ne 0) {
    Write-Host "패키지 설치 실패. 위 pip 오류 메시지를 확인하세요(사내망 프록시/방화벽이 이 PC에도 걸려 있는지 등)." -ForegroundColor Red
    exit 1
}

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
$buildExitCode = $LASTEXITCODE
Pop-Location
if ($buildExitCode -ne 0) {
    Write-Host "PyInstaller 빌드 실패. 위 오류 메시지를 확인하세요." -ForegroundColor Red
    exit 1
}

Write-Host "== 완료: packaging\dist\EmailQuickscan.exe ==" -ForegroundColor Green
Write-Host "배포 전 BUILD.md의 '번들 검증 체크리스트'를 반드시 확인하세요." -ForegroundColor Yellow
