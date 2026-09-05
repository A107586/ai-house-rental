"""智能体服务配置。

所有密钥一律从环境变量读取，**任何情况下不写入代码或提交至仓库**
（对应非功能需求 N-05）。本地开发把 `.env.example` 复制为 `.env` 填写，
`.env` 已在 `.gitignore` 中排除。
"""

import os
import sys
from pathlib import Path

from dotenv import load_dotenv


# **跑测试时不读 .env。**
#
# 谁在本机填了大模型密钥，测试就会跟着真的往厂商发请求。实测的表现是
# 整个套件卡死——每条用例几秒到二十几秒，五百多条跑不完；跑完也要计费，
# 而且结论随模型心情变，「同一份代码两次跑出不同结果」的测试等于没有。
#
# 项目一直宣称「测试不依赖网络、数据库与大模型」。在没人填密钥的机器上
# 这话自动成立，但它是**靠运气成立的**。给中转站接上 GPT 之后，
# 第一个炸掉的就是测试套件。
#
# 判据用 `unittest`/`pytest` 在不在 sys.modules 里：`python -m unittest`
# 会先把 unittest 导进来，再由用例去 import app.*；而 uvicorn 起服务时
# 这两个模块都不在。放在**配置层**而不是 tests/__init__.py，是因为
# `unittest discover -s tests` 把用例当顶层模块导入，那个 __init__ 压根不执行——
# 这一点是踩了坑才知道的。
#
# 要测「接上大模型之后」的行为，用假 provider 显式注入
# （见 tests/test_router.py 的 FakeProvider），那样才可复现。
_UNDER_TEST = "unittest" in sys.modules or "pytest" in sys.modules

# 本地开发配置位于 agent/.env。显式指定路径，避免从仓库根目录或
# start-all.ps1 启动时因当前工作目录不同而漏读配置。
if not _UNDER_TEST:
    load_dotenv(Path(__file__).resolve().parents[1] / ".env")


def _get(name, default=None):
    value = os.environ.get(name)
    return value if value not in (None, "") else default


def _get_bool(name, default):
    raw = os.environ.get(name)
    if raw is None or not str(raw).strip():
        return default
    return str(raw).strip().lower() in ("1", "true", "yes", "on")


def _get_int(name, default):
    try:
        return int(_get(name, default))
    except (TypeError, ValueError):
        return default


def _get_float(name, default):
    try:
        return float(_get(name, default))
    except (TypeError, ValueError):
        return default


