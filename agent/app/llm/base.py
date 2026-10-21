"""大模型访问的抽象层。

需求文档待定事项 9.2 明确「不绑定具体厂商，接口层做抽象以便替换」，
因此上层代码只依赖 `LLMProvider` 这个接口，换厂商只需新增一个实现类。

`NullProvider` 是未配置密钥时的空实现：不抛异常，而是返回「不可用」，
由调用方降级到规则逻辑（非功能需求 N-07）。
"""

import json
import re


class LLMUnavailable(Exception):
    """大模型不可用。调用方应捕获它并降级，而不是把错误抛给用户。"""


class LLMProvider(object):
    """所有厂商实现的公共接口。"""

    name = "base"

    def complete(self, system_prompt, user_prompt, max_tokens=None,
                 deadline=None):
        """返回模型输出的纯文本。不可用时抛 `LLMUnavailable`。

        `deadline` 是调用方给的墙钟截止时间（`time.time()` 口径）。
        实现**应当**把连接超时压到它以内——一轮对话会调好几次模型，
        只守自己的那份预算，最后一次照样能把总时长顶穿。
        可以忽略它，那样只是拦不住超时，不会出错。
        """
        raise NotImplementedError

    def complete_vision(self, system_prompt, user_prompt, images,
                        max_tokens=None, deadline=None):
        """带图片的补全。`images` 是 data URI 或 http 地址的列表。

        **默认实现直接判不可用，而不是 NotImplementedError。**
        识图是后加的能力，不是每个厂商、每个本地模型都带视觉头；
        测试里那些只实现了 `complete` 的假 provider 也是完全合法的实现。
        抛 `LLMUnavailable` 的话调用方照常降级，抛 NotImplementedError
        就会一路冒到用户面前。
        """
        raise LLMUnavailable("当前大模型没有识图能力")

    def chat(self, system_prompt, messages, max_tokens=None, deadline=None,
             temperature=0.0):
        """多轮消息的补全。

        默认实现把对话压平成一段文字交给 `complete`——只实现了 `complete`
        的厂商和测试里的假 provider 因此都能直接用，只是模型看到的是
        转述而不是原生的多轮格式。
        """
        lines = []
        for m in messages or []:
            who = "用户" if m.get("role") == "user" else "助手"
            lines.append("%s：%s" % (who, m.get("content") or ""))
        kwargs = {"max_tokens": max_tokens}
        if deadline is not None:
            kwargs["deadline"] = deadline
        return self.complete(system_prompt, "\n".join(lines), **kwargs)

    def chat_stream(self, system_prompt, messages, max_tokens=None,
                    deadline=None, temperature=0.0):
        """`chat` 的流式版本：边生成边一段段吐出正文（生成器）。

        默认实现不流式——调一次 `chat`，把整段当作唯一的一段吐出来。
        这样不支持流式的厂商、测试里的假 provider 都能直接用，调用方
        不用分两套写法；只是用户要等整段生成完才看到字。
        """
        kwargs = {"max_tokens": max_tokens, "temperature": temperature}
        if deadline is not None:
            kwargs["deadline"] = deadline
        yield self.chat(system_prompt, messages, **kwargs)

    def complete_json(self, system_prompt, user_prompt, max_tokens=None,
                      deadline=None):
        """要求模型输出 JSON，并解析为 dict。

        模型经常在 JSON 外面裹一层 ```json 代码块或解释文字，
        这里统一做容错提取，避免每个调用点重复处理。
        """
        # **只有真的有截止时间才把这个参数传下去。**
        #
        # `deadline` 是后加的可选参数，而 `LLMProvider` 是对外承诺的
        # 接口——已经写好的实现（包括测试里的假 provider）签名里没有它。
        # 无条件传的话，那些实现全部 TypeError；而它们本来是完全合法的实现。
        kwargs = {"max_tokens": max_tokens}
        if deadline is not None:
            kwargs["deadline"] = deadline
        text = self.complete(system_prompt, user_prompt, **kwargs)
        return extract_json(text)

    def stats(self):
        """调用成败统计。子类可以覆盖，默认是「没统计」。"""
        return {"calls": 0, "failures": 0, "last_error": "", "healthy": None}

    @property
    def available(self):
        return True


class NullProvider(LLMProvider):
    """未配置大模型时使用的空实现。"""

    name = "null"

    def complete(self, system_prompt, user_prompt, max_tokens=None,
                 deadline=None):
        raise LLMUnavailable("未配置大模型，当前运行在纯规则降级模式")

    @property
    def available(self):
        return False


_JSON_BLOCK = re.compile(r"```(?:json)?\s*(.+?)\s*```", re.S)


def extract_json(text):
    """从模型输出里提取第一个 JSON 对象，失败返回 None。"""
    if not text:
        return None
    text = text.strip()

    # 优先取 ```json ... ``` 代码块里的内容
    block = _JSON_BLOCK.search(text)
    if block:
        text = block.group(1).strip()

    try:
        return json.loads(text)
    except (ValueError, TypeError):
        pass

    # 退一步：截取第一个 { 到最后一个 } 之间的内容
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end > start:
        try:
            return json.loads(text[start:end + 1])
        except (ValueError, TypeError):
            return None
    return None
