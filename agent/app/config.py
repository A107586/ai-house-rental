"""智能体服务配置。

所有密钥一律从环境变量读取，**任何情况下不写入代码或提交至仓库**
（对应非功能需求 N-05）。本地开发把 `.env.example` 复制为 `.env` 填写，
`.env` 已在 `.gitignore` 中排除。
"""

import os
from pathlib import Path

from dotenv import load_dotenv


# 本地开发配置位于 agent/.env。显式指定路径，避免从仓库根目录或
# start-all.ps1 启动时因当前工作目录不同而漏读配置。
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
        self.llm_timeout = _get_float("LLM_TIMEOUT", 30.0)
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
        # 规则解析置信度低于此值才调用大模型兜底
        self.rule_confidence_threshold = _get_float("RULE_CONFIDENCE_THRESHOLD", 0.5)
        # 规则与大模型是否同时参与。开启时每次请求都会调一次大模型，
        # 由它在规则结论的基础上给出最终结果；关闭则退回纯规则。
        #
        # 需求 N-08 要求「Token 消耗受控」——每请求一次调用确实比
        # 「置信度不足才调」费钱，所以留这个开关：演示或压测时可以关掉，
        # 关掉之后功能仍然完整，只是少了大模型的修正。
        self.fusion_enabled = _get_bool("FUSION_ENABLED", True)

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
            "embed_enabled": bool(self.embed_base_url),
            "embed_model": self.embed_model or "(未配置)",
        }


settings = Settings()
