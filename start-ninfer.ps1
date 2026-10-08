# 本地模型服务（NInfer 线）—— 与 start-local-llm.ps1 并存，一次只能起一个
#
# 用法：powershell -ExecutionPolicy Bypass -File .\start-ninfer.ps1 [开关]
#
#   .\start-ninfer.ps1                     # 默认：极速档（DFlash2）、思考开、无识图
#   .\start-ninfer.ps1 -Vision             # 加识图
#   .\start-ninfer.ps1 -Long               # 长文档档（MTP，上下文大一倍多）
#   .\start-ninfer.ps1 -NoMtp              # 关投机解码（对照用）
#   .\start-ninfer.ps1 -NoThinking         # 关思考（换速度，会变笨）
#   .\start-ninfer.ps1 -Vision -Switch     # 起完顺便把智能体切过来
#   .\start-ninfer.ps1 -Restore            # 只把智能体切回 kvmem(18240)，不起服务
#   .\start-ninfer.ps1 -Legacy             # 旧引擎 D:\bonsai-ninfer（对照/回退用）
#
# ---------------------------------------------------------------------------
# 这条线是什么
#
# 和 start-local-llm.ps1 跑的是**同一个模型的两种制品**：
#   kvmem  : Ternary-Bonsai-2-27B-PQ2_0.gguf        7.21 GB，端口 18240
#   NInfer : bonsai2_27b_ternary_v2(.ninfer)        8.31 GB，端口 18241
#
# 2026-09-28 起默认用 D:\pkg 的新部署（「极速档 PQ2」包 + A3 补丁，按本机 58 SM
# 重编，另修了一个引擎 bug）。全部读数与依据在 D:\pkg\DEPLOY-NOTES.md，要点：
#   极速档  DFlash2 K=7：代码 ~200 t/s、带思考的中英混合 ~160 t/s；中文散文 ~63，
#           和 MTP 一样（草稿模型猜不中中文，这是内容性质，不是配置）
#   长文档  MTP K=4：代码 ~122 t/s，上下文大
#   KV     必须 bf16：包里默认的 rk4v4 / fp8 会把答案算错（46×78 答成 2,588）
#   上下文 极速档 10k；长文档档 32k（带识图也是 32k）。极速档 + 识图放不下，
#           自动改用长文档档。-WddmBudget 能翻倍，但默认不开，原因见「档位」一节
# 旧引擎（自建，基座树 Ambolio/ninfer-4090-windows + 三元补丁）留在
# D:\bonsai-ninfer，-Legacy 用它。移植过程见 docs/NInfer移植调研.md。
#
# 为什么值得有这条线（2026-09-24 实测）
#
#  MTP    满时钟下背靠背对测：英文 42.7 → 72.1 t/s（1.69×）、
#         中文 39.3 → 61.9 t/s（1.57×），真实路由负载接受率 64.6~66.7%。
#         **而 kvmem 这边的 MTP 是起不来的**——带 NextN 头的那份 GGUF 会被
#         引擎的 Hadamard 图校验拒绝（词嵌入表没做逆变换）。
#         所以想要投机解码的加速，目前只有这条线。
#
#  识图   制品里自带 333 个 vision/* 张量（282 MB，打包时从 Qwen 模板借的），
#         **不需要也不能挂 mmproj**——那是 llama.cpp 线的格式。
#         实测能准确描述照片细节；但「真故障照片认不认得出」仍未验证，
#         理由见 scripts/try_repair_photo.py 的文档。
#
# ⚠️ 思考与 max_tokens 的冲突（这条最容易踩）
#
#  start-local-llm.ps1 里那句 `--reasoning-effort none` 是有来历的：
#  工具选择器只给 max_tokens=150，思考会把它全吃光、content 返回空，
#  链路静默退回规则，而 /health 还显示 healthy。
#
#  本脚本默认**开**思考（关掉会明显变笨，实测「押一付三」关思考答错、
#  开思考答对）。代价是同一个坑会回来：识图调用 max_tokens=120
#  （vision.py:63）、图片说明 80（caption.py:74），一思考就返回空。
#
#  解法是**按请求关**：ninfer-serve 认 OpenAI 的 `reasoning_effort` 字段，
#  实测 "none" 能让那一次调用 reasoning_tokens=0。已经接好了——
#  agent/.env 里的 `LLM_VISION_REASONING_EFFORT=none` 只作用于这两条路径，
#  文本路径继续思考。清空它就回到改动前的行为。
#
# **这个文件必须存成带 BOM 的 UTF-8**，理由同 start-local-llm.ps1 文末那段。
# ---------------------------------------------------------------------------

