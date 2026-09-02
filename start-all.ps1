# ============================================================
#  一键启动：业务后端(8080) + 智能体(8000) + 前端网站(5173)
#  用法：powershell -ExecutionPolicy Bypass -File .\start-all.ps1
#  停止：关掉弹出的三个窗口
#  注意：本文件必须保存为 UTF-8 with BOM。
# ============================================================

$root = $PSScriptRoot

# ---------- 找 Python ----------
$python = $null
foreach ($candidate in @("python", "py")) {
    try {
        & $candidate --version 2>&1 | Out-Null
        if ($LASTEXITCODE -eq 0) { $python = $candidate; break }
    } catch { }
}
if (-not $python) {
    Write-Host "找不到 python，请安装 Python 3.10+ 并加入 PATH。" -ForegroundColor Red
    exit 1
}

# ---------- 找 npm ----------
# 刚装完 Node 的终端里 PATH 还没刷新，所以除了 PATH 还要去常见安装位置找
$npm = $null
try {
    & npm --version 2>&1 | Out-Null
    if ($LASTEXITCODE -eq 0) { $npm = "npm" }
} catch { }

if (-not $npm) {
    $guesses = @(
        "$env:ProgramFiles\nodejs\npm.cmd",
        "${env:ProgramFiles(x86)}\nodejs\npm.cmd",
        "$env:LOCALAPPDATA\Programs\nodejs\npm.cmd",
        "$env:APPDATA\npm\npm.cmd"
    )
    foreach ($guess in $guesses) {
        if (Test-Path $guess) { $npm = $guess; break }
    }
}

if ($npm -and $npm -ne "npm") {
    Write-Host "npm 不在当前终端的 PATH 里，已自动定位到：$npm" -ForegroundColor DarkYellow
    Write-Host "（这是因为本终端在安装 Node 之前就打开了。开个新终端就正常了。）" -ForegroundColor DarkGray
    # 把 node 目录补进本次会话的 PATH，子进程才能用
    $env:PATH = (Split-Path $npm) + ";" + $env:PATH
}

# ---------- 检查后端依赖 ----------
# 只探测最容易被漏掉的那个：python-multipart 是后加的依赖，
# 组员拉了新代码却没重装依赖时，图片上传会退化成 503。
& $python -c "import multipart" 2>&1 | Out-Null
if ($LASTEXITCODE -ne 0) {
    Write-Host "正在安装后端依赖 ..." -ForegroundColor Cyan
    & $python -m pip install -r (Join-Path $root "server\requirements.txt")
}

# ---------- 首次运行自动建库 ----------
if (-not (Test-Path (Join-Path $root "server\rental.db"))) {
    Write-Host "首次运行，正在生成演示数据 ..." -ForegroundColor Cyan
    Push-Location (Join-Path $root "server")
    & $python seed.py
    Pop-Location
}

# ---------- 首次运行自动装前端依赖 ----------
if ($npm -and -not (Test-Path (Join-Path $root "web\node_modules"))) {
    Write-Host "正在安装前端依赖（约 30 秒）..." -ForegroundColor Cyan
    Push-Location (Join-Path $root "web")
    & $npm install --no-audit --no-fund
    Pop-Location
}

Write-Host "启动业务后端 http://localhost:8080 ..." -ForegroundColor Cyan
Start-Process powershell -ArgumentList @(
    "-NoExit", "-Command",
    "cd '$root\server'; $python -m uvicorn app.main:app --reload --port 8080"
)
Start-Sleep -Seconds 3

Write-Host "启动智能体服务 http://localhost:8000 ..." -ForegroundColor Cyan
Start-Process powershell -ArgumentList @(
    "-NoExit", "-Command",
    "cd '$root\agent'; $python -m uvicorn app.main:app --reload --port 8000"
)
Start-Sleep -Seconds 3

if ($npm) {
    Write-Host "启动前端网站 http://localhost:5173 ..." -ForegroundColor Cyan
    Start-Process powershell -ArgumentList @(
        "-NoExit", "-Command",
        "cd '$root\web'; & '$npm' run dev"
    )
    Start-Sleep -Seconds 5
} else {
    Write-Host "找不到 npm，前端未启动。请安装 Node.js 16 后重试。" -ForegroundColor Red
}

Write-Host ""
Write-Host "==================================================" -ForegroundColor Green
Write-Host "  网站入口    http://localhost:5173" -ForegroundColor Green
Write-Host "==================================================" -ForegroundColor Green
Write-Host ""
Write-Host "  业务后端接口文档   http://localhost:8080/docs" -ForegroundColor DarkGray
Write-Host "  智能体接口文档     http://localhost:8000/docs" -ForegroundColor DarkGray
Write-Host ""
Write-Host "  演示账号  tenant1/test123  landlord1/test123  admin/admin123" -ForegroundColor DarkGray
Write-Host "  测试步骤见 docs\测试说明.md" -ForegroundColor DarkGray