class Settings:
    """运行期配置，进程启动时读取一次。"""

    def __init__(self):
        # ---- 业务后端 ----
        # 必须写 127.0.0.1 而不是 localhost。Windows 上 localhost 会先解析成
        # IPv6 的 ::1，而后端默认只绑 IPv4；连不上再回退，这一次回退实测
        # 要 2 秒——每一次智能体调后端都白付这 2 秒，找房、预约、报修、
        # 推荐全都被拖慢。显式写 IPv4 就没有这个歧义。
        self.backend_base_url = _get("BACKEND_BASE_URL", "http://127.0.0.1:8080")
        self.backend_timeout = _get_float("BACKEND_TIMEOUT", 10.0)

        # ---- 大模型 ----
        # provider 留空表示不接大模型，服务以纯规则模式运行（降级可用）
        self.llm_provider = _get("LLM_PROVIDER", "")
        self.llm_api_key = _get("LLM_API_KEY", "")
        self.llm_base_url = _get("LLM_BASE_URL", "")
        self.llm_model = _get("LLM_MODEL", "")
        # 30 秒改成 8 秒（K-10）。**用户等 30 秒和等 8 秒后拿到规则结果，
        # 后者体验好得多**——降级路径是完整可用的，不是残次品，
        # 没有理由为了「说不定第 29 秒能回来」让人干等着。
        self.llm_timeout = _get_float("LLM_TIMEOUT", 8.0)
        # 单次请求上下文上限，避免 Token 消耗失控（非功能需求 N-08）
        self.llm_max_tokens = _get_int("LLM_MAX_TOKENS", 1024)

        # ---- 向量嵌入（AI-03 智能客服的可选通道）----
        # 与 LLM_* 是**独立的一组**配置，不能复用：团队的大模型是 DeepSeek，
        # 而 DeepSeek 不提供 embeddings 接口。嵌入走本地部署的模型
        # （LM Studio / Ollama 都实现了 OpenAI 兼容的 /v1/embeddings）。
        #
        # 留空即不启用，客服问答只用词法检索——功能完整，全部测试也跑在这个模式下。
        self.embed_base_url = _get("EMBED_BASE_URL", "")
        self.embed_api_key = _get("EMBED_API_KEY", "")
        self.embed_model = _get("EMBED_MODEL", "")
        self.embed_timeout = _get_float("EMBED_TIMEOUT", 30.0)
        # 语料向量的落盘缓存。按内容哈希做键，语料改了自动失效
        self.embed_cache_path = _get(
            "EMBED_CACHE_PATH",
            str(Path(__file__).resolve().parents[1] / "knowledge" / "vectors.json"))

        # ---- 路由阈值 ----
        # 规则解析置信度低于此值才调用大模型兜底。
        # 融合模式下它同样生效：置信度到线就直接用规则结果，
        # 不再为一次已经确定的解析多付一趟网络往返（K-10）
        self.rule_confidence_threshold = _get_float("RULE_CONFIDENCE_THRESHOLD", 0.5)
        # 规则与大模型是否同时参与。开启时每次请求都会调一次大模型，
        # 由它在规则结论的基础上给出最终结果；关闭则退回纯规则。
        #
        # 需求 N-08 要求「Token 消耗受控」——每请求一次调用确实比
        # 「置信度不足才调」费钱，所以留这个开关：演示或压测时可以关掉，
        # 关掉之后功能仍然完整，只是少了大模型的修正。
        self.fusion_enabled = _get_bool("FUSION_ENABLED", True)

        # 规则置信度够高时跳过大模型（K-10 提速开关）。
        #
        # **默认关闭，因为它推翻的是本模块开头那段刻意的设计决定**：
        # 融合的立场是「规则与大模型同时参与」，理由是规则再全也覆盖不了
        # 所有说法，置信度高只说明规则认得出，不等于它认得对。
        #
        # 但代价是实打实的：融合模式下**每一次请求都要等一趟大模型往返**，
        # 哪怕规则已经百分之百确定。「中山区两室一厅三千以内」这类说法
        # 规则全都认得，为它多付一趟往返，用户感受到的就是「智能体很慢」。
        #
        # 做成开关而不是写死，**默认开启**（团队定的）：
        #   - 默认（true）：规则很确定时直接用规则结论，不等大模型；
        #   - 置 false：回到「规则与大模型永远同时参与」的老行为，
        #     跑 AI-01 验收指标、或怀疑解析变差时用它对照。
        #
        # 取舍要说清楚：置信度高只说明规则**认得出**，不等于它**认得对**，
        # 所以开着它理论上会漏掉一部分「规则认得出但认错了、本可由大模型
        # 纠正」的情况。换来的是这类查询不再多付一趟往返。
        # 阈值 rule_confidence_threshold 控制「多确定才算确定」，
        # 觉得漏判多就把它调高，不必整个关掉。
        #
        # 关掉大模型（不配 LLM_PROVIDER）本来就是纯规则模式，
        # 这个开关只影响「配了大模型」的那条路径。
        self.fusion_skip_when_confident = _get_bool(
            "FUSION_SKIP_WHEN_CONFIDENT", True)

        self.service_port = _get_int("AGENT_PORT", 8000)

    @property
    def llm_enabled(self):
        """是否配置了可用的大模型。未配置时全链路降级为规则模式。"""
        return bool(self.llm_provider and self.llm_api_key)

    def describe(self):
        """用于健康检查，**不得包含任何密钥内容**。"""
        return {
            "backend_base_url": self.backend_base_url,
            "llm_provider": self.llm_provider or "(未配置)",
            "llm_model": self.llm_model or "(未配置)",
            "llm_enabled": self.llm_enabled,
            "mode": "大模型 + 规则" if self.llm_enabled else "纯规则（降级模式）",
            "rule_confidence_threshold": self.rule_confidence_threshold,
            "fusion_enabled": self.fusion_enabled,
            "fusion_skip_when_confident": self.fusion_skip_when_confident,
            "embed_enabled": bool(self.embed_base_url),
            "embed_model": self.embed_model or "(未配置)",
        }


settings = Settings()