param(
    [switch]$Vision,
    [switch]$Long,
    [switch]$NoMtp,
    [switch]$NoThinking,
    # 0 = 按档位自动：DFlash2 取 7、MTP 取 4（实测最优；MTP 引擎只收 1~5）。
    [int]$DraftTokens = 0,
    [int]$ThinkingBudget = 512,
    # 0 = 按档位 + 显存自动取能起得来的最大值（见文件头「显存」）。引擎在显存
    # 规划阶段就会拒绝放不下的值（FATAL ... runtime reservation），不会运行中爆。
    [int]$Context = 0,
    [switch]$Switch,
    [switch]$Restore,
    [switch]$Legacy,
    # 按整卡显存预算（--wddm-evictable-budget），默认关。为什么见下面「档位」一节。
    [switch]$WddmBudget
)

$ErrorActionPreference = 'Stop'

# 引擎与制品都在 D:\bonsai-ninfer 这一个自包含目录里（见那里的 README.txt）：
#   bin\     运行需要的全部 exe 与 dll
#   model\   制品
#   tools\   打包器 / 校验脚本 / 专用 python
#   _build\  只有重建才用得到（源码树、编译产物、18 GB 模板）
#
# 新部署在 D:\pkg（见 DEPLOY-NOTES.md）：
#   build-89\apps\             自编引擎（exe + 7 个 FFmpeg dll）
#   pkg1-fast-pq2\model\       基座制品 + DFlash2 制品（多一支草稿分支，9.10 GiB）
if ($Legacy) {
    $NInferRoot = 'D:\bonsai-ninfer'
    $Engine   = Join-Path $NInferRoot 'bin\ninfer-serve.exe'
    $Artifact = Join-Path $NInferRoot 'model\ternary-bonsai-2-27b.ninfer'
} else {
    $NInferRoot = 'D:\pkg'
    $Engine   = Join-Path $NInferRoot 'build-89\apps\ninfer-serve.exe'
    $Artifact = Join-Path $NInferRoot 'pkg1-fast-pq2\model\bonsai2_27b_ternary_v2.ninfer'
    $ArtifactDFlash = Join-Path $NInferRoot 'pkg1-fast-pq2\model\bonsai2_27b_ternary_v2-dflash2.ninfer'
}
$Port     = 18241
$KvmemPort = 18240
$EnvFile  = Join-Path $PSScriptRoot 'agent\.env'

# --- -Restore：只改回指向，不起任何服务 -----------------------------------
function Set-AgentBaseUrl {
    param([int]$TargetPort)
    if (-not (Test-Path -LiteralPath $EnvFile)) {
        Write-Host "找不到 $EnvFile，跳过切换。" -ForegroundColor Yellow
        return $false
    }
    $text = Get-Content -LiteralPath $EnvFile -Raw -Encoding UTF8
    $new = [regex]::Replace(
        $text,
        'LLM_BASE_URL\s*=\s*http://127\.0\.0\.1:\d+/v1',
        "LLM_BASE_URL=http://127.0.0.1:$TargetPort/v1")
    if ($new -eq $text) {
        Write-Host "agent\.env 里没找到本地 LLM_BASE_URL，没动它。" -ForegroundColor Yellow
        return $false
    }
    Set-Content -LiteralPath $EnvFile -Value $new -Encoding UTF8 -NoNewline
    Write-Host "agent\.env 已指向 127.0.0.1:$TargetPort" -ForegroundColor Green
    return $true
}

