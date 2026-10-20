# ============================================================
#  一键启动：打开本地控制面板 http://127.0.0.1:8099，并在后台起全部服务
#  （业务后端 8080、智能体 8000、前端网站 5173；智能体用本机模型时还有模型 18241、模型对话页 8090）。
#  没有黑窗口：启停、看日志、选大模型、填高德 Key 都在面板上。逻辑全在 launcher\panel.py。
#
#  用法：双击 start-everything.bat，或
#    powershell -ExecutionPolicy Bypass -File .\start-everything.ps1
#  面板已经开着时再运行，只会重新打开那个网页。
#  注意：本文件必须保存为 UTF-8 with BOM。
# ============================================================

$root = $PSScriptRoot
$pyw = Join-Path $root '.venv\Scripts\pythonw.exe'
if (-not (Test-Path $pyw)) { $pyw = 'pythonw' }
Start-Process -FilePath $pyw -ArgumentList @("`"$root\launcher\panel.py`"", '--autostart')
Write-Host '控制面板已在后台启动，浏览器会打开 http://127.0.0.1:8099' -ForegroundColor Green
