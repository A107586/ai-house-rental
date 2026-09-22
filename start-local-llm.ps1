# 本地模型服务 —— 为本项目的负载调过尺寸
#
# 用法：powershell -ExecutionPolicy Bypass -File .\start-local-llm.ps1
# 起完再跑 start-all.ps1 启动业务后端与智能体。
#
# ---------------------------------------------------------------------------
# 为什么是这套参数（每一项都有实测依据，别随手改）
#
#  -c 16384        本项目实测最长 prompt 约 1,140 token，整轮上下文不到 1 KB。
#                  原先按 262144 配（KVMem 长上下文模式）会预留 10 GiB 内存池，
#                  纯属浪费。实测右尺寸后：整轮平均 3.6s → 3.0s，
#                  最慢 8.2s → 4.2s，显存省 1.6 GB，内存占用 83% → 52%。
#
#  --no-kvmem      KVMem 的分层 KV 是给 20 万 token 上下文用的。本项目用不到，
#                  关掉少一层开销。真要跑长文再开。
#
#  --reasoning-effort none
#                  **这一项必须有。** 模型默认开思考，而工具选择器只给
#                  max_tokens=150；思考会把这 150 全部吃光，content 返回空，
#                  extract_json 失败 → tool="none" → 永远退回规则链路。
#                  最阴险的是 /health 仍显示 healthy=true、failures=0。
#                  实测：思考开着 3 个用例挂 2 个；关掉后 7 个用例中 6 个。
#
#  -ub 512         实测 128/256/512 之间差异在噪声内，512 是 llama-bench 最优。
#
#  -Vision         可选开关。加载视觉头，模型就能看报修照片
#                  （见 agent/app/agents/repair/tools/vision.py）。
#                  这块显存是把嵌入模型挪到 CPU 之后腾出来的。
#                  不加这个开关，纯文字链路一切照旧：识图请求会被引擎
#                  拒绝、`complete_vision` 抛 LLMUnavailable、报修自动
#                  退回纯文字，现有功能一个都不受影响。
#
# **这个文件必须存成带 BOM 的 UTF-8。** Windows PowerShell 5.1 读无 BOM 的
# UTF-8 会按本地代码页解，中文全乱；乱掉的中文一旦落在双引号字符串里，
# 就会凑出一个提前终止的引号——整份脚本连解析都过不去，
# 报的错却是几十行之外的「字符串缺少终止符」。
# ---------------------------------------------------------------------------

param([switch]$Vision)

$ErrorActionPreference = 'Stop'

$Engine = 'D:\kvmem\bin\llama-kvmem-server.exe'
$Model  = 'D:\models\bonsai\Ternary-Bonsai-2-27B-PQ2_0.gguf'
# Q8 的视觉头。BF16 那份大 300 MB，实测识图质量没有可见差别，
# 在 12 GB 卡上不值这个显存。
$MMProj = 'D:\models\bonsai\Ternary-Bonsai-2-27B-mmproj-Q8_0.gguf'

foreach ($p in @($Engine, $Model)) {
    if (-not (Test-Path -LiteralPath $p)) {
        Write-Host "找不到：$p" -ForegroundColor Red
        Write-Host "引擎与权重不在本仓库里，见桌面 Bonsai-LLM\README.md" -ForegroundColor Yellow
        exit 1
    }
}

# 防重入：两份模型同时加载在 12 GB 卡上装不下，
# 结果是显存只剩几十 MB，而两个进程都「看起来正常」。
$running = Get-Process llama-kvmem-server -ErrorAction SilentlyContinue
if ($running) {
    Write-Host '已有模型服务在跑，先停掉…' -ForegroundColor Yellow
    $running | Stop-Process -Force
    Start-Sleep -Seconds 4
}

$env:PATH = 'D:\kvmem\bin;' + $env:PATH

$EngineArgs = @(
    '-m', $Model,
    '--host', '127.0.0.1', '--port', '18240',
    '-c', '16384',
    '-ngl', '99',
    '--kv-dtype', 'q8_0',
    '-ub', '512', '-b', '2048',
    '--no-kvmem',
    '--spec-type', 'none',
    '--reasoning-effort', 'none'
)

if ($Vision) {
    if (-not (Test-Path -LiteralPath $MMProj)) {
        Write-Host "找不到视觉头：$MMProj" -ForegroundColor Red
        exit 1
    }
    $EngineArgs += @('--mmproj', $MMProj)
    Write-Host '已加载视觉头：报修可以直接拍照。' -ForegroundColor Green
}

Write-Host '启动本地模型（约 15 秒）…' -ForegroundColor Cyan
& $Engine @EngineArgs