# 改完必须**整进程重启**智能体，不能靠 uvicorn --reload。
# config.py 调 load_dotenv 时没传 override=True，而 python-dotenv 默认
# 不覆盖 os.environ 里已有的值——reload 时旧的 LLM_BASE_URL 还在，
# 新值永远读不进来，表现为「改了没反应，还在连旧端口」。
#
# **还得把整棵进程树杀干净。** Windows 上 `uvicorn --reload` 是三层：
# 重载器 → uvicorn 进程 → multiprocessing 拉起的工作进程（命令行是
# `-c "from multiprocessing.spawn import spawn_main; spawn_main(parent_pid=N ...)"`，
# 里面没有 uvicorn 字样）。只杀端口持有者和它的直接子进程，工作进程会变成孤儿，
# 继续攥着继承来的监听句柄；uvicorn 开了端口复用，新进程照样能绑上——
# 于是 8000 上同时挂着新旧好几个，请求随机落到旧的那个上。
# 实测就是这么撞上的：改完代码、重启完，复测结果和改之前一字不差。
function Get-AgentTree {
    $all = Get-CimInstance Win32_Process -Filter "Name='python.exe'" -ErrorAction SilentlyContinue
    $roots = @($all | Where-Object { $_.CommandLine -match 'uvicorn app\.main:app' -and $_.CommandLine -match '--port 8000' } |
               ForEach-Object { [int]$_.ProcessId })
    $roots += @(Get-NetTCPConnection -LocalPort 8000 -State Listen -ErrorAction SilentlyContinue |
                Select-Object -ExpandProperty OwningProcess)
    $tree = @{}
    foreach ($id in $roots) { $tree[[int]$id] = $true }
    # 反复扩一圈，直到找不到新成员：按父进程号，也按 spawn_main(parent_pid=N)
    # ——父进程已经没了的孤儿，只能靠后者认出来
    do {
        $grew = $false
        foreach ($p in $all) {
            $id = [int]$p.ProcessId
            if ($tree.ContainsKey($id)) { continue }
            $parent = [int]$p.ParentProcessId
            $spawned = $null
            if ($p.CommandLine -match 'parent_pid=(\d+)') { $spawned = [int]$Matches[1] }
            if ($tree.ContainsKey($parent) -or ($spawned -and $tree.ContainsKey($spawned))) {
                $tree[$id] = $true
                $grew = $true
            }
        }
    } while ($grew)
    return @($tree.Keys)
}

function Restart-Agent {
    $pids = Get-AgentTree
    if (-not $pids) {
        Write-Host '智能体没在跑（8000 无监听），跳过重启。' -ForegroundColor Yellow
        Write-Host '起它：powershell -File .\start-all.ps1' -ForegroundColor DarkGray
        return
    }
    foreach ($procid in $pids) {
        Stop-Process -Id $procid -Force -ErrorAction SilentlyContinue
    }
    # 等端口真的空出来再起新的，最多 10 秒
    for ($i = 0; $i -lt 20; $i++) {
        if (-not (Get-NetTCPConnection -LocalPort 8000 -State Listen -ErrorAction SilentlyContinue)) { break }
        Start-Sleep -Milliseconds 500
    }
    if (Get-NetTCPConnection -LocalPort 8000 -State Listen -ErrorAction SilentlyContinue) {
        Write-Host '8000 端口还被占着，没敢起新的智能体——不然新旧会同时在答。' -ForegroundColor Red
        netstat -ano | findstr ':8000 ' | findstr LISTENING
        return
    }
    $py = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
    $agentDir = Join-Path $PSScriptRoot 'agent'
    Start-Process -FilePath 'powershell.exe' -WindowStyle Hidden -ArgumentList @(
        '-NoProfile', '-Command',
        "cd '$agentDir'; & '$py' -m uvicorn app.main:app --reload --host 0.0.0.0 --port 8000")
    Write-Host '智能体已重启（整进程，不是热重载）。' -ForegroundColor Green
}

if ($Restore) {
    [void](Set-AgentBaseUrl -TargetPort $KvmemPort)
    Restart-Agent
    Write-Host ''
    Write-Host '已切回 kvmem 线。记得起它：' -ForegroundColor Cyan
    Write-Host '  powershell -File .\start-local-llm.ps1 -Vision' -ForegroundColor DarkGray
    exit 0
}

