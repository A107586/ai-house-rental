# NInfer 移植调研 · 交接给下一个人

> 写于 2026-09-23，**2026-09-24 按实测大改**。
> 原文是调研记录（成文时一行引擎代码都没编译过）；现在 NInfer 已经跑通了，
> 结论也跟着变了——**§0 和 §4 都被实测推翻过，改动见 §2.5。**
> 凡是实测的都标了数字和复跑办法；凡是从文章/文档抄来的都写明出处；
> 凡是没验证的都明说「没验证」。**看的时候请按这三类区别对待。**

---

## 0. 一句话结论（2026-09-24 重写）

**NInfer 跑通了，MTP 稳定给 ~1.8×；而 KVMem 那条「成本更低」的 MTP 路是断的。**

原结论说「MTP 在现有引擎上就能开，权重上游已经拼好」——**这句是错的，已实测推翻**：
带头权重下完之后 KVMem 拒绝启动（词嵌入表没做逆变换，引擎的 Hadamard 图校验拦下了），
详见 **§2.5.4**。所以现在想要 MTP，**只有 NInfer 这一条路**。

**MTP 在本机稳定给 1.57×（中文）~ 1.69×（英文）**，满时钟下背靠背对测。

关于速度要提醒两句：**t/s 绝对值很容易量错**——同一条命令因显卡时钟状态不同
跑出过 69.5 和 29.0；而并发跑 `nvidia-smi` 采样会把 61.9 干扰成 13.0
（**采样器本身就是污染源**）。可信的是比值和接受率，详见 **§2.5.2**。

---

## 1. 背景：现在跑的是什么

| | |
|---|---|
| 引擎 | KVMem v0.16.0-rc3（llama.cpp 派生），`D:\kvmem\bin\llama-kvmem-server.exe` |
| 权重 | `D:\models\bonsai\Ternary-Bonsai-2-27B-PQ2_0.gguf`，7.206 GB |
| 视觉头 | 同目录 `-mmproj-Q8_0.gguf`，629 MB（`-Vision` 开关加载） |
| 启动 | 仓库根 `start-local-llm.ps1` |
| 卡 | RTX 4080 **Laptop**（AD104，sm_89），12 GB |

本项目的租房智能体通过 `.env` 里的 `LLM_BASE_URL=http://127.0.0.1:18240/v1` 用它。
**长上下文用不到**：`--no-kvmem -c 16384` 已经是默认，实测最长 prompt 约 1,140 token。

---

## 2. ⚠️ 先看这一节：换引擎为什么换不出速度（实测）

这是整份调研最重要的一条，**先看它再决定要不要投入**。

解码是**纯显存带宽瓶颈**。每吐一个 token 要把全部权重读一遍，
所以 `t/s × 权重字节数 = 等效带宽`，而它的天花板是卡的峰值带宽。

| | 本机 4080 Laptop | 文章的 4080 SUPER |
|---|---|---|
| 位宽 × 等效时钟 | 192 bit × 18 Gbps | 256 bit × 23 Gbps |
| **理论峰值带宽** | **432 GB/s** | **736 GB/s** |
| 实测纯解码 | **39–41 t/s** | 62–64 t/s |
| 等效带宽 | **282 GB/s** | 447 GB/s |
| **带宽利用率** | **65%** | **61%** |

**结论：现有引擎在这张卡上已经跑到峰值的 65%，比文章那套在它自己卡上还高一点。**
62 vs 40 的差距**全部来自硬件**，不是引擎。文章里拿来对比的 356 GB/s
是原版 llama.cpp，不是本项目在用的这个引擎。

位宽怎么查的（别信规格站，直接问驱动）：

```python
import ctypes
cuda = ctypes.WinDLL("nvcuda"); cuda.cuInit(0)
dev = ctypes.c_int(); cuda.cuDeviceGet(ctypes.byref(dev), 0)
def attr(n):
    v = ctypes.c_int(); cuda.cuDeviceGetAttribute(ctypes.byref(v), n, dev); return v.value
bus, clk = attr(37), attr(36)          # 37=BUS_WIDTH, 36=MEM_CLOCK(kHz)
print(bus, "bit", clk*2/1e6, "Gbps", clk*1000*2*(bus/8)/1e9, "GB/s")
```

把文章的数按带宽折到本机（`×432/736`）：

| | 文章（SUPER） | 折到本机 |
|---|---|---|
| 纯解码 | 62–64 t/s | **36–38 t/s**（我们已有 39–41） |
| MTP K=2 单流 | 96.7–130.8 t/s | **57–77 t/s** |

**所以唯一值得投入的是投机解码那一项。**

---

## 2.5 ✅ NInfer 已经跑通了（2026-09-24 实测）

