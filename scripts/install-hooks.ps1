# ============================================================
# 安装知识库 pre-commit 钩子
# 用法：在仓库根目录执行
#   powershell -ExecutionPolicy Bypass -File scripts/install-hooks.ps1
# ============================================================

$ErrorActionPreference = "Continue"

$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root

Write-Output "仓库根目录: $Root"

# ---------- 1. 定位 uv ----------
$Uv = (Get-Command uv -ErrorAction SilentlyContinue).Source
if (-not $Uv) {
    $Fallback = Join-Path $env:LOCALAPPDATA "hermes\bin\uv.exe"
    if (Test-Path $Fallback) { $Uv = $Fallback }
}
if (-not $Uv) {
    throw "未找到 uv。请先安装 uv，或手动创建 .venv 并安装 pyyaml。"
}

# ---------- 2. 准备虚拟环境 ----------
$VenvDir = Join-Path $Root ".venv"
$VenvPy = Join-Path $VenvDir "Scripts\python.exe"
if (-not (Test-Path $VenvPy)) {
    Write-Output "创建虚拟环境 .venv …"
    if (Test-Path $VenvDir) {
        # 残留目录不是有效 venv 时，uv 的 --clear/--force 都会拒绝，直接移除
        Remove-Item $VenvDir -Recurse -Force
    }
    & $Uv venv $VenvDir --python 3.12 2>&1 | Out-Null
    if (-not (Test-Path $VenvPy)) { throw "虚拟环境创建失败，请手动执行：uv venv $VenvDir --python 3.12" }
}

# ---------- 3. 安装 pyyaml ----------
& $VenvPy -c "import yaml" 2>$null
if ($LASTEXITCODE -ne 0) {
    Write-Output "安装 pyyaml …"
    $env:UV_INDEX_URL = "https://pypi.tuna.tsinghua.edu.cn/simple"
    & $Uv pip install --python $VenvPy pyyaml 2>&1 | Out-Null
    & $VenvPy -c "import yaml"
    if ($LASTEXITCODE -ne 0) { throw "pyyaml 安装失败" }
}
Write-Output "Python 环境就绪: $VenvPy"

# ---------- 4. 安装钩子 ----------
$HookSrc = Join-Path $Root "scripts\pre-commit"
$HookDst = Join-Path $Root ".git\hooks\pre-commit"
if (-not (Test-Path $HookSrc)) { throw "未找到 $HookSrc" }

# 统一转换为 LF 行尾，并按 UTF-8 无 BOM 写出，避免 Git Bash 解析异常
$Content = Get-Content $HookSrc -Raw -Encoding UTF8
$Content = $Content -replace "`r`n", "`n"
$Content = $Content -replace "`r", "`n"
$Utf8NoBom = New-Object System.Text.UTF8Encoding($false)
[System.IO.File]::WriteAllText($HookDst, $Content, $Utf8NoBom)

Write-Output "已安装钩子: $HookDst"

# ---------- 5. 自检 ----------
Write-Output ""
Write-Output "自检：对当前仓库执行一次校验…"
& $VenvPy (Join-Path $Root "kb_validator.py") (Join-Path $Root "cases") (Join-Path $Root "domains")
if ($LASTEXITCODE -ne 0) {
    Write-Output "注意：当前仓库存在未通过项，提交会被钩子阻止，请先修复。"
} else {
    Write-Output "自检通过。"
}