foreach ($p in @($Engine, $Artifact) + @($ArtifactDFlash | Where-Object { $_ })) {
    if (-not (Test-Path -LiteralPath $p)) {
        Write-Host "找不到：$p" -ForegroundColor Red
        Write-Host '引擎与制品不在本仓库里（8.3 GB，不进 git）。' -ForegroundColor Yellow
        Write-Host "应该在 $NInferRoot，重建步骤见那里的 README.txt" -ForegroundColor Yellow
        Write-Host '以及 docs/NInfer移植调研.md 的 2.5.4b 一节。' -ForegroundColor Yellow
        exit 1
    }
}

# 防重入：12 GB 卡一次只装得下一个引擎。两个都停，包括另一条线的 kvmem，
# 否则会出现「两个进程都看起来正常、显存只剩几十 MB」那种怪事。
foreach ($name in @('ninfer-serve', 'llama-kvmem-server')) {
    $running = Get-Process $name -ErrorAction SilentlyContinue
    if ($running) {
        Write-Host "已有 $name 在跑，先停掉…" -ForegroundColor Yellow
        $running | Stop-Process -Force
        Start-Sleep -Seconds 4
    }
}

# --- 档位 ------------------------------------------------------------------------
# 极速档 = DFlash2 制品 + --spec dflash2；长文档档 = 基座制品 + MTP。旧引擎只有 MTP。
$Spec = if ($NoMtp) { 'none' } elseif ($Legacy -or $Long) { 'mtp' } else { 'dflash2' }

# --wddm-evictable-budget 让引擎按整卡显存做预算（而不是 WDDM 给单进程的那份），
# 上下文能翻倍。**默认不开，要用加 -WddmBudget。**
#
# 2026-09-29 实测踩坑：桌面切到核显后开着它，压测一切正常；可真跑项目时 dwm 仍在
# 独显上占 1.8 GiB、NVIDIA Overlay 占 1.6 GiB，挤不走——被 WDDM 挤进内存的反而是
# 引擎自己（引擎的「共享 GPU 内存」9.2 GiB，其中 ~5.7 GiB 是本该在显存里的数据）。
# 结果 decode 从 ~210 掉到 29 t/s、prefill 从 ~1,700 掉到 ~650，智能体一轮要半分钟。
# 不开这个开关时引擎按启动那一刻真实可用的显存规划，不会把自己挤出去。
$WddmBudget = [bool]$WddmBudget -and -not $Legacy
if ($Spec -eq 'dflash2' -and $Vision -and -not $WddmBudget) {
    Write-Host '极速档 + 识图在 12 GB 卡上只剩 6k 上下文，改用长文档档（MTP，32k）。' -ForegroundColor Yellow
    $Spec = 'mtp'
}

if ($DraftTokens -eq 0) {
    $DraftTokens = if ($Spec -eq 'dflash2') { 7 } elseif ($Legacy) { 2 } else { 4 }
}

if ($Context -eq 0) {
    # 都是 bf16 KV 下实测能起来的值（D:\pkg\DEPLOY-NOTES.md）。
    # 不开 wddm：极速档 10k；长文档档 32k，带识图也是 32k（剩 283 MiB）。
    $Context = if ($Legacy) { 16384 }
        elseif ($Spec -eq 'dflash2') { if ($WddmBudget) { if ($Vision) { 12288 } else { 20480 } } else { 10240 } }
        else { if ($WddmBudget) { if ($Vision) { 40960 } else { 49152 } } else { 32768 } }
}

$ModelFile = if ($Spec -eq 'dflash2') { $ArtifactDFlash } else { $Artifact }
$EngineArgs = @(
    $ModelFile,
    '--host', '127.0.0.1', '--port', "$Port",
    '--max-context', "$Context",
    '--model-id', 'Ternary-Bonsai-2-27B-PQ2_0.gguf',
    # 智能体按 .env 里的 LLM_MODEL 发 model 字段，这里对齐，省得对不上。
    '--tolerant-tool-calls'
    # 工具调用的包装/后缀偶尔会脏，引擎能捞回来。本项目大量依赖工具选择，
    # 这个开关是净收益。
)

