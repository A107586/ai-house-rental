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

    def complete(self, system_prompt, user_prompt, max_tokens=None):
        """返回模型输出的纯文本。不可用时抛 `LLMUnavailable`。"""
        raise NotImplementedError

    def complete_json(self, system_prompt, user_prompt, max_tokens=None):
        """要求模型输出 JSON，并解析为 dict。

        模型经常在 JSON 外面裹一层 ```json 代码块或解释文字，
        这里统一做容错提取，避免每个调用点重复处理。
        """
        text = self.complete(system_prompt, user_prompt, max_tokens=max_tokens)
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

    def complete(self, system_prompt, user_prompt, max_tokens=None):
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