**这一节是实测结果，不是调研。** 整条链路在本机（4080 Laptop / sm_89 / 12 GB）跑通：
引擎自建、三元制品打包、四项制品校验、端到端出字、MTP 投机解码全部通过。

### 2.5.1 制品与校验

| 判据 | 论文 | 本机 | |
|---|---|---|---|
| 制品尺寸 | 8,306,927,628 B | **8,306,927,628 B** | ✅ 逐字节相同 |
| 对象数 | 1126 | **1126** | ✅ |
| 文本部分 | 6.696 GiB | **6.696 GiB** | ✅ |
| `pack.py check` | — | **RESULT OK** | 8 个形状 pad 全 0；zero_share 0.3278 |
| `check_assembly.py` | 15/15 | **15/15** | 含 `gdn_value_z` 头级置换 |
| `check_row_order.py` | — | **全部成立** | 重复 0 / 丢失 0 / 顺序错 0 |
| `check_signs.py` | 逐值相同 | **max\|diff\|=0.0** | 宽度前缀和也对 |
| `check_payload_order.py` | row-split | **row-split（A）** | A: neg=0；B: neg=219,927 |
| 端到端出字 | 通顺 | **通顺** | `The capital of France is **Paris**.` |
| PPL | 6.445 | **11.10** | ⚠️ 见下，**不可直接比** |

> **PPL 11.10 只能证明「没坏」，不能和论文的 6.445 比。** 判据是论文自己给的
> 「对了是个位数到十几，错了是几百」（他们坏的时候是 1,128,420）——11.10 属于「对了」。
> 不可比的原因有两个，都会把 PPL 抬高：①**语料不同**（论文用他们自建的
> wikitext/pg19/zhwiki/ninfer 四路 1M 语料，但仓库里 `eval/corpora/perplexity-1m/data/`
> **是空的**，只提交了 manifest 和字节区间，要重建得先把上游数据集下回来切；
> 我用的是一本 Gutenberg 小说，属于 pg19 同域）；②**上下文只有 512**
> （2048 会 `bad allocation`——全窗口 logits 是 2048×248320×4 ≈ 2 GB 一次性分配）。

### 2.5.2 速度 ⚠️ 绝对值全部作废，只有比值和接受率可信

**我踩了论文测量学那节点名的坑，而且是踩完才发现的。** 先看证据——
**同一个二进制、同一个制品、同一条命令，跑两次**：

| | 前一次 | 后一次 |
|---|---|---|
| MTP K=2 decode | 69.5 t/s | **29.0 t/s** |
| 接受率 | 59.6% | **59.6%** |
| tok/轮 | 2.19 | **2.19** |

**吞吐差了 2.4 倍，接受率和 tok/轮一位不差。** 原因是显存/SM 时钟：
后一次采样到 `显存 6001 MHz / 上限 9001`、`SM 1065 / 上限 3105`、`45 W / 上限 150 W`，
利用率 98%——**卡根本没跑起来**（不是温度也不是功耗墙，`nvidia-smi` 的
throttle reason 全是 Not Active，温度才 45–46°C）。

于是本节的数分成两类：

**❌ 不可信（随时钟变，本次会话内变化 2.4 倍）**

| 配置 | 高时钟那次 | 低时钟那次 |
|---|---|---|
| 纯解码 | 39.4–40.0 t/s | 16.1 t/s |
| MTP K=2 | 69.5 t/s | 29.0 t/s |

**✅ 可信（比值 / 比例，与时钟无关，多次复现）**

| 判据 | 值 | 论文 |
|---|---|---|
| **MTP 加速比** | **1.57× ~ 1.80×**（四次独立测到） | — |
| **K=2 接受率（英文）** | **59.6%**（三次一模一样） | 59.4% |
| **K=2 tok/轮（英文）** | **2.19**（三次一模一样） | 2.19 |

**满时钟下背靠背对测（`clocks.mem` 9501 MHz，最干净的一组）：**

| prompt | 无 MTP | 有 MTP | 加速 | 接受率 / tok轮 |
|---|---|---|---|---|
| 英文 | 42.7 t/s | **72.1 t/s** | **1.69×** | 59.6% / 2.19 |
| 中文（本项目租房场景） | 39.3 t/s | **61.9 t/s** | **1.57×** | 44.4% / 1.89 |

> **中文接受率比英文低**（44.4% vs 59.6%），和论文说的「中文短答 80%」对不上。
> 但论文那条指的是「短答」，我这条 prompt 是带法条口吻、引号、数字的结构化建议，
> 不是一类任务。**单条 prompt，不外推。**