if (-not $Legacy) {
    # KV 必须 bf16：rk4v4 / fp8 在这个模型上会翻转贪心答案（DEPLOY-NOTES.md）。
    $EngineArgs += @('--kv-capacity', "$Context", '--kv-dtype', 'bf16')
    if ($WddmBudget) {
        # 主机 KV 在这个模式下 2 GB 就是顶：3 GB 起 cudaMallocHost 失败——锁页内存和显存
        # 共用同一个 WDDM 额度。给桌面合成的预留保持引擎默认 512 MiB：dwm 实际占到
        # 1.8 GiB，再往下收只会挤得更狠。
        Write-Host '  -WddmBudget：按整卡显存预算。先关掉 NVIDIA Overlay 等占独显的程序，' -ForegroundColor Yellow
        Write-Host '  否则引擎自己会被挤进内存、慢好几倍（看任务管理器里它的「共享 GPU 内存」）。' -ForegroundColor Yellow
        $EngineArgs += @('--wddm-evictable-budget', '--host-kv-mib', '2048')
    }
    # A3（prefill 把 token 维铺进 grid.y，+17~18%，输出逐位不变）和 int8 档起点 24
    # （31~39 token 的 prefill 快 3~13%）。两个都在 CUDA 图捕获时读死，必须起服前设。
    $env:NINFER_TERNARY_TOKEN_GRID    = '1'
    $env:NINFER_TERNARY_S8_MIN_TOKENS = '24'
}

# 不能用 switch 语句：它会给自动变量 $switch 赋枚举器，而本脚本有个 [switch]$Switch
# 参数（变量名不分大小写），类型约束一撞就报「无法转换为 SwitchParameter」。
if ($Spec -eq 'none') {
    Write-Host '投机解码关（对照模式）。' -ForegroundColor Yellow
} elseif ($Spec -eq 'mtp') {
    $EngineArgs += @('--spec', 'mtp', '--draft-tokens', "$DraftTokens")
    Write-Host "长文档档：MTP，draft-tokens=$DraftTokens，上下文 $Context。" -ForegroundColor Green
} else {
    # --lm-head-draft 对 DFlash2 是必需的：不加引擎启动就报 linear_topk 头配置不支持。
    $EngineArgs += @('--spec', 'dflash2', '--draft-tokens', "$DraftTokens", '--lm-head-draft')
    Write-Host "极速档：DFlash2，draft-tokens=$DraftTokens，上下文 $Context。" -ForegroundColor Green
}

if ($NoThinking) {
    $EngineArgs += @('--no-thinking')
    Write-Host '思考关：快，但开放性问题会明显变笨。' -ForegroundColor Yellow
} else {
    $EngineArgs += @('--default-thinking-budget', "$ThinkingBudget", '--default-max-tokens', '2048')
    Write-Host "思考开，预算 $ThinkingBudget token。" -ForegroundColor Green
    Write-Host '  识图/图片说明已单独关思考（agent/.env 的 LLM_VISION_REASONING_EFFORT）。' -ForegroundColor DarkGray
}

if ($Vision) {
    $EngineArgs += @('--vision')
    Write-Host '识图开：用制品自带的 vision tower（282 MB，不需要 mmproj）。' -ForegroundColor Green
}

Write-Host ''
Write-Host "启动 NInfer（约 10 秒）… 端口 $Port  引擎 $Engine" -ForegroundColor Cyan
Write-Host "显存参考：权重 7.12 GiB（DFlash2 制品 9.10），+识图约 0.5；上下文按剩余显存取满。" -ForegroundColor DarkGray
Write-Host ''

if ($Switch) {
    # 先把指向改好再起服务：服务起来的那一刻智能体就是通的。
    [void](Set-AgentBaseUrl -TargetPort $Port)
    Restart-Agent
    Write-Host ''
}

& $Engine @EngineArgs
