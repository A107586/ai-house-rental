# -*- coding: utf-8 -*-
"""一轮对话的大模型时间预算。

## 为什么单次超时不够

`llm_timeout` 管的是**一次连接**，`total_budget` 管的是**一次调用含重试**。
两个都设好之后，实测「退租要提前多久」仍然等了 43.9 秒——

因为**一轮对话会调好几次模型**：工具选择一次、路由一次、组织回答一次。
每一次都各自守着自己的预算，加起来就是三倍。

    K-10 把超时从 30 秒压到 8 秒，理由写得很清楚：
    「用户等 30 秒和等 8 秒后拿到规则结果，后者体验好得多」。
    可这条约束是按**单次调用**写的，而用户感觉到的是**这一句话等了多久**。

所以这里再包一层：给整轮对话一个墙钟截止时间，过了就一律报
`LLMUnavailable`。所有子智能体本来就都能在模型不可用时降级到规则——
**这条降级路径是完整可用的，不是残次品**，让它提前接手比让用户干等好。

## 为什么用包装而不是全局变量

`provider` 是模块级的单例，多个请求共用。把截止时间写进它里面，
两个并发请求会互相踩。包一层薄的、每轮新建一个，天然是请求隔离的，
而且下游一行都不用改——它们收的仍然是一个 `LLMProvider`。
"""

import inspect
import time

from app.llm.base import LLMProvider, LLMUnavailable


def _accepts_deadline(provider):
    """这个实现的 `complete` 收不收 `deadline`。看不出来就当不收。"""
    try:
        params = inspect.signature(provider.complete).parameters
    except (TypeError, ValueError):
        return False
    return "deadline" in params or any(
        p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values())


class DeadlineProvider(LLMProvider):
    """给一个 provider 套上墙钟截止时间。

    到点之后所有调用直接抛 `LLMUnavailable`，**不发请求**——
    发出去也来不及用，只是白花一次额度。
    """

    def __init__(self, inner, deadline):
        self._inner = inner
        self._deadline = deadline
        # 内层实现认不认 `deadline`，**开一次签名就知道了**。
        #
        # 原来是 try / except TypeError——那会把内层自己抛的 TypeError
        # 也一起吞掉，然后不带 deadline 地重发一次同样的请求：
        # 一个真 bug 被伪装成「重试一次」，还多花一次额度。
        self._takes_deadline = _accepts_deadline(inner)

    @property
    def name(self):
        return getattr(self._inner, "name", "budgeted")

    @property
    def available(self):
        # **过期之后要如实报 unavailable。** 上层普遍用
        # `provider.available` 决定要不要走模型那条路；报 True
        # 会让它先走过去再撞一个异常，白绕一圈
        return bool(getattr(self._inner, "available", False)) and self.left > 0

    @property
    def left(self):
        return self._deadline - time.time()

    def _check(self):
        if self.left <= 0:
            raise LLMUnavailable("本轮对话的大模型时间已用完，改用规则结果")

    def _call(self, method, system_prompt, user_prompt, max_tokens):
        """把本轮截止时间一起传下去。

        **光在这里挡住「还发不发下一次」是不够的**：已经发出去的那一次
        仍然按它自己的超时在等。实测一轮 20 秒的预算，最后一次调用
        照样把总时长顶到 27 秒——因为预算只剩 3 秒时，
        它还是按 8 秒等，重试起来更久。

        不认识 `deadline` 的实现（测试里的假 provider、第三方实现）
        照旧调用，只是拦不住超时，不会出错。
        """
        self._check()
        if self._takes_deadline:
            return method(system_prompt, user_prompt, max_tokens=max_tokens,
                          deadline=self._deadline)
        return method(system_prompt, user_prompt, max_tokens=max_tokens)

    def complete(self, system_prompt, user_prompt, max_tokens=None,
                 deadline=None):
        return self._call(self._inner.complete, system_prompt, user_prompt,
                          max_tokens)

    def complete_json(self, system_prompt, user_prompt, max_tokens=None,
                      deadline=None):
        return self._call(self._inner.complete_json, system_prompt,
                          user_prompt, max_tokens)

    # **这一层之前只转发了上面两个方法。** 没转发的会落到基类的默认实现：
    # complete_vision 在基类里直接抛「当前大模型没有识图能力」——
    # 所以聊天里带着照片报修时模型永远「看不见」照片，而单独打
    # /agent/repair 却看得见（那条路不套这层壳）。表现是同一张图两处结果不一样。
    def complete_vision(self, system_prompt, user_prompt, images,
                        max_tokens=None, deadline=None):
        self._check()
        method = getattr(self._inner, "complete_vision")
        if self._takes_deadline:
            return method(system_prompt, user_prompt, images,
                          max_tokens=max_tokens, deadline=self._deadline)
        return method(system_prompt, user_prompt, images, max_tokens=max_tokens)

    def chat(self, system_prompt, messages, max_tokens=None, deadline=None,
             temperature=0.0):
        self._check()
        if self._takes_deadline:
            return self._inner.chat(system_prompt, messages,
                                    max_tokens=max_tokens,
                                    deadline=self._deadline,
                                    temperature=temperature)
        return self._inner.chat(system_prompt, messages, max_tokens=max_tokens,
                                temperature=temperature)

    def stats(self):
        return self._inner.stats()


def for_turn(provider, seconds):
    """给这一轮包一个预算。`seconds` 不是正数时原样返回，不加限制。

    **套在一个已经有预算的 provider 外面是安全的**：里层那个仍然
    照自己的截止时间拦，两个取严的那个生效。子预算就是这么实现的——
    工具选择器拿一个 8 秒的壳，套在整轮 20 秒的壳外面。
    """
    if not provider or not seconds or seconds <= 0:
        return provider
    return DeadlineProvider(provider, time.time() + seconds)