**⚠️ 又一个测量坑（这次是我自己造的）**：并发跑 `nvidia-smi` 采样循环会**严重干扰被测进程**——
同一条中文 MTP 命令，带采样器量到 **13.0 t/s**（看起来 MTP 是负收益！），
去掉采样器干净重跑是 **61.9 t/s**。**采样器是我为了让数字可信才加的，结果它污染了数字。**
要采时钟就单独跑一次采、另一次不采着量，别在同一次里既量又采。

**✅ mma 快路径确实在工作（正面对照）**

把作者那条张量核快路径关掉：

```powershell
$env:NINFER_TERNARY_MMA=0   # 同一条中文输入
```

掉到 **4.3 t/s / 24.7 s**——**慢 14 倍**。这证明：
① 补丁里的 `ternary_rowsplit_mma_small_t.cuh` 在我们这个构建里真的接上了；
② 论文 §4.7 说的「没有 mma、验证轮走 SIMT 就是负收益」是对的，而且比他报的还严重。

- **接受率和 tok/轮和论文几乎完全重合**，而且在两种时钟状态下都稳定复现。
  这**解掉了 §5.5 的一条风险**：担心「借 MXFP8-CRACK 这个改过的头会掉接受率」，
  实测没掉。
- **MTP 在本机稳定给 ~1.8×。** 这是本次唯一可以拿出去说的性能结论。
- K=3 / K=4 接受率 49.2% / 40.0%，比论文的 40.9% / 33.2% **衰减更缓**（接受率是比值，
  这条可信）。但 K=3 的 t/s 是不是真比 K=2 高——**不下结论**，那是时钟噪声里的 4%。
- **§2 那张「换引擎换不出速度」的表还不能说被证实**：要证实它得让两个引擎在
  同一时钟状态下对测，而我没做到（见 §2.5.4）。

### 2.5.3 显存（原来最担心的一条）

12 GB 卡装得下，而且不紧张：

```
gpu weights used      6.70 GiB / 6.70 GiB
free after startup    3.66 GiB
kv cache payload      128.0 MiB   (context 2048)
```

### 2.5.4 ★ KVMem + MTP **根本起不来**——§4 那条路是断的

这是本次最重要的发现，**它推翻了 §4**。

7.66 GB 那份带头权重已经下完了（`Ternary-Bonsai-2-27B-PQ2_0-MTP-Q8_0.gguf`，
7,657,489,728 B）。按 §4.3 加 `-Mtp` 起服务，**KVMem 直接拒绝启动**：

```
llama_verify_hadamard_graph: latent lookup 'mtp_tok_embd-64'
                             consumed by op=RMS_NORM name='norm-64'
llama_init_from_model: failed to initialize the context:
    Hadamard-latent table 'token_embd.weight' is read without the inverse transform
common_speculative_init_result: failed to create MTP context
```

**不是配置问题，是这份权重和这个引擎对不上。** 词嵌入表存在旋转基里，
查表后必须施加逆变换（`h = s ⊙ (H·z)`），而这份 GGUF 的 MTP 块把
`mtp_tok_embd-64` 直接喂进 RMS_NORM，没有逆变换。KVMem 有一道 Hadamard 图校验，
**它拦住了并且拒绝启动**——没有输出乱码，这是个好引擎该有的样子。

**对照组（证明不是装坏了）**：同一份权重、同一个引擎，只把 `--spec-type` 换成 `none`，
服务正常起来、正常出字，日志里是
`model has unused tensor blk.64.nextn.* -- ignoring`。
**所以引擎和权重本身都是好的，坏的只有 MTP 那条路。**

后果：

1. **§4「优先做 MTP，成本低」这条路目前是断的。** 上游拼好的那个头，
   KVMem 用不了。
2. NInfer 能用它——因为这个移植**显式实现了旋转/逆变换的接线**，
   而且它借的 MTP 块来自模板、处在原始基（论文 §4.8 的前提）。
3. **所以「NInfer 值不值」这个问题的答案变了**：不是「NInfer 比 KVMem 快多少」，
   而是「**在本机想要 MTP 的 1.8×，目前只有 NInfer 这一条路**」。

⚠️ **仍然没做到的对照**：KVMem 纯解码 vs NInfer 纯解码，**在同一时钟状态下**对测。
本次量到 KVMem 12.2 t/s、NInfer 16.1 t/s，但两者时钟状态不同、
且都远低于 §2 记录的 39–41 t/s，**这组数字不能用来比较**。
要补这个对照，必须两个引擎背靠背连续测、并全程采样 `clocks.mem`。

### 2.5.3b 识图能用（§5.5 那条「没有结论」已经有结论了）

