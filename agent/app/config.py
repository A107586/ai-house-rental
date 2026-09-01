"""智能体服务配置。

所有密钥一律从环境变量读取，**任何情况下不写入代码或提交至仓库**
（对应非功能需求 N-05）。本地开发把 `.env.example` 复制为 `.env` 填写，
`.env` 已在 `.gitignore` 中排除。
"""

import os


def _get(name, default=None):
    value = os.environ.get(name)
    return value if value not in (None, "") else default


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
        self.backend_base_url = _get("BACKEND_BASE_URL", "http://localhost:8080")
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

        # ---- 路由阈值 ----
        # 规则解析置信度低于此值才调用大模型兜底
        self.rule_confidence_threshold = _get_float("RULE_CONFIDENCE_THRESHOLD", 0.5)

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
        }


settings = Settings()
