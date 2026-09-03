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
import time
import socket
import random
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

    # 可重试的 HTTP 状态：限流与网关侧的临时故障。
    #
    # 400/401/403 不重试——请求本身有问题或密钥不对，重试多少次都一样，
    # 只会白白拖长响应时间。**区分「暂时不行」和「根本不行」是重试的
    # 全部意义所在**，不分就成了固定放大三倍延迟。
    RETRYABLE_STATUS = (429, 500, 502, 503, 504)
    MAX_ATTEMPTS = 3
    # 退避基数。429 时服务端往往还在过载，立刻重试只会加剧
    BACKOFF_SECONDS = 0.6

    def _post_with_retry(self, request):
        """发请求，遇到瞬时故障时重试。

        原先任何一次失败都直接降级到纯规则。但**超时与限流是可重试的
        瞬时故障**，一次失败就放弃过于保守——尤其限流，等半秒往往就好了。

        重试仍然有上限：三次之内不成功就老实降级，不能让用户干等。
        """
        last = "未知错误"
        for attempt in range(self.MAX_ATTEMPTS):
            try:
                with urllib.request.urlopen(
                        request, timeout=self._timeout) as response:
                    return json.loads(response.read().decode("utf-8"))
            except urllib.error.HTTPError as exc:
                # 不要把响应体原样抛出，其中可能回显密钥
                last = "大模型接口返回 HTTP %s" % exc.code
                if exc.code not in self.RETRYABLE_STATUS:
                    break
            except socket.timeout:
                last = "大模型接口超时"
            except urllib.error.URLError as exc:
                # 连不上：网络抖动或 DNS 临时失败，值得再试
                last = "大模型接口连接失败：%s" % type(exc.reason).__name__
            except Exception as exc:
                # 解析异常之类不是瞬时故障，重试没有意义
                last = "大模型接口调用失败：%s" % type(exc).__name__
                break

            if attempt < self.MAX_ATTEMPTS - 1:
                # 指数退避：0.6s、1.2s。加一点点随机避免多个请求同时重试
                time.sleep(self.BACKOFF_SECONDS * (2 ** attempt)
                           * (0.8 + random.random() * 0.4))

        raise LLMUnavailable(last)

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
        body = self._post_with_retry(request)

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