**能用，而且不需要 mmproj。** 制品里自带 333 个 `vision/*`（282 MB，打包时从
Qwen 模板借的），`ninfer-serve --vision` 直接就能看图——两份 mmproj
（Q8_0 629 MB / BF16 931 MB）是 llama.cpp 线的格式，**NInfer 既不需要也挂不上**。

判据是「说出只有看过图才知道的细节」，不是「返回 200」。拿仓库里
`xznstatic/img/19.jpg` 试（白墙上的装饰摆件），模型说出了：卡通白猫的**蓝色领结**、
女孩的**浅黄短发 + 粉色上衣**、布卷的**蓝底白星**、玻璃花瓶的**竖向棱纹**、
**鲜红色浆果**——逐条核对全对，图片也确实吃掉 1,324 个 prompt token。

⚠️ **仍未验证**：真故障照片（漏水/发霉/裂缝）认不认得出。仓库里 14 张全是完好房间，
这正是 `scripts/try_repair_photo.py` 文档里写的那个空白，换了引擎也还在。

### 2.5.3c ★ 思考 vs max_tokens：老坑换了个地方又出现

`start-local-llm.ps1` 里那句 `--reasoning-effort none` 是有血泪的（见它第 17-23 行）：
工具选择器只给 max_tokens=150，思考会把它全吃光、content 返回空，
链路静默退回规则，而 `/health` 还显示 healthy。

**换成开思考的 NInfer 之后，这个坑原样回来了**，只是换到识图上：
`vision.py:63` 给 120、`caption.py:74` 给 80，一思考就返回空。
实测报修链路「照片里读到」那栏一直是空的，引擎日志里是 `output limit`。

但**不能因此就关掉思考**——实测同一个问题「押一付三是什么」，
关思考答错（说成买房首付分期），开思考答对。论文自己的消融也是
low 档 16/20、xhigh 档 19/20。

**解法：按请求关，而不是全局关。** `ninfer-serve` 认 OpenAI 的 `reasoning_effort`：

| 字段 | reasoning_tokens |
|---|---|
| `reasoning_effort: "none"` | **0** ✅ |
| `reasoning_effort: "low"` | 40 |
| `enable_thinking: false` | 0 ✅ |
| 不传（默认） | 50 |
| `thinking: false` / `thinking_budget: 0` | 50（**不认，别用**） |

已经接进项目：`agent/.env` 的 `LLM_VISION_REASONING_EFFORT=none` 只作用于
`complete_vision`，文本路径继续思考。**留空即回到改动前**——默认留空是有意的，
不是每家兼容端点都认这个字段。

### 2.5.4b 东西都在哪 / 怎么再跑一次

```
D:\ninfer\base\              基座树 @6eb70a07 + 三元补丁（已覆盖）
D:\ninfer\patches\           ModelScope 上那个发布包（git clone 下来的）
D:\ninfer\build_sm89\        构建产物：ninfer.exe / ninfer-serve.exe / ninfer-perplexity.exe
D:\ninfer\models\ternary-bonsai-2-27b.ninfer     制品，8,306,927,628 B
D:\ninfer\template\crack.ninfer                  模板，18.2 GB（打包用完就没用了）
D:\ninfer\build_ada.bat      构建脚本（configure + build）
D:\python312\python.exe      跑 pack.py / verify 的解释器（numpy + torch-cpu）
```

**日常用仓库根的 `start-ninfer.ps1`**（开关式，和 `start-local-llm.ps1` 并存，
一次只起一个引擎）：

```powershell
.\start-ninfer.ps1                  # MTP 开、思考开、无识图
.\start-ninfer.ps1 -Vision -Switch  # 加识图，并把智能体切过来（会重启它）
.\start-ninfer.ps1 -NoMtp           # 关投机解码，做对照
.\start-ninfer.ps1 -Restore         # 只把智能体切回 kvmem(18240)
```

下面是脚本底下实际在跑的东西，手动调参时用：

出字（注意 **`--max-context 2048` 起步**，KV 和 workspace 都吃显存）：

```powershell
cd D:\ninfer\build_sm89
.\ninfer.exe D:\ninfer\models\ternary-bonsai-2-27b.ninfer `
  --prompt "..." --max-new 200 --greedy --no-thinking --max-context 4096 `
  --spec mtp --draft-tokens 2
```

**量速度之前先确认显卡时钟**（否则量出来的是时钟，不是引擎，见 §2.5.2）：

```powershell
nvidia-smi --query-gpu=clocks.mem,clocks.sm,power.draw,utilization.gpu --format=csv
```

本机见过 `显存 6001 / 上限 9001 MHz`（只有 67%）。**`clocks.mem` 没贴近上限时量的数直接扔掉。**

