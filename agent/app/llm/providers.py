"""大模型厂商实现。

目前提供一个兼容 OpenAI Chat Completions 协议的通用实现——
国内主流厂商（通义千问、DeepSeek、智谱、Kimi、豆包等）均提供
该协议的兼容端点，因此换厂商通常只需改 `.env` 里的三个变量：

    LLM_PROVIDER=openai_compatible
    LLM_BASE_URL=<厂商的兼容端点>
    LLM_MODEL=<模型名>

若某厂商只提供私有协议，再新增一个 `LLMProvider` 子类即可，
上层的智能体代码无需改动。
"""

import json
import urllib.error
import urllib.request

from app.llm.base import LLMProvider, LLMUnavailable, NullProvider


class OpenAICompatibleProvider(LLMProvider):
    """兼容 OpenAI Chat Completions 协议的通用实现。

    只用标准库 `urllib`，不引入厂商 SDK，避免依赖膨胀和版本冲突。
    """

    name = "openai_compatible"

    def __init__(self, api_key, base_url, model, timeout=30.0, max_tokens=1024):
        if not api_key:
            raise ValueError("api_key 不能为空")
        self._api_key = api_key
        self._base_url = (base_url or "https://api.openai.com/v1").rstrip("/")
        self._model = model or "gpt-4o-mini"
        self._timeout = timeout
        self._max_tokens = max_tokens

    def complete(self, system_prompt, user_prompt, max_tokens=None):
        payload = {
            "model": self._model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            # 解析类任务需要稳定输出，温度压到最低
            "temperature": 0.0,
            "max_tokens": max_tokens or self._max_tokens,
        }
        request = urllib.request.Request(
            self._base_url + "/chat/completions",
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": "Bearer " + self._api_key,
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self._timeout) as response:
                body = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            # 不要把响应体原样抛出，其中可能回显密钥
            raise LLMUnavailable("大模型接口返回 HTTP %s" % exc.code)
        except Exception as exc:
            raise LLMUnavailable("大模型接口调用失败：%s" % type(exc).__name__)

        try:
            return body["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError):
            raise LLMUnavailable("大模型返回结构异常")


def build_provider(settings):
    """按配置构造 provider；未配置或配置错误一律回退到 `NullProvider`。

    这里刻意不抛异常：缺少密钥属于预期情况，服务应当以降级模式正常启动。
    """
    if not settings.llm_enabled:
        return NullProvider()

    provider = settings.llm_provider.strip().lower()
    if provider in ("openai_compatible", "openai", "qwen", "deepseek", "glm", "kimi"):
        try:
            return OpenAICompatibleProvider(
                api_key=settings.llm_api_key,
                base_url=settings.llm_base_url,
                model=settings.llm_model,
                timeout=settings.llm_timeout,
                max_tokens=settings.llm_max_tokens,
            )
        except ValueError:
            return NullProvider()
    return NullProvider()
