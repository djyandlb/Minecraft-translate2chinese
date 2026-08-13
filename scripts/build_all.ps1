# ============================================================
# 像素译站 一键构建脚本（中文注释）
# 流程：前端 vite build -> PyInstaller(onedir + onefile) -> 整理发布目录 -> Inno Setup 安装版
# 用法：powershell -ExecutionPolicy Bypass -File scripts\build_all.ps1
# 依赖：Node.js / npm、Python 3.14 + pyinstaller、Inno Setup 6（仅安装版需要）
# 说明：本脚本是给用户一键重建用的完整流程；
#       Inno Setup 未安装时自动跳过安装版，仅产出便携版，并给出安装指引
# ============================================================

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

# ---------- 工具路径探测 ----------
function Find-ISCC {
    $candidates = @(
        "C:\Program Files (x86)\Inno Setup 6\ISCC.exe",
        "C:\Program Files\Inno Setup 6\ISCC.exe"
    )
    # 修复：winget 装 Inno Setup 默认到用户级（%LOCALAPPDATA%\Programs）——build_all 漏检
    $userInno = Join-Path $env:LOCALAPPDATA "Programs\Inno Setup 6\ISCC.exe"
    if (Test-Path $userInno) { $candidates += $userInno }
    foreach ($c in $candidates) {
        if (Test-Path $c) { return $c }
    }
    return $null
}

Write-Host "========== [0/4] 前置检查 =========="
# Node / npm
if (-not (Get-Command npm -ErrorAction SilentlyContinue)) {
    throw "未找到 npm：请先安装 Node.js（https://nodejs.org/）"
}
if (-not (Test-Path "frontend\node_modules")) {
    throw "缺少前端依赖：请先在 frontend 目录执行 npm install"
}
# PyInstaller
if (-not (Get-Command pyinstaller -ErrorAction SilentlyContinue)) {
    throw "未找到 pyinstaller：请先执行 pip install pyinstaller"
}
# Inno Setup（仅安装版需要，便携版不强制）
$iscc = Find-ISCC
if ($iscc) {
    Write-Host "  已找到 Inno Setup: $iscc"
} else {
    Write-Host "  [警告] 未找到 Inno Setup 6，本次将跳过安装版，仅产出便携版。"
    Write-Host "          安装 Inno Setup 6（https://jrsoftware.org/isdl.php）后重跑本脚本即可补出安装版。"
}

# ---------- [1/4] 构建前端 ----------
Write-Host "========== [1/4] 构建前端（vite build）=========="
Push-Location frontend
try {
    npm run build
    if ($LASTEXITCODE -ne 0) { throw "前端构建失败（npm run build 退出码 $LASTEXITCODE）" }
} finally {
    Pop-Location
}
Write-Host "  前端产物: frontend\dist"

# ---------- [2/4] PyInstaller 打包 ----------
Write-Host "========== [2/4] PyInstaller 打包（onedir + onefile）=========="
# 清理旧产物，保证全新一致（spec 原始输出到 dist\ 根，之后统一整理移动）。
# 修复：必须连 PyInstaller 的 build\ 缓存一起清——否则 EXE 图标/内容复用旧构建，
# 新 app-icon.ico 不生效（用户实测「应用图标还是原来的」）
foreach ($p in @("build", "dist\像素译站", "dist\像素译站.exe", "dist\安装版", "dist\便携版")) {
    if (Test-Path $p) {
        Remove-Item $p -Recurse -Force
        Write-Host "  清理旧产物: $p"
    }
}
pyinstaller scripts\mc_translator.spec
if ($LASTEXITCODE -ne 0) { throw "PyInstaller 打包失败（退出码 $LASTEXITCODE）" }
Write-Host "  PyInstaller 原始输出: dist\像素译站\ + dist\像素译站.exe"

# ---------- [3/4] 整理产物到发布目录 ----------
Write-Host "========== [3/4] 整理发布目录 =========="
# onedir -> 安装版源（供 Inno Setup 打包）
New-Item -ItemType Directory -Path "dist\安装版" -Force | Out-Null
Move-Item "dist\像素译站" "dist\安装版\像素译站"
Write-Host "  onedir 已移动到: dist\安装版\像素译站\"
# onefile -> 便携版
New-Item -ItemType Directory -Path "dist\便携版" -Force | Out-Null
Move-Item "dist\像素译站.exe" "dist\便携版\release-v1.1.0-portable.exe"
Write-Host "  onefile 已移动到: dist\便携版\release-v1.1.0-portable.exe"

# ---------- [4/4] Inno Setup 安装版 ----------
if ($iscc) {
    Write-Host "========== [4/4] Inno Setup 安装版 =========="
    & $iscc scripts\installer.iss
    if ($LASTEXITCODE -ne 0) { throw "Inno Setup 编译失败（退出码 $LASTEXITCODE）" }
    Write-Host "  安装版产物: dist\安装版\release-v1.1.0-setup.exe"
} else {
    Write-Host "========== [4/4] 跳过 Inno Setup（未安装）=========="
}

Write-Host "=============================================="
Write-Host "全部完成！"
Write-Host "  安装版: dist\安装版\release-v1.1.0-setup.exe（需 Inno Setup 6）"
Write-Host "  便携版: dist\便携版\release-v1.1.0-portable.exe"
Write-Host "  onedir:  dist\安装版\像素译站\（安装版源）"