### 2.5.5 与论文环境的差异（可能影响复现）

- **MSVC 用的是 14.42（VS2022），不是文档要求的 14.51。** VS2026 BuildTools 装了三次
  都没把编译器载荷拉下来（14.51 目录里只有 props 和一个中文语言包，没有 `cl.exe`）。
  14.42 编译三元代码没报任何问题，所以 14.51 **看起来**不是必需的——但这是一条
  「没验证过等价」的差异，不是「验证过等价」。
- 本机代理会掐大文件传输（curl 多次 `exit 56`，Python MSI 下下来是坏的），
  大文件都得套重试续传循环。

---

## 3. 当前基线（2026-09-23 实测，复跑见 §8）

```
纯解码            40.7 t/s          （输出 200 token）
工具选择单次      0.82s / 27 token / 33.0 t/s   ← 本项目真实负载
整轮对话          中位 1.93s，最慢 2.40s        ← 用户唯一感觉得到的
```

**判据是第三行。** 只报「纯解码翻了几倍」是拿一个用户感觉不到的数字邀功——
本项目一次工具选择只吐 27 个 token，投机解码在这个长度上能不能摊开，
必须看第二、三行。

---

## 4. MTP 这条路（~~优先做，成本低~~ **⛔ 已实测：这条路是断的，见 §2.5.4**）

> **2026-09-24：本节结论作废。** 权重下完了，但 `-Mtp` 起不来——
> KVMem 的 Hadamard 图校验拒绝这份 GGUF 的 MTP 块（`token_embd.weight`
> 没做逆变换）。§4.2「上游已经把头拼好了，我们不用自己拼」这句话对 NInfer 成立，
> **对 KVMem 不成立**。下面的内容保留作记录，别照着做。

### 4.1 引擎本来就支持，缺的是权重

`llama-kvmem-server.exe` 有 `--spec-type draft-mtp`、`--spec-draft-n-max`、
`--spec-draft-p-min`。直接开会得到一句非常明确的拒绝：

```
kvmem_spec_start: MTP draft context is null
  (GGUF missing nextn / qwen35.nextn_predict_layers?)
llama_init_from_model: context type MTP requested but model doesn't contain MTP layers
```

本体 GGUF 确实没有：**851 张量、零个 `blk.64.*`**（和 `pack.py` 头注释里说的一致）。

### 4.2 上游已经把头拼好了

`ProCreations/Ternary-Bonsai-2-27B-MTP` → `Ternary-Bonsai-2-27B-PQ2_0-MTP-Q8_0.gguf`，
**7.657 GB**（HF 与 ModelScope 都有；国内走 ModelScope 快）。

**用 HTTP Range 只读头部几百 KB 验完的**（§7 有可复用的做法）：

```
866 张量（比本体多 15 个）
qwen35.nextn_predict_layers = 1        ← 引擎点名要的那个键
qwen35.block_count = 65                ← 64 + NextN 那一层
blk.64.nextn.eh_proj (10240,5120) / enorm / hnorm / shared_head_norm
blk.64.attn_{q,k,v,output} / ffn_{gate,up,down} / 各 norm
```

**同一个 PQ2_0 本体 + Q8_0 的 NextN 层。** 文章里最费劲的「拼头」
（作者说"决定成败"）上游做完了，我们不用自己拼。

### 4.3 已经做好的接入

`start-local-llm.ps1` 加了 `-Mtp`：换成带头的权重 +
`--spec-type draft-mtp --spec-draft-n-max 2`。

- **N=2 不是拍的**：原文章实测 N=3/4/5 接受率掉到 40.9 / 33.2 / 22.9%；
- **两份权重并存，KVMem 一字未动**——不加 `-Mtp` 完全走原路，随时对照；
- 权重不在时打印的是一行可以直接粘的下载命令，不是报错栈。

```powershell
.\start-local-llm.ps1 -Mtp -Vision     # 带 MTP，同时挂视觉头
.\start-local-llm.ps1 -Vision          # 原样
```

### 4.4 还没做的

- **`-Mtp -Vision` 一起开装不装得下没验过。** 本体 + NextN + mmproj
  粗估 ≈ 10.8 GB / 12.0 GB，紧。挤不下时要做取舍，把两边的数摆出来再定。
- 接受率没量过。文章报 K=2 约 59.4%（英文代码 40.8% → 中文短答 80%），
  而本项目的负载正好偏中文短答，**理论上是它最占便宜的一档**。

---

## 5. NInfer 这条路

### 5.1 依赖链（照这个来，别猜）

