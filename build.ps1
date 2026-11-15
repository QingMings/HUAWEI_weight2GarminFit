# 一键打包：跑回归测试 → PyInstaller 出 exe 目录 → Inno Setup 出安装包
# 用法：powershell -ExecutionPolicy Bypass -File .\build.ps1
#       -SkipTests 跳过测试；-SkipInstaller 只出 exe
param(
    [switch]$SkipTests,
    [switch]$SkipInstaller,
    [string]$Python
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
if (-not $Python) {
    # 本地用项目虚拟环境；CI（GitHub Actions）里没有 .venv，直接用 runner 的 python
    $venv = Join-Path $root "desktop\.venv\Scripts\python.exe"
    $Python = if (Test-Path $venv) { $venv } else { "python" }
}
$spec = Join-Path $root "desktop\GarminWeightHelper.spec"
$iss = Join-Path $root "desktop\installer.iss"
$dist = Join-Path $root "dist"
$work = Join-Path $env:TEMP "garmin_weight_build"

if (-not (Test-Path $Python) -and -not (Get-Command $Python -ErrorAction SilentlyContinue)) {
    throw "没找到 Python：$Python"
}

if (-not $SkipTests) {
    Write-Host "[1/3] 回归测试" -ForegroundColor Cyan
    & $Python -m unittest discover -s (Join-Path $root "tests") -t (Join-Path $root "tests")
    if ($LASTEXITCODE -ne 0) { throw "测试未通过，已停止打包" }
}

Write-Host "[2/3] PyInstaller 打包 exe" -ForegroundColor Cyan
& $Python -m PyInstaller --noconfirm --clean --distpath $dist --workpath $work $spec
if ($LASTEXITCODE -ne 0) { throw "PyInstaller 失败" }

if ($SkipInstaller) { Write-Host "完成：$dist\GarminWeightHelper"; exit 0 }

Write-Host "[3/3] Inno Setup 生成安装包" -ForegroundColor Cyan
$iscc = @(
    (Join-Path $env:LOCALAPPDATA "Programs\Inno Setup 6\ISCC.exe"),
    "C:\Program Files (x86)\Inno Setup 6\ISCC.exe",
    "C:\Program Files\Inno Setup 6\ISCC.exe"
) | Where-Object { Test-Path $_ } | Select-Object -First 1
if (-not $iscc) {
    Write-Warning "未安装 Inno Setup，只生成了 exe 目录：$dist\GarminWeightHelper"
    Write-Warning "安装地址：https://jrsoftware.org/isdl.php"
    exit 0
}
& $iscc $iss
if ($LASTEXITCODE -ne 0) { throw "Inno Setup 编译失败" }
Write-Host "完成：$dist" -ForegroundColor Green
