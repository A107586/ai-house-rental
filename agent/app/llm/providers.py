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

    def __init__(self, api_key, base_url, model, timeout=30.0, max_tokens=1024,
                 total_budget=None):
        if not api_key:
            raise ValueError("api_key 不能为空")
        self._api_key = api_key
        self._base_url = (base_url or "https://api.openai.com/v1").rstrip("/")
        self._model = model or "gpt-4o-mini"
        self._timeout = timeout
        # **一次调用最多占用多久，含全部重试。**
        #
        # 只有 `timeout` 是不够的：它管的是单次连接，而重试三次
        # 加上退避，最坏是 8 + 0.6 + 8 + 1.2 + 8 ≈ 26 秒。
        # 实测「水电费谁交」就等了 25.97 秒——**配置里写着 8 秒，
        # 用户等了 26 秒**，而 K-10 把超时从 30 秒压到 8 秒，
        # 图的正是「宁可拿规则结果也别干等」。
        #
        # 默认给单次超时的两倍：限流那种失败得快的仍然重试得了三次
        # （429 是立刻返回的，几乎不花时间），而真超时最多再试一次。
        self._total_budget = total_budget or (timeout * 2)
        self._max_tokens = max_tokens
        # 调用统计。进程内存着就够——要的是「现在通不通」，不是历史报表
        self._calls = 0
        self._failures = 0
        self._last_error = ""

        # ---- 熔断 ----
        #
        # 中转站会**间歇性挂死**：同一句话有时 4.6 秒返回，有时挂满超时。
        # 挂的时候每个请求都要先等满 7 秒才降级，而规则本来就能答——
        # 于是所有人都为同一个坑各付一次 7 秒。
        #
        # 熔断就是「有人已经踩过了，后面的人别再踩」：连挂两次就停一分钟，
        # 这一分钟里直接走规则，**不发请求、不等待**。任何一次成功立刻恢复。
        #
        # 两次而不是一次：单次超时可能只是一个慢请求，为它停掉整条链路
        # 太急躁；连着两次就不是偶然了。
        self._consecutive_timeouts = 0
        self._circuit_until = 0.0
        self._short_circuited = 0

    # 连续超时多少次就熔断，以及熔断多久。
    CIRCUIT_AFTER = 2
    CIRCUIT_SECONDS = 60.0

    # 可重试的 HTTP 状态：限流与网关侧的临时故障。
    #
    # 400/401/403 不重试——请求本身有问题或密钥不对，重试多少次都一样，
    # 只会白白拖长响应时间。**区分「暂时不行」和「根本不行」是重试的
    # 全部意义所在**，不分就成了固定放大三倍延迟。
    RETRYABLE_STATUS = (429, 500, 502, 503, 504)
    MAX_ATTEMPTS = 3
    # 退避基数。429 时服务端往往还在过载，立刻重试只会加剧
    BACKOFF_SECONDS = 0.6

    def stats(self):
        """这个进程里大模型调用的成败情况。

        **配置上了不等于调得通。** 实测撞过一次：中转站把某个模型下线，
        每次调用返回 503，系统按设计静默退回规则——功能全在，
        只是模型没参与，而 `/health` 还写着「大模型 + 规则」。
        排查花的时间远超写这几行。所以把成败摆出来。
        """
        open_for = max(0.0, self._circuit_until - time.time())
        return {
            "calls": self._calls,
            "failures": self._failures,
            "last_error": self._last_error,
            # 熔断期间被挡下的次数。**和 failures 分开**——
            # 它们连请求都没发，混在一起会出现「调用 2 次、失败 34 次」
            # 这种自相矛盾的数字，看的人第一反应是监控坏了
            "short_circuited": self._short_circuited,
            "circuit_open": open_for > 0,
            "circuit_reopen_in": round(open_for, 1) if open_for else 0,
            # 一次都没调过时是**未知**，不是「不健康」。
            # 报 False 会让人以为出了问题，其实只是还没人问过话。
            "healthy": None if self._calls == 0
                       else self._failures < self._calls,
        }

    def _post_with_retry(self, request, deadline=None):
        """发请求，遇到瞬时故障时重试。

        原先任何一次失败都直接降级到纯规则。但**超时与限流是可重试的
        瞬时故障**，一次失败就放弃过于保守——尤其限流，等半秒往往就好了。

        重试仍然有上限：三次之内不成功就老实降级，不能让用户干等。
        """
        # 熔断打开期间直接认输，不发请求。**这是「快速失败」不是「失败」**——
        # 调用方收到 LLMUnavailable 会退回规则，而规则答得出来，还快得多。
        if time.time() < self._circuit_until:
            # **单独计数，不算进 failures。**
            #
            # 熔断拒绝不是一次失败的调用——它连请求都没发。混进 failures
            # 会让 /health 出现「调用 2 次、失败 34 次」这种自相矛盾的数字，
            # 看的人第一反应是监控坏了，而不是上游坏了。
            self._short_circuited += 1
            raise LLMUnavailable(
                "大模型连续超时，已暂时熔断（%.0f 秒后重试）"
                % (self._circuit_until - time.time()))

        last = "未知错误"
        self._calls += 1
        # 两个截止时间取更早的那个：自己的调用预算，和调用方给的
        # 「这一轮还剩多少」。**只守自己的那个是不够的**——
        # 一轮对话会调好几次，前面几次花掉的时间这里看不见，
        # 于是最后一次仍然会把总时长顶穿。
        own = time.time() + self._total_budget
        deadline = min(own, deadline) if deadline else own
        for attempt in range(self.MAX_ATTEMPTS):
            # 剩下的预算不够再发一次就收手。**这里比较的是「还剩多少」
            # 而不是「试了几次」**——三次快速失败该允许，
            # 三次各等满八秒不该
            remaining = deadline - time.time()
            if remaining <= 0:
                last = "大模型调用超出总时限（%.0fs）" % self._total_budget
                break
            try:
                # 每次连接的超时都压到「剩余预算」以内，第一次也不例外：
                # 调用方可能只剩 3 秒了，这时候还按 8 秒等，
                # 预算就成了摆设
                with urllib.request.urlopen(
                        request,
                        timeout=max(min(self._timeout, remaining), 0.5)
                        ) as response:
                    # 成功一次就把熔断计数清零。**恢复不需要等满那一分钟**——
                    # 上游好了就该立刻用起来
                    self._consecutive_timeouts = 0
                    self._circuit_until = 0.0
                    return json.loads(response.read().decode("utf-8"))
            except urllib.error.HTTPError as exc:
                # 不要把响应体原样抛出，其中可能回显密钥
                last = "大模型接口返回 HTTP %s" % exc.code
                if exc.code not in self.RETRYABLE_STATUS:
                    break
            except socket.timeout:
                # **超时不重试。**
                #
                # 重试是给「瞬时故障」用的：限流、网关抖动，等半秒就好了。
                # 而这里的超时不是抖动，是中转站**挂住不回**——实测同一句话
                # 有时 4.6 秒返回、有时挂满 60 秒，再发一次多半还是挂。
                #
                # 重试它的代价是实打实的：一次挂死烧掉 8 秒，重试再烧 8 秒，
                # 16 秒的预算见底，这一轮只能降级到规则。而规则**本来就能
                # 答对**——用户白等了 16 秒，拿到的是同一个答案。
                # 实测八句话的中位耗时 20.5 秒，全卡在这上面。
                #
                # 所以超时直接收手，把时间还给后面真正要答题的那几步。
                last = "大模型接口超时，不再重试"
                self._consecutive_timeouts += 1
                if self._consecutive_timeouts >= self.CIRCUIT_AFTER:
                    self._circuit_until = time.time() + self.CIRCUIT_SECONDS
                break
            except urllib.error.URLError as exc:
                # 连不上：网络抖动或 DNS 临时失败，值得再试
                last = "大模型接口连接失败：%s" % type(exc.reason).__name__
            except Exception as exc:
                # 解析异常之类不是瞬时故障，重试没有意义
                last = "大模型接口调用失败：%s" % type(exc).__name__
                break

            if attempt < self.MAX_ATTEMPTS - 1:
                # 指数退避：0.6s、1.2s。加一点点随机避免多个请求同时重试。
                # 退避本身也要算进预算——不算的话「等着退避」会把
                # 剩余时间耗光，下一次尝试直接被判超时
                nap = (self.BACKOFF_SECONDS * (2 ** attempt)
                       * (0.8 + random.random() * 0.4))
                time.sleep(min(nap, max(deadline - time.time(), 0)))

        self._failures += 1
        self._last_error = last
        raise LLMUnavailable(last)

    def complete_vision(self, system_prompt, user_prompt, images,
                        max_tokens=None, deadline=None):
        """带图片的补全，走 OpenAI 多模态消息格式。

        和 `complete` 共用 `_post_with_retry`，所以熔断、重试、预算、
        统计这几样一个不少——识图请求比纯文本慢一个数量级，
        正是最需要这些护栏的那条路径。

        图片放在文字前面，跟 OpenAI 文档里的示例顺序一致。
        **没有实测过两种顺序的差别**，只是没有理由跟主流用法不一样。
        """
        content = [{"type": "image_url", "image_url": {"url": url}}
                   for url in images if url]
        if not content:
            raise LLMUnavailable("没有给出任何图片")
        content.append({"type": "text", "text": user_prompt})
        payload = {
            "model": self._model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": content},
            ],
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
        body = self._post_with_retry(request, deadline=deadline)
        try:
            return body["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError):
            raise LLMUnavailable("大模型返回结构异常")

    def complete(self, system_prompt, user_prompt, max_tokens=None,
                 deadline=None):
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
        body = self._post_with_retry(request, deadline=deadline)

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
                total_budget=getattr(settings, "llm_total_budget", None),
                timeout=settings.llm_timeout,
                max_tokens=settings.llm_max_tokens,
            )
        except ValueError:
            return NullProvider()
    return NullProvider()