```
NInfer 引擎（源码构建，sm_89）
  └─ 基座树  github.com/Ambolio/ninfer-4090-windows  @ 6eb70a07（v1.0.8 线）
     └─ 覆盖  shensanshu/ninfer-ada-ternary 的 patches/changed-files/*
              （覆盖后会多出 src/ops/linear/ternary/，基座树原本没有）
  ⚠️ 上游总仓 github.com/Neroued/ninfer 是 Linux/5090/sm_120a，**不是这条线**

tools/pack.py（把三元 GGUF 打成 .ninfer）
  ├─ --gguf      我们现有的 Ternary-Bonsai-2-27B-PQ2_0.gguf        ✅ 有
  └─ --template  一份 groupwise-int 的 qwen3.8-27b .ninfer 制品     ← 原本的大障碍
```

### 5.2 ★ 最大的发现：模板不用自己转

作者给的产模板办法是先下 **Qwen3.8-27B（55.6 GB）** + DFlash2，
再跑 NInfer 自带转换器（文档要求**物理内存 ≥64 GB**，本机只有 31.7 GB）。

**但 HF 上已经有现成的合规模板：**

```
WaveCut/Qwen3.8-27B-MXFP8-CRACK-NInfer  →  crack.ninfer   18.21 GB（单文件）
```

我**只读了它前 8 MB 的清单**就把文档给的两条自检都过了
（`.ninfer` 格式 = 跳过 16 字节 + 一段 JSON manifest，见 `pack.py: load_template`）：

```python
req = urllib.request.Request(URL, headers={"Range": "bytes=16-8388623"})
obj, _ = json.JSONDecoder().raw_decode(
    urllib.request.urlopen(req).read().decode("utf-8", "replace"))
# obj["identity"], obj["objects"]
```

结果：

| 自检 | 结果 |
|---|---|
| ① `identity.weights_id == "groupwise-int"` | ✅ 正是 `groupwise-int` |
| ② `text/layers/3/` 下是 `attention/query_key` + `attention/gate_value` **两个** | ✅ 两个分开的对象（融合成 `query_key_gate_value` 就是 nvfp4，用不了） |
| 对象总数 | 1124 |
| 借用载荷 | `mtp/` 12、`vision/` 333、`frontend/` 6、`text/draft_head` 2 — **和文档逐项对上** |
| text layers | 64 |

**55.6 GB + 一次要 64 GB 内存的转换  →  下一个 18.2 GB 的文件。**

另一份 `Avalonec/Qwen3.8-27B-QUASAR-All-NVFP4-...-NInfer`（19.8 GB）
文档明确说 **nvfp4 用不了**（投影被融合，名字不在映射表里，一进来就
`unmapped gdn object` 中止），已排除。它附带一个 `ninfer-serve.exe`（0.2 GB），
**没验过能不能在 sm_89 上跑**——如果能，也许连构建都省了，值得花十分钟试一下。

> **2026-09-23 已验证：这条路是死路。** 下载了 `ninfer-serve.exe`（203 MB），
> 用 PE 导入表（`objdump` 不在手，改用 Python 手解 PE 头）拿到完整依赖清单：
> `libcurl.dll`、`avcodec-62.dll`、`avformat-62.dll`、`avutil-60.dll`、`swscale-9.dll`
> （其余是标准 VC++ 运行时/Windows API，不缺）。
> `libcurl.dll` 用 Git for Windows 自带的 `libcurl-4.dll` 改名能顶上，
> 但 FFmpeg 四件套锁定的是一个**特定快照版本**——BtbN 当前 `latest` 构建已经是
> `avcodec-63 / avformat-63 / avutil-61 / swscale-10`，文件名直接对不上，
> 得去翻历史 release 才能凑出 62/62/60/9 这组合。**没有继续追，判定为死路**，
> 花了十分钟验证符合文档原本的预期（"值得试，不行就走构建"）。

### 5.3 本机与要求的差距

| 要求（docs/01） | 本机 | |
|---|---|---|
| CUDA **13.3.33**（旁装，不覆盖系统） | 没装 | 要装 |
| MSVC **14.51.36231**（VS **18** BuildTools） | 只有 VS **2022**（=17） | 要装 |
| Ninja **1.13.2** | 没有 | 要装 |
| Python **3.12** + torch + numpy | 项目 venv 是 3.14、无 torch | 要另建 |
| 物理内存 ≥ 64 GB | **31.7 GB** | ⚠️ 见下 |
| 显卡 sm_89 | 4080 Laptop = AD104 = **sm_89** | ✅ |
| 磁盘 | D 盘剩 345 GB | ✅ |

> **内存那条不一定卡。** 文档写的是「转换大模型时占内存」，而我们用现成模板
> **跳过了转换**；`pack.py` 自称流式写。**但没验证过**，别当结论。

