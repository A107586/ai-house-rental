# NInfer 移植调研 · 交接给下一个人

> 写于 2026-09-23。**这份是调研记录，不是操作手册**——到成文为止一行引擎代码都没编译过。
> 凡是我实测的都标了数字和复跑办法；凡是从文章/文档抄来的都写明了出处；
> 凡是没验证的都明说「没验证」。**看的时候请按这三类区别对待。**

---

## 0. 一句话结论

**换引擎换不出速度，值钱的是投机解码（MTP）。** MTP 在现有引擎上就能开，
权重上游已经拼好；NInfer 是另一条可以并行试的路，它的最大障碍
（要先有一份 `groupwise-int` 模板）**已经找到现成的解**，
剩下的成本是一次 Windows 工具链安装 + 从源码构建。

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

## 4. MTP 这条路（**优先做，成本低**）

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
