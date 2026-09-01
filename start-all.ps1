# ============================================================
#  一键启动：业务后端(8080) + 智能体服务(8000)
#  用法：powershell -ExecutionPolicy Bypass -File .\start-all.ps1
#  停止：关掉弹出的两个窗口，或按 Ctrl+C
#  注意：本文件必须保存为 UTF-8 with BOM。
# ============================================================

$root = $PSScriptRoot
$python = "python"

# 找一个可用的 Python
try { & $python --version | Out-Null } catch {
    Write-Host "找不到 python 命令，请先安装 Python 3.10+ 并加入 PATH。" -ForegroundColor Red
    exit 1
}

# 首次运行自动建库
if (-not (Test-Path (Join-Path $root "server\rental.db"))) {
    Write-Host "首次运行，正在生成演示数据 ..." -ForegroundColor Cyan
    Push-Location (Join-Path $root "server")
    & $python seed.py
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
Write-Host ""
Write-Host "已启动：" -ForegroundColor Green
Write-Host "  业务后端接口文档   http://localhost:8080/docs"
Write-Host "  智能体服务接口文档 http://localhost:8000/docs"
Write-Host ""
Write-Host "测试步骤见 docs\测试说明.md" -ForegroundColor DarkGray