**装 CUDA / VS BuildTools / Ninja 是对开发机的实质改动**，动手前要本人点头。

### 5.4 文档里两个「用血换」的坑（构建时一定会遇到）

抄录自 `docs/03`、`docs/04`，**不是我踩的**，但值得先知道：

1. **激活布局**：NInfer 的 Tensor 是 `ne[0]` 连续（token 主序 `t*K+k`），
   而旋转内核与 GEMM 按行主序索引。**T=1 时两种排布完全重合**
   ⇒ 只测 T=1 的解码/oracle 永远发现不了，prefill 全乱。
   判据：引擎侧注入式 dump，`T=1 cos +0.999997 ✓ / T=55 cos +0.006 ❌`。
   **必须至少有一个 T>1 用例。**
2. **打包器 `gdn_value_z` 头级置换漏 ×128**：`perm48` 套在 6144 行上而不是
   48 头上 ⇒ 非双射 ⇒ 重复行 + 丢失行。该 bug **守恒一切可数之物**
   （尺寸、行数、字节总数、往返无损全绿）。判据只能是**行级指纹的多重集比对**。

> 这两条共同的教训：**「搬运无损」类判据对偏移/置换错误完全盲。**
> 这和本项目自己踩过的「单元测试把真正会执行的那几行整个绕开」是同一类病。

另外 `docs/01` 记了一个 Windows 特有的坑：这棵树的 **MSVC 头文件依赖扫描是坏的**，
改了 `.h` 之后 ninja 说「没活干」，改动静默不生效，跨 TU 结构体尺寸不一致
⇒ 运行期 `/GS` 栈金丝雀崩 `0xC0000409`。**改 `.h` 后必须 touch 所有 include 它的 `.cpp`。**

### 5.4b 发布包缺两块，得自己补（2026-09-23 实测）

照 §5.1 把基座树 + 补丁搭起来之后，**发布包少了两样东西**，都不是可选的：

1. **`_ternary_ref.py` 没有。** `verify/check_row_order.py` 和 `check_assembly.py` 都
   `from _ternary_ref import Gguf`，但包里没这个文件——它原本在作者自己的引擎树
   （`E:\ninfer\src\ninfer-4090w-ternary`）。需要的接口很小：
   `Gguf(path).tensors[name] -> (ne, tt, off)` 和 `.raw(name, rows) -> bytes`。
   **应该包在 `pack.py` 自己的 GGUF 解析器外面，不要另写一个**——这两个脚本验的就是
   打包器的行序，必须和打包器用同一双眼睛读源文件；而偏移读错这件事，
   `pack.py` 里作者自己的注释专门说了它会「静默且极具说服力地」骗过你。

2. **`tools/artifact/` 的三元格式注册没有。** 补丁的 `changed-files/` 只有
   `include/` 和 `src/`（C++ 引擎侧），但 `pack.py` 把 `NINFER_ROOT` 指向作者
   **打过补丁的引擎树**再 `from tools.artifact import ...`——也就是说 Python 侧的
   artifact 库他也改了，只是没放进包里。不补的话第一步就死在
   `ValueError: unknown numeric format: 'PQ2_0_G128'`。

   要改三处（都在基座树 `tools/artifact/`）：
   - `numeric.py`：加格式类型并注册 `PQ2_0_G128` / `PTQ1_0_G128`；
   - `layouts.py`：`ROW_SPLIT_K128_V1` 的 formats 集合加这两个名字；
     `row_split_geometry()` 认这个类型；`encoded_size()` 的 row-split 分支放行。

   **几何不能从 bit 数推。** 现成公式是「低平面半字节 + 高平面溢出」：
   `base = group//2`、`high = group*(bits-4)/8`，对 group=128 怎么取 bits 都凑不出
   三元要的 32+0（PQ2_0）和 24+2（PTQ1_0）。按引擎 `ternary_row_view.h` 的口径
   **把每组字节数直接写死**，别去发明一个能凑出总数的分数 bit 宽度。

   > 判据：`pack.py check` 的 CHECK 1 里 **8 个形状组合 pad 全是 0**
   > （`payload == src`，三平面正好 34 B/组、无对齐余量）。几何错了这里会立刻差出来。

**这一条会连累 `git status`**：§6 第 3 步说「覆盖补丁后 `git status` 应当只显示补丁里那些文件」，
补完上面第 2 项之后基座树会**多出 `tools/artifact/numeric.py`、`layouts.py` 两个改动**。
这是有意的，不是装错了。

### 5.5 没解决的风险（做之前先想清楚）

- **模板是 `MXFP8-CRACK`——一个改过的变体**，而我们会从它借 `mtp/*` 那 12 个张量。
  文档要求 MTP 头是「Qwen3.8-27B 自带的 NextN 单层，与本体同族、**同一份**」。
  借一个改过的头，**接受率可能掉**，没验过。
- **`frontend/*`（分词器）也是借的。** 我们这份词表是 248,320，
  得确认和模板一致，否则就是"形状全对、输出乱码"的另一种版本。
- **识图。** 我一开始说「NInfer 会没有识图」——**说错了**，模板里有 333 个
  `vision/*`，打包器会借过来。但引擎在文本解码时读不读它，
  `pack.py` 头注释写的是「the template's tower ... is **not read during text decode**」，
  **能不能真的走视觉推理，我没有结论。**
- 文章的 PPL 6.445、20 题 16/20 是作者在 SUPER 上的数，**本机没复现过**。

---

## 6. 建议的推进顺序

1. **先把 MTP 量完**（权重下载中，7.66 GB）。跑
   `scripts/bench_mtp.py off` / `on` / `compare`，拿到**整轮延迟**的对照。
2. 如果 MTP 就把整轮从 1.93s 压到 1.3s 左右，**NInfer 的边际价值会掉很多**——
   它要装一整套工具链、从源码构建、并且面对 §5.4 那两个坑。
3. 真要做 NInfer，建议这个次序（每步都可独立验证，别一路盲走）：
   1. 先试 `Avalonec` 那个 `ninfer-serve.exe` 能不能在 sm_89 上启动
      （十分钟，成了能省掉整个构建）；
   2. 下 `crack.ninfer`（18.2 GB），本地再跑一遍 §5.2 的两条自检；
   3. clone 基座树 + 覆盖补丁，`git status` 应当**只**显示补丁里那些文件；
   4. 装工具链 → 构建 → 先跑基座树自带的制品，确认引擎本身是好的；
   5. 最后才跑 `pack.py check`（几何 + 解码 + 字节往返三个证明，不写文件），
      过了再 `pack.py build`。

**任何一步失败都不要往下走**——文章作者的两个 bug 都是"前面全绿、后面全乱"。

---

## 7. 可复用的手艺：先读头，再决定下不下

这次省掉了两次大下载（7.66 GB 的 GGUF、18.2 GB 的 .ninfer），
靠的都是同一招：**HTTP Range 只取文件头，把格式解出来验一遍**。

- **GGUF**：`magic/version/n_tensors/n_kv` 各 4/4/8/8 字节，然后是 KV 区
  （字符串是「8 字节长度 + 内容」），再是张量信息表（名字、维度、量化类型、偏移）。
  一兆一兆地取就够读完整个头。
- **`.ninfer`**：跳过 16 字节，剩下是 JSON manifest（`pack.py: load_template` 读 8 MB）。

两份现成脚本已经放进仓库：`scripts/dl_mtp.py`（断点续传下载）、
`scripts/bench_mtp.py`（对照测量）。

---

## 8. 怎么复跑本文的数字

```powershell
# 先把权重下下来（7.66 GB，断了能接着下）
python scripts\dl_mtp.py

# 基线（MTP 关）
.\start-local-llm.ps1 -Vision
python scriptsench_mtp.py off

# MTP 开
.\start-local-llm.ps1 -Mtp -Vision
python scriptsench_mtp.py on

python scriptsench_mtp.py compare
```

三个服务（8080 后端 / 8000 智能体 / 18240 模型）都要起着——
第三项量的是整轮对话，它要打真实链路。

`bench_mtp.py` 量三样并按「离用户有多近」排序输出，判据是**整轮中位秒**。
它还会从 `D:\kvmem\*.log` 里捡和投机解码有关的行（接受率是旁证，不是判据）。

---

## 9. 相关文件在哪

| 东西 | 位置 |
|---|---|
| 启动脚本（`-Vision` / `-Mtp`） | 仓库根 `start-local-llm.ps1` |
| 智能体怎么连模型 | `agent/.env`、`agent/.env.example`（有实测注释） |
| 识图接入（报修 / 图片说明） | `agent/app/images.py`、`agent/app/agents/repair/tools/vision.py`、`agent/app/agents/describe/tools/caption.py` |
| 拿自己的照片试识图 | `scripts/try_repair_photo.py` |
| 下 MTP 权重 / 对照测量 | `scripts/dl_mtp.py`、`scripts/bench_mtp.py` |
| NInfer 的文档与工具 | 没进仓库（不是本项目的代码）。要用时按 §9 末尾那个地址重新取：`docs/01` `03` `04`、`README.md`、`tools/pack.py`、`tools/MAPPING.json` |
| 原始出处 | `modelscope.cn/models/shensanshu/ninfer-ada-ternary` |
